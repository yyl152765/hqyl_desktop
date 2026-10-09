from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from backend.core.ziniao_browser import ZiniaoClickInterceptedError
from backend.services.temu_on_sale_workbook import inspect_source, iter_source_rows


# Locate by the labels in the supplied export screenshot. Every click is resolved
# from the current DOM and must be unique. No TEMU business endpoint is guessed.
PAGE_STATE = r"""(() => {
  const visible = e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const clean = v => String(v || '').replace(/\s+/g, '').trim();
  const labelText = e => clean(e.innerText ?? e.textContent);
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
  const labels = ['商品管理', '商品列表', '重置', '查询', '展开', '在售中', '下载查询结果'];
  const found = {};
  for (const label of labels) {
    const re = new RegExp('^' + label + '(?:[（(]?[\\d,，]+[）)]?)?$');
    const leaves = [...document.querySelectorAll('a,button,span,div,li,[role="tab"],[role="button"]')]
      .filter(e => visible(e) && re.test(labelText(e)) && ![...e.children].some(c => visible(c) && re.test(labelText(c))));
    const targets = [...new Set(leaves.map(e => e.closest('button,a,[role="tab"],[role="button"]') || e))];
    found[label] = targets.map(e => {
      let selected = false, disabled = false;
      for (let node = e, i = 0; node && i < 3; node = node.parentElement, i++) {
        selected ||= node.getAttribute('aria-selected') === 'true' || [...node.classList].some(c => /^(?:active|selected|is-active)$|(?:^|[-_])(?:tab-active|tab-selected)$|^TAB_active_/.test(c));
        disabled ||= node.matches(':disabled') || node.getAttribute('aria-disabled') === 'true';
      }
      return {selector: path(e), text: labelText(e), selected, disabled};
    });
  }
  const reset = found['重置'].length === 1 ? document.querySelector(found['重置'][0].selector) : null;
  let form = reset;
  while (form && !form.querySelector('input,select,textarea,[role="combobox"]')) form = form.parentElement;
  const filters = form ? [...form.querySelectorAll('input,select,textarea,[role="combobox"]')].filter(visible).map(e => ({
    tag: e.tagName, type: e.type || '', value: e.value || '', text: e.tagName === 'SELECT' ? e.options[e.selectedIndex]?.text || '' : '',
    role: e.getAttribute('role') || '', label: e.getAttribute('aria-label') || e.placeholder || '',
    control: e.getAttribute('data-testid') || '', readOnly: Boolean(e.readOnly),
    checked: Boolean(e.checked), selectedText: e.getAttribute('role') === 'combobox' ? e.textContent.trim() :
      (e.closest('[data-testid="beast-core-select-header"]')?.innerText || '').trim(),
  })) : null;
  const quickCards = [...document.querySelectorAll('[class*="use-quick-field_tsx_card__"]')].filter(visible);
  return JSON.stringify({url: location.href, title: document.title, found, filters,
    quick_filters_clear: quickCards.length > 0 && quickCards.every(e => e.classList.length === 1),
    sellers: [...document.querySelectorAll('[class*="account-info_mallInfo__"]')].filter(visible).map(e => e.innerText.trim()),
    busy: [...document.querySelectorAll('[aria-busy="true"],.ant-spin-spinning,.el-loading-mask')].some(visible),
    login: /^\/auth(?:\/|$)/.test(location.pathname) || /\/seller-login(?:\/|$)/.test(location.pathname),
    verification: [...document.querySelectorAll('[role="dialog"],.ant-modal,.el-dialog')].filter(visible).some(e => /验证码|安全验证|滑块验证|验证身份/.test(e.textContent)),
    body: document.body.innerText.slice(0, 16000)});
})()"""


