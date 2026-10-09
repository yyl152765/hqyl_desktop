from __future__ import annotations

import csv
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from bs4 import BeautifulSoup

from backend.core.mabang_client import MabangApiError, MabangClient
from backend.core.mabang_order_client import OrderShopInfo, list_order_shops
from backend.services.vietnam_income_collection import (
    REPORT_TYPES,
    load_manifest,
    report_is_completed,
    write_manifest,
)


ProgressCallback = Callable[[str], None]

MABANG_BASE_URL = "https://900853.private.mabangerp.com"
SHOPEE_PLATFORM_ID = "17"
DETAIL_PAGE_SIZE = 2000
DETAIL_MAX_PAGES = 2000
DETAIL_CONCURRENCY = 4
MONEY_TYPE = "RMB"

INCOME_DETAIL_PATH = "/index.php"
INCOME_DETAIL_PARAMS = {"mod": "reports.getIncomePayParticularsData"}
INCOME_SUMMARY_PATH = "/index.php"
INCOME_SUMMARY_PARAMS = {"mod": "reports.getIncomePayData"}
INCOME_REPORT_REFERER = "/index.php?mod=reports.incomePay"

MABANG_REPORT_TYPES = (
    "income_detail",
    "income_summary",
    "refund_part_01_10",
    "refund_part_11_20",
    "refund_part_21_end",
    "refunds_merged",
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
    "站点",
    "国家",
    "订单业务模式",
    "商品名称",
    "库存sku*数量",
    "平台sku*数量",
    "库存sku",
    "组合sku",
    "itemId",
    "付款日期",
    "发货日期",
    "创建时间",
    "结算时间",
    "订单重量",
    "计量重量",
    "仓库",
    "物流公司",
    "物流渠道",
    "买家自选物流",
    "货运单号",
    "内部单号",
    "平台",
    "原始币种",
    "应收货款(原始货币)",
    "汇率",
    "订单备注",
    "店长",
    "业务员",
    "收入-应收货款",
    "收入-应收邮资",
    "收入-其他收入",
    "收入-退货成本",
    "收入-运费退回",
    "收入-补贴金额",
    "收入-小计",
    "支出-成本",
    "预估运费",
    "实际运费",
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
    "是否测评单",
    "是否拆分订单",
    "是否合并订单",
    "是否重发订单",
    "是否FBA订单",
    "是否COD订单",
    "是否赠品订单",
    "放款订单",
    "平台订单状态",
)

REQUIRED_DETAIL_COLUMNS = (
    "订单编号",
    "店铺名称",
    "付款日期",
    "收入-应收货款",
    "汇率",
    "订单重量",
    "物流渠道",
    "预估运费",
    "支出-包材费",
    "销量",
)

REQUIRED_DETAIL_SOURCE_FIELDS = (
    "platformOrder",
    "shopName",
    "paidTime",
    "itemTotal",
    "rate",
    "orderWeight",
    "logisticsChannel",
    "shippingCostNew",
    "packageFee",
    "quantityTotal",
)


class MabangIncomeValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VietnamMabangIncomeJob:
    manifest_path: Path
    username: str
    password: str
    account_name: str = ""
    base_url: str = MABANG_BASE_URL
    page_size: int = DETAIL_PAGE_SIZE
    concurrency: int = DETAIL_CONCURRENCY


def validate_vietnam_mabang_income_payload(
    payload: dict[str, Any] | None,
) -> VietnamMabangIncomeJob:
    data = dict(payload or {})
    manifest_path = Path(str(data.get("manifest_path") or "").strip())
    manifest = load_manifest(manifest_path)
    if manifest.get("source") != "ziniao":
        raise ValueError("批次不是越南紫鸟收支核对批次")
    if not _ziniao_sources_ready(manifest_path, manifest):
        raise ValueError("请先完成该批次的紫鸟四类数据采集")
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not username or not password:
        raise ValueError("马帮账号配置不完整")
    return VietnamMabangIncomeJob(
        manifest_path=manifest_path,
        username=username,
        password=password,
        account_name=str(data.get("account_name") or username).strip(),
        base_url=str(data.get("base_url") or MABANG_BASE_URL).strip(),
        page_size=max(100, min(int(data.get("page_size") or DETAIL_PAGE_SIZE), DETAIL_PAGE_SIZE)),
        concurrency=max(1, min(int(data.get("concurrency") or DETAIL_CONCURRENCY), 8)),
    )


