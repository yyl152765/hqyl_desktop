from __future__ import annotations

import base64
import csv
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from backend.core.ziniao_client import ZiniaoClient, ZiniaoCredentials
from backend.services.vietnam_income_collection import (
    infer_store_type,
    load_manifest,
    report_is_completed,
    write_manifest,
)
from backend.services.shopee_verification import (
    complete_shopee_password_verification,
    dismiss_shopee_ams_welcome,
)


ProgressCallback = Callable[[str], None]

VN_TIMEZONE = timezone(timedelta(hours=7))
SOURCE_REPORT_TYPES: tuple[str, ...] = ("ads", "affiliate", "ads_credit")

PAS_EXPORT_TRIGGER_API = "/api/pas/v1/report/export_job/trigger/"
PAS_EXPORT_SINGLE_API = "/api/pas/v1/report/export_job/get_single_result/"
PAS_EXPORT_DOWNLOAD_API = "/api/pas/v1/report/export_job/get_download_url/"
ADS_WALLET_TRANSACTION_API = "/api/pas/v1/transaction_history/get/"

AFFILIATE_GQL_API = "/api/v3/affiliateplatform/gql"
AFFILIATE_EXPORT_TASK_TYPE = "export_ams_conversion_report"

SUBMIT_AFFILIATE_EXPORT_MUTATION = """
mutation SubmitAsyncExportTaskMutation($params: String, $taskType: String!) {
  submitAsyncExportTask(params: $params, taskType: $taskType) {
    taskId
    success
  }
}
"""

AFFILIATE_EXPORT_TASK_LIST_QUERY = """
query AsyncExportTaskListQuery($fileName: String, $pageNum: Int!, $pageSize: Int!) {
  asyncExportTaskList(fileName: $fileName, pageNum: $pageNum, pageSize: $pageSize) {
    total
    taskInfoList {
      taskId
      taskType
      taskStatus
      fileKey
      fileName
      result
      progress
      reason
      exportTime
    }
  }
}
"""

ADS_CREDIT_COLUMNS = (
    "店铺名称",
    "日期",
    "交易分组",
    "交易类型",
    "金额",
    "充值金额",
    "付费余额",
    "免费余额",
    "交易ID",
    "来源/对象",
    "关联名称",
    "是否广告补贴候选",
)

ADS_CREDIT_KEYWORDS = (
    "free ads credit",
    "free_ads_credit",
    "roas protection",
    "roas_protection",
    "free ads credit rebate",
    "ads credit rebate",
    "credit_rebate",
)

AFFILIATE_EXPORT_FILE_PATTERN = re.compile(r"SellerConversionReport_\d+\.csv")
ADS_EXPORT_BUTTON_PATTERN = re.compile(r"Export\s*Data|导出数据|匯出資料|Xuất\s+dữ\s+liệu", re.I)
ADS_OVERALL_EXPORT_PATTERN = re.compile(
    r"Overall\s+Ads\s+Data|综合广告数据|整体广告数据|总体广告数据|全部广告数据|所有广告数据|Dữ\s+liệu.*quảng\s+cáo",
    re.I,
)


class ShopeeExportTimeout(RuntimeError):
    pass


@dataclass(frozen=True)
class ZiniaoSourcesCollectionJob:
    manifest_path: Path
    company: str
    username: str
    password: str
    client_path: Path
    socket_port: int = 16851


def validate_ziniao_sources_collection_payload(
    payload: dict[str, Any] | None,
    *,
    client_path: str | Path,
    socket_port: int = 16851,
) -> ZiniaoSourcesCollectionJob:
    data = dict(payload or {})
    manifest_path = Path(str(data.get("manifest_path") or "").strip())
    manifest = load_manifest(manifest_path)
    if manifest.get("source") != "ziniao":
        raise ValueError("清单来源不是紫鸟")
    company = str(data.get("company") or "").strip()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not company or not username or not password:
        raise ValueError("紫鸟账号信息不完整")
    resolved_client_path = Path(client_path)
    if not resolved_client_path.is_file():
        raise ValueError(f"紫鸟客户端不存在：{resolved_client_path}")
    return ZiniaoSourcesCollectionJob(
        manifest_path=manifest_path,
        company=company,
        username=username,
        password=password,
        client_path=resolved_client_path,
        socket_port=int(socket_port),
    )


