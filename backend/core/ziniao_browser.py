"""ZiNiao account login using the same desktop runtime as Shopee Ads."""
from __future__ import annotations

import hashlib
import json
import errno
from pathlib import Path
import re
import socket
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from selenium.common.exceptions import ElementClickInterceptedException, NoSuchWindowException

from backend.core.ziniao_ip_check import ZiniaoIPCheckError, ensure_ziniao_ip_check
from backend.core.ziniao_driver import ZiniaoDriverError, ensure_ziniao_driver
from backend.core.ziniao_startup import ZiniaoStartupError, prepare_ziniao_client, ziniao_port_owned


class ZiniaoBrowserError(RuntimeError):
    def __init__(self, message: str, *, auth: bool = False):
        super().__init__(message)
        self.auth = auth


class ZiniaoClickInterceptedError(ZiniaoBrowserError):
    """Selenium confirmed that an overlay prevented the click from occurring."""


class ZiniaoPageNavigatingError(ZiniaoBrowserError):
    """A read-only script lost its document while the page was navigating."""


class ZiniaoTransportError(ZiniaoBrowserError):
    """The local automation request has no trustworthy response."""


def _driver_os_error_detail(error: Exception, opened: dict) -> str:
    """Expose numeric OS diagnostics, never exception text or private paths."""
    if not isinstance(error, OSError):
        return ''
    details = []
    version = opened.get('core_version')
    if isinstance(version, str) and re.fullmatch(r'[0-9]{1,4}(?:\.[0-9]{1,6}){0,3}', version):
        major = version.split('.')[0]
        details.extend((f'紫鸟内核 {version}', f'对应驱动 chromedriver{major}.exe'))
    winerror = getattr(error, 'winerror', None)
    code = getattr(error, 'errno', None)
    winerror = winerror if type(winerror) is int and 0 < winerror <= 65535 else None
    code = code if type(code) is int and 0 < code <= 65535 else None
    if winerror is not None:
        details.append(f'WinError {winerror}')
    elif code is not None:
        details.append(f'errno {code}')
    advice = {
        2: '驱动程序或依赖文件不存在，请检查驱动目录和安全软件隔离记录',
        3: '驱动或临时目录路径不存在，请检查目录是否可用',
        5: '系统拒绝访问，请检查驱动目录读写、执行权限和安全软件拦截记录',
        32: '驱动相关文件被占用，请结束相关运行任务后重试',
        112: '驱动或临时目录所在磁盘空间不足',
        193: '文件不是有效的 Windows 可执行程序，请核对并重新获取匹配版本的紫鸟官方驱动',
        216: '可执行程序与当前 Windows 系统架构不兼容，请核对系统及驱动架构',
    }.get(winerror)
    if not advice and winerror is None:
        advice = {errno.ENOENT: '驱动程序或依赖文件不存在，请检查驱动目录',
                  errno.EACCES: '系统拒绝访问，请检查目录读写、执行权限和安全软件拦截记录',
                  errno.ENOEXEC: '驱动可执行文件格式异常，请重新获取匹配版本的紫鸟官方驱动',
                  errno.ENOSPC: '驱动或临时目录所在磁盘空间不足'}.get(code)
    details.append(advice or '请在用户电脑对该版本驱动执行 --version，提供执行结果以定位原因')
    return '；' + '；'.join(details)


