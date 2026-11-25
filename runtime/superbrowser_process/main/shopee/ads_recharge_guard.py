from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from util import yingdao_util, database_credentials
from util.dingtalk_message_util import send_text_msg
from util.webdriver_util import WebdriverUtil
from implement.shopee.ads_recharge_rules import SITE_BALANCE_SUCCESS_TOLERANCE


BUILTIN_DB_PASSWORD = ""

GUARD_DB_CONFIG = {}
GUARD_DB_FALLBACK_HOST = ""
DEFAULT_DAILY_LIMIT = Decimal("999999999")
DEFAULT_ALLOWED_IP = "1.1.1.28"
DEFAULT_TOLERANCE = Decimal("1")
FALLBACK_SUCCESS_MIN_BALANCE = Decimal("300")
SUCCESS_STATUSES = {"success", "completed", "corrected_success"}
CHECKABLE_STATUSES = SUCCESS_STATUSES | {"failed"}
_GUARD_TABLES_LOCK = threading.Lock()
_GUARD_TABLES_READY = False
BALANCE_BEFORE_CHECK_ATTEMPTS = 3
BALANCE_BEFORE_CHECK_TIMEOUT_SECONDS = 12
BALANCE_BEFORE_RETRY_DELAY_SECONDS = 2
BALANCE_CONFIRM_ATTEMPTS = 2
BALANCE_CONFIRM_RETRY_DELAY_SECONDS = 45
BALANCE_PROCESSING_INITIAL_DELAY_SECONDS = 45

SITE_DAILY_LIMITS = {}
QUOTA_LIMITED_SITES = set()

SITE_NAMES = {
    "th": "泰国",
    "ph": "菲律宾",
    "my": "马来",
    "id": "印尼",
    "vn": "越南",
}

SITE_CURRENCIES = {
    "th": "THB",
    "ph": "PHP",
    "my": "MYR",
    "id": "IDR",
    "vn": "VND",
}


def get_site_daily_limit(site_code: str) -> Decimal:
    return SITE_DAILY_LIMITS.get(site_code, DEFAULT_DAILY_LIMIT)


def is_quota_limited_site(site_code: str) -> bool:
    return site_code in QUOTA_LIMITED_SITES

OVER_LIMIT_RECIPIENTS = {}


class GuardError(RuntimeError):
    pass


class PermissionDenied(GuardError):
    pass


