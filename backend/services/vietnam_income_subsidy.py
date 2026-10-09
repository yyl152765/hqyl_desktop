from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from playwright.sync_api import Page, sync_playwright

from backend.core.ziniao_client import ZiniaoClient, ZiniaoCredentials
from backend.services.vietnam_income_collection import (
    infer_store_type,
    load_manifest,
    report_is_completed,
    write_manifest,
)
from backend.services.shopee_verification import (
    ShopeePasswordVerificationRequired,
    complete_shopee_password_verification,
)


ProgressCallback = Callable[[str], None]
INCOME_DETAIL_API = "/api/v4/accounting/pc/seller_income/income_overview/get_income_detail"
WAREHOUSE_INCOME_DETAIL_API = "/api/v4/accounting/cbpc/seller_income/income_overview/get_income_detail"
WAREHOUSE_PAYOUT_API = "/api/v4/accounting/cbpc/seller_income/income_overview/get_available_payout_detail_list"
SHOP_INFO_API = "/api/selleraccount/shop_info/"
SUBSIDY_COLUMNS = (
    "店铺名称",
    "订单编号",
    "收入金额",
    "补贴金额",
    "净收入金额",
    "预计拨款时间",
    "调整拨款时间",
    "实际拨款时间",
    "付款方式",
)


class IncomeVerificationRequired(ShopeePasswordVerificationRequired):
    pass


@dataclass(frozen=True)
class SubsidyCollectionJob:
    manifest_path: Path
    company: str
    username: str
    password: str
    client_path: Path
    socket_port: int = 16851


def validate_subsidy_collection_payload(
    payload: dict[str, Any] | None,
    *,
    client_path: str | Path,
    socket_port: int = 16851,
) -> SubsidyCollectionJob:
    data = dict(payload or {})
    manifest_path = Path(str(data.get("manifest_path") or "").strip())
    manifest = load_manifest(manifest_path)
    if manifest.get("source") != "ziniao":
        raise ValueError("批次清单不是紫鸟数据源")
    company = str(data.get("company") or "").strip()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not company or not username or not password:
        raise ValueError("紫鸟账号信息不完整")
    resolved_client_path = Path(client_path)
    if not resolved_client_path.is_file():
        raise ValueError(f"紫鸟客户端不存在：{resolved_client_path}")
    return SubsidyCollectionJob(
        manifest_path=manifest_path,
        company=company,
        username=username,
        password=password,
        client_path=resolved_client_path,
        socket_port=int(socket_port),
    )


def normal_income_url(launcher_page: str, start_date: str, end_date: str) -> str:
    start_text = _compact_date(start_date)
    end_text = _compact_date(end_date)
    parts = urlsplit(str(launcher_page or ""))
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("紫鸟未返回有效的 Shopee 店铺入口")
    return f"{parts.scheme}://{parts.netloc}/portal/finance/income?type=2&dateRange={start_text}{end_text}"


def build_income_request(start_date: str, end_date: str) -> dict[str, Any]:
    return {
        "source_type": 0,
        "income_category": 2,
        "pagination_info": {"direction": 0, "limit": 10},
        "local_query_condition": {
            "start_date": _iso_date(start_date),
            "end_date": _iso_date(end_date),
        },
    }


def flatten_income_item(item: dict[str, Any], store_name: str) -> dict[str, Any]:
    detail = item.get("local_income_detail") if isinstance(item, dict) else {}
    detail = detail if isinstance(detail, dict) else {}
    order_info = detail.get("order_income_info")
    order_info = order_info if isinstance(order_info, dict) else {}
    return {
        "店铺名称": store_name,
        "订单编号": str(order_info.get("order_sn") or ""),
        "收入金额": _number(detail.get("income_amount")),
        "补贴金额": _number(detail.get("adjustment_income_amount")),
        "净收入金额": _number(detail.get("net_income_amount")),
        "预计拨款时间": _timestamp(detail.get("income_estimated_escrow_time")),
        "调整拨款时间": _timestamp(detail.get("income_adjustment_released_time")),
        "实际拨款时间": _timestamp(detail.get("income_released_time")),
        "付款方式": str(order_info.get("payment_method_name") or ""),
    }