EXPORT_DIALOG = r"""(() => {
  const visible = e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const path = e => { const parts=[]; while(e && e.nodeType===1) {
    const peers=e.parentElement?[...e.parentElement.children].filter(x=>x.tagName===e.tagName):[e];
    parts.unshift(e.tagName.toLowerCase()+':nth-of-type('+(peers.indexOf(e)+1)+')'); e=e.parentElement;
  } return parts.join(' > '); };
  const modals=[...document.querySelectorAll('[data-testid="beast-core-modal"]')].filter(visible);
  const found=modals.filter(e=>e.innerText.trim().startsWith('下载查询结果'));
  const notices=modals.filter(e=>e.innerText.trim().startsWith('您有以下待办任务需要尽快处理'));
  const closeButtons=notices.map(e=>e.querySelector('[data-testid="beast-core-modal-icon-close"]')).filter(Boolean);
  for (const title of [...document.querySelectorAll('[class*="modal-todo-alert_title__"]')].filter(visible)) {
    if (title.innerText.trim()!=='您有以下待办任务需要尽快处理') continue;
    let parent=title.parentElement;
    for(let depth=0;parent && parent!==document.body && depth<5;depth++,parent=parent.parentElement) {
      const buttons=[...parent.querySelectorAll('[data-testid="beast-core-modal-icon-close"]')].filter(visible);
      if(buttons.length===1) {closeButtons.push(buttons[0]);break;}
      if(buttons.length>1) break;
    }
  }
  return JSON.stringify({notices:[...new Set(closeButtons)].map(path),
    dialogs:found.map(e=>({text:e.innerText, skcCount:Number(e.innerText.match(/共查询到([\d,]+)个SKC/)?.[1].replace(/,/g,'')||0),
      fields:[...e.querySelectorAll('label')].map(l=>({text:l.innerText.trim(),checked:Boolean(l.querySelector('input')?.checked),selector:path(l)})),
      buttons:[...e.querySelectorAll('button')].filter(visible).map(b=>({text:b.innerText.trim(),disabled:b.disabled,selector:path(b)}))}))});
})()"""


