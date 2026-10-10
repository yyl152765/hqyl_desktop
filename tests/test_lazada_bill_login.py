"""账单明细进站与登录的离线契约测试（含 Playwright 合成页面）。"""
from __future__ import annotations

import html
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - 环境未安装时跳过
    sync_playwright = None

from backend.services import lazada_bill_page as page_module
from backend.services.lazada_ads_page import LoginRequiredError, WorkflowError
from backend.services.lazada_bill_detail import COUNTRY_PROFILES

LAUNCHER = "chrome-extension://abcdefghijklmnop/launcher.html"

PREFILLED_LOGIN_CARD = """<!doctype html><html><body>
<div class="login-card">
  <div class="tabs"><span>扫码登录</span><span>密码登录</span></div>
  <div class="phone-field"><span class="prefix">+60</span>
    <input type="text" value="60194809188" /></div>
  <input type="password" value="prefilled-secret" />
  <button class="primary">登录</button>
  <button>使用验证码登录</button>
</div>
</body></html>"""

REGISTER_CARD = """<!doctype html><html><body>
<div class="register-card">
  <h2>注册Lazada卖家</h2>
  <input type="text" placeholder="请输入手机号码" />
  <input type="password" placeholder="新密码" />
  <button>通过WhatsApp发送OTP</button>
</div>
</body></html>"""

TWO_LOGIN_CARDS = """<!doctype html><html><body>
<div class="card-a"><input type="text" value="a-user" />
  <input type="password" value="a-secret" /><button>登录</button></div>
<div class="card-b"><input type="text" value="b-user" />
  <input type="password" value="b-secret" /><button>登录</button></div>
</body></html>"""


class LoginUrlTests(unittest.TestCase):
    def test_local_store_stays_on_the_local_site(self) -> None:
        profile = COUNTRY_PROFILES["MY"]
        self.assertEqual(
            page_module._finance_url_for_session(LAUNCHER, profile, "黄金娟-LZ马来001").split("/portal")[0],
            "https://sellercenter.lazada.com.my",
        )

    def test_cross_border_store_uses_the_cross_border_site(self) -> None:
        profile = COUNTRY_PROFILES["MY"]
        self.assertEqual(
            page_module._finance_url_for_session(LAUNCHER, profile, "徐能悦-LZ跨境马来001").split("/portal")[0],
            "https://sellercenter-my.lazada-seller.cn",
        )

    def test_session_site_wins_over_the_store_name(self) -> None:
        profile = COUNTRY_PROFILES["MY"]
        local_session = "https://sellercenter.lazada.com.my/portal/apps/finance/myIncome/index"
        cross_session = "https://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index"
        self.assertIn(
            "sellercenter.lazada.com.my",
            page_module._finance_url_for_session(local_session, profile, "跨境马来001"),
        )
        self.assertIn(
            "sellercenter-my.lazada-seller.cn",
            page_module._finance_url_for_session(cross_session, profile, "马来001"),
        )

    def test_gsp_session_maps_to_the_country_cross_border_site(self) -> None:
        profile = COUNTRY_PROFILES["TH"]
        for url in ("https://gsp.lazada-seller.cn/page/login", "https://gsp.lazada.com/portal/home"):
            with self.subTest(url=url):
                self.assertIn(
                    "sellercenter-th.lazada-seller.cn",
                    page_module._finance_url_for_session(url, profile, "泰国001"),
                )

    def test_finance_url_never_carries_login_parameters(self) -> None:
        profile = COUNTRY_PROFILES["TH"]
        login_page = "https://sellercenter.lazada.co.th/app/seller/login?login=1&redirect_uri=x"
        target = page_module._finance_url_for_session(login_page, profile, "泰国001")
        self.assertNotIn("login=1", target)
        self.assertIn("/portal/apps/finance/myIncome/index", target)

    def test_canonical_login_url_targets_the_country_host(self) -> None:
        profile = COUNTRY_PROFILES["MY"]
        url = page_module.canonical_login_url(profile, "sellercenter.lazada.com.my")
        self.assertTrue(url.startswith("https://sellercenter.lazada.com.my/app/seller/login?login=1"))
        self.assertIn("redirect_uri=", url)
        self.assertIn("%2Fportal%2Fapps%2Ffinance%2FmyIncome%2Findex", url)

    def test_auth_and_gsp_routes(self) -> None:
        self.assertEqual(
            page_module._auth_route("https://sellercenter.lazada.com.my/app/seller/login?login=1"), "login"
        )
        self.assertEqual(
            page_module._auth_route("https://sellercenter.lazada.com.my/app/seller/register"), "register"
        )
        self.assertIsNone(
            page_module._auth_route("https://sellercenter.lazada.com.my/portal/apps/finance/myIncome/index")
        )
        self.assertTrue(page_module._gsp_login("https://gsp.lazada-seller.cn/page/login"))
        self.assertFalse(page_module._gsp_login("https://gsp.lazada.com/page/login"))

    def test_login_page_allowed_only_for_country_hosts_and_gsp_login(self) -> None:
        profile = COUNTRY_PROFILES["MY"]
        self.assertTrue(
            page_module._login_page_allowed("https://sellercenter.lazada.com.my/app/seller/login", profile)
        )
        self.assertTrue(
            page_module._login_page_allowed("https://gsp.lazada-seller.cn/page/login", profile)
        )
        self.assertFalse(
            page_module._login_page_allowed("https://gsp.lazada.com/app/seller/register", profile)
        )
        self.assertFalse(
            page_module._login_page_allowed("https://sellercenter.lazada.com.ph/app/seller/login", profile)
        )

    def test_login_errors_are_classified_for_the_runner(self) -> None:
        self.assertEqual(page_module.login_status(page_module.BillLoginRequired("x")), "login_required")
        self.assertEqual(
            page_module.login_status(page_module.VerificationRequiredError("x")),
            "verification_required",
        )
        self.assertEqual(page_module.login_status(WorkflowError("其它错误")), "")
        self.assertTrue(page_module.is_login_blocked(page_module.VerificationRequiredError("x")))
        self.assertFalse(page_module.is_login_blocked(WorkflowError("其它错误")))
        self.assertTrue(
            page_module.browser_connection_failed(
                Exception("Target page, context or browser has been closed")
            )
        )
        self.assertTrue(page_module.browser_connection_failed(page_module.BrowserSessionUnavailable("x")))
        self.assertFalse(page_module.browser_connection_failed(WorkflowError("页面未就绪")))

    def test_is_auth_page_only_for_login_or_register_routes(self) -> None:
        self.assertTrue(
            page_module.is_auth_page("https://sellercenter.lazada.com.my/app/seller/register")
        )
        self.assertTrue(
            page_module.is_auth_page("https://sellercenter.lazada.com.my/app/seller/login?login=1")
        )
        self.assertFalse(
            page_module.is_auth_page("https://sellercenter.lazada.com.my/portal/apps/finance/myIncome/index")
        )


