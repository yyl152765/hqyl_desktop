from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlencode, urlparse

import httpx


LOGGER = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_CONNECT_TIMEOUT_SECONDS = 20.0
DEFAULT_WRITE_TIMEOUT_SECONDS = 20.0
DEFAULT_POOL_TIMEOUT_SECONDS = 20.0

LOGIN_PAGE_PATH = "/index.htm"
LOGIN_PATH = "/index.php?mod=main.doLogin&lang=cn"
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


class MabangApiError(RuntimeError):
    """Raised when Mabang ERP returns an invalid or failed response."""


@dataclass(slots=True)
class MabangExportFile:
    file_name: str
    file_type: str
    file_url: str
    content: bytes
    metadata: dict[str, Any] = field(default_factory=dict)


class MabangClient:
    """Minimal Mabang ERP HTTP client used by the desktop app sample."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        verify_ssl: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.verify_ssl = verify_ssl
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=_build_http_timeout(timeout_seconds),
            follow_redirects=True,
            verify=verify_ssl,
            trust_env=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        self._logged_in = False

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "MabangClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def login(self, username: str, password: str) -> None:
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
        payload = _response_json_dict(response, context="马帮登录")
        if not payload.get("success"):
            raise MabangApiError(f"登录失败: {payload}")
        self._logged_in = True

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}{path}"

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
        """Download Mabang sales report CSV through reports.exportProductAll."""
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
        response = self.client.get(export_url, headers={"Referer": referer_url}, timeout=self.timeout_seconds)
        response.raise_for_status()
        file_name = _guess_download_filename(response.headers.get("Content-Disposition"), str(response.url))
        file_type = _safe_text(Path(file_name).suffix).lower().lstrip(".") or "csv"
        return MabangExportFile(
            file_name=file_name,
            file_type=file_type,
            file_url=str(response.url),
            content=response.content,
            metadata={
                "reportType": normalized_report_type,
                "platformOrShop": effective_platform_or_shop,
                "fieldCodes": selected_fields,
                "firstPageHasRows": bool(_safe_text(search_payload.get("tableContent"))),
                "updateDateStr": _safe_text(search_payload.get("updateDateStr")),
            },
        )

    def _post_sales_report(
        self,
        *,
        url: str,
        params: dict[str, Any] | None,
        form_data: Any,
        referer: str | None,
    ) -> dict[str, Any]:
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
        return _response_json_dict(response, context="销售报表")


def _build_http_timeout(timeout_seconds: int | float) -> httpx.Timeout:
    read_timeout = max(5.0, float(timeout_seconds))
    connect_timeout = min(read_timeout, DEFAULT_CONNECT_TIMEOUT_SECONDS)
    write_timeout = min(read_timeout, DEFAULT_WRITE_TIMEOUT_SECONDS)
    pool_timeout = min(read_timeout, DEFAULT_POOL_TIMEOUT_SECONDS)
    return httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=write_timeout,
        pool=pool_timeout,
    )


def decode_html_page(response: httpx.Response) -> str:
    for encoding in (response.encoding, "gb18030", "utf-8"):
        if not encoding:
            continue
        try:
            return response.content.decode(encoding, errors="ignore")
        except LookupError:
            continue
    return response.text


def _response_json_dict(response: httpx.Response, *, context: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        snippet = " ".join(decode_html_page(response).split())[:500]
        content_type = response.headers.get("Content-Type") or "-"
        raise MabangApiError(
            f"{context} 返回非 JSON 响应: status={response.status_code}, "
            f"content_type={content_type}, url={response.url}, body={snippet}"
        ) from exc
    if not isinstance(payload, dict):
        raise MabangApiError(f"{context} 返回 JSON 结构无效: {payload}")
    return payload


def _safe_text(value: Any) -> str:
    return "" if value is None or value == "" else str(value).strip()


def _clone_form_payload(form_data: Any) -> Any:
    if hasattr(form_data, "items"):
        return dict(form_data)
    return [(str(key), value) for key, value in list(form_data or [])]


def _set_form_payload_value(form_data: Any, key: str, value: Any) -> Any:
    if hasattr(form_data, "items"):
        form_data[key] = value
        return form_data
    normalized_key = _safe_text(key)
    filtered = [(item_key, item_value) for item_key, item_value in list(form_data or []) if item_key != normalized_key]
    filtered.append((normalized_key, value))
    return filtered


def _get_form_payload_first(form_data: Any, key: str) -> Any:
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
    normalized = _safe_text(report_type)
    if not normalized:
        mod_value = _safe_text((params or {}).get("mod"))
        if not mod_value:
            mod_value = _extract_first(r"[?&]mod=([^&]+)", url)
        normalized = mod_value.split(".")[-1] if mod_value else ""
    if normalized not in SALES_REPORT_PAGE_PATHS:
        raise MabangApiError(f"不支持的销量报表导出类型: {normalized or '-'}")
    return normalized


def _extract_first(pattern: str, text: str) -> str:
    match = re.search(pattern, text or "")
    return match.group(1) if match else ""


def _guess_download_filename(content_disposition: str | None, url: str) -> str:
    header = content_disposition or ""
    filename = _extract_first(r"filename\*=UTF-8''([^;]+)", header)
    if filename:
        return unquote(filename).strip('"')
    filename = _extract_first(r'filename="?([^";]+)"?', header)
    if filename:
        return unquote(filename).strip('"')
    parsed_name = Path(urlparse(url).path).name
    return parsed_name or "mabang_export.csv"
