from __future__ import annotations

import csv
import os
from pathlib import Path
from uuid import uuid4

from openpyxl import load_workbook
from openpyxl.utils.cell import get_column_letter, range_boundaries


def _identity(value, label):
    value = str(value or '').strip()
    if not value:
        raise ValueError(f'缺少{label}，无法生成带身份信息的导出文件')
    return value


def _shift_range(value):
    min_col, min_row, max_col, max_row = range_boundaries(value)
    return f'{get_column_letter(min_col + 2)}{min_row}:{get_column_letter(max_col + 2)}{max_row}'


def _xlsx(source: Path, temporary: Path, operator: str, store: str) -> int:
    book = load_workbook(source, data_only=False)
    total = 0
    try:
        for sheet in book.worksheets:
            # openpyxl does not update these references when columns are inserted.
            # Refuse rather than silently corrupt a platform template.
            if (sheet.merged_cells.ranges or sheet.tables or sheet.data_validations.count
                    or len(sheet.conditional_formatting) or sheet._charts or sheet._images):
                raise ValueError(f'工作表「{sheet.title}」含复杂布局，无法安全插入列')
            if any(cell.data_type == 'f' for row in sheet.iter_rows() for cell in row):
                raise ValueError(f'工作表「{sheet.title}」含公式，无法安全插入列')
            rows = [row for row in sheet.iter_rows() if any(cell.value is not None for cell in row)]
            if not rows:
                continue
            if rows[0][0].row != 1:
                raise ValueError(f'工作表「{sheet.title}」首行不是表头')
            data_rows = [row[0].row for row in rows[1:]]
            old_filter = sheet.auto_filter.ref
            old_freeze = sheet.freeze_panes
            # Preserve original column widths, hidden status and formatting metadata.
            dimensions = list(sheet.column_dimensions.items())
            sheet.column_dimensions.clear()
            for key, dimension in dimensions:
                from openpyxl.utils.cell import column_index_from_string
                new_key = get_column_letter(column_index_from_string(key) + 2)
                dimension.index = new_key
                if dimension.min is not None:
                    dimension.min += 2
                if dimension.max is not None:
                    dimension.max += 2
                sheet.column_dimensions[new_key] = dimension
            sheet.insert_cols(1, 2)
            if old_filter:
                sheet.auto_filter.ref = _shift_range(old_filter)
            if old_freeze:
                from openpyxl.utils.cell import coordinate_from_string, column_index_from_string
                col, row = coordinate_from_string(old_freeze)
                sheet.freeze_panes = f'{get_column_letter(column_index_from_string(col) + 2)}{row}'
            sheet.cell(1, 1, '运营名')
            sheet.cell(1, 2, '店铺名')
            for row in data_rows:
                for col, value in ((1, operator), (2, store)):
                    cell = sheet.cell(row, col, value)
                    cell.data_type = 's'
            total += len(data_rows)
        if not any(sheet['A1'].value == '运营名' and sheet['B1'].value == '店铺名' for sheet in book.worksheets):
            raise ValueError('导出文件没有可识别的表头')
        book.save(temporary)
    finally:
        book.close()
    check = load_workbook(temporary, read_only=True, data_only=False)
    try:
        if not any(sheet['A1'].value == '运营名' and sheet['B1'].value == '店铺名' for sheet in check.worksheets):
            raise ValueError('输出文件校验失败')
    finally:
        check.close()
    return total


def _csv(source: Path, temporary: Path, operator: str, store: str) -> int:
    if any(value.startswith(('=', '+', '-', '@')) for value in (operator, store)):
        raise ValueError('运营名和店铺名不能以公式字符开头')
    raw = source.read_bytes()
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            contents = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('无法识别 CSV 编码')
    rows = list(csv.reader(contents.splitlines(keepends=True)))
    if not rows or not rows[0] or not any(rows[0]):
        raise ValueError('导出文件没有可识别的表头')
    total = 0
    with temporary.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['运营名', '店铺名', *rows[0]])
        for row in rows[1:]:
            if any(row):
                writer.writerow([operator, store, *row])
                total += 1
            else:
                writer.writerow(['', '', *row])
    with temporary.open(encoding='utf-8-sig', newline='') as stream:
        if next(csv.reader(stream), [])[:2] != ['运营名', '店铺名']:
            raise ValueError('输出文件校验失败')
    return total


def add_store_columns(source: str | Path, destination: str | Path, operator_name: str, store_name: str) -> int:
    """Create a separate export with identity columns; leave the original untouched."""
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    operator = _identity(operator_name, '运营名')
    store = _identity(store_name, '店铺名')
    if not source.is_file() or not source.stat().st_size:
        raise ValueError('原始导出文件不存在或为空')
    if source == destination:
        raise ValueError('输出路径不能覆盖原始导出文件')
    if source.suffix.lower() not in {'.xlsx', '.csv'} or source.suffix.lower() != destination.suffix.lower():
        raise ValueError('仅支持与原文件格式一致的 .xlsx 或 .csv 输出')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f'.{destination.stem}.{uuid4().hex}{destination.suffix}')
    try:
        count = (_xlsx if source.suffix.lower() == '.xlsx' else _csv)(source, temporary, operator, store)
        os.replace(temporary, destination)
        return count
    finally:
        temporary.unlink(missing_ok=True)
