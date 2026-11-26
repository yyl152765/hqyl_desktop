"""Synthetic regressions: a missing bill is an observed response, never a timeout."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.services import shopee_id_income_report as income
from backend.services import shopee_id_monthly_invoice as service
from backend.services.shopee_id_invoice_gateway import ShopeeIdInvoiceGateway
from tests.shopee_id_fixtures import income_file
from tests.test_shopee_id_income_report import ReportBrowser, history_row
from tests.test_shopee_id_invoice_gateway import FakeWebDriverBrowser
from tests.test_shopee_id_monthly_invoice import FakeInvoiceGateway


def empty_income():
    return {"no_data": True, "source": "empty", "period_start": "2026-10-01", "period_end": "2026-10-31",
            "empty_evidence": {"month": "2026-10", "date_range": "2026100120261031", "history_checked": True,
                               "reason": "export_no_data", "export_notice": income.NO_DOWNLOAD_DATA_NOTICE,
                               "transaction_row_count": 0, "transaction_empty_text": "No Data"}}


def empty_result():
    return {"files": [], "row_count": 0,
            "evidence": {"income_no_data": empty_income()["empty_evidence"],
                         "selected_month": "October 2026", "empty_text": "No receipt found"}}


class EmptyReportBrowser(ReportBrowser):
    def __init__(self, pages=None):
        super().__init__([[]] if pages is None else pages)
        self.exported = False
        self.no_data_response = True
        self.stale_notice = False
        self.page_overrides = {}
        self.state_overrides = {}

    def click(self, store_id, selector):
        if selector == "#export" and self.no_data_response:
            self.clicks.append(selector)
            self.exported = True
        else:
            super().click(store_id, selector)

    def evaluate(self, store_id, script):
        state = super().evaluate(store_id, script)
        state["income_page"].update(
            transaction_table_count=1, transaction_body_present=True, transaction_row_count=0,
            transaction_empty_text="No Data",
            export_notices=[income.NO_DOWNLOAD_DATA_NOTICE] if self.exported or self.stale_notice else [],
        )
        state["income_page"].update(self.page_overrides)
        if self.exported:
            state.update(self.state_overrides)
        return state


class EmptyMonthTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = {"store_id": "synthetic", "store_name": "测试-印尼001", "platform_name": "Shopee"}

    def export_income(self, browser):
        return income.export_monthly_income_report(
            browser, "synthetic", "2026-10", self.root / "income", timeout=.06,
            generation_timeout=.06, download_timeout=.1, poll_interval=.005, refresh_interval=.005)

    def test_no_data_response_finishes_without_generation_wait_or_file(self):
        browser = EmptyReportBrowser()
        result = self.export_income(browser)
        self.assertTrue(result["no_data"])
        self.assertTrue(income.validate_empty_income_evidence(result["empty_evidence"], "2026-10"))
        self.assertEqual(browser.clicks, ["#export"])
        self.assertFalse((self.root / "income").exists())

    def test_history_report_takes_precedence_over_empty_order_table(self):
        for status in ("Download", "Processing"):
            with self.subTest(status=status):
                browser = EmptyReportBrowser([[history_row(status=status)]])
                result = self.export_income(browser)
                self.assertEqual(result["source"], "history")
                self.assertEqual(browser.clicks, ["#download-1"])
                Path(result["path"]).unlink()

    def test_empty_orders_can_still_generate_a_report_with_adjustments(self):
        browser = EmptyReportBrowser()
        browser.no_data_response = False
        result = self.export_income(browser)
        self.assertEqual(result["source"], "generated")
        self.assertEqual(browser.clicks, ["#export", "#download-1"])

    def test_timeout_is_not_empty_and_export_never_repeated(self):
        browser = EmptyReportBrowser()
        browser.no_data_response = False
        browser.ready_on_refresh = 10000
        with self.assertRaisesRegex(ValueError, "未重复点击 Export"):
            self.export_income(browser)
        self.assertEqual(browser.clicks, ["#export"])

    def test_absent_history_stale_notice_disabled_export_do_not_confirm_empty(self):
        for case in ("history", "notice", "disabled", "wrong_month"):
            with self.subTest(case=case):
                browser = EmptyReportBrowser()
                if case == "history":
                    browser.empty_confirmed = False
                elif case == "notice":
                    browser.stale_notice = True
                elif case == "disabled":
                    browser.page_overrides["export_button_disabled"] = True
                else:
                    browser.wrong_range = True
                with self.assertRaises(ValueError):
                    self.export_income(browser)
                self.assertEqual(browser.clicks, [])

    def test_response_requires_matching_month_visible_empty_table_and_no_loading(self):
        variants = [({"transaction_table_count": 0}, {}), ({"transaction_table_count": 2}, {}),
                    ({"transaction_row_count": 1}, {}), ({"transaction_empty_text": ""}, {}),
                    ({"transaction_body_present": False}, {}), ({}, {"busy": True}),
                    ({}, {"url": income.month_date_bounds("2026-09")["income_url"]}),
                    ({}, {"url": "https://accounts.shopee.co.id/seller/login"}),
                    ({}, {"challenge": True})]
        for page_overrides, state_overrides in variants:
            with self.subTest(page=page_overrides, state=state_overrides):
                browser = EmptyReportBrowser()
                browser.page_overrides, browser.state_overrides = page_overrides, state_overrides
                with self.assertRaises(ValueError):
                    self.export_income(browser)
                self.assertEqual(browser.clicks, ["#export"])

    def test_manual_close_remains_an_error_without_reopening(self):
        browser = EmptyReportBrowser()
        browser.evaluate = lambda *_: (_ for _ in ()).throw(RuntimeError("disconnected: not connected to DevTools"))
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            self.export_income(browser)
        self.assertEqual(browser.navigations, [])
        self.assertEqual(browser.clicks, [])

    def test_gateway_still_downloads_pdfs_after_income_empty_and_checks_empty_month(self):
        for no_invoices in (False, True):
            with self.subTest(no_invoices=no_invoices):
                browser = FakeWebDriverBrowser(self.store)
                if no_invoices:
                    browser.month_rows["October 2026"] = []
                    browser.month_empty.add("October 2026")
                    browser.options[2]["is_empty_month"] = True
                gateway = ShopeeIdInvoiceGateway(browser.profile, browser=browser,
                                                 timeout=.3, download_timeout=.3, poll_interval=.005)
                gateway.preflight([self.store])
                with patch("backend.services.shopee_id_invoice_gateway.export_monthly_income_report", return_value=empty_income()):
                    result = gateway.export(self.store, "2026-10", self.root / str(no_invoices))
                self.assertEqual(result["income_report_count"], 0)
                self.assertEqual(result["file_count"], 0 if no_invoices else 6)
                service._validate_completeness(result["files"], result["row_count"], result["evidence"], "2026-10")
                self.assertEqual(browser.closed_stores, ["synthetic"])

    def test_empty_store_continues_next_store_and_retry_skips_verified_empty(self):
        stores = [self.store, {**self.store, "store_id": "next", "store_name": "测试-印尼002"}]
        manifest = service.create_batch(self.root, "synthetic", "2026-10", stores)
        def next_store(store, month, folder):
            return {"row_count": 0, "files": [income_file(folder, month)], "evidence": {"empty_text": "No receipt found"}}
        gateway = FakeInvoiceGateway("synthetic", {"synthetic": empty_result(), "next": next_store})
        result = service.run_batch(manifest, gateway)
        self.assertEqual(gateway.exported_stores, ["synthetic", "next"])
        self.assertTrue(result["is_complete"], result)
        self.assertEqual((result["empty_month_count"], result["success_count"], result["failed_count"]), (1, 1, 0))
        self.assertEqual(result["completed_count"], 2)
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(result["stores"][0]["status"], "empty_month")
        self.assertIn("当月无账单", result["completion_message"])
        gateway.exported_stores.clear()
        self.assertTrue(service.run_batch(manifest, gateway)["is_complete"])
        self.assertEqual(gateway.exported_stores, [])

    def test_incomplete_or_wrong_month_empty_evidence_never_completes(self):
        evidence = empty_result()["evidence"]
        for name, value in (("month", "2026-09"), ("date_range", "2026101620261031"),
                            ("history_checked", False), ("reason", "timeout"),
                            ("export_notice", "No Data"), ("transaction_row_count", 1),
                            ("transaction_empty_text", "")):
            invalid = copy.deepcopy(evidence)
            invalid["income_no_data"][name] = value
            with self.subTest(name=name), self.assertRaises(ValueError):
                service._validate_completeness([], 0, invalid, "2026-10")
        for invalid in ({"income_no_data": evidence["income_no_data"]},
                        {**evidence, "selected_month": "November 2026"}, {"empty_text": "No receipt found"}):
            with self.assertRaises(ValueError):
                service._validate_completeness([], 0, invalid, "2026-10")

    def test_v3_success_preserved_and_invalid_empty_evidence_rechecked(self):
        manifest = service.create_batch(self.root, "synthetic", "2026-10", [self.store])
        gateway = FakeInvoiceGateway("synthetic", {"synthetic": empty_result()})
        service.run_batch(manifest, gateway)
        old = json.loads(manifest.read_text('utf-8'))
        old["schema"] = service.V3_SCHEMA
        old["stores"][0]["evidence"]["income_no_data"]["month"] = "2026-09"
        manifest.write_text(json.dumps(old), encoding='utf-8')
        self.assertFalse(service.public_result(manifest)["is_complete"])
        gateway.exported_stores.clear()
        self.assertTrue(service.run_batch(manifest, gateway)["is_complete"])
        self.assertEqual(gateway.exported_stores, ["synthetic"])
        self.assertEqual(json.loads(manifest.with_name("manifest.v3.json").read_text('utf-8')), old)


class EmptyIncomeDomTests(unittest.TestCase):
    def test_scoped_transaction_table_and_visible_export_notice(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page()
            page.set_content('''<div class="grid-table"><div class="grid-table-empty">No Data</div></div>
                <div class="transaction-table"><div class="grid-table-body">
                  <div class="grid-table-empty">ps_no_data</div></div></div>
                <div class="eds-toast"><div class="eds-toast__content">No data is available for download.</div></div>
                <div class="eds-toast" style="display:none"><div class="eds-toast__content">Stale</div></div>''')
            state = json.loads(page.evaluate(income.INCOME_PAGE_STATE))["income_page"]
            self.assertEqual(state["transaction_table_count"], 1)
            self.assertTrue(state["transaction_body_present"])
            self.assertEqual(state["transaction_row_count"], 0)
            self.assertEqual(state["transaction_empty_text"], "ps_no_data")
            self.assertEqual(state["export_notices"], [income.NO_DOWNLOAD_DATA_NOTICE])
            page.locator('.transaction-table .grid-table-body').evaluate("e => e.innerHTML='<div class=grid-table-row style=display:contents><span>Order 1</span></div>'")
            state = json.loads(page.evaluate(income.INCOME_PAGE_STATE))["income_page"]
            self.assertEqual(state["transaction_row_count"], 1)
            self.assertEqual(state["transaction_empty_text"], "")
            browser.close()
