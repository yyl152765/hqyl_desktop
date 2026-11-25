"""
Lazada 跨境店铺余额提现统计 - 服务层
职责: 调度、配置管理、钉钉表格读写、店铺匹配、输出文件、日志
"""

from __future__ import annotations

import copy
import csv
import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import traceback
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Callable

import yaml


def _bootstrap_local_site_packages() -> None:
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    candidate = os.path.join(repo_root, ".venv", "Lib", "site-packages")
    if os.path.isdir(candidate) and candidate not in sys.path:
        sys.path.insert(0, candidate)


_bootstrap_local_site_packages()

from implement.lazada import lazada_balance_withdrawal as withdrawal_module
from main.super_browser_desktop import LoadSuperBrowser
from util import yingdao_util
from util.dingtalk_doc_api import (
    get_access_token,
    get_union_id,
    get_sheet_content,
    safe_read_range,
    safe_update_range,
    APP_KEY,
    APP_SECRET,
    USER_ID,
)
from util.log import setup_logger
from util.hqyl_runtime import HqylRuntimeError, is_hqyl_runtime
from util.ziniao_runtime_util import get_runtime_root


PROCESS_NAME = "Lazada跨境余额提现统计"
CHANNEL = "Lazada"
SETTINGS_APP_DIR = "LazadaCrossBorderBalanceWithdrawal"
DEFAULT_MAX_CONCURRENT_STORES = 3
COMPLETE_COLLECTION_REMARK_PREFIX = "[完整采集:"
CONFIRMED_NO_WITHDRAWAL_REMARK = withdrawal_module.CONFIRMED_NO_WITHDRAWAL_REMARK

# 钉钉表格配置
DINGTALK_DOC_NODE = "https://alidocs.dingtalk.com/i/nodes/Y1OQX0akWm33a9rwSvLDb6nkJGlDd3mE?utm_scene=person_space"
DINGTALK_DOC_ID = "Y1OQX0akWm33a9rwSvLDb6nkJGlDd3mE"
DINGTALK_SHEET_NAME_DOC = "LZ跨境店铺余额提现统计"
DINGTALK_THAILAND_DRIVE_SPACE_ID = "28932857521"
DINGTALK_THAILAND_DRIVE_ROOT_FOLDER_ID = "225456777657"
SHEET_CONFIG = {
    "PH": {"name": "菲律宾", "sheet_id": "st-0b5831c5-46051"},
    "MY": {"name": "马来", "sheet_id": "st-549fbaeb-79425"},
    "TH": {"name": "泰国", "sheet_id": "st-de97ffc6-42521"},
    "VN": {"name": "越南", "sheet_id": "st-ed8d8d0e-40101"},
}

# 国家关键词
COUNTRY_KEYWORDS = {
    "PH": ["菲律宾", "philippines", "菲律宾仓", "LZ跨境菲律宾"],
    "MY": ["马来", "malaysia", "马来仓", "LZ跨境马来"],
    "TH": ["泰国", "thailand", "thai", "(th)", "泰国仓", "LZ跨境泰国"],
    "VN": ["越南", "vietnam", "LZ跨境越南"],
}

DEFAULT_GLOBAL_CONFIG = {
    "ziniao": {
        "browser": {
            "version": "v6",
            "client_path": "",
            "webdriver_path": "",
            "socket_port": 16851,
        },
        "script": {
            "output_path": "",
            "project_path": "",
            "super_browser_download_path": "",
        },
    },
    "dingtalk": {
        "agent": {
            "access_token": "",
            "secret": "",
        },
        "drive": {
            "thailand_income_statement": {
                "space_id": DINGTALK_THAILAND_DRIVE_SPACE_ID,
                "root_folder_id": DINGTALK_THAILAND_DRIVE_ROOT_FOLDER_ID,
            },
        },
    },
}

DEFAULT_PROCESS_CONFIG = {
    "ziniao": {
        "user_info": {
            "company": "",
            "username": "",
            "password": "",
        },
    },
    "dingtalk": {
        "agent": {
            "access_token": "",
            "secret": "",
        },
    },
    "task": ["lazada_balance_withdrawal"],
    "thread_num": DEFAULT_MAX_CONCURRENT_STORES,
    "operator": {
        "country_mode": "auto",
        "start_date": "",
        "end_date": "",
        "store_names": [],
        "browser_window_mode": "normal",
        "max_concurrent_stores": DEFAULT_MAX_CONCURRENT_STORES,
        "output_root": "",
        "collect_unmatched_stores": False,
    },
}


# ========== 数据模型 ==========


@dataclass
class LazadaWithdrawalSettings:
    company: str = ""
    username: str = ""
    password: str = ""
    country_mode: str = "auto"
    start_date: str = ""
    end_date: str = ""
    store_names: list[str] = field(default_factory=list)
    client_path: str = ""
    webdriver_path: str = ""
    browser_window_mode: str = "normal"
    max_concurrent_stores: int = DEFAULT_MAX_CONCURRENT_STORES
    output_root: str = ""
    collect_unmatched_stores: bool = False


@dataclass
class LazadaStoreTask:
    input_name: str = ""
    country: str = ""
    sheet_name: str = ""
    sheet_id: str = ""
    row_number: int | None = None


@dataclass
class LazadaWithdrawalResult:
    success: bool = False
    message: str = ""
    summary: str = ""
    record_file: str = ""
    detail_file: str = ""
    execution_file: str = ""
    stats_file: str = ""
    log_file: str = ""
    log_directory: str = ""
    matched_store_count: int = 0
    processed_store_count: int = 0
    failed_count: int = 0


# ========== 日志桥接 ==========


class _LogCallbackHandler(logging.Handler):
    def __init__(self, callback: Callable[[str], None]):
        super().__init__(level=logging.INFO)
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._callback(self.format(record))
        except Exception:
            pass


class _CallbackStream(io.TextIOBase):
    def __init__(self, callback: Callable[[str], None]):
        super().__init__()
        self._callback = callback
        self._buffer = ""

    def write(self, s: str) -> int:
        text = str(s or "")
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            message = line.rstrip()
            if message:
                self._callback(message)
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self._callback(self._buffer.rstrip())
        self._buffer = ""


# ========== 运行时文件 ==========


def get_runtime_files() -> dict:
    if is_hqyl_runtime():
        raise HqylRuntimeError("Lazada 余额提现流程尚未配置 HQYL 执行入口，禁止读取本机配置")
    runtime_root = get_runtime_root()
    local_appdata = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        or tempfile.gettempdir()
    )
    settings_directory = os.path.join(local_appdata, SETTINGS_APP_DIR)
    fallback_directory = os.path.join(runtime_root, ".runtime")
    return {
        "runtime_root": runtime_root,
        "settings_directory": settings_directory,
        "settings_file": os.path.join(settings_directory, "settings.json"),
        "fallback_directory": fallback_directory,
        "fallback_file": os.path.join(fallback_directory, "lazada_balance_withdrawal_settings.json"),
        "local_output_root": os.path.join(settings_directory, "output"),
        "global_config_file": os.path.join(runtime_root, "config", "config.yaml"),
        "process_config_file": os.path.join(
            runtime_root, "main", "lazada", "config", "Lazada跨境余额提现统计.yaml"
        ),
    }


# ========== 配置管理 ==========


def _ensure_settings_directory(runtime_files: dict) -> None:
    for key in ("settings_directory", "fallback_directory", "local_output_root"):
        os.makedirs(runtime_files[key], exist_ok=True)
    if os.name != "nt":
        return
    try:
        import ctypes
        hidden = 0x02
        ctypes.windll.kernel32.SetFileAttributesW(runtime_files["settings_directory"], hidden)
        ctypes.windll.kernel32.SetFileAttributesW(runtime_files["fallback_directory"], hidden)
    except Exception:
        pass


def _load_json_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f) or {}
    return payload if isinstance(payload, dict) else {}


def _load_yaml_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        payload = yaml.safe_load(f) or {}
    return payload if isinstance(payload, dict) else {}


