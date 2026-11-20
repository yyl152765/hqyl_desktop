"""Sign in through the China phone-login page, then finish TEMU authorization."""
from __future__ import annotations

from dataclasses import dataclass
import time
from urllib.parse import urlsplit

from selenium.common.exceptions import NoSuchWindowException, StaleElementReferenceException


REGION_ENTRY = "//div[contains(@class,'authentication_regionItem')][div[normalize-space()='中国地区']]//div[contains(@class,'authentication_goto')]"
LOGIN_BUTTON = "//button[normalize-space()='授权登录']"
CONFIRM_BUTTON = "//button[normalize-space()='确认授权并前往']"
PHONE_LOGIN_URL = 'https://seller.kuajingmaihuo.com/login?redirectUrl=https%3A%2F%2Fseller.kuajingmaihuo.com%2F'
PHONE_TAB = "//div[contains(@class,'password-login_tabItem') and normalize-space()='手机号登录']"
PHONE_BUTTON = "//button[normalize-space()='登录']"
GLOBAL_ENTRY = "//div[contains(@class,'site-main_content')][div[normalize-space()='全球']][div[normalize-space()='除欧区、美国']]/div[contains(@class,'site-main_enter') and normalize-space()='进入']"
SELLER_MARKER = '[class*="account-info_mallInfo__"],._Wrz4-O9w'
AUTOFILL_WAIT = 10
LOGIN_READY = """return (() => {
  const visible=e=>e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
  const passwords=[...document.querySelectorAll('input[type=password]')].filter(visible);
  const accounts=[...document.querySelectorAll('input#usernameId,input[placeholder="手机号码"],input[placeholder="请输入手机号"]')].filter(visible);
  const confirmation=[...document.querySelectorAll('button')].filter(visible)
    .some(e=>e.innerText.trim()==='确认授权并前往') &&
    [...document.querySelectorAll('input')].filter(visible).every(e=>e.type==='checkbox');
  const challenge=[...document.querySelectorAll('[role=dialog],.ant-modal,.el-dialog,[data-testid="beast-core-modal-container"],iframe[src*="captcha"]')]
    .filter(visible).some(e=>e.tagName==='IFRAME' || /验证码|安全验证|滑块|验证身份/.test(e.innerText));
  const keys=performance.getEntriesByType('resource').filter(e=> {
    const url=new URL(e.name,location.href);
    return ['fetch','xmlhttprequest'].includes(e.initiatorType) && url.origin===location.origin &&
      url.pathname==='/bg/quiet/api/mms/key/login' && e.responseEnd>e.startTime;
  });
  const key=keys[keys.length-1];
  return {filled:passwords.length===1 && accounts.length===1 && !!passwords[0].value && !!accounts[0].value,
    form:passwords.length>0, confirmation,challenge,
    key_ready:!!key && key.responseStatus===200,
    key_error:document.body.innerText.includes('获取公钥失败') || (!!key && key.responseStatus>=400)};
})();"""


@dataclass
class _PhoneLogin:
    return_url: str
    selected: bool = False
    submitted: bool = False
    refreshed: bool = False
    wait_since: float | None = None
    resumed: bool = False
    resume_since: float | None = None


def _visible(driver, by, selector):
    return [element for element in driver.find_elements(by, selector)
            if element.is_displayed() and element.is_enabled()]


def _inspection_handles(driver, known_urls, preferred=None):
    """Choose the relevant tab without activating every window on each poll."""
    handles = list(driver.window_handles)
    if not handles:
        return []
    try:
        targets = driver.execute_cdp_cmd('Target.getTargets', {}).get('targetInfos', [])
    except Exception:
        targets = []
    for target in targets:
        if target.get('type') != 'page':
            continue
        for handle in handles:
            if handle.removeprefix('CDwindow-') == target.get('targetId'):
                known_urls[handle] = str(target.get('url') or '')
    business, login, sites, seller, region, unknown = [], [], [], [], [], []
    for handle in handles:
        if handle not in known_urls:
            unknown.append(handle)
            continue
        url = urlsplit(known_urls[handle])
        if url.hostname == 'agentseller.temu.com':
            (region if url.path.startswith('/auth') else business).append(handle)
        elif url.hostname == 'seller.kuajingmaihuo.com':
            path = url.path.rstrip('/')
            (sites if path == '/settle/site-main' else login if path in {'/login', '/settle/seller-login'} else seller).append(handle)
    # ZiNiao may open an authenticated business tab while leaving /login open.
    # Discover new tabs and changed business routes before staying on autofill.
    if preferred in handles:
        return unknown or list(dict.fromkeys(business + sites + [preferred]))
    # An old global tab may never become ready. It must not starve another
    # authenticated tab or the login popup; unrelated tabs are still excluded.
    return unknown or list(dict.fromkeys(business + sites + (login or seller or region or handles[:1])))


