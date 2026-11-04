"""Observed TEMU shipping/return-label bill exports and completeness checks."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from openpyxl import load_workbook

from backend.services.temu_balance_calendar import (
    DEFAULT_LOADING_SELECTOR, REFRESH_CLEANUP, REFRESH_INSTALL, REFRESH_READ,
)
from backend.services.temu_fee_refund_controls import (
    _evaluate, _script, _wait, click_control, dismiss_overlays, field_state, set_range,
)
from backend.services.temu_fee_refund_download import download_xlsx


SUMMARY_SELECTOR = '[class*="dashboard_container__"]'
TABLE_SELECTOR = '[data-testid="beast-core-table-middle-body"]'
PATHS = {'shipping': '/labor/stml-logistics', 'return_label': '/labor/stml-reverse-logistics'}
TITLES = {'shipping': '发货面单费', 'return_label': '退货面单费'}
DATE_LABEL = '账单支出/退款时间'

SUMMARY_STATE = _script(r'''
const roots=[...document.querySelectorAll('[class*="dashboard_container__"]')].filter(visible);
// Read each labelled card independently. CSS may display the three cards in a
// different order from their DOM order; concatenating the whole panel loses
// that label/value association. A card must contain just one metric label.
const metricLabels=['币种','账单数','总金额'];
const metricLabel=e=>clean(e.innerText).replace(/[：:]$/, '');
const metricFields=root=>{
  const leaves=[...root.querySelectorAll('*')].filter(e=>visible(e)&&metricLabels.includes(metricLabel(e))
    &&![...e.children].some(c=>visible(c)&&metricLabel(c)===metricLabel(e)));
  const fields={};
  for(const label of metricLabels){
    const matches=leaves.filter(e=>metricLabel(e)===label);
    if(matches.length!==1)return null;
    for(let node=matches[0].parentElement;node&&root.contains(node);node=node.parentElement){
      if(leaves.filter(e=>node.contains(e)).length!==1)break;
      const text=clean(node.innerText), value=text.replace(new RegExp(label+'[：:]?'), '');
      if(value&&value!==text){fields[label]=value;break;}
    }
  }
  return metricLabels.every(label=>Object.hasOwn(fields,label))?fields:null;
};
const query=[...document.querySelectorAll('button')].filter(e=>visible(e)&&clean(e.innerText)==='查询');
let form=query.length===1?query[0].parentElement:null;
while(form && !form.querySelector('input,textarea,select'))form=form.parentElement;
const controls=form?[...form.querySelectorAll('input,textarea,select')].filter(e=>visible(e)&&e.type!=='hidden'&&e.type!=='checkbox').map(e=>({
  testid:e.getAttribute('data-testid')||'',value:String(e.value||''),placeholder:e.getAttribute('placeholder')||''
})):null;
const tables=[...document.querySelectorAll('[data-testid="beast-core-table-middle-body"]')].filter(visible);
const empty=tables.length===1&&clean(tables[0].innerText)==='暂无数据'&&!tables[0].querySelector('tbody tr');
return {url:location.origin+location.pathname,panels:roots.map(e=>e.innerText),metrics:roots.map(metricFields),controls,empty,
  loading:[...document.querySelectorAll('[aria-busy="true"],[data-loading="true"],[data-testid="beast-core-loading"],[data-testid="beast-core-spin"],.ant-spin-spinning,.el-loading-mask')].some(visible),
  region:[...document.querySelectorAll('[class*="index-module__drLabel"]')].filter(visible).map(e=>clean(e.innerText))};
''')

HISTORY_STATE = _script(r'''
const drawers=[...document.querySelectorAll('[data-testid="beast-core-drawer"]')].filter(visible);
const localNow=()=>{const d=new Date(),p=x=>String(x).padStart(2,'0');return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());};
const roots=drawers.filter(e=>e.querySelector('[class*="export-history_exportHistoryList__"]')||/导出.*刷新|暂无.*(?:导出|记录)/.test(e.innerText));
if(!roots.length)return {open:false,rows:[],now:localNow()};
if(roots.length!==1)return {error:'导出历史抽屉不唯一'};
const root=roots[0];
const rows=[...root.querySelectorAll('[class*="export-history_list__"]')].filter(visible).map(e=>({
  text:e.innerText,generated:(e.innerText.match(/导出任务生成时间\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})/)||[])[1]||'',
  condition:[...e.querySelectorAll('[class*="export-history_middle__"]')].map(x=>x.innerText).join('\n'),
  downloads:[...e.querySelectorAll('button')].filter(b=>visible(b)&&clean(b.innerText)==='下载数据报表').map(b=>({selector:path(b),disabled:disabled(b)}))
}));
const refreshLeaves=[...root.querySelectorAll('button,a,span,div')].filter(e=>visible(e)&&clean(e.innerText)==='刷新'&&![...e.children].some(c=>visible(c)&&clean(c.innerText)==='刷新'));
return {open:true,rows,now:localNow(),empty:/暂无数据|暂无记录|暂无导出/.test(root.innerText),
  loading:[...root.querySelectorAll('[aria-busy="true"],[data-testid="beast-core-loading"],[data-testid="beast-core-spin"]')].some(visible),
  refresh:[...new Set(refreshLeaves.map(e=>e.closest('button,a,[role="button"]')||e))].map(e=>({selector:path(e),disabled:disabled(e)}))};
''')

CLOCK_STATE = _script(r'''
const d=new Date(),p=x=>String(x).padStart(2,'0');
return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
''')


def parse_summary(text, *, empty=False, fields=None):
    if fields is not None:
        if not isinstance(fields, dict) or set(fields) != {'币种', '账单数', '总金额'}:
            raise ValueError('面单汇总字段无法唯一关联')
        text = ''.join(label + str(fields[label]) for label in ('币种', '账单数', '总金额'))
    compact = re.sub(r'\s+', '', str(text)).replace('：', ':')
    compact = re.sub(r'(币种|账单数|总金额):', r'\1', compact)
    if compact == '币种--账单数--总金额--' and empty:
        return {'currency': None, 'bill_count': 0, 'total_text': '--', 'empty_table': True}
    match = re.fullmatch(r'币种([A-Z]{3})账单数(\d+|\d{1,3}(?:,\d{3})+)总金额(.+)', compact)
    if not match:
        raise ValueError('无法确认面单费用的币种、账单数和总金额')
    amount = match[3].replace('−', '-')
    # Do not treat a loading caption, a second summary, or placeholder amount
    # as a completed monetary summary merely because a count has appeared.
    currency = r'(?:US\$|R\$|[$€£¥₱฿₫]|[A-Z]{3})'
    if not re.fullmatch(rf'(?:[+-]?{currency}?|{currency}[+-]?)(?:\d+|\d{{1,3}}(?:,\d{{3}})+)(?:\.\d+)?', amount):
        raise ValueError('面单汇总总金额尚未加载或格式无法确认')
    return {'currency': match[1], 'bill_count': int(match[2].replace(',', '')), 'total_text': match[3]}


def _check_filters(state, kind, start, end):
    route = urlsplit(state.get('url', ''))
    if route.hostname != 'agentseller.temu.com' or route.path.rstrip('/') != PATHS[kind]:
        raise ValueError('当前页面与所选面单导出类型不一致')
    controls = state.get('controls')
    if not isinstance(controls, list):
        raise ValueError('无法确认面单查询筛选条件')
    ranges = [c for c in controls if c['testid'] == 'beast-core-rangePicker-htmlInput']
    if len(ranges) != 1 or tuple(re.findall(r'\d{4}-\d{2}-\d{2}', ranges[0]['value'])) != (start.isoformat(), end.isoformat()):
        raise ValueError('面单查询日期与本次请求不一致')
    for control in controls:
        if control in ranges:
            continue
        if control['testid'] == 'beast-core-select-htmlInput':
            if control['value'].strip() not in {'', '全部', '请选择'}:
                raise ValueError('面单查询保留了额外账单类型筛选')
        elif control['value'].strip():
            raise ValueError('面单查询保留了包裹号、运单号或其他额外筛选')
    if state.get('region') and state['region'] != ['全球']:
        raise ValueError('面单费用未处于全球区域')


def query_summary(gateway, identity, kind, start, end, *, timeout=30, settle_ms=800):
    browser = gateway.browser
    _check_filters(_evaluate(browser, identity, SUMMARY_STATE), kind, start, end)
    expected = field_state(browser, identity, DATE_LABEL)['value']
    key = '__hqyl_fee_query_' + uuid4().hex
    try:
        installed = _evaluate(browser, identity, _script(REFRESH_INSTALL, key, SUMMARY_SELECTOR, expected, DEFAULT_LOADING_SELECTOR, TABLE_SELECTOR))
        click_control(browser, identity, installed['button'])

        def refreshed():
            value = _evaluate(browser, identity, _script(REFRESH_READ, key))
            observed = value.get('mutations', 0) > 0 or value.get('saw_loading')
            if not (value.get('armed') and observed and value.get('unique') and not value.get('loading') and value.get('stable_ms', 0) >= settle_ms):
                return None
            state = _evaluate(browser, identity, SUMMARY_STATE)
            _check_filters(state, kind, start, end)
            if state.get('loading') or len(state.get('panels', [])) != 1:
                return None
            metrics = state.get('metrics')
            fields = metrics[0] if isinstance(metrics, list) and len(metrics) == 1 else None
            try:
                summary = parse_summary(state['panels'][0], empty=state.get('empty') is True, fields=fields)
            except ValueError:
                # TEMU can first replace the dashboard with -- placeholders,
                # then render data after the first mutation has settled. A DOM
                # refresh alone therefore does not establish usable data.
                return None
            return {**summary, 'range_value': expected, 'refresh_mutations': value['mutations'],
                    'saw_loading': bool(value['saw_loading'])}

        return _wait(refreshed, timeout, '查询后未确认面单汇总刷新，或无法确认币种、账单数和总金额，不能导出筛选前数据')
    finally:
        try:
            browser.evaluate(identity, _script(REFRESH_CLEANUP + '; return true;', key))
        except Exception:
            pass


def _generated(row):
    try:
        return datetime.strptime(row.get('generated', ''), '%Y-%m-%d %H:%M:%S')
    except ValueError:
        raise ValueError('导出历史缺少可核对的任务生成时间') from None


def select_new_task(rows, baseline, submitted_at, now, start, end):
    """Identify one new timestamp and refuse concurrency or wider conditions."""
    for row in rows:
        _generated(row)
    new = [row for row in rows if row['generated'] not in baseline]
    if len(new) > 1:
        raise ValueError('同时出现多个新导出任务，无法确认本次报表')
    if not new:
        return None
    row = new[0]
    generated = _generated(row)
    if not submitted_at - timedelta(seconds=2) <= generated <= now + timedelta(seconds=2):
        raise ValueError('新导出任务生成时间不在本次提交时间内')
    condition = re.sub(r'\s+', '', row.get('condition', '')).replace('：', ':')
    expected = f'{DATE_LABEL}:{start.isoformat()}~{end.isoformat()}'
    if condition != expected:
        raise ValueError('新导出任务日期或附加筛选与本次请求不一致')
    if re.search(r'导出失败|生成失败|任务失败', row.get('text', '')):
        raise ValueError('平台面单导出任务失败')
    downloads = row.get('downloads', [])
    if len(downloads) != 1:
        raise ValueError('新导出任务的下载按钮不唯一')
    return row if not downloads[0].get('disabled') else None


def validate_fee_workbook(path, kind, start, end, expected_count, currency=None):
    if kind not in PATHS or type(expected_count) is not int or expected_count < 0:
        raise ValueError('面单文件校验参数无效')
    book = load_workbook(path, read_only=True, data_only=False)
    try:
        if len(book.worksheets) != 1:
            raise ValueError('面单报表工作表数量异常')
        sheet = book.worksheets[0]
        sheet.reset_dimensions()
        rows = sheet.iter_rows(values_only=True)
        headers = [str(x or '').strip() for x in next(rows, ())]
        date_header = '支出/退款时间(时区：GMT+8)' if kind == 'shipping' else DATE_LABEL
        required = ({'包裹号', '运单号', '账单类型', '币种', '对账单状态', date_header}
                    if kind == 'shipping' else {'账务单ID', '运单号', 'PO单号', '账单类型', '币种', DATE_LABEL})
        if not required.issubset(headers) or any(headers.count(label) != 1 for label in required):
            raise ValueError('下载文件不是当前面单类型的完整报表')
        when_index, currency_index = headers.index(date_header), headers.index('币种')
        count = 0
        for cells in rows:
            if not any(value is not None for value in cells):
                continue
            count += 1
            if len(cells) <= max(when_index, currency_index):
                raise ValueError('面单文件存在缺少日期或币种的记录')
            value = cells[when_index]
            if isinstance(value, datetime):
                when = value.date()
            elif isinstance(value, date):
                when = value
            else:
                match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?', str(value or '').strip())
                if not match:
                    raise ValueError('面单文件包含无法解析的支出/退款时间')
                when = date.fromisoformat(match[1])
            if not start <= when <= end:
                raise ValueError('面单文件包含所选日期范围外的记录')
            if currency and str(cells[currency_index] or '').strip() != currency:
                raise ValueError('面单文件币种与查询汇总不一致')
        if count != expected_count:
            raise ValueError(f'面单文件仅有 {count} 条，页面账单数为 {expected_count}，不能确认完整')
        return count
    finally:
        book.close()


def export_fee(gateway, identity, kind, start, end, raw_dir, seller):
    if kind not in PATHS or start > end:
        raise ValueError('面单导出类型或日期无效')
    browser = gateway.browser
    timeout = getattr(gateway, 'timeout', 180)
    dismiss_overlays(browser, identity)
    initial = gateway.state(identity)
    route = urlsplit(initial.get('url', ''))
    if route.hostname != 'agentseller.temu.com' or route.path.rstrip('/') != PATHS[kind]:
        raise ValueError('未进入对应面单费用页面')
    gateway.click(identity, '导出历史')
    baseline_state = _wait(lambda: (s if (s := _evaluate(browser, identity, HISTORY_STATE)).get('open') and not s.get('loading') and (s.get('rows') or s.get('empty')) else None), 30, '未能读取现有导出历史')
    baseline = {_generated(row).strftime('%Y-%m-%d %H:%M:%S') for row in baseline_state['rows']}
    if len(baseline) != len(baseline_state['rows']):
        raise ValueError('现有导出任务生成时间重复，无法安全关联新任务')
    # A direct same-page navigation closes the history drawer without relying
    # on its SVG close control (unsupported by some CLI versions).
    gateway.navigate(identity, 'https://agentseller.temu.com' + PATHS[kind])
    gateway.wait(identity, lambda state: len(state['found'].get('重置', [])) == 1, ['重置'])
    dismiss_overlays(browser, identity)
    tab = '已出账' if kind == 'shipping' else '退至商家仓'
    state = gateway.state(identity, [tab])
    tabs = state['found'].get(tab, [])
    if len(tabs) != 1:
        raise ValueError(f'无法唯一识别“{tab}”页签')
    if not tabs[0].get('selected'):
        gateway.click(identity, tab)
        gateway.wait(identity, lambda s: len(s['found'].get(tab, [])) == 1 and s['found'][tab][0].get('selected'), [tab])
    gateway.click(identity, '重置')
    set_range(browser, identity, start, end, label=DATE_LABEL)
    summary = query_summary(gateway, identity, kind, start, end)
    evidence = {'seller': seller, 'kind': kind, 'start_date': start.isoformat(), 'end_date': end.isoformat(), 'tab': tab, **summary}
    if summary['bill_count'] == 0:
        return {'no_data': True, 'row_count': 0, 'evidence': evidence}
    submitted_text = _evaluate(browser, identity, CLOCK_STATE)
    submitted = datetime.strptime(submitted_text, '%Y-%m-%d %H:%M:%S')
    gateway.click(identity, '导出')
    next_refresh = time.monotonic() + 3

    def completed():
        nonlocal next_refresh
        snapshot = _evaluate(browser, identity, HISTORY_STATE)
        if not snapshot.get('open') or snapshot.get('loading'):
            return None
        now = datetime.strptime(snapshot['now'], '%Y-%m-%d %H:%M:%S')
        row = select_new_task(snapshot['rows'], baseline, submitted, now, start, end)
        if row:
            return row
        if time.monotonic() >= next_refresh:
            refresh = snapshot.get('refresh', [])
            if len(refresh) == 1 and not refresh[0].get('disabled'):
                click_control(browser, identity, refresh[0]['selector'])
            next_refresh = time.monotonic() + 3
        return None

    row = _wait(completed, timeout, '未能确认本次面单导出任务完成')
    path = download_xlsx(browser, identity, row['downloads'][0]['selector'], raw_dir, filename=TITLES[kind]+'.xlsx', timeout=timeout)
    count = validate_fee_workbook(path, kind, start, end, summary['bill_count'], summary['currency'])
    evidence.update(submitted_at=submitted_text, task_generated_at=row['generated'], export_condition=row['condition'])
    return {'path': str(path), 'row_count': count, 'evidence': evidence}
