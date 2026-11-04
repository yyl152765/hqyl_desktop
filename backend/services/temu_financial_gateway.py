"""Verified DOM gateway for the TEMU reconciliation-center financial export."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from backend.services.temu_fee_refund_controls import (
    _evaluate,
    _script,
    _wait,
    click_control,
    dismiss_overlays,
)
from backend.services.temu_financial_download import download_xlsx
from backend.services.temu_financial_controls import (
    CLOCK_STATE,
    ensure_other_filters_default,
    field_state,
    filter_state,
    query_and_wait,
    select_field,
    set_custom_range,
)


BASE_URL = "https://agentseller.temu.com/"
RECONCILIATION_URL = "https://seller.kuajingmaihuo.com/labor/bill"
SELLER_HOSTS = {"agentseller.temu.com", "seller.kuajingmaihuo.com"}
DATE_LABEL = "日期选择"
GLOBAL_DOWNLOAD = "下载财务明细(全球)"
LIST_OPTION = "导出列表"
DETAIL_OPTIONS = ("账务详情", "财务详情", "账务明细", "财务明细")
DEFAULT_FILTERS = {"", "全部", "--", "请选择"}
FAILURE_PATTERN = re.compile(r"导出失败|生成失败|任务失败|下载失败")
REGION_DOWNLOAD_PATTERN = re.compile(r"下载财务明细\((.+)\)")


PAGE_STATE = r'''
const labels=arguments[0], found={};
for(const label of labels){
  const nodes=[...document.querySelectorAll('a,button,span,div,li,[role="tab"],[role="button"]')].filter(e=>visible(e)
    && clean(e.innerText||e.textContent)===clean(label)
    && ![...e.children].some(c=>visible(c)&&clean(c.innerText||c.textContent)===clean(label)));
  let controls=[...new Set(nodes.map(e=>e.closest('button,a,[role="tab"],[role="button"]')||e))];
  if(label==='导出历史'){
    const actions=controls.filter(e=>e.matches('button,[data-testid="beast-core-button-link"]'));
    if(actions.length)controls=actions;
  }
  found[label]=controls
    .map(e=>({selector:path(e),disabled:disabled(e),
      selected:[e,e.parentElement,e.parentElement?.parentElement].filter(Boolean).some(n=>
        n.getAttribute('aria-selected')==='true'
        || [...n.classList].some(c=>/(^|[-_])(active|selected)([-_]|$)/i.test(c)))}));
}
return {url:location.href,found,body:document.body.innerText.slice(0,40000),
  sellers:[...document.querySelectorAll('[class*="account-info_mallInfo__"],._Wrz4-O9w')].filter(visible).map(e=>e.innerText.trim().split('\n')[0].trim()),
  verification:[...document.querySelectorAll('[role="dialog"],[data-testid="beast-core-modal"]')].filter(visible).some(e=>/验证码|安全验证|滑块验证|验证身份/.test(e.innerText||e.textContent)),
  busy:[...document.querySelectorAll('[aria-busy="true"],[data-loading="true"],[data-testid="beast-core-loading"],[data-testid="beast-core-spin"],.ant-spin-spinning,.el-loading-mask')].some(visible)};
'''

HISTORY_STATE = r'''
const shown=e=>{if(!visible(e))return false;const r=e.getBoundingClientRect();
  if(r.right<=0||r.bottom<=0||r.left>=innerWidth||r.top>=innerHeight)return false;
  for(let n=e;n;n=n.parentElement)if(n.getAttribute('aria-hidden')==='true'||n.hasAttribute('inert')||getComputedStyle(n).opacity==='0')return false;
  return true;};
const localNow=()=>{const d=new Date(),p=x=>String(x).padStart(2,'0');
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());};
const text=e=>e.innerText||e.textContent||'';
const historyNodes=[...document.querySelectorAll('[class*="export-history"]')].filter(shown);
let roots=historyNodes.map(e=>e.closest('[data-testid="beast-core-drawer"],[role="dialog"]')||e);
roots=[...new Set(roots)].filter(shown);
if(!roots.length){
  roots=[...document.querySelectorAll('[data-testid="beast-core-drawer"],[data-testid="beast-core-drawer-content"],[role="dialog"]')].filter(shown).filter(e=>{
    const value=text(e);
    return /账务明细生成时间|导出任务生成时间|下载财务明细/.test(value)
      || (/导出/.test(value)&&/暂无数据|暂无记录|暂无导出|刷新/.test(value));
  });
}
if(!roots.length)return {open:false,rows:[],now:localNow()};
const top=roots.filter(e=>!roots.some(other=>other!==e&&other.contains(e)));
if(top.length!==1)return {error:top.length?'导出历史面板不唯一':'无法确认导出历史面板',open:false,rows:[],now:localNow()};
const root=top[0];
let rowNodes=[...root.querySelectorAll('[class*="export-history_list__"]')].filter(visible);
if(!rowNodes.length){
  const hasMarker=e=>/账务明细生成时间|导出任务生成时间/.test(text(e));
  const isCard=e=>visible(e)&&hasMarker(e)&&/账务明细查询时间/.test(text(e))
    && ([...e.querySelectorAll('button,a')].some(b=>clean(text(b)).startsWith('下载'))
      || /导出失败|生成失败|任务失败|正在生成|处理中|导出中|暂无数据/.test(text(e)));
  rowNodes=[...root.querySelectorAll('div,li,section,article')].filter(e=>isCard(e)&&![...e.children].some(isCard));
}
const rows=[...new Set(rowNodes)].map(e=>{
  const value=text(e);
  const generated=(value.match(/(?:账务明细生成时间|导出任务生成时间)\s*[:：]?\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})/)||[])[1]||'';
  const downloads=[...e.querySelectorAll('button,a')].filter(x=>visible(x))
    .filter(x=>clean(x.innerText||x.textContent).startsWith('下载'))
    .map(x=>({selector:path(x),label:String(x.innerText||x.textContent||'').replace(/\s+/g,''),
      disabled:disabled(x)}));
  return {text:value,generated,downloads,selector:path(e)};
});
const refreshLeaves=[...root.querySelectorAll('button,a,span,div')].filter(e=>visible(e)&&clean(e.innerText||e.textContent)==='刷新'
  &&![...e.children].some(c=>visible(c)&&clean(c.innerText||c.textContent)==='刷新'));
const closeNodes=[...root.querySelectorAll('[data-testid="beast-core-icon-close"],[data-testid="beast-core-icon-close-mms-home-modal"],button[aria-label*="关闭"],[aria-label*="关闭"]')].filter(visible);
return {open:true,rows,now:localNow(),empty:/暂无数据|暂无记录|暂无导出/.test(text(root)),
  loading:[...root.querySelectorAll('[aria-busy="true"],[data-loading="true"],[data-testid="beast-core-loading"],[data-testid="beast-core-spin"],.ant-spin-spinning,.el-loading-mask')].some(visible),
  refresh:[...new Set(refreshLeaves.map(e=>e.closest('button,a,[role="button"]')||e))]
    .map(e=>({selector:path(e),disabled:disabled(e)})),
  close:[...new Set(closeNodes.map(e=>e.closest('button,[role="button"]')||e))]
    .map(e=>({selector:path(e),disabled:disabled(e)}))};
'''

MODAL_STATE = r'''
const text=e=>e.innerText||e.textContent||'';
const buttons=e=>[...e.querySelectorAll('button')].filter(visible);
let roots=[...document.querySelectorAll('[role="dialog"],[data-testid="beast-core-modal"],[data-testid*="modal"],[class*="modal"]')]
  .filter(visible).filter(e=>/导出内容|导出列表/.test(text(e))
    && buttons(e).some(b=>['确认','确定'].includes(clean(b.innerText||b.textContent))));
if(!roots.length)return {open:false,options:[],confirm:[]};
const inner=roots.filter(e=>!roots.some(other=>other!==e&&e.contains(other)));
if(inner.length!==1)return {open:false,error:'导出弹窗不唯一',options:[],confirm:[]};
const root=inner[0];
const wanted=['导出列表','账务详情','财务详情','账务明细','财务明细'];
const nodes=[...root.querySelectorAll('label,[role="radio"],[role="checkbox"],button,div,span')].filter(e=>visible(e)
  && wanted.some(label=>clean(text(e)).includes(label))
  && ![...e.children].some(c=>visible(c)&&wanted.some(label=>clean(text(c)).includes(label))));
const options=[...new Set(nodes.map(e=>e.closest('label,[role="radio"],[role="checkbox"],button')||e))].map(e=>{
  const input=e.querySelector('input[type="radio"],input[type="checkbox"]')||(e.matches('input')?e:null);
  const selected=input?Boolean(input.checked):[e,e.parentElement].filter(Boolean).some(n=>
    n.getAttribute('aria-checked')==='true'
    || [...n.classList].some(c=>/(^|[-_])(selected|checked|active)([-_]|$)/i.test(c)));
  return {text:clean(text(e)),selector:path(e),disabled:disabled(e),selected};
});
const confirm=buttons(root).filter(b=>['确认','确定'].includes(clean(b.innerText||b.textContent)))
  .map(b=>({selector:path(b),disabled:disabled(b),text:clean(b.innerText||b.textContent)}));
return {open:true,options,confirm,text:text(root).slice(0,4000)};
'''


def normalize_download_label(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("（", "(").replace("）", ")")


def _compact(value) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _generated(row) -> str:
    text = str(row.get("text") or "")
    match = re.search(
        r"(?:账务明细生成时间|导出任务生成时间)\s*[:：]?\s*"
        r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})",
        text,
    )
    if not match:
        raise ValueError("导出历史卡片缺少可核对的账务明细生成时间")
    return match.group(1)


def parse_query_condition(text: str, start: date, end: date) -> dict:
    """Read and verify the full-day query, type, flow and exported content."""
    raw = str(text or "")
    query = re.search(
        r"账务明细查询时间\s*[:：]?\s*"
        r"(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}(?::\d{2})?))?\s*[~～至]\s*"
        r"(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}(?::\d{2})?))?",
        raw,
    )
    if not query:
        raise ValueError("新导出任务缺少可核对的账务明细查询时间")
    start_date, start_time, end_date, end_time = query.groups()
    if start_date != start.isoformat() or end_date != end.isoformat():
        raise ValueError("新导出任务的查询日期与本次请求不一致")
    if start_time != "00:00:00":
        raise ValueError("新导出任务的查询开始时间不是全天起点")
    if end_time != "23:59:59":
        raise ValueError("新导出任务的查询结束时间不是全天终点")

    kind = re.search(r"账务类型\s*[:：]?\s*([^\s:：]+)", raw)
    if not kind or _compact(kind.group(1)) != "全部":
        raise ValueError("新导出任务不是“全部”账务类型")

    flow = re.search(r"资金流向\s*[:：]?\s*([^\s:：]+)", raw)
    flow_value = _compact(flow.group(1)).replace("—", "--") if flow else ""
    if flow_value not in {"全部", "--", "-"}:
        raise ValueError("新导出任务的资金流向不是全部")

    content = re.search(r"导出内容\s*[:：]?\s*([^\n\r]+)", raw)
    if not content:
        raise ValueError("新导出任务缺少可核对的导出内容")
    tokens = [
        _compact(token).replace("、", "")
        for token in re.split(r"[、,，/]", content.group(1))
        if _compact(token)
    ]
    details = [token for token in tokens if token in DETAIL_OPTIONS]
    if "列表数据" not in tokens or len(details) != 1 or len(tokens) != 2:
        raise ValueError("新导出任务的内容不是“列表数据 + 账务详情”")

    return {
        "start_date": start_date,
        "end_date": end_date,
        "start_time": start_time or "",
        "end_time": end_time or "",
        "account_type": "全部",
        "fund_flow": flow_value,
        "content": "、".join(tokens),
    }


def select_new_task(rows, baseline, submitted_at: datetime, now: datetime, start: date, end: date):
    """Return the single new global-financial card, or None while not ready."""
    if not isinstance(baseline, (set, frozenset)):
        raise ValueError("导出历史基线无效")
    baseline_set = set(baseline)
    missing = [row for row in rows if not _generated_or_empty(row)]
    timestamped = [row for row in rows if _generated_or_empty(row)]
    new = [row for row in timestamped if _generated(row) not in baseline_set]
    for row in new:
        generated = datetime.strptime(_generated(row), "%Y-%m-%d %H:%M:%S")
        if not submitted_at - timedelta(seconds=2) <= generated <= now + timedelta(seconds=2):
            raise ValueError("新导出任务生成时间不在本次提交时间内")
    if missing:
        if new:
            raise ValueError("导出历史同时出现多个无法唯一确认的新任务")
        return None
    if len(new) > 1:
        raise ValueError("同时出现多个新导出任务，无法确认本次财务明细")
    if not new:
        return None
    row = new[0]
    text = str(row.get("text") or "")
    if FAILURE_PATTERN.search(text):
        raise ValueError("平台财务明细导出任务失败")
    condition = parse_query_condition(text, start, end)

    labels = [normalize_download_label(item.get("label")) for item in row.get("downloads", [])]
    globals_ = [item for item, label in zip(row.get("downloads", []), labels) if label == GLOBAL_DOWNLOAD]
    regions = [label for label in labels if REGION_DOWNLOAD_PATTERN.fullmatch(label) and label != GLOBAL_DOWNLOAD]
    if len(globals_) > 1:
        raise ValueError("新导出任务存在多个全球财务明细下载按钮")
    if not globals_:
        if any(not item.get("disabled") for item in row.get("downloads", [])
               if normalize_download_label(item.get("label")) in regions):
            raise ValueError("新导出任务仅提供欧区/美国等非全球下载，不能使用")
        if "已完成" in text:
            raise ValueError("新导出任务未提供全球财务明细下载按钮")
        return None
    button = globals_[0]
    if button.get("disabled"):
        return None
    return {**row, "global_download": button, "condition": condition}


def _generated_or_empty(row) -> str:
    try:
        return _generated(row)
    except ValueError:
        return ""


class TemuFinancialGateway:
    def __init__(self, profile, *, browser, timeout=180):
        if browser is None or browser.profile != profile:
            raise ValueError("请使用本次所选紫鸟账号创建浏览器会话")
        self.profile = profile
        self.browser = browser
        self.timeout = timeout
        self.current_stores = {}

    @property
    def auth_failed(self):
        return self.browser.auth_failed

    @property
    def halted(self):
        return self.auth_failed or getattr(self.browser, "cleanup_failed", False)

    @property
    def halt_reason(self):
        return "店铺未确认关闭或紫鸟认证失效，请检查后重试"

    def preflight(self, stores):
        self.browser.ready()
        self.current_stores = {
            str(store.get("store_id")): store for store in self.browser.list_stores()
        }

    def state(self, store_id, labels=()):
        state = _evaluate(self.browser, store_id, _script(PAGE_STATE, list(labels)))
        if not isinstance(state, dict) or "found" not in state:
            raise ValueError("无法识别 TEMU 页面")
        host = urlsplit(state.get("url", "")).hostname or ""
        if host not in SELLER_HOSTS:
            raise ValueError("当前页面不是 TEMU 卖家后台")
        if re.search(r"/(?:auth|seller-login|login)(?:/|[.?]|$)", urlsplit(state.get("url", "")).path):
            raise ValueError("TEMU 登录已失效，请先登录该店铺后重试")
        if state.get("verification"):
            raise ValueError("TEMU 需要人工验证，请完成验证后重新导出")
        return state

    def wait(self, store_id, predicate, labels=(), seconds=30):
        return _wait(
            lambda: (state if not (state := self.state(store_id, labels)).get("busy") and predicate(state) else None),
            seconds, "页面未就绪或控件未能确认",
        )

    def _navigate(self, identity, url):
        if urlsplit(url).hostname not in SELLER_HOSTS:
            raise ValueError("仅可导航到 TEMU 卖家后台")
        if callable(getattr(self.browser, "navigate", None)):
            self.browser.navigate(identity, url)
        else:
            self.browser._driver(identity).get(url)

    def _click_label(self, identity, label, labels):
        state = self.state(identity, labels)
        choices = state["found"].get(label, [])
        if len(choices) != 1 or choices[0].get("disabled"):
            raise ValueError(f"无法唯一定位可用的“{label}”")
        click_control(self.browser, identity, choices[0]["selector"])

    @staticmethod
    def verify_seller(store, state):
        sellers = [str(item or "").strip() for item in state.get("sellers", [])]
        if len(sellers) != 1 or sellers[0] in {"", "-", "--"}:
            raise ValueError("无法唯一确认当前 TEMU 卖家身份")
        seller = sellers[0]
        store_key = re.sub(r"\s+", "", str(store.get("store_name") or "")).casefold()
        seller_key = re.sub(r"\s+", "", seller).casefold()
        if not (seller_key == store_key or any(store_key.endswith(mark + seller_key) for mark in "-－—–")):
            raise ValueError("当前 TEMU 卖家身份与所选紫鸟店铺不一致，请核对后重试")
        return seller

    @staticmethod
    def _looks_reconciliation(state) -> bool:
        body = state.get("body", "")
        return (
            len(state.get("found", {}).get("查询", [])) == 1
            and ("账务明细查询时间" in body or ("账务类型" in body and "资金流向" in body))
        )

    def _open_reconciliation(self, identity, store):
        # The observed reconciliation entry is hosted by the China seller
        # centre even when the requested report is the global finance detail.
        self._navigate(identity, RECONCILIATION_URL)
        labels = ["账户资金", "对账中心", "查询", "导出", "导出历史", "重置"]
        state = self.wait(
            identity,
            lambda s: len(s.get("sellers", [])) == 1,
            labels, 60,
        )
        seller = self.verify_seller(store, state)
        if not self._looks_reconciliation(state):
            books = state["found"].get("对账中心", [])
            if len(books) != 1:
                self._click_label(identity, "账户资金", labels)
                state = self.wait(
                    identity,
                    lambda s: len(s.get("found", {}).get("对账中心", [])) == 1,
                    labels, 60,
                )
            self._click_label(identity, "对账中心", labels)
            state = self.wait(identity, self._looks_reconciliation, labels, 60)
        self.verify_seller(store, state)
        return seller, state

    def _reset_filters(self, identity):
        state = self.state(identity, ["重置", "查询"])
        resets = state["found"].get("重置", [])
        if len(resets) > 1:
            raise ValueError("财务筛选存在多个“重置”按钮，无法确认")
        if resets:
            if resets[0].get("disabled"):
                raise ValueError("财务筛选“重置”按钮不可用")
            click_control(self.browser, identity, resets[0]["selector"])
            _wait(
                lambda: (s if not (s := self.state(identity)).get("busy") else None),
                30, "财务筛选重置后页面未就绪",
            )

    def _verify_known_filters(self, identity):
        def allowed(label, values):
            try:
                field = field_state(self.browser, identity, label, "select")
            except ValueError:
                body = self.state(identity).get("body", "")
                match = re.search(label + r"\s*[:：]?\s*([^\s:：]+)", body)
                if not match or _compact(match.group(1)).replace("—", "--") not in values:
                    raise ValueError(f"无法确认{label}为全部，请重置筛选后重试") from None
                return
            if not any(
                _compact(field.get(key)).replace("—", "--") in values
                for key in ("value", "text", "placeholder")
            ):
                raise ValueError(f"{label}未确认选择全部，请重置筛选后重试")

        allowed("账务类型", {"", "全部"})
        allowed("资金流向", {"", "全部", "--", "-"})

    def _ensure_filters(self, identity, start: date, end: date):
        ensure_other_filters_default(self.browser, identity, start, end)
        self._verify_known_filters(identity)

    def _history_state(self, identity):
        state = _evaluate(self.browser, identity, _script(HISTORY_STATE))
        if state.get("error"):
            raise ValueError(state["error"])
        return state

    @staticmethod
    def _history_loaded(state):
        if not state.get("open") or state.get("loading"):
            return None
        if state.get("rows") or state.get("empty"):
            return state
        return None

    def _wait_history(self, identity, timeout=None):
        return _wait(
            lambda: self._history_loaded(self._history_state(identity)),
            timeout or 30, "未能读取已加载的财务导出历史",
        )

    def _open_history(self, identity, timeout=None):
        state = self._history_state(identity)
        if state.get("open"):
            return self._wait_history(identity, timeout)
        self._click_label(identity, "导出历史", ["导出历史"])
        return self._wait_history(identity, timeout)

    def _close_history(self, identity):
        state = self._history_state(identity)
        if not state.get("open"):
            return
        closes = [item for item in state.get("close", []) if not item.get("disabled")]
        if len(closes) == 1:
            click_control(self.browser, identity, closes[0]["selector"])
        else:
            choices = self.state(identity, ["导出历史"])["found"].get("导出历史", [])
            if len(choices) != 1 or choices[0].get("disabled"):
                raise ValueError("无法关闭导出历史面板，不能开始本次导出")
            click_control(self.browser, identity, choices[0]["selector"])
        _wait(
            lambda: not (s := self._history_state(identity)).get("open"),
            30, "导出历史面板未关闭",
        )

    def _baseline(self, identity):
        state = self._open_history(identity)
        generated = [_generated(row) for row in state.get("rows", [])]
        if len(generated) != len(set(generated)):
            raise ValueError("现有导出任务生成时间重复，无法安全关联新任务")
        return set(generated)

    def _modal_state(self, identity):
        state = _evaluate(self.browser, identity, _script(MODAL_STATE))
        if state.get("error"):
            raise ValueError(state["error"])
        return state

    def _open_export_modal(self, identity):
        self._click_label(identity, "导出", ["导出"])

        def ready():
            state = self._modal_state(identity)
            if not state.get("open"):
                return None
            options = state.get("options", [])
            if not any("导出列表" in item.get("text", "") for item in options):
                return None
            if not any(any(label in item.get("text", "") for label in DETAIL_OPTIONS) for item in options):
                return None
            return state

        return _wait(ready, 30, "导出弹窗未出现列表数据与账务详情选项")

    @staticmethod
    def _pick_option(options, labels):
        for label in labels:
            exact = [item for item in options if item.get("text") == label]
            if exact:
                if len(exact) != 1:
                    raise ValueError(f"导出弹窗存在多个“{label}”选项")
                return exact[0]
        for label in labels:
            partial = [item for item in options if label in item.get("text", "")]
            if len(partial) == 1:
                return partial[0]
            if len(partial) > 1:
                raise ValueError(f"导出弹窗存在多个“{label}”选项")
        raise ValueError(f"导出弹窗缺少“{labels[0]}”选项")

    def _select_export_options(self, identity):
        self._open_export_modal(identity)

        def combined():
            state = self._modal_state(identity)
            if not state.get("open"):
                raise ValueError("选择导出内容时弹窗意外关闭")
            choices = [item for item in state.get("options", [])
                       if LIST_OPTION in item.get("text", "")
                       and any(detail in item.get("text", "") for detail in DETAIL_OPTIONS)]
            if len(choices) != 1 or choices[0].get("disabled"):
                raise ValueError("无法唯一确认“导出列表 + 账务详情”组合选项")
            return state, choices[0]

        state, option = combined()
        if not option.get("selected"):
            click_control(self.browser, identity, option["selector"])

        def selected():
            state, option = combined()
            return state if option.get("selected") else None

        return _wait(selected, 15, "未能确认“导出列表 + 账务详情”已选中")

    def _confirm_export(self, identity):
        state = self._modal_state(identity)
        if not state.get("open"):
            raise ValueError("确认前导出弹窗已关闭")
        confirms = [item for item in state.get("confirm", []) if not item.get("disabled")]
        if len(confirms) != 1:
            raise ValueError("无法唯一定位可用的导出确认按钮")
        click_control(self.browser, identity, confirms[0]["selector"])
        _wait(
            lambda: True if not self._modal_state(identity).get("open") else None,
            30, "确认导出后弹窗未关闭",
        )

    def _refresh_history_if_possible(self, identity, state):
        refresh = [item for item in state.get("refresh", []) if not item.get("disabled")]
        if len(refresh) == 1:
            click_control(self.browser, identity, refresh[0]["selector"])

    def _wait_for_new_task(self, identity, baseline, submitted, start, end):
        self._open_history(identity)
        next_refresh = time.monotonic() + 3

        def completed():
            nonlocal next_refresh
            state = self._history_state(identity)
            if not state.get("open"):
                choices = self.state(identity, ["导出历史"])["found"].get("导出历史", [])
                if len(choices) == 1 and not choices[0].get("disabled"):
                    click_control(self.browser, identity, choices[0]["selector"])
                return None
            if state.get("loading"):
                return None
            loaded = self._history_loaded(state)
            if loaded is None:
                return None
            now = datetime.strptime(loaded["now"], "%Y-%m-%d %H:%M:%S")
            row = select_new_task(loaded.get("rows", []), baseline, submitted, now, start, end)
            if row:
                return row
            if time.monotonic() >= next_refresh:
                self._refresh_history_if_possible(identity, loaded)
                next_refresh = time.monotonic() + 3
            return None

        return _wait(completed, self.timeout, "未能确认本次财务明细导出任务完成")

    def export(self, store, start_date, end_date, raw_dir):
        try:
            start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        except (ValueError, TypeError):
            raise ValueError("日期必须为 YYYY-MM-DD") from None
        if start > end:
            raise ValueError("开始日期不能晚于结束日期")
        actual = self.current_stores.get(store["store_id"])
        if not actual or actual["store_name"] != store["store_name"] or "temu" not in str(actual.get("platform_name") or "").casefold():
            raise ValueError("店铺权限、名称或平台发生变化，请重新匹配")
        identity = store["store_id"]
        export_error = None
        try:
            self.browser.open_store(store, url=BASE_URL)
            seller, _ = self._open_reconciliation(identity, store)
            self._reset_filters(identity)
            set_custom_range(self.browser, identity, DATE_LABEL, start, end)
            self._ensure_filters(identity, start, end)
            baseline = self._baseline(identity)
            try:
                self._close_history(identity)
            except ValueError:
                # A full-page history can hide the export toolbar. Reloading the
                # reconciliation page is safe because the history baseline has
                # already been captured for new-task matching.
                seller, _ = self._open_reconciliation(identity, store)
                self._reset_filters(identity)
                set_custom_range(self.browser, identity, DATE_LABEL, start, end)
            self._ensure_filters(identity, start, end)
            query_and_wait(self.browser, identity)
            self.verify_seller(store, self.state(identity))
            self._select_export_options(identity)
            submitted_text = _evaluate(self.browser, identity, CLOCK_STATE)
            submitted = datetime.strptime(submitted_text, "%Y-%m-%d %H:%M:%S")
            self._confirm_export(identity)
            row = self._wait_for_new_task(identity, baseline, submitted, start, end)
            path = download_xlsx(
                self.browser, identity, row["global_download"]["selector"], raw_dir,
                filename="财务明细_全球.xlsx", timeout=self.timeout, expected_seller=seller,
            )
            return {
                "path": str(path),
                "evidence": {
                    "seller": seller,
                    "submitted_at": submitted_text,
                    "task_generated_at": row["generated"],
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "query": row["condition"],
                    "download_label": normalize_download_label(
                        row["global_download"]["label"]
                    ),
                },
            }
        except Exception as exc:
            export_error = exc
            raise
        finally:
            if getattr(self.browser, "cleanup_failed", False) is not True:
                try:
                    self.browser.close_store(identity)
                except Exception as close_error:
                    if export_error is not None:
                        raise ValueError(f"{export_error}；清理店铺时：{close_error}") from None
                    raise