def _prepare_submit(driver, selector, *, wait_for_controls=False, optional_consent=False):
    labels = _visible(driver, 'css selector', 'label:has(input[type="checkbox"])')
    # The login button may become enabled only after consent is checked.
    buttons = [e for e in driver.find_elements('xpath', selector) if e.is_displayed()]
    # The site picker omits the checkbox after authorization was already
    # granted. A hidden/not-yet-operable checkbox still requires waiting.
    if optional_consent and not labels and not driver.find_elements('css selector', 'input[type="checkbox"]'):
        if len(buttons) > 1:
            raise ValueError('无法唯一确认 TEMU 登录控件，请在紫鸟中检查登录页面')
        return buttons[0] if len(buttons) == 1 and buttons[0].is_enabled() else False
    if wait_for_controls and (not labels or not buttons):
        return False
    if len(labels) != 1 or len(buttons) != 1:
        raise ValueError('无法唯一确认 TEMU 登录控件，请在紫鸟中检查登录页面')
    checkbox = labels[0].find_element('css selector', 'input[type="checkbox"]')
    if not checkbox.is_selected():
        labels[0].click()
    buttons = _visible(driver, 'xpath', selector)
    if len(buttons) != 1:
        return False
    # The caller marks the submission before clicking: a closed popup or a
    # stale element after dispatch must never cause an uncertain resubmission.
    return buttons[0]


def _phone_login(driver, state, attempt, now):
    if attempt.submitted:
        if state.get('key_error'):
            raise ValueError('TEMU 手机号登录提交后获取公钥失败，请在紫鸟中检查登录结果后重试')
        return
    tabs = _visible(driver, 'xpath', PHONE_TAB)
    if len(tabs) > 1:
        raise ValueError('无法唯一确认 TEMU 手机号登录选项')
    if not tabs:
        return
    selected = ('tabItemChecked' in (tabs[0].get_attribute('class') or '')
                or tabs[0].get_attribute('aria-selected') == 'true')
    if not attempt.selected:
        if not selected:
            tabs[0].click()
        attempt.selected = True
        attempt.wait_since = now
        return  # Re-read the phone form after switching away from QR/email.
    if state.get('key_error') or not selected or not state.get('filled') or not state.get('key_ready'):
        if not state.get('key_error') and now - attempt.wait_since < AUTOFILL_WAIT:
            return
        if attempt.refreshed:
            if state.get('key_error'):
                reason = '获取公钥失败'
            elif not selected:
                reason = '选项未切换完成'
            elif not state.get('filled'):
                reason = '账号或密码未自动填充'
            else:
                reason = '登录公钥初始化未完成'
            raise ValueError(f'TEMU 手机号登录{reason}，刷新一次后仍未恢复，请在紫鸟中检查后重试')
        attempt.refreshed = True
        attempt.selected = False
        attempt.wait_since = None
        driver.refresh()
        return
    button = _prepare_submit(driver, PHONE_BUTTON)
    if button:
        attempt.submitted = True
        button.click()


