"""Verified TEMU controls over either the desktop or CLI browser transport."""
from __future__ import annotations

import json
import re
import time
from datetime import date

from backend.services.temu_balance_calendar import CALENDAR_STATE
from backend.core.ziniao_browser import ZiniaoClickInterceptedError


_COMMON = r'''
const visible=e=>Boolean(e && e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
const clean=s=>String(s||'').replace(/\s+/g,'').replace(/^\*/, '');
const disabled=e=>Boolean(e.matches(':disabled') || e.closest('[disabled],[aria-disabled="true"]'));
const path=e=>{const parts=[];while(e && e.nodeType===1){
  for(const attr of ['id','data-testid']){const value=e.getAttribute(attr);
    if(value){const anchor='['+attr+'='+JSON.stringify(value)+']';
      if(document.querySelectorAll(anchor).length===1){parts.unshift(anchor);return parts.join(' > ');}}}
  const peers=e.parentElement?[...e.parentElement.children].filter(x=>x.tagName===e.tagName):[e];
  parts.unshift(e.tagName.toLowerCase()+':nth-of-type('+(peers.indexOf(e)+1)+')');e=e.parentElement;
}return parts.join(' > ');};
const pack=v=>v instanceof Element?path(v):Array.isArray(v)?v.map(pack):v&&typeof v==='object'?Object.fromEntries(Object.entries(v).map(([k,x])=>[k,pack(x)])):v;
'''


def _script(body: str, *args) -> str:
    # Every DOM handle is serialized in the same observation as its state; the
    # transport subsequently sends a trusted click to that exact CSS path.
    return '(() => {' + _COMMON + 'return JSON.stringify(pack((function(){' + body + '}).apply(null,' + json.dumps(args, ensure_ascii=False) + ')));})()'


CALENDAR_SCRIPT = _script(CALENDAR_STATE)
OVERLAY_STATE = r'''
// Drawers can stay mounted off-screen (or transparent) after closing. Their
// non-zero layout rectangle alone does not mean they are blocking this page.
const shown=e=>{
  if(!visible(e))return false;
  const r=e.getBoundingClientRect();
  if(r.right<=0||r.bottom<=0||r.left>=innerWidth||r.top>=innerHeight)return false;
  for(let node=e;node;node=node.parentElement){
    if(node.getAttribute('aria-hidden')==='true'||node.hasAttribute('inert')||getComputedStyle(node).opacity==='0')return false;
  }
  return true;
};
const headers=[...document.querySelectorAll('[class*="new-bell_header__"]')].filter(shown)
  .filter(e=>[...e.querySelectorAll('div')].some(label=>visible(label)&&clean(label.textContent)==='全部消息'));
if(!headers.length)return {present:false};
if(headers.length!==1)return {present:true,error:'TEMU 消息面板无法唯一确认'};
const labels=[...document.querySelectorAll('a,button,span,div')].filter(e=>visible(e)&&clean(e.innerText||e.textContent)==='消息'
  && !headers[0].contains(e) && ![...e.children].some(c=>visible(c)&&clean(c.innerText||c.textContent)==='消息'));
const toggles=[...new Set(labels.map(e=>e.closest('button,a,[role="button"]')||e))].filter(e=>!disabled(e));
// Selenium checks the in-view centre. The message panel can cover its own
// toolbar toggle; in that case use the close control inside the panel.
const reachable=e=>{const r=e.getBoundingClientRect(),l=Math.max(0,r.left),t=Math.max(0,r.top),
  right=Math.min(innerWidth,r.right),bottom=Math.min(innerHeight,r.bottom);
  if(right<=l||bottom<=t)return false;
  const hit=document.elementFromPoint((l+right)/2,(t+bottom)/2);
  return Boolean(hit&&(hit===e||e.contains(hit)));};
if(toggles.length===1&&reachable(toggles[0]))return {present:true,close:toggles[0],method:'toggle'};
const closes=[...headers[0].querySelectorAll(':scope > svg[data-testid="beast-core-icon-close"]')].filter(visible);
if(closes.length!==1)return {present:true,error:'TEMU 消息面板关闭按钮无法唯一确认'};
if(!reachable(closes[0]))return {present:true,blocked:true};
return {present:true,close:closes[0],method:'close'};
'''
OVERLAY_SCRIPT = _script(OVERLAY_STATE)

