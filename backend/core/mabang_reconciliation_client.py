"""Request-based Mabang ERP client for KEC reconciliation.

Ported from ``finance_process/app/modules/seaya_reconciliation/mabang_client.py``.
Provides batch order lookup by platform order ID or waybill number, plus the
return and refund list lookups used by the outbound and return checks.
"""

from __future__ import annotations

import html
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
from urllib.parse import urljoin

import httpx

LOGGER = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_WORKERS = 1
DEFAULT_ORDER_BATCH_SIZE = 2000
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 2
LOGIN_PAGE_PATH = "/index.htm"
LOGIN_PATH = "/index.php?mod=main.doLogin&lang=cn"
ORDER_PAGE_PATH = "/index.php?mod=order.list&Order_orderStatus=2"
ORDER_QUERY_PATH = "/index.php?mod=order.oTc"
RETURNS_PAGE_PATH = "/index.php?mod=order.refundsOrderList"
REFUNDS_PAGE_PATH = "/index.php?mod=paypalconfig.paypalorderlist"
RETURNS_QUERY_URL = "https://private-amz.mabangerp.com/index.php?mod=order.getRefundsOrderList"
REFUNDS_QUERY_URL = "https://private-amz.mabangerp.com/index.php?mod=paypal.dosearchpaypalrefund"
NO_DATA_MARKER = "group-nodata"


class MabangApiError(RuntimeError):
    """Raised when the Mabang ERP request flow fails."""


@dataclass(slots=True)
class MabangOrderInfo:
    tracking_number: str
    platform_order_id: str
    shop_name: str = ""
    platform_name: str = ""
    show_order_status_text: str = ""
    platform_order_status: str = ""
    order_status: str = ""
    refund_flag: str = ""
    created_at: str = ""
    express_time: str = ""
    origin_transport_time: str = ""
    order_deliver_time: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_completed(self) -> bool:
        return _contains_completed_status(
            self.show_order_status_text,
            self.platform_order_status,
            self.order_status,
        )

    @property
    def shipping_time_text(self) -> str:
        for value in (self.express_time, self.origin_transport_time, self.order_deliver_time):
            normalized = _safe_text(value)
            if normalized:
                return normalized
        return ""

    @property
    def order_id(self) -> str:
        return _safe_text(self.raw_data.get("id") or self.raw_data.get("orderId"))

    @property
    def table_base(self) -> str:
        return _safe_text(self.raw_data.get("tableBase") or self.raw_data.get("post_tableBase") or "2")

    @property
    def item_summary_text(self) -> str:
        return _safe_text(
            self.raw_data.get("order_ellipsis_title")
            or self.raw_data.get("order_ellipsis_other_title")
            or self.raw_data.get("order_ellipsis_text")
            or self.raw_data.get("order_ellipsis_other_text")
        )

    @property
    def item_kind_count(self) -> int | None:
        return _extract_order_item_summary_count(self.item_summary_text, "商品种类")

    @property
    def item_total_quantity(self) -> int | None:
        return _extract_order_item_summary_count(self.item_summary_text, "商品个数")

@dataclass(slots=True)
class MabangReturnRecord:
    tracking_number: str
    platform_order_id: str
    return_order_no: str = ""
    platform_return_order_no: str = ""
    platform_order_status: str = ""
    return_status: str = ""
    register_time: str = ""
    raw_summary_text: str = ""


@dataclass(slots=True)
class MabangRefundRecord:
    platform_order_id: str
    refund_id: str = ""
    refund_order_no: str = ""
    refund_status: str = ""
    refund_reason: str = ""
    order_status_at_refund: str = ""
    refund_date: str = ""
    raw_summary_text: str = ""


@dataclass(slots=True)
class MabangLookupResult:
    tracking_number: str
    order: MabangOrderInfo | None = None
    return_records: list[MabangReturnRecord] = field(default_factory=list)
    refund_records: list[MabangRefundRecord] = field(default_factory=list)
    error_message: str = ""

    @property
    def has_return_hit(self) -> bool:
        return bool(self.return_records)

    @property
    def has_refund_hit(self) -> bool:
        return bool(self.refund_records)


