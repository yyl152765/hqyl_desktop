import sys
import unittest
from decimal import Decimal, localcontext
from pathlib import Path


SUPERBROWSER_ROOT = Path(__file__).resolve().parents[2] / "superbrowser_process"
if str(SUPERBROWSER_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERBROWSER_ROOT))

from util.shopee_ads_money import (
    MONEY_PARSER_VERSION,
    SITE_CURRENCY,
    SITE_CURRENCY_DECIMALS,
    ShopeeMoneyParseError,
    parse_shopee_money,
)


class ShopeeAdsMoneyTests(unittest.TestCase):
    def test_parser_metadata(self):
        self.assertEqual(MONEY_PARSER_VERSION, "1.0.0")
        self.assertEqual(SITE_CURRENCY["id"], "IDR")
        self.assertEqual(SITE_CURRENCY["vn"], "VND")
        self.assertEqual(SITE_CURRENCY_DECIMALS["id"], 0)
        self.assertEqual(SITE_CURRENCY_DECIMALS["vn"], 0)
        self.assertTrue(issubclass(ShopeeMoneyParseError, ValueError))

    def test_id_vn_grouping_and_currency(self):
        for site, marker in (("id", "Rp"), ("vn", "₫")):
            for text, expected in (
                ("0", "0"),
                ("790.329", "790329"),
                ("790,329", "790329"),
                ("4.363", "4363"),
                ("1.000", "1000"),
                ("1,000", "1000"),
                ("1.500.000", "1500000"),
                ("1,500,000", "1500000"),
                ("1234567890", "1234567890"),
                (marker + "\u00a0790.329", "790329"),
                (" 790,329\u202f" + SITE_CURRENCY[site] + " ", "790329"),
            ):
                with self.subTest(site=site, text=text):
                    self.assertEqual(parse_shopee_money(text, site), Decimal(expected))
        self.assertEqual(parse_shopee_money("idr 123.456", " ID "), Decimal(123456))
        self.assertEqual(parse_shopee_money("VND 123.456", "VN"), Decimal(123456))
        self.assertEqual(parse_shopee_money("123.456 đ", "vn"), Decimal(123456))

    def test_zero_decimal_sites_reject_fractional_strings_and_bad_groups(self):
        for site in ("id", "vn"):
            for text in (
                "790.32", "790,32", "790.3290", "1.234,56", "1,234.56",
                "0.00", "1.000,00", "1,000.00", "1.2", "1,2",
                "1.234,567", "1,234.567", "12.34.567", "1234.567",
                "1,23,456", "1.23.456", "1..000", "1,,000", ".100", "100.",
                "1 000", "1\u00a0000", "1\u202f000", "1_000",
            ):
                with self.subTest(site=site, text=text):
                    with self.assertRaises(ShopeeMoneyParseError):
                        parse_shopee_money(text, site)

    def test_nonzero_decimal_sites_accept_exact_minor_units(self):
        for site, marker in (("my", "RM"), ("th", "฿"), ("ph", "₱")):
            for text, expected in (
                (marker + "1,234.56", "1234.56"),
                (marker + "1.234,56", "1234.56"),
                ("1234.5", "1234.5"),
                ("1234,50", "1234.50"),
                ("1.234", "1234"),
                ("1,234", "1234"),
                ("0.00", "0.00"),
            ):
                with self.subTest(site=site, text=text):
                    self.assertEqual(parse_shopee_money(text, site), Decimal(expected))
            self.assertEqual(parse_shopee_money(Decimal("1234.5600"), site), Decimal("1234.56"))
            for text in ("1234.567", "1.2345", "1,2345", "1,23.45", "1.23,45"):
                with self.subTest(site=site, invalid=text):
                    with self.assertRaises(ShopeeMoneyParseError):
                        parse_shopee_money(text, site)

    def test_numeric_types_preserve_numeric_meaning(self):
        for site in SITE_CURRENCY:
            for number in (0, 790329, 790329.0, Decimal("790329"), Decimal("790329.000")):
                with self.subTest(site=site, number=number):
                    self.assertEqual(parse_shopee_money(number, site), Decimal(str(number)))
            with self.assertRaises(ShopeeMoneyParseError):
                parse_shopee_money(790.329, site)
            with self.assertRaises(ShopeeMoneyParseError):
                parse_shopee_money(Decimal("790.329"), site)
        for site in ("id", "vn"):
            with self.assertRaises(ShopeeMoneyParseError):
                parse_shopee_money(Decimal("123.01"), site)
        self.assertEqual(parse_shopee_money(Decimal("0.000"), "id"), Decimal(0))

    def test_blank_unsupported_negative_and_nonfinite_values_fail(self):
        values = (
            None, True, False, {}, [], object(), "", " \t\u00a0", "-1", "+1",
            "-0", "(1000)", "NaN", "Infinity", "inf", "1e3", "1E+3",
            float("nan"), float("inf"), float("-inf"), -1, -0.0,
            Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"),
            Decimal("-Infinity"), Decimal("-1"), Decimal("-0"),
        )
        for value in values:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ShopeeMoneyParseError):
                    parse_shopee_money(value, "id")

    def test_complete_match_rejects_fragments_wrong_currency_and_compact_units(self):
        for text in (
            "Rp1.4m", "Rp1,4m", "Rp1K", "Rp1k", "Rp1w", "Rp1万",
            "Expense Rp123.456", "Rp123.456 12%", "Rp1.000\nRp2.000",
            "Rp\n1.000", "Rp1.000\n", "Rp1.000 USD", "USD1.000",
            "$1,000", "RM1.000", "VND1.000", "₫1.000", "RpRp1.000",
            "Rp1.000IDR", "1.000RpRp", "Rp１.０００", "Rp١.٠٠٠",
            "Rp1.000\u200b", "Rp1.000\x00", "Rp1.000<script>",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ShopeeMoneyParseError):
                    parse_shopee_money(text, "id")
        for text in ("Rp1.000", "IDR1.000", "₫1,2m", "₫10k"):
            with self.assertRaises(ShopeeMoneyParseError):
                parse_shopee_money(text, "vn")

    def test_failure_messages_do_not_echo_input(self):
        secret_fragment = "private-value-do-not-echo-728314"
        for value, site in ((secret_fragment, "id"), ("123", secret_fragment)):
            with self.assertRaises(ShopeeMoneyParseError) as caught:
                parse_shopee_money(value, site)
            self.assertNotIn(secret_fragment, str(caught.exception))
        for site in (None, True, "", "br", "IDR"):
            with self.assertRaises(ShopeeMoneyParseError):
                parse_shopee_money("123", site)

    def test_large_amount_is_not_rounded_by_decimal_context(self):
        value = Decimal("1234567890123456789012345678901234567890.000")
        with localcontext() as context:
            context.prec = 6
            self.assertEqual(parse_shopee_money(value, "id"), value)
            self.assertEqual(parse_shopee_money(str(value).removesuffix(".000"), "id"), value)
            with self.assertRaises(ShopeeMoneyParseError):
                parse_shopee_money(Decimal("123456789012345678901234567890.001"), "id")


if __name__ == "__main__":
    unittest.main()