def _write_json_file(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.name == "nt" and os.path.exists(path):
        try:
            import ctypes
            normal = 0x80
            ctypes.windll.kernel32.SetFileAttributesW(path, normal)
        except Exception:
            pass
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _deep_merge_dict(base: dict, override: dict) -> dict:
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _force_local_runtime_paths(config: dict, runtime_files: dict) -> dict:
    merged = copy.deepcopy(config or {})
    merged.setdefault("ziniao", {}).setdefault("script", {})
    output_root = str(merged.get("operator", {}).get("output_root", "") or "").strip()
    if output_root:
        output_root = os.path.abspath(os.path.expandvars(os.path.expanduser(output_root)))
    else:
        output_root = runtime_files["local_output_root"]
    merged["ziniao"]["script"]["output_path"] = output_root
    return merged


def load_runtime_configs() -> tuple[dict, dict]:
    runtime_files = get_runtime_files()
    _ensure_settings_directory(runtime_files)

    pcfg = copy.deepcopy(DEFAULT_PROCESS_CONFIG)
    cfg = copy.deepcopy(DEFAULT_GLOBAL_CONFIG)
    cfg = _deep_merge_dict(cfg, _load_yaml_file(runtime_files["global_config_file"]))
    pcfg = _deep_merge_dict(pcfg, _load_yaml_file(runtime_files["process_config_file"]))
    pcfg.setdefault("ziniao", {}).setdefault("browser", copy.deepcopy(cfg.get("ziniao", {}).get("browser", {})))
    pcfg.setdefault("ziniao", {}).setdefault("script", copy.deepcopy(cfg.get("ziniao", {}).get("script", {})))
    for candidate in (runtime_files["settings_file"], runtime_files["fallback_file"]):
        json_settings = _load_json_file(candidate)
        if json_settings:
            pcfg = _deep_merge_dict(pcfg, json_settings)
            cfg = _deep_merge_dict(cfg, json_settings)

    pcfg = _force_local_runtime_paths(pcfg, runtime_files)
    cfg = _force_local_runtime_paths(cfg, runtime_files)
    return pcfg, cfg


def build_settings_from_config(pcfg: dict) -> LazadaWithdrawalSettings:
    browser_cfg = pcfg.get("ziniao", {}).get("browser", {}) or {}
    user_info = pcfg.get("ziniao", {}).get("user_info", {}) or {}
    operator = pcfg.get("operator", {}) or {}
    try:
        max_concurrent = max(int(operator.get("max_concurrent_stores", DEFAULT_MAX_CONCURRENT_STORES) or 1), 1)
    except (TypeError, ValueError):
        max_concurrent = DEFAULT_MAX_CONCURRENT_STORES
    return LazadaWithdrawalSettings(
        company=str(user_info.get("company", "") or ""),
        username=str(user_info.get("username", "") or ""),
        password=str(user_info.get("password", "") or ""),
        country_mode=str(operator.get("country_mode", "auto") or "auto"),
        start_date=str(operator.get("start_date", "") or ""),
        end_date=str(operator.get("end_date", "") or ""),
        store_names=parse_store_names(operator.get("store_names", [])),
        client_path=str(browser_cfg.get("client_path", "") or ""),
        webdriver_path=str(browser_cfg.get("webdriver_path", "") or ""),
        browser_window_mode=str(operator.get("browser_window_mode", "normal") or "normal"),
        max_concurrent_stores=max_concurrent,
        output_root=str(operator.get("output_root", "") or ""),
        collect_unmatched_stores=bool(operator.get("collect_unmatched_stores", False)),
    )


def _merge_settings_into_config(config: dict, settings: LazadaWithdrawalSettings) -> dict:
    merged = copy.deepcopy(config or {})
    merged.setdefault("ziniao", {}).setdefault("user_info", {})
    merged.setdefault("ziniao", {}).setdefault("browser", {})
    merged.setdefault("operator", {})
    merged.setdefault("dingtalk", {}).setdefault("agent", {})

    merged["ziniao"]["user_info"]["company"] = settings.company
    merged["ziniao"]["user_info"]["username"] = settings.username
    merged["ziniao"]["user_info"]["password"] = settings.password
    merged["ziniao"]["browser"]["version"] = merged["ziniao"]["browser"].get("version") or "v6"
    merged["ziniao"]["browser"]["socket_port"] = merged["ziniao"]["browser"].get("socket_port") or 16851
    merged["ziniao"]["browser"]["client_path"] = settings.client_path
    merged["ziniao"]["browser"]["webdriver_path"] = settings.webdriver_path
    merged["operator"]["country_mode"] = settings.country_mode
    merged["operator"]["start_date"] = settings.start_date
    merged["operator"]["end_date"] = settings.end_date
    merged["operator"]["store_names"] = list(settings.store_names)
    merged["operator"]["browser_window_mode"] = (
        "background" if str(settings.browser_window_mode).lower() == "background" else "normal"
    )
    merged["operator"]["max_concurrent_stores"] = max(int(settings.max_concurrent_stores or DEFAULT_MAX_CONCURRENT_STORES), 1)
    merged["operator"]["output_root"] = settings.output_root
    merged["operator"]["collect_unmatched_stores"] = bool(settings.collect_unmatched_stores)
    merged["thread_num"] = merged["operator"]["max_concurrent_stores"]
    merged["task"] = ["lazada_balance_withdrawal"]
    return merged


def save_settings_to_config(settings: LazadaWithdrawalSettings) -> str:
    runtime_files = get_runtime_files()
    _ensure_settings_directory(runtime_files)

    payload = _deep_merge_dict(copy.deepcopy(DEFAULT_GLOBAL_CONFIG), copy.deepcopy(DEFAULT_PROCESS_CONFIG))
    payload = _deep_merge_dict(payload, _load_json_file(runtime_files["settings_file"]))
    payload = _merge_settings_into_config(payload, settings)
    payload = _force_local_runtime_paths(payload, runtime_files)
    _write_json_file(runtime_files["settings_file"], payload)
    try:
        _write_json_file(runtime_files["fallback_file"], payload)
    except OSError:
        pass
    return runtime_files["settings_file"]


# ========== 店铺名解析 ==========


def parse_store_names(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value or "")
    # Keep commas intact because a supported input form is "国家,店铺名".
    normalized = raw.replace(";", "\n")
    return [item.strip() for item in normalized.splitlines() if item.strip()]


def detect_country_from_store_name(store_name: str) -> str:
    """从店铺名自动识别国家"""
    name_lower = store_name.lower()
    for country, keywords in COUNTRY_KEYWORDS.items():
        for keyword in keywords:
            if keyword.lower() in name_lower:
                return country
    return ""


def parse_store_name_with_country(raw_line: str, country_mode: str) -> tuple[str, str]:
    """
    解析店铺行，返回 (store_name, country)
    支持格式:
    - 店铺名
    - 国家,店铺名
    """
    line = raw_line.strip()
    if not line:
        return "", ""

    # 检查是否有国家前缀
    if "," in line or "，" in line:
        sep = "," if "," in line else "，"
        parts = line.split(sep, 1)
        prefix = parts[0].strip()
        store_name = parts[1].strip() if len(parts) > 1 else ""
        if prefix in ("菲律宾", "PH", "ph"):
            return store_name, "PH"
        elif prefix in ("马来", "MY", "my", "马来西亚"):
            return store_name, "MY"
        elif prefix in ("泰国", "TH", "th", "Thailand", "thailand", "Thai", "thai"):
            return store_name, "TH"
        elif prefix in ("越南", "VN", "vn", "Vietnam", "vietnam"):
            return store_name, "VN"

    # 按 country_mode 决定国家
    store_name = line
    if country_mode == "PH":
        return store_name, "PH"
    elif country_mode == "MY":
        return store_name, "MY"
    elif country_mode == "TH":
        return store_name, "TH"
    elif country_mode == "VN":
        return store_name, "VN"
    else:
        # auto: 从店铺名识别
        country = detect_country_from_store_name(store_name)
        return store_name, country


# ========== 钉钉表格操作 ==========


def get_dingtalk_credentials() -> tuple[str, str]:
    """获取钉钉 access_token 和 union_id"""
    token = get_access_token(APP_KEY, APP_SECRET)
    union_id = get_union_id(token, USER_ID)
    return token, union_id


def load_sheet_context(log: logging.Logger = None, *, countries=None, credentials=None) -> dict:
    """
    读取钉钉表格 sheet 列表并校验
    返回: {"doc_id": ..., "sheets": {"PH": {...}, "MY": {...}, "TH": {...}, "VN": {...}}}
    """
    token, union_id = credentials if credentials is not None else get_dingtalk_credentials()
    if not token or not union_id:
        raise ValueError("钉钉应用凭据或操作人未就绪")
    context = {
        "doc_id": DINGTALK_DOC_ID,
        "token": token,
        "union_id": union_id,
        "sheets": {},
    }

    selected_countries = tuple(countries) if countries is not None else ("PH", "MY", "TH")
    for country in selected_countries:
        sheet_cfg = SHEET_CONFIG.get(country)
        if sheet_cfg is None:
            raise ValueError(f"不支持的 Lazada 站点: {country}")
        sheet_name = sheet_cfg["name"]
        expected_sheet_id = sheet_cfg["sheet_id"]
        try:
            info = get_sheet_content(token, union_id, DINGTALK_DOC_ID, sheet_name)
            actual_sheet_id = info.get("sheetid", "")
            if expected_sheet_id and actual_sheet_id != expected_sheet_id:
                if country in ("TH", "VN"):
                    raise RuntimeError(f"{sheet_name} Sheet ID 与已核对的目标不一致，已停止写入")
                if log:
                    log.warning(
                        f"Sheet '{sheet_name}' ID 变化: 配置={expected_sheet_id}, 实际={actual_sheet_id}"
                    )
            context["sheets"][country] = {
                "name": sheet_name,
                "sheet_id": actual_sheet_id or expected_sheet_id,
                "row_count": info.get("row_count", 0),
                "last_non_empty_row": info.get("last_non_empty_row", 0),
            }
            if log:
                log.info(f"Sheet '{sheet_name}' 读取成功, ID={actual_sheet_id}, 行数={info.get('last_non_empty_row', 0)}")
        except Exception as e:
            if log:
                log.error(f"读取 Sheet '{sheet_name}' 失败: {e}")
            raise RuntimeError(f"无法读取钉钉表格 Sheet '{sheet_name}': {e}")

    # 校验表头
    _validate_sheet_headers(context, log)

    return context


def _validate_sheet_headers(context: dict, log: logging.Logger = None):
    """校验 sheet 表头；泰国和越南严格校验全部写入列。"""
    token = context["token"]
    union_id = context["union_id"]

    for country, sheet_info in context["sheets"].items():
        sheet_name = sheet_info["name"]
        try:
            if country in ("PH", "TH", "VN"):
                expected_headers = ["店铺名", "交易时间", "金额（原币）", "备注（美金）"]
            else:
                expected_headers = ["店铺名", "交易时间", "金额", "备注（美金）"]
            if country == "TH":
                expected_headers.append("合计（usd）")
            range_name = "A1:E1" if country == "TH" else "A1:D1"
            headers = safe_read_range(
                token, union_id, DINGTALK_DOC_ID, sheet_name, range_name
            )
            if country in ("TH", "VN") and (not headers or len(headers) == 0):
                raise ValueError(f"{sheet_name} Sheet 表头为空")
            if headers and len(headers) > 0:
                row = headers[0] if isinstance(headers[0], list) else headers
                actual = [str(h).strip() for h in row[:len(expected_headers)]]
                if country in ("TH", "VN") and len(actual) != len(expected_headers):
                    raise ValueError(f"{sheet_name} Sheet 表头列数不符")
                for i, (expected, actual_val) in enumerate(zip(expected_headers, actual)):
                    if expected != actual_val:
                        if country in ("TH", "VN"):
                            raise ValueError(f"{sheet_name} Sheet 第{i+1}列表头不匹配")
                        if log:
                            log.warning(
                                f"Sheet '{sheet_name}' 第{i+1}列表头不匹配: 期望='{expected}', 实际='{actual_val}'"
                            )
        except Exception as e:
            if country in ("TH", "VN"):
                raise RuntimeError(f"无法核对{sheet_name} Sheet 表头，已停止写入") from e
            if log:
                log.warning(f"校验 Sheet '{sheet_name}' 表头失败: {e}")


def read_sheet_rows(context: dict, country: str, log: logging.Logger = None) -> list[dict]:
    """
    读取指定 sheet 的所有行（A:D 列）
    返回: [{"row": 1, "store_name": "...", "trade_time": "...", "amount": "...", "remark": "..."}, ...]
    """
    sheet_info = context["sheets"].get(country)
    if not sheet_info:
        raise ValueError(f"未知国家: {country}")

    sheet_name = sheet_info["name"]
    token = context["token"]
    union_id = context["union_id"]

    last_row = sheet_info.get("last_non_empty_row", 0)
    if last_row < 2:
        if log:
            log.info(f"Sheet '{sheet_name}' 无数据行")
        return []

    range_str = f"A2:D{last_row}"
    try:
        values = safe_read_range(token, union_id, DINGTALK_DOC_ID, sheet_name, range_str)
    except Exception as e:
        if log:
            log.error(f"读取 Sheet '{sheet_name}' 数据失败: {e}")
        raise

    rows = []
    if values:
        for i, row in enumerate(values):
            row_num = i + 2  # 行号从 2 开始（第 1 行是表头）
            row_data = row if isinstance(row, list) else [row]
            store_name = str(row_data[0] if len(row_data) > 0 and row_data[0] else "").strip()
            trade_time = str(row_data[1] if len(row_data) > 1 and row_data[1] else "").strip()
            amount = str(row_data[2] if len(row_data) > 2 and row_data[2] else "").strip()
            remark = str(row_data[3] if len(row_data) > 3 and row_data[3] else "").strip()
            rows.append({
                "row": row_num,
                "store_name": store_name,
                "trade_time": trade_time,
                "amount": amount,
                "remark": remark,
            })

    if log:
        log.info(f"Sheet '{sheet_name}' 读取到 {len(rows)} 行数据")
    return rows


def validate_sheet_period(
    context: dict, country: str, start_date: str, end_date: str, log: logging.Logger = None,
) -> list[dict]:
    """Verify the active sheet's existing month before replacing any withdrawal data."""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if start > end:
        raise ValueError("交易开始日期不能晚于结束日期")
    sheet_info = context.get("sheets", {}).get(country)
    if not sheet_info:
        raise ValueError("目标国家的在线表未完成校验")
    # Refresh both identity and extent: another editor can add rows or replace
    # the active sheet after the run's initial metadata snapshot.
    current = get_sheet_content(
        context["token"], context["union_id"], DINGTALK_DOC_ID, sheet_info["name"],
    )
    if str(current.get("sheetid", "")) != str(sheet_info.get("sheet_id", "")):
        raise ValueError("在线表 Sheet ID 已变化，已停止写入")
    fresh_context = {
        **context,
        "sheets": {**context["sheets"], country: {
            **sheet_info, "last_non_empty_row": int(current.get("last_non_empty_row", 0) or 0),
        }},
    }
    rows = read_sheet_rows(fresh_context, country, log)
    periods = {str(row.get("trade_time", "") or "").strip() for row in rows} - {""}
    expected = f"{start_date}~{end_date}"
    if not periods:
        raise ValueError("在线表没有可核对的交易期间，已停止写入")
    if periods != {expected}:
        raise ValueError(f"在线表交易期间与本次查询 {expected} 不一致，已停止写入")
    return rows


def match_store_row(store_name: str, rows: list[dict]) -> tuple[int | None, str]:
    """
    按 A 列店铺名匹配行
    返回: (row_number, match_reason) 或 (None, fail_reason)
    """
    if not rows:
        return None, "表格无数据行"

    # 标准化函数
    def normalize(s):
        return re.sub(r"[\s　]+", "", str(s)).strip()

    def extract_numbers(s):
        return re.sub(r"[^\d]", "", str(s))

    normalized_input = normalize(store_name)

    # 匹配策略
    exact_matches = []
    normalized_matches = []
    contains_matches = []
    number_matches = []

    for row_data in rows:
        table_name = row_data["store_name"]
        if not table_name:
            continue
        row_num = row_data["row"]

        # 1. 完全匹配
        if store_name == table_name:
            exact_matches.append(row_num)
            continue

        # 2. 去空格/全半角后匹配
        normalized_table = normalize(table_name)
        if normalized_input == normalized_table:
            normalized_matches.append(row_num)
            continue

        # 3. 输入包含表格
        if normalized_table in normalized_input and len(normalized_table) > 3:
            contains_matches.append(row_num)
            continue

        # 4. 表格包含输入
        if normalized_input in normalized_table and len(normalized_input) > 3:
            contains_matches.append(row_num)
            continue

        # 5. 数字补零兼容
        input_numbers = extract_numbers(store_name)
        table_numbers = extract_numbers(table_name)
        if input_numbers and table_numbers and input_numbers.lstrip("0") == table_numbers.lstrip("0"):
            if normalize(store_name.replace(input_numbers, "")) == normalize(table_name.replace(table_numbers, "")):
                number_matches.append(row_num)

    # 按优先级返回
    for matches, reason in [
        (exact_matches, "完全匹配"),
        (normalized_matches, "标准化匹配"),
        (contains_matches, "包含匹配"),
        (number_matches, "数字兼容匹配"),
    ]:
        if len(matches) == 1:
            return matches[0], reason
        elif len(matches) > 1:
            return None, f"匹配到多行: {matches}"

    return None, "表格未找到店铺"


def build_store_task_for_collection(
    store_name: str,
    country: str,
    sheet_cfg: dict,
    rows: list[dict],
    *,
    collect_unmatched_stores: bool = False,
) -> tuple[dict | None, str]:
    """Build a normal task or an explicitly non-writing collection-only task."""
    row_number, match_reason = match_store_row(store_name, rows)
    if row_number is None and not (
        collect_unmatched_stores and country in ("PH", "MY", "TH", "VN")
    ):
        return None, match_reason
    return {
        "store_name": store_name,
        "country": country,
        "sheet_name": str(sheet_cfg.get("name", "") or ""),
        "sheet_id": str(sheet_cfg.get("sheet_id", "") or ""),
        "row_number": row_number,
        "sheet_store_name": next(
            (str(row.get("store_name", "") or "") for row in rows if row.get("row") == row_number),
            "",
        ) if row_number is not None else "",
        "collection_only": row_number is None,
        "match_reason": match_reason,
    }, match_reason


def sheet_row_needs_withdrawal_collection(row: dict, expected_trade_time: str = "") -> bool:
    """Return True for legacy, stale, zero, partial, or otherwise unverified sheet rows."""
    trade_time = str(row.get("trade_time", "") or "").strip()
    amount_text = str(row.get("amount", "") or "").strip()
    remark = str(row.get("remark", "") or "").strip()
    expected = str(expected_trade_time or "").strip()
    if not expected:
        return True
    if expected and trade_time != expected:
        return True

    amount = _parse_sheet_amount(amount_text)
    if re.match(r"^\[完整采集:[1-9]\d*笔\](?:\s|$)", remark):
        return amount is None or amount <= Decimal("0")
    if remark == CONFIRMED_NO_WITHDRAWAL_REMARK:
        return amount is None or amount != Decimal("0")
    return True


def _parse_sheet_amount(value) -> Decimal | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _withdrawal_write_decision(result: dict) -> dict:
    """Fail-closed decision for whether a collector result may overwrite the online sheet."""
    if not isinstance(result, dict):
        return {"write": False, "reason": "任务结果不是字典"}
    record = result.get("record", {})
    if not isinstance(record, dict):
        return {"write": False, "reason": "任务记录不是字典"}

    top_status = str(result.get("status", "") or "").strip().lower()
    status = str(record.get("status", "") or "").strip().lower()
    if not top_status or not status:
        return {"write": False, "reason": "任务结果缺少状态"}
    if top_status != status:
        return {"write": False, "reason": f"顶层状态 {top_status} 与记录状态 {status} 不一致"}

    outcome = str(record.get("collection_outcome", "") or "").strip().lower()
    collection_complete = record.get("collection_complete") is True
    empty_confirmed = record.get("empty_confirmed") is True
    amount_text = str(record.get("amount", "") or "").strip()
    amount = _parse_sheet_amount(amount_text)
    try:
        record_count = int(record.get("record_count", 0) or 0)
    except (TypeError, ValueError):
        return {"write": False, "reason": "记录数量无效"}

    if status == "success":
        if outcome != "complete" or not collection_complete or empty_confirmed:
            return {"write": False, "reason": "成功结果缺少完整采集证明"}
        if record_count <= 0:
            return {"write": False, "reason": "完整采集结果没有交易记录"}
        if amount is None or amount <= Decimal("0"):
            return {"write": False, "reason": "完整采集金额不是有效正数"}
        marker = f"{COMPLETE_COLLECTION_REMARK_PREFIX}{record_count}笔]"
        raw_remark = str(record.get("remark", "") or "").strip()
        online_remark = f"{marker} {raw_remark}".strip()
        return {
            "write": True,
            "reason": "完整采集结果",
            "amount": amount_text,
            "remark": online_remark,
        }

    if status == "no_withdrawal":
        if outcome != "confirmed_empty" or not collection_complete or not empty_confirmed:
            return {"write": False, "reason": "无提现结果缺少表内空态证明"}
        if record_count != 0 or amount is None or amount != Decimal("0"):
            return {"write": False, "reason": "无提现结果的数量或金额不一致"}
        return {
            "write": True,
            "reason": "已确认无提现",
            "amount": "0.00",
            "remark": CONFIRMED_NO_WITHDRAWAL_REMARK,
        }

    return {"write": False, "reason": f"状态 {status or 'unknown'} 不允许写表"}


def write_withdrawal_result(
    context: dict,
    sheet_name: str,
    row_number: int,
    trade_time: str,
    amount: str,
    remark: str = "",
    log: logging.Logger = None,
):
    """
    写入单个店铺的结果到钉钉表格 B:C 列
    B{row}: 交易时间
    C{row}: 金额
    """
    token = context["token"]
    union_id = context["union_id"]

    # 写入 B 列（交易时间）
    b_range = f"B{row_number}"
    safe_update_range(token, union_id, DINGTALK_DOC_ID, sheet_name, b_range, [[trade_time]])
    if log:
        log.info(f"写入 {sheet_name}!{b_range} = {trade_time}")

    # 写入 C 列（金额）
    c_range = f"C{row_number}"
    safe_update_range(token, union_id, DINGTALK_DOC_ID, sheet_name, c_range, [[amount]])
    if log:
        log.info(f"写入 {sheet_name}!{c_range} = {amount}")

    d_range = f"D{row_number}"
    safe_update_range(token, union_id, DINGTALK_DOC_ID, sheet_name, d_range, [[remark]])
    if log:
        log.info(f"写入 {sheet_name}!{d_range} = {remark}")


def write_withdrawal_result_atomic(
    context: dict,
    sheet_name: str,
    row_number: int,
    trade_time: str,
    amount: str,
    remark: str = "",
    log: logging.Logger = None,
    *,
    usd_total: str | None = None,
):
    """Atomically update the verified withdrawal columns, including Thai USD total."""
    token = context["token"]
    union_id = context["union_id"]
    target_range = f"B{row_number}:D{row_number}"
    values = [[trade_time, amount, remark]]
    if sheet_name == SHEET_CONFIG["TH"]["name"]:
        # The old Thai sheet used E for statement IDs. Never overwrite that
        # layout implicitly; the one-time migration must already be complete.
        headers = safe_read_range(token, union_id, DINGTALK_DOC_ID, sheet_name, "E1")
        if headers != [["合计（usd）"]]:
            raise ValueError("泰国 E 列尚未切换为合计（usd），已停止写入")
        parsed_usd = _parse_sheet_amount(usd_total)
        if parsed_usd is None or parsed_usd < Decimal("0"):
            raise ValueError("泰国提现结果缺少有效美元合计")
        target_range = f"B{row_number}:E{row_number}"
        values[0].append(format(parsed_usd, ".2f"))
    safe_update_range(
        token,
        union_id,
        DINGTALK_DOC_ID,
        sheet_name,
        target_range,
        values,
    )
    if log:
        log.info("原子写入 %s!%s", sheet_name, target_range)


def _withdrawal_usd_total(record: dict) -> str | None:
    """Sum paid USD values in individual remarks; never recompute from FX rates."""
    if record.get("collection_outcome") == "confirmed_empty":
        return "0.00"
    currency = str(record.get("currency", "") or "").strip().upper()
    transactions = record.get("transaction_records") or record.get("records") or []
    if not currency or not transactions:
        return None
    total = Decimal("0")
    pattern = re.compile(
        r"(?<![\d.,+\-])([\d,]+\.\d{2})\s*USD\s*\(\s*1\s*"
        + re.escape(currency)
        + r"\s*=\s*\d+(?:\.\d+)?\s*USD\s*\)", re.IGNORECASE
    )
    for transaction in transactions:
        text = str(transaction.get("raw_remark") or transaction.get("remark") or "")
        amounts = pattern.findall(text)
        if len(amounts) != 1:
            return None
        total += Decimal(amounts[0].replace(",", ""))
    return format(total, ".2f")


def _mark_result_need_manual_review(result: dict, reason: str) -> None:
    if not isinstance(result, dict):
        return
    record = result.get("record")
    if not isinstance(record, dict):
        return
    result["status"] = "need_manual_review"
    record["status"] = "need_manual_review"
    record["collection_outcome"] = "parse_failed"
    record["collection_complete"] = False
    record["empty_confirmed"] = False
    record["message"] = str(reason or "本地证据校验失败")


def apply_withdrawal_result_to_sheet(
    sheet_context: dict,
    task_info: dict | None,
    result: dict,
    log: logging.Logger,
    write_lock=None,
) -> dict:
    """Apply the unchanged fail-closed gate, including collection-only blocking."""
    record = result.get("record", {}) if isinstance(result, dict) else {}
    store_name = str(record.get("store_name", "") or "")
    if not task_info:
        return {"status": "blocked_missing_task", "reason": "无法定位钉钉表格行"}
    details_consistent, detail_reason = withdrawal_module.validate_transaction_record_consistency(record)
    if not details_consistent:
        _mark_result_need_manual_review(result, detail_reason)
        return {"status": "blocked_detail_mismatch", "reason": detail_reason}
    if task_info.get("collection_only") or task_info.get("row_number") in (None, ""):
        return {
            "status": "blocked_unmatched",
            "reason": str(task_info.get("match_reason", "在线表无唯一匹配行") or "在线表无唯一匹配行"),
        }

    write_decision = _withdrawal_write_decision(result)
    if not write_decision.get("write"):
        return {
            "status": "blocked_collection_guard",
            "reason": str(write_decision.get("reason", "完整性校验未通过") or "完整性校验未通过"),
        }

    country = str(task_info.get("country", "") or "").upper()
    currency = str(record.get("currency", "") or "").strip().upper()
    expected_currency = {"PH": "PHP", "MY": "MYR", "TH": "THB", "VN": "VND"}.get(country)
    if expected_currency and currency != expected_currency:
        return {"status": "blocked_currency_mismatch", "reason": "采集币种与目标国家不一致"}

    remark = str(write_decision.get("remark", "") or "").strip()
    remark_country = withdrawal_module.infer_lazada_transaction_country_from_text(remark)
    if remark_country and remark_country != country:
        return {"status": "blocked_country_mismatch", "reason": "备注国家与目标国家不一致"}

    write_options = {}
    if country == "TH":
        usd_total = _withdrawal_usd_total(record)
        if usd_total is None:
            return {"status": "blocked_usd_unverified", "reason": "提现明细缺少完整美元实付金额，已停止写入"}
        write_options["usd_total"] = usd_total

    expected_sheet_store_name = str(
        task_info.get("sheet_store_name")
        or task_info.get("store_name")
        or store_name
        or ""
    ).strip()
    if not expected_sheet_store_name:
        return {"status": "blocked_row_identity_unverified", "reason": "目标行缺少店铺身份"}

    def guarded_write() -> dict:
        row_number = int(task_info["row_number"])
        sheet_name = str(task_info["sheet_name"])
        expected_trade_time = str(task_info.get("expected_trade_time", "") or "").strip()
        if expected_trade_time:
            if str(record.get("trade_time", "") or "").strip() != expected_trade_time:
                return {"status": "blocked_period_mismatch", "reason": "采集结果与任务交易期间不一致"}
            try:
                start_date, end_date = expected_trade_time.split("~")
                validate_sheet_period(sheet_context, country, start_date, end_date, log)
            except Exception as exc:
                log.error("%s: 写表前核对交易期间失败：error_type=%s", store_name, type(exc).__name__)
                return {"status": "blocked_period_mismatch", "reason": "写表前无法确认在线表交易期间与本次查询一致"}
        try:
            current = safe_read_range(
                sheet_context["token"],
                sheet_context["union_id"],
                DINGTALK_DOC_ID,
                sheet_name,
                f"A{row_number}:B{row_number}" if expected_trade_time else f"A{row_number}",
            )
        except Exception as exc:
            log.error("%s: 写表前核对 A 列失败：error_type=%s", store_name, type(exc).__name__)
            return {"status": "blocked_row_identity_unverified", "reason": "写表前无法核对店铺行"}
        if not (
            isinstance(current, list)
            and len(current) == 1
            and isinstance(current[0], list)
            and len(current[0]) == (2 if expected_trade_time else 1)
        ):
            return {"status": "blocked_row_identity_unverified", "reason": "写表前 A 列返回格式不符"}
        if str(current[0][0] or "").strip() != expected_sheet_store_name:
            return {"status": "blocked_row_identity_changed", "reason": "在线表店铺行已变化，停止写入"}
        if expected_trade_time and str(current[0][1] or "").strip() not in ("", expected_trade_time):
            return {"status": "blocked_period_mismatch", "reason": "在线表目标行的交易期间已变化，停止写入"}

        try:
            write_withdrawal_result_atomic(
                sheet_context,
                sheet_name,
                row_number,
                str(record.get("trade_time", "") or "").strip(),
                str(write_decision.get("amount", "") or "").strip(),
                remark,
                log,
                **write_options,
            )
        except Exception as exc:
            log.error(
                "%s: 钉钉表格写入失败：error_type=%s",
                store_name,
                type(exc).__name__,
            )
            return {"status": "write_failed", "reason": type(exc).__name__, "error": exc}
        return {"status": "written", "reason": "写表成功"}

    if write_lock is None:
        return guarded_write()
    with write_lock:
        return guarded_write()


def finalize_withdrawal_result_artifacts_and_sheet(
    sheet_context: dict,
    task_info: dict | None,
    result: dict,
    log: logging.Logger,
    write_lock=None,
) -> dict:
    """Persist local details before any online write, then record the final outcome."""
    record = result.get("record", {}) if isinstance(result, dict) else {}
    country = str(record.get("country", "") or "").strip().upper()
    if country not in ("PH", "MY", "TH", "VN"):
        return apply_withdrawal_result_to_sheet(
            sheet_context, task_info, result, log, write_lock=write_lock
        )

    details_consistent, detail_reason = withdrawal_module.validate_transaction_record_consistency(record)
    provisional_status = "pending_sheet_evaluation"
    provisional_reason = "本地逐笔明细已保存，等待在线写表判定"
    if not details_consistent:
        _mark_result_need_manual_review(result, detail_reason)
        withdrawal_module.update_record_result(record)
        provisional_status = "blocked_detail_mismatch"
        provisional_reason = detail_reason

    try:
        exported_rows = withdrawal_module.append_detail_records(
            record,
            sheet_write_status=provisional_status,
            sheet_write_reason=provisional_reason,
        )
        if int(exported_rows or 0) <= 0:
            raise RuntimeError("逐笔明细 CSV 未写入任何行")
    except Exception as exc:
        reason = f"逐笔明细 CSV 写入失败: {type(exc).__name__}"
        _mark_result_need_manual_review(result, reason)
        withdrawal_module.update_record_result(record)
        log.error("%s: %s", str(record.get("store_name", "") or ""), reason)
        return {"status": "blocked_detail_export_failed", "reason": reason}

    if not details_consistent:
        return {"status": "blocked_detail_mismatch", "reason": detail_reason}

    sheet_outcome = apply_withdrawal_result_to_sheet(
        sheet_context,
        task_info,
        result,
        log,
        write_lock=write_lock,
    )
    sheet_status = str(sheet_outcome.get("status", "") or "")
    sheet_reason = str(sheet_outcome.get("reason", "") or "")
    try:
        updated_rows = withdrawal_module.update_detail_records_sheet_outcome(
            record,
            sheet_status,
            sheet_reason,
        )
        if int(updated_rows or 0) != int(exported_rows or 0):
            raise RuntimeError("逐笔明细在线写表状态更新数量不一致")
    except Exception as exc:
        reason = f"逐笔明细 CSV 状态更新失败: {type(exc).__name__}"
        if sheet_status == "written":
            # The online write already succeeded. Do not turn it into a blocked
            # write or retry it merely because the local audit status failed.
            reason = f"钉钉在线表已写入，但{reason}"
            log.error("%s: %s", str(record.get("store_name", "") or ""), reason)
            return {"status": "written_detail_status_update_failed", "reason": reason}
        _mark_result_need_manual_review(result, reason)
        withdrawal_module.update_record_result(record)
        log.error("%s: %s", str(record.get("store_name", "") or ""), reason)
        return {"status": "blocked_detail_export_failed", "reason": reason}
    return sheet_outcome


def _find_task_for_browser_name(browser_name: str, store_task_map: dict) -> dict | None:
    """Resolve a sheet task from the actual Zinia browser name."""
    browser_clean = str(browser_name or "").strip()
    if browser_clean in store_task_map:
        return store_task_map[browser_clean]
    matches = []
    for store_name, task in store_task_map.items():
        score = _store_browser_match_score(store_name, browser_clean)
        if score:
            matches.append((score, task))
    if matches:
        best_score = max(item[0] for item in matches)
        best = [item[1] for item in matches if item[0] == best_score]
    else:
        best = []
    if len(best) == 1:
        return best[0]
    return None


# ========== 紫鸟浏览器过滤 ==========


_STORE_NAME_CHAR_FOLD = str.maketrans({
    "萬": "万", "賽": "赛", "諾": "诺", "鋒": "锋", "廣": "广",
    "為": "为", "爲": "为", "硯": "砚", "國": "国", "營": "营",
})


def _normalize_store_name(value: str) -> str:
    """Normalize harmless display differences without discarding store identity."""
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_STORE_NAME_CHAR_FOLD)
    return "".join(char for char in text.casefold() if char.isalnum())


