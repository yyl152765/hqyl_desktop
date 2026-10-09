"""Dismiss the observed TEMU message panel using its own native close control."""
from __future__ import annotations

import time

from selenium.common.exceptions import ElementClickInterceptedException


MESSAGE_PANEL_STATE = r"""return (() => {
  const visible=e=>Boolean(e && e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
  const headers=[...document.querySelectorAll('[class*="new-bell_header__"]')].filter(visible)
    .filter(e=>[...e.querySelectorAll('div')].some(label=>visible(label) && label.textContent.trim()==='全部消息'));
  if(!headers.length) return {present:false};
  if(headers.length!==1) return {present:true,error:'TEMU 消息面板无法唯一确认'};
  const closes=[...headers[0].querySelectorAll(':scope > svg[data-testid="beast-core-icon-close"]')].filter(visible);
  if(closes.length!==1) return {present:true,error:'TEMU 消息面板关闭按钮无法唯一确认'};
  return {present:true,close:closes[0]};
})();"""


def dismiss_temu_message_panel(driver, *, timeout: float = 3) -> bool:
    """Close only 全部消息; never click message actions or unrelated X icons."""
    state = driver.execute_script(MESSAGE_PANEL_STATE)
    if state.get('error'):
        raise ValueError(state['error'])
    if not state.get('present'):
        return False
    close = state['close']
    if not close.is_displayed() or not close.is_enabled():
        raise ValueError('TEMU 消息面板关闭按钮尚不可操作')
    close.click()
    deadline = time.monotonic() + timeout
    while True:
        state = driver.execute_script(MESSAGE_PANEL_STATE)
        if not state.get('present'):
            return True
        if state.get('error'):
            raise ValueError(state['error'])
        if time.monotonic() >= deadline:
            raise ValueError('TEMU 消息面板未关闭，已停止操作')
        time.sleep(.1)


def click_with_message_panel_retry(driver, action):
    """Retry once only when Selenium proves the first click was intercepted."""
    dismiss_temu_message_panel(driver)
    try:
        return action()
    except ElementClickInterceptedException:
        if not dismiss_temu_message_panel(driver):
            raise
        return action()
