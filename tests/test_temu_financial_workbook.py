from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from backend.services import temu_financial_workbook as workbooks


def make_workbook(path, sheets):
    book = Workbook()
    book.remove(book.active)
    for title, rows in sheets.items():
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
        if sheet.max_row > 1 and sheet.max_column > 1:
            sheet.cell(2, 2).number_format = '#,##0.00'
    book.save(path)
    book.close()
    return path


class FinancialWorkbookTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'results').mkdir()

    def prepare(self, specs):
        raw = []
        infos = []
        for index, (name, sheets) in enumerate(specs):
            source = make_workbook(self.root / f'raw{index}.xlsx', sheets)
            raw.append((source, source.read_bytes()))
            infos.append(workbooks.inspect_workbook(source))
        mapping = workbooks.build_header_mapping({s.title: s.headers for s in info.sheets} for info in infos)
        entries = []
        for index, ((name, _), info) in enumerate(zip(specs, infos)):
            destination = self.root / 'results' / f'shop{index}.xlsx'
            result = workbooks.enrich_store(info.path, destination, name, mapping)
            entries.append({'file': str(destination.relative_to(self.root)), 'store_name': name,
                            'row_count': result['row_count']})
        return entries, raw

    def test_groups_same_sheets_aligns_columns_preserves_values_and_sources(self):
        entries, raw = self.prepare([
            ('紫鸟店铺甲', {'账务列表': [['订单号', '金额'], ['00012345678901234567890', -12.5]],
                         '财务明细': [['备注', '金额'], ['=literal', 0]], '空表': [], '仅表头': [['编号']]}),
            ('紫鸟店铺乙', {'账务列表': [['金额', '订单号', '币种'], [8.75, '00099', 'USD']],
                         '财务明细': [['金额', '备注'], [-1, '退款']], '其他': [['编号'], ['002']]})])
        result = workbooks.merge_store_workbooks(self.root, entries, self.root / 'results/all.xlsx', incomplete=False)
        self.assertEqual(result['row_counts'], {'账务列表': 2, '财务明细': 2, '空表': 0, '仅表头': 0, '其他': 1})
        book = load_workbook(self.root / result['file'])
        try:
            self.assertEqual(list(book['账务列表'].values), [
                ('店铺名', '订单号', '金额', '币种'), ('紫鸟店铺甲', '00012345678901234567890', -12.5, None),
                ('紫鸟店铺乙', '00099', 8.75, 'USD')])
            self.assertEqual(book['财务明细']['B2'].value, '=literal')
            self.assertEqual(book['财务明细']['B2'].data_type, 's')
            self.assertEqual(book['账务列表']['C2'].number_format, '#,##0.00')
            self.assertEqual(list(book['仅表头'].values), [('店铺名', '编号')])
            self.assertEqual(book['账务列表'].freeze_panes, 'B2')
            self.assertTrue(book['账务列表'].auto_filter.ref)
        finally:
            book.close()
        self.assertTrue(all(path.read_bytes() == contents for path, contents in raw))

    def test_existing_shop_column_is_renamed_consistently_without_collision(self):
        entries, _ = self.prepare([
            ('甲', {'明细': [['店铺名', '金额'], ['平台甲', 1]]}),
            ('乙', {'明细': [['金额', '店铺名（原表）', '店铺名'], [2, '另一字段', '平台乙']]})])
        workbooks.merge_store_workbooks(self.root, entries, self.root / 'results/all.xlsx', incomplete=False)
        book = load_workbook(self.root / 'results/all.xlsx')
        try:
            self.assertEqual(list(book.active.values), [
                ('店铺名', '店铺名（原表2）', '金额', '店铺名（原表）'),
                ('甲', '平台甲', 1, None), ('乙', '平台乙', 2, '另一字段')])
        finally:
            book.close()

    def test_original_sheet_title_is_not_trimmed_and_limits_apply_per_sheet(self):
        with patch.object(workbooks, 'MAX_DATA_ROWS', 2):
            entries, _ = self.prepare([('甲', {' 明细 ': [['编号'], ['1'], ['2']], '其他': [['编号'], ['3'], ['4']]})])
            result = workbooks.merge_store_workbooks(self.root, entries, self.root / 'results/all.xlsx', incomplete=False)
        self.assertEqual(result['sheet_names'], [' 明细 ', '其他'])
        self.assertEqual(result['row_count'], 4)

    def test_invalid_headers_data_and_formula_release_file_handles(self):
        for rows in ([['ID', 'ID'], ['1', '2']], [['ID', None, '金额']], [['ID'], ['1', '无表头']]):
            with self.subTest(rows=rows):
                source = make_workbook(self.root / 'invalid.xlsx', {'明细': rows})
                with self.assertRaises(ValueError):
                    workbooks.inspect_workbook(source)
                source.unlink()  # Must work immediately on Windows, including errors.
        source = make_workbook(self.root / 'formula.xlsx', {'明细': [['ID'], ['001']]})
        book = load_workbook(source)
        book.active['A2'] = '=1+1'
        book.save(source)
        book.close()
        with self.assertRaisesRegex(ValueError, '公式'):
            workbooks.inspect_workbook(source)
        source.unlink()

    def test_no_overwrite_atomic_failure_and_excel_limits(self):
        source = make_workbook(self.root / 'raw.xlsx', {'明细': [['ID', '金额'], ['001', 1]]})
        destination = self.root / 'results/shop.xlsx'
        with patch.object(workbooks, 'MAX_COLUMNS', 2):
            with self.assertRaisesRegex(ValueError, '列数上限'):
                workbooks.enrich_store(source, destination, '甲', {})
        self.assertEqual(list(destination.parent.iterdir()), [])
        entries, _ = self.prepare([('甲', {'明细': [['ID'], ['1']]}), ('乙', {'明细': [['ID'], ['2']]})])
        with patch.object(workbooks, 'MAX_DATA_ROWS', 1):
            with self.assertRaisesRegex(ValueError, '行数上限'):
                workbooks.merge_store_workbooks(self.root, entries, self.root / 'results/limit.xlsx', incomplete=False)
        self.assertFalse((self.root / 'results/limit.xlsx').exists())
        result = workbooks.merge_store_workbooks(self.root, entries, self.root / 'results/all.xlsx', incomplete=True)
        before = (self.root / result['file']).read_bytes()
        with self.assertRaisesRegex(ValueError, '已存在'):
            workbooks.merge_store_workbooks(self.root, entries, self.root / result['file'], incomplete=False)
        self.assertEqual((self.root / result['file']).read_bytes(), before)

    def test_identity_mismatch_is_not_published_and_paths_stay_in_batch(self):
        entries, _ = self.prepare([('甲', {'明细': [['ID'], ['1']]})])
        entries[0]['store_name'] = '其他店'
        with self.assertRaisesRegex(ValueError, '店铺名'):
            workbooks.merge_store_workbooks(self.root, entries, self.root / 'results/all.xlsx', incomplete=False)
        self.assertFalse((self.root / 'results/all.xlsx').exists())
        entries[0]['file'] = '../outside.xlsx'
        with self.assertRaisesRegex(ValueError, '超出'):
            workbooks.merge_store_workbooks(self.root, entries, self.root / 'results/all.xlsx', incomplete=False)

    def two_level_source(self, filename, leaf_headers, data):
        source = self.root / filename
        book = Workbook()
        sheet = book.active
        sheet.title = '结算'
        sheet.append(['流水ID', '商品信息', *([None] * (len(leaf_headers) - 1)), '金额'])
        sheet.append([None, *leaf_headers, None])
        sheet.merge_cells('A1:A2')
        last = workbooks.get_column_letter(len(leaf_headers) + 2)
        sheet.merge_cells(f'{last}1:{last}2')
        end = workbooks.get_column_letter(len(leaf_headers) + 1)
        sheet.merge_cells(f'B1:{end}1')
        for row in data:
            sheet.append(row)
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
            sheet.cell(sheet.max_row, len(leaf_headers) + 2).number_format = '#,##0.00'
        book.save(source)
        book.close()
        return source

    def test_merged_two_row_headers_preserve_groups_align_fields_and_count_only_data(self):
        sources = [
            self.two_level_source('a.xlsx', ['SKU ID', '件数'], [['001', '00012', 2, 3.5]]),
            self.two_level_source('b.xlsx', ['件数', 'SKU ID', 'SKU名称'], [['002', 4, '00013', '=literal', 9.5]]),
        ]
        original = [source.read_bytes() for source in sources]
        entries = []
        for index, source in enumerate(sources):
            info = workbooks.inspect_workbook(source)
            self.assertEqual(info.row_count, 1)
            self.assertEqual(info.sheets[0].header_depth, 2)
            destination = self.root / 'results' / f'shop{index}.xlsx'
            result = workbooks.enrich_store(source, destination, f'店铺{index}', {})
            entries.append({'file': str(destination.relative_to(self.root)),
                            'store_name': f'店铺{index}', 'row_count': result['row_count']})
        result = workbooks.merge_store_workbooks(self.root, entries, self.root / 'results/all.xlsx', incomplete=False)
        self.assertEqual(result['row_counts'], {'结算': 2})
        book = load_workbook(self.root / result['file'])
        try:
            sheet = book['结算']
            self.assertEqual(list(sheet.values), [
                ('店铺名', '流水ID', '商品信息', None, '金额', '商品信息'),
                (None, None, 'SKU ID', '件数', None, 'SKU名称'),
                ('店铺0', '001', '00012', 2, 3.5, None),
                ('店铺1', '002', '00013', 4, 9.5, '=literal'),
            ])
            self.assertEqual({str(r) for r in sheet.merged_cells.ranges}, {'A1:A2', 'B1:B2', 'C1:D1', 'E1:E2'})
            self.assertEqual(sheet.freeze_panes, 'B3')
            self.assertEqual(sheet['E3'].number_format, '#,##0.00')
            self.assertEqual(sheet['F4'].data_type, 's')
        finally:
            book.close()
        self.assertEqual([source.read_bytes() for source in sources], original)

    def test_two_header_rows_use_excel_row_capacity_and_invalid_merges_fail_closed(self):
        source = self.two_level_source('raw.xlsx', ['SKU ID', '件数'], [['001', 'SKU', 2, 1]])
        with patch.object(workbooks, 'MAX_DATA_ROWS', 1):
            with self.assertRaisesRegex(ValueError, '行数上限'):
                workbooks.inspect_workbook(source)
        book = load_workbook(source)
        book.active['C2'] = None
        book.save(source)
        book.close()
        with self.assertRaisesRegex(ValueError, '空白表头'):
            workbooks.inspect_workbook(source)
        source.unlink()
        source = make_workbook(self.root / 'merged_data.xlsx', {'明细': [['ID', '金额'], ['001', 1], ['002', 2]]})
        book = load_workbook(source)
        book.active.merge_cells('A2:A3')
        book.save(source)
        book.close()
        with self.assertRaisesRegex(ValueError, '数据区合并'):
            workbooks.inspect_workbook(source)
        source.unlink()
