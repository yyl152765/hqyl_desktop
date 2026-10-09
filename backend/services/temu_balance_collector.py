"""Read TEMU's visible balance cards and retain their native-window evidence."""
from __future__ import annotations

import re
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from selenium.common.exceptions import NoSuchWindowException, StaleElementReferenceException

from backend.services.balance_evidence import capture_balance_evidence
from backend.services.temu_on_sale_login import ensure_temu_session
from backend.services.temu_balance_calendar import set_calendar_month, query_and_wait_for_refresh
from backend.services.temu_balance_overlays import dismiss_temu_message_panel, click_with_message_panel_retry


SETTLEMENT_URL = 'https://agentseller.temu.com/labor/settle'
FUNDS_URL = 'https://seller.kuajingmaihuo.com/labor/account'
CARD_SELECTOR = '[class*="dashboard-item_item__"]'

PAGE_STATE = r"""return (() => {
  const visible=e=>e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
  const text=e=>(e?.innerText||'').trim();
  const unobscured=e=>{if(!e)return false;const r=e.getBoundingClientRect();
    if(r.left<0||r.top<0||r.right>innerWidth||r.bottom>innerHeight||r.width<=0||r.height<=0)return false;
    const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);return hit===e||e.contains(hit);};
  const path=e=>{const parts=[];while(e?.nodeType===1){
    const peers=e.parentElement?[...e.parentElement.children].filter(x=>x.tagName===e.tagName):[e];
    parts.unshift(e.tagName.toLowerCase()+':nth-of-type('+(peers.indexOf(e)+1)+')');e=e.parentElement;
  }return parts.join(' > ');};
  const cards=[...document.querySelectorAll('[class*="dashboard-item_item__"]')].filter(visible).map(e=>({
    title:text(e.querySelector('[class*="dashboard-item_titleWrapper"]')),
    label:text(e.querySelector('[class*="dashboard-item_titleWrapper"] span')),
    amount:text(e.querySelector('[class*="number-tip_wrapper__"]')),
    capturable:unobscured(e.querySelector('[class*="number-tip_wrapper__"]')),
    selector:path(e)
  }));
  const tabs=[...document.querySelectorAll('[data-testid="beast-core-tab-itemLabel-wrapper"]')].filter(visible)
    .map(e=>({text:text(e),selected:[...e.classList].some(x=>x.startsWith('TAB_active_'))}));
  const fields=[...document.querySelectorAll('input[data-testid="beast-core-rangePicker-htmlInput"]')].filter(visible);
  const beforeTable=(document.body.innerText||'').split('PO单号\t')[0].split('提现记录')[0];
  const errors=[...document.querySelectorAll('[role="alert"],[data-testid="beast-core-toast"],[class*="Toast_error"]')]
    .filter(visible).map(text).filter(t=>/失败|异常|错误|稍后重试|网络|无权限/.test(t));
  return {url:location.origin+location.pathname,cards,tabs,range:fields.length===1?fields[0].value:null,
    rangeCapturable:fields.length===1 && unobscured(fields[0]),
    extraFilters:[...document.querySelectorAll('input[placeholder*="多个"]')].filter(visible).map(e=>e.value),
    region:text(document.querySelector('[class*="index-module__drLabel"]')),
    merchant:[...document.querySelectorAll('[class*="account-info_mallInfo__"]')].filter(visible).map(text),
    usd:/\bUSD\b/.test(beforeTable),hasLoanAccount:beforeTable.includes('货款账户'),
    hasOrderTime:beforeTable.includes('订单创建时间'),errors,
    busy:[...document.querySelectorAll('[aria-busy="true"],[data-loading="true"],[data-testid="beast-core-loading"],[data-testid="beast-core-spin"],.ant-spin-spinning,.el-loading-mask')].some(visible),
    updateNotice:(beforeTable.match(/数据更新时间[^\n]+/)||[])[0]||''};
})();"""


