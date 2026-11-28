from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from io import StringIO
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from backend.core.mabang_client import MabangApiError, MabangExportFile

LOGGER = logging.getLogger(__name__)

API_BASE = "https://mas-oapi-private.mabangerp.com"
PAGE_PATH = "/index.php?mod=reports.productSaleReports"
AUTH_PATH = "/mas-erp-system-service/auth/token/by-key"
OPTIONS_PATH = "/mas-erp-report-service/productSaleReportsFilterOptions"
STOCK_SHOP_REPORT_PATH = "/mas-erp-report-service/stockShopReports"
COUNTRY_REPORT_PATH = "/mas-erp-report-service/countryReports"
WAREHOUSE_REPORT_PATH = "/mas-erp-report-service/warehouseV2Reports"
WAREHOUSE_CHILD_PATH = "/mas-erp-report-service/getProductSaleChild"
PAGE_SIZE = 2000
MAX_TOTAL_ROWS = 1_000_000

TARGET_SALES_HEADERS = [
    "SKU",
    "商品中文名",
    "商品英文名",
    "item_id",
    "NCM",
    "主SKU",
    "状态",
    "统一成本价",
    "重量(单位:g)",
    "库存",
    "创建时间",
    "销售员",
    "开发员",
    "关联订单数",
    "销售数量",
    "平均单天销量",
    "平均销售价格",
    "收入-订单金额",
    "收入-运费",
    "收入-其他收入",
    "收入-补贴金额",
    "收入-合计",
    "支出-商品成本",
    "支出-运费",
    "支出-包材费",
    "支出-平台费",
    "支出-转帐费",
    "支出-头程费",
    "支出-FBA费用",
    "支出-VAT税费",
    "支出-优惠券",
    "支出-其他费用",
    "支出-其他支出",
    "支出-测评费",
    "支出-合计",
    "退款订单数",
    "退款商品数",
    "退款金额",
    "退款率%",
    "毛利",
    "毛利率%",
]

FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "SKU": ("stockSku",),
    "商品中文名": ("nameCN",),
    "商品英文名": ("nameEN",),
    "item_id": ("itemId",),
    "NCM": ("ncm",),
    "主SKU": ("salesSku",),
    "状态": ("status",),
    "统一成本价": ("defaultCost",),
    "重量(单位:g)": ("weight",),
    "库存": ("stockQuantity",),
    "创建时间": ("timeCreated",),
    "销售员": ("sale",),
    "开发员": ("develop",),
    "关联订单数": ("orderNum",),
    "销售数量": ("quantity",),
    "平均销售价格": ("averageSellingPrice",),
    "收入-订单金额": ("saleMoney",),
    "收入-运费": ("shippingTotal",),
    "收入-其他收入": ("otherIncome",),
    "收入-补贴金额": ("subsidyAmount",),
    "收入-合计": ("incomeTotal",),
    "支出-商品成本": ("itemTotalCost",),
    "支出-运费": ("shippingCostNew", "shippingCost"),
    "支出-包材费": ("packageFee",),
    "支出-平台费": ("platformFee",),
    "支出-转帐费": ("paypalFee",),
    "支出-头程费": ("headShipping",),
    "支出-FBA费用": ("fbaMoney",),
    "支出-VAT税费": ("vatTaxation",),
    "支出-优惠券": ("voucherPrice",),
    "支出-其他费用": ("otherMoney",),
    "支出-其他支出": ("otherExpend",),
    "支出-测评费": ("evaluationFee",),
    "支出-合计": ("expendTotal",),
    "退款订单数": ("refundNum",),
    "退款商品数": ("itemRefundNum",),
    "退款金额": ("refundMoneyRMB", "refundMoney"),
    "退款率%": ("returnRate",),
    "毛利": ("gross",),
    "毛利率%": ("grossRate",),
}

REQUIRED_ROW_HEADERS = tuple(header for header in TARGET_SALES_HEADERS if header not in {"平均单天销量", "平均销售价格"})
TEXT_HEADERS = {"SKU", "商品中文名", "商品英文名", "item_id", "NCM", "主SKU", "状态", "创建时间", "销售员", "开发员"}
# Native renderer treats explicit null numeric cells as zero. Missing keys are
# always a protocol error; the principal measures must also be non-null.
NON_NULL_MEASURES = {"销售数量", "收入-订单金额", "收入-合计", "支出-合计", "毛利"}
ADDITIVE_HEADERS = tuple(h for h in TARGET_SALES_HEADERS if h not in TEXT_HEADERS | {
    "统一成本价", "重量(单位:g)", "库存", "平均单天销量", "平均销售价格", "退款率%", "毛利率%"})
WAREHOUSE_CHECK_HEADERS = ("销售数量", "收入-订单金额", "收入-合计", "支出-合计", "毛利")


class ModernSalesAuthenticationExpired(MabangApiError):
    pass


class ModernSalesSnapshotChanged(MabangApiError):
    pass


@dataclass(frozen=True)
class ModernOption:
    id: str
    name: str


@dataclass(frozen=True)
class ModernFilterOptions:
    categories: tuple[ModernOption, ...]
    warehouses: tuple[ModernOption, ...]
    developers: tuple[ModernOption, ...]
    salespeople: tuple[ModernOption, ...]


