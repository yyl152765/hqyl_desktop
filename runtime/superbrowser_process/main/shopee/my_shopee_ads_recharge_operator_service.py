from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Callable


def _bootstrap_local_site_packages() -> None:
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    candidate = os.path.join(repo_root, ".venv", "Lib", "site-packages")
    if os.path.isdir(candidate) and candidate not in sys.path:
        sys.path.insert(0, candidate)


_bootstrap_local_site_packages()

import yaml

from implement.shopee import my_shopee_ads_recharge as recharge_module
from main.super_browser_desktop import LoadSuperBrowser
from main.shopee import ads_recharge_guard
from main.shopee.ads_recharge_success_log import (
    append_success_recharge_log,
    build_completion_summary,
    completion_recipients,
    is_success_status,
    send_completion_files,
    send_completion_text,
)
from util import yingdao_util
from util.dingtalk_doc_api import get_access_token, get_sheet_row_count, get_union_id, safe_read_range, safe_update_range
from util.dingtalk_message_util import send_file_dingtalk, send_text_msg
from util.log import setup_logger
from util.webdriver_util import WebdriverUtil
from util.ziniao_runtime_util import get_runtime_root


PROCESS_NAME = "Shopee马来广告充值_运营版"
LEGACY_PROCESS_NAME = "Shopee马来广告充值"
CHANNEL = "Shopee"
SETTINGS_APP_DIR = "ShopeeMalaysiaAdsRecharge"
MIN_CONCURRENT_STORES = 1
MAX_CONCURRENT_STORES = 4
DEFAULT_MAX_CONCURRENT_STORES = 2
DEFAULT_PAYMENT_WAIT_SECONDS = 150
DEFAULT_PAYMENT_CARD_TYPE = "payoneer"
PAYMENT_CARD_TYPES = {"payoneer", "world"}
MIN_PAYMENT_WAIT_SECONDS = 30
MAX_PAYMENT_WAIT_SECONDS = 300
WORLD_MIN_PAYMENT_WAIT_SECONDS = MAX_PAYMENT_WAIT_SECONDS
SITE_CODE = "my"
CUSTOM_RECHARGE_AMOUNT_UNIT = "马币"
DEFAULT_ADS_WALLET_URL = "https://seller.shopee.com.my/portal/marketing/pas/index"
DEFAULT_CUSTOM_RECHARGE_STATUS_DOC_ID = ""
DEFAULT_CUSTOM_RECHARGE_STATUS_SHEET = "Sheet1"

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
    "email_config": {
        "userinfo": {
            "email_name": "",
            "email_auth": "",
        },
    },
}

DEFAULT_PROCESS_CONFIG = {
    "ziniao": {
        "user_info": {
            "company": "",
            "username": "",
            "password": "",
        }
    },
    "dingtalk": {
        "agent": {
            "access_token": "",
            "secret": "",
        },
        "doc_config": {
            "recipient_names": "",
            "custom_recharge_status_doc_id": DEFAULT_CUSTOM_RECHARGE_STATUS_DOC_ID,
            "custom_recharge_status_sheet": DEFAULT_CUSTOM_RECHARGE_STATUS_SHEET,
            "custom_recharge_operator_name": "",
        },
    },
    "task": ["my_shopee_ads_recharge"],
    "thread_num": DEFAULT_MAX_CONCURRENT_STORES,
    "shopee": {
        "ads_wallet_url": DEFAULT_ADS_WALLET_URL,
        "security_verify": {},
        "world_card_sms": {
            "sim_number": "",
            "platform_name": "万里汇",
            "merchant_keyword": "MY SHOPEE MARKETPLACE",
            "merchant_keywords": [
                "MY SHOPEE MARKETPLACE",
                "SHOPEE MY MARKETPLACE",
                "FOR SHOPEE",
                "SHOPEE",
            ],
            "auth_button_timeout": 180,
            "timeout": 180,
            "poll_interval": 3,
        },
    },
    "operator": {
        "store_names": [],
        "sync_database": True,
        "recharge_mode": "sheet",
        "custom_recharge_amount": "",
        "custom_recharge_items": [],
        "max_concurrent_stores": DEFAULT_MAX_CONCURRENT_STORES,
        "payment_wait_seconds": DEFAULT_PAYMENT_WAIT_SECONDS,
        "payment_card_type": DEFAULT_PAYMENT_CARD_TYPE,
        "browser_window_mode": "normal",
        "enable_quota_limit": True,
    },
}


@dataclass
class RuntimeFiles:
    runtime_root: str
    settings_directory: str
    settings_file: str
    fallback_settings_directory: str
    fallback_settings_file: str
    legacy_process_config_file: str
    legacy_global_config_file: str


@dataclass
class AdsRechargeSettings:
    company: str = ""
    username: str = ""
    password: str = ""
    store_names: list[str] = field(default_factory=list)
    client_path: str = ""
    webdriver_path: str = ""
    sync_database: bool = True
    recharge_mode: str = "sheet"
    custom_recharge_amount: str = ""
    custom_recharge_items: list[dict] = field(default_factory=list)
    max_concurrent_stores: int = DEFAULT_MAX_CONCURRENT_STORES
    payment_wait_seconds: int = DEFAULT_PAYMENT_WAIT_SECONDS
    payment_card_type: str = DEFAULT_PAYMENT_CARD_TYPE
    browser_window_mode: str = "normal"
    sms_verification_required: bool = False
    send_notifications: bool = False
    enable_quota_limit: bool = True


@dataclass
class AdsRechargeResult:
    success: bool
    message: str
    summary: str = ""
    record_file: str = ""
    log_file: str = ""
    log_directory: str = ""
    matched_store_count: int = 0
    processed_store_count: int = 0
    records: list[dict] = field(default_factory=list)


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


def get_runtime_files(process_name: str = LEGACY_PROCESS_NAME) -> RuntimeFiles:
    runtime_root = get_runtime_root()
    process_config_path = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.dirname(os.path.dirname(process_config_path))
    local_appdata = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        or tempfile.gettempdir()
    )
    settings_directory = os.path.join(local_appdata, SETTINGS_APP_DIR)
    settings_file = os.path.join(settings_directory, "settings.json")
    fallback_settings_directory = os.path.join(runtime_root, ".runtime")
    fallback_settings_file = os.path.join(fallback_settings_directory, "my_shopee_ads_recharge_settings.json")

    runtime_process_config_path = os.path.join(runtime_root, "main", "shopee", "config", f"{process_name}.yaml")
    runtime_global_config_path = os.path.join(runtime_root, "config", "config.yaml")
    source_process_config_path = os.path.join(process_config_path, "config", f"{process_name}.yaml")
    source_global_config_path = os.path.join(config_path, "config", "config.yaml")

    return RuntimeFiles(
        runtime_root=runtime_root,
        settings_directory=settings_directory,
        settings_file=settings_file,
        fallback_settings_directory=fallback_settings_directory,
        fallback_settings_file=fallback_settings_file,
        legacy_process_config_file=runtime_process_config_path if os.path.exists(runtime_process_config_path) else source_process_config_path,
        legacy_global_config_file=runtime_global_config_path if os.path.exists(runtime_global_config_path) else source_global_config_path,
    )


def parse_store_names(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value or "")
    normalized = raw.replace("，", "\n").replace(",", "\n")
    return [item.strip() for item in normalized.splitlines() if item.strip()]


def parse_custom_recharge_items(value) -> list[dict]:
    if isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, dict):
                store_name = str(item.get("store_name") or "").strip()
                amount = str(item.get("amount") or "").strip()
            else:
                parsed = parse_custom_recharge_items(str(item))
                items.extend(parsed)
                continue
            if store_name and amount:
                items.append({"store_name": store_name, "amount": amount})
        return items

    items = []
    raw = str(value or "")
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        if "," in line or "，" in line:
            parts = re.split(r"[,，]", line, maxsplit=1)
        elif "\t" in line:
            parts = line.split("\t", 1)
        else:
            parts = re.split(r"\s+", line, maxsplit=1)

        if len(parts) != 2:
            raise ValueError(f"第 {line_number} 行格式错误，请检查：必须包含店铺名称和金额")

        store_name = parts[0].strip()
        amount = parts[1].strip()
        if not store_name or not amount:
            raise ValueError(f"第 {line_number} 行格式错误，请检查：必须包含店铺名称和金额")
        try:
            numeric_amount = float(amount)
        except ValueError as exc:
            raise ValueError(f"第 {line_number} 行充值金额不是数字：{amount}") from exc
        if numeric_amount <= 0:
            raise ValueError(f"第 {line_number} 行充值金额必须大于0")

        items.append({"store_name": store_name, "amount": amount})
    return items


