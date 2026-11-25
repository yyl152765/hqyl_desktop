"""Batch lifecycle for Shopee ID monthly income XLSX and all Master/Tax Invoice PDFs."""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import shutil
from typing import Any, Callable, Mapping
import unicodedata
from uuid import uuid4

from backend.services.shopee_id_invoice_gateway import (
    InvoiceExportError,
    REQUIRED_ACTIONS,
    safe_slug,
    validate_pdf,
)
from backend.services.temu_on_sale_export import (
    batch_lock,
    writable_directory,
    write_json,
)
from backend.services.shopee_id_income_report import INCOME_REPORT_TYPE, validate_income_xlsx


SCHEMA = "shopee_id_monthly_invoice_v2"
LEGACY_SCHEMA = "shopee_id_monthly_invoice_v1"
TERMINAL = {"success"}
MAX_STORES = 200

_OTHER_COUNTRY_CN_RE = re.compile(
    r"泰国|菲律宾|越南|马来(?:西亚)?|新加坡|台湾|巴西|墨西哥|哥伦比亚|智利"
)
_OTHER_COUNTRY_CODE_RE = re.compile(
    r"(?:^|[\s\-_]|sp|shopee)(?:th|ph|vn|my|sg|tw|br|mx|co|cl)(?:\d+|[\s\-_]|$)",
    re.IGNORECASE,
)
_OTHER_PLATFORM_NAME_RE = re.compile(
    r"(?:^|[\s\-_])(?:lz|lazada|lzmall|lz跨境|temu|tk|tiktok|bigseller)(?:[\s\-_\d]|$)",
    re.IGNORECASE,
)
_INDONESIA_MARKER_RE = re.compile(
    r"印尼|印度尼西亚|\bindonesia\b|\.co\.id\b|(?:^|[\s\-_]|sp|shopee|虾皮)id(?:\d+|[\s\-_]|$)",
    re.IGNORECASE,
)
_ID_SHOP_NUMBER_PRIMARY_RE = re.compile(
    r"(?:印尼|印度尼西亚)(?:本土|跨境|仓发|企业|自营|官方|商城|mall|店|号|[\s\-_])*(?<!\d)(\d+)(?!\d)",
    re.IGNORECASE,
)
_ID_SHOP_NUMBER_FALLBACK_RE = re.compile(
    r"(?:^|[\s\-_])(?:sp|shopee|虾皮)?[\s\-_]*id(?:本土|跨境|仓发|企业|mall|店|号|[\s\-_])*(?<!\d)(\d+)(?!\d)",
    re.IGNORECASE,
)
_SHORTHAND_ID_RE = re.compile(
    r"^\s*(?:sp|shopee|虾皮)?[\s\-_]*(?:印尼|印度尼西亚|indonesia|id)"
    r"(?:本土|跨境|仓发|企业|自营|官方|商城|mall|[\s\-_])*(?<!\d)(\d+)(?!\d)[\s号店]*$",
    re.IGNORECASE,
)
_SHORTHAND_OTHER_COUNTRY_RE = re.compile(
    r"^\s*(?:lz|lazada|sp|shopee|虾皮|tk|tiktok)?[\s\-_]*"
    r"(?:泰国|菲律宾|越南|马来(?:西亚)?|新加坡|台湾|巴西|墨西哥|哥伦比亚|智利|th|ph|vn|my|sg|tw|br|mx|co|cl)"
    r"(?:本土|跨境|仓发|企业|自营|官方|商城|mall|[\s\-_])*(?<!\d)(\d+)(?!\d)[\s号店]*$",
    re.IGNORECASE,
)


def _normalize_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).strip()


def default_month(today: date | None = None) -> str:
    """Return the previous completed calendar month in YYYY-MM format."""
    current = today or date.today()
    last_month_end = current.replace(day=1) - timedelta(days=1)
    return last_month_end.strftime("%Y-%m")


def validate_month(value: Any, *, today: date | None = None) -> str:
    """Validate YYYY-MM and reject current uncompleted or future months."""
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", text)
    if not match:
        raise ValueError("账单月份格式必须为 YYYY-MM")
    year, month = int(match.group(1)), int(match.group(2))
    date(year, month, 1)
    current = today or date.today()
    if (year, month) >= (current.year, current.month):
        raise ValueError(f"所选月份 {text} 尚未完整结束，仅支持历史完整自然月")
    return f"{year:04d}-{month:02d}"


