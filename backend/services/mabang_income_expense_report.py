from __future__ import annotations

import hashlib
import os
import random
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO
from urllib.parse import urlencode

import httpx
import pandas as pd
from bs4 import BeautifulSoup

from backend.core.mabang_client import MabangApiError, MabangClient


ProgressCallback = Callable[[str], None]

MABANG_BASE_URL = "https://900853.private.mabangerp.com"
INCOME_DETAIL_PATH = "/index.php"
INCOME_DETAIL_PARAMS = {"mod": "reports.getIncomePayParticularsData"}
INCOME_REPORT_REFERER = "/index.php?mod=reports.incomePayParticulars"
INCOME_REPORT_PARAMS = {"mod": "reports.incomePayParticulars"}
INCOME_EXPORT_MOD = "reports.exportParticularsAll"
DEFAULT_PAGE_SIZE = 2000
DEFAULT_CONCURRENCY = 4
MAX_PAGES = 2000
EXPORT_INIT_ROWS_PER_PAGE = 100
EXPORT_REQUEST_TIMEOUT_SECONDS = 360
MAX_EXPORT_CHUNKS = 10_000
MAX_EXPORT_ATTEMPTS = 4
WAF_RETRY_DELAYS = (60, 120, 300)
TRANSIENT_RETRY_DELAYS = (5, 15, 30)

MALAYSIA_WAREHOUSE_KEY = "1096191"
MALAYSIA_SHOP_LABEL_GROUPS = (
    ("Lazada 马来", "1038956"),
    ("Shopee 马来", "1038957"),
    ("TikTok 马来", "1042623"),
)
MALAYSIA_TEMU_PLATFORM_IDS = frozenset({"154", "162"})
MALAYSIA_COVERAGE_BATCH_SIZE = 850

WAF_MARKERS = (
    "api.waf.qq.com/waf-attack-feedback",
    "submitWafFeedback",
    "block-pages/src/main",
    "waf拦截页面",
)

# The official export endpoint always includes the order number, transaction
# number, settlement status, shop, site, country, gross profit and gross margin.
# These fields request the remaining columns supported by the endpoint.
INCOME_EXPORT_FIELDS = (
    "skuName",
    "platformSkuInfo",
    "paidTime",
    "expressTime",
    "orderWeight",
    "warehouseName",
    "logisticsName",
    "logisticsChannel",
    "trackNumber",
    "trackNumber1",
    "platform",
    "currency",
    "itemTotalOrigin",
    "rate",
    "remark",
    "itemTotal",
    "shippingTotal",
    "otherIncome",
    "refundCost",
    "freightReturnFee",
    "subsidyAmount",
    "incomeTotal",
    "itemTotalCost",
    "shippingCostNew",
    "paypalFee",
    "platformFee",
    "allianceCommission",
    "packageFee",
    "headShipping",
    "FBAMoney",
    "voucherPrice",
    "vatTaxation",
    "otherMoney",
    "otherExpend",
    "evaluationFee",
    "expendTotal",
    "refundFee",
    "quantityTotal",
    "refundQuantity",
    "recruitmentQuantity",
    "itemCount",
)


@dataclass(frozen=True)
class IncomeExpenseFilterTarget:
    key: str
    name: str
    filter_field: str
    filter_values: tuple[str, ...]
    mabang_names: tuple[str, ...]
    kind: str
    note: str = ""

    @property
    def id(self) -> str:
        return self.key

    @property
    def mabang_name(self) -> str:
        return "、".join(self.mabang_names)


def _category(name: str, category_id: str) -> IncomeExpenseFilterTarget:
    return IncomeExpenseFilterTarget(
        key=category_id,
        name=name,
        filter_field="shopLabelIds[]",
        filter_values=(category_id,),
        mabang_names=(name,),
        kind="category",
    )


def _warehouse(
    key: str,
    name: str,
    warehouse_ids: tuple[str, ...],
    mabang_names: tuple[str, ...],
    *,
    note: str = "",
) -> IncomeExpenseFilterTarget:
    return IncomeExpenseFilterTarget(
        key=key,
        name=name,
        filter_field="stockWarehouseId[]",
        filter_values=warehouse_ids,
        mabang_names=mabang_names,
        kind="warehouse",
        note=note,
    )


# These IDs were migrated from the 21 legacy income-report form snapshots.
# Runtime requests are generated locally and never read those snapshots or
# their former network-share directory.
REPORT_CATEGORIES = (
    _category("lazada菲律宾", "1038955"),
    _category("lazada马来", "1038956"),
    _category("lazada泰国", "1038954"),
    _category("lazada印尼", "1041684"),
    _category("lazada越南", "1054139"),
    _category("shopee菲律宾", "1038952"),
    _category("shopee马来", "1038957"),
    _category("shopee泰国", "1038953"),
    _category("shopee印尼", "1039175"),
    _category("shopee越南", "1054138"),
    _category("tk菲律宾", "1042622"),
    _category("tk马来", "1042623"),
    _category("tk泰国", "1042621"),
    _category("tk印尼", "1042624"),
    _category("tk越南", "1045562"),
    _category("tk巴西", "1059666"),
    _category("MercadoLivre巴西", "1059517"),
    _category("shopee巴西", "1059368"),
    _category("菲TEMU-袁晶组", "1058948"),
    _category("马TEMU-裴龙清组", "1058376"),
    _category("泰TEMU-郭乐组", "1058457"),
)


OVERSEAS_WAREHOUSES = (
    _warehouse("1091307", "实速通", ("1091307",), ("莫斯科五号仓-实速通",)),
    _warehouse("1089489", "北俄", ("1089489",), ("北俄海外仓-俄罗斯",)),
    _warehouse(
        "1089481",
        "极光速达",
        ("1089481",),
        ("MSK01-极光速达海外仓",),
        note="7月开始不合作，保留历史查询",
    ),
    _warehouse(
        "1090061",
        "艾姆勒",
        ("1090061",),
        ("RUS2-艾姆勒（新）",),
        note="8月开始不合作，保留历史查询",
    ),
    _warehouse("1097986", "福仓", ("1097986",), ("美洲7仓-JOP海外仓",)),
    _warehouse("1098323", "吉风", ("1098323",), ("南宁仓-吉风边境仓",)),
    _warehouse("1078593", "皓程", ("1078593",), ("俄罗斯owms仓",)),
    _warehouse("1093204", "KEC", ("1093204",), ("KEC越南胡志明仓-kerry",)),
    _warehouse("1081421", "知意-胡志明", ("1081421",), ("越南胡志明仓库-知意",)),
    _warehouse("1091491", "知意-河内", ("1091491",), ("寰球河内2仓-知意",)),
    _warehouse("1096221", "元仓", ("1096221",), ("YCMY-TENGFEI-元仓海外仓（新）",)),
    _warehouse("1100204", "印尼雅仓海外仓", ("1100204",), ("ID8803-雅仓海外仓",)),
    _warehouse("1100500", "菲律宾雅仓海外仓", ("1100500",), ("PH8807-雅仓海外仓",)),
    _warehouse("1096191", "马来雅仓海外仓", ("1096191",), ("MY8805-雅仓海外仓",)),
    _warehouse(
        "1097191",
        "AllSome",
        ("1097191",),
        ("AllSome Malaysia - Selangor, Shah Alam-AllSome海外仓",),
    ),
    _warehouse(
        "jiasente",
        "嘉森特",
        ("1078593", "1099710"),
        ("俄罗斯owms仓", "哈萨克斯坦仓"),
        note="组合导出俄罗斯OWMS仓和哈萨克斯坦仓",
    ),
    _warehouse(
        "1100602",
        "腾海",
        ("1100602",),
        ("T&C 海外 2 号仓-T&C海外仓",),
        note="当前暂无数据",
    ),
)


