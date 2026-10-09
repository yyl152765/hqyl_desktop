import sys
import unittest
from pathlib import Path


SUPERBROWSER_ROOT = Path(__file__).resolve().parents[2] / "superbrowser_process"
if str(SUPERBROWSER_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERBROWSER_ROOT))

from implement.shopee import vn_shopee_ads_recharge as vn_recharge
from main.shopee import vn_shopee_ads_recharge_operator_service as vn_service


class _FakeTimeouts:
    def __init__(self, implicit_wait=12):
        self.implicit_wait = implicit_wait


class _FakeElement:
    def __init__(self, displayed=True):
        self.displayed = displayed
        self.click_count = 0

    def is_displayed(self):
        return self.displayed

    def click(self):
        self.click_count += 1


class _FakeDriver:
    def __init__(self, elements=None, promo_result=None):
        self.timeouts = _FakeTimeouts()
        self.elements = list(elements or [])
        self.promo_result = promo_result or {"clicked": False, "overlayVisible": False}
        self.wait_calls = []

    def implicitly_wait(self, seconds):
        self.wait_calls.append(seconds)
        self.timeouts.implicit_wait = seconds

    def find_elements(self, _by, _value):
        return list(self.elements)

    def execute_script(self, script, *args):
        if script == vn_recharge._DISMISS_VISIBLE_PROMO_SCRIPT:
            return dict(self.promo_result)
        if args:
            args[0].click()
        return None


class _FakeWebdriverUtil:
    def __init__(self, driver):
        self.driver = driver


class VietnamShopeeAdsRechargeRecoveryTests(unittest.TestCase):
    def test_close_alert_clicks_only_visible_button_and_restores_wait(self):
        hidden = _FakeElement(displayed=False)
        visible = _FakeElement(displayed=True)
        driver = _FakeDriver([hidden, visible])

        closed = vn_recharge.close_alert(_FakeWebdriverUtil(driver))

        self.assertTrue(closed)
        self.assertEqual(hidden.click_count, 0)
        self.assertEqual(visible.click_count, 1)
        self.assertEqual(driver.wait_calls, [0, 12])

    def test_close_alert_uses_unified_popup_fallback(self):
        driver = _FakeDriver(promo_result={"clicked": True, "reason": "promo-top-right", "overlayVisible": True})

        closed = vn_recharge.close_alert(_FakeWebdriverUtil(driver))

        self.assertTrue(closed)
        self.assertEqual(driver.wait_calls, [0, 12])

    def test_browser_window_disconnect_is_recoverable(self):
        self.assertTrue(
            vn_service._is_browser_session_error(
                "no such window: target window already closed; web view not found"
            )
        )
        self.assertFalse(vn_service._is_browser_session_error("Ads Credit element not found"))

    def test_all_failed_run_is_not_reported_as_success(self):
        records = [
            {"store_name": f"shop-{index}", "status": "failed", "message": "window closed"}
            for index in range(38)
        ]

        success, run_status, message = vn_service._summarize_run_outcome(records, 38)

        self.assertFalse(success)
        self.assertEqual(run_status, "failed")
        self.assertIn("失败 38", message)

    def test_non_failure_statuses_keep_successful_run_state(self):
        records = [
            {"status": "success"},
            {"status": "processing"},
            {"status": "skipped"},
        ]

        success, run_status, message = vn_service._summarize_run_outcome(records, 3)

        self.assertTrue(success)
        self.assertEqual(run_status, "success")
        self.assertIn("失败 0", message)


if __name__ == "__main__":
    unittest.main()
