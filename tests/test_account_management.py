from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.app_bridge import AppBridge
from backend.config_store import ConfigStore


class AccountManagementTests(unittest.TestCase):
    def make_bridge(self, temp_dir: str) -> AppBridge:
        bridge = AppBridge()
        bridge.config_store = ConfigStore(Path(temp_dir) / "settings.json")
        return bridge

    def test_account_crud_never_returns_password(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = self.make_bridge(temp_dir)
            added = bridge.add_account(
                {
                    "vendor": "mabang",
                    "name": "菲律宾主账号",
                    "username": "demo-user",
                    "password": "secret-value",
                }
            )
            self.assertTrue(added["ok"])
            account = added["account_state"]["accounts"][0]
            self.assertNotIn("password", account)
            self.assertEqual(account["username"], "demo-user")

            account_id = account["id"]
            resolved = bridge._payload_with_account({"account_id": account_id}, "mabang")
            self.assertEqual(resolved["account_id"], account_id)
            selected = bridge.select_account("mabang", account_id)
            self.assertTrue(selected["ok"])
            self.assertEqual(selected["account_state"]["active_account_ids"]["mabang"], account_id)

            deleted = bridge.delete_account(account_id)
            self.assertTrue(deleted["ok"])
            self.assertEqual(deleted["account_state"]["accounts"], [])

    def test_bigseller_account_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = self.make_bridge(temp_dir)
            added = bridge.add_account(
                {
                    "vendor": "bigseller",
                    "name": "BigSeller 主账号",
                    "username": "bs-user",
                    "password": "secret-value",
                    "extra": {"company": "紫鸟公司名不应保存"},
                }
            )
            self.assertTrue(added["ok"])
            account = added["account_state"]["accounts"][0]
            self.assertEqual(account["vendor"], "bigseller")
            self.assertEqual(account["extra"], {})
            self.assertNotIn("password", account)
            self.assertEqual(added["account_state"]["active_account_ids"]["bigseller"], account["id"])

    def test_echotik_account_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = self.make_bridge(temp_dir)
            added = bridge.add_account(
                {
                    "vendor": "echotik",
                    "name": "EchoTik 泰国",
                    "username": "demo@example.com",
                    "password": "secret-value",
                    "extra": {"company": "不应保存"},
                }
            )
            self.assertTrue(added["ok"])
            account = added["account_state"]["accounts"][0]
            self.assertEqual(account["vendor"], "echotik")
            self.assertEqual(account["extra"], {})
            self.assertNotIn("password", account)
            self.assertEqual(added["account_state"]["active_account_ids"]["echotik"], account["id"])

    def test_ziniao_account_keeps_company(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = self.make_bridge(temp_dir)
            added = bridge.add_account(
                {
                    "vendor": "ziniao",
                    "username": "zn-user",
                    "password": "secret-value",
                    "extra": {"company": "紫鸟公司"},
                }
            )
            self.assertTrue(added["ok"])
            account = added["account_state"]["accounts"][0]
            self.assertEqual(account["vendor"], "ziniao")
            self.assertEqual(account["name"], "紫鸟公司")
            self.assertEqual(account["extra"], {"company": "紫鸟公司"})

            payload = bridge._payload_with_account({}, "ziniao")
            self.assertEqual(payload["company"], "紫鸟公司")
            self.assertEqual(payload["username"], "zn-user")
            self.assertEqual(payload["password"], "secret-value")

    def test_task_requires_bound_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = self.make_bridge(temp_dir)
            result = bridge.start_purchase_log_query({})
        self.assertFalse(result["ok"])
        self.assertTrue(result["requires_account"])
        self.assertEqual(result["vendor"], "mabang")

    def test_bigseller_item_query_requires_bound_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = self.make_bridge(temp_dir)
            bridge.tasks = Mock()
            result = bridge.start_bigseller_item_id_query(
                {"username": "unbound-user", "password": "unbound-password", "skus": "SKU-1"}
            )
            bridge.tasks.start.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertTrue(result["requires_account"])
        self.assertEqual(result["vendor"], "bigseller")

    def test_bigseller_item_query_uses_bound_account_and_preserves_service_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = self.make_bridge(temp_dir)
            account = bridge.add_account(
                {"vendor": "bigseller", "username": "bound-bs-user", "password": "bound-bs-secret"}
            )["account_state"]["accounts"][0]
            bridge.config_store.save(
                {"captcha_username": "bound-captcha-user", "captcha_password": "bound-captcha-secret"}
            )
            output_dir = Path(temp_dir) / "exports"
            job = SimpleNamespace(output_dir=output_dir, skus=("SKU-1", "SKU-2"))
            bridge.tasks = Mock()
            task_started = {"ok": True, "id": "bs-query-task"}
            bridge.tasks.start.return_value = task_started
            service_result = {
                "sku_count": 2,
                "matched_count": 1,
                "not_found_count": 1,
                "failed_count": 0,
                "output_file": str(output_dir / "result.xlsx"),
                "output_dir": str(output_dir),
                "rows": [{"sku": "SKU-1", "item_id": "1234567890123456789"}],
                "preview_limited": False,
            }
            with (
                patch("backend.app_bridge.validate_bigseller_item_id_query_payload", return_value=job) as validate,
                patch("backend.app_bridge.run_bigseller_item_id_query", return_value=service_result) as run,
                patch.object(bridge.config_store, "save", wraps=bridge.config_store.save) as save,
            ):
                started = bridge.start_bigseller_item_id_query(
                    {
                        "sku_text": "SKU-1\nSKU-2",
                        "output_dir": str(output_dir),
                        "username": "frontend-override",
                        "password": "frontend-override",
                        "captcha_username": "frontend-override",
                        "captcha_password": "frontend-override",
                    }
                )
                self.assertEqual(started, task_started)
                request_payload = validate.call_args.args[0]
                self.assertEqual(request_payload["account_id"], account["id"])
                self.assertEqual(request_payload["username"], "bound-bs-user")
                self.assertEqual(request_payload["password"], "bound-bs-secret")
                self.assertEqual(request_payload["captcha_username"], "bound-captcha-user")
                self.assertEqual(request_payload["captcha_password"], "bound-captcha-secret")
                self.assertEqual(request_payload["sku_text"], "SKU-1\nSKU-2")
                save.assert_called_once_with({"output_dir": str(output_dir)})
                bridge.tasks.start.assert_called_once()
                self.assertEqual(bridge.tasks.start.call_args.args[0], "BS 商品ID查询")
                self.assertEqual(bridge.tasks.start.call_args.kwargs, {
                    "tool": "bigseller_item_id_query", "context": {"sku_count": 2},
                })
                runner = bridge.tasks.start.call_args.args[1]
                progress = Mock()
                self.assertIs(runner(progress), service_result)
                run.assert_called_once_with(job, progress)

    def test_bigseller_item_query_validation_error_does_not_start_or_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = self.make_bridge(temp_dir)
            bridge.add_account(
                {"vendor": "bigseller", "username": "bound-bs-user", "password": "bound-bs-secret"}
            )
            bridge.tasks = Mock()
            with (
                patch("backend.app_bridge.validate_bigseller_item_id_query_payload", side_effect=ValueError("请输入 SKU")),
                patch.object(bridge.config_store, "save") as save,
            ):
                result = bridge.start_bigseller_item_id_query({})
                save.assert_not_called()
            bridge.tasks.start.assert_not_called()
        self.assertEqual(result, {"ok": False, "error": "请输入 SKU"})

    def test_bigseller_item_query_uses_bigseller_account_gate_in_tool_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = self.make_bridge(temp_dir)
            tool = next(
                tool for tool in bridge.get_app_info()["tools"]
                if tool["key"] == "bigseller_item_id_query"
            )
        self.assertEqual(tool["vendor"], "bigseller")
        self.assertEqual(tool["status"], "ready")


if __name__ == "__main__":
    unittest.main()
