from __future__ import annotations

import copy
import csv
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Iterable

from bs4 import BeautifulSoup
from openpyxl import Workbook

from backend.core.mabang_client import MabangApiError, MabangClient, decode_html_page


BASE_URL = "https://900853.private.mabangerp.com"
REPORT_URL = f"{BASE_URL}/index.php"
REPORT_PARAMS = {"mod": "reports.countryReports"}
DEFAULT_ROWS_PER_PAGE = "2000"
TEXT_COLUMNS = {
    "SKU",
    "商品中文名",
    "商品英文名",
    "NCM",
    "主SKU",
    "状态",
    "创建时间",
    "销售员",
    "开发员",
}
TARGET_SALES_HEADERS = [
    "SKU",
    "商品中文名",
    "商品英文名",
    "item_id",
    "NCM",
    "主SKU",
    "状态",
    "统一成本价",
    "重量(单位:g)",
    "库存",
    "创建时间",
    "销售员",
    "开发员",
    "关联订单数",
    "销售数量",
    "平均单天销量",
    "平均销售价格",
    "收入-订单金额",
    "收入-运费",
    "收入-其他收入",
    "收入-补贴金额",
    "收入-合计",
    "支出-商品成本",
    "支出-运费",
    "支出-包材费",
    "支出-平台费",
    "支出-转帐费",
    "支出-头程费",
    "支出-FBA费用",
    "支出-VAT税费",
    "支出-优惠券",
    "支出-其他费用",
    "支出-其他支出",
    "支出-测评费",
    "支出-合计",
    "退款订单数",
    "退款商品数",
    "退款金额",
    "退款率%",
    "毛利",
    "毛利率%",
]
DEFAULT_GROUPS = (
    {"name": "菲S-雷莹莹团队", "shop_label_id": "1057656"},
    {"name": "菲S-严儒组", "shop_label_id": "1047454"},
    {"name": "菲S-余瑶怡组", "shop_label_id": "1047452"},
    {"name": "菲S-湛玉组", "shop_label_id": "1047451"},
    {"name": "菲S-孔简组（广州）", "shop_label_id": "1047456"},
    {"name": "菲S-徐成黎组", "shop_label_id": "1056889"},
    {"name": "菲S-黄景熙组-广州", "shop_label_id": "1059720"},
    {"name": "菲S-解爽组", "shop_label_id": "1059719"},
)
BASE_FORM_DATA = {
    "historyType": "1",
    "platformOrShop": "country",
    "table": "country",
    "timeStart2": "2021-12-01 00:00:00",
    "timeEnd2": "2021-12-31 23:59:59",
    "timeStart3": "2019-12-01 00:00:00",
    "timeEnd3": "2019-12-31 23:59:59",
    "timeKey": "payTime",
    "searchType": "stockSku",
    "operate": "eq",
    "moneyType": "RMB",
    "page": "1",
    "rowsPerPage": DEFAULT_ROWS_PER_PAGE,
}

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class SalesGroup:
    name: str
    shop_label_id: str

    @property
    def id(self) -> str:
        return self.shop_label_id


@dataclass(frozen=True)
class GroupSalesReportQuery:
    username: str
    password: str
    groups: tuple[SalesGroup, ...]
    start_date: str
    end_date: str
    output_dir: Path


@dataclass(frozen=True)
class GroupSalesReportResult:
    record_count: int
    group_count: int
    csv_file_count: int
    output_file: Path
    output_dir: Path


def safe_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def parse_group_options(html: str) -> tuple[SalesGroup, ...]:
    soup = BeautifulSoup(html, "html.parser")
    controls = soup.select('input[name="shopLabelIds[]"], select[name="shopLabelIds[]"] option')
    groups: list[SalesGroup] = []
    seen: set[str] = set()
    for control in controls:
        label_id = safe_text(control.get("value"))
        label = control if control.name == "option" else control.find_parent("label")
        name = label.get_text(" ", strip=True) if label else ""
        if not label_id or label_id in {"0", "-1"} or not name or label_id in seen:
            continue
        seen.add(label_id)
        groups.append(SalesGroup(name=name, shop_label_id=label_id))
    if not groups:
        raise MabangApiError("未能读取马帮自定义分类，请确认账号有销量报表权限后重新加载")
    return tuple(groups)


