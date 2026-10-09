from __future__ import annotations

import time
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from backend.services.shopee_verification import (
    ShopeePasswordVerificationRequired,
    complete_shopee_password_verification,
)
from backend.services.vietnam_income_subsidy import _browser_fetch


class _FakePasswordLocator:
    def __init__(self, page: "_FakePage") -> None:
        self.page = page

    def count(self) -> int:
        return int(self.page.password_visible)

    @property
    def last(self) -> "_FakePasswordLocator":
        return self

    def press(self, _key: str, **_kwargs) -> None:
        self.page.enter_presses += 1


class _FakeButtonLocator:
    def __init__(self, page: "_FakePage") -> None:
        self.page = page

    def count(self) -> int:
        return int(self.page.password_visible)

    @property
    def first(self) -> "_FakeButtonLocator":
        return self

    def click(self, **_kwargs) -> None:
        self.page.verify_clicks += 1
        if (
            self.page.remove_after_clicks is not None
            and self.page.verify_clicks >= self.page.remove_after_clicks
        ):
            self.page.password_visible = False


class _FakePage:
    def __init__(
        self,
        *,
        appear_after_waits: int = 0,
        remove_after_clicks: int | None = 1,
    ) -> None:
        self.appear_after_waits = appear_after_waits
        self.remove_after_clicks = remove_after_clicks
        self.wait_count = 0
        self.verify_clicks = 0
        self.enter_presses = 0
        self.password_visible = appear_after_waits == 0

    def locator(self, _selector: str) -> _FakePasswordLocator:
        return _FakePasswordLocator(self)

    def get_by_role(self, _role: str, **_kwargs) -> _FakeButtonLocator:
        return _FakeButtonLocator(self)

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.wait_count += 1
        if self.wait_count >= self.appear_after_waits:
            self.password_visible = True
        time.sleep(max(timeout_ms, 0) / 1000)

    def wait_for_function(self, _script: str, **_kwargs) -> None:
        if self.password_visible:
            raise PlaywrightTimeoutError("password dialog is still visible")


class ShopeeVerificationTests(unittest.TestCase):
    def test_clicks_immediately_visible_verify_dialog(self) -> None:
        page = _FakePage()

        handled = complete_shopee_password_verification(
            page,
            appear_timeout_ms=0,
        )

        self.assertTrue(handled)
        self.assertEqual(page.verify_clicks, 1)
        self.assertFalse(page.password_visible)

    def test_waits_for_delayed_verify_dialog(self) -> None:
        page = _FakePage(appear_after_waits=2)

        handled = complete_shopee_password_verification(
            page,
            appear_timeout_ms=100,
            appear_poll_interval_ms=10,
        )

        self.assertTrue(handled)
        self.assertEqual(page.verify_clicks, 1)

    def test_retries_when_shopee_ignores_first_click(self) -> None:
        page = _FakePage(remove_after_clicks=2)

        handled = complete_shopee_password_verification(
            page,
            max_attempts=3,
            wait_after_click_ms=0,
            appear_timeout_ms=0,
        )

        self.assertTrue(handled)
        self.assertEqual(page.verify_clicks, 2)

    def test_raises_when_verify_dialog_never_closes(self) -> None:
        page = _FakePage(remove_after_clicks=None)

        with self.assertRaises(ShopeePasswordVerificationRequired):
            complete_shopee_password_verification(
                page,
                max_attempts=2,
                wait_after_click_ms=0,
                appear_timeout_ms=0,
            )

        self.assertEqual(page.verify_clicks, 2)

    def test_income_fetch_checks_for_late_dialog_before_request(self) -> None:
        page = Mock()
        page.evaluate.return_value = {"status": 200, "body": {"code": 0}}

        with patch(
            "backend.services.vietnam_income_subsidy._complete_income_verification"
        ) as verify:
            result = _browser_fetch(page, "/income", {"page": 2})

        self.assertEqual(result, {"code": 0})
        verify.assert_called_once_with(page, progress=None, appear_timeout_ms=0)


if __name__ == "__main__":
    unittest.main()
