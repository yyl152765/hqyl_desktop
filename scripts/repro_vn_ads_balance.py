"""Offline fault injection against current VN balance/timeout source functions.

This diagnostic characterizes existing behavior; it does not assert that the
behavior is desirable. No production module initialization or real I/O runs.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import threading
import traceback
from decimal import Decimal
from types import SimpleNamespace
import uuid


ROOT = Path(__file__).resolve().parents[2] / "superbrowser_process"
GUARD = ROOT / "main/shopee/ads_recharge_guard.py"
SERVICE = ROOT / "main/shopee/vn_shopee_ads_recharge_operator_service.py"
LOADER = ROOT / "main/super_browser_desktop.py"
LOCK = ROOT / "main/shopee/ads_store_execution_lock.py"
RULES = ROOT / "implement/shopee/ads_recharge_rules.py"


def extract(path, names, namespace, *, class_name=None):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    body = tree.body
    if class_name:
        body = next(n.body for n in body if isinstance(n, ast.ClassDef) and n.name == class_name)
    selected = []
    found = set()
    for node in body:
        name = getattr(node, "name", None)
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name in names:
            selected.append(node)
            found.add(name)
    assert found == set(names), (path, set(names) - found)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), namespace)


class Log:
    def __init__(self):
        self.messages = []

    def info(self, message, *args):
        self.messages.append(str(message) % args if args else str(message))

    warning = info
    error = info


class Driver:
    def __init__(self, loader, identity):
        self._ziniao_loader = loader
        self._ziniao_browser_oauth = "synthetic-vn-shop"
        self._ziniao_current_store_id = "synthetic-vn-shop"
        self.identity = identity

    def implicitly_wait(self, seconds):
        pass


class Loader:
    def __init__(self, *, fail_close=False):
        self.fail_close = fail_close
        self.user_info = {}
        self.log = Log()
        self.events = []
        self.open_count = 0
        self.pending = 0
        self.peak_pending = 0
        self.mutex = threading.Lock()
        self.release = threading.Event()
        self.first_started = threading.Event()
        self.second_started = threading.Event()
        self.returned = False
        self.completed_after_return = 0
        self.active_checks = 0
        self.peak_active_checks = 0
        self.read_calls = 0
        self.late_read_completions = 0

    def close_driver_window_safely(self, driver, store_name, timeout=3):
        self.events.append({"action": "close_window", "driver": driver.identity, "failed": self.fail_close})
        if self.fail_close:
            raise RuntimeError("synthetic: no such window")

    def send_http(self, payload, timeout):
        assert payload["action"] == "stopBrowser"
        self.events.append({"action": "stopBrowser", "response": "timeout" if self.fail_close else "ok"})
        return None if self.fail_close else {"statusCode": 0}

    def open_store(self, store):
        with self.mutex:
            self.open_count += 1
            number = self.open_count
            self.pending += 1
            self.peak_pending = max(self.peak_pending, self.pending)
            self.events.append({"action": "open_start", "number": number, "pending": self.pending})
        if number == 1:
            self.first_started.set()
        else:
            self.second_started.set()
        try:
            with self.mutex:
                self.events.append({"action": "open_finish", "number": number, "after_verifier_return": self.returned})
                self.completed_after_return += int(self.returned)
            return {"browserOauth": store, "diagnostic_number": number}
        finally:
            with self.mutex:
                self.pending -= 1

    def get_driver(self, response):
        return Driver(self, f"reopened-{response['diagnostic_number']}")

    def _move_window_offscreen(self, driver, store_name):
        pass

    def quit_driver_safely(self, driver, store_name, timeout=5):
        self.events.append({"action": "quit_driver", "driver": driver.identity})


class FakeDB:
    """Emulates only advisory lock ownership; never connects anywhere."""
    def __init__(self):
        self.owners = {}
        self.mutex = threading.Lock()
        self.closed = threading.Event()

    def connect(self):
        db = self

        class Connection:
            autocommit = False

            def cursor(self):
                return Cursor(self)

            def close(self):
                with db.mutex:
                    for key in list(db.owners):
                        if db.owners[key] is self:
                            del db.owners[key]
                db.closed.set()

        class Cursor:
            def __init__(self, connection):
                self.connection = connection
                self.row = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, args):
                key = args[0]
                with db.mutex:
                    if "pg_try_advisory_lock" in sql:
                        acquired = key not in db.owners
                        if acquired:
                            db.owners[key] = self.connection
                        self.row = (acquired,)
                    elif "pg_advisory_unlock" in sql:
                        if db.owners.get(key) is self.connection:
                            del db.owners[key]
                    else:
                        raise AssertionError(sql)

            def fetchone(self):
                return self.row

        return Connection()


def environment():
    # This module imports stdlib only. Its lazy real DB factory is replaced.
    spec = importlib.util.spec_from_file_location("diagnostic_ads_store_lock", LOCK)
    lock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lock)
    database = FakeDB()
    lock.get_guard_connection = database.connect
    workers = []

    def register(worker):
        workers.append(worker)
        lock.register_ads_store_lock_worker(worker)

    namespace = {"Decimal": Decimal, "threading": threading, "traceback": traceback,
                 "time": SimpleNamespace(sleep=lambda seconds: None),
                 "bind_ads_store_lock_context": lock.bind_ads_store_lock_context,
                 "register_ads_store_lock_worker": register}
    extract(RULES, ["SITE_BALANCE_SUCCESS_TOLERANCE"], namespace)
    extract(GUARD, ["DEFAULT_TOLERANCE", "FALLBACK_SUCCESS_MIN_BALANCE", "SUCCESS_STATUSES",
                    "CHECKABLE_STATUSES", "BALANCE_CONFIRM_ATTEMPTS", "BALANCE_CONFIRM_RETRY_DELAY_SECONDS",
                    "BALANCE_PROCESSING_INITIAL_DELAY_SECONDS", "decimal_amount", "get_balance_success_tolerance",
                    "should_correct_to_success", "query_ads_credit_with_reopened_store",
                    "verify_recharge_status_with_balance_check"], namespace)
    extract(SERVICE, ["StoreTaskTimeout", "_run_with_timeout"], namespace)
    close_namespace = {"uuid": uuid, "_safe_response_summary": lambda response: {"statusCode": response.get("statusCode")}}
    extract(LOADER, ["close_store"], close_namespace, class_name="LoadSuperBrowser")
    Loader.close_store = close_namespace["close_store"]
    return namespace, lock, database, workers


def case(*, fail_close=False, confirmed=True, overlap=False, initial_status="success", initial_message="synthetic Payment Successful"):
    namespace, lock, database, workers = environment()
    loader = Loader(fail_close=fail_close)
    driver = Driver(loader, "original")

    def read_balance(*args, **kwargs):
        with loader.mutex:
            loader.read_calls += 1
            number = loader.read_calls
            loader.events.append({"action": "read_balance_start", "number": number})
        if overlap and number == 1:
            # Model an unresponsive navigation/remote read, not a startBrowser
            # HTTP request exceeding its own separate 120-second limit.
            if not loader.release.wait(3):
                raise RuntimeError("diagnostic read gate was not released")
        with loader.mutex:
            loader.late_read_completions += int(loader.returned)
            loader.events.append({"action": "read_balance_finish", "number": number,
                                  "after_verifier_return": loader.returned})
        return Decimal("1780000" if confirmed else "100000")

    namespace["query_ads_credit"] = read_balance
    actual_reopened_query = namespace["query_ads_credit_with_reopened_store"]

    def measured_query(*args, **kwargs):
        with loader.mutex:
            loader.active_checks += 1
            loader.peak_active_checks = max(loader.peak_active_checks, loader.active_checks)
        try:
            return actual_reopened_query(*args, **kwargs)
        finally:
            with loader.mutex:
                loader.active_checks -= 1

    namespace["query_ads_credit_with_reopened_store"] = measured_query
    timing = []

    def accelerated_timeout(action, seconds, message, log):
        timing.append({"production_timeout_seconds": seconds, "test_timeout_seconds": seconds / 1000})
        return namespace["_run_with_timeout"](action, seconds / 1000, message, log)

    if overlap:
        import time
        namespace["time"] = SimpleNamespace(sleep=lambda seconds: time.sleep(seconds / 1000))

    @lock.with_ads_store_lock("vn")
    def verify(_driver, _store):
        return namespace["verify_recharge_status_with_balance_check"](
            status=initial_status, message=initial_message, recharge_amount=1680000,
            balance_before=100000, balance_after=None, site_code="vn", store_name=_store,
            recharge_module=object(), driver=_driver, default_ads_wallet_url="https://example.invalid/ads",
            log=loader.log, run_with_timeout=accelerated_timeout if overlap else namespace["_run_with_timeout"],
        )

    try:
        result = verify(driver, "diagnostic-越南001")
        loader.returned = True
        key = lock.store_advisory_lock_key("vn", "diagnostic-越南001")
        owns_after_return = key in database.owners
        if overlap:
            assert loader.first_started.is_set() and loader.second_started.is_set()
            assert loader.peak_active_checks == 2, loader.events
            assert owns_after_return, "actual store lock must remain held while worker is alive"
            assert result["status"] == "success", result
        else:
            assert loader.open_count == (1 if confirmed else 2), loader.events
            expected = ("corrected_success" if confirmed else "processing") if initial_status == "processing" else ("success" if confirmed else "failed")
            assert result["status"] == expected, result
            assert not owns_after_return
        if fail_close:
            first_open = next(i for i, e in enumerate(loader.events) if e["action"] == "open_start")
            assert any(e["action"] == "stopBrowser" and e["response"] == "timeout" for e in loader.events[:first_open])
    finally:
        loader.release.set()
        for worker in workers:
            worker.join(2)
        assert all(not worker.is_alive() for worker in workers), "diagnostic worker leaked"
        assert database.closed.wait(2), "store lock cleanup did not finish"
        assert not database.owners
    if overlap:
        assert loader.late_read_completions == 1, loader.events
    return {"result_status": result["status"], "open_calls": loader.open_count,
            "peak_simultaneous_open_calls": loader.peak_pending,
            "peak_simultaneous_balance_checks": loader.peak_active_checks,
            "late_balance_read_completions": loader.late_read_completions,
            "lock_held_after_verifier_return": owns_after_return,
            "all_workers_finished": True, "timing_override": timing,
            "events": loader.events, "messages": loader.log.messages}


def run():
    return {"method": "execute unchanged AST-extracted production functions with synthetic dependencies",
            "live_browser": False, "live_payment": False,
            "timing": "ordinary cases skip retry sleep; blocked balance-read case scales 90s timeout/45s delay by 1:1000 and uses an Event gate",
            "sources": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (GUARD, SERVICE, LOADER, LOCK, RULES)},
            "cases": {
                "healthy_success_control": case(),
                "close_timeout_still_reopens": case(fail_close=True),
                "success_but_balance_unconfirmed_reopens_twice": case(fail_close=True, confirmed=False),
                "timed_out_worker_overlaps_next_check_with_real_store_lock": case(overlap=True),
            }, "passed": True}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
