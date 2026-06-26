"""dingtalk_workbook 配置读取测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.core import dingtalk_workbook as dw


class DingTalkCredentialTests(unittest.TestCase):
    def test_config_candidates_do_not_read_legacy_project(self):
        candidates = [str(path).lower() for path in dw._config_file_candidates()]
        self.assertFalse(any("mabang_process" in path for path in candidates))

    def test_loads_app_rpa_config(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.yaml"
            p.write_text(
                """
dingtalk:
  app:
    rpa:
      app_key: key-from-rpa
      app_secret: secret-from-rpa
      user_id: user-from-rpa
""",
                encoding="utf-8",
            )

            with patch.object(dw, "_config_file_candidates", return_value=[p]):
                creds = dw.load_dingtalk_credentials()

        self.assertEqual(creds["app_key"], "key-from-rpa")
        self.assertEqual(creds["app_secret"], "secret-from-rpa")
        self.assertEqual(creds["user_id"], "user-from-rpa")

    def test_loads_legacy_doc_config(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.yaml"
            p.write_text(
                """
dingtalk:
  doc_config:
    app_key: key-from-doc
    app_secret: secret-from-doc
    app_userid: user-from-doc
""",
                encoding="utf-8",
            )

            with patch.object(dw, "_config_file_candidates", return_value=[p]):
                creds = dw.load_dingtalk_credentials()

        self.assertEqual(creds["app_key"], "key-from-doc")
        self.assertEqual(creds["app_secret"], "secret-from-doc")
        self.assertEqual(creds["user_id"], "user-from-doc")

    def test_missing_credentials_raises_typed_error(self):
        with patch.object(dw, "_config_file_candidates", return_value=[]), patch.dict(
            os.environ,
            {
                "DINGTALK_APP_KEY": "",
                "DINGTALK_APP_SECRET": "",
                "DINGTALK_USER_ID": "",
            },
        ):
            with self.assertRaises(dw.DingTalkCredentialsError):
                dw.get_credentials()


if __name__ == "__main__":
    unittest.main()
