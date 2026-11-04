"""DOM controls for the TEMU reconciliation-center financial export.

Controls are resolved afresh from visible labels and are never assumed to be at
a fixed position.  Every selection is read back before the export is submitted.
"""
from __future__ import annotations

import json
import re
from datetime import date
from uuid import uuid4

from backend.services.temu_fee_refund_controls import (
    CALENDAR_SCRIPT,
    _evaluate,
    _script,
    _wait,
    click_control,
    dismiss_overlays,
)


FIELD_STATE = r'''
const [label,kind]=arguments;
const selector=kind==='range'
  ?'input[data-testid="beast-core-rangePicker-htmlInput"]'
  :'[data-testid="beast-core-select-header"],[role="combobox"],[data-testid="beast-core-select-htmlInput"],select';
const leaves=[...document.querySelectorAll('label,span,div')].filter(e=>visible(e)
  && clean(e.innerText||e.textContent)===clean(label)
  && ![...e.children].some(c=>visible(c)&&clean(c.innerText||c.textContent)===clean(label)));
const found=[];
for(const leaf of leaves){
  let parent=leaf.parentElement;
  for(let i=0;parent&&parent!==document.body&&i<8;i++,parent=parent.parentElement){
    let fields=[...parent.querySelectorAll(selector)].filter(visible);
    fields=fields.filter(e=>!fields.some(other=>other!==e&&other.contains(e)));
    if(fields.length===1){found.push(fields[0]);break;}
    if(fields.length>1)break;
  }
}
return [...new Set(found)].map(e=>{
  const input=e.matches('input')?e:e.querySelector('input');
  const selected=e.tagName==='SELECT'?(e.selectedOptions[0]?.textContent||''):'';
  const value=kind==='range'?String(e.value||''):
    clean(selected||(input?input.value:'')||e.textContent);
  return {selector:path(e),disabled:disabled(e),tag:e.tagName,value,
    text:clean(e.textContent),placeholder:input?.getAttribute('placeholder')||''};
});
'''

OPTIONS_STATE = r'''
const [value]=arguments;
const roots=[...document.querySelectorAll('[role="listbox"],[data-testid="beast-core-select-dropdown"],[data-testid="beast-core-select-panel"]')].filter(visible);
const candidates=[...(roots.length?roots:[document.body])].flatMap(root=>[...root.querySelectorAll('[role="option"],li,div,span')]);
const leaves=[...new Set(candidates)].filter(e=>visible(e)&&clean(e.innerText||e.textContent)===clean(value)
  &&![...e.children].some(c=>visible(c)&&clean(c.innerText||c.textContent)===clean(value)));
return [...new Set(leaves.map(e=>e.closest('[role="option"],[data-testid="beast-core-select-option"]')||e))]
  .map(e=>({selector:path(e),disabled:disabled(e)}));
'''

CONFIRM_STATE = r'''
const roots=[...document.querySelectorAll('[data-testid="beast-core-rangePicker-dropdown-contentRoot"]')].filter(visible);
if(!roots.length)return {open:false,buttons:[]};
if(roots.length!==1)return {error:'日期选择器不唯一'};
return {open:true,buttons:[...roots[0].querySelectorAll('button')].filter(e=>visible(e)&&clean(e.innerText||e.textContent)==='确认')
  .map(e=>({selector:path(e),disabled:disabled(e)}))};
'''

CUSTOM_MODE_STATE = r'''
const leaves=[...document.querySelectorAll('a,button,span,div,li,[role="tab"],[role="button"]')].filter(e=>visible(e)
  && clean(e.innerText||e.textContent)==='自定义'
  && ![...e.children].some(c=>visible(c)&&clean(c.innerText||c.textContent)==='自定义'));
return [...new Set(leaves.map(e=>e.closest('button,a,[role="tab"],[role="button"],label')||e))]
  .map(e=>({selector:path(e),disabled:disabled(e)}));
'''

