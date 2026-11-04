from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from backend.services import temu_financial_gateway as gateway
from backend.services.temu_financial_controls import validate_inclusive_range


START, END = date(2026, 10, 1), date(2026, 10, 31)
SUBMITTED = datetime(2026, 11, 4, 10, 0, 0)
NOW = datetime(2026, 11, 4, 10, 0, 10)


def task_row(stamp='2026-11-04 10:00:01', **changes):
    row = {'generated': stamp, 'text': f'''账务明细生成时间 {stamp}
账务明细查询时间: 2026-10-01 00:00:00 ~ 2026-10-31 23:59:59
账务类型: 全部
资金流向: --
导出内容: 列表数据、财务明细''', 'downloads': [
        {'label': '下载账务明细(卖家中心)', 'selector': '#seller'},
        {'label': '下载财务明细（全球）', 'selector': '#global'},
        {'label': '下载财务明细(欧区)', 'selector': '#eu'},
        {'label': '下载财务明细(美国)', 'selector': '#us'}]}
    row.update(changes)
    return row


class FinancialTaskMatchingTests(TestCase):
    def test_only_new_card_and_exact_global_download_are_selected(self):
        old = task_row('2026-11-03 10:00:00')
        row = gateway.select_new_task([old, task_row()], {old['generated']}, SUBMITTED, NOW, START, END)
        self.assertEqual(row['global_download']['selector'], '#global')
        self.assertEqual(row['condition']['end_time'], '23:59:59')
        self.assertIsNone(gateway.select_new_task([old], {old['generated']}, SUBMITTED, NOW, START, END))

    def test_ambiguous_stale_future_and_incomplete_history_are_not_downloaded(self):
        for rows in ([task_row(), task_row('2026-11-04 10:00:02')],
                     [task_row('2026-11-03 10:00:00')], [task_row('2026-11-05 10:00:00')],
                     [task_row(), {'text': '正在生成'}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                gateway.select_new_task(rows, set(), SUBMITTED, NOW, START, END)
        self.assertIsNone(gateway.select_new_task([{'text': '正在生成'}], set(), SUBMITTED, NOW, START, END))

    def test_wrong_range_content_type_flow_and_partial_day_are_rejected(self):
        replacements = [('2026-10-31', '2026-10-30'), ('00:00:00', '01:00:00'),
                        ('23:59:59', '23:59:00'), ('账务类型: 全部', '账务类型: 支出'),
                        ('资金流向: --', '资金流向: 流出'), ('列表数据、财务明细', '列表数据')]
        for old, new in replacements:
            row = task_row()
            row['text'] = row['text'].replace(old, new)
            with self.subTest(new=new), self.assertRaises(ValueError):
                gateway.select_new_task([row], set(), SUBMITTED, NOW, START, END)

    def test_wrong_region_duplicate_or_disabled_global_never_return_a_file(self):
        row = task_row()
        row['downloads'] = [row['downloads'][2]]
        with self.assertRaisesRegex(ValueError, '非全球'):
            gateway.select_new_task([row], set(), SUBMITTED, NOW, START, END)
        row = task_row()
        row['downloads'].append({'label': gateway.GLOBAL_DOWNLOAD, 'selector': '#another'})
        with self.assertRaisesRegex(ValueError, '多个全球'):
            gateway.select_new_task([row], set(), SUBMITTED, NOW, START, END)
        row = task_row()
        row['downloads'][1]['disabled'] = True
        self.assertIsNone(gateway.select_new_task([row], set(), SUBMITTED, NOW, START, END))

    def test_date_readback_requires_requested_range_and_full_day(self):
        self.assertTrue(validate_inclusive_range('2026-10-01 ~ 2026-10-31', START, END))
        for value in ('2026-10-01 08:00:00 ~ 2026-10-31 23:59:59', '2026-10-01 ~ 2026-10-30'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_inclusive_range(value, START, END)


class FinancialGatewayTests(TestCase):
    def setUp(self):
        self.browser = SimpleNamespace(profile='p', auth_failed=False, cleanup_failed=False,
                                       open_store=Mock(), close_store=Mock())
        self.gateway = gateway.TemuFinancialGateway('p', browser=self.browser, timeout=0)
        self.store = {'store_id': '1', 'store_name': '示例-Seller', 'platform_name': 'TEMU'}
        self.gateway.current_stores = {'1': self.store}

    def test_combined_radio_is_selected_without_requiring_list_only_radio(self):
        selected = False
        def state(*_):
            return {'open': True, 'options': [
                {'text': '导出列表', 'selector': '#list', 'selected': not selected},
                {'text': '导出列表+账务详情', 'selector': '#details', 'selected': selected}]}
        def click(*args):
            nonlocal selected
            self.assertEqual(args[-1], '#details')
            selected = True
        with patch.object(self.gateway, '_open_export_modal'), patch.object(self.gateway, '_modal_state', side_effect=state), patch.object(gateway, 'click_control', side_effect=click) as clicked:
            self.gateway._select_export_options('1')
        clicked.assert_called_once()

    def test_export_uses_date_selection_query_and_only_matched_global_button(self):
        row = task_row()
        row['condition'] = gateway.parse_query_condition(row['text'], START, END)
        row['global_download'] = row['downloads'][1]
        with (patch.object(self.gateway, '_open_reconciliation', return_value=('Seller', {})),
              patch.object(self.gateway, '_reset_filters'), patch.object(self.gateway, '_ensure_filters'),
              patch.object(self.gateway, '_baseline', return_value=set()), patch.object(self.gateway, '_close_history'),
              patch.object(self.gateway, '_select_export_options'), patch.object(self.gateway, '_confirm_export'),
              patch.object(self.gateway, '_wait_for_new_task', return_value=row),
              patch.object(self.gateway, 'state', return_value={'sellers': ['Seller']}),
              patch.object(gateway, '_evaluate', return_value='2026-11-04 10:00:00'),
              patch.object(gateway, 'set_custom_range') as dates, patch.object(gateway, 'query_and_wait') as query,
              patch.object(gateway, 'download_xlsx', return_value=Path('global.xlsx')) as download):
            result = self.gateway.export(self.store, START.isoformat(), END.isoformat(), Path('.'))
        self.assertEqual(dates.call_args.args[2], '日期选择')
        query.assert_called_once()
        self.assertEqual(download.call_args.args[2], '#global')
        self.assertEqual(result['evidence']['download_label'], gateway.GLOBAL_DOWNLOAD)
        self.browser.close_store.assert_called_once_with('1')

    def test_identity_changes_and_login_or_verification_fail_explicitly(self):
        for state in ({'url': 'https://agentseller.temu.com/auth/login', 'found': {}},
                      {'url': gateway.BASE_URL, 'found': {}, 'verification': True}):
            with self.subTest(state=state), patch.object(gateway, '_evaluate', return_value=state), self.assertRaises(ValueError):
                self.gateway.state('1')
        with self.assertRaisesRegex(ValueError, '身份'):
            self.gateway.verify_seller(self.store, {'sellers': ['Sell']})
        self.assertEqual(self.gateway.verify_seller({'store_name': 'Seller'}, {'sellers': ['Seller']}), 'Seller')
        with self.assertRaisesRegex(ValueError, '变化'):
            self.gateway.export({**self.store, 'store_name': '另一店'}, START.isoformat(), END.isoformat(), Path('.'))
        self.browser.open_store.assert_not_called()

    def test_opened_store_closes_and_original_error_survives_cleanup_failure(self):
        with patch.object(self.gateway, '_open_reconciliation', side_effect=ValueError('页面错误')):
            with self.assertRaisesRegex(ValueError, '页面错误'):
                self.gateway.export(self.store, START.isoformat(), END.isoformat(), Path('.'))
        self.browser.close_store.assert_called_once_with('1')
        self.browser.close_store.side_effect = ValueError('关闭失败')
        with patch.object(self.gateway, '_open_reconciliation', side_effect=ValueError('页面错误')):
            with self.assertRaisesRegex(ValueError, '页面错误；清理店铺时：关闭失败'):
                self.gateway.export(self.store, START.isoformat(), END.isoformat(), Path('.'))

    def test_history_loading_is_not_an_empty_success(self):
        self.assertIsNone(self.gateway._history_loaded({'open': True, 'rows': [], 'empty': False}))
        self.assertIsNone(self.gateway._history_loaded({'open': True, 'rows': [task_row()], 'loading': True}))
        self.assertTrue(self.gateway._history_loaded({'open': True, 'rows': [], 'empty': True}))
