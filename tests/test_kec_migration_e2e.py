"""Offline legacy-template migration and complete report regression."""
from __future__ import annotations

import hashlib
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from backend.core.mabang_reconciliation_client import MabangOrderInfo
from backend.services.kec_reconciliation import (
    CONTRACT_RULE_VERSION, KecQuoteRules, KecReconciliationService, _calculate_storage_age_days,
    inspect_kec_source_workbook, run_kec_reconciliation,
    validate_kec_reconciliation_payload,
)
from tests.test_kec_reconciliation import EQUIVALENT_SHEETS, STORAGE_HEADERS, build_bill, storage_row


def values_by_header(sheet, row=2):
    return dict(zip(next(sheet.iter_rows(values_only=True)), next(sheet.iter_rows(min_row=row, max_row=row, values_only=True))))


class LegacyCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_chinese_headers_and_title_rows_use_the_same_columns_for_preview_and_money(self):
        path = build_bill(self.root / 'localized.xlsx', [])
        workbook = load_workbook(path)
        self.addCleanup(workbook.close)
        sheet = workbook[EQUIVALENT_SHEETS['storage']]
        sheet.delete_rows(1)
        sheet.append(['越南仓储明细'])
        sheet.append([])
        replacements = {'Unit Length(CM)': '长', 'Unit Width(CM)': '宽', 'Unit Height(CM)': '高', '数量(PCS)': '数量'}
        sheet.append([replacements.get(header, header) for header in STORAGE_HEADERS])
        sheet.append(storage_row(length=100, width=100, height=20, quantity=6, unit_cbm='0.4', total_cbm='2.4',
                                 inbound='2026-07-17', bill='2026-09-15', age=61, price='1.5', amount='1.8'))
        sheet.title = '仓租费'
        workbook.save(path)
        preview = inspect_kec_source_workbook(path)
        self.assertEqual(preview.columns['长(CM)'], '长')
        self.assertTrue(any('第 3 行' in warning for warning in preview.warnings))
        result_book = Workbook()
        self.addCleanup(result_book.close)
        stats = KecReconciliationService()._write_storage_sheet(result_book.active, sheet, KecQuoteRules())
        self.assertEqual(stats['仓储费_核对金额'], Decimal('1.8000000'))
        record = values_by_header(result_book.active)
        self.assertEqual(record['账单行号'], 4)
        self.assertEqual(record['核对结果'], '正常')

    def test_named_unloading_is_not_reused_as_an_unnamed_storage_section(self):
        workbook = Workbook()
        self.addCleanup(workbook.close)
        workbook.active.title = 'Invoice'
        workbook.create_sheet('卸货费')
        workbook.create_sheet('旧仓库明细')
        for title in ('出库+耗材', '退件上架费', '增值服务费'):
            workbook.create_sheet(title)
        notes = []
        sheets = KecReconciliationService(notes.append)._resolve_source_sheets(workbook)
        self.assertEqual(sheets['storage'].title, '旧仓库明细')
        self.assertEqual(len({sheet.title for sheet in sheets.values()}), 6)
        self.assertTrue(notes)

    def test_missing_sections_cannot_reuse_another_fee_table(self):
        path = build_bill(self.root / 'missing.xlsx', [])
        workbook = load_workbook(path)
        del workbook['增值服务费']
        workbook.save(path)
        workbook.close()
        with self.assertRaisesRegex(ValueError, '杂费'):
            inspect_kec_source_workbook(path)

    def test_mixed_month_outbound_data_cannot_select_the_wrong_tier(self):
        workbook = Workbook()
        self.addCleanup(workbook.close)
        sheet = workbook.active
        sheet.append(['表头'] * 9)
        for month in (9, 10):
            row = [None] * 9
            row[4:7] = [date(2026, month, 1), f'FIXTURE-{month}', f'TRACK-{month}']
            sheet.append(row)
        with self.assertRaisesRegex(ValueError, '多个自然月'):
            KecReconciliationService()._read_outbound_identifiers(sheet)

    def test_storage_age_rejects_a_bill_before_receipt(self):
        self.assertIsNone(_calculate_storage_age_days(datetime(2026, 9, 2), datetime(2026, 9, 1)))

    def test_legacy_quote_accepts_explicit_free_text_but_rejects_prose_as_a_price(self):
        quote = Workbook()
        self.addCleanup(quote.close)
        sheet = quote.active
        for row, amount in ((5, '免费\nfree'), (6, 2), (7, 3.5), (8, 5.1), (9, 1.8), (10, 1.7), (11, 1.5)):
            sheet.cell(row, 9, amount)
        sheet.cell(9, 10, 0.6)
        sheet.cell(43, 9, 0.5)
        sheet.cell(43, 11, 'ABF1')
        path = self.root / 'legacy_quote.xlsx'
        quote.save(path)
        rules = KecQuoteRules.from_workbook(path)
        self.assertEqual(rules.storage_price_for_age(30), Decimal('0'))
        self.assertEqual(rules.storage_price_for_age(31), Decimal('2'))
        sheet.cell(5, 9, '免费30天之后另收2元')
        quote.save(path)
        with self.assertRaisesRegex(ValueError, '有效费率'):
            KecQuoteRules.from_workbook(path)

    def test_invalid_erp_origin_is_rejected_without_logging_credentials(self):
        path = build_bill(self.root / 'valid.xlsx', [])
        for origin in ('file:///tmp/data', 'https://name:private@mabangerp.com', 'https://mabangerp.com/path', 'https://mabangerp.com?token=private'):
            with self.subTest(origin=origin), self.assertRaises(ValueError) as context:
                validate_kec_reconciliation_payload({'input_file': str(path), 'output_dir': str(self.root),
                                                     'username': 'fixture', 'password': 'fixture', 'base_url': origin})
            self.assertNotIn('private', str(context.exception))

    def test_complete_report_preserves_source_and_applies_new_contract_offline(self):
        path = build_bill(self.root / 'legacy_bill.xlsx', [storage_row(
            length=100, width=100, height=20, quantity=6, unit_cbm='0.4', total_cbm='2.4',
            inbound='2026-07-17', bill='2026-09-15', age=61, price='1.5', amount='1.8')])
        workbook = load_workbook(path)
        invoice = workbook['Invoice']
        invoice.cell(17, 2, '仓储费')
        invoice.cell(17, 10, 1.8)
        unloading = workbook[EQUIVALENT_SHEETS['unloading']]
        unloading.append(['日期', '批次', '说明', '方数', '备注', '卸货费'])
        unloading.append(['', 'FIXTURE-BATCH', '', 1.2, '', 54])
        outbound = workbook[EQUIVALENT_SHEETS['outbound']]
        outbound.append([f'字段-{i}' for i in range(29)])
        row = [None] * 29
        row[4:7] = ['2026-09-15', 'FIXTURE-ORDER', 'FIXTURE-TRACK']
        row[8], row[17] = 20, 'ABF1'
        row[24:27], row[28] = [0.5, 1.8, 9.9], 12.2
        outbound.append(row)
        returns = workbook[EQUIVALENT_SHEETS['return']]
        headers = [f'字段-{i}' for i in range(21)]
        for index, text in ((5, 'ASN #'), (6, 'PO Number'), (7, 'Is Return'), (8, 'SKU Code'), (10, 'Actual QTY'), (20, 'Amount')):
            headers[index] = text
        returns.append(headers)
        for sku, qty, amount in (('SKU-A', 1, 3), ('SKU-B', 3, 0)):
            row = [None] * 21
            row[5:9], row[10], row[20] = ['RETURN-TRACK', 'RETURN-ORDER', 'Y', sku], qty, amount
            returns.append(row)
        misc = workbook[EQUIVALENT_SHEETS['misc']]
        misc.append(['日期', '订单', '数量', '费用说明', '总费用'])
        misc.append(['', 'FIXTURE-MISC', 3, '退仓费', 3.3])
        workbook.save(path)
        workbook.close()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        order = MabangOrderInfo('FIXTURE-TRACK', 'FIXTURE-ORDER', express_time='2026-09-15',
                                raw_data={'order_ellipsis_title': '商品种类：1，商品个数：20'})
        lookup = SimpleNamespace(order=None, error_message='', has_return_hit=True, has_refund_hit=False)
        job = validate_kec_reconciliation_payload({'username': 'fixture', 'password': 'fixture',
                                                   'input_file': str(path), 'output_dir': str(self.root / 'results')})
        with patch.object(KecReconciliationService, '_fetch_outbound_orders', return_value=({'FIXTURE-ORDER': order}, {})), \
                patch.object(KecReconciliationService, '_fetch_return_lookups', return_value={'RETURN-TRACK': lookup}), \
                patch('httpx.Client.send', side_effect=AssertionError('Offline test must not access ERP')):
            result = run_kec_reconciliation(job)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(result.avg_daily_orders, Decimal(1) / Decimal(30))
        expected = {'仓储费': '1.8', '卸货费': '54', '出库+耗材': '12.2', '退件上架费': '3', '杂费': '3.3'}
        for section, amount in expected.items():
            self.assertEqual(result.summary[section + '_核对金额'], Decimal(amount))
        output = load_workbook(result.output_file, read_only=True, data_only=True)
        self.addCleanup(output.close)
        self.assertEqual(len(output.sheetnames), 6)
        self.assertEqual(values_by_header(output['出库耗材核对'])['核对续件费'], 9.9)
        self.assertTrue(any(CONTRACT_RULE_VERSION in str(row) for row in output['汇总'].iter_rows(values_only=True)))


if __name__ == '__main__':
    unittest.main()
