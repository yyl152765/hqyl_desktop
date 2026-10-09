from __future__ import annotations

import csv
import io
import json
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from backend.core.mabang_client import MabangApiError, MabangClient
from backend.services.vietnam_income_collection import (
    load_manifest,
    report_is_completed,
    write_manifest,
)
from backend.services.vietnam_mabang_income import (
    MABANG_BASE_URL,
    SHOPEE_PLATFORM_ID,
    _ensure_mabang_section,
    _set_report_running,
    _touch_manifest,
)


ProgressCallback = Callable[[str], None]

REFUND_LIST_PATH = "/index.php?mod=paypalconfig.paypalorderlist"
REFUND_SEARCH_PATH = "/index.php?mod=paypal.dosearchpaypalrefund"
REFUND_EXPORT_PATH = "/index.php?mod=paypal.findpaypalexcelout&ids=&type=2&downloadType=2"
REFUND_PAGE_SIZE = 20
REFUND_DIRECT_EXPORT_LIMIT = 50_000

REFUND_SEGMENT_KEYS = (
    "refund_part_01_10",
    "refund_part_11_20",
    "refund_part_21_end",
)

REFUND_REQUIRED_COLUMNS = (
    "订单号",
    "订单状态",
    "创建退款时订单状态",
    "平台",
    "店铺",
    "国家",
    "退款单编号",
    "退款金额",
    "退款币种",
    "退款金额RMB",
    "退款时间",
    "退款原因",
)

REFUND_EXPORT_COLUMNS = (
    "订单号",
    "交易单号",
    "订单状态",
    "创建退款时订单状态",
    "状态",
    "平台",
    "店铺",
    "买家账号",
    "客户姓名",
    "订单金额",
    "运费(支出运费)",
    "SKU",
    "退款数量(仅支持2021年12月22日之后创建的退款单)",
    "sku中文名",
    "平台sku",
    "成本价",
    "开发员",
    "采购员",
    "销售员",
    "付款流水号",
    "物流方式",
    "国家",
    "订单时间",
    "是否成功退款",
    "退款流水号",
    "退款单编号",
    "退款金额",
    "退款币种",
    "退款金额RMB",
    "退款金额USD",
    "商品税",
    "退款运费税",
    "退款类型",
    "出资类型",
    "申请人",
    "申请时间",
    "退款时间",
    "发货时间",
    "退款原因",
    "备注",
    "描述",
    "货运单号",
)


@dataclass(frozen=True)
class RefundSegment:
    key: str
    label: str
    start_date: str
    end_date: str


@dataclass(frozen=True)
class VietnamMabangRefundJob:
    manifest_path: Path
    username: str
    password: str
    account_name: str = ""
    base_url: str = MABANG_BASE_URL


def validate_vietnam_mabang_refund_payload(
    payload: dict[str, Any] | None,
) -> VietnamMabangRefundJob:
    data = dict(payload or {})
    manifest_path = Path(str(data.get("manifest_path") or "").strip())
    manifest = load_manifest(manifest_path)
    mabang = _ensure_mabang_section(manifest)
    reports = mabang["reports"]
    if str((mabang.get("validation") or {}).get("status") or "") != "passed":
        raise ValueError("请先完成马帮收支明细与汇总校验")
    if not all(
        report_is_completed(manifest_path, reports.get(key))
        for key in ("income_detail", "income_summary")
    ):
        raise ValueError("马帮收支明细或汇总文件不存在，请先重新运行收支采集")
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not username or not password:
        raise ValueError("马帮账号配置不完整")
    return VietnamMabangRefundJob(
        manifest_path=manifest_path,
        username=username,
        password=password,
        account_name=str(data.get("account_name") or username).strip(),
        base_url=str(data.get("base_url") or MABANG_BASE_URL).strip(),
    )


def refund_segments(start_date: str, end_date: str) -> list[RefundSegment]:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if start.year != end.year or start.month != end.month:
        raise ValueError("退款采集只支持同一自然月")
    last_day = monthrange(start.year, start.month)[1]
    month_start = start.replace(day=1)
    month_end = start.replace(day=last_day)
    if start != month_start or end != month_end:
        raise ValueError("退款采集批次必须覆盖完整自然月")
    return [
        RefundSegment("refund_part_01_10", "01-10", str(start.replace(day=1)), str(start.replace(day=10))),
        RefundSegment("refund_part_11_20", "11-20", str(start.replace(day=11)), str(start.replace(day=20))),
        RefundSegment("refund_part_21_end", "21-月底", str(start.replace(day=21)), str(month_end)),
    ]


