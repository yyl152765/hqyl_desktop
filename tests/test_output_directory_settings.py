"""Output directory creation and recovery without touching real user settings."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import config_store
from backend.app_bridge import AppBridge
from backend.config_store import ConfigStore, DEFAULT_OUTPUT_DIR, normalize_output_directory


class OutputDirectorySettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target, value in (
            ("_base_local_data_dir", self.root / "local"),
            ("_load_legacy_dingtalk_credentials", {}),
            ("_load_default_dingtalk_user_bindings", []),
        ):
            patcher = patch.object(config_store, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = ConfigStore(self.root / "settings.json")
        self.bridge = AppBridge.__new__(AppBridge)
        self.bridge.config_store = self.store

    def test_new_settings_default_to_reports_without_requiring_the_drive_at_startup(self):
        with patch.object(config_store, "ensure_output_directory", side_effect=OSError("no D drive")) as create:
            settings = self.store.load()
            self.assertEqual(settings.output_dir, "D:\\报表")
            self.assertEqual(self.store.save({"output_dir": "  "}).output_dir, DEFAULT_OUTPUT_DIR)
            self.assertEqual(self.store.load().output_dir, DEFAULT_OUTPUT_DIR)
        create.assert_not_called()

    def test_manual_nonexistent_directory_is_created_saved_and_reusable(self):
        chosen = self.root / "新目录" / "报表"
        self.assertFalse(chosen.exists())
        result = self.bridge.save_settings({"output_dir": f' "{chosen}" '})
        self.assertTrue(result["ok"], result)
        self.assertTrue(chosen.is_dir())
        self.assertEqual(result["settings"]["output_dir"], str(chosen))
        self.assertEqual(ConfigStore(self.store.config_path).load().output_dir, str(chosen))
        self.assertEqual(list(chosen.iterdir()), [])
        self.assertTrue(self.bridge.save_settings({"output_dir": str(chosen)})["ok"])

    def test_fallback_does_not_overwrite_an_unreadable_settings_file(self):
        original = b'{"output_dir": "unfinished'
        self.store.config_path.write_bytes(original)
        self.assertEqual(self.store.load().output_dir, DEFAULT_OUTPUT_DIR)
        self.assertEqual(self.store.config_path.read_bytes(), original)

    def test_old_nested_report_path_is_repaired_and_other_preferences_survive(self):
        original_root = self.root / "custom"
        nested = original_root
        for index in range(13):
            nested /= f"菲律宾各组商品销量报表_20261001_20261009_{index:06d}"
        payload = {
            "output_dir": str(nested), "sales_group_ids": ["group"],
            "captcha_password": "fixture-password", "tool_output_dirs": {"custom:output": "C:/keep"},
            "dingtalk": {"app_key": "fixture-key", "app_secret": "fixture-secret", "users": []},
        }
        self.store.config_path.write_text(json.dumps(payload), encoding="utf-8")
        settings = self.store.load()
        self.assertEqual(settings.output_dir, str(original_root))
        self.assertEqual(settings.sales_group_ids, ["group"])
        self.assertEqual(settings.captcha_password, "fixture-password")
        self.assertEqual(settings.dingtalk.app_secret, "fixture-secret")
        self.assertEqual(settings.tool_output_dirs, {"custom:output": "C:/keep"})
        self.assertEqual(json.loads(self.store.config_path.read_text(encoding="utf-8"))["output_dir"], str(original_root))
        self.assertFalse(original_root.exists())

    def test_similar_custom_directory_names_are_preserved(self):
        for name in ("自定义报表", "菲律宾各组商品销量报表", "菲律宾各组商品销量报表_20261001_20261009_备注"):
            with self.subTest(name=name):
                chosen = str(self.root / name)
                self.store.save({"output_dir": chosen})
                self.assertEqual(self.store.load().output_dir, chosen)

    def test_windows_extended_and_unc_paths_recover_to_short_roots(self):
        run = "菲律宾各组商品销量报表_20261001_20261009_120000"
        self.assertEqual(normalize_output_directory(f"\\\\?\\D:\\报表\\{run}\\{run}_2\\"), "D:\\报表")
        self.assertEqual(normalize_output_directory(f"\\\\?\\UNC\\server\\share\\{run}\\"), "\\\\server\\share\\")

    def test_bad_directory_does_not_replace_saved_settings(self):
        self.store.save({"output_dir": str(self.root / "keep")})
        before = self.store.config_path.read_bytes()
        blocked = self.root / "existing-file"
        blocked.write_text("keep this file", encoding="utf-8")
        for value in (str(blocked), "relative-folder"):
            with self.subTest(value=value):
                result = self.bridge.save_settings({"output_dir": value})
                self.assertFalse(result["ok"], result)
                self.assertIn("输出目录", result["error"])
                self.assertEqual(self.store.config_path.read_bytes(), before)
        self.assertEqual(blocked.read_text(encoding="utf-8"), "keep this file")

    def test_permission_failure_is_reported_without_saving_bad_path(self):
        self.store.load()
        before = self.store.config_path.read_bytes()
        with patch.object(config_store.tempfile, "TemporaryFile", side_effect=PermissionError("denied")):
            result = self.bridge.save_settings({"output_dir": str(self.root / "readonly")})
        self.assertFalse(result["ok"], result)
        self.assertIn("写入权限", result["error"])
        self.assertEqual(self.store.config_path.read_bytes(), before)

    def test_nonexistent_drive_is_reported_without_changing_preferences(self):
        self.store.load()
        before = self.store.config_path.read_bytes()
        with patch.object(Path, "mkdir", side_effect=FileNotFoundError("drive unavailable")):
            result = self.bridge.save_settings({"output_dir": str(self.root / "missing-drive")})
        self.assertFalse(result["ok"], result)
        self.assertIn("盘符", result["error"])
        self.assertEqual(self.store.config_path.read_bytes(), before)

    def test_picker_handles_uncreated_path_and_cancel_preserves_config(self):
        self.store.load()
        before = self.store.config_path.read_bytes()
        dialog = Mock(return_value=None)
        webview = SimpleNamespace(FOLDER_DIALOG=1, windows=[SimpleNamespace(create_file_dialog=dialog)])
        with patch.dict("sys.modules", {"webview": webview}):
            cancelled = self.bridge.choose_output_dir(str(self.root / "not-yet" / "created"))
            self.assertTrue(cancelled["cancelled"])
            self.assertEqual(dialog.call_args.kwargs["directory"], str(self.root))
            self.assertEqual(self.store.config_path.read_bytes(), before)
            chosen = self.root / "picked"
            dialog.return_value = [str(chosen)]
            result = self.bridge.choose_output_dir(str(self.root))
        self.assertTrue(result["ok"], result)
        self.assertTrue(chosen.is_dir())
        self.assertEqual(result["path"], self.store.load().output_dir)


if __name__ == "__main__":
    unittest.main()
