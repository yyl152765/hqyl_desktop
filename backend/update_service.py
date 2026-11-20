from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from backend.update_sources import (
    DEFAULT_UPDATE_MANIFEST_URL,
    DOMAIN_UPDATE_MANIFEST_URL,
    OFFICIAL_MANIFEST_URLS,
    official_installer_url,
)


@dataclass(frozen=True)
class UpdateCheckResult:
    configured: bool
    current_version: str
    latest_version: str = ""
    update_available: bool = False
    notes: str = ""
    package_url: str = ""
    sha256: str = ""
    frontend_url: str = ""
    mandatory: bool = False
    error: str = ""


def _fetch_manifest(url: str) -> httpx.Response:
    response = httpx.get(url, timeout=12, follow_redirects=True)
    response.raise_for_status()
    return response


def _read_manifest(url: str) -> tuple[str, httpx.Response]:
    try:
        return url, _fetch_manifest(url)
    except (httpx.TransportError, httpx.HTTPStatusError) as exc:
        if url not in OFFICIAL_MANIFEST_URLS:
            raise
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (502, 503, 504):
            raise

    alternate = DEFAULT_UPDATE_MANIFEST_URL if url == DOMAIN_UPDATE_MANIFEST_URL else DOMAIN_UPDATE_MANIFEST_URL
    try:
        return alternate, _fetch_manifest(alternate)
    except httpx.RequestError as exc:
        raise ValueError("两个官方更新地址均未能连接，请稍后再次检查更新。") from exc
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"两个官方更新地址均不可用，备用地址返回 HTTP {exc.response.status_code}。") from exc


def check_update_manifest(manifest_url: str, current_version: str) -> UpdateCheckResult:
    url = (manifest_url or "").strip()
    if not url:
        return UpdateCheckResult(configured=False, current_version=current_version)
    try:
        source, response = _read_manifest(url)
        manifest = response.json()
        if not isinstance(manifest, dict):
            raise ValueError("更新清单必须是 JSON 对象")
        latest_version = str(manifest.get("version") or "").strip()
        if not latest_version:
            raise ValueError("更新清单缺少 version")
        package_url = str(manifest.get("package_url") or "").strip()
        if source in OFFICIAL_MANIFEST_URLS and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", latest_version):
            # Older manifests hard-code the DNS name even when fetched via IIS by IP.
            # Match only published HQYL installer URLs for this exact release.
            if package_url in {official_installer_url(item, latest_version) for item in OFFICIAL_MANIFEST_URLS}:
                package_url = official_installer_url(source, latest_version)
        return UpdateCheckResult(
            configured=True,
            current_version=current_version,
            latest_version=latest_version,
            update_available=_version_tuple(latest_version) > _version_tuple(current_version),
            notes=str(manifest.get("notes") or "").strip(),
            package_url=package_url,
            sha256=str(manifest.get("sha256") or "").strip().upper(),
            frontend_url=str(manifest.get("frontend_url") or "").strip(),
            # Updates are always optional, including when an older release
            # manifest requests a mandatory upgrade.
            mandatory=False,
        )
    except Exception as exc:
        return UpdateCheckResult(configured=True, current_version=current_version, error=str(exc))


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for item in version.split("."):
        digits = "".join(ch for ch in item if ch.isdigit())
        parts.append(int(digits or "0"))
    return tuple(parts or [0])


def calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def download_update_package(
    package_url: str,
    version: str,
    expected_sha256: str,
    destination_dir: Path,
    progress,
) -> Path:
    parsed = urlparse(str(package_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("更新包地址无效，仅支持 HTTP 或 HTTPS")

    expected_hash = str(expected_sha256 or "").strip().upper()
    if len(expected_hash) != 64 or any(ch not in "0123456789ABCDEF" for ch in expected_hash):
        raise ValueError("发布清单缺少有效的 SHA256，已拒绝下载安装")

    safe_version = "".join(ch for ch in str(version or "") if ch.isdigit() or ch in ".-_")
    if not safe_version:
        raise ValueError("更新版本号无效")

    destination_dir.mkdir(parents=True, exist_ok=True)
    installer_path = destination_dir / f"HQYLAutomationSetup_{safe_version}.exe"
    partial_path = installer_path.with_suffix(".exe.part")
    partial_path.unlink(missing_ok=True)

    digest = hashlib.sha256()
    downloaded = 0
    last_percent = -1
    try:
        progress("正在连接更新服务器")
        with httpx.stream("GET", package_url, timeout=60, follow_redirects=True) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            with partial_path.open("wb") as target:
                for chunk in response.iter_bytes(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    target.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    if total:
                        percent = min(99, int(downloaded * 100 / total))
                        if percent >= last_percent + 2:
                            last_percent = percent
                            progress(f"下载进度 {percent}%")
        if downloaded <= 0:
            raise ValueError("更新包内容为空")

        actual_hash = digest.hexdigest().upper()
        progress("下载完成，正在校验安装包")
        if actual_hash != expected_hash:
            raise ValueError("更新包 SHA256 校验失败，请重新下载")
        os.replace(partial_path, installer_path)
        progress("安装包校验通过")
        return installer_path
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise


def result_payload(result: UpdateCheckResult) -> dict[str, Any]:
    return {
        "ok": not bool(result.error),
        "configured": result.configured,
        "current_version": result.current_version,
        "latest_version": result.latest_version,
        "update_available": result.update_available,
        "notes": result.notes,
        "package_url": result.package_url,
        "sha256": result.sha256,
        "frontend_url": result.frontend_url,
        "mandatory": result.mandatory,
        "error": result.error,
    }
