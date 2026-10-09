"""KEC supplier bill reconciliation.

Ported from ``finance_process/app/modules/kec_reconciliation/service.py`` and
adapted to the desktop platform (payload validation, task progress callback).

The only business rule that changed during the migration is the storage-fee
basis.  It used to be the bill's ``Total CBM within period`` column; it is now
the captured actual volume of the row's goods::

    actual_cbm = 长(CM) × 宽(CM) × 高(CM) × 数量 × 0.000001

Everything else (unloading, outbound + packaging, return shelving, sundry fees,
the quote workbook layout and the Mabang lookups) keeps the original behaviour.
"""

from __future__ import annotations

import calendar
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from openpyxl import Workbook, load_workbook

from backend.core.mabang_reconciliation_client import (
    DEFAULT_ORDER_BATCH_SIZE,
    DEFAULT_TIMEOUT_SECONDS,
    MabangClient,
    MabangLookupCoordinator,
    MabangLookupResult,
    MabangOrderInfo,
)

LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[str], None]
MONEY_PRECISION = Decimal('0.01')
COMPARE_TOLERANCE = Decimal('0.0001')
DEFAULT_MABANG_BASE_URL = 'https://900853.private.mabangerp.com'
DEFAULT_ORDER_BATCH_SIZE_LIMIT = 1000
RESULT_FILENAME_SUFFIX = '_核对结果.xlsx'
#: 单件体积以立方厘米计量，换算成立方米需要乘上这个系数。
CUBIC_METER_SCALE = Decimal('0.000001')
#: 单元格里的长宽高可能带单位或千分位，取数时按第一个数字解析。
NUMERIC_PREFIX_PATTERN = re.compile(r'-?\d+(?:\.\d+)?')
ACTUAL_VOLUME_BASIS_LABEL = '实际体积'
#: 长宽高与数量列既用于核对，也决定实际体积能否算出来。
STORAGE_DIMENSION_COLUMNS: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ('长(CM)', ('Unit Length',), 7),
    ('宽(CM)', ('Unit Width',), 8),
    ('高(CM)', ('Unit Height',), 9),
    ('数量(PCS)', ('PCS',), 16),
)
WORKBOOK_SHEET_ALIASES: dict[str, tuple[str, ...]] = {
    'invoice': ('Invoice',),
    'storage': ('Inventory CBM-仓储费', '仓储费'),
    'unloading': ('Unloading 卸货费', '卸货费'),
    'outbound': ('出库+耗材',),
    'return': ('退件上架费',),
    'misc': ('增值服务费', '杂费'),
}
WORKBOOK_SHEET_FALLBACK_INDEXES: dict[str, tuple[int, ...]] = {
    'invoice': (0,),
    'storage': (1, 2),
    'unloading': (2, 1),
    'outbound': (5,),
    'return': (6,),
    'misc': (8, 7),
}
ROLE_LABELS: dict[str, str] = {
    'invoice': 'Invoice 发票',
    'storage': 'Inventory CBM 仓储费',
    'unloading': 'Unloading 卸货费',
    'outbound': '出库+耗材',
    'return': '退件上架费',
    'misc': '杂费',
}


@dataclass(frozen=True, slots=True)
class KecReconciliationJob:
    username: str = field(repr=False)
    password: str = field(repr=False)
    input_file: Path
    output_dir: Path
    quote_file: Path | None = None
    base_url: str = DEFAULT_MABANG_BASE_URL
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    order_batch_size: int = DEFAULT_ORDER_BATCH_SIZE_LIMIT
    max_workers: int = 2
    verify_ssl: bool = True
    sample_outbound_orders: int = 0
    sample_return_trackings: int = 0


@dataclass(frozen=True, slots=True)
class KecReconciliationResult:
    output_file: Path
    avg_daily_orders: Decimal
    outbound_base_fee: Decimal
    summary: Mapping[str, Any]

    @property
    def storage_billed_amount(self) -> Decimal:
        return _to_decimal(self.summary.get('仓储费_账单金额'), default=Decimal('0')) or Decimal('0')

    @property
    def storage_expected_amount(self) -> Decimal:
        return _to_decimal(self.summary.get('仓储费_核对金额'), default=Decimal('0')) or Decimal('0')

    @property
    def storage_difference(self) -> Decimal:
        return self.storage_billed_amount - self.storage_expected_amount


@dataclass(frozen=True, slots=True)
class KecWorkbookPreview:
    """Header-level inspection result, used before login to fail fast."""

    input_file: Path
    sheet_titles: Mapping[str, str]
    columns: Mapping[str, str]
    warnings: tuple[str, ...] = ()


def inspect_kec_source_workbook(source_file: str | Path) -> KecWorkbookPreview:
    """Check that the bill carries every sheet and column the checks depend on.

    Only sheet metadata and header rows are read, so this stays fast even for a
    month-sized bill with tens of thousands of storage rows.
    """

    path = Path(source_file).expanduser()
    if not path.is_file():
        raise ValueError(f'KEC 供应商账单不存在: {path}')
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError('KEC 供应商账单无法打开，请确认文件未加密且格式正确') from exc

    try:
        service = KecReconciliationService()
        warnings: list[str] = []
        sheet_titles: dict[str, str] = {}
        for role in WORKBOOK_SHEET_ALIASES:
            sheet = service._resolve_source_sheet(workbook, role)
            sheet_titles[role] = sheet.title
        columns: dict[str, str] = {}
        storage_headers = list(
            next(workbook[sheet_titles['storage']].iter_rows(min_row=1, max_row=1, values_only=True))
        )
        for label, keywords, _default in STORAGE_DIMENSION_COLUMNS:
            # Require a real header match here: falling back to a column position
            # would silently reconcile money against the wrong column.
            index = _find_header_index(storage_headers, include_keywords=keywords)
            header_text = _text(_row_value(tuple(storage_headers), index))
            if index is None or not header_text:
                raise ValueError(
                    f'KEC 账单仓储费表缺少「{label}」列（期望表头含 {" 或 ".join(keywords)}），无法按实际体积核对'
                )
            columns[label] = header_text
        if not _find_header_index(storage_headers, include_keywords=('Total CBM',)):
            warnings.append('仓储费表未找到 Total CBM within period 列，账单体积差异将留空')
        return KecWorkbookPreview(
            input_file=path,
            sheet_titles=sheet_titles,
            columns=columns,
            warnings=tuple(warnings),
        )
    finally:
        workbook.close()


def validate_kec_reconciliation_payload(payload: dict[str, Any] | None) -> KecReconciliationJob:
    """Validate bridge data and return an immutable reconciliation job."""

    data = dict(payload or {})
    username = _text(data.get('username'))
    password = str(data.get('password') or '')
    if not username or not password.strip():
        raise ValueError('请选择有效的马帮账号')

    input_text = _text(data.get('input_file'))
    if not input_text:
        raise ValueError('请选择 KEC 供应商账单 Excel')
    input_file = Path(input_text).expanduser()
    # Fail before login when the bill is unreadable or uses an unexpected template.
    inspect_kec_source_workbook(input_file)

    output_text = _text(data.get('output_dir'))
    if not output_text:
        raise ValueError('请选择输出目录')
    output_dir = Path(output_text).expanduser()
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError('输出目录不能是文件')

    quote_text = _text(data.get('quote_file'))
    quote_file = Path(quote_text).expanduser() if quote_text else None
    if quote_file is not None and not quote_file.is_file():
        raise ValueError(f'KEC 报价表不存在: {quote_file}')

    base_url = (_text(data.get('base_url')) or DEFAULT_MABANG_BASE_URL).rstrip('/')
    return KecReconciliationJob(
        username=username,
        password=password,
        input_file=input_file,
        output_dir=output_dir,
        quote_file=quote_file,
        base_url=base_url,
        timeout_seconds=_bounded_int(
            data.get('timeout_seconds'), default=DEFAULT_TIMEOUT_SECONDS, minimum=5, maximum=600
        ),
        order_batch_size=_bounded_int(
            data.get('order_batch_size'),
            default=DEFAULT_ORDER_BATCH_SIZE_LIMIT,
            minimum=1,
            maximum=DEFAULT_ORDER_BATCH_SIZE_LIMIT,
        ),
        max_workers=_bounded_int(data.get('max_workers'), default=2, minimum=1, maximum=8),
        verify_ssl=bool(data.get('verify_ssl', True)),
        sample_outbound_orders=max(0, _bounded_int(
            data.get('sample_outbound_orders'), default=0, minimum=0, maximum=100000
        )),
        sample_return_trackings=max(0, _bounded_int(
            data.get('sample_return_trackings'), default=0, minimum=0, maximum=100000
        )),
    )