FIELD_MAPPING = {
    "gross": "毛利",
    "grossRate": "毛利率",
    "platformOrder": "订单编号",
    "salesRecordNumber": "交易号",
    "isSettlement": "结算状态",
    "shopName": "店铺名称",
    "orderSite": "站点",
    "countryCN": "国家",
    "choiceFlag": "订单业务模式",
    "shippingCostNew": "预估运费",
    "shippingCost": "实际运费",
    "trackNumber": "货运单号",
    "logisticsName": "物流公司",
    "logisticsChannel": "物流渠道",
    "shippingService": "买家自选物流",
    "trackNumber1": "内部单号",
    "stockSkuInfo": "库存sku*数量",
    "platformSkuInfo": "平台sku*数量",
    "stockSku": "库存sku",
    "comboSku": "组合sku",
    "itemId": "itemId",
    "skuName": "商品名称",
    "platform": "平台",
    "currency": "原始币种",
    "itemTotalOrigin": "应收货款(原始货币)",
    "rate": "汇率",
    "remark": "订单备注",
    "salesId": "店长",
    "sellerName": "业务员",
    "paidTime": "付款日期",
    "expressTime": "发货日期",
    "orderCreateTime": "创建时间",
    "escrowReleaseTime": "结算时间",
    "orderWeight": "订单重量",
    "shippingWeight": "计量重量",
    "warehouseName": "仓库",
    "itemTotal": "收入-应收货款",
    "shippingTotal": "收入-应收邮资",
    "otherIncome": "收入-其他收入",
    "refundCost": "收入-退货成本",
    "freightReturnFee": "收入-运费退回",
    "subsidyAmount": "收入-补贴金额",
    "incomeTotal": "收入-小计",
    "itemTotalCost": "支出-成本",
    "paypalFee": "支出-转帐费",
    "platformFee": "支出-平台费",
    "transactionFee": "支出-交易手续费",
    "commissionFee": "支出-佣金",
    "serviceFee": "支出-服务费",
    "allianceCommission": "支出-广告费",
    "packageFee": "支出-包材费",
    "headShipping": "支出-头程费",
    "FBAMoney": "支出-FBA费用",
    "voucherPrice": "支出-优惠券",
    "vatTaxation": "支出-VAT税费",
    "otherMoney": "支出-其他费用",
    "otherExpend": "支出-其他支出",
    "evaluationFee": "支出-测评费",
    "expendTotal": "支出-小计",
    "refundFee": "退款金额",
    "quantityTotal": "销量",
    "refundQuantity": "退货数量",
    "recruitmentQuantity": "退货入库数量",
    "itemCount": "商品种类",
    "is_evaluation": "是否测评单",
    "is_split": "是否拆分订单",
    "is_union": "是否合并订单",
    "is_resend": "是否重发订单",
    "fba_flag": "是否FBA订单",
    "cod_flag": "是否COD订单",
    "is_gift": "是否赠品订单",
    "is_loan": "放款订单",
    "platform_order_status": "平台订单状态",
}

ORDERED_COLUMNS = (
    "订单编号",
    "交易号",
    "结算状态",
    "店铺名称",
    "国家",
    "商品名称",
    "平台sku*数量",
    "付款日期",
    "发货日期",
    "订单重量",
    "仓库",
    "物流公司",
    "物流渠道",
    "货运单号",
    "内部单号",
    "平台",
    "原始币种",
    "应收货款(原始货币)",
    "汇率",
    "订单备注",
    "收入-应收货款",
    "收入-应收邮资",
    "收入-其他收入",
    "收入-退货成本",
    "收入-运费退回",
    "收入-补贴金额",
    "收入-小计",
    "支出-成本",
    "预估运费",
    "支出-转帐费",
    "支出-平台费",
    "支出-交易手续费",
    "支出-佣金",
    "支出-服务费",
    "支出-广告费",
    "支出-包材费",
    "支出-头程费",
    "支出-FBA费用",
    "支出-优惠券",
    "支出-VAT税费",
    "支出-其他费用",
    "支出-其他支出",
    "支出-测评费",
    "支出-小计",
    "退款金额",
    "销量",
    "退货数量",
    "退货入库数量",
    "商品种类",
    "毛利",
    "毛利率",
)

# The current official export endpoint no longer returns these three legacy
# columns. They remain in the desktop CSV contract and are filled with blanks.
KNOWN_EMPTY_EXPORT_COLUMNS = (
    "支出-交易手续费",
    "支出-佣金",
    "支出-服务费",
)

EXPORT_HEADER_ALIASES = {
    "支出-预估运费": "预估运费",
    "支出-转账费": "支出-转帐费",
    "支出-联盟佣金": "支出-广告费",
    "支出-包材": "支出-包材费",
    "付款时间": "付款日期",
    "发货时间": "发货日期",
}

NUMERIC_COLUMNS = (
    "预估运费",
    "应收货款(原始货币)",
    "汇率",
    "收入-应收货款",
    "收入-应收邮资",
    "收入-其他收入",
    "收入-退货成本",
    "收入-运费退回",
    "收入-补贴金额",
    "收入-小计",
    "支出-成本",
    "支出-转帐费",
    "支出-平台费",
    "支出-交易手续费",
    "支出-佣金",
    "支出-服务费",
    "支出-广告费",
    "支出-包材费",
    "支出-头程费",
    "支出-FBA费用",
    "支出-优惠券",
    "支出-VAT税费",
    "支出-其他费用",
    "支出-其他支出",
    "支出-测评费",
    "支出-小计",
    "退款金额",
    "订单重量",
    "毛利",
    "毛利率",
)

INTEGER_COLUMNS = ("销量", "退货数量", "退货入库数量", "商品种类")