@dataclass(frozen=True)
class ModernSalesResult:
    rows: tuple[dict[str, Any], ...]
    total: int
    snapshot: tuple[Any, ...]
    currency: str


class ModernSalesReportSession:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.headers = _authenticate(client)

    def post(self, path: str, body: dict[str, Any], *, context: str) -> Any:
        try:
            return _json_request(self.client, path, body, self.headers, context=context)
        except ModernSalesAuthenticationExpired:
            self.headers = _authenticate(self.client)
            return _json_request(self.client, path, body, self.headers, context=context)

    def filter_options(self) -> ModernFilterOptions:
        data = self.post(OPTIONS_PATH, {}, context="新版销量筛选目录")
        if not isinstance(data, dict):
            raise MabangApiError("新版销量筛选目录结构无效")
        return parse_filter_options_payload(data)

    def fetch_product_rows(self, form_data: Any, *, report_type: str | None = None) -> ModernSalesResult:
        filters = modern_filters_from_legacy_form(form_data, report_type=report_type)
        rows, total, snapshot = _fetch_report_rows(self, filters, context="新版商品销量报表")
        return ModernSalesResult(rows=tuple(rows), total=total, snapshot=snapshot, currency=filters["currency"])


# --------------------------------------------------------------------------- public helpers


def _session(client: Any) -> ModernSalesReportSession:
    session = getattr(client, "_modern_report_session", None)
    if session is None:
        session = ModernSalesReportSession(client)
        client._modern_report_session = session
    return session


def fetch_modern_filter_options(client: Any) -> ModernFilterOptions:
    return _session(client).filter_options()


def probe_modern_report_entry(client: Any) -> bool:
    """Return True only when the account really serves the MAS report SPA.

    A successful classic page has no trusted SPA iframe. Network/permission
    failures do not prove that the account is using the classic protocol.
    """
    if not getattr(client, "_logged_in", False):
        return False
    try:
        response = client.client.get(
            client._url(PAGE_PATH),
            timeout=httpx.Timeout(getattr(client, "timeout_seconds", 60), connect=10),
            follow_redirects=False,
        )
    except httpx.HTTPError:
        raise MabangApiError("销量入口探测网络异常，无法判断新旧版本") from None
    _raise_if_unauthenticated_page(response, context="销量入口探测")
    if response.status_code != 200:
        raise MabangApiError(f"销量入口探测被拒绝: HTTP={response.status_code}，未切换旧协议")
    if _trusted_spa_url(response) is not None:
        return True
    soup = BeautifulSoup(response.text, "html.parser")
    if soup.select('[name="shopLabelIds[]"], [name="stockWarehouseId[]"], [name="developid[]"]'):
        return False
    raise MabangApiError("销量入口未返回可识别的新旧报表页面")


def _trusted_spa_url(response):
    for frame in BeautifulSoup(response.text, "html.parser").find_all("iframe"):
        raw = frame.get("src") or ""
        if not raw:
            continue
        try:
            url = urlsplit(urljoin(str(response.url), raw))
            port = url.port
        except ValueError:
            continue
        if (
            url.scheme == "https"
            and url.hostname == "fe-private.mabangerp.com"
            and port in (None, 443)
            and not url.username
            and not url.password
            and url.path.rstrip("/") == "/views/product-sale-reports"
            and not url.fragment
        ):
            return url
    return None


def modern_download_product_sales_report_csv(
    client: Any,
    *,
    form_data: Any,
    report_type: str | None = None,
    platform_or_shop: str | None = None,
    fields: Sequence[str] | None = None,
) -> MabangExportFile:
    session = _session(client)
    filters = modern_filters_from_legacy_form(form_data, report_type=report_type, platform_or_shop=platform_or_shop)
    rows, total, snapshot = _fetch_report_rows(session, filters, context="新版商品销量报表导出")
    income_only = fields is not None and tuple(fields) == ("salesSkuNewId", "sale", "develop", "income")
    if rows and not income_only:
        from backend.core.mabang_sales_metadata import enrich_ncm

        enrich_ncm(client, rows)
    if income_only:
        stream = StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["SKU", "销售员", "开发员", "收入-订单金额"])
        for row in rows:
            if "sale" not in row or "develop" not in row:
                raise MabangApiError("新版人员收入明细缺少人员字段")
            writer.writerow([row["stockSku"], _safe_text(row["sale"]), _safe_text(row["develop"]),
                             format_decimal(_measure(row, "收入-订单金额", "新版人员收入"))])
        content = ("\ufeff" + stream.getvalue()).encode("utf-8")
    else:
        content = rows_to_sales_csv(rows, expected_days=_expected_days(filters), context="新版商品销量报表导出")
    return MabangExportFile(
        file_name="product-sales-report.csv",
        file_type="csv",
        file_url="modern://product-sale-reports",
        content=content,
        metadata={
            "reportType": report_type or filters.get("reportType", ""),
            "platformOrShop": platform_or_shop or filters.get("platformOrShop", ""),
            "fieldCodes": list(fields or ()),
            "firstPageHasRows": bool(rows),
            "updateDateStr": _safe_text(snapshot[0]) if snapshot else "",
            "total": total,
            "source": "modern-product-sale-reports",
        },
    )


