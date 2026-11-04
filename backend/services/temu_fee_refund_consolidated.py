"""Combine verified store exports into the user's three-sheet report layout."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path
from uuid import uuid4
import csv
import os

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


SHEETS = {'shipping': '发货面单费', 'return_label': '退货面单费', 'refund': '退货退款'}
HEADERS = {
    'shipping': ['运营', '店铺名', '包裹号', '运单号', '服务商code', '账单类型', '备注',
                 '运费(单位元)-支出为负、退款为正', '币种', '对账单状态', '支出/退款时间(时区：GMT+8)'],
    'return_label': ['运营', '店铺', '账务单ID', '运单号', 'PO单号', '账单类型', '币种', '账单备注', '金额', '账单支出/退款时间'],
    'refund': ['月份', '运营', '店铺名', '售后单号', '售后单类型', '国家/地区', '订单号', '订单状态',
               'skuID', '售后创建时间', '售后原因', '售后件数', '售后单状态', '售后申请金额', '退款金额'],
}
MAX_DATA_ROWS = 1_048_575
MAX_COLUMNS = 16_384


def period_label(start, end):
    first, last = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    if first > last:
        raise ValueError('合并报表日期范围无效')
    return f'{first.month}月' if (first.year, first.month) == (last.year, last.month) else f'{first.isoformat()}~{last.isoformat()}'


def _key(value):
    label = str(value or '').strip()
    return {'运营名': '运营', '店铺': '店铺名'}.get(label, label).casefold()


def _confined_file(root, value):
    path = Path(str(value or ''))
    resolved = (root / path).resolve()
    if not value or path.is_absolute() or not resolved.is_relative_to((root / 'results').resolve()):
        raise ValueError('合并来源文件超出本次导出目录')
    if not resolved.is_file():
        raise ValueError('合并来源文件不存在')
    return resolved


@contextmanager
def _rows(path):
    if path.suffix.lower() == '.xlsx':
        book = load_workbook(path, read_only=True, data_only=False)
        try:
            if len(book.worksheets) != 1:
                raise ValueError('逐店报表必须只有一个工作表')
            sheet = book.worksheets[0]
            sheet.reset_dimensions()

            def values():
                for row in sheet.iter_rows():
                    if any(cell.data_type == 'f' for cell in row):
                        raise ValueError('逐店报表包含公式，无法确认原始数据')
                    yield [cell.value for cell in row]

            iterator = values()
            try:
                yield iterator
            finally:
                iterator.close()
        finally:
            book.close()
    elif path.suffix.lower() == '.csv':
        import io
        raw = path.read_bytes()
        try:
            content = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            content = raw.decode('gb18030')
        yield csv.reader(io.StringIO(content, newline=''))
    else:
        raise ValueError('合并仅支持已校验的 XLSX 或 CSV 逐店报表')


def _header(rows, required):
    labels = [str(value or '').strip() for value in next(rows, ())]
    keys = [_key(value) for value in labels]
    named = [key for key in keys if key]
    if len(named) != len(set(named)):
        raise ValueError('逐店报表表头重复，无法安全合并')
    missing = [label for label in required if _key(label) not in named]
    if missing:
        raise ValueError('逐店报表缺少必要列：' + '、'.join(missing))
    return labels, {key: i for i, key in enumerate(keys) if key}


def _cell(sheet, value):
    cell = WriteOnlyCell(sheet, value=value)
    # Literal identifiers must never be converted to formulas or numbers.
    if isinstance(value, str):
        cell.data_type = 's'
    return cell


def create_consolidated_workbook(manifest_path, record):
    """Return relative output metadata without modifying the supplied manifest."""
    root = Path(manifest_path).resolve().parent
    month = period_label(record['start_date'], record['end_date'])
    stores = record.get('stores') or []
    entries = {kind: [] for kind in SHEETS}
    headers = {kind: list(labels) for kind, labels in HEADERS.items()}
    counts = {kind: 0 for kind in SHEETS}
    complete, usable = True, False
    for store in stores:
        for kind in SHEETS:
            item = store.get('exports', {}).get(kind, {})
            if item.get('status') == 'no_data':
                if item.get('row_count') != 0:
                    raise ValueError('无数据结果的行数必须为零')
                usable = True
                continue
            if item.get('status') != 'success':
                complete = False
                continue
            usable = True
            count = item.get('row_count')
            if type(count) is not int or count < 0:
                raise ValueError('逐店报表缺少有效行数')
            source = _confined_file(root, item.get('file'))
            required = HEADERS[kind][1:] if kind == 'refund' else HEADERS[kind]
            with _rows(source) as rows:
                labels, _ = _header(rows, required)
            if kind == 'refund' and any(_key(label) == _key('月份') for label in labels):
                raise ValueError('逐店退款报表已有月份列，无法安全添加本次期间')
            known = {_key(label) for label in headers[kind]}
            for label in labels:
                if label and _key(label) not in known:
                    headers[kind].append(label)
                    known.add(_key(label))
            if len(headers[kind]) > MAX_COLUMNS:
                raise ValueError('合并报表超过 Excel 列数上限')
            counts[kind] += count
            if counts[kind] > MAX_DATA_ROWS:
                raise ValueError('合并报表超过 Excel 行数上限，请缩小日期或店铺范围')
            entries[kind].append((store, item, source))
    if not usable:
        raise ValueError('没有已完成或明确无数据的结果可合并')

    suffix = '' if complete else '_部分完成'
    filename = f"TEMU面单费与退货退款_{record['start_date']}_{record['end_date']}{suffix}.xlsx"
    destination = root / 'results' / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError('合并总表已存在，请创建新批次，避免覆盖已有结果')
    temporary = destination.with_name('.' + uuid4().hex + '.xlsx')
    book = Workbook(write_only=True)
    try:
        for kind, title in SHEETS.items():
            sheet = book.create_sheet(title)
            sheet.freeze_panes = 'D2' if kind == 'refund' else 'C2'
            labels = headers[kind]
            for column, label in enumerate(labels, 1):
                width = 48 if _key(label) == '店铺名' else 14 if label in {'运营', '月份'} else 28
                if label == '月份' and '~' in month:
                    width = 26
                if '时间' in label or '日期' in label:
                    width = 30
                sheet.column_dimensions[get_column_letter(column)].width = width
            header_cells = [_cell(sheet, label) for label in labels]
            for cell in header_cells:
                cell.font = Font(name='Microsoft YaHei', size=10, bold=True)
                cell.fill = PatternFill('solid', fgColor='E7E6E6')
                cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
            sheet.row_dimensions[1].height = 32
            sheet.append(header_cells)
            total = 0
            for store, item, source in entries[kind]:
                required = HEADERS[kind][1:] if kind == 'refund' else HEADERS[kind]
                with _rows(source) as rows:
                    source_labels, positions = _header(rows, required)
                    count = 0
                    for values in rows:
                        if not any(value is not None and value != '' for value in values):
                            continue
                        if any(value is not None and value != '' for index, value in enumerate(values)
                               if index >= len(source_labels) or not source_labels[index]):
                            raise ValueError('逐店报表有无表头数据列，无法安全合并')
                        take = lambda label: values[positions[_key(label)]] if _key(label) in positions and positions[_key(label)] < len(values) else None
                        if take('运营') != store['operator_name'] or take('店铺名') != store['store_name']:
                            raise ValueError('逐店报表运营或店铺名与本次清单不一致')
                        result = [month if kind == 'refund' and label == '月份' else take(label) for label in labels]
                        sheet.append([_cell(sheet, value) for value in result])
                        count += 1
                        if total + count > MAX_DATA_ROWS:
                            raise ValueError('合并报表超过 Excel 行数上限')
                    if count != item['row_count']:
                        raise ValueError(f"{store['store_name']} {title}实际行数与本次清单不一致")
                    total += count
            if total != counts[kind]:
                raise ValueError('合并总表行数不一致')
            sheet.auto_filter.ref = f'A1:{get_column_letter(len(labels))}{total + 1}'
        book.save(temporary)
        os.replace(temporary, destination)
    finally:
        for sheet in book.worksheets:
            if not sheet.closed:
                sheet.close()
        book.close()
        temporary.unlink(missing_ok=True)
    return {'status': 'success' if complete else 'partial', 'file': str(destination.relative_to(root)), 'row_counts': counts}
