"""KEC reconciliation regression tests.

The migration into the desktop platform changed exactly one business rule: the
storage fee is now reconciled on the captured **actual volume** of each bill row

    长(CM) × 宽(CM) × 高(CM) × 数量(PCS) × 0.000001

instead of the bill's rounded ``Total CBM within period`` column.  These tests
pin that rule down, keep ``Total CBM`` as a comparison-only column, and cover
the payload validation and the bridge contract around it.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from backend.services.kec_reconciliation import (
    ACTUAL_VOLUME_BASIS_LABEL,
    CUBIC_METER_SCALE,
    KecQuoteRules,
    KecReconciliationService,
    calculate_actual_volume,
    inspect_kec_source_workbook,
    validate_kec_reconciliation_payload,
)
from backend.services.kec_reconciliation import _unique_output_path
from backend.services.kec_reconciliation import KecReconciliationResult

STORAGE_HEADERS = [
    '仓库代码', '客户名称', 'SKU', '品名', '入库单号', '箱号', '库位',
    'Unit Length(CM)', 'Unit Width(CM)', 'Unit Height(CM)', 'Unit Billing CBM',
    '计费开始日期', '备注', '货物状态', '入库时间', '计费时间',
    '数量(PCS)', 'Total CBM within period', '库龄', '单价', '金额',
]
EQUIVALENT_SHEETS = {
    'invoice': 'Invoice',
    'storage': 'Inventory CBM-仓储费',
    'unloading': 'Unloading 卸货费',
    'outbound': '出库+耗材',
    'return': '退件上架费',
    'misc': '增值服务费',
}


def storage_row(*, length, width, height, quantity, unit_cbm, total_cbm, inbound, bill,
                age, price, amount, key='K-1001') -> list[object]:
    """One storage row laid out on the bill's real column order."""
    return [
        'WH01', '客户甲', 'SKU-1', '测试商品', key, 'BOX-1', 'A-01',
        length, width, height, unit_cbm,
        '2026-02-01', '备注', '正常', inbound, bill,
        quantity, total_cbm, age, price, amount,
    ]


def build_bill(path: Path, rows: list[list[object]], headers: list[object] | None = None) -> Path:
    workbook = Workbook()
    workbook.active.title = EQUIVALENT_SHEETS['invoice']
    storage = workbook.create_sheet(EQUIVALENT_SHEETS['storage'])
    storage.append(headers if headers is not None else STORAGE_HEADERS)
    for row in rows:
        storage.append(row)
    for role in ('unloading', 'outbound', 'return', 'misc'):
        workbook.create_sheet(EQUIVALENT_SHEETS[role])
    workbook.save(path)
    workbook.close()
    return path


def reconcile_storage(source_path: Path, quote_rules: KecQuoteRules | None = None):
    """Run only the storage sheet and return (worksheet, stats, column indexes)."""
    service = KecReconciliationService()
    source_workbook = load_workbook(source_path)
    try:
        source_sheet = source_workbook[EQUIVALENT_SHEETS['storage']]
        output_workbook = Workbook()
        worksheet = output_workbook.active
        stats = service._write_storage_sheet(worksheet, source_sheet, quote_rules or KecQuoteRules())
        header = [cell.value for cell in worksheet[1]]
        indexes = {name: header.index(name) for name in (
            '账单行号', '核算基数(实际体积)', '账单 Total CBM', '体积差额',
            '核对金额', '金额差额', '核对结果', '核对说明',
        )}
        return worksheet, stats, indexes
    finally:
        source_workbook.close()


def cell(worksheet, row_number: int, index: int):
    return worksheet.cell(row=row_number, column=index + 1).value