def modern_query_sales_report_summary(
    client: Any,
    *,
    form_data: Any,
    report_type: str | None = None,
    platform_or_shop: str | None = None,
) -> dict[str, Any]:
    session = _session(client)
    filters = modern_filters_from_legacy_form(form_data, report_type=report_type, platform_or_shop=platform_or_shop)
    rows, total, snapshot = _fetch_report_rows(session, filters, context="新版商品销量报表汇总")
    income_total = Decimal("0")
    for index, row in enumerate(rows, start=1):
        income_total += _decimal_value(_field_value(row, "收入-订单金额"), context=f"新版商品销量报表汇总 row={index} 收入-订单金额")
    amount = format_decimal(income_total, places=4)
    return {
        "success": True,
        "total": total,
        "orderIncomeTotal": amount,
        "currency": filters["currency"],
        "updateDateStr": _safe_text(snapshot[0]) if snapshot else "",
        "source": "modern-product-sale-reports",
    }


def rows_to_sales_csv(rows: Sequence[dict[str, Any]], *, expected_days: int, context: str) -> bytes:
    if expected_days <= 0:
        raise MabangApiError(f"{context} 统计天数无效")
    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(TARGET_SALES_HEADERS)
    for index, row in enumerate(rows, start=1):
        row_context = f"{context} row={index}"
        missing = [header for header in REQUIRED_ROW_HEADERS if not any(key in row for key in FIELD_CANDIDATES[header])]
        if missing:
            preview = ", ".join(missing[:8])
            raise MabangApiError(f"{row_context} 缺少新版字段映射: {preview}")
        quantity = _decimal_value(_field_value(row, "销售数量"), context=f"{row_context} 销售数量")
        order_income = _decimal_value(_field_value(row, "收入-订单金额"), context=f"{row_context} 收入-订单金额")
        if not _safe_text(_field_value(row, "SKU")):
            raise MabangApiError(f"{row_context} SKU 为空")
        for header in REQUIRED_ROW_HEADERS:
            if header not in TEXT_HEADERS:
                _measure(row, header, row_context)
        output_row: list[str] = []
        for header in TARGET_SALES_HEADERS:
            if header == "平均单天销量":
                output_row.append(format_decimal((quantity / Decimal(expected_days)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), places=2) + "\t")
            elif header == "平均销售价格":
                value = Decimal("0") if quantity == 0 else order_income / quantity
                output_row.append(format_decimal(value, places=4) + "\t")
            elif header in {"退款率%", "毛利率%"}:
                output_row.append(format_decimal(_measure(row, header, row_context) * 100, places=2))
            elif header == "状态":
                output_row.append(_normalize_status(_field_value(row, header)))
            elif header not in TEXT_HEADERS:
                output_row.append(format_decimal(_measure(row, header, row_context)))
            else:
                output_row.append(_format_cell(_field_value(row, header)))
        writer.writerow(output_row)
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


# --------------------------------------------------------------------------- filters and options


def parse_filter_options_payload(data: dict[str, Any]) -> ModernFilterOptions:
    categories = _parse_categories(data.get("shopCategories") or data.get("shopCategoryList") or [])
    warehouses = _parse_options_from_candidates(data, ("warehouses", "warehouseList", "stockWarehouses", "stockWarehouseList"))
    developers = _parse_options_from_candidates(data, ("developers", "developerList", "developList", "develops"), allow_zero=True)
    salespeople = _parse_options_from_candidates(data, ("sales", "salespeople", "salespersonList", "saleList", "salesList", "sellers"), allow_zero=True)
    return ModernFilterOptions(categories=categories, warehouses=warehouses, developers=developers, salespeople=salespeople)


