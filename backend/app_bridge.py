from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.config_store import AppSettings, BoundAccount, ConfigStore, default_date_range
from backend.services.group_sales_report import (
    get_group_options,
    run_group_sales_report,
    validate_group_sales_payload,
)
from backend.services.captcha_service import query_ttshitu_account_info
from backend.services.purchase_log import run_purchase_log_query, validate_query_payload
from backend.services.bigseller_sync import run_bigseller_sync, validate_bigseller_sync_payload
from backend.services.shopee_ads_recharge import (
    get_site_options_list,
    resolve_runtime_paths,
    run_shopee_ads_recharge,
    validate_shopee_ads_payload,
)
from backend.task_manager import TaskManager
from backend.update_service import (
    calculate_sha256,
    check_update_manifest,
    download_update_package,
    result_payload,
)


APP_VERSION = "0.2.5"
APP_NAME = "寰球云联自动化平台"


class AppBridge:
    """Methods exposed to the H5 UI through pywebview."""

    def __init__(self) -> None:
        self.config_store = ConfigStore()
        self.tasks = TaskManager()
        self.update_tasks = TaskManager()

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
            "tools": [
                {
                    "key": "purchase_log",
                    "name": "SKU 采购日志查询",
                    "description": "按 SKU 查询马帮采购日志并导出 Excel。",
                    "status": "sample",
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
                    "key": "bigseller_sync",
                    "name": "BigSeller 同步",
                    "description": "通过 BigSeller 接口同步产品或库存，支持在售、售完状态。",
                    "status": "ready",
                    "vendor": "bigseller",
                },
                {
                    "key": "sample_registration",
                    "name": "网红寄样登记",
                    "description": "查询样品订单、导出 Excel、同步钉钉在线表格。",
                    "status": "ready",
                    "vendor": "mabang",
                }
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

        allowed_vendors = {"mabang", "ziniao", "bigseller"}
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
        request_payload["username"] = account.username
        request_payload["password"] = account.password
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
