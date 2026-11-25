from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from urllib.parse import parse_qs, urlparse

from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By

from util.shopee_ads_money import MONEY_PARSER_VERSION, SITE_CURRENCY, parse_shopee_money


def expense_range_t8_to_t1(today: date | None = None) -> tuple[str, str]:
    base_date = today or datetime.today().date()
    start_date = base_date - timedelta(days=8)
    end_date = base_date - timedelta(days=1)
    return start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")


def month_to_yesterday_range(today: date | None = None) -> tuple[str, str]:
    base_date = today or datetime.today().date()
    end_date = base_date - timedelta(days=1)
    # On the first day of a month, yesterday belongs to the previous month.
    # Using the month that contains the last completed day keeps daily jobs
    # valid across month boundaries instead of producing an empty range.
    start_date = end_date.replace(day=1)
    return start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")


def previous_7_days_without_today(today: date | None = None) -> tuple[str, str]:
    base_date = today or datetime.today().date()
    start_date = base_date - timedelta(days=7)
    end_date = base_date - timedelta(days=1)
    return start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")


_AMOUNT_TOKEN_RE = re.compile(r"(-?\d+(?:[.,]\d+)*)([kKmMwW\u4e07]?)")
_ISO_DATE_RANGE_RE = re.compile(
    r"(?P<start_year>\d{4})[-/](?P<start_month>\d{1,2})[-/](?P<start_day>\d{1,2})"
    r"\s*(?:-|~|–|—|至|到)\s*"
    r"(?P<end_year>\d{4})[-/](?P<end_month>\d{1,2})[-/](?P<end_day>\d{1,2})"
)
_DAY_FIRST_DATE_RANGE_RE = re.compile(
    r"(?P<start_day>\d{1,2})[/-](?P<start_month>\d{1,2})(?:[/-](?P<start_year>\d{2,4}))?"
    r"\s*(?:-|~|–|—|至|到)\s*"
    r"(?P<end_day>\d{1,2})[/-](?P<end_month>\d{1,2})(?:[/-](?P<end_year>\d{2,4}))?"
)
_SHOPEE_REPORT_TIMEZONE_OFFSETS = {
    "seller.shopee.com.my": 8,
    "seller.shopee.ph": 8,
    "seller.shopee.cn": 8,
    "seller.shopee.co.th": 7,
    "seller.shopee.co.id": 7,
    "seller.shopee.vn": 7,
    "banhang.shopee.vn": 7,
    "seller.shopee.com.br": -3,
}


def _normalize_amount_token(raw_amount: str, suffix: str) -> str:
    value = str(raw_amount or "").strip()
    suffix = str(suffix or "").lower()
    if not value:
        return value

    if "," in value and "." in value:
        # The last separator is the decimal separator:
        # 1,234.56 -> 1234.56; 1.234,56 -> 1234.56.
        decimal_sep = "," if value.rfind(",") > value.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        value = value.replace(thousands_sep, "")
        if decimal_sep == ",":
            value = value.replace(",", ".")
        return value

    if "," in value:
        parts = value.split(",")
        if len(parts) == 2:
            left, right = parts
            if suffix or len(right) == 2:
                return f"{left}.{right}"
        return value.replace(",", "")

    if "." in value:
        parts = value.split(".")
        if len(parts) >= 3:
            return value.replace(".", "")
        if len(parts) == 2:
            left, right = parts
            if suffix:
                return value
            if len(right) == 3 and len(left) <= 3:
                return value.replace(".", "")
        return value

    return value


def parse_expense_amount_text(value: Any) -> Decimal | None:
    text = str(value or "").replace("\xa0", " ").strip()
    if not text:
        return None

    compact_text = re.sub(r"\s+", "", text)
    match = _AMOUNT_TOKEN_RE.search(compact_text)
    if not match:
        return None

    suffix = match.group(2).lower()
    raw_amount = _normalize_amount_token(match.group(1), suffix)

    try:
        amount = Decimal(raw_amount)
    except InvalidOperation:
        return None

    if suffix == "m":
        amount *= Decimal("1000000")
    elif suffix == "k":
        amount *= Decimal("1000")
    elif suffix in ("w", "\u4e07"):
        amount *= Decimal("10000")
    return amount


def read_expense_amount(driver: Any, amount_xpath: str) -> dict[str, Any] | None:
    try:
        elements = driver.find_elements(By.XPATH, amount_xpath)
    except WebDriverException:
        return None

    first_snapshot = None
    for element in elements:
        try:
            is_displayed = getattr(element, "is_displayed", None)
            if callable(is_displayed) and not is_displayed():
                continue
            text = str(getattr(element, "text", "") or "").strip()
        except (StaleElementReferenceException, WebDriverException):
            continue

        amount = parse_expense_amount_text(text)
        if amount is not None:
            snapshot = {"amount": amount, "text": text}
            if first_snapshot is None:
                first_snapshot = snapshot
            if amount != 0:
                return snapshot
    return first_snapshot


def _coerce_year(value: str | None, fallback: int) -> int:
    if not value:
        return fallback
    year = int(value)
    return 2000 + year if year < 100 else year


def parse_selected_date_range_text(
    value: Any,
    reference_date: date | None = None,
) -> tuple[str, str] | None:
    """Parse the visible Shopee date selector text into ISO dates."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None

    match = _ISO_DATE_RANGE_RE.search(text)
    if match:
        try:
            start = date(
                int(match.group("start_year")),
                int(match.group("start_month")),
                int(match.group("start_day")),
            )
            end = date(
                int(match.group("end_year")),
                int(match.group("end_month")),
                int(match.group("end_day")),
            )
        except ValueError:
            return None
        return start.isoformat(), end.isoformat()

    match = _DAY_FIRST_DATE_RANGE_RE.search(text)
    if not match:
        return None

    reference = reference_date or datetime.today().date()
    start_year = _coerce_year(match.group("start_year"), reference.year)
    end_year = _coerce_year(match.group("end_year"), start_year)
    start_month = int(match.group("start_month"))
    end_month = int(match.group("end_month"))
    if not match.group("end_year") and end_month < start_month:
        end_year += 1
    try:
        start = date(start_year, start_month, int(match.group("start_day")))
        end = date(end_year, end_month, int(match.group("end_day")))
    except ValueError:
        return None
    return start.isoformat(), end.isoformat()


def _browser_timezone_offset_hours(driver: Any) -> float | None:
    """Return the timezone used by the current browser profile, when available."""
    script = "return -new Date().getTimezoneOffset() / 60;"
    try:
        value = driver.execute_script(script)
    except (AttributeError, WebDriverException):
        return None

    # bool is a subclass of int. Reject it so simple Selenium fakes returning
    # False do not accidentally override the site fallback with UTC.
    if isinstance(value, bool):
        return None
    try:
        offset_hours = float(value)
    except (TypeError, ValueError):
        return None
    if not -14 <= offset_hours <= 14:
        return None
    return offset_hours


def _range_from_url(
    value: Any,
    timezone_offset_hours: float | None = None,
) -> tuple[str, str] | None:
    try:
        parsed_url = urlparse(str(value or ""))
        query = parse_qs(parsed_url.query)
        start_timestamp = float(query["from"][0])
        end_timestamp = float(query["to"][0])
        offset_hours = timezone_offset_hours
        if offset_hours is None:
            offset_hours = _SHOPEE_REPORT_TIMEZONE_OFFSETS.get(
                parsed_url.hostname or "",
                0,
            )
        report_timezone = timezone(timedelta(hours=offset_hours))
        start = datetime.fromtimestamp(start_timestamp, tz=timezone.utc).astimezone(
            report_timezone
        )
        end = datetime.fromtimestamp(end_timestamp, tz=timezone.utc).astimezone(
            report_timezone
        )
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return None
    if end.date() < start.date():
        return None
    return start.date().isoformat(), end.date().isoformat()


def _selected_date_range_snapshot(
    driver: Any,
    reference_date: date | None = None,
) -> dict[str, Any]:
    try:
        current_url = str(getattr(driver, "current_url", "") or "")
    except WebDriverException:
        current_url = ""

    script = """