@dataclass(slots=True)
class KecScanResult:
    invoice_amounts: dict[str, Decimal] = field(default_factory=dict)
    outbound_order_numbers: list[str] = field(default_factory=list)
    outbound_tracking_numbers_by_order: dict[str, str] = field(default_factory=dict)
    outbound_month: date | None = None
    outbound_order_count: int = 0
    return_tracking_numbers: list[str] = field(default_factory=list)
    return_quantity_by_order: dict[str, Decimal] = field(default_factory=dict)

    @property
    def avg_daily_orders(self) -> Decimal:
        if self.outbound_month is None or self.outbound_order_count <= 0:
            return Decimal('0')
        days_in_month = Decimal(str(calendar.monthrange(self.outbound_month.year, self.outbound_month.month)[1]))
        return Decimal(str(self.outbound_order_count)) / days_in_month


@dataclass(slots=True)
class KecQuoteRules:
    storage_price_day_0_30: Decimal = Decimal('0')
    storage_price_day_31_120: Decimal = Decimal('2')
    storage_price_day_121_180: Decimal = Decimal('3.5')
    storage_price_day_181_plus: Decimal = Decimal('5.1')
    outbound_fee_0_500: Decimal = Decimal('1.8')
    outbound_fee_501_1000: Decimal = Decimal('1.7')
    outbound_fee_1001_plus: Decimal = Decimal('1.5')
    outbound_extra_piece_fee: Decimal = Decimal('0.6')
    return_first_piece_fee: Decimal = Decimal('2')
    return_additional_piece_fee: Decimal = Decimal('1')
    unloading_lcl_unit_price: Decimal = Decimal('27')
    bubble_wrap_rates: dict[str, Decimal] = field(
        default_factory=lambda: {
            'ABF1': Decimal('0.5'),
            'ABF2': Decimal('0.6'),
            'ABF3': Decimal('0.9'),
            'ABF4': Decimal('1.1'),
            'ABF5': Decimal('1.6'),
            'ABF6': Decimal('1.8'),
            'ABF7': Decimal('2.7'),
            'ABF8': Decimal('3.6'),
            'ABF9': Decimal('4.3'),
            'ABF10': Decimal('5'),
            'ABF11': Decimal('6.4'),
            'ABF12': Decimal('6.5'),
        }
    )
    misc_unit_rates: dict[str, Decimal] = field(
        default_factory=lambda: {
            '库内订单拦截': Decimal('1.7'),
            '贴标费': Decimal('0.5'),
            '拍照': Decimal('0.9'),
            '录制视频<=1分钟': Decimal('2.6'),
            '录制视频<=3分钟': Decimal('4.5'),
            '组包费': Decimal('0.6'),
            '更换包装费': Decimal('2.7'),
            '退仓费': Decimal('1.1'),
            '销毁费': Decimal('0.6'),
        }
    )

    @classmethod
    def from_workbook(cls, quote_file: str | Path | None) -> 'KecQuoteRules':
        rules = cls()
        if not quote_file:
            return rules
        path = Path(quote_file)
        if not path.is_file():
            raise FileNotFoundError(f'KEC 报价表不存在: {path}')
        try:
            workbook = load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:
            raise ValueError(f'KEC 报价表无法打开，请确认文件未加密且格式正确: {path}') from exc
        try:
            sheet = workbook.worksheets[0]
            required_rates = (
                ('storage_price_day_0_30', 5, 9, '0-30天仓租'),
                ('storage_price_day_31_120', 6, 9, '31-120天仓租'),
                ('storage_price_day_121_180', 7, 9, '121-180天仓租'),
                ('storage_price_day_181_plus', 8, 9, '181天以上仓租'),
                ('outbound_fee_0_500', 9, 9, '0-500单出库费'),
                ('outbound_fee_501_1000', 10, 9, '501-1000单出库费'),
                ('outbound_fee_1001_plus', 11, 9, '1001单以上出库费'),
                ('outbound_extra_piece_fee', 9, 10, '出库续件费'),
                ('return_first_piece_fee', 12, 9, '退件首件费'),
                ('return_additional_piece_fee', 12, 10, '退件续件费'),
                ('unloading_lcl_unit_price', 24, 9, '散货卸货费'),
            )
            for attribute, row, column, label in required_rates:
                rate = _to_decimal(sheet.cell(row, column).value)
                if rate is None:
                    coordinate = f'R{row}C{column}'
                    raise ValueError(f'KEC 报价表缺少数字型「{label}」: {coordinate}')
                setattr(rules, attribute, rate)
            bubble_wrap_rates: dict[str, Decimal] = {}
            for row_index in range(43, 55):
                code = _text(sheet.cell(row_index, 11).value)
                rate = _to_decimal(sheet.cell(row_index, 9).value)
                if bool(code) != (rate is not None):
                    raise ValueError(
                        f'KEC 报价表气泡膜行不完整（代码与费率必须成对）: 第 {row_index} 行'
                    )
                if code and rate is not None:
                    bubble_wrap_rates[code.upper()] = rate
            if not bubble_wrap_rates:
                raise ValueError('KEC 报价表第 43-54 行没有读到任何气泡膜费率')
            rules.bubble_wrap_rates = bubble_wrap_rates
        finally:
            workbook.close()
        return rules

    def storage_price_for_age(self, age_days: int | None) -> Decimal | None:
        if age_days is None:
            return None
        if age_days <= 30:
            return self.storage_price_day_0_30
        if age_days <= 120:
            return self.storage_price_day_31_120
        if age_days <= 180:
            return self.storage_price_day_121_180
        return self.storage_price_day_181_plus

    def outbound_base_fee_for_avg_daily_orders(self, avg_daily_orders: Decimal) -> Decimal:
        if avg_daily_orders <= Decimal('500'):
            return self.outbound_fee_0_500
        if avg_daily_orders <= Decimal('1000'):
            return self.outbound_fee_501_1000
        return self.outbound_fee_1001_plus

    def return_fee_for_quantity(self, quantity: Decimal | int | None) -> Decimal:
        qty = _to_decimal(quantity, default=Decimal('0')) or Decimal('0')
        if qty <= 0:
            return Decimal('0')
        if qty <= 1:
            return self.return_first_piece_fee
        return self.return_first_piece_fee + (qty - Decimal('1')) * self.return_additional_piece_fee

    def bubble_wrap_fee_for_code(self, bubble_wrap_code: str) -> Decimal:
        return self.bubble_wrap_rates.get(_text(bubble_wrap_code).upper(), Decimal('0'))


