"""Select a complete TEMU month and verify a DOM-observable query refresh."""
from __future__ import annotations

import calendar
import re
import time
from uuid import uuid4

from selenium.webdriver.common.by import By
from backend.services.temu_balance_overlays import dismiss_temu_message_panel, click_with_message_panel_retry


RANGE_INPUT = 'input[data-testid="beast-core-rangePicker-htmlInput"]'


class TemuCalendarError(ValueError):
    pass


CALENDAR_STATE = r"""
return (() => {
  const visible=e=>Boolean(e && e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
  const disabled=e=>Boolean(e.closest('[disabled],[aria-disabled="true"]')) || [e,e.closest('td'),e.parentElement].filter(Boolean).some(n=>/disabled/i.test(n.className?.baseVal || n.className || ''));
  const roots=[...document.querySelectorAll('[data-testid="beast-core-rangePicker-dropdown-contentRoot"]')].filter(visible);
  if(roots.length!==1) return {error:roots.length ? '日期选择器不唯一' : '',panes:[]};
  const root=roots[0];
  const headers=[...root.querySelectorAll('[data-testid="beast-core-monthRangePicker-year-header"]')].filter(visible);
  const wrappers=[...root.querySelectorAll('[class*="RPR_tableWrapper"]')].filter(e=>visible(e) && e.querySelector('td') && ![...e.querySelectorAll('[class*="RPR_tableWrapper"]')].some(c=>c.querySelector('td')));
  if(headers.length!==wrappers.length || !headers.length) return {error:'无法对应日期选择器的月份标题与日期表格',panes:[]};
  const panes=headers.map((header,index)=>{
    const year=String(header.querySelector('input')?.value || '').match(/\d{4}/)?.[0];
    const label=[...header.querySelectorAll('[class*="RPR_dateText"]')].map(e=>e.textContent).join(' ') || header.textContent;
    const month=label.match(/(?:^|\D)(1[0-2]|[1-9])\s*月/)?.[1];
    const days={};
    for(const div of wrappers[index].querySelectorAll('td > div[title]')) {
      const td=div.closest('td');
      if(!visible(div) || /outofmonth/i.test(td.className) || disabled(div)) continue;
      const day=div.getAttribute('title');
      if(/^([1-9]|[12]\d|3[01])$/.test(day)) (days[day] ||= []).push(div);
    }
    return {year:Number(year),month:Number(month),days};
  });
  if(panes.some(p=>!p.year || !p.month)) return {error:'无法确认日期选择器当前年月',panes:[]};
  const arrows=direction=>[...root.querySelectorAll('[data-testid="beast-core-icon-'+direction+'"]')].filter(e=>visible(e)&&!disabled(e));
  return {panes,left:arrows('left'),right:arrows('right')};
})();
"""


