from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any


APP_NAME = "HQYLAutomation"
DEFAULT_ROWS_PER_PAGE = 500
DEFAULT_UPDATE_MANIFEST_URL = "http://rpa.whhqyl.com.cn/hqyl/latest.json"
LEGACY_DINGTALK_CONFIG_ENV = "HQYL_LEGACY_DINGTALK_CONFIG"
LEGACY_UPDATE_MANIFEST_URLS = {
    "http://47.112.20.68/hqyl/latest.json",
}


@dataclass(frozen=True)
class BoundAccount:
    id: str
    vendor: str
    name: str
    username: str
    password: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DingTalkUserBinding:
    name: str
    user_id: str


@dataclass
class DingTalkSettings:
    app_key: str = ""
    app_secret: str = ""
    users: list[DingTalkUserBinding] = field(default_factory=list)


@dataclass
class AppSettings:
    output_dir: str = ""
    rows_per_page: int = DEFAULT_ROWS_PER_PAGE
    update_manifest_url: str = DEFAULT_UPDATE_MANIFEST_URL
    captcha_username: str = ""
    captcha_password: str = ""
    dingtalk: DingTalkSettings = field(default_factory=DingTalkSettings)
    sales_group_ids: list[str] = field(default_factory=list)
    income_expense_category_ids: list[str] = field(default_factory=list)
    income_expense_warehouse_keys: list[str] = field(default_factory=list)
    accounts: list[BoundAccount] = field(default_factory=list)
    active_account_ids: dict[str, str] = field(default_factory=dict)
    vietnam_last_manifest_path: str = ""


def _base_app_data_dir() -> Path:
    return Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME


def _base_local_data_dir() -> Path:
    return Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME


def desktop_path() -> Path:
    desktop = Path.home() / "Desktop"
    return desktop if desktop.exists() else Path.cwd()


def default_date_range() -> dict[str, str]:
    end = date.today()
    start = end - timedelta(days=30)
    return {
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": end.strftime("%Y-%m-%d"),
    }


