from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any
from decimal import Decimal
from datetime import datetime

from backend.services.vietnam_income_collection import load_manifest, write_manifest, report_is_completed
from backend.services.vietnam_final_reconciliation import _clean_store_name, _decimal, _decimal_text, parse_profit_rate, parse_profit_rates

REQUIRED_MABANG_REPORTS = (
    "income_detail",
    "income_summary",
    "refund_part_01_10",
    "refund_part_11_20",
    "refund_part_21_end",
    "refunds_merged",
)
ZINIAO_REPORT_TYPES = ("income", "ads", "affiliate", "ads_credit")


def derive_vietnam_manifest_status(manifest_path: Path, manifest: dict[str, Any]) -> str:
    final = manifest.get("final") or {}
    run_dir = manifest_path.parent

    # 1. Check final status
    final_status = final.get("status")
    if final_status == "ready":
        output_rel = final.get("output_file")
        if output_rel:
            output_abs = run_dir / output_rel
            if output_abs.is_file():
                return "final_ready"
            return "final_file_missing"
        return "final_file_missing"
    elif final_status == "failed":
        return "failed"
    elif final_status == "running":
        return "generating"

    # 2. Check running status in reports
    mabang = manifest.get("mabang") or {}
    mabang_reports = mabang.get("reports") or {}
    stores = manifest.get("stores") or []

    any_running = False
    any_failed = False
    all_pending = True
    any_completed = False

    # Check mabang reports
    for key in REQUIRED_MABANG_REPORTS:
        report = mabang_reports.get(key) or {}
        st = report.get("status")
        if st in {"success", "no_data"}:
            any_completed = True
            all_pending = False
        elif st == "running":
            any_running = True
            all_pending = False
        elif st == "failed":
            any_failed = True
            all_pending = False
        elif st == "pending":
            pass

    # Check ziniao reports
    for store in stores:
        reports = store.get("reports") or {}
        for rtype in ZINIAO_REPORT_TYPES:
            report = reports.get(rtype) or {}
            st = report.get("status")
            if st in {"success", "no_data"}:
                any_completed = True
                all_pending = False
            elif st == "running":
                any_running = True
                all_pending = False
            elif st == "failed":
                any_failed = True
                all_pending = False
            elif st == "pending":
                pass

    if any_running:
        return "running"

    # 3. Check Mabang validation status
    validation_status = mabang.get("validation", {}).get("status")
    if validation_status == "failed":
        return "blocked"

    # 4. Check if all reports completed
    all_completed = True
    for key in REQUIRED_MABANG_REPORTS:
        if not report_is_completed(manifest_path, mabang_reports.get(key)):
            all_completed = False
            break
    if all_completed:
        for store in stores:
            reports = store.get("reports") or {}
            for rtype in ZINIAO_REPORT_TYPES:
                if not report_is_completed(manifest_path, reports.get(rtype)):
                    all_completed = False
                    break

    if all_completed:
        if validation_status != "passed":
            return "blocked"

        # Check missing profit rates if refunds merged exists
        refund_stores = extract_refund_stores(manifest_path, manifest)
        config_rates = manifest.get("config", {}).get("profit_rates") or {}
        missing_rates = [s for s in refund_stores if s not in config_rates]
        if missing_rates:
            return "needs_attention"

        return "ready_to_generate"

    if any_failed:
        return "blocked"

    if all_pending:
        return "draft"

    return "running"