def normalize_payment_wait_seconds(value) -> int:
    try:
        seconds = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return DEFAULT_PAYMENT_WAIT_SECONDS
    if seconds < MIN_PAYMENT_WAIT_SECONDS:
        return DEFAULT_PAYMENT_WAIT_SECONDS
    if seconds > MAX_PAYMENT_WAIT_SECONDS:
        return MAX_PAYMENT_WAIT_SECONDS
    return seconds


def normalize_payment_card_type(value) -> str:
    card_type = str(value or DEFAULT_PAYMENT_CARD_TYPE).strip().lower()
    return card_type if card_type in PAYMENT_CARD_TYPES else DEFAULT_PAYMENT_CARD_TYPE


def effective_payment_wait_seconds(payment_card_type, value) -> int:
    seconds = normalize_payment_wait_seconds(value)
    if normalize_payment_card_type(payment_card_type) == "world":
        return max(seconds, WORLD_MIN_PAYMENT_WAIT_SECONDS)
    return seconds


def normalize_max_concurrent_stores(value) -> int:
    try:
        count = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CONCURRENT_STORES
    if count < MIN_CONCURRENT_STORES:
        return DEFAULT_MAX_CONCURRENT_STORES
    if count > MAX_CONCURRENT_STORES:
        return MAX_CONCURRENT_STORES
    return count


def _deep_merge_dict(base: dict, override: dict) -> dict:
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _get_nested_value(payload: dict, path: tuple[str, ...], default=""):
    value = payload
    for key in path:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return default if value is None else value


def _remove_nested_key(payload: dict, path: tuple[str, ...]) -> None:
    value = payload
    for key in path[:-1]:
        if not isinstance(value, dict):
            return
        value = value.get(key)
    if isinstance(value, dict):
        value.pop(path[-1], None)


def _drop_blank_secret_overrides(base: dict, override: dict) -> dict:
    if not isinstance(override, dict):
        return {}
    cleaned = json.loads(json.dumps(override, ensure_ascii=False))
    secret_paths = [
        ("ziniao", "user_info", "password"),
    ]
    for path in secret_paths:
        override_value = str(_get_nested_value(cleaned, path, "") or "").strip()
        base_value = str(_get_nested_value(base, path, "") or "").strip()
        if not override_value and base_value:
            _remove_nested_key(cleaned, path)
    return cleaned