class QuotaExceeded(GuardError):
    def __init__(self, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.detail = detail or {}


@dataclass
class PermissionResult:
    ip: str
    computer_name: str
    windows_username: str
    allowed_sites: str
    allow_dingtalk_recharge: bool
    allow_custom_recharge: bool


@dataclass
class QuotaReservation:
    site_code: str
    quota_date: date
    amount: Decimal
    enabled: bool = True


class RechargeGuardContext:
    def __init__(self, recharge_module: Any, original_recharge_ads: Any, contexts: dict[str, dict[str, Any]]):
        self.recharge_module = recharge_module
        self.original_recharge_ads = original_recharge_ads
        self._contexts = contexts
        self._lock = threading.Lock()

    def pop(self, store_name: str) -> dict[str, Any]:
        with self._lock:
            return self._contexts.pop(store_name, {})

    def put(self, store_name: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._contexts[store_name] = value

    def restore(self) -> None:
        self.recharge_module.recharge_ads = self.original_recharge_ads


def decimal_amount(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value)).quantize(Decimal("0.01"))


def get_balance_success_tolerance(site_code: str) -> Decimal:
    return SITE_BALANCE_SUCCESS_TOLERANCE.get(str(site_code or "").lower(), FALLBACK_SUCCESS_MIN_BALANCE)


def _read_balance_before_with_retries(
    recharge_module: Any,
    webdriver_util: WebdriverUtil,
    store_name: str,
    *,
    attempts: int = BALANCE_BEFORE_CHECK_ATTEMPTS,
    timeout: int = BALANCE_BEFORE_CHECK_TIMEOUT_SECONDS,
    retry_delay_seconds: int = BALANCE_BEFORE_RETRY_DELAY_SECONDS,
    log: Any = None,
) -> Decimal | None:
    last_error = ""
    total_attempts = max(int(attempts or 1), 1)
    for attempt in range(1, total_attempts + 1):
        try:
            _, balance, balance_error = recharge_module._wait_for_ads_credit(
                webdriver_util,
                store_name,
                timeout=timeout,
            )
            if not balance_error:
                return decimal_amount(balance)
            last_error = str(balance_error)
        except Exception as exc:
            last_error = str(exc)

        if log:
            log.warning(f"{store_name}: 充值前读取广告余额第 {attempt}/{total_attempts} 次失败: {last_error}")
        if attempt >= total_attempts:
            break

        try:
            recharge_module.close_alert(webdriver_util)
        except Exception:
            pass
        try:
            driver = getattr(webdriver_util, "driver", None)
            if driver is not None:
                driver.refresh()
        except Exception as refresh_exc:
            if log:
                log.warning(f"{store_name}: 刷新广告余额页失败，继续重试: {refresh_exc}")
        if retry_delay_seconds > 0:
            time.sleep(retry_delay_seconds)

    if log:
        log.warning(f"{store_name}: 充值前读取广告余额最终失败，继续执行额度校验: {last_error}")
    return None


def current_machine_context() -> dict[str, str]:
    try:
        ip = yingdao_util.get_ip()
    except Exception:
        ip = ""
    return {
        "ip": ip,
        "computer_name": socket.gethostname(),
        "windows_username": os.environ.get("USERNAME") or os.environ.get("USER") or "",
    }


def get_guard_connection():
    from backend.ads_database import connect_runtime_database
    return connect_runtime_database(guard=True)


def ensure_guard_tables() -> None:
    global _GUARD_TABLES_READY
    if _GUARD_TABLES_READY:
        return
    with _GUARD_TABLES_LOCK:
        if _GUARD_TABLES_READY:
            return
        _ensure_guard_tables_once()
        _GUARD_TABLES_READY = True


def _ensure_guard_tables_once():
    from backend.ads_database import ensure_daily_quotas
    ensure_daily_quotas(SITE_CURRENCIES, get_site_daily_limit)


def check_permission(site_code, recharge_mode):
    from backend.ads_database import authorized_machine
    return PermissionResult(**authorized_machine(current_machine_context(), site_code, recharge_mode))


def get_today_quota(site_code: str) -> dict[str, Any]:
    ensure_guard_tables()
    today = date.today()
    with get_guard_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                '\n                SELECT *\n                FROM tbl_fin_ads_recharge_site_quota\n                WHERE tenant_id = 1 AND NOT archived AND site_code = %s AND quota_date = %s\n                ',
                (site_code, today),
            )
            row = cur.fetchone()
    if not row:
        raise GuardError(f"未配置 {SITE_NAMES.get(site_code, site_code)} 今日广告充值额度")
    row["remaining_amount"] = decimal_amount(row["daily_limit"]) - decimal_amount(row["used_amount"]) - decimal_amount(row["reserved_amount"])
    return dict(row)


