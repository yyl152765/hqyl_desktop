"""Offline reproduction of the current VN recharge retry/batch control flow.

No business module imports: selected functions are compiled from their source AST.
All browser, recharge, quota, log persistence and balance-verification operations
are fake. Payment events below are counters only, never financial transactions.
The store-lock decorator is omitted because execution uses one worker and unique
fake stores; this script does not test cross-process locking or real payment UI.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2] / "superbrowser_process"
LOADER_SOURCE = ROOT / "main" / "super_browser_desktop.py"
VN_SOURCE = ROOT / "main" / "shopee" / "vn_shopee_ads_recharge_operator_service.py"


def _node(tree, name, kind=ast.FunctionDef):
    found = [n for n in ast.walk(tree) if isinstance(n, kind) and n.name == name]
    if len(found) != 1:
        raise AssertionError(f"Expected one {name}; got {len(found)}")
    return copy.deepcopy(found[0])


def _compile(nodes, namespace, filename):
    tree = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(tree, str(filename), "exec"), namespace)


class FakeLog:
    def __init__(self):
        self.messages = []

    def info(self, text, *args):
        self.messages.append(text % args if args else text)

    warning = info
    error = info


class FakeDriver:
    def set_page_load_timeout(self, seconds):
        pass

    set_script_timeout = set_page_load_timeout
    implicitly_wait = set_page_load_timeout


class QuotaExceeded(Exception):
    pass


class StoreTaskTimeout(TimeoutError):
    pass


def _run_case(loader_tree, vn_tree, scenario):
    events = []
    records = []
    session_failed = threading.Event()
    log = FakeLog()

    def event(action, store, **detail):
        events.append({"action": action, "store": store, **detail})

    def fake_recharge(driver, store, output_path):
        event("recharge_entry", store, attempt=driver._ziniao_attempt_index)
        if store == "VN-B":
            return {"status": "completed", "message": "Fake next store completed"}
        if scenario == "raw_connection_reset":
            raise RuntimeError("net::ERR_CONNECTION_RESET")
        if scenario == "session_exception":
            raise RuntimeError("no such window: target window already closed")
        if scenario == "session_failed_result":
            return {"status": "failed", "message": "invalid session id"}
        if scenario == "ordinary_exception":
            raise RuntimeError("simulated ordinary task failure")
        if scenario == "fake_payment_then_session_loss":
            event("fake_payment_submission", store, attempt=driver._ziniao_attempt_index)
            raise RuntimeError("no such window: target window already closed")
        raise AssertionError(f"Unknown scenario {scenario}")

    def fake_verification(**kw):
        event("fake_balance_verification", kw["store_name"])
        return {
            "status": kw["status"], "message": kw["message"],
            "balance_after": kw["balance_after"],
            "retry_balance_check": False, "corrected_to_success": False,
        }

    def fake_timeout(callback, *args):
        return callback()

    recharge_module = SimpleNamespace(
        vn_shopee_ads_recharge=fake_recharge,
        _write_error=lambda *args: None,
        _write_success=lambda *args: None,
    )
    guard = SimpleNamespace(
        QuotaExceeded=QuotaExceeded,
        force_close_current_store=lambda *args: None,
        verify_recharge_status_with_balance_check=fake_verification,
        write_store_log=lambda *args: None,
        release_quota=lambda *args: None,
        confirm_quota=lambda *args: None,
    )
    namespace = {
        "datetime": datetime, "log": log, "quota_stop_event": threading.Event(),
        "browser_session_failure_event": session_failed,
        "append_process_record": records.append, "guard_context": None,
        "recharge_mode": "sheet", "recharge_module": recharge_module,
        "ads_recharge_guard": guard, "StoreTaskTimeout": StoreTaskTimeout,
        "_run_with_timeout": fake_timeout, "store_task_timeout_seconds": 240,
        "_find_sheet_recharge_amount": lambda *args: "0",
        "sheet_recharge_amount_map": {}, "SITE_CODE": "VN", "run_id": "fake-run",
        "DEFAULT_ADS_WALLET_URL": "https://example.invalid/fake-wallet",
        "is_success_status": lambda status: status in ("completed", "success"),
        "append_success_recharge_log": lambda *args, **kwargs: None,
        "pcfg": {}, "username": "offline-fake", "success_log_sheet_names": set(),
        "success_log_sheet_names_lock": threading.Lock(),
        "ThreadPoolExecutor": ThreadPoolExecutor, "partial": partial,
        "time": SimpleNamespace(sleep=lambda seconds: None),
        "_safe_response_summary": lambda value: "fake-response",
    }
    markers = next(
        node for node in vn_tree.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_BROWSER_SESSION_ERROR_MARKERS"
                for target in node.targets)
    )
    namespace["_BROWSER_SESSION_ERROR_MARKERS"] = ast.literal_eval(markers.value)
    tracked = _node(vn_tree, "tracked_vn_shopee_ads_recharge")
    tracked.decorator_list = []
    factory = ast.parse("def _make_tracked():\n    over_limit_notified = False\n").body[0]
    factory.body.extend([tracked, ast.Return(value=ast.Name(id=tracked.name, ctx=ast.Load()))])
    _compile([_node(vn_tree, "_is_browser_session_error"), factory], namespace, VN_SOURCE)
    _compile([
        _node(loader_tree, "use_one_browser_run_task"),
        _node(loader_tree, "use_all_browser_run_task_with_thread_pool"),
    ], namespace, LOADER_SOURCE)

    class FakeLoader:
        use_one_browser_run_task = namespace["use_one_browser_run_task"]
        use_all_browser_run_task_with_thread_pool = namespace["use_all_browser_run_task_with_thread_pool"]

        def __init__(self):
            self.log = log
            self.max_threads = 1
            self.user_info = {}
            self.socket_port = 0
            self.driver_folder_path = "fake"
            self.task_list = ["vn_shopee_ads_recharge"]
            self.func_dict = {"vn_shopee_ads_recharge": namespace["_make_tracked"]()}

        def open_store(self, store_id):
            event("open_store", store_id)
            return {"browserOauth": store_id, "ipDetectionPage": "fake-ip-check",
                    "launcherPage": "fake-launcher", "downloadPath": "fake/"}

        def get_driver(self, response):
            return FakeDriver()

        def close_store(self, store_id):
            event("close_store", store_id)

        def _move_window_offscreen(self, driver, store):
            pass

        def open_ip_check(self, *args):
            return True

        def open_launcher_page(self, *args):
            pass

        def _restore_window_onscreen(self, driver, store):
            event("restore_window", store)

        def close_driver_window_safely(self, driver, store):
            event("close_driver_window", store)

        def quit_driver_safely(self, driver, store):
            event("quit_driver", store)

    FakeLoader().use_all_browser_run_task_with_thread_pool([
        {"browserName": name, "browserOauth": name} for name in ("VN-A", "VN-B")
    ])
    opens = [e["store"] for e in events if e["action"] == "open_store"]
    retry_expected = scenario in ("session_exception", "session_failed_result", "fake_payment_then_session_loss")
    expected_opens = ["VN-A"] * (3 if retry_expected else 1) + ["VN-B"]
    assert opens == expected_opens, (scenario, opens, log.messages)
    assert [r["store_name"] for r in records] == ["VN-A", "VN-B"], (scenario, records)
    assert [r["status"] for r in records] == ["failed", "completed"], (scenario, records)
    assert session_failed.is_set() == retry_expected, (scenario, records)
    assert not namespace["_is_browser_session_error"]("net::ERR_CONNECTION_RESET")
    assert namespace["_is_browser_session_error"]("no such window")
    closed = Counter(e["store"] for e in events if e["action"] == "close_store")
    quits = Counter(e["store"] for e in events if e["action"] == "quit_driver")
    assert closed == Counter(opens) and quits == Counter(opens), events
    payments = [e for e in events if e["action"] == "fake_payment_submission"]
    assert len(payments) == (3 if scenario == "fake_payment_then_session_loss" else 0)
    if scenario == "ordinary_exception":
        assert records[0]["message"] == "simulated ordinary task failure", records
    if scenario == "raw_connection_reset":
        assert records[0]["message"] == "net::ERR_CONNECTION_RESET", records
    return {
        "scenario": scenario, "passed": True, "open_sequence": opens,
        "session_failure_flag": session_failed.is_set(),
        "store_records": [{"store": r["store_name"], "status": r["status"], "message": r["message"]}
                          for r in records],
        "fake_payment_submission_count": len(payments),
        "cleanup_close_store_count": sum(closed.values()),
        "cleanup_quit_driver_count": sum(quits.values()),
        "events": events,
    }


def run():
    loader_text = LOADER_SOURCE.read_text(encoding="utf-8-sig")
    vn_text = VN_SOURCE.read_text(encoding="utf-8-sig")
    loader_tree = ast.parse(loader_text)
    vn_tree = ast.parse(vn_text)
    cases = [_run_case(loader_tree, vn_tree, scenario) for scenario in (
        "raw_connection_reset", "session_exception", "session_failed_result",
        "ordinary_exception", "fake_payment_then_session_loss",
    )]
    return {
        "passed": all(case["passed"] for case in cases), "test_count": len(cases),
        "mode": "offline_source_ast_with_fake_io",
        "sources": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                    for path in (LOADER_SOURCE, VN_SOURCE)],
        "scope": "Current VN wrapper and loader retry/batch control flow; genuine source functions, fake external dependencies",
        "limitations": [
            "Does not reproduce the Electron modal or determine the actual cause of the network reset.",
            "Does not call real browsers, APIs, payment, database, notification or balance verification.",
            "Repeated payment counters prove whole-task re-entry conditional on a post-submission session failure; they do not prove a real duplicate charge.",
            "Store lock and timeout worker behavior are outside this script; one worker and synchronous fake timeout are used.",
        ],
        "cases": cases,
    }


if __name__ == "__main__":
    # ASCII JSON also works in Windows terminals configured for cp950.
    print(json.dumps(run(), ensure_ascii=True, indent=2))