def collect_income_pages(
    page: Page,
    start_date: str,
    end_date: str,
    *,
    progress: ProgressCallback | None = None,
    max_pages: int = 10000,
) -> list[dict[str, Any]]:
    payload = build_income_request(start_date, end_date)
    rows: list[dict[str, Any]] = []
    seen_cursors: set[str] = set()
    for page_number in range(1, max_pages + 1):
        result = _browser_fetch(page, INCOME_DETAIL_API, payload, progress=progress)
        if int(result.get("code") or 0) != 0:
            raise RuntimeError(str(result.get("user_message") or result.get("message") or "Shopee 收入接口失败"))
        data = result.get("data")
        data = data if isinstance(data, dict) else {}
        page_rows = data.get("list")
        page_rows = [item for item in page_rows if isinstance(item, dict)] if isinstance(page_rows, list) else []
        rows.extend(page_rows)
        if progress is not None:
            progress(f"收入数据第 {page_number} 页完成，本页 {len(page_rows)} 条，累计 {len(rows)} 条")
        next_page = data.get("next_page")
        next_page = next_page if isinstance(next_page, dict) else {}
        cursor = str(next_page.get("cursor") or "")
        if not cursor or cursor in seen_cursors or not page_rows:
            break
        seen_cursors.add(cursor)
        payload = build_income_request(start_date, end_date)
        payload["pagination_info"] = next_page
    else:
        raise RuntimeError(f"收入数据超过最大分页数 {max_pages}")
    return rows


