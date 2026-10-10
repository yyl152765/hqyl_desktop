"""Lazada 后台收支数据的页面动作（进站登录、收入详情、日期区间、指标与截图）。

只保留浏览器页面操作。进站与登录对齐原脚本（含「泰国lzd违规下架处理」）经过生产验证的规则：

本土店与跨境店的区别只在「进站主机 + 登录入口」，其余流程完全一致：
- 进站主机：会话已在本人站点（本地站或跨境站）就沿用；会话在 GSP 或店铺名含「跨境」用跨境站；
  其余情况用本地站。绝不会把本地店带到跨境站，也不会反过来落到注册页。
- 本土店登录：本地站自己的 `/app/seller/login` 登录卡片（紫鸟预填），落在「注册 Lazada 卖家」页时
  先点登录入口，点不到再回到该主机标准登录地址；不自动输入凭据、不处理验证码。
- 跨境店登录：先过 `gsp.lazada-seller.cn/page/login`（GSP 共享登录）提交预填凭据，登录成功后
  可能停在 GSP 落地面，需要再进一次目标国家的账单地址才到卖家中心。

三次进站机会、每次只提交一次、多候选表单/验证码/站点异常一律停下并给出明确原因。

账号、钉钉写入、运行器生命周期与调度属于 lazada_bill_detail，本模块不导入旧项目。
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import time
import unicodedata
from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

from backend.services.lazada_ads_page import (
    LoginRequiredError,
    WorkflowError,
    dismiss_lazada_popups,
    goto_dom_ready,
)
from backend.services.lazada_monthly_report import safe_store_filename

_ACTIVE_LOGGER: ContextVar[logging.Logger] = ContextVar(
    "lazada_bill_logger", default=logging.getLogger(__name__)
)


class _TaskLogger:
    def info(self, message: str) -> None:
        _ACTIVE_LOGGER.get().info(message)

    def warning(self, message: str) -> None:
        _ACTIVE_LOGGER.get().warning(message)


LOGGER = _TaskLogger()


class BillLoginRequired(LoginRequiredError):
    """需要人工登录：本店本次不再重试，也不保存可能含凭据的认证页截图。"""

    status = "login_required"


class VerificationRequiredError(BillLoginRequired):
    """出现验证码/短信 OTP：必须人工处理。"""

    status = "verification_required"


class BrowserSessionUnavailable(WorkflowError):
    """紫鸟会话/页面已断开：本店不能再重试。"""


def browser_connection_failed(exc: BaseException) -> bool:
    """页面/浏览器被关闭的连接类错误：重试没有意义。"""
    return isinstance(exc, BrowserSessionUnavailable) or any(
        marker in str(exc).casefold()
        for marker in (
            "connection closed while reading from the driver",
            "target page, context or browser has been closed",
            "browser has been closed",
            "target closed",
        )
    )


def login_status(exc: BaseException) -> str:
    """登录阻断状态（login_required / verification_required）；非登录类问题返回空串。"""
    if not isinstance(exc, LoginRequiredError):
        return ""
    return str(getattr(exc, "status", "") or "") or "login_required"


def is_login_blocked(exc: BaseException) -> bool:
    return isinstance(exc, LoginRequiredError)


LOGIN_ENTRY_TIMEOUT = 10.0
# 预填是「轮询」等待：紫鸟正常 1～3 秒就填好了，这个值只在「一直填不上」时才被吃满，
# 所以不能给太大（原脚本用 8 秒，实测足够）。重试标准登录地址后只再给 8 秒，避免干等 30 秒。
LOGIN_PREFILL_TIMEOUT = 8.0
# 「注册 Lazada 卖家」页上的登录入口通常立刻可点；点不到就快速回退标准登录地址，
# 不再像原脚本那样先干等满 10 秒。
REGISTER_ENTRY_TIMEOUT = 5.0
# 注册页处理最多两轮：①点页面上的登录入口；②点不到或被弹回时改走标准登录地址再点一次。
REGISTER_ENTRY_ROUNDS = 2
LOGIN_SUBMIT_TIMEOUT = 20.0
INCOME_ENTRY_TIMEOUT = 20.0
NAVIGATION_TIMEOUT_MS = 180_000
LOGIN_NAMES = ("登录", "Login", "Log in", "Đăng nhập", "เข้าสู่ระบบ", "Masuk")
INCOME_DETAIL_NAMES = (
    "收入详情",
    "Income Details",
    "Chi tiết Thu nhập",
    "Chi tiết thu nhập",
)
CHINESE_MARKERS = ("我的收入", "收入概览", "收入详情")
LANGUAGE_CONTROLS = (
    "Tiếng Việt",
    "English",
    "ภาษาไทย",
    "Bahasa Indonesia",
    "Bahasa Melayu",
)
CONFIRM_LABELS = ("确定", "确认", "OK", "Xác nhận", "Confirm")

TOTAL_AMOUNT_LABELS = ("总金额", "Total Amount", "Tổng số dư", "Tổng số tiền", "Tổng tiền")
REVENUE_LABELS = ("收入", "Revenue", "Doanh thu")
DEDUCTIONS_LABELS = (
    "扣减项",
    "Deductions",
    "Khoản giảm trừ",
    "Các khoản khấu trừ",
    "Khoản khấu trừ",
)


def _safe_url(value: Any) -> str:
    """Keep host and path only: queries can carry seller identifiers."""
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return "<invalid-url>"
    if not parsed.hostname:
        return "<invalid-url>"
    return f"{parsed.scheme}://{parsed.hostname}{parsed.path}"


def _safe_error_detail(value: Any) -> str:
    text = re.sub(r"(https?://[^\s?\"']+)\?[^\s\"']+", r"\1", str(value))
    return re.sub(
        r"(?i)([\"']?(?:password|access_token|app_secret|authorization|cookie)[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^&\s,;}]+)",
        r"\1[REDACTED]",
        text,
    )


def _first_visible(locator: Any, reverse: bool = False) -> Any | None:
    try:
        count = locator.count()
    except Exception:
        return None
    indexes = range(count - 1, -1, -1) if reverse else range(count)
    for index in indexes:
        item = locator.nth(index)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def click_first_text(page: Any, names: Iterable[str], timeout: int = 5_000) -> str | None:
    for name in names:
        for role in ("button", "tab", "link"):
            item = _first_visible(page.get_by_role(role, name=name, exact=True))
            if item is not None:
                item.click(timeout=timeout)
                return name
        item = _first_visible(page.get_by_text(name, exact=True))
        if item is not None:
            item.click(timeout=timeout)
            return name
    return None


def _click_roots(page: Any) -> list[Any]:
    """主文档 + 各子框架：本地站的登录/注册卡片常嵌在 iframe 里，只查主文档会点不到。"""
    roots = [page]
    try:
        main = page.main_frame
        roots.extend(frame for frame in page.frames if frame is not main)
    except Exception:
        pass
    return roots


def click_login_entry(page: Any, names: Iterable[str], timeout: int = 5_000) -> str | None:
    """点「登录」入口：先精确匹配（主文档 + 子框架），再退化为链接/按钮的「包含」匹配。

    Lazada 本地站的注册卡片常写成「已有账号？点击这里登录」，入口既可能在 iframe 里，
    也常常不是「文本恰好等于登录」的元素——只按精确文本找会点不到，白等到超时。
    """
    roots = _click_roots(page)
    for root in roots:
        found = click_first_text(root, names, timeout=timeout)
        if found:
            return found
    for root in roots:
        for name in names:
            for role in ("link", "button"):
                try:
                    item = _first_visible(root.get_by_role(role, name=name))
                except Exception:
                    continue
                if item is not None:
                    item.click(timeout=timeout)
                    return name
    return None


# ---------------------------------------------------------------------------
# 站点判定
# ---------------------------------------------------------------------------

def _host_of(url: Any) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").casefold()
    except ValueError:
        return ""


def seller_hosts(profile: Any) -> set[str]:
    """该国家允许的站点主机：本地站 + 跨境站。"""
    return {str(profile.local_host).casefold(), str(profile.cross_border_host).casefold()}


def _is_https_origin(url: Any, hosts: Iterable[str]) -> bool:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() in {str(item).casefold() for item in hosts}
        and parsed.username is None
        and parsed.password is None
        and parsed.port in (None, 443)
    )


def _seller_origin(url: Any, profile: Any) -> bool:
    return _is_https_origin(url, seller_hosts(profile))


def _gsp_origin(url: Any) -> bool:
    return _is_https_origin(url, ("gsp.lazada-seller.cn", "gsp.lazada.com"))


def _gsp_login(url: Any) -> bool:
    """GSP 密码登录页：只有 .cn 的 /page/login 是允许提交密码的面。"""
    return (
        _gsp_origin(url)
        and _host_of(url) == "gsp.lazada-seller.cn"
        and urlsplit(str(url or "")).path.rstrip("/").casefold() == "/page/login"
    )


def _auth_route(url: Any) -> str | None:
    path = urlsplit(str(url or "")).path.casefold()
    if re.search(r"/(?:register|signup)(?:/|$)", path):
        return "register"
    if re.search(r"/(?:login|signin|sign-in)(?:/|$)", path):
        return "login"
    return None


def safe_page_url(value: Any) -> str:
    """日志用：只保留主机与路径（查询串可能带店铺标识）。"""
    return _safe_url(value)


def is_auth_page(url: Any) -> bool:
    """登录/注册页：这类页面的截图可能带预填账号或已展开的密码，不应保存。"""
    return bool(_auth_route(url))


def _login_page_allowed(url: Any, profile: Any) -> bool:
    if _seller_origin(url, profile):
        return True
    # GSP 只是共享登录入口，不是国家账单站点；其它 GSP 登录/注册路由不允许提交。
    return _gsp_origin(url) and (_gsp_login(url) or not _auth_route(url))


def _finance_url_for_session(url: Any, profile: Any, shop_name: str = "") -> str:
    """按会话实际所在站点拼接收入页地址；绝不把登录路由或参数带进目标页。

    主机优先级：会话已有的目标国家站点 > GSP 跨境入口 > 按店铺名判断（含「跨境」用跨境站，
    否则用本地站，与广告页同一规则），避免把本地店铺带到跨境站、或反过来落到注册页。
    """
    configured = urlsplit(profile.finance_url)
    if _seller_origin(url, profile):
        host = _host_of(url)
    elif _gsp_origin(url):
        # GSP 只服务跨境流程：会话停在 GSP 时用该国跨境站，绝不落到本地站。
        host = str(profile.cross_border_host)
    elif "跨境" in str(shop_name or ""):
        host = str(profile.cross_border_host)
    else:
        host = str(profile.local_host)
    return configured._replace(scheme="https", netloc=host).geturl()


def canonical_login_url(profile: Any, host: str) -> str:
    """本地站的注册页没有可直接提交的登录卡片时，回到该主机的标准密码登录页。

    只允许国家站点主机，并带上回到账单页的 redirect_uri；不复制任何登录页参数。
    """
    target = urlsplit(profile.finance_url)
    redirect = target._replace(scheme="https", netloc=str(host)).geturl()
    return (
        f"https://{host}/app/seller/login?login=1"
        f"&redirect_uri={quote(redirect, safe='')}"
    )


def _site_page(page: Any, profile: Any) -> Any:
    for candidate in reversed(list(page.context.pages)):
        if candidate.is_closed():
            continue
        candidate_url = str(candidate.url or "")
        owned_gsp_landing = (
            _gsp_origin(candidate_url)
            and not _auth_route(candidate_url)
            and candidate is not page
            and candidate.opener() is page
        )
        if (
            not _seller_origin(candidate_url, profile)
            and not _gsp_login(candidate_url)
            and not owned_gsp_landing
        ):
            continue
        # 本地站与跨境站登录不应借用其它来源的旧标签页：只接受启动页自己开出的页面。
        if (
            candidate is not page
            and not str(page.url or "").startswith("chrome-extension://")
            and candidate.opener() is not page
        ):
            continue
        return candidate
    return page


def activate_lazada_from_launcher(page: Any, shop_name: str, profile: Any) -> Any:
    """紫鸟启动页需要先点一次店铺入口，才能真正进入该店铺自己的站点。"""
    if str(page.url or "").startswith("chrome-extension://"):
        clicked = click_first_text(page, LOGIN_NAMES, timeout=5_000)
        if clicked:
            LOGGER.info(f"{shop_name} 已点击紫鸟启动页的 {clicked} 按钮")
            deadline = time.monotonic() + LOGIN_ENTRY_TIMEOUT
            while time.monotonic() < deadline:
                candidate = _site_page(page, profile)
                if _seller_origin(candidate.url, profile) or _gsp_login(candidate.url):
                    return candidate
                page.wait_for_timeout(200)
    return _site_page(page, profile)


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------

def ensure_lazada_session(page: Any, allow_auth: bool = False) -> None:
    if page.is_closed():
        raise BrowserSessionUnavailable("页面或浏览器已关闭")
    verification = _first_visible(
        page.locator(
            ".nc-container, .nc_wrapper, iframe[src*='captcha'], "
            "input[autocomplete='one-time-code'], input[placeholder*='验证码'], "
            "input[placeholder*='OTP' i]"
        )
    )
    if verification is not None:
        raise VerificationRequiredError("出现验证码或短信验证码，需人工处理后再运行")
    if not allow_auth and _auth_route(page.url):
        raise BillLoginRequired(f"登录未完成，当前停留在登录页：{_safe_url(page.url)}")


def _login_dom_boolean(page: Any, locator: Any, expression: str, arg: Any = None) -> bool:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        # 凭据探测绝不能继承页面默认的 30 秒等待。
        return bool(locator.evaluate(expression, arg, timeout=250))
    except Exception as exc:
        # DOM 在导航中消失不是错误；其它异常按原样抛出，由外层站点校验给出明确原因。
        if isinstance(exc, PlaywrightTimeoutError) or any(
            marker in str(exc).casefold()
            for marker in (
                "execution context was destroyed",
                "cannot find context",
                "element is not attached",
                "unable to adopt element handle",
            )
        ):
            return False
        raise


def _login_element_available(page: Any, element: Any) -> bool:
    return element.is_visible() and _login_dom_boolean(
        page,
        element,
        "element => !element.matches(':disabled') && !element.closest(\"[aria-disabled='true'], [inert]\")",
    )


def _login_scope_owns(page: Any, element: Any, scope_handle: Any) -> bool:
    """包含不等于归属：绝不借用其它表单或嵌套登录卡片的字段与按钮。"""
    return _login_dom_boolean(
        page,
        element,
        """(element, scope) => {
        if (!scope || !scope.contains(element)) return false;
        if (scope.tagName === 'FORM') {
            if ('form' in element || element.hasAttribute('form'))
                return element.form === scope;
            return element.closest('form') === scope;
        }
        if (element.form || element.hasAttribute('form') || element.closest('form'))
            return false;
        const account = "input[type='email'], input[autocomplete='username'], input[type='text'], input:not([type])";
        for (let ancestor = element.parentElement; ancestor && ancestor !== scope;
             ancestor = ancestor.parentElement) {
            if (ancestor.tagName === 'FORM' || (ancestor.querySelector(account) &&
                ancestor.querySelector("input[type='password']"))) return false;
        }
        return true;
    }""",
        scope_handle,
    )


def _login_field_scope(page: Any) -> Any | None:
    """定位唯一的凭据容器：密码框已填、账号框已填、可交互密码框唯一。"""
    passwords = page.locator("input[type='password']")
    password_total = passwords.count()
    visible = [passwords.nth(index) for index in range(password_total) if passwords.nth(index).is_visible()]
    interactive = [password for password in visible if _login_element_available(page, password)]
    candidates = []
    scope_handles = []
    owned_scope_handles = []
    try:
        for password in interactive:
            if not _login_dom_boolean(page, password, "element => Boolean(element.value)"):
                continue
            scope = password.locator("xpath=ancestor::form[1]")
            if not scope.count():
                # 非 form 的登录卡片用「最近的含账号框的父容器」，而不是页头链接或兄弟账号框。
                scope = password.locator(
                    "xpath=ancestor::*[descendant::input[@type='email' or "
                    "@autocomplete='username' or @type='text' or not(@type)]][1]"
                )
                if not scope.count():
                    continue
            if any(
                _login_dom_boolean(page, scope, "(element, other) => element === other", handle)
                for handle in owned_scope_handles
            ):
                continue
            handle = scope.element_handle(timeout=250)
            if handle is None:
                continue
            scope_handles.append(handle)
            if not _login_scope_owns(page, password, handle):
                continue
            owned_scope_handles.append(handle)
            accounts = scope.locator(
                "input[type='email'], input[autocomplete='username'], input[type='text'], input:not([type])"
            )
            if not any(
                _login_scope_owns(page, accounts.nth(index), handle)
                and _login_element_available(page, accounts.nth(index))
                and _login_dom_boolean(page, accounts.nth(index), "element => Boolean(element.value.trim())")
                for index in range(accounts.count())
            ):
                continue
            scoped_passwords = scope.locator("input[type='password']")
            interactive_count = sum(
                _login_scope_owns(page, scoped_passwords.nth(index), handle)
                and _login_element_available(page, scoped_passwords.nth(index))
                for index in range(scoped_passwords.count())
            )
            candidates.append((scope, interactive_count, _login_controls_in_scope(page, scope)))
        submittable = [candidate for candidate in candidates if candidate[2]]
        selected = submittable or candidates
        if len(selected) > 1 or any(candidate[1] > 1 for candidate in selected):
            raise BillLoginRequired(
                "密码登录页存在多个可交互密码框或候选登录表单，无法唯一确认"
                f"（password_total={password_total}; visible={len(visible)}; "
                f"interactive={len(interactive)}; candidate_forms={len(candidates)}），已跳过"
            )
        return selected[0][0] if selected else None
    finally:
        for handle in scope_handles:
            handle.dispose()


def _filled_login_fields(page: Any) -> bool:
    return _login_field_scope(page) is not None


def _login_controls_in_scope(page: Any, scope: Any) -> list[Any]:
    selector = "button, a, input[type='submit'], input[type='button'], [role='button'], [role='link']"
    controls = scope.locator(selector)
    enabled: list[Any] = []
    visible_matches = 0
    handle = scope.element_handle(timeout=250)
    if handle is None:
        return enabled
    try:
        for index in range(controls.count()):
            control = controls.nth(index)
            if (
                not _login_scope_owns(page, control, handle)
                or not control.is_visible()
                or not _login_dom_boolean(
                    page,
                    control,
                    """(element, names) => names.includes((element.getAttribute('aria-label') ||
                        (element.tagName === 'INPUT' ? element.value : element.textContent)).trim())""",
                    list(LOGIN_NAMES),
                )
            ):
                continue
            visible_matches += 1
            if _login_element_available(page, control):
                enabled.append(control)
        if not visible_matches:
            for name in LOGIN_NAMES:
                matches = scope.get_by_text(name, exact=True)
                for index in range(matches.count()):
                    control = matches.nth(index)
                    # 禁用语义按钮里的文字不是第二条提交路径；精确名称也排除了 OTP/扫码切换。
                    if (
                        _login_scope_owns(page, control, handle)
                        and control.is_visible()
                        and _login_dom_boolean(
                            page,
                            control,
                            "element => !element.closest(\"button, a, input, [role='button'], "
                            "[role='link'], [aria-disabled='true'], [inert]\")",
                        )
                    ):
                        enabled.append(control)
    finally:
        handle.dispose()
    return enabled


def _login_submit_control(page: Any) -> Any | None:
    scope = _login_field_scope(page)
    if scope is None:
        return None
    enabled = _login_controls_in_scope(page, scope)
    if len(enabled) > 1:
        raise BillLoginRequired("密码登录页存在多个可用登录控件，无法唯一确认，已跳过")
    return enabled[0] if enabled else None


def _login_redirect_error(url: Any, phase: str) -> LoginRequiredError:
    # 只记录分类，不记录原始 URL：SSO 过程中的路径/参数可能带凭据或店铺标识。
    scheme, host, port, auth, reason = "other", "unavailable", "unknown", "invalid", "invalid_url"
    try:
        parsed = urlsplit(str(url or ""))
        scheme = parsed.scheme if parsed.scheme in {"https", "http", "about", "chrome-extension"} else "other"
        hostname = parsed.hostname or "unavailable"
        host = hostname if re.fullmatch(r"[a-z0-9.:-]{1,253}", hostname) else "unavailable"
        port = str(parsed.port) if parsed.port is not None else "default"
        auth = _auth_route(url) or "non-auth"
        reason = (
            "unsupported_gsp_auth"
            if _gsp_origin(url) and auth != "non-auth" and not _gsp_login(url)
            else "unapproved_origin"
        )
    except ValueError:
        pass
    return BillLoginRequired(
        f"登录跳转至其他站点，需人工核对（phase={phase}; scheme={scheme}; "
        f"host={host}; port={port}; auth={auth}; reason={reason}）"
    )


def _checked_login_page(page: Any, profile: Any) -> Any:
    ensure_lazada_session(page, allow_auth=True)
    current_url = page.url
    if not _login_page_allowed(current_url, profile):
        raise _login_redirect_error(current_url, "current")
    if not _auth_route(current_url):
        return page
    page = _site_page(page, profile)
    ensure_lazada_session(page, allow_auth=True)
    candidate_url = page.url
    if not _login_page_allowed(candidate_url, profile):
        raise _login_redirect_error(candidate_url, "candidate")
    return page


def _login_credentials_rejected(page: Any) -> bool:
    # 只返回布尔值，不回显页面文本（可能包含账号等个人信息）。
    rejection = re.compile(
        r"(?:账号|账户|用户名|邮箱)(?:或密码)(?:错误|不正确)|密码(?:错误|不正确)|"
        r"(?:incorrect|invalid|wrong)\s+(?:email\s+or\s+|username\s+or\s+)?password|"
        r"(?:account|username|email)\s+or\s+password\s+is\s+incorrect",
        re.I,
    )
    return _first_visible(page.get_by_text(rejection)) is not None


def _canonical_host(page: Any, profile: Any) -> str:
    return _host_of(page.url) if _seller_origin(page.url, profile) else str(profile.local_host)


def _goto_canonical_login(page: Any, shop_name: str, profile: Any, reason: str) -> Any:
    """改走该主机的标准登录地址（会触发紫鸟自动填表），并返回落地后的页面。"""
    login_url = canonical_login_url(profile, _canonical_host(page, profile))
    LOGGER.info(f"{shop_name} {reason}，改用标准登录地址：{_safe_url(login_url)}")
    goto_dom_ready(page, login_url, NAVIGATION_TIMEOUT_MS)
    return _checked_login_page(page, profile)


def _enter_login_from_register(page: Any, shop_name: str, profile: Any) -> Any:
    """在「注册 Lazada 卖家」页点登录入口回到密码登录页；点不到就原样返回。"""
    deadline = time.monotonic() + REGISTER_ENTRY_TIMEOUT
    clicked = None
    while True:
        clicked = click_login_entry(page, LOGIN_NAMES, timeout=5_000)
        if clicked:
            break
        ensure_lazada_session(page, allow_auth=True)
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(200)
    if not clicked:
        LOGGER.warning(f"{shop_name} 注册页未找到可点击的登录入口")
        return page
    LOGGER.info(f"{shop_name} 已点击注册页登录入口（{clicked}），等待密码登录页")
    deadline = time.monotonic() + REGISTER_ENTRY_TIMEOUT
    while True:
        page = _checked_login_page(page, profile)
        if _auth_route(page.url) != "register":
            break
        if time.monotonic() >= deadline:
            LOGGER.warning(f"{shop_name} 注册页登录入口未完成跳转")
            break
        page.wait_for_timeout(200)
    return page


def complete_lazada_login(
    page: Any,
    shop_name: str,
    profile: Any,
    login_state: dict[str, Any] | None = None,
) -> Any:
    """每次采集尝试只提交一次浏览器预填的账号密码；绝不处理验证码/OTP。"""
    if not _auth_route(page.url):
        return page
    state = login_state if login_state is not None else {}
    try:
        ensure_lazada_session(page, allow_auth=True)
        if state.get("submitted"):
            raise BillLoginRequired("本次采集尝试已提交过密码登录，不重复提交，已跳过")
        entry_url = page.url
        if not _login_page_allowed(entry_url, profile):
            raise _login_redirect_error(entry_url, "entry")

        # 本地站常先被导到「注册 Lazada 卖家」页：先点页面上的登录入口；点不到、或被标准登录
        # 地址再次弹回注册页，就再点一次（最多两轮），绝不在注册页上干等到超时。
        for _round in range(REGISTER_ENTRY_ROUNDS):
            if _auth_route(page.url) != "register":
                break
            page = _enter_login_from_register(page, shop_name, profile)
            if _auth_route(page.url) != "register":
                break
            page = _goto_canonical_login(page, shop_name, profile, "注册页未点到登录入口")

        if not _auth_route(page.url):
            return page
        deadline = time.monotonic() + LOGIN_PREFILL_TIMEOUT
        while True:
            page = _checked_login_page(page, profile)
            if not _auth_route(page.url):
                return page
            if _auth_route(page.url) == "register":
                # 标准登录地址也可能被弹回注册页：这里再点一次登录入口，且只允许一次。
                if state.get("register_entry_retried"):
                    raise BillLoginRequired("本地站注册页面未提供可用的登录入口，需人工登录")
                state["register_entry_retried"] = True
                page = _enter_login_from_register(page, shop_name, profile)
                if _auth_route(page.url) != "register":
                    continue
                page = _goto_canonical_login(
                    page, shop_name, profile, "注册页未点到登录入口"
                )
                if _auth_route(page.url) != "register":
                    continue
                raise BillLoginRequired("本地站注册页面未提供可用的登录入口，需人工登录")
            if _filled_login_fields(page):
                break
            page = _checked_login_page(page, profile)
            if not _auth_route(page.url):
                return page
            if time.monotonic() >= deadline:
                if state.get("entry_retried"):
                    raise BillLoginRequired("密码登录页账号或密码未填好，已跳过；不自动填写凭据")
                # 紫鸟自动填表常只在标准登录地址触发：只重试一次标准登录页，绝不自己填凭据。
                state["entry_retried"] = True
                page = _goto_canonical_login(page, shop_name, profile, "登录页未预填")
                deadline = time.monotonic() + LOGIN_PREFILL_TIMEOUT
                continue
            page.wait_for_timeout(200)
        deadline = time.monotonic() + LOGIN_ENTRY_TIMEOUT
        while True:
            page = _checked_login_page(page, profile)
            if not _auth_route(page.url):
                return page
            submit = _login_submit_control(page)
            if submit is not None:
                break
            page = _checked_login_page(page, profile)
            if not _auth_route(page.url):
                return page
            if time.monotonic() >= deadline:
                raise BillLoginRequired("密码登录页未找到登录按钮或控件未启用，已跳过")
            page.wait_for_timeout(200)
        # 保留元素本身：Locator 会在点击前重新解析，可能落到另一个国家的表单。
        submit_element = submit.element_handle(timeout=250)
        page = _checked_login_page(page, profile)
        if not _auth_route(page.url):
            return page
        if submit_element is None:
            raise BillLoginRequired("登录控件已消失，不再次提交，已跳过")
        # 一次采集尝试内不重复点击：点击超时也可能已经提交成功。
        state["submitted"] = True
        submit_element.click(timeout=5_000)
        LOGGER.info(f"{shop_name} 已提交浏览器预填的密码登录（本次采集尝试）")
        deadline = time.monotonic() + LOGIN_SUBMIT_TIMEOUT
        while True:
            page = _checked_login_page(page, profile)
            if not _auth_route(page.url):
                return page
            if _login_credentials_rejected(page):
                raise BillLoginRequired("账号或密码被拒绝，已提交一次预填登录，需人工处理")
            if time.monotonic() >= deadline:
                raise BillLoginRequired("登录跳转等待超时，未确认账号密码错误，需人工登录")
            page.wait_for_timeout(200)
    except (LoginRequiredError, BrowserSessionUnavailable):
        raise
    except Exception as exc:
        if browser_connection_failed(exc):
            raise BrowserSessionUnavailable(_safe_error_detail(exc)) from exc
        page = _checked_login_page(page, profile)
        if not _auth_route(page.url):
            return page
        raise BillLoginRequired(f"登录步骤未完成，不再次提交：{_safe_error_detail(exc)}") from exc


def ensure_preferred_language(page: Any, shop_name: str, language: str) -> bool:
    if not language:
        return False
    if any(_first_visible(page.get_by_text(name, exact=True)) for name in CHINESE_MARKERS):
        return True
    current = None
    for name in LANGUAGE_CONTROLS:
        if current is not None:
            break
        current = _first_visible(page.get_by_text(name, exact=True), reverse=True)
    if current is None:
        LOGGER.warning(f"{shop_name} 未找到语言入口，继续使用页面现有语言")
        return False
    try:
        current.click(timeout=5_000)
        page.wait_for_timeout(500)
        target = _first_visible(page.get_by_text(language, exact=True), reverse=True)
        if target is None:
            LOGGER.warning(f"{shop_name} 语言菜单中没有 {language}，继续使用页面现有语言")
            return False
        target.click(timeout=5_000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(5_000)
        LOGGER.info(f"{shop_name} 已切换为{language}")
        return True
    except Exception as exc:
        LOGGER.warning(f"{shop_name} 切换{language}失败，继续使用现有语言：{_safe_error_detail(exc)}")
        return False


def open_income_details(
    page: Any,
    shop_name: str,
    profile: Any,
    login_state: dict[str, Any] | None = None,
    *,
    navigation_timeout_ms: int = NAVIGATION_TIMEOUT_MS,
) -> Any:
    """进入目标国家「我的收入」并打开收入详情。

    与参考脚本一致：点完紫鸟启动页后先无条件补一次登录（不在登录页时是空操作），
    再进账单地址；最多两次进站，因为跨境店登录成功后可能先停在 GSP 落地面而不是卖家中心。
    """
    state = login_state if login_state is not None else {}
    page = activate_lazada_from_launcher(page, shop_name, profile)
    # 本土店会话可能直接落在本地站登录页、跨境店落在 GSP 密码登录页：先提交一次再进站。
    page = complete_lazada_login(page, shop_name, profile, state)
    ensure_lazada_session(page)

    finance_path = urlsplit(str(profile.finance_url)).path
    for attempt in range(1, 3):
        target = _finance_url_for_session(page.url, profile, shop_name)
        LOGGER.info(f"{shop_name} 打开收入页（第 {attempt}/2 次）：{_safe_url(target)}")
        goto_dom_ready(page, target, navigation_timeout_ms)
        page = complete_lazada_login(page, shop_name, profile, state)
        page = _checked_login_page(page, profile)
        ensure_lazada_session(page)
        if _gsp_origin(page.url) and not _auth_route(page.url):
            # 跨境店刚过 GSP，还没落到卖家中心：再进一次目标地址。
            continue
        if _seller_origin(page.url, profile) and urlsplit(str(page.url)).path == finance_path:
            break
        if attempt == 2:
            raise BillLoginRequired("登录后未进入目标国家账单站点，需人工核对")
    if not _seller_origin(page.url, profile):
        raise BillLoginRequired("登录后未进入目标国家账单站点，需人工核对")
    LOGGER.info(f"{shop_name} 已进入账单站点：{_safe_url(page.url)}")
    dismiss_lazada_popups(page)
    if profile.preferred_language:
        ensure_preferred_language(page, shop_name, profile.preferred_language)
        dismiss_lazada_popups(page)

    deadline = time.monotonic() + INCOME_ENTRY_TIMEOUT
    while True:
        ensure_lazada_session(page)
        clicked = click_first_text(page, INCOME_DETAIL_NAMES, timeout=5_000)
        if clicked:
            LOGGER.info(f"{shop_name} 已打开 {clicked}")
            break
        if time.monotonic() >= deadline:
            raise WorkflowError(f"未找到收入详情入口，当前地址：{_safe_url(page.url)}")
        page.wait_for_timeout(250)
    page.wait_for_timeout(1_500)
    ensure_lazada_session(page)
    return page


# ---------------------------------------------------------------------------
# 日期区间
# ---------------------------------------------------------------------------

def _visible_date_inputs(container: Any) -> list[Any]:
    date_inputs: list[Any] = []
    inputs = container.locator("input")
    try:
        count = inputs.count()
    except Exception:
        return date_inputs
    for index in range(count):
        item = inputs.nth(index)
        try:
            if item.is_visible() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", item.input_value().strip()):
                date_inputs.append(item)
        except Exception:
            continue
    return date_inputs


def _opened_date_overlay(page: Any) -> Any | None:
    overlays = page.locator(".next-overlay-wrapper.opened")
    try:
        count = overlays.count()
    except Exception:
        return None
    for index in range(count - 1, -1, -1):
        overlay = overlays.nth(index)
        try:
            if len(_visible_date_inputs(overlay)) >= 1:
                return overlay
        except Exception:
            continue
    return None


def _date_boundary_input(page: Any, boundary: str) -> Any | None:
    placeholders = {
        "start": ("起始日期", "开始日期", "Start date", "Start Date", "Ngày bắt đầu"),
        "end": ("结束日期", "终止日期", "End date", "End Date", "Ngày kết thúc"),
    }[boundary]
    for placeholder in placeholders:
        item = _first_visible(page.locator(f"input[placeholder='{placeholder}']"))
        if item is not None:
            return item
    return None


def _sorted_overlay_date_inputs(overlay: Any) -> list[Any]:
    positioned: list[tuple[float, float, Any]] = []
    for item in _visible_date_inputs(overlay):
        try:
            box = item.bounding_box()
            if box is not None:
                positioned.append((box["x"], box["y"], item))
        except Exception:
            continue
    positioned.sort(key=lambda entry: (entry[0], entry[1]))
    return [entry[2] for entry in positioned]


def _fill_date_input(page: Any, item: Any, target_date: date) -> bool:
    value = target_date.isoformat()
    try:
        item.click(timeout=5_000, force=True)
        item.press("Control+A", timeout=3_000)
        item.type(value, delay=30, timeout=5_000)
        item.press("Enter", timeout=3_000)
    except Exception:
        try:
            item.evaluate(
                """(element, value) => {
                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value'
                    ).set;
                    setter.call(element, value);
                    element.dispatchEvent(new Event('input', {bubbles: true}));
                    element.dispatchEvent(new Event('change', {bubbles: true}));
                    element.dispatchEvent(new KeyboardEvent('keydown', {
                        key: 'Enter', code: 'Enter', bubbles: true
                    }));
                    element.dispatchEvent(new KeyboardEvent('keyup', {
                        key: 'Enter', code: 'Enter', bubbles: true
                    }));
                }""",
                value,
            )
        except Exception:
            return False
    page.wait_for_timeout(300)
    try:
        return item.input_value().strip() == value
    except Exception:
        return False


def _click_calendar_day(page: Any, overlay: Any, target_date: date) -> bool:
    iso_date = target_date.isoformat()
    for _ in range(4):
        cells = overlay.locator(f".next-calendar-cell[title='{iso_date}']")
        spillover = None
        try:
            count = cells.count()
        except Exception:
            return False
        for index in range(count):
            cell = cells.nth(index)
            try:
                if not cell.is_visible():
                    continue
                classes = (cell.get_attribute("class") or "").casefold()
                if "disabled" in classes:
                    continue
                if "next-calendar-cell-prev-month" in classes:
                    spillover = "prev"
                    continue
                if "next-calendar-cell-next-month" in classes:
                    spillover = "next"
                    continue
                day = _first_visible(cell.locator(".next-calendar-date")) or cell
                day.click(timeout=5_000, force=True)
                return True
            except Exception:
                continue
        nav_selectors = {
            "prev": (".next-calendar-btn-prev-month", "button[aria-label='Previous month']",
                     "button[aria-label='上个月']"),
            "next": (".next-calendar-btn-next-month", "button[aria-label='Next month']",
                     "button[aria-label='下个月']"),
        }.get(spillover)
        if not nav_selectors:
            return False
        navigation = None
        for selector in nav_selectors:
            navigation = _first_visible(overlay.locator(selector))
            if navigation is not None:
                break
        if navigation is None:
            LOGGER.warning(f"日期 {iso_date} 仅显示为跨月单元格，但未找到翻月按钮")
            return False
        navigation.click(timeout=5_000, force=True)
        page.wait_for_timeout(300)
    return False


def _set_dates_in_overlay(page: Any, trigger: Any, start_date: date, end_date: date) -> bool:
    try:
        trigger.click(timeout=5_000, force=True)
        page.wait_for_timeout(300)
        if _opened_date_overlay(page) is None:
            return False
        # 区间选择器打开后有两个独立边界输入框：左侧取开始，右侧取结束，最后统一确认。
        for input_index, target_date, label in ((0, start_date, "开始"), (1, end_date, "结束")):
            overlay = _opened_date_overlay(page)
            if overlay is None:
                LOGGER.warning(f"选择{label}日期前日期弹层已消失")
                return False
            overlay_inputs = _sorted_overlay_date_inputs(overlay)
            if len(overlay_inputs) < 2:
                LOGGER.warning("日期弹层未找到两个独立输入框")
                return False
            overlay_inputs[input_index].click(timeout=5_000, force=True)
            page.wait_for_timeout(200)
            overlay = _opened_date_overlay(page)
            if overlay is None:
                LOGGER.warning(f"激活{label}日期输入框后日期弹层消失")
                return False
            if not _click_calendar_day(page, overlay, target_date):
                overlay = _opened_date_overlay(page)
                if overlay is None:
                    return False
                overlay_inputs = _sorted_overlay_date_inputs(overlay)
                if len(overlay_inputs) < 2 or not _fill_date_input(
                    page, overlay_inputs[input_index], target_date
                ):
                    LOGGER.warning(f"点选并输入均未能设置{label}日期 {target_date.isoformat()}")
                    return False
            page.wait_for_timeout(300)

        overlay = _opened_date_overlay(page)
        if overlay is None:
            return False
        confirm = None
        for label in CONFIRM_LABELS:
            confirm = _first_visible(overlay.get_by_text(label, exact=True))
            if confirm is not None:
                break
        if confirm is None:
            LOGGER.warning("选择日期区间后未找到确认按钮")
            return False
        confirm.click(timeout=5_000)
        try:
            overlay.wait_for(state="hidden", timeout=5_000)
        except Exception:
            pass
        return True
    except Exception as exc:
        LOGGER.warning(f"日期弹层输入失败，改为直接填写边界输入框：{_safe_error_detail(exc)}")
        return False


def set_date_range(page: Any, start_date: date, end_date: date, shop_name: str) -> None:
    ensure_lazada_session(page)
    start_input = _date_boundary_input(page, "start")
    end_input = _date_boundary_input(page, "end")
    if start_input is None or end_input is None:
        visible = _visible_date_inputs(page)
        if len(visible) < 2:
            raise WorkflowError("账单页未找到独立的起始/结束日期输入框")
        start_input, end_input = visible[0], visible[1]

    if not _set_dates_in_overlay(page, start_input, start_date, end_date):
        if not _fill_date_input(page, start_input, start_date) or not _fill_date_input(
            page, end_input, end_date
        ):
            raise WorkflowError(
                f"无法设置取数区间 {start_date.isoformat()} 至 {end_date.isoformat()}"
            )

    try:
        actual = (start_input.input_value().strip(), end_input.input_value().strip())
    except Exception as exc:
        raise WorkflowError(f"无法回读账单页日期区间：{_safe_error_detail(exc)}") from exc
    expected = (start_date.isoformat(), end_date.isoformat())
    if actual != expected:
        raise WorkflowError(f"日期区间未生效：期望 {expected}，实际 {actual}")
    page.wait_for_timeout(2_000)
    for selector in (".next-loading", ".next-overlay-wrapper", "[class*='loading']"):
        try:
            page.locator(selector).first.wait_for(state="hidden", timeout=5_000)
        except Exception:
            pass
    LOGGER.info(f"{shop_name} 取数范围：{expected[0]} 至 {expected[1]}")


# ---------------------------------------------------------------------------
# 金额
# ---------------------------------------------------------------------------

def clean_amount(value: Any, absolute: bool = False) -> int | float:
    text = unicodedata.normalize("NFKC", str(value if value is not None else "")).strip()
    negative_parentheses = text.startswith("(") and text.endswith(")")
    compact_text = text.replace(" ", "")
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", compact_text)
    if not match:
        raise WorkflowError(f"无法从文本中解析金额：{value!r}")
    try:
        amount = Decimal(match.group(0).replace(",", ""))
    except InvalidOperation as exc:
        raise WorkflowError(f"无效金额：{value!r}") from exc
    if negative_parentheses or "-" in compact_text[: match.start() + 1]:
        amount = -abs(amount)
    if absolute:
        amount = abs(amount)
    if amount == amount.to_integral_value():
        return int(amount)
    return float(amount)


def _amount_from_text(text: str, currency_pattern: str, absolute: bool = False):
    currency_match = re.search(
        rf"[-(]?\s*{currency_pattern}\s*-?\s*\d[\d,]*(?:\.\d+)?\s*\)?",
        text,
        flags=re.IGNORECASE,
    )
    if currency_match:
        return clean_amount(currency_match.group(0), absolute=absolute)
    plain_match = re.search(r"[-(]?\s*\d[\d,]*(?:\.\d+)?\s*\)?", text)
    if plain_match:
        return clean_amount(plain_match.group(0), absolute=absolute)
    return None


def find_metric(page: Any, labels: Iterable[str], currency_pattern: str, absolute: bool = False):
    for label in labels:
        label_locator = page.get_by_text(label, exact=True)
        try:
            count = label_locator.count()
        except Exception:
            continue
        for index in range(count):
            item = label_locator.nth(index)
            try:
                if not item.is_visible():
                    continue
            except Exception:
                continue
            for path in (
                "xpath=following-sibling::*[1]",
                "xpath=../following-sibling::*[1]",
                "xpath=../../following-sibling::*[1]",
            ):
                sibling = _first_visible(item.locator(path))
                if sibling is None:
                    continue
                try:
                    amount = _amount_from_text(
                        sibling.inner_text(), currency_pattern, absolute=absolute
                    )
                except Exception:
                    amount = None
                if amount is not None:
                    return amount
            container = item
            for _ in range(4):
                container = container.locator("xpath=..")
                try:
                    text = container.inner_text(timeout=2_000)
                except Exception:
                    continue
                text = text.replace(label, "", 1).strip()
                amount = _amount_from_text(text, currency_pattern, absolute=absolute)
                if amount is not None:
                    return amount
    raise WorkflowError(f"未找到指标 {tuple(labels)[0]!r} 的金额")


def collect_bill_metrics(page: Any, profile: Any) -> dict[str, Any]:
    return {
        "total_amount": find_metric(page, TOTAL_AMOUNT_LABELS, profile.currency_pattern),
        "revenue": find_metric(page, REVENUE_LABELS, profile.currency_pattern),
        "deductions": find_metric(
            page, DEDUCTIONS_LABELS, profile.currency_pattern, absolute=True
        ),
    }


# ---------------------------------------------------------------------------
# 截图
# ---------------------------------------------------------------------------

def _add_screenshot_shop_header(screenshot_bytes: bytes, shop_name: str):
    """把店铺名渲染在图片上方，避免遮挡账单证据。"""
    from PIL import Image, ImageDraw, ImageFont

    label = "店铺：" + " ".join(str(shop_name).split())
    with Image.open(BytesIO(screenshot_bytes)) as source:
        viewport = source.convert("RGB")
    font_size = max(20, min(36, viewport.width // 40))
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    font = None
    for font_path in (fonts_dir / "msyhbd.ttc", fonts_dir / "msyh.ttc", fonts_dir / "simhei.ttf"):
        try:
            font = ImageFont.truetype(str(font_path), font_size)
            break
        except OSError:
            continue
    if font is None:
        raise WorkflowError("截图店铺名标识缺少中文字体，请安装微软雅黑或黑体")

    margin = font_size
    draw = ImageDraw.Draw(viewport)
    lines: list[str] = []
    line = ""
    for character in label:
        if line and draw.textlength(line + character, font=font) > viewport.width - margin * 2:
            lines.append(line)
            line = ""
        line += character
    lines.append(line)
    line_height = sum(font.getmetrics()) + 6
    header_height = line_height * len(lines) + 28
    result = Image.new("RGB", (viewport.width, viewport.height + header_height), "#c62828")
    result.paste(viewport, (0, header_height))
    draw = ImageDraw.Draw(result)
    for index, text in enumerate(lines):
        draw.text(
            (viewport.width / 2, 14 + index * line_height),
            text,
            font=font,
            fill="white",
            anchor="mt",
        )
    return result


def save_screenshot(page: Any, screenshot_folder: Any, shop_name: str, failed: bool = False) -> Path:
    """截图保存到调用方给定的目录（已按国家与账单区间建好）；失败截图进入 debug 子目录。"""
    target_dir = Path(screenshot_folder) / ("debug" if failed else "")
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_failed_{datetime.now():%H%M%S}" if failed else ""
    target_path = target_dir / f"{safe_store_filename(shop_name)}{suffix}.jpg"
    raw_image = page.screenshot(type="png", full_page=False, timeout=60_000)
    labeled_image = None
    temporary_path = None
    try:
        try:
            labeled_image = _add_screenshot_shop_header(raw_image, shop_name)
        except Exception as exc:
            # Pillow 或中文字体不可用时不阻塞账单采集，仅记录后保存原始截图。
            LOGGER.warning(f"{shop_name} 截图添加店铺名标识失败，改为保存原始截图：{_safe_error_detail(exc)}")
            from PIL import Image

            labeled_image = Image.open(BytesIO(raw_image)).convert("RGB")
        with tempfile.NamedTemporaryFile(dir=target_dir, suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        labeled_image.save(temporary_path, format="JPEG", quality=90)
        os.replace(temporary_path, target_path)
    finally:
        if labeled_image is not None:
            labeled_image.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return target_path


class PlaywrightLazadaBillPageActions:
    """采集单个店铺的账单区间与三项指标；任一指标缺失即整行不写入。

    `last_page` 记录本次采集真正结束在的那一页：紫鸟/Lazada 登录常会**另开标签页**，
    而调用方拿到的初始页面可能还停在注册页，截图必须用 `last_page`。
    """

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        navigation_timeout_ms: int = NAVIGATION_TIMEOUT_MS,
    ):
        self.logger = logger or logging.getLogger(__name__)
        self.navigation_timeout_ms = navigation_timeout_ms
        self.last_page: Any = None

    def collect(
        self,
        page: Any,
        shop_name: str,
        profile: Any,
        start_date: date,
        end_date: date,
        login_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # 每次采集先清空：上一次店铺的页面绝不能当成这一次的截图对象。
        self.last_page = None
        token = _ACTIVE_LOGGER.set(self.logger)
        try:
            page = open_income_details(
                page,
                shop_name,
                profile,
                login_state,
                navigation_timeout_ms=self.navigation_timeout_ms,
            )
            self.last_page = page
            set_date_range(page, start_date, end_date, shop_name)
            # 日期与指标都在同一页上完成，收尾再记录一次，保证与截图对象一致。
            self.last_page = page
            metrics = collect_bill_metrics(page, profile)
            LOGGER.info(
                f"{shop_name} 账单数据：总金额={metrics['total_amount']}，"
                f"收入={metrics['revenue']}，扣减项={metrics['deductions']}"
            )
            return metrics
        finally:
            _ACTIVE_LOGGER.reset(token)
