"""Download snapshot, completion detector and coupon file parser for Lazada Ads Monthly Report.

Implements rules from specification 2.15 and 2.16:
- Directory snapshot before download
- Stable file detection (no temp extensions, size verified across samples)
- CSV / XLSX parsing with Decimal summation
- Excludes Total/summary rows and non-detail rows
- Generates parse audit records
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

TEMP_EXTENSIONS = frozenset({".crdownload", ".tmp", ".part", ".download", ".partial"})
VALID_EXTENSIONS = frozenset({".xlsx", ".csv"})


class DownloadError(RuntimeError):
    def __init__(self, message: str, code: str = "download_invalid"):
        super().__init__(message)
        self.code = code


def compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def snapshot_directory(directory: Path) -> dict[str, float]:
    """Capture snapshot of existing files and their modification times."""
    if not directory.exists():
        return {}
    snapshot = {}
    try:
        for entry in directory.iterdir():
            if entry.is_file():
                try:
                    snapshot[entry.name] = entry.stat().st_mtime
                except OSError:
                    pass
    except OSError:
        pass
    return snapshot


def wait_for_download(
    directory: Path,
    initial_snapshot: dict[str, float],
    *,
    timeout: float = 180.0,
    check_interval: float = 1.0,
    stability_samples: int = 3,
    is_cancelled: Callable[[], bool] | None = None,
    expected_name: str | None = None,
) -> Path:
    """Wait for a newly downloaded file to appear and stabilize."""
    directory = Path(directory)
    if not directory.exists():
        raise DownloadError(f"下载目录不存在: {directory}", code="download_invalid")

    deadline = time.time() + timeout
    candidate: Path | None = None
    last_size = -1
    consecutive_stable_count = 0

    while time.time() < deadline:
        if is_cancelled and is_cancelled():
            raise DownloadError("用户取消了下载等待", code="cancelled")

        # Scan directory for new or modified files
        new_files: list[Path] = []
        try:
            for entry in directory.iterdir():
                if not entry.is_file():
                    continue
                name = entry.name
                mtime = entry.stat().st_mtime
                if name not in initial_snapshot or mtime > initial_snapshot[name] + 0.001:
                    new_files.append(entry)
        except OSError:
            pass

        # Check for temporary downloads still in progress
        temp_files = [f for f in new_files if f.suffix.lower() in TEMP_EXTENSIONS]
        completed_candidates = [
            f for f in new_files
            if f.suffix.lower() in VALID_EXTENSIONS and not f.name.startswith("~$")
        ]

        if expected_name:
            completed_candidates = [p for p in completed_candidates if p.name == expected_name]
        if len(completed_candidates) > 1:
            raise DownloadError("出现多个下载文件，无法确认本次导出来源", code="download_ambiguous")
        if completed_candidates:
            next_candidate = completed_candidates[0]
            if next_candidate != candidate:
                last_size, consecutive_stable_count = -1, 0
            candidate = next_candidate

            try:
                current_size = candidate.stat().st_size
            except OSError:
                current_size = -1

            if current_size > 0 and current_size == last_size and not temp_files:
                consecutive_stable_count += 1
                if consecutive_stable_count >= stability_samples:
                    # Verify file is readable and non-corrupted
                    if _verify_readable(candidate):
                        return candidate
            else:
                consecutive_stable_count = 0
                last_size = current_size

        time.sleep(check_interval)

    raise DownloadError("等待文件下载超时或文件未稳定生成", code="download_timeout")


def _verify_readable(file_path: Path) -> bool:
    """Test if file is complete and not locked or damaged."""
    try:
        if file_path.stat().st_size == 0:
            return False
        suffix = file_path.suffix.lower()
        if suffix in {".xlsx", ".zip"}:
            with zipfile.ZipFile(file_path, "r") as zf:
                if zf.testzip() is not None:
                    return False
        elif suffix == ".csv":
            with file_path.open("rb") as f:
                head = f.read(1024)
                if not head or head.startswith(b"<!DOCTYPE html") or head.startswith(b"<html"):
                    return False
        return True
    except Exception:
        return False


def _clean_header(text: Any) -> str:
    """Normalize header text: strip, remove newlines and extra spaces."""
    s = str(text or "")
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _parse_decimal(value: Any, default: Decimal | None = None) -> Decimal:
    """Parse string representation of decimal, stripping currency symbols and commas."""
    if isinstance(value, bool):
        raise InvalidOperation("布尔值不是金额")
    if value is None or value == "":
        if default is not None:
            return default
        raise InvalidOperation("空金额")
    s = str(value).strip()
    negative = s.startswith('(') and s.endswith(')')
    if negative:
        s = s[1:-1].strip()
    currency = r'(?:PHP|MYR|THB|IDR|VND|SGD|RM|Rp|S\$|₱|฿|₫|đ|\$)'
    s = re.sub(rf'^{currency}\s*|\s*{currency}$', '', s, flags=re.I).strip()
    if re.fullmatch(r'[-+]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?', s):
        s = s.replace(',', '')
    elif re.fullmatch(r'[-+]?\d{1,3}(?:\.\d{3})+,\d{1,2}', s):
        s = s.replace('.', '').replace(',', '.')
    elif re.fullmatch(r'[-+]?\d+,\d{1,2}', s):
        s = s.replace(',', '.')
    else:
        raise InvalidOperation('金额格式不明确或不是完整数值')
    if negative and s.startswith(('-', '+')):
        raise InvalidOperation('重复符号')
    amount = Decimal(s) * (-1 if negative else 1)
    if not amount.is_finite():
        raise InvalidOperation('金额非有限值')
    return amount


@dataclass
class ParseAudit:
    file_name: str
    sha256: str
    total_raw_rows: int
    detail_rows_count: int
    excluded_rows_count: int
    status_distribution: dict[str, int]
    total_amount: Decimal
    min_date: str | None = None
    max_date: str | None = None
    is_empty: bool = False
    audit_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_name": self.file_name,
            "sha256": self.sha256,
            "total_raw_rows": self.total_raw_rows,
            "detail_rows_count": self.detail_rows_count,
            "excluded_rows_count": self.excluded_rows_count,
            "status_distribution": self.status_distribution,
            "total_amount": str(self.total_amount),
            "min_date": self.min_date,
            "max_date": self.max_date,
            "is_empty": self.is_empty,
            "audit_notes": self.audit_notes,
        }


def parse_coupon_file(
    file_path: Path | str,
    target_month: str,  # "YYYY-MM"
    *, filters_verified: bool = False, expected_count: int | None = None, currency: str = '',
) -> tuple[Decimal, ParseAudit]:
    """Parse downloaded coupon transaction history file and sum recharge Amount.

    Target month: e.g. "2026-10", start "2026-10-01", end "2026-10-31".
    """
    path = Path(file_path).resolve()
    if not path.is_file() or not _verify_readable(path):
        raise DownloadError(f"优惠券交易文件不存在: {path}", code="download_invalid")

    sha256_hash = compute_sha256(path)
    suffix = path.suffix.lower()

    if suffix == ".xlsx":
        result = _parse_coupon_xlsx(path, target_month, sha256_hash, filters_verified=filters_verified, currency=currency)
    elif suffix == ".csv":
        result = _parse_coupon_csv(path, target_month, sha256_hash, filters_verified=filters_verified, currency=currency)
    else:
        raise DownloadError(f"不支持的优惠券文件类型: {suffix}", code="download_invalid")
    if expected_count is not None and result[1].detail_rows_count != expected_count:
        raise DownloadError("导出明细行数与页面总记录数不一致，完整性未通过", code="download_incomplete")
    return result


def _find_column_index(headers: list[str], keywords: list[str]) -> int | None:
    aliases = {kw.lower().replace(' ', '') for kw in keywords}
    found = [i for i, h in enumerate(headers) if _clean_header(h).lower().replace(' ', '') in aliases]
    if len(found) > 1:
        raise DownloadError(f"文件存在重复或歧义列: {keywords[0]}", code="column_ambiguous")
    return found[0] if found else None


def _is_summary_row(row_values: list[Any]) -> bool:
    first_non_empty = ""
    for v in row_values:
        s = _clean_header(v)
        if s:
            first_non_empty = s
            break
    if not first_non_empty:
        return True
    pattern = re.compile(r"^(total|合计|总计|summary)\s*:?$", re.IGNORECASE)
    return bool(pattern.search(first_non_empty))


def _extract_date_str(val: Any) -> str | None:
    if val is None or val == "":
        return None
    if isinstance(val, (datetime, date)):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    match = re.search(r"\b(\d{4}[-/]\d{1,2}[-/]\d{1,2})\b", s)
    if match:
        parts = re.split(r"[-/]", match.group(1))
        try:
            return date(*map(int, parts)).isoformat()
        except ValueError:
            return None
    # Transaction History displays/exports day/month/year, as verified on the site.
    match = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})(?:\s|$)', s)
    if match:
        day, month, year = map(int, match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def _parse_coupon_rows(
    headers: list[str],
    rows: list[list[Any]],
    target_month: str,
    file_name: str,
    sha256_hash: str,
    *, filters_verified: bool = False, currency: str = '',
) -> tuple[Decimal, ParseAudit]:
    # Locate Amount column
    amount_col = _find_column_index(headers, ["Amount", "金额", "交易金额", "充值金额"])
    if amount_col is None:
        raise DownloadError(f"未在文件 {file_name} 中找到 Amount/金额 列，表头为: {headers}", code="amount_invalid")

    # Locate Date, Wallet, Type, Status columns
    date_col = _find_column_index(headers, ["Date & Time", "Date", "Transaction Date", "Create Time", "交易时间", "时间", "日期", "日期&时间", "日期和时间"])
    wallet_col = _find_column_index(headers, ["Wallet Type", "Wallet", "钱包类型", "钱包"])
    # Lazada exports Segmentation as transaction type and a separate lowercase
    # type as category. Generic Type is only a fallback for older export schemas.
    type_col = _find_column_index(headers, ["Segmentation", "Transaction Type", "交易类型"])
    if type_col is None:
        type_col = _find_column_index(headers, ["Type", "类型"])
    status_col = _find_column_index(headers, ["Status", "状态", "交易状态"])
    currency_col = _find_column_index(headers, ['Currency', '币种', '货币'])
    if not filters_verified and any(c is None for c in (date_col, wallet_col, type_col)):
        raise DownloadError("文件缺少日期/钱包/类型列，且没有已核实的页面筛选证据", code="filters_unverified")

    total_amount = Decimal("0")
    total_raw_rows = len(rows)
    detail_count = 0
    excluded_count = 0
    status_dist: dict[str, int] = {}
    audit_notes: list[str] = []
    dates_found: list[str] = []

    for row_idx, row in enumerate(rows, start=1):
        # Pad row to headers length
        if len(row) < len(headers):
            row = list(row) + [""] * (len(headers) - len(row))

        if _is_summary_row(row):
            excluded_count += 1
            audit_notes.append(f"第 {row_idx} 行为汇总/空行，已排除")
            continue

        raw_amount_str = str(row[amount_col] if amount_col < len(row) else "").strip()
        codes = re.findall(r'\b(?:PHP|MYR|THB|IDR|VND|SGD|USD)\b', raw_amount_str, re.I)
        if currency_col is not None:
            codes.append(str(row[currency_col]).strip())
        if currency and any(code.upper() != currency for code in codes):
            raise DownloadError('优惠券文件币种与店铺站点不一致', code='currency_unverified')
        if not raw_amount_str or row[amount_col] is None:
            raise DownloadError(f"第 {row_idx} 行明细金额为空", code="amount_invalid")

        # Check date constraint if date column exists
        if date_col is not None and date_col < len(row):
            date_val = _extract_date_str(row[date_col])
            if not date_val:
                raise DownloadError(f"第 {row_idx} 行交易日期无法核实", code="date_unverified")
            if date_val:
                dates_found.append(date_val)
                if not date_val.startswith(target_month):
                    raise DownloadError(
                        f"第 {row_idx} 行交易日期 {date_val} 不属于目标月份 {target_month}",
                        code="date_unverified",
                    )

        for column, accepted, label in (
            (wallet_col, {'coupon', 'coupons', '优惠券', '优惠券钱包'}, '钱包'),
            (type_col, {'topup', 'top up', 'top-up', 'recharge', '充值'}, '类型'),
        ):
            if column is not None and _clean_header(row[column]).lower() not in accepted:
                raise DownloadError(f"第 {row_idx} 行{label}与优惠券充值筛选不一致", code="filters_unverified")

        # Record status
        if status_col is not None and status_col < len(row):
            st = _clean_header(row[status_col])
            status_dist[st] = status_dist.get(st, 0) + 1

        try:
            amt = _parse_decimal(raw_amount_str)
        except Exception as exc:
            raise DownloadError(f"第 {row_idx} 行 Amount 解析失败: '{raw_amount_str}'", code="amount_invalid") from exc

        total_amount += amt
        detail_count += 1

    dates_found.sort()
    min_date = dates_found[0] if dates_found else None
    max_date = dates_found[-1] if dates_found else None
    is_empty = (detail_count == 0)

    audit = ParseAudit(
        file_name=file_name,
        sha256=sha256_hash,
        total_raw_rows=total_raw_rows,
        detail_rows_count=detail_count,
        excluded_rows_count=excluded_count,
        status_distribution=status_dist,
        total_amount=total_amount,
        min_date=min_date,
        max_date=max_date,
        is_empty=is_empty,
        audit_notes=audit_notes,
    )
    return total_amount, audit


def _parse_coupon_xlsx(path: Path, target_month: str, sha256_hash: str, **options) -> tuple[Decimal, ParseAudit]:
    from openpyxl import load_workbook

    try:
        # Avoid read_only=True because openpyxl truncates columns if the worksheet XML lacks <dimension> tags
        wb = load_workbook(filename=str(path), data_only=False)
    except Exception as exc:
        raise DownloadError(f"无法读取 XLSX 工作簿: {exc}", code="download_invalid") from exc

    try:
        if len(wb.worksheets) != 1:
            raise DownloadError("导出包含多个工作表，需明确明细表后解析")
        sheet = wb.active
        if any(c.data_type == 'f' for row in sheet for c in row):
            raise DownloadError("导出明细包含公式，不能作为原始金额解析")
        all_rows = list(sheet.iter_rows(values_only=True))
    finally:
        wb.close()

    if not all_rows:
        raise DownloadError("工作表没有有效表头，不能判定零金额")

    headers = [_clean_header(c) for c in all_rows[0]]
    data_rows = [list(r) for r in all_rows[1:]]
    return _parse_coupon_rows(headers, data_rows, target_month, path.name, sha256_hash, **options)


def _parse_coupon_csv(path: Path, target_month: str, sha256_hash: str, **options) -> tuple[Decimal, ParseAudit]:
    encodings = ["utf-8-sig", "gb18030"]
    content = None
    for enc in encodings:
        try:
            with path.open("r", encoding=enc) as f:
                content = f.read()
            break
        except UnicodeDecodeError:
            continue

    if content is None:
        raise DownloadError(f"无法以支持的编码解析 CSV 文件: {path.name}", code="download_invalid")

    reader = csv.reader(io.StringIO(content))
    all_rows = list(reader)

    if not all_rows:
        raise DownloadError("CSV 没有有效表头，不能判定零金额")

    headers = [_clean_header(c) for c in all_rows[0]]
    data_rows = all_rows[1:]
    return _parse_coupon_rows(headers, data_rows, target_month, path.name, sha256_hash, **options)
