from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount
from backend.services.group_sales_report import GroupSalesReportResult


class GroupSalesBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output_root = Path(temporary.name) / "new-reports"
        self.bridge = AppBridge.__new__(AppBridge)
        self.settings = AppSettings(
            output_dir=str(self.output_root),
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
                "start_date": "2026-09-01", "end_date": "2026-09-20", "output_dir": str(self.output_root),
            })
        self.assertTrue(result["ok"])
        self.bridge.config_store.save.assert_called_once_with({
            "output_dir": str(self.output_root), "sales_group_ids": ["new-category"],
        })
        self.assertTrue(self.output_root.is_dir())
        runner = self.bridge.tasks.start.call_args.args[1]
        run_dir = self.output_root / "菲律宾各组商品销量报表_20260901_20260920_120000"
        export_result = GroupSalesReportResult(3, 1, 1, run_dir / "summary.xlsx", run_dir)
        with patch("backend.app_bridge.run_group_sales_report", return_value=export_result) as exporter:
            output = runner(Mock())
        job = exporter.call_args.args[0]
        self.assertEqual(job.groups[0].name, "新增自定义分类")
        self.assertEqual(job.groups[0].id, "new-category")
        self.assertEqual(output["group_count"], 1)
        self.assertEqual(output["record_count"], 3)
        self.assertEqual(output["output_dir"], str(run_dir))
        self.assertEqual(output["output_root"], str(self.output_root))
        self.assertEqual(self.bridge.tasks.start.call_args.kwargs["context"]["output_root"], str(self.output_root))

    def test_missing_directory_uses_saved_root_and_invalid_directory_is_not_saved(self) -> None:
        payload = {
            "account_id": "a", "group_ids": ["new-category"],
            "start_date": "2026-09-01", "end_date": "2026-09-20",
        }
        with patch("backend.services.group_sales_report.get_group_options", return_value=self.options):
            result = self.bridge.start_group_sales_report(payload)
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.output_root.is_dir())
        self.bridge.config_store.save.reset_mock()
        self.bridge.tasks.start.reset_mock()
        blocked = self.output_root / "a-file"
        blocked.write_text("keep", encoding="utf-8")
        with patch("backend.services.group_sales_report.get_group_options", return_value=self.options):
            result = self.bridge.start_group_sales_report({**payload, "output_dir": str(blocked)})
        self.assertFalse(result["ok"])
        self.assertIn("输出目录", result["error"])
        self.bridge.config_store.save.assert_not_called()
        self.bridge.tasks.start.assert_not_called()
        self.assertEqual(blocked.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
