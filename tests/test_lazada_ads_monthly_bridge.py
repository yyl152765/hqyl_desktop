"""Bridge tests for Lazada Ads Monthly Report."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend import app_bridge
from backend.app_bridge import AppBridge
from backend.config_store import AppSettings
from backend.services.lazada_ads_monthly import FORMULA_VERSION


class TestLazadaAdsMonthlyBridge(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = AppSettings(output_dir=str(self.root))
        config = SimpleNamespace(load=lambda: self.settings, local_data_dir=self.root, config_dir=self.root)
        with patch.object(app_bridge, "ConfigStore", return_value=config):
            self.bridge = AppBridge()
        self.bridge.tasks = MagicMock()
        self.bridge.tasks.start.return_value = {"ok": True, "id": "test-task"}

    def test_dashboard_exposes_monthly_report(self):
        with patch.object(app_bridge, "get_group_options", return_value=[]), patch.object(
            app_bridge, "get_income_expense_category_options", return_value=[]
        ), patch.object(app_bridge, "get_income_expense_warehouse_options", return_value=[]):
            result = self.bridge.get_app_info()
        cards = [tool for tool in result["tools"] if tool["key"] == "lazada_ads_monthly"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["vendor"], "ziniao")
        self.assertEqual(cards[0]["status"], "ready")

    def test_launcher_menu_and_dashboard_reach_monthly_page(self):
        from launcher.main import resolve_entry_html

        page = resolve_entry_html(["--page=lazada-ads-monthly"])
        self.assertEqual(page.name, "lazada-ads-monthly.html")
        self.assertTrue(page.is_file())
        assets = Path(__file__).resolve().parents[1] / "frontend" / "assets"
        self.assertIn('id: "lazada_ads_monthly", href: "lazada-ads-monthly.html"',
                      (assets / "common.js").read_text(encoding="utf-8"))
        self.assertIn('lazada_ads_monthly: { href: "lazada-ads-monthly.html"',
                      (assets / "pages" / "dashboard.js").read_text(encoding="utf-8"))

    def test_get_lazada_ads_monthly_info(self):
        with patch.object(app_bridge, 'ZiniaoMonthlyCli') as cls:
            cls.return_value.profile = 'offline-test'
            res = self.bridge.get_lazada_ads_monthly_info()
        self.assertTrue(res["ok"])
        self.assertRegex(res["default_month"], r"^\d{4}-\d{2}$")
        self.assertTrue(len(res["countries"]) >= 7)
        codes = [c["code"] for c in res["countries"]]
        self.assertIn("AUTO", codes)
        self.assertIn("PH", codes)
        self.assertIn("MY", codes)
        self.assertIn("TH", codes)
        self.assertIn("ID", codes)
        self.assertIn("VN", codes)
        self.assertIn("SG", codes)

    def test_preview_lazada_ads_monthly(self):
        payload = {
            "month": "2026-10",
            "country": "TH",
            "store_names": ["Store TH 1"],
            "output_root": str(self.root),
        }
        with patch("backend.services.lazada_ads_monthly.ZiniaoMonthlyCli") as mock_cli_cls:
            cli = mock_cli_cls.return_value
            cli.ready.return_value = None
            cli.profile = "default"
            cli.list_stores.return_value = [
                {"store_id": "111", "store_name": "Store TH 1", "platform_name": "lazada", "site": "TH"}
            ]
            cli.match_store.return_value = {
                "store_id": "111",
                "store_name": "Store TH 1",
                "platform_name": "lazada",
                "site": "TH",
            }

            res = self.bridge.preview_lazada_ads_monthly(payload)
            self.assertTrue(res["ok"])
            self.assertEqual(res["matched_count"], 1)
            self.assertEqual(res["matched"][0]["store_id"], "111")

    def test_start_lazada_ads_monthly_mutex(self):
        payload = {
            "month": "2026-10",
            "country": "TH",
            "store_names": ["Store TH 1"],
            "output_root": str(self.root),
        }
        res1 = self.bridge.start_lazada_ads_monthly(payload)
        self.assertTrue(res1["ok"])

        # Trying to start while active should be rejected by mutex
        res2 = self.bridge.start_lazada_ads_monthly(payload)
        self.assertFalse(res2["ok"])
        self.assertIn("已有 Lazada 广告月报任务正在运行", res2["error"])

    def test_retry_lazada_ads_monthly_invalid_manifest(self):
        res = self.bridge.retry_lazada_ads_monthly({"manifest_path": "nonexistent.json"})
        self.assertFalse(res["ok"])
        self.assertIn("请提供有效的批次 manifest", res["error"])


if __name__ == "__main__":
    unittest.main()