const selectors = [
  '[data-testid="ads-performance-date-range-selector"]',
  '.eds-date-picker__input',
  '.eds-date-range-picker'
];
for (const selectorText of selectors) {
  const selector = document.querySelector(selectorText);
  if (!selector) continue;
  const values = [
    selector.value,
    selector.getAttribute && selector.getAttribute('value'),
    selector.innerText,
    selector.textContent,
    selector.getAttribute && selector.getAttribute('aria-label'),
    selector.getAttribute && selector.getAttribute('title')
  ];
  const text = values
    .map((value) => String(value || '').replace(/\\s+/g, ' ').trim())
    .filter(Boolean)
    .join(' ');
  if (text) return text;
}
return '';
"""
    try:
        text = str(driver.execute_script(script) or "").strip()
    except (AttributeError, WebDriverException):
        text = ""
    text_range = parse_selected_date_range_text(text, reference_date=reference_date)
    if text_range is not None:
        return {"range": text_range, "source": "text", "url": current_url, "text": text}

    # Shopee builds report timestamps from the browser profile timezone. Purple
    # Bird profiles for the same marketplace can use different timezones, so a
    # fixed hostname offset can shift midnight to the previous calendar day.
    browser_timezone_offset = _browser_timezone_offset_hours(driver)
    url_range = _range_from_url(
        current_url,
        timezone_offset_hours=browser_timezone_offset,
    )
    if url_range is not None:
        return {
            "range": url_range,
            "source": "url",
            "url": current_url,
            "text": text,
            "timezone_offset_hours": browser_timezone_offset,
        }
    return {"range": None, "source": "text", "url": current_url, "text": text}


def read_selected_date_range(
    driver: Any,
    reference_date: date | None = None,
) -> tuple[str, str] | None:
    """Read the committed Shopee report range from visible text or URL timestamps."""
    return _selected_date_range_snapshot(driver, reference_date=reference_date).get("range")


def _date_picker_is_open(driver: Any) -> bool:
    script = """
