from __future__ import annotations

import copy
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from backend.services import shopee_id_income_report as income
from tests.shopee_id_fixtures import income_file


def history_row(month="2026-10", status="Download", index=1):
    return {
        "row_index": index, "cell_count": 5, "report_type": "released",
        "request_time": "2026-11-01 00:00:00", "report_name": income.month_date_bounds(month)["expected_filename"],
        "action_text": status, "has_download_button": status == "Download",
        "download_text": status, "download_disabled": status != "Download",
        "download_selector": f"#download-{index}",
    }


class ReportBrowser:
    def __init__(self, pages=None):
        self.pages = copy.deepcopy(pages if pages is not None else [[history_row()]])
        self.page = 1
        self.url = income.MY_REPORTS_INCOME_URL
        self.clicks = []
        self.navigations = []
        self.download_dir = None
        self.refreshes = 0
        self.ready_on_refresh = 1
        self.generated = False
        self.stale_page = False
        self.empty_confirmed = True
        self.wrong_range = False
        self.driver = Mock()
        self.driver.get.side_effect = self.navigate
        self.driver.execute_cdp_cmd.side_effect = self.cdp

    def _driver(self, store_id):
        return self.driver

    def navigate(self, url):
        self.navigations.append(url)
        self.url, self.page = url, 1
        if url == income.MY_REPORTS_INCOME_URL:
            self.refreshes += 1
            if self.refreshes >= self.ready_on_refresh:
                for page in self.pages:
                    for row in page:
                        if row["report_name"] == history_row()["report_name"]:
                            row.update(history_row(index=row["row_index"]))

    def cdp(self, command, options):
        self.download_dir = Path(options["downloadPath"]) if options["behavior"] == "allow" else None

    def click(self, store_id, selector):
        self.clicks.append(selector)
        if selector == "#next":
            if not self.stale_page:
                self.page += 1
        elif selector == "#export":
            self.generated = True
            self.pages = [[history_row(status="Processing")]]
        elif selector.startswith("#download-"):
            income_file(self.download_dir)
        else:
            raise AssertionError(selector)

    def evaluate(self, store_id, script):
        assert script == income.INCOME_PAGE_STATE
        return {
            "url": self.url, "busy": False, "challenge": False, "login_page": False,
            "password_dialog": {"password_visible": False},
            "history_grid": {
                "grid_count": 1, "header_columns": list(income.EXPECTED_HISTORY_HEADERS),
                "body_present": True, "rows": copy.deepcopy(self.pages[self.page - 1]),
                "empty_state_confirmed": self.empty_confirmed,
                "pagination": {"active_page_count": 1, "active_page_text": str(self.page),
                               "next_count": 1, "next_disabled": self.page == len(self.pages),
                               "next_selector": "#next"},
            },
            "income_page": {
                "active_tab_count": 1, "active_tab_text": "Released", "date_range_count": 1,
                "date_range_text": "16/10/2026 - 31/10/2026" if self.wrong_range else "01/10/2026 - 31/10/2026",
                "export_button_count": 1, "export_button_text": "Export",
                "export_button_disabled": False, "export_button_selector": "#export",
                "exporter_container": {"items": [{"name": history_row()["report_name"]}] if self.generated else []},
            },
        }


class IncomeReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def run_export(self, browser, **kwargs):
        return income.export_monthly_income_report(
            browser, "synthetic", "2026-10", self.root / "download", timeout=.12,
            generation_timeout=.15, download_timeout=.12, poll_interval=.01,
            refresh_interval=.01, **kwargs,
        )

    def test_calendar_and_exact_filename(self):
        self.assertEqual(income.month_date_bounds("2024-02")["end_iso"], "2024-02-29")
        self.assertEqual(income.month_date_bounds("2025-02")["end_iso"], "2025-02-28")
        self.assertEqual(income.month_date_bounds("2025-12")["date_range_param"], "2025120120251231")
        self.assertTrue(income.is_matching_monthly_income_filename(history_row()["report_name"], "2026-10"))
        for name in ["Income.pending.id.20261001_20261031.xlsx", "Income.released.my.20261001_20261031.xlsx",
                     "Income.released.id.20261016_20261031.xlsx", "Income.released.id.20261001_20261231.xlsx",
                     "Income.released.id.20260101_20261231.xlsx", "Income.released.id.20261001_20261031 (1).xlsx"]:
            self.assertFalse(income.is_matching_monthly_income_filename(name, "2026-10"), name)

    def test_page_navigation_retries_only_transient_connection_failure(self):
        browser = ReportBrowser()
        browser.driver.get.side_effect = [RuntimeError("net::ERR_CONNECTION_CLOSED"), None]
        with patch.object(income.time, "sleep"):
            income._navigate_url(browser, "synthetic", income.MY_REPORTS_INCOME_URL)
        self.assertEqual(browser.driver.get.call_count, 2)
        self.assertEqual(browser.clicks, [])
        browser.driver.get.reset_mock(side_effect=True)
        browser.driver.get.side_effect = RuntimeError("access denied")
        with self.assertRaisesRegex(RuntimeError, "access denied"):
            income._navigate_url(browser, "synthetic", income.MY_REPORTS_INCOME_URL)
        self.assertEqual(browser.driver.get.call_count, 1)

    def test_second_history_page_download_without_export(self):
        browser = ReportBrowser([[history_row("2026-09")], [history_row()]])
        result = self.run_export(browser)
        self.assertEqual(result["source"], "history")
        self.assertEqual(result["history_page"], 2)
        self.assertEqual(browser.clicks, ["#next", "#download-1"])
        self.assertIsNone(browser.download_dir)

    def test_processing_history_refreshes_until_ready_without_export(self):
        browser = ReportBrowser([[history_row(status="Processing")]])
        browser.ready_on_refresh = 2
        result = self.run_export(browser)
        self.assertEqual(result["source"], "history")
        self.assertEqual(browser.refreshes, 2)
        self.assertEqual(browser.clicks, ["#download-1"])

    def test_missing_after_all_pages_exports_once_and_downloads(self):
        browser = ReportBrowser([[history_row("2026-09")], [history_row("2026-08")]])
        result = self.run_export(browser)
        self.assertEqual(result["source"], "generated")
        self.assertEqual(browser.clicks, ["#next", "#export", "#download-1"])
        self.assertIn(income.month_date_bounds("2026-10")["income_url"], browser.navigations)

    def test_generation_timeout_never_resubmits(self):
        browser = ReportBrowser([[]])
        browser.ready_on_refresh = 1000
        with self.assertRaisesRegex(ValueError, "未重复点击 Export"):
            self.run_export(browser)
        self.assertEqual(browser.clicks, ["#export"])

    def test_stale_paging_transient_empty_and_wrong_range_fail_without_export(self):
        for kind in ("stale", "empty", "range", "limit"):
            with self.subTest(kind=kind):
                browser = ReportBrowser([[history_row("2026-09")], [history_row("2026-08")]])
                if kind == "stale":
                    browser.stale_page = True
                elif kind == "empty":
                    browser.pages, browser.empty_confirmed = [[]], False
                elif kind == "range":
                    browser.wrong_range = True
                with self.assertRaises(ValueError):
                    self.run_export(browser, max_history_pages=1 if kind == "limit" else 10)
                self.assertNotIn("#export", browser.clicks)

    def test_zip_period_currency_and_bad_dimensions(self):
        info = income_file(self.root / "good")
        path = Path(info["path"])
        with zipfile.ZipFile(path) as original:
            members = {name: original.read(name) for name in original.namelist()}
        members["xl/worksheets/sheet4.xml"] = re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1:A1"', members["xl/worksheets/sheet4.xml"])
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as edited:
            for name, content in members.items():
                edited.writestr(name, content)
        self.assertEqual(income.validate_income_xlsx(path, "2026-10")["currency"], "Rp")
        for label, content in (("html", b"<!doctype html><html>login</html>"), ("truncated", path.read_bytes()[:-70])):
            candidate = self.root / label / path.name
            candidate.parent.mkdir()
            candidate.write_bytes(content)
            with self.assertRaises(ValueError):
                income.validate_income_xlsx(candidate, "2026-10")
        for changed in ({"B7": "2026-10-16"}, {"D10": "MYR"}):
            bad = income_file(self.root / str(len(changed)) / next(iter(changed)), **changed)
            with self.assertRaises(ValueError):
                income.validate_income_xlsx(bad["path"], "2026-10")

    def test_large_workbook_numbered_sheets_and_missing_partition(self):
        from openpyxl import load_workbook
        info = income_file(self.root)
        path = Path(info["path"])
        wb = load_workbook(path)
        for base in ("Income", "Seller Fee"):
            ws = wb[base]
            ws.title = base + " - 1"
            wb.copy_worksheet(ws).title = base + " - 2"
        wb.save(path)
        self.assertEqual(income.validate_income_xlsx(path, "2026-10")["month"], "2026-10")
        wb["Income - 2"].title = "Income - 3"
        wb.save(path)
        wb.close()
        with self.assertRaisesRegex(ValueError, "不连续"):
            income.validate_income_xlsx(path, "2026-10")

    def test_pending_spinner_is_not_page_loading_and_selector_is_button(self):
        from playwright.sync_api import sync_playwright
        edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=str(edge), headless=True)
            try:
                page = browser.new_page()
                page.set_content('''<style>.grid-table-row {display:contents}</style><div class="grid-table">
                  <div class="grid-table-header"><div class="grid-table-row"><div>Report Type</div><div>Request Time</div><div>Request Account</div><div>Report name</div><div>Action</div></div></div>
                  <div class="grid-table-body"><div class="grid-table-grid">
                    <div class="grid-table-row"><div>released</div><div>time</div><div>synthetic</div><div>Income.released.id.20261001_20261031.xlsx</div><div><div class="operation"><div class="processing"><span class="eds-spin">◌</span>Processing</div></div></div></div>
                    <div class="grid-table-row"><div>released</div><div>time</div><div>synthetic</div><div>Income.released.id.20260901_20260930.xlsx</div><div><div class="operation"><div class="download"><button>Download</button></div></div></div></div>
                  </div></div></div><span class="eds-pager__page active">1</span><button class="eds-pager__button-next" disabled>Next</button>''')
                state = json.loads(page.evaluate(income.INCOME_PAGE_STATE))
                self.assertFalse(state["busy"])
                rows = state["history_grid"]["rows"]
                self.assertEqual(len(rows), 2)
                self.assertTrue(income._validate_history_headers(state["history_grid"]["header_columns"]))
                self.assertEqual(income._classify_history_row_status(rows[0]), "processing")
                selector = rows[1]["download_selector"]
                self.assertEqual(page.locator(selector).count(), 1)
                self.assertEqual(page.locator(selector).evaluate("e => e.tagName"), "BUTTON")
                page.evaluate("document.body.insertAdjacentHTML('beforeend','<div class=eds-spin>loading</div>')")
                self.assertTrue(json.loads(page.evaluate(income.INCOME_PAGE_STATE))["busy"])
            finally:
                browser.close()
