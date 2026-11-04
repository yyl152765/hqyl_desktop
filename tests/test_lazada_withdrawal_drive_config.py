from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from backend.services import lazada_withdrawal_statistics as service


def task(root: Path, **kwargs):
    return service.LazadaWithdrawalStatisticsQuery(
        company="fixture", username="fixture-user", password="private-browser-password",
        country="MY", start_date="2026-09-01", end_date="2026-09-30",
        store_names=("店铺一", "店铺二"), output_root=str(root), **kwargs,
    )


class DriveConfigPreflightTests(unittest.TestCase):
    def setUp(self):
        self.legacy = ModuleType("util.dingtalk_doc_api")
        self.legacy.APP_KEY = ""
        self.legacy.APP_SECRET = ""
        self.legacy.USER_ID = ""
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}, clear=True).start()
        patch.object(service, "_ensure_superbrowser_path").start()
        patch.dict("sys.modules", {"util.dingtalk_doc_api": self.legacy}).start()

    def test_saved_task_credentials_work_with_sanitized_packaged_defaults(self):
        job = task(Path("fixture"), dingtalk_app_key="saved-key",
                   dingtalk_app_secret="saved-secret", dingtalk_user_id="saved-user")
        self.assertEqual(service._resolve_drive_credentials(job), ("saved-key", "saved-secret", "saved-user"))
        for value in (job.password, job.dingtalk_app_key, job.dingtalk_app_secret, job.dingtalk_user_id):
            self.assertNotIn(value, repr(job))

    def test_env_overrides_remain_supported_for_cli(self):
        with patch.dict(os.environ, {
            "LAZADA_WITHDRAWAL_DINGTALK_APP_KEY": "environment-key",
            "LAZADA_WITHDRAWAL_DINGTALK_APP_SECRET": "environment-secret",
            "LAZADA_WITHDRAWAL_DINGTALK_USER_ID": "environment-user",
        }):
            self.assertEqual(service._resolve_drive_credentials(task(Path("fixture"))),
                             ("environment-key", "environment-secret", "environment-user"))

    def test_empty_packaged_defaults_stop_before_any_browser_or_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            progress = []
            with (
                patch.object(service, "_reference_modules") as references,
                patch.object(service, "_make_logger", return_value=(Mock(), root / "run.log")),
            ):
                result = service.run_lazada_withdrawal_statistics(task(root), progress.append)
            references.assert_not_called()
            self.assertFalse(result["success"])
            self.assertEqual(result["processed_store_count"], 1)
            self.assertEqual(result["matched_store_count"], 0)
            self.assertIn("请先在设置", result["stores"][0]["message"])
            self.assertIn("AppSecret", result["stores"][0]["message"])
            self.assertEqual(list(root.rglob("*.xlsx")), [])
            self.assertEqual(json.loads(Path(result["output_file"]).read_text(encoding="utf-8")), result)
            self.assertNotIn("private-browser-password", json.dumps(result))

    def test_partial_credentials_error_names_only_missing_fields(self):
        job = task(Path("fixture"), dingtalk_app_key="private-key", dingtalk_app_secret="private-secret")
        with self.assertRaises(ValueError) as caught:
            service._resolve_drive_credentials(job)
        self.assertIn("钉钉操作人", str(caught.exception))
        self.assertNotIn("private-", str(caught.exception))
        self.assertNotIn("AppKey", str(caught.exception))

    def test_resolved_credentials_are_the_real_drive_constructor_arguments(self):
        drive_module = ModuleType("util.dingtalk_drive_util")
        drive_module.DingDriveClient = Mock()
        job = task(Path("fixture"), dingtalk_app_key="configured-key",
                   dingtalk_app_secret="configured-secret", dingtalk_user_id="configured-user")
        with patch.dict("sys.modules", {"util.dingtalk_drive_util": drive_module}):
            client = service._create_drive_client(job)
        drive_module.DingDriveClient.assert_called_once_with(
            "configured-key", "configured-secret", "configured-user", service.DRIVE_SPACE_ID,
        )
        self.assertIs(client, drive_module.DingDriveClient.return_value)


if __name__ == "__main__":
    unittest.main()