REFRESH_INSTALL = r"""
return (() => {
  const [key,selector,expectedRange,loadingSelector]=arguments;
  const visible=e=>Boolean(e && e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
  const roots=()=>[...document.querySelectorAll(selector)].filter(visible);
  const inputs=()=>[...document.querySelectorAll('input[data-testid="beast-core-rangePicker-htmlInput"]')].filter(visible);
  const query=[...document.querySelectorAll('button')].filter(e=>visible(e) && e.textContent.replace(/\s/g,'')==='查询');
  if(roots().length!==1) return {error:'无法唯一确认待处理款项汇总面板'};
  if(query.length!==1 || query[0].disabled || query[0].getAttribute('aria-disabled')==='true') return {error:'查询按钮不可用或不唯一'};
  const button=query[0];
  const state={root:roots()[0],button,armed:false,mutations:0,saw_loading:false,last_change:performance.now(),error:'',before_text:''};
  const loading=root=>Boolean(button.disabled || button.getAttribute('aria-busy')==='true' || root?.matches(loadingSelector) || (root && [...root.querySelectorAll(loadingSelector)].some(visible)));
  if(loading(state.root)) return {error:'汇总面板仍在加载，请加载完成后查询'};
  state.sample=()=>{
    const current=roots();
    const rangeInputs=inputs();
    const range=rangeInputs.length===1 ? rangeInputs[0].value : '';
    if(expectedRange && range.replace(/\s/g,'')!==expectedRange.replace(/\s/g,'')) state.error='查询期间日期范围发生变化';
    const root=current.length===1 ? current[0] : null;
    const busy=loading(root);
    if(state.armed && busy) state.saw_loading=true;
    if(state.armed && busy!==state.loading) state.last_change=performance.now();
    state.loading=busy;
    const alerts=[...document.querySelectorAll('[role="alert"],[data-testid="beast-core-toast"]')].filter(visible).map(e=>e.innerText || e.textContent).join(' ');
    if(/请求失败|加载失败|查询失败|网络异常|系统繁忙|操作失败/.test(alerts)) state.error='查询返回错误：'+alerts.slice(0,300);
    return {armed:state.armed,mutations:state.mutations,saw_loading:state.saw_loading,loading:busy,
      unique:current.length===1,text:root?.innerText || '',before_text:state.before_text,
      stable_ms:performance.now()-state.last_change,range_value:range,error:state.error};
  };
  state.arm=()=>{
    const current=roots();
    if(current.length!==1 || loading(current[0])) {state.error='点击查询时面板尚未就绪';return;}
    state.root=current[0];state.before_text=state.root.innerText;state.mutations=0;
    state.saw_loading=false;state.loading=false;state.last_change=performance.now();state.armed=true;
  };
  state.observer=new MutationObserver(records=>{
    if(!state.armed) return;
    const current=roots();
    const next=current.length===1 ? current[0] : null;
    const contains=(parent,child)=>Boolean(parent && child && (parent===child || parent.contains(child)));
    for(const record of records) {
      if(record.type==='attributes') continue;
      const related=contains(state.root,record.target) || contains(next,record.target)
        || [...record.addedNodes,...record.removedNodes].some(node=>contains(node,state.root)||contains(node,next));
      if(related) {state.mutations++;state.last_change=performance.now();}
    }
    if(next) state.root=next;
    state.sample();
  });
  state.observer.observe(document.documentElement,{subtree:true,childList:true,characterData:true,attributes:true});
  button.addEventListener('click',state.arm,{once:true,capture:true});
  window[key]=state;
  return {button};
})();
"""

REFRESH_READ = "return window[arguments[0]] ? window[arguments[0]].sample() : {error:'查询刷新观察器已失效'};"
REFRESH_CLEANUP = "const state=window[arguments[0]]; if(state){state.observer.disconnect();state.button.removeEventListener('click',state.arm,true);delete window[arguments[0]];}"
DEFAULT_LOADING_SELECTOR = '[aria-busy="true"],[data-loading="true"],[data-testid="beast-core-loading"],[data-testid="beast-core-spin"],.ant-spin-spinning,.el-loading-mask'


def _wait(check, timeout: float, message: str):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TemuCalendarError(message)
        time.sleep(min(.15, max(0, deadline - time.monotonic())))


def _range_input(driver):
    inputs = [item for item in driver.find_elements(By.CSS_SELECTOR, RANGE_INPUT) if item.is_displayed()]
    if len(inputs) != 1:
        raise TemuCalendarError("无法唯一确认订单创建日期筛选框")
    return inputs[0]


