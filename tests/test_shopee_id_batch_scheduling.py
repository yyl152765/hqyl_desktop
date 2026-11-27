"""Behavioral regressions for month selection, isolated workers and retry scopes."""
from contextlib import contextmanager
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from backend.services import shopee_id_monthly_invoice as service
from backend.services.shopee_id_income_report import month_date_bounds, NO_DOWNLOAD_DATA_NOTICE
from backend.services.shopee_id_invoice_gateway import ShopeeIdInvoiceGateway
from tests.shopee_id_fixtures import income_file
from tests.test_shopee_id_monthly_invoice import FakeInvoiceGateway


def stores(count):
    return [{"store_id": str(i), "store_name": f"合成-SP印尼{i:03d}", "platform_name": "Shopee-印尼"}
            for i in range(1, count + 1)]


def empty_month(month):
    return {"row_count": 0, "files": [], "evidence": {
        "selected_month": month, "empty_text": "No receipt found",
        "income_no_data": {"month": month, "date_range": month_date_bounds(month)["date_range_param"],
            "history_checked": True, "reason": "export_no_data", "export_notice": NO_DOWNLOAD_DATA_NOTICE,
            "transaction_row_count": 0, "transaction_empty_text": "No Data"}}}


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_ranges_cross_year_deduplicate_sort_and_reject_invalid_inputs(self):
        today = date(2026, 11, 27)
        self.assertEqual(service.validate_months({"month": None}, today=today), ["2026-10"])
        self.assertEqual(service.validate_months({"start_month": "2025-12", "end_month": "2026-02"}, today=today),
                         ["2025-12", "2026-01", "2026-02"])
        self.assertEqual(service.validate_months({"months": ["2026-06", "2026-01", "2026-06"]}, today=today),
                         ["2026-01", "2026-06"])
        for payload in ({"months": []}, {"months": "2026-01"}, {"months": [None]},
                        {"months": ["2026-01", "2026-11"]}, {"start_month": "2026-05", "end_month": "2026-01"},
                        {"start_month": "2026-01"}, {"months": ["0000-01"]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                service.validate_months(payload, today=today)
        for value in (0, 6, -1, True, "5", 2.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                service.validate_workers(value)

    def test_five_independent_workers_reuse_store_and_execute_six_months_in_order(self):
        months = [f"2026-{i:02d}" for i in range(1, 7)]
        manifest = service.create_batch(self.root, "fixture", months[0], stores(8), months=months)
        lock = threading.Lock()
        barrier = threading.Barrier(5, timeout=10)
        active = set()
        opened, closed, calls, workers = [], [], [], []
        peak = 0

        class Worker(FakeInvoiceGateway):
            @contextmanager
            def store_session(worker, store):
                nonlocal peak
                sid = store["store_id"]
                with lock:
                    self.assertNotIn(sid, active)
                    active.add(sid)
                    opened.append(sid)
                    peak = max(peak, len(active))
                    first_wave = len(opened) <= 5
                if first_wave:
                    barrier.wait()
                try:
                    yield
                finally:
                    with lock:
                        active.remove(sid)
                        closed.append(sid)

            def export(worker, store, month, folder):
                with lock:
                    calls.append((store["store_id"], month, str(folder), id(worker)))
                return empty_month(month)

        @contextmanager
        def factory():
            worker = Worker("fixture", {})
            workers.append(worker)
            yield worker

        result = service.run_batch(manifest, FakeInvoiceGateway("fixture", {}), gateway_factory=factory)
        self.assertTrue(result["is_complete"], result["completion_message"])
        self.assertEqual(peak, 5)
        self.assertEqual(len(workers), 5)
        self.assertEqual(len(opened), 8)
        self.assertCountEqual(closed, opened)
        self.assertEqual((result["store_count"], result["task_count"], result["completed_count"]), (8, 48, 48))
        for sid in opened:
            items = [item for item in calls if item[0] == sid]
            self.assertEqual([item[1] for item in items], months)
            self.assertEqual(len({item[3] for item in items}), 1)
            self.assertTrue(all(item[1] in Path(item[2]).parts for item in items))
        self.assertEqual(len(service.load_batch(manifest)["stores"]), 48)

    def test_only_failed_month_is_retried_and_other_store_continues(self):
        months = ["2026-01", "2026-02", "2026-03"]
        manifest = service.create_batch(self.root, "fixture", months[0], stores(2), months=months)
        calls = []
        fail = True

        def export(store, month, folder):
            calls.append((store["store_id"], month))
            if fail and store["store_id"] == "1" and month == "2026-02":
                raise ValueError("synthetic month failure")
            return empty_month(month)

        gateway = FakeInvoiceGateway("fixture", {"1": export, "2": export})
        first = service.run_batch(manifest, gateway)
        self.assertEqual(first["failed_count"], 1)
        self.assertEqual(first["completed_count"], 5)
        self.assertEqual(len(calls), 6)
        fail = False
        calls.clear()
        second = service.run_batch(manifest, gateway)
        self.assertTrue(second["is_complete"])
        self.assertEqual(calls, [("1", "2026-02")])
        self.assertEqual([s["attempts"] for s in second["stores"]], [1, 2, 1, 1, 1, 1])

    def test_failed_retry_skips_unavailable_without_even_preflight(self):
        manifest = service.create_batch(self.root, "fixture", "2026-01", stores(1))
        record = service.load_batch(manifest)
        record["stores"][0].update(status="unavailable", attempts=1)
        service.write_json(manifest, record)
        gateway = FakeInvoiceGateway("fixture", {})
        gateway.preflight = Mock(side_effect=AssertionError("No browser access"))
        result = service.run_batch(manifest, gateway)
        gateway.preflight.assert_not_called()
        self.assertEqual(result["stores"][0]["attempts"], 1)
        self.assertFalse(result["can_retry"])
        self.assertTrue(result["can_supplement"])

    def test_supplement_does_not_run_or_change_existing_failed_and_empty_months(self):
        manifest = service.create_batch(self.root, "fixture", "2026-01", stores(3))
        record = service.load_batch(manifest)
        for store, status in zip(record["stores"], ("unavailable", "failed", "empty_month")):
            store.update(status=status, attempts=1)
        service.write_json(manifest, record)
        gateway = FakeInvoiceGateway("fixture", {"1": lambda s, m, d: empty_month(m)})
        result = service.run_batch(manifest, gateway, retry_mode="supplement")
        self.assertEqual(gateway.exported_stores, ["1"])
        self.assertEqual([s["status"] for s in result["stores"]], ["empty_month", "failed", "empty_month"])
        self.assertEqual([s["attempts"] for s in result["stores"]], [2, 1, 1])

    def test_v4_single_month_restores_and_backs_up_without_reexporting(self):
        manifest = service.create_batch(self.root, "fixture", "2026-01", stores(1))
        gateway = FakeInvoiceGateway("fixture", {"1": lambda s, m, d: empty_month(m)})
        service.run_batch(manifest, gateway)
        old = service.load_batch(manifest)
        old["schema"] = service.V4_SCHEMA
        old.pop("months")
        old.pop("max_workers")
        old["stores"][0].pop("month")
        service.write_json(manifest, old)
        gateway.exported_stores.clear()
        result = service.run_batch(manifest, gateway)
        self.assertTrue(result["is_complete"])
        self.assertEqual(gateway.exported_stores, [])
        self.assertEqual(result["months"], ["2026-01"])
        self.assertEqual(json.loads(manifest.with_name("manifest.v4.json").read_text("utf-8")), old)

    def test_multi_month_files_are_validated_against_their_own_period(self):
        manifest = service.create_batch(self.root, "fixture", "2026-01", stores(1), months=["2026-01", "2026-02"])
        def export(store, month, folder):
            return {"files": [income_file(folder, month)], "row_count": 0,
                    "evidence": {"empty_text": "No receipt found"}}
        gateway = FakeInvoiceGateway("fixture", {"1": export})
        first = service.run_batch(manifest, gateway)
        self.assertTrue(first["is_complete"], first["completion_message"])
        self.assertEqual(len(set(first["output_files"])), 2)
        Path(first["stores"][1]["files"][0]["path"]).write_bytes(b"corrupt")
        calls = []
        def retried(store, month, folder):
            calls.append(month)
            return export(store, month, folder)
        second = service.run_batch(manifest, FakeInvoiceGateway("fixture", {"1": retried}))
        self.assertTrue(second["is_complete"])
        self.assertEqual(calls, ["2026-02"])

    def test_closure_failure_stops_queue_and_retains_completed_month_files(self):
        manifest = service.create_batch(self.root, "fixture", "2026-01", stores(3), max_workers=1)
        class Worker(FakeInvoiceGateway):
            @contextmanager
            def store_session(worker, store):
                yield
                worker.halted = True
                worker.halt_reason = "synthetic closure unconfirmed"
                raise ValueError(worker.halt_reason)
        gateway = Worker("fixture", {"1": lambda s, m, d: {
            "row_count": 0, "files": [income_file(d, m)], "evidence": {"empty_text": "No receipt found"}}})
        result = service.run_batch(manifest, gateway)
        self.assertEqual(gateway.exported_stores, ["1"])
        self.assertFalse(result["is_complete"])
        self.assertEqual(result["failed_count"], 3)
        self.assertTrue(Path(result["output_files"][0]).is_file())

    def test_manifest_rejects_duplicate_month_and_conflicting_identity(self):
        manifest = service.create_batch(self.root, "fixture", "2026-01", stores(1), months=["2026-01", "2026-02"])
        original = service.load_batch(manifest)
        for field, value in (("month", "2026-01"), ("month", "2026-03"), ("store_name", "other shop")):
            record = json.loads(json.dumps(original))
            record["stores"][1][field] = value
            service.write_json(manifest, record)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                service.load_batch(manifest)

    def test_auth_failure_stops_new_stores_and_joins_all_five_active_workers(self):
        manifest = service.create_batch(self.root, "fixture", "2026-01", stores(10), months=["2026-01", "2026-02"])
        barrier = threading.Barrier(5, timeout=10)
        lock = threading.Lock()
        opened, closed = [], []

        class Worker(FakeInvoiceGateway):
            @contextmanager
            def store_session(worker, store):
                with lock:
                    opened.append(store["store_id"])
                barrier.wait()
                try:
                    yield
                finally:
                    with lock:
                        closed.append(store["store_id"])

            def export(worker, store, month, folder):
                if store["store_id"] == "1":
                    worker.auth_failed = True
                    raise ValueError("synthetic authentication failure")
                self.assertTrue(worker.stop_event.wait(5))
                return empty_month(month)

        @contextmanager
        def factory():
            yield Worker("fixture", {})

        result = service.run_batch(manifest, FakeInvoiceGateway("fixture", {}), gateway_factory=factory)
        self.assertFalse(result["is_complete"])
        self.assertEqual(len(opened), 5)
        self.assertCountEqual(opened, closed)
        self.assertEqual(result["running_count"], 0)
        self.assertEqual(result["empty_month_count"], 4)
        self.assertEqual(result["failed_count"], 16)

    def test_real_gateway_session_opens_and_closes_once_for_several_exports(self):
        store = stores(1)[0]
        browser = Mock(profile="fixture", auth_failed=False, cleanup_failed=False)
        browser.list_stores.return_value = [store]
        gateway = ShopeeIdInvoiceGateway("fixture", browser=browser)
        gateway.preflight([store])
        with patch.object(gateway, "_export", return_value={}) as export:
            with gateway.store_session(store):
                gateway.export(store, "2026-01", self.root)
                gateway.export(store, "2026-02", self.root)
            self.assertEqual(export.call_count, 2)
        browser.open_store.assert_called_once()
        browser.close_store.assert_called_once_with(store["store_id"])

    def test_disconnected_progress_callback_does_not_abort_or_lose_manifest(self):
        manifest = service.create_batch(self.root, "fixture", "2026-01", stores(2))
        gateway = FakeInvoiceGateway("fixture", {s["store_id"]: lambda s, m, d: empty_month(m) for s in stores(2)})
        progress = Mock(side_effect=RuntimeError("synthetic UI disconnected"))
        result = service.run_batch(manifest, gateway, progress)
        self.assertTrue(result["is_complete"])
        self.assertEqual(service.load_batch(manifest)["status"], "complete")
        self.assertTrue(progress.called)

    def test_prepared_worker_never_starts_client_when_preflight_fails(self):
        browser = Mock(profile="fixture", auth_failed=False, cleanup_failed=False)
        browser.list_stores.side_effect = RuntimeError("synthetic client unavailable")
        gateway = ShopeeIdInvoiceGateway("fixture", browser=browser, client_prepared=True)
        with self.assertRaisesRegex(RuntimeError, "client unavailable"):
            gateway.preflight(stores(1))
        browser.ready.assert_not_called()
        browser.prepare_task.assert_not_called()
        browser.open_store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
