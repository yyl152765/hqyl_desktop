from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from types import SimpleNamespace
import unittest

from openpyxl import Workbook

from backend.services import temu_fee_refund_fees as fees
from test_temu_balance_calendar import Error, sync_playwright
from test_temu_fee_refund_controls import BrowserFixture


class FeeExportTests(TestCase):
    def setUp(self):
        self.start, self.end = date(2026, 9, 1), date(2026, 9, 30)
        self.submitted = datetime(2026, 10, 27, 9, 53, 32)
        self.now = datetime(2026, 10, 27, 9, 53, 36)
        self.row = {'generated': '2026-10-27 09:53:33', 'text': '下载数据报表',
                    'condition': '账单支出/退款时间: 2026-09-01 ~ 2026-09-30',
                    'downloads': [{'selector': '#download', 'disabled': False}]}

    def pick(self, rows, baseline=()):
        return fees.select_new_task(rows, set(baseline), self.submitted, self.now, self.start, self.end)

    def test_new_task_requires_unique_new_time_and_exact_conditions(self):
        self.assertEqual(self.pick([self.row])['generated'], self.row['generated'])
        self.assertIsNone(self.pick([self.row], [self.row['generated']]))
        self.assertIsNone(self.pick([{**self.row, 'downloads': [{'selector':'#download', 'disabled': True}]}]))
        bad = [
            {**self.row, 'generated': '2026-10-27 09:40:00'},
            {**self.row, 'condition': '账单支出/退款时间: 2026-08-01 ~ 2026-08-31'},
            {**self.row, 'condition': self.row['condition'] + '\n账单类型：支出'},
            {**self.row, 'downloads': []},
            {**self.row, 'text': '导出失败'},
        ]
        for row in bad:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.pick([row])
        with self.assertRaisesRegex(ValueError, '多个新'):
            self.pick([self.row, {**self.row, 'generated': '2026-10-27 09:53:34'}])

    def test_summary_rejects_unloaded_or_ambiguous_counts(self):
        self.assertEqual(fees.parse_summary('币种\nUSD\n账单数\n1,219\n总金额\n- $12.50')['bill_count'], 1219)
        self.assertEqual(fees.parse_summary('币种 USD 账单数 0 总金额 $0.00')['bill_count'], 0)
        for text in ('币种 -- 账单数 -- 总金额 --', '账单数219', '币种USD账单数1.2总金额$2'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                fees.parse_summary(text)

    def test_placeholder_zero_requires_explicit_empty_table_evidence(self):
        text = '币种 -- 账单数 -- 总金额 --'
        result = fees.parse_summary(text, empty=True)
        self.assertEqual(result['bill_count'], 0)
        self.assertTrue(result['empty_table'])
        self.assertIsNone(result['currency'])
        for placeholder in (text, '币种 USD 账单数 -- 总金额 --'):
            with self.subTest(placeholder=placeholder), self.assertRaises(ValueError):
                fees.parse_summary(placeholder)
        with self.assertRaises(ValueError):
            fees.parse_summary('币种 USD 账单数 -- 总金额 --', empty=True)

    def test_labelled_card_values_are_independent_of_visual_and_dom_order(self):
        result = fees.parse_summary('账单数 1,613 总金额 - $1,382.02 币种 USD',
                                    fields={'账单数':'1,613', '总金额':'- $1,382.02', '币种':'USD'})
        self.assertEqual(result['bill_count'], 1613)
        self.assertEqual(result['currency'], 'USD')
        self.assertEqual(fees.parse_summary('币种：USD 账单数：0 总金额：$0.00')['bill_count'], 0)
        for total in ('--', '加载中', '$1账单数2', '--$1', '$1,20'):
            with self.subTest(total=total), self.assertRaises(ValueError):
                fees.parse_summary('币种USD账单数1总金额'+total)

    def test_extra_filters_or_wrong_region_are_rejected(self):
        state = {'url':'https://agentseller.temu.com/labor/stml-logistics',
                 'region':['全球'], 'controls':[
                     {'testid':'beast-core-rangePicker-htmlInput','value':'2026-09-01 ~ 2026-09-30'},
                     {'testid':'beast-core-input','value':''},
                     {'testid':'beast-core-select-htmlInput','value':''}]}
        fees._check_filters(state, 'shipping', self.start, self.end)
        for changed in ({**state, 'region':['美国']}, {**state, 'url':'https://agentseller.temu.com/labor/stml-reverse-logistics'},
                        {**state, 'controls':[state['controls'][0], {'testid':'input','value':'old package'}]},
                        {**state, 'controls':[state['controls'][0], {'testid':'beast-core-select-htmlInput','value':'支出'}]}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                fees._check_filters(changed, 'shipping', self.start, self.end)

    def workbook(self, root, rows, *, shipping=False):
        path = Path(root) / 'fees.xlsx'
        book = Workbook()
        headers = (['包裹号','运单号','账单类型','币种','对账单状态','支出/退款时间(时区：GMT+8)'] if shipping else
                   ['账务单ID','运单号','PO单号','账单类型','币种','账单支出/退款时间'])
        book.active.append(headers)
        for row in rows:
            book.active.append(row)
        book.save(path)
        book.close()
        return path

    def test_workbook_requires_all_rows_dates_currency_and_expected_type(self):
        with TemporaryDirectory() as tmp:
            path = self.workbook(tmp, [['001','002','PO-003','支出','USD','2026-09-16 06:23:47.332']])
            self.assertEqual(fees.validate_fee_workbook(path, 'return_label', self.start, self.end, 1, 'USD'), 1)
            for kind, count, currency in [('return_label', 2, 'USD'), ('return_label', 1, 'THB'), ('shipping', 1, 'USD')]:
                with self.subTest(kind=kind, count=count), self.assertRaises(ValueError):
                    fees.validate_fee_workbook(path, kind, self.start, self.end, count, currency)
            path = self.workbook(tmp, [['001','002','PO-003','支出','USD','2026-10-01 00:00:00']])
            with self.assertRaisesRegex(ValueError, '范围外'):
                fees.validate_fee_workbook(path, 'return_label', self.start, self.end, 1, 'USD')

    def test_shipping_workbook_accepts_native_excel_dates(self):
        with TemporaryDirectory() as tmp:
            path = self.workbook(tmp, [['PK-001','002','支出','USD','已出账',datetime(2026,9,30,23,59,59)]], shipping=True)
            self.assertEqual(fees.validate_fee_workbook(path, 'shipping', self.start, self.end, 1, 'USD'), 1)


class FeeQueryDomTests(TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest('Playwright unavailable')
        cls.runtime = sync_playwright().start()
        cls.addClassCleanup(cls.runtime.stop)
        try:
            cls.browser = cls.runtime.chromium.launch(headless=True, channel='msedge')
        except Error:
            cls.browser = cls.runtime.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        # Fulfil locally: this verifies location/readback without contacting a
        # TEMU account or issuing a real business query.
        html = '''<div><div><label>账单支出/退款时间</label><input readonly data-testid="beast-core-rangePicker-htmlInput" value="2026-09-01 ~ 2026-09-30"></div><input value=""><button id="query">查询</button></div>
        <section class="dashboard_container__fixture"><div>币种</div><div>USD</div><div>账单数</div><div id="count">12</div><div>总金额</div><div>$0.00</div></section>
        <script>document.querySelector('#query').onclick=()=>{window.queryCount=(window.queryCount||0)+1;if(window.silent)return;const panel=document.querySelector('section');panel.setAttribute('aria-busy','true');setTimeout(()=>{document.querySelector('#count').textContent='0';panel.removeAttribute('aria-busy')},30)}</script>'''
        self.page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8', body=html))
        self.page.goto('https://agentseller.temu.com/labor/stml-logistics')
        self.gateway = SimpleNamespace(browser=BrowserFixture(self.page))

    def query(self):
        return fees.query_summary(self.gateway, 'store', 'shipping', date(2026,9,1), date(2026,9,30), timeout=.5, settle_ms=30)

    def test_query_is_submitted_once_and_waits_for_observed_zero_count(self):
        self.assertEqual(self.query()['bill_count'], 0)
        self.assertEqual(self.page.evaluate('window.queryCount'), 1)
        self.assertEqual(self.page.evaluate("Object.keys(window).filter(k=>k.startsWith('__hqyl_fee_query_'))"), [])

    def test_silent_query_never_uses_previous_count(self):
        self.page.evaluate('window.silent=true')
        with self.assertRaisesRegex(ValueError, '未确认'):
            self.query()
        self.assertEqual(self.page.evaluate('window.queryCount'), 1)

    def test_observed_history_dom_keeps_each_task_and_download_state(self):
        self.page.set_content('''<div data-testid="beast-core-drawer"><h3>导出 <span>刷新</span></h3><div class="export-history_exportHistoryList__cb3dx"><div class="export-history_list__5Eto0"><div class="export-history_title__x">导出任务生成时间 <svg></svg>2026-10-27 09:53:33</div><div class="export-history_middle__x">账单支出/退款时间: 2026-09-01 ~ 2026-09-30</div><button disabled data-testid="beast-core-button"><svg></svg><span>下载数据报表</span></button></div></div></div>''')
        snapshot = self.gateway.browser.evaluate('store', fees.HISTORY_STATE)
        self.assertTrue(snapshot['open'])
        self.assertEqual(snapshot['rows'][0]['generated'], '2026-10-27 09:53:33')
        self.assertEqual(snapshot['rows'][0]['condition'], '账单支出/退款时间: 2026-09-01 ~ 2026-09-30')
        self.assertTrue(snapshot['rows'][0]['downloads'][0]['disabled'])
        self.assertEqual(len(snapshot['refresh']), 1)

    def test_history_download_selector_survives_temporary_body_sibling_removal(self):
        # Real pages insert a toast before the drawer. Removing the toast after
        # capture installation must not redirect a saved download selector.
        self.page.set_content('''<div class="temporary-toast">导出已提交</div>
        <div><button data-testid="beast-core-button" onclick="window.wrong=true">其他按钮</button></div>
        <div data-testid="beast-core-drawer"><h3>导出历史 <span>刷新</span></h3>
          <div class="export-history_exportHistoryList__fixture">
            <div class="export-history_list__fixture"><div class="export-history_title__fixture">导出任务生成时间 2026-10-27 09:53:33</div>
              <div class="export-history_middle__fixture">账单支出/退款时间: 2026-09-01 ~ 2026-09-30</div>
              <button data-testid="beast-core-button" onclick="window.downloaded='current'">下载数据报表</button></div>
            <div class="export-history_list__fixture"><div class="export-history_title__fixture">导出任务生成时间 2026-10-26 09:53:33</div>
              <div class="export-history_middle__fixture">账单支出/退款时间: 2026-08-01 ~ 2026-08-31</div>
              <button data-testid="beast-core-button" onclick="window.downloaded='old'">下载数据报表</button></div>
          </div>
        </div>''')
        snapshot = self.gateway.browser.evaluate('store', fees.HISTORY_STATE)
        selector = snapshot['rows'][0]['downloads'][0]['selector']
        self.assertTrue(selector.startswith('[data-testid="beast-core-drawer"]'), selector)
        self.page.locator('.temporary-toast').evaluate('e=>e.remove()')
        self.gateway.browser.click('store', selector)
        self.assertEqual(self.page.evaluate('window.downloaded'), 'current')
        self.assertIsNone(self.page.evaluate('window.wrong'))

    def test_query_waits_for_nested_beast_loading_indicator_to_finish(self):
        self.page.evaluate('''() => {
          document.querySelector('#query').onclick=()=>{
            window.queryCount=(window.queryCount||0)+1;
            const loading=document.createElement('span');loading.dataset.testid='beast-core-spin';loading.textContent='加载中';
            document.querySelector('section').append(loading);
            setTimeout(()=>{document.querySelector('#count').textContent='0';loading.remove()},120);
          };
        }''')
        result = self.query()
        self.assertEqual(result['bill_count'], 0)
        self.assertTrue(result['saw_loading'])
        self.assertEqual(self.page.evaluate('window.queryCount'), 1)
        self.assertEqual(self.page.locator('[data-testid="beast-core-spin"]').count(), 0)

    def test_refreshed_placeholder_summary_requires_exact_empty_table(self):
        for show_empty in (True, False):
            with self.subTest(show_empty=show_empty):
                self.page.reload()
                self.page.evaluate('''showEmpty => {
                  document.querySelector('#query').onclick=()=>{
                    const panel=document.querySelector('section');panel.setAttribute('aria-busy','true');
                    setTimeout(()=>{
                      panel.innerHTML='<div>币种</div><div>--</div><div>账单数</div><div>--</div><div>总金额</div><div>--</div>';
                      if(showEmpty){const table=document.createElement('div');table.dataset.testid='beast-core-table-middle-body';table.textContent='暂无数据';document.body.append(table)}
                      panel.removeAttribute('aria-busy');
                    },30);
                  };
                }''', show_empty)
                if show_empty:
                    result = self.query()
                    self.assertEqual(result['bill_count'], 0)
                    self.assertTrue(result['empty_table'])
                else:
                    with self.assertRaisesRegex(ValueError, '无法确认'):
                        self.query()

    def test_empty_table_evidence_rejects_table_rows_and_multiple_tables(self):
        self.page.set_content('<div data-testid="beast-core-table-middle-body">暂无数据<table><tbody><tr><td></td></tr></tbody></table></div>')
        self.assertFalse(self.gateway.browser.evaluate('store', fees.SUMMARY_STATE)['empty'])
        self.page.set_content('<div data-testid="beast-core-table-middle-body">暂无数据</div><div data-testid="beast-core-table-middle-body">暂无数据</div>')
        self.assertFalse(self.gateway.browser.evaluate('store', fees.SUMMARY_STATE)['empty'])

    def test_observed_nested_dashboard_cards_keep_label_value_pairs(self):
        # Structure captured from the native store on 2026-10-29. Only synthetic
        # values are substituted; cards are reordered to prove label association.
        self.page.set_content('''<div class="dashboard_container__dQA4I">
          <div style="flex: 1 1 0%;"><div class="dashboard_titleWrapper__JMOiF">总金额</div>
            <div class="dashboard_valueWrapper__WZZIY"><div class="dashboard_valueItem__rocz8"><span>- $</span><span>1,382.02</span><div class="dashboard_bottomNode__mMtTZ"></div></div></div></div>
          <div style="flex: 1 1 0%;"><div class="dashboard_titleWrapper__JMOiF">账单数</div>
            <div class="dashboard_valueWrapper__WZZIY"><div class="dashboard_valueItem__rocz8">1,613<div class="dashboard_bottomNode__mMtTZ"></div></div></div></div>
          <div style="flex: 1 1 0%;"><div class="dashboard_titleWrapper__JMOiF">币种</div>
            <div class="dashboard_valueWrapper__WZZIY"><div class="dashboard_valueItem__rocz8">USD<div class="dashboard_bottomNode__mMtTZ"></div></div></div></div>
          <div hidden>币种 USD</div></div>''')
        state = self.gateway.browser.evaluate('store', fees.SUMMARY_STATE)
        self.assertEqual(state['metrics'], [{'币种':'USD', '账单数':'1,613', '总金额':'-$1,382.02'}])
        summary = fees.parse_summary(state['panels'][0], fields=state['metrics'][0])
        self.assertEqual(summary['bill_count'], 1613)

    def test_query_waits_past_a_settled_placeholder_before_data_arrives(self):
        self.page.evaluate('''() => {
          document.querySelector('#query').onclick=()=>{
            window.queryCount=(window.queryCount||0)+1;
            const panel=document.querySelector('section');
            panel.innerHTML='<div>币种</div><div>--</div><div>账单数</div><div>--</div><div>总金额</div><div>--</div>';
            setTimeout(()=>{panel.innerHTML='<div>币种</div><div>USD</div><div>账单数</div><div>13</div><div>总金额</div><div>- $17.39</div>'},220);
          };
        }''')
        result = self.query()
        self.assertEqual(result['bill_count'], 13)
        self.assertEqual(self.page.evaluate('window.queryCount'), 1)

    def test_empty_placeholder_is_not_zero_while_table_loading_outside_dashboard(self):
        self.page.evaluate('''() => {
          const table=document.createElement('div');table.dataset.testid='beast-core-table-middle-body';table.textContent='暂无数据';document.body.append(table);
          document.querySelector('#query').onclick=()=>{
            const panel=document.querySelector('section');
            const loading=document.createElement('div');loading.dataset.testid='beast-core-spin';loading.textContent='加载中';document.body.append(loading);
            panel.innerHTML='<div>币种</div><div>--</div><div>账单数</div><div>--</div><div>总金额</div><div>--</div>';
            setTimeout(()=>{table.innerHTML='<table><tbody><tr><td>fixture row</td></tr></tbody></table>';
              panel.innerHTML='<div>币种</div><div>USD</div><div>账单数</div><div>1</div><div>总金额</div><div>- $1.00</div>';loading.remove()},220);
          };
        }''')
        self.assertEqual(self.query()['bill_count'], 1)

    def test_unchanged_placeholder_summary_uses_only_its_fresh_empty_table(self):
        for mode in ('text', 'replace', 'loading_only'):
            with self.subTest(mode=mode):
                self.page.reload()
                self.page.evaluate('''mode => {
                  const panel=document.querySelector('section');
                  panel.innerHTML='<div>币种</div><div>--</div><div>账单数</div><div>--</div><div>总金额</div><div>--</div>';
                  const table=document.createElement('div');table.dataset.testid='beast-core-table-middle-body';
                  table.textContent=mode==='loading_only'?'暂无数据':'old fixture result';document.body.append(table);
                  document.querySelector('#query').onclick=()=>{
                    window.queryCount=(window.queryCount||0)+1;
                    table.setAttribute('aria-busy','true');
                    setTimeout(()=>{
                      if(mode==='replace'){const next=table.cloneNode(false);next.textContent='暂无数据';next.removeAttribute('aria-busy');table.replaceWith(next)}
                      else{if(mode==='text')table.textContent='暂无数据';table.removeAttribute('aria-busy')}
                    },120);
                  };
                }''', mode)
                result = self.query()
                self.assertEqual(result['bill_count'], 0)
                self.assertTrue(result['empty_table'])
                self.assertEqual(self.page.evaluate('window.queryCount'), 1)
                self.assertEqual(self.page.evaluate("Object.keys(window).filter(k=>k.startsWith('__hqyl_fee_query_'))"), [])

    def test_unrelated_toast_cannot_make_an_old_empty_table_fresh(self):
        self.page.evaluate('''() => {
          document.querySelector('section').innerHTML='<div>币种</div><div>--</div><div>账单数</div><div>--</div><div>总金额</div><div>--</div>';
          const table=document.createElement('div');table.dataset.testid='beast-core-table-middle-body';table.textContent='暂无数据';document.body.append(table);
          document.querySelector('#query').onclick=()=>{const toast=document.createElement('div');toast.textContent='fixture toast';document.body.append(toast)};
        }''')
        with self.assertRaisesRegex(ValueError, '未确认面单汇总刷新'):
            self.query()
