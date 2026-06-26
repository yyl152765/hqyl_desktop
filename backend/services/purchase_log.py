from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urljoin

import pandas as pd
from bs4 import BeautifulSoup

from backend.core.mabang_client import MabangApiError, MabangClient, decode_html_page


BASE_URL = "https://900853.private.mabangerp.com"
PRIVATE_BASE_URL = "https://private-amz.mabangerp.com"
STOCK_LIST_PAGE_PATH = "/index.php?mod=stock.list&searchStatus=3"
STOCK_LIST_API_URL = f"{PRIVATE_BASE_URL}/index.php?mod=stock.getStockList"
STOCK_MODIFY_PAGE_PATH = "/index.php?mod=stock.modify&id={stock_id}"
PURCHASE_LOG_API_URL = f"{PRIVATE_BASE_URL}/index.php?mod=stock.findRecordOfOrder"
DEFAULT_ROWS_PER_PAGE = 500

OUTPUT_COLUMNS = (
    "sku编号",
    "采购单号",
    "下单员/采购员",
    "采购日期",
    "供应商",
    "采购价格",
    "采购数量",
    "仓库",
    "备注",
)

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class PurchaseLogQuery:
    username: str
    password: str
    sku_list: tuple[str, ...]
    start_date: str
    end_date: str
    output_dir: Path
    rows_per_page: int = DEFAULT_ROWS_PER_PAGE


@dataclass(frozen=True)
class PurchaseLogResult:
    record_count: int
    sku_count: int
    output_file: Path


@dataclass(frozen=True)
class StockLookup:
    sku: str
    stock_id: str


def safe_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def parse_sku_text(value: str) -> tuple[str, ...]:
    seen: set[str] = set()
    sku_list: list[str] = []
    for line in value.splitlines():
        sku = safe_text(line)
        if not sku or sku in seen:
            continue
        seen.add(sku)
        sku_list.append(sku)
    return tuple(sku_list)


def validate_query_payload(payload: dict) -> PurchaseLogQuery:
    username = safe_text(payload.get("username"))
    password = safe_text(payload.get("password"))
    sku_text = safe_text(payload.get("sku_text"))
    start_date = safe_text(payload.get("start_date"))
    end_date = safe_text(payload.get("end_date"))
    output_dir = Path(safe_text(payload.get("output_dir"))).expanduser()

    if not username:
        raise ValueError("请输入马帮账号")
    if not password:
        raise ValueError("请输入马帮密码")
    sku_list = parse_sku_text(sku_text)
    if not sku_list:
        raise ValueError("请输入至少一个 SKU")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_date):
        raise ValueError("开始日期格式必须是 YYYY-MM-DD")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end_date):
        raise ValueError("结束日期格式必须是 YYYY-MM-DD")
    start_at = datetime.strptime(start_date, "%Y-%m-%d")
    end_at = datetime.strptime(end_date, "%Y-%m-%d")
    if start_at > end_at:
        raise ValueError("开始日期不能晚于结束日期")
    if not output_dir:
        raise ValueError("请选择输出目录")
    try:
        rows_per_page = int(payload.get("rows_per_page") or DEFAULT_ROWS_PER_PAGE)
    except (TypeError, ValueError):
        rows_per_page = DEFAULT_ROWS_PER_PAGE

    return PurchaseLogQuery(
        username=username,
        password=password,
        sku_list=sku_list,
        start_date=start_date,
        end_date=end_date,
        output_dir=output_dir,
        rows_per_page=max(50, min(rows_per_page, 1000)),
    )


def normalize_date_bounds(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    start_at = datetime.strptime(start_date, "%Y-%m-%d")
    end_at = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    return start_at, end_at


def parse_datetime(value: object) -> datetime | None:
    text = safe_text(value)
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).replace("/", "-")
    match = re.search(r"\d{4}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{1,2}(?::\d{1,2})?)?", text)
    if not match:
        return None
    normalized = match.group(0)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None