FILTER_STATE = r'''
const query=[...document.querySelectorAll('button')].filter(e=>visible(e)&&clean(e.innerText||e.textContent)==='查询');
if(query.length!==1)return {query_count:query.length,controls:[]};
let form=query[0].parentElement;
while(form&&form!==document.body){
  const text=form.innerText||'';
  if(form.querySelector('input,textarea,select')&&/账务明细查询时间|账务类型|资金流向/.test(text))break;
  form=form.parentElement;
}
if(!form||form===document.body)return {query_count:1,controls:[],form_missing:true};
let fields=[...form.querySelectorAll('input,textarea,select,[role="combobox"],[data-testid="beast-core-select-header"]')].filter(visible);
fields=fields.filter(e=>!fields.some(other=>other!==e&&other.contains(e)));
const controls=fields.filter(e=>e.type!=='hidden'&&e.type!=='checkbox'&&e.type!=='radio').map(e=>{
  const input=e.matches('input')?e:e.querySelector('input');
  const select_like=e.tagName==='SELECT'||e.matches('[role="combobox"],[data-testid="beast-core-select-header"]');
  const selected=e.tagName==='SELECT'?(e.selectedOptions[0]?.textContent||''):'';
  return {tag:e.tagName,testid:e.getAttribute('data-testid')||'',kind:select_like?'select':'input',
    value:select_like?clean(selected||(input?input.value:'')||e.textContent):String(e.value||''),
    text:clean(e.textContent),placeholder:input?.getAttribute('placeholder')||''};
});
return {query_count:1,controls};
'''

CLOCK_STATE = _script(r'''
const d=new Date(),p=x=>String(x).padStart(2,'0');
return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
''')


def field_state(browser, store_id, label, kind="range"):
    fields = _evaluate(browser, store_id, _script(FIELD_STATE, label, kind))
    if not isinstance(fields, list) or len(fields) != 1 or fields[0].get("disabled"):
        raise ValueError(f"无法唯一定位可用的“{label}”筛选框")
    return fields[0]


def filter_state(browser, store_id):
    state = _evaluate(browser, store_id, _script(FILTER_STATE))
    if state.get("form_missing"):
        raise ValueError("无法确认财务查询筛选区域")
    if state.get("query_count") != 1:
        raise ValueError("无法唯一定位财务查询按钮，不能确认筛选条件")
    return state


def _pair(value):
    return tuple(re.findall(r"\d{4}-\d{2}-\d{2}", value or ""))


def _range_readback(value):
    """Dates plus optional times, in display order."""
    return [
        (match.group(1), match.group(2) or "")
        for match in re.finditer(
            r"(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}(?::\d{2})?))?", value or "",
        )
    ]


def validate_inclusive_range(value, start: date, end: date) -> str:
    """Require the exact inclusive range and reject non-full-day explicit times."""
    parts = _range_readback(value)
    dates = [item[0] for item in parts]
    if len(dates) != 2 or dates[0] != start.isoformat() or dates[-1] != end.isoformat():
        raise ValueError("账务明细查询日期未回显为本次请求的完整范围")
    start_time = parts[0][1]
    end_time = parts[-1][1]
    if start_time and start_time not in {"00:00", "00:00:00"}:
        raise ValueError("账务明细查询开始时间不是 00:00:00")
    if end_time and end_time not in {"23:59", "23:59:59"}:
        raise ValueError("账务明细查询结束时间不是 23:59:59")
    return value


