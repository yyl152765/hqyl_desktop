"""Small durable checkpoints for claim queries; resumed databases are read only."""
from __future__ import annotations

import json
import math
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4


class ClaimCheckpointError(ValueError):
    pass


def _decode(value: str) -> Any:
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result
    return json.loads(value, object_pairs_hook=unique)


def _row(value: Any, skus: set[str]) -> dict[str, Any]:
    keys = ("sku", "shop_name", "item_id", "created_time", "listed_time", "status", "message")
    if not isinstance(value, dict) or any(not isinstance(value.get(key), str) for key in keys):
        raise ClaimCheckpointError("认领进度结果格式异常，无法恢复")
    row = {key: value[key] for key in keys}
    if row["sku"] not in skus or row["status"] not in {"matched", "not_found", "failed"}:
        raise ClaimCheckpointError("认领进度 SKU 或状态异常，无法恢复")
    if row["status"] == "matched":
        if not row["shop_name"].strip() or not row["item_id"].isdigit() or not int(row["item_id"]):
            raise ClaimCheckpointError("认领进度已匹配结果缺少店铺或商品编号")
        for key in ("created_time", "listed_time"):
            if key == "listed_time" and not row[key]:
                continue
            try:
                # Checkpoint stores the displayed BS wall-clock value, without an inferred timezone.
                parsed = datetime.strptime(row[key], "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
                if parsed.strftime("%Y-%m-%d %H:%M:%S") != row[key]:
                    raise ValueError()
            except ValueError:
                raise ClaimCheckpointError("认领进度商品时间格式异常，无法恢复") from None
    elif any(row[key] for key in ("shop_name", "item_id", "created_time", "listed_time")):
        raise ClaimCheckpointError("认领进度未匹配或未完成结果含旧数据，无法恢复")
    return row


class ClaimCheckpoint:
    def __init__(self, path: Path, connection: sqlite3.Connection, skus: set[str]):
        self.path, self.connection, self.skus = path, connection, skus
        self.deadline = 0.0

    def save_results(self, rows) -> None:
        clean = [_row(row, self.skus) for row in rows]
        try:
            encoded = [(row["sku"], json.dumps(row, ensure_ascii=False, allow_nan=False)) for row in clean]
            with self.connection:
                self.connection.executemany("INSERT OR REPLACE INTO results VALUES (?, ?)", encoded)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            raise ClaimCheckpointError("认领进度保存失败，请检查磁盘空间、权限及文件占用；已提交结果仍保留") from None

    def save_result(self, row) -> None:
        self.save_results([row])

    def save_deadline(self, deadline: float) -> None:
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not math.isfinite(deadline) or deadline < 0:
            raise ClaimCheckpointError("认领进度冷却时间异常，查询已停止")
        deadline = max(self.deadline, deadline)
        try:
            with self.connection:
                self.connection.execute("INSERT OR REPLACE INTO metadata VALUES ('retry_not_before', ?)", (str(deadline),))
            self.deadline = deadline
        except (sqlite3.Error, OSError):
            raise ClaimCheckpointError("认领进度冷却时间保存失败，查询已停止") from None

    def close(self) -> None:
        self.connection.close()


def create_claim_checkpoint(output_dir: Path, identity: dict[str, Any]) -> ClaimCheckpoint:
    connection = None
    try:
        directory = Path(output_dir) / "BigSeller认领进度"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"进度_{datetime.now().astimezone():%Y%m%d_%H%M%S}_{uuid4().hex}.sqlite3"
        with path.open("xb"):
            pass
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        with connection:
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("CREATE TABLE results (sku TEXT PRIMARY KEY, row_json TEXT NOT NULL)")
            connection.executemany("INSERT INTO metadata VALUES (?, ?)", [
                ("version", "claim-1"), ("identity", json.dumps(identity, ensure_ascii=False, sort_keys=True)),
                ("retry_not_before", "0"),
            ])
        return ClaimCheckpoint(path, connection, set(identity["skus"]))
    except (sqlite3.Error, OSError):
        if connection is not None:
            connection.close()
        raise ClaimCheckpointError("无法创建认领进度，请检查输出目录权限和磁盘空间") from None


def _read_connection(connection: sqlite3.Connection, identity: dict[str, Any]):
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise ClaimCheckpointError("认领进度文件损坏，无法恢复")
    entries = connection.execute("SELECT key, value FROM metadata").fetchall()
    metadata = dict(entries)
    if (len(entries) != len(metadata) or set(metadata) != {"version", "identity", "retry_not_before"}
            or metadata["version"] != "claim-1"):
        raise ClaimCheckpointError("认领进度版本或结构不受支持")
    if _decode(metadata["identity"]) != identity:
        raise ClaimCheckpointError("进度与当前账号、源文件、工作表、站点或商品范围不一致，无法恢复")
    deadline = float(metadata["retry_not_before"])
    if not math.isfinite(deadline) or deadline < 0:
        raise ClaimCheckpointError("认领进度冷却时间异常，无法恢复")
    rows = {}
    allowed_skus = set(identity["skus"])
    for sku, encoded in connection.execute("SELECT sku, row_json FROM results"):
        row = _row(_decode(encoded), allowed_skus)
        if row["sku"] != sku or sku in rows:
            raise ClaimCheckpointError("认领进度 SKU 不一致，无法恢复")
        rows[sku] = row
    return rows, deadline


def read_claim_checkpoint(path: Path, identity: dict[str, Any]) -> tuple[dict[str, dict[str, str]], float]:
    connection = None
    try:
        source = Path(path).resolve()
        connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        try:
            return _read_connection(connection, identity)
        except sqlite3.Error as exc:
            if getattr(exc, "sqlite_errorname", "") != "SQLITE_READONLY_ROLLBACK":
                raise
        finally:
            connection.close()
            connection = None
        # A crash during the next transaction can leave dirty database pages.
        # Recover a private copy of the rollback journal, never the source file.
        with TemporaryDirectory(prefix="bs_claim_recovery_") as temporary:
            recovered = Path(temporary) / source.name
            shutil.copyfile(source, recovered)
            shutil.copyfile(Path(str(source) + "-journal"), Path(str(recovered) + "-journal"))
            connection = sqlite3.connect(recovered)
            try:
                return _read_connection(connection, identity)
            finally:
                connection.close()
                connection = None
    except ClaimCheckpointError:
        raise
    except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
        raise ClaimCheckpointError("认领进度文件无法读取或已损坏，请选择有效的进度文件") from None
    finally:
        if connection is not None:
            connection.close()
