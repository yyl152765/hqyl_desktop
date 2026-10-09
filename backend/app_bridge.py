from __future__ import annotations

import os
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.config_store import AppSettings, BoundAccount, ConfigStore, default_date_range
from backend.sidebar_preferences import SidebarPreferencesStore
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
from backend.task_manager import TaskManager
from backend.core.ziniao_browser import ZiniaoBrowser, account_profile as ziniao_account_profile, runtime_paths as ziniao_runtime_paths
from backend.services import temu_on_sale_export as temu_export
from backend.services import balance_statistics
from backend.services.temu_on_sale_gateway import TemuOnSaleGateway
from backend.update_service import (
    calculate_sha256,
    check_update_manifest,
    download_update_package,
    result_payload,
)


APP_VERSION = "0.2.66"
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
        self._temu_operation_lock = threading.Lock()
        self._temu_running_accounts: set[str] = set()
        self._temu_export_lock = threading.RLock()
        self._temu_export_active = False
        self._temu_export_catalog: dict[str, Any] = {}
        self._temu_export_previews: dict[str, dict[str, Any]] = {}
        self._balance_statistics_lock = threading.RLock()
        self._balance_statistics_active = False

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
        return {
            "ok": True,
            "app": {
                "name": APP_NAME,
                "version": APP_VERSION,
            },
            "date_range": default_date_range(),
            "settings": _settings_payload(settings),
            "account_state": _account_state_payload(settings),
            "group_sales_report": {
                "groups": get_group_options(),
            },
            "mabang_income_expense_report": {
                "categories": get_income_expense_category_options(),
                "warehouses": get_income_expense_warehouse_options(),
                "date_range": income_expense_default_period(),
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
                    "key": "sku_inventory_query",
                    "name": "SKU库存与可售天数查询",
                    "description": "按开发人员和SKU创建时间查询所有仓库有库存、未发货或在途的明细。",
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
                    "key": "shopee_ads",
                    "name": "Shopee 广告充值",
                    "description": "多站点店铺广告充值，支持印尼、泰国、菲律宾、越南、马来。",
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
                    "key": "lazada_monthly_report",
                    "name": "Lazada 月度账单下载",
                    "description": "按月份下载泰国、马来西亚和菲律宾店铺的月度报告。",
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
                    "key": "echotik_collect",
                    "name": "EchoTik 商品达人采集",
                    "description": "固定泰国站，按关键词采集商品库和商品达人列表。",
                    "status": "ready",
                    "vendor": "echotik",
                },
            ],
        }

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.config_store.save(payload or {})
        return {"ok": True, "settings": _settings_payload(settings)}

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

    def choose_output_dir(self, current_dir: str = "") -> dict[str, Any]:
        try:
            import webview

            directory = current_dir or self.config_store.load().output_dir
            result = webview.windows[0].create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=directory if directory else None,
            )
            if not result:
                return {"ok": False, "cancelled": True}
            selected = result[0] if isinstance(result, (list, tuple)) else result
            settings = self.config_store.save({"output_dir": str(selected)})
            return {"ok": True, "path": str(selected), "settings": _settings_payload(settings)}
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
            }

        return self.tasks.start("菲律宾各组商品销量报表", runner, tool="group_sales")

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
        settings = self.config_store.load()
        return {
            "ok": True,
            "settings": _settings_payload(settings),
            "account_state": _account_state_payload(settings),
            "basis": "实际体积（长 × 宽 × 高 × 数量 × 0.000001）",
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
                "account_id": str(request_payload.get("account_id") or ""),
            },
        )

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

    def get_lazada_withdrawal_statistics_info(self) -> dict[str, Any]:
        """返回 Lazada 提现统计的国家、默认上月日期和紫鸟运行路径。"""
        try:
            paths = resolve_lazada_withdrawal_runtime_paths()
            return {
                "ok": True,
                "countries": lazada_withdrawal_country_options(),
                "period": lazada_withdrawal_default_period(),
                "client_path": paths.get("client_path", ""),
                "webdriver_path": paths.get("webdriver_path", ""),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _temu_export_latest_path(self) -> Path:
        return self.config_store.local_data_dir / "temu_on_sale_export" / "latest.json"

    def get_balance_statistics_info(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Load local defaults without logging in or querying the store catalog."""
        try:
            defaults = balance_statistics.get_defaults((payload or {}).get("platform", ""))
            try:
                paths = ziniao_runtime_paths()
            except Exception:
                # Runtime discovery is optional until the user starts a task.
                paths = {}
            return {"ok": True, **defaults, "output_dir": self.config_store.load().output_dir,
                    "client_path": paths.get("client_path", ""), "webdriver_path": paths.get("webdriver_path", "")}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

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
            task = self.tasks.start(f"{title} 余额统计", runner, tool=f"{platform}_balance_statistics", context=context)
            if not task.get("ok"):
                release()
            return task
        except Exception as exc:
            release()
            return {"ok": False, "error": str(exc)}

    def start_balance_statistics(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._start_balance_statistics(payload, retry=False)

    def retry_balance_statistics(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._start_balance_statistics(payload, retry=True)

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

    def save_shopee_ads_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存 Shopee 充值配置到 AppData（按站点分别存储）。"""
        try:
            site_code = str((payload or {}).get("site_code") or "id").strip()
            from backend.services.shopee_ads_recharge import save_site_config

            saved_path = save_site_config(site_code, payload or {})
            return {"ok": True, "saved_path": saved_path}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

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

    def start_lazada_withdrawal_statistics(self, payload: dict[str, Any]) -> dict[str, Any]:
        """启动 Lazada 提现统计任务（异步）。"""
        try:
            settings = self.config_store.load()
            request_payload = self._payload_with_account(payload, "ziniao")
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
