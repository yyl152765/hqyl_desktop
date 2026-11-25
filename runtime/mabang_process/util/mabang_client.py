from __future__ import annotations

import html
import csv
import hashlib
import logging
import math
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import unquote, urlencode, urljoin, urlparse
from html.parser import HTMLParser
from zipfile import is_zipfile

import httpx
from bs4 import BeautifulSoup
from openpyxl import Workbook, load_workbook

from util.mabang_network import current_erp_proxy_url

LOGGER = logging.getLogger(__name__)
INVENTORY_EXPORT_PAGE_SIZE = 10000

# ===== 默认配置与常量 =====
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_SECONDS = 60 # 默认超时时间
DEFAULT_CONNECT_TIMEOUT_SECONDS = 20.0 # 默认连接超时时间
DEFAULT_WRITE_TIMEOUT_SECONDS = 20.0 # 默认写入超时秒数
DEFAULT_POOL_TIMEOUT_SECONDS = 20.0
DEFAULT_RETRY_BACKOFF_CAP_SECONDS = 10.0  # 请求失败重试时的最大退避等待时间
DEFAULT_MAX_WORKERS = 4  # 并发查询时的默认线程数
DEFAULT_ORDER_BATCH_SIZE = 2000  # 批量查询订单时的每次最大单量
DEFAULT_ORDER_PAGE_SIZE = 100  # 默认订单每页查询数量
MAX_STOCK_SKU_SEARCH_BATCH_SIZE = 100  # 库存列表接口实际最多返回100行，避免大批次被静默截断
DEFAULT_WAREHOUSE_LIST_TIMEOUT_SECONDS = 30.0  # 仓库列表接口偶发卡住，单独限制等待时间
DEFAULT_STOCK_SYNC_READ_RETRIES = 4  # 库存同步查询接口偶发读超时，读取类请求允许安全重试
SUPPORTED_ORDER_PAGE_SIZES = (100, 200, 300, 500)
DEFAULT_INVENTORY_SKU_BATCH_SIZE = 500  # 库存查询时SKU的默认分批大小
SALES_REPORT_OUTPUT_HEADERS = ("SKU", "商品中文名", "统一成本价", "重量(单位:g)", "创建时间", "销售数量")
DEFAULT_SALES_REPORT_EXPORT_INIT_ROWS_PER_PAGE = 20
DEFAULT_SALES_REPORT_EXPORT_FIELDS = (
    "salesSkuNewId",
    "status",
    "defaultCost",
    "weight",
    "stockQuantity",
    "sale",
    "develop",
    "orderNum",
    "quantity",
    "quantityAvg",
    "income",
    "expenditure",
    "refundNum",
    "itemRefundNum",
    "refundQuantity",
    "refundMoney",
    "returnRate",
    "gross",
    "grossRate",
    "timeCreated",
)
SALES_REPORT_PAGE_PATHS = {
    "countryReports": "/index.php?mod=reports.countryReports",
    "warehouseReports": "/index.php?mod=reports.warehouseReports",
    "stockShopReports": "/index.php?mod=reports.stockShopReports",
}
SALES_REPORT_PLATFORM_OR_SHOP_DEFAULTS = {
    "countryReports": "country",
    "warehouseReports": "warehouse",
    "stockShopReports": "stockShop",
}

# ===== 接口路径 =====
LOGIN_PAGE_PATH = "/index.htm"  # 登录页
LOGIN_PATH = "/index.php?mod=main.doLogin&lang=cn"  # 登录接口
ORDER_PAGE_PATH = "/index.php?mod=order.list&Order_orderStatus=2"  # 订单页
ORDER_QUERY_PATH = "/index.php?mod=order.oTc"  # 订单查询接口
ORDER_INFO_SYNC_PATH = "/index.php?mod=order.orderInfoSynchronize"  # 订单信息同步接口
ORDER_EXPORT_PAGE_PATH = "/index.php?mod=order.exportOrderByTemplate&isCloud=2&tableBase=&os=&orderItemOrderBy=id+asc%2CstockId+asc"  # 订单导出页
ORDER_EXPORT_ACTION_PATH = "/index.php?mod=order.doExportByTemplateData"  # 订单导出动作接口
RETURNS_PAGE_PATH = "/index.php?mod=order.refundsOrderList"  # 退货页
REFUNDS_PAGE_PATH = "/index.php?mod=paypalconfig.paypalorderlist"  # 退款页

# ===== 马帮其它接口 =====
RETURNS_QUERY_URL = "/index.php?mod=order.getRefundsOrderList" # 退货记录查询
REFUNDS_QUERY_URL = "/index.php?mod=paypal.dosearchpaypalrefund" # 退款记录查询
INVENTORY_DETAIL_PATH = "/index.php?mod=warehouse.inventorydetail" # 实时库存详情页
INVENTORY_SEARCH_URL = "/index.php?mod=warehouse.searchwarehousestock" # 实时库存查询
INVENTORY_TOTAL_URL = "/index.php?mod=warehouse.getStockTotalAndStockTotalCost" # 库存总数查询
INVENTORY_EXPORT_URL = "/index.php?mod=warehouse.doexportwarehousestock" # 实时库存导出地址
STOCK_LIST_PAGE_PATH = "/index.php?mod=stock.list&searchStatus=3" # 库存列表页面路径
STOCK_LIST_API_URL = "/index.php?mod=stock.getStockList" # 库存列表接口地址
STOCK_BATCH_LABEL_PATH = "/index.php?mod=stock.doBatchStockType" # 库存SKU批量添加自定义分类
STOCK_EXPORT_ACTION_PATH = "/index.php?mod=export.doStockExportFile" # 库存导出执行(下载文件)
EXPORT_ENTRY_PATH = "/index.php"
WAREHOUSE_LIST_PAGE_PATH = "/index.php?mod=warehouse.listNew&platform=other&version=1" # 所有仓库列表页面
WAREHOUSE_LIST_API_URL = "/index.php?mod=warehouse.dosearchwarehouse" #仓库列表请求地址
WAREHOUSE_TRANSFER_SHIPMENT_PAGE_PATH = (
    "/index.php?mod=allocationwarehouse.shipments&platform=other&version=1"
)
WAREHOUSE_TRANSFER_SEARCH_PATH = "/index.php?mod=warehouseallocation.searchallocation"
STOCK_SYNC_PAGE_PATH = "/index.php?mod=stock.stockSyncNew" # 库存同步地址
MATCHING_PRODUCT_PAGE_PATH = "/index.php?mod=stock.matchingProductNew" # 在线商品匹配页
MATCHING_PRODUCT_SHOP_LIST_PATH = "/index.php?mod=stock.showshopdown" # 拉取在线商品店铺列表
MATCHING_PRODUCT_CNSC_SHOP_LIST_PATH = "/index.php?mod=stock.showshopdownCnsc" # ShopeeCNSC拉取店铺列表
MATCHING_PRODUCT_PULL_PATH = "/index.php?mod=stock.downMatchByRule" # “立即下载”同步在线商品
YUNBI_SSO_TOKEN_URL = "https://api-private.mabangerp.com/sso/api/v1/getTokenByKey" # 获取YUNBI token地址
YUNBI_API_BASE_URL = "https://api-private.mabangerp.com/v2"  # YUNBI 同步规则地址

NO_DATA_MARKER = "group-nodata"
DEFAULT_ORDER_EXPORT_CHUNK_SIZE = 5000  # 单次订单导出的最大上限
DEFAULT_ORDER_EXPORT_POLL_INTERVAL_SECONDS = 0.5  # 异步导出任务状态轮询间隔
DEFAULT_ORDER_EXPORT_POLL_TIMEOUT_SECONDS = 300.0  # 异步导出任务最长等待时间
DEFAULT_ORDER_EXPORT_STEP_RETRIES = 6  # 导出过程中每个分步任务的重试次数


# ===== 自定义异常 =====
class MabangApiError(RuntimeError):
    """马帮接口异常（统一封装）"""


class MabangWarehouseQueryError(MabangApiError):
    """Safe warehouse-query diagnostics, without URLs, headers or response bodies."""

    def __init__(self, *, status_code: int | None = None, transport_error_type: str = ""):
        self.status_code = status_code
        detail = f"HTTP={status_code}" if status_code is not None else f"error_type={transport_error_type}"
        super().__init__(f"仓库目录查询失败：接口=warehouse.dosearchwarehouse；{detail}")


class MabangModernWarehouseQueryError(MabangWarehouseQueryError):
    """Only fixed diagnostic codes may cross the warehouse SSO boundary."""

    def __init__(self, reason: str, *, status_code: int | None = None):
        self.status_code = status_code
        detail = f"；HTTP={status_code}" if status_code is not None else ""
        MabangApiError.__init__(self, f"仓库目录查询失败：接口=mas.warehouse.list；原因={reason}{detail}")


class MabangSessionExpiredError(MabangApiError):
    """马帮登录会话已失效，需要重新认证。"""


class _StockListTotalChangedError(MabangApiError):
    """库存标签在分页期间发生变更，必须丢弃本轮结果后重新读取。"""


# ===== 快捷函数：获取cookie =====
def get_mabang_cookie_header(username: str, password: str,
                             base_url: str = "https://900853.private.mabangerp.com") -> str:
    """
    登录并返回 cookie header
    用于后续请求复用

    :param username: 账号
    :param password: 密码
    :param base_url: ERP地址
    """
    with MabangClient(base_url) as client:
        client.login(username, password)
        return client.cookie_header()


# ===== 数据结构：订单信息 =====
@dataclass(slots=True)
class MabangOrderInfo:
    tracking_number: str  # 物流单号
    platform_order_id: str  # 平台订单号
    shop_name: str = ""  # 店铺名
    platform_name: str = ""  # 平台名
    show_order_status_text: str = ""  # 状态文本
    platform_order_status: str = ""  # 平台状态
    order_status: str = ""  # 系统状态
    refund_flag: str = ""  # 是否退款
    created_at: str = ""  # 创建时间
    express_time: str = ""  # 发货时间
    origin_transport_time: str = ""  # 运输时间
    order_deliver_time: str = ""  # 签收时间
    raw_data: dict[str, Any] = field(default_factory=dict)  # 原始数据


# ===== 数据结构：退货记录 =====
@dataclass(slots=True)
class MabangReturnRecord:
    tracking_number: str  # 关联的物流单号
    platform_order_id: str  # 平台订单号
    return_order_no: str = ""  # ERP内部退货单号
    platform_return_order_no: str = ""  # 平台侧退货单号
    platform_order_status: str = ""  # 平台订单状态
    return_status: str = ""  # ERP内退货处理状态
    register_time: str = ""  # 退货登记时间
    raw_summary_text: str = ""  # 原始摘要文本


# ===== 数据结构：退款记录 =====
@dataclass(slots=True)
class MabangRefundRecord:
    platform_order_id: str  # 平台订单号
    refund_id: str = ""  # ERP退款ID
    refund_order_no: str = ""  # 退款流水号
    refund_status: str = ""  # 退款状态
    refund_reason: str = ""  # 退款原因
    order_status_at_refund: str = ""  # 发起退款时的订单状态
    refund_date: str = ""  # 退款日期
    raw_summary_text: str = ""  # 原始摘要文本


# ===== 数据结构：综合查询结果 =====
@dataclass(slots=True)
class MabangLookupResult:
    tracking_number: str  # 物流单号
    order: MabangOrderInfo | None = None  # 订单信息
    return_records: list[MabangReturnRecord] = field(default_factory=list)  # 退货记录列表
    refund_records: list[MabangRefundRecord] = field(default_factory=list)  # 退款记录列表
    error_message: str = ""  # 查询错误信息


# ===== 数据结构：库存盘点汇总 =====
@dataclass(slots=True)
class MabangInventorySummary:
    warehouse_id: str  # 仓库ID
    sku_count: int = 0  # SKU数量
    total_quantity: float = 0.0  # 总数量
    total_cost: float = 0.0  # 总成本
    total_cost_currency: float = 0.0  # 本币总成本
    in_transit_total: float = 0.0  # 在途总数
    in_transit_total_currency: float = 0.0  # 在途本币总数
    cache_update_time: str = ""  # 缓存更新时间


@dataclass(slots=True)
class MabangWarehouseTransferShipment:
    allocation_id: str
    batch_code: str
    warehouse_id: str
    remark: str = ""
    stock_skus: list[str] = field(default_factory=list)


# ===== 数据结构：订单店铺 =====
@dataclass(slots=True)
class MabangOrderShop:
    shop_id: str  # 店铺ID
    shop_name: str = ""  # 店铺名
    platform_id: str = ""  # 所属平台ID
    platform_name: str = ""  # 所属平台名
    raw_data: dict[str, Any] = field(default_factory=dict)  # 原始数据


# ===== 数据结构：订单平台 =====
@dataclass(slots=True)
class MabangOrderPlatform:
    platform_id: str  # 平台ID
    platform_name: str = ""  # 平台名


# ===== 数据结构：在线商品拉取店铺 =====
@dataclass(slots=True)
class MabangOnlineProductShop:
    shop_id: str  # 店铺ID
    shop_name: str = ""  # 店铺名称
    platform_id: str = ""  # 平台ID
    status: str = ""  # 店铺状态
    token_flag: str = ""  # 授权状态
    is_running: str = ""  # 拉取状态
    last_run_time: str = ""  # 最近拉取时间
    last_run_timezone: str = ""  # 最近拉取时间（带时区）
    error: str = ""  # 页面展示的状态/错误
    available: bool = True  # 是否可提交拉取
    raw_data: dict[str, Any] = field(default_factory=dict)  # 原始店铺数据


# ===== 数据结构：导出文件 =====
@dataclass(slots=True)
class MabangExportFile:
    file_name: str  # 文件名
    file_type: str  # 文件类型后缀
    file_url: str  # 远端下载链接
    content: bytes  # 文件二进制内容
    metadata: dict[str, Any] = field(default_factory=dict)  # 导出元数据


