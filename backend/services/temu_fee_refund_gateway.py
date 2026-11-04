from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from backend.services.temu_fee_refund_controls import _wait, click_control, dismiss_overlays, field_state, select_field, set_range
from backend.services.temu_fee_refund_download import download_xlsx
from backend.services.temu_fee_refund_fees import CLOCK_STATE, export_fee
from backend.services.temu_fee_refund_refunds import validate_refund_file
from backend.services.temu_on_sale_gateway import TemuOnSaleGateway

ROUTES = {'shipping': '/labor/stml-logistics', 'return_label': '/labor/stml-reverse-logistics', 'refund': '/mmsos/return-refund-list.html'}

KINDS = {'shipping': '发货面单费', 'return_label': '退货面单费', 'refund': '退货/退款'}

# Resolve controls afresh on every interaction.  The page must expose exactly one
# visible control with the requested label; ambiguity is an error, never a guess.
STATE = r'''(() => {
 const vis=e=>e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
 const clean=s=>String(s||'').replace(/\s+/g,'');
 const path=e=>{const p=[];while(e&&e.nodeType===1){for(const attr of ['id','data-testid']){const value=e.getAttribute(attr);if(value){const anchor='['+attr+'='+JSON.stringify(value)+']';if(document.querySelectorAll(anchor).length===1){p.unshift(anchor);return p.join(' > ');}}}const s=e.parentElement?[...e.parentElement.children].filter(x=>x.tagName===e.tagName):[e];p.unshift(e.tagName.toLowerCase()+':nth-of-type('+(s.indexOf(e)+1)+')');e=e.parentElement;}return p.join(' > ')};
 const labels=__LABELS__, found={};
 for(const label of labels){
  const nodes=[...document.querySelectorAll('a,button,span,div,li,[role="tab"],[role="button"]')].filter(e=>vis(e)&&clean(e.innerText||e.textContent)===clean(label)&&![...e.children].some(c=>vis(c)&&clean(c.innerText||c.textContent)===clean(label)));
  found[label]=[...new Set(nodes.map(e=>e.closest('button,a,[role="tab"],[role="button"]')||e))].map(e=>({selector:path(e),disabled:e.matches(':disabled')||e.getAttribute('aria-disabled')==='true',selected:[e,e.parentElement,e.parentElement?.parentElement].filter(Boolean).some(n=>n.getAttribute('aria-selected')==='true'||[...n.classList].some(c=>/(^|[-_])(active|selected)([-_]|$)/i.test(c)))}));
 }
 const drawers=[...document.querySelectorAll('[data-testid="beast-core-drawer-content"]')].filter(e=>vis(e)&&e.innerText.includes('售后单创建日期'));
 const rows=[...(drawers.length===1?drawers[0]:document.createElement('div')).querySelectorAll('tr')].filter(vis).map(e=>({text:e.innerText,requested:e.cells?.[2]?.innerText||'',selector:path(e),downloads:[...e.querySelectorAll('a,button')].filter(x=>vis(x)&&clean(x.innerText)==='下载').map(path)}));
 return JSON.stringify({url:location.href,found,rows,refund_regions:[...document.querySelectorAll('._2JBlx01R._1Q9JwBPE')].filter(vis).map(e=>clean(e.innerText)),body:document.body.innerText.slice(0,30000),sellers:[...document.querySelectorAll('[class*="account-info_mallInfo__"],._Wrz4-O9w')].filter(vis).map(e=>e.innerText.trim()),refund_history_empty:drawers.length===1&&/暂无数据|暂无记录/.test(drawers[0].innerText),refund_history:drawers.length===1&&drawers[0].innerText.includes('导出记录'),busy:[...document.querySelectorAll('[aria-busy="true"],.ant-spin-spinning,.el-loading-mask,[data-testid="beast-core-loading"]')].some(vis)});
})()'''