def run_subsidy_collection(
    job: SubsidyCollectionJob,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(job.manifest_path)
    stores = [
        store
        for store in manifest.get("stores") or []
        if str(store.get("store_name") or "").strip()
    ]
    completed_count = sum(
        report_is_completed(job.manifest_path, (store.get("reports") or {}).get("income"))
        for store in stores
    )
    if stores and completed_count == len(stores):
        _emit(progress, "补贴订单已全部采集过，本次跳过")
        return {
            "manifest_path": str(job.manifest_path),
            "run_dir": str(job.manifest_path.parent),
            "success_count": completed_count,
            "failed_count": 0,
            "order_count": 0,
            "subsidy_count": 0,
            "skipped_count": completed_count,
        }
    credentials = ZiniaoCredentials(job.company, job.username, job.password)
    client = ZiniaoClient(credentials, job.client_path, port=job.socket_port)
    _emit(progress, "正在连接紫鸟客户端")
    client.ensure_started()
    browsers = client.list_browsers()
    browser_by_name = _browser_name_index(browsers)
    success_count = 0
    failed_count = 0
    subsidy_count = 0
    total_order_count = 0

    try:
        for store in stores:
            store_name = str(store.get("store_name") or "").strip()
            if report_is_completed(job.manifest_path, (store.get("reports") or {}).get("income")):
                success_count += 1
                _emit(progress, f"{store_name}：补贴订单已采集过，跳过")
                continue
            browser = browser_by_name.get(store_name.casefold())
            if browser is None:
                message = "紫鸟店铺列表中未找到该店铺"
                _set_income_status(job.manifest_path, store_name, "failed", error_message=message)
                _emit(progress, f"{store_name}：{message}")
                failed_count += 1
                continue

            _set_income_status(job.manifest_path, store_name, "running")
            _emit(progress, f"{store_name}：开始采集补贴订单")
            store_dir = job.manifest_path.parent / "raw" / "ziniao" / str(store.get("safe_name")) / "income"
            try:
                collector = collect_warehouse_store if infer_store_type(store_name) == "warehouse" else collect_normal_store
                result = collector(
                    client,
                    browser,
                    store_dir,
                    manifest["start_date"],
                    manifest["end_date"],
                    progress=progress,
                )
                status = "success" if result["order_count"] else "no_data"
                relative_csv = str(Path(result["subsidy_file"]).relative_to(job.manifest_path.parent))
                relative_raw = str(Path(result["raw_file"]).relative_to(job.manifest_path.parent))
                _set_income_status(
                    job.manifest_path,
                    store_name,
                    status,
                    file=relative_csv,
                    raw_file=relative_raw,
                    row_count=result["order_count"],
                    subsidy_count=result["subsidy_count"],
                )
                success_count += 1
                subsidy_count += result["subsidy_count"]
                total_order_count += result["order_count"]
                _emit(
                    progress,
                    f"{store_name}：采集完成，收入订单 {result['order_count']} 条，补贴订单 {result['subsidy_count']} 条",
                )
            except Exception as exc:
                failed_count += 1
                _set_income_status(job.manifest_path, store_name, "failed", error_message=str(exc))
                _emit(progress, f"{store_name}：采集失败：{exc}")
    finally:
        client.exit_if_started()

    _finish_income_stage(job.manifest_path, failed_count)
    return {
        "manifest_path": str(job.manifest_path),
        "run_dir": str(job.manifest_path.parent),
        "success_count": success_count,
        "failed_count": failed_count,
        "order_count": total_order_count,
        "subsidy_count": subsidy_count,
    }


def collect_normal_store(
    client: ZiniaoClient,
    browser: dict[str, Any],
    output_dir: Path,
    start_date: str,
    end_date: str,
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    session = client.open_browser(browser, output_dir, headless=False)
    try:
        with sync_playwright() as playwright:
            connected = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{session.debugging_port}"
            )
            context = connected.contexts[0]
            page = context.pages[-1]
            page.set_default_timeout(30000)
            if session.launcher_page:
                page.goto(session.launcher_page, wait_until="domcontentloaded", timeout=120000)
            target_url = normal_income_url(session.launcher_page or page.url, start_date, end_date)
            page.goto(target_url, wait_until="domcontentloaded", timeout=120000)
            _complete_income_verification(page, progress=progress)
            raw_rows = collect_income_pages(page, start_date, end_date, progress=progress)
    finally:
        client.close_browser(session.browser_oauth)

    return _save_income_files(
        output_dir,
        raw_rows,
        session.browser_name,
        start_date,
        end_date,
        raw_payload=raw_rows,
    )


def collect_warehouse_store(
    client: ZiniaoClient,
    browser: dict[str, Any],
    output_dir: Path,
    start_date: str,
    end_date: str,
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    session = client.open_browser(browser, output_dir, headless=False)
    selected_payouts: list[dict[str, Any]] = []
    raw_rows: list[dict[str, Any]] = []
    try:
        with sync_playwright() as playwright:
            connected = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{session.debugging_port}"
            )
            context = connected.contexts[0]
            page = context.pages[-1]
            page.set_default_timeout(30000)
            if session.launcher_page:
                page.goto(session.launcher_page, wait_until="domcontentloaded", timeout=120000)
            parts = urlsplit(session.launcher_page or page.url)
            if parts.scheme not in {"http", "https"} or not parts.netloc:
                raise ValueError("紫鸟未返回有效的仓发店入口")
            page.goto(
                f"{parts.scheme}://{parts.netloc}/portal/finance/income",
                wait_until="domcontentloaded",
                timeout=120000,
            )
            _complete_income_verification(page, progress=progress)
            shop_info = _browser_get(page, SHOP_INFO_API, progress=progress)
            shop_data = shop_info.get("data") if isinstance(shop_info, dict) else {}
            shop_region = str((shop_data or {}).get("shop_region") or "").upper()
            if shop_region != "VN":
                raise RuntimeError(f"仓发账号当前子店不是越南店（当前区域：{shop_region or '未知'}）")
            payout_result = _browser_get(page, WAREHOUSE_PAYOUT_API, progress=progress)
            payout_data = payout_result.get("data") if isinstance(payout_result, dict) else {}
            payouts = (payout_data or {}).get("list") if isinstance(payout_data, dict) else []
            payouts = [item for item in payouts if isinstance(item, dict)] if isinstance(payouts, list) else []
            selected_payouts = filter_payouts_by_date(payouts, start_date, end_date)
            _emit(progress, f"{session.browser_name}：目标月份包含 {len(selected_payouts)} 个拨款轮次")
            for payout in selected_payouts:
                payout_ids = [
                    str(record.get("payout_id") or "")
                    for record in payout.get("payout_records") or []
                    if isinstance(record, dict) and record.get("payout_id")
                ]
                if not payout_ids:
                    continue
                rows = collect_warehouse_income_pages(
                    page,
                    payout_ids,
                    progress=progress,
                    payout_date=str(payout.get("payout_date") or ""),
                )
                raw_rows.extend(rows)
    finally:
        client.close_browser(session.browser_oauth)

    return _save_income_files(
        output_dir,
        raw_rows,
        session.browser_name,
        start_date,
        end_date,
        raw_payload={"selected_payouts": selected_payouts, "items": raw_rows},
    )


def filter_payouts_by_date(
    payouts: list[dict[str, Any]],
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    start_text = _iso_date(start_date)
    end_text = _iso_date(end_date)
    return [
        payout
        for payout in payouts
        if start_text <= str(payout.get("payout_date") or "") <= end_text
    ]


def build_warehouse_income_request(payout_ids: list[str]) -> dict[str, Any]:
    return {
        "source_type": 0,
        "income_category": 2,
        "pagination_info": {"direction": 0, "limit": 10},
        "cb_query_condition": {"payout_ids": [str(item) for item in payout_ids if str(item)]},
    }


def collect_warehouse_income_pages(
    page: Page,
    payout_ids: list[str],
    *,
    progress: ProgressCallback | None = None,
    payout_date: str = "",
    max_pages: int = 10000,
) -> list[dict[str, Any]]:
    payload = build_warehouse_income_request(payout_ids)
    rows: list[dict[str, Any]] = []
    seen_cursors: set[str] = set()
    for page_number in range(1, max_pages + 1):
        result = _browser_fetch(page, WAREHOUSE_INCOME_DETAIL_API, payload, progress=progress)
        if int(result.get("code") or 0) != 0:
            raise RuntimeError(str(result.get("user_message") or result.get("message") or "Shopee 仓发收入接口失败"))
        data = result.get("data")
        data = data if isinstance(data, dict) else {}
        page_rows = data.get("list")
        page_rows = [item for item in page_rows if isinstance(item, dict)] if isinstance(page_rows, list) else []
        rows.extend(page_rows)
        if progress is not None:
            progress(
                f"拨款轮次 {payout_date or '-'} 第 {page_number} 页完成，本页 {len(page_rows)} 条，轮次累计 {len(rows)} 条"
            )
        next_page = data.get("next_page")
        next_page = next_page if isinstance(next_page, dict) else {}
        cursor = str(next_page.get("cursor") or "")
        if not cursor or cursor in seen_cursors or not page_rows:
            break
        seen_cursors.add(cursor)
        payload = build_warehouse_income_request(payout_ids)
        payload["pagination_info"] = next_page
    else:
        raise RuntimeError(f"仓发收入数据超过最大分页数 {max_pages}")
    return rows


def _complete_income_verification(
    page: Page,
    *,
    progress: ProgressCallback | None = None,
    appear_timeout_ms: int = 3000,
) -> bool:
    try:
        handled = complete_shopee_password_verification(
            page,
            appear_timeout_ms=appear_timeout_ms,
        )
    except ShopeePasswordVerificationRequired as exc:
        raise IncomeVerificationRequired(str(exc)) from exc
    if handled:
        _emit(progress, "检测到 Shopee 登录密码验证，已自动点击 Verify 并通过")
    return handled


def _browser_fetch(
    page: Page,
    path: str,
    payload: dict[str, Any],
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    _complete_income_verification(page, progress=progress, appear_timeout_ms=0)
    result = page.evaluate(
        """async ({path, payload}) => {
          const response = await fetch(path, {
            method: 'POST',
            credentials: 'include',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify(payload),
          });
          return {status: response.status, body: await response.json()};
        }""",
        {"path": path, "payload": payload},
    )
    if int(result.get("status") or 0) != 200:
        raise RuntimeError(f"Shopee 收入接口返回 HTTP {result.get('status')}")
    body = result.get("body")
    if not isinstance(body, dict):
        raise RuntimeError("Shopee 收入接口返回格式不正确")
    return body


def _browser_get(
    page: Page,
    path: str,
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    _complete_income_verification(page, progress=progress, appear_timeout_ms=0)
    result = page.evaluate(
        """async (path) => {
          const response = await fetch(path, {method: 'GET', credentials: 'include'});
          return {status: response.status, body: await response.json()};
        }""",
        path,
    )
    if int(result.get("status") or 0) != 200:
        raise RuntimeError(f"Shopee 接口返回 HTTP {result.get('status')}：{path}")
    body = result.get("body")
    if not isinstance(body, dict):
        raise RuntimeError(f"Shopee 接口返回格式不正确：{path}")
    return body


def _save_income_files(
    output_dir: Path,
    raw_rows: list[dict[str, Any]],
    store_name: str,
    start_date: str,
    end_date: str,
    *,
    raw_payload: Any,
) -> dict[str, Any]:
    flat_rows = [flatten_income_item(item, store_name) for item in raw_rows]
    subsidy_rows = [row for row in flat_rows if _number(row.get("补贴金额")) != 0]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = output_dir / f"income_raw__{_compact_date(start_date)}_{_compact_date(end_date)}__{stamp}.json"
    csv_path = output_dir / f"subsidy_orders__{_compact_date(start_date)}_{_compact_date(end_date)}__{stamp}.csv"
    raw_path.write_text(json.dumps(raw_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUBSIDY_COLUMNS)
        writer.writeheader()
        writer.writerows(subsidy_rows)
    return {
        "raw_file": str(raw_path),
        "subsidy_file": str(csv_path),
        "order_count": len(flat_rows),
        "subsidy_count": len(subsidy_rows),
    }


def _set_income_status(
    manifest_path: Path,
    store_name: str,
    status: str,
    *,
    file: str = "",
    raw_file: str = "",
    row_count: int | None = None,
    subsidy_count: int | None = None,
    error_message: str = "",
) -> None:
    manifest = load_manifest(manifest_path)
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target = next(
        (store for store in manifest.get("stores") or [] if str(store.get("store_name") or "") == store_name),
        None,
    )
    if target is None:
        raise ValueError(f"批次清单中不存在店铺：{store_name}")
    report = target["reports"]["income"]
    report["status"] = status
    report["error_message"] = error_message
    report["error_code"] = "" if not error_message else "collection_failed"
    if status == "running":
        report["started_at"] = now_text
        report["finished_at"] = ""
    else:
        report["finished_at"] = now_text
    if file:
        report["file"] = file
    if raw_file:
        report["raw_file"] = raw_file
    if row_count is not None:
        report["row_count"] = row_count
    if subsidy_count is not None:
        report["subsidy_count"] = subsidy_count
    manifest["updated_at"] = now_text
    manifest["status"] = "collecting"
    _refresh_summary(manifest)
    write_manifest(manifest_path, manifest)


def _finish_income_stage(manifest_path: Path, failed_count: int) -> None:
    manifest = load_manifest(manifest_path)
    manifest["status"] = "partial_success" if failed_count else "income_ready"
    manifest["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _refresh_summary(manifest)
    write_manifest(manifest_path, manifest)


def _refresh_summary(manifest: dict[str, Any]) -> None:
    reports = [
        report
        for store in manifest.get("stores") or []
        for report in (store.get("reports") or {}).values()
        if isinstance(report, dict)
    ]
    for status in ("pending", "running", "success", "no_data", "failed"):
        manifest["summary"][f"{status}_count"] = sum(report.get("status") == status for report in reports)


def _browser_name_index(browsers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for browser in browsers:
        name = str(browser.get("browserName") or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key in result:
            duplicates.add(key)
        else:
            result[key] = browser
    for key in duplicates:
        result.pop(key, None)
    return result


def _compact_date(value: str) -> str:
    return _iso_date(value).replace("-", "")


def _iso_date(value: str) -> str:
    return datetime.strptime(str(value or "").strip(), "%Y-%m-%d").strftime("%Y-%m-%d")


def _number(value: Any) -> int | float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return int(number) if number.is_integer() else number


def _timestamp(value: Any) -> str:
    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)
