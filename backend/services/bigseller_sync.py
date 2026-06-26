from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import yaml


ProgressCallback = Callable[[str], None]

DEFAULT_PAGE_SIZE = 300
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_PLATFORM = "shopee"
BIGSELLER_REQUEST_UTIL_MODULE_NAME = "_hqyl_mabang_bigseller_request_util"
_BIGSELLER_REQUEST_UTIL_LOAD_LOCK = threading.Lock()

SYNC_TYPE_LABELS = {
    "product": "同步产品",
    "inventory": "同步库存",
}

LISTING_STATUS_LABELS = {
    "live": "在售",
    "soldOut": "售完",
}


@dataclass(frozen=True)
class BigSellerSyncJob:
    username: str
    password: str
    sync_type: str
    listing_status: str
    output_dir: Path
    platform: str = DEFAULT_PLATFORM
    page_size: int = DEFAULT_PAGE_SIZE
    max_rounds: int = 0
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    poll_interval_seconds: int = 2
    captcha_username: str = ""
    captcha_password: str = ""


@dataclass(frozen=True)
class BigSellerSyncResult:
    processed_count: int
    success_count: int
    failed_count: int
    round_count: int
    output_file: Path
    output_dir: Path


class ProgressLogger:
    def __init__(self, progress: ProgressCallback) -> None:
        self.progress = progress

    def info(self, message: str) -> None:
        self.progress(str(message))

    def warning(self, message: str) -> None:
        self.progress(f"警告：{message}")

    def error(self, message: str) -> None:
        self.progress(f"错误：{message}")


def safe_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except Exception:
        match = re.search(r"\d+", str(value or ""))
        return int(match.group(0)) if match else default


def safe_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def normalize_listing_status(value: object) -> str:
    text = safe_text(value) or "live"
    if text.lower() in {"active", "live"}:
        return "live"
    if text.lower() in {"soldout", "sold_out", "sold-out"}:
        return "soldOut"
    if text not in LISTING_STATUS_LABELS:
        raise ValueError("请选择在售或售完")
    return text


def validate_bigseller_sync_payload(payload: dict[str, Any]) -> BigSellerSyncJob:
    username = safe_text(payload.get("username"))
    password = safe_text(payload.get("password"))
    sync_type = safe_text(payload.get("sync_type") or "inventory").lower()
    listing_status = normalize_listing_status(payload.get("listing_status") or "live")
    output_dir = Path(safe_text(payload.get("output_dir"))).expanduser()
    platform = safe_text(payload.get("platform") or DEFAULT_PLATFORM).lower()

    if not username:
        raise ValueError("请输入 BigSeller 账号")
    if not password:
        raise ValueError("请输入 BigSeller 密码")
    if sync_type not in SYNC_TYPE_LABELS:
        raise ValueError("请选择同步产品或同步库存")
    if platform != "shopee":
        raise ValueError("当前桌面端 BigSeller 同步先支持 Shopee")
    if not output_dir:
        raise ValueError("请选择输出目录")

    page_size = max(1, min(safe_int(payload.get("page_size"), DEFAULT_PAGE_SIZE), DEFAULT_PAGE_SIZE))
    max_rounds = max(0, safe_int(payload.get("max_rounds"), 0))
    timeout_seconds = max(60, safe_int(payload.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS))
    poll_interval_seconds = max(1, safe_int(payload.get("poll_interval_seconds"), 2))
    return BigSellerSyncJob(
        username=username,
        password=password,
        sync_type=sync_type,
        listing_status=listing_status,
        output_dir=output_dir,
        platform=platform,
        page_size=page_size,
        max_rounds=max_rounds,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        captcha_username=safe_text(payload.get("captcha_username")),
        captcha_password=str(payload.get("captcha_password") or ""),
    )


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


def mabang_process_root() -> Path:
    env_root = safe_text(os.getenv("HQYL_MABANG_PROCESS_ROOT"))
    if env_root:
        return Path(env_root).expanduser()
    frozen_root = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else None
    if frozen_root and (frozen_root / "mabang_process").exists():
        return frozen_root / "mabang_process"
    return workspace_root() / "mabang_process"