def ensure_temu_session(driver, *, timeout: float = 60,
                        business_url: str = 'https://agentseller.temu.com/goods/list') -> str:
    """Return the authenticated business tab; never extract or persist credentials."""
    deadline = time.monotonic() + timeout
    if urlsplit(business_url).hostname != 'agentseller.temu.com':
        raise ValueError('无法确认 TEMU 业务页面地址')
    region_clicked = set()
    confirmed = set()
    site_entered = set()
    phone_logins = {}
    known_urls = {}
    while time.monotonic() < deadline:
        preferred = next((h for h, a in phone_logins.items() if not a.resumed and h in driver.window_handles), None)
        for handle in _inspection_handles(driver, known_urls, preferred):
            try:
                try:
                    current = driver.current_window_handle
                except (AttributeError, NoSuchWindowException):
                    current = None
                if current != handle:
                    driver.switch_to.window(handle)
                known_urls[handle] = driver.current_url
                url = urlsplit(driver.current_url)
                if url.hostname == 'agentseller.temu.com':
                    if _visible(driver, 'css selector', '[class*="account-info_mallInfo__"]'):
                        return handle
                    if url.path == '/auth/authentication' and handle not in region_clicked:
                        entries = _visible(driver, 'xpath', REGION_ENTRY)
                        if len(entries) > 1:
                            raise ValueError('无法唯一确认 TEMU 中国地区商家入口')
                        if len(entries) == 1:
                            region_clicked.add(handle)
                            entries[0].click()
                            break
                elif url.hostname == 'seller.kuajingmaihuo.com' and url.path.rstrip('/') == '/settle/site-main':
                    if driver.execute_script(LOGIN_READY).get('challenge'):
                        raise ValueError('TEMU 登录需要人工验证，请在紫鸟中处理后重试')
                    if handle not in site_entered and _visible(driver, 'xpath', GLOBAL_ENTRY):
                        entry = _prepare_submit(driver, GLOBAL_ENTRY, wait_for_controls=True, optional_consent=True)
                        if entry:
                            site_entered.add(handle)
                            if not driver.find_elements('css selector', 'input[type="checkbox"]'):
                                # Already-authorized variant has no consent
                                # control. Restore the requested route instead
                                # of repeating a region authorization.
                                attempt = phone_logins.get(handle)
                                driver.get(attempt.return_url if attempt else business_url)
                                if attempt:
                                    attempt.resumed = True
                                    attempt.resume_since = time.monotonic()
                            else:
                                entry.click()
                elif url.hostname == 'seller.kuajingmaihuo.com' and url.path.rstrip('/') in {'/login', '/settle/seller-login'}:
                    state = driver.execute_script(LOGIN_READY)
                    if state.get('challenge'):
                        raise ValueError('TEMU 登录需要人工验证，请在紫鸟中处理后重试')
                    if url.path.rstrip('/') == '/login':
                        attempt = phone_logins.setdefault(handle, _PhoneLogin(business_url))
                        _phone_login(driver, state, attempt, time.monotonic())
                        continue
                    if state.get('confirmation'):
                        if handle in confirmed:
                            continue
                        button = _prepare_submit(driver, CONFIRM_BUTTON)
                        if button:
                            confirmed.add(handle)
                            button.click()
                            break
                    elif state.get('form') or state.get('key_error'):
                        if handle in phone_logins:
                            attempt = phone_logins[handle]
                            # The SSO page renders a password form before its
                            # session lookup switches to authorization. Allow
                            # that read-only transition without resubmitting.
                            if attempt.resume_since is not None and time.monotonic() - attempt.resume_since < AUTOFILL_WAIT:
                                continue
                            raise ValueError('中国卖家中心登录后仍未恢复 TEMU 授权，请在紫鸟中检查后重试')
                        # Keep the complete original SSO URL only in memory,
                        # so its opener and authorization parameters survive.
                        phone_logins[handle] = _PhoneLogin(driver.current_url)
                        driver.get(PHONE_LOGIN_URL)
                elif url.hostname == 'seller.kuajingmaihuo.com' and handle in phone_logins:
                    attempt = phone_logins[handle]
                    if not attempt.resumed and _visible(driver, 'css selector', SELLER_MARKER):
                        attempt.resumed = True
                        driver.get(attempt.return_url)
                        attempt.resume_since = time.monotonic()
            except (NoSuchWindowException, StaleElementReferenceException):
                # Successful authorization closes the popup and returns to the
                # original TEMU tab; inspect surviving tabs on the next pass.
                continue
        time.sleep(.5)
    raise ValueError('TEMU 自动登录未完成，请检查紫鸟保存的店铺登录信息或处理人工验证后重试')
