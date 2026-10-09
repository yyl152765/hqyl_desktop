"""Complete TEMU's region/login popup using ZiNiao's existing autofill."""
from __future__ import annotations

import time
from urllib.parse import urlsplit

from selenium.common.exceptions import NoSuchWindowException


REGION_ENTRY = "//div[contains(@class,'authentication_regionItem')][div[normalize-space()='中国地区']]//div[contains(@class,'authentication_goto')]"
LOGIN_BUTTON = "//button[normalize-space()='授权登录']"
CONFIRM_BUTTON = "//button[normalize-space()='确认授权并前往']"
LOGIN_READY = """return (() => {
  const visible=e=>e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
  const passwords=[...document.querySelectorAll('input[type=password]')].filter(visible);
  const accounts=[...document.querySelectorAll('input[placeholder="手机号码"],input[placeholder="邮箱"]')].filter(visible);
  const confirmation=[...document.querySelectorAll('button')].filter(visible)
    .some(e=>e.innerText.trim()==='确认授权并前往') &&
    [...document.querySelectorAll('input')].filter(visible).every(e=>e.type==='checkbox');
  const challenge=[...document.querySelectorAll('[role=dialog],.ant-modal,.el-dialog,iframe[src*="captcha"]')]
    .filter(visible).some(e=>e.tagName==='IFRAME' || /验证码|安全验证|滑块|验证身份/.test(e.innerText));
  return {filled:passwords.length===1 && accounts.length===1 && !!passwords[0].value && !!accounts[0].value,confirmation,challenge};
})();"""


def _visible(driver, by, selector):
    return [element for element in driver.find_elements(by, selector)
            if element.is_displayed() and element.is_enabled()]


def ensure_temu_session(driver, *, timeout: float = 60) -> str:
    """Return the authenticated business tab; never extract or persist credentials."""
    deadline = time.monotonic() + timeout
    region_clicked = submitted = confirmed = False
    while time.monotonic() < deadline:
        for handle in list(driver.window_handles):
            try:
                driver.switch_to.window(handle)
                url = urlsplit(driver.current_url)
                if url.hostname == 'agentseller.temu.com':
                    if _visible(driver, 'css selector', '[class*="account-info_mallInfo__"]'):
                        return handle
                    if url.path == '/auth/authentication' and not region_clicked:
                        entries = _visible(driver, 'xpath', REGION_ENTRY)
                        if len(entries) > 1:
                            raise ValueError('无法唯一确认 TEMU 中国地区商家入口')
                        if len(entries) == 1:
                            region_clicked = True
                            entries[0].click()
                            break
                elif url.hostname == 'seller.kuajingmaihuo.com' and url.path == '/settle/seller-login':
                    state = driver.execute_script(LOGIN_READY)
                    if state.get('challenge'):
                        raise ValueError('TEMU 登录需要人工验证，请在紫鸟中处理后重试')
                    if state.get('confirmation'):
                        if confirmed:
                            continue
                        selector = CONFIRM_BUTTON
                    else:
                        if submitted or not state.get('filled'):
                            continue
                        selector = LOGIN_BUTTON
                    labels = _visible(driver, 'css selector', 'label:has(input[type="checkbox"])')
                    buttons = _visible(driver, 'xpath', selector)
                    if len(labels) != 1 or len(buttons) != 1:
                        raise ValueError('无法唯一确认 TEMU 登录控件，请在紫鸟中检查登录页面')
                    checkbox = labels[0].find_element('css selector', 'input[type="checkbox"]')
                    if not checkbox.is_selected():
                        labels[0].click()
                    if selector == CONFIRM_BUTTON:
                        confirmed = True
                    else:
                        submitted = True
                    buttons[0].click()
                    break
            except NoSuchWindowException:
                # Successful authorization closes the popup and returns to the
                # original TEMU tab; inspect surviving tabs on the next pass.
                continue
        time.sleep(.5)
    raise ValueError('TEMU 自动登录未完成，请检查紫鸟保存的店铺登录信息或处理人工验证后重试')
