"""Deterministic Lazada balance collection tests without a real store browser."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from backend.services import lazada_balance_collector as collector


class _Element:
    def __init__(self, text: str, callback) -> None:
        self.text = text
        self._callback = callback

    def is_displayed(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    def click(self) -> None:
        self._callback()


class _Driver:
    def __init__(self, country="PH", *, status="发起提现 | 银行流水号 ABC", unavailable=False) -> None:
        self.country = country
        self.current_url = f"https://{collector.COUNTRY_DOMAINS[country]}{collector.PATHS['income']}"
        self.status = status
        self.unavailable = unavailable
        self.filter_active = False
        self.maximized = False
        self.metrics = {
            "income": {"label": "待释放", "text": "₱ 9,782.76", "located": True},
            "balance": {"label": "", "text": "PHP 0.00", "located": True},
            "ads": {"label": "可用余额", "text": "菲律宾比索 1,065.69", "located": True},
        }
        self.rows = [
            ["1", "28 Sep 2026 08:22:30", "提现", "自动提现", "-23,014.94", status],
            ["2", "21 Sep 2026 08:22:11", "提现", "自动提现", "-16,933.12", "提现成功 | 银行流水号 DEF"],
        ]
        self.date_start = (date.today() - timedelta(days=184)).isoformat()
        self.date_end = date.today().isoformat()
        self.scroll_visible = True

    def get(self, url: str) -> None:
        self.current_url = url
        self.filter_active = False
        if self.unavailable and url.endswith("/apps/balance"):
            self.current_url += "/unaccessable"

    def maximize_window(self) -> None:
        self.maximized = True

    def find_elements(self, by: str, selector: str):
        if selector == ".cap-type button.button-groups":
            return [_Element("提现", lambda: setattr(self, "filter_active", True))]
        return []

    def execute_script(self, script: str, *args):
        if "lazada_balance_metric" in script:
            return dict(self.metrics[args[0]])
        if "lazada_balance_transactions" in script:
            return {
                "filter_present": True,
                "filter_active": self.filter_active,
                "rows": list(self.rows) if self.filter_active else list(self.rows[:1]),
                "empty": not self.rows,
                "loading": False,
                "date_start": self.date_start,
                "date_end": self.date_end,
                "page_number": 1 if self.rows else 0,
                "total_count": len(self.rows),
            }
        if "lazada_balance_evidence_scroll" in script:
            return self.scroll_visible
        if script == collector.ADS_CHECK:
            return self.scroll_visible
        if "lazada_balance_view_check" in script:
            return self.scroll_visible
        if script.startswith("return document.body"):
            return "feature is in pilot phase and not accessible" if self.unavailable else "我的收入 我的余额 余额流水"
        raise AssertionError(f"Unexpected script: {script[:80]}")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class LazadaBalanceCollectorTests(unittest.TestCase):
    def test_ads_capture_uses_prepared_view_and_read_only_checks_before_restoring(self):
        events = []
        driver = _Driver()
        execute = driver.execute_script

        def checked(script, *args):
            if script == collector.ADS_CHECK:
                events.append('check')
            elif 'lazada_balance_evidence_scroll' in script:
                self.fail('Ads verification must not execute the scrolling script')
            return execute(script, *args)

        driver.execute_script = checked

        @contextmanager
        def view(_driver):
            events.append('prepare')
            try:
                yield
            finally:
                events.append('restore')

        def capture(_driver, path, *_args):
            events.append('capture')
            return str(path)

        with tempfile.TemporaryDirectory() as folder, patch.object(collector, 'ads_evidence_view', side_effect=view), patch.object(collector, '_dismiss_ads_guides'), patch.object(collector, 'time', _Clock()), patch.object(collector, 'capture_balance_evidence', side_effect=capture):
            output = collector._evidence(driver, Path(folder), {'store_name':'fixture','store_id':'123'}, 'ads', 'stamp')
        self.assertTrue(output.endswith('_ads.png'))
        self.assertEqual(events, ['prepare', 'check', 'check', 'capture', 'check', 'restore'])

    def test_ads_move_during_capture_is_rejected_without_repreparing_view(self):
        driver = _Driver()
        events = []

        @contextmanager
        def view(_driver):
            events.append('prepare')
            try:
                yield
            finally:
                events.append('restore')

        def capture(_driver, path, *_args):
            events.append('capture')
            driver.scroll_visible = False
            return str(path)

        with tempfile.TemporaryDirectory() as folder, patch.object(collector, 'ads_evidence_view', side_effect=view), patch.object(collector, '_dismiss_ads_guides'), patch.object(collector, 'time', _Clock()), patch.object(collector, 'capture_balance_evidence', side_effect=capture):
            with self.assertRaises(collector.LazadaBalanceError):
                collector._evidence(driver, Path(folder), {'store_name':'fixture','store_id':'123'}, 'ads', 'stamp')
        self.assertEqual(events, ['prepare', 'capture', 'restore'])

    def test_ads_amount_change_during_capture_keeps_value_but_rejects_picture(self):
        driver = _Driver()
        def capture(active_driver, path, _name, label):
            if label.startswith('Ads'):
                active_driver.metrics['ads']['text'] = 'PHP -3.52'
            return str(path)
        result = self._collect(driver, capture=capture)
        self.assertEqual(result['values']['ads'], '1065.69')
        self.assertNotIn('ads', result['evidence'])
        self.assertIn('截图期间变化', result['field_errors']['ads'])
        self.assertIn('balance', result['evidence'])
        self.assertIn('processing', result['evidence'])

    def test_ads_specific_visibility_failure_preserves_other_fields(self):
        original_evidence = collector._evidence
        def evidence(driver, folder, store, field, stamp, **kwargs):
            if field == 'ads':
                raise collector.LazadaAdsViewError('Ads 标签或金额被纵向滚动区域裁剪')
            return original_evidence(driver, folder, store, field, stamp, **kwargs)
        with patch.object(collector, '_evidence', side_effect=evidence):
            result = self._collect(_Driver())
        self.assertEqual(result['field_errors']['ads'], 'Ads 标签或金额被纵向滚动区域裁剪')
        self.assertEqual(result['values']['ads'], '1065.69')
        self.assertNotIn('ads', result['evidence'])
        self.assertIn('income', result['evidence'])
        self.assertIn('balance', result['evidence'])
        self.assertIn('processing', result['evidence'])

    def test_full_page_layout_retry_restores_view_before_preparing_again(self):
        events = []
        @contextmanager
        def view(_driver):
            events.append('prepare')
            try:
                yield
            finally:
                events.append('restore')
        def capture(_driver, path, *_args):
            events.append('capture')
            if events.count('capture') == 1:
                raise collector.BalanceLayoutChangedError('layout changed')
            return str(path)
        with tempfile.TemporaryDirectory() as folder, patch.object(collector, 'balance_page_view', side_effect=view), patch.object(collector, 'time', _Clock()), patch.object(collector, 'capture_balance_page_evidence', side_effect=capture):
            output = collector._evidence(_Driver(), Path(folder), {'store_name':'fixture','store_id':'123'}, 'balance', 'stamp')
        self.assertTrue(output.endswith('_balance.png'))
        self.assertEqual(events, ['prepare','capture','restore','prepare','capture','restore'])

    def test_layout_retry_is_bounded_and_other_capture_errors_are_not_retried(self):
        for error, expected in ((collector.BalanceLayoutChangedError('layout changed'), 3),
                                (collector.BalanceEvidenceError('identity changed'), 1)):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as folder, patch.object(collector, 'balance_page_view', side_effect=lambda _: nullcontext()), patch.object(collector, 'time', _Clock()), patch.object(collector, 'capture_balance_page_evidence', side_effect=error) as capture:
                with self.assertRaises(collector.BalanceEvidenceError):
                    collector._evidence(_Driver(), Path(folder), {'store_name':'fixture','store_id':'123'}, 'balance', 'stamp')
                self.assertEqual(capture.call_count, expected)

    def test_amount_change_during_layout_retry_cannot_be_accepted(self):
        calls = 0
        def capture(driver, path, _name, label):
            nonlocal calls
            if label.startswith('Balance'):
                calls += 1
                if calls == 1:
                    driver.metrics['balance']['text'] = 'PHP 12.00'
                    raise collector.BalanceLayoutChangedError('layout changed')
            return str(path)
        result = self._collect(_Driver(status='提现成功'), capture=capture)
        self.assertEqual(calls, 2)
        self.assertNotIn('balance', result['evidence'])
        self.assertIn('总计余额在截图期间变化', result['field_errors']['balance'])

    def _collect(self, driver: _Driver, *, store=None, capture=None, language_result=None) -> dict:
        store = store or {"country": driver.country, "store_id": "123", "store_name": "测试店铺"}
        with tempfile.TemporaryDirectory() as folder:
            def open_page(target_driver, target_url, _state):
                target_driver.get(target_url)
                return "fake-business-tab"
            with patch.object(collector, "capture_balance_evidence", side_effect=capture or (lambda _driver, path, _name, _label: str(path))), patch.object(collector, "capture_balance_page_evidence", side_effect=capture or (lambda _driver, path, _name, _label: str(path))), patch.object(collector, "time", _Clock()), patch.object(collector, "ensure_lazada_page", side_effect=open_page), patch.object(collector, "_dismiss_ads_guides"), patch.object(collector, "balance_page_view", side_effect=lambda _: nullcontext()), patch.object(collector, "ads_evidence_view", side_effect=lambda _: nullcontext()), patch.object(collector, "ensure_lazada_chinese", return_value=language_result or {'language': 'zh_CN', 'changed': False, 'fallback': False, 'reason': ''}):
                return collector.collect_lazada_balance(driver, store, Path(folder))

    def test_explicit_english_fallback_is_recorded_for_each_read(self):
        result = self._collect(_Driver(), language_result={'language': 'en', 'changed': False, 'fallback': True, 'reason': '当前明确为 English，语言菜单未提供简体中文选项'})
        self.assertEqual(len([note for note in result['notes'] if 'English' in note]), 4)
        self.assertFalse(result['field_errors'])

    def test_ads_known_overlays_are_dismissed_before_language_switch(self):
        driver = _Driver()
        calls = []
        def open_page(_driver, url, _state):
            calls.append('open')
            _driver.get(url)
        with patch.object(collector, 'ensure_lazada_page', side_effect=open_page), patch.object(collector, '_dismiss_ads_guides', side_effect=lambda _: calls.append('dismiss')), patch.object(collector, 'ensure_lazada_chinese', side_effect=lambda _: calls.append('language') or {'language': 'zh_CN'}):
            collector._open_page(driver, 'PH', 'ads', {})
        self.assertEqual(calls, ['open', 'dismiss', 'language'])

    def test_four_current_values_pending_latest_withdrawal_and_evidence(self) -> None:
        driver = _Driver()
        result = self._collect(driver)
        self.assertTrue(driver.maximized)
        self.assertEqual(result["values"], {
            "income": "9782.76", "balance": "0.00", "ads": "1065.69", "processing": "-23014.94",
        })
        self.assertEqual(result["currency"], "PHP")
        self.assertEqual(set(result["evidence"]), {"income", "balance", "ads", "processing"})
        self.assertEqual(result["field_errors"], {})
        self.assertEqual(result["evidence_exemptions"], {})
        self.assertTrue(all("?" not in url for url in result["source_urls"].values()))

    def test_successful_latest_withdrawal_is_blank_with_verified_status_picture(self) -> None:
        driver = _Driver(status="提现成功 | 银行流水号 ABC")
        result = self._collect(driver)
        self.assertIsNone(result["values"]["processing"])
        self.assertEqual(result["evidence"]["processing"], result["evidence"]["balance"])
        self.assertEqual(result["evidence_exemptions"]["processing"], "withdrawal_success")
        self.assertTrue(any('状态截图复用' in note for note in result['notes']))

    def test_explicit_empty_withdrawal_filter_is_blank_with_verified_status_picture(self) -> None:
        driver = _Driver()
        driver.rows = []
        result = self._collect(driver)
        self.assertIsNone(result["values"]["processing"])
        self.assertEqual(result["evidence"]["processing"], result["evidence"]["balance"])
        self.assertEqual(result["evidence_exemptions"]["processing"], "no_withdrawal")
        self.assertNotIn("processing", result["field_errors"])

    def test_success_or_empty_independently_captures_full_page_when_balance_picture_fails(self) -> None:
        for empty in (False, True):
            with self.subTest(empty=empty):
                driver = _Driver(status='提现成功')
                if empty:
                    driver.rows = []
                original = driver.execute_script
                scripts = []

                def execute(script, *args):
                    if args == ('processing',):
                        scripts.append(script)
                    return original(script, *args)

                driver.execute_script = execute

                def capture(_driver, path, _name, label):
                    if label.startswith('Balance'):
                        raise RuntimeError('Balance image unavailable')
                    return str(path)

                result = self._collect(driver, capture=capture)
                self.assertIn('balance', result['field_errors'])
                self.assertIsNone(result['values']['processing'])
                self.assertTrue(result['evidence']['processing'].endswith('_processing.png'))
                self.assertEqual(result['evidence_exemptions']['processing'], 'no_withdrawal' if empty else 'withdrawal_success')
                self.assertNotIn('processing', result['field_errors'])
                self.assertTrue(scripts)
                self.assertTrue(all('lazada_balance_view_check' in script for script in scripts))

    def test_success_or_empty_without_status_picture_is_an_error(self) -> None:
        for empty in (False, True):
            with self.subTest(empty=empty):
                driver = _Driver(status='提现成功')
                if empty:
                    driver.rows = []

                def capture(_driver, path, _name, label):
                    if label.startswith(('Balance', 'processing')):
                        raise RuntimeError('image unavailable')
                    return str(path)

                result = self._collect(driver, capture=capture)
                self.assertIsNone(result['values']['processing'])
                self.assertIn('processing', result['field_errors'])
                self.assertNotIn('processing', result['evidence'])
                self.assertNotIn('processing', result['evidence_exemptions'])

    def test_changed_empty_state_during_independent_picture_rejects_evidence(self) -> None:
        driver = _Driver()
        driver.rows = []

        def capture(active_driver, path, _name, label):
            if label.startswith('Balance'):
                raise RuntimeError('Balance image unavailable')
            if label.startswith('processing'):
                active_driver.rows = [['1', '28 Sep 2026 08:22:30', '提现', '自动提现', '-100.00', '发起提现']]
            return str(path)

        result = self._collect(driver, capture=capture)
        self.assertIn('提现状态在截图核对期间变化', result['field_errors']['processing'])
        self.assertNotIn('processing', result['evidence'])
        self.assertNotIn('processing', result['evidence_exemptions'])

    def test_reused_balance_picture_requires_state_to_remain_unchanged(self) -> None:
        driver = _Driver(status='提现成功')
        original = collector._latest_withdrawal

        def status(snapshot, country):
            outcome = original(snapshot, country)
            driver.rows[0] = ['1', '28 Sep 2026 08:22:30', '提现', '自动提现', '-100.00', '发起提现']
            return outcome

        with patch.object(collector, '_latest_withdrawal', side_effect=status):
            result = self._collect(driver)
        self.assertIn('balance', result['evidence'])
        self.assertIn('提现状态在截图核对期间变化', result['field_errors']['processing'])
        self.assertNotIn('processing', result['evidence'])
        self.assertNotIn('processing', result['evidence_exemptions'])

    def test_unknown_withdrawal_status_fails_closed(self) -> None:
        driver = _Driver(status="银行状态待确认")
        result = self._collect(driver)
        self.assertIsNone(result["values"]["processing"])
        self.assertIn("processing", result["field_errors"])
        self.assertIn("balance", result["evidence"])

    def test_balance_and_processing_are_captured_before_ads_can_block(self) -> None:
        driver = _Driver()
        original_get = driver.get

        def get(url):
            if collector.PATHS['ads'] in url:
                driver.current_url = 'https://sellercenter.lazada.com.ph/apps/seller/login'
                raise collector.LazadaLoginError('店铺尚未登录')
            original_get(url)

        driver.get = get
        result = self._collect(driver)
        self.assertIn('balance', result['evidence'])
        self.assertIn('processing', result['evidence'])
        self.assertNotIn('balance', result['field_errors'])

    def test_balance_picture_waits_for_verified_withdrawal_filter(self) -> None:
        driver = _Driver()
        observed = []

        def capture(active_driver, path, _name, label):
            if label.startswith("Balance"):
                observed.append(active_driver.filter_active)
            return str(path)

        result = self._collect(driver, capture=capture)
        self.assertEqual(observed, [True])
        self.assertIn("balance", result["evidence"])

    def test_changed_balance_amount_rejects_its_picture_and_keeps_other_fields(self) -> None:
        driver = _Driver()

        def capture(active_driver, path, _name, label):
            if label.startswith("Balance"):
                active_driver.metrics["balance"]["text"] = "PHP 1.00"
            return str(path)

        result = self._collect(driver, capture=capture)
        self.assertNotIn("balance", result["evidence"])
        self.assertIn("截图期间变化", result["field_errors"]["balance"])
        self.assertEqual(result["values"]["processing"], "-23014.94")

    def test_changed_withdrawal_table_rejects_balance_picture_and_rereads_processing(self) -> None:
        driver = _Driver()

        def capture(active_driver, path, _name, label):
            if label.startswith("Balance"):
                active_driver.rows[0] = ["1", "28 Sep 2026 08:22:30", "提现", "自动提现", "-100.00", "发起提现"]
            return str(path)

        result = self._collect(driver, capture=capture)
        self.assertNotIn("balance", result["evidence"])
        self.assertIn("提现列表在截图期间变化", result["field_errors"]["balance"])
        self.assertEqual(result["values"]["processing"], "-100.00")

    def test_cross_border_unavailable_is_the_only_zero_exemption(self) -> None:
        driver = _Driver(unavailable=True)
        result = self._collect(driver, store={
            "country": "PH", "store_name": "跨境菲律宾测试店", "store_id": "123",
            "platform_name": "Lazada-全球(CN)",
        })
        self.assertEqual(result["values"]["balance"], "0.00")
        self.assertEqual(result["evidence_exemptions"]["balance"], "cross_border_unavailable")
        self.assertNotIn("balance", result["evidence"])
        self.assertIn("processing", result["field_errors"])

    def test_local_unavailable_never_becomes_zero(self) -> None:
        driver = _Driver(unavailable=True)
        result = self._collect(driver)
        self.assertIsNone(result["values"]["balance"])
        self.assertIn("balance", result["field_errors"])

    def test_screenshot_failure_keeps_read_amount_but_marks_error(self) -> None:
        driver = _Driver()
        def capture(_driver, path, _name, label):
            if label.startswith("Balance"):
                raise RuntimeError("native window clipped")
            return str(path)
        result = self._collect(driver, capture=capture)
        self.assertEqual(result["values"]["balance"], "0.00")
        self.assertIn("balance", result["field_errors"])
        self.assertNotIn("balance", result["evidence"])
        self.assertEqual(result["values"]["processing"], "-23014.94")

    def test_shortened_date_range_rejects_latest_withdrawal_claim(self) -> None:
        driver = _Driver()
        driver.date_start = (date.today() - timedelta(days=10)).isoformat()
        result = self._collect(driver)
        self.assertIn("processing", result["field_errors"])
        self.assertIsNone(result["values"]["processing"])
        self.assertIn("balance", result["field_errors"])
        self.assertNotIn("balance", result["evidence"])

    def test_currency_mismatch_never_returns_a_value(self) -> None:
        driver = _Driver()
        driver.metrics["ads"]["text"] = "USD 1,065.69"
        result = self._collect(driver)
        self.assertIsNone(result["values"]["ads"])
        self.assertIn("ads", result["field_errors"])

    def test_amount_parsing_for_indonesia_and_vietnam(self) -> None:
        self.assertEqual(collector._decimal_amount("印尼盾 122,729", "ID"), collector.Decimal("122729"))
        self.assertEqual(collector._decimal_amount("Rp 1.234.567,89", "ID"), collector.Decimal("1234567.89"))
        self.assertEqual(collector._decimal_amount("越南盾 282,837", "VN"), collector.Decimal("282837"))
        self.assertEqual(collector._decimal_amount("4,408,214.00 ₫", "VN"), collector.Decimal("4408214.00"))

    def test_scoped_loading_rejects_a_stable_zero_placeholder(self) -> None:
        driver = _Driver()
        driver.current_url = f"https://{collector.COUNTRY_DOMAINS['PH']}{collector.PATHS['balance']}"
        original = driver.execute_script
        calls = 0

        def scripted(script, *args):
            nonlocal calls
            if "lazada_balance_metric" in script and args == ("balance",):
                calls += 1
                return {"label": "", "text": "PHP 0.00" if calls < 9 else "PHP 1,234.56",
                        "located": True, "loading": calls < 9}
            return original(script, *args)

        driver.execute_script = scripted
        with patch.object(collector, "time", _Clock()):
            self.assertEqual(collector._metric(driver, "balance"), "PHP 1,234.56")
        self.assertGreaterEqual(calls, 12)


if __name__ == "__main__":
    unittest.main()
