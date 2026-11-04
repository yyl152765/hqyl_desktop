"""Read exact seller-SKU price floors from a local Excel reference workbook."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ROWS = 100_000
MAX_COLUMNS = 200

_SELLER_HEADERS = {"sellersku", "卖家sku", "商家sku"}
_SKU_HEADERS = {"sku", "商品sku", "子sku", "规格sku", "商家编码", "商品编码", "规格编码"}
_PRICE_HEADERS = {
    "price", "referenceprice", "minimumprice", "minprice", "floorprice", "skuprice",
    "价格", "参考价格", "参考价", "最低价", "底价", "最低价格", "最低活动价",
    "sku价格", "活动底价", "最低售价", "最低销售价格", "最低销售价",
}
_NOTE_TITLES = {"说明", "字段说明", "使用说明", "字段定义", "备注", "instructions", "notes"}


def _header(value: Any) -> str:
    first_line = unicodedata.normalize("NFKC", str(value or "")).lstrip("\ufeff").strip().split("\n", 1)[0]
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", first_line.lower())
    return re.sub(r"(?:mandatory|required|optional|必填|选填)$", "", text)


def _sku_columns(row: Any) -> list[int]:
    seller = [cell.column for cell in row if _header(cell.value) in _SELLER_HEADERS]
    return seller or [cell.column for cell in row if _header(cell.value) in _SKU_HEADERS]


def _is_price_header(value: Any) -> bool:
    text = _header(value)
    if text in _PRICE_HEADERS:
        return True
    return any(text.startswith(name) and re.fullmatch(r"(?:cny|rmb|usd|php|thb|myr|vnd|元|价格单位)", text[len(name):])
               for name in _PRICE_HEADERS)


def find_activity_sku_column(sheet: Any, header_row: int) -> int:
    """Prefer an explicit seller SKU over generic SKU; never substitute platform IDs."""
    if isinstance(header_row, bool) or not isinstance(header_row, int) or header_row < 1:
        raise ValueError("活动表 SKU 表头行号无效")
    columns = _sku_columns(sheet[header_row])
    if not columns:
        raise ValueError(f"工作表「{sheet.title}」缺少卖家SKU/SKU表头，不能自动使用 Sku ID 或店铺SKU匹配")
    if len(columns) != 1:
        raise ValueError(f"工作表「{sheet.title}」第 {header_row} 行 SKU 表头重复或有歧义，请只保留一个同类 SKU 列")
    return columns[0]


def sku_value(cell: Any) -> str:
    """Normalize representation without changing SKU case, punctuation or text zeros."""
    value = cell.value
    if cell.data_type in {"f", "e"}:
        raise ValueError(f"{cell.coordinate} SKU 含公式或 Excel 错误，请先粘贴为文本值")
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("="):
            raise ValueError(f"{cell.coordinate} SKU 含公式，请先粘贴为文本值")
        return text
    if isinstance(value, (bool, date, time)) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{cell.coordinate} SKU 必须是文本或不超过15位的非负整数")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{cell.coordinate} SKU 数字无效，请改为文本") from exc
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError(f"{cell.coordinate} SKU 数字必须是非负整数，请将完整 SKU 作为文本填写")
    text = str(int(number))
    if len(text) > 15:
        raise ValueError(f"{cell.coordinate} SKU 数字超过 Excel 的15位精度，请使用原始文本 SKU")
    number_format = str(cell.number_format or "")
    if re.fullmatch(r"0+", number_format):
        text = text.zfill(len(number_format))
    return text


def _reference_layout(sheet: Any) -> tuple[int, int, int] | None:
    first = {_header(cell.value) for cell in sheet[1]}
    if (first & {"field", "字段", "字段名"}) and (first & {"description", "说明", "描述", "字段说明"}):
        return None
    incomplete = []
    has_values = False
    has_table_row = False
    for row in sheet.iter_rows(max_row=min(10, sheet.max_row), max_col=sheet.max_column):
        filled = sum(cell.value is not None and bool(str(cell.value).strip()) for cell in row)
        has_values = has_values or bool(filled)
        has_table_row = has_table_row or filled >= 2
        skus = _sku_columns(row)
        prices = [cell.column for cell in row if _is_price_header(cell.value)]
        if skus and prices:
            if len(skus) != 1:
                raise ValueError(f"工作表「{sheet.title}」第 {row[0].row} 行 SKU 表头重复或有歧义")
            if len(prices) != 1:
                raise ValueError(f"工作表「{sheet.title}」第 {row[0].row} 行价格表头重复或有歧义")
            return row[0].row, skus[0], prices[0]
        if skus or prices:
            incomplete.append((row[0].row, "价格" if skus else "卖家SKU/SKU"))
    if incomplete:
        row, missing = incomplete[0]
        raise ValueError(f"工作表「{sheet.title}」第 {row} 行缺少{missing}表头")
    if not has_values or not has_table_row:
        # An out-of-range header must not make an additional data sheet look
        # empty. Inspect stored cells without allocating the entire sheet grid.
        filled_by_row: dict[int, int] = {}
        for cell in sheet._cells.values():
            if cell.value is not None and str(cell.value).strip():
                has_values = True
                filled_by_row[cell.row] = filled_by_row.get(cell.row, 0) + 1
                if filled_by_row[cell.row] >= 2:
                    has_table_row = True
                    break
    if has_values and (has_table_row or sheet.title.strip().lower() not in _NOTE_TITLES):
        raise ValueError(f"工作表「{sheet.title}」前10行未找到完整的 SKU 和价格表头")
    return None


def load_sku_prices(path: Path) -> dict[str, Decimal]:
    """Validate all reference rows before returning the price floor for each SKU."""
    # The main workbook service imports this module, so load its price parser
    # only when called after module initialization has completed.
    from backend.services.lazada_campaign_workbook import _read_price

    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise ValueError("请选择有效的 SKU 价格表")
    source = Path(path).expanduser().resolve()
    if source.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError("SKU 价格表仅支持 .xlsx 或 .xlsm")
    if not source.is_file():
        raise ValueError(f"SKU 价格表不存在：{source.name}")
    if not 0 < source.stat().st_size <= MAX_FILE_BYTES:
        raise ValueError("SKU 价格表必须非空且不超过50 MiB")
    try:
        book = load_workbook(source, keep_links=False, keep_vba=source.suffix.lower() == ".xlsm")
    except Exception as exc:
        raise ValueError(f"无法读取 SKU 价格表：{source.name}，请确认文件未损坏或加密") from exc
    prices: dict[str, Decimal] = {}
    origins: dict[str, tuple[str, int]] = {}
    try:
        for sheet in book:
            if sheet.max_row > MAX_ROWS or sheet.max_column > MAX_COLUMNS:
                raise ValueError(f"SKU 价格表工作表「{sheet.title}」超过 {MAX_ROWS:,} 行或 {MAX_COLUMNS} 列")
            layout = _reference_layout(sheet)
            if layout is None:
                continue
            header_row, sku_column, price_column = layout
            for row in sheet.iter_rows(min_row=header_row + 1):
                if not any(cell.value is not None and str(cell.value).strip() for cell in row):
                    continue
                number = row[0].row
                try:
                    sku = sku_value(sheet.cell(number, sku_column))
                    if not sku:
                        raise ValueError("缺少 SKU")
                    price = _read_price(sheet.cell(number, price_column))
                except ValueError as exc:
                    raise ValueError(f"SKU 价格表工作表「{sheet.title}」第 {number} 行：{exc}") from exc
                if sku in prices and prices[sku] != price:
                    first_sheet, first_row = origins[sku]
                    raise ValueError(f"SKU「{sku}」价格冲突：工作表「{first_sheet}」第 {first_row} 行为 {prices[sku]}，"
                                     f"工作表「{sheet.title}」第 {number} 行为 {price}；请修正价格表后重试")
                prices[sku] = price
                origins.setdefault(sku, (sheet.title, number))
    finally:
        book.close()
        if book.vba_archive is not None:
            book.vba_archive.close()
    if not prices:
        raise ValueError("SKU 价格表没有可用的 SKU 和价格数据")
    return prices
