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
                BoundAccount("foreign", "ziniao", "其他平台测试账号", "foreign-user", "foreign-secret"),
            ],
            active_account_ids={"mabang": "other"},
        )
        self.bridge.tasks = Mock()
        self.bridge.tasks.start.return_value = {"ok": True, "id": "lz-task"}

    def test_current_active_account_takes_precedence_over_previous_fixed_account(self):
        result = self.bridge.get_lz_warehouse_inventory_info()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["account_id"], "other")
        self.assertEqual(result["account_name"], "其他测试账号")

    def test_info_follows_active_bound_account_without_returning_credentials(self):
        with patch("backend.app_bridge.fetch_lz_inventory_warehouses") as loader:
            result = self.bridge.get_lz_warehouse_inventory_info()
        loader.assert_not_called()
        self.assertEqual(result, {
            "ok": True, "account_id": "other", "account_name": "其他测试账号",
            "warehouses": ["泰国TH04仓", "泰国TH05仓", "泰国TH06仓"],
            **resolve_output_directories(self.bridge.config_store.load(), "lz_warehouse_inventory"),
        })
        self.assertNotIn("secret", str(result))
        self.assertNotIn("username", result)
        self.assertNotIn("password", result)
        self.bridge.config_store.save.assert_not_called()

    def test_invalid_or_missing_selection_never_falls_back_to_another_bound_account(self):
        cases = (
            ("", {}),
            ("missing", {}),
            ("foreign", {}),
            ("other", {"account_id": "missing"}),
            ("other", {"account_id": "foreign"}),
        )
        for active_id, selection in cases:
            self.bridge.config_store.load.return_value.active_account_ids = {"mabang": active_id}
            request = {**selection, "username": "forged-user", "password": "forged-secret"}
            with self.subTest(active=active_id, selection=selection), patch("backend.app_bridge.fetch_lz_inventory_warehouses") as loader:
                for result in (
                    self.bridge.get_lz_warehouse_inventory_info(request),
                    self.bridge.get_lz_warehouse_inventory_warehouses(request),
                    self.bridge.start_lz_warehouse_inventory({**request, "warehouse_names": ["泰国TH04仓"], "output_dir": "unused"}),
                ):
                    self.assertFalse(result["ok"])
                    self.assertTrue(result["requires_account"])
                    self.assertEqual(result["vendor"], "mabang")
                    self.assertNotIn("secret", str(result))
                loader.assert_not_called()
        self.bridge.tasks.start.assert_not_called()
        self.bridge.config_store.save.assert_not_called()

    def test_explicit_account_can_be_selected_without_any_active_account(self):
        self.bridge.config_store.load.return_value.active_account_ids = {}
        result = self.bridge.get_lz_warehouse_inventory_info({"account_id": "target"})
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["account_id"], result["account_name"]), ("target", "指定测试账号"))
        self.assertNotIn("secret", str(result))

    def test_active_account_start_uses_bound_credentials_and_context(self):
        names = ("泰国TH04仓",)
        job = SimpleNamespace(username="other-user", password="other-secret", output_dir=Path("exports"), warehouse_names=names)
        with patch("backend.app_bridge.validate_lz_inventory_payload", return_value=job) as validator:
            result = self.bridge.start_lz_warehouse_inventory({
                "username": "forged-user", "password": "forged-secret", "warehouse_names": list(names), "output_dir": "exports",
            })
        self.assertTrue(result["ok"], result)
        validated = validator.call_args.args[0]
        self.assertEqual((validated["account_id"], validated["username"], validated["password"]), ("other", "other-user", "other-secret"))
        self.assertEqual(self.bridge.tasks.start.call_args.kwargs["context"]["account_id"], "other")
        self.assertNotIn("secret", str(self.bridge.tasks.start.call_args.kwargs["context"]))

    def test_forged_credentials_cannot_repair_missing_bound_credentials(self):
        settings = self.bridge.config_store.load.return_value
        for username, password in (("", "stored-secret"), ("stored-user", "")):
            settings.accounts = [BoundAccount("other", "mabang", "测试账号", username, password)]
            with self.subTest(username_set=bool(username), password_set=bool(password)):
                result = self.bridge.start_lz_warehouse_inventory({
                    "username": "forged-user", "password": "forged-secret", "warehouse_names": ["泰国TH04仓"], "output_dir": "exports",
                })
                self.assertFalse(result["ok"], result)
                self.assertNotIn("secret", str(result))
        self.bridge.tasks.start.assert_not_called()
        self.bridge.config_store.save.assert_not_called()

    def test_start_overrides_untrusted_credentials_and_preserves_export_counts(self):
        names = ("泰国TH04仓", "菲律宾PH01仓")
        job = SimpleNamespace(username="fixture-lz-user", password="fixture-lz-secret", output_dir=Path("exports"), warehouse_names=names)
        with patch("backend.app_bridge.validate_lz_inventory_payload", return_value=job) as validator:
            result = self.bridge.start_lz_warehouse_inventory({
                "account_id": "target", "username": "untrusted", "password": "untrusted", "output_dir": "exports", "warehouse_names": list(names),
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

    def test_warehouse_loader_uses_active_account_and_identifies_its_result(self):
        warehouses = ({"id": "W1", "name": "任意仓库"}, {"id": "W2", "name": "菲律宾PH01仓"})
        with patch("backend.app_bridge.fetch_lz_inventory_warehouses", return_value=warehouses) as loader:
            result = self.bridge.get_lz_warehouse_inventory_warehouses()
        loader.assert_called_once_with("other-user", "other-secret")
        self.assertEqual(result, {"ok": True, "account_id": "other", "warehouses": list(warehouses)})
        self.assertNotIn("secret", str(result))
        with patch("backend.app_bridge.fetch_lz_inventory_warehouses", side_effect=RuntimeError("目录接口暂时不可用")):
            result = self.bridge.get_lz_warehouse_inventory_warehouses()
        self.assertEqual(result, {"ok": False, "error": "目录接口暂时不可用"})

    def test_warehouse_loader_explicit_selection_overrides_active_account(self):
        warehouses = ({"id": "A-only", "name": "A账号可见仓库"},)
        with patch("backend.app_bridge.fetch_lz_inventory_warehouses", return_value=warehouses) as loader:
            result = self.bridge.get_lz_warehouse_inventory_warehouses({"account_id": "target", "username": "forged", "password": "forged"})
        loader.assert_called_once_with("fixture-lz-user", "fixture-lz-secret")
        self.assertEqual(result, {"ok": True, "account_id": "target", "warehouses": list(warehouses)})
        self.assertEqual(self.bridge.config_store.load.return_value.active_account_ids["mabang"], "other")

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