def _browser_platform_name(browser: dict) -> str:
    return str(
        browser.get("platform_name")
        or browser.get("platformName")
        or browser.get("platform")
        or ""
    )


def _browser_identity(browser: dict) -> str:
    return str(
        browser.get("browserOauth")
        or browser.get("browserId")
        or browser.get("browser_id")
        or browser.get("id")
        or browser.get("storeId")
        or browser.get("store_id")
        or ""
    )


_STORE_ALIAS_COUNTRY_LABELS = {
    "菲律宾": "PH", "菲律賓": "PH",
    "马来西亚": "MY", "馬來西亞": "MY", "马来": "MY", "馬來": "MY",
    "泰国": "TH",
}
_STORE_ALIAS_COUNTRY_PATTERN = "|".join(
    re.escape(label) for label in sorted(_STORE_ALIAS_COUNTRY_LABELS, key=len, reverse=True)
)


def _parse_store_country_number_alias(value: str) -> tuple[str, str] | None:
    """Recognize only an entire country + store-number shorthand, never a warehouse code."""
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_STORE_NAME_CHAR_FOLD).strip()
    match = re.fullmatch(rf"({_STORE_ALIAS_COUNTRY_PATTERN})[\s_-]*([0-9]+)", text)
    if match is None:
        return None
    return _STORE_ALIAS_COUNTRY_LABELS[match.group(1)], match.group(2).lstrip("0") or "0"


