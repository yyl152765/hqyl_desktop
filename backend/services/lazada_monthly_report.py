"""Lazada cross-border monthly report download service.

The service is deliberately independent from the existing withdrawal-statistics
flow.  A monthly report is selected by an exact calendar month and the downloaded
artifact is copied atomically to a deterministic, store-named output file.

Browser/runtime dependencies are injected at the runner boundary so unit tests do
not start a real Ziniao process or a real browser.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import unicodedata
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urljoin, urlsplit


ProgressCallback = Callable[[str], None]

COUNTRIES: dict[str, dict[str, Any]] = {
    "TH": {
        "name": "泰国",
        "origins": (
            "https://sellercenter-th.lazada-seller.cn",
            "https://sellercenter.lazada.co.th",
        ),
    },
    "MY": {
        "name": "马来西亚",
        "origins": (
            "https://sellercenter-my.lazada-seller.cn",
            "https://sellercenter.lazada.com.my",
        ),
    },
    "PH": {
        "name": "菲律宾",
        "origins": (
            "https://sellercenter-ph.lazada-seller.cn",
            "https://sellercenter.lazada.com.ph",
        ),
    },
}
REPORT_DOWNLOAD_HOSTS = {
    "TH": frozenset({"fbprivacy.lazada.co.th"}),
    "MY": frozenset({"fbprivacy.lazada.com.my"}),
    "PH": frozenset({"fbprivacy.lazada.com.ph"}),
}
EXPORT_CREATION_API_HOSTS = {
    "TH": "m-th.lazada-seller.cn",
    "MY": "m-my.lazada-seller.cn",
    "PH": "m-ph.lazada-seller.cn",
}
EXPORT_CREATION_API_PATH = (
    "/h5/mtop.lazada.finance.sellerfund3.create.download.task/1.0/"
)
GSP_ORIGINS = (
    "https://gsp.lazada-seller.cn",
    "https://gsp.lazada.com",
)
INCOME_PAGE_PATH = "/portal/apps/finance/myIncome/index"
DEFAULT_COUNTRIES = tuple(COUNTRIES)
# 本土店（本地卖家中心）域名后缀白名单：与 COUNTRY_PROFILES 的 local_host 对应，
# 用于开店落地判定，精确锚定域名避免把 *.evil.example 之类误判成店铺页面。
LOCAL_STORE_FRONT_SUFFIXES = (
    ".lazada.co.th",
    ".lazada.com.ph",
    ".lazada.com.my",
    ".lazada.co.id",
    ".lazada.vn",
)
# 紫鸟开店后等待店铺落地的时间预算。本土店直接是本地卖家中心（不是 GSP），
# 因此这里必须同时接受「跨境 GSP」与「本地/跨境卖家中心」两类页面，否则本土店会白等。
STORE_PAGE_WAIT_SECONDS = 12.0
STORE_PAGE_WAIT_AFTER_LAUNCH_SECONDS = 20.0
SUPPORTED_REPORT_SUFFIXES = frozenset({".xlsx", ".xls", ".csv", ".zip"})
TEMP_DOWNLOAD_SUFFIXES = (
    ".crdownload",
    ".tmp",
    ".part",
    ".download",
    ".partial",
)
KNOWN_DOWNLOAD_OPTIONS = (
    "Order Details (excel)",
    "Order Details (xlsx)",
    "Transaction Details (excel)",
    "Transaction Details (xlsx)",
    "Transactions Details (excel)",
    "Transactions Details (xlsx)",
    "订单详情 (excel)",
    "订单详情（excel）",
    "交易详情 (excel)",
    "交易详情（excel）",
)
DOWNLOAD_HISTORY_OPTIONS = (
    "下载历史",
    "Download History",
)

_INVALID_FILE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_STORE_NAME_CHAR_FOLD = str.maketrans(
    {
        "萬": "万",
        "賽": "赛",
        "諾": "诺",
        "鋒": "锋",
        "廣": "广",
        "為": "为",
        "爲": "为",
        "硯": "砚",
        "國": "国",
        "營": "营",
    }
)
_ENGLISH_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


class LazadaMonthlyReportError(RuntimeError):
    """Base error for one monthly-report operation."""


class MonthlyReportNotFoundError(LazadaMonthlyReportError):
    """The selected calendar month is not present on the page."""


class UnknownDownloadOptionError(LazadaMonthlyReportError):
    """The download menu exists, but none of its options is evidence-backed."""


@dataclass(frozen=True)
class LazadaMonthlyReportQuery:
    company: str
    username: str
    password: str
    countries: tuple[str, ...]
    month: str
    store_names: tuple[str, ...]
    browser_window_mode: str = "normal"
    client_path: str = ""
    webdriver_path: str = ""
    output_root: str = ""
    socket_port: int = 16851
    max_concurrent_stores: int = 1


@dataclass(frozen=True)
class MonthlyReportDownload:
    source_file: Path
    period_text: str
    period_start: date
    period_end: date


@dataclass
class MonthlyStoreResult:
    country: str
    country_name: str
    requested_store_name: str
    store_name: str
    month: str
    status: str = "running"
    message: str = ""
    period_text: str = ""
    source_file: str = ""
    local_file: str = ""
    started_at: str = ""
    finished_at: str = ""

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = _now_text()


@dataclass
class OpenedBrowser:
    browser_name: str
    page: Any
    download_path: Path
    browser_oauth: str = ""
    connection: Any = None


class BrowserRuntime(Protocol):
    def start(self) -> None: ...

    def list_browsers(self) -> list[dict[str, Any]]: ...

    def open_browser(self, browser: dict[str, Any], download_path: Path) -> OpenedBrowser: ...

    def close_browser(self, opened: OpenedBrowser) -> None: ...

    def shutdown(self) -> None: ...


class MonthlyReportPageActions(Protocol):
    def download_report(
        self,
        page: Any,
        country: str,
        month: str,
        download_path: Path,
        progress: ProgressCallback | None = None,
    ) -> MonthlyReportDownload: ...


RuntimeFactory = Callable[[LazadaMonthlyReportQuery, logging.Logger], BrowserRuntime]


def previous_month_value(today: date | None = None) -> str:
    current = today or date.today()
    return (current.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def month_date_range(month: str) -> tuple[date, date]:
    text = str(month or "").strip()
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", text):
        raise ValueError("月份格式必须为 YYYY-MM")
    start = date.fromisoformat(f"{text}-01")
    if start.month == 12:
        following = date(start.year + 1, 1, 1)
    else:
        following = date(start.year, start.month + 1, 1)
    return start, following - timedelta(days=1)


def country_options() -> list[dict[str, str]]:
    return [{"code": code, "name": item["name"]} for code, item in COUNTRIES.items()]


def parse_store_names(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_values = [str(item or "").strip() for item in value]
    else:
        text = str(value or "").replace("；", "\n").replace(";", "\n")
        raw_values = [item.strip() for item in text.splitlines()]
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        key = item.casefold()
        if item and key not in seen:
            result.append(item)
            seen.add(key)
    return result


def _parse_countries(payload: dict[str, Any]) -> tuple[str, ...]:
    raw = payload.get("countries")
    if raw in (None, "", []):
        raw = payload.get("country")
    if raw in (None, "", []):
        values = list(DEFAULT_COUNTRIES)
    elif isinstance(raw, (list, tuple, set)):
        values = [str(item or "").strip().upper() for item in raw]
    else:
        values = [
            item.strip().upper()
            for item in re.split(r"[,，;；\s]+", str(raw or ""))
            if item.strip()
        ]
    result: list[str] = []
    for value in values:
        if value not in COUNTRIES:
            raise ValueError(f"不支持的国家代码：{value or '空'}")
        if value not in result:
            result.append(value)
    if not result:
        raise ValueError("请至少选择一个国家")
    return tuple(result)


def validate_lazada_monthly_report_payload(
    payload: dict[str, Any],
    *,
    default_output_root: str = "",
    today: date | None = None,
) -> LazadaMonthlyReportQuery:
    request = dict(payload or {})
    username = str(request.get("username") or "").strip()
    password = str(request.get("password") or "")
    if not username or not password:
        raise ValueError("请先绑定并选择紫鸟账号")

    current = today or date.today()
    month = str(request.get("month") or previous_month_value(current)).strip()
    month_start, _ = month_date_range(month)
    if month_start >= current.replace(day=1):
        raise ValueError("月份不得晚于上个月")

    stores = tuple(parse_store_names(request.get("store_names")))
    if not stores:
        raise ValueError("请输入店铺名称，每行一个")

    window_mode = str(request.get("browser_window_mode") or "normal").strip().lower()
    if window_mode not in {"normal", "background"}:
        raise ValueError("浏览器窗口模式无效")
    try:
        socket_port = int(request.get("socket_port") or 16851)
    except (TypeError, ValueError) as exc:
        raise ValueError("紫鸟端口必须为整数") from exc
    if not 1 <= socket_port <= 65535:
        raise ValueError("紫鸟端口必须在 1～65535 之间")
    try:
        max_concurrent_stores = int(request.get("max_concurrent_stores") or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("并发店铺数必须为整数") from exc
    if not 1 <= max_concurrent_stores <= 4:
        raise ValueError("并发店铺数必须在 1～4 之间")

    return LazadaMonthlyReportQuery(
        company=str(request.get("company") or "").strip(),
        username=username,
        password=password,
        countries=_parse_countries(request),
        month=month,
        store_names=stores,
        browser_window_mode=window_mode,
        client_path=str(request.get("client_path") or "").strip(),
        webdriver_path=str(request.get("webdriver_path") or "").strip(),
        output_root=str(
            request.get("output_root")
            or request.get("output_dir")
            or default_output_root
            or ""
        ).strip(),
        socket_port=socket_port,
        max_concurrent_stores=max_concurrent_stores,
    )


def resolve_lazada_runtime_paths() -> dict[str, str]:
    """Discover the installed client without loading legacy workflow projects."""
    from backend.core.ziniao_paths import resolve_ziniao_client_path

    # Playwright connects over CDP and does not need a Selenium driver directory.
    return {"client_path": resolve_ziniao_client_path(), "webdriver_path": ""}


def safe_store_filename(value: Any, *, max_length: int = 150) -> str:
    text = unicodedata.normalize("NFKC", str(value or "").strip())
    text = _INVALID_FILE_CHARS.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = text[: max(int(max_length), 1)].rstrip(" .") or "store"
    if text.split(".", 1)[0].upper() in _RESERVED_WINDOWS_NAMES:
        text = f"_{text}"
    return text


def snapshot_download_files(download_dir: str | Path) -> dict[str, tuple[int, int]]:
    root = Path(download_dir)
    snapshot: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return snapshot
    for path in root.iterdir():
        try:
            if path.is_file():
                stat = path.stat()
                snapshot[path.name] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            continue
    return snapshot


def download_activity_started(
    download_dir: str | Path,
    before_snapshot: dict[str, tuple[int, int]],
) -> bool:
    current = snapshot_download_files(download_dir)
    return any(before_snapshot.get(name) != fingerprint for name, fingerprint in current.items())


def _is_temporary_download(path: Path) -> bool:
    lower_name = path.name.casefold()
    return any(lower_name.endswith(suffix) for suffix in TEMP_DOWNLOAD_SUFFIXES)


def wait_for_new_download_file(
    download_dir: str | Path,
    before_snapshot: dict[str, tuple[int, int]],
    *,
    timeout: float = 120,
    poll_interval: float = 0.5,
    stable_polls: int = 2,
    allowed_suffixes: frozenset[str] = SUPPORTED_REPORT_SUFFIXES,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Path | None:
    """Return a new/changed, non-temporary file after its size becomes stable."""
    root = Path(download_dir)
    deadline = clock() + max(float(timeout), 0.0)
    required_stability = max(int(stable_polls), 2)
    stable: dict[Path, tuple[tuple[int, int], int]] = {}
    while True:
        candidates: list[Path] = []
        if root.is_dir():
            for path in root.iterdir():
                try:
                    if not path.is_file() or _is_temporary_download(path):
                        continue
                    if path.suffix.casefold() not in allowed_suffixes:
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                fingerprint = (stat.st_mtime_ns, stat.st_size)
                if stat.st_size <= 0 or before_snapshot.get(path.name) == fingerprint:
                    continue
                previous = stable.get(path)
                count = previous[1] + 1 if previous and previous[0] == fingerprint else 1
                stable[path] = (fingerprint, count)
                if count >= required_stability:
                    candidates.append(path)
        if candidates:
            candidates.sort(key=lambda item: item.stat().st_mtime_ns, reverse=True)
            return candidates[0]
        if clock() >= deadline:
            return None
        sleep(max(float(poll_interval), 0.0))


def validate_downloaded_report(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_file() or target.stat().st_size <= 0:
        raise LazadaMonthlyReportError("月度报告文件不存在或为空")
    suffix = target.suffix.casefold()
    if suffix not in SUPPORTED_REPORT_SUFFIXES:
        raise LazadaMonthlyReportError(f"不支持的月度报告格式：{suffix or '无扩展名'}")
    with target.open("rb") as stream:
        signature = stream.read(64)
    if suffix in {".xlsx", ".zip"}:
        if not zipfile.is_zipfile(target):
            raise LazadaMonthlyReportError(f"下载文件内容与 {suffix} 扩展名不匹配")
        try:
            with zipfile.ZipFile(target) as archive:
                members = [item for item in archive.infolist() if not item.is_dir()]
                if not members or len(members) > 10_000:
                    raise LazadaMonthlyReportError("下载的压缩报告为空或条目数量异常")
                if sum(item.file_size for item in members) > 2 * 1024 * 1024 * 1024:
                    raise LazadaMonthlyReportError("下载的压缩报告解压后体积异常")
                if archive.testzip() is not None:
                    raise LazadaMonthlyReportError("下载的压缩报告校验失败")
                names = {item.filename.replace("\\", "/") for item in members}
                if suffix == ".xlsx" and not {
                    "[Content_Types].xml",
                    "xl/workbook.xml",
                }.issubset(names):
                    raise LazadaMonthlyReportError("下载文件不是完整的 Excel 工作簿")
        except (OSError, zipfile.BadZipFile) as exc:
            raise LazadaMonthlyReportError(f"下载文件内容与 {suffix} 扩展名不匹配") from exc
    elif suffix == ".xls":
        ole_signature = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        if target.stat().st_size < 512 or not signature.startswith(ole_signature):
            raise LazadaMonthlyReportError("下载文件内容与 .xls 扩展名不匹配")
    elif suffix == ".csv":
        with target.open("rb") as stream:
            sample = stream.read(64 * 1024)
        decoded = ""
        for encoding in ("utf-8-sig", "utf-16", "gb18030"):
            try:
                decoded = sample.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if not decoded:
            raise LazadaMonthlyReportError("下载的 CSV 不是可识别的文本文件")
        normalized = decoded.lstrip("\ufeff\x00 \t\r\n").casefold()
        if re.match(r"(?:<!doctype\s+html|<html\b|<head\b|<body\b)", normalized):
            raise LazadaMonthlyReportError("下载的 CSV 实际为网页内容，可能需要重新登录")
        if not any(delimiter in decoded for delimiter in (",", "\t", ";")):
            raise LazadaMonthlyReportError("下载的 CSV 缺少可识别的字段分隔符")
    return target


def finalize_store_named_report(
    source_file: str | Path,
    destination_dir: str | Path,
    store_name: str,
) -> Path:
    source = validate_downloaded_report(source_file)
    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"{safe_store_filename(store_name)}{source.suffix.casefold()}"
    temporary = destination / f".{target.name}.{uuid.uuid4().hex}.part"
    try:
        shutil.copy2(source, temporary)
        if temporary.stat().st_size != source.stat().st_size:
            raise LazadaMonthlyReportError("月度报告复制后大小不一致")
        os.replace(temporary, target)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    return target


def parse_monthly_report_period(value: Any) -> tuple[date, date] | None:
    text = unicodedata.normalize("NFKC", str(value or "")).replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip()

    numeric = re.search(
        r"(?P<y1>\d{4})[-/.](?P<m1>\d{1,2})[-/.](?P<d1>\d{1,2})\s*[-–—~至]\s*"
        r"(?P<y2>\d{4})[-/.](?P<m2>\d{1,2})[-/.](?P<d2>\d{1,2})",
        text,
    )
    if numeric:
        try:
            return (
                date(int(numeric["y1"]), int(numeric["m1"]), int(numeric["d1"])),
                date(int(numeric["y2"]), int(numeric["m2"]), int(numeric["d2"])),
            )
        except ValueError:
            return None

    chinese = re.search(
        r"(?P<y1>\d{4})年(?P<m1>\d{1,2})月(?P<d1>\d{1,2})日?\s*[-–—~至]\s*"
        r"(?:(?P<y2>\d{4})年)?(?P<m2>\d{1,2})月(?P<d2>\d{1,2})日?",
        text,
    )
    if chinese:
        try:
            right_year = int(chinese["y2"] or chinese["y1"])
            return (
                date(int(chinese["y1"]), int(chinese["m1"]), int(chinese["d1"])),
                date(right_year, int(chinese["m2"]), int(chinese["d2"])),
            )
        except ValueError:
            return None

    english = re.search(
        r"(?P<d1>\d{1,2})\s+(?P<m1>[A-Za-z]{3,9})(?:\s+(?P<y1>\d{4}))?\s*[-–—~]\s*"
        r"(?P<d2>\d{1,2})\s+(?P<m2>[A-Za-z]{3,9})\s+(?P<y2>\d{4})",
        text,
    )
    if english:
        first_month = _ENGLISH_MONTHS.get(english["m1"][:3].casefold())
        second_month = _ENGLISH_MONTHS.get(english["m2"][:3].casefold())
        if not first_month or not second_month:
            return None
        try:
            right_year = int(english["y2"])
            left_year = int(english["y1"] or right_year)
            return (
                date(left_year, first_month, int(english["d1"])),
                date(right_year, second_month, int(english["d2"])),
            )
        except ValueError:
            return None
    return None


def period_matches_month(period_start: date, period_end: date, month: str) -> bool:
    expected_start, expected_end = month_date_range(month)
    return period_start == expected_start and period_end == expected_end


def _parse_export_time(value: Any) -> datetime | None:
    text = unicodedata.normalize("NFKC", str(value or "")).replace("年", "-").replace("月", "-").replace("日", " ")
    text = re.sub(r"\s+", " ", text).strip()
    numeric = re.search(
        r"(?P<year>\d{4})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})"
        r"(?:\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?",
        text,
    )
    if numeric:
        try:
            return datetime(
                int(numeric["year"]),
                int(numeric["month"]),
                int(numeric["day"]),
                int(numeric["hour"] or 0),
                int(numeric["minute"] or 0),
                int(numeric["second"] or 0),
            )
        except ValueError:
            return None
    english = re.search(
        r"(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]{3,9})\s+(?P<year>\d{4})"
        r"(?:\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?",
        text,
    )
    if english:
        month_number = _ENGLISH_MONTHS.get(english["month"][:3].casefold())
        if not month_number:
            return None
        try:
            return datetime(
                int(english["year"]),
                month_number,
                int(english["day"]),
                int(english["hour"] or 0),
                int(english["minute"] or 0),
                int(english["second"] or 0),
            )
        except ValueError:
            return None
    return None


def _is_fresh_export_time(value: Any, trigger_time: datetime) -> bool:
    text = unicodedata.normalize("NFKC", str(value or ""))
    # Without seconds, two exports created in the same displayed minute cannot be
    # distinguished safely when the history modal was not already mounted.
    if not re.search(r"\d{1,2}:\d{2}:\d{2}", text):
        return False
    parsed = _parse_export_time(text)
    if parsed is None:
        return False
    delta_seconds = (parsed - trigger_time).total_seconds()
    return -2 <= delta_seconds <= 10 * 60


def _find_export_id(value: Any) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z]", "", str(key).casefold())
            if normalized_key == "exportid" and re.fullmatch(r"\d+", str(item or "").strip()):
                return str(item).strip()
        for item in value.values():
            found = _find_export_id(item)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _find_export_id(item)
            if found:
                return found
    return ""


def _find_creation_export_id(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    if str(value.get("api") or "").casefold() != (
        "mtop.lazada.finance.sellerfund3.create.download.task"
    ):
        return ""
    envelope = value.get("data")
    if not isinstance(envelope, dict) or envelope.get("succeeded") is not True:
        return ""
    export_id = str(envelope.get("data") or "").strip()
    return export_id if re.fullmatch(r"\d+", export_id) else ""


def _is_export_creation_response(response: Any, country: str) -> bool:
    try:
        method = str(response.request.method or "").upper()
        parsed = urlsplit(str(response.url or ""))
        status = int(response.status)
    except (AttributeError, TypeError, ValueError):
        return False
    if country not in EXPORT_CREATION_API_HOSTS:
        return False
    if method != "GET" or not 200 <= status < 300:
        return False
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != EXPORT_CREATION_API_HOSTS[country]
        or parsed.port not in (None, 443)
        or parsed.path.casefold() != EXPORT_CREATION_API_PATH.casefold()
    ):
        return False
    try:
        encoded_data = (parse_qs(parsed.query).get("data") or [""])[0]
        request_data = json.loads(encoded_data)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(request_data, dict)
        and str(request_data.get("taskType") or "").casefold() == "export"
        and str(request_data.get("businessCode") or "").casefold()
        == f"lazada_{country}_finance-".casefold()
    )


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_store_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_STORE_NAME_CHAR_FOLD)
    return "".join(character for character in text.casefold() if character.isalnum())


_STORE_ALIAS_SEPARATOR = re.compile(r"[\s\-_‐‑‒–—―|/\\]+")
_ALLOWED_STORE_ALIAS_SUFFIXES = {"半运营"}


def _is_controlled_store_alias(requested: str, actual: str) -> bool:
    """Accept only a complete, delimited core name in a known Ziniao profile shape."""
    requested_normalized = _normalize_store_name(requested)
    if len(requested_normalized) < 8:
        return False
    parts = tuple(
        normalized
        for part in _STORE_ALIAS_SEPARATOR.split(
            unicodedata.normalize("NFKC", str(actual or "")).translate(_STORE_NAME_CHAR_FOLD)
        )
        if (normalized := _normalize_store_name(part))
    )
    positions = [index for index, part in enumerate(parts) if part == requested_normalized]
    if len(positions) != 1:
        return False
    position = positions[0]
    prefix = parts[:position]
    suffix = parts[position + 1 :]
    allowed_suffixes = {_normalize_store_name(value) for value in _ALLOWED_STORE_ALIAS_SUFFIXES}
    return bool(prefix or suffix) and all(part in allowed_suffixes for part in suffix)


def _store_match_score(requested: str, actual: str) -> int:
    if str(requested or "").strip() == str(actual or "").strip():
        return 100
    left = _normalize_store_name(requested)
    right = _normalize_store_name(actual)
    if left and left == right:
        return 90
    # Some Ziniao profiles prepend an operator name and/or append the known
    # “半运营” marker.  Require the requested core name to be a complete,
    # delimited segment so that names such as 002 and 0020 can never cross-match.
    if _is_controlled_store_alias(requested, actual):
        return 70
    return 0


def _browser_identity(browser: dict[str, Any]) -> str:
    return str(
        browser.get("browserOauth")
        or browser.get("browserId")
        or browser.get("id")
        or browser.get("browserName")
        or ""
    )


def _is_lazada_browser(browser: dict[str, Any]) -> bool:
    combined = " ".join(
        str(browser.get(key) or "")
        for key in ("platform_name", "platformName", "platform", "browserType", "browserName")
    ).casefold()
    return "lazada" in combined or "lz跨境" in combined


def match_lazada_browsers(
    browser_list: list[dict[str, Any]],
    store_names: tuple[str, ...] | list[str],
) -> tuple[list[tuple[str, dict[str, Any]]], list[tuple[str, str]]]:
    requested_names = list(store_names)
    provisional: dict[int, tuple[int, str, str, dict[str, Any]]] = {}
    failures: dict[int, str] = {}
    for request_index, requested in enumerate(requested_names):
        candidates_by_identity: dict[str, tuple[int, str, dict[str, Any]]] = {}
        for browser in browser_list:
            identity = _browser_identity(browser)
            if not identity or not _is_lazada_browser(browser):
                continue
            actual = str(browser.get("browserName") or "").strip()
            score = _store_match_score(requested, actual)
            if score:
                previous = candidates_by_identity.get(identity)
                if previous is None or score > previous[0]:
                    candidates_by_identity[identity] = (score, actual, browser)
        candidates = [
            (score, actual, identity, browser)
            for identity, (score, actual, browser) in candidates_by_identity.items()
        ]
        best_score = max((item[0] for item in candidates), default=0)
        best = [item for item in candidates if item[0] == best_score and best_score > 0]
        if len(best) == 1:
            provisional[request_index] = best[0]
        elif len(best) > 1:
            names = "、".join(item[1] for item in best)
            failures[request_index] = f"匹配到多个 Lazada 紫鸟店铺：{names}"
        else:
            failures[request_index] = "未唯一匹配到 Lazada 紫鸟店铺"

    claims_by_identity: dict[str, list[tuple[int, int, str]]] = {}
    for request_index, (score, actual, identity, _) in provisional.items():
        claims_by_identity.setdefault(identity, []).append((request_index, score, actual))
    for claims in claims_by_identity.values():
        if len(claims) == 1:
            continue
        highest_score = max(claim[1] for claim in claims)
        winners = [claim for claim in claims if claim[1] == highest_score]
        winner_index = winners[0][0] if len(winners) == 1 else None
        for request_index, _, actual in claims:
            if request_index == winner_index:
                continue
            failures[request_index] = f"与其他请求争用同一 Lazada 紫鸟店铺：{actual}"

    matched: list[tuple[str, dict[str, Any]]] = []
    unmatched: list[tuple[str, str]] = []
    for request_index, requested in enumerate(requested_names):
        if request_index in failures:
            unmatched.append((requested, failures[request_index]))
            continue
        candidate = provisional.get(request_index)
        if candidate is None:
            unmatched.append((requested, "未唯一匹配到 Lazada 紫鸟店铺"))
            continue
        matched.append((requested, candidate[3]))
    return matched, unmatched


class ZiniaoPlaywrightRuntime:
    """Production runtime using the existing Ziniao HTTP API and CDP session."""

    def __init__(self, job: LazadaMonthlyReportQuery, logger: logging.Logger) -> None:
        self.job = job
        self.logger = logger
        self.client: Any = None
        self.playwright: Any = None

    def start(self) -> None:
        from backend.core.ziniao_client import ZiniaoClient, ZiniaoCredentials
        from playwright.sync_api import sync_playwright

        client_path = self.job.client_path
        if not client_path:
            client_path = resolve_lazada_runtime_paths().get("client_path", "")
        self.client = ZiniaoClient(
            ZiniaoCredentials(self.job.company, self.job.username, self.job.password),
            client_path,
            port=self.job.socket_port,
        )
        self.client.ensure_started()
        self.playwright = sync_playwright().start()

    def list_browsers(self) -> list[dict[str, Any]]:
        if self.client is None:
            raise RuntimeError("紫鸟运行时尚未启动")
        return self.client.list_browsers()

    def open_browser(self, browser: dict[str, Any], download_path: Path) -> OpenedBrowser:
        if self.client is None or self.playwright is None:
            raise RuntimeError("紫鸟运行时尚未启动")
        session = self.client.open_browser(browser, download_path, headless=False)
        connection = None
        try:
            connection = self.playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{session.debugging_port}"
            )
            if not connection.contexts:
                raise RuntimeError("紫鸟浏览器没有可用的浏览器上下文")
            context = connection.contexts[0]
            page = self._wait_for_store_page(context, timeout=STORE_PAGE_WAIT_SECONDS)
            if page is None:
                pages = [candidate for candidate in context.pages if not candidate.is_closed()]
                page = next(
                    (candidate for candidate in pages if str(candidate.url or "") == "about:blank"),
                    None,
                )
                if page is None:
                    page = context.new_page()
                if session.launcher_page:
                    try:
                        page.goto(
                            session.launcher_page,
                            wait_until="domcontentloaded",
                            timeout=120_000,
                        )
                    except Exception:
                        try:
                            page.evaluate("window.stop()")
                        except Exception:
                            pass
                    page = (
                        self._wait_for_store_page(
                            context, timeout=STORE_PAGE_WAIT_AFTER_LAUNCH_SECONDS
                        )
                        or page
                    )
            page.set_default_timeout(30_000)
            if self.job.browser_window_mode == "background":
                try:
                    page.evaluate("window.moveTo(-32000, -32000)")
                except Exception:
                    pass
            return OpenedBrowser(
                browser_name=session.browser_name,
                page=page,
                download_path=session.download_path,
                browser_oauth=session.browser_oauth,
                connection=connection,
            )
        except Exception:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            try:
                self.client.close_browser(session.browser_oauth)
            except Exception:
                pass
            raise

    @staticmethod
    def _wait_for_store_page(context: Any, *, timeout: float, only_gsp: bool = False) -> Any | None:
        """等待紫鸟店铺真正落地，优先跨境 GSP 工作台首页。

        本土店（本地卖家中心）**不是** GSP 页面：只认 GSP 会让开店环节白等 20+40 秒，
        所以默认同时接受两类店铺页面，只有 `only_gsp=True` 时才退回旧的严格判定。
        """
        deadline = time.monotonic() + max(float(timeout), 0)
        while True:
            candidates = [
                page
                for page in context.pages
                if not page.is_closed()
                and (
                    _url_matches_gsp(str(page.url or ""))
                    if only_gsp
                    else _url_matches_store_front(str(page.url or ""))
                )
            ]
            if candidates:
                home_pages = [
                    page
                    for page in candidates
                    if urlsplit(str(page.url or "")).path.casefold().startswith("/portal/home")
                ]
                if home_pages:
                    return home_pages[-1]
                gsp_pages = [
                    page for page in candidates if _url_matches_gsp(str(page.url or ""))
                ]
                return (gsp_pages or candidates)[-1]
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)

    @staticmethod
    def _wait_for_gsp_page(context: Any, *, timeout: float) -> Any | None:
        return ZiniaoPlaywrightRuntime._wait_for_store_page(
            context, timeout=timeout, only_gsp=True
        )

    def close_browser(self, opened: OpenedBrowser) -> None:
        if opened.connection is not None:
            try:
                opened.connection.close()
            except Exception:
                self.logger.warning("关闭 Playwright CDP 连接失败", exc_info=True)
        if self.client is not None and opened.browser_oauth:
            try:
                self.client.close_browser(opened.browser_oauth)
            except Exception:
                self.logger.warning("关闭紫鸟店铺失败：%s", opened.browser_name, exc_info=True)

    def shutdown(self) -> None:
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                self.logger.warning("停止 Playwright 失败", exc_info=True)
            self.playwright = None
        if self.client is not None:
            try:
                self.client.exit_if_started()
            except Exception:
                self.logger.warning("退出本次启动的紫鸟客户端失败", exc_info=True)
            self.client = None


class PlaywrightMonthlyReportPageActions:
    """Fail-closed Lazada page actions for the monthly-report card."""

    def download_report(
        self,
        page: Any,
        country: str,
        month: str,
        download_path: Path,
        progress: ProgressCallback | None = None,
    ) -> MonthlyReportDownload:
        page = self._open_income_page(page, country)
        self._dismiss_safe_popups(page)
        if not self._click_monthly_report_tab(page):
            raise LazadaMonthlyReportError("未找到“月度报告 / Monthly Report”入口")
        rows = self._wait_monthly_rows(page, target_month=month)
        if not self._matching_month_rows(rows, month) and self._click_more_monthly_reports(page):
            if not _url_matches_country(str(page.url or ""), country):
                raise LazadaMonthlyReportError("打开更多月度报告后跳转到了错误国家站点")
            overview_signature = tuple(str(row.get("periodText") or "") for row in rows)
            expanded_rows = self._wait_for_monthly_rows_change(page, overview_signature)
            rows = self._search_monthly_report_pages(
                page,
                month,
                initial_rows=expanded_rows or None,
            )
        matched: list[tuple[dict[str, Any], date, date]] = []
        for row in rows:
            period = parse_monthly_report_period(row.get("periodText") or row.get("text"))
            if period and period_matches_month(period[0], period[1], month):
                matched.append((row, period[0], period[1]))
        if not matched:
            raise MonthlyReportNotFoundError(f"页面没有 {month} 的完整自然月报告")
        if len(matched) > 1:
            raise LazadaMonthlyReportError(f"页面出现多个 {month} 月度报告，已拒绝自动选择")

        row, period_start, period_end = matched[0]
        if not _url_matches_country(str(page.url or ""), country):
            raise LazadaMonthlyReportError("下载前检测到页面已跳转到错误国家站点，已停止任务")
        period_text = str(row.get("periodText") or "").strip()
        # The modern Seller Center can render the report list while its separate
        # country download host is still unauthenticated.  Establish that session
        # before opening the menu so every accepted file is tied to a browser
        # download event from the expected country host.
        self._ensure_country_download_session(page, country)
        if not self._click_row_download(page, period_text):
            raise LazadaMonthlyReportError(f"无法点击 {period_text or month} 对应的下载按钮")
        options = self._wait_visible_download_options(page)
        option = self._choose_download_option(options)
        if not option:
            raise UnknownDownloadOptionError(
                "月度报告下载菜单没有唯一可确认的 Excel 选项："
                + ("、".join(options) if options else "未读取到菜单项")
            )
        if progress:
            progress(f"选择下载项：{option}")
        history_option = self._choose_download_history_option(options)
        if not history_option:
            raise LazadaMonthlyReportError("月度报告下载菜单没有唯一的“下载历史”入口")
        if not self._click_download_option(page, history_option):
            raise LazadaMonthlyReportError("无法打开月度报告下载历史")
        existing_export_ids = self._wait_export_history_snapshot(page)
        if existing_export_ids is None:
            raise LazadaMonthlyReportError("月度报告下载历史尚未完整加载，已停止导出")
        self._close_export_modal(page)
        if not self._wait_for_export_history_state(page, visible=False):
            raise LazadaMonthlyReportError("无法关闭月度报告下载历史弹窗")
        if not self._click_row_download(page, period_text):
            raise LazadaMonthlyReportError(f"无法重新打开 {period_text or month} 的下载菜单")
        reopened_options = self._wait_visible_download_options(page)
        if option not in reopened_options:
            raise LazadaMonthlyReportError("重新打开月度报告下载菜单后，Excel 下载项已变化")
        clicked, expected_export_id = self._trigger_download_option(
            page,
            option,
            country=country,
        )
        if not clicked:
            raise LazadaMonthlyReportError(f"无法点击月度报告下载项：{option}")
        if not expected_export_id:
            raise LazadaMonthlyReportError(
                "Lazada 创建导出响应没有返回唯一任务 ID，已拒绝猜测下载历史文件"
            )
        if not self._export_history_visible(page):
            if not self._open_export_history(page, period_text):
                raise LazadaMonthlyReportError("导出已提交，但无法重新打开月度报告下载历史")
        source = self._wait_async_export(
            page,
            download_path,
            existing_export_ids=existing_export_ids,
            expected_export_id=expected_export_id,
            country=country,
        )
        if source is None:
            raise LazadaMonthlyReportError("月度报告导出完成后文件未落地")
        self._close_export_modal(page)
        return MonthlyReportDownload(
            source_file=validate_downloaded_report(source),
            period_text=period_text,
            period_start=period_start,
            period_end=period_end,
        )

    @staticmethod
    def _is_login_page_url(value: Any) -> bool:
        try:
            path = urlsplit(str(value or "")).path.casefold()
        except Exception:
            path = str(value or "").casefold()
        return bool(re.search(r"(?:^|/)(?:login|signin|sign-in)(?:/|$)", path))

    @staticmethod
    def _is_auth_page_url(value: Any) -> bool:
        try:
            path = urlsplit(str(value or "")).path.casefold()
        except Exception:
            path = str(value or "").casefold()
        return bool(re.search(r"(?:^|/)(?:login|signin|sign-in|register)(?:/|$)", path))

    @staticmethod
    def _trusted_register_login_url(page: Any, country: str, target_url: str) -> str:
        current_url = str(page.url or "")
        if not _url_matches_country(current_url, country):
            return ""
        try:
            current_path = urlsplit(current_url).path.casefold()
        except ValueError:
            return ""
        if not re.search(r"(?:^|/)register(?:/|$)", current_path):
            return ""
        try:
            hrefs = page.evaluate(
                r"""
