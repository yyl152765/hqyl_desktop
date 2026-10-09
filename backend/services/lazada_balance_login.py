"""Navigate a store-scoped Selenium session through Lazada's saved login.

Only the current driver's tabs are inspected. No credentials leave the page:
scripts report booleans, and login submission uses Selenium's trusted click.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urljoin, urlsplit

from backend.services.lazada_balance_language import LazadaLanguageError, ensure_lazada_login_chinese


SELLER_HOSTS = frozenset({
    "sellercenter.lazada.com.ph", "sellercenter.lazada.com.my",
    "sellercenter.lazada.co.th", "sellercenter.lazada.co.id",
    "sellercenter.lazada.vn", "sellercenter.lazada.sg",
})
GSP_HOSTS = frozenset({"gsp.lazada.com", "gsp.lazada-seller.cn"})
BUSINESS_PATHS = frozenset({
    "/portal/apps/finance/myIncome/index", "/apps/balance",
    "/sponsor/solutions/ads/productads",
})


class LazadaLoginError(ValueError):
    """The target finance page or a single saved-login attempt is unverified."""


LOGIN_STATUS_SCRIPT = r"""
/* lazada_login_feedback */
const visible = el => {
  const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const body = String(document.body?.innerText || '');
return {
  rejected: /PASSWORD_NOT_MATCH|账户或密码不正确|账号或密码错误|密码错误|incorrect password|invalid (?:account|password)|wrong password/i.test(body),
  challenge: Array.from(document.querySelectorAll('iframe[src*="captcha" i],iframe[src*="recaptcha" i],[class*="captcha" i],[id*="captcha" i]')).some(visible)
    || /请完成验证|拖动滑块|安全验证|请输入验证码|please complete the verification|enter verification code|security verification/i.test(body)
};
"""

PREFILLED_STATE_SCRIPT = r"""
/* lazada_login_prefilled_booleans */
const gsp = arguments[0];
const visible = el => {
  const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const accounts = Array.from(document.querySelectorAll(gsp ? 'input[name="account"],input[placeholder="Email"]' : 'input[name="account"]')).filter(visible);
const passwords = Array.from(document.querySelectorAll(gsp ? 'input[name="password"][type="password"],input[type="password"][placeholder="Password"]' : 'input[name="password"][type="password"]')).filter(visible);
return {filled: accounts.length === 1 && passwords.length === 1
  && accounts[0].form === passwords[0].form && !!accounts[0].value && !!passwords[0].value};
"""


def _parts(value: object):
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
        return parsed
    except ValueError:
        return None


def _trusted(value: object, target_host: str) -> bool:
    parsed = _parts(value)
    return bool(parsed and parsed.hostname in ({target_host} | GSP_HOSTS))


def is_lazada_login_url(value: object) -> bool:
    """Recognize exact supported login/register routes, never substring hosts."""
    parsed = _parts(value)
    if not parsed:
        return False
    path = parsed.path.rstrip("/")
    if parsed.hostname in GSP_HOSTS:
        return path == "/page/login"
    return parsed.hostname in SELLER_HOSTS and (
        path == "/apps/seller/login" or path == "/apps/register" or path.startswith("/apps/register/")
    )


def _register(value: str) -> bool:
    parsed = _parts(value)
    return bool(parsed and parsed.hostname in SELLER_HOSTS and
                (parsed.path.rstrip("/") == "/apps/register" or parsed.path.startswith("/apps/register/")))


def _matches(value: str, target: str) -> bool:
    actual, expected = _parts(value), _parts(target)
    if not actual or not expected or actual.hostname != expected.hostname:
        return False
    if actual.path.rstrip("/") == expected.path.rstrip("/"):
        return not expected.fragment or actual.fragment == expected.fragment
    # An explicit product denial is a readable result, not a successful balance.
    return expected.path.rstrip("/") == "/apps/balance" and actual.path.rstrip("/") in {
        "/apps/balance/unaccessable", "/apps/balance/unavailable",
    }


def _tabs(driver) -> dict[str, str]:
    """Inspect URLs, restoring the original selected tab when it still exists."""
    try:
        original = driver.current_window_handle
        handles = list(driver.window_handles)
    except Exception as exc:
        raise LazadaLoginError("无法枚举当前紫鸟店铺的页签") from exc
    found = {}
    try:
        for handle in handles:
            try:
                driver.switch_to.window(handle)
                found[handle] = str(driver.current_url or "")
            except Exception:
                # A just-closed authentication tab may disappear while polling.
                continue
    finally:
        if original in found:
            driver.switch_to.window(original)
    return found


def _feedback(driver, state: dict) -> None:
    feedback = driver.execute_script(LOGIN_STATUS_SCRIPT)
    if not isinstance(feedback, dict) or type(feedback.get("rejected")) is not bool or type(feedback.get("challenge")) is not bool:
        raise LazadaLoginError("无法确认 Lazada 登录反馈，已停止自动提交")
    if feedback["rejected"]:
        state["blocked"] = "Lazada 提示账号或密码错误，请人工检查后重试"
    elif feedback["challenge"]:
        state["blocked"] = "Lazada 登录出现验证码或安全验证，请人工完成后重试"
    if state.get("blocked"):
        raise LazadaLoginError(state["blocked"])


def _select(driver, candidates: list[str], preferred: str | None = None) -> str:
    if preferred in candidates:
        handle = preferred
    elif len(candidates) == 1:
        handle = candidates[0]
    else:
        raise LazadaLoginError("Lazada 目标页签不存在或存在多个候选，无法确认登录页面")
    driver.switch_to.window(handle)
    return handle


def ensure_lazada_page(driver, target_url: str, login_state: dict, timeout: float = 30) -> str:
    """Return the verified finance tab handle, sharing at most one login click.

    A caller must pass the same ``login_state`` to every field of a store. Failed
    credentials, verification challenges and uncertain click outcomes block any
    subsequent automatic attempt. Raw credentials and feedback text are not kept.
    """
    target = _parts(target_url)
    if not target or target.hostname not in SELLER_HOSTS or target.path.rstrip("/") not in BUSINESS_PATHS or target.query:
        raise LazadaLoginError("Lazada 目标资金网址不受支持")
    if login_state.get("blocked"):
        raise LazadaLoginError(str(login_state["blocked"]))
    if timeout <= 0:
        raise LazadaLoginError("Lazada 页面等待时间必须大于 0")
    target_host = target.hostname
    tabs = _tabs(driver)
    current = getattr(driver, "current_window_handle", None)
    exact = [handle for handle, url in tabs.items() if _matches(url, target_url)]
    if exact:
        handle = _select(driver, exact, current)
        login_state["business_handle"] = handle
        return handle
    eligible = [handle for handle, url in tabs.items() if _trusted(url, target_host)]
    selected = _select(driver, eligible, current)
    current_url = tabs[selected]
    if not is_lazada_login_url(current_url):
        driver.get(target_url)

    deadline = time.monotonic() + float(timeout)
    baseline = _tabs(driver)
    register_clicked = False
    submitted_here = False
    returned_to_target = False
    observed_login = is_lazada_login_url(str(driver.current_url or ""))
    last_was_login = observed_login
    while time.monotonic() < deadline:
        current_tabs = _tabs(driver)
        handle = getattr(driver, "current_window_handle", None)
        changed = [key for key, url in current_tabs.items() if baseline.get(key) != url and _trusted(url, target_host)]
        if changed:
            desired = [key for key in changed if _matches(current_tabs[key], target_url)]
            desired = desired or [key for key in changed if is_lazada_login_url(current_tabs[key])]
            desired = desired or changed
            handle = _select(driver, desired, handle)
        elif handle not in current_tabs:
            handle = _select(driver, [key for key, url in current_tabs.items() if _trusted(url, target_host)])
        url = str(driver.current_url or "")
        if not _trusted(url, target_host):
            if url in {"", "about:blank"}:
                time.sleep(.2)
                continue
            raise LazadaLoginError("Lazada 跳转到未知站点或其他国家，已停止登录")
        auth = is_lazada_login_url(url)
        observed_login = observed_login or auth
        if auth or submitted_here or (observed_login and not returned_to_target):
            _feedback(driver, login_state)
        if not auth:
            # A home page (including GSP) is not the requested finance page.
            if observed_login and not returned_to_target:
                driver.get(target_url)
                returned_to_target = True
                baseline = _tabs(driver)
                last_was_login = False
                continue
            if _matches(url, target_url):
                login_state["business_handle"] = handle
                return handle
            if last_was_login:
                driver.get(target_url)
                baseline = _tabs(driver)
                returned_to_target = True
            last_was_login = False
            time.sleep(.2)
            continue
        last_was_login = True
        if _register(url):
            if not register_clicked:
                links = driver.find_elements("css selector", 'a[href*="/apps/seller/login"]')
                links = [link for link in links if link.is_displayed() and
                         re.fullmatch(r"登录|log\s*in|sign\s*in", link.text.strip(), re.IGNORECASE) and
                         _trusted(urljoin(url, str(link.get_attribute("href") or "")), target_host) and
                         is_lazada_login_url(urljoin(url, str(link.get_attribute("href") or "")))]
                if len(links) != 1:
                    raise LazadaLoginError("Lazada 注册页没有唯一且受信任的登录入口")
                baseline = current_tabs
                register_clicked = True
                links[0].click()
            time.sleep(.2)
            continue
        if not login_state.get("attempted"):
            try:
                language = ensure_lazada_login_chinese(driver, timeout=min(20, max(.1, deadline - time.monotonic())))
            except LazadaLanguageError as exc:
                login_state["blocked"] = str(exc)
                raise LazadaLoginError(str(exc)) from None
            if language and language.get("changed"):
                # The language selection reloads the document. Recheck feedback
                # and the saved form in the next iteration before submitting.
                baseline = _tabs(driver)
                continue
            filled = driver.execute_script(PREFILLED_STATE_SCRIPT, _parts(url).hostname in GSP_HOSTS)
            if not isinstance(filled, dict) or filled.get("filled") is not True:
                # ZiNiao may populate the form just after navigation.
                time.sleep(.2)
                continue
            buttons = driver.find_elements("css selector", "button.login-button, button[type=submit]")
            buttons = [button for button in buttons if button.is_displayed() and button.is_enabled() and
                       re.fullmatch(r"登录|log\s*in|sign\s*in", button.text.strip(), re.IGNORECASE)]
            if len(buttons) != 1:
                raise LazadaLoginError("Lazada 登录按钮不唯一或不可用")
            baseline = current_tabs
            login_state["attempted"] = True
            submitted_here = True
            try:
                buttons[0].click()
            except Exception as exc:
                login_state["blocked"] = "Lazada 登录提交结果不确定，已停止以避免重复提交"
                raise LazadaLoginError(login_state["blocked"]) from exc
        time.sleep(.2)
    if observed_login:
        login_state["blocked"] = "Lazada 登录未完成，请检查已保存登录信息或人工验证"
        raise LazadaLoginError(login_state["blocked"])
    raise LazadaLoginError("Lazada 未进入指定资金页面，不能只凭站点域名确认成功")
