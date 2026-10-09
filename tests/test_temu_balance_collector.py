import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.services import temu_balance_collector as collector


def state(field='pending', amount='$\n1,270.99'):
    return {'url': collector.SETTLEMENT_URL if field == 'pending' else collector.FUNDS_URL,
            'usd': True, 'region': '全球' if field == 'pending' else '商家中心',
            'hasLoanAccount': True, 'hasOrderTime': True, 'busy': False, 'errors': [],
            'cards': [{'label': '待处理款项总额' if field == 'pending' else '总金额',
                       'amount': amount, 'selector': '#actual-card', 'capturable': True}],
            'tabs': [{'text': '待处理款项', 'selected': True}],
            'range': '2026-08-01 ~ 2026-08-31', 'rangeCapturable': True, 'extraFilters': ['', ''], 'updateNotice': '数据更新时间：2026-09-28'}


class TemuBalanceCollectorTests(unittest.TestCase):
    def test_usd_zero_negative_and_thousands(self):
        for raw, expected in [('$\n3,379.71', '3379.71'), ('$\n0.00', '0.00'),
                              ('-$2.50', '-2.50'), ('($1,234.50)', '-1234.50')]:
            self.assertEqual(collector.parse_usd_amount(raw), expected)
        for raw in ['', '--', '***', '$1.234,56', '$1,23', '$NaN', '$1.00 $2.00']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                collector.parse_usd_amount(raw)

    def test_current_total_uses_total_card_not_available_or_reserve(self):
        page = state('total', '$\n3,379.71')
        page['cards'] += [{'label': '可用余额', 'amount': '$0.00'}, {'label': '账户预留金额', 'amount': '$2,000.00'}]
        self.assertEqual(collector.metric_from_state(page, 'total')[0], '3379.71')

    def test_pending_rejects_wrong_tab_region_currency_and_route(self):
        for update in [{'usd': False}, {'region': '美国'}, {'tabs': [{'text': '已到账款项', 'selected': True}]},
                       {'hasOrderTime': False}, {'url': collector.FUNDS_URL}, {'busy': True},
                       {'errors': ['查询失败']}, {'cards': []}]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                collector.metric_from_state({**state(), **update}, 'pending')

    def test_duplicate_or_masked_card_rejected(self):
        duplicate = state()
        duplicate['cards'] *= 2
        with self.assertRaises(ValueError):
            collector.metric_from_state(duplicate, 'pending')
        with self.assertRaises(ValueError):
            collector.metric_from_state(state(amount='***'), 'pending')

    def _run(self, *, query_error=None, screenshot_error=None, after=None, page_states=None):
        pending, total = state(), state('total', '$3,379.71')
        driver = Mock(current_url=collector.FUNDS_URL)
        driver.execute_script.side_effect = page_states or [pending, pending, pending, after or pending, total]
        selection = {'range_value': pending['range'], 'start_date': '2026-08-01', 'end_date': '2026-08-31'}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(collector, '_business_page', side_effect=[pending, total]), \
                patch.object(collector, '_dismiss_tour'), \
                patch.object(collector, 'dismiss_temu_message_panel', return_value=False), \
                patch.object(collector, '_click_unique'), \
                patch.object(collector, 'set_calendar_month', return_value=selection), \
                patch.object(collector, 'query_and_wait_for_refresh', side_effect=query_error), \
                patch.object(collector, 'capture_balance_evidence', side_effect=screenshot_error,
                             return_value=str(Path(directory) / 'proof.png')):
            return collector.collect_temu_balance(driver, {'store_name': '指定店铺'}, '2026-08', Path(directory))

    def test_collects_current_total_and_monthly_pending(self):
        result = self._run()
        self.assertEqual(result['values'], {'total': '3379.71', 'pending': '1270.99'})
        self.assertEqual(result['field_errors'], {})
        self.assertEqual(set(result['evidence']), {'total', 'pending'})
        self.assertIn('2026-08-01', result['notes'][0])

    def test_query_failure_never_reads_old_pending_amount(self):
        pending, total = state(), state('total', '$3,379.71')
        driver = Mock(current_url=collector.FUNDS_URL)
        driver.execute_script.side_effect = [pending, pending, total]
        with tempfile.TemporaryDirectory() as directory, patch.object(collector, '_business_page', side_effect=[pending, total]), \
                patch.object(collector, '_dismiss_tour'), \
                patch.object(collector, 'dismiss_temu_message_panel', return_value=False), \
                patch.object(collector, '_click_unique'), \
                patch.object(collector, 'set_calendar_month', return_value={'range_value': pending['range']}), \
                patch.object(collector, 'query_and_wait_for_refresh', side_effect=ValueError('查询无刷新确认')), \
                patch.object(collector, 'capture_balance_evidence', return_value='proof.png'):
            result = collector.collect_temu_balance(driver, {'store_name': '指定店铺'}, '2026-08', Path(directory))
        self.assertIsNone(result['values']['pending'])
        self.assertEqual(result['values']['total'], '3379.71')
        self.assertIn('pending', result['field_errors'])

    def test_screenshot_failure_preserves_value_and_marks_field(self):
        result = self._run(screenshot_error=ValueError('窗口已最小化'))
        self.assertEqual(result['values']['pending'], '1270.99')
        self.assertIn('pending', result['field_errors'])
        self.assertNotIn('pending', result['evidence'])

    def test_value_changed_during_capture_invalidates_evidence(self):
        result = self._run(after=state(amount='$99.00'))
        self.assertIn('pending', result['field_errors'])
        self.assertNotIn('pending', result['evidence'])

    def test_query_requires_both_po_and_sku_filters_to_remain_empty(self):
        for filters in ([], [''], ['', '', ''], ['PO123', '']):
            with self.subTest(filters=filters):
                pending, total = state(), state('total', '$3,379.71')
                post_query = {**pending, 'extraFilters': filters}
                result = self._run(page_states=[pending, pending, post_query, total])
                self.assertIsNone(result['values']['pending'])
                self.assertIn('pending', result['field_errors'])
                self.assertNotIn('pending', result['evidence'])
                self.assertEqual(result['values']['total'], '3379.71')
                self.assertNotIn('total', result['field_errors'])

    def test_filter_change_during_capture_invalidates_same_amount_evidence(self):
        for filters in ([], ['PO123', ''], ['', 'SKU123']):
            with self.subTest(filters=filters):
                result = self._run(after={**state(), 'extraFilters': filters})
                self.assertEqual(result['values']['pending'], '1270.99')
                self.assertIn('pending', result['field_errors'])
                self.assertNotIn('pending', result['evidence'])
                self.assertEqual(result['values']['total'], '3379.71')
                self.assertNotIn('total', result['field_errors'])

    def test_page_selection_inspects_original_tab_instead_of_login_popup(self):
        driver = Mock()
        driver.current_window_handle = 'login'
        driver.window_handles = ['login', 'business']
        selected = {'handle': 'login'}
        driver.switch_to.window.side_effect = lambda handle: selected.update(handle=handle)
        class TabDriver:
            get = driver.get
            switch_to = driver.switch_to
            window_handles = driver.window_handles
            current_window_handle = 'login'
            execute_script = Mock(return_value=state('total'))
            @property
            def current_url(self):
                return collector.FUNDS_URL if selected['handle'] == 'business' else 'https://seller.kuajingmaihuo.com/login'
        with patch.object(collector.time, 'sleep'):
            result = collector._business_page(TabDriver(), collector.FUNDS_URL, 'total', timeout=.1, settle_seconds=0)
        self.assertEqual(result['cards'][0]['label'], '总金额')
        self.assertEqual(selected['handle'], 'business')


if __name__ == '__main__':
    unittest.main()