def _date_pair(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\d{4}-\d{2}-\d{2}", value or ""))


def _calendar_state(driver):
    state = driver.execute_script(CALENDAR_STATE)
    if state.get("error"):
        raise TemuCalendarError(state["error"])
    return state if state.get("panes") else None


def _months(state) -> tuple[tuple[int, int], ...]:
    return tuple((pane["year"], pane["month"]) for pane in state["panes"])


def set_calendar_month(driver, month: str, *, timeout: float = 20, max_month_steps: int = 240) -> dict:
    """Select day 1 through month end with native clicks; do not submit the query."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(month)):
        raise TemuCalendarError("统计月份格式必须为 YYYY-MM")
    year, month_number = map(int, month.split("-"))
    if year < 1:
        raise TemuCalendarError("统计年份无效")
    last_day = calendar.monthrange(year, month_number)[1]
    start, end = f"{month}-01", f"{month}-{last_day:02d}"
    dismiss_temu_message_panel(driver)
    original = _range_input(driver).get_attribute("value") or ""
    if _date_pair(original) != (start, end):
        click_with_message_panel_retry(driver, lambda: _range_input(driver).click())
        state = _wait(lambda: _calendar_state(driver), timeout, "日期选择器未打开")
        target = (year, month_number)
        for step in range(max_month_steps + 1):
            displayed = _months(state)
            if target in displayed:
                break
            if step == max_month_steps:
                raise TemuCalendarError("统计月份超出日历可导航范围")
            # Earlier months use the left panel; later months use the right.
            # A gap between independent panels is advanced from the left.
            backwards = target < min(displayed)
            direction = "left" if backwards else "right"
            arrows = state.get(direction) or []
            if not arrows:
                raise TemuCalendarError("日历翻月按钮不可用")
            arrow = arrows[-1] if target > max(displayed) else arrows[0]
            click_with_message_panel_retry(driver, arrow.click)

            def moved():
                current = _calendar_state(driver)
                return current if current and _months(current) != displayed else None

            state = _wait(moved, timeout, "日历翻月后未刷新年月，已停止筛选")
        for day in (1, last_day):
            state = _wait(lambda: _calendar_state(driver), timeout, "选择日期后日历意外关闭")
            panes = [pane for pane in state["panes"] if (pane["year"], pane["month"]) == target]
            choices = [element for pane in panes for element in pane["days"].get(str(day), [])]
            if len(choices) != 1:
                raise TemuCalendarError(f"无法唯一选择 {month}-{day:02d}，日期可能被禁用")
            click_with_message_panel_retry(driver, choices[0].click)
        _wait(lambda: _date_pair(_range_input(driver).get_attribute("value")) == (start, end), timeout,
              "日历未回显完整月份范围，不能继续查询")
    return {"month": month, "start_date": start, "end_date": end, "range_value": _range_input(driver).get_attribute("value")}


def query_and_wait_for_refresh(driver, summary_selector: str, *, expected_range: str | None = None,
                               timeout: float = 30, settle_ms: int = 1000,
                               loading_selector: str = DEFAULT_LOADING_SELECTOR) -> dict:
    """Submit 查询 and require a new stable summary or an observed loading cycle.

    Only DOM mutations are observed. No fetch/XHR wrappers, endpoints or browser
    performance hooks are installed. Unchanged values are accepted only if the
    result panel changed or loading was observed; a silent query times out.
    """
    if not summary_selector or settle_ms < 0 or timeout <= 0:
        raise TemuCalendarError("查询刷新校验参数无效")
    key = "__hqyl_balance_refresh_" + uuid4().hex
    try:
        def submit():
            # A confirmed intercepted click did not submit the query. Reinstall
            # its observer and re-resolve the button after closing the panel.
            driver.execute_script(REFRESH_CLEANUP, key)
            installed = driver.execute_script(REFRESH_INSTALL, key, summary_selector, expected_range or "", loading_selector)
            if installed.get("error"):
                raise TemuCalendarError(installed["error"])
            installed["button"].click()

        click_with_message_panel_retry(driver, submit)

        def refreshed():
            state = driver.execute_script(REFRESH_READ, key)
            if state.get("error"):
                raise TemuCalendarError(state["error"])
            observed = state.get("mutations", 0) > 0 or state.get("saw_loading", False)
            return state if state.get("armed") and observed and state.get("unique") and not state.get("loading") and state.get("stable_ms", 0) >= settle_ms else None

        state = _wait(refreshed, timeout, "查询后未能确认汇总面板刷新，已拒绝使用筛选前金额")
        return {"range_value": state["range_value"], "text": state["text"],
                "changed": state["text"] != state["before_text"], "saw_loading": state["saw_loading"], "mutations": state["mutations"]}
    finally:
        driver.execute_script(REFRESH_CLEANUP, key)