def get_group_options(username: str | None = None, password: str | None = None) -> list[dict[str, str]]:
    if username is None and password is None:
        # Keep app-wide startup offline; the report page loads the full account list separately.
        groups = _load_groups_from_mabang_config() or _default_groups()
    else:
        if not username or not password:
            raise ValueError("请先绑定并选择马帮账号")
        with MabangClient(BASE_URL) as client:
            client.login(username, password)
            response = client.client.get(REPORT_URL, params=REPORT_PARAMS)
            response.raise_for_status()
            groups = parse_group_options(decode_html_page(response))
    return [{"id": group.id, "name": group.name, "shop_label_id": group.shop_label_id} for group in groups]


def validate_group_sales_payload(payload: dict[str, Any]) -> GroupSalesReportQuery:
    username = safe_text(payload.get("username"))
    password = safe_text(payload.get("password"))
    start_date = safe_text(payload.get("start_date"))
    end_date = safe_text(payload.get("end_date"))
    output_dir = Path(safe_text(payload.get("output_dir"))).expanduser()

    if not username:
        raise ValueError("请输入马帮账号")
    if not password:
        raise ValueError("请输入马帮密码")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_date):
        raise ValueError("开始日期格式必须是 YYYY-MM-DD")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end_date):
        raise ValueError("结束日期格式必须是 YYYY-MM-DD")
    start_at = datetime.strptime(start_date, "%Y-%m-%d")
    end_at = datetime.strptime(end_date, "%Y-%m-%d")
    if start_at > end_at:
        raise ValueError("开始日期不能晚于结束日期")
    if not output_dir:
        raise ValueError("请选择输出目录")

    selected_ids = _coerce_group_ids(payload.get("group_ids") or payload.get("group_id"))
    if not selected_ids:
        raise ValueError("请选择至少一个自定义分类")
    group_map = {
        item["id"]: SalesGroup(name=item["name"], shop_label_id=item["id"])
        for item in get_group_options(username, password)
    }
    selected_groups = tuple(group_map[group_id] for group_id in selected_ids if group_id in group_map)
    missing_ids = [group_id for group_id in selected_ids if group_id not in group_map]
    if missing_ids:
        raise ValueError(f"自定义分类不存在或当前账号不可见，请重新加载: {', '.join(missing_ids)}")
    if not selected_groups:
        raise ValueError("请选择至少一个自定义分类")

    return GroupSalesReportQuery(
        username=username,
        password=password,
        groups=selected_groups,
        start_date=start_date,
        end_date=end_date,
        output_dir=output_dir,
    )


def run_group_sales_report(
    job: GroupSalesReportQuery,
    progress: ProgressCallback | None = None,
) -> GroupSalesReportResult:
    start_date = datetime.strptime(job.start_date, "%Y-%m-%d").date()
    end_date = datetime.strptime(job.end_date, "%Y-%m-%d").date()
    period_label = f"{start_date:%Y%m%d}_{end_date:%Y%m%d}"
    run_dir = job.output_dir / f"菲律宾各组商品销量报表_{period_label}_{datetime.now():%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)

    _emit(progress, f"输出目录: {run_dir}")
    _emit(progress, "正在登录马帮...")
    with MabangClient(BASE_URL, timeout_seconds=300, verify_ssl=False) as client:
        client.login(job.username, job.password)
        for index, group in enumerate(job.groups, start=1):
            _emit(progress, f"[{index}/{len(job.groups)}] 开始导出 {group.name}")
            export_group_csv(client, run_dir, group, period_label, start_date, end_date, progress)

    _emit(progress, "正在生成汇总工作簿...")
    summary_path = build_summary_workbook(run_dir, job.groups, period_label, start_date, end_date)
    record_count = count_csv_data_rows(run_dir, job.groups, period_label)
    _emit(progress, f"汇总完成: {summary_path}")
    return GroupSalesReportResult(
        record_count=record_count,
        group_count=len(job.groups),
        csv_file_count=len(job.groups),
        output_file=summary_path,
        output_dir=run_dir,
    )


def decode_csv_content(content: bytes) -> str:
    if not content:
        raise MabangApiError("马帮导出返回空文件")
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8-sig", errors="replace")


