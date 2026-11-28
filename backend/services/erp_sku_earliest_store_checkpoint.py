# ruff: noqa: DTZ007
"""Durable local progress for the ERP earliest-store query (no credentials stored)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

STATUSES = ("matched", "not_found", "incomplete", "failed", "not_run")
CONFIRMED_STATUSES = frozenset({"matched", "not_found", "incomplete"})
TEXT_FIELDS = (
    "sku", "status", "message", "candidate_shop_id", "candidate_shop_name", "region", "item_id", "model_id",
    "matched_sku", "create_time", "item_status", "item_archived", "model_archived", "match_source", "tied_shops",
    "base_clean_time", "model_clean_time", "queried_at",
)
INT_FIELDS = ("matched_item_count", "matched_shop_count", "data_issue_count", "tied_shop_count")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
VERSION = "erp-sku-1"


class CheckpointError(ValueError):
    pass


def empty_row(sku: str, message: str, status: str = "not_run") -> dict[str, Any]:
    row: dict[str, Any] = {key: "" for key in TEXT_FIELDS}
    row.update({key: 0 for key in INT_FIELDS})
    row.update(sku=sku, status=status, message=message)
    return row


def _valid_time(value: str) -> bool:
    if not value:
        return True
    try:
        return datetime.strptime(value, TIME_FORMAT).strftime(TIME_FORMAT) == value
    except ValueError:
        return False


def validate_row(value: Any, skus: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(TEXT_FIELDS) | set(INT_FIELDS):
        raise CheckpointError("进度结果格式异常，无法恢复")
    if any(not isinstance(value[key], str) for key in TEXT_FIELDS):
        raise CheckpointError("进度结果格式异常，无法恢复")
    if any(isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0 for key in INT_FIELDS):
        raise CheckpointError("进度结果数量字段异常，无法恢复")
    row = {key: value[key] for key in (*TEXT_FIELDS, *INT_FIELDS)}
    if row["sku"] not in skus or row["status"] not in STATUSES:
        raise CheckpointError("进度 SKU 或状态异常，无法恢复")
    if not all(_valid_time(row[key]) for key in ("create_time", "base_clean_time", "model_clean_time", "queried_at")):
        raise CheckpointError("进度时间格式异常，无法恢复")
    if row["status"] in {"failed", "not_run", "not_found"} and any(
            row[key] for key in ("candidate_shop_id", "candidate_shop_name", "item_id", "create_time")):
        raise CheckpointError("进度未完成或未找到结果含旧数据，无法恢复")
    if row["status"] == "matched" and not all(
            row[key] for key in ("candidate_shop_id", "candidate_shop_name", "item_id", "create_time")):
        raise CheckpointError("进度已匹配结果缺少店铺或商品编号")
    return row


class Checkpoint:
    def __init__(self, path: Path, connection: sqlite3.Connection, skus: set[str]) -> None:
        self.path, self.connection, self.skus = path, connection, skus

    def save_results(self, rows) -> None:
        clean = [validate_row(row, self.skus) for row in rows]
        try:
            encoded = [(row["sku"], json.dumps(row, ensure_ascii=False, allow_nan=False)) for row in clean]
            with self.connection:
                self.connection.executemany("INSERT OR REPLACE INTO results VALUES (?, ?)", encoded)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            raise CheckpointError("进度保存失败，请检查磁盘空间、权限及文件占用；已提交结果仍保留") from None

    def close(self) -> None:
        self.connection.close()


def _decode(value: str) -> Any:
    def unique(pairs):
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result
    return json.loads(value, object_pairs_hook=unique, parse_constant=_reject_constant)


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite number")


def create_checkpoint(run_dir: Path, identity: dict[str, Any]) -> Checkpoint:
    connection = None
    try:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"进度_{datetime.now().astimezone():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.sqlite3"
        with path.open("xb"):
            pass
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        with connection:
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("CREATE TABLE results (sku TEXT PRIMARY KEY, row_json TEXT NOT NULL)")
            connection.executemany("INSERT INTO metadata VALUES (?, ?)", [
                ("version", VERSION), ("identity", json.dumps(identity, ensure_ascii=False, sort_keys=True)),
            ])
        return Checkpoint(path, connection, set(identity["skus"]))
    except (sqlite3.Error, OSError):
        if connection is not None:
            connection.close()
        raise CheckpointError("无法创建进度文件，请检查输出目录权限和磁盘空间") from None


def read_checkpoint(path: Path, identity: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Read a previous checkpoint (read-only). The identity must match exactly."""
    connection = None
    try:
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise CheckpointError("进度文件损坏，无法恢复")
        entries = connection.execute("SELECT key, value FROM metadata").fetchall()
        metadata = dict(entries)
        if len(entries) != len(metadata) or set(metadata) != {"version", "identity"} or metadata["version"] != VERSION:
            raise CheckpointError("进度版本或结构不受支持")
        if _decode(metadata["identity"]) != identity:
            raise CheckpointError("进度与当前输入、查询范围或查询口径不一致，无法恢复；如需最新结果请新建查询")
        rows: dict[str, dict[str, Any]] = {}
        allowed = set(identity["skus"])
        for sku, encoded in connection.execute("SELECT sku, row_json FROM results"):
            row = validate_row(_decode(encoded), allowed)
            if row["sku"] != sku or sku in rows:
                raise CheckpointError("进度 SKU 不一致，无法恢复")
            rows[sku] = row
        return rows
    except CheckpointError:
        raise
    except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
        raise CheckpointError("进度文件无法读取或已损坏，请选择有效的进度文件") from None
    finally:
        if connection is not None:
            connection.close()