def run_vietnam_mabang_income_collection(
    job: VietnamMabangIncomeJob,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(job.manifest_path)
    _ensure_mabang_section(manifest, job.account_name)
    if _mabang_income_completed(job.manifest_path, manifest):
        validation = manifest["mabang"].get("validation") or {}
        _emit(progress, "马帮收支明细和汇总校验已完成，跳过重复导出")
        return _result_payload(job.manifest_path, validation, skipped_count=2)

    run_dir = job.manifest_path.parent
    mabang_root = run_dir / "raw" / "mabang"
    shops_dir = mabang_root / "shops"
    detail_dir = mabang_root / "income_detail"
    summary_dir = mabang_root / "income_summary"
    for directory in (shops_dir, detail_dir, summary_dir):
        directory.mkdir(parents=True, exist_ok=True)

    manifest["status"] = "mabang_collecting"
    manifest["mabang"]["status"] = "collecting_income"
    _set_report_running(manifest, "income_detail")
    _set_report_running(manifest, "income_summary")
    manifest["mabang"]["validation"] = _empty_validation("running")
    _touch_manifest(manifest)
    write_manifest(job.manifest_path, manifest)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    start_date = str(manifest["start_date"])
    end_date = str(manifest["end_date"])

    try:
        _emit(progress, f"正在使用马帮账号 {job.account_name or job.username} 登录")
        with MabangClient(job.base_url, timeout_seconds=120) as client:
            client.login(job.username, job.password)
            shops = discover_shopee_vietnam_shops(client)
            if not shops:
                raise RuntimeError("马帮未找到 Shopee 越南店铺")
            _emit(progress, f"已识别 Shopee 越南店铺 {len(shops)} 家")

            snapshot_path = shops_dir / f"shopee_vietnam_shop_snapshot__{stamp}.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "platform_id": SHOPEE_PLATFORM_ID,
                        "country": "越南",
                        "collected_at": _now_text(),
                        "store_count": len(shops),
                        "stores": [
                            {
                                "shop_id": shop.shop_id,
                                "shop_name": shop.shop_name,
                                "platform_id": shop.platform_id,
                                "platform_name": shop.platform_name,
                            }
                            for shop in shops
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            manifest = load_manifest(job.manifest_path)
            _ensure_mabang_section(manifest, job.account_name)
            manifest["mabang"]["shop_snapshot_file"] = _relative(run_dir, snapshot_path)
            manifest["mabang"]["shop_count"] = len(shops)
            _touch_manifest(manifest)
            write_manifest(job.manifest_path, manifest)

            raw_rows, detail_meta = fetch_income_detail_rows(
                client,
                [shop.shop_id for shop in shops],
                start_date,
                end_date,
                page_size=job.page_size,
                concurrency=job.concurrency,
                progress=progress,
            )
            _validate_detail_source_fields(raw_rows)
            normalized_rows = normalize_income_rows(raw_rows)

            detail_path = detail_dir / (
                f"mabang_income_detail__{_compact(start_date)}_{_compact(end_date)}__{stamp}.csv"
            )
            _write_csv(detail_path, normalized_rows, ORDERED_COLUMNS)
            detail_meta_path = detail_dir / f"income_detail_meta__{stamp}.json"
            detail_meta_path.write_text(
                json.dumps(
                    {
                        **detail_meta,
                        "source": "reports.getIncomePayParticularsData",
                        "date_type": "paytime",
                        "currency": MONEY_TYPE,
                        "start_date": start_date,
                        "end_date": end_date,
                        "shop_count": len(shops),
                        "file": str(detail_path),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            detail_order_count = len(
                {str(row.get("订单编号") or "").strip() for row in normalized_rows if str(row.get("订单编号") or "").strip()}
            )
            _set_report_success(
                job.manifest_path,
                "income_detail",
                file=_relative(run_dir, detail_path),
                raw_file=_relative(run_dir, detail_meta_path),
                row_count=len(normalized_rows),
                order_count=detail_order_count,
                page_count=int(detail_meta.get("page_count") or 0),
            )
            _emit(progress, f"马帮收支明细完成：{len(normalized_rows)} 行，{detail_order_count} 个订单")

            summary_rows, summary_meta = fetch_income_summary(
                client,
                [shop.shop_id for shop in shops],
                start_date,
                end_date,
            )
            summary_path = summary_dir / (
                f"mabang_income_summary_rmb__{_compact(start_date)}_{_compact(end_date)}__{stamp}.csv"
            )
            _write_csv(
                summary_path,
                summary_rows,
                ("店铺名称", "订单数", "应收货款", "收入小计"),
            )
            summary_meta_path = summary_dir / f"income_summary_meta__{stamp}.json"
            summary_meta_path.write_text(
                json.dumps(
                    {
                        **summary_meta,
                        "source": "reports.getIncomePayData",
                        "date_type": "paytime",
                        "currency": MONEY_TYPE,
                        "start_date": start_date,
                        "end_date": end_date,
                        "file": str(summary_path),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            _set_report_success(
                job.manifest_path,
                "income_summary",
                file=_relative(run_dir, summary_path),
                raw_file=_relative(run_dir, summary_meta_path),
                row_count=len(summary_rows),
            )
            _emit(progress, f"马帮收支汇总完成：{len(summary_rows)} 个有数据店铺")

        validation = validate_income_totals(normalized_rows, summary_rows)
        validation_path = summary_dir / f"income_validation__{stamp}.json"
        validation["file"] = _relative(run_dir, validation_path)
        validation_path.write_text(
            json.dumps(validation, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest = load_manifest(job.manifest_path)
        _ensure_mabang_section(manifest, job.account_name)
        manifest["mabang"]["validation"] = validation
        if validation["status"] == "passed":
            manifest["mabang"]["status"] = "income_ready"
            manifest["status"] = "mabang_income_ready"
        else:
            manifest["mabang"]["status"] = "validation_failed"
            manifest["status"] = "mabang_validation_failed"
        _touch_manifest(manifest)
        write_manifest(job.manifest_path, manifest)

        if validation["status"] != "passed":
            raise MabangIncomeValidationError(
                "马帮收支校验失败："
                f"订单数差异 {validation['order_count_diff']}，"
                f"应收货款差异 {validation['receivable_diff']} RMB"
            )
        _emit(
            progress,
            "马帮收支校验通过："
            f"订单 {validation['detail_order_count']}，"
            f"应收货款 {validation['detail_receivable']} RMB",
        )
        return _result_payload(job.manifest_path, validation)
    except MabangIncomeValidationError:
        raise
    except Exception as exc:
        manifest = load_manifest(job.manifest_path)
        _ensure_mabang_section(manifest, job.account_name)
        for report_type in ("income_detail", "income_summary"):
            report = manifest["mabang"]["reports"][report_type]
            if report.get("status") == "running":
                report.update(
                    {
                        "status": "failed",
                        "finished_at": _now_text(),
                        "error_code": "collection_failed",
                        "error_message": str(exc),
                    }
                )
        manifest["mabang"]["status"] = "failed"
        manifest["status"] = "partial_success"
        _touch_manifest(manifest)
        write_manifest(job.manifest_path, manifest)
        raise


def discover_shopee_vietnam_shops(client: MabangClient) -> list[OrderShopInfo]:
    shops = list_order_shops(client)
    result: list[OrderShopInfo] = []
    seen: set[str] = set()
    for shop in shops:
        shop_id = str(shop.shop_id or "").strip()
        shop_name = str(shop.shop_name or "").strip()
        if not shop_id or shop_id in seen:
            continue
        if str(shop.platform_id or "").strip() != SHOPEE_PLATFORM_ID:
            continue
        if "越南" not in shop_name and not re.search(r"(?:^|[-_/\s])VN\d", shop_name, re.I):
            continue
        seen.add(shop_id)
        result.append(shop)
    return sorted(result, key=lambda item: item.shop_name.casefold())


def build_income_detail_form(
    shop_ids: list[str],
    start_date: str,
    end_date: str,
    *,
    page: int = 1,
    page_size: int = DETAIL_PAGE_SIZE,
) -> list[tuple[str, str]]:
    form = [
        ("formType", "1"),
        ("orderByField", ""),
        ("orderByValue", ""),
        ("historyType", "1"),
        ("keyColumnName", ""),
        ("keyColumnValue", ""),
        ("categoryIdType", ""),
        ("params", ""),
        ("shippingType", "shippingPreCost"),
        ("fixCategoryRangeisRefund", "inside"),
        ("paytimeTimeStart", f"{start_date} 00:00:00"),
        ("paytimeTimeEnd", f"{end_date} 23:59:59"),
        ("expresstimeTimeStart", ""),
        ("expresstimeTimeEnd", ""),
        ("createTimeStart", ""),
        ("createTimeEnd", ""),
        ("escrowReleaseTimeStart", ""),
        ("escrowReleaseTimeEnd", ""),
        ("returntimeTimeStart", ""),
        ("returntimeTimeEnd", ""),
        ("searchTextValue", ""),
        ("searchTextType", "platformOrderId"),
        ("cost_type", "1"),
        ("moneyType", MONEY_TYPE),
        ("orderFlag", "1"),
        ("page", str(max(int(page), 1))),
        ("rowsPerPage", str(max(100, min(int(page_size), DETAIL_PAGE_SIZE)))),
    ]
    form.extend(("shopIdMultiple[]", str(shop_id)) for shop_id in shop_ids if str(shop_id).strip())
    return form


def fetch_income_detail_rows(
    client: MabangClient,
    shop_ids: list[str],
    start_date: str,
    end_date: str,
    *,
    page_size: int = DETAIL_PAGE_SIZE,
    concurrency: int = DETAIL_CONCURRENCY,
    progress: ProgressCallback | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    first = _request_detail_page(client, shop_ids, start_date, end_date, 1, page_size)
    first_rows = _data_detail_rows(first.get("exportData"))
    page_count, expected_count = parse_income_pagination(str(first.get("pageHtml") or ""))
    page_count = max(page_count, 1)
    if page_count > DETAIL_MAX_PAGES:
        raise RuntimeError(f"马帮收支明细页数超过保护上限：{page_count}")
    _emit(progress, f"马帮收支明细共 {expected_count or '?'} 条，{page_count} 页")

    page_rows: dict[int, list[dict[str, Any]]] = {1: first_rows}
    if page_count > 1:
        with ThreadPoolExecutor(max_workers=max(1, min(int(concurrency), 8))) as executor:
            futures = {
                executor.submit(
                    _request_detail_page,
                    client,
                    shop_ids,
                    start_date,
                    end_date,
                    page,
                    page_size,
                ): page
                for page in range(2, page_count + 1)
            }
            for future in as_completed(futures):
                page = futures[future]
                payload = future.result()
                rows = _data_detail_rows(payload.get("exportData"))
                page_rows[page] = rows
                _emit(progress, f"马帮收支明细第 {page}/{page_count} 页完成：{len(rows)} 条")

    rows = [row for page in sorted(page_rows) for row in page_rows[page]]
    if expected_count is not None and len(rows) != expected_count:
        raise RuntimeError(f"马帮收支明细不完整：页面总数 {expected_count}，实际获取 {len(rows)}")
    return rows, {
        "row_count": len(rows),
        "expected_row_count": expected_count,
        "page_count": page_count,
        "page_size": page_size,
        "concurrency": concurrency,
    }


def _request_detail_page(
    client: MabangClient,
    shop_ids: list[str],
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
                    shop_ids,
                    start_date,
                    end_date,
                    page=page,
                    page_size=page_size,
                ),
                context=f"马帮收支明细第 {page} 页",
                referer=INCOME_REPORT_REFERER,
            )
            if not payload.get("success"):
                raise MabangApiError(f"马帮收支明细第 {page} 页失败：{payload}")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(attempt)
    raise RuntimeError(f"马帮收支明细第 {page} 页连续失败：{last_error}") from last_error


def parse_income_pagination(page_html: str) -> tuple[int, int | None]:
    text = BeautifulSoup(str(page_html or ""), "html.parser").get_text(" ", strip=True)
    page_match = re.search(r"(\d+)\s*/\s*(\d+)\s*页", text)
    count_match = re.search(r"共\s*([\d,]+)\s*条", text)
    page_count = int(page_match.group(2)) if page_match else 1
    total_count = int(count_match.group(1).replace(",", "")) if count_match else None
    return page_count, total_count


def _data_detail_rows(value: Any) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("platformOrder") or "").strip()
        and str(row.get("platformOrder") or "").strip() != "合计"
    ]


def normalize_income_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        item = {column: "" for column in ORDERED_COLUMNS}
        for key, value in row.items():
            column = FIELD_MAPPING.get(str(key))
            if column:
                item[column] = "" if value is None else value
        normalized.append(item)
    return normalized


def _validate_detail_source_fields(rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise RuntimeError("马帮 Shopee 越南收支明细为空，请确认月份和店铺范围")
    fields = {str(key) for row in rows for key in row}
    missing = [field for field in REQUIRED_DETAIL_SOURCE_FIELDS if field not in fields]
    if missing:
        raise RuntimeError(f"马帮收支明细缺少关键字段：{', '.join(missing)}")


def build_income_summary_form(
    shop_ids: list[str],
    start_date: str,
    end_date: str,
) -> list[tuple[str, str]]:
    month = str(start_date)[:7]
    form = [
        ("searchType", "shop"),
        ("moneyType", MONEY_TYPE),
        ("orderByField", ""),
        ("orderByValue", ""),
        ("fbaFlag", ""),
        ("sqlTest", ""),
        ("historyType", "1"),
        ("timeDimension", "day"),
        ("timeType", "paytime"),
        ("dayDateStart", start_date),
        ("dayDateEnd", end_date),
        ("monthDateStart", month),
        ("monthDateEnd", month),
        ("profitMargin", "grossRate"),
        ("page", "1"),
        ("rowsPerPage", "2000"),
        ("platformId[]", SHOPEE_PLATFORM_ID),
    ]
    form.extend(("shopIdMultiple[]", str(shop_id)) for shop_id in shop_ids if str(shop_id).strip())
    return form


def fetch_income_summary(
    client: MabangClient,
    shop_ids: list[str],
    start_date: str,
    end_date: str,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    payload = client.post_form_json(
        INCOME_SUMMARY_PATH,
        params=INCOME_SUMMARY_PARAMS,
        form_data=build_income_summary_form(shop_ids, start_date, end_date),
        context="马帮收支汇总",
        referer=INCOME_REPORT_REFERER,
    )
    if not payload.get("success"):
        raise MabangApiError(f"马帮收支汇总失败：{payload}")
    rows = parse_income_summary_table(str(payload.get("tableContent") or ""))
    return rows, {
        "row_count": len(rows),
        "update_time": str(payload.get("updateDateStr") or ""),
    }


def parse_income_summary_table(table_html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(str(table_html or ""), "html.parser")
    result: list[dict[str, str]] = []
    for row in soup.select("tr"):
        cells = row.select("td")
        if len(cells) < 2:
            continue
        fields = {
            str(cell.get("data-field") or ""): _clean_number(cell.get_text(" ", strip=True))
            for cell in cells
            if cell.get("data-field")
        }
        if "totalNum" not in fields or "itemTotal" not in fields:
            continue
        shop_name = " ".join(cells[1].get_text(" ", strip=True).split())
        if not shop_name or shop_name == "合计":
            continue
        result.append(
            {
                "店铺名称": shop_name,
                "订单数": fields.get("totalNum", "0"),
                "应收货款": fields.get("itemTotal", "0"),
                "收入小计": fields.get("incomeTotal", "0"),
            }
        )
    return result


def validate_income_totals(
    detail_rows: list[dict[str, Any]],
    summary_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    detail_by_store: dict[str, dict[str, Any]] = {}
    for row in detail_rows:
        shop = str(row.get("店铺名称") or "").strip()
        order = str(row.get("订单编号") or "").strip()
        if not shop or not order:
            continue
        values = detail_by_store.setdefault(shop, {"orders": set(), "receivable": Decimal("0")})
        values["orders"].add(order)
        values["receivable"] += _decimal(row.get("收入-应收货款"))

    summary_by_store: dict[str, dict[str, Any]] = {}
    for row in summary_rows:
        shop = str(row.get("店铺名称") or "").strip()
        if not shop:
            continue
        summary_by_store[shop] = {
            "order_count": int(_decimal(row.get("订单数"))),
            "receivable": _decimal(row.get("应收货款")),
        }

    mismatches: list[dict[str, Any]] = []
    for shop in sorted(set(detail_by_store) | set(summary_by_store)):
        detail = detail_by_store.get(shop) or {"orders": set(), "receivable": Decimal("0")}
        summary = summary_by_store.get(shop) or {"order_count": 0, "receivable": Decimal("0")}
        detail_count = len(detail["orders"])
        detail_amount = detail["receivable"]
        summary_count = int(summary["order_count"])
        summary_amount = summary["receivable"]
        count_diff = detail_count - summary_count
        amount_diff = detail_amount - summary_amount
        if count_diff or abs(amount_diff) > Decimal("0.01"):
            mismatches.append(
                {
                    "store_name": shop,
                    "detail_order_count": detail_count,
                    "summary_order_count": summary_count,
                    "order_count_diff": count_diff,
                    "detail_receivable": _decimal_text(detail_amount),
                    "summary_receivable": _decimal_text(summary_amount),
                    "receivable_diff": _decimal_text(amount_diff),
                }
            )

    detail_order_count = sum(len(values["orders"]) for values in detail_by_store.values())
    summary_order_count = sum(int(values["order_count"]) for values in summary_by_store.values())
    detail_receivable = sum((values["receivable"] for values in detail_by_store.values()), Decimal("0"))
    summary_receivable = sum((values["receivable"] for values in summary_by_store.values()), Decimal("0"))
    order_diff = detail_order_count - summary_order_count
    amount_diff = detail_receivable - summary_receivable
    passed = not mismatches and order_diff == 0 and abs(amount_diff) <= Decimal("0.01")
    return {
        "status": "passed" if passed else "failed",
        "currency": MONEY_TYPE,
        "date_type": "paytime",
        "checked_at": _now_text(),
        "detail_row_count": len(detail_rows),
        "detail_order_count": detail_order_count,
        "summary_order_count": summary_order_count,
        "detail_receivable": _decimal_text(detail_receivable),
        "summary_receivable": _decimal_text(summary_receivable),
        "order_count_diff": order_diff,
        "receivable_diff": _decimal_text(amount_diff),
        "store_mismatches": mismatches,
    }


def _ziniao_sources_ready(manifest_path: Path, manifest: dict[str, Any]) -> bool:
    stores = manifest.get("stores") or []
    if not stores:
        return False
    return all(
        report_is_completed(manifest_path, (store.get("reports") or {}).get(report_type))
        for store in stores
        for report_type in REPORT_TYPES
    )


def _ensure_mabang_section(manifest: dict[str, Any], account_name: str = "") -> dict[str, Any]:
    mabang = manifest.setdefault("mabang", {})
    mabang.setdefault("status", "pending")
    if account_name:
        mabang["account_name"] = account_name
    mabang.setdefault("shop_snapshot_file", "")
    mabang.setdefault("shop_count", 0)
    reports = mabang.setdefault("reports", {})
    for report_type in MABANG_REPORT_TYPES:
        reports.setdefault(report_type, _empty_report())
    mabang.setdefault("validation", _empty_validation("pending"))
    return mabang


def _empty_report() -> dict[str, Any]:
    return {
        "status": "pending",
        "file": "",
        "raw_file": "",
        "row_count": None,
        "retry_count": 0,
        "started_at": "",
        "finished_at": "",
        "error_code": "",
        "error_message": "",
    }


def _empty_validation(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "file": "",
        "order_count_diff": None,
        "receivable_diff": "",
        "store_mismatches": [],
    }


def _set_report_running(manifest: dict[str, Any], report_type: str) -> None:
    report = manifest["mabang"]["reports"][report_type]
    retry_count = int(report.get("retry_count") or 0)
    if report.get("status") in {"failed", "running"}:
        retry_count += 1
    report.update(
        {
            "status": "running",
            "started_at": _now_text(),
            "finished_at": "",
            "error_code": "",
            "error_message": "",
            "retry_count": retry_count,
        }
    )


def _set_report_success(manifest_path: Path, report_type: str, **values: Any) -> None:
    manifest = load_manifest(manifest_path)
    _ensure_mabang_section(manifest)
    report = manifest["mabang"]["reports"][report_type]
    report.update(
        {
            "status": "success",
            "finished_at": _now_text(),
            "error_code": "",
            "error_message": "",
            **values,
        }
    )
    _touch_manifest(manifest)
    write_manifest(manifest_path, manifest)


def _mabang_income_completed(manifest_path: Path, manifest: dict[str, Any]) -> bool:
    mabang = _ensure_mabang_section(manifest)
    reports = mabang["reports"]
    return (
        report_is_completed(manifest_path, reports.get("income_detail"))
        and report_is_completed(manifest_path, reports.get("income_summary"))
        and str((mabang.get("validation") or {}).get("status") or "") == "passed"
    )


def _result_payload(
    manifest_path: Path,
    validation: dict[str, Any],
    *,
    skipped_count: int = 0,
) -> dict[str, Any]:
    return {
        "manifest_path": str(manifest_path),
        "run_dir": str(manifest_path.parent),
        "validation_passed": str(validation.get("status") or "") == "passed",
        "detail_row_count": int(validation.get("detail_row_count") or 0),
        "detail_order_count": int(validation.get("detail_order_count") or 0),
        "summary_order_count": int(validation.get("summary_order_count") or 0),
        "detail_receivable": str(validation.get("detail_receivable") or "0"),
        "summary_receivable": str(validation.get("summary_receivable") or "0"),
        "order_count_diff": int(validation.get("order_count_diff") or 0),
        "receivable_diff": str(validation.get("receivable_diff") or "0"),
        "skipped_count": skipped_count,
        "failed_count": 0,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _clean_number(value: Any) -> str:
    text = str(value or "").strip().replace(",", "")
    return "0" if text in {"", "-", "--"} else text


def _decimal(value: Any) -> Decimal:
    text = _clean_number(value)
    text = re.sub(r"[^0-9.\-]", "", text)
    try:
        return Decimal(text or "0")
    except InvalidOperation:
        return Decimal("0")


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _relative(run_dir: Path, path: Path) -> str:
    return str(path.relative_to(run_dir))


def _compact(value: str) -> str:
    return str(value or "").replace("-", "")


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _touch_manifest(manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = _now_text()


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)