class MabangClient:
    """Request-based Mabang ERP client for order, return, and refund lookups."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        verify_ssl: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.verify_ssl = verify_ssl
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            verify=verify_ssl,
            trust_env=False,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        self._logged_in = False
        self._order_page_key: str | None = None
        self._order_tab_id: str | None = None
        self._returns_bootstrapped = False
        self._refunds_bootstrapped = False

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "MabangClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def login(self, username: str, password: str) -> None:
        LOGGER.info("登录马帮 ERP: %s", _mask_username(username))
        login_page = self.client.get(self._url(LOGIN_PAGE_PATH))
        login_page.raise_for_status()
        response = self.client.post(
            self._url(LOGIN_PATH),
            data={
                "username": username,
                "password": password,
                "loginEntrance": "1",
                "timezone": "UTC+8",
            },
            headers={
                "Referer": str(login_page.url),
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise MabangApiError(f"马帮登录失败: {payload}")
        self._logged_in = True

    def search_order_by_tracking_number(self, tracking_number: str) -> MabangOrderInfo | None:
        self._ensure_order_context()
        response = self.client.post(
            self._url(ORDER_QUERY_PATH),
            data={
                **self._build_order_payload_base(),
                "OrderSearchFuSKey": "a.trackNumber",
                "OrderSearch.fuzzySearchKey": "Order.trackNumber",
                "OrderSearch.fuzzySearchValue": tracking_number,
            },
            headers=self._order_headers(),
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise MabangApiError(f"马帮订单查询失败: {payload}")

        rows = payload.get("orderDataList") or []
        if not rows:
            return None

        normalized_tracking = tracking_number.strip()
        selected = None
        for row in rows:
            if _safe_text(row.get("trackNumber")) == normalized_tracking:
                selected = row
                break
        if selected is None:
            selected = rows[0]

        return self._build_order_info(selected, fallback_tracking_number=normalized_tracking)

    def search_orders_by_tracking_numbers(
        self,
        tracking_numbers: Sequence[str],
        *,
        batch_size: int = DEFAULT_ORDER_BATCH_SIZE,
        fallback_single: bool = True,
    ) -> dict[str, MabangOrderInfo]:
        """Batch query orders by waybill number and fall back to single lookups for misses."""
        self._ensure_order_context()
        deduplicated = list(dict.fromkeys(_safe_text(value) for value in tracking_numbers if _safe_text(value)))
        if not deduplicated:
            return {}

        if batch_size <= 0:
            batch_size = DEFAULT_ORDER_BATCH_SIZE
        batch_size = min(batch_size, DEFAULT_ORDER_BATCH_SIZE)

        total_batches = (len(deduplicated) + batch_size - 1) // batch_size
        results: dict[str, MabangOrderInfo] = {}
        missing_tracking_numbers: list[str] = []

        for batch_index in range(total_batches):
            chunk = deduplicated[batch_index * batch_size : (batch_index + 1) * batch_size]
            chunk_results, chunk_missing = self._search_orders_by_tracking_chunk(chunk)
            LOGGER.info(
                "马帮货运单批量查询 %s/%s 命中 %s 个订单",
                batch_index + 1,
                total_batches,
                len(chunk_results),
            )
            for tracking_number, order in chunk_results.items():
                if tracking_number not in results:
                    results[tracking_number] = order
            missing_tracking_numbers.extend(chunk_missing)

        if missing_tracking_numbers and fallback_single:
            LOGGER.info(
                "马帮货运单批量查询未命中 %s 个，开始单号回补",
                len(missing_tracking_numbers),
            )
            for tracking_number in missing_tracking_numbers:
                if tracking_number in results:
                    continue
                order = self.search_order_by_tracking_number(tracking_number)
                if order is not None:
                    results[tracking_number] = order

        return results

    def _search_orders_by_tracking_chunk(self, chunk: Sequence[str]) -> tuple[dict[str, MabangOrderInfo], list[str]]:
        chunk_results: dict[str, MabangOrderInfo] = {}
        matched_tracking_numbers: set[str] = set()
        page = 1
        while True:
            response = self.client.post(
                self._url(ORDER_QUERY_PATH),
                data={
                    **self._build_order_payload_base(rows_per_page=100, page=page),
                    "OrderSearch.batchSearch": "Order.trackNumber",
                    "OrderSearch.batchSearchValue": ",".join(chunk),
                    "OrderSearch.fuzzySearchConditions3": "1",
                },
                headers=self._order_headers(),
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("success"):
                raise MabangApiError(f"马帮货运单批量查询失败: {payload}")

            rows = payload.get("orderDataList") or []
            LOGGER.info(
                "马帮货运单批量查询页 %s 命中 %s 个订单",
                page,
                len(rows),
            )
            for row in rows:
                order = self._build_order_info(row)
                tracking_number = order.tracking_number
                if tracking_number:
                    matched_tracking_numbers.add(tracking_number)
                    if tracking_number not in chunk_results:
                        chunk_results[tracking_number] = order

            if len(rows) < 100:
                break
            page += 1

        missing_tracking_numbers = [tracking_number for tracking_number in chunk if tracking_number not in matched_tracking_numbers]
        return chunk_results, missing_tracking_numbers

    def _search_orders_by_platform_order_id_chunk(self, chunk: Sequence[str]) -> dict[str, MabangOrderInfo]:
        chunk_results: dict[str, MabangOrderInfo] = {}
        rows_per_page = 500
        page = 1
        while True:
            payload = self._post_form_with_retry(
                self._url(ORDER_QUERY_PATH),
                data={
                    **self._build_order_payload_base(rows_per_page=rows_per_page, page=page),
                    "OrderSearch.batchSearch": "Order.platformOrderId",
                    "OrderSearch.batchSearchValue": ",".join(chunk),
                    "OrderSearch.fuzzySearchConditions3": "1",
                },
                headers=self._order_headers(),
            )

            rows = payload.get("orderDataList") or []
            LOGGER.info(
                "Mabang platform order batch page %s hit %s orders",
                page,
                len(rows),
            )
            for row in rows:
                order = self._build_order_info(row)
                if order.platform_order_id and order.platform_order_id not in chunk_results:
                    chunk_results[order.platform_order_id] = order

            if len(rows) < rows_per_page:
                break
            page += 1

        return chunk_results

    def search_orders_by_platform_order_ids(
        self,
        platform_order_ids: Sequence[str],
        *,
        batch_size: int = DEFAULT_ORDER_BATCH_SIZE,
    ) -> dict[str, MabangOrderInfo]:
        """?????????????????????? 2000 ??"""
        self._ensure_order_context()
        deduplicated = list(dict.fromkeys(_safe_text(value) for value in platform_order_ids if _safe_text(value)))
        if not deduplicated:
            return {}

        if batch_size <= 0:
            batch_size = DEFAULT_ORDER_BATCH_SIZE
        batch_size = min(batch_size, DEFAULT_ORDER_BATCH_SIZE)

        total_batches = (len(deduplicated) + batch_size - 1) // batch_size
        results: dict[str, MabangOrderInfo] = {}
        for batch_index in range(total_batches):
            chunk = deduplicated[batch_index * batch_size : (batch_index + 1) * batch_size]
            chunk_results = self._search_orders_by_platform_order_id_chunk(chunk)
            LOGGER.info(
                "Mabang platform order batch %s/%s hit %s orders",
                batch_index + 1,
                total_batches,
                len(chunk_results),
            )
            for platform_order_id, order in chunk_results.items():
                if platform_order_id not in results:
                    results[platform_order_id] = order

        return results

    def search_returns_by_tracking_number(self, tracking_number: str) -> list[MabangReturnRecord]:
        self._ensure_returns_context()
        payload = self._post_form_with_retry(
            RETURNS_QUERY_URL,
            data={
                "searchCnt": "trackNumber",
                "search-content-text1": tracking_number,
                "page": "1",
                "rowsPerPage": "20",
                "act": "refundsOrderList",
            },
            headers={
                "Referer": RETURNS_PAGE_PATH,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        return parse_return_records(payload.get("message") or "", default_tracking_number=tracking_number)

    def search_refunds_by_platform_order_id(self, platform_order_id: str) -> list[MabangRefundRecord]:
        self._ensure_refunds_context()
        payload = self._post_form_with_retry(
            REFUNDS_QUERY_URL,
            data={
                "search-content": "platformOrderId",
                "search-content-text": platform_order_id,
                "page": "1",
                "rowsPerPage": "20",
            },
            headers={
                "Referer": REFUNDS_PAGE_PATH,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        return parse_refund_records(payload.get("message") or "")

    def _build_order_info(
        self,
        row: dict[str, Any],
        *,
        fallback_tracking_number: str = "",
    ) -> MabangOrderInfo:
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

    def _build_order_payload_base(self, *, rows_per_page: int = 100, page: int = 1) -> dict[str, str]:
        return {
            "orderPageKey": self._order_page_key or "",
            "goPaypalRefundStatus": "1",
            "page": str(page),
            "rowsPerPage": str(rows_per_page),
            "Order_isCloud": "2",
            "m": "order",
            "a": "orderalllist",
            "isNewOrderPage": "1",
            "post_tableBase": "2",
            "tabId": self._order_tab_id or "222",
            "Order.orderStatus": "",
            "startPageNum": "",
            "endPageNum": "",
        }

    def _post_form_with_retry(
        self,
        url: str,
        *,
        data: dict[str, Any],
        headers: dict[str, str],
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS,
    ) -> dict[str, Any]:
        last_payload: dict[str, Any] | None = None
        for attempt in range(1, retry_attempts + 1):
            response = self.client.post(url, data=data, headers=headers)
            response.raise_for_status()
            payload = response.json()
            last_payload = payload
            if payload.get("success"):
                return payload
            if not _is_retryable_mabang_payload(payload) or attempt >= retry_attempts:
                raise MabangApiError(f"Mabang request failed: {payload}")
            LOGGER.info("Mabang interface busy, retry %s/%s after %ss", attempt, retry_attempts, backoff_seconds)
            time.sleep(backoff_seconds * attempt)
        raise MabangApiError(f"Mabang request failed: {last_payload}")

    def _order_headers(self) -> dict[str, str]:
        return {
            "Referer": self._url(ORDER_PAGE_PATH),
            "X-Requested-With": "XMLHttpRequest",
        }

    def _ensure_order_context(self) -> None:
        if self._order_page_key and self._order_tab_id:
            return
        self._ensure_logged_in()
        response = self.client.get(self._url(ORDER_PAGE_PATH))
        response.raise_for_status()
        page_html = _decode_html_page(response)
        self._order_page_key = _extract_first(r"var\s+orderPageKey\s*=\s*'([^']+)'", page_html)
        self._order_tab_id = (
            _extract_first(r'<li[^>]*class="[^"]*active[^"]*"[^>]*data-id="([^"]+)"', page_html)
            or _extract_first(r'<li[^>]*data-id="([^"]+)"[^>]*class="[^"]*active[^"]*"', page_html)
            or "222"
        )
        if not self._order_page_key:
            raise MabangApiError("未能从马帮订单页解析 orderPageKey")

    def _ensure_returns_context(self) -> None:
        if self._returns_bootstrapped:
            return
        self._ensure_logged_in()
        response = self.client.get(self._url(RETURNS_PAGE_PATH))
        response.raise_for_status()
        page_html = _decode_html_page(response)
        iframe_src = _extract_first(
            r'<iframe[^>]+src="([^"]*mod=order\.refundsOrderList[^"]*)"',
            page_html,
        )
        if iframe_src:
            iframe_response = self.client.get(urljoin(self.base_url + "/", iframe_src))
            iframe_response.raise_for_status()
        self._returns_bootstrapped = True

    def _ensure_refunds_context(self) -> None:
        if self._refunds_bootstrapped:
            return
        self._ensure_logged_in()
        response = self.client.get(self._url(REFUNDS_PAGE_PATH))
        response.raise_for_status()
        page_html = _decode_html_page(response)
        iframe_src = _extract_first(
            r'<iframe[^>]+src="([^"]*mod=paypal\.paypalrefund[^"]*)"',
            page_html,
        )
        if iframe_src:
            iframe_response = self.client.get(urljoin(self.base_url + "/", iframe_src))
            iframe_response.raise_for_status()
        self._refunds_bootstrapped = True

    def _ensure_logged_in(self) -> None:
        if not self._logged_in:
            raise MabangApiError("马帮客户端尚未登录")

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}{path}"


class MabangLookupCoordinator:
    """Parallel lookup helper for Mabang return-to-warehouse reconciliation."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        verify_ssl: bool = False,
    ) -> None:
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.verify_ssl = verify_ssl

    def lookup_tracking_numbers(
        self,
        *,
        tracking_numbers: Iterable[str],
        username: str,
        password: str,
        max_workers: int = DEFAULT_MAX_WORKERS,
    ) -> dict[str, MabangLookupResult]:
        ordered_unique_tracking_numbers = list(dict.fromkeys(_safe_text(value) for value in tracking_numbers if _safe_text(value)))
        if not ordered_unique_tracking_numbers:
            return {}

        worker_count = max(1, min(max_workers, len(ordered_unique_tracking_numbers)))
        chunks = _split_tracking_numbers(ordered_unique_tracking_numbers, worker_count)
        results: dict[str, MabangLookupResult] = {}

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(
                    self._lookup_chunk,
                    chunk,
                    username,
                    password,
                    worker_index + 1,
                ): chunk
                for worker_index, chunk in enumerate(chunks)
                if chunk
            }
            for future in as_completed(future_map):
                results.update(future.result())

        return results

    def _lookup_chunk(
        self,
        chunk: list[str],
        username: str,
        password: str,
        worker_index: int,
    ) -> dict[str, MabangLookupResult]:
        chunk_results: dict[str, MabangLookupResult] = {}
        with MabangClient(
            self.base_url,
            timeout_seconds=self.timeout_seconds,
            verify_ssl=self.verify_ssl,
        ) as client:
            client.login(username, password)
            try:
                batch_results = client.search_orders_by_tracking_numbers(chunk)
            except Exception as exc:
                LOGGER.warning("Mabang batch order lookup failed, fallback to single lookup: %s", exc)
                batch_results = {}
            for item_index, tracking_number in enumerate(chunk, start=1):
                LOGGER.info(
                    "马帮货运单查询 [worker %s] %s/%s: %s",
                    worker_index,
                    item_index,
                    len(chunk),
                    tracking_number,
                )
                try:
                    order = batch_results.get(tracking_number) or client.search_order_by_tracking_number(tracking_number)
                    return_records: list[MabangReturnRecord] = []
                    refund_records: list[MabangRefundRecord] = []
                    if order is not None:
                        return_records = client.search_returns_by_tracking_number(tracking_number)
                        if order.platform_order_id:
                            refund_records = client.search_refunds_by_platform_order_id(order.platform_order_id)
                    chunk_results[tracking_number] = MabangLookupResult(
                        tracking_number=tracking_number,
                        order=order,
                        return_records=return_records,
                        refund_records=refund_records,
                    )
                except Exception as exc:  # pragma: no cover - defensive guard for live requests
                    LOGGER.exception("马帮货运单查询失败: %s", tracking_number)
                    chunk_results[tracking_number] = MabangLookupResult(
                        tracking_number=tracking_number,
                        error_message=str(exc),
                    )
        return chunk_results