def modern_filters_from_legacy_form(
    form_data: Any,
    *,
    report_type: str | None = None,
    platform_or_shop: str | None = None,
) -> dict[str, Any]:
    values = _legacy_values(form_data)

    def single(name: str, default: str = "") -> str:
        entries = values.get(name, [])
        if not entries:
            return default
        if len(entries) != 1:
            raise MabangApiError(f"新版销量查询参数重复: {name}")
        return _safe_text(entries[0])

    report = _safe_text(report_type) or _safe_text(values.get("__reportType", [""])[0])
    platform = _safe_text(platform_or_shop) or single("platformOrShop", "")
    if not report:
        report = "warehouseReports" if platform == "warehouse" else "countryReports"
    if report not in {"countryReports", "warehouseReports", "stockShopReports"}:
        raise MabangApiError(f"新版销量查询不支持该报表类型: {report or '-'}")

    currency = single("moneyType", "RMB").upper()
    currency = "USD" if currency == "US" else currency
    if currency not in {"RMB", "USD"}:
        raise MabangApiError(f"新版销量查询币种未验证: {currency or '-'}")
    time_start = single("timeStart")
    time_end = single("timeEnd")
    if not time_start or not time_end:
        raise MabangApiError("新版销量查询缺少付款日期范围")

    if report == "warehouseReports":
        report_mode = "warehousev2"
        platform_value = "warehouse"
    elif report == "stockShopReports":
        report_mode = "stockShop"
        platform_value = "stockShop"
    else:
        report_mode = "countryCode"
        platform_value = "country"

    filters: dict[str, Any] = {
        "reportType": report,
        "platformOrShop": platform or platform_value,
        "type": report_mode,
        "timeStart": time_start,
        "timeEnd": time_end,
        "timeKey": single("timeKey", "payTime") or "payTime",
        "historyType": _int_choice(single("historyType", "1"), "historyType", {0, 1, 2}),
        "searchType": single("searchType", "stockSku") or "stockSku",
        "operate": single("operate", "eq") or "eq",
        "searchValue": single("searchValue", ""),
        "currency": currency,
        "moneySuffix": currency,
        "page": 1,
        "rowsPerPage": PAGE_SIZE,
        "orderByField": "quantity",
        "orderByValue": "desc",
        "shopLabelIds": _list_values(values, "shopLabelIds"),
        "stockWarehouseId": _positive_int_list(_list_values(values, "stockWarehouseId"), "仓库ID"),
        "developId": _list_values(values, "developid") or _list_values(values, "developId"),
        "saleId": _list_values(values, "saleid") or _list_values(values, "saleId"),
    }
    if single("formType") == "2":
        if single("selectTypeMore") != "stockSkuIn":
            raise MabangApiError("新版销量批量查询类型未验证")
        filters.update(searchType="stockSku", operate="eq", searchValue="",
                       searchValues=list(dict.fromkeys(single("selectValueMore").splitlines())))
    return filters


def _report_path_for_filters(filters: dict[str, Any]) -> str:
    if filters.get("reportType") == "warehouseReports":
        return WAREHOUSE_REPORT_PATH
    if filters.get("reportType") == "countryReports":
        return COUNTRY_REPORT_PATH
    return STOCK_SHOP_REPORT_PATH


def _legacy_values(form_data: Any) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    items = form_data.items() if hasattr(form_data, "items") else list(form_data or [])
    for key, value in items:
        name = str(key).removesuffix("[]")
        values = list(value) if isinstance(value, (list, tuple, set)) and not isinstance(value, (str, bytes)) else [value]
        result.setdefault(name, []).extend(values)
    return result


def _list_values(values: dict[str, list[Any]], key: str) -> list[str]:
    return [_safe_text(value) for value in values.get(key, []) if _safe_text(value) or _safe_text(value) == "0"]


def _positive_int_list(values: Iterable[str], label: str) -> list[int]:
    result: list[int] = []
    for value in values:
        text = _safe_text(value)
        if not text:
            continue
        if not text.isascii() or not text.isdecimal() or int(text) <= 0:
            raise MabangApiError(f"新版销量查询{label}无效")
        result.append(int(text))
    return result


# --------------------------------------------------------------------------- HTTP and auth


def _authenticate(client: Any) -> dict[str, str]:
    if not getattr(client, "_logged_in", False):
        raise MabangApiError("新版销量入口读取失败: 马帮客户端当前尚未登录")
    try:
        response = client.client.get(client._url(PAGE_PATH), timeout=httpx.Timeout(getattr(client, "timeout_seconds", 60), connect=10), follow_redirects=False)
    except httpx.HTTPError:
        raise MabangApiError("新版销量入口读取失败") from None
    _raise_if_unauthenticated_page(response, context="新版销量入口")
    if response.status_code != 200:
        raise MabangApiError(f"新版销量入口不可用: HTTP={response.status_code}")
    url = _trusted_spa_url(response)
    if url is None:
        raise MabangApiError("新版销量入口地址不符合已验证的接口契约")
    query = parse_qs(url.query, keep_blank_values=True)

    key = _required_query_param(query, "key")
    lang = _required_query_param(query, "lang", default="cn")
    timezone = _required_query_param(query, "tz", default="UTC+8")
    headers = {"Content-Type": "application/json", "key": key, "cluster-id": "2"}
    auth = _json_request(
        client,
        AUTH_PATH,
        {"key": key, "lang": "zh" if lang == "cn" else lang, "clusterId": "2"},
        headers,
        context="新版销量认证",
    )
    if not isinstance(auth, dict) or not isinstance(auth.get("token"), str):
        raise MabangApiError("新版销量认证结果无效")
    token = auth["token"].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or any(ord(char) <= 32 or ord(char) > 126 for char in token):
        raise MabangApiError("新版销量认证令牌无效")
    return {
        "Content-Type": "application/json",
        "ProjectId": "erp",
        "Authorization": "Bearer " + token,
        "key": key,
        "cluster-id": "2",
        "lang": lang,
        "timezone": timezone,
    }


