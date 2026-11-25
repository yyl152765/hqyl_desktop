"""Offline coverage of Thailand's extra Shopee card-payment confirmation."""

from contextlib import ExitStack, nullcontext
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime" / "superbrowser_process"))
from implement.shopee import th_shopee_ads_recharge as recharge


class Button:
    def __init__(self, on_click=None, text="Pay"):
        self.clicks = 0
        self.on_click = on_click
        self.text = text

    def click(self):
        self.clicks += 1
        if self.on_click:
            self.on_click()

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def send_keys(self, *args):
        pass


def gateway(order="12345", total="฿321.00", button=None):
    return SimpleNamespace(
        url=f"https://shopee.co.th/payment/{order}?client_id=70013",
        text=f"Credit/Debit Card\nTotal Payment\n{total}\nPayment Method\nCard [*1234]\nPay",
        button=button or Button(),
    )


def seller(text="Ads Credit"):
    return SimpleNamespace(url="https://seller.shopee.co.th/portal/marketing/pas/index", text=text, button=None)


class Driver:
    def __init__(self):
        self.pages = {"source": seller()}
        self.current_window_handle = "source"
        self.timeouts = SimpleNamespace(implicit_wait=0)
        self.switch_to = SimpleNamespace(window=self.switch_window, default_content=lambda: None)

    @property
    def window_handles(self):
        return list(self.pages)

    @property
    def current_url(self):
        return self.pages[self.current_window_handle].url

    def switch_window(self, handle):
        self.current_window_handle = handle

    def implicitly_wait(self, seconds):
        self.timeouts.implicit_wait = seconds

    def execute_script(self, script, *args):
        if "document.body" in script:
            return self.pages[self.current_window_handle].text
        return None

    def find_elements(self, by, value):
        page = self.pages[self.current_window_handle]
        if value == recharge.SHOPEE_GATEWAY_PAY_XPATH:
            return [page.button] if page.button else []
        if value == recharge.PAYMENT_SUCCESS_XPATH and page.text == "Payment Successful":
            return [Button(text=page.text)]
        return []