def reserve_quota(site_code: str, amount: Any, enable_quota_limit: bool = True) -> QuotaReservation:
    amount_dec = decimal_amount(amount)
    if not enable_quota_limit or not is_quota_limited_site(site_code) or amount_dec <= 0:
        return QuotaReservation(site_code=site_code, quota_date=date.today(), amount=amount_dec, enabled=False)

    ensure_guard_tables()
    today = date.today()
    with get_guard_connection() as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    '\n                    SELECT *\n                    FROM tbl_fin_ads_recharge_site_quota\n                    WHERE tenant_id = 1 AND NOT archived AND site_code = %s AND quota_date = %s\n                    FOR UPDATE\n                    ',
                    (site_code, today),
                )
                row = cur.fetchone()
                if not row:
                    raise GuardError(f"未配置 {SITE_NAMES.get(site_code, site_code)} 今日广告充值额度")
                if not row.get("enabled"):
                    raise GuardError(f"{SITE_NAMES.get(site_code, site_code)} 今日广告充值额度已停用")

                daily_limit = decimal_amount(row["daily_limit"])
                used_amount = decimal_amount(row["used_amount"])
                reserved_amount = decimal_amount(row["reserved_amount"])
                remaining = daily_limit - used_amount - reserved_amount
                if remaining < amount_dec:
                    detail = {
                        "site_code": site_code,
                        "quota_date": str(today),
                        "daily_limit": daily_limit,
                        "used_amount": used_amount,
                        "reserved_amount": reserved_amount,
                        "remaining_amount": remaining,
                        "request_amount": amount_dec,
                    }
                    raise QuotaExceeded(
                        f"{SITE_NAMES.get(site_code, site_code)} 今日广告充值额度不足，剩余额度 {remaining}，本次需要 {amount_dec}",
                        detail,
                    )
                cur.execute(
                    '\n                    UPDATE tbl_fin_ads_recharge_site_quota\n                    SET reserved_amount = reserved_amount + %s, updated_on = NOW(), version = version + 1\n                    WHERE tenant_id = 1 AND NOT archived AND site_code = %s AND quota_date = %s\n                    ',
                    (amount_dec, site_code, today),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return QuotaReservation(site_code=site_code, quota_date=today, amount=amount_dec, enabled=True)


def confirm_quota(reservation: QuotaReservation | None) -> None:
    if not reservation or not reservation.enabled or reservation.amount <= 0:
        return
    with get_guard_connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    '\n                    UPDATE tbl_fin_ads_recharge_site_quota\n                    SET reserved_amount = GREATEST(reserved_amount - %s, 0),\n                        used_amount = used_amount + %s,\n                        updated_on = NOW(), version = version + 1\n                    WHERE tenant_id = 1 AND NOT archived AND site_code = %s AND quota_date = %s\n                    ',
                    (reservation.amount, reservation.amount, reservation.site_code, reservation.quota_date),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def release_quota(reservation: QuotaReservation | None) -> None:
    if not reservation or not reservation.enabled or reservation.amount <= 0:
        return
    with get_guard_connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    '\n                    UPDATE tbl_fin_ads_recharge_site_quota\n                    SET reserved_amount = GREATEST(reserved_amount - %s, 0), updated_on = NOW(), version = version + 1\n                    WHERE tenant_id = 1 AND NOT archived AND site_code = %s AND quota_date = %s\n                    ',
                    (reservation.amount, reservation.site_code, reservation.quota_date),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def create_run_log(app_name: str, site_code: str, ziniao_username: str, recharge_mode: str, total_stores: int = 0) -> str:
    ensure_guard_tables()
    ctx = current_machine_context()
    run_id = uuid.uuid4().hex
    with get_guard_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                '\n                INSERT INTO tbl_fin_ads_recharge_run_log (\n                    run_id, app_name, site_code, ip, computer_name, windows_username,\n                    ziniao_username, recharge_mode, started_at, total_stores\n                )\n                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)\n                ',
                (
                    run_id,
                    app_name,
                    site_code,
                    ctx["ip"],
                    ctx["computer_name"],
                    ctx["windows_username"],
                    ziniao_username,
                    recharge_mode,
                    datetime.now(),
                    total_stores,
                ),
            )
        conn.commit()
    return run_id


