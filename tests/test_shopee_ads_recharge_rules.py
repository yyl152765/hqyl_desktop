from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPERBROWSER_ROOT = PROJECT_ROOT.parent / "superbrowser_process"
_INSERTED_SUPERBROWSER_PATH = False
if str(SUPERBROWSER_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERBROWSER_ROOT))
    _INSERTED_SUPERBROWSER_PATH = True

from implement.shopee.ads_recharge_rules import (
    SITE_BALANCE_SUCCESS_TOLERANCE,
    format_minimum_recharge_skip_message,
    get_min_recharge_amount,
    is_recharge_amount_below_minimum,
)

if _INSERTED_SUPERBROWSER_PATH:
    sys.path.remove(str(SUPERBROWSER_ROOT))


class ShopeeAdsRechargeRulesTests(unittest.TestCase):
    def test_min_recharge_amounts(self):
        self.assertEqual(str(get_min_recharge_amount("vn")), "300000")
        self.assertEqual(str(get_min_recharge_amount("th")), "500")
        self.assertEqual(str(get_min_recharge_amount("my")), "50")
        self.assertEqual(str(get_min_recharge_amount("ph")), "500")
        self.assertIsNone(get_min_recharge_amount("id"))

    def test_balance_success_tolerance_for_indonesia(self):
        self.assertEqual(str(SITE_BALANCE_SUCCESS_TOLERANCE["id"]), "40000")

    def test_below_minimum_is_strictly_less_than_threshold(self):
        self.assertTrue(is_recharge_amount_below_minimum("299999", "vn"))
        self.assertFalse(is_recharge_amount_below_minimum("300000", "vn"))
        self.assertTrue(is_recharge_amount_below_minimum("499.99", "th"))
        self.assertFalse(is_recharge_amount_below_minimum("500", "th"))

    def test_skip_message_includes_amount_and_threshold(self):
        message = format_minimum_recharge_skip_message("ph", "shop-a", "499")
        self.assertIn("shop-a", message)
        self.assertIn("499", message)
        self.assertIn("500", message)


if __name__ == "__main__":
    unittest.main()
