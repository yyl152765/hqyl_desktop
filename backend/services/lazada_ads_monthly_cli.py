"""Native ZiNiao CLI adapter for Lazada Ads Monthly Report.

Adheres strictly to specifications 2.4 - 2.10 and 2.21:
- Uses native CLI executable
- Assert active profile before and after invocations
- Recovers from unlogged client via `ziniao-cli zclaw login`
- Manages store window lifecycle (only closes windows opened by current task)
- Binds target-id for deterministic page interactions
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from backend.core.ziniao_cli import ZiniaoCliError, cli_executable

logger = logging.getLogger(__name__)
MAX_OPEN_STORES = 3


def _window_error(message: str, code: str = 'WINDOW_STATE_UNVERIFIED') -> ZiniaoCliError:
    error = ZiniaoCliError(message)
    error.code = code
    return error


def normalize_store_name(name: str) -> str:
    """Normalize store name for robust NFKC whitespace-insensitive matching."""
    if not name:
        return ""
    norm = unicodedata.normalize("NFKC", str(name))
    norm = re.sub(r"\s+", " ", norm)
    return norm.strip()


class ZiniaoMonthlyCli:
    """CLI Gateway with resilient login recovery and window lifecycle tracking."""

    def __init__(self, profile: str = ""):
        self.executable = cli_executable()
        self.profile = profile or self.current_profile()
        self.auth_failed = False
        self.targets: dict[str, str] = {}
        self.owned_windows: set[str] = set()
        self.download_dirs: dict[str, Path] = {}
        self.expected_urls: dict[str, str] = {}
        self._identity = self.identity_fingerprint()
        self._profile_verified = False

    def identity_fingerprint(self) -> str:
        # Only a digest is retained. Never copy the API key into settings or reports.
        config = Path.home() / '.ziniao-cli' / 'config.json'
        if not config.is_file():
            raise ZiniaoCliError('紫鸟配置文件不存在，无法固定本次账号', auth=True)
        return hashlib.sha256(self.profile.encode('utf8') + b'\0' + config.read_bytes()).hexdigest()

    def _process(self, arguments: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self.executable, *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                env={**os.environ, "ZINIAO_CLI_NO_UPDATE_CHECK": "1"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            raise ZiniaoCliError(f"紫鸟命令超时 ({arguments[:2]})", auth=False) from None
        except OSError as exc:
            raise ZiniaoCliError(f"无法启动紫鸟 CLI: {exc}", auth=False) from None

    def current_profile(self) -> str:
        res = self._process(["config", "list"], timeout=15)
        profiles = [line.strip()[1:].strip() for line in res.stdout.splitlines() if line.strip().startswith('*')]
        if res.returncode != 0 or len(profiles) != 1 or not profiles[0]:
            raise ZiniaoCliError("无法读取当前紫鸟 CLI 账号配置，请先运行 ziniao-cli config list", auth=True)
        return profiles[0]

    def assert_profile(self) -> None:
        identity = getattr(self, '_identity', None)
        if identity:
            if self.identity_fingerprint() != identity:
                self.auth_failed = True
                raise ZiniaoCliError('紫鸟账号配置已变化，停止批次并要求重新预检', auth=True)
            # The digest includes the active profile setting and encrypted credential.
            # Once verified, unchanged config proves the same account without two extra CLI processes per command.
            if getattr(self, '_profile_verified', False):
                return
        curr = self.current_profile()
        if not curr or not self.profile or curr != self.profile:
            self.auth_failed = True
            raise ZiniaoCliError(f"紫鸟 CLI 当前 profile 已从 {self.profile} 切换为 {curr}，批次已安全暂停", auth=True)
        self._profile_verified = True

    def ready(self) -> None:
        """Self-test doctor and handle recovery decisions as specified in 2.7."""
        self.assert_profile()
        res = self._process(["doctor"], timeout=60)
        output = res.stdout + "\n" + res.stderr

        # Check bridge connection
        if "Bridge" in output and ("连通失败" in output or "无法连接" in output):
            raise ZiniaoCliError("紫鸟 Bridge 127.0.0.1:9481 连通失败，请确认紫鸟客户端已启动且未开启多开工作台")

        # Check client login state
        if ("CLIENT_LOGGED_OUT" in output or "客户端未登录" in output or "尚未登录" in output) and "✓ API Key 有效" in output and "✓ ZClaw Bridge 连通正常" in output and not any(s in output for s in ('不一致', '未绑定', '无效', '被拒绝')):
            logger.info("Doctor 提示客户端未登录，尝试 ziniao-cli zclaw login...")
            login_res = self._process(["zclaw", "login"], timeout=45)
            if login_res.returncode != 0:
                raise ZiniaoCliError("紫鸟客户端登录恢复失败，请在紫鸟客户端完成登录", auth=True)
            # Re-run doctor once
            res = self._process(["doctor"], timeout=60)
            output = res.stdout + "\n" + res.stderr

        required_markers = ("✓ API Key 有效", "✓ ZClaw Bridge 连通正常", "✓ 客户端登录用户:")
        if res.returncode != 0 or not all(m in output for m in required_markers) or '✗' in output:
            raise ZiniaoCliError("紫鸟 doctor 自检未通过，请检查 API Key、客户端登录或终端绑定状态")

        self.assert_profile()

    def run(self, arguments: list[str], timeout: int = 60, retry_on_socks: bool = True) -> Any:
        """Run CLI command with automatic recovery for transient errors and login expiration."""
        if self.auth_failed:
            raise ZiniaoCliError("紫鸟认证已失效，已停止发送后续请求", auth=True)

        self.assert_profile()
        stage = " ".join(arguments[:2])

        # Never replay a click/export/open after an uncertain acknowledgement.
        read_only = tuple(arguments[:2]) in {('store', 'list'), ('page', 'content'), ('page', 'query')}
        max_attempts = 3 if retry_on_socks and read_only else 1
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            res = self._process(arguments, timeout=timeout)
            out = res.stdout + "\n" + res.stderr

            # Check transient socks connection failed (2.19)
            if "ERR_SOCKS_CONNECTION_FAILED" in out:
                if attempt < max_attempts:
                    logger.warning(f"店铺代理暂时连通失败 (第 {attempt} 次)，等待 5 秒后重试...")
                    time.sleep(5.0)
                    continue
                raise ZiniaoCliError(f"紫鸟店铺代理连接失败 (ERR_SOCKS_CONNECTION_FAILED)，已重试 {max_attempts} 次")

            # Check Ziniao user is not logged in during run (2.7)
            if "Ziniao user is not logged in" in out or "CLIENT_LOGGED_OUT" in out:
                logger.info("运行中检测到客户端未登录，执行 ziniao-cli zclaw login 恢复...")
                login = self._process(["zclaw", "login"], timeout=45)
                if login.returncode or any(s in login.stdout + login.stderr for s in ('"ok": false', '失败', '无法连接')):
                    self.auth_failed = True
                    raise ZiniaoCliError("紫鸟客户端登录恢复失败，已停止批次", auth=True)
                # Retry once
                res = self._process(arguments, timeout=timeout)
                out = res.stdout + "\n" + res.stderr

            # Parse payload
            payload = None
            for stream in (res.stdout, res.stderr):
                stream_clean = stream.lstrip("\ufeff").strip()
                if not stream_clean:
                    continue
                try:
                    parsed = json.loads(stream_clean)
                    if isinstance(parsed, dict) and "ok" in parsed:
                        payload = parsed
                        break
                except ValueError:
                    continue

            # Check plain text acknowledgements
            plain_acks = {
                ("page", "click"): "✓ 鼠标指令：已发送",
                ("page", "visit"): "✓ 页面已导航",
                ("page", "input"): "✓ 已输入",
                ("store", "close"): "✓ 店铺已关闭",
            }
            cmd_key = tuple(arguments[:2])
            if payload is None and res.returncode == 0:
                expected_ack = plain_acks.get(cmd_key)
                if expected_ack and expected_ack in out:
                    self.assert_profile()
                    return {"sent": True, "verified": False}

            if isinstance(payload, dict) and payload.get("ok") is True and res.returncode == 0:
                self.assert_profile()
                data = payload.get("data")
                if not isinstance(data, dict) or data.get('ok') is not False:
                    return data
                payload = data

            # Error handling
            err_info = payload.get("error", {}) if isinstance(payload, dict) else {}
            err_type = str(err_info.get("type") or "") if isinstance(err_info, dict) else ""
            err_msg = str(err_info.get("message") or "") if isinstance(err_info, dict) else ""

            if err_type == "auth" or any(s in err_msg + out for s in ('API Key 无效', '终端未绑定', '账号不一致', 'Ziniao user is not logged in', 'CLIENT_LOGGED_OUT')):
                self.auth_failed = True
                raise ZiniaoCliError(f"紫鸟 {stage} 认证拒绝，请检查 API Key、账号和终端绑定", auth=True)

            marker = re.search(r'\[([A-Z_]+)\]', err_msg)
            code = marker.group(1) if marker else err_type or '返回格式异常'
            exc = ZiniaoCliError(f"紫鸟 {stage} 执行失败 ({code})，请检查当前页面或客户端")
            exc.code = code
            raise exc

    def list_stores(self) -> list[dict[str, str]]:
        """Fetch all stores from Ziniao CLI and sanitize."""
        data = self.run(["store", "list", "--all", "--format", "json"], timeout=45)
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ZiniaoCliError("紫鸟店铺列表返回结构发生变化，未找到 items 数组")

        result = []
        for it in items:
            if not isinstance(it, dict):
                continue
            s_id = str(it.get("storeId") or it.get("id") or "").strip()
            s_name = str(it.get("storeName") or it.get("name") or "").strip()
            platform = str(it.get("platformName") or it.get("platform_name") or "").strip()
            site = str(it.get("site") or it.get("country") or "").strip()
            if s_id and s_name:
                result.append({
                    "store_id": s_id,
                    "store_name": s_name,
                    "platform_name": platform,
                    "site": site,
                })
        return result

    def match_store(self, requested_name: str, stores: list[dict[str, str]]) -> dict[str, str]:
        """Find store by exact normalized name. Stops if ambiguous."""
        target_norm = normalize_store_name(requested_name)
        if not target_norm:
            raise ZiniaoCliError("店铺名称不能为空")

        matches = []
        for s in stores:
            if normalize_store_name(s["store_name"]) == target_norm:
                matches.append(s)

        if not matches:
            raise ZiniaoCliError(f"未找到店铺: '{requested_name}'，请确认紫鸟客户端中店铺名称")
        if len(matches) > 1:
            raise ZiniaoCliError(f"店铺名称 '{requested_name}' 匹配到多个紫鸟店铺 (ID: {[m['store_id'] for m in matches]})，存在歧义")

        return matches[0]

    def open_store(self, store_id: str, store_name: str, entry_url: str = "") -> dict[str, Any]:
        """Open store window, tracking lifecycle ownership and binding targetId."""
        active = self.running_store_ids()
        if len(active) > MAX_OPEN_STORES or (store_id not in active and len(active) >= MAX_OPEN_STORES):
            raise _window_error(f'当前已有 {len(active)} 个店铺窗口，最多允许 {MAX_OPEN_STORES} 个；请关闭空闲窗口后重试', 'WINDOW_LIMIT')
        args = ["store", "open", "--id", store_id, "--expected-name", store_name]
        if entry_url:
            args.extend(["--url", entry_url])
        self.expected_urls[store_id] = entry_url

        res = self.run(args, timeout=120)
        if not isinstance(res, dict) or str(res.get('storeId')) != store_id or str(res.get('storeName') or res.get('name')) != store_name or type(res.get('reused')) is not bool:
            raise ZiniaoCliError("打开店铺的身份或窗口归属未核实，已停止批次", auth=True)
        reused = bool(res.get("reused") if isinstance(res, dict) else False)
        if not reused:
            self.owned_windows.add(store_id)

        active = self.running_store_ids()
        if len(active) > MAX_OPEN_STORES:
            raise _window_error('店铺窗口数量已超过三个，停止批次并清理本任务窗口', 'WINDOW_LIMIT')
        if store_id not in active:
            raise _window_error('打开指令返回后未查到目标店铺运行状态，已停止批次')

        # Clear any stale target ID for this store
        self.targets.pop(store_id, None)
        time.sleep(2.0)

        # Retrieve fresh targetId
        try:
            page_info = self.run(["page", "content", "--store-id", store_id, "--content-format", "structured", "--timeout", "20000"], timeout=30)
        except ZiniaoCliError as exc:
            if getattr(exc,'code','') != 'TARGET_NOT_FOUND':
                raise
            page_info = {'targetId':self.rebind_store_page(store_id)}
        target_id = None
        if isinstance(page_info, dict):
            target_id = page_info.get("targetId") or (page_info.get("data") or {}).get("targetId")
        if target_id:
            self.targets[store_id] = str(target_id)
        else:
            raise ZiniaoCliError("无法固定本次店铺的页面 targetId")
        download_dir = res.get('downloadFolderPath')
        if download_dir:
            self.download_dirs[store_id] = Path(download_dir).resolve()

        return {"store_id": store_id, "reused": reused, "target_id": target_id, "download_dir": download_dir}

    def rebind_store_page(self, store_id: str, *, require_path: bool = False) -> str:
        expected = urlparse(self.expected_urls.get(store_id,''))
        data = self.run(['zclaw','invoke','list_pages','--args',json.dumps({'storeId':store_id})])
        pages = data.get('data',data).get('pages',[]) if isinstance(data,dict) else []
        candidates = [p for p in pages if urlparse(p.get('url','')).hostname == expected.hostname and p.get('targetId')]
        exact = [p for p in candidates if urlparse(p.get('url','')).path.rstrip('/') == expected.path.rstrip('/')]
        candidates = exact if require_path else (exact or candidates)
        if len(candidates) != 1:
            raise ZiniaoCliError('登录后页面发生变化，无法唯一核实目标站点标签')
        self.targets[store_id] = candidates[0]['targetId']
        return self.targets[store_id]

    def running_store_ids(self) -> set[str]:
        """Read the actual running windows, including windows opened outside this task."""
        try:
            data = self.run(['zclaw', 'invoke', 'extract_data', '--args', json.dumps({'mode':'running'})], timeout=20)
        except ZiniaoCliError as exc:
            if exc.auth:
                raise
            raise _window_error('无法核实正在运行的店铺窗口，已停止新增窗口') from exc
        rows = data.get('items') if isinstance(data, dict) else None
        if not isinstance(rows, list) or type(data.get('total')) is not int or data['total'] != len(rows):
            raise _window_error('正在运行的店铺列表不完整，无法核实窗口上限')
        ids = {str(row.get('storeId') or '').strip() for row in rows if isinstance(row, dict)}
        if '' in ids or len(ids) != len(rows):
            raise _window_error('正在运行的店铺身份缺失或重复，无法核实窗口上限')
        return ids

    def close_store(self, store_id: str) -> None:
        """Close store window only if it was newly opened by this task."""
        if store_id in self.owned_windows:
            self.run(["store", "close", "--id", store_id], timeout=30)
            for attempt in range(6):
                if store_id not in self.running_store_ids():
                    break
                if attempt < 5:
                    time.sleep(1)
            else:
                raise _window_error('关闭指令已发送，但店铺仍在运行；停止打开下一家并保留窗口归属')
            self.owned_windows.discard(store_id)
            self.targets.pop(store_id, None)

    def _execute_page_command(self, cmd: str, store_id: str, extra_args: list[str], timeout: int) -> dict[str, Any]:
        """Execute page command with automatic recovery from stale target IDs."""
        tid = self.targets.get(store_id)
        args_first = ["page", cmd, "--store-id", store_id]
        if tid:
            args_first.extend(["--target-id", tid])
        args_first.extend(extra_args)

        try:
            res = self.run(args_first, timeout=timeout + 10, retry_on_socks=False)
        except ZiniaoCliError as exc:
            if cmd not in ('content','query') or getattr(exc,'code','') != 'TARGET_NOT_FOUND':
                raise
            target = self.rebind_store_page(store_id)
            res = self.run(['page',cmd,'--store-id',store_id,'--target-id',target,*extra_args],timeout=timeout+10,retry_on_socks=False)

        if isinstance(res, dict):
            inner = res.get("data") if isinstance(res.get("data"), dict) else res
            new_tid = inner.get("targetId")
            if new_tid:
                self.targets[store_id] = str(new_tid)

        return res

    def evaluate(self, store_id: str, script: str, timeout: int = 25) -> Any:
        """Execute JS in page and return parsed JSON result."""
        data = self._execute_page_command("exec", store_id, ["--script", script, "--timeout", f"{timeout * 1000}"], timeout=timeout)
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict) or data.get("exceptionDetails"):
            exc = (data or {}).get("exceptionDetails") if isinstance(data, dict) else None
            raise ZiniaoCliError(f"页面脚本执行异常: {exc or '未知异常'}")
        raw_val = data.get("result")
        if isinstance(raw_val, dict) and "value" in raw_val:
            raw_val = raw_val["value"]
        if isinstance(raw_val, str):
            try:
                return json.loads(raw_val)
            except ValueError:
                return raw_val
        return raw_val

    def visit(self, store_id: str, url: str, wait_until: str = "load", timeout: int = 30) -> None:
        """Native page navigation with automatic wait-until condition."""
        self.expected_urls[store_id] = url
        extra = ["--url", url, "--wait-until", wait_until, "--timeout", f"{timeout * 1000}"]
        self._execute_page_command("visit", store_id, extra, timeout=timeout)

    def click(self, store_id: str, selector: str, timeout: int = 15) -> None:
        self._execute_page_command("click", store_id, ["--selector", selector, "--timeout", f"{timeout * 1000}"], timeout=timeout)

    def input_text(self, store_id: str, selector: str, text: str, clear: bool = True, timeout: int = 15, submit: bool = False) -> None:
        extra = ["--selector", selector, "--text", text, "--timeout", f"{timeout * 1000}"]
        if clear:
            extra.append("--clear")
        if submit:
            extra.append("--submit")
        self._execute_page_command("input", store_id, extra, timeout=timeout)

    def screenshot(self, store_id: str, save_path: Path | str, full_page: bool = True, timeout: int = 15) -> Path:
        p = Path(save_path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        extra = ["--path", str(p), "--timeout", f"{timeout * 1000}"]
        if full_page:
            extra.append("--full-page")

        result = self._execute_page_command("screenshot", store_id, extra, timeout=timeout)
        inner = result.get('data', result) if isinstance(result, dict) else {}
        # CLI 1.1.2 returns a Bridge temporary file, even when --path is supplied.
        source = inner.get('filePath')
        if source:
            source_path = Path(source).resolve()
            if source_path != p:
                shutil.copyfile(source_path, p)
        try:
            from PIL import Image
            with Image.open(p) as img:
                img.verify()
        except Exception as exc:
            raise ZiniaoCliError("原始截图缺失或损坏，不能作为采集证据") from exc
        return p