def finish_run_log(run_id: str, status: str, records: list[dict], message: str = "") -> None:
    if not run_id:
        return
    failed_count = sum(1 for item in records if str(item.get("status")) == "failed")
    processing_count = sum(1 for item in records if str(item.get("status")) == "processing")
    skipped_count = sum(1 for item in records if str(item.get("status", "")).startswith("skipped"))
    success_count = sum(1 for item in records if str(item.get("status")) in SUCCESS_STATUSES)
    total_success_amount = sum(
        decimal_amount(item.get("recharge_amount"))
        for item in records
        if str(item.get("status")) in SUCCESS_STATUSES
    )
    with get_guard_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                '\n                UPDATE tbl_fin_ads_recharge_run_log\n                SET ended_at = NOW(),\n                    status = %s,\n                    total_stores = %s,\n                    success_count = %s,\n                    failed_count = %s,\n                    processing_count = %s,\n                    skipped_count = %s,\n                    total_success_amount = %s,\n                    message = %s,\n                    updated_on = NOW(), version = version + 1\n                WHERE tenant_id = 1 AND NOT archived AND run_id = %s\n                ',
                (
                    status,
                    len(records),
                    success_count,
                    failed_count,
                    processing_count,
                    skipped_count,
                    total_success_amount,
                    message,
                    run_id,
                ),
            )
        conn.commit()


def write_store_log(run_id: str, site_code: str, record: dict[str, Any]) -> None:
    if not run_id:
        return
    with get_guard_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                '\n                INSERT INTO tbl_fin_ads_recharge_store_log (\n                    run_id, site_code, store_name, recharge_amount, balance_before, balance_after,\n                    status, message, retry_balance_check, corrected_to_success, quota_reserved,\n                    started_at, ended_at\n                )\n                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)\n                ',
                (
                    run_id,
                    site_code,
                    str(record.get("store_name") or ""),
                    decimal_amount(record.get("recharge_amount")),
                    record.get("balance_before"),
                    record.get("balance_after"),
                    str(record.get("status") or ""),
                    str(record.get("message") or ""),
                    bool(record.get("retry_balance_check")),
                    bool(record.get("corrected_to_success")),
                    bool(record.get("quota_reserved")),
                    record.get("started_at"),
                    record.get("ended_at"),
                ),
            )
        conn.commit()


