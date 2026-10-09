from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from backend.services import lazada_monthly_report as service


def _write_valid_xlsx(path: Path, payload: bytes = b"monthly-report") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", payload)
        archive.writestr("xl/workbook.xml", b"<workbook/>")


class MonthlyReportPayloadTests(unittest.TestCase):
    def test_previous_month_crosses_year_boundary(self) -> None:
        self.assertEqual(service.previous_month_value(date(2026, 1, 8)), "2025-12")

    def test_payload_defaults_to_previous_month_and_all_countries(self) -> None:
        job = service.validate_lazada_monthly_report_payload(
            {
                "username": "ziniao-user",
                "password": "secret",
                "store_names": "店铺 A\n店铺 A\n店铺 B",
                "output_dir": "D:/monthly-output",
                "max_concurrent_stores": "2",
            },
            today=date(2026, 8, 27),
        )
        self.assertEqual(job.month, "2026-07")
        self.assertEqual(job.countries, ("TH", "MY", "PH"))
        self.assertEqual(job.store_names, ("店铺 A", "店铺 B"))
        self.assertEqual(job.output_root, "D:/monthly-output")
        self.assertEqual(job.max_concurrent_stores, 2)

    def test_countries_array_takes_precedence_over_country(self) -> None:
        job = service.validate_lazada_monthly_report_payload(
            {
                "username": "u",
                "password": "p",
                "country": "TH",
                "countries": ["my", "PH", "MY"],
                "month": "2026-06",
                "store_names": "店铺",
            },
            today=date(2026, 8, 1),
        )
        self.assertEqual(job.countries, ("MY", "PH"))

    def test_single_country_is_normalized_to_tuple(self) -> None:
        job = service.validate_lazada_monthly_report_payload(
            {
                "username": "u",
                "password": "p",
                "country": "ph",
                "month": "2026-06",
                "store_names": "店铺",
            },
            today=date(2026, 8, 1),
        )
        self.assertEqual(job.countries, ("PH",))

    def test_invalid_or_future_month_is_rejected(self) -> None:
        base = {"username": "u", "password": "p", "country": "TH", "store_names": "店铺"}
        with self.assertRaisesRegex(ValueError, "YYYY-MM"):
            service.validate_lazada_monthly_report_payload(
                {**base, "month": "2026-7"}, today=date(2026, 8, 1)
            )
        with self.assertRaisesRegex(ValueError, "不得晚于"):
            service.validate_lazada_monthly_report_payload(
                {**base, "month": "2026-09"}, today=date(2026, 8, 1)
            )
        with self.assertRaisesRegex(ValueError, "不得晚于上个月"):
            service.validate_lazada_monthly_report_payload(
                {**base, "month": "2026-08"}, today=date(2026, 8, 27)
            )

    def test_unique_operator_prefixed_store_matches_core_name(self) -> None:
        requested = "LZ跨境马来萬中002"
        actual = "黄金娟-LZ跨境马来萬中002-半运营"
        matched, unmatched = service.match_lazada_browsers(
            [
                {
                    "browserName": actual,
                    "browserOauth": "browser-1",
                    "platform_name": "Lazada",
                }
            ],
            (requested,),
        )
        self.assertEqual(unmatched, [])
        self.assertEqual(matched[0][0], requested)
        self.assertEqual(matched[0][1]["browserName"], actual)

    def test_ambiguous_core_name_containment_is_rejected(self) -> None:
        requested = "LZ跨境马来萬中002"
        browsers = [
            {
                "browserName": f"{owner}-{requested}-半运营",
                "browserOauth": f"browser-{index}",
                "platform_name": "Lazada",
            }
            for index, owner in enumerate(("甲", "乙"), start=1)
        ]
        matched, unmatched = service.match_lazada_browsers(browsers, (requested,))
        self.assertEqual(matched, [])
        self.assertEqual(len(unmatched), 1)
        self.assertIn("多个", unmatched[0][1])

    def test_core_alias_does_not_match_numeric_prefix_neighbor(self) -> None:
        requested = "LZ跨境马来萬中002"
        matched, unmatched = service.match_lazada_browsers(
            [
                {
                    "browserName": "黄金娟-LZ跨境马来萬中0020-半运营",
                    "browserOauth": "browser-1",
                    "platform_name": "Lazada",
                }
            ],
            (requested,),
        )
        self.assertEqual(matched, [])
        self.assertEqual(unmatched[0][0], requested)

    def test_browser_claim_resolution_is_independent_of_request_order(self) -> None:
        core = "LZ跨境马来萬中002"
        full = f"黄金娟-{core}-半运营"
        browsers = [
            {
                "browserName": full,
                "browserOauth": "browser-1",
                "platform_name": "Lazada",
            }
        ]
        outcomes = []
        for requested in ((core, full), (full, core)):
            matched, unmatched = service.match_lazada_browsers(browsers, requested)
            outcomes.append(
                (
                    {name for name, _ in matched},
                    {name for name, _ in unmatched},
                )
            )
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0][0], {full})
        self.assertEqual(outcomes[0][1], {core})


class MonthlyPeriodTests(unittest.TestCase):
    def test_screenshot_style_english_period_is_parsed(self) -> None:
        actual = service.parse_monthly_report_period("01 Jul - 31 Jul 2026 下载")
        self.assertEqual(actual, (date(2026, 7, 1), date(2026, 7, 31)))
        self.assertTrue(service.period_matches_month(actual[0], actual[1], "2026-07"))

    def test_chinese_and_numeric_periods_are_parsed(self) -> None:
        self.assertEqual(
            service.parse_monthly_report_period("2026年2月1日 至 2026年2月28日"),
            (date(2026, 2, 1), date(2026, 2, 28)),
        )
        self.assertEqual(
            service.parse_monthly_report_period("2024-02-01 - 2024-02-29"),
            (date(2024, 2, 1), date(2024, 2, 29)),
        )

    def test_partial_or_intersecting_period_does_not_match(self) -> None:
        self.assertFalse(
            service.period_matches_month(date(2026, 6, 29), date(2026, 7, 5), "2026-07")
        )
        self.assertFalse(
            service.period_matches_month(date(2026, 7, 1), date(2026, 7, 30), "2026-07")
        )


class DownloadCompletionTests(unittest.TestCase):
    def test_windows_reserved_store_name_is_made_safe(self) -> None:
        self.assertEqual(service.safe_store_filename("CON.txt"), "_CON.txt")

    def test_temporary_files_are_ignored_and_stable_final_file_is_returned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            before = service.snapshot_download_files(root)
            (root / "report.xlsx.crdownload").write_bytes(b"incomplete")
            final = root / "report.xlsx"
            _write_valid_xlsx(final)
            actual = service.wait_for_new_download_file(
                root,
                before,
                timeout=1,
                poll_interval=0,
                sleep=lambda _seconds: None,
            )
            self.assertEqual(actual, final)

    def test_unchanged_preexisting_file_is_not_returned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            existing = root / "old.xlsx"
            _write_valid_xlsx(existing)
            before = service.snapshot_download_files(root)
            self.assertIsNone(
                service.wait_for_new_download_file(root, before, timeout=0, poll_interval=0)
            )

    def test_temporary_file_counts_as_direct_download_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            existing = root / "old.xlsx"
            _write_valid_xlsx(existing)
            before = service.snapshot_download_files(root)
            self.assertFalse(service.download_activity_started(root, before))
            (root / "new.xlsx.crdownload").write_bytes(b"partial")
            self.assertTrue(service.download_activity_started(root, before))

    def test_file_signature_must_match_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            invalid_files = {
                "fake.xlsx": b"PKnot-a-real-zip",
                "fake.xls": b"\xd0\xcf\x11\xe0" + (b"x" * 508),
                "fake.csv": b"<!doctype html><html><body>login</body></html>",
            }
            for name, content in invalid_files.items():
                with self.subTest(name=name):
                    fake = root / name
                    fake.write_bytes(content)
                    with self.assertRaises(service.LazadaMonthlyReportError):
                        service.validate_downloaded_report(fake)

    def test_valid_xlsx_zip_xls_and_csv_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            xlsx = root / "valid.xlsx"
            _write_valid_xlsx(xlsx)
            archive = root / "valid.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("report.csv", "order,amount\nA,1\n")
            xls = root / "valid.xls"
            xls.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + (b"\x00" * 504))
            csv = root / "valid.csv"
            csv.write_text("order,amount\nA,1\n", encoding="utf-8-sig")

            for path in (xlsx, archive, xls, csv):
                with self.subTest(path=path.name):
                    self.assertEqual(service.validate_downloaded_report(path), path)

    def test_store_named_report_is_sanitized_and_atomically_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "platform.xlsx"
            _write_valid_xlsx(source, b"first")
            output = service.finalize_store_named_report(source, root / "out", "店/铺:*?")
            self.assertEqual(output.name, "店_铺_.xlsx")
            self.assertTrue(output.read_bytes().startswith(b"PK"))
            self.assertEqual(list((root / "out").glob("*.part")), [])

            _write_valid_xlsx(source, b"second")
            replaced = service.finalize_store_named_report(source, root / "out", "店/铺:*?")
            self.assertEqual(replaced, output)
            with zipfile.ZipFile(replaced) as archive:
                self.assertEqual(archive.read("[Content_Types].xml"), b"second")


