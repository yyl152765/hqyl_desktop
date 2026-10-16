"""Offline integration checks for tool/country output-directory isolation."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import app_bridge, config_store
from backend.app_bridge import AppBridge
from backend.config_store import AppSettings, BoundAccount, ConfigStore, DingTalkSettings, DingTalkUserBinding
from backend.output_paths import directory_scope_key, resolve_output_directories


class ToolOutputDirectoryIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.desktop = self.root / "Desktop"
        self.global_output = str(self.root / "unrelated-tool-output")
        self.legacy_screenshots = str(self.root / "legacy-bill-parent")
        for target, value in (
            ("desktop_path", self.desktop),
            ("_base_local_data_dir", self.root / "local"),
            ("_load_legacy_dingtalk_credentials", {}),
            ("_load_default_dingtalk_user_bindings", []),
        ):
            patcher = patch.object(config_store, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = ConfigStore(self.root / "settings.json")
        self.store.save(AppSettings(
            output_dir=self.global_output,
            lazada_bill_screenshot_dir=self.legacy_screenshots,
            accounts=[
                BoundAccount("fixture-ziniao", "ziniao", "Fixture company", "fixture-ziniao-user", "fixture-password"),
                BoundAccount("fixture-stock", "mabang", "Fixture stock", "fixture-stock-user", "fixture-password"),
            ],
            active_account_ids={"ziniao": "fixture-ziniao", "mabang": "fixture-stock"},
            dingtalk=DingTalkSettings("fixture-app", "fixture-secret", [DingTalkUserBinding("Fixture operator", "fixture-user-id")]),
        ))
        self.bridge = AppBridge.__new__(AppBridge)
        self.bridge.config_store = self.store
        self.bridge.tasks = Mock()
        self.bridge.tasks.start.return_value = {"ok": True, "id": "fixture-task"}

    def save_scope(self, tool: str, country: str = "TH", **paths: str) -> None:
        result = self.bridge.save_scoped_output_dirs({"tool": tool, "country": country, **paths})
        self.assertTrue(result["ok"], result)

    def assert_legacy_unchanged(self) -> None:
        settings = self.store.load()
        self.assertEqual(settings.output_dir, self.global_output)
        self.assertEqual(settings.lazada_bill_screenshot_dir, self.legacy_screenshots)

    def test_info_defaults_ignore_unrelated_global_and_legacy_bill_parent(self) -> None:
        with patch.object(app_bridge, "resolve_lazada_monthly_report_runtime_paths", return_value={}):
            ads = self.bridge.get_lazada_ads_data_info()
            bill = self.bridge.get_lazada_bill_detail_info()
            stock = self.bridge.get_lz_warehouse_inventory_info()
        expected = (
            (ads, "lz泰国广告数据"),
            (bill, "Lazada泰国账单明细"),
            (stock, "lz各仓库存"),
        )
        for result, folder in expected:
            with self.subTest(folder=folder):
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["output_dir"], str(self.desktop / "log" / folder))
        self.assertEqual(bill["screenshot_root"], str(self.desktop / "log" / "Lazada泰国账单明细截图"))
        self.assertEqual(self.store.load().tool_output_dirs, {})
        self.assert_legacy_unchanged()

    def test_each_start_uses_its_scope_and_preserves_the_other_tools(self) -> None:
        ads_dir = str(self.root / "ads-custom")
        bill_dir = str(self.root / "vn-bill-custom")
        bill_shots = str(self.root / "vn-shots-custom")
        stock_dir = str(self.root / "stock-custom")
        self.save_scope("lazada_ads_data", output_dir=ads_dir)
        self.save_scope("lazada_bill_detail", "VN", output_dir=bill_dir, screenshot_root=bill_shots)
        self.save_scope("lz_warehouse_inventory", output_dir=stock_dir)
        scopes_before = dict(self.store.load().tool_output_dirs)
        starts = (
            (self.bridge.start_lazada_ads_data, {"target_date": "2026-01-01", "store_names": ["Fixture shop"]}, ads_dir, None),
            (self.bridge.start_lazada_bill_detail, {"country": "VN", "start_date": "2026-01-01", "end_date": "2026-01-02", "store_names": ["Fixture shop"]}, bill_dir, bill_shots),
            (self.bridge.start_lz_warehouse_inventory, {"warehouse_names": ["Fixture warehouse"]}, stock_dir, None),
        )
        for start, payload, expected_output, expected_shots in starts:
            with self.subTest(tool=start.__name__):
                result = start(payload)
                self.assertTrue(result["ok"], result)
                context = self.bridge.tasks.start.call_args.kwargs["context"]
                self.assertEqual(context["output_root"], expected_output)
                if expected_shots:
                    self.assertEqual(context["screenshot_root"], expected_shots)
                self.assertEqual(self.store.load().tool_output_dirs, scopes_before)
                self.assert_legacy_unchanged()

    def test_bill_screenshot_preferences_apply_only_to_requested_country(self) -> None:
        th_shots = str(self.root / "th-shots")
        ph_shots = str(self.root / "ph-shots")
        self.save_scope("lazada_bill_detail", "TH", screenshot_root=th_shots)
        saved = self.bridge.save_lazada_bill_preferences({"country": "PH", "screenshot_root": ph_shots})
        self.assertTrue(saved["ok"], saved)
        settings = self.store.load()
        self.assertEqual(resolve_output_directories(settings, "lazada_bill_detail", "PH")["screenshot_root"], ph_shots)
        self.assertEqual(resolve_output_directories(settings, "lazada_bill_detail", "TH")["screenshot_root"], th_shots)
        self.assertEqual(resolve_output_directories(settings, "lazada_bill_detail", "VN")["screenshot_root"], str(self.desktop / "log" / "Lazada越南账单明细截图"))
        self.assert_legacy_unchanged()

    def test_picker_captures_original_country_while_another_scope_is_saved(self) -> None:
        chosen_th = str(self.root / "chosen-th")
        chosen_ph = str(self.root / "chosen-ph")
        request = {"tool": "lazada_bill_detail", "country": "TH", "kind": "output"}

        def choose_while_country_changes(*args, **kwargs):
            request["country"] = "PH"
            self.save_scope("lazada_bill_detail", "PH", output_dir=chosen_ph)
            return [chosen_th]

        dialog = Mock(side_effect=choose_while_country_changes)
        webview = SimpleNamespace(FOLDER_DIALOG=1, windows=[SimpleNamespace(create_file_dialog=dialog)])
        with patch.dict("sys.modules", {"webview": webview}):
            result = self.bridge.choose_scoped_output_dir(request)
        self.assertTrue(result["ok"], result)
        settings = self.store.load()
        self.assertEqual(resolve_output_directories(settings, "lazada_bill_detail", "TH")["output_dir"], chosen_th)
        self.assertEqual(resolve_output_directories(settings, "lazada_bill_detail", "PH")["output_dir"], chosen_ph)
        self.assertEqual(set(settings.tool_output_dirs), {
            directory_scope_key("lazada_bill_detail", "TH"),
            directory_scope_key("lazada_bill_detail", "PH"),
        })
        self.assert_legacy_unchanged()

    def test_cancelled_screenshot_picker_preserves_existing_country_paths(self) -> None:
        self.save_scope("lazada_bill_detail", "VN", output_dir=str(self.root / "vn-output"), screenshot_root=str(self.root / "vn-shots"))
        before = self.store.config_path.read_bytes()
        dialog = Mock(return_value=None)
        webview = SimpleNamespace(FOLDER_DIALOG=1, windows=[SimpleNamespace(create_file_dialog=dialog)])
        with patch.dict("sys.modules", {"webview": webview}):
            result = self.bridge.choose_scoped_output_dir({"tool": "lazada_bill_detail", "country": "VN", "kind": "screenshot"})
        self.assertTrue(result.get("cancelled"), result)
        self.assertEqual(self.store.config_path.read_bytes(), before)
        self.assertEqual(dialog.call_args.kwargs["directory"], str(self.root / "vn-shots"))


if __name__ == "__main__":
    unittest.main()