def _json_request(client: Any, path: str, body: dict[str, Any], headers: dict[str, str], *, context: str) -> Any:
    for attempt in range(1, 4):
        reason = "invalid_json"
        try:
            response = client.client.post(
                API_BASE + path,
                json=body,
                headers=headers,
                timeout=httpx.Timeout(getattr(client, "timeout_seconds", 60), connect=10, write=30, pool=30),
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            reason = type(exc).__name__
        else:
            if response.status_code == 401:
                raise ModernSalesAuthenticationExpired(f"{context}: 新版销量登录已失效")
            if response.status_code in {429, 500, 502, 503, 504}:
                reason = f"HTTP={response.status_code}"
            elif not 200 <= response.status_code < 300:
                raise MabangApiError(f"{context}请求失败: HTTP={response.status_code}")
            else:
                try:
                    payload = response.json(parse_float=Decimal)
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    if payload.get("code") in {401, 1001}:
                        raise ModernSalesAuthenticationExpired(f"{context}: 新版销量登录已失效")
                    if type(payload.get("code")) is not int or payload["code"] not in {0, 200} or payload.get("success") is False:
                        raise MabangApiError(f"{context}被接口拒绝")
                    return payload.get("data")
        if attempt == 3:
            raise MabangApiError(f"{context}读取重试耗尽: {reason}") from None
        LOGGER.warning("%s只读重试: attempt=%s/3 reason=%s", context, attempt, reason)
        time.sleep(float(attempt))
    raise MabangApiError(f"{context}读取失败")


def _fetch_pages(
    session: ModernSalesReportSession,
    path: str,
    filters: dict[str, Any],
    *,
    context: str,
    snapshot_anchor: tuple[Any, ...] | None = None,
    allow_undated_child: bool = False,
) -> tuple[list[dict[str, Any]], int, tuple[Any, ...]]:
    total: int | None = None
    snapshot: tuple[Any, ...] | None = None
    collected: list[dict[str, Any]] = []
    page_fingerprints: set[str] = set()
    row_fingerprints: set[str] = set()
    identities: set[tuple[Any, ...]] = set()
    page = 1
    while True:
        body = dict(filters, page=page, rowsPerPage=PAGE_SIZE)
        data = session.post(path, body, context=f"{context} page={page}")
        if not isinstance(data, dict) or type(data.get("total")) is not int or not isinstance(data.get("data"), list):
            raise MabangApiError(f"{context}分页结构无效")
        observed = data["total"]
        if not 0 <= observed <= MAX_TOTAL_ROWS:
            raise MabangApiError(f"{context}总行数超出范围")
        current_snapshot = (data.get("reportDate"), data.get("updateTime"), data.get("timezone"), data.get("moneySuffix"))
        currency = _safe_text(data.get("moneySuffix")).upper()
        currency = "USD" if currency == "US" else currency
        if currency != filters.get("currency"):
            raise MabangApiError(f"{context}响应币种缺失或与请求不一致")
        if snapshot_anchor and current_snapshot[:3] == (None, None, None) and path == WAREHOUSE_CHILD_PATH:
            current_snapshot = snapshot_anchor[:3] + (current_snapshot[3],)
        undated_child = (allow_undated_child and path == WAREHOUSE_CHILD_PATH
                         and current_snapshot[:3] == (None, None, None))
        if not undated_child and any(not isinstance(value, str) or not value for value in current_snapshot[:3]):
            raise MabangApiError(f"{context}缺少统计快照或时区")
        if total is None:
            total = observed
            snapshot = current_snapshot
        if observed != total or current_snapshot != snapshot:
            changed = [name for name, before, after in zip(("reportDate", "updateTime", "timezone", "moneySuffix"), snapshot, current_snapshot) if before != after]
            raise ModernSalesSnapshotChanged(f"{context}分页期间统计快照发生变化: total={total}->{observed}, fields={','.join(changed) or '-'}")
        rows = data["data"]
        expected_count = min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE))
        if len(rows) != expected_count:
            raise MabangApiError(f"{context}分页不完整: page={page}")
        if any(not isinstance(row, dict) for row in rows):
            raise MabangApiError(f"{context}返回无效数据行")
        fingerprints = []
        for row in rows:
            fingerprint = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
            if fingerprint in row_fingerprints:
                raise MabangApiError(f"{context}存在重复明细或重复分页")
            row_fingerprints.add(fingerprint)
            fingerprints.append(fingerprint)
            if path in {WAREHOUSE_REPORT_PATH, WAREHOUSE_CHILD_PATH}:
                warehouse = row.get("stockWarehouseId")
                selected = filters.get("stockWarehouseId", [])
                if type(warehouse) is not int or warehouse <= 0 or (selected and warehouse not in selected):
                    raise MabangApiError(f"{context}仓库身份无法核实")
                if path == WAREHOUSE_CHILD_PATH and not _field_value(row, "SKU"):
                    raise MabangApiError(f"{context}返回仓库汇总而非商品明细，不能导出")
            if path == WAREHOUSE_REPORT_PATH:
                identity = (row.get("stockWarehouseId"),)
            else:
                if type(row.get("stockid")) is not int or row["stockid"] < 0 or not _field_value(row, "SKU"):
                    raise MabangApiError(f"{context}商品身份无法核实")
                identity = (row["stockid"], _field_value(row, "SKU"))
            if identity in identities:
                raise MabangApiError(f"{context}存在重复商品身份或重复分页")
            identities.add(identity)
        page_fingerprint = hashlib.sha256("".join(sorted(fingerprints)).encode()).hexdigest()
        if rows and page_fingerprint in page_fingerprints:
            raise MabangApiError(f"{context}返回重复分页")
        page_fingerprints.add(page_fingerprint)
        collected.extend(rows)
        LOGGER.info("%s读取完成: page=%s/%s rows=%s/%s", context, page, max(1, math.ceil(total / PAGE_SIZE)), len(collected), total)
        if len(collected) == total:
            return collected, total, snapshot or ()
        page += 1


