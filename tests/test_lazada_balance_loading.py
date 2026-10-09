"""Regression cases for real Seller Center placeholder and changing balances."""
import unittest
from unittest.mock import Mock, patch

from backend.services import lazada_balance_collector as service


class Clock:
    def __init__(self):
        self.now = 0.0
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class LazadaLoadingTests(unittest.TestCase):
    def metric(self, field, value_at):
        clock = Clock()
        driver = Mock(current_url='https://sellercenter.lazada.co.id' + service.PATHS[field])
        def execute(script, selected):
            raw = value_at(clock.now)
            return {'located': raw is not None, 'text': raw or '', 'label': '可用余额'}
        driver.execute_script.side_effect = execute
        with patch.object(service.time, 'monotonic', clock.monotonic), patch.object(service.time, 'sleep', clock.sleep):
            value = service._metric(driver, field)
        return value, clock.now

    def test_ads_placeholder_zero_waits_for_real_value_and_full_stability(self):
        value, elapsed = self.metric('ads', lambda t: '印尼盾 0' if t < 2.5 else '印尼盾 120,948')
        self.assertEqual(value, '印尼盾 120,948')
        self.assertGreaterEqual(elapsed, 5.5)

    def test_undefined_and_temporarily_missing_card_are_not_money(self):
        value, elapsed = self.metric('income', lambda t: 'Rp undefined' if t < 1 else (None if t < 2 else 'Rp -3.01'))
        self.assertEqual(value, 'Rp -3.01')
        self.assertGreaterEqual(elapsed, 4)

    def test_confirmed_zero_remains_a_valid_amount(self):
        value, elapsed = self.metric('balance', lambda t: 'IDR 0.00')
        self.assertEqual(value, 'IDR 0.00')
        self.assertGreaterEqual(elapsed, 2)

    def test_continuously_changing_amount_is_rejected(self):
        with self.assertRaisesRegex(service.LazadaBalanceError, '持续变化'):
            self.metric('ads', lambda t: f'印尼盾 {int(t * 10)}')

    def test_foreign_or_missing_currency_is_not_inferred_from_country(self):
        for raw in ('USD 99.00', '$99.00', '99.00', 'MYR 99.00'):
            with self.subTest(raw=raw), self.assertRaises(service.LazadaBalanceError):
                service._currency_from_text(raw, 'ID')
        self.assertEqual(service._currency_from_text('印尼盾 120,948', 'ID'), 'IDR')


if __name__ == '__main__':
    unittest.main()
