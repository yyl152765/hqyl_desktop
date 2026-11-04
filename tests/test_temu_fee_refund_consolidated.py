from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from backend.services.temu_fee_refund_consolidated import HEADERS, SHEETS, create_consolidated_workbook, period_label
from backend.services import temu_fee_refund_export as service


def source_headers(kind):
    return ['运营名', '店铺名'] + HEADERS[kind][3 if kind == 'refund' else 2:]


def source_record(root, store, kind, values=None, header=None):
    path = root / 'results' / (store['store_id'] + '_' + kind + '.xlsx')
    header = header or source_headers(kind)
    values = values or [store['operator_name'], store['store_name']] + ['001234567890123456789'] * (len(header) - 2)
    book = Workbook()
    book.active.append(header)
    book.active.append(values)
    # A literal leading equals sign must remain literal, never a formula.
    for cell in book.active[2]:
        if isinstance(cell.value, str):
            cell.data_type = 's'
    book.save(path)
    book.close()
    store['exports'][kind] = {'status': 'success', 'row_count': 1, 'file': str(path.relative_to(root))}
    return path


def make_batch(root, count=1):
    (root / 'results').mkdir()
    return {'start_date': '2026-09-01', 'end_date': '2026-09-30', 'stores': [
        {'store_id': str(index), 'store_name': f'甲-店{index}', 'operator_name': '甲', 'status': 'success',
         'exports': {kind: {'status': 'no_data', 'row_count': 0} for kind in SHEETS}}
        for index in range(count)]}


