"""Tests for Lazada Ads Monthly Report business logic, formulas, date handling, and workbook."""
import os
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from backend.services.lazada_ads_monthly import (
    calculate_month_dates,
    get_default_month,
    validate_ads_monthly_request,
)
from backend.services.lazada_ads_monthly_download import (
    DownloadError,
    _parse_decimal,
    parse_coupon_file,
)
from backend.services.lazada_ads_monthly_page import parse_spend_card_value
from backend.services.lazada_ads_monthly_workbook import (
    HEADERS,
    create_ads_monthly_workbook,
    export_ads_monthly_workbook,
)


class TestLazadaAdsMonthlyLogic(unittest.TestCase):
    def test_default_month(self):
        m = get_default_month()
        self.assertRegex(m, r"^\d{4}-\d{2}$")

    def test_month_calculation_leap_year(self):
        # 2024 is leap year (February has 29 days)
        start, end = calculate_month_dates("2024-02")
        self.assertEqual(start, "2024-02-01")
        self.assertEqual(end, "2024-02-29")

        # 2025 is not leap year (February has 28 days)
        start, end = calculate_month_dates("2025-02")
        self.assertEqual(start, "2025-02-01")
        self.assertEqual(end, "2025-02-28")

    def test_month_calculation_invalid(self):
        # Month 13
        with self.assertRaises(ValueError):
            calculate_month_dates("2024-13")

        # Future month
        with self.assertRaises(ValueError):
            calculate_month_dates("2099-01")

        # Malformed
        with self.assertRaises(ValueError):
            calculate_month_dates("invalid-month")

    def test_formula_baseline_n01(self):
        # 903.15 - 49.59 - 0 = 853.56
        ads = Decimal("903.15")
        aff = Decimal("49.59")
        cou = Decimal("0")
        act = ads - aff - cou
        self.assertEqual(act, Decimal("853.56"))

        # 0 - 66.91 - 0 = -66.91 (retaining negative number)
        ads = Decimal("0")
        aff = Decimal("66.91")
        cou = Decimal("0")
        act = ads - aff - cou
        self.assertEqual(act, Decimal("-66.91"))

    def test_spec_eight_values_f01(self):
        # Specification 2.16 coupon values:
        # ["2200", "29.54", "41.70", "3305", "16525", "13.22", "20.29", "9.91"] -> 22144.66
        values = ["2200", "29.54", "41.70", "3305", "16525", "13.22", "20.29", "9.91"]
        total = sum((Decimal(v) for v in values), Decimal("0"))
        self.assertEqual(total, Decimal("22144.66"))

    def test_spend_card_rounded_display_n03(self):
        # 50.16K without full_attr -> rounded_display
        res = parse_spend_card_value("50.16K", full_attr="")
        self.assertEqual(res.precision, "rounded_display")
        self.assertIsNone(res.decimal_value)
        self.assertEqual(res.error, "precision_unverified")

        # 50.16K with exact attribute "50162.34"
        res = parse_spend_card_value("50.16K", full_attr="50162.34")
        self.assertEqual(res.precision, "exact")
        self.assertEqual(res.decimal_value, Decimal("50162.34"))

        # 15.94K
        res = parse_spend_card_value("15.94K")
        self.assertEqual(res.precision, "rounded_display")
        self.assertIsNone(res.decimal_value)

        # Normal exact value 903.15
        res = parse_spend_card_value("903.15")
        self.assertEqual(res.precision, "exact")
        self.assertEqual(res.decimal_value, Decimal("903.15"))

    def test_parse_coupon_csv_with_line_breaks_and_total_f02_f06(self):
        tmp_dir = Path(tempfile.mkdtemp())
        try:
            csv_file = tmp_dir / "coupon.csv"
            # Header with newline in Amount column (properly quoted in CSV), and Total row at bottom
            content = (
                'Date & Time,Wallet Type,Transaction Type,Status,"Amo\nunt"\n'
                "2026-10-02 10:00:00,Coupon,Top up,Success,2200\n"
                "2026-10-05 11:00:00,Coupon,Top up,Success,29.54\n"
                "2026-10-08 12:00:00,Coupon,Top up,Success,41.70\n"
                "2026-10-12 13:00:00,Coupon,Top up,Success,3305\n"
                "2026-10-15 14:00:00,Coupon,Top up,Success,16525\n"
                "2026-10-18 15:00:00,Coupon,Top up,Success,13.22\n"
                "2026-10-22 16:00:00,Coupon,Top up,Success,20.29\n"
                "2026-10-28 17:00:00,Coupon,Top up,Success,9.91\n"
                "Total,,,,22144.66\n"
            )
            csv_file.write_text(content, encoding="utf-8")

            total, audit = parse_coupon_file(csv_file, "2026-10")
            self.assertEqual(total, Decimal("22144.66"))
            self.assertEqual(audit.detail_rows_count, 8)
            self.assertEqual(audit.excluded_rows_count, 1)  # Total row excluded
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_parse_coupon_file_date_mismatch_f04(self):
        tmp_dir = Path(tempfile.mkdtemp())
        try:
            csv_file = tmp_dir / "coupon_wrong_date.csv"
            content = (
                "Date,Wallet,Type,Status,Amount\n"
                "2026-09-30 23:59:00,Coupon,Top up,Success,100\n"
            )
            csv_file.write_text(content, encoding="utf-8")
            with self.assertRaises(DownloadError) as ctx:
                parse_coupon_file(csv_file, "2026-10")
            self.assertIn("不属于目标月份", str(ctx.exception))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_workbook_creation_and_headers(self):
        self.assertEqual(
            HEADERS[:8],
            [
                "店铺名称",
                "广告金额统计",
                "广告截图",
                "联盟花费",
                "联盟截图",
                "优惠金额",
                "优惠券截图",
                "真实花费",
            ],
        )

        rows = [
            {
                "store_name": "TestStore-TH",
                "ads_spend": "903.15",
                "evidence_ads": None,
                "affiliate_spend": "49.59",
                "evidence_affiliate": None,
                "coupon_amount": "0",
                "evidence_coupon": None,
                "actual_spend": "853.56",
                "country": "TH",
                "currency": "THB",
                "month": "2026-10",
                "status": "success",
                "captured_at": "2026-11-04 12:00:00",
                "note": "",
                "run_id": "test_run",
            }
        ]

        tmp_dir = Path(tempfile.mkdtemp())
        try:
            out_file = tmp_dir / "report.xlsx"
            from PIL import Image
            for key in ('evidence_ads','evidence_affiliate','evidence_coupon'):
                img = tmp_dir / (key + '.png')
                Image.new('RGB', (50, 50), 'white').save(img)
                rows[0][key] = str(img)
            export_ads_monthly_workbook(out_file, rows)
            self.assertTrue(out_file.is_file())

            # Verify it can be loaded
            from openpyxl import load_workbook
            wb = load_workbook(out_file, data_only=True)
            sheet = wb["广告报表"]
            self.assertEqual(sheet.cell(1, 1).value, "店铺名称")
            self.assertEqual(sheet.cell(1, 8).value, "真实花费")
            self.assertEqual(sheet.cell(2, 1).value, "TestStore-TH")
            self.assertEqual(sheet.cell(2, 2).value, 903.15)
            self.assertEqual(sheet.cell(2, 4).value, 49.59)
            self.assertEqual(sheet.cell(2, 6).value, 0.0)
            self.assertEqual(sheet.cell(2, 8).value, 853.56)
            self.assertEqual(len(sheet._images), 3)
            wb.close()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