# --------------------------------------------------------------------------- parsing helpers


def _measure(row: dict[str, Any], header: str, context: str) -> Decimal:
    if not any(key in row for key in FIELD_CANDIDATES[header]):
        raise MabangApiError(f"{context} 缺少新版字段映射: {header}")
    value = _field_value(row, header)
    if value is None and header not in NON_NULL_MEASURES:
        return Decimal(0)
    return _decimal_value(value, context=f"{context} {header}")


def _extra_measure(row, key, context):
    if key not in row:
        raise MabangApiError(f"{context} 缺少新版字段映射: {key}")
    return _decimal_value(0 if row[key] is None else row[key], context=f"{context} {key}")


def _verify_warehouse_amounts(parent: dict[str, Any] | None, rows: list[dict[str, Any]], context: str) -> None:
    # The native warehouse footer counts distinct orders, while SKU rows count
    # orders containing each SKU. Only additive financial/quantity measures
    # may be reconciled against that footer.
    for header in WAREHOUSE_CHECK_HEADERS:
        expected = _measure(parent, header, context) if parent is not None else Decimal(0)
        actual = sum((_measure(row, header, context) for row in rows), Decimal(0))
        # Each detail is rounded by the source to four decimal places.
        tolerance = Decimal(0) if header == "销售数量" else Decimal("0.00005") * (len(rows) + 1)
        if abs(actual - expected) > tolerance:
            raise MabangApiError(f"{context}仓库汇总与商品明细不一致: {header}")


def _fetch_report_rows(session: ModernSalesReportSession, filters: dict[str, Any], *, context: str):
    if filters["reportType"] != "warehouseReports":
        for attempt in range(2):
            try:
                return _fetch_pages(session, _report_path_for_filters(filters), filters, context=context)
            except ModernSalesSnapshotChanged:
                if attempt:
                    raise
                LOGGER.warning("%s源数据在分页期间更新，丢弃本次明细并从第一页重读一次", context)
                time.sleep(1)
    return _fetch_warehouse_rows(session, filters, context=context)


def _parent_signature(rows, context):
    return tuple(sorted((row["stockWarehouseId"], *(_measure(row, header, context) for header in WAREHOUSE_CHECK_HEADERS))
                        for row in rows))