class TemuFeeRefundGateway:
    def __init__(self, profile, *, browser, timeout=180):
        if browser is None or browser.profile != profile:
            raise ValueError('请使用本次所选紫鸟账号创建浏览器会话')
        self.profile, self.browser, self.timeout = profile, browser, timeout
        self.current_stores = {}

    @property
    def auth_failed(self):
        return self.browser.auth_failed

    @property
    def halted(self):
        return self.auth_failed or getattr(self.browser, 'cleanup_failed', False)

    @property
    def halt_reason(self):
        return '店铺未确认关闭或紫鸟认证失效，请检查后重试'

    def preflight(self, stores):
        self.browser.ready()
        self.current_stores = {s['store_id']: s for s in self.browser.list_stores()}

    def state(self, store_id, labels=()):
        state = self.browser.evaluate(store_id, STATE.replace('__LABELS__', json.dumps(list(labels), ensure_ascii=False)))
        if not isinstance(state, dict) or 'found' not in state:
            raise ValueError('无法识别 TEMU 页面')
        host = urlsplit(state.get('url', '')).hostname or ''
        if host != 'temu.com' and not host.endswith('.temu.com'):
            raise ValueError('当前页面不是 TEMU 卖家后台')
        return state

    def wait(self, store_id, predicate, labels=(), seconds=30):
        return _wait(lambda: (s if not (s := self.state(store_id, labels)).get('busy') and predicate(s) else None), seconds, '页面未就绪或控件未能确认')

    def click(self, store_id, label):
        def resolve():
            state = self.state(store_id, [label])
            choices = state['found'].get(label, [])
            if len(choices) != 1 or choices[0].get('disabled'):
                raise ValueError(f'无法唯一定位可用的“{label}”')
            return choices[0]['selector']
        click_control(self.browser, store_id, resolve)

    def navigate(self, identity, url):
        if urlsplit(url).hostname != 'agentseller.temu.com':
            raise ValueError('仅可导航到 TEMU 卖家后台')
        if callable(getattr(self.browser, 'navigate', None)):
            self.browser.navigate(identity, url)
        else:
            self.browser._driver(identity).get(url)

    def export(self, store, kind, start_date, end_date, raw_dir):
        if kind not in KINDS:
            raise ValueError('导出类型无效')
        try:
            start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        except (ValueError, TypeError):
            raise ValueError('日期必须为 YYYY-MM-DD') from None
        if start > end:
            raise ValueError('开始日期不能晚于结束日期')
        actual = self.current_stores.get(store['store_id'])
        if not actual or actual['store_name'] != store['store_name'] or 'temu' not in actual['platform_name'].casefold():
            raise ValueError('店铺权限、名称或平台发生变化，请重新匹配')
        identity = store['store_id']
        export_error = None
        try:
            self.browser.open_store(store, url='https://agentseller.temu.com/')
            state = self.wait(identity, lambda s: len(s.get('sellers', [])) == 1 and s['sellers'][0] not in {'', '-'})
            seller = TemuOnSaleGateway.verify_seller(store, state)
            self.navigate(identity, 'https://agentseller.temu.com' + ROUTES[kind])
            action = '售后单导出' if kind == 'refund' else '导出'
            state = self.wait(identity, lambda s: len(s['found'].get(action, [])) == 1 and len(s.get('sellers', [])) == 1 and s['sellers'][0] not in {'', '-'}, [action])
            TemuOnSaleGateway.verify_seller(store, state)
            dismiss_overlays(self.browser, identity)
            if kind != 'refund':
                return export_fee(self, identity, kind, start, end, raw_dir, seller)
            if state.get('refund_regions') != ['全球']:
                raise ValueError('售后页面未确认处于全球区域')
            self.click(identity, '售后单导出')
            self.wait(identity, lambda s: s.get('refund_history') and len(s['found'].get('确认导出', [])) == 1, ['确认导出'])
            set_range(self.browser, identity, start, end, label='售后单创建日期')
            select_field(self.browser, identity, '售后单类型', '退货退款')
            select_field(self.browser, identity, '售后单状态', '已退款')
            country = field_state(self.browser, identity, '国家/地区', 'select')
            if country['value'] not in {'', '全部'} or country.get('text') not in {'', '全部'} or country.get('placeholder') != '全部':
                raise ValueError('售后导出国家/地区未确认选择全部，请清除地区筛选后重试')
            return self.export_refund(identity, raw_dir, start, end, seller)
        except Exception as exc:
            export_error = exc
            raise
        finally:
            if getattr(self.browser, 'cleanup_failed', False) is not True:
                try:
                    self.browser.close_store(identity)
                except Exception as close_error:
                    if export_error is not None:
                        raise ValueError(f'{export_error}；清理店铺时：{close_error}') from None
                    raise

    @staticmethod
    def task_id(row):
        match = re.search(r'\bEX-[A-Za-z0-9-]+\b', row.get('text', ''))
        return match.group(0) if match else ''

    @staticmethod
    def matching_dates(row, start, end):
        dates = re.findall(r'(\d{4})\s*(?:年|[-/])\s*(\d{1,2})\s*(?:月|[-/])\s*(\d{1,2})(?:日)?', row.get('text', ''))
        try:
            return len(dates) >= 2 and tuple(date(*map(int, item)) for item in dates[:2]) == (start, end)
        except ValueError:
            return False

    def export_refund(self, identity, raw_dir, start, end, seller):
        history = self.wait(identity, lambda s: s.get('refund_history') and (any(self.task_id(r) for r in s.get('rows', [])) or s.get('refund_history_empty')))
        before = history.get('rows', [])
        ids = [self.task_id(row) for row in before if self.task_id(row)]
        if len(ids) != len(set(ids)):
            raise ValueError('导出记录存在重复任务编号，无法安全匹配新任务')
        submitted = datetime.strptime(self.browser.evaluate(identity, CLOCK_STATE), '%Y-%m-%d %H:%M:%S')
        self.click(identity, '确认导出')

        def completed():
            state = self.state(identity)
            new = [r for r in state.get('rows', []) if self.task_id(r) and self.task_id(r) not in ids]
            if len(new) > 1:
                raise ValueError('同时出现多个新导出任务，无法安全确定本次任务')
            if not new:
                return None
            row = new[0]
            stamp = re.fullmatch(r'(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})\s+CST', row.get('requested', '').strip())
            if not stamp:
                raise ValueError('新售后任务缺少可核对的请求时间')
            requested = datetime(*map(int, stamp.groups()))
            now = datetime.strptime(self.browser.evaluate(identity, CLOCK_STATE), '%Y-%m-%d %H:%M:%S')
            if not submitted.replace(second=0) - timedelta(minutes=1) <= requested <= now + timedelta(minutes=1):
                raise ValueError('售后任务请求时间不在本次提交时间内')
            if not self.matching_dates(row, start, end):
                raise ValueError('新导出任务日期与请求不一致')
            if '失败' in row.get('text', ''):
                raise ValueError('平台售后导出任务失败')
            if '无数据' in row.get('text', ''):
                return row
            if '已完成' not in row.get('text', ''):
                return None
            if len(row.get('downloads', [])) != 1:
                raise ValueError('无法唯一定位新任务的下载按钮')
            return row

        row = _wait(completed, self.timeout, '未能确认本次售后导出任务完成')
        evidence = {'seller': seller, 'task_id': self.task_id(row), 'submitted_at': submitted.isoformat(), 'requested_at': row['requested'], 'start_date': start.isoformat(), 'end_date': end.isoformat(), 'type': '退货退款', 'status': '已退款', 'country': '全部'}
        if '无数据' in row['text']:
            return {'no_data': True, 'row_count': 0, 'evidence': evidence}
        path = download_xlsx(self.browser, identity, row['downloads'][0], raw_dir, filename='售后单.xlsx', timeout=self.timeout)
        count = validate_refund_file(path, start, end)
        return {'path': str(path), 'row_count': count, 'evidence': evidence}