def open_iframe(client: MabangClient, page_path: str) -> tuple[str, str]:
    response = client.client.get(client._url(page_path))
    response.raise_for_status()
    page_url = str(response.url)
    page_html = decode_html_page(response)
    soup = BeautifulSoup(page_html, "html.parser")
    iframe = soup.find("iframe", id="iframeContent") or soup.find("iframe")
    iframe_src = safe_text(iframe.get("src") if iframe else "")
    if not iframe_src:
        return page_url, page_html
    iframe_url = urljoin(page_url, iframe_src)
    iframe_response = client.client.get(iframe_url, headers={"Referer": page_url})
    iframe_response.raise_for_status()
    return str(iframe_response.url), decode_html_page(iframe_response)


def post_form_json(client: MabangClient, url: str, payload: list[tuple[str, str]], referer: str) -> dict:
    response = client.client.post(
        url,
        content=urlencode(payload, doseq=True),
        headers={
            "Referer": referer,
            "Origin": PRIVATE_BASE_URL,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise MabangApiError(f"接口返回格式异常: {url}")
    if data.get("success") is False and "登录信息已超时" in safe_text(data.get("message")):
        raise MabangApiError("马帮登录信息已超时，请重新运行查询")
    return data


def build_stock_lookup_payload(sku: str) -> list[tuple[str, str]]:
    return [
        ("searchKey", "Stock_stockSku"),
        ("operate", "="),
        ("orderBys[]", ""),
        ("Stock_stockSku", ""),
        ("Stock_nameCN", ""),
        ("Stock_nameEN", ""),
        ("Stock_defaultRetailNameCn", ""),
        ("StockPlus_financial", ""),
        ("search-content", "库存SKU"),
        ("searchValue", sku),
        ("status", "3"),
        ("parentCategoryId", ""),
        ("categoryId", ""),
        ("third_category_id", ""),
        ("parentBrandId", ""),
        ("list-brandId", ""),
        ("labelId", ""),
        ("buyerId", ""),
        ("developerIdM", ""),
        ("dev_assistant", ""),
        ("artDesignerId", ""),
        ("salesId", ""),
        ("defaultStockWarehouseDetailId", ""),
        ("livenessType", ""),
        ("isNewType", ""),
        ("stock_type", ""),
        ("isMachining", ""),
        ("showstart", "1"),
        ("isCloud", ""),
        ("isGift", ""),
        ("exceptionDeclaration", ""),
        ("singleWarehouseType", ""),
        ("isGoogsExpireManageSearch", ""),
        ("page", "1"),
        ("rowsPerPage", "10"),
        ("stockOrderby", ""),
        ("warehouseMold", "all"),
        ("isBatchSearch", "0"),
    ]


def lookup_stock_id(client: MabangClient, sku: str, stock_list_referer: str) -> StockLookup | None:
    data = post_form_json(client, STOCK_LIST_API_URL, build_stock_lookup_payload(sku), stock_list_referer)
    if not data.get("success"):
        raise MabangApiError(f"查询 SKU 失败: {sku}，返回: {safe_text(data.get('message')) or data}")
    rows = data.get("stockData") or []
    if not isinstance(rows, list):
        raise MabangApiError(f"查询 SKU 返回字段异常: {sku}")
    normalized_sku = sku.strip()
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_sku = safe_text(row.get("stockSku") or row.get("sku"))
        stock_id = safe_text(row.get("id") or row.get("stockId"))
        if row_sku == normalized_sku and stock_id:
            return StockLookup(sku=row_sku, stock_id=stock_id)
    return None


def parse_purchase_log_rows(html_fragment: str) -> list[dict]:
    soup = BeautifulSoup(f"<table>{html_fragment}</table>", "html.parser")
    records: list[dict] = []
    for tr in soup.find_all("tr"):
        cells = [cell.get_text("\n", strip=True) for cell in tr.find_all("td")]
        if len(cells) < len(OUTPUT_COLUMNS):
            continue
        normalized = [re.sub(r"\s*\n\s*", "/", cell).strip() for cell in cells[: len(OUTPUT_COLUMNS)]]
        records.append(dict(zip(OUTPUT_COLUMNS, normalized)))
    return records


def fetch_purchase_log_page(
    client: MabangClient,
    *,
    stock_id: str,
    page: int,
    rows_per_page: int,
    referer: str,
) -> tuple[list[dict], int]:
    payload = [
        ("stockId", stock_id),
        ("page", str(page)),
        ("rowsPerPage", str(rows_per_page)),
        ("createTimeSort", "2"),
    ]
    data = post_form_json(client, PURCHASE_LOG_API_URL, payload, referer)
    if not data.get("success"):
        raise MabangApiError(f"查询采购日志失败: stockId={stock_id}，返回: {safe_text(data.get('message')) or data}")
    total_num_text = safe_text(data.get("totalNum"))
    total_num = int(total_num_text) if total_num_text.isdigit() else 0
    return parse_purchase_log_rows(safe_text(data.get("message"))), total_num


def purchase_date_in_range(record: dict, start_at: datetime, end_at: datetime) -> bool:
    purchase_at = parse_datetime(record.get("采购日期"))
    if purchase_at is None:
        return False
    return start_at <= purchase_at <= end_at


def query_purchase_logs(job: PurchaseLogQuery, progress: ProgressCallback | None = None) -> list[dict]:
    start_at, end_at = normalize_date_bounds(job.start_date, job.end_date)
    output_records: list[dict] = []
    with MabangClient(BASE_URL) as client:
        _emit(progress, "正在登录马帮...")
        client.login(job.username, job.password)
        _emit(progress, "正在打开库存 SKU 页面...")
        stock_list_referer, _ = open_iframe(client, STOCK_LIST_PAGE_PATH)

        total_skus = len(job.sku_list)
        for index, sku in enumerate(job.sku_list, start=1):
            _emit(progress, f"[{index}/{total_skus}] 正在查询 SKU: {sku}")
            lookup = lookup_stock_id(client, sku, stock_list_referer)
            if lookup is None:
                _emit(progress, f"[{index}/{total_skus}] 未找到 SKU: {sku}")
                continue

            modify_referer, _ = open_iframe(client, STOCK_MODIFY_PAGE_PATH.format(stock_id=lookup.stock_id))
            page = 1
            sku_records = 0
            fetched_records = 0
            while True:
                records, total_num = fetch_purchase_log_page(
                    client,
                    stock_id=lookup.stock_id,
                    page=page,
                    rows_per_page=job.rows_per_page,
                    referer=modify_referer,
                )
                fetched_records += len(records)
                for record in records:
                    if purchase_date_in_range(record, start_at, end_at):
                        output_records.append(record)
                        sku_records += 1
                if not records:
                    break
                if total_num and fetched_records >= total_num:
                    break
                if len(records) < job.rows_per_page and not total_num:
                    break
                page += 1
                if page > 500:
                    raise MabangApiError(f"采购日志分页超过 500 页，已停止: {sku}")
            _emit(progress, f"[{index}/{total_skus}] {sku} 匹配到 {sku_records} 条采购日志")
    return output_records


def export_records(records: list[dict], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"SKU采购日志_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    pd.DataFrame(records, columns=OUTPUT_COLUMNS).to_excel(output_file, index=False)
    return output_file


def run_purchase_log_query(job: PurchaseLogQuery, progress: ProgressCallback | None = None) -> PurchaseLogResult:
    records = query_purchase_logs(job, progress)
    output_file = export_records(records, job.output_dir)
    return PurchaseLogResult(
        record_count=len(records),
        sku_count=len(job.sku_list),
        output_file=output_file,
    )


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress:
        progress(message)
