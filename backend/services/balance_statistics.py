"""Account-bound serial collection, retry and export of balance snapshots."""
from __future__ import annotations

import copy
import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from backend.core.ziniao_browser import ZiniaoBrowser, account_profile
from backend.services.balance_workbook import FIELDS, write_balance_workbook
from backend.services.temu_on_sale_export import batch_lock, writable_directory, write_json


SCHEMA = "balance_statistics_v1"
COUNTRIES = {"AUTO": "自动识别", "PH": "菲律宾", "MY": "马来西亚", "TH": "泰国", "ID": "印度尼西亚", "VN": "越南", "SG": "新加坡"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_defaults(platform: str, today: date | None = None) -> dict:
    platform = str(platform).strip().lower()
    if platform not in FIELDS:
        raise ValueError("请选择 TEMU 或 Lazada")
    current = today or date.today()
    month = (current.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    return {"platform": platform, "month": month, "country": "AUTO", "store_names": "",
            "countries": [{"code": code, "name": name} for code, name in COUNTRIES.items()]}


def parse_store_names(value) -> list[str]:
    items = value.splitlines() if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        raise ValueError("请填写完整店铺名称，每行一个")
    result = list(dict.fromkeys(str(item or "").strip() for item in items if str(item or "").strip()))
    if not result or len(result) > 200:
        raise ValueError("单批请选择 1 至 200 个店铺")
    return result


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def match_stores(names, stores: list[dict], platform: str) -> dict:
    """Full-name matching only; ambiguous or wrong-platform matches fail closed."""
    matched, issues, used = [], [], set()
    catalog = []
    ids = set()
    for item in stores:
        identity, name = str(item.get("store_id") or "").strip(), str(item.get("store_name") or "").strip()
        if not identity or not name or identity in ids:
            raise ValueError("紫鸟店铺清单缺少唯一 ID 或完整名称，请刷新后重试")
        ids.add(identity)
        catalog.append({**item, "store_id": identity, "store_name": name})
    for name in parse_store_names(names):
        candidates = [item for item in catalog if item["store_name"] == name]
        if not candidates:
            candidates = [item for item in catalog if _normalized(item["store_name"]) == _normalized(name)]
        if len(candidates) != 1:
            error = "未匹配到完整店铺名称" if not candidates else "存在同名店铺，无法唯一确认店铺 ID"
            issues.append({"name": name, "error": error})
            continue
        store = candidates[0]
        if platform not in str(store.get("platform_name") or "").casefold():
            issues.append({"name": name, "error": f"紫鸟平台信息未确认是 {platform.upper()}"})
        elif store["store_id"] not in used:
            matched.append({**store, "requested_store_name": name})
            used.add(store["store_id"])
    return {"stores": matched, "issues": issues, "can_start": bool(matched) and not issues}


def validate_request(payload: dict, platform: str | None = None, *, default_output_root: str = "", today: date | None = None) -> dict:
    request = dict(payload or {})
    selected = str(platform or request.get("platform") or "").strip().lower()
    defaults = get_defaults(selected, today)
    if platform and request.get("platform") and str(request["platform"]).lower() != selected:
        raise ValueError("请求平台与余额统计模块不一致")
    account_profile(request)
    request["platform"] = selected
    request["country"] = str(request.get("country") or "AUTO").strip().upper()
    if request["country"] not in COUNTRIES:
        raise ValueError("请选择受支持的站点或自动识别")
    request["month"] = str(request.get("month") or defaults["month"]).strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", request["month"]):
        raise ValueError("统计月份格式必须为 YYYY-MM")
    selected_date = date.fromisoformat(request["month"] + "-01")
    if selected_date > (today or date.today()).replace(day=1):
        raise ValueError("统计月份不能晚于当前月份")
    if not request.get("manifest_path"):
        request["store_names"] = parse_store_names(request.get("store_names"))
        request["output_root"] = str(request.get("output_root") or request.get("output_dir") or default_output_root).strip()
        if not request["output_root"]:
            raise ValueError("请选择输出目录")
    return request


def _safe_text(value, request: dict) -> str:
    text = str(value or "")
    for key in ("password", "username", "company"):
        secret = str(request.get(key) or "")
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return text[:4000]


def _empty_result(name: str, platform: str, **extra) -> dict:
    return {"store_id": "", "store_name": name, "requested_store_name": name, "platform": platform,
            "country": "", "currency": "", "status": "pending", "message": "", "attempts": 0,
            "values": {field: None for field in FIELDS[platform]}, "evidence": {}, "notes": [],
            "field_errors": {}, "evidence_exemptions": {}, "source_urls": {}, "captured_at": "", **extra}


def _normalize_collection(raw: dict, platform: str, country: str, evidence_dir: Path, request: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("页面返回的余额采集结果无效")
    result = _empty_result("", platform)
    values, evidence, errors = raw.get("values") or {}, raw.get("evidence") or {}, raw.get("field_errors") or {}
    exemptions = raw.get("evidence_exemptions") or {}
    result["field_errors"] = {str(field): _safe_text(error, request) for field, error in errors.items() if error}
    for field in FIELDS[platform]:
        value = values.get(field)
        exempt = (platform == "lazada" and (
            (field == "processing" and value is None and exemptions.get(field) in {"withdrawal_success", "no_withdrawal"})
            or (field == "balance" and str(value) in {"0", "0.00"} and exemptions.get(field) == "cross_border_unavailable")
        ))
        if exempt:
            result["evidence_exemptions"][field] = exemptions[field]
        if value is None:
            if field != "processing" or not exempt:
                result["field_errors"].setdefault(field, "页面未提供可核实的金额")
        else:
            try:
                amount = Decimal(str(value))
                if isinstance(value, bool) or not amount.is_finite():
                    raise InvalidOperation()
                if field == "processing":
                    amount = -abs(amount)
                result["values"][field] = format(amount, "f")
            except (InvalidOperation, ValueError):
                result["field_errors"][field] = "页面金额不是有效数字"
        if exempt:
            # These documented branches deliberately have no screenshot.
            continue
        image_path = evidence.get(field)
        if image_path:
            candidate = Path(str(image_path)).resolve()
            if candidate.is_relative_to(evidence_dir.resolve()) and candidate.is_file():
                try:
                    from PIL import Image
                    with Image.open(candidate) as image:
                        image.verify()
                    result["evidence"][field] = str(candidate)
                except Exception:
                    result["field_errors"].setdefault(field, "截图文件无效")
            else:
                result["field_errors"].setdefault(field, "截图文件不存在或不属于本次店铺采集")
        elif not exempt:
            result["field_errors"].setdefault(field, "缺少页面截图")
    result["country"] = _safe_text(raw.get("country"), request).upper()
    result["currency"] = _safe_text(raw.get("currency"), request).upper()
    if not result["country"] or (platform == "lazada" and result["country"] not in COUNTRIES.keys() - {"AUTO"}):
        result["field_errors"]["country"] = "无法确认当前站点"
    if country != "AUTO" and result["country"] != country:
        result["field_errors"]["country"] = f"页面站点 {result['country'] or '未知'} 与所选 {country} 不一致"
    if not re.fullmatch(r"[A-Z]{3}", result["currency"]):
        result["field_errors"]["currency"] = "无法确认金额币种"
    result["notes"] = [_safe_text(item, request) for item in raw.get("notes", [])]
    result["captured_at"] = _safe_text(raw.get("captured_at"), request) or _now()
    for field, url in (raw.get("source_urls") or {}).items():
        try:
            parts = urlsplit(str(url))
            if parts.scheme in {"https", "http"} and parts.hostname:
                # Exclude credentials, session tokens and all URL query values.
                result["source_urls"][str(field)] = urlunsplit((parts.scheme, parts.hostname, parts.path, "", ""))
        except ValueError:
            pass
    result["status"] = "success" if not result["field_errors"] else ("partial" if any(value is not None for value in result["values"].values()) else "failed")
    result["message"] = "采集完成" if result["status"] == "success" else "；".join(f"{key}: {value}" for key, value in result["field_errors"].items())
    return {key: result[key] for key in ("country", "currency", "values", "evidence", "evidence_exemptions", "notes", "field_errors", "source_urls", "captured_at", "status", "message")}


def load_manifest(path: str | Path, profile: str, platform: str | None = None) -> dict:
    path = Path(path).expanduser().resolve()
    if path.name != "manifest.json":
        raise ValueError("请选择本批次的 manifest.json")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("无法读取余额统计批次清单") from exc
    if not isinstance(record, dict) or record.get("schema") != SCHEMA or record.get("platform") not in FIELDS:
        raise ValueError("不是有效的余额统计批次")
    if record.get("profile") != profile:
        raise ValueError("所选紫鸟账号与原批次不一致，请选择原账号重试")
    if platform and record["platform"] != platform:
        raise ValueError("批次平台与当前模块不一致")
    stores = record.get("stores")
    if not isinstance(stores, list) or not stores or len(stores) > 200:
        raise ValueError("批次店铺清单无效")
    ids = set()
    for store in stores:
        if not isinstance(store, dict) or not store.get("store_name") or store.get("status") not in {"pending", "running", "success", "partial", "failed"}:
            raise ValueError("批次店铺信息无效")
        identity = store.get("store_id")
        if identity and identity in ids:
            raise ValueError("批次店铺 ID 重复")
        if identity:
            ids.add(identity)
        for evidence in (store.get("evidence") or {}).values():
            if not Path(evidence).resolve().is_relative_to((path.parent / "evidence").resolve()):
                raise ValueError("截图路径超出本批次目录")
    output = record.get("output_file")
    if output and not Path(output).resolve().is_relative_to(path.parent):
        raise ValueError("导出路径超出本批次目录")
    return record


def _public_result(path: Path, record: dict) -> dict:
    stores = copy.deepcopy(record["stores"])
    counts = {status: sum(row["status"] == status for row in stores) for status in ("success", "partial", "failed")}
    complete = counts["success"] == len(stores) and bool(record.get("output_file")) and not record.get("export_error") and not record.get("cleanup_error")
    message = "全部店铺余额统计完成" if complete else f"成功 {counts['success']} 家，部分完成 {counts['partial']} 家，失败 {counts['failed']} 家"
    if record.get("export_error"):
        message += "；" + record["export_error"]
    if record.get("cleanup_error"):
        message += "；" + record["cleanup_error"]
    return {"platform": record["platform"], "month": record.get("month", ""), "run_id": record["run_id"],
            "output_file": record.get("output_file", ""), "output_dir": str(path.parent), "manifest_path": str(path),
            "is_complete": complete, "stores": stores, "summary": {"total": len(stores), **counts},
            "completion_message": message, "can_retry": not complete}


def _collector(platform: str):
    if platform == "temu":
        from backend.services.temu_balance_collector import collect_temu_balance
        return collect_temu_balance
    from backend.services.lazada_balance_collector import collect_lazada_balance
    return collect_lazada_balance


def _invalidate_missing_saved_evidence(row: dict, platform: str) -> None:
    """A successful snapshot is reusable only while its required images survive."""
    from PIL import Image

    errors = {}
    evidence = row.get("evidence") or {}
    exemptions = row.get("evidence_exemptions") or {}
    values = row.get("values") or {}
    for field in FIELDS[platform]:
        exempt = platform == "lazada" and (
            (field == "processing" and values.get(field) is None and exemptions.get(field) in {"withdrawal_success", "no_withdrawal"})
            or (field == "balance" and str(values.get(field)) in {"0", "0.00"} and exemptions.get(field) == "cross_border_unavailable")
        )
        image_path = evidence.get(field)
        if not image_path and exempt:
            continue
        try:
            if not image_path or not Path(image_path).is_file():
                raise FileNotFoundError()
            with Image.open(image_path) as image:
                image.verify()
        except Exception:
            errors[field] = "原截图丢失或损坏，需要重新采集"
            # Failed recollection must still export the surviving evidence.
            evidence.pop(field, None)
    if errors:
        row["evidence"] = evidence
        row["field_errors"] = {**(row.get("field_errors") or {}), **errors}
        row["status"] = "partial" if any(value is not None for value in values.values()) else "failed"
        row["message"] = "；".join(f"{field}: {error}" for field, error in errors.items())


def _run(request: dict, path: Path, record: dict, progress, browser_factory, collectors) -> dict:
    platform = record["platform"]
    browser = None
    log = lambda message: progress(_safe_text(message, request)) if progress else None

    def save():
        record["updated_at"] = _now()
        write_json(path, record)

    with batch_lock(path):
        # Load again after the lock so a waiting retry cannot use stale state.
        record = load_manifest(path, account_profile(request), platform)
        record.update(status="running", export_error="", cleanup_error="")
        for row in record["stores"]:
            if row["status"] == "success":
                _invalidate_missing_saved_evidence(row, platform)
            if row["status"] in {"running", "pending"}:
                row.update(status="failed", message="上次采集未完成，等待重试")
        save()
        try:
            pending = [row for row in record["stores"] if row["status"] != "success"]
            if pending:
                browser = (browser_factory or ZiniaoBrowser)(request)
                if getattr(browser, "profile", account_profile(request)) != account_profile(request):
                    raise ValueError("紫鸟会话账号与请求账号不一致")
                browser.ready()
                catalog = browser.list_stores()
                collect = (collectors or {}).get(platform) or _collector(platform)
                halted = ""
                used_ids = {row["store_id"] for row in record["stores"] if row["status"] == "success"}
                for index, row in enumerate(record["stores"], 1):
                    if row["status"] == "success":
                        continue
                    if halted:
                        row.update(status="failed", message=halted)
                        save()
                        continue
                    matched = match_stores([row["requested_store_name"]], catalog, platform)
                    if not matched["can_start"]:
                        row.update(status="failed", message=matched["issues"][0]["error"])
                        save()
                        continue
                    store = matched["stores"][0]
                    if store["store_id"] in used_ids:
                        row.update(status="failed", message="多个输入名称指向同一店铺 ID，请去掉重复店铺")
                        save()
                        continue
                    if row["store_id"] and row["store_id"] != store["store_id"]:
                        row.update(status="failed", message="店铺 ID 与原批次不一致，已停止该店重试")
                        save()
                        continue
                    row.update(store_id=store["store_id"], store_name=store["store_name"], status="running", attempts=row.get("attempts", 0) + 1, message="正在采集")
                    used_ids.add(store["store_id"])
                    save()
                    log(f"[{index}/{len(record['stores'])}] {row['store_name']}：开始采集")
                    directory = path.parent / "evidence" / f"store_{index}_{uuid4().hex[:8]}"
                    directory.mkdir(parents=True, exist_ok=False)
                    try:
                        browser.open_store(store)
                        driver = browser._driver(store["store_id"])
                        collector_store = {**store, "country": record["country"]}
                        if platform == "temu":
                            raw = collect(driver, collector_store, record["month"], directory, progress=log)
                        else:
                            raw = collect(driver, collector_store, directory, progress=log)
                        row.update(_normalize_collection(raw, platform, record["country"], directory, request))
                    except Exception as exc:
                        row.update(status="failed", message=_safe_text(exc, request), captured_at=_now(),
                                   values={field: None for field in FIELDS[platform]}, evidence={}, field_errors={"collection": _safe_text(exc, request)})
                        if getattr(exc, "auth", False) or getattr(browser, "auth_failed", False):
                            halted = "紫鸟登录已失效，后续店铺未采集"
                    finally:
                        try:
                            browser.close_store(store["store_id"])
                            if getattr(browser, "cleanup_failed", False):
                                raise ValueError("店铺未确认关闭")
                        except Exception as exc:
                            halted = "上一店铺未确认关闭，后续店铺未采集"
                            record["cleanup_error"] = _safe_text(exc, request)
                            row["field_errors"]["cleanup"] = record["cleanup_error"]
                            row["status"] = "partial" if any(value is not None for value in row["values"].values()) else "failed"
                            row["message"] += "；" + halted
                    save()
                    log(f"[{index}/{len(record['stores'])}] {row['store_name']}：{row['message']}")
        except Exception as exc:
            for row in record["stores"]:
                if row["status"] != "success":
                    row.update(status="failed", message=_safe_text(exc, request))
            save()
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception as exc:
                    record["cleanup_error"] = _safe_text(exc, request)
            try:
                output = path.parent / ("TEMU余额统计.xlsx" if platform == "temu" else "Lazada余额统计.xlsx")
                record["output_file"] = write_balance_workbook(platform, record["stores"], output, month=record["month"])
            except Exception as exc:
                record["export_error"] = "Excel 导出失败：" + _safe_text(exc, request)
                record["output_file"] = ""
            result = _public_result(path, record)
            record["status"] = "complete" if result["is_complete"] else "partial"
            save()
    return _public_result(path, record)


def run_balance_statistics(request: dict, progress: Callable[[str], None] | None = None, *, browser_factory=None, collectors: dict | None = None) -> dict:
    request = validate_request(request)
    if request.get("manifest_path"):
        return retry_balance_statistics(request, progress, browser_factory=browser_factory, collectors=collectors)
    output = writable_directory(request["output_root"])
    run_id = uuid4().hex[:12]
    path = output / f"{request['platform'].upper()}余额统计_{datetime.now():%Y%m%d_%H%M%S}_{run_id}" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=False)
    record = {"schema": SCHEMA, "run_id": run_id, "profile": account_profile(request), "platform": request["platform"],
              "month": request["month"], "country": request["country"], "created_at": _now(), "captured_at": _now(),
              "balance_basis": "current_page", "pending_basis": "order_created_month" if request["platform"] == "temu" else "current_page",
              "status": "pending", "output_file": "", "stores": [_empty_result(name, request["platform"]) for name in request["store_names"]]}
    write_json(path, record)
    return _run(request, path, record, progress, browser_factory, collectors)


def retry_balance_statistics(request: dict, progress: Callable[[str], None] | None = None, *, browser_factory=None, collectors: dict | None = None) -> dict:
    if not request.get("manifest_path"):
        raise ValueError("请选择需要重试的余额统计批次")
    path = Path(request["manifest_path"]).expanduser().resolve()
    record = load_manifest(path, account_profile(request), request.get("platform"))
    request = validate_request({**request, "platform": record["platform"], "month": record["month"], "country": record["country"]})
    return _run(request, path, record, progress, browser_factory, collectors)
