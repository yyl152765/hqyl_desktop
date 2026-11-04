import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from backend.services.temu_fee_refund_gateway import TemuFeeRefundGateway


class Browser:
    profile = 'profile'
    auth_failed = False
    cleanup_failed = False
    def __init__(self):
        self.closed = []
        self.opened = []
    def ready(self): pass
    def list_stores(self): return [{'store_id':'1', 'store_name':'运营-店铺', 'platform_name':'TEMU'}]
    def open_store(self, store, **kwargs): self.opened.append(store['store_id'])
    def close_store(self, identity): self.closed.append(identity)
    def evaluate(self, identity, script): return '2026-10-27 15:20:00'


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.browser = Browser()
        self.gateway = TemuFeeRefundGateway('profile', browser=self.browser)
        self.store = self.browser.list_stores()[0]
        self.gateway.preflight([self.store])
        self.gateway.timeout = 1

    @staticmethod
    def refund_row(identity, status='已完成', *, start='2026年9月1日', end='2026年9月30日', downloads=None):
        return {'text': f'{identity} {start} 0:00 CST ~ {end} 23:59 CST 2026年10月27日 15:20 CST {status}',
                'requested': '2026年10月27日 15:20 CST',
                'downloads': [f'#{identity}-download'] if downloads is None and status == '已完成' else (downloads or [])}

    def refund_states(self, before, following):
        """Keep the baseline stable until the one submission actually occurs."""
        self.submitted = False
        def state(*_args, **_kwargs):
            rows = before if not self.submitted else following.pop(0) if len(following) > 1 else following[0]
            return {'rows': rows, 'refund_history': True, 'refund_history_empty': not rows, 'busy': False, 'empty': not rows,
                    'history_empty': not rows, 'now': '2026-10-27 15:20:00'}
        def click(identity, label):
            self.assertEqual((identity, label), ('1', '确认导出'))
            self.assertFalse(self.submitted, 'must never resubmit while polling')
            self.submitted = True
        return state, click

    def test_invalid_range_fails_before_open(self):
        with self.assertRaisesRegex(ValueError, '开始日期'):
            self.gateway.export(self.store, 'refund', '2026-10-01', '2026-09-01', Path('.'))
        self.assertFalse(self.browser.opened)

    def test_identity_change_fails_before_open(self):
        with self.assertRaisesRegex(ValueError, '变化'):
            self.gateway.export({**self.store, 'store_name':'别的店'}, 'refund', '2026-09-01', '2026-09-30', Path('.'))
        self.assertFalse(self.browser.opened)

    def test_opened_store_closes_on_error(self):
        with patch.object(self.gateway, 'wait', side_effect=ValueError('page error')):
            with self.assertRaisesRegex(ValueError, 'page error'):
                self.gateway.export(self.store, 'shipping', '2026-09-01', '2026-09-30', Path('.'))
        self.assertEqual(self.browser.opened, ['1'])
        self.assertEqual(self.browser.closed, ['1'])

    def test_close_failure_does_not_hide_the_original_export_error(self):
        with (patch.object(self.gateway, 'wait', side_effect=ValueError('页面原因')),
              patch.object(self.browser, 'close_store', side_effect=ValueError('关闭原因'))):
            with self.assertRaisesRegex(ValueError, '页面原因；清理店铺时：关闭原因'):
                self.gateway.export(self.store, 'shipping', '2026-09-01', '2026-09-30', Path('.'))

    def test_task_date_and_identifier_matching(self):
        row = {'text':'EX-2197-0000003320F8 2026年9月1日 0:00 CST ~ 2026年9月30日 23:59 CST 已完成 下载'}
        self.assertEqual(self.gateway.task_id(row), 'EX-2197-0000003320F8')
        self.assertTrue(self.gateway.matching_dates(row, date(2026,9,1), date(2026,9,30)))
        self.assertFalse(self.gateway.matching_dates(row, date(2026,9,1), date(2026,9,29)))
        self.assertFalse(self.gateway.matching_dates({'text':'2026年2月31日 ~ 2026年9月30日'}, date(2026,9,1), date(2026,9,30)))

    def test_multiple_new_tasks_refused_before_download(self):
        state, click = self.refund_states([], [[{'text':'EX-1-A'}, {'text':'EX-1-B'}]])
        with patch.object(self.gateway, 'state', side_effect=state), patch.object(self.gateway, 'click', side_effect=click):
            with self.assertRaisesRegex(ValueError, '多个新导出任务'):
                self.gateway.export_refund('1', Path('.'), date(2026,9,1), date(2026,9,30), 'seller')

    def test_refund_waits_for_new_task_and_downloads_only_its_completed_row(self):
        previous = self.refund_row('EX-1-OLD')
        queued = self.refund_row('EX-1-NEW', '处理中')
        completed = self.refund_row('EX-1-NEW')
        state, click = self.refund_states([previous], [[previous], [queued, previous], [completed, previous]])
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / '售后单.xlsx'
            with (patch.object(self.gateway, 'state', side_effect=state),
                  patch.object(self.gateway, 'click', side_effect=click) as submitted,
                  patch('backend.services.temu_fee_refund_gateway.download_xlsx', return_value=destination) as download,
                  patch('backend.services.temu_fee_refund_gateway.validate_refund_file', return_value=7) as validate):
                result = self.gateway.export_refund('1', Path(temp), date(2026,9,1), date(2026,9,30), 'seller')
        submitted.assert_called_once_with('1', '确认导出')
        self.assertEqual(download.call_args.args[2], '#EX-1-NEW-download')
        validate.assert_called_once_with(destination, date(2026,9,1), date(2026,9,30))
        self.assertEqual(result['row_count'], 7)
        self.assertEqual(result['evidence']['task_id'], 'EX-1-NEW')
        self.assertEqual((result['evidence']['type'], result['evidence']['status']), ('退货退款', '已退款'))

    def test_refund_no_data_requires_the_new_task_and_never_downloads_an_old_file(self):
        previous = self.refund_row('EX-1-OLD')
        empty = self.refund_row('EX-1-NEW', '无数据')
        state, click = self.refund_states([previous], [[empty, previous]])
        with (patch.object(self.gateway, 'state', side_effect=state),
              patch.object(self.gateway, 'click', side_effect=click),
              patch('backend.services.temu_fee_refund_gateway.download_xlsx') as download):
            result = self.gateway.export_refund('1', Path('.'), date(2026,9,1), date(2026,9,30), 'seller')
        download.assert_not_called()
        self.assertIs(result['no_data'], True)
        self.assertEqual(result['row_count'], 0)
        self.assertEqual(result['evidence']['task_id'], 'EX-1-NEW')

    def test_refund_wrong_range_failure_and_ambiguous_download_never_download(self):
        previous = self.refund_row('EX-1-OLD')
        cases = [
            (self.refund_row('EX-1-NEW', end='2026年10月1日'), '日期'),
            (self.refund_row('EX-1-NEW', '导出失败'), '失败'),
            (self.refund_row('EX-1-NEW', downloads=['#one', '#two']), '下载按钮'),
        ]
        for row, message in cases:
            with self.subTest(message=message):
                state, click = self.refund_states([previous], [[row, previous]])
                with (patch.object(self.gateway, 'state', side_effect=state),
                      patch.object(self.gateway, 'click', side_effect=click),
                      patch('backend.services.temu_fee_refund_gateway.download_xlsx') as download):
                    with self.assertRaisesRegex(ValueError, message):
                        self.gateway.export_refund('1', Path('.'), date(2026,9,1), date(2026,9,30), 'seller')
                download.assert_not_called()

    def test_refund_timeout_never_reuses_the_preexisting_completed_task(self):
        previous = self.refund_row('EX-1-OLD')
        state, click = self.refund_states([previous], [[previous]])
        self.gateway.timeout = 0
        with (patch.object(self.gateway, 'state', side_effect=state),
              patch.object(self.gateway, 'click', side_effect=click) as submitted,
              patch('backend.services.temu_fee_refund_gateway.download_xlsx') as download):
            with self.assertRaisesRegex(ValueError, '未能确认'):
                self.gateway.export_refund('1', Path('.'), date(2026,9,1), date(2026,9,30), 'seller')
        submitted.assert_called_once_with('1', '确认导出')
        download.assert_not_called()

    def test_refund_validation_failure_cannot_become_success(self):
        previous = self.refund_row('EX-1-OLD')
        completed = self.refund_row('EX-1-NEW')
        state, click = self.refund_states([previous], [[completed, previous]])
        with (patch.object(self.gateway, 'state', side_effect=state),
              patch.object(self.gateway, 'click', side_effect=click),
              patch('backend.services.temu_fee_refund_gateway.download_xlsx', return_value=Path('wrong.xlsx')),
              patch('backend.services.temu_fee_refund_gateway.validate_refund_file', side_effect=ValueError('错误售后状态'))):
            with self.assertRaisesRegex(ValueError, '错误售后状态'):
                self.gateway.export_refund('1', Path('.'), date(2026,9,1), date(2026,9,30), 'seller')

    def test_delayed_history_is_loaded_before_submitting_and_old_no_data_is_not_reused(self):
        old = self.refund_row('EX-1-OLD', '无数据')
        observations = 0
        submitted = False
        def state(*_args, **_kwargs):
            nonlocal observations
            observations += 1
            # The drawer is visible before its old export records arrive.
            rows = [] if observations == 1 else [old]
            return {'rows': rows, 'refund_history': True, 'refund_history_empty': False, 'busy': False}
        def click(identity, label):
            nonlocal submitted
            self.assertGreaterEqual(observations, 2, 'must wait for actual export history before submitting')
            self.assertFalse(submitted)
            submitted = True
        self.gateway.timeout = 0
        with (patch.object(self.gateway, 'state', side_effect=state),
              patch.object(self.gateway, 'click', side_effect=click),
              patch('backend.services.temu_fee_refund_gateway.download_xlsx') as download):
            with self.assertRaisesRegex(ValueError, '未能确认'):
                self.gateway.export_refund('1', Path('.'), date(2026,9,1), date(2026,9,30), 'seller')
        self.assertTrue(submitted)
        download.assert_not_called()

    def test_refund_unseen_old_or_future_or_missing_request_time_is_rejected(self):
        previous = self.refund_row('EX-1-OLD')
        for timestamp in ('2026年10月26日 15:20 CST', '2026年10月28日 15:20 CST', ''):
            with self.subTest(timestamp=timestamp):
                candidate = {**self.refund_row('EX-1-NEW', '无数据'), 'requested': timestamp}
                state, click = self.refund_states([previous], [[candidate, previous]])
                with (patch.object(self.gateway, 'state', side_effect=state),
                      patch.object(self.gateway, 'click', side_effect=click),
                      patch('backend.services.temu_fee_refund_gateway.download_xlsx') as download):
                    with self.assertRaisesRegex(ValueError, '时间'):
                        self.gateway.export_refund('1', Path('.'), date(2026,9,1), date(2026,9,30), 'seller')
                download.assert_not_called()


if __name__ == '__main__': unittest.main()