() => Array.from(document.querySelectorAll('a')).map((el) => ({
  text: String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim(),
  href: String(el.getAttribute('href') || '').trim(),
})).filter((item) => /^(登录|Log\s*In)$/i.test(item.text)).map((item) => item.href)
"""
            )
        except Exception as exc:
            raise LazadaMonthlyReportError(
                "无法确认 Lazada 注册页的登录入口，已停止自动登录"
            ) from exc
        candidates: set[str] = set()
        for href in hrefs if isinstance(hrefs, list) else []:
            candidate = urljoin(current_url, str(href or ""))
            if not _url_matches_country(candidate, country):
                continue
            if not PlaywrightMonthlyReportPageActions._is_login_page_url(candidate):
                continue
            try:
                redirects = parse_qs(urlsplit(candidate).query).get("redirect_url") or []
            except ValueError:
                continue
            if redirects == [target_url]:
                candidates.add(candidate)
        return next(iter(candidates)) if len(candidates) == 1 else ""

    @staticmethod
    def _page_body_text(page: Any) -> str:
        try:
            return str(page.locator("body").inner_text(timeout=5_000) or "")
        except Exception:
            return ""

    @staticmethod
    def _income_permission_denied(body: str) -> bool:
        text = " ".join(str(body or "").split())
        return bool(
            re.search(
                r"没有权限|无权访问|必要的权限|permission\s+(?:is\s+)?required|"
                r"access\s+denied|not\s+authorized|not\s+authorised",
                text,
                re.IGNORECASE,
            )
        )

    @staticmethod
    def _visible_login_challenge(page: Any) -> bool:
        try:
            return bool(
                page.evaluate(
                    r"""
() => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect(); const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const challenge = Array.from(document.querySelectorAll(
    'iframe[src*="captcha" i], iframe[src*="recaptcha" i], [class*="captcha" i], [id*="captcha" i]'
  )).some(visible);
  const text = String(document.body?.innerText || '').replace(/\s+/g, ' ');
  return challenge || /请完成验证|拖动滑块|安全验证|please complete the verification/i.test(text);
}
"""
                )
            )
        except Exception as exc:
            raise LazadaMonthlyReportError(
                "无法确认 Lazada 登录安全验证状态，已停止登录"
            ) from exc

    @staticmethod
    def _submit_prefilled_login(page: Any) -> bool:
        """Click Login only when Ziniao already populated both credential fields."""
        try:
            return bool(
                page.evaluate(
                    r"""
() => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect(); const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const usernames = Array.from(document.querySelectorAll(
    'input:not([type]),input[type=""],input[type="text"],input[type="email"],input[type="tel"]'
  )).filter((el) => visible(el) && clean(el.value));
  const passwords = Array.from(document.querySelectorAll('input[type="password"]'))
    .filter((el) => visible(el) && clean(el.value));
  if (usernames.length !== 1 || passwords.length !== 1) return false;
  const username = usernames[0]; const password = passwords[0];
  if (username.form !== password.form) return false;
  const scope = password.form || document;
  const buttons = Array.from(scope.querySelectorAll('button,[role="button"],input[type="submit"]'))
    .filter((el) => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
    .filter((el) => /^(登录|Login|Sign\s*In)$/i.test(
      clean(el.innerText || el.textContent || el.value || el.getAttribute('aria-label'))
    ));
  if (buttons.length !== 1) return false;
  buttons[0].click();
  return true;
}
"""
                )
            )
        except Exception as exc:
            raise LazadaMonthlyReportError(
                "Lazada 登录提交结果不确定，已停止以避免重复提交"
            ) from exc

    @staticmethod
    def _context_pages(page: Any) -> list[Any]:
        context = getattr(page, "context", None)
        if callable(context):
            context = context()
        pages = getattr(context, "pages", None)
        if callable(pages):
            pages = pages()
        if not isinstance(pages, (list, tuple)):
            pages = [page]
        result: list[Any] = []
        for candidate in pages:
            if candidate is None:
                continue
            is_closed = getattr(candidate, "is_closed", None)
            if callable(is_closed) and is_closed():
                continue
            result.append(candidate)
        return result

    @staticmethod
    def _find_gsp_page(page: Any) -> Any | None:
        candidates = [
            candidate
            for candidate in PlaywrightMonthlyReportPageActions._context_pages(page)
            if _url_matches_gsp(str(getattr(candidate, "url", "") or ""))
        ]
        if not candidates:
            return None
        home_pages = [
            candidate
            for candidate in candidates
            if urlsplit(str(getattr(candidate, "url", "") or ""))
            .path.casefold()
            .startswith("/portal/home")
        ]
        return (home_pages or candidates)[-1]

    @staticmethod
    def _is_gsp_login_url(value: Any) -> bool:
        if not _url_matches_gsp(str(value or "")):
            return False
        try:
            path = urlsplit(str(value or "")).path.rstrip("/").casefold()
        except ValueError:
            return False
        return path == "/page/login"

    @staticmethod
    def _is_gsp_home_url(value: Any) -> bool:
        if not _url_matches_gsp(str(value or "")):
            return False
        try:
            path = urlsplit(str(value or "")).path.rstrip("/").casefold()
        except ValueError:
            return False
        return path.startswith("/portal/home")

    @staticmethod
    def _ensure_gsp_home(page: Any) -> Any:
        current_url = str(getattr(page, "url", "") or "")
        if PlaywrightMonthlyReportPageActions._is_gsp_home_url(current_url):
            return page
        if not PlaywrightMonthlyReportPageActions._is_gsp_login_url(current_url):
            raise LazadaMonthlyReportError("紫鸟打开了未识别的 GSP 页面，已停止自动登录")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if PlaywrightMonthlyReportPageActions._visible_login_challenge(page):
                raise LazadaMonthlyReportError("GSP 登录出现验证码或安全验证，请人工完成后重试")
            if PlaywrightMonthlyReportPageActions._submit_prefilled_login(page):
                break
            page.wait_for_timeout(500)
        else:
            raise LazadaMonthlyReportError(
                "GSP 登录页没有唯一且已由紫鸟预填的账号、密码和登录按钮"
            )

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            candidates = PlaywrightMonthlyReportPageActions._context_pages(page)
            home_pages = [
                candidate
                for candidate in candidates
                if PlaywrightMonthlyReportPageActions._is_gsp_home_url(
                    str(getattr(candidate, "url", "") or "")
                )
            ]
            if home_pages:
                return home_pages[-1]
            if not any(
                _url_matches_gsp(str(getattr(candidate, "url", "") or ""))
                for candidate in candidates
            ):
                raise LazadaMonthlyReportError("GSP 登录后跳转到了非 Lazada GSP 站点")
            page.wait_for_timeout(500)
        raise LazadaMonthlyReportError("GSP 登录提交后未进入跨境工作台首页")

    @staticmethod
    def _gsp_label_visible(page: Any, labels: tuple[str, ...]) -> bool:
        try:
            visible = []
            for label in labels:
                locator = page.get_by_text(label, exact=True)
                for index in range(locator.count()):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        visible.append(candidate)
            return len(visible) == 1
        except Exception:
            return False

    @staticmethod
    def _activate_gsp_label(page: Any, labels: tuple[str, ...], action: str) -> bool:
        try:
            visible = []
            for label in labels:
                locator = page.get_by_text(label, exact=True)
                for index in range(locator.count()):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        visible.append(candidate)
            if len(visible) != 1:
                return False
            if action == "hover":
                visible[0].hover(timeout=10_000)
            elif action == "click":
                visible[0].click(timeout=10_000)
            else:
                return False
            return True
        except Exception as exc:
            raise LazadaMonthlyReportError("无法确认 GSP 导航菜单状态，已停止任务") from exc

    @staticmethod
    def _open_income_from_gsp(page: Any, country: str) -> Any:
        if not _url_matches_gsp(str(page.url or "")):
            raise LazadaMonthlyReportError("当前页面不是受信任的 Lazada GSP 工作台")
        page = PlaywrightMonthlyReportPageActions._ensure_gsp_home(page)

        finance_labels = ("财务", "Finance")
        statement_labels = (
            "账户对账单",
            "Account Statement",
            "Account Statements",
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if PlaywrightMonthlyReportPageActions._gsp_label_visible(page, finance_labels):
                break
            page.wait_for_timeout(500)
        else:
            raise LazadaMonthlyReportError("GSP 工作台加载完成后仍未显示“财务 / Finance”入口")

        if not PlaywrightMonthlyReportPageActions._activate_gsp_label(
            page, finance_labels, "hover"
        ):
            raise LazadaMonthlyReportError("GSP 页面没有唯一的“财务 / Finance”入口")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if PlaywrightMonthlyReportPageActions._gsp_label_visible(page, statement_labels):
                break
            page.wait_for_timeout(250)
        else:
            PlaywrightMonthlyReportPageActions._activate_gsp_label(
                page, finance_labels, "click"
            )
            PlaywrightMonthlyReportPageActions._activate_gsp_label(
                page, finance_labels, "hover"
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if PlaywrightMonthlyReportPageActions._gsp_label_visible(page, statement_labels):
                    break
                page.wait_for_timeout(250)
            else:
                raise LazadaMonthlyReportError("GSP 财务菜单没有“账户对账单”入口")

        before_pages = PlaywrightMonthlyReportPageActions._context_pages(page)
        before_state = {
            id(candidate): (
                str(getattr(candidate, "url", "") or ""),
                PlaywrightMonthlyReportPageActions._page_body_text(candidate)
                if _is_any_income_route(str(getattr(candidate, "url", "") or ""))
                else "",
            )
            for candidate in before_pages
        }
        if not PlaywrightMonthlyReportPageActions._activate_gsp_label(
            page, statement_labels, "click"
        ):
            raise LazadaMonthlyReportError("GSP 页面没有唯一的“账户对账单”入口")

        deadline = time.monotonic() + 120
        auth_seen = False
        wrong_country_seen = False
        poll_count = 0
        while time.monotonic() < deadline:
            poll_count += 1
            all_pages = PlaywrightMonthlyReportPageActions._context_pages(page)
            changed: list[Any] = []
            unchanged_target: list[Any] = []
            for candidate in all_pages:
                current_url = str(getattr(candidate, "url", "") or "")
                previous = before_state.get(id(candidate))
                body = ""
                changed_since_click = previous is None or previous[0] != current_url
                if _is_any_income_route(current_url):
                    body = PlaywrightMonthlyReportPageActions._page_body_text(candidate)
                    changed_since_click = changed_since_click or bool(
                        previous is not None and previous[1] != body
                    )
                if changed_since_click or candidate is page:
                    changed.append(candidate)
                elif _income_route_matches_country(current_url, country):
                    unchanged_target.append(candidate)

            # Prefer a tab created or changed by this click.  Some GSP builds
            # instead focus an already-open Seller Center tab; after a short grace
            # window, accepting that already verified target avoids a false timeout.
            candidates = changed + (unchanged_target if poll_count >= 6 else [])
            for candidate in candidates:
                current_url = str(getattr(candidate, "url", "") or "")
                if _income_route_matches_country(current_url, country):
                    body = PlaywrightMonthlyReportPageActions._page_body_text(candidate)
                    if PlaywrightMonthlyReportPageActions._income_permission_denied(body):
                        raise LazadaMonthlyReportError(
                            "通过 GSP 进入后，Lazada 当前店铺仍没有“账户对账单”权限"
                        )
                    if re.search(r"我的收入|My\s+Income", body, re.IGNORECASE) and re.search(
                        r"月度报告|Monthly\s+Report|收入账单|Income\s+Statement",
                        body,
                        re.IGNORECASE,
                    ):
                        return candidate
                elif _url_matches_country(current_url, country) and (
                    PlaywrightMonthlyReportPageActions._is_auth_page_url(current_url)
                ):
                    auth_seen = True
                elif _is_any_income_route(current_url):
                    wrong_country_seen = True
            page.wait_for_timeout(500)

        if wrong_country_seen:
            raise LazadaMonthlyReportError("GSP 的账户对账单跳转到了错误国家站点")
        if auth_seen:
            raise LazadaMonthlyReportError(
                "GSP 单点登录未完成；请在紫鸟店铺中重新登录 Lazada 后重试"
            )
        raise LazadaMonthlyReportError("GSP 点击账户对账单后未打开目标 My Income 页面")

    @staticmethod
    def _open_income_page(page: Any, country: str) -> Any:
        current_url = str(getattr(page, "url", "") or "")
        if _income_route_matches_country(current_url, country):
            body = PlaywrightMonthlyReportPageActions._page_body_text(page)
            if PlaywrightMonthlyReportPageActions._income_permission_denied(body):
                raise LazadaMonthlyReportError(
                    "Lazada 当前店铺账号没有“账户对账单”权限，请使用所有者账号授权后重试"
                )
            if re.search(r"我的收入|My\s+Income", body, re.IGNORECASE):
                return page

        gsp_page = PlaywrightMonthlyReportPageActions._find_gsp_page(page)
        if gsp_page is not None:
            return PlaywrightMonthlyReportPageActions._open_income_from_gsp(
                gsp_page, country
            )

        return PlaywrightMonthlyReportPageActions._open_income_page_direct(page, country)

    @staticmethod
    def _ensure_country_download_session(page: Any, country: str) -> None:
        legacy_origins = [
            origin
            for origin in COUNTRIES[country]["origins"]
            if not (urlsplit(origin).hostname or "").casefold().endswith(".lazada-seller.cn")
        ]
        if len(legacy_origins) != 1:
            raise LazadaMonthlyReportError(f"无法确定 {COUNTRIES[country]['name']} 文件下载会话域名")
        legacy_url = f"{legacy_origins[0]}{INCOME_PAGE_PATH}"
        context = page.context
        bridge_page = context.new_page()
        try:
            try:
                bridge_page.goto(legacy_url, wait_until="domcontentloaded", timeout=90_000)
            except Exception:
                try:
                    bridge_page.evaluate("window.stop()")
                except Exception:
                    pass
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                current_url = str(bridge_page.url or "")
                body = PlaywrightMonthlyReportPageActions._page_body_text(bridge_page)
                if PlaywrightMonthlyReportPageActions._is_auth_page_url(current_url):
                    raise LazadaMonthlyReportError(
                        f"{COUNTRIES[country]['name']} 文件下载会话未登录，请在紫鸟店铺重新登录 Lazada"
                    )
                if _income_route_matches_country(current_url, country):
                    if PlaywrightMonthlyReportPageActions._income_permission_denied(body):
                        raise LazadaMonthlyReportError(
                            f"{COUNTRIES[country]['name']} 旧 Seller Center 没有账户对账单权限"
                        )
                    if re.search(r"我的收入|My\s+Income", body, re.IGNORECASE):
                        cookies = context.cookies([legacy_url])
                        if any(
                            str(item.get("name") or "") == "t_sid"
                            and bool(str(item.get("value") or ""))
                            for item in cookies
                        ):
                            return
                bridge_page.wait_for_timeout(500)
            raise LazadaMonthlyReportError(
                f"未能建立 {COUNTRIES[country]['name']} Lazada 文件下载会话"
            )
        finally:
            try:
                bridge_page.close()
            except Exception:
                pass

    @staticmethod
    def _open_income_page_direct(page: Any, country: str) -> Any:
        last_error: Exception | None = None
        login_redirect_seen = False
        for origin in COUNTRIES[country]["origins"]:
            url = f"{origin}{INCOME_PAGE_PATH}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                deadline = time.monotonic() + 120
                login_submitted = False
                target_reopened = False
                register_login_opened = False
                while time.monotonic() < deadline:
                    current_url = str(page.url or "")
                    body = PlaywrightMonthlyReportPageActions._page_body_text(page)
                    trusted_country_host = _url_matches_country(current_url, country)
                    if PlaywrightMonthlyReportPageActions._is_auth_page_url(current_url):
                        login_redirect_seen = True
                        if not trusted_country_host:
                            raise LazadaMonthlyReportError(
                                "Lazada 登录页跳转到非目标国家站点，已拒绝提交预填凭据"
                            )
                        if not PlaywrightMonthlyReportPageActions._is_login_page_url(current_url):
                            if not register_login_opened:
                                login_url = (
                                    PlaywrightMonthlyReportPageActions._trusted_register_login_url(
                                        page, country, url
                                    )
                                )
                                if login_url:
                                    page.goto(
                                        login_url,
                                        wait_until="domcontentloaded",
                                        timeout=90_000,
                                    )
                                    register_login_opened = True
                                    continue
                            page.wait_for_timeout(1_000)
                            continue
                        if PlaywrightMonthlyReportPageActions._visible_login_challenge(page):
                            raise LazadaMonthlyReportError(
                                "Lazada 登录出现验证码或安全验证，请人工完成后重试"
                            )
                        if not login_submitted:
                            login_submitted = PlaywrightMonthlyReportPageActions._submit_prefilled_login(page)
                        page.wait_for_timeout(1_000)
                        continue
                    if not trusted_country_host:
                        raise LazadaMonthlyReportError(
                            "Lazada 页面跳转到非目标国家站点，已停止任务"
                        )
                    if PlaywrightMonthlyReportPageActions._income_permission_denied(body):
                        raise LazadaMonthlyReportError(
                            "Lazada 当前店铺账号没有“账户对账单”权限，请使用所有者账号授权后重试"
                        )
                    if re.search(r"我的收入|My\s+Income", body, re.IGNORECASE):
                        return page
                    if login_submitted and not target_reopened:
                        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                        target_reopened = True
                        continue
                    page.wait_for_timeout(1_000)
            except LazadaMonthlyReportError:
                raise
            except Exception as exc:
                last_error = exc
                try:
                    page.evaluate("window.stop()")
                except Exception:
                    pass
        if last_error is not None:
            raise LazadaMonthlyReportError(f"无法进入 {COUNTRIES[country]['name']} My Income 页面") from last_error
        if login_redirect_seen:
            raise LazadaMonthlyReportError(
                "Lazada 登录未完成；请检查紫鸟店铺预填账号密码或人工完成安全验证后重试"
            )
        raise LazadaMonthlyReportError(f"无法确认 {COUNTRIES[country]['name']} My Income 页面")

    @staticmethod
    def _dismiss_safe_popups(page: Any, max_rounds: int = 4) -> int:
        dismissed = 0
        for _ in range(max_rounds):
            clicked = bool(
                page.evaluate(
                    r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const safe = /^(Skip|跳过|Got it|知道了|Close|关闭)$/i;
  const target = Array.from(document.querySelectorAll('button,a,[role="button"]'))
    .find((el) => visible(el) && safe.test(clean(el.innerText || el.textContent || el.getAttribute('aria-label'))));
  if (!target) return false; target.click(); return true;
}
"""
                )
            )
            if not clicked:
                break
            dismissed += 1
            page.wait_for_timeout(500)
        return dismissed

    @staticmethod
    def _click_monthly_report_tab(page: Any) -> bool:
        clicked = bool(
            page.evaluate(
                r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const nodes = Array.from(document.querySelectorAll('[role="tab"],button,a,span,div'))
    .filter((el) => visible(el) && /^(月度报告|Monthly Report)$/i.test(clean(el.innerText || el.textContent)));
  if (!nodes.length) return false;
  nodes.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
  nodes[0].click(); return true;
}
"""
            )
        )
        if clicked:
            page.wait_for_timeout(1_000)
        return clicked

    @staticmethod
    def _matching_month_rows(rows: list[dict[str, Any]], month: str) -> list[dict[str, Any]]:
        matched: list[dict[str, Any]] = []
        for row in rows:
            period = parse_monthly_report_period(row.get("periodText") or row.get("text"))
            if period and period_matches_month(period[0], period[1], month):
                matched.append(row)
        return matched

    def _wait_monthly_rows(
        self,
        page: Any,
        timeout: float = 30,
        *,
        target_month: str = "",
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        previous_signature: tuple[str, ...] = ()
        stable_polls = 0
        latest_rows: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            rows = self._extract_monthly_rows(page)
            if target_month and self._matching_month_rows(rows, target_month):
                return rows
            if rows:
                latest_rows = rows
                signature = tuple(str(row.get("periodText") or "") for row in rows)
                stable_polls = stable_polls + 1 if signature == previous_signature else 1
                previous_signature = signature
                if stable_polls >= 4:
                    return rows
            page.wait_for_timeout(500)
        return latest_rows

    def _search_monthly_report_pages(
        self,
        page: Any,
        month: str,
        *,
        max_pages: int = 24,
        initial_rows: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        last_rows: list[dict[str, Any]] = []
        visited: set[tuple[str, ...]] = set()
        rows = initial_rows or self._wait_monthly_rows(page, timeout=10, target_month=month)
        for _ in range(max_pages):
            last_rows = rows
            if self._matching_month_rows(rows, month):
                return rows
            signature = tuple(str(row.get("periodText") or "") for row in rows)
            if not signature or signature in visited:
                break
            visited.add(signature)
            if not self._click_next_monthly_page(page):
                break
            rows = self._wait_for_monthly_rows_change(page, signature)
            if not rows:
                break
        return last_rows

    def _wait_for_monthly_rows_change(
        self,
        page: Any,
        previous_signature: tuple[str, ...],
        *,
        timeout: float = 15,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self._extract_monthly_rows(page)
            signature = tuple(str(row.get("periodText") or "") for row in rows)
            if signature and signature != previous_signature:
                return rows
            page.wait_for_timeout(500)
        return []

    @staticmethod
    def _click_more_monthly_reports(page: Any) -> bool:
        clicked = bool(
            page.evaluate(
                r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const labels = Array.from(document.querySelectorAll('[role="tab"],button,a,span,div'))
    .filter((el) => visible(el) && /^(月度报告|Monthly Report)$/i.test(clean(el.innerText || el.textContent)));
  const candidates = [];
  for (const label of labels) {
    let scope = label.parentElement;
    while (scope && scope !== document.body) {
      const more = Array.from(scope.querySelectorAll('button,a,[role="button"]'))
        .find((el) => visible(el) && /^(更多|More)\s*[>›»]?$/i.test(clean(el.innerText || el.textContent)));
      if (more) { candidates.push({scope, more}); break; }
      scope = scope.parentElement;
    }
  }
  if (!candidates.length) return false;
  candidates.sort((a, b) => a.scope.querySelectorAll('*').length - b.scope.querySelectorAll('*').length);
  candidates[0].more.click(); return true;
}
"""
            )
        )
        if clicked:
            page.wait_for_timeout(1_000)
        return clicked

    @staticmethod
    def _click_next_monthly_page(page: Any) -> bool:
        return bool(
            page.evaluate(
                r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const containers = Array.from(document.querySelectorAll('.next-pagination,.ant-pagination,[class*="pagination"],[role="navigation"]'))
    .filter(visible);
  for (const scope of containers) {
    const target = Array.from(scope.querySelectorAll('button,a,[role="button"]')).find((el) => {
      if (!visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true' || /disabled/i.test(el.className || '')) return false;
      const label = clean(el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || el.textContent);
      return /^(下一页|Next(?: Page)?|>|›|»)$/i.test(label);
    });
    if (target) { target.click(); return true; }
  }
  return false;
}
"""
            )
        )

    @staticmethod
    def _extract_monthly_rows(page: Any) -> list[dict[str, Any]]:
        return page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const period = /(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\s*[-–—~至]\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日?\s*[-–—~至]\s*(?:\d{4}年)?\d{1,2}月\d{1,2}日?|\d{1,2}\s+[A-Za-z]{3,9}(?:\s+\d{4})?\s*[-–—~]\s*\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})/g;
  const seenNodes = new Set(); const rows = [];
  const buttons = Array.from(document.querySelectorAll('button,a,[role="button"]'))
    .filter((el) => visible(el) && /^(下载|Download)$/i.test(clean(el.innerText || el.textContent)));
  for (const button of buttons) {
    const node = button.closest('.report-item,tr,li,[role="row"]');
    if (!node || !visible(node)) continue;
    const text = clean(node.innerText || node.textContent);
    const matches = Array.from(text.matchAll(period)).map((match) => match[0]);
    if (matches.length !== 1 || seenNodes.has(node)) continue;
    seenNodes.add(node); rows.push({periodText: matches[0], text});
  }
  return rows;
}
"""
        ) or []

    @staticmethod
    def _click_row_download(page: Any, period_text: str) -> bool:
        return bool(
            page.evaluate(
                r"""
(wanted) => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const candidates = [];
  const buttons = Array.from(document.querySelectorAll('button,a,[role="button"]'))
    .filter((item) => visible(item) && /^(下载|Download)$/i.test(clean(item.innerText || item.textContent)));
  for (const button of buttons) {
    const row = button.closest('.report-item,tr,li,[role="row"]');
    if (!row || !visible(row)) continue;
    const text = clean(row.innerText || row.textContent);
    if (text.includes(wanted)) candidates.push(button);
  }
  if (candidates.length !== 1) return false;
  candidates[0].click(); return true;
}
""",
                period_text,
            )
        )

    @staticmethod
    def _visible_download_options(page: Any) -> list[str]:
        return page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  return Array.from(document.querySelectorAll('li,[role="menuitem"],.next-menu-item'))
    .filter(visible).map((el) => clean(el.innerText || el.textContent)).filter(Boolean);
}
"""
        ) or []

    def _wait_visible_download_options(self, page: Any, timeout: float = 5) -> list[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            options = self._visible_download_options(page)
            if options:
                return options
            page.wait_for_timeout(250)
        return []

    @staticmethod
    def _choose_download_option(options: list[str]) -> str:
        normalized = {re.sub(r"\s+", " ", item).strip().casefold(): item for item in options}
        for known in KNOWN_DOWNLOAD_OPTIONS:
            selected = normalized.get(re.sub(r"\s+", " ", known).strip().casefold())
            if selected:
                return selected
        supported_pattern = re.compile(
            r"^(?:monthly\s+report|月度报告|order\s+details|transactions?\s+details|订单(?:详情|明细))"
            r"\s*(?:\(|（)?(?:excel|xlsx)(?:\)|）)?$",
            re.IGNORECASE,
        )
        excel_options = [item for item in options if supported_pattern.fullmatch(item.strip())]
        return excel_options[0] if len(excel_options) == 1 else ""

    @staticmethod
    def _choose_download_history_option(options: list[str]) -> str:
        allowed = {
            re.sub(r"\s+", " ", label).strip().casefold()
            for label in DOWNLOAD_HISTORY_OPTIONS
        }
        matches = [
            item
            for item in options
            if re.sub(r"\s+", " ", item).strip().casefold() in allowed
        ]
        return matches[0] if len(matches) == 1 else ""

    @staticmethod
    def _click_download_option(page: Any, option: str) -> bool:
        return bool(
            page.evaluate(
                r"""
