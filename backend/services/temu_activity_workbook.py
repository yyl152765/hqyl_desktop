"""Read and verify the TEMU activity registration template that the seller backend exports.

The export is the only input for every later step (price matching, review package,
batch splitting), so it is verified here instead of trusting the page: sheet names,
header text, site coverage and the four separate counts (SPU / SKU / quote rows /
stock rows) are read from the file itself.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook


QUOTE_SHEET = "活动申报价格"
STOCK_SHEET = "活动库存"

# Header text is matched on a bracket/space-insensitive form.  The platform writes
# 「活动类型（活动主题）」 today; older exports drop the suffix, so both are accepted
# while any other change still fails closed.
QUOTE_COLUMNS = {
    "topic": {"活动类型活动主题", "活动类型", "活动主题"},
    "spu_id": {"SPUID"},
    "skc_id": {"SKCID"},
    "skc_code": {"SKC货号"},
    "sku_id": {"SKUID"},
    "sku_code": {"SKU货号"},
    "color": {"颜色属性"},
    "size": {"尺码属性"},
    "site": {"站点"},
    "price": {"活动申报价格"},
    "currency": {"币种"},
}
STOCK_COLUMNS = {
    "topic": {"活动类型活动主题", "活动类型", "活动主题"},
    "spu_id": {"SPUID"},
    "stock": {"活动库存"},
}

SITE_DISPLAY = {
    "PH": {"菲律宾", "菲律宾站"},
    "MY": {"马来西亚", "马来站", "马来西亚站"},
    "TH": {"泰国", "泰国站"},
}
SITE_NAMES = {"PH": "菲律宾", "MY": "马来西亚", "TH": "泰国"}
CLEARANCE_KEYWORDS = ("清仓", "清货", "甩卖", "清库存")
MAX_TOPICS = 200


class WorkbookFormatError(ValueError):
    """The downloaded file is not the activity registration template."""


def as_text(value) -> str:
    """Return an identifier exactly as the platform stored it, never as a float."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value).rstrip("0").rstrip(".")
    return str(value).strip()


def normalize_header(value) -> str:
    return re.sub(r"[\s\u3000（）()\[\]【】]", "", as_text(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _header_columns(header_row, spec: dict[str, set[str]], sheet_name: str) -> dict[str, int]:
    actual = [normalize_header(cell.value) for cell in header_row]
    located: dict[str, int] = {}
    for field, accepted in spec.items():
        hits = [index for index, text in enumerate(actual) if text in accepted]
        if len(hits) != 1:
            shown = [text for text in actual if text]
            raise WorkbookFormatError(
                f"「{sheet_name}」表头无法唯一识别 {field}（实际表头：{'、'.join(shown) or '空'}）"
            )
        located[field] = hits[0]
    return located


def _row_values(row, columns: dict[str, int]) -> dict[str, str]:
    values = {}
    for field, index in columns.items():
        values[field] = as_text(row[index].value if index < len(row) else None)
    return values


def _data_rows(sheet, columns: dict[str, int], key_fields: tuple[str, ...]):
    rows = sheet.iter_rows(min_row=2)
    for row in rows:
        values = _row_values(row, columns)
        if not any(values[field] for field in key_fields):
            continue
        yield values


def inspect_export(path: str | Path) -> dict:
    """Return the four independent counts plus site/topic coverage of the export."""
    path = Path(path).resolve()
    if not path.is_file():
        raise WorkbookFormatError("导出文件不存在")
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - surface any openpyxl parse failure as a format error
        raise WorkbookFormatError(f"导出文件无法作为 Excel 读取：{exc}") from None
    try:
        names = list(workbook.sheetnames)
        missing = [name for name in (QUOTE_SHEET, STOCK_SHEET) if name not in names]
        if missing:
            raise WorkbookFormatError(
                f"导出文件缺少工作表：{'、'.join(missing)}（实际：{'、'.join(names) or '空'}）"
            )
        quote_sheet = workbook[QUOTE_SHEET]
        stock_sheet = workbook[STOCK_SHEET]
        quote_columns = _header_columns(next(quote_sheet.iter_rows(min_row=1, max_row=1)), QUOTE_COLUMNS, QUOTE_SHEET)
        stock_columns = _header_columns(next(stock_sheet.iter_rows(min_row=1, max_row=1)), STOCK_COLUMNS, STOCK_SHEET)

        sites: Counter[str] = Counter()
        topics: dict[str, int] = {}
        spu_ids: set[str] = set()
        sku_ids: set[str] = set()
        sku_codes: set[str] = set()
        currencies: set[str] = set()
        quote_rows = 0
        blank_prices = 0
        clearance_rows = 0
        for values in _data_rows(quote_sheet, quote_columns, ("topic", "spu_id", "sku_id")):
            quote_rows += 1
            sites[values["site"]] += 1
            topic = values["topic"]
            topics[topic] = topics.get(topic, 0) + 1
            if any(keyword in topic for keyword in CLEARANCE_KEYWORDS):
                clearance_rows += 1
            if values["spu_id"]:
                spu_ids.add(values["spu_id"])
            if values["sku_id"]:
                sku_ids.add(values["sku_id"])
            if values["sku_code"]:
                sku_codes.add(values["sku_code"])
            if values["currency"]:
                currencies.add(values["currency"].upper())
            if not values["price"]:
                blank_prices += 1

        stock_topics: dict[str, int] = {}
        stock_spus: set[str] = set()
        stock_rows = 0
        for values in _data_rows(stock_sheet, stock_columns, ("topic", "spu_id")):
            stock_rows += 1
            stock_topics[values["topic"]] = stock_topics.get(values["topic"], 0) + 1
            if values["spu_id"]:
                stock_spus.add(values["spu_id"])
    finally:
        workbook.close()

    ordered_topics = sorted(topics.items(), key=lambda item: (-item[1], item[0]))
    return {
        "sha256": sha256_file(path),
        "file_size": path.stat().st_size,
        "sheet_names": names,
        "quote_rows": quote_rows,
        "stock_rows": stock_rows,
        "spu_count": len(spu_ids),
        "sku_id_count": len(sku_ids),
        "sku_code_count": len(sku_codes),
        "topic_count": len(topics),
        "stock_topic_count": len(stock_topics),
        "clearance_rows": clearance_rows,
        "blank_price_rows": blank_prices,
        "currencies": sorted(currencies),
        "sites": dict(sites),
        "topics": [{"topic": topic, "rows": rows} for topic, rows in ordered_topics[:MAX_TOPICS]],
        "topics_truncated": len(ordered_topics) > MAX_TOPICS,
        "stock_spu_count": len(stock_spus),
    }


def site_matches(display: str, code: str) -> bool:
    return display.strip() in SITE_DISPLAY.get(code, set())


def foreign_sites(sites: dict[str, str], selected: list[str]) -> list[str]:
    """Return exported site labels that the operator did not select."""
    return [label for label in sites if not any(site_matches(label, code) for code in selected)]
