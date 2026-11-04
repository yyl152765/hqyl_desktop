"""Receive one global report through the owned browser's download directory."""
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

from backend.services.temu_fee_refund_controls import click_control
from backend.services.temu_fee_refund_download import (
    MAX_BYTES, _validate_xlsx, download_xlsx as capture_xlsx,
)


def _continue_global_authorization(driver, baseline, handled, expected_seller):
    """Confirm only this download's new, same-store global-centre handoff."""
    if not expected_seller:
        return
    response = driver.execute_cdp_cmd('Target.getTargets', {})
    targets = response.get('targetInfos', []) if isinstance(response, dict) else []
    handles = {handle.removeprefix('CDwindow-'): handle for handle in driver.window_handles}
    for target in targets:
        target_id = target.get('targetId')
        handle = handles.get(target_id)
        if target.get('type') != 'page' or not handle or handle in baseline or target_id in handled:
            continue
        url = urlsplit(target.get('url') or '')
        if url.hostname != 'seller.kuajingmaihuo.com' or url.path != '/link-agent-seller':
            continue
        params = parse_qs(url.query)
        destination = urlsplit((params.get('targetUrl') or [''])[0])
        if params.get('region') != ['1'] or destination.hostname != 'agentseller.temu.com' or destination.path != '/labor/bill-download-with-detail':
            raise ValueError('全球财务下载授权的目标页面不一致')
        if driver.current_window_handle != handle:
            driver.switch_to.window(handle)
        state = driver.execute_script('''return {
          text:document.body.innerText,
          challenge:[...document.querySelectorAll('[role=dialog],iframe[src*="captcha"]')]
            .some(e=>e.getClientRects().length && (e.tagName==='IFRAME'||/验证码|安全验证|验证身份/.test(e.innerText)))
        };''')
        if state.get('challenge'):
            raise ValueError('全球财务下载需要人工验证，请完成后重试')
        match = re.search(r'即将前往\s*Seller\s*Central\s*[（(]全球[）)]\s*的\s*[「【\[]([^」】\]]+)[」】\]]\s*店铺', state.get('text', ''))
        if not match:
            continue  # The new page may still be loading.
        if match.group(1).strip().casefold() != expected_seller.strip().casefold():
            raise ValueError('全球财务下载授权店铺与本次店铺不一致')
        buttons = [e for e in driver.find_elements('xpath', "//button[normalize-space()='确认授权并前往']")
                   if e.is_displayed() and e.is_enabled()]
        labels = [e for e in driver.find_elements('css selector', 'label:has(input[type="checkbox"])')
                  if e.is_displayed() and '账号ID和店铺名称' in e.text]
        # Current TEMU also renders the sharing notice as plain text with an
        # enabled confirmation button; its sole checkbox is 今日不再提醒.
        # That preference is not consent and must not be toggled here.
        notice = '您授权您的账号ID和店铺名称在卖家中心各板块共享'
        plain_notice = notice in re.sub(r'\s+', '', state.get('text', ''))
        if len(buttons) != 1 or len(labels) > 1 or (not labels and not plain_notice):
            raise ValueError('无法唯一确认全球财务下载授权控件')
        if labels:
            checkbox = labels[0].find_element('css selector', 'input[type="checkbox"]')
            if not checkbox.is_selected():
                labels[0].click()
        handled.add(target_id)  # Never submit the handoff twice after an ambiguous result.
        buttons[0].click()


def download_xlsx(browser, identity, selector, raw_dir, *, filename="财务明细_全球.xlsx", timeout=180,
                  expected_seller=None):
    # Native downloads also support platform links hosted on a signed CDN.  No
    # signed URL or browser credential is copied into an application request.
    directory = Path(raw_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if Path(filename).name != filename or Path(filename).suffix.lower() != ".xlsx":
        raise ValueError("下载文件名必须是本目录内的 XLSX 文件名")
    if any(directory.iterdir()):
        raise ValueError("本次下载目录非空，请创建新批次，避免使用旧文件")
    driver_factory = getattr(browser, "_driver", None)
    if not callable(driver_factory):
        return capture_xlsx(browser, identity, selector, directory, filename=filename, timeout=timeout)
    driver = driver_factory(identity)
    original_handle = driver.current_window_handle if expected_seller else None
    baseline = set(driver.window_handles) if expected_seller else set()
    handled = set()
    command = None
    for candidate in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
        try:
            driver.execute_cdp_cmd(candidate, {"behavior": "allow", "downloadPath": str(directory)})
            command = candidate
            break
        except Exception:
            continue
    if command is None:
        # The fallback is chosen before the single download click is dispatched.
        return capture_xlsx(browser, identity, selector, directory, filename=filename, timeout=timeout)
    try:
        click_control(browser, identity, selector)
        deadline = time.monotonic() + timeout
        while True:
            files = [path for path in directory.iterdir() if path.is_file()]
            if any(path.stat().st_size > MAX_BYTES for path in files):
                raise ValueError("导出文件超过 128 MiB 接收限制")
            pending = [p for p in files if p.suffix.lower() in {".crdownload", ".tmp", ".download"}]
            completed = [p for p in files if p not in pending]
            if len(completed) > 1:
                raise ValueError("一次下载产生多个文件，无法确认全球财务明细")
            if completed and not pending:
                source = completed[0]
                _validate_xlsx(source)
                destination = directory / filename
                if source != destination:
                    source.replace(destination)
                return destination
            if time.monotonic() >= deadline:
                raise ValueError("等待本次全球财务明细下载超时")
            _continue_global_authorization(driver, baseline, handled, expected_seller)
            time.sleep(min(.2, max(0, deadline - time.monotonic())))
    finally:
        try:
            if original_handle and original_handle in driver.window_handles and driver.current_window_handle != original_handle:
                driver.switch_to.window(original_handle)
        except Exception:
            pass
        try:
            driver.execute_cdp_cmd(command, {"behavior": "default"})
        except Exception:
            pass
