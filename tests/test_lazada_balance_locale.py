"""Vietnamese balance-page controls observed in the 2026-09-30 store run."""
from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import patch

from backend.services import lazada_balance_collector as collector

try:
    from playwright.sync_api import Error, sync_playwright
except ImportError:
    sync_playwright = None


FIXTURE = """
<!doctype html><html><body>
<button id="payout">Rút tiền</button>
<section class="balance-transactions">
  <div class="cap-type">
    <button class="button-groups next-btn-normal">Nhận tiền</button>
    <button class="button-groups next-btn-normal">Rút tiền</button>
    <button class="button-groups next-btn-normal">Thanh toán</button>
    <button class="button-groups next-btn-normal">Phạt</button>
    <button class="button-groups next-btn-normal">Điều chỉnh</button>
  </div>
  <input placeholder="Ngày bắt đầu" value="2026-03-30">
  <input placeholder="Ngày kết thúc" value="2026-09-30">
  <table>
    <thead><tr><th>Mã số giao dịch</th><th>Thời gian giao dịch</th>
      <th>Loại giao dịch</th><th>Chi tiết</th><th>Số tiền</th><th>Ghi chú</th></tr></thead>
    <tbody>
      <tr><td>id1</td><td>28 Sep 2026 10:21:04</td><td>Rút tiền</td>
        <td>Rút tiền tự động</td><td>-3,736,567.00</td>
        <td>Đã thanh toán | Số Thanh Toán Ngân Hàng …<br>
        Khoản thanh toán của bạn đã được Lazada xử lý và sẽ được ngân hàng thụ hưởng trả vào tài khoản ngân hàng của bạn.
        Nếu bạn không nhận được khoản thanh toán sau 2 ngày làm việc, vui lòng liên hệ với ngân hàng thụ hưởng để được hỗ trợ.<br>
        142.96USD(1VND = 0.00003826USD)</td></tr>
      <tr><td>id2</td><td>21 Sep 2026 10:21:07</td><td>Rút tiền</td>
        <td>Rút tiền tự động</td><td>-4,678,958.00</td>
        <td>Đã thanh toán | Số Thanh Toán Ngân Hàng …</td></tr>
    </tbody>
  </table>
  <span class="next-pagination-total">Tổng số: 11</span>
  <div class="next-pagination-list"><button class="next-current">1</button></div>
</section>
<script>
window.clicks={filters:[],trusted:[],payout:0};
document.querySelector('#payout').onclick=()=>window.clicks.payout++;
document.querySelectorAll('.cap-type button').forEach(button=>button.onclick=event=>{
  window.clicks.filters.push(button.innerText);window.clicks.trusted.push(event.isTrusted);
  document.querySelectorAll('.cap-type button').forEach(item=>item.className='button-groups next-btn-normal');
  button.className='button-groups next-btn-primary';
});
</script></body></html>
"""


class _Element:
    def __init__(self, locator):
        self.locator = locator

    @property
    def text(self):
        return self.locator.inner_text()

    def is_displayed(self):
        return self.locator.is_visible()

    def click(self):
        self.locator.click()


class _Driver:
    def __init__(self, page):
        self.page = page

    def find_elements(self, by, selector):
        if by != "css selector":
            raise AssertionError(by)
        locator = self.page.locator(selector)
        return [_Element(locator.nth(index)) for index in range(locator.count())]

    def execute_script(self, script):
        return self.page.evaluate("script => Function(script)()", script)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _ObservedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 30)


class LazadaBalanceLocaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise RuntimeError("Playwright is required for the locale DOM regression")
        cls.runtime = sync_playwright().start()
        cls.addClassCleanup(cls.runtime.stop)
        try:
            cls.browser = cls.runtime.chromium.launch(headless=True, channel="msedge")
        except Error:
            cls.browser = cls.runtime.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(1000)
        self.page.set_content(FIXTURE)
        self.driver = _Driver(self.page)
        clock = patch.object(collector, "time", _Clock())
        clock.start()
        self.addCleanup(clock.stop)
        observed_date = patch.object(collector, "date", _ObservedDate)
        observed_date.start()
        self.addCleanup(observed_date.stop)

    def select(self):
        return collector._select_withdrawal_filter(self.driver)

    def set_status(self, text):
        self.page.locator('tbody tr').first.locator('td').nth(5).evaluate(
            '(cell,text)=>cell.innerText=text', text)

    def test_observed_vietnamese_controls_and_paid_status(self):
        snapshot = self.select()
        self.assertEqual(snapshot['date_start'], '2026-03-30')
        self.assertEqual(snapshot['date_end'], '2026-09-30')
        self.assertEqual((snapshot['total_count'], snapshot['page_number']), (11, 1))
        self.assertEqual(snapshot['rows'][0][1:5], [
            '28 Sep 2026 10:21:04', 'Rút tiền', 'Rút tiền tự động', '-3,736,567.00'])
        self.assertEqual(collector._latest_withdrawal(snapshot, 'VN'), (None, 'withdrawal_success'))
        self.assertEqual(self.page.evaluate('window.clicks'), {
            'filters': ['Rút tiền'], 'trusted': [True], 'payout': 0})
        self.select()
        self.assertEqual(self.page.evaluate('window.clicks.filters'), ['Rút tiền'])

    def test_paid_must_match_the_entire_leading_status(self):
        for unknown in ('Chưa thanh toán', 'Chưa Đã thanh toán', 'Đã thanh toán?', 'Đang xử lý'):
            with self.subTest(status=unknown):
                self.set_status(unknown + ' | Đã thanh toán\nwithdrawal successful')
                snapshot = self.select()
                with self.assertRaisesRegex(collector.LazadaBalanceError, '最新提现状态未知'):
                    collector._latest_withdrawal(snapshot, 'VN')

    def test_explanatory_text_does_not_override_leading_paid_status(self):
        self.set_status('Đã thanh toán | Số Thanh Toán Ngân Hàng …\nfailed pending 未收到')
        self.assertEqual(collector._latest_withdrawal(self.select(), 'VN'), (None, 'withdrawal_success'))

    def test_supported_pending_status_keeps_negative_vnd_amount(self):
        self.set_status('Processing | reference\nĐã thanh toán')
        self.assertEqual(collector._latest_withdrawal(self.select(), 'VN'), ('-3736567.00', 'pending'))

    def test_shortened_scope_is_rejected_before_native_filter_click(self):
        self.page.locator('input').first.fill('2026-09-25')
        with self.assertRaisesRegex(collector.LazadaBalanceError, '日期范围未加载'):
            self.select()
        self.assertEqual(self.page.evaluate('window.clicks.filters'), [])

    def test_wrong_transaction_type_is_not_accepted_after_filter_click(self):
        self.page.locator('tbody tr').first.locator('td').nth(2).evaluate(
            "cell=>cell.innerText='Thanh toán'")
        with self.assertRaisesRegex(collector.LazadaBalanceError, '提现筛选后流水未稳定'):
            self.select()
        self.assertEqual(self.page.evaluate('window.clicks.payout'), 0)

    def test_unobserved_vietnamese_empty_state_is_not_inferred_from_zero(self):
        self.page.locator('tbody').evaluate("body=>body.innerHTML=''")
        self.page.locator('.next-pagination-total').evaluate("el=>el.innerText='Tổng số: 0'")
        self.page.locator('table').evaluate("el=>el.insertAdjacentHTML('afterend','<p>Không có dữ liệu</p>')")
        with self.assertRaisesRegex(collector.LazadaBalanceError, '提现筛选后流水未稳定'):
            self.select()
        self.assertFalse(collector._transaction_snapshot(self.driver)['empty'])

    def test_latest_paid_cannot_bypass_transaction_sort_check(self):
        self.page.locator('tbody tr').first.locator('td').nth(1).evaluate(
            "cell=>cell.innerText='14 Sep 2026 10:21:04'")
        with self.assertRaisesRegex(collector.LazadaBalanceError, '未按最新交易时间排序'):
            collector._latest_withdrawal(self.select(), 'VN')


if __name__ == '__main__':
    unittest.main()
