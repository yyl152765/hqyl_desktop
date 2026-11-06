"""Page interaction and country adapter for Lazada Ads Monthly Report.

Implements page navigation, login handling, date range selection,
Spend cards extraction with exact title resolution, and coupon filter submission.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from backend.services.lazada_ads_monthly_download import _parse_decimal

from backend.core.ziniao_cli import ZiniaoCliError
from backend.services.lazada_ads_monthly_cli import ZiniaoMonthlyCli

logger = logging.getLogger(__name__)

SITE_PROFILES: dict[str, dict[str, Any]] = {
    "PH": {
        "country": "PH",
        "name": "菲律宾",
        "currency": "PHP",
        "host": "sellercenter.lazada.com.ph",
        "timezone": "Asia/Manila",
    },
    "MY": {
        "country": "MY",
        "name": "马来西亚",
        "currency": "MYR",
        "host": "sellercenter.lazada.com.my",
        "timezone": "Asia/Kuala_Lumpur",
    },
    "TH": {
        "country": "TH",
        "name": "泰国",
        "currency": "THB",
        "host": "sellercenter.lazada.co.th",
        "timezone": "Asia/Bangkok",
    },
    "ID": {
        "country": "ID",
        "name": "印度尼西亚",
        "currency": "IDR",
        "host": "sellercenter.lazada.co.id",
        "timezone": "Asia/Jakarta",
    },
    "VN": {
        "country": "VN",
        "name": "越南",
        "currency": "VND",
        "host": "sellercenter.lazada.vn",
        "timezone": "Asia/Ho_Chi_Minh",
    },
    "SG": {
        "country": "SG",
        "name": "新加坡",
        "currency": "SGD",
        "host": "sellercenter.lazada.sg",
        "timezone": "Asia/Singapore",
    },
}


def get_site_profile(country_code: str) -> dict[str, Any]:
    c = country_code.upper().strip()
    if c in SITE_PROFILES:
        return SITE_PROFILES[c]
    raise ValueError(f"无法核实 Lazada 站点: {c}")


def detect_country_from_url_or_name(url: str, store_name: str) -> str:
    hostname = urlparse(url).hostname
    for code, prof in SITE_PROFILES.items():
        if prof["host"] == hostname:
            return code
    name_upper = store_name.upper()
    for code in ("PH", "MY", "TH", "ID", "VN", "SG"):
        if re.search(rf'(?:^|[-_\s\[(]){code}(?=$|[-_\s\])]|\d)', name_upper):
            return code
    # Check Chinese aliases
    zh_map = {
        "菲律宾": "PH", "马来": "MY", "泰国": "TH",
        "印尼": "ID", "印度尼西亚": "ID", "越南": "VN", "新加坡": "SG"
    }
    for zh, code in zh_map.items():
        if zh in store_name:
            return code
    return ""


@dataclass
class SpendResult:
    raw_text: str
    decimal_value: Decimal | None
    precision: str  # "exact", "rounded_display", "unknown"
    currency: str
    error: str | None = None


def parse_spend_card_value(card_text: str, full_attr: str = "", currency_hint: str = "") -> SpendResult:
    """Evaluate precision and parse decimal spend.

    Per spec 1.2:
    50.16K / 15.94K is rounded display and cannot be treated as exact.
    If exact attribute (such as title="50169.92") is found, use it;
    otherwise flag precision_unverified.
    """
    clean_text = str(card_text or '').strip()
    abbreviated = bool(re.search(r'\d[\d.,]*\s*[KkMmBb万亿]', clean_text))
    for source in ([full_attr] if full_attr else []) + ([] if abbreviated else [clean_text]):
        try:
            amount = _parse_decimal(source)
            return SpendResult(clean_text, amount, 'exact', currency_hint)
        except Exception:
            continue
    return SpendResult(clean_text, None, 'rounded_display' if abbreviated else 'unknown',
                       currency_hint, 'precision_unverified')


def verify_spend_label_currency(label: str, expected_currency: str) -> None:
    match = re.fullmatch(r'(?:Spend|花费|消耗|广告花费)(?:\s*[（(]([^()（）]+)[)）])?', label.strip())
    if not match:
        raise ZiniaoCliError('无法核实 Spend 卡片标题')
    token = (match.group(1) or '').strip()
    if token:
        code = {'泰铢': 'THB'}.get(token, token.upper())
        if code != expected_currency:
            raise ZiniaoCliError('Spend 币种与站点不一致或币种标识无法核实')


class LazadaMonthlyPageAdapter:
    """Verified DOM reads and native CLI input/clicks; never infer success from an ACK."""

    def __init__(self, cli: ZiniaoMonthlyCli, store_id: str, country: str):
        self.cli, self.store_id = cli, store_id
        self.profile = get_site_profile(country)
        self.country = country
        self.evidence = {}
        self.coupon_scope = ''

    def read(self, expression, *, retry=True):
        for attempt in range(3):
            try:
                return self.cli.evaluate(self.store_id, f'JSON.stringify({expression})')
            except ZiniaoCliError as exc:
                code = getattr(exc,'code','')
                if not retry or exc.auth or code not in ('CDP_ERROR','TARGET_NOT_FOUND') or attempt == 2:
                    raise
                if code == 'TARGET_NOT_FOUND':
                    self.cli.rebind_store_page(self.store_id)
                # A redirect may replace the execution context. Re-read only; never replay clicks.
                time.sleep(1)

    def wait(self, predicate, message, timeout=25):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            value = predicate()
            if value:
                return value
            time.sleep(0.5)
        raise ZiniaoCliError(message)

    def element(self, selector, texts=None, *, label=None, optional=False):
        # Return a unique selector for the observed node, without modifying the DOM.
        result = self.read('''(() => {
            const selector = %s, texts = %s, label = %s;
            const visible = e => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0 && getComputedStyle(e).visibility !== 'hidden';
            let found = [...document.querySelectorAll(selector)].filter(visible);
            if (texts) found = found.filter(e => texts.includes((e.innerText || '').trim()));
            if (label) found = found.filter(e => label.includes((e.querySelector('label')?.innerText || '').trim()));
            if (found.length !== 1) return {count:found.length};
            let e = found[0], path = [];
            while(e && e.nodeType===1) {
                const tag=e.tagName.toLowerCase();
                const siblings=e.parentElement ? [...e.parentElement.children].filter(n=>n.tagName===e.tagName) : [e];
                path.unshift(tag+':nth-of-type('+(siblings.indexOf(e)+1)+')');
                e=e.parentElement;
            }
            return {count:1,selector:path.join(' > ')};
        })()''' % (json.dumps(selector), json.dumps(texts), json.dumps(label)))
        if result.get('count') != 1:
            if optional and result.get('count') == 0:
                return None
            raise ZiniaoCliError(f'页面控件无法唯一定位 ({selector})，匹配数量 {result.get("count")}')
        return result['selector']

    def click(self, selector, texts=None, **kwargs):
        target = self.element(selector, texts, **kwargs)
        if target:
            self.cli.click(self.store_id, target)

    def ensure_logged_in(self):
        state = self.read('({path:location.pathname, host:location.hostname})')
        if 'register' in state['path'].lower():
            self.assert_site()
            href = self.wait(lambda: self.read('document.querySelector("a.to-login-link")?.href'), '注册页登录入口未加载')
            parsed = urlparse(href)
            if parsed.scheme != 'https' or parsed.hostname != self.profile['host'] or parsed.path != '/apps/seller/login':
                raise ZiniaoCliError('注册页登录链接站点或路径不一致')
            self.click('a.to-login-link')
            # The registration link opens a new tab; the original tab remains alive.
            self.cli.expected_urls[self.store_id] = href
            def login_tab():
                try:
                    return self.cli.rebind_store_page(self.store_id,require_path=True)
                except ZiniaoCliError as exc:
                    if exc.auth:
                        raise
                    return None
            self.wait(login_tab, '未找到注册页打开的唯一登录标签')
            self.wait_for_page_ready()
            state = self.read('({path:location.pathname, host:location.hostname})')
        if 'login' not in state['path'].lower():
            self.assert_site()
            return
        state = self.read('''(() => {
            const v=e=>e&&e.getBoundingClientRect().width>0;
            return {path:location.pathname,challenge:!![...document.querySelectorAll('#nc_1_wrapper,[class*="captcha"]')].find(v)};
        })()''')
        if 'login' not in state['path'].lower():
            self.assert_site()
            return
        if state['challenge']:
            raise ZiniaoCliError('Lazada 登录需要人工验证，请处理后重试')
        def prefilled():
            content = self.cli._execute_page_command('content', self.store_id, ['--content-format','structured','--timeout','15000'],15)
            forms = content.get('data',content).get('content',{}).get('forms',[])
            # Structured content masks passwords. Never read password values through page.exec.
            return any(f.get('name')=='account' and f.get('value') for f in forms) and any(f.get('type')=='password' and f.get('value') for f in forms)
        self.wait(prefilled,'紫鸟未自动填充完整登录信息，请检查店铺保存的账号',20)
        self.click('button.login-button')
        self.wait(lambda: 'login' not in str(self.read('location.pathname')).lower(), 'Lazada 登录未完成，请检查安全验证')
        self.assert_site()

    def assert_site(self):
        if self.read('location.hostname') != self.profile['host']:
            raise ZiniaoCliError('页面国家域名与店铺站点不一致，停止采集')

    def dismiss_overlays(self):
        for selector in ('.next-dialog .next-dialog-close', 'button.ant-modal-close', '[data-testid="dialog-close"]'):
            self.click(selector, optional=True)

    def wait_for_page_ready(self, max_wait=25):
        self.wait(lambda: self.read('document.readyState') in ('complete', 'interactive'), '页面加载超时', max_wait)

    def navigate(self, target_url):
        current = self.read('location.href')
        if str(current).rstrip('/#') != target_url.rstrip('/#'):
            self.cli.visit(self.store_id, target_url, timeout=40)
        self.wait_for_page_ready()
        self.ensure_logged_in()
        current = self.read('location.href')
        if urlparse(current).path.rstrip('/') != urlparse(target_url).path.rstrip('/'):
            self.cli.visit(self.store_id,target_url,timeout=40)
            self.wait_for_page_ready()
            self.ensure_logged_in()
            if urlparse(self.read('location.href')).path.rstrip('/') != urlparse(target_url).path.rstrip('/'):
                raise ZiniaoCliError('登录后无法进入目标报表，请检查店铺权限')
        self.dismiss_overlays()

    def select_date_range_ant(self, start_date, end_date):
        container = self.wait(lambda: self.read('''(() => {
            if(document.querySelector('.lzd-main-range-picker')) return '.lzd-main-range-picker';
            if(document.querySelector('.affiliate-performance__picker')) return '.affiliate-performance__picker';
            return null;
        })()'''), '未找到主统计日期控件')
        if container == '.lzd-main-range-picker':
            if self.read("!!document.querySelector('.lzd-compare-checkbox input')?.checked"):
                self.click('.lzd-compare-checkbox input')
        # The affiliate VS label is display-only. Never click product checkboxes.
        selectors = self.read('''(() => {
            const c=document.querySelector(%s); return [...c.querySelectorAll('input')].map(e=>e.placeholder);
        })()''' % json.dumps(container))
        if len(selectors) != 2 or not all(selectors):
            raise ZiniaoCliError('主日期控件结构变化')
        before = self.read(f'[...document.querySelectorAll({json.dumps(container + " input")})].map(e=>e.value)')
        order = [(0, start_date), (1, end_date)]
        if before[1] < start_date:
            order.reverse()
        for idx, value in order:
            selector = self.element(container + ' input[placeholder=' + json.dumps(selectors[idx], ensure_ascii=False) + ']')
            self.cli.input_text(self.store_id, selector, value, submit=True)
        self.wait(lambda: self.read(f'[...document.querySelectorAll({json.dumps(container + " input")})].map(e=>e.value)') == [start_date, end_date], '日期选择没有生效')
        time.sleep(1.5)
        self.wait(lambda: not self.read('''[...document.querySelectorAll('.ant-spin-spinning,.next-loading-mask,.next-loading-tip')].some(e=>e.getBoundingClientRect().width>0)'''), '统计数据仍在加载')
        return container

    def _collect_spend(self, kind, start, end, screenshot_path):
        route = 'report' if kind == 'ads' else 'cps'
        self.navigate(f'https://{self.profile["host"]}/sponsor/solutions/ads/{route}/')
        container = self.select_date_range_ant(start, end)
        selector = '.lzd-target-active-item,.lzd-target-unActive-item' if kind == 'ads' else '.affiliate-performance__card'
        label_selector = '.lzd-target-title-text' if kind == 'ads' else '.affiliate-performance__card-title-text'
        value_selector = '.lzd-money-wrapper' if kind == 'ads' else '.affiliate-performance__card-value'
        def snapshot():
            return self.read(r'''(() => {
                const cards=[...document.querySelectorAll(%s)].filter(e=>/^(Spend|花费|消耗|广告花费)(\s*[（(][^()（）]+[)）])?$/.test((e.querySelector(%s)?.innerText||'').trim()));
                const c=cards.length===1?cards[0]:null, v=c?.querySelector(%s);
                return {count:cards.length, text:v?.innerText, exact:v?.getAttribute('title'), label:c?.querySelector(%s)?.innerText,
                    country:document.querySelector('.affiliate-performance')?.getAttribute('sea'), host:location.hostname,
                    dates:[...document.querySelectorAll(%s)].map(e=>e.value)};
            })()''' % tuple(json.dumps(x) for x in (selector, label_selector, value_selector, label_selector, container+' input')))
        state = self.wait(lambda: (r if (r := snapshot()).get('text') else None), 'Spend 卡片未加载')
        # Wait for stable values after the SPA commits the selected range.
        for _ in range(20):
            time.sleep(0.5)
            next_state = snapshot()
            if next_state == state:
                break
            state = next_state
        else:
            raise ZiniaoCliError('Spend 卡片未稳定')
        if state['count'] != 1 or state['dates'] != [start, end] or state['host'] != self.profile['host']:
            raise ZiniaoCliError('Spend 日期、站点或卡片范围核实失败')
        verify_spend_label_currency(state['label'], self.profile['currency'])
        if state.get('country') and state['country'].upper() != self.country:
            raise ZiniaoCliError('联盟页面站点标识不一致')
        result = parse_spend_card_value(state['text'], state.get('exact') or '', self.profile['currency'])
        # Affiliate promotion dialogs can appear after the initial navigation.
        self.dismiss_overlays()
        self.wait(lambda: not self.read("[...document.querySelectorAll('.next-dialog.svc-container')].some(e=>e.getBoundingClientRect().width>0)"), '推广弹窗未关闭，无法保留清晰截图')
        self.cli.screenshot(self.store_id, screenshot_path, full_page=False)
        self.evidence[kind] = {**state, 'currency':self.profile['currency'], 'precision':result.precision}
        return result

    def collect_ads_spend(self, start_date, end_date, screenshot_path):
        return self._collect_spend('ads', start_date, end_date, screenshot_path)

    def collect_affiliate_spend(self, start_date, end_date, screenshot_path):
        return self._collect_spend('affiliate', start_date, end_date, screenshot_path)

    def _filter(self, labels, values):
        selector = self.element(self.coupon_scope+' .next-select', label=labels)
        selected = self.read(f'document.querySelector({json.dumps(selector)})?.querySelector("em")?.innerText')
        if selected not in values:
            self.cli.click(self.store_id, selector)
            self.wait(lambda: self.read('document.querySelectorAll(".next-select-menu-item").length') > 0, '筛选选项未展开')
            self.click('.next-select-menu-item', values)
        selected = self.read(f'document.querySelector({json.dumps(selector)})?.querySelector("em")?.innerText')
        if selected not in values:
            raise ZiniaoCliError('优惠券筛选没有生效')
        return selected

    def prepare_and_search_coupons(self, start_date, end_date, screenshot_path):
        from datetime import date
        self.navigate(f'https://{self.profile["host"]}/sponsor/solutions/ads/sd-account/#!/ads/1')
        self.wait(lambda: self.read('document.querySelectorAll("[action_type=transaction_history_tab]").length') == 1, '交易历史入口未加载')
        self.click('[action_type="transaction_history_tab"]')
        self.wait(lambda: self.read('!!document.querySelector("input.date-input")'), '交易历史筛选未加载')
        # The innermost tab panel containing this unique date field owns all filters/table.
        self.coupon_scope = self.element('.next-tabs-tabpane:has(input.date-input):not(:has(.next-tabs-tabpane input.date-input))')
        fields = ((['钱包','Wallet'], ['优惠券','Coupon']),
                  (['交易类型','Transaction Type','Type'], ['充值','Top up','Top Up','Recharge']),
                  (['状态','Status'], ['全部','All']))
        selected = [self._filter(labels, values) for labels, values in fields]
        dates = [date.fromisoformat(d).strftime('%d/%m/%Y') for d in (start_date,end_date)]
        for side, text in zip(('left','right'), dates):
            if not self.read('!!document.querySelector(".rc-calendar")'):
                self.click('input.date-input')
            self.wait(lambda: self.read(f'!!document.querySelector(".rc-calendar-range-{side} input")'), '交易日期面板未打开')
            target = self.element(f'.rc-calendar-range-{side} input')
            self.cli.input_text(self.store_id, target, text, submit=True)
            # Enter commits one side and asynchronously unmounts the popover.
            # Finish that transition before opening it for the other endpoint;
            # an immediate presence check can otherwise observe a closing panel.
            self.click('[action_type="transaction_history_tab"]')
            self.wait(lambda: not self.read('!!document.querySelector(".rc-calendar")'), '日期提交后面板未关闭')
        expected = ' - '.join(dates)
        if self.read('document.querySelector("input.date-input").value') != expected:
            raise ZiniaoCliError('交易历史日期核实失败')
        if self.read('!!document.querySelector(".rc-calendar")'):
            self.click('[action_type="transaction_history_tab"]')
            self.wait(lambda: not self.read('!!document.querySelector(".rc-calendar")'), '日期面板未关闭')
        self.click(self.coupon_scope+' button', ['搜索','Search'])
        time.sleep(1.5)
        def table_state():
            return self.read('''(() => {
                const c=document.querySelector(%s), table=c?.querySelector('.next-table');
                if(!table) return null;
                const text=table.innerText, busy=!!c.querySelector('.next-loading-mask');
                const empty=/暂无数据|No Data|没有找到数据|No records|无记录/i.test(text);
                const pagination=c.querySelector('.next-pagination')?.innerText || '';
                const rows=[...table.querySelectorAll('tbody tr')].filter(e=>e.querySelector('td')&&!/next-table-empty/.test(e.className));
                return {empty, busy, headers:[...table.querySelectorAll('thead th')].map(e=>e.innerText.trim()),
                    rowCount:rows.length,pagination,date:c.querySelector('input.date-input')?.value,
                    filters:[...c.querySelectorAll('.next-select em')].map(e=>e.innerText),
                    lastPageDisabled:!!c.querySelector('.next-pagination .next-next[disabled]'),
                    host:location.hostname};
            })()''' % json.dumps(self.coupon_scope))
        state = self.wait(lambda: (s if (s := table_state()) and not s['busy'] and (s['rowCount'] or s['empty']) else None), '交易记录未加载完成')
        if state['date'] != expected or state['filters'] != selected or state['host'] != self.profile['host']:
            raise ZiniaoCliError('交易历史筛选回读不一致')
        if not any(h in ('金额','Amount') for h in state['headers']):
            raise ZiniaoCliError('交易历史明细表头未核实')
        total_match = re.search(r'(?:共\s*|Total\s*[:：]?\s*|of\s+)([\d,]+)',state['pagination'], re.I)
        count = int(total_match.group(1).replace(',','')) if total_match else (state['rowCount'] if state['lastPageDisabled'] else None)
        if state['empty']:
            count = 0
        if count is None:
            raise ZiniaoCliError('未能核实优惠券总记录数，无法确认导出完整性')
        self.cli.screenshot(self.store_id, screenshot_path, full_page=False)
        result = {'is_empty':state['empty'], 'has_download':not state['empty'], 'filters_verified':True,
                  'expected_count':count, 'start_date':start_date,'end_date':end_date,'country':self.country,
                  'currency':self.profile['currency'], 'state':state}
        self.evidence['coupon'] = result
        return result

    def capture_coupon_download_blob(self, dest_path, timeout=30):
        """Capture only the blob attached to this export's download link; click once."""
        import base64
        self.read('''(() => {
            if(window.__hqylDownload) throw new Error('download already armed');
            const state=window.__hqylDownload={blobs:new Map(),result:null,origURL:URL.createObjectURL,origClick:HTMLAnchorElement.prototype.click};
            URL.createObjectURL=function(blob){const u=state.origURL.call(URL,blob);state.blobs.set(u,blob);return u;};
            HTMLAnchorElement.prototype.click=function(){
                const blob=state.blobs.get(this.href);
                if(this.download && blob){const name=this.download;const reader=new FileReader();reader.onload=()=>{state.result={filename:name,size:blob.size,base64:String(reader.result).split(',')[1]};};reader.readAsDataURL(blob);}
                return state.origClick.call(this);
            };
            return true;
        })()''', retry=False)
        try:
            self.trigger_coupon_download()
            result = self.wait(lambda: self.read('window.__hqylDownload.result'), '优惠券导出未返回可核实的文件，请检查下载后重试', timeout)
            data = base64.b64decode(result['base64'], validate=True)
            if len(data) != result['size'] or not data:
                raise ZiniaoCliError('导出文件大小校验失败')
            ext = '.xlsx' if data.startswith(b'PK\x03\x04') else '.csv'
            path = Path(dest_path).with_suffix(ext)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.evidence['download'] = {'method':'anchor_blob','file_name':Path(result['filename']).name,'size':len(data)}
            return path
        finally:
            # Restore hooks even on timeouts; never click Download again as a fallback.
            if not self.cli.auth_failed:
                self.read('''(() => {const s=window.__hqylDownload;if(s){URL.createObjectURL=s.origURL;HTMLAnchorElement.prototype.click=s.origClick;delete window.__hqylDownload;}return true;})()''')

    def trigger_coupon_download(self):
        self.click(self.coupon_scope+' button', ['下载','Download','Export','导出'])