def set_custom_range(browser, store_id, label, start: date, end: date, *, timeout=25, max_month_steps=240):
    """Open custom mode and select an inclusive full-day range, then read it back."""
    if not isinstance(start, date) or not isinstance(end, date) or start > end:
        raise ValueError("日期范围无效")
    dismiss_overlays(browser, store_id)
    modes = _evaluate(browser, store_id, _script(CUSTOM_MODE_STATE))
    if len(modes) > 1:
        raise ValueError("页面存在多个“自定义”日期选项，无法确认日期模式")
    if len(modes) == 1:
        if modes[0].get("disabled"):
            raise ValueError("“自定义”日期选项不可用")
        click_control(browser, store_id, modes[0]["selector"])

    field = field_state(browser, store_id, label)
    current = None
    if _pair(field.get("value")) == (start.isoformat(), end.isoformat()):
        try:
            current = validate_inclusive_range(
                (field.get("value") or "") + " " + (field.get("text") or ""), start, end,
            )
        except ValueError:
            current = None
    if current is not None:
        return field["value"]

    state = _evaluate(browser, store_id, CALENDAR_SCRIPT)
    if not state.get("panes"):
        click_control(browser, store_id, lambda: field_state(browser, store_id, label)["selector"])
        modes = _evaluate(browser, store_id, _script(CUSTOM_MODE_STATE))
        if len(modes) > 1 or (modes and modes[0].get("disabled")):
            raise ValueError("无法唯一确认可用的自定义日期选项")
        if modes:
            click_control(browser, store_id, modes[0]["selector"])
        state = _wait(lambda: (s if (s := _evaluate(browser, store_id, CALENDAR_SCRIPT)).get("panes") else None),
                      timeout, "日期选择器未打开")
    for target in (start, end):
        month = (target.year, target.month)
        for step in range(max_month_steps + 1):
            displayed = tuple((pane["year"], pane["month"]) for pane in state["panes"])
            if month in displayed:
                break
            if step == max_month_steps:
                raise ValueError("日期超出日历可导航范围")
            forward = month > max(displayed)
            arrows = state.get("right" if forward else "left") or []
            if not arrows:
                raise ValueError("日历翻月按钮不可用")
            click_control(browser, store_id, arrows[-1] if forward else arrows[0])
            state = _wait(
                lambda: (s if (s := _evaluate(browser, store_id, CALENDAR_SCRIPT)).get("panes")
                         and tuple((p["year"], p["month"]) for p in s["panes"]) != displayed else None),
                timeout, "日期选择器翻月未刷新",
            )
        choices = [
            selector for pane in state["panes"]
            if (pane["year"], pane["month"]) == month
            for selector in pane["days"].get(str(target.day), [])
        ]
        if len(choices) != 1:
            raise ValueError(f"日期 {target.isoformat()} 不可用或无法唯一定位")
        click_control(browser, store_id, choices[0])

    confirmation = _evaluate(browser, store_id, _script(CONFIRM_STATE))
    if confirmation.get("open"):
        buttons = confirmation.get("buttons", [])
        if len(buttons) != 1 or buttons[0].get("disabled"):
            raise ValueError("日期已选择，但无法唯一定位可用的确认按钮")
        click_control(browser, store_id, buttons[0]["selector"])

    def readback():
        state = field_state(browser, store_id, label)
        text = (state.get("value") or "") + " " + (state.get("text") or "")
        try:
            return validate_inclusive_range(text, start, end)
        except ValueError:
            return None

    return _wait(readback, timeout, "账务明细查询日期未回显为完整全天范围")


def _displayed(field, value):
    desired = re.sub(r"\s+", "", str(value))
    return any(
        re.sub(r"\s+", "", str(field.get(key) or "")) == desired
        for key in ("value", "text", "placeholder")
    )


def select_field(browser, store_id, label, value, *, timeout=10):
    desired = re.sub(r"\s+", "", str(value))
    if not desired:
        raise ValueError("筛选值不能为空")
    dismiss_overlays(browser, store_id)
    field = field_state(browser, store_id, label, "select")
    if _displayed(field, value):
        return field["value"]
    click_control(browser, store_id, lambda: field_state(browser, store_id, label, "select")["selector"])
    choices = _wait(lambda: _evaluate(browser, store_id, _script(OPTIONS_STATE, value)), timeout, f"未找到“{value}”选项")
    if len(choices) != 1 or choices[0].get("disabled"):
        raise ValueError(f"无法唯一定位可用的“{value}”选项")
    click_control(browser, store_id, choices[0]["selector"])
    result = _wait(
        lambda: (f if _displayed(f := field_state(browser, store_id, label, "select"), value) else None),
        timeout, f"无法确认“{label}”已选择“{value}”",
    )
    return result["value"]