def _store_country_mentions(value: str) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_STORE_NAME_CHAR_FOLD).casefold()
    countries = {
        _STORE_ALIAS_COUNTRY_LABELS[match.group(0)]
        for match in re.finditer(_STORE_ALIAS_COUNTRY_PATTERN, text)
    }
    for label, country in (("philippines", "PH"), ("malaysia", "MY"), ("thailand", "TH"), ("vietnam", "VN")):
        if re.search(rf"\b{label}\b", text):
            countries.add(country)
    if "越南" in text:
        countries.add("VN")
    return countries


def _browser_country_mentions(browser: dict) -> set[str]:
    platform = _browser_platform_name(browser)
    countries = _store_country_mentions(str(browser.get("browserName", ""))) | _store_country_mentions(platform)
    # Platform country codes describe the store, whereas PH004 inside a name may be a warehouse.
    countries.update(re.findall(r"(?<![A-Za-z0-9])(PH|MY|TH|VN)(?![A-Za-z0-9])", platform.upper()))
    return countries


def _country_number_alias_matches(alias: tuple[str, str], browser_name: str) -> bool:
    country, number = alias
    text = unicodedata.normalize("NFKC", str(browser_name or "")).translate(_STORE_NAME_CHAR_FOLD)
    if _store_country_mentions(text) != {country}:
        return False
    tokens = {
        (_STORE_ALIAS_COUNTRY_LABELS[match.group(1)], match.group(2).lstrip("0") or "0")
        for match in re.finditer(rf"({_STORE_ALIAS_COUNTRY_PATTERN})[\s_-]*([0-9]+)(?![A-Za-z0-9])", text)
    }
    return tokens == {(country, number)}


