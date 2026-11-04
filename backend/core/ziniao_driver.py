"""Validate and repair one ZiNiao Windows driver before attaching to a store.

Only the vendor's HTTPS manifest and exact vendor executable paths are used.
A completed, hash-checked, runnable candidate replaces the old file atomically.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid

import requests


CONFIG_URL = 'https://cdn-superbrowser-attachment.ziniao.com/webdriver/exe_32/config.json'
_HOST = 'cdn-superbrowser-attachment.ziniao.com'
_MAX_DRIVER_BYTES = 64 * 1024 * 1024
_MAX_CONFIG_BYTES = 256 * 1024
_LOCK = threading.Lock()


class ZiniaoDriverError(RuntimeError):
    """Contains only application-authored messages, no upstream response text."""


class _InvalidDriver(Exception):
    pass


def _os_detail(error: OSError) -> str:
    winerror = getattr(error, 'winerror', None)
    code = getattr(error, 'errno', None)
    if type(winerror) is int and 0 < winerror <= 65535:
        label = f'WinError {winerror}'
    elif type(code) is int and 0 < code <= 65535:
        label = f'errno {code}'
    else:
        label = 'OSError'
    advice = {
        5: '请检查驱动目录权限和安全软件拦截记录',
        32: '驱动文件被其他任务占用，请结束相关任务后重试',
        112: '请检查驱动目录所在磁盘剩余空间',
        193: '驱动不是有效的 Windows 可执行文件',
        216: '官方驱动与当前 Windows 系统架构不兼容',
    }.get(winerror)
    if not advice:
        advice = {errno.EACCES: '请检查驱动目录权限和安全软件拦截记录',
                  errno.ENOSPC: '请检查驱动目录所在磁盘剩余空间'}.get(code)
    return label + ('；' + advice if advice else '；请检查驱动目录和系统环境')


def _major(opened: dict) -> str:
    core = opened.get('core_type')
    if core != 'Chromium' and not (type(core) is int and core == 0):
        raise ZiniaoDriverError('紫鸟未返回可支持的 Chromium 内核，无法自动匹配驱动')
    version = opened.get('core_version')
    if not isinstance(version, str) or not re.fullmatch(r'[1-9][0-9]{0,3}(?:\.[0-9]{1,6}){0,3}', version):
        raise ZiniaoDriverError('紫鸟内核版本无效，无法自动匹配驱动')
    return version.split('.')[0]


def _validate_driver(path: Path, major: str) -> str:
    """Check PE structure before running only --version in a hidden process."""
    if path.is_symlink():
        raise ZiniaoDriverError('驱动文件为链接，无法自动替换，请检查驱动目录')
    try:
        size = path.stat().st_size
        if not path.is_file() or not 88 <= size <= _MAX_DRIVER_BYTES:
            raise _InvalidDriver()
        with path.open('rb') as source:
            header = source.read(64)
            if header[:2] != b'MZ':
                raise _InvalidDriver()
            offset = struct.unpack_from('<I', header, 60)[0]
            if offset < 64 or offset > size - 24:
                raise _InvalidDriver()
            source.seek(offset)
            pe = source.read(24)
            if pe[:4] != b'PE\0\0' or struct.unpack_from('<H', pe, 4)[0] not in (0x14c, 0x8664, 0xaa64):
                raise _InvalidDriver()
        with tempfile.TemporaryFile() as output:
            result = subprocess.run([str(path), '--version'], stdout=output, stderr=subprocess.DEVNULL,
                                    timeout=5, check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            output.seek(0)
            raw = output.read(8193)
        match = re.fullmatch(rb'ChromeDriver ([0-9]{1,4}(?:\.[0-9]{1,6}){3})(?:[ \t]+[^\r\n]*)?', raw.strip())
        if result.returncode or len(raw) > 8192 or not match:
            raise _InvalidDriver()
        version = match[1].decode('ascii')
        if version.split('.')[0] != major:
            raise _InvalidDriver()
        return version
    except FileNotFoundError as error:
        raise _InvalidDriver(_os_detail(error)) from None
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps only the process it created.
        raise _InvalidDriver() from None
    except OSError as error:
        if getattr(error, 'winerror', None) in (2, 3, 193, 216) or error.errno == errno.ENOEXEC:
            raise _InvalidDriver(_os_detail(error)) from None
        raise


def _allowed_driver_url(url: object, major: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and parts.netloc == _HOST
                and not parts.query and not parts.fragment
                and parts.path in (f'/webdriver/exe_32/chromedriver{major}.exe',
                                   f'/webdriver/exe_64/chromedriver{major}.exe'))
    except ValueError:
        return False


def _stream(session, url: str, destination, limit: int, deadline: float):
    if time.monotonic() >= deadline:
        raise ZiniaoDriverError('官方驱动下载超时，请检查网络后重试')
    with session.get(url, stream=True, timeout=(5, 15), allow_redirects=False,
                     headers={'Accept-Encoding': 'identity'}) as response:
        if response.status_code != 200:
            raise ZiniaoDriverError('官方驱动下载服务暂不可用，请检查网络后重试')
        if response.headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
            raise ZiniaoDriverError('官方驱动下载响应格式异常，请稍后重试')
        length = response.headers.get('Content-Length')
        if length is not None and (not re.fullmatch(r'[0-9]+', length) or int(length) > limit):
            raise ZiniaoDriverError('官方驱动下载大小异常，请稍后重试')
        count = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if time.monotonic() >= deadline:
                raise ZiniaoDriverError('官方驱动下载超时，请检查网络后重试')
            if chunk:
                count += len(chunk)
                if count > limit:
                    raise ZiniaoDriverError('官方驱动下载超过大小限制，请稍后重试')
                destination.write(chunk)
        if not count or (length is not None and count != int(length)):
            raise ZiniaoDriverError('官方驱动下载不完整，请检查网络后重试')


def _download_candidate(session, major: str, target: Path, deadline: float):
    with tempfile.TemporaryFile() as manifest:
        _stream(session, CONFIG_URL, manifest, _MAX_CONFIG_BYTES, deadline)
        manifest.seek(0)
        try:
            config = json.load(manifest)
        except (ValueError, UnicodeError):
            raise ZiniaoDriverError('紫鸟官方驱动清单格式异常，请稍后重试') from None
    if not isinstance(config, list):
        raise ZiniaoDriverError('紫鸟官方驱动清单格式异常，请稍后重试')
    entries = [item for item in config if isinstance(item, dict) and item.get('name') == 'chromedriver' + major]
    if len(entries) != 1:
        raise ZiniaoDriverError(f'紫鸟官方清单没有唯一的 {major} 内核驱动，请更新紫鸟 V6 或联系支持')
    item = entries[0]
    if not _allowed_driver_url(item.get('url'), major):
        raise ZiniaoDriverError('紫鸟驱动下载地址未通过官方来源校验')
    digest = item.get('sha1')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-fA-F]{40}', digest):
        raise ZiniaoDriverError('紫鸟官方驱动缺少有效校验值，请稍后重试')
    with target.open('wb') as output:
        _stream(session, item['url'], output, _MAX_DRIVER_BYTES, deadline)
        output.flush()
        os.fsync(output.fileno())
    checksum = hashlib.sha1()
    with target.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            checksum.update(chunk)
    if checksum.hexdigest() != digest.lower():
        raise ZiniaoDriverError('紫鸟官方驱动完整性校验失败，原驱动已保留，请重试')


def ensure_ziniao_driver(browser, opened: dict, progress=None) -> Path:
    """Return a verified driver path; repair a missing/broken version once.

    No client/store is started or stopped here. Existing valid drivers work
    offline. At most one official manifest + executable is fetched per call.
    """
    major = _major(opened)
    name = f'chromedriver{major}.exe'
    directory = getattr(browser, 'driver_folder_path', None)
    if not isinstance(directory, (str, os.PathLike)) or not str(directory).strip():
        raise ZiniaoDriverError('未配置紫鸟驱动目录，无法自动下载')
    if os.name != 'nt':
        raise ZiniaoDriverError('自动驱动修复目前仅支持 Windows')
    if not _LOCK.acquire(timeout=90):
        raise ZiniaoDriverError('另一项驱动检查尚未完成，请稍后重试')
    candidate = None
    session = None
    try:
        folder = Path(directory).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        try:
            _validate_driver(target, major)
            return target
        except _InvalidDriver:
            pass
        if target.exists() and not target.is_file():
            raise ZiniaoDriverError(f'{name} 不是普通文件，请检查驱动目录')
        if progress:
            progress(f'检查内核 {major}：正在自动下载并校验匹配驱动')
        fd, temporary = tempfile.mkstemp(prefix=f'.{name}.', suffix='.download.exe', dir=folder)
        os.close(fd)
        candidate = Path(temporary)
        session = requests.Session()
        session.trust_env = False
        _download_candidate(session, major, candidate, time.monotonic() + 90)
        try:
            _validate_driver(candidate, major)
        except _InvalidDriver as error:
            detail = '；' + str(error) if str(error) else ''
            raise ZiniaoDriverError(f'下载的 {name} 无法通过执行或版本校验，原驱动已保留；请检查 Windows 架构和安全软件' + detail) from None
        if target.is_symlink():
            raise ZiniaoDriverError('驱动文件在下载期间变为链接，已停止替换，请检查驱动目录')
        if target.exists():
            # Keep the original until the validated replacement is committed.
            backup = folder / f'{name}.invalid-{uuid.uuid4().hex}.bak'
            with target.open('rb') as source, backup.open('xb') as output:
                shutil.copyfileobj(source, output)
        os.replace(candidate, target)
        candidate = None
        if progress:
            progress(f'内核 {major} 驱动已就绪')
        return target
    except requests.RequestException:
        raise ZiniaoDriverError(f'{name} 自动下载失败，原驱动已保留；请检查网络后重试') from None
    except OSError as error:
        raise ZiniaoDriverError(f'{name} 检查或修复失败：' + _os_detail(error)) from None
    finally:
        if session is not None:
            session.close()
        if candidate is not None:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
        _LOCK.release()
