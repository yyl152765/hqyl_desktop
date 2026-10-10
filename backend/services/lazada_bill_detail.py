"""采集五个国家 Lazada 后台收支数据并写入钉钉指定 Sheet。

原项目为「泰国 + 4 个国家包装脚本」的写法，这里聚合为一个服务：
- 国家差异（入口、币种、语言、默认工作簿）全部收敛到 COUNTRY_PROFILES；
- 账号、钉钉凭证与操作人来自桌面平台配置（由 app_bridge 注入）；
- 账单区间由前端选择，留空时默认上月 1 日至上月末；
- 数据写入指定 Sheet 的 B:D，截图保存到「截图保存位置」，并保留 F 列图片同步逻辑。

浏览器、钉钉表格与图片上传都可在测试中注入，因此完整运行器可以离线验证。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from backend.services.lazada_ads_data import normalize_workbook_id
from backend.services.lazada_ads_page import (
    WorkflowError,
    is_pending_value,
    parse_amount,
    values_equal,
)
from backend.services.lazada_monthly_report import (
    ZiniaoPlaywrightRuntime,
    safe_store_filename,
)

DEFAULT_SOCKET_PORT = 16851
# 五国账单 Sheet 同处一本钉钉文档：这是共用的默认文档 ID（旧脚本中菲律宾/马来/印尼用它，
# 越南的截图也写到这里）。各国旧脚本里各自写死的文档 ID 仅作为历史信息保留在档案里。
DEFAULT_WORKBOOK_ID = "np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l"
SHOP_COLUMN = "A"
METRIC_COLUMNS = ("B", "C", "D")
IMAGE_COLUMN = "F"
# 列号按 1 起算：A=1…E=5 属于店铺名与账单数据，截图只允许写入 F 列（6）及其右侧。
IMAGE_COLUMN_MIN_NUMBER = 6
IMAGE_HEADER = "图片"
IMAGE_CELL_WIDTH = 240
IMAGE_CELL_HEIGHT = 135
DWS_TIMEOUT_SECONDS = 60
DWS_VERIFY_ATTEMPTS = 5
# 钉钉 F 列图片依赖外部命令行工具 dws（读写钉钉文档）。它不在工程内，必须由使用者的 PATH 提供。
DWS_COMMAND = "dws"
DWS_MISSING_HINT = (
    "本机未安装 dws（PATH 中没有 dws 命令）：本次不会写入钉钉 F 列图片，"
    "截图仍会保存到「截图保存位置」。dws 是读写钉钉文档的命令行工具，"
    "请把它所在目录加入系统 PATH 后重开应用（本机示例：C:\\Users\\<用户名>\\.local\\bin\\dws.exe）。"
)
# 与原脚本一致：一家店最多 3 次采集机会（每次都是全新的登录流程），两次之间稍作等待。
STORE_ATTEMPTS = 3
STORE_RETRY_BACKOFF_MS = 2_000
SCREENSHOT_ROOT_FOLDER = "log"
SCREENSHOT_FOLDER_SUFFIX = "账单明细截图"

_WRITE_LOCK = threading.Lock()
_DWS_SHEET_ID_LOCK = threading.Lock()
_DWS_SHEET_IDS: dict[tuple[str, str], str] = {}


class SheetWriteUncertain(WorkflowError):
    """B:D 已经提交过写入、但回读未确认：绝不能重复采集或重复写表。"""


@dataclass(frozen=True)
class CountryProfile:
    """单个国家的站点参数，替代原脚本的 5 份包装文件。

    `legacy_workbook_id` 只是旧脚本里各国写死的文档 ID（仅作历史参考）；
    运行时统一使用前端填写的共用文档 ID，国家只决定站点入口、币种、语言与目标 Sheet。
    """

    code: str
    name: str
    currency_pattern: str
    finance_url: str
    local_host: str
    cross_border_host: str
    legacy_workbook_id: str
    preferred_language: str | None = None


COUNTRY_PROFILES: dict[str, CountryProfile] = {
    "TH": CountryProfile(
        code="TH",
        name="泰国",
        currency_pattern=r"(?:฿|THB)",
        finance_url=(
            "https://sellercenter.lazada.co.th/portal/apps/finance/myIncome/index"
            "?spm=a1zawg.24399322.navi_left_sidebar."
            "droot_normal_rp_asc_v2_finance_rp_asc_v2_accountstatementnew"
        ),
        local_host="sellercenter.lazada.co.th",
        cross_border_host="sellercenter-th.lazada-seller.cn",
        legacy_workbook_id="14lgGw3P8vvv7b2gCgg3PkQr85daZ90D",
    ),
    "PH": CountryProfile(
        code="PH",
        name="菲律宾",
        currency_pattern=r"(?:₱|PHP)",
        finance_url=(
            "https://sellercenter.lazada.com.ph/portal/apps/finance/myIncome/index"
            "?spm=a1zawj.17752401.navi_left_sidebar."
            "droot_normal_finance_accountstatementnew.15bc1e13LmE9T7"
        ),
        local_host="sellercenter.lazada.com.ph",
        cross_border_host="sellercenter-ph.lazada-seller.cn",
        legacy_workbook_id="np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l",
    ),
    "MY": CountryProfile(
        code="MY",
        name="马来西亚",
        currency_pattern=r"(?:RM|MYR)",
        finance_url=(
            "https://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index"
            "?spm=a1z10uk.home.navi_left_sidebar."
            "droot_normal_finance_accountstatement.4bc52c96elltlA"
        ),
        local_host="sellercenter.lazada.com.my",
        cross_border_host="sellercenter-my.lazada-seller.cn",
        legacy_workbook_id="np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l",
    ),
    "ID": CountryProfile(
        code="ID",
        name="印尼",
        currency_pattern=r"(?:Rp|IDR)",
        finance_url=(
            "https://sellercenter.lazada.co.id/portal/apps/finance/myIncome/index"
            "?spm=a1zawh.portal_apps_finance_myIncome_index.navi_left_sidebar."
            "droot_normal_rp_asc_v2_finance_rp_asc_v2_accountstatementnew.49271e13CiOFYa"
        ),
        local_host="sellercenter.lazada.co.id",
        cross_border_host="sellercenter-id.lazada-seller.cn",
        legacy_workbook_id="np9zOoBVBYnBP2eXsnOjR5qaW1DK0g6l",
        preferred_language="简体中文",
    ),
    "VN": CountryProfile(
        code="VN",
        name="越南",
        currency_pattern=r"(?:₫|VND)",
        finance_url=(
            "https://sellercenter.lazada.vn/portal/apps/finance/myIncome/index"
            "?spm=a1zawf.portal_home.navi_left_sidebar."
            "droot_normal_rp_asc_v2_finance_rp_asc_v2_accountstatementnew.4d771e13QNuRhX"
        ),
        local_host="sellercenter.lazada.vn",
        cross_border_host="sellercenter-vn.lazada-seller.cn",
        legacy_workbook_id="pLdn55X2E5o4yno8",
        preferred_language="简体中文",
    ),
}

_COUNTRY_ALIASES: dict[str, str] = {}
for _code, _profile in COUNTRY_PROFILES.items():
    _COUNTRY_ALIASES[_code.casefold()] = _code
    _COUNTRY_ALIASES[_profile.name] = _code
_COUNTRY_ALIASES["马来"] = "MY"
del _code, _profile

# 旧脚本里各国的默认工作表名（见各国包装脚本的 DEFAULT_SHEET_NAME）。
# 「指定 Sheet」下拉据此优先选中与国家同名的表：马来西亚的表叫「马来」而不是「马来西亚」。
SHEET_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "TH": ("泰国",),
    "PH": ("菲律宾",),
    "MY": ("马来", "马来西亚"),
    "ID": ("印尼", "印度尼西亚"),
    "VN": ("越南",),
}


def sheet_name_aliases(profile: Any) -> tuple[str, ...]:
    """该国家可用于匹配工作表名的别名（第一个是旧脚本的默认表名）。"""
    aliases = SHEET_NAME_ALIASES.get(str(getattr(profile, "code", "")), ())
    return tuple(aliases) or (str(getattr(profile, "name", "")),)


def country_options() -> list[dict[str, str]]:
    return [
        {
            "code": profile.code,
            "name": profile.name,
            "currency": profile.currency_pattern,
            "legacy_workbook_id": profile.legacy_workbook_id,
        }
        for profile in COUNTRY_PROFILES.values()
    ]


def resolve_country(value: Any) -> CountryProfile:
    text = str(value or "").strip()
    if text.upper() in COUNTRY_PROFILES:
        return COUNTRY_PROFILES[text.upper()]
    code = _COUNTRY_ALIASES.get(text.casefold())
    if code is None:
        supported = "、".join(profile.name for profile in COUNTRY_PROFILES.values())
        raise ValueError(f"请选择支持的国家（{supported}）")
    return COUNTRY_PROFILES[code]


def default_sheet_name(today: date | None = None) -> str:
    current = today or date.today()
    return f"{current:%y}年{current.month}月"


def previous_month_range(today: date | None = None) -> dict[str, str]:
    """默认区间：上月 1 日至上月最后一天。"""
    current = today or date.today()
    end = current.replace(day=1) - timedelta(days=1)
    return {"start_date": end.replace(day=1).isoformat(), "end_date": end.isoformat()}


def default_screenshot_root() -> str:
    """默认截图根目录：桌面\\log，其下按国家与账单区间自动建文件夹。"""
    try:
        from backend.config_store import desktop_path

        return str(Path(desktop_path()) / SCREENSHOT_ROOT_FOLDER)
    except Exception:
        return str(Path.cwd() / SCREENSHOT_ROOT_FOLDER)


def screenshot_folder(
    screenshot_root: Any, country_name: str, start_date: Any, end_date: Any
) -> Path:
    """截图实际落地目录：<根目录>\\Lazada<国家名>账单明细截图\\<开始>到<结束>。"""
    folder = f"Lazada{country_name}{SCREENSHOT_FOLDER_SUFFIX}"
    return Path(str(screenshot_root)) / folder / f"{start_date}到{end_date}"


def validate_image_column(column: Any) -> str:
    """钉钉截图列必须是 F 列或其后，A～E 列不允许写图。"""
    text = str(column or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}", text):
        raise ValueError("钉钉图片列必须是字母列名，例如 F")
    index = 0
    for char in text:
        index = index * 26 + (ord(char) - ord("A") + 1)
    if index < IMAGE_COLUMN_MIN_NUMBER:
        raise ValueError("钉钉图片只能写入 F 列或 F 列之后，不能写入 A～E 列")
    return text


# ---------------------------------------------------------------------------
# 店铺名匹配（沿用原脚本的保守规则：只做唯一匹配，绝不模糊猜测）
# ---------------------------------------------------------------------------

def _simplified_shop_text(text: str) -> str:
    """用 Windows 的简体转换能力消除繁简差异；其它平台原样返回。"""
    if os.name == "nt":
        try:
            import ctypes

            convert = ctypes.WinDLL("kernel32", use_last_error=True).LCMapStringEx
            convert.argtypes = [
                ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_int,
                ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_ssize_t,
            ]
            convert.restype = ctypes.c_int
            arguments = ("zh-CN", 0x02000000, text, -1)
            length = convert(*arguments, None, 0, None, None, 0)
            if length:
                buffer = ctypes.create_unicode_buffer(length)
                if convert(*arguments, buffer, length, None, None, 0):
                    return buffer.value
        except (AttributeError, OSError):
            pass
    return text


def normalize_shop_name(value: Any) -> str:
    text = _simplified_shop_text(
        unicodedata.normalize("NFKC", str(value if value is not None else ""))
    ).casefold()
    return re.sub(r"[\s\-‐‑‒–—−﹣_/\\|，,。.【】\[\]（）():：;；·•]+", "", text)


def lazada_store_key(value: Any) -> str:
    text = normalize_shop_name(value)
    marker = re.search(r"lz(?:跨境)?(?:泰国|越南|马来(?:西亚)?|菲律宾|印尼)", text)
    return text[marker.start():] if marker else ""


def _lazada_body_is_extension(short_body: str, candidate_body: str) -> bool:
    """只接受唯一的 LZ 名称后缀，且不把 001 与 0010 混为一谈。"""
    if not short_body or not candidate_body or not short_body[-1:].isdigit():
        return False
    if re.search(r"(?:th|vn|my|ph|id)\d+$", short_body):
        return False
    if not candidate_body.startswith(short_body):
        return False
    suffix = candidate_body[len(short_body):]
    return bool(suffix) and not suffix[0].isdigit()


def lazada_numbered_store_key(value: Any) -> tuple[str, ...] | None:
    """品牌英文可变，但国家、店主、完整编号与中文尾缀不可变。"""
    text = normalize_shop_name(value)
    match = re.fullmatch(
        r"(?P<owner>.+?)lz(?P<cross>跨境)?"
        r"(?P<country>泰国|越南|马来(?:西亚)?|菲律宾|印尼)"
        r"(?P<kind>[^0-9]*?)(?P<store>[0-9]+)"
        r"(?P<code>th|vn|my|ph|id)(?P<account>[0-9]+)(?P<suffix>.+)",
        text,
    )
    if match is None:
        return None
    country_codes = {
        "泰国": "th", "越南": "vn", "马来": "my",
        "马来西亚": "my", "菲律宾": "ph", "印尼": "id",
    }
    if country_codes[match["country"]] != match["code"]:
        return None
    suffix = re.sub(r"[a-z]+", "", match["suffix"])
    if not re.search(r"[\u4e00-\u9fff]", suffix):
        return None
    return (
        match["owner"], match["cross"] or "", match["code"],
        match["kind"], match["store"], match["account"], suffix,
    )


def _identity(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value if value is not None else "")).strip().casefold()


# ---------------------------------------------------------------------------
# 查询与校验
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LazadaBillDetailQuery:
    company: str
    username: str
    password: str = field(repr=False)
    country: str
    country_name: str
    sheet_name: str
    start_date: str
    end_date: str
    store_names: tuple[str, ...]
    dingtalk_app_key: str = field(repr=False)
    dingtalk_app_secret: str = field(repr=False)
    dingtalk_user_id: str = field(repr=False)
    workbook_id: str
    dws_node: str = ""
    browser_window_mode: str = "normal"
    client_path: str = ""
    webdriver_path: str = ""
    output_root: str = ""
    screenshot_root: str = ""
    socket_port: int = DEFAULT_SOCKET_PORT

    @property
    def profile(self) -> CountryProfile:
        return COUNTRY_PROFILES[self.country]

    @property
    def start(self) -> date:
        return date.fromisoformat(self.start_date)

    @property
    def end(self) -> date:
        return date.fromisoformat(self.end_date)


def _validate_iso_date(value: Any, label: str) -> date:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"{label}格式必须为 YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label}不是有效日期") from exc


def validate_image_node(value: Any) -> str:
    node = str(value or "").strip()
    if not node:
        return ""
    parsed = urlsplit(node)
    if (parsed.scheme in {"http", "https"} and parsed.netloc) or re.fullmatch(r"[A-Za-z0-9]{32}", node):
        return node
    raise ValueError("钉钉图片节点格式无效：请填写完整表格 URL 或 32 位 dentryUuid")


def _resolve_image_node(requested: Any, workbook_id: str) -> str:
    """截图默认写进同一本钉钉文档；只有显式配置了其它图片文档才分开。"""
    text = str(requested or "").strip()
    if text:
        return validate_image_node(text)
    try:
        return validate_image_node(workbook_id)
    except ValueError:
        return ""


def validate_lazada_bill_detail_payload(
    payload: dict[str, Any],
    *,
    default_output_root: str = "",
    default_screenshot_root_value: str = "",
    today: date | None = None,
) -> LazadaBillDetailQuery:
    request = dict(payload or {})
    current = today or date.today()
    username = str(request.get("username") or "").strip()
    password = str(request.get("password") or "")
    if not username or not password:
        raise ValueError("请先绑定并选择紫鸟账号")
    credentials = {
        name: str(request.get(name) or "").strip()
        for name in ("dingtalk_app_key", "dingtalk_app_secret", "dingtalk_user_id")
    }
    if not all(credentials.values()):
        raise ValueError("请在设置中配置钉钉应用凭证和操作人")

    profile = resolve_country(request.get("country"))
    sheet_name = str(request.get("sheet_name") or "").strip() or default_sheet_name(current)
    if not sheet_name or len(sheet_name) > 100 or any(ord(ch) < 32 for ch in sheet_name):
        raise ValueError("指定 Sheet 名称无效")

    fallback = previous_month_range(current)
    start = _validate_iso_date(request.get("start_date") or fallback["start_date"], "开始日期")
    end = _validate_iso_date(request.get("end_date") or fallback["end_date"], "结束日期")
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    if end > current:
        raise ValueError("结束日期不能晚于今天")

    raw_names = request.get("store_names") or []
    if isinstance(raw_names, str):
        raw_names = raw_names.replace("，", "\n").replace(",", "\n").splitlines()
    if not isinstance(raw_names, (list, tuple)):
        raise ValueError("请输入店铺名称，每行一个")
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_names:
        name = str(raw_name or "").strip()
        key = _identity(name)
        if key and key not in seen:
            names.append(name)
            seen.add(key)
    if not names:
        raise ValueError("请输入店铺名称，每行一个")

    window_mode = str(request.get("browser_window_mode") or "normal").strip().lower()
    if window_mode not in {"normal", "background"}:
        raise ValueError("浏览器窗口模式无效")
    try:
        socket_port = int(request.get("socket_port") or DEFAULT_SOCKET_PORT)
    except (TypeError, ValueError) as exc:
        raise ValueError("紫鸟端口必须为整数") from exc
    if not 1 <= socket_port <= 65535:
        raise ValueError("紫鸟端口必须在 1～65535 之间")

    output_root = str(
        request.get("output_root") or request.get("output_dir") or default_output_root
    ).strip()
    screenshot_root = str(
        request.get("screenshot_root")
        or request.get("screenshot_dir")
        or default_screenshot_root_value
        or ""
    ).strip() or default_screenshot_root()

    workbook_id = normalize_workbook_id(request.get("workbook_id") or DEFAULT_WORKBOOK_ID)
    return LazadaBillDetailQuery(
        company=str(request.get("company") or "").strip(),
        username=username,
        password=password,
        country=profile.code,
        country_name=profile.name,
        sheet_name=sheet_name,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        store_names=tuple(names),
        workbook_id=workbook_id,
        dws_node=_resolve_image_node(request.get("dws_node"), workbook_id),
        browser_window_mode=window_mode,
        client_path=str(request.get("client_path") or "").strip(),
        webdriver_path=str(request.get("webdriver_path") or "").strip(),
        output_root=output_root,
        screenshot_root=screenshot_root,
        socket_port=socket_port,
        **credentials,
    )


# ---------------------------------------------------------------------------
# 钉钉表格
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BillSheetRow:
    row_number: int
    shop_name: str
    total_amount: Any = ""
    revenue: Any = ""
    deductions: Any = ""

    @property
    def has_values(self) -> bool:
        return not any(
            is_pending_value(value)
            for value in (self.total_amount, self.revenue, self.deductions)
        )


def _sheet_last_row(meta: dict[str, Any]) -> int:
    for key in ("lastNonEmptyRow", "last_non_empty_row"):
        if meta.get(key) is not None:
            try:
                return int(meta[key]) + 1
            except (TypeError, ValueError):
                continue
    for key in ("rowCount", "row_count"):
        if meta.get(key) is not None:
            try:
                return int(meta[key])
            except (TypeError, ValueError):
                continue
    return 0


class DingTalkBillSheet:
    """钉钉在线表格适配器：A 列店铺名，B/C/D 指标，F 列截图。"""

    def __init__(self, query: LazadaBillDetailQuery):
        from backend.core import dingtalk_workbook as workbook

        self.query = query
        self.workbook = workbook
        self.access_token = ""
        self.operator_id = ""
        self.expires_at = 0.0
        self._refresh_token()
        meta = workbook.get_sheet_meta_by_name(
            self.access_token,
            self.operator_id,
            query.workbook_id,
            query.sheet_name,
            app_key=query.dingtalk_app_key,
            app_secret=query.dingtalk_app_secret,
        )
        if not meta or not meta.get("id"):
            raise WorkflowError(f"钉钉工作簿中没有指定 Sheet：{query.sheet_name}")
        self.sheet_id = str(meta["id"])
        self.last_non_empty_row = _sheet_last_row(meta)

    def _refresh_token(self) -> None:
        import httpx

        if self.access_token and time.time() < self.expires_at - 60:
            return
        response = httpx.get(
            "https://oapi.dingtalk.com/gettoken",
            params={
                "appkey": self.query.dingtalk_app_key,
                "appsecret": self.query.dingtalk_app_secret,
            },
            timeout=20,
        )
        if response.status_code != 200:
            raise WorkflowError(f"获取钉钉应用令牌失败（HTTP {response.status_code}）")
        data = response.json()
        if data.get("errcode") != 0 or not data.get("access_token"):
            raise WorkflowError("获取钉钉应用令牌失败，请检查设置中的应用凭证")
        self.access_token = str(data["access_token"])
        self.expires_at = time.time() + int(data.get("expires_in") or 7200)
        response = httpx.post(
            "https://oapi.dingtalk.com/topapi/v2/user/get",
            params={"access_token": self.access_token},
            json={"userid": self.query.dingtalk_user_id, "language": "zh_CN"},
            timeout=20,
        )
        if response.status_code != 200:
            raise WorkflowError(f"读取钉钉操作人失败（HTTP {response.status_code}）")
        data = response.json()
        self.operator_id = str((data.get("result") or {}).get("unionid") or "")
        if data.get("errcode") != 0 or not self.operator_id:
            raise WorkflowError("无法获取钉钉操作人 unionId，请检查设置中的操作人")

    def read(self, cell_range: str) -> list[list[Any]]:
        from backend.core.dingtalk_workbook import read_sheet_range

        self._refresh_token()
        return read_sheet_range(
            self.access_token,
            self.operator_id,
            self.query.workbook_id,
            self.sheet_id,
            cell_range,
            app_key=self.query.dingtalk_app_key,
            app_secret=self.query.dingtalk_app_secret,
        )

    def update(self, cell_range: str, values: list[list[str]]) -> None:
        from alibabacloud_dingtalk.doc_1_0 import models

        from backend.core.dingtalk_workbook import _get_sdk_client, _runtime_options

        self._refresh_token()
        client = _get_sdk_client(self.query.dingtalk_app_key, self.query.dingtalk_app_secret)
        client.update_range_with_options(
            self.query.workbook_id,
            self.sheet_id,
            cell_range,
            models.UpdateRangeRequest(
                operator_id=self.operator_id, values=values, number_format="General"
            ),
            models.UpdateRangeHeaders(x_acs_dingtalk_access_token=self.access_token),
            _runtime_options(),
        )


def list_workbook_sheets(
    workbook_id: str, *, app_key: str, app_secret: str, user_id: str
) -> list[dict[str, str]]:
    """读取钉钉工作簿的 Sheet 列表，供前端「指定 Sheet」下拉选择。"""
    from backend.core import dingtalk_workbook as workbook

    access_token = workbook.get_access_token(app_key, app_secret)
    union_id = workbook.get_union_id(access_token, user_id)
    sheets = workbook.get_all_sheets(
        access_token, union_id, workbook_id, app_key=app_key, app_secret=app_secret
    )
    result: list[dict[str, str]] = []
    for item in sheets or []:
        name = str((item or {}).get("name") or "").strip()
        if name:
            result.append({"name": name, "id": str((item or {}).get("id") or "")})
    return result


def read_bill_rows(sheet: Any) -> list[BillSheetRow]:
    last_row = int(getattr(sheet, "last_non_empty_row", 0) or 0)
    if last_row < 2:
        return []
    rows: list[BillSheetRow] = []
    for start in range(2, last_row + 1, 400):
        end = min(start + 399, last_row)
        for offset, raw_row in enumerate(sheet.read(f"{SHOP_COLUMN}{start}:D{end}") or []):
            cells = list(raw_row or [])[:4]
            cells.extend([""] * (4 - len(cells)))
            shop_name = str(cells[0] or "").strip()
            if not shop_name:
                continue
            rows.append(
                BillSheetRow(
                    row_number=start + offset,
                    shop_name=shop_name,
                    total_amount=cells[1],
                    revenue=cells[2],
                    deductions=cells[3],
                )
            )
    return rows


def _first_value(rows: Any) -> Any:
    return rows[0][0] if rows and rows[0] else None


def resolve_requested_stores(
    rows: Iterable[BillSheetRow], requested_names: Iterable[str]
) -> tuple[dict[str, BillSheetRow], dict[str, dict[str, Any]]]:
    """把用户输入的店铺名映射到钉钉行；歧义或无候选一律不写入。

    返回 (已匹配的 requested -> 行, 未匹配的 requested -> 说明)。
    """
    rows = list(rows)
    exact: dict[str, list[BillSheetRow]] = {}
    numbered: dict[tuple[str, ...], list[BillSheetRow]] = {}
    for row in rows:
        exact.setdefault(normalize_shop_name(row.shop_name), []).append(row)
        key = lazada_numbered_store_key(row.shop_name)
        if key is not None:
            numbered.setdefault(key, []).append(row)

    matched: dict[str, BillSheetRow] = {}
    issues: dict[str, dict[str, Any]] = {}
    used: dict[int, str] = {}
    for requested in requested_names:
        candidates = exact.get(normalize_shop_name(requested), [])
        if not candidates:
            key = lazada_numbered_store_key(requested)
            if key is not None and len(numbered.get(key, [])) == 1:
                candidates = numbered[key]
        if not candidates:
            issues[requested] = {
                "requested_store_name": requested,
                "store_name": requested,
                "status": "unmatched_store",
                "message": "钉钉指定 Sheet 的 A 列未匹配到该店铺",
            }
            continue
        if len(candidates) > 1:
            issues[requested] = {
                "requested_store_name": requested,
                "store_name": requested,
                "status": "unmatched_store",
                "message": "钉钉 A 列存在多个同名店铺，无法确定写入行",
            }
            continue
        row = candidates[0]
        if row.row_number in used:
            issues[requested] = {
                "requested_store_name": requested,
                "store_name": requested,
                "row": row.row_number,
                "status": "unmatched_store",
                "message": f"第 {row.row_number} 行已被「{used[row.row_number]}」使用，未复用行号",
            }
            continue
        used[row.row_number] = requested
        matched[requested] = row
    return matched, issues


def match_browsers_to_rows(
    rows: list[BillSheetRow], browser_list: list[dict[str, Any]]
) -> tuple[list[tuple[BillSheetRow, dict[str, Any], str]], list[BillSheetRow]]:
    """沿用原脚本的浏览器匹配顺序，任何歧义都拒绝自动执行。"""
    matches: list[tuple[BillSheetRow, dict[str, Any], str]] = []
    unmatched: list[BillSheetRow] = []
    used: set[str] = set()

    exact_names: dict[str, dict[str, dict[str, Any]]] = {}
    oauth_names: dict[str, dict[str, dict[str, Any]]] = {}
    bodies: dict[str, dict[str, dict[str, Any]]] = {}
    numbered_aliases: dict[Any, dict[str, dict[str, Any]]] = {}
    lazada_names: dict[str, tuple[str, dict[str, Any]]] = {}
    for browser in browser_list or []:
        identity = str(browser.get("browserOauth") or browser.get("browser_oauth") or "")
        if not identity:
            continue
        browser_name = browser.get("browserName") or browser.get("name")
        browser_name_key = normalize_shop_name(browser_name)
        if browser_name_key:
            exact_names.setdefault(browser_name_key, {})[identity] = browser
        oauth_key = normalize_shop_name(identity)
        if oauth_key:
            oauth_names.setdefault(oauth_key, {})[identity] = browser
        body = lazada_store_key(browser_name)
        if body:
            bodies.setdefault(body, {})[identity] = browser
            lazada_names[identity] = (body, browser)
        numbered_key = lazada_numbered_store_key(browser_name)
        if numbered_key is not None:
            numbered_aliases.setdefault(numbered_key, {})[identity] = browser

    for row in rows:
        shop_key = normalize_shop_name(row.shop_name)
        row_body = lazada_store_key(row.shop_name)
        candidates = exact_names.get(shop_key, {})
        label = "店铺名精确匹配"
        if not candidates:
            candidates = bodies.get(row_body, {})
            label = "唯一主体匹配" if candidates else label
        if not candidates:
            candidates = numbered_aliases.get(lazada_numbered_store_key(row.shop_name), {})
            label = "唯一编号及中文尾缀匹配" if candidates else label
        if not candidates:
            # Sheet 名称可能省略后续的账号/品类后缀，仅唯一标准 LZ 主体可命中。
            candidates = {
                identity: browser
                for identity, (body, browser) in lazada_names.items()
                if _lazada_body_is_extension(row_body, body)
            }
            label = "唯一LZ名称扩展匹配" if candidates else label
        if not candidates:
            candidates = oauth_names.get(shop_key, {})
            label = "唯一浏览器身份匹配" if candidates else label
        if len(candidates) != 1:
            unmatched.append(row)
            continue
        identity, browser = next(iter(candidates.items()))
        if identity in used:
            unmatched.append(row)
            continue
        used.add(identity)
        matches.append((row, browser, label))
    return matches, unmatched


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def _metric_value(value: Any, label: str) -> Decimal:
    if value is None or value == "":
        raise WorkflowError(f"{label}未取得有效数据")
    try:
        decimal_value = parse_amount(value)
    except WorkflowError:
        raise WorkflowError(f"{label}不是有效金额：{value!r}") from None
    if not decimal_value.is_finite():
        raise WorkflowError(f"{label}不是有效数字")
    return decimal_value


def write_bill_metrics(
    sheet: Any, row: BillSheetRow, metrics: dict[str, Any], *, overwrite: bool = False
) -> dict[str, Any]:
    """一次写入 B:D 三项并回读校验；写入本身绝不重试。"""
    values = [
        _metric_value(metrics.get("total_amount"), "总金额"),
        _metric_value(metrics.get("revenue"), "收入"),
        _metric_value(metrics.get("deductions"), "扣减项"),
    ]
    cell_range = f"{METRIC_COLUMNS[0]}{row.row_number}:{METRIC_COLUMNS[-1]}{row.row_number}"
    with _WRITE_LOCK:
        current_name = _first_value(sheet.read(f"{SHOP_COLUMN}{row.row_number}:{SHOP_COLUMN}{row.row_number}"))
        if normalize_shop_name(current_name) != normalize_shop_name(row.shop_name):
            raise WorkflowError("钉钉店铺行已发生变化，已停止写入")
        existing = sheet.read(cell_range) or []
        existing_values = list(existing[0]) if existing else []
        if not overwrite and all(not is_pending_value(value) for value in existing_values[:3]):
            return {"written": False, "reason": "existing", "existing": existing_values}
        rendered = [[str(int(value)) if value == value.to_integral_value() else format(value.normalize(), "f") for value in values]]
        sheet.update(cell_range, rendered)
        for attempt in range(3):
            actual = sheet.read(cell_range) or []
            actual_values = list(actual[0]) if actual else []
            if len(actual_values) >= 3 and all(
                values_equal(actual_values[index], values[index]) for index in range(3)
            ):
                return {"written": True, "existing": actual_values, "cell_range": cell_range}
            if attempt < 2:
                time.sleep(0.5)
        # 数据已经提交过，只是回读未确认：抛可识别的异常，禁止外层重复采集/写入。
        raise SheetWriteUncertain(f"{cell_range} 写入后回读不一致，请核对表格")


# ---------------------------------------------------------------------------
# F 列截图同步（原脚本 dws 逻辑，本机未安装 dws 时明确降级为仅本地保存）
# ---------------------------------------------------------------------------

def _parse_dws_json(stdout: str) -> Any:
    text = str(stdout or "").lstrip("\ufeff\r\n ")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"[\[{]", text):
            try:
                value, _ = decoder.raw_decode(text[match.start():])
                return value
            except json.JSONDecodeError:
                continue
    raise WorkflowError("dws 未返回有效 JSON")


def dws_path() -> str:
    """本机 dws 可执行文件路径；找不到返回空串（F 列图片会降级为仅本地截图）。"""
    return str(shutil.which(DWS_COMMAND) or "")


def dws_available() -> bool:
    return bool(dws_path())


def _run_dws_json(arguments: list[str], *, write: bool = False) -> dict[str, Any]:
    executable = dws_path()
    if not executable:
        raise WorkflowError("未找到 dws 命令，无法同步钉钉 F 列图片")
    command = [executable, *arguments, "--format", "json", "--timeout", str(DWS_TIMEOUT_SECONDS)]
    if write:
        command.append("--yes")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DWS_TIMEOUT_SECONDS + 15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError("钉钉图片命令超时，远端结果未知，未自动重试") from exc
    except OSError as exc:
        raise WorkflowError(f"无法启动 dws：{type(exc).__name__}") from exc
    if completed.returncode != 0:
        detail = str(completed.stderr or completed.stdout or "").strip()[-300:]
        raise WorkflowError(f"dws 命令失败：{detail or 'unknown error'}")
    payload = _parse_dws_json(completed.stdout)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        detail = str(payload.get("message") if isinstance(payload, dict) else "").strip()[-300:]
        raise WorkflowError(f"dws 命令未成功：{detail or 'unknown error'}")
    return payload


def _dws_sheet_id(node: str, sheet_name: str) -> str:
    key = (str(node), str(sheet_name))
    with _DWS_SHEET_ID_LOCK:
        if key in _DWS_SHEET_IDS:
            return _DWS_SHEET_IDS[key]
        payload = _run_dws_json(["sheet", "list", "--node", str(node)])
        sheets = payload.get("sheets")
        if not isinstance(sheets, list):
            raise WorkflowError("dws 工作表列表结构无效")
        matches = [
            item
            for item in sheets
            if isinstance(item, dict) and item.get("name") == sheet_name and item.get("sheetId")
        ]
        if len(matches) != 1:
            raise WorkflowError(f"钉钉工作表 {sheet_name!r} 匹配数量为 {len(matches)}，拒绝写图")
        _DWS_SHEET_IDS[key] = str(matches[0]["sheetId"])
        return _DWS_SHEET_IDS[key]


def _dws_read_row(node: str, sheet_name: str, row_number: int) -> dict[str, Any]:
    sheet_id = _dws_sheet_id(node, sheet_name)
    payload = _run_dws_json(
        [
            "sheet", "range", "read",
            "--node", str(node),
            "--sheet-id", sheet_id,
            "--range", f"A{row_number}:F{row_number}",
            "--value-render-option", "raw_value",
        ]
    )
    cells = payload.get("cells")
    rows = payload.get("rowIndices")
    columns = payload.get("colIndices")
    if not all(isinstance(value, list) for value in (cells, rows, columns)):
        raise WorkflowError("dws 单行读取缺少坐标")
    if rows != [row_number] or len(cells) != 1 or len(cells[0]) != len(columns):
        raise WorkflowError("dws 单行读取坐标异常")
    mapped = {column: cell for column, cell in zip(columns, cells[0])}
    return {column: mapped.get(column, {"value": ""}) for column in "ABCDEF"}


def _dws_cell_text(cell: Any) -> str:
    if not isinstance(cell, dict):
        return "" if cell is None else str(cell).strip()
    value = cell.get("value", "")
    if isinstance(value, dict):
        value = value.get("text") if value.get("text") is not None else value.get("value", "")
    return "" if value is None else str(value).strip()


def _recursive_resource_ids(value: Any) -> set[str]:
    resource_ids: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() == "resourceid" and child is not None:
                resource_id = str(child).strip()
                if resource_id:
                    resource_ids.add(resource_id)
            else:
                resource_ids.update(_recursive_resource_ids(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            resource_ids.update(_recursive_resource_ids(child))
    return resource_ids


def _dws_cell_image_resource_ids(cell: Any) -> set[str]:
    if not isinstance(cell, dict):
        return set()
    rich_text = cell.get("richText")
    if not isinstance(rich_text, dict) or not isinstance(rich_text.get("texts"), list):
        return set()
    return {
        str(item.get("resourceId")).strip()
        for item in rich_text["texts"]
        if isinstance(item, dict)
        and str(item.get("type") or "").casefold() == "image"
        and str(item.get("resourceId") or "").strip()
    }


def _dws_cell_has_image(cell: Any) -> bool:
    return bool(_dws_cell_image_resource_ids(cell))


def _dws_cell_is_occupied(cell: Any) -> bool:
    if cell is None:
        return False
    if not isinstance(cell, dict):
        return bool(str(cell).strip())
    if _dws_cell_has_image(cell) or _recursive_resource_ids(cell):
        return True
    if _dws_cell_text(cell):
        return True
    rich_text = cell.get("richText")
    if isinstance(rich_text, dict):
        texts = rich_text.get("texts")
        if isinstance(texts, list) and texts:
            return True
        if texts not in (None, "", []):
            return True
    return any(
        cell.get(key) is not None and str(cell.get(key)).strip()
        for key in ("formula", "note", "hyperlink")
    )


def _uploader_available(uploader: Any) -> bool:
    """注入的图片上传器可以不带 available()，此时按可用处理。"""
    checker = getattr(uploader, "available", None)
    if not callable(checker):
        return True
    try:
        return bool(checker())
    except Exception:
        return False


class DwsImageUploader:
    """把本地截图写入指定文档的截图列；沿用原脚本的前置校验与回读确认。

    截图列固定为 F 列（可显式指定 F 之后的列），A～E 列属于店铺名与账单数据，永不写图。
    """

    header = IMAGE_HEADER

    def __init__(self, node: str, sheet_name: str, column: str = IMAGE_COLUMN):
        parsed = urlsplit(str(node or ""))
        if not ((parsed.scheme in {"http", "https"} and parsed.netloc) or re.fullmatch(r"[A-Za-z0-9]{32}", str(node or ""))):
            raise ValueError("钉钉图片节点格式无效：请填写完整表格 URL 或 32 位 dentryUuid")
        self.node = str(node)
        self.sheet_name = str(sheet_name)
        self.column = validate_image_column(column)
        self._header_verified = False

    @staticmethod
    def available() -> bool:
        return dws_available()

    def _verify_header(self) -> None:
        if self._header_verified:
            return
        cells = _dws_read_row(self.node, self.sheet_name, 1)
        actual = _dws_cell_text(cells[self.column])
        if actual != self.header:
            raise WorkflowError(
                f"{self.sheet_name}!{self.column}1 表头应为 {self.header!r}，实际为 {actual!r}，拒绝写图"
            )
        self._header_verified = True

    def inspect(self, row_number: int, shop_name: str) -> dict[str, Any]:
        self._verify_header()
        cells = _dws_read_row(self.node, self.sheet_name, row_number)
        if normalize_shop_name(_dws_cell_text(cells[SHOP_COLUMN])) != normalize_shop_name(shop_name):
            raise WorkflowError(f"{SHOP_COLUMN}{row_number} 店铺名已变化，拒绝按旧行号写图")
        if any(is_pending_value(_dws_cell_text(cells[column])) for column in METRIC_COLUMNS):
            raise WorkflowError(f"{shop_name} 的 B:D 尚未完整写入，拒绝写截图")
        target = cells[self.column]
        if _dws_cell_has_image(target):
            return {"status": "skipped_existing", "cell": f"{self.column}{row_number}"}
        if _dws_cell_is_occupied(target):
            return {"status": "occupied", "cell": f"{self.column}{row_number}"}
        return {"status": "empty", "cell": f"{self.column}{row_number}"}

    def upload(self, row_number: int, shop_name: str, image_path: Path) -> dict[str, Any]:
        before = self.inspect(row_number, shop_name)
        if before["status"] == "skipped_existing":
            return before
        if before["status"] == "occupied":
            raise WorkflowError(f"{before['cell']} 已有非图片内容，未覆盖")
        sheet_id = _dws_sheet_id(self.node, self.sheet_name)
        cell = before["cell"]
        response = _run_dws_json(
            [
                "sheet", "write-image",
                "--node", self.node,
                "--sheet-id", sheet_id,
                "--range", f"{cell}:{cell}",
                "--file", str(image_path),
                "--name", f"{safe_store_filename(shop_name)}.jpg",
                "--mime-type", "image/jpeg",
                "--width", str(IMAGE_CELL_WIDTH),
                "--height", str(IMAGE_CELL_HEIGHT),
            ],
            write=True,
        )
        response_resource_ids = _recursive_resource_ids(response)
        for attempt in range(1, DWS_VERIFY_ATTEMPTS + 1):
            after = _dws_read_row(self.node, self.sheet_name, row_number)
            if normalize_shop_name(_dws_cell_text(after[SHOP_COLUMN])) != normalize_shop_name(shop_name):
                raise WorkflowError(f"{cell} 对应店铺在写图后发生变化，请人工核对")
            cell_resource_ids = _dws_cell_image_resource_ids(after[self.column])
            if _dws_cell_has_image(after[self.column]) and (
                not response_resource_ids or bool(cell_resource_ids.intersection(response_resource_ids))
            ):
                return {
                    "status": "uploaded",
                    "cell": cell,
                    "resource_ids": sorted(cell_resource_ids),
                }
            if attempt < DWS_VERIFY_ATTEMPTS:
                time.sleep(1)
        raise WorkflowError(f"{cell} 图片命令已返回，但回读未确认对应 resourceId")


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------

class LazadaBillBrowserRuntime(ZiniaoPlaywrightRuntime):
    def close_browser(self, opened: Any) -> None:
        errors = []
        if opened.connection is not None:
            try:
                opened.connection.close()
            except Exception:
                errors.append("关闭 Playwright 连接失败")
        if self.client is not None and opened.browser_oauth:
            try:
                self.client.close_browser(opened.browser_oauth)
            except Exception:
                errors.append("关闭紫鸟店铺失败")
        if errors:
            raise WorkflowError("；".join(errors))


def _redact(value: Any, query: LazadaBillDetailQuery) -> str:
    text = str(value)
    for secret in (
        query.password,
        query.dingtalk_app_secret,
        query.dingtalk_app_key,
        query.dingtalk_user_id,
    ):
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return re.sub(
        r"(?i)((?:access[_-]?token|x-acs-dingtalk-access-token|appsecret)[\"']?\s*[=:]\s*[\"']?)[^\s&\"'<>]+",
        r"\1[已隐藏]",
        text,
    )


def _store_result(query: LazadaBillDetailQuery, requested: str) -> dict[str, Any]:
    return {
        "requested_store_name": requested,
        "store_name": requested,
        "country": query.country,
        "country_name": query.country_name,
        "sheet_name": query.sheet_name,
        "start_date": query.start_date,
        "end_date": query.end_date,
        "row": None,
        "status": "failed",
        "message": "",
        "total_amount": "",
        "revenue": "",
        "deductions": "",
        "written_range": "",
        "screenshot": "",
        "image_status": "",
        "image_cell": "",
    }


def run_lazada_bill_detail(
    query: LazadaBillDetailQuery,
    progress: Callable[[str], None] | None = None,
    *,
    runtime_factory: Callable[..., Any] | None = None,
    sheet_factory: Callable[..., Any] | None = None,
    page_actions: Any = None,
    image_uploader: Any = None,
) -> dict[str, Any]:
    root = Path(query.output_root or Path.cwd() / "outputs")
    run_dir = root / "lazada_bill_detail" / f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_file = run_dir / "run.log"
    result_file = run_dir / "result.json"
    logger = logging.getLogger(f"lazada_bill_detail.{uuid.uuid4().hex}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    class ProgressHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            message = _redact(record.getMessage(), query)
            with log_file.open("a", encoding="utf-8") as stream:
                stream.write(f"{datetime.now():%H:%M:%S} {message}\n")
            if progress:
                progress(message)

    handler = ProgressHandler()
    logger.addHandler(handler)
    results: list[dict[str, Any]] = []
    runtime = None
    uploader = image_uploader
    image_unavailable_reason = ""
    matched_count = 0
    try:
        logger.info(
            "开始采集 Lazada 后台收支数据：国家=%s，Sheet=%s，区间=%s 至 %s，店铺=%s",
            query.country_name, query.sheet_name, query.start_date, query.end_date,
            len(query.store_names),
        )
        logger.info("连接钉钉工作簿：%s（指定 Sheet：%s）", query.workbook_id, query.sheet_name)
        sheet = (sheet_factory or DingTalkBillSheet)(query)
        rows = read_bill_rows(sheet)
        logger.info("指定 Sheet 共读取 %s 个店铺行（A 列）", len(rows))
        resolved, issues = resolve_requested_stores(rows, query.store_names)

        pending: list[tuple[dict[str, Any], BillSheetRow]] = []
        for requested in query.store_names:
            issue = issues.get(requested)
            if issue is not None:
                result = _store_result(query, requested)
                result.update(issue)
                results.append(result)
                logger.warning("%s：%s", requested, result["message"])
                continue
            row = resolved[requested]
            result = _store_result(query, requested)
            result.update(
                row=row.row_number,
                store_name=row.shop_name,
                total_amount=row.total_amount if row.total_amount is not None else "",
                revenue=row.revenue if row.revenue is not None else "",
                deductions=row.deductions if row.deductions is not None else "",
            )
            results.append(result)
            if row.has_values:
                result.update(status="skipped", message="B:D 已有数据，已跳过（未覆盖）")
                logger.info("%s：%s", row.shop_name, result["message"])
            else:
                pending.append((result, row))

        if pending:
            uploader = uploader if uploader is not None else (
                DwsImageUploader(query.dws_node, query.sheet_name) if query.dws_node else None
            )
            if uploader is not None and not _uploader_available(uploader):
                image_unavailable_reason = DWS_MISSING_HINT
                logger.warning(image_unavailable_reason)
            elif uploader is None:
                image_unavailable_reason = "未配置钉钉图片节点，截图只保存到「截图保存位置」"

            runtime = (runtime_factory or LazadaBillBrowserRuntime)(query, logger)
            runtime.start()
            browser_matches, unmatched_rows = match_browsers_to_rows(
                [row for _result, row in pending], runtime.list_browsers()
            )
            browser_by_row: dict[int, dict[str, Any]] = {}
            for row, browser, label in browser_matches:
                browser_by_row[row.row_number] = browser
                logger.info("%s：%s -> %s", row.shop_name, label, browser.get("browserName") or "")
            unmatched_numbers = {row.row_number for row in unmatched_rows}

            from backend.services.lazada_bill_page import (
                PlaywrightLazadaBillPageActions,
                browser_connection_failed,
                is_auth_page,
                login_status,
                safe_page_url,
            )

            actions = page_actions
            if actions is None:
                actions = PlaywrightLazadaBillPageActions(logger)
            profile = query.profile
            shot_folder = screenshot_folder(
                query.screenshot_root, query.country_name, query.start_date, query.end_date
            )

            for result, row in pending:
                browser = browser_by_row.get(row.row_number)
                if browser is None:
                    result["status"] = "unmatched_store"
                    result["message"] = (
                        "紫鸟存在多个同名店铺，已停止采集"
                        if row.row_number in unmatched_numbers
                        else "紫鸟未匹配到该店铺"
                    )
                    logger.warning("%s：%s", row.shop_name, result["message"])
                    continue
                matched_count += 1
                opened = None
                written = False
                blocked = ""
                message_logged = False
                try:
                    logger.info(
                        "%s：打开店铺，采集 %s 至 %s 的账单", row.shop_name, query.start_date, query.end_date
                    )
                    opened = runtime.open_browser(
                        browser, run_dir / "_browser_downloads" / safe_store_filename(row.shop_name)
                    )
                    page = opened.page
                    # 与原脚本一致：一家店最多 3 次采集机会，每次重试都是一条全新的登录流程
                    # （独立 login_state），不会把登录机会耗在第一次失败上。
                    for attempt in range(1, STORE_ATTEMPTS + 1):
                        login_state: dict[str, Any] = {}
                        if attempt > 1:
                            logger.info(
                                "%s：第 %s/%s 次重试采集", row.shop_name, attempt, STORE_ATTEMPTS
                            )
                            page.wait_for_timeout(STORE_RETRY_BACKOFF_MS)
                        try:
                            metrics = actions.collect(
                                page, row.shop_name, profile, query.start, query.end, login_state
                            )
                            result.update(
                                total_amount=str(metrics["total_amount"]),
                                revenue=str(metrics["revenue"]),
                                deductions=str(metrics["deductions"]),
                            )
                            outcome = write_bill_metrics(sheet, row, metrics)
                            # 写入步骤一旦执行过就不再重采：避免覆盖或重复提交钉钉。
                            written = True
                            if outcome["written"]:
                                result.update(written_range=outcome["cell_range"])
                                logger.info(
                                    "%s：%s 写入并回读验证通过", row.shop_name, outcome["cell_range"]
                                )
                            else:
                                logger.info("%s：B:D 已有数据，保留现有值", row.shop_name)
                            result["status"] = "success"
                            result["message"] = (
                                "账单已写入指定 Sheet" if outcome["written"] else "B:D 已有数据，未覆盖"
                            )
                            break
                        except Exception as exc:
                            message = _redact(exc, query)
                            status = login_status(exc)
                            if written or isinstance(exc, SheetWriteUncertain):
                                # 钉钉这一行已经写下去了（或可能已写），绝不重复采集/写入。
                                written = True
                                result.update(
                                    status="write_failed",
                                    message=f"B:D 已提交写入但未确认，未重复采集：{message}",
                                )
                                logger.warning("%s：%s", row.shop_name, result["message"])
                                message_logged = True
                                break
                            if status:
                                blocked = status
                                result.update(status=status, message=message)
                                logger.warning("%s：%s；不再店内重试", row.shop_name, message)
                                message_logged = True
                                break
                            if browser_connection_failed(exc):
                                result.update(status="browser_failed", message=message)
                                logger.warning(
                                    "%s：浏览器会话不可用，停止店内重试：%s", row.shop_name, message
                                )
                                message_logged = True
                                break
                            if attempt >= STORE_ATTEMPTS:
                                result.update(status="failed", message=message)
                                logger.error(
                                    "%s：第 %s/%s 次采集失败：%s",
                                    row.shop_name, attempt, STORE_ATTEMPTS, message,
                                )
                                message_logged = True
                            else:
                                logger.warning(
                                    "%s：第 %s/%s 次采集失败，稍后重试：%s",
                                    row.shop_name, attempt, STORE_ATTEMPTS, message,
                                )
                except Exception as exc:
                    result.update(status="failed", message=_redact(exc, query))
                finally:
                    if opened is not None:
                        # 登录常另开标签页：必须用真正采集到数据的那一页截图，
                        # 否则会把还停在注册页/登录页的旧标签存成账单证据。
                        captured_page = getattr(actions, "last_page", None) or opened.page
                        try:
                            if result["status"] == "success":
                                logger.info(
                                    "%s：截图取用页面：%s",
                                    row.shop_name,
                                    safe_page_url(getattr(captured_page, "url", "")),
                                )
                                _capture_and_sync(
                                    result, captured_page, row, shot_folder, uploader,
                                    image_unavailable_reason, logger, query,
                                )
                            elif blocked:
                                # 登录/验证类失败：认证页可能含预填账号或已展开的密码，只记录状态。
                                logger.info(
                                    "%s：登录/验证类失败（%s），不保存认证页截图", row.shop_name, blocked
                                )
                            else:
                                try:
                                    auth_page = bool(is_auth_page(captured_page.url))
                                except Exception:
                                    auth_page = True
                                if auth_page:
                                    logger.info(
                                        "%s：当前停在认证页，不保存可能含凭据的截图", row.shop_name
                                    )
                                else:
                                    _save_failure_screenshot(
                                        captured_page, shot_folder, row.shop_name, logger, query
                                    )
                        except Exception as exc:
                            result["message"] = f"{result['message']}；{_redact(exc, query)}".strip("；")
                            logger.warning("%s：截图处理失败：%s", row.shop_name, result["message"])
                            message_logged = True
                        try:
                            runtime.close_browser(opened)
                        except Exception as exc:
                            logger.warning("%s：关闭店铺失败：%s", row.shop_name, _redact(exc, query))
                    if not message_logged:
                        logger.info("%s：%s", row.shop_name, result["message"] or result["status"])
    except Exception as exc:
        message = _redact(exc, query)
        if "invalidRequest.resource.notFound" in message:
            message = (
                f"钉钉未找到请求的资源（工作簿 ID={query.workbook_id}，Sheet={query.sheet_name}），"
                f"请核对链接和操作人访问权限。原始错误：{message}"
            )
        logger.error("任务失败：%s", message)
        if not results:
            for requested in query.store_names:
                failed = _store_result(query, requested)
                failed.update(status="failed", message=message)
                results.append(failed)
        else:
            for item in results:
                if item["status"] in {"pending", "starting"} or not item["message"]:
                    item.update(status="failed", message=message)
    finally:
        if runtime is not None:
            try:
                runtime.shutdown()
            except Exception as exc:
                logger.warning("清理紫鸟运行时失败：%s", _redact(exc, query))

    success_count = sum(item["status"] == "success" for item in results)
    skipped_count = sum(item["status"] == "skipped" for item in results)
    failed_count = len(results) - success_count - skipped_count
    login_blocked_count = sum(
        item["status"] in {"login_required", "verification_required"} for item in results
    )
    browser_failed_count = sum(item["status"] == "browser_failed" for item in results)
    image_uploaded = sum(item["image_status"] == "uploaded" for item in results)
    image_failed = sum(item["image_status"] == "failed" for item in results)
    image_local_only = sum(item["image_status"] == "local_only" for item in results)
    breakdown_parts = []
    if login_blocked_count:
        breakdown_parts.append(f"需人工登录/验证 {login_blocked_count}")
    if browser_failed_count:
        breakdown_parts.append(f"浏览器会话不可用 {browser_failed_count}")
    breakdown = f"（{'；'.join(breakdown_parts)}）" if breakdown_parts else ""
    message = (
        f"处理完成：成功 {success_count}，已有数据跳过 {skipped_count}，失败 {failed_count}{breakdown}"
        f"；F 列图片：上传 {image_uploaded}，失败 {image_failed}，仅本地 {image_local_only}"
    )
    payload = {
        "success": failed_count == 0,
        "is_complete": failed_count == 0,
        "message": message,
        "completion_message": message,
        "country": query.country,
        "country_name": query.country_name,
        "workbook_id": query.workbook_id,
        "sheet_name": query.sheet_name,
        "start_date": query.start_date,
        "end_date": query.end_date,
        "screenshot_root": str(query.screenshot_root),
        "screenshot_dir": str(
            screenshot_folder(
                query.screenshot_root, query.country_name, query.start_date, query.end_date
            )
        ),
        "image_sync_available": bool(uploader is not None and uploader.available()),
        "image_sync_note": image_unavailable_reason,
        "input_store_count": len(query.store_names),
        "matched_store_count": matched_count,
        "success_store_count": success_count,
        "skipped_store_count": skipped_count,
        "failed_store_count": failed_count,
        "image_uploaded_count": image_uploaded,
        "image_failed_count": image_failed,
        "image_local_only_count": image_local_only,
        "output_dir": str(run_dir),
        "output_file": str(result_file),
        "log_file": str(log_file),
        "stores": results,
        "results": results,
    }
    try:
        temporary = result_file.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        os.replace(temporary, result_file)
        logger.info(message)
        return payload
    finally:
        logger.removeHandler(handler)
        handler.close()


def _capture_and_sync(
    result: dict[str, Any],
    page: Any,
    row: BillSheetRow,
    screenshot_folder: Path,
    uploader: Any,
    unavailable_reason: str,
    logger: logging.Logger,
    query: LazadaBillDetailQuery,
) -> None:
    from backend.services.lazada_bill_page import save_screenshot

    screenshot_path = save_screenshot(page, screenshot_folder, row.shop_name)
    result["screenshot"] = str(screenshot_path)
    if uploader is None:
        result["image_status"] = "local_only"
        return
    if not _uploader_available(uploader):
        result["image_status"] = "local_only"
        result["message"] = f"{result['message']}；{unavailable_reason}"
        return
    try:
        outcome = uploader.upload(row.row_number, row.shop_name, screenshot_path)
        status = str(outcome.get("status") or "")
        result["image_cell"] = str(outcome.get("cell") or "")
        result["image_status"] = "uploaded" if status == "uploaded" else "skipped_existing"
        logger.info("%s：截图已写入 %s（%s）", row.shop_name, result["image_cell"] or "-", status)
    except Exception as exc:
        result["image_status"] = "failed"
        result["message"] = f"{result['message']}；F 列截图写入失败：{_redact(exc, query)}"


def _save_failure_screenshot(
    page: Any,
    screenshot_folder: Path,
    shop_name: str,
    logger: logging.Logger,
    query: LazadaBillDetailQuery,
) -> None:
    from backend.services.lazada_bill_page import save_screenshot

    try:
        save_screenshot(page, screenshot_folder, shop_name, failed=True)
    except Exception as exc:
        logger.warning("%s：保存失败截图时发生异常：%s", shop_name, _redact(exc, query))