FIELD_STATE = r'''
const [label,kind]=arguments;
const selector=kind==='range'?'input[data-testid="beast-core-rangePicker-htmlInput"]':'[data-testid="beast-core-select-header"],[role="combobox"],select';
const drawers=[...document.querySelectorAll('[data-testid="beast-core-drawer-content"]')].filter(e=>visible(e)&&e.innerText.includes('售后单创建日期'));
if(drawers.length>1)return [];
const scope=drawers[0]||document;
const labels=[...scope.querySelectorAll('label,span,div')].filter(e=>visible(e)&&clean(e.innerText||e.textContent)===clean(label)&&![...e.children].some(c=>visible(c)&&clean(c.innerText||c.textContent)===clean(label)));
let found=[];
for(const leaf of labels){let parent=leaf.parentElement;
  for(let i=0;parent&&parent!==document.body&&i<6;i++,parent=parent.parentElement){
    let fields=[...parent.querySelectorAll(selector)].filter(visible);
    fields=fields.filter(e=>!fields.some(other=>other!==e&&other.contains(e)));
    if(fields.length===1){found.push(fields[0]);break;}
    if(fields.length>1)break;
  }
}
return [...new Set(found)].map(e=>({selector:path(e),disabled:disabled(e),
  value:kind==='range'?e.value:clean(e.tagName==='SELECT'?e.selectedOptions[0]?.textContent:
    e.matches('input')?e.value:e.querySelector('input')?e.querySelector('input').value:(e.innerText||e.textContent||e.value)),tag:e.tagName,
  text:clean(e.textContent),placeholder:e.querySelector('input')?.getAttribute('placeholder')||''}));
'''

OPTIONS_STATE = r'''
const [value]=arguments;
const roots=[...document.querySelectorAll('[role="listbox"],[data-testid="beast-core-select-dropdown"],[data-testid="beast-core-select-panel"]')].filter(visible);
const candidates=[...(roots.length?roots: [document.body])].flatMap(root=>[...root.querySelectorAll('[role="option"],li,div,span')]);
const leaves=[...new Set(candidates)].filter(e=>visible(e)&&clean(e.innerText||e.textContent)===clean(value)&&![...e.children].some(c=>visible(c)&&clean(c.innerText||c.textContent)===clean(value)));
return [...new Set(leaves.map(e=>e.closest('[role="option"],[data-testid="beast-core-select-option"]')||e))].map(e=>({selector:path(e),disabled:disabled(e)}));
'''

CONFIRM_STATE = r'''
const roots=[...document.querySelectorAll('[data-testid="beast-core-rangePicker-dropdown-contentRoot"]')].filter(visible);
if(!roots.length)return {open:false,buttons:[]};
if(roots.length!==1)return {error:'日期选择器不唯一'};
return {open:true,buttons:[...roots[0].querySelectorAll('button')].filter(e=>visible(e)&&clean(e.innerText||e.textContent)==='确认').map(e=>({selector:path(e),disabled:disabled(e)}))};
'''


def _wait(check, timeout, message):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise ValueError(message)
        time.sleep(min(.15, max(0, deadline - time.monotonic())))


def _evaluate(browser, store_id, script):
    value = browser.evaluate(store_id, script)
    if isinstance(value, dict) and value.get('error'):
        raise ValueError(value['error'])
    return value


def field_state(browser, store_id, label, kind='range'):
    fields = _evaluate(browser, store_id, _script(FIELD_STATE, label, kind))
    if not isinstance(fields, list) or len(fields) != 1 or fields[0].get('disabled'):
        raise ValueError(f'无法唯一定位可用的“{label}”筛选框')
    return fields[0]


def dismiss_overlays(browser, store_id, *, timeout=5):
    """Dismiss 全部消息 once through its HTML toggle, or its own close icon.

    The CLI cannot reliably click SVG elements. The observed 消息 entry toggles
    this same panel, and is used only while the panel is confirmed open.
    """
    state = _evaluate(browser, store_id, OVERLAY_SCRIPT)
    if not state.get('present'):
        return False
    if state.get('blocked'):
        state = _wait(lambda: (s if not (s := _evaluate(browser, store_id, OVERLAY_SCRIPT)).get('blocked') else None),
                      timeout, 'TEMU 消息面板关闭按钮被遮挡')
        if not state.get('present'):
            return True
    browser.click(store_id, state['close'])
    _wait(lambda: not _evaluate(browser, store_id, OVERLAY_SCRIPT).get('present'), timeout, 'TEMU 消息面板未关闭')
    return True


