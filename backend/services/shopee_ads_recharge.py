"""Shopee 广告充值服务 — 路由层，动态调用 superbrowser_process 站点模块。"""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

ProgressCallback = Callable[[str], None]

# ---------------------------------------------------------------------------
# 站点定义
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShopeeAdsSite:
    code: str
    name: str
    label: str
    module_name: str


SITE_OPTIONS: tuple[ShopeeAdsSite, ...] = (
    ShopeeAdsSite(code="id", name="印尼", label="印尼", module_name="main.shopee.id_shopee_ads_recharge_operator_service"),
    ShopeeAdsSite(code="th", name="泰国", label="泰国", module_name="main.shopee.th_shopee_ads_recharge_operator_service"),
    ShopeeAdsSite(code="ph", name="菲律宾", label="菲律宾", module_name="main.shopee.ph_shopee_ads_recharge_operator_service"),
    ShopeeAdsSite(code="vn", name="越南", label="越南", module_name="main.shopee.vn_shopee_ads_recharge_operator_service"),
    ShopeeAdsSite(code="my", name="马来", label="马来", module_name="main.shopee.my_shopee_ads_recharge_operator_service"),
)

SITE_BY_CODE: dict[str, ShopeeAdsSite] = {s.code: s for s in SITE_OPTIONS}

# 统一的常量（所有站点相同）
MIN_CONCURRENT_STORES = 1
MAX_CONCURRENT_STORES = 4
DEFAULT_MAX_CONCURRENT_STORES = 2
MIN_PAYMENT_WAIT_SECONDS = 30
MAX_PAYMENT_WAIT_SECONDS = 300
DEFAULT_PAYMENT_WAIT_SECONDS = 60

# superbrowser_process 项目根目录（相对于本项目的上一级）
_SUPERBROWSER_ROOT: str | None = None


def _ensure_superbrowser_path() -> None:
    """将 superbrowser_process 加入 sys.path 以便动态 import。"""
    global _SUPERBROWSER_ROOT
    if _SUPERBROWSER_ROOT is not None:
        return

    # PyInstaller 会将这些模块放进自己的模块归档，不需要外部源码目录。
    # 不要用 find_spec("main.shopee") 探测：开发环境直接运行 launcher/main.py
    # 时，它会先把启动文件误导入成非包模块 "main"，污染 sys.modules。
    if getattr(sys, "frozen", False):
        _SUPERBROWSER_ROOT = "<bundled>"
        return

    # 开发环境允许显式配置路径，默认仍使用相邻项目目录。
    project_root = Path(__file__).resolve().parents[2]
    candidates = [
        Path(os.environ["SUPERBROWSER_PROCESS_ROOT"]).expanduser()
        if os.environ.get("SUPERBROWSER_PROCESS_ROOT")
        else None,
        project_root.parent / "superbrowser_process",
    ]
    candidate = next((path.resolve() for path in candidates if path and (path / "main").is_dir()), None)
    if candidate is None:
        searched = ", ".join(str(path) for path in candidates if path)
        raise ModuleNotFoundError(f"未找到 superbrowser_process（已检查：{searched}）")

    _SUPERBROWSER_ROOT = str(candidate)
    if _SUPERBROWSER_ROOT not in sys.path:
        sys.path.insert(0, _SUPERBROWSER_ROOT)


def _get_site(code: str) -> ShopeeAdsSite:
    site = SITE_BY_CODE.get(code)
    if site is None:
        raise ValueError(f"不支持的 Shopee 站点：{code}")
    return site


def get_site_module(code: str) -> ModuleType:
    _ensure_superbrowser_path()
    site = _get_site(code)
    return importlib.import_module(site.module_name)


# ---------------------------------------------------------------------------
# 查询数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShopeeAdsRechargeQuery:
    site_code: str
    company: str
    username: str
    password: str
    store_names: tuple[str, ...]
    recharge_mode: str = "sheet"
    custom_recharge_items: tuple[dict[str, str], ...] = ()
    payment_wait_seconds: int = DEFAULT_PAYMENT_WAIT_SECONDS
    max_concurrent_stores: int = DEFAULT_MAX_CONCURRENT_STORES
    browser_window_mode: str = "normal"
    sync_database: bool = True
    client_path: str = ""
    webdriver_path: str = ""


@dataclass(frozen=True)
class ShopeeAdsRechargeResult:
    success: bool
    message: str
    summary: str = ""
    record_file: str = ""
    log_file: str = ""
    matched_store_count: int = 0
    processed_store_count: int = 0


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        v = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    if v < lo:
        return default
    if v > hi:
        return hi
    return v


