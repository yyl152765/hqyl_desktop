"""马帮 ERP 订单查询客户端。

该模块是桌面版 ``backend.core.mabang_client.MabangClient`` 的订单页适配层，
提供网红寄样登记需要的店铺列表、订单列表、订单商品明细抓取能力。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

from bs4 import BeautifulSoup

from backend.core.mabang_client import MabangApiError, MabangClient, _response_json_dict, decode_html_page


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_PAGE_SIZE = 500
SHOP_CHUNK_SIZE = 50
DETAIL_BATCH_SIZE = 30
DETAIL_LOG_INTERVAL = 50
SUPPORTED_ORDER_PAGE_SIZES = (100, 200, 300, 500)

ORDER_PAGE_PATH = "/index.php?mod=order.list&Order_orderStatus=2"
ORDER_QUERY_PATH = "/index.php?mod=order.oTc"
ORDER_ITEMS_PATH = "/index.php?mod=order.showOrderItems"
ORDER_LABEL_SAMPLE = "样品订单"

DEFAULT_ORDER_ITEM_BY = "id asc,stockId asc"
DEFAULT_TABLE_BASE = "2"
DEFAULT_RETRY_BACKOFF_CAP_SECONDS = 10.0

_PROGRESS: Callable[[str], None] | None = None


def _emit(msg: str) -> None:
    if _PROGRESS:
        _PROGRESS(msg)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OrderShopInfo:
    shop_id: str
    shop_name: str
    platform_id: str = ""
    platform_name: str = ""


@dataclass(slots=True)
class OrderRecord:
    order_id: str = ""
    platform_order_id: str = ""
    shop_id: str = ""
    shop_name: str = ""
    pay_time: str = ""
    create_time: str = ""
    order_label: str = ""
    sales_name: str = ""
    buyer_name: str = ""
    order_weight: str = ""
    table_base: str = DEFAULT_TABLE_BASE
    order_by_item_for_ary: str = DEFAULT_ORDER_ITEM_BY
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OrderItemRecord:
    order_id: str = ""
    platform_order_id: str = ""
    shop_name: str = ""
    pay_time: str = ""
    create_time: str = ""
    buyer_name: str = ""
    store_manager: str = ""
    sku: str = ""
    product_name: str = ""
    sell_price: str = ""
    quantity: str = "1"
    weight: str = ""


# ---------------------------------------------------------------------------
# 通用清洗/格式化
# ---------------------------------------------------------------------------

def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\u200b", "").replace("\xa0", " ")
    text = text.replace("\r", " ").replace("\n", " ")
    return " ".join(text.split()).strip()


def _format_datetime_text(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    match = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", text)
    if match:
        return match.group(0)
    match = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", text)
    if match:
        return f"{match.group(0)}:00"
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        return f"{match.group(0)} 00:00:00"
    return text


def _date_part(value: Any) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", _format_datetime_text(value))
    return match.group(0) if match else ""


def _format_decimal(value: Any) -> str:
    text = _clean_text(value).replace(",", "")
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return text
    if number.is_integer():
        return str(int(number))
    return ("%.4f" % number).rstrip("0").rstrip(".")


def _extract_item_cost(row_tag: Any) -> str:
    cost_cell = row_tag.select_one("td[data-field='sellPrice']")
    if cost_cell is None:
        return ""
    for paragraph in cost_cell.select("p"):
        text = _clean_text(paragraph.get_text(" ", strip=True))
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if match:
            return _format_decimal(match.group(0))
    text = _clean_text(cost_cell.get_text(" ", strip=True))
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return _format_decimal(match.group(0)) if match else ""


def _compute_retry_sleep_seconds(attempt: int, *, cap: float = DEFAULT_RETRY_BACKOFF_CAP_SECONDS) -> float:
    return min(float(cap), float(2 ** (max(1, int(attempt)) - 1)))


def _extract_first(pattern: str, text: str) -> str:
    if not text:
        return ""
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    value = match.group(1) if match.lastindex else match.group(0)
    return _clean_text(BeautifulSoup(value, "html.parser").get_text(" ", strip=True))


# ---------------------------------------------------------------------------
# 马帮订单页上下文
# ---------------------------------------------------------------------------

def _ensure_logged_in(client: MabangClient) -> None:
    if not getattr(client, "_logged_in", False):
        raise MabangApiError("马帮客户端当前尚未登录")


def _order_headers(client: MabangClient) -> dict[str, str]:
    return {
        "Referer": client._url(ORDER_PAGE_PATH),
        "X-Requested-With": "XMLHttpRequest",
    }


def _get_order_page_html(client: MabangClient) -> str:
    _ensure_logged_in(client)
    response = client.client.get(client._url(ORDER_PAGE_PATH))
    response.raise_for_status()
    return decode_html_page(response)


def _parse_order_context(client: MabangClient, page_html: str) -> None:
    order_page_key = _extract_first(r"var\s+orderPageKey\s*=\s*'([^']+)'", page_html)
    order_tab_id = (
        _extract_first(r'<li[^>]*class="[^"]*active[^"]*"[^>]*data-id="([^"]+)"', page_html)
        or _extract_first(r'<li[^>]*data-id="([^"]+)"[^>]*class="[^"]*active[^"]*"', page_html)
        or "222"
    )
    if not order_page_key:
        raise MabangApiError("无法从订单页面解析出 orderPageKey 防抖验证码")
    setattr(client, "_order_page_key", order_page_key)
    setattr(client, "_order_tab_id", order_tab_id)


def _ensure_order_context(client: MabangClient) -> None:
    if getattr(client, "_order_page_key", "") and getattr(client, "_order_tab_id", ""):
        return
    page_html = _get_order_page_html(client)
    _parse_order_context(client, page_html)


def _invalidate_order_context(client: MabangClient) -> None:
    setattr(client, "_order_page_key", None)
    setattr(client, "_order_tab_id", None)


def _refresh_order_context_for_retry(client: MabangClient) -> None:
    _invalidate_order_context(client)
    _ensure_order_context(client)


def _build_order_payload_base(client: MabangClient, *, rows_per_page: int, page: int) -> dict[str, str]:
    return {
        "orderPageKey": _clean_text(getattr(client, "_order_page_key", "")),
        "goPaypalRefundStatus": "1",
        "page": str(page),
        "rowsPerPage": str(rows_per_page),
        "Order_isCloud": "2",
        "m": "order",
        "a": "orderalllist",
        "isNewOrderPage": "1",
        "post_tableBase": "2",
        "tabId": _clean_text(getattr(client, "_order_tab_id", "")) or "222",
        "Order.orderStatus": "",
        "startPageNum": "",
        "endPageNum": "",
    }


def _request_order_json(
    client: MabangClient,
    path: str,
    *,
    payload: dict[str, Any],
    retries: int = 3,
    error_prefix: str = "马帮订单请求失败",
) -> dict[str, Any]:
    endpoint = client._url(path)
    retry_count = max(1, int(retries))
    last_error: Exception | None = None
    page_hint = _clean_text(payload.get("page")) or "-"
    rows_hint = _clean_text(payload.get("rowsPerPage")) or "-"
    for attempt in range(1, retry_count + 1):
        try:
            _ensure_order_context(client)
            response = client.client.post(endpoint, data=payload, headers=_order_headers(client))
            response.raise_for_status()
            data = _response_json_dict(response, context=error_prefix)
            if not data.get("success"):
                raise MabangApiError(f"{error_prefix}: {data}")
            return data
        except Exception as exc:
            last_error = exc
            if attempt >= retry_count:
                break
            _emit(f"马帮订单请求重试 {attempt}/{retry_count} 失败（page={page_hint}, rows={rows_hint}）：{exc}")
            _refresh_order_context_for_retry(client)
            time.sleep(_compute_retry_sleep_seconds(attempt))
    detail = str(last_error) if last_error else "unknown error"
    raise MabangApiError(f"{error_prefix} 多次重试异常: {detail}") from last_error


# ---------------------------------------------------------------------------
# 店铺列表
# ---------------------------------------------------------------------------

def list_order_shops(client: MabangClient) -> list[OrderShopInfo]:
    """从订单列表页面获取店铺列表。"""
    page_html = _get_order_page_html(client)
    _parse_order_context(client, page_html)
    soup = BeautifulSoup(page_html, "html.parser")
    shops: list[OrderShopInfo] = []
    seen: set[str] = set()

    for inp in soup.select("input[name='shopIdMultiple[]']"):
        shop_id = _clean_text(inp.get("value"))
        if not shop_id or shop_id in seen:
            continue
        label = inp.find_parent("label")
        span = label.find("span", class_="text") if label else None
        if span is None:
            span = inp.find_next_sibling("span", class_="text")
        shop_name = _clean_text(span.get_text(" ", strip=True)) if span else ""
        if not shop_name and label is not None:
            shop_name = _clean_text(label.get_text(" ", strip=True))
        if not shop_name:
            continue
        seen.add(shop_id)
        shops.append(
            OrderShopInfo(
                shop_id=shop_id,
                shop_name=shop_name,
                platform_id=_clean_text(inp.get("data-platform-id")),
                platform_name=_clean_text(inp.get("data-platform-name") or inp.get("data-platformname")),
            )
        )
    return shops


# ---------------------------------------------------------------------------
# 订单列表查询
# ---------------------------------------------------------------------------

def _normalize_page_size(value: Any) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        size = DEFAULT_PAGE_SIZE
    if size in SUPPORTED_ORDER_PAGE_SIZES:
        return size
    if size > max(SUPPORTED_ORDER_PAGE_SIZES):
        return max(SUPPORTED_ORDER_PAGE_SIZES)
    smaller_or_equal = [item for item in SUPPORTED_ORDER_PAGE_SIZES if item <= size]
    if smaller_or_equal:
        return max(smaller_or_equal)
    return min(SUPPORTED_ORDER_PAGE_SIZES)


def _fetch_order_page(
    client: MabangClient,
    shop_ids: list[str],
    start_time: str,
    end_time: str,
    page: int = 1,
    rows_per_page: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    _ensure_order_context(client)
    payload: dict[str, Any] = _build_order_payload_base(client, rows_per_page=rows_per_page, page=page)
    normalized_shop_ids = [_clean_text(value) for value in shop_ids if _clean_text(value)]
    if normalized_shop_ids:
        payload["Order.shops[]"] = normalized_shop_ids
        payload["shopIdMultiple[]"] = normalized_shop_ids
    payload.update(
        {
            "queryTime": "paidTime",
            "startTime1": _clean_text(start_time),
            "endTime1": _clean_text(end_time),
        }
    )
    return _request_order_json(client, ORDER_QUERY_PATH, payload=payload, retries=3, error_prefix="查询订单列表")


def _parse_order_rows(data: dict[str, Any]) -> list[OrderRecord]:
    rows_raw = data.get("orderDataList") or data.get("rows") or data.get("Rows") or []
    orders: list[OrderRecord] = []
    for row in rows_raw:
        if not isinstance(row, dict):
            continue
        shop_name = _clean_text(row.get("shopIdText") or row.get("shopName") or row.get("shopOwner"))
        paid_time = _format_datetime_text(row.get("paidTime") or row.get("paidTimeTimezone") or row.get("createDate"))
        create_time = _format_datetime_text(row.get("createDate") or row.get("createDateTimezone") or paid_time)
        order = OrderRecord(
            order_id=_clean_text(row.get("id") or row.get("orderId")),
            platform_order_id=_clean_text(row.get("platformOrderId")),
            shop_id=_clean_text(row.get("shopId")),
            shop_name=shop_name,
            pay_time=paid_time,
            create_time=create_time,
            order_label=_clean_text(row.get("order_label") or row.get("orderLabel")),
            sales_name=_clean_text(
                row.get("salesIdText")
                or row.get("salesName")
                or row.get("shopManager")
                or row.get("sellerName")
                or row.get("salesId")
            ),
            buyer_name=_clean_text(row.get("buyerName")),
            order_weight=_format_decimal(row.get("orderWeight")),
            table_base=_clean_text(row.get("tableBase")) or DEFAULT_TABLE_BASE,
            order_by_item_for_ary=_clean_text(row.get("orderByItemForAry")) or DEFAULT_ORDER_ITEM_BY,
            raw=row,
        )
        orders.append(order)
    return orders


def is_sample_order(order: OrderRecord) -> bool:
    return ORDER_LABEL_SAMPLE in _clean_text(order.order_label)


def _dedupe_orders(orders: list[OrderRecord]) -> list[OrderRecord]:
    seen: set[str] = set()
    result: list[OrderRecord] = []
    for order in orders:
        key = _clean_text(order.order_id or order.platform_order_id)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        result.append(order)
    return result


def _split_time_range(start_time: str, end_time: str) -> tuple[tuple[str, str], tuple[str, str]] | None:
    try:
        start_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
        end_dt = datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if (end_dt - start_dt).total_seconds() <= 3600:
        return None
    midpoint = start_dt + (end_dt - start_dt) / 2
    left_end = midpoint.replace(microsecond=0)
    right_start = left_end + timedelta(seconds=1)
    if right_start > end_dt:
        return None
    return (
        (start_dt.strftime("%Y-%m-%d %H:%M:%S"), left_end.strftime("%Y-%m-%d %H:%M:%S")),
        (right_start.strftime("%Y-%m-%d %H:%M:%S"), end_dt.strftime("%Y-%m-%d %H:%M:%S")),
    )


def fetch_orders_with_retry(
    client: MabangClient,
    shop_ids: list[str],
    start_time: str,
    end_time: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_retries: int = 3,
    progress: Callable[[str], None] | None = None,
) -> list[OrderRecord]:
    """查询订单列表，支持分页、店铺分片和递归降级。"""
    global _PROGRESS
    _PROGRESS = progress

    normalized_shop_ids = [_clean_text(value) for value in shop_ids if _clean_text(value)]
    if not normalized_shop_ids:
        return []

    page_size = _normalize_page_size(page_size)
    all_orders: list[OrderRecord] = []

    for chunk_start in range(0, len(normalized_shop_ids), SHOP_CHUNK_SIZE):
        chunk = normalized_shop_ids[chunk_start:chunk_start + SHOP_CHUNK_SIZE]
        _emit(f"查询订单：店铺 {chunk_start + 1}-{chunk_start + len(chunk)}/{len(normalized_shop_ids)}，每页 {page_size} 条")
        all_orders.extend(
            _fetch_chunk_with_split(
                client,
                chunk,
                start_time,
                end_time,
                page_size,
                max(0, int(max_retries)),
            )
        )

    result = _dedupe_orders(all_orders)
    _emit(f"共获取订单 {len(result)} 条（去重前 {len(all_orders)} 条）")
    return result


def _fetch_chunk_with_split(
    client: MabangClient,
    shop_ids: list[str],
    start_time: str,
    end_time: str,
    page_size: int,
    retries: int,
) -> list[OrderRecord]:
    try:
        return _fetch_all_pages(client, shop_ids, start_time, end_time, page_size)
    except Exception as exc:
        if retries <= 0:
            raise MabangApiError(
                f"查询订单失败：店铺数={len(shop_ids)}，每页={page_size}，时间={start_time}~{end_time}，错误={exc}"
            ) from exc

        if len(shop_ids) > 1:
            mid = max(1, len(shop_ids) // 2)
            _emit(f"查询失败，拆分为两组店铺重试（{len(shop_ids)} → {mid} + {len(shop_ids) - mid}）")
            return (
                _fetch_chunk_with_split(client, shop_ids[:mid], start_time, end_time, page_size, retries - 1)
                + _fetch_chunk_with_split(client, shop_ids[mid:], start_time, end_time, page_size, retries - 1)
            )

        if page_size > min(SUPPORTED_ORDER_PAGE_SIZES):
            new_size = _normalize_page_size(page_size // 2)
            _emit(f"查询失败，缩小页面大小 {page_size} → {new_size}")
            return _fetch_chunk_with_split(client, shop_ids, start_time, end_time, new_size, retries - 1)

        split_ranges = _split_time_range(start_time, end_time)
        if split_ranges is not None:
            left, right = split_ranges
            _emit(f"查询失败，拆分时间范围重试：{start_time}~{end_time}")
            return (
                _fetch_chunk_with_split(client, shop_ids, left[0], left[1], page_size, retries - 1)
                + _fetch_chunk_with_split(client, shop_ids, right[0], right[1], page_size, retries - 1)
            )

        raise MabangApiError(
            f"查询订单失败：店铺={','.join(shop_ids)}，每页={page_size}，时间={start_time}~{end_time}，错误={exc}"
        ) from exc


def _fetch_all_pages(
    client: MabangClient,
    shop_ids: list[str],
    start_time: str,
    end_time: str,
    page_size: int,
) -> list[OrderRecord]:
    all_orders: list[OrderRecord] = []
    page = 1
    while True:
        data = _fetch_order_page(client, shop_ids, start_time, end_time, page, page_size)
        orders = _parse_order_rows(data)
        all_orders.extend(orders)
        actual_page_size = _normalize_page_size(data.get("rowsPerPage") or page_size)
        total = int(data.get("total") or data.get("Total") or 0)
        if not orders:
            break
        if total > 0 and len(all_orders) >= total:
            break
        if len(orders) < actual_page_size:
            break
        page += 1
    return all_orders


# ---------------------------------------------------------------------------
# 订单商品明细
# ---------------------------------------------------------------------------

def _fetch_order_item_html_map(
    client: MabangClient,
    order_rows: Sequence[OrderRecord],
    table_base: str,
    order_item_by: str,
) -> dict[str, str]:
    order_ids = [_clean_text(order.order_id) for order in order_rows if _clean_text(order.order_id)]
    if not order_ids:
        return {}
    _ensure_order_context(client)
    response = client.client.post(
        client._url(ORDER_ITEMS_PATH),
        data={
            "orderItemIq": ",".join(order_ids),
            "tableBase": _clean_text(table_base) or DEFAULT_TABLE_BASE,
            "cloudStatus": "",
            "orderItemBy": _clean_text(order_item_by) or DEFAULT_ORDER_ITEM_BY,
            "tabId": _clean_text(getattr(client, "_order_tab_id", "")) or "222",
        },
        headers=_order_headers(client),
    )
    response.raise_for_status()
    payload = _response_json_dict(response, context="查询订单商品明细")
    if not payload.get("success"):
        raise MabangApiError(f"查询订单商品明细失败: {payload}")
    html_map = payload.get("order_list_html_header") or {}
    if not isinstance(html_map, dict):
        raise MabangApiError(f"查询订单商品明细失败：返回结构无效: {payload}")
    return {_clean_text(order_id): str(html_text or "") for order_id, html_text in html_map.items() if _clean_text(order_id)}


def _parse_item_rows(item_html: str, order: OrderRecord) -> list[OrderItemRecord]:
    soup = BeautifulSoup(f"<table><tbody>{item_html}</tbody></table>", "html.parser")
    items: list[OrderItemRecord] = []
    for row_tag in soup.select("tr"):
        sku_tag = row_tag.select_one("a.SkuNumber")
        name_tag = row_tag.select_one("span[data-field='productName']")
        if sku_tag is None or name_tag is None:
            continue
        sku = _clean_text(sku_tag.get_text(" ", strip=True))
        product_name = _clean_text(name_tag.get("title") or name_tag.get_text(" ", strip=True))
        if not sku or not product_name:
            continue
        quantity_cell = row_tag.select_one("td[data-field='quantity']")
        quantity = _clean_text(quantity_cell.get_text(" ", strip=True)) if quantity_cell else "1"
        items.append(
            OrderItemRecord(
                order_id=order.order_id,
                platform_order_id=order.platform_order_id,
                shop_name=order.shop_name,
                pay_time=order.pay_time,
                create_time=order.create_time,
                buyer_name=order.buyer_name,
                store_manager=order.sales_name,
                sku=sku,
                product_name=product_name,
                sell_price=_extract_item_cost(row_tag),
                quantity=quantity or "1",
                weight=order.order_weight,
            )
        )
    return items


def _chunk_list(values: Sequence[OrderRecord], size: int) -> list[list[OrderRecord]]:
    return [list(values[index:index + size]) for index in range(0, len(values), size)]


def fetch_order_items(
    client: MabangClient,
    orders: list[OrderRecord],
    *,
    progress: Callable[[str], None] | None = None,
) -> list[OrderItemRecord]:
    """批量获取订单商品明细。"""
    global _PROGRESS
    _PROGRESS = progress

    valid_orders = [order for order in orders if _clean_text(order.order_id)]
    if not valid_orders:
        return []

    groups: dict[tuple[str, str], list[OrderRecord]] = {}
    for order in valid_orders:
        key = (
            _clean_text(order.table_base) or DEFAULT_TABLE_BASE,
            _clean_text(order.order_by_item_for_ary) or DEFAULT_ORDER_ITEM_BY,
        )
        groups.setdefault(key, []).append(order)

    all_items: list[OrderItemRecord] = []
    seen_keys: set[str] = set()
    detail_batches: list[tuple[str, str, list[OrderRecord]]] = []
    for (table_base, order_item_by), group_orders in groups.items():
        for chunk in _chunk_list(group_orders, DETAIL_BATCH_SIZE):
            detail_batches.append((table_base, order_item_by, chunk))

    total_orders = len(valid_orders)
    processed_orders = 0
    total_batches = len(detail_batches)
    _emit(f"开始抓取订单明细，共 {total_orders} 单，分 {total_batches} 批")

    for batch_index, (table_base, order_item_by, order_chunk) in enumerate(detail_batches, start=1):
        html_map = _fetch_order_item_html_map(client, order_chunk, table_base, order_item_by)
        requested_order_ids = [_clean_text(order.order_id) for order in order_chunk if _clean_text(order.order_id)]
        missing_order_ids = [order_id for order_id in requested_order_ids if order_id not in html_map]
        if missing_order_ids:
            _emit(f"明细批次返回不足：批次 {batch_index}/{total_batches}，缺少 {len(missing_order_ids)} 单")

        for order in order_chunk:
            order_id = _clean_text(order.order_id)
            item_html = html_map.get(order_id, "")
            if not item_html and len(order_chunk) == 1 and len(html_map) == 1:
                item_html = next(iter(html_map.values()))
            if not item_html:
                continue
            for item in _parse_item_rows(item_html, order):
                dedupe_order_no = _clean_text(item.platform_order_id or item.order_id).upper()
                dedupe_key = f"{dedupe_order_no}||{_clean_text(item.sku).upper()}"
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                all_items.append(item)

        processed_orders += len(order_chunk)
        if batch_index % 5 == 0 or batch_index == total_batches or processed_orders % DETAIL_LOG_INTERVAL == 0:
            _emit(f"商品明细进度：{processed_orders}/{total_orders} 单，批次 {batch_index}/{total_batches}")

    _emit(f"共获取商品明细 {len(all_items)} 条（去重后）")
    return all_items


# ---------------------------------------------------------------------------
# 辅助：构建输出记录
# ---------------------------------------------------------------------------

def build_output_record(item: OrderItemRecord, sort_time_field: str = "付款时间") -> dict[str, str]:
    order_no = _clean_text(item.platform_order_id or item.order_id)
    pay_date = _date_part(item.pay_time or item.create_time)
    sort_time = item.pay_time if sort_time_field == "付款时间" else item.create_time
    return {
        "付款时间": item.pay_time,
        "寄样日期": pay_date,
        "创建时间": item.create_time,
        "订单编号": order_no,
        "订单号": order_no,
        "店铺名": item.shop_name,
        "店铺": item.shop_name,
        "客户姓名": item.buyer_name,
        "SKU": item.sku,
        "商品中文名称": item.product_name,
        "中文名": item.product_name,
        "商品总成本": _format_decimal(item.sell_price),
        "商品单个成本": _format_decimal(item.sell_price),
        "重量": _format_decimal(item.weight),
        "订单重量": _format_decimal(item.weight),
        "店长": item.store_manager,
        "排序时间": sort_time,
    }
