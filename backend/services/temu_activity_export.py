"""Batch export of the TEMU activity registration template.

One store produces one template; the operator-selected sites are a filter on the
platform page and are re-checked against the downloaded file.  Nothing here submits
or changes a registration, so a failed store can simply be run again.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from backend.services.temu_on_sale_export import (
    batch_lock,
    is_temu,
    parse_names,
    write_json,
)


SCHEMA = "temu_activity_export_v1"
SITES = {"PH": "菲律宾", "MY": "马来西亚", "TH": "泰国"}
TERMINAL = {"success"}
BEIJING = timezone(timedelta(hours=8))
MAX_STORES = 200


def beijing_today() -> str:
    return datetime.now(BEIJING).date().isoformat()


def parse_sites(value) -> list[str]:
    values = value.splitlines() if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("请至少勾选一个站点")
    codes = []
    for item in values:
        code = str(item or "").strip().upper()
        if code not in SITES:
            raise ValueError(f"不支持的站点：{item}（仅支持菲律宾、马来西亚、泰国）")
        if code not in codes:
            codes.append(code)
    if len(codes) != len(values):
        raise ValueError("站点不能重复")
    return codes


def validate_request(payload: dict) -> dict:
    names = parse_names(payload.get("store_names"))
    if len(names) > MAX_STORES:
        raise ValueError(f"单批最多支持 {MAX_STORES} 个店铺")
    sites = parse_sites(payload.get("sites"))
    raw_date = str(payload.get("business_date") or "").strip() or beijing_today()
    try:
        business_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("运行日期格式应为 YYYY-MM-DD") from None
    today = datetime.now(BEIJING).date()
    if business_date > today:
        raise ValueError("运行日期不能晚于北京时间当天")
    try:
        earliest = today.replace(year=today.year - 1)
    except ValueError:  # 2 月 29 日：上一年没有这一天
        earliest = today.replace(year=today.year - 1, day=28)
    if business_date < earliest:
        raise ValueError("运行日期不能早于一年前")
    return {"store_names": names, "sites": sites, "business_date": business_date.isoformat()}


def resolve_stores(names: list[str], catalog: list[dict]) -> dict:
    """Resolve a complete store name first, or a uniquely contained partial name."""
    selected, issues, used = [], [], set()
    for name in names:
        key = name.casefold()
        exact = [store for store in catalog if str(store.get("store_name") or "").strip().casefold() == key]
        candidates = exact or [
            store for store in catalog
            if key in str(store.get("store_name") or "").strip().casefold()
        ]
        by_id = {str(store.get("store_id") or ""): store for store in candidates}
        if len(by_id) != 1 or "" in by_id:
            issues.append({
                "name": name,
                "error": "未匹配到店铺" if not by_id else "匹配到多个店铺，请输入更完整的名称",
            })
            continue
        store_id, store = next(iter(by_id.items()))
        if not is_temu(store):
            issues.append({"name": name, "error": "紫鸟平台信息未确认是 TEMU"})
            continue
        if store_id in used:
            issues.append({"name": name, "error": "多个输入匹配到同一店铺，请删除重复项"})
            continue
        selected.append(copy.deepcopy(store))
        used.add(store_id)
    return {"stores": selected, "issues": issues, "can_start": bool(selected) and not issues}


def safe_slug(name: str, limit: int = 40) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "")).strip(" .")[:limit] or "店铺"


def create_batch(directory: Path, profile: str, stores: list[dict], sites: list[str], business_date: str) -> Path:
    directory = Path(directory).resolve()
    root = directory / f"TEMU活动报名导出_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=False)
    for name in ("raw", "results", "logs"):
        (root / name).mkdir()
    record = {
        "schema": SCHEMA,
        "run_id": root.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "profile": profile,
        "status": "pending",
        "business_date": business_date,
        "sites": list(sites),
        "error": "",
        "stores": [
            {
                **copy.deepcopy(store),
                "status": "pending",
                "attempts": 0,
                "raw_file": "",
                "result_file": "",
                "sha256": "",
                "quote_rows": None,
                "stock_rows": None,
                "spu_count": None,
                "sku_count": None,
                "topic_count": None,
                "exported_sites": {},
                "evidence": {},
                "error": "",
            }
            for store in stores
        ],
    }
    path = root / "manifest.json"
    write_json(path, record)
    return path


def load_batch(path: str | Path, profile: str | None = None) -> dict:
    path = Path(path).resolve()
    if path.name != "manifest.json":
        raise ValueError("请选择该批次的 manifest.json")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"批次清单无法读取：{exc}") from None
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise ValueError("不是有效的 TEMU 活动报名导出批次")
    if profile is not None and record.get("profile") != profile:
        raise ValueError("所选紫鸟账号与此批次不一致，请选择原账号或重新发起导出")
    stores = record.get("stores")
    if not isinstance(stores, list) or not stores or len(stores) > MAX_STORES:
        raise ValueError("批次店铺清单无效")
    seen = set()
    for store in stores:
        if not isinstance(store, dict) or not store.get("store_id") or not store.get("store_name"):
            raise ValueError("批次缺少稳定店铺标识或店铺名")
        if store["store_id"] in seen:
            raise ValueError("批次存在重复店铺 ID")
        seen.add(store["store_id"])
        if store.get("status") not in {"pending", "running", "success", "failed"}:
            raise ValueError("批次店铺状态无效")
        for field, folder in (("raw_file", "raw"), ("result_file", "results")):
            if store.get(field):
                candidate = (path.parent / store[field]).resolve()
                if Path(store[field]).is_absolute() or not candidate.is_relative_to((path.parent / folder).resolve()):
                    raise ValueError("批次文件路径超出本次导出目录")
    parse_sites(record.get("sites"))
    return record


def public_result(path: Path, record: dict | None = None) -> dict:
    path = path.resolve()
    record = record or load_batch(path)
    stores = copy.deepcopy(record["stores"])
    for store in stores:
        for field in ("raw_file", "result_file"):
            if store.get(field):
                store[field] = str(path.parent / store[field])
    counts = {status: sum(store["status"] == status for store in stores) for status in ("pending", "running", "success", "failed")}
    complete = record["status"] == "complete"
    if complete:
        message = f"{counts['success']} 个店铺已导出，共 {sum(store.get('quote_rows') or 0 for store in stores)} 条报价行"
    elif record["status"] == "running":
        message = f"正在逐店导出：已完成 {counts['success']}/{len(stores)}"
    else:
        message = record.get("error") or f"导出未全部完成：成功 {counts['success']}，失败 {counts['failed']}"
    site_rows: dict[str, int] = {}
    for store in stores:
        for label, rows in (store.get("exported_sites") or {}).items():
            site_rows[label] = site_rows.get(label, 0) + int(rows or 0)
    return {
        **record,
        "stores": stores,
        "manifest_path": str(path),
        "output_dir": str(path.parent),
        "store_count": len(stores),
        "success_count": counts["success"],
        "failed_count": counts["failed"],
        "pending_count": counts["pending"] + counts["running"],
        "quote_rows": sum(store.get("quote_rows") or 0 for store in stores if store["status"] == "success"),
        "stock_rows": sum(store.get("stock_rows") or 0 for store in stores if store["status"] == "success"),
        "site_rows": site_rows,
        "is_complete": complete,
        "completion_message": message,
        "can_retry": not complete and record["status"] != "running",
    }


def fail_batch(path: str | Path, profile: str | None, error: str) -> dict:
    path = Path(path).resolve()
    record = load_batch(path, profile)
    record["status"] = "failed"
    record["error"] = str(error)
    for store in record["stores"]:
        if store["status"] in {"pending", "running"}:
            store["status"] = "failed"
            store["error"] = str(error)
    write_json(path, record)
    return public_result(path, record)


def _log(path: Path, message: str, progress) -> None:
    stamp = datetime.now().isoformat(timespec="seconds")
    with (path.parent / "logs" / "run.log").open("a", encoding="utf-8") as stream:
        stream.write(f"{stamp} {message}\n")
    if progress:
        progress(message)


def _result_name(store: dict, business_date: str, suffix: str) -> str:
    site_part = "_".join(store.get("exported_sites") or {}) or "站点"
    return f"{safe_slug(store['store_name'])}_{safe_slug(store['store_id'])}_{site_part}_{business_date}_{suffix}.xlsx"


def run_batch(path: str | Path, gateway, progress=None) -> dict:
    """Run every store that has not succeeded yet; successful files are never rewritten."""
    path = Path(path).resolve()
    with batch_lock(path):
        record = load_batch(path, gateway.profile)
        record.update(status="running", error="")
        write_json(path, record)
        for store in record["stores"]:
            if store["status"] == "success" and store.get("raw_file"):
                if not (path.parent / store["raw_file"]).is_file():
                    store.update(status="failed", error="上一次的结果文件已被移动或删除，请重新导出该店")
        write_json(path, record)
        try:
            gateway.preflight(record["stores"])
        except Exception as exc:  # noqa: BLE001 - the reason is reported per store
            for store in record["stores"]:
                if store["status"] not in TERMINAL:
                    store.update(status="failed", error=str(exc))
            record.update(status="failed", error=str(exc))
            write_json(path, record)
            _log(path, f"运行前检查失败：{exc}", progress)
            return public_result(path, record)

        for index, store in enumerate(record["stores"], 1):
            if store["status"] in TERMINAL:
                continue
            store.update(status="running", error="", raw_file="", result_file="", sha256="",
                         quote_rows=None, stock_rows=None, spu_count=None, sku_count=None,
                         topic_count=None, exported_sites={}, evidence={},
                         attempts=int(store.get("attempts") or 0) + 1)
            write_json(path, record)
            _log(path, f"[{index}/{len(record['stores'])}] {store['store_name']}：开始导出活动报名模板", progress)
            raw_dir = path.parent / "raw" / f"{index:03d}_{safe_slug(store['store_name'])}" / f"attempt_{store['attempts']}"
            try:
                if getattr(gateway, "halted", False):
                    raise ValueError(getattr(gateway, "halt_reason", "浏览器会话已停止"))
                raw_dir.mkdir(parents=True, exist_ok=False)
                result = gateway.export(store, raw_dir, sites=record["sites"])
                raw = Path(result.get("path") or "").resolve()
                if not raw.is_file() or not raw.is_relative_to(raw_dir.resolve()):
                    raise ValueError("下载文件不在本店本次导出目录内")
                report = result.get("report") or {}
                evidence = result.get("evidence") or {}
                if not evidence:
                    raise ValueError("缺少本次导出的页面与文件核对信息")
                store["raw_file"] = str(raw.relative_to(path.parent))
                store["sha256"] = str(report.get("sha256") or "")
                store["quote_rows"] = int(report.get("quote_rows") or 0)
                store["stock_rows"] = int(report.get("stock_rows") or 0)
                store["spu_count"] = int(report.get("spu_count") or 0)
                store["sku_count"] = int(report.get("sku_id_count") or 0)
                store["topic_count"] = int(report.get("topic_count") or 0)
                store["exported_sites"] = dict(report.get("sites") or {})
                store["evidence"] = copy.deepcopy(evidence)
                # The raw download stays untouched; the named copy only helps 袁晶 find it.
                destination = path.parent / "results" / _result_name(store, record["business_date"], "活动报名模板")
                shutil.copy2(raw, destination)
                store["result_file"] = str(destination.relative_to(path.parent))
                store["status"] = "success"
            except Exception as exc:  # noqa: BLE001 - one store must not stop the batch
                store["status"] = "failed"
                store["error"] = str(exc)
            write_json(path, record)
            summary = f"{store['status']}"
            if store["status"] == "success":
                summary += f"，报价 {store['quote_rows']} 行 / 库存 {store['stock_rows']} 行 / {store['topic_count']} 个活动主题"
            _log(path, f"{store['store_name']}：{summary} {store['error']}".strip(), progress)
            if getattr(gateway, "halted", False):
                reason = getattr(gateway, "halt_reason", "浏览器会话已停止")
                for pending in record["stores"]:
                    if pending["status"] not in TERMINAL and pending is not store:
                        pending.update(status="failed", error=reason)
                break

        complete = all(store["status"] in TERMINAL for store in record["stores"])
        record["status"] = "complete" if complete else "failed"
        if not complete and not record.get("error"):
            record["error"] = f"部分店铺导出失败：{sum(s['status'] == 'failed' for s in record['stores'])} 个"
        write_json(path, record)
        result = public_result(path, record)
        _log(path, result["completion_message"], progress)
        return result