def parse_return_records(message_html: str, *, default_tracking_number: str = "") -> list[MabangReturnRecord]:
    if not message_html or NO_DATA_MARKER in message_html:
        return []

    records: list[MabangReturnRecord] = []
    for title_html, content_html in _iter_row_pairs(message_html):
        title_text = _strip_html(title_html)
        content_text = _strip_html(content_html)
        records.append(
            MabangReturnRecord(
                tracking_number=(
                    _extract_first(r"【([^】]+)】", content_text)
                    or default_tracking_number
                ),
                platform_order_id=_extract_first(r"platformOrderId=([^\"&]+)", title_html + content_html),
                return_order_no=_extract_first(r"<input[^>]*>\s*([^<\s]+)\s*<span", title_html),
                platform_return_order_no=(
                    _extract_first(r"return_tracknumber&quot;:&quot;([^\"&]+)", content_html)
                    or _extract_first(r"平台退货单编号[:：]\s*([^\s]+)", title_text)
                ),
                platform_order_status=_extract_first(r'data-field="ro_platformOrderStatus"[^>]*>(.*?)<', content_html),
                return_status=_extract_first(r'data-field="ro_returnStatus"[^>]*>(.*?)<', content_html),
                register_time=_extract_first(r"登记[:：]\s*([0-9:\- ]+)", title_text + " " + content_text),
                raw_summary_text=_compact_text(f"{title_text} | {content_text}"),
            )
        )
    return records