(wanted) => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const target = Array.from(document.querySelectorAll('li,[role="menuitem"],.next-menu-item'))
    .find((el) => visible(el) && clean(el.innerText || el.textContent) === wanted);
  if (!target) return false; target.click(); return true;
}
""",
                option,
            )
        )

    def _open_export_history(self, page: Any, period_text: str) -> bool:
        if self._export_history_visible(page):
            return True
        if not self._click_row_download(page, period_text):
            return False
        options = self._wait_visible_download_options(page)
        history_option = self._choose_download_history_option(options)
        if not history_option or not self._click_download_option(page, history_option):
            return False
        return self._wait_for_export_history_state(page, visible=True)

    @classmethod
    def _trigger_download_option(
        cls,
        page: Any,
        option: str,
        *,
        country: str,
    ) -> tuple[bool, str]:
        """Click once and bind to the export id returned by the creation API when available."""
        try:
            response_context = page.expect_response(
                lambda response: _is_export_creation_response(response, country),
                timeout=15_000,
            )
        except Exception as exc:
            raise LazadaMonthlyReportError(
                "无法监听 Lazada 导出创建响应，已停止点击下载项"
            ) from exc

        clicked = False
        try:
            with response_context as response_info:
                clicked = cls._click_download_option(page, option)
            try:
                body = response_info.value.json()
            except Exception:
                body = None
            return clicked, _find_creation_export_id(body)
        except Exception:
            # Never click twice: an evaluation/response error may happen after the
            # browser already accepted the original click.
            return clicked, ""

    def _wait_async_export(
        self,
        page: Any,
        download_path: Path,
        *,
        existing_export_ids: set[str] | None = None,
        expected_export_id: str = "",
        country: str = "",
        timeout: float = 210,
    ) -> Path | None:
        deadline = time.monotonic() + timeout
        baseline = {str(value) for value in (existing_export_ids or set()) if str(value)}
        expected_id = str(expected_export_id or "").strip()
        if not expected_id:
            raise LazadaMonthlyReportError(
                "未取得本次导出的唯一 ID，已拒绝点击下载历史文件"
            )
        if expected_id in baseline:
            raise LazadaMonthlyReportError("本次导出 ID 在点击前已经存在，已拒绝下载历史文件")
        while time.monotonic() < deadline:
            rows = self._export_rows(page)
            target = next(
                (row for row in rows if str(row.get("exportId") or "") == expected_id),
                None,
            )
            if target:
                status = str(target.get("status") or "")
                if re.search(r"failed|失败", status, re.IGNORECASE):
                    raise LazadaMonthlyReportError(f"月度报告导出失败：exportId={expected_id}")
                if (
                    target.get("hasDownloadLink")
                    and re.search(r"finished|completed|success|完成|已完成", status, re.IGNORECASE)
                ):
                    return self._download_export_link(
                        page,
                        expected_id,
                        download_path,
                        country=country,
                    )
            page.wait_for_timeout(500)
        return None

    @staticmethod
    def _all_export_ids(page: Any) -> set[str]:
        rows = page.evaluate(
            r"""