def build_refund_search_form(segment: RefundSegment) -> list[tuple[str, str]]:
    return [
        ("platformId", SHOPEE_PLATFORM_ID),
        ("countryCodes[]", "VN"),
        ("refundTimeStart", f"{segment.start_date} 00:00:00"),
        ("refundTimeEnd", f"{segment.end_date} 23:59:59"),
        ("orderStatus[]", "3"),
        ("search-content", "platformOrderId"),
        ("search-content-text", ""),
        ("page", "1"),
        ("rowsPerPage", str(REFUND_PAGE_SIZE)),
    ]


def parse_refund_total(page_html: str) -> int:
    text = BeautifulSoup(str(page_html or ""), "html.parser").get_text(" ", strip=True)
    match = re.search(r"共\s*([\d,]+)\s*条", text)
    if not match:
        if "暂无数据" in text:
            return 0
        raise RuntimeError("无法读取马帮退款列表总条数")
    return int(match.group(1).replace(",", ""))


def parse_refund_csv(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError("马帮退款导出文件不是 UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    columns = [str(item or "").strip() for item in (reader.fieldnames or [])]
    missing = [column for column in REFUND_REQUIRED_COLUMNS if column not in columns]
    if missing:
        raise RuntimeError(f"马帮退款导出缺少关键字段：{', '.join(missing)}")
    rows: list[dict[str, str]] = []
    for source in reader:
        if not isinstance(source, dict):
            continue
        row = {column: str(source.get(column) or "") for column in columns}
        if any(_clean_cell(value) for value in row.values()):
            rows.append(row)
    return columns, rows


def merge_refund_rows(
    segment_rows: list[tuple[RefundSegment, list[dict[str, str]]]],
) -> tuple[list[dict[str, str]], int]:
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    duplicate_count = 0
    for _segment, rows in segment_rows:
        for row in rows:
            key = refund_dedupe_key(row)
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            merged.append(dict(row))
    merged.sort(key=lambda row: (_clean_cell(row.get("退款时间")), _clean_cell(row.get("订单号"))))
    return merged, duplicate_count


def refund_dedupe_key(row: dict[str, Any]) -> str:
    refund_number = _clean_cell(row.get("退款单编号"))
    if refund_number and refund_number not in {"--", "无"}:
        return f"refund:{refund_number}"
    parts = (
        _clean_cell(row.get("订单号")),
        _clean_cell(row.get("退款时间")),
        _clean_cell(row.get("退款金额RMB")),
        _clean_cell(row.get("退款原因")) or _clean_cell(row.get("描述")),
    )
    return "composite:" + "\x1f".join(parts)


def run_vietnam_mabang_refund_collection(
    job: VietnamMabangRefundJob,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(job.manifest_path)
    mabang = _ensure_mabang_section(manifest, job.account_name)
    if _refund_collection_completed(job.manifest_path, mabang):
        merged = mabang["reports"]["refunds_merged"]
        _emit(progress, "马帮退款三段和合并文件已完成，跳过重复导出")
        return _result_payload(job.manifest_path, merged, skipped_count=4)

    segments = refund_segments(str(manifest["start_date"]), str(manifest["end_date"]))
    refund_dir = job.manifest_path.parent / "raw" / "mabang" / "refunds"
    refund_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    manifest["status"] = "mabang_refunds_collecting"
    mabang["status"] = "collecting_refunds"
    _touch_manifest(manifest)
    write_manifest(job.manifest_path, manifest)

    skipped_count = 0
    try:
        with MabangClient(job.base_url, timeout_seconds=300) as client:
            _emit(progress, f"正在使用马帮账号 {job.account_name or job.username} 登录退款列表")
            client.login(job.username, job.password)
            iframe_url = bootstrap_refund_list(client)

            for segment in segments:
                manifest = load_manifest(job.manifest_path)
                mabang = _ensure_mabang_section(manifest, job.account_name)
                report = mabang["reports"][segment.key]
                if report_is_completed(job.manifest_path, report):
                    skipped_count += 1
                    _emit(progress, f"退款 {segment.label} 已导出，跳过")
                    continue
                _set_report_running(manifest, segment.key)
                _touch_manifest(manifest)
                write_manifest(job.manifest_path, manifest)

                _emit(progress, f"正在导出退款 {segment.label}：{segment.start_date} 至 {segment.end_date}")
                raw_bytes, columns, rows, metadata = export_refund_segment(client, iframe_url, segment)
                file_path = refund_dir / (
                    f"{segment.key}__{_compact(segment.start_date)}_{_compact(segment.end_date)}__{stamp}.csv"
                )
                file_path.write_bytes(raw_bytes)
                meta_path = refund_dir / f"{segment.key}__{stamp}.meta.json"
                meta_path.write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                _set_report_result(
                    job.manifest_path,
                    segment.key,
                    status="success" if rows else "no_data",
                    file=_relative(job.manifest_path.parent, file_path),
                    raw_file=_relative(job.manifest_path.parent, file_path),
                    meta_file=_relative(job.manifest_path.parent, meta_path),
                    row_count=len(rows),
                    expected_row_count=metadata["expected_row_count"],
                    columns=columns,
                    start_date=segment.start_date,
                    end_date=segment.end_date,
                )
                _emit(progress, f"退款 {segment.label} 导出完成：{len(rows)} 条")

        segment_data: list[tuple[RefundSegment, list[dict[str, str]]]] = []
        columns: list[str] = []
        for segment in segments:
            report = _ensure_mabang_section(load_manifest(job.manifest_path))["reports"][segment.key]
            file_path = _report_file(job.manifest_path, report)
            if file_path is None:
                segment_data.append((segment, []))
                continue
            current_columns, current_rows = parse_refund_csv(file_path.read_bytes())
            if not columns:
                columns = current_columns
            segment_data.append((segment, current_rows))

        manifest = load_manifest(job.manifest_path)
        _ensure_mabang_section(manifest, job.account_name)
        _set_report_running(manifest, "refunds_merged")
        _touch_manifest(manifest)
        write_manifest(job.manifest_path, manifest)

        merged_rows, duplicate_count = merge_refund_rows(segment_data)
        columns = columns or list(REFUND_EXPORT_COLUMNS)
        merged_path = refund_dir / (
            f"refunds_merged__{_compact(segments[0].start_date)}_{_compact(segments[-1].end_date)}__{stamp}.csv"
        )
        _write_csv(merged_path, columns, merged_rows)
        merge_meta = {
            "generated_at": _now_text(),
            "filters": {
                "platform": "Shopee",
                "platform_id": SHOPEE_PLATFORM_ID,
                "country": "越南",
                "country_code": "VN",
                "order_status": "已发货",
                "order_status_code": "3",
                "date_type": "refundTime",
            },
            "source_row_count": sum(len(rows) for _segment, rows in segment_data),
            "duplicate_count": duplicate_count,
            "merged_row_count": len(merged_rows),
            "dedupe_rule": "退款单编号；缺失时使用订单号+退款时间+退款金额RMB+退款原因/描述",
        }
        merge_meta_path = refund_dir / f"refunds_merge_meta__{stamp}.json"
        merge_meta_path.write_text(json.dumps(merge_meta, ensure_ascii=False, indent=2), encoding="utf-8")
        _set_report_result(
            job.manifest_path,
            "refunds_merged",
            status="success" if merged_rows else "no_data",
            file=_relative(job.manifest_path.parent, merged_path),
            raw_file="",
            meta_file=_relative(job.manifest_path.parent, merge_meta_path),
            row_count=len(merged_rows),
            source_row_count=merge_meta["source_row_count"],
            duplicate_count=duplicate_count,
        )

        manifest = load_manifest(job.manifest_path)
        mabang = _ensure_mabang_section(manifest, job.account_name)
        mabang["status"] = "ready"
        manifest["status"] = "mabang_ready"
        _touch_manifest(manifest)
        write_manifest(job.manifest_path, manifest)
        _emit(progress, f"马帮退款合并完成：{len(merged_rows)} 条，去重 {duplicate_count} 条")
        return _result_payload(
            job.manifest_path,
            mabang["reports"]["refunds_merged"],
            skipped_count=skipped_count,
        )
    except Exception as exc:
        manifest = load_manifest(job.manifest_path)
        mabang = _ensure_mabang_section(manifest, job.account_name)
        for key in (*REFUND_SEGMENT_KEYS, "refunds_merged"):
            report = mabang["reports"][key]
            if report.get("status") == "running":
                report.update(
                    {
                        "status": "failed",
                        "finished_at": _now_text(),
                        "error_code": "collection_failed",
                        "error_message": str(exc),
                    }
                )
        mabang["status"] = "refunds_failed"
        manifest["status"] = "partial_success"
        _touch_manifest(manifest)
        write_manifest(job.manifest_path, manifest)
        raise


def bootstrap_refund_list(client: MabangClient) -> str:
    outer_url = urljoin(f"{client.base_url}/", REFUND_LIST_PATH.lstrip("/"))
    outer = client.client.get(outer_url)
    outer.raise_for_status()
    soup = BeautifulSoup(outer.text, "html.parser")
    iframe = soup.select_one("#iframeContent") or soup.select_one("iframe")
    src = str(iframe.get("src") or "").strip() if iframe else ""
    if not src:
        raise RuntimeError("马帮退款列表页面未找到数据窗口")
    iframe_url = urljoin(str(outer.url), src)
    response = client.client.get(iframe_url)
    response.raise_for_status()
    if "AdvanceSearch" not in response.text and "paypalrefund-form" not in response.text:
        raise RuntimeError("马帮退款列表加载失败或登录已失效")
    return iframe_url


def export_refund_segment(
    client: MabangClient,
    iframe_url: str,
    segment: RefundSegment,
) -> tuple[bytes, list[str], list[dict[str, str]], dict[str, Any]]:
    search_url = urljoin(iframe_url, REFUND_SEARCH_PATH)
    payload = client.post_form_json(
        search_url,
        form_data=build_refund_search_form(segment),
        context=f"马帮退款 {segment.label} 高级搜索",
        referer=iframe_url,
    )
    if not payload.get("success"):
        raise MabangApiError(f"马帮退款 {segment.label} 搜索失败：{payload}")
    expected_count = parse_refund_total(str(payload.get("pageHtml") or payload.get("message") or ""))
    if expected_count > REFUND_DIRECT_EXPORT_LIMIT:
        raise RuntimeError(
            f"退款 {segment.label} 共 {expected_count} 条，超过页面单次导出上限 {REFUND_DIRECT_EXPORT_LIMIT} 条"
        )

    if expected_count == 0:
        raw_bytes = _empty_csv_bytes(list(REFUND_EXPORT_COLUMNS))
        columns, rows = parse_refund_csv(raw_bytes)
        content_type = "text/csv; generated-empty"
    else:
        export_url = urljoin(iframe_url, REFUND_EXPORT_PATH)
        response = client.client.get(export_url, headers={"Referer": iframe_url}, timeout=300)
        response.raise_for_status()
        content_type = str(response.headers.get("Content-Type") or "")
        if "csv" not in content_type.lower() and not response.content.startswith(b"\xef\xbb\xbf"):
            raise RuntimeError(f"马帮退款 {segment.label} 导出未返回 CSV 文件")
        raw_bytes = response.content
        columns, rows = parse_refund_csv(raw_bytes)
        if len(rows) != expected_count:
            raise RuntimeError(
                f"马帮退款 {segment.label} 导出不完整：页面总数 {expected_count}，文件 {len(rows)} 条"
            )

    metadata = {
        "collected_at": _now_text(),
        "source_page": "报表 > 退款报表 > 退款列表 > 高级搜索",
        "export_action": "导入/导出 > 导出搜索的全部记录 > 单行导出",
        "expected_row_count": expected_count,
        "exported_row_count": len(rows),
        "content_type": content_type,
        "filters": {
            "platform": "Shopee",
            "platform_id": SHOPEE_PLATFORM_ID,
            "country": "越南",
            "country_code": "VN",
            "order_status": "已发货",
            "order_status_code": "3",
            "date_type": "refundTime",
            "start": f"{segment.start_date} 00:00:00",
            "end": f"{segment.end_date} 23:59:59",
        },
    }
    return raw_bytes, columns, rows, metadata


def _refund_collection_completed(manifest_path: Path, mabang: dict[str, Any]) -> bool:
    reports = mabang["reports"]
    return all(
        report_is_completed(manifest_path, reports.get(key))
        for key in (*REFUND_SEGMENT_KEYS, "refunds_merged")
    )


def _set_report_result(
    manifest_path: Path,
    report_type: str,
    *,
    status: str,
    **values: Any,
) -> None:
    manifest = load_manifest(manifest_path)
    report = _ensure_mabang_section(manifest)["reports"][report_type]
    report.update(
        {
            "status": status,
            "finished_at": _now_text(),
            "error_code": "",
            "error_message": "",
            **values,
        }
    )
    _touch_manifest(manifest)
    write_manifest(manifest_path, manifest)


def _report_file(manifest_path: Path, report: dict[str, Any]) -> Path | None:
    file_text = str(report.get("file") or "").strip()
    if not file_text:
        return None
    path = Path(file_text)
    if not path.is_absolute():
        path = manifest_path.parent / path
    return path if path.is_file() else None


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _empty_csv_bytes(columns: list[str]) -> bytes:
    stream = io.StringIO(newline="")
    csv.writer(stream).writerow(columns)
    return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")


def _clean_cell(value: Any) -> str:
    return str(value or "").strip().lstrip("\t").strip()


def _relative(run_dir: Path, path: Path) -> str:
    return str(path.relative_to(run_dir))


def _compact(value: str) -> str:
    return str(value or "").replace("-", "")


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _result_payload(
    manifest_path: Path,
    merged_report: dict[str, Any],
    *,
    skipped_count: int,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    reports = _ensure_mabang_section(manifest)["reports"]
    return {
        "manifest_path": str(manifest_path),
        "run_dir": str(manifest_path.parent),
        "segment_row_counts": {
            key: int((reports.get(key) or {}).get("row_count") or 0)
            for key in REFUND_SEGMENT_KEYS
        },
        "merged_row_count": int(merged_report.get("row_count") or 0),
        "duplicate_count": int(merged_report.get("duplicate_count") or 0),
        "skipped_count": skipped_count,
        "failed_count": 0,
    }


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)
