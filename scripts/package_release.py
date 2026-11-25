import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import zipfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.update_sources import DEFAULT_UPDATE_MANIFEST_URL, installer_object_url

DESKTOP_REPOSITORY = "https://github.com/yyl152765/hqyl_desktop.git"


def project_version(root_dir: Path) -> str:
    """Read version literals without importing the desktop application."""
    sources = (
        ("backend/app_bridge.py", r'^APP_VERSION = "([^"]+)"'),
        ("packaging/hqyl_automation.iss", r'^#define MyAppVersion "([^"]+)"'),
        ("frontend/assets/common.js", r'\bversion:\s*"([^"]+)"'),
    )
    versions = []
    for relative, pattern in sources:
        match = re.search(pattern, (root_dir / relative).read_text("utf-8-sig"), re.MULTILINE)
        if not match or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", match.group(1)):
            raise ValueError(f"Missing or invalid release version in {relative}")
        versions.append(match.group(1))
    if len(set(versions)) != 1:
        raise ValueError(f"Application, installer and frontend versions differ: {versions}")
    return versions[0]


def find_rar() -> Path:
    candidates = [shutil.which("rar.exe"), shutil.which("rar")]
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if os.environ.get(key):
            base = Path(os.environ[key])
            candidates.extend((base / "WinRAR/Rar.exe", base / "Programs/WinRAR/Rar.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise FileNotFoundError("WinRAR Rar.exe not found. Install WinRAR or add Rar.exe to PATH.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _backup_existing(path: Path) -> None:
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup = path.with_name(f"{path.name}.{stamp}.bak")
        path.rename(backup)
        print(f"Preserved previous output: {backup}")


def _git(root_dir: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root_dir), *args], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _build_manifest(root_dir: Path, version: str, installer_info: dict, fresh_build: bool) -> dict:
    source = None
    dirty = None
    if fresh_build:
        git_root = _git(root_dir, "rev-parse", "--show-toplevel")
        origin = _git(root_dir, "remote", "get-url", "origin")
        independent = bool(git_root and Path(git_root).resolve() == root_dir.resolve())
        is_desktop_origin = independent and origin == DESKTOP_REPOSITORY
        status = _git(root_dir, "status", "--porcelain", "--untracked-files=normal")
        dirty = bool(status) if status is not None else None
        source = {
            "repository": DESKTOP_REPOSITORY if is_desktop_origin else None,
            "workspace_kind": "independent_desktop" if independent else "parent_or_untracked_workspace",
            "branch": _git(root_dir, "branch", "--show-current"),
            "commit": _git(root_dir, "rev-parse", "HEAD"),
            "desktop_upstream": {
                "repository": DESKTOP_REPOSITORY,
                "branch": "main",
                "commit": _git(root_dir, "rev-parse", "refs/remotes/origin/main") if is_desktop_origin else None,
                "verification": "local_tracking_ref_only; not fetched by packaging",
            },
        }
    return {
        "product": "HQYLAutomation",
        "version": version,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "DIRTY": dirty,
        "source": source,
        "installer": installer_info,
        "validation": {
            "build": "build.ps1 completed PyInstaller and Inno Setup" if fresh_build else "not_recorded",
            "installer_sha256": "calculated_from_installer",
            "application_tests": "not_run_by_packager",
        },
    }


def create_upload_rar(upload_dir: Path, output_dir: Path, rar: Path) -> Path:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = _release_output_path(output_dir, f"{upload_dir.name}.rar")
    files = sorted(path for path in upload_dir.iterdir() if path.is_file())
    # Build and test a separate archive before replacing a previous delivery.
    with TemporaryDirectory(prefix=".hqyl-rar-", dir=output_dir) as temporary:
        staged = Path(temporary) / destination.name
        subprocess.run(
            [str(rar), "a", "-cfg-", "-idq", "-y", "-m1", str(staged),
             *[str(path.relative_to(upload_dir.parent)) for path in files]],
            cwd=upload_dir.parent, check=True, timeout=600,
        )
        subprocess.run([str(rar), "t", "-cfg-", "-idq", str(staged)], check=True, timeout=600)
        _backup_existing(destination)
        staged.replace(destination)
    print(f"RAR archive: {destination}")
    return destination


def _release_output_path(release_dir: Path, name: str) -> Path:
    """Keep generated files and backup renames inside the output directory."""
    release_root = release_dir.resolve()
    output_path = (release_root / name).resolve()
    if output_path.parent != release_root or output_path.name != name:
        raise ValueError(f"Release output must be a direct child of {release_root}: {name}")
    return output_path


def package_release(version: str | None = None, *, notes: str = "", fresh_build: bool = False,
                    rar_output_dir: Path | None = None, self_check_report: Path | None = None) -> Path:
    root_dir = Path(__file__).resolve().parents[1]
    if version is None:
        version = project_version(root_dir)
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Release version must use the numeric major.minor.patch format")

    rar = find_rar() if rar_output_dir is not None else None
    release_dir = root_dir / "release"
    installer_name = f"HQYLAutomationSetup_{version}.exe"
    installer = release_dir / installer_name
    upload_dir = _release_output_path(release_dir, f"upload_{version}")
    server_dir = _release_output_path(release_dir, f"server_upload_{version}")
    server_zip = _release_output_path(release_dir, f"HQYLAutomation_ServerUpload_{version}.zip")
    upload_zip = _release_output_path(release_dir, f"HQYLAutomation_{version}.zip")
    build_manifest = _release_output_path(
        release_dir,
        f"HQYLAutomation_{version}_BUILD_MANIFEST.json",
    )

    if not installer.exists():
        raise FileNotFoundError(f"Installer {installer} not found!")

    validation_report = None
    if self_check_report is not None:
        if not fresh_build:
            raise ValueError("A frozen self-check report can only describe a fresh build")
        validation_report = json.loads(self_check_report.read_text("utf-8-sig"))
        executable = root_dir / "dist/HQYLAutomation/HQYLAutomation.exe"
        packaged_profile = json.loads((executable.parent / "_internal/build_profile.json").read_text("utf-8"))
        if (validation_report.get("status") != "passed" or validation_report.get("version") != version
                or validation_report.get("build_profile", {}).get("name") != packaged_profile.get("name")
                or validation_report.get("executable", {}).get("sha256") != _sha256(executable)):
            raise ValueError("Frozen self-check report does not match the built application")

    sha256_hash = _sha256(installer)
    print(f"Installer: {installer_name}")
    print(f"Installer SHA256: {sha256_hash}")

    installer_info = {"file": installer_name, "size_bytes": installer.stat().st_size, "sha256": sha256_hash}
    if build_manifest.is_file() and not fresh_build:
        manifest = json.loads(build_manifest.read_text("utf-8-sig"))
        recorded_installer = manifest.get("installer", {})
        if manifest.get("version") != version or any(
            recorded_installer.get(key) != value for key, value in installer_info.items()
        ):
            raise ValueError("Existing BUILD_MANIFEST does not match this installer. Rebuild with scripts/build.ps1.")
    else:
        manifest = _build_manifest(root_dir, version, installer_info, fresh_build)
        if validation_report is not None:
            manifest["build_profile"] = validation_report["build_profile"]
            manifest["validation"]["frozen_self_check"] = validation_report
        _backup_existing(build_manifest)
        build_manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    sha256_txt = f"{sha256_hash}  {installer_name}\n"

    release_notes = notes.strip() or f"版本 {version} 更新。"
    if manifest.get("build_profile", {}).get("name") == "limited":
        release_notes = "精简版：不包含 Shopee 充值、BigSeller 查询/同步、Lazada 提现与余额统计、TEMU 财务/费用退款/活动/在售导出与余额统计。" + release_notes
    latest_json_data = {
        "version": version,
        "mandatory": False,
        "notes": release_notes,
        "frontend_url": "",
        "package_url": installer_object_url(version),
        "sha256": sha256_hash,
    }

    # 1. Create release/upload_<version>
    _backup_existing(upload_dir)
    upload_dir.mkdir()
    shutil.copy2(installer, upload_dir / installer_name)
    (upload_dir / "SHA256.txt").write_text(sha256_txt, encoding="utf-8")
    (upload_dir / "latest.json").write_text(
        json.dumps(latest_json_data, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    shutil.copy2(build_manifest, upload_dir / build_manifest.name)

    # 2. Create release/server_upload_<version>
    _backup_existing(server_dir)
    server_dir.mkdir()
    shutil.copy2(installer, server_dir / installer_name)
    (server_dir / "SHA256.txt").write_text(sha256_txt, encoding="utf-8")
    (server_dir / "latest.json").write_text(
        json.dumps(latest_json_data, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    shutil.copy2(build_manifest, server_dir / build_manifest.name)

    deploy_ps1 = f"""$ErrorActionPreference = "Stop"

$SourceDir = $PSScriptRoot
$SiteRoot = "C:\\inetpub\\wwwroot\\hqyl"
$InstallerName = "{installer_name}"
$ExpectedVersion = "{version}"
$ExpectedSha256 = "{sha256_hash}"
$SourceInstaller = Join-Path $SourceDir $InstallerName
$DeployedInstaller = Join-Path $SiteRoot $InstallerName
$ManifestPath = Join-Path $SiteRoot "latest.json"
$ManifestStagingPath = Join-Path $SiteRoot "latest.json.uploading"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"

$sourceHash = (Get-FileHash -LiteralPath $SourceInstaller -Algorithm SHA256).Hash
if ($sourceHash -ne $ExpectedSha256) {{
    throw "Source installer SHA256 verification failed."
}}

New-Item -ItemType Directory -Path $SiteRoot -Force | Out-Null
if (Test-Path -LiteralPath $ManifestPath) {{
    Copy-Item -LiteralPath $ManifestPath -Destination (Join-Path $SiteRoot "latest.json.$Timestamp.bak") -Force
}}

# Deploy and verify the versioned installer before making the new manifest visible.
Copy-Item -LiteralPath $SourceInstaller -Destination $DeployedInstaller -Force
$deployedHash = (Get-FileHash -LiteralPath $DeployedInstaller -Algorithm SHA256).Hash
if ($deployedHash -ne $ExpectedSha256) {{
    throw "Deployed installer SHA256 verification failed."
}}

$webConfig = @'
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <system.webServer>
    <staticContent>
      <remove fileExtension=".json" />
      <mimeMap fileExtension=".json" mimeType="application/json; charset=utf-8" />
      <remove fileExtension=".exe" />
      <mimeMap fileExtension=".exe" mimeType="application/octet-stream" />
    </staticContent>
  </system.webServer>
</configuration>
'@
Set-Content -LiteralPath (Join-Path $SiteRoot "web.config") -Value $webConfig -Encoding UTF8

# Publish latest.json last so clients never see a manifest before its installer is ready.
Copy-Item -LiteralPath (Join-Path $SourceDir "latest.json") -Destination $ManifestStagingPath -Force
$manifest = Get-Content -LiteralPath $ManifestStagingPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.version -ne $ExpectedVersion) {{
    throw "Manifest version verification failed."
}}
if ($manifest.sha256 -ne $ExpectedSha256) {{
    throw "Manifest SHA256 verification failed."
}}
Move-Item -LiteralPath $ManifestStagingPath -Destination $ManifestPath -Force

Write-Host "Deployment files updated." -ForegroundColor Green
Write-Host "Manifest URL: {DEFAULT_UPDATE_MANIFEST_URL}"
"""
    (server_dir / "deploy.ps1").write_text(deploy_ps1, encoding="utf-8")

    bat_content = """@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1"
pause
"""
    (server_dir / "部署更新源.bat").write_bytes(bat_content.encode("utf-8"))

    # 3. Create release/HQYLAutomation_ServerUpload_<version>.zip
    _backup_existing(server_zip)
    with zipfile.ZipFile(server_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in server_dir.iterdir():
            if file.is_file():
                zf.write(file, file.name)

    # 4. Create release/HQYLAutomation_{version}.zip
    _backup_existing(upload_zip)
    with zipfile.ZipFile(upload_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in upload_dir.iterdir():
            if file.is_file():
                zf.write(file, file.name)

    for directory in (upload_dir, server_dir):
        if _sha256(directory / installer_name) != sha256_hash:
            raise ValueError(f"Copied installer SHA256 mismatch: {directory}")
    for archive in (server_zip, upload_zip):
        with zipfile.ZipFile(archive) as zf:
            if zf.testzip() is not None:
                raise ValueError(f"ZIP verification failed: {archive}")
    if rar is not None:
        create_upload_rar(upload_dir, Path(rar_output_dir), rar)
    print(f"Release packaging complete for version {version}: {upload_dir}")
    return upload_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the four release files and optional upload_<version>.rar.")
    parser.add_argument("version", nargs="?", help="Defaults to the matching application/installer/frontend version")
    parser.add_argument("--notes", default="", help="Release notes written to latest.json")
    parser.add_argument("--rar-output-dir", type=Path, help="Create a verified RAR in this directory (requires WinRAR)")
    parser.add_argument("--fresh-build", action="store_true", help="Used by build.ps1 only after a successful source build")
    parser.add_argument("--self-check-report", type=Path, help="Verified frozen application check from build.ps1")
    parser.add_argument("--print-version", action="store_true", help="Check all version literals, print version and exit")
    parser.add_argument("--check", action="store_true", help="Check version and WinRAR availability without generating files")
    args = parser.parse_args()
    if args.print_version or args.check:
        print(project_version(Path(__file__).resolve().parents[1]))
        if args.check:
            print(f"WinRAR: {find_rar()}")
        return 0
    package_release(args.version, notes=args.notes, fresh_build=args.fresh_build, rar_output_dir=args.rar_output_dir,
                    self_check_report=args.self_check_report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
