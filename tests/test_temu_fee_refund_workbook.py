from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from backend.services.temu_fee_refund_workbook import add_store_columns


class FeeRefundWorkbookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_xlsx_preserves_values_types_styles_and_hidden_rows(self):
        source = self.root / 'source.xlsx'
        output = self.root / 'output.xlsx'
        book = Workbook()
        sheet = book.active
        sheet.append(['账单号', '金额', '备注'])
        sheet.append(['00001234567890123456', -1.23, '=原始文本'])
        sheet['C2'].data_type = 's'
        sheet['A2'].font = Font(bold=True)
        sheet.row_dimensions[2].hidden = True
        sheet.column_dimensions['A'].width = 33
        sheet.freeze_panes = 'B2'
        sheet.auto_filter.ref = 'A1:C2'
        book.save(source)
        book.close()
        before = source.read_bytes()
        count = add_store_columns(source, output, '=运营', '测试店')
        self.assertEqual(count, 1)
        self.assertEqual(before, source.read_bytes())
        book = load_workbook(output)
        self.addCleanup(book.close)
        sheet = book.active
        self.assertEqual([c.value for c in sheet[1]], ['运营名', '店铺名', '账单号', '金额', '备注'])
        self.assertEqual([c.value for c in sheet[2]], ['=运营', '测试店', '00001234567890123456', -1.23, '=原始文本'])
        self.assertEqual(sheet['A2'].data_type, 's')
        self.assertEqual(sheet['E2'].data_type, 's')
        self.assertTrue(sheet['C2'].font.bold)
        self.assertTrue(sheet.row_dimensions[2].hidden)
        self.assertEqual(sheet.column_dimensions['C'].width, 33)
        self.assertEqual(sheet.freeze_panes, 'D2')
        self.assertEqual(sheet.auto_filter.ref, 'C1:E2')

    def test_csv_preserves_quoted_newline_and_long_id(self):
        source, output = self.root / 'in.csv', self.root / 'out.csv'
        with source.open('w', encoding='gb18030', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerows([['订单', '说明'], ['00000123456789012345', '第一行\n第二行']])
        self.assertEqual(add_store_columns(source, output, '小王', '店铺A'), 1)
        with output.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows, [['运营名', '店铺名', '订单', '说明'],
                                ['小王', '店铺A', '00000123456789012345', '第一行\n第二行']])

    def test_formula_rejected_without_replacing_existing_output(self):
        source, output = self.root / 'in.xlsx', self.root / 'out.xlsx'
        book = Workbook()
        book.active.append(['金额'])
        book.active.append(['=1+1'])
        book.save(source)
        book.close()
        output.write_bytes(b'previous result')
        with self.assertRaisesRegex(ValueError, '公式'):
            add_store_columns(source, output, '小王', '店铺A')
        self.assertEqual(output.read_bytes(), b'previous result')
        self.assertEqual(list(self.root.glob('.*.xlsx')), [])

    def test_missing_identity_and_in_place_rejected(self):
        source = self.root / 'in.csv'
        source.write_text('a\n1\n', encoding='utf-8')
        for destination, operator in ((self.root / 'out.csv', ''), (source, '小王')):
            with self.assertRaises(ValueError):
                add_store_columns(source, destination, operator, '店铺A')
        self.assertEqual(source.read_text(encoding='utf-8'), 'a\n1\n')


if __name__ == '__main__':
    unittest.main()
