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


class DingTalkRetryTests(unittest.TestCase):
    SERVICE_UNAVAILABLE = (
        "Error: ServiceUnavailable code: 503, The request has failed due to a "
        "temporary failure of the server. request id: 01A1240A-002C-7F17"
    )

    def test_classifies_temporary_and_business_errors(self):
        self.assertTrue(dw.is_retryable_dingtalk_error(RuntimeError(self.SERVICE_UNAVAILABLE)))
        self.assertTrue(dw.is_retryable_dingtalk_error(RuntimeError("Throttling.User")))
        self.assertTrue(dw.is_retryable_dingtalk_error(RuntimeError("获取钉钉应用令牌失败（HTTP 503）")))
        self.assertTrue(dw.is_retryable_dingtalk_error(RuntimeError("HTTPSConnectionPool Read timed out")))
        self.assertFalse(dw.is_retryable_dingtalk_error(RuntimeError("invalidRequest.resource.notFound")))
        self.assertFalse(dw.is_retryable_dingtalk_error(RuntimeError("无权限访问该文档")))
        self.assertFalse(dw.is_retryable_dingtalk_error(RuntimeError("请先绑定并选择紫鸟账号")))

    def test_retries_temporary_error_until_success(self):
        calls: list[int] = []
        delays: list[float] = []

        def operation():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError(self.SERVICE_UNAVAILABLE)
            return "ok"

        result = dw.call_with_retry(operation, sleeper=delays.append)

        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, [1.0, 2.0])

    def test_business_error_is_not_retried(self):
        calls: list[int] = []

        def operation():
            calls.append(1)
            raise RuntimeError("invalidRequest.resource.notFound")

        with self.assertRaises(RuntimeError):
            dw.call_with_retry(operation, sleeper=lambda _delay: None)

        self.assertEqual(len(calls), 1)

    def test_gives_up_after_max_attempts(self):
        calls: list[int] = []

        def operation():
            calls.append(1)
            raise RuntimeError(self.SERVICE_UNAVAILABLE)

        with self.assertRaises(RuntimeError):
            dw.call_with_retry(operation, sleeper=lambda _delay: None)

        self.assertEqual(len(calls), dw.DINGTALK_MAX_ATTEMPTS)
        self.assertEqual(dw.DINGTALK_MAX_ATTEMPTS, 5)

    def test_read_sheet_range_retries_service_unavailable(self):
        message = self.SERVICE_UNAVAILABLE

        class Response:
            class body:
                values = [["广告费", "业绩"]]

        calls: list[tuple] = []

        class Client:
            def get_range_with_options(self, *args):
                calls.append(args)
                if len(calls) < 3:
                    raise RuntimeError(message)
                return Response

        with patch.object(dw, "_get_sdk_client", return_value=Client()), patch.object(
            dw, "_runtime_options", return_value=None
        ), patch.object(dw, "DINGTALK_RETRY_DELAYS", (0.0, 0.0, 0.0, 0.0)):
            values = dw.read_sheet_range("token", "union", "doc", "sheet", "AK1:AL3")

        self.assertEqual(values, [["广告费", "业绩"]])
        self.assertEqual(len(calls), 3)

    def test_update_sheet_range_retries_then_writes(self):
        message = self.SERVICE_UNAVAILABLE
        calls: list[tuple] = []

        class Client:
            def update_range_with_options(self, *args):
                calls.append(args)
                if len(calls) < 2:
                    raise RuntimeError(message)

        with patch.object(dw, "_get_sdk_client", return_value=Client()), patch.object(
            dw, "_runtime_options", return_value=None
        ), patch.object(dw, "DINGTALK_RETRY_DELAYS", (0.0, 0.0, 0.0, 0.0)):
            dw.update_sheet_range("token", "union", "doc", "sheet", "AH3:AH3", [["0"]])

        self.assertEqual(len(calls), 2)

    def test_read_sheet_range_does_not_retry_missing_resource(self):
        calls: list[tuple] = []

        class Client:
            def get_range_with_options(self, *args):
                calls.append(args)
                raise RuntimeError("invalidRequest.resource.notFound")

        with patch.object(dw, "_get_sdk_client", return_value=Client()), patch.object(
            dw, "_runtime_options", return_value=None
        ), patch.object(dw, "DINGTALK_RETRY_DELAYS", (0.0, 0.0, 0.0, 0.0)):
            with self.assertRaises(RuntimeError):
                dw.read_sheet_range("token", "union", "doc", "sheet", "AK1:AL3")

        self.assertEqual(len(calls), 1)


    def test_retry_observer_receives_retry_notices(self):
        notices: list[str] = []

        def operation():
            raise RuntimeError(self.SERVICE_UNAVAILABLE)

        try:
            dw.set_retry_observer(notices.append)
            with self.assertRaises(RuntimeError):
                dw.call_with_retry(operation, attempts=3, sleeper=lambda _delay: None)
        finally:
            dw.set_retry_observer(None)

        self.assertEqual(len(notices), 2)
        self.assertIn("2/3", notices[-1])

    def test_observer_failure_never_breaks_retry(self):
        def bad_observer(_message):
            raise RuntimeError("observer boom")

        calls: list[int] = []

        def operation():
            calls.append(1)
            if len(calls) < 2:
                raise RuntimeError(self.SERVICE_UNAVAILABLE)
            return "ok"

        try:
            dw.set_retry_observer(bad_observer)
            result = dw.call_with_retry(operation, sleeper=lambda _delay: None)
        finally:
            dw.set_retry_observer(None)

        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 2)

    def test_no_observer_means_no_notification(self):
        dw.set_retry_observer(None)

        def operation():
            raise RuntimeError(self.SERVICE_UNAVAILABLE)

        with self.assertRaises(RuntimeError):
            dw.call_with_retry(operation, attempts=2, sleeper=lambda _delay: None)


if __name__ == "__main__":
    unittest.main()
