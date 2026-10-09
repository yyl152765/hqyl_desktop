from pathlib import Path
import ast
import importlib.util
import re
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.package_release import _release_output_path, package_release


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PackagingContractTests(unittest.TestCase):
    def test_application_and_installer_versions_match(self):
        bridge = (PROJECT_ROOT / "backend" / "app_bridge.py").read_text(encoding="utf-8")
        installer = (PROJECT_ROOT / "packaging" / "hqyl_automation.iss").read_text(encoding="utf-8")
        package_script = (PROJECT_ROOT / "scripts" / "package_release.py").read_text(encoding="utf-8")
        frontend = (PROJECT_ROOT / "frontend" / "assets" / "common.js").read_text(encoding="utf-8")

        app_version = re.search(r'^APP_VERSION = "([^"]+)"', bridge, re.MULTILINE)
        installer_version = re.search(r'^#define MyAppVersion "([^"]+)"', installer, re.MULTILINE)
        package_version = re.search(r'^def package_release\(version: str = "([^"]+)"\)', package_script, re.MULTILINE)
        package_cli_version = re.search(
            r'else "([^"]+)"\s*\n\s*package_release\(ver\)',
            package_script,
            re.MULTILINE,
        )

        self.assertIsNotNone(app_version)
        self.assertIsNotNone(installer_version)
        self.assertIsNotNone(package_version)
        self.assertIsNotNone(package_cli_version)
        self.assertEqual(app_version.group(1), installer_version.group(1))
        self.assertEqual(app_version.group(1), package_version.group(1))
        self.assertEqual(app_version.group(1), package_cli_version.group(1))
        self.assertIn(f'version: "{app_version.group(1)}"', frontend)

    def test_shopee_ads_runtime_is_bundled_by_pyinstaller(self):
        spec = (PROJECT_ROOT / "HQYLAutomation.spec").read_text(encoding="utf-8")

        self.assertIn('["desktop_entry.py"]', spec)
        self.assertNotIn('["launcher/main.py"]', spec)
        self.assertIn('project_root.parent / "superbrowser_process"', spec)
        self.assertIn('str(superbrowser_process_root)', spec)
        self.assertIn('HQYL_SUPERBROWSER_SOURCE', spec)
        self.assertIn('HQYL_MABANG_UTIL_SOURCE', spec)
        self.assertIn('(str(mabang_util_root), "mabang_process/util")', spec)
        self.assertIn('"main/shopee/config"', spec)
        self.assertIn('public_runtime_config', spec)
        self.assertNotIn('superbrowser_process_root / "config" / "config.yaml"', spec)
        self.assertNotIn('superbrowser_process_root / "main" / "shopee" / "config"', spec)
        self.assertNotIn('mabang_process_root / "vietnam" / "config" / "BigSeller库存同步.yaml"', spec)
        self.assertIn('project_root / "backend" / "resources" / "user_map.yaml"', spec)
        self.assertNotIn('superbrowser_process_root / "config" / "user_map.yaml"', spec)
        self.assertIn('collect_submodules("selenium")', spec)

        for site_code in ("id", "th", "ph", "vn", "my"):
            module = f"main.shopee.{site_code}_shopee_ads_recharge_operator_service"
            self.assertIn(f'"{module}"', spec)

    def test_shopee_ads_packaging_dependencies_are_declared(self):
        requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()

        for package in ("selenium", "psycopg2-binary", "alibabacloud-dingtalk"):
            self.assertIn(package, requirements)

    def test_arrival_xls_reader_is_declared_and_bundled(self):
        requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        spec = (PROJECT_ROOT / "HQYLAutomation.spec").read_text(encoding="utf-8")

        self.assertIn("xlrd", requirements)
        self.assertIn('collect_submodules("xlrd")', spec)

    def test_lazada_monthly_report_runtime_is_bundled(self):
        spec = (PROJECT_ROOT / "HQYLAutomation.spec").read_text(encoding="utf-8")

        self.assertIn('(\"frontend\", \"frontend\")', spec)
        self.assertIn('"main.lazada.lazada_balance_withdrawal_operator_service"', spec)
        self.assertIn('"implement.lazada.lazada_balance_withdrawal"', spec)
        self.assertIn('collect_submodules("selenium")', spec)
        self.assertIn('collect_all("playwright")', spec)
        self.assertIn("*playwright_hiddenimports", spec)

    def test_build_script_fixes_reproducibility_inputs(self):
        build_script = (PROJECT_ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")

        self.assertIn('$env:SOURCE_DATE_EPOCH = "946684800"', build_script)
        self.assertIn('$env:PYTHONHASHSEED = "0"', build_script)

    def test_release_packaging_rejects_invalid_versions(self):
        for version in (None, "", "../0.2.41", "0.2.41/../../outside", "0.2", "0.2.41-beta"):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    package_release(version)

    def test_release_output_paths_cannot_escape_release_directory(self):
        with TemporaryDirectory() as temp_dir:
            release_dir = Path(temp_dir) / "release"
            self.assertEqual(
                _release_output_path(release_dir, "upload_0.2.41"),
                release_dir.resolve() / "upload_0.2.41",
            )
            for name in ("../outside", "nested/upload_0.2.41", str(Path(temp_dir) / "outside")):
                with self.subTest(name=name):
                    with self.assertRaises(ValueError):
                        _release_output_path(release_dir, name)

    def test_release_staging_removes_credentials_and_preserves_runtime_injection(self):
        path = PROJECT_ROOT / "scripts/stage_superbrowser_source.py"
        spec = importlib.util.spec_from_file_location("release_superbrowser_staging", path)
        staging = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(staging)

        source = b'import psycopg2\n\ndef get_pg_connection():\n    return psycopg2.connect(host="database-host", user="runtime-user", password="fixture-old-password")\n'
        sanitized, keys = staging.sanitize_source("util/db_helper.py", source)
        self.assertEqual(keys, ["password"])
        self.assertNotIn(b"fixture-old-password", sanitized)
        captured = []
        driver = SimpleNamespace(connect=lambda **kwargs: captured.append(kwargs))
        with patch.dict(sys.modules, {"psycopg2": driver}):
            namespace = {}
            exec(compile(sanitized, "staged-db-helper", "exec"), namespace)
            with patch.dict("os.environ", {"PGPASSWORD": "fixture-runtime-password"}, clear=True):
                namespace["get_pg_connection"]()
            with patch.dict("os.environ", {}, clear=True):
                namespace["get_pg_connection"]()
        self.assertEqual(captured[0], {"host": "database-host", "user": "runtime-user", "password": "fixture-runtime-password"})
        self.assertEqual(captured[1]["password"], "")

        source = b'APP_KEY = "fixture-key"\nAPP_SECRET = "fixture-secret"\nUSER_ID = "fixture-user"\ndef get_access_token(app_key, app_secret):\n    return app_key, app_secret\n'
        sanitized, keys = staging.sanitize_source("util/dingtalk_doc_api.py", source)
        namespace = {}
        exec(compile(sanitized, "staged-dingtalk", "exec"), namespace)
        self.assertEqual([namespace[key] for key in keys], ["", "", ""])
        self.assertEqual(namespace["get_access_token"]("runtime-key", "runtime-secret"), ("runtime-key", "runtime-secret"))

        source = b'def login(username, auth_code):\n    return username, auth_code\nif __name__ == "__main__":\n    username = "fixture-mail"\n    auth_code = "fixture-code"\n    get_email_name = "fixture-target"\n    get_email_type = "runtime-mode"\n'
        sanitized, keys = staging.sanitize_source("util/email_util.py", source)
        tree = ast.parse(sanitized)
        guard_values = {node.targets[0].id: node.value.value for node in tree.body[1].body if isinstance(node, ast.Assign)}
        self.assertEqual([guard_values[key] for key in keys], ["", "", ""])
        self.assertEqual(guard_values["get_email_type"], "runtime-mode")
        namespace = {"__name__": "staged_email"}
        exec(compile(sanitized, "staged-email", "exec"), namespace)
        self.assertEqual(namespace["login"]("runtime-mail", "runtime-code"), ("runtime-mail", "runtime-code"))

    def test_release_staging_refuses_unreviewed_credential_layout(self):
        path = PROJECT_ROOT / "scripts/stage_superbrowser_source.py"
        spec = importlib.util.spec_from_file_location("release_superbrowser_staging", path)
        staging = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(staging)
        with self.assertRaisesRegex(RuntimeError, "locations changed"):
            staging.sanitize_source("util/dingtalk_doc_api.py", b'APP_KEY = "fixture"\n')



if __name__ == "__main__":
    unittest.main()