def _scripted_page(urls: list[str]):
    """按 goto 顺序切换 URL 的页面替身，只用于验证进站循环。"""
    calls: list[str] = []
    state = {"index": 0}

    class ScriptedPage:
        def __init__(self) -> None:
            self.url = urls[0]
            self.context = SimpleNamespace(pages=[self])

        def is_closed(self) -> bool:
            return False

        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        def goto(self, url: str, **_kwargs) -> None:
            calls.append(url)
            state["index"] = min(state["index"] + 1, len(urls) - 1)
            self.url = urls[state["index"]]

    return ScriptedPage(), calls


class EntryFlowTests(unittest.TestCase):
    """本土店与跨境店的进站循环（最多两次，跨境登录后可能先停在 GSP 落地面）。"""

    MY = COUNTRY_PROFILES["MY"]
    LOCAL_FINANCE = "https://sellercenter.lazada.com.my/portal/apps/finance/myIncome/index"
    CROSS_FINANCE = "https://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index"
    GSP_LANDING = "https://gsp.lazada-seller.cn/portal/home"

    @staticmethod
    def _origin(url: str) -> str:
        """只比较主机与路径：目标地址会保留配置里的业务参数。"""
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.hostname}{parsed.path}"

    def _run(self, page, shop_name: str, *, login_calls: list | None = None, state: dict | None = None):
        events: list[tuple[str, str]] = []

        def fake_login(target, _shop, _profile, login_state=None):
            if login_calls is not None:
                login_calls.append(login_state)
            events.append(("login", target.url))
            return target

        def fake_goto(target, url, _ms):
            events.append(("goto", url))
            return target.goto(url, wait_until="domcontentloaded")

        with patch.object(page_module, "activate_lazada_from_launcher", side_effect=lambda p, *a: p), \
                patch.object(page_module, "complete_lazada_login", side_effect=fake_login), \
                patch.object(page_module, "_checked_login_page", side_effect=lambda p, *a: p), \
                patch.object(page_module, "ensure_lazada_session"), \
                patch.object(page_module, "dismiss_lazada_popups"), \
                patch.object(page_module, "click_first_text", return_value="收入详情"), \
                patch.object(page_module, "goto_dom_ready", side_effect=fake_goto):
            result = page_module.open_income_details(page, shop_name, self.MY, state)
        return result, events

    def test_local_store_enters_the_local_finance_page_in_one_pass(self) -> None:
        page, calls = _scripted_page([self.LOCAL_FINANCE])
        result, _events = self._run(page, "黄金娟-LZ马来001")
        self.assertEqual([self._origin(item) for item in calls], [self.LOCAL_FINANCE])
        self.assertEqual(self._origin(result.url), self.LOCAL_FINANCE)

    def test_cross_border_store_retries_after_landing_on_the_gsp_page(self) -> None:
        page, calls = _scripted_page([self.GSP_LANDING, self.GSP_LANDING, self.CROSS_FINANCE])
        result, _events = self._run(page, "徐能悦-LZ跨境马来001")
        self.assertEqual(
            [self._origin(item) for item in calls],
            [self.CROSS_FINANCE, self.CROSS_FINANCE],
            "跨境落在 GSP 落地面要再进一次",
        )
        self.assertEqual(self._origin(result.url), self.CROSS_FINANCE)

    def test_gsp_login_page_is_submitted_before_entering(self) -> None:
        login_calls: list = []
        state = {"submitted": False}
        page, calls = _scripted_page(
            ["https://gsp.lazada-seller.cn/page/login", self.CROSS_FINANCE]
        )
        result, events = self._run(page, "徐能悦-LZ跨境马来001", login_calls=login_calls, state=state)
        self.assertTrue(login_calls, "GSP 密码登录页必须先提交预填凭据再进站")
        self.assertTrue(
            all(item is state for item in login_calls),
            "整个进站流程共用同一个 login_state，登录机会不会被重复消耗",
        )
        self.assertEqual(events[0][0], "login", "登录必须先于进站")
        self.assertEqual([self._origin(item) for item in calls], [self.CROSS_FINANCE])
        self.assertEqual(self._origin(result.url), self.CROSS_FINANCE)

    def test_local_login_page_is_submitted_before_the_first_navigation(self) -> None:
        """参考脚本在点完启动页后无条件先补一次登录：本土站登录页同样适用。"""
        local_login = "https://sellercenter.lazada.com.my/app/seller/login?login=1&redirect_uri=x"
        page, calls = _scripted_page([local_login, self.LOCAL_FINANCE])
        result, events = self._run(page, "黄金娟-LZ马来001")
        self.assertEqual(events[0][0], "login", "先在本地站登录页提交预填凭据，再进账单页")
        self.assertEqual(self._origin(events[1][1]), self.LOCAL_FINANCE)
        self.assertEqual([self._origin(item) for item in calls], [self.LOCAL_FINANCE])
        self.assertEqual(self._origin(result.url), self.LOCAL_FINANCE)

    def test_entry_flow_gives_up_after_two_attempts(self) -> None:
        page, calls = _scripted_page(["about:blank", "about:blank", "about:blank"])
        with self.assertRaisesRegex(LoginRequiredError, "未进入目标国家账单站点"):
            self._run(page, "黄金娟-LZ马来001")
        self.assertEqual(len(calls), 2, "最多两次进站")

    def test_local_register_page_falls_back_to_the_standard_login_url(self) -> None:
        """本土店最常见的失败：进账单页时被本地站导到「注册 Lazada 卖家」页。"""
        register = "https://sellercenter.lazada.com.my/app/seller/register"
        # 会话先在店铺自己的本地站；进账单页后被重定向到注册页；回退标准登录地址后才回到账单页。
        page, calls = _scripted_page([self.LOCAL_FINANCE, register, self.LOCAL_FINANCE])

        def click(target, names, timeout=5_000):
            del names, timeout
            # 注册页找不到可点的登录入口（原脚本此处会超时跳过），其它页面按正常点击处理。
            return None if "register" in target.url else "收入详情"

        # 这里的 complete_lazada_login 必须用真实实现：注册页回退正是它的职责。
        with patch.object(page_module, "activate_lazada_from_launcher", side_effect=lambda p, *a: p), \
                patch.object(page_module, "_checked_login_page", side_effect=lambda p, *a: p), \
                patch.object(page_module, "ensure_lazada_session"), \
                patch.object(page_module, "dismiss_lazada_popups"), \
                patch.object(page_module, "click_first_text", side_effect=click), \
                patch.object(page_module, "REGISTER_ENTRY_TIMEOUT", 0.05), \
                patch.object(page_module, "goto_dom_ready",
                    side_effect=lambda p, url, _ms: p.goto(url, wait_until="domcontentloaded"),
                ):
            result = page_module.open_income_details(page, "黄金娟-LZ马来001", self.MY, {})

        login_urls = [
            url for url in calls
            if url.startswith("https://sellercenter.lazada.com.my/app/seller/login?login=1")
        ]
        self.assertEqual(len(login_urls), 1, f"注册页应回退到本地站标准登录地址，实际：{calls}")
        self.assertIn("redirect_uri=", login_urls[0])
        self.assertEqual(self._origin(result.url), self.LOCAL_FINANCE)

    def test_register_page_bounced_from_standard_login_is_clicked_not_waited(self) -> None:
        """真实故障回放（泰国 TH007）：先落在密码登录页 → 预填失败 → 标准登录地址被弹回注册页。

        旧实现只在预填循环里干等，最后报出误导性的「密码登录页账号或密码未填好」；
        现在必须去点注册页上的登录入口，点通后继续完成登录。
        """
        local_login = "https://sellercenter.lazada.com.my/app/seller/login?login=1"
        register = "https://sellercenter.lazada.com.my/apps/register/index?redirect_uri=x"
        page, calls = _scripted_page([local_login, register, self.LOCAL_FINANCE])
        register_clicks: list[str] = []

        def click(target, names, timeout=5_000):
            del names, timeout
            if "register" in target.url:
                register_clicks.append(target.url)
                # 模拟「点注册页登录入口」真的跳到了密码登录页。
                target.url = local_login
                return "登录"
            return "收入详情"

        class _Submit:
            def __init__(self, target):
                self._target = target

            def element_handle(self, timeout=None):
                del timeout
                target = self._target

                class _Element:
                    def click(self, timeout=None):
                        del timeout
                        target.url = EntryFlowTests.LOCAL_FINANCE

                return _Element()

        def filled(target):
            # 紫鸟只在真正的密码登录页预填；注册页永远不会。
            return bool(register_clicks) and "login" in target.url

        with patch.object(page_module, "activate_lazada_from_launcher", side_effect=lambda p, *a: p), \
                patch.object(page_module, "_checked_login_page", side_effect=lambda p, *a: p), \
                patch.object(page_module, "ensure_lazada_session"), \
                patch.object(page_module, "dismiss_lazada_popups"), \
                patch.object(page_module, "ensure_preferred_language", return_value=False), \
                patch.object(page_module, "click_first_text", side_effect=click), \
                patch.object(page_module, "_filled_login_fields", side_effect=filled), \
                patch.object(page_module, "_login_submit_control",
                    side_effect=lambda target: _Submit(target)), \
                patch.object(page_module, "REGISTER_ENTRY_TIMEOUT", 0.05), \
                patch.object(page_module, "LOGIN_PREFILL_TIMEOUT", 0.05), \
                patch.object(page_module, "goto_dom_ready",
                    side_effect=lambda p, url, _ms: p.goto(url, wait_until="domcontentloaded"),
                ):
            result = page_module.open_income_details(
                page, "张萌-LZ泰国企业048-TH007-半运营", self.MY, {}
            )

        self.assertEqual(len(register_clicks), 1, "被弹回注册页时必须点一次登录入口")
        self.assertEqual(
            len([url for url in calls if url.startswith("https://sellercenter.lazada.com.my/app/seller/login?login=1")]),
            1,
            f"点通注册页登录入口后不必再回退，实际导航：{calls}",
        )
        self.assertEqual(self._origin(result.url), self.LOCAL_FINANCE)

    def test_register_page_without_any_login_entry_reports_it_clearly(self) -> None:
        """注册页确实点不到入口时，报错必须指向注册页，而不是误导性的密码登录页。"""
        local_login = "https://sellercenter.lazada.com.my/app/seller/login?login=1"
        register = "https://sellercenter.lazada.com.my/apps/register/index?redirect_uri=x"
        page, _calls = _scripted_page([local_login, register, register, register])

        with patch.object(page_module, "activate_lazada_from_launcher", side_effect=lambda p, *a: p), \
                patch.object(page_module, "_checked_login_page", side_effect=lambda p, *a: p), \
                patch.object(page_module, "ensure_lazada_session"), \
                patch.object(page_module, "click_first_text", return_value=None), \
                patch.object(page_module, "_filled_login_fields", return_value=False), \
                patch.object(page_module, "REGISTER_ENTRY_TIMEOUT", 0.05), \
                patch.object(page_module, "LOGIN_PREFILL_TIMEOUT", 0.05), \
                patch.object(page_module, "goto_dom_ready",
                    side_effect=lambda p, url, _ms: p.goto(url, wait_until="domcontentloaded"),
                ):
            with self.assertRaisesRegex(LoginRequiredError, "注册页面未提供可用的登录入口"):
                page_module.complete_lazada_login(
                    page, "张萌-LZ泰国企业048-TH007-半运营", self.MY, {}
                )


