# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH)
mabang_process_root = project_root.parent / "mabang_process"
superbrowser_process_root = project_root.parent / "superbrowser_process"

shopee_ads_site_modules = [
    "main.shopee.id_shopee_ads_recharge_operator_service",
    "main.shopee.th_shopee_ads_recharge_operator_service",
    "main.shopee.ph_shopee_ads_recharge_operator_service",
    "main.shopee.vn_shopee_ads_recharge_operator_service",
    "main.shopee.my_shopee_ads_recharge_operator_service",
]

a = Analysis(
    ["desktop_entry.py"],
    pathex=[
        str(project_root),
        str(superbrowser_process_root),
    ],
    binaries=[],
    datas=[
        ("frontend", "frontend"),
        (str(project_root / "packaging" / "HQYLAutomation.ico"), "."),
        (str(project_root / "backend" / "resources" / "user_map.yaml"), "backend/resources"),
        (str(mabang_process_root / "util"), "mabang_process/util"),
        (str(mabang_process_root / "vietnam" / "config" / "BigSeller库存同步.yaml"), "mabang_process/vietnam/config"),
        (str(superbrowser_process_root / "config" / "config.yaml"), "config"),
        (str(project_root / "backend" / "resources" / "user_map.yaml"), "config"),
        (str(superbrowser_process_root / "main" / "shopee" / "config"), "main/shopee/config"),
    ],
    hiddenimports=[
        "requests",
        "cryptography",
        "cryptography.hazmat.primitives.padding",
        "cryptography.hazmat.primitives.ciphers",
        *collect_submodules("selenium"),
        *shopee_ads_site_modules,
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
