"""马帮「商品仓库报表」按月份导出：多选自定义分类 + 多选仓库，逐月一个 sheet。

数据源是 ``reports.warehouseReports``（商品仓库报表）。它与「国家报表」
（``reports.countryReports``）的区别是：页面上同时提供店铺自定义分类
（``shopLabelIds[]``）和仓库（``stockWarehouseId[]``）两组筛选控件，
同维度多选取并集、不同维度之间取交集。

导出走 ``reports.exportProductAll``（``types=warehouseReports``）。马帮在大数据量
时会**静默截断到一页 2000 行**，而且截断文件仍然带「合计」行，用合计校验不出来，
所以每导一个月都要用报表页的「共 N 条」复核行数。
"""

from __future__ import annotations

import copy
import csv
import io
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

import xlsxwriter
from bs4 import BeautifulSoup

from backend.core.mabang_client import MabangApiError, MabangClient, decode_html_page
from backend.services.group_sales_report import (
    BASE_FORM_DATA,
    TARGET_SALES_HEADERS,
    convert_cell,
    csv_text_for_workbook,
    normalize_sales_csv,
    safe_sheet_name,
    safe_text,
    validate_sales_csv,
)


ProgressCallback = Callable[[str], None]

BASE_URL = "https://900853.private.mabangerp.com"
REPORT_URL = f"{BASE_URL}/index.php"
REPORT_PARAMS: dict[str, str] = {"mod": "reports.warehouseReports"}
REPORT_REFERER = "/index.php?mod=reports.warehouseReports&salesSwitch=&supplierId="
REPORT_TYPE = "warehouseReports"
PLATFORM_OR_SHOP = "warehouse"
CATEGORY_FIELD = "shopLabelIds[]"
WAREHOUSE_FIELD = "stockWarehouseId[]"
DEFAULT_EXPORT_ROWS_PER_PAGE = "2000"
PAGE_SIZE_HINT = 2000
MAX_EXPORT_ATTEMPTS = 4
RETRY_DELAYS = (10, 30, 60)
TOTAL_PATTERN = re.compile(r"共\s*([\d,]+)\s*条")
MONTH_PATTERN = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
TEXT_FORCE_COLUMNS = {"item_id"}
SHEET_NAME_NOTE = "导出说明"


@dataclass(frozen=True)
class FilterOption:
    id: str
    name: str


@dataclass(frozen=True)
class WarehouseSalesExportJob:
    username: str
    password: str
    category_ids: tuple[str, ...]
    warehouse_ids: tuple[str, ...]
    start_month: str
    end_month: str
    output_dir: Path

    @property
    def months(self) -> tuple[tuple[int, int], ...]:
        return month_sequence(self.start_month, self.end_month)


@dataclass(frozen=True)
class MonthExport:
    label: str
    record_count: int
    csv_path: Path


