from __future__ import annotations

import re
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError


class ShopeePasswordVerificationRequired(RuntimeError):
    pass


VERIFY_BUTTON_PATTERN = re.compile(r"Verify|验证|驗證|Xác minh", re.I)
AMS_WELCOME_PATTERN = re.compile(
    r"欢迎使用\s*Shopee\s*联盟营销|Welcome\s+to\s+Shopee.*(?:AMS|Affiliate)|Shopee\s*联盟营销",
    re.I,
)
AMS_START_BUTTON_PATTERN = re.compile(r"开始|Start|Get Started|Bắt đầu", re.I)
AMS_AGREE_PATTERN = re.compile(r"我同意|I agree|Agree|Đồng ý", re.I)
PASSWORD_GONE_SCRIPT = """
() => {
  const inputs = Array.from(document.querySelectorAll('input[type=password]'));
  return inputs.length === 0 || !inputs.some((el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden'
      && style.display !== 'none'
      && rect.width > 0
      && rect.height > 0;
  });
}
"""


def complete_shopee_password_verification(
    page: Page,
    *,
    max_attempts: int = 5,
    wait_after_click_ms: int = 1000,
    appear_timeout_ms: int = 3000,
    appear_poll_interval_ms: int = 200,
) -> bool:
    """Click Shopee's password verification dialog until it disappears.

    Shopee occasionally ignores the first click on the Verify button. The
    password is already handled by the live browser session; this helper only
    repeats the confirmation action and never types or logs credentials.
    """
    password = _wait_for_visible_password(
        page,
        timeout_ms=appear_timeout_ms,
        poll_interval_ms=appear_poll_interval_ms,
    )
    if password is None:
        return False

    attempts = max(int(max_attempts), 1)
    handled = False
    for _attempt in range(attempts):
        password = _visible_password_input(page)
        if password is None:
            return handled
        handled = True
        verify_button = page.get_by_role("button", name=VERIFY_BUTTON_PATTERN)
        if verify_button.count():
            try:
                verify_button.first.click(timeout=5000)
            except PlaywrightError:
                try:
                    verify_button.first.click(timeout=3000, force=True)
                except PlaywrightError:
                    _press_enter_on_password(password)
        else:
            _press_enter_on_password(password)

        try:
            page.wait_for_function(PASSWORD_GONE_SCRIPT, timeout=8000)
            return True
        except PlaywrightTimeoutError:
            page.wait_for_timeout(max(int(wait_after_click_ms), 0))

    if page.locator('input[type="password"]:visible').count():
        raise ShopeePasswordVerificationRequired(
            f"Shopee 登录密码验证未通过，已尝试点击 Verify {attempts} 次；请在紫鸟窗口中手动确认后重试"
        )
    return True


def _wait_for_visible_password(
    page: Page,
    *,
    timeout_ms: int,
    poll_interval_ms: int,
):
    timeout = max(int(timeout_ms), 0)
    poll_interval = max(int(poll_interval_ms), 50)
    deadline = time.monotonic() + timeout / 1000
    while True:
        password = _visible_password_input(page)
        if password is not None:
            return password
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            return None
        page.wait_for_timeout(min(poll_interval, remaining_ms))


def _visible_password_input(page: Page):
    try:
        password = page.locator('input[type="password"]:visible')
        return password if password.count() else None
    except PlaywrightError:
        return None


def dismiss_shopee_ams_welcome(
    page: Page,
    *,
    max_attempts: int = 5,
    appear_timeout_ms: int = 5000,
) -> bool:
    """Dismiss the asynchronously rendered Shopee AMS onboarding modal.

    China Seller Center can render this dialog several seconds after
    ``domcontentloaded``.  Always scope actions to the visible dialog so a
    hidden modal template cannot steal the click.
    """
    handled = False
    for _attempt in range(max(int(max_attempts), 1)):
        dialog = _wait_for_visible_ams_welcome(
            page,
            timeout_ms=appear_timeout_ms if not handled else 1000,
        )
        if dialog is None:
            return handled
        handled = True
        _ensure_ams_terms_checked(dialog)
        button = dialog.get_by_role("button", name=AMS_START_BUTTON_PATTERN).first
        if not button.count():
            button = dialog.locator("button").filter(has_text=AMS_START_BUTTON_PATTERN).first
        if not button.count():
            raise ShopeePasswordVerificationRequired("Shopee AMS 欢迎弹窗存在，但未找到“开始”按钮")
        button.click(timeout=10000, force=True)
        try:
            dialog.wait_for(state="hidden", timeout=10000)
        except PlaywrightTimeoutError:
            page.wait_for_timeout(1000)
            continue
        if _wait_for_visible_ams_welcome(page, timeout_ms=1000) is None:
            return True
    if _visible_ams_welcome_dialog(page) is not None:
        raise ShopeePasswordVerificationRequired("Shopee AMS 欢迎弹窗未关闭，请在紫鸟窗口手动点击开始后重试")
    return True


def _press_enter_on_password(password_locator) -> None:
    try:
        password_locator.last.press("Enter", timeout=3000)
    except PlaywrightError:
        pass


def _wait_for_visible_ams_welcome(page: Page, *, timeout_ms: int):
    deadline = time.monotonic() + max(int(timeout_ms), 0) / 1000
    while True:
        dialog = _visible_ams_welcome_dialog(page)
        if dialog is not None:
            return dialog
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(200)


def _visible_ams_welcome_dialog(page: Page):
    dialog_selectors = (
        '[role="dialog"]',
        ".eds-modal",
        ".eds-dialog",
        ".eds-react-modal",
        ".eds-react-dialog",
    )
    for selector in dialog_selectors:
        dialogs = page.locator(selector).filter(has_text=AMS_WELCOME_PATTERN)
        try:
            for index in range(dialogs.count() - 1, -1, -1):
                candidate = dialogs.nth(index)
                if candidate.is_visible():
                    return candidate
        except Exception:
            continue

    # Some localized builds do not expose dialog semantics. Fall back to the
    # nearest visible ancestor that contains both the welcome copy and button.
    texts = page.get_by_text(AMS_WELCOME_PATTERN)
    try:
        for index in range(texts.count() - 1, -1, -1):
            text = texts.nth(index)
            if not text.is_visible():
                continue
            candidate = text.locator(
                "xpath=ancestor::*[.//button][1]"
            )
            if candidate.count() and candidate.is_visible():
                return candidate
    except Exception:
        pass
    return None


def _ensure_ams_terms_checked(container) -> None:
    checkbox = container.locator('input[type="checkbox"]:visible').first
    try:
        if checkbox.count() and not checkbox.is_checked():
            checkbox.check(timeout=5000, force=True)
            return
    except Exception:
        pass
    agree = container.get_by_text(AMS_AGREE_PATTERN).first
    try:
        if agree.count():
            agree.click(timeout=5000)
    except Exception:
        pass