() => {
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"],.next-dialog,.ant-modal,.next-overlay-inner'))
    .filter((el) => /导出记录|Download Result File/i.test(clean(el.innerText || el.textContent)));
  const ids = [];
  for (const dialog of dialogs) {
    for (const row of Array.from(dialog.querySelectorAll('tr')).slice(1)) {
      const first = row.querySelector('td'); const value = clean(first && (first.innerText || first.textContent));
      if (/^\d+$/.test(value)) ids.push(value);
    }
  }
  return Array.from(new Set(ids));
}
"""
        ) or []
        return {str(value) for value in rows if str(value)}

    @staticmethod
    def _export_history_visible(page: Any) -> bool:
        return bool(
            page.evaluate(
                r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  return Array.from(document.querySelectorAll('[role="dialog"],.next-dialog,.ant-modal,.next-overlay-inner'))
    .some((el) => visible(el) && /导出记录|Download Result File/i.test(clean(el.innerText || el.textContent)));
}
"""
            )
        )

    def _wait_for_export_history_state(
        self,
        page: Any,
        *,
        visible: bool,
        timeout: float = 10,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._export_history_visible(page) is visible:
                return True
            page.wait_for_timeout(250)
        return self._export_history_visible(page) is visible

    @staticmethod
    def _export_history_load_state(page: Any) -> dict[str, Any]:
        return page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"],.next-dialog,.ant-modal,.next-overlay-inner'))
    .filter((el) => visible(el) && /导出记录|Download Result File/i.test(clean(el.innerText || el.textContent)));
  if (!dialogs.length) return {visible: false, ready: false, ids: []};
  dialogs.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
  const dialog = dialogs[0];
  const headers = Array.from(dialog.querySelectorAll('thead th,tr:first-child th'))
    .map((cell) => clean(cell.innerText || cell.textContent));
  const contract = headers.length >= 5
    && /^#$/.test(headers[0])
    && /导出时间|Export Time/i.test(headers[1])
    && /文件名|File Name/i.test(headers[2])
    && /状态|Status/i.test(headers[3])
    && /操作|Action|Operation/i.test(headers[4]);
  const loading = Array.from(dialog.querySelectorAll(
    '[aria-busy="true"],.next-loading,.next-loading-tip,.ant-spin-spinning,[class*="loading"]'
  )).some(visible);
  const ids = Array.from(dialog.querySelectorAll('tbody tr,tr')).map((row) => {
    const first = row.querySelector('td');
    return clean(first && (first.innerText || first.textContent));
  }).filter((value) => /^\d+$/.test(value));
  const text = clean(dialog.innerText || dialog.textContent);
  const explicitEmpty = /暂无数据|暂无记录|没有数据|No Data|No Records/i.test(text);
  return {
    visible: true,
    ready: Boolean(contract && !loading && (ids.length > 0 || explicitEmpty)),
    ids: Array.from(new Set(ids)),
  };
}
"""
        ) or {"visible": False, "ready": False, "ids": []}

    def _wait_export_history_snapshot(
        self,
        page: Any,
        *,
        timeout: float = 20,
    ) -> set[str] | None:
        deadline = time.monotonic() + timeout
        previous: tuple[str, ...] | None = None
        stable_polls = 0
        while time.monotonic() < deadline:
            state = self._export_history_load_state(page)
            if state.get("ready"):
                signature = tuple(str(value) for value in (state.get("ids") or []) if str(value))
                stable_polls = stable_polls + 1 if signature == previous else 1
                previous = signature
                # A briefly mounted empty table is common while Lazada fetches old
                # rows.  Require a materially longer empty-state observation so an
                # old row cannot arrive just after a falsely trusted empty baseline.
                required_polls = 10 if not signature else 2
                if stable_polls >= required_polls:
                    return set(signature)
            else:
                previous = None
                stable_polls = 0
            page.wait_for_timeout(300)
        return None

    @staticmethod
    def _export_rows(page: Any) -> list[dict[str, Any]]:
        return page.evaluate(
            r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const dialog = Array.from(document.querySelectorAll('[role="dialog"],.next-dialog,.ant-modal,.next-overlay-inner'))
    .find((el) => visible(el) && /导出记录|Download Result File/i.test(clean(el.innerText || el.textContent)));
  if (!dialog) return [];
  return Array.from(dialog.querySelectorAll('tr')).slice(1).map((row) => {
    const cells = Array.from(row.querySelectorAll('td')).map((cell) => clean(cell.innerText || cell.textContent));
    const link = Array.from(row.querySelectorAll('a')).find((item) => /^(Download Result File|下载结果文件|下载文件)$/i.test(clean(item.innerText || item.textContent)));
    return {
      exportId: cells[0] || '',
      exportTime: cells[1] || '',
      fileName: cells[2] || '',
      status: cells[3] || '',
      hasDownloadLink: Boolean(link),
    };
  }).filter((row) => /^\d+$/.test(row.exportId));
}
"""
        ) or []

    @staticmethod
    def _export_link_info(page: Any, export_id: str) -> dict[str, str]:
        return page.evaluate(
            r"""
(wanted) => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"],.next-dialog,.ant-modal,.next-overlay-inner'))
    .filter((el) => visible(el) && /导出记录|Download Result File/i.test(clean(el.innerText || el.textContent)));
  dialogs.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
  const dialog = dialogs[0];
  if (!dialog) return {};
  const matches = Array.from(dialog.querySelectorAll('tr')).filter((row) => {
    const first = row.querySelector('td');
    return clean(first && (first.innerText || first.textContent)) === wanted;
  });
  if (matches.length !== 1) return {};
  const cells = Array.from(matches[0].querySelectorAll('td'));
  const links = Array.from(matches[0].querySelectorAll('a')).filter((item) =>
    visible(item) && /^(Download Result File|下载结果文件|下载文件)$/i.test(clean(item.innerText || item.textContent))
  );
  if (links.length !== 1) return {};
  return {
    href: links[0].href || links[0].getAttribute('href') || '',
    fileName: clean(cells[2] && (cells[2].innerText || cells[2].textContent)),
  };
}
""",
            export_id,
        ) or {}

    def _download_export_link(
        self,
        page: Any,
        export_id: str,
        download_path: Path,
        *,
        country: str,
    ) -> Path:
        info = self._export_link_info(page, export_id)
        href = str(info.get("href") or "").strip()
        file_name = str(info.get("fileName") or "").strip()
        if not _url_matches_report_download(href, country):
            raise LazadaMonthlyReportError(
                f"本次导出记录的下载地址不属于 {COUNTRIES.get(country, {}).get('name', country)} 可信域名"
            )
        suffix = Path(urlsplit(href).path).suffix.lower()
        file_suffix = Path(file_name).suffix.lower()
        if file_suffix in SUPPORTED_REPORT_SUFFIXES:
            suffix = file_suffix
        if suffix not in SUPPORTED_REPORT_SUFFIXES:
            raise LazadaMonthlyReportError(f"本次导出记录返回了不支持的文件类型：{suffix or '未知'}")

        download_path.mkdir(parents=True, exist_ok=True)
        try:
            with page.expect_download(timeout=60_000) as download_info:
                clicked = self._click_export_link(page, export_id)
            if not clicked:
                raise LazadaMonthlyReportError(f"无法点击本次导出记录：exportId={export_id}")
            download = download_info.value
        except LazadaMonthlyReportError:
            raise
        except Exception as exc:
            raise LazadaMonthlyReportError(f"点击导出记录后未收到下载事件：exportId={export_id}") from exc
        failure = download.failure()
        if failure:
            raise LazadaMonthlyReportError(f"Lazada 导出文件下载失败：{failure}")
        event_url = str(getattr(download, "url", "") or "").strip()
        if not _url_matches_report_download(event_url, country):
            raise LazadaMonthlyReportError(
                f"浏览器下载事件不属于 {COUNTRIES.get(country, {}).get('name', country)} 可信域名"
            )

        target = download_path / f"lazada-export-{export_id}{suffix}"
        part = download_path / f".{target.name}.{uuid.uuid4().hex}.part"
        try:
            download.save_as(str(part))
            os.replace(part, target)
        finally:
            if part.exists():
                part.unlink(missing_ok=True)
        return target

    @staticmethod
    def _click_export_link(page: Any, export_id: str) -> bool:
        return bool(
            page.evaluate(
                r"""
(wanted) => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const dialog = Array.from(document.querySelectorAll('[role="dialog"],.next-dialog,.ant-modal,.next-overlay-inner'))
    .find((el) => visible(el) && /导出记录|Download Result File/i.test(clean(el.innerText || el.textContent)));
  if (!dialog) return false;
  for (const row of Array.from(dialog.querySelectorAll('tr'))) {
    const cells = Array.from(row.querySelectorAll('td'));
    if (!cells.length || clean(cells[0].innerText || cells[0].textContent) !== wanted) continue;
    const link = Array.from(row.querySelectorAll('a')).find((item) => /^(Download Result File|下载结果文件|下载文件)$/i.test(clean(item.innerText || item.textContent)));
    if (!link) return false; link.click(); return true;
  }
  return false;
}
""",
                export_id,
            )
        )

    @staticmethod
    def _close_export_modal(page: Any) -> None:
        try:
            page.evaluate(
                r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"],.next-dialog,.ant-modal,.next-overlay-inner'))
    .filter((el) => visible(el) && /导出记录|Download Result File/i.test(clean(el.innerText || el.textContent)));
  dialogs.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
  const dialog = dialogs[0];
  if (!dialog) return;
  const target = Array.from(dialog.querySelectorAll('button,a,[role="button"],.next-dialog-close,.ant-modal-close'))
    .find((el) => visible(el) && /^(OK|确定|取消|Close|关闭)$/i.test(clean(el.innerText || el.textContent || el.getAttribute('aria-label'))));
  if (target) target.click();
}
"""
            )
        except Exception:
            pass


def _url_matches_report_download(url: str, country: str) -> bool:
    try:
        parsed = urlsplit(str(url or ""))
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    if country not in REPORT_DOWNLOAD_HOSTS:
        return False
    if parsed.scheme.casefold() != "https" or port not in (None, 443):
        return False
    if parsed.username or parsed.password:
        return False
    if hostname not in REPORT_DOWNLOAD_HOSTS[country]:
        return False
    return parsed.path.casefold().startswith("/sf-t/")


def _url_matches_country(url: str, country: str) -> bool:
    try:
        parsed = urlsplit(str(url or ""))
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme.casefold() != "https" or port not in (None, 443):
        return False
    allowed_hosts = {
        (urlsplit(origin).hostname or "").casefold().rstrip(".")
        for origin in COUNTRIES[country]["origins"]
    }
    return hostname in allowed_hosts


def _url_matches_store_front(url: str) -> bool:
    """是否是「紫鸟店铺已经打开」的页面。

    紫鸟开店后可能落在三类地址：跨境 GSP（gsp.*）、跨境卖家中心
    （sellercenter-<cc>.lazada-seller.cn）、本地卖家中心（sellercenter.lazada.<cc>）。
    只认 GSP 会让本土店的开店等待白等到超时。
    """
    if _url_matches_gsp(url):
        return True
    try:
        parsed = urlsplit(str(url or ""))
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme.casefold() != "https" or port not in (None, 443):
        return False
    # 跨境店：sellercenter-<cc>.lazada-seller.cn
    if hostname.endswith(".lazada-seller.cn"):
        return True
    # 本土店：sellercenter.lazada.co.th / .com.ph / .com.my / .co.id / .vn
    if hostname.startswith("sellercenter.") and hostname.endswith(
        LOCAL_STORE_FRONT_SUFFIXES
    ):
        return True
    return False


def _url_matches_gsp(url: str) -> bool:
    try:
        parsed = urlsplit(str(url or ""))
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme.casefold() != "https" or port not in (None, 443):
        return False
    allowed_hosts = {
        (urlsplit(origin).hostname or "").casefold().rstrip(".")
        for origin in GSP_ORIGINS
    }
    return hostname in allowed_hosts


def _income_route_matches_country(url: str, country: str) -> bool:
    if not _url_matches_country(url, country):
        return False
    try:
        path = urlsplit(str(url or "")).path.rstrip("/").casefold()
    except ValueError:
        return False
    return path == INCOME_PAGE_PATH.casefold()


def _is_any_income_route(url: str) -> bool:
    return any(_income_route_matches_country(url, country) for country in COUNTRIES)


class _ProgressHandler(logging.Handler):
    def __init__(self, callback: ProgressCallback | None) -> None:
        super().__init__(logging.INFO)
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        if self.callback is not None:
            self.callback(self.format(record))


def _make_logger(run_dir: Path, progress: ProgressCallback | None) -> tuple[logging.Logger, Path]:
    logger = logging.getLogger(f"lazada_monthly_report.{time.time_ns()}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    log_file = run_dir / "run.log"
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    callback_handler = _ProgressHandler(progress)
    callback_handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))
    logger.addHandler(callback_handler)
    return logger, log_file


def _close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.flush()
        finally:
            handler.close()


def _failed_result(
    job: LazadaMonthlyReportQuery,
    country: str,
    requested_store: str,
    actual_store: str,
    status: str,
    message: str,
) -> MonthlyStoreResult:
    return MonthlyStoreResult(
        country=country,
        country_name=str((COUNTRIES.get(country) or {}).get("name") or ""),
        requested_store_name=requested_store,
        store_name=actual_store,
        month=job.month,
        status=status,
        message=message,
        finished_at=_now_text(),
    )


def run_lazada_monthly_report(
    job: LazadaMonthlyReportQuery,
    progress: ProgressCallback | None = None,
    *,
    runtime_factory: RuntimeFactory | None = None,
    page_actions: MonthlyReportPageActions | None = None,
) -> dict[str, Any]:
    root = Path(job.output_root or (Path.cwd() / "outputs"))
    run_dir = (
        root
        / "lazada_monthly_report"
        / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    logger, log_file = _make_logger(run_dir, progress)
    factory = runtime_factory or (lambda query, log: ZiniaoPlaywrightRuntime(query, log))
    actions = page_actions or PlaywrightMonthlyReportPageActions()
    runtime = factory(job, logger)
    results: list[MonthlyStoreResult] = []

    try:
        logger.info(
            "开始 Lazada 月度报告下载：月份=%s，国家=%s，店铺数=%s",
            job.month,
            ",".join(job.countries),
            len(job.store_names),
        )
        runtime.start()
        browsers = runtime.list_browsers()
        matched, unmatched = match_lazada_browsers(browsers, job.store_names)
        for requested_store, reason in unmatched:
            for country in job.countries:
                results.append(
                    _failed_result(
                        job,
                        country,
                        requested_store,
                        "",
                        "unmatched_store",
                        reason,
                    )
                )

        filename_groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for requested_store, browser in matched:
            key = safe_store_filename(requested_store).casefold()
            filename_groups.setdefault(key, []).append((requested_store, browser))
        colliding_keys = {key for key, group in filename_groups.items() if len(group) > 1}
        if colliding_keys:
            safe_matched: list[tuple[str, dict[str, Any]]] = []
            for requested_store, browser in matched:
                actual_store = str(browser.get("browserName") or requested_store).strip()
                key = safe_store_filename(requested_store).casefold()
                if key not in colliding_keys:
                    safe_matched.append((requested_store, browser))
                    continue
                names = "、".join(
                    str(item.get("browserName") or requested).strip()
                    for requested, item in filename_groups[key]
                )
                message = f"店铺名清洗后会生成同名文件，已拒绝覆盖：{names}"
                for country in job.countries:
                    results.append(
                        _failed_result(
                            job,
                            country,
                            requested_store,
                            actual_store,
                            "filename_collision",
                            message,
                        )
                    )
            matched = safe_matched

        # One Ziniao profile may expose all three Lazada country sites.  Countries
        # therefore run sequentially inside one opened profile to avoid profile and
        # download-directory contention.  max_concurrent_stores is retained in the
        # validated contract for a future runtime that can prove isolation.
        for requested_store, browser in matched:
            actual_store = str(browser.get("browserName") or requested_store).strip()
            browser_download = run_dir / "_browser_downloads" / safe_store_filename(actual_store)
            opened: OpenedBrowser | None = None
            try:
                logger.info("打开紫鸟店铺：%s", actual_store)
                opened = runtime.open_browser(browser, browser_download)
                for country in job.countries:
                    result = MonthlyStoreResult(
                        country=country,
                        country_name=COUNTRIES[country]["name"],
                        requested_store_name=requested_store,
                        store_name=actual_store,
                        month=job.month,
                    )
                    try:
                        logger.info("%s：下载 %s %s 月度报告", actual_store, country, job.month)
                        downloaded = actions.download_report(
                            opened.page,
                            country,
                            job.month,
                            opened.download_path,
                            progress,
                        )
                        if not period_matches_month(
                            downloaded.period_start,
                            downloaded.period_end,
                            job.month,
                        ):
                            raise LazadaMonthlyReportError(
                                f"报告账期 {downloaded.period_start}～{downloaded.period_end} 与 {job.month} 不完全一致"
                            )
                        destination = run_dir / country / job.month
                        local_file = finalize_store_named_report(
                            downloaded.source_file,
                            destination,
                            requested_store,
                        )
                        result.status = "success"
                        result.message = "月度报告下载完成"
                        result.period_text = downloaded.period_text
                        result.source_file = str(downloaded.source_file)
                        result.local_file = str(local_file)
                        logger.info("%s：%s 月度报告已保存：%s", actual_store, country, local_file)
                    except MonthlyReportNotFoundError as exc:
                        result.status = "no_report"
                        result.message = str(exc)
                        logger.warning("%s：%s", actual_store, exc)
                    except Exception as exc:
                        result.status = "failed"
                        result.message = str(exc)
                        logger.exception("%s：%s 月度报告下载失败", actual_store, country)
                    finally:
                        result.finished_at = _now_text()
                        results.append(result)
            except Exception as exc:
                logger.exception("%s：紫鸟店铺启动或连接失败", actual_store)
                for country in job.countries:
                    results.append(
                        _failed_result(
                            job,
                            country,
                            requested_store,
                            actual_store,
                            "failed",
                            str(exc),
                        )
                    )
            finally:
                if opened is not None:
                    try:
                        runtime.close_browser(opened)
                    except Exception:
                        logger.exception("%s：关闭紫鸟店铺失败；继续处理后续店铺", actual_store)
    except Exception as exc:
        logger.exception("Lazada 月度报告任务失败")
        if not results:
            results.append(_failed_result(job, "", "", "", "failed", str(exc)))
    finally:
        try:
            runtime.shutdown()
        except Exception:
            logger.exception("清理紫鸟运行时失败；保留已完成的月度报告结果")

    success_count = sum(item.status == "success" for item in results)
    no_report_count = sum(item.status == "no_report" for item in results)
    unmatched_count = sum(item.status == "unmatched_store" for item in results)
    # "no_report" is a completed business outcome, not a technical failure.  It
    # remains visible as its own count while only actual failures affect success.
    failed_count = len(results) - success_count - no_report_count
    result_file = run_dir / "result.json"
    serialized = [asdict(item) for item in results]
    payload = {
        "success": failed_count == 0,
        "message": (
            f"处理完成：成功 {success_count}，无报告 {no_report_count}，"
            f"未匹配 {unmatched_count}，失败合计 {failed_count}"
        ),
        "countries": list(job.countries),
        "month": job.month,
        "input_store_count": len(job.store_names),
        "result_count": len(results),
        "success_store_count": success_count,
        "no_report_count": no_report_count,
        "unmatched_store_count": unmatched_count,
        "failed_store_count": failed_count,
        "output_dir": str(run_dir),
        "output_file": str(result_file),
        "log_file": str(log_file),
        "stores": serialized,
        "results": serialized,
    }
    temporary = result_file.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, result_file)
        logger.info(payload["message"])
        return payload
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
        _close_logger(logger)
