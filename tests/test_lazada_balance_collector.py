"""Deterministic Lazada balance collection tests without a real store browser."""

from __future__ import annotations

import tempfile
import unittest
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
    def _collect(self, driver: _Driver, *, store=None, capture=None, language_result=None) -> dict:
        store = store or {"country": driver.country, "store_id": "123", "store_name": "测试店铺"}
        with tempfile.TemporaryDirectory() as folder:
            def open_page(target_driver, target_url, _state):
                target_driver.get(target_url)
                return "fake-business-tab"
            with patch.object(collector, "capture_balance_evidence", side_effect=capture or (lambda _driver, path, _name, _label: str(path))), patch.object(collector, "time", _Clock()), patch.object(collector, "ensure_lazada_page", side_effect=open_page), patch.object(collector, "_dismiss_ads_guides"), patch.object(collector, "ensure_lazada_chinese", return_value=language_result or {'language': 'zh_CN', 'changed': False, 'fallback': False, 'reason': ''}):
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

    def test_successful_latest_withdrawal_is_blank_without_screenshot(self) -> None:
        driver = _Driver(status="提现成功 | 银行流水号 ABC")
        result = self._collect(driver)
        self.assertIsNone(result["values"]["processing"])
        self.assertNotIn("processing", result["evidence"])
        self.assertEqual(result["evidence_exemptions"]["processing"], "withdrawal_success")

    def test_explicit_empty_withdrawal_filter_is_blank_without_screenshot(self) -> None:
        driver = _Driver()
        driver.rows = []
        result = self._collect(driver)
        self.assertIsNone(result["values"]["processing"])
        self.assertNotIn("processing", result["evidence"])
        self.assertEqual(result["evidence_exemptions"]["processing"], "no_withdrawal")
        self.assertNotIn("processing", result["field_errors"])

    def test_unknown_withdrawal_status_fails_closed(self) -> None:
        driver = _Driver(status="银行状态待确认")
        result = self._collect(driver)
        self.assertIsNone(result["values"]["processing"])
        self.assertIn("processing", result["field_errors"])

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
