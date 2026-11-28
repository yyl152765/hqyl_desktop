"""Validated classic report pagination shared by desktop sales services.

The page's filtered detail rows are authoritative; tableFoot and SKU membership
in a separately filtered export are not filtered financial totals.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator

from bs4 import BeautifulSoup, Tag

from backend.core.mabang_client import MabangApiError

TOTAL_PATTERN = re.compile(r"共\s*([\d,]+)\s*条")
MAX_TOTAL = 1_000_000


def _has_empty_placeholder(payload: dict[str, Any]) -> bool:
    """Recognize the actual classic zero-result row, never a missing body."""
    content = payload.get("tableContent")
    if not isinstance(content, str) or "alert-nodata" not in content or "group-nodata" not in content:
        return False
    soup = BeautifulSoup(content, "html.parser")
    rows = soup.find_all("tr")
    if len(rows) != 1:
        return False
    cells = rows[0].find_all("td", recursive=False)
    if len(cells) != 1 or cells[0].has_attr("data-field"):
        return False
    cell = cells[0]
    placeholder = cell.select_one("div.alert.alert-nodata.group-nodata")
    colspan = str(cell.get("colspan", ""))
    return (colspan.isdecimal() and int(colspan) > 1 and placeholder is not None
            and placeholder.get_text(" ", strip=True) == "暂无数据"
            and cell.get_text(" ", strip=True) == "暂无数据"
            and cell.find(attrs={"data-field": True}) is None
            and cell.find("a", attrs={"data-stock-id": True}) is None)


def decimal_cell(value: object, context: str) -> Decimal:
    text = str(value if value is not None else "").strip().replace(",", "")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise MabangApiError(f"{context}不是有效数字") from None
    if not number.is_finite() or abs(number) > Decimal("1e20"):
        raise MabangApiError(f"{context}不是有效数字")
    return number


def report_total(payload: dict[str, Any], context: str) -> int:
    if payload.get("success") is not True or not isinstance(payload.get("pageHtml"), str):
        raise MabangApiError(f"{context}缺少有效分页总数")
    text = BeautifulSoup(payload["pageHtml"], "html.parser").get_text(" ", strip=True)
    match = TOTAL_PATTERN.search(text)
    if not match:
        if not payload["pageHtml"].strip() and _has_empty_placeholder(payload):
            return 0
        raise MabangApiError(f"{context}无法读取报表总数")
    total = int(match.group(1).replace(",", ""))
    if total > MAX_TOTAL:
        raise MabangApiError(f"{context}总行数超出范围")
    return total


def detail_rows(payload: dict[str, Any], context: str) -> list[Tag]:
    if not isinstance(payload.get("tableContent"), str):
        raise MabangApiError(f"{context}缺少明细字段")
    if _has_empty_placeholder(payload):
        return []
    soup = BeautifulSoup(payload["tableContent"], "html.parser")
    return [row for row in soup.find_all("tr") if row.find("td")]


def field_text(row: Tag, field: str, context: str) -> str:
    cell = row.find("td", attrs={"data-field": field})
    if cell is None:
        raise MabangApiError(f"{context}缺少字段 {field}")
    return cell.get_text(" ", strip=True)


def row_sku(row: Tag) -> str:
    product = row.find("a", attrs={"data-stock-id": True})
    if product is not None:
        return product.get_text(" ", strip=True)
    raise MabangApiError("销量明细缺少商品身份，不能把主 SKU 当作明细 SKU")


def _form_values(form: Any) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for key, value in (form.items() if hasattr(form, "items") else form):
        values.setdefault(str(key).removesuffix("[]"), []).extend(
            str(v) for v in (value if isinstance(value, (list, tuple)) else [value])
        )
    return values


def validate_filter_echo(payload: dict[str, Any], form: Any, context: str) -> None:
    echo = payload.get("postdata")
    if echo is None:
        return
    if not isinstance(echo, dict):
        raise MabangApiError(f"{context}筛选回显结构无效")
    expected = _form_values(form)
    for key in ("shopLabelIds", "stockWarehouseId", "developid", "saleid", "moneyType", "timeKey", "timeStart", "timeEnd"):
        if key not in expected:
            continue
        actual = echo.get(key)
        if key in {"developid", "saleid"} and actual is None:
            # countryReports resolves personnel arrays to these SQL-filter
            # strings in its response (observed with the bound account).
            resolved = echo.get({"developid": "developIdIn", "saleid": "saleIdIn"}[key])
            if isinstance(resolved, str):
                actual = resolved.split(",") if resolved else []
        values = [str(v) for v in (actual if isinstance(actual, list) else [actual])]
        requested = expected[key]
        if key in {"timeStart", "timeEnd"}:
            # Classic PHP appends the day boundary a second time in its echo.
            values = [v[:19] for v in values]
        if sorted(values) != sorted(requested):
            raise MabangApiError(f"{context}筛选回显不一致: {key}")


def iter_report_pages(client: Any, *, url: str, params: dict, form_data: Any,
                      referer: str, context: str, page_size: int = 2000,
                      max_pages: int = 500, first_payload: dict | None = None,
                      ) -> Iterator[tuple[dict, list[Tag]]]:
    expected_total = None
    snapshot = None
    seen_pages: set[str] = set()
    seen_identities: set[tuple[str, str]] = set()
    for page in range(1, max_pages + 1):
        pairs = [(k, v) for k, v in (form_data.items() if hasattr(form_data, "items") else form_data)
                 if k not in {"page", "rowsPerPage"}]
        pairs.extend((("page", str(page)), ("rowsPerPage", str(page_size))))
        request = dict(pairs) if hasattr(form_data, "items") else pairs
        payload = first_payload if page == 1 and first_payload is not None else client._post_sales_report(
            url=url, params=params, form_data=request, referer=referer,
        )
        total = report_total(payload, context)
        validate_filter_echo(payload, request, context)
        current = payload.get("updateDateStr")
        if expected_total is None:
            expected_total, snapshot = total, current
            if total > page_size and (not isinstance(current, str) or not current.strip()):
                raise MabangApiError(f"{context}多页查询缺少统计快照")
            if (total + page_size - 1) // page_size > max_pages:
                raise MabangApiError(f"{context}收入分页超过 {max_pages} 页，已中止")
        if total != expected_total or current != snapshot:
            raise MabangApiError(f"{context}分页期间总数或统计快照发生变化")
        rows = detail_rows(payload, context)
        expected = min(page_size, max(0, total - (page - 1) * page_size))
        if len(rows) != expected:
            raise MabangApiError(f"{context}分页不完整: page={page} 预期={expected} 实际={len(rows)}")
        signature = []
        for row in rows:
            sku = row_sku(row)
            product = row.find("a", attrs={"data-stock-id": True})
            identity = (str(product.get("data-stock-id")) if product else "", sku)
            if not sku or identity in seen_identities:
                raise MabangApiError(f"{context}存在重复或无效商品明细")
            seen_identities.add(identity)
            signature.append((identity, [td.get_text(" ", strip=True) for td in row.find_all("td")[1:]]))
        fingerprint = hashlib.sha256(json.dumps(signature, ensure_ascii=False).encode()).hexdigest()
        if rows and fingerprint in seen_pages:
            raise MabangApiError(f"{context}返回重复分页")
        seen_pages.add(fingerprint)
        yield payload, rows
        if page * page_size >= total:
            return
    raise MabangApiError(f"{context}分页超出上限")


# Repeated data-field values follow the visible classic table's column order.
INCOME_COLUMNS = ("收入-订单金额", "收入-运费", "收入-其他收入", "收入-补贴金额", "收入-合计")
EXPENSE_COLUMNS = ("支出-商品成本", "支出-运费", "支出-包材费", "支出-平台费", "支出-转帐费", "支出-头程费",
                   "支出-FBA费用", "支出-优惠券", "支出-其他费用", "支出-其他支出", "支出-测评费", "支出-合计")
SINGLE_COLUMNS = {"主SKU": "salesSkuNewId", "状态": "status", "统一成本价": "defaultCost", "重量(单位:g)": "weight",
                  "库存": "stockQuantity", "创建时间": "timeCreated", "销售员": "sale", "开发员": "develop",
                  "关联订单数": "orderNum", "销售数量": "quantity", "支出-VAT税费": "vatTaxation",
                  "退款订单数": "refundNum", "退款商品数": "itemRefundNum", "退款金额": "refundMoney",
                  "退款率%": "returnRate", "毛利": "gross", "毛利率%": "grossRate"}


def table_sales_row(row: Tag) -> dict[str, str]:
    result = {"SKU": row_sku(row)}
    for column, field in SINGLE_COLUMNS.items():
        result[column] = field_text(row, field, "商品仓库报表")
    for field, columns in (("income", INCOME_COLUMNS), ("expenditure", EXPENSE_COLUMNS)):
        cells = row.find_all("td", attrs={"data-field": field})
        if len(cells) != len(columns):
            raise MabangApiError(f"商品仓库报表 {field} 列结构不完整")
        result.update(zip(columns, (cell.get_text(" ", strip=True) for cell in cells)))
    product = row.find("a", attrs={"data-stock-id": True})
    if product is None:
        raise MabangApiError("商品仓库报表缺少商品身份")
    info = product.find_parent("td")
    names = info.select("p[title]")
    if len(names) != 2:
        raise MabangApiError("商品仓库报表商品名称结构无效")
    result["商品中文名"], result["商品英文名"] = (cell.get("title", "") for cell in names)
    return result


def verify_csv_rows(client: Any, rows: list[dict[str, str]], *, url: str, params: dict,
                    form_data: Any, referer: str, context: str) -> None:
    """Compare the complete SKU multiset and financial values, for any row count."""
    columns = (("销售数量", "quantity"), ("收入-订单金额", "income"), ("毛利", "gross"))
    expected = Counter((r["SKU"].strip(), *(decimal_cell(r[c], context) for c, _ in columns)) for r in rows)
    actual: Counter = Counter()
    for payload, page_rows in iter_report_pages(client, url=url, params=params, form_data=form_data, referer=referer, context=context):
        if report_total(payload, context) != len(rows):
            raise MabangApiError(f"{context}导出被截断或行数不一致")
        for row in page_rows:
            actual[(row_sku(row), *(decimal_cell(field_text(row, f, context), context) for _, f in columns))] += 1
    if expected != actual:
        raise MabangApiError(f"{context}导出被截断或与筛选明细不一致: CSV={len(rows)} 查询={sum(actual.values())}")
