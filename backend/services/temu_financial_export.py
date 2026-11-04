"""Batch export of TEMU global financial details.

Each store keeps its untouched platform download plus an enriched workbook whose
first column on every non-empty sheet is the complete Ziniao store name.  The
batch merges only successfully prepared stores into one workbook, grouped by the
original sheet name.  A batch directory is written once and is never retried or
overwritten.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from backend.services.temu_on_sale_export import (
    batch_lock,
    is_temu,
    parse_names,
    write_json,
)
from backend.services.temu_financial_workbook import (
    FinancialWorkbookError,
    build_header_mapping,
    enrich_store,
    file_sha256,
    inspect_workbook,
    merge_store_workbooks,
)


SCHEMA = "temu_financial_export_v1"
TERMINAL = {"success"}
MAX_STORES = 200


def validate_request(payload: Mapping) -> tuple[list[str], str, str]:
    names = parse_names(payload.get("store_names"))
    if len(names) > MAX_STORES:
        raise ValueError(f"单次最多导出 {MAX_STORES} 个店铺")
    supplied = payload.get("store_names")
    lines = supplied.splitlines() if isinstance(supplied, str) else supplied
    if len([line for line in lines if str(line or "").strip()]) != len(names):
        raise ValueError("店铺名称不能重复")
    try:
        start, end = (
            date.fromisoformat(str(payload.get(key) or ""))
            for key in ("start_date", "end_date")
        )
    except ValueError:
        raise ValueError("请选择有效的开始和结束日期") from None
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    return names, start.isoformat(), end.isoformat()


def match_stores(names, catalog: list[dict]) -> dict:
    """Resolve a complete name first, or a uniquely contained partial name.

    Only the platform-returned full ``store_name`` is used.  No operator name is
    derived and no hyphen in the store name is required.
    """
    selected, issues, used = [], [], set()
    for name in parse_names(names):
        key = name.casefold()
        exact = [
            store for store in catalog
            if str(store.get("store_name") or "").strip().casefold() == key
        ]
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


def create_batch(directory: str | Path, profile: str, stores: list[dict], start: str, end: str) -> Path:
    root = Path(directory).resolve() / f"TEMU财务报表_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=False)
    for name in ("raw", "results", "logs"):
        (root / name).mkdir()
    record = {
        "schema": SCHEMA,
        "run_id": root.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "profile": profile,
        "status": "pending",
        "start_date": start,
        "end_date": end,
        "error": "",
        "stores": [
            {
                **copy.deepcopy(store),
                "status": "pending",
                "attempts": 0,
                "raw_file": "",
                "raw_row_count": None,
                "sha256": "",
                "result_file": "",
                "row_count": None,
                "sheet_count": None,
                "sheets": {},
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
        raise ValueError("不是有效的 TEMU 财务报表导出批次")
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
            if not store.get(field):
                continue
            value = Path(store[field])
            candidate = (path.parent / value).resolve()
            if value.is_absolute() or not candidate.is_relative_to((path.parent / folder).resolve()):
                raise ValueError("批次文件路径超出本次导出目录")
    merged = record.get("merged") or {}
    if merged.get("file"):
        value = Path(merged["file"])
        if value.is_absolute() or not (path.parent / value).resolve().is_relative_to((path.parent / "results").resolve()):
            raise ValueError("合并文件路径超出本次导出目录")
    return record


def public_result(path: str | Path, record: dict | None = None) -> dict:
    path = Path(path).resolve()
    record = record or load_batch(path)
    stores = copy.deepcopy(record["stores"])
    files = []
    merged = copy.deepcopy(record.get("merged") or {})
    if merged.get("file"):
        merged["file"] = str(path.parent / merged["file"])
        files.append(merged["file"])
    for store in stores:
        for field in ("raw_file", "result_file"):
            if store.get(field):
                store[field] = str(path.parent / store[field])
                if field == "result_file":
                    files.append(store[field])
    counts = {
        status: sum(store["status"] == status for store in stores)
        for status in ("pending", "running", "success", "failed")
    }
    complete = record["status"] == "complete"
    if complete and merged.get("file"):
        message = f"已完成 {counts['success']} 个店铺导出并生成合并财务报表"
    elif complete:
        message = f"已完成 {counts['success']} 个店铺导出"
    elif record["status"] in {"pending", "running"}:
        message = f"正在逐店导出：已完成 {counts['success']}/{len(stores)}"
    elif merged.get("status") == "partial":
        message = "部分店铺导出失败，已生成仅含成功店铺的不完整合并总表，请查看各店铺错误"
    else:
        message = record.get("error") or f"导出未全部完成：成功 {counts['success']}，失败 {counts['failed']}"
    return {
        **record,
        "stores": stores,
        "manifest_path": str(path),
        "output_dir": str(path.parent),
        "merged": merged,
        "output_files": files,
        "store_count": len(stores),
        "success_count": counts["success"],
        "failed_count": counts["failed"],
        "pending_count": counts["pending"] + counts["running"],
        "row_count": sum(store.get("row_count") or 0 for store in stores if store["status"] == "success"),
        "is_complete": complete,
        "completion_message": message,
        "can_retry": False,
    }


def fail_batch(path: str | Path, profile: str | None, error: str) -> dict:
    """End pending items without discarding already prepared store files."""
    path = Path(path).resolve()
    record = load_batch(path, profile)
    record["status"] = "failed"
    record["error"] = str(error)
    for store in record["stores"]:
        if store["status"] in {"pending", "running"} or (store["status"] == "success" and not store.get("result_file")):
            store["status"] = "failed"
            store["error"] = str(error)
    _merge_prepared(path, record, incomplete=True)
    write_json(path, record)
    return public_result(path, record)


def _log(path: Path, message: str, progress) -> None:
    stamp = datetime.now().isoformat(timespec="seconds")
    with (path.parent / "logs" / "run.log").open("a", encoding="utf-8") as stream:
        stream.write(f"{stamp} {message}\n")
    if progress:
        progress(message)


def _prepared_entries(record: dict) -> list[dict]:
    entries = []
    for store in record["stores"]:
        if store["status"] != "success":
            continue
        if not store.get("result_file") or not store.get("raw_file"):
            continue
        entries.append({
            "file": store["result_file"],
            "store_name": store["store_name"],
            "row_count": store.get("row_count"),
        })
    return entries


def _merge_prepared(path: Path, record: dict, *, incomplete: bool) -> None:
    """Best-effort merge of stores whose enriched workbook is already written."""
    entries = _prepared_entries(record)
    if not entries:
        return
    if record.get("merged", {}).get("file"):
        return
    suffix = "_不完整" if incomplete else ""
    destination = path.parent / "results" / f"TEMU财务报表汇总_{record['run_id']}{suffix}.xlsx"
    try:
        record["merged"] = merge_store_workbooks(
            path.parent, entries, destination, incomplete=incomplete or record["status"] == "failed",
        )
    except Exception as exc:  # noqa: BLE001 - keep per-store results visible
        record["merged"] = {"status": "failed", "error": str(exc)}


def run_batch(path: str | Path, gateway, progress=None) -> dict:
    path = Path(path).resolve()
    with batch_lock(path):
        record = load_batch(path, gateway.profile)
        if record["status"] != "pending":
            raise ValueError("该批次已经运行，请创建新批次；已保存结果不会覆盖")
        try:
            return _run_batch(path, gateway, progress, record)
        except Exception as exc:  # noqa: BLE001 - finalize every unfinished store
            return fail_batch(path, gateway.profile, f"批次中断：{exc}")


def _run_batch(path: Path, gateway, progress, record: dict) -> dict:
    record["status"] = "running"
    write_json(path, record)
    try:
        gateway.preflight(record["stores"])
    except Exception as exc:  # noqa: BLE001 - a login/catalog failure affects all stores
        for store in record["stores"]:
            if store["status"] not in TERMINAL:
                store.update(status="failed", error=str(exc))
        record.update(status="failed", error=str(exc))
        write_json(path, record)
        _log(path, f"运行前检查失败：{exc}", progress)
        return public_result(path, record)

    # Phase 1: download and validate every raw file.  Downloads are never reused
    # from another attempt, and existing batch directories cannot be re-run.
    for index, store in enumerate(record["stores"], 1):
        if store["status"] in TERMINAL:
            continue
        store.update(
            status="running", error="", raw_file="", raw_row_count=None,
            sha256="", result_file="", row_count=None, sheet_count=None,
            sheets={}, evidence={}, attempts=int(store.get("attempts") or 0) + 1,
        )
        write_json(path, record)
        slug = safe_slug(store["store_name"])
        raw_dir = path.parent / "raw" / f"{index:03d}_{slug}" / f"attempt_{store['attempts']}"
        _log(path, f"[{index}/{len(record['stores'])}] {store['store_name']}：开始导出全球财务明细", progress)
        try:
            if getattr(gateway, "halted", False):
                raise ValueError(getattr(gateway, "halt_reason", "浏览器会话已停止"))
            raw_dir.mkdir(parents=True, exist_ok=False)
            result = gateway.export(store, record["start_date"], record["end_date"], raw_dir)
            evidence = result.get("evidence") if isinstance(result, dict) else None
            if not isinstance(evidence, dict) or not evidence:
                raise ValueError("缺少本次导出任务的确认信息")
            raw = Path(result.get("path") or "").resolve()
            if not raw.is_file() or not raw.is_relative_to(raw_dir.resolve()):
                raise ValueError("下载文件不在本店本次导出目录内")
            info = inspect_workbook(raw)
            store.update(
                status="success",
                raw_file=str(raw.relative_to(path.parent)),
                raw_row_count=info.row_count,
                row_count=info.row_count,
                sheet_count=len(info.sheets),
                sha256=info.sha256,
                sheets={sheet.title: {"headers": list(sheet.headers), "row_count": sheet.row_count}
                        for sheet in info.sheets},
                evidence=copy.deepcopy(evidence),
            )
        except Exception as exc:  # noqa: BLE001 - one store must not stop the batch
            store.update(status="failed", error=str(exc))
        write_json(path, record)
        _log(path, f"{store['store_name']}：下载{store['status']} {store['error']}".strip(), progress)
        if getattr(gateway, "halted", False):
            reason = getattr(gateway, "halt_reason", "浏览器会话已停止")
            for pending in record["stores"]:
                if pending["status"] == "pending":
                    pending.update(status="failed", error=reason)
            break

    # Phase 2: compute one batch-wide disambiguation for pre-existing 店铺名
    # columns, then write each store's enriched workbook from its verified source.
    source_infos = {}
    for store in record["stores"]:
        if store["status"] != "success":
            continue
        try:
            raw = path.parent / store["raw_file"]
            if file_sha256(raw) != store["sha256"]:
                raise ValueError("原始财务明细在生成逐店工作簿前发生变化")
            info = inspect_workbook(raw)
            if info.row_count != store["raw_row_count"]:
                raise ValueError("原始财务明细行数发生变化")
            source_infos[store["store_id"]] = info
        except Exception as exc:  # noqa: BLE001
            store.update(status="failed", error=f"逐店工作簿前校验失败：{exc}")
    mapping = build_header_mapping(
        {sheet.title: sheet.headers for sheet in info.sheets}
        for info in source_infos.values()
    )
    for index, store in enumerate(record["stores"], 1):
        if store["status"] != "success":
            continue
        info = source_infos.get(store["store_id"])
        if info is None:
            store.update(status="failed", error="缺少可用的原始财务明细")
            write_json(path, record)
            continue
        destination = path.parent / "results" / f"{index:03d}_{safe_slug(store['store_name'])}_财务明细.xlsx"
        try:
            details = enrich_store(info.path, destination, store["store_name"], mapping)
            if details["row_count"] != info.row_count:
                raise ValueError("逐店工作簿行数与原始财务明细不一致")
            if details["sheet_count"] != len(info.sheets):
                raise ValueError("逐店工作簿工作表数量与原始财务明细不一致")
            if file_sha256(info.path) != info.sha256:
                raise ValueError("原始财务明细在生成逐店工作簿时发生变化")
            store.update(
                status="success",
                result_file=str(destination.relative_to(path.parent)),
                row_count=details["row_count"],
                sheet_count=details["sheet_count"],
                sheets=details["sheets"],
            )
        except Exception as exc:  # noqa: BLE001
            store.update(status="failed", error=f"生成逐店工作簿失败：{exc}")
        write_json(path, record)
        _log(path, f"{store['store_name']}：逐店工作簿{store['status']} {store['error']}".strip(), progress)

    complete = all(store["status"] in TERMINAL for store in record["stores"])
    record["status"] = "complete" if complete else "failed"
    if not complete and not record.get("error"):
        record["error"] = f"部分店铺导出失败：{sum(store['status'] == 'failed' for store in record['stores'])} 个"
    _merge_prepared(path, record, incomplete=not complete)
    if record.get("merged", {}).get("status") == "failed":
        if not complete:
            record["error"] = f"{record.get('error') or '导出未全部完成'}；合并总表失败：{record['merged'].get('error', '')}"
        else:
            record["status"] = "failed"
            record["error"] = f"合并总表失败：{record['merged'].get('error', '')}"
    write_json(path, record)
    result = public_result(path, record)
    _log(path, result["completion_message"], progress)
    return result
