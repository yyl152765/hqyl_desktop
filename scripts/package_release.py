import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

def _release_output_path(release_dir: Path, name: str) -> Path:
    """Keep generated files and recursive cleanup inside the release directory."""
    release_root = release_dir.resolve()
    output_path = (release_root / name).resolve()
    if output_path.parent != release_root or output_path.name != name:
        raise ValueError(f"Release output must be a direct child of {release_root}: {name}")
    return output_path


def package_release(version: str = "0.2.66"):
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Release version must use the numeric major.minor.patch format")

    root_dir = Path(__file__).resolve().parents[1]
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

    # Calculate SHA256
    digest = hashlib.sha256()
    with installer.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    sha256_hash = digest.hexdigest().upper()
    print(f"Installer: {installer_name}")
    print(f"Installer SHA256: {sha256_hash}")

    sha256_txt = f"{sha256_hash}  {installer_name}\n"

    latest_json_data = {
        "version": version,
        "mandatory": False,
        "notes": "Lazada 采集前通过页面语言菜单统一为简体中文并验证生效，保留英文兼容；修复跨境中文登录页及广告余额被多层推广、礼包和余额不足弹窗遮挡的问题，保留截图、站点和币种校验。增强紫鸟店铺打开与关闭同时失败时的阶段提示。保留 TEMU 余额统计、新品认领时间查询及 0.2.65 的已有功能。新品认领范围仍为当前账号可见的 Shopee 在售商品；广告数据库同步仍需 PGPASSWORD 环境变量，不影响 Excel 导出。Lazada 新加坡站仍未实店验证。",
        "frontend_url": "",
        "package_url": f"http://rpa.whhqyl.com.cn/hqyl/{installer_name}",
        "sha256": sha256_hash,
    }

    # 1. Create release/upload_<version>
    if upload_dir.exists():
        shutil.rmtree(upload_dir)
    upload_dir.mkdir()
    shutil.copy2(installer, upload_dir / installer_name)
    (upload_dir / "SHA256.txt").write_text(sha256_txt, encoding="utf-8")
    (upload_dir / "latest.json").write_text(
        json.dumps(latest_json_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if build_manifest.is_file():
        shutil.copy2(build_manifest, upload_dir / build_manifest.name)

    # 2. Create release/server_upload_<version>
    if server_dir.exists():
        shutil.rmtree(server_dir)
    server_dir.mkdir()
    shutil.copy2(installer, server_dir / installer_name)
    (server_dir / "SHA256.txt").write_text(sha256_txt, encoding="utf-8")
    (server_dir / "latest.json").write_text(
        json.dumps(latest_json_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if build_manifest.is_file():
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
$manifest = Get-Content -LiteralPath $ManifestStagingPath -Raw | ConvertFrom-Json
if ($manifest.version -ne $ExpectedVersion) {{
    throw "Manifest version verification failed."
}}
if ($manifest.sha256 -ne $ExpectedSha256) {{
    throw "Manifest SHA256 verification failed."
}}
Move-Item -LiteralPath $ManifestStagingPath -Destination $ManifestPath -Force

Write-Host "Deployment files updated." -ForegroundColor Green
Write-Host "Manifest URL: http://rpa.whhqyl.com.cn/hqyl/latest.json"
"""
    (server_dir / "deploy.ps1").write_text(deploy_ps1, encoding="utf-8")

    bat_content = """@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1"
pause
"""
    (server_dir / "部署更新源.bat").write_bytes(bat_content.encode("utf-8"))

    # 3. Create release/HQYLAutomation_ServerUpload_<version>.zip
    with zipfile.ZipFile(server_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in server_dir.iterdir():
            if file.is_file():
                zf.write(file, file.name)

    # 4. Create release/HQYLAutomation_{version}.zip
    with zipfile.ZipFile(upload_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in upload_dir.iterdir():
            if file.is_file():
                zf.write(file, file.name)

    print(f"Release packaging complete for version {version}.")

if __name__ == "__main__":
    ver = sys.argv[1] if len(sys.argv) > 1 else "0.2.66"
    package_release(ver)