def parse_store_names(value: Any) -> list[str]:
    """Parse and deduplicate store names while rejecting empty or excessive lists."""
    if isinstance(value, str):
        raw_lines = value.splitlines()
    elif isinstance(value, (list, tuple)):
        raw_lines = list(value)
    else:
        raise ValueError("请输入店铺名称，每行一个")

    names: list[str] = []
    seen: set[str] = set()
    for item in raw_lines:
        cleaned = _normalize_text(item)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key not in seen:
            seen.add(key)
            names.append(cleaned)
    if not names:
        raise ValueError("请至少填写一个 Shopee 印尼店铺名或店号简称，每行一个")
    if len(names) > MAX_STORES:
        raise ValueError(f"单批最多支持 {MAX_STORES} 个店铺")
    return names


def validate_request(
    payload: Mapping[str, Any],
    *,
    default_output_dir: str = "",
    today: date | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("请求参数必须是对象")
    raw_month = payload.get("month")
    if raw_month is None:
        month = validate_month(default_month(today), today=today)
    else:
        month = validate_month(raw_month, today=today)

    raw_stores = payload.get("store_names")
    names = parse_store_names(raw_stores)
    if isinstance(raw_stores, str):
        non_empty = [_normalize_text(line) for line in raw_stores.splitlines() if _normalize_text(line)]
    elif isinstance(raw_stores, (list, tuple)):
        non_empty = [_normalize_text(line) for line in raw_stores if _normalize_text(line)]
    else:
        non_empty = names
    if len(non_empty) != len(names):
        raise ValueError("店铺名称不能重复，请删除重复项")

    raw_output = str(payload.get("output_dir") or default_output_dir or "").strip()
    output_dir = writable_directory(raw_output)
    return {
        "month": month,
        "store_names": names,
        "output_dir": str(output_dir),
    }


def is_shopee_store(store: Mapping[str, Any]) -> bool:
    platform = _normalize_text(
        store.get("platform_name") or store.get("platformName") or store.get("platform") or ""
    ).casefold()
    store_name = _normalize_text(store.get("store_name") or store.get("browserName") or "")
    if not any(token in platform for token in ("shopee", "虾皮")):
        return False
    if _OTHER_PLATFORM_NAME_RE.search(store_name):
        return False
    return True


def classify_store_country(store: Mapping[str, Any]) -> str:
    platform = _normalize_text(store.get("platform_name") or "")
    if _OTHER_COUNTRY_CN_RE.search(platform) or _OTHER_COUNTRY_CODE_RE.search(platform):
        return "OTHER"
    explicit = _normalize_text(store.get("site") or store.get("country") or "").upper()
    if explicit and explicit not in {"ID", "INDONESIA", "印尼", "印度尼西亚"}:
        return "OTHER"
    store_name = _normalize_text(store.get("store_name") or store.get("browserName") or "")
    if _OTHER_COUNTRY_CN_RE.search(store_name) or _OTHER_COUNTRY_CODE_RE.search(store_name):
        return "OTHER"
    if (explicit in {"ID", "INDONESIA", "印尼", "印度尼西亚"}
            or _INDONESIA_MARKER_RE.search(platform) or _INDONESIA_MARKER_RE.search(store_name)):
        return "ID"
    return "UNKNOWN"


def extract_id_shop_numbers(store_name: str) -> set[int]:
    """Extract delimited Indonesia shop numbers from a ZiNiao store name.

    A complete digit token is required so 046 (46) never matches 0460 (460).
    """
    text = _normalize_text(store_name)
    if not text:
        return set()
    primary = {int(match.group(1)) for match in _ID_SHOP_NUMBER_PRIMARY_RE.finditer(text)}
    if primary:
        return primary
    return {int(match.group(1)) for match in _ID_SHOP_NUMBER_FALLBACK_RE.finditer(text)}


def _parse_shorthand(query: str) -> tuple[str, int] | None:
    text = _normalize_text(query)
    if not text:
        return None
    match_id = _SHORTHAND_ID_RE.match(text)
    if match_id:
        return ("ID", int(match_id.group(1)))
    match_other = _SHORTHAND_OTHER_COUNTRY_RE.match(text)
    if match_other:
        return ("OTHER", int(match_other.group(1)))
    if re.fullmatch(r"\d+", text):
        return ("MISSING_COUNTRY", int(text))
    return None


def match_stores(names: Any, catalog: list[dict[str, Any]]) -> dict[str, Any]:
    """Match requested names against ZiNiao catalog by exact full name or unique ID shorthand."""
    requested = parse_store_names(names)
    sanitized_catalog: list[dict[str, Any]] = []
    seen_catalog_ids: set[str] = set()
    for row in catalog:
        if not isinstance(row, dict):
            raise ValueError("紫鸟店铺列表包含无效记录")
        store_id = str(row.get("store_id") or "").strip()
        store_name = _normalize_text(row.get("store_name") or "")
        platform_name = _normalize_text(row.get("platform_name") or "")
        if not store_id or not store_name or store_id in seen_catalog_ids:
            raise ValueError("紫鸟店铺列表缺少稳定 ID、完整名称或存在重复 ID")
        seen_catalog_ids.add(store_id)
        sanitized_catalog.append({
            **row,
            "store_id": store_id,
            "store_name": store_name,
            "platform_name": platform_name,
        })

    matched: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for name in requested:
        norm_key = _normalize_text(name).casefold()
        exact_matches = [
            store for store in sanitized_catalog
            if _normalize_text(store["store_name"]).casefold() == norm_key
        ]
        if len(exact_matches) > 1:
            issues.append({
                "name": name,
                "error": "存在同名店铺，请先在紫鸟中修改为不同名称后重试",
                "candidates": [store["store_name"] for store in exact_matches],
            })
            continue

        if len(exact_matches) == 1:
            store = exact_matches[0]
            if not is_shopee_store(store):
                issues.append({
                    "name": name,
                    "error": "紫鸟平台信息未确认是 Shopee",
                    "candidates": [store["store_name"]],
                })
                continue
            country = classify_store_country(store)
            if country == "OTHER":
                issues.append({
                    "name": name,
                    "error": "店铺站点不是印尼，仅支持 Shopee 印尼站点店铺",
                    "candidates": [store["store_name"]],
                })
                continue
            if country != "ID":
                issues.append({
                    "name": name,
                    "error": "店铺名称未确认为印尼站点，请核对紫鸟店铺名称",
                    "candidates": [store["store_name"]],
                })
                continue
            if store["store_id"] in used_ids:
                issues.append({
                    "name": name,
                    "error": "多个输入匹配到同一店铺，请删除重复项",
                    "candidates": [store["store_name"]],
                })
                continue
            matched.append({**copy.deepcopy(store), "requested_name": name})
            used_ids.add(store["store_id"])
            continue

        shorthand = _parse_shorthand(name)
        if shorthand is None:
            issues.append({
                "name": name,
                "error": "未匹配到完整紫鸟店铺名；如需按简称匹配请使用“印尼+店号”（例如 印尼046）",
                "candidates": [],
            })
            continue

        kind, target_number = shorthand
        if kind == "OTHER":
            issues.append({
                "name": name,
                "error": "仅支持 Shopee 印尼站点店铺，不支持其他国家站点",
                "candidates": [],
            })
            continue
        if kind == "MISSING_COUNTRY":
            issues.append({
                "name": name,
                "error": "店号简称必须包含印尼站点标识（例如 印尼046），不能仅输入纯数字",
                "candidates": [],
            })
            continue

        shopee_id_candidates = [
            store
            for store in sanitized_catalog
            if is_shopee_store(store)
            and classify_store_country(store) == "ID"
            and extract_id_shop_numbers(store["store_name"]) == {target_number}
        ]
        if len(shopee_id_candidates) > 1:
            candidate_names = [store["store_name"] for store in shopee_id_candidates]
            issues.append({
                "name": name,
                "error": f"简称匹配到多个 Shopee 印尼店铺（{'、'.join(candidate_names[:5])}），请填写完整紫鸟店铺名",
                "candidates": candidate_names,
            })
            continue

        if len(shopee_id_candidates) == 1:
            store = shopee_id_candidates[0]
            if store["store_id"] in used_ids:
                issues.append({
                    "name": name,
                    "error": "多个输入匹配到同一店铺，请删除重复项",
                    "candidates": [store["store_name"]],
                })
                continue
            matched.append({**copy.deepcopy(store), "requested_name": name})
            used_ids.add(store["store_id"])
            continue

        non_shopee_same_number = [
            store
            for store in sanitized_catalog
            if not is_shopee_store(store)
            and classify_store_country(store) == "ID"
            and extract_id_shop_numbers(store["store_name"]) == {target_number}
        ]
        if non_shopee_same_number:
            issues.append({
                "name": name,
                "error": "紫鸟平台信息未确认是 Shopee，不能跨平台匹配店铺",
                "candidates": [store["store_name"] for store in non_shopee_same_number],
            })
        else:
            issues.append({
                "name": name,
                "error": "当前紫鸟账号下未找到对应的 Shopee 印尼店铺，请核对店号或完整名称",
                "candidates": [],
            })

    return {
        "stores": matched,
        "issues": issues,
        "can_start": bool(matched) and not issues,
    }


def create_batch(
    output_dir: str | Path,
    profile: str,
    month: str,
    stores: list[dict[str, Any]],
) -> Path:
    validated_month = validate_month(month)
    root_dir = writable_directory(output_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = uuid4().hex[:10]
    batch_dir = root_dir / validated_month / f"{stamp}_{run_id[:6]}"
    batch_dir.mkdir(parents=True, exist_ok=False)
    (batch_dir / "invoices").mkdir()
    (batch_dir / "logs").mkdir()
    manifest_path = batch_dir / "manifest.json"
    record = {
        "schema": SCHEMA,
        "run_id": batch_dir.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "profile": profile,
        "month": validated_month,
        "status": "pending",
        "error": "",
        "stores": [
            {
                "store_id": str(store["store_id"]),
                "store_name": str(store["store_name"]),
                "requested_name": str(store.get("requested_name") or store["store_name"]),
                "platform_name": str(store.get("platform_name") or "Shopee"),
                "status": "pending",
                "attempts": 0,
                "row_count": None,
                "file_count": 0,
                "income_report_count": 0,
                "master_invoice_count": 0,
                "tax_invoice_count": 0,
                "files": [],
                "rows": [],
                "evidence": {},
                "error": "",
            }
            for store in stores
        ],
    }
    write_json(manifest_path, record)
    return manifest_path


def load_batch(path: str | Path, profile: str | None = None) -> dict[str, Any]:
    manifest_path = Path(path).resolve()
    if manifest_path.name != "manifest.json":
        raise ValueError("请选择该批次的 manifest.json")
    try:
        record = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取税票批次清单：{exc}") from None
    if not isinstance(record, dict) or record.get("schema") not in {SCHEMA, LEGACY_SCHEMA}:
        raise ValueError("不是有效的 Shopee 印尼月度税票批次清单")
    if profile is not None and record.get("profile") != profile:
        raise ValueError("所选紫鸟账号与此批次不一致，请切换回原账号或重新开始下载")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(record.get("month") or "")):
        raise ValueError("批次月份格式无效")
    stores = record.get("stores")
    if not isinstance(stores, list) or not stores or len(stores) > MAX_STORES:
        raise ValueError("批次店铺清单无效")
    invoices_root = (manifest_path.parent / "invoices").resolve()
    seen_ids: set[str] = set()
    for store in stores:
        if not isinstance(store, dict) or not store.get("store_id") or not store.get("store_name"):
            raise ValueError("批次缺少稳定店铺 ID 或完整店铺名称")
        store_id = str(store["store_id"])
        if store_id in seen_ids:
            raise ValueError("批次包含重复店铺 ID")
        seen_ids.add(store_id)
        if store.get("status") not in {"pending", "running", "success", "no_data", "failed"}:
            raise ValueError("批次店铺状态无效")
        for file_entry in store.get("files") or []:
            if not isinstance(file_entry, dict) or not file_entry.get("path"):
                raise ValueError("批次文件记录无效")
            rel = Path(str(file_entry["path"]))
            resolved = (manifest_path.parent / rel).resolve()
            if rel.is_absolute() or not resolved.is_relative_to(invoices_root):
                raise ValueError("税票文件路径超出该批次目录")
    if record["schema"] == LEGACY_SCHEMA:
        record.update(schema=SCHEMA, migrated_from=LEGACY_SCHEMA, status="incomplete",
                      error="此批次由旧版仅下载税票；请重试补齐整月收入 Excel，原文件保留")
        for store in stores:
            store["income_report_count"] = 0
            if store["status"] in {"success", "no_data"}:
                store.update(status="failed", error="旧版批次尚未下载整月收入 Excel")
    return record


def public_result(path: str | Path, record: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest_path = Path(path).resolve()
    record = record or load_batch(manifest_path)
    stores = copy.deepcopy(record["stores"])
    output_files: list[str] = []
    for store in stores:
        store_dir = ""
        for file_entry in store.get("files") or []:
            if file_entry.get("path"):
                abs_path = (manifest_path.parent / file_entry["path"]).resolve()
                file_entry["path"] = str(abs_path)
                output_files.append(str(abs_path))
                if not store_dir:
                    # Both Income_Report/file.xlsx and row_N/Type/file.pdf live under attempt_N.
                    store_dir = str(next((p for p in abs_path.parents if re.fullmatch(r"attempt_\d+", p.name)), abs_path.parent))
        store["store_output_dir"] = store_dir

    count = lambda status: sum(store["status"] == status for store in stores)
    success_count = count("success")
    no_data_count = sum(s["status"] == "success" and (s.get("evidence") or {}).get("empty_text") == "No receipt found" for s in stores)
    failed_count = count("failed")
    completed_count = success_count
    total_rows = sum(int(store.get("row_count") or 0) for store in stores if store["status"] == "success")
    master_count = sum(int(store.get("master_invoice_count") or 0) for store in stores)
    tax_count = sum(int(store.get("tax_invoice_count") or 0) for store in stores)
    income_count = sum(int(store.get("income_report_count") or 0) for store in stores)
    file_count = sum(int(store.get("file_count") or 0) for store in stores)
    complete = (record.get("status") == "complete" and success_count == len(stores)
                and all(int(s.get("income_report_count") or 0) == 1 for s in stores))

    if complete:
        message = f"全部 {success_count} 个店铺已完成：收入 Excel {income_count} 份，账单和税票 PDF {master_count + tax_count} 份"
        if no_data_count:
            message += f"（其中 {no_data_count} 家当月无税票，收入 Excel 已保存）"
    elif record.get("status") in {"pending", "running"}:
        message = f"正在逐店下载：已完成 {completed_count}/{len(stores)} 个店铺"
    else:
        message = (
            record.get("error")
            or f"部分结果：已完成 {completed_count}/{len(stores)} 个店铺（成功 {success_count}，无税票 {no_data_count}，失败 {failed_count}）"
        )

    return {
        "run_id": record["run_id"],
        "profile": record["profile"],
        "month": record["month"],
        "status": record["status"],
        "manifest_path": str(manifest_path),
        "output_dir": str(manifest_path.parent),
        "stores": stores,
        "output_files": output_files,
        "store_count": len(stores),
        "success_count": success_count,
        "no_data_count": no_data_count,
        "failed_count": failed_count,
        "completed_count": completed_count,
        "row_count": total_rows,
        "file_count": file_count,
        "master_invoice_count": master_count,
        "tax_invoice_count": tax_count,
        "income_report_count": income_count,
        "pdf_count": master_count + tax_count,
        "is_complete": complete,
        "completion_message": message,
        "can_retry": not complete and record.get("status") != "running",
    }


def fail_batch(path: str | Path, profile: str | None, error: str) -> dict[str, Any]:
    manifest_path = Path(path).resolve()
    record = load_batch(manifest_path, profile)
    record["status"] = "failed"
    record["error"] = str(error)
    for store in record["stores"]:
        if store["status"] in {"pending", "running"}:
            store["status"] = "failed"
            store["error"] = str(error)
    write_json(manifest_path, record)
    return public_result(manifest_path, record)


def _validate_completeness(files: list[dict[str, Any]], row_count: int, evidence: dict[str, Any]) -> None:
    if row_count < 0 or len(files) != 1 + row_count * len(REQUIRED_ACTIONS):
        raise ValueError("下载结果需包含一份整月收入 Excel，以及每行 Master Invoice 和 Tax Invoice PDF")
    income = [f for f in files if f["invoice_type"] == INCOME_REPORT_TYPE]
    if len(income) != 1 or income[0]["row_index"] != 0:
        raise ValueError("下载结果缺少唯一的整月收入 Excel")
    if len({f["path"] for f in files}) != len(files):
        raise ValueError("下载结果存在重复文件路径")
    by_row: dict[int, set[str]] = {}
    for item in files:
        if item["invoice_type"] != INCOME_REPORT_TYPE:
            by_row.setdefault(item["row_index"], set()).add(item["invoice_type"])
    if set(by_row) != set(range(1, row_count + 1)) or any(v != set(REQUIRED_ACTIONS) for v in by_row.values()):
        raise ValueError("部分税票行缺少 Master Invoice 或 Tax Invoice")
    if row_count == 0 and evidence.get("empty_text") != "No receipt found":
        raise ValueError("缺少页面 No receipt found 空状态确认")


def _verify_saved_store_files(batch_root: Path, store: dict[str, Any], month: str) -> None:
    files = store.get("files") or []
    _validate_completeness(files, int(store.get("row_count") or 0), store.get("evidence") or {})
    for entry in files:
        file_path = (batch_root / str(entry.get("path") or "")).resolve()
        info = (validate_income_xlsx(file_path, month) if entry["invoice_type"] == INCOME_REPORT_TYPE
                else validate_pdf(file_path))
        if info["sha256"] != entry.get("sha256"):
            raise ValueError(f"文件校验值发生变化：{file_path.name}")


def _normalize_store_files(
    batch_root: Path, attempt_dir: Path, files: list[dict[str, Any]], month: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in files:
        raw_path = Path(str(item.get("path") or "")).resolve()
        if not raw_path.is_relative_to(attempt_dir.resolve()):
            raise ValueError("下载的文件不在本店本次尝试目录内")
        kind = str(item.get("invoice_type") or "")
        if kind not in {*REQUIRED_ACTIONS, INCOME_REPORT_TYPE}:
            raise ValueError("下载文件类型无效")
        verified = validate_income_xlsx(raw_path, month) if kind == INCOME_REPORT_TYPE else validate_pdf(raw_path)
        if item.get("sha256") and verified["sha256"] != item["sha256"]:
            raise ValueError("下载文件 SHA-256 校验不一致")
        entry = {
            "row_index": int(item.get("row_index") or 0),
            "description": str(item.get("description") or ""),
            "amount": str(item.get("amount") or ""),
            "date": str(item.get("date") or ""),
            "invoice_type": kind,
            "invoice_label": str(item.get("invoice_label") or ""),
            "filename": verified["filename"],
            "path": str(raw_path.relative_to(batch_root)),
            "size": verified["size"],
            "sha256": verified["sha256"],
        }
        if kind == INCOME_REPORT_TYPE:
            source = item.get("report_source")
            if source not in {"history", "generated"}:
                raise ValueError("收入报表缺少历史复用或新建来源记录")
            entry.update(period_start=verified["period_start"], period_end=verified["period_end"],
                         currency=verified["currency"], report_source=source)
        normalized.append(entry)
    return normalized


def _file_counts(files: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "file_count": len(files),
        "income_report_count": sum(f["invoice_type"] == INCOME_REPORT_TYPE for f in files),
        "master_invoice_count": sum(f["invoice_type"] == "master_invoice" for f in files),
        "tax_invoice_count": sum(f["invoice_type"] == "tax_invoice" for f in files),
    }


def run_batch(
    path: str | Path,
    gateway: Any,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    manifest_path = Path(path).resolve()
    with batch_lock(manifest_path):
        record = load_batch(manifest_path, gateway.profile)
        if record.get("migrated_from") == LEGACY_SCHEMA:
            backup = manifest_path.with_name("manifest.tax_only_v1.json")
            if not backup.exists():
                shutil.copyfile(manifest_path, backup)

        def save() -> None:
            record["updated_at"] = datetime.now().isoformat(timespec="seconds")
            write_json(manifest_path, record)

        def log(message: str) -> None:
            with (manifest_path.parent / "logs" / "run.log").open("a", encoding="utf-8") as stream:
                stream.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
            if progress:
                progress(message)

        if hasattr(gateway, "progress"):
            gateway.progress = log
        record.update(status="running", error="")
        for store in record["stores"]:
            if store["status"] == "success":
                try:
                    _verify_saved_store_files(manifest_path.parent, store, record["month"])
                except Exception as exc:
                    store.update(status="failed", error=f"历史税票文件无法复用：{exc}")
                else:
                    store.update(**_file_counts(store.get("files") or []))
            elif store["status"] == "no_data":
                store.update(status="failed", error="尚未确认整月收入 Excel，请重试补下载")
        save()

        try:
            gateway.preflight(record["stores"])
        except Exception as exc:
            for store in record["stores"]:
                if store["status"] not in TERMINAL:
                    store.update(status="failed", error=str(exc))
            record["error"] = str(exc)
            log(f"运行前检查失败：{exc}")
        else:
            for index, store in enumerate(record["stores"], 1):
                if store["status"] in TERMINAL:
                    continue
                attempts = int(store.get("attempts") or 0) + 1
                store.update(
                    status="running",
                    attempts=attempts,
                    row_count=None,
                    file_count=0,
                    income_report_count=0,
                    master_invoice_count=0,
                    tax_invoice_count=0,
                    files=[],
                    rows=[],
                    evidence={},
                    error="",
                )
                slug = safe_slug(store["store_name"], 24)
                attempt_dir = (
                    manifest_path.parent
                    / "invoices"
                    / f"{index:03d}_{slug}"
                    / f"attempt_{attempts}"
                )
                save()
                log(f"[{index}/{len(record['stores'])}] {store['store_name']}：开始下载 {record['month']} 收入账单与税票")
                result: dict[str, Any] = {}
                try:
                    attempt_dir.mkdir(parents=True, exist_ok=False)
                    result = gateway.export(store, record["month"], attempt_dir)
                    row_count = int(result.get("row_count") or 0)
                    evidence = copy.deepcopy(result.get("evidence") or {})
                    normalized_files = _normalize_store_files(
                        manifest_path.parent, attempt_dir, result.get("files") or [], record["month"]
                    )
                    _validate_completeness(normalized_files, row_count, evidence)
                    store.update(
                        status="success", row_count=row_count,
                        **_file_counts(normalized_files), files=normalized_files,
                        rows=[{
                            "row_index": int(r.get("row_index") or 0),
                            "description": str(r.get("description") or ""),
                            "amount": str(r.get("amount") or ""),
                            "date": str(r.get("date") or ""),
                        } for r in (result.get("rows") or [])],
                        evidence=evidence, error="",
                    )
                    log(f"{store['store_name']}：已保存收入 Excel 1 份、账单和税票 PDF {row_count * 2} 份")
                except Exception as exc:
                    candidates = exc.partial_files if isinstance(exc, InvoiceExportError) else result.get("files") or []
                    partial_files: list[dict[str, Any]] = []
                    # Retain each independently validated file even if another entry failed validation.
                    for item in candidates:
                        try:
                            normalized = _normalize_store_files(
                                manifest_path.parent, attempt_dir, [item], record["month"]
                            )[0]
                            if not any(f["path"] == normalized["path"] for f in partial_files):
                                partial_files.append(normalized)
                        except Exception:
                            continue
                    store.update(status="failed", **_file_counts(partial_files), files=partial_files, error=str(exc))
                    log(f"{store['store_name']}：失败 — {exc}")

                save()
                if getattr(gateway, "auth_failed", False) or getattr(gateway, "halted", False) is True:
                    reason = getattr(gateway, "halt_reason", "紫鸟会话已停止，请恢复后重试")
                    for pending in record["stores"]:
                        if pending["status"] not in TERMINAL and pending is not store:
                            pending.update(status="failed", error=reason)
                    break

        complete = all(store["status"] in TERMINAL for store in record["stores"])
        record["status"] = "complete" if complete else "incomplete"
        save()
        final_result = public_result(manifest_path, record)
        log(final_result["completion_message"])
        return final_result
