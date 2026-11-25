from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from tests.shopee_id_fixtures import income_file
from backend.services.shopee_id_income_report import validate_income_xlsx

from backend.services.shopee_id_invoice_gateway import (
    INVOICE_URL,
    InvoiceExportError,
    PAGE_STATE,
    ShopeeIdInvoiceGateway,
    download_pdf,
    extract_months_from_text,
    parse_month_label,
    validate_pdf,
)

try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
except ImportError:
    sync_playwright = None


def valid_pdf_bytes(title: str = "Invoice") -> bytes:
    return (
        b"%PDF-1.7\n"
        + f"1 0 obj << /Title ({title}) >> endobj\n".encode("ascii")
        + b"trailer << /Root 1 0 R >>\n%%EOF\n"
    )


def make_row_state(
    row_index: int,
    description: str,
    date_text: str = "October 2026",
    *,
    include_master: bool = True,
    include_tax: bool = True,
    include_itemised: bool = True,
) -> dict:
    actions = []
    idx = 1
    if include_master:
        actions.append({
            "action_index": idx,
            "text": "Master Invoice",
            "disabled": False,
            "selector": f"tr.eds-table__row:nth-child({row_index}) .download-row > .eds-popover:nth-child({idx}) button",
        })
        idx += 1
    if include_tax:
        actions.append({
            "action_index": idx,
            "text": "Tax Invoice",
            "disabled": False,
            "selector": f"tr.eds-table__row:nth-child({row_index}) .download-row > .eds-popover:nth-child({idx}) button",
        })
        idx += 1
    if include_itemised:
        actions.append({
            "action_index": idx,
            "text": "Itemised Billing",
            "disabled": False,
            "selector": f"tr.eds-table__row:nth-child({row_index}) .download-row > .eds-popover:nth-child({idx}) button",
        })
    return {
        "row_index": row_index,
        "cell_count": 4,
        "description": description,
        "amount": "Rp 1.250.000",
        "date": date_text,
        "download_row_present": True,
        "actions": actions,
    }


