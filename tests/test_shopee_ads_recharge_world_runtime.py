import sys
import unittest
import json
import logging
import tempfile
from pathlib import Path
from unittest.mock import ANY, patch


SUPERBROWSER_ROOT = Path(__file__).resolve().parents[2] / "superbrowser_process"
if str(SUPERBROWSER_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERBROWSER_ROOT))

from implement.shopee import my_shopee_ads_recharge as my_recharge
from implement.shopee import th_shopee_ads_recharge as th_recharge
from main.shopee import id_shopee_ads_recharge_operator_service as id_service
from main.shopee import my_shopee_ads_recharge_operator_service as my_service
from main.shopee import ph_shopee_ads_recharge_operator_service as ph_service
from main.shopee import th_shopee_ads_recharge_operator_service as th_service
from main.shopee import vn_shopee_ads_recharge_operator_service as vn_service


class _FakeElement:
    def __init__(self, attrs=None, selected=False, children=None, text=""):
        self._attrs = attrs or {}
        self._selected = selected
        self._children = children or []
        self.text = text
        self.click_count = 0

    def get_attribute(self, name):
        return self._attrs.get(name, "")

    def is_selected(self):
        return self._selected

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def click(self):
        self.click_count += 1

    def find_elements(self, _by, xpath):
        if "input[@type='radio']" in xpath:
            return self._children
        if "aria-checked='true'" in xpath:
            return [child for child in self._children if my_recharge._element_has_selected_state(child)]
        return []


class _ScriptFailingDriver:
    def execute_script(self, *_args, **_kwargs):
        raise my_recharge.WebDriverException("script unavailable")


class _FrameNode:
    def __init__(self, frames=None, target=None):
        self.frames = frames or []
        self.target = target


class _FrameElement(_FakeElement):
    def __init__(self, node):
        super().__init__()
        self.node = node


class _FrameSwitchTo:
    def __init__(self, driver):
        self.driver = driver

    def frame(self, frame_element):
        self.driver.stack.append(frame_element.node)

    def parent_frame(self):
        if len(self.driver.stack) > 1:
            self.driver.stack.pop()

    def default_content(self):
        self.driver.stack = [self.driver.root]

    def window(self, _handle):
        self.default_content()


class _NestedFrameDriver:
    def __init__(self, root):
        self.root = root
        self.stack = [root]
        self.switch_to = _FrameSwitchTo(self)

    @property
    def depth(self):
        return len(self.stack) - 1

    def find_elements(self, _by, xpath):
        current = self.stack[-1]
        if xpath == "//iframe":
            return [_FrameElement(node) for node in current.frames]
        if xpath == "//worldfirst-target" and current.target is not None:
            return [current.target]
        return []

    def implicitly_wait(self, _seconds):
        return None


def _nested_frame_tree(target_depth):
    target = _FakeElement()
    node = _FrameNode(target=target)
    for _ in range(target_depth):
        node = _FrameNode(frames=[node])
    return node, target


class _ClickTargetDriver:
    def __init__(self, clickable_target):
        self.clickable_target = clickable_target

    def execute_script(self, script, _element):
        if "let node = arguments[0]" in script:
            return self.clickable_target
        return None


class _WebdriverUtilStub:
    def __init__(self):
        self.driver = object()


class _CardWebdriverUtilStub:
    def __init__(self):
        self.driver = object()
        self.clicked = []

    def move_to_click(self, element):
        self.clicked.append(element)


