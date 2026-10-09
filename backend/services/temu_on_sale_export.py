from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from backend.services.temu_on_sale_workbook import inspect_source, merge_sources


SCHEMA = "temu_on_sale_export_v1"
TERMINAL = {"success", "no_data"}


def parse_names(value: Any) -> list[str]:
    values = value.splitlines() if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        raise ValueError("请输入店铺名称，每行一个")
    names, seen = [], set()
    for value in values:
        name = str(value or "").strip()
        key = name.casefold()
        if name and key not in seen:
            names.append(name)
            seen.add(key)
    if not names:
        raise ValueError("请至少填写一个完整店铺名，每行一个")
    if len(names) > 200:
        raise ValueError("单批最多支持 200 个店铺")
    return names


def match_stores(names: Any, stores: list[dict]) -> dict:
    requested = parse_names(names)
    matched, issues, used = [], [], set()
    for name in requested:
        candidates = [store for store in stores if store["store_name"].casefold() == name.casefold()]
        by_id = {store["store_id"]: store for store in candidates}
        if len(by_id) != 1:
            issues.append({"name": name, "error": "未匹配到完整店铺名" if not by_id else "存在同名店铺，请先在紫鸟中修改为不同名称后重试", "candidates": list(by_id.values())})
            continue
        store = next(iter(by_id.values()))
        if not is_temu(store):
            issues.append({"name": name, "error": "紫鸟平台信息未确认是 TEMU，不能仅按店铺名称判断", "candidates": [store]})
        elif store["store_id"] not in used:
            matched.append(copy.deepcopy(store))
            used.add(store["store_id"])
    return {"stores": matched, "issues": issues, "can_start": bool(matched) and not issues}


def is_temu(store: dict) -> bool:
    return "temu" in str(store.get("platform_name") or "").casefold()


def select_stores(ids: Any, stores: list[dict]) -> list[dict]:
    if not isinstance(ids, list) or not ids or len(ids) > 200:
        raise ValueError("请从候选列表选择 1 至 200 个店铺")
    selected = []
    for value in dict.fromkeys(str(item) for item in ids):
        candidates = [store for store in stores if store["store_id"] == value]
        if len(candidates) != 1 or not is_temu(candidates[0]):
            raise ValueError(f"店铺 ID {value} 不存在、重复或平台信息不是 TEMU，请刷新店铺列表")
        selected.append(copy.deepcopy(candidates[0]))
    return selected


def writable_directory(value: Any) -> Path:
    if not str(value or "").strip():
        raise ValueError("请选择输出目录")
    path = Path(str(value).strip()).expanduser().resolve()
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path) as stream:
            stream.write(b"HQYL")
            stream.flush()
    except OSError as exc:
        raise ValueError("输出目录不可写，请选择有写入权限的本地目录") from exc
    return path


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def create_batch(output_dir: Path, profile: str, stores: list[dict]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = uuid4().hex[:12]
    path = output_dir / f"TEMU在售商品_{stamp}_{run_id}" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=False)
    (path.parent / "raw").mkdir()
    (path.parent / "logs").mkdir()
    record = {
        "schema": SCHEMA, "run_id": run_id, "created_at": stamp, "profile": profile,
        "status": "pending", "output_file": "", "merge_error": "",
        "stores": [{**copy.deepcopy(store), "status": "pending", "attempts": 0,
                    "raw_file": "", "raw_row_count": None, "row_count": None,
                    "sha256": "", "error": "", "added_columns": [], "missing_columns": []}
                   for store in stores],
    }
    write_json(path, record)
    return path