_CLICK_TARGET_STATE = r'''
const element=arguments[0];
if(!element || !element.isConnected) return {connected:false};
if(arguments[1]) element.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
const rect=element.getClientRects()[0];
if(!rect) return {connected:true,clear:false};
// Match WebDriver's in-view centre, including targets clipped by the viewport.
const left=Math.max(0,Math.min(rect.x,rect.x+rect.width));
const right=Math.min(innerWidth,Math.max(rect.x,rect.x+rect.width));
const top=Math.max(0,Math.min(rect.y,rect.y+rect.height));
const bottom=Math.min(innerHeight,Math.max(rect.y,rect.y+rect.height));
if(right<=left || bottom<=top) return {connected:true,clear:false};
const x=Math.floor((left+right)/2),y=Math.floor((top+bottom)/2);
const hit=document.elementFromPoint(x,y);
return {connected:true,clear:Boolean(hit && (hit===element || element.contains(hit))),x,y};
'''


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
    from backend.core.ziniao_paths import resolve_ziniao_client_path as discover_client
    client = discover_client(require_v6=True)
    if not client:
        candidate = Path(resolve_ziniao_client_path(None, 'v6') or '')
        if candidate.name.casefold() == 'ziniao.exe' and candidate.is_file():
            client = str(candidate)
    return {'client_path': client,
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
        self.progress = None
        self._task_prepared = False
        self._credentials = {key: request[key] for key in ('company', 'username', 'password')}
        self._native_runtime = browser is None
        self.http = http or requests.Session()
        self.http.trust_env = False
        if browser is None:
            from backend.services.shopee_ads_recharge import _ensure_superbrowser_path
            _ensure_superbrowser_path()
            from main.super_browser_desktop import LoadSuperBrowser
            defaults = runtime_paths()
            client = str(request.get('client_path') or defaults['client_path']).strip()
            driver = str(request.get('webdriver_path') or defaults['webdriver_path']).strip()
            if not client or not Path(client).is_file() or Path(client).name.casefold() != 'ziniao.exe':
                raise ValueError('未找到紫鸟 V6，请先通过页面的“下载紫鸟 V6”安装，再点击“重新检测”或选择 ziniao.exe')
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
        if self._native_runtime:
            try:
                if not ziniao_port_owned(self.browser.client_path):
                    raise ZiniaoTransportError('紫鸟自动化接口尚未监听，请检查客户端运行状态')
            except ZiniaoStartupError as error:
                raise ZiniaoBrowserError(str(error)) from None
        try:
            response = self.http.post('http://127.0.0.1:16851', json=data,
                                      timeout=min(timeout, 120), allow_redirects=False)
            if response.status_code != 200:
                raise ValueError()
            result = response.json()
            if not isinstance(result, dict) or 'statusCode' not in result:
                raise ValueError()
        except Exception:
            raise ZiniaoTransportError('紫鸟自动化接口无有效响应，请检查客户端运行状态') from None
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

    def prepare_task(self, *, progress=None):
        """Restart once before a real batch, never during preview or per store."""
        if self._task_prepared:
            return
        if self.auth_failed or self.cleanup_failed or self._opened or self.drivers:
            raise ZiniaoBrowserError('当前紫鸟会话不可重新启动，请结束本次任务后重试', auth=self.auth_failed)
        self.progress = progress
        try:
            client_path = self.browser.client_path
            prepare_ziniao_client(client_path, progress=progress)
            if progress:
                progress('正在启动紫鸟 V6 自动化模式')
            self.browser.start_browser()
            deadline = time.monotonic() + 45
            while not ziniao_port_owned(client_path):
                if time.monotonic() >= deadline:
                    raise ZiniaoBrowserError('旧进程已清理，但紫鸟自动化模式在 45 秒内未就绪，请检查 V6 安装和安全软件拦截记录')
                time.sleep(.5)
            # Ownership alone is insufficient: the listener must also speak the
            # automation protocol before receiving the selected credentials.
            result = self._send_http({'action': 'getRunningInfo', 'requestId': str(uuid4())}, timeout=5)
            if str(result.get('statusCode')) != '0':
                raise ZiniaoBrowserError('紫鸟自动化接口尚未就绪，请更新紫鸟 V6 后重试')
            self._prepare_core(progress)
            self._task_prepared = True
            if progress:
                progress('紫鸟自动化模式已就绪，开始读取店铺')
        except ZiniaoStartupError as error:
            raise ZiniaoBrowserError(str(error)) from None
        except ZiniaoBrowserError:
            raise
        except Exception as error:
            raise ZiniaoBrowserError(f'紫鸟启动准备失败（{type(error).__name__}），请检查 V6 客户端和本机权限') from None

    def _prepare_core(self, progress):
        """Fresh V6 installs may need browser cores; never use the legacy infinite loop."""
        if progress:
            progress('正在检查紫鸟浏览器内核，首次安装可能需要下载')
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                result = self._send_http({'action': 'updateCore', 'requestId': str(uuid4()), **self._credentials},
                                         timeout=min(20, remaining))
                if str(result.get('statusCode')) == '0':
                    return
            except ZiniaoTransportError:
                pass  # Downloads may outlast a single HTTP request.
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        raise ZiniaoBrowserError('紫鸟内核在 180 秒内未准备完成，请检查网络或先在紫鸟 V6 中完成内核下载后重试')

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
            try:
                ensure_ziniao_driver(self.browser, opened, progress=self.progress)
            except ZiniaoDriverError as error:
                raise ZiniaoBrowserError(str(error)) from None
            driver = self.browser.get_driver(opened)
            if driver is None:
                raise ZiniaoBrowserError('紫鸟未返回可操作的浏览器，请检查内核与驱动')
            self.drivers[identity] = driver
            phase = '驱动初始化'
            driver.set_page_load_timeout(45)
            driver.set_script_timeout(30)
            # The shared boolean helper masks navigation timeouts as failed IP
            # checks. Observe the original check page with a bounded local
            # adapter; only its real success marker permits the launcher.
            phase = '代理 IP 检测'
            try:
                ensure_ziniao_ip_check(driver, opened.get('ipDetectionPage'))
            except ZiniaoIPCheckError as error:
                raise ZiniaoBrowserError(str(error)) from None
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
                    ensure_temu_session(driver, business_url=url)
                except ValueError as error:
                    raise ZiniaoBrowserError(str(error)) from None
            phase = '业务页签确认'
            self.targets[identity] = driver.current_window_handle
        except Exception as opening_error:
            driver_detail = _driver_os_error_detail(opening_error, opened) if phase == '驱动连接' else ''
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
                    f'打开店铺失败：{phase}阶段（{type(opening_error).__name__}）{driver_detail}；'
                    '随后店铺未确认关闭，已暂停后续店铺。请关闭该店窗口后重试',
                    auth=bool(getattr(opening_error, 'auth', False)
                              or getattr(cleanup_error, 'auth', False) or self.auth_failed),
                ) from None
            if isinstance(opening_error, ZiniaoBrowserError):
                raise
            raise ZiniaoBrowserError(
                f'打开店铺失败：{phase}阶段（{type(opening_error).__name__}）'
                + (driver_detail or '，请检查客户端、驱动或店铺登录状态'),
                auth=bool(getattr(opening_error, 'auth', False) or self.auth_failed),
            ) from None

    def _driver(self, identity: str):
        if self.auth_failed:
            raise ZiniaoBrowserError('紫鸟账号登录已失效，本次任务已停止', auth=True)
        driver = self.drivers.get(identity)
        if driver is None or not self.targets.get(identity):
            raise ZiniaoBrowserError('店铺浏览器连接已失效，请重试该店铺')
        try:
            current = driver.current_window_handle
        except NoSuchWindowException:
            current = None
        if current != self.targets[identity]:
            driver.switch_to.window(self.targets[identity])
        return driver

    def evaluate(self, identity: str, script: str):
        try:
            value = self._driver(identity).execute_script('return (' + script + '\n);')
            return json.loads(value) if isinstance(value, str) else value
        except ZiniaoBrowserError:
            raise
        except Exception as exc:
            if any(marker in str(exc).casefold() for marker in (
                'execution context was destroyed',
                'cannot find context with specified id',
                'document unloaded while waiting for result',
            )):
                raise ZiniaoPageNavigatingError('店铺页面正在跳转，请稍后重试') from None
            raise ZiniaoBrowserError('读取紫鸟店铺页面失败，请检查页面、登录状态或人工验证') from None

    def click(self, identity: str, selector: str):
        try:
            driver = self._driver(identity)
            elements = driver.find_elements('css selector', selector)
            if len(elements) != 1:
                raise ZiniaoBrowserError('无法唯一定位可操作的页面控件，请重试该店铺')
            element = elements[0]
            # Visible/enabled controls may still be moving under an animation
            # mask. Observe only this exact element, without dismissing unknown
            # overlays or re-resolving a positional selector. Two clear samples
            # at the same centre avoid dispatching into a moving drawer.
            deadline = time.monotonic() + 3
            previous_center = None
            scrolled = False
            while True:
                operable = element.is_displayed() and element.is_enabled()
                if operable:
                    state = driver.execute_script(_CLICK_TARGET_STATE, element, not scrolled)
                    scrolled = True
                    if not isinstance(state, dict) or state.get('connected') is not True:
                        raise ZiniaoBrowserError('页面控件已变化，尚未执行点击，请重试该店铺')
                    center = (state.get('x'), state.get('y')) if state.get('clear') is True else None
                    if center is not None and center == previous_center:
                        break
                    previous_center = center
                else:
                    previous_center = None
                if time.monotonic() >= deadline:
                    if not operable:
                        raise ZiniaoBrowserError('页面控件尚不可操作，请重试该店铺')
                    raise ZiniaoClickInterceptedError('页面控件被遮挡或仍在移动，尚未执行点击')
                time.sleep(.1)
            # Exactly one dispatch. Errors after this point never retry here.
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
        phase = '关闭请求'
        try:
            if opened is None:
                return
            handle, port = opened
            try:
                result = self.browser.close_store(handle)
            except ZiniaoTransportError:
                # stopBrowser can complete after its eight-second HTTP timeout.
                # Never repeat the request or assume it succeeded: confirm the
                # exact debug port recorded for this owned store has closed.
                result = None
            if self.auth_failed or (result is not None and
                    (not isinstance(result, dict) or str(result.get('statusCode')) != '0')):
                raise ValueError()
            if not 1 <= port <= 65535:
                phase = '店铺端口无效'
                raise ValueError()
            phase = '等待店铺窗口退出'
            deadline = time.monotonic() + 15
            while self._port_open(port):
                if time.monotonic() >= deadline:
                    raise ValueError()
                time.sleep(.25)
            self._opened.pop(identity, None)
        except Exception as error:
            self.cleanup_failed = True
            raise ZiniaoBrowserError(
                f'店铺未确认关闭（{phase}），已暂停后续店铺。请关闭该店窗口后重试',
                auth=self.auth_failed or bool(getattr(error, 'auth', False)),
            ) from None
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