const visible = (element) => {
  if (!element) return false;
  const rect = element.getBoundingClientRect();
  const style = window.getComputedStyle(element);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
return Array.from(document.querySelectorAll('.eds-popper, .eds-date-picker__picker, [class*="picker-panel"]'))
  .filter(visible)
  .some((element) => element.querySelectorAll('.eds-date-table__cell, [role="gridcell"]').length >= 14);
"""
    try:
        return bool(driver.execute_script(script))
    except (AttributeError, WebDriverException):
        return False


def wait_for_selected_date_range(
    driver: Any,
    start_date: str,
    end_date: str,
    timeout: int = 12,
    poll_interval: float = 0.4,
) -> dict[str, Any]:
    target = (start_date, end_date)
    try:
        reference_date = date.fromisoformat(start_date)
    except ValueError:
        reference_date = None
    deadline = time.monotonic() + max(timeout, 0)
    last_snapshot: dict[str, Any] = {}
    while True:
        last_snapshot = _selected_date_range_snapshot(driver, reference_date=reference_date)
        popup_open = _date_picker_is_open(driver)
        if last_snapshot.get("range") == target and not popup_open:
            return {**last_snapshot, "ok": True, "target": target, "popup_open": False}
        if time.monotonic() >= deadline:
            return {**last_snapshot, "ok": False, "target": target, "popup_open": popup_open}
        time.sleep(poll_interval)


def _expense_metrics_loading(driver: Any) -> bool:
    script = """
const selectors = [
  '[data-testid="line-metrics"][loading="true"]',
  '[data-testid="metric-chart"][loading="true"]',
  '[data-testid="ads-performance-chart"][aria-busy="true"]',
  '[data-testid="ads-performance-date-range-selector"][aria-busy="true"]'
];
return selectors.some((selector) => document.querySelector(selector));
"""
    try:
        return bool(driver.execute_script(script))
    except (AttributeError, WebDriverException):
        return False


def expense_amount_is_loaded(
    before_amount: Decimal | None,
    current_amount: Decimal | None,
    require_non_zero: bool = True,
    require_changed: bool = True,
) -> bool:
    if current_amount is None:
        return False
    if require_non_zero and current_amount == 0:
        return False
    if require_changed and before_amount is not None and current_amount == before_amount:
        return False
    return True


def wait_for_expense_amount_change(
    driver: Any,
    amount_xpath: str,
    before_amount: Decimal | None,
    timeout: int = 60,
    poll_interval: float = 1,
    require_non_zero: bool = True,
    require_changed: bool = True,
    stable_reads: int = 2,
    minimum_wait_seconds: float = 0,
) -> dict[str, Any]:
    started_at = time.monotonic()
    deadline = time.monotonic() + max(timeout, 0)
    last_snapshot: dict[str, Any] | None = None
    stable_count = 0
    stable_amount: Decimal | None = None

    while True:
        snapshot = read_expense_amount(driver, amount_xpath)
        loading = _expense_metrics_loading(driver)
        if snapshot is not None:
            last_snapshot = snapshot
            amount = snapshot.get("amount")
            loaded = expense_amount_is_loaded(
                before_amount,
                amount,
                require_non_zero=require_non_zero,
                require_changed=require_changed,
            )
            changed = before_amount is not None and amount != before_amount
            # A changed number can be an intermediate render (including the
            # previous report replacing an initial zero). Apply the same
            # stability and minimum-wait gates to changed and unchanged values.
            if loaded and not loading:
                if amount == stable_amount:
                    stable_count += 1
                else:
                    stable_amount = amount
                    stable_count = 1
                minimum_wait_met = time.monotonic() - started_at >= max(minimum_wait_seconds, 0)
                if stable_count >= max(stable_reads, 1) and minimum_wait_met:
                    return {
                        **snapshot,
                        "ok": True,
                        "before_amount": before_amount,
                        "changed": changed,
                        "stable_reads": stable_count,
                    }
            else:
                stable_count = 0
                stable_amount = None
        else:
            stable_count = 0
            stable_amount = None

        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)

    result = {
        "ok": False,
        "before_amount": before_amount,
        "changed": False,
        "stable_reads": stable_count,
    }
    if last_snapshot is not None:
        result.update(last_snapshot)
    return result


def set_ads_expense_date_range(driver: Any, start_date: str, end_date: str) -> dict[str, Any]:
    script = """
const startDate = arguments[0];
const endDate = arguments[1];
const done = arguments[arguments.length - 1];
const finish = (payload) => done(payload || {});
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
const roots = Array.from(document.querySelectorAll(
  '.eds-popper, .eds-popover, .eds-date-picker, .eds-date-picker-panel, .eds-date-range-picker, [class*="date-picker"], [class*="datepicker"], [class*="picker-panel"]'
)).filter(visible);
const areaOf = (el) => {
  const rect = el.getBoundingClientRect();
  return rect.width * rect.height;
};
roots.sort((a, b) => areaOf(b) - areaOf(a));
const root = roots.find((el) => Array.from(el.querySelectorAll('input')).filter(visible).length >= 2)
  || roots.find((el) => el.querySelector('td, [role="gridcell"], [class*="cell"], [class*="day"]'))
  || roots[0]
  || document;
const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
function setInput(el, value) {
  el.focus();
  setter.call(el, '');
  el.dispatchEvent(new Event('input', {bubbles: true}));
  setter.call(el, value);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  el.dispatchEvent(new Event('blur', {bubbles: true}));
}
function visiblePickerRoots() {
  return Array.from(document.querySelectorAll(
    '.eds-popper, .eds-popover, .eds-date-picker, .eds-date-picker-panel, .eds-daterange-picker-panel, .eds-date-range-picker, [class*="date-picker"], [class*="datepicker"], [class*="picker-panel"]'
  )).filter(visible);
}
function enabled(el) {
  if (!el) return false;
  const className = String(el.className || '');
  return !el.disabled && el.getAttribute('aria-disabled') !== 'true' && !/disabled/.test(className);
}
function clickApply() {
  const labels = /^(OK|Apply|Confirm|Done|Save|确定|确认|確認|应用|套用|好|Selesai|Terapkan|Gunakan|Áp dụng|Xác nhận|Đồng ý|Aplicar|Appliquer|ตกลง|ยืนยัน|นำไปใช้)$/i;
  const scopes = [];
  for (const scope of [root, ...visiblePickerRoots()]) {
    if (scope && scope !== document && !scopes.includes(scope)) scopes.push(scope);
  }
  const buttons = [];
  for (const scope of scopes) {
    for (const button of Array.from(scope.querySelectorAll('button, [role="button"], .eds-button, .eds-btn'))) {
      if (visible(button) && enabled(button) && !buttons.includes(button)) buttons.push(button);
    }
  }
  let button = buttons.find((candidate) => labels.test(textOf(candidate)));
  if (!button) {
    const footerSelector = '.eds-date-picker__footer, .eds-daterange-picker-panel__footer, .eds-picker-panel__footer, [class*="picker-footer"], [class*="picker__footer"]';
    button = buttons.find((candidate) => {
      const footer = candidate.closest(footerSelector);
      return footer && /primary/.test(String(candidate.className || ''));
    });
  }
  if (!button) return null;
  button.scrollIntoView({block: 'center', inline: 'center'});
  button.click();
  return textOf(button);
}
function pickerIsOpen() {
  return visiblePickerRoots().some((scope) => (
    scope.querySelectorAll('.eds-date-table__cell, td[class*="day"], [class*="date-table"] td, [role="gridcell"]').length >= 14
  ));
}
function finishAfterApply(payload) {
  const deadline = Date.now() + 1500;
  const attempt = () => {
    const applyButtonText = clickApply();
    if (applyButtonText !== null) {
      finish({...payload, ok: true, applyClicked: true, applyButtonText});
      return;
    }
    // Some Shopee date pickers auto-submit after the end date is clicked. If
    // the picker closes without an Apply button, let Python verify the actual
    // committed range before treating the selection as successful.
    if (!pickerIsOpen()) {
      finish({...payload, ok: true, applyClicked: false, commitMode: 'auto-close'});
      return;
    }
    if (Date.now() < deadline) {
      window.setTimeout(attempt, 100);
      return;
    }
    finish({...payload, ok: true, applyClicked: false, commitMode: 'verify-required'});
  };
  attempt();
}
function findDateCell(value) {
  const compact = value.replace(/-/g, '');
  const slash = value.replace(/-/g, '/');
  const nodes = Array.from(root.querySelectorAll('td, button, [role="gridcell"], [class*="cell"], [class*="day"]')).filter(visible);
  return nodes.find((el) => {
    const haystack = [
      el.getAttribute('title'),
      el.getAttribute('aria-label'),
      el.getAttribute('data-date'),
      el.getAttribute('data-value'),
      el.getAttribute('data-time'),
      textOf(el),
    ].filter(Boolean).join(' ');
    const disabled = /disabled/.test(String(el.className || '')) || el.getAttribute('aria-disabled') === 'true';
    return !disabled && (haystack.includes(value) || haystack.includes(slash) || haystack.includes(compact));
  });
}
const monthNumbers = {
  january: 1, jan: 1,
  february: 2, feb: 2,
  march: 3, mar: 3,
  april: 4, apr: 4,
  may: 5,
  june: 6, jun: 6,
  july: 7, jul: 7,
  august: 8, aug: 8,
  september: 9, sep: 9, sept: 9,
  october: 10, oct: 10,
  november: 11, nov: 11,
  december: 12, dec: 12,
  '一月': 1, '1月': 1,
  '二月': 2, '2月': 2,
  '三月': 3, '3月': 3,
  '四月': 4, '4月': 4,
  '五月': 5, '5月': 5,
  '六月': 6, '6月': 6,
  '七月': 7, '7月': 7,
  '八月': 8, '8月': 8,
  '九月': 9, '9月': 9,
  '十月': 10, '10月': 10,
  '十一月': 11, '11月': 11,
  '十二月': 12, '12月': 12,
};
function parseNumericMonthYear(text) {
  const source = String(text || '').replace(/\\s+/g, ' ').trim();
  const yearMatch = source.match(/(20\\d{2}|19\\d{2})/);
  if (yearMatch) {
    for (const [name, value] of Object.entries(monthNumbers)) {
      if (/[\\u4e00-\\u9fff]/.test(name) && source.includes(name)) {
        return {year: Number(yearMatch[1]), month: value};
      }
    }
  }
  let match = source.match(/(20\\d{2}|19\\d{2})\\s*年?\\s*[年/-]?\\s*(\\d{1,2})\\s*月?/);
  if (match) return {year: Number(match[1]), month: Number(match[2])};
  match = source.match(/(\\d{1,2})\\s*月\\s*(20\\d{2}|19\\d{2})\\s*年?/);
  if (match) return {year: Number(match[2]), month: Number(match[1])};
  match = source.match(/\\b(\\d{1,2})\\s*[/-]\\s*(20\\d{2}|19\\d{2})\\b/);
  if (match) return {year: Number(match[2]), month: Number(match[1])};
  match = source.match(/\\b(20\\d{2}|19\\d{2})\\s*[/-]\\s*(\\d{1,2})\\b/);
  if (match) return {year: Number(match[1]), month: Number(match[2])};
  return null;
}
function parseYmd(value) {
  const parts = String(value || '').split('-').map((part) => Number(part));
  if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) return null;
  return {year: parts[0], month: parts[1], day: parts[2]};
}
function parsePanelMonth(panel) {
  const labels = Array.from(panel.querySelectorAll('.eds-picker-header__label')).map(textOf).filter(Boolean);
  let month = 0;
  let year = 0;
  for (const label of labels) {
    const lower = label.toLowerCase();
    if (monthNumbers[lower]) month = monthNumbers[lower];
    if (/^\\d{4}$/.test(label)) year = Number(label);
    const numeric = parseNumericMonthYear(label);
    if (numeric) {
      month = numeric.month;
      year = numeric.year;
    }
  }
  if (month && year) return {year, month};

  const labelsNumeric = parseNumericMonthYear(labels.join(' '));
  if (labelsNumeric) return labelsNumeric;

  const headerText = textOf(panel.querySelector('.eds-picker-header') || panel).toLowerCase();
  const headerNumeric = parseNumericMonthYear(headerText);
  if (headerNumeric) return headerNumeric;
  for (const [name, value] of Object.entries(monthNumbers)) {
    if (headerText.includes(name)) {
      const match = headerText.match(/(20\\d{2}|19\\d{2})/);
      if (match) return {year: Number(match[1]), month: value};
    }
  }
  return null;
}
function visibleDatePanels() {
  const panels = Array.from(root.querySelectorAll(
    '.eds-date-picker-panel__date, .eds-daterange-picker-panel__body-left, .eds-daterange-picker-panel__body-right'
  )).filter(visible);
  return panels.length ? panels : [root];
}
function _dayCellTextMatches(cellText, dayNum) {
  const t = String(cellText || '').trim();
  if (t === String(dayNum)) return true;
  if (t === String(dayNum).padStart(2, '0')) return true;
  const n = parseInt(t, 10);
  return Number.isFinite(n) && n === dayNum;
}
function findVisibleCalendarDateCell(value) {
  const target = parseYmd(value);
  if (!target) return null;
  const compact = value.replace(/-/g, '');
  const slash = value.replace(/-/g, '/');
  for (const panel of visibleDatePanels()) {
    const panelMonth = parsePanelMonth(panel);
    if (panelMonth && (panelMonth.year !== target.year || panelMonth.month !== target.month)) continue;
    const rows = Array.from(panel.querySelectorAll('.eds-date-table__rows .eds-date-table__cell')).filter(visible);
    let cells = rows.length
      ? rows
      : Array.from(panel.querySelectorAll('.eds-date-table__cell')).filter(visible);
    if (!cells.length) {
      cells = Array.from(panel.querySelectorAll('td, [role="gridcell"], [class*="day"]')).filter(visible);
    }
    if (!cells.length) {
      cells = Array.from(panel.querySelectorAll('.eds-date-table td, .eds-date-table__row td, .el-date-table td')).filter(visible);
    }
    const cell = cells.find((el) => {
      const className = String(el.className || '');
      if (/disabled|out-of-month|is-empty/.test(className) || el.getAttribute('aria-disabled') === 'true') {
        return false;
      }
      if (!panelMonth) {
        const haystack = [
          el.getAttribute('title'),
          el.getAttribute('aria-label'),
          el.getAttribute('data-date'),
          el.getAttribute('data-value'),
          el.getAttribute('data-time'),
        ].filter(Boolean).join(' ');
        return haystack.includes(value) || haystack.includes(slash) || haystack.includes(compact);
      }
      return _dayCellTextMatches(textOf(el), target.day);
    });
    if (cell) {
      return cell.querySelector('.eds-date-table__cell-inner') || cell;
    }
  }
  return null;
}
function clickPrevMonth() {
  const prevButtons = Array.from(root.querySelectorAll('.eds-picker-header__prev')).filter((el) => {
    const className = String(el.className || '');
    return visible(el) && !/disabled/.test(className);
  });
  const button = prevButtons[prevButtons.length - 1];
  if (!button) return false;
  button.scrollIntoView({block: 'center', inline: 'center'});
  button.click();
  return true;
}
function clickNextMonth() {
  const nextButtons = Array.from(root.querySelectorAll('.eds-picker-header__next')).filter((el) => {
    const className = String(el.className || '');
    return visible(el) && !/disabled/.test(className);
  });
  const button = nextButtons[nextButtons.length - 1];
  if (!button) return false;
  button.scrollIntoView({block: 'center', inline: 'center'});
  button.click();
  return true;
}
function tryClickShortcut() {
  const patterns = [
    /^(过去7天|过去七天|最近7天|最近七天)$/,
    /^(Last\\s*7\\s*Days|Past\\s*7\\s*Days|Previous\\s*7\\s*Days)$/i,
    /^(Últimos\\s*7\\s*dias|Ultimos\\s*7\\s*dias)$/i,
  ];
  const buttons = Array.from(root.querySelectorAll('button, [role="button"], .eds-button, .eds-btn, .shortcut-item, [class*="shortcut"]')).filter(visible);
  for (const p of patterns) {
    const match = buttons.find((btn) => p.test(textOf(btn).trim()));
    if (match) {
      match.scrollIntoView({block: 'center', inline: 'center'});
      match.click();
      return textOf(match);
    }
  }
  return null;
}
function collectDiagnostics() {
  const panels = visibleDatePanels();
  return panels.map((p, i) => {
    const pm = parsePanelMonth(p);
    const cells = Array.from(p.querySelectorAll('.eds-date-table__cell')).filter(visible).slice(0, 6);
    const texts = cells.map((el) => {
      const cls = String(el.className || '').slice(0, 40);
      const txt = textOf(el).trim();
      const filt = /disabled|out-of-month|is-empty/.test(cls) || el.getAttribute('aria-disabled') === 'true';
      return (filt ? '[' + txt + ']' : txt) + (cls ? '(' + cls + ')' : '');
    }).join(',');
    return 'panel' + i + ':' + (pm ? pm.year + '-' + pm.month : '?') + ' cells=[' + texts + ']';
  }).join(' | ');
}
const startCell = findDateCell(startDate);
const endCell = findDateCell(endDate);
if (startCell && endCell) {
  startCell.scrollIntoView({block: 'center', inline: 'center'});
  startCell.click();
  window.setTimeout(() => {
    endCell.scrollIntoView({block: 'center', inline: 'center'});
    endCell.click();
    window.setTimeout(() => {
      finishAfterApply({method: 'cells', startDate, endDate});
    }, 300);
  }, 350);
  return;
}
const visibleStartCell = findVisibleCalendarDateCell(startDate);
const visibleEndCell = findVisibleCalendarDateCell(endDate);
if (visibleStartCell && visibleEndCell) {
  visibleStartCell.scrollIntoView({block: 'center', inline: 'center'});
  visibleStartCell.click();
  window.setTimeout(() => {
    visibleEndCell.scrollIntoView({block: 'center', inline: 'center'});
    visibleEndCell.click();
    window.setTimeout(() => {
      finishAfterApply({method: 'visible-calendar-cells', startDate, endDate});
    }, 300);
  }, 350);
  return;
}
if ((!visibleStartCell || !visibleEndCell) && clickPrevMonth()) {
  finish({
    ok: false,
    method: 'navigate-prev-month',
    missingStart: !visibleStartCell,
    missingEnd: !visibleEndCell,
    rootText: textOf(root).slice(0, 300),
    diagnostics: collectDiagnostics(),
  });
  return;
}
const shortcutLabel = tryClickShortcut();
if (shortcutLabel) {
  finish({
    ok: false,
    method: 'shortcut-clicked',
    shortcutLabel: shortcutLabel,
    missingStart: !visibleStartCell,
    missingEnd: !visibleEndCell,
    diagnostics: collectDiagnostics(),
  });
  return;
}
const inputs = Array.from(root.querySelectorAll('input')).filter(visible);
if (inputs.length >= 2) {
  setInput(inputs[0], startDate);
  setInput(inputs[1], endDate);
  finishAfterApply({method: 'inputs', values: [inputs[0].value, inputs[1].value]});
  return;
}
finish({ok: false, method: 'none', inputCount: inputs.length, rootText: textOf(root).slice(0, 300), diagnostics: collectDiagnostics()});
"""
    result: dict[str, Any] = {}
    for _ in range(4):
        result = driver.execute_async_script(script, start_date, end_date) or {}
        if result.get("ok") or result.get("method") not in ("navigate-prev-month", "shortcut-clicked"):
            return result
        # If shortcut was clicked, the date picker state has changed — retry
        # the full script which will attempt cell selection on the updated calendar
        time.sleep(0.5)
    return result


def _page_context(driver: Any, max_body_chars: int = 300) -> str:
    try:
        url = str(getattr(driver, "current_url", "") or "")
    except WebDriverException:
        url = ""
    try:
        title = str(getattr(driver, "title", "") or "")
    except WebDriverException:
        title = ""
    body = ""
    try:
        body = str(driver.find_element(By.TAG_NAME, "body").text or "").replace("\n", " ").strip()
    except WebDriverException:
        pass
    if len(body) > max_body_chars:
        body = body[:max_body_chars] + "..."
    return f"url={url}; title={title}; body={body}"


def _find_date_input(driver: Any, date_input_xpaths: Any, timeout: int = 20) -> tuple[Any, str]:
    if isinstance(date_input_xpaths, str):
        xpaths = [date_input_xpaths]
    else:
        xpaths = [str(item) for item in date_input_xpaths if str(item or "").strip()]

    deadline = time.monotonic() + max(timeout, 1)
    js_selector = """
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const selectors = [
  '.eds-date-picker__input .eds-selector',
  '.eds-date-picker__input',
  '.eds-date-range-picker .eds-selector',
  '[class*="date-picker"] [class*="selector"]',
  '[class*="daterange"] [class*="selector"]'
];
for (const selector of selectors) {
  const found = Array.from(document.querySelectorAll(selector)).find(visible);
  if (found) return found;
}
return null;
"""

    while True:
        for xpath in xpaths:
            try:
                elements = driver.find_elements(By.XPATH, xpath)
            except WebDriverException:
                elements = []
            for element in elements:
                try:
                    if not hasattr(element, "is_displayed") or element.is_displayed():
                        return element, xpath
                except WebDriverException:
                    continue

        try:
            element = driver.execute_script(js_selector)
            if element:
                return element, "js-visible-date-selector"
        except WebDriverException:
            pass

        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)

    raise RuntimeError(f"未找到广告日期选择器：{_page_context(driver)}")


def _wait_for_date_picker_ready(driver: Any, timeout: int = 8, log: Any = None) -> None:
    """Wait for the date picker popover to open and render calendar cells after clicking the date input."""
    deadline = time.monotonic() + max(timeout, 1)
    js_check = """
