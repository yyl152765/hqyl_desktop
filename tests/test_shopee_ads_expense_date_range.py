from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPERBROWSER_ROOT = PROJECT_ROOT.parent / "superbrowser_process"
_INSERTED_SUPERBROWSER_PATH = False
if str(SUPERBROWSER_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERBROWSER_ROOT))
    _INSERTED_SUPERBROWSER_PATH = True

from implement.shopee.ads_expense_date_range import (
    collect_previous_7_days_expense_snapshot,
    expense_amount_is_loaded,
    expense_range_t8_to_t1,
    parse_expense_amount_text,
    parse_selected_date_range_text,
    previous_7_days_without_today,
    read_expense_amount,
    read_selected_date_range,
    select_ads_expense_date_range,
    set_ads_expense_date_range,
    wait_for_expense_amount_change,
    wait_for_selected_date_range,
)
from implement.shopee import ads_expense_date_range as expense_range_module

if _INSERTED_SUPERBROWSER_PATH:
    sys.path.remove(str(SUPERBROWSER_ROOT))


class ShopeeAdsExpenseDateRangeTests(unittest.TestCase):
    def test_expense_range_uses_t8_to_t1(self):
        self.assertEqual(
            expense_range_t8_to_t1(date(2026, 6, 25)),
            ("2026-06-17", "2026-06-24"),
        )

    def test_expense_range_handles_month_boundary(self):
        self.assertEqual(
            expense_range_t8_to_t1(date(2026, 7, 2)),
            ("2026-06-24", "2026-07-01"),
        )

    def test_previous_7_days_without_today_uses_7_calendar_days(self):
        self.assertEqual(
            previous_7_days_without_today(date(2026, 6, 25)),
            ("2026-06-18", "2026-06-24"),
        )

    def test_previous_7_days_without_today_handles_month_boundary(self):
        self.assertEqual(
            previous_7_days_without_today(date(2026, 7, 3)),
            ("2026-06-26", "2026-07-02"),
        )

    def test_expense_amount_parse_handles_currency_and_suffixes(self):
        self.assertEqual(parse_expense_amount_text("Rp1.4m"), Decimal("1400000.0"))
        self.assertEqual(parse_expense_amount_text("RM 1,234.56"), Decimal("1234.56"))
        self.assertEqual(parse_expense_amount_text("35.6K"), Decimal("35600.0"))

    def test_selected_date_range_text_parses_live_shopee_format(self):
        self.assertEqual(
            parse_selected_date_range_text(
                "09/07 - 15/07 (GMT+8)",
                reference_date=date(2026, 7, 9),
            ),
            ("2026-07-09", "2026-07-15"),
        )

    def test_selected_date_range_text_handles_year_boundary(self):
        self.assertEqual(
            parse_selected_date_range_text(
                "29/12 - 04/01 (GMT+8)",
                reference_date=date(2026, 12, 29),
            ),
            ("2026-12-29", "2027-01-04"),
        )

    def test_read_selected_date_range_prefers_shopee_url_timestamps(self):
        driver = _FakeDriverWithUrl(
            ["431.67"],
            "https://seller.shopee.com.my/portal/marketing/pas/index"
            "?from=1783526400&to=1784131199&type=new_cpc_homepage&group=custom",
        )

        self.assertEqual(
            read_selected_date_range(driver),
            ("2026-07-09", "2026-07-15"),
        )

    def test_read_selected_date_range_uses_site_timezone(self):
        driver = _FakeDriverWithUrl(
            ["431.67"],
            "https://seller.shopee.co.th/portal/marketing/pas/index"
            "?from=1783530000&to=1784134799",
        )

        self.assertEqual(
            read_selected_date_range(driver),
            ("2026-07-09", "2026-07-15"),
        )

    def test_read_selected_date_range_prefers_visible_text_over_stale_url_timezone(self):
        driver = _FakeDriverWithVisibleRange(
            ["431.67"],
            "https://seller.shopee.co.th/portal/marketing/pas/index"
            "?from=1786550400&to=1787155199",
            "13/08 - 19/08 (GMT+8)",
        )

        self.assertEqual(
            read_selected_date_range(driver, reference_date=date(2026, 8, 13)),
            ("2026-08-13", "2026-08-19"),
        )

    def test_wait_for_selected_date_range_uses_browser_profile_timezone(self):
        driver = _FakeDriverWithBrowserTimezone(
            ["431.67"],
            "https://seller.shopee.co.th/portal/marketing/pas/index"
            "?from=1786550400&to=1787155199",
            timezone_offset_hours=8,
        )

        result = wait_for_selected_date_range(
            driver,
            "2026-08-13",
            "2026-08-19",
            timeout=0,
            poll_interval=0,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["range"], result["target"])
        self.assertEqual(result["timezone_offset_hours"], 8.0)

    def test_expense_amount_loaded_requires_non_zero_changed_value(self):
        before = Decimal("100")
        self.assertFalse(expense_amount_is_loaded(before, Decimal("0")))
        self.assertFalse(expense_amount_is_loaded(before, Decimal("100")))
        self.assertTrue(expense_amount_is_loaded(before, Decimal("120")))

    def test_wait_for_expense_amount_change_skips_zero_and_same_amount(self):
        driver = _FakeDriver(["0", "100", "100", "120"])

        result = wait_for_expense_amount_change(
            driver,
            "//expense",
            before_amount=Decimal("100"),
            timeout=1,
            poll_interval=0,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["amount"], Decimal("120"))

    def test_wait_for_expense_amount_accepts_stable_unchanged_value(self):
        driver = _FakeDriver(["431.67", "431.67"])

        result = wait_for_expense_amount_change(
            driver,
            "//expense",
            before_amount=Decimal("431.67"),
            timeout=1,
            poll_interval=0,
            require_changed=False,
            stable_reads=2,
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["amount"], Decimal("431.67"))

    def test_changed_amount_still_requires_minimum_wait_and_stability(self):
        clock = _FakeClock()
        driver = _FakeDriver(["120", "120", "140", "140"])
        with patch("implement.shopee.ads_expense_date_range.time.monotonic", clock.monotonic), patch(
            "implement.shopee.ads_expense_date_range.time.sleep", clock.sleep
        ):
            result = wait_for_expense_amount_change(
                driver, "//expense", before_amount=Decimal("100"),
                timeout=10, poll_interval=1, require_changed=False,
                stable_reads=2, minimum_wait_seconds=3,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["amount"], Decimal("140"))
        self.assertEqual(clock.now, 3)
        self.assertEqual(result["stable_reads"], 2)

    def test_missing_read_breaks_consecutive_stability(self):
        clock = _FakeClock()
        driver = _FakeDriver(["120", "--", "120", "140", "140"])
        with patch("implement.shopee.ads_expense_date_range.time.monotonic", clock.monotonic), patch(
            "implement.shopee.ads_expense_date_range.time.sleep", clock.sleep
        ):
            result = wait_for_expense_amount_change(
                driver, "//expense", before_amount=Decimal("100"),
                timeout=10, poll_interval=1, stable_reads=2,
            )
        self.assertEqual(result["amount"], Decimal("140"))
        self.assertEqual(clock.now, 4)

    def test_wait_for_expense_amount_accepts_stable_zero_when_allowed(self):
        driver = _FakeDriver(["0"])

        result = wait_for_expense_amount_change(
            driver,
            "//expense",
            before_amount=Decimal("0"),
            timeout=0,
            poll_interval=0,
            require_non_zero=False,
            require_changed=False,
            stable_reads=1,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["amount"], Decimal("0"))

    def test_wait_for_selected_date_range_rejects_uncommitted_range(self):
        driver = _FakeDriverWithUrl(
            ["100"],
            "https://seller.shopee.com.my/portal/marketing/pas/index"
            "?from=1782921600&to=1783526399",
        )

        result = wait_for_selected_date_range(
            driver,
            "2026-07-09",
            "2026-07-15",
            timeout=0,
            poll_interval=0,
        )

        self.assertFalse(result["ok"])
        self.assertNotEqual(result["range"], result["target"])

    def test_wait_for_selected_date_range_requires_picker_to_close(self):
        driver = _FakeDriverWithOpenPicker(
            ["431.67"],
            "https://seller.shopee.com.my/portal/marketing/pas/index"
            "?from=1783526400&to=1784131199",
        )

        result = wait_for_selected_date_range(
            driver,
            "2026-07-09",
            "2026-07-15",
            timeout=0,
            poll_interval=0,
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["popup_open"])

    def test_select_date_range_skips_click_when_target_is_already_selected(self):
        driver = _FakeDriverWithUrl(
            ["431.67", "431.67"],
            "https://seller.shopee.com.my/portal/marketing/pas/index"
            "?from=1783526400&to=1784131199&type=new_cpc_homepage&group=custom",
        )
        webdriver_util = _FakeWebdriverUtil(driver)

        result = select_ads_expense_date_range(
            webdriver_util,
            "魏胜贤-马来080-MY001",
            "//date-selector",
            "2026-07-09",
            "2026-07-15",
            amount_xpath="//expense",
            amount_wait_timeout=0,
        )

        self.assertEqual(result, ("2026-07-09", "2026-07-15"))
        self.assertEqual(webdriver_util.click_count, 0)

    def test_select_date_range_validates_committed_range_before_accepting_amount(self):
        driver = _FakeChangingDateDriver()
        webdriver_util = _FakeWebdriverUtil(driver)

        result = select_ads_expense_date_range(
            webdriver_util,
            "store",
            "//date-selector",
            "2026-07-09",
            "2026-07-15",
            amount_xpath="//expense",
            amount_wait_timeout=1,
        )

        self.assertEqual(result, ("2026-07-09", "2026-07-15"))
        self.assertEqual(webdriver_util.click_count, 1)
        self.assertEqual(
            read_selected_date_range(driver),
            ("2026-07-09", "2026-07-15"),
        )

    def test_select_date_range_retries_when_committed_range_mismatches(self):
        driver = _FakeRetryingDateDriver()
        webdriver_util = _FakeWebdriverUtil(driver)
        verification_results = [
            {
                "ok": False,
                "range": ("2026-07-08", "2026-07-15"),
                "source": "url",
                "text": "",
                "popup_open": False,
            },
            {
                "ok": True,
                "range": ("2026-07-09", "2026-07-15"),
                "source": "url",
                "text": "",
                "popup_open": False,
            },
        ]

        with patch(
            "implement.shopee.ads_expense_date_range.wait_for_selected_date_range",
            side_effect=verification_results,
        ) as verify_range, patch(
            "implement.shopee.ads_expense_date_range.time.sleep"
        ):
            result = select_ads_expense_date_range(
                webdriver_util,
                "store",
                "//date-selector",
                "2026-07-09",
                "2026-07-15",
                wait_seconds=0,
            )

        self.assertEqual(result, ("2026-07-09", "2026-07-15"))
        self.assertEqual(driver.selection_count, 2)
        self.assertEqual(webdriver_util.click_count, 2)
        self.assertEqual(verify_range.call_count, 2)

    def test_set_range_navigates_before_shortcut_for_cross_month_target(self):
        driver = _FakeCrossMonthCalendarDriver()

        with patch("implement.shopee.ads_expense_date_range.time.sleep"):
            result = set_ads_expense_date_range(
                driver,
                "2026-07-28",
                "2026-08-03",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "visible-calendar-cells")
        self.assertEqual(driver.call_count, 2)

    def test_shortcut_selector_requires_an_exact_label(self):
        driver = _FakeScriptCaptureDriver()

        set_ads_expense_date_range(
            driver,
            "2026-07-28",
            "2026-08-03",
        )

        self.assertIn(
            "/^(过去7天|过去七天|最近7天|最近七天)$/",
            driver.script,
        )

    def test_set_range_script_uses_async_apply_wait_without_blocking_dom(self):
        driver = _FakeScriptCaptureDriver()

        set_ads_expense_date_range(
            driver,
            "2026-07-28",
            "2026-08-03",
        )

        self.assertNotIn("while (Date.now() < deadline)", driver.script)
        self.assertIn("function visiblePickerRoots()", driver.script)
        self.assertIn("window.setTimeout(attempt, 100)", driver.script)
        self.assertIn("Gunakan", driver.script)

    def test_read_expense_amount_prefers_non_zero_when_multiple_elements_exist(self):
        driver = _FakeDriverBatch(["0", "88"])

        result = read_expense_amount(driver, "//expense")

        self.assertEqual(result["amount"], Decimal("88"))


