import ast
import logging
from pathlib import Path
import sys
import threading
import traceback
import unittest
from typing import Callable
from unittest.mock import patch


SUPERBROWSER_ROOT = Path(__file__).resolve().parents[2] / "superbrowser_process"
if str(SUPERBROWSER_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERBROWSER_ROOT))

from main.shopee import ads_store_execution_lock as store_lock


class _FakeDatabase:
    def __init__(self):
        self.mutex = threading.Lock()
        self.owners = {}
        self.connections = []
        self.events = []

    def connect(self):
        connection = _FakeConnection(self)
        self.connections.append(connection)
        return connection


class _FakeConnection:
    def __init__(self, database):
        self.database = database
        self.autocommit = False
        self.closed = False
        self.closed_event = threading.Event()
        self.unlock_error = False

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        with self.database.mutex:
            for key, owner in list(self.database.owners.items()):
                if owner is self:
                    del self.database.owners[key]
            self.database.events.append(("close", self))
            self.closed = True
            self.closed_event.set()


class _FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, args):
        database = self.connection.database
        key = args[0]
        with database.mutex:
            if sql == "SELECT pg_try_advisory_lock(%s)":
                acquired = key not in database.owners
                if acquired:
                    database.owners[key] = self.connection
                self.row = (acquired,)
                database.events.append(("acquire", key, acquired))
            elif sql == "SELECT pg_advisory_unlock(%s)":
                if self.connection.unlock_error:
                    raise RuntimeError("password=DO_NOT_EXPOSE")
                assert database.owners.get(key) is self.connection
                del database.owners[key]
                database.events.append(("unlock", key))
            else:
                raise AssertionError(sql)

    def fetchone(self):
        return self.row


class _StoreTaskTimeout(RuntimeError):
    pass


def _load_timeout_function(site):
    source = SUPERBROWSER_ROOT / "main" / "shopee" / f"{site}_shopee_ads_recharge_operator_service.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_run_with_timeout")
    scope = {
        "Callable": Callable, "logging": logging, "threading": threading, "traceback": traceback,
        "StoreTaskTimeout": _StoreTaskTimeout,
        "bind_ads_store_lock_context": store_lock.bind_ads_store_lock_context,
        "register_ads_store_lock_worker": store_lock.register_ads_store_lock_worker,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), scope)
    return scope["_run_with_timeout"]