const pickers = document.querySelectorAll(
  '.eds-popper, .eds-popover, .eds-date-picker, .eds-date-picker-panel, .eds-date-range-picker, [class*="date-picker"], [class*="picker-panel"]'
);
for (const el of pickers) {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  if (rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden') {
    const cells = el.querySelectorAll('.eds-date-table__cell, td[class*="day"], [class*="date-table"] td');
    if (cells.length >= 14) return cells.length;
  }
}
return 0;
"""
    while True:
        try:
            cell_count = driver.execute_script(js_check)
        except Exception:
            cell_count = 0
        if isinstance(cell_count, (int, float)) and cell_count >= 14:
            if log:
                log.info(f"日期选择器已就绪，日历单元格数: {cell_count}")
            return
        if time.monotonic() >= deadline:
            if log:
                log.warning(f"等待日期选择器超时（{timeout}s），当前单元格数: {cell_count}")
            return
        time.sleep(0.3)

def select_ads_expense_date_range(
    webdriver_util: Any,
    store_name: str,
    date_input_xpath: Any,
    start_date: str,
    end_date: str,
    log: Any = None,
    wait_seconds: int = 5,
    amount_xpath: str | None = None,
    amount_wait_timeout: int = 60,
    require_non_zero: bool = True,
) -> tuple[str, str]:
    if log:
        log.info(f"{store_name}: 选择广告消耗金额日期范围 {start_date} ~ {end_date}")

    target_range = (start_date, end_date)
    try:
        reference_date = date.fromisoformat(start_date)
    except ValueError:
        reference_date = None
    initial_range_snapshot = _selected_date_range_snapshot(
        webdriver_util.driver,
        reference_date=reference_date,
    )
    initial_range = initial_range_snapshot.get("range")

    before_snapshot = None
    before_amount = None
    if amount_xpath:
        before_snapshot = read_expense_amount(webdriver_util.driver, amount_xpath)
        if before_snapshot is not None:
            before_amount = before_snapshot.get("amount")
            if log:
                log.info(f"{store_name}: Expense before date range: {before_snapshot.get('text')}")

    already_selected = initial_range == target_range
    if already_selected:
        result = {
            "ok": True,
            "method": "already-selected",
            "source": initial_range_snapshot.get("source"),
            "text": initial_range_snapshot.get("text"),
        }
        if log:
            log.info(
                f"{store_name}: 当前页面已经是目标日期范围 {start_date} ~ {end_date}，跳过重复选择"
            )
    else:
        max_selection_attempts = 3
        result: dict[str, Any] = {}
        date_result: dict[str, Any] = {}
        for selection_attempt in range(1, max_selection_attempts + 1):
            # A failed commit may leave the picker either open or closed. Re-find
            # the trigger after every close because Shopee can replace it when the
            # report URL/state changes.
            if not _date_picker_is_open(webdriver_util.driver):
                date_input, input_method = _find_date_input(
                    webdriver_util.driver,
                    date_input_xpath,
                )
                if log:
                    log.info(
                        f"{store_name}: 找到广告日期选择器，方式={input_method}，"
                        f"尝试={selection_attempt}/{max_selection_attempts}"
                    )
                webdriver_util.move_to_click(date_input)
            elif log:
                log.info(
                    f"{store_name}: 日期选择器仍处于打开状态，直接重试选择，"
                    f"尝试={selection_attempt}/{max_selection_attempts}"
                )

            # Poll-wait outside the page's JavaScript thread so the calendar can
            # actually render before the selection script runs.
            _wait_for_date_picker_ready(webdriver_util.driver, log=log)
            result = set_ads_expense_date_range(
                webdriver_util.driver,
                start_date,
                end_date,
            )

            if result.get("ok"):
                # applyClicked=False is valid for Shopee variants that auto-submit
                # after the end date. The committed range and closed picker are the
                # authoritative success gate.
                date_result = wait_for_selected_date_range(
                    webdriver_util.driver,
                    start_date,
                    end_date,
                )
                if date_result.get("ok"):
                    break
            else:
                selection_snapshot = _selected_date_range_snapshot(
                    webdriver_util.driver,
                    reference_date=reference_date,
                )
                popup_open = _date_picker_is_open(webdriver_util.driver)
                date_result = {
                    **selection_snapshot,
                    "ok": (
                        selection_snapshot.get("range") == target_range
                        and not popup_open
                    ),
                    "target": target_range,
                    "popup_open": popup_open,
                }
                if date_result.get("ok"):
                    break

            if selection_attempt < max_selection_attempts:
                if log:
                    log.warning(
                        f"{store_name}: 广告日期范围尚未提交为目标值，准备重试，"
                        f"尝试={selection_attempt}/{max_selection_attempts}，"
                        f"当前={date_result.get('range')}，"
                        f"popup_open={date_result.get('popup_open')}，选择结果={result}"
                    )
                time.sleep(0.5)
                continue

            if not result.get("ok"):
                raise RuntimeError(
                    f"{store_name}:未能选择广告消耗金额日期范围 {start_date} ~ {end_date}，"
                    f"尝试次数={max_selection_attempts}，页面日期控件返回: {result}"
                )
            raise RuntimeError(
                f"{store_name}: 广告日期范围未提交成功，目标={target_range}，"
                f"当前={date_result.get('range')}，source={date_result.get('source')}，"
                f"text={date_result.get('text')!r}，popup_open={date_result.get('popup_open')}，"
                f"尝试次数={max_selection_attempts}，选择结果={result}"
            )

    if amount_xpath:
        amount_result = wait_for_expense_amount_change(
            webdriver_util.driver,
            amount_xpath,
            before_amount=before_amount,
            timeout=amount_wait_timeout,
            require_non_zero=require_non_zero,
            require_changed=False,
            stable_reads=1 if already_selected else 2,
            minimum_wait_seconds=(
                0 if already_selected else min(wait_seconds, max(amount_wait_timeout, 0))
            ),
        )
        if not amount_result.get("ok"):
            before_text = before_snapshot.get("text") if before_snapshot else ""
            last_text = amount_result.get("text") or amount_result.get("amount")
            raise RuntimeError(
                f"{store_name}: Expense amount did not become ready after date range "
                f"{start_date} ~ {end_date}; before={before_text or before_amount}, "
                f"last={last_text}, initial_range={initial_range}, "
                f"method={result.get('method')}, timeout={amount_wait_timeout}s"
            )
        if log:
            log.info(
                f"{store_name}: Expense after date range: {amount_result.get('text')} "
                f"(before={before_snapshot.get('text') if before_snapshot else before_amount}, "
                f"changed={amount_result.get('changed')})"
            )
    elif not already_selected:
        time.sleep(wait_seconds)

    if log:
        log.info(f"{store_name}: 广告消耗金额日期范围已选择，方式={result.get('method')}")
    return start_date, end_date


def select_previous_7_days_expense_range(
    webdriver_util: Any,
    store_name: str,
    date_input_xpath: str,
    log: Any = None,
    wait_seconds: int = 5,
    amount_xpath: str | None = None,
    amount_wait_timeout: int = 60,
) -> tuple[str, str]:
    start_date, end_date = previous_7_days_without_today()
    if log:
        log.info(f"{store_name}: 选择前7天消耗金额日期范围 {start_date} ~ {end_date}，不含当日")
    return select_ads_expense_date_range(
        webdriver_util,
        store_name,
        date_input_xpath,
        start_date,
        end_date,
        log=log,
        wait_seconds=wait_seconds,
        amount_xpath=amount_xpath,
        amount_wait_timeout=amount_wait_timeout,
        require_non_zero=False,
    )


_REPORT_TIMEZONE = timezone(timedelta(hours=7))
_EXPENSE_REPORT_PATH = "/api/pas/v1/report/get_time_graph/"
_EXPENSE_OBSERVER_KEY = "__hqylAdsExpenseCaptureV1"
_STRICT_EXPENSE_TIMEOUT_SECONDS = 60
_STRICT_EXPENSE_SITES = {"vn", "id"}

# Observe only the report requested by Shopee's own UI. Do not issue requests,
# read headers, retain fingerprints, or return complete request/response bodies.
_INSTALL_EXPENSE_OBSERVER_SCRIPT = r"""
const key = '__hqylAdsExpenseCaptureV1';
if (window[key] && window[key].active) return {installed: false};
const state = {
  active: true, token: String(Date.now()) + '-' + String(Math.random()),
  records: [], sequence: 0
};
const originalFetch = window.fetch;
const originalOpen = XMLHttpRequest.prototype.open;
const originalSend = XMLHttpRequest.prototype.send;
const xhrRequests = new WeakMap();
const allowedPath = '/api/pas/v1/report/get_time_graph/';
function isReportRequest(method, urlValue) {
  let url;
  try { url = new URL(String(urlValue), window.location.href); }
  catch (_) { return false; }
  return state.active && String(method).toUpperCase() === 'POST' &&
      url.origin === window.location.origin && url.pathname === allowedPath;
}
function begin(method, urlValue, body) {
  if (!isReportRequest(method, urlValue)) return null;
  let data;
  try { data = typeof body === 'string' ? JSON.parse(body) : null; }
  catch (_) { return null; }
  if (!data || typeof data !== 'object') return null;
  const filter = data.filter_params;
  const record = {
    id: ++state.sequence, observer_token: state.token,
    endpoint: allowedPath, method: 'POST',
    start_time: Number.isSafeInteger(data.start_time) ? data.start_time : null,
    end_time: Number.isSafeInteger(data.end_time) ? data.end_time : null,
    campaign_type: data.campaign_type === 'product_homepage_v2' ? data.campaign_type : null,
    filter_campaign_type: filter && filter.campaign_type === 'new_cpc_homepage'
      ? filter.campaign_type : null,
    started_at_ms: Date.now(), completed_at_ms: null, complete: false,
    http_status: null, code: null, report_key: null, cost: null
  };
  state.records.push(record);
  if (state.records.length > 20) state.records.shift();
  return record;
}
function finish(record, status, data) {
  if (!record || !state.active) return;
  record.http_status = Number.isInteger(status) ? status : null;
  record.code = data && Number.isInteger(data.code) ? data.code : null;
  const report = data && data.data;
  record.report_key = report && report.key === 'product_homepage_v2' ? report.key : null;
  const cost = report && report.report_aggregate && report.report_aggregate.cost;
  // The observed API returns cost in 1/100000 currency units. Preserve the
  // integer exactly; silently rounded JS numbers must never become evidence.
  record.cost = Number.isSafeInteger(cost) && cost >= 0 ? String(cost) : null;
  record.complete = true;
  record.completed_at_ms = Date.now();
}
function observedFetch(input, init) {
  let bodyPromise;
  const method = init && init.method || input && input.method || 'GET';
  const url = typeof input === 'string' || input instanceof URL ? String(input) : input.url;
  if (!isReportRequest(method, url)) return originalFetch.apply(this, arguments);
  try {
    bodyPromise = init && Object.prototype.hasOwnProperty.call(init, 'body')
      ? Promise.resolve(init.body)
      : (input && typeof input.clone === 'function' ? input.clone().text() : Promise.resolve(null));
  } catch (_) { bodyPromise = Promise.resolve(null); }
  const pendingRecord = bodyPromise.then(body => begin(method, url, body)).catch(() => null);
  const result = originalFetch.apply(this, arguments);
  result.then(response => {
    pendingRecord.then(record => {
      if (!record) return;
      try {
        response.clone().json().then(data => finish(record, response.status, data))
          .catch(() => finish(record, response.status, null));
      } catch (_) { finish(record, response.status, null); }
    });
  }, () => pendingRecord.then(record => finish(record, 0, null)));
  return result;
}
function observedOpen(method, url) {
  xhrRequests.set(this, {method: method, url: url});
  return originalOpen.apply(this, arguments);
}
function observedSend(body) {
  const request = xhrRequests.get(this);
  const record = request ? begin(request.method, request.url, body) : null;
  if (record) {
    this.addEventListener('loadend', () => {
      let data = null;
      try { data = this.responseType === 'json' ? this.response : JSON.parse(this.responseText); }
      catch (_) {}
      finish(record, this.status, data);
    }, {once: true});
  }
  return originalSend.apply(this, arguments);
}
state.release = function () {
  state.active = false;
  if (window.fetch === observedFetch) window.fetch = originalFetch;
  if (XMLHttpRequest.prototype.open === observedOpen) XMLHttpRequest.prototype.open = originalOpen;
  if (XMLHttpRequest.prototype.send === observedSend) XMLHttpRequest.prototype.send = originalSend;
  state.records.length = 0;
  if (window[key] === state) delete window[key];
};
window[key] = state;
try {
  window.fetch = observedFetch;
  XMLHttpRequest.prototype.open = observedOpen;
  XMLHttpRequest.prototype.send = observedSend;
  if (window.fetch !== observedFetch || XMLHttpRequest.prototype.send !== observedSend ||
      XMLHttpRequest.prototype.open !== observedOpen) throw new Error('observer unavailable');
} catch (_) {
  state.release();
  return {installed: false};
}
return {installed: true, token: state.token};
"""

_READ_EXPENSE_OBSERVER_SCRIPT = r"""
const state = window.__hqylAdsExpenseCaptureV1;
if (!state || !state.active || state.token !== arguments[0]) return null;
return {token: state.token, records: state.records.map(record => Object.assign({}, record))};
"""

_RELEASE_EXPENSE_OBSERVER_SCRIPT = r"""
const state = window.__hqylAdsExpenseCaptureV1;
if (state && state.token === arguments[0]) state.release();
"""


def _report_epoch_bounds(start_date: str, end_date: str) -> tuple[int, int]:
    start = datetime.fromisoformat(start_date).replace(tzinfo=_REPORT_TIMEZONE)
    end = datetime.fromisoformat(end_date).replace(tzinfo=_REPORT_TIMEZONE) + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp()) - 1


def _validated_report_record(
    observer: Any,
    token: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any] | None:
    """Accept the latest matching request only after its verified response."""
    if not isinstance(observer, dict) or observer.get("token") != token:
        return None
    start_time, end_time = _report_epoch_bounds(start_date, end_date)
    records = observer.get("records")
    if not isinstance(records, list):
        return None
    matching = [
        record for record in records
        if isinstance(record, dict)
        and record.get("observer_token") == token
        and record.get("endpoint") == _EXPENSE_REPORT_PATH
        and record.get("method") == "POST"
        and record.get("start_time") == start_time
        and record.get("end_time") == end_time
        and record.get("campaign_type") == "product_homepage_v2"
        and record.get("filter_campaign_type") == "new_cpc_homepage"
        and type(record.get("id")) is int
    ]
    if not matching:
        return None
    record = max(matching, key=lambda item: item["id"])
    if (
        record.get("complete") is not True
        or record.get("http_status") != 200
        or type(record.get("code")) is not int
        or record.get("code") != 0
        or record.get("report_key") != "product_homepage_v2"
    ):
        return None
    cost_text = record.get("cost")
    if not isinstance(cost_text, str) or not re.fullmatch(r"\d+", cost_text):
        return None
    if int(cost_text) > 9007199254740991:
        return None
    started, completed = record.get("started_at_ms"), record.get("completed_at_ms")
    if type(started) is not int or type(completed) is not int or completed < started:
        return None
    amount = (Decimal(cost_text) / Decimal("100000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return {**record, "amount": amount}


def _read_strict_expense_amount(driver: Any, amount_xpath: str, site_code: str) -> dict[str, Any] | None:
    try:
        candidates = []
        for element in driver.find_elements(By.XPATH, amount_xpath):
            if not element.is_displayed():
                continue
            candidates.append(str(element.text or "").strip())
        # Multiple visible matches are ambiguous, even if one is zero. Never
        # choose the first nonzero value or fall back to a hidden old component.
        if len(candidates) != 1:
            return None
        text = candidates[0]
        return {"text": text, "amount": parse_shopee_money(text, site_code)}
    except (ValueError, StaleElementReferenceException, WebDriverException):
        return None


def _strict_selected_range_matches(driver: Any, start_date: str, end_date: str) -> bool:
    snapshot = _selected_date_range_snapshot(driver, reference_date=date.fromisoformat(start_date))
    if snapshot.get("range") != (start_date, end_date) or _date_picker_is_open(driver):
        return False
    text = str(snapshot.get("text") or "")
    timezone_match = re.search(r"GMT\s*([+-]\s*\d+(?::\d+)?)", text, re.IGNORECASE)
    if timezone_match:
        return timezone_match.group(1).replace(" ", "") in ("+7", "+07", "+7:00", "+07:00")
    # Some page variants omit the timezone label. The request evidence still
    # has to match exact GMT+7 epoch bounds; never accept a conflicting profile.
    offset = snapshot.get("timezone_offset_hours")
    return offset is None or offset == 7


def _wait_for_verified_expense_snapshot(
    driver: Any, amount_xpath: str, site_code: str, token: str,
    start_date: str, end_date: str,
    *, timeout: int = _STRICT_EXPENSE_TIMEOUT_SECONDS, poll_interval: float = 0.5,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout, 0)
    stable_key = None
    stable_reads = 0
    while True:
        try:
            observer = driver.execute_script(_READ_EXPENSE_OBSERVER_SCRIPT, token)
        except (AttributeError, WebDriverException) as exc:
            raise RuntimeError("无法读取广告报表刷新证据，已停止采集") from exc
        if observer is None:
            raise RuntimeError("广告报表观察器已失效或页面已跳转，已停止采集")
        report = _validated_report_record(observer, token, start_date, end_date)
        snapshot = None
        if report is not None and _strict_selected_range_matches(driver, start_date, end_date):
            snapshot = _read_strict_expense_amount(driver, amount_xpath, site_code)
        if snapshot is not None and snapshot["amount"] == report["amount"] and not _expense_metrics_loading(driver):
            key = (report["id"], snapshot["amount"], snapshot["text"])
            stable_reads = stable_reads + 1 if key == stable_key else 1
            stable_key = key
            if stable_reads >= 2:
                # Recheck after reading the DOM: a later report request or a
                # date change during the read invalidates the candidate.
                latest_observer = driver.execute_script(_READ_EXPENSE_OBSERVER_SCRIPT, token)
                latest_report = _validated_report_record(latest_observer, token, start_date, end_date)
                if (
                    latest_report is None or latest_report["id"] != report["id"]
                    or latest_report["amount"] != snapshot["amount"]
                    or not _strict_selected_range_matches(driver, start_date, end_date)
                ):
                    stable_key = None
                    stable_reads = 0
                    if time.monotonic() >= deadline:
                        raise RuntimeError("广告报表在采集期间发生变化，已停止采集")
                    time.sleep(poll_interval)
                    continue
                return {
                    **snapshot,
                    "refresh_evidence": {
                        "verified": True,
                        "method": "shopee-report-response-and-visible-expense",
                        "endpoint": report["endpoint"],
                        "request_id": report["id"],
                        "start_time": report["start_time"],
                        "end_time": report["end_time"],
                        "http_status": report["http_status"],
                        "code": report["code"],
                        "report_key": report["report_key"],
                        "aggregate_cost": report["cost"],
                        "cost_scale": 100000,
                        "rounding": "ROUND_HALF_UP",
                        "started_at_ms": report["started_at_ms"],
                        "completed_at_ms": report["completed_at_ms"],
                        "stable_reads": stable_reads,
                    },
                }
        else:
            stable_key = None
            stable_reads = 0
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "未取得与目标日期和页面消耗一致的 Shopee 广告报表响应，已停止采集；"
                f"目标={start_date}~{end_date} GMT+7"
            )
        time.sleep(poll_interval)


def _ensure_product_ads_report(webdriver_util: Any, *, timeout: int = 20) -> None:
    """Select the report required by the strict request/response validator.

    Shopee remembers Shop+ / Live Ads across visits to /pas/index. Date
    selection alone does not change that report and cannot produce the product
    report evidence. Only a unique visible tab and its active state are trusted.
    """
    driver = webdriver_util.driver
    deadline = time.monotonic() + max(timeout, 0)
    clicked = False
    while True:
        try:
            candidates = [element for element in driver.find_elements(
                By.CSS_SELECTOR, '[data-testid="ads-tab-new_cpc_homepage"]'
            ) if element.is_displayed()]
            if len(candidates) == 1:
                tab = candidates[0]
                parent = tab.find_element(By.XPATH, "..")
                active = "active" in str(parent.get_attribute("class") or "").split()
                report_type = parse_qs(urlparse(str(driver.current_url or "")).query).get("type", [])
                if active and report_type == ["new_cpc_homepage"]:
                    return
                if not clicked:
                    webdriver_util.move_to_click(tab)
                    clicked = True
        except (StaleElementReferenceException, WebDriverException):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("无法确认已切换至商品广告报表，已停止采集")
        time.sleep(0.5)


def collect_previous_7_days_expense_snapshot(
    webdriver_util: Any,
    store_name: str,
    date_input_xpath: Any,
    log: Any = None,
    *,
    amount_xpath: str,
    site_code: str,
) -> dict[str, Any]:
    """Read a complete VN/ID report, with request-bound freshness evidence."""
    site_code = str(site_code or "").strip().lower()
    if site_code not in _STRICT_EXPENSE_SITES:
        raise ValueError("严格七天广告消耗采集仅支持越南和印尼")
    driver = webdriver_util.driver
    allowed_hosts = {
        "vn": {"banhang.shopee.vn", "seller.shopee.vn", "seller.shopee.cn"},
        "id": {"seller.shopee.co.id", "seller.shopee.cn"},
    }
    if urlparse(str(driver.current_url or "")).hostname not in allowed_hosts[site_code]:
        raise RuntimeError("当前广告页面与所选站点不一致，已停止采集")
    today = datetime.now(_REPORT_TIMEZONE).date()
    start_date, end_date = previous_7_days_without_today(today)
    installation = driver.execute_script(_INSTALL_EXPENSE_OBSERVER_SCRIPT)
    if not isinstance(installation, dict) or installation.get("installed") is not True or not installation.get("token"):
        raise RuntimeError("无法安装广告报表刷新观察器，已停止采集")
    token = installation["token"]
    try:
        _ensure_product_ads_report(webdriver_util)
        current_range = read_selected_date_range(driver, reference_date=date.fromisoformat(start_date))
        if current_range == (start_date, end_date):
            # An already selected report may be cached or still rendering. Make
            # the UI request the target again; a cached result without a fresh
            # matching response will fail closed in the verification below.
            alternate = (date.fromisoformat(start_date) - timedelta(days=1)).isoformat()
            select_ads_expense_date_range(
                webdriver_util, store_name, date_input_xpath, alternate, alternate,
                log=log, wait_seconds=0,
            )
        select_ads_expense_date_range(
            webdriver_util, store_name, date_input_xpath, start_date, end_date,
            log=log, wait_seconds=0,
        )
        snapshot = _wait_for_verified_expense_snapshot(
            driver, amount_xpath, site_code, token, start_date, end_date,
        )
        result = {
            **snapshot,
            "ready": True,
            "start_date": start_date,
            "end_date": end_date,
            "timezone": "GMT+7",
            "collected_at": datetime.now(_REPORT_TIMEZONE).isoformat(),
            "site_code": site_code,
            "currency": SITE_CURRENCY[site_code],
            "parser_version": MONEY_PARSER_VERSION,
            "store_name": store_name,
        }
        if log:
            log.info(
                f"{store_name}: 已核对七天广告报表及页面金额 "
                f"{start_date}~{end_date} GMT+7，{result['currency']} {result['amount']}"
            )
        return result
    finally:
        try:
            driver.execute_script(_RELEASE_EXPENSE_OBSERVER_SCRIPT, token)
        except (AttributeError, WebDriverException):
            # Page navigation destroys its observer. Do not hide the original
            # failed collection with a secondary cleanup error.
            pass


def select_month_to_yesterday_expense_range(
    webdriver_util: Any,
    store_name: str,
    date_input_xpath: str,
    log: Any = None,
    today: date | None = None,
    wait_seconds: int = 5,
    amount_xpath: str | None = None,
    amount_wait_timeout: int = 60,
    require_non_zero: bool = False,
) -> tuple[str, str]:
    start_date, end_date = month_to_yesterday_range(today)
    return select_ads_expense_date_range(
        webdriver_util,
        store_name,
        date_input_xpath,
        start_date,
        end_date,
        log=log,
        wait_seconds=wait_seconds,
        amount_xpath=amount_xpath,
        amount_wait_timeout=amount_wait_timeout,
        require_non_zero=require_non_zero,
    )