def parse_store_names(value: Any) -> list[str]:
    """解析店铺名称输入（文本或列表）→ 去重去空。"""
    if isinstance(value, (list, tuple)):
        return [str(s).strip() for s in value if str(s).strip()]
    text = str(value or "")
    for ch in ("，", ","):
        text = text.replace(ch, "\n")
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_custom_recharge_items(value: Any) -> list[dict[str, str]]:
    """解析自定义充值条目（文本或列表）。"""
    if isinstance(value, (list, tuple)):
        items: list[dict[str, str]] = []
        for item in value:
            if isinstance(item, dict):
                sn = str(item.get("store_name") or "").strip()
                am = str(item.get("amount") or "").strip()
                if sn and am:
                    items.append({"store_name": sn, "amount": am})
        return items
    text = str(value or "").strip()
    if not text:
        return []
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for sep in ("\t", "，", ","):
            if sep in line:
                parts = line.split(sep, 1)
                break
        else:
            parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        sn = parts[0].strip()
        am = parts[1].strip()
        if sn and am:
            try:
                float(am)
            except ValueError:
                continue
            items.append({"store_name": sn, "amount": am})
    return items


# ---------------------------------------------------------------------------
# 验证
# ---------------------------------------------------------------------------

def validate_shopee_ads_payload(payload: dict[str, Any]) -> ShopeeAdsRechargeQuery:
    """校验前端传来的充值参数，返回不可变 Query 对象。"""
    site_code = str(payload.get("site_code") or "").strip()
    if not site_code or site_code not in SITE_BY_CODE:
        raise ValueError(f"请选择充值站点")

    company = str(payload.get("company") or "").strip()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username:
        raise ValueError("请输入紫鸟账号")
    if not password:
        raise ValueError("请输入紫鸟密码")

    recharge_mode = str(payload.get("recharge_mode") or "sheet").strip()
    if recharge_mode not in ("sheet", "custom"):
        recharge_mode = "sheet"

    raw_stores = payload.get("store_names")
    raw_custom = payload.get("custom_recharge_items")
    if recharge_mode == "custom":
        custom_items = parse_custom_recharge_items(raw_custom or raw_stores)
        if not custom_items:
            raise ValueError("自定义模式下请输入店铺名称和金额，每行一个")
        store_names = tuple(item["store_name"] for item in custom_items)
    else:
        store_list = parse_store_names(raw_stores)
        if not store_list:
            raise ValueError("请输入店铺名称，每行一个")
        store_names = tuple(store_list)
        custom_items = parse_custom_recharge_items(raw_custom) if raw_custom else []

    client_path = str(payload.get("client_path") or "").strip()
    webdriver_path = str(payload.get("webdriver_path") or "").strip()

    payment_wait = _clamp_int(
        payload.get("payment_wait_seconds"),
        DEFAULT_PAYMENT_WAIT_SECONDS,
        MIN_PAYMENT_WAIT_SECONDS,
        MAX_PAYMENT_WAIT_SECONDS,
    )
    max_concurrent = _clamp_int(
        payload.get("max_concurrent_stores"),
        DEFAULT_MAX_CONCURRENT_STORES,
        MIN_CONCURRENT_STORES,
        MAX_CONCURRENT_STORES,
    )

    browser_window_mode = str(payload.get("browser_window_mode") or "normal").strip()
    if browser_window_mode not in ("normal", "background"):
        browser_window_mode = "normal"

    sync_database = bool(payload.get("sync_database", True))

    return ShopeeAdsRechargeQuery(
        site_code=site_code,
        company=company,
        username=username,
        password=password,
        store_names=store_names,
        recharge_mode=recharge_mode,
        custom_recharge_items=tuple(custom_items),
        payment_wait_seconds=payment_wait,
        max_concurrent_stores=max_concurrent,
        browser_window_mode=browser_window_mode,
        sync_database=sync_database,
        client_path=client_path,
        webdriver_path=webdriver_path,
    )


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def run_shopee_ads_recharge(
    job: ShopeeAdsRechargeQuery,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """执行 Shopee 广告充值任务。返回结果 dict。"""
    _emit(progress, f"正在加载 {SITE_BY_CODE[job.site_code].name} 站点模块...")

    try:
        module = get_site_module(job.site_code)
    except ImportError as exc:
        raise RuntimeError(
            f"无法加载站点模块，请确认 superbrowser_process 项目在正确位置：{exc}"
        ) from exc

    _emit(progress, f"正在构建运行配置...")

    # 构建 AdsRechargeSettings
    settings = module.AdsRechargeSettings(
        company=job.company,
        username=job.username,
        password=job.password,
        store_names=list(job.store_names),
        client_path=job.client_path,
        webdriver_path=job.webdriver_path,
        sync_database=job.sync_database,
        recharge_mode=job.recharge_mode,
        custom_recharge_amount=job.custom_recharge_items[0]["amount"] if job.custom_recharge_items else "",
        custom_recharge_items=[dict(item) for item in job.custom_recharge_items],
        max_concurrent_stores=job.max_concurrent_stores,
        payment_wait_seconds=job.payment_wait_seconds,
        browser_window_mode=job.browser_window_mode,
        send_notifications=False,
        enable_quota_limit=False,
    )

    # 如果没有传入路径，尝试自动解析
    if not settings.client_path or not settings.webdriver_path:
        try:
            from util.ziniao_runtime_util import resolve_webdriver_path, resolve_ziniao_client_path

            _, cfg, _ = module.load_runtime_configs()
            browser_cfg = cfg.get("ziniao", {}).get("browser", {})
            if not settings.client_path:
                settings.client_path = resolve_ziniao_client_path(
                    browser_cfg.get("client_path"), browser_cfg.get("version", "v6")
                )
            if not settings.webdriver_path:
                settings.webdriver_path = resolve_webdriver_path(
                    browser_cfg.get("webdriver_path")
                )
        except Exception:
            pass

    _emit(progress, f"正在启动 {SITE_BY_CODE[job.site_code].name} 广告充值...")
    _emit(progress, f"账号：{job.username} | 店铺数：{len(job.store_names)} | 并发：{job.max_concurrent_stores}")

    result = module.run_ads_recharge(settings=settings, log_callback=lambda msg: _emit(progress, str(msg)))

    return {
        "success": result.success,
        "message": result.message,
        "summary": result.summary,
        "record_file": result.record_file,
        "log_file": result.log_file,
        "matched_store_count": result.matched_store_count,
        "processed_store_count": result.processed_store_count,
    }


# ---------------------------------------------------------------------------
# 站点信息（供前端下拉框使用）
# ---------------------------------------------------------------------------

def get_site_options_list() -> list[dict[str, str]]:
    return [{"code": s.code, "name": s.name, "label": s.label} for s in SITE_OPTIONS]


def resolve_runtime_paths(site_code: str) -> dict[str, str]:
    """解析紫鸟客户端和驱动路径。"""
    try:
        module = get_site_module(site_code)
        _, cfg, _ = module.load_runtime_configs()
        browser_cfg = cfg.get("ziniao", {}).get("browser", {})
    except Exception:
        browser_cfg = {}

    try:
        from util.ziniao_runtime_util import resolve_webdriver_path, resolve_ziniao_client_path

        client_path = resolve_ziniao_client_path(
            browser_cfg.get("client_path"), browser_cfg.get("version", "v6")
        )
        driver_path = resolve_webdriver_path(browser_cfg.get("webdriver_path"))
    except Exception:
        client_path = ""
        driver_path = ""

    return {"client_path": client_path, "webdriver_path": driver_path}


def save_site_config(site_code: str, payload: dict[str, Any]) -> str:
    """保存站点充值配置到 superbrowser_process 的 settings.json。返回保存路径。"""
    _ensure_superbrowser_path()
    module = get_site_module(site_code)

    # 构建 settings 对象
    custom_items = parse_custom_recharge_items(payload.get("custom_recharge_items") or payload.get("store_names"))
    recharge_mode = str(payload.get("recharge_mode") or "sheet").strip()
    if recharge_mode == "custom" and custom_items:
        store_names = [item["store_name"] for item in custom_items]
    else:
        store_names = parse_store_names(payload.get("store_names"))

    settings = module.AdsRechargeSettings(
        company=str(payload.get("company") or "").strip(),
        username=str(payload.get("username") or "").strip(),
        password=str(payload.get("password") or ""),
        store_names=store_names,
        client_path=str(payload.get("client_path") or "").strip(),
        webdriver_path=str(payload.get("webdriver_path") or "").strip(),
        sync_database=bool(payload.get("sync_database", True)),
        recharge_mode=recharge_mode,
        custom_recharge_amount=custom_items[0]["amount"] if custom_items else "",
        custom_recharge_items=[dict(item) for item in custom_items],
        max_concurrent_stores=_clamp_int(
            payload.get("max_concurrent_stores"),
            DEFAULT_MAX_CONCURRENT_STORES, MIN_CONCURRENT_STORES, MAX_CONCURRENT_STORES,
        ),
        payment_wait_seconds=_clamp_int(
            payload.get("payment_wait_seconds"),
            DEFAULT_PAYMENT_WAIT_SECONDS, MIN_PAYMENT_WAIT_SECONDS, MAX_PAYMENT_WAIT_SECONDS,
        ),
        browser_window_mode=str(payload.get("browser_window_mode") or "normal").strip(),
        send_notifications=False,
        enable_quota_limit=False,
    )

    return module.save_settings_to_config(settings)