def load_batch(path: str | Path, profile: str | None = None) -> dict:
    path = Path(path).resolve()
    if path.name != "manifest.json":
        raise ValueError("请选择该批次的 manifest.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise ValueError("不是有效的 TEMU 在售商品批次清单")
    if profile is not None and record.get("profile") != profile:
        raise ValueError("所选紫鸟账号与此批次不一致，请选择原账号；旧 CLI 批次请使用新账号重新导出")
    stores = record.get("stores")
    if not isinstance(stores, list) or not stores or len(stores) > 200:
        raise ValueError("批次店铺清单无效")
    ids = set()
    for store in stores:
        if not isinstance(store, dict) or not store.get("store_id") or not store.get("store_name"):
            raise ValueError("批次缺少稳定店铺标识或店铺名")
        if store["store_id"] in ids:
            raise ValueError("批次存在重复店铺 ID")
        ids.add(store["store_id"])
        if store.get("status") not in {"pending", "running", "success", "no_data", "failed"}:
            raise ValueError("批次店铺状态无效")
        if store.get("raw_file"):
            raw = (path.parent / store["raw_file"]).resolve()
            if not raw.is_relative_to((path.parent / "raw").resolve()):
                raise ValueError("原始文件路径超出该批次目录")
    output = str(record.get("output_file") or "")
    if output and not (path.parent / output).resolve().is_relative_to(path.parent):
        raise ValueError("汇总文件路径超出该批次目录")
    return record


def public_result(path: Path, record: dict | None = None) -> dict:
    path = path.resolve()
    record = record or load_batch(path)
    stores = copy.deepcopy(record["stores"])
    for store in stores:
        if store.get("raw_file"):
            store["raw_file"] = str(path.parent / store["raw_file"])
    count = lambda status: sum(store["status"] == status for store in stores)
    done = count("success") + count("no_data")
    complete = record["status"] == "complete"
    message = ("全部店铺无在售商品" if count("no_data") == len(stores) else "全部选定店铺已导出并汇总") if complete else (
        record.get("merge_error") or f"部分结果：{done}/{len(stores)} 个店铺完成，失败 {count('failed')} 个"
    )
    return {
        "run_id": record["run_id"], "profile": record["profile"], "status": record["status"],
        "manifest_path": str(path), "output_dir": str(path.parent), "stores": stores,
        "output_file": str(path.parent / record["output_file"]) if record.get("output_file") else "",
        "store_count": len(stores), "success_count": count("success"), "no_data_count": count("no_data"),
        "failed_count": count("failed"), "completed_count": done,
        "row_count": sum(store.get("row_count") or 0 for store in stores if store["status"] == "success"),
        "is_complete": complete, "completion_message": message,
        "can_retry": not complete and record["status"] != "running",
    }


@contextmanager
def batch_lock(path: Path):
    stream = path.with_name(".batch.lock").open("a+b")
    try:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("该批次正在运行，请等待完成后重试") from exc
        yield
    finally:
        stream.close()  # OS releases the lock even after process interruption.


def run_batch(path: Path, gateway: Any, progress: Callable[[str], None]) -> dict:
    path = path.resolve()
    with batch_lock(path):
        record = load_batch(path, gateway.profile)

        def save():
            record["updated_at"] = datetime.now().isoformat(timespec="seconds")
            write_json(path, record)

        def log(message: str):
            with (path.parent / "logs" / "run.log").open("a", encoding="utf-8") as stream:
                stream.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
            progress(message)

        record.update(status="running", merge_error="", output_file="")
        # Successful files remain the sole source of truth on retry; never append to an old summary.
        for store in record["stores"]:
            if store["status"] == "success":
                try:
                    info = inspect_source(path.parent / store["raw_file"])
                    if info.sha256 != store["sha256"] or info.row_count != store["row_count"]:
                        raise ValueError("原始文件校验值或行数发生变化")
                except Exception as exc:
                    store.update(status="failed", error=f"原始结果无法复用：{exc}")
            elif store["status"] == "no_data" and not store.get("no_data_evidence"):
                store.update(status="failed", error="缺少页面零条结果证据")
        save()
        try:
            gateway.preflight(record["stores"])
        except Exception as exc:
            for store in record["stores"]:
                if store["status"] not in TERMINAL:
                    store.update(status="failed", error=str(exc), row_count=None, raw_row_count=None)
            log(f"运行前检查失败：{exc}")
        else:
            for store in record["stores"]:
                if store["status"] in TERMINAL:
                    continue
                store.update(status="running", error="", raw_file="", row_count=None, raw_row_count=None, sha256="",
                             added_columns=[], missing_columns=[],
                             attempts=int(store.get("attempts") or 0) + 1)
                # A generated ordinal isolates stores and retries without path-sensitive names.
                index = record["stores"].index(store) + 1
                slug = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", store["store_name"]).strip(" .")[:35] or "店铺"
                raw_dir = path.parent / "raw" / f"{index:03d}_{slug}" / f"attempt_{store['attempts']}_{uuid4().hex[:8]}"
                save()
                log(f"[{index}/{len(record['stores'])}] {store['store_name']}：开始导出")
                try:
                    raw_dir.mkdir(parents=True, exist_ok=False)
                    result = gateway.export(store, raw_dir)
                    if result.get("no_data") is True:
                        if not result.get("evidence"):
                            raise ValueError("缺少页面零条在售商品的确认信息")
                        store.update(status="no_data", row_count=0, raw_row_count=0,
                                     no_data_evidence=result["evidence"])
                    else:
                        raw = Path(result.get("path") or "").resolve()
                        if not raw.is_relative_to(raw_dir.resolve()):
                            raise ValueError("下载文件不在本店本次尝试目录内")
                        store["raw_file"] = str(raw.relative_to(path.parent))
                        info = inspect_source(raw)
                        store["raw_row_count"] = info.row_count
                        if info.row_count == 0:
                            raise ValueError("导出为空但页面未确认零条，请核实后重试")
                        store.update(status="success", raw_file=str(raw.relative_to(path.parent)),
                                     row_count=info.row_count, raw_row_count=info.row_count, sha256=info.sha256,
                                     added_columns=info.added_columns, missing_columns=info.missing_columns)
                        if result.get("evidence"):
                            store["export_evidence"] = result["evidence"]
                        if info.added_columns or info.missing_columns:
                            log(f"{store['store_name']}：模板变化，新增 {info.added_columns}，缺少 {info.missing_columns}")
                except Exception as exc:
                    store.update(status="failed", error=str(exc))
                save()
                log(f"{store['store_name']}：{store['status']}，保留 {store['row_count'] if store['row_count'] is not None else '—'} 行 {store['error']}")
                if getattr(gateway, "auth_failed", False) or getattr(gateway, "halted", False) is True:
                    reason = getattr(gateway, "halt_reason", "紫鸟认证已失效，请恢复后重试")
                    for pending in record["stores"]:
                        if pending["status"] not in TERMINAL and pending is not store:
                            pending.update(status="failed", error=reason)
                    break
        save()
        sources = []
        for store in record["stores"]:
            if store["status"] == "success":
                try:
                    info = inspect_source(path.parent / store["raw_file"])
                    if info.sha256 != store["sha256"]:
                        raise ValueError("原始文件发生变化")
                    sources.append((store["store_name"], info))
                except Exception as exc:
                    store.update(status="failed", error=f"汇总前校验失败：{exc}")
        complete = all(store["status"] in TERMINAL for store in record["stores"])
        if sources or complete:
            suffix = "" if complete else "_部分结果"
            # Each retry writes a new summary; earlier partial files are retained as historical outputs.
            output = path.parent / f"TEMU在售商品汇总_{record['created_at']}{suffix}_{uuid4().hex[:6]}.xlsx"
            try:
                log("正在重新汇总并校验 Excel…")
                merge_sources(sources, output)
                record["output_file"] = output.name
            except Exception as exc:
                complete = False
                record["merge_error"] = f"汇总保存失败：{exc}，可重试汇总"
        record["status"] = "complete" if complete and record["output_file"] else "incomplete"
        save()
        result = public_result(path, record)
        log(result["completion_message"])
        return result
