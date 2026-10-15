from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import config_store
from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, ConfigStore
from backend.output_paths import directory_scope_key, resolve_output_directories


class ScopedOutputDirectoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target, value in (
            ("desktop_path", self.root / "Desktop"),
            ("_base_local_data_dir", self.root / "local"),
            ("_load_legacy_dingtalk_credentials", {}),
            ("_load_default_dingtalk_user_bindings", []),
        ):
            patcher = patch.object(config_store, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = ConfigStore(self.root / "settings.json")
        self.store.save(AppSettings(output_dir=str(self.root / "unrelated-legacy")))
        self.bridge = AppBridge.__new__(AppBridge)
        self.bridge.config_store = self.store

    def test_defaults_ignore_global_and_legacy_screenshot_directories(self):
        settings = self.store.save({"lazada_bill_screenshot_dir": "old-screenshot-parent"})
        expected = (
            ("lazada_ads_data", "TH", "lz泰国广告数据"),
            ("lazada_bill_detail", "TH", "Lazada泰国账单明细"),
            ("lazada_bill_detail", "VN", "Lazada越南账单明细"),
            ("lz_warehouse_inventory", "TH", "lz各仓库存"),
        )
        for tool, country, folder in expected:
            with self.subTest(tool=tool, country=country):
                resolved = resolve_output_directories(settings, tool, country)
                self.assertEqual(resolved["output_dir"], str(self.root / "Desktop" / "log" / folder))
        bill = resolve_output_directories(settings, "lazada_bill_detail", "PH")
        self.assertEqual(bill["screenshot_root"], str(self.root / "Desktop" / "log" / "Lazada菲律宾账单明细截图"))

    def test_config_map_incremental_merge_reset_and_reload_preserve_other_settings(self):
        first = directory_scope_key("lazada_ads_data", "th")
        second = directory_scope_key("lazada_bill_detail", "vn", "screenshot")
        self.store.save({"tool_output_dirs": {first: " C:/custom/ads "}})
        self.store.save({"tool_output_dirs": {second: "D:/custom/vn-shots"}})
        saved = ConfigStore(self.store.config_path).load()
        self.assertEqual(saved.tool_output_dirs, {first: "C:/custom/ads", second: "D:/custom/vn-shots"})
        self.assertEqual(saved.output_dir, str(self.root / "unrelated-legacy"))
        reset = self.store.save({"tool_output_dirs": {first: ""}})
        self.assertEqual(resolve_output_directories(reset, "lazada_ads_data")["output_dir"], str(self.root / "Desktop" / "log" / "lz泰国广告数据"))
        self.assertEqual(resolve_output_directories(reset, "lazada_bill_detail", "VN")["screenshot_root"], "D:/custom/vn-shots")

    def test_older_config_without_scope_map_loads_safely(self):
        payload = json.loads(self.store.config_path.read_text(encoding="utf-8"))
        payload.pop("tool_output_dirs", None)
        self.store.config_path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = self.store.load()
        self.assertEqual(loaded.tool_output_dirs, {})
        self.assertEqual(loaded.output_dir, str(self.root / "unrelated-legacy"))

    def test_concurrent_scoped_saves_merge_without_losing_other_preferences(self):
        start = threading.Barrier(2)
        failures = []
        original_write = ConfigStore._write_settings

        def slow_write(store, settings):
            time.sleep(0.001)
            original_write(store, settings)

        def save_scopes(prefix):
            try:
                other_store = ConfigStore(self.store.config_path)
                start.wait(timeout=5)
                for index in range(20):
                    other_store.save({"tool_output_dirs": {f"{prefix}:{index}": f"D:/{prefix}/{index}"}})
                    other_store.load()
            except Exception as exc:
                failures.append(exc)

        self.store.save({"lazada_ads_client_path": "C:/saved-client.exe"})
        with patch.object(ConfigStore, "_write_settings", slow_write):
            threads = [threading.Thread(target=save_scopes, args=(prefix,)) for prefix in ("ads", "bill")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        settings = self.store.load()
        self.assertEqual(len(settings.tool_output_dirs), 40)
        self.assertEqual(settings.lazada_ads_client_path, "C:/saved-client.exe")
        self.assertEqual(settings.output_dir, str(self.root / "unrelated-legacy"))

    def test_manual_country_paths_are_persisted_only_for_their_scope(self):
        saved = self.bridge.save_scoped_output_dirs({
            "tool": "lazada_bill_detail", "country": "VN", "output_dir": "D:/vn-output", "screenshot_root": "D:/vn-shots"
        })
        self.assertTrue(saved["ok"], saved)
        settings = self.store.load()
        self.assertEqual(resolve_output_directories(settings, "lazada_bill_detail", "VN")["output_dir"], "D:/vn-output")
        self.assertEqual(resolve_output_directories(settings, "lazada_bill_detail", "VN")["screenshot_root"], "D:/vn-shots")
        self.assertEqual(resolve_output_directories(settings, "lazada_bill_detail", "TH")["output_dir"], str(self.root / "Desktop" / "log" / "Lazada泰国账单明细"))
        self.assertEqual(settings.output_dir, str(self.root / "unrelated-legacy"))

    def test_picker_updates_only_requested_scope_and_cancel_writes_nothing(self):
        dialog = Mock(return_value=[str(self.root / "chosen-stock")])
        webview = SimpleNamespace(FOLDER_DIALOG=1, windows=[SimpleNamespace(create_file_dialog=dialog)])
        with patch.dict("sys.modules", {"webview": webview}):
            result = self.bridge.choose_scoped_output_dir({"tool": "lz_warehouse_inventory", "current_dir": "D:/current"})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["path"], str(self.root / "chosen-stock"))
            self.assertEqual(dialog.call_args.kwargs["directory"], "D:/current")
            before = self.store.config_path.read_bytes()
            dialog.return_value = None
            cancelled = self.bridge.choose_scoped_output_dir({"tool": "lazada_ads_data"})
        self.assertTrue(cancelled["cancelled"])
        self.assertEqual(self.store.config_path.read_bytes(), before)
        settings = self.store.load()
        self.assertEqual(settings.output_dir, str(self.root / "unrelated-legacy"))
        self.assertEqual(resolve_output_directories(settings, "lz_warehouse_inventory")["output_dir"], result["path"])
        self.assertNotEqual(resolve_output_directories(settings, "lazada_ads_data")["output_dir"], result["path"])

    def test_invalid_tool_country_or_screenshot_scope_cannot_change_settings(self):
        for payload in (
            {"tool": "unknown", "output_dir": "bad"},
            {"tool": "lazada_bill_detail", "country": "unknown", "output_dir": "bad"},
            {"tool": "lz_warehouse_inventory", "screenshot_root": "bad"},
        ):
            with self.subTest(payload=payload):
                before = self.store.config_path.read_bytes()
                result = self.bridge.save_scoped_output_dirs(payload)
                self.assertFalse(result["ok"], result)
                self.assertEqual(self.store.config_path.read_bytes(), before)
