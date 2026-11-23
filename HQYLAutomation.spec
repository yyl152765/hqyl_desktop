# -*- mode: python ; coding: utf-8 -*-

import os
import json
import hashlib
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules


project_root = Path(SPECPATH)
mabang_process_root = project_root.parent / "mabang_process"
mabang_util_root = Path(
    os.environ.get("HQYL_MABANG_UTIL_SOURCE", str(mabang_process_root / "util"))
).resolve()
superbrowser_process_root = Path(
    os.environ.get("HQYL_SUPERBROWSER_SOURCE", str(project_root / "build/release_inputs_1.0.3/superbrowser_source"))
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

public_mabang_inputs = json.loads((project_root / "scripts/public_mabang_inputs.json").read_text("utf-8"))
mabang_data = []
for relative, expected in public_mabang_inputs.items():
    source = mabang_util_root / relative
    if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        raise ValueError(f"Mabang input differs from the reviewed public version: {relative}")
    mabang_data.append((str(source), "mabang_process/util"))

import ast
guard_tree = ast.parse((superbrowser_process_root / "main/shopee/ads_recharge_guard.py").read_bytes())
if not any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "BUILTIN_DB_PASSWORD"
           for t in n.targets) and isinstance(n.value, ast.Constant) and n.value.value == ""
           for n in guard_tree.body):
    raise ValueError("The public build requires a sanitized recharge runtime")
for relative, name in (("main/shopee/ads_recharge_guard.py", "get_guard_connection"), ("util/db_helper.py", "get_pg_connection")):
    tree = ast.parse((superbrowser_process_root / relative).read_bytes())
    body = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    if not any(isinstance(node, ast.ImportFrom) and node.module == "backend.ads_database" for node in body.body):
        raise ValueError("Public recharge runtime must use the configured database")

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
    "backend.services.lazada_balance_view",
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
        *mabang_data,
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
        *collect_submodules("pywinauto"),
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