class DownloadOptionTests(unittest.TestCase):
    def test_login_page_detection_does_not_treat_income_route_as_login(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()
        self.assertTrue(
            actions._is_login_page_url(
                "https://sellercenter-my.lazada-seller.cn/apps/seller/login?redirect_url=x"
            )
        )
        self.assertFalse(
            actions._is_login_page_url(
                "https://sellercenter.lazada.com.my/apps/register/index?redirect_url=x"
            )
        )
        self.assertTrue(
            actions._is_auth_page_url(
                "https://sellercenter.lazada.com.my/apps/register/index?redirect_url=x"
            )
        )
        self.assertFalse(
            actions._is_login_page_url(
                "https://sellercenter-my.lazada-seller.cn/apps/seller/login-history"
            )
        )
        self.assertFalse(
            actions._is_login_page_url(
                "https://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index"
            )
        )

    def test_income_permission_denied_is_detected_from_live_page_wording(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()
        self.assertTrue(
            actions._income_permission_denied(
                "没有权限 抱歉，您无权访问此页面。必要的权限是 账户对账单"
            )
        )
        self.assertFalse(actions._income_permission_denied("我的收入 月度报告"))

    def test_register_page_uses_only_login_link_bound_to_exact_income_redirect(self) -> None:
        target = (
            "https://sellercenter-my.lazada-seller.cn/"
            "portal/apps/finance/myIncome/index"
        )

        class Page:
            url = (
                "https://sellercenter-my.lazada-seller.cn/apps/register/index"
                f"?redirect_url={target}"
            )

            def evaluate(self, _script):
                return [
                    "/apps/seller/login",
                    "/apps/seller/login?login=1&redirect_url="
                    "https%3A%2F%2Fsellercenter-my.lazada-seller.cn%2Fportal%2Fapps%2F"
                    "finance%2FmyIncome%2Findex",
                ]

        selected = service.PlaywrightMonthlyReportPageActions._trusted_register_login_url(
            Page(), "MY", target
        )
        self.assertIn("/apps/seller/login?", selected)
        self.assertIn("redirect_url=", selected)

    def test_register_login_link_with_wrong_redirect_is_rejected(self) -> None:
        target = (
            "https://sellercenter-my.lazada-seller.cn/"
            "portal/apps/finance/myIncome/index"
        )

        class Page:
            url = "https://sellercenter-my.lazada-seller.cn/apps/register/index"

            def evaluate(self, _script):
                return [
                    "/apps/seller/login?redirect_url="
                    "https%3A%2F%2Fevil.example%2Fcollect"
                ]

        self.assertEqual(
            service.PlaywrightMonthlyReportPageActions._trusted_register_login_url(
                Page(), "MY", target
            ),
            "",
        )

    def test_prefilled_login_is_submitted_then_income_route_is_reopened(self) -> None:
        class Body:
            def __init__(self, page) -> None:
                self.page = page

            def inner_text(self, timeout=0):
                return self.page.body

        class Page:
            def __init__(self) -> None:
                self.url = ""
                self.body = ""
                self.goto_calls = 0

            def goto(self, url, **_kwargs):
                self.goto_calls += 1
                if self.goto_calls == 1:
                    self.url = "https://sellercenter-my.lazada-seller.cn/apps/seller/login"
                    self.body = "使用密码登录"
                else:
                    self.url = url
                    self.body = "我的收入 月度报告"

            def locator(self, _selector):
                return Body(self)

            def evaluate(self, script):
                if "please complete the verification" in script:
                    return False
                if "buttons[0].click()" in script:
                    self.url = "https://sellercenter-my.lazada-seller.cn/portal/home"
                    self.body = "Lazada Seller Center"
                    return True
                return None

            def wait_for_timeout(self, _milliseconds):
                return None

        page = Page()
        service.PlaywrightMonthlyReportPageActions._open_income_page(page, "MY")
        self.assertEqual(page.goto_calls, 2)
        self.assertIn("/portal/apps/finance/myIncome/index", page.url)

    def test_permission_denied_after_prefilled_login_has_specific_error(self) -> None:
        class Body:
            def __init__(self, page) -> None:
                self.page = page

            def inner_text(self, timeout=0):
                return self.page.body

        class Page:
            url = ""
            body = ""

            def goto(self, _url, **_kwargs):
                self.url = "https://sellercenter-my.lazada-seller.cn/apps/seller/login"
                self.body = "使用密码登录"

            def locator(self, _selector):
                return Body(self)

            def evaluate(self, script):
                if "please complete the verification" in script:
                    return False
                if "buttons[0].click()" in script:
                    self.url = (
                        "https://sellercenter-my.lazada-seller.cn/"
                        "portal/apps/finance/myIncome/index"
                    )
                    self.body = "没有权限 抱歉，您无权访问此页面。必要的权限是 账户对账单"
                    return True
                return None

            def wait_for_timeout(self, _milliseconds):
                return None

        with self.assertRaisesRegex(service.LazadaMonthlyReportError, "账户对账单"):
            service.PlaywrightMonthlyReportPageActions._open_income_page(Page(), "MY")

    def test_gsp_account_statement_new_tab_is_used_without_direct_login(self) -> None:
        class Body:
            def __init__(self, page) -> None:
                self.page = page

            def inner_text(self, timeout=0):
                return self.page.body

        class Context:
            pages: list[object] = []

        class Page:
            def __init__(self, context, url: str, body: str = "") -> None:
                self.context = context
                self.url = url
                self.body = body

            def locator(self, _selector):
                return Body(self)

            def wait_for_timeout(self, _milliseconds):
                return None

        context = Context()
        gsp = Page(context, "https://gsp.lazada-seller.cn/portal/home/index")
        stale = Page(
            context,
            "https://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index",
            "我的收入 月度报告 旧标签",
        )
        opened = Page(
            context,
            "https://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index",
            "我的收入 收入账单 月度报告",
        )
        context.pages = [gsp, stale]

        def activate(_page, labels, action):
            if "账户对账单" in labels and action == "click":
                context.pages.append(opened)
            return True

        actions = service.PlaywrightMonthlyReportPageActions
        with (
            patch.object(actions, "_gsp_label_visible", return_value=True),
            patch.object(actions, "_activate_gsp_label", side_effect=activate),
            patch.object(
                actions,
                "_submit_prefilled_login",
                side_effect=AssertionError("GSP SSO 不得提交 Seller Center 登录表单"),
            ),
        ):
            selected = actions._open_income_page(gsp, "MY")

        self.assertIs(selected, opened)
        self.assertIsNot(selected, stale)

    def test_prefilled_gsp_login_reaches_home_before_account_statement(self) -> None:
        class Body:
            def __init__(self, page) -> None:
                self.page = page

            def inner_text(self, timeout=0):
                return self.page.body

        class Context:
            pages: list[object] = []

        class Page:
            def __init__(self, context, url: str, body: str = "") -> None:
                self.context = context
                self.url = url
                self.body = body

            def locator(self, _selector):
                return Body(self)

            def wait_for_timeout(self, _milliseconds):
                return None

        context = Context()
        gsp = Page(context, "https://gsp.lazada-seller.cn/page/login", "登录")
        opened = Page(
            context,
            "https://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index",
            "我的收入 收入账单 月度报告",
        )
        context.pages = [gsp]

        def submit_prefilled(selected):
            self.assertIs(selected, gsp)
            gsp.url = "https://gsp.lazada-seller.cn/portal/home/index"
            gsp.body = "财务 Lazada Cross Border"
            return True

        def activate(_page, labels, action):
            if "账户对账单" in labels and action == "click":
                context.pages.append(opened)
            return True

        actions = service.PlaywrightMonthlyReportPageActions
        with (
            patch.object(actions, "_visible_login_challenge", return_value=False),
            patch.object(actions, "_submit_prefilled_login", side_effect=submit_prefilled) as submit,
            patch.object(actions, "_gsp_label_visible", return_value=True),
            patch.object(actions, "_activate_gsp_label", side_effect=activate),
            patch.object(
                actions,
                "_open_income_page_direct",
                side_effect=AssertionError("GSP 登录成功后不得走 Seller Center 直登"),
            ),
        ):
            selected = actions._open_income_page(gsp, "MY")

        self.assertIs(selected, opened)
        submit.assert_called_once_with(gsp)

    def test_gsp_account_statement_same_tab_navigation_is_supported(self) -> None:
        class Body:
            def __init__(self, page) -> None:
                self.page = page

            def inner_text(self, timeout=0):
                return self.page.body

        class Context:
            pages: list[object] = []

        class Page:
            def __init__(self, context) -> None:
                self.context = context
                self.url = "https://gsp.lazada-seller.cn/portal/home/index"
                self.body = "Lazada Cross Border"

            def locator(self, _selector):
                return Body(self)

            def wait_for_timeout(self, _milliseconds):
                return None

        context = Context()
        gsp = Page(context)
        context.pages = [gsp]

        def activate(_page, labels, action):
            if "账户对账单" in labels and action == "click":
                gsp.url = (
                    "https://sellercenter-my.lazada-seller.cn/"
                    "portal/apps/finance/myIncome/index"
                )
                gsp.body = "我的收入 收入账单 月度报告"
            return True

        actions = service.PlaywrightMonthlyReportPageActions
        with (
            patch.object(actions, "_gsp_label_visible", return_value=True),
            patch.object(actions, "_activate_gsp_label", side_effect=activate),
        ):
            selected = actions._open_income_page(gsp, "MY")

        self.assertIs(selected, gsp)

    def test_gsp_can_reuse_an_already_open_country_income_tab(self) -> None:
        class Body:
            def __init__(self, page) -> None:
                self.page = page

            def inner_text(self, timeout=0):
                return self.page.body

        class Context:
            pages: list[object] = []

        class Page:
            def __init__(self, context, url: str, body: str = "") -> None:
                self.context = context
                self.url = url
                self.body = body

            def locator(self, _selector):
                return Body(self)

            def wait_for_timeout(self, _milliseconds):
                return None

        context = Context()
        gsp = Page(context, "https://gsp.lazada-seller.cn/portal/home/index")
        existing = Page(
            context,
            "https://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index",
            "我的收入 收入账单 月度报告",
        )
        context.pages = [gsp, existing]

        actions = service.PlaywrightMonthlyReportPageActions
        with (
            patch.object(actions, "_gsp_label_visible", return_value=True),
            patch.object(actions, "_activate_gsp_label", return_value=True),
        ):
            selected = actions._open_income_page(gsp, "MY")

        self.assertIs(selected, existing)

    def test_country_download_session_uses_only_the_matching_legacy_origin(self) -> None:
        expected_origins = {
            "TH": "https://sellercenter.lazada.co.th",
            "MY": "https://sellercenter.lazada.com.my",
            "PH": "https://sellercenter.lazada.com.ph",
        }

        for country, origin in expected_origins.items():
            with self.subTest(country=country):
                class Body:
                    def __init__(self, page) -> None:
                        self.page = page

                    def inner_text(self, timeout=0):
                        return self.page.body

                class BridgePage:
                    def __init__(self) -> None:
                        self.url = ""
                        self.body = ""
                        self.closed = False

                    def goto(self, url, **_kwargs):
                        self.url = url
                        self.body = "My Income Monthly Report"

                    def locator(self, _selector):
                        return Body(self)

                    def wait_for_timeout(self, _milliseconds):
                        return None

                    def close(self):
                        self.closed = True

                bridge = BridgePage()

                class Context:
                    def __init__(self) -> None:
                        self.cookie_urls: list[list[str]] = []

                    def new_page(self):
                        return bridge

                    def cookies(self, urls):
                        self.cookie_urls.append(urls)
                        return [{"name": "t_sid", "value": "signed-session"}]

                class Page:
                    context = Context()

                service.PlaywrightMonthlyReportPageActions._ensure_country_download_session(
                    Page(), country
                )
                expected_url = f"{origin}{service.INCOME_PAGE_PATH}"
                self.assertEqual(bridge.url, expected_url)
                self.assertEqual(Page.context.cookie_urls, [[expected_url]])
                self.assertTrue(bridge.closed)

    def test_country_download_session_rejects_auth_redirect_and_closes_page(self) -> None:
        class Body:
            def inner_text(self, timeout=0):
                return "登录"

        class BridgePage:
            url = ""
            closed = False

            def goto(self, _url, **_kwargs):
                self.url = "https://sellercenter.lazada.com.my/apps/seller/login"

            def locator(self, _selector):
                return Body()

            def wait_for_timeout(self, _milliseconds):
                return None

            def close(self):
                self.closed = True

        bridge = BridgePage()

        class Context:
            @staticmethod
            def new_page():
                return bridge

            @staticmethod
            def cookies(_urls):
                return []

        class Page:
            context = Context()

        with self.assertRaisesRegex(service.LazadaMonthlyReportError, "文件下载会话未登录"):
            service.PlaywrightMonthlyReportPageActions._ensure_country_download_session(
                Page(), "MY"
            )
        self.assertTrue(bridge.closed)

    def test_country_download_session_requires_target_route_and_t_sid(self) -> None:
        cases = (
            (
                "wrong-country",
                "https://sellercenter.lazada.com.ph/portal/apps/finance/myIncome/index",
                [{"name": "t_sid", "value": "wrong-country"}],
            ),
            (
                "missing-cookie",
                "https://sellercenter.lazada.com.my/portal/apps/finance/myIncome/index",
                [],
            ),
        )
        for name, final_url, cookies in cases:
            with self.subTest(case=name):
                class Body:
                    def inner_text(self, timeout=0):
                        return "My Income Monthly Report"

                class BridgePage:
                    url = ""
                    closed = False

                    def goto(self, _url, **_kwargs):
                        self.url = final_url

                    def locator(self, _selector):
                        return Body()

                    def wait_for_timeout(self, _milliseconds):
                        return None

                    def close(self):
                        self.closed = True

                bridge = BridgePage()

                class Context:
                    @staticmethod
                    def new_page():
                        return bridge

                    @staticmethod
                    def cookies(_urls):
                        return cookies

                class Page:
                    context = Context()

                with (
                    patch.object(service.time, "monotonic", side_effect=[0, 0, 31]),
                    self.assertRaisesRegex(service.LazadaMonthlyReportError, "未能建立"),
                ):
                    service.PlaywrightMonthlyReportPageActions._ensure_country_download_session(
                        Page(), "MY"
                    )
                self.assertTrue(bridge.closed)

    def test_existing_gsp_tab_is_preferred_over_direct_login_page(self) -> None:
        class Body:
            def __init__(self, page) -> None:
                self.page = page

            def inner_text(self, timeout=0):
                return self.page.body

        class Context:
            pages: list[object] = []

        class Page:
            def __init__(self, context, url: str, body: str = "") -> None:
                self.context = context
                self.url = url
                self.body = body

            def locator(self, _selector):
                return Body(self)

            def wait_for_timeout(self, _milliseconds):
                return None

        context = Context()
        login = Page(
            context,
            "https://sellercenter-my.lazada-seller.cn/apps/seller/login",
            "使用密码登录",
        )
        gsp = Page(context, "https://gsp.lazada-seller.cn/portal/home/index")
        opened = Page(
            context,
            "https://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index",
            "我的收入 收入账单 月度报告",
        )
        context.pages = [login, gsp]

        def activate(_page, labels, action):
            if "账户对账单" in labels and action == "click":
                context.pages.append(opened)
            return True

        actions = service.PlaywrightMonthlyReportPageActions
        with (
            patch.object(actions, "_gsp_label_visible", return_value=True),
            patch.object(actions, "_activate_gsp_label", side_effect=activate),
            patch.object(
                actions,
                "_submit_prefilled_login",
                side_effect=AssertionError("已有 GSP 标签时不得走直接登录"),
            ),
        ):
            selected = actions._open_income_page(login, "MY")

        self.assertIs(selected, opened)

    def test_gsp_auth_redirect_never_submits_prefilled_login(self) -> None:
        class Body:
            def __init__(self, page) -> None:
                self.page = page

            def inner_text(self, timeout=0):
                return self.page.body

        class Context:
            pages: list[object] = []

        class Page:
            def __init__(self, context, url: str, body: str = "") -> None:
                self.context = context
                self.url = url
                self.body = body

            def locator(self, _selector):
                return Body(self)

            def wait_for_timeout(self, _milliseconds):
                return None

        context = Context()
        gsp = Page(context, "https://gsp.lazada-seller.cn/portal/home/index")
        login = Page(
            context,
            "https://sellercenter-my.lazada-seller.cn/apps/seller/login",
            "使用密码登录",
        )
        context.pages = [gsp]

        def activate(_page, labels, action):
            if "账户对账单" in labels and action == "click":
                context.pages.append(login)
            return True

        actions = service.PlaywrightMonthlyReportPageActions
        with (
            patch.object(actions, "_gsp_label_visible", return_value=True),
            patch.object(actions, "_activate_gsp_label", side_effect=activate),
            patch.object(
                actions,
                "_submit_prefilled_login",
                side_effect=AssertionError("GSP SSO 不得提交 Seller Center 登录表单"),
            ),
            patch.object(
                service.time,
                "monotonic",
                side_effect=[0, 0, 0, 0, 0, 0, 121],
            ),
        ):
            with self.assertRaisesRegex(service.LazadaMonthlyReportError, "单点登录未完成"):
                actions._open_income_page(gsp, "MY")

    def test_gsp_permission_page_reports_specific_error(self) -> None:
        class Body:
            def __init__(self, page) -> None:
                self.page = page

            def inner_text(self, timeout=0):
                return self.page.body

        class Context:
            pages: list[object] = []

        class Page:
            def __init__(self, context, url: str, body: str = "") -> None:
                self.context = context
                self.url = url
                self.body = body

            def locator(self, _selector):
                return Body(self)

            def wait_for_timeout(self, _milliseconds):
                return None

        context = Context()
        gsp = Page(context, "https://gsp.lazada-seller.cn/portal/home/index")
        denied = Page(
            context,
            "https://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index",
            "没有权限 抱歉，您无权访问此页面。必要的权限是 账户对账单",
        )
        context.pages = [gsp]

        def activate(_page, labels, action):
            if "账户对账单" in labels and action == "click":
                context.pages.append(denied)
            return True

        actions = service.PlaywrightMonthlyReportPageActions
        with (
            patch.object(actions, "_gsp_label_visible", return_value=True),
            patch.object(actions, "_activate_gsp_label", side_effect=activate),
        ):
            with self.assertRaisesRegex(service.LazadaMonthlyReportError, "通过 GSP 进入后"):
                actions._open_income_page(gsp, "MY")

    def test_download_report_uses_page_returned_by_gsp_navigation(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()

        class Page:
            url = "https://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index"

        source_page = object()
        popup = Page()
        seen: list[object] = []
        actions._open_income_page = lambda _page, _country: popup
        actions._dismiss_safe_popups = lambda selected: seen.append(selected) or 0
        actions._click_monthly_report_tab = lambda selected: seen.append(selected) or True
        actions._wait_monthly_rows = lambda selected, **_kwargs: (
            seen.append(selected) or [{"periodText": "01 Jul - 31 Jul 2026"}]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            trusted = root / "trusted.xlsx"
            _write_valid_xlsx(trusted)

            def click_download(selected, _period):
                seen.append(selected)
                return True

            actions._ensure_country_download_session = lambda selected, _country: seen.append(selected)
            actions._click_row_download = click_download
            actions._wait_visible_download_options = lambda selected: (
                seen.append(selected) or ["交易详情 (excel)", "下载历史"]
            )
            actions._click_download_option = lambda selected, _option: seen.append(selected) or True
            actions._wait_export_history_snapshot = lambda selected: seen.append(selected) or set()
            actions._close_export_modal = lambda selected: seen.append(selected)
            actions._wait_for_export_history_state = (
                lambda selected, **_kwargs: seen.append(selected) or True
            )
            actions._trigger_download_option = lambda selected, _option, **_kwargs: (
                seen.append(selected) or True,
                "101",
            )
            actions._export_history_visible = lambda selected: seen.append(selected) or True
            actions._wait_async_export = lambda selected, _path, **_kwargs: (
                seen.append(selected) or trusted
            )
            with patch.object(
                service,
                "wait_for_new_download_file",
                side_effect=AssertionError("生产下载链路不得扫描目录猜测文件"),
            ):
                actions.download_report(source_page, "MY", "2026-07", root)

        self.assertTrue(seen)
        self.assertTrue(all(selected is popup for selected in seen))

    def test_unknown_login_host_never_receives_prefilled_credentials(self) -> None:
        class Body:
            def inner_text(self, timeout=0):
                return "使用密码登录"

        class Page:
            url = ""
            evaluate_calls = 0

            def goto(self, _url, **_kwargs):
                self.url = "https://evil.example/login"

            def locator(self, _selector):
                return Body()

            def evaluate(self, _script):
                self.evaluate_calls += 1
                return True

            def wait_for_timeout(self, _milliseconds):
                return None

        page = Page()
        with self.assertRaisesRegex(service.LazadaMonthlyReportError, "拒绝提交预填凭据"):
            service.PlaywrightMonthlyReportPageActions._open_income_page(page, "MY")
        self.assertEqual(page.evaluate_calls, 0)

    def test_login_challenge_inspection_error_fails_closed(self) -> None:
        class Page:
            def evaluate(self, _script):
                raise RuntimeError("execution context destroyed")

        with self.assertRaisesRegex(service.LazadaMonthlyReportError, "安全验证状态"):
            service.PlaywrightMonthlyReportPageActions._visible_login_challenge(Page())

    def test_login_submit_unknown_outcome_fails_without_retry(self) -> None:
        class Page:
            def evaluate(self, _script):
                raise RuntimeError("execution context destroyed")

        with self.assertRaisesRegex(service.LazadaMonthlyReportError, "避免重复提交"):
            service.PlaywrightMonthlyReportPageActions._submit_prefilled_login(Page())

    def test_evidence_backed_order_details_option_is_preferred(self) -> None:
        selected = service.PlaywrightMonthlyReportPageActions._choose_download_option(
            ["Income Overview", "Order Details (excel)"]
        )
        self.assertEqual(selected, "Order Details (excel)")

    def test_live_chinese_transaction_details_excel_option_is_supported(self) -> None:
        selected = service.PlaywrightMonthlyReportPageActions._choose_download_option(
            ["交易概览 (pdf)", "交易详情 (excel)", "交易详情 (csv)", "下载历史"]
        )
        self.assertEqual(selected, "交易详情 (excel)")

    def test_live_english_transactions_details_excel_option_is_supported(self) -> None:
        selected = service.PlaywrightMonthlyReportPageActions._choose_download_option(
            [
                "Transactions Overview (pdf)",
                "Transactions Details (excel)",
                "Transactions Details (csv)",
                "Download History",
            ]
        )
        self.assertEqual(selected, "Transactions Details (excel)")

    def test_download_history_option_must_be_unique(self) -> None:
        choose = service.PlaywrightMonthlyReportPageActions._choose_download_history_option
        self.assertEqual(choose(["交易详情 (excel)", "下载历史"]), "下载历史")
        self.assertEqual(choose(["下载历史", "下载历史"]), "")
        self.assertEqual(choose(["交易详情 (excel)"]), "")

    def test_single_adaptive_excel_report_option_is_allowed(self) -> None:
        selected = service.PlaywrightMonthlyReportPageActions._choose_download_option(
            ["Monthly Report XLSX", "PDF"]
        )
        self.assertEqual(selected, "Monthly Report XLSX")

    def test_ambiguous_unknown_excel_options_fail_closed(self) -> None:
        selected = service.PlaywrightMonthlyReportPageActions._choose_download_option(
            ["Report A Excel", "Report B Excel"]
        )
        self.assertEqual(selected, "")

    def test_unrelated_single_excel_report_is_not_selected(self) -> None:
        selected = service.PlaywrightMonthlyReportPageActions._choose_download_option(
            ["Tax Report Excel"]
        )
        self.assertEqual(selected, "")

    def test_duplicate_month_rows_fail_closed_before_clicking_download(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()
        actions._open_income_page = lambda selected_page, _country: selected_page
        actions._dismiss_safe_popups = lambda _page: 0
        actions._click_monthly_report_tab = lambda _page: True
        actions._wait_monthly_rows = lambda _page, **_kwargs: [
            {"periodText": "01 Jul - 31 Jul 2026"},
            {"periodText": "01 Jul - 31 Jul 2026"},
        ]
        actions._click_row_download = lambda _page, _period: self.fail("不应点击重复账期")

        with self.assertRaisesRegex(service.LazadaMonthlyReportError, "多个"):
            actions.download_report(object(), "TH", "2026-07", Path("."))

    def test_country_host_is_rechecked_immediately_before_download(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()
        actions._open_income_page = lambda selected_page, _country: selected_page
        actions._dismiss_safe_popups = lambda _page: 0
        actions._click_monthly_report_tab = lambda _page: True
        actions._wait_monthly_rows = lambda _page, **_kwargs: [
            {"periodText": "01 Jul - 31 Jul 2026"}
        ]
        actions._click_row_download = lambda _page, _period: self.fail("错误国家不得下载")

        class Page:
            url = "https://sellercenter-ph.lazada-seller.cn/portal/apps/finance/myIncome/index"

        with self.assertRaisesRegex(service.LazadaMonthlyReportError, "错误国家"):
            actions.download_report(Page(), "TH", "2026-07", Path("."))

    def test_async_export_binds_processing_record_instead_of_old_finished_record(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()
        rows = [
            [
                {"exportId": "100", "exportTime": "2026-08-27 11:30:00", "status": "Finished", "hasDownloadLink": True},
                {"exportId": "101", "exportTime": "2026-08-27 12:00:00", "status": "Processing", "hasDownloadLink": False},
            ],
            [
                {"exportId": "100", "exportTime": "2026-08-27 11:30:00", "status": "Finished", "hasDownloadLink": True},
                {"exportId": "101", "exportTime": "2026-08-27 12:00:00", "status": "Finished", "hasDownloadLink": True},
            ],
        ]
        selected: list[str] = []

        class Page:
            def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        def export_rows(_page):
            return rows.pop(0) if len(rows) > 1 else rows[0]

        actions._export_rows = export_rows
        downloaded = Path("new.xlsx")
        actions._download_export_link = (
            lambda _page, export_id, _path, **_kwargs: selected.append(export_id) or downloaded
        )
        actual = actions._wait_async_export(
            Page(),
            Path("."),
            expected_export_id="101",
            country="MY",
            timeout=1,
        )

        self.assertEqual(actual, downloaded)
        self.assertEqual(selected, ["101"])

    def test_export_id_is_extracted_from_nested_creation_response(self) -> None:
        self.assertEqual(
            service._find_export_id({"code": 0, "data": {"export_id": 12345}}),
            "12345",
        )
        self.assertEqual(service._find_export_id({"data": {"taskId": 99}}), "")
        self.assertEqual(
            service._find_creation_export_id(
                {
                    "api": "mtop.lazada.finance.sellerfund3.create.download.task",
                    "data": {"data": 8747563, "succeeded": True},
                }
            ),
            "8747563",
        )
        self.assertEqual(
            service._find_creation_export_id(
                {
                    "api": "mtop.lazada.finance.sellerfund3.query.download.task",
                    "data": {"data": 8747563, "succeeded": True},
                }
            ),
            "",
        )

    def test_only_exact_country_creation_api_response_is_accepted(self) -> None:
        class Request:
            method = "GET"

        class Response:
            request = Request()
            status = 200

            def __init__(self, url: str) -> None:
                self.url = url

        request_data = quote(
            json.dumps(
                {"taskType": "Export", "businessCode": "LAZADA_MY_finance-"},
                separators=(",", ":"),
            )
        )
        valid = (
            "https://m-my.lazada-seller.cn"
            f"{service.EXPORT_CREATION_API_PATH}?data={request_data}"
        )
        self.assertTrue(service._is_export_creation_response(Response(valid), "MY"))
        self.assertFalse(service._is_export_creation_response(Response(valid), "TH"))
        self.assertFalse(
            service._is_export_creation_response(
                Response(valid.replace("create.download.task", "query.download.task")),
                "MY",
            )
        )
        self.assertFalse(
            service._is_export_creation_response(
                Response(valid.replace("LAZADA_MY_finance-", "LAZADA_PH_finance-")),
                "MY",
            )
        )

    def test_trigger_download_binds_id_from_exact_creation_response(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()
        request_data = quote(
            json.dumps(
                {"taskType": "Export", "businessCode": "LAZADA_MY_finance-"},
                separators=(",", ":"),
            )
        )

        class Request:
            method = "GET"

        class Response:
            request = Request()
            status = 200
            url = (
                "https://m-my.lazada-seller.cn"
                f"{service.EXPORT_CREATION_API_PATH}?data={request_data}"
            )

            @staticmethod
            def json():
                return {
                    "api": "mtop.lazada.finance.sellerfund3.create.download.task",
                    "data": {"data": 8747563, "succeeded": True},
                }

        class ResponseInfo:
            value = Response()

        class ResponseContext:
            def __enter__(self):
                return ResponseInfo()

            def __exit__(self, _exc_type, _exc, _traceback):
                return False

        class Page:
            @staticmethod
            def expect_response(predicate, timeout):
                self.assertEqual(timeout, 15_000)
                self.assertTrue(predicate(Response()))
                return ResponseContext()

        with patch.object(
            service.PlaywrightMonthlyReportPageActions,
            "_click_download_option",
            return_value=True,
        ):
            clicked, export_id = actions._trigger_download_option(
                Page(), "交易详情 (excel)", country="MY"
            )
        self.assertTrue(clicked)
        self.assertEqual(export_id, "8747563")

    def test_async_export_never_clicks_history_without_id_or_baseline(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()
        selected: list[str] = []

        class Page:
            def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        actions._export_rows = lambda _page: [
            {"exportId": "100", "exportTime": "2026-08-27 12:00:00", "status": "Finished", "hasDownloadLink": True}
        ]
        actions._click_export_link = lambda _page, export_id: selected.append(export_id) or True
        with self.assertRaisesRegex(service.LazadaMonthlyReportError, "唯一 ID"):
            actions._wait_async_export(
                Page(),
                Path("."),
                timeout=0.01,
            )

        self.assertEqual(selected, [])

    def test_confirmed_empty_history_never_guesses_even_perfect_looking_row(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()
        selected: list[str] = []

        class Page:
            def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        actions._export_rows = lambda _page: [
            {
                "exportId": "8747168",
                "exportTime": "2026-08-27 12:00:00",
                "fileName": "MY4NK2GCRT-TRANSACTION-8747168-20260827.xlsx",
                "status": "Finished.",
                "hasDownloadLink": True,
            }
        ]
        actions._download_export_link = (
            lambda _page, export_id, _path, **_kwargs: selected.append(export_id) or Path("new.xlsx")
        )
        with self.assertRaisesRegex(service.LazadaMonthlyReportError, "唯一 ID"):
            actions._wait_async_export(
                Page(),
                Path("."),
                existing_export_ids=set(),
                country="MY",
                timeout=0.01,
            )
        self.assertEqual(selected, [])

    def test_history_snapshot_waits_for_async_old_rows(self) -> None:
        states = [
            {"visible": True, "ready": False, "ids": []},
            {"visible": True, "ready": True, "ids": ["8738588"]},
            {"visible": True, "ready": True, "ids": ["8738588"]},
        ]

        class Page:
            def evaluate(self, _script):
                return states.pop(0) if len(states) > 1 else states[0]

            def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        snapshot = service.PlaywrightMonthlyReportPageActions()._wait_export_history_snapshot(
            Page(), timeout=1
        )
        self.assertEqual(snapshot, {"8738588"})

    def test_history_snapshot_does_not_accept_brief_empty_state(self) -> None:
        states = [
            {"visible": True, "ready": True, "ids": []},
            {"visible": True, "ready": True, "ids": []},
            {"visible": True, "ready": True, "ids": ["8738588"]},
            {"visible": True, "ready": True, "ids": ["8738588"]},
        ]

        class Page:
            def evaluate(self, _script):
                return states.pop(0) if len(states) > 1 else states[0]

            def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        snapshot = service.PlaywrightMonthlyReportPageActions()._wait_export_history_snapshot(
            Page(), timeout=1
        )
        self.assertEqual(snapshot, {"8738588"})

    def test_missing_history_stops_before_excel_export(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()
        actions._open_income_page = lambda selected_page, _country: selected_page
        actions._dismiss_safe_popups = lambda _page: 0
        actions._click_monthly_report_tab = lambda _page: True
        actions._wait_monthly_rows = lambda _page, **_kwargs: [
            {"periodText": "01 Jul - 31 Jul 2026"}
        ]
        actions._click_row_download = lambda _page, _period: True
        actions._ensure_country_download_session = lambda _page, _country: None
        actions._wait_visible_download_options = lambda _page: ["交易详情 (excel)"]
        actions._trigger_download_option = (
            lambda _page, _option, **_kwargs: self.fail("不应触发 Excel 导出")
        )

        class Page:
            url = "https://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index"

        with self.assertRaisesRegex(service.LazadaMonthlyReportError, "下载历史"):
            actions.download_report(Page(), "MY", "2026-07", Path("."))

    def test_export_time_requires_seconds_and_must_not_precede_trigger(self) -> None:
        trigger = datetime(2026, 8, 27, 12, 0, 0)
        self.assertTrue(service._is_fresh_export_time("2026-08-27 12:00:00", trigger))
        self.assertFalse(service._is_fresh_export_time("2026-08-27 11:59:57", trigger))
        self.assertFalse(service._is_fresh_export_time("2026-08-27 11:59:49", trigger))
        self.assertFalse(service._is_fresh_export_time("2026-08-27 12:00", trigger))

    def test_older_month_uses_more_report_page_before_declaring_no_report(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()

        class Page:
            url = "https://sellercenter-th.lazada-seller.cn/portal/apps/finance/myIncome/index"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            trusted = root / "trusted.xlsx"
            _write_valid_xlsx(trusted)
            actions._open_income_page = lambda selected_page, _country: selected_page
            actions._dismiss_safe_popups = lambda _page: 0
            actions._click_monthly_report_tab = lambda _page: True
            actions._wait_monthly_rows = lambda _page, **_kwargs: [
                {"periodText": "01 Aug - 31 Aug 2026"}
            ]
            actions._click_more_monthly_reports = lambda _page: True
            actions._wait_for_monthly_rows_change = lambda _page, _signature: [
                {"periodText": "01 Jul - 31 Jul 2026"}
            ]
            actions._search_monthly_report_pages = lambda _page, _month, **_kwargs: [
                {"periodText": "01 Jul - 31 Jul 2026"}
            ]

            actions._ensure_country_download_session = lambda _page, _country: None
            actions._click_row_download = lambda _page, _period: True
            actions._wait_visible_download_options = lambda _page: [
                "交易详情 (excel)",
                "下载历史",
            ]
            actions._click_download_option = lambda _page, _option: True
            actions._wait_export_history_snapshot = lambda _page: set()
            actions._close_export_modal = lambda _page: None
            actions._wait_for_export_history_state = lambda _page, **_kwargs: True
            actions._trigger_download_option = lambda _page, _option, **_kwargs: (True, "101")
            actions._export_history_visible = lambda _page: True
            actions._wait_async_export = lambda _page, _path, **_kwargs: trusted
            report = actions.download_report(Page(), "TH", "2026-07", root)

            self.assertEqual(report.period_start, date(2026, 7, 1))
            self.assertEqual(report.period_end, date(2026, 7, 31))

    def test_pagination_waits_for_row_signature_change(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()
        page_one = [{"periodText": "01 Jul - 31 Jul 2026"}]
        page_two = [{"periodText": "01 Jun - 30 Jun 2026"}]
        next_clicks: list[bool] = []
        actions._click_next_monthly_page = lambda _page: next_clicks.append(True) or True
        actions._wait_for_monthly_rows_change = lambda _page, signature: (
            page_two if signature == ("01 Jul - 31 Jul 2026",) else []
        )

        rows = actions._search_monthly_report_pages(
            object(),
            "2026-06",
            initial_rows=page_one,
        )

        self.assertEqual(rows, page_two)
        self.assertEqual(next_clicks, [True])

    def test_async_export_uses_response_id_amid_concurrent_new_export(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()
        selected: list[str] = []

        class Page:
            def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        rows = [
            [
                {"exportId": "100", "status": "Finished", "hasDownloadLink": True},
                {"exportId": "101", "status": "Finished", "hasDownloadLink": True},
                {"exportId": "102", "status": "Processing", "hasDownloadLink": False},
            ],
            [
                {"exportId": "100", "status": "Finished", "hasDownloadLink": True},
                {"exportId": "101", "status": "Finished", "hasDownloadLink": True},
                {"exportId": "102", "status": "Finished", "hasDownloadLink": True},
            ],
        ]
        actions._export_rows = lambda _page: rows.pop(0) if len(rows) > 1 else rows[0]
        downloaded = Path("new.xlsx")
        actions._download_export_link = (
            lambda _page, export_id, _path, **_kwargs: selected.append(export_id) or downloaded
        )
        actual = actions._wait_async_export(
            Page(),
            Path("."),
            existing_export_ids={"100"},
            expected_export_id="102",
            country="MY",
            timeout=1,
        )

        self.assertEqual(actual, downloaded)
        self.assertEqual(selected, ["102"])

    def test_response_export_id_already_in_baseline_is_rejected(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()
        selected: list[str] = []

        class Page:
            def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        actions._export_rows = lambda _page: [
            {"exportId": "100", "status": "Finished", "hasDownloadLink": True}
        ]
        actions._click_export_link = lambda _page, export_id: selected.append(export_id) or True
        with self.assertRaisesRegex(service.LazadaMonthlyReportError, "点击前已经存在"):
            actions._wait_async_export(
                Page(),
                Path("."),
                existing_export_ids={"100"},
                expected_export_id="100",
                timeout=0.01,
            )

        self.assertEqual(selected, [])

    def test_export_download_event_is_saved_explicitly(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()
        actions._export_link_info = lambda _page, _export_id: {
            "href": "https://fbprivacy.lazada.com.my/sf-t/report.xlsx?authkey=signed",
            "fileName": "MY-TRANSACTION-8747168.xlsx",
        }
        selected: list[str] = []
        actions._click_export_link = lambda _page, export_id: selected.append(export_id) or True

        class Download:
            url = "https://fbprivacy.lazada.com.my/sf-t/report.xlsx?authkey=signed"

            @staticmethod
            def failure():
                return None

            @staticmethod
            def save_as(path: str) -> None:
                _write_valid_xlsx(Path(path))

        class DownloadEvent:
            value = Download()

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                return False

        class Page:
            @staticmethod
            def expect_download(timeout: int):
                self.assertEqual(timeout, 60_000)
                return DownloadEvent()

        with tempfile.TemporaryDirectory() as temp_dir:
            source = actions._download_export_link(
                Page(), "8747168", Path(temp_dir), country="MY"
            )
            self.assertEqual(source.name, "lazada-export-8747168.xlsx")
            self.assertEqual(service.validate_downloaded_report(source), source)
        self.assertEqual(selected, ["8747168"])

    def test_download_event_url_must_match_country_even_when_dom_href_is_trusted(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions()
        actions._export_link_info = lambda _page, _export_id: {
            "href": "https://fbprivacy.lazada.com.my/sf-t/report.xlsx?authkey=signed",
            "fileName": "MY-TRANSACTION-8747168.xlsx",
        }
        actions._click_export_link = lambda _page, _export_id: True
        saved: list[str] = []

        class Download:
            url = "https://fbprivacy.lazada.co.th/sf-t/report.xlsx?authkey=signed"

            @staticmethod
            def failure():
                return None

            @staticmethod
            def save_as(path: str) -> None:
                saved.append(path)

        class DownloadEvent:
            value = Download()

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                return False

        class Page:
            @staticmethod
            def expect_download(timeout: int):
                return DownloadEvent()

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(service.LazadaMonthlyReportError, "浏览器下载事件"):
                actions._download_export_link(
                    Page(), "8747168", Path(temp_dir), country="MY"
                )
        self.assertEqual(saved, [])

    def test_export_download_url_requires_exact_country_host(self) -> None:
        self.assertTrue(
            service._url_matches_report_download(
                "https://fbprivacy.lazada.com.my/sf-t/report.xlsx?authkey=signed", "MY"
            )
        )
        self.assertTrue(
            service._url_matches_report_download(
                "https://fbprivacy.lazada.co.th/sf-t/report.xlsx", "TH"
            )
        )
        self.assertTrue(
            service._url_matches_report_download(
                "https://fbprivacy.lazada.com.ph/sf-t/report.xlsx", "PH"
            )
        )
        for url, country in (
            ("http://fbprivacy.lazada.com.my/sf-t/report.xlsx", "MY"),
            ("https://fbprivacy.lazada.com.my.evil.test/sf-t/report.xlsx", "MY"),
            ("https://fbprivacy.lazada.co.th/sf-t/report.xlsx", "MY"),
            ("https://fbprivacy.lazada.com.my/other/report.xlsx", "MY"),
        ):
            with self.subTest(url=url):
                self.assertFalse(service._url_matches_report_download(url, country))


class FakeRuntime:
    def __init__(self, browsers: list[dict[str, str]], *, shutdown_error: bool = False) -> None:
        self.browsers = browsers
        self.shutdown_error = shutdown_error
        self.started = False
        self.shutdown_called = False
        self.closed: list[str] = []

    def start(self) -> None:
        self.started = True

    def list_browsers(self) -> list[dict[str, str]]:
        return self.browsers

    def open_browser(self, browser: dict[str, str], download_path: Path) -> service.OpenedBrowser:
        download_path.mkdir(parents=True, exist_ok=True)
        return service.OpenedBrowser(
            browser_name=browser["browserName"],
            page=object(),
            download_path=download_path,
            browser_oauth=browser["browserOauth"],
        )

    def close_browser(self, opened: service.OpenedBrowser) -> None:
        self.closed.append(opened.browser_name)

    def shutdown(self) -> None:
        self.shutdown_called = True
        if self.shutdown_error:
            raise RuntimeError("shutdown failed")


class FakeActions:
    def __init__(self, *, mismatched_period: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.mismatched_period = mismatched_period

    def download_report(
        self,
        _page: object,
        country: str,
        month: str,
        download_path: Path,
        _progress=None,
    ) -> service.MonthlyReportDownload:
        self.calls.append((country, month))
        source = download_path / f"platform-{country}.xlsx"
        _write_valid_xlsx(source, country.encode("ascii"))
        start, end = service.month_date_range(month)
        if self.mismatched_period:
            end = date(start.year, start.month, 7)
        return service.MonthlyReportDownload(source, f"{start} - {end}", start, end)


class MonthlyReportRunnerTests(unittest.TestCase):
    def _job(self, output_root: str, countries=("TH", "MY", "PH")) -> service.LazadaMonthlyReportQuery:
        return service.LazadaMonthlyReportQuery(
            company="company",
            username="user",
            password="password",
            countries=tuple(countries),
            month="2026-07",
            store_names=("LZ跨境测试主",),
            output_root=output_root,
        )

    def test_fake_runtime_runs_three_countries_and_names_each_file_by_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = FakeRuntime(
                [
                    {
                        "browserName": "LZ跨境测试主",
                        "browserOauth": "browser-1",
                        "platform_name": "Lazada",
                    }
                ]
            )
            actions = FakeActions()
            result = service.run_lazada_monthly_report(
                self._job(temp_dir),
                runtime_factory=lambda _job, _logger: runtime,
                page_actions=actions,
            )
            self.assertTrue(result["success"])
            self.assertEqual(result["success_store_count"], 3)
            self.assertEqual(actions.calls, [("TH", "2026-07"), ("MY", "2026-07"), ("PH", "2026-07")])
            self.assertTrue(runtime.started)
            self.assertTrue(runtime.shutdown_called)
            self.assertEqual(runtime.closed, ["LZ跨境测试主"])
            for item in result["stores"]:
                path = Path(item["local_file"])
                self.assertEqual(path.name, "LZ跨境测试主.xlsx")
                self.assertEqual(path.parent.name, "2026-07")
                self.assertEqual(path.parent.parent.name, item["country"])
            persisted = json.loads(Path(result["output_file"]).read_text(encoding="utf-8"))
            self.assertEqual(persisted["stores"], result["stores"])

    def test_operator_prefixed_profile_saves_file_with_requested_core_store_name(self) -> None:
        requested = "LZ跨境马来萬中002"
        actual = "黄金娟-LZ跨境马来萬中002-半运营"
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = FakeRuntime(
                [
                    {
                        "browserName": actual,
                        "browserOauth": "browser-1",
                        "platform_name": "Lazada",
                    }
                ]
            )
            job = service.LazadaMonthlyReportQuery(
                company="company",
                username="user",
                password="password",
                countries=("MY",),
                month="2026-07",
                store_names=(requested,),
                output_root=temp_dir,
            )
            result = service.run_lazada_monthly_report(
                job,
                runtime_factory=lambda _job, _logger: runtime,
                page_actions=FakeActions(),
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["stores"][0]["store_name"], actual)
            self.assertEqual(Path(result["stores"][0]["local_file"]).name, f"{requested}.xlsx")

    def test_runner_rejects_a_download_whose_period_only_intersects_month(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = FakeRuntime(
                [
                    {
                        "browserName": "LZ跨境测试主",
                        "browserOauth": "browser-1",
                        "platform_name": "Lazada",
                    }
                ]
            )
            result = service.run_lazada_monthly_report(
                self._job(temp_dir, countries=("TH",)),
                runtime_factory=lambda _job, _logger: runtime,
                page_actions=FakeActions(mismatched_period=True),
            )
            self.assertFalse(result["success"])
            self.assertEqual(result["stores"][0]["status"], "failed")
            self.assertIn("不完全一致", result["stores"][0]["message"])
            self.assertEqual(result["stores"][0]["local_file"], "")

    def test_no_report_is_counted_as_completed_outcome_not_failure(self) -> None:
        class NoReportActions:
            @staticmethod
            def download_report(*_args, **_kwargs):
                raise service.MonthlyReportNotFoundError("页面没有 2026-07 的完整自然月报告")

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = FakeRuntime(
                [
                    {
                        "browserName": "LZ跨境测试主",
                        "browserOauth": "browser-1",
                        "platform_name": "Lazada",
                    }
                ]
            )
            result = service.run_lazada_monthly_report(
                self._job(temp_dir, countries=("MY",)),
                runtime_factory=lambda _job, _logger: runtime,
                page_actions=NoReportActions(),
            )

            self.assertTrue(result["success"])
            self.assertEqual(result["result_count"], 1)
            self.assertEqual(result["success_store_count"], 0)
            self.assertEqual(result["no_report_count"], 1)
            self.assertEqual(result["failed_store_count"], 0)
            self.assertEqual(result["stores"][0]["status"], "no_report")
            persisted = json.loads(Path(result["output_file"]).read_text(encoding="utf-8"))
            self.assertEqual(persisted["failed_store_count"], 0)

    def test_shutdown_failure_keeps_result_and_releases_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = FakeRuntime(
                [
                    {
                        "browserName": "LZ跨境测试主",
                        "browserOauth": "browser-1",
                        "platform_name": "Lazada",
                    }
                ],
                shutdown_error=True,
            )
            result = service.run_lazada_monthly_report(
                self._job(temp_dir, countries=("TH",)),
                runtime_factory=lambda _job, _logger: runtime,
                page_actions=FakeActions(),
            )
            self.assertTrue(result["success"])
            self.assertTrue(Path(result["output_file"]).is_file())
            log_file = Path(result["log_file"])
            renamed = log_file.with_name("run-closed.log")
            log_file.rename(renamed)
            self.assertIn("清理紫鸟运行时失败", renamed.read_text(encoding="utf-8"))

    def test_filename_collision_fails_closed_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = FakeRuntime(
                [
                    {"browserName": "Shop/A", "browserOauth": "browser-1", "platform_name": "Lazada"},
                    {"browserName": "Shop:A", "browserOauth": "browser-2", "platform_name": "Lazada"},
                ]
            )
            job = service.LazadaMonthlyReportQuery(
                company="company",
                username="user",
                password="password",
                countries=("TH",),
                month="2026-07",
                store_names=("Shop/A", "Shop:A"),
                output_root=temp_dir,
            )
            actions = FakeActions()
            result = service.run_lazada_monthly_report(
                job,
                runtime_factory=lambda _job, _logger: runtime,
                page_actions=actions,
            )

            self.assertFalse(result["success"])
            self.assertEqual(actions.calls, [])
            self.assertEqual(
                [item["status"] for item in result["stores"]],
                ["filename_collision", "filename_collision"],
            )


class BrowserSafetyTests(unittest.TestCase):
    def test_gsp_url_requires_exact_https_host(self) -> None:
        self.assertTrue(service._url_matches_gsp("https://gsp.lazada-seller.cn/portal/home/index"))
        self.assertTrue(service._url_matches_gsp("https://gsp.lazada-seller.cn:443/portal/home/index"))
        self.assertTrue(service._url_matches_gsp("https://gsp.lazada.com/portal/home/index"))
        for candidate in (
            "http://gsp.lazada-seller.cn/portal/home/index",
            "https://gsp.lazada-seller.cn:8443/portal/home/index",
            "https://gsp.lazada-seller.cn.evil.example/portal/home/index",
            "https://evil.example/?next=https://gsp.lazada-seller.cn/portal/home/index",
            "https://gsp.lazada.com.evil.example/portal/home/index",
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(service._url_matches_gsp(candidate))

    def test_gsp_login_and_home_routes_are_exact(self) -> None:
        actions = service.PlaywrightMonthlyReportPageActions
        self.assertTrue(actions._is_gsp_login_url("https://gsp.lazada-seller.cn/page/login"))
        self.assertTrue(actions._is_gsp_home_url("https://gsp.lazada-seller.cn/portal/home/index"))
        self.assertFalse(actions._is_gsp_login_url("https://gsp.lazada-seller.cn/page/login-history"))
        self.assertFalse(actions._is_gsp_login_url("https://evil.example/page/login"))
        self.assertFalse(actions._is_gsp_home_url("https://gsp.lazada-seller.cn.evil/portal/home/index"))

    def test_income_route_requires_exact_country_host_and_path(self) -> None:
        self.assertTrue(
            service._income_route_matches_country(
                "https://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index?spm=x",
                "MY",
            )
        )
        for candidate in (
            "https://sellercenter-ph.lazada-seller.cn/portal/apps/finance/myIncome/index",
            "https://sellercenter-my.lazada-seller.cn/portal/home/index",
            "https://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index/extra",
            "http://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index",
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(service._income_route_matches_country(candidate, "MY"))

    def test_closed_gsp_tab_is_ignored(self) -> None:
        class Context:
            pages: list[object] = []

        class Page:
            def __init__(self, context, url: str, closed: bool = False) -> None:
                self.context = context
                self.url = url
                self.closed = closed

            def is_closed(self):
                return self.closed

        context = Context()
        current = Page(context, "about:blank")
        closed_gsp = Page(
            context,
            "https://gsp.lazada-seller.cn/portal/home/index",
            closed=True,
        )
        context.pages = [current, closed_gsp]

        self.assertIsNone(service.PlaywrightMonthlyReportPageActions._find_gsp_page(current))

    def test_runtime_prefers_gsp_home_over_later_unrelated_page(self) -> None:
        class Page:
            def __init__(self, url: str) -> None:
                self.url = url

            def is_closed(self):
                return False

        class Context:
            pages = [
                Page("https://gsp.lazada-seller.cn/portal/home/index"),
                Page("https://sellercenter-my.lazada-seller.cn/apps/seller/login"),
            ]

        selected = service.ZiniaoPlaywrightRuntime._wait_for_gsp_page(Context(), timeout=0)
        self.assertEqual(selected.url, "https://gsp.lazada-seller.cn/portal/home/index")

    def test_country_url_check_uses_hostname_not_query_substring(self) -> None:
        self.assertTrue(
            service._url_matches_country(
                "https://sellercenter-my.lazada-seller.cn/portal/apps/finance/myIncome/index",
                "MY",
            )
        )
        self.assertFalse(
            service._url_matches_country(
                "https://evil.example/login?next=https://sellercenter-my.lazada-seller.cn/",
                "MY",
            )
        )
        self.assertFalse(
            service._url_matches_country("https://sellercenter-ph.lazada-seller.cn/", "MY")
        )
        self.assertFalse(
            service._url_matches_country(
                "http://sellercenter-my.lazada-seller.cn/apps/seller/login", "MY"
            )
        )
        self.assertFalse(
            service._url_matches_country(
                "https://sellercenter-my.lazada-seller.cn:8443/apps/seller/login", "MY"
            )
        )

    def test_store_matching_does_not_accept_substring_only_name(self) -> None:
        browsers = [
            {
                "browserName": "LZ跨境测试主-错误后缀",
                "browserOauth": "browser-1",
                "platform_name": "Lazada",
            }
        ]
        matched, unmatched = service.match_lazada_browsers(browsers, ["LZ跨境测试主"])
        self.assertEqual(matched, [])
        self.assertEqual(unmatched[0][0], "LZ跨境测试主")


if __name__ == "__main__":
    unittest.main()
