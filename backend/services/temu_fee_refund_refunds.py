"""Validate the observed TEMU refunded return export without changing its data."""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook


HEADERS = (
    '售后单号', '售后单类型', '国家/地区', '订单号', '订单状态', 'skuID',
    '售后创建时间', '售后原因', '售后件数', '售后单状态', '售后申请金额', '退款金额',
)
REQUIRED_VALUES = ('售后单号', '售后单类型', '国家/地区', '订单号', 'skuID', '售后创建时间', '售后单状态')
EXPORT_LIMIT = 10000


def _text(value) -> str:
    return '' if value is None else str(value).strip()


def _range_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError('售后导出日期范围无效') from exc


def _created_date(value) -> date:
    """Compare the platform's displayed calendar date, without timezone shifts."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    # The real export uses e.g. 2026年9月26日 19:39 CST. ISO-formatted
    # timestamps may be produced by Excel when resaving an equivalent file.
    matched = re.fullmatch(
        r'(\d{4})(?:年|-)(\d{1,2})(?:月|-)(\d{1,2})(?:日)?'
        r'(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?(?:\s+CST)?', _text(value),
    )
    if not matched:
        raise ValueError('售后创建时间格式无法确认')
    year, month, day, hour, minute, second = matched.groups()
    try:
        return datetime(int(year), int(month), int(day), int(hour or 0),
                        int(minute or 0), int(second or 0)).date()
    except ValueError as exc:
        raise ValueError('售后创建时间不是有效日期') from exc


def validate_refund_file(path: str | Path, start, end) -> int:
    """Return all valid SKU rows; zero/negative/blank monetary cells are retained."""
    start, end = _range_date(start), _range_date(end)
    if start > end:
        raise ValueError('开始日期不能晚于结束日期')
    book = load_workbook(Path(path), read_only=True, data_only=False)
    try:
        if len(book.worksheets) != 1:
            raise ValueError('售后导出工作表数量异常，无法确认文件类型')
        sheet = book.worksheets[0]
        sheet.reset_dimensions()
        rows = sheet.iter_rows()
        header = next(rows, ())
        names = [_text(cell.value).casefold() for cell in header]
        nonempty = [name for name in names if name]
        if len(nonempty) != len(set(nonempty)):
            raise ValueError('售后导出表头重复，无法唯一识别列')
        missing = [label for label in HEADERS if label.casefold() not in names]
        if missing:
            raise ValueError('售后导出文件缺少必要表头：' + '、'.join(missing))
        positions = {name: names.index(name.casefold()) for name in HEADERS}
        count = 0
        for row_number, cells in enumerate(rows, 2):
            if all(cell.value is None for cell in cells):
                continue
            if any(cell.data_type == 'f' for cell in cells):
                raise ValueError(f'售后导出第 {row_number} 行包含公式，无法确认原始数据')
            values = {name: cells[index].value if index < len(cells) else None for name, index in positions.items()}
            missing = [name for name in REQUIRED_VALUES if not _text(values[name])]
            if missing:
                raise ValueError(f'售后导出第 {row_number} 行缺少必填值：' + '、'.join(missing))
            if _text(values['售后单类型']) != '退货退款':
                raise ValueError(f'售后导出第 {row_number} 行不是退货退款类型')
            if _text(values['售后单状态']) != '已退款':
                raise ValueError(f'售后导出第 {row_number} 行不是已退款状态')
            try:
                created = _created_date(values['售后创建时间'])
            except ValueError as exc:
                raise ValueError(f'售后导出第 {row_number} 行{exc}') from exc
            if not start <= created <= end:
                raise ValueError(f'售后导出第 {row_number} 行创建日期超出请求范围')
            # One after-sales case may have several SKU rows. No deduplication
            # and no monetary filtering are valid for this platform export.
            count += 1
            if count >= EXPORT_LIMIT:
                raise ValueError('售后导出达到平台 10,000 条上限，无法确认结果完整，请缩小日期范围')
        return count
    finally:
        book.close()
