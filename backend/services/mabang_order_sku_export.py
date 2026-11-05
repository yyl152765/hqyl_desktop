"""马帮订单批量查询与 SKU 汇总导出。

流程与马帮后台「订单管理 -> 全部订单 -> 批量查询(按订单编号) -> 订单导出 -> 勾选 SKU 字段」一致，
只是把界面操作换成 ERP 的 HTTP 接口：

1. 读取用户上传的订单编号文件（xlsx / xls / csv / txt），按规则清洗去重；
2. ``mod=order.oTc`` 批量按订单编号检索，每批最多 2000 个（马帮自身限制）；
3. ``mod=order.showOrderItems`` 按批取订单商品明细，拿到 SKU、商品名称、数量、单价；
4. 汇总成一张工作簿：唯一 SKU 清单 + 订单明细 + 未查询到的订单号。

接口细节（2026-11 实测）：
- 批量查询必须在请求体带上 ``platformTracknumberSearchInput=platformOrderId``、
  ``platformTracknumberSearchtextarea=<换行分隔的订单号>`` 以及 ``orderCursorMode=1``，
  缺少 ``orderCursorMode`` 时服务端会忽略批量条件、退回默认的整页订单列表；
- 响应里的 ``notFoundPlatformOrderIds`` 就是本次没有命中的订单号。
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from bs4 import BeautifulSoup
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from backend.core import mabang_order_client as order_client
from backend.core.mabang_client import MabangApiError, MabangClient

ProgressCallback = Callable[[str], None]

MABANG_BASE_URL = "https://900853.private.mabangerp.com"
ORDER_PAGE_PATH = "/index.php?mod=order.list&Order_orderStatus=2"

# 马帮批量查询弹窗自身的上限：每次最多 2000 个订单号
MAX_ORDERS_PER_BATCH = 2000
MAX_ORDER_NUMBER_TOTAL = 50000
MAX_ORDERS_PER_TASK_HINT = "单次最多 2000 个订单号，超出会自动分批查询"

DEFAULT_ROWS_PER_PAGE = 200
DEFAULT_DETAIL_BATCH_SIZE = 30
DEFAULT_MAX_PAGES_PER_BATCH = 60
DEFAULT_STEP_RETRIES = 3

ORDER_FILE_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".csv", ".txt")
OUTPUT_DIR_NAME = "马帮订单SKU汇总"
OUTPUT_FILENAME_PREFIX = "马帮订单SKU汇总"

ORDER_NUMBER_HEADERS = (
    "订单编号",
    "订单号",
    "平台订单编号",
    "平台订单号",
    "平台订单id",
    "orderid",
    "ordernumber",
    "platformorderid",
    "交易号",
)
ORDER_NUMBER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{3,63}$")

# 字段键 -> (表头, 是否默认勾选)
FIELD_DEFINITIONS: tuple[tuple[str, str, bool], ...] = (
    ("order_no", "订单编号", True),
    ("shop", "店铺", True),
    ("platform", "平台", True),
    ("order_status", "订单状态", True),
    ("pay_time", "付款时间", True),
    ("create_time", "创建时间", False),
    ("buyer", "客户姓名", False),
    ("sku", "SKU", True),
    ("product_name", "商品中文名称", True),
    ("quantity", "数量", True),
    ("unit_price", "商品单价", False),
    ("amount", "商品金额", False),
    ("order_weight", "订单重量(g)", False),
    ("ship_time", "发货时间", False),
    ("sales", "店长", False),
    ("mabang_order_id", "马帮订单ID", False),
)
FIELD_LABELS = {key: label for key, label, _ in FIELD_DEFINITIONS}
DEFAULT_FIELDS = tuple(key for key, _, default in FIELD_DEFINITIONS if default)

STATUS_TEXT_MAP = {
    "1": "待付款",
    "2": "待审核",
    "3": "待处理",
    "4": "配货中",
    "5": "已发货",
    "6": "已作废",
    "7": "已完成",
    "8": "退款中",
}


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OrderNumberPreview:
    """订单编号文件解析结果。"""

    numbers: tuple[str, ...]
    source_file: str = ""
    sheet_name: str = ""
    column_name: str = ""
    raw_row_count: int = 0
    duplicate_count: int = 0
    invalid_count: int = 0
    header_detected: bool = False


@dataclass(frozen=True, slots=True)
class OrderSkuExportJob:
    username: str
    password: str
    order_numbers: tuple[str, ...]
    output_dir: Path
    fields: tuple[str, ...] = DEFAULT_FIELDS
    include_detail: bool = True
    include_not_found: bool = True
    base_url: str = MABANG_BASE_URL
    rows_per_page: int = DEFAULT_ROWS_PER_PAGE
    detail_batch_size: int = DEFAULT_DETAIL_BATCH_SIZE
    step_retries: int = DEFAULT_STEP_RETRIES


@dataclass(slots=True)
class OrderSkuRow:
    order_no: str = ""
    mabang_order_id: str = ""
    shop: str = ""
    platform: str = ""
    order_status: str = ""
    pay_time: str = ""
    create_time: str = ""
    ship_time: str = ""
    buyer: str = ""
    sales: str = ""
    order_weight: str = ""
    sku: str = ""
    product_name: str = ""
    quantity: float = 0.0
    unit_price: float = 0.0
    amount: float = 0.0


@dataclass(frozen=True, slots=True)
class UniqueSkuRow:
    sku: str
    product_name: str
    order_count: int
    total_quantity: float
    order_numbers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OrderSkuExportResult:
    output_file: Path
    output_dir: Path
    input_order_count: int
    matched_order_count: int
    matched_order_numbers: tuple[str, ...]
    not_found_orders: tuple[str, ...]
    detail_row_count: int
    unique_sku_count: int
    unique_skus: tuple[UniqueSkuRow, ...]
    preview_rows: tuple[dict[str, Any], ...] = ()


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _safe_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _normalize_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    text = text.replace("，", ",").replace("；", ";")
    return text.strip()


def _is_header_text(text: str) -> bool:
    folded = re.sub(r"[\s_\-:：]", "", text).casefold()
    if not folded:
        return False
    return any(folded == item.casefold() or folded.startswith(item.casefold()) for item in ORDER_NUMBER_HEADERS)


def _looks_like_order_number(text: str) -> bool:
    return bool(ORDER_NUMBER_PATTERN.match(text))


def _split_multi_value(text: str) -> list[str]:
    """把「一行多个订单号」拆开，兼容逗号/分号/顿号/空白分隔。"""
    parts = re.split(r"[,\s;、|]+", text)
    return [part for part in (item.strip() for item in parts) if part]


def _datetime_text(value: Any) -> str:
    text = _normalize_cell(value)
    if not text or text in {"--", "0"}:
        return ""
    match = re.search(r"\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?", text)
    return match.group(0) if match else text


def _format_number(value: Any, *, digits: int = 4) -> str:
    text = _normalize_cell(value).replace(",", "")
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return text
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return ("%.*f" % (digits, number)).rstrip("0").rstrip(".")


def _to_float(value: Any) -> float:
    try:
        return float(_normalize_cell(value).replace(",", "") or 0)
    except ValueError:
        return 0.0


def _chunked(values: Sequence[Any], size: int) -> list[list[Any]]:
    return [list(values[index:index + size]) for index in range(0, len(values), size)]


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress:
        progress(message)


# ---------------------------------------------------------------------------
# 订单编号文件解析（上传 -> 清洗）
# ---------------------------------------------------------------------------

def _read_tabular_rows(path: Path) -> tuple[list[list[Any]], str]:
    """读取文件为二维表，返回 (行, sheet 名)。"""
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = workbook[workbook.sheetnames[0]]
            rows = [list(row) for row in sheet.iter_rows(values_only=True)]
            return rows, sheet.title
        finally:
            workbook.close()
    if suffix == ".xls":
        import xlrd

        book = xlrd.open_workbook(str(path))
        sheet = book.sheet_by_index(0)
        rows = [[sheet.cell_value(row_index, col_index) for col_index in range(sheet.ncols)] for row_index in range(sheet.nrows)]
        return rows, sheet.name
    if suffix in {".csv", ".txt"}:
        content = ""
        for encoding in ("utf-8-sig", "gb18030", "utf-16"):
            try:
                content = path.read_text(encoding=encoding)
                break
            except UnicodeDecodeError:
                continue
        if not content:
            raise ValueError("订单编号文件为空或编码无法识别")
        rows = [line.split("\t") if "\t" in line else [line] for line in content.splitlines()]
        return rows, ""
    raise ValueError(f"不支持的订单编号文件类型：{path.suffix or '未知'}，请上传 xlsx / xls / csv / txt")


def _pick_order_column(rows: Sequence[Sequence[Any]]) -> tuple[int, bool, str]:
    """定位订单编号列：优先表头文字匹配，否则取第一列。"""
    scan_rows = rows[:5]
    for row_index, row in enumerate(scan_rows):
        for column_index, cell in enumerate(row):
            text = _normalize_cell(cell)
            if text and _is_header_text(text) and row_index < 3:
                return column_index, True, text
    # 没有表头时，找第一个"看起来像订单号"的列
    for row in scan_rows:
        for column_index, cell in enumerate(row):
            text = _normalize_cell(cell)
            if text and _looks_like_order_number(text):
                return column_index, False, ""
    return 0, False, ""


def parse_order_number_file(path: Path | str) -> OrderNumberPreview:
    """读取并清洗订单编号文件：去空行、去表头、去非法字符、去重保序。"""
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise ValueError(f"订单编号文件不存在：{file_path}")
    if file_path.suffix.lower() not in ORDER_FILE_SUFFIXES:
        raise ValueError(f"不支持的订单编号文件类型：{file_path.suffix or '未知'}")

    rows, sheet_name = _read_tabular_rows(file_path)
    column_index, header_detected, column_name = _pick_order_column(rows)

    seen: dict[str, None] = {}
    invalid_count = 0
    duplicate_count = 0
    raw_count = 0

    for row in rows:
        if not row:
            continue
        raw_cell = row[column_index] if column_index < len(row) else ""
        text = _normalize_cell(raw_cell)
        if not text:
            # 主列空时，兜底扫描整行，避免用户把订单号放在第 2 列
            candidates = [_normalize_cell(cell) for cell in row if _normalize_cell(cell)]
            text = next((item for item in candidates if _looks_like_order_number(item)), "")
            if not text:
                continue
        if _is_header_text(text):
            continue
        for item in _split_multi_value(text):
            raw_count += 1
            if not _looks_like_order_number(item):
                invalid_count += 1
                continue
            if item in seen:
                duplicate_count += 1
                continue
            seen[item] = None

    return OrderNumberPreview(
        numbers=tuple(seen.keys()),
        source_file=str(file_path),
        sheet_name=sheet_name,
        column_name=column_name,
        raw_row_count=raw_count,
        duplicate_count=duplicate_count,
        invalid_count=invalid_count,
        header_detected=header_detected,
    )


def _validate_fields(raw_fields: Any) -> tuple[str, ...]:
    if raw_fields is None:
        return DEFAULT_FIELDS
    if isinstance(raw_fields, str):
        candidates = [item.strip() for item in raw_fields.split(",") if item.strip()]
    elif isinstance(raw_fields, Iterable):
        candidates = [str(item).strip() for item in raw_fields if str(item).strip()]
    else:
        raise ValueError("导出字段格式无效")
    selected = tuple(dict.fromkeys(key for key in candidates if key in FIELD_LABELS))
    if not selected:
        raise ValueError("请至少选择一个导出字段")
    if "sku" not in selected:
        selected = ("sku",) + selected
    return selected


def validate_order_sku_export_payload(payload: dict[str, Any]) -> OrderSkuExportJob:
    username = _safe_text(payload.get("username"))
    password = str(payload.get("password") or "")
    if not username or not password:
        raise ValueError("请先绑定并选择马帮账号")

    source_file = _safe_text(payload.get("source_file"))
    raw_text = str(payload.get("order_text") or "")

    if source_file:
        preview = parse_order_number_file(source_file)
        numbers = list(preview.numbers)
    elif raw_text.strip():
        numbers = []
        for line in raw_text.splitlines():
            for item in _split_multi_value(_normalize_cell(line)):
                if _is_header_text(item) or not _looks_like_order_number(item):
                    continue
                numbers.append(item)
        numbers = list(dict.fromkeys(numbers))
    else:
        raise ValueError("请上传订单编号文件，或直接粘贴订单编号")

    if not numbers:
        raise ValueError("没有解析到有效订单编号，请检查文件内容")
    if len(numbers) > MAX_ORDER_NUMBER_TOTAL:
        raise ValueError(f"单次最多处理 {MAX_ORDER_NUMBER_TOTAL} 个订单编号，当前 {len(numbers)} 个，请拆分后重试")

    output_dir = _safe_text(payload.get("output_dir"))
    if not output_dir:
        raise ValueError("请选择输出目录")

    return OrderSkuExportJob(
        username=username,
        password=password,
        order_numbers=tuple(numbers),
        output_dir=Path(output_dir).expanduser(),
        fields=_validate_fields(payload.get("fields")),
        include_detail=bool(payload.get("include_detail", True)),
        include_not_found=bool(payload.get("include_not_found", True)),
        base_url=_safe_text(payload.get("base_url")) or MABANG_BASE_URL,
    )


# ---------------------------------------------------------------------------
# 马帮接口：批量查询 + 商品明细
# ---------------------------------------------------------------------------

def _order_row_to_record(row: dict[str, Any]) -> OrderSkuRow:
    status_text = ""
    status_html = _safe_text(row.get("order_status_image_html"))
    if status_html:
        status_text = BeautifulSoup(status_html, "html.parser").get_text(" ", strip=True)
    if not status_text:
        status_code = _safe_text(row.get("orderStatus"))
        status_text = STATUS_TEXT_MAP.get(status_code, status_code)

    return OrderSkuRow(
        order_no=_safe_text(row.get("platformOrderId")),
        mabang_order_id=_safe_text(row.get("id")),
        shop=_safe_text(row.get("shopIdText") or row.get("shopName")),
        platform=_safe_text(row.get("platformIdText") or row.get("platformName")),
        order_status=status_text,
        pay_time=_datetime_text(row.get("paidTime")),
        create_time=_datetime_text(row.get("createDate")),
        ship_time=_datetime_text(row.get("expressTime")),
        buyer=_safe_text(row.get("buyerName")),
        sales=_safe_text(row.get("salesIdText") or row.get("salesName")),
        order_weight=_format_number(row.get("orderWeight"), digits=2),
    )


def _batch_search_orders(
    client: MabangClient,
    numbers: Sequence[str],
    *,
    rows_per_page: int,
    progress: ProgressCallback | None = None,
) -> tuple[list[Any], list[str]]:
    """一批订单号（≤2000）批量查询，返回命中的 OrderRecord 与未命中订单号。"""
    matched: list[Any] = []
    not_found: list[str] = []
    page = 1
    while page <= DEFAULT_MAX_PAGES_PER_BATCH:
        payload: dict[str, Any] = order_client._build_order_payload_base(client, rows_per_page=rows_per_page, page=page)
        payload.update(
            {
                "platformTracknumberSearchInput": "platformOrderId",
                "platformTracknumberSearchtextarea": "\n".join(numbers),
                # 缺少 orderCursorMode 时马帮会忽略批量条件，退回默认列表
                "orderCursorMode": "1",
            }
        )
        data = order_client._request_order_json(
            client,
            order_client.ORDER_QUERY_PATH,
            payload=payload,
            retries=DEFAULT_STEP_RETRIES,
            error_prefix="批量查询订单",
        )
        raw_rows = data.get("orderDataList") or []
        matched.extend(order_client._parse_order_rows({"orderDataList": raw_rows}))

        for item in data.get("notFoundPlatformOrderIds") or []:
            text = _safe_text(item)
            if text:
                not_found.append(text)

        has_more = bool(data.get("hasMore"))
        page_count = int(data.get("pageCount") or 1)
        if not raw_rows:
            break
        if not has_more and (page >= page_count or len(raw_rows) < rows_per_page):
            break
        if page >= page_count and not has_more:
            break
        page += 1

    return matched, not_found


def _fetch_items_for_orders(
    client: MabangClient,
    orders: Sequence[Any],
    *,
    batch_size: int,
    progress: ProgressCallback | None = None,
) -> dict[str, list[Any]]:
    """按批抓取订单商品明细，返回 订单ID -> 明细列表。"""
    result: dict[str, list[Any]] = {}
    if not orders:
        return result

    groups: dict[tuple[str, str], list[Any]] = {}
    for order in orders:
        key = (
            order_client._clean_text(order.table_base) or order_client.DEFAULT_TABLE_BASE,
            order_client._clean_text(order.order_by_item_for_ary) or order_client.DEFAULT_ORDER_ITEM_BY,
        )
        groups.setdefault(key, []).append(order)

    batches: list[tuple[str, str, list[Any]]] = []
    for (table_base, order_item_by), group_orders in groups.items():
        for chunk in _chunked(group_orders, max(1, batch_size)):
            batches.append((table_base, order_item_by, chunk))

    total_orders = len(orders)
    processed = 0
    for index, (table_base, order_item_by, chunk) in enumerate(batches, start=1):
        html_map = order_client._fetch_order_item_html_map(client, chunk, table_base, order_item_by)
        for order in chunk:
            html = html_map.get(order_client._clean_text(order.order_id), "")
            if not html and len(chunk) == 1 and len(html_map) == 1:
                html = next(iter(html_map.values()))
            result[order_client._clean_text(order.order_id)] = (
                order_client._parse_item_rows(html, order) if html else []
            )
        processed += len(chunk)
        if index % 5 == 0 or index == len(batches):
            _emit(progress, f"商品明细进度：{processed}/{total_orders} 单，批次 {index}/{len(batches)}")
    return result


# ---------------------------------------------------------------------------
# 汇总：同订单同 SKU 合并，跨订单重复 SKU 只留一条
# ---------------------------------------------------------------------------

def aggregate_detail_rows(orders: Sequence[Any], items_map: dict[str, list[Any]]) -> list[OrderSkuRow]:
    """把订单商品明细按 (订单编号, SKU) 聚合，返回明细行。"""
    order_meta = {order_client._clean_text(item.order_id): item for item in orders}
    aggregated: dict[tuple[str, str], OrderSkuRow] = {}

    for order_id, items in items_map.items():
        base = order_meta.get(order_id)
        for item in items:
            sku = order_client._clean_text(item.sku)
            if not sku:
                continue
            order_no = order_client._clean_text(item.platform_order_id or item.order_id)
            key = (order_no, sku.upper())
            quantity = _to_float(item.quantity) or 1.0
            price = _to_float(item.sell_price)
            existing = aggregated.get(key)
            if existing is not None:
                existing.quantity += quantity
                existing.amount += quantity * price
                continue

            row = OrderSkuRow(
                order_no=order_no,
                mabang_order_id=order_id,
                shop=order_client._clean_text(item.shop_name),
                pay_time=_datetime_text(item.pay_time),
                create_time=_datetime_text(item.create_time),
                buyer=order_client._clean_text(item.buyer_name),
                sales=order_client._clean_text(item.store_manager),
                order_weight=_format_number(item.weight, digits=2),
                sku=sku,
                product_name=order_client._clean_text(item.product_name),
                quantity=quantity,
                unit_price=price,
                amount=quantity * price,
            )
            if base is not None:
                meta = _order_row_to_record(getattr(base, "raw", {}) or {})
                row.platform = meta.platform
                row.order_status = meta.order_status
                row.create_time = meta.create_time or row.create_time
                row.ship_time = meta.ship_time
                row.sales = meta.sales or row.sales
                row.mabang_order_id = meta.mabang_order_id or row.mabang_order_id
                row.order_weight = meta.order_weight or row.order_weight
            aggregated[key] = row

    return sorted(aggregated.values(), key=lambda item: (item.order_no, item.sku))


def build_unique_rows(rows: Sequence[OrderSkuRow]) -> list[UniqueSkuRow]:
    """按 SKU 去重，统计出现订单数与总数量。"""
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = buckets.setdefault(row.sku, {"names": [], "orders": [], "quantity": 0.0})
        if row.product_name and row.product_name not in bucket["names"]:
            bucket["names"].append(row.product_name)
        if row.order_no and row.order_no not in bucket["orders"]:
            bucket["orders"].append(row.order_no)
        bucket["quantity"] += row.quantity

    unique_rows = [
        UniqueSkuRow(
            sku=sku,
            product_name=bucket["names"][0] if bucket["names"] else "",
            order_count=len(bucket["orders"]),
            total_quantity=bucket["quantity"],
            order_numbers=tuple(bucket["orders"]),
        )
        for sku, bucket in buckets.items()
    ]
    unique_rows.sort(key=lambda item: (-item.order_count, item.sku))
    return unique_rows


# ---------------------------------------------------------------------------
# Excel 输出
# ---------------------------------------------------------------------------

HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
TITLE_FONT = Font(bold=True, size=11)
THIN_BORDER = Border(*(Side(style="thin", color="D9D9D9"),) * 4)
CENTER = Alignment(horizontal="center", vertical="center")


def _write_sheet(worksheet, headers: Sequence[str], rows: Sequence[Sequence[Any]], *, widths: Sequence[int] | None = None) -> None:
    worksheet.append(list(headers))
    for cell in worksheet[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = CENTER
        cell.border = THIN_BORDER
    for row in rows:
        worksheet.append(list(row))
    for row_cells in worksheet.iter_rows(min_row=2, max_row=worksheet.max_row):
        for cell in row_cells:
            cell.border = THIN_BORDER
            cell.alignment = Alignment(vertical="center")
    worksheet.freeze_panes = "A2"
    if worksheet.max_row > 1:
        worksheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{worksheet.max_row}"
    for index, header in enumerate(headers, start=1):
        current = worksheet.column_dimensions[get_column_letter(index)].width or 0
        target = (widths[index - 1] if widths and index - 1 < len(widths) else 0) or max(12, len(str(header)) * 2 + 4)
        worksheet.column_dimensions[get_column_letter(index)].width = min(60, max(current, target))


def _row_values(row: OrderSkuRow, fields: Sequence[str]) -> list[Any]:
    mapping: dict[str, Any] = {
        "order_no": row.order_no,
        "shop": row.shop,
        "platform": row.platform,
        "order_status": row.order_status,
        "pay_time": row.pay_time,
        "create_time": row.create_time,
        "buyer": row.buyer,
        "sku": row.sku,
        "product_name": row.product_name,
        "quantity": _format_number(row.quantity, digits=2),
        "unit_price": _format_number(row.unit_price),
        "amount": _format_number(row.amount),
        "order_weight": row.order_weight,
        "ship_time": row.ship_time,
        "sales": row.sales,
        "mabang_order_id": row.mabang_order_id,
    }
    return [mapping.get(key, "") for key in fields]


def build_workbook(
    *,
    rows: Sequence[OrderSkuRow],
    unique_rows: Sequence[UniqueSkuRow],
    not_found: Sequence[str],
    job: OrderSkuExportJob,
    output_file: Path,
) -> None:
    workbook = Workbook()
    default_sheet = workbook.active
    workbook.remove(default_sheet)

    # 主表：唯一 SKU 汇总（对应「汇总到 1 个表、重复项只留唯一 SKU」）
    sku_sheet = workbook.create_sheet("唯一SKU")
    sku_headers = ["SKU", "商品中文名称", "出现订单数", "商品总数量"]
    if job.include_detail:
        sku_headers.append("关联订单编号")
    sku_values = [
        [
            item.sku,
            item.product_name,
            item.order_count,
            _format_number(item.total_quantity, digits=2),
            *([" / ".join(item.order_numbers[:20]) + (" ..." if len(item.order_numbers) > 20 else "")] if job.include_detail else []),
        ]
        for item in unique_rows
    ]
    _write_sheet(sku_sheet, sku_headers, sku_values, widths=[26, 46, 12, 12, 60])

    # 明细表：按勾选字段输出，便于回溯订单来源
    if job.include_detail:
        detail_sheet = workbook.create_sheet("订单明细")
        headers = [FIELD_LABELS[key] for key in job.fields]
        detail_values = [_row_values(row, job.fields) for row in rows]
        _write_sheet(detail_sheet, headers, detail_values)

    # 未查询到的订单号，方便用户核对
    if job.include_not_found:
        missing_sheet = workbook.create_sheet("未查询到订单")
        _write_sheet(missing_sheet, ["订单编号", "说明"], [[item, "马帮未检索到该订单编号"] for item in not_found], widths=[28, 30])

    workbook.properties.title = "马帮订单SKU汇总"
    workbook.properties.creator = "HQYL Automation"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output_file)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_mabang_order_sku_export(job: OrderSkuExportJob, progress: ProgressCallback | None = None) -> OrderSkuExportResult:
    total = len(job.order_numbers)
    batches = _chunked(list(job.order_numbers), MAX_ORDERS_PER_BATCH)
    _emit(progress, f"待查询订单编号 {total} 个，分 {len(batches)} 批（每批最多 {MAX_ORDERS_PER_BATCH} 个）")

    all_rows: list[OrderSkuRow] = []
    matched_orders: dict[str, Any] = {}
    not_found: list[str] = []

    with MabangClient(job.base_url, timeout_seconds=300, verify_ssl=False) as client:
        _emit(progress, "正在登录马帮 ERP")
        client.login(job.username, job.password)
        order_client._parse_order_context(client, order_client._get_order_page_html(client))
        _emit(progress, "登录成功，开始批量查询订单")

        for index, chunk in enumerate(batches, start=1):
            _emit(progress, f"第 {index}/{len(batches)} 批：按订单编号查询 {len(chunk)} 个订单号")
            orders, missing = _batch_search_orders(
                client,
                chunk,
                rows_per_page=DEFAULT_ROWS_PER_PAGE,
                progress=progress,
            )
            for order in orders:
                matched_orders.setdefault(order.order_id, order)
            not_found.extend(missing)
            _emit(progress, f"第 {index}/{len(batches)} 批：命中 {len(orders)} 单，未命中 {len(missing)} 个")

        # 未命中兜底：用返回的订单号集合与输入集合做差集
        matched_order_numbers = {
            order_client._clean_text(order.platform_order_id) for order in matched_orders.values()
        }
        for order_no in job.order_numbers:
            if order_no not in matched_order_numbers and order_no not in not_found:
                not_found.append(order_no)

        _emit(progress, f"批量查询完成：命中 {len(matched_orders)} 单，未命中 {len(not_found)} 个")

        order_rows = list(matched_orders.values())
        items_map = _fetch_items_for_orders(
            client,
            order_rows,
            batch_size=job.detail_batch_size,
            progress=progress,
        )

    # 明细聚合：同一订单同一 SKU 合并数量与金额；跨订单重复的 SKU 在唯一清单里只留一条
    detail_rows = aggregate_detail_rows(order_rows, items_map)
    unique_rows = build_unique_rows(detail_rows)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = job.output_dir
    output_file = output_dir / f"{OUTPUT_FILENAME_PREFIX}_{timestamp}.xlsx"

    _emit(progress, f"正在生成汇总表：{len(detail_rows)} 条明细，{len(unique_rows)} 个唯一 SKU")
    build_workbook(
        rows=detail_rows,
        unique_rows=unique_rows,
        not_found=not_found,
        job=job,
        output_file=output_file,
    )
    _emit(progress, f"导出完成：{output_file}")

    preview_rows = tuple(
        {
            "sku": item.sku,
            "product_name": item.product_name,
            "order_count": item.order_count,
            "total_quantity": _format_number(item.total_quantity, digits=2),
            "order_numbers": " / ".join(item.order_numbers[:5]),
        }
        for item in unique_rows[:100]
    )

    return OrderSkuExportResult(
        output_file=output_file,
        output_dir=output_dir,
        input_order_count=total,
        matched_order_count=len(matched_orders),
        matched_order_numbers=tuple(sorted(matched_order_numbers)),
        not_found_orders=tuple(not_found),
        detail_row_count=len(detail_rows),
        unique_sku_count=len(unique_rows),
        unique_skus=tuple(unique_rows),
        preview_rows=preview_rows,
    )
