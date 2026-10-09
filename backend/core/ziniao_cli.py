from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


class ZiniaoCliError(RuntimeError):
    def __init__(self, message: str, *, auth: bool = False):
        super().__init__(message)
        self.auth = auth


def cli_executable() -> str:
    direct = shutil.which("ziniao-cli.exe")
    if direct:
        return direct
    shim = shutil.which("ziniao-cli")
    candidates = []
    if shim:
        if os.name != "nt" and Path(shim).suffix not in {".cmd", ".ps1"}:
            return shim
        candidates.append(Path(shim).parent / "node_modules/@ziniao-open/cli/bin/ziniao-cli.exe")
    if os.environ.get("APPDATA"):
        candidates.append(Path(os.environ["APPDATA"]) / "npm/node_modules/@ziniao-open/cli/bin/ziniao-cli.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise ZiniaoCliError("未找到 ziniao-cli，请先在本机安装并授权紫鸟 CLI")


class ZiniaoCli:
    """Use the user's existing CLI authorization without copying API keys into app settings."""

    def __init__(self, profile: str = ""):
        self.executable = cli_executable()
        self.profile = profile or self.current_profile()
        self.auth_failed = False
        self.targets: dict[str, str] = {}

    def _process(self, arguments: list[str], timeout: int = 60):
        try:
            return subprocess.run(
                [self.executable, *arguments], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, shell=False,
                env={**os.environ, "ZINIAO_CLI_NO_UPDATE_CHECK": "1"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            raise ZiniaoCliError("紫鸟命令执行超时，请检查客户端状态后重试") from None
        except OSError:
            raise ZiniaoCliError("紫鸟 CLI 无法启动，请检查本机安装") from None

    def current_profile(self) -> str:
        result = self._process(["config", "list"], 15)
        profiles = [line[1:].strip() for line in result.stdout.splitlines() if line.startswith("*")]
        if result.returncode or len(profiles) != 1 or not profiles[0]:
            raise ZiniaoCliError("无法读取当前紫鸟 CLI 账号，请先完成 CLI 授权配置")
        return profiles[0]

    def assert_profile(self):
        if self.current_profile() != self.profile:
            self.auth_failed = True
            raise ZiniaoCliError("紫鸟 CLI 当前账号已切换，请返回原账号后重新匹配店铺", auth=True)

    def ready(self):
        self.assert_profile()
        result = self._process(["doctor"], 60)
        text = result.stdout + result.stderr
        required = ("✓ API Key 有效", "✓ ZClaw Bridge 连通正常", "✓ 客户端登录用户:")
        if result.returncode or not all(marker in text for marker in required):
            raise ZiniaoCliError("紫鸟连接检查未通过，请运行 ziniao-cli doctor 查看客户端、账号或终端绑定问题")
        self.assert_profile()

    def run(self, arguments: list[str], timeout: int = 60) -> Any:
        if self.auth_failed:
            raise ZiniaoCliError("紫鸟认证已失效，本次任务已停止发送请求", auth=True)
        self.assert_profile()
        result = self._process(arguments, timeout)
        payload = None
        for text in (result.stdout, result.stderr):
            try:
                value = json.loads(text.lstrip("\ufeff"))
                if isinstance(value, dict):
                    payload = value
                    break
            except ValueError:
                pass
        # CLI 1.1.2 prints acknowledgements instead of JSON for these commands.
        # A sent mouse event is not a verified page change; the gateway checks it.
        plain_ack = {
            ("page", "click"): "✓ 鼠标指令：已发送",
            ("page", "visit"): "✓ 页面已导航",
        }.get(tuple(arguments[:2]))
        if payload is None and result.returncode == 0 and plain_ack and plain_ack in (result.stdout + result.stderr).splitlines():
            self.assert_profile()
            return {"sent": True, "verified": False}
        if not isinstance(payload, dict) or payload.get("ok") is not True or result.returncode:
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            error_type = error.get("type") if isinstance(error, dict) else ""
            stage = " ".join(arguments[:2])
            if error_type == "auth":
                self.auth_failed = True
                raise ZiniaoCliError(
                    f"紫鸟 {stage} 拒绝认证。请检查已授权应用的终端绑定和当前账号；doctor 通过不代表店铺接口可用。",
                    auth=True,
                )
            # Raw command responses can contain credentials, page content or signed URLs.
            raise ZiniaoCliError(f"紫鸟 {stage} 执行失败（{error_type or '返回格式异常'}），请检查客户端和页面后重试")
        self.assert_profile()
        return payload.get("data")

    def list_stores(self) -> list[dict]:
        data = self.run(["store", "list", "--all", "--format", "json"])
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ZiniaoCliError("紫鸟店铺列表结构发生变化，需重新联调")
        stores = {}
        for item in items:
            if not isinstance(item, dict):
                raise ZiniaoCliError("紫鸟店铺列表包含无效记录")
            identity = item.get("storeId", item.get("id"))
            name = item.get("storeName", item.get("name"))
            if type(identity) not in (int, str) or not isinstance(name, str) or not name.strip():
                raise ZiniaoCliError("紫鸟店铺缺少稳定 ID 或完整名称")
            store = {"store_id": str(identity).strip(), "store_name": name.strip(),
                     "platform_name": str(item.get("platformName") or item.get("platform_name") or "")}
            if not store["store_id"] or (store["store_id"] in stores and stores[store["store_id"]] != store):
                raise ZiniaoCliError("紫鸟店铺 ID 重复且信息不一致")
            stores[store["store_id"]] = store
        return list(stores.values())

    def open_store(self, store: dict, *, url: str = ""):
        arguments = ["store", "open", "--id", store["store_id"], "--expected-name", store["store_name"]]
        if url:
            arguments.extend(["--url", url])
        data = self.run(arguments, 180)
        if not isinstance(data, dict) or str(data.get("storeId") or "") != store["store_id"] or str(data.get("storeName") or data.get("name") or "") != store["store_name"]:
            raise ZiniaoCliError("紫鸟打开的店铺与本批次预览不一致，已停止该店导出")
        if url:
            page = self.run(["page", "content", "--store-id", store["store_id"], "--content-format", "structured", "--timeout", "20000"])
            page = page.get("data", {}) if isinstance(page, dict) else {}
            if not page.get("targetId"):
                raise ZiniaoCliError("无法固定本次商品页面，请检查店铺页面后重试")
            self.targets[store["store_id"]] = page["targetId"]
        return data

    def _page_arguments(self, command: str, store_id: str) -> list[str]:
        arguments = ["page", command, "--store-id", store_id]
        target = getattr(self, "targets", {}).get(store_id)
        if target:
            arguments.extend(["--target-id", target])
        return arguments

    def evaluate(self, store_id: str, script: str) -> Any:
        data = self.run([*self._page_arguments("exec", store_id), "--script", script, "--timeout", "20000"], 35)
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict) or data.get("exceptionDetails") or "result" not in data:
            raise ZiniaoCliError("读取紫鸟页面状态失败，请检查登录、人工验证或页面变化")
        value = data["result"]
        if isinstance(value, dict) and "value" in value:
            value = value["value"]
        if not isinstance(value, str):
            raise ZiniaoCliError("紫鸟页面返回值格式异常")
        try:
            return json.loads(value)
        except ValueError:
            raise ZiniaoCliError("紫鸟页面未返回有效状态") from None

    def click(self, store_id: str, selector: str):
        self.run([*self._page_arguments("click", store_id), "--selector", selector, "--timeout", "15000"], 30)
