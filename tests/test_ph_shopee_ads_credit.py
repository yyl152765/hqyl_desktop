from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPERBROWSER_ROOT = PROJECT_ROOT.parent / "superbrowser_process"
if str(SUPERBROWSER_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERBROWSER_ROOT))

from implement.shopee import ph_shopee_ads_recharge as ph_recharge


class _FakeElement:
    def __init__(self, text):
        self.text = text


class PhilippinesShopeeAdsCreditTests(unittest.TestCase):
    def test_strict_amount_parser_rejects_seven_day_rebate_copy(self):
        self.assertIsNone(ph_recharge._parse_ads_credit_amount("过去 7 天的回扣: ₱0.00"))
        self.assertIsNone(ph_recharge._parse_ads_credit_amount("对比过去 7 天 7.86%"))

    def test_strict_amount_parser_accepts_real_balance_formats(self):
        self.assertEqual(ph_recharge._parse_ads_credit_amount("₱8,352.74"), 8352.74)
        self.assertEqual(ph_recharge._parse_ads_credit_amount("PHP 8,352.74"), 8352.74)
        self.assertEqual(ph_recharge._parse_ads_credit_amount("₱0.00"), 0)
        self.assertEqual(ph_recharge._parse_ads_credit_amount("₱7.00"), 7)

    def test_candidate_scan_skips_rebate_and_uses_pure_balance(self):
        rebate = _FakeElement("过去 7 天的回扣: ₱0.00")
        balance = _FakeElement("₱8,352.74")

        def visible_elements(_driver, xpath):
            return [rebate] if xpath == "rebate" else [balance]

        with patch.object(ph_recharge, "_find_visible_elements", side_effect=visible_elements):
            element, amount, text = ph_recharge._find_ads_credit_candidate(
                object(),
                ["rebate", "balance"],
            )

        self.assertIs(element, balance)
        self.assertEqual(amount, 8352.74)
        self.assertEqual(text, "₱8,352.74")

    def test_candidate_scan_fails_closed_when_only_rebate_is_present(self):
        rebate = _FakeElement("过去 7 天的回扣: ₱0.00")
        with patch.object(ph_recharge, "_find_visible_elements", return_value=[rebate]):
            element, amount, text = ph_recharge._find_ads_credit_candidate(
                object(),
                ["rebate"],
            )

        self.assertIsNone(element)
        self.assertIsNone(amount)
        self.assertEqual(text, "过去 7 天的回扣: ₱0.00")

    def test_wait_requires_two_stable_balance_reads(self):
        balance = _FakeElement("₱8,352.74")
        candidate = (balance, 8352.74, "₱8,352.74")
        with (
            patch.object(ph_recharge, "_find_ads_credit_candidate", return_value=candidate) as find_candidate,
            patch.object(ph_recharge.time, "time", return_value=0),
            patch.object(ph_recharge.time, "sleep"),
        ):
            element, amount, text = ph_recharge._wait_for_amount_element_by_xpaths(
                object(),
                ["balance"],
                timeout=1,
            )

        self.assertIs(element, balance)
        self.assertEqual(amount, 8352.74)
        self.assertEqual(text, "₱8,352.74")
        self.assertEqual(find_candidate.call_count, 2)

    def test_balance_selectors_do_not_include_page_wide_currency_fallback(self):
        selectors = "\n".join(ph_recharge.ADS_CREDIT_FALLBACKS)
        self.assertNotIn("ancestor::*", selectors)
        self.assertNotIn("(//*[contains(text(), '₱')", selectors)
        self.assertIn("credit-expense-label-wrapper", selectors)
        self.assertIn("summary-primary", selectors)

    def test_unreliable_balance_stops_before_dingtalk_or_payment(self):
        webdriver_util = Mock()
        webdriver_util.driver = object()

        with (
            patch.object(ph_recharge, "WebdriverUtil", return_value=webdriver_util),
            patch.object(ph_recharge, "_ensure_login", return_value=True),
            patch.object(ph_recharge.time, "sleep"),
            patch.object(
                ph_recharge,
                "_wait_for_ads_credit",
                return_value=(None, 0, "未可靠识别广告余额，已停止充值"),
            ),
            patch.object(ph_recharge, "ansy_dingtalk_doc") as sync_dingtalk,
            patch.object(ph_recharge, "recharge_ads") as recharge_ads,
            patch.object(ph_recharge, "_write_error"),
        ):
            result = ph_recharge.ph_shopee_ads_recharge(
                webdriver_util.driver,
                "陈玉莲-菲律宾005-PH007",
            )

        self.assertEqual(result["status"], "failed")
        self.assertIn("已停止充值", result["message"])
        sync_dingtalk.assert_not_called()
        recharge_ads.assert_not_called()


if __name__ == "__main__":
    unittest.main()
