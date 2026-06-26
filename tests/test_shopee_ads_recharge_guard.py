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
for _stub_name in _INSERTED_STUB_MODULES:
    sys.modules.pop(_stub_name, None)
if _INSERTED_SUPERBROWSER_PATH:
    sys.path.remove(str(SUPERBROWSER_ROOT))


class ShopeeAdsRechargeGuardTests(unittest.TestCase):
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
            )

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["retry_balance_check"])


if __name__ == "__main__":
    unittest.main()