class ConfigStore:
    """Small JSON config store for desktop settings."""

    def __init__(self, config_path: Path | None = None) -> None:
        self._explicit_config_path = config_path is not None
        self.config_dir = config_path.parent if config_path is not None else _base_app_data_dir()
        self.local_data_dir = _base_local_data_dir()
        self.logs_dir = self.local_data_dir / "logs"
        self.config_path = config_path or self.config_dir / "settings.json"

    def ensure_dirs(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> AppSettings:
        self.ensure_dirs()
        if not self.config_path.exists():
            legacy_dingtalk = _load_legacy_dingtalk_credentials(
                include_source_fallback=not self._explicit_config_path,
            )
            settings = AppSettings(
                output_dir=str(desktop_path()),
                dingtalk=_coerce_dingtalk_settings(legacy_dingtalk, seed_users=True),
            )
            self.save(settings)
            return settings
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8-sig")) or {}
        except Exception:
            payload = {}
        accounts = _coerce_accounts(payload.get("accounts"))
        raw_dingtalk = payload.get("dingtalk")
        dingtalk_seed_users = not isinstance(raw_dingtalk, dict) or not raw_dingtalk.get("users")
        dingtalk = _coerce_dingtalk_settings(raw_dingtalk, seed_users=dingtalk_seed_users)
        migrated_dingtalk = False
        if not dingtalk.app_key or not dingtalk.app_secret:
            legacy_dingtalk = _load_legacy_dingtalk_credentials(
                include_source_fallback=not self._explicit_config_path,
            )
            legacy_app_key = str(legacy_dingtalk.get("app_key") or "").strip()
            legacy_app_secret = str(legacy_dingtalk.get("app_secret") or "")
            if legacy_app_key and legacy_app_secret:
                dingtalk = DingTalkSettings(
                    app_key=dingtalk.app_key or legacy_app_key,
                    app_secret=dingtalk.app_secret or legacy_app_secret,
                    users=dingtalk.users,
                )
                migrated_dingtalk = True
        legacy_username = str(payload.get("username") or "").strip()
        legacy_password = str(payload.get("password") or "")
        if legacy_username and legacy_password and not any(account.vendor == "mabang" for account in accounts):
            accounts.append(
                BoundAccount(
                    id="legacy-mabang",
                    vendor="mabang",
                    name=legacy_username,
                    username=legacy_username,
                    password=legacy_password,
                )
            )
        settings = AppSettings(
            output_dir=str(payload.get("output_dir") or desktop_path()).strip(),
            rows_per_page=_coerce_rows_per_page(payload.get("rows_per_page")),
            update_manifest_url=_coerce_update_manifest_url(payload.get("update_manifest_url")),
            captcha_username=str(payload.get("captcha_username") or "").strip(),
            captcha_password=str(payload.get("captcha_password") or ""),
            dingtalk=dingtalk,
            sales_group_ids=_coerce_string_list(payload.get("sales_group_ids")),
            income_expense_category_ids=_coerce_string_list(
                payload.get("income_expense_category_ids")
            ),
            income_expense_warehouse_keys=_coerce_string_list(
                payload.get("income_expense_warehouse_keys")
            ),
            accounts=accounts,
            active_account_ids=_coerce_active_account_ids(payload.get("active_account_ids"), accounts),
            vietnam_last_manifest_path=str(payload.get("vietnam_last_manifest_path") or "").strip(),
        )
        if migrated_dingtalk:
            self._write_settings(settings)
        return settings

    def save(self, settings: AppSettings | dict[str, Any]) -> AppSettings:
        self.ensure_dirs()
        if isinstance(settings, dict):
            current = self.load()
            accounts = _coerce_accounts(settings.get("accounts", current.accounts))
            dingtalk = _merge_dingtalk_settings(settings.get("dingtalk"), current.dingtalk)
            settings = AppSettings(
                output_dir=str(settings.get("output_dir", current.output_dir) or desktop_path()).strip(),
                rows_per_page=_coerce_rows_per_page(settings.get("rows_per_page", current.rows_per_page)),
                update_manifest_url=_coerce_update_manifest_url(
                    settings.get("update_manifest_url", current.update_manifest_url)
                ),
                captcha_username=str(settings.get("captcha_username", current.captcha_username) or "").strip(),
                captcha_password=str(settings.get("captcha_password", current.captcha_password) or ""),
                dingtalk=dingtalk,
                sales_group_ids=_coerce_string_list(settings.get("sales_group_ids", current.sales_group_ids)),
                income_expense_category_ids=_coerce_string_list(
                    settings.get(
                        "income_expense_category_ids",
                        current.income_expense_category_ids,
                    )
                ),
                income_expense_warehouse_keys=_coerce_string_list(
                    settings.get(
                        "income_expense_warehouse_keys",
                        current.income_expense_warehouse_keys,
                    )
                ),
                accounts=accounts,
                active_account_ids=_coerce_active_account_ids(
                    settings.get("active_account_ids", current.active_account_ids),
                    accounts,
                ),
                vietnam_last_manifest_path=str(settings.get("vietnam_last_manifest_path", current.vietnam_last_manifest_path) or "").strip(),
            )
        if not settings.output_dir:
            settings.output_dir = str(desktop_path())
        settings.dingtalk = _coerce_dingtalk_settings(settings.dingtalk)
        settings.accounts = _coerce_accounts(settings.accounts)
        settings.active_account_ids = _coerce_active_account_ids(settings.active_account_ids, settings.accounts)
        self._write_settings(settings)
        return settings

    def _write_settings(self, settings: AppSettings) -> None:
        self.config_path.write_text(
            json.dumps(asdict(settings), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _legacy_dingtalk_config_candidates(*, include_source_fallback: bool) -> list[Path]:
    candidates: list[Path] = []
    env_path = str(os.getenv(LEGACY_DINGTALK_CONFIG_ENV) or "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser())

    if getattr(sys, "frozen", False):
        install_root = Path(sys.executable).resolve().parent
        candidates.extend(
            [
                install_root / "_internal" / "config" / "config.yaml",
                install_root / "config" / "config.yaml",
                install_root
                / "_internal"
                / "main"
                / "shopee"
                / "config"
                / "Shopee马来广告充值.yaml",
            ]
        )
    elif include_source_fallback:
        superbrowser_root = Path(__file__).resolve().parents[2] / "superbrowser_process"
        candidates.extend(
            [
                superbrowser_root / "config" / "config.yaml",
                superbrowser_root
                / "main"
                / "shopee"
                / "config"
                / "Shopee马来广告充值.yaml",
            ]
        )

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        normalized = str(path.resolve())
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(path)
    return unique


def _load_legacy_dingtalk_credentials(*, include_source_fallback: bool) -> dict[str, str]:
    try:
        import yaml
    except ImportError:
        return {}

    for path in _legacy_dingtalk_config_candidates(
        include_source_fallback=include_source_fallback
    ):
        if not path.is_file():
            continue
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        dingtalk = payload.get("dingtalk") or {}
        if not isinstance(dingtalk, dict):
            continue
        app = dingtalk.get("app") or {}
        app_rpa = app.get("rpa") if isinstance(app, dict) else {}
        doc_config = dingtalk.get("doc_config") or {}
        for source in (app_rpa, doc_config):
            if not isinstance(source, dict):
                continue
            app_key = str(source.get("app_key") or "").strip()
            app_secret = str(source.get("app_secret") or "")
            if app_key and app_secret:
                return {"app_key": app_key, "app_secret": app_secret}
    return {}


def _coerce_rows_per_page(value: Any) -> int:
    try:
        rows = int(value)
    except (TypeError, ValueError):
        rows = DEFAULT_ROWS_PER_PAGE
    return max(50, min(rows, 1000))


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _coerce_update_manifest_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url or url in LEGACY_UPDATE_MANIFEST_URLS:
        return DEFAULT_UPDATE_MANIFEST_URL
    return url


def _coerce_accounts(value: Any) -> list[BoundAccount]:
    values = value if isinstance(value, (list, tuple)) else []
    result: list[BoundAccount] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, BoundAccount):
            account = item
        elif isinstance(item, dict):
            extra_raw = item.get("extra")
            account = BoundAccount(
                id=str(item.get("id") or "").strip(),
                vendor=str(item.get("vendor") or "").strip().lower(),
                name=str(item.get("name") or item.get("username") or "").strip(),
                username=str(item.get("username") or "").strip(),
                password=str(item.get("password") or ""),
                extra=extra_raw if isinstance(extra_raw, dict) else {},
            )
        else:
            continue
        if not account.id or not account.vendor or not account.username or not account.password or account.id in seen:
            continue
        seen.add(account.id)
        result.append(account)
    return result


def _coerce_dingtalk_settings(value: Any, *, seed_users: bool = False) -> DingTalkSettings:
    if isinstance(value, DingTalkSettings):
        users = _coerce_dingtalk_users(value.users)
        if seed_users and not users:
            users = _load_default_dingtalk_user_bindings()
        return DingTalkSettings(
            app_key=str(value.app_key or "").strip(),
            app_secret=str(value.app_secret or ""),
            users=users,
        )
    raw = value if isinstance(value, dict) else {}
    users = _coerce_dingtalk_users(raw.get("users"))
    if not users and (seed_users or "users" not in raw):
        users = _load_default_dingtalk_user_bindings()
    return DingTalkSettings(
        app_key=str(raw.get("app_key") or raw.get("appKey") or "").strip(),
        app_secret=str(raw.get("app_secret") or raw.get("appSecret") or ""),
        users=users,
    )


def _merge_dingtalk_settings(value: Any, current: DingTalkSettings) -> DingTalkSettings:
    if value is None:
        return _coerce_dingtalk_settings(current)
    raw = value if isinstance(value, dict) else {}
    app_key_update = str(raw.get("app_key") or raw.get("appKey") or "").strip()
    app_secret_update = str(raw.get("app_secret") or raw.get("appSecret") or "")
    users = current.users
    if "users" in raw:
        users = _coerce_dingtalk_users(raw.get("users"))
    return DingTalkSettings(
        app_key=app_key_update or current.app_key,
        app_secret=app_secret_update or current.app_secret,
        users=users,
    )


def _coerce_dingtalk_users(value: Any) -> list[DingTalkUserBinding]:
    if isinstance(value, dict):
        values = [{"name": name, "user_id": user_id} for name, user_id in value.items()]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = []
    result: list[DingTalkUserBinding] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, DingTalkUserBinding):
            binding = item
        elif isinstance(item, dict):
            binding = DingTalkUserBinding(
                name=str(item.get("name") or item.get("user_name") or item.get("username") or "").strip(),
                user_id=str(item.get("user_id") or item.get("userid") or item.get("userId") or "").strip(),
            )
        else:
            continue
        if not binding.name or not binding.user_id or binding.name in seen:
            continue
        seen.add(binding.name)
        result.append(binding)
    return result


def _load_default_dingtalk_user_bindings() -> list[DingTalkUserBinding]:
    for path in _default_user_map_candidates():
        if not path.exists():
            continue
        try:
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        except Exception:
            continue
        users = data.get("users", data) if isinstance(data, dict) else {}
        bindings = _coerce_dingtalk_users(users)
        if bindings:
            return bindings
    return []


def _default_user_map_candidates() -> list[Path]:
    candidates = [Path(__file__).resolve().parent / "resources" / "user_map.yaml"]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        candidates.append(Path(meipass) / "backend" / "resources" / "user_map.yaml")
        candidates.append(Path(meipass) / "config" / "user_map.yaml")
    candidates.append(Path.cwd() / "backend" / "resources" / "user_map.yaml")
    return candidates


def _coerce_active_account_ids(value: Any, accounts: list[BoundAccount]) -> dict[str, str]:
    requested = value if isinstance(value, dict) else {}
    by_vendor: dict[str, list[str]] = {}
    for account in accounts:
        by_vendor.setdefault(account.vendor, []).append(account.id)
    result: dict[str, str] = {}
    for vendor, account_ids in by_vendor.items():
        selected = str(requested.get(vendor) or "").strip()
        result[vendor] = selected if selected in account_ids else account_ids[0]
    return result
