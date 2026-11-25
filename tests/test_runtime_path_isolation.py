from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import patch

from backend import config_store, runtime_paths
from backend.services import bigseller_sync, group_sales_report, lazada_withdrawal_statistics, shopee_ads_recharge


class RuntimePathIsolationTests(unittest.TestCase):
    def test_browser_services_use_owned_source_despite_old_project_and_environment(self):
        for service in (shopee_ads_recharge, lazada_withdrawal_statistics):
            with self.subTest(service=service.__name__), TemporaryDirectory() as directory:
                workspace = Path(directory)
                project = workspace / "desktop"
                owned = project / "runtime" / "superbrowser_process"
                neighbor = workspace / "superbrowser_process"
                for root, marker in ((owned, "desktop"), (neighbor, "other-project")):
                    (root / "main").mkdir(parents=True)
                    (root / "_hqyl_runtime_isolation_probe.py").write_text(f"SOURCE = {marker!r}\n", encoding="utf-8")
                original_path = sys.path[:]
                try:
                    # A launcher's inherited path may already contain the owned
                    # runtime, but place a different project ahead of it.
                    sys.path[:0] = [str(neighbor), str(owned)]
                    with (
                        patch.object(runtime_paths, "project_root", return_value=project),
                        patch.object(sys, "frozen", False, create=True),
                        patch.object(service, "_SUPERBROWSER_ROOT", None),
                        patch.dict(sys.modules, {name: None for name in ("main", "implement", "util", "config")}),
                        patch.dict(os.environ, {"SUPERBROWSER_PROCESS_ROOT": str(neighbor)}),
                    ):
                        service._ensure_superbrowser_path()
                        probe = importlib.import_module("_hqyl_runtime_isolation_probe")
                        self.assertEqual(probe.SOURCE, "desktop")
                        self.assertEqual(Path(service._SUPERBROWSER_ROOT), owned.resolve())
                        self.assertEqual(sys.path[0], str(owned))
                finally:
                    sys.path[:] = original_path
                    sys.modules.pop("_hqyl_runtime_isolation_probe", None)

    def test_loaded_foreign_package_is_rejected_without_mutating_it(self):
        with TemporaryDirectory() as directory:
            project = Path(directory) / "desktop"
            (project / "runtime/superbrowser_process/main").mkdir(parents=True)
            foreign = ModuleType("util")
            foreign.__file__ = str(Path(directory) / "superbrowser_process/util/__init__.py")
            with (
                patch.object(runtime_paths, "project_root", return_value=project),
                patch.object(sys, "frozen", False, create=True),
                patch.dict(sys.modules, {"main": None, "implement": None, "config": None, "util": foreign}),
            ):
                with self.assertRaisesRegex(ModuleNotFoundError, "其他项目的 util"):
                    runtime_paths.ensure_superbrowser_import_path()
                self.assertIs(sys.modules["util"], foreign)

    def test_missing_owned_browser_source_does_not_fall_back_to_a_neighbor(self):
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            neighbor = workspace / "superbrowser_process"
            (neighbor / "main").mkdir(parents=True)
            with (
                patch.object(runtime_paths, "project_root", return_value=workspace / "desktop"),
                patch.object(sys, "frozen", False, create=True),
                patch.dict(os.environ, {"SUPERBROWSER_PROCESS_ROOT": str(neighbor)}),
            ):
                with self.assertRaisesRegex(ModuleNotFoundError, "runtime/superbrowser_process"):
                    runtime_paths.ensure_superbrowser_import_path()

    def test_bigseller_ignores_other_project_and_config_overrides(self):
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            project = workspace / "desktop"
            owned = project / "runtime" / "mabang_process"
            neighbor = workspace / "mabang_process"
            for root, marker in ((owned, "desktop"), (neighbor, "other-project")):
                path = root / "vietnam" / "config" / "BigSeller库存同步.yaml"
                path.parent.mkdir(parents=True)
                path.write_text(f"process_config:\n  process_name: {marker}\n", encoding="utf-8")
            with (
                patch.object(runtime_paths, "project_root", return_value=project),
                patch.object(sys, "frozen", False, create=True),
                patch.dict(os.environ, {
                    "HQYL_MABANG_PROCESS_ROOT": str(neighbor),
                    "HQYL_BIGSELLER_CONFIG": str(neighbor / "vietnam/config/BigSeller库存同步.yaml"),
                }),
            ):
                self.assertEqual(bigseller_sync.mabang_process_root(), owned.resolve())
                self.assertEqual(bigseller_sync.load_reference_bigseller_config()["process_name"], "desktop")

    def test_legacy_credentials_are_not_imported_from_other_source_projects(self):
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            project = workspace / "desktop"
            external = workspace / "superbrowser_process/config/config.yaml"
            external.parent.mkdir(parents=True)
            external.write_text("dingtalk:\n  app:\n    rpa:\n      app_key: foreign\n      app_secret: foreign\n", encoding="utf-8")
            with (
                patch.object(runtime_paths, "project_root", return_value=project),
                patch.object(sys, "frozen", False, create=True),
                patch.dict(os.environ, {"HQYL_LEGACY_DINGTALK_CONFIG": str(external)}),
            ):
                candidates = config_store._legacy_dingtalk_config_candidates(include_source_fallback=True)
                self.assertTrue(all(path.is_relative_to(project) for path in candidates))
                self.assertEqual(config_store._load_legacy_dingtalk_credentials(include_source_fallback=True), {})

    def test_optional_group_defaults_do_not_read_neighboring_mabang_project(self):
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            project = workspace / "desktop"
            external = workspace / "mabang_process/philippines/config/各组商品销量报表.yaml"
            external.parent.mkdir(parents=True)
            external.write_text("process_config:\n  groups:\n    - name: Foreign\n      shop_label_id: foreign-id\n", encoding="utf-8")
            with (
                patch.object(runtime_paths, "project_root", return_value=project),
                patch.object(sys, "frozen", False, create=True),
            ):
                self.assertEqual(group_sales_report._load_groups_from_mabang_config(), ())
                owned = project / "runtime/mabang_process/philippines/config/各组商品销量报表.yaml"
                owned.parent.mkdir(parents=True)
                owned.write_text("process_config:\n  groups:\n    - name: Desktop\n      shop_label_id: local-id\n", encoding="utf-8")
                result = group_sales_report._load_groups_from_mabang_config()
                self.assertEqual([(item.name, item.shop_label_id) for item in result], [("Desktop", "local-id")])

    def test_frozen_runtime_uses_installer_layout_without_source_directories(self):
        with TemporaryDirectory() as directory:
            payload = Path(directory) / "_internal"
            original_path = sys.path[:]
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", str(payload), create=True),
                patch.dict(os.environ, {"SUPERBROWSER_PROCESS_ROOT": "ignored", "HQYL_MABANG_PROCESS_ROOT": "ignored"}),
            ):
                self.assertEqual(runtime_paths.superbrowser_root(), payload)
                self.assertEqual(bigseller_sync.mabang_process_root(), payload / "mabang_process")
                self.assertEqual(runtime_paths.ensure_superbrowser_import_path(), "<bundled>")
                self.assertEqual(sys.path, original_path)



if __name__ == "__main__":
    unittest.main()
