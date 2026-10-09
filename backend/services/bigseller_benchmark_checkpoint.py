"""Durable, account-bound checkpoints for the BigSeller SKU benchmark.

Only a newly created database is ever writable.  A resumed run reads its source
checkpoint and copies confirmed rows into a separate database, so concurrently
started runs cannot alter one another's recovery data.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import sqlite3
import errno
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4


CHECKPOINT_VERSION = 1
IDENTITY_FIELDS = (
    "source_file", "source_sha256", "sheet_name", "metric", "account_key", "skus",
)
ROW_FIELDS = (
    "sku", "shop_name", "shop_id", "item_id", "metric_value", "shop_count",
    "status", "message", "attempts", "queried_at", "retry_not_before",
)


class CheckpointError(ValueError):
    """A local diagnostic that never includes untrusted database contents."""


def _sqlite_primary_code(error: BaseException) -> int | None:
    code = getattr(error, "sqlite_errorcode", None)
    return (code & 0xFF) if isinstance(code, int) and not isinstance(code, bool) else None


def _storage_failure(error: BaseException, action: str) -> CheckpointError:
    """Describe known storage failures without echoing paths, SQL, or row data."""
    code = _sqlite_primary_code(error)
    sqlite_reasons = {
        sqlite3.SQLITE_BUSY: "进度文件被其他程序占用（SQLITE_BUSY），请关闭占用程序后继续补查",
        sqlite3.SQLITE_LOCKED: "进度文件被其他程序占用（SQLITE_LOCKED），请关闭占用程序后继续补查",
        sqlite3.SQLITE_FULL: "磁盘空间不足（SQLITE_FULL），请释放进度所在磁盘空间后继续补查",
        sqlite3.SQLITE_READONLY: "进度文件或所在目录不可写（SQLITE_READONLY），请检查文件权限或是否被移动",
        sqlite3.SQLITE_IOERR: "磁盘读写失败（SQLITE_IOERR），请检查磁盘连接及文件访问状态",
        sqlite3.SQLITE_CANTOPEN: "无法打开进度文件（SQLITE_CANTOPEN），请检查目录是否存在及访问权限",
        sqlite3.SQLITE_CORRUPT: "进度文件损坏（SQLITE_CORRUPT），请保留整个进度目录并联系维护人员",
        sqlite3.SQLITE_NOTADB: "进度文件格式异常（SQLITE_NOTADB），请保留整个进度目录并联系维护人员",
    }
    if isinstance(error, UnicodeError):
        reason = "结果文字编码异常，请联系维护人员检查返回数据"
    elif isinstance(error, sqlite3.Error):
        reason = sqlite_reasons.get(code, "SQLite 进度写入异常，请保留整个进度目录并联系维护人员")
    elif isinstance(error, OSError):
        if error.errno == errno.ENOSPC or getattr(error, "winerror", None) == 112:
            reason = "磁盘空间不足，请释放进度所在磁盘空间后继续补查"
        elif error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
            reason = "进度目录没有写入权限，请检查目录权限和文件保护设置"
        else:
            reason = "无法访问进度目录，请检查磁盘连接和目录访问状态"
    else:
        reason = "结果数据序列化异常，请联系维护人员检查返回数据"
    return CheckpointError(f"对标进度{action}失败：{reason}")


def _identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or any(key not in value for key in IDENTITY_FIELDS):
        raise CheckpointError("对标进度身份信息不完整，请重新开始查询")
    clean = {key: value[key] for key in IDENTITY_FIELDS}
    if any(not isinstance(clean[key], str) or not clean[key].strip()
           for key in IDENTITY_FIELDS if key != "skus"):
        raise CheckpointError("对标进度身份格式异常，请重新开始查询")
    if not Path(clean["source_file"]).is_absolute():
        raise CheckpointError("对标进度源文件必须使用绝对路径")
    if any(re.fullmatch(r"[0-9a-fA-F]{64}", clean[key]) is None
           for key in ("source_sha256", "account_key")):
        raise CheckpointError("对标进度文件或账号摘要格式异常")
    if clean["metric"] not in {"views", "sales"}:
        raise CheckpointError("对标进度指标格式异常")
    skus = clean["skus"]
    if not isinstance(skus, list) or any(not isinstance(sku, str) or not sku.strip() for sku in skus):
        raise CheckpointError("对标进度 SKU 列表格式异常")
    if len(skus) != len(set(skus)):
        raise CheckpointError("对标进度 SKU 列表存在重复项")
    clean["skus"] = list(skus)
    return clean


def _nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _row(value: Any, allowed_skus: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CheckpointError("对标进度结果格式异常，请重新查询")
    clean = {key: value[key] for key in ROW_FIELDS if key in value}
    sku = clean.get("sku")
    if not isinstance(sku, str) or sku not in allowed_skus:
        raise CheckpointError("对标进度包含不属于源表的 SKU，无法恢复")
    if not isinstance(clean.get("status"), str) or clean["status"] not in {"matched", "not_found", "failed"}:
        raise CheckpointError("对标进度查询状态异常，无法恢复")
    for key in ("shop_name", "message", "queried_at"):
        clean.setdefault(key, "")
        if not isinstance(clean[key], str):
            raise CheckpointError("对标进度结果文字格式异常，无法恢复")
    for key in ("shop_id", "item_id"):
        clean.setdefault(key, "")
        if isinstance(clean[key], bool) or not isinstance(clean[key], (str, int)):
            raise CheckpointError("对标进度商品或店铺编号格式异常，无法恢复")
        clean[key] = str(clean[key])
    for key in ("shop_count", "attempts"):
        clean.setdefault(key, 0)
        if not _nonnegative_integer(clean[key]):
            raise CheckpointError("对标进度计数格式异常，无法恢复")
    clean.setdefault("metric_value", None)
    if clean["metric_value"] is not None and not _nonnegative_integer(clean["metric_value"]):
        raise CheckpointError("对标进度商品指标格式异常，无法恢复")
    clean.setdefault("retry_not_before", 0.0)
    retry_at = clean["retry_not_before"]
    try:
        retry_valid = (not isinstance(retry_at, bool) and isinstance(retry_at, (int, float))
                       and math.isfinite(retry_at) and retry_at >= 0)
    except OverflowError:
        retry_valid = False
    if not retry_valid:
        raise CheckpointError("对标进度重试时间格式异常，无法恢复")
    clean["retry_not_before"] = float(retry_at)
    if clean["status"] == "matched":
        if not clean["shop_name"].strip() or not _nonnegative_integer(clean["metric_value"]):
            raise CheckpointError("对标进度已匹配结果缺少有效店铺或指标，无法恢复")
    elif (clean["shop_name"].strip() or clean["shop_id"].strip() or clean["item_id"].strip()
          or clean["metric_value"] is not None or clean["shop_count"] != 0):
        raise CheckpointError("对标进度未完成或未匹配结果含有旧店铺数据，无法恢复")
    return clean


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointError("对标进度内容存在重复字段，无法恢复")
        result[key] = value
    return result


def _parse(value: Any) -> Any:
    try:
        return json.loads(value, object_pairs_hook=_unique_object)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise CheckpointError("对标进度内容损坏，无法恢复，请重新查询") from None


class Checkpoint:
    def __init__(self, path: Path, connection: sqlite3.Connection, identity: dict[str, Any]):
        self.path = path
        self._connection: sqlite3.Connection | None = connection
        self._allowed_skus = set(identity["skus"])

    def save_result(self, row: dict[str, Any]) -> None:
        """Commit a completed SKU before the caller starts its next request."""
        self.save_results([row])

    def save_results(self, rows: Iterable[dict[str, Any]]) -> None:
        """Validate a batch completely, then commit all its rows atomically."""
        if self._connection is None:
            raise CheckpointError("对标进度文件已关闭，无法保存")
        clean_rows = [_row(row, self._allowed_skus) for row in rows]
        if len({row["sku"] for row in clean_rows}) != len(clean_rows):
            raise CheckpointError("本次保存的对标结果包含重复 SKU")
        try:
            encoded_rows = [(row["sku"], _json(row)) for row in clean_rows]
        except (ValueError, TypeError, OverflowError) as exc:
            raise _storage_failure(exc, "保存") from None
        for attempt in range(3):
            try:
                with self._connection:
                    self._connection.executemany(
                        "INSERT INTO results(sku, row_json) VALUES (?, ?) "
                        "ON CONFLICT(sku) DO UPDATE SET row_json=excluded.row_json",
                        encoded_rows,
                    )
                return
            except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError) as exc:
                # Each failed transaction has rolled back before retrying. Only
                # transient lock errors are retried; disk/data failures stop now.
                if (_sqlite_primary_code(exc) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                        and attempt < 2):
                    time.sleep(0.25 * (2 ** attempt))
                    continue
                raise _storage_failure(exc, "保存") from None

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Checkpoint:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def create_checkpoint(output_dir: Path, identity: dict[str, Any]) -> Checkpoint:
    clean_identity = _identity(identity)
    connection = None
    try:
        directory = Path(output_dir).expanduser().resolve() / "BigSeller对标进度"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"进度_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex}.sqlite3"
        # Exclusive creation is an additional safeguard against overwriting any
        # earlier run, even in the exceptionally unlikely event of an ID clash.
        with path.open("xb"):
            pass
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        with connection:
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("CREATE TABLE results (sku TEXT PRIMARY KEY, row_json TEXT NOT NULL)")
            connection.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)", [
                ("version", str(CHECKPOINT_VERSION)), ("identity", _json(clean_identity)),
            ])
        return Checkpoint(path, connection, clean_identity)
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        raise _storage_failure(exc, "创建") from None


def _read_rows(connection: sqlite3.Connection, expected: dict[str, Any]) -> dict[str, dict[str, Any]]:
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise CheckpointError("对标进度文件损坏，无法恢复，请重新查询")
    entries = connection.execute("SELECT key, value FROM metadata").fetchall()
    metadata = dict(entries)
    if len(metadata) != len(entries) or set(metadata) != {"version", "identity"}:
        raise CheckpointError("对标进度文件结构异常，无法恢复")
    if metadata.get("version") != str(CHECKPOINT_VERSION):
        raise CheckpointError("对标进度文件版本不受支持，请重新查询")
    if _identity(_parse(metadata["identity"])) != expected:
        raise CheckpointError("对标进度与当前源文件、工作表、指标、账号或 SKU 列表不一致，无法恢复")
    allowed_skus = set(expected["skus"])
    rows: dict[str, dict[str, Any]] = {}
    for sku, encoded in connection.execute("SELECT sku, row_json FROM results"):
        row = _row(_parse(encoded), allowed_skus)
        if sku != row["sku"] or sku in rows:
            raise CheckpointError("对标进度 SKU 重复或编号不一致，无法恢复")
        rows[sku] = row
    return rows


def read_checkpoint(path: Path, expected_identity: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Read and verify all rows; recovery never modifies the source database."""
    expected = _identity(expected_identity)
    connection = None
    try:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise CheckpointError("找不到对标进度文件，请重新选择")
        connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        try:
            return _read_rows(connection, expected)
        except sqlite3.Error as exc:
            if getattr(exc, "sqlite_errorname", "") != "SQLITE_READONLY_ROLLBACK":
                raise
        finally:
            connection.close()
            connection = None
        # A crash during the *next* transaction can leave a hot rollback journal.
        # SQLite must undo that incomplete transaction before a reader can see
        # prior commits. Recover an isolated copy; never write the user's source
        # checkpoint or let a resumed run become a second writer to that file.
        with TemporaryDirectory(prefix="bigseller_checkpoint_recovery_") as temporary:
            recovered = Path(temporary) / source.name
            shutil.copyfile(source, recovered)
            shutil.copyfile(Path(str(source) + "-journal"), Path(str(recovered) + "-journal"))
            connection = sqlite3.connect(recovered)
            try:
                return _read_rows(connection, expected)
            finally:
                connection.close()
                connection = None
    except (OSError, sqlite3.Error):
        raise CheckpointError("对标进度文件无法读取或已损坏，请重新选择有效的进度文件") from None
    finally:
        if connection is not None:
            connection.close()