@dataclass(frozen=True)
class IncomeExpenseReportQuery:
    username: str
    password: str
    categories: tuple[IncomeExpenseFilterTarget, ...]
    warehouses: tuple[IncomeExpenseFilterTarget, ...]
    start_date: str
    end_date: str
    output_dir: Path
    base_url: str = MABANG_BASE_URL
    page_size: int = DEFAULT_PAGE_SIZE
    concurrency: int = DEFAULT_CONCURRENCY

    @property
    def targets(self) -> tuple[IncomeExpenseFilterTarget, ...]:
        return (*self.categories, *self.warehouses)


@dataclass(frozen=True)
class TargetExportResult:
    target: IncomeExpenseFilterTarget
    row_count: int
    page_count: int
    output_file: Path


@dataclass(frozen=True)
class IncomeExpenseReportResult:
    record_count: int
    target_count: int
    category_count: int
    warehouse_count: int
    csv_file_count: int
    output_dir: Path
    files: tuple[Path, ...]
    targets: tuple[str, ...]


def get_category_options() -> list[dict[str, str]]:
    return [
        {
            "id": category.id,
            "name": category.name,
            "mabang_name": category.mabang_name,
        }
        for category in REPORT_CATEGORIES
    ]


def get_warehouse_options() -> list[dict[str, Any]]:
    return [
        {
            "key": warehouse.key,
            "name": warehouse.name,
            "warehouse_ids": list(warehouse.filter_values),
            "mabang_names": list(warehouse.mabang_names),
            "note": warehouse.note,
        }
        for warehouse in OVERSEAS_WAREHOUSES
    ]


def previous_month_date_range(today: date | None = None) -> dict[str, str]:
    current = today or date.today()
    first_of_current = current.replace(day=1)
    end = first_of_current - timedelta(days=1)
    start = end.replace(day=1)
    return {"start_date": start.isoformat(), "end_date": end.isoformat()}


def validate_income_expense_payload(payload: dict[str, Any] | None) -> IncomeExpenseReportQuery:
    data = dict(payload or {})
    username = _safe_text(data.get("username"))
    password = str(data.get("password") or "")
    start_date = _validate_date(_safe_text(data.get("start_date")), "开始日期")
    end_date = _validate_date(_safe_text(data.get("end_date")), "结束日期")
    if start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")

    output_text = _safe_text(data.get("output_dir"))
    if not output_text:
        raise ValueError("请选择输出目录")
    if not username:
        raise ValueError("请先绑定并选择马帮账号")
    if not password:
        raise ValueError("马帮账号密码为空，请到设置中重新绑定")

    category_ids = _coerce_selection_keys(data.get("category_ids") or data.get("category_id"))
    category_map = {category.id: category for category in REPORT_CATEGORIES}
    unknown = [category_id for category_id in category_ids if category_id not in category_map]
    if unknown:
        raise ValueError(f"店铺自定义分类不存在: {', '.join(unknown)}")
    categories = tuple(category_map[category_id] for category_id in category_ids)

    warehouse_keys = _coerce_selection_keys(
        data.get("warehouse_keys") or data.get("warehouse_key")
    )
    warehouse_map = {warehouse.key: warehouse for warehouse in OVERSEAS_WAREHOUSES}
    unknown_warehouses = [key for key in warehouse_keys if key not in warehouse_map]
    if unknown_warehouses:
        raise ValueError(f"海外仓不存在: {', '.join(unknown_warehouses)}")
    warehouses = tuple(warehouse_map[key] for key in warehouse_keys)
    if not categories and not warehouses:
        raise ValueError("请至少选择一个店铺自定义分类或海外仓")

    return IncomeExpenseReportQuery(
        username=username,
        password=password,
        categories=categories,
        warehouses=warehouses,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        output_dir=Path(output_text).expanduser(),
        base_url=_safe_text(data.get("base_url")) or MABANG_BASE_URL,
        page_size=max(100, min(int(data.get("page_size") or DEFAULT_PAGE_SIZE), DEFAULT_PAGE_SIZE)),
        concurrency=max(1, min(int(data.get("concurrency") or DEFAULT_CONCURRENCY), 8)),
    )


def run_income_expense_report(
    job: IncomeExpenseReportQuery,
    progress: ProgressCallback | None = None,
) -> IncomeExpenseReportResult:
    period_label = f"{job.start_date.replace('-', '')}_{job.end_date.replace('-', '')}"
    run_dir = _create_run_dir(job.output_dir, period_label)
    _emit(progress, f"输出目录: {run_dir}")
    _emit(progress, f"发货时间: {job.start_date} 至 {job.end_date}")
    _emit(
        progress,
        f"待导出项目: {len(job.targets)} 个（分类 {len(job.categories)}，海外仓 {len(job.warehouses)}）",
    )

    completed: list[TargetExportResult] = []
    failures: list[tuple[str, str]] = []
    _emit(progress, "正在登录马帮...")
    with MabangClient(
        job.base_url,
        timeout_seconds=EXPORT_REQUEST_TIMEOUT_SECONDS,
        verify_ssl=True,
    ) as client:
        client.login(job.username, job.password)
        exporter = IncomeExpenseCsvExporter(client, progress=progress)
        for index, target in enumerate(job.targets, start=1):
            _emit(
                progress,
                f"[{index}/{len(job.targets)}] 开始通过马帮官方文件接口导出 {target.name}",
            )
            try:
                output_file = run_dir / f"{_safe_filename(target.name)}_收支报表_{period_label}.csv"
                row_count, chunk_count = exporter.export_to_csv(
                    target,
                    job.start_date,
                    job.end_date,
                    output_file,
                )
                completed.append(
                    TargetExportResult(
                        target=target,
                        row_count=row_count,
                        page_count=chunk_count,
                        output_file=output_file,
                    )
                )
                _emit(
                    progress,
                    f"{target.name} 完成: {row_count} 行，{chunk_count} 个官方导出分片",
                )
            except Exception as exc:
                failures.append((target.name, str(exc)))
                _emit(progress, f"{target.name} 导出失败: {exc}")

    if failures:
        failed_names = "、".join(name for name, _ in failures)
        raise RuntimeError(
            f"{len(failures)} 个项目导出失败（{failed_names}）；"
            f"已成功生成 {len(completed)} 个文件，保留在 {run_dir}"
        )

    files = tuple(item.output_file for item in completed)
    record_count = sum(item.row_count for item in completed)
    _emit(progress, f"全部完成: {len(files)} 个官方导出 CSV，共 {record_count} 行")
    return IncomeExpenseReportResult(
        record_count=record_count,
        target_count=len(completed),
        category_count=sum(1 for item in completed if item.target.kind == "category"),
        warehouse_count=sum(1 for item in completed if item.target.kind == "warehouse"),
        csv_file_count=len(files),
        output_dir=run_dir,
        files=files,
        targets=tuple(item.target.name for item in completed),
    )