def load_bigseller_request_util():
    root = mabang_process_root()
    if not root.exists():
        raise RuntimeError(f"未找到 mabang_process，无法加载 BigSeller 登录组件: {root}")

    module_path = root / "util" / "bigseller_request_util.py"
    if not module_path.is_file():
        raise RuntimeError(f"未找到 BigSeller 登录组件: {module_path}")

    resolved_module_path = module_path.resolve()
    with _BIGSELLER_REQUEST_UTIL_LOAD_LOCK:
        loaded_module = sys.modules.get(BIGSELLER_REQUEST_UTIL_MODULE_NAME)
        loaded_path = getattr(loaded_module, "__file__", None)
        if loaded_path and Path(loaded_path).resolve() == resolved_module_path:
            return loaded_module

        spec = importlib.util.spec_from_file_location(
            BIGSELLER_REQUEST_UTIL_MODULE_NAME,
            resolved_module_path,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法创建 BigSeller 登录组件加载器: {resolved_module_path}")

        module = importlib.util.module_from_spec(spec)
        previous_module = sys.modules.get(BIGSELLER_REQUEST_UTIL_MODULE_NAME)
        sys.modules[BIGSELLER_REQUEST_UTIL_MODULE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            if previous_module is None:
                sys.modules.pop(BIGSELLER_REQUEST_UTIL_MODULE_NAME, None)
            else:
                sys.modules[BIGSELLER_REQUEST_UTIL_MODULE_NAME] = previous_module
            raise
        return module


def load_reference_bigseller_config() -> dict[str, Any]:
    explicit = safe_text(os.getenv("HQYL_BIGSELLER_CONFIG"))
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    root = mabang_process_root()
    candidates.extend(
        [
            root / "vietnam" / "config" / "BigSeller库存同步.yaml",
            root / "philippines" / "config" / "BigSeller同步产品.yaml",
        ]
    )
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            process_config = data.get("process_config") or {}
            if isinstance(process_config, dict):
                return process_config
        except Exception:
            continue
    return {}


def build_process_config(job: BigSellerSyncJob) -> dict[str, Any]:
    base_config = load_reference_bigseller_config()
    captcha_config = dict(base_config.get("captcha_service") or {})

    if job.captcha_username:
        captcha_config["username"] = job.captcha_username
    if job.captcha_password:
        captcha_config["password"] = job.captcha_password

    env_captcha_username = safe_text(os.getenv("HQYL_TTSHITU_USERNAME"))
    env_captcha_password = safe_text(os.getenv("HQYL_TTSHITU_PASSWORD"))
    if env_captcha_username:
        captcha_config["username"] = env_captcha_username
    if env_captcha_password:
        captcha_config["password"] = env_captcha_password
    captcha_config.setdefault("provider", "ttshitu")
    captcha_config.setdefault("url", "http://api.ttshitu.com/predict")
    captcha_config.setdefault("type_name", "数英混合识别-增强")
    captcha_config.setdefault("typeid", 1003)
    captcha_config.setdefault("max_attempts", 8)

    if not safe_text(captcha_config.get("username")) or not safe_text(captcha_config.get("password")):
        raise ValueError("BigSeller 验证码服务未配置，请配置 HQYL_TTSHITU_USERNAME/HQYL_TTSHITU_PASSWORD 或复用现有流程配置")

    return {
        "process_name": "BigSeller同步",
        "user_agent": base_config.get("user_agent"),
        "request": base_config.get("request") or {},
        "captcha_service": captcha_config,
        "bigseller_account": {
            "username": job.username,
            "password": job.password,
        },
    }


def api_headers(util: Any, process_config: dict[str, Any], platform: str, content_type: str | None = None) -> dict[str, str]:
    user_agent = util.get_request_user_agent(process_config)
    return util.build_request_headers(
        user_agent,
        referer=util.get_listing_page_url(platform),
        content_type=content_type,
    )


def parse_api_json(response: Any, description: str, *, check_code: bool = True) -> dict[str, Any]:
    response.raise_for_status()
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"{description} 返回不是 JSON: {response.text[:500]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{description} 返回格式异常: {data}")
    if check_code and data.get("code") not in (None, 0, "0"):
        raise RuntimeError(f"{description} 失败: {data}")
    return data


def listing_query(job: BigSellerSyncJob, page_no: int = 1) -> dict[str, Any]:
    return {
        "isTrim": "false",
        "orderBy": "update_time",
        "desc": "false",
        "timeType": "create_time",
        "startDateStr": "",
        "endDateStr": "",
        "searchType": "productName",
        "inquireType": 0,
        "shopeeStatus": job.listing_status,
        "status": "active",
        "pageNo": page_no,
        "pageSize": job.page_size,
    }


def count_query(query: dict[str, Any]) -> dict[str, Any]:
    payload = dict(query)
    payload.pop("isTrim", None)
    if str(payload.get("desc")).lower() in {"true", "false"}:
        payload["desc"] = str(payload["desc"]).lower() == "true"
    return payload


def find_nested_value(obj: Any, keys: tuple[str, ...], max_depth: int = 5) -> Any:
    if max_depth < 0:
        return None
    if isinstance(obj, dict):
        for key in keys:
            if key in obj and obj.get(key) not in (None, ""):
                return obj.get(key)
        for value in obj.values():
            found = find_nested_value(value, keys, max_depth - 1)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_nested_value(item, keys, max_depth - 1)
            if found not in (None, ""):
                return found
    return None


def find_nested_rows(obj: Any, max_depth: int = 5) -> list[dict[str, Any]]:
    if max_depth < 0:
        return []
    if isinstance(obj, list):
        return [item for item in obj if isinstance(item, dict)]
    if not isinstance(obj, dict):
        return []
    for key in ("rows", "records", "list", "items", "dataList", "pageList"):
        value = obj.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for value in obj.values():
        rows = find_nested_rows(value, max_depth - 1)
        if rows:
            return rows
    return []


def listing_total(payload: dict[str, Any]) -> int:
    value = find_nested_value(payload, ("totalSize", "totalCount", "totalElements", "recordsTotal", "allCount", "total"))
    return safe_int(value, 0)


def count_total(payload: dict[str, Any]) -> int:
    data = payload.get("data")
    if isinstance(data, int):
        return data
    if isinstance(data, str) and data.isdigit():
        return int(data)
    direct = find_nested_value(data, ("activeCount", "count", "num", "total", "totalCount", "totalSize"), max_depth=5)
    return safe_int(direct, 0)


def row_product_id(row: dict[str, Any]) -> str:
    for key in ("id", "productId", "listingId", "product_id", "listing_id"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def request_listing_page(session: Any, util: Any, process_config: dict[str, Any], job: BigSellerSyncJob, page_no: int = 1) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    query = listing_query(job, page_no=page_no)
    response = session.get(
        util.build_api_url(f"/v1/product/listing/{job.platform}/active.json"),
        params=query,
        headers=api_headers(util, process_config, job.platform),
        timeout=60,
    )
    payload = parse_api_json(response, "获取 BigSeller 产品列表")
    rows = find_nested_rows(payload)
    total = listing_total(payload) or len(rows)
    return rows, total, payload


def request_listing_count(session: Any, util: Any, process_config: dict[str, Any], job: BigSellerSyncJob) -> int:
    response = session.post(
        util.build_api_url(f"/v1/product/listing/{job.platform}/count.json"),
        json=count_query(listing_query(job)),
        headers=api_headers(util, process_config, job.platform, content_type="application/json"),
        timeout=60,
    )
    payload = parse_api_json(response, "获取 BigSeller 产品数量")
    return count_total(payload)


def extract_sync_key(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    if isinstance(data, str) and data.strip():
        return data.strip()
    if isinstance(data, dict):
        for key in ("key", "syncKey", "processKey", "redisKey", "taskKey"):
            value = data.get(key)
            if value not in (None, ""):
                return str(value).strip()
    for key in ("key", "syncKey", "processKey", "redisKey", "taskKey"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value).strip()
    found = find_nested_value(payload, ("key", "syncKey", "processKey", "redisKey", "taskKey"), max_depth=5)
    return str(found).strip() if found not in (None, "") else ""


def wait_new_listing_sync_key(
    util: Any,
    session: Any,
    process_config: dict[str, Any],
    platform: str,
    old_key: object,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> str:
    end_at = time.time() + timeout_seconds
    while time.time() < end_at:
        key = util.get_listing_sync_key(session, platform=platform, process_config=process_config)
        if key not in (None, "", -1, "-1") and key != old_key:
            return str(key)
        time.sleep(poll_interval_seconds)
    return ""


def process_containers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    containers: list[dict[str, Any]] = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        containers.append(data)
        info_map = data.get("infoMap")
        if isinstance(info_map, dict):
            containers.extend(item for item in info_map.values() if isinstance(item, dict))
    info_map = payload.get("infoMap")
    if isinstance(info_map, dict):
        containers.extend(item for item in info_map.values() if isinstance(item, dict))
    return containers


def int_from_container(container: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        if key in container and container.get(key) not in (None, ""):
            return safe_int(container.get(key), 0)
    return 0


def process_counts(payload: dict[str, Any], fallback_total: int = 0) -> dict[str, int]:
    total = processed = success = failed = 0
    for container in process_containers(payload):
        total = max(total, int_from_container(container, ("totalNum", "totalCount", "allNum", "allCount", "total")))
        processed = max(processed, int_from_container(container, ("num", "handleNum", "processedNum", "currentNum")))
        success = max(success, int_from_container(container, ("successNum", "successCount", "success")))
        failed = max(failed, int_from_container(container, ("failNum", "failedNum", "failCount", "errorNum")))
    if not processed and (success or failed):
        processed = success + failed
    if not total:
        total = fallback_total
    return {"total": total, "processed": processed, "success": success, "failed": failed}


def process_state_code(payload: dict[str, Any]) -> int | str | None:
    data = payload.get("data")
    if isinstance(data, dict) and data.get("code") not in (None, ""):
        return safe_int(data.get("code"), 0)
    if payload.get("processCode") not in (None, ""):
        return safe_int(payload.get("processCode"), 0)
    if payload.get("status") not in (None, ""):
        return safe_text(payload.get("status")).lower()
    if payload.get("code") not in (None, ""):
        return safe_int(payload.get("code"), 0)
    return None


def is_check_process_finished(payload: dict[str, Any], fallback_total: int) -> bool:
    counts = process_counts(payload, fallback_total=fallback_total)
    code = process_state_code(payload)
    if isinstance(code, int) and code not in (0, -1):
        return True
    if isinstance(code, str) and code in {"success", "done", "finished", "complete", "completed", "fail", "failed"}:
        return True
    if counts["total"] and counts["processed"] >= counts["total"]:
        return True
    if counts["total"] and counts["success"] + counts["failed"] >= counts["total"]:
        return True
    return False


def wait_sync_by_ids_result(
    session: Any,
    util: Any,
    process_config: dict[str, Any],
    job: BigSellerSyncJob,
    sync_key: str,
    selected_count: int,
    progress: ProgressCallback,
) -> dict[str, Any]:
    if not sync_key:
        return {"code": 0, "msg": "syncByIds 未返回 key，已按触发成功处理", "data": {}}
    end_at = time.time() + job.timeout_seconds
    last_result: dict[str, Any] = {}
    while time.time() < end_at:
        response = session.post(
            util.build_api_url("/v1/process/checkProcess.json"),
            data={"key": sync_key, "type": "syncProduct"},
            headers=api_headers(
                util,
                process_config,
                job.platform,
                content_type="application/x-www-form-urlencoded; charset=UTF-8",
            ),
            timeout=60,
        )
        last_result = parse_api_json(response, "查询 BigSeller 同步进度", check_code=False)
        counts = process_counts(last_result, fallback_total=selected_count)
        if is_check_process_finished(last_result, fallback_total=selected_count):
            progress(f"同步完成，key={sync_key}，进度 {counts['processed']}/{counts['total']}，成功 {counts['success']}，失败 {counts['failed']}")
            return last_result
        progress(f"同步处理中，key={sync_key}，进度 {counts['processed']}/{counts['total']}")
        time.sleep(job.poll_interval_seconds)
    raise TimeoutError(f"等待 BigSeller 同步超时，key={sync_key}，最后结果: {last_result}")


def sync_inventory_by_listing_ids(
    session: Any,
    util: Any,
    process_config: dict[str, Any],
    job: BigSellerSyncJob,
    progress: ProgressCallback,
) -> tuple[list[dict[str, Any]], int, int, int, int]:
    rows, listing_size, _ = request_listing_page(session, util, process_config, job, page_no=1)
    try:
        total = request_listing_count(session, util, process_config, job) or listing_size
    except Exception as exc:
        progress(f"获取数量接口失败，改用列表数量：{exc}")
        total = listing_size
    planned_rounds = (total + job.page_size - 1) // job.page_size if total else 0
    if job.max_rounds:
        planned_rounds = min(planned_rounds or job.max_rounds, job.max_rounds)
    if not planned_rounds and rows:
        planned_rounds = 1

    progress(f"{LISTING_STATUS_LABELS[job.listing_status]}库存同步：总数 {total}，每轮 {job.page_size}，计划 {planned_rounds} 轮")
    output_rows: list[dict[str, Any]] = []
    processed_count = success_count = failed_count = 0
    for round_no in range(1, planned_rounds + 1):
        page_rows, current_total, _ = request_listing_page(session, util, process_config, job, page_no=1)
        product_ids = list(dict.fromkeys(row_product_id(row) for row in page_rows[: job.page_size] if row_product_id(row)))
        selected_count = len(product_ids)
        if not selected_count:
            progress(f"第 {round_no}/{planned_rounds} 轮第一页无可同步产品，提前结束")
            break
        old_key = None
        try:
            old_key = util.get_listing_sync_key(session, platform=job.platform, process_config=process_config)
        except Exception:
            old_key = None
        progress(f"第 {round_no}/{planned_rounds} 轮提交 {selected_count} 个产品")
        response = session.post(
            util.build_api_url(f"/v1/product/listing/{job.platform}/syncByIds.json"),
            data={"ids": ",".join(product_ids)},
            headers=api_headers(
                util,
                process_config,
                job.platform,
                content_type="application/x-www-form-urlencoded; charset=UTF-8",
            ),
            timeout=60,
        )
        trigger_result = parse_api_json(response, "触发 BigSeller 库存同步")
        sync_key = extract_sync_key(trigger_result)
        if not sync_key:
            sync_key = wait_new_listing_sync_key(
                util,
                session,
                process_config,
                job.platform,
                old_key,
                timeout_seconds=60,
                poll_interval_seconds=job.poll_interval_seconds,
            )
        final_result = wait_sync_by_ids_result(session, util, process_config, job, sync_key, selected_count, progress)
        counts = process_counts(final_result, fallback_total=selected_count)
        processed_count += selected_count
        if counts["success"] or counts["failed"]:
            success_count += counts["success"]
            failed_count += counts["failed"]
        else:
            success_count += selected_count
        output_rows.append(
            {
                "同步类型": SYNC_TYPE_LABELS[job.sync_type],
                "商品状态": LISTING_STATUS_LABELS[job.listing_status],
                "状态值": job.listing_status,
                "轮次": round_no,
                "计划轮数": planned_rounds,
                "本轮列表总数": current_total,
                "本轮提交数": selected_count,
                "同步Key": sync_key,
                "进度总数": counts["total"],
                "进度已处理": counts["processed"],
                "成功": counts["success"],
                "失败": counts["failed"],
                "触发结果": json.dumps(trigger_result, ensure_ascii=False, default=str),
                "最终结果": json.dumps(final_result, ensure_ascii=False, default=str),
                "执行时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return output_rows, processed_count, success_count, failed_count, planned_rounds


def sync_products(
    session: Any,
    util: Any,
    process_config: dict[str, Any],
    job: BigSellerSyncJob,
    progress: ProgressCallback,
) -> tuple[list[dict[str, Any]], int, int, int, int]:
    try:
        total = request_listing_count(session, util, process_config, job)
    except Exception:
        _rows, total, _payload = request_listing_page(session, util, process_config, job, page_no=1)

    old_key = None
    try:
        old_key = util.get_listing_sync_key(session, platform=job.platform, process_config=process_config)
    except Exception:
        old_key = None
    query = listing_query(job, page_no=1)
    query.pop("isTrim", None)
    progress(f"{LISTING_STATUS_LABELS[job.listing_status]}产品同步：调用 BigSeller 产品同步接口，当前状态数量约 {total}")
    response = session.get(
        util.build_api_url(f"/v1/product/listing/{job.platform}/sync.json"),
        params=query,
        headers=api_headers(util, process_config, job.platform),
        timeout=60,
    )
    trigger_result = parse_api_json(response, "触发 BigSeller 产品同步")
    sync_key = extract_sync_key(trigger_result)
    if not sync_key:
        sync_key = wait_new_listing_sync_key(
            util,
            session,
            process_config,
            job.platform,
            old_key,
            timeout_seconds=60,
            poll_interval_seconds=job.poll_interval_seconds,
        )
    if sync_key:
        progress(f"已提交产品同步，key={sync_key}，等待完成")
        final_result = util.wait_listing_sync_result(
            session,
            sync_key,
            platform=job.platform,
            process_config=process_config,
            poll_interval_seconds=job.poll_interval_seconds,
            timeout_seconds=job.timeout_seconds,
            logger=ProgressLogger(progress),
        )
    else:
        final_result = {"code": 0, "msg": "产品同步已触发，但未返回同步 key", "data": {}}
    counts = process_counts(final_result, fallback_total=total)
    processed = counts["processed"] or total
    success = counts["success"] or processed
    failed = counts["failed"]
    output_rows = [
        {
            "同步类型": SYNC_TYPE_LABELS[job.sync_type],
            "商品状态": LISTING_STATUS_LABELS[job.listing_status],
            "状态值": job.listing_status,
            "轮次": 1,
            "计划轮数": 1,
            "本轮列表总数": total,
            "本轮提交数": processed,
            "同步Key": sync_key,
            "进度总数": counts["total"],
            "进度已处理": counts["processed"],
            "成功": counts["success"],
            "失败": counts["failed"],
            "触发结果": json.dumps(trigger_result, ensure_ascii=False, default=str),
            "最终结果": json.dumps(final_result, ensure_ascii=False, default=str),
            "执行时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    ]
    return output_rows, processed, success, failed, 1


def export_rows(job: BigSellerSyncJob, rows: list[dict[str, Any]]) -> Path:
    job.output_dir.mkdir(parents=True, exist_ok=True)
    safe_user = re.sub(r'[<>:"/\\|?*]+', "_", job.username)
    filename = (
        f"BigSeller{SYNC_TYPE_LABELS[job.sync_type]}_"
        f"{LISTING_STATUS_LABELS[job.listing_status]}_{safe_user}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    )
    output_file = job.output_dir / filename
    pd.DataFrame(rows).to_excel(output_file, index=False)
    return output_file


def run_bigseller_sync(job: BigSellerSyncJob, progress: ProgressCallback) -> BigSellerSyncResult:
    util = load_bigseller_request_util()
    process_config = build_process_config(job)
    progress(f"开始登录 BigSeller：{job.username}")
    session = util.login_bigseller_by_request(process_config, logger=ProgressLogger(progress))
    progress("BigSeller 登录成功")

    if job.sync_type == "product":
        rows, processed, success, failed, rounds = sync_products(session, util, process_config, job, progress)
    else:
        rows, processed, success, failed, rounds = sync_inventory_by_listing_ids(session, util, process_config, job, progress)

    output_file = export_rows(job, rows)
    progress(f"结果已导出：{output_file}")
    return BigSellerSyncResult(
        processed_count=processed,
        success_count=success,
        failed_count=failed,
        round_count=rounds,
        output_file=output_file,
        output_dir=output_file.parent,
    )
