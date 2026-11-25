"""Cross-process exclusion for one complete VN/ID ads execution.

Only the dedicated PostgreSQL session owns the advisory lock. A timed-out
worker can keep running, so the connection stays open until every registered
worker has actually exited. This module never retries a store action.
"""

from __future__ import annotations

import hashlib
import re
import threading
import unicodedata
from contextvars import ContextVar
from datetime import datetime
from functools import wraps


_ACTIVE_LOCK = ContextVar("shopee_ads_store_execution_lock", default=None)
_SITE_PREFIX = {"vn": "越南", "id": "印尼"}


def get_guard_connection():
    # Avoid loading database/configuration/browser modules merely to import
    # this lock or test its deterministic key and concurrency behavior.
    from main.shopee.ads_recharge_guard import get_guard_connection as connect

    return connect()


def canonical_store_token(site_code: str, store_name: str) -> tuple[str, str]:
    site = str(site_code or "").strip().lower()
    if site not in _SITE_PREFIX or not isinstance(store_name, str):
        raise ValueError("店铺站点或名称无效")
    normalized = unicodedata.normalize("NFKC", store_name).strip()
    tokens = [part.strip() for part in re.split(r"[-‐‑‒–—]", normalized)]
    store_tokens = []
    for token in tokens:
        ordinary = re.fullmatch(r"(?P<country>越南|印尼)[0-9]+", token)
        warehouse = re.fullmatch(
            r"(?P<company>[^\s]+)仓发(?P<country>越南|印尼)[0-9]+", token,
        )
        match = ordinary or warehouse
        if match is not None:
            if warehouse is not None and "仓发" in warehouse.group("company"):
                raise ValueError("仓发店铺名称存在多个公司标记")
            # Keep the complete company+warehouse token. Extracting only
            # 越南001 would merge unrelated companies and ordinary stores.
            store_tokens.append((token, match.group("country")))
    if len(store_tokens) != 1 or store_tokens[0][1] != _SITE_PREFIX[site]:
        raise ValueError("无法唯一确定店铺编号，已停止充值")
    return site, store_tokens[0][0]


def store_advisory_lock_key(site_code: str, store_name: str) -> int:
    site, token = canonical_store_token(site_code, store_name)
    identity = f"hqyl:shopee:ads-store:v1\0{site}\0{token}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(identity).digest()[:8], "big", signed=True)


def _close_connection(connection) -> None:
    if connection is not None:
        try:
            connection.close()
        except Exception:
            # Never leak connection parameters via a cleanup exception.
            pass


class _StoreLockSession:
    def __init__(self, connection, key: int):
        self.connection = connection
        self.key = key
        self.workers = []
        self.mutex = threading.Lock()
        self.released = False

    def register(self, worker) -> None:
        with self.mutex:
            if self.released:
                raise RuntimeError("店铺执行锁已释放，禁止启动后续操作")
            self.workers.append(worker)

    def _release(self) -> None:
        with self.mutex:
            if self.released:
                return
            self.released = True
        try:
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (self.key,))
        except Exception:
            # Closing the dedicated session also releases its session locks.
            pass
        finally:
            _close_connection(self.connection)

    def _wait_and_release(self) -> None:
        while True:
            with self.mutex:
                workers = list(self.workers)
            for worker in workers:
                worker.join()
            with self.mutex:
                # A running child may have registered another nested timeout
                # worker. Check the current list, not just the earlier copy.
                if all(not worker.is_alive() for worker in self.workers):
                    break
        self._release()

    def finish(self) -> None:
        with self.mutex:
            pending = any(worker.is_alive() for worker in self.workers)
        if not pending:
            self._release()
            return
        cleanup = threading.Thread(
            target=self._wait_and_release,
            name="shopee-ads-lock-cleanup",
            daemon=True,
        )
        try:
            cleanup.start()
        except RuntimeError:
            # Resource exhaustion must not unlock a live payment worker.
            self._wait_and_release()


def bind_ads_store_lock_context(action):
    """Explicitly carry the owning lock into a timeout worker thread."""
    session = _ACTIVE_LOCK.get()

    @wraps(action)
    def bound(*args, **kwargs):
        marker = _ACTIVE_LOCK.set(session)
        try:
            return action(*args, **kwargs)
        finally:
            _ACTIVE_LOCK.reset(marker)

    return bound


def register_ads_store_lock_worker(worker) -> None:
    """Call immediately after starting a timeout worker, before joining it."""
    session = _ACTIVE_LOCK.get()
    if session is not None:
        session.register(worker)


def with_ads_store_lock(site_code: str, *, on_blocked=None):
    """Hold one session lock across preparation, payment, and final checking."""
    def decorate(action):
        @wraps(action)
        def locked(driver, store_name: str, *args, **kwargs):
            started_at = datetime.now()

            def blocked(reason: str, message: str):
                result = {
                    "status": "failed",
                    "needs_review": True,
                    "retryable": False,
                    "reason_code": reason,
                    "message": message,
                }
                if on_blocked is not None:
                    on_blocked({
                        **result, "store_name": store_name,
                        "duration_seconds": round((datetime.now() - started_at).total_seconds(), 1),
                        "recharge_amount": "0", "balance_before": None, "balance_after": None,
                        "retry_balance_check": False, "corrected_to_success": False,
                        "quota_reserved": False,
                        "started_at": started_at, "ended_at": datetime.now(),
                    })
                return result

            try:
                key = store_advisory_lock_key(site_code, store_name)
            except ValueError:
                return blocked("invalid_store_identity", "无法唯一确定店铺编号，已停止充值，请核对店铺名称")
            connection = None
            try:
                connection = get_guard_connection()
                # Session locks survive transactions. Autocommit avoids an
                # idle transaction while a browser/payment flow is running.
                connection.autocommit = True
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                    row = cursor.fetchone()
                acquired = isinstance(row, (tuple, list)) and len(row) == 1 and row[0] is True
            except Exception:
                _close_connection(connection)
                return blocked("store_lock_unavailable", "无法取得店铺执行锁，已停止充值，请检查数据库连接")
            if not acquired:
                _close_connection(connection)
                return blocked("store_execution_in_progress", "该店铺已有任务正在执行或等待支付核验，本次未执行，请稍后核对")
            session = _StoreLockSession(connection, key)
            marker = _ACTIVE_LOCK.set(session)
            try:
                return action(driver, store_name, *args, **kwargs)
            finally:
                _ACTIVE_LOCK.reset(marker)
                session.finish()

        return locked

    return decorate
