# -*- mode: python ; coding: utf-8 -*-

import os
import json
import hashlib
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules


project_root = Path(SPECPATH)
sys.path.insert(0, str(project_root))
from scripts.builtin_ads_database import release_connection_data

# Private builds receive the authorized connection from an explicit, untracked
# file outside this source export. The public source contains no connection value.
ads_database_data = release_connection_data(os.environ.get("HQYL_ADS_DATABASE_RESOURCE", ""), project_root)
mabang_process_root = project_root / "runtime" / "mabang_process"
mabang_util_root = mabang_process_root / "util"
superbrowser_process_root = project_root / "build/runtime/superbrowser_source"
public_runtime_config_root = project_root / "build" / "public_runtime_config"
public_shopee_config_root = public_runtime_config_root / "main" / "shopee" / "config"
public_bigseller_config = public_runtime_config_root / "mabang_process" / "vietnam" / "config" / "BigSeller库存同步.yaml"

build_profile = os.environ.get("HQYL_BUILD_PROFILE", "full")
if build_profile not in ("full", "limited"):
    raise ValueError(f"Unknown build profile: {build_profile}")
limited_build = build_profile == "limited"
profile_file = project_root / "build" / "runtime_profile" / "build_profile.json"
profile_file.parent.mkdir(parents=True, exist_ok=True)
profile_file.write_text(json.dumps({"name": build_profile}), encoding="utf-8")
mabang_data = []
internal_data = []

if not limited_build:
    if not mabang_util_root.is_dir():
        raise FileNotFoundError(f"Mabang util source is missing: {mabang_util_root}")

    if not superbrowser_process_root.is_dir():
        raise FileNotFoundError(f"Superbrowser source is missing: {superbrowser_process_root}")

    if not public_shopee_config_root.is_dir() or not public_bigseller_config.is_file():
        raise FileNotFoundError(
            "Public runtime configs are missing. Run scripts/build_public_runtime_configs.py before PyInstaller."
        )

    public_mabang_inputs = json.loads((project_root / "scripts/public_mabang_inputs.json").read_text("utf-8"))
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

    for site in ("th", "my"):
        payment = (superbrowser_process_root / f"implement/shopee/{site}_shopee_ads_recharge.py").read_bytes()
        if b"public.sms_receiver_message" in payment or b"shopee_ads.world_card_payment_messages" not in payment:
            raise ValueError("Public release requires the migrated card verification module")

    internal_data = [
        *mabang_data,
        (str(public_bigseller_config), "mabang_process/vietnam/config"),
        (str(public_shopee_config_root), "main/shopee/config"),
    ]

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

if limited_build:
    shopee_ads_site_modules = []
    lazada_withdrawal_modules = []
    balance_statistics_modules = [name for name in balance_statistics_modules if name.startswith("backend.")]

playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all("playwright")

a = Analysis(
    ["desktop_entry.py"],
    pathex=[str(project_root)] + ([] if limited_build else [str(superbrowser_process_root)]),
    binaries=[*playwright_binaries],
    datas=[
        *ads_database_data,
        *playwright_datas,
        ("frontend", "frontend"),
        (str(project_root / "packaging" / "HQYLAutomation.ico"), "."),
        (str(project_root / "backend" / "resources" / "user_map.yaml"), "backend/resources"),
        *internal_data,
        (str(profile_file), "."),
        (str(project_root / "backend" / "resources" / "user_map.yaml"), "config"),
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
    excludes=["main", "implement", "util"] if limited_build else [],
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
