from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.app_bridge import AppBridge
from backend.sidebar_preferences import SidebarPreferencesStore


class SidebarPreferencesTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="sidebar-preferences-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "ui" / "sidebar.json"
        self.store = SidebarPreferencesStore(self.path)

    def test_missing_preferences_do_not_create_any_files(self) -> None:
        self.assertIsNone(self.store.load())
        self.assertFalse(self.path.parent.exists())

    def test_preferences_persist_across_instances_with_order_and_deduplication(self) -> None:
        result = self.store.save({
            "favorites": ["mabang_arrival_query", "purchase_log", "mabang_arrival_query"],
            "expandedGroups": ["mabang", "ziniao", "mabang"],
            "password": "must-not-be-stored",
        })
        expected = {
            "favorites": ["mabang_arrival_query", "purchase_log"],
            "expandedGroups": ["mabang", "ziniao"],
        }
        self.assertEqual(result, expected)
        self.assertEqual(SidebarPreferencesStore(self.path).load(), expected)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {
            "schema_version": 1, "preferences": expected,
        })

    def test_empty_favorites_and_all_collapsed_survive_restart(self) -> None:
        self.store.save({"favorites": ["purchase_log"], "expandedGroups": ["mabang"]})
        empty = {"favorites": [], "expandedGroups": []}
        self.store.save(empty)
        self.assertEqual(SidebarPreferencesStore(self.path).load(), empty)

    def test_corrupt_or_unsupported_files_fall_back_without_rewriting(self) -> None:
        self.path.parent.mkdir()
        empty = {"favorites": [], "expandedGroups": []}
        for contents in (
            b"{broken json", b"\xff\xfe", b"null", b"[]",
            json.dumps({"schema_version": 2, "preferences": empty}).encode(),
            json.dumps({"schema_version": True, "preferences": empty}).encode(),
            json.dumps({"schema_version": 1, "preferences": {"favorites": "bad"}}).encode(),
        ):
            with self.subTest(contents=contents):
                self.path.write_bytes(contents)
                self.assertIsNone(self.store.load())
                self.assertEqual(self.path.read_bytes(), contents)

    def test_invalid_input_never_overwrites_valid_preferences(self) -> None:
        self.store.save({"favorites": ["purchase_log"], "expandedGroups": ["mabang"]})
        original = self.path.read_bytes()
        invalid_payloads = [None, [], "invalid", {}, {"favorites": []}]
        for field in ("favorites", "expandedGroups"):
            for value in (None, {}, "mabang", ("mabang",), [1], [None], [True],
                          [""], ["中文"], ["a/b"], ["a.b"], [" a"], ["a\n"], ["a" * 81]):
                invalid_payloads.append({"favorites": [], "expandedGroups": [], field: value})
        invalid_payloads.extend([
            {"favorites": [f"module-{number}" for number in range(6)], "expandedGroups": []},
            {"favorites": [], "expandedGroups": [f"group-{number}" for number in range(33)]},
        ])
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.store.save(payload)
                self.assertEqual(self.path.read_bytes(), original)

    def test_valid_limits_and_identifiers_are_accepted(self) -> None:
        preferences = {
            "favorites": ["A" * 80, "camelCase", "module-name", "module_name", "123"],
            "expandedGroups": [f"group-{number}" for number in range(32)],
        }
        self.assertEqual(self.store.save(preferences), preferences)
        self.assertEqual(self.store.load(), preferences)

    def test_failed_atomic_replace_preserves_original_and_removes_temporary_file(self) -> None:
        self.store.save({"favorites": ["purchase_log"], "expandedGroups": ["mabang"]})
        original = self.path.read_bytes()
        with patch("backend.sidebar_preferences.os.replace", side_effect=OSError("failed")):
            with self.assertRaises(OSError):
                self.store.save({"favorites": [], "expandedGroups": []})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_concurrent_store_instances_leave_complete_preferences(self) -> None:
        preferences = [{"favorites": [f"module-{number}"], "expandedGroups": [f"group-{number}"]}
                       for number in range(12)]

        def save_and_read(value):
            store = SidebarPreferencesStore(self.path)
            store.save(value)
            return store.load()

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(save_and_read, preferences))
        for result in results:
            self.assertIn(result, preferences)
        self.assertIn(self.store.load(), preferences)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def make_bridge(self) -> tuple[AppBridge, SimpleNamespace]:
        config = SimpleNamespace(
            config_dir=self.path.parent,
            load=Mock(side_effect=AssertionError("must not read business settings")),
            save=Mock(side_effect=AssertionError("must not write business settings")),
        )
        with patch("backend.app_bridge.ConfigStore", return_value=config), \
                patch("backend.app_bridge.TaskManager"):
            return AppBridge(), config

    def test_bridge_uses_only_ui_preferences_and_restores_them_after_restart(self) -> None:
        bridge, config = self.make_bridge()
        self.assertEqual(bridge.get_sidebar_preferences(), {"ok": True, "preferences": None})
        self.path.parent.mkdir()
        settings_path = self.path.parent / "settings.json"
        settings_path.write_text("business settings sentinel", encoding="utf-8")
        preferences = {"favorites": ["purchase_log"], "expandedGroups": []}
        expected = {"ok": True, "preferences": preferences}
        self.assertEqual(bridge.save_sidebar_preferences(preferences), expected)
        restarted, restarted_config = self.make_bridge()
        self.assertEqual(restarted.get_sidebar_preferences(), expected)
        self.assertEqual(settings_path.read_text(encoding="utf-8"), "business settings sentinel")
        self.assertEqual(set(self.path.parent.iterdir()), {self.path, settings_path})
        for current_config in (config, restarted_config):
            current_config.load.assert_not_called()
            current_config.save.assert_not_called()

    def test_bridge_reports_invalid_input_and_generic_io_errors(self) -> None:
        bridge, _ = self.make_bridge()
        invalid = bridge.save_sidebar_preferences({"favorites": "private-input", "expandedGroups": []})
        self.assertFalse(invalid["ok"])
        self.assertNotIn("private-input", invalid["error"])
        for method, call in (("load", bridge.get_sidebar_preferences),
                             ("save", lambda: bridge.save_sidebar_preferences({"favorites": [], "expandedGroups": []}))):
            with self.subTest(method=method), patch.object(
                SidebarPreferencesStore, method, side_effect=OSError("private-path-and-file-content"),
            ):
                result = call()
                self.assertFalse(result["ok"])
                self.assertNotIn("private-path-and-file-content", result["error"])
                self.assertNotIn("preferences", result)


if __name__ == "__main__":
    unittest.main()