def _has_safe_store_name_containment(short_name: str, long_name: str) -> bool:
    """Retain harmless display separators while refusing partial ASCII identifiers."""
    text = unicodedata.normalize("NFKC", str(long_name or "")).translate(_STORE_NAME_CHAR_FOLD).casefold()
    pattern = r"[\W_]*".join(re.escape(char) for char in short_name)
    if short_name[0].isascii() and short_name[0].isalnum():
        pattern = r"(?<![a-z0-9])" + pattern
    if short_name[-1].isascii() and short_name[-1].isalnum():
        pattern += r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _store_browser_match_score(store_name: str, browser_name: str) -> int:
    store_raw = str(store_name or "").strip()
    browser_raw = str(browser_name or "").strip()
    if not store_raw or not browser_raw:
        return 0
    alias = _parse_store_country_number_alias(store_raw)
    if alias is not None:
        # All stores sharing the alias have equal rank, including a literal alias name.
        # An exact-looking shorthand must not hide another real store with the same number.
        return 60 if _country_number_alias_matches(alias, browser_raw) else 0
    if store_raw == browser_raw:
        return 100
    store_clean = _normalize_store_name(store_raw)
    browser_clean = _normalize_store_name(browser_raw)
    if not store_clean or not browser_clean:
        return 0
    if store_clean == browser_clean:
        return 90
    shorter = min(len(store_clean), len(browser_clean))
    if shorter >= 8:
        if store_clean in browser_clean and _has_safe_store_name_containment(store_clean, browser_raw):
            return 70
        if browser_clean in store_clean and _has_safe_store_name_containment(browser_clean, store_raw):
            return 70
    return 0