def click_control(browser, store_id, selector):
    """Retry only a click explicitly rejected by Selenium, never a submission
    with an unknown result. Re-resolve dynamic controls after closing a panel.
    """
    dismiss_overlays(browser, store_id)
    resolve = selector if callable(selector) else lambda: selector
    try:
        return browser.click(store_id, resolve())
    except ZiniaoClickInterceptedError:
        if not dismiss_overlays(browser, store_id):
            raise
        return browser.click(store_id, resolve())


def _calendar(browser, store_id):
    state = _evaluate(browser, store_id, CALENDAR_SCRIPT)
    return state if state.get('panes') else None


def _months(state):
    return tuple((p['year'], p['month']) for p in state['panes'])


def _pair(value):
    return tuple(re.findall(r'\d{4}-\d{2}-\d{2}', value or ''))


def set_range(browser, store_id, start: date, end: date, *, label: str, timeout=20, max_month_steps=240):
    if not isinstance(start, date) or not isinstance(end, date) or start > end:
        raise ValueError('日期范围无效')
    expected = (start.isoformat(), end.isoformat())
    dismiss_overlays(browser, store_id)
    field = field_state(browser, store_id, label)
    if _pair(field.get('value')) == expected:
        return field['value']
    click_control(browser, store_id, lambda: field_state(browser, store_id, label)['selector'])
    for target in (start, end):
        state = _wait(lambda: _calendar(browser, store_id), timeout, '日期选择器未打开')
        month = (target.year, target.month)
        for step in range(max_month_steps + 1):
            displayed = _months(state)
            if month in displayed:
                break
            if step == max_month_steps:
                raise ValueError('日期超出日历可导航范围')
            forward = month > max(displayed)
            arrows = state.get('right' if forward else 'left') or []
            if not arrows:
                raise ValueError('日历翻月按钮不可用')
            click_control(browser, store_id, arrows[-1] if forward else arrows[0])
            state = _wait(lambda: (s if (s := _calendar(browser, store_id)) and _months(s) != displayed else None), timeout, '日期选择器翻月未刷新')
        choices = [selector for pane in state['panes'] if (pane['year'], pane['month']) == month for selector in pane['days'].get(str(target.day), [])]
        if len(choices) != 1:
            raise ValueError(f'日期 {target.isoformat()} 不可用或无法唯一定位')
        click_control(browser, store_id, choices[0])
    # Some Beast versions commit on the second date, others require 确认.
    confirmation = _evaluate(browser, store_id, _script(CONFIRM_STATE))
    if confirmation.get('open'):
        buttons = confirmation.get('buttons', [])
        if len(buttons) != 1 or buttons[0].get('disabled'):
            raise ValueError('日期已选择，但无法唯一定位可用的确认按钮')
        click_control(browser, store_id, buttons[0]['selector'])
    result = _wait(lambda: (f if _pair((f := field_state(browser, store_id, label)).get('value')) == expected else None), timeout, '日期范围未正确回显')
    return result['value']


def select_field(browser, store_id, label, value, *, timeout=10):
    desired = re.sub(r'\s+', '', str(value))
    if not desired:
        raise ValueError('筛选值不能为空')
    dismiss_overlays(browser, store_id)
    field = field_state(browser, store_id, label, 'select')
    if field['value'] == desired:
        return field['value']
    click_control(browser, store_id, lambda: field_state(browser, store_id, label, 'select')['selector'])
    choices = _wait(lambda: _evaluate(browser, store_id, _script(OPTIONS_STATE, value)), timeout, f'未找到“{value}”选项')
    if len(choices) != 1 or choices[0].get('disabled'):
        raise ValueError(f'无法唯一定位可用的“{value}”选项')
    click_control(browser, store_id, choices[0]['selector'])
    result = _wait(lambda: (f if (f := field_state(browser, store_id, label, 'select'))['value'] == desired else None), timeout, f'无法确认“{label}”已选择“{value}”')
    return result['value']