class FakeWebDriverBrowser:
    """Simulates ZiniaoBrowser + CDP download behavior and live page state transitions."""

    def __init__(
        self,
        store: dict,
        *,
        initial_selected_month: str = "November 2026",
        options: list[dict] | None = None,
        month_rows: dict[str, list[dict]] | None = None,
        month_empty: set[str] | None = None,
        password_visible: bool = False,
        password_filled: bool = True,
        verify_clicks_needed: int = 1,
        challenge: bool = False,
        pagination_count: int = 0,
        table_count: int = 1,
        stale_polls_after_month_select: int = 0,
        download_generator=None,
    ) -> None:
        self.profile = "ziniao-account:fixture"
        self.auth_failed = False
        self.cleanup_failed = False
        self.store = dict(store)
        self.selected_month_text = initial_selected_month
        self.popper_open = False
        self.options = options if options is not None else [
            {"index": 1, "text": "November 2026", "is_empty_month": True, "disabled": False, "selector": ".eds-select__options > .eds-option:nth-child(1)"},
            {"index": 2, "text": "September 2026", "is_empty_month": False, "disabled": False, "selector": ".eds-select__options > .eds-option:nth-child(2)"},
            {"index": 3, "text": "October 2026", "is_empty_month": False, "disabled": False, "selector": ".eds-select__options > .eds-option:nth-child(3)"},
        ]
        self.month_rows = month_rows if month_rows is not None else {
            "November 2026": [],
            "October 2026": [
                make_row_state(1, "Affiliate M.S. - October 2026", "October 2026"),
                make_row_state(2, "Shopee invoices - October 2026", "October 2026"),
                make_row_state(3, "SVS invoices - October 2026", "October 2026"),
            ],
        }
        self.month_empty = month_empty if month_empty is not None else {"November 2026"}
        self.password_visible = password_visible
        self.password_filled = password_filled
        self.verify_clicks_needed = verify_clicks_needed
        self.verify_clicks = 0
        self.challenge = challenge
        self.pagination_count = pagination_count
        self.table_count = table_count
        self.stale_polls_remaining = 0
        self.stale_polls_after_month_select = stale_polls_after_month_select
        self.previous_rows: list[dict] = []
        self.clicks: list[str] = []
        self.opened_urls: list[str] = []
        self.closed_stores: list[str] = []
        self.active_download_dir: Path | None = None
        self.cdp_calls: list[tuple[str, dict]] = []
        self.driver = Mock()
        self.driver.execute_cdp_cmd.side_effect = self._execute_cdp_cmd
        self.download_generator = download_generator or self._default_download_generator
        self.download_call_count = 0

    def _execute_cdp_cmd(self, cmd: str, params: dict) -> dict:
        self.cdp_calls.append((cmd, dict(params)))
        if params.get("behavior") == "allow":
            self.active_download_dir = Path(params["downloadPath"])
        elif params.get("behavior") == "default":
            self.active_download_dir = None
        return {}

    def _default_download_generator(self, selector: str, download_dir: Path, call_index: int) -> None:
        filename = f"Shopee_ID_Invoice_{call_index:02d} (2026-10) #A.pdf"
        (download_dir / filename).write_bytes(valid_pdf_bytes(f"{selector}-{call_index}"))

    def ready(self) -> None:
        return None

    def list_stores(self) -> list[dict]:
        return [dict(self.store)]

    def open_store(self, store: dict, *, url: str = "") -> None:
        self.opened_urls.append(url)

    def close_store(self, store_id: str) -> None:
        self.closed_stores.append(store_id)

    def _driver(self, _store_id: str):
        return self.driver

    def evaluate(self, _store_id: str, _script: str) -> dict:
        if self.stale_polls_remaining > 0:
            self.stale_polls_remaining -= 1
            rows = copy.deepcopy(self.previous_rows)
            no_receipt = False
        else:
            rows = copy.deepcopy(self.month_rows.get(self.selected_month_text, []))
            no_receipt = self.selected_month_text in self.month_empty
        return {
            "url": INVOICE_URL,
            "title": "My Tax Invoices",
            "login_page": False,
            "challenge": self.challenge,
            "busy": False,
            "more_button_selector": "",
            "password_dialog": {
                "password_visible": self.password_visible,
                "password_count": 1 if self.password_visible else 0,
                "password_filled": self.password_filled if self.password_visible else False,
                "verify_count": 1 if self.password_visible else 0,
                "verify_disabled": False,
                "verify_selector": "button.eds-button--primary.action" if self.password_visible else "",
            },
            "month_selector": {
                "count": 1,
                "selector": ".eds-select .eds-selector",
                "selected_text": self.selected_month_text,
            },
            "month_options": {
                "popper_count": 1 if self.popper_open else 0,
                "options": copy.deepcopy(self.options) if self.popper_open else [],
            },
            "table_count": self.table_count,
            "pagination_count": self.pagination_count,
            "no_receipt_found": no_receipt,
            "rows": rows,
        }

    def click(self, _store_id: str, selector: str) -> None:
        self.clicks.append(selector)
        if selector == "button.eds-button--primary.action":
            self.verify_clicks += 1
            if self.verify_clicks >= self.verify_clicks_needed:
                self.password_visible = False
            return
        if selector == ".eds-select .eds-selector":
            self.popper_open = True
            return
        for option in self.options:
            if option["selector"] == selector:
                self.previous_rows = copy.deepcopy(
                    self.month_rows.get(self.selected_month_text, [])
                )
                self.selected_month_text = option["text"]
                self.popper_open = False
                self.stale_polls_remaining = self.stale_polls_after_month_select
                return
        if ".download-row" in selector:
            if "nth-child(3)" in selector.split(".download-row")[1]:
                raise AssertionError("Itemised Billing must NEVER be clicked")
            self.download_call_count += 1
            if self.active_download_dir is None:
                raise AssertionError("CDP downloadPath must be configured before clicking download")
            self.download_generator(selector, self.active_download_dir, self.download_call_count)