def build_income_detail_form(
    target: IncomeExpenseFilterTarget,
    start_date: str,
    end_date: str,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> list[tuple[str, str]]:
    """Build the request locally with one business filter and shipping time only."""

    form = [
        ("categoryIdType", ""),
        ("cod_flag", ""),
        ("cost_type", "1"),
        ("costMax", ""),
        ("costMin", ""),
        ("createTimeEnd", ""),
        ("createTimeStart", ""),
        ("escrowReleaseTimeEnd", ""),
        ("escrowReleaseTimeStart", ""),
        ("fba_flag", ""),
        ("fbaFlag", ""),
        ("fixCategoryRangeisRefund", "inside"),
        ("formType", "1"),
        ("historyType", "1"),
        ("is_evaluation", ""),
        ("is_gift", ""),
        ("is_resend", ""),
        ("is_settlement", ""),
        ("is_split", ""),
        ("is_union", ""),
        ("isAuth", ""),
        ("keyColumnName", ""),
        ("keyColumnValue", ""),
        ("moneyType", "RMB"),
        ("orderByField", ""),
        ("orderByValue", ""),
        ("orderFlag", "1"),
        ("orderIds", ""),
        ("page", str(max(1, int(page)))),
        ("params", ""),
        ("paytimeTimeEnd", ""),
        ("paytimeTimeStart", ""),
        ("profitMargin", "grossRate"),
        ("profitMarginMax", ""),
        ("profitMarginMin", ""),
        ("returntimeTimeEnd", ""),
        ("returntimeTimeStart", ""),
        ("rowsPerPage", str(max(100, min(int(page_size), DEFAULT_PAGE_SIZE)))),
        ("searchTextType", "platformOrderId"),
        ("searchTextTypeWhere", ""),
        ("searchTextValue", ""),
        ("shippingType", "shippingPreCost"),
    ]
    form.extend((target.filter_field, value) for value in target.filter_values)
    form.extend(
        [
            ("expresstimeTimeEnd", f"{end_date} 23:59:59"),
            ("expresstimeTimeStart", f"{start_date} 00:00:00"),
        ]
    )
    return form


class IncomeExpenseExportError(MabangApiError):
    """Raised when the official income/expense file export is incomplete."""


def _is_explicit_no_data_payload(payload: Mapping[str, Any]) -> bool:
    """Recognize Mabang's real, explicit zero-row response shape."""

    if "exportData" in payload:
        return False
    if "tableFoot" not in payload or _safe_text(payload.get("tableFoot")):
        return False
    table_content = payload.get("tableContent")
    if not isinstance(table_content, str) or not table_content.strip():
        return False
    soup = BeautifulSoup(table_content, "html.parser")
    no_data = soup.select_one("div.alert.alert-nodata span.fsize16")
    return no_data is not None and no_data.get_text(" ", strip=True) == "暂无数据"


@dataclass(frozen=True)
class MalaysiaReportShopScope:
    all_shop_ids: tuple[str, ...]
    temu_shop_ids: tuple[str, ...]
    label_shop_ids: tuple[tuple[str, tuple[str, ...]], ...]
    remaining_shop_ids: tuple[str, ...]


def _parse_income_report_soup(page_html: str) -> BeautifulSoup:
    body = str(page_html or "")
    if not body.strip():
        raise IncomeExpenseExportError("马来雅仓店铺解析失败: 马帮报表页面为空")
    return BeautifulSoup(body, "html.parser")


def _all_shop_entries_from_soup(
    soup: BeautifulSoup,
) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    for input_tag in soup.select('input[name="shopIdMultiple[]"]'):
        shop_id = _safe_text(input_tag.get("value"))
        if not shop_id:
            raise IncomeExpenseExportError(
                "马来雅仓店铺解析失败: 页面存在缺少 value 的 shopIdMultiple[] 店铺"
            )
        entries.append(
            (shop_id, _safe_text(input_tag.get("data-shop-platform-id")))
        )
    return tuple(entries)


def _temu_shop_ids_from_soup(
    soup: BeautifulSoup,
    shop_entries: Sequence[tuple[str, str]] | None = None,
) -> tuple[str, ...]:
    """Return all TEMU shop IDs in page order, including disabled shops.

    The page contains both enabled and disabled shops.  Neither state is
    filtered here: the platform marker is the authoritative grouping used by
    the report UI.  A partially malformed match is an error because silently
    skipping it could produce an incomplete income report.
    """

    shop_ids: list[str] = []
    seen: set[str] = set()
    entries = tuple(shop_entries) if shop_entries is not None else _all_shop_entries_from_soup(soup)
    for shop_id, platform_id in entries:
        if platform_id not in MALAYSIA_TEMU_PLATFORM_IDS:
            continue
        if shop_id in seen:
            continue
        seen.add(shop_id)
        shop_ids.append(shop_id)

    if shop_ids:
        return tuple(shop_ids)

    page_text = soup.get_text(" ", strip=True)
    password_input = soup.find(
        "input",
        attrs={"type": lambda value: _safe_text(value).casefold() == "password"},
    )
    login_marker = (
        "用户登录" in page_text
        or "账号登录" in page_text
        or (password_input is not None and "登录" in page_text)
    )
    if login_marker:
        raise IncomeExpenseExportError(
            "马来雅仓 TEMU 店铺解析失败: 马帮返回登录页面，会话可能已失效"
        )
    raise IncomeExpenseExportError(
        "马来雅仓 TEMU 店铺解析失败: 报表页面未找到 platform 154/162 的店铺"
    )


def parse_malaysia_temu_shop_ids(page_html: str) -> tuple[str, ...]:
    return _temu_shop_ids_from_soup(_parse_income_report_soup(page_html))


def parse_malaysia_report_shop_scope(page_html: str) -> MalaysiaReportShopScope:
    """Parse the four known groups and the remaining-shop coverage scope."""

    soup = _parse_income_report_soup(page_html)
    shop_entries = _all_shop_entries_from_soup(soup)
    temu_shop_ids = _temu_shop_ids_from_soup(soup, shop_entries)
    all_shop_ids = tuple(dict.fromkeys(shop_id for shop_id, _ in shop_entries))
    label_memberships: list[tuple[str, tuple[str, ...]]] = []

    for group_name, label_id in MALAYSIA_SHOP_LABEL_GROUPS:
        label_inputs = [
            input_tag
            for input_tag in soup.select('input[name="shopLabelIds[]"]')
            if _safe_text(input_tag.get("value")) == label_id
        ]
        if not label_inputs:
            raise IncomeExpenseExportError(
                f"马来雅仓店铺解析失败: 页面缺少 {group_name} 标签 {label_id}"
            )

        member_ids: list[str] = []
        seen_members: set[str] = set()
        found_membership_attribute = False
        for input_tag in label_inputs:
            raw_shop_ids = input_tag.get("data-shopids")
            if raw_shop_ids is None:
                raw_shop_ids = input_tag.get("data-shop-ids")
            if raw_shop_ids is None:
                continue
            found_membership_attribute = True
            for value in str(raw_shop_ids).split(","):
                shop_id = value.strip()
                if not shop_id or shop_id in seen_members:
                    continue
                seen_members.add(shop_id)
                member_ids.append(shop_id)

        if not found_membership_attribute:
            raise IncomeExpenseExportError(
                f"马来雅仓店铺解析失败: {group_name} 标签缺少 data-shopIds"
            )
        if not member_ids:
            raise IncomeExpenseExportError(
                f"马来雅仓店铺解析失败: {group_name} 标签的 data-shopIds 为空"
            )
        label_memberships.append((group_name, tuple(member_ids)))

    named_groups = [
        *((group_name, set(shop_ids)) for group_name, shop_ids in label_memberships),
        ("TEMU 马来", set(temu_shop_ids)),
    ]
    for left_index, (left_name, left_ids) in enumerate(named_groups):
        for right_name, right_ids in named_groups[left_index + 1 :]:
            overlap = sorted(left_ids & right_ids)
            if overlap:
                preview = "、".join(overlap[:10])
                suffix = "..." if len(overlap) > 10 else ""
                raise IncomeExpenseExportError(
                    f"马来雅仓店铺范围重叠: {left_name} 与 {right_name} "
                    f"共有 {len(overlap)} 个店铺（{preview}{suffix}）"
                )

    known_shop_ids = set(temu_shop_ids)
    for _, shop_ids in label_memberships:
        known_shop_ids.update(shop_ids)
    remaining_shop_ids = tuple(
        shop_id for shop_id in all_shop_ids if shop_id not in known_shop_ids
    )

    return MalaysiaReportShopScope(
        all_shop_ids=all_shop_ids,
        temu_shop_ids=temu_shop_ids,
        label_shop_ids=tuple(label_memberships),
        remaining_shop_ids=remaining_shop_ids,
    )


class IncomeExpenseCsvExporter:
    """Initialize a query and download its official CSV chunks in one session."""

    def __init__(
        self,
        mabang: MabangClient,
        *,
        max_attempts: int = MAX_EXPORT_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.mabang = mabang
        self.max_attempts = max(1, int(max_attempts))
        self.sleep = sleep
        self.jitter = jitter or (lambda: random.uniform(0, 10))
        self.progress = progress
        base_url = (_safe_text(getattr(mabang, "base_url", "")) or MABANG_BASE_URL).rstrip("/")
        self.index_url = f"{base_url}/index.php"

    def _request_json(
        self,
        method: str,
        *,
        params: Sequence[tuple[str, str]] | Mapping[str, str],
        content: str | None = None,
        context: str,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            waf_blocked = False
            try:
                base_url = self.index_url.rsplit("/index.php", 1)[0]
                headers = {
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": base_url,
                    "Referer": f"{base_url}{INCOME_REPORT_REFERER}",
                    "X-Requested-With": "XMLHttpRequest",
                }
                request_kwargs: dict[str, Any] = {
                    "params": params,
                    "headers": headers,
                }
                if content is not None:
                    request_kwargs["content"] = content
                response = self.mabang.client.request(
                    method,
                    self.index_url,
                    **request_kwargs,
                )
                body = response.text
                if _looks_like_waf(body):
                    waf_blocked = True
                    raise IncomeExpenseExportError("马帮请求被 WAF 暂时拦截")
                if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                    raise IncomeExpenseExportError(
                        f"马帮暂时不可用，HTTP {response.status_code}"
                    )
                response.raise_for_status()
                if not body.strip():
                    raise IncomeExpenseExportError("马帮返回空响应")
                if body.lstrip().startswith("<"):
                    raise IncomeExpenseExportError("马帮返回了非预期 HTML，登录可能已失效")
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise IncomeExpenseExportError("马帮返回了无法解析的 JSON") from exc
                if not isinstance(payload, dict):
                    raise IncomeExpenseExportError("马帮返回的 JSON 顶层不是对象")
                return payload
            except (httpx.HTTPError, IncomeExpenseExportError) as exc:
                last_error = exc
                if attempt >= self.max_attempts:
                    break
                delays = WAF_RETRY_DELAYS if waf_blocked else TRANSIENT_RETRY_DELAYS
                delay = float(delays[min(attempt - 1, len(delays) - 1)])
                if waf_blocked:
                    delay += max(0.0, float(self.jitter()))
                    _emit(
                        self.progress,
                        f"{context} 遇到 WAF，等待 {delay:.0f} 秒后重试同一请求 "
                        f"({attempt}/{self.max_attempts})",
                    )
                else:
                    _emit(
                        self.progress,
                        f"{context} 请求异常，等待 {delay:.0f} 秒后重试 "
                        f"({attempt}/{self.max_attempts})",
                    )
                self.sleep(delay)

        detail = (
            _safe_text(last_error) or type(last_error).__name__
            if last_error
            else "未知错误"
        )
        raise IncomeExpenseExportError(
            f"{context} 连续 {self.max_attempts} 次请求失败: {detail}"
        ) from last_error

    def _request_report_page(self, context: str) -> str:
        """Load the authenticated report page used to discover TEMU shops."""

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            waf_blocked = False
            try:
                response = self.mabang.client.request(
                    "GET",
                    self.index_url,
                    params=INCOME_REPORT_PARAMS,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": self.index_url,
                    },
                )
                body = response.text
                if _looks_like_waf(body):
                    waf_blocked = True
                    raise IncomeExpenseExportError("马帮请求被 WAF 暂时拦截")
                if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                    raise IncomeExpenseExportError(
                        f"马帮暂时不可用，HTTP {response.status_code}"
                    )
                response.raise_for_status()
                if not body.strip():
                    raise IncomeExpenseExportError("马帮返回空报表页面")
                return body
            except (httpx.HTTPError, IncomeExpenseExportError) as exc:
                last_error = exc
                if attempt >= self.max_attempts:
                    break
                delays = WAF_RETRY_DELAYS if waf_blocked else TRANSIENT_RETRY_DELAYS
                delay = float(delays[min(attempt - 1, len(delays) - 1)])
                if waf_blocked:
                    delay += max(0.0, float(self.jitter()))
                    _emit(
                        self.progress,
                        f"{context} 遇到 WAF，等待 {delay:.0f} 秒后重试同一请求 "
                        f"({attempt}/{self.max_attempts})",
                    )
                else:
                    _emit(
                        self.progress,
                        f"{context} 请求异常，等待 {delay:.0f} 秒后重试 "
                        f"({attempt}/{self.max_attempts})",
                    )
                self.sleep(delay)

        detail = (
            _safe_text(last_error) or type(last_error).__name__
            if last_error
            else "未知错误"
        )
        raise IncomeExpenseExportError(
            f"{context} 连续 {self.max_attempts} 次请求失败: {detail}"
        ) from last_error

    def discover_malaysia_report_shop_scope(self) -> MalaysiaReportShopScope:
        context = "马来雅仓海外仓 获取收支报表店铺列表页面"
        page_html = self._request_report_page(context)
        scope = parse_malaysia_report_shop_scope(page_html)
        coverage_batch_count = (
            len(scope.remaining_shop_ids) + MALAYSIA_COVERAGE_BATCH_SIZE - 1
        ) // MALAYSIA_COVERAGE_BATCH_SIZE
        _emit(
            self.progress,
            f"马来雅仓海外仓: 页面共识别 {len(scope.all_shop_ids)} 个唯一店铺，"
            f"其中 TEMU {len(scope.temu_shop_ids)} 个；"
            "并确认 4 个业务组店铺范围互不重叠；"
            f"其余 {len(scope.remaining_shop_ids)} 个店铺分 {coverage_batch_count} 批覆盖；"
            "所有查询均限定 stockWarehouseId=1096191",
        )
        return scope

    def discover_malaysia_temu_shop_ids(self) -> tuple[str, ...]:
        return self.discover_malaysia_report_shop_scope().temu_shop_ids

    def initialize_query(
        self,
        target: IncomeExpenseFilterTarget,
        start_date: str,
        end_date: str,
    ) -> tuple[list[tuple[str, str]], int]:
        form_data = build_income_detail_form(
            target,
            start_date,
            end_date,
            page=1,
            page_size=EXPORT_INIT_ROWS_PER_PAGE,
        )
        expected_count = self._initialize_form_query(target.name, form_data)
        return form_data, expected_count

    def _initialize_form_query(
        self,
        display_name: str,
        form_data: list[tuple[str, str]],
    ) -> int:
        payload = self._request_json(
            "POST",
            params=INCOME_DETAIL_PARAMS,
            content=urlencode(form_data, doseq=True),
            context=f"{display_name} 初始化收支报表查询",
        )
        if payload.get("success") is not True:
            raise IncomeExpenseExportError(f"{display_name} 查询未明确返回成功")
        if not _safe_text(payload.get("excelOutAllNewKey")):
            raise IncomeExpenseExportError(f"{display_name} 查询响应缺少导出缓存标识")

        _, expected_count = parse_income_pagination(str(payload.get("pageHtml") or ""))
        export_data = payload.get("exportData")
        if not isinstance(export_data, list):
            if expected_count is None and _is_explicit_no_data_payload(payload):
                expected_count = 0
            else:
                raise IncomeExpenseExportError(
                    f"{display_name} 查询响应缺少 exportData 列表"
                )
        if expected_count is None:
            raise IncomeExpenseExportError(
                f"{display_name} 无法从查询响应解析总条数，已停止生成文件"
            )
        _emit(self.progress, f"{display_name}: 官方查询共 {expected_count} 条")
        return expected_count

    @staticmethod
    def _parse_has_next(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value in (0, "0", None, "", "false", "False"):
            return False
        if value in (1, "1", "true", "True"):
            return True
        raise IncomeExpenseExportError(f"无法识别 hasNext: {value!r}")

    @staticmethod
    def _normalize_frame(raw_rows: list[Any], header: list[str]) -> pd.DataFrame:
        if any(not isinstance(row, list) for row in raw_rows):
            raise IncomeExpenseExportError("导出分片包含非数组行")
        wrong_widths = sorted({len(row) for row in raw_rows if len(row) != len(header)})
        if wrong_widths:
            raise IncomeExpenseExportError(
                f"导出分片列数异常: 表头 {len(header)} 列，数据出现 {wrong_widths} 列"
            )

        normalized_header = [
            EXPORT_HEADER_ALIASES.get(_safe_text(name), _safe_text(name)) for name in header
        ]
        if len(set(normalized_header)) != len(normalized_header):
            raise IncomeExpenseExportError("导出表头规范化后出现重复列")

        frame = pd.DataFrame(raw_rows, columns=normalized_header)
        required = [
            column for column in ORDERED_COLUMNS if column not in KNOWN_EMPTY_EXPORT_COLUMNS
        ]
        missing_required = [column for column in required if column not in frame.columns]
        if missing_required:
            raise IncomeExpenseExportError(
                "导出结果缺少必需列: " + ", ".join(missing_required)
            )

        for column in ORDERED_COLUMNS:
            if column not in frame.columns:
                frame[column] = ""
        frame = frame[list(ORDERED_COLUMNS)]
        order_numbers = frame["订单编号"].fillna("").astype(str).str.strip()
        frame = frame[(order_numbers != "") & (order_numbers != "合计")].copy()
        for column in NUMERIC_COLUMNS:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        for column in INTEGER_COLUMNS:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Int64")
        return frame

    @staticmethod
    def _row_fingerprint(row: Sequence[Any]) -> bytes:
        digest = hashlib.sha256()
        for value in row:
            text = "" if pd.isna(value) else str(value)
            encoded = text.encode("utf-8", errors="surrogatepass")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.digest()

    def export_to_csv(
        self,
        target: IncomeExpenseFilterTarget,
        start_date: str,
        end_date: str,
        output_path: str | os.PathLike[str],
    ) -> tuple[int, int]:
        """Write official chunks to a temporary CSV and atomically publish it."""

        if target.key == MALAYSIA_WAREHOUSE_KEY:
            return self._export_malaysia_warehouse_to_csv(
                target,
                start_date,
                end_date,
                output_path,
            )

        form_data, expected_count = self.initialize_query(target, start_date, end_date)
        return self._write_initialized_groups_atomically(
            [(target.name, form_data, expected_count)],
            output_path,
        )

    def _export_malaysia_warehouse_to_csv(
        self,
        target: IncomeExpenseFilterTarget,
        start_date: str,
        end_date: str,
        output_path: str | os.PathLike[str],
    ) -> tuple[int, int]:
        _emit(
            self.progress,
            f"{target.name}: 使用完整日期范围按 4 个业务组+其余店铺覆盖导出"
            "（日期不拆分）",
        )
        shop_scope = self.discover_malaysia_report_shop_scope()

        groups: list[tuple[str, list[tuple[str, str]]]] = []
        for group_name, shop_label_id in MALAYSIA_SHOP_LABEL_GROUPS:
            form_data = build_income_detail_form(
                target,
                start_date,
                end_date,
                page=1,
                page_size=EXPORT_INIT_ROWS_PER_PAGE,
            )
            form_data.append(("shopLabelIds[]", shop_label_id))
            groups.append((group_name, form_data))

        temu_form_data = build_income_detail_form(
            target,
            start_date,
            end_date,
            page=1,
            page_size=EXPORT_INIT_ROWS_PER_PAGE,
        )
        temu_form_data.extend(
            ("shopIdMultiple[]", shop_id) for shop_id in shop_scope.temu_shop_ids
        )
        groups.append(("TEMU 马来", temu_form_data))

        remaining_shop_ids = shop_scope.remaining_shop_ids
        coverage_batch_count = (
            len(remaining_shop_ids) + MALAYSIA_COVERAGE_BATCH_SIZE - 1
        ) // MALAYSIA_COVERAGE_BATCH_SIZE
        for batch_index in range(coverage_batch_count):
            batch_start = batch_index * MALAYSIA_COVERAGE_BATCH_SIZE
            batch_shop_ids = remaining_shop_ids[
                batch_start : batch_start + MALAYSIA_COVERAGE_BATCH_SIZE
            ]
            coverage_form_data = build_income_detail_form(
                target,
                start_date,
                end_date,
                page=1,
                page_size=EXPORT_INIT_ROWS_PER_PAGE,
            )
            coverage_form_data.extend(
                ("shopIdMultiple[]", shop_id) for shop_id in batch_shop_ids
            )
            if len(coverage_form_data) >= 1000:
                raise IncomeExpenseExportError(
                    "马来雅仓其他店铺覆盖请求字段数达到安全上限，已停止导出"
                )
            groups.append(
                (
                    f"其他店铺覆盖 {batch_index + 1}/{coverage_batch_count}",
                    coverage_form_data,
                )
            )

        final_path = Path(output_path)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._temporary_csv_path(final_path)
        total_rows = 0
        total_chunks = 0
        wrote_header = False
        previous_group_fingerprints: set[bytes] = set()

        try:
            with temp_path.open("w", encoding="utf-8-sig", newline="") as output:
                for index, (group_name, form_data) in enumerate(groups, start=1):
                    display_name = f"{target.name} / {group_name}"
                    current_group_fingerprints: set[bytes] = set()
                    _emit(
                        self.progress,
                        f"{target.name}: [{index}/{len(groups)}] 开始导出 {group_name}",
                    )
                    expected_count = self._initialize_form_query(display_name, form_data)
                    row_count, chunk_count, wrote_header = self._write_export_chunks(
                        display_name,
                        form_data,
                        expected_count,
                        output,
                        wrote_header=wrote_header,
                        previous_group_fingerprints=previous_group_fingerprints,
                        current_group_fingerprints=current_group_fingerprints,
                    )
                    previous_group_fingerprints.update(current_group_fingerprints)
                    total_rows += row_count
                    total_chunks += chunk_count
                    _emit(
                        self.progress,
                        f"{target.name}: [{index}/{len(groups)}] {group_name} 完成，"
                        f"{row_count} 行，{chunk_count} 个分片",
                    )

                if not wrote_header:
                    pd.DataFrame(columns=ORDERED_COLUMNS).to_csv(output, index=False)
                    wrote_header = True
                output.flush()
                os.fsync(output.fileno())

            self._publish_temporary_csv(temp_path, final_path, wrote_header)
        finally:
            self._remove_temporary_csv(temp_path)
        return total_rows, total_chunks

    @staticmethod
    def _temporary_csv_path(final_path: Path) -> Path:
        return final_path.with_name(
            f".{final_path.name}.{os.getpid()}.{uuid.uuid4().hex}.part"
        )

    @staticmethod
    def _publish_temporary_csv(
        temp_path: Path,
        final_path: Path,
        wrote_header: bool,
    ) -> None:
        if not wrote_header or not temp_path.is_file() or temp_path.stat().st_size <= 0:
            raise IncomeExpenseExportError("临时 CSV 未正确生成")
        os.replace(temp_path, final_path)

    @staticmethod
    def _remove_temporary_csv(temp_path: Path) -> None:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass

    def _write_initialized_groups_atomically(
        self,
        groups: Sequence[tuple[str, list[tuple[str, str]], int]],
        output_path: str | os.PathLike[str],
    ) -> tuple[int, int]:
        final_path = Path(output_path)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._temporary_csv_path(final_path)
        total_rows = 0
        total_chunks = 0
        wrote_header = False

        try:
            with temp_path.open("w", encoding="utf-8-sig", newline="") as output:
                for display_name, form_data, expected_count in groups:
                    row_count, chunk_count, wrote_header = self._write_export_chunks(
                        display_name,
                        form_data,
                        expected_count,
                        output,
                        wrote_header=wrote_header,
                    )
                    total_rows += row_count
                    total_chunks += chunk_count
                if not wrote_header:
                    pd.DataFrame(columns=ORDERED_COLUMNS).to_csv(output, index=False)
                    wrote_header = True
                output.flush()
                os.fsync(output.fileno())

            self._publish_temporary_csv(temp_path, final_path, wrote_header)
        finally:
            self._remove_temporary_csv(temp_path)
        return total_rows, total_chunks

    def _write_export_chunks(
        self,
        display_name: str,
        form_data: Sequence[tuple[str, str]],
        expected_count: int,
        output: TextIO,
        *,
        wrote_header: bool,
        previous_group_fingerprints: set[bytes] | None = None,
        current_group_fingerprints: set[bytes] | None = None,
    ) -> tuple[int, int, bool]:
        if expected_count == 0:
            return 0, 0, wrote_header

        money_type = _form_value(form_data, "moneyType") or "RMB"
        history_type = _form_value(form_data, "historyType") or "1"
        cache_key = "1"
        page_no = 0
        max_page: int | None = None
        header: list[str] | None = None
        written_rows = 0
        chunk_count = 0

        while page_no < MAX_EXPORT_CHUNKS:
            params: list[tuple[str, str]] = [
                ("mod", INCOME_EXPORT_MOD),
                ("moneyType", money_type),
                ("historyType", history_type),
                ("isNew", "1"),
                ("cacheKey", cache_key),
                ("pageNo", str(page_no)),
            ]
            params.extend(("field[]", field) for field in INCOME_EXPORT_FIELDS)
            payload = self._request_json(
                "GET",
                params=params,
                context=f"{display_name} 导出第 {page_no + 1} 个分片",
            )
            raw_data = payload.get("data")
            if not isinstance(raw_data, list):
                raise IncomeExpenseExportError("导出响应缺少 data 列表")
            if "hasNext" not in payload:
                raise IncomeExpenseExportError("导出响应缺少 hasNext")
            data = list(raw_data)

            if page_no == 0:
                if not data or not isinstance(data[0], list):
                    raise IncomeExpenseExportError("首个导出分片缺少表头")
                header = [_safe_text(value) for value in data.pop(0)]
                if "订单编号" not in header:
                    raise IncomeExpenseExportError("导出表头缺少订单编号")
                try:
                    max_page = int(payload.get("max"))
                except (TypeError, ValueError) as exc:
                    raise IncomeExpenseExportError("首个导出分片缺少 max") from exc
                if max_page < 0:
                    raise IncomeExpenseExportError("首个导出分片的 max 无效")
                _emit(
                    self.progress,
                    f"{display_name}: 预计 {max_page + 1} 个官方导出分片",
                )
            elif header is None:
                raise IncomeExpenseExportError("导出表头状态丢失")
            elif data and data[0] == header:
                data.pop(0)

            assert header is not None
            frame = self._normalize_frame(data, header)
            if (
                previous_group_fingerprints is not None
                and current_group_fingerprints is not None
            ):
                for row in frame.itertuples(index=False, name=None):
                    fingerprint = self._row_fingerprint(row)
                    if fingerprint in previous_group_fingerprints:
                        order_number = _safe_text(row[0]) or "未知订单"
                        message = (
                            f"{display_name} 检测到跨业务组重复明细，"
                            f"订单 {order_number}；已停止合并，未覆盖目标文件"
                        )
                        _emit(self.progress, message)
                        raise IncomeExpenseExportError(message)
                    current_group_fingerprints.add(fingerprint)
            frame.to_csv(output, index=False, header=not wrote_header)
            wrote_header = True
            written_rows += len(frame)
            chunk_count += 1

            has_next = self._parse_has_next(payload.get("hasNext"))
            response_cache_key = _safe_text(payload.get("cacheKey"))
            _emit(
                self.progress,
                f"{display_name}: 第 {page_no + 1} 个分片完成，"
                f"累计 {written_rows}/{expected_count} 条",
            )
            if has_next:
                if not response_cache_key:
                    raise IncomeExpenseExportError("后续分片缺少 cacheKey")
                if max_page is not None and page_no >= max_page:
                    raise IncomeExpenseExportError("hasNext 与 max 分片数矛盾")
                cache_key = response_cache_key
                page_no += 1
                continue

            if max_page is not None and page_no != max_page:
                raise IncomeExpenseExportError(
                    f"导出提前结束: 当前分片 {page_no + 1}，预计 {max_page + 1}"
                )
            break
        else:
            raise IncomeExpenseExportError("导出分片超过安全上限")

        if written_rows != expected_count:
            raise IncomeExpenseExportError(
                f"{display_name} 导出明细数不一致: "
                f"查询 {expected_count} 条，实际 {written_rows} 条"
            )
        return written_rows, chunk_count, wrote_header


def fetch_target_rows(
    client: MabangClient,
    target: IncomeExpenseFilterTarget,
    start_date: str,
    end_date: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    concurrency: int = DEFAULT_CONCURRENCY,
    progress: ProgressCallback | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    first = _request_income_page(client, target, start_date, end_date, 1, page_size)
    first_rows = _export_rows(first)
    page_count, expected_count = parse_income_pagination(str(first.get("pageHtml") or ""))
    page_count = max(1, page_count)
    if page_count > MAX_PAGES:
        raise RuntimeError(f"分页数超过保护上限: {page_count}")
    _emit(progress, f"{target.name}: 共 {expected_count if expected_count is not None else '?'} 条，{page_count} 页")

    page_rows: dict[int, list[dict[str, Any]]] = {1: first_rows}
    if page_count > 1:
        with ThreadPoolExecutor(max_workers=max(1, min(int(concurrency), 8))) as executor:
            futures = {
                executor.submit(
                    _request_income_page,
                    client,
                    target,
                    start_date,
                    end_date,
                    page,
                    page_size,
                ): page
                for page in range(2, page_count + 1)
            }
            for future in as_completed(futures):
                page = futures[future]
                rows = _export_rows(future.result())
                page_rows[page] = rows
                _emit(progress, f"{target.name}: 第 {page}/{page_count} 页完成，{len(rows)} 条")

    rows = [row for page in sorted(page_rows) for row in page_rows[page]]
    if expected_count is not None and len(rows) != expected_count:
        raise RuntimeError(f"数据不完整: 页面显示 {expected_count} 条，实际获取 {len(rows)} 条")
    return rows, {
        "row_count": len(rows),
        "expected_row_count": expected_count,
        "page_count": page_count,
        "page_size": page_size,
    }


def process_income_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows).rename(columns=FIELD_MAPPING)
    for column in ORDERED_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""

    for column in NUMERIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in INTEGER_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Int64")

    frame = frame[list(ORDERED_COLUMNS)]
    order_numbers = frame["订单编号"].fillna("").astype(str).str.strip()
    return frame[(order_numbers != "") & (order_numbers != "合计")].reset_index(drop=True)


def parse_income_pagination(page_html: str) -> tuple[int, int | None]:
    text = BeautifulSoup(str(page_html or ""), "html.parser").get_text(" ", strip=True)
    page_match = re.search(r"(\d+)\s*/\s*(\d+)\s*页", text)
    count_match = re.search(r"共\s*([\d,]+)\s*条", text)
    page_count = int(page_match.group(2)) if page_match else 1
    expected_count = int(count_match.group(1).replace(",", "")) if count_match else None
    return page_count, expected_count


def _request_income_page(
    client: MabangClient,
    target: IncomeExpenseFilterTarget,
    start_date: str,
    end_date: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            payload = client.post_form_json(
                INCOME_DETAIL_PATH,
                params=INCOME_DETAIL_PARAMS,
                form_data=build_income_detail_form(
                    target,
                    start_date,
                    end_date,
                    page=page,
                    page_size=page_size,
                ),
                context=f"{target.name} 收支报表第 {page} 页",
                referer=INCOME_REPORT_REFERER,
            )
            if not payload.get("success"):
                raise MabangApiError(f"{target.name} 收支报表第 {page} 页返回失败")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(attempt)
    raise RuntimeError(f"第 {page} 页连续失败: {last_error}") from last_error


def _export_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("exportData")
    if rows is None:
        raise MabangApiError("马帮响应缺少 exportData")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise MabangApiError("马帮 exportData 格式异常")
    return list(rows)


def _create_run_dir(output_root: Path, period_label: str) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = output_root / f"马帮收支报表_{period_label}_{stamp}"
    run_dir.mkdir(parents=False, exist_ok=False)
    return run_dir


def _coerce_selection_keys(value: Any) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[,$，\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    elif value is None:
        values = []
    else:
        values = [value]
    result: list[str] = []
    for item in values:
        key = _safe_text(item)
        if key and key not in result:
            result.append(key)
    return result


def _validate_date(value: str, label: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{label}格式必须是 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label}不是有效日期") from exc


def _looks_like_waf(body: str) -> bool:
    lowered = str(body or "").casefold()
    return any(marker.casefold() in lowered for marker in WAF_MARKERS)


def _form_value(form_data: Sequence[tuple[str, str]], key: str) -> str:
    for item_key, item_value in form_data:
        if item_key == key:
            return _safe_text(item_value)
    return ""


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", _safe_text(value)).strip(" .")
    return cleaned or "未命名分类"


def _safe_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress:
        progress(message)