class ActualVolumeFormulaTests(unittest.TestCase):
    def test_actual_volume_is_dimensions_times_quantity_in_cubic_metres(self) -> None:
        self.assertEqual(calculate_actual_volume(Decimal('10'), Decimal('20'), Decimal('30'), Decimal('5')), Decimal('0.03'))
        self.assertEqual(CUBIC_METER_SCALE, Decimal('0.000001'))
        self.assertEqual(calculate_actual_volume(Decimal('100'), Decimal('50'), Decimal('20'), Decimal('10')), Decimal('1'))

    def test_missing_dimensions_yield_none_instead_of_a_silent_zero(self) -> None:
        for arguments in (
            (None, Decimal('1'), Decimal('1'), Decimal('1')),
            (Decimal('1'), None, Decimal('1'), Decimal('1')),
            (Decimal('1'), Decimal('1'), None, Decimal('1')),
            (Decimal('1'), Decimal('1'), Decimal('1'), None),
        ):
            with self.subTest(arguments=arguments):
                self.assertIsNone(calculate_actual_volume(*arguments))

    def test_negative_dimensions_are_rejected(self) -> None:
        self.assertIsNone(calculate_actual_volume(Decimal('-1'), Decimal('1'), Decimal('1'), Decimal('1')))
        self.assertIsNone(calculate_actual_volume(Decimal('1'), Decimal('1'), Decimal('1'), Decimal('-2')))

    def test_zero_dimension_stays_a_real_zero(self) -> None:
        self.assertEqual(calculate_actual_volume(Decimal('0'), Decimal('1'), Decimal('1'), Decimal('1')), Decimal('0'))


class StorageSheetActualVolumeBasisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.output_dir = Path(self.temp_dir.name)

    def bill_path(self, name: str = 'bill.xlsx') -> Path:
        return self.output_dir / name

    def test_storage_sheet_keeps_total_cbm_only_as_a_comparison_column(self) -> None:
        # Row 1 is billed at the goods' real volume; row 2 is billed 1.5 CBM for
        # goods that really measure 1.2 CBM (100 × 40 × 30 cm × 10 pcs), at 2 元/CBM/day.
        rows = [
            storage_row(length='10', width='20', height='30', quantity='5', unit_cbm='0.008',
                        total_cbm='0.04', inbound='2026-02-01', bill='2026-02-10',
                        age='10', price='0', amount='0', key='K-1'),
            storage_row(length='100', width='40', height='30', quantity='10', unit_cbm='0.15',
                        total_cbm='1.5', inbound='2026-01-01', bill='2026-02-20',
                        age='51', price='2', amount='3', key='K-2'),
        ]
        path = build_bill(self.bill_path(), rows)
        worksheet, stats, indexes = reconcile_storage(path)

        self.assertIn(f'核算基数({ACTUAL_VOLUME_BASIS_LABEL})', [cell.value for cell in worksheet[1]])
        self.assertEqual(cell(worksheet, 2, indexes['核算基数(实际体积)']), 0.03)
        self.assertEqual(cell(worksheet, 3, indexes['核算基数(实际体积)']), 1.2)
        # Total CBM is still reported, just no longer used for the money.
        self.assertEqual(cell(worksheet, 2, indexes['账单 Total CBM']), 0.04)
        self.assertEqual(cell(worksheet, 3, indexes['账单 Total CBM']), 1.5)
        self.assertEqual(cell(worksheet, 2, indexes['体积差额']), round(0.04 - 0.03, 10))
        self.assertEqual(cell(worksheet, 3, indexes['体积差额']), round(1.5 - 1.2, 10))

        # 0.03 × 0（≤30 天免费）and 1.2 × 2（31-120 天）
        self.assertEqual(cell(worksheet, 2, indexes['核对金额']), 0)
        self.assertEqual(cell(worksheet, 3, indexes['核对金额']), 2.4)
        self.assertEqual(cell(worksheet, 3, indexes['金额差额']), round(3 - 2.4, 10))

        self.assertEqual(stats['仓储费_账单金额'], Decimal('3'))
        self.assertEqual(stats['仓储费_核对金额'], Decimal('2.4'))
        self.assertEqual(stats['仓储费_体积差额合计'], Decimal('0.31'))
        self.assertEqual(stats['仓储费_缺失尺寸行数'], 0)
        # The bill was priced on Total CBM, so it now flags as a real difference.
        self.assertEqual(stats['仓储费_异常数'], 1)
        self.assertIn(f'{ACTUAL_VOLUME_BASIS_LABEL}×单价', cell(worksheet, 3, indexes['核对说明']))

    def test_expected_amount_ignores_the_bills_total_cbm_rounding(self) -> None:
        """The whole point of the migration: money follows the goods, not the rounding."""

        common = dict(length='10', width='20', height='30', quantity='5', unit_cbm='0.008',
                      inbound='2025-06-01', bill='2026-02-01', age='246', price='5.1')
        first = self.bill_path('first.xlsx')
        second = self.bill_path('second.xlsx')
        build_bill(first, [storage_row(total_cbm='0.04', amount='0.204', key='K-1', **common)])
        build_bill(second, [storage_row(total_cbm='0.05', amount='0.255', key='K-1', **common)])

        first_sheet, first_stats, first_indexes = reconcile_storage(first)
        second_sheet, second_stats, second_indexes = reconcile_storage(second)

        self.assertNotEqual(
            cell(first_sheet, 2, first_indexes['账单 Total CBM']),
            cell(second_sheet, 2, second_indexes['账单 Total CBM']),
        )
        self.assertEqual(first_stats['仓储费_核对金额'], second_stats['仓储费_核对金额'])
        self.assertEqual(first_stats['仓储费_核对金额'], Decimal('0.153'))
        self.assertNotEqual(first_stats['仓储费_体积差额合计'], second_stats['仓储费_体积差额合计'])
        self.assertEqual(first_stats['仓储费_体积差额合计'], Decimal('0.01'))
        self.assertEqual(second_stats['仓储费_体积差额合计'], Decimal('0.02'))

    def test_rows_without_dimensions_are_counted_and_scored_as_zero(self) -> None:
        rows = [
            storage_row(length='10', width='20', height='30', quantity='5', unit_cbm='0.008',
                        total_cbm='0.04', inbound='2026-02-01', bill='2026-02-10',
                        age='10', price='0', amount='1.5', key='K-1'),
            storage_row(length=None, width='20', height='30', quantity='5', unit_cbm='0.008',
                        total_cbm='0.04', inbound='2026-02-01', bill='2026-02-10',
                        age='10', price='0', amount='1.5', key='K-2'),
        ]
        path = build_bill(self.bill_path(), rows)
        worksheet, stats, indexes = reconcile_storage(path)

        self.assertIsNone(cell(worksheet, 3, indexes['核算基数(实际体积)']))
        self.assertIsNone(cell(worksheet, 3, indexes['核对金额']))
        self.assertEqual(cell(worksheet, 3, indexes['核对结果']), '异常')
        self.assertIn('无法计算实际体积', cell(worksheet, 3, indexes['核对说明']))
        self.assertEqual(stats['仓储费_缺失尺寸行数'], 1)
        # The unmeasurable row must not silently inflate the reconciled total.
        self.assertEqual(stats['仓储费_核对金额'], Decimal('0'))

    def test_quote_rules_still_drive_the_storage_tier_price(self) -> None:
        rules = KecQuoteRules(storage_price_day_181_plus=Decimal('9.9'))
        rows = [storage_row(length='10', width='10', height='10', quantity='1000', unit_cbm='0.001',
                            total_cbm='1', inbound='2025-01-01', bill='2026-02-01',
                            age='397', price='9.9', amount='9.9', key='K-1')]
        path = build_bill(self.bill_path(), rows)
        worksheet, stats, indexes = reconcile_storage(path, rules)

        self.assertEqual(cell(worksheet, 2, indexes['核算基数(实际体积)']), 1.0)
        self.assertEqual(cell(worksheet, 2, indexes['核对金额']), 9.9)
        self.assertEqual(stats['仓储费_异常数'], 0)


class SourceWorkbookInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.output_dir = Path(self.temp_dir.name)

    def test_valid_bill_reports_sheets_and_dimension_columns(self) -> None:
        path = build_bill(self.output_dir / 'bill.xlsx', [
            storage_row(length='10', width='20', height='30', quantity='5', unit_cbm='0.008',
                        total_cbm='0.04', inbound='2026-02-01', bill='2026-02-10',
                        age='10', price='0', amount='0'),
        ])
        preview = inspect_kec_source_workbook(path)

        self.assertEqual(preview.sheet_titles['storage'], 'Inventory CBM-仓储费')
        self.assertEqual(preview.columns['长(CM)'], 'Unit Length(CM)')
        self.assertEqual(preview.columns['数量(PCS)'], '数量(PCS)')
        self.assertEqual(preview.warnings, ())

    def test_missing_dimension_column_fails_before_login(self) -> None:
        headers = [header for header in STORAGE_HEADERS if header != 'Unit Height(CM)']
        path = build_bill(self.output_dir / 'broken.xlsx', [], headers=headers)
        with self.assertRaisesRegex(ValueError, '高\\(CM\\)'):
            inspect_kec_source_workbook(path)
        with self.assertRaisesRegex(ValueError, '高\\(CM\\)'):
            validate_kec_reconciliation_payload({
                'username': 'operator', 'password': 'secret',
                'input_file': str(path), 'output_dir': str(self.output_dir),
            })

    def test_missing_total_cbm_only_warns(self) -> None:
        headers = [header for header in STORAGE_HEADERS if header != 'Total CBM within period']
        path = build_bill(self.output_dir / 'no-total-cbm.xlsx', [], headers=headers)
        preview = inspect_kec_source_workbook(path)
        self.assertEqual(len(preview.warnings), 1)
        self.assertIn('Total CBM', preview.warnings[0])

    def test_missing_file_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, '不存在'):
            inspect_kec_source_workbook(self.output_dir / 'missing.xlsx')


class PayloadValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.output_dir = Path(self.temp_dir.name)
        self.bill = build_bill(self.output_dir / 'bill.xlsx', [
            storage_row(length='10', width='20', height='30', quantity='5', unit_cbm='0.008',
                        total_cbm='0.04', inbound='2026-02-01', bill='2026-02-10',
                        age='10', price='0', amount='0'),
        ])

    def payload(self, **overrides) -> dict[str, object]:
        data: dict[str, object] = {
            'username': 'operator',
            'password': 'secret',
            'input_file': str(self.bill),
            'output_dir': str(self.output_dir),
        }
        data.update(overrides)
        return data

    def test_valid_payload_returns_job_with_clamped_defaults(self) -> None:
        job = validate_kec_reconciliation_payload(self.payload(order_batch_size=999999, max_workers=99))
        self.assertEqual(job.username, 'operator')
        self.assertEqual(job.input_file, self.bill)
        self.assertEqual(job.output_dir, self.output_dir)
        self.assertIsNone(job.quote_file)
        self.assertEqual(job.order_batch_size, 1000)
        self.assertEqual(job.max_workers, 8)
        self.assertNotIn('secret', repr(job))

    def test_account_and_paths_are_required(self) -> None:
        with self.assertRaisesRegex(ValueError, '马帮账号'):
            validate_kec_reconciliation_payload(self.payload(username='', password=''))
        with self.assertRaisesRegex(ValueError, '供应商账单'):
            validate_kec_reconciliation_payload(self.payload(input_file=''))
        with self.assertRaisesRegex(ValueError, '输出目录'):
            validate_kec_reconciliation_payload(self.payload(output_dir=''))
        with self.assertRaisesRegex(ValueError, '报价表不存在'):
            validate_kec_reconciliation_payload(self.payload(quote_file=str(self.output_dir / 'nope.xlsx')))

    def test_output_path_never_overwrites_an_existing_workbook(self) -> None:
        first = _unique_output_path(self.output_dir, 'KEC对账_2026-02_核对结果.xlsx')
        self.assertEqual(first.name, 'KEC对账_2026-02_核对结果.xlsx')
        first.write_text('placeholder', encoding='utf-8')
        second = _unique_output_path(self.output_dir, 'KEC对账_2026-02_核对结果.xlsx')
        self.assertEqual(second.name, 'KEC对账_2026-02_核对结果_2.xlsx')
        self.assertFalse(second.exists())


