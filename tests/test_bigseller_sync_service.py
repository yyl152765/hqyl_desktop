from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.services import bigseller_sync


class BigSellerSyncServiceTests(unittest.TestCase):
    def test_process_config_uses_saved_captcha_credentials(self) -> None:
        job = bigseller_sync.BigSellerSyncJob(
            username="bigseller-user",
            password="bigseller-pass",
            sync_type="inventory",
            listing_status="live",
            output_dir=Path.cwd(),
            captcha_username="captcha-user",
            captcha_password="captcha-pass",
        )
        with patch.object(bigseller_sync, "load_reference_bigseller_config", return_value={}), patch.dict(
            bigseller_sync.os.environ,
            {},
            clear=True,
        ):
            config = bigseller_sync.build_process_config(job)

        self.assertEqual(config["captcha_service"]["username"], "captcha-user")
        self.assertEqual(config["captcha_service"]["password"], "captcha-pass")

    def test_loader_ignores_conflicting_top_level_util_package(self) -> None:
        conflicting_util = types.ModuleType("util")
        conflicting_util.__file__ = r"D:\hqyl_project\superbrowser_process\util\__init__.py"
        previous_util = sys.modules.get("util")
        previous_bigseller_util = sys.modules.pop(
            bigseller_sync.BIGSELLER_REQUEST_UTIL_MODULE_NAME,
            None,
        )
        sys.modules["util"] = conflicting_util

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir) / "mabang_process"
                util_dir = root / "util"
                util_dir.mkdir(parents=True)
                (util_dir / "bigseller_request_util.py").write_text(
                    "SOURCE = 'mabang_process'\n",
                    encoding="utf-8",
                )

                with patch.object(bigseller_sync, "mabang_process_root", return_value=root):
                    loaded = bigseller_sync.load_bigseller_request_util()
                    loaded_again = bigseller_sync.load_bigseller_request_util()

                self.assertEqual(loaded.SOURCE, "mabang_process")
                self.assertIs(loaded_again, loaded)
                self.assertIs(sys.modules["util"], conflicting_util)
        finally:
            sys.modules.pop(bigseller_sync.BIGSELLER_REQUEST_UTIL_MODULE_NAME, None)
            if previous_bigseller_util is not None:
                sys.modules[bigseller_sync.BIGSELLER_REQUEST_UTIL_MODULE_NAME] = previous_bigseller_util
            if previous_util is None:
                sys.modules.pop("util", None)
            else:
                sys.modules["util"] = previous_util

    def test_loader_reports_missing_component_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "mabang_process"
            root.mkdir()
            with patch.object(bigseller_sync, "mabang_process_root", return_value=root):
                with self.assertRaisesRegex(RuntimeError, "bigseller_request_util.py"):
                    bigseller_sync.load_bigseller_request_util()


if __name__ == "__main__":
    unittest.main()
