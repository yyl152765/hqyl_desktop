from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import urlparse

import httpx

from backend.update_sources import (
    DEFAULT_UPDATE_MANIFEST_URL,
    normalize_manifest_url,
    official_oss_package,
)

_Result = TypeVar("_Result")


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


def _unavailable(exc: httpx.TransportError | httpx.HTTPStatusError) -> bool:
    return isinstance(exc, httpx.TransportError) or exc.response.status_code in (502, 503, 504)


def _using_update_network(request: Callable[[bool], _Result], *, direct_first: bool) -> _Result:
    if direct_first:
        try:
            return request(False)
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if not _unavailable(exc):
                raise
    return request(True)


def _fetch_manifest(url: str) -> httpx.Response:
    def fetch(trust_env: bool) -> httpx.Response:
        response = httpx.get(url, timeout=12, follow_redirects=True, trust_env=trust_env)
        response.raise_for_status()
        return response

    return _using_update_network(fetch, direct_first=url == DEFAULT_UPDATE_MANIFEST_URL)


def check_update_manifest(manifest_url: str, current_version: str) -> UpdateCheckResult:
    url = (manifest_url or "").strip()
    if not url:
        return UpdateCheckResult(configured=False, current_version=current_version)
    try:
        response = _fetch_manifest(normalize_manifest_url(url))
        manifest = response.json()
        if not isinstance(manifest, dict):
            raise ValueError("更新清单必须是 JSON 对象")
        latest_version = str(manifest.get("version") or "").strip()
        if not latest_version:
            raise ValueError("更新清单缺少 version")
        package_url = str(manifest.get("package_url") or "").strip()
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

    def download(trust_env: bool) -> None:
        digest = hashlib.sha256()
        downloaded = 0
        last_percent = -1
        progress("正在连接更新服务器")
        with httpx.stream("GET", package_url, timeout=60, follow_redirects=True, trust_env=trust_env) as response:
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

    try:
        direct_first = (
            re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(version)) is not None
            and official_oss_package(package_url, version)
        )
        _using_update_network(download, direct_first=direct_first)
        os.replace(partial_path, installer_path)
        progress("安装包校验通过")
        return installer_path
    except httpx.HTTPStatusError as exc:
        partial_path.unlink(missing_ok=True)
        raise ValueError(f"更新包下载失败（HTTP {exc.response.status_code}），请重新检查更新。") from None
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