class KecReconciliationBridgeContractTests(unittest.TestCase):
    def bridge(self, *, output_dir: Path, accounts=(), active=()):
        from backend.app_bridge import AppBridge
        from backend.config_store import AppSettings

        instance = AppBridge()
        instance.config_store = SimpleNamespace(
            load=lambda: AppSettings(output_dir=str(output_dir), accounts=list(accounts), active_account_ids=dict(active)),
            save=lambda payload: None,
        )
        return instance

    def test_info_reports_the_actual_volume_basis(self) -> None:
        bridge = self.bridge(output_dir=Path('C:/Exports'))
        info = bridge.get_kec_reconciliation_info()
        self.assertTrue(info['ok'])
        self.assertIn(ACTUAL_VOLUME_BASIS_LABEL, info['basis'])
        self.assertIn('0.000001', info['basis'])

    def test_start_injects_bound_account_and_registers_scoped_task(self) -> None:
        from backend.config_store import BoundAccount

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            bill = build_bill(output_dir / 'bill.xlsx', [
                storage_row(length='10', width='20', height='30', quantity='5', unit_cbm='0.008',
                            total_cbm='0.04', inbound='2026-02-01', bill='2026-02-10',
                            age='10', price='0', amount='0'),
            ])
            account = BoundAccount(id='mabang-1', vendor='mabang', name='跨境团队',
                                   username='operator', password='secret', extra={})
            bridge = self.bridge(output_dir=output_dir, accounts=[account], active={'mabang': account.id})
            started: dict[str, object] = {}

            class RecordingTasks:
                def start(self, name, runner, **kwargs):
                    started.update(name=name, runner=runner, **kwargs)
                    return {'ok': True, 'id': 'kec-task', 'context': kwargs.get('context', {})}

            bridge.tasks = RecordingTasks()
            with patch('backend.app_bridge.run_kec_reconciliation') as runner_patch:
                runner_patch.return_value = KecReconciliationResult(
                    output_file=output_dir / 'KEC对账_核对结果.xlsx',
                    avg_daily_orders=Decimal('140'),
                    outbound_base_fee=Decimal('0.75'),
                    summary={'仓储费_账单金额': Decimal('21603.09'), '仓储费_核对金额': Decimal('19694.02')},
                )
                result = bridge.start_kec_reconciliation({
                    'account_id': account.id,
                    'input_file': str(bill),
                    'output_dir': str(output_dir),
                    'quote_file': '',
                })

                self.assertEqual(result['ok'], True)
                self.assertEqual(started['tool'], 'kec_reconciliation')
                self.assertEqual(started['name'], 'KEC 对账')
                self.assertEqual(started['context']['account_id'], account.id)
                self.assertEqual(started['context']['input_file'], str(bill))
                self.assertEqual(started['context']['output_dir'], str(output_dir))

                # The runner resolves the module-level service lazily, so the job
                # is only visible once the task actually runs.
                payload = started['runner'](lambda _message: None)
                job = runner_patch.call_args.args[0]

            self.assertEqual(job.username, account.username)
            self.assertEqual(job.password, account.password)
            self.assertEqual(job.input_file, bill)

            self.assertEqual(payload['storage_billed_amount'], 21603.09)
            self.assertEqual(payload['storage_expected_amount'], 19694.02)
            self.assertEqual(payload['storage_difference'], 1909.07)
            self.assertEqual(payload['avg_daily_orders'], 140.0)
            self.assertIsInstance(payload['summary']['仓储费_账单金额'], float)

    def test_start_requests_account_binding_when_mabang_is_missing(self) -> None:
        bridge = self.bridge(output_dir=Path('C:/Exports'))
        result = bridge.start_kec_reconciliation({'input_file': 'C:/bill.xlsx', 'output_dir': 'C:/Exports'})
        self.assertFalse(result['ok'])
        self.assertTrue(result['requires_account'])
        self.assertEqual(result['vendor'], 'mabang')


if __name__ == '__main__':
    unittest.main()
