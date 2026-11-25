from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.app_bridge import AppBridge
from backend.build_profile import current_profile, profile_details, requires_internal_runtime
from backend.config_store import AppSettings
from scripts.package_release import package_release


ROOT = Path(__file__).resolve().parents[1]


class LimitedBuildTests(unittest.TestCase):
    def test_full_profile_passes_arguments_to_existing_operations(self):
        target = Mock(return_value={"ok": True})
        with patch.dict(os.environ, {"HQYL_BUILD_PROFILE": "full"}):
            self.assertEqual(requires_internal_runtime(target)("payload", mode=2), {"ok": True})
        target.assert_called_once_with("payload", mode=2)

    def test_limited_catalog_and_direct_bridge_calls_are_consistent(self):
        with patch.dict(os.environ, {"HQYL_BUILD_PROFILE": "limited"}), patch("backend.app_bridge.get_group_options", return_value=[]):
            bridge = AppBridge()
            bridge.config_store = SimpleNamespace(load=AppSettings)
            info = bridge.get_app_info()
            unavailable = {tool["key"] for tool in info["tools"] if tool["status"] == "unavailable"}
            self.assertEqual(unavailable, set(profile_details("limited")["unavailable_tools"]))
            ready = {tool["key"] for tool in info["tools"] if tool["status"] == "ready"}
            self.assertTrue({"lazada_campaign_download", "lazada_bill_detail", "mabang_arrival_query", "temu_shipping_channel"} <= ready)
            bridge.config_store = Mock()
            bridge.tasks = Mock()
            for name in dir(AppBridge):
                method = getattr(bridge, name)
                if callable(method) and hasattr(method, "__wrapped__"):
                    with self.subTest(operation=name):
                        result = method({})
                        self.assertFalse(result["ok"])
                        self.assertTrue(result["unavailable"])
            self.assertEqual(bridge.config_store.mock_calls, [])
            self.assertEqual(bridge.tasks.mock_calls, [])

    def test_external_loaders_reject_before_consulting_source_paths_or_cache(self):
        from backend.services import bigseller_sync, shopee_ads_recharge, lazada_withdrawal_statistics
        with patch.dict(os.environ, {"HQYL_BUILD_PROFILE": "limited"}), patch.object(shopee_ads_recharge, "_SUPERBROWSER_ROOT", "already-loaded"):
            for loader in (bigseller_sync.load_bigseller_request_util, shopee_ads_recharge._ensure_superbrowser_path,
                           lazada_withdrawal_statistics._ensure_superbrowser_path):
                with self.subTest(loader=loader.__module__):
                    with self.assertRaisesRegex(RuntimeError, "精简版"):
                        loader()

    def test_frozen_profile_cannot_be_overridden_by_environment(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "build_profile.json").write_text('{"name":"limited"}', encoding="utf-8")
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", str(root), create=True), patch.dict(os.environ, {"HQYL_BUILD_PROFILE": "full"}):
                self.assertEqual(current_profile()["name"], "limited")

    def test_pyinstaller_limited_profile_omits_external_sources_and_imports(self):
        with TemporaryDirectory() as temporary:
            analysis = Mock(return_value=SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[]))
            context = {"SPECPATH": temporary, "Analysis": analysis, "PYZ": Mock(), "EXE": Mock(), "COLLECT": Mock()}
            with patch.dict(os.environ, {"HQYL_BUILD_PROFILE": "limited"}), patch("PyInstaller.utils.hooks.collect_all", return_value=([], [], [])), patch("PyInstaller.utils.hooks.collect_submodules", return_value=[]):
                runpy.run_path(str(ROOT / "HQYLAutomation.spec"), init_globals=context)
            arguments = analysis.call_args.kwargs
            self.assertEqual(arguments["pathex"], [temporary])
            self.assertFalse(any(name.startswith(("main.", "util.", "implement.")) for name in arguments["hiddenimports"]))
            self.assertFalse(any("mabang_process" in target or "main/shopee" in target for _, target in arguments["datas"]))
            self.assertIn("main", arguments["excludes"])
            self.assertEqual(json.loads((Path(temporary) / "build/runtime_profile/build_profile.json").read_text())["name"], "limited")

    def test_full_spec_still_requires_internal_source(self):
        with TemporaryDirectory() as temporary, patch.dict(os.environ, {"HQYL_BUILD_PROFILE": "full"}):
            with self.assertRaisesRegex(FileNotFoundError, "Mabang util source"):
                runpy.run_path(str(ROOT / "HQYLAutomation.spec"), init_globals={"SPECPATH": temporary})

    def test_package_records_actual_frozen_profile_and_rejects_stale_check(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "release"
            release.mkdir()
            (release / "HQYLAutomationSetup_1.0.6.exe").write_bytes(b"fixture installer")
            exe = root / "dist/HQYLAutomation/HQYLAutomation.exe"
            (exe.parent / "_internal").mkdir(parents=True)
            exe.write_bytes(b"fixture frozen exe")
            (exe.parent / "_internal/build_profile.json").write_text('{"name":"limited"}')
            report = root / "check.json"
            report.write_text(json.dumps({"status": "passed", "version": "1.0.6", "build_profile": profile_details("limited"),
                                         "executable": {"sha256": hashlib.sha256(exe.read_bytes()).hexdigest().upper()}}), encoding="utf-8")
            with patch("scripts.package_release.__file__", str(root / "scripts/package_release.py")), patch("scripts.package_release._git", return_value=None):
                output = package_release("1.0.6", fresh_build=True, self_check_report=report)
                manifest = json.loads((output / "HQYLAutomation_1.0.6_BUILD_MANIFEST.json").read_text("utf-8"))
                self.assertEqual(manifest["build_profile"]["name"], "limited")
                self.assertEqual(manifest["validation"]["frozen_self_check"]["status"], "passed")
                self.assertIn("精简版", json.loads((output / "latest.json").read_text())["notes"])
                old_manifest = (output / "HQYLAutomation_1.0.6_BUILD_MANIFEST.json").read_bytes()
                exe.write_bytes(b"different binary")
                with self.assertRaisesRegex(ValueError, "does not match"):
                    package_release("1.0.6", fresh_build=True, self_check_report=report)
                self.assertEqual((output / "HQYLAutomation_1.0.6_BUILD_MANIFEST.json").read_bytes(), old_manifest)


if __name__ == "__main__":
    unittest.main()