def _ensure_settings_directory(runtime_files: RuntimeFiles) -> None:
    os.makedirs(runtime_files.settings_directory, exist_ok=True)
    os.makedirs(runtime_files.fallback_settings_directory, exist_ok=True)
    if os.name != "nt":
        return
    try:
        import ctypes

        FILE_ATTRIBUTE_HIDDEN = 0x02
        ctypes.windll.kernel32.SetFileAttributesW(runtime_files.settings_directory, FILE_ATTRIBUTE_HIDDEN)
        ctypes.windll.kernel32.SetFileAttributesW(runtime_files.fallback_settings_directory, FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        pass


def _load_json_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f) or {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        backup_path = _backup_corrupt_json_file(path)
        logging.getLogger(__name__).warning(
            "配置文件为空或已损坏，将使用默认配置重新生成：%s；备份：%s；原因：%s",
            path,
            backup_path or "备份失败",
            exc,
        )
        return {}
    return payload if isinstance(payload, dict) else {}


def _backup_corrupt_json_file(path: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{path}.corrupt-{timestamp}"
    suffix = 1
    while os.path.exists(backup_path):
        backup_path = f"{path}.corrupt-{timestamp}-{suffix}"
        suffix += 1
    try:
        os.replace(path, backup_path)
    except OSError:
        return ""
    return backup_path


def _write_json_file(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.name == "nt" and os.path.exists(path):
        try:
            import ctypes

            FILE_ATTRIBUTE_NORMAL = 0x80
            ctypes.windll.kernel32.SetFileAttributesW(path, FILE_ATTRIBUTE_NORMAL)
        except Exception:
            pass
    temp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _load_yaml_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8-sig") as f:
        payload = yaml.safe_load(f) or {}
    return payload if isinstance(payload, dict) else {}


def load_runtime_configs() -> tuple[dict, dict, RuntimeFiles]:
    runtime_files = get_runtime_files()
    _ensure_settings_directory(runtime_files)

    pcfg = _deep_merge_dict(DEFAULT_PROCESS_CONFIG, _load_yaml_file(runtime_files.legacy_process_config_file))
    cfg = _deep_merge_dict(DEFAULT_GLOBAL_CONFIG, _load_yaml_file(runtime_files.legacy_global_config_file))

    json_settings = _load_json_file(runtime_files.settings_file) or _load_json_file(runtime_files.fallback_settings_file)
    if json_settings:
        json_settings = _drop_blank_secret_overrides(pcfg, json_settings)
        pcfg = _deep_merge_dict(pcfg, json_settings)
        cfg = _deep_merge_dict(cfg, json_settings)

    return pcfg, cfg, runtime_files


def build_settings_from_config(pcfg: dict, cfg: dict | None = None) -> AdsRechargeSettings:
    browser_cfg = (cfg or {}).get("ziniao", {}).get("browser", {})
    payment_card_type = normalize_payment_card_type(
        pcfg.get("operator", {}).get("payment_card_type", DEFAULT_PAYMENT_CARD_TYPE)
    )
    return AdsRechargeSettings(
        company=str(pcfg.get("ziniao", {}).get("user_info", {}).get("company", "") or ""),
        username=str(pcfg.get("ziniao", {}).get("user_info", {}).get("username", "") or ""),
        password=str(pcfg.get("ziniao", {}).get("user_info", {}).get("password", "") or ""),
        store_names=parse_store_names(pcfg.get("operator", {}).get("store_names", [])),
        client_path=str(browser_cfg.get("client_path", "") or ""),
        webdriver_path=str(browser_cfg.get("webdriver_path", "") or ""),
        sync_database=bool(pcfg.get("operator", {}).get("sync_database", True)),
        recharge_mode=str(pcfg.get("operator", {}).get("recharge_mode", "sheet") or "sheet"),
        custom_recharge_amount=str(pcfg.get("operator", {}).get("custom_recharge_amount", "") or ""),
        custom_recharge_items=parse_custom_recharge_items(pcfg.get("operator", {}).get("custom_recharge_items", [])),
        max_concurrent_stores=normalize_max_concurrent_stores(
            pcfg.get("operator", {}).get("max_concurrent_stores", DEFAULT_MAX_CONCURRENT_STORES)
        ),
        payment_wait_seconds=effective_payment_wait_seconds(
            payment_card_type,
            pcfg.get("operator", {}).get("payment_wait_seconds", DEFAULT_PAYMENT_WAIT_SECONDS),
        ),
        payment_card_type=payment_card_type,
        browser_window_mode=str(pcfg.get("operator", {}).get("browser_window_mode", "normal") or "normal"),
        enable_quota_limit=bool(pcfg.get("operator", {}).get("enable_quota_limit", True)),
        send_notifications=False,
    )


def _merge_settings_into_config(config: dict, settings: AdsRechargeSettings) -> dict:
    merged = dict(config or {})
    merged.setdefault("ziniao", {}).setdefault("user_info", {})
    merged.setdefault("ziniao", {}).setdefault("browser", {})
    merged.setdefault("operator", {})

    doc_credentials = getattr(settings, "dingtalk_doc_credentials", {})
    if isinstance(doc_credentials, dict):
        doc_config = merged.setdefault("dingtalk", {}).setdefault("doc_config", {})
        for key in ("app_key", "app_secret"):
            value = str(doc_credentials.get(key) or "")
            if value:
                doc_config[key] = value

    merged["ziniao"]["user_info"]["company"] = settings.company
    merged["ziniao"]["user_info"]["username"] = settings.username
    merged["ziniao"]["user_info"]["password"] = settings.password
    merged["ziniao"]["browser"]["client_path"] = settings.client_path
    merged["ziniao"]["browser"]["webdriver_path"] = settings.webdriver_path
    merged["operator"]["store_names"] = list(settings.store_names)
    merged["operator"]["sync_database"] = bool(settings.sync_database)
    merged["operator"]["recharge_mode"] = settings.recharge_mode
    merged["operator"]["custom_recharge_amount"] = str(settings.custom_recharge_amount or "").strip()
    merged["operator"]["custom_recharge_items"] = list(settings.custom_recharge_items)
    max_concurrent_stores = normalize_max_concurrent_stores(settings.max_concurrent_stores)
    merged["operator"]["max_concurrent_stores"] = max_concurrent_stores
    payment_card_type = normalize_payment_card_type(
        getattr(settings, "payment_card_type", DEFAULT_PAYMENT_CARD_TYPE)
    )
    merged["operator"]["payment_card_type"] = payment_card_type
    merged["operator"]["payment_wait_seconds"] = effective_payment_wait_seconds(
        payment_card_type,
        settings.payment_wait_seconds,
    )
    merged["operator"]["browser_window_mode"] = (
        "background" if str(settings.browser_window_mode or "").strip().lower() == "background" else "normal"
    )
    sms_verification_required = bool(getattr(settings, "sms_verification_required", False))
    merged["operator"]["sms_verification_required"] = sms_verification_required
    merged.setdefault("shopee", {}).setdefault("security_verify", {})["enabled"] = sms_verification_required
    merged.setdefault("shopee", {}).setdefault("world_card_sms", {})["enabled"] = sms_verification_required
    merged["operator"]["enable_quota_limit"] = bool(settings.enable_quota_limit)
    merged["thread_num"] = max_concurrent_stores
    merged["task"] = ["my_shopee_ads_recharge"]
    return merged


def save_settings_to_config(settings: AdsRechargeSettings, process_config_file: str | None = None) -> str:
    runtime_files = get_runtime_files()
    config_path = process_config_file or runtime_files.settings_file
    _ensure_settings_directory(runtime_files)

    payload = _deep_merge_dict(DEFAULT_GLOBAL_CONFIG, DEFAULT_PROCESS_CONFIG)
    payload = _deep_merge_dict(payload, _load_json_file(config_path))
    payload = _merge_settings_into_config(payload, settings)
    _write_json_file(config_path, payload)

    if config_path != runtime_files.fallback_settings_file:
        try:
            _write_json_file(runtime_files.fallback_settings_file, payload)
        except OSError:
            pass

    return config_path


def _prepare_log_directory(cfg: dict, runtime_root: str) -> str:
    output_path = str(cfg.get("ziniao", {}).get("script", {}).get("output_path", "")).strip()
    preferred_directory = os.path.join(output_path, CHANNEL, PROCESS_NAME, "logs")
    try:
        if not output_path:
            raise OSError("output_path not configured")
        os.makedirs(preferred_directory, exist_ok=True)
        return preferred_directory
    except OSError:
        fallback_directory = os.path.join(runtime_root, "logs", PROCESS_NAME)
        os.makedirs(fallback_directory, exist_ok=True)
        return fallback_directory


def _prepare_module_runtime(pcfg: dict, cfg: dict, log: logging.Logger, log_directory: str) -> None:
    output_root = os.path.dirname(log_directory)
    output_directory = os.path.join(output_root, "output")
    error_directory = os.path.join(output_root, "error")
    for directory in (output_directory, error_directory, log_directory):
        os.makedirs(directory, exist_ok=True)

    today_suffix = f"{datetime.today().month}.{datetime.today().day}"
    recharge_module.pcfg = pcfg
    recharge_module.cfg = cfg
    recharge_module.output_path = str(cfg.get("ziniao", {}).get("script", {}).get("output_path", "") or "")
    recharge_module.email_name = str(cfg.get("email_config", {}).get("userinfo", {}).get("email_name", "") or "")
    recharge_module.email_auth = str(cfg.get("email_config", {}).get("userinfo", {}).get("email_auth", "") or "")
    recharge_module.ads_wallet_url = str(pcfg.get("shopee", {}).get("ads_wallet_url", "") or DEFAULT_ADS_WALLET_URL)
    recharge_module.security_verify_config = dict(pcfg.get("shopee", {}).get("security_verify", {}) or {})
    sms_verification_required = bool(pcfg.get("operator", {}).get("sms_verification_required", False))
    recharge_module.security_verify_config["enabled"] = sms_verification_required
    recharge_module.world_card_sms_config = dict(pcfg.get("shopee", {}).get("world_card_sms", {}) or {})
    recharge_module.world_card_sms_config["enabled"] = sms_verification_required
    recharge_module.payment_card_type = normalize_payment_card_type(
        pcfg.get("operator", {}).get("payment_card_type", DEFAULT_PAYMENT_CARD_TYPE)
    )
    recharge_module.doc_config = pcfg.get("dingtalk", {}).get("doc_config", {}) or {}
    recharge_module.PAYMENT_RESULT_TIMEOUT_SECONDS = effective_payment_wait_seconds(
        recharge_module.payment_card_type,
        pcfg.get("operator", {}).get("payment_wait_seconds", DEFAULT_PAYMENT_WAIT_SECONDS)
    )
    recharge_module.output_directory = output_directory
    recharge_module.error_directory = error_directory
    recharge_module.log_directory = log_directory
    recharge_module.error_shop_file = os.path.join(error_directory, f"{LEGACY_PROCESS_NAME}_失败店铺_{today_suffix}.txt")
    recharge_module.success_shop_file = os.path.join(error_directory, f"{LEGACY_PROCESS_NAME}_成功店铺_{today_suffix}.txt")
    recharge_module.log = log
    for file_path in (recharge_module.error_shop_file, recharge_module.success_shop_file):
        if not os.path.exists(file_path):
            with open(file_path, "w", encoding="utf-8"):
                pass


def _get_cell(row, index, default="") -> str:
    if len(row) <= index or row[index] in (None, ""):
        return default
    return str(row[index]).strip()


def _is_today_yyyy_mm_dd(date_input) -> bool:
    if not isinstance(date_input, str):
        return False
    clean_date_str = date_input.strip().replace("'", "")
    return bool(clean_date_str) and clean_date_str == datetime.today().strftime("%Y-%m-%d")


def _is_success_message(message: str) -> bool:
    text = str(message or "").strip().lower()
    return any(keyword.lower() in text for keyword in ("支付成功", "余额复查确认支付成功", "payment successful", "completed", "successful"))


def _get_recharge_plan_shop(log: logging.Logger, pcfg: dict) -> list[dict]:
    doc_config = pcfg.get("dingtalk", {}).get("doc_config", {}) or {}
    app_key = doc_config.get("app_key", "")
    app_secret = doc_config.get("app_secret", "")
    user_id = doc_config.get("app_userid", "")
    config_doc_id = doc_config.get("config_doc_id", "")
    config_sheet = doc_config.get("config_sheet", "Sheet1")
    if not all([app_key, app_secret, user_id, config_doc_id, config_sheet]):
        raise RuntimeError("钉钉文档配置不完整，请检查 Shopee 配置文件 .yaml")

    log.info("开始读取钉钉表格配置数据")
    access_token = get_access_token(app_key, app_secret)
    union_id = get_union_id(access_token, user_id)
    total_rows = get_sheet_row_count(access_token, union_id, config_doc_id, config_sheet)
    if total_rows <= 1:
        log.warning("表格中没有找到相关数据")
        return []

    recharge_plan_data = safe_read_range(access_token, union_id, config_doc_id, config_sheet, f"A2:A{total_rows}")
    amount_data = safe_read_range(access_token, union_id, config_doc_id, config_sheet, f"O2:O{total_rows}")
    need_recharge_data = safe_read_range(access_token, union_id, config_doc_id, config_sheet, f"P2:P{total_rows}")
    status_data = safe_read_range(access_token, union_id, config_doc_id, config_sheet, f"Q2:R{total_rows}")
    recharge_plan = []
    for i, row in enumerate(recharge_plan_data):
        shop_name = _get_cell(row, 0)
        amount_row = amount_data[i] if i < len(amount_data) else []
        need_recharge_row = need_recharge_data[i] if i < len(need_recharge_data) else []
        status_row = status_data[i] if i < len(status_data) else []
        recharge_amount = _get_cell(amount_row, 0)
        need_recharge = _get_cell(need_recharge_row, 0)
        update_time = _get_cell(status_row, 0)
        update_message = _get_cell(status_row, 1)
        if not shop_name:
            continue
        if _is_today_yyyy_mm_dd(update_time):
            if need_recharge == "需充值":
                if _is_success_message(update_message):
                    log.info(f"店铺 [{shop_name}] 今天已充值成功，跳过")
                    continue
                log.info(f"店铺 [{shop_name}] 今天充值未成功，重新处理。上次消息: {update_message}")
            elif need_recharge:
                log.info(f"店铺 [{shop_name}] 今天已确认不需要充值，跳过")
                continue
            else:
                log.info(f"店铺 [{shop_name}] 今天已登记但 P 列为空，继续处理")
        recharge_plan.append({
            "shop_name": shop_name,
            "recharge_amount": recharge_amount,
            "update_time": update_time,
            "need_recharge": need_recharge,
            "update_message": update_message,
        })
    log.info(f"Recharge plan stores: {len(recharge_plan)}")
    return recharge_plan


def _write_sheet_corrected_success(
    log: logging.Logger,
    pcfg: dict,
    store_name: str,
    recharge_amount,
    balance_before,
    balance_after,
) -> bool:
    doc_config = pcfg.get("dingtalk", {}).get("doc_config", {}) or {}
    app_key = doc_config.get("app_key", "")
    app_secret = doc_config.get("app_secret", "")
    user_id = doc_config.get("app_userid", "")
    config_doc_id = doc_config.get("config_doc_id", "")
    config_sheet = doc_config.get("config_sheet", "Sheet1")
    if not all([app_key, app_secret, user_id, config_doc_id, config_sheet]):
        log.warning(f"{store_name}: DingTalk doc config incomplete, skip corrected success writeback")
        return False

    try:
        access_token = get_access_token(app_key, app_secret)
        union_id = get_union_id(access_token, user_id)
        total_rows = get_sheet_row_count(access_token, union_id, config_doc_id, config_sheet)
        shop_data = safe_read_range(access_token, union_id, config_doc_id, config_sheet, f"A2:A{total_rows}")
        row_count = -1
        for i, row in enumerate(shop_data):
            shop_name = _get_cell(row, 0)
            if shop_name and shop_name in store_name:
                row_count = i + 2
                break
        if row_count < 0:
            log.warning(f"{store_name}: DingTalk row not found, skip corrected success writeback")
            return False

        update_message = (
            f"余额复查确认支付成功！充值金额：{recharge_amount}，"
            f"充值前余额：{balance_before}，复查余额：{balance_after}。"
        )
        safe_update_range(
            access_token,
            union_id,
            config_doc_id,
            config_sheet,
            f"Q{row_count}:R{row_count}",
            [["'" + datetime.today().strftime("%Y-%m-%d"), update_message]],
            number_format="@",
        )
        log.info(f"{store_name}: corrected success written back to DingTalk row {row_count}")
        return True
    except Exception as exc:
        log.warning(f"{store_name}: corrected success writeback failed: {exc}")
        return False


def _decimal_from_text(value) -> Decimal | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        amount = Decimal(match.group(0))
        if "万" in text:
            amount *= Decimal("10000")
        return amount
    except (InvalidOperation, ValueError):
        return None


def _same_amount(left, right) -> bool:
    left_amount = _decimal_from_text(left)
    right_amount = _decimal_from_text(right)
    if left_amount is None or right_amount is None:
        return False
    return left_amount.quantize(Decimal("0.01")) == right_amount.quantize(Decimal("0.01"))


def _extract_message_value(message: str, labels: list[str]) -> str:
    text = str(message or "")
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*[:：]\s*([^,，。;\n]+)", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _custom_status_row_values(doc_config: dict, username: str, recharge_amount, message: str) -> list[str]:
    actual_payment_amount = _extract_message_value(message, ["支付总额", "Payment Total", "Total"])
    if not actual_payment_amount:
        actual_payment_amount = str(recharge_amount or "")
    operator_name = str(doc_config.get("custom_recharge_operator_name") or username or "").strip()
    feedback = f"{datetime.today().month}.{datetime.today().day}已充值"
    return [feedback, operator_name, actual_payment_amount]


def _custom_status_account_name(store_name: str) -> str:
    text = str(store_name or "").strip()
    match = re.search(r"(?:马来|菲律宾|越南|印尼|泰国)\s*\d+", text)
    if match:
        return re.sub(r"\s+", "", match.group(0))
    return text


def _custom_status_recharge_amount(doc_config: dict, recharge_amount) -> str:
    amount = _decimal_from_text(recharge_amount)
    if amount is None:
        amount_text = str(recharge_amount or "").strip()
    else:
        amount_text = format(amount, "f")
        if "." in amount_text:
            amount_text = amount_text.rstrip("0").rstrip(".")
    unit = str(doc_config.get("custom_recharge_amount_unit") or CUSTOM_RECHARGE_AMOUNT_UNIT).strip()
    return f"{amount_text}{unit}" if amount_text else unit


def _custom_status_append_row_values(
    doc_config: dict,
    username: str,
    store_name: str,
    recharge_amount,
    message: str,
) -> list[str]:
    feedback, operator_name, actual_payment_amount = _custom_status_row_values(
        doc_config,
        username,
        recharge_amount,
        message,
    )
    today = datetime.today()
    return [
        f"{today.year}/{today.month}/{today.day}",
        _custom_status_account_name(store_name),
        "",
        _custom_status_recharge_amount(doc_config, recharge_amount),
        "",
        feedback,
        operator_name,
        actual_payment_amount,
    ]


def _find_custom_status_target_row(rows: list, store_name: str, recharge_amount, first_row_number: int = 2) -> int:
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        account_name = _get_cell(row, 0)
        amount_text = _get_cell(row, 2)
        if not account_name:
            continue
        store_match = account_name in store_name or store_name in account_name
        if store_match and _same_amount(amount_text, recharge_amount):
            return index + first_row_number
    return -1


def _dws_executable_path() -> str:
    executable = shutil.which("dws")
    if executable:
        return executable
    if os.name == "nt":
        candidate = os.path.join(os.path.expanduser("~"), ".local", "bin", "dws.exe")
        if os.path.exists(candidate):
            return candidate
    return ""


def _parse_dws_json(stdout: str) -> dict:
    text = str(stdout or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start_candidates = [pos for pos in (text.find("{"), text.find("[")) if pos >= 0]
        if not start_candidates:
            raise
        start = min(start_candidates)
        return json.loads(text[start:])


def _run_dws_json(args: list[str], timeout: int = 60) -> dict:
    executable = _dws_executable_path()
    if not executable:
        raise RuntimeError("未找到 dws 命令，无法使用 spreadsheetv2 兜底登记")
    command = [executable, *args]
    if "--format" not in command:
        command.extend(["--format", "json"])
    if "--timeout" not in command:
        command.extend(["--timeout", str(max(30, min(int(timeout), 300)))])
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout + 10,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"dws 命令执行失败：{' '.join(args)}；{detail}")
    payload = _parse_dws_json(completed.stdout)
    if isinstance(payload, dict) and payload.get("success") is False:
        message = payload.get("message") or payload.get("error") or payload
        raise RuntimeError(f"dws 返回失败：{message}")
    return payload if isinstance(payload, dict) else {}


def _dws_spreadsheet_node_arg(doc_id: str) -> str:
    raw = str(doc_id or "").strip()
    if raw.startswith(("http://", "https://")) or raw.startswith("n"):
        return raw
    return f"https://alidocs.dingtalk.com/spreadsheetv2/{raw}/edit?dentryKey={raw}&type=s"


def _resolve_dws_sheet(doc_id: str, sheet_name: str) -> tuple[str, str]:
    node_arg = _dws_spreadsheet_node_arg(doc_id)
    try:
        doc_info = _run_dws_json(["doc", "info", "--node", node_arg], timeout=30)
        node_arg = str(doc_info.get("nodeId") or node_arg)
    except Exception:
        pass

    sheet_payload = _run_dws_json(["sheet", "list", "--node", node_arg], timeout=30)
    sheets = sheet_payload.get("sheets") or []
    if not sheets:
        raise RuntimeError("DWS 未读取到工作表列表")
    target = None
    for sheet in sheets:
        if str(sheet.get("name") or "") == sheet_name or str(sheet.get("sheetId") or "") == sheet_name:
            target = sheet
            break
    if target is None:
        target = sheets[0]
    sheet_id = str(target.get("sheetId") or target.get("id") or sheet_name)
    if not sheet_id:
        raise RuntimeError("DWS 未读取到工作表 ID")
    return node_arg, sheet_id


def _custom_status_search_terms(store_name: str) -> list[str]:
    text = str(store_name or "").strip()
    terms: list[str] = []
    for match in re.findall(r"(?:马来|菲律宾|越南|印尼|泰国)\s*\d+", text):
        terms.append(re.sub(r"\s+", "", match))
    if text:
        terms.append(text)
    for part in re.split(r"[-_/\\\s]+", text):
        part = part.strip()
        if len(part) >= 2:
            terms.append(part)

    unique_terms = []
    seen = set()
    for term in terms:
        if term and term not in seen:
            unique_terms.append(term)
            seen.add(term)
    return unique_terms


def _dws_find_candidate_rows(node_arg: str, sheet_id: str, store_name: str, total_rows: int) -> list[tuple[int, list]]:
    candidates: dict[int, list] = {}
    for term in _custom_status_search_terms(store_name):
        find_payload = _run_dws_json(
            [
                "sheet",
                "find",
                "--node",
                node_arg,
                "--sheet-id",
                sheet_id,
                "--find",
                term,
                "--use-regexp",
                "false",
                "--match-case",
                "false",
            ],
            timeout=60,
        )
        matched_cells = find_payload.get("matchedCells") or []
        row_numbers = []
        for cell in matched_cells:
            a1_notation = str((cell or {}).get("a1Notation") or "")
            match = re.fullmatch(r"([A-Z]+)(\d+)", a1_notation.upper())
            if not match:
                continue
            column, row_text = match.groups()
            row_number = int(row_text)
            if column == "B" and 2 <= row_number <= total_rows:
                row_numbers.append(row_number)

        for row_number in sorted(set(row_numbers), reverse=True):
            if row_number in candidates:
                continue
            row_payload = _run_dws_json(
                [
                    "sheet",
                    "range",
                    "read",
                    "--node",
                    node_arg,
                    "--sheet-id",
                    sheet_id,
                    "--range",
                    f"B{row_number}:H{row_number}",
                ],
                timeout=45,
            )
            values = row_payload.get("displayValues") or row_payload.get("values") or []
            if values:
                candidates[row_number] = values[0]

        if candidates:
            break

    return sorted(candidates.items(), key=lambda item: item[0], reverse=True)


def _write_custom_status_with_dws(
    log: logging.Logger,
    doc_config: dict,
    doc_id: str,
    sheet_name: str,
    store_name: str,
    recharge_amount,
    message: str,
    username: str,
) -> bool:
    node_arg, sheet_id = _resolve_dws_sheet(doc_id, sheet_name)
    sheet_info = _run_dws_json(["sheet", "info", "--node", node_arg, "--sheet-id", sheet_id], timeout=30)
    non_empty_range = sheet_info.get("nonEmptyRange") if isinstance(sheet_info.get("nonEmptyRange"), dict) else {}
    total_rows = int(non_empty_range.get("lastRow") or sheet_info.get("lastNonEmptyRow") or sheet_info.get("rowCount") or 0)
    candidates = _dws_find_candidate_rows(node_arg, sheet_id, store_name, total_rows) if total_rows > 1 else []
    target_row = -1
    for row_number, row in candidates:
        account_name = _get_cell(row, 0)
        amount_text = _get_cell(row, 2)
        store_match = account_name and (account_name in store_name or store_name in account_name)
        if store_match and _same_amount(amount_text, recharge_amount):
            target_row = row_number
            break
    if target_row < 0:
        values = [
            _custom_status_append_row_values(
                doc_config,
                username,
                store_name,
                recharge_amount,
                message,
            )
        ]
        _run_dws_json(
            [
                "sheet",
                "append",
                "--node",
                node_arg,
                "--sheet-id",
                sheet_id,
                "--values",
                json.dumps(values, ensure_ascii=False),
                "--yes",
            ],
            timeout=60,
        )
        log.info(f"{store_name}: 未找到匹配行，已通过 DWS 在表尾新增自定义充值记录")
        return True

    values = [_custom_status_row_values(doc_config, username, recharge_amount, message)]
    _run_dws_json(
        [
            "sheet",
            "range",
            "update",
            "--node",
            node_arg,
            "--sheet-id",
            sheet_id,
            "--range",
            f"F{target_row}:H{target_row}",
            "--values",
            json.dumps(values, ensure_ascii=False),
            "--number-format",
            "@",
            "--yes",
        ],
        timeout=60,
    )
    log.info(f"{store_name}: 已通过 DWS 登记自定义充值成功状态到在线表格第 {target_row} 行")
    return True


def _custom_status_doc_id(doc_config: dict) -> str:
    return str(doc_config.get("custom_recharge_status_doc_id") or DEFAULT_CUSTOM_RECHARGE_STATUS_DOC_ID).strip()


def _custom_status_sheet(doc_config: dict) -> str:
    return str(doc_config.get("custom_recharge_status_sheet") or DEFAULT_CUSTOM_RECHARGE_STATUS_SHEET).strip()


def _write_custom_recharge_success_status(
    log: logging.Logger,
    pcfg: dict,
    store_name: str,
    recharge_amount,
    message: str,
    username: str,
) -> bool:
    doc_config = pcfg.get("dingtalk", {}).get("doc_config", {}) or {}
    app_key = doc_config.get("app_key", "")
    app_secret = doc_config.get("app_secret", "")
    user_id = doc_config.get("app_userid", "")
    doc_id = _custom_status_doc_id(doc_config)
    sheet_name = _custom_status_sheet(doc_config)
    if not all([doc_id, sheet_name]):
        log.warning(f"{store_name}: 自定义充值登记表配置不完整，跳过登记")
        return False

    if all([app_key, app_secret, user_id]):
        try:
            access_token = get_access_token(app_key, app_secret)
            union_id = get_union_id(access_token, user_id)
            total_rows = get_sheet_row_count(access_token, union_id, doc_id, sheet_name)
            rows = safe_read_range(access_token, union_id, doc_id, sheet_name, f"B2:H{total_rows}") if total_rows > 1 else []
            target_row = _find_custom_status_target_row(rows, store_name, recharge_amount)
            if target_row < 0:
                target_row = max(total_rows + 1, 2)
                safe_update_range(
                    access_token,
                    union_id,
                    doc_id,
                    sheet_name,
                    f"A{target_row}:H{target_row}",
                    [
                        _custom_status_append_row_values(
                            doc_config,
                            username,
                            store_name,
                            recharge_amount,
                            message,
                        )
                    ],
                    number_format="@",
                )
                log.info(f"{store_name}: 未找到匹配行，已在在线表格第 {target_row} 行新增自定义充值记录")
                return True

            safe_update_range(
                access_token,
                union_id,
                doc_id,
                sheet_name,
                f"F{target_row}:H{target_row}",
                [_custom_status_row_values(doc_config, username, recharge_amount, message)],
                number_format="@",
            )
            log.info(f"{store_name}: 已登记自定义充值成功状态到在线表格第 {target_row} 行")
            return True
        except Exception as exc:
            log.warning(f"{store_name}: 旧接口登记失败，尝试 DWS 兜底：{exc}")
    else:
        log.warning(f"{store_name}: 自定义充值旧接口配置不完整，尝试 DWS 兜底")

    try:
        return _write_custom_status_with_dws(log, doc_config, doc_id, sheet_name, store_name, recharge_amount, message, username)
    except Exception as exc:
        log.warning(f"{store_name}: DWS 自定义充值成功状态登记失败：{exc}")
        return False


def _filter_super_browser_shop(
    log: logging.Logger,
    browser_list: list,
    recharge_plan: list[dict],
    store_names: list[str],
) -> list:
    log.info("Filter target stores from recharge plan")
    if not store_names:
        log.warning("No store names configured")
        return []

    allowed_names = set(store_names)
    store_list = []
    matched_browser_names = set()
    for item in recharge_plan:
        shop_name = item["shop_name"]
        if shop_name not in allowed_names:
            continue
        matched = False
        for browser in browser_list:
            browser_name = str(browser.get("browserName", ""))
            platform_name = str(browser.get("platform_name") or browser.get("platformName") or "")
            if shop_name in browser_name and browser_name not in matched_browser_names and "Shopee" in platform_name:
                store_list.append(browser)
                matched_browser_names.add(browser_name)
                matched = True
                break
        if not matched:
            log.warning(f"Store not found in browser list: {shop_name}")

    for store_name in allowed_names:
        if not any(store_name == item.get("shop_name") for item in recharge_plan):
            log.info(f"Store not in recharge plan, skipped: {store_name}")

    log.info(f"Matched stores: {len(store_list)}")
    return store_list


def _filter_custom_recharge_stores(log: logging.Logger, browser_list: list, custom_items: list[dict]) -> list:
    log.info("Filter target stores from custom recharge list")
    if not custom_items:
        log.warning("No custom recharge items configured")
        return []

    store_list = []
    matched_browser_names = set()
    for item in custom_items:
        shop_name = str(item.get("store_name") or "").strip()
        matched = False
        for browser in browser_list:
            browser_name = str(browser.get("browserName", ""))
            platform_name = str(browser.get("platform_name") or browser.get("platformName") or "")
            if shop_name in browser_name and browser_name not in matched_browser_names and "Shopee" in platform_name:
                store_list.append(browser)
                matched_browser_names.add(browser_name)
                matched = True
                break
        if not matched:
            log.warning(f"Store not found in browser list: {shop_name}")

    log.info(f"Matched custom stores: {len(store_list)}")
    return store_list


def _find_sheet_recharge_amount(store_name: str, recharge_amount_map: dict[str, str]) -> str:
    for shop_name, amount in recharge_amount_map.items():
        if shop_name and shop_name in store_name and str(amount or "").strip():
            return str(amount).strip()
    return ""


def _sync_shop_data_to_database(log: logging.Logger, browser_list: list, pcfg: dict) -> None:
    try:
        from util.db_helper import get_ansy_datalist, get_pg_connection, upsert_batch_to_pg

        field_mapping = pcfg.get("database", {}).get("FIELD_MAPPING", {})
        data_list = get_ansy_datalist(browser_list, field_mapping, ["platform_id", "site_id"])
        conn = get_pg_connection()
        try:
            upsert_batch_to_pg(
                conn=conn,
                table_name="shop_browsers",
                data_list=data_list,
                conflict_keys=["browser_oauth"],
            )
            log.info("Ziniao store data synced to database")
        finally:
            conn.close()
    except Exception as exc:
        log.warning(f"紫鸟店铺数据同步失败，可能影响后续流程：{exc}")


def _safe_send_message(log: logging.Logger, message: str, message_token: str, message_secret: str, enabled: bool) -> None:
    log.info(message)
    if not enabled or not message_token or not message_secret:
        return
    try:
        send_text_msg(message, message_token, message_secret)
    except Exception as exc:
        log.error(f"send_text_msg failed: {exc}")


def _safe_send_result_files(log: logging.Logger, recipient_names: list[str], log_directory: str, enabled: bool) -> None:
    if not enabled or not recipient_names:
        return
    output_root = os.path.dirname(log_directory)
    for subdir in ("output", "error"):
        directory = os.path.join(output_root, subdir)
        if not os.path.isdir(directory):
            continue
        for filename in os.listdir(directory):
            file_path = os.path.join(directory, filename)
            if os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
                try:
                    send_file_dingtalk(file_path, recipient_names)
                except Exception as exc:
                    log.error(f"send_file_dingtalk failed: {file_path}: {exc}")


def _safe_super_browser_exit(log: logging.Logger, super_browser, timeout: int = 10) -> None:
    if super_browser is None:
        return

    exit_error = {}

    def _exit():
        try:
            super_browser.get_exit()
        except Exception:
            exit_error["traceback"] = traceback.format_exc()

    log.info("Start exiting Ziniao client")
    exit_thread = threading.Thread(target=_exit, daemon=True)
    exit_thread.start()
    exit_thread.join(timeout)
    if exit_thread.is_alive():
        log.warning(f"Ziniao client exit timeout after {timeout} seconds")
    elif exit_error:
        log.warning(f"紫鸟客户端退出异常：{exit_error['traceback']}")
    else:
        log.info("Ziniao client exited")


class StoreTaskTimeout(TimeoutError):
    pass


def _run_with_timeout(
    action: Callable[[], object],
    timeout_seconds: int,
    timeout_message: str,
    log: logging.Logger | None = None,
    on_timeout: Callable[[], None] | None = None,
):
    box: dict = {}

    def _target() -> None:
        try:
            box["result"] = action()
        except BaseException as exc:
            box["exc"] = exc
            box["traceback"] = traceback.format_exc()

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        if log:
            log.error(timeout_message)
        if on_timeout:
            try:
                on_timeout()
            except Exception as exc:
                if log:
                    log.warning(f"超时清理失败: {exc}")
        raise StoreTaskTimeout(timeout_message)
    if "exc" in box:
        if log and box.get("traceback"):
            log.error(str(box["traceback"]))
        raise box["exc"]
    return box.get("result")


def _build_record_text(records: list[dict], start_time_mark: datetime, username: str, store_names: list[str]) -> str:
    lines = [
        f"任务: {PROCESS_NAME}",
        f"开始时间: {start_time_mark.strftime('%Y-%m-%d %H:%M:%S')}",
        f"结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"紫鸟账号: {username or '未填写'}",
        f"店铺名称: {', '.join(store_names) if store_names else '未填写'}",
        "",
    ]
    for index, record in enumerate(records, start=1):
        lines.append(
            " | ".join(
                [
                    f"{index}. {record.get('store_name')}",
                    f"状态: {record.get('status')}",
                    f"耗时: {record.get('duration_seconds', 0)}s",
                    f"说明: {record.get('message')}",
                ]
            )
        )
    return "\n".join(lines)


def _run_custom_ads_recharge(driver, store_name: str, recharge_amount: str | float | int) -> dict:
    log = recharge_module.log
    log.info(f"Start custom recharge: {store_name}, amount: {recharge_amount}")
    webdriver_util = WebdriverUtil(driver)

    if not recharge_module._ensure_login(webdriver_util, store_name):
        message = f"{store_name}: Shopee login failed"
        recharge_module._write_error(store_name, message)
        return {"status": "failed", "message": message, "recharge_amount": recharge_amount}

    target_url = recharge_module.ads_wallet_url or DEFAULT_ADS_WALLET_URL
    if "仓发" in store_name:
        target_url = "https://seller.shopee.cn/portal/marketing/pas/index"

    log.info(f"{store_name}: go to Shopee Ads page: {target_url}")
    webdriver_util.go_to(target_url)
    time.sleep(8)
    try:
        current_url = str(driver.current_url or "")
        log.info(f"{store_name}: current Shopee URL after Ads page navigation: {current_url}")
    except Exception as exc:
        log.warning(f"{store_name}: read current URL after Ads navigation failed: {exc}")

    try:
        recharge_module.close_alert(webdriver_util)
    except Exception:
        pass

    is_recharge, message = recharge_module.recharge_ads(webdriver_util, store_name, recharge_amount)
    if is_recharge == "processing":
        log.info(message)
        return {"status": "processing", "message": message, "recharge_amount": recharge_amount}
    if is_recharge:
        log.info(message)
        return {"status": "success", "message": message, "recharge_amount": recharge_amount}

    recharge_module._write_error(store_name, message)
    return {"status": "failed", "message": message, "recharge_amount": recharge_amount}


def _find_custom_recharge_amount(store_name: str, custom_items: list[dict]) -> str:
    for item in custom_items:
        item_store_name = str(item.get("store_name") or "").strip()
        if item_store_name and item_store_name in store_name:
            return str(item.get("amount") or "").strip()
    return ""


def run_ads_recharge(
    settings: AdsRechargeSettings | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> AdsRechargeResult:
    pcfg, cfg, runtime_files = load_runtime_configs()
    pcfg = _deep_merge_dict(pcfg, getattr(settings, "private_site_settings", {}))
    effective_settings = settings or build_settings_from_config(pcfg, cfg)
    pcfg = _merge_settings_into_config(pcfg, effective_settings)
    cfg = _merge_settings_into_config(cfg, effective_settings)

    start_time_mark = datetime.now()
    log_directory = _prepare_log_directory(cfg, runtime_files.runtime_root)
    log_file = os.path.join(log_directory, f"{PROCESS_NAME}_{start_time_mark.strftime('%Y%m%d')}.log")
    record_file_path = os.path.join(log_directory, f"{PROCESS_NAME}_{start_time_mark.strftime('%Y%m%d_%H%M%S')}_运行记录.txt")
    log = setup_logger(PROCESS_NAME, log_file)
    _prepare_module_runtime(pcfg, cfg, log, log_directory)

    if log_callback:
        callback_handler = _LogCallbackHandler(log_callback)
        callback_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        log.addHandler(callback_handler)

    stream = _CallbackStream(log_callback) if log_callback else None

    username = str(pcfg.get("ziniao", {}).get("user_info", {}).get("username", "") or "")
    message_token = str(pcfg.get("dingtalk", {}).get("agent", {}).get("access_token", "") or "")
    message_secret = str(pcfg.get("dingtalk", {}).get("agent", {}).get("secret", "") or "")
    doc_config = pcfg.get("dingtalk", {}).get("doc_config", {}) or {}
    recipient_names = [name.strip() for name in str(doc_config.get("recipient_names", "")).split(",") if name.strip()]
    store_names = parse_store_names(pcfg.get("operator", {}).get("store_names", []))
    recharge_mode = str(pcfg.get("operator", {}).get("recharge_mode", "sheet") or "sheet")
    custom_recharge_amount = str(pcfg.get("operator", {}).get("custom_recharge_amount", "") or "").strip()
    custom_recharge_items = parse_custom_recharge_items(pcfg.get("operator", {}).get("custom_recharge_items", []))
    sheet_recharge_amount_map: dict[str, str] = {}
    payment_card_type = normalize_payment_card_type(
        pcfg.get("operator", {}).get("payment_card_type", DEFAULT_PAYMENT_CARD_TYPE)
    )
    payment_wait_seconds = effective_payment_wait_seconds(
        payment_card_type,
        pcfg.get("operator", {}).get("payment_wait_seconds", DEFAULT_PAYMENT_WAIT_SECONDS)
    )
    max_concurrent_stores = normalize_max_concurrent_stores(
        pcfg.get("operator", {}).get("max_concurrent_stores", DEFAULT_MAX_CONCURRENT_STORES)
    )
    sms_verification_required = bool(pcfg.get("operator", {}).get("sms_verification_required", False))
    if payment_card_type == "world" and sms_verification_required and max_concurrent_stores > 1:
        log.info("WORLD 卡短信验证码按单 SIM 匹配，已将并发店铺数强制调整为 1")
        max_concurrent_stores = 1
    pcfg["thread_num"] = max_concurrent_stores
    pcfg.setdefault("operator", {})["max_concurrent_stores"] = max_concurrent_stores
    enable_quota_limit = bool(pcfg.get("operator", {}).get("enable_quota_limit", True))
    store_task_timeout_seconds = max(int(payment_wait_seconds) + 180, 240)
    if payment_card_type == "world" and sms_verification_required:
        sms_timeout = normalize_payment_wait_seconds(
            pcfg.get("shopee", {}).get("world_card_sms", {}).get("timeout", 180)
        )
        store_task_timeout_seconds = max(store_task_timeout_seconds + sms_timeout + 60, 360)
    if recharge_mode == "custom":
        store_names = [str(item.get("store_name") or "").strip() for item in custom_recharge_items if str(item.get("store_name") or "").strip()]
    ip = yingdao_util.get_ip()
    run_id = ads_recharge_guard.create_run_log(
        PROCESS_NAME, SITE_CODE, username, recharge_mode, len(store_names)
    )
    try:
        permission = ads_recharge_guard.check_permission(SITE_CODE, recharge_mode)
    except Exception as exc:
        ads_recharge_guard.finish_run_log(run_id, "failed", [], str(exc))
        raise

    process_records: list[dict] = []
    process_records_lock = threading.Lock()
    success_log_sheet_names: set[str] = set()
    success_log_sheet_names_lock = threading.Lock()

    def append_process_record(record: dict) -> None:
        with process_records_lock:
            process_records.append(record)

    guard_context = None
    over_limit_notified = False
    quota_stop_event = threading.Event()

    def tracked_my_shopee_ads_recharge(driver, store_name: str, output_path: str = ""):
        nonlocal over_limit_notified
        start_at = datetime.now()
        guard_info = {}
        result = {}
        if quota_stop_event.is_set():
            duration_seconds = round((datetime.now() - start_at).total_seconds(), 1)
            record = {
                "store_name": store_name,
                "status": "skipped_limit",
                "message": "Daily quota exhausted, skipped recharge",
                "duration_seconds": duration_seconds,
                "recharge_amount": "0",
                "balance_before": None,
                "balance_after": None,
                "retry_balance_check": False,
                "corrected_to_success": False,
                "quota_reserved": False,
                "started_at": start_at,
                "ended_at": datetime.now(),
            }
            append_process_record(record)
            try:
                ads_recharge_guard.write_store_log(run_id, SITE_CODE, record)
            except Exception as log_exc:
                log.warning(f"{store_name}: 写入店铺执行日志失败: {log_exc}")
            return {"status": "skipped_limit", "message": record["message"]}
        try:
            if recharge_mode == "custom":
                store_recharge_amount = _find_custom_recharge_amount(store_name, custom_recharge_items)
                if not store_recharge_amount:
                    raise RuntimeError(f"未找到自定义充值金额：{store_name}")
                try:
                    result = _run_with_timeout(
                        lambda: _run_custom_ads_recharge(driver, store_name, store_recharge_amount),
                        store_task_timeout_seconds,
                        f"{store_name}: 单店铺任务超过 {store_task_timeout_seconds} 秒未结束，按失败处理并触发余额复查",
                        log,
                        lambda: ads_recharge_guard.force_close_current_store(driver, store_name, log),
                    ) or {}
                except StoreTaskTimeout as timeout_exc:
                    result = {"status": "failed", "message": str(timeout_exc), "task_timeout": True}
            else:
                try:
                    result = _run_with_timeout(
                        lambda: recharge_module.my_shopee_ads_recharge(driver, store_name, output_path),
                        store_task_timeout_seconds,
                        f"{store_name}: 单店铺任务超过 {store_task_timeout_seconds} 秒未结束，按失败处理并触发余额复查",
                        log,
                        lambda: ads_recharge_guard.force_close_current_store(driver, store_name, log),
                    ) or {}
                except StoreTaskTimeout as timeout_exc:
                    result = {"status": "failed", "message": str(timeout_exc), "task_timeout": True}

            guard_info = guard_context.pop(store_name) if guard_context else {}
            reservation = guard_info.get("reservation")
            status = str(result.get("status") or "completed")
            message = str(result.get("message") or "任务执行完成")
            recharge_amount = result.get("recharge_amount") or guard_info.get("recharge_amount") or 0
            if not recharge_amount and recharge_mode != "custom":
                recharge_amount = _find_sheet_recharge_amount(store_name, sheet_recharge_amount_map) or 0
            balance_before = result.get("balance_before")
            if balance_before is None:
                balance_before = guard_info.get("balance_before")
            balance_after = result.get("balance_after")

            if status == "processing":
                log.info(f"{store_name}: 支付处理中，不做失败复查，避免重复支付")

            verification = ads_recharge_guard.verify_recharge_status_with_balance_check(
                status=status,
                message=message,
                recharge_amount=recharge_amount,
                balance_before=balance_before,
                balance_after=balance_after,
                site_code=SITE_CODE,
                store_name=store_name,
                recharge_module=recharge_module,
                driver=driver,
                default_ads_wallet_url=DEFAULT_ADS_WALLET_URL,
                log=log,
                run_with_timeout=_run_with_timeout,
            )
            status = verification["status"]
            message = verification["message"]
            balance_after = verification["balance_after"]
            retry_balance_check = bool(verification["retry_balance_check"])
            corrected_to_success = bool(verification["corrected_to_success"])
            if corrected_to_success:
                _write_sheet_corrected_success(log, pcfg, store_name, recharge_amount, balance_before, balance_after)

            if reservation:
                if status in ("success", "corrected_success", "completed"):
                    ads_recharge_guard.confirm_quota(reservation)
                else:
                    ads_recharge_guard.release_quota(reservation)

            duration_seconds = round((datetime.now() - start_at).total_seconds(), 1)
            record = {
                "store_name": store_name,
                "status": status,
                "message": message,
                "duration_seconds": duration_seconds,
                "recharge_amount": str(recharge_amount or 0),
                "balance_before": str(balance_before) if balance_before is not None else None,
                "balance_after": str(balance_after) if balance_after is not None else None,
                "retry_balance_check": retry_balance_check,
                "corrected_to_success": corrected_to_success,
                "quota_reserved": bool(guard_info.get("quota_reserved")),
                "started_at": start_at,
                "ended_at": datetime.now(),
            }
            append_process_record(record)
            try:
                ads_recharge_guard.write_store_log(run_id, SITE_CODE, record)
            except Exception as log_exc:
                log.warning(f"{store_name}: 写入店铺执行日志失败: {log_exc}")
            if status == "failed":
                try:
                    recharge_module._write_error(store_name, message)
                except Exception as error_file_exc:
                    log.warning(f"{store_name}: 失败店铺文件写入失败: {error_file_exc}")
            if is_success_status(status):
                try:
                    recharge_module._write_success(store_name, message)
                except Exception as success_file_exc:
                    log.warning(f"{store_name}: 成功店铺文件写入失败: {success_file_exc}")
                if recharge_mode == "custom":
                    _write_custom_recharge_success_status(
                        log,
                        pcfg,
                        store_name,
                        recharge_amount,
                        message,
                        username,
                    )
                try:
                    success_sheet_name = append_success_recharge_log(
                        log,
                        pcfg,
                        record,
                        run_id=run_id,
                        site_code=SITE_CODE,
                        username=username,
                        recharge_mode=recharge_mode,
                    )
                    if success_sheet_name:
                        with success_log_sheet_names_lock:
                            success_log_sheet_names.add(success_sheet_name)
                except Exception as success_log_exc:
                    log.warning(f"{store_name}: 成功充值登记失败: {success_log_exc}")
            result["status"] = status
            result["message"] = message
            return result
        except ads_recharge_guard.QuotaExceeded as exc:
            quota_stop_event.set()
            guard_info = guard_context.pop(store_name) if guard_context else {}
            detail = dict(getattr(exc, "detail", {}) or {})
            detail["store_name"] = store_name
            if not over_limit_notified:
                try:
                    ads_recharge_guard.send_over_limit_notification(
                        SITE_CODE,
                        detail,
                        message_token,
                        message_secret,
                        effective_settings.send_notifications,
                    )
                    over_limit_notified = True
                except Exception as notify_exc:
                    log.error(f"超限通知发送失败: {notify_exc}")
            duration_seconds = round((datetime.now() - start_at).total_seconds(), 1)
            record = {
                "store_name": store_name,
                "status": "skipped_limit",
                "message": str(exc),
                "duration_seconds": duration_seconds,
                "recharge_amount": str(detail.get("request_amount") or guard_info.get("recharge_amount") or 0),
                "balance_before": str(guard_info.get("balance_before")) if guard_info.get("balance_before") is not None else None,
                "balance_after": None,
                "retry_balance_check": False,
                "corrected_to_success": False,
                "quota_reserved": False,
                "started_at": start_at,
                "ended_at": datetime.now(),
            }
            append_process_record(record)
            try:
                ads_recharge_guard.write_store_log(run_id, SITE_CODE, record)
            except Exception as log_exc:
                log.warning(f"{store_name}: 写入店铺执行日志失败: {log_exc}")
            return {"status": "skipped_limit", "message": str(exc)}
        except Exception as exc:
            guard_info = guard_context.pop(store_name) if guard_context else {}
            reservation = guard_info.get("reservation")
            if reservation:
                try:
                    ads_recharge_guard.release_quota(reservation)
                except Exception as release_exc:
                    log.error(f"{store_name}: 释放充值额度异常: {release_exc}")
            duration_seconds = round((datetime.now() - start_at).total_seconds(), 1)
            record = {
                "store_name": store_name,
                "status": "failed",
                "message": str(exc),
                "duration_seconds": duration_seconds,
                "recharge_amount": str(guard_info.get("recharge_amount") or 0),
                "balance_before": str(guard_info.get("balance_before")) if guard_info.get("balance_before") is not None else None,
                "balance_after": None,
                "retry_balance_check": False,
                "corrected_to_success": False,
                "quota_reserved": bool(guard_info.get("quota_reserved")),
                "started_at": start_at,
                "ended_at": datetime.now(),
            }
            append_process_record(record)
            try:
                ads_recharge_guard.write_store_log(run_id, SITE_CODE, record)
            except Exception as log_exc:
                log.warning(f"{store_name}: 写入店铺执行日志失败: {log_exc}")
            return {"status": "failed", "message": str(exc)}

    func_dict = {
        "my_shopee_ads_recharge": tracked_my_shopee_ads_recharge,
    }

    super_browser = None
    with contextlib.ExitStack() as stack:
        if stream:
            stack.enter_context(contextlib.redirect_stdout(stream))
            stack.enter_context(contextlib.redirect_stderr(stream))

        try:
            _safe_send_message(
                log,
                f"{ip} {PROCESS_NAME} 开始运行，紫鸟账号: {username}",
                message_token,
                message_secret,
                effective_settings.send_notifications,
            )
            guard_context = ads_recharge_guard.install_recharge_quota_guard(
                recharge_module, SITE_CODE, enable_quota_limit, log
            )
            super_browser = LoadSuperBrowser(func_dict=func_dict, pcfg=pcfg, cfg=cfg, log=log)
            browser_list = super_browser.load_super_browser() or []
            if effective_settings.sync_database:
                _sync_shop_data_to_database(log, browser_list, pcfg)

            if recharge_mode == "custom":
                target_store_list = _filter_custom_recharge_stores(log, browser_list, custom_recharge_items)
            else:
                recharge_plan = _get_recharge_plan_shop(log, pcfg)
                sheet_recharge_amount_map = {
                    str(item.get("shop_name") or "").strip(): str(item.get("recharge_amount") or "").strip()
                    for item in recharge_plan
                    if str(item.get("shop_name") or "").strip()
                }
                target_store_list = _filter_super_browser_shop(log, browser_list, recharge_plan, store_names)
            if not target_store_list:
                _safe_super_browser_exit(log, super_browser)
                super_browser = None
                message = "未找到匹配的店铺"
                _safe_send_message(log, message, message_token, message_secret, effective_settings.send_notifications)
                ads_recharge_guard.finish_run_log(run_id, "failed", [], message)
                return AdsRechargeResult(success=False, message=message, log_file=log_file, log_directory=log_directory)

            log.info(f"Start recharge with max {max_concurrent_stores} concurrent stores; payment wait {payment_wait_seconds}s")
            super_browser.use_all_browser_run_task_with_thread_pool(target_store_list)
            log.info("All store tasks finished")
            _safe_super_browser_exit(log, super_browser)
            super_browser = None

            with process_records_lock:
                records = list(process_records)
            recorded_names = {record.get("store_name") for record in records}
            for browser in target_store_list:
                store_name = str(browser.get("browserName") or "")
                if store_name and store_name not in recorded_names:
                    records.append({"store_name": store_name, "status": "failed", "message": "Task did not finish", "duration_seconds": 0})
            records.sort(key=lambda item: item.get("store_name") or "")

            record_text = _build_record_text(records, start_time_mark, username, store_names)
            with open(record_file_path, "w", encoding="utf-8") as f:
                f.write(record_text)

            with success_log_sheet_names_lock:
                success_sheet_summary = ", ".join(sorted(success_log_sheet_names))
            summary = build_completion_summary(
                process_name=PROCESS_NAME,
                site_code=SITE_CODE,
                run_id=run_id,
                ip=ip,
                start_time=start_time_mark,
                records=records,
                target_count=len(target_store_list),
                recharge_mode=recharge_mode,
                success_sheet_name=success_sheet_summary,
            )
            _safe_send_result_files(log, recipient_names, log_directory, effective_settings.send_notifications)
            completion_targets = completion_recipients(doc_config, recipient_names)
            send_completion_text(log, completion_targets, summary)
            send_completion_files(
                log,
                completion_targets,
                [
                    record_file_path,
                    log_file,
                    getattr(recharge_module, "success_shop_file", ""),
                    getattr(recharge_module, "error_shop_file", ""),
                ],
            )
            _safe_send_message(
                log,
                f"{ip} {PROCESS_NAME} 执行完成，共 {len(target_store_list)} 个店铺",
                message_token,
                message_secret,
                effective_settings.send_notifications,
            )
            ads_recharge_guard.finish_run_log(run_id, "success", records, "执行完成")
            return AdsRechargeResult(
                success=True,
                message="执行完成",
                summary=summary,
                record_file=record_file_path,
                log_file=log_file,
                log_directory=log_directory,
                matched_store_count=len(target_store_list),
                processed_store_count=len(records),
                records=records,
            )
        except SystemExit as exc:
            message = f"任务执行异常：{exc}"
            log.error(message)
            return AdsRechargeResult(success=False, message=message, log_file=log_file, log_directory=log_directory)
        except Exception as exc:
            message = f"执行异常: {exc}"
            log.exception(message)
            ads_recharge_guard.finish_run_log(run_id, "failed", process_records, message)
            return AdsRechargeResult(success=False, message=message, log_file=log_file, log_directory=log_directory)
        finally:
            if guard_context:
                guard_context.restore()
            _safe_super_browser_exit(log, super_browser, timeout=5)