def run_ziniao_sources_collection(
    job: ZiniaoSourcesCollectionJob,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(job.manifest_path)
    stores = [
        store
        for store in manifest.get("stores") or []
        if str(store.get("store_name") or "").strip() and str(store.get("safe_name") or "").strip()
    ]
    completed_cells = sum(
        report_is_completed(job.manifest_path, (store.get("reports") or {}).get(report_type))
        for store in stores
        for report_type in SOURCE_REPORT_TYPES
    )
    total_cells = len(stores) * len(SOURCE_REPORT_TYPES)
    if stores and completed_cells == total_cells:
        _emit(progress, "广告、联盟、广告补贴已全部采集过，本次跳过")
        return {
            "manifest_path": str(job.manifest_path),
            "run_dir": str(job.manifest_path.parent),
            "success_count": completed_cells,
            "no_data_count": 0,
            "failed_count": 0,
            "file_count": 0,
            "skipped_count": completed_cells,
        }
    credentials = ZiniaoCredentials(job.company, job.username, job.password)
    client = ZiniaoClient(credentials, job.client_path, port=job.socket_port)
    _emit(progress, "正在连接紫鸟浏览器，准备采集广告、联盟、广告补贴数据")
    client.ensure_started()
    browsers = client.list_browsers()
    browser_by_name = _browser_name_index(browsers)

    success_count = 0
    failed_count = 0
    no_data_count = 0
    file_count = 0
    skipped_count = 0

    try:
        for store in stores:
            store_name = str(store.get("store_name") or "").strip()
            safe_name = str(store.get("safe_name") or "").strip()
            reports = store.get("reports") or {}
            pending_report_types = [
                report_type
                for report_type in SOURCE_REPORT_TYPES
                if not report_is_completed(job.manifest_path, reports.get(report_type))
            ]
            completed_report_types = [report_type for report_type in SOURCE_REPORT_TYPES if report_type not in pending_report_types]
            if completed_report_types:
                skipped_count += len(completed_report_types)
                _emit(
                    progress,
                    f"{store_name}：已采集过 {', '.join(_report_label(item) for item in completed_report_types)}，跳过",
                )
            if not pending_report_types:
                continue
            browser = browser_by_name.get(store_name.casefold())
            if browser is None:
                message = "紫鸟浏览器列表中没有找到这个店铺"
                for report_type in pending_report_types:
                    _set_report_status(job.manifest_path, store_name, report_type, "failed", error_message=message)
                    failed_count += 1
                _emit(progress, f"{store_name}：{message}")
                continue

            store_root = job.manifest_path.parent / "raw" / "ziniao" / safe_name
            for report_type in pending_report_types:
                _set_report_status(job.manifest_path, store_name, report_type, "running")

            session = None
            connected = None
            try:
                session = client.open_browser(browser, store_root, headless=False)
                with sync_playwright() as playwright:
                    connected = playwright.chromium.connect_over_cdp(
                        f"http://127.0.0.1:{session.debugging_port}"
                    )
                    context = connected.contexts[0]
                    page = context.pages[-1]
                    page.set_default_timeout(30000)
                    if session.launcher_page:
                        page.goto(session.launcher_page, wait_until="domcontentloaded", timeout=120000)

                    collectors = (
                        ("ads", collect_ads_export),
                        ("affiliate", collect_affiliate_export),
                        ("ads_credit", collect_ads_credit_history),
                    )
                    for report_type, collector in collectors:
                        if report_type not in pending_report_types:
                            continue
                        output_dir = store_root / report_type
                        try:
                            result = collector(
                                page,
                                session.launcher_page or page.url,
                                output_dir,
                                manifest["start_date"],
                                manifest["end_date"],
                                store_name=store_name,
                                store_type=infer_store_type(store_name),
                                progress=progress,
                            )
                            row_count = int(result.get("row_count") or 0)
                            status = "success" if row_count > 0 or result.get("file") else "no_data"
                            if status == "success":
                                success_count += 1
                            else:
                                no_data_count += 1
                            if result.get("file"):
                                file_count += 1
                            _set_report_status(
                                job.manifest_path,
                                store_name,
                                report_type,
                                status,
                                file=_relative_to_run(job.manifest_path, result.get("file")),
                                raw_file=_relative_to_run(job.manifest_path, result.get("raw_file")),
                                row_count=row_count,
                                extra={
                                    key: value
                                    for key, value in result.items()
                                    if key not in {"file", "raw_file", "row_count"}
                                },
                            )
                            _emit(progress, f"{store_name}：{_report_label(report_type)}采集完成，记录 {row_count} 条")
                        except Exception as exc:
                            failed_count += 1
                            _set_report_status(
                                job.manifest_path,
                                store_name,
                                report_type,
                                "failed",
                                error_message=str(exc),
                            )
                            _emit(progress, f"{store_name}：{_report_label(report_type)}采集失败：{exc}")
            finally:
                if connected is not None:
                    try:
                        connected.close()
                    except Exception:
                        pass
                if session is not None:
                    try:
                        client.close_browser(session.browser_oauth)
                    except Exception as exc:
                        _emit(progress, f"{store_name}：关闭紫鸟浏览器失败：{exc}")
    finally:
        client.exit_if_started()

    _finish_sources_stage(job.manifest_path, failed_count)
    return {
        "manifest_path": str(job.manifest_path),
        "run_dir": str(job.manifest_path.parent),
        "success_count": success_count,
        "no_data_count": no_data_count,
        "failed_count": failed_count,
        "file_count": file_count,
        "skipped_count": skipped_count,
    }


def ads_report_url(launcher_page: str, start_date: str, end_date: str) -> str:
    base = _portal_base_url(launcher_page)
    return (
        f"{base}/portal/marketing/pas/index?"
        f"from={vn_day_timestamp(start_date)}&"
        f"to={vn_day_timestamp(end_date, end_of_day=True)}&"
        "type=new_cpc_homepage&group=custom&offset=777.7777709960938"
    )


def build_ads_export_payload(start_date: str, end_date: str) -> dict[str, Any]:
    return {
        "language": "en",
        "report_type": "product_homepage_v2__overall",
        "start_time": vn_day_timestamp(start_date),
        "end_time": vn_day_timestamp(end_date, end_of_day=True),
    }


def collect_ads_export(
    page: Page,
    launcher_page: str,
    output_dir: Path,
    start_date: str,
    end_date: str,
    *,
    store_name: str,
    store_type: str = "normal",
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    del store_type
    output_dir.mkdir(parents=True, exist_ok=True)
    page.goto(ads_report_url(launcher_page, start_date, end_date), wait_until="domcontentloaded", timeout=120000)
    complete_shopee_password_verification(page)
    _wait_soft_network_idle(page)
    _emit(progress, f"{store_name}：已进入 Shopee Ads 页面，准备点击 Export Data")

    export_id: int | None = None
    export_info: dict[str, Any] = {}
    source_method = "page_export_overall_ads_data"
    try:
        export_id = trigger_ads_overall_export_from_page(page)
        _emit(progress, f"{store_name}：广告报表已提交导出任务 {export_id}，等待平台生成")
        export_info = poll_ads_export(page, export_id, progress=progress)
        download_url = get_ads_export_download_url(page, export_id)
        fallback_name = str(export_info.get("file_name") or f"shopee_ads_overall_{_compact_date(start_date)}_{_compact_date(end_date)}.csv")
        csv_path = _download_url_with_browser(page, download_url, output_dir, fallback_name)
    except PlaywrightTimeoutError:
        _emit(progress, f"{store_name}：未捕获到广告导出接口响应，改从页面最新报告下载")
        csv_path = download_latest_ads_report_from_page(page, output_dir, start_date, end_date)
        source_method = "page_export_latest_reports_download"
    row_count = _csv_data_row_count(csv_path)

    meta_path = _unique_path(output_dir / f"ads_export_meta__{_stamp()}.json")
    meta_path.write_text(
        json.dumps(
            {
                "store_name": store_name,
                "source": "Shopee Ads 页面 Export Data / Overall Ads Data",
                "export_id": export_id,
                "export_info": export_info,
                "start_date": start_date,
                "end_date": end_date,
                "file": str(csv_path),
                "source_method": source_method,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "file": str(csv_path),
        "raw_file": str(meta_path),
        "row_count": row_count,
        "export_id": export_id,
        "source_method": source_method,
    }


def trigger_ads_overall_export_from_page(page: Page) -> int:
    export_button = page.get_by_role("button", name=ADS_EXPORT_BUTTON_PATTERN).first
    if not export_button.count():
        export_button = page.locator("button").filter(has_text=ADS_EXPORT_BUTTON_PATTERN).first
    export_button.wait_for(state="visible", timeout=60000)
    export_button.click()
    menu_item = page.locator('[data-testid="export-data-dropdown-item"]').filter(has_text=ADS_OVERALL_EXPORT_PATTERN).first
    if not menu_item.count():
        menu_item = page.get_by_text(ADS_OVERALL_EXPORT_PATTERN).first
    menu_item.wait_for(state="visible", timeout=30000)
    with page.expect_response(
        lambda response: PAS_EXPORT_TRIGGER_API in response.url and response.request.method == "POST",
        timeout=60000,
    ) as response_info:
        menu_item.click()
    body = response_info.value.json()
    if int(body.get("code") or 0) != 0:
        raise RuntimeError(str(body.get("msg") or body.get("message") or "广告报表导出任务提交失败"))
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    try:
        return int(data.get("export_id"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("广告报表导出任务没有返回 export_id") from exc


def poll_ads_export(
    page: Page,
    export_id: int,
    *,
    progress: ProgressCallback | None = None,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_seconds, 30)
    last_status = ""
    while time.monotonic() < deadline:
        body = _post_json(page, PAS_EXPORT_SINGLE_API, {"export_id": int(export_id)})
        if int(body.get("code") or 0) != 0:
            raise RuntimeError(str(body.get("msg") or body.get("message") or "广告报表导出状态查询失败"))
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        status = str(data.get("status") or "").lower()
        if status and status != last_status:
            _emit(progress, f"广告报表导出任务 {export_id} 状态：{status}")
            last_status = status
        if status == "success":
            return data
        if status in {"failed", "fail", "error"}:
            raise RuntimeError(f"广告报表导出失败：{data}")
        time.sleep(3)
    raise ShopeeExportTimeout(f"广告报表导出任务 {export_id} 超时")


def get_ads_export_download_url(page: Page, export_id: int) -> str:
    body = _post_json(page, PAS_EXPORT_DOWNLOAD_API, {"export_id": int(export_id)})
    if int(body.get("code") or 0) != 0:
        raise RuntimeError(str(body.get("msg") or body.get("message") or "广告报表下载地址获取失败"))
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    url = str(data.get("download_url") or "").strip()
    if not url:
        raise RuntimeError("广告报表下载地址为空")
    return url


def download_latest_ads_report_from_page(
    page: Page,
    output_dir: Path,
    start_date: str,
    end_date: str,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    expected_names = ads_report_name_candidates(start_date, end_date)
    _open_ads_latest_reports(page)
    page.wait_for_timeout(3000)
    for _attempt in range(12):
        button = _visible_ads_download_button(page, expected_names)
        if button is not None:
            with page.expect_download(timeout=120000) as download_info:
                button.click()
            download = download_info.value
            suggested = _safe_filename(download.suggested_filename or expected_names[0])
            target = _unique_path(output_dir / suggested)
            download.save_as(str(target))
            return target
        page.wait_for_timeout(5000)
    raise ShopeeExportTimeout("广告报表已提交，但页面最新报告中未找到目标日期的下载按钮")


def _open_ads_latest_reports(page: Page) -> None:
    latest_reports_pattern = re.compile(r"Latest Reports|最新报告|最新報告|Báo cáo mới nhất", re.I)
    headings = page.get_by_text(latest_reports_pattern)
    try:
        if any(headings.nth(index).is_visible() for index in range(headings.count())):
            return
    except Exception:
        pass

    trigger = page.locator('[data-testid="export-data-result-trigger"]').first
    if not trigger.count():
        export_button = page.locator('[data-testid="export-data-dropdown-trigger"]').first
        if export_button.count():
            trigger = export_button.locator("xpath=following::button[1]")
    trigger.wait_for(state="visible", timeout=30000)
    trigger.click()
    try:
        page.get_by_text(latest_reports_pattern).last.wait_for(state="visible", timeout=10000)
    except PlaywrightTimeoutError:
        # The report rows can still be actionable in builds without a heading.
        pass


def ads_report_name_candidates(start_date: str, end_date: str) -> list[str]:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    return [
        f"Shopee广告-整体-数据-{start.strftime('%Y/%m/%d')}-{end.strftime('%Y/%m/%d')}.csv",
        f"Shopee-Ads-Overall-Data-{start.strftime('%d/%m/%Y')}-{end.strftime('%d/%m/%Y')}.csv",
        f"{start.strftime('%Y/%m/%d')}-{end.strftime('%Y/%m/%d')}",
        f"{start.strftime('%d/%m/%Y')}-{end.strftime('%d/%m/%Y')}",
    ]


def _report_row_with_download(page: Page, expected_text: str):
    download_text = re.compile(r"Download|下载|下載|Tải xuống", re.I)
    for selector in ("tr", ".eds-table-row", "li", "div"):
        row = page.locator(selector).filter(has_text=expected_text).filter(has_text=download_text).first
        if row.count():
            return row
    return None


def _visible_ads_download_button(page: Page, expected_names: list[str]):
    """Return the visible Download button nearest the requested report row.

    Shopee keeps hidden report drawers in the DOM. Selecting the first matching
    row can therefore resolve to a hidden button even while the current drawer
    contains a visible one.
    """
    download_text = re.compile(r"Download|下载|下載|Tải xuống", re.I)
    buttons = page.get_by_role("button", name=download_text)
    best_button = None
    best_depth = 10_000
    try:
        count = buttons.count()
    except Exception:
        return None
    for index in range(count):
        button = buttons.nth(index)
        try:
            if not button.is_visible() or not button.is_enabled():
                continue
            depth = button.evaluate(
                """
                (element, names) => {
                  let node = element;
                  for (let depth = 0; node && depth <= 10; depth += 1) {
                    const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (names.some((name) => text.includes(name))) return depth;
                    node = node.parentElement;
                  }
                  return -1;
                }
                """,
                expected_names,
            )
        except Exception:
            continue
        if isinstance(depth, int) and 0 <= depth < best_depth:
            best_button = button
            best_depth = depth
    return best_button


def collect_affiliate_export(
    page: Page,
    launcher_page: str,
    output_dir: Path,
    start_date: str,
    end_date: str,
    *,
    store_name: str,
    store_type: str = "normal",
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    del store_type
    output_dir.mkdir(parents=True, exist_ok=True)
    base = _portal_base_url(launcher_page)
    existing_files = read_affiliate_export_file_names(page, base)
    page.goto(f"{base}/portal/web-seller-affiliate/conversion_report", wait_until="domcontentloaded", timeout=120000)
    complete_shopee_password_verification(page)
    _wait_soft_network_idle(page)
    dismiss_shopee_ams_welcome(page)

    params = affiliate_export_params(start_date, end_date)
    task = submit_affiliate_export_task(page, params)
    task_id = str(task.get("taskId") or "").strip()
    if not task_id:
        raise RuntimeError("联盟 Conversion Report 导出任务没有返回 taskId")
    _emit(progress, f"{store_name}：联盟报表已提交导出任务 {task_id}，等待平台生成")
    file_name = wait_for_new_affiliate_export_file(
        page,
        base,
        existing_files,
        progress=progress,
    )
    csv_path = download_affiliate_export_from_page(page, base, file_name, output_dir)
    row_count = _csv_data_row_count(csv_path)

    meta_path = _unique_path(output_dir / f"affiliate_export_meta__{_stamp()}.json")
    meta_path.write_text(
        json.dumps(
            {
                "store_name": store_name,
                "source": "Affiliate Marketing Solution / Conversion Report 导出 CSV",
                "task": task,
                "file_name": file_name,
                "params": json.loads(params),
                "file": str(csv_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "file": str(csv_path),
        "raw_file": str(meta_path),
        "row_count": row_count,
        "task_id": task_id,
        "file_name": file_name,
        "source_method": "affiliate_conversion_report_export",
    }


def affiliate_export_params(start_date: str, end_date: str) -> str:
    return json.dumps(
        {
            "conversionReportFilter": {
                "purchase_time_s": vn_day_timestamp(start_date),
                "purchase_time_e": vn_day_timestamp(end_date, end_of_day=True),
            }
        },
        separators=(",", ":"),
    )


def submit_affiliate_export_task(page: Page, params: str) -> dict[str, Any]:
    body = _affiliate_gql(
        page,
        "SubmitAsyncExportTaskMutation",
        SUBMIT_AFFILIATE_EXPORT_MUTATION,
        {"params": params, "taskType": AFFILIATE_EXPORT_TASK_TYPE},
    )
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    task = data.get("submitAsyncExportTask") if isinstance(data.get("submitAsyncExportTask"), dict) else {}
    if not task.get("success"):
        raise RuntimeError(f"联盟 Conversion Report 导出任务提交失败：{body}")
    return task


def poll_affiliate_export_task(
    page: Page,
    task_id: str,
    *,
    progress: ProgressCallback | None = None,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_seconds, 30)
    last_status = ""
    while time.monotonic() < deadline:
        body = _affiliate_gql(
            page,
            "AsyncExportTaskListQuery",
            AFFILIATE_EXPORT_TASK_LIST_QUERY,
            {"fileName": "", "pageNum": 1, "pageSize": 10},
        )
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        task_list = data.get("asyncExportTaskList") if isinstance(data.get("asyncExportTaskList"), dict) else {}
        tasks = task_list.get("taskInfoList") if isinstance(task_list.get("taskInfoList"), list) else []
        task = next((item for item in tasks if str(item.get("taskId") or "") == str(task_id)), None)
        if isinstance(task, dict):
            status = str(task.get("taskStatus") or "")
            if status and status != last_status:
                _emit(progress, f"联盟导出任务 {task_id} 状态：{status}")
                last_status = status
            if status == "TaskStatusSuccess":
                return task
            if status in {"TaskStatusFailed", "TaskStatusError"}:
                raise RuntimeError(f"联盟 Conversion Report 导出失败：{task}")
        time.sleep(3)
    raise ShopeeExportTimeout(f"联盟 Conversion Report 导出任务 {task_id} 超时")


def read_affiliate_export_file_names(page: Page, base_url: str) -> set[str]:
    page.goto(f"{base_url}/portal/web-seller-affiliate/export", wait_until="domcontentloaded", timeout=120000)
    complete_shopee_password_verification(page)
    _wait_soft_network_idle(page)
    dismiss_shopee_ams_welcome(page)
    try:
        page.wait_for_timeout(3000)
        text = page.locator("body").inner_text(timeout=10000)
    except PlaywrightTimeoutError:
        return set()
    return set(AFFILIATE_EXPORT_FILE_PATTERN.findall(text))


def wait_for_new_affiliate_export_file(
    page: Page,
    base_url: str,
    existing_files: set[str],
    *,
    progress: ProgressCallback | None = None,
    timeout_seconds: int = 300,
) -> str:
    deadline = time.monotonic() + max(timeout_seconds, 30)
    last_seen = ""
    while time.monotonic() < deadline:
        page.goto(f"{base_url}/portal/web-seller-affiliate/export", wait_until="domcontentloaded", timeout=120000)
        complete_shopee_password_verification(page)
        _wait_soft_network_idle(page)
        dismiss_shopee_ams_welcome(page)
        try:
            page.wait_for_timeout(3000)
            text = page.locator("body").inner_text(timeout=10000)
        except PlaywrightTimeoutError:
            text = ""
        names = AFFILIATE_EXPORT_FILE_PATTERN.findall(text)
        new_names = [name for name in names if name not in existing_files]
        if new_names:
            return new_names[0]
        first = names[0] if names else ""
        if first and first != last_seen:
            _emit(progress, f"联盟导出列表最新文件仍为 {first}，继续等待新文件")
            last_seen = first
        time.sleep(5)
    raise ShopeeExportTimeout("联盟 Conversion Report 导出文件生成超时")


def download_affiliate_export_from_page(
    page: Page,
    base_url: str,
    file_name: str,
    output_dir: Path,
) -> Path:
    page.goto(f"{base_url}/portal/web-seller-affiliate/export", wait_until="domcontentloaded", timeout=120000)
    complete_shopee_password_verification(page)
    _wait_soft_network_idle(page)
    dismiss_shopee_ams_welcome(page)
    page.get_by_text(file_name, exact=True).wait_for(timeout=60000)
    row = page.locator("tr", has_text=file_name).first
    if not row.count():
        row = page.locator("div", has_text=file_name).first
    button = row.get_by_role("button", name=re.compile(r"Download|下载|Tải xuống", re.I)).first
    button.wait_for(state="visible", timeout=30000)
    with page.expect_download(timeout=120000) as download_info:
        button.click()
    download = download_info.value
    suggested = _safe_filename(download.suggested_filename or file_name)
    target = _unique_path(output_dir / suggested)
    download.save_as(str(target))
    return target


def collect_ads_credit_history(
    page: Page,
    launcher_page: str,
    output_dir: Path,
    start_date: str,
    end_date: str,
    *,
    store_name: str,
    store_type: str = "normal",
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    base = _portal_base_url(launcher_page)
    page.goto(f"{base}/portal/marketing/pas/wallet", wait_until="domcontentloaded", timeout=120000)
    complete_shopee_password_verification(page)
    _wait_soft_network_idle(page)
    rows = collect_ads_credit_transactions(page, start_date, end_date, progress=progress)
    candidate_rows = [
        flatten_ads_credit_transaction(row, store_name, store_type)
        for row in rows
        if is_ads_credit_transaction(row, store_type=store_type)
    ]
    all_rows = [flatten_ads_credit_transaction(row, store_name, store_type) for row in rows]

    stamp = _stamp()
    raw_path = _unique_path(output_dir / f"ads_wallet_transactions_raw__{_compact_date(start_date)}_{_compact_date(end_date)}__{stamp}.json")
    all_csv_path = _unique_path(output_dir / f"ads_wallet_transactions_all__{_compact_date(start_date)}_{_compact_date(end_date)}__{stamp}.csv")
    credit_csv_path = _unique_path(output_dir / f"ads_credit_candidates__{_compact_date(start_date)}_{_compact_date(end_date)}__{stamp}.csv")
    raw_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_dict_csv(all_csv_path, all_rows, ADS_CREDIT_COLUMNS)
    _write_dict_csv(credit_csv_path, candidate_rows, ADS_CREDIT_COLUMNS)
    return {
        "file": str(credit_csv_path),
        "raw_file": str(raw_path),
        "row_count": len(candidate_rows),
        "transaction_count": len(rows),
        "all_file": str(all_csv_path),
        "source_method": "ads_wallet_transaction_history",
    }


def build_ads_credit_request(
    start_date: str,
    end_date: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    return {
        "start_time": vn_day_timestamp(start_date),
        "end_time": vn_day_timestamp(end_date, end_of_day=True),
        "transaction_type_list": [],
        "limit": int(limit),
        "offset": int(offset),
        "new_transaction_log_flag": True,
    }


def collect_ads_credit_transactions(
    page: Page,
    start_date: str,
    end_date: str,
    *,
    progress: ProgressCallback | None = None,
    limit: int = 50,
    max_pages: int = 1000,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total: int | None = None
    for page_number in range(max_pages):
        offset = page_number * limit
        body = _post_json(page, ADS_WALLET_TRANSACTION_API, build_ads_credit_request(start_date, end_date, limit=limit, offset=offset))
        if int(body.get("code") or 0) != 0:
            raise RuntimeError(str(body.get("msg") or body.get("message") or "广告钱包交易流水查询失败"))
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        page_rows = data.get("translog_list") or data.get("list") or []
        page_rows = [item for item in page_rows if isinstance(item, dict)] if isinstance(page_rows, list) else []
        if total is None:
            total = _safe_int(data.get("total"), len(page_rows))
        rows.extend(page_rows)
        _emit(progress, f"广告钱包流水第 {page_number + 1} 页：{len(page_rows)} 条，累计 {len(rows)} / {total or '?'}")
        if not page_rows or (total is not None and len(rows) >= total):
            break
    else:
        raise RuntimeError(f"广告钱包流水超过最大分页数 {max_pages}")
    return rows


def is_ads_credit_transaction(item: dict[str, Any], *, store_type: str = "normal") -> bool:
    text = " ".join(
        str(item.get(key) or "")
        for key in (
            "transaction_group",
            "transaction_type",
            "received_from",
            "target_affiliate_name",
            "description",
            "remark",
            "title",
        )
    ).casefold()
    keywords = ADS_CREDIT_KEYWORDS
    if store_type == "warehouse":
        keywords = (*ADS_CREDIT_KEYWORDS, "technical support fee", "technical_support_fee")
    return any(keyword in text for keyword in keywords)


def flatten_ads_credit_transaction(item: dict[str, Any], store_name: str, store_type: str = "normal") -> dict[str, Any]:
    return {
        "店铺名称": store_name,
        "日期": _timestamp(item.get("date")),
        "交易分组": str(item.get("transaction_group") or ""),
        "交易类型": str(item.get("transaction_type") or ""),
        "金额": _number(item.get("amount")),
        "充值金额": _number(item.get("topup")),
        "付费余额": _number(item.get("paid_credit")),
        "免费余额": _number(item.get("free_credit")),
        "交易ID": str(item.get("transaction_id") or ""),
        "来源/对象": str(item.get("received_from") or ""),
        "关联名称": str(item.get("target_affiliate_name") or ""),
        "是否广告补贴候选": "是" if is_ads_credit_transaction(item, store_type=store_type) else "否",
    }


def vn_day_timestamp(value: str, *, end_of_day: bool = False) -> int:
    parsed = datetime.strptime(str(value or "").strip(), "%Y-%m-%d")
    if end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    parsed = parsed.replace(tzinfo=VN_TIMEZONE)
    return int(parsed.timestamp())


def _post_json(page: Page, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = page.evaluate(
        """async ({path, payload}) => {
          const response = await fetch(path, {
            method: 'POST',
            credentials: 'include',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify(payload),
          });
          const text = await response.text();
          let body = {};
          try { body = text ? JSON.parse(text) : {}; } catch (_error) { body = {raw: text}; }
          return {status: response.status, body};
        }""",
        {"path": path, "payload": payload},
    )
    if int(result.get("status") or 0) != 200:
        body = result.get("body")
        message = ""
        if isinstance(body, dict):
            message = str(body.get("message") or body.get("msg") or body.get("error") or body.get("raw") or "")
        suffix = f"；{message}" if message else ""
        raise RuntimeError(f"Shopee 请求失败 HTTP {result.get('status')}：{path}{suffix}")
    body = result.get("body")
    if not isinstance(body, dict):
        raise RuntimeError(f"Shopee 请求返回格式异常：{path}")
    return body


def _affiliate_gql(page: Page, operation_name: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    path = AFFILIATE_GQL_API
    if operation_name == "AsyncExportTaskListQuery":
        path = f"{AFFILIATE_GQL_API}?q=asyncExportTaskList"
    body = _post_json(
        page,
        path,
        {"operationName": operation_name, "query": query, "variables": variables},
    )
    if body.get("errors"):
        raise RuntimeError(f"联盟 GraphQL 请求失败：{body.get('errors')}")
    return body


def _download_url_with_browser(page: Page, url: str, output_dir: Path, fallback_name: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with page.expect_download(timeout=120000) as download_info:
            page.evaluate(
                """url => {
                  const link = document.createElement('a');
                  link.href = url;
                  link.style.display = 'none';
                  document.body.appendChild(link);
                  link.click();
                  link.remove();
                }""",
                url,
            )
        download = download_info.value
        suggested = _safe_filename(download.suggested_filename or fallback_name)
        target = _unique_path(output_dir / suggested)
        download.save_as(str(target))
        return target
    except PlaywrightTimeoutError:
        fetched = page.evaluate(
            """async (url) => {
              const response = await fetch(url, {credentials: 'include'});
              const buffer = await response.arrayBuffer();
              let binary = '';
              const bytes = new Uint8Array(buffer);
              const chunkSize = 0x8000;
              for (let i = 0; i < bytes.length; i += chunkSize) {
                binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
              }
              return {
                ok: response.ok,
                status: response.status,
                contentType: response.headers.get('content-type') || '',
                data: btoa(binary),
              };
            }""",
            url,
        )
        if not fetched.get("ok"):
            raise RuntimeError(f"文件下载失败 HTTP {fetched.get('status')}")
        target = _unique_path(output_dir / _safe_filename(fallback_name))
        target.write_bytes(base64.b64decode(str(fetched.get("data") or "")))
        return target


def _set_report_status(
    manifest_path: Path,
    store_name: str,
    report_type: str,
    status: str,
    *,
    file: str = "",
    raw_file: str = "",
    row_count: int | None = None,
    error_message: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    manifest = load_manifest(manifest_path)
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target = next(
        (store for store in manifest.get("stores") or [] if str(store.get("store_name") or "") == store_name),
        None,
    )
    if target is None:
        raise ValueError(f"清单中没有这个店铺：{store_name}")
    reports = target.setdefault("reports", {})
    report = reports.setdefault(report_type, {})
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
    if extra:
        for key, value in extra.items():
            if key not in {"password", "username", "company"}:
                report[key] = value
    manifest["updated_at"] = now_text
    manifest["status"] = "collecting"
    _refresh_summary(manifest)
    write_manifest(manifest_path, manifest)


def _finish_sources_stage(manifest_path: Path, failed_count: int) -> None:
    manifest = load_manifest(manifest_path)
    manifest["status"] = "partial_success" if failed_count else "sources_ready"
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


def _portal_base_url(launcher_page: str) -> str:
    parts = urlsplit(str(launcher_page or ""))
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("紫鸟没有返回有效的 Shopee 店铺入口地址")
    return f"{parts.scheme}://{parts.netloc}"


def _wait_soft_network_idle(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass


def _relative_to_run(manifest_path: Path, value: Any) -> str:
    if not value:
        return ""
    try:
        return str(Path(str(value)).relative_to(manifest_path.parent))
    except ValueError:
        return str(value)


def _csv_data_row_count(path: Path) -> int:
    payload = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1258", "latin-1"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = payload.decode("utf-8", errors="replace")
    rows = list(csv.reader(text.splitlines()))
    return max(len(rows) - 1, 0)


def _write_dict_csv(path: Path, rows: list[dict[str, Any]], fieldnames: tuple[str, ...]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _safe_filename(value: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or f"download_{_stamp()}.csv"


def _unique_path(path: Path) -> Path:
    candidate = path
    counter = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        counter += 1
    return candidate


def _compact_date(value: str) -> str:
    return datetime.strptime(str(value or "").strip(), "%Y-%m-%d").strftime("%Y%m%d")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _number(value: Any) -> int | float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return int(number) if number.is_integer() else number


def _timestamp(value: Any) -> str:
    timestamp = _safe_int(value, 0)
    if timestamp <= 0:
        return ""
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _report_label(report_type: str) -> str:
    return {
        "ads": "广告花费",
        "affiliate": "联盟花费",
        "ads_credit": "广告补贴",
    }.get(report_type, report_type)


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)