CAPTURE_START = r"""(() => {
  const key = __KEY__;
  if (window[key]) return JSON.stringify({ok:false});
  const state = {files: [], pending: 0, error: '', urls: new Set(), original: URL.createObjectURL};
  const collect = (blob, name) => {
    if (!(blob instanceof Blob) || blob.size < 4 || blob.size > 128 * 1024 * 1024) return;
    state.pending++;
    blob.arrayBuffer().then(buffer => {
      const bytes = new Uint8Array(buffer);
      if (bytes[0] === 80 && bytes[1] === 75 && bytes[2] === 3 && bytes[3] === 4) state.files.push({bytes, name: name || '商品基础信息.xlsx'});
    }).catch(() => {state.error = '读取浏览器导出文件失败';}).finally(() => state.pending--);
  };
  state.wrapper = function(blob) {
    const url = state.original.call(this, blob);
    state.urls.add(url);
    collect(blob, '商品基础信息.xlsx');
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


class TemuOnSaleGateway:
    def __init__(self, profile: str, *, browser=None, cli=None, timeout: float = 300):
        # cli is retained only as an injectable transport for existing fixtures.
        self.browser = browser if browser is not None else cli
        if self.browser is None or self.browser.profile != profile:
            raise ValueError("请使用本次所选紫鸟账号创建浏览器会话")
        self.profile = profile
        self.timeout = timeout
        self.current_stores = {}

    @property
    def auth_failed(self):
        return self.browser.auth_failed

    @property
    def halted(self):
        return self.auth_failed or getattr(self.browser, "cleanup_failed", False) is True

    @property
    def halt_reason(self):
        if getattr(self.browser, "cleanup_failed", False) is True:
            return "上一店铺未确认关闭，已暂停后续店铺，请关闭窗口后重试"
        return "紫鸟认证已失效，请恢复后重试"

    def preflight(self, stores: list[dict]):
        if all(store["status"] in {"success", "no_data"} for store in stores):
            return
        self.browser.ready()
        self.current_stores = {store["store_id"]: store for store in self.browser.list_stores()}

    def state(self, store_id: str) -> dict:
        result = self.browser.evaluate(store_id, PAGE_STATE)
        if not isinstance(result, dict) or "found" not in result:
            raise ValueError("无法识别商品列表页面结构")
        hostname = urlsplit(result.get("url") or "").hostname or ""
        if not (hostname == "temu.com" or hostname.endswith(".temu.com")):
            raise ValueError("当前页面不是 TEMU 卖家后台，请打开该店的卖家后台后重试")
        if result.get("login") or result.get("verification"):
            raise ValueError("店铺需要重新登录或人工验证，请处理后重试")
        return result

    def wait_state(self, store_id: str, predicate, *, seconds: float = 45) -> dict:
        deadline = time.monotonic() + seconds
        while True:
            state = self.state(store_id)
            if not state.get("busy") and predicate(state):
                return state
            if time.monotonic() >= deadline:
                raise ValueError("页面条件未在等待时间内就绪，请检查登录状态、筛选控件或页面变化")
            time.sleep(0.8)

    def click_label(self, store_id: str, label: str):
        for attempt in range(2):
            self.dismiss_notices(store_id)
            state = self.state(store_id)
            candidates = state["found"].get(label, [])
            if len(candidates) != 1 or candidates[0].get("disabled"):
                raise ValueError(f"无法唯一定位可用的“{label}”，请检查页面或关闭遮挡弹窗")
            try:
                self.browser.click(store_id, candidates[0]["selector"])
                return
            except ZiniaoClickInterceptedError:
                # The asynchronous todo notice can appear after the last DOM
                # read. Retry only a confirmed unperformed click, after closing
                # this known notice; never retry an uncertain export submission.
                if attempt or not self.dismiss_notices(store_id):
                    raise

    def dismiss_notices(self, store_id: str):
        dismissed = False
        for _ in range(3):
            notices = self.browser.evaluate(store_id, EXPORT_DIALOG).get('notices', [])
            if not notices:
                return dismissed
            if len(notices) != 1:
                raise ValueError('存在多个待办提示弹窗，请检查页面后重试')
            self.browser.click(store_id, notices[0])
            dismissed = True
            time.sleep(.25)
        raise ValueError('待办提示弹窗未关闭，请检查页面后重试')

    @staticmethod
    def filters_clear(state: dict) -> bool:
        filters = state.get("filters")
        if not filters or state.get("quick_filters_clear") is False:
            return False
        defaults = {"", "全部", "请选择", "SKC", "SPU", "SKU", "SKU(精准查询)", "SKU（精准查询）", "包含", "不包含"}
        for item in filters:
            if item.get("control") == "beast-core-select-htmlInput" and item.get("selectedText", "").strip() not in defaults:
                return False
            if item.get("type") in {"checkbox", "radio"}:
                if item.get("checked"):
                    return False
            elif item.get("tag") == "SELECT":
                if item.get("text", "").strip() not in defaults:
                    return False
            elif item.get("role") == "combobox":
                if item.get("value", "").strip() not in defaults or item.get("selectedText", "").strip() not in defaults:
                    return False
            elif item.get("readOnly") and item.get("control") == "beast-core-select-htmlInput":
                if item.get("value", "").strip() not in defaults:
                    return False
            elif str(item.get("value") or "").strip():
                return False
        return True

    @staticmethod
    def on_sale(state: dict) -> bool:
        tabs = state["found"].get("在售中", [])
        return len(tabs) == 1 and tabs[0].get("selected") is True

    @staticmethod
    def query_ready(state: dict) -> bool:
        if not TemuOnSaleGateway.on_sale(state) or not TemuOnSaleGateway.filters_clear(state):
            return False
        downloads = state["found"].get("下载查询结果", [])
        return len(downloads) == 1 and re.sub(r"\D", "", downloads[0]["text"]) == re.sub(r"\D", "", state["found"]["在售中"][0]["text"])

    @staticmethod
    def verify_seller(store: dict, state: dict) -> str:
        sellers = state.get("sellers", [])
        if len(sellers) != 1 or not sellers[0] or not store["store_name"].endswith("-" + sellers[0]):
            raise ValueError("TEMU 当前卖家名与紫鸟店铺名末尾不一致，需核对卖家身份后重试")
        return sellers[0]

    def confirm_export(self, store_id: str, expected_skc: int):
        deadline = time.monotonic() + 20
        while True:
            data = self.browser.evaluate(store_id, EXPORT_DIALOG)
            if data["dialogs"]:
                break
            if time.monotonic() >= deadline:
                raise ValueError("没有出现下载查询结果的字段选择弹窗")
            time.sleep(0.8)
        if len(data["dialogs"]) != 1:
            raise ValueError("存在多个导出弹窗，不能确认本次下载范围")
        dialog = data["dialogs"][0]
        if dialog["skcCount"] != expected_skc or expected_skc > 40000:
            raise ValueError("导出弹窗数量与在售查询不一致或超过平台 40,000 条上限")
        fields = dialog["fields"]
        required = {"商品标题", "SPU ID", "SKC ID", "SKU ID", "商品状态", "申报价格", "库存"}
        if not required.issubset({item["text"] for item in fields}):
            raise ValueError("导出字段模板发生变化，缺少必要商品字段")
        if not all(item["checked"] for item in fields):
            all_fields = [item for item in fields if item["text"] == "全部商品信息"]
            if len(all_fields) != 1:
                raise ValueError("无法定位全部商品信息选项")
            self.browser.click(store_id, all_fields[0]["selector"])
            data = self.browser.evaluate(store_id, EXPORT_DIALOG)
            if len(data["dialogs"]) != 1:
                raise ValueError("导出弹窗状态发生变化")
            dialog = data["dialogs"][0]
            if not dialog["fields"] or not all(item["checked"] for item in dialog["fields"]):
                raise ValueError("无法确认全部导出字段已勾选")
        buttons = [item for item in dialog["buttons"] if item["text"] == "导出" and not item["disabled"]]
        if len(buttons) != 1:
            raise ValueError("无法唯一定位导出确认按钮")
        self.browser.click(store_id, buttons[0]["selector"])

    def export(self, store: dict, raw_dir: Path) -> dict:
        try:
            return self._export(store, raw_dir)
        finally:
            if getattr(self.browser, "cleanup_failed", False) is not True:
                close = getattr(self.browser, "close_store", None)
                if callable(close):
                    close(store["store_id"])

    def _export(self, store: dict, raw_dir: Path) -> dict:
        store_id = store["store_id"]
        actual = self.current_stores.get(store_id)
        if not actual or actual["store_name"] != store["store_name"] or "temu" not in actual["platform_name"].casefold():
            raise ValueError("该店的紫鸟权限、名称或平台信息发生变化，请重新匹配")
        self.browser.open_store(store, url="https://agentseller.temu.com/goods/list")
        state = self.wait_state(store_id, lambda state: (
            bool(state["found"].get("商品管理") or state["found"].get("商品列表"))
            and len(state.get('sellers', [])) == 1 and bool(state['sellers'][0])
            and store['store_name'].endswith('-' + state['sellers'][0])))
        seller = self.verify_seller(store, state)
        if not state["found"].get("商品列表"):
            self.click_label(store_id, "商品管理")
            self.wait_state(store_id, lambda state: bool(state["found"].get("商品列表")))
        if urlsplit(state["url"]).path != "/goods/list":
            self.click_label(store_id, "商品列表")
        self.wait_state(store_id, lambda state: bool(state["found"].get("重置") and state["found"].get("在售中")))
        state = self.state(store_id)
        if state["found"].get("展开"):
            self.click_label(store_id, "展开")
        self.click_label(store_id, "重置")
        self.wait_state(store_id, self.filters_clear)
        self.click_label(store_id, "在售中")
        self.wait_state(store_id, self.query_ready)
        self.click_label(store_id, "查询")
        state = self.wait_state(store_id, self.query_ready)
        self.verify_seller(store, state)
        # Require both the active tab's explicit zero and a separate empty-result indication.
        tab_text = state["found"]["在售中"][0]["text"]
        if tab_text in {"在售中0", "在售中(0)", "在售中（0）"} and any(text in state.get("body", "") for text in ("暂无数据", "暂无商品", "没有商品")):
            return {"no_data": True, "evidence": {"tab": tab_text, "filters_clear": True, "empty_result": True}}
        expected_skc = int(re.sub(r"\D", "", tab_text))
        if expected_skc > 40000:
            raise ValueError("在售查询超过平台 40,000 条导出上限，不能截取前部分冒充完整结果")
        # Capture the bytes of the *new* browser-generated file. Old filesystem downloads
        # cannot enter this batch. Native/download-center variants must be calibrated live.
        key = "__hqyl_temu_export_" + uuid4().hex
        key_js = json.dumps(key)
        if self.browser.evaluate(store_id, CAPTURE_START.replace("__KEY__", key_js)).get("ok") is not True:
            raise ValueError("无法开始本次导出文件接收")
        try:
            self.click_label(store_id, "下载查询结果")
            self.confirm_export(store_id, expected_skc)
        except Exception:
            self.clear_capture(store_id, key)
            raise
        return self.receive_capture(store_id, raw_dir, key, expected_skc=expected_skc, seller=seller)

    def receive_capture(self, store_id: str, raw_dir: Path, key: str, *, expected_skc: int, seller: str) -> dict:
        key_js = json.dumps(key)
        try:
            deadline = time.monotonic() + self.timeout
            while True:
                info = self.browser.evaluate(store_id, f"JSON.stringify((() => {{ const s=window[{key_js}]; return s ? {{files:s.files.map(f=>({{size:f.bytes.length}})),pending:s.pending,error:s.error}} : {{lost:true}}; }})())")
                if info.get("lost"):
                    raise ValueError("下载导致页面跳转，当前下载方式需要实机适配；未使用历史下载文件")
                if info.get("error"):
                    raise ValueError(info["error"])
                if info.get("files") and not info.get("pending"):
                    if len(info["files"]) != 1:
                        raise ValueError("本次查询生成多个文件，需确认平台拆分规则后适配")
                    break
                if time.monotonic() >= deadline:
                    raise ValueError("未收到本次完整 Excel。可能需要下载中心、原生下载或权限验证，请完成实机适配后重试")
                time.sleep(1)
            path = raw_dir / "商品基础信息.xlsx"
            temporary = path.with_suffix(".download")
            size = info["files"][0]["size"]
            try:
                with temporary.open("xb") as stream:
                    for start in range(0, size, 512 * 1024):
                        end = min(size, start + 512 * 1024)
                        script = f"JSON.stringify((() => {{const a=window[{key_js}].files[0].bytes.subarray({start},{end});let s='';for(let i=0;i<a.length;i+=8192)s+=String.fromCharCode(...a.subarray(i,i+8192));return btoa(s);}})())"
                        chunk = base64.b64decode(self.browser.evaluate(store_id, script), validate=True)
                        if len(chunk) != end - start:
                            raise ValueError("导出文件传输长度不一致")
                        stream.write(chunk)
                if temporary.stat().st_size != size:
                    raise ValueError("导出文件未完整保存")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            source = inspect_source(path)
            skcs = {row["SKC ID"] for row in iter_source_rows(source)}
            if source.row_count >= 40000 or len(skcs) != expected_skc:
                raise ValueError(f"导出完整性未通过：查询 {expected_skc} 个 SKC，文件 {len(skcs)} 个 SKC / {source.row_count} 行；需核对平台导出上限")
            return {"path": str(path), "evidence": {"seller": seller, "query_skc_count": expected_skc,
                    "export_skc_count": len(skcs), "export_rows": source.row_count,
                    "all_fields_selected": True, "filters_clear": True, "on_sale": True}}
        finally:
            self.clear_capture(store_id, key)

    def clear_capture(self, store_id: str, key: str):
        key_js = json.dumps(key)
        if not self.auth_failed:
            try:
                self.browser.evaluate(store_id, f"JSON.stringify((() => {{const s=window[{key_js}];if(s){{if(URL.createObjectURL===s.wrapper)URL.createObjectURL=s.original;document.removeEventListener('click',s.listener,true);delete window[{key_js}];}}return true;}})())")
            except Exception:
                pass