@dataclass
class WarehouseSalesExportResult:
    month_count: int
    record_count: int
    output_file: Path
    output_dir: Path
    months: tuple[MonthExport, ...]
    failed_months: tuple[str, ...] = ()
    category_names: tuple[str, ...] = ()
    warehouse_names: tuple[str, ...] = ()
    csv_files: tuple[Path, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- 选项


def _collect_options(soup: BeautifulSoup, field_name: str) -> tuple[FilterOption, ...]:
    options: list[FilterOption] = []
    seen: set[str] = set()
    selector = f'input[name="{field_name}"], select[name="{field_name}"] option'
    for tag in soup.select(selector):
        value = safe_text(tag.get("value"))
        if not value or value in {"0", "-1"} or value in seen:
            continue
        container = tag if tag.name == "option" else tag.find_parent("label")
        name = container.get_text(" ", strip=True) if container is not None else ""
        name = re.sub(r"\s+", " ", name).strip() or value
        seen.add(value)
        options.append(FilterOption(id=value, name=name))
    return tuple(options)


def parse_report_options(html: str) -> tuple[tuple[FilterOption, ...], tuple[FilterOption, ...]]:
    soup = BeautifulSoup(html, "html.parser")
    categories = _collect_options(soup, CATEGORY_FIELD)
    warehouses = _collect_options(soup, WAREHOUSE_FIELD)
    if not categories and not warehouses:
        raise MabangApiError("未能读取商品仓库报表的分类与仓库，请确认账号有报表权限后重试")
    return categories, warehouses


def fetch_report_options(username: str, password: str) -> tuple[tuple[FilterOption, ...], tuple[FilterOption, ...]]:
    if not username or not password:
        raise ValueError("请先绑定并选择马帮账号")
    with MabangClient(BASE_URL, timeout_seconds=180, verify_ssl=False) as client:
        client.login(username, password)
        return _fetch_report_options(client)


def _fetch_report_options(client: MabangClient) -> tuple[tuple[FilterOption, ...], tuple[FilterOption, ...]]:
    response = client.client.get(
        f"{BASE_URL}{REPORT_REFERER}",
        headers={"Referer": f"{BASE_URL}/index.php"},
    )
    response.raise_for_status()
    return parse_report_options(decode_html_page(response))


def get_report_option_payload(username: str | None = None, password: str | None = None) -> dict[str, Any]:
    """供启动页离线加载；未给账号时返回空清单，不联网。"""
    if username or password:
        categories, warehouses = fetch_report_options(username or "", password or "")
        category_payload = [{"id": item.id, "name": item.name} for item in categories]
        warehouse_payload = [{"id": item.id, "name": item.name} for item in warehouses]
    else:
        category_payload, warehouse_payload = [], []
    return {
        "categories": category_payload,
        "warehouses": warehouse_payload,
        "month_range": default_month_range(),
    }


# --------------------------------------------------------------------------- 月份


def parse_month(value: str, label: str) -> tuple[int, int]:
    text = safe_text(value)
    match = MONTH_PATTERN.match(text)
    if not match:
        raise ValueError(f"{label}格式必须是 YYYY-MM")
    return int(match.group(1)), int(match.group(2))


def month_sequence(start_month: str, end_month: str) -> tuple[tuple[int, int], ...]:
    start_year, start_value = parse_month(start_month, "开始月份")
    end_year, end_value = parse_month(end_month, "结束月份")
    cursor = start_year * 12 + (start_value - 1)
    last = end_year * 12 + (end_value - 1)
    if cursor > last:
        raise ValueError("开始月份不能晚于结束月份")
    months: list[tuple[int, int]] = []
    while cursor <= last:
        months.append((cursor // 12, cursor % 12 + 1))
        cursor += 1
    return tuple(months)


def month_bounds(year: int, month: int) -> tuple[date, date, str]:
    start = date(year, month, 1)
    next_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    end = date.fromordinal(next_first.toordinal() - 1)
    return start, end, f"{year:04d}{month:02d}"


def month_label(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def default_month_range(today: date | None = None, span: int = 6) -> dict[str, str]:
    """默认最近 6 个自然月（含当月）。"""
    current = today or date.today()
    end_index = current.year * 12 + (current.month - 1)
    start_index = end_index - (max(1, span) - 1)
    return {
        "start_month": f"{start_index // 12:04d}-{start_index % 12 + 1:02d}",
        "end_month": f"{end_index // 12:04d}-{end_index % 12 + 1:02d}",
    }


# --------------------------------------------------------------------------- 入参


def _coerce_ids(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Sequence[Any] = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = safe_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


def validate_export_payload(payload: dict[str, Any]) -> WarehouseSalesExportJob:
    username = safe_text(payload.get("username"))
    password = safe_text(payload.get("password"))
    category_ids = _coerce_ids(payload.get("category_ids"))
    warehouse_ids = _coerce_ids(payload.get("warehouse_ids"))
    start_month = safe_text(payload.get("start_month"))
    end_month = safe_text(payload.get("end_month"))
    output_dir = Path(safe_text(payload.get("output_dir"))).expanduser()

    if not username:
        raise ValueError("请输入马帮账号")
    if not password:
        raise ValueError("请输入马帮密码")
    if not category_ids and not warehouse_ids:
        raise ValueError("请至少选择一个自定义分类或一个仓库")
    months = month_sequence(start_month, end_month)
    if len(months) > 60:
        raise ValueError("一次最多导出 60 个月，请缩小月份区间")
    if not safe_text(payload.get("output_dir")):
        raise ValueError("请选择输出目录")

    return WarehouseSalesExportJob(
        username=username,
        password=password,
        category_ids=category_ids,
        warehouse_ids=warehouse_ids,
        start_month=start_month,
        end_month=end_month,
        output_dir=output_dir,
    )


# --------------------------------------------------------------------------- 请求


def build_report_form(
    category_ids: Sequence[str],
    warehouse_ids: Sequence[str],
    start: date,
    end: date,
    *,
    page: int = 1,
    rows_per_page: int = 100,
) -> list[tuple[str, str]]:
    form = copy.deepcopy(BASE_FORM_DATA)
    form.update(
        {
            "platformOrShop": PLATFORM_OR_SHOP,
            "table": "warehouse",
            "timeStart": f"{start:%Y-%m-%d} 00:00:00",
            "timeEnd": f"{end:%Y-%m-%d} 23:59:59",
            "timeKey": "payTime",
            "moneyType": "RMB",
            "page": str(max(1, page)),
            "rowsPerPage": str(max(1, rows_per_page)),
        }
    )
    form.pop(CATEGORY_FIELD, None)
    form.pop(WAREHOUSE_FIELD, None)
    pairs: list[tuple[str, str]] = [(key, str(value)) for key, value in form.items()]
    pairs.extend((CATEGORY_FIELD, category_id) for category_id in category_ids)
    pairs.extend((WAREHOUSE_FIELD, warehouse_id) for warehouse_id in warehouse_ids)
    return pairs


def query_report_total(client: MabangClient, form: list[tuple[str, str]], context: str) -> int | None:
    payload = client.post_form_json(
        "/index.php",
        params=REPORT_PARAMS,
        form_data=form,
        context=f"{context} 总数复核",
        referer=REPORT_REFERER,
    )
    if payload.get("success") is not True:
        return None
    text = BeautifulSoup(str(payload.get("pageHtml") or ""), "html.parser").get_text(" ", strip=True)
    match = TOTAL_PATTERN.search(text)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def count_detail_rows(content: bytes) -> int:
    text = content.decode("utf-8-sig", errors="replace") if content else ""
    reader = csv.reader(io.StringIO(text))
    try:
        next(reader)
    except StopIteration:
        return 0
    total = 0
    for row in reader:
        sku = (row[0] if row else "").lstrip("\ufeff").strip()
        if sku and sku != "合计":
            total += 1
    return total


# --------------------------------------------------------------------------- 导出


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)


def export_month(
    client: MabangClient,
    run_dir: Path,
    job: WarehouseSalesExportJob,
    year: int,
    month: int,
    progress: ProgressCallback | None = None,
) -> MonthExport:
    start, end, stamp = month_bounds(year, month)
    label = month_label(year, month)
    csv_path = run_dir / f"{stamp}.csv"
    expected_days = (end - start).days + 1
    context = f"{label} 商品仓库报表"
    last_error = ""

    for attempt in range(1, MAX_EXPORT_ATTEMPTS + 1):
        try:
            form = build_report_form(
                job.category_ids,
                job.warehouse_ids,
                start,
                end,
                page=1,
                rows_per_page=int(DEFAULT_EXPORT_ROWS_PER_PAGE),
            )
            export_file = client.download_product_sales_report_csv(
                url=REPORT_URL,
                params=REPORT_PARAMS,
                form_data=form,
                report_type=REPORT_TYPE,
                platform_or_shop=PLATFORM_OR_SHOP,
                referer=f"{BASE_URL}{REPORT_REFERER}",
            )
            content = export_file.content
            if not content:
                raise MabangApiError(f"{context} 导出结果为空")
            validate_sales_csv(content, context)
            rows = count_detail_rows(content)
            if rows <= 0:
                raise MabangApiError(f"{context} 导出结果没有明细行")

            # 马帮按 2000 条一页拼装导出，截断必然落在整页边界上。
            if rows % PAGE_SIZE_HINT == 0:
                total = query_report_total(
                    client,
                    build_report_form(job.category_ids, job.warehouse_ids, start, end, page=1, rows_per_page=100),
                    context,
                )
                if total is None:
                    raise MabangApiError(f"{context} 无法读取报表总数，不能确认导出完整性")
                if total != rows:
                    raise MabangApiError(f"{context} 导出被截断：文件 {rows} 行，报表共 {total} 条")

            try:
                content = normalize_sales_csv(content, expected_days)
            except MabangApiError as exc:
                _emit(progress, f"{label}: 标准列整理跳过（{exc}），按原始列导出")
            validate_sales_csv(content, f"{context} 整理后")
            csv_path.write_bytes(content)
            _emit(progress, f"{label}: 已导出 {rows} 行")
            return MonthExport(label=label, record_count=rows, csv_path=csv_path)
        except Exception as exc:  # noqa: BLE001 - 逐月重试，最后汇总失败
            last_error = f"{type(exc).__name__}: {exc}"
            csv_path.unlink(missing_ok=True)
            if attempt < MAX_EXPORT_ATTEMPTS:
                delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                _emit(progress, f"{label}: 第 {attempt} 次导出失败（{last_error}），{delay}s 后重试")
                time.sleep(delay)

    raise MabangApiError(f"{label} 连续 {MAX_EXPORT_ATTEMPTS} 次导出失败：{last_error}")


def run_warehouse_sales_export(
    job: WarehouseSalesExportJob,
    progress: ProgressCallback | None = None,
) -> WarehouseSalesExportResult:
    months = job.months
    period = f"{job.start_month.replace('-', '')}_{job.end_month.replace('-', '')}"
    run_dir = job.output_dir / f"商品仓库报表_{period}_{datetime.now():%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    _emit(progress, f"输出目录: {run_dir}")
    _emit(progress, f"月份区间: {job.start_month} ~ {job.end_month}，共 {len(months)} 个月")

    with MabangClient(BASE_URL, timeout_seconds=900, verify_ssl=False) as client:
        _emit(progress, "正在登录马帮…")
        client.login(job.username, job.password)
        try:
            categories, warehouses = _fetch_report_options(client)
        except Exception as exc:  # noqa: BLE001 - 清单只用于展示名称，失败不阻塞导出
            _emit(progress, f"读取分类/仓库名称失败（{exc}），继续导出")
            categories, warehouses = (), ()

        category_names = _resolve_names(job.category_ids, categories)
        warehouse_names = _resolve_names(job.warehouse_ids, warehouses)
        _emit(progress, f"分类 {len(job.category_ids)} 个：{_preview(category_names)}")
        _emit(progress, f"仓库 {len(job.warehouse_ids)} 个：{_preview(warehouse_names)}")

        exported: list[MonthExport] = []
        failed: list[str] = []
        for index, (year, month) in enumerate(months, start=1):
            label = month_label(year, month)
            _emit(progress, f"[{index}/{len(months)}] 开始导出 {label}")
            try:
                exported.append(export_month(client, run_dir, job, year, month, progress))
            except Exception as exc:  # noqa: BLE001 - 单月失败不中断其余月份
                failed.append(label)
                _emit(progress, f"{label}: 导出失败，已跳过（{exc}）")

    if not exported:
        raise MabangApiError("全部月份都导出失败，请检查账号权限或稍后重试")

    output_file = run_dir / f"商品仓库报表_{period}.xlsx"
    _emit(progress, "正在合并工作簿…")
    total_rows = build_workbook(
        run_dir=run_dir,
        output_path=output_file,
        job=job,
        months=exported,
        category_names=category_names,
        warehouse_names=warehouse_names,
        failed_months=tuple(failed),
        progress=progress,
    )
    _emit(progress, f"工作簿已生成: {output_file}（共 {total_rows} 行）")
    return WarehouseSalesExportResult(
        month_count=len(exported),
        record_count=total_rows,
        output_file=output_file,
        output_dir=run_dir,
        months=tuple(exported),
        failed_months=tuple(failed),
        category_names=category_names,
        warehouse_names=warehouse_names,
        csv_files=tuple(item.csv_path for item in exported),
    )


def _resolve_names(ids: Sequence[str], options: Sequence[FilterOption]) -> tuple[str, ...]:
    mapping = {item.id: item.name for item in options}
    return tuple(mapping.get(item_id, item_id) for item_id in ids)


def _preview(names: Sequence[str], limit: int = 4) -> str:
    if not names:
        return "（未选择）"
    head = "、".join(names[:limit])
    return head if len(names) <= limit else f"{head} 等 {len(names)} 个"


# --------------------------------------------------------------------------- 工作簿


def _clean_cell(raw: str, column: str):
    text = str(raw or "").replace("\t", "").strip()
    if not text:
        return None
    if column in TEXT_FORCE_COLUMNS:
        return text
    return convert_cell(text, column)


def _write_note_sheet(
    worksheet,
    header_format,
    job: WarehouseSalesExportJob,
    months: Sequence[MonthExport],
    category_names: Sequence[str],
    warehouse_names: Sequence[str],
    failed: Sequence[str],
) -> None:
    rows: list[tuple[str, str]] = [
        ("账号", job.username),
        ("数据源", "马帮 ERP · 商品仓库报表（reports.warehouseReports）"),
        ("时间口径", "付款时间 payTime，每月 1 日 00:00:00 ~ 月末 23:59:59"),
        ("金额币种", "RMB"),
        ("月份区间", f"{job.start_month} ~ {job.end_month}"),
        ("成功月份", f"{len(months)} 个"),
        ("自定义分类", f"{len(job.category_ids)} 个：{'、'.join(category_names) if category_names else '（未选择）'}"),
        ("仓库", f"{len(job.warehouse_ids)} 个：{'、'.join(warehouse_names) if warehouse_names else '（未选择）'}"),
        ("多选口径", "同一维度多选取并集，分类与仓库之间取交集"),
        ("导出时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]
    if failed:
        rows.append(("失败月份", "、".join(failed)))
    rows.append(("每月行数", " / ".join(f"{item.label}={item.record_count}" for item in months)))
    rows.append(("合计行数", str(sum(item.record_count for item in months))))

    worksheet.write_row(0, 0, ("项目", "内容"), header_format)
    for index, (key, value) in enumerate(rows, start=1):
        worksheet.write(index, 0, key)
        worksheet.write(index, 1, value)
    worksheet.set_column(0, 0, 14)
    worksheet.set_column(1, 1, 120)


def build_workbook(
    *,
    run_dir: Path,
    output_path: Path,
    job: WarehouseSalesExportJob,
    months: Sequence[MonthExport],
    category_names: Sequence[str],
    warehouse_names: Sequence[str],
    failed_months: Sequence[str] = (),
    progress: ProgressCallback | None = None,
) -> int:
    workbook = xlsxwriter.Workbook(
        str(output_path),
        {"constant_memory": True, "strings_to_numbers": False, "strings_to_formulas": False},
    )
    header_format = workbook.add_format({"bold": True, "bg_color": "#F2F2F2", "border": 1})
    used_names: set[str] = set()

    note_sheet = workbook.add_worksheet(safe_sheet_name(SHEET_NAME_NOTE, used_names))
    _write_note_sheet(
        note_sheet,
        header_format,
        job,
        months,
        category_names,
        warehouse_names,
        failed_months,
    )

    total_rows = 0
    for item in months:
        worksheet = workbook.add_worksheet(safe_sheet_name(item.label, used_names))
        worksheet.write_row(0, 0, list(TARGET_SALES_HEADERS), header_format)
        text = csv_text_for_workbook(item.csv_path)
        reader = csv.reader(io.StringIO(text))
        try:
            csv_header = next(reader)
        except StopIteration:
            continue
        csv_header = [
            cell.lstrip("\ufeff") if index == 0 else cell for index, cell in enumerate(csv_header)
        ]
        index_map = {name: position for position, name in enumerate(csv_header)}
        written = 0
        for row in reader:
            sku = (row[0] if row else "").lstrip("\ufeff").strip()
            if not sku or sku == "合计":
                continue
            values = []
            for column in TARGET_SALES_HEADERS:
                position = index_map.get(column)
                raw = row[position] if position is not None and position < len(row) else ""
                values.append(_clean_cell(raw, column))
            worksheet.write_row(written + 1, 0, values)
            written += 1
        total_rows += written
        _emit(progress, f"{item.label}: 写入 {written} 行")

    workbook.close()
    return total_rows
