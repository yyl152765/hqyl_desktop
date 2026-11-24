"""Private build inputs must be explicit and stay outside published source."""
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.builtin_ads_database import release_connection_data


class BuiltinDatabasePackagingTests(unittest.TestCase):
    def test_requires_an_explicit_external_untracked_resource(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source_export = root / "export"
            source_export.mkdir()
            private = root / "private"
            private.mkdir()
            resource = private / "ads_database.json"
            resource.write_text(json.dumps({"host": "fixture.invalid", "dbname": "hrms", "user": "fixture",
                                            "password": "test-value", "sslmode": "prefer"}), encoding="utf-8")
            self.assertEqual(release_connection_data("", source_export), [])
            self.assertEqual(release_connection_data(str(resource), source_export), [(str(resource), "backend/resources")])
            with self.assertRaisesRegex(ValueError, "absolute"):
                release_connection_data("ads_database.json", source_export)
            with self.assertRaisesRegex(ValueError, "outside"):
                release_connection_data(str(resource), private)
            git_environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")}
            with patch.dict(os.environ, git_environment, clear=True):
                subprocess.run(["git", "init", "--quiet", str(private)], check=True)
                subprocess.run(["git", "-C", str(private), "add", "--", resource.name], check=True)
                with self.assertRaisesRegex(ValueError, "tracked"):
                    release_connection_data(str(resource), source_export)

    def test_invalid_resource_stops_packaging(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            resource = root / "ads_database.json"
            resource.write_text('{"password": "fixture-only"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "内置.*无效") as error:
                release_connection_data(str(resource), root / "export")
            self.assertNotIn("fixture-only", str(error.exception))
