from __future__ import annotations

from contextlib import contextmanager

import json
import os
import shutil
import threading
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.config_store import AppSettings, BoundAccount, ConfigStore, default_date_range, ensure_output_directory
from backend.output_paths import directory_scope_key, resolve_output_directories
from backend.sidebar_preferences import SidebarPreferencesStore
from backend.services import ph_cancel_refund_sync
from backend.services.kec_reconciliation import (
    inspect_kec_source_workbook,
    run_kec_reconciliation,
    validate_kec_reconciliation_payload,
)
from backend.services.group_sales_report import (
    get_group_options,
    run_group_sales_report,
    validate_group_sales_payload,
)
from backend.services.warehouse_sales_export import (
    default_month_range as warehouse_export_default_months,
    fetch_report_options as fetch_warehouse_report_options,
    run_warehouse_sales_export,
    validate_export_payload as validate_warehouse_export_payload,
)
from backend.services.developer_sales_income_summary import (
    run_developer_sales_income_summary,
    validate_developer_sales_income_payload,
)
from backend.services.mabang_income_expense_report import (
    get_category_options as get_income_expense_category_options,
    get_warehouse_options as get_income_expense_warehouse_options,
    previous_month_date_range as income_expense_default_period,
    run_income_expense_report,
    validate_income_expense_payload,
)
from backend.services.mabang_arrival_query import (
    run_mabang_arrival_query,
    validate_arrival_query_payload,
)
from backend.services.mabang_order_sku_export import (
    DEFAULT_FIELDS as ORDER_SKU_DEFAULT_FIELDS,
    FIELD_DEFINITIONS as ORDER_SKU_FIELD_DEFINITIONS,
    parse_order_number_file as parse_mabang_order_number_file,
    run_mabang_order_sku_export,
    validate_order_sku_export_payload,
)
from backend.services.lz_warehouse_inventory import (
    TARGET_WAREHOUSE_NAMES as LZ_INVENTORY_WAREHOUSES,
    fetch_lz_inventory_warehouses,
    run_lz_inventory_export,
    validate_lz_inventory_payload,
)
from backend.services.captcha_service import query_ttshitu_account_info
from backend.services.purchase_log import run_purchase_log_query, validate_query_payload
from backend.services.sku_inventory_query import (
    PREVIEW_ROW_LIMIT as SKU_INVENTORY_PREVIEW_ROW_LIMIT,
    fetch_developer_options as fetch_sku_inventory_developers,
    preview_rows as sku_inventory_preview_rows,
    run_sku_inventory_query,
    validate_query_payload as validate_sku_inventory_payload,
)
from backend.services.mabang_warehouse_permission import (
    build_batch_job,
    load_mabang_warehouse_options,
    parse_employee_names,
    parse_permission_types,
    parse_warehouse_ids,
    preview_mabang_warehouse_permissions,
    run_mabang_warehouse_permission_batch,
    validate_account_payload as validate_mabang_permission_account,
    validate_preview_payload as validate_mabang_permission_preview,
)
from backend.services.mabang_developer_permission import (
    build_batch_job as build_developer_permission_batch_job,
    preview_mabang_developer_permissions,
    run_mabang_developer_permission_batch,
    validate_preview_payload as validate_mabang_developer_preview,
)
from backend.services.temu_shipping_workbook import read_temu_shipping_workbook
from backend.services.temu_shipping_channel import (
    TemuShippingRunStore,
    account_key as temu_shipping_account_key,
    preview_temu_shipping,
    public_result as temu_shipping_public_result,
    run_temu_shipping_batch,
)
from backend.services.bigseller_sync import run_bigseller_sync, validate_bigseller_sync_payload
from backend.services.bigseller_item_id_query import (
    run_bigseller_item_id_query,
    validate_bigseller_item_id_query_payload,
)
from backend.services.bigseller_benchmark_workbook import inspect_workbook as inspect_benchmark_workbook
from backend.services.bigseller_sku_benchmark import (
    run_bigseller_sku_benchmark,
    validate_bigseller_benchmark_payload,
)
from backend.services.bigseller_claim_workbook import inspect_claim_workbook
from backend.services.bigseller_claim_query import (
    run_bigseller_claim_query,
    validate_bigseller_claim_payload,
)
from backend.services.bigseller_link_query import (
    run_bigseller_link_query,
    validate_bigseller_link_query_payload,
)
from backend.services.echotik_collector import (
    get_echotik_filter_options,
    run_echotik_collection,
    validate_echotik_payload,
)
from backend.services.shopee_ads_recharge import (
    get_site_options_list,
    resolve_runtime_paths,
    run_shopee_ads_recharge,
    validate_shopee_ads_payload,
)
from backend.services.lazada_withdrawal_statistics import (
    country_options as lazada_withdrawal_country_options,
    previous_month_date_range as lazada_withdrawal_default_period,
    resolve_lazada_runtime_paths as resolve_lazada_withdrawal_runtime_paths,
    run_lazada_withdrawal_statistics,
    validate_lazada_withdrawal_payload,
)
from backend.services.lazada_monthly_report import (
    country_options as lazada_monthly_report_country_options,
    previous_month_value as lazada_monthly_report_default_month,
    resolve_lazada_runtime_paths as resolve_lazada_monthly_report_runtime_paths,
    run_lazada_monthly_report,
    validate_lazada_monthly_report_payload,
)
from backend.services import lazada_ads_data
from backend.services import lazada_bill_detail
from backend.services import download_lazada_campaign, lazada_campaign_workbook
from backend.services.vietnam_income_collection import (
    create_collection_run,
    default_collection_period,
    load_manifest,
    report_options,
    validate_collection_payload,
)
from backend.services.vietnam_income_subsidy import (
    run_subsidy_collection,
    validate_subsidy_collection_payload,
)
from backend.services.vietnam_income_sources import (
    run_ziniao_sources_collection,
    validate_ziniao_sources_collection_payload,
)
from backend.services.vietnam_mabang_income import (
    run_vietnam_mabang_income_collection,
    validate_vietnam_mabang_income_payload,
)
from backend.services.vietnam_mabang_refunds import (
    run_vietnam_mabang_refund_collection,
    validate_vietnam_mabang_refund_payload,
)
from backend.services.vietnam_final_reconciliation import (
    run_vietnam_final_reconciliation,
    validate_vietnam_final_reconciliation_payload,
)
from backend.build_profile import annotate_app_info, requires_internal_runtime
from backend.task_manager import TaskManager
from backend.core.ziniao_browser import ZiniaoBrowser, account_profile as ziniao_account_profile, runtime_paths as ziniao_runtime_paths
from backend.services import temu_on_sale_export as temu_export
from backend.services import temu_fee_refund_export as temu_fee_refund
from backend.services import temu_activity_export as activity_export
from backend.services import temu_activity_page as activity_page
from backend.services import temu_financial_export as financial_export
from backend.services.temu_fee_refund_gateway import TemuFeeRefundGateway
from backend.services.temu_financial_gateway import TemuFinancialGateway
from backend.services import balance_statistics, tax_declaration_service
from backend.services import lazada_ads_monthly
from backend.services.lazada_ads_monthly_cli import ZiniaoMonthlyCli
from backend.services.temu_on_sale_gateway import TemuOnSaleGateway
from backend.services import shopee_id_monthly_invoice
from backend.services.shopee_id_invoice_gateway import ShopeeIdInvoiceGateway
from backend.update_service import (
    calculate_sha256,
    check_update_manifest,
    download_update_package,
    result_payload,
)


APP_VERSION = "1.0.12"
APP_NAME = "寰球云联自动化平台"


