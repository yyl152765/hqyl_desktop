from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPERBROWSER_ROOT = PROJECT_ROOT.parent / "superbrowser_process"
if str(SUPERBROWSER_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERBROWSER_ROOT))

from implement.shopee import id_shopee_ads_recharge as id_recharge
from implement.shopee import my_shopee_ads_recharge as my_recharge
from implement.shopee import th_shopee_ads_recharge as th_recharge
from implement.shopee import vn_shopee_ads_recharge as vn_recharge


class _FakeElement:
    def __init__(self, text):
        self.text = text


SITE_CASES = (
    (
        "id",
        id_recharge,
        (("Rp8.352", 8352), ("IDR 8.352", 8352), ("Rp0", 0), ("Rp7", 7)),
        "过去 7 天的回扣: Rp0",
    ),
    (
        "th",
        th_recharge,
        (("฿8,352.74", 8352.74), ("THB 8,352.74", 8352.74), ("฿0.00", 0), ("฿7.00", 7)),
        "过去 7 天的回扣: ฿0.00",
    ),
    (
        "my",
        my_recharge,
        (("RM8,352.74", 8352.74), ("MYR 8,352.74", 8352.74), ("RM0.00", 0), ("RM7.00", 7)),
        "过去 7 天的回扣: RM0.00",
    ),
    (
        "vn",
        vn_recharge,
        (("₫8.352.000", 8352000), ("VND 8.352.000", 8352000), ("₫0", 0), ("₫7", 7)),
        "过去 7 天的回扣: ₫0",
    ),
)


