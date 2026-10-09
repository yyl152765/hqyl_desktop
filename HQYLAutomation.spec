# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules


project_root = Path(SPECPATH)
mabang_process_root = project_root.parent / "mabang_process"
mabang_util_root = Path(
    os.environ.get("HQYL_MABANG_UTIL_SOURCE", str(mabang_process_root / "util"))
).resolve()
superbrowser_process_root = Path(
    os.environ.get("HQYL_SUPERBROWSER_SOURCE", str(project_root.parent / "superbrowser_process"))
).resolve()
public_runtime_config_root = project_root / "build" / "public_runtime_config"
public_shopee_config_root = public_runtime_config_root / "main" / "shopee" / "config"
public_bigseller_config = public_runtime_config_root / "mabang_process" / "vietnam" / "config" / "BigSeller库存同步.yaml"

if not mabang_util_root.is_dir():
    raise FileNotFoundError(f"Mabang util source is missing: {mabang_util_root}")

if not superbrowser_process_root.is_dir():
    raise FileNotFoundError(f"Superbrowser source is missing: {superbrowser_process_root}")

if not public_shopee_config_root.is_dir() or not public_bigseller_config.is_file():
    raise FileNotFoundError(
        "Public runtime configs are missing. Run scripts/build_public_runtime_configs.py before PyInstaller."
    )

shopee_ads_site_modules = [
    "main.shopee.id_shopee_ads_recharge_operator_service",
    "main.shopee.th_shopee_ads_recharge_operator_service",
    "main.shopee.ph_shopee_ads_recharge_operator_service",
    "main.shopee.vn_shopee_ads_recharge_operator_service",
    "main.shopee.my_shopee_ads_recharge_operator_service",
]

lazada_withdrawal_modules = [
    "main.lazada.lazada_balance_withdrawal_operator_service",
    "implement.lazada.lazada_balance_withdrawal",
    "util.dingtalk_drive_util",
]

balance_statistics_modules = [
    "backend.services.balance_statistics",
    "backend.services.balance_workbook",
    "backend.services.balance_evidence",
    "backend.services.temu_balance_collector",
    "backend.services.temu_balance_calendar",
    "backend.services.temu_balance_overlays",
    "backend.services.lazada_balance_collector",
    "backend.services.lazada_balance_login",
    "backend.services.lazada_balance_language",
    "backend.services.lazada_balance_overlays",
    "util.ziniao_window_position",
]

playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all("playwright")

a = Analysis(
    ["desktop_entry.py"],
    pathex=[
        str(project_root),
        str(superbrowser_process_root),
    ],
    binaries=[*playwright_binaries],
    datas=[
        *playwright_datas,
        ("frontend", "frontend"),
        (str(project_root / "packaging" / "HQYLAutomation.ico"), "."),
        (str(project_root / "backend" / "resources" / "user_map.yaml"), "backend/resources"),
        (str(mabang_util_root), "mabang_process/util"),
        (str(public_bigseller_config), "mabang_process/vietnam/config"),
        (str(project_root / "backend" / "resources" / "user_map.yaml"), "config"),
        (str(public_shopee_config_root), "main/shopee/config"),
    ],
    hiddenimports=[
        "requests",
        *collect_submodules("openpyxl"),
        *collect_submodules("xlrd"),
        *collect_submodules("PIL"),
        "cryptography",
        "cryptography.hazmat.primitives.padding",
        "cryptography.hazmat.primitives.ciphers",
        *collect_submodules("selenium"),
        *playwright_hiddenimports,
        *shopee_ads_site_modules,
        *lazada_withdrawal_modules,
        *balance_statistics_modules,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HQYLAutomation",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(project_root / "packaging" / "HQYLAutomation.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="HQYLAutomation",
)