class KecReconciliationService:
    """Reconcile every KEC fee section against the quote workbook."""

    def __init__(self, progress: ProgressCallback | None = None) -> None:
        self._progress = progress

    def _emit(self, message: str) -> None:
        if self._progress is not None:
            self._progress(message)

    def run(self, job: KecReconciliationJob) -> KecReconciliationResult:
        input_path = job.input_file
        if not input_path.exists():
            raise FileNotFoundError(f'KEC 供应商账单不存在: {input_path}')
        output_dir = job.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        self._emit(f'读取账单: {input_path.name}（{input_path.stat().st_size / 1048576:.1f} MB）')
        scan_result = self._scan_workbook(input_path)
        quote_rules = KecQuoteRules.from_workbook(job.quote_file)
        if job.quote_file is None:
            self._emit('未提供报价表，本次使用内置默认费率核对，请确认费率是否仍然有效')
        else:
            self._emit(f'使用报价表: {Path(job.quote_file).name}')
        outbound_base_fee = quote_rules.outbound_base_fee_for_avg_daily_orders(scan_result.avg_daily_orders)
        sampled_outbound_orders = self._slice_identifiers(scan_result.outbound_order_numbers, job.sample_outbound_orders)
        included_outbound_orders = set(sampled_outbound_orders) if job.sample_outbound_orders > 0 else None
        sampled_tracking_numbers_by_order = {
            order_number: scan_result.outbound_tracking_numbers_by_order[order_number]
            for order_number in sampled_outbound_orders
            if order_number in scan_result.outbound_tracking_numbers_by_order
        }
        sampled_return_trackings = self._slice_identifiers(scan_result.return_tracking_numbers, job.sample_return_trackings)
        included_return_trackings = set(sampled_return_trackings) if job.sample_return_trackings > 0 else None
        if included_outbound_orders is not None:
            self._emit(f'样本模式：本次只核对前 {len(included_outbound_orders)} 个出库订单')
        if included_return_trackings is not None:
            self._emit(f'样本模式：本次只核对前 {len(included_return_trackings)} 个退件货运单')
        self._emit(
            f'账单扫描完成：出库订单 {scan_result.outbound_order_count} 个，'
            f'月均日单量 {scan_result.avg_daily_orders}，出库操作费按 {outbound_base_fee} 元/票'
        )
        outbound_orders_by_order, outbound_orders_by_tracking = self._fetch_outbound_orders(
            job=job,
            order_numbers=sampled_outbound_orders,
            tracking_numbers_by_order=sampled_tracking_numbers_by_order,
        )
        return_lookup_results = self._fetch_return_lookups(
            job=job,
            tracking_numbers=sampled_return_trackings,
        )
        output_path = _unique_output_path(output_dir, f'{input_path.stem}{RESULT_FILENAME_SUFFIX}')
        summary_stats = self._write_result_workbook(
            input_path=input_path,
            output_path=output_path,
            scan_result=scan_result,
            quote_rules=quote_rules,
            outbound_base_fee=outbound_base_fee,
            outbound_orders_by_order=outbound_orders_by_order,
            outbound_orders_by_tracking=outbound_orders_by_tracking,
            return_lookup_results=return_lookup_results,
            included_outbound_orders=included_outbound_orders,
            included_return_trackings=included_return_trackings,
            sample_outbound_order_count=len(sampled_outbound_orders) if included_outbound_orders is not None else 0,
            sample_return_tracking_count=len(sampled_return_trackings) if included_return_trackings is not None else 0,
        )
        self._emit(f'核对结果已写入: {output_path.name}')
        return KecReconciliationResult(
            output_file=output_path,
            avg_daily_orders=scan_result.avg_daily_orders,
            outbound_base_fee=outbound_base_fee,
            summary=summary_stats,
        )

    def _resolve_source_sheets(self, workbook) -> dict[str, Any]:
        return {role: self._resolve_source_sheet(workbook, role) for role in WORKBOOK_SHEET_ALIASES}

    def _resolve_source_sheet(self, workbook, role: str):
        aliases = WORKBOOK_SHEET_ALIASES[role]
        for sheet_name in aliases:
            if sheet_name in workbook.sheetnames:
                return workbook[sheet_name]
        for fallback_index in WORKBOOK_SHEET_FALLBACK_INDEXES.get(role, ()):
            if 0 <= fallback_index < len(workbook.worksheets):
                fallback_sheet = workbook.worksheets[fallback_index]
                self._emit(
                    f'账单缺少子表 {"/".join(aliases)}，已按位置回退到第 {fallback_index + 1} 个子表'
                    f'（{fallback_sheet.title}），请人工确认'
                )
                LOGGER.warning(
                    'KEC workbook missing sheet names %s, fallback to index %s (%s)',
                    aliases,
                    fallback_index,
                    fallback_sheet.title,
                )
                return fallback_sheet
        raise ValueError(
            f'KEC 账单缺少「{ROLE_LABELS.get(role, role)}」子表（期望名称：{"/".join(aliases)}），请确认账单模板'
        )

    def _scan_workbook(self, input_path: Path) -> KecScanResult:
        workbook = load_workbook(input_path, read_only=True, data_only=True)
        try:
            sheets = self._resolve_source_sheets(workbook)
            scan_result = KecScanResult()
            self._emit('解析 Invoice 与出库/退件匹配字段...')
            scan_result.invoice_amounts = self._read_invoice_amounts(sheets['invoice'])
            scan_result.outbound_order_numbers, scan_result.outbound_tracking_numbers_by_order, scan_result.outbound_month = self._read_outbound_identifiers(sheets['outbound'])
            scan_result.outbound_order_count = len(scan_result.outbound_order_numbers)
            scan_result.return_tracking_numbers, scan_result.return_quantity_by_order = self._read_return_identifiers(sheets['return'])
            return scan_result
        finally:
            workbook.close()

    def _read_invoice_amounts(self, worksheet) -> dict[str, Decimal]:
        results: dict[str, Decimal] = {}
        label_mapping = {
            '仓储费': '仓储费',
            '卸货费': '卸货费',
            '出库+耗材': '出库+耗材',
            '退件入库（上架费）': '退件上架费',
            '退件上架费': '退件上架费',
            '增值服务费': '杂费',
            '赔付': '赔付',
        }
        for row in worksheet.iter_rows(min_row=17, max_row=40, values_only=True):
            description = _text(row[1] if len(row) > 1 else None)
            mapped = label_mapping.get(description)
            if not mapped:
                continue
            amount = _to_decimal(row[9] if len(row) > 9 else None)
            if amount is not None:
                results[mapped] = amount
        return results

    def _read_outbound_identifiers(self, worksheet) -> tuple[list[str], dict[str, str], date | None]:
        order_numbers: list[str] = []
        tracking_numbers_by_order: dict[str, str] = {}
        outbound_month: date | None = None
        seen_orders: set[str] = set()
        for row in _iter_non_empty_rows(worksheet, key_indexes=[5, 6]):
            order_number = _text(row[5] if len(row) > 5 else None)
            tracking_number = _text(row[6] if len(row) > 6 else None)
            if not order_number:
                continue
            if order_number not in seen_orders:
                seen_orders.add(order_number)
                order_numbers.append(order_number)
            if tracking_number and order_number not in tracking_numbers_by_order:
                tracking_numbers_by_order[order_number] = tracking_number
            if outbound_month is None:
                handover_dt = _parse_datetime(row[4] if len(row) > 4 else None)
                if handover_dt is not None:
                    outbound_month = handover_dt.date().replace(day=1)
        return order_numbers, tracking_numbers_by_order, outbound_month

    def _read_return_identifiers(self, worksheet) -> tuple[list[str], dict[str, Decimal]]:
        raw_headers = list(next(worksheet.iter_rows(min_row=1, max_row=1, values_only=True)))
        tracking_index = _find_header_index(raw_headers, include_keywords=('ASN #',), default=5)
        order_index = _find_header_index(raw_headers, include_keywords=('PO Number',), default=6)
        quantity_index = _find_header_index(raw_headers, include_keywords=('Actual QTY',), default=10)
        tracking_numbers: list[str] = []
        quantity_by_order: dict[str, Decimal] = defaultdict(lambda: Decimal('0'))
        seen_tracking_numbers: set[str] = set()
        for row in _iter_non_empty_rows(worksheet, key_indexes=[5, 7, 8]):
            tracking_number = _text(_row_value(row, tracking_index))
            order_number = _text(_row_value(row, order_index)) or tracking_number
            lookup_identifier = tracking_number or order_number
            if not lookup_identifier:
                continue
            if lookup_identifier not in seen_tracking_numbers:
                seen_tracking_numbers.add(lookup_identifier)
                tracking_numbers.append(lookup_identifier)
            if order_number:
                quantity_by_order[order_number] += _to_decimal(_row_value(row, quantity_index), default=Decimal('0')) or Decimal('0')
        return tracking_numbers, dict(quantity_by_order)

    def _fetch_outbound_orders(self, *, job: KecReconciliationJob, order_numbers: Iterable[str], tracking_numbers_by_order: dict[str, str]) -> tuple[dict[str, MabangOrderInfo], dict[str, MabangOrderInfo]]:
        ordered_order_numbers = list(dict.fromkeys(_text(value) for value in order_numbers if _text(value)))
        if not ordered_order_numbers:
            return {}, {}
        self._emit(f'登录马帮并回查 {len(ordered_order_numbers)} 个出库订单...')
        with MabangClient(job.base_url, timeout_seconds=job.timeout_seconds, verify_ssl=job.verify_ssl) as client:
            client.login(job.username, job.password)
            orders_by_order = client.search_orders_by_platform_order_ids(ordered_order_numbers, batch_size=max(1, min(job.order_batch_size, DEFAULT_ORDER_BATCH_SIZE)))
            missing_tracking_numbers = [tracking_numbers_by_order[order_number] for order_number in ordered_order_numbers if order_number not in orders_by_order and tracking_numbers_by_order.get(order_number)]
            self._emit(
                f'出库订单按订单号命中 {len(orders_by_order)} 个，'
                f'剩余 {len(missing_tracking_numbers)} 个改按货运单号回查'
            )
            orders_by_tracking = client.search_orders_by_tracking_numbers(missing_tracking_numbers, batch_size=max(1, min(job.order_batch_size, DEFAULT_ORDER_BATCH_SIZE)))
        return orders_by_order, orders_by_tracking

    def _fetch_return_lookups(self, *, job: KecReconciliationJob, tracking_numbers: Iterable[str]) -> dict[str, MabangLookupResult]:
        ordered_tracking_numbers = list(dict.fromkeys(_text(value) for value in tracking_numbers if _text(value)))
        if not ordered_tracking_numbers:
            return {}
        self._emit(
            f'查询 {len(ordered_tracking_numbers)} 个退件的订单/退货/退款记录'
            f'（{max(1, job.max_workers)} 个并发，耗时较长，请勿关闭窗口）...'
        )
        coordinator = MabangLookupCoordinator(base_url=job.base_url, timeout_seconds=job.timeout_seconds, verify_ssl=job.verify_ssl)
        results = coordinator.lookup_tracking_numbers(tracking_numbers=ordered_tracking_numbers, username=job.username, password=job.password, max_workers=max(1, job.max_workers))
        failed_count = sum(1 for item in results.values() if item.error_message)
        self._emit(f'退件查询完成：成功 {len(results) - failed_count} 个，失败 {failed_count} 个')
        return results

    def _slice_identifiers(self, identifiers: list[str], limit: int) -> list[str]:
        if limit <= 0:
            return list(identifiers)
        return list(identifiers[:limit])

    def _write_result_workbook(self, *, input_path: Path, output_path: Path, scan_result: KecScanResult, quote_rules: KecQuoteRules, outbound_base_fee: Decimal, outbound_orders_by_order: dict[str, MabangOrderInfo], outbound_orders_by_tracking: dict[str, MabangOrderInfo], return_lookup_results: dict[str, MabangLookupResult], included_outbound_orders: set[str] | None, included_return_trackings: set[str] | None, sample_outbound_order_count: int, sample_return_tracking_count: int) -> dict[str, Any]:
        workbook = load_workbook(input_path, read_only=True, data_only=True)
        try:
            sheets = self._resolve_source_sheets(workbook)
            output_workbook = Workbook(write_only=True)
            summary_stats: dict[str, Any] = {}
            self._emit('核对卸货费...')
            summary_stats.update(self._write_unloading_sheet(output_workbook.create_sheet('卸货费核对'), sheets['unloading'], quote_rules))
            self._emit(f'核对仓储费（核算基数：{ACTUAL_VOLUME_BASIS_LABEL}）...')
            summary_stats.update(self._write_storage_sheet(output_workbook.create_sheet('仓储费核对'), sheets['storage'], quote_rules))
            self._emit('核对出库+耗材...')
            summary_stats.update(self._write_outbound_sheet(output_workbook.create_sheet('出库耗材核对'), sheets['outbound'], quote_rules, outbound_base_fee, outbound_orders_by_order, outbound_orders_by_tracking, included_outbound_orders))
            self._emit('核对退件上架费...')
            summary_stats.update(self._write_return_sheet(output_workbook.create_sheet('退件上架费核对'), sheets['return'], quote_rules, scan_result.return_quantity_by_order, return_lookup_results, included_return_trackings))
            self._emit('核对杂费...')
            summary_stats.update(self._write_misc_sheet(output_workbook.create_sheet('杂费核对'), sheets['misc'], quote_rules))
            self._write_summary_sheet(output_workbook.create_sheet('汇总'), scan_result, summary_stats, sample_outbound_order_count=sample_outbound_order_count, sample_return_tracking_count=sample_return_tracking_count)
            self._emit('写出核对结果工作簿...')
            output_workbook.save(output_path)
            output_workbook.close()
            return summary_stats
        finally:
            workbook.close()

    def _write_unloading_sheet(self, worksheet, source_sheet, quote_rules: KecQuoteRules) -> dict[str, Any]:
        raw_headers = list(next(source_sheet.iter_rows(min_row=1, max_row=1, values_only=True)))
        cbm_index = _find_header_index(raw_headers, include_keywords=('方数',), default=3)
        billed_fee_index = _find_header_index(raw_headers, include_keywords=('卸货费',), default=5)
        worksheet.append(['账单行号', *raw_headers, '核对方数取整', '核对单价', '核对卸货费', '金额差额', '核对结果', '核对说明'])
        billed_total = Decimal('0')
        expected_total = Decimal('0')
        abnormal_count = 0
        for row_number, row in enumerate(_iter_non_empty_rows(source_sheet, key_indexes=[1]), start=2):
            cbm = _to_decimal(row[cbm_index] if len(row) > cbm_index else None)
            billed_fee = _to_decimal(row[billed_fee_index] if len(row) > billed_fee_index else None, default=Decimal('0')) or Decimal('0')
            rounded_cbm = _round_up_cubic_meter(cbm)
            expected_fee = None if rounded_cbm is None else rounded_cbm * quote_rules.unloading_lcl_unit_price
            diff_amount = _decimal_diff(billed_fee, expected_fee)
            status = '正常' if _is_zero(diff_amount) else '异常'
            notes = '' if status == '正常' else '卸货费与方数向上取整规则不一致'
            if status == '异常':
                abnormal_count += 1
            billed_total += billed_fee
            expected_total += expected_fee or Decimal('0')
            worksheet.append([row_number, *list(row[: len(raw_headers)]), _excel_decimal(rounded_cbm), _excel_decimal(quote_rules.unloading_lcl_unit_price), _excel_decimal(expected_fee), _excel_decimal(diff_amount), status, notes])
        return {'卸货费_账单金额': billed_total, '卸货费_核对金额': expected_total, '卸货费_异常数': abnormal_count}
    def _write_storage_sheet(self, worksheet, source_sheet, quote_rules: KecQuoteRules) -> dict[str, Any]:
        """Reconcile storage fees on the captured actual volume of each row.

        The basis used to be the bill's ``Total CBM within period`` column, which
        is the warehouse's rounded *billing* volume.  It is now the actual volume
        measured from the bill's own dimensions (长 × 宽 × 高 × 数量 × 0.000001),
        so the reported difference is exactly what the billing-volume rounding
        adds on top of the goods' real volume.
        """

        raw_headers = list(next(source_sheet.iter_rows(min_row=1, max_row=1, values_only=True)))
        length_index = _find_header_index(raw_headers, include_keywords=('Unit Length',), default=7)
        width_index = _find_header_index(raw_headers, include_keywords=('Unit Width',), default=8)
        height_index = _find_header_index(raw_headers, include_keywords=('Unit Height',), default=9)
        unit_cbm_index = _find_header_index(raw_headers, include_keywords=('Unit Billing CBM',), default=10)
        inbound_time_index = _find_header_index(raw_headers, include_keywords=('入库时间',), default=14)
        bill_time_index = _find_header_index(raw_headers, include_keywords=('计费时间',), default=15)
        quantity_index = _find_header_index(raw_headers, include_keywords=('PCS',), default=16)
        total_cbm_index = _find_header_index(raw_headers, include_keywords=('Total CBM',), default=17)
        age_index = _find_header_index(raw_headers, include_keywords=('库龄',), default=18)
        unit_price_index = _find_header_index(raw_headers, include_keywords=('单价',), default=19)
        amount_index = _find_header_index(raw_headers, include_keywords=('金额',), default=20)
        worksheet.append([
            '账单行号', *raw_headers,
            '核对数量', f'核算基数({ACTUAL_VOLUME_BASIS_LABEL})', '账单 Total CBM', '体积差额',
            '核对库龄', '库龄差额', '核对单价', '单价差额',
            '核对金额', '金额差额', '核对结果', '核对说明',
        ])
        billed_total = Decimal('0')
        expected_total = Decimal('0')
        volume_diff_total = Decimal('0')
        abnormal_count = 0
        missing_dimension_count = 0
        for row_number, row in enumerate(_iter_non_empty_rows(source_sheet, key_indexes=[4, 12]), start=2):
            length = _to_decimal(_row_value(row, length_index))
            width = _to_decimal(_row_value(row, width_index))
            height = _to_decimal(_row_value(row, height_index))
            unit_billing_cbm = _to_decimal(_row_value(row, unit_cbm_index))
            billed_quantity = _to_decimal(_row_value(row, quantity_index))
            inbound_dt = _parse_datetime(_row_value(row, inbound_time_index))
            bill_dt = _parse_datetime(_row_value(row, bill_time_index))
            billed_total_cbm = _to_decimal(_row_value(row, total_cbm_index))
            billed_age = _to_int(_row_value(row, age_index))
            billed_unit_price = _to_decimal(_row_value(row, unit_price_index))
            billed_amount = _to_decimal(_row_value(row, amount_index), default=Decimal('0')) or Decimal('0')
            actual_cbm = _calculate_actual_cbm(length, width, height, billed_quantity)
            expected_quantity = _calculate_storage_quantity(unit_billing_cbm, billed_total_cbm)
            expected_age = _calculate_storage_age_days(inbound_dt, bill_dt)
            expected_unit_price = quote_rules.storage_price_for_age(expected_age)
            expected_amount = None if expected_unit_price is None or actual_cbm is None else actual_cbm * expected_unit_price
            quantity_diff = _decimal_diff(billed_quantity, expected_quantity)
            volume_diff = _decimal_diff(billed_total_cbm, actual_cbm)
            age_diff = None if billed_age is None or expected_age is None else Decimal(str(billed_age - expected_age))
            unit_price_diff = _decimal_diff(billed_unit_price, expected_unit_price)
            amount_diff = _decimal_diff(billed_amount, expected_amount)
            notes: list[str] = []
            if actual_cbm is None:
                missing_dimension_count += 1
                notes.append('长宽高或数量为空，无法计算实际体积')
            if expected_quantity is not None and not _is_zero(quantity_diff):
                notes.append('数量与 Total CBM / Unit Billing CBM 反算不一致')
            if expected_age is None:
                notes.append('入库时间或计费时间为空')
            elif not _is_zero(age_diff):
                notes.append('库龄与自然日口径不一致')
            if expected_unit_price is not None and not _is_zero(unit_price_diff):
                notes.append('仓储单价与库龄分档不一致')
            if expected_amount is not None and not _is_zero(amount_diff):
                notes.append(f'金额与 {ACTUAL_VOLUME_BASIS_LABEL}×单价 不一致')
            status = '正常' if not notes else '异常'
            if status == '异常':
                abnormal_count += 1
            billed_total += billed_amount
            expected_total += expected_amount or Decimal('0')
            volume_diff_total += volume_diff or Decimal('0')
            worksheet.append([
                row_number, *list(row[: len(raw_headers)]),
                _excel_decimal(expected_quantity), _excel_decimal(actual_cbm), _excel_decimal(billed_total_cbm), _excel_decimal(volume_diff),
                expected_age, _excel_decimal(age_diff), _excel_decimal(expected_unit_price), _excel_decimal(unit_price_diff),
                _excel_decimal(expected_amount), _excel_decimal(amount_diff), status, '; '.join(notes),
            ])
        return {
            '仓储费_账单金额': billed_total,
            '仓储费_核对金额': expected_total,
            '仓储费_异常数': abnormal_count,
            '仓储费_体积差额合计': volume_diff_total,
            '仓储费_缺失尺寸行数': missing_dimension_count,
        }

    def _write_outbound_sheet(self, worksheet, source_sheet, quote_rules: KecQuoteRules, outbound_base_fee: Decimal, outbound_orders_by_order: dict[str, MabangOrderInfo], outbound_orders_by_tracking: dict[str, MabangOrderInfo], included_outbound_orders: set[str] | None = None) -> dict[str, Any]:
        raw_headers = list(next(source_sheet.iter_rows(min_row=1, max_row=1, values_only=True)))
        worksheet.append(['账单行号', *raw_headers, '马帮命中方式', '马帮订单号', '马帮货运单号', '马帮订单状态', '马帮发货时间', '马帮商品种类', '马帮商品总数量', '订单号匹配', '货运单号匹配', '发货日期匹配', '数量匹配', '核对包材费', '包材费差额', '核对出库操作费', '出库操作费差额', '核对续件费', '续件费差额', '核对总金额', '总金额差额', '核对结果', '核对说明'])
        billed_total = Decimal('0')
        expected_total = Decimal('0')
        abnormal_count = 0
        missing_count = 0
        quantity_mismatch_count = 0
        for row_number, row in enumerate(_iter_non_empty_rows(source_sheet, key_indexes=[5, 6]), start=2):
            raw_order_number = _text(row[5] if len(row) > 5 else None)
            if included_outbound_orders is not None and raw_order_number not in included_outbound_orders:
                continue
            raw_tracking_number = _text(row[6] if len(row) > 6 else None)
            raw_handover_dt = _parse_datetime(row[4] if len(row) > 4 else None)
            raw_qty = _to_decimal(row[8] if len(row) > 8 else None, default=Decimal('0')) or Decimal('0')
            bubble_wrap_code = _text(row[17] if len(row) > 17 else None)
            raw_packaging_fee = _to_decimal(row[24] if len(row) > 24 else None, default=Decimal('0')) or Decimal('0')
            raw_outbound_fee = _to_decimal(row[25] if len(row) > 25 else None, default=Decimal('0')) or Decimal('0')
            raw_extra_fee = _to_decimal(row[26] if len(row) > 26 else None, default=Decimal('0')) or Decimal('0')
            raw_total_amount = _to_decimal(row[28] if len(row) > 28 else None, default=Decimal('0')) or Decimal('0')
            matched_order = outbound_orders_by_order.get(raw_order_number)
            hit_type = '订单号'
            if matched_order is None and raw_tracking_number:
                matched_order = outbound_orders_by_tracking.get(raw_tracking_number)
                hit_type = '货运单号回补'
            mabang_total_quantity = matched_order.item_total_quantity if matched_order else None
            mabang_item_kind_count = matched_order.item_kind_count if matched_order else None
            mabang_status = (matched_order.show_order_status_text or matched_order.platform_order_status or matched_order.order_status) if matched_order else ''
            mabang_shipping_time = matched_order.shipping_time_text if matched_order else ''
            order_match = matched_order is not None and raw_order_number == matched_order.platform_order_id
            tracking_match = matched_order is not None and raw_tracking_number == matched_order.tracking_number
            shipping_match = _same_date(raw_handover_dt, _parse_datetime(mabang_shipping_time)) if matched_order else False
            quantity_match = mabang_total_quantity is not None and raw_qty == Decimal(str(mabang_total_quantity))
            expected_packaging_fee = quote_rules.bubble_wrap_fee_for_code(bubble_wrap_code)
            qty_for_fee = Decimal(str(mabang_total_quantity)) if mabang_total_quantity is not None else raw_qty
            expected_extra_fee = Decimal('0') if qty_for_fee <= Decimal('3') else (qty_for_fee - Decimal('3')) * quote_rules.outbound_extra_piece_fee
            expected_total_amount = expected_packaging_fee + outbound_base_fee + expected_extra_fee
            packaging_diff = _decimal_diff(raw_packaging_fee, expected_packaging_fee)
            outbound_diff = _decimal_diff(raw_outbound_fee, outbound_base_fee)
            extra_diff = _decimal_diff(raw_extra_fee, expected_extra_fee)
            total_diff = _decimal_diff(raw_total_amount, expected_total_amount)
            notes: list[str] = []
            if matched_order is None:
                missing_count += 1
                notes.append('马帮未找到订单')
            elif hit_type == '货运单号回补':
                notes.append('订单号未命中，已按货运单号回补')
            if matched_order is not None and not order_match:
                notes.append('订单号与马帮结果不一致')
            if matched_order is not None and not tracking_match:
                notes.append('货运单号与马帮结果不一致')
            if matched_order is not None and mabang_total_quantity is None:
                notes.append('未取到马帮商品总数量，续件费按账单数量核')
            elif matched_order is not None and not quantity_match:
                quantity_mismatch_count += 1
                notes.append('Order Handover QTY 与马帮商品总数量不一致')
            if matched_order is not None and not shipping_match:
                notes.append('发货日期与马帮结果不一致')
            if bubble_wrap_code and expected_packaging_fee == Decimal('0') and raw_packaging_fee != Decimal('0'):
                notes.append('气泡膜代码未在报价表找到')
            if not _is_zero(packaging_diff):
                notes.append('包材费与气泡膜报价不一致')
            if not _is_zero(outbound_diff):
                notes.append('出库操作费与单量分档不一致')
            if not _is_zero(extra_diff):
                notes.append('续件费与商品总数量不一致')
            if not _is_zero(total_diff):
                notes.append('总金额与分项核对结果不一致')
            status = '正常' if matched_order is not None and not notes else ('待确认' if matched_order is not None and notes == ['未取到马帮商品总数量，续件费按账单数量核'] else '异常')
            if status == '异常':
                abnormal_count += 1
            billed_total += raw_total_amount
            expected_total += expected_total_amount
            worksheet.append([row_number, *list(row[: len(raw_headers)]), '' if matched_order is None else hit_type, matched_order.platform_order_id if matched_order else '', matched_order.tracking_number if matched_order else '', mabang_status, mabang_shipping_time, mabang_item_kind_count, mabang_total_quantity, _yes_no(order_match), _yes_no(tracking_match), _yes_no(shipping_match), _yes_no(quantity_match) if mabang_total_quantity is not None else '', _excel_decimal(expected_packaging_fee), _excel_decimal(packaging_diff), _excel_decimal(outbound_base_fee), _excel_decimal(outbound_diff), _excel_decimal(expected_extra_fee), _excel_decimal(extra_diff), _excel_decimal(expected_total_amount), _excel_decimal(total_diff), status, '; '.join(notes)])
        return {'出库+耗材_账单金额': billed_total, '出库+耗材_核对金额': expected_total, '出库+耗材_异常数': abnormal_count, '出库+耗材_未命中订单数': missing_count, '出库+耗材_数量不一致数': quantity_mismatch_count}

    def _write_return_sheet(self, worksheet, source_sheet, quote_rules: KecQuoteRules, return_quantity_by_order: dict[str, Decimal], return_lookup_results: dict[str, MabangLookupResult], included_return_trackings: set[str] | None = None) -> dict[str, Any]:
        raw_headers = list(next(source_sheet.iter_rows(min_row=1, max_row=1, values_only=True)))
        tracking_index = _find_header_index(raw_headers, include_keywords=('ASN #',), default=5)
        order_index = _find_header_index(raw_headers, include_keywords=('PO Number',), default=6)
        is_return_index = _find_header_index(raw_headers, include_keywords=('Is Return',), default=7)
        sku_index = _find_header_index(raw_headers, include_keywords=('SKU Code',), default=8)
        amount_index = _find_header_index(raw_headers, include_keywords=('Amount',), default=20)
        worksheet.append(['账单行号', *raw_headers, '订单+SKU匹配键', '马帮订单号', '马帮货运单号', '马帮订单状态', '马帮退款标识', '马帮发货时间', '退货命中', '退款命中', '订单退货总数量', '核对上架费', '金额差额', '售后判定', '核对结果', '核对说明'])
        billed_total = Decimal('0')
        expected_total = Decimal('0')
        abnormal_count = 0
        manual_review_count = 0
        seen_order_numbers: set[str] = set()
        for row_number, row in enumerate(_iter_non_empty_rows(source_sheet, key_indexes=[5, 7, 8]), start=2):
            tracking_number = _text(_row_value(row, tracking_index))
            order_number = _text(_row_value(row, order_index)) or tracking_number
            lookup_identifier = tracking_number or order_number
            if included_return_trackings is not None and lookup_identifier not in included_return_trackings:
                continue
            sku_code = _text(_row_value(row, sku_index))
            order_sku_key = f'{order_number}+{sku_code}' if order_number or sku_code else ''
            raw_amount = _to_decimal(_row_value(row, amount_index), default=Decimal('0')) or Decimal('0')
            group_quantity = return_quantity_by_order.get(order_number, Decimal('0'))
            expected_amount = quote_rules.return_fee_for_quantity(group_quantity) if order_number not in seen_order_numbers else Decimal('0')
            seen_order_numbers.add(order_number)
            amount_diff = _decimal_diff(raw_amount, expected_amount)
            lookup = return_lookup_results.get(lookup_identifier) or return_lookup_results.get(order_number)
            order = lookup.order if lookup else None
            after_sale_judgement = '未查询'
            if lookup is not None:
                if lookup.error_message:
                    after_sale_judgement = '查询失败'
                elif lookup.has_return_hit and lookup.has_refund_hit:
                    after_sale_judgement = '退货退款命中'
                elif lookup.has_return_hit:
                    after_sale_judgement = '退货命中'
                elif lookup.has_refund_hit:
                    after_sale_judgement = '退款命中'
                elif order is None:
                    after_sale_judgement = '马帮未找到订单'
                else:
                    after_sale_judgement = '需后台确认'
            notes: list[str] = []
            if _text(_row_value(row, is_return_index)).upper() != 'Y':
                notes.append('账单未标记 Is Return=Y')
            if lookup is None:
                notes.append('未发起马帮查询')
            elif lookup.error_message:
                notes.append(lookup.error_message)
            elif after_sale_judgement in {'需后台确认', '马帮未找到订单'}:
                manual_review_count += 1
                notes.append('退货列表和退款列表均未命中，需要后台确认')
            if not _is_zero(amount_diff):
                notes.append('上架费与首件2元续件1元规则不一致')
            status = '正常'
            if lookup is None or lookup.error_message or not _is_zero(amount_diff):
                status = '异常'
            elif after_sale_judgement in {'需后台确认', '马帮未找到订单'}:
                status = '待确认'
            if status == '异常':
                abnormal_count += 1
            billed_total += raw_amount
            expected_total += expected_amount
            worksheet.append([row_number, *list(row[: len(raw_headers)]), order_sku_key, order.platform_order_id if order else '', order.tracking_number if order else '', (order.show_order_status_text or order.platform_order_status or order.order_status) if order else '', order.refund_flag if order else '', order.shipping_time_text if order else '', _yes_no(lookup.has_return_hit) if lookup else '', _yes_no(lookup.has_refund_hit) if lookup else '', _excel_decimal(group_quantity), _excel_decimal(expected_amount), _excel_decimal(amount_diff), after_sale_judgement, status, '; '.join(notes)])
        return {'退件上架费_账单金额': billed_total, '退件上架费_核对金额': expected_total, '退件上架费_异常数': abnormal_count, '退件上架费_待确认数': manual_review_count}
    def _write_misc_sheet(self, worksheet, source_sheet, quote_rules: KecQuoteRules) -> dict[str, Any]:
        raw_headers = list(next(source_sheet.iter_rows(min_row=1, max_row=1, values_only=True)))
        description_index = _find_header_index(raw_headers, include_keywords=('费用说明',), default=3)
        quantity_index = _find_header_index(raw_headers, include_keywords=('数量',))
        amount_index = _find_header_index(raw_headers, include_keywords=('总费用',))
        if amount_index is None:
            amount_index = _find_header_index(raw_headers, include_keywords=('金额',), exclude_keywords=('总金额',))
        if amount_index is None:
            amount_index = _find_header_index(raw_headers, include_keywords=('费用',), exclude_keywords=('费用说明', '耗材类型'), default=4)
        worksheet.append(['账单行号', *raw_headers, '核对单价', '核对费用', '金额差额', '规则命中', '核对结果', '核对说明'])
        billed_total = Decimal('0')
        expected_total = Decimal('0')
        abnormal_count = 0
        unknown_rule_count = 0
        for row_number, row in enumerate(_iter_non_empty_rows(source_sheet, key_indexes=[1, 3]), start=2):
            description = _text(row[description_index] if len(row) > description_index else None)
            quantity = _to_decimal(row[quantity_index] if quantity_index is not None and len(row) > quantity_index else None, default=Decimal('1')) or Decimal('1')
            billed_amount = _to_decimal(row[amount_index] if len(row) > amount_index else None, default=Decimal('0')) or Decimal('0')
            rule_key, unit_rate = _match_misc_rate(description, quote_rules.misc_unit_rates)
            expected_amount = None if unit_rate is None else quantity * unit_rate
            diff_amount = _decimal_diff(billed_amount, expected_amount)
            if unit_rate is None:
                unknown_rule_count += 1
                status = '待确认'
                note = '报价规则未覆盖该费用说明'
            elif _is_zero(diff_amount):
                status = '正常'
                note = ''
            else:
                abnormal_count += 1
                status = '异常'
                note = '杂费与报价单价不一致'
            billed_total += billed_amount
            expected_total += expected_amount or Decimal('0')
            worksheet.append([row_number, *list(row[: len(raw_headers)]), _excel_decimal(unit_rate), _excel_decimal(expected_amount), _excel_decimal(diff_amount), rule_key, status, note])
        return {'杂费_账单金额': billed_total, '杂费_核对金额': expected_total, '杂费_异常数': abnormal_count, '杂费_待确认数': unknown_rule_count}

    def _write_summary_sheet(self, worksheet, scan_result: KecScanResult, summary_stats: dict[str, Any], *, sample_outbound_order_count: int = 0, sample_return_tracking_count: int = 0) -> None:
        worksheet.append(['项目', '账单金额', '核对金额', '差额', '异常/待确认', '备注'])
        rows = [
            ('仓储费', '仓储费_账单金额', '仓储费_核对金额', '仓储费_异常数', f'核算基数改为{ACTUAL_VOLUME_BASIS_LABEL}：长 × 宽 × 高 × 数量 × 0.000001；按 入库日期 -> 计费日期 自然日 + 1 核对库龄；数量仍按 Total CBM / Unit Billing CBM 反算核对'),
            ('卸货费', '卸货费_账单金额', '卸货费_核对金额', '卸货费_异常数', '按方数向上取整 * 27 计算'),
            ('出库+耗材', '出库+耗材_账单金额', '出库+耗材_核对金额', '出库+耗材_异常数', f'按月均日单量 {scan_result.avg_daily_orders} 采用按票费率' + (f'；本次仅核对前 {sample_outbound_order_count} 个订单样本' if sample_outbound_order_count else '')),
            ('退件上架费', '退件上架费_账单金额', '退件上架费_核对金额', '退件上架费_待确认数', '明细按订单+SKU匹配，费用按订单退货总数量计算；首件 2 元，续件 1 元；未命中退货/退款的单子需要后台确认' + (f'；本次仅核对前 {sample_return_tracking_count} 个退件货运单样本' if sample_return_tracking_count else '')),
            ('杂费', '杂费_账单金额', '杂费_核对金额', '杂费_待确认数', '仅按已覆盖报价规则核对，未覆盖费用保留待确认'),
        ]
        for label, billed_key, expected_key, count_key, note in rows:
            billed_amount = _to_decimal(summary_stats.get(billed_key))
            expected_amount = _to_decimal(summary_stats.get(expected_key))
            worksheet.append([label, _excel_decimal(billed_amount), _excel_decimal(expected_amount), _excel_decimal(_decimal_diff(billed_amount, expected_amount)), summary_stats.get(count_key, 0), note])
        worksheet.append([])
        worksheet.append(['Invoice 项目', 'Invoice 金额'])
        for label in ['仓储费', '卸货费', '出库+耗材', '退件上架费', '杂费', '赔付']:
            worksheet.append([label, _excel_decimal(scan_result.invoice_amounts.get(label))])
        worksheet.append([])
        worksheet.append(['辅助统计', '值'])
        worksheet.append(['仓储费核算基数', ACTUAL_VOLUME_BASIS_LABEL + '（长 × 宽 × 高 × 数量 × 0.000001）'])
        worksheet.append(['仓储费体积差额合计(CBM)', _excel_decimal(summary_stats.get('仓储费_体积差额合计'))])
        worksheet.append(['仓储费缺失尺寸行数', summary_stats.get('仓储费_缺失尺寸行数', 0)])
        worksheet.append(['出库订单数', scan_result.outbound_order_count])
        worksheet.append(['出库月均日单量', _excel_decimal(scan_result.avg_daily_orders)])
        worksheet.append(['出库未命中订单数', summary_stats.get('出库+耗材_未命中订单数', 0)])
        worksheet.append(['出库数量不一致数', summary_stats.get('出库+耗材_数量不一致数', 0)])
        worksheet.append(['退件待确认数', summary_stats.get('退件上架费_待确认数', 0)])
        if sample_outbound_order_count:
            worksheet.append(['出库样本订单数', sample_outbound_order_count])
        if sample_return_tracking_count:
            worksheet.append(['退件样本追踪单数', sample_return_tracking_count])
        worksheet.append([])
        worksheet.append(['差额 = 账单 - 核对；体积差额为正表示账单计费体积大于实际体积，金额差额为正表示账单金额大于按实际体积核算的金额'])


