import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.core import ziniao_paths as paths


class RegistryKey:
    def __init__(self, root, path):
        self.root, self.path = root, path

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeRegistry:
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = "HKCU", "HKLM"
    KEY_READ, KEY_WOW64_64KEY, KEY_WOW64_32KEY = 1, 2, 4

    def __init__(self, records):
        self.records = records
        self.queries = []

    def OpenKey(self, root, name, reserved=0, access=0):
        if isinstance(root, RegistryKey):
            root, name = root.root, root.path + "\\" + name
        if (root, name) not in self.records:
            raise FileNotFoundError(name)
        return RegistryKey(root, name)

    def EnumKey(self, key, index):
        prefix = key.path + "\\"
        names = [name[len(prefix):] for root, name in self.records
                 if root == key.root and name.startswith(prefix) and "\\" not in name[len(prefix):]]
        if index >= len(names):
            raise OSError("no more keys")
        return names[index]

    def QueryValueEx(self, key, name):
        self.queries.append((key.path, name))
        value = self.records[(key.root, key.path)].get(name)
        if value is None:
            raise FileNotFoundError(name)
        return value, 1


class ZiniaoPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(patch.stopall)
        patch.dict(paths.os.environ, {}, clear=True).start()
        patch.object(paths.sys, "frozen", False, create=True).start()
        patch.object(paths, "__file__", str(self.root / "app" / "backend" / "core" / "ziniao_paths.py")).start()

    def executable(self, relative):
        candidate = self.root / relative
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.touch()
        return candidate

    def test_environment_path_precedes_registry_even_for_v5(self):
        explicit = self.executable("chosen/SuperBrowser.exe")
        detected = self.executable("detected/ziniao.exe")
        with patch.dict(paths.os.environ, {"ZINIAO_CLIENT_PATH": str(explicit)}), \
                patch.object(paths, "_registry_client_paths", return_value=[str(detected)]) as registry:
            self.assertEqual(paths.resolve_ziniao_client_path(), str(explicit))
        registry.assert_not_called()

    def test_registry_prefers_v6_even_when_v5_is_listed_first(self):
        v5 = self.executable("old/SuperBrowser.exe")
        v6 = self.executable("new/ziniao.exe")
        with patch.object(paths, "_registry_client_paths", return_value=[str(v5), str(v6) + ",0"]):
            self.assertEqual(paths.resolve_ziniao_client_path(), str(v6))

    def test_quoted_display_icon_directory_and_starter_are_normalized(self):
        executable = self.executable("install, stable/ziniao.exe")
        self.executable("install, stable/starter.exe")
        for raw in (f'"{executable}",0', str(executable.parent), f'"{executable.parent / "starter.exe"}",0'):
            with self.subTest(raw=raw), patch.object(paths, "_registry_client_paths", return_value=[raw]):
                self.assertEqual(paths.resolve_ziniao_client_path(), str(executable))

    def test_starter_without_actual_client_and_unrelated_exe_are_rejected(self):
        starter = self.executable("broken/starter.exe")
        unrelated = self.executable("broken/uninstaller.exe")
        with patch.object(paths, "_registry_client_paths", return_value=[str(starter), str(unrelated)]):
            self.assertEqual(paths.resolve_ziniao_client_path(), "")

    def test_registry_reads_app_paths_and_uninstall_values_with_query_value_ex(self):
        v6 = self.executable("registry/ziniao.exe")
        uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        app_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\ziniao.exe"
        registry = FakeRegistry({
            ("HKCU", app_path): {"": str(v6)},
            ("HKLM", uninstall): {},
            ("HKLM", uninstall + r"\ziniao-fixture"): {"DisplayName": "紫鸟浏览器", "DisplayIcon": f'"{v6}",0'},
            ("HKLM", uninstall + r"\other-fixture"): {"DisplayName": "Unrelated app", "InstallLocation": "unrelated"},
        })
        with patch.object(paths, "winreg", registry):
            candidates = paths._registry_client_paths()
            self.assertEqual(paths.resolve_ziniao_client_path(), str(v6))
        self.assertIn(str(v6), candidates)
        self.assertIn(f'"{v6}",0', candidates)
        self.assertNotIn("unrelated", candidates)
        self.assertIn((app_path, ""), registry.queries)
        self.assertIn((uninstall + r"\ziniao-fixture", "DisplayIcon"), registry.queries)

    def test_unavailable_or_denied_registry_falls_back_to_common_directory(self):
        executable = self.executable("program-files/ziniao/ziniao.exe")
        denied = SimpleNamespace(HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2, KEY_READ=1,
                                 OpenKey=lambda *args: (_ for _ in ()).throw(PermissionError("denied")))
        for registry in (None, denied):
            with self.subTest(registry=registry), patch.object(paths, "winreg", registry), \
                    patch.dict(paths.os.environ, {"ProgramFiles": str(executable.parent.parent)}):
                self.assertEqual(paths.resolve_ziniao_client_path(), str(executable))

    def test_missing_registry_values_do_not_abort_other_candidates(self):
        executable = self.executable("installed/ziniao.exe")
        uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        registry = FakeRegistry({
            ("HKCU", uninstall): {},
            ("HKCU", uninstall + r"\broken"): {"DisplayName": "紫鸟浏览器"},
            ("HKCU", uninstall + r"\valid"): {"DisplayName": "Ziniao", "InstallLocation": str(executable.parent)},
        })
        with patch.object(paths, "winreg", registry):
            self.assertEqual(paths.resolve_ziniao_client_path(), str(executable))

    def test_frozen_app_uses_executable_location_without_sibling_project(self):
        executable = self.executable("frozen/ziniao/ziniao.exe")
        with patch.object(paths.sys, "frozen", True), \
                patch.object(paths.sys, "executable", str(executable.parent.parent / "hqyl.exe")), \
                patch.object(paths, "_registry_client_paths", return_value=[]):
            self.assertEqual(paths.resolve_ziniao_client_path(), str(executable))

    def test_missing_client_returns_empty_without_legacy_project(self):
        with patch.object(paths, "_registry_client_paths", return_value=[]):
            self.assertEqual(paths.resolve_ziniao_client_path(), "")


if __name__ == "__main__":
    unittest.main()
