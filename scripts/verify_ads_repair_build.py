"""Verify bundled ID/VN fixes against source without importing business code."""
import argparse
import hashlib
import json
from pathlib import Path
import types

from PyInstaller.archive.readers import CArchiveReader

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT.parent / "superbrowser_process"
MODULES = (
    "util.shopee_ads_money",
    "implement.shopee.ads_expense_date_range",
    "implement.shopee.ads_budget_guard",
    "implement.shopee.ads_verified_collection",
    "implement.shopee.vn_shopee_ads_recharge",
    "implement.shopee.id_shopee_ads_recharge",
    "main.shopee.ads_store_execution_lock",
    "main.shopee.vn_shopee_ads_recharge_operator_service",
    "main.shopee.id_shopee_ads_recharge_operator_service",
)


def code_shape(code):
    if isinstance(code, types.CodeType):
        return (code.co_code, code.co_names, code.co_varnames, code.co_freevars,
                code.co_cellvars, code.co_argcount, code.co_kwonlyargcount,
                code.co_posonlyargcount, code.co_flags,
                tuple(code_shape(value) for value in code.co_consts))
    return code


def manifest(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            with path.open("rb") as stream:
                result[path.relative_to(root).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def verify(root):
    archive = CArchiveReader(str(root / "HQYLAutomation.exe"))
    pyz = archive.open_embedded_archive("PYZ.pyz")
    if "0.2.56" not in pyz.extract("backend.app_bridge").co_consts:
        raise RuntimeError("Bundled application version mismatch")
    verified = []
    for name in MODULES:
        source_path = SOURCE.joinpath(*name.split(".")).with_suffix(".py")
        expected = compile(source_path.read_text(encoding="utf-8-sig"), str(source_path), "exec", dont_inherit=True)
        if code_shape(pyz.extract(name)) != code_shape(expected):
            raise RuntimeError(f"Bundled source mismatch: {name}")
        verified.append(name)
    # The frozen tree must contain only generated public Shopee configs.
    private_names = ("config/config.yaml", "config.yaml", "cookies.json", "cookie.json", ".runtime")
    files = manifest(root)
    forbidden = [name for name in files if name.lower().endswith(private_names) and not name.startswith("_internal/playwright/")]
    if forbidden:
        raise RuntimeError("Unexpected private runtime configuration in build")
    with (PROJECT / "release" / "HQYLAutomationSetup_0.2.56.exe").open("rb") as stream:
        installer_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"version": "0.2.56", "verified_modules": verified, "files": files,
            "installer_sha256": installer_hash}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    report = verify(PROJECT / "dist" / "HQYLAutomation")
    if args.compare:
        old = json.loads(args.compare.read_text(encoding="utf-8"))
        report["identical_application_files"] = report["files"] == old["files"]
        report["identical_installer"] = report["installer_sha256"] == old["installer_sha256"]
        if not report["identical_application_files"]:
            raise RuntimeError("Application contents differ between builds")
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: len(value) if key in ("files", "verified_modules") else value
                      for key, value in report.items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