def parse_usd_amount(raw: str) -> str:
    """Accept the observed USD format; blanks and masked values never become zero."""
    value = re.sub(r'\s+', '', str(raw or '')).replace('−', '-').replace('－', '-')
    value = value.replace('USD', '').replace('$', '')
    negative = value.startswith('(') and value.endswith(')')
    if negative:
        value = value[1:-1]
    if not re.fullmatch(r'[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?', value):
        raise ValueError('TEMU 金额为空、被隐藏或格式无法确认')
    amount = Decimal(value.replace(',', ''))
    if negative:
        amount = -abs(amount)
    return format(amount, '.2f')


def metric_from_state(state: dict, field: str) -> tuple[str, str]:
    """Validate scope, selected tab and currency before reading one exact card."""
    route = urlsplit(state.get('url', ''))
    host, path = ('agentseller.temu.com', '/labor/settle') if field == 'pending' else ('seller.kuajingmaihuo.com', '/labor/account')
    if route.hostname != host or route.path.rstrip('/') != path:
        raise ValueError('TEMU 未进入对应资金页面，请检查登录状态')
    if state.get('busy') or state.get('errors'):
        raise ValueError('TEMU 页面仍在加载或显示查询错误')
    if not state.get('usd'):
        raise ValueError('TEMU 页面未确认美元 USD 币种')
    if field == 'pending':
        if state.get('region') != '全球':
            raise ValueError('TEMU 结算数据未确认处于全球页面')
        selected = [tab for tab in state.get('tabs', []) if tab.get('selected') and tab.get('text') == '待处理款项']
        if len(selected) != 1 or not state.get('hasOrderTime'):
            raise ValueError('TEMU 未确认待处理款项及订单创建时间筛选')
        label = '待处理款项总额'
    else:
        if not state.get('hasLoanAccount'):
            raise ValueError('TEMU 未确认货款账户')
        label = '总金额'
    cards = [card for card in state.get('cards', []) if card.get('label') == label]
    if len(cards) != 1:
        raise ValueError(f'TEMU 无法唯一确认“{label}”金额卡片')
    return parse_usd_amount(cards[0].get('amount', '')), cards[0]['selector']


def _click_unique(driver, by: str, selector: str, description: str) -> None:
    def click():
        elements = [e for e in driver.find_elements(by, selector) if e.is_displayed() and e.is_enabled()]
        if len(elements) != 1:
            raise ValueError(f'TEMU 无法唯一确认{description}')
        elements[0].click()
    click_with_message_panel_retry(driver, click)


def _dismiss_tour(driver):
    for _ in range(4):
        popovers = [e for e in driver.find_elements('css selector', '.driver-popover') if e.is_displayed()]
        if not popovers:
            return
        _click_unique(driver, 'css selector', '.driver-popover-next-btn', '结算页面介绍按钮')
        time.sleep(.2)
    if any(e.is_displayed() for e in driver.find_elements('css selector', '.driver-popover')):
        raise ValueError('TEMU 页面介绍弹窗未关闭')


def _business_page(driver, url: str, field: str, *, timeout: float = 35, settle_seconds: float = 1) -> dict:
    driver.get(url)
    if field == 'pending':
        handle = ensure_temu_session(driver, timeout=timeout)
        driver.switch_to.window(handle)
        if urlsplit(driver.current_url).path != '/labor/settle':
            driver.get(url)
    deadline = time.monotonic() + timeout
    preferred = driver.current_window_handle
    stable = {}
    while time.monotonic() < deadline:
        # ZiNiao may leave an unrelated login tab active while the original
        # business tab is already ready. Inspect every surviving tab by route.
        handles = list(driver.window_handles)
        if preferred in handles:
            handles.remove(preferred)
            handles.insert(0, preferred)
        for handle in handles:
            try:
                driver.switch_to.window(handle)
                actual = urlsplit(driver.current_url)
                expected = urlsplit(url)
                if (actual.hostname, actual.path.rstrip('/')) != (expected.hostname, expected.path):
                    continue
                state = driver.execute_script(PAGE_STATE)
                if state.get('cards') and state.get('usd') and not state.get('busy'):
                    signature = repr((state['cards'], state.get('region'), state.get('errors')))
                    previous, since = stable.get(handle, (None, time.monotonic()))
                    if previous == signature and time.monotonic() - since >= settle_seconds:
                        return state
                    stable[handle] = (signature, since if previous == signature else time.monotonic())
                else:
                    stable.pop(handle, None)
            except (NoSuchWindowException, StaleElementReferenceException):
                continue
        time.sleep(.35)
    raise ValueError('TEMU 资金页面未加载完成；请在该店铺完成登录或处理验证后重试')


