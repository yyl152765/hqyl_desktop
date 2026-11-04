from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from backend.services import temu_fee_refund_refunds as refunds


class RefundFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'refund.xlsx'
        self.row = ['CASE-01', '退货退款', '泰国', 'PO-01', '已发货', '00123456789012345678',
                    '2026年9月26日 19:39 CST', '与描述不符', 1, '已退款', 0, 0]

    def write(self, rows=(), headers=None, extra_sheet=False):
        workbook = Workbook()
        workbook.active.append(list(headers or refunds.HEADERS))
        for row in rows:
            workbook.active.append(row)
        if extra_sheet:
            workbook.create_sheet('unexpected')
        workbook.save(self.path)
        workbook.close()

    def validate(self):
        return refunds.validate_refund_file(self.path, date(2026, 9, 1), date(2026, 9, 30))

    def test_trailing_header_spaces_repeated_cases_and_all_amounts_are_retained(self):
        rows = []
        for index, amount in enumerate((0, -3.5, None, 'USD 2.00')):
            row = list(self.row)
            row[5] = str(index + 1)  # The same case with distinct SKUs is valid.
            row[10:12] = [amount, amount]
            rows.append(row)
        headers = [label + ' ' for label in refunds.HEADERS]
        self.write(rows, headers=headers)
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(self.validate(), 4)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_dates_include_both_boundaries_and_excel_datetime(self):
        rows = []
        for value in ('2026年9月1日 00:00 CST', '2026年9月30日 23:59:59 CST', datetime(2026, 9, 12, 15, 45)):
            row = list(self.row); row[6] = value; rows.append(row)
        self.write(rows)
        self.assertEqual(self.validate(), 3)

    def test_wrong_type_status_missing_identity_and_invalid_or_outside_date_fail(self):
        for column, value, message in (
            (1, '仅退款', '不是退货退款'), (9, '退款中', '不是已退款'),
            (0, None, '缺少必填值'), (5, ' ', '缺少必填值'),
            (6, '2026年8月31日 23:59 CST', '超出请求范围'),
            (6, '2026年9月31日 00:00 CST', '不是有效日期'),
            (6, '2026年9月26日 25:00 CST', '不是有效日期'),
            (6, '2026年9月26日 19:39 UTC', '格式无法确认'),
        ):
            with self.subTest(column=column, value=value):
                row = list(self.row); row[column] = value; self.write([row])
                with self.assertRaisesRegex(ValueError, message): self.validate()

    def test_empty_export_is_valid_but_wrong_headers_or_extra_sheet_are_not(self):
        self.write()
        self.assertEqual(self.validate(), 0)
        self.write(headers=['订单号', '金额'])
        with self.assertRaisesRegex(ValueError, '缺少必要表头'): self.validate()
        self.write(headers=[*refunds.HEADERS, '售后单号 '])
        with self.assertRaisesRegex(ValueError, '表头重复'): self.validate()
        self.write([self.row], extra_sheet=True)
        with self.assertRaisesRegex(ValueError, '工作表数量'): self.validate()

    def test_upper_bound_is_rejected_and_formulas_are_not_trusted(self):
        self.write([self.row, self.row])
        with patch.object(refunds, 'EXPORT_LIMIT', 2):
            with self.assertRaisesRegex(ValueError, '缩小日期范围'): self.validate()
        row = list(self.row); row[11] = '=1+2'; self.write([row])
        with self.assertRaisesRegex(ValueError, '包含公式'): self.validate()


if __name__ == '__main__':
    unittest.main()
