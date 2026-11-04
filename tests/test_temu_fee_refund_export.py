from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from openpyxl import Workbook, load_workbook
from backend.services import temu_fee_refund_export as service
from backend.services.temu_fee_refund_consolidated import HEADERS

class FeeRefundBatchTests(TestCase):
    def test_validation_rejects_invalid_dates_or_duplicate_names(self):
        base = {'store_names':'店A\n店B','start_date':'2026-09-01','end_date':'2026-09-30'}
        self.assertEqual(service.validate_request(base)[0], ['店A','店B'])
        for values in ({'store_names':'店A\n店A'},
                       {'start_date':'2026-10-01'}, {'end_date':'invalid'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                service.validate_request({**base, **values})

    def test_match_uses_complete_shop_name_and_its_operator(self):
        catalog = [
            {'store_id':'1', 'store_name':'袁晶-全站点-马来001-躍飛-buao', 'platform_name':'TEMU'},
            {'store_id':'2', 'store_name':'袁晶-泰国026-信橋-staceyose', 'platform_name':'TEMU'},
            {'store_id':'3', 'store_name':'李四-泰国026-另一个', 'platform_name':'TEMU'},
        ]
        result = service.match_stores(['buao', '袁晶-泰国026-信橋-staceyose'], catalog)
        self.assertTrue(result['can_start'])
        self.assertEqual([(s['store_name'], s['operator_name']) for s in result['stores']],
                         [(catalog[0]['store_name'], '袁晶'), (catalog[1]['store_name'], '袁晶')])
        for names in (['泰国026'], ['buao', catalog[0]['store_name']], ['无此店']):
            self.assertFalse(service.match_stores(names, catalog)['can_start'])
        self.assertFalse(service.match_stores(['bad'], [{'store_id':'4', 'store_name':'bad', 'platform_name':'TEMU'}])['can_start'])
        self.assertFalse(service.match_stores(['abc'], [{'store_id':'4', 'store_name':'王-abc', 'platform_name':'Shopee'}])['can_start'])

    def test_batch_attributes_each_file_and_does_not_hide_partial_failure(self):
        with TemporaryDirectory() as tmp:
            path = service.create_batch(Path(tmp), 'profile', [{'store_id':'1','store_name':'店A','platform_name':'TEMU'}], ['甲'], '2026-09-01', '2026-09-30')
            class Gateway:
                profile = 'profile'
                halted = False
                def preflight(self, stores): pass
                def export(self, store, kind, start, end, directory):
                    if kind == 'return_label': raise ValueError('未确认导出任务')
                    headers = HEADERS[kind][3 if kind == 'refund' else 2:]
                    workbook = Workbook(); workbook.active.append(headers); workbook.active.append(['001'] * len(headers))
                    source = directory / 'raw.xlsx'; workbook.save(source); workbook.close()
                    return {'path': str(source), 'row_count': 1, 'evidence': {'task_id': f'TASK-{kind}'}}
            result = service.run_batch(path, Gateway(), lambda _: None)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(len(result['output_files']), 3)
            self.assertEqual(result['consolidated']['status'], 'partial')
            self.assertIn('部分完成', result['output_files'][0])
            self.assertIn('未确认导出任务', result['stores'][0]['error'])
            book = load_workbook(result['output_files'][0]); self.addCleanup(book.close)
            self.assertEqual([book.active.cell(2, col).value for col in range(1,4)], ['甲','店A','001'])
            self.assertEqual(service.load_batch(path, 'profile')['status'], 'failed')
            self.assertEqual(result['stores'][0]['exports']['shipping']['evidence']['task_id'], 'TASK-shipping')

    def test_success_requires_combined_report_and_merge_error_preserves_individual_files(self):
        for merge_error in (False, True):
            with self.subTest(merge_error=merge_error), TemporaryDirectory() as tmp:
                path = service.create_batch(Path(tmp), 'profile', [{'store_id':'1','store_name':'甲-店A','platform_name':'TEMU'}], ['甲'], '2026-09-01', '2026-09-30')
                class Gateway:
                    profile = 'profile'
                    halted = False
                    def preflight(self, stores): pass
                    def export(self, store, kind, start, end, directory):
                        if kind != 'shipping':
                            return {'no_data': True, 'row_count': 0, 'evidence': {'task_id': kind}}
                        headers = HEADERS[kind][2:]
                        workbook = Workbook(); workbook.active.append(headers); workbook.active.append(['001'] * len(headers))
                        source = directory / 'raw.xlsx'; workbook.save(source); workbook.close()
                        return {'path': str(source), 'row_count': 1, 'evidence': {'task_id': kind}}
                if merge_error:
                    with patch('backend.services.temu_fee_refund_consolidated.create_consolidated_workbook', side_effect=ValueError('合并校验失败')):
                        result = service.run_batch(path, Gateway(), lambda _: None)
                    self.assertFalse(result['is_complete'])
                    self.assertEqual(result['consolidated']['status'], 'failed')
                    self.assertIn('合并校验失败', result['completion_message'])
                    self.assertEqual(len(result['output_files']), 1)
                    self.assertTrue(Path(result['output_files'][0]).is_file())
                else:
                    result = service.run_batch(path, Gateway(), lambda _: None)
                    self.assertTrue(result['is_complete'])
                    self.assertEqual(result['consolidated']['status'], 'success')
                    self.assertEqual(result['consolidated']['row_counts'], {'shipping':1,'return_label':0,'refund':0})
                    self.assertEqual(len(result['output_files']), 2)

    def test_row_count_mismatch_preserves_raw_but_does_not_publish_file(self):
        with TemporaryDirectory() as tmp:
            path = service.create_batch(Path(tmp), 'profile', [{'store_id':'1','store_name':'店A','platform_name':'TEMU'}], ['甲'], '2026-09-01', '2026-09-30')
            class Gateway:
                profile = 'profile'
                halted = False
                def preflight(self, stores): pass
                def export(self, store, kind, start, end, directory):
                    source = directory / 'raw.csv'; source.write_text('订单号\n001\n', encoding='utf-8')
                    return {'path': str(source), 'row_count': 2, 'evidence': {'task_id': kind}}
            result = service.run_batch(path, Gateway(), lambda _: None)
            self.assertFalse(result['is_complete'])
            self.assertEqual(result['output_files'], [])
            self.assertEqual(list((path.parent / 'results').iterdir()), [])
            for item in result['stores'][0]['exports'].values():
                self.assertIn('不一致', item['error'])
                self.assertTrue((path.parent / item['raw_file']).is_file())

    def test_unexpected_progress_error_finalizes_pending_items(self):
        with TemporaryDirectory() as tmp:
            path = service.create_batch(Path(tmp), 'profile', [{'store_id':'1','store_name':'店A','platform_name':'TEMU'}], ['甲'], '2026-09-01', '2026-09-30')
            class Gateway:
                profile = 'profile'
                def preflight(self, stores): pass
            def progress(_): raise RuntimeError('日志写入失败')
            result = service.run_batch(path, Gateway(), progress)
            self.assertEqual(result['status'], 'failed')
            self.assertTrue(all(item['status'] == 'failed' for item in result['stores'][0]['exports'].values()))
            self.assertIn('日志写入失败', service.load_batch(path, 'profile')['error'])

    def test_manifest_cannot_reference_another_batch_output(self):
        with TemporaryDirectory() as tmp:
            path = service.create_batch(Path(tmp), 'profile', [{'store_id':'1','store_name':'店A','platform_name':'TEMU'}], ['甲'], '2026-09-01', '2026-09-30')
            record = service.load_batch(path, 'profile')
            record['stores'][0]['exports']['shipping']['file'] = '../other/result.xlsx'
            service.write_json(path, record)
            with self.assertRaisesRegex(ValueError, '超出'):
                service.load_batch(path, 'profile')