class LoginCardTests(unittest.TestCase):
    """用合成页面验证「唯一凭据容器 + 唯一登录控件」的判定，不联网。"""

    def setUp(self) -> None:
        if sync_playwright is None:
            self.skipTest("Playwright is not installed")
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch()
        self.page = self.browser.new_page()
        self.addCleanup(self.playwright.stop)
        self.addCleanup(self.browser.close)

    def test_prefilled_card_without_form_is_recognised(self) -> None:
        self.page.set_content(PREFILLED_LOGIN_CARD)
        self.assertTrue(page_module._filled_login_fields(self.page))
        submit = page_module._login_submit_control(self.page)
        self.assertIsNotNone(submit)
        self.assertEqual(submit.inner_text().strip(), "登录")

    def test_register_card_without_credentials_is_not_submittable(self) -> None:
        self.page.set_content(REGISTER_CARD)
        self.assertFalse(page_module._filled_login_fields(self.page))
        self.assertIsNone(page_module._login_submit_control(self.page))

    def test_two_filled_cards_fail_closed(self) -> None:
        self.page.set_content(TWO_LOGIN_CARDS)
        with self.assertRaisesRegex(LoginRequiredError, "无法唯一确认"):
            page_module._login_field_scope(self.page)

    def test_captcha_page_stops_the_session(self) -> None:
        self.page.set_content(
            '<!doctype html><html><body>'
            '<div class="nc-container" style="width:200px;height:60px"></div>'
            '<input type="password" value="x" /></body></html>'
        )
        with self.assertRaisesRegex(LoginRequiredError, "验证码"):
            page_module.ensure_lazada_session(self.page, allow_auth=True)

    def test_register_route_helper_detects_the_local_register_page(self) -> None:
        self.page.set_content(PREFILLED_LOGIN_CARD)
        self.assertIsNone(page_module._auth_route(self.page.url))

    def test_login_entry_click_reaches_into_iframes(self) -> None:
        """本地站的注册卡片常嵌在 iframe 里：只查主文档会点不到「已有账号？点击这里登录」。"""
        inner = '<a href="javascript:void(0)">登录</a>'
        self.page.set_content(
            '<html><body><iframe id="f" srcdoc="'
            + html.escape(inner, quote=True)
            + '"></iframe></body></html>'
        )
        self.page.wait_for_selector("iframe#f")
        self.page.wait_for_timeout(200)
        self.assertEqual(page_module.click_login_entry(self.page, page_module.LOGIN_NAMES), "登录")

    def test_login_entry_click_matches_a_link_that_merely_contains_it(self) -> None:
        """入口常写成「已有账号？点击这里登录」：链接文本不等于「登录」时也要能点到。"""
        self.page.set_content(
            '<html><body><p>已有账号？<a href="javascript:void(0)">点击这里登录</a></p></body></html>'
        )
        self.assertEqual(page_module.click_login_entry(self.page, page_module.LOGIN_NAMES), "登录")