class _FakeClock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _report_record(**overrides):
    start, end = expense_range_module._report_epoch_bounds("2026-09-02", "2026-09-08")
    record = {
        "id": 1, "observer_token": "test-token",
        "endpoint": "/api/pas/v1/report/get_time_graph/", "method": "POST",
        "start_time": start, "end_time": end,
        "campaign_type": "product_homepage_v2", "filter_campaign_type": "new_cpc_homepage",
        "complete": True, "http_status": 200, "code": 0,
        "report_key": "product_homepage_v2", "cost": "647178097534",
        "started_at_ms": 1000, "completed_at_ms": 9000,
    }
    record.update(overrides)
    return record


class StrictExpenseSnapshotTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the isolated observer test")
    def test_observer_only_records_report_fields_and_preserves_network_calls(self):
        script = r"""
const assert = require('node:assert/strict');
const scripts = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let report = {code: 0, data: {key: 'product_homepage_v2',
  report_aggregate: {cost: 647178097534}, private_field: 'RESPONSE_SECRET'}};
let returnedPromise, sentInit, clonedUnrelated = false;
const response = {status: 200, clone: () => ({json: async () => report})};
const originalFetch = function(input, init) {
  sentInit = init;
  returnedPromise = Promise.resolve(response);
  return returnedPromise;
};
class FakeXHR {
  open() {}
  addEventListener(name, callback) { this.listener = callback; }
  send(body) {
    this.sentBody = body;
    this.status = 200; this.responseType = 'json'; this.response = report;
    this.listener && this.listener();
  }
}
global.XMLHttpRequest = FakeXHR;
global.window = {location: {href: 'https://banhang.shopee.vn/portal/marketing/pas/index',
  origin: 'https://banhang.shopee.vn'}, fetch: originalFetch};
const originalOpen = FakeXHR.prototype.open;
const originalSend = FakeXHR.prototype.send;
const install = new Function(scripts.install);
const read = new Function(scripts.read);
const release = new Function(scripts.release);
const path = '/api/pas/v1/report/get_time_graph/';
const body = {start_time: 1788282000, end_time: 1788886799,
  campaign_type: 'product_homepage_v2', filter_params: {campaign_type: 'new_cpc_homepage'},
  fingerprint: 'REQUEST_SECRET'};
const settle = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  const installation = install();
  assert.equal(installation.installed, true);
  const init = {method: 'POST', body: JSON.stringify(body), headers: {cookie: 'HEADER_SECRET'}};
  const promise = window.fetch(path, init);
  assert.equal(promise, returnedPromise);
  assert.equal(sentInit, init);
  assert.equal(await promise, response);
  await settle();
  let observed = read(installation.token);
  assert.equal(observed.records.length, 1);
  assert.equal(observed.records[0].cost, '647178097534');
  assert.equal(observed.records[0].code, 0);
  assert.equal(observed.records[0].complete, true);
  assert.equal(/SECRET|fingerprint|headers|private_field/.test(JSON.stringify(observed)), false);
  await window.fetch({url: 'https://banhang.shopee.vn/unrelated', method: 'POST',
    clone() { clonedUnrelated = true; throw Error('must not read unrelated request'); }});
  await window.fetch('https://other.example' + path, init);
  await settle();
  assert.equal(clonedUnrelated, false);
  assert.equal(read(installation.token).records.length, 1);
  const xhr = new FakeXHR();
  xhr.open('POST', path);
  xhr.send(init.body);
  assert.equal(xhr.sentBody, init.body);
  assert.equal(read(installation.token).records.length, 2);
  assert.equal(read(installation.token).records[1].cost, '647178097534');
  report = {code: 0, data: {key: 'product_homepage_v2',
    report_aggregate: {cost: Number.MAX_SAFE_INTEGER + 1}}};
  await window.fetch(path, init);
  await settle();
  assert.equal(read(installation.token).records[2].cost, null);
  release(installation.token);
  assert.equal(window.fetch, originalFetch);
  assert.equal(FakeXHR.prototype.open, originalOpen);
  assert.equal(FakeXHR.prototype.send, originalSend);
  assert.equal(read(installation.token), null);
  process.stdout.write('observer checks passed');
})().catch(error => { process.stderr.write(error.stack); process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            input=json.dumps({
                "install": expense_range_module._INSTALL_EXPENSE_OBSERVER_SCRIPT,
                "read": expense_range_module._READ_EXPENSE_OBSERVER_SCRIPT,
                "release": expense_range_module._RELEASE_EXPENSE_OBSERVER_SCRIPT,
            }),
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "observer checks passed")

    def _wait(self, driver, clock, timeout=12):
        with patch.object(expense_range_module.time, "monotonic", clock.monotonic), patch.object(
            expense_range_module.time, "sleep", clock.sleep
        ):
            return expense_range_module._wait_for_verified_expense_snapshot(
                driver, "//expense", "vn", "test-token", "2026-09-02", "2026-09-08",
                timeout=timeout,
            )

    def test_real_vn_report_cost_scale_and_rounding(self):
        record = expense_range_module._validated_report_record(
            {"token": "test-token", "records": [_report_record()]},
            "test-token", "2026-09-02", "2026-09-08",
        )
        self.assertEqual(record["amount"], Decimal("6471781"))

    def test_real_id_report_cost_matches_full_precision_visible_amount(self):
        record = expense_range_module._validated_report_record(
            {"token": "test-token", "records": [_report_record(cost="81216727599")]},
            "test-token", "2026-09-02", "2026-09-08",
        )
        visible = expense_range_module._read_strict_expense_amount(
            _FakeDriverBatch(["Rp812.167"]), "//expense", "id",
        )
        self.assertEqual(record["amount"], Decimal("812167"))
        self.assertEqual(visible["amount"], record["amount"])

    def test_strict_wait_does_not_accept_old_amount_for_eight_seconds(self):
        clock = _FakeClock()
        driver = _StrictExpenseDriver(clock, complete_at=8, render_at=10)
        result = self._wait(driver, clock)
        self.assertEqual(result["amount"], Decimal("6471781"))
        self.assertEqual(result["text"], "₫6.471.781")
        self.assertGreaterEqual(clock.now, 10.5)
        self.assertTrue(result["refresh_evidence"]["verified"])
        self.assertEqual(result["refresh_evidence"]["aggregate_cost"], "647178097534")

    def test_zero_is_valid_only_after_matching_response(self):
        clock = _FakeClock()
        driver = _StrictExpenseDriver(
            clock, complete_at=8, render_at=0, record=_report_record(cost="0"), text="₫0",
        )
        result = self._wait(driver, clock)
        self.assertEqual(result["amount"], Decimal("0"))
        self.assertGreaterEqual(clock.now, 8.5)

    def test_same_amount_waits_for_fresh_response(self):
        clock = _FakeClock()
        driver = _StrictExpenseDriver(clock, complete_at=8, render_at=0)
        result = self._wait(driver, clock)
        self.assertEqual(result["amount"], Decimal("6471781"))
        self.assertGreaterEqual(clock.now, 8.5)

    def test_wrong_dates_failed_response_and_unsafe_cost_are_rejected(self):
        variants = (
            {"start_time": 1788282000 + 3600},
            {"end_time": 1788886799 - 86400},
            {"complete": False},
            {"http_status": 500},
            {"code": 1001},
            {"code": False},
            {"report_key": "other_report"},
            {"campaign_type": "homepage"},
            {"filter_campaign_type": "all"},
            {"cost": None},
            {"cost": "-1"},
            {"cost": "9007199254740992"},
            {"observer_token": "previous-run"},
            {"endpoint": "/api/pas/v1/homepage/query/"},
        )
        for variant in variants:
            with self.subTest(variant=variant):
                observer = {"token": "test-token", "records": [_report_record(**variant)]}
                self.assertIsNone(expense_range_module._validated_report_record(
                    observer, "test-token", "2026-09-02", "2026-09-08",
                ))

    def test_later_pending_request_blocks_an_earlier_completed_response(self):
        observer = {"token": "test-token", "records": [
            _report_record(), _report_record(id=2, complete=False),
        ]}
        self.assertIsNone(expense_range_module._validated_report_record(
            observer, "test-token", "2026-09-02", "2026-09-08",
        ))

    def test_missing_evidence_or_wrong_visible_range_fails_closed(self):
        for variant in ("missing-evidence", "wrong-range", "wrong-timezone", "wrong-amount"):
            with self.subTest(variant=variant):
                clock = _FakeClock()
                driver = _StrictExpenseDriver(clock, complete_at=0, render_at=0)
                if variant == "missing-evidence":
                    driver.records_enabled = False
                elif variant == "wrong-range":
                    driver.visible_range = "01/09 - 08/09 (GMT+7)"
                elif variant == "wrong-timezone":
                    driver.visible_range = "02/09 - 08/09 (GMT+8)"
                else:
                    driver.text = "₫248.210"
                with self.assertRaisesRegex(RuntimeError, "未取得"):
                    self._wait(driver, clock, timeout=2)

    def test_observer_loss_fails_immediately(self):
        clock = _FakeClock()
        driver = _StrictExpenseDriver(clock, complete_at=0, render_at=0)
        driver.observer_active = False
        with self.assertRaisesRegex(RuntimeError, "观察器已失效"):
            self._wait(driver, clock)
        self.assertEqual(clock.now, 0)

    def test_multiple_visible_amounts_and_invalid_text_are_not_zero(self):
        for texts in (["₫0", "₫6.471.781"], ["--"], ["₫6.4m"]):
            with self.subTest(texts=texts):
                driver = _FakeDriverBatch(texts)
                self.assertIsNone(expense_range_module._read_strict_expense_amount(driver, "//expense", "vn"))

    def test_collector_uses_gmt7_day_and_refreshes_an_already_selected_range(self):
        clock = _FakeClock()
        driver = _StrictExpenseDriver(clock, complete_at=0, render_at=0)
        driver.visible_range = "03/09 - 09/09 (GMT+7)"
        expected = {"amount": Decimal("6471781"), "text": "₫6.471.781",
                    "refresh_evidence": {"verified": True}}
        with patch.object(expense_range_module, "datetime", _FixedReportDateTime), patch.object(
            expense_range_module, "select_ads_expense_date_range"
        ) as select, patch.object(
            expense_range_module, "_wait_for_verified_expense_snapshot", return_value=expected
        ):
            result = collect_previous_7_days_expense_snapshot(
                _FakeWebdriverUtil(driver), "越南072", "//date", amount_xpath="//expense", site_code="vn",
            )
        self.assertEqual(result["start_date"], "2026-09-03")
        self.assertEqual(result["end_date"], "2026-09-09")
        self.assertEqual(result["timezone"], "GMT+7")
        self.assertEqual(datetime.fromisoformat(result["collected_at"]).utcoffset(), timedelta(hours=7))
        self.assertEqual(result["currency"], "VND")
        self.assertTrue(result["ready"])
        self.assertEqual(select.call_count, 2)
        self.assertEqual(select.call_args_list[0].args[3:5], ("2026-09-02", "2026-09-02"))
        self.assertEqual(select.call_args_list[1].args[3:5], ("2026-09-03", "2026-09-09"))
        self.assertTrue(driver.released)

    def test_collector_releases_observer_after_failed_verification(self):
        clock = _FakeClock()
        driver = _StrictExpenseDriver(clock, complete_at=0, render_at=0)
        with patch.object(expense_range_module, "select_ads_expense_date_range"), patch.object(
            expense_range_module, "_wait_for_verified_expense_snapshot", side_effect=RuntimeError("no evidence")
        ):
            with self.assertRaisesRegex(RuntimeError, "no evidence"):
                collect_previous_7_days_expense_snapshot(
                    _FakeWebdriverUtil(driver), "越南072", "//date", amount_xpath="//expense", site_code="vn",
                )
        self.assertTrue(driver.released)


class _FixedReportDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        # Taiwan is already on September 11 while the report is still on
        # September 10. The collector must use the report calendar day.
        value = datetime(2026, 9, 11, 0, 30, tzinfo=timezone(timedelta(hours=8)))
        return value.astimezone(tz)


class _StrictExpenseDriver:
    def __init__(self, clock, *, complete_at, render_at, record=None, text="₫6.471.781"):
        self.clock = clock
        self.complete_at = complete_at
        self.render_at = render_at
        self.record = record or _report_record()
        self.text = text
        self.current_url = "https://banhang.shopee.vn/portal/marketing/pas/index"
        self.visible_range = "02/09 - 08/09 (GMT+7)"
        self.records_enabled = True
        self.observer_active = True
        self.released = False

    def execute_script(self, script, *args):
        if script == expense_range_module._INSTALL_EXPENSE_OBSERVER_SCRIPT:
            return {"installed": True, "token": "test-token"}
        if script == expense_range_module._RELEASE_EXPENSE_OBSERVER_SCRIPT:
            self.released = True
            return None
        if script == expense_range_module._READ_EXPENSE_OBSERVER_SCRIPT:
            if not self.observer_active:
                return None
            record = {**self.record, "complete": self.clock.now >= self.complete_at}
            return {"token": "test-token", "records": [record] if self.records_enabled else []}
        if "selector.getAttribute" in script:
            return self.visible_range
        return False

    def find_elements(self, by, value):
        text = self.text if self.clock.now >= self.render_at else "₫248.210"
        return [_FakeExpenseElement(text)]


class _FakeExpenseElement:
    def __init__(self, text: str):
        self.text = text

    def is_displayed(self):
        return True


class _FakeDriver:
    def __init__(self, texts: list[str]):
        self.texts = texts
        self.index = 0

    def find_elements(self, by, value):
        text = self.texts[min(self.index, len(self.texts) - 1)]
        self.index += 1
        return [_FakeExpenseElement(text)]


class _FakeDriverBatch:
    def __init__(self, texts: list[str]):
        self.texts = texts

    def find_elements(self, by, value):
        return [_FakeExpenseElement(text) for text in self.texts]


class _FakeDriverWithUrl(_FakeDriver):
    def __init__(self, texts: list[str], current_url: str):
        super().__init__(texts)
        self.current_url = current_url

    def execute_script(self, script):
        return False


class _FakeDriverWithOpenPicker(_FakeDriverWithUrl):
    def execute_script(self, script):
        return "querySelectorAll('.eds-date-table__cell" in script


class _FakeDriverWithVisibleRange(_FakeDriverWithUrl):
    def __init__(self, texts: list[str], current_url: str, visible_range: str):
        super().__init__(texts, current_url)
        self.visible_range = visible_range

    def execute_script(self, script):
        if "ads-performance-date-range-selector" in script:
            return self.visible_range
        return False


class _FakeDriverWithBrowserTimezone(_FakeDriverWithUrl):
    def __init__(
        self,
        texts: list[str],
        current_url: str,
        timezone_offset_hours: float,
    ):
        super().__init__(texts, current_url)
        self.timezone_offset_hours = timezone_offset_hours

    def execute_script(self, script):
        if "getTimezoneOffset" in script:
            return self.timezone_offset_hours
        return False


class _FakeChangingDateDriver:
    def __init__(self):
        self.current_url = (
            "https://seller.shopee.com.my/portal/marketing/pas/index"
            "?from=1782921600&to=1783526399"
        )
        self.amounts = ["100", "120"]
        self.amount_index = 0

    def find_elements(self, by, value):
        if value == "//expense":
            text = self.amounts[min(self.amount_index, len(self.amounts) - 1)]
            self.amount_index += 1
            return [_FakeExpenseElement(text)]
        return [_FakeExpenseElement("")]

    def execute_script(self, script):
        if "const pickers = document.querySelectorAll" in script:
            return 91
        return False

    def execute_async_script(self, script, start_date, end_date):
        self.current_url = (
            "https://seller.shopee.com.my/portal/marketing/pas/index"
            "?from=1783526400&to=1784131199"
        )
        return {
            "ok": True,
            "method": "visible-calendar-cells",
            "applyClicked": False,
            "startDate": start_date,
            "endDate": end_date,
        }


class _FakeRetryingDateDriver(_FakeDriverWithUrl):
    def __init__(self):
        super().__init__(
            ["100"],
            "https://seller.shopee.com.my/portal/marketing/pas/index"
            "?from=1782921600&to=1783526399",
        )
        self.selection_count = 0

    def execute_script(self, script):
        if "const pickers = document.querySelectorAll" in script:
            return 91
        return False

    def execute_async_script(self, script, start_date, end_date):
        self.selection_count += 1
        return {
            "ok": True,
            "method": "visible-calendar-cells",
            "applyClicked": False,
            "startDate": start_date,
            "endDate": end_date,
        }


class _FakeCrossMonthCalendarDriver:
    def __init__(self):
        self.call_count = 0

    def execute_async_script(self, script, start_date, end_date):
        self.call_count += 1
        navigation_call = script.index(
            "if ((!visibleStartCell || !visibleEndCell) && clickPrevMonth())"
        )
        shortcut_call = script.index("const shortcutLabel = tryClickShortcut();")
        if navigation_call > shortcut_call:
            return {
                "ok": False,
                "method": "shortcut-clicked",
                "shortcutLabel": "今天 昨天 过去7天 过去30天 过去的3个月",
            }
        if self.call_count == 1:
            return {"ok": False, "method": "navigate-prev-month"}
        return {
            "ok": True,
            "method": "visible-calendar-cells",
            "startDate": start_date,
            "endDate": end_date,
        }


class _FakeScriptCaptureDriver:
    def __init__(self):
        self.script = ""

    def execute_async_script(self, script, start_date, end_date):
        self.script = script
        return {"ok": True, "method": "visible-calendar-cells"}


class _FakeWebdriverUtil:
    def __init__(self, driver):
        self.driver = driver
        self.click_count = 0

    def move_to_click(self, element):
        self.click_count += 1


if __name__ == "__main__":
    unittest.main()
