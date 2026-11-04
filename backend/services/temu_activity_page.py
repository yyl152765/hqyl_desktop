"""Drive the TEMU seller backend to export the activity registration template.

Every control is resolved from the live DOM by its visible label and must be unique;
no TEMU business endpoint is guessed.  Page-level confirmations (activity types,
clearance topics, selected product count) are recorded as *evidence* and are allowed
to be unverified, because the mandatory guard is the downloaded file itself: sheet
names, headers, site coverage and row counts are checked by
``temu_activity_workbook.inspect_export``.
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit
from uuid import uuid4

from backend.core.ziniao_browser import ZiniaoClickInterceptedError
from backend.services.temu_activity_workbook import (
    CLEARANCE_KEYWORDS,
    SITE_DISPLAY,
    SITE_NAMES as SITES_NAMES,
    WorkbookFormatError,
    foreign_sites,
    inspect_export,
)


ACTIVITY_HOST_SUFFIX = "temu.com"
DEFAULT_HOME = "https://agentseller.temu.com/"
MAX_PRODUCT_PAGES = 200

MENU_SALES = ("店铺营销",)
MENU_ACTIVITY = ("营销活动",)
MENU_BATCH = ("批量报名活动", "活动报名", "批量活动报名")
ACTIVITY_TYPES = ("限时秒杀", "官方大促", "专题活动")
PRODUCT_ENTRY = ("选择商品",)
SELECT_ALL = ("全选", "全选商品")
PAGE_SIZE_TRIGGER = ("20条/页", "10条/页", "50条/页", "5条/页", "200条/页")
PAGE_SIZE_TARGET = ("100条/页", "100 条/页")
PAGE_SIZE = PAGE_SIZE_TRIGGER + PAGE_SIZE_TARGET
NEXT_PAGE = ("下一页",)
GENERATE = ("生成模板", "下载模板", "导出模板", "导出报名模板", "生成报名模板", "导出")

LABELS = tuple(dict.fromkeys(
    MENU_SALES + MENU_ACTIVITY + MENU_BATCH + ACTIVITY_TYPES + PRODUCT_ENTRY
    + SELECT_ALL + PAGE_SIZE + NEXT_PAGE + GENERATE
    + tuple(label for aliases in SITE_DISPLAY.values() for label in sorted(aliases))
))


PAGE_STATE = r"""(() => {
  const visible = e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const clean = v => String(v || '').replace(/\s+/g, '');
  const text = e => clean(e.innerText ?? e.textContent);
  const path = e => {
    if (e.id && document.querySelectorAll('#' + CSS.escape(e.id)).length === 1) return '#' + CSS.escape(e.id);
    const parts = [];
    while (e && e.nodeType === 1) {
      const peers = e.parentElement ? [...e.parentElement.children].filter(x => x.tagName === e.tagName) : [e];
      parts.unshift(e.tagName.toLowerCase() + ':nth-of-type(' + (peers.indexOf(e) + 1) + ')');
      e = e.parentElement;
    }
    return parts.join(' > ');
  };
  const selectedOf = node => {
    for (let n = node, i = 0; n && i < 3; n = n.parentElement, i++) {
      if (n.getAttribute('aria-selected') === 'true') return true;
      if (n.getAttribute('aria-checked') === 'true') return true;
      if ([...n.classList].some(c => /^(?:active|selected|is-active|checked)$|(?:^|[-_])(?:tab-active|tab-selected|is-checked)$/.test(c))) return true;
    }
    return false;
  };
  const found = {};
  for (const label of __LABELS__) {
    const nodes = [...document.querySelectorAll('a,button,span,div,li,label,[role="tab"],[role="checkbox"],[role="button"],[role="option"]')]
      .filter(e => visible(e) && text(e) === clean(label));
    const targets = [...new Set(nodes.map(e => e.closest('button,a,label,[role="tab"],[role="checkbox"],[role="button"],[role="option"]') || e))];
    found[label] = targets.map(e => ({
      selector: path(e), text: text(e),
      disabled: e.matches(':disabled') || e.getAttribute('aria-disabled') === 'true',
      selected: selectedOf(e), checked: e.querySelector?.('input[type="checkbox"]')?.checked === true,
    }));
  }
  const checkables = [...document.querySelectorAll('[role="checkbox"],input[type="checkbox"],[role="option"]')]
    .filter(visible).map(e => ({
      selector: path(e),
      text: text(e.closest('label') || e.parentElement || e),
      checked: e.checked === true || e.getAttribute('aria-checked') === 'true',
      disabled: e.matches?.(':disabled') || e.getAttribute('aria-disabled') === 'true',
    }));
  const body = document.body.innerText || '';
  const num = v => Number(String(v).replace(/[^\d]/g, ''));
  // Counters and page-turn detection read the top-most open dialog (the 选择商品 list)
  // when there is one, so unrelated page text such as 「共3个活动」 cannot be mistaken
  // for the product total, and the full text is hashed instead of a truncated slice.
  const dialogSelector = '[role="dialog"],.ant-modal,.el-dialog,.ant-drawer,[data-testid="beast-core-modal"],[data-testid="beast-core-drawer"]';
  const dialogs = [...document.querySelectorAll(dialogSelector)].filter(visible);
  const dialog = dialogs.length ? dialogs[dialogs.length - 1] : null;
  const scopeText = dialog ? (dialog.innerText || '') : body;
  const signature = s => { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return s.length + ':' + h; };
  const total = scopeText.match(/共(?:查询到|有)?\s*([\d,]+)\s*(?:个|条)?\s*(?:商品|SPU|SKU)/)
    || (dialog ? scopeText.match(/共\s*([\d,]+)\s*条/) : null);
  const selected = scopeText.match(/已选(?:择)?\s*(?:商品)?\s*[:：]?\s*([\d,]+)/);
  // Topic checkboxes are the closest checkbox group around the page-level 「全选」, so a
  // filter such as 「仅看清仓商品」 elsewhere on the page is never treated as a topic.
  const topics = (() => {
    const labels = __SELECT_ALL__.map(clean);
    const anchors = [...new Set([...document.querySelectorAll('a,button,span,div,li,label,[role="checkbox"]')]
      .filter(e => visible(e) && labels.includes(text(e)) && !e.closest(dialogSelector))
      .map(e => e.closest('label,[role="checkbox"]') || e))];
    if (anchors.length !== 1) return null;
    const own = anchors[0];
    for (let n = own.parentElement; n && n !== document.body; n = n.parentElement) {
      const boxes = [...n.querySelectorAll('[role="checkbox"],input[type="checkbox"]')]
        .filter(b => visible(b) && !own.contains(b) && !b.contains(own));
      const list = boxes.filter(b => !boxes.some(o => o !== b && b.contains(o)));
      if (list.length) return list.map(e => ({
        selector: path(e),
        text: text(e.closest('label') || e.parentElement || e),
        checked: e.checked === true || e.getAttribute('aria-checked') === 'true',
        disabled: e.matches?.(':disabled') || e.getAttribute('aria-disabled') === 'true',
      }));
    }
    return null;
  })();
  return JSON.stringify({
    url: location.href, host: location.hostname, title: document.title,
    login: /^\/auth(?:\/|$)/.test(location.pathname) || /\/seller-login(?:\/|$)/.test(location.pathname),
    verification: [...document.querySelectorAll('[role="dialog"],.ant-modal,.el-dialog')].filter(visible)
      .some(e => /验证码|安全验证|滑块验证|验证身份/.test(e.textContent)),
    busy: [...document.querySelectorAll('[aria-busy="true"],.ant-spin-spinning,.el-loading-mask,[data-testid="beast-core-loading"]')].some(visible),
    found, checkables, topics,
    counters: {total: total ? num(total[1]) : null, selected: selected ? num(selected[1]) : null},
    scope_sig: signature(scopeText), in_dialog: Boolean(dialog),
    body: body.slice(0, 20000),
  });
})()"""


CAPTURE_START = r"""(() => {
  const key = __KEY__;
  if (window[key]) return JSON.stringify({ok:false});
  const state = {files: [], pending: 0, error: '', urls: new Set(), original: URL.createObjectURL};
  const collect = (blob, name) => {
    if (!(blob instanceof Blob) || blob.size < 4 || blob.size > 512 * 1024 * 1024) return;
    state.pending++;
    blob.arrayBuffer().then(buffer => {
      const bytes = new Uint8Array(buffer);
      if (bytes[0] === 80 && bytes[1] === 75 && bytes[2] === 3 && bytes[3] === 4) state.files.push({bytes, name: name || 'TEMU活动报名模板.xlsx'});
    }).catch(() => {state.error = '读取浏览器导出文件失败';}).finally(() => state.pending--);
  };
  state.wrapper = function(blob) {
    const url = state.original.call(this, blob);
    state.urls.add(url);
    collect(blob, 'TEMU活动报名模板.xlsx');
    return url;
  };
  URL.createObjectURL = state.wrapper;
  state.listener = event => {
    const a = event.target?.closest?.('a[href]');
    if (!a || state.urls.has(a.href)) return;
    let url;
    try { url = new URL(a.href, location.href); } catch (_) { return; }
    if (!(a.download || /\.xlsx(?:$|[?#])/i.test(url.href))) return;
    if (url.protocol !== 'blob:' && url.origin !== location.origin) return;
    state.urls.add(url.href);
    state.pending++;
    fetch(url.href, {credentials: 'same-origin'}).then(r => {if (!r.ok) throw Error(); return r.blob();})
      .then(blob => collect(blob, a.download)).catch(() => {state.error = '读取导出下载链接失败';}).finally(() => state.pending--);
  };
  document.addEventListener('click', state.listener, true);
  window[key] = state;
  return JSON.stringify({ok: true});
})()"""


def page_state_script() -> str:
    return (PAGE_STATE
            .replace("__LABELS__", json.dumps(list(LABELS), ensure_ascii=False))
            .replace("__SELECT_ALL__", json.dumps(list(SELECT_ALL), ensure_ascii=False)))


class ActivityPageError(ValueError):
    """The activity registration page did not match the calibrated structure."""


class TemuActivityGateway:
    def __init__(self, profile: str, *, browser=None, timeout: float = 600):
        self.browser = browser
        if self.browser is None or self.browser.profile != profile:
            raise ValueError("请使用本次所选紫鸟账号创建浏览器会话")
        self.profile = profile
        self.timeout = timeout
        self.current_stores: dict[str, dict] = {}

    @property
    def auth_failed(self) -> bool:
        return self.browser.auth_failed

    @property
    def halted(self) -> bool:
        return self.auth_failed or getattr(self.browser, "cleanup_failed", False) is True

    @property
    def halt_reason(self) -> str:
        if getattr(self.browser, "cleanup_failed", False) is True:
            return "上一店铺未确认关闭，已暂停后续店铺，请关闭窗口后重试"
        return "紫鸟认证已失效，请恢复后重试"

    def preflight(self, stores: list[dict]) -> None:
        if all(store["status"] in {"success", "no_data"} for store in stores):
            return
        self.browser.ready()
        self.current_stores = {store["store_id"]: store for store in self.browser.list_stores()}

    def state(self, store_id: str) -> dict:
        result = self.browser.evaluate(store_id, page_state_script())
        if not isinstance(result, dict) or "found" not in result:
            raise ActivityPageError("无法识别 TEMU 页面结构，请在卖家后台确认页面可正常打开")
        host = result.get("host") or ""
        if not (host == ACTIVITY_HOST_SUFFIX or host.endswith("." + ACTIVITY_HOST_SUFFIX)):
            raise ActivityPageError("当前页面不是 TEMU 卖家后台，请在该店后台重新打开活动报名页")
        if result.get("login") or result.get("verification"):
            raise ActivityPageError("店铺需要重新登录或人工验证，请处理后重试")
        return result

    def wait_state(self, store_id: str, predicate, *, seconds: float = 45, hint: str = "") -> dict:
        deadline = time.monotonic() + seconds
        while True:
            state = self.state(store_id)
            if not state.get("busy") and predicate(state):
                return state
            if time.monotonic() >= deadline:
                raise ActivityPageError(hint or "页面未在等待时间内就绪，请检查活动报名页结构")
            time.sleep(0.8)

    @staticmethod
    def _unique(state: dict, labels: Iterable[str], purpose: str) -> dict:
        for label in labels:
            choices = state["found"].get(label, [])
            if len(choices) == 1 and not choices[0]["disabled"]:
                return choices[0]
        seen = {label: len(state["found"].get(label, [])) for label in labels}
        raise ActivityPageError(f"无法唯一定位{purpose}（{seen}），请核对活动报名页结构")

    def click(self, store_id: str, labels: Iterable[str], purpose: str) -> dict:
        labels = tuple(labels)
        for attempt in range(2):
            state = self.state(store_id)
            target = self._unique(state, labels, purpose)
            try:
                self.browser.click(store_id, target["selector"])
                return target
            except ZiniaoClickInterceptedError:
                if attempt:
                    raise
        raise ActivityPageError(f"{purpose}点击未生效，请检查页面遮挡")

    @staticmethod
    def has_any(state: dict, labels: Iterable[str]) -> bool:
        return any(state["found"].get(label) for label in labels)

    def open_activity_page(self, store: dict) -> dict:
        store_id = store["store_id"]
        actual = self.current_stores.get(store_id)
        if not actual or actual["store_name"] != store["store_name"] or "temu" not in actual["platform_name"].casefold():
            raise ActivityPageError("该店的紫鸟权限、名称或平台信息发生变化，请重新匹配")
        self.browser.open_store(store, url=DEFAULT_HOME)
        state = self.wait_state(store_id, lambda s: bool(s.get("body")), seconds=60,
                                hint="店铺后台未就绪，请检查登录状态")
        if not self.has_any(state, ACTIVITY_TYPES):
            self.click(store_id, MENU_SALES, "「店铺营销」菜单")
            state = self.wait_state(store_id, lambda s: self.has_any(s, MENU_ACTIVITY) or self.has_any(s, ACTIVITY_TYPES))
            if not self.has_any(state, ACTIVITY_TYPES):
                self.click(store_id, MENU_ACTIVITY, "「营销活动」菜单")
                state = self.wait_state(store_id, lambda s: self.has_any(s, MENU_BATCH) or self.has_any(s, ACTIVITY_TYPES))
            if not self.has_any(state, ACTIVITY_TYPES):
                self.click(store_id, MENU_BATCH, "「批量报名活动」入口")
                state = self.wait_state(
                    store_id,
                    lambda s: self.has_any(s, ACTIVITY_TYPES),
                    seconds=60,
                    hint="未进入活动报名页，未找到「限时秒杀／官方大促／专题活动」活动类型",
                )
        return state

    def select_activity_types(self, store_id: str, wanted: Iterable[str] = ACTIVITY_TYPES) -> dict:
        wanted = [label for label in wanted if label]
        state = self.state(store_id)
        clicked, already = [], []
        for label in wanted:
            choices = state["found"].get(label, [])
            if not choices:
                raise ActivityPageError(f"活动类型「{label}」未出现在页面上，请核对活动范围")
            if len(choices) > 1:
                raise ActivityPageError(f"活动类型「{label}」出现多个可点击节点，请核对页面")
            if choices[0]["selected"] or choices[0]["checked"]:
                already.append(label)
                continue
            self.browser.click(store_id, choices[0]["selector"])
            clicked.append(label)
            time.sleep(0.6)
            state = self.state(store_id)
        state = self.wait_state(store_id, lambda s: not s.get("busy"))
        unverified = [
            label for label in wanted
            if not any(
                (item["selected"] or item["checked"])
                for item in state["found"].get(label, [])
            )
        ]
        return {"clicked": clicked, "already_selected": already, "unverified": unverified}

    def exclude_clearance(self, store_id: str) -> dict:
        """Select every topic, then clear the topics whose name contains a clearance keyword."""
        state = self.state(store_id)
        select_all = None
        for label in SELECT_ALL:
            choices = state["found"].get(label, [])
            if len(choices) == 1:
                select_all = choices[0]
                break
        if select_all is None:
            raise ActivityPageError("未找到专题主题「全选」，无法确认专题范围")
        if not select_all["selected"] and not select_all["checked"]:
            self.browser.click(store_id, select_all["selector"])
            state = self.wait_state(store_id, lambda s: not s.get("busy"))
        if state.get("topics") is None:
            raise ActivityPageError("未找到「全选」所在的专题主题列表，无法确认专题范围")
        topics = [item for item in state["topics"] if item["checked"] and item["text"]]
        clearance = [item for item in topics if any(keyword in item["text"] for keyword in CLEARANCE_KEYWORDS)]
        for item in clearance:
            try:
                self.browser.click(store_id, item["selector"])
            except ZiniaoClickInterceptedError:
                raise ActivityPageError(f"清仓主题「{item['text']}」被遮挡，未完成排除") from None
        state = self.wait_state(store_id, lambda s: not s.get("busy"))
        remaining = [item for item in (state.get("topics") or []) if item["checked"] and item["text"]]
        still = sorted({item["text"] for item in remaining if any(k in item["text"] for k in CLEARANCE_KEYWORDS)})
        if still:
            raise ActivityPageError(f"清仓主题未全部排除：{'、'.join(still[:5])}")
        return {
            "selected_before": len(topics),
            "excluded": [item["text"] for item in clearance],
            "selected_after": len(remaining),
            "keywords": list(CLEARANCE_KEYWORDS),
        }

    def select_sites(self, store_id: str, sites: list[str]) -> dict:
        """Tick the site options; one alias per site is enough, the longest wins."""
        clicked, missing, used = [], [], []
        state = self.state(store_id)
        for code in sites:
            target = None
            for label in sorted(SITE_DISPLAY.get(code, ()), key=len, reverse=True):
                choices = [item for item in state["found"].get(label, []) if not item["disabled"]]
                if len(choices) == 1:
                    target = (label, choices[0])
                    break
            if target is None:
                missing.append(code)
                continue
            label, item = target
            used.append(label)
            if not (item["selected"] or item["checked"]):
                self.browser.click(store_id, item["selector"])
                clicked.append(label)
                time.sleep(0.5)
                state = self.state(store_id)
        if missing:
            names = "、".join(SITES_NAMES.get(code, code) for code in missing)
            raise ActivityPageError(f"页面上找不到站点选项：{names}；请核对店铺站点权限与页面结构")
        return {"sites": sites, "clicked": clicked, "labels": used}

    def _set_page_size(self, store_id: str) -> bool:
        """Open the page-size selector and choose 100 rows; False when the page has none."""
        state = self.state(store_id)
        target = None
        for label in PAGE_SIZE_TARGET:
            choices = [item for item in state["found"].get(label, []) if not item["disabled"]]
            if len(choices) == 1:
                target = choices[0]
                break
        trigger = None
        for label in PAGE_SIZE_TRIGGER:
            choices = [item for item in state["found"].get(label, []) if not item["disabled"]]
            if len(choices) == 1 and (target is None or choices[0]["selector"] != target["selector"]):
                trigger = choices[0]
                break
        if target is not None and (target["selected"] or target["checked"]):
            return True
        if trigger is not None:
            self.browser.click(store_id, trigger["selector"])
            self.wait_state(store_id, lambda s: not s.get("busy"))
            state = self.state(store_id)
            for label in PAGE_SIZE_TARGET:
                choices = [item for item in state["found"].get(label, []) if not item["disabled"]]
                if len(choices) == 1:
                    target = choices[0]
                    break
        if target is None:
            return False
        if not (target["selected"] or target["checked"]):
            self.browser.click(store_id, target["selector"])
            self.wait_state(store_id, lambda s: not s.get("busy"))
        state = self.state(store_id)
        return any(
            (item["selected"] or item["checked"])
            for label in PAGE_SIZE_TARGET
            for item in state["found"].get(label, [])
        )

    def select_all_products(self, store_id: str) -> dict:
        """Open 选择商品, switch to 100 rows per page and select every page."""
        self.click(store_id, PRODUCT_ENTRY, "「选择商品」")
        state = self.wait_state(
            store_id,
            lambda s: self.has_any(s, SELECT_ALL),
            seconds=60,
            hint="「选择商品」未打开，请确认页面弹窗结构",
        )
        page_size_set = self._set_page_size(store_id)
        pages = 0
        while True:
            state = self.state(store_id)
            target = None
            for label in SELECT_ALL:
                choices = [item for item in state["found"].get(label, []) if not item["disabled"]]
                if len(choices) == 1:
                    target = choices[0]
                    break
            if target is None:
                raise ActivityPageError("商品列表未找到可用的「全选」，无法确认全选范围")
            if not target["selected"] and not target["checked"]:
                self.browser.click(store_id, target["selector"])
                state = self.wait_state(store_id, lambda s: not s.get("busy"))
            pages += 1
            next_page = None
            for label in NEXT_PAGE:
                choices = [item for item in state["found"].get(label, []) if not item["disabled"]]
                if len(choices) == 1:
                    next_page = choices[0]
                    break
            if next_page is None:
                break
            if pages >= MAX_PRODUCT_PAGES:
                raise ActivityPageError(f"商品分页超过 {MAX_PRODUCT_PAGES} 页，请核对分页控件")
            before = state.get("scope_sig")
            self.browser.click(store_id, next_page["selector"])
            # Wait for the list itself to change (and loading to finish) instead of a fixed
            # sleep; the signature covers the whole dialog text, not a truncated slice.
            state = self.wait_state(
                store_id,
                lambda s: bool(s.get("scope_sig")) and s.get("scope_sig") != before,
                seconds=30,
                hint="翻页后页面内容未变化，无法确认已遍历全部商品",
            )
        state = self.state(store_id)
        counters = state.get("counters") or {}
        total, selected = counters.get("total"), counters.get("selected")
        if isinstance(total, int) and isinstance(selected, int) and total != selected:
            raise ActivityPageError(f"已选 {selected} 个商品与平台总数 {total} 不一致，本次不导出")
        return {
            "pages": pages,
            "page_size_100": page_size_set,
            "total": total,
            "selected": selected,
            "count_match": isinstance(total, int) and isinstance(selected, int) and total == selected,
        }

    def export(self, store: dict, raw_dir: Path, *, sites: list[str]) -> dict:
        try:
            return self._export(store, raw_dir, sites=sites)
        finally:
            if getattr(self.browser, "cleanup_failed", False) is not True:
                close = getattr(self.browser, "close_store", None)
                if callable(close):
                    close(store["store_id"])

    def _export(self, store: dict, raw_dir: Path, *, sites: list[str]) -> dict:
        store_id = store["store_id"]
        self.open_activity_page(store)
        types = self.select_activity_types(store_id)
        clearance = self.exclude_clearance(store_id)
        site_evidence = self.select_sites(store_id, sites)
        products = self.select_all_products(store_id)
        path = self.download_template(store_id, raw_dir)
        try:
            report = inspect_export(path)
        except WorkbookFormatError as exc:
            raise ActivityPageError(f"下载文件不是活动报名模板：{exc}") from None
        if report["quote_rows"] <= 0:
            raise ActivityPageError("模板报价行为 0，请核对活动范围与全选结果")
        if report["clearance_rows"]:
            raise ActivityPageError(
                f"导出文件仍包含 {report['clearance_rows']} 条清仓报价，专题排除未生效，请核对活动范围后重试"
            )
        unexpected = foreign_sites(report["sites"], sites)
        if unexpected:
            raise ActivityPageError(
                f"导出文件包含未勾选站点：{'、'.join(unexpected)}；请核对该店站点权限后重新导出"
            )
        missing = [code for code in sites if not any(
            label in SITE_DISPLAY.get(code, set()) for label in report["sites"]
        )]
        if missing:
            raise ActivityPageError(
                f"导出文件缺少勾选站点：{'、'.join(missing)}；本次不生成可提交文件"
            )
        evidence = {
            "seller_sites": site_evidence,
            "activity_types": types,
            "clearance": clearance,
            "products": products,
            "quote_rows": report["quote_rows"],
            "stock_rows": report["stock_rows"],
            "spu_count": report["spu_count"],
            "sku_id_count": report["sku_id_count"],
            "topic_count": report["topic_count"],
            "exported_sites": report["sites"],
        }
        return {"path": str(path), "evidence": evidence, "report": report}

    def download_template(self, store_id: str, raw_dir: Path) -> Path:
        key = "__hqyl_temu_activity_" + uuid4().hex
        key_js = json.dumps(key)
        if self.browser.evaluate(store_id, CAPTURE_START.replace("__KEY__", key_js)).get("ok") is not True:
            raise ActivityPageError("无法开始本次导出文件接收")
        try:
            self.click(store_id, GENERATE, "生成或下载活动报名模板")
            return self._receive(store_id, raw_dir, key)
        finally:
            self.clear_capture(store_id, key)

    def _receive(self, store_id: str, raw_dir: Path, key: str) -> Path:
        key_js = json.dumps(key)
        deadline = time.monotonic() + self.timeout
        while True:
            info = self.browser.evaluate(
                store_id,
                f"JSON.stringify((() => {{ const s=window[{key_js}]; return s ? "
                f"{{files:s.files.map(f=>({{size:f.bytes.length}})),pending:s.pending,error:s.error}} : {{lost:true}}; }})())",
            )
            if info.get("lost"):
                raise ActivityPageError("下载导致页面跳转，当前下载方式需要实机适配；未使用历史下载文件")
            if info.get("error"):
                raise ActivityPageError(info["error"])
            if info.get("files") and not info.get("pending"):
                if len(info["files"]) != 1:
                    raise ActivityPageError("本次导出生成多个文件，需确认平台拆分规则后适配")
                break
            if time.monotonic() >= deadline:
                raise ActivityPageError("未收到本次导出的 Excel，可能需要下载中心或权限验证，请完成实机适配后重试")
            time.sleep(1)

        path = raw_dir / "活动报名模板.xlsx"
        temporary = path.with_suffix(".download")
        size = info["files"][0]["size"]
        try:
            with temporary.open("xb") as stream:
                for start in range(0, size, 512 * 1024):
                    end = min(size, start + 512 * 1024)
                    script = (
                        f"JSON.stringify((() => {{const a=window[{key_js}].files[0].bytes.subarray({start},{end});"
                        "let s='';for(let i=0;i<a.length;i+=8192)s+=String.fromCharCode(...a.subarray(i,i+8192));"
                        "return btoa(s);}})())"
                    )
                    chunk = base64.b64decode(self.browser.evaluate(store_id, script), validate=True)
                    if len(chunk) != end - start:
                        raise ActivityPageError("导出文件传输长度不一致")
                    stream.write(chunk)
            if temporary.stat().st_size != size:
                raise ActivityPageError("导出文件未完整保存")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def clear_capture(self, store_id: str, key: str) -> None:
        key_js = json.dumps(key)
        if self.auth_failed:
            return
        try:
            self.browser.evaluate(
                store_id,
                f"JSON.stringify((() => {{const s=window[{key_js}];if(s){{"
                "if(URL.createObjectURL===s.wrapper)URL.createObjectURL=s.original;"
                f"document.removeEventListener('click',s.listener,true);delete window[{key_js}];}}return true;}})())",
            )
        except Exception:  # noqa: BLE001 - cleanup must never mask the export result
            pass

    def inspect(self, store: dict, sites: list[str]) -> dict:
        """Read-only page probe used to calibrate the label set on a real store."""
        try:
            state = self.open_activity_page(store)
            labels = {
                label: len(state["found"].get(label, []))
                for label in LABELS
                if state["found"].get(label)
            }
            return {
                "url": state.get("url", ""),
                "title": state.get("title", ""),
                "labels": labels,
                "activity_types_found": [label for label in ACTIVITY_TYPES if state["found"].get(label)],
                "counters": state.get("counters"),
                "checkable_count": len(state.get("checkables") or []),
                "sites": sites,
                "body_sample": re.sub(r"\s+", " ", state.get("body") or "")[:1200],
            }
        finally:
            if getattr(self.browser, "cleanup_failed", False) is not True:
                close = getattr(self.browser, "close_store", None)
                if callable(close):
                    close(store["store_id"])