def validate_sales_csv(content: bytes, context: str) -> None:
    text = decode_csv_content(content[:8192])
    if text.lstrip("\ufeff \r\n\t").startswith("<"):
        raise MabangApiError(f"{context} 导出结果不是 CSV")
    reader = csv.reader(StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise MabangApiError(f"{context} 导出结果没有表头") from exc
    first_cell = (header[0] if header else "").lstrip("\ufeff").strip()
    if first_cell != "SKU":
        raise MabangApiError(f"{context} 导出表头异常: {header[:5]}")


def parse_number(value: str | None) -> float:
    text = str(value or "").strip().rstrip("\t").replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_required_decimal(value: str | None, field_name: str, context: str) -> Decimal:
    text = str(value or "").strip().rstrip("\t").replace(",", "")
    if not text:
        raise MabangApiError(f"{context} {field_name}不是有效数字: {value!r}")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise MabangApiError(f"{context} {field_name}不是有效数字: {value!r}") from exc
    if not parsed.is_finite():
        raise MabangApiError(f"{context} {field_name}不是有效数字: {value!r}")
    return parsed


def format_daily_average(quantity: Decimal, expected_days: int) -> str:
    try:
        value = (quantity / Decimal(expected_days)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    except DecimalException as exc:
        raise MabangApiError(f"销量报表无法计算平均单天销量: 销售数量={quantity}") from exc
    return f"{value:.2f}\t"


def format_decimal(value: float, places: int = 4) -> str:
    if abs(value) < 0.0000001:
        return "0"
    return f"{value:.{places}f}"


def adjusted_header_for_row(header: list[str], row: list[str]) -> list[str]:
    adjusted = list(header)
    if "退款数量" in adjusted and len(row) == len(adjusted) - 1:
        adjusted.remove("退款数量")
    return adjusted


def row_dict_from_csv(header: list[str], row: list[str]) -> dict[str, str]:
    adjusted = adjusted_header_for_row(header, row)
    return {name: row[index] if index < len(row) else "" for index, name in enumerate(adjusted)}


def normalize_status(value: str) -> str:
    text = str(value or "").strip()
    return "正常" if text == "正常销售" else text


def normalize_sales_csv(content: bytes, expected_days: int) -> bytes:
    if expected_days <= 0:
        raise ValueError("统计天数必须大于 0")

    text = decode_csv_content(content)
    reader = csv.reader(StringIO(text))
    try:
        header = [cell.lstrip("\ufeff") if index == 0 else cell for index, cell in enumerate(next(reader))]
    except StopIteration as exc:
        raise MabangApiError("马帮导出 CSV 没有表头") from exc
    missing_headers = [column for column in ("SKU", "销售数量") if column not in header]
    if missing_headers:
        raise MabangApiError(f"马帮导出 CSV 缺少必需列: {', '.join(missing_headers)}")

    output = StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(TARGET_SALES_HEADERS)
    for row in reader:
        if not row or not any(str(cell or "").strip() for cell in row):
            continue
        values = row_dict_from_csv(header, row)
        row_context = f"SKU={(values.get('SKU') or '-').strip()}"
        quantity_decimal = parse_required_decimal(values.get("销售数量"), "销售数量", row_context)
        quantity = float(quantity_decimal)
        order_income = parse_number(values.get("收入-订单金额"))
        daily_average_value = format_daily_average(quantity_decimal, expected_days)
        average_price_value = values.get("平均销售价格") or (
            f"{format_decimal(order_income / quantity)}\t" if quantity else "0\t"
        )
        normalized = []
        for column in TARGET_SALES_HEADERS:
            if column == "平均单天销量":
                normalized.append(daily_average_value)
            elif column == "平均销售价格":
                normalized.append(average_price_value)
            elif column == "状态":
                normalized.append(normalize_status(values.get(column, "")))
            else:
                normalized.append(values.get(column, ""))
        writer.writerow(normalized)
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def build_form_data(label_id: str, start_date, end_date) -> dict[str, Any]:
    form_data = copy.deepcopy(BASE_FORM_DATA)
    form_data.update(
        {
            "historyType": "1",
            "platformOrShop": "country",
            "table": "country",
            "timeStart": f"{start_date:%Y-%m-%d} 00:00:00",
            "timeEnd": f"{end_date:%Y-%m-%d} 23:59:59",
            "timeKey": "payTime",
            "searchType": "stockSku",
            "operate": "eq",
            "moneyType": "RMB",
            "page": "1",
            "rowsPerPage": DEFAULT_ROWS_PER_PAGE,
            "shopLabelIds[]": str(label_id),
        }
    )
    return form_data


def export_group_csv(
    client: MabangClient,
    output_dir: Path,
    group: SalesGroup,
    period_label: str,
    start_date,
    end_date,
    progress: ProgressCallback | None = None,
) -> Path:
    output_path = output_dir / f"{safe_filename(group.name)}_{period_label}.csv"
    context = f"{group.name}_{period_label}"
    form_data = build_form_data(group.shop_label_id, start_date, end_date)
    export_file = client.download_product_sales_report_csv(
        url=REPORT_URL,
        params=REPORT_PARAMS,
        form_data=form_data,
        report_type="countryReports",
        platform_or_shop="country",
    )
    content = export_file.content
    validate_sales_csv(content, context)

    expected_days = (end_date - start_date).days + 1
    content = normalize_sales_csv(content, expected_days)
    validate_sales_csv(content, f"{context}_normalized")
    output_path.write_bytes(content)
    _emit(progress, f"{group.name} CSV 已生成: {output_path.name}")
    return output_path


def csv_text_for_workbook(path: Path) -> str:
    content = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            pass
    return content.decode("utf-8-sig", errors="replace")


def convert_cell(value: str, column_name: str):
    if value == "":
        return None
    if column_name in TEXT_COLUMNS:
        return value
    normalized = value.replace(",", "")
    if re.fullmatch(r"-?\d+", normalized):
        try:
            return int(normalized)
        except ValueError:
            return value
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", normalized):
        try:
            return float(normalized)
        except ValueError:
            return value
    return value


def safe_sheet_name(name: str, used_names: set[str]) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", "_", name).strip() or "Sheet"
    base = cleaned[:31]
    candidate = base
    index = 1
    while candidate in used_names:
        suffix = f"_{index}"
        candidate = f"{base[:31 - len(suffix)]}{suffix}"
        index += 1
    used_names.add(candidate)
    return candidate


def append_csv_sheet(workbook: Workbook, path: Path, sheet_name: str, used_names: set[str]) -> None:
    worksheet = workbook.create_sheet(safe_sheet_name(sheet_name, used_names))
    text = csv_text_for_workbook(path)
    reader = csv.reader(StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return
    header = [cell.lstrip("\ufeff") if index == 0 else cell for index, cell in enumerate(header)]
    worksheet.append(header)
    for row in reader:
        values = [convert_cell(cell, header[index] if index < len(header) else "") for index, cell in enumerate(row)]
        worksheet.append(values)


def build_summary_workbook(
    output_dir: Path,
    groups: Iterable[SalesGroup],
    period_label: str,
    start_date,
    end_date,
) -> Path:
    summary_path = output_dir / f"菲律宾各组销量汇总_{start_date:%Y%m%d}_{end_date:%Y%m%d}.xlsx"
    workbook = Workbook(write_only=False)
    default_sheet = workbook.active
    workbook.remove(default_sheet)
    used_names: set[str] = set()

    for group in groups:
        csv_path = output_dir / f"{safe_filename(group.name)}_{period_label}.csv"
        if not csv_path.exists():
            raise FileNotFoundError(f"汇总缺少 CSV 文件: {csv_path}")
        append_csv_sheet(workbook, csv_path, group.name, used_names)

    workbook.save(summary_path)
    workbook.close()
    return summary_path


def count_csv_data_rows(output_dir: Path, groups: Iterable[SalesGroup], period_label: str) -> int:
    total = 0
    for group in groups:
        csv_path = output_dir / f"{safe_filename(group.name)}_{period_label}.csv"
        text = csv_text_for_workbook(csv_path)
        reader = csv.reader(StringIO(text))
        try:
            next(reader)
        except StopIteration:
            continue
        for row in reader:
            sku = (row[0] if row else "").lstrip("\ufeff").strip()
            if sku and sku != "合计":
                total += 1
    return total


def safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\r\n\t]+', "_", value).strip(" .")
    return cleaned or "未命名"


def _default_groups() -> tuple[SalesGroup, ...]:
    return tuple(SalesGroup(name=item["name"], shop_label_id=item["shop_label_id"]) for item in DEFAULT_GROUPS)


def _load_groups_from_mabang_config() -> tuple[SalesGroup, ...]:
    config_path = (
        Path(__file__).resolve().parents[3]
        / "mabang_process"
        / "philippines"
        / "config"
        / "各组商品销量报表.yaml"
    )
    if not config_path.exists():
        return tuple()
    try:
        import yaml
    except ImportError:
        return tuple()
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return tuple()
    groups = ((payload.get("process_config") or {}).get("groups")) or []
    result: list[SalesGroup] = []
    seen: set[str] = set()
    for item in groups:
        if not isinstance(item, dict):
            continue
        name = safe_text(item.get("name"))
        label_id = safe_text(item.get("shop_label_id"))
        if not name or not label_id or label_id in seen:
            continue
        seen.add(label_id)
        result.append(SalesGroup(name=name, shop_label_id=label_id))
    return tuple(result)


def _coerce_group_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
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
    return result


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress:
        progress(message)
