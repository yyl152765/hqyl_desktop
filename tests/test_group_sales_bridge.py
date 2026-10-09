from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount
from backend.services.group_sales_report import GroupSalesReportResult


class GroupSalesBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = AppBridge.__new__(AppBridge)
        self.settings = AppSettings(
            accounts=[BoundAccount("a", "mabang", "测试账号", "bound-user", "bound-password")],
            active_account_ids={"mabang": "a"},
        )
        self.bridge.config_store = Mock()
        self.bridge.config_store.load.return_value = self.settings
        self.bridge.tasks = Mock()
        self.bridge.tasks.start.return_value = {"ok": True, "id": "test-task"}
        self.options = [{"id": "new-category", "name": "新增自定义分类", "shop_label_id": "new-category"}]

    def test_options_use_bound_account_credentials(self) -> None:
        with patch("backend.app_bridge.get_group_options", return_value=self.options) as loader:
            result = self.bridge.get_group_sales_options({
                "account_id": "a", "username": "untrusted", "password": "untrusted",
            })
        self.assertEqual(result, {"ok": True, "groups": self.options})
        loader.assert_called_once_with("bound-user", "bound-password")

    def test_options_failure_and_missing_account_are_reported_without_fallback(self) -> None:
        with patch("backend.app_bridge.get_group_options", side_effect=RuntimeError("分类读取失败")):
            self.assertEqual(self.bridge.get_group_sales_options(), {"ok": False, "error": "分类读取失败"})
        with patch("backend.app_bridge.get_group_options") as loader:
            result = self.bridge.get_group_sales_options({"account_id": "missing"})
            self.assertFalse(result["ok"])
            self.assertTrue(result["requires_account"])
            loader.assert_not_called()

    def test_new_category_is_saved_and_sent_to_export_with_official_name(self) -> None:
        with patch("backend.services.group_sales_report.get_group_options", return_value=self.options):
            result = self.bridge.start_group_sales_report({
                "account_id": "a", "group_ids": ["new-category"],
                "start_date": "2026-09-01", "end_date": "2026-09-20", "output_dir": "exports",
            })
        self.assertTrue(result["ok"])
        self.bridge.config_store.save.assert_called_once_with({
            "output_dir": "exports", "sales_group_ids": ["new-category"],
        })
        runner = self.bridge.tasks.start.call_args.args[1]
        export_result = GroupSalesReportResult(3, 1, 1, Path("exports/summary.xlsx"), Path("exports"))
        with patch("backend.app_bridge.run_group_sales_report", return_value=export_result) as exporter:
            output = runner(Mock())
        job = exporter.call_args.args[0]
        self.assertEqual(job.groups[0].name, "新增自定义分类")
        self.assertEqual(job.groups[0].id, "new-category")
        self.assertEqual(output["group_count"], 1)
        self.assertEqual(output["record_count"], 3)


if __name__ == "__main__":
    unittest.main()
