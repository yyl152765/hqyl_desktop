"""ZiNiao account login using the same desktop runtime as Shopee Ads."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket
import time
from typing import Any
from urllib.parse import urlsplit

import requests
from selenium.common.exceptions import ElementClickInterceptedException


class ZiniaoBrowserError(RuntimeError):
    def __init__(self, message: str, *, auth: bool = False):
        super().__init__(message)
        self.auth = auth


class ZiniaoClickInterceptedError(ZiniaoBrowserError):
    """Selenium confirmed that an overlay prevented the click from occurring."""


def account_profile(request: dict) -> str:
    identity = [str(request.get(key) or '').strip() for key in ('account_id', 'company', 'username')]
    if not identity[0] or not identity[2] or not request.get('password'):
        raise ValueError('请先绑定并选择紫鸟账号')
    # Only an opaque account identity is stored with exports; never credentials.
    return 'ziniao-account:' + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def runtime_paths() -> dict[str, str]:
    from backend.services.shopee_ads_recharge import _ensure_superbrowser_path
    _ensure_superbrowser_path()
    from util.ziniao_runtime_util import resolve_webdriver_path, resolve_ziniao_client_path
    return {'client_path': resolve_ziniao_client_path(None, 'v6'),
            'webdriver_path': resolve_webdriver_path(None)}


class _QuietLogger:
    """Legacy runtime diagnostics may include untrusted response text."""
    def info(self, *args, **kwargs):
        pass
    warning = error = debug = exception = info


class ZiniaoBrowser:
    def __init__(self, request: dict, *, browser=None, http=None):
        self.profile = account_profile(request)
        self.auth_failed = False
        self.cleanup_failed = False
        self.drivers: dict[str, Any] = {}
        self._opened: dict[str, tuple[str, int]] = {}
        self.targets: dict[str, str] = {}
        self.catalog: dict[str, dict] = {}
        self._raw_stores: dict[str, dict] = {}
        self.http = http or requests.Session()
        self.http.trust_env = False
        if browser is None:
            from backend.services.shopee_ads_recharge import _ensure_superbrowser_path
            _ensure_superbrowser_path()
            from main.super_browser_desktop import LoadSuperBrowser
            defaults = runtime_paths()
            client = str(request.get('client_path') or defaults['client_path']).strip()
            driver = str(request.get('webdriver_path') or defaults['webdriver_path']).strip()
            if not client or not Path(client).is_file():
                raise ValueError('未找到紫鸟客户端，请选择紫鸟客户端程序')
            if not driver:
                raise ValueError('请选择紫鸟驱动目录')
            browser = LoadSuperBrowser(
                func_dict={}, log=_QuietLogger(),
                pcfg={'ziniao': {'user_info': {key: request[key] for key in ('company', 'username', 'password')}},
                      'dingtalk': {'agent': {}}, 'task': []},
                cfg={'ziniao': {'browser': {'client_path': client, 'webdriver_path': driver,
                                           'socket_port': 16851, 'version': 'v6'}}})
        self.browser = browser
        # Preserve the existing account/automation protocol, while ensuring local
        # credentials cannot be sent through a proxy or an HTTP redirect.
        self.browser.send_http = self._send_http

    def _send_http(self, data: dict, timeout: float = 120):
        if self.auth_failed:
            raise ZiniaoBrowserError('紫鸟账号登录已失效，请核对所选账号后重试', auth=True)
        try:
            response = self.http.post('http://127.0.0.1:16851', json=data,
                                      timeout=min(timeout, 120), allow_redirects=False)
            if response.status_code != 200:
                raise ValueError()
            result = response.json()
            if not isinstance(result, dict) or 'statusCode' not in result:
                raise ValueError()
        except Exception:
            raise ZiniaoBrowserError('紫鸟自动化接口无有效响应，请检查客户端运行状态') from None
        if str(result['statusCode']) == '-10003':
            self.auth_failed = True
            raise ZiniaoBrowserError('紫鸟账号登录失败，请在设置中核对公司、账号和密码', auth=True)
        return result

    @staticmethod
    def _listening() -> bool:
        try:
            with socket.create_connection(('127.0.0.1', 16851), timeout=1):
                return True
        except OSError:
            return False

    def ready(self):
        if self.auth_failed:
            raise ZiniaoBrowserError('紫鸟账号登录已失效，请核对所选账号后重试', auth=True)
        if not self._listening():
            try:
                self.browser.start_browser()
            except Exception:
                raise ZiniaoBrowserError('紫鸟客户端启动失败，请检查客户端路径') from None
            deadline = time.monotonic() + 15
            while not self._listening():
                if time.monotonic() >= deadline:
                    raise ZiniaoBrowserError('紫鸟自动化模式未就绪。请结束其他紫鸟任务、退出普通模式客户端，再重新开始导出')
                time.sleep(.5)

    def list_stores(self) -> list[dict]:
        if self.auth_failed:
            raise ZiniaoBrowserError('紫鸟账号登录已失效，请核对所选账号后重试', auth=True)
        try:
            rows = self.browser.get_browser_list()
        except ZiniaoBrowserError:
            raise
        except Exception:
            raise ZiniaoBrowserError('无法读取所选紫鸟账号的店铺，请检查账号权限和客户端') from None
        if not isinstance(rows, list):
            raise ZiniaoBrowserError('紫鸟店铺列表结构异常')
        catalog, raw = {}, {}
        for row in rows:
            if not isinstance(row, dict):
                raise ZiniaoBrowserError('紫鸟店铺列表包含无效记录')
            identity = str(row.get('browserId') or row.get('browser_id') or row.get('id') or '').strip()
            name = str(row.get('browserName') or row.get('store_name') or '').strip()
            platform = str(row.get('platform_name') or row.get('platformName') or '').strip()
            if not identity or not identity.isdecimal() or not name or identity in catalog:
                raise ZiniaoBrowserError('紫鸟店铺缺少稳定 ID、完整名称或 ID 重复')
            catalog[identity] = {'store_id': identity, 'store_name': name, 'platform_name': platform}
            raw[identity] = row
        self.catalog, self._raw_stores = catalog, raw
        return list(catalog.values())

    def open_store(self, store: dict, *, url: str = ''):
        if self.cleanup_failed:
            raise ZiniaoBrowserError('上一店铺未确认关闭，已停止打开后续店铺')
        # This transport owns at most one store. Retire it before requesting
        # another, even if the caller forgot its normal per-store cleanup.
        for previous in list(self._opened):
            self.close_store(previous)
        identity = store['store_id']
        expected = self.catalog.get(identity)
        if not expected or expected['store_name'] != store['store_name']:
            raise ZiniaoBrowserError('店铺 ID 或名称与所选账号的预览不一致，请重新匹配')
        self.detach_store(identity)
        phase = '请求启动'
        try:
            opened = self.browser.open_store(identity)
            phase = '店铺身份核验'
            actual_id = str(opened.get('browserId') or opened.get('browser_id') or '')
            oauth = self._raw_stores[identity].get('browserOauth')
            if actual_id:
                if actual_id != identity:
                    raise ZiniaoBrowserError('紫鸟返回的店铺 ID 与预览不一致')
            elif not oauth or opened.get('browserOauth') != oauth:
                raise ZiniaoBrowserError('无法确认紫鸟打开的店铺身份')
            handle = str(opened.get('browserOauth') or oauth or identity)
            phase = '调试端口核验'
            try:
                port = int(opened.get('debuggingPort') or 0)
            except (TypeError, ValueError):
                port = 0
            self._opened[identity] = (handle, port)
            if not 1 <= port <= 65535:
                raise ZiniaoBrowserError('紫鸟未返回有效的店铺端口，无法验证店铺关闭状态')
            phase = '驱动连接'
            driver = self.browser.get_driver(opened)
            if driver is None:
                raise ZiniaoBrowserError('紫鸟未返回可操作的浏览器，请检查内核与驱动')
            self.drivers[identity] = driver
            phase = '驱动初始化'
            driver.set_page_load_timeout(45)
            driver.set_script_timeout(30)
            # The launcher initializes ZiNiao's store-specific session and login.
            # ZiNiao can switch proxy lines while its safety check is pending.
            # Allow that transition to finish; the real success marker remains
            # mandatory and a failed check must never open the business page.
            phase = '代理 IP 检测'
            driver.implicitly_wait(60)
            if not opened.get('ipDetectionPage') or not self.browser.open_ip_check(driver, opened['ipDetectionPage']):
                raise ZiniaoBrowserError('店铺代理 IP 检测未通过，已停止该店导出')
            driver.implicitly_wait(0)
            phase = '店铺登录入口'
            if not opened.get('launcherPage'):
                raise ZiniaoBrowserError('紫鸟未返回店铺登录入口')
            self.browser.open_launcher_page(driver, opened['launcherPage'])
            if url:
                phase = '业务页面导航'
                driver.get(url)
            if urlsplit(url).hostname == 'agentseller.temu.com':
                phase = 'TEMU 登录确认'
                from backend.services.temu_on_sale_login import ensure_temu_session
                try:
                    ensure_temu_session(driver)
                except ValueError as error:
                    raise ZiniaoBrowserError(str(error)) from None
            phase = '业务页签确认'
            self.targets[identity] = driver.current_window_handle
        except Exception as opening_error:
            if identity not in self._opened:
                # A lost or mismatched start response cannot prove that no
                # window opened. Stop the batch rather than open another.
                self.cleanup_failed = True
            try:
                self.close_store(identity)
            except Exception as cleanup_error:
                self.cleanup_failed = True
                # Preserve the opening phase without exposing response text,
                # credentials or exception messages from either failure.
                raise ZiniaoBrowserError(
                    f'打开店铺失败：{phase}阶段（{type(opening_error).__name__}）；'
                    '随后店铺未确认关闭，已暂停后续店铺。请关闭该店窗口后重试',
                    auth=bool(getattr(opening_error, 'auth', False)
                              or getattr(cleanup_error, 'auth', False) or self.auth_failed),
                ) from None
            if isinstance(opening_error, ZiniaoBrowserError):
                raise
            raise ZiniaoBrowserError('无法打开该店的紫鸟浏览器，请检查客户端、驱动或店铺登录状态') from None

    def _driver(self, identity: str):
        if self.auth_failed:
            raise ZiniaoBrowserError('紫鸟账号登录已失效，本次任务已停止', auth=True)
        driver = self.drivers.get(identity)
        if driver is None or not self.targets.get(identity):
            raise ZiniaoBrowserError('店铺浏览器连接已失效，请重试该店铺')
        driver.switch_to.window(self.targets[identity])
        return driver

    def evaluate(self, identity: str, script: str):
        try:
            value = self._driver(identity).execute_script('return (' + script + '\n);')
            return json.loads(value) if isinstance(value, str) else value
        except ZiniaoBrowserError:
            raise
        except Exception:
            raise ZiniaoBrowserError('读取紫鸟店铺页面失败，请检查页面、登录状态或人工验证') from None

    def click(self, identity: str, selector: str):
        try:
            elements = self._driver(identity).find_elements('css selector', selector)
            if len(elements) != 1:
                raise ZiniaoBrowserError('无法唯一定位可操作的页面控件，请重试该店铺')
            element = elements[0]
            # A newly rendered modal can have its full layout while its parent
            # is still fading in. Wait on this exact element, never re-resolve
            # its positional selector or repeat a click after an uncertain result.
            deadline = time.monotonic() + 3
            while not element.is_displayed() or not element.is_enabled():
                if time.monotonic() >= deadline:
                    raise ZiniaoBrowserError('页面控件尚不可操作，请重试该店铺')
                time.sleep(.1)
            element.click()
        except ElementClickInterceptedException:
            raise ZiniaoClickInterceptedError('页面控件被弹窗遮挡，尚未执行点击') from None
        except ZiniaoBrowserError:
            raise
        except Exception:
            raise ZiniaoBrowserError('紫鸟页面控件操作失败，请检查遮挡弹窗或页面变化') from None

    def detach_store(self, identity: str):
        driver = self.drivers.pop(identity, None)
        self.targets.pop(identity, None)
        if driver is not None:
            try:
                driver.service.stop()
            except Exception:
                pass

    @staticmethod
    def _port_open(port: int) -> bool:
        try:
            # Windows can take about two seconds to reject a closed loopback
            # port. A one-second timeout would falsely report it still open.
            with socket.create_connection(('127.0.0.1', port), timeout=3):
                return True
        except ConnectionRefusedError:
            return False
        except OSError:
            # A timeout or unknown networking error does not prove closure.
            return True

    def close_store(self, identity: str):
        opened = self._opened.get(identity)
        try:
            if opened is None:
                return
            handle, port = opened
            result = self.browser.close_store(handle)
            if not isinstance(result, dict) or str(result.get('statusCode')) != '0' or not 1 <= port <= 65535:
                raise ValueError()
            deadline = time.monotonic() + 15
            while self._port_open(port):
                if time.monotonic() >= deadline:
                    raise ValueError()
                time.sleep(.25)
            self._opened.pop(identity, None)
        except Exception:
            self.cleanup_failed = True
            raise ZiniaoBrowserError('店铺未确认关闭，已暂停后续店铺。请关闭该店窗口后重试') from None
        finally:
            self.detach_store(identity)

    def close(self):
        try:
            if not self.cleanup_failed:
                for identity in list(self._opened):
                    self.close_store(identity)
        finally:
            for identity in list(self.drivers):
                self.detach_store(identity)
            self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