def ensure_other_filters_default(browser, store_id, start: date, end: date):
    """Reset must leave every non-date filter at all/empty with no amount bounds."""
    state = filter_state(browser, store_id)
    ranges = [item for item in state["controls"] if item.get("testid") == "beast-core-rangePicker-htmlInput"]
    if len(ranges) != 1:
        raise ValueError("无法唯一确认账务明细查询时间筛选框")
    validate_inclusive_range((ranges[0].get("value") or "") + " " + (ranges[0].get("text") or ""), start, end)
    allowed = {"", "全部", "--", "请选择"}
    for control in state["controls"]:
        if control is ranges[0]:
            continue
        displayed = re.sub(r"\s+", "", str(control.get("value") or ""))
        if control.get("kind") == "select":
            if displayed not in allowed:
                raise ValueError("账务明细查询仍保留了账务类型、资金流向或其他筛选，请先重置")
        elif displayed:
            raise ValueError("账务明细查询仍保留了金额、流水号或其他筛选，请先重置")
    return state


QUERY_INSTALL = r'''
const key=arguments[0];
const buttons=[...document.querySelectorAll('button')].filter(e=>visible(e)&&clean(e.innerText||e.textContent)==='查询');
if(buttons.length!==1||disabled(buttons[0]))return {error:'无法唯一定位可用的查询按钮'};
const resultRoots=()=>{
  const all=[...document.querySelectorAll('table,[role="table"],[data-testid="beast-core-table"]')].filter(visible);
  return all.filter(e=>!all.some(other=>other!==e&&other.contains(e)));
};
if(!resultRoots().length)return {error:'无法确认财务查询结果区域'};
const loading=()=>[...document.querySelectorAll('[aria-busy="true"],[data-loading="true"],[data-testid="beast-core-loading"],[data-testid="beast-core-spin"],.ant-spin-spinning,.el-loading-mask')].some(visible);
const s={button:buttons[0],roots:resultRoots(),armed:false,mutations:0,saw_loading:false,last:performance.now()};
s.sample=()=>{const busy=loading();if(s.armed&&busy){s.saw_loading=true;s.last=performance.now();}
  return {armed:s.armed,mutations:s.mutations,saw_loading:s.saw_loading,busy,stable_ms:performance.now()-s.last};};
s.arm=()=>{s.armed=true;s.last=performance.now();s.mutations=0;s.saw_loading=false;};
s.observer=new MutationObserver(records=>{
  if(!s.armed)return;
  const current=resultRoots(),roots=[...s.roots,...current];
  const contains=(a,b)=>Boolean(a&&b&&(a===b||a.contains(b)));
  if(records.some(r=>roots.some(e=>contains(e,r.target))||[...r.addedNodes,...r.removedNodes].some(n=>roots.some(e=>contains(n,e))))){s.mutations++;s.last=performance.now();}
  s.roots=current;s.sample();
});
s.observer.observe(document.documentElement,{subtree:true,childList:true,characterData:true,attributes:true});
s.button.addEventListener('click',s.arm,{once:true,capture:true});window[key]=s;
return {selector:path(s.button)};
'''


def query_and_wait(browser, store_id, *, timeout=30):
    """Require a query click followed by observable result/loading progress."""
    key = "__hqyl_financial_query_" + uuid4().hex
    try:
        installed = _evaluate(browser, store_id, _script(QUERY_INSTALL, key))
        click_control(browser, store_id, installed["selector"])

        def refreshed():
            state = _evaluate(browser, store_id, _script(
                "const s=window[arguments[0]];return s?s.sample():{error:'查询观察器已失效'};", key))
            observed = state.get("mutations", 0) > 0 or state.get("saw_loading")
            return state if state.get("armed") and observed and not state.get("busy") and state.get("stable_ms", 0) >= 300 else None

        return _wait(refreshed, timeout, "查询后未能确认财务列表刷新，请检查页面或网络后重试")
    finally:
        try:
            _evaluate(browser, store_id, _script(
                "const s=window[arguments[0]];if(s){s.observer.disconnect();s.button.removeEventListener('click',s.arm,true);delete window[arguments[0]];}return true;", key))
        except Exception:
            pass