def _iter_non_empty_rows(worksheet, *, key_indexes: list[int], min_row: int = 2, empty_streak_limit: int = 200) -> Iterable[tuple[Any, ...]]:
    empty_streak = 0
    for row in worksheet.iter_rows(min_row=min_row, values_only=True):
        if not row:
            empty_streak += 1
            if empty_streak >= empty_streak_limit:
                break
            continue
        has_key = any(_text(row[index]) for index in key_indexes if index < len(row))
        if not has_key and not any(_text(cell) for cell in row[: max(key_indexes) + 3]):
            empty_streak += 1
            if empty_streak >= empty_streak_limit:
                break
            continue
        empty_streak = 0
        if has_key:
            yield row


def _find_header_index(headers: list[Any], *, include_keywords: tuple[str, ...], exclude_keywords: tuple[str, ...] = (), default: int | None = None) -> int | None:
    normalized_includes = tuple(_normalize_header_text(value) for value in include_keywords)
    normalized_excludes = tuple(_normalize_header_text(value) for value in exclude_keywords)
    for index, header in enumerate(headers):
        normalized_header = _normalize_header_text(header)
        if not normalized_header:
            continue
        if all(keyword in normalized_header for keyword in normalized_includes) and all(keyword not in normalized_header for keyword in normalized_excludes):
            return index
    return default