def _clean_url(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def collect_temu_balance(driver, store: dict, month: str, evidence_dir: Path, progress=None) -> dict:
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    if callable(getattr(driver, 'maximize_window', None)):
        driver.maximize_window()
    result = {'country': 'GLOBAL', 'currency': 'USD', 'values': {'total': None, 'pending': None},
              'evidence': {}, 'field_errors': {}, 'evidence_exemptions': {}, 'source_urls': {},
              'notes': [], 'captured_at': ''}
    for field, url, label in [('pending', SETTLEMENT_URL, '预估待结算销售额'),
                              ('total', FUNDS_URL, '账户总金额')]:
        try:
            if progress:
                progress(f'{store["store_name"]}：读取{label}')
            state = _business_page(driver, url, field)
            if dismiss_temu_message_panel(driver):
                state = driver.execute_script(PAGE_STATE)
            if field == 'pending':
                _dismiss_tour(driver)
                state = driver.execute_script(PAGE_STATE)
                if not any(t.get('selected') and t.get('text') == '待处理款项' for t in state.get('tabs', [])):
                    _click_unique(driver, 'xpath', '//*[@data-testid="beast-core-tab-itemLabel-wrapper"][normalize-space()="待处理款项"]', '待处理款项页签')
                    time.sleep(.5)
                    state = driver.execute_script(PAGE_STATE)
                _click_unique(driver, 'xpath', '//button[normalize-space()="重置"]', '重置筛选按钮')
                state = driver.execute_script(PAGE_STATE)
                if len(state.get('extraFilters', [])) != 2 or any(state['extraFilters']):
                    raise ValueError('TEMU PO 单号或 SKU 筛选未清空，不能统计整月总额')
                _, selector = metric_from_state(state, field)
                selection = set_calendar_month(driver, month)
                query_and_wait_for_refresh(driver, selector, expected_range=selection['range_value'])
                dismiss_temu_message_panel(driver)
                state = driver.execute_script(PAGE_STATE)
                if (state.get('range') != selection['range_value']
                        or len(state.get('extraFilters', [])) != 2
                        or any(state['extraFilters'])):
                    raise ValueError('TEMU 查询后月份或 PO/SKU 筛选范围发生变化，请重新采集')
                result['notes'].append(f'订单创建时间：{selection["start_date"]} 至 {selection["end_date"]}')
                if state.get('updateNotice'):
                    result['notes'].append(state['updateNotice'])
            value, _ = metric_from_state(state, field)
            result['values'][field] = value
            result['source_urls'][field] = _clean_url(driver.current_url)
            card_label = '待处理款项总额' if field == 'pending' else '总金额'
            card = next(c for c in state['cards'] if c['label'] == card_label)
            if not card.get('capturable') or (field == 'pending' and not state.get('rangeCapturable')):
                raise ValueError('TEMU 金额或日期范围不在可见区域或被遮挡，请展开窗口并关闭遮挡后重试')
            result['evidence'][field] = capture_balance_evidence(
                driver, evidence_dir / f'{field}.png', store['store_name'], label)
            # A navigation or data refresh during screenshot invalidates that
            # field's evidence instead of presenting a different value as proof.
            after = driver.execute_script(PAGE_STATE)
            if (metric_from_state(after, field)[0] != value
                    or (field == 'pending' and (after.get('range') != state.get('range')
                                               or len(after.get('extraFilters', [])) != 2
                                               or any(after['extraFilters'])))):
                result['evidence'].pop(field, None)
                raise ValueError('TEMU 截图期间金额、月份或 PO/SKU 筛选发生变化，请重新采集')
        except Exception as exc:
            result['field_errors'][field] = str(exc)
            if progress:
                progress(f'{store["store_name"]}：{label}未完成，{exc}')
    result['captured_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
    return result
