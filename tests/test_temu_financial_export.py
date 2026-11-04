import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from openpyxl import load_workbook

from backend.services import temu_financial_export as service
from tests.test_temu_financial_workbook import make_workbook


class Gateway:
    profile = 'fixture-profile'
    halted = False

    def __init__(self, failed=()):
        self.failed = set(failed)

    def preflight(self, stores):
        pass

    def export(self, store, start, end, raw_dir):
        if store['store_id'] in self.failed:
            raise ValueError('平台导出失败')
        source = make_workbook(raw_dir / 'global.xlsx', {
            '账务列表': [['订单号', '金额'], ['000' + store['store_id'], -1.25]],
            '财务明细': [['编号', '备注'], [store['store_id'], '财务详情']]})
        return {'path': source, 'evidence': {'task_generated_at': '2026-11-04 10:00:00'}}


class FinancialExportTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stores = [{'store_id': '1', 'store_name': '紫鸟店铺甲', 'platform_name': 'TEMU'},
                       {'store_id': '2', 'store_name': '紫鸟店铺乙', 'platform_name': 'TEMU'}]

    def batch(self):
        return service.create_batch(self.root, Gateway.profile, self.stores, '2026-10-01', '2026-10-31')

    def test_full_batch_preserves_raw_and_groups_all_sheets(self):
        path = self.batch()
        result = service.run_batch(path, Gateway())
        self.assertTrue(result['is_complete'], result)
        self.assertEqual(result['merged']['row_counts'], {'账务列表': 2, '财务明细': 2})
        self.assertEqual(result['success_count'], 2)
        self.assertEqual(len(result['output_files']), 3)
        self.assertTrue(all(Path(s['raw_file']).is_file() and Path(s['result_file']).is_file() for s in result['stores']))
        record = service.load_batch(path, Gateway.profile)
        before = copy.deepcopy(record)
        service.public_result(path, record)
        self.assertEqual(record, before)
        with self.assertRaisesRegex(ValueError, '已经运行'):
            service.run_batch(path, Gateway())

    def test_failed_store_never_leaks_into_merge_and_partial_name_is_explicit(self):
        result = service.run_batch(self.batch(), Gateway(failed={'2'}))
        self.assertFalse(result['is_complete'])
        self.assertEqual(result['failed_count'], 1)
        self.assertEqual(result['merged']['status'], 'partial')
        self.assertIn('不完整', result['merged']['file'])
        book = load_workbook(result['merged']['file'])
        try:
            self.assertTrue(all(list(s.values)[1][0] == '紫鸟店铺甲' and s.max_row == 2 for s in book.worksheets))
        finally:
            book.close()

    def test_all_failed_and_preflight_failure_publish_no_merge(self):
        result = service.run_batch(self.batch(), Gateway(failed={'1', '2'}))
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result.get('merged', {}).get('file'))
        gateway = Gateway()
        with patch.object(gateway, 'preflight', side_effect=ValueError('登录失效')):
            result = service.run_batch(self.batch(), gateway)
        self.assertTrue(all(s['status'] == 'failed' for s in result['stores']))
        self.assertIn('登录失效', result['error'])

    def test_finalization_marks_unprepared_downloads_failed(self):
        path = self.batch()
        record = service.load_batch(path)
        record['stores'][0]['status'] = 'success'
        service.write_json(path, record)
        result = service.fail_batch(path, Gateway.profile, '任务中断')
        self.assertEqual(result['success_count'], 0)
        self.assertEqual(result['failed_count'], 2)

    def test_account_path_and_store_resolution_are_confined(self):
        path = self.batch()
        with self.assertRaisesRegex(ValueError, '账号'):
            service.load_batch(path, 'another-account')
        record = service.load_batch(path)
        record['stores'][0]['raw_file'] = '../outside.xlsx'
        service.write_json(path, record)
        with self.assertRaisesRegex(ValueError, '超出'):
            service.load_batch(path)
        self.assertTrue(service.match_stores(['店铺甲'], self.stores)['can_start'])
        self.assertFalse(service.match_stores(['店铺'], self.stores)['can_start'])
        self.assertFalse(service.match_stores(['店铺甲', '紫鸟店铺甲'], self.stores)['can_start'])
        self.assertFalse(service.match_stores(['店铺甲'], [{**self.stores[0], 'platform_name': 'Lazada'}])['can_start'])

    def test_invalid_date_empty_and_duplicate_names_are_rejected(self):
        payload = {'store_names': '店铺甲', 'start_date': '2026-10-01', 'end_date': '2026-10-31'}
        for changes in ({'store_names': ''}, {'store_names': '店铺甲\n店铺甲'},
                        {'start_date': '2026-11-01'}, {'end_date': 'invalid'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                service.validate_request({**payload, **changes})
