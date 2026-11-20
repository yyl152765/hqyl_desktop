"""Preview and synchronize the Philippine Shopee cancellation queue in Mabang."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Callable

import httpx
import xlsxwriter

from backend.core import mabang_order_client as order_page
from backend.core.mabang_client import MabangApiError, MabangClient
from backend.services.mabang_order_sku_export import MABANG_BASE_URL


TOOL = "ph_cancel_refund_sync"
TITLE = "菲律宾取消订单同步退款"
PLATFORM_ID = "17"
SYNC_PATH = "/index.php?mod=order.orderInfoSynchronize"
PAGE_SIZE = 500
SHOP_BATCH = 80
SYNC_BATCH = 300
MAX_PAGES_PER_BATCH = 500
PREVIEW_ROWS = 100
PH_SHOP = re.compile(r"菲律宾|PH\d+", re.IGNORECASE)
ORDER_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")


@dataclass(frozen=True)
class CancelOrder:
    order_id: str
    shop_id: str
    shop_name: str
    raw: dict[str, Any]


def _text(value: Any) -> str:
    return str(value if value is not None else "").replace("\u200b", "").strip()


def _batches(items, size):
    return (items[start : start + size] for start in range(0, len(items), size))


def _true(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1"}


def _identify_order(row, shops) -> CancelOrder:
    order_id = _text(row.get("platformOrderId"))
    platform_id = _text(row.get("platformId"))
    country = _text(row.get("countryCode")).upper()
    shop_id = _text(row.get("shopId"))
    if not shop_id:
        name = _text(row.get("shopName") or row.get("shopIdText"))
        matches = [key for key, value in shops.items() if value == name]
        shop_id = matches[0] if len(matches) == 1 else ""
    if (
        not ORDER_ID.fullmatch(order_id)
        or shop_id not in shops
        or (platform_id and platform_id != PLATFORM_ID)
        or (len(country) == 2 and country.isalpha() and country != "PH")
    ):
        raise MabangApiError("马帮查询结果含无法核对范围的订单，请检查店铺和异常列表")
    return CancelOrder(order_id, shop_id, shops[shop_id], row)


def _fetch_batch(client, shops) -> list[CancelOrder]:
    seen_pages: set[tuple[str, ...]] = set()
    result: list[CancelOrder] = []
    shop_ids = list(shops)
    client.client.cookies.set("order_rows_per_page_data_cookie", str(PAGE_SIZE))
    for page in range(1, MAX_PAGES_PER_BATCH + 1):
        payload = order_page._build_order_payload_base(client, rows_per_page=PAGE_SIZE, page=page)
        payload.update({
            "a": "abnormalList",
            "fixCategory[]": ["10"],
            "orderSearchHistory": "",
            "Order.shops[]": shop_ids,
            "shopIdMultiple[]": shop_ids,
            "platformId[]": [PLATFORM_ID],
        })
        try:
            data = client.post_form_json(
                order_page.ORDER_QUERY_PATH, form_data=payload, referer=order_page.ORDER_PAGE_PATH,
                context="查询菲律宾 Shopee 取消订单",
            )
        except MabangApiError:
            raise MabangApiError("马帮取消订单列表格式异常，请重新登录后查询") from None
        rows = data.get("orderDataList")
        if not _true(data.get("success")) or not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise MabangApiError("马帮取消订单列表返回异常，请检查账号权限或重新查询")
        orders = [_identify_order(row, shops) for row in rows]
        if orders:
            fingerprint = tuple(sorted(order.order_id for order in orders))
            if fingerprint in seen_pages:
                raise MabangApiError("马帮返回重复的订单页，请重新查询")
            seen_pages.add(fingerprint)
            result.extend(orders)
        try:
            page_count = int(data.get("pageCount") or 0)
            page_size = int(data.get("rowsPerPage") or PAGE_SIZE)
        except (TypeError, ValueError):
            raise MabangApiError("马帮取消订单分页信息无效") from None
        if page_size < 1 or len(rows) > page_size or page_count < 0:
            raise MabangApiError("马帮取消订单分页信息与返回行数不一致")
        more = _true(data.get("hasMore")) or page < page_count
        if more and not orders:
            raise MabangApiError("马帮取消订单分页未返回完整数据")
        if not more and (not orders or (page_count and page >= page_count) or len(orders) < page_size):
            return result
    raise MabangApiError("取消订单查询超过分页上限，请在马帮核对后重试")


def _read_queue(client, progress: Callable[[str], None]) -> tuple[int, dict[str, CancelOrder]]:
    try:
        shops = {
            shop.shop_id: shop.shop_name for shop in order_page.list_order_shops(client)
            if shop.platform_id == PLATFORM_ID and PH_SHOP.search(shop.shop_name)
        }
        if not shops:
            raise MabangApiError("当前马帮账号未匹配到菲律宾 Shopee 店铺")
        candidates: dict[str, CancelOrder] = {}
        for index, keys in enumerate(_batches(list(shops), SHOP_BATCH), start=1):
            orders = _fetch_batch(client, {key: shops[key] for key in keys})
            for order in orders:
                previous = candidates.get(order.order_id)
                if previous and previous.shop_id != order.shop_id:
                    raise MabangApiError("同一平台订单号匹配到多个店铺，请在马帮核实")
                candidates.setdefault(order.order_id, order)
            progress(f"第 {index} 批店铺已查询，累计 {len(candidates)} 单")
    except httpx.HTTPError:
        raise MabangApiError("读取马帮取消订单失败，请检查网络或重新登录") from None
    return len(shops), candidates


def _login(client, username: str, password: str) -> None:
    try:
        client.login(username, password)
    except (httpx.HTTPError, MabangApiError, ValueError):
        raise MabangApiError("马帮登录失败，请检查绑定的账号和网络") from None


def _submit(client, ids: list[str]) -> str:
    # Type 1 is the original order synchronization request. Code 111 is a
    # confirmation gate in Mabang. A retry or type 2 would be a second write.
    try:
        response = client.client.post(
            client._url(SYNC_PATH),
            headers={"Referer": client._url(order_page.ORDER_PAGE_PATH), "X-Requested-With": "XMLHttpRequest"},
            data={"synchronization_type": "2", "synchronizationOrderData": "\n".join(ids), "endTime": "", "type": "1"},
            follow_redirects=False,
        )
        response.raise_for_status()
        receipt = response.json()
    except (httpx.HTTPError, ValueError):
        return "回执未知"
    if not isinstance(receipt, dict):
        return "回执未知"
    if _text(receipt.get("code")) == "111":
        return "马帮要求人工确认"
    if _true(receipt.get("success")):
        return "马帮已受理"
    if _text(receipt.get("success")).lower() in {"false", "0"}:
        return "马帮未受理"
    return "回执未知"


OVERVIEW_FIELDS = (
    ("平台", ("platformName", "platformIdText")), ("订单状态", ("showOrderStatusText",)),
    ("异常说明", ("exceptionInfo",)), ("买家留言", ("buyerMessageText",)),
    ("卖家留言", ("sellerMessage",)), ("下单时间", ("createDate",)),
    ("标记发货时间", ("expressTime",)), ("交运时间", ("originTransportTime", "transportTime")),
    ("发货时间", ("orderDeliverTime", "deliverRealTime")), ("物流单号", ("trackNumber",)),
    ("买家", ("buyerName",)), ("国家", ("countryCodeCn", "countryCode")),
)


def _short_row(order: CancelOrder) -> dict[str, str]:
    return {
        "order_id": order.order_id, "shop_name": order.shop_name,
        "status": _text(order.raw.get("showOrderStatusText")),
        "reason": _text(order.raw.get("exceptionInfo")),
    }


def _overview_row(order):
    return [order.order_id, order.shop_name, *(
        next((_text(order.raw[key]) for key in keys if _text(order.raw.get(key))), "")
        for _, keys in OVERVIEW_FIELDS
    )]


def _write_sheet(book, name, headers, rows):
    sheet = book.add_worksheet(name)
    if not headers:
        return
    sheet.write_row(0, 0, headers, book.add_format({"bold": True, "bg_color": "#D9EAF4"}))
    sheet.freeze_panes(1, 0)
    sheet.set_column(0, len(headers) - 1, 22)
    count = 0
    for count, row in enumerate(rows, start=1):
        values = [json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value for value in row]
        if sheet.write_row(count, 0, values) != 0:
            raise ValueError("取消订单结果超出 Excel 容量")
    sheet.autofilter(0, 0, count, len(headers) - 1)


def _export(output: Path, summary, queue, sync_details=()) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"菲律宾Shopee取消订单_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    temp = path.with_suffix(".tmp")
    ordered = list(queue.values())
    raw_keys = sorted({key for order in ordered for key in order.raw})
    try:
        with xlsxwriter.Workbook(str(temp), {"strings_to_formulas": False, "strings_to_urls": False, "nan_inf_to_errors": True}) as book:
            _write_sheet(book, "汇总", ["项目", "结果"], summary)
            _write_sheet(book, "概览", ["订单号", "店铺名", *(name for name, _ in OVERVIEW_FIELDS)], map(_overview_row, ordered))
            _write_sheet(book, "原始数据", raw_keys or ["platformOrderId"], ([order.raw.get(key) for key in raw_keys] for order in ordered))
            if sync_details:
                _write_sheet(book, "同步结果", ["订单号", "店铺名", "同步回执", "异常列表复查"], sync_details)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def _run_directory(root: Path, mode: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    return Path(mkdtemp(prefix=f"{datetime.now():%Y%m%d_%H%M%S}_{mode}_", dir=root))


def preview_queue(username, password, output_root: Path, progress):
    with MabangClient(MABANG_BASE_URL, verify_ssl=True, timeout_seconds=240) as client:
        _login(client, username, password)
        shop_count, queue = _read_queue(client, progress)
    output = _run_directory(output_root, "预览")
    file = _export(output, [("模式", "预览"), ("店铺数", shop_count), ("异常订单数", len(queue))], queue)
    return queue, {
        "mode": "preview", "shop_count": shop_count, "order_count": len(queue),
        "rows": [_short_row(order) for order in list(queue.values())[:PREVIEW_ROWS]],
        "output_dir": str(output), "output_file": str(file),
    }


def _journal(stream, **event):
    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def sync_queue(username, password, output_root: Path, approved, progress):
    output = _run_directory(output_root, "同步")
    receipts = {order_id: "未提交" for order_id in approved}
    after = None
    with (output / "同步记录.jsonl").open("x", encoding="utf-8") as journal:
        with MabangClient(MABANG_BASE_URL, verify_ssl=True, timeout_seconds=240) as client:
            _login(client, username, password)
            progress("复查预览订单")
            shop_count, current = _read_queue(client, progress)
            if any(key in current and current[key].shop_id != order.shop_id for key, order in approved.items()):
                raise MabangApiError("预览订单的店铺归属已变化，请重新预览")
            for key in approved.keys() - current.keys():
                receipts[key] = "同步前已离开异常列表"
            targets = [key for key in approved if key in current]
            for index, batch in enumerate(_batches(targets, SYNC_BATCH), start=1):
                _journal(journal, event="准备提交", batch=index, order_ids=batch)
                status = _submit(client, batch)
                _journal(journal, event="批次回执", batch=index, status=status)
                receipts.update(dict.fromkeys(batch, status))
                progress(f"第 {index} 批共 {len(batch)} 单，{status}")
                if status == "回执未知":
                    break
            try:
                progress("读取同步后的异常列表")
                _, after = _read_queue(client, progress)
            except (MabangApiError, ValueError):
                progress("同步后复查未完成，已保留批次回执")

    details = []
    for key, order in approved.items():
        if after is None:
            recheck = "待复查"
        elif key in after:
            recheck = "仍在异常列表" if after[key].shop_id == order.shop_id else "店铺归属有变，待核对"
        else:
            recheck = "已离开异常列表"
        details.append((key, order.shop_name, receipts[key], recheck))
    accepted = sum(status == "马帮已受理" for status in receipts.values())
    skipped = sum(status == "同步前已离开异常列表" for status in receipts.values())
    review = len(approved) - accepted - skipped
    remaining = None if after is None else sum(key in after for key in approved)
    summary = [("模式", "同步"), ("店铺数", shop_count), ("预览订单数", len(approved)),
               ("马帮已受理", accepted), ("同步前已离开异常列表", skipped), ("回执待处理", review),
               ("复查仍在异常列表", remaining if after is not None else "复查未完成")]
    file = _export(output, summary, after or {}, details)
    complete = after is not None and not review
    return {
        "mode": "sync", "output_dir": str(output), "output_file": str(file), "shop_count": shop_count,
        "order_count": len(approved), "accepted_count": accepted, "skipped_count": skipped,
        "review_count": review, "remaining_count": remaining, "is_complete": complete,
        "completion_message": "部分订单回执或复查结果待核对，请查看同步结果表",
        "rows": [dict(order_id=key, shop_name=shop, status=status, reason=recheck)
                 for key, shop, status, recheck in details[:PREVIEW_ROWS]],
    }