def _filter_lazada_stores(
    browser_list: list,
    store_names: list[str],
    log: logging.Logger,
    *,
    diagnostics: dict | None = None,
    target_country: str = "",
) -> tuple[list, list]:
    """
    从紫鸟浏览器列表中过滤出 Lazada 店铺
    返回: (matched_stores, unmatched_names)
    """
    matched = []
    unmatched = []
    used_browser_ids = set()
    target_country = str(target_country or "").strip().upper()
    for name in store_names:
        alias = _parse_store_country_number_alias(name)
        candidates_by_id = {}
        wrong_country_names = []
        for browser_index, browser in enumerate(browser_list):
            browser_name = str(browser.get("browserName", ""))
            platform_name = _browser_platform_name(browser)
            identity = _browser_identity(browser) or ("catalog_entry_without_id", browser_index)
            if not re.search(r"(?<![a-z0-9])lazada(?![a-z0-9])", platform_name.casefold()):
                continue
            score = _store_browser_match_score(name, browser_name)
            if not score:
                continue
            countries = _browser_country_mentions(browser)
            requested_country = target_country or (alias[0] if alias is not None else "")
            if requested_country and countries and countries != {requested_country}:
                wrong_country_names.append(browser_name)
                continue
            candidate = (score, browser_name, identity, browser)
            previous = candidates_by_id.get(identity)
            if previous is None or score > previous[0]:
                candidates_by_id[identity] = candidate
        candidates = list(candidates_by_id.values())
        if alias is not None and target_country and alias[0] != target_country:
            unmatched.append(name)
            candidate_names = sorted({item[1] for item in candidates} | set(wrong_country_names))
            if diagnostics is not None:
                diagnostics[name] = {"reason": "country_mismatch", "candidate_names": candidate_names}
            log.error("%s: 简称国家与所选国家 %s 不一致，拒绝自动选择: %s", name, target_country, candidate_names)
            continue
        if candidates:
            best_score = max(item[0] for item in candidates)
            best = [item for item in candidates if item[0] == best_score]
        else:
            best = []
        if len(best) == 1:
            _, browser_name, identity, found = best[0]
            if identity in used_browser_ids:
                unmatched.append(name)
                if diagnostics is not None:
                    diagnostics[name] = {"reason": "duplicate", "candidate_names": [browser_name]}
                log.warning("%s: 与已选择的 Lazada 店铺重复，仅执行一次: %s", name, browser_name)
                continue
            selected = dict(found)
            selected["_withdrawal_requested_store_name"] = name
            matched.append(selected)
            used_browser_ids.add(identity)
            if str(name).strip() != browser_name.strip():
                log.info("%s: 兼容匹配到紫鸟浏览器 %s", name, browser_name)
        elif len(best) > 1:
            unmatched.append(name)
            candidate_names = sorted(item[1] for item in best)
            if diagnostics is not None:
                diagnostics[name] = {"reason": "ambiguous", "candidate_names": candidate_names}
            log.error(
                "%s: 匹配到多个 Lazada 紫鸟浏览器，拒绝自动选择: %s",
                name,
                candidate_names,
            )
        else:
            unmatched.append(name)
            reason = "country_mismatch" if wrong_country_names else "not_found"
            candidate_names = sorted(set(wrong_country_names))
            if diagnostics is not None:
                diagnostics[name] = {"reason": reason, "candidate_names": candidate_names}
            if wrong_country_names:
                log.error("%s: 匹配的 Lazada 店铺国家与所选国家 %s 不一致: %s", name, target_country or alias[0], candidate_names)
            else:
                log.error("%s: 未匹配到 Lazada 紫鸟浏览器", name)
    return matched, unmatched


# ========== 本地输出 ==========


def _prepare_log_directory(cfg: dict, runtime_root: str) -> str:
    output_path = str(cfg.get("ziniao", {}).get("script", {}).get("output_path", "") or "")
    preferred = os.path.join(output_path, CHANNEL, PROCESS_NAME, "logs")
    try:
        os.makedirs(preferred, exist_ok=True)
        return preferred
    except OSError:
        fallback = os.path.join(runtime_root, "logs", PROCESS_NAME)
        os.makedirs(fallback, exist_ok=True)
        return fallback


def _prepare_module_runtime(
    cfg: dict,
    log: logging.Logger,
    log_directory: str,
    record_file: str,
    detail_file: str,
) -> None:
    output_root = str(cfg.get("ziniao", {}).get("script", {}).get("output_path", "") or "")
    pro_directory = os.path.join(output_root, CHANNEL, PROCESS_NAME)
    output_directory = os.path.join(pro_directory, "output")
    error_directory = os.path.join(pro_directory, "error")
    for directory in (output_directory, error_directory, log_directory):
        os.makedirs(directory, exist_ok=True)
    withdrawal_module.configure_runtime(
        output_dir=output_directory,
        error_dir=error_directory,
        log_dir=log_directory,
        record_file=record_file,
        detail_file=detail_file,
        logger=log,
    )


_SUCCESSFUL_RECORD_STATUSES = frozenset(("success", "no_bill", "no_withdrawal"))
_SUCCESSFUL_SHEET_WRITE_STATUSES = frozenset(("written",))
EXECUTION_CSV_FIELDNAMES = [
    "store_name", "country", "trade_time", "status", "collection_outcome",
    "collection_complete", "empty_confirmed", "record_count", "candidate_count",
    "detail_consistent", "detail_consistency_message",
    "amount", "currency", "query_screenshot", "result_screenshot",
    "diagnostic_screenshot", "screenshot", "sheet_write_status",
    "sheet_write_reason", "message",
]


def _execution_store_key(value) -> str:
    normalized = _normalize_store_name(value)
    return normalized or str(value or "").strip().casefold()


def summarize_withdrawal_execution(
    record_rows: list[dict],
    sheet_write_outcomes: dict[str, dict | str] | None = None,
) -> tuple[int, int]:
    """Pure store-level summary that also accounts for PH/MY sheet-write failures.

    CSV failures retain their historical row count. A failed or pending sheet write
    adds one failure only when that store was not already failed by collection.
    """
    rows = list(record_rows or [])
    csv_failed_count = 0
    csv_failed_store_keys = Counter()
    for row in rows:
        status = str(row.get("status", "") or "").strip().lower()
        if status in _SUCCESSFUL_RECORD_STATUSES:
            continue
        csv_failed_count += 1
        store_key = _execution_store_key(row.get("store_name", ""))
        if store_key:
            csv_failed_store_keys[store_key] += 1

    sheet_failed_store_keys = Counter()
    for store_name, outcome in (sheet_write_outcomes or {}).items():
        if isinstance(outcome, dict):
            status = str(outcome.get("status", "") or "").strip().lower()
        else:
            status = str(outcome or "").strip().lower()
        if status in _SUCCESSFUL_SHEET_WRITE_STATUSES:
            continue
        store_key = _execution_store_key(store_name)
        if store_key:
            sheet_failed_store_keys[store_key] += 1

    additional_sheet_failures = sum(
        max(count - csv_failed_store_keys.get(store_key, 0), 0)
        for store_key, count in sheet_failed_store_keys.items()
    )
    return len(rows), csv_failed_count + additional_sheet_failures