class ShopeeIdInvoiceGatewayTests(unittest.TestCase):
    def test_old_empty_placeholder_waits_for_nonempty_month(self) -> None:
        class SlowMonthBrowser(FakeWebDriverBrowser):
            stale_empty = 4

            def evaluate(self, store_id, script):
                state = super().evaluate(store_id, script)
                if self.selected_month_text == "October 2026" and not self.popper_open and self.stale_empty:
                    self.stale_empty -= 1
                    state.update(rows=[], no_receipt_found=True)
                return state

        browser = SlowMonthBrowser(self.store)
        gateway = ShopeeIdInvoiceGateway(browser.profile, browser=browser, timeout=.3, download_timeout=.3, poll_interval=.01)
        gateway.preflight([self.store])
        result = gateway.export(self.store, "2026-10", self.root / "slow")
        self.assertFalse(result["no_data"])
        self.assertEqual(result["file_count"], 7)

    def test_close_failure_keeps_completed_files_in_error(self) -> None:
        browser = FakeWebDriverBrowser(self.store)
        def failed_close(_):
            browser.cleanup_failed = True
            raise ValueError("关闭超时")
        browser.close_store = failed_close
        gateway = ShopeeIdInvoiceGateway(browser.profile, browser=browser, timeout=.3, download_timeout=.3, poll_interval=.01)
        gateway.preflight([self.store])
        with self.assertRaises(InvoiceExportError) as failed:
            gateway.export(self.store, "2026-10", self.root / "close-failure")
        self.assertEqual(len(failed.exception.partial_files), 7)
        self.assertTrue(all(Path(f["path"]).is_file() for f in failed.exception.partial_files))

    def test_default_archive_with_long_original_filename_fits_windows_limit(self) -> None:
        from backend.services import shopee_id_monthly_invoice as service
        browser = FakeWebDriverBrowser(self.store)
        name = "1234567890123456789 - Master Invoice - SAMPLE202610310000000000000000000 .pdf"
        browser.download_generator = lambda selector, folder, index: (folder / name).write_bytes(valid_pdf_bytes())
        gateway = ShopeeIdInvoiceGateway(browser.profile, browser=browser, timeout=.3, download_timeout=.3, poll_interval=.01)
        path = service.create_batch(self.root / "Shopee印尼月度账单", browser.profile, "2026-10", [self.store])
        result = service.run_batch(path, gateway)
        self.assertTrue(result["is_complete"], result["completion_message"])
        self.assertTrue(all(len(p) < 260 for p in result["output_files"]))

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Isolate the already-covered PDF DOM behavior from income history navigation.
        # The real income helper has its own DOM/download tests; combined output is checked here.
        income_patch = patch("backend.services.shopee_id_invoice_gateway.export_monthly_income_report",
                             side_effect=lambda browser, store_id, month, directory, **kw: income_file(directory, month))
        income_patch.start()
        self.addCleanup(income_patch.stop)
        self.store = {
            "store_id": "101",
            "store_name": "运营甲-SP印尼046-本土",
            "platform_name": "Shopee",
        }

    def test_month_label_parser_supports_english_and_indonesian_locales(self) -> None:
        cases = {
            "October 2026": "2026-10",
            "Oktober 2026": "2026-10",
            "November 2026": "2026-11",
            "Nopember 2026": "2026-11",
            "Agustus 2026": "2026-08",
            "August 2026": "2026-08",
            "Mei 2026": "2026-05",
            "Desember 2025": "2025-12",
            "Januari 2026": "2026-01",
            "Februari 2026": "2026-02",
            "Maret 2026": "2026-03",
            "Juni 2026": "2026-06",
            "Juli 2026": "2026-07",
            "2026-10": "2026-10",
        }
        for label, expected in cases.items():
            with self.subTest(label=label):
                self.assertEqual(parse_month_label(label), expected)
        self.assertEqual(
            extract_months_from_text("Affiliate M.S. (01 Okt 2026 - 31 Okt 2026)"),
            {"2026-10"},
        )

    def test_october_three_rows_download_six_pdfs_and_never_click_itemised_billing(self) -> None:
        browser = FakeWebDriverBrowser(
            self.store,
            password_visible=True,
            password_filled=True,
            stale_polls_after_month_select=1,
        )
        # Ensure initial month has stale November rows to test anti-stale-row transition guard.
        browser.month_rows["November 2026"] = [
            make_row_state(1, "Shopee invoices - November 2026", "November 2026")
        ]
        browser.month_empty.discard("November 2026")

        gateway = ShopeeIdInvoiceGateway(
            browser.profile,
            browser=browser,
            timeout=2.0,
            download_timeout=2.0,
            poll_interval=0.01,
        )
        gateway.preflight([self.store])
        result = gateway.export(self.store, "2026-10", self.root / "store_101")

        self.assertFalse(result["no_data"])
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(result["file_count"], 7)
        self.assertEqual(result["master_invoice_count"], 3)
        self.assertEqual(result["tax_invoice_count"], 3)
        self.assertEqual(browser.verify_clicks, 1)
        self.assertEqual(browser.closed_stores, ["101"])
        # Verify option 3 ("October 2026") was clicked, not hardcoded index 2.
        self.assertIn(".eds-select__options > .eds-option:nth-child(3)", browser.clicks)
        # Verify all 6 clicks targeted nth-child(1) (Master) and nth-child(2) (Tax), never nth-child(3).
        download_clicks = [c for c in browser.clicks if ".download-row" in c]
        self.assertEqual(len(download_clicks), 6)
        for row_idx in (1, 2, 3):
            self.assertIn(
                f"tr.eds-table__row:nth-child({row_idx}) .download-row > .eds-popover:nth-child(1) button",
                download_clicks,
            )
            self.assertIn(
                f"tr.eds-table__row:nth-child({row_idx}) .download-row > .eds-popover:nth-child(2) button",
                download_clicks,
            )
            self.assertNotIn(
                f"tr.eds-table__row:nth-child({row_idx}) .download-row > .eds-popover:nth-child(3) button",
                download_clicks,
            )
        for file_entry in result["files"]:
            verified = (validate_income_xlsx(file_entry["path"], "2026-10") if file_entry["invoice_type"] == "income_report"
                        else validate_pdf(file_entry["path"]))
            self.assertEqual(verified["sha256"], file_entry["sha256"])

    def test_indonesian_month_option_and_explicit_no_receipt_found_return_no_data(self) -> None:
        browser = FakeWebDriverBrowser(
            self.store,
            options=[
                {"index": 1, "text": "November 2026", "is_empty_month": True, "disabled": False, "selector": ".eds-select__options > .eds-option:nth-child(1)"},
                {"index": 2, "text": "Oktober 2026", "is_empty_month": True, "disabled": False, "selector": ".eds-select__options > .eds-option:nth-child(2)"},
            ],
            month_rows={"November 2026": [], "Oktober 2026": []},
            month_empty={"November 2026", "Oktober 2026"},
        )
        gateway = ShopeeIdInvoiceGateway(
            browser.profile, browser=browser, timeout=1.0, poll_interval=0.01
        )
        gateway.preflight([self.store])
        result = gateway.export(self.store, "2026-10", self.root / "empty_store")

        self.assertTrue(result["no_data"])
        self.assertEqual(result["row_count"], 0)
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(result["evidence"]["empty_text"], "No receipt found")
        self.assertEqual(browser.download_call_count, 0)

    def test_empty_rows_without_no_receipt_found_text_fails_closed(self) -> None:
        browser = FakeWebDriverBrowser(
            self.store,
            month_rows={"November 2026": [], "October 2026": []},
            month_empty=set(),  # No rows AND no "No receipt found"
        )
        gateway = ShopeeIdInvoiceGateway(
            browser.profile, browser=browser, timeout=0.1, poll_interval=0.01
        )
        gateway.preflight([self.store])
        with self.assertRaisesRegex(ValueError, "No receipt found"):
            gateway.export(self.store, "2026-10", self.root / "unconfirmed_empty")

    def test_absent_month_option_fails_as_unavailable_not_no_data(self) -> None:
        browser = FakeWebDriverBrowser(self.store)
        gateway = ShopeeIdInvoiceGateway(
            browser.profile, browser=browser, timeout=0.5, poll_interval=0.01
        )
        gateway.preflight([self.store])
        with self.assertRaisesRegex(ValueError, "未提供 2026-08"):
            gateway.export(self.store, "2026-08", self.root / "absent_month")

    def test_stale_mismatched_row_month_is_rejected(self) -> None:
        browser = FakeWebDriverBrowser(
            self.store,
            month_rows={
                "October 2026": [
                    make_row_state(1, "Shopee invoices - September 2026", "September 2026")
                ]
            },
            month_empty=set(),
        )
        gateway = ShopeeIdInvoiceGateway(
            browser.profile, browser=browser, timeout=0.1, poll_interval=0.01
        )
        gateway.preflight([self.store])
        with self.assertRaisesRegex(ValueError, "2026-09.*与所选 2026-10 不一致"):
            gateway.export(self.store, "2026-10", self.root / "stale_month")

    def test_unfilled_password_and_captcha_challenge_fail_without_clicking_verify(self) -> None:
        unfilled_browser = FakeWebDriverBrowser(
            self.store, password_visible=True, password_filled=False
        )
        gateway = ShopeeIdInvoiceGateway(
            unfilled_browser.profile,
            browser=unfilled_browser,
            timeout=0.2,
            autofill_timeout=0.05,
            poll_interval=0.01,
        )
        gateway.preflight([self.store])
        with self.assertRaisesRegex(ValueError, "自动填充密码"):
            gateway.export(self.store, "2026-10", self.root / "unfilled")
        self.assertEqual(unfilled_browser.verify_clicks, 0)

        captcha_browser = FakeWebDriverBrowser(self.store, challenge=True)
        gateway_captcha = ShopeeIdInvoiceGateway(
            captcha_browser.profile,
            browser=captcha_browser,
            timeout=0.2,
            poll_interval=0.01,
        )
        gateway_captcha.preflight([self.store])
        with self.assertRaisesRegex(ValueError, "人工验证或验证码"):
            gateway_captcha.export(self.store, "2026-10", self.root / "captcha")

    def test_pagination_extra_table_or_missing_action_type_fail_closed(self) -> None:
        paginated = FakeWebDriverBrowser(self.store, pagination_count=1)
        gw_page = ShopeeIdInvoiceGateway(
            paginated.profile, browser=paginated, timeout=0.2, poll_interval=0.01
        )
        gw_page.preflight([self.store])
        with self.assertRaisesRegex(ValueError, "分页"):
            gw_page.export(self.store, "2026-10", self.root / "paginated")

        multi_table = FakeWebDriverBrowser(self.store, table_count=2)
        gw_table = ShopeeIdInvoiceGateway(
            multi_table.profile, browser=multi_table, timeout=0.2, poll_interval=0.01
        )
        gw_table.preflight([self.store])
        with self.assertRaisesRegex(ValueError, "多个数据表格"):
            gw_table.export(self.store, "2026-10", self.root / "multi_table")

        missing_tax = FakeWebDriverBrowser(
            self.store,
            month_rows={
                "October 2026": [
                    make_row_state(1, "Shopee invoices - October 2026", include_tax=False)
                ]
            },
            month_empty=set(),
        )
        gw_missing = ShopeeIdInvoiceGateway(
            missing_tax.profile, browser=missing_tax, timeout=0.2, poll_interval=0.01
        )
        gw_missing.preflight([self.store])
        with self.assertRaisesRegex(ValueError, "Tax Invoice"):
            gw_missing.export(self.store, "2026-10", self.root / "missing_tax")

    def test_corrupt_or_wrong_pdf_and_partial_failure_preserve_completed_pdfs(self) -> None:
        def fail_on_fourth_download(selector: str, download_dir: Path, call_index: int) -> None:
            if call_index == 4:
                (download_dir / "corrupt.pdf").write_bytes(b"%PDF-1.4\ntruncated without eof")
                return
            (download_dir / f"Invoice_{call_index}.pdf").write_bytes(valid_pdf_bytes(selector))

        browser = FakeWebDriverBrowser(self.store, download_generator=fail_on_fourth_download)
        gateway = ShopeeIdInvoiceGateway(
            browser.profile,
            browser=browser,
            timeout=1.0,
            download_timeout=0.05,
            poll_interval=0.01,
        )
        gateway.preflight([self.store])
        with self.assertRaises(InvoiceExportError) as caught:
            gateway.export(self.store, "2026-10", self.root / "partial_store")
        self.assertIn("%%EOF", str(caught.exception))
        self.assertEqual(len(caught.exception.partial_files), 4)
        for item in caught.exception.partial_files:
            self.assertTrue(Path(item["path"]).is_file())
            if item["invoice_type"] == "income_report":
                validate_income_xlsx(item["path"], "2026-10")
            else:
                validate_pdf(item["path"])
        # Verify CDP download behavior was reset to default in finally.
        self.assertEqual(browser.cdp_calls[-1][1], {"behavior": "default"})

    def test_validate_pdf_and_download_pdf_reject_non_pdf_empty_and_multiple_files(self) -> None:
        bad_header = self.root / "bad_header.pdf"
        bad_header.write_bytes(b"<!DOCTYPE html><html>login</html>%%EOF")
        with self.assertRaisesRegex(ValueError, "%PDF-"):
            validate_pdf(bad_header)

        bad_tail = self.root / "bad_tail.pdf"
        bad_tail.write_bytes(b"%PDF-1.5\n1 0 obj\n")
        with self.assertRaisesRegex(ValueError, "%%EOF"):
            validate_pdf(bad_tail)

        wrong_ext = self.root / "itemised.xlsx"
        wrong_ext.write_bytes(valid_pdf_bytes())
        with self.assertRaisesRegex(ValueError, "PDF"):
            validate_pdf(wrong_ext)

        def generate_two_files(_sel: str, download_dir: Path, _idx: int) -> None:
            (download_dir / "a.pdf").write_bytes(valid_pdf_bytes("a"))
            (download_dir / "b.pdf").write_bytes(valid_pdf_bytes("b"))

        browser = FakeWebDriverBrowser(self.store, download_generator=generate_two_files)
        with self.assertRaisesRegex(ValueError, "多个下载文件"):
            download_pdf(
                browser,
                "101",
                "tr.eds-table__row:nth-child(1) .download-row > .eds-popover:nth-child(1) button",
                self.root / "multi_files",
                timeout=0.05,
                poll_interval=0.01,
            )

    def test_synthetic_dom_page_state_script_in_browser(self) -> None:
        if sync_playwright is None:
            self.skipTest("Playwright is optional for synthetic DOM evaluation")
        runtime = sync_playwright().start()
        self.addCleanup(runtime.stop)
        try:
            browser = runtime.chromium.launch(headless=True, channel="msedge")
        except PlaywrightError:
            try:
                browser = runtime.chromium.launch(headless=True)
            except PlaywrightError as exc:
                self.skipTest(f"No browser available: {exc}")
        self.addCleanup(browser.close)
        page = browser.new_page()
        self.addCleanup(page.close)

        page.set_content("""
        <div role="dialog" class="eds-modal">
          <input type="password" value="autofilled-secret-never-read" />
          <button class="eds-button eds-button--primary action">Verify</button>
        </div>
        <div class="eds-select">
          <div class="eds-selector"><div class="eds-selector__inner">October 2026</div></div>
        </div>
        <div class="eds-popper">
          <div class="eds-select__options">
            <div class="eds-option is-empty-month">November 2026</div>
            <div class="eds-option">October 2026</div>
            <div class="eds-option">September 2026</div>
          </div>
        </div>
        <div class="eds-table">
          <table>
            <tbody>
              <tr class="eds-table__row">
                <td>Affiliate M.S. - October 2026</td>
                <td>Rp 350.000</td>
                <td>October 2026</td>
                <td>
                  <div class="download-row">
                    <div class="eds-popover"><div class="eds-popover__ref"><button>Master Invoice</button></div></div>
                    <div class="eds-popover"><div class="eds-popover__ref"><button>Tax Invoice</button></div></div>
                    <div class="eds-popover"><div class="eds-popover__ref"><button>Itemised Billing</button></div></div>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        """)
        raw = page.evaluate(PAGE_STATE)
        state = json.loads(raw)
        self.assertNotIn("autofilled-secret-never-read", raw)
        self.assertTrue(state["password_dialog"]["password_visible"])
        self.assertTrue(state["password_dialog"]["password_filled"])
        self.assertEqual(state["password_dialog"]["verify_selector"], "button.eds-button--primary.action")
        self.assertEqual(state["month_selector"]["selected_text"], "October 2026")
        self.assertEqual(len(state["month_options"]["options"]), 3)
        self.assertTrue(state["month_options"]["options"][0]["is_empty_month"])
        self.assertEqual(len(state["rows"]), 1)
        actions = state["rows"][0]["actions"]
        self.assertEqual([a["text"] for a in actions], ["Master Invoice", "Tax Invoice", "Itemised Billing"])
        self.assertEqual(
            actions[0]["selector"],
            "tr.eds-table__row:nth-child(1) .download-row > .eds-popover:nth-child(1) button",
        )
        self.assertEqual(
            actions[1]["selector"],
            "tr.eds-table__row:nth-child(1) .download-row > .eds-popover:nth-child(2) button",
        )


if __name__ == "__main__":
    unittest.main()