def send_over_limit_notification(
    site_code: str,
    detail: dict[str, Any],
    message_token: str,
    message_secret: str,
    enabled: bool = True,
) -> None:
    if not enabled or not message_token or not message_secret:
        return
    recipients = "、".join(OVER_LIMIT_RECIPIENTS.get(site_code, ["站点负责人"]))
    site_name = SITE_NAMES.get(site_code, site_code)
    ctx = current_machine_context()
    message = (
        f"Shopee广告充值超额提醒\n"
        f"站点: {site_name}\n"
        f"通知人: {recipients}\n"
        f"电脑IP: {ctx['ip']}\n"
        f"电脑名: {ctx['computer_name']}\n"
        f"本次店铺: {detail.get('store_name', '-')}\n"
        f"本次充值金额: {detail.get('request_amount', '-')}\n"
        f"今日额度: {detail.get('daily_limit', '-')}\n"
        f"已用额度: {detail.get('used_amount', '-')}\n"
        f"预占额度: {detail.get('reserved_amount', '-')}\n"
        f"剩余额度: {detail.get('remaining_amount', '-')}\n"
        f"触发时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    send_text_msg(message, message_token, message_secret)


def install_recharge_quota_guard(
    recharge_module: Any,
    site_code: str,
    enable_quota_limit: bool,
    log: Any = None,
) -> RechargeGuardContext:
    original_recharge_ads = recharge_module.recharge_ads
    guard_context = RechargeGuardContext(recharge_module, original_recharge_ads, {})

    def guarded_recharge_ads(webdriver_util: WebdriverUtil, store_name: str, recharge_amount: Any):
        amount_dec = decimal_amount(recharge_amount)
        balance_before = None
        reservation = None
        try:
            balance_before = _read_balance_before_with_retries(
                recharge_module,
                webdriver_util,
                store_name,
                log=log,
            )

            reservation = reserve_quota(site_code, amount_dec, enable_quota_limit=enable_quota_limit)
            guard_context.put(
                store_name,
                {
                    "reservation": reservation,
                    "recharge_amount": amount_dec,
                    "balance_before": balance_before,
                    "quota_reserved": bool(reservation and reservation.enabled),
                },
            )
            return original_recharge_ads(webdriver_util, store_name, recharge_amount)
        except Exception:
            if reservation:
                try:
                    release_quota(reservation)
                except Exception as release_exc:
                    if log:
                        log.error(f"{store_name}: 释放预占额度失败: {release_exc}")
            raise

    recharge_module.recharge_ads = guarded_recharge_ads
    return guard_context


def query_ads_credit(recharge_module: Any, driver: Any, store_name: str, default_url: str, timeout: int = 30) -> Decimal | None:
    webdriver_util = WebdriverUtil(driver)
    target_url = getattr(recharge_module, "ads_wallet_url", "") or default_url
    if "仓发" in store_name:
        target_url = "https://seller.shopee.cn/portal/marketing/pas/index"
    webdriver_util.go_to(target_url)
    time.sleep(5)
    try:
        recharge_module.close_alert(webdriver_util)
    except Exception:
        pass
    _, amount, error = recharge_module._wait_for_ads_credit(webdriver_util, store_name, timeout=timeout)
    if error:
        return None
    return decimal_amount(amount)


def force_close_current_store(driver: Any, store_name: str, log: Any = None) -> None:
    loader = getattr(driver, "_ziniao_loader", None)
    browser_oauth = getattr(driver, "_ziniao_browser_oauth", None)
    current_store_id = getattr(driver, "_ziniao_current_store_id", None) or browser_oauth

    if loader is not None:
        try:
            loader.close_driver_window_safely(driver, store_name, timeout=3)
        except Exception as exc:
            if log:
                log.warning(f"{store_name}: timeout close current driver failed: {exc}")
        if current_store_id:
            try:
                loader.close_store(current_store_id)
            except Exception as exc:
                if log:
                    log.warning(f"{store_name}: timeout stop current ziniao browser failed: {exc}")
        try:
            loader.quit_driver_safely(driver, store_name, timeout=3)
        except Exception as exc:
            if log:
                log.warning(f"{store_name}: timeout quit current driver failed: {exc}")
        return

    try:
        driver.quit()
    except Exception as exc:
        if log:
            log.warning(f"{store_name}: timeout quit current driver failed: {exc}")


def query_ads_credit_with_reopened_store(
    recharge_module: Any,
    driver: Any,
    store_name: str,
    default_url: str,
    timeout: int = 30,
    log: Any = None,
) -> Decimal | None:
    loader = getattr(driver, "_ziniao_loader", None)
    browser_oauth = getattr(driver, "_ziniao_browser_oauth", None)
    current_store_id = getattr(driver, "_ziniao_current_store_id", None) or browser_oauth

    if loader is None or not browser_oauth:
        if log:
            log.warning(f"{store_name}: missing ziniao context, fallback to current driver for balance retry")
        return query_ads_credit(recharge_module, driver, store_name, default_url, timeout=timeout)

    new_driver = None
    new_store_id = None
    try:
        if log:
            log.info(f"{store_name}: reopen store for retry balance check")
        try:
            loader.close_driver_window_safely(driver, store_name, timeout=3)
        except Exception as exc:
            if log:
                log.warning(f"{store_name}: close current driver before retry failed: {exc}")
        try:
            loader.close_store(current_store_id)
        except Exception as exc:
            if log:
                log.warning(f"{store_name}: stop current ziniao browser before retry failed: {exc}")

        ret_json = loader.open_store(browser_oauth)
        if log:
            log.info(ret_json)
        new_store_id = ret_json.get("browserOauth") or ret_json.get("browserId") or browser_oauth
        new_driver = loader.get_driver(ret_json)
        if new_driver is None:
            raise RuntimeError("reopen store failed: webdriver is None")
        new_driver.implicitly_wait(20)
        try:
            loader._move_window_offscreen(new_driver, store_name)
        except Exception:
            pass
        ip_check_url = ret_json.get("ipDetectionPage")
        if ip_check_url and not loader.open_ip_check(new_driver, ip_check_url) and log:
            log.warning(f"{store_name}: retry balance check ip check failed, continue to ads wallet page")
        return query_ads_credit(recharge_module, new_driver, store_name, default_url, timeout=timeout)
    finally:
        if new_driver is not None:
            try:
                loader.close_driver_window_safely(new_driver, store_name, timeout=3)
            except Exception:
                pass
        if new_store_id:
            try:
                loader.close_store(new_store_id)
            except Exception as exc:
                if log:
                    log.warning(f"{store_name}: stop reopened ziniao browser failed: {exc}")
        if new_driver is not None:
            try:
                loader.quit_driver_safely(new_driver, store_name, timeout=5)
            except Exception:
                pass


def should_correct_to_success(
    balance_before: Any,
    balance_after: Any,
    recharge_amount: Any,
    tolerance: Any = DEFAULT_TOLERANCE,
) -> bool:
    if balance_after is None:
        return False
    amount_dec = decimal_amount(recharge_amount)
    tolerance_dec = decimal_amount(tolerance)
    if balance_before is None:
        return False
    balance_delta = decimal_amount(balance_after) - decimal_amount(balance_before)
    if balance_delta <= 0:
        return False
    return abs(balance_delta - amount_dec) <= tolerance_dec


def verify_recharge_status_with_balance_check(
    *,
    status: Any,
    message: Any,
    recharge_amount: Any,
    balance_before: Any,
    balance_after: Any,
    site_code: str,
    store_name: str,
    recharge_module: Any,
    driver: Any,
    default_ads_wallet_url: str,
    log: Any = None,
    run_with_timeout: Any = None,
    query_timeout: int = 30,
    timeout_seconds: int = 90,
    balance_check_attempts: int = BALANCE_CONFIRM_ATTEMPTS,
    balance_check_retry_delay_seconds: int = BALANCE_CONFIRM_RETRY_DELAY_SECONDS,
    processing_initial_delay_seconds: int = BALANCE_PROCESSING_INITIAL_DELAY_SECONDS,
) -> dict[str, Any]:
    status_text = str(status or "completed")
    message_text = str(message or "任务执行完成")
    amount_dec = decimal_amount(recharge_amount)
    retry_balance_check = False
    corrected_to_success = False

    if amount_dec <= 0:
        return {
            "status": status_text,
            "message": message_text,
            "balance_after": balance_after,
            "retry_balance_check": retry_balance_check,
            "corrected_to_success": corrected_to_success,
        }

    initial_processing = status_text == "processing"
    needs_check = status_text in CHECKABLE_STATUSES or initial_processing
    if not needs_check:
        return {
            "status": status_text,
            "message": message_text,
            "balance_after": balance_after,
            "retry_balance_check": retry_balance_check,
            "corrected_to_success": corrected_to_success,
        }

    retry_balance_check = True
    initial_success = status_text in SUCCESS_STATUSES
    timeout_message = (
        f"{store_name}: 支付提交后余额查询超过 {timeout_seconds} 秒，保留处理中状态"
        if initial_processing
        else (
            f"{store_name}: 充值成功后二次余额查询超过 {timeout_seconds} 秒，按失败处理"
            if initial_success
            else f"{store_name}: 二次余额查询超过 {timeout_seconds} 秒，按失败处理"
        )
    )

    success_tolerance = get_balance_success_tolerance(site_code)
    try:
        confirmed = False
        check_error = None
        total_attempts = max(int(balance_check_attempts or 1), 1)
        for attempt in range(1, total_attempts + 1):
            delay_seconds = (
                processing_initial_delay_seconds
                if initial_processing and attempt == 1
                else balance_check_retry_delay_seconds if attempt > 1 else 0
            )
            if delay_seconds > 0:
                if log:
                    log.info(
                        f"{store_name}: 余额复查第 {attempt}/{total_attempts} 次，"
                        f"等待 {delay_seconds} 秒后再次确认"
                    )
                time.sleep(delay_seconds)

            def query_fresh_balance():
                return query_ads_credit_with_reopened_store(
                    recharge_module,
                    driver,
                    store_name,
                    default_ads_wallet_url,
                    timeout=query_timeout,
                    log=log,
                )

            try:
                checked_balance_after = (
                    run_with_timeout(query_fresh_balance, timeout_seconds, timeout_message, log)
                    if run_with_timeout
                    else query_fresh_balance()
                )
                check_error = None
            except Exception as attempt_exc:
                check_error = attempt_exc
                if log:
                    log.warning(f"{store_name}: 余额复查第 {attempt}/{total_attempts} 次失败: {attempt_exc}")
                continue

            if checked_balance_after is not None:
                balance_after = checked_balance_after
                if log:
                    log.info(f"{store_name}: 余额复查第 {attempt}/{total_attempts} 次结果: {balance_after}")

            confirmed = should_correct_to_success(
                balance_before,
                balance_after,
                amount_dec,
                tolerance=success_tolerance,
            )
            if confirmed:
                break

        if not confirmed and check_error is not None and balance_after is None:
            raise check_error

        if confirmed:
            if initial_processing:
                status_text = "corrected_success"
                corrected_to_success = True
                message_text = (
                    f"支付已提交，延迟余额复查确认到账。before={balance_before}, "
                    f"after={balance_after}, amount={amount_dec}. original={message_text}"
                )
            elif initial_success:
                message_text = (
                    f"页面提示成功，余额复查确认到账。before={balance_before}, "
                    f"after={balance_after}, amount={amount_dec}. original={message_text}"
                )
            else:
                status_text = "corrected_success"
                corrected_to_success = True
                message_text = (
                    f"初始结果失败，余额复查确认到账。before={balance_before}, "
                    f"after={balance_after}, amount={amount_dec}. original={message_text}"
                )
        else:
            if initial_processing:
                status_text = "processing"
                message_text = (
                    f"支付已提交，延迟余额复查尚未确认到账，保留处理中，请勿重复支付。"
                    f"before={balance_before}, after={balance_after}, amount={amount_dec}, "
                    f"tolerance={success_tolerance}. original={message_text}"
                )
            elif initial_success:
                status_text = "failed"
                message_text = (
                    f"页面提示成功，但余额复查未确认到账，已改为失败。before={balance_before}, "
                    f"after={balance_after}, amount={amount_dec}, tolerance={success_tolerance}. "
                    f"original={message_text}"
                )
            else:
                message_text = (
                    f"{message_text}; retry balance check not corrected. before={balance_before}, "
                    f"after={balance_after}, amount={amount_dec}, tolerance={success_tolerance}"
                )
    except Exception as check_exc:
        if initial_processing:
            status_text = "processing"
            message_text = (
                f"支付已提交，延迟余额复查异常，保留处理中，请勿重复支付："
                f"{check_exc}; original={message_text}"
            )
        elif initial_success:
            status_text = "failed"
            message_text = f"页面提示成功，但余额复查失败，已改为失败：{check_exc}; original={message_text}"
        else:
            message_text = f"{message_text}; retry balance check failed: {check_exc}"

    return {
        "status": status_text,
        "message": message_text,
        "balance_after": balance_after,
        "retry_balance_check": retry_balance_check,
        "corrected_to_success": corrected_to_success,
    }
