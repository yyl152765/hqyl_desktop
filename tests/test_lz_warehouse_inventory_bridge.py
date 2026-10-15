from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount
from backend.services.lz_warehouse_inventory import LzInventoryResult, LzWarehouseExport
from backend.output_paths import directory_scope_key, resolve_output_directories


class LzWarehouseInventoryBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = AppBridge.__new__(AppBridge)
        self.bridge.config_store = Mock()
        self.bridge.config_store.load.return_value = AppSettings(
            accounts=[
                BoundAccount("other", "mabang", "其他测试账号", "other-user", "other-secret"),
                BoundAccount("target", "mabang", "指定测试账号", "fixture-lz-user", "fixture-lz-secret"),
            ],
            active_account_ids={"mabang": "other"},
        )
        self.bridge.tasks = Mock()
        self.bridge.tasks.start.return_value = {"ok": True, "id": "lz-task"}
        account_patch = patch("backend.app_bridge.LZ_INVENTORY_ACCOUNT_USERNAME", "fixture-lz-user")
        account_patch.start()
        self.addCleanup(account_patch.stop)

    def test_info_finds_designated_bound_account_without_returning_credentials(self):
        with patch("backend.app_bridge.fetch_lz_inventory_warehouses") as loader:
            result = self.bridge.get_lz_warehouse_inventory_info()
        loader.assert_not_called()
        self.assertEqual(result, {
            "ok": True, "account_id": "target", "account_name": "指定测试账号",
            "warehouses": ["泰国TH04仓", "泰国TH05仓", "泰国TH06仓"],
            **resolve_output_directories(self.bridge.config_store.load(), "lz_warehouse_inventory"),
        })
        self.assertNotIn("secret", str(result))
        self.bridge.config_store.save.assert_not_called()

    def test_missing_designated_account_never_falls_back_to_active_account(self):
        self.bridge.config_store.load.return_value.accounts.pop()
        for result in (
            self.bridge.get_lz_warehouse_inventory_info(),
            self.bridge.get_lz_warehouse_inventory_warehouses(),
            self.bridge.start_lz_warehouse_inventory({"account_id": "other", "output_dir": "unused"}),
        ):
            self.assertFalse(result["ok"])
            self.assertTrue(result["requires_account"])
            self.assertEqual(result["vendor"], "mabang")
        self.bridge.tasks.start.assert_not_called()
        self.bridge.config_store.save.assert_not_called()

    def test_start_overrides_untrusted_credentials_and_preserves_export_counts(self):
        names = ("泰国TH04仓", "菲律宾PH01仓")
        job = SimpleNamespace(username="fixture-lz-user", password="fixture-lz-secret", output_dir=Path("exports"), warehouse_names=names)
        with patch("backend.app_bridge.validate_lz_inventory_payload", return_value=job) as validator:
            result = self.bridge.start_lz_warehouse_inventory({
                "account_id": "other", "username": "untrusted", "password": "untrusted", "output_dir": "exports", "warehouse_names": list(names),
            })
        self.assertEqual(result, {"ok": True, "id": "lz-task"})
        self.assertEqual(validator.call_args.args[0], {
            "account_id": "target", "username": "fixture-lz-user", "password": "fixture-lz-secret", "output_dir": "exports", "warehouse_names": list(names),
        })
        self.bridge.config_store.save.assert_called_once_with({"tool_output_dirs": {directory_scope_key("lz_warehouse_inventory"): "exports"}})
        self.assertEqual(self.bridge.tasks.start.call_args.kwargs, {
            "tool": "lz_warehouse_inventory", "context": {"account_id": "target", "output_root": "exports", "warehouse_names": list(names)},
        })
        service_result = LzInventoryResult(exports=tuple(
            LzWarehouseExport(
                warehouse_name=name, output_file=Path(f"exports/{name}.xlsx"), record_count=10001-index,
                source_record_count=10001-index, export_page_count=2-index,
                preview_columns=("库存SKU", "仓库", "可用库存量"),
                preview_rows=tuple({"库存SKU": str(row), "仓库": name, "可用库存量": 0} for row in range(101)),
            ) for index, name in enumerate(names)
        ))
        progress = Mock()
        with patch("backend.app_bridge.run_lz_inventory_export", return_value=service_result) as exporter:
            output = self.bridge.tasks.start.call_args.args[1](progress)
        exporter.assert_called_once_with(job, progress)
        self.assertEqual(output["record_count"], 20001)
        self.assertEqual(output["source_record_count"], 20001)
        self.assertEqual(output["export_page_count"], 3)
        self.assertEqual(output["warehouse_count"], 2)
        self.assertEqual(output["selected_warehouse_count"], 2)
        self.assertEqual(output["warehouse_counts"], service_result.warehouse_counts)
        self.assertEqual(output["preview_columns"], list(service_result.preview_columns))
        self.assertEqual(len(output["rows"]), 100)
        self.assertTrue(output["preview_truncated"])
        self.assertEqual(output["output_file"], str(service_result.output_file))
        self.assertEqual(output["output_files"], [str(path) for path in service_result.output_files])
        self.assertEqual([item["warehouse_name"] for item in output["exports"]], list(names))
        self.assertEqual([len(item["rows"]) for item in output["exports"]], [100, 100])
        self.assertTrue(output["is_complete"])
        self.assertEqual(output["failures"], [])
        self.assertNotIn("secret", str(output))

    def test_warehouse_loader_uses_only_the_designated_account_and_surfaces_network_errors(self):
        warehouses = ({"id": "W1", "name": "任意仓库"}, {"id": "W2", "name": "菲律宾PH01仓"})
        with patch("backend.app_bridge.fetch_lz_inventory_warehouses", return_value=warehouses) as loader:
            result = self.bridge.get_lz_warehouse_inventory_warehouses()
        loader.assert_called_once_with("fixture-lz-user", "fixture-lz-secret")
        self.assertEqual(result, {"ok": True, "warehouses": list(warehouses)})
        with patch("backend.app_bridge.fetch_lz_inventory_warehouses", side_effect=RuntimeError("目录接口暂时不可用")):
            result = self.bridge.get_lz_warehouse_inventory_warehouses()
        self.assertEqual(result, {"ok": False, "error": "目录接口暂时不可用"})

    def test_all_failed_result_keeps_failure_details_without_a_fake_file_path(self):
        names = ("泰国TH04仓", "菲律宾PH01仓")
        job = SimpleNamespace(output_dir=Path("exports"), warehouse_names=names)
        with patch("backend.app_bridge.validate_lz_inventory_payload", return_value=job):
            self.bridge.start_lz_warehouse_inventory({"warehouse_names": list(names), "output_dir": "exports"})
        failures = tuple({"warehouse_name": name, "error": "导出失败"} for name in names)
        with patch("backend.app_bridge.run_lz_inventory_export", return_value=LzInventoryResult(exports=(), failures=failures)):
            output = self.bridge.tasks.start.call_args.args[1](Mock())
        self.assertEqual(output["output_file"], "")
        self.assertEqual(output["output_files"], [])
        self.assertEqual(output["output_dir"], "exports")
        self.assertEqual(output["selected_warehouse_count"], 2)
        self.assertEqual(output["failures"], list(failures))
        self.assertFalse(output["is_complete"])

    def test_zero_warehouse_selection_is_rejected_before_task_creation(self):
        result = self.bridge.start_lz_warehouse_inventory({"warehouse_names": [], "output_dir": "unused"})
        self.assertFalse(result["ok"])
        self.bridge.tasks.start.assert_not_called()

    def test_invalid_payload_is_reported_before_a_task_is_created(self):
        with patch("backend.app_bridge.validate_lz_inventory_payload", side_effect=ValueError("输出目录无效")):
            result = self.bridge.start_lz_warehouse_inventory({"output_dir": ""})
        self.assertEqual(result, {"ok": False, "error": "输出目录无效"})
        self.bridge.tasks.start.assert_not_called()
        self.bridge.config_store.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
