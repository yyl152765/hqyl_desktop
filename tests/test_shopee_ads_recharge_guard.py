from decimal import Decimal
from pathlib import Path
import importlib
import sys
import types
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPERBROWSER_ROOT = PROJECT_ROOT.parent / "superbrowser_process"
_INSERTED_SUPERBROWSER_PATH = False
if str(SUPERBROWSER_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERBROWSER_ROOT))
    _INSERTED_SUPERBROWSER_PATH = True


_INSERTED_STUB_MODULES = []


def _install_external_stubs():
    psycopg2_stub = types.ModuleType("psycopg2")
    class OperationalError(Exception):
        pass

    psycopg2_stub.OperationalError = OperationalError
    psycopg2_stub.connect = lambda *args, **kwargs: None
    psycopg2_extras_stub = types.ModuleType("psycopg2.extras")
    psycopg2_extras_stub.RealDictCursor = object
    dingtalk_stub = types.ModuleType("util.dingtalk_message_util")
    dingtalk_stub.send_text_msg = lambda *args, **kwargs: None

    for name, module in (
        ("psycopg2", psycopg2_stub),
        ("psycopg2.extras", psycopg2_extras_stub),
        ("util.dingtalk_message_util", dingtalk_stub),
    ):
        if name not in sys.modules:
            sys.modules[name] = module
            _INSERTED_STUB_MODULES.append(name)


_install_external_stubs()
ads_recharge_guard = importlib.import_module("main.shopee.ads_recharge_guard")
vn_ads_recharge = importlib.import_module("implement.shopee.vn_shopee_ads_recharge")
for _stub_name in _INSERTED_STUB_MODULES:
    sys.modules.pop(_stub_name, None)
if _INSERTED_SUPERBROWSER_PATH:
    sys.path.remove(str(SUPERBROWSER_ROOT))


