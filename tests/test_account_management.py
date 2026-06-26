from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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

    def test_task_requires_bound_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = self.make_bridge(temp_dir)
            result = bridge.start_purchase_log_query({})
        self.assertFalse(result["ok"])
        self.assertTrue(result["requires_account"])
        self.assertEqual(result["vendor"], "mabang")


if __name__ == "__main__":
    unittest.main()