class ShopeeAdsRechargeWorldRuntimeTests(unittest.TestCase):
    def test_dingtalk_doc_credentials_are_merged_in_memory_for_all_sites(self):
        for service in (id_service, th_service, ph_service, vn_service, my_service):
            settings = service.AdsRechargeSettings()
            settings.dingtalk_doc_credentials = {
                "app_key": "desktop-app-key",
                "app_secret": "desktop-app-secret",
            }

            merged = service._merge_settings_into_config(
                {"dingtalk": {"doc_config": {"config_doc_id": "doc-id"}}},
                settings,
            )

            self.assertEqual(merged["dingtalk"]["doc_config"]["app_key"], "desktop-app-key")
            self.assertEqual(
                merged["dingtalk"]["doc_config"]["app_secret"],
                "desktop-app-secret",
            )
            self.assertEqual(merged["dingtalk"]["doc_config"]["config_doc_id"], "doc-id")

    def test_thailand_recharge_module_exports_the_operator_entry_point(self):
        self.assertTrue(callable(th_recharge.th_shopee_ads_recharge))
        self.assertIs(
            th_recharge.my_shopee_ads_recharge,
            th_recharge.th_shopee_ads_recharge,
        )

    def test_world_payment_wait_is_raised_to_maximum_for_malaysia_and_thailand(self):
        self.assertEqual(my_service.effective_payment_wait_seconds("world", 150), 300)
        self.assertEqual(th_service.effective_payment_wait_seconds("world", 150), 300)

    def test_payoneer_payment_wait_keeps_configured_value(self):
        self.assertEqual(my_service.effective_payment_wait_seconds("payoneer", 150), 150)
        self.assertEqual(th_service.effective_payment_wait_seconds("payoneer", 150), 150)

    def test_custom_radio_checked_class_is_treated_as_selected(self):
        element = _FakeElement({"class": "eds-radio eds-radio--checked"})

        self.assertTrue(my_recharge._is_radio_label_selected(_ScriptFailingDriver(), element))

    def test_child_radio_selected_state_is_treated_as_selected(self):
        element = _FakeElement(children=[_FakeElement(selected=True)])

        self.assertTrue(my_recharge._is_radio_label_selected(_ScriptFailingDriver(), element))

    def test_explicit_world_card_type_cannot_fall_back_to_module_default(self):
        for module in (my_recharge, th_recharge):
            with (
                patch.object(module, "payment_card_type", "payoneer"),
                patch.object(module, "_select_first_world_card", return_value=(True, "")) as world,
                patch.object(module, "_select_first_payoneer_card") as payoneer,
            ):
                result = module._select_payment_card(_CardWebdriverUtilStub(), "测试店铺", "world")

            self.assertEqual(result, (True, ""))
            world.assert_called_once()
            payoneer.assert_not_called()

    def test_world_card_selection_fails_closed_without_selected_state(self):
        for module in (my_recharge, th_recharge):
            card = _FakeElement(text="WORLD CARD *7093")
            webdriver_util = _CardWebdriverUtilStub()
            with (
                patch.object(module, "_wait_for_visible_element_by_xpaths", return_value=card),
                patch.object(module, "_resolve_payment_card_click_target", return_value=card),
                patch.object(module, "_is_radio_label_selected", return_value=False),
                patch.object(module, "_short_page_context", return_value="page context"),
                patch.object(module.time, "time", side_effect=[0, 0, 6]),
                patch.object(module.time, "sleep"),
            ):
                success, message = module._select_first_world_card(webdriver_util, "测试店铺")

            self.assertFalse(success)
            self.assertIn("未检测到 WORLD 银行卡被选中", message)
            self.assertEqual(webdriver_util.clicked, [card])

    def test_worldfirst_sms_button_supports_traditional_chinese(self):
        for module in (my_recharge, th_recharge):
            selectors = " ".join(module.WORLDFIRST_SMS_BUTTON_XPATHS)
            self.assertIn("獲取短信驗證碼", selectors)

    def test_worldfirst_plain_text_sms_selector_and_tight_otp_selector(self):
        for module in (my_recharge, th_recharge):
            selectors = " ".join(module.WORLDFIRST_SMS_BUTTON_XPATHS)
            self.assertIn("normalize-space(.)='获取短信验证码'", selectors)
            self.assertNotIn(
                "//input[not(@type='hidden') and not(@disabled)]",
                module.WORLDFIRST_OTP_INPUT_XPATH,
            )
            self.assertIn("one-time-code", module.WORLDFIRST_OTP_INPUT_XPATH)

    def test_worldfirst_scans_nested_iframes_through_depth_eight(self):
        for module in (my_recharge, th_recharge):
            root, target = _nested_frame_tree(target_depth=8)
            driver = _NestedFrameDriver(root)
            diagnostics = module._reset_worldfirst_scan_diagnostics()

            found = module._find_worldfirst_element_recursive(
                driver,
                ["//worldfirst-target"],
                diagnostics,
            )

            self.assertIs(found, target)
            self.assertEqual(diagnostics["found_depth"], 8)
            self.assertEqual(diagnostics["found_frame_path"], "0/0/0/0/0/0/0/0")
            self.assertEqual(driver.depth, 8)

    def test_worldfirst_stops_scanning_beyond_depth_eight(self):
        for module in (my_recharge, th_recharge):
            root, _target = _nested_frame_tree(target_depth=9)
            driver = _NestedFrameDriver(root)
            diagnostics = module._reset_worldfirst_scan_diagnostics()

            found = module._find_worldfirst_element_recursive(
                driver,
                ["//worldfirst-target"],
                diagnostics,
            )

            self.assertIsNone(found)
            self.assertEqual(diagnostics["max_depth_seen"], 8)
            self.assertTrue(diagnostics["depth_limit_reached"])
            self.assertEqual(driver.depth, 0)

    def test_worldfirst_sms_click_uses_clickable_ancestor(self):
        for module in (my_recharge, th_recharge):
            text_element = _FakeElement()
            clickable_target = _FakeElement()
            driver = _ClickTargetDriver(clickable_target)

            clicked, error = module._click_worldfirst_sms_button(driver, text_element)

            self.assertTrue(clicked)
            self.assertEqual(error, "")
            self.assertEqual(text_element.click_count, 0)
            self.assertEqual(clickable_target.click_count, 1)

    def test_worldfirst_sms_click_retries_until_otp_input_appears(self):
        for module in (my_recharge, th_recharge):
            sms_button = _FakeElement()
            otp_input = _FakeElement()
            wait_results = [None, sms_button, None, None, sms_button, otp_input]
            with (
                patch.object(module, "world_card_sms_config", {"auth_button_timeout": 30}),
                patch.object(module, "_wait_worldfirst_element", side_effect=wait_results),
                patch.object(module, "_click_worldfirst_sms_button", return_value=(True, "")) as click_sms,
                patch.object(module, "_wait_world_card_sms_code", return_value="123456"),
                patch.object(module, "_fill_worldfirst_otp_code") as fill_otp,
                patch.object(module.time, "sleep"),
            ):
                success, message = module._handle_worldfirst_id_check(
                    _WebdriverUtilStub(),
                    "测试店铺",
                    100,
                    "MYR 108.00",
                )

            self.assertTrue(success)
            self.assertIn("已回填", message)
            self.assertEqual(click_sms.call_count, 2)
            fill_otp.assert_called_once_with(ANY, "123456")

    def test_worldfirst_confirm_button_supports_traditional_chinese(self):
        for module in (my_recharge, th_recharge):
            selectors = " ".join(module.WORLDFIRST_CONFIRM_BUTTON_XPATHS)
            self.assertIn("確認", selectors)
            self.assertIn("驗證", selectors)
            self.assertIn("繼續", selectors)

    def test_world_sms_matches_new_short_shopee_merchant_label(self):
        cases = (
            (
                th_recharge,
                th_service,
                ["FOR SHOPEE", "TH SHOPEE MARKETPLACE"],
                "【万里汇】您的一次性验证码为：123456，用于完成一笔交易，"
                "商户：SHOPEE，金额为THB 3424.00，卡号尾号为7093。",
                "THB 3424.00",
            ),
            (
                my_recharge,
                my_service,
                ["MY SHOPEE MARKETPLACE", "SHOPEE MY MARKETPLACE", "FOR SHOPEE"],
                "【万里汇】Your one-time password (OTP) is: 123456, for completing "
                "a transaction with merchant: SHOPEE, amount: MYR 108.00.",
                "MYR 108.00",
            ),
        )

        for module, service, legacy_keywords, raw_message, payment_total in cases:
            message = raw_message.upper().replace(",", "")
            merchant_keywords = module._world_sms_merchant_keywords(
                {"merchant_keywords": legacy_keywords}
            )
            amount_keywords = module._world_sms_amount_keywords(payment_total)
            default_keywords = service.DEFAULT_PROCESS_CONFIG["shopee"]["world_card_sms"][
                "merchant_keywords"
            ]

            self.assertIn("SHOPEE", merchant_keywords)
            self.assertIn("SHOPEE", default_keywords)
            self.assertTrue(any(keyword in message for keyword in merchant_keywords))
            self.assertTrue(any(keyword in message for keyword in amount_keywords))
            self.assertEqual(
                module._extract_world_otp_code({"otp_code": "123456", "message": raw_message}),
                "123456",
            )
            self.assertFalse(
                any(keyword in message for keyword in module._world_sms_amount_keywords("999.99"))
            )

    def test_worldfirst_delivery_classifier_distinguishes_email_from_sms(self):
        email_text = "输入发送到你的电子邮件的密码 p******2@1*6.com"
        sms_text = "输入发送到你的电话号码的密码 ****7093"
        for module in (my_recharge, th_recharge):
            email_context = module._classify_worldfirst_otp_delivery_text(email_text)
            sms_context = module._classify_worldfirst_otp_delivery_text(sms_text)

            self.assertEqual(email_context["channel"], "email")
            self.assertEqual(email_context["masked_email"], "p******2@1*6.com")
            self.assertEqual(sms_context["channel"], "sms")

    def test_worldfirst_email_delivery_uses_manual_email_task_and_not_sms_query(self):
        for module in (my_recharge, th_recharge):
            with (
                patch.object(module, "world_card_sms_config", {"auth_button_timeout": 30}),
                patch.object(module, "_wait_worldfirst_element", return_value=_FakeElement()),
                patch.object(
                    module,
                    "_worldfirst_otp_delivery_context",
                    return_value={"channel": "email", "masked_email": "p******2@1*6.com"},
                ),
                patch.object(module, "_wait_world_card_email_code", return_value="654321") as wait_email,
                patch.object(module, "_wait_world_card_sms_code") as wait_sms,
                patch.object(module, "_fill_worldfirst_otp_code") as fill_otp,
            ):
                success, message = module._handle_worldfirst_id_check(
                    _WebdriverUtilStub(),
                    "李俊杰-泰国273-TH011",
                    5100,
                    "THB 5457.00",
                )

            self.assertTrue(success)
            self.assertIn("邮箱验证码已回填", message)
            wait_email.assert_called_once_with(
                "李俊杰-泰国273-TH011",
                5100,
                "THB 5457.00",
                "p******2@1*6.com",
            )
            wait_sms.assert_not_called()
            fill_otp.assert_called_once_with(ANY, "654321")

    def test_worldfirst_email_task_keeps_masked_recipient_in_task_name(self):
        for module in (my_recharge, th_recharge):
            with (
                patch.object(
                    module,
                    "security_verify_config",
                    {
                        "base_url": "http://otp.test",
                        "phone": "无",
                        "user_id": "user-1",
                        "world_email_code_system_name": "【{shop_name}】邮箱码（{masked_email}）",
                    },
                ),
                patch.object(module, "_request_security_code", return_value=("123456", "task-1")) as request,
            ):
                code = module._wait_world_card_email_code(
                    "李俊杰-泰国273-TH011",
                    5100,
                    "THB 5457.00",
                    "p******2@1*6.com",
                )

            self.assertEqual(code, "123456")
            self.assertEqual(request.call_args.args[:5], (
                "李俊杰-泰国273-TH011",
                5100,
                "THB 5457.00",
                "world_email",
                "WORLD 邮箱验证码",
            ))
            self.assertIn("p******2@1*6.com", request.call_args.args[5])

    def test_success_result_removes_stale_failure_for_same_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            error_path = Path(temp_dir) / "failed.txt"
            success_path = Path(temp_dir) / "success.txt"
            error_path.write_text(
                "黄娟-马来105-MY005: WORLD 支付未找到 WorldFirst 验证页面。url=; text=\n"
                "其他店铺: 保留失败记录\n",
                encoding="utf-8",
            )
            success_path.write_text("", encoding="utf-8")
            with (
                patch.object(my_recharge, "error_shop_file", str(error_path)),
                patch.object(my_recharge, "success_shop_file", str(success_path)),
            ):
                my_recharge._write_success("黄娟-马来105-MY005", "黄娟-马来105-MY005: 支付成功")

            error_text = error_path.read_text(encoding="utf-8")
            self.assertNotIn("黄娟-马来105-MY005", error_text)
            self.assertIn("其他店铺", error_text)
            self.assertIn("黄娟-马来105-MY005", success_path.read_text(encoding="utf-8"))

    def test_missing_malaysia_status_row_builds_complete_append_record(self):
        row = my_service._custom_status_append_row_values(
            {},
            "影刀1",
            "赵丽媛-马来156-MY001",
            "100.00",
            "支付成功，支付总额：108.00。",
        )

        self.assertEqual(row[1:], ["马来156", "", "100马币", "", row[5], "影刀1", "108.00"])
        today = my_service.datetime.today()
        self.assertEqual(row[0], f"{today.year}/{today.month}/{today.day}")
        self.assertTrue(row[5].endswith("已充值"))

    def test_missing_thailand_status_row_uses_thai_baht_unit(self):
        row = th_service._custom_status_append_row_values(
            {},
            "影刀3",
            "测试-泰国461-TH001",
            "600",
            "支付成功，支付总额：600。",
        )

        self.assertEqual(row[1], "泰国461")
        self.assertEqual(row[3], "600泰铢")
        self.assertEqual(row[6], "影刀3")

    def test_dws_missing_match_appends_full_row(self):
        dws_results = [
            {"nonEmptyRange": {"lastRow": 20}},
            {"success": True},
        ]
        with (
            patch.object(my_service, "_resolve_dws_sheet", return_value=("node-id", "s1")),
            patch.object(my_service, "_dws_find_candidate_rows", return_value=[]),
            patch.object(my_service, "_run_dws_json", side_effect=dws_results) as run_dws,
        ):
            result = my_service._write_custom_status_with_dws(
                logging.getLogger("test-custom-status-append"),
                {},
                "doc-id",
                "Sheet1",
                "赵丽媛-马来156-MY001",
                "100",
                "支付成功，支付总额：108.00。",
                "影刀1",
            )

        self.assertTrue(result)
        append_args = run_dws.call_args_list[1].args[0]
        self.assertEqual(append_args[:2], ["sheet", "append"])
        values = json.loads(append_args[append_args.index("--values") + 1])
        self.assertEqual(len(values[0]), 8)
        self.assertEqual(values[0][1], "马来156")
        self.assertEqual(values[0][3], "100马币")


if __name__ == "__main__":
    unittest.main()
