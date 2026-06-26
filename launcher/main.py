from __future__ import annotations

import sys
import ctypes
from pathlib import Path


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1]


ROOT = project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app_bridge import AppBridge


def self_check() -> int:
    index_html = ROOT / "frontend" / "pages" / "group-sales.html"
    if not index_html.exists():
        raise RuntimeError(f"frontend entry not found: {index_html}")

    sample_html = ROOT / "frontend" / "pages" / "sample-registration.html"
    if not sample_html.exists():
        raise RuntimeError(f"sample registration page not found: {sample_html}")

    info = AppBridge().get_app_info()
    if not info.get("ok"):
        raise RuntimeError(f"bridge self-check failed: {info}")

    # Exercise the dynamic imports that PyInstaller cannot discover on its own.
    from backend.services.shopee_ads_recharge import SITE_OPTIONS, get_site_module

    for site in SITE_OPTIONS:
        module = get_site_module(site.code)
        if not hasattr(module, "run_ads_recharge"):
            raise RuntimeError(f"Shopee Ads module is incomplete: {site.module_name}")

    # Verify sample registration config can be loaded (no network).
    from backend.services.sample_registration_config import load_config, validate_config
    cfg = load_config()
    _ = validate_config(cfg)

    return 0


def main() -> None:
    import webview

    index_html = ROOT / "frontend" / "pages" / "group-sales.html"
    icon_path = (
        ROOT / "HQYLAutomation.ico"
        if getattr(sys, "frozen", False)
        else ROOT / "packaging" / "HQYLAutomation.ico"
    )
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("HQYL.Automation")
    api = AppBridge()
    webview.create_window(
        "寰球云联自动化平台",
        index_html.as_uri(),
        js_api=api,
        width=1180,
        height=760,
        min_size=(980, 640),
    )
    webview.start(debug="--debug" in sys.argv, icon=str(icon_path))


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        raise SystemExit(self_check())
    main()