class ShopeeAdsRechargeGuardTests(unittest.TestCase):
    def test_guard_connection_falls_back_to_ip_only_for_dns_failure(self):
        calls = []
        connection = object()

        def connect(**config):
            calls.append(config)
            if len(calls) == 1:
                raise ads_recharge_guard.psycopg2.OperationalError(
                    'could not translate host name "rpa.whhqyl.com.cn" to address: Name or service not known'
                )
            return connection

        with patch.object(ads_recharge_guard.psycopg2, "connect", side_effect=connect):
            result = ads_recharge_guard.get_guard_connection()

        self.assertIs(result, connection)
        self.assertEqual(calls[0]["host"], "rpa.whhqyl.com.cn")
        self.assertEqual(calls[1]["host"], "47.112.20.68")
        self.assertEqual(calls[1]["connect_timeout"], 8)

    def test_guard_connection_does_not_bypass_non_dns_database_errors(self):
        with patch.object(
            ads_recharge_guard.psycopg2,
            "connect",
            side_effect=ads_recharge_guard.psycopg2.OperationalError("password authentication failed"),
        ) as connect:
            with self.assertRaisesRegex(ads_recharge_guard.psycopg2.OperationalError, "authentication"):
                ads_recharge_guard.get_guard_connection()

        connect.assert_called_once()

    def test_page_success_becomes_failed_when_balance_not_confirmed(self):
        with patch.object(ads_recharge_guard, "query_ads_credit_with_reopened_store", return_value=Decimal("100")):
            result = ads_recharge_guard.verify_recharge_status_with_balance_check(
                status="success",
                message="payment popup success",
                recharge_amount=Decimal("50"),
                balance_before=Decimal("100"),
                balance_after=None,
                site_code="my",
                store_name="shop-a",
                recharge_module=object(),
                driver=object(),
                default_ads_wallet_url="https://example.test",
                balance_check_attempts=1,
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["balance_after"], Decimal("100"))
        self.assertTrue(result["retry_balance_check"])
        self.assertFalse(result["corrected_to_success"])

    def test_failed_result_is_corrected_when_balance_confirms(self):
        with patch.object(ads_recharge_guard, "query_ads_credit_with_reopened_store", return_value=Decimal("150")):
            result = ads_recharge_guard.verify_recharge_status_with_balance_check(
                status="failed",
                message="payment popup failed",
                recharge_amount=Decimal("50"),
                balance_before=Decimal("100"),
                balance_after=None,
                site_code="my",
                store_name="shop-b",
                recharge_module=object(),
                driver=object(),
                default_ads_wallet_url="https://example.test",
                balance_check_attempts=1,
            )

        self.assertEqual(result["status"], "corrected_success")
        self.assertEqual(result["balance_after"], Decimal("150"))
        self.assertTrue(result["retry_balance_check"])
        self.assertTrue(result["corrected_to_success"])

    def test_vietnam_balance_delta_outside_tolerance_is_not_confirmed(self):
        with patch.object(ads_recharge_guard, "query_ads_credit_with_reopened_store", return_value=Decimal("1400001")):
            result = ads_recharge_guard.verify_recharge_status_with_balance_check(
                status="success",
                message="payment popup success",
                recharge_amount=Decimal("1000000"),
                balance_before=Decimal("100000"),
                balance_after=None,
                site_code="vn",
                store_name="shop-vn",
                recharge_module=object(),
                driver=object(),
                default_ads_wallet_url="https://example.test",
                balance_check_attempts=1,
            )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["corrected_to_success"])

    def test_thailand_balance_delta_inside_tolerance_is_confirmed(self):
        with patch.object(ads_recharge_guard, "query_ads_credit_with_reopened_store", return_value=Decimal("10499")):
            result = ads_recharge_guard.verify_recharge_status_with_balance_check(
                status="success",
                message="payment popup success",
                recharge_amount=Decimal("10000"),
                balance_before=Decimal("0"),
                balance_after=None,
                site_code="th",
                store_name="shop-th",
                recharge_module=object(),
                driver=object(),
                default_ads_wallet_url="https://example.test",
                balance_check_attempts=1,
            )

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["retry_balance_check"])

    def test_second_balance_check_can_confirm_delayed_arrival(self):
        balances = [Decimal("100"), Decimal("150")]
        with patch.object(ads_recharge_guard, "query_ads_credit_with_reopened_store", side_effect=balances):
            result = ads_recharge_guard.verify_recharge_status_with_balance_check(
                status="success",
                message="payment popup success",
                recharge_amount=Decimal("50"),
                balance_before=Decimal("100"),
                balance_after=None,
                site_code="my",
                store_name="shop-delayed",
                recharge_module=object(),
                driver=object(),
                default_ads_wallet_url="https://example.test",
                balance_check_attempts=2,
                balance_check_retry_delay_seconds=0,
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["balance_after"], Decimal("150"))

    def test_balance_before_read_retries_stale_element(self):
        class FakeDriver:
            def __init__(self):
                self.refresh_count = 0

            def refresh(self):
                self.refresh_count += 1

        class FakeWebdriverUtil:
            def __init__(self):
                self.driver = FakeDriver()

        class FakeRechargeModule:
            def __init__(self):
                self.calls = 0
                self.close_calls = 0

            def _wait_for_ads_credit(self, webdriver_util, store_name, timeout=30):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("stale element reference")
                return None, Decimal("12345"), ""

            def close_alert(self, webdriver_util):
                self.close_calls += 1

        module = FakeRechargeModule()
        webdriver_util = FakeWebdriverUtil()

        result = ads_recharge_guard._read_balance_before_with_retries(
            module,
            webdriver_util,
            "shop-stale",
            attempts=2,
            retry_delay_seconds=0,
        )

        self.assertEqual(result, Decimal("12345.00"))
        self.assertEqual(module.calls, 2)
        self.assertEqual(module.close_calls, 1)
        self.assertEqual(webdriver_util.driver.refresh_count, 1)

    def test_payment_wait_stops_when_browser_session_is_invalid(self):
        class InvalidSessionDriver:
            @property
            def window_handles(self):
                raise vn_ads_recharge.InvalidSessionIdException("invalid session id")

        payment_ok, message = vn_ads_recharge._wait_for_payment_result(
            InvalidSessionDriver(),
            timeout=300,
        )

        self.assertEqual(payment_ok, "processing")
        self.assertIn("等待重新开店余额复查", message)

    def test_processing_payment_is_confirmed_after_delayed_balance_check(self):
        with patch.object(ads_recharge_guard, "query_ads_credit_with_reopened_store", return_value=Decimal("150")):
            result = ads_recharge_guard.verify_recharge_status_with_balance_check(
                status="processing",
                message="payment submitted, session closed",
                recharge_amount=Decimal("50"),
                balance_before=Decimal("100"),
                balance_after=None,
                site_code="my",
                store_name="shop-processing-success",
                recharge_module=object(),
                driver=object(),
                default_ads_wallet_url="https://example.test",
                balance_check_attempts=1,
                processing_initial_delay_seconds=0,
            )

        self.assertEqual(result["status"], "corrected_success")
        self.assertTrue(result["corrected_to_success"])

    def test_processing_payment_stays_processing_when_balance_is_unchanged(self):
        with patch.object(ads_recharge_guard, "query_ads_credit_with_reopened_store", return_value=Decimal("100")):
            result = ads_recharge_guard.verify_recharge_status_with_balance_check(
                status="processing",
                message="payment submitted, session closed",
                recharge_amount=Decimal("50"),
                balance_before=Decimal("100"),
                balance_after=None,
                site_code="my",
                store_name="shop-processing-pending",
                recharge_module=object(),
                driver=object(),
                default_ads_wallet_url="https://example.test",
                balance_check_attempts=1,
                processing_initial_delay_seconds=0,
            )

        self.assertEqual(result["status"], "processing")
        self.assertIn("请勿重复支付", result["message"])


if __name__ == "__main__":
    unittest.main()