def parse_refund_records(message_html: str) -> list[MabangRefundRecord]:
    if not message_html or NO_DATA_MARKER in message_html:
        return []

    records: list[MabangRefundRecord] = []
    for title_html, content_html in _iter_row_pairs(message_html):
        title_text = _strip_html(title_html)
        content_text = _strip_html(content_html)
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", content_text)
        records.append(
            MabangRefundRecord(
                platform_order_id=_extract_first(r"platformOrderId=([^\"&]+)", title_html),
                refund_id=_extract_first(r'data-id="([^"]+)"', content_html),
                refund_order_no=(
                    _extract_first(r'<td class="pct12">.*?<span[^>]*title="([^"]+)"', content_html)
                    or _extract_first(r"\bTKD\d+\b", content_text)
                ),
                refund_status=(
                    _extract_first(r'text-success">([^<]+)<', content_html)
                    or _extract_first(r'<td class="text-center">\s*<span[^>]*>([^<]+)</span>', content_html)
                ),
                refund_reason=(
                    _extract_first(r'error-text fixed" title="([^"]*)"', content_html)
                    or _extract_first(r"(Failed Delivery|[A-Za-z ]+)", content_text)
                ),
                order_status_at_refund=_extract_first(r"创建退款时订单状态[:：]\s*([^\s]+)", title_text + " " + content_text),
                refund_date=dates[0] if dates else "",
                raw_summary_text=_compact_text(f"{title_text} | {content_text}"),
            )
        )
    return records