class Clock:
    now = 0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ThailandPaymentConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.driver = Driver()
        timeout = patch.object(recharge, "_TemporaryRemoteTimeout", side_effect=lambda *args: nullcontext())
        timeout.start()
        self.addCleanup(timeout.stop)

    def context(self, total="321.00"):
        return recharge._new_payment_confirmation_context(self.driver, total)

    def test_same_window_gateway_is_clicked_once_across_polls(self):
        context = self.context()
        page = gateway()
        self.driver.pages["source"] = page
        for _ in range(3):
            result, _ = recharge._confirm_shopee_payment_once(self.driver, context)
            self.assertEqual(result, "attempted")
        self.assertEqual(page.button.clicks, 1)

    def test_new_gateway_is_used_while_existing_payment_tab_is_ignored(self):
        old = gateway(order="10000")
        self.driver.pages["old"] = old
        context = self.context()
        new = gateway(order="20000")
        self.driver.pages["new"] = new
        recharge._confirm_shopee_payment_once(self.driver, context)
        self.assertEqual(old.button.clicks, 0)
        self.assertEqual(new.button.clicks, 1)

    def test_preexisting_success_cannot_complete_this_recharge(self):
        self.driver.pages["old"] = seller("Payment Successful")
        context = self.context()
        self.assertEqual(recharge._probe_current_payment_result(self.driver, context), (None, ""))

    def test_invalid_or_mismatched_total_never_clicks_pay(self):
        for expected, actual in (("300", "฿321"), ("-", "฿321"), ("321.00", "฿321.01"), ("321", "฿321 000")):
            with self.subTest(expected=expected, actual=actual):
                self.driver.pages["source"] = seller()
                context = self.context(expected)
                page = gateway(total=actual)
                self.driver.pages["source"] = page
                result, message = recharge._confirm_shopee_payment_once(self.driver, context)
                self.assertEqual(result, "blocked")
                self.assertIn("总额", message)
                self.assertEqual(page.button.clicks, 0)

    def test_thousands_and_decimal_totals_match_exactly(self):
        context = self.context("THB 5,350.00")
        page = gateway(total="฿5,350")
        self.driver.pages["source"] = page
        recharge._confirm_shopee_payment_once(self.driver, context)
        self.assertEqual(page.button.clicks, 1)

    def test_click_timeout_does_not_cause_retry(self):
        def timeout():
            raise TimeoutError("response lost after the click")
        context = self.context()
        page = gateway(button=Button(timeout))
        self.driver.pages["source"] = page
        for _ in range(3):
            recharge._confirm_shopee_payment_once(self.driver, context)
        self.assertEqual(page.button.clicks, 1)
        self.assertIn("未重复点击", context["detail"])

    def test_multiple_order_ids_are_not_automatically_selected(self):
        context = self.context()
        self.driver.pages.update({"new1": gateway("1"), "new2": gateway("2")})
        result, _ = recharge._confirm_shopee_payment_once(self.driver, context)
        self.assertEqual(result, "blocked")
        self.assertTrue(all(page.button.clicks == 0 for name, page in self.driver.pages.items() if name != "source"))

    def test_lookalike_domains_http_and_non_payment_paths_are_ignored(self):
        for url in (
            "http://shopee.co.th/payment/123", "https://shopee.co.th.evil.test/payment/123",
            "https://shopee.co.th@evil.test/payment/123", "https://shopee.co.th/payment-method/123",
            "https://shopee.co.th:8443/payment/123",
        ):
            self.driver.pages["source"] = seller()
            context = self.context()
            page = gateway()
            page.url = url
            self.driver.pages["source"] = page
            recharge._confirm_shopee_payment_once(self.driver, context)
            self.assertEqual(page.button.clicks, 0, url)

    def test_delayed_gateway_is_confirmed_then_success_is_observed(self):
        context = self.context()
        clock = Clock()
        page = gateway(button=Button(lambda: setattr(page, "text", "Payment Successful")))

        def sleep(seconds):
            clock.sleep(seconds)
            self.driver.pages["new"] = page

        with (
            patch.object(recharge.time, "time", side_effect=clock.time),
            patch.object(recharge.time, "sleep", side_effect=sleep),
            patch.object(recharge, "_confirm_payment_success", side_effect=lambda driver, msg: (True, msg)),
        ):
            result, message = recharge._wait_for_payment_result(self.driver, 10, payment_context=context)
        self.assertTrue(result)
        self.assertIn("Successful", message)
        self.assertEqual(page.button.clicks, 1)

    def test_direct_success_does_not_require_a_gateway(self):
        context = self.context()
        self.driver.pages["source"] = seller("Payment Successful")
        with patch.object(recharge, "_confirm_payment_success", return_value=(True, "Payment Successful")):
            result, _ = recharge._wait_for_payment_result(self.driver, 1, payment_context=context)
        self.assertIs(result, True)
        self.assertEqual(context["attempted_order_id"], "")

    def test_timeout_without_terminal_evidence_stays_processing(self):
        context = self.context()
        clock = Clock()
        with patch.object(recharge.time, "time", side_effect=clock.time), patch.object(recharge.time, "sleep", side_effect=clock.sleep):
            result, message = recharge._wait_for_payment_result(self.driver, 3, payment_context=context)
        self.assertEqual(result, "processing")
        self.assertIn("不要立即重复", message)

    def test_unrelated_extension_failure_is_not_a_payment_failure(self):
        context = self.context()
        self.driver.pages["source"].text = "Ads Credit\n店小秘\n采集失败"
        self.assertEqual(recharge._probe_current_payment_result(self.driver, context), (None, ""))

    def test_actual_success_xpath_ignores_extension_and_history_text(self):
        # .NET's XPath engine evaluates these well-formed HTML fixtures without
        # launching a browser or adding another Python dependency on Windows.
        powershell = shutil.which("powershell.exe")
        if not powershell:
            self.skipTest("Windows XPath fixture runner unavailable")
        fixtures = [
            ("<html><body><main>Ads Credit</main><aside><span>采集成功</span></aside></body></html>", False),
            ("<html><body><table><tr><td>Completed</td></tr></table></body></html>", False),
            ("<html><body><main><script>Payment Successful</script></main></body></html>", False),
            ("<html><body><p>尚未支付成功</p></body></html>", False),
            ("<html><body><main><h1>Payment Successful</h1></main></body></html>", True),
            ("<html><body><div id='payment-fe-popup-title'>支付成功</div></body></html>", True),
            ("<html><body><div id='payment-fe-popup-title'>Completed</div></body></html>", True),
        ]
        payload = [{"html": html, "xpath": recharge.PAYMENT_SUCCESS_XPATH} for html, _ in fixtures]
        script = """
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
$fixtures = [Console]::In.ReadToEnd() | ConvertFrom-Json
$matches = @($fixtures | ForEach-Object {
    $document = [System.Xml.XmlDocument]::new()
    $document.LoadXml($_.html)
    @($document.SelectNodes($_.xpath)).Count -gt 0
})
ConvertTo-Json -InputObject $matches -Compress
"""
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            input=json.dumps(payload, ensure_ascii=True), capture_output=True, text=True,
            encoding="utf-8", check=True, timeout=20,
        )
        self.assertEqual(json.loads(completed.stdout), [expected for _, expected in fixtures])

    def test_negative_payment_success_phrase_is_not_success(self):
        context = self.context()
        self.driver.pages["source"].text = "尚未支付成功"
        self.assertEqual(recharge._probe_current_payment_result(self.driver, context), (None, ""))

    def test_changed_total_during_render_is_not_clicked(self):
        context = self.context()
        page = gateway()
        self.driver.pages["source"] = page
        with patch.object(recharge, "_get_visible_body_text", side_effect=[page.text, gateway(total="฿999").text]):
            result, message = recharge._confirm_shopee_payment_once(self.driver, context)
        self.assertEqual(result, "blocked")
        self.assertIn("点击前", message)
        self.assertEqual(page.button.clicks, 0)

    def test_worldfirst_pay_precedes_otp_and_uses_current_payment_scope(self):
        context = self.context()
        page = gateway()
        self.driver.pages["source"] = page

        def otp(*args, **kwargs):
            self.assertEqual(page.button.clicks, 1)
            self.assertIs(kwargs["payment_context"], context)
            return Button()

        with (
            patch.object(recharge, "world_card_sms_config", {"enabled": True, "auth_button_timeout": 30}),
            patch.object(recharge, "_wait_worldfirst_element", side_effect=otp),
            patch.object(recharge, "_worldfirst_otp_delivery_context", return_value={"channel": "sms"}),
            patch.object(recharge, "_wait_world_card_sms_code", return_value="123456"),
            patch.object(recharge, "_fill_worldfirst_otp_code") as fill,
        ):
            result, _ = recharge._handle_worldfirst_id_check(
                SimpleNamespace(driver=self.driver), "test", 300, "321", payment_context=context,
            )
        self.assertIs(result, True)
        fill.assert_called_once_with(self.driver, "123456", payment_context=context)

    def test_worldfirst_frictionless_success_skips_otp(self):
        context = self.context()
        self.driver.pages["source"] = seller("Payment Successful")
        with patch.object(recharge, "world_card_sms_config", {"enabled": True}), patch.object(recharge, "_wait_worldfirst_element") as otp:
            result, _ = recharge._handle_worldfirst_id_check(
                SimpleNamespace(driver=self.driver), "test", 300, "321", payment_context=context,
            )
        self.assertEqual(result, "paid")
        otp.assert_not_called()

    def run_recharge(self, on_submit, wait_result=None):
        submit = Button(on_submit)
        browser = Mock(driver=self.driver)
        browser.find_element.side_effect = lambda by, xpath: (
            submit if xpath == recharge.CONFIRM_BTN else Button(text="500")
        )
        # An unrelated OK button must never turn failed/unknown payment into success.
        browser.find_elements.return_value = [Button(text="OK")]
        clock = Clock()
        with ExitStack() as stack:
            for name, kwargs in (
                ("close_alert", {}),
                ("_wait_for_visible_element_by_xpaths", {"return_value": Button()}),
                ("_wait_for_visible_text_by_xpaths", {"side_effect": ["500", "฿535.00"]}),
                ("_get_optional_currency_text", {"return_value": "35"}),
                ("_ensure_credit_card_payment_selected", {"return_value": (True, "")}),
                ("_select_payment_card", {"return_value": (True, "")}),
                ("_confirm_payment_success", {"side_effect": lambda driver, msg: (True, msg)}),
            ):
                stack.enter_context(patch.object(recharge, name, **kwargs))
            stack.enter_context(patch.object(recharge, "payment_card_type", "payoneer"))
            stack.enter_context(patch.object(recharge, "PAYMENT_RESULT_TIMEOUT_SECONDS", 6))
            stack.enter_context(patch.object(recharge.time, "time", side_effect=clock.time))
            stack.enter_context(patch.object(recharge.time, "sleep", side_effect=clock.sleep))
            if wait_result is not None:
                stack.enter_context(patch.object(recharge, "_wait_for_payment_result", return_value=wait_result))
            result = recharge.recharge_ads(browser, "test", 500)
        return result, submit

    def test_full_recharge_submits_then_confirms_gateway_once(self):
        page = gateway(total="฿535.00", button=Button(lambda: setattr(page, "text", "Payment Successful")))
        result, submit = self.run_recharge(lambda: self.driver.pages.update({"gateway": page}))
        self.assertIs(result[0], True)
        self.assertEqual(submit.clicks, 1)
        self.assertEqual(page.button.clicks, 1)
        self.assertIn("535.00", result[1])

    def test_full_recharge_keeps_failure_reason_despite_unrelated_ok_button(self):
        result, submit = self.run_recharge(lambda: None, (False, "Payment failed: card declined"))
        self.assertIs(result[0], False)
        self.assertIn("card declined", result[1])
        self.assertEqual(submit.clicks, 1)

    def test_initial_submit_timeout_is_not_retried_and_remains_processing(self):
        def timeout():
            raise TimeoutError("submission response lost")
        result, submit = self.run_recharge(timeout)
        self.assertEqual(result[0], "processing")
        self.assertEqual(submit.clicks, 1)
        self.assertIn("不要立即重复", result[1])

    def test_missing_payment_window_context_stops_before_initial_submit(self):
        self.driver.current_window_handle = "disappeared"
        result, submit = self.run_recharge(lambda: None)
        self.assertIs(result[0], False)
        self.assertIn("已停止提交支付", result[1])
        self.assertEqual(submit.clicks, 0)


if __name__ == "__main__":
    unittest.main()