# ===== 主客户端类 =====
class MabangClient:
    """马帮ERP客户端（核心类）"""

    def __init__(
            self,
            base_url: str,
            *,
            client: httpx.Client | None = None,
            timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
            verify_ssl: bool = True,
    ) -> None:
        """
        初始化客户端

        :param base_url: ERP地址
        :param client: 可传入自定义httpx客户端
        :param timeout_seconds: 默认超时时间（秒）
        :param verify_ssl: 是否验证SSL证书
        """
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.verify_ssl = verify_ssl
        self._owns_client = client is None
        self.client = client if client is not None else httpx.Client(
            timeout=_build_http_timeout(timeout_seconds),
            follow_redirects=True,
            verify=verify_ssl,
            trust_env=False,
            proxy=current_erp_proxy_url(),
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        self._logged_in = False
        self._login_username = ""
        self._login_password = ""
        self._auth_lock = threading.Lock()
        self._auth_epoch = 0
        self._order_page_key: str | None = None
        self._order_tab_id: str | None = None
        self._returns_bootstrapped = False
        self._refunds_bootstrapped = False
        self._inventory_detail_bootstrapped = False
        self._inventory_iframe_url: str | None = None
        self._warehouse_transfer_iframe_url: str | None = None
        self._matching_product_iframe_url: str | None = None
        self._order_export_page_url: str | None = None
        self._order_export_form_defaults: dict[str, str] = {}
        self._order_export_field_map: dict[str, str] = {}

    def close(self) -> None:
        """关闭 HTTP 连接池"""
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "MabangClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def login(self, username: str, password: str) -> None:
        """
        执行登录操作并获取会话状态

        :param username: 用户名
        :param password: 密码
        """
        with self._auth_lock:
            try:
                self._perform_login_locked(username, password)
            except Exception:
                self._logged_in = False
                self._invalidate_context_cache()
                self._auth_epoch += 1
                raise

    def _perform_login_locked(self, username: str, password: str) -> None:
        """在认证锁内执行原始登录请求并更新本地会话状态。"""
        login_page = self.client.get(self._url(LOGIN_PAGE_PATH))
        login_page.raise_for_status()
        response = self.client.post(
            self._url(LOGIN_PATH),
            data={"username": username, "password": password, "loginEntrance": "1", "timezone": "UTC+8"},
            headers={"Referer": str(login_page.url), "X-Requested-With": "XMLHttpRequest"},
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise MabangApiError(
                "登录失败: 返回非JSON响应 "
                f"status={response.status_code}, final_url={_safe_url_for_log(response.url)}"
            ) from exc
        if not isinstance(payload, dict):
            raise MabangApiError(f"登录失败: 返回JSON结构无效 type={type(payload).__name__}")
        if not payload.get("success"):
            raise MabangApiError("登录失败: 服务端拒绝认证")
        self._login_username = username
        self._login_password = password
        self._invalidate_context_cache()
        self._logged_in = True
        self._auth_epoch += 1

    def cookie_header(self) -> str:
        """
        将当前会话的 Cookie 格式化为 Header 字符串
        """
        return "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.client.cookies.jar)

    def search_order_by_tracking_number(self, tracking_number: str) -> MabangOrderInfo | None:
        """
        根据物流单号查询单个订单信息

        :param tracking_number: 物流单号
        """
        self._ensure_order_context()
        payload = self.request_order_page(
            {
                **self._build_order_payload_base(),
                "OrderSearchFuSKey": "a.trackNumber",
                "OrderSearch.fuzzySearchKey": "Order.trackNumber",
                "OrderSearch.fuzzySearchValue": tracking_number,
            },
            retries=3,
        )
        rows = payload.get("orderDataList") or []
        if not rows:
            return None
        normalized_tracking = tracking_number.strip()
        selected = next((row for row in rows if _safe_text(row.get("trackNumber")) == normalized_tracking), rows[0])
        return self._build_order_info(selected, fallback_tracking_number=normalized_tracking)

    def search_orders_by_tracking_numbers(self, tracking_numbers: Sequence[str], *,
                                          batch_size: int = DEFAULT_ORDER_BATCH_SIZE) -> dict[str, MabangOrderInfo]:
        """
        批量查询订单列表

        :param tracking_numbers: 物流单号列表
        :param batch_size: 每次请求的批量大小
        """
        self._ensure_order_context()
        deduplicated = list(dict.fromkeys(_safe_text(value) for value in tracking_numbers if _safe_text(value)))
        if not deduplicated:
            return {}
        batch_size = max(1, min(batch_size, DEFAULT_ORDER_BATCH_SIZE))
        total_batches = (len(deduplicated) + batch_size - 1) // batch_size
        results: dict[str, MabangOrderInfo] = {}
        missing: list[str] = []
        for batch_index in range(total_batches):
            chunk = deduplicated[batch_index * batch_size: (batch_index + 1) * batch_size]
            chunk_results, chunk_missing = self._search_orders_by_tracking_chunk(chunk)
            LOGGER.info("马帮运单号批量处理 %s/%s，匹配到 %s 个订单", batch_index + 1, total_batches, len(chunk_results))
            results.update({k: v for k, v in chunk_results.items() if k not in results})
            missing.extend(chunk_missing)
        if missing:
            LOGGER.info("马帮轨迹批量获取漏掉 %s 个数据，正在尝试逐个查询", len(missing))
            for tracking_number in missing:
                if tracking_number in results:
                    continue
                order = self.search_order_by_tracking_number(tracking_number)
                if order is not None:
                    results[tracking_number] = order
        return results

    def list_order_shops(self, *, platform_ids: Sequence[str] | None = None) -> list[MabangOrderShop]:
        """
        获取订单页可用的店铺列表

        :param platform_ids: 可选的平台ID列表过滤条件
        """
        page_html = self._get_order_page_html()
        self._parse_order_context(page_html)
        platform_id_set = {_safe_text(value) for value in (platform_ids or []) if _safe_text(value)}
        shops: list[MabangOrderShop] = []
        seen_shop_ids: set[str] = set()
        for shop in _parse_order_shops(page_html):
            shop_id = _safe_text(shop.shop_id)
            platform_id = _safe_text(shop.platform_id)
            if not shop_id or shop_id in seen_shop_ids:
                continue
            if platform_id_set and platform_id not in platform_id_set:
                continue
            shops.append(shop)
            seen_shop_ids.add(shop_id)
        return shops

    def list_order_platforms(self) -> list[MabangOrderPlatform]:
        """
        获取可用的订单平台列表
        """
        page_html = self._get_order_page_html()
        self._parse_order_context(page_html)
        platforms: list[MabangOrderPlatform] = []
        seen_platform_ids: set[str] = set()
        for platform in _parse_order_platforms(page_html):
            platform_id = _safe_text(platform.platform_id)
            if not platform_id or platform_id in seen_platform_ids:
                continue
            platforms.append(platform)
            seen_platform_ids.add(platform_id)
        return platforms

    def list_order_export_fields(self) -> dict[str, str]:
        """
        获取订单导出模板支持的字段映射
        """
        self._ensure_order_export_context()
        return dict(self._order_export_field_map)

    def resolve_order_export_field_codes(self, fields: Sequence[str] | None) -> list[str]:
        """
        将订单导出字段解析为马帮 uq 字段码。

        fields 可以传 uq 字段码，也可以传导出页上的中文字段名。
        """
        self._ensure_order_export_context()
        return _resolve_export_field_codes(
            fields,
            self._order_export_field_map,
            fallback_codes=_default_order_export_field_codes(self._order_export_field_map),
            allow_fallback=fields is None,
        )

    def export_orders_by_template(
            self,
            *,
            platform_order_ids: Sequence[str],
            field_codes: Sequence[str] | None = None,
            include_shared_info: bool = True,
            request_retries: int = DEFAULT_ORDER_EXPORT_STEP_RETRIES,
            poll_interval_seconds: float = DEFAULT_ORDER_EXPORT_POLL_INTERVAL_SECONDS,
            poll_timeout_seconds: float = DEFAULT_ORDER_EXPORT_POLL_TIMEOUT_SECONDS,
    ) -> MabangExportFile:
        """
        通过模板异步导出订单数据并下载文件

        :param platform_order_ids: 平台订单号列表
        :param field_codes: 需要导出的字段编码或导出页中文字段名
        :param include_shared_info: 是否包含商品合并信息
        :param request_retries: 请求重试次数
        :param poll_interval_seconds: 异步轮询间隔
        :param poll_timeout_seconds: 异步轮询超时时间
        """
        normalized_order_ids = list(
            dict.fromkeys(_safe_text(value) for value in platform_order_ids if _safe_text(value)))
        if not normalized_order_ids:
            raise MabangApiError("订单导出失败：订单 ID 为空")
        if len(normalized_order_ids) > DEFAULT_ORDER_EXPORT_CHUNK_SIZE:
            raise MabangApiError(
                f"订单导出失败：实时导出最多支持 {DEFAULT_ORDER_EXPORT_CHUNK_SIZE} 个 ID，当前获取到 {len(normalized_order_ids)} 个"
            )

        self._ensure_order_export_context()
        selected_fields = self.resolve_order_export_field_codes(field_codes)
        if not selected_fields:
            raise MabangApiError("订单导出失败: 无导出选项")

        request_retries = max(1, int(request_retries))
        payload: dict[str, Any] = dict(self._order_export_form_defaults)
        payload["orderIds"] = "\n".join(normalized_order_ids)
        payload["fieldlabel"] = selected_fields
        payload["map-uq[]"] = selected_fields
        payload["map-name[]"] = [self._order_export_field_map.get(code, code) for code in selected_fields]
        payload["map-text[]"] = ["" for _ in selected_fields]
        payload["tableBase"] = ""
        payload["hbddgyxx"] = "1" if include_shared_info else "2"
        payload["step1"] = "1"

        step1_data = self._post_order_export_action(
            payload,
            retries=request_retries,
            allow_failed_payload=True,
            retry_on_failed_payload=True,
        )
        if not step1_data.get("success"):
            missing_ids = _extract_missing_order_ids(step1_data)
            if missing_ids:
                valid_order_ids = [value for value in normalized_order_ids if value not in set(missing_ids)]
                if valid_order_ids:
                    payload["orderIds"] = "\n".join(valid_order_ids)
                    step1_data = self._post_order_export_action(payload, retries=request_retries,
                                                                allow_failed_payload=False)
                    normalized_order_ids = valid_order_ids
            if not step1_data.get("success"):
                raise MabangApiError(f"订单导出 step1 失败: {step1_data}")

        export_data = self._resolve_order_export_data(
            step1_data,
            step_retries=request_retries,
            poll_interval_seconds=poll_interval_seconds,
            poll_timeout_seconds=poll_timeout_seconds,
        )

        file_url = _safe_text(export_data.get("file_url") or export_data.get("gourl"))
        if not file_url:
            raise MabangApiError(f"订单导出失败: 缺失文件地址: {export_data}")

        file_payload = self._download_export_file(file_url)
        metadata = {str(k): v for k, v in export_data.items()}
        metadata["fieldCodes"] = selected_fields
        metadata["orderCount"] = len(normalized_order_ids)
        metadata["missingOrderIds"] = _extract_missing_order_ids(step1_data)
        return MabangExportFile(
            file_name=file_payload[0],
            file_type=file_payload[1],
            file_url=file_url,
            content=file_payload[2],
            metadata=metadata,
        )

    def request_order_page(self, payload: dict[str, Any], *, retries: int = 3) -> dict[str, Any]:
        """
        请求订单页数据

        :param payload: 查询条件字典
        :param retries: 重试次数
        """
        return self.request_order_json(
            ORDER_QUERY_PATH,
            payload=payload,
            retries=retries,
            error_prefix="order query failed",
        )

    def request_order_json(
            self,
            path: str,
            *,
            payload: dict[str, Any],
            retries: int = 3,
            error_prefix: str = "order request failed",
    ) -> dict[str, Any]:
        """
        带有重试和防抖恢复机制的通用订单JSON请求

        :param path: 请求路径
        :param payload: 请求体
        :param retries: 重试次数
        :param error_prefix: 错误提示前缀
        """
        endpoint = self._url(path)
        retry_count = max(1, int(retries))
        last_error: Exception | None = None
        page_hint = _safe_text(payload.get("page")) or "-"
        rows_hint = _safe_text(payload.get("rowsPerPage")) or "-"
        for attempt in range(1, retry_count + 1):
            try:
                self._ensure_order_context()
                response = self.client.post(endpoint, data=payload, headers=self._order_headers())
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise MabangApiError(f"{error_prefix}: 无响应")
                if not data.get("success"):
                    raise MabangApiError(f"{error_prefix}: {data}")
                return data
            except Exception as exc:  # pragma: no cover - live request guard
                last_error = exc
                if attempt >= retry_count:
                    break
                sleep_seconds = _compute_retry_sleep_seconds(attempt, cap=DEFAULT_RETRY_BACKOFF_CAP_SECONDS)
                LOGGER.warning(
                    "马帮订单请求重试 %s/%s 失败 (path=%s, page=%s, rows=%s): %s",
                    attempt,
                    retry_count,
                    path,
                    page_hint,
                    rows_hint,
                    exc,
                )
                self._refresh_order_context_for_retry()
                time.sleep(sleep_seconds)
        detail = str(last_error) if last_error else "unknown error"
        raise MabangApiError(f"{error_prefix} 多次重试异常: {detail}") from last_error

    def sync_order_information_by_order_ids(
            self,
            platform_order_ids: Sequence[str],
            *,
            sync_storage: bool = False,
            request_retries: int = 3,
            auto_confirm_filtered_orders: bool = True,
    ) -> dict[str, Any]:
        """
        同步（拉取）最新订单信息到马帮

        :param platform_order_ids: 平台订单号列表
        :param sync_storage: 是否同步库存
        :param request_retries: 请求重试次数
        :param auto_confirm_filtered_orders: 遇到异常拦截时是否自动确认继续同步
        """
        normalized_order_ids = list(
            dict.fromkeys(_safe_text(value) for value in platform_order_ids if _safe_text(value)))
        if not normalized_order_ids:
            return {"success": True, "message": "no order ids to synchronize", "code": 0, "orderCount": 0}

        payload: dict[str, Any] = {
            "synchronization_type": "2",
            "synchronizationOrderData": "\n".join(normalized_order_ids),
            "endTime": "",
        }
        if sync_storage:
            payload["isSyncStorage"] = "1"

        first_response = self._post_order_info_sync(payload=payload, type_value="1", retries=request_retries)
        filtered_orders_text = ""
        if str(first_response.get("code")) == "111":
            filtered_orders_value = first_response.get("data")
            filtered_orders_text = "" if filtered_orders_value in (None, "") else str(filtered_orders_value).strip()
        final_response = first_response
        if auto_confirm_filtered_orders and str(first_response.get("code")) == "111":
            final_response = self._post_order_info_sync(payload=payload, type_value="2", retries=request_retries)
        if not isinstance(final_response, dict):
            raise MabangApiError(f"订单信息同步失败: 无效响应格式: {final_response}")
        if not final_response.get("success"):
            raise MabangApiError(f"订单信息同步失败: {final_response}")

        result = dict(final_response)
        result["requestedOrderIds"] = normalized_order_ids
        result["orderCount"] = len(normalized_order_ids)
        if filtered_orders_text:
            result["filteredOrdersText"] = filtered_orders_text
        if first_response is not final_response:
            result["precheckResponse"] = first_response
        return result

    def search_order_rows(
            self,
            *,
            rows_per_page: int = DEFAULT_ORDER_PAGE_SIZE,
            start_page: int = 1,
            extra_payload: dict[str, Any] | None = None,
            request_retries: int = 3,
            page_callback: Callable[[int, int, list[dict[str, Any]], dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """
        分页查询并收集所有订单行

        :param rows_per_page: 每页数量
        :param start_page: 起始页码
        :param extra_payload: 附加查询条件
        :param request_retries: 重试次数
        :param page_callback: 每查完一页的回调函数
        """
        self._ensure_order_context()
        normalized_rows_per_page = normalize_order_page_size(rows_per_page)
        self.client.cookies.set("order_rows_per_page_data_cookie", str(normalized_rows_per_page))
        rows: list[dict[str, Any]] = []
        page = max(1, int(start_page))
        while True:
            payload = dict(self._build_order_payload_base(rows_per_page=normalized_rows_per_page, page=page))
            if extra_payload:
                payload.update(extra_payload)
            data = self.request_order_page(payload, retries=request_retries)
            page_rows = data.get("orderDataList") or []
            actual_rows_per_page = normalize_order_page_size(data.get("rowsPerPage"), default=normalized_rows_per_page)
            if page_callback is not None:
                page_callback(page, actual_rows_per_page, list(page_rows), data)
            rows.extend(page_rows)
            if not page_rows or len(page_rows) < actual_rows_per_page:
                break
            page += 1
        return rows

    def search_orders_by_shop_ids(
            self,
            *,
            shop_ids: Sequence[str],
            platform_ids: Sequence[str] | None = None,
            query_time: str = "",
            start_time: str = "",
            end_time: str = "",
            rows_per_page: int = DEFAULT_ORDER_PAGE_SIZE,
            request_retries: int = 3,
            extra_payload: dict[str, Any] | None = None,
            page_callback: Callable[[int, int, list[dict[str, Any]], dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """
        按店铺等条件搜索订单

        :param shop_ids: 店铺ID列表
        :param platform_ids: 平台ID列表
        :param query_time: 时间过滤类型字段
        :param start_time: 起始时间
        :param end_time: 结束时间
        :param rows_per_page: 每页数量
        :param request_retries: 重试次数
        :param extra_payload: 其他条件
        :param page_callback: 翻页回调
        """
        payload = dict(extra_payload or {})
        normalized_shop_ids = [_safe_text(value) for value in shop_ids if _safe_text(value)]
        if normalized_shop_ids:
            payload["Order.shops[]"] = normalized_shop_ids
            payload["shopIdMultiple[]"] = normalized_shop_ids
        normalized_platform_ids = [_safe_text(value) for value in (platform_ids or []) if _safe_text(value)]
        if normalized_platform_ids:
            payload["platformId[]"] = normalized_platform_ids
        if query_time:
            payload["queryTime"] = _safe_text(query_time)
        if start_time:
            payload["startTime1"] = _safe_text(start_time)
        if end_time:
            payload["endTime1"] = _safe_text(end_time)
        return self.search_order_rows(
            rows_per_page=rows_per_page,
            extra_payload=payload,
            request_retries=request_retries,
            page_callback=page_callback,
        )

    def search_returns_by_tracking_number(self, tracking_number: str) -> list[MabangReturnRecord]:
        """
        根据物流单号查询退货记录

        :param tracking_number: 物流单号
        """
        self._ensure_returns_context()
        response = self.client.post(
            self._url(RETURNS_QUERY_URL),
            data={"searchCnt": "trackNumber", "search-content-text1": tracking_number, "page": "1", "rowsPerPage": "20",
                  "act": "refundsOrderList"},
            headers={"Referer": self._url(RETURNS_PAGE_PATH), "X-Requested-With": "XMLHttpRequest"},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise MabangApiError(f"退货列表查询失败: {payload}")
        return parse_return_records(payload.get("message") or "", default_tracking_number=tracking_number)

    def search_refunds_by_platform_order_id(self, platform_order_id: str) -> list[MabangRefundRecord]:
        """
        根据平台订单号查询退款记录

        :param platform_order_id: 平台侧订单号
        """
        self._ensure_refunds_context()
        response = self.client.post(
            self._url(REFUNDS_QUERY_URL),
            data={"search-content": "platformOrderId", "search-content-text": platform_order_id, "page": "1",
                  "rowsPerPage": "20"},
            headers={"Referer": self._url(REFUNDS_PAGE_PATH), "X-Requested-With": "XMLHttpRequest"},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise MabangApiError(f"退款列表查询失败: {payload}")
        return parse_refund_records(payload.get("message") or "")

    def lookup_tracking_numbers(self, *, tracking_numbers: Iterable[str], username: str, password: str,
                                max_workers: int = DEFAULT_MAX_WORKERS) -> dict[str, MabangLookupResult]:
        """
        多线程并发查询多个单号的详细信息(含订单、退货、退款)

        :param tracking_numbers: 单号集合
        :param username: 账号(供子线程登录)
        :param password: 密码
        :param max_workers: 最大并发线程数
        """
        ordered_unique = list(dict.fromkeys(_safe_text(value) for value in tracking_numbers if _safe_text(value)))
        if not ordered_unique:
            return {}
        worker_count = max(1, min(max_workers, len(ordered_unique)))
        chunks = _split_tracking_numbers(ordered_unique, worker_count)
        results: dict[str, MabangLookupResult] = {}
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {executor.submit(self._lookup_chunk, chunk, username, password, index + 1): chunk for
                          index, chunk in enumerate(chunks) if chunk}
            for future in as_completed(future_map):
                results.update(future.result())
        return results

    def get_inventory_total_by_skus(
            self,
            *,
            warehouse_id: str | int,
            skus: Sequence[str],
            sku_batch_size: int = DEFAULT_INVENTORY_SKU_BATCH_SIZE,
            status: str = "",
    ) -> MabangInventorySummary:
        """
        获取指定SKU在对应仓库的库存汇总数据

        :param warehouse_id: 仓库ID
        :param skus: SKU列表
        :param sku_batch_size: 查询时的分批数量
        :param status: 库存状态过滤
        """
        self._ensure_inventory_detail_context()
        warehouse_id_text = _safe_text(warehouse_id)
        normalized_skus = list(dict.fromkeys(_safe_text(value) for value in skus if _safe_text(value)))
        summary = MabangInventorySummary(warehouse_id=warehouse_id_text, sku_count=len(normalized_skus))
        if not warehouse_id_text or not normalized_skus:
            return summary

        sku_batch_size = max(1, sku_batch_size)
        for start_index in range(0, len(normalized_skus), sku_batch_size):
            chunk = normalized_skus[start_index: start_index + sku_batch_size]
            self._search_inventory_by_skus(warehouse_id=warehouse_id_text, skus=chunk, status=status)
            totals = self._fetch_inventory_totals()
            summary.total_quantity += _safe_float_number(totals.get("total"))
            summary.total_cost += _safe_float_number(totals.get("totalCost"))
            summary.total_cost_currency += _safe_float_number(totals.get("totalCostCurrency"))
            summary.in_transit_total += _safe_float_number(totals.get("inTransitTotal"))
            summary.in_transit_total_currency += _safe_float_number(totals.get("inTransitTotalCurrency"))
            if totals.get("cacheUpdateTime"):
                summary.cache_update_time = _safe_text(totals.get("cacheUpdateTime"))
        return summary

    def export_realtime_inventory_warehouse(
            self,
            *,
            warehouse_id: str | int,
            output_path: str | Path,
            show_rmb_column: bool = False,
            max_pages: int = 100,
            expected_warehouse_name: str | None = None,
    ) -> Path:
        """
        导出指定仓库的实时库存到本地Excel文件

        :param warehouse_id: 仓库ID
        :param output_path: 输出文件路径
        :param show_rmb_column: 是否展示人民币列
        :param max_pages: 最大拉取页数限制
        """
        warehouse_id_text = _safe_text(warehouse_id)
        if not warehouse_id_text:
            raise MabangApiError("仓库ID是唯一项")

        search_payload = self._build_inventory_search_payload(warehouse_id=warehouse_id_text, skus=[])
        search_payload.update(
            {
                "stockSkuStr": "",
                "warehouseIdArr": warehouse_id_text,
                "warehouseIds[]": warehouse_id_text,
            }
        )
        # Each export owns its query token; another warehouse must never reuse
        # it. Old ERP versions omit the field and retain the session-only API.
        search_token = ""

        def select_warehouse() -> None:
            nonlocal search_token
            search_token = ""
            payload = self._post_inventory_json(
                INVENTORY_SEARCH_URL,
                data=search_payload,
                context=f"实时库存查询 warehouse_id={warehouse_id_text}",
                retries=1,  # The page retry owns context recovery and warehouse replay.
            )
            if not payload.get("success"):
                raise MabangApiError(f"实时库存查询失败 warehouse_id={warehouse_id_text}")
            if "searchToken" in payload:
                token = payload["searchToken"]
                if not isinstance(token, str) or not token.strip():
                    raise MabangApiError("实时库存查询返回无效导出令牌")
                search_token = token

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        workbook_out = Workbook(write_only=True)
        worksheet_out = workbook_out.create_sheet("Sheet1")
        wrote_header = False
        total_rows = 0
        seen_page_digests: set[str] = set()
        seen_skus: set[str] = set()
        sku_col_index: int = -1

        try:
            for page in range(1, max(1, int(max_pages)) + 1):
                # The ERP keeps the warehouse filter in the session. A later
                # export page can occasionally use a different filter, so
                # validate the page before adding any rows to the workbook.
                for attempt in range(2):
                    page_bytes = self._download_realtime_inventory_page(
                        page=page, show_rmb_column=show_rmb_column,
                        select_warehouse=select_warehouse,
                        get_search_token=lambda: search_token,
                    )
                    if expected_warehouse_name is None:
                        break
                    check = load_workbook(BytesIO(page_bytes), read_only=True, data_only=True)
                    try:
                        page_rows = check.active.iter_rows(values_only=True)
                        page_header = next(page_rows, None)
                        if not page_header or "仓库" not in page_header:
                            raise MabangApiError(f"实时库存导出 page={page} 缺少仓库列")
                        page_warehouse_index = page_header.index("仓库")
                        unexpected = {
                            _safe_text(row[page_warehouse_index]).strip()
                            for row in page_rows if len(row) > page_warehouse_index
                            and _safe_text(row[page_warehouse_index]).strip() != expected_warehouse_name
                        }
                    finally:
                        check.close()
                    if not unexpected:
                        break
                    if attempt:
                        raise MabangApiError(
                            f"实时库存导出 page={page} 仓库不符: 请求={expected_warehouse_name}, "
                            f"实际={sorted(unexpected)}"
                        )
                    LOGGER.warning("实时库存导出 page=%s 仓库串页，重新选择仓库后重试: %s", page, warehouse_id_text)
                    select_warehouse()
                page_digest = hashlib.sha1(page_bytes).hexdigest()
                if page_digest in seen_page_digests:
                    raise MabangApiError(f"实时库存导出重复页面 page={page} warehouse_id={warehouse_id_text}")
                seen_page_digests.add(page_digest)
                workbook_in = load_workbook(BytesIO(page_bytes), read_only=True, data_only=True)
                try:
                    worksheet_in = workbook_in.active
                    rows = worksheet_in.iter_rows(values_only=True)
                    header = next(rows, None)
                    if not header:
                        break
                    if not wrote_header:
                        worksheet_out.append(list(header))
                        wrote_header = True
                        header_list = list(header)
                        for idx, col_name in enumerate(header_list):
                            if _safe_text(col_name).strip() in ('库存SKU编号', '库存SKU'):
                                sku_col_index = idx
                                break

                    page_rows = 0
                    new_rows = 0
                    for row in rows:
                        row_list = list(row)
                        page_rows += 1
                        if sku_col_index >= 0 and len(row_list) > sku_col_index:
                            sku_value = _safe_text(row_list[sku_col_index]).strip()
                            if sku_value and sku_value in seen_skus:
                                continue
                            if sku_value:
                                seen_skus.add(sku_value)
                        worksheet_out.append(row_list)
                        new_rows += 1
                    total_rows += new_rows
                    if page_rows < INVENTORY_EXPORT_PAGE_SIZE:
                        break
                finally:
                    workbook_in.close()

            if not wrote_header:
                worksheet_out.append([])
            workbook_out.save(output)
            LOGGER.info("已导出的实时库存 warehouse_id=%s rows=%s (去重后唯一SKU=%s) to %s", warehouse_id_text, total_rows, len(seen_skus), output)
            return output
        except Exception:
            self._discard_write_only_workbook(workbook_out)
            if output.exists():
                output.unlink()
            raise
        finally:
            close = getattr(workbook_out, "close", None)
            if callable(close):
                close()

    def export_realtime_inventory_warehouses(
            self,
            warehouse_map: dict[str, str | int],
            output_dir: str | Path,
            *,
            show_rmb_column: bool = False,
            skip_existing: bool = True,
    ) -> list[str]:
        """
        批量导出多个仓库的实时库存

        :param warehouse_map: 仓库名称到ID的映射字典
        :param output_dir: 输出目录
        :param show_rmb_column: 是否展示人民币列
        :param skip_existing: 若文件已存在是否跳过
        """
        output_base = Path(output_dir)
        output_base.mkdir(parents=True, exist_ok=True)
        exported_files: list[str] = []
        for warehouse_name, warehouse_id in warehouse_map.items():
            output_path = output_base / f"{warehouse_name}.xlsx"
            LOGGER.info("正在导出的实时库存 仓库名=%s  至 %s", warehouse_name,output_path)
            if skip_existing and output_path.exists():
                exported_files.append(str(output_path))
                continue
            self.export_realtime_inventory_warehouse(
                warehouse_id=warehouse_id,
                output_path=output_path,
                show_rmb_column=show_rmb_column,
                expected_warehouse_name=warehouse_name,
            )
            exported_files.append(str(output_path))
        return exported_files

    def export_stock_label_skus(
            self,
            *,
            label_name: str,
            output_path: str | Path,
            rows_per_page: int = 500,
    ) -> Path:
        """
        导出特定库存标签下的所有SKU

        :param label_name: 标签名称
        :param output_path: 输出文件路径
        :param rows_per_page: 分页抓取时的页大小
        """
        label_id = self._find_stock_label_id(label_name)
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        skus = self._collect_stock_skus_by_label_id(label_id=label_id, rows_per_page=rows_per_page)

        workbook = Workbook(write_only=True)
        try:
            worksheet = workbook.create_sheet("Sheet1")
            worksheet.append(["库存SKU"])
            for sku in skus:
                worksheet.append([sku])
            workbook.save(output)
            LOGGER.info("成功导出库存标签 [%s]，数据共 %s 行，导出至 %s", label_name, len(skus), output)
            return output
        finally:
            close = getattr(workbook, "close", None)
            if callable(close):
                close()

    def find_stock_label_id(self, label_name: str) -> str:
        """
        根据库存自定义分类名称查找内部ID。

        :param label_name: 自定义分类名称
        """
        return self._find_stock_label_id(label_name)

    def search_stock_rows_by_skus(
            self,
            skus: Sequence[str],
            *,
            batch_size: int = 200,
            rows_per_page_minimum: int = 50,
            status: str = "3",
            progress_callback: Callable[[int, int, int], None] | None = None,
            retries: int = 3,
    ) -> dict[str, dict[str, Any]]:
        """
        通过库存SKU批量查询库存行数据。

        :param skus: 库存SKU列表
        :param batch_size: 每次批量查询的SKU数量
        :param rows_per_page_minimum: 马帮接口每页最小行数
        :param status: SKU状态，空字符串表示全部，3表示正常销售
        :param progress_callback: 每批完成后的回调，参数为已完成批次、总批次、累计命中SKU数
        :param retries: 单批网络或接口异常时的最大尝试次数
        """
        normalized_skus = list(dict.fromkeys(_safe_text(value) for value in skus if _safe_text(value)))
        if not normalized_skus:
            return {}

        batch_size = max(1, min(int(batch_size or 200), MAX_STOCK_SKU_SEARCH_BATCH_SIZE))
        retries = max(1, int(retries or 1))
        result: dict[str, dict[str, Any]] = {}
        total_batches = (len(normalized_skus) + batch_size - 1) // batch_size
        stock_context: dict[str, str] = {}

        def invalidate_stock_context() -> None:
            stock_context.clear()

        for start_index in range(0, len(normalized_skus), batch_size):
            batch_number = start_index // batch_size + 1
            chunk = normalized_skus[start_index:start_index + batch_size]
            payload = _build_stock_sku_batch_search_payload(
                skus=chunk,
                rows_per_page=min(
                    MAX_STOCK_SKU_SEARCH_BATCH_SIZE,
                    max(int(rows_per_page_minimum or 50), len(chunk)),
                ),
                status=status,
            )
            response_payload: dict[str, Any] | None = None
            last_error: Exception | None = None
            for attempt in range(1, retries + 1):
                try:
                    def query_batch_once() -> dict[str, Any]:
                        referer = stock_context.get("referer")
                        if not referer:
                            referer, _ = self._load_iframe_html(STOCK_LIST_PAGE_PATH)
                            stock_context["referer"] = referer
                        return self._post_stock_list_json(
                            request_url=urljoin(referer, STOCK_LIST_API_URL),
                            referer=referer,
                            data=payload,
                            context=f"库存SKU批量查询 batch={batch_number}",
                        )

                    response_payload = self._run_replay_safe_authenticated_read(
                        query_batch_once,
                        context=f"库存SKU批量查询 batch={batch_number}",
                        before_replay=invalidate_stock_context,
                    )
                    break
                except MabangSessionExpiredError:
                    raise
                except Exception as exc:
                    last_error = exc
                    if attempt >= retries:
                        break
                    LOGGER.warning(
                        "库存SKU批量查询失败，刷新页面后重试 batch=%s attempt=%s/%s error=%s",
                        batch_number,
                        attempt,
                        retries,
                        exc,
                    )
                    invalidate_stock_context()
                    time.sleep(_compute_retry_sleep_seconds(attempt, cap=3.0))
            if response_payload is None:
                raise MabangApiError(
                    f"库存SKU批量查询失败 batch={batch_number}: {last_error}"
                ) from last_error
            if "stockData" not in response_payload:
                raise MabangApiError(f"库存SKU批量查询返回缺少stockData字段 batch={batch_number}")
            rows = response_payload.get("stockData")
            if not isinstance(rows, list):
                raise MabangApiError(
                    f"库存SKU批量查询stockData结构无效 batch={batch_number}: "
                    f"type={type(rows).__name__}"
                )
            requested_exact = set(chunk)
            requested_by_casefold: dict[str, str] = {}
            ambiguous_casefold_keys: set[str] = set()
            for requested_sku in chunk:
                casefold_key = requested_sku.casefold()
                previous = requested_by_casefold.get(casefold_key)
                if previous is not None and previous != requested_sku:
                    ambiguous_casefold_keys.add(casefold_key)
                else:
                    requested_by_casefold[casefold_key] = requested_sku
            for row_index, row in enumerate(rows, start=1):
                if not isinstance(row, dict):
                    raise MabangApiError(
                        f"库存SKU批量查询行结构无效 batch={batch_number}, row={row_index}, "
                        f"type={type(row).__name__}"
                    )
                response_sku = _safe_text(row.get("stockSku"))
                if not response_sku:
                    raise MabangApiError(
                        f"库存SKU批量查询行缺少stockSku batch={batch_number}, row={row_index}"
                    )
                if response_sku in requested_exact:
                    result_sku = response_sku
                else:
                    casefold_key = response_sku.casefold()
                    if casefold_key in ambiguous_casefold_keys or casefold_key not in requested_by_casefold:
                        raise MabangApiError(
                            f"库存SKU批量查询返回非请求SKU batch={batch_number}, row={row_index}"
                        )
                    result_sku = requested_by_casefold[casefold_key]
                if result_sku not in result:
                    result[result_sku] = row
            if progress_callback is not None:
                progress_callback(batch_number, total_batches, len(result))
        return result

    def add_stock_label_to_stock_ids(
            self,
            *,
            stock_ids: Sequence[str | int],
            label_id: str | int,
            label_name: str,
            is_order_flag: str | int = "2",
    ) -> dict[str, Any]:
        """
        批量给库存SKU添加自定义分类。

        :param stock_ids: 马帮库存SKU内部ID列表
        :param label_id: 自定义分类ID
        :param label_name: 自定义分类名称
        :param is_order_flag: 页面分类继承标记，库存分类通常为2
        """
        normalized_stock_ids = list(dict.fromkeys(_safe_text(value) for value in stock_ids if _safe_text(value)))
        if not normalized_stock_ids:
            return {"success": True, "message": "stock_ids为空", "requestedStockIds": []}

        label_id_text = _safe_text(label_id)
        label_name_text = _safe_text(label_name)
        is_order_flag_text = _safe_text(is_order_flag) or "2"
        if not label_id_text or not label_name_text:
            raise MabangApiError("库存分类ID或名称为空")

        referer, _ = self._load_stock_list_iframe_html()
        response = self.client.post(
            urljoin(referer, STOCK_BATCH_LABEL_PATH),
            data={
                "label": f"{label_id_text}||{label_name_text}||{is_order_flag_text}",
                "stockIds": ",".join(normalized_stock_ids),
            },
            headers={
                "Referer": referer,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        response.raise_for_status()
        payload = _response_json_dict(response, context=f"库存SKU添加分类 label={label_name_text}")
        if not payload.get("success"):
            raise MabangApiError(f"库存SKU添加分类失败: {payload}")
        payload["requestedStockIds"] = normalized_stock_ids
        return payload

    def export_stock_by_skus(
            self,
            *,
            skus: Sequence[str],
            output_path: str | Path,
            field_codes: Sequence[str],
            chunk_size: int = 2000,
    ) -> Path:
        """
        按SKU集合导出库存信息

        :param skus: SKU列表
        :param output_path: 输出文件路径
        :param field_codes: 需要导出的字段配置码
        :param chunk_size: 批次切片大小
        """
        normalized_skus = list(dict.fromkeys(_safe_text(value) for value in skus if _safe_text(value)))
        selected_field_codes = [_safe_text(value) for value in field_codes if _safe_text(value)]
        if not normalized_skus:
            raise MabangApiError("库存导出失败: SKU列表为空")
        if not selected_field_codes:
            raise MabangApiError("库存导出失败: 字段配置码为空")

        referer, _ = self._load_stock_list_iframe_html()
        export_page_url, form_defaults, field_map = self._load_stock_export_template(referer=referer)
        missing_fields = [code for code in selected_field_codes if code not in field_map]
        if missing_fields:
            raise MabangApiError(f"库存导出失败: 模板中缺失字段配置码: {missing_fields}")

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        workbook_out = Workbook(write_only=True)
        worksheet_out = workbook_out.create_sheet("Sheet1")
        expected_header = None
        temporary_output = None
        chunk_size = max(1, min(500, int(chunk_size)))
        try:
            for start_index in range(0, len(normalized_skus), chunk_size):
                chunk = normalized_skus[start_index: start_index + chunk_size]
                export_file = self._export_stock_sku_chunk(
                    skus=chunk,
                    field_codes=selected_field_codes,
                    field_map=field_map,
                    form_defaults=form_defaults,
                    export_page_url=export_page_url,
                )
                try:
                    workbook_in = load_workbook(BytesIO(export_file.content), read_only=True, data_only=True)
                except Exception as exc:
                    raise MabangApiError(
                        f"库存SKU导出文件无法解析为Excel: batch={start_index // chunk_size + 1}, "
                        f"error_type={type(exc).__name__}"
                    ) from None
                try:
                    worksheet_in = workbook_in.active
                    rows = worksheet_in.iter_rows(values_only=True)
                    header = next(rows, None)
                    if not header or not any(_safe_text(cell) for cell in header):
                        raise MabangApiError(f"库存SKU导出缺少表头: batch={start_index // chunk_size + 1}")
                    if expected_header is None:
                        expected_header = header
                        worksheet_out.append(list(header))
                    elif header != expected_header:
                        raise MabangApiError(f"库存SKU导出批次表头不一致: batch={start_index // chunk_size + 1}")
                    for row in rows:
                        worksheet_out.append(list(row))
                finally:
                    workbook_in.close()

            fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
            os.close(fd)
            temporary_output = Path(temporary_name)
            workbook_out.save(temporary_output)
            os.replace(temporary_output, output)
            return output
        except Exception:
            self._discard_write_only_workbook(workbook_out)
            raise
        finally:
            if temporary_output is not None:
                temporary_output.unlink(missing_ok=True)
            close = getattr(workbook_out, "close", None)
            if callable(close):
                close()

    def list_inventory_warehouses(self) -> list[dict[str, str]]:
        """Read selectable warehouse identities from the realtime inventory page.

        The management directory has a different scope. Names must therefore
        come from the label belonging to each actual inventory checkbox.
        """
        context = "实时库存可选仓库目录"
        selector = 'input[name="warehouseIds[]"][value]'

        def warehouse_name(control: Any, document: Any, warehouse_id: str) -> str:
            labels = []
            parent_label = control.find_parent("label")
            if parent_label is not None:
                labels.append(parent_label)
            control_id = _safe_text(control.get("id")).strip()
            if control_id:
                labels.extend(document.find_all("label", attrs={"for": control_id}))
            if labels:
                names = set()
                for label in labels:
                    contained = label.select(selector)
                    if contained and (len(contained) != 1 or contained[0] is not control):
                        raise MabangApiError(f"{context}: 仓库标签关联不明确 ID={warehouse_id}")
                    name = label.get_text("", strip=True).strip()
                    if not name:
                        raise MabangApiError(f"{context}: 仓库名称为空 ID={warehouse_id}")
                    names.add(name)
                if len(names) != 1:
                    raise MabangApiError(f"{context}: 仓库名称存在歧义 ID={warehouse_id}")
                return names.pop()

            # A bare checkbox can only use its nearest container when the
            # container has one checkbox and one independent piece of text.
            for container in control.parents:
                if container.name in {"body", "html", "form", "[document]"}:
                    break
                checkboxes = container.select('input[type="checkbox"], input[name="warehouseIds[]"]')
                if len(checkboxes) != 1:
                    break
                texts = [str(value).strip() for value in container.stripped_strings if str(value).strip()]
                if len(texts) > 1:
                    raise MabangApiError(f"{context}: 仓库名称存在歧义 ID={warehouse_id}")
                if texts:
                    return texts[0]
            raise MabangApiError(f"{context}: 无法确认仓库名称 ID={warehouse_id}")

        def read_catalog() -> list[dict[str, str]]:
            self._ensure_inventory_detail_context()
            if not self._inventory_iframe_url:
                raise MabangApiError(f"{context}: 缺少库存 iframe 上下文")
            response = self.client.get(
                self._inventory_iframe_url,
                headers={"Referer": self._url(INVENTORY_DETAIL_PATH)},
                timeout=_build_http_timeout(self.timeout_seconds),
            )
            _validate_inventory_page_response(response, context=context)
            document = BeautifulSoup(_decode_html_page(response), "html.parser")
            rows: list[dict[str, str]] = []
            names_by_id: dict[str, str] = {}
            ids_by_name: dict[str, str] = {}
            for control in document.select(selector):
                if _safe_text(control.get("type")).casefold() != "checkbox":
                    continue
                if control.has_attr("disabled") or any(
                        parent.name == "fieldset" and parent.has_attr("disabled")
                        for parent in control.parents):
                    continue
                warehouse_id = _safe_text(control.get("value")).strip()
                if warehouse_id in {"", "0"}:
                    continue
                # The inventory page also names its select-all checkbox
                # warehouseIds[]. It is not a warehouse identity; accept only
                # the observed sentinel with an unambiguous associated label.
                if warehouse_id == "all" and warehouse_name(control, document, warehouse_id) == "全部仓库":
                    continue
                if not warehouse_id.isascii() or not warehouse_id.isdigit() or int(warehouse_id) <= 0:
                    raise MabangApiError(f"{context}: 仓库选项 ID 无效")
                name = warehouse_name(control, document, warehouse_id)
                previous_name = names_by_id.get(warehouse_id)
                if previous_name is not None and previous_name != name:
                    raise MabangApiError(f"{context}: 相同仓库 ID 对应不同名称 ID={warehouse_id}")
                name_key = name.casefold()
                previous_id = ids_by_name.get(name_key)
                if previous_id is not None and previous_id != warehouse_id:
                    raise MabangApiError(f"{context}: 不同仓库 ID 对应相同名称 ID={previous_id},{warehouse_id}")
                if previous_name is None:
                    rows.append({"id": warehouse_id, "name": name})
                    names_by_id[warehouse_id] = name
                    ids_by_name[name_key] = warehouse_id
            if not rows:
                raise MabangApiError(f"{context}: 没有实际可选的仓库选项")
            return rows

        failure: MabangApiError | None = None
        try:
            return self._run_replay_safe_authenticated_read(read_catalog, context=context)
        except MabangSessionExpiredError:
            failure = MabangSessionExpiredError(f"{context}: 登录会话不可用，单次恢复未成功")
        except httpx.HTTPError as exc:
            failure = MabangApiError(f"{context}: 页面读取失败 error_type={type(exc).__name__}")
        # Raise outside the handler so a transport/login exception cannot leak
        # through either __cause__ or __context__ in a caller's diagnostic log.
        raise failure from None

    def list_warehouses(self, *, strict_value_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
        """
        列出系统所有仓库
        """
        required = {str(value) for value in strict_value_ids}
        public_referer, public_html = self._load_iframe_html(WAREHOUSE_LIST_PAGE_PATH)
        if urlparse(public_referer).path.rstrip("/") == "/views/warehouse/list":
            # The new SPA has no legacy PHP directory endpoint. The helper
            # validates its origin before exchanging any authentication value.
            all_rows = self._list_modern_warehouses(public_referer, strict_value_ids=required)
        else:
            all_rows = _parse_warehouse_table(public_html, preserve_invalid_values=bool(required))
            seen_ids = {_safe_text(row.get("id")) for row in all_rows}

            # Legacy HTML tenants still expose the self/third-party directory.
            for w_type in ["1", "2"]:
                options = {'preserve_invalid_values': True} if required else {}
                rows = self._query_warehouse_list_by_type(referer=public_referer, warehouse_type=w_type, **options)
                for row in rows:
                    row_id = _safe_text(row.get("id"))
                    if row_id and row_id not in seen_ids:
                        all_rows.append(row)
                        seen_ids.add(row_id)
                    elif required and row_id:
                        # Explicit queries are later than the initial page snapshot.
                        all_rows = [row if _safe_text(old.get('id')) == row_id else old for old in all_rows]

        for row in all_rows:
            needs_zero_check = row.pop('_inventory_value_placeholder', False)
            if str(row['id']) in required:
                if needs_zero_check and row['totalInventoryValue'] is None:
                    row['inventoryValueVerification'] = self._verify_zero_warehouse_inventory(row)
                    row['totalInventoryValue'] = 0.0
                for field in ('totalInventoryValue', 'totalInTransitValue'):
                    if row[field] is None or not math.isfinite(row[field]):
                        raise MabangApiError(f"仓库金额缺失或无效: {row['name']} ({row['id']})/{field}")
        missing = required - {_safe_text(row.get('id')) for row in all_rows}
        if missing:
            raise MabangApiError(f"仓库列表缺少货值统计仓库: {', '.join(sorted(missing))}")

        return all_rows

    def _list_modern_warehouses(
            self, referer: str, *, strict_value_ids: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Read the SPA directory atomically, using its fixed MAS SSO contract.

        These endpoint/field names come from the published warehouse SPA.
        DOMAIN/cloud are navigation hints, never authority for API requests.
        No response body or authentication value is included in diagnostics.
        """
        from urllib.parse import parse_qs

        try:
            parsed = urlparse(referer)
            allowed = (
                parsed.scheme == "https"
                and parsed.hostname == "fe-private.mabangerp.com"
                and parsed.port in (None, 443)
                and parsed.username is None and parsed.password is None
                and parsed.path.rstrip("/") == "/views/warehouse/list"
                and not parsed.fragment
            )
            query = parse_qs(parsed.query, keep_blank_values=True)
        except (TypeError, ValueError):
            allowed, query = False, {}
        if not allowed:
            raise MabangModernWarehouseQueryError("untrusted_frontend")

        def parameter(name: str, default: str = "") -> str:
            values = query.get(name, [default])
            if (len(set(values)) != 1 or not values[0].strip()
                    or any(ord(c) < 32 or ord(c) > 126 for c in values[0])):
                raise MabangModernWarehouseQueryError("invalid_sso_parameters")
            return values[0]

        key = parameter("key")
        lang = parameter("lang", "cn")
        timezone = parameter("tz", "UTC+8")
        # The published SPA defaults cluster-id to 2 independently of cloud.
        cluster = "2"
        api_base = "https://mas-oapi-private.mabangerp.com/mas-erp-system-service"

        def post_json(path: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
            response = None
            failure = None
            try:
                response = self.client.post(
                    api_base + path, json=body, headers=headers,
                    timeout=DEFAULT_WAREHOUSE_LIST_TIMEOUT_SECONDS,
                    follow_redirects=False,
                )
            except (httpx.HTTPError, ValueError, UnicodeError):
                failure = MabangModernWarehouseQueryError("transport_failure")
            # Raise outside except: neither exception chain may retain a key.
            if failure is not None:
                raise failure from None
            assert response is not None
            if not 200 <= response.status_code < 300:
                raise MabangModernWarehouseQueryError("http_failure", status_code=response.status_code)
            payload = None
            try:
                payload = response.json()
            except (ValueError, UnicodeError):
                pass
            if not isinstance(payload, dict):
                raise MabangModernWarehouseQueryError("invalid_json_object")
            code = payload.get("code")
            if (type(code) is not int or code not in (0, 200)
                    or ("success" in payload and payload["success"] is not True)):
                raise MabangModernWarehouseQueryError("api_failure")
            return payload

        auth = post_json("/auth/token/by-key", {
            "key": key, "lang": "zh" if lang == "cn" else lang, "clusterId": cluster,
        }, {"Content-Type": "application/json", "key": key, "cluster-id": cluster})
        identity = auth.get("data")
        if auth["code"] != 200 or not isinstance(identity, dict):
            raise MabangModernWarehouseQueryError("invalid_sso_identity")
        token = identity.get("token")
        if not isinstance(token, str):
            raise MabangModernWarehouseQueryError("invalid_sso_token")
        token = token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token or any(ord(c) <= 32 or ord(c) > 126 for c in token):
            raise MabangModernWarehouseQueryError("invalid_sso_token")
        for name in ("companyId", "employeeId", "isAdmin"):
            if identity.get(name) is None or str(identity[name]).strip() == "":
                raise MabangModernWarehouseQueryError("invalid_sso_identity")
        headers = {
            "Content-Type": "application/json", "ProjectId": "erp",
            "Authorization": f"Bearer {token}", "key": key, "cluster-id": cluster,
            "lang": lang, "timezone": timezone,
        }

        def integer(value: Any) -> bool:
            return type(value) is int and value >= 0

        def amount(row: dict[str, Any], names: tuple[str, ...]) -> float | None:
            for name in names:
                value = row.get(name)
                if value is not None:
                    return None if isinstance(value, bool) else _warehouse_amount(value, True)
            return None

        required = {str(value) for value in strict_value_ids}
        value_sources = {
            "totalInventoryValue": (
                ("totalCostRmb", "totalCostRMB", "inventoryValueRmb", "inventoryValueRMB", "totalCost"),
                "/warehouse/cost", "canViewInventoryValue",
            ),
            "totalInTransitValue": (
                ("inTransitValueRmb", "inTransitValueRMB", "transitValueRmb", "transitValueRMB", "inTransitValue"),
                "/warehouse/transit-cost", "canViewInTransitValue",
            ),
        }

        def detail_totals(warehouse_id: str, path: str) -> dict[str, Any]:
            # These explicit aggregates cover the whole warehouse, independently
            # of its paginated detail rows. Never derive them from a page sum.
            expected = None
            proof = []
            for detail_page in (1, 2):
                detail_request = {
                    "warehouseId": int(warehouse_id), "pageIndex": detail_page, "pageSize": 20,
                }
                payload = post_json(path, detail_request, headers)
                data = payload.get("data")
                if (not isinstance(data, dict) or not isinstance(data.get("records"), list)
                        or ("success" in data and data["success"] is not True)):
                    raise MabangModernWarehouseQueryError("invalid_detail_structure")
                current, size, total, pages = (data.get(k) for k in ("current", "size", "total", "pages"))
                if (not all(integer(v) for v in (current, size, total, pages))
                        or current != detail_page or size != 20 or pages != (total + 19) // 20):
                    raise MabangModernWarehouseQueryError("invalid_detail_metadata")
                records = data["records"]
                if (len(records) != min(20, max(0, total - (detail_page - 1) * 20))
                        or any(not isinstance(row, dict) for row in records)):
                    raise MabangModernWarehouseQueryError("incomplete_detail_page")
                value = amount(data, ("totalStockValue",))
                quantity = amount(data, ("totalStockQuantity",))
                if value is None or quantity is None:
                    raise MabangModernWarehouseQueryError("invalid_detail_totals")
                if total == 0 and (value != 0 or quantity != 0):
                    raise MabangModernWarehouseQueryError("inconsistent_empty_detail_totals")
                observed = (total, pages, value, quantity)
                if expected is None:
                    expected = observed
                elif expected != observed:
                    raise MabangModernWarehouseQueryError("detail_totals_changed")
                proof.append({"page": detail_page, "request": detail_request,
                              "current": current, "size": size, "total": total, "pages": pages,
                              "totalStockValue": value, "totalStockQuantity": quantity,
                              "record_count": len(records),
                              "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                if pages <= 1:
                    break
            assert expected is not None
            return {"path": path, "total": expected[0], "pages": expected[1],
                    "totalStockValue": expected[2], "totalStockQuantity": expected[3],
                    "checked_pages": proof}

        all_rows: list[dict[str, Any]] = []
        missing_values: list[tuple[dict[str, Any], list[str], dict[str, Any]]] = []
        seen: set[str] = set()
        filters = (
            {"type": 1, "attributeType": 1}, {"type": 1, "attributeType": 2},
            {"type": 2}, {"type": 3}, {"type": 4},
        )
        for warehouse_filter in filters:
            page = 1
            expected = None
            while True:
                payload = post_json("/warehouse/list", {
                    "orderBy": "asc", "pageIndex": page, "pageSize": 20, **warehouse_filter,
                }, headers)
                data = payload.get("data")
                if not isinstance(data, dict) or not isinstance(data.get("records"), list):
                    raise MabangModernWarehouseQueryError("invalid_page_structure")
                current, size, total, pages = (data.get(k) for k in ("current", "size", "total", "pages"))
                if (not all(integer(v) for v in (current, size, total, pages))
                        or current != page or size != 20 or pages != (total + 19) // 20
                        or pages > 10000):
                    raise MabangModernWarehouseQueryError("invalid_page_metadata")
                if expected is None:
                    expected = (total, pages)
                elif expected != (total, pages):
                    raise MabangModernWarehouseQueryError("page_totals_changed")
                records = data["records"]
                if len(records) != min(20, max(0, total - (page - 1) * 20)):
                    raise MabangModernWarehouseQueryError("incomplete_page")
                for row in records:
                    if not isinstance(row, dict):
                        raise MabangModernWarehouseQueryError("invalid_warehouse_identity")
                    raw_id = row.get("id")
                    row_id = str(raw_id).strip() if type(raw_id) in (int, str) else ""
                    name = row.get("name")
                    row_type = row.get("type")
                    if (not row_id.isascii() or not row_id.isdigit() or len(row_id) > 30 or int(row_id) <= 0
                            or not isinstance(name, str) or not name.strip()
                            or any(ord(c) < 32 for c in name)
                            or type(row_type) is not int or row_type != warehouse_filter["type"]):
                        raise MabangModernWarehouseQueryError("invalid_warehouse_identity")
                    row_id = str(int(row_id))
                    if "attributeType" in warehouse_filter and (
                            type(row.get("attributeType")) is not int
                            or row["attributeType"] != warehouse_filter["attributeType"]):
                        raise MabangModernWarehouseQueryError("wrong_warehouse_attribute")
                    if row_id in seen:
                        raise MabangModernWarehouseQueryError("duplicate_warehouse_identity")
                    seen.add(row_id)
                    normalized = {
                        "id": row_id, "name": name.strip(), "type": str(row_type),
                        "totalStock": row.get("total"), "status": _safe_text(row.get("status")),
                        # The SPA maps these native fields to its RMB columns;
                        # *DefaultCurrency and partial transit components are
                        # separate values, never substitutes for missing RMB.
                        **{field: amount(row, source[0]) for field, source in value_sources.items()},
                    }
                    all_rows.append(normalized)
                    # Null directory statistics are absent data, not proof of
                    # zero. Invalid explicit amounts must still fail strictly.
                    absent = [field for field, source in value_sources.items()
                              if all(row.get(alias) is None for alias in source[0])]
                    if row_id in required and absent:
                        missing_values.append((normalized, absent, {
                            "orderBy": "asc", "pageIndex": page, "pageSize": 20, **warehouse_filter,
                        }))
                if page >= pages:
                    break
                page += 1

        permissions: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}
        for row, absent, request in missing_values:
            cache_key = tuple(sorted(request.items()))
            if cache_key not in permissions:
                payload = post_json("/warehouse/list/init", request, headers)
                init = payload.get("data")
                if not isinstance(init, dict) or ("success" in init and init["success"] is not True):
                    raise MabangModernWarehouseQueryError("invalid_detail_permissions")
                currency = init.get("systemDefaultCurrency")
                if not isinstance(currency, str) or currency.strip().upper() not in {"RMB", "CNY"}:
                    raise MabangModernWarehouseQueryError("unverified_detail_currency")
                permissions[cache_key] = init
            init = permissions[cache_key]
            if (init.get("canViewCost") is not True or init.get("canViewInventoryDetail") is not True
                    or any(init.get(value_sources[field][2]) is not True for field in absent)):
                raise MabangModernWarehouseQueryError("detail_permission_denied")
            evidence = {"source": "warehouse_detail_global_totals", "warehouse_id": row["id"],
                        "currency": "RMB", "source_system_currency": init["systemDefaultCurrency"],
                        "permission_path": "/warehouse/list/init",
                        "permissions": {name: init.get(name) is True for name in (
                            "canViewInventoryValue", "canViewInTransitValue", "canViewCost", "canViewInventoryDetail",
                        )},
                        "directory_path": "/warehouse/list", "directory_request": request,
                        "directory_missing_aliases": {
                            field: {alias: None for alias in value_sources[field][0]} for field in absent
                        }, "fields": {}}
            for field in absent:
                proof = detail_totals(row["id"], value_sources[field][1])
                row[field] = proof["totalStockValue"]
                evidence["fields"][field] = proof
            row["modernValueVerification"] = evidence
        return all_rows

    def _verify_zero_warehouse_inventory(self, row: dict[str, Any]) -> dict[str, Any]:
        """Resolve the ERP's --/-- stock/value display only after an explicit zero check."""
        warehouse_id = str(row['id'])
        label = f"{row['name']} ({warehouse_id})"
        # Totals depend on this session's last search. Keep the unfiltered warehouse
        # search and totals read adjacent; never use get_inventory_total_by_skus([]),
        # which returns a default zero without querying the ERP.
        try:
            search = self._search_inventory_by_skus(warehouse_id=warehouse_id, skus=[], status='')
            if search.get('success') is not True:
                raise MabangApiError('库存查询未成功')
            # Refreshing/relogging between search and totals loses the warehouse
            # binding. A failed totals read must fail this check, not retry alone.
            totals = self._fetch_inventory_totals(retries=1)
            if totals.get('success') is not True:
                raise MabangApiError('库存合计查询未成功')
        except Exception as exc:
            raise MabangApiError(f"仓库占位货值复核失败: {label} ({type(exc).__name__})") from None
        fields = ('total', 'totalCost', 'totalCostCurrency')
        values = {field: _warehouse_amount(totals.get(field), True) for field in fields}
        if any(value is None or value != 0 for value in values.values()):
            raise MabangApiError(f"仓库占位货值无法确认为零: {label}，全仓库存数量及两种币种货值须明确为零")
        LOGGER.info('仓库库存及货值占位符经全仓合计复核为零: %s', label)
        return {'source': 'full_warehouse_inventory_totals', 'warehouse_id': warehouse_id,
                **values, 'cacheUpdateTime': _safe_text(totals.get('cacheUpdateTime'))}

    def list_pending_warehouse_transfer_shipments(
            self,
            warehouse_id: str | int,
            *,
            rows_per_page: int = 100,
            max_pages: int = 100,
            request_retries: int = 3,
    ) -> list[MabangWarehouseTransferShipment]:
        """查询目标仓库的待签收调拨单，并返回每个批次及其库存SKU。"""
        warehouse_id_text = _safe_text(warehouse_id)
        if not warehouse_id_text:
            raise MabangApiError("调拨查询失败：目标仓库ID为空")

        rows_per_page = max(1, min(int(rows_per_page), 500))
        max_pages = max(1, int(max_pages))
        retry_count = max(1, int(request_retries))
        all_shipments: list[MabangWarehouseTransferShipment] = []
        seen_allocation_ids: set[str] = set()

        for page in range(1, max_pages + 1):
            payload = _build_warehouse_transfer_search_payload(
                warehouse_id=warehouse_id_text,
                page=page,
                rows_per_page=rows_per_page,
            )
            response_payload: dict[str, Any] | None = None
            last_error: Exception | None = None

            for attempt in range(1, retry_count + 1):
                try:
                    self._ensure_warehouse_transfer_context()
                    referer = self._warehouse_transfer_iframe_url or ""
                    endpoint = urljoin(referer, WAREHOUSE_TRANSFER_SEARCH_PATH)
                    parsed_referer = urlparse(referer)
                    origin = f"{parsed_referer.scheme}://{parsed_referer.netloc}"
                    response = self.client.post(
                        endpoint,
                        data=payload,
                        headers={
                            "Referer": referer,
                            "Origin": origin,
                            "X-Requested-With": "XMLHttpRequest",
                        },
                    )
                    response.raise_for_status()
                    parsed_payload = response.json()
                    if not isinstance(parsed_payload, dict):
                        raise MabangApiError("调拨查询返回了非JSON对象")
                    if not parsed_payload.get("success"):
                        raise MabangApiError(f"调拨查询失败: {parsed_payload}")
                    response_payload = parsed_payload
                    break
                except Exception as exc:  # pragma: no cover - live request guard
                    last_error = exc
                    if attempt >= retry_count:
                        break
                    LOGGER.warning(
                        "待签收调拨查询失败，准备重试 warehouse_id=%s page=%s attempt=%s error=%s",
                        warehouse_id_text,
                        page,
                        attempt,
                        exc,
                    )
                    self._refresh_warehouse_transfer_context_for_retry()
                    time.sleep(_compute_retry_sleep_seconds(attempt, cap=3.0))

            if response_payload is None:
                raise MabangApiError(
                    f"待签收调拨查询失败 warehouse_id={warehouse_id_text} page={page}: {last_error}"
                ) from last_error

            raw_message = response_payload.get("message")
            message_html = "" if raw_message is False or raw_message is None else _safe_text(raw_message)
            if not message_html:
                break

            page_shipments = parse_warehouse_transfer_shipments(
                message_html,
                warehouse_id=warehouse_id_text,
            )
            if not page_shipments:
                raise MabangApiError(
                    f"待签收调拨返回了无法解析的列表 warehouse_id={warehouse_id_text} page={page}"
                )
            missing_sku_batches = [
                shipment.batch_code or shipment.allocation_id
                for shipment in page_shipments
                if not shipment.stock_skus
            ]
            if missing_sku_batches:
                raise MabangApiError(
                    "待签收调拨未解析出库存SKU: " + ", ".join(missing_sku_batches[:10])
                )

            for shipment in page_shipments:
                allocation_key = shipment.allocation_id or shipment.batch_code
                if not allocation_key or allocation_key in seen_allocation_ids:
                    continue
                seen_allocation_ids.add(allocation_key)
                all_shipments.append(shipment)

            total_pages = _parse_warehouse_transfer_total_pages(
                response_payload.get("pageHtml")
            )
            if page >= total_pages:
                break
        else:
            raise MabangApiError(
                f"待签收调拨分页超过限制 warehouse_id={warehouse_id_text} max_pages={max_pages}"
            )

        return all_shipments

    def manual_sync_stock_rules(
            self,
            rule_name_prefixes: Sequence[str] | None = None,
            *,
            page_size: int = 100,
            dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        按规则名称前缀触发手动库存同步。

        :param rule_name_prefixes: 规则名称前缀；接口查询方式与页面“规则名称/开头是”一致。
            传入 None 时保留旧版行为：分页同步全部规则。
        :param page_size: 分页大小
        :param dry_run: 仅查询匹配规则，不提交同步
        """
        if rule_name_prefixes is None:
            prefixes = [""]
        else:
            prefixes = list(dict.fromkeys(
                _safe_text(prefix) for prefix in rule_name_prefixes if _safe_text(prefix)
            ))
        if rule_name_prefixes is not None and not prefixes:
            raise ValueError("rule_name_prefixes 不能为空")

        token = self._get_yunbi_token(STOCK_SYNC_PAGE_PATH)
        matched_by_id: dict[str, dict[str, Any]] = {}
        prefix_matches: dict[str, list[str]] = {}

        for prefix in prefixes:
            page = 1
            prefix_matches[prefix] = []
            while True:
                payload = self._get_stock_sync_rule_page(
                    token=token,
                    page=page,
                    page_size=page_size,
                    rule_name_prefix=prefix,
                )
                data = payload.get("data") if isinstance(payload, dict) else {}
                if not isinstance(data, dict):
                    raise MabangApiError(f"库存同步列表返回无效数据: {payload}")
                rows = data.get("data") or []
                if not isinstance(rows, list):
                    raise MabangApiError(f"库存同步列表明细返回无效数据: {payload}")
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    rule_id = _safe_text(row.get("id"))
                    rule_name = _safe_text(row.get("rule_name"))
                    # 即使服务端搜索语义变化，也不允许带入不符合前缀的规则。
                    if rule_id and rule_name.startswith(prefix):
                        matched_by_id[rule_id] = row
                        prefix_matches[prefix].append(rule_id)

                if len(rows) < max(1, int(page_size)):
                    break
                last_page = data.get("last_page")
                try:
                    if last_page and page >= int(last_page):
                        break
                except (TypeError, ValueError):
                    pass
                page += 1

        matched_rules = list(matched_by_id.values())
        result: dict[str, Any] = {
            "prefixes": prefixes,
            "prefixMatches": prefix_matches,
            "matchedRules": matched_rules,
            "requestedIds": list(matched_by_id),
            "dryRun": bool(dry_run),
        }
        if dry_run or not matched_by_id:
            return result

        result["response"] = self._post_stock_sync_manual_sync(
            token=token,
            ids=list(matched_by_id),
        )
        return result

    def manual_sync_unsynced_stock_rule_details(
            self,
            rule_name_prefixes: Sequence[str],
            *,
            page_size: int = 100,
            submit_batch_size: int = 100,
            submit_delay_seconds: float = 0.05,
            fetch_workers: int = 4,
            dry_run: bool = False,
            continue_on_error: bool = False,
            progress_callback: Callable[[dict[str, Any]], None] | None = None,
            progress_interval_pages: int = 50,
            progress_interval_batches: int = 50,
    ) -> dict[str, Any]:
        """同步指定规则下“同步状态=未同步”的全部商品明细。

        该流程对应页面操作：进入“同步平台商品明细”，配对状态不限制，
        同步状态选择“未同步”，全选当前结果并点击明细弹窗内的“手动同步”。

        读取请求由底层接口执行退避重试。continue_on_error=True 时，单条规则
        或单个提交批次失败不会阻断后续规则；失败详情会返回给调用方，由调用方
        将本次任务标记为部分失败。提交超时不会在当前批次内盲目重试，因为服务端
        可能已经受理；后续运行重新查询“未同步”状态即可安全补偿。
        """
        prefixes = list(dict.fromkeys(
            _safe_text(prefix) for prefix in rule_name_prefixes if _safe_text(prefix)
        ))
        if not prefixes:
            raise ValueError("rule_name_prefixes 不能为空")
        page_size = min(100, max(1, int(page_size)))
        submit_batch_size = min(100, max(1, int(submit_batch_size)))
        fetch_workers = min(8, max(1, int(fetch_workers)))
        progress_interval_pages = max(1, int(progress_interval_pages))
        progress_interval_batches = max(1, int(progress_interval_batches))

        token = self._get_yunbi_token(STOCK_SYNC_PAGE_PATH)
        matched_by_id: dict[str, dict[str, Any]] = {}
        prefix_matches: dict[str, list[str]] = {}
        for prefix in prefixes:
            page = 1
            prefix_matches[prefix] = []
            while True:
                payload = self._get_stock_sync_rule_page(
                    token=token,
                    page=page,
                    page_size=100,
                    rule_name_prefix=prefix,
                )
                data = payload.get("data") if isinstance(payload, dict) else {}
                rows = data.get("data") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    raise MabangApiError(f"库存同步列表明细返回无效数据: {payload}")
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    rule_id = _safe_text(row.get("id"))
                    rule_name = _safe_text(row.get("rule_name"))
                    if rule_id and rule_name.startswith(prefix):
                        matched_by_id[rule_id] = row
                        prefix_matches[prefix].append(rule_id)
                if len(rows) < 100:
                    break
                last_page = data.get("last_page")
                try:
                    if last_page and page >= int(last_page):
                        break
                except (TypeError, ValueError):
                    pass
                page += 1

        rule_results: list[dict[str, Any]] = []
        failed_rules: list[dict[str, Any]] = []
        failed_batches: list[dict[str, Any]] = []
        total_detail_count = 0
        submitted_batch_count = 0
        uncertain_batch_count = 0

        def emit_progress(row: dict[str, Any]) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(dict(row))
            except Exception:
                LOGGER.exception("库存同步进度回调失败，已忽略")

        for rule_index, (rule_id, rule) in enumerate(matched_by_id.items(), start=1):
            rule_name = _safe_text(rule.get("rule_name"))
            progress_base = {
                "ruleIndex": rule_index,
                "ruleCount": len(matched_by_id),
                "ruleId": rule_id,
                "ruleName": rule_name,
            }
            emit_progress({
                **progress_base,
                "event": "rule_started",
                "status": "fetching",
            })
            detail_ids: list[str] = []
            seen_detail_ids: set[str] = set()
            sku_row_count = 0
            detail_row_count = 0
            expected_total = 0
            last_page = 0
            pages_read = 0
            batch_count = 0
            rule_failed_batches: list[dict[str, Any]] = []
            phase = "fetch"

            try:
                first_payload = self._get_stock_sync_detail_page(
                    token=token,
                    stock_sync_id=rule_id,
                    platform_id=_safe_text(rule.get("platform_id")),
                    page=1,
                    page_size=page_size,
                    sync_flag="1",
                )
                first_data = first_payload.get("data") if isinstance(first_payload, dict) else {}
                first_rows = first_data.get("data") if isinstance(first_data, dict) else None
                if not isinstance(first_rows, list):
                    raise MabangApiError(f"同步商品明细返回无效数据: {first_payload}")
                has_expected_total = first_data.get("total") is not None
                try:
                    expected_total = int(first_data.get("total") or 0)
                except (TypeError, ValueError):
                    expected_total = 0
                try:
                    last_page = max(1, int(first_data.get("last_page") or 1))
                except (TypeError, ValueError):
                    last_page = 1

                def consume_detail_rows(rows: Sequence[dict[str, Any]]) -> None:
                    nonlocal sku_row_count, detail_row_count
                    sku_row_count += len(rows)
                    for detail_row in rows:
                        if not isinstance(detail_row, dict):
                            continue
                        for detail in detail_row.get("v_list") or []:
                            if not isinstance(detail, dict):
                                continue
                            detail_row_count += 1
                            if _safe_text(detail.get("sync_flag")) != "1":
                                continue
                            detail_id = _safe_text(detail.get("id"))
                            if detail_id and detail_id not in seen_detail_ids:
                                seen_detail_ids.add(detail_id)
                                detail_ids.append(detail_id)

                consume_detail_rows(first_rows)
                pages_read = 1
                emit_progress({
                    **progress_base,
                    "event": "fetch_progress",
                    "status": "fetching",
                    "pagesRead": pages_read,
                    "totalPages": last_page,
                    "detailRowsRead": detail_row_count,
                    "detailCount": len(detail_ids),
                })
                if last_page > 1:
                    with ThreadPoolExecutor(max_workers=fetch_workers) as executor:
                        futures = {
                            executor.submit(
                                self._get_stock_sync_detail_page,
                                token=token,
                                stock_sync_id=rule_id,
                                platform_id=_safe_text(rule.get("platform_id")),
                                page=page,
                                page_size=page_size,
                                sync_flag="1",
                            ): page
                            for page in range(2, last_page + 1)
                        }
                        for future in as_completed(futures):
                            page_payload = future.result()
                            page_data = page_payload.get("data") if isinstance(page_payload, dict) else {}
                            page_rows = page_data.get("data") if isinstance(page_data, dict) else None
                            if not isinstance(page_rows, list):
                                raise MabangApiError(
                                    f"同步商品明细第{futures[future]}页返回无效数据: {page_payload}"
                                )
                            consume_detail_rows(page_rows)
                            pages_read += 1
                            if (
                                    pages_read % progress_interval_pages == 0
                                    or pages_read == last_page
                            ):
                                emit_progress({
                                    **progress_base,
                                    "event": "fetch_progress",
                                    "status": "fetching",
                                    "pagesRead": pages_read,
                                    "totalPages": last_page,
                                    "detailRowsRead": detail_row_count,
                                    "detailCount": len(detail_ids),
                                })

                # detailedList 以 v_list 明细数分页。拉取期间服务端可能正好完成
                # 少量同步，首屏 total 与随后分页会出现小幅漂移，不应因此丢弃
                # 已完整读取的数万条明细。超过动态容忍值仍按异常处理。
                pagination_drift = expected_total - detail_row_count if has_expected_total else 0
                drift_tolerance = max(5, min(page_size, (expected_total + 999) // 1000))
                if pagination_drift and abs(pagination_drift) > drift_tolerance:
                    raise MabangApiError(
                        f"同步商品明细分页校验失败: rule={rule_name}, "
                        f"expected_detail_rows={expected_total}, collected_detail_rows={detail_row_count}, "
                        f"drift={pagination_drift}, tolerance={drift_tolerance}, "
                        f"eligible_details={len(detail_ids)}, sku_rows={sku_row_count}, "
                        f"pages={pages_read}/{last_page}"
                    )

                phase = "submit"
                total_batch_count = (
                    (len(detail_ids) + submit_batch_size - 1) // submit_batch_size
                    if detail_ids else 0
                )
                emit_progress({
                    **progress_base,
                    "event": "submit_started",
                    "status": "submitting" if not dry_run else "dry_run",
                    "detailCount": len(detail_ids),
                    "totalBatchCount": total_batch_count,
                })
                if not dry_run:
                    for start in range(0, len(detail_ids), submit_batch_size):
                        batch = detail_ids[start:start + submit_batch_size]
                        batch_number = start // submit_batch_size + 1
                        try:
                            self._post_stock_sync_detail_manual_sync(token=token, detail_ids=batch)
                            batch_count += 1
                            submitted_batch_count += 1
                        except Exception as exc:
                            if not continue_on_error:
                                raise
                            # POST 读超时/断连时无法确认服务端是否已受理，当前运行
                            # 不重发同一批；下次只查询 sync_flag=1 可自然补偿。
                            uncertain = isinstance(exc, (httpx.TimeoutException, httpx.TransportError))
                            failed_batch = {
                                "ruleId": rule_id,
                                "ruleName": rule_name,
                                "batchNumber": batch_number,
                                "detailCount": len(batch),
                                "uncertain": uncertain,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                            failed_batches.append(failed_batch)
                            rule_failed_batches.append(failed_batch)
                            if uncertain:
                                uncertain_batch_count += 1
                        processed_batch_count = batch_count + len(rule_failed_batches)
                        if (
                                processed_batch_count % progress_interval_batches == 0
                                or processed_batch_count == total_batch_count
                        ):
                            emit_progress({
                                **progress_base,
                                "event": "submit_progress",
                                "status": "submitting",
                                "detailCount": len(detail_ids),
                                "processedBatchCount": processed_batch_count,
                                "totalBatchCount": total_batch_count,
                                "submittedBatchCount": batch_count,
                                "failedBatchCount": len(rule_failed_batches),
                            })
                        if submit_delay_seconds > 0:
                            time.sleep(float(submit_delay_seconds))

                total_detail_count += len(detail_ids)
                rule_result = {
                    **progress_base,
                    "event": "rule_finished",
                    "expectedDetailRows": expected_total,
                    "detailRowsRead": detail_row_count,
                    "paginationDrift": pagination_drift,
                    "skuRowsRead": sku_row_count,
                    "detailCount": len(detail_ids),
                    "pagesRead": pages_read,
                    "submittedBatchCount": batch_count,
                    "failedBatchCount": len(rule_failed_batches),
                    "status": "partial" if rule_failed_batches else "completed",
                }
                rule_results.append(rule_result)
                if rule_failed_batches:
                    failed_rule = {
                        "ruleId": rule_id,
                        "ruleName": rule_name,
                        "phase": "submit",
                        "failedBatchCount": len(rule_failed_batches),
                        "error": "一个或多个提交批次失败或结果不确定",
                    }
                    failed_rules.append(failed_rule)
                    rule_result["error"] = failed_rule["error"]
                emit_progress(rule_result)
            except Exception as exc:
                if not continue_on_error:
                    raise
                failed_rule = {
                    "ruleId": rule_id,
                    "ruleName": rule_name,
                    "phase": phase,
                    "failedBatchCount": len(rule_failed_batches),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                failed_rules.append(failed_rule)
                rule_result = {
                    **progress_base,
                    "event": "rule_finished",
                    "expectedDetailRows": expected_total,
                    "detailRowsRead": detail_row_count,
                    "skuRowsRead": sku_row_count,
                    "detailCount": len(detail_ids),
                    "pagesRead": pages_read,
                    "submittedBatchCount": batch_count,
                    "failedBatchCount": len(rule_failed_batches),
                    "status": "failed",
                    "phase": phase,
                    "error": failed_rule["error"],
                }
                rule_results.append(rule_result)
                emit_progress(rule_result)

        return {
            "prefixes": prefixes,
            "prefixMatches": prefix_matches,
            "matchedRules": list(matched_by_id.values()),
            "ruleResults": rule_results,
            "totalDetailCount": total_detail_count,
            "submittedBatchCount": submitted_batch_count,
            "completedRuleCount": sum(row.get("status") == "completed" for row in rule_results),
            "failedRuleCount": len(failed_rules),
            "failedRules": failed_rules,
            "failedBatchCount": len(failed_batches),
            "uncertainBatchCount": uncertain_batch_count,
            "failedBatches": failed_batches,
            "dryRun": bool(dry_run),
        }

    def list_online_product_pull_shops(
            self,
            *,
            platform_id: str = "17",
            include_unavailable: bool = False,
    ) -> list[MabangOnlineProductShop]:
        """
        获取“在线商品匹配 > 拉取商品”弹窗内的店铺列表。

        :param platform_id: 平台ID，Shopee 为 17，ShopeeCNSC 为 -1
        :param include_unavailable: 是否返回授权失效/正在拉取等不可提交的店铺
        """
        normalized_platform_id = _safe_text(platform_id) or "17"
        endpoint = (
            MATCHING_PRODUCT_CNSC_SHOP_LIST_PATH
            if normalized_platform_id == "-1"
            else MATCHING_PRODUCT_SHOP_LIST_PATH
        )
        payload = self._post_matching_product_json(
            endpoint,
            data={},
            context="在线商品拉取店铺列表",
            retries=2,
        )
        raw_data = payload.get("data") or {}
        if not isinstance(raw_data, dict):
            raise MabangApiError(f"在线商品拉取店铺列表结构异常: {payload}")

        if normalized_platform_id == "-1":
            shop_mapping = raw_data
        else:
            shop_mapping = raw_data.get(normalized_platform_id) or {}
        if not isinstance(shop_mapping, dict):
            raise MabangApiError(f"在线商品拉取平台店铺结构异常 platformId={normalized_platform_id}: {payload}")

        shops: list[MabangOnlineProductShop] = []
        for shop_id, shop_data in shop_mapping.items():
            if not isinstance(shop_data, dict):
                continue
            shop = self._build_online_product_shop(
                shop_id=_safe_text(shop_id),
                platform_id=normalized_platform_id,
                data=shop_data,
            )
            if include_unavailable or shop.available:
                shops.append(shop)
        return shops

    def find_online_product_pull_shop(
            self,
            *,
            platform_id: str = "17",
            shop_keyword: str,
            include_unavailable: bool = False,
    ) -> MabangOnlineProductShop:
        """
        按店铺名称定位“拉取商品”可用店铺。

        优先精确匹配，其次前缀匹配，最后包含匹配；若命中多个店铺会抛出异常，避免每日任务拉错店。
        """
        keyword = _safe_text(shop_keyword)
        if not keyword:
            raise MabangApiError("在线商品拉取失败：店铺关键词为空")

        shops = self.list_online_product_pull_shops(
            platform_id=platform_id,
            include_unavailable=include_unavailable,
        )
        exact_matches = [shop for shop in shops if shop.shop_name == keyword or shop.shop_id == keyword]
        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(exact_matches) > 1:
            raise MabangApiError(f"在线商品拉取失败：店铺精确匹配到多个结果 {keyword}")

        prefix_matches = [shop for shop in shops if shop.shop_name.startswith(keyword)]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
        if len(prefix_matches) > 1:
            names = "；".join(f"{shop.shop_name}({shop.shop_id})" for shop in prefix_matches[:10])
            raise MabangApiError(f"在线商品拉取失败：店铺前缀匹配到多个结果 {keyword}: {names}")

        contains_matches = [shop for shop in shops if keyword in shop.shop_name]
        if len(contains_matches) == 1:
            return contains_matches[0]
        if len(contains_matches) > 1:
            names = "；".join(f"{shop.shop_name}({shop.shop_id})" for shop in contains_matches[:10])
            raise MabangApiError(f"在线商品拉取失败：店铺包含匹配到多个结果 {keyword}: {names}")

        available_hint = "" if include_unavailable else "可用"
        raise MabangApiError(f"在线商品拉取失败：未找到{available_hint}店铺 {keyword}")

    def pull_online_products(
            self,
            *,
            platform_id: str = "17",
            shop_ids: Sequence[str],
            raise_on_failed_shops: bool = True,
    ) -> dict[str, Any]:
        """
        提交“立即下载”同步在线商品任务。

        :param platform_id: 平台ID，Shopee 为 17，ShopeeCNSC 为 -1
        :param shop_ids: 店铺ID列表
        :param raise_on_failed_shops: 接口返回失败店铺明细时是否抛错
        """
        normalized_platform_id = _safe_text(platform_id) or "17"
        normalized_shop_ids = list(dict.fromkeys(_safe_text(value) for value in shop_ids if _safe_text(value)))
        if not normalized_shop_ids:
            raise MabangApiError("在线商品拉取失败：店铺ID为空")

        payload = self._post_matching_product_json(
            MATCHING_PRODUCT_PULL_PATH,
            data={
                "platformId": normalized_platform_id,
                "shopId": ",".join(normalized_shop_ids) + ",",
            },
            context="在线商品立即下载同步提交",
            retries=2,
        )
        payload["requestedShopIds"] = normalized_shop_ids
        payload["platformId"] = normalized_platform_id

        failed_rows = payload.get("html") or []
        if raise_on_failed_shops and failed_rows:
            failure_messages: list[str] = []
            if isinstance(failed_rows, list):
                for row in failed_rows:
                    if not isinstance(row, dict):
                        continue
                    shop_name = _safe_text(row.get("shopname")) or "-"
                    description = _safe_text(row.get("description")) or "-"
                    failure_messages.append(f"{shop_name}: {description}")
            message = "；".join(failure_messages) or _safe_text(payload.get("message")) or str(payload)
            raise MabangApiError(f"在线商品拉取存在失败店铺: {message}")
        return payload

    def pull_online_products_by_shop_name(
            self,
            *,
            platform_id: str = "17",
            shop_keyword: str,
            include_unavailable: bool = False,
            raise_on_failed_shops: bool = True,
    ) -> dict[str, Any]:
        """
        按店铺名提交“拉取在线商品”任务，并在返回值中带上解析出的店铺信息。
        """
        shop = self.find_online_product_pull_shop(
            platform_id=platform_id,
            shop_keyword=shop_keyword,
            include_unavailable=include_unavailable,
        )
        if not shop.available:
            raise MabangApiError(
                f"在线商品拉取失败：店铺不可下载 {shop.shop_name}({shop.shop_id})，状态: {shop.error or shop.status}"
            )
        payload = self.pull_online_products(
            platform_id=platform_id,
            shop_ids=[shop.shop_id],
            raise_on_failed_shops=raise_on_failed_shops,
        )
        payload["shop"] = {
            "shopId": shop.shop_id,
            "shopName": shop.shop_name,
            "lastRunTime": shop.last_run_time,
            "lastRunTimezone": shop.last_run_timezone,
            "status": shop.status,
            "error": shop.error,
        }
        return payload

    def export_sales_report(
            self,
            *,
            url: str,
            params: dict[str, Any] | None,
            form_data: dict[str, Any],
            output_path: str | Path,
            referer: str | None = None,
    ) -> Path:
        """
        导出销售报表

        :param url: 报表接口地址
        :param params: URL参数
        :param form_data: POST表单数据
        :param output_path: 输出路径
        :param referer: 来源页
        """
        from util.mabang_html_parse import parse_table_reports_countryReports

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        request_form = dict(form_data)
        total_pages = self._get_sales_report_total_pages(url=url, params=params, form_data=request_form,
                                                         referer=referer)

        seen_skus: set[str] = set()
        workbook = Workbook(write_only=True)
        temporary_output: Path | None = None
        try:
            worksheet = workbook.create_sheet("Sheet1")
            worksheet.append(list(SALES_REPORT_OUTPUT_HEADERS))
            for page in range(1, total_pages + 1):
                request_form["page"] = str(page)
                payload = self._read_sales_report_page(url=url, params=params, form_data=request_form, referer=referer)
                if not payload.get("success"):
                    raise MabangApiError(f"销售报表页面查询失败: page={page}")
                for item in parse_table_reports_countryReports(payload.get("tableContent") or ""):
                    sku = _safe_text(item.get("stock_code"))
                    if not sku or sku in seen_skus:
                        continue
                    seen_skus.add(sku)
                    worksheet.append(
                        [
                            sku,
                            _safe_text(item.get("product_title")),
                            _safe_text(item.get("defaultCost")),
                            _safe_text(item.get("weight")),
                            _safe_text(item.get("timeCreated")),
                            _safe_text(item.get("quantity") or 0),
                        ]
                    )
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp.xlsx", dir=output.parent)
            temporary_output = Path(temporary_name)
            os.close(descriptor)
            workbook.save(temporary_output)
            os.replace(temporary_output, output)
            return output
        except BaseException:
            # Workbook.close() alone does not close write-only worksheet generators.
            # Release their XML streams before removing their Windows temp files.
            self._discard_write_only_workbook(workbook)
            if temporary_output is not None:
                try:
                    temporary_output.unlink(missing_ok=True)
                except Exception as cleanup_error:
                    LOGGER.warning("销售报表不完整输出清理失败: type=%s", type(cleanup_error).__name__)
            raise
        finally:
            try:
                workbook.close()
            except Exception as cleanup_error:
                LOGGER.warning("销售报表工作簿关闭失败: type=%s", type(cleanup_error).__name__)

    def download_product_sales_report_csv(
            self,
            *,
            url: str,
            params: dict[str, Any] | None,
            form_data: Any,
            report_type: str | None = None,
            platform_or_shop: str | None = None,
            referer: str | None = None,
            fields: Sequence[str] | None = None,
            init_rows_per_page: int = DEFAULT_SALES_REPORT_EXPORT_INIT_ROWS_PER_PAGE,
    ) -> MabangExportFile:
        """
        通过马帮“按商品导出全部”接口下载销量报表 CSV。

        马帮该接口依赖服务端最近一次报表搜索条件，因此这里会先 POST 一次报表查询接口初始化上下文，
        再 GET ``reports.exportProductAll`` 下载 CSV。
        """
        normalized_report_type = _resolve_sales_report_type(report_type=report_type, params=params, url=url)
        request_form = _clone_form_payload(form_data)
        request_form = _set_form_payload_value(request_form, "page", "1")
        request_form = _set_form_payload_value(
            request_form,
            "rowsPerPage",
            str(max(1, int(init_rows_per_page or DEFAULT_SALES_REPORT_EXPORT_INIT_ROWS_PER_PAGE))),
        )

        effective_platform_or_shop = (
                _safe_text(platform_or_shop)
                or _safe_text(_get_form_payload_first(request_form, "platformOrShop"))
                or SALES_REPORT_PLATFORM_OR_SHOP_DEFAULTS.get(normalized_report_type, "")
        )
        if not effective_platform_or_shop:
            raise MabangApiError(f"销售报表导出失败: 无法确定 platformOrShop report_type={normalized_report_type}")

        referer_url = referer or self._url(SALES_REPORT_PAGE_PATHS.get(normalized_report_type, "/index.php"))
        search_payload = self._post_sales_report(
            url=url,
            params=params,
            form_data=request_form,
            referer=referer_url,
        )
        if not search_payload.get("success"):
            raise MabangApiError(f"销售报表导出搜索初始化失败: {search_payload}")

        selected_fields = list(fields or DEFAULT_SALES_REPORT_EXPORT_FIELDS)
        export_params: list[tuple[str, str]] = [
            ("mod", "reports.exportProductAll"),
            ("historyType", _safe_text(_get_form_payload_first(request_form, "historyType")) or "1"),
            ("types", normalized_report_type),
            ("platformOrShop", effective_platform_or_shop),
        ]
        export_params.extend(("field[]", _safe_text(field_name)) for field_name in selected_fields if _safe_text(field_name))
        export_params.append(("isSameType", _safe_text(_get_form_payload_first(request_form, "isSameType")) or "0"))
        if _safe_text(_get_form_payload_first(request_form, "isSame")) in {"1", "true", "True"}:
            export_params.append(("isSame", "1"))

        export_url = f"{self.base_url}/index.php?{urlencode(export_params, doseq=True)}"
        if os.environ.get("MABANG_SALES_REPORT_DOWNLOAD_MODE", "").strip().lower() == "stream":
            content = bytearray()
            file_name = ""
            response_url = export_url
            idle_timeout_seconds = float(os.environ.get("MABANG_SALES_REPORT_STREAM_IDLE_TIMEOUT", "30") or "30")
            max_seconds = float(os.environ.get("MABANG_SALES_REPORT_STREAM_MAX_SECONDS", "600") or "600")
            stream_timeout = httpx.Timeout(
                connect=DEFAULT_CONNECT_TIMEOUT_SECONDS,
                read=idle_timeout_seconds,
                write=DEFAULT_WRITE_TIMEOUT_SECONDS,
                pool=DEFAULT_POOL_TIMEOUT_SECONDS,
            )
            deadline = time.monotonic() + max_seconds
            try:
                with self.client.stream(
                    "GET",
                    export_url,
                    headers={"Referer": referer_url},
                    timeout=stream_timeout,
                ) as response:
                    response.raise_for_status()
                    response_url = str(response.url)
                    file_name = _guess_download_filename(
                        response.headers.get("Content-Disposition"),
                        response_url,
                    )
                    for chunk in response.iter_bytes():
                        if chunk:
                            content.extend(chunk)
                        if time.monotonic() >= deadline:
                            LOGGER.warning("销售报表流式下载达到最大等待时间，已收到 %s bytes", len(content))
                            break
            except httpx.ReadTimeout:
                if not content:
                    raise
                LOGGER.warning("销售报表流式下载空闲超时，按已收到内容收尾: %s bytes", len(content))
            if not content:
                raise MabangApiError("销售报表流式下载未收到任何内容")
            raw_content = bytes(content)
            last_newline = max(raw_content.rfind(b"\n"), raw_content.rfind(b"\r"))
            if 0 < last_newline < len(raw_content) - 1:
                raw_content = raw_content[: last_newline + 1]
            if not file_name:
                file_name = _guess_download_filename(None, response_url)
            file_type = _safe_text(Path(file_name).suffix).lower().lstrip(".") or "csv"
        else:
            response = self.client.get(export_url, headers={"Referer": referer_url}, timeout=self.timeout_seconds)
            response.raise_for_status()
            file_name = _guess_download_filename(response.headers.get("Content-Disposition"), str(response.url))
            file_type = _safe_text(Path(file_name).suffix).lower().lstrip(".") or "csv"
            raw_content = response.content
        return MabangExportFile(
            file_name=file_name,
            file_type=file_type,
            file_url=response_url if os.environ.get("MABANG_SALES_REPORT_DOWNLOAD_MODE", "").strip().lower() == "stream" else str(response.url),
            content=raw_content,
            metadata={
                "reportType": normalized_report_type,
                "platformOrShop": effective_platform_or_shop,
                "fieldCodes": selected_fields,
                "firstPageHasRows": bool(_safe_text(search_payload.get("tableContent"))),
                "updateDateStr": _safe_text(search_payload.get("updateDateStr")),
            },
        )

    def export_sales_report_by_product_all(
            self,
            *,
            url: str,
            params: dict[str, Any] | None,
            form_data: Any,
            output_path: str | Path,
            report_type: str | None = None,
            platform_or_shop: str | None = None,
            referer: str | None = None,
            fields: Sequence[str] | None = None,
            init_rows_per_page: int = DEFAULT_SALES_REPORT_EXPORT_INIT_ROWS_PER_PAGE,
            fallback_to_pagination: bool = True,
            preserve_export_columns: bool = False,
    ) -> Path:
        """
        使用“按商品导出全部”CSV接口导出销量报表，并保存为兼容旧脚本的 Excel。

        默认输出列保持为 ``SALES_REPORT_OUTPUT_HEADERS``，便于替换旧的分页抓取函数。
        ``preserve_export_columns=True`` 时保留导出接口返回的全部表头和明细行，
        不按 SKU 去重，适合需要与马帮“按商品导出全部”文件保持一致的流程。
        """
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            export_file = self.download_product_sales_report_csv(
                url=url,
                params=params,
                form_data=form_data,
                report_type=report_type,
                platform_or_shop=platform_or_shop,
                referer=referer,
                fields=fields,
                init_rows_per_page=init_rows_per_page,
            )
            if preserve_export_columns:
                headers, rows = _parse_product_sales_report_csv_table(export_file.content)
            else:
                headers = list(SALES_REPORT_OUTPUT_HEADERS)
                rows = _parse_product_sales_report_csv_rows(export_file.content)
            if not rows and export_file.metadata.get("firstPageHasRows"):
                raise MabangApiError("按商品导出全部 CSV 结果为空，但搜索页已返回数据")
            _write_sales_report_rows_to_xlsx(rows, output, headers=headers)
            return output
        except Exception as exc:
            if not fallback_to_pagination:
                raise
            LOGGER.warning("按商品导出全部接口失败，退回分页抓取: %s", exc)
            return self.export_sales_report(
                url=url,
                params=params,
                form_data=dict(form_data) if hasattr(form_data, "items") else form_data,
                output_path=output,
                referer=referer,
            )

    # ===== 内部私有方法 =====
    @staticmethod
    def _discard_write_only_workbook(workbook: Workbook) -> None:
        """Dispose only this failed export's write-only streams without masking its error."""
        for worksheet in workbook.worksheets:
            try:
                if not worksheet.closed:
                    worksheet.close()
            except Exception as cleanup_error:
                LOGGER.warning("导出工作表关闭失败: type=%s", type(cleanup_error).__name__)
            # Also close the generators independently if worksheet.close() failed midway.
            rows = getattr(worksheet, "_rows", None)
            writer = getattr(worksheet, "_writer", None)
            for stream in (rows, writer):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception as cleanup_error:
                        LOGGER.warning("导出写入流关闭失败: type=%s", type(cleanup_error).__name__)
            if writer is not None:
                try:
                    if Path(writer.out).exists():
                        writer.cleanup()
                except Exception as cleanup_error:
                    LOGGER.warning("导出临时文件清理失败: type=%s", type(cleanup_error).__name__)

    def _read_sales_report_page(
            self,
            *,
            url: str,
            params: dict[str, Any] | None,
            form_data: dict[str, Any],
            referer: str | None,
    ) -> dict[str, Any]:
        """Retry read interruptions only for the pagination export's report queries."""
        page = int(form_data["page"])
        for attempt in range(1, 4):
            LOGGER.info("销售报表分页读取: page=%s, attempt=%s/3", page, attempt)
            try:
                return self._post_sales_report(url=url, params=params, form_data=form_data, referer=referer)
            except (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                LOGGER.warning(
                    "销售报表分页读取%s: page=%s, attempt=%s/3, type=%s",
                    "重试已耗尽" if attempt == 3 else "将重试",
                    page, attempt, type(exc).__name__,
                )
                if attempt == 3:
                    raise
                time.sleep(float(attempt))

    def _post_sales_report(
            self,
            *,
            url: str,
            params: dict[str, Any] | None,
            form_data: Any,
            referer: str | None,
    ) -> dict[str, Any]:
        """发起销售报表请求"""
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if referer:
            headers["Referer"] = referer
        if hasattr(form_data, "items"):
            response = self.client.post(url, params=params or {}, data=form_data, headers=headers)
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            response = self.client.post(
                url,
                params=params or {},
                content=urlencode(list(form_data or []), doseq=True),
                headers=headers,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise MabangApiError("销售报表返回无效的JSON对象")
        return payload

    def _get_sales_report_total_pages(
            self,
            *,
            url: str,
            params: dict[str, Any] | None,
            form_data: dict[str, Any],
            referer: str | None,
    ) -> int:
        """获取销售报表总页数"""
        form_data["page"] = "1"
        payload = self._read_sales_report_page(url=url, params=params, form_data=form_data, referer=referer)
        if not payload.get("success"):
            raise MabangApiError("销售报表总页数查询失败: page=1")
        page_html = _safe_text(payload.get("pageHtml"))
        total_pages_match = re.search(r'<span class="semibold">\d+/(\d+)</span>页', page_html)
        if total_pages_match:
            return max(1, int(total_pages_match.group(1)))
        if payload.get("tableContent"):
            return 1
        return 0

    def _lookup_chunk(self, chunk: list[str], username: str, password: str, worker_index: int) -> dict[
        str, MabangLookupResult]:
        """并发查询时的工作线程方法"""
        chunk_results: dict[str, MabangLookupResult] = {}
        with MabangClient(self.base_url, timeout_seconds=self.timeout_seconds, verify_ssl=self.verify_ssl) as client:
            client.login(username, password)
            batch_results = client.search_orders_by_tracking_numbers(chunk)
            for item_index, tracking_number in enumerate(chunk, start=1):
                LOGGER.info("马帮查询 [线程 %s] %s/%s: %s", worker_index, item_index, len(chunk),
                            tracking_number)
                try:
                    order = batch_results.get(tracking_number) or client.search_order_by_tracking_number(
                        tracking_number)
                    return_records: list[MabangReturnRecord] = []
                    refund_records: list[MabangRefundRecord] = []
                    if order is not None:
                        return_records = client.search_returns_by_tracking_number(tracking_number)
                        if order.platform_order_id:
                            refund_records = client.search_refunds_by_platform_order_id(order.platform_order_id)
                    chunk_results[tracking_number] = MabangLookupResult(tracking_number=tracking_number, order=order,
                                                                        return_records=return_records,
                                                                        refund_records=refund_records)
                except Exception as exc:  # pragma: no cover - live request guard
                    chunk_results[tracking_number] = MabangLookupResult(tracking_number=tracking_number,
                                                                        error_message=str(exc))
        return chunk_results

    def _build_order_info(self, row: dict[str, Any], *, fallback_tracking_number: str = "") -> MabangOrderInfo:
        """构建订单信息数据对象"""
        return MabangOrderInfo(
            tracking_number=_safe_text(row.get("trackNumber")) or fallback_tracking_number,
            platform_order_id=_safe_text(row.get("platformOrderId")),
            shop_name=_safe_text(row.get("shopName") or row.get("shopIdText") or row.get("shopOwner")),
            platform_name=_safe_text(row.get("platformName") or row.get("platformDesc") or row.get("platformIdText")),
            show_order_status_text=_safe_text(row.get("showOrderStatusText")),
            platform_order_status=_safe_text(row.get("platform_order_status")),
            order_status=_safe_text(row.get("orderStatus")),
            refund_flag=_safe_text(row.get("isRefund")),
            created_at=_safe_text(row.get("createDate") or row.get("createdAt")),
            express_time=_safe_text(row.get("expressTime")),
            origin_transport_time=_safe_text(row.get("originTransportTime") or row.get("transportTime")),
            order_deliver_time=_safe_text(row.get("orderDeliverTime") or row.get("deliverRealTime")),
            raw_data=dict(row),
        )

    def _build_online_product_shop(
            self,
            *,
            shop_id: str,
            platform_id: str,
            data: dict[str, Any],
    ) -> MabangOnlineProductShop:
        """构建在线商品拉取店铺对象，并按页面规则判断是否可提交。"""
        status = _safe_text(data.get("status"))
        token_flag = _safe_text(data.get("tokenflag") or data.get("tokenFlag"))
        is_running = _safe_text(data.get("isRunning"))
        error = _safe_text(data.get("error"))
        unavailable_reasons: list[str] = []
        if status and status != "1":
            unavailable_reasons.append(f"店铺状态不可用({status})")
        if token_flag and token_flag != "1":
            unavailable_reasons.append("店铺授权失效")
        if is_running in {"2", "6"}:
            unavailable_reasons.append(error or _safe_text(data.get("tag")) or "店铺正在拉取/暂不可拉取")
        available = not unavailable_reasons
        return MabangOnlineProductShop(
            shop_id=_safe_text(shop_id),
            shop_name=_safe_text(data.get("name")),
            platform_id=_safe_text(platform_id),
            status=status,
            token_flag=token_flag,
            is_running=is_running,
            last_run_time=_safe_text(data.get("lastRunTime")),
            last_run_timezone=_safe_text(data.get("lastRunTimezone")),
            error="；".join(unavailable_reasons) if unavailable_reasons else (error or "店铺可以下载"),
            available=available,
            raw_data=dict(data),
        )

    def _build_order_payload_base(self, *, rows_per_page: int = DEFAULT_ORDER_PAGE_SIZE, page: int = 1) -> dict[
        str, str]:
        """生成订单查询基础参数"""
        return {"orderPageKey": self._order_page_key or "", "goPaypalRefundStatus": "1", "page": str(page),
                "rowsPerPage": str(rows_per_page), "Order_isCloud": "2", "m": "order", "a": "orderalllist",
                "isNewOrderPage": "1", "post_tableBase": "2", "tabId": self._order_tab_id or "222",
                "Order.orderStatus": "", "startPageNum": "", "endPageNum": ""}

    def _build_inventory_search_payload(
            self,
            *,
            warehouse_id: str,
            skus: Sequence[str],
            status: str = "",
            rows_per_page: int = 50,
    ) -> dict[str, str]:
        """生成库存查询表单数据"""
        return {
            "stockOrderby": "",
            "search-content": "stocksku",
            "search-content-text1": "",
            "status": status,
            "livenessType": "",
            "isNewType": "",
            "showstart": "1",
            "timeField": "updateDate2",
            "warehouseMold": "all",
            "warehouseIdStr": "",
            "gridcodeStr": "",
            "stockSkuStr": "\r\n".join(skus),
            "page": "1",
            "rowsPerPage": str(rows_per_page),
            "warehouseId": warehouse_id,
            "stockQuantitynum": "",
            "stockWarningQuantitynum": "",
            "saleAvailableDaysnum": "",
            "startTime": "",
            "endTime": "",
            "isIdn": "1",
            "warehouseIdArr": warehouse_id,
            "warehouseIds[]": warehouse_id,
        }

    def _download_realtime_inventory_page(
            self, *, page: int, show_rmb_column: bool,
            select_warehouse: Callable[[], None],
            get_search_token: Callable[[], str],
    ) -> bytes:
        """下载同一库存页：刷新并重选仓一次，仍失败则单次重登后重选。"""
        params = {"flag": "1", "showRmbColumn": "1" if show_rmb_column else "0"}
        if page > 1:
            params["page"] = str(page)
        last_error = ""
        for attempt in range(1, 4):
            if attempt == 3:
                if not (self._login_username and self._login_password):
                    last_error += "；没有可用于恢复会话的凭据"
                    break
                # A generic ERP error page does not prove session expiry. This
                # is one bounded recovery attempt, never a replay of a write.
                LOGGER.warning("实时库存导出刷新后仍失败，尝试单次重新登录 page=%s", page)
                login_failed = False
                try:
                    self.login(self._login_username, self._login_password)
                except Exception as exc:
                    last_error = f"重新登录失败 error_type={type(exc).__name__}"
                    login_failed = True
                if login_failed:
                    break
            try:
                self._ensure_inventory_detail_context()
                if page == 1 or attempt > 1:
                    # The ERP stores this filter in the session, not the GET
                    # URL. Refresh/relogin must never export the default scope.
                    select_warehouse()
                # The current ERP binds every exported page to the most recent
                # search response. Re-read after refresh/relogin re-selection.
                request_params = dict(params)
                search_token = get_search_token()
                if search_token:
                    request_params["searchToken"] = search_token
                export_url = f"{self._inventory_url(INVENTORY_EXPORT_URL)}&{urlencode(request_params)}"
                response = self.client.get(
                    export_url,
                    headers={"Referer": self._inventory_iframe_url or self._url(INVENTORY_DETAIL_PATH)},
                )
                _raise_if_session_expired_response(response, context=f"实时库存导出 page={page}")
                _raise_for_status_safely(response, context=f"实时库存导出 page={page}")
                content_type = _safe_text(response.headers.get("Content-Type")).lower()
                if "html" in content_type or not response.content.startswith(b"PK\x03\x04"):
                    raise MabangApiError(
                        f"实时库存导出返回非Excel数据: status={response.status_code}, "
                        f"content_type={content_type or '-'}, final_url={_safe_url_for_log(response.url)}"
                    )
                return response.content
            except (MabangApiError, httpx.HTTPError) as exc:
                last_error = str(exc) if isinstance(exc, MabangApiError) else type(exc).__name__
            if attempt < 3:
                LOGGER.warning(
                    "实时库存导出失败，将重建上下文并重新选择原仓库 page=%s attempt=%s/3 error=%s",
                    page, attempt, last_error,
                )
                self._inventory_detail_bootstrapped = False
                self._inventory_iframe_url = None
                time.sleep(_compute_retry_sleep_seconds(attempt, cap=3.0))
        # Do not retain response bodies or raw HTTP exceptions in the chain.
        self._inventory_detail_bootstrapped = False
        self._inventory_iframe_url = None
        LOGGER.error("实时库存导出恢复失败 page=%s error=%s", page, last_error)
        raise MabangApiError(f"实时库存导出下载失败 page={page}: {last_error}")

    def _find_stock_label_id(self, label_name: str) -> str:
        """查找库存标签对应的内部ID"""
        label_text = _safe_text(label_name)
        if not label_text:
            raise MabangApiError("库存名称未找到对应ID")
        _, page_html = self._load_stock_list_iframe_html()
        for match in re.finditer(
                r'<li[^>]+data-id="([^"]+)"[^>]*>\s*<a[^>]*>\s*([^<]+?)\s*</a>',
                page_html,
                flags=re.IGNORECASE,
        ):
            label_id, parsed_label = match.groups()
            if html.unescape(parsed_label).strip() == label_text:
                return _safe_text(label_id)
        raise MabangApiError(f"查询ID失败: 未找到库存名称{label_text}")

    def _load_stock_list_iframe_html(self) -> tuple[str, str]:
        """加载库存列表iframe页；会话失效时只重新登录并重放一次。"""
        return self._run_replay_safe_authenticated_read(
            lambda: self._load_iframe_html(STOCK_LIST_PAGE_PATH),
            context="加载库存列表页面",
        )

    def _get_stock_list_page_response_with_retry(
            self, url: str, *, headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """库存页面每次 GET 最多尝试三次；认证重放时重新计算此读取预算。"""
        for attempt in range(1, 4):
            try:
                # Client.get reads the complete response body before returning.
                return self.client.get(url, headers=headers)
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout) as exc:
                if attempt == 3:
                    LOGGER.warning(
                        "库存列表页面 GET 响应读取失败，attempt=%s/3 error_type=%s，重试已耗尽",
                        attempt, type(exc).__name__,
                    )
                    raise
                delay = _compute_retry_sleep_seconds(attempt, cap=2.0)
                LOGGER.warning(
                    "库存列表页面 GET 响应读取中断，attempt=%s/3 error_type=%s，%.1f 秒后重试",
                    attempt, type(exc).__name__, delay,
                )
                time.sleep(delay)

    def _load_iframe_html(self, page_path: str) -> tuple[str, str]:
        """加载带有iframe内容的页面并返回其实际URL和HTML内容"""
        self._ensure_logged_in()
        safe_page_path = urlparse(page_path).path or "/"
        request_get = (
            self._get_stock_list_page_response_with_retry
            if page_path == STOCK_LIST_PAGE_PATH else self.client.get
        )
        page_response = request_get(self._url(page_path))
        page_html = _decode_html_page(page_response)
        _raise_if_session_expired_response(
            page_response,
            context=f"加载页面 {safe_page_path}",
            response_text=page_html,
        )
        _raise_for_status_safely(page_response, context=f"加载页面 {safe_page_path}")

        soup = BeautifulSoup(page_html, "html.parser")
        iframe = soup.find("iframe", id="iframeContent")
        if iframe is None or not _safe_text(iframe.get("src")):
            iframe = soup.find("iframe", src=True)
        iframe_src = _safe_text(iframe.get("src") if iframe is not None else "")
        if iframe_src.lower().startswith(("about:", "javascript:", "data:")):
            iframe_src = ""
        if not iframe_src:
            if _looks_like_direct_iframe_content(page_path, page_html):
                return str(page_response.url), page_html
            raise MabangApiError(
                "无法定位iframe地址: "
                f"path={safe_page_path}, status={page_response.status_code}, "
                f"final_url={_safe_url_for_log(page_response.url)}"
            )
        iframe_url = urljoin(str(page_response.url), html.unescape(iframe_src))
        iframe_response = request_get(iframe_url, headers={"Referer": str(page_response.url)})
        iframe_html = _decode_html_page(iframe_response)
        _raise_if_session_expired_response(
            iframe_response,
            context=f"加载页面iframe {safe_page_path}",
            response_text=iframe_html,
        )
        _raise_for_status_safely(iframe_response, context=f"加载页面iframe {safe_page_path}")
        return str(iframe_response.url), iframe_html

    def _load_stock_export_template(self, *, referer: str) -> tuple[str, dict[str, str], dict[str, str]]:
        """加载并解析库存导出的表单模板"""
        export_page_url, export_html = self._open_export_template_page(
            entry_payload=[
                ("mod", "export.exportTemplate"),
                ("data", ""),
                ("type", "2"),
                ("menu", "stock"),
                ("exportUrl", self._url(STOCK_EXPORT_ACTION_PATH)),
            ],
            referer=referer,
        )
        form_defaults = _extract_hidden_form_values(export_html, form_id="theform")
        field_map = _extract_export_field_map(export_html)
        if not field_map:
            raise MabangApiError(f"库存导出字段列表解析失败: {export_page_url}")
        form_defaults.setdefault("templateId", "0")
        form_defaults.setdefault("templateName", "")
        form_defaults.setdefault("datasOpen", "1")
        form_defaults.setdefault("pageSave", "1")
        form_defaults.setdefault("operateType", "3")
        return export_page_url, form_defaults, field_map

    def _open_export_template_page(
            self,
            *,
            entry_payload: Sequence[tuple[str, str]],
            referer: str,
    ) -> tuple[str, str]:
        """打开导出配置面板弹窗页面"""
        response = self.client.post(
            self._url(EXPORT_ENTRY_PATH),
            content=urlencode(list(entry_payload), doseq=True),
            headers={"Referer": referer, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        response.raise_for_status()
        page_url = str(response.url)
        page_html = _decode_html_page(response)
        if 'id="theform"' in page_html:
            return page_url, page_html

        iframe_src = _extract_first(r'<iframe[^>]+src="([^"]+)"', page_html)
        if not iframe_src:
            raise MabangApiError(f"无法定位导出模板iframe: {page_url}")
        iframe_url = urljoin(page_url, html.unescape(iframe_src))
        iframe_response = self.client.get(iframe_url, headers={"Referer": page_url})
        iframe_response.raise_for_status()
        iframe_html = _decode_html_page(iframe_response)
        if 'id="theform"' not in iframe_html:
            raise MabangApiError(f"无法加载导出模板表单: {iframe_url}")
        return str(iframe_response.url), iframe_html

    def _export_stock_sku_chunk(
            self,
            *,
            skus: Sequence[str],
            field_codes: Sequence[str],
            field_map: dict[str, str],
            form_defaults: dict[str, str],
            export_page_url: str,
    ) -> MabangExportFile:
        """执行某一SKU批次的实际导出动作"""
        payload: dict[str, Any] = dict(form_defaults)
        payload["orderIds"] = "\n".join(skus)
        payload["fieldlabel"] = list(field_codes)
        payload["map-uq[]"] = list(field_codes)
        payload["map-name[]"] = [field_map.get(code, code) for code in field_codes]
        payload["map-text[]"] = ["" for _ in field_codes]
        payload["tableBase"] = ""
        payload["isMerage"] = "2"
        payload["version"] = "v2"
        payload["step"] = "1"

        step1_data = self._post_generic_export_action(
            STOCK_EXPORT_ACTION_PATH,
            payload,
            referer=export_page_url,
            retries=DEFAULT_ORDER_EXPORT_STEP_RETRIES,
        )
        if not step1_data.get("success"):
            raise MabangApiError(f"库存导出失败: {step1_data}")
        export_data = self._resolve_generic_export_v2(
            STOCK_EXPORT_ACTION_PATH,
            step1_data,
            referer=export_page_url,
            step_retries=DEFAULT_ORDER_EXPORT_STEP_RETRIES,
            poll_interval_seconds=DEFAULT_ORDER_EXPORT_POLL_INTERVAL_SECONDS,
            poll_timeout_seconds=DEFAULT_ORDER_EXPORT_POLL_TIMEOUT_SECONDS,
        )
        file_url = _safe_text(export_data.get("file_url") or export_data.get("gourl"))
        if not file_url:
            raise MabangApiError(f"未找到对应文件地址: {export_data}")
        file_name, file_type, content = self._download_export_file_from_url(file_url, referer=export_page_url)
        return MabangExportFile(
            file_name=file_name,
            file_type=file_type,
            file_url=file_url,
            content=content,
            metadata={"fieldCodes": list(field_codes), "skuCount": len(skus),
                      **{str(k): v for k, v in export_data.items()}},
        )

    def _post_generic_export_action(
            self,
            action_path: str,
            payload: dict[str, Any],
            *,
            referer: str,
            retries: int,
    ) -> dict[str, Any]:
        """通用的导出任务POST请求"""
        endpoint = self._url(action_path)
        retry_count = max(1, int(retries))
        last_error: Exception | None = None
        for attempt in range(1, retry_count + 1):
            try:
                response = self.client.post(
                    endpoint,
                    content=urlencode(_flatten_form_payload(payload), doseq=True),
                    headers={
                        "Referer": referer,
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise MabangApiError(f"导出异常: {data}")
                return data
            except Exception as exc:
                last_error = exc
                if attempt >= retry_count:
                    break
                time.sleep(_compute_retry_sleep_seconds(attempt, cap=DEFAULT_RETRY_BACKOFF_CAP_SECONDS))
        raise MabangApiError(f"导出请求失败: {last_error}") from last_error

    def _resolve_generic_export_v2(
            self,
            action_path: str,
            step1_data: dict[str, Any],
            *,
            referer: str,
            step_retries: int,
            poll_interval_seconds: float,
            poll_timeout_seconds: float,
    ) -> dict[str, Any]:
        """处理版本2的导出分片和轮询合并逻辑"""
        if _safe_text(step1_data.get("file_url") or step1_data.get("gourl")):
            return step1_data
        sn = _safe_text(step1_data.get("sn"))
        try:
            subtask_num = int(step1_data.get("subtask_num") or 0)
        except (TypeError, ValueError):
            subtask_num = 0
        if not sn or subtask_num <= 0:
            raise MabangApiError(f"库存导出返回无效数据: {step1_data}")

        for sub_no in range(1, subtask_num + 1):
            subtask_ok = False
            for attempt in range(1, max(1, int(step_retries)) + 1):
                step2_data = self._post_generic_export_action(
                    action_path,
                    {"tableBase": "", "isMerage": "2", "version": "v2", "sn": sn, "sub_no": str(sub_no), "step": "2",
                     "1": "1"},
                    referer=referer,
                    retries=1,
                )
                message = _safe_text(step2_data.get("message"))
                if step2_data.get("success") and "处理中" not in message:
                    subtask_ok = True
                    break
                time.sleep(_compute_retry_sleep_seconds(attempt, cap=3.0))
            if not subtask_ok:
                raise MabangApiError(f"export step2 failed: sn={sn}, sub_no={sub_no}")

        step3_data: dict[str, Any] = {}
        task_id = ""
        for attempt in range(1, max(1, int(step_retries)) + 1):
            step3_data = self._post_generic_export_action(
                action_path,
                {"tableBase": "", "isMerage": "2", "version": "v2", "sn": sn, "step": "3", "1": "1"},
                referer=referer,
                retries=1,
            )
            task_id = _safe_text(step3_data.get("taskId") or step3_data.get("taskid"))
            if step3_data.get("success") and task_id:
                break
            time.sleep(_compute_retry_sleep_seconds(attempt, cap=3.0))
        if not task_id:
            raise MabangApiError(f"库存导出失败: {step3_data}")

        deadline = time.time() + max(5.0, float(poll_timeout_seconds))
        interval = max(0.1, float(poll_interval_seconds))
        while time.time() < deadline:
            step4_data = self._post_generic_export_action(
                action_path,
                {"tableBase": "", "isMerage": "2", "version": "v2", "sn": sn, "step": "4", "taskId": task_id, "1": "1"},
                referer=referer,
                retries=1,
            )
            if step4_data.get("success") and _safe_text(step4_data.get("file_url") or step4_data.get("gourl")):
                return step4_data
            time.sleep(interval)
        raise MabangApiError(f"库存导出超时: sn={sn}, taskId={task_id}")

    def _download_export_file_from_url(self, file_url: str, *, referer: str) -> tuple[str, str, bytes]:
        """下载已生成的库存文件。仅重试下载 GET，不重新提交导出任务。"""
        resolved_url = _resolve_export_download_url(file_url, referer, self.base_url)
        last_error = ""
        for attempt in range(1, 4):
            try:
                response = self.client.get(resolved_url, headers={"Referer": referer})
                if _response_looks_like_login(response):
                    raise MabangSessionExpiredError("库存SKU导出下载: 马帮登录会话已失效")
                status = response.status_code
                content_type = _safe_text(response.headers.get("Content-Type")).split(";", 1)[0].lower()
                # Only a known media type is included: neither body nor URL nor arbitrary headers are logged.
                kind = content_type if content_type in {
                    "text/html", "application/json", "text/plain", "application/octet-stream",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                } else "other"
                if 200 <= status < 300 and is_zipfile(BytesIO(response.content)):
                    try:
                        probe = load_workbook(BytesIO(response.content), read_only=True, data_only=True)
                        try:
                            if probe.active is None:
                                raise ValueError("workbook has no active worksheet")
                        finally:
                            probe.close()
                    except Exception as exc:
                        last_error = f"status={status}, content_type={kind}, bytes={len(response.content)}, workbook_error={type(exc).__name__}"
                    else:
                        file_name = _guess_download_filename(response.headers.get("Content-Disposition"), resolved_url)
                        file_type = _safe_text(Path(file_name).suffix).lower().lstrip(".") or "xlsx"
                        return file_name, file_type, response.content
                else:
                    last_error = f"status={status}, content_type={kind}, bytes={len(response.content)}"
            except MabangSessionExpiredError:
                raise
            except httpx.HTTPError as exc:
                last_error = f"error_type={type(exc).__name__}"
            if attempt < 3:
                LOGGER.warning("库存SKU导出下载返回无效数据，重试下载 attempt=%s/3 %s", attempt, last_error)
                time.sleep(_compute_retry_sleep_seconds(attempt, cap=3.0))
        raise MabangApiError(f"库存SKU导出下载未返回可解析的XLSX: {last_error}") from None

    def _query_warehouse_list_by_type(self, *, referer: str, warehouse_type: str,
                                      preserve_invalid_values: bool = False) -> list[dict[str, Any]]:
        """按仓库类型查询仓库列表"""
        # The warehouse UI can live on a separate frontend host that has no
        # legacy PHP API. Keep API requests on the configured, logged-in ERP.
        request_url = self._url(WAREHOUSE_LIST_API_URL)
        request_data = {"status": "1", "type": warehouse_type, "orderby": "", "attributetype": "1", "dataTpye": "public"}
        response: httpx.Response | None = None
        failure: MabangWarehouseQueryError | None = None
        for attempt in range(1, 4):
            try:
                response = self.client.post(
                    request_url,
                    data=request_data,
                    headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"},
                    timeout=DEFAULT_WAREHOUSE_LIST_TIMEOUT_SECONDS,
                )
                if 200 <= response.status_code < 300:
                    failure = None
                    break
                failure = MabangWarehouseQueryError(status_code=response.status_code)
                # This endpoint only reads a directory. Never retry permission
                # or routing errors; transient responses have a bounded budget.
                if response.status_code not in {429, 500, 502, 503, 504}:
                    break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                failure = MabangWarehouseQueryError(transport_error_type=type(exc).__name__)
            if attempt < 3:
                LOGGER.warning("仓库目录只读查询重试 attempt=%s/3：%s", attempt, failure)
                time.sleep(min(2 ** attempt, DEFAULT_RETRY_BACKOFF_CAP_SECONDS))
        if failure is not None:
            # Raise outside the handler: the original HTTP error may contain
            # authentication URLs and must not survive in the exception chain.
            raise failure from None
        assert response is not None
        _raise_if_session_expired_response(response, context="warehouse.dosearchwarehouse")
        try:
            payload = response.json()
        except Exception:
            return _parse_warehouse_table(_decode_html_page(response), preserve_invalid_values=preserve_invalid_values)

        if isinstance(payload, dict):
            html_payload = _safe_text(
                payload.get("message") or payload.get("html") or payload.get("tableContent"))
            if html_payload:
                return _parse_warehouse_table(html_payload, preserve_invalid_values=preserve_invalid_values)

            rows_container = payload.get("data")
            if isinstance(rows_container, dict):
                html_payload = _safe_text(
                    rows_container.get("message") or rows_container.get("html") or rows_container.get("tableContent"))
                if html_payload:
                    return _parse_warehouse_table(html_payload, preserve_invalid_values=preserve_invalid_values)
                rows = (
                    rows_container.get("data")
                    or rows_container.get("list")
                    or rows_container.get("rows")
                    or rows_container.get("items")
                )
            else:
                rows = rows_container or payload.get("list") or payload.get("rows") or payload.get("items")
            if isinstance(rows, list):
                result = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue

                    # 统一字段格式，与 parse_warehouse_table 返回结构保持一致
                    # An explicit zero is a value, not a reason to use another field.
                    inv_val = _warehouse_amount(row.get('totalInventoryValue', row.get('inventoryValue')), preserve_invalid_values)
                    tran_val = _warehouse_amount(row.get('totalInTransitValue', row.get('inTransitValue')), preserve_invalid_values)

                    item = {
                        "id": _safe_text(row.get("id") or row.get("warehouseId")),
                        "name": _safe_text(row.get("name") or row.get("warehouseName")),
                        "type": _safe_text(row.get("type")),
                        "totalStock": int(str(row.get("totalStock", 0)).replace(",", "") if str(row.get("totalStock", 0)).replace(",", "").isdigit() else 0),
                        "totalInventoryValue": inv_val,
                        "totalInTransitValue": tran_val,
                        "status": _safe_text(row.get("status") or row.get("warehouseStatus"))
                    }
                    result.append(item)
                return result
        return []

    def _get_yunbi_token(self, page_path: str) -> str:
        """获取云币系统的单点登录Token"""
        self._ensure_logged_in()
        response = self.client.get(self._url(page_path))
        response.raise_for_status()
        page_html = _decode_html_page(response)
        sso_key = _extract_first(r"[?&]key=([^&\"']+)", page_html)
        if not sso_key:
            iframe_src = _extract_first(r'<iframe[^>]+src="([^"]+)"', page_html)
            sso_key = _extract_first(r"[?&]key=([^&\"']+)", html.unescape(iframe_src))
        if not sso_key:
            raise MabangApiError(f"库存导出失败: 无法解析云币SSO密钥 {page_path}")
        token_response = self.client.post(YUNBI_SSO_TOKEN_URL, json={"key": sso_key})
        token_response.raise_for_status()
        payload = token_response.json()
        token = _safe_text(((payload.get("data") or {}) if isinstance(payload, dict) else {}).get("token"))
        if not token:
            raise MabangApiError(f"库存导出失败: 无法获取云币Token {payload}")
        return token

    def _yunbi_headers(self, token: str) -> dict[str, str]:
        """构建云币接口请求头"""
        return {
            "Authorization": f"Bearer {token}",
            "ProjectId": "erp",
            "projectId": "erp",
            "Referer": "https://yunbi-private.mabangerp.com/",
            "X-Requested-With": "XMLHttpRequest",
        }

    def _get_stock_sync_rule_page(
            self,
            *,
            token: str,
            page: int,
            page_size: int,
            rule_name_prefix: str,
    ) -> dict[str, Any]:
        """分页拉取库存同步规则数据；读取类请求可安全退避重试。"""
        response: httpx.Response | None = None
        for attempt in range(1, DEFAULT_STOCK_SYNC_READ_RETRIES + 1):
            try:
                response = self.client.get(
                    f"{YUNBI_API_BASE_URL}/product/StockSync/list",
                    params={
                        "flag": "",
                        "platform_id": "",
                        "shop_id": "",
                        "search_value": _safe_text(rule_name_prefix),
                        "search_type": "rule_name",
                        "select_type": "aLike",
                        "page": str(page),
                        "page_size": str(max(1, int(page_size))),
                    },
                    headers=self._yunbi_headers(token),
                )
                break
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt >= DEFAULT_STOCK_SYNC_READ_RETRIES:
                    raise
                sleep_seconds = _compute_retry_sleep_seconds(attempt)
                LOGGER.warning(
                    "库存同步规则列表读取失败，准备重试 prefix=%s page=%s attempt=%s/%s",
                    rule_name_prefix,
                    page,
                    attempt,
                    DEFAULT_STOCK_SYNC_READ_RETRIES,
                )
                time.sleep(sleep_seconds)
        if response is None:
            raise MabangApiError("库存同步规则列表请求未返回响应")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or str(payload.get("code")) != "200":
            raise MabangApiError(f"库存同步列表获取失败: {payload}")
        return payload

    def _post_stock_sync_manual_sync(self, *, token: str, ids: Sequence[str]) -> dict[str, Any]:
        """提交对应规则ID的同步申请"""
        normalized_ids = [_safe_text(value) for value in ids if _safe_text(value)]
        response = self.client.post(
            f"{YUNBI_API_BASE_URL}/product/StockSync/manualSync",
            json={"id": normalized_ids},
            headers=self._yunbi_headers(token),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise MabangApiError(f"库存同步手动同步返回无效数据: {payload}")
        if str(payload.get("code")) != "200" and not payload.get("success"):
            raise MabangApiError(f"库存同步手动同步失败: {payload}")
        payload["requestedIds"] = normalized_ids
        return payload

    def _get_stock_sync_detail_page(
            self,
            *,
            token: str,
            stock_sync_id: str,
            platform_id: str,
            page: int,
            page_size: int,
            sync_flag: str = "1",
    ) -> dict[str, Any]:
        """读取规则弹窗中的同步商品明细；sync_flag=1 表示未同步。"""
        response: httpx.Response | None = None
        for attempt in range(1, DEFAULT_STOCK_SYNC_READ_RETRIES + 1):
            try:
                response = self.client.get(
                    f"{YUNBI_API_BASE_URL}/product/StockSync/detailedList",
                    params={
                        "platform_id": platform_id,
                        "stock_sync_id": stock_sync_id,
                        "shop_id": "",
                        "match_type": "",
                        "sync_flag": sync_flag,
                        "search_value": "",
                        "search_type": "stock_sku",
                        "page": str(page),
                        "page_source": "list",
                        "time_type": "updateTime",
                        "page_size": str(min(100, max(1, int(page_size)))),
                        "start_time": "",
                        "end_time": "",
                    },
                    headers=self._yunbi_headers(token),
                )
                break
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt >= DEFAULT_STOCK_SYNC_READ_RETRIES:
                    raise
                sleep_seconds = _compute_retry_sleep_seconds(attempt)
                LOGGER.warning(
                    "同步商品明细读取失败，准备重试 rule_id=%s page=%s attempt=%s/%s",
                    stock_sync_id,
                    page,
                    attempt,
                    DEFAULT_STOCK_SYNC_READ_RETRIES,
                )
                time.sleep(sleep_seconds)
        if response is None:
            raise MabangApiError("同步商品明细请求未返回响应")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or str(payload.get("code")) != "200":
            raise MabangApiError(f"同步商品明细获取失败: {payload}")
        return payload

    def _post_stock_sync_detail_manual_sync(
            self,
            *,
            token: str,
            detail_ids: Sequence[str],
    ) -> dict[str, Any]:
        """调用明细弹窗的手动同步接口，单批最多与页面上限一致的100条。"""
        normalized_ids = list(dict.fromkeys(
            _safe_text(value) for value in detail_ids if _safe_text(value)
        ))
        if not normalized_ids:
            raise ValueError("detail_ids 不能为空")
        if len(normalized_ids) > 100:
            raise ValueError("detail_ids 单批不能超过100条")
        response = self.client.post(
            f"{YUNBI_API_BASE_URL}/product/StockSync/manualSyncSku",
            json={"stock_sync_id": 0, "detail_id": normalized_ids},
            headers=self._yunbi_headers(token),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or str(payload.get("code")) != "200":
            raise MabangApiError(f"同步商品明细手动同步失败: {payload}")
        return payload

    def _collect_stock_skus_by_label_id(self, *, label_id: str, rows_per_page: int) -> list[str]:
        """翻页收集标签 SKU；总数变化时有界重读，避免拼接不同批次。"""
        if isinstance(rows_per_page, bool) or int(rows_per_page) <= 0:
            raise ValueError("rows_per_page 必须是正整数")
        # The stock list endpoint caps responses at 100 rows. Comparing its
        # response length with a requested 500 prematurely ends on page one.
        rows_per_page = min(int(rows_per_page), MAX_STOCK_SKU_SEARCH_BATCH_SIZE)
        for scan_attempt in range(1, 4):
            try:
                return self._collect_stock_skus_by_label_id_once(
                    label_id=label_id, rows_per_page=rows_per_page,
                )
            except _StockListTotalChangedError as exc:
                if scan_attempt == 3:
                    raise MabangApiError(
                        f"{exc}；已尝试 3 轮完整查询，拒绝交付不完整结果"
                    ) from exc
                LOGGER.warning(
                    "%s；丢弃本轮结果并从首页重试 scan_attempt=%s/3",
                    exc, scan_attempt,
                )
                time.sleep(_compute_retry_sleep_seconds(scan_attempt, cap=3.0))
        raise AssertionError("unreachable stock label scan state")

    def _collect_stock_skus_by_label_id_once(self, *, label_id: str, rows_per_page: int) -> list[str]:
        """单轮严格分页读取；任何已收集数据只在本轮内使用。"""
        referer, _ = self._load_stock_list_iframe_html()
        request_url = urljoin(referer, STOCK_LIST_API_URL)
        page = 1
        seen: set[str] = set()
        skus: list[str] = []
        expected_total: int | None = None
        while page <= 500:
            last_error: Exception | None = None
            payload: dict[str, Any] | None = None
            for attempt in range(1, 3):
                try:
                    response = self.client.post(
                        request_url,
                        data=_build_stock_list_payload(label_id=label_id, page=page, rows_per_page=rows_per_page),
                        headers={
                            "Referer": referer,
                            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                            "X-Requested-With": "XMLHttpRequest",
                        },
                    )
                    _raise_if_session_expired_response(response, context=f"库存标签查询 page={page}")
                    _raise_for_status_safely(response, context=f"库存标签查询 page={page}")
                    payload = _response_json_dict(
                        response,
                        context=f"库存名称查询 page={page} labelId={label_id}",
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt >= 2:
                        break
                    LOGGER.warning("库存名称查询失败，刷新iframe后重试 page=%s labelId=%s error_type=%s", page, label_id, type(exc).__name__)
                    referer, _ = self._load_stock_list_iframe_html()
                    request_url = urljoin(referer, STOCK_LIST_API_URL)
                    time.sleep(_compute_retry_sleep_seconds(attempt, cap=3.0))
            if payload is None:
                raise MabangApiError(f"库存名称查询失败 page={page} labelId={label_id}") from last_error
            if not payload.get("success"):
                raise MabangApiError(f"库存名称查询业务失败 page={page}")
            rows = payload.get("stockData")
            if not isinstance(rows, list):
                raise MabangApiError(f"库存名称查询 stockData 缺失或结构无效 page={page}")
            page_total = _stock_list_total_count(payload.get("pageHtml"))
            if page_total is not None:
                if expected_total is not None and page_total != expected_total:
                    raise _StockListTotalChangedError(
                        f"库存名称查询总数在分页期间变化 page={page} "
                        f"expected_total={expected_total} observed_total={page_total}"
                    )
                expected_total = page_total
            previous_count = len(skus)
            for row in rows:
                if not isinstance(row, dict):
                    raise MabangApiError(f"库存名称查询行结构无效 page={page}")
                sku = _safe_text(row.get("stockSku"))
                if not sku:
                    raise MabangApiError(f"库存名称查询行缺少 stockSku page={page}")
                if sku not in seen:
                    seen.add(sku)
                    skus.append(sku)
            if rows and len(skus) == previous_count:
                raise MabangApiError(f"库存名称查询分页未前进 page={page}，拒绝交付不完整结果")
            if expected_total is not None:
                if len(skus) > expected_total:
                    raise MabangApiError(f"库存名称查询行数超过页面总数 page={page}")
                if len(skus) == expected_total:
                    return skus
            if not rows or len(rows) < int(rows_per_page):
                if expected_total is not None and len(skus) != expected_total:
                    raise MabangApiError(f"库存名称查询提前结束 page={page}，拒绝交付不完整结果")
                return skus
            page += 1
        raise MabangApiError("库存名称查询超过 500 页，拒绝交付不完整结果")

    def _post_stock_list_json(
            self,
            *,
            request_url: str,
            referer: str,
            data: dict[str, Any],
            context: str,
    ) -> dict[str, Any]:
        """提交库存列表查询并返回JSON。"""
        response = self.client.post(
            request_url,
            data=data,
            headers={
                "Referer": referer,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        _raise_if_session_expired_response(response, context=context)
        _raise_for_status_safely(response, context=context)
        payload = _response_json_dict(response, context=context)
        if not payload.get("success"):
            raise MabangApiError(f"{context}业务失败")
        return payload

    def _search_orders_by_tracking_chunk(self, chunk: Sequence[str]) -> tuple[dict[str, MabangOrderInfo], list[str]]:
        """小批量查询单号时的核心操作逻辑"""
        chunk_results: dict[str, MabangOrderInfo] = {}
        matched_tracking_numbers: set[str] = set()
        page = 1
        while True:
            payload = self.request_order_page(
                {
                    **self._build_order_payload_base(rows_per_page=DEFAULT_ORDER_PAGE_SIZE, page=page),
                    "OrderSearch.batchSearch": "Order.platformOrderId",
                    "OrderSearch.batchSearchValue": ",".join(chunk),
                    "OrderSearch.fuzzySearchConditions3": "1",
                }
            )
            rows = payload.get("orderDataList") or []
            if not rows:
                break
            for row in rows:
                order = self._build_order_info(row)
                if order.tracking_number and order.tracking_number not in chunk_results:
                    chunk_results[order.tracking_number] = order
                    matched_tracking_numbers.add(order.tracking_number)
            if len(rows) < 100:
                break
            page += 1
        missing = [value for value in chunk if value not in matched_tracking_numbers]
        return chunk_results, missing

    def _search_inventory_by_skus(self, *, warehouse_id: str, skus: Sequence[str], status: str = "") -> dict[str, Any]:
        """驱动库存查询并让ERP后台缓存数据"""
        payload = self._post_inventory_json(
            INVENTORY_SEARCH_URL,
            data=self._build_inventory_search_payload(warehouse_id=warehouse_id, skus=skus, status=status),
            context=f"库存查询 warehouse_id={warehouse_id}",
        )
        if not payload.get("success"):
            raise MabangApiError(f"库存查询失败: {payload}")
        return payload

    def _fetch_inventory_totals(self, *, retries: int = 2) -> dict[str, Any]:
        """抓取上方缓存后的库存合计指标"""
        payload = self._post_inventory_json(
            INVENTORY_TOTAL_URL,
            data={"refreshOrderDataFlag": "0"},
            context="库存总数查询",
            retries=retries,
        )
        if not payload.get("success"):
            raise MabangApiError(f"库存总数查询失败，结果: {payload}")
        return payload

    def _post_order_info_sync(self, *, payload: dict[str, Any], type_value: str, retries: int) -> dict[str, Any]:
        """重发订单同步信息的底层逻辑"""
        retry_count = max(1, int(retries))
        last_error: Exception | None = None
        endpoint = self._url(ORDER_INFO_SYNC_PATH)
        request_payload = dict(payload)
        request_payload["type"] = _safe_text(type_value) or "1"
        request_payload.setdefault("endTime", "")
        for attempt in range(1, retry_count + 1):
            try:
                self._ensure_order_context()
                response = self.client.post(endpoint, data=request_payload, headers=self._order_headers())
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise MabangApiError("订单信息同步失败: 无效响应格式")
                return data
            except Exception as exc:  # pragma: no cover - live request guard
                last_error = exc
                if attempt >= retry_count:
                    break
                sleep_seconds = _compute_retry_sleep_seconds(attempt, cap=DEFAULT_RETRY_BACKOFF_CAP_SECONDS)
                LOGGER.warning("马帮订单信息同步重试 %s/%s 失败：%s", attempt, retry_count, exc)
                self._refresh_order_context_for_retry()
                time.sleep(sleep_seconds)
        detail = str(last_error) if last_error else "unknown error"
        raise MabangApiError(f"订单信息同步失败: {detail}") from last_error

    def _order_headers(self) -> dict[str, str]:
        """获取订单常规请求头"""
        return {"Referer": self._url(ORDER_PAGE_PATH), "X-Requested-With": "XMLHttpRequest"}

    def _inventory_headers(self) -> dict[str, str]:
        """获取库存常规请求头"""
        return {"Referer": self._inventory_iframe_url or self._url(INVENTORY_DETAIL_PATH),
                "X-Requested-With": "XMLHttpRequest"}

    def _inventory_url(self, path: str) -> str:
        """按库存iframe域名拼接接口地址。"""
        return urljoin(self._inventory_iframe_url or self._url(INVENTORY_DETAIL_PATH), path)

    def _post_inventory_json(
            self,
            path: str,
            *,
            data: dict[str, Any],
            context: str,
            retries: int = 2,
    ) -> dict[str, Any]:
        """库存模块JSON POST，自动处理iframe域名和一次上下文刷新重试。"""
        retry_count = max(1, int(retries))
        last_error = ""
        for attempt in range(1, retry_count + 1):
            try:
                self._ensure_inventory_detail_context()
                response = self.client.post(
                    self._inventory_url(path),
                    data=data,
                    headers=self._inventory_headers(),
                )
                _raise_if_session_expired_response(response, context=context)
                _raise_for_status_safely(response, context=context)
                try:
                    payload = response.json()
                except ValueError:
                    raise MabangApiError(f"{context}: 库存接口返回非JSON响应") from None
                if not isinstance(payload, dict):
                    raise MabangApiError(f"{context}: 库存接口返回非对象JSON")
                return payload
            except (MabangApiError, httpx.HTTPError) as exc:
                last_error = str(exc) if isinstance(exc, MabangApiError) else type(exc).__name__
                if attempt >= retry_count:
                    break
                LOGGER.warning("%s失败，刷新库存iframe后重试 attempt=%s/%s error=%s", context, attempt, retry_count, last_error)
                self._refresh_inventory_detail_context_for_retry()
                time.sleep(_compute_retry_sleep_seconds(attempt, cap=3.0))
        raise MabangApiError(f"{context}请求失败: {last_error}")

    def _matching_product_headers(self) -> dict[str, str]:
        """获取在线商品匹配页常规请求头。"""
        return {
            "Referer": self._matching_product_iframe_url or self._url(MATCHING_PRODUCT_PAGE_PATH),
            "X-Requested-With": "XMLHttpRequest",
        }

    def _matching_product_url(self, path: str) -> str:
        """按在线商品匹配iframe域名拼接接口地址。"""
        return urljoin(self._matching_product_iframe_url or self._url(MATCHING_PRODUCT_PAGE_PATH), path)

    def _post_matching_product_json(
            self,
            path: str,
            *,
            data: dict[str, Any],
            context: str,
            retries: int = 2,
    ) -> dict[str, Any]:
        """在线商品匹配模块JSON POST，自动刷新一次iframe上下文。"""
        retry_count = max(1, int(retries))
        last_error: Exception | None = None
        for attempt in range(1, retry_count + 1):
            try:
                self._ensure_matching_product_context()
                response = self.client.post(
                    self._matching_product_url(path),
                    data=data,
                    headers=self._matching_product_headers(),
                )
                response.raise_for_status()
                payload = _response_json_dict(response, context=context)
                if "success" in payload and not payload.get("success"):
                    raise MabangApiError(f"{context}失败: {payload}")
                return payload
            except Exception as exc:
                last_error = exc
                if attempt >= retry_count:
                    break
                LOGGER.warning("%s失败，刷新在线商品匹配iframe后重试 attempt=%s/%s error=%s", context, attempt, retry_count, exc)
                self._refresh_matching_product_context_for_retry()
                time.sleep(_compute_retry_sleep_seconds(attempt, cap=3.0))
        raise MabangApiError(f"{context}请求失败: {last_error}") from last_error

    def _order_export_headers(self) -> dict[str, str]:
        """获取导出常规请求头"""
        return {"Referer": self._order_export_page_url or self._url(ORDER_EXPORT_PAGE_PATH),
                "X-Requested-With": "XMLHttpRequest"}

    def _post_order_export_action(
            self,
            payload: dict[str, Any],
            *,
            retries: int,
            allow_failed_payload: bool,
            retry_on_failed_payload: bool = False,
    ) -> dict[str, Any]:
        """包装处理导出相关的底层请求动作"""
        retry_count = max(1, int(retries))
        last_error: Exception | None = None
        for attempt in range(1, retry_count + 1):
            try:
                self._ensure_order_export_context()
                endpoint = _resolve_order_export_action_url(self._order_export_page_url, self.base_url)
                request_payload = payload
                if _safe_text(payload.get("step1")) == "1":
                    request_payload = _merge_order_export_step1_payload(self._order_export_form_defaults, payload)
                form_payload = _flatten_form_payload(request_payload)
                form_body = urlencode(form_payload, doseq=True)
                headers = dict(self._order_export_headers())
                headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
                LOGGER.info(
                    "马帮订单导出请求: step=%s%s%s%s, attempt=%s/%s, body_size=%s",
                    _safe_text(payload.get("step1")) and "1" or "",
                    _safe_text(payload.get("step2")) and "2" or "",
                    _safe_text(payload.get("step3")) and "3" or "",
                    _safe_text(payload.get("step4")) and "4" or "",
                    attempt,
                    retry_count,
                    len(form_body),
                )
                response = self.client.post(endpoint, content=form_body, headers=headers)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise MabangApiError(f"订单导出请求失败：响应格式无效: {data}")
                if not data.get("success"):
                    if not allow_failed_payload:
                        raise MabangApiError(f"订单导出请求失败: {data}")
                    if retry_on_failed_payload and _is_retryable_order_export_failure(data):
                        raise MabangApiError(f"订单导出请求遭遇可重试错误: {data}")
                return data
            except Exception as exc:  # pragma: no cover - live request guard
                last_error = exc
                if attempt >= retry_count:
                    break
                time.sleep(_compute_retry_sleep_seconds(attempt, cap=DEFAULT_RETRY_BACKOFF_CAP_SECONDS))
                self._order_export_page_url = None
                self._order_export_form_defaults = {}
                self._order_export_field_map = {}
        raise MabangApiError(f"多次重试后订单导出请求依然失败: {last_error}") from last_error

    def _resolve_order_export_data(
            self,
            step1_data: dict[str, Any],
            *,
            step_retries: int,
            poll_interval_seconds: float,
            poll_timeout_seconds: float,
    ) -> dict[str, Any]:
        """处理导出的多个轮询阶段，直至确认任务完成并拉取文件直链"""
        direct_file_url = _safe_text(step1_data.get("file_url") or step1_data.get("gourl"))
        if direct_file_url:
            return step1_data

        success_type = _safe_text(step1_data.get("success_type"))
        if success_type == "2":
            sn = _safe_text(step1_data.get("sn"))
            try:
                subtask_num = int(step1_data.get("subtask_num") or 0)
            except (TypeError, ValueError):
                subtask_num = 0
            if not sn or subtask_num <= 0:
                raise MabangApiError(f"订单导出第一步响应无效: {step1_data}")

            for sub_no in range(1, subtask_num + 1):
                subtask_ok = False
                for attempt in range(1, max(1, step_retries) + 1):
                    step2_data = self._post_order_export_action(
                        {"step2": "1", "sn": sn, "sub_no": str(sub_no)},
                        retries=1,
                        allow_failed_payload=True,
                    )
                    if step2_data.get("success"):
                        subtask_ok = True
                        break
                    time.sleep(_compute_retry_sleep_seconds(attempt, cap=3.0))
                if not subtask_ok:
                    raise MabangApiError(f"订单导出第二步处理失败: sn={sn}, sub_no={sub_no}")

            step3_data = self._post_order_export_action(
                {"step3": "1", "sn": sn},
                retries=max(1, step_retries),
                allow_failed_payload=False,
            )
            if _safe_text(step3_data.get("async")).lower() == "true":
                task_id = _safe_text(step3_data.get("taskId"))
                if not task_id:
                    raise MabangApiError(f"订单导出第三步失败 (缺少taskId): {step3_data}")
                deadline = time.time() + max(5.0, float(poll_timeout_seconds))
                interval = max(0.1, float(poll_interval_seconds))
                while time.time() < deadline:
                    step4_data = self._post_order_export_action(
                        {"step4": "1", "sn": sn, "taskId": task_id},
                        retries=1,
                        allow_failed_payload=False,
                    )
                    step4_file_url = _safe_text(step4_data.get("file_url") or step4_data.get("gourl"))
                    step4_state = step4_data.get("state")
                    if step4_file_url and (step4_state is True or str(step4_state) in {"1", "true", "True"}):
                        return step4_data
                    time.sleep(interval)
                raise MabangApiError(f"订单导出第四步轮询超时: sn={sn}, taskId={task_id}")
            return step3_data

        raise MabangApiError(f"订单导出失败：不支持的第一步响应结构: {step1_data}")

    def _download_export_file(self, file_url: str) -> tuple[str, str, bytes]:
        """下载导出成功生成的文件二进制数据"""
        resolved_url = _resolve_export_download_url(file_url, self._order_export_page_url, self.base_url)
        response = self.client.get(resolved_url, headers={
            "Referer": self._order_export_page_url or self._url(ORDER_EXPORT_PAGE_PATH)})
        response.raise_for_status()
        file_name = _guess_download_filename(response.headers.get("Content-Disposition"), resolved_url)
        file_type = _safe_text(Path(file_name).suffix).lower().lstrip(".")
        if not file_type:
            file_type = "xlsx"
        return file_name, file_type, response.content

    def assert_authenticated_response(self, response: httpx.Response, *, context: str) -> None:
        """确认响应仍来自已登录业务页面；不执行自动重放。"""
        _raise_if_session_expired_response(response, context=context)

    def _auth_snapshot(self) -> tuple[int, bool]:
        """返回当前认证代次和本地登录状态。"""
        with self._auth_lock:
            return self._auth_epoch, self._logged_in

    def _reauthenticate_singleflight(self, observed_epoch: int, *, context: str) -> None:
        """同一认证代次仅允许一个线程重新登录，其余线程复用结果。"""
        with self._auth_lock:
            if self._auth_epoch != observed_epoch:
                if self._logged_in:
                    return
                raise MabangSessionExpiredError(f"{context}: 其他线程重新登录失败")
            if not (self._login_username and self._login_password):
                self._logged_in = False
                self._invalidate_context_cache()
                self._auth_epoch += 1
                raise MabangSessionExpiredError(f"{context}: 会话失效且没有可用于重新登录的凭据")

            LOGGER.warning("%s检测到马帮会话失效，正在执行单次重新登录", context)
            try:
                self._perform_login_locked(self._login_username, self._login_password)
            except Exception as exc:
                self._logged_in = False
                self._invalidate_context_cache()
                self._auth_epoch += 1
                raise MabangSessionExpiredError(f"{context}: 重新登录失败") from exc

    def _run_replay_safe_authenticated_read(
            self,
            operation: Callable[[], Any],
            *,
            context: str,
            before_replay: Callable[[], None] | None = None,
    ) -> Any:
        """认证失效时单飞重登，并且只重放一次确认可安全重放的读取操作。"""
        observed_epoch, logged_in = self._auth_snapshot()
        if not logged_in:
            raise MabangSessionExpiredError(f"{context}: 马帮客户端当前尚未登录")
        try:
            return operation()
        except MabangSessionExpiredError:
            self._reauthenticate_singleflight(observed_epoch, context=context)
            replay_epoch, _ = self._auth_snapshot()
            if before_replay is not None:
                before_replay()
            try:
                return operation()
            except MabangSessionExpiredError as replay_error:
                with self._auth_lock:
                    if self._auth_epoch == replay_epoch:
                        self._logged_in = False
                        self._invalidate_context_cache()
                        self._auth_epoch += 1
                raise MabangSessionExpiredError(f"{context}: 重新登录后会话仍无效") from replay_error

    def _refresh_order_context_for_retry(self) -> None:
        """出现网络或状态错误时，清空当前页面上下文以备重新获取"""
        self._invalidate_order_context()
        try:
            self._ensure_order_context()
            return
        except Exception as exc:
            LOGGER.warning("马帮订单上下文刷新失败：%s", exc)
        if not (self._login_username and self._login_password):
            return
        try:
            self.login(self._login_username, self._login_password)
            self._ensure_order_context()
            LOGGER.info("重新登录后马帮订单上下文已恢复")
        except Exception as exc:
            LOGGER.warning("重试前重新登录马帮失败：%s", exc)

    def _refresh_inventory_detail_context_for_retry(self) -> None:
        """库存接口返回异常时，清空库存iframe缓存并重新加载。"""
        self._inventory_detail_bootstrapped = False
        self._inventory_iframe_url = None
        try:
            self._ensure_inventory_detail_context()
            return
        except Exception as exc:
            LOGGER.warning("马帮库存iframe刷新失败：%s", exc)
        if not (self._login_username and self._login_password):
            return
        try:
            self.login(self._login_username, self._login_password)
            self._ensure_inventory_detail_context()
            LOGGER.info("重新登录后马帮库存iframe已恢复")
        except Exception as exc:
            LOGGER.warning("重试前重新登录马帮失败：%s", exc)

    def _refresh_matching_product_context_for_retry(self) -> None:
        """在线商品匹配接口异常时，清空iframe缓存并重新加载。"""
        self._matching_product_iframe_url = None
        try:
            self._ensure_matching_product_context()
            return
        except Exception as exc:
            LOGGER.warning("马帮在线商品匹配iframe刷新失败：%s", exc)
        if not (self._login_username and self._login_password):
            return
        try:
            self.login(self._login_username, self._login_password)
            self._ensure_matching_product_context()
            LOGGER.info("重新登录后马帮在线商品匹配iframe已恢复")
        except Exception as exc:
            LOGGER.warning("重试前重新登录马帮失败：%s", exc)

    def _refresh_warehouse_transfer_context_for_retry(self) -> None:
        """调拨接口异常时刷新iframe上下文，必要时重新登录。"""
        self._warehouse_transfer_iframe_url = None
        try:
            self._ensure_warehouse_transfer_context()
            return
        except Exception as exc:
            LOGGER.warning("马帮调拨iframe刷新失败：%s", exc)
        if not (self._login_username and self._login_password):
            return
        try:
            self.login(self._login_username, self._login_password)
            self._ensure_warehouse_transfer_context()
            LOGGER.info("重新登录后马帮调拨iframe已恢复")
        except Exception as exc:
            LOGGER.warning("重试前重新登录马帮失败：%s", exc)

    def _invalidate_order_context(self) -> None:
        """置空订单上下文属性缓存"""
        self._order_page_key = None
        self._order_tab_id = None

    def _invalidate_context_cache(self) -> None:
        """全量置空各大模块页面初始化标记"""
        self._invalidate_order_context()
        self._returns_bootstrapped = False
        self._refunds_bootstrapped = False
        self._inventory_detail_bootstrapped = False
        self._inventory_iframe_url = None
        self._warehouse_transfer_iframe_url = None
        self._matching_product_iframe_url = None
        self._order_export_page_url = None
        self._order_export_form_defaults = {}
        self._order_export_field_map = {}

    def _ensure_order_context(self) -> None:
        """确保在发送请求前订单页面防抖校验参数就绪"""
        if self._order_page_key and self._order_tab_id:
            return
        page_html = self._get_order_page_html()
        self._parse_order_context(page_html)

    def _ensure_order_export_context(self) -> None:
        """确保订单导出页面参数和模板项准备就绪"""
        if self._order_export_page_url and self._order_export_form_defaults and self._order_export_field_map:
            return
        page_html = self._get_order_page_html()
        self._parse_order_context(page_html)
        export_page_url = _extract_order_export_page_url(page_html, self.base_url) or self._url(ORDER_EXPORT_PAGE_PATH)
        response = self.client.get(export_page_url, headers={"Referer": self._url(ORDER_PAGE_PATH)})
        response.raise_for_status()
        resolved_export_page_url = str(response.url)
        export_html = _decode_html_page(response)
        form_defaults = _extract_hidden_form_values(export_html, form_id="theform")
        field_map = _extract_order_export_field_map(export_html)
        if not field_map:
            iframe_src = _extract_first(r'<iframe[^>]+src="([^"]*mod=order\.exportOrderByTemplate[^"]*)"', export_html)
            if iframe_src:
                iframe_url = urljoin(self.base_url.rstrip("/") + "/", iframe_src)
                if iframe_url != resolved_export_page_url:
                    iframe_response = self.client.get(iframe_url, headers={"Referer": resolved_export_page_url})
                    iframe_response.raise_for_status()
                    resolved_export_page_url = str(iframe_response.url)
                    export_html = _decode_html_page(iframe_response)
                    form_defaults = _extract_hidden_form_values(export_html, form_id="theform")
                    field_map = _extract_order_export_field_map(export_html)
        if not field_map:
            raise MabangApiError(f"无法从 {resolved_export_page_url} 解析出订单导出字段")
        if "orderItemOrderBy" not in form_defaults:
            form_defaults["orderItemOrderBy"] = "id asc,stockId asc"
        form_defaults.setdefault("templateName", "")
        form_defaults.setdefault("templateId", "0")
        form_defaults.setdefault("standardVersion", "1")
        form_defaults.setdefault("pageSave", "1")
        self._order_export_page_url = resolved_export_page_url
        self._order_export_form_defaults = form_defaults
        self._order_export_field_map = field_map

    def _ensure_matching_product_context(self) -> None:
        """进入在线商品匹配页，拿到后续接口所需的iframe地址。"""
        if self._matching_product_iframe_url:
            return
        iframe_url, _ = self._load_iframe_html(MATCHING_PRODUCT_PAGE_PATH)
        self._matching_product_iframe_url = iframe_url

    def _ensure_warehouse_transfer_context(self) -> None:
        """进入调拨发货页面，初始化查询接口需要的iframe会话。"""
        if self._warehouse_transfer_iframe_url:
            return
        iframe_url, _ = self._load_iframe_html(WAREHOUSE_TRANSFER_SHIPMENT_PAGE_PATH)
        self._warehouse_transfer_iframe_url = iframe_url

    def _get_order_page_html(self) -> str:
        """拉取订单页面的原生HTML文本"""
        self._ensure_logged_in()
        response = self.client.get(self._url(ORDER_PAGE_PATH))
        response.raise_for_status()
        return _decode_html_page(response)

    def _parse_order_context(self, page_html: str) -> None:
        """解析HTML当中的认证防抖变量"""
        self._order_page_key = _extract_first(r"var\s+orderPageKey\s*=\s*'([^']+)'", page_html)
        self._order_tab_id = _extract_first(r'<li[^>]*class="[^"]*active[^"]*"[^>]*data-id="([^"]+)"',
                                            page_html) or _extract_first(
            r'<li[^>]*data-id="([^"]+)"[^>]*class="[^"]*active[^"]*"', page_html) or "222"
        if not self._order_page_key:
            raise MabangApiError("无法从订单页面解析出 orderPageKey 防抖验证码")

    def _ensure_returns_context(self) -> None:
        """确认是否访问过退货模块来初始化系统"""
        if self._returns_bootstrapped:
            return
        self._ensure_logged_in()
        response = self.client.get(self._url(RETURNS_PAGE_PATH))
        response.raise_for_status()
        page_html = _decode_html_page(response)
        iframe_src = _extract_first(r'<iframe[^>]+src="([^"]*mod=order\.refundsOrderList[^"]*)"', page_html)
        if iframe_src:
            iframe_response = self.client.get(urljoin(self.base_url + "/", iframe_src))
            iframe_response.raise_for_status()
        self._returns_bootstrapped = True

    def _ensure_refunds_context(self) -> None:
        """确认是否访问过退款模块来初始化系统"""
        if self._refunds_bootstrapped:
            return
        self._ensure_logged_in()
        response = self.client.get(self._url(REFUNDS_PAGE_PATH))
        response.raise_for_status()
        page_html = _decode_html_page(response)
        iframe_src = _extract_first(r'<iframe[^>]+src="([^"]*mod=paypal\.paypalrefund[^"]*)"', page_html)
        if iframe_src:
            iframe_response = self.client.get(urljoin(self.base_url + "/", iframe_src))
            iframe_response.raise_for_status()
        self._refunds_bootstrapped = True

    def _ensure_inventory_detail_context(self) -> None:
        """进入库存详情拿到后续所需对应的iframe地址"""
        if self._inventory_detail_bootstrapped and self._inventory_iframe_url:
            return
        self._ensure_logged_in()
        response = self.client.get(self._url(INVENTORY_DETAIL_PATH))
        _validate_inventory_page_response(response, context="加载库存详情页")
        page_html = _decode_html_page(response)
        iframe_src = _extract_first(r'<iframe[^>]+id="iframeContent"[^>]+src="([^"]+)"', page_html) or _extract_first(
            r'<iframe[^>]+src="([^"]*mod=warehouse\.inventorydetail[^"]*)"', page_html)
        if not iframe_src:
            raise MabangApiError("无法从库存详情页解析出子框架(iframe)的链接")
        iframe_response = self.client.get(urljoin(self.base_url + "/", iframe_src))
        _validate_inventory_page_response(iframe_response, context="加载库存iframe")
        self._inventory_iframe_url = str(iframe_response.url)
        self._inventory_detail_bootstrapped = True

    def _ensure_logged_in(self) -> None:
        """判定当前操作是否处于已登入状态"""
        _, logged_in = self._auth_snapshot()
        if not logged_in:
            raise MabangApiError("马帮客户端当前尚未登录")

    def _url(self, path: str) -> str:
        """将内部路径转换拼接为绝对URL路径"""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}{path}"


# ===== 内部辅助函数 =====
def _build_http_timeout(timeout_seconds: int | float) -> httpx.Timeout:
    """构建 httpx 的细粒度超时配置对象"""
    read_timeout = max(5.0, float(timeout_seconds))
    connect_timeout = min(read_timeout, DEFAULT_CONNECT_TIMEOUT_SECONDS)
    write_timeout = min(read_timeout, DEFAULT_WRITE_TIMEOUT_SECONDS)
    pool_timeout = min(read_timeout, DEFAULT_POOL_TIMEOUT_SECONDS)
    return httpx.Timeout(connect=connect_timeout, read=read_timeout, write=write_timeout, pool=pool_timeout)


def _compute_retry_sleep_seconds(attempt: int, *, cap: float = DEFAULT_RETRY_BACKOFF_CAP_SECONDS) -> float:
    """计算退避重试休眠等待的时长(带阈值限制)"""
    step = max(1, int(attempt))
    return min(float(cap), float(2 ** (step - 1)))


def parse_return_records(message_html: str, *, default_tracking_number: str = "") -> list[MabangReturnRecord]:
    """通过正则清洗HTML提取退货数据的明细列表"""
    if not message_html or NO_DATA_MARKER in message_html:
        return []
    records: list[MabangReturnRecord] = []
    for title_html, content_html in _iter_row_pairs(message_html):
        title_text = _strip_html(title_html)
        content_text = _strip_html(content_html)
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", content_text)
        records.append(
            MabangReturnRecord(tracking_number=_extract_first(r"【([^】]+)】", content_text) or default_tracking_number,
                               platform_order_id=_extract_first(r"platformOrderId=([^\"&]+)",
                                                                title_html + content_html),
                               return_order_no=_extract_first(r"<input[^>]*>\s*([^<\s]+)\s*<span", title_html),
                               platform_return_order_no=_extract_first(r"return_tracknumber&quot;:&quot;([^\"&]+)",
                                                                       content_html) or _extract_first(
                                   r"平台退货单编号[:：]\s*([^\s]+)", title_text),
                               platform_order_status=_extract_first(r'data-field="ro_platformOrderStatus"[^>]*>(.*?)<',
                                                                    content_html),
                               return_status=_extract_first(r'data-field="ro_returnStatus"[^>]*>(.*?)<', content_html),
                               register_time=_extract_first(r"登记[:：]\s*([0-9:\- ]+)",
                                                            title_text + " " + content_text),
                               raw_summary_text=_compact_text(f"{title_text} | {content_text}")))
    return records


def parse_refund_records(message_html: str) -> list[MabangRefundRecord]:
    """通过正则清洗HTML提取退款数据的明细列表"""
    if not message_html or NO_DATA_MARKER in message_html:
        return []
    records: list[MabangRefundRecord] = []
    for title_html, content_html in _iter_row_pairs(message_html):
        title_text = _strip_html(title_html)
        content_text = _strip_html(content_html)
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", content_text)
        records.append(MabangRefundRecord(platform_order_id=_extract_first(r"platformOrderId=([^\"&]+)", title_html),
                                          refund_id=_extract_first(r'data-id="([^"]+)"', content_html),
                                          refund_order_no=_extract_first(
                                              r'<td class="pct12">.*?<span[^>]*title="([^"]+)"',
                                              content_html) or _extract_first(r"\bTKD\d+\b", content_text),
                                          refund_status=_extract_first(r'text-success">([^<]+)<',
                                                                       content_html) or _extract_first(
                                              r'<td class="text-center">\s*<span[^>]*>([^<]+)</span>', content_html),
                                          refund_reason=_extract_first(r'error-text fixed" title="([^"]*)"',
                                                                       content_html) or _extract_first(
                                              r"(Failed Delivery|[A-Za-z ]+)", content_text),
                                          order_status_at_refund=_extract_first(r"创建退款时订单状态[:：]\s*([^\s]+)",
                                                                                title_text + " " + content_text),
                                          refund_date=dates[0] if dates else "",
                                          raw_summary_text=_compact_text(f"{title_text} | {content_text}")))
    return records


# ===== 内部解析器 =====
class _OrderShopParser(HTMLParser):
    """基于 HTMLParser 的店铺DOM标签解析器"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.shops: list[MabangOrderShop] = []
        self._label_depth = 0
        self._current_input_attrs: dict[str, str] | None = None
        self._current_label_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        if normalized_tag == "label":
            if self._label_depth == 0:
                self._current_input_attrs = None
                self._current_label_parts = []
            self._label_depth += 1
            return
        if self._label_depth <= 0:
            return
        if normalized_tag == "input":
            attrs_dict = {name: "" if value is None else value for name, value in attrs}
            if _safe_text(attrs_dict.get("name")) == "shopIdMultiple[]":
                self._current_input_attrs = attrs_dict

    def handle_data(self, data: str) -> None:
        if self._label_depth > 0 and data:
            self._current_label_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "label" or self._label_depth <= 0:
            return
        self._label_depth -= 1
        if self._label_depth != 0 or self._current_input_attrs is None:
            return
        attrs = dict(self._current_input_attrs)
        self.shops.append(
            MabangOrderShop(
                shop_id=_safe_text(attrs.get("value")),
                shop_name=_compact_text(" ".join(self._current_label_parts)),
                platform_id=_safe_text(attrs.get("data-platform-id")),
                platform_name=_safe_text(attrs.get("data-platform-name") or attrs.get("data-platformname")),
                raw_data=attrs,
            )
        )
        self._current_input_attrs = None
        self._current_label_parts = []


def _parse_order_shops(page_html: str) -> list[MabangOrderShop]:
    """触发解析店铺标签信息"""
    parser = _OrderShopParser()
    parser.feed(page_html)
    parser.close()
    return parser.shops


def _parse_order_platforms(page_html: str) -> list[MabangOrderPlatform]:
    """利用正则解析平台名称等信息"""
    pattern = re.compile(
        r'<input[^>]*name=["\']platformId\[\]["\'][^>]*value=["\']([^"\']+)["\'][^>]*>\s*<span[^>]*>(.*?)</span>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    platforms: list[MabangOrderPlatform] = []
    for match in pattern.finditer(page_html):
        platform_id = _safe_text(match.group(1))
        if not platform_id:
            continue
        platforms.append(
            MabangOrderPlatform(
                platform_id=platform_id,
                platform_name=_clean_fragment(match.group(2)),
            )
        )
    return platforms


def _build_warehouse_transfer_search_payload(
        *,
        warehouse_id: str,
        page: int,
        rows_per_page: int,
) -> dict[str, str]:
    """生成“分仓调拨发货/待签收”的查询表单。"""
    return {
        "orderBys[]": "",
        "warehouseMold": "all",
        "startWarehouseIdStr": "",
        "targetWarehouseIdStr": warehouse_id,
        "search-content1": "allocationCode",
        "search-content-text1": "",
        "tablebase": "",
        "Orderby": "",
        "third_in_status": "",
        "third_out_status": "",
        "page": str(page),
        "rowsPerPage": str(rows_per_page),
        "type": "1",
        "allocationstatus": "2",
        "startwarhouseId": "",
        "targetwarhouseId": "",
        "timetype": "",
        "datepickerfrom": "",
        "datepickerto": "",
        "orderbysVal": "",
        "orderbydac": "",
        "auditStatus": "",
        "labelId": "",
        "freight_set": "",
        "transportType": "",
    }


def parse_warehouse_transfer_shipments(
        message_html: str,
        *,
        warehouse_id: str = "",
) -> list[MabangWarehouseTransferShipment]:
    """解析调拨查询返回的列表HTML。"""
    if not message_html:
        return []

    soup = BeautifulSoup(message_html, "html.parser")
    shipments: list[MabangWarehouseTransferShipment] = []
    seen_allocation_ids: set[str] = set()

    for checkbox in soup.select("input.allow[name='allot[]']"):
        allocation_id = _safe_text(checkbox.get("value"))
        batch_code = _safe_text(checkbox.get("data-code"))
        allocation_list = checkbox.find_parent("ul")
        shipment_node = allocation_list.parent if allocation_list is not None else None
        if shipment_node is None:
            continue

        if not batch_code:
            batch_link = shipment_node.select_one(
                "a[href*='mod=warehouseallocation.editallocation']"
            )
            batch_code = _compact_text(batch_link.get_text(" ", strip=True)) if batch_link else ""

        remark_node = shipment_node.select_one("textarea.fhRemark")
        remark = ""
        if remark_node is not None:
            remark = _compact_text(
                _safe_text(
                    remark_node.get_text(" ", strip=True)
                    or remark_node.get("value")
                )
            )

        stock_skus: list[str] = []
        seen_skus: set[str] = set()
        for sku_link in shipment_node.select("a[href*='mod=stock.modify']"):
            sku = _compact_text(sku_link.get_text(" ", strip=True))
            if not sku or sku in seen_skus:
                continue
            seen_skus.add(sku)
            stock_skus.append(sku)

        allocation_key = allocation_id or batch_code
        if not allocation_key or allocation_key in seen_allocation_ids:
            continue
        seen_allocation_ids.add(allocation_key)
        shipments.append(
            MabangWarehouseTransferShipment(
                allocation_id=allocation_id,
                batch_code=batch_code,
                warehouse_id=_safe_text(warehouse_id),
                remark=remark,
                stock_skus=stock_skus,
            )
        )

    return shipments


def _parse_warehouse_transfer_total_pages(page_html: Any) -> int:
    """空仓库的pageHtml为false；有数据时从分页HTML读取总页数。"""
    if page_html is False or page_html is None:
        return 0
    page_html_text = _safe_text(page_html)
    if not page_html_text:
        return 0
    match = re.search(
        r'<span[^>]*class=["\'][^"\']*\bsemibold\b[^"\']*["\'][^>]*>'
        r'\s*[\d,]+\s*/\s*([\d,]+)\s*</span>\s*页',
        page_html_text,
        flags=re.IGNORECASE,
    )
    if not match:
        return 1
    return max(1, int(match.group(1).replace(",", "")))


def normalize_order_page_size(value: Any, *, default: int = DEFAULT_ORDER_PAGE_SIZE) -> int:
    """限制每页请求的数值防止出界导致被拦截"""
    try:
        size = int(value)
    except (TypeError, ValueError):
        size = default
    if size in SUPPORTED_ORDER_PAGE_SIZES:
        return size
    if size > max(SUPPORTED_ORDER_PAGE_SIZES):
        return max(SUPPORTED_ORDER_PAGE_SIZES)
    smaller_or_equal = [item for item in SUPPORTED_ORDER_PAGE_SIZES if item <= size]
    if smaller_or_equal:
        return max(smaller_or_equal)
    return min(SUPPORTED_ORDER_PAGE_SIZES)


def _split_tracking_numbers(values: Sequence[str], chunk_count: int) -> list[list[str]]:
    """工具函数：将物流单号根据数量划分为指定份数的小段"""
    if chunk_count <= 1:
        return [list(values)]
    chunk_size = (len(values) + chunk_count - 1) // chunk_count
    return [list(values[index: index + chunk_size]) for index in range(0, len(values), chunk_size)]


def _stock_list_total_count(page_html: Any) -> int | None:
    """Read the displayed filtered total; Mabang repeats its final full page."""
    if not isinstance(page_html, str) or not page_html:
        return None
    pager = BeautifulSoup(page_html, "html.parser")
    # The first standalone semibold number is the filtered total. Later
    # semibold text is the visible range (for example, 7201-7300).
    for item in pager.select("span.semibold"):
        value = item.get_text(strip=True).replace(",", "")
        if re.fullmatch(r"\d+", value):
            return int(value)
    return None


def _build_stock_list_payload(*, label_id: str, page: int, rows_per_page: int) -> dict[str, Any]:
    """生成库存列表查询表单数据"""
    return {
        "searchKey": "Stock_stockSku",
        "operate": "likeStart",
        "orderBys[]": "",
        "Stock_stockSku": "",
        "Stock_nameCN": "",
        "Stock_nameEN": "",
        "Stock_defaultRetailNameCn": "",
        "StockPlus_financial": "",
        "search-content": "库存SKU",
        "searchValue": "",
        "status": "",
        "parentCategoryId": "",
        "categoryId": "",
        "third_category_id": "",
        "parentBrandId": "",
        "list-brandId": "",
        "labelId": label_id,
        "buyerId": "",
        "developerIdM": "",
        "dev_assistant": "",
        "artDesignerId": "",
        "salesId": "",
        "defaultStockWarehouseDetailId": "",
        "livenessType": "",
        "isNewType": "",
        "stock_type": "",
        "isMachining": "",
        "showstart": "1",
        "isCloud": "",
        "isGift": "",
        "exceptionDeclaration": "",
        "singleWarehouseType": "",
        "isGoogsExpireManageSearch": "",
        "page": str(page),
        "rowsPerPage": str(rows_per_page),
        "stockOrderby": "",
        "warehouseIdArr": "",
        "warehouseIdStr": "",
        "warehouseMold": "all",
        "isBatchSearch": "0",
    }


def _build_stock_sku_batch_search_payload(
        *,
        skus: Sequence[str],
        rows_per_page: int,
        status: str = "3",
) -> dict[str, Any]:
    """生成库存SKU批量查询表单数据。"""
    normalized_skus = [_safe_text(value) for value in skus if _safe_text(value)]
    return {
        "searchKey": "Stock_stockSku",
        "operate": "=",
        "orderBys[]": "",
        "Stock_stockSku": "",
        "Stock_nameCN": "",
        "Stock_nameEN": "",
        "Stock_defaultRetailNameCn": "",
        "StockPlus_financial": "",
        "search-content": "库存SKU",
        "searchValue": normalized_skus[0] if len(normalized_skus) == 1 else "",
        "status": "3" if status is None else _safe_text(status),
        "parentCategoryId": "",
        "categoryId": "",
        "third_category_id": "",
        "parentBrandId": "",
        "list-brandId": "",
        "labelId": "",
        "buyerId": "",
        "developerIdM": "",
        "dev_assistant": "",
        "artDesignerId": "",
        "salesId": "",
        "defaultStockWarehouseDetailId": "",
        "livenessType": "",
        "isNewType": "",
        "stock_type": "",
        "isMachining": "",
        "showstart": "1",
        "isCloud": "",
        "isGift": "",
        "exceptionDeclaration": "",
        "singleWarehouseType": "",
        "isGoogsExpireManageSearch": "",
        "stockData": "\n".join(normalized_skus),
        "isBatchSearch": "1",
        "selecttype": "stockSku",
        "page": "1",
        "rowsPerPage": str(rows_per_page),
        "stockOrderby": "",
        "warehouseIdArr": "",
        "warehouseIdStr": "",
        "warehouseMold": "all",
    }


def _iter_row_pairs(message_html: str) -> list[tuple[str, str]]:
    """工具函数：提取HTML中成对出现的列表表头与表内容项"""
    pair_pattern = re.compile(
        r'(?is)(<tr[^>]*class="title[^"]*"[^>]*>.*?</tr>)\s*(<tr[^>]*class="content[^"]*"[^>]*>.*?</tr>)')
    return [(match.group(1), match.group(2)) for match in pair_pattern.finditer(message_html)]


def _warehouse_amount(value: Any, preserve_invalid_values: bool):
    if not preserve_invalid_values:
        return _safe_float_number(value)
    try:
        result = float(_safe_text(value).replace(',', ''))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _warehouse_rmb_text(row_html: str, field: str) -> str:
    row = BeautifulSoup(row_html, 'html.parser')
    if row.select('.default-rmb-column, .default-currency-column'):
        matches = row.select(f'.default-rmb-column .{field}, .default-rmb-column.{field}')
    else:
        matches = row.select(f'.{field}')
    values = {element.get_text('', strip=True) for element in matches}
    # Ambiguous currencies or missing markup must never become a business zero.
    return values.pop() if len(values) == 1 else ''


def _parse_warehouse_table(html_text: str, *, preserve_invalid_values: bool = False) -> list[dict[str, Any]]:
    """解析马帮仓库列表HTML，不依赖第三方HTML库。"""
    if not html_text:
        return []

    warehouses: list[dict[str, Any]] = []
    row_pattern = re.compile(r"(?is)<tr\b[^>]*>.*?</tr>")
    for row_match in row_pattern.finditer(html_text):
        row_html = row_match.group(0)
        if "checkWarehouse" not in row_html:
            continue

        warehouse_id = _extract_warehouse_checkbox_id(row_html)
        if not warehouse_id:
            continue

        text_center_cells = re.findall(
            r'(?is)<td\b[^>]*class=["\'][^"\']*text-center[^"\']*["\'][^>]*>(.*?)</td>',
            row_html,
        )
        data = {
            "id": warehouse_id,
            "name": _extract_tag_text_by_class(row_html, "a", "semibold"),
            "type": _strip_html(text_center_cells[0]) if text_center_cells else "",
            "totalStock": int(_safe_float_number(_extract_tag_text_by_class(row_html, "span", "totalStock"))),
            **{field: _warehouse_amount(
                _warehouse_rmb_text(row_html, field) if preserve_invalid_values else _extract_tag_text_by_class(row_html, '', field),
                preserve_invalid_values) for field in ('totalInventoryValue', 'totalInTransitValue')},
            "status": _extract_tag_text_by_class(row_html, "td", "text-success"),
        }
        if (preserve_invalid_values
                and _warehouse_rmb_text(row_html, 'totalInventoryValue') == '--'
                and _extract_tag_text_by_class(row_html, 'span', 'totalStock') == '--'):
            # This is a query trigger, never a numeric zero on its own.
            data['_inventory_value_placeholder'] = True
        warehouses.append(data)
    return warehouses


def _extract_warehouse_checkbox_id(row_html: str) -> str:
    input_match = re.search(
        r'(?is)<input\b(?=[^>]*class=["\'][^"\']*checkWarehouse[^"\']*["\'])([^>]*)>',
        row_html,
    )
    if not input_match:
        return ""
    attrs = input_match.group(1)
    return (
        _extract_attr(attrs, "data-id")
        or _extract_attr(attrs, "warehouseid")
        or _extract_attr(attrs, "warehouse-id")
        or _extract_attr(attrs, "value")
    )


def _extract_tag_text_by_class(fragment: str, tag_name: str, class_name: str) -> str:
    tag_pattern = re.escape(tag_name) if tag_name else r"[a-z][a-z0-9]*"
    pattern = re.compile(
        rf'(?is)<(?P<tag>{tag_pattern})\b[^>]*class=["\'][^"\']*\b{re.escape(class_name)}\b[^"\']*["\'][^>]*>(.*?)</(?P=tag)>'
    )
    match = pattern.search(fragment)
    return _strip_html(match.group(2)) if match else ""


def _extract_attr(attrs: str, attr_name: str) -> str:
    match = re.search(rf'(?is)\b{re.escape(attr_name)}\s*=\s*(["\'])(.*?)\1', attrs)
    return html.unescape(match.group(2)).strip() if match else ""


def _decode_html_page(response: httpx.Response) -> str:
    """兼容多编码集的页面解码函数"""
    for encoding in [response.encoding, "gb18030", "utf-8"]:
        if not encoding:
            continue
        try:
            return response.content.decode(encoding, errors="ignore")
        except LookupError:
            continue
    return response.text


def _safe_url_for_log(url: Any) -> str:
    """只保留主机和路径，避免把查询参数或认证信息写入日志。"""
    try:
        parsed = urlparse(str(url))
    except (TypeError, ValueError):
        return "-"
    hostname = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    host = f"{hostname}:{port}" if hostname and port is not None else hostname
    path = parsed.path or "/"
    return f"{host}{path}" if host else path


def _raise_for_status_safely(response: httpx.Response, *, context: str) -> None:
    """抛出不包含响应正文、查询参数或底层完整URL的HTTP状态异常。"""
    if 200 <= response.status_code < 300:
        return
    content_type = _safe_text(response.headers.get("Content-Type")) or "-"
    raise MabangApiError(
        f"{context}: HTTP响应异常 status={response.status_code}, "
        f"content_type={content_type}, final_url={_safe_url_for_log(response.url)}"
    )


def _validate_inventory_page_response(response: httpx.Response, *, context: str) -> None:
    """HTTP 200 登录页/马帮错误页不能作为可用库存上下文缓存。"""
    page_html = _decode_html_page(response)
    _raise_if_session_expired_response(response, context=context, response_text=page_html)
    _raise_for_status_safely(response, context=context)
    title = _strip_html(_extract_first(r"(?is)<title\b[^>]*>(.*?)</title>", page_html))
    if not response.content or any(marker in title for marker in ("页面无法访问", "无权访问", "访问被拒绝")):
        raise MabangApiError(
            f"{context}: 库存页面不可用 status={response.status_code}, "
            f"final_url={_safe_url_for_log(response.url)}"
        )


def _response_looks_like_login(response: httpx.Response, *, response_text: str | None = None) -> bool:
    """保守识别登录页或明确的会话失效响应。"""
    if response.status_code in {401, 419, 440}:
        return True

    final_path = urlparse(str(response.url)).path.rstrip("/").lower()
    if final_path == LOGIN_PAGE_PATH.lower():
        return True

    location = _safe_text(response.headers.get("Location"))
    if location:
        location_path = urlparse(urljoin(str(response.url), location)).path.rstrip("/").lower()
        if location_path == LOGIN_PAGE_PATH.lower():
            return True

    page_text = response_text if response_text is not None else _decode_html_page(response)
    normalized = html.unescape(page_text).lower()
    content_type = _safe_text(response.headers.get("Content-Type")).lower()
    if "html" not in content_type and not normalized.lstrip().startswith("<"):
        return False
    return "mod=main.dologin" in normalized and bool(
        re.search(r"\btype\s*=\s*(?:['\"]password['\"]|password\b)", normalized)
    )


def _raise_if_session_expired_response(
        response: httpx.Response,
        *,
        context: str,
        response_text: str | None = None,
) -> None:
    """会话失效时抛出专用异常，异常文本不包含响应正文或URL参数。"""
    if not _response_looks_like_login(response, response_text=response_text):
        return
    content_type = _safe_text(response.headers.get("Content-Type")) or "-"
    raise MabangSessionExpiredError(
        f"{context}: 马帮登录会话已失效 "
        f"status={response.status_code}, content_type={content_type}, "
        f"final_url={_safe_url_for_log(response.url)}"
    )


def _looks_like_direct_iframe_content(page_path: str, page_html: str) -> bool:
    """仅允许库存列表页在确有业务标记时兼容无iframe直出页面。"""
    if "mod=stock.list" not in page_path.lower():
        return False
    normalized = page_html.lower()
    return bool(re.search(r"\bid\s*=\s*(['\"])sku-status\1", normalized)) or "search-content-text1" in normalized


def _response_json_dict(response: httpx.Response, *, context: str) -> dict[str, Any]:
    """解析接口JSON对象，失败时仅附带不含敏感参数的响应元数据。"""
    _raise_if_session_expired_response(response, context=context)
    try:
        payload = response.json()
    except ValueError as exc:
        content_type = _safe_text(response.headers.get("Content-Type")) or "-"
        raise MabangApiError(
            f"{context} 返回非JSON响应: status={response.status_code}, "
            f"content_type={content_type}, final_url={_safe_url_for_log(response.url)}"
        ) from exc
    if not isinstance(payload, dict):
        raise MabangApiError(f"{context} 返回JSON结构无效: type={type(payload).__name__}")
    return payload


def _extract_first(pattern: str, text: str) -> str:
    """工具函数：快捷提取正则命中的第一个括号结果或首个匹配字符串"""
    if not text:
        return ""
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    if match.lastindex:
        return _clean_fragment(match.group(1))
    return _clean_fragment(match.group(0))


def _strip_html(text: str) -> str:
    """工具函数：移除字符串内的所有HTML节点并压缩占位"""
    if not text:
        return ""
    normalized = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    normalized = re.sub(r"<[^>]+>", " ", normalized)
    return _compact_text(html.unescape(normalized))


def _clean_fragment(value: str | None) -> str:
    """工具函数：无害化处理HTML标签并转移转义实体字符"""
    if value is None:
        return ""
    return _compact_text(html.unescape(re.sub(r"<[^>]+>", " ", value)))


def _compact_text(value: str) -> str:
    """工具函数：压缩连续空行/空格以保证展示紧凑"""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _safe_text(value: Any) -> str:
    """工具函数：强制转化未定义值为可用空字符串或移除首尾空格"""
    return "" if value in {None, ""} else str(value).strip()


def _clean_sales_report_cell(value: Any) -> str:
    """清理马帮 CSV 中用于防止 Excel 科学计数法的制表符和 BOM。"""
    return _safe_text(value).replace("\ufeff", "").replace("\t", "").strip()


def _clone_form_payload(form_data: Any) -> Any:
    """复制表单 payload，兼容 dict 和重复 key 的 tuple 列表。"""
    if hasattr(form_data, "items"):
        return dict(form_data)
    return [(str(key), value) for key, value in list(form_data or [])]


def _set_form_payload_value(form_data: Any, key: str, value: Any) -> Any:
    """设置表单字段，兼容 dict 和 tuple 列表。"""
    if hasattr(form_data, "items"):
        form_data[key] = value
        return form_data
    normalized_key = _safe_text(key)
    filtered = [(item_key, item_value) for item_key, item_value in list(form_data or []) if item_key != normalized_key]
    filtered.append((normalized_key, value))
    return filtered


def _get_form_payload_first(form_data: Any, key: str) -> Any:
    """读取表单字段第一个值，兼容 dict 和 tuple 列表。"""
    if hasattr(form_data, "items"):
        return form_data.get(key)
    for item_key, item_value in list(form_data or []):
        if item_key == key:
            return item_value
    return ""


def _resolve_sales_report_type(
        *,
        report_type: str | None,
        params: dict[str, Any] | None,
        url: str,
) -> str:
    """解析销量报表页面类型，结果用于 reports.exportProductAll 的 types 参数。"""
    normalized = _safe_text(report_type)
    if not normalized:
        mod_value = _safe_text((params or {}).get("mod"))
        if not mod_value:
            mod_value = _extract_first(r"[?&]mod=([^&]+)", url)
        normalized = mod_value.split(".")[-1] if mod_value else ""
    if normalized not in SALES_REPORT_PAGE_PATHS:
        raise MabangApiError(f"不支持的销量报表导出类型: {normalized or '-'}")
    return normalized


def _decode_product_sales_report_csv(content: bytes) -> str:
    """解码马帮按商品导出全部 CSV。"""
    if not content:
        raise MabangApiError("按商品导出全部返回空文件")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = content.decode("utf-8", errors="ignore")
    if text.lstrip("\ufeff \r\n\t").startswith("<"):
        raise MabangApiError(f"按商品导出全部返回的不是 CSV: {_compact_text(_strip_html(text))[:300]}")
    return text


def _parse_product_sales_report_csv_rows(content: bytes) -> list[list[str]]:
    """把马帮原始 CSV 转成旧分页导出的标准列行。"""
    text = _decode_product_sales_report_csv(content)
    reader = csv.reader(StringIO(text))
    try:
        header = [_clean_sales_report_cell(value) for value in next(reader)]
    except StopIteration:
        return []

    required_columns = {
        "SKU": 0,
        "商品中文名": 1,
        "统一成本价": 2,
        "重量(单位:g)": 3,
        "创建时间": 4,
        "销售数量": 5,
    }
    index_map: dict[str, int] = {}
    for column in required_columns:
        try:
            index_map[column] = header.index(column)
        except ValueError as exc:
            preview = "、".join(header[:20])
            raise MabangApiError(f"按商品导出全部 CSV 缺少字段 {column}，实际字段: {preview}") from exc

    rows: list[list[str]] = []
    seen_skus: set[str] = set()
    summary_skus = {"合计", "总计", "Total", "TOTAL"}
    for raw_row in reader:
        def cell(column_name: str) -> str:
            index = index_map[column_name]
            return _clean_sales_report_cell(raw_row[index]) if index < len(raw_row) else ""

        sku = cell("SKU")
        if not sku or sku in seen_skus or sku in summary_skus:
            continue
        seen_skus.add(sku)
        rows.append([
            sku,
            cell("商品中文名"),
            cell("统一成本价"),
            cell("重量(单位:g)"),
            cell("创建时间"),
            cell("销售数量"),
        ])
    return rows


def _parse_product_sales_report_csv_table(content: bytes) -> tuple[list[str], list[list[str]]]:
    """保留马帮“按商品导出全部”CSV 的全部有效表头和原始明细顺序。"""
    text = _decode_product_sales_report_csv(content)
    reader = csv.reader(StringIO(text))
    try:
        raw_header = [_clean_sales_report_cell(value) for value in next(reader)]
    except StopIteration:
        return [], []

    # 马帮偶尔会在行尾返回空列；只移除没有表头的列，避免生成 Unnamed 列。
    selected_indexes = [index for index, value in enumerate(raw_header) if value]
    headers = [raw_header[index] for index in selected_indexes]
    if not headers:
        raise MabangApiError("按商品导出全部 CSV 缺少有效表头")

    rows: list[list[str]] = []
    for raw_row in reader:
        row = [
            _clean_sales_report_cell(raw_row[index]) if index < len(raw_row) else ""
            for index in selected_indexes
        ]
        if any(row):
            rows.append(row)
    return headers, rows


def _write_sales_report_rows_to_xlsx(
        rows: list[list[str]],
        output_path: Path,
        *,
        headers: Sequence[str] = SALES_REPORT_OUTPUT_HEADERS,
) -> None:
    """写出与旧分页导出兼容的 Excel。"""
    workbook = Workbook(write_only=True)
    try:
        worksheet = workbook.create_sheet("Sheet1")
        worksheet.append(list(headers))
        for row in rows:
            worksheet.append(row)
        workbook.save(output_path)
    finally:
        close = getattr(workbook, "close", None)
        if callable(close):
            close()


def _safe_float_number(value: Any) -> float:
    """工具函数：强力清洗带有金融分隔符的价格文字转为浮点型"""
    text = _safe_text(value).replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _extract_order_export_page_url(order_page_html: str, base_url: str) -> str:
    """工具函数：解析HTML提取导出专用的跳转/iframe链接"""
    iframe_src = _extract_first(r'<iframe[^>]+src="([^"]*mod=order\.exportOrderByTemplate[^"]*)"', order_page_html)
    if iframe_src:
        return urljoin(base_url.rstrip("/") + "/", iframe_src)
    absolute_url = _extract_first(r"(https?://[^\"']+mod=order\.exportOrderByTemplate[^\"']*)", order_page_html)
    if absolute_url:
        return absolute_url
    relative_url = _extract_first(r"(index\.php\?mod=order\.exportOrderByTemplate[^\"']*)", order_page_html)
    if relative_url:
        return urljoin(base_url.rstrip("/") + "/", relative_url)
    return ""


def _extract_hidden_form_values(page_html: str, *, form_id: str) -> dict[str, str]:
    """工具函数：解析隐藏式的HTML input字段"""
    form_block = _extract_first(rf'(?is)<form[^>]*id=["\']{re.escape(form_id)}["\'][^>]*>(.*?)</form>', page_html)
    if not form_block:
        return {}
    hidden_pattern = re.compile(
        r'(?is)<input[^>]*type=["\']hidden["\'][^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\'][^>]*>'
    )
    values: dict[str, str] = {}
    for match in hidden_pattern.finditer(form_block):
        values[_safe_text(match.group(1))] = html.unescape(match.group(2) or "")
    return values


def _extract_order_export_field_map(page_html: str) -> dict[str, str]:
    """工具函数：正则匹配界面上的可用导出字段字典"""
    field_pattern = re.compile(
        r'(?is)<input[^>]*name=["\']fieldlabel["\'][^>]*value=["\']([^"\']+)["\'][^>]*>\s*<span>(.*?)</span>'
    )
    field_map: dict[str, str] = {}
    for match in field_pattern.finditer(page_html):
        code = _safe_text(match.group(1))
        if not code or code == "all":
            continue
        label = _compact_text(_strip_html(match.group(2)))
        if not label:
            continue
        field_map[code] = label
    return field_map


def _extract_export_field_map(page_html: str) -> dict[str, str]:
    """工具函数：复合式备用的解析可用导出字段提取"""
    field_map = _extract_order_export_field_map(page_html)
    if field_map:
        return field_map
    label_pattern = re.compile(
        r'(?is)<label[^>]*>(?:(?!</label>).)*?<input[^>]*name=["\']fieldlabel["\'][^>]*value=["\']([^"\']+)["\'][^>]*>(.*?)</label>'
    )
    for match in label_pattern.finditer(page_html):
        code = _safe_text(match.group(1))
        if not code or code == "all":
            continue
        label = _compact_text(_strip_html(match.group(2)))
        if label:
            field_map[code] = label
    return field_map


def _default_order_export_field_codes(field_map: dict[str, str]) -> list[str]:
    """工具函数：获取备选的缺省常用导出字段"""
    preferred = ["uq101", "uq130", "uq135", "uq136", "uq163", "uq282", "uq254", "uq202", "uq143"]
    selected = [code for code in preferred if code in field_map]
    if selected:
        return selected
    return list(field_map.keys())[: min(12, len(field_map))]


def _resolve_export_field_codes(
        field_codes: Sequence[str] | None,
        field_map: dict[str, str],
        *,
        fallback_codes: Sequence[str],
        allow_fallback: bool = True,
) -> list[str]:
    """工具函数：确保选中的字段码或字段中文名存在于支持列内并去重"""
    label_to_codes: dict[str, list[str]] = {}
    for code, label in field_map.items():
        label_text = _safe_text(label)
        if label_text:
            label_to_codes.setdefault(label_text, []).append(code)
    normalized: list[str] = []
    missing: list[str] = []
    ambiguous: list[str] = []
    for code in field_codes or []:
        code_text = _safe_text(code)
        if not code_text:
            continue
        if code_text in field_map and code_text not in normalized:
            normalized.append(code_text)
            continue
        matching_codes = label_to_codes.get(code_text) or []
        if len(matching_codes) == 1:
            matching_code = matching_codes[0]
            if matching_code not in normalized:
                normalized.append(matching_code)
            continue
        if len(matching_codes) > 1:
            ambiguous.append(code_text)
            continue
        missing.append(code_text)
    if ambiguous:
        raise MabangApiError(f"订单导出字段中文名不唯一，请直接指定字段码: {', '.join(ambiguous)}")
    if missing and not normalized:
        raise MabangApiError(f"订单导出字段不存在: {', '.join(missing)}")
    if missing:
        raise MabangApiError(f"订单导出字段部分不存在: {', '.join(missing)}")
    if normalized:
        return normalized
    if not allow_fallback:
        return []
    fallback: list[str] = []
    for code in fallback_codes:
        code_text = _safe_text(code)
        if code_text in field_map and code_text not in fallback:
            fallback.append(code_text)
    return fallback


def _extract_missing_order_ids(payload: dict[str, Any]) -> list[str]:
    """工具函数：解析因为风控或其他异常而提取不到的订单列表"""
    raw = _safe_text(payload.get("errorOrderPlatformIdData"))
    if not raw:
        return []
    tokens = re.split(r"[\s,，;；\n]+", raw)
    return [token for token in (_safe_text(item) for item in tokens) if token]


def _is_retryable_order_export_failure(payload: dict[str, Any]) -> bool:
    """工具函数：判断某个异常信息是否适合让系统进行重试（例如token超时等）"""
    message = _safe_text(payload.get("message"))
    if not message:
        return False
    retryable_keywords = ["参数丢失", "操作不合法", "非法操作", "会话", "超时", "token", "csrf"]
    return any(keyword.lower() in message.lower() for keyword in retryable_keywords)


def _guess_download_filename(content_disposition: str | None, file_url: str) -> str:
    """工具函数：探测返回下载文件的官方预设的文件名称"""
    header_text = _safe_text(content_disposition)
    if header_text:
        match = re.search(r"filename\*=UTF-8''([^;]+)", header_text, flags=re.IGNORECASE)
        if match:
            return unquote(match.group(1))
        match = re.search(r'filename="?([^";]+)"?', header_text, flags=re.IGNORECASE)
        if match:
            return unquote(match.group(1))
    parsed = urlparse(file_url)
    file_name = Path(parsed.path).name
    return file_name or f"order_export_{int(time.time())}.xlsx"


def _resolve_order_export_action_url(export_page_url: str | None, fallback_base_url: str) -> str:
    """工具函数：获取导出的表单提交最终路径地址"""
    page_url = _safe_text(export_page_url)
    if page_url:
        parsed = urlparse(page_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}{ORDER_EXPORT_ACTION_PATH}"
    return f"{fallback_base_url.rstrip('/')}{ORDER_EXPORT_ACTION_PATH}"


def _resolve_export_download_url(file_url: str, export_page_url: str | None, fallback_base_url: str) -> str:
    """工具函数：组装出正确的最终下载URL"""
    target = _safe_text(file_url)
    if not target:
        return ""
    if target.startswith("http://") or target.startswith("https://"):
        return target
    page_url = _safe_text(export_page_url)
    if page_url:
        return urljoin(page_url, target)
    return urljoin(fallback_base_url.rstrip('/') + '/', target)


def _merge_order_export_step1_payload(form_defaults: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    """工具函数：合并表单初始默认值和额外请求参数"""
    merged: dict[str, Any] = dict(form_defaults or {})
    explicit_keys = {"orderIds", "fieldlabel", "map-uq[]", "map-name[]", "map-text[]", "tableBase", "hbddgyxx", "step1"}
    for key, value in payload.items():
        key_text = _safe_text(key)
        if not key_text:
            continue
        if key_text in explicit_keys or key_text not in merged:
            merged[key_text] = value
    return merged


def _flatten_form_payload(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """工具函数：拍扁表单参数里的各种嵌套列表供urlencode使用"""
    pairs: list[tuple[str, str]] = []
    for key, value in payload.items():
        key_text = _safe_text(key)
        if not key_text:
            continue
        if isinstance(value, (list, tuple, set)):
            for item in value:
                pairs.append((key_text, _safe_text(item)))
            continue
        pairs.append((key_text, _safe_text(value)))
    return pairs


def _contains_completed_status(*values: str) -> bool:
    """工具函数：嗅探值内是否包含已完成标志"""
    keywords = ["已完成", "complete", "completed"]
    for value in values:
        normalized = _safe_text(value).lower()
        if normalized and any(keyword in normalized for keyword in keywords):
            return True
    return False


def _mask_username(username: str) -> str:
    """工具函数：用以生成打码掩码后的账户名称"""
    if len(username) <= 4:
        return username
    return f"{username[:3]}****{username[-3:]}"