def extract_refund_stores(manifest_path: Path, manifest: dict[str, Any]) -> list[str]:
    mabang = manifest.get("mabang") or {}
    reports = mabang.get("reports") or {}
    refunds_merged = reports.get("refunds_merged") or {}
    file_rel = refunds_merged.get("file")
    if not file_rel:
        return []
    file_abs = manifest_path.parent / file_rel
    if not file_abs.is_file():
        return []

    stores = set()
    try:
        with file_abs.open("r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                store = _clean_store_name(row.get("店铺"))
                if store:
                    stores.add(store)
    except Exception:
        pass
    return sorted(list(stores))


def get_vietnam_report_dashboard(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path)
    manifest = load_manifest(path)
    run_dir = path.parent
    run_id = manifest.get("run_id") or run_dir.name
    period = manifest.get("period") or ""

    manifest_status = derive_vietnam_manifest_status(path, manifest)
    state = manifest_status
    if manifest_status == "final_ready":
        final_info = manifest.get("final") or {}
        unmatched_count = final_info.get("unmatched_count") or 0
        if unmatched_count > 0:
            state = "ready_with_issues"
        else:
            state = "ready"

    # Calculate progress
    stores_list = manifest.get("stores") or []
    mabang = manifest.get("mabang") or {}
    mabang_reports = mabang.get("reports") or {}

    total = len(stores_list) * len(ZINIAO_REPORT_TYPES) + len(REQUIRED_MABANG_REPORTS)
    completed = 0
    failed = 0

    for key in REQUIRED_MABANG_REPORTS:
        report = mabang_reports.get(key) or {}
        if report_is_completed(path, report):
            completed += 1
        elif report.get("status") == "failed":
            failed += 1

    for store in stores_list:
        reports = store.get("reports") or {}
        for rtype in ZINIAO_REPORT_TYPES:
            report = reports.get(rtype) or {}
            if report_is_completed(path, report):
                completed += 1
            elif report.get("status") == "failed":
                failed += 1

    data_progress = {
        "completed": completed,
        "total": total,
        "failed": failed
    }

    # Load final summary if ready
    totals = {}
    stores = []
    exception_summary = {}
    files = {
        "output_file": "",
        "output_exists": False,
        "summary_file": "",
        "unmatched_file": "",
        "unmatched_exists": False,
        "processing_log": "",
        "run_dir": str(run_dir.resolve())
    }

    final = manifest.get("final") or {}
    output_rel = final.get("output_file")
    if output_rel:
        output_abs = run_dir / output_rel
        if output_abs.is_file():
            files["output_file"] = str(output_abs.resolve())
            files["output_exists"] = True

    summary_rel = final.get("summary_file")
    summary_data = None
    if summary_rel:
        summary_abs = run_dir / summary_rel
        if summary_abs.is_file():
            files["summary_file"] = str(summary_abs.resolve())
            try:
                summary_data = json.loads(summary_abs.read_text(encoding="utf-8"))
                totals = summary_data.get("totals") or {}
                stores = summary_data.get("stores") or []
                exception_summary = summary_data.get("exception_summary") or {}
            except Exception:
                pass

    unmatched_rel = final.get("unmatched_file")
    if unmatched_rel:
        unmatched_abs = run_dir / unmatched_rel
        if unmatched_abs.is_file():
            files["unmatched_file"] = str(unmatched_abs.resolve())
            files["unmatched_exists"] = True

    log_rel = final.get("processing_log")
    if log_rel:
        log_abs = run_dir / log_rel
        if log_abs.is_file():
            files["processing_log"] = str(log_abs.resolve())

    # Build anomalies / exceptions
    exceptions = []

    # 1. Mabang validation exception
    validation = mabang.get("validation") or {}
    if validation.get("status") == "failed":
        diff_order = validation.get("order_count_diff") or 0
        diff_money = validation.get("receivable_diff") or "0"
        exceptions.append({
            "id": "mabang-val-failed",
            "severity": "blocking",
            "category": "mabang_validation",
            "source": "mabang",
            "store_name": "",
            "order_number": "",
            "amount": str(diff_money),
            "title": "马帮收支明细与汇总不一致",
            "message": f"订单数差异 {diff_order}，应收货款差异 {diff_money} RMB。当前不能生成报表。",
            "suggestion": "请重新导出收支并校验；若仍有差异，请查看异常店铺。",
            "action": "retry_mabang_income"
        })
        for index, mismatch in enumerate(validation.get("store_mismatches") or []):
            store_name = _clean_store_name(mismatch.get("store_name") or mismatch.get("店铺") or "")
            store_order_diff = mismatch.get("order_count_diff") or 0
            store_receivable_diff = mismatch.get("receivable_diff") or "0"
            exceptions.append({
                "id": f"mabang-val-store-{index}",
                "severity": "blocking",
                "category": "mabang_validation",
                "source": "mabang",
                "store_name": store_name,
                "order_number": "",
                "amount": str(store_receivable_diff),
                "title": "店铺级收支差异",
                "message": f"订单数差异 {store_order_diff}，应收货款差异 {store_receivable_diff} RMB。",
                "suggestion": "请核对该店铺的明细与汇总导出范围。",
                "action": "retry_mabang_income",
            })

    # 2. Source reports failed exceptions
    for store in stores_list:
        reports = store.get("reports") or {}
        for rtype in ZINIAO_REPORT_TYPES:
            report = reports.get(rtype) or {}
            if report.get("status") == "failed":
                exceptions.append({
                    "id": f"ziniao-{store['store_name']}-{rtype}-failed",
                    "severity": "blocking",
                    "category": "ziniao_collection",
                    "source": "ziniao",
                    "store_name": store["store_name"],
                    "order_number": "",
                    "amount": "",
                    "title": f"紫鸟数据采集失败: {rtype}",
                    "message": report.get("error_message") or "未知采集错误",
                    "suggestion": "请点击重试以重新采集该店铺的数据。",
                    "action": "retry_ziniao_subsidy" if rtype == "income" else "retry_ziniao_sources"
                })

    for key in REQUIRED_MABANG_REPORTS:
        report = mabang_reports.get(key) or {}
        if report.get("status") == "failed":
            exceptions.append({
                "id": f"mabang-{key}-failed",
                "severity": "blocking",
                "category": "mabang_collection",
                "source": "mabang",
                "store_name": "",
                "order_number": "",
                "amount": "",
                "title": f"马帮数据采集失败: {key}",
                "message": report.get("error_message") or "未知导出错误",
                "suggestion": "请检查马帮登录状态，点击重试进行补采。",
                "action": "retry_mabang_income" if "income" in key else "retry_mabang_refunds"
            })

    # 3. Missing profit rates
    refund_stores = extract_refund_stores(path, manifest)
    config_rates = manifest.get("config", {}).get("profit_rates") or {}
    missing_rates = [s for s in refund_stores if s not in config_rates]
    for s in missing_rates:
        exceptions.append({
            "id": f"profit-rate-missing-{s}",
            "severity": "blocking",
            "category": "missing_profit_rate",
            "source": "config",
            "store_name": s,
            "order_number": "",
            "amount": "",
            "title": "缺少上月利润率",
            "message": f"店铺「{s}」有退款订单，但尚未配置上月利润率。",
            "suggestion": "请在生成前配置该店铺的利润率。",
            "action": "focus_profit_rate"
        })

    # 4. Final generation failure
    if final.get("status") == "failed":
        exceptions.append({
            "id": "final-generation-failed",
            "severity": "blocking",
            "category": "generation_failed",
            "source": "final",
            "store_name": "",
            "order_number": "",
            "amount": "",
            "title": "报表生成失败",
            "message": final.get("error_message") or "最终合并错误",
            "suggestion": "请检查错误日志并点击重新生成报表。",
            "action": "retry_final"
        })

    # 5. Load unmatched preview anomalies (up to 20 preview)
    unmatched_file = files["unmatched_file"]
    if unmatched_file and Path(unmatched_file).is_file():
        try:
            with open(unmatched_file, "r", encoding="utf-8-sig", errors="replace") as f:
                reader = csv.DictReader(f)
                count = 0
                for idx, row in enumerate(reader):
                    if count >= 20:
                        break
                    exceptions.append({
                        "id": f"unmatched-{idx}",
                        "severity": "attention",
                        "category": "unmatched_record",
                        "source": "unmatched",
                        "store_name": row.get("店铺名称") or "",
                        "order_number": row.get("订单编号") or "",
                        "amount": row.get("金额") or "",
                        "title": row.get("类型") or "未匹配数据",
                        "message": row.get("说明") or "未能成功匹配",
                        "suggestion": "请在生成的 Excel 报表「未匹配清单」页中复核明细。",
                        "action": "open_unmatched_file"
                    })
                    count += 1
        except Exception:
            pass

    return {
        "ok": True,
        "manifest_path": str(path.resolve()),
        "run_dir": str(run_dir.resolve()),
        "run_id": run_id,
        "period": period,
        "state": state,
        "created_at": manifest.get("created_at") or "",
        "updated_at": manifest.get("updated_at") or "",
        "generated_at": final.get("finished_at") or "",
        "data_progress": data_progress,
        "totals": totals,
        "stores": stores,
        "exceptions": exceptions,
        "exception_summary": exception_summary or {
            "total_count": len(exceptions),
            "blocking_count": sum(1 for e in exceptions if e["severity"] == "blocking"),
            "affected_store_count": len(set(e["store_name"] for e in exceptions if e["store_name"])),
            "amount": "0"
        },
        "summary": {
            "average_exchange_rate": (summary_data or {}).get("average_exchange_rate"),
            "mabang_detail_row_count": (summary_data or {}).get("mabang_detail_row_count"),
            "refund_row_count": (summary_data or {}).get("refund_row_count"),
        },
        "required_profit_rate_stores": refund_stores,
        "config": manifest.get("config") or {"profit_rates": {}},
        "files": files
    }


def list_vietnam_report_runs(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(payload or {})
    output_dir_text = str(data.get("output_dir") or "").strip()
    period = str(data.get("period") or "").strip()
    limit = min(max(int(data.get("limit") or 50), 1), 200)

    if not output_dir_text:
        return {"ok": False, "error": "必须提供输出目录以检索批次"}

    output_dir = Path(output_dir_text)
    runs_root = output_dir / "越南收支核对"
    if not runs_root.is_dir():
        return {"ok": True, "runs": []}

    runs = []
    search_dirs = [runs_root / period] if period else list(runs_root.iterdir())

    for month_dir in search_dirs:
        if not month_dir.is_dir():
            continue
        m_match = re.fullmatch(r"\d{4}-\d{2}", month_dir.name)
        if not m_match:
            continue

        for run_dir in month_dir.iterdir():
            if not run_dir.is_dir():
                continue
            manifest_file = run_dir / "manifest.json"
            if not manifest_file.is_file():
                continue

            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
                schema = manifest.get("schema_version")
                if schema != 1:
                    continue

                status = manifest.get("status") or "created"
                final = manifest.get("final") or {}
                unmatched_count = final.get("unmatched_count") or 0

                stores_list = manifest.get("stores") or []
                store_count = len(stores_list)

                runs.append({
                    "run_id": manifest.get("run_id") or run_dir.name,
                    "period": manifest.get("period") or month_dir.name,
                    "manifest_path": str(manifest_file.resolve()),
                    "status": status,
                    "store_count": store_count,
                    "created_at": manifest.get("created_at") or "",
                    "updated_at": manifest.get("updated_at") or "",
                    "generated_at": final.get("finished_at") or "",
                    "unmatched_count": unmatched_count
                })
            except Exception:
                pass

    runs.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"ok": True, "runs": runs[:limit]}


def save_vietnam_report_config(payload: dict[str, Any]) -> dict[str, Any]:
    manifest_path_text = str(payload.get("manifest_path") or "").strip()
    if not manifest_path_text:
        return {"ok": False, "error": "缺少 manifest_path"}

    manifest_path = Path(manifest_path_text)
    manifest = load_manifest(manifest_path)

    profit_rates_payload = payload.get("profit_rates") or {}

    normalized = {}
    for store, value in profit_rates_payload.items():
        store_cleaned = _clean_store_name(store)
        if not store_cleaned:
            continue
        try:
            rate_dec = parse_profit_rate(value)
            normalized[store_cleaned] = _decimal_text(rate_dec)
        except Exception as e:
            return {"ok": False, "error": f"店铺「{store}」的利润率「{value}」格式不正确: {str(e)}"}

    config = manifest.setdefault("config", {})
    config["profit_rates"] = normalized
    config["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    manifest["updated_at"] = config["updated_at"]

    write_manifest(manifest_path, manifest)
    return {"ok": True, "config": config}


def get_vietnam_report_anomalies(payload: dict[str, Any]) -> dict[str, Any]:
    manifest_path_text = str(payload.get("manifest_path") or "").strip()
    if not manifest_path_text:
        return {"ok": False, "error": "缺少 manifest_path"}

    manifest_path = Path(manifest_path_text)
    manifest = load_manifest(manifest_path)

    category = str(payload.get("category") or "all").strip().lower()
    severity = str(payload.get("severity") or "all").strip().lower()
    store = _clean_store_name(payload.get("store") or "").lower()
    query = str(payload.get("query") or "").strip().lower()

    page = max(int(payload.get("page") or 1), 1)
    page_size = min(max(int(payload.get("page_size") or 50), 1), 200)

    dashboard = get_vietnam_report_dashboard(manifest_path)
    all_items = dashboard.get("exceptions") or []

    all_items = [item for item in all_items if item["source"] != "unmatched"]

    unmatched_file = dashboard["files"]["unmatched_file"]
    if unmatched_file and Path(unmatched_file).is_file():
        try:
            with open(unmatched_file, "r", encoding="utf-8-sig", errors="replace") as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader):
                    all_items.append({
                        "id": f"unmatched-{idx}",
                        "severity": "attention",
                        "category": "unmatched_record",
                        "source": "unmatched",
                        "store_name": row.get("店铺名称") or "",
                        "order_number": row.get("订单编号") or "",
                        "amount": row.get("金额") or "",
                        "title": row.get("类型") or "未匹配数据",
                        "message": row.get("说明") or "未能成功匹配",
                        "suggestion": "请在生成的 Excel 报表「未匹配清单」页中复核明细。",
                        "action": "open_unmatched_file"
                    })
        except Exception:
            pass

    filtered = []
    for item in all_items:
        if category != "all" and item["category"].lower() != category:
            continue
        if severity != "all" and item["severity"].lower() != severity:
            continue
        if store and store not in item["store_name"].lower():
            continue
        if query:
            match = (
                query in item["title"].lower() or
                query in item["message"].lower() or
                query in item["order_number"].lower() or
                query in item["store_name"].lower()
            )
            if not match:
                continue
        filtered.append(item)

    total = len(filtered)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    page_items = filtered[start_idx:end_idx]

    return {
        "ok": True,
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": page_items
    }
