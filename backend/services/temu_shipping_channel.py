"""Ordered TEMU channel updates with a durable, credential-free execution record."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from backend.services.temu_shipping_workbook import read_temu_shipping_workbook

CHANNEL_FIELDS = (
    "channel_id", "channel_name", "logistics_id", "my_logistics_id", "source",
    "enabled_state",
)
RUN_SCHEMA_VERSION = 2  # v1 included disabled channels; its mappings must never be reused.
DIMENSIONS = ("length", "width", "height", "weight")
FIELD_LABELS = {"length": "长", "width": "宽", "height": "高", "weight": "重量",
                "auto_fill_dimensions": "自动填写包裹尺寸", "prefer_channel_weight": "优先按渠道申报重量",
                "prefer_fixed_volume": "优先按渠道固定体积"}
SUCCESS_STATES = {"success", "unchanged"}
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_IO_LOCKS: dict[str, threading.RLock] = {}
_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


class TemuShippingProgressError(RuntimeError):
    """A checkpoint could not be persisted; stop before any further submission."""


def _replace_checkpoint(temporary: Path, target: Path) -> None:
    for attempt in range(len(_REPLACE_RETRY_DELAYS) + 1):
        try:
            os.replace(temporary, target)
            return
        except OSError as exc:
            # Other processes (including scanners) can briefly hold a Windows
            # handle without FILE_SHARE_DELETE, even with our own readers locked.
            if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == len(_REPLACE_RETRY_DELAYS):
                raise
            time.sleep(_REPLACE_RETRY_DELAYS[attempt])


def account_key(username: str) -> str:
    return hashlib.sha256(("mabang:900853:" + str(username).strip()).encode()).hexdigest()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _mapping_digest(record: dict) -> str:
    return _digest({
        "account_key": record["account_key"],
        "workbook_fingerprint": record["workbook_fingerprint"],
        "rows": [{k: row.get(k) for k in ("channel", "excel_row", *DIMENSIONS)} for row in record["rows"]],
    })


def channel_identity(channel: dict) -> tuple[str, ...]:
    return tuple(str(channel.get(k) or "") for k in ("logistics_id", "my_logistics_id", "source", "channel_id"))


def _managed_values(values: dict | None) -> dict:
    """Only these non-secret values may enter a checkpoint or public result."""
    return {field: values[field] for field in FIELD_LABELS if values and field in values}


def _display_value(field: str, value: Any) -> str:
    if value is None:
        return "未读取"
    if field not in DIMENSIONS:
        return "开启" if value else "关闭"
    return f"{value} {'g' if field == 'weight' else 'cm'}"


def _change_details(row: dict, wanted: dict) -> str:
    original = row.get("original_values", {})
    observed = row.get("observed_values", {})
    parts = []
    for field in DIMENSIONS:
        parts.append(f"{FIELD_LABELS[field]} {_display_value(field, original.get(field))} → {_display_value(field, wanted[field])}")
    for field in FIELD_LABELS:
        if field in DIMENSIONS:
            continue
        if field in original and original[field] != wanted.get(field):
            parts.append(f"{FIELD_LABELS[field]} {_display_value(field, original[field])} → {_display_value(field, wanted[field])}")
    if observed:
        stage = "保存后读回" if row.get("observation_stage") == "after_save" else "当前读值"
        parts.append(stage + "：" + "，".join(
            f"{FIELD_LABELS[field]} {_display_value(field, observed.get(field))}" for field in DIMENSIONS))
    else:
        parts.append("实际值未读取，尚未核验")
    return "；".join(parts)


def _verification_error(observed: dict, wanted: dict) -> str:
    differences = [f"{FIELD_LABELS[field]}实际 {_display_value(field, observed.get(field))}，目标 {_display_value(field, value)}"
                   for field, value in wanted.items() if observed.get(field) != value]
    return "保存后核验未通过：" + ("；".join(differences) or "渠道身份或其他设置不一致")


class TemuShippingRunStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        lock_key = os.path.normcase(str(self.directory.resolve()))
        with _LOCKS_GUARD:
            self._io_lock = _IO_LOCKS.setdefault(lock_key, threading.RLock())

    def _path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", str(run_id)):
            raise ValueError("任务记录无效，请重新查询预览")
        return self.directory / f"{run_id}.json"

    def save(self, record: dict) -> None:
        target = self._path(record["run_id"])
        temporary = target.with_name(target.name + "." + uuid4().hex + ".tmp")
        with self._io_lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                record["updated_at"] = _now()
                with temporary.open("w", encoding="utf-8") as stream:
                    json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                _replace_checkpoint(temporary, target)
            except OSError as exc:
                raise TemuShippingProgressError(
                    "本地进度保存失败，可能是文件被占用、目录无写入权限或磁盘空间不足；已有进度保留，请处理后继续未完成渠道"
                ) from exc
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # Cleanup must not hide the actual failure or invalidate a
                    # successful atomic commit. Never delete the original JSON.
                    pass

    def load(self, run_id: str, key: str) -> dict:
        try:
            with self._io_lock:
                record = json.loads(self._path(run_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("无法读取任务记录，请重新查询预览") from exc
        if not isinstance(record, dict):
            raise ValueError("任务记录格式不正确，请重新查询预览")
        if record.get("schema_version") != RUN_SCHEMA_VERSION or record.get("run_id") != run_id:
            raise ValueError("任务筛选规则已更新为仅已开启渠道，请重新查询预览")
        if record.get("account_key") != key:
            raise ValueError("该任务属于其他马帮账号，请切回原账号")
        try:
            if record["status"] not in {"ready", "blocked", "running", "partial", "stale", "complete"}:
                raise ValueError("任务记录状态无效，请重新查询预览")
            if record["mode"] not in {"preview", "batch"}:
                raise ValueError("任务记录模式无效，请重新查询预览")
            if not isinstance(record["excel_row_count"], int) or record["excel_row_count"] < 1:
                raise ValueError("任务 Excel 数据数量无效，请重新查询预览")
            for field in ("input_file", "sheet_name", "workbook_fingerprint", "updated_at"):
                if not isinstance(record[field], str) or not record[field]:
                    raise ValueError("任务记录信息不完整，请重新查询预览")
            rows = record["rows"]
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError("任务渠道映射格式无效，请重新查询预览")
            for row in rows:
                if not isinstance(row["channel"], dict):
                    raise ValueError("任务渠道信息无效，请重新查询预览")
                if row["channel"].get("enabled_state") != "1":
                    raise ValueError("任务包含未确认开启的渠道，请重新查询预览")
                if not all(isinstance(row["channel"][field], str) and row["channel"][field]
                           for field in ("channel_id", "channel_name")):
                    raise ValueError("任务渠道标识或名称缺失，请重新查询预览")
                if row["status"] not in {"ready", "missing_data", "saving", "failed", "success", "unchanged"}:
                    raise ValueError("任务渠道状态无效，请重新查询预览")
                if not isinstance(row["attempts"], int) or row["attempts"] < 0:
                    raise ValueError("任务渠道重试次数无效，请重新查询预览")
            identities = [channel_identity(row["channel"]) for row in rows]
            if not rows or len(rows) > 10000 or len(set(identities)) != len(rows):
                raise ValueError("任务渠道映射不完整或重复")
            if record["mapping_digest"] != _mapping_digest(record):
                raise ValueError("任务渠道与 Excel 的对应记录已损坏")
            for row in rows:
                if row.get("excel_row") is None:
                    continue
                if not isinstance(row["excel_row"], int) or row["excel_row"] < 2:
                    raise ValueError("Excel 行号无效")
                for field in DIMENSIONS:
                    value = Decimal(str(row[field]))
                    if not value.is_finite() or value <= 0:
                        raise ValueError("任务尺寸或重量无效")
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise ValueError("任务记录不完整，请重新查询预览") from exc
        return record

    def latest(self, key: str) -> dict | None:
        with self._io_lock:
            if not self.directory.exists():
                return None
            paths = sorted(self.directory.glob("*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
            for path in paths:
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(record, dict) and record.get("account_key") == key:
                    if record.get("schema_version") != RUN_SCHEMA_VERSION:
                        continue
                    return self.load(path.stem, key)
            return None

    @contextmanager
    def operation(self, key: str):
        """The OS releases the lock after a crash, allowing safe restart and readback."""
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_path = self.directory / f"{key}.lock"
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(str(lock_path.resolve()), threading.Lock())
        if not lock.acquire(blocking=False):
            raise ValueError("该马帮账号已有 TEMU 任务正在运行，请等待完成")
        stream = None
        locked = False
        try:
            stream = lock_path.open("a+b")
            if stream.seek(0, os.SEEK_END) == 0:
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
                locked = True
            except OSError as exc:
                raise ValueError("该马帮账号在另一个程序窗口运行 TEMU 任务，请等待完成") from exc
            yield
        finally:
            if stream is not None:
                if locked:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()
            lock.release()


def public_result(record: dict, *, mode: str | None = None) -> dict:
    rows = record["rows"]
    changed_count = sum(row["status"] == "success" for row in rows)
    unchanged_count = sum(row["status"] == "unchanged" for row in rows)
    success_count = sum(row["status"] in SUCCESS_STATES for row in rows)
    failed_count = sum(row["status"] in {"failed", "saving"} for row in rows)
    missing_count = sum(row.get("excel_row") is None for row in rows)
    complete = success_count == len(rows) and bool(rows)
    can_apply = bool(rows) and not missing_count and not complete and record.get("status") != "stale"
    actual_mode = mode or record.get("mode", "preview")
    message = record.get("message", "")
    if missing_count:
        message = f"查询到 {len(rows)} 条已开启渠道，Excel 有 {record['excel_row_count']} 组，缺少 {missing_count} 组；请补齐后重新预览"
    elif complete:
        message = f"全部 {len(rows)} 条渠道已核验：已修改 {changed_count} 条，无需修改 {unchanged_count} 条"
        if unchanged_count == len(rows):
            message += "；本次未提交任何修改，当前值与本次导入数据相同"
    elif actual_mode == "preview":
        message = f"已按 Excel 顺序匹配 {len(rows)} 条渠道，可以开始批量设置"
    return {
        "selection_policy": "enabled_only",
        "mode": actual_mode, "run_id": record["run_id"],
        "input_file": record["input_file"], "sheet_name": record["sheet_name"],
        "excel_row_count": record["excel_row_count"], "channel_count": len(rows),
        "ready_count": sum(row["status"] == "ready" for row in rows),
        "success_count": success_count, "failed_count": failed_count,
        "changed_count": changed_count, "unchanged_count": unchanged_count,
        "complete": complete, "can_apply": can_apply,
        "status": record["status"], "message": message,
        "calculation_date": record.get("calculation_date"),
        "warnings": record.get("warnings", []), "updated_at": record["updated_at"],
        "is_complete": not missing_count if actual_mode == "preview" else complete,
        "completion_message": message or "部分渠道尚未完成，请继续未完成渠道",
        "rows": [{
            "channel_id": row["channel"]["channel_id"],
            "channel_name": row["channel"]["channel_name"],
            **{field: row.get(field) for field in ("excel_row", *DIMENSIONS, "status", "message")},
            "original_values": _managed_values(row.get("original_values")),
            "observed_values": _managed_values(row.get("observed_values")),
            "observation_stage": row.get("observation_stage"),
        } for row in rows],
    }


def preview_temu_shipping(
    *, username: str, password: str, input_file: str,
    store: TemuShippingRunStore, progress: Callable[[str], None] | None = None,
    gateway_factory=None,
) -> dict:
    from backend.services.temu_shipping_gateway import TemuShippingGateway

    emit = progress or (lambda _message: None)
    workbook = read_temu_shipping_workbook(input_file)
    key = account_key(username)
    with store.operation(key):
        emit(f"已读取 Excel {workbook['row_count']} 组尺寸和重量，正在查询已开启的 TEMU 渠道")
        with (gateway_factory or TemuShippingGateway)(username, password) as gateway:
            channels = gateway.list_channels()
        if not channels:
            raise ValueError("没有找到已开启且可设置的 TEMU 物流渠道，请检查物流授权")
        if any(channel.get("enabled_state") != "1" for channel in channels):
            raise ValueError("查询结果包含未确认开启的渠道，请重新查询")
        identities = [channel_identity(channel) for channel in channels]
        if len(set(identities)) != len(identities):
            raise ValueError("查询结果包含重复渠道，无法建立唯一顺序，请重新查询")
        rows = []
        for index, channel in enumerate(channels):
            dimensions = workbook["rows"][index] if index < workbook["row_count"] else {}
            rows.append({
                "channel": {field: str(channel.get(field) or "") for field in CHANNEL_FIELDS},
                **{field: dimensions.get(field) for field in ("excel_row", *DIMENSIONS)},
                "status": "ready" if dimensions else "missing_data",
                "message": "等待设置" if dimensions else "Excel 数据不足",
                "attempts": 0,
            })
        record = {
            "schema_version": RUN_SCHEMA_VERSION, "run_id": uuid4().hex, "account_key": key,
            "input_file": workbook["input_file"], "sheet_name": workbook["sheet_name"],
            "workbook_fingerprint": workbook["fingerprint"],
            "excel_row_count": workbook["row_count"], "rows": rows,
            "calculation_date": workbook.get("calculation_date"),
            "warnings": workbook.get("warnings", []),
            "created_at": _now(), "mode": "preview",
            "status": "ready" if len(channels) <= workbook["row_count"] else "blocked",
        }
        record["mapping_digest"] = _mapping_digest(record)
        store.save(record)
        result = public_result(record)
        emit(result["message"])
        return result


def run_temu_shipping_batch(
    *, username: str, password: str, run_id: str,
    store: TemuShippingRunStore, progress: Callable[[str], None] | None = None,
    gateway_factory=None,
) -> dict:
    from backend.services.temu_shipping_gateway import TemuShippingGateway, desired_values, settings_match

    emit = progress or (lambda _message: None)

    def pause_for_progress_error(record: dict, error: TemuShippingProgressError) -> dict:
        # The durable record still contains the last committed snapshot. Do not
        # attempt further POSTs or discard it just because local storage is busy.
        stale = record.get("status") == "stale"
        message = f"任务已暂停：{error}"
        if stale:
            message += "；已开启渠道已变化，请重新查询预览"
        record.update(mode="batch", status="stale" if stale else "partial", message=message)
        result = public_result(record, mode="batch")
        result.update(complete=False, is_complete=False, can_apply=not stale,
                      message=record["message"], completion_message=record["message"])
        emit(record["message"])
        return result

    key = account_key(username)
    with store.operation(key):
        record = store.load(run_id, key)
        if public_result(record)["complete"]:
            return public_result(record, mode="batch")
        if not public_result(record)["can_apply"]:
            raise ValueError(public_result(record)["message"] or "任务不能执行，请重新查询预览")
        record.update(mode="batch", status="running", message="正在处理渠道")
        try:
            store.save(record)
            with (gateway_factory or TemuShippingGateway)(username, password) as gateway:
                current = gateway.list_channels()
                if any(channel.get("enabled_state") != "1" for channel in current) or {channel_identity(channel) for channel in current} != {channel_identity(row["channel"]) for row in record["rows"]}:
                    record.update(status="stale", message="TEMU 已开启渠道列表或开关状态已变化，请重新查询预览；原进度已保留")
                    store.save(record)
                    return public_result(record, mode="batch")
                for index, row in enumerate(record["rows"], 1):
                    if row["status"] in SUCCESS_STATES:
                        continue
                    channel = row["channel"]
                    prefix = f"[{index}/{len(record['rows'])}] {channel['channel_name']}（Excel 第 {row['excel_row']} 行）"
                    wanted = desired_values(row)
                    before = None
                    attempted = False
                    row.pop("observed_values", None)
                    row.pop("observation_stage", None)
                    try:
                        before = gateway.read_settings(channel)
                        # Preserve the first actual values across retries and restarts.
                        if not row.get("original_values"):
                            row["original_values"] = _managed_values(before.managed_values)
                        row.update(observed_values=_managed_values(before.managed_values), observation_stage="before_save")
                        if row.get("other_fingerprint") and row["other_fingerprint"] != before.other_fingerprint:
                            raise ValueError("重新读取发现其他渠道设置发生变化，请人工核对后重新预览")
                        if settings_match(before, wanted):
                            if row.get("attempts", 0):
                                row.update(status="success", message="重新读取已确认上次提交结果，本次未重复提交")
                            else:
                                row.update(status="unchanged", message="无需修改：当前值与本次导入数据相同，未提交修改")
                        else:
                            row.update(
                                other_fingerprint=before.other_fingerprint,
                                status="saving", message="正在提交并核验",
                                attempts=int(row.get("attempts", 0)) + 1,
                            )
                            # Persist before POST, so an interrupted submission is read back on resume.
                            store.save(record)
                            emit(prefix + "：准备修改；" + _change_details(row, wanted))
                            attempted = True
                            # A pre-submit reading must never appear as post-save evidence.
                            row.pop("observed_values", None)
                            row.pop("observation_stage", None)
                            gateway.save_settings(channel, before, row)
                            after = gateway.read_settings(channel)
                            row.update(observed_values=_managed_values(after.managed_values), observation_stage="after_save")
                            if not settings_match(after, wanted, before):
                                raise ValueError(_verification_error(after.managed_values, wanted))
                            row.update(status="success", message="保存并核验成功")
                    except TemuShippingProgressError:
                        row.update(status="ready", message="本地进度未能保存，本次尚未提交该渠道")
                        raise
                    except Exception as exc:
                        recovered = False
                        if attempted and before is not None:
                            try:
                                reread = gateway.read_settings(channel)
                                row.update(observed_values=_managed_values(reread.managed_values), observation_stage="after_save")
                                recovered = settings_match(reread, wanted, before)
                            except Exception:
                                pass
                        if recovered:
                            row.update(status="success", message="提交后重新读取，已确认保存成功")
                        else:
                            # Never include credentials or whole platform responses in logs or records.
                            message = str(exc).replace(str(password), "[已隐藏]") if password else str(exc)
                            row.update(status="failed", message=message[:300] or "渠道处理失败，请重试")
                    store.save(record)
                    emit(prefix + "：" + row["message"] + "；" + _change_details(row, wanted))
        except TemuShippingProgressError as exc:
            return pause_for_progress_error(record, exc)
        except Exception as exc:
            message = str(exc).replace(str(password), "[已隐藏]") if password else str(exc)
            record.update(status="partial", message=(message[:300] or "连接马帮失败") + "；原顺序已保留，可继续未完成渠道")
            try:
                store.save(record)
            except TemuShippingProgressError as progress_error:
                return pause_for_progress_error(record, progress_error)
            emit(record["message"])
            return public_result(record, mode="batch")
        complete = all(row["status"] in SUCCESS_STATES for row in record["rows"])
        record.update(
            status="complete" if complete else "partial",
            message="全部渠道已设置并核验" if complete else "部分渠道设置失败，已保留原对应关系，可继续未完成渠道",
        )
        try:
            store.save(record)
        except TemuShippingProgressError as exc:
            return pause_for_progress_error(record, exc)
        result = public_result(record, mode="batch")
        emit(result["message"])
        return result
