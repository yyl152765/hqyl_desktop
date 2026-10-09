from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from backend.core.ziniao_browser import account_profile
from backend.services import balance_statistics as service


def store(identity, name=None, platform="temu"):
    return {"store_id": str(identity), "store_name": name or f"店铺{identity}", "platform_name": platform.upper()}


class FakeBrowser:
    def __init__(self, request, stores, *, open_error=None, close_error=None, ready_error=False):
        self.profile = account_profile(request)
        self.stores = stores
        self.open_error, self.close_error, self.ready_error = open_error, close_error, ready_error
        self.calls = []
        self.cleanup_failed = False

    def ready(self):
        self.calls.append("ready")
        if self.ready_error:
            raise ValueError("客户端未就绪")

    def list_stores(self):
        self.calls.append("list")
        return copy.deepcopy(self.stores)

    def open_store(self, row):
        self.calls.append(("open", row["store_id"]))
        if row["store_id"] == self.open_error:
            raise ValueError("打开失败")

    def _driver(self, identity):
        return object()

    def close_store(self, identity):
        self.calls.append(("close", identity))
        if identity == self.close_error:
            self.cleanup_failed = True
            raise ValueError("窗口未关闭")

    def close(self):
        self.calls.append("dispose")


class BalanceStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request = {"account_id": "bound-account-1", "company": "fixture-company", "username": "fixture-user", "password": "sensitive-password",
                        "platform": "temu", "month": "2026-08", "store_names": ["店铺1", "店铺2"], "output_root": str(self.root)}
        self.calls = []

    def collector(self, driver, selected, *args, progress=None):
        self.calls.append(selected["store_id"])
        directory = args[-1]
        image = directory / "proof.png"
        Image.new("RGB", (160, 90), "white").save(image)
        is_temu = len(args) == 2
        fields = ("total", "pending") if is_temu else ("income", "balance", "ads")
        values = {field: "0.00" if field in {"total", "balance"} else "-2.34" for field in fields}
        if not is_temu:
            values["processing"] = None
        return {"country": "TH", "currency": "THB", "values": values,
                "evidence": {field: str(image) for field in fields},
                "evidence_exemptions": {} if is_temu else {"processing": "withdrawal_success"},
                "captured_at": "2026-09-28T12:00:00+08:00", "notes": [], "field_errors": {},
                "source_urls": {"total": "https://seller.example.invalid/page?token=secret#fragment"}}

    def run_batch(self, *, request=None, browser=None, collector=None):
        request = request or self.request
        browser = browser or FakeBrowser(request, [store(1), store(2)])
        return service.run_balance_statistics(request, browser_factory=lambda request: browser, collectors={request["platform"]: collector or self.collector}), browser

    def test_previous_month_year_boundary_and_leap_year(self):
        self.assertEqual(service.get_defaults("temu", date(2026, 1, 1))["month"], "2025-12")
        self.assertEqual(service.get_defaults("temu", date(2024, 3, 31))["month"], "2024-02")
        self.assertEqual(service.validate_request({**self.request, "month": ""}, today=date(2026, 9, 1))["month"], "2026-08")
        for month in ("2026-13", "2026-1", "2026-10", "0000-01"):
            with self.subTest(month=month), self.assertRaises(ValueError):
                service.validate_request({**self.request, "month": month}, today=date(2026, 9, 1))

    def test_match_exact_normalized_full_names_and_reject_ambiguity_wrong_platform(self):
        matched = service.match_stores(["ＡＢＣ  店铺"], [store(1, "ABC 店铺")], "temu")
        self.assertTrue(matched["can_start"])
        self.assertFalse(service.match_stores(["ABC"], [store(1, "ABC 店铺")], "temu")["can_start"])
        self.assertFalse(service.match_stores(["ABC"], [store(1, "ABC"), store(2, "ABC")], "temu")["can_start"])
        self.assertFalse(service.match_stores(["ABC"], [store(1, "ABC", "lazada")], "temu")["can_start"])
        with self.assertRaises(ValueError):
            service.match_stores(["店铺1"], [store(1), store(1)], "temu")

    def test_serial_run_money_evidence_manifest_and_no_credentials(self):
        result, browser = self.run_batch()
        self.assertTrue(result["is_complete"])
        self.assertEqual(result["summary"], {"total": 2, "success": 2, "partial": 0, "failed": 0})
        self.assertEqual(browser.calls, ["ready", "list", ("open", "1"), ("close", "1"), ("open", "2"), ("close", "2"), "dispose"])
        self.assertEqual(result["stores"][0]["values"], {"total": "0.00", "pending": "-2.34"})
        self.assertTrue(Path(result["output_file"]).is_file())
        manifest_text = Path(result["manifest_path"]).read_text(encoding="utf-8")
        for secret in ("sensitive-password", "fixture-user", "fixture-company", "token=secret"):
            self.assertNotIn(secret, manifest_text)
        record = json.loads(manifest_text)
        self.assertEqual(record["month"], "2026-08")
        self.assertEqual(record["balance_basis"], "current_page")
        self.assertEqual(record["pending_basis"], "order_created_month")

    def test_partial_missing_value_has_blank_not_zero_and_retry_only_partial(self):
        def partial(*args, **kwargs):
            raw = self.collector(*args, **kwargs)
            if args[1]["store_id"] == "2":
                raw["values"]["pending"] = None
                raw["field_errors"] = {"pending": "页面筛选未验证"}
            return raw
        result, _ = self.run_batch(collector=partial)
        self.assertFalse(result["is_complete"])
        self.assertEqual(result["summary"]["partial"], 1)
        self.assertIsNone(result["stores"][1]["values"]["pending"])
        previous_success = copy.deepcopy(result["stores"][0])
        self.calls.clear()
        retry_browser = FakeBrowser(self.request, [store(1), store(2)])
        retried = service.retry_balance_statistics({**self.request, "month": "2026-07", "manifest_path": result["manifest_path"]}, browser_factory=lambda request: retry_browser, collectors={"temu": self.collector})
        self.assertTrue(retried["is_complete"])
        self.assertEqual(self.calls, ["2"])
        self.assertEqual(retried["month"], "2026-08")
        self.assertEqual(retried["stores"][0], previous_success)

    def test_open_failure_always_closes_and_continues(self):
        browser = FakeBrowser(self.request, [store(1), store(2)], open_error="1")
        result, _ = self.run_batch(browser=browser)
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertIn(("close", "1"), browser.calls)
        self.assertIn(("open", "2"), browser.calls)
        self.assertEqual(result["stores"][0]["values"], {"total": None, "pending": None})

    def test_close_failure_halts_following_stores_and_retains_captured_amounts(self):
        browser = FakeBrowser(self.request, [store(1), store(2)], close_error="1")
        result, _ = self.run_batch(browser=browser)
        self.assertFalse(result["is_complete"])
        self.assertNotIn(("open", "2"), browser.calls)
        self.assertEqual(result["stores"][0]["status"], "partial")
        self.assertEqual(result["stores"][0]["values"]["total"], "0.00")
        self.assertEqual(result["stores"][1]["status"], "failed")
        self.assertEqual(browser.calls[-1], "dispose")

    def test_retry_rejects_other_account_and_changed_identity(self):
        result, _ = self.run_batch(browser=FakeBrowser(self.request, [store(1), store(2)], open_error="2"))
        factory = Mock()
        with self.assertRaisesRegex(ValueError, "账号.*不一致"):
            service.retry_balance_statistics({**self.request, "account_id": "other-account", "manifest_path": result["manifest_path"]}, browser_factory=factory)
        factory.assert_not_called()
        browser = FakeBrowser(self.request, [store(1), store(3, "店铺2")])
        retried = service.retry_balance_statistics({**self.request, "manifest_path": result["manifest_path"]}, browser_factory=lambda request: browser, collectors={"temu": self.collector})
        self.assertFalse(retried["is_complete"])
        self.assertNotIn(("open", "3"), browser.calls)
        self.assertIn("ID", retried["stores"][1]["message"])

    def test_retry_rejects_paths_outside_batch(self):
        result, _ = self.run_batch()
        path = Path(result["manifest_path"])
        record = json.loads(path.read_text(encoding="utf-8"))
        record["stores"][0]["evidence"]["total"] = str(self.root / "outside.png")
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "截图路径"):
            service.retry_balance_statistics({**self.request, "manifest_path": str(path)})

    def test_lazada_explicit_exemptions_blank_and_negative_processing(self):
        request = {**self.request, "platform": "lazada"}
        def lazada(*args, **kwargs):
            raw = self.collector(*args, **kwargs)
            if args[1]["store_id"] == "1":
                raw["evidence"].pop("balance")
                raw["evidence_exemptions"]["balance"] = "cross_border_unavailable"
            else:
                raw["values"]["processing"] = "12.34"
                raw["evidence"]["processing"] = raw["evidence"]["ads"]
                raw["evidence_exemptions"] = {}
            return raw
        result, _ = self.run_batch(request=request, browser=FakeBrowser(request, [store(1, platform="lazada"), store(2, platform="lazada")]), collector=lazada)
        self.assertTrue(result["is_complete"])
        self.assertIsNone(result["stores"][0]["values"]["processing"])
        self.assertNotIn("processing", result["stores"][0]["evidence"])
        self.assertEqual(result["stores"][0]["values"]["balance"], "0.00")
        self.assertEqual(result["stores"][1]["values"]["processing"], "-12.34")

    def test_unknown_blank_processing_missing_screenshots_and_wrong_country_fail(self):
        request = {**self.request, "platform": "lazada", "country": "PH"}
        def invalid(*args, **kwargs):
            raw = self.collector(*args, **kwargs)
            raw["evidence_exemptions"] = {}
            raw["evidence"].pop("ads")
            raw["currency"] = ""
            return raw
        result, _ = self.run_batch(request=request, browser=FakeBrowser(request, [store(1, platform="lazada"), store(2, platform="lazada")]), collector=invalid)
        self.assertFalse(result["is_complete"])
        errors = result["stores"][0]["field_errors"]
        self.assertTrue({"processing", "ads", "country", "currency"}.issubset(errors))

    def test_ready_failure_still_exports_failed_rows_and_redacts_error(self):
        def failing(*args, **kwargs):
            raise ValueError("error sensitive-password fixture-user")
        result, _ = self.run_batch(collector=failing)
        self.assertEqual(result["summary"]["failed"], 2)
        self.assertTrue(Path(result["output_file"]).exists())
        self.assertNotIn("sensitive-password", Path(result["manifest_path"]).read_text(encoding="utf-8"))
        result, browser = self.run_batch(browser=FakeBrowser(self.request, [store(1)], ready_error=True))
        self.assertEqual(result["summary"]["failed"], 2)
        self.assertEqual(browser.calls[-1], "dispose")

    def test_same_store_entered_twice_does_not_open_twice(self):
        request = {**self.request, "store_names": ["ABC", "ＡＢＣ"]}
        browser = FakeBrowser(request, [store(1, "ABC")])
        result, _ = self.run_batch(request=request, browser=browser)
        self.assertEqual(browser.calls.count(("open", "1")), 1)
        self.assertEqual(result["summary"], {"total": 2, "success": 1, "partial": 0, "failed": 1})

    def test_export_retry_reuses_success_without_starting_browser(self):
        with patch.object(service, "write_balance_workbook", side_effect=OSError("工作簿被占用")):
            result, _ = self.run_batch()
        self.assertFalse(result["is_complete"])
        factory = Mock()
        retried = service.retry_balance_statistics({**self.request, "manifest_path": result["manifest_path"]}, browser_factory=factory)
        factory.assert_not_called()
        self.assertTrue(retried["is_complete"])

    def test_retry_recollects_only_success_store_with_missing_or_corrupt_evidence(self):
        for damage in ("missing", "corrupt"):
            with self.subTest(damage=damage):
                result, _ = self.run_batch()
                previous_success = copy.deepcopy(result["stores"][1])
                image = Path(result["stores"][0]["evidence"]["total"])
                if damage == "missing":
                    image.unlink()
                else:
                    image.write_bytes(b"broken image fixture")
                self.calls.clear()
                browser = FakeBrowser(self.request, [store(1), store(2)])
                retried = service.retry_balance_statistics(
                    {**self.request, "manifest_path": result["manifest_path"]},
                    browser_factory=lambda request: browser, collectors={"temu": self.collector},
                )
                self.assertTrue(retried["is_complete"])
                self.assertEqual(self.calls, ["1"])
                self.assertEqual(retried["stores"][1], previous_success)
                self.assertEqual(retried["stores"][0]["attempts"], 2)
                self.assertEqual(retried["summary"], {"total": 2, "success": 2, "partial": 0, "failed": 0})

    def test_missing_evidence_is_not_counted_success_when_recollection_cannot_start(self):
        result, _ = self.run_batch()
        Path(result["stores"][0]["evidence"]["total"]).unlink()
        browser = FakeBrowser(self.request, [store(1), store(2)], ready_error=True)
        retried = service.retry_balance_statistics(
            {**self.request, "manifest_path": result["manifest_path"]},
            browser_factory=lambda request: browser, collectors={"temu": self.collector},
        )
        self.assertFalse(retried["is_complete"])
        self.assertEqual(retried["summary"], {"total": 2, "success": 1, "partial": 0, "failed": 1})
        self.assertEqual(retried["stores"][0]["evidence"], {})
        self.assertTrue(Path(retried["output_file"]).is_file())

    def test_lazada_retry_keeps_confirmed_evidence_exemptions_without_browser(self):
        request = {**self.request, "platform": "lazada"}
        result, _ = self.run_batch(request=request, browser=FakeBrowser(request, [store(1, platform="lazada"), store(2, platform="lazada")]))
        factory = Mock()
        retried = service.retry_balance_statistics({**request, "manifest_path": result["manifest_path"]}, browser_factory=factory)
        factory.assert_not_called()
        self.assertTrue(retried["is_complete"])


if __name__ == "__main__":
    unittest.main()