class AllSitesShopeeAdsCreditTests(unittest.TestCase):
    def test_strict_parsers_accept_balances_and_reject_narrative_copy(self):
        for site, module, accepted, narrative in SITE_CASES:
            with self.subTest(site=site, kind="reject"):
                self.assertIsNone(module._parse_ads_credit_amount(narrative))
                self.assertIsNone(module._parse_ads_credit_amount("对比过去 7 天 7.86%"))
            for text, expected in accepted:
                with self.subTest(site=site, text=text):
                    self.assertEqual(module._parse_ads_credit_amount(text), expected)

    def test_candidate_scan_skips_narrative_and_uses_bound_balance(self):
        for site, module, accepted, narrative in SITE_CASES:
            balance_text, expected = accepted[0]
            rebate = _FakeElement(narrative)
            balance = _FakeElement(balance_text)

            def visible_elements(_driver, xpath):
                return [rebate] if xpath == "rebate" else [balance]

            with self.subTest(site=site), patch.object(
                module,
                "_find_visible_elements",
                side_effect=visible_elements,
            ):
                element, amount, text = module._find_ads_credit_candidate(
                    object(),
                    ["rebate", "balance"],
                )
                self.assertIs(element, balance)
                self.assertEqual(amount, expected)
                self.assertEqual(text, balance_text)

    def test_candidate_scan_fails_closed_when_only_narrative_is_present(self):
        for site, module, _accepted, narrative in SITE_CASES:
            rebate = _FakeElement(narrative)
            with self.subTest(site=site), patch.object(
                module,
                "_find_visible_elements",
                return_value=[rebate],
            ):
                element, amount, text = module._find_ads_credit_candidate(
                    object(),
                    ["rebate"],
                )
                self.assertIsNone(element)
                self.assertIsNone(amount)
                self.assertEqual(text, narrative)

    def test_wait_times_out_and_fails_closed_when_only_narrative_is_present(self):
        for site, module, _accepted, narrative in SITE_CASES:
            webdriver_util = Mock()
            webdriver_util.driver = object()
            with (
                self.subTest(site=site),
                patch.object(module, "close_alert"),
                patch.object(
                    module,
                    "_find_ads_credit_candidate",
                    return_value=(None, None, narrative),
                ) as find_candidate,
                patch.object(module, "_short_page_context", return_value="page context"),
                patch.object(module.time, "time", side_effect=[0, 0, 2]),
                patch.object(module.time, "sleep"),
            ):
                element, amount, error = module._wait_for_ads_credit(
                    webdriver_util,
                    f"{site}-测试店铺",
                    timeout=1,
                )

            self.assertIsNone(element)
            self.assertEqual(amount, 0)
            self.assertIn("未可靠识别", error)
            self.assertIn(narrative, error)
            self.assertEqual(find_candidate.call_count, 1)

    def test_wait_requires_two_stable_balance_reads(self):
        for site, module, accepted, _narrative in SITE_CASES:
            balance_text, expected = accepted[0]
            balance = _FakeElement(balance_text)
            candidate = (balance, expected, balance_text)
            with (
                self.subTest(site=site),
                patch.object(module, "_find_ads_credit_candidate", return_value=candidate) as find_candidate,
                patch.object(module.time, "time", return_value=0),
                patch.object(module.time, "sleep"),
            ):
                element, amount, text = module._wait_for_amount_element_by_xpaths(
                    object(),
                    ["balance"],
                    timeout=1,
                )
                self.assertIs(element, balance)
                self.assertEqual(amount, expected)
                self.assertEqual(text, balance_text)
                self.assertEqual(find_candidate.call_count, 2)

    def test_wait_requires_two_stable_reads_after_narrative_changes_to_balance(self):
        for site, module, accepted, narrative in SITE_CASES:
            balance_text, expected = accepted[0]
            balance = _FakeElement(balance_text)
            with (
                self.subTest(site=site),
                patch.object(
                    module,
                    "_find_ads_credit_candidate",
                    side_effect=[
                        (None, None, narrative),
                        (balance, expected, balance_text),
                        (balance, expected, balance_text),
                    ],
                ) as find_candidate,
                patch.object(module.time, "time", return_value=0),
                patch.object(module.time, "sleep"),
            ):
                element, amount, text = module._wait_for_amount_element_by_xpaths(
                    object(),
                    ["balance"],
                    timeout=1,
                )

            self.assertIs(element, balance)
            self.assertEqual(amount, expected)
            self.assertEqual(text, balance_text)
            self.assertEqual(find_candidate.call_count, 3)

    def test_balance_selectors_are_label_bound_and_not_page_wide(self):
        for site, module, _accepted, _narrative in SITE_CASES:
            selectors = "\n".join(module.ADS_CREDIT_FALLBACKS)
            with self.subTest(site=site):
                self.assertNotIn("ancestor::*", selectors)
                self.assertNotIn("not(self::script)", selectors)
                self.assertIn("credit-expense-label-wrapper", selectors)
                self.assertIn("summary-primary", selectors)
                self.assertIn("Ads Credit", selectors)

    def test_unreliable_balance_marks_review_without_budget_or_payment(self):
        for site, module, _accepted, _narrative in SITE_CASES:
            webdriver_util = Mock()
            webdriver_util.driver = object()
            entrypoint = getattr(module, f"{site}_shopee_ads_recharge")
            with (
                self.subTest(site=site),
                patch.object(module, "WebdriverUtil", return_value=webdriver_util),
                patch.object(module, "_ensure_login", return_value=True),
                patch.object(module, "close_alert"),
                patch.object(module.time, "sleep"),
                patch.object(
                    module,
                    "_wait_for_ads_credit",
                    return_value=(None, 0, "未可靠识别广告余额，已停止充值"),
                ),
                patch.object(module, "ansy_dingtalk_doc") as sync_dingtalk,
                patch.object(module, "mark_ads_pending_review", create=True) as mark_review,
                patch.object(module, "recharge_ads") as recharge_ads,
                patch.object(module, "_write_error"),
            ):
                result = entrypoint(webdriver_util.driver, f"{site}-测试店铺")

            self.assertEqual(result["status"], "failed")
            self.assertIn("已停止充值", result["message"])
            sync_dingtalk.assert_not_called()
            recharge_ads.assert_not_called()
            if site in ("id", "vn"):
                mark_review.assert_called_once()


if __name__ == "__main__":
    unittest.main()
