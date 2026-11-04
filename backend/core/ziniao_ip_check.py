"""Observe ZiNiao's own IP-check result without restarting or bypassing it."""
from __future__ import annotations

import time
from urllib.parse import urldefrag

from selenium.common.exceptions import StaleElementReferenceException, TimeoutException


SUCCESS_XPATH = '//button[contains(@class, "styles_btn--success")]'


class ZiniaoIPCheckError(ValueError):
    """A safe diagnostic that contains no detection URL or driver response."""


def _same_page(driver, expected_url: str, handle: str) -> bool:
    current_url = driver.current_url
    return (driver.current_window_handle == handle
            and isinstance(current_url, str)
            and urldefrag(current_url).url == expected_url)


def ensure_ziniao_ip_check(driver, detection_url: str, *, timeout: float = 60) -> None:
    """Navigate once and require the visible, enabled success marker on this tab.

    The single deadline includes navigation. A page-load timeout can occur after
    the check page has rendered, so only that exception permits observing the
    original page further. No proxy settings, retry controls or business pages
    are touched; a missing result remains unconfirmed.
    """
    if not isinstance(detection_url, str) or not detection_url.strip():
        raise ZiniaoIPCheckError('紫鸟未返回有效的代理 IP 检测页面，请重新打开该店后重试')
    detection_url = detection_url.strip()
    deadline = time.monotonic() + timeout
    completed = False
    stage = '初始化'
    try:
        driver.implicitly_wait(0)
        handle = driver.current_window_handle
        if not isinstance(handle, str) or not handle:
            raise ZiniaoIPCheckError('无法确认代理 IP 检测页签，请重新打开该店后重试')
        expected_url = urldefrag(detection_url).url
        stage = '页面导航'
        try:
            driver.get(detection_url)
        except TimeoutException:
            # Do not navigate again or stop page scripts: ZiNiao may still be
            # checking or switching its own proxy line on the original page.
            pass
        stage = '结果核验'
        while True:
            if time.monotonic() >= deadline:
                raise ZiniaoIPCheckError('代理 IP 检测等待超时，尚未确认通过；请检查该店代理线路和检测页后重试')
            if not _same_page(driver, expected_url, handle):
                raise ZiniaoIPCheckError('代理 IP 检测页面或页签已变化，尚未确认通过；请检查该店代理线路后重试')
            try:
                visible = [element for element in driver.find_elements('xpath', SUCCESS_XPATH)
                           if element.is_displayed()]
                if len(visible) > 1:
                    raise ZiniaoIPCheckError('代理 IP 检测成功标记不唯一，尚未确认通过；请检查该店检测页后重试')
                ready = len(visible) == 1 and visible[0].is_enabled()
            except StaleElementReferenceException:
                # ZiNiao can replace its status control while switching lines.
                # Observe again under the same deadline and page guards. All
                # other driver errors still fail instead of masking a lost tab.
                ready = False
            if ready:
                if not _same_page(driver, expected_url, handle):
                    raise ZiniaoIPCheckError('代理 IP 检测页面或页签已变化，尚未确认通过；请检查该店代理线路后重试')
                if time.monotonic() < deadline:
                    completed = True
                    return
            time.sleep(min(.25, max(0, deadline - time.monotonic())))
    except ZiniaoIPCheckError:
        raise
    except Exception as error:
        # Driver exception messages often contain authenticated URLs. Report
        # only the phase and class, never the original message or page text.
        raise ZiniaoIPCheckError(
            f'代理 IP 检测{stage}异常（{type(error).__name__}），尚未确认通过；'
            '请检查该店代理线路及紫鸟连接后重试'
        ) from None
    finally:
        try:
            driver.implicitly_wait(0)
        except Exception as error:
            # Preserve the original safe failure if the connection also died
            # during cleanup. A successful result cannot hide a reset failure.
            if completed:
                raise ZiniaoIPCheckError(
                    f'代理 IP 检测连接恢复失败（{type(error).__name__}），请重新打开该店后重试'
                ) from None
