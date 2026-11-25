"""Read both Mabang inventory sales layouts without changing delivery columns."""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd


LEGACY_SALES_COLUMN = "销量(7/28/42)"
SPLIT_SALES_COLUMNS = ("销量(7)", "销量(28)", "销量(42)")
SKU_COLUMN = "库存SKU编号"
SKU_DTYPES = {SKU_COLUMN: str, "SKU": str, "库存SKU": str}


def _sales_integer(value: object) -> int:
    if isinstance(value, bool) or pd.isna(value):
        raise ValueError("销量必须是非负整数，不能留空")
    text = str(value).strip()
    # Old exports use comma thousands separators. Reject malformed grouping.
    if "," in text:
        if not re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.0+)?", text):
            raise ValueError("销量千位分隔格式无效")
        text = text.replace(",", "")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("销量不是有效数值") from exc
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError("销量必须是有限的非负整数")
    return int(number)


def _parse_sales_column(series: pd.Series, *, source: str, combined: bool) -> list[object]:
    parsed = []
    for row_number, value in enumerate(series, start=2):
        try:
            if combined:
                if not isinstance(value, str) or len(value.split("/")) != 3:
                    raise ValueError("销量组合必须包含 7/28/42 三个分量")
                parsed.append(tuple(_sales_integer(part) for part in value.split("/")))
            else:
                parsed.append(_sales_integer(value))
        except ValueError as exc:
            raise ValueError(f"{source}: 第 {row_number} 行 {series.name}: {exc}") from exc
    return parsed


def normalize_inventory_sales(frame: pd.DataFrame, *, source: str) -> pd.DataFrame:
    """Validate sales and return the legacy column layout in a separate frame.

    Complete old/new layouts and matching dual layouts are accepted. Invalid or
    partially present layouts fail before aggregation can treat missing sales as
    zero. New sales columns replace the old column only inside the input copy;
    downloaded source files are never rewritten.
    """
    source = str(source)
    duplicates = frame.columns[frame.columns.duplicated()].tolist()
    if duplicates:
        raise ValueError(f"{source}: 库存表存在重复列: {duplicates}")
    has_legacy = LEGACY_SALES_COLUMN in frame.columns
    present = [column for column in SPLIT_SALES_COLUMNS if column in frame.columns]
    if present and len(present) != len(SPLIT_SALES_COLUMNS):
        missing = [column for column in SPLIT_SALES_COLUMNS if column not in present]
        raise ValueError(f"{source}: 新版库存表缺少必需销量列: {missing}")
    if not has_legacy and not present:
        raise ValueError(
            f"{source}: 缺少 {LEGACY_SALES_COLUMN} 或完整的 {list(SPLIT_SALES_COLUMNS)}"
        )

    old_values = (
        _parse_sales_column(frame[LEGACY_SALES_COLUMN], source=source, combined=True)
        if has_legacy else None
    )
    new_values = (
        list(zip(*[
            _parse_sales_column(frame[column], source=source, combined=False)
            for column in SPLIT_SALES_COLUMNS
        ]))
        if present else None
    )
    if old_values is not None and new_values is not None:
        for row_number, (old, new) in enumerate(zip(old_values, new_values), start=2):
            if old != new:
                raise ValueError(
                    f"{source}: 第 {row_number} 行 {LEGACY_SALES_COLUMN} "
                    f"与 {list(SPLIT_SALES_COLUMNS)} 不一致"
                )
    values = old_values if old_values is not None else new_values
    result = frame.drop(columns=present).copy()
    serialized = ["/".join(str(number) for number in parts) for parts in values]
    if has_legacy:
        result[LEGACY_SALES_COLUMN] = pd.Series(serialized, index=result.index, dtype=object)
    else:
        first_sales = min(frame.columns.get_loc(column) for column in present)
        result.insert(first_sales, LEGACY_SALES_COLUMN, serialized)
    return result


def read_inventory_excel(
    path: str | Path, *, allow_duplicate_skus: bool = False
) -> pd.DataFrame:
    """Read a warehouse without losing textual SKU identity.

    Location-level exports may explicitly retain repeated SKUs. Their caller
    must preserve or aggregate that grain according to the existing business
    contract before using the result as a SKU-keyed merge table.
    """
    source = str(path)
    with pd.ExcelFile(path) as workbook:
        # Check the actual header before pandas can mangle duplicate labels.
        header = pd.read_excel(workbook, header=None, nrows=1)
        if header.empty:
            raise ValueError(f"{source}: 库存表为空，缺少 {SKU_COLUMN} 和销量表头")
        labels = pd.Index(header.iloc[0].tolist())
        duplicates = labels[labels.duplicated()].tolist()
        if duplicates:
            raise ValueError(f"{source}: 库存表存在重复列: {duplicates}")
        # Mabang may contain literal SKUs such as "null" and "NA". pandas'
        # default NA vocabulary would erase those identifiers. Only genuinely
        # empty cells are missing; invalid textual sales still fail below.
        frame = pd.read_excel(workbook, dtype=SKU_DTYPES, keep_default_na=False, na_values=[""])
    if SKU_COLUMN not in frame:
        raise ValueError(f"{source}: 缺少 {SKU_COLUMN}")
    sku = frame[SKU_COLUMN]
    empty = sku.isna() | sku.astype(str).str.strip().eq("")
    if empty.any():
        raise ValueError(f"{source}: {SKU_COLUMN} 包含空值")
    if not allow_duplicate_skus and sku.duplicated().any():
        raise ValueError(f"{source}: {SKU_COLUMN} 存在重复值，无法安全合并库存")
    result = normalize_inventory_sales(frame, source=source)
    logging.getLogger(__name__).info(
        "库存表结构校验通过: file=%s layout=%s rows=%d",
        Path(path).name,
        "split" if SPLIT_SALES_COLUMNS[0] in frame else "combined",
        len(result),
    )
    return result