class CollectPageTests(unittest.TestCase):
    """采集动作必须把「真正拿到数据的那一页」暴露出来：登录常会另开标签页。"""

    def _collect(self, actions, initial):
        return actions.collect(
            initial,
            "黄金娟-LZ马来001",
            COUNTRY_PROFILES["MY"],
            date(2026, 9, 1),
            date(2026, 9, 30),
        )

    def test_collect_exposes_the_page_it_finished_on(self) -> None:
        actions = page_module.PlaywrightLazadaBillPageActions()
        initial, working = object(), object()
        with patch.object(page_module, "open_income_details", return_value=working), \
                patch.object(page_module, "set_date_range"), \
                patch.object(
                    page_module, "collect_bill_metrics",
                    return_value={"total_amount": 1, "revenue": 2, "deductions": 3},
                ):
            metrics = self._collect(actions, initial)
        self.assertEqual(metrics["revenue"], 2)
        self.assertIs(actions.last_page, working, "必须是进站后实际工作的那一页")
        self.assertIsNot(actions.last_page, initial, "不能是调用方传进来的旧标签页")

    def test_collect_clears_the_previous_page_before_starting(self) -> None:
        actions = page_module.PlaywrightLazadaBillPageActions()
        actions.last_page = object()
        with patch.object(page_module, "open_income_details", side_effect=ValueError("进站失败")):
            with self.assertRaisesRegex(ValueError, "进站失败"):
                self._collect(actions, object())
        self.assertIsNone(actions.last_page, "上一家店的页面绝不能被当成这一家的截图对象")


if __name__ == "__main__":
    unittest.main()
