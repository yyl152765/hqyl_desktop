import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from backend.update_sources import installer_object_url
from scripts.package_release import (
    _build_manifest, create_upload_rar, find_rar, package_release,
)


class UploadPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.release = self.root / "release"
        self.release.mkdir()
        self.version = "2.3.4"
        self.installer = self.release / "HQYLAutomationSetup_2.3.4.exe"
        self.installer.write_bytes(b"MZ fixture installer\x00\x80\xff")
        self.manifest = self.release / "HQYLAutomation_2.3.4_BUILD_MANIFEST.json"
        for name, source in {
            "backend/app_bridge.py": 'APP_VERSION = "2.3.4"\n',
            "packaging/hqyl_automation.iss": '#define MyAppVersion "2.3.4"\n',
            "frontend/assets/common.js": 'const APP = { version: "2.3.4" };\n',
        }.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        patcher = patch("scripts.package_release.__file__", str(self.root / "scripts/package_release.py"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_auto_version_produces_exactly_four_consistent_files(self):
        output = package_release(notes="本次更新说明")
        expected = {self.installer.name, self.manifest.name, "latest.json", "SHA256.txt"}
        self.assertEqual({path.name for path in output.iterdir()}, expected)
        digest = hashlib.sha256(self.installer.read_bytes()).hexdigest().upper()
        latest = json.loads((output / "latest.json").read_text("utf-8"))
        manifest = json.loads((output / self.manifest.name).read_text("utf-8"))
        self.assertEqual(latest["version"], "2.3.4")
        self.assertEqual(latest["notes"], "本次更新说明")
        self.assertEqual(latest["package_url"], installer_object_url("2.3.4"))
        self.assertEqual(latest["sha256"], digest)
        self.assertEqual(manifest["installer"]["sha256"], digest)
        self.assertEqual(manifest["installer"]["size_bytes"], self.installer.stat().st_size)
        self.assertEqual((output / "SHA256.txt").read_text(), f"{digest}  {self.installer.name}\n")
        self.assertIsNone(manifest["source"])
        self.assertEqual(manifest["validation"]["build"], "not_recorded")
        with ZipFile(self.release / "HQYLAutomation_2.3.4.zip") as archive:
            self.assertEqual(set(archive.namelist()), expected)
            for name in expected:
                self.assertEqual(archive.read(name), (output / name).read_bytes())

    def test_version_mismatch_stops_before_creating_outputs(self):
        (self.root / "frontend/assets/common.js").write_text('version: "9.0.0"', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "versions differ"):
            package_release()
        self.assertEqual(list(self.release.iterdir()), [self.installer])

    def test_missing_installer_does_not_create_manifest(self):
        with self.assertRaises(FileNotFoundError):
            package_release("9.9.9")
        self.assertFalse((self.release / "upload_9.9.9").exists())

    def test_matching_original_build_evidence_is_preserved(self):
        package_release()
        manifest = json.loads(self.manifest.read_text("utf-8"))
        manifest["source"] = {"commit": "original-build-commit"}
        manifest["test_results"] = {"tests": 20, "failures": 0}
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        output = package_release()
        self.assertEqual(json.loads((output / self.manifest.name).read_text("utf-8")), manifest)

    def test_stale_manifest_rejected_and_previous_output_kept(self):
        output = package_release()
        original = (output / self.installer.name).read_bytes()
        self.installer.write_bytes(b"new installer with old build evidence")
        with self.assertRaisesRegex(ValueError, "does not match"):
            package_release()
        self.assertEqual((output / self.installer.name).read_bytes(), original)

    def test_fresh_build_replaces_old_evidence_and_retains_backups(self):
        output = package_release()
        old = self.installer.read_bytes()
        self.installer.write_bytes(b"newly built fixture installer")
        with patch("scripts.package_release._git", return_value=None):
            package_release(fresh_build=True)
        manifest = json.loads(self.manifest.read_text("utf-8"))
        self.assertIsNone(manifest["source"]["commit"])
        self.assertEqual(manifest["validation"]["application_tests"], "not_run_by_packager")
        self.assertEqual(manifest["installer"]["sha256"], hashlib.sha256(self.installer.read_bytes()).hexdigest().upper())
        backups = list(self.release.glob("upload_2.3.4.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / self.installer.name).read_bytes(), old)
        self.assertEqual((output / self.installer.name).read_bytes(), self.installer.read_bytes())
        self.assertEqual(len(list(self.release.glob(self.manifest.name + ".*.bak"))), 1)

    def test_parent_repository_is_not_reported_as_desktop_upstream(self):
        def git_result(root, *args):
            return {
                ("rev-parse", "--show-toplevel"): str(self.root.parent),
                ("remote", "get-url", "origin"): "https://token@example.test/private-parent.git",
                ("rev-parse", "HEAD"): "parent-commit",
                ("branch", "--show-current"): "main",
                ("status", "--porcelain", "--untracked-files=normal"): " M local.py",
            }.get(args)
        with patch("scripts.package_release._git", side_effect=git_result):
            manifest = _build_manifest(self.root, "2.3.4", {}, True)
        self.assertEqual(manifest["source"]["commit"], "parent-commit")
        self.assertEqual(manifest["source"]["workspace_kind"], "parent_or_untracked_workspace")
        self.assertIsNone(manifest["source"]["desktop_upstream"]["commit"])
        self.assertTrue(manifest["DIRTY"])
        self.assertNotIn("token", json.dumps(manifest))

    def test_missing_rar_stops_before_mutating_existing_outputs(self):
        output = package_release()
        with patch("scripts.package_release.find_rar", side_effect=FileNotFoundError("WinRAR missing")):
            with self.assertRaises(FileNotFoundError):
                package_release(rar_output_dir=self.root / "desktop")
        self.assertTrue(output.is_dir())
        self.assertFalse(list(self.release.glob("upload_2.3.4.*.bak")))

    def test_rar_failure_keeps_previous_archive(self):
        output = package_release()
        desktop = self.root / "desktop"
        desktop.mkdir()
        previous = desktop / "upload_2.3.4.rar"
        previous.write_bytes(b"previous archive")
        with patch("scripts.package_release.subprocess.run", side_effect=subprocess.CalledProcessError(1, ["rar"])):
            with self.assertRaises(subprocess.CalledProcessError):
                create_upload_rar(output, desktop, Path("rar.exe"))
        self.assertEqual(previous.read_bytes(), b"previous archive")
        self.assertEqual(list(desktop.iterdir()), [previous])

    def test_real_rar_round_trip_and_previous_archive_backup(self):
        try:
            rar = find_rar()
        except FileNotFoundError:
            self.skipTest("WinRAR is not installed")
        desktop = self.root / "desktop with spaces"
        desktop.mkdir()
        archive = desktop / "upload_2.3.4.rar"
        archive.write_bytes(b"previous archive")
        output = package_release(rar_output_dir=desktop)
        extracted = self.root / "extracted"
        extracted.mkdir()
        subprocess.run([str(rar), "x", "-cfg-", "-idq", "-y", str(archive), str(extracted) + "/"], check=True, timeout=30)
        self.assertEqual(len(list((extracted / output.name).iterdir())), 4)
        for source in output.iterdir():
            self.assertEqual((extracted / output.name / source.name).read_bytes(), source.read_bytes())
        backups = list(desktop.glob("upload_2.3.4.rar.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"previous archive")


if __name__ == "__main__":
    unittest.main()
