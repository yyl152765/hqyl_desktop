"""Offline login diagnostics: actionable reasons without remote payload leaks."""
from __future__ import annotations

import traceback
import unittest
from typing import Any
from urllib.parse import parse_qs
from unittest.mock import Mock

import httpx

from backend.core import mabang_client
from backend.services import lz_warehouse_inventory as inventory


USERNAME = "fixture-login-user-sensitive"
PASSWORD = "fixture-login-password-sensitive&+%=中文"
TOKEN = "fixture-authentication-token-sensitive"
AUTH_URL = f"https://fixture-login.private.mabangerp.com/auth?token={TOKEN}"
BASE_URL = "https://fixture-login.private.mabangerp.com"
SENSITIVE_VALUES = (USERNAME, PASSWORD, TOKEN, AUTH_URL, "https://")
REAL_INCORRECT_CREDENTIALS_MESSAGE = (
    "登录失败，账号或密码不正确。（若连续输错19次后，账号将被锁定1小时）"
)


class LoginTransportFixture:
    def __init__(self, *, payload: Any = None, body: str | None = None,
                 failure: str = "", failure_stage: str = "login") -> None:
        self.payload = {"success": False, "message": "fixture refusal"} if payload is None else payload
        self.body = body
        self.failure = failure
        self.failure_stage = failure_stage
        self.stages: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        mod = request.url.params.get("mod", "")
        stage = "login_page" if request.url.path == "/index.htm" else "finalize" if mod == "main.plogin" else "login"
        self.stages.append(stage)
        if stage == self.failure_stage:
            if self.failure == "timeout":
                raise httpx.ReadTimeout(f"private timeout: {AUTH_URL} {PASSWORD}", request=request)
            if self.failure == "network":
                raise httpx.ConnectError(f"private connection error: {AUTH_URL} {USERNAME}", request=request)
            if self.failure == "http":
                return httpx.Response(503, text=f"private HTTP response {PASSWORD} {AUTH_URL}")
        if stage == "login_page":
            return httpx.Response(200, text="<html>Fixture login page</html>")
        if stage == "finalize":
            return httpx.Response(200, text="<html>Fixture authenticated page</html>")
        if mod != "main.doLogin" or request.method != "POST":
            raise AssertionError("Unexpected offline login request")
        form = parse_qs(request.content.decode(), keep_blank_values=True)
        if form.get("username") != [USERNAME] or form.get("password") != [PASSWORD]:
            raise AssertionError("Bound fixture credentials must be posted unchanged")
        if self.body is not None:
            return httpx.Response(200, text=self.body)
        return httpx.Response(200, json=self.payload)


