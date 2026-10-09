"""Select Simplified Chinese through Lazada's observed language menus."""
from __future__ import annotations

import time
from urllib.parse import urlsplit

from selenium.common.exceptions import JavascriptException, StaleElementReferenceException, TimeoutException


class LazadaLanguageError(ValueError):
    pass


SELLER_LANGUAGE_STATE = r"""
/* lazada_seller_language_state */
return (() => {
  const visible=e=>{
    if(!e || !e.isConnected || !e.getClientRects().length)return false;
    const style=getComputedStyle(e);
    return style.display!=='none' && !['hidden','collapse'].includes(style.visibility);
  };
  const text=e=>(e?.innerText || e?.textContent || '').trim();
  const base={ready:document.readyState==='complete',time_origin:performance.timeOrigin};
  if(arguments[0]===true){
    const triggers=[...document.querySelectorAll('button[data-spm="d_switch_lang_btn"][aria-haspopup="true"]')].filter(visible);
    if(!triggers.length)return {...base,present:false};
    if(triggers.length!==1)return {...base,error:'Lazada 登录语言入口无法唯一确认'};
    const trigger=triggers[0];
    const labels=[...trigger.querySelectorAll('span.next-btn-helper')].filter(e=>visible(e) && text(e));
    if(labels.length!==1)return {...base,error:'Lazada 登录当前语言标签无法唯一确认'};
    const menus=[...document.querySelectorAll('ul.next-menu.next-menu-selectable-single[role="listbox"][aria-multiselectable="false"]')].filter(visible);
    if(menus.length>1)return {...base,error:'Lazada 登录语言菜单无法唯一确认'};
    const options=menus.length ? [...menus[0].querySelectorAll(':scope > li[role="option"]')].filter(visible) : [];
    const chinese=options.filter(e=>e.getAttribute('title')==='简体中文' && text(e)==='简体中文');
    if(chinese.length>1)return {...base,error:'Lazada 简体中文选项无法唯一确认'};
    const label=text(labels[0]);
    return {...base,present:true,locale:label==='简体中文'?'zh_CN':label==='English'?'en':'',label,
      trigger,trigger_enabled:!trigger.disabled && trigger.getAttribute('aria-disabled')!=='true',
      expanded:menus.length===1,menu_present:menus.length===1,option_count:options.length,chinese:chinese[0] || null};
  }
  const roots=[...document.querySelectorAll('ul#language_position[role="listbox"]')].filter(visible);
  if(!roots.length)return {...base,present:false};
  if(roots.length!==1)return {...base,error:'Lazada 当前语言控件无法唯一确认'};
  const root=roots[0];
  const triggers=[...root.querySelectorAll(':scope > li[aria-haspopup="true"]')].filter(visible);
  if(triggers.length>1)return {...base,error:'Lazada 语言菜单入口无法唯一确认'};
  const trigger=triggers[0];
  const menus=[...document.querySelectorAll('ul.a-l-language-popup-container[role="menu"]')].filter(visible);
  if(menus.length>1)return {...base,error:'Lazada 语言选项菜单无法唯一确认'};
  const options=menus.length ? [...menus[0].querySelectorAll(':scope > li[role="option"]')].filter(visible) : [];
  const chinese=options.filter(e=>e.getAttribute('data-spm')==='d_lang_zh_cn' &&
    (e.getAttribute('title')==='简体中文' || text(e)==='简体中文'));
  if(chinese.length>1)return {...base,error:'Lazada 简体中文选项无法唯一确认'};
  return {...base,present:true,locale:root.getAttribute('data-more') || '',
    label:trigger ? (trigger.getAttribute('title') || text(trigger)) : text(root),
    trigger:trigger || null,trigger_enabled:!!trigger && trigger.getAttribute('aria-disabled')!=='true' && !trigger.hasAttribute('disabled'),
    expanded:trigger?.getAttribute('aria-expanded')==='true',menu_present:menus.length===1,
    option_count:options.length,chinese:chinese[0] || null};
})();
"""

LANGUAGE_CLICK_HIT = r"""
/* lazada_language_click_hit */
const target=arguments[0],rect=target.getBoundingClientRect();
const x=rect.left+rect.width/2,y=rect.top+rect.height/2;
if(rect.width<=0 || rect.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight)return false;
const hit=document.elementFromPoint(x,y);
return hit===target || target.contains(hit);
"""


def _location(driver):
    from backend.services.lazada_balance_login import SELLER_HOSTS, GSP_HOSTS, BUSINESS_PATHS
    try:
        value = urlsplit(str(driver.current_url or ''))
        supported = BUSINESS_PATHS | {'/apps/balance/unaccessable', '/apps/balance/unavailable'}
        path = value.path.rstrip('/')
        allowed = ((value.hostname in SELLER_HOSTS and path in (supported | {'/apps/seller/login'}))
                   or (value.hostname in GSP_HOSTS and path == '/page/login'))
        if (value.scheme != 'https' or value.username or value.password or value.port not in (None, 443) or not allowed):
            raise LazadaLanguageError('Lazada 语言切换仅支持当前受信任的资金或登录页面')
        return value.hostname, value.path.rstrip('/'), value.fragment
    except ValueError as exc:
        if isinstance(exc, LazadaLanguageError):
            raise
        raise LazadaLanguageError('Lazada 当前页面网址无法确认') from None