def _warehouse_parent_ranges(session, filters, context):
    """Independent full-scope totals, partitioned by date rather than child SKUs.

    The unfiltered monthly parent query exceeds the source's 60s statement
    limit. Disjoint closed seven-day windows retain the entire requested SKU
    universe and also detect a child endpoint returning a false empty total.
    """
    from datetime import datetime, timedelta

    try:
        cursor = datetime.strptime(filters["timeStart"], "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(filters["timeEnd"], "%Y-%m-%d %H:%M:%S")
    except (KeyError, ValueError):
        raise MabangApiError(f"{context}付款日期无效") from None
    if cursor > end:
        raise MabangApiError(f"{context}付款日期范围无效")
    merged, checks, snapshot = {}, [], None
    while cursor <= end:
        boundary = min(end, (cursor + timedelta(days=6)).replace(hour=23, minute=59, second=59))
        query = dict(filters, timeStart=cursor.strftime("%Y-%m-%d %H:%M:%S"), timeEnd=boundary.strftime("%Y-%m-%d %H:%M:%S"))
        rows, _, current = _fetch_pages(session, WAREHOUSE_REPORT_PATH, query, context=context + "全范围汇总")
        if snapshot is not None and snapshot != current:
            raise MabangApiError(f"{context}分段汇总期间统计快照发生变化")
        snapshot = current
        checks.append((query, _parent_signature(rows, context)))
        for source in rows:
            identity = source["stockWarehouseId"]
            row = merged.setdefault(identity, {"stockWarehouseId": identity})
            for header in WAREHOUSE_CHECK_HEADERS:
                key = FIELD_CANDIDATES[header][0]
                row[key] = row.get(key, Decimal(0)) + _measure(source, header, context)
        cursor = boundary + timedelta(seconds=1)
    return list(merged.values()), checks, snapshot


def _fetch_warehouse_rows(session: ModernSalesReportSession, filters: dict[str, Any], *, context: str):
    # warehouseReports and warehouseV2Reports ignore stockWarehouseId on the
    # deployed SPA. Only getProductSaleChild(stockIds=...) proves warehouse scope.
    visible = set(_positive_int_list((option.id for option in session.filter_options().warehouses), "仓库目录 ID"))
    selected = list(dict.fromkeys(filters.get("stockWarehouseId", [])))
    if any(identity not in visible for identity in selected):
        raise MabangApiError(f"{context}仓库不存在或当前账号不可见")
    parent_filters = dict(filters, type="warehousev2", stockWarehouseId=[])
    if not selected or filters.get("shopLabelIds"):
        initial_parents, _, snapshot = _fetch_pages(session, WAREHOUSE_REPORT_PATH, parent_filters, context=context + "仓库目录")
        parent_checks = [(parent_filters, _parent_signature(initial_parents, context))]
        if not selected:
            selected = [row["stockWarehouseId"] for row in initial_parents]
        if any(identity not in visible for identity in selected):
            raise MabangApiError(f"{context}返回当前目录外仓库")
    else:
        initial_parents, parent_checks, snapshot = _warehouse_parent_ranges(session, parent_filters, context)

    collected = []
    for warehouse_id in selected:
        child_filters = dict(filters, type="warehousev2", types="warehousev2", childType="warehousev2",
                             stockIds=str(warehouse_id), stockWarehouseId=[warehouse_id])
        children, _, _ = _fetch_pages(session, WAREHOUSE_CHILD_PATH, child_filters,
                                     context=context + "仓库明细", snapshot_anchor=snapshot, allow_undated_child=True)
        parent = next((row for row in initial_parents if row["stockWarehouseId"] == warehouse_id), None)
        _verify_warehouse_amounts(parent, children, context)
        collected.extend(children)
    for query, signature in parent_checks:
        after, _, after_snapshot = _fetch_pages(session, WAREHOUSE_REPORT_PATH, query, context=context + "仓库最终复核")
        if snapshot != after_snapshot or _parent_signature(after, context) != signature:
            raise MabangApiError(f"{context}仓库范围或统计快照发生变化")

    merged: dict[tuple[int, str], dict[str, Any]] = {}
    for source in collected:
        identity = (source["stockid"], source["stockSku"])
        row = merged.get(identity)
        if row is None:
            row = dict(source)
            # Store each additive measure in one canonical key to avoid stale
            # aliases after merging. Attributes/stock/ratios are never summed.
            for header in ADDITIVE_HEADERS:
                value = _measure(source, header, context)
                for key in FIELD_CANDIDATES[header]:
                    row.pop(key, None)
                row[FIELD_CANDIDATES[header][0]] = value
            for key in ("deductibleShipping", "refundFeeRMB"):
                row[key] = _extra_measure(source, key, context)
            merged[identity] = row
        else:
            for header in ADDITIVE_HEADERS:
                row[FIELD_CANDIDATES[header][0]] += _measure(source, header, context)
            for key in ("deductibleShipping", "refundFeeRMB"):
                row[key] += _extra_measure(source, key, context)
    rows = list(merged.values())
    for row in rows:
        denominator = row["incomeTotal"] - row["deductibleShipping"]
        row["grossRate"] = row["gross"] / denominator if denominator else Decimal(0)
        row["returnRate"] = row["refundFeeRMB"] / row["incomeTotal"] if row["incomeTotal"] else Decimal(0)
    _warehouse_product_attributes(session, rows, filters, context)
    rows.sort(key=lambda row: (-row["quantity"], row["stockSku"], row["stockid"]))
    return rows, len(rows), snapshot or ()


def _warehouse_product_attributes(session, rows, filters, context):
    """Country product attributes only; warehouse metrics stay untouched."""
    needed = list(dict.fromkeys(row["stockSku"] for row in rows))
    metadata = {}
    snapshot = None
    for offset in range(0, len(needed), 500):
        batch = needed[offset:offset + 500]
        query = dict(filters, reportType="countryReports", type="countryCode", stockWarehouseId=[],
                     searchType="stockSku", operate="eq", searchValue="", searchValues=batch)
        products, _, current = _fetch_pages(session, COUNTRY_REPORT_PATH, query, context=context + "商品资料")
        if snapshot is not None and snapshot != current:
            raise MabangApiError(f"{context}商品资料读取期间统计快照发生变化")
        snapshot = current
        for product in products:
            if product["stockSku"] not in batch:
                raise MabangApiError(f"{context}商品资料批量筛选未生效")
            metadata[(product["stockid"], product["stockSku"])] = product
    for row in rows:
        product = metadata.get((row["stockid"], row["stockSku"]))
        if product is None:
            raise MabangApiError(f"{context}商品资料不完整，不能生成报表")
        for header in TEXT_HEADERS | {"统一成本价", "重量(单位:g)", "库存"}:
            if not any(key in product for key in FIELD_CANDIDATES[header]):
                raise MabangApiError(f"{context}商品资料缺少字段: {header}")
            value = _field_value(product, header)
            for key in FIELD_CANDIDATES[header]:
                row.pop(key, None)
            row[FIELD_CANDIDATES[header][0]] = value


def _parse_categories(rows: Any) -> tuple[ModernOption, ...]:
    result: list[ModernOption] = []
    seen: set[str] = set()

    def visit(items: Any) -> None:
        if items is None:
            return
        if not isinstance(items, list):
            raise MabangApiError("新版销量分类目录结构无效")
        for row in items:
            if not isinstance(row, dict):
                raise MabangApiError("新版销量分类目录选项无效")
            identity = _option_id(row, allow_zero=False)
            name = _option_name(row)
            if identity and name and identity not in seen:
                seen.add(identity)
                result.append(ModernOption(identity, name))
            visit(row.get("children"))

    visit(rows)
    return tuple(result)


def _parse_options_from_candidates(data: dict[str, Any], keys: Sequence[str], *, allow_zero: bool = False) -> tuple[ModernOption, ...]:
    for key in keys:
        options = _parse_flat_options(data.get(key), allow_zero=allow_zero)
        if options:
            return options
    return ()


def _parse_flat_options(rows: Any, *, allow_zero: bool = False) -> tuple[ModernOption, ...]:
    if rows is None:
        return ()
    if not isinstance(rows, list):
        raise MabangApiError("新版销量目录结构无效")
    result: list[ModernOption] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise MabangApiError("新版销量目录选项无效")
        identity = _option_id(row, allow_zero=allow_zero)
        name = _option_name(row)
        if not identity or (not name and not (allow_zero and identity == "0")) or identity in seen:
            continue
        seen.add(identity)
        result.append(ModernOption(identity, name))
    return tuple(result)


def _option_id(row: dict[str, Any], *, allow_zero: bool) -> str:
    for key in ("id", "value", "stockWarehouseId", "warehouseId", "developId", "developerId", "saleId", "salespersonId", "userId"):
        text = _safe_text(row.get(key))
        if text and (allow_zero or text != "0"):
            return text
        if allow_zero and text == "0":
            return text
    return ""


def _option_name(row: dict[str, Any]) -> str:
    for key in ("name", "label", "warehouseName", "stockWarehouseName", "developName", "developerName", "saleName", "salespersonName", "userName"):
        text = _safe_text(row.get(key))
        if text:
            return text
    return ""


def _field_value(row: dict[str, Any], header: str) -> Any:
    values = [row[key] for key in FIELD_CANDIDATES.get(header, (header,)) if row.get(key) is not None]
    distinct = {_safe_text(value) for value in values}
    if len(values) > 1 and header not in TEXT_HEADERS:
        distinct = {_decimal_value(value, context=f"新版销量 {header}") for value in values}
    if len(distinct) > 1:
        raise MabangApiError(f"新版销量字段存在歧义: {header}")
    return values[0] if values else None


def _decimal_value(value: Any, *, context: str) -> Decimal:
    text = _safe_text(value).rstrip("\t").replace(",", "")
    if text == "":
        raise MabangApiError(f"{context}不是有效数字")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise MabangApiError(f"{context}不是有效数字") from None
    if not amount.is_finite() or abs(amount) > Decimal("1e20"):
        raise MabangApiError(f"{context}不是有效数字")
    return amount


def _expected_days(filters: dict[str, Any]) -> int:
    from datetime import datetime

    try:
        start = datetime.strptime(str(filters["timeStart"]), "%Y-%m-%d %H:%M:%S").date()
        end = datetime.strptime(str(filters["timeEnd"]), "%Y-%m-%d %H:%M:%S").date()
    except (KeyError, ValueError) as exc:
        raise MabangApiError("新版销量查询日期格式无效") from exc
    return (end - start).days + 1


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format_decimal(value)
    return str(value)


def format_decimal(value: Decimal, *, places: int = 4) -> str:
    quantum = Decimal("1") if places <= 0 else Decimal("1").scaleb(-places)
    quantized = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if places <= 0:
        return f"{quantized:.0f}"
    return f"{quantized:.{places}f}"


def _normalize_status(value: Any) -> str:
    text = _safe_text(value)
    if text in {"-1", "1", "2", "3", "4", "5"}:
        return {"-1": "已删除", "1": "自动创建", "2": "待开发", "3": "正常", "4": "清仓", "5": "停止销售"}[text]
    return "正常" if text == "正常销售" else text


def _int_choice(value: str, name: str, allowed: set[int]) -> int:
    text = _safe_text(value)
    if not text or not text.lstrip("-").isdigit():
        raise MabangApiError(f"新版销量查询{name}无效")
    parsed = int(text)
    if parsed not in allowed:
        raise MabangApiError(f"新版销量查询{name}未验证")
    return parsed


def _required_query_param(query: dict[str, list[str]], name: str, *, default: str = "") -> str:
    entries = [entry for entry in query.get(name, []) if entry] or ([default] if default else [])
    if len(set(entries)) != 1 or not entries[0] or any(ord(char) < 32 or ord(char) > 126 for char in entries[0]):
        raise MabangApiError("新版销量入口认证参数无效")
    return entries[0]


def _raise_if_unauthenticated_page(response: httpx.Response, *, context: str) -> None:
    location = response.headers.get("Location", "")
    if response.status_code in {301, 302, 303, 307, 308} and ("login" in location.casefold() or "index.htm" in location.casefold()):
        raise MabangApiError(f"{context}: 马帮会话已失效")
    text = response.text[:2000].casefold()
    if "name=\"username\"" in text or "doLogin".casefold() in text:
        raise MabangApiError(f"{context}: 马帮会话已失效")


def _safe_text(value: Any) -> str:
    return "" if value is None else str(value).strip()
