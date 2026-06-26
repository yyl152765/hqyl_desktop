from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from backend.config_store import BoundAccount, ConfigStore, DEFAULT_UPDATE_MANIFEST_URL
from backend.services.group_sales_report import get_group_options, validate_group_sales_payload


class GroupSalesReportServiceTests(unittest.TestCase):
    def test_validate_group_sales_payload_accepts_known_group(self) -> None:
        group = get_group_options()[0]
        with tempfile.TemporaryDirectory() as temp_dir:
            job = validate_group_sales_payload(
                {
                    "username": "u",
                    "password": "p",
                    "group_ids": [group["id"]],
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-18",
                    "output_dir": temp_dir,
                }
            )
        self.assertEqual(job.groups[0].id, group["id"])

    def test_validate_group_sales_payload_requires_group(self) -> None:
        with self.assertRaises(ValueError):
            validate_group_sales_payload(
                {
                    "username": "u",
                    "password": "p",
                    "group_ids": [],
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-18",
                    "output_dir": str(Path.cwd()),
                }
            )

    def test_config_store_round_trips_accounts_and_sales_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ConfigStore(Path(temp_dir) / "settings.json")
            saved = store.save(
                {
                    "output_dir": temp_dir,
                    "sales_group_ids": ["1057656", "1057656", "1047454"],
                    "accounts": [
                        BoundAccount("account-1", "mabang", "主账号", "u", "p")
                    ],
                    "active_account_ids": {"mabang": "account-1"},
                }
            )
            loaded = store.load()
        self.assertEqual(saved.accounts[0].password, "p")
        self.assertEqual(loaded.accounts[0].password, "p")
        self.assertEqual(loaded.active_account_ids, {"mabang": "account-1"})
        self.assertEqual(loaded.sales_group_ids, ["1057656", "1047454"])

    def test_config_store_migrates_legacy_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "settings.json"
            config_path.write_text(
                json.dumps({"username": "legacy-user", "password": "legacy-pass"}),
                encoding="utf-8",
            )
            loaded = ConfigStore(config_path).load()
        self.assertEqual(len(loaded.accounts), 1)
        self.assertEqual(loaded.accounts[0].vendor, "mabang")
        self.assertEqual(loaded.accounts[0].username, "legacy-user")
        self.assertEqual(loaded.active_account_ids, {"mabang": "legacy-mabang"})

    def test_config_store_migrates_legacy_update_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ConfigStore(Path(temp_dir) / "settings.json")
            saved = store.save(
                {"update_manifest_url": "http://47.112.20.68/hqyl/latest.json"}
            )
        self.assertEqual(saved.update_manifest_url, DEFAULT_UPDATE_MANIFEST_URL)

    def test_config_store_preserves_saved_captcha_password_on_partial_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ConfigStore(Path(temp_dir) / "settings.json")
            store.save({"captcha_username": "first-user", "captcha_password": "secret"})
            store.save({"captcha_username": "second-user"})
            loaded = store.load()
        self.assertEqual(loaded.captcha_username, "second-user")
        self.assertEqual(loaded.captcha_password, "secret")

    def test_config_store_seeds_dingtalk_user_map(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            loaded = ConfigStore(Path(temp_dir) / "settings.json").load()
        self.assertTrue(any(user.name == "01-JaJ" for user in loaded.dingtalk.users))

    def test_config_store_preserves_dingtalk_secret_on_partial_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ConfigStore(Path(temp_dir) / "settings.json")
            store.save({"dingtalk": {"app_key": "app-key", "app_secret": "app-secret", "users": [{"name": "王小妹", "user_id": "user-id"}]}})
            store.save({"dingtalk": {"app_key": "new-key"}})
            loaded = store.load()
        self.assertEqual(loaded.dingtalk.app_key, "new-key")
        self.assertEqual(loaded.dingtalk.app_secret, "app-secret")
        self.assertEqual(loaded.dingtalk.users[0].name, "王小妹")


if __name__ == "__main__":
    unittest.main()