def _normalize_header_text(value: Any) -> str:
    return _text(value).replace('\n', '').replace('\r', '').replace(' ', '').lower()


def _round_up_cubic_meter(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    rounded = Decimal(str(int(value)))
    if value != rounded:
        rounded = Decimal(str(int(value) + 1))
    return Decimal('1') if rounded < 1 else rounded


def _calculate_storage_age_days(inbound_dt: datetime | None, bill_dt: datetime | None) -> int | None:
    if inbound_dt is None or bill_dt is None:
        return None
    delta_days = (bill_dt.date() - inbound_dt.date()).days + 1
    return delta_days if delta_days >= 0 else None


def _calculate_storage_quantity(unit_billing_cbm: Decimal | None, total_cbm: Decimal | None) -> Decimal | None:
    if unit_billing_cbm is None or total_cbm is None or unit_billing_cbm == Decimal('0'):
        return None
    return (total_cbm / unit_billing_cbm).quantize(Decimal('0.0001'))


def calculate_actual_volume(
    length_cm: Decimal | None,
    width_cm: Decimal | None,
    height_cm: Decimal | None,
    quantity: Decimal | None,
) -> Decimal | None:
    """Return the captured actual volume (m³) of one bill row.

    ``长 × 宽 × 高`` is measured in centimetres and ``0.000001`` converts the
    result to cubic metres.  Any missing input yields ``None`` rather than a
    silent zero, so the row is reported instead of under-counting the bill.
    """

    if length_cm is None or width_cm is None or height_cm is None or quantity is None:
        return None
    if length_cm < 0 or width_cm < 0 or height_cm < 0 or quantity < 0:
        return None
    return length_cm * width_cm * height_cm * quantity * CUBIC_METER_SCALE


_calculate_actual_cbm = calculate_actual_volume


def _match_misc_rate(description: str, misc_unit_rates: dict[str, Decimal]) -> tuple[str, Decimal | None]:
    normalized = description.replace(' ', '')
    if not normalized:
        return '', None
    if '拦截' in normalized:
        return '库内订单拦截', misc_unit_rates.get('库内订单拦截')
    if '贴标' in normalized:
        return '贴标费', misc_unit_rates.get('贴标费')
    if '拍照' in normalized:
        return '拍照', misc_unit_rates.get('拍照')
    if '录制视频' in normalized:
        if '3分钟' in normalized or '超1分钟' in normalized:
            return '录制视频<=3分钟', misc_unit_rates.get('录制视频<=3分钟')
        return '录制视频<=1分钟', misc_unit_rates.get('录制视频<=1分钟')
    if '组包' in normalized:
        return '组包费', misc_unit_rates.get('组包费')
    if '更换包装' in normalized or '换包装' in normalized:
        return '更换包装费', misc_unit_rates.get('更换包装费')
    if '退仓' in normalized:
        return '退仓费', misc_unit_rates.get('退仓费')
    if '销毁' in normalized:
        return '销毁费', misc_unit_rates.get('销毁费')
    return '', None


def _same_date(left: datetime | None, right: datetime | None) -> bool:
    return left is not None and right is not None and left.date() == right.date()


def _yes_no(value: bool) -> str:
    return '是' if value else '否'


def _is_zero(value: Decimal | None) -> bool:
    return value is None or abs(value) <= COMPARE_TOLERANCE


def _decimal_diff(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None or right is None:
        return None
    return left - right


def _excel_decimal(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _row_value(row: tuple[Any, ...], index: int | None) -> Any:
    if index is None or index >= len(row):
        return None
    return row[index]


def _to_decimal(value: Any, *, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == '':
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return default
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        pass
    # Supplier cells sometimes carry a unit or thousands separator, e.g. "27cm".
    match = NUMERIC_PREFIX_PATTERN.search(str(value).replace(',', ''))
    if not match:
        return default
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return default


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value if value not in (None, '') else default)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _unique_output_path(output_dir: Path, suggested_name: str) -> Path:
    """Never overwrite a previous reconciliation workbook."""

    safe_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', Path(suggested_name).name).strip(' .')
    if not safe_name.endswith('.xlsx'):
        safe_name = f'KEC核对结果{safe_name}.xlsx'
    candidate = output_dir / safe_name
    counter = 2
    while candidate.exists():
        candidate = output_dir / f'{Path(safe_name).stem}_{counter}{Path(safe_name).suffix}'
        counter += 1
    return candidate


def run_kec_reconciliation(
    job: KecReconciliationJob | dict[str, Any],
    progress: ProgressCallback | None = None,
) -> KecReconciliationResult:
    """Run the complete KEC reconciliation workflow."""

    if isinstance(job, dict):
        job = validate_kec_reconciliation_payload(job)
    if not isinstance(job, KecReconciliationJob):
        raise TypeError('job 必须是 KecReconciliationJob 或请求字典')
    return KecReconciliationService(progress=progress).run(job)


def _to_int(value: Any) -> int | None:
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return None
    try:
        return int(decimal_value)
    except (ValueError, TypeError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if value in {None, ''}:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text_value = _text(value)
    for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d']:
        try:
            return datetime.strptime(text_value, fmt)
        except ValueError:
            continue
    return None


def _text(value: Any) -> str:
    return '' if value is None else str(value).strip()


__all__ = [
    'ACTUAL_VOLUME_BASIS_LABEL',
    'CUBIC_METER_SCALE',
    'DEFAULT_MABANG_BASE_URL',
    'KecQuoteRules',
    'KecReconciliationJob',
    'KecReconciliationResult',
    'KecReconciliationService',
    'KecScanResult',
    'KecWorkbookPreview',
    'calculate_actual_volume',
    'inspect_kec_source_workbook',
    'run_kec_reconciliation',
    'validate_kec_reconciliation_payload',
]