class AdsStoreExecutionLockTests(unittest.TestCase):
    def setUp(self):
        self.database = _FakeDatabase()
        self.connection_patch = patch.object(store_lock, "get_guard_connection", self.database.connect)
        self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)

    def test_stable_exact_store_identity_across_owner_names(self):
        self.assertEqual(
            store_lock.store_advisory_lock_key("vn", "刘雨嫣-越南072-VN002"),
            store_lock.store_advisory_lock_key("VN", "另一负责人 — 越南０７２ — VN999"),
        )
        self.assertNotEqual(
            store_lock.store_advisory_lock_key("id", "印尼1"),
            store_lock.store_advisory_lock_key("id", "印尼10"),
        )
        key = store_lock.store_advisory_lock_key("vn", "越南072")
        self.assertGreaterEqual(key, -(2**63))
        self.assertLess(key, 2**63)
        for name in ("", "越南072-越南073", "越南072-印尼001", "越南072x"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                store_lock.store_advisory_lock_key("vn", name)

    def test_lock_spans_prepare_payment_and_final_balance_check(self):
        key = store_lock.store_advisory_lock_key("vn", "越南072")
        stages = []

        @store_lock.with_ads_store_lock("vn")
        def execute(_driver, _store_name):
            for stage in ("prepare", "payment", "verify_balance"):
                self.assertIn(key, self.database.owners)
                self.assertTrue(self.database.connections[0].autocommit)
                stages.append(stage)
            return {"status": "success"}

        result = execute(None, "刘雨嫣-越南072-VN002")
        self.assertEqual(result["status"], "success")
        self.assertEqual(stages, ["prepare", "payment", "verify_balance"])
        self.assertNotIn(key, self.database.owners)
        self.assertTrue(self.database.connections[0].closed)

    def test_warehouse_token_keeps_company_identity_and_ignores_separate_staff_prefix(self):
        first = store_lock.store_advisory_lock_key("vn", "负责人甲-諾鋒仓发越南001-VN002")
        renamed_staff = store_lock.store_advisory_lock_key("vn", "负责人乙—諾鋒仓发越南００１—VN003")
        other_company = store_lock.store_advisory_lock_key("vn", "负责人甲-創越仓发越南001-VN002")
        ordinary = store_lock.store_advisory_lock_key("vn", "负责人甲-越南001-VN002")
        self.assertEqual(first, renamed_staff)
        self.assertNotEqual(first, other_company)
        self.assertNotEqual(first, ordinary)
        self.assertEqual(
            store_lock.canonical_store_token("vn", "负责人甲-諾鋒仓发越南001-VN002"),
            ("vn", "諾鋒仓发越南001"),
        )
        for name in (
            "负责人甲-諾鋒仓发印尼001-VN002",
            "諾鋒仓发越南001-印尼001",
            "諾鋒仓发越南001-創越仓发越南001",
            "諾鋒仓发越南001x",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                store_lock.store_advisory_lock_key("vn", name)

    def test_same_store_is_blocked_without_executing_or_retrying(self):
        records = []
        invoked = []

        @store_lock.with_ads_store_lock("vn", on_blocked=records.append)
        def duplicate(_driver, _store_name):
            invoked.append(True)

        @store_lock.with_ads_store_lock("vn")
        def first(_driver, _store_name):
            result = duplicate(None, "另一负责人-越南072-VN002")
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["needs_review"])
            self.assertFalse(result["retryable"])
            self.assertEqual(result["reason_code"], "store_execution_in_progress")

        first(None, "刘雨嫣-越南072-VN002")
        self.assertEqual(invoked, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["store_name"], "另一负责人-越南072-VN002")

    def test_different_stores_can_execute_concurrently(self):
        invoked = []

        @store_lock.with_ads_store_lock("id")
        def second(_driver, _store_name):
            invoked.append(True)

        @store_lock.with_ads_store_lock("id")
        def first(_driver, _store_name):
            second(None, "印尼10")

        first(None, "印尼1")
        self.assertEqual(invoked, [True])
        self.assertEqual(self.database.owners, {})

    def test_database_failure_and_invalid_identity_fail_closed_without_credentials(self):
        invoked = []

        @store_lock.with_ads_store_lock("id")
        def execute(_driver, _store_name):
            invoked.append(True)

        with patch.object(store_lock, "get_guard_connection", side_effect=RuntimeError("password=DO_NOT_EXPOSE")):
            result = execute(None, "印尼049")
        self.assertEqual(result["reason_code"], "store_lock_unavailable")
        self.assertNotIn("DO_NOT_EXPOSE", str(result))
        with patch.object(store_lock, "get_guard_connection") as connection:
            result = execute(None, "unknown")
        connection.assert_not_called()
        self.assertEqual(result["reason_code"], "invalid_store_identity")
        self.assertEqual(invoked, [])

    def test_connection_closes_after_task_exception_and_unlock_failure(self):
        @store_lock.with_ads_store_lock("id")
        def execute(_driver, _store_name):
            self.database.connections[0].unlock_error = True
            raise ValueError("task failed")

        with self.assertRaisesRegex(ValueError, "task failed"):
            execute(None, "印尼049")
        self.assertTrue(self.database.connections[0].closed)
        self.assertEqual(self.database.owners, {})

    def test_both_service_timeout_workers_keep_lock_until_they_really_exit(self):
        for site, name in (("vn", "越南072"), ("id", "印尼049")):
            with self.subTest(site=site):
                run_with_timeout = _load_timeout_function(site)
                release_worker = threading.Event()
                started_worker = threading.Event()
                cleanup_called = []
                invoked = []

                def payment():
                    started_worker.set()
                    release_worker.wait(2)

                @store_lock.with_ads_store_lock(site)
                def execute(_driver, _store_name):
                    with self.assertRaises(_StoreTaskTimeout):
                        run_with_timeout(payment, 0, "timeout", on_timeout=lambda: cleanup_called.append(True))
                    return {"status": "failed", "task_timeout": True}

                @store_lock.with_ads_store_lock(site)
                def duplicate(_driver, _store_name):
                    invoked.append(True)

                try:
                    execute(None, name)
                    self.assertTrue(started_worker.wait(1))
                    owner = self.database.owners[store_lock.store_advisory_lock_key(site, name)]
                    self.assertFalse(owner.closed)
                    result = duplicate(None, name)
                    self.assertEqual(result["reason_code"], "store_execution_in_progress")
                    self.assertEqual(invoked, [])
                    self.assertEqual(cleanup_called, [True])
                finally:
                    release_worker.set()
                self.assertTrue(owner.closed_event.wait(1))
                self.assertNotIn(store_lock.store_advisory_lock_key(site, name), self.database.owners)

    def test_nested_worker_inherits_lock_and_prevents_early_release(self):
        release_child = threading.Event()
        child_started = threading.Event()
        run_with_timeout = _load_timeout_function("vn")

        def child():
            child_started.set()
            release_child.wait(2)

        def parent():
            try:
                run_with_timeout(child, 0, "timeout")
            except _StoreTaskTimeout:
                pass

        @store_lock.with_ads_store_lock("vn")
        def execute(_driver, _store_name):
            run_with_timeout(parent, 1, "parent timeout")

        try:
            execute(None, "越南072")
            self.assertTrue(child_started.wait(1))
            key = store_lock.store_advisory_lock_key("vn", "越南072")
            owner = self.database.owners[key]
            self.assertFalse(owner.closed)
        finally:
            release_child.set()
        self.assertTrue(owner.closed_event.wait(1))

    def test_both_tracked_service_functions_are_decorated(self):
        for site in ("vn", "id"):
            source = SUPERBROWSER_ROOT / "main" / "shopee" / f"{site}_shopee_ads_recharge_operator_service.py"
            tree = ast.parse(source.read_text(encoding="utf-8-sig"))
            tracked = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                           and node.name == f"tracked_{site}_shopee_ads_recharge")
            decorator = tracked.decorator_list[0]
            self.assertIsInstance(decorator, ast.Call)
            self.assertEqual(decorator.func.id, "with_ads_store_lock")
            self.assertEqual(decorator.args[0].id, "SITE_CODE")
            self.assertEqual(decorator.keywords[0].arg, "on_blocked")


if __name__ == "__main__":
    unittest.main()
