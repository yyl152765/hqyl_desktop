from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd
import requests


ProgressCallback = Callable[[str], None]

WEB_BASE_URL = "https://echotik.live"
API_BASE_URL = f"{WEB_BASE_URL}/api/v1"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 50
MAX_PAGES = 1000
DEFAULT_MAX_PRODUCTS = 200
MAX_PRODUCT_LIMIT = 10000
PAGINATION_RECOVERY_PASSES = 5
PRIMARY_PRODUCT_ORDER = "total_sale_nd_cnt"
PRODUCT_PAGINATION_RECOVERY_ORDERS = (
    ("total_sale_nd_cnt", "asc", "周期销量升序"),
    ("total_sale_cnt", "desc", "累计销量降序"),
    ("total_sale_cnt", "asc", "累计销量升序"),
    ("total_sale_gmv_nd_amt", "desc", "周期 GMV 降序"),
    ("total_sale_gmv_nd_amt", "asc", "周期 GMV 升序"),
    ("total_sale_gmv_amt", "desc", "累计 GMV 降序"),
    ("total_sale_gmv_amt", "asc", "累计 GMV 升序"),
    ("influencers_count", "desc", "关联达人数降序"),
    ("influencers_count", "asc", "关联达人数升序"),
    ("videos_count", "desc", "关联视频数降序"),
    ("videos_count", "asc", "关联视频数升序"),
    ("avg_price", "desc", "价格降序"),
    ("avg_price", "asc", "价格升序"),
)
REQUEST_MAX_ATTEMPTS = 3
REQUEST_RETRY_BACKOFF_SECONDS = (2.0, 5.0)
HTML_RETRY_BACKOFF_SECONDS = (30.0, 60.0)
SESSION_REQUEST_BUDGET = 450
SESSION_RENEWAL_COOLDOWN_SECONDS = 15.0
SUSTAINED_HTML_FAILURE_LIMIT = 2
RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
THAILAND_REGION = "TH"
ALL_PRODUCTS_LABEL = "全部商品"
KEYWORD_SEPARATOR = re.compile(r"[\r\n,，;；]+")

PRODUCT_COLUMNS = (
    "keyword",
    "product_id",
    "product_title",
    "product_url",
    "product_image",
    "shop_id",
    "shop_name",
    "price",
    "currency",
    "recent_7d_sales",
    "recent_7d_gmv",
    "total_sales",
    "total_gmv",
    "creator_count",
    "video_count",
    "video_play_count",
    "category",
    "raw_category",
    "source_creator_count",
    "matched_creator_count",
    "filtered_creator_count",
    "collected_at",
    "error",
)

CREATOR_COLUMNS = (
    "keyword",
    "product_id",
    "product_title",
    "creator_id",
    "creator_uid",
    "creator_name",
    "creator_avatar",
    "country",
    "fans_count",
    "likes_count",
    "creator_categories",
    "creator_sales",
    "creator_sales_value",
    "product_gmv",
    "product_gmv_value",
    "video_count",
    "video_play_count",
    "video_play_count_value",
    "fans_count_value",
    "live_count",
    "live_view_count",
    "creator_url",
    "collected_at",
    "error",
)


@dataclass(frozen=True)
class NumberRange:
    minimum: Decimal | None = None
    maximum: Decimal | None = None

    @property
    def enabled(self) -> bool:
        return self.minimum is not None or self.maximum is not None


@dataclass(frozen=True)
class ProductFilters:
    category_ids: tuple[str, ...] = ()
    category_names: tuple[str, ...] = ()
    category_paths: tuple[tuple[str, ...], ...] = ()
    sales_period_days: int = 7
    period_sales: NumberRange = field(default_factory=NumberRange)
    total_sales: NumberRange = field(default_factory=NumberRange)
    video_views: NumberRange = field(default_factory=NumberRange)
    video_count: NumberRange = field(default_factory=NumberRange)
    creator_count: NumberRange = field(default_factory=NumberRange)

    @property
    def enabled(self) -> bool:
        return bool(
            self.category_ids
            or self.category_names
            or self.category_paths
            or self.period_sales.enabled
            or self.total_sales.enabled
            or self.video_views.enabled
            or self.video_count.enabled
            or self.creator_count.enabled
        )


@dataclass(frozen=True)
class CreatorFilters:
    category_names: tuple[str, ...] = ()
    sales: NumberRange = field(default_factory=NumberRange)
    video_play_count: NumberRange = field(default_factory=NumberRange)
    fans_count: NumberRange = field(default_factory=NumberRange)
    video_count: NumberRange = field(default_factory=NumberRange)
    product_gmv: NumberRange = field(default_factory=NumberRange)

    @property
    def enabled(self) -> bool:
        return bool(
            self.category_names
            or self.sales.enabled
            or self.video_play_count.enabled
            or self.fans_count.enabled
            or self.video_count.enabled
            or self.product_gmv.enabled
        )


@dataclass(frozen=True)
class EchoTikCollectJob:
    username: str
    password: str
    keywords: tuple[str, ...]
    output_dir: Path
    page_size: int = DEFAULT_PAGE_SIZE
    max_products: int = DEFAULT_MAX_PRODUCTS
    product_delay_seconds: float = 0.6
    creator_delay_seconds: float = 0.8
    max_pages: int = MAX_PAGES
    fetch_product_details: bool = False
    product_filters: ProductFilters = field(default_factory=ProductFilters)
    creator_filters: CreatorFilters = field(default_factory=CreatorFilters)


@dataclass(frozen=True)
class EchoTikCollectResult:
    product_candidate_count: int
    product_count: int
    creator_source_count: int
    creator_count: int
    creator_filtered_count: int
    creator_missing_metric_count: int
    failed_product_count: int
    output_file: Path
    output_dir: Path
    products_preview: list[dict[str, Any]]
    creators_preview: list[dict[str, Any]]


@dataclass(frozen=True)
class EchoTikPage:
    rows: list[dict[str, Any]]
    total: int | None = None
    last_page: int | None = None
    current_page: int | None = None


@dataclass(frozen=True)
class CreatorCollectionStats:
    source_count: int
    matched_count: int
    missing_metric_count: int
    warning: str = ""


class EchoTikApiError(RuntimeError):
    """Raised when EchoTik returns an HTTP or business-level error."""


class EchoTikHtmlResponseError(EchoTikApiError):
    """Raised after EchoTik repeatedly returns an HTML error page to an API call."""