def _read_record_rows(record_file: str) -> list[dict]:
    if not os.path.exists(record_file):
        return []
    with open(record_file, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _count_record_statuses(record_file: str) -> tuple[int, int]:
    return summarize_withdrawal_execution(_read_record_rows(record_file))


def _count_record_and_sheet_statuses(
    record_file: str,
    sheet_write_outcomes: dict[str, dict | str] | None,
) -> tuple[int, int]:
    return summarize_withdrawal_execution(
        _read_record_rows(record_file),
        sheet_write_outcomes,
    )


def _append_unmatched_records(record_file: str, store_names: list[str], reason: str) -> None:
    if not store_names:
        return
    os.makedirs(os.path.dirname(record_file), exist_ok=True)
    exists = os.path.exists(record_file)
    fieldnames = list(withdrawal_module.RESULT_CSV_FIELDNAMES)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(record_file, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        for store_name in store_names:
            writer.writerow({
                "store_name": store_name,
                "country": "",
                "status": "unmatched",
                "trade_time": "",
                "amount": "",
                "currency": "",
                "remark": "",
                "collection_outcome": "",
                "collection_complete": "",
                "empty_confirmed": "",
                "record_count": "",
                "candidate_count": "",
                "observed_amount": "",
                "message": reason,
                "started_at": now,
                "finished_at": now,
                "screenshot": "",
                "download_count": "",
                "downloaded_files": "",
                "bill_periods": "",
                "drive_upload_count": "",
                "drive_uploaded_files": "",
                "drive_upload_message": "",
            })


def _sheet_outcome_for_store(store_name: str, outcomes: dict[str, dict | str]) -> dict:
    exact = outcomes.get(store_name)
    if exact is not None:
        return exact if isinstance(exact, dict) else {"status": str(exact), "reason": ""}
    target_key = _execution_store_key(store_name)
    compatible = []
    for candidate_name, candidate_outcome in outcomes.items():
        if _execution_store_key(candidate_name) == target_key:
            compatible.append(candidate_outcome)
    if len(compatible) == 1:
        value = compatible[0]
        return value if isinstance(value, dict) else {"status": str(value), "reason": ""}
    return {}


def write_store_execution_stats(
    record_file: str,
    execution_file: str,
    sheet_write_outcomes: dict[str, dict | str] | None = None,
) -> str:
    """Materialize one row per local store result, including online-write outcome."""
    rows = _read_record_rows(record_file)
    outcomes = dict(sheet_write_outcomes or {})
    directory = os.path.dirname(execution_file)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp_path = f"{execution_file}.tmp"
    with open(temp_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXECUTION_CSV_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for source in rows:
            row = dict(source)
            outcome = _sheet_outcome_for_store(str(source.get("store_name", "") or ""), outcomes)
            sheet_status = str(outcome.get("status", "") or "").strip().lower()
            sheet_reason = str(outcome.get("reason", "") or "").strip()
            if not sheet_status and str(source.get("status", "") or "").lower() == "unmatched":
                sheet_status = "blocked_unmatched"
                sheet_reason = sheet_reason or str(source.get("message", "") or "")
            if sheet_status in ("blocked_detail_mismatch", "blocked_detail_export_failed"):
                row["status"] = "need_manual_review"
                row["collection_complete"] = "False"
                row["empty_confirmed"] = "False"
                row["message"] = sheet_reason
            row["sheet_write_status"] = sheet_status
            row["sheet_write_reason"] = sheet_reason
            writer.writerow(row)
    os.replace(temp_path, execution_file)
    return execution_file


# ========== 主流程 ==========


def run_lazada_balance_withdrawal(
    settings: LazadaWithdrawalSettings | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> LazadaWithdrawalResult:
    """主流程入口"""
    pcfg, cfg = load_runtime_configs()
    effective_settings = settings or build_settings_from_config(pcfg)
    pcfg = _merge_settings_into_config(pcfg, effective_settings)
    cfg = _merge_settings_into_config(cfg, effective_settings)

    runtime_files = get_runtime_files()
    pcfg = _force_local_runtime_paths(pcfg, runtime_files)
    cfg = _force_local_runtime_paths(cfg, runtime_files)
    start_time = datetime.now()
    log_directory = _prepare_log_directory(cfg, runtime_files["runtime_root"])
    log_file = os.path.join(log_directory, f"{PROCESS_NAME}_{start_time.strftime('%Y%m%d')}.log")
    record_file = os.path.join(log_directory, f"{PROCESS_NAME}_{start_time.strftime('%Y%m%d_%H%M%S')}_结果.csv")
    detail_file = os.path.join(log_directory, f"{PROCESS_NAME}_{start_time.strftime('%Y%m%d_%H%M%S')}_逐笔明细.csv")
    execution_file = os.path.join(log_directory, f"{PROCESS_NAME}_{start_time.strftime('%Y%m%d_%H%M%S')}_执行统计.csv")
    log = setup_logger(PROCESS_NAME, log_file)
    _prepare_module_runtime(cfg, log, log_directory, record_file, detail_file)

    if log_callback:
        callback_handler = _LogCallbackHandler(log_callback)
        callback_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        log.addHandler(callback_handler)

    store_names = parse_store_names(pcfg.get("operator", {}).get("store_names", []))
    country_mode = str(pcfg.get("operator", {}).get("country_mode", "auto") or "auto")
    start_date = str(pcfg.get("operator", {}).get("start_date", "") or "")
    end_date = str(pcfg.get("operator", {}).get("end_date", "") or "")
    collect_unmatched_stores = bool(
        pcfg.get("operator", {}).get("collect_unmatched_stores", False)
    )

    matched_store_count = 0
    processed_store_count = 0
    failed_count = 0
    super_browser = None
    sheet_write_outcomes: dict[str, dict[str, str]] = {}
    sheet_write_outcomes_lock = threading.Lock()

    try:
        if not store_names:
            raise RuntimeError("请输入至少一个店铺名")
        if not start_date or not end_date:
            raise RuntimeError("请输入交易开始日期和结束日期")

        log.info("本机IP: %s", yingdao_util.get_ip())

        # 1. 读取钉钉表格
        log.info("=" * 50)
        log.info("开始读取钉钉在线表格")
        try:
            requested_countries = tuple(dict.fromkeys(
                country for raw_name in store_names
                for _, country in [parse_store_name_with_country(raw_name, country_mode)]
                if country in SHEET_CONFIG
            ))
            sheet_context = load_sheet_context(log, countries=requested_countries)
            log.info("钉钉表格读取成功")
        except Exception as e:
            raise RuntimeError(f"钉钉表格读取失败: {e}")

        # 2. 读取各国家 sheet 的数据行
        sheet_rows = {}
        for country in sheet_context["sheets"]:
            if country == "TH":
                # The active Thai sheet is an existing monthly register, not a
                # scratch destination for whatever month the UI defaults to.
                sheet_rows[country] = validate_sheet_period(
                    sheet_context, country, start_date, end_date, log,
                )
                continue
            try:
                sheet_rows[country] = read_sheet_rows(sheet_context, country, log)
            except Exception as e:
                log.error(f"读取 {country} sheet 数据失败: {e}")
                sheet_rows[country] = []

        # 3. 解析店铺并匹配行
        log.info("=" * 50)
        log.info("开始匹配店铺")
        store_tasks: list[dict] = []
        unmatched_names: list[str] = []

        for raw_name in store_names:
            store_name, country = parse_store_name_with_country(raw_name, country_mode)
            if not store_name:
                continue
            if not country:
                unmatched_names.append(store_name)
                log.error(f"{store_name}: 无法识别国家，跳过")
                continue

            sheet_cfg = SHEET_CONFIG.get(country)
            if not sheet_cfg:
                unmatched_names.append(store_name)
                log.error(f"{store_name}: 未知国家 {country}，跳过")
                continue

            rows = sheet_rows.get(country, [])
            task, match_reason = build_store_task_for_collection(
                store_name,
                country,
                sheet_cfg,
                rows,
                collect_unmatched_stores=collect_unmatched_stores,
            )
            if task is not None and country == "TH":
                task["expected_trade_time"] = f"{start_date}~{end_date}"

            if task is None:
                unmatched_names.append(store_name)
                sheet_write_outcomes[store_name] = {
                    "status": "blocked_unmatched",
                    "reason": match_reason,
                }
                log.error(f"{store_name}: {match_reason}")
            elif task.get("collection_only"):
                log.warning(
                    "%s: %s；已启用仅采集模式，本地查询/截图/导出继续，禁止写钉钉",
                    store_name,
                    match_reason,
                )
                store_tasks.append(task)
                sheet_write_outcomes[store_name] = {
                    "status": "blocked_unmatched",
                    "reason": match_reason,
                }
            else:
                log.info(
                    "%s: 国家=%s, Sheet='%s', 行号=%s, 匹配=%s",
                    store_name,
                    country,
                    sheet_cfg["name"],
                    task["row_number"],
                    match_reason,
                )
                store_tasks.append(task)

        # 记录未匹配的店铺
        _append_unmatched_records(record_file, unmatched_names, "店铺匹配失败")

        if not store_tasks:
            raise RuntimeError("所有店铺均未匹配到可执行任务")

        store_task_map = {t["store_name"]: t for t in store_tasks}
        sheet_write_lock = threading.Lock()

        def record_sheet_write_outcome(store_name: str, status: str, reason: str = "") -> None:
            key = str(store_name or "").strip()
            if not key:
                return
            with sheet_write_outcomes_lock:
                sheet_write_outcomes[key] = {
                    "status": str(status or "").strip().lower(),
                    "reason": str(reason or "").strip(),
                }

        def tracked_lazada_balance_withdrawal(driver, store_name: str, output_path: str = ""):
            result = withdrawal_module.lazada_balance_withdrawal(driver, store_name, output_path)
            task_info = _find_task_for_browser_name(store_name, store_task_map)
            record = result.get("record", {}) if isinstance(result, dict) else {}
            status = str(record.get("status", "") or "").strip().lower()
            sheet_outcome = finalize_withdrawal_result_artifacts_and_sheet(
                sheet_context,
                task_info,
                result,
                log,
                write_lock=sheet_write_lock,
            )
            sheet_status = str(sheet_outcome.get("status", "") or "").strip().lower()
            sheet_reason = str(sheet_outcome.get("reason", "") or "").strip()
            if sheet_status != "not_applicable":
                record_sheet_write_outcome(store_name, sheet_status, sheet_reason)
            if sheet_status.startswith("blocked_"):
                log.warning(
                    "%s: 状态=%s，本地证据保留但不覆盖钉钉表格：%s",
                    store_name,
                    status or "unknown",
                    sheet_reason,
                )
            if sheet_outcome.get("error") is not None:
                raise sheet_outcome["error"]
            return result

        # 4. 启动紫鸟浏览器
        log.info("=" * 50)
        log.info("开始启动紫鸟浏览器")
        func_dict = {"lazada_balance_withdrawal": tracked_lazada_balance_withdrawal}
        super_browser = LoadSuperBrowser(func_dict=func_dict, pcfg=pcfg, cfg=cfg, log=log)
        browser_list = super_browser.load_super_browser()
        if not browser_list:
            raise RuntimeError("未读取到紫鸟浏览器列表")

        # 5. 过滤 Lazada 店铺
        task_store_names = [t["store_name"] for t in store_tasks]
        deal_store, unmatched_browser = _filter_lazada_stores(browser_list, task_store_names, log)

        # 将未匹配到浏览器的店铺记录为失败
        for name in unmatched_browser:
            _append_unmatched_records(record_file, [name], "未匹配到 Lazada 紫鸟浏览器")
            record_sheet_write_outcome(name, "blocked_unmatched", "未匹配到 Lazada 紫鸟浏览器")
            # 从 store_tasks 中移除
            store_tasks = [t for t in store_tasks if t["store_name"] != name]

        matched_store_count = len(deal_store)
        if not deal_store:
            raise RuntimeError("输入店铺均未匹配到 Lazada 紫鸟浏览器")

        # 6. 注入 operator_config（包含日期信息）和店铺任务信息到 browser 对象
        for browser in deal_store:
            browser_name = str(browser.get("browserName", ""))
            task = _find_task_for_browser_name(browser_name, store_task_map)
            if task:
                browser["_lazada_task_info"] = task
                if task.get("country") in ("PH", "MY", "TH", "VN"):
                    if task.get("collection_only"):
                        record_sheet_write_outcome(
                            browser_name,
                            "blocked_unmatched",
                            str(task.get("match_reason", "在线表无唯一匹配行") or "在线表无唯一匹配行"),
                        )
                    else:
                        record_sheet_write_outcome(browser_name, "pending", "等待采集并写表")
            else:
                _, inferred_country = parse_store_name_with_country(browser_name, country_mode)
                if inferred_country in ("PH", "MY", "TH", "VN"):
                    record_sheet_write_outcome(browser_name, "blocked_missing_task", "无法定位钉钉表格行")
                log.error("%s: 兼容匹配成功但无法唯一定位任务信息", browser_name)

        # 在 operator_config 中设置日期（所有店铺共用，只读，线程安全）
        super_browser.operator_config["start_date"] = start_date
        super_browser.operator_config["end_date"] = end_date
        drive_cfg = (
            (cfg.get("dingtalk", {}) or {})
            .get("drive", {})
            .get("thailand_income_statement", {})
            or {}
        )
        super_browser.operator_config["thailand_drive_space_id"] = str(
            drive_cfg.get("space_id") or DINGTALK_THAILAND_DRIVE_SPACE_ID
        )
        super_browser.operator_config["thailand_drive_root_folder_id"] = str(
            drive_cfg.get("root_folder_id") or DINGTALK_THAILAND_DRIVE_ROOT_FOLDER_ID
        )

        # 在 super_browser 上存储国家映射（线程安全：不同 key 不冲突）
        super_browser._lazada_country_map = {}
        for browser in deal_store:
            task_info = browser.get("_lazada_task_info")
            if task_info:
                store_name = browser.get("browserName", "")
                super_browser._lazada_country_map[store_name] = task_info["country"]

        # 7. 执行任务
        log.info("=" * 50)
        log.info(f"匹配到 {matched_store_count} 个店铺，开始执行")

        # 使用自定义的任务执行函数（支持写入钉钉表格）
        _execute_stores_with_thread_pool(
            super_browser, deal_store, store_task_map,
            sheet_context, log,
        )

        processed_store_count, failed_count = _count_record_and_sheet_statuses(
            record_file,
            sheet_write_outcomes,
        )
        write_store_execution_stats(record_file, execution_file, sheet_write_outcomes)

        message = "处理完成" if failed_count == 0 else f"处理完成，{failed_count} 个店铺失败或未匹配"
        summary = "\n".join([
            f"输入店铺数：{len(store_names)}",
            f"匹配店铺数：{matched_store_count}",
            f"结果记录数：{processed_store_count}",
            f"失败/未匹配数：{failed_count}",
        ])
        return LazadaWithdrawalResult(
            success=True,
            message=message,
            summary=summary,
            record_file=record_file,
            detail_file=detail_file,
            execution_file=execution_file,
            stats_file=execution_file,
            log_file=log_file,
            log_directory=log_directory,
            matched_store_count=matched_store_count,
            processed_store_count=processed_store_count,
            failed_count=failed_count,
        )

    except Exception as exc:
        log.error("任务失败: %s", traceback.format_exc())
        processed_store_count, failed_count = _count_record_and_sheet_statuses(
            record_file,
            sheet_write_outcomes,
        )
        try:
            write_store_execution_stats(record_file, execution_file, sheet_write_outcomes)
        except Exception:
            log.error("生成逐店执行统计 CSV 失败: %s", traceback.format_exc())
        return LazadaWithdrawalResult(
            success=False,
            message=f"任务失败: {exc}",
            summary="",
            record_file=record_file,
            detail_file=detail_file,
            execution_file=execution_file,
            stats_file=execution_file,
            log_file=log_file,
            log_directory=log_directory,
            matched_store_count=matched_store_count,
            processed_store_count=processed_store_count,
            failed_count=failed_count,
        )
    finally:
        if super_browser is not None:
            try:
                super_browser.get_exit()
            except Exception:
                log.warning("关闭紫鸟 WebDriver 服务失败: %s", traceback.format_exc())


def _execute_stores_with_thread_pool(
    super_browser: LoadSuperBrowser,
    browser_list: list,
    store_task_map: dict,
    sheet_context: dict,
    log: logging.Logger,
):
    """
    使用线程池执行店铺任务
    国家信息通过 browser["_lazada_country"] 传递给 implement 层，线程安全。
    operator_config 中的 start_date/end_date 在调用前已设置（只读，线程安全）。
    """
    def deal_store(browser):
        """单店铺执行函数"""
        store_name = browser.get("browserName", "")
        task_info = browser.get("_lazada_task_info")
        if not task_info:
            log.error(f"{store_name}: 缺少任务信息，跳过")
            return

        # 使用 LoadSuperBrowser 的单店铺执行方法
        super_browser.use_one_browser_run_task(
            super_browser.user_info,
            super_browser.socket_port,
            super_browser.driver_folder_path,
            super_browser.task_list,
            super_browser.func_dict,
            browser,
        )

    # 使用线程池并发执行
    max_workers = max(int(getattr(super_browser, "max_threads", DEFAULT_MAX_CONCURRENT_STORES) or 1), 1)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for browser in browser_list:
            futures.append(executor.submit(deal_store, browser))
        for future in futures:
            try:
                future.result()
            except Exception as e:
                log.error(f"店铺任务执行异常: {e}")