class AppBridge:
    """Methods exposed to the H5 UI through pywebview."""

    def __init__(self) -> None:
        self.config_store = ConfigStore()
        self.tasks = TaskManager()
        self.update_tasks = TaskManager()
        self._echotik_filter_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._mabang_permission_previews: dict[str, dict[str, Any]] = {}
        self._mabang_permission_preview_lock = threading.RLock()
        self._mabang_developer_previews: dict[str, dict[str, Any]] = {}
        self._mabang_developer_preview_lock = threading.RLock()
        self._cancel_refund_preview: dict[str, Any] | None = None
        self._cancel_refund_lock = threading.Lock()
        self._temu_operation_lock = threading.Lock()
        self._temu_running_accounts: set[str] = set()
        self._temu_export_lock = threading.RLock()
        self._temu_export_active = False
        self._temu_export_catalog: dict[str, Any] = {}
        self._temu_export_previews: dict[str, dict[str, Any]] = {}
        self._balance_statistics_lock = threading.RLock()
        self._balance_statistics_active = False
        self._lazada_ads_monthly_lock = threading.RLock()
        self._lazada_ads_monthly_active = False
        self._shopee_id_invoice_lock = threading.RLock()
        self._shopee_id_invoice_active = False
        self._shopee_id_invoice_previews: dict[str, dict[str, Any]] = {}
        self._campaign_operation_lock = threading.Lock()
        self._campaign_download_start_lock = threading.Lock()

    def get_sidebar_preferences(self) -> dict[str, Any]:
        try:
            preferences = SidebarPreferencesStore(self.config_store.config_dir / "sidebar.json").load()
            return {"ok": True, "preferences": preferences}
        except Exception:
            return {"ok": False, "error": "读取侧栏偏好失败，请稍后重试"}

    def save_sidebar_preferences(self, preferences: Any) -> dict[str, Any]:
        try:
            saved = SidebarPreferencesStore(self.config_store.config_dir / "sidebar.json").save(preferences)
            return {"ok": True, "preferences": saved}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "保存侧栏偏好失败，请稍后重试"}

    def get_app_info(self) -> dict[str, Any]:
        settings = self.config_store.load()
        return annotate_app_info({
            "ok": True,
            "app": {
                "name": APP_NAME,
                "version": APP_VERSION,
            },
            "date_range": default_date_range(),
            "settings": _settings_payload(settings),
            "account_state": _account_state_payload(settings),
            "ph_cancel_refund_sync": resolve_output_directories(settings, ph_cancel_refund_sync.TOOL),
            "group_sales_report": {
                "groups": get_group_options(),
            },
            "mabang_income_expense_report": {
                "categories": get_income_expense_category_options(),
                "warehouses": get_income_expense_warehouse_options(),
                "date_range": income_expense_default_period(),
            },
            "warehouse_sales_export": {
                "month_range": warehouse_export_default_months(),
                "categories": [],
                "warehouses": [],
                "output_dir": resolve_output_directories(settings, "warehouse_sales_export")["output_dir"],
            },
            "tools": [
                {
                    "key": "purchase_log",
                    "name": "SKU 采购日志查询",
                    "description": "按 SKU 查询马帮采购日志并导出 Excel。",
                    "status": "sample",
                    "vendor": "mabang",
                },
                {
                    "key": "mabang_arrival_query",
                    "name": "到货查询与导出",
                    "description": "批量按备注查询调拨批次并导出到货明细 Excel。",
                    "status": "ready",
                    "vendor": "mabang",
                },
                {
                    "key": "mabang_order_sku_export",
                    "name": "订单批量查询与 SKU 汇总",
                    "description": "导入订单编号，按编号批量查询订单并汇总唯一 SKU 清单。",
                    "status": "ready",
                    "vendor": "mabang",
                },
                {
                    "key": "sku_inventory_query",
                    "name": "SKU库存与可售天数查询",
                    "description": "按开发人员和SKU创建时间查询所有仓库有库存、未发货或在途的明细。",
                    "status": "ready",
                    "vendor": "mabang",
                },
                {
                    "key": "lz_warehouse_inventory",
                    "name": "lz各仓实时库存",
                    "description": "多选账号有权限的仓库，每仓独立导出 Excel，仓内全部分页自动合并。",
                    "status": "ready",
                    "vendor": "mabang",
                },
                {
                    "key": "developer_sales_income_summary",
                    "name": "开发与销售收入汇总导出",
                    "description": "按付款日期和开发员名单汇总开发、销售收入订单金额（美元）。",
                    "status": "ready",
                    "vendor": "mabang",
                },
                {
                    "key": "ph_group_sales_report",
                    "name": "菲律宾各组商品销量报表",
                    "description": "按小组和时间范围导出马帮商品销量报表。",
                    "status": "ready",
                    "vendor": "mabang",
                },
                {
                    "key": "warehouse_sales_export",
                    "name": "商品仓库报表按月导出",
                    "description": "多选自定义分类与仓库，按月份导出商品仓库报表，每月一个工作表合并成一张表。",
                    "status": "ready",
                    "vendor": "mabang",
                },
                {
                    "key": ph_cancel_refund_sync.TOOL,
                    "name": ph_cancel_refund_sync.TITLE,
                    "description": "预览菲律宾 Shopee 取消订单，确认后同步订单信息并复查。",
                    "status": "ready",
                    "vendor": "mabang",
                },
                {
                    "key": "shopee_ads",
                    "name": "Shopee 广告充值",
                    "description": "多站点店铺广告充值，支持印尼、泰国、菲律宾、越南、马来。",
                    "status": "ready",
                    "vendor": "ziniao",
                },
                {
                    "key": "shopee_id_monthly_invoice",
                    "name": "Shopee 印尼月度账单与税票下载",
                    "description": "按月下载 Released 收入 Excel 与全部服务费账单、税票 PDF，优先复用导出历史。",
                    "status": "ready",
                    "vendor": "ziniao",
                },
                {
                    "key": "lazada_withdrawal_statistics",
                    "name": "Lazada 提现统计",
                    "description": "菲律宾、马来提现流水 Excel 及泰国收入账单归档。",
                    "status": "ready",
                    "vendor": "ziniao",
                },
                {
                    "key": "lazada_ads_monthly",
                    "name": "Lazada 广告月报",
                    "description": "按完整自然月采集广告、联盟及优惠券金额，导出带原始截图的报表。",
                    "status": "ready",
                    "vendor": "ziniao",
                },
                {
                    "key": "lazada_monthly_report",
                    "name": "Lazada 月度账单下载",
                    "description": "按月份下载泰国、马来西亚和菲律宾店铺的月度报告。",
                    "status": "ready",
                    "vendor": "ziniao",
                },
                {
                    "key": "lazada_ads_data",
                    "name": "泰国 Lazada 广告数据",
                    "description": "按指定日期采集店铺广告费和业绩，写入指定钉钉 Sheet。",
                    "status": "ready",
                    "vendor": "ziniao",
                },
                {
                    "key": "lazada_bill_detail",
                    "name": "Lazada 后台收支数据",
                    "description": "按国家和账单区间采集总金额、收入与扣减项，写入指定钉钉 Sheet 并保存截图。",
                    "status": "ready",
                    "vendor": "ziniao",
                },
                {
                    "key": "lazada_campaign_download",
                    "name": "Lazada 活动模板下载",
                    "description": "按店铺下载子活动模板，全部下载成功后按最低折扣率筛选。",
                    "status": "ready",
                    "vendor": "ziniao",
                },
                {
                    "key": "lazada_campaign_workbook",
                    "name": "Excel 折扣筛选",
                    "description": "批量导入 Excel，按最低折扣率筛选并导出表格和异常明细。",
                    "status": "ready",
                    "vendor": "local",
                },
                {
                    "key": "temu_fee_refund_export",
                    "name": "TEMU 面单费及退货退款导出",
                    "description": "按店铺和日期导出发货面单费、退货面单费及已退款售后单。",
                    "status": "ready",
                    "vendor": "ziniao",
                },
                {
                    "key": "temu_financial_export",
                    "name": "TEMU 财务报表导出",
                    "description": "按店铺和日期导出全球财务明细，保留原始文件并合并全部工作表。",
                    "status": "ready",
                    "vendor": "ziniao",
                },
                {
                    "key": "temu_on_sale_export",
                    "name": "TEMU 在售商品导出",
                    "description": "自定义选店，保留原始文件并汇总全部在售 SKU，支持失败重试。",
                    "status": "sample",
                    "vendor": "ziniao",
                },
                {
                    "key": "temu_activity_export",
                    "name": "TEMU 活动报名导出",
                    "description": "按店铺批量导出活动报名模板，核对站点、活动范围与报价行数。",
                    "status": "ready",
                    "vendor": "ziniao",
                },
                {
                    "key": "temu_balance_statistics",
                    "name": "TEMU 余额统计",
                    "description": "按所选月份采集待处理款项，保存当前账户总金额和页面截图。",
                    "status": "ready",
                    "vendor": "ziniao",
                },
                {
                    "key": "lazada_balance_statistics",
                    "name": "Lazada 余额统计",
                    "description": "多站点采集当前 Income、Balance、Ads 与处理中提现，并保存截图。",
                    "status": "ready",
                    "vendor": "ziniao",
                },
                {
                    "key": "bigseller_sync",
                    "name": "BigSeller 同步",
                    "description": "通过 BigSeller 接口同步产品或库存，支持在售、售完状态。",
                    "status": "ready",
                    "vendor": "bigseller",
                },
                {
                    "key": "bigseller_item_id_query",
                    "name": "BS 商品ID查询",
                    "description": "批量按 SKU（含子SKU）模糊搜索，取 Views 降序首条的 Item ID 并导出 Excel。",
                    "status": "ready",
                    "vendor": "bigseller",
                },
                {
                    "key": "bigseller_sku_benchmark",
                    "name": "滞销SKU爆款对标",
                    "description": "读取 Excel 中的 SKU，按浏览量或销量对标第一店铺，并保留逐行查询状态。",
                    "status": "ready",
                    "vendor": "bigseller",
                },
                {
                    "key": "bigseller_claim_query",
                    "name": "新品认领时间查询",
                    "description": "导入 Excel，按主 SKU 查询 Shopee 在售商品，回填最早创建商品的店铺、创建时间和平台创建时间。",
                    "status": "ready",
                    "vendor": "bigseller",
                },
                {
                    "key": "bigseller_link_query",
                    "name": "BS 抓取链接",
                    "description": "批量输入 SKU，在售优先、无匹配再查下架，查询最早上架商品的店铺和链接。",
                    "status": "ready",
                    "vendor": "bigseller",
                },
                {
                    "key": "sample_registration",
                    "name": "网红寄样登记",
                    "description": "查询样品订单、导出 Excel、同步钉钉在线表格。",
                    "status": "ready",
                    "vendor": "mabang",
                },
                {
                    "key": "mabang_income_expense_report",
                    "name": "马帮收支报表",
                    "description": "按店铺自定义分类、海外仓和发货时间导出收支明细 CSV。",
                    "status": "ready",
                    "vendor": "mabang",
                },
                {
                    "key": "mabang_warehouse_permission",
                    "name": "马帮仓库权限批量开通",
                    "description": "批量匹配员工并新增指定仓库的数据权限。",
                    "status": "ready",
                    "vendor": "mabang",
                },
                {
                    "key": "mabang_developer_permission",
                    "name": "马帮批量添加开发员",
                    "description": "批量追加开发员岗位，并设置按商品父目录查看。",
                    "status": "ready",
                    "vendor": "mabang",
                },
                {
                    "key": "temu_shipping_channel",
                    "name": "TEMU发货渠道更改",
                    "description": "导入 Excel，按顺序设置已开启的 TEMU 渠道长宽高和申报重量。",
                    "status": "ready",
                    "vendor": "mabang",
                },
                {
                    "key": "kec_reconciliation",
                    "name": "KEC 对账",
                    "description": "核对 KEC 账单的仓储费、卸货费、出库耗材、退件上架费与杂费，回查马帮订单并导出结果。",
                    "status": "ready",
                    "vendor": "mabang",
                },
                {
                    "key": "tax_declaration",
                    "name": "电子税务局申报",
                    "description": "批量导出申报记录，或在确认后执行正式申报并导出。",
                    "status": "ready",
                    "vendor": "local",
                },
                {
                    "key": "echotik_collect",
                    "name": "EchoTik 商品达人采集",
                    "description": "固定泰国站，按关键词采集商品库和商品达人列表。",
                    "status": "ready",
                    "vendor": "echotik",
                },
            ],
        })

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            updates = dict(payload or {})
            if "output_dir" in updates:
                updates["output_dir"] = str(ensure_output_directory(updates["output_dir"]))
            settings = self.config_store.save(updates)
            return {"ok": True, "settings": _settings_payload(settings)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def query_captcha_balance(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request_payload = dict(payload or {})
        settings = self.config_store.load()
        username = str(request_payload.get("username") or settings.captcha_username or "").strip()
        password = str(request_payload.get("password") or settings.captcha_password or "")
        try:
            data = query_ttshitu_account_info(username, password)
            return {"ok": True, "data": data}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def add_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        vendor = str((payload or {}).get("vendor") or "").strip().lower()
        name = str((payload or {}).get("name") or "").strip()
        username = str((payload or {}).get("username") or "").strip()
        password = str((payload or {}).get("password") or "")
        extra_raw = (payload or {}).get("extra")
        extra = extra_raw if isinstance(extra_raw, dict) else {}

        allowed_vendors = {"mabang", "ziniao", "bigseller", "echotik"}
        if vendor not in allowed_vendors:
            return {"ok": False, "error": f"不支持的账号类型：{vendor}"}
        if not username:
            label = _vendor_label(vendor) + "账号"
            return {"ok": False, "error": f"请输入{label}"}
        if not password:
            label = _vendor_label(vendor) + "密码"
            return {"ok": False, "error": f"请输入{label}"}
        if vendor == "ziniao":
            company = str(extra.get("company") or "").strip()
            if not company:
                return {"ok": False, "error": "请输入紫鸟公司名"}
            extra = {"company": company}
        else:
            extra = {}

        settings = self.config_store.load()
        if any(
            account.vendor == vendor and account.username.casefold() == username.casefold()
            for account in settings.accounts
        ):
            label = _vendor_label(vendor)
            return {"ok": False, "error": f"该{label}账号已经绑定，请直接选择使用"}

        account = BoundAccount(
            id=uuid4().hex,
            vendor=vendor,
            name=name or str(extra.get("company") or "").strip() or username,
            username=username,
            password=password,
            extra=extra,
        )
        settings.accounts.append(account)
        settings.active_account_ids[vendor] = account.id
        saved = self.config_store.save(settings)
        return {"ok": True, "account_state": _account_state_payload(saved)}

    def delete_account(self, account_id: str) -> dict[str, Any]:
        target_id = str(account_id or "").strip()
        settings = self.config_store.load()
        target = next((account for account in settings.accounts if account.id == target_id), None)
        if target is None:
            return {"ok": False, "error": "账号不存在或已被删除"}
        settings.accounts = [account for account in settings.accounts if account.id != target_id]
        if settings.active_account_ids.get(target.vendor) == target_id:
            settings.active_account_ids.pop(target.vendor, None)
        saved = self.config_store.save(settings)
        return {"ok": True, "account_state": _account_state_payload(saved)}

    def select_account(self, vendor: str, account_id: str) -> dict[str, Any]:
        target_vendor = str(vendor or "").strip().lower()
        target_id = str(account_id or "").strip()
        settings = self.config_store.load()
        if not any(
            account.id == target_id and account.vendor == target_vendor
            for account in settings.accounts
        ):
            return {"ok": False, "error": "所选账号不存在"}
        settings.active_account_ids[target_vendor] = target_id
        saved = self.config_store.save(settings)
        return {"ok": True, "account_state": _account_state_payload(saved)}

    def choose_output_dir(self, current_dir: str = "", remember: bool = True) -> dict[str, Any]:
        try:
            import webview

            directory = current_dir or self.config_store.load().output_dir
            # A typed directory may not exist yet; start the picker at its nearest existing parent.
            candidate = Path(directory).expanduser()
            while not candidate.is_dir() and candidate != candidate.parent:
                candidate = candidate.parent
            result = webview.windows[0].create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=str(candidate) if candidate.is_dir() else None,
            )
            if not result:
                return {"ok": False, "cancelled": True}
            selected = result[0] if isinstance(result, (list, tuple)) else result
            if not remember:
                return {"ok": True, "path": str(selected)}
            saved = self.save_settings({"output_dir": str(selected)})
            if not saved["ok"]:
                return saved
            return {"ok": True, "path": saved["settings"]["output_dir"], "settings": saved["settings"]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def save_scoped_output_dirs(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if not isinstance(payload, dict):
                raise ValueError("保存内容必须是对象")
            tool = str(payload.get("tool") or "").strip()
            country = str(payload.get("country") or "TH").strip()
            updates = {}
            # Validate every requested field before writing any preference.
            directory_scope_key(tool, country)
            for field, kind in (("output_dir", "output"), ("screenshot_root", "screenshot")):
                if field in payload:
                    key = directory_scope_key(tool, country, kind)
                    value = payload.get(field)
                    if value is not None and not isinstance(value, str):
                        raise ValueError("目录必须是文本路径")
                    updates[key] = str(value or "").strip().strip('"')
            settings = self.config_store.save({"tool_output_dirs": updates}) if updates else self.config_store.load()
            return {"ok": True, **resolve_output_directories(settings, tool, country)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def choose_scoped_output_dir(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if not isinstance(payload, dict):
                raise ValueError("目录选择参数必须是对象")
            # Capture the scope before the native dialog opens.
            tool = str(payload.get("tool") or "").strip()
            country = str(payload.get("country") or "TH").strip()
            kind = str(payload.get("kind") or "output").strip()
            directory_scope_key(tool, country, kind)
            field = "screenshot_root" if kind == "screenshot" else "output_dir"
            directories = resolve_output_directories(self.config_store.load(), tool, country)
            current_dir = str(payload.get("current_dir") or directories[field]).strip()
            import webview

            result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG, directory=current_dir or None)
            if not result:
                return {"ok": False, "cancelled": True}
            selected = result[0] if isinstance(result, (list, tuple)) else result
            saved = self.save_scoped_output_dirs({"tool": tool, "country": country, field: str(selected)})
            return {**saved, "path": str(selected)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_purchase_log_query(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            job = validate_query_payload(request_payload)
            self.config_store.save(
                {
                    "output_dir": str(job.output_dir),
                    "rows_per_page": job.rows_per_page,
                }
            )
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            result = run_purchase_log_query(job, progress)
            return {
                "record_count": result.record_count,
                "sku_count": result.sku_count,
                "output_file": str(result.output_file),
                "output_dir": str(result.output_file.parent),
            }

        return self.tasks.start("SKU 采购日志查询", runner, tool="purchase_log")

    def start_mabang_arrival_query(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            job = validate_arrival_query_payload(request_payload)
            self.config_store.save({"output_dir": str(job.output_dir)})
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            result = run_mabang_arrival_query(job, progress)
            return {
                "remark_count": result.remark_count,
                "matched_remark_count": result.matched_remark_count,
                "matched_batch_count": result.matched_batch_count,
                "exported_batch_count": result.exported_batch_count,
                "unmatched_remarks": list(result.unmatched_remarks),
                "output_file": str(result.output_files[0]) if result.output_files else "",
                "output_files": [str(path) for path in result.output_files],
                "file_count": len(result.output_files),
                "output_dir": str(result.output_dir),
                "output_root": str(result.output_root),
                "exports": [
                    {
                        "remark": item.remark,
                        "matched_batch_count": item.matched_batch_count,
                        "output_file": str(item.output_file),
                    }
                    for item in result.exports
                ],
            }

        return self.tasks.start(
            "马帮到货查询与导出",
            runner,
            tool="mabang_arrival_query",
            context={
                "account_id": str(request_payload.get("account_id") or ""),
                "remark_count": len(job.remarks),
                "output_root": str(job.output_dir),
            },
        )

    # ------------------------------------------------------------------
    # 马帮：订单批量查询与 SKU 汇总
    # ------------------------------------------------------------------

    def get_mabang_order_sku_export_info(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            settings = self.config_store.load()
            account_id = str((payload or {}).get("account_id") or settings.active_account_ids.get("mabang") or "").strip()
            account = next(
                (item for item in settings.accounts if item.vendor == "mabang" and item.id == account_id),
                None,
            )
            return {
                "ok": True,
                "account_id": account.id if account else "",
                "account_name": account.name if account else "",
                "fields": [
                    {"key": key, "label": label, "default": default}
                    for key, label, default in ORDER_SKU_FIELD_DEFINITIONS
                ],
                "default_fields": list(ORDER_SKU_DEFAULT_FIELDS),
                "output_dir": resolve_output_directories(settings, "mabang_order_sku_export")["output_dir"],
                "max_batch": 2000,
                "max_total": 50000,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def choose_mabang_order_file(self) -> dict[str, Any]:
        try:
            import webview

            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=(
                    "订单号文件 (*.xlsx;*.xlsm;*.xls)",
                    "文本文件 (*.csv;*.txt)",
                    "所有文件 (*.*)",
                ),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            selected = result[0] if isinstance(result, (list, tuple)) else result
            return self.inspect_mabang_order_file(str(selected))
        except Exception:
            return {"ok": False, "error": "打开文件选择器失败，请直接粘贴订单编号"}

    def inspect_mabang_order_file(self, path: str = "") -> dict[str, Any]:
        try:
            target = str(path or "").strip().strip('"')
            if not target:
                return {"ok": False, "error": "请先选择订单编号文件"}
            preview = parse_mabang_order_number_file(target)
            numbers = list(preview.numbers)
            return {
                "ok": True,
                "path": preview.source_file,
                "name": Path(preview.source_file).name,
                "sheet_name": preview.sheet_name,
                "column_name": preview.column_name,
                "order_count": len(numbers),
                "raw_row_count": preview.raw_row_count,
                "duplicate_count": preview.duplicate_count,
                "invalid_count": preview.invalid_count,
                "header_detected": preview.header_detected,
                "preview": numbers[:200],
                "preview_truncated": len(numbers) > 200,
            }
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "读取订单编号文件失败，请确认文件未加密且格式正确"}

    def start_mabang_order_sku_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            directories = resolve_output_directories(self.config_store.load(), "mabang_order_sku_export")
            if not str(request_payload.get("output_dir") or "").strip():
                request_payload["output_dir"] = directories["output_dir"]
            job = validate_order_sku_export_payload(request_payload)
            self.config_store.save(
                {"tool_output_dirs": {directory_scope_key("mabang_order_sku_export"): str(job.output_dir)}}
            )
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "读取参数或订单编号失败，请检查文件和账号配置"}

        def runner(progress):
            result = run_mabang_order_sku_export(job, progress)
            unique_skus = list(result.unique_skus)
            not_found = list(result.not_found_orders)
            preview_skus = unique_skus[:200]
            return {
                "input_order_count": result.input_order_count,
                "matched_order_count": result.matched_order_count,
                "matched_order_numbers": list(result.matched_order_numbers[:200]),
                "not_found_count": len(not_found),
                "not_found_orders": not_found[:200],
                "detail_row_count": result.detail_row_count,
                "unique_sku_count": result.unique_sku_count,
                "unique_skus": [
                    {
                        "sku": item.sku,
                        "product_name": item.product_name,
                        "order_count": item.order_count,
                        "total_quantity": item.total_quantity,
                        "order_numbers": " / ".join(item.order_numbers[:10]),
                    }
                    for item in preview_skus
                ],
                "unique_sku_truncated": len(unique_skus) > len(preview_skus),
                "preview_rows": list(result.preview_rows),
                "output_file": str(result.output_file),
                "output_dir": str(result.output_dir),
            }

        return self.tasks.start(
            "马帮订单SKU汇总",
            runner,
            tool="mabang_order_sku_export",
            context={
                "account_id": str(request_payload.get("account_id") or ""),
                "order_count": len(job.order_numbers),
                "source_file": str(request_payload.get("source_file") or ""),
                "output_dir": str(job.output_dir),
            },
        )

    def _lz_inventory_account(self, payload: dict[str, Any] | None = None) -> BoundAccount:
        settings = self.config_store.load()
        request = payload or {}
        raw_account_id = request.get("account_id") if "account_id" in request else settings.active_account_ids.get("mabang")
        account_id = raw_account_id.strip() if isinstance(raw_account_id, str) else ""
        account = next(
            (item for item in settings.accounts
             if account_id and item.vendor == "mabang" and item.id == account_id),
            None,
        )
        if account is None:
            raise AccountRequiredError("mabang", "请先绑定并选择马帮账号")
        return account

    def get_lz_warehouse_inventory_info(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            account = self._lz_inventory_account(payload)
            return {
                "ok": True,
                "account_id": account.id,
                "account_name": account.name,
                "warehouses": list(LZ_INVENTORY_WAREHOUSES),
                **resolve_output_directories(self.config_store.load(), "lz_warehouse_inventory"),
            }
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_lz_warehouse_inventory(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            account = self._lz_inventory_account(payload)
            request_payload = {
                **dict(payload or {}),
                "account_id": account.id,
                "username": account.username,
                "password": account.password,
            }
            directories = resolve_output_directories(self.config_store.load(), "lz_warehouse_inventory")
            if not str(request_payload.get("output_dir") or "").strip():
                request_payload["output_dir"] = directories["output_dir"]
            job = validate_lz_inventory_payload(request_payload)
            self.config_store.save({"tool_output_dirs": {directory_scope_key("lz_warehouse_inventory"): str(job.output_dir)}})
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            result = run_lz_inventory_export(job, progress)
            rows = list(result.preview_rows[:100])
            return {
                "output_file": str(result.output_file) if result.output_file else "",
                "output_files": [str(path) for path in result.output_files],
                "output_dir": str(job.output_dir),
                "output_root": str(job.output_dir),
                "record_count": result.record_count,
                "source_record_count": result.source_record_count,
                "export_page_count": result.export_page_count,
                "warehouse_counts": dict(result.warehouse_counts),
                "warehouse_count": len(result.warehouse_counts),
                "selected_warehouse_count": len(job.warehouse_names),
                "preview_columns": list(result.preview_columns),
                "rows": rows,
                "preview_limit": 100,
                "preview_truncated": result.record_count > len(rows),
                "exports": [
                    {
                        "warehouse_name": item.warehouse_name,
                        "output_file": str(item.output_file),
                        "record_count": item.record_count,
                        "source_record_count": item.source_record_count,
                        "export_page_count": item.export_page_count,
                        "preview_columns": list(item.preview_columns),
                        "rows": list(item.preview_rows[:100]),
                    }
                    for item in result.exports
                ],
                "failures": [dict(item) for item in result.failures],
                "is_complete": result.is_complete,
                "completion_message": result.completion_message,
            }

        return self.tasks.start(
            "lz各仓实时库存",
            runner,
            tool="lz_warehouse_inventory",
            context={"account_id": account.id, "output_root": str(job.output_dir), "warehouse_names": list(job.warehouse_names)},
        )

    def get_lz_warehouse_inventory_warehouses(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            account = self._lz_inventory_account(payload)
            warehouses = fetch_lz_inventory_warehouses(account.username, account.password)
            return {"ok": True, "account_id": account.id, "warehouses": [dict(item) for item in warehouses]}
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_sku_inventory_developers(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            developers = fetch_sku_inventory_developers(
                str(request_payload.get("username") or ""),
                str(request_payload.get("password") or ""),
            )
            return {
                "ok": True,
                "developer_count": len(developers),
                "developers": [developer.to_dict() for developer in developers],
            }
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_sku_inventory_query(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            job = validate_sku_inventory_payload(request_payload)
            self.config_store.save(
                {
                    "output_dir": str(job.output_dir),
                    "rows_per_page": job.rows_per_page,
                }
            )
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            result = run_sku_inventory_query(job, progress)
            rows = sku_inventory_preview_rows(result.records)
            return {
                "query_mode": result.query_mode,
                "scope_warehouses": list(result.scope_warehouses),
                "unclassified_warehouses": list(result.unclassified_warehouses),
                "missing_sku_count": result.missing_sku_count,
                "record_count": len(result.records),
                "sku_count": result.sku_count,
                "developer_count": result.developer_count,
                "warehouse_count": result.warehouse_count,
                "output_file": str(result.output_file),
                "output_dir": str(result.output_file.parent),
                "rows": rows,
                "preview_limit": SKU_INVENTORY_PREVIEW_ROW_LIMIT,
                "preview_truncated": len(result.records) > len(rows),
            }

        return self.tasks.start(
            "SKU库存与可售天数查询",
            runner,
            tool="sku_inventory_query",
        )

    def start_developer_sales_income_summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            job = validate_developer_sales_income_payload(request_payload)
            self.config_store.save({"output_dir": str(job.output_dir)})
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            result = run_developer_sales_income_summary(job, progress)
            return {
                "developer_count": result.developer_count,
                "salesperson_count": result.salesperson_count,
                "source_row_count": result.source_row_count,
                "missing_developer_names": list(result.missing_developer_names),
                "output_file": str(result.output_file),
                "output_dir": str(result.output_dir),
            }

        return self.tasks.start(
            "开发与销售收入汇总导出",
            runner,
            tool="developer_sales_income_summary",
        )

    def get_mabang_warehouse_options(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            query = validate_mabang_permission_account(request_payload)
            result = load_mabang_warehouse_options(query)
            return {"ok": True, **result}
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


    def start_mabang_warehouse_permission_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            query = validate_mabang_permission_preview(request_payload)
            account_id = str(request_payload.get("account_id") or "").strip()
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            result = preview_mabang_warehouse_permissions(query, progress)
            preview_token = uuid4().hex
            now = time.time()
            record = {
                "created_at": now,
                "account_id": account_id,
                "employee_names": list(query.employee_names),
                "warehouse_ids": list(query.warehouse_ids),
                "permission_types": list(query.permission_types),
                "rows": list(result.get("rows") or []),
            }
            with self._mabang_permission_preview_lock:
                self._mabang_permission_previews = {
                    token: item
                    for token, item in self._mabang_permission_previews.items()
                    if now - float(item.get("created_at") or 0) <= 1800
                }
                self._mabang_permission_previews[preview_token] = record
            result["preview_token"] = preview_token
            return result

        return self.tasks.start(
            "马帮仓库权限预览",
            runner,
            tool="mabang_warehouse_permission_preview",
            context={"account_id": account_id, "mode": "preview"},
        )

    def start_mabang_warehouse_permission_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            account_id = str(request_payload.get("account_id") or "").strip()
            preview_token = str(request_payload.get("preview_token") or "").strip()
            employee_names = parse_employee_names(request_payload.get("employee_names") or request_payload.get("employee_text"))
            warehouse_ids = parse_warehouse_ids(request_payload.get("warehouse_ids"))
            permission_types = parse_permission_types(request_payload.get("permission_types"))
            if not preview_token:
                raise ValueError("请先完成预览检查")
            with self._mabang_permission_preview_lock:
                preview = self._mabang_permission_previews.get(preview_token)
            if preview is None or time.time() - float(preview.get("created_at") or 0) > 1800:
                raise ValueError("预览已失效，请重新预览")
            if preview.get("account_id") != account_id:
                raise ValueError("马帮账号已变化，请重新预览")
            if list(employee_names) != list(preview.get("employee_names") or []):
                raise ValueError("员工名单已变化，请重新预览")
            if list(warehouse_ids) != list(preview.get("warehouse_ids") or []):
                raise ValueError("仓库选择已变化，请重新预览")
            if list(permission_types) != list(preview.get("permission_types") or []):
                raise ValueError("开通权限选择已变化，请重新预览")
            job = build_batch_job(
                username=str(request_payload.get("username") or ""),
                password=str(request_payload.get("password") or ""),
                warehouse_ids=warehouse_ids,
                preview_rows=preview.get("rows"),
                permission_types=permission_types,
            )
            with self._mabang_permission_preview_lock:
                self._mabang_permission_previews.pop(preview_token, None)
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            return run_mabang_warehouse_permission_batch(job, progress)

        return self.tasks.start(
            "马帮仓库权限批量开通",
            runner,
            tool="mabang_warehouse_permission_batch",
            context={"account_id": account_id, "mode": "batch"},
        )

    def start_mabang_developer_permission_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            query = validate_mabang_developer_preview(request_payload)
            account_id = str(request_payload.get("account_id") or "").strip()
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            result = preview_mabang_developer_permissions(query, progress)
            preview_token = uuid4().hex
            now = time.time()
            record = {
                "created_at": now,
                "account_id": account_id,
                "employee_names": list(query.employee_names),
                "rows": list(result.get("rows") or []),
            }
            with self._mabang_developer_preview_lock:
                self._mabang_developer_previews = {
                    token: item
                    for token, item in self._mabang_developer_previews.items()
                    if now - float(item.get("created_at") or 0) <= 1800
                }
                self._mabang_developer_previews[preview_token] = record
            result["preview_token"] = preview_token
            return result

        return self.tasks.start(
            "马帮开发员预览检查",
            runner,
            tool="mabang_developer_permission_preview",
            context={"account_id": account_id, "mode": "preview", "employee_names": list(query.employee_names)},
        )

    def start_mabang_developer_permission_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            account_id = str(request_payload.get("account_id") or "").strip()
            preview_token = str(request_payload.get("preview_token") or "").strip()
            employee_names = parse_employee_names(
                request_payload.get("employee_names") or request_payload.get("employee_text")
            )
            if not preview_token:
                raise ValueError("请先完成预览检查")
            with self._mabang_developer_preview_lock:
                preview = self._mabang_developer_previews.get(preview_token)
                if preview is None or time.time() - float(preview.get("created_at") or 0) > 1800:
                    raise ValueError("预览已失效，请重新预览")
                if preview.get("account_id") != account_id:
                    raise ValueError("马帮账号已变化，请重新预览")
                if list(employee_names) != list(preview.get("employee_names") or []):
                    raise ValueError("员工名单已变化，请重新预览")
                job = build_developer_permission_batch_job(
                    username=str(request_payload.get("username") or ""),
                    password=str(request_payload.get("password") or ""),
                    preview_rows=preview.get("rows"),
                )
                # Consume under the same lock so simultaneous clicks cannot save twice.
                self._mabang_developer_previews.pop(preview_token)
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            return run_mabang_developer_permission_batch(job, progress)

        try:
            result = self.tasks.start(
                "马帮批量添加开发员",
                runner,
                tool="mabang_developer_permission_batch",
                context={"account_id": account_id, "mode": "batch", "employee_names": list(employee_names)},
            )
            if result.get("ok"):
                return result
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        with self._mabang_developer_preview_lock:
            self._mabang_developer_previews[preview_token] = preview
        return result

    def inspect_temu_shipping_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            workbook = read_temu_shipping_workbook(str((payload or {}).get("input_file") or "").strip())
            return {"ok": True, "file_path": workbook["input_file"], "workbook": workbook}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def choose_temu_shipping_file(self) -> dict[str, Any]:
        try:
            import webview

            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False, file_types=("Excel 文件 (*.xlsx)",),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            selected = result[0] if isinstance(result, (list, tuple)) else result
            return self.inspect_temu_shipping_file({"input_file": str(selected)})
        except Exception:
            return {"ok": False, "error": "打开文件选择器失败，请直接输入 Excel 完整路径"}

    def _temu_shipping_store(self) -> TemuShippingRunStore:
        return TemuShippingRunStore(self.config_store.local_data_dir / "temu_shipping")

    def get_temu_shipping_checkpoint(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            request = self._payload_with_account(payload, "mabang")
            record = self._temu_shipping_store().latest(temu_shipping_account_key(request["username"]))
            return {"ok": True, "result": temu_shipping_public_result(record) if record else None}
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _start_temu_task(self, request: dict, *, mode: str, input_file: str, run_id: str = "") -> dict:
        key = temu_shipping_account_key(request["username"])
        with self._temu_operation_lock:
            if key in self._temu_running_accounts:
                return {"ok": False, "error": "该马帮账号已有 TEMU 任务正在运行，请等待完成"}
            self._temu_running_accounts.add(key)

        def release():
            with self._temu_operation_lock:
                self._temu_running_accounts.discard(key)

        def runner(progress):
            try:
                common = {
                    "username": request["username"], "password": request["password"],
                    "store": self._temu_shipping_store(), "progress": progress,
                }
                if mode == "preview":
                    return preview_temu_shipping(input_file=input_file, **common)
                return run_temu_shipping_batch(run_id=run_id, **common)
            except Exception as exc:
                secret = request.get("password")
                message = str(exc).replace(secret, "[已隐藏]") if secret else str(exc)
                raise ValueError(message) from None
            finally:
                release()

        try:
            result = self.tasks.start(
                "TEMU渠道查询预览" if mode == "preview" else "TEMU发货渠道批量设置",
                runner, tool="temu_shipping_preview" if mode == "preview" else "temu_shipping_batch",
                context={"account_id": request["account_id"], "input_file": input_file, "run_id": run_id, "mode": mode},
            )
            if not result.get("ok"):
                release()
            return result
        except Exception as exc:
            release()
            return {"ok": False, "error": str(exc)}

    def start_temu_shipping_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request = self._payload_with_account(payload, "mabang")
            input_file = str(request.get("input_file") or "").strip()
            if not input_file or Path(input_file).suffix.lower() != ".xlsx" or not Path(input_file).is_file():
                raise ValueError("请选择存在的 .xlsx 文件")
            return self._start_temu_task(request, mode="preview", input_file=input_file)
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_temu_shipping_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request = self._payload_with_account(payload, "mabang")
            run_id = str(request.get("run_id") or "").strip()
            record = self._temu_shipping_store().load(run_id, temu_shipping_account_key(request["username"]))
            result = temu_shipping_public_result(record)
            if not result["can_apply"]:
                raise ValueError(result["message"] or "没有待处理渠道，请重新查询预览")
            return self._start_temu_task(request, mode="batch", input_file=record["input_file"], run_id=run_id)
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_group_sales_options(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            groups = get_group_options(request_payload["username"], request_payload["password"])
            return {"ok": True, "groups": groups}
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_group_sales_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            if "output_dir" not in request_payload:
                request_payload["output_dir"] = self.config_store.load().output_dir
            job = validate_group_sales_payload(request_payload)
            self.config_store.save(
                {
                    "output_dir": str(job.output_dir),
                    "sales_group_ids": [group.id for group in job.groups],
                }
            )
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            result = run_group_sales_report(job, progress)
            return {
                "record_count": result.record_count,
                "group_count": result.group_count,
                "csv_file_count": result.csv_file_count,
                "output_file": str(result.output_file),
                "output_dir": str(result.output_dir),
                "output_root": str(job.output_dir),
            }

        return self.tasks.start(
            "菲律宾各组商品销量报表", runner, tool="group_sales",
            context={"output_root": str(job.output_dir)},
        )

    def get_warehouse_export_options(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            categories, warehouses = fetch_warehouse_report_options(
                request_payload["username"],
                request_payload["password"],
            )
            settings = self.config_store.load()
            return {
                "ok": True,
                "categories": [{"id": item.id, "name": item.name} for item in categories],
                "warehouses": [{"id": item.id, "name": item.name} for item in warehouses],
                "output_dir": resolve_output_directories(settings, "warehouse_sales_export")["output_dir"],
                "month_range": warehouse_export_default_months(),
            }
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_warehouse_sales_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            if not str(request_payload.get("output_dir") or "").strip():
                request_payload["output_dir"] = resolve_output_directories(
                    self.config_store.load(), "warehouse_sales_export"
                )["output_dir"]
            job = validate_warehouse_export_payload(request_payload)
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            result = run_warehouse_sales_export(job, progress)
            return {
                "month_count": result.month_count,
                "record_count": result.record_count,
                "output_file": str(result.output_file),
                "output_dir": str(result.output_dir),
                "failed_months": list(result.failed_months),
                "category_count": len(job.category_ids),
                "warehouse_count": len(job.warehouse_ids),
                "months": [
                    {"label": item.label, "record_count": item.record_count}
                    for item in result.months
                ],
            }

        return self.tasks.start("商品仓库报表按月导出", runner, tool="warehouse_sales_export")

    def start_ph_cancel_refund_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"ok": False, "error": "取消订单查询参数无效"}
        try:
            account = self._payload_with_account({"account_id": payload.get("account_id")}, "mabang")
            fallback = resolve_output_directories(self.config_store.load(), ph_cancel_refund_sync.TOOL)["output_dir"]
            output = Path(str(payload.get("output_dir") or fallback).strip().strip('"')).expanduser()
            if not output.is_absolute():
                raise ValueError("请输入完整的输出目录")
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except (ValueError, OSError) as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            queue, result = ph_cancel_refund_sync.preview_queue(account["username"], account["password"], output, progress)
            token = uuid4().hex if queue else ""
            with self._cancel_refund_lock:
                self._cancel_refund_preview = {
                    "token": token, "created": time.monotonic(), "account_id": account["account_id"],
                    "username": account["username"], "queue": queue, "output": output,
                } if queue else None
            return {**result, "preview_token": token}

        # Keep preview creation and acceptance ordered even if separate pages
        # ask to start a task at the same time.
        with self._cancel_refund_lock:
            task = self.tasks.start("菲律宾取消订单预览", runner, tool=ph_cancel_refund_sync.TOOL,
                                    context={"account_id": account["account_id"], "mode": "preview"}, exclusive=True)
            if task.get("ok"):
                self._cancel_refund_preview = None
        return task

    def start_ph_cancel_refund_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("confirm_sync") is not True:
            return {"ok": False, "error": "请核对预览后确认同步"}
        try:
            account = self._payload_with_account({"account_id": payload.get("account_id")}, "mabang")
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        token = str(payload.get("preview_token") or "")
        if not token:
            return {"ok": False, "error": "请先查询待同步订单"}

        with self._cancel_refund_lock:
            preview = self._cancel_refund_preview
            if preview is None or preview["token"] != token or time.monotonic() - preview["created"] > 900:
                return {"ok": False, "error": "预览已失效或已提交，请重新查询"}
            if (preview["account_id"], preview["username"]) != (account["account_id"], account["username"]):
                return {"ok": False, "error": "当前马帮账号与预览不一致，请重新查询"}

            def runner(progress):
                return ph_cancel_refund_sync.sync_queue(account["username"], account["password"],
                                                       preview["output"], preview["queue"], progress)

            task = self.tasks.start(ph_cancel_refund_sync.TITLE, runner, tool=ph_cancel_refund_sync.TOOL,
                                    context={"account_id": account["account_id"], "mode": "sync"}, exclusive=True)
            if task.get("ok"):
                self._cancel_refund_preview = None
        return task

    def start_mabang_income_expense_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            job = validate_income_expense_payload(request_payload)
            self.config_store.save(
                {
                    "output_dir": str(job.output_dir),
                    "income_expense_category_ids": [category.id for category in job.categories],
                    "income_expense_warehouse_keys": [warehouse.key for warehouse in job.warehouses],
                }
            )
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            result = run_income_expense_report(job, progress)
            return {
                "record_count": result.record_count,
                "target_count": result.target_count,
                "category_count": result.category_count,
                "warehouse_count": result.warehouse_count,
                "csv_file_count": result.csv_file_count,
                "output_dir": str(result.output_dir),
                "output_files": [str(path) for path in result.files],
                "targets": list(result.targets),
            }

        return self.tasks.start(
            "马帮收支报表",
            runner,
            tool="mabang_income_expense_report",
            context={
                "account_id": str(request_payload.get("account_id") or ""),
                "category_ids": [category.id for category in job.categories],
                "warehouse_keys": [warehouse.key for warehouse in job.warehouses],
                "start_date": job.start_date,
                "end_date": job.end_date,
            },
        )

    def get_kec_reconciliation_info(self) -> dict[str, Any]:
        from backend.services.kec_reconciliation import DEFAULT_MABANG_BASE_URL

        settings = self.config_store.load()
        return {
            "ok": True,
            "settings": _settings_payload(settings),
            "account_state": _account_state_payload(settings),
            "basis": "实际体积（长 × 宽 × 高 × 数量 × 0.000001）",
            "default_base_url": DEFAULT_MABANG_BASE_URL,
        }

    def inspect_kec_reconciliation_source(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        source_file = str((payload or {}).get("input_file") or "").strip()
        try:
            preview = inspect_kec_source_workbook(source_file)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "读取账单失败，请确认文件可访问、未加密且格式正确"}
        return {
            "ok": True,
            "input_file": str(preview.input_file),
            "sheet_titles": dict(preview.sheet_titles),
            "columns": dict(preview.columns),
            "warnings": list(preview.warnings),
        }

    def choose_kec_reconciliation_file(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        kind = str((payload or {}).get("kind") or "input").strip()
        is_input = kind != "quote"
        try:
            import webview

            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("Excel 文件 (*.xlsx;*.xlsm)",),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            selected = result[0] if isinstance(result, (list, tuple)) else result
            path = str(Path(selected).expanduser())
            if not is_input:
                return {"ok": True, "kind": "quote", "path": path, "name": Path(path).name}
            preview = self.inspect_kec_reconciliation_source({"input_file": path})
            if not preview.get("ok"):
                return preview
            preview.update({"kind": "input", "path": path, "name": Path(path).name})
            return preview
        except Exception:
            return {"ok": False, "error": "打开文件选择器失败，请直接粘贴 Excel 完整路径"}

    def start_kec_reconciliation(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "mabang")
            job = validate_kec_reconciliation_payload(request_payload)
            self.config_store.save({"output_dir": str(job.output_dir)})
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "读取参数或账单失败，请检查文件和账号配置"}

        def runner(progress):
            result = run_kec_reconciliation(job, progress)
            summary = {
                key: (float(value) if isinstance(value, Decimal) else value)
                for key, value in result.summary.items()
            }
            return {
                "output_file": str(result.output_file),
                "avg_daily_orders": float(result.avg_daily_orders),
                "outbound_base_fee": float(result.outbound_base_fee),
                "storage_billed_amount": float(result.storage_billed_amount),
                "storage_expected_amount": float(result.storage_expected_amount),
                "storage_difference": float(result.storage_difference),
                "summary": summary,
            }

        return self.tasks.start(
            "KEC 对账",
            runner,
            tool="kec_reconciliation",
            context={
                "input_file": str(job.input_file),
                "quote_file": str(job.quote_file or ""),
                "output_dir": str(job.output_dir),
                "base_url": job.base_url,
                "account_id": str(request_payload.get("account_id") or ""),
            },
        )

    def get_tax_declaration_info(self) -> dict[str, Any]:
        return {"ok": True, **tax_declaration_service.get_defaults(self.config_store.local_data_dir)}

    def choose_tax_declaration_file(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        kind = (payload or {}).get("kind")
        if kind not in ("workbook", "app"):
            return {"ok": False, "error": "请选择账号表或税务客户端"}
        try:
            import webview

            file_types = ("Excel 账号表 (*.xlsx;*.xlsm)",) if kind == "workbook" else ("税务客户端 (*.exe)",)
            selected = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False, file_types=file_types,
            )
            if not selected:
                return {"ok": False, "cancelled": True}
            path = Path(selected[0] if isinstance(selected, (list, tuple)) else selected)
            return {"ok": True, "path": str(path), "name": path.name}
        except Exception:
            return {"ok": False, "error": "无法打开文件选择器，请粘贴文件的完整路径"}

    def start_tax_declaration(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            settings = tax_declaration_service.validate_request(payload, self.config_store.local_data_dir)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "message": str(exc)}
        except OSError:
            message = "无法读取账号表或税务客户端路径，请检查文件权限"
            return {"ok": False, "error": message, "message": message}

        def runner(progress):
            return tax_declaration_service.run_tax_declaration(settings, progress)

        try:
            task = self.tasks.start(
                "电子税务局申报", runner, tool="tax_declaration", exclusive=True,
                context={"mode": "export_only" if settings.export_records_only else "declare"},
            )
        except (OSError, RuntimeError):
            message = "无法启动后台任务，请检查桌面程序运行环境"
            return {"ok": False, "error": message, "message": message}
        if not task.get("ok"):
            message = "已有任务正在运行，请等待完成后再启动电子税务局流程"
            return {"ok": False, "error": message, "message": message}
        return {**task, "task_id": task["id"]}

    def get_task_status(self, task_id: str) -> dict[str, Any]:
        return self.tasks.snapshot(str(task_id or ""))

    def get_latest_task_status(self, tool: str = "") -> dict[str, Any]:
        """Return the latest task, optionally scoped to one frontend module."""
        return self.tasks.latest_snapshot(str(tool or ""))

    def check_for_updates(self) -> dict[str, Any]:
        settings = self.config_store.load()
        result = check_update_manifest(settings.update_manifest_url, APP_VERSION)
        return result_payload(result)

    def start_update_download(self) -> dict[str, Any]:
        settings = self.config_store.load()
        update = check_update_manifest(settings.update_manifest_url, APP_VERSION)
        if update.error:
            return {"ok": False, "error": update.error}
        if not update.configured:
            return {"ok": False, "error": "请先配置更新源"}
        if not update.update_available:
            return {"ok": False, "error": "当前已是最新版本"}
        if not update.package_url:
            return {"ok": False, "error": "发布清单缺少安装包地址"}
        if not update.sha256:
            return {"ok": False, "error": "发布清单缺少安装包 SHA256"}

        updates_dir = self.config_store.local_data_dir / "updates"

        def runner(progress):
            installer_path = download_update_package(
                update.package_url,
                update.latest_version,
                update.sha256,
                updates_dir,
                progress,
            )
            return {
                "installer_path": str(installer_path),
                "version": update.latest_version,
                "sha256": update.sha256,
            }

        return self.update_tasks.start(f"下载客户端更新 {update.latest_version}", runner)

    def get_update_task_status(self, task_id: str) -> dict[str, Any]:
        return self.update_tasks.snapshot(str(task_id or ""))

    def install_downloaded_update(self, installer_path: str) -> dict[str, Any]:
        try:
            candidate = self._validated_update_installer(installer_path)
            os.startfile(str(candidate))
            threading.Thread(target=_close_app_after_update_launch, daemon=True).start()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_path(self, path: str) -> dict[str, Any]:
        target = Path(path)
        if not target.exists():
            return {"ok": False, "error": f"路径不存在: {target}"}
        os.startfile(str(target))
        return {"ok": True}

    def reveal_path(self, path: str) -> dict[str, Any]:
        target = Path(path)
        if target.is_file():
            target = target.parent
        if not target.exists():
            return {"ok": False, "error": f"路径不存在: {target}"}
        os.startfile(str(target))
        return {"ok": True}

    @requires_internal_runtime
    def get_shopee_ads_info(self) -> dict[str, Any]:
        """返回 Shopee 广告充值页面所需信息（站点列表、路径等）。"""
        sites = get_site_options_list()
        default_site = sites[0]["code"] if sites else "id"
        paths = resolve_runtime_paths(default_site)
        return {
            "ok": True,
            "sites": sites,
            "client_path": paths.get("client_path", ""),
            "webdriver_path": paths.get("webdriver_path", ""),
        }

    @requires_internal_runtime
    def get_lazada_withdrawal_statistics_info(self) -> dict[str, Any]:
        """加载提现统计默认值和钉钉配置状态，不要求已安装紫鸟。"""
        try:
            settings = self.config_store.load()
            try:
                paths = resolve_lazada_withdrawal_runtime_paths()
            except Exception:
                paths = {}
            return {
                "ok": True,
                "countries": lazada_withdrawal_country_options(),
                "period": lazada_withdrawal_default_period(),
                "operator_names": [binding.name for binding in settings.dingtalk.users],
                "default_dingtalk_operator_name": (
                    settings.dingtalk.users[0].name if len(settings.dingtalk.users) == 1 else ""
                ),
                "dingtalk_configured": bool(
                    str(settings.dingtalk.app_key or "").strip()
                    and str(settings.dingtalk.app_secret or "").strip()
                ),
                "client_path": paths.get("client_path", ""),
                "webdriver_path": paths.get("webdriver_path", ""),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _temu_export_latest_path(self) -> Path:
        return self.config_store.local_data_dir / "temu_on_sale_export" / "latest.json"

    @requires_internal_runtime
    def get_balance_statistics_info(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Load local defaults without logging in or querying the store catalog."""
        try:
            defaults = balance_statistics.get_defaults((payload or {}).get("platform", ""))
            try:
                paths = ziniao_runtime_paths()
            except Exception:
                # Runtime discovery is optional until the user starts a task.
                paths = {}
            selected = str((payload or {}).get("client_path") or "").strip()
            if selected:
                path = Path(selected).expanduser()
                paths["client_path"] = str(path) if path.name.casefold() == "ziniao.exe" and path.is_file() else ""
            return {"ok": True, **defaults, "output_dir": self.config_store.load().output_dir,
                    "client_path": paths.get("client_path", ""), "webdriver_path": paths.get("webdriver_path", ""),
                    "client_installed": bool(paths.get("client_path"))}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_ziniao_download(self) -> dict[str, Any]:
        """Open the fixed vendor download page; never execute an installer."""
        import webbrowser
        try:
            if not webbrowser.open("https://www.ziniao.com/download/?tab=win", new=2):
                raise RuntimeError()
            return {"ok": True}
        except Exception:
            return {"ok": False, "error": "无法打开下载页，请在浏览器访问 https://www.ziniao.com/download/ 下载紫鸟 V6"}

    def _start_balance_statistics(self, payload: dict[str, Any], *, retry: bool) -> dict[str, Any]:
        with self._balance_statistics_lock:
            try:
                if self._balance_statistics_active:
                    raise ValueError("已有余额统计任务正在运行，请完成后再开始")
                request = self._payload_with_account(payload, "ziniao")
                if retry and not str(request.get("manifest_path") or "").strip():
                    raise ValueError("请选择需要重试的余额统计批次")
                if not retry and request.get("manifest_path"):
                    raise ValueError("恢复已有批次请使用重试入口")
                request = balance_statistics.validate_request(request, default_output_root=self.config_store.load().output_dir)
                platform = request["platform"]
                context = {"account_id": request["account_id"], "platform": platform}
                if retry:
                    path = Path(str(request["manifest_path"])).expanduser().resolve()
                    balance_statistics.load_manifest(path, ziniao_account_profile(request), platform)
                    request["manifest_path"] = str(path)
                    context["manifest_path"] = str(path)
                self._balance_statistics_active = True
            except AccountRequiredError as exc:
                return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def release() -> None:
            with self._balance_statistics_lock:
                self._balance_statistics_active = False

        def runner(progress):
            try:
                operation = balance_statistics.retry_balance_statistics if retry else balance_statistics.run_balance_statistics
                result = operation(request, progress)
                return {**result, "account_id": request["account_id"]}
            finally:
                release()

        try:
            title = "TEMU" if platform == "temu" else "Lazada"
            task = self.tasks.start(f"{title} 余额统计", runner, tool=f"{platform}_balance_statistics", context=context, exclusive=True)
            if not task.get("ok"):
                release()
            return task
        except Exception as exc:
            release()
            return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def start_balance_statistics(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._start_balance_statistics(payload, retry=False)

    @requires_internal_runtime
    def retry_balance_statistics(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._start_balance_statistics(payload, retry=True)

    @requires_internal_runtime
    def get_temu_fee_refund_info(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            try:
                paths = ziniao_runtime_paths()
            except Exception:
                paths = {}
            return {"ok": True, **paths,
                    **resolve_output_directories(self.config_store.load(), "temu_fee_refund_export")}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def get_temu_fee_refund_progress(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request = self._payload_with_account(payload, "ziniao")
            path = Path(str(payload.get("manifest_path") or "")).resolve()
            record = temu_fee_refund.load_batch(path, ziniao_account_profile(request))
            return {"ok": True, "result": temu_fee_refund.public_result(path, record)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def start_temu_fee_refund_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._temu_export_lock:
            try:
                if self._temu_export_active:
                    raise ValueError("已有 TEMU 导出任务正在运行")
                names, start, end = temu_fee_refund.validate_request(payload)
                directories = resolve_output_directories(self.config_store.load(), "temu_fee_refund_export")
                directory = temu_export.writable_directory(payload.get("output_dir") or directories["output_dir"])
                request = self._payload_with_account(payload, "ziniao")
                profile = ziniao_account_profile(request)
                with ZiniaoBrowser(request) as browser:
                    browser.ready()
                    matched = temu_fee_refund.match_stores(names, browser.list_stores())
                if not matched["can_start"]:
                    raise ValueError("\n".join(f"{x['name']}：{x['error']}" for x in matched["issues"]))
                path = temu_fee_refund.create_batch(directory, profile, matched["stores"],
                                                    [s["operator_name"] for s in matched["stores"]], start, end)
                self._temu_export_active = True
            except AccountRequiredError as exc:
                return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def release():
            with self._temu_export_lock:
                self._temu_export_active = False

        def runner(progress):
            try:
                with ZiniaoBrowser(request) as browser:
                    return temu_fee_refund.run_batch(path, TemuFeeRefundGateway(profile, browser=browser), progress)
            except Exception as exc:
                return temu_fee_refund.fail_batch(path, profile, f"批次中断：{exc}")
            finally:
                release()

        try:
            task = self.tasks.start("TEMU 面单费及退货退款导出", runner, tool="temu_fee_refund_export",
                                    context={"manifest_path": str(path), "profile": profile, "account_id": request["account_id"]})
            if not task.get("ok"):
                try:
                    temu_fee_refund.fail_batch(path, profile, task.get("error") or "任务未能启动")
                finally:
                    release()
            return task
        except Exception as exc:
            try:
                temu_fee_refund.fail_batch(path, profile, f"任务未能启动：{exc}")
            finally:
                release()
            return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def get_temu_financial_export_info(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return {"ok": True, **resolve_output_directories(self.config_store.load(), "temu_financial_export")}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def get_temu_financial_export_progress(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request = self._payload_with_account(payload, "ziniao")
            path = Path(str(payload.get("manifest_path") or "")).resolve()
            record = financial_export.load_batch(path, ziniao_account_profile(request))
            return {"ok": True, "result": financial_export.public_result(path, record)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def start_temu_financial_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._temu_export_lock:
            try:
                if self._temu_export_active:
                    raise ValueError("已有 TEMU 导出任务正在运行，请等待完成")
                names, start, end = financial_export.validate_request(payload)
                directories = resolve_output_directories(self.config_store.load(), "temu_financial_export")
                directory = temu_export.writable_directory(payload.get("output_dir") or directories["output_dir"])
                request = self._payload_with_account(payload, "ziniao")
                profile = ziniao_account_profile(request)
                with ZiniaoBrowser(request) as browser:
                    browser.ready()
                    matched = financial_export.match_stores(names, browser.list_stores())
                if not matched["can_start"]:
                    raise ValueError("\n".join(f"{item['name']}：{item['error']}" for item in matched["issues"]))
                path = financial_export.create_batch(directory, profile, matched["stores"], start, end)
                self._temu_export_active = True
            except AccountRequiredError as exc:
                return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def release():
            with self._temu_export_lock:
                self._temu_export_active = False

        def runner(progress):
            try:
                with ZiniaoBrowser(request) as browser:
                    return financial_export.run_batch(
                        path, TemuFinancialGateway(profile, browser=browser), progress,
                    )
            except Exception as exc:
                return financial_export.fail_batch(path, profile, f"批次中断：{exc}")
            finally:
                release()

        try:
            task = self.tasks.start(
                "TEMU 财务报表导出", runner, tool="temu_financial_export",
                context={"manifest_path": str(path), "profile": profile, "account_id": request["account_id"]},
            )
            if not task.get("ok"):
                try:
                    financial_export.fail_batch(path, profile, task.get("error") or "任务未能启动")
                finally:
                    release()
            return task
        except Exception as exc:
            try:
                financial_export.fail_batch(path, profile, f"任务未能启动：{exc}")
            finally:
                release()
            return {"ok": False, "error": str(exc)}

    def _temu_activity_latest_path(self) -> Path:
        return self.config_store.local_data_dir / "temu_activity_export" / "latest.json"

    @requires_internal_runtime
    def get_temu_activity_export_info(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            directories = resolve_output_directories(self.config_store.load(), "temu_activity_export")
            latest = None
            pointer = self._temu_activity_latest_path()
            if pointer.is_file():
                try:
                    manifest_path = str(json.loads(pointer.read_text(encoding="utf-8")).get("manifest_path") or "")
                    if manifest_path and Path(manifest_path).is_file():
                        record = activity_export.load_batch(manifest_path)
                        latest = activity_export.public_result(Path(manifest_path), record)
                except Exception:
                    latest = None
            return {"ok": True, **directories, "latest": latest,
                    "sites": [{"code": code, "name": name} for code, name in activity_export.SITES.items()],
                    "activity_types": list(activity_page.ACTIVITY_TYPES),
                    "business_date": activity_export.beijing_today()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _temu_activity_catalog(self, request: dict[str, Any]) -> dict[str, Any]:
        with ZiniaoBrowser(request) as browser:
            browser.ready()
            return {store["store_id"]: store for store in browser.list_stores()}

    @requires_internal_runtime
    def start_temu_activity_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._temu_export_lock:
            try:
                if self._temu_export_active:
                    raise ValueError("已有 TEMU 导出任务正在运行，请等待完成")
                options = activity_export.validate_request(payload)
                directories = resolve_output_directories(self.config_store.load(), "temu_activity_export")
                directory = temu_export.writable_directory(payload.get("output_dir") or directories["output_dir"])
                request = self._payload_with_account(payload, "ziniao")
                profile = ziniao_account_profile(request)
                catalog = self._temu_activity_catalog(request)
                matched = activity_export.resolve_stores(options["store_names"], list(catalog.values()))
                if not matched["can_start"]:
                    raise ValueError("\n".join(f"{item['name']}：{item['error']}" for item in matched["issues"]))
                path = activity_export.create_batch(directory, profile, matched["stores"],
                                                    options["sites"], options["business_date"])
                self._temu_export_active = True
            except AccountRequiredError as exc:
                return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def release():
            with self._temu_export_lock:
                self._temu_export_active = False

        def runner(progress):
            try:
                with ZiniaoBrowser(request) as browser:
                    return activity_export.run_batch(path, activity_page.TemuActivityGateway(profile, browser=browser), progress)
            except Exception as exc:
                return activity_export.fail_batch(path, profile, f"批次中断：{exc}")
            finally:
                release()

        try:
            task = self.tasks.start("TEMU 活动报名导出", runner, tool="temu_activity_export",
                                    context={"manifest_path": str(path), "profile": profile, "account_id": request["account_id"]})
            if not task.get("ok"):
                try:
                    activity_export.fail_batch(path, profile, task.get("error") or "任务未能启动")
                finally:
                    release()
            else:
                try:
                    temu_export.write_json(self._temu_activity_latest_path(), {"manifest_path": str(path)})
                except Exception:
                    pass
            return task
        except Exception as exc:
            try:
                activity_export.fail_batch(path, profile, f"任务未能启动：{exc}")
            finally:
                release()
            return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def retry_temu_activity_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._temu_export_lock:
            try:
                if self._temu_export_active:
                    raise ValueError("已有 TEMU 导出任务正在运行，请等待完成")
                request = self._payload_with_account(payload, "ziniao")
                profile = ziniao_account_profile(request)
                path = Path(str(payload.get("manifest_path") or "")).resolve()
                record = activity_export.load_batch(path, profile)
                if record["status"] == "complete":
                    raise ValueError("该批次已全部导出，无需重试")
                if not any(store["status"] != "success" for store in record["stores"]):
                    raise ValueError("该批次没有失败的店铺")
                self._temu_export_active = True
            except AccountRequiredError as exc:
                return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def release():
            with self._temu_export_lock:
                self._temu_export_active = False

        def runner(progress):
            try:
                with ZiniaoBrowser(request) as browser:
                    return activity_export.run_batch(path, activity_page.TemuActivityGateway(profile, browser=browser), progress)
            except Exception as exc:
                return activity_export.fail_batch(path, profile, f"批次中断：{exc}")
            finally:
                release()

        try:
            task = self.tasks.start("TEMU 活动报名导出重试", runner, tool="temu_activity_export",
                                    context={"manifest_path": str(path), "profile": profile, "account_id": request["account_id"]})
            if not task.get("ok"):
                release()
            return task
        except Exception as exc:
            release()
            return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def get_temu_activity_export_progress(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request = self._payload_with_account(payload, "ziniao")
            path = Path(str(payload.get("manifest_path") or "")).resolve()
            record = activity_export.load_batch(path, ziniao_account_profile(request))
            return {"ok": True, "result": activity_export.public_result(path, record)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def inspect_temu_activity_page(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Read-only probe: open one store and report which calibrated labels exist."""
        with self._temu_export_lock:
            if self._temu_export_active:
                return {"ok": False, "error": "已有 TEMU 导出任务正在运行，请等待完成"}
            self._temu_export_active = True
        try:
            options = activity_export.validate_request({**payload, "business_date": payload.get("business_date") or activity_export.beijing_today()})
            request = self._payload_with_account(payload, "ziniao")
            profile = ziniao_account_profile(request)
            with ZiniaoBrowser(request) as browser:
                browser.ready()
                catalog = {store["store_id"]: store for store in browser.list_stores()}
                matched = activity_export.resolve_stores([options["store_names"][0]], list(catalog.values()))
                if not matched["can_start"]:
                    raise ValueError("\n".join(f"{item['name']}：{item['error']}" for item in matched["issues"]))
                gateway = activity_page.TemuActivityGateway(profile, browser=browser)
                gateway.preflight([{**matched["stores"][0], "status": "pending"}])
                result = gateway.inspect(matched["stores"][0], options["sites"])
            return {"ok": True, "profile": profile, "store_name": matched["stores"][0]["store_name"], **result}
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            with self._temu_export_lock:
                self._temu_export_active = False

    @requires_internal_runtime
    def get_temu_on_sale_export_info(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            import json
            paths = ziniao_runtime_paths()
            try:
                request = self._payload_with_account(payload, "ziniao")
            except AccountRequiredError:
                return {"ok": True, **paths, "profile": "", "account_id": "", "latest": None,
                        "output_dir": self.config_store.load().output_dir}
            profile = ziniao_account_profile(request)
            latest = None
            pointer_path = self._temu_export_latest_path()
            if pointer_path.is_file():
                pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
                manifest_path = pointer.get(profile)
                if manifest_path and Path(manifest_path).is_file():
                    latest = self.get_temu_on_sale_export_progress({"manifest_path": manifest_path, "account_id": request["account_id"]}).get("result")
            return {"ok": True, **paths, "profile": profile, "account_id": request["account_id"],
                    "output_dir": self.config_store.load().output_dir, "latest": latest}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def list_temu_on_sale_stores(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._temu_export_lock:
            if self._temu_export_active:
                return {"ok": False, "error": "TEMU 导出正在运行，请完成后刷新店铺"}
            try:
                self._temu_export_catalog = {}
                self._temu_export_previews.clear()
                request = self._payload_with_account(payload, "ziniao")
                profile = ziniao_account_profile(request)
                with ZiniaoBrowser(request) as browser:
                    browser.ready()
                    all_stores = browser.list_stores()
                stores = [store for store in all_stores if temu_export.is_temu(store)]
                self._temu_export_catalog = {"profile": profile, "stores": stores, "created": time.monotonic()}
                return {"ok": True, "profile": profile, "account_id": request["account_id"], "stores": stores,
                        "other_count": len(all_stores) - len(stores)}
            except AccountRequiredError as exc:
                return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def preview_temu_on_sale_stores(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._temu_export_lock:
            try:
                if self._temu_export_active:
                    raise ValueError("TEMU 导出正在运行，请完成后重新匹配")
                request = self._payload_with_account(payload, "ziniao")
                profile = ziniao_account_profile(request)
                catalog = self._temu_export_catalog
                if catalog.get("profile") != profile or time.monotonic() - catalog.get("created", 0) > 900:
                    raise ValueError("请先刷新当前账号的店铺列表")
                if payload.get("selection_mode") == "list":
                    stores = temu_export.select_stores(payload.get("store_ids"), catalog["stores"])
                    result = {"stores": stores, "issues": [], "can_start": True}
                else:
                    result = temu_export.match_stores(payload.get("store_names"), catalog["stores"])
                self._temu_export_previews.clear()
                token = ""
                if result["can_start"]:
                    token = uuid4().hex
                    self._temu_export_previews[token] = {"profile": profile, "stores": result["stores"], "created": time.monotonic()}
                return {"ok": True, **result, "preview_token": token, "profile": profile, "account_id": request["account_id"]}
            except AccountRequiredError as exc:
                return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def get_temu_on_sale_export_progress(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request = self._payload_with_account(payload, "ziniao")
            path = Path(str(payload.get("manifest_path") or "")).resolve()
            record = temu_export.load_batch(path, ziniao_account_profile(request))
            # The durable manifest survives an app restart. An interrupted process
            # releases its OS lock, so its pending/running stores can be retried.
            if record["status"] in {"running", "pending"}:
                try:
                    with temu_export.batch_lock(path):
                        if not self._temu_export_active:
                            record["status"] = "interrupted"
                except ValueError:
                    pass
            return {"ok": True, "result": temu_export.public_result(path, record)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _launch_temu_export(self, path: Path, request: dict[str, Any]) -> dict[str, Any]:
        profile = ziniao_account_profile(request)
        def release():
            with self._temu_export_lock:
                self._temu_export_active = False

        def runner(progress):
            try:
                with ZiniaoBrowser(request) as browser:
                    return temu_export.run_batch(path, TemuOnSaleGateway(profile, browser=browser), progress)
            finally:
                release()

        try:
            import json
            pointer = self._temu_export_latest_path()
            previous = json.loads(pointer.read_text(encoding="utf-8")) if pointer.is_file() else {}
            previous[profile] = str(path)
            temu_export.write_json(pointer, previous)
            task = self.tasks.start("TEMU 在售商品批量导出", runner, tool="temu_on_sale_export",
                                    context={"manifest_path": str(path), "profile": profile, "account_id": request["account_id"]})
            if not task.get("ok"):
                release()
            return task
        except Exception as exc:
            release()
            return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def start_temu_on_sale_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._temu_export_lock:
            try:
                if self._temu_export_active:
                    raise ValueError("已有 TEMU 导出任务正在运行")
                request = self._payload_with_account(payload, "ziniao")
                profile = ziniao_account_profile(request)
                directory = temu_export.writable_directory(payload.get("output_dir"))
                token = str(payload.get("preview_token") or "")
                if token:
                    # Older pages can still submit a previously validated list.
                    preview = self._temu_export_previews.get(token)
                    if not preview or preview["profile"] != profile or time.monotonic() - preview["created"] > 900:
                        raise ValueError("店铺预览已过期或账号变化，请重新匹配")
                    stores = preview["stores"]
                else:
                    names = temu_export.parse_names(payload.get("store_names"))
                    # A fresh name-only submission resolves against the bound
                    # account, without requiring any separate UI preparation.
                    with ZiniaoBrowser(request) as browser:
                        browser.ready()
                        current_stores = browser.list_stores()
                    matched = temu_export.match_stores(names, current_stores)
                    if not matched["can_start"]:
                        raise ValueError("\n".join(f"{issue['name']}：{issue['error']}" for issue in matched["issues"]))
                    stores = matched["stores"]
                path = temu_export.create_batch(directory, profile, stores)
                self._temu_export_active = True
                self._temu_export_previews.clear()
                return self._launch_temu_export(path, request)
            except AccountRequiredError as exc:
                return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def retry_temu_on_sale_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._temu_export_lock:
            try:
                if self._temu_export_active:
                    raise ValueError("已有 TEMU 导出任务正在运行")
                path = Path(str(payload.get("manifest_path") or "")).resolve()
                request = self._payload_with_account(payload, "ziniao")
                record = temu_export.load_batch(path, ziniao_account_profile(request))
                if record["status"] == "complete":
                    raise ValueError("此批次已完成，无需重试")
                with temu_export.batch_lock(path):
                    temu_export.writable_directory(path.parent)
                self._temu_export_active = True
                return self._launch_temu_export(path, request)
            except AccountRequiredError as exc:
                return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    def _shopee_id_invoice_latest_path(self) -> Path:
        return self.config_store.local_data_dir / "shopee_id_monthly_invoice" / "latest.json"

    @requires_internal_runtime
    def get_shopee_id_monthly_invoice_info(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            try:
                paths = ziniao_runtime_paths()
            except Exception:
                paths = {"client_path": "", "webdriver_path": ""}
            directories = resolve_output_directories(self.config_store.load(), "shopee_id_monthly_invoice")
            default_month = shopee_id_monthly_invoice.default_month()
            try:
                request = self._payload_with_account(payload, "ziniao")
            except AccountRequiredError:
                return {
                    "ok": True,
                    "default_month": default_month,
                    **paths,
                    **directories,
                    "profile": "",
                    "account_id": "",
                    "latest": None,
                }
            profile = ziniao_account_profile(request)
            latest = None
            pointer_path = self._shopee_id_invoice_latest_path()
            if pointer_path.is_file():
                try:
                    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
                    manifest_path = pointer.get(profile) if isinstance(pointer, dict) else ""
                    if manifest_path and Path(manifest_path).is_file():
                        progress_res = self.get_shopee_id_monthly_invoice_progress(
                            {"manifest_path": manifest_path, "account_id": request["account_id"]}
                        )
                        latest = progress_res.get("result")
                except Exception:
                    latest = None
            return {
                "ok": True,
                "default_month": default_month,
                **paths,
                **directories,
                "profile": profile,
                "account_id": request["account_id"],
                "latest": latest,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def preview_shopee_id_monthly_invoice_stores(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._shopee_id_invoice_lock:
            try:
                if self._shopee_id_invoice_active:
                    raise ValueError("已有 Shopee 印尼月度账单与税票下载任务正在运行，请完成后再匹配")
                data = dict(payload or {})
                months = shopee_id_monthly_invoice.validate_months(data)
                month = months[0]
                selection = shopee_id_monthly_invoice.validate_store_selection(data)
                request = self._payload_with_account(data, "ziniao")
                profile = ziniao_account_profile(request)
                with ZiniaoBrowser(request) as browser:
                    browser.ready()
                    current_stores = browser.list_stores()
                result = shopee_id_monthly_invoice.select_stores(selection, current_stores)
                self._shopee_id_invoice_previews.clear()
                token = ""
                if result["can_start"]:
                    token = uuid4().hex
                    self._shopee_id_invoice_previews[token] = {
                        "profile": profile,
                        "month": month,
                        "stores": result["stores"],
                        "created": time.monotonic(),
                    }
                return {
                    "ok": True,
                    **result,
                    "month": month,
                    "months": months,
                    "store_scope": selection["store_scope"],
                    "preview_token": token,
                    "profile": profile,
                    "account_id": request["account_id"],
                }
            except AccountRequiredError as exc:
                return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def get_shopee_id_monthly_invoice_progress(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request = self._payload_with_account(payload, "ziniao")
            path = Path(str((payload or {}).get("manifest_path") or "")).resolve()
            record = shopee_id_monthly_invoice.load_batch(path, ziniao_account_profile(request))
            if record["status"] in {"running", "pending"}:
                try:
                    with temu_export.batch_lock(path):
                        if not self._shopee_id_invoice_active:
                            record["status"] = "interrupted"
                except ValueError:
                    pass
            return {"ok": True, "result": shopee_id_monthly_invoice.public_result(path, record)}
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _launch_shopee_id_monthly_invoice(
        self,
        path: Path | None,
        request: dict[str, Any],
        *,
        month: str,
        retry: bool = False,
        retry_mode: str = "failed",
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        profile = ziniao_account_profile(request)
        context = {"profile": profile, "account_id": request["account_id"], "month": month}
        if path is not None:
            context["manifest_path"] = str(path)

        def release() -> None:
            with self._shopee_id_invoice_lock:
                self._shopee_id_invoice_active = False

        def redact(text: Any) -> str:
            value = str(text or "")
            secret = str(request.get("password") or "")
            return value.replace(secret, "[已隐藏]") if secret else value

        def runner(progress):
            nonlocal path
            try:
                with ZiniaoBrowser(request) as browser:
                    # Acquire the exclusive TaskManager slot before restarting
                    # a normal-mode client or accessing any account's stores.
                    browser.prepare_task(progress=progress)
                    if path is None:
                        matched = shopee_id_monthly_invoice.select_stores(
                            options, browser.list_stores()
                        )
                        if not matched["can_start"]:
                            raise ValueError("\n".join(
                                f"{issue['name']}：{issue['error']}" for issue in matched["issues"]
                            ))
                        path = shopee_id_monthly_invoice.create_batch(
                            options["output_dir"], profile, month, matched["stores"],
                            store_scope=options["store_scope"],
                            months=options["months"], max_workers=options["max_workers"],
                        )
                        progress(f"已确认 {len(matched['stores'])} 家印尼 Shopee 店铺、{len(options['months'])} 个月份，最多同时 {options['max_workers']} 家；重试沿用本批名单和月份")
                        # TaskManager retains this context mapping; subsequent
                        # snapshots expose the manifest as soon as it exists.
                        context["manifest_path"] = str(path)
                    pointer = self._shopee_id_invoice_latest_path()
                    previous = json.loads(pointer.read_text(encoding="utf-8")) if pointer.is_file() else {}
                    if not isinstance(previous, dict):
                        previous = {}
                    previous[profile] = str(path)
                    shopee_id_monthly_invoice.write_json(pointer, previous)

                    @contextmanager
                    def worker_gateway():
                        # Independent maps, drivers and HTTP sessions; only the
                        # coordinator above may restart the shared V6 client.
                        with ZiniaoBrowser(request) as worker_browser:
                            yield ShopeeIdInvoiceGateway(profile, browser=worker_browser, client_prepared=True)

                    return shopee_id_monthly_invoice.run_batch(
                        path,
                        ShopeeIdInvoiceGateway(profile, browser=browser),
                        progress,
                        retry_mode=retry_mode,
                        gateway_factory=worker_gateway,
                    )
            except Exception as exc:
                if path is not None:
                    return shopee_id_monthly_invoice.fail_batch(
                        path, profile, f"批次中断：{redact(exc)}"
                    )
                raise ValueError(redact(exc)) from None
            finally:
                release()

        try:
            title = ("Shopee 印尼月度税票补查" if retry_mode == "supplement" else
                     "Shopee 印尼月度账单与税票失败重试" if retry else "Shopee 印尼月度账单与税票下载")
            task = self.tasks.start(
                title,
                runner,
                tool="shopee_id_monthly_invoice",
                context=context,
                exclusive=True,
            )
            if not task.get("ok"):
                release()
            return task
        except Exception as exc:
            release()
            return {"ok": False, "error": redact(exc)}

    @requires_internal_runtime
    def start_shopee_id_monthly_invoice(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._shopee_id_invoice_lock:
            try:
                if self._shopee_id_invoice_active:
                    raise ValueError("已有 Shopee 印尼月度账单与税票下载任务正在运行，请等待完成")
                request = self._payload_with_account(payload, "ziniao")
                directories = resolve_output_directories(self.config_store.load(), "shopee_id_monthly_invoice")
                validated = shopee_id_monthly_invoice.validate_request(
                    payload or {},
                    default_output_dir=directories["output_dir"],
                )
                self.config_store.save({
                    "tool_output_dirs": {
                        directory_scope_key("shopee_id_monthly_invoice"): validated["output_dir"],
                    }
                })
                self._shopee_id_invoice_active = True
                self._shopee_id_invoice_previews.clear()
                return self._launch_shopee_id_monthly_invoice(
                    None, request, month=validated["month"], retry=False, options=validated
                )
            except AccountRequiredError as exc:
                return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def retry_shopee_id_monthly_invoice(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._shopee_id_invoice_lock:
            try:
                if self._shopee_id_invoice_active:
                    raise ValueError("已有 Shopee 印尼月度账单与税票下载任务正在运行，请等待完成")
                path = Path(str((payload or {}).get("manifest_path") or "")).resolve()
                request = self._payload_with_account(payload, "ziniao")
                record = shopee_id_monthly_invoice.load_batch(path, ziniao_account_profile(request))
                retry_mode = (payload or {}).get("retry_mode", "failed")
                if retry_mode not in {"failed", "supplement"}:
                    raise ValueError("重试范围无效")
                # A saved running status may represent an interrupted process;
                # the file lock below is authoritative for an active batch.
                eligible = any(s["status"] == "unavailable" if retry_mode == "supplement"
                               else s["status"] in shopee_id_monthly_invoice.RETRYABLE for s in record["stores"])
                if not eligible:
                    raise ValueError("没有待补查税票的店铺月份" if retry_mode == "supplement" else "没有失败或中断的店铺月份，无需重试")
                with temu_export.batch_lock(path):
                    temu_export.writable_directory(path.parent)
                self._shopee_id_invoice_active = True
                return self._launch_shopee_id_monthly_invoice(
                    path, request, month=record["month"], retry=True, retry_mode=retry_mode
                )
            except AccountRequiredError as exc:
                return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    def get_lazada_monthly_report_info(self) -> dict[str, Any]:
        """返回 Lazada 月度账单下载的国家、默认月份和紫鸟运行路径。"""
        try:
            paths = resolve_lazada_monthly_report_runtime_paths()
            return {
                "ok": True,
                "countries": lazada_monthly_report_country_options(),
                "default_month": lazada_monthly_report_default_month(),
                "client_path": paths.get("client_path", ""),
                "webdriver_path": paths.get("webdriver_path", ""),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_lazada_ads_data_info(self) -> dict[str, Any]:
        """返回广告数据默认日期、Sheet 和已配置的钉钉操作人。"""
        try:
            settings = self.config_store.load()
            try:
                paths = resolve_lazada_monthly_report_runtime_paths()
            except RuntimeError:
                paths = {}
            return {
                "ok": True,
                "default_sheet_name": lazada_ads_data.default_sheet_name(),
                "default_target_date": lazada_ads_data.default_target_date(),
                "workbook_id": settings.lazada_ads_workbook_id or lazada_ads_data.DEFAULT_WORKBOOK_ID,
                "saved_preferences": {
                    "client_path": settings.lazada_ads_client_path,
                    "workbook_id": settings.lazada_ads_workbook_id,
                },
                "operator_names": [binding.name for binding in settings.dingtalk.users],
                "dingtalk_configured": bool(settings.dingtalk.app_key and settings.dingtalk.app_secret),
                **resolve_output_directories(settings, "lazada_ads_data"),
                "client_path": settings.lazada_ads_client_path or paths.get("client_path", ""),
                "webdriver_path": paths.get("webdriver_path", ""),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def save_lazada_ads_preferences(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存广告页的客户端路径与工作簿；独立于账号和采集任务。"""
        try:
            if not isinstance(payload, dict):
                raise ValueError("保存内容必须是对象")
            preferences = {}
            if "client_path" in payload:
                preferences["lazada_ads_client_path"] = str(payload.get("client_path") or "").strip().strip('"')
            if "workbook_id" in payload:
                workbook = str(payload.get("workbook_id") or "").strip()
                preferences["lazada_ads_workbook_id"] = lazada_ads_data.normalize_workbook_id(workbook) if workbook else ""
            if preferences:
                self.config_store.save(preferences)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_lazada_campaign_download_info(self) -> dict[str, Any]:
        settings = self.config_store.load()
        return {"ok": True, "output_dir": str(download_lazada_campaign.default_output_dir(settings)),
                "site_aliases": dict(download_lazada_campaign.COUNTRY_ALIASES),
                "default_threshold": str(lazada_campaign_workbook.DEFAULT_THRESHOLD),
                "timeout_minutes": download_lazada_campaign.DEFAULT_TIMEOUT_MINUTES}

    def start_lazada_campaign_download(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            settings = self.config_store.load()
            options = download_lazada_campaign.download_options(payload, settings)
            account_id = payload.get("account_id") or settings.active_account_ids.get("ziniao", "")
            config = download_lazada_campaign.resolve_browser_config(settings, account_id)
        except download_lazada_campaign.ZiniaoAccountRequired as exc:
            return {"ok": False, "requires_account": True, "vendor": "ziniao", "error": str(exc)}
        except (TypeError, ValueError, OSError) as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            try:
                return download_lazada_campaign.run_download(options, config, progress)
            except (RuntimeError, ValueError, OSError) as exc:
                return {"is_complete": False, "completion_message": str(exc)}

        tool = "lazada_campaign_download"
        with self._campaign_download_start_lock:
            if self.tasks.latest_snapshot(tool).get("status") in {"pending", "running"}:
                return {"ok": False, "error": "活动模板任务正在运行，可在运行日志中查看进度"}
            return self.tasks.start("Lazada 活动模板下载", runner, tool=tool,
                                    context={**asdict(options), "threshold": str(options.threshold), "account_id": account_id})

    def get_lazada_campaign_workbook_info(self) -> dict[str, Any]:
        settings = self.config_store.load()
        return {
            "ok": True,
            "workflow": "local_excel",
            "default_threshold": str(lazada_campaign_workbook.DEFAULT_THRESHOLD),
            "output_dir": settings.output_dir,
            "max_files": lazada_campaign_workbook.MAX_FILES,
            "supports_reprice_equal_prices": True,
            "supports_sku_price_filter": True,
            "supports_removed_sku_export": True,
            "modes": [{"value": "auto", "label": "自动识别"},
                      {"value": "plain", "label": "按已有活动价筛选"},
                      {"value": "invited", "label": "按推荐价填充并筛选"}],
        }

    def choose_lazada_campaign_files(self) -> dict[str, Any]:
        try:
            import webview

            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True,
                file_types=("Excel 文件 (*.xlsx;*.xlsm)",),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            selected = result if isinstance(result, (list, tuple)) else [result]
            return {"ok": True, "paths": [str(Path(path).expanduser().resolve()) for path in selected]}
        except Exception:
            return {"ok": False, "error": "打开文件选择器失败，请粘贴 Excel 完整路径"}

    def choose_lazada_campaign_sku_price_file(self) -> dict[str, Any]:
        try:
            import webview

            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("Excel 文件 (*.xlsx;*.xlsm)",),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            selected = result[0] if isinstance(result, (list, tuple)) else result
            return {"ok": True, "path": str(Path(selected).expanduser().resolve())}
        except Exception:
            return {"ok": False, "error": "打开 SKU 价格表选择器失败，请粘贴 Excel 完整路径"}

    def inspect_lazada_campaign_files(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"ok": True, **lazada_campaign_workbook.inspect_campaign_files(payload)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "检查表格失败，请确认文件可读取且未损坏"}

    def export_lazada_campaign_removed_skus(self, payload: dict[str, Any]) -> dict[str, Any]:
        temporary = None
        try:
            if not isinstance(payload, dict) or not isinstance(payload.get("task_id"), str):
                raise ValueError("请选择已完成任务中的删除SKU明细")
            index = payload.get("file_index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ValueError("删除SKU明细文件序号无效")
            task = self.tasks.snapshot(payload["task_id"])
            if not task.get("ok") or task.get("tool") != "lazada_campaign_workbook" or task.get("status") not in {"success", "failed"}:
                raise ValueError("该任务尚无可导出的删除SKU明细")
            result = task.get("result") or {}
            files = result.get("files") or []
            if index >= len(files) or not files[index].get("removed_sku_file"):
                raise ValueError("此文件没有可导出的删除SKU明细，请重新处理后重试")
            source = Path(files[index]["removed_sku_file"]).resolve()
            batch_root = Path(result["output_dir"]).resolve()
            if not source.is_relative_to(batch_root) or not source.is_file() or source.suffix.lower() != ".xlsx":
                raise ValueError("删除SKU明细不存在或已被移动，请重新处理")
            import webview

            selected = webview.windows[0].create_file_dialog(
                webview.FileDialog.SAVE, directory=str(source.parent), save_filename=source.name,
                file_types=("Excel 文件 (*.xlsx)",),
            )
            if not selected:
                return {"ok": False, "cancelled": True}
            target = Path(selected[0] if isinstance(selected, (list, tuple)) else selected).expanduser().resolve()
            if not target.suffix:
                target = target.with_suffix(".xlsx")
            if target.suffix.lower() != ".xlsx":
                raise ValueError("请将删除SKU明细保存为 .xlsx 文件")
            context = task.get("context") or {}
            protected = [*context.get("source_files", []), context.get("sku_price_file", "")]
            protected.extend(value for value in result.get("output_files", []) if value and Path(value).resolve() != source)
            if any(value and target == Path(value).resolve() for value in protected):
                raise ValueError("请另存为新文件，不能覆盖原活动表、SKU价格表或其他处理结果")
            if target != source:
                temporary = target.with_name(f".{target.stem}.{uuid4().hex}.tmp.xlsx")
                shutil.copyfile(source, temporary)
                os.replace(temporary, target)
            return {"ok": True, "path": str(target)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "删除SKU明细导出失败，请检查保存位置和文件占用"}
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def export_lazada_campaign_template(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            if payload is not None and not isinstance(payload, dict):
                raise ValueError("模板导出参数必须是对象")
            request = dict(payload or {})
            request.setdefault("output_dir", self.config_store.load().output_dir)
            return lazada_campaign_workbook.export_campaign_template(request)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "模板保存失败，请检查目录权限和文件占用"}

    def start_lazada_campaign_workbook(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if not isinstance(payload, dict):
                raise ValueError("表格处理参数必须是对象")
            request = dict(payload)
            request.setdefault("output_dir", self.config_store.load().output_dir)
            job = lazada_campaign_workbook.validate_campaign_payload(request)
            if job.sku_price_file:
                lazada_campaign_workbook.load_sku_prices(job.sku_price_file)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "表格处理参数无效，请检查 Excel 文件、折扣率和输出目录"}

        if not self._campaign_operation_lock.acquire(blocking=False):
            return {"ok": False, "error": "正在处理表格，请等待当前任务结束"}

        def runner(progress):
            try:
                return lazada_campaign_workbook.run_campaign_batch(job, progress)
            finally:
                self._campaign_operation_lock.release()

        try:
            self.config_store.save({"output_dir": str(job.output_dir)})
            task = self.tasks.start(
                "Excel 折扣筛选", runner, tool="lazada_campaign_workbook",
                context={"workflow": "local_excel", "source_files": [str(path) for path in job.source_files],
                         "mode": job.mode, "threshold": str(job.threshold), "output_dir": str(job.output_dir),
                         "reprice_equal_prices": job.reprice_equal_prices,
                         "sku_price_file": str(job.sku_price_file or "")},
            )
            if not task.get("ok"):
                self._campaign_operation_lock.release()
            return task
        except Exception:
            self._campaign_operation_lock.release()
            return {"ok": False, "error": "表格处理任务启动失败，请稍后重试"}

    def get_lazada_bill_detail_info(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """返回账单明细页的国家选项、默认 Sheet/区间与已保存偏好。"""
        try:
            settings = self.config_store.load()
            try:
                paths = resolve_lazada_monthly_report_runtime_paths()
            except RuntimeError:
                paths = {}
            countries = lazada_bill_detail.country_options()
            directory_preferences = {
                item["code"]: resolve_output_directories(settings, "lazada_bill_detail", item["code"])
                for item in countries
            }
            country = lazada_bill_detail.resolve_country((payload or {}).get("country") or "TH").code
            return {
                "ok": True,
                "countries": [{**item, **directory_preferences[item["code"]]} for item in countries],
                "directory_preferences": directory_preferences,
                "default_sheet_name": lazada_bill_detail.default_sheet_name(),
                "date_range": lazada_bill_detail.previous_month_range(),
                "workbook_id": settings.lazada_bill_workbook_id
                or lazada_bill_detail.DEFAULT_WORKBOOK_ID,
                "dws_node": settings.lazada_bill_dws_node,
                "image_column": lazada_bill_detail.IMAGE_COLUMN,
                # 运行前就能知道 F 列图片会不会写入：dws 缺失时页面会给出安装提示。
                "dws_available": lazada_bill_detail.dws_available(),
                "dws_path": lazada_bill_detail.dws_path(),
                "dws_hint": (
                    "" if lazada_bill_detail.dws_available() else lazada_bill_detail.DWS_MISSING_HINT
                ),
                **directory_preferences[country],
                "operator_names": [binding.name for binding in settings.dingtalk.users],
                "default_dingtalk_operator_name": (
                    settings.dingtalk.users[0].name if len(settings.dingtalk.users) == 1 else ""
                ),
                "dingtalk_configured": bool(
                    settings.dingtalk.app_key and settings.dingtalk.app_secret
                ),
                "client_path": settings.lazada_bill_client_path or paths.get("client_path", ""),
                "webdriver_path": paths.get("webdriver_path", ""),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def save_lazada_bill_preferences(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存共用文档和客户端偏好；截图目录仅属于指定国家。"""
        try:
            if not isinstance(payload, dict):
                raise ValueError("保存内容必须是对象")
            preferences: dict[str, Any] = {}
            if "client_path" in payload:
                preferences["lazada_bill_client_path"] = (
                    str(payload.get("client_path") or "").strip().strip('"')
                )
            if "screenshot_root" in payload or "screenshot_dir" in payload:
                country = lazada_bill_detail.resolve_country(payload.get("country") or "TH").code
                preferences["tool_output_dirs"] = {
                    directory_scope_key("lazada_bill_detail", country, "screenshot"): (
                        str(payload.get("screenshot_root", payload.get("screenshot_dir")) or "").strip().strip('"')
                    )
                }
            if "workbook_id" in payload:
                raw_workbook = str(payload.get("workbook_id") or "").strip()
                preferences["lazada_bill_workbook_id"] = (
                    lazada_bill_detail.normalize_workbook_id(raw_workbook) if raw_workbook else ""
                )
            if "dws_node" in payload:
                raw_node = str(payload.get("dws_node") or "").strip()
                preferences["lazada_bill_dws_node"] = (
                    lazada_bill_detail.validate_image_node(raw_node) if raw_node else ""
                )
            if preferences:
                self.config_store.save(preferences)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def list_lazada_bill_sheets(self, payload: dict[str, Any]) -> dict[str, Any]:
        """读取共用钉钉文档的 Sheet 列表，供账单明细页「指定 Sheet」下拉选择。"""
        try:
            settings = self.config_store.load()
            request = dict(payload or {})
            profile = lazada_bill_detail.resolve_country(request.get("country"))
            workbook_id = lazada_bill_detail.normalize_workbook_id(
                request.get("workbook_id")
                or settings.lazada_bill_workbook_id
                or lazada_bill_detail.DEFAULT_WORKBOOK_ID
            )
            operator_name = str(request.get("dingtalk_operator_name") or "").strip()
            if not operator_name and len(settings.dingtalk.users) == 1:
                operator_name = settings.dingtalk.users[0].name
            if not settings.dingtalk.app_key or not settings.dingtalk.app_secret:
                raise ValueError("请先在设置中配置钉钉 AppKey 和 AppSecret")
            user_id = _resolve_dingtalk_user_id(settings, operator_name) if operator_name else ""
            if not user_id:
                raise ValueError("请先在设置中配置钉钉操作人，或直接使用默认 Sheet")
            sheets = lazada_bill_detail.list_workbook_sheets(
                workbook_id,
                app_key=settings.dingtalk.app_key,
                app_secret=settings.dingtalk.app_secret,
                user_id=user_id,
            )
            names = [item["name"] for item in sheets]
            return {
                "ok": True,
                "workbook_id": workbook_id,
                "sheet_names": names,
                "sheets": sheets,
                "country": profile.code,
                "country_name": profile.name,
                "default_sheet_name": lazada_bill_detail.default_sheet_name(),
                # 下拉默认优先选与国家同名的 Sheet（泰国/菲律宾/马来/印尼/越南）。
                "preferred_sheet_name": _preferred_bill_sheet_name(names, profile),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_vietnam_collection_info(self) -> dict[str, Any]:
        """返回越南收支数据采集页面的批次默认值。"""
        settings = self.config_store.load()
        paths = resolve_runtime_paths("vn")
        return {
            "ok": True,
            "period": default_collection_period(),
            "output_dir": settings.output_dir,
            "report_types": report_options(),
            "milestone": "A",
            "real_collection_enabled": True,
            "client_path": paths.get("client_path", ""),
        }

    def create_vietnam_collection_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """创建紫鸟数据采集批次目录和 manifest；本阶段不启动浏览器。"""
        try:
            settings = self.config_store.load()
            request_payload = self._payload_with_account(payload, "ziniao")
            request = validate_collection_payload(
                request_payload,
                default_output_dir=settings.output_dir,
            )
            account_id = str(
                request_payload.get("account_id")
                or settings.active_account_ids.get("ziniao")
                or ""
            ).strip()
            account = next(
                (
                    item
                    for item in settings.accounts
                    if item.id == account_id and item.vendor == "ziniao"
                ),
                None,
            )
            result = create_collection_run(
                request,
                account_name=account.name if account is not None else "",
            )
            self.config_store.save({"output_dir": str(request.output_dir)})
            return {"ok": True, **result}
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_vietnam_collection_run(self, manifest_path: str) -> dict[str, Any]:
        """读取已落盘的批次清单，供页面恢复状态矩阵。"""
        try:
            return {"ok": True, "manifest": load_manifest(manifest_path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _check_run_lock_and_get_context(self, manifest_path: Path) -> dict[str, Any]:
        with self.tasks._lock:
            for task in self.tasks._tasks.values():
                if task.status in {"pending", "running"} and task.context.get("manifest_path") == str(manifest_path.resolve()):
                    raise RuntimeError(f"当前批次正在执行任务「{task.name}」，请等待该任务完成后再操作。")
        manifest = load_manifest(manifest_path)
        return {
            "manifest_path": str(manifest_path.resolve()),
            "run_id": manifest.get("run_id") or manifest_path.parent.name,
        }

    def start_vietnam_subsidy_collection(self, payload: dict[str, Any]) -> dict[str, Any]:
        """使用 Playwright 采集普通店和仓发店的补贴订单。"""
        try:
            settings = self.config_store.load()
            request_payload = self._payload_with_account(payload, "ziniao")
            account_id = str(
                request_payload.get("account_id")
                or settings.active_account_ids.get("ziniao")
                or ""
            ).strip()
            account = next(
                (
                    item
                    for item in settings.accounts
                    if item.id == account_id and item.vendor == "ziniao"
                ),
                None,
            )
            if account is None:
                raise AccountRequiredError("ziniao", "请先绑定并选择紫鸟账号")
            request_payload["company"] = str(account.extra.get("company") or account.name).strip()
            paths = resolve_runtime_paths("vn")
            job = validate_subsidy_collection_payload(
                request_payload,
                client_path=paths.get("client_path", ""),
                socket_port=16851,
            )
            context = self._check_run_lock_and_get_context(job.manifest_path)
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            return run_subsidy_collection(job, progress)

        return self.tasks.start("越南 Shopee 补贴订单采集", runner, tool="vietnam_subsidy_collection", context=context)

    def start_vietnam_ziniao_sources_collection(self, payload: dict[str, Any]) -> dict[str, Any]:
        """使用 Playwright 采集广告、联盟、广告补贴原始数据源。"""
        try:
            settings = self.config_store.load()
            request_payload = self._payload_with_account(payload, "ziniao")
            account_id = str(
                request_payload.get("account_id")
                or settings.active_account_ids.get("ziniao")
                or ""
            ).strip()
            account = next(
                (
                    item
                    for item in settings.accounts
                    if item.id == account_id and item.vendor == "ziniao"
                ),
                None,
            )
            if account is None:
                raise AccountRequiredError("ziniao", "请先绑定并选择紫鸟账号")
            request_payload["company"] = str(account.extra.get("company") or account.name).strip()
            paths = resolve_runtime_paths("vn")
            job = validate_ziniao_sources_collection_payload(
                request_payload,
                client_path=paths.get("client_path", ""),
                socket_port=16851,
            )
            context = self._check_run_lock_and_get_context(job.manifest_path)
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            return run_ziniao_sources_collection(job, progress)

        return self.tasks.start("越南 Shopee 广告/联盟/广告补贴采集", runner, tool="vietnam_ziniao_sources_collection", context=context)

    def start_vietnam_mabang_income_collection(self, payload: dict[str, Any]) -> dict[str, Any]:
        """拉取马帮越南 Shopee 收支明细、汇总并执行阻断校验。"""
        try:
            settings = self.config_store.load()
            request_payload = self._payload_with_account(payload, "mabang")
            account_id = str(
                request_payload.get("account_id")
                or settings.active_account_ids.get("mabang")
                or ""
            ).strip()
            account = next(
                (
                    item
                    for item in settings.accounts
                    if item.id == account_id and item.vendor == "mabang"
                ),
                None,
            )
            if account is None:
                raise AccountRequiredError("mabang", "请先绑定并选择马帮账号")
            request_payload["account_name"] = account.name or account.username
            job = validate_vietnam_mabang_income_payload(request_payload)
            context = self._check_run_lock_and_get_context(job.manifest_path)
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            return run_vietnam_mabang_income_collection(job, progress)

        return self.tasks.start(
            "越南 Shopee 马帮收支明细与汇总校验",
            runner,
            tool="vietnam_mabang_income_collection",
            context=context,
        )

    def start_vietnam_mabang_refund_collection(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按 1-10、11-20、21-月底导出马帮退款并合并去重。"""
        try:
            settings = self.config_store.load()
            request_payload = self._payload_with_account(payload, "mabang")
            account_id = str(
                request_payload.get("account_id")
                or settings.active_account_ids.get("mabang")
                or ""
            ).strip()
            account = next(
                (
                    item
                    for item in settings.accounts
                    if item.id == account_id and item.vendor == "mabang"
                ),
                None,
            )
            if account is None:
                raise AccountRequiredError("mabang", "请先绑定并选择马帮账号")
            request_payload["account_name"] = account.name or account.username
            job = validate_vietnam_mabang_refund_payload(request_payload)
            context = self._check_run_lock_and_get_context(job.manifest_path)
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            return run_vietnam_mabang_refund_collection(job, progress)

        return self.tasks.start(
            "越南 Shopee 马帮退款三段导出与合并",
            runner,
            tool="vietnam_mabang_refund_collection",
            context=context,
        )

    def start_vietnam_final_reconciliation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """基于已冻结的数据源生成最终收支核对工作簿。"""
        try:
            job = validate_vietnam_final_reconciliation_payload(payload)
            context = self._check_run_lock_and_get_context(job.manifest_path)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            return run_vietnam_final_reconciliation(job, progress)

        return self.tasks.start(
            "越南 Shopee 收支报表生成",
            runner,
            tool="vietnam_final_reconciliation",
            context=context,
        )

    def get_vietnam_report_dashboard(self, manifest_path: str) -> dict[str, Any]:
        try:
            from backend.services.vietnam_report_dashboard import get_vietnam_report_dashboard
            manifest_path_abs = Path(manifest_path).resolve()
            self.config_store.save({
                "vietnam_last_manifest_path": str(manifest_path_abs)
            })
            return get_vietnam_report_dashboard(manifest_path_abs)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def list_vietnam_report_runs(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            from backend.services.vietnam_report_dashboard import list_vietnam_report_runs
            return list_vietnam_report_runs(payload)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def save_vietnam_report_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            from backend.services.vietnam_report_dashboard import save_vietnam_report_config
            return save_vietnam_report_config(payload)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_vietnam_report_anomalies(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            from backend.services.vietnam_report_dashboard import get_vietnam_report_anomalies
            return get_vietnam_report_anomalies(payload)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def save_shopee_ads_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存 Shopee 充值配置到 AppData（按站点分别存储）。"""
        try:
            site_code = str((payload or {}).get("site_code") or "id").strip()
            from backend.services.shopee_ads_recharge import save_site_config

            saved_path = save_site_config(site_code, payload or {})
            return {"ok": True, "saved_path": saved_path}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @requires_internal_runtime
    def start_shopee_ads_recharge(self, payload: dict[str, Any]) -> dict[str, Any]:
        """启动 Shopee 广告充值任务（异步）。"""
        try:
            request_payload = self._payload_with_account(payload, "ziniao")
            job = validate_shopee_ads_payload(request_payload)
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        site_name = next(
            (s["name"] for s in get_site_options_list() if s["code"] == job.site_code),
            job.site_code,
        )

        def runner(progress):
            result = run_shopee_ads_recharge(job, progress)
            return result

        return self.tasks.start(f"Shopee{site_name}广告充值", runner, tool="shopee_ads")

    @requires_internal_runtime
    def start_lazada_withdrawal_statistics(self, payload: dict[str, Any]) -> dict[str, Any]:
        """启动 Lazada 提现统计任务（异步）。"""
        try:
            settings = self.config_store.load()
            request_payload = self._payload_with_account(payload, "ziniao")
            operator_name = str(request_payload.get("dingtalk_operator_name") or "").strip()
            if not operator_name and len(settings.dingtalk.users) == 1:
                operator_name = settings.dingtalk.users[0].name
            if not str(settings.dingtalk.app_key or "").strip() or not str(settings.dingtalk.app_secret or "").strip():
                raise ValueError("请先在设置中配置钉钉 AppKey 和 AppSecret，再开始提现统计")
            if not operator_name:
                raise ValueError("请选择钉钉操作人；可在设置中配置钉钉用户")
            user_id = _resolve_dingtalk_user_id(settings, operator_name)
            if not user_id:
                raise ValueError(f"操作人「{operator_name}」不在钉钉用户配置表中，请先在设置中配置")
            request_payload.update({
                "dingtalk_app_key": settings.dingtalk.app_key,
                "dingtalk_app_secret": settings.dingtalk.app_secret,
                "dingtalk_user_id": user_id,
            })
            job = validate_lazada_withdrawal_payload(
                request_payload,
                default_output_root=settings.output_dir,
            )
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        country_name = next(
            (item["name"] for item in lazada_withdrawal_country_options() if item["code"] == job.country),
            job.country,
        )

        def runner(progress):
            return run_lazada_withdrawal_statistics(job, progress)

        return self.tasks.start(
            f"Lazada{country_name}提现统计",
            runner,
            tool="lazada_withdrawal_statistics",
        )

    def start_lazada_monthly_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        """启动 Lazada 跨境店铺月度账单下载任务（异步）。"""
        try:
            settings = self.config_store.load()
            request_payload = self._payload_with_account(payload, "ziniao")
            request_payload["output_root"] = str(
                request_payload.get("output_root")
                or request_payload.get("output_dir")
                or settings.output_dir
                or ""
            ).strip()
            job = validate_lazada_monthly_report_payload(
                request_payload,
                default_output_root=settings.output_dir,
            )
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        country_names_by_code = {
            item["code"]: item["name"] for item in lazada_monthly_report_country_options()
        }
        country_names = [country_names_by_code.get(code, code) for code in job.countries]

        def runner(progress):
            return run_lazada_monthly_report(job, progress)

        return self.tasks.start(
            f"Lazada{'/'.join(country_names)}月度账单下载",
            runner,
            tool="lazada_monthly_report",
            context={
                "account_id": str(request_payload.get("account_id") or ""),
                "countries": list(job.countries),
                "month": job.month,
                "store_count": len(job.store_names),
                "output_root": str(job.output_root),
            },
        )

    def start_lazada_ads_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        """使用平台绑定的紫鸟与钉钉配置启动指定日期的数据采集。"""
        try:
            settings = self.config_store.load()
            request_payload = self._payload_with_account(payload, "ziniao")
            request_payload.setdefault("client_path", settings.lazada_ads_client_path)
            request_payload.setdefault("workbook_id", settings.lazada_ads_workbook_id)
            operator_name = str(request_payload.get("dingtalk_operator_name") or "").strip()
            if not operator_name and len(settings.dingtalk.users) == 1:
                operator_name = settings.dingtalk.users[0].name
            if not settings.dingtalk.app_key or not settings.dingtalk.app_secret:
                raise ValueError("请先在设置中配置钉钉 AppKey 和 AppSecret")
            if not operator_name:
                raise ValueError("请选择钉钉操作人；可在设置中配置钉钉用户")
            user_id = _resolve_dingtalk_user_id(settings, operator_name)
            if not user_id:
                raise ValueError(f"操作人「{operator_name}」不在钉钉用户配置表中，请先在设置中配置")
            request_payload.update({
                "dingtalk_app_key": settings.dingtalk.app_key,
                "dingtalk_app_secret": settings.dingtalk.app_secret,
                "dingtalk_user_id": user_id,
            })
            job = lazada_ads_data.validate_lazada_ads_payload(
                request_payload,
                default_output_root=resolve_output_directories(settings, "lazada_ads_data")["output_dir"],
            )
            self.config_store.save({"tool_output_dirs": {
                directory_scope_key("lazada_ads_data"): str(job.output_root),
            }})
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            return lazada_ads_data.run_lazada_ads_data(job, progress)

        return self.tasks.start(
            "泰国 Lazada 广告数据",
            runner,
            tool="lazada_ads_data",
            context={
                "account_id": str(request_payload.get("account_id") or ""),
                "sheet_name": job.sheet_name,
                "target_date": str(job.target_date),
                "store_count": len(job.store_names),
                "output_root": str(job.output_root),
            },
        )

    def start_lazada_bill_detail(self, payload: dict[str, Any]) -> dict[str, Any]:
        """使用平台绑定的紫鸟与钉钉配置采集指定国家、指定区间的账单明细。"""
        try:
            settings = self.config_store.load()
            request_payload = self._payload_with_account(payload, "ziniao")
            country = lazada_bill_detail.resolve_country(request_payload.get("country")).code
            directories = resolve_output_directories(settings, "lazada_bill_detail", country)
            if not str(request_payload.get("workbook_id") or "").strip():
                request_payload["workbook_id"] = (
                    settings.lazada_bill_workbook_id or lazada_bill_detail.DEFAULT_WORKBOOK_ID
                )
            if not str(request_payload.get("dws_node") or "").strip():
                request_payload["dws_node"] = settings.lazada_bill_dws_node
            request_payload.setdefault("client_path", settings.lazada_bill_client_path)
            request_payload.setdefault(
                "screenshot_root",
                request_payload.get("screenshot_dir") or directories["screenshot_root"],
            )
            operator_name = str(request_payload.get("dingtalk_operator_name") or "").strip()
            if not operator_name and len(settings.dingtalk.users) == 1:
                operator_name = settings.dingtalk.users[0].name
            if not settings.dingtalk.app_key or not settings.dingtalk.app_secret:
                raise ValueError("请先在设置中配置钉钉 AppKey 和 AppSecret")
            if not operator_name:
                raise ValueError("请选择钉钉操作人；可在设置中配置钉钉用户")
            user_id = _resolve_dingtalk_user_id(settings, operator_name)
            if not user_id:
                raise ValueError(f"操作人「{operator_name}」不在钉钉用户配置表中，请先在设置中配置")
            request_payload.update(
                {
                    "dingtalk_app_key": settings.dingtalk.app_key,
                    "dingtalk_app_secret": settings.dingtalk.app_secret,
                    "dingtalk_user_id": user_id,
                }
            )
            job = lazada_bill_detail.validate_lazada_bill_detail_payload(
                request_payload,
                default_output_root=directories["output_dir"],
                default_screenshot_root_value=directories["screenshot_root"],
            )
            self.config_store.save({"tool_output_dirs": {
                directory_scope_key("lazada_bill_detail", country): str(job.output_root),
                directory_scope_key("lazada_bill_detail", country, "screenshot"): str(job.screenshot_root),
            }})
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            return lazada_bill_detail.run_lazada_bill_detail(job, progress)

        return self.tasks.start(
            f"{job.country_name} Lazada 后台收支数据",
            runner,
            tool="lazada_bill_detail",
            context={
                "account_id": str(request_payload.get("account_id") or ""),
                "country": job.country,
                "country_name": job.country_name,
                "sheet_name": job.sheet_name,
                "start_date": job.start_date,
                "end_date": job.end_date,
                "store_count": len(job.store_names),
                "output_root": str(job.output_root),
                "screenshot_root": str(job.screenshot_root),
                "screenshot_dir": str(
                    lazada_bill_detail.screenshot_folder(
                        job.screenshot_root, job.country_name, job.start_date, job.end_date
                    )
                ),
            },
        )

    @requires_internal_runtime
    def start_bigseller_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        """启动 BigSeller 产品/库存同步任务（异步）。"""
        try:
            request_payload = self._payload_with_account(payload, "bigseller")
            job = validate_bigseller_sync_payload(request_payload)
            self.config_store.save({"output_dir": str(job.output_dir)})
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            result = run_bigseller_sync(job, progress)
            return {
                "processed_count": result.processed_count,
                "success_count": result.success_count,
                "failed_count": result.failed_count,
                "round_count": result.round_count,
                "output_file": str(result.output_file),
                "output_dir": str(result.output_dir),
            }

        sync_type_name = "产品" if job.sync_type == "product" else "库存"
        status_name = "售完" if job.listing_status == "soldOut" else "在售"
        return self.tasks.start(f"BigSeller{status_name}{sync_type_name}同步", runner, tool="bigseller_sync")

    @requires_internal_runtime
    def start_bigseller_item_id_query(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按 SKU（含子SKU）模糊查询 Views 最高的商品 ID 并异步导出。"""
        try:
            request_payload = self._payload_with_account(payload, "bigseller")
            job = validate_bigseller_item_id_query_payload(request_payload)
            self.config_store.save({"output_dir": str(job.output_dir)})
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            return run_bigseller_item_id_query(job, progress)

        return self.tasks.start(
            "BS 商品ID查询", runner, tool="bigseller_item_id_query",
            context={"sku_count": len(job.skus)},
        )

    @requires_internal_runtime
    def inspect_bigseller_benchmark_file(self, source_file: str) -> dict[str, Any]:
        try:
            info = inspect_benchmark_workbook(str(source_file or "").strip())
            if not info["default_sheet"]:
                return {"ok": False, "error": "工作簿中未找到包含 SKU 数据的子表；请检查前 10 行的库存SKU编号或SKU表头"}
            return {
                "ok": True, "path": info["source_path"], "sheets": info["sheet_names"],
                "sheet_name": info["default_sheet"], "warnings": info.get("warnings", []),
            }
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "读取 Excel 失败，请确认文件可访问、未加密且格式正确"}

    @requires_internal_runtime
    def choose_bigseller_benchmark_file(self) -> dict[str, Any]:
        try:
            import webview

            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("Excel 文件 (*.xlsx;*.xls)",),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            selected = result[0] if isinstance(result, (list, tuple)) else result
            return self.inspect_bigseller_benchmark_file(str(selected))
        except Exception:
            return {"ok": False, "error": "打开文件选择器失败，请直接输入 Excel 完整路径"}

    @requires_internal_runtime
    def choose_bigseller_benchmark_checkpoint(self) -> dict[str, Any]:
        try:
            import webview

            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("BigSeller 对标进度 (*.sqlite3)",),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            selected = result[0] if isinstance(result, (list, tuple)) else result
            return {"ok": True, "path": str(Path(selected).resolve())}
        except Exception:
            return {"ok": False, "error": "打开文件选择器失败，请直接输入进度文件完整路径"}

    @requires_internal_runtime
    def start_bigseller_sku_benchmark(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request_payload = self._payload_with_account(payload, "bigseller")
            job = validate_bigseller_benchmark_payload(request_payload)
            self.config_store.save({"output_dir": str(job.output_dir)})
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "读取参数或 Excel 失败，请检查文件和账号配置"}

        def runner(progress):
            return run_bigseller_sku_benchmark(job, progress)

        return self.tasks.start(
            "滞销SKU爆款对标", runner, tool="bigseller_sku_benchmark",
            context={"source_file": str(job.source_file), "sheet_name": job.sheet_name,
                     "metric": job.metric, "output_dir": str(job.output_dir),
                     "resume_checkpoint": str(getattr(job, "resume_checkpoint", None) or "")},
        )

    @requires_internal_runtime
    def start_bigseller_link_query(self, payload: dict[str, Any]) -> dict[str, Any]:
        """批量查询 SKU 最早上架商品的店铺和链接。"""
        try:
            request_payload = self._payload_with_account(payload, "bigseller")
            job = validate_bigseller_link_query_payload(request_payload)
            self.config_store.save({"output_dir": str(job.output_dir)})
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "无法读取查询参数，请检查账号配置与输出目录"}
        return self.tasks.start(
            "BS 抓取链接", lambda progress: run_bigseller_link_query(job, progress),
            tool="bigseller_link_query", context={"sku_count": len(job.skus), "site": job.site},
        )

    @requires_internal_runtime
    def inspect_bigseller_claim_file(self, source_file: str) -> dict[str, Any]:
        try:
            source = str(source_file or "").strip()
            info = inspect_claim_workbook(source)
            if not info.get("sheet_name"):
                return {"ok": False, "error": "未找到包含 SKU 或主SKU 表头和有效数据的工作表"}
            return {"ok": True, "path": str(Path(source).expanduser().resolve()), **info}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "读取 Excel 失败，请确认文件可访问、未加密且格式正确"}

    @requires_internal_runtime
    def choose_bigseller_claim_file(self) -> dict[str, Any]:
        try:
            import webview

            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("Excel 文件 (*.xlsx)",),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            selected = result[0] if isinstance(result, (list, tuple)) else result
            return self.inspect_bigseller_claim_file(str(selected))
        except Exception:
            return {"ok": False, "error": "打开文件选择器失败，请直接输入 Excel 完整路径"}

    @requires_internal_runtime
    def choose_bigseller_claim_checkpoint(self) -> dict[str, Any]:
        try:
            import webview

            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("新品认领查询进度 (*.sqlite3)",),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            selected = result[0] if isinstance(result, (list, tuple)) else result
            return {"ok": True, "path": str(Path(selected).resolve())}
        except Exception:
            return {"ok": False, "error": "打开文件选择器失败，请直接输入进度文件完整路径"}

    @requires_internal_runtime
    def start_bigseller_claim_query(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            job = validate_bigseller_claim_payload(self._payload_with_account(payload, "bigseller"))
            self.config_store.save({"output_dir": str(job.output_dir)})
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "读取参数或 Excel 失败，请检查文件和账号配置"}

        return self.tasks.start(
            "新品认领时间查询", lambda progress: run_bigseller_claim_query(job, progress),
            tool="bigseller_claim_query",
            context={"source_file": str(job.source_file), "sheet_name": job.sheet_name,
                     "site": job.site, "listing_scope": job.listing_scope,
                     "output_dir": str(job.output_dir),
                     "resume_checkpoint": str(job.resume_checkpoint or "")},
        )

    def start_echotik_collection(self, payload: dict[str, Any]) -> dict[str, Any]:
        """启动 EchoTik 商品达人采集任务（异步）。"""
        try:
            request_payload = self._payload_with_account(payload, "echotik")
            job = validate_echotik_payload(request_payload)
            self.config_store.save({"output_dir": str(job.output_dir)})
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            result = run_echotik_collection(job, progress)
            return {
                "product_candidate_count": result.product_candidate_count,
                "product_count": result.product_count,
                "creator_source_count": result.creator_source_count,
                "creator_count": result.creator_count,
                "creator_filtered_count": result.creator_filtered_count,
                "creator_missing_metric_count": result.creator_missing_metric_count,
                "failed_product_count": result.failed_product_count,
                "output_file": str(result.output_file),
                "output_dir": str(result.output_dir),
                "products_preview": result.products_preview,
                "creators_preview": result.creators_preview,
            }

        return self.tasks.start("EchoTik 商品达人采集", runner, tool="echotik_collect")

    def get_echotik_filter_options(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """返回 EchoTik 泰国商品类目和官方推荐筛选区间。"""
        try:
            request_payload = self._payload_with_account(payload, "echotik")
            cache_key = str(request_payload.get("username") or "").casefold()
            cached = self._echotik_filter_cache.get(cache_key)
            now = time.monotonic()
            if cached and now - cached[0] < 1800:
                return {"ok": True, **cached[1], "cached": True}
            options = get_echotik_filter_options(
                str(request_payload.get("username") or ""),
                str(request_payload.get("password") or ""),
            )
            self._echotik_filter_cache[cache_key] = (now, options)
            return {"ok": True, **options, "cached": False}
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def choose_client_path(self) -> dict[str, Any]:
        """打开文件选择器选择紫鸟客户端。"""
        try:
            import webview

            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("可执行文件 (*.exe)",),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            selected = result[0] if isinstance(result, (list, tuple)) else result
            return {"ok": True, "path": str(selected)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def choose_driver_path(self) -> dict[str, Any]:
        """打开目录选择器选择驱动目录。"""
        try:
            import webview

            result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            if not result:
                return {"ok": False, "cancelled": True}
            selected = result[0] if isinstance(result, (list, tuple)) else result
            return {"ok": True, "path": str(selected)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_sample_registration_config(self) -> dict[str, Any]:
        from backend.services.sample_registration_config import load_config
        cfg = load_config()
        from backend.services.sample_registration_config import _config_to_dict
        return {"ok": True, "config": _config_to_dict(cfg, reveal_secret=False)}

    def save_sample_registration_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        from backend.services.sample_registration_config import (
            SampleRegistrationConfig, SampleGroup, load_config, parse_bool, save_config, validate_config,
        )
        try:
            existing = load_config()
            data = payload or {}
            groups_raw = data.get("groups") or []
            groups = []
            for g in groups_raw:
                if not isinstance(g, dict):
                    continue
                stores_raw = g.get("stores") or []
                if isinstance(stores_raw, str):
                    stores = [s for s in stores_raw.splitlines() if s.strip()]
                elif isinstance(stores_raw, list):
                    stores = [str(s) for s in stores_raw]
                else:
                    stores = []
                groups.append(SampleGroup(
                    leader=str(g.get("leader") or ""),
                    target_sheet=str(g.get("target_sheet") or ""),
                    output_file_name=str(g.get("output_file_name") or ""),
                    stores=stores,
                ))
            cfg = SampleRegistrationConfig(
                target_doc_id=str(data.get("target_doc_id") or existing.target_doc_id or ""),
                enable_target_update=parse_bool(data.get("enable_target_update"), existing.enable_target_update),
                dingtalk_operator_name=str(
                    data.get("dingtalk_operator_name")
                    or existing.dingtalk_operator_name
                    or ""
                ),
                output_dir=str(data.get("output_dir") or existing.output_dir or ""),
                groups=groups if groups else existing.groups,
            )
            errors = validate_config(cfg)
            if errors:
                return {"ok": False, "error": "；".join(errors)}
            binding_error = _sample_operator_binding_error(self.config_store.load(), cfg)
            if binding_error:
                return {"ok": False, "error": binding_error}
            save_config(cfg)
            from backend.services.sample_registration_config import _config_to_dict
            return {"ok": True, "config": _config_to_dict(cfg, reveal_secret=False)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_sample_store_match(self, payload: dict[str, Any]) -> dict[str, Any]:
        from backend.services.sample_registration_config import load_config, SampleRegistrationConfig, SampleGroup, parse_bool
        from backend.services.sample_registration import check_store_matches
        try:
            request_payload = self._payload_with_account(payload, "mabang")
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        config_data = (payload or {}).get("config")
        if isinstance(config_data, dict):
            existing = load_config()
            groups_raw = config_data.get("groups") or []
            groups = []
            for g in groups_raw:
                if not isinstance(g, dict):
                    continue
                stores_raw = g.get("stores") or []
                if isinstance(stores_raw, str):
                    stores = [s for s in stores_raw.splitlines() if s.strip()]
                elif isinstance(stores_raw, list):
                    stores = [str(s) for s in stores_raw]
                else:
                    stores = []
                groups.append(SampleGroup(
                    leader=str(g.get("leader") or ""),
                    target_sheet=str(g.get("target_sheet") or ""),
                    output_file_name=str(g.get("output_file_name") or ""),
                    stores=stores,
                ))
            cfg = SampleRegistrationConfig(
                target_doc_id=str(config_data.get("target_doc_id") or ""),
                enable_target_update=parse_bool(config_data.get("enable_target_update"), False),
                dingtalk_operator_name=str(
                    config_data.get("dingtalk_operator_name")
                    or existing.dingtalk_operator_name
                    or ""
                ),
                groups=groups,
            )
        else:
            cfg = load_config()

        username = request_payload.get("username", "")
        password = request_payload.get("password", "")

        def runner(progress):
            summary = check_store_matches(cfg, username, password, progress=progress)
            return {
                "resolved_count": summary.resolved_count,
                "unresolved_count": summary.unresolved_count,
                "resolved_rows": summary.resolved_rows,
                "unresolved_rows": summary.unresolved_rows,
            }

        return self.tasks.start("网红寄样登记-店铺匹配", runner)

    def start_sample_registration(self, payload: dict[str, Any]) -> dict[str, Any]:
        from backend.services.sample_registration_config import load_config, SampleRegistrationConfig, SampleGroup, parse_bool
        from backend.services.sample_registration import run_registration

        # 并发限制：同时只允许一个正式任务
        for task in self.tasks._tasks.values():
            if task.name == "网红寄样登记" and task.status in ("pending", "running"):
                return {"ok": False, "error": "已有一个寄样登记任务在运行中，请等待完成后再试"}

        try:
            request_payload = self._payload_with_account(payload, "mabang")
        except AccountRequiredError as exc:
            return {"ok": False, "requires_account": True, "vendor": exc.vendor, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        config_data = (payload or {}).get("config")
        if isinstance(config_data, dict):
            existing = load_config()
            groups_raw = config_data.get("groups") or []
            groups = []
            for g in groups_raw:
                if not isinstance(g, dict):
                    continue
                stores_raw = g.get("stores") or []
                if isinstance(stores_raw, str):
                    stores = [s for s in stores_raw.splitlines() if s.strip()]
                elif isinstance(stores_raw, list):
                    stores = [str(s) for s in stores_raw]
                else:
                    stores = []
                groups.append(SampleGroup(
                    leader=str(g.get("leader") or ""),
                    target_sheet=str(g.get("target_sheet") or ""),
                    output_file_name=str(g.get("output_file_name") or ""),
                    stores=stores,
                ))
            cfg = SampleRegistrationConfig(
                target_doc_id=str(config_data.get("target_doc_id") or ""),
                enable_target_update=parse_bool(config_data.get("enable_target_update"), False),
                dingtalk_operator_name=str(
                    config_data.get("dingtalk_operator_name")
                    or existing.dingtalk_operator_name
                    or ""
                ),
                output_dir=str(config_data.get("output_dir") or ""),
                groups=groups,
            )
        else:
            cfg = load_config()

        start_date = str((payload or {}).get("start_date") or "").strip()
        end_date = str((payload or {}).get("end_date") or start_date).strip()
        allow_unmatched = bool((payload or {}).get("allow_unmatched", False))
        username = request_payload.get("username", "")
        password = request_payload.get("password", "")
        try:
            dingtalk_app_key, dingtalk_app_secret, dingtalk_user_id = _sample_dingtalk_runtime(
                self.config_store.load(),
                cfg,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        def runner(progress):
            result = run_registration(
                cfg, start_date, end_date,
                allow_unmatched=allow_unmatched,
                username=username,
                password=password,
                dingtalk_app_key=dingtalk_app_key,
                dingtalk_app_secret=dingtalk_app_secret,
                dingtalk_user_id=dingtalk_user_id,
                progress=progress,
            )
            return {
                "processed_count": result.processed_count,
                "online_append_count": result.online_append_count,
                "unmatched_store_count": result.unmatched_store_count,
                "resolved_count": result.resolved_count,
                "group_count": result.group_count,
                "output_directory": result.output_directory,
                "output_files": result.output_files,
            }

        return self.tasks.start("网红寄样登记", runner)

    def _payload_with_account(self, payload: dict[str, Any] | None, vendor: str) -> dict[str, Any]:
        settings = self.config_store.load()
        request_payload = dict(payload or {})
        account_id = str(request_payload.get("account_id") or settings.active_account_ids.get(vendor) or "").strip()
        account = next(
            (
                item
                for item in settings.accounts
                if item.id == account_id and item.vendor == vendor
            ),
            None,
        )
        if account is None:
            raise AccountRequiredError(vendor, f"请先绑定并选择{_vendor_label(vendor)}账号")
        request_payload["account_id"] = account.id
        request_payload["username"] = account.username
        request_payload["password"] = account.password
        if vendor == "ziniao":
            request_payload["company"] = str(
                account.extra.get("company") or account.name or ""
            ).strip()
        if vendor == "bigseller":
            request_payload["captcha_username"] = settings.captcha_username
            request_payload["captcha_password"] = settings.captcha_password
        return request_payload

    def _validated_update_installer(self, installer_path: str) -> Path:
        updates_dir = (self.config_store.local_data_dir / "updates").resolve()
        candidate = Path(str(installer_path or "")).resolve(strict=True)
        try:
            candidate.relative_to(updates_dir)
        except ValueError as exc:
            raise ValueError("安装包不在受信任的更新目录中") from exc
        if candidate.suffix.lower() != ".exe":
            raise ValueError("更新文件不是 Windows 安装程序")

        settings = self.config_store.load()
        update = check_update_manifest(settings.update_manifest_url, APP_VERSION)
        if update.error:
            raise ValueError(f"重新校验发布清单失败：{update.error}")
        if not update.sha256:
            raise ValueError("发布清单缺少安装包 SHA256")
        if calculate_sha256(candidate) != update.sha256.upper():
            raise ValueError("安装包 SHA256 校验失败，已阻止运行")
        return candidate

    def get_lazada_ads_monthly_info(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            default_month = lazada_ads_monthly.get_default_month()
            default_out = str(lazada_ads_monthly.default_output_root())
            cli_status = {"available": False, "profile": "", "error": None}
            try:
                cli = ZiniaoMonthlyCli()
                cli.ready()
                cli_status["available"] = True
                cli_status["profile"] = cli.profile
            except Exception as exc:
                cli_status["error"] = str(exc)

            return {
                "ok": True,
                "default_month": default_month,
                "default_output_dir": default_out,
                "countries": [
                    {"code": "AUTO", "name": "自动识别"},
                    {"code": "PH", "name": "菲律宾 (PHP)"},
                    {"code": "MY", "name": "马来西亚 (MYR)"},
                    {"code": "TH", "name": "泰国 (THB)"},
                    {"code": "ID", "name": "印尼 (IDR)"},
                    {"code": "VN", "name": "越南 (VND)"},
                    {"code": "SG", "name": "新加坡 (SGD)"},
                ],
                "cli_status": cli_status,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def preview_lazada_ads_monthly(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            service = lazada_ads_monthly.LazadaAdsMonthlyService()
            validated = lazada_ads_monthly.validate_ads_monthly_request(payload)
            res = service.preview_stores(validated)
            return {"ok": True, **res}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_lazada_ads_monthly(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lazada_ads_monthly_lock:
            try:
                if self._lazada_ads_monthly_active:
                    raise ValueError("已有 Lazada 广告月报任务正在运行，请完成后再开始")
                validated = lazada_ads_monthly.validate_ads_monthly_request(payload)
                self._lazada_ads_monthly_active = True
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def release() -> None:
            with self._lazada_ads_monthly_lock:
                self._lazada_ads_monthly_active = False

        def runner(progress_cb):
            try:
                service = lazada_ads_monthly.LazadaAdsMonthlyService()
                return service.run_batch(validated, progress_cb=lambda event: progress_cb(
                    ' '.join(str(event.get(k) or '') for k in ('type', 'store_name', 'step', 'status', 'note')).strip()))
            finally:
                release()

        try:
            task = self.tasks.start(
                "Lazada 广告月报",
                runner,
                tool="lazada_ads_monthly",
                context={"month": validated["month"]},
                exclusive=True,
            )
            if not task.get("ok"):
                release()
            return task
        except Exception as exc:
            release()
            return {"ok": False, "error": str(exc)}

    def retry_lazada_ads_monthly(self, payload: dict[str, Any]) -> dict[str, Any]:
        manifest_path = (payload or {}).get("manifest_path")
        if not manifest_path or not Path(manifest_path).is_file():
            return {"ok": False, "error": "请提供有效的批次 manifest 文件路径"}

        with self._lazada_ads_monthly_lock:
            try:
                if self._lazada_ads_monthly_active:
                    raise ValueError("已有 Lazada 广告月报任务正在运行，请完成后再重试")
                self._lazada_ads_monthly_active = True
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        def release() -> None:
            with self._lazada_ads_monthly_lock:
                self._lazada_ads_monthly_active = False

        def runner(progress_cb):
            try:
                service = lazada_ads_monthly.LazadaAdsMonthlyService()
                return service.retry_batch(manifest_path, progress_cb=lambda event: progress_cb(
                    ' '.join(str(event.get(k) or '') for k in ('type', 'store_name', 'step', 'status', 'note')).strip()))
            finally:
                release()

        try:
            task = self.tasks.start(
                "Lazada 广告月报重试",
                runner,
                tool="lazada_ads_monthly",
                context={"manifest_path": manifest_path},
                exclusive=True,
            )
            if not task.get("ok"):
                release()
            return task
        except Exception as exc:
            release()
            return {"ok": False, "error": str(exc)}


class AccountRequiredError(ValueError):
    def __init__(self, vendor: str, message: str) -> None:
        super().__init__(message)
        self.vendor = vendor


def _vendor_label(vendor: str) -> str:
    return {
        "mabang": "马帮",
        "ziniao": "紫鸟",
        "bigseller": "BigSeller",
        "echotik": "EchoTik",
    }.get(str(vendor or "").strip().lower(), str(vendor or "").strip() or "业务")


def _close_app_after_update_launch() -> None:
    time.sleep(1.2)
    try:
        import webview

        if webview.windows:
            webview.windows[0].destroy()
    except Exception:
        pass


def _mask_config_value(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 6:
        return text[:1] + "***"
    return f"{text[:3]}****{text[-3:]}"


def _preferred_bill_sheet_name(names: list[str], profile: Any) -> str:
    """下拉默认 Sheet：优先与国家同名（旧脚本默认表名：泰国/菲律宾/马来/印尼/越南）。"""

    def normalized(value: Any) -> str:
        return " ".join(str(value or "").split()).strip().casefold()

    aliases = [normalized(alias) for alias in lazada_bill_detail.sheet_name_aliases(profile)]
    aliases = [alias for alias in aliases if alias]
    for alias in aliases:
        for name in names:
            if normalized(name) == alias:
                return name
    for alias in aliases:
        for name in names:
            if alias in normalized(name):
                return name
    return ""


def _resolve_dingtalk_user_id(settings: AppSettings, operator_name: str) -> str:
    target = " ".join(str(operator_name or "").split()).strip()
    if not target:
        return ""
    for binding in settings.dingtalk.users:
        name = " ".join(str(binding.name or "").split()).strip()
        if name == target or name.casefold() == target.casefold():
            return str(binding.user_id or "").strip()
    return ""


def _sample_operator_binding_error(settings: AppSettings, cfg: Any) -> str:
    if not getattr(cfg, "enable_target_update", False):
        return ""
    operator_name = " ".join(str(getattr(cfg, "dingtalk_operator_name", "") or "").split()).strip()
    if not operator_name:
        return ""
    if _resolve_dingtalk_user_id(settings, operator_name):
        return ""
    return f"操作人「{operator_name}」不在钉钉用户配置表中，请检查姓名是否和配置表一致"


def _sample_dingtalk_runtime(settings: AppSettings, cfg: Any) -> tuple[str, str, str]:
    if not getattr(cfg, "enable_target_update", False):
        return "", "", ""
    app_key = str(settings.dingtalk.app_key or "").strip()
    app_secret = str(settings.dingtalk.app_secret or "")
    operator_name = " ".join(str(getattr(cfg, "dingtalk_operator_name", "") or "").split()).strip()
    missing: list[str] = []
    if not app_key:
        missing.append("钉钉 AppKey")
    if not app_secret:
        missing.append("钉钉 AppSecret")
    if not operator_name:
        missing.append("操作人姓名")
    if missing:
        raise ValueError("开启钉钉在线表格同步前，请先在设置里补齐：" + "、".join(missing))
    user_id = _resolve_dingtalk_user_id(settings, operator_name)
    if not user_id:
        raise ValueError(f"操作人「{operator_name}」不在钉钉用户配置表中，请检查姓名是否和配置表一致")
    return app_key, app_secret, user_id


def _settings_payload(settings: AppSettings) -> dict[str, Any]:
    return {
        "output_dir": settings.output_dir,
        "rows_per_page": settings.rows_per_page,
        "update_manifest_url": settings.update_manifest_url,
        "captcha_username": settings.captcha_username,
        "captcha_password_configured": bool(settings.captcha_password),
        "dingtalk": {
            "app_key_configured": bool(str(settings.dingtalk.app_key or "").strip()),
            "app_key_masked": _mask_config_value(settings.dingtalk.app_key),
            "app_secret_configured": bool(str(settings.dingtalk.app_secret or "")),
            "user_count": len(settings.dingtalk.users),
            "operator_names": [binding.name for binding in settings.dingtalk.users],
        },
        "sales_group_ids": settings.sales_group_ids,
        "income_expense_category_ids": settings.income_expense_category_ids,
        "income_expense_warehouse_keys": settings.income_expense_warehouse_keys,
        "active_account_ids": settings.active_account_ids,
    }


def _account_state_payload(settings: AppSettings) -> dict[str, Any]:
    return {
        "accounts": [
            {
                "id": account.id,
                "vendor": account.vendor,
                "name": account.name,
                "username": account.username,
                "extra": account.extra,
            }
            for account in settings.accounts
        ],
        "active_account_ids": settings.active_account_ids,
    }