class MabangLoginDiagnosticsTests(unittest.TestCase):
    def capture_failure(self, fixture: LoginTransportFixture, *, through_inventory: bool = False):
        with httpx.Client(transport=httpx.MockTransport(fixture.handle), follow_redirects=True) as http:
            client = mabang_client.MabangClient(BASE_URL, client=http)
            try:
                if through_inventory:
                    inventory._login(client, USERNAME, PASSWORD)
                else:
                    client.login(USERNAME, PASSWORD)
            except Exception as exc:
                self.assertFalse(client._logged_in)
                return exc, traceback.format_exc()
        self.fail("The refused or malformed login must not succeed")

    def assert_safe_error(self, error: Exception, displayed_traceback: str) -> None:
        for value in SENSITIVE_VALUES:
            self.assertNotIn(value, str(error))
            self.assertNotIn(value, displayed_traceback)

    def assert_reason(self, fixture: LoginTransportFixture, reason: str):
        error, displayed_traceback = self.capture_failure(fixture)
        error_type = getattr(mabang_client, "MabangLoginError", None)
        self.assertIsNotNone(error_type, "Login rejections need a typed, sanitized reason")
        self.assertIsInstance(error, error_type)
        self.assertIsInstance(error, mabang_client.MabangApiError)
        self.assertEqual(error.reason, reason)
        self.assert_safe_error(error, displayed_traceback)
        self.assertNotIn("finalize", fixture.stages)
        return error

    def test_incorrect_credentials_with_future_lockout_warning_is_not_already_locked(self):
        fixture = LoginTransportFixture(payload={
            "success": False,
            "message": REAL_INCORRECT_CREDENTIALS_MESSAGE,
            "private_username": USERNAME, "private_password": PASSWORD, "redirect": AUTH_URL,
        })
        error = self.assert_reason(fixture, "incorrect_credentials")
        self.assertIn("账号或密码", str(error))
        self.assertNotIn("账号已锁定", str(error))

    def test_canonical_login_rejections_have_distinct_sanitized_reasons(self):
        cases = (
            ("验证码错误，请重新输入验证码", "verification_required"),
            ("账号已被锁定，请1小时后重试", "account_locked"),
            ("暂时无法完成登录，请稍后再试", "unknown"),
        )
        messages = []
        for raw_message, reason in cases:
            with self.subTest(reason=reason):
                error = self.assert_reason(LoginTransportFixture(payload={
                    "success": False,
                    "message": f"{raw_message} {USERNAME} {PASSWORD} {AUTH_URL}",
                }), reason)
                messages.append(str(error))
        self.assertEqual(len(set(messages)), len(cases))

    def test_msg_and_error_string_fields_can_classify_without_forwarding_them(self):
        for field, message, reason in (
            ("msg", "账号或密码不正确", "incorrect_credentials"),
            ("error", "请输入验证码", "verification_required"),
        ):
            with self.subTest(field=field):
                self.assert_reason(LoginTransportFixture(payload={
                    "success": False, field: f"{message} {PASSWORD} {AUTH_URL}",
                }), reason)

    def test_non_json_and_non_object_responses_have_safe_invalid_response_reason(self):
        fixtures = (
            LoginTransportFixture(body=f"<html>{USERNAME} {PASSWORD} {AUTH_URL}</html>"),
            LoginTransportFixture(payload=[USERNAME, PASSWORD, AUTH_URL]),
            LoginTransportFixture(payload=f"{PASSWORD} {AUTH_URL}"),
            LoginTransportFixture(body="null"),
        )
        for fixture in fixtures:
            with self.subTest(response_type="html" if fixture.body else type(fixture.payload).__name__):
                self.assert_reason(fixture, "invalid_response")

    def test_missing_success_and_nested_error_fields_are_unknown_without_raw_payload(self):
        for payload in (
            {"private": f"{USERNAME} {PASSWORD} {AUTH_URL}"},
            {"success": False, "message": {"private": f"{USERNAME} {PASSWORD} {AUTH_URL}"}},
        ):
            with self.subTest(nested=isinstance(payload.get("message"), dict)):
                self.assert_reason(LoginTransportFixture(payload=payload), "unknown")

    def test_successful_login_still_sends_exact_credentials_and_finalizes_once(self):
        fixture = LoginTransportFixture(payload={"success": True})
        with httpx.Client(transport=httpx.MockTransport(fixture.handle)) as http:
            client = mabang_client.MabangClient(BASE_URL, client=http)
            client.login(USERNAME, PASSWORD)
            self.assertTrue(client._logged_in)
        self.assertEqual(fixture.stages, ["login_page", "login", "finalize"])

    def test_inventory_preserves_the_typed_login_errors_safe_message(self):
        for raw_message in (
            REAL_INCORRECT_CREDENTIALS_MESSAGE,
            "请输入验证码",
            "账号已被锁定，请稍后重试",
            "暂时无法完成登录",
        ):
            with self.subTest(message_category=raw_message[:6]):
                payload = {"success": False, "message": f"{raw_message} {PASSWORD} {AUTH_URL}"}
                core_error, _ = self.capture_failure(LoginTransportFixture(payload=payload))
                error, displayed_traceback = self.capture_failure(LoginTransportFixture(payload=payload), through_inventory=True)
                self.assertIsInstance(error, mabang_client.MabangApiError)
                self.assertEqual(str(error), str(core_error))
                self.assert_safe_error(error, displayed_traceback)

    def test_inventory_distinguishes_timeout_network_and_http_failures_at_every_login_stage(self):
        messages = {}
        for failure, hint in (("timeout", "超时"), ("network", "网络"), ("http", "HTTP")):
            for stage in ("login_page", "login", "finalize"):
                with self.subTest(failure=failure, stage=stage):
                    fixture = LoginTransportFixture(payload={"success": True}, failure=failure, failure_stage=stage)
                    error, displayed_traceback = self.capture_failure(fixture, through_inventory=True)
                    self.assertIsInstance(error, mabang_client.MabangApiError)
                    self.assertIn(hint, str(error))
                    self.assert_safe_error(error, displayed_traceback)
                    messages[failure] = str(error)
        self.assertEqual(len(set(messages.values())), 3)

    def test_inventory_keeps_untyped_api_errors_generic_and_suppresses_raw_exception_context(self):
        client = Mock()
        client.login.side_effect = mabang_client.MabangApiError(f"untrusted API payload {USERNAME} {PASSWORD} {AUTH_URL}")
        try:
            inventory._login(client, USERNAME, PASSWORD)
        except mabang_client.MabangApiError as error:
            self.assert_safe_error(error, traceback.format_exc())
        else:
            self.fail("An untyped upstream error must stop login")


if __name__ == "__main__":
    unittest.main()