def _iter_row_pairs(message_html: str) -> list[tuple[str, str]]:
    pair_pattern = re.compile(
        r'(?is)(<tr[^>]*class="title[^"]*"[^>]*>.*?</tr>)\s*(<tr[^>]*class="content[^"]*"[^>]*>.*?</tr>)'
    )
    return [(match.group(1), match.group(2)) for match in pair_pattern.finditer(message_html)]


def _decode_html_page(response: httpx.Response) -> str:
    for encoding in [response.encoding, "gb18030", "utf-8"]:
        if not encoding:
            continue
        try:
            return response.content.decode(encoding, errors="ignore")
        except LookupError:
            continue
    return response.text


def _extract_first(pattern: str, text: str) -> str:
    if not text:
        return ""
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    if match.lastindex:
        return _clean_fragment(match.group(1))
    return _clean_fragment(match.group(0))


def _strip_html(text: str) -> str:
    if not text:
        return ""
    normalized = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    normalized = re.sub(r"<[^>]+>", " ", normalized)
    return _compact_text(html.unescape(normalized))


def _clean_fragment(value: str | None) -> str:
    if value is None:
        return ""
    return _compact_text(html.unescape(re.sub(r"<[^>]+>", " ", value)))


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _contains_completed_status(*values: str) -> bool:
    completed_keywords = ["已完成", "complete", "completed"]
    for value in values:
        lowered = _safe_text(value).lower()
        if any(keyword in lowered for keyword in completed_keywords):
            return True
    return False


def _extract_order_item_summary_count(summary_text: str, label: str) -> int | None:
    match = re.search(rf"{re.escape(label)}\s*[:：]\s*(\d+)", summary_text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _safe_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()

def _is_retryable_mabang_payload(payload: dict[str, Any]) -> bool:
    message_text = _safe_text(payload.get("message"))
    normalized = message_text.lower()
    retry_keywords = ["请稍后再试", "稍后再试", "busy", "retry", "频繁", "too many requests"]
    return any(keyword in normalized or keyword in message_text for keyword in retry_keywords)


def _mask_username(username: str) -> str:
    normalized = _safe_text(username)
    if not normalized:
        return ""
    return "[REDACTED]"


def _split_tracking_numbers(tracking_numbers: list[str], worker_count: int) -> list[list[str]]:
    if worker_count <= 1:
        return [tracking_numbers]
    chunks: list[list[str]] = [[] for _ in range(worker_count)]
    for index, tracking_number in enumerate(tracking_numbers):
        chunks[index % worker_count].append(tracking_number)
    return [chunk for chunk in chunks if chunk]