def _language(state: dict) -> str:
    locale = str(state.get('locale') or '').replace('-', '_').lower()
    label = str(state.get('label') or '').strip()
    if locale == 'zh_cn' and label == '简体中文':
        return 'zh_CN'
    if locale in {'en', 'en_us', 'en_gb'} and label.lower() == 'english':
        return 'en'
    return ''


def _read(driver, *, login: bool = False) -> dict:
    try:
        state = driver.execute_script(SELLER_LANGUAGE_STATE, login) or {}
    except (JavascriptException, StaleElementReferenceException):
        return {}
    if state.get('error'):
        raise LazadaLanguageError(state['error'])
    return state


def _click(driver, element, description: str) -> None:
    if (element is None or not element.is_displayed() or not element.is_enabled()
            or driver.execute_script(LANGUAGE_CLICK_HIT, element) is not True):
        raise LazadaLanguageError(f'Lazada {description}被遮挡或不可操作')
    element.click()


def _result(language: str, changed: bool, reason: str = '') -> dict:
    return {'language': language, 'changed': changed, 'fallback': language == 'en', 'reason': reason}


def ensure_lazada_login_chinese(driver, *, timeout: float = 20) -> dict | None:
    """Switch an observed login menu, preserving login forms without this UI."""
    location = _location(driver)
    if location[1] not in {'/apps/seller/login', '/page/login'}:
        raise LazadaLanguageError('Lazada 当前页面不是已确认的语言登录页面')
    if not _read(driver, login=True).get('present'):
        return None
    return ensure_lazada_chinese(driver, timeout=timeout)


def ensure_lazada_chinese(driver, *, timeout: float = 20) -> dict:
    """Confirm Chinese, or explicitly report English when no Chinese choice exists.

    A Chinese selection must cause a real document reload and a confirmed current
    language change. No fallback is allowed after selecting Chinese. HTML lang is
    diagnostic only: the real Ads page may retain lang=en while showing Chinese.
    """
    if timeout <= 0:
        raise LazadaLanguageError('Lazada 语言切换等待时间必须大于 0')
    original = _location(driver)
    login = original[1] in {'/apps/seller/login', '/page/login'}
    deadline = time.monotonic() + timeout
    state = {}
    while time.monotonic() < deadline:
        state = _read(driver, login=login)
        if state.get('present') and state.get('ready'):
            break
        time.sleep(.1)
    else:
        raise LazadaLanguageError('Lazada 当前语言控件未加载，不能确认采集语言')
    if _location(driver) != original:
        raise LazadaLanguageError('Lazada 语言确认期间站点或页面发生变化')
    language = _language(state)
    if language == 'zh_CN':
        return _result('zh_CN', False)
    if not state.get('trigger') or not state.get('trigger_enabled'):
        if language == 'en':
            return _result('en', False, '当前明确为 English，页面未提供可用语言切换入口')
        raise LazadaLanguageError('Lazada 未提供可用语言切换入口，且当前不是已确认的简体中文或 English')
    if not state.get('expanded'):
        _click(driver, state['trigger'], '语言菜单入口')
    while time.monotonic() < deadline:
        if _location(driver) != original:
            raise LazadaLanguageError('Lazada 打开语言菜单后站点或页面发生变化')
        state = _read(driver, login=login)
        if state.get('chinese'):
            break
        if state.get('menu_present') and state.get('option_count'):
            if language == 'en' and state.get('ready') and _language(state) == 'en':
                return _result('en', False, '当前明确为 English，语言菜单未提供简体中文选项')
            raise LazadaLanguageError('Lazada 语言菜单未提供简体中文，不能读取未确认语言的数据')
        time.sleep(.1)
    else:
        if language == 'en' and state.get('ready') and _language(state) == 'en':
            return _result('en', False, '当前明确为 English，页面未提供可确认的中文语言选项')
        raise LazadaLanguageError('Lazada 简体中文选项未加载')
    before_origin = state.get('time_origin')
    if not isinstance(before_origin, (int, float)) or before_origin <= 0:
        raise LazadaLanguageError('Lazada 页面重载标识无法确认')
    try:
        _click(driver, state['chinese'], '简体中文选项')
    except TimeoutException:
        # A native navigation click may time out after it already took effect.
        # Its success still requires the new document and current language below.
        pass
    stable = 0
    while time.monotonic() < deadline:
        state = _read(driver, login=login)
        if state.get('ready'):
            if _location(driver) != original:
                raise LazadaLanguageError('Lazada 切换语言后站点或资金页面发生变化')
            valid = state.get('time_origin') != before_origin and _language(state) == 'zh_CN'
            stable = stable + 1 if valid else 0
            if stable >= 2:
                return _result('zh_CN', True)
        else:
            stable = 0
        time.sleep(.1)
    raise LazadaLanguageError('Lazada 点击简体中文后未确认页面重载及语言生效，已停止采集')
