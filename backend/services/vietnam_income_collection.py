from __future__ import annotations

import json
import os
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha1
from pathlib import Path
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = 1
REPORT_TYPES: tuple[str, ...] = ("income", "ads", "affiliate", "ads_credit")
REPORT_LABELS: dict[str, str] = {
    "income": "收入拨款",
    "ads": "广告消费",
    "affiliate": "联盟消费",
    "ads_credit": "广告补贴",
}
STORE_TYPE_LABELS: dict[str, str] = {
    "normal": "普通店",
    "warehouse": "仓发店",
}

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True)
class CollectionStore:
    name: str
    store_type: str


@dataclass(frozen=True)
class CollectionRunRequest:
    period: str
    start_date: str
    end_date: str
    output_dir: Path
    stores: tuple[CollectionStore, ...]


def default_collection_period(reference: date | None = None) -> str:
    current = reference or date.today()
    if current.month == 1:
        year, month = current.year - 1, 12
    else:
        year, month = current.year, current.month - 1
    return f"{year:04d}-{month:02d}"


def period_date_range(period: str) -> tuple[str, str]:
    match = re.fullmatch(r"(\d{4})-(\d{2})", str(period or "").strip())
    if not match:
        raise ValueError("核对月份格式不正确，请使用 YYYY-MM")
    year, month = int(match.group(1)), int(match.group(2))
    if month < 1 or month > 12:
        raise ValueError("核对月份格式不正确，请使用 YYYY-MM")
    end_day = monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{end_day:02d}"


def report_options() -> list[dict[str, str]]:
    return [{"key": key, "label": REPORT_LABELS[key]} for key in REPORT_TYPES]


def validate_collection_payload(
    payload: dict[str, Any] | None,
    *,
    default_output_dir: str | Path = "",
) -> CollectionRunRequest:
    data = dict(payload or {})
    period = str(data.get("period") or default_collection_period()).strip()
    start_date, end_date = period_date_range(period)

    output_text = str(data.get("output_dir") or default_output_dir or "").strip()
    if not output_text:
        raise ValueError("请选择输出目录")
    output_dir = Path(output_text).expanduser()

    raw_stores = data.get("stores")
    if not isinstance(raw_stores, (list, tuple)):
        raise ValueError("请至少添加一个越南 Shopee 店铺")

    stores: list[CollectionStore] = []
    seen: set[str] = set()
    for raw in raw_stores:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("store_name") or "").strip()
        if not name:
            continue
        store_type = infer_store_type(name)
        identity = name.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        stores.append(CollectionStore(name=name, store_type=store_type))

    if not stores:
        raise ValueError("请至少添加一个越南 Shopee 店铺")
    if len(stores) > 200:
        raise ValueError("单个批次最多支持 200 个店铺")

    return CollectionRunRequest(
        period=period,
        start_date=start_date,
        end_date=end_date,
        output_dir=output_dir,
        stores=tuple(stores),
    )


def create_collection_run(
    request: CollectionRunRequest,
    *,
    account_name: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    created_at = now or datetime.now()
    run_id = (
        f"vn_{request.period.replace('-', '')}_{created_at.strftime('%Y%m%d_%H%M%S')}_"
        f"{uuid4().hex[:6]}"
    )
    output_root = request.output_dir.resolve()
    run_dir = output_root / "越南收支核对" / request.period / run_id
    manifest_path = run_dir / "manifest.json"

    (run_dir / "logs").mkdir(parents=True, exist_ok=False)
    (run_dir / "screenshots").mkdir()
    raw_root = run_dir / "raw" / "ziniao"
    raw_root.mkdir(parents=True)

    stores_payload: list[dict[str, Any]] = []
    used_slugs: set[str] = set()
    for store in request.stores:
        safe_name = safe_store_name(store.name, used_slugs)
        report_payload: dict[str, dict[str, Any]] = {}
        for report_type in REPORT_TYPES:
            (raw_root / safe_name / report_type).mkdir(parents=True, exist_ok=True)
            report_payload[report_type] = {
                "status": "pending",
                "file": "",
                "row_count": None,
                "retry_count": 0,
                "started_at": "",
                "finished_at": "",
                "error_code": "",
                "error_message": "",
            }
        stores_payload.append(
            {
                "store_name": store.name,
                "safe_name": safe_name,
                "store_type": store.store_type,
                "store_type_label": STORE_TYPE_LABELS[store.store_type],
                "reports": report_payload,
            }
        )

    timestamp = created_at.strftime("%Y-%m-%d %H:%M:%S")
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "source": "ziniao",
        "period": request.period,
        "start_date": request.start_date,
        "end_date": request.end_date,
        "status": "created",
        "account_name": str(account_name or "").strip(),
        "report_types": report_options(),
        "created_at": timestamp,
        "updated_at": timestamp,
        "summary": {
            "store_count": len(stores_payload),
            "report_count": len(stores_payload) * len(REPORT_TYPES),
            "pending_count": len(stores_payload) * len(REPORT_TYPES),
            "success_count": 0,
            "no_data_count": 0,
            "failed_count": 0,
        },
        "stores": stores_payload,
    }
    write_manifest(manifest_path, manifest)
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
    }


def write_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = manifest_path.with_name(f".{manifest_path.name}.{uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, manifest_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def load_manifest(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path)
    if path.name != "manifest.json" or not path.is_file():
        raise ValueError("批次清单不存在")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("批次清单格式不受支持")
    return payload


def report_is_completed(manifest_path: str | Path, report: dict[str, Any] | None) -> bool:
    data = report if isinstance(report, dict) else {}
    status = str(data.get("status") or "")
    if status == "no_data":
        return True
    if status != "success":
        return False
    file_text = str(data.get("file") or "").strip()
    if not file_text:
        return False
    file_path = Path(file_text)
    if not file_path.is_absolute():
        file_path = Path(manifest_path).parent / file_path
    return file_path.is_file()


def safe_store_name(store_name: str, used: set[str] | None = None) -> str:
    original = str(store_name or "").strip()
    cleaned = _INVALID_FILENAME_CHARS.sub("_", original)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned.upper() in _WINDOWS_RESERVED_NAMES:
        cleaned = "store"
    cleaned = cleaned[:72].rstrip(" .") or "store"
    digest = sha1(original.encode("utf-8")).hexdigest()[:8]
    candidate = f"{cleaned}__{digest}"
    if used is not None:
        counter = 2
        unique = candidate
        while unique.casefold() in used:
            unique = f"{candidate}_{counter}"
            counter += 1
        candidate = unique
        used.add(candidate.casefold())
    return candidate


def infer_store_type(store_name: str) -> str:
    return "warehouse" if "仓发" in str(store_name or "") else "normal"