class ConsolidatedTests(TestCase):
    def test_reorders_named_columns_appends_stores_preserves_types_and_extra_columns(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = make_batch(root, 2)
            originals = {}
            for store in record['stores']:
                for kind in SHEETS:
                    headers = source_headers(kind)
                    values = [store['operator_name'], store['store_name']] + ['001234567890123456789'] * (len(headers) - 2)
                    values[-1] = -1.06 if kind != 'refund' else 'RM8.93'
                    if store['store_id'] == '1':
                        headers, values = list(reversed(headers)), list(reversed(values))
                        headers += ['额外列 ']
                        values += ['=literal']
                    source = source_record(root, store, kind, values, headers)
                    originals[source] = source.read_bytes()
            before = deepcopy(record)
            result = create_consolidated_workbook(root / 'manifest.json', record)
            self.assertEqual(record, before)
            self.assertEqual(result['status'], 'success')
            self.assertEqual(result['row_counts'], {kind: 2 for kind in SHEETS})
            book = load_workbook(root / result['file'], read_only=False)
            self.addCleanup(book.close)
            self.assertEqual(book.sheetnames, list(SHEETS.values()))
            for kind, title in SHEETS.items():
                sheet = book[title]
                self.assertEqual([c.value for c in sheet[1]], HEADERS[kind] + ['额外列'])
                offset = 1 if kind == 'refund' else 0
                self.assertEqual(sheet.cell(2, 1 + offset).value, '甲')
                self.assertEqual(sheet.cell(3, 2 + offset).value, '甲-店1')
                self.assertEqual(sheet.cell(2, 3 + offset).value, '001234567890123456789')
                self.assertEqual(sheet.cell(3, len(HEADERS[kind]) + 1).value, '=literal')
                self.assertEqual(sheet.cell(3, len(HEADERS[kind]) + 1).data_type, 's')
                self.assertEqual(sheet.max_row, 3)
                self.assertTrue(sheet.auto_filter.ref)
            self.assertEqual(book['退货退款']['A2'].value, '9月')
            self.assertTrue(all(path.read_bytes() == original for path, original in originals.items()))

    def test_all_no_data_has_three_header_only_sheets_and_failed_only_has_no_output(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = make_batch(root)
            result = create_consolidated_workbook(root / 'manifest.json', record)
            book = load_workbook(root / result['file'])
            self.addCleanup(book.close)
            self.assertEqual(result['row_counts'], {kind: 0 for kind in SHEETS})
            self.assertTrue(all(sheet.max_row == 1 for sheet in book.worksheets))
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = make_batch(root)
            for item in record['stores'][0]['exports'].values():
                item['status'] = 'failed'
            with self.assertRaisesRegex(ValueError, '没有已完成'):
                create_consolidated_workbook(root / 'manifest.json', record)
            self.assertEqual(list((root / 'results').iterdir()), [])

    def test_partial_is_labeled_and_never_overwrites(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = make_batch(root)
            source_record(root, record['stores'][0], 'shipping')
            record['stores'][0]['exports']['refund'] = {'status': 'failed', 'error': '平台错误'}
            result = create_consolidated_workbook(root / 'manifest.json', record)
            self.assertEqual(result['status'], 'partial')
            self.assertIn('部分完成', result['file'])
            path = root / result['file']
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, '已存在'):
                create_consolidated_workbook(root / 'manifest.json', record)
            self.assertEqual(path.read_bytes(), before)

    def test_bad_headers_identity_count_and_paths_refuse_without_publishing(self):
        for issue in ('missing', 'duplicate', 'identity', 'count', 'path'):
            with self.subTest(issue=issue), TemporaryDirectory() as tmp:
                root = Path(tmp)
                record = make_batch(root)
                store = record['stores'][0]
                headers = source_headers('shipping')
                if issue == 'missing': headers[-1] = '错字段'
                if issue == 'duplicate': headers[-1] = headers[-2] + ' '
                values = [store['operator_name'], store['store_name']] + ['001'] * (len(headers) - 2)
                if issue == 'identity': values[1] = '另一家店'
                source = source_record(root, store, 'shipping', values, headers)
                if issue == 'count': store['exports']['shipping']['row_count'] = 2
                if issue == 'path': store['exports']['shipping']['file'] = '../outside.xlsx'
                with self.assertRaises(ValueError):
                    create_consolidated_workbook(root / 'manifest.json', record)
                self.assertEqual(list((root / 'results').iterdir()), [source])

    def test_period_uses_selected_reporting_range_and_excel_limit_is_checked(self):
        self.assertEqual(period_label('2026-09-02', '2026-09-30'), '9月')
        self.assertEqual(period_label('2025-09-01', '2026-09-01'), '2025-09-01~2026-09-01')
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = make_batch(root, 2)
            for store in record['stores']: source_record(root, store, 'shipping')
            with patch('backend.services.temu_fee_refund_consolidated.MAX_DATA_ROWS', 1):
                with self.assertRaisesRegex(ValueError, '行数上限'):
                    create_consolidated_workbook(root / 'manifest.json', record)

    def test_more_than_legacy_xls_row_limit_is_preserved(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = make_batch(root)
            store = record['stores'][0]
            source = root / 'results/source.xlsx'
            book = Workbook(write_only=True)
            sheet = book.create_sheet('明细')
            sheet.append(source_headers('shipping'))
            count = 65_537
            for index in range(count):
                sheet.append(['甲', store['store_name'], f'000{index}'])
            book.save(source)
            book.close()
            store['exports']['shipping'] = {'status':'success', 'row_count':count, 'file':'results/source.xlsx'}
            result = create_consolidated_workbook(root / 'manifest.json', record)
            book = load_workbook(root / result['file'], read_only=True)
            try:
                rows = book['发货面单费'].iter_rows(values_only=True)
                next(rows)
                actual, last = 0, None
                for last in rows: actual += 1
            finally:
                book.close()
            self.assertEqual(actual, count)
            self.assertEqual(last[2], f'000{count - 1}')

    def test_consolidated_manifest_path_confined_and_public_result_does_not_mutate(self):
        with TemporaryDirectory() as tmp:
            path = service.create_batch(Path(tmp), 'p', [], [], '2026-09-01', '2026-09-30')
            record = service.load_batch(path, 'p')
            record['consolidated'] = {'status': 'success', 'file': 'results/report.xlsx', 'row_counts': {}}
            before = deepcopy(record)
            public = service.public_result(path, record)
            self.assertEqual(record, before)
            self.assertEqual(public['output_files'][0], str(path.parent / 'results/report.xlsx'))
            record['consolidated']['file'] = '../outside.xlsx'
            service.write_json(path, record)
            with self.assertRaisesRegex(ValueError, '超出'):
                service.load_batch(path, 'p')
