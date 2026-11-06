"""Regression cases found during the monthly workflow acceptance audit."""
import subprocess
import tempfile
import unittest
from decimal import Decimal, InvalidOperation
from pathlib import Path
from unittest.mock import patch

from backend.core.ziniao_cli import ZiniaoCliError
from backend.services.lazada_ads_monthly import calculate_month_dates
from backend.services.lazada_ads_monthly_cli import ZiniaoMonthlyCli
from backend.services.lazada_ads_monthly_download import DownloadError, _parse_decimal, parse_coupon_file, wait_for_download
from backend.services.lazada_ads_monthly_page import detect_country_from_url_or_name, get_site_profile, parse_spend_card_value, verify_spend_label_currency


class MonthlyRegressionTests(unittest.TestCase):
    def test_doctor_zero_exit_does_not_mean_healthy(self):
        cli = ZiniaoMonthlyCli.__new__(ZiniaoMonthlyCli)
        cli.profile, cli.auth_failed = 'test', False
        result = subprocess.CompletedProcess([], 0, '✓ API Key 有效\n✗ 客户端未启动\n✗ ZClaw Bridge 未就绪', '')
        with patch.object(cli, 'assert_profile'), patch.object(cli, '_process', return_value=result):
            with self.assertRaises(ZiniaoCliError):
                cli.ready()

    def test_screenshot_failure_must_not_create_substitute(self):
        cli = ZiniaoMonthlyCli.__new__(ZiniaoMonthlyCli)
        with tempfile.TemporaryDirectory() as td, patch.object(cli, '_execute_page_command', side_effect=ZiniaoCliError('timeout')):
            with self.assertRaises(ZiniaoCliError):
                cli.screenshot('1', Path(td) / 'evidence.png')

    def test_close_failure_keeps_ownership_and_propagates(self):
        cli = ZiniaoMonthlyCli.__new__(ZiniaoMonthlyCli)
        cli.owned_windows, cli.targets = {'1'}, {'1': 'target'}
        with patch.object(cli, 'run', side_effect=ZiniaoCliError('close failed')):
            with self.assertRaises(ZiniaoCliError):
                cli.close_store('1')
        self.assertIn('1', cli.owned_windows)

    def test_unknown_country_is_not_thailand(self):
        self.assertEqual(detect_country_from_url_or_name('', 'An unidentified shop'), '')
        with self.assertRaises(ValueError):
            get_site_profile('XX')

    def test_host_match_is_exact(self):
        self.assertEqual(detect_country_from_url_or_name('https://sellercenter.lazada.co.th.evil.example/', ''), '')

    def test_year_zero_rejected(self):
        with self.assertRaises(ValueError):
            calculate_month_dates('0000-01')

    def test_abbreviations_never_exact(self):
        for text in ('฿50.16K', '50.16 K', '1.2M', '1.2万', '1.2B', '1,2K'):
            with self.subTest(text=text):
                self.assertIsNone(parse_spend_card_value(text).decimal_value)

    def test_localized_spend_currency_is_verified(self):
        for label in ('花费(泰铢)', '花费（泰铢）', 'Spend (THB)', 'Spend'):
            with self.subTest(label=label):
                verify_spend_label_currency(label, 'THB')
        for label in ('花费(USD)', '花费(未知)', '订单(THB)', '花费(泰铢) 20%'):
            with self.subTest(label=label), self.assertRaises(ZiniaoCliError):
                verify_spend_label_currency(label, 'THB')
        with self.assertRaises(ZiniaoCliError):
            verify_spend_label_currency('花费(泰铢)', 'MYR')

    def test_non_amount_text_cannot_be_parsed_as_amount(self):
        for text in ('2026-10-01 Spend 500', '500 20%', 'Spend 42 orders 10'):
            with self.subTest(text=text):
                self.assertIsNone(parse_spend_card_value(text).decimal_value)

    def test_decimal_rejects_corrupt_or_shortened_value(self):
        for value in ('10oops20', '1.2K', '100%', '1,2,3', 'NaN', 'Infinity', True):
            with self.subTest(value=value), self.assertRaises((InvalidOperation, ValueError)):
                _parse_decimal(value)

    def test_parenthesized_negative(self):
        self.assertEqual(_parse_decimal('(1,234.50)'), Decimal('-1234.50'))

    def parse(self, text):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'coupon.csv'
            p.write_text(text, encoding='utf8')
            return parse_coupon_file(p, '2026-10')

    def test_bad_coupon_rows_rejected(self):
        for row in ('2026-10-02,Cash,Top up,Success,100',
                    '2026-10-02,Coupon,Spend,Success,100',
                    'not-a-date,Coupon,Top up,Success,100',
                    '2026-10-32,Coupon,Top up,Success,100',
                    '2026-10-02,Coupon,Top up,Success,',
                    '2026-10-02,Coupon,Top up,Success,1.2K'):
            with self.subTest(row=row), self.assertRaises(DownloadError):
                self.parse('Date,Wallet,Type,Status,Amount\n' + row + '\n')

    def test_empty_file_not_verified_zero(self):
        with self.assertRaises(DownloadError):
            self.parse('')

    def test_ambiguous_amount_header_rejected(self):
        with self.assertRaises(DownloadError):
            self.parse('Date,Wallet,Type,Amount,Amount\n2026-10-01,Coupon,Top up,20,30\n')

    def test_lazada_segmentation_and_category_are_distinct(self):
        total, audit = self.parse(
            'Date & Time,WalletType,Segmentation,Amount,type,subType,Method,Status\n'
            '2026-10-02 08:00:00,优惠券,充值,12.34,券的授予,券的授予,优惠券余额,SUCCESS\n'
            '2026-10-03 08:00:00,优惠券,充值,0.66,券的授予,券的授予,优惠券余额,SUCCESS\n')
        self.assertEqual(total, Decimal('13.00'))
        self.assertEqual(audit.detail_rows_count, 2)

    def test_category_cannot_override_wrong_transaction_type(self):
        with self.assertRaises(DownloadError):
            self.parse('Date,Wallet,Segmentation,Amount,type\n2026-10-02,Coupon,Spend,10,Top up\n')

    def test_multiple_explicit_transaction_type_columns_are_ambiguous(self):
        with self.assertRaises(DownloadError):
            self.parse('Date,Wallet,Segmentation,Transaction Type,Amount\n2026-10-02,Coupon,Top up,Top up,10\n')

    def test_status_all_preserves_failed_and_pending_amounts(self):
        total, audit = self.parse('Date,Wallet,Type,Status,Amount\n2026-10-01,Coupon,Top up,Failed,20\n2026-10-02,Coupon,Top up,Pending,30\n')
        self.assertEqual(total, Decimal('50'))
        self.assertEqual(audit.detail_rows_count, 2)

    def test_unstable_timeout_never_returns_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'coupon.csv'
            p.write_text('Amount\n10\n')
            with self.assertRaises(DownloadError):
                wait_for_download(Path(td), {}, timeout=0.01, check_interval=0.02)


if __name__ == '__main__':
    unittest.main()
