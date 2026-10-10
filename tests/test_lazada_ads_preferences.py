"""Persist ads-page preferences using temporary settings, never user config."""
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from backend import config_store
from backend.config_store import AppSettings, BoundAccount, ConfigStore, DingTalkSettings, DingTalkUserBinding


class LazadaAdsPreferencesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "settings.json"
        for target, value in (
            ("_base_local_data_dir", self.root / "local"),
            ("desktop_path", self.root / "outputs"),
            ("_load_legacy_dingtalk_credentials", {}),
            ("_load_default_dingtalk_user_bindings", []),
        ):
            patcher = patch.object(config_store, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.account = BoundAccount("fixture-account", "ziniao", "fixture company", "fixture user", "fixture password")
        self.dingtalk = DingTalkSettings("fixture app", "fixture secret", [DingTalkUserBinding("fixture operator", "fixture id")])
        self.store = ConfigStore(self.path)
        self.store.save(AppSettings(accounts=[self.account], active_account_ids={"ziniao": self.account.id}, dingtalk=self.dingtalk))

    def save_preferences(self):
        return self.store.save({"lazada_ads_client_path": " C:/fixture/ziniao.exe ", "lazada_ads_workbook_id": " fixture-workbook "})

    def assert_existing_settings(self, settings):
        self.assertEqual(settings.accounts, [self.account])
        self.assertEqual(settings.active_account_ids, {"ziniao": self.account.id})
        self.assertEqual(settings.dingtalk, self.dingtalk)

    def test_older_config_without_preferences_loads_empty_defaults(self):
        payload = asdict(self.store.load())
        payload.pop("lazada_ads_client_path", None)
        payload.pop("lazada_ads_workbook_id", None)
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        settings = ConfigStore(self.path).load()
        self.assertEqual(settings.lazada_ads_client_path, "")
        self.assertEqual(settings.lazada_ads_workbook_id, "")
        self.assert_existing_settings(settings)

    def test_two_preferences_survive_reconstructed_store(self):
        self.save_preferences()
        settings = ConfigStore(self.path).load()
        self.assertEqual(settings.lazada_ads_client_path, "C:/fixture/ziniao.exe")
        self.assertEqual(settings.lazada_ads_workbook_id, "fixture-workbook")
        self.assert_existing_settings(settings)

    def test_partial_settings_save_preserves_preferences_and_accounts(self):
        self.save_preferences()
        self.store.save({"rows_per_page": 250})
        settings = ConfigStore(self.path).load()
        self.assertEqual(settings.rows_per_page, 250)
        self.assertEqual(settings.lazada_ads_client_path, "C:/fixture/ziniao.exe")
        self.assertEqual(settings.lazada_ads_workbook_id, "fixture-workbook")
        self.assert_existing_settings(settings)

    def test_updating_one_preference_preserves_the_other(self):
        for field, value, other_field, other_value in (
            ("lazada_ads_client_path", " C:/another/ziniao.exe ", "lazada_ads_workbook_id", "fixture-workbook"),
            ("lazada_ads_workbook_id", " another-workbook ", "lazada_ads_client_path", "C:/fixture/ziniao.exe"),
        ):
            with self.subTest(field=field):
                self.save_preferences()
                self.store.save({field: value})
                settings = ConfigStore(self.path).load()
                self.assertEqual(getattr(settings, field), value.strip())
                self.assertEqual(getattr(settings, other_field), other_value)
                self.assert_existing_settings(settings)

    def test_explicit_empty_whitespace_or_none_clears_requested_field(self):
        for field in ("lazada_ads_client_path", "lazada_ads_workbook_id"):
            for value in ("", " \t ", None):
                with self.subTest(field=field, value=value):
                    self.save_preferences()
                    self.store.save({field: value})
                    settings = ConfigStore(self.path).load()
                    self.assertEqual(getattr(settings, field), "")
                    other = "lazada_ads_workbook_id" if field == "lazada_ads_client_path" else "lazada_ads_client_path"
                    self.assertTrue(getattr(settings, other))
                    self.assert_existing_settings(settings)

    def test_loading_existing_preferences_strips_outer_whitespace(self):
        payload = asdict(self.store.load())
        payload.update(lazada_ads_client_path=" C:/fixture/ziniao.exe \n", lazada_ads_workbook_id=" fixture-workbook \t")
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        settings = ConfigStore(self.path).load()
        self.assertEqual(settings.lazada_ads_client_path, "C:/fixture/ziniao.exe")
        self.assertEqual(settings.lazada_ads_workbook_id, "fixture-workbook")

    def test_dataclass_save_roundtrips_trimmed_preferences(self):
        settings = self.store.load()
        settings.lazada_ads_client_path = " C:/fixture/ziniao.exe "
        settings.lazada_ads_workbook_id = " fixture-workbook "
        saved = self.store.save(settings)
        self.assertEqual(saved.lazada_ads_client_path, "C:/fixture/ziniao.exe")
        self.assertEqual(saved.lazada_ads_workbook_id, "fixture-workbook")
        self.assertEqual(ConfigStore(self.path).load(), saved)


if __name__ == "__main__":
    unittest.main()