class EchoTikClient:
    def __init__(self, username: str, password: str, timeout: int = 60) -> None:
        self.username = username
        self.password = password
        self.timeout = timeout
        self.access_token = ""
        self.progress: ProgressCallback | None = None
        self.request_count = 0
        self.session = self._new_session()

    @staticmethod
    def _new_session() -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Origin": WEB_BASE_URL,
                "Referer": f"{WEB_BASE_URL}/products",
                "x-lang": "zh-CN",
                "x-region": THAILAND_REGION,
                "x-currency": "CNY",
                "x-secondary-currency": "THB",
            }
        )
        return session

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.request_count += 1
        return self.session.request(method, url, **kwargs)

    def _renew_authenticated_session(self, reason: str, cooldown_seconds: float) -> None:
        emit(self.progress, f"EchoTik {reason}，等待 {cooldown_seconds:g} 秒后重建登录会话")
        sleep_seconds(cooldown_seconds)
        try:
            self.session.close()
        except Exception:
            pass
        self.session = self._new_session()
        self.access_token = ""
        self.request_count = 0
        self.login()

    def _ensure_request_budget(self) -> None:
        if self.request_count < SESSION_REQUEST_BUDGET:
            return
        self._renew_authenticated_session(
            f"本轮请求已达到 {self.request_count} 次，为避免触发访问阈值",
            SESSION_RENEWAL_COOLDOWN_SECONDS,
        )

    def login(self) -> None:
        try:
            self._send("GET", f"{WEB_BASE_URL}/api/csrf-cookie", timeout=self.timeout)
        except requests.RequestException:
            # The CSRF endpoint is best-effort for this app; login can still return a clear error.
            pass
        payload = self._request(
            "POST",
            "/users/login",
            json={"email": self.username, "password": self.password},
            auth_required=False,
        )
        if not isinstance(payload, dict) or not safe_text(payload.get("access_token")):
            raise EchoTikApiError("EchoTik 登录成功响应缺少 access_token")
        self.access_token = safe_text(payload.get("access_token"))
        self.session.headers["Authorization"] = f"Bearer {self.access_token}"

    def product_page(
        self,
        keyword: str,
        page: int,
        per_page: int,
        filters: ProductFilters | None = None,
        *,
        order: str = PRIMARY_PRODUCT_ORDER,
        sort: str = "desc",
    ) -> EchoTikPage:
        filters = filters or ProductFilters()
        params: dict[str, Any] = {
            "type": 1,
            "page": page,
            "per_page": per_page,
            "dateRange": filters.sales_period_days,
            # EchoTik's unsorted search result is reshuffled between page
            # requests, which produces duplicate pages and silently skips
            # products. This is the same default order used by the website.
            "order": order,
            "sort": sort,
        }
        if safe_text(keyword):
            params["keyword"] = keyword
        category_path = filters.category_paths[0] if len(filters.category_paths) == 1 else ()
        if category_path:
            # EchoTik's cascaded category filter expects the complete path and
            # the bracketed array key, e.g. [parent_id, child_id].
            params["product_categories[]"] = list(category_path)
        elif filters.category_ids:
            # Keep accepting legacy callers that only provide flat IDs.
            params["product_categories"] = list(filters.category_ids)
        product_ranges = {
            "sales": filters.period_sales,
            "total_sale_cnt": filters.total_sales,
            "views_count": filters.video_views,
            "videos_count": filters.video_count,
            "related_influencers": filters.creator_count,
        }
        for key, value in product_ranges.items():
            api_value = number_range_to_api(value)
            if api_value:
                params[key] = api_value
        return self._request_page(
            "GET",
            "/data/products",
            params=params,
            description="商品列表",
        )

    def product_filter_options(self) -> dict[str, Any]:
        data = self._request("GET", "/data/products/filters")
        return data if isinstance(data, dict) else {}

    def product_categories(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/data/products/product-category")
        return ensure_row_list(data, "商品类目")

    def product_detail(self, product_id: str) -> dict[str, Any]:
        data = self._request("GET", f"/data/products/{product_id}")
        return data if isinstance(data, dict) else {}

    def influencer_page(self, product_id: str, page: int, per_page: int) -> EchoTikPage:
        return self._request_page(
            "GET",
            f"/data/products/{product_id}/influencers",
            # The endpoint's natural order is stable. Explicit `order=sales`
            # reorders equal-sales creators between pages and can itself lose
            # one row at a page boundary.
            params={"page": page, "per_page": per_page},
            description="达人列表",
        )

    def _request_page(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any],
        description: str,
    ) -> EchoTikPage:
        data, meta = self._request(method, path, params=params, include_meta=True)
        rows = ensure_row_list(data, description)
        meta = meta if isinstance(meta, dict) else {}
        return EchoTikPage(
            rows=rows,
            total=optional_int(meta.get("total")),
            last_page=optional_int(meta.get("last_page")),
            current_page=optional_int(meta.get("current_page")),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth_required: bool = True,
        include_meta: bool = False,
    ) -> Any:
        if auth_required and not self.access_token:
            self.login()
        if auth_required:
            self._ensure_request_budget()
        url = path if path.startswith("http") else f"{API_BASE_URL}{path}"
        renewed_for_html = False
        renewed_for_unauthorized = False
        last_exception: Exception | None = None

        for attempt in range(REQUEST_MAX_ATTEMPTS):
            try:
                response = self._send(
                    method,
                    url,
                    params=params,
                    json=json,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_exception = exc
                if attempt + 1 >= REQUEST_MAX_ATTEMPTS:
                    raise EchoTikApiError(f"请求 EchoTik 失败：{exc}") from exc
                delay = retry_delay(REQUEST_RETRY_BACKOFF_SECONDS, attempt)
                emit(self.progress, f"请求 EchoTik 失败：{exc}；{delay:g} 秒后重试")
                sleep_seconds(delay)
                continue

            if response.status_code == 401 and auth_required and not renewed_for_unauthorized:
                renewed_for_unauthorized = True
                self._renew_authenticated_session("登录状态已失效", retry_delay(REQUEST_RETRY_BACKOFF_SECONDS, attempt))
                continue

            if response.status_code in RETRYABLE_HTTP_STATUS_CODES and attempt + 1 < REQUEST_MAX_ATTEMPTS:
                delay = retry_after_seconds(response) or retry_delay(REQUEST_RETRY_BACKOFF_SECONDS, attempt)
                emit(self.progress, f"EchoTik 返回 HTTP {response.status_code}；{delay:g} 秒后重试")
                sleep_seconds(delay)
                continue

            if response_is_html(response):
                body_preview = safe_text(response.text)[:300]
                last_exception = EchoTikHtmlResponseError(
                    f"EchoTik 返回 HTML 错误页：HTTP {response.status_code} {body_preview}"
                )
                if attempt + 1 >= REQUEST_MAX_ATTEMPTS:
                    raise last_exception
                delay = retry_delay(HTML_RETRY_BACKOFF_SECONDS, attempt)
                if auth_required and not renewed_for_html:
                    renewed_for_html = True
                    self._renew_authenticated_session("接口持续返回 HTML 错误页", delay)
                else:
                    emit(self.progress, f"EchoTik 返回 HTML 错误页；{delay:g} 秒后重试")
                    sleep_seconds(delay)
                continue

            try:
                payload = response.json()
            except ValueError as exc:
                raise EchoTikApiError(
                    f"EchoTik 返回不是 JSON：HTTP {response.status_code} {response.text[:300]}"
                ) from exc
            if not isinstance(payload, dict):
                raise EchoTikApiError(f"EchoTik 返回格式异常：{payload!r}")
            code = payload.get("code")
            if response.status_code >= 400 or code not in (0, "0", None):
                message = safe_text(payload.get("msg") or payload.get("message")) or response.text[:300]
                raise EchoTikApiError(f"EchoTik 接口失败：HTTP {response.status_code} code={code} {message}")
            if include_meta:
                return payload.get("data"), payload.get("meta")
            return payload.get("data")

        raise EchoTikApiError(f"请求 EchoTik 失败：{last_exception or '超过最大重试次数'}")


def validate_echotik_payload(payload: dict[str, Any]) -> EchoTikCollectJob:
    username = safe_text(payload.get("username"))
    password = str(payload.get("password") or "")
    keyword_value = payload.get("keywords") or payload.get("keyword")
    keywords = parse_keywords(keyword_value) or ("",)
    output_dir = Path(safe_text(payload.get("output_dir"))).expanduser()
    page_size = safe_int(payload.get("page_size"), DEFAULT_PAGE_SIZE)
    max_products = safe_int(payload.get("max_products"), DEFAULT_MAX_PRODUCTS)
    filter_payload = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    product_filters = parse_product_filters(filter_payload.get("products"))
    creator_filters = parse_creator_filters(filter_payload.get("creators"))

    if not username:
        raise ValueError("请输入 EchoTik 账号")
    if not password:
        raise ValueError("请输入 EchoTik 密码")
    if not output_dir:
        raise ValueError("请选择输出目录")
    if not 1 <= max_products <= MAX_PRODUCT_LIMIT:
        raise ValueError(f"最多采集商品数必须在 1 到 {MAX_PRODUCT_LIMIT} 之间")

    return EchoTikCollectJob(
        username=username,
        password=password,
        keywords=keywords,
        output_dir=output_dir,
        page_size=max(1, min(page_size, MAX_PAGE_SIZE)),
        max_products=max_products,
        product_delay_seconds=max(0.0, float(payload.get("product_delay_seconds") or 0.6)),
        creator_delay_seconds=max(0.0, float(payload.get("creator_delay_seconds") or 0.8)),
        max_pages=max(1, safe_int(payload.get("max_pages"), MAX_PAGES)),
        fetch_product_details=parse_bool(payload.get("fetch_product_details")),
        product_filters=product_filters,
        creator_filters=creator_filters,
    )


def get_echotik_filter_options(username: str, password: str) -> dict[str, Any]:
    client = EchoTikClient(username, password)
    client.login()
    raw_filters = client.product_filter_options()
    categories = normalize_filter_categories(client.product_categories())
    more_filters = raw_filters.get("more_filters") if isinstance(raw_filters.get("more_filters"), dict) else {}
    preset_mapping = {
        "period_sales": "sales",
        "total_sales": "total_sold_count",
        "video_views": "views_count",
        "video_count": "videos_count",
        "creator_count": "related_influencers",
    }
    presets: dict[str, list[dict[str, str]]] = {}
    for output_key, source_key in preset_mapping.items():
        values = more_filters.get(source_key)
        presets[output_key] = [
            {"value": safe_text(item.get("id")), "label": safe_text(item.get("name"))}
            for item in values or []
            if isinstance(item, dict) and safe_text(item.get("id"))
        ]
    return {"categories": categories, "presets": presets}


def run_echotik_collection(
    job: EchoTikCollectJob,
    progress: ProgressCallback | None = None,
) -> EchoTikCollectResult:
    client = EchoTikClient(job.username, job.password)
    client.progress = progress
    emit(progress, f"正在登录 EchoTik：{mask_account(job.username)}")
    client.login()
    emit(progress, "EchoTik 登录成功，国家固定为泰国")
    if not job.fetch_product_details:
        emit(progress, "已启用低请求模式：使用商品列表数据，不再逐商品请求详情接口")

    creators: list[dict[str, Any]] = []
    raw_products: dict[str, dict[str, Any]] = {}
    product_keywords: dict[str, str] = {}
    product_order: list[str] = []
    candidate_product_ids: set[str] = set()
    creator_source_count = 0
    creator_missing_metric_count = 0

    all_products_mode = job.keywords == ("",)
    if all_products_mode:
        emit(progress, f"开始采集全部商品（未指定关键词），最多保留 {job.max_products} 个商品")
    else:
        emit(
            progress,
            f"开始采集 {len(job.keywords)} 个关键词，"
            f"每个关键词最多保留 {job.max_products} 个商品",
        )
    if job.product_filters.enabled or job.creator_filters.enabled:
        emit(progress, f"筛选条件：{filter_summary_text(job)}")
    for keyword_index, keyword in enumerate(job.keywords, start=1):
        keyword_label = display_keyword(keyword)
        before = len(product_order)
        if keyword:
            emit(progress, f"[{keyword_index}/{len(job.keywords)}] 搜索关键词：{keyword}")
        else:
            emit(progress, f"[{keyword_index}/{len(job.keywords)}] 搜索范围：{keyword_label}（未指定关键词）")
        keyword_rows = collect_complete_product_pages(
            client,
            job,
            keyword,
            progress=progress,
        )
        retained_rows: list[dict[str, Any]] = []
        for row in keyword_rows:
            product_id = safe_text(row.get("product_id"))
            if not product_id:
                continue
            candidate_product_ids.add(product_id)
            if not product_matches_filters(row, job.product_filters):
                continue
            retained_rows.append(row)
            if product_id in raw_products:
                product_keywords[product_id] = merge_keywords(product_keywords[product_id], keyword_label)
                continue
            raw_products[product_id] = row
            product_keywords[product_id] = keyword_label
            product_order.append(product_id)
        emit(
            progress,
            f"[{keyword_index}/{len(job.keywords)}] 接口返回 {len(keyword_rows)} 个，"
            f"本地复核保留 {len(retained_rows)} 个，新增 {len(product_order) - before} 个商品",
        )

    emit(
        progress,
        f"商品列表采集完成，候选 {len(candidate_product_ids)} 个，筛选并去重后 {len(product_order)} 个",
    )

    product_index: dict[str, dict[str, Any]] = {}
    total = len(product_order)
    consecutive_html_failures = 0
    for index, product_id in enumerate(product_order, start=1):
        raw = raw_products[product_id]
        matched_keywords = product_keywords[product_id]
        # Build the row from the raw list data first so that list-only fields
        # (e.g. 近 7 日销量/GMV) survive even if the detail request fails.
        row = product_output_row(matched_keywords, raw, None)
        product_index[product_id] = row
        product_title = safe_text(row.get("product_title"))
        emit(progress, f"[{index}/{total}] 正在采集商品达人：{product_title or product_id}")
        if job.fetch_product_details:
            try:
                detail = client.product_detail(product_id)
                row.update(product_output_row(matched_keywords, raw, detail))
                consecutive_html_failures = 0
            except Exception as exc:
                row["error"] = append_error(row.get("error"), f"商品详情失败：{exc}")
                emit(progress, f"[{index}/{total}] 商品详情获取失败：{exc}")
                consecutive_html_failures = (
                    consecutive_html_failures + 1 if isinstance(exc, EchoTikHtmlResponseError) else 0
                )
        try:
            creator_stats = collect_product_influencers(
                client,
                job,
                matched_keywords,
                product_id,
                safe_text(row.get("product_title")),
                creators,
                progress,
            )
            creator_source_count += creator_stats.source_count
            creator_missing_metric_count += creator_stats.missing_metric_count
            row["source_creator_count"] = creator_stats.source_count
            row["matched_creator_count"] = creator_stats.matched_count
            row["filtered_creator_count"] = creator_stats.source_count - creator_stats.matched_count
            if creator_stats.warning:
                row["error"] = append_error(row.get("error"), f"达人列表不完整：{creator_stats.warning}")
            consecutive_html_failures = 0
            emit(
                progress,
                f"[{index}/{total}] 达人采集完成：原始 {creator_stats.source_count} 条，"
                f"保留 {creator_stats.matched_count} 条，排除 {creator_stats.source_count - creator_stats.matched_count} 条"
                f"{'（结果不完整，已记录告警）' if creator_stats.warning else ''}",
            )
        except Exception as exc:
            row["error"] = append_error(row.get("error"), f"达人列表失败：{exc}")
            emit(progress, f"[{index}/{total}] 达人采集失败，已跳过：{exc}")
            consecutive_html_failures = (
                consecutive_html_failures + 1 if isinstance(exc, EchoTikHtmlResponseError) else 0
            )

        if consecutive_html_failures >= SUSTAINED_HTML_FAILURE_LIMIT:
            remaining_count = total - index
            circuit_message = (
                f"EchoTik 连续 {consecutive_html_failures} 次返回 HTML 错误页，已触发熔断；"
                f"保留当前结果，剩余 {remaining_count} 个商品不再继续请求"
            )
            emit(progress, circuit_message)
            for remaining_product_id in product_order[index:]:
                remaining_row = product_output_row(
                    product_keywords[remaining_product_id],
                    raw_products[remaining_product_id],
                    None,
                )
                remaining_row["error"] = append_error(
                    remaining_row.get("error"),
                    "未采集：EchoTik 连续返回 HTML 错误页，任务已熔断",
                )
                product_index[remaining_product_id] = remaining_row
            break
        sleep_seconds(job.creator_delay_seconds)

    creator_filtered_count = creator_source_count - len(creators)
    summary = {
        "product_candidate_count": len(candidate_product_ids),
        "product_count": len(product_index),
        "creator_source_count": creator_source_count,
        "creator_count": len(creators),
        "creator_filtered_count": creator_filtered_count,
        "creator_missing_metric_count": creator_missing_metric_count,
    }
    output_file = export_echotik_excel(job, list(product_index.values()), creators, summary)
    emit(progress, f"Excel 已生成：{output_file}")
    return EchoTikCollectResult(
        product_candidate_count=len(candidate_product_ids),
        product_count=len(product_index),
        creator_source_count=creator_source_count,
        creator_count=len(creators),
        creator_filtered_count=creator_filtered_count,
        creator_missing_metric_count=creator_missing_metric_count,
        failed_product_count=sum(1 for row in product_index.values() if safe_text(row.get("error"))),
        output_file=output_file,
        output_dir=output_file.parent,
        products_preview=list(product_index.values())[:20],
        creators_preview=creators[:20],
    )


def collect_product_influencers(
    client: EchoTikClient,
    job: EchoTikCollectJob,
    keyword: str,
    product_id: str,
    product_title: str,
    output_rows: list[dict[str, Any]],
    progress: ProgressCallback | None,
) -> CreatorCollectionStats:
    before = len(output_rows)
    partial_warnings: list[str] = []
    rows = collect_complete_pages(
        lambda page: client.influencer_page(product_id, page=page, per_page=job.page_size),
        row_key=creator_row_key,
        page_size=job.page_size,
        max_pages=job.max_pages,
        delay_seconds=job.creator_delay_seconds,
        description=f"商品 {product_id} 达人列表",
        progress=progress,
        partial_warnings=partial_warnings,
    )
    missing_metric_count = 0
    for row in rows:
        output_row = creator_output_row(keyword, product_id, product_title, row)
        matched, missing_metric = creator_matches_filters(row, job.creator_filters)
        if matched:
            output_rows.append(output_row)
        elif missing_metric:
            missing_metric_count += 1
    return CreatorCollectionStats(
        source_count=len(rows),
        matched_count=len(output_rows) - before,
        missing_metric_count=missing_metric_count,
        warning="；".join(partial_warnings),
    )


def collect_complete_product_pages(
    client: EchoTikClient,
    job: EchoTikCollectJob,
    keyword: str,
    *,
    progress: ProgressCallback | None,
) -> list[dict[str, Any]]:
    """Collect EchoTik products up to the configured per-scope limit.

    Equal-ranked products can cross page boundaries and cause duplicate IDs.
    Repeating the same ranking cannot recover a stable page set, so first retry
    the primary ranking and then union official alternative rankings until
    enough candidates exist. The candidates are finally ranked locally by the
    website's primary metric plus product ID, giving a deterministic top-N
    result. Unfiltered all-products searches currently advertise up to 10,000
    rows, so the configured limit also keeps downstream creator requests safe.
    """
    if len(job.product_filters.category_paths) > 1:
        merged: dict[str, dict[str, Any]] = {}
        total_paths = len(job.product_filters.category_paths)
        for path_index, category_path in enumerate(job.product_filters.category_paths, start=1):
            emit(
                progress,
                f"  第 {path_index}/{total_paths} 个类目路径：{' / '.join(category_path)}",
            )
            path_filters = replace(
                job.product_filters,
                category_ids=(),
                category_paths=(category_path,),
            )
            path_job = replace(job, product_filters=path_filters)
            for row in collect_complete_product_pages(client, path_job, keyword, progress=progress):
                product_id = safe_text(row.get("product_id"))
                if product_id:
                    merged.setdefault(product_id, row)
        return rank_product_candidates(
            merged.values(),
            job.max_products,
            sales_period_days=job.product_filters.sales_period_days,
        )

    collected: dict[str, dict[str, Any]] = {}
    expected_total: int | None = None
    advertised_total: int | None = None
    limit_notice_emitted = False

    def collect_strategy(
        order: str,
        sort: str,
        *,
        label: str,
        pass_number: int = 1,
    ) -> int:
        nonlocal advertised_total, expected_total, limit_notice_emitted
        before = len(collected)
        page = 1
        expected_pages: int | None = None
        while page <= job.max_pages:
            kwargs: dict[str, Any] = {}
            if job.product_filters.enabled:
                kwargs["filters"] = job.product_filters
            if order != PRIMARY_PRODUCT_ORDER or sort != "desc":
                kwargs["order"] = order
                kwargs["sort"] = sort
            result = normalize_page_result(
                client.product_page(
                    keyword,
                    page=page,
                    per_page=job.page_size,
                    **kwargs,
                ),
                "商品列表",
            )
            rows = result.rows
            if advertised_total is None and result.total is not None:
                advertised_total = result.total
                expected_total = min(advertised_total, job.max_products)
                if advertised_total > expected_total and not limit_notice_emitted:
                    emit(
                        progress,
                        f"  商品列表接口匹配 {advertised_total} 条，"
                        f"本次按设置最多采集 {expected_total} 条",
                    )
                    limit_notice_emitted = True
            if result.last_page is not None:
                expected_pages = result.last_page
            target_page_count = (job.max_products + job.page_size - 1) // job.page_size
            if expected_total is not None:
                target_page_count = (expected_total + job.page_size - 1) // job.page_size
            if expected_pages is None:
                expected_pages = target_page_count
            else:
                expected_pages = min(expected_pages, target_page_count)
            pass_label = f"（补采第 {pass_number} 轮）" if pass_number > 1 else ""
            strategy_label = f"（{label}）" if label else ""
            total_label = f" / {expected_total}" if expected_total is not None else ""
            emit(
                progress,
                f"  商品列表第 {page} 页{pass_label}{strategy_label}返回 {len(rows)} 条，"
                f"已去重 {len(collected)}{total_label}",
            )
            if not rows:
                break
            for row in rows:
                product_id = safe_text(row.get("product_id"))
                if product_id:
                    collected.setdefault(product_id, row)
            target_total = expected_total if expected_total is not None else job.max_products
            if len(collected) >= target_total:
                break
            if expected_pages is not None and page >= expected_pages:
                break
            page += 1
            sleep_seconds(job.product_delay_seconds)
        else:
            raise EchoTikApiError(f"商品列表分页超过 {job.max_pages} 页，已停止")
        return len(collected) - before

    collect_strategy(PRIMARY_PRODUCT_ORDER, "desc", label="")
    if expected_total is None:
        return list(collected.values())[: job.max_products]
    if len(collected) >= expected_total:
        return list(collected.values())[:expected_total]

    for pass_number in range(2, PAGINATION_RECOVERY_PASSES + 1):
        emit(
            progress,
            f"  商品列表接口标称 {expected_total} 条，当前仅 {len(collected)} 条，"
            "开始补采缺失分页",
        )
        added = collect_strategy(
            PRIMARY_PRODUCT_ORDER,
            "desc",
            label="",
            pass_number=pass_number,
        )
        if len(collected) >= expected_total:
            return list(collected.values())[:expected_total]
        if not added:
            break
        sleep_seconds(job.product_delay_seconds)

    emit(
        progress,
        f"  默认排序仅取得 {len(collected)} / {expected_total} 个唯一商品，"
        "启用多排序稳定分页恢复",
    )
    for order, sort, label in PRODUCT_PAGINATION_RECOVERY_ORDERS:
        before = len(collected)
        collect_strategy(order, sort, label=label)
        added = len(collected) - before
        emit(
            progress,
            f"  {label}补采新增 {added} 个，候选集共 {len(collected)} 个",
        )
        if len(collected) >= expected_total:
            ranked = rank_product_candidates(
                collected.values(),
                expected_total,
                sales_period_days=job.product_filters.sales_period_days,
            )
            emit(
                progress,
                f"  多排序恢复完成：候选 {len(collected)} 个，"
                f"按周期销量和商品 ID 稳定重排后保留 {len(ranked)} 个",
            )
            return ranked
        sleep_seconds(job.product_delay_seconds)

    message = (
        f"商品列表多排序恢复仍不完整：接口标称 {expected_total} 条，"
        f"仅采集到 {len(collected)} 个唯一商品"
    )
    raise EchoTikApiError(message)


def rank_product_candidates(
    rows: Iterable[dict[str, Any]],
    limit: int,
    *,
    sales_period_days: int,
) -> list[dict[str, Any]]:
    period_key = f"total_sale_{sales_period_days}d_cnt"

    def ranking_key(row: dict[str, Any]) -> tuple[bool, Decimal, str]:
        sales = parse_compact_number(first_value(row, period_key, PRIMARY_PRODUCT_ORDER, "sales"))
        return (
            sales is None,
            -(sales or Decimal(0)),
            safe_text(row.get("product_id")),
        )

    return sorted(rows, key=ranking_key)[:limit]


def collect_complete_pages(
    fetch_page: Callable[[int], EchoTikPage | list[dict[str, Any]]],
    *,
    row_key: Callable[[dict[str, Any]], str],
    page_size: int,
    max_pages: int,
    delay_seconds: float,
    description: str,
    progress: ProgressCallback | None,
    partial_warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Collect a changing paginated result without silently losing rows.

    EchoTik may reorder equal-ranked rows between requests. We merge by the
    resource ID and, when pagination metadata is available, repeat the page
    range until the advertised total has been recovered.
    """
    collected: dict[str, dict[str, Any]] = {}
    anonymous_rows: list[dict[str, Any]] = []
    expected_total: int | None = None
    expected_pages: int | None = None

    for pass_number in range(1, PAGINATION_RECOVERY_PASSES + 1):
        before = len(collected) + len(anonymous_rows)
        page = 1
        while page <= max_pages:
            result = normalize_page_result(fetch_page(page), description)
            rows = result.rows
            expected_total = result.total if result.total is not None else expected_total
            expected_pages = result.last_page if result.last_page is not None else expected_pages
            pass_label = f"（补采第 {pass_number} 轮）" if pass_number > 1 else ""
            total_label = f" / {expected_total}" if expected_total is not None else ""
            emit(progress, f"  {description}第 {page} 页{pass_label}返回 {len(rows)} 条，已去重 {len(collected)}{total_label}")
            if not rows:
                break
            for row in rows:
                key = row_key(row)
                if key:
                    collected.setdefault(key, row)
                elif pass_number == 1:
                    anonymous_rows.append(row)
            if expected_total is not None and len(collected) + len(anonymous_rows) >= expected_total:
                return [*collected.values(), *anonymous_rows][:expected_total]
            if expected_pages is not None and page >= expected_pages:
                break
            page += 1
            sleep_seconds(delay_seconds)
        else:
            raise EchoTikApiError(f"{description}分页超过 {max_pages} 页，已停止")

        current = len(collected) + len(anonymous_rows)
        if expected_total is None:
            return [*collected.values(), *anonymous_rows]
        if current >= expected_total:
            return [*collected.values(), *anonymous_rows][:expected_total]
        if current == before and pass_number > 1:
            break
        emit(progress, f"  {description}接口标称 {expected_total} 条，当前仅 {current} 条，开始补采缺失分页")
        sleep_seconds(delay_seconds)

    current = len(collected) + len(anonymous_rows)
    message = f"{description}分页结果持续变动：接口标称 {expected_total} 条，仅稳定采集到 {current} 条"
    if partial_warnings is not None and current:
        missing = max(0, (expected_total or current) - current)
        warning = f"{message}，缺失 {missing} 条"
        partial_warnings.append(warning)
        emit(progress, f"  警告：{warning}；已保留当前采集结果")
        return [*collected.values(), *anonymous_rows]
    raise EchoTikApiError(message)


def normalize_page_result(value: EchoTikPage | list[dict[str, Any]], description: str) -> EchoTikPage:
    if isinstance(value, EchoTikPage):
        return value
    return EchoTikPage(rows=ensure_row_list(value, description))


def creator_row_key(row: dict[str, Any]) -> str:
    return first_value(row, "influencer_id", "user_id", "creator_id", "unique_id")


def product_output_row(keyword: str, row: dict[str, Any], detail: dict[str, Any] | None) -> dict[str, Any]:
    # The search-list row carries fields the detail endpoint omits (近 7 日销量/GMV、
    # 更精确的末级分类), so let it win over the detail payload when keys overlap.
    source = {**(detail or {}), **(row or {})}
    product_id = safe_text(source.get("product_id"))
    seller = source.get("seller") if isinstance(source.get("seller"), dict) else {}
    category = normalize_category(source.get("category") or source.get("categories"))
    categories = source.get("categories") or source.get("category")
    image = first_image(source)
    return {
        "keyword": keyword,
        "product_id": product_id,
        "product_title": first_value(source, "product_title", "product_name", "product_title_brief"),
        "product_url": first_value(source, "product_url") or f"{WEB_BASE_URL}/products/{product_id}",
        "product_image": image,
        "shop_id": first_value(seller, "seller_id", "id", "shop_id", "user_id"),
        "shop_name": first_value(seller, "seller_name", "name", "shop_name", "nickname"),
        "price": first_value(source, "avg_price_fz", "min_price_fz", "avg_price", "real_price", "min_price", "price"),
        "currency": first_value(source, "currency") or "THB",
        "recent_7d_sales": first_value(source, "total_sale_7d_cnt", "total_sale_nd_cnt", "sale_7d_cnt", "sales_7d"),
        "recent_7d_gmv": first_value(source, "total_sale_gmv_7d_amt_fz", "total_sale_gmv_nd_amt_fz", "total_sale_gmv_7d_amt"),
        "total_sales": first_value(source, "total_sale_cnt", "sale_cnt", "total_sold_count", "sales"),
        "total_gmv": first_value(source, "total_sale_gmv_amt_fz", "gmv_amt_fz", "total_sale_gmv_amt", "gmv_amt", "gmv"),
        "creator_count": first_value(source, "total_ifl_cnt", "influencers_count", "ifl_30d_cnt"),
        "video_count": first_value(source, "total_video_count", "videos_count", "total_video_cnt", "video_30d_cnt"),
        "video_play_count": first_value(source, "view_count", "views_count", "total_video_viewers"),
        "category": category,
        "raw_category": json_dumps(categories),
        "source_creator_count": 0,
        "matched_creator_count": 0,
        "filtered_creator_count": 0,
        "collected_at": now_text(),
        "error": safe_text(row.get("error")) if isinstance(row, dict) else "",
    }


def creator_output_row(keyword: str, product_id: str, product_title: str, row: dict[str, Any]) -> dict[str, Any]:
    # 达人 ID 用 TikTok 唯一账号名（unique_id，如 oranid888），纯数字 influencer_id 单独保留一列。
    creator_uid = first_value(row, "influencer_id", "user_id", "creator_id")
    creator_id = first_value(row, "unique_id") or creator_uid
    region = row.get("region") if isinstance(row.get("region"), dict) else {}
    fans_count = first_value(row, "follower_count", "followers_count", "fans_count")
    creator_sales = first_value(row, "sales", "sale_count", "sales_count")
    product_gmv = first_value(row, "product_ifl_gmv_amt_fz", "product_ifl_gmv_amt", "gmv_fz", "gmv")
    video_play_count = first_value(row, "total_video_viewers", "views", "views_count")
    return {
        "keyword": keyword,
        "product_id": product_id,
        "product_title": product_title,
        "creator_id": creator_id,
        "creator_uid": creator_uid,
        "creator_name": first_value(row, "influencer_name", "nickname", "creator_name"),
        "creator_avatar": first_value(row, "avatar_url", "avatar"),
        "country": first_value(region, "name", "id", "key"),
        "fans_count": fans_count,
        "likes_count": first_value(row, "heart_count", "likes_count"),
        "creator_categories": join_values(row.get("categories") or row.get("category") or row.get("category_product")),
        "creator_sales": creator_sales,
        "creator_sales_value": number_for_excel(parse_compact_number(creator_sales)),
        "product_gmv": product_gmv,
        "product_gmv_value": number_for_excel(parse_compact_number(product_gmv)),
        "video_count": first_value(row, "related_video", "video_count"),
        "video_play_count": video_play_count,
        "video_play_count_value": number_for_excel(parse_compact_number(video_play_count)),
        "fans_count_value": number_for_excel(parse_compact_number(fans_count)),
        "live_count": first_value(row, "related_live", "live_count"),
        "live_view_count": first_value(row, "total_live_viewers", "live_view_count"),
        "creator_url": f"{WEB_BASE_URL}/influencers/{creator_uid}" if creator_uid else "",
        "collected_at": now_text(),
        "error": "",
    }


def export_echotik_excel(
    job: EchoTikCollectJob,
    products: list[dict[str, Any]],
    creators: list[dict[str, Any]],
    summary: dict[str, Any] | None = None,
) -> Path:
    job.output_dir.mkdir(parents=True, exist_ok=True)
    keyword_label = (
        safe_filename(display_keyword(job.keywords[0]))
        if len(job.keywords) == 1
        else f"多关键词{len(job.keywords)}个"
    )
    filename = f"EchoTik_商品达人采集_{keyword_label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    output_file = job.output_dir / filename
    product_df = pd.DataFrame(products, columns=PRODUCT_COLUMNS)
    creator_df = pd.DataFrame(creators, columns=CREATOR_COLUMNS)
    summary_df = pd.DataFrame(filter_summary_rows(job, summary or {}), columns=("项目", "值"))
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        product_df.to_excel(writer, sheet_name="Products", index=False)
        creator_df.to_excel(writer, sheet_name="Creators", index=False)
        summary_df.to_excel(writer, sheet_name="FilterSummary", index=False)
    return output_file


def parse_product_filters(value: Any) -> ProductFilters:
    data = value if isinstance(value, dict) else {}
    period = safe_int(data.get("sales_period_days"), 7)
    if period not in {1, 7, 30, 90}:
        raise ValueError("商品销量周期只支持 1、7、30、90 天")
    return ProductFilters(
        category_ids=parse_string_tuple(data.get("category_ids")),
        category_names=parse_string_tuple(data.get("category_names")),
        category_paths=parse_category_paths(data.get("category_paths")),
        sales_period_days=period,
        period_sales=parse_number_range(data.get("period_sales"), "商品周期销量"),
        total_sales=parse_number_range(data.get("total_sales"), "商品累计销量"),
        video_views=parse_number_range(data.get("video_views"), "商品视频播放量"),
        video_count=parse_number_range(data.get("video_count"), "商品关联视频数"),
        creator_count=parse_number_range(data.get("creator_count"), "商品关联达人数"),
    )


def parse_creator_filters(value: Any) -> CreatorFilters:
    data = value if isinstance(value, dict) else {}
    return CreatorFilters(
        category_names=parse_string_tuple(data.get("category_names")),
        sales=parse_number_range(data.get("sales"), "达人销量"),
        video_play_count=parse_number_range(data.get("video_play_count"), "达人视频播放量"),
        fans_count=parse_number_range(data.get("fans_count"), "达人粉丝数"),
        video_count=parse_number_range(data.get("video_count"), "达人关联视频数"),
        product_gmv=parse_number_range(data.get("product_gmv"), "当前商品带货 GMV"),
    )


def parse_number_range(value: Any, label: str) -> NumberRange:
    data = value if isinstance(value, dict) else {}
    minimum = parse_filter_number(data.get("min"), f"{label}最小值")
    maximum = parse_filter_number(data.get("max"), f"{label}最大值")
    if minimum is not None and minimum < 0:
        raise ValueError(f"{label}最小值不能小于 0")
    if maximum is not None and maximum < 0:
        raise ValueError(f"{label}最大值不能小于 0")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"{label}最小值不能大于最大值")
    return NumberRange(minimum=minimum, maximum=maximum)


def parse_filter_number(value: Any, label: str) -> Decimal | None:
    if value in (None, ""):
        return None
    parsed = parse_compact_number(value)
    if parsed is None:
        raise ValueError(f"{label}格式不正确")
    return parsed


def parse_compact_number(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = safe_text(value).replace(",", "").replace("，", "").replace(" ", "")
    if not text or text in {"-", "—", "--", "N/A", "n/a"}:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        number = Decimal(match.group(0))
    except InvalidOperation:
        return None
    suffix = text[match.end() :].lower()
    if suffix.startswith("万"):
        number *= Decimal("10000")
    elif suffix.startswith("亿"):
        number *= Decimal("100000000")
    elif suffix.startswith("k"):
        number *= Decimal("1000")
    elif suffix.startswith("m"):
        number *= Decimal("1000000")
    elif suffix.startswith("b"):
        number *= Decimal("1000000000")
    return number


def number_for_excel(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def number_range_to_api(value: NumberRange) -> str:
    """Return a slightly widened third-party range; local validation stays exact."""
    if not value.enabled:
        return ""
    minimum = None if value.minimum is not None and value.minimum <= 0 else widen_boundary(value.minimum, direction=-1)
    maximum = widen_boundary(value.maximum, direction=1)
    if minimum is not None and maximum is not None:
        return f"{decimal_text(minimum)}-{decimal_text(maximum)}"
    if minimum is not None:
        return f">{decimal_text(minimum)}"
    if maximum is not None:
        return f"<{decimal_text(maximum)}"
    return ""


def widen_boundary(value: Decimal | None, *, direction: int) -> Decimal | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        widened = value + Decimal(direction)
    else:
        widened = value + Decimal("0.000001") * direction
    return max(Decimal(0), widened)


def decimal_text(value: Decimal) -> str:
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")


def product_matches_filters(row: dict[str, Any], filters: ProductFilters) -> bool:
    if filters.category_names and not categories_match(
        row.get("categories") or row.get("category"),
        filters.category_names,
    ):
        return False
    period_key = f"total_sale_{filters.sales_period_days}d_cnt"
    checks = (
        (filters.period_sales, first_value(row, period_key, "total_sale_nd_cnt", "sales")),
        (filters.total_sales, first_value(row, "total_sale_cnt", "sale_cnt", "total_sold_count")),
        (filters.video_views, first_value(row, "view_count", "views_count", "total_video_viewers")),
        (filters.video_count, first_value(row, "videos_count", "total_video_count", "total_video_cnt")),
        (filters.creator_count, first_value(row, "influencers_count", "total_ifl_cnt", "ifl_30d_cnt")),
    )
    return all(number_matches_range(raw_value, number_range)[0] for number_range, raw_value in checks)


def creator_matches_filters(row: dict[str, Any], filters: CreatorFilters) -> tuple[bool, bool]:
    if filters.category_names:
        categories = row.get("categories") or row.get("category") or row.get("category_product")
        if not categories_match(categories, filters.category_names):
            return False, not bool(category_tokens(categories))
    checks = (
        (filters.sales, first_value(row, "sales", "sale_count", "sales_count")),
        (filters.video_play_count, first_value(row, "total_video_viewers", "views", "views_count")),
        (filters.fans_count, first_value(row, "follower_count", "followers_count", "fans_count")),
        (filters.video_count, first_value(row, "related_video", "video_count")),
        (filters.product_gmv, first_value(row, "product_ifl_gmv_amt", "gmv", "gmv_amt_30d")),
    )
    missing_metric = False
    for number_range, raw_value in checks:
        matched, missing = number_matches_range(raw_value, number_range)
        if not matched:
            missing_metric = missing_metric or missing
            return False, missing_metric
    return True, False


def number_matches_range(value: Any, number_range: NumberRange) -> tuple[bool, bool]:
    if not number_range.enabled:
        return True, False
    number = parse_compact_number(value)
    if number is None:
        return False, True
    if number_range.minimum is not None and number < number_range.minimum:
        return False, False
    if number_range.maximum is not None and number > number_range.maximum:
        return False, False
    return True, False


def categories_match(value: Any, selected_names: tuple[str, ...]) -> bool:
    available = {token.casefold() for token in category_tokens(value)}
    if not available:
        return False
    for selected in selected_names:
        wanted = selected.casefold()
        if any(wanted == item or wanted in item or item in wanted for item in available):
            return True
    return False


def category_tokens(value: Any) -> list[str]:
    if isinstance(value, dict):
        direct = first_value(value, "name", "label", "category_name", "title", "key", "value")
        children = category_tokens(value.get("children"))
        return [item for item in [direct, *children] if item]
    if isinstance(value, (list, tuple, set)):
        return [token for item in value for token in category_tokens(item)]
    text = safe_text(value)
    if not text:
        return []
    return [part.strip() for part in re.split(r"[/|,，]", text) if part.strip()]


def normalize_filter_categories(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        category_id = first_value(row, "value", "id", "key")
        name = first_value(row, "label", "name", "title")
        if not category_id or not name or category_id.casefold() in {"all", "0"}:
            continue
        children = row.get("children") if isinstance(row.get("children"), list) else []
        result.append(
            {
                "id": category_id,
                "name": name,
                "children": normalize_filter_categories([item for item in children if isinstance(item, dict)]),
            }
        )
    return result


def parse_string_tuple(value: Any) -> tuple[str, ...]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = safe_text(item)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return tuple(result)


def parse_category_paths(value: Any) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    result: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for item in value:
        if not isinstance(item, (list, tuple, set)):
            continue
        path = parse_string_tuple(item)
        if not path or path in seen:
            continue
        seen.add(path)
        result.append(path)
    return tuple(result)


def format_number_range(value: NumberRange) -> str:
    if value.minimum is not None and value.maximum is not None:
        return f"{decimal_text(value.minimum)}-{decimal_text(value.maximum)}"
    if value.minimum is not None:
        return f"≥{decimal_text(value.minimum)}"
    if value.maximum is not None:
        return f"≤{decimal_text(value.maximum)}"
    return "不限"


def filter_summary_text(job: EchoTikCollectJob) -> str:
    parts: list[str] = []
    product = job.product_filters
    creator = job.creator_filters
    if product.category_names:
        parts.append(f"商品类目={','.join(product.category_names)}")
    for label, value in (
        (f"近{product.sales_period_days}天销量", product.period_sales),
        ("商品累计销量", product.total_sales),
        ("商品播放量", product.video_views),
        ("商品视频数", product.video_count),
        ("商品达人数", product.creator_count),
    ):
        if value.enabled:
            parts.append(f"{label}{format_number_range(value)}")
    if creator.category_names:
        parts.append(f"达人类目={','.join(creator.category_names)}")
    for label, value in (
        ("达人销量", creator.sales),
        ("达人播放量", creator.video_play_count),
        ("达人粉丝数", creator.fans_count),
        ("达人视频数", creator.video_count),
        ("当前商品GMV", creator.product_gmv),
    ):
        if value.enabled:
            parts.append(f"{label}{format_number_range(value)}")
    return "；".join(parts) if parts else "未设置筛选"


def filter_summary_rows(job: EchoTikCollectJob, summary: dict[str, Any]) -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = [
        ("采集时间", now_text()),
        ("国家", "泰国"),
        ("关键词", " | ".join(display_keyword(keyword) for keyword in job.keywords)),
        ("每个搜索范围最多商品数", job.max_products),
        ("筛选条件", filter_summary_text(job)),
    ]
    labels = (
        ("product_candidate_count", "候选商品数"),
        ("product_count", "保留商品数"),
        ("creator_source_count", "原始达人明细数"),
        ("creator_count", "保留达人明细数"),
        ("creator_filtered_count", "排除达人明细数"),
        ("creator_missing_metric_count", "指标缺失排除数"),
    )
    rows.extend((label, summary.get(key, 0)) for key, label in labels)
    return rows


def ensure_row_list(value: Any, description: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise EchoTikApiError(f"{description}返回格式异常：期望数组，实际 {type(value).__name__}")
    return [item for item in value if isinstance(item, dict)]


def first_value(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            if isinstance(value, (dict, list, tuple)):
                return json_dumps(value)
            return str(value).strip()
    return ""


def first_image(data: dict[str, Any]) -> str:
    cover = safe_text(data.get("cover_url") or data.get("product_image"))
    if cover:
        return cover
    images = data.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            return first_value(first, "url", "image_url", "src")
        return safe_text(first)
    return ""


def normalize_category(value: Any) -> str:
    if isinstance(value, dict):
        return first_value(value, "name", "category_name", "title", "key")
    if isinstance(value, list):
        parts = []
        for item in value:
            text = normalize_category(item)
            if text:
                parts.append(text)
        return " / ".join(parts)
    return safe_text(value)


def join_values(value: Any) -> str:
    if isinstance(value, list):
        return " / ".join(safe_text(item) for item in value if safe_text(item))
    if isinstance(value, dict):
        return normalize_category(value)
    return safe_text(value)


def json_dumps(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return safe_text(value)


def safe_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return safe_text(value).casefold() in {"1", "true", "yes", "on"}


def retry_delay(values: tuple[float, ...], attempt: int) -> float:
    if not values:
        return 0.0
    return values[min(max(attempt, 0), len(values) - 1)]


def retry_after_seconds(response: requests.Response) -> float:
    try:
        value = float(safe_text(response.headers.get("Retry-After")))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(value, 300.0))


def response_is_html(response: requests.Response) -> bool:
    content_type = safe_text(response.headers.get("Content-Type")).casefold()
    body_prefix = safe_text(response.text)[:200].casefold()
    return "text/html" in content_type or body_prefix.startswith(("<!doctype html", "<html"))


def append_error(existing: object, message: str) -> str:
    prefix = safe_text(existing)
    return f"{prefix}；{message}" if prefix else message


def parse_keywords(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        candidates = [part for item in value for part in parse_keywords(item)]
    else:
        candidates = [part.strip() for part in KEYWORD_SEPARATOR.split(safe_text(value))]

    result: list[str] = []
    seen: set[str] = set()
    for keyword in candidates:
        if not keyword:
            continue
        key = keyword.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(keyword)
    return tuple(result)


def display_keyword(keyword: str) -> str:
    return safe_text(keyword) or ALL_PRODUCTS_LABEL


def merge_keywords(existing: str, keyword: str) -> str:
    return " | ".join(parse_keywords([*existing.split(" | "), keyword]))


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except Exception:
        match = re.search(r"\d+", str(value or ""))
        return int(match.group(0)) if match else default


def optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    return safe_int(value)


def safe_filename(value: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", safe_text(value))
    name = re.sub(r"\s+", "_", name).strip("._ ")
    return (name or "keyword")[:80]


def mask_account(value: str) -> str:
    text = safe_text(value)
    if "@" in text:
        name, domain = text.split("@", 1)
        return f"{name[:2]}***@{domain}"
    if len(text) <= 4:
        return "***"
    return f"{text[:2]}***{text[-2:]}"


def sleep_seconds(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def emit(progress: ProgressCallback | None, message: str) -> None:
    if progress:
        progress(message)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
