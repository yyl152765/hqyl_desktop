from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


AUTH_ACTIONS = {"updateCore", "getBrowserList", "startBrowser", "stopBrowser", "ClearOnline"}


class ZiniaoClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class ZiniaoCredentials:
    company: str
    username: str
    password: str


@dataclass(frozen=True)
class ZiniaoBrowserSession:
    browser_oauth: str
    browser_name: str
    debugging_port: int
    launcher_page: str
    download_path: Path


class ZiniaoClient:
    def __init__(
        self,
        credentials: ZiniaoCredentials,
        client_path: str | Path,
        *,
        port: int = 16851,
        request_timeout: int = 120,
    ) -> None:
        self.credentials = credentials
        self.client_path = Path(client_path)
        self.port = int(port)
        self.request_timeout = max(int(request_timeout), 10)
        self.started_by_client = False

    def ensure_started(self, *, core_timeout: int = 300) -> None:
        if not self._service_responds():
            if not self.client_path.is_file():
                raise ZiniaoClientError(
                    f"未找到紫鸟客户端，请在页面选择紫鸟客户端程序（ziniao.exe 或 SuperBrowser.exe）：{self.client_path}"
                )
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(
                [
                    str(self.client_path),
                    "--run_type=web_driver",
                    "--ipc_type=http",
                    f"--port={self.port}",
                ],
                creationflags=creation_flags,
            )
            self.started_by_client = True

        deadline = time.monotonic() + max(core_timeout, 10)
        last_error = ""
        while time.monotonic() < deadline:
            try:
                result = self.request("updateCore", timeout=self.request_timeout)
            except ZiniaoClientError as exc:
                last_error = str(exc)
                time.sleep(2)
                continue
            status = str(result.get("statusCode"))
            if status == "0":
                return
            last_error = self._error_message(result, "紫鸟内核尚未就绪")
            time.sleep(2)
        raise ZiniaoClientError(f"等待紫鸟内核超时：{last_error or '未知错误'}")

    def list_browsers(self) -> list[dict[str, Any]]:
        result = self.request("getBrowserList")
        self._raise_unless_success(result, "获取紫鸟店铺列表失败")
        browsers = result.get("browserList") or []
        return [item for item in browsers if isinstance(item, dict)]

    def open_browser(
        self,
        browser: dict[str, Any],
        download_path: str | Path,
        *,
        headless: bool = False,
    ) -> ZiniaoBrowserSession:
        browser_oauth = str(browser.get("browserOauth") or "").strip()
        if not browser_oauth:
            raise ZiniaoClientError("目标店铺缺少 browserOauth")
        target_download_path = Path(download_path).resolve()
        target_download_path.mkdir(parents=True, exist_ok=True)
        result = self.request(
            "startBrowser",
            browserOauth=browser_oauth,
            isWaitPluginUpdate=0,
            isHeadless=1 if headless else 0,
            cookieTypeLoad=0,
            cookieTypeSave=0,
            runMode="1",
            isLoadUserPlugin=False,
            pluginIdType=1,
            privacyMode=0,
            notPromptForDownload=1,
            forceDownloadPath=str(target_download_path),
        )
        self._raise_unless_success(result, f"打开店铺“{browser.get('browserName') or ''}”失败")
        try:
            debugging_port = int(result.get("debuggingPort"))
        except (TypeError, ValueError) as exc:
            raise ZiniaoClientError("紫鸟未返回有效的调试端口") from exc
        return ZiniaoBrowserSession(
            browser_oauth=browser_oauth,
            browser_name=str(browser.get("browserName") or "").strip(),
            debugging_port=debugging_port,
            launcher_page=str(result.get("launcherPage") or "").strip(),
            download_path=target_download_path,
        )

    def close_browser(self, browser_oauth: str) -> None:
        result = self.request(
            "stopBrowser",
            timeout=min(self.request_timeout, 15),
            browserOauth=str(browser_oauth or "").strip(),
            duplicate=0,
        )
        self._raise_unless_success(result, "关闭紫鸟店铺失败")

    def exit_if_started(self) -> None:
        if not self.started_by_client:
            return
        try:
            self.request("exit", timeout=15)
        finally:
            self.started_by_client = False

    def request(self, action: str, *, timeout: int | None = None, **fields: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": action,
            "requestId": str(uuid.uuid4()),
            **fields,
        }
        if action in AUTH_ACTIONS:
            payload.update(
                {
                    "company": self.credentials.company,
                    "username": self.credentials.username,
                    "password": self.credentials.password,
                }
            )
        try:
            response = requests.post(
                f"http://127.0.0.1:{self.port}",
                json=payload,
                timeout=timeout or self.request_timeout,
            )
            response.encoding = "utf-8"
            result = response.json()
        except requests.RequestException as exc:
            raise ZiniaoClientError(f"紫鸟接口 {action} 连接失败") from exc
        except ValueError as exc:
            raise ZiniaoClientError(f"紫鸟接口 {action} 返回了无法解析的数据") from exc
        if not isinstance(result, dict):
            raise ZiniaoClientError(f"紫鸟接口 {action} 返回格式不正确")
        return result

    def _service_responds(self) -> bool:
        try:
            result = self.request("getRunningInfo", timeout=3)
        except ZiniaoClientError:
            return False
        return "statusCode" in result

    @classmethod
    def _raise_unless_success(cls, result: dict[str, Any], prefix: str) -> None:
        if str(result.get("statusCode")) == "0":
            return
        raise ZiniaoClientError(f"{prefix}：{cls._error_message(result, '未知错误')}")

    @staticmethod
    def _error_message(result: dict[str, Any], fallback: str) -> str:
        return str(result.get("err") or result.get("LastError") or result.get("statusMsg") or fallback).strip()
