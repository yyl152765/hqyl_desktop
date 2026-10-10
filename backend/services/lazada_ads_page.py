"""Thai Lazada metric extraction adapted from the existing Yingdao ads workflow.

Only browser page extraction is retained. Accounts, scheduling, sheet writes and
runtime lifecycle belong to lazada_ads_data; no legacy project import is needed.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.parse import urlsplit

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from backend.services.lazada_monthly_report import PlaywrightMonthlyReportPageActions

_ACTIVE_LOGGER: ContextVar[logging.Logger] = ContextVar("lazada_ads_logger", default=logging.getLogger(__name__))


class _TaskLogger:
    def info(self, message: str) -> None:
        _ACTIVE_LOGGER.get().info(message)

    def warning(self, message: str) -> None:
        _ACTIVE_LOGGER.get().warning(message)


LOGGER = _TaskLogger()


def parse_iso_date(value: str) -> date:
    parts = re.split(r"[-/.]", str(value).strip())
    return date(*(int(part) for part in parts))


def open_lazada_data_page(
    page: Page, shop_name: str, target_url: str, navigation_timeout_ms: int,
) -> Page:
    """Reuse this application's trusted, prefilled-login helpers for Thai pages."""
    helper = PlaywrightMonthlyReportPageActions
    expected = urlsplit(target_url)
    allowed_hosts = {"sellercenter.lazada.co.th", "sellercenter-th.lazada-seller.cn"}
    if expected.scheme != "https" or expected.hostname not in allowed_hosts:
        raise WorkflowError("广告采集目标不是受信任的泰国 Lazada 地址")
    gsp_page = helper._find_gsp_page(page)
    if gsp_page is not None:
        page = helper._ensure_gsp_home(gsp_page)
    goto_dom_ready(page, target_url, navigation_timeout_ms)
    deadline = time.monotonic() + 60
    submitted = False
    reopened = False
    register_login_opened = False
    while time.monotonic() < deadline:
        # SSO may complete in a new tab. Adopt only a page on the exact target
        # origin/path; do not borrow a different country's seller session.
        candidates = helper._context_pages(page)
        target_pages = [candidate for candidate in candidates if (
            urlsplit(str(candidate.url or "")).scheme == "https"
            and urlsplit(str(candidate.url or "")).hostname == expected.hostname
            and urlsplit(str(candidate.url or "")).path.rstrip("/") == expected.path.rstrip("/")
        )]
        if target_pages:
            page = target_pages[-1]
        actual = urlsplit(str(page.url or ""))
        if helper._is_gsp_home_url(page.url) or helper._is_gsp_login_url(page.url):
            page = helper._ensure_gsp_home(page)
            if reopened:
                raise LoginRequiredError(f"{shop_name} 跨境登录未进入目标数据页")
            goto_dom_ready(page, target_url, navigation_timeout_ms)
            reopened = True
            continue
        if actual.scheme != "https" or actual.hostname not in allowed_hosts:
            raise LoginRequiredError(f"{shop_name} 登录跳转到了非泰国 Lazada 站点")
        if helper._visible_login_challenge(page):
            raise LoginRequiredError(f"{shop_name} 需要人工完成验证码或安全验证")
        if helper._is_auth_page_url(page.url):
            if not helper._is_login_page_url(page.url):
                if not register_login_opened:
                    login_url = helper._trusted_register_login_url(page, "TH", target_url)
                    if login_url:
                        goto_dom_ready(page, login_url, navigation_timeout_ms)
                        register_login_opened = True
                        continue
                raise LoginRequiredError(f"{shop_name} Lazada 登录失效，请先在紫鸟中登录")
            if not submitted:
                submitted = helper._submit_prefilled_login(page)
            page.wait_for_timeout(500)
            continue
        if actual.hostname == expected.hostname and actual.path.rstrip("/") == expected.path.rstrip("/"):
            return page
        if submitted and not reopened:
            goto_dom_ready(page, target_url, navigation_timeout_ms)
            reopened = True
        page.wait_for_timeout(500)
    raise LoginRequiredError(f"{shop_name} 未能确认目标数据页，请检查 Lazada 登录状态")


class PlaywrightLazadaAdsPageActions:
    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger(__name__)

    def collect(self, page: Page, task: ShopTask, target_date: date) -> dict[str, Any]:
        token = _ACTIVE_LOGGER.set(self.logger)
        try:
            return self._collect(page, task, target_date)
        finally:
            _ACTIVE_LOGGER.reset(token)

    def _collect(self, page: Page, task: ShopTask, target_date: date) -> dict[str, Any]:
        values: dict[str, Any] = {"advertising": None, "performance": None, "errors": []}
        for metric, needed, collector, label in (
            ("performance", task.need_performance, collect_performance, "业绩"),
            ("advertising", task.need_advertising, collect_advertising, "广告费"),
        ):
            if not needed:
                continue
            try:
                values[metric] = collector(page, task, target_date, 90_000)
                if values[metric] is None:
                    values["errors"].append(f"{label}未返回有效金额，未写入")
            except Exception as exc:
                values["errors"].append(str(exc))
        return values

LOCAL_DASHBOARD_URL = "https://sellercenter.lazada.co.th/ba/dashboard"


CROSS_BORDER_DASHBOARD_URL = (
    "https://sellercenter-th.lazada-seller.cn/ba/dashboard"
)


LOCAL_AD_REPORT_URL = (
    "https://sellercenter.lazada.co.th/sponsor/solutions/ads/report/#!/"
)


CROSS_BORDER_AD_REPORT_URL = (
    "https://sellercenter-th.lazada-seller.cn/sponsor/solutions/ads/report/#!/"
)


class WorkflowError(RuntimeError):
    """可直接展示给操作人员的流程异常。"""


class LoginRequiredError(WorkflowError):
    """Lazada 登录失效或需要人工验证。"""


@dataclass(frozen=True)
class ShopTask:
    row: int
    shop_name: str
    owner: str = ""
    existing_advertising: Any = ""
    existing_performance: Any = ""
    need_advertising: bool = True
    need_performance: bool = True


def is_pending_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in {
            "",
            "-",
            "—",
            "–",
            "none",
            "null",
            "unde",
            "undefined",
        }
    return False


def decimal_text(value: Decimal | int | float) -> str:
    decimal_value = Decimal(str(value))
    if decimal_value == 0:
        return "0"
    rendered = format(decimal_value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def parse_amount(value: Any) -> Decimal:
    text = unicodedata.normalize("NFKC", str(value if value is not None else "")).strip()
    if not text or text in {"-", "—", "–"}:
        raise WorkflowError(f"金额为空：{value!r}")
    multiplier = Decimal("1")
    if re.search(r"\b[kK]\b|\d[kK](?:\D|$)", text):
        multiplier = Decimal("1000")
    elif re.search(r"\b[mM]\b|\d[mM](?:\D|$)", text):
        multiplier = Decimal("1000000")
    cleaned = (
        text.replace("฿", "")
        .replace("THB", "")
        .replace("thb", "")
        .replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(" ", "")
    )
    match = re.search(r"[-+−]?(?:\d[\d,.]*|[.,]\d+)", cleaned)
    if not match:
        raise WorkflowError(f"无法解析金额：{value!r}")
    number = match.group(0).replace("−", "-")
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        groups = number.lstrip("+-").split(",")
        if len(groups) > 1 and all(len(group) == 3 for group in groups[1:]):
            number = number.replace(",", "")
        else:
            number = number.replace(",", ".")
    try:
        return Decimal(number) * multiplier
    except InvalidOperation as exc:
        raise WorkflowError(f"无法解析金额：{value!r}") from exc


def values_equal(actual: Any, expected: Any) -> bool:
    try:
        return parse_amount(actual) == Decimal(str(expected))
    except Exception:
        return str(actual if actual is not None else "").strip() == str(expected if expected is not None else "").strip()


def page_body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5_000)
    except Exception:
        return ""


def goto_dom_ready(page: Page, url: str, timeout_ms: int) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        pass


def dismiss_lazada_popups(page: Page) -> None:
    patterns = (
        re.compile(r"^(?:知道了|我知道了|确定|确认|关闭|取消|稍后)$", re.I),
        re.compile(r"^(?:Got it|OK|Confirm|Close|Cancel|Later)$", re.I),
    )
    for pattern in patterns:
        try:
            locator = page.get_by_role("button", name=pattern)
            for index in range(min(locator.count(), 5)):
                button = locator.nth(index)
                if button.is_visible():
                    button.click(timeout=2_000)
                    page.wait_for_timeout(300)
        except Exception:
            continue


def dashboard_url(shop_name: str, target_date: date) -> str:
    base = (
        CROSS_BORDER_DASHBOARD_URL
        if "跨境" in shop_name
        else LOCAL_DASHBOARD_URL
    )
    day_text = target_date.isoformat()
    return f"{base}?dateRange={day_text}%7C{day_text}&dateType=day"


def ad_report_url(shop_name: str) -> str:
    return CROSS_BORDER_AD_REPORT_URL if "跨境" in shop_name else LOCAL_AD_REPORT_URL


def date_markers(target_date: date) -> tuple[str, ...]:
    return (
        target_date.isoformat(),
        target_date.strftime("%Y/%m/%d"),
        target_date.strftime("%d/%m/%Y"),
        target_date.strftime("%d-%m-%Y"),
        target_date.strftime("%d.%m.%Y"),
    )


def visible_page_and_input_text(page: Page) -> str:
    values: list[str] = [page_body_text(page)]
    try:
        inputs = page.locator("input:visible:not([type='password'])")
        for index in range(min(inputs.count(), 30)):
            value = inputs.nth(index).input_value(timeout=1_000)
            if value:
                values.append(value)
    except Exception:
        pass
    return "\n".join(values)


def verify_dashboard_date(page: Page, target_date: date) -> None:
    deadline = time.monotonic() + 30
    markers = date_markers(target_date)
    last_text = ""
    while time.monotonic() < deadline:
        last_text = visible_page_and_input_text(page)
        if any(re.search(
            re.escape(marker) + r"\s*(?:~|～|至|to|\||–|—|-)\s*" + re.escape(marker),
            last_text, re.I,
        ) for marker in markers):
            return
        page.wait_for_timeout(1_000)
    text_summary = re.sub(r"\s+", " ", last_text)[:200]
    raise WorkflowError(
        f"业绩页未确认目标日期 {target_date.isoformat()}，"
        f"页面文本摘要={text_summary!r}"
    )


def amount_from_labeled_text(
    text: str,
    label_pattern: re.Pattern[str],
) -> tuple[bool, Decimal | None]:
    normalized = unicodedata.normalize("NFKC", text or "")
    label_match = label_pattern.search(normalized)
    if not label_match:
        return False, None
    segment = normalized[label_match.start() : label_match.start() + 260]
    currency_match = re.search(
        r"(?:฿|THB)\s*([-+−]?(?:\d[\d,.]*|[.,]\d+)[ \t]*(?:[kKmM](?![A-Za-z]))?)",
        segment,
        re.I,
    )
    if currency_match:
        return True, parse_amount(currency_match.group(0))
    reverse_match = re.search(
        r"([-+−]?(?:\d[\d,.]*|[.,]\d+)[ \t]*(?:[kKmM](?![A-Za-z]))?)[ \t]*(?:฿|THB)",
        segment,
        re.I,
    )
    if reverse_match:
        return True, parse_amount(reverse_match.group(0))
    lines = [line.strip() for line in segment.splitlines() if line.strip()]
    for line in lines[1:6]:
        if line in {"-", "—", "–"}:
            return True, None
        if "%" in line or re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", line):
            continue
        if re.search(r"\d", line):
            try:
                return True, parse_amount(line)
            except WorkflowError:
                continue
    return False, None


REVENUE_LABEL = re.compile(r"(?:^|\b)(?:Revenue|营收|營收)(?:$|\b)", re.I)


SPEND_LABEL = re.compile(
    r"(?:Spend\s*[（(]?\s*THB\s*[）)]?|花费\s*[（(]\s*泰铢\s*[）)]|"
    r"花費\s*[（(]\s*泰銖\s*[）)])",
    re.I,
)


def extract_metric_near_visible_label(
    page: Page,
    label_pattern: re.Pattern[str],
) -> tuple[bool, Decimal | None]:
    try:
        labels = page.get_by_text(label_pattern)
        for index in range(min(labels.count(), 40)):
            label = labels.nth(index)
            if not label.is_visible():
                continue
            candidates = [label]
            candidates.extend(
                label.locator("xpath=" + "/".join([".."] * depth))
                for depth in range(1, 6)
            )
            for candidate in candidates:
                try:
                    text = candidate.inner_text(timeout=2_000).strip()
                except Exception:
                    continue
                found, value = amount_from_labeled_text(text, label_pattern)
                if found:
                    return True, value
    except Exception:
        pass
    return amount_from_labeled_text(page_body_text(page), label_pattern)


def extract_revenue(page: Page) -> Decimal | None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        # 同一页上方有今日“实时表现”，历史业绩只读取所选日期的“关键指标”。
        text = page_body_text(page)
        heading = re.search(r"关键指标|關鍵指標|Key\s*Metrics", text, re.I)
        if heading:
            found, value = amount_from_labeled_text(text[heading.end():], REVENUE_LABEL)
            if found:
                return value
        page.wait_for_timeout(750)
    raise WorkflowError("Lazada 业绩页未找到所选日期关键指标中的 Revenue/营收")


def extract_realtime_revenue(page: Page, target_date: date) -> Decimal | None:
    """今日历史指标尚不可用，限定到带今日更新时间的实时表现区域。"""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        text = page_body_text(page)
        heading = re.search(r"实时表现|實時表現|Real[\s-]*time\s*Performance", text, re.I)
        if heading:
            section = text[heading.end():]
            following_heading = re.search(
                r"关键指标|關鍵指標|Key\s*Metrics|店铺诊断|店鋪診斷|"
                r"Store\s*Diagnostic|实时排名|實時排名|Real[\s-]*time\s*Ranking",
                section, re.I,
            )
            if following_heading:
                section = section[:following_heading.start()]
            updated = re.search(
                r"(?:更新时间|更新時間|Last\s*updated(?:\s*at)?|Updated(?:\s*at)?)"
                r"[\s:：]*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
                section[:200], re.I,
            )
            if updated and parse_iso_date(updated.group(1)) == target_date:
                found, value = amount_from_labeled_text(section, REVENUE_LABEL)
                if found:
                    return value
        page.wait_for_timeout(750)
    raise WorkflowError(f"实时表现未提供 {target_date} 的有效营收")


def first_visible(page: Page, selectors: Iterable[str], timeout_ms: int = 2_000) -> Any | None:
    selector_list = tuple(selectors)
    deadline = time.monotonic() + timeout_ms / 1_000
    while time.monotonic() < deadline:
        for selector in selector_list:
            try:
                locator = page.locator(selector)
                for index in range(min(locator.count(), 20)):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        return candidate
            except Exception:
                continue
        page.wait_for_timeout(250)
    return None


def click_date_cell(page: Page, target_date: date) -> bool:
    iso_date = target_date.isoformat()
    selectors = (
        f"td[title='{iso_date}']:not(.rc-calendar-disabled-cell) .rc-calendar-date",
        f"td[title='{iso_date}']:not([aria-disabled='true'])",
        f"[data-date='{iso_date}']:not([aria-disabled='true'])",
        f"[data-value='{iso_date}']:not([aria-disabled='true'])",
        f"button[aria-label*='{iso_date}']",
    )
    candidate = first_visible(page, selectors, timeout_ms=5_000)
    if candidate is None:
        return False
    candidate.click(timeout=5_000, force=True)
    return True


def fill_visible_date_inputs(page: Page, target_date: date) -> bool:
    selectors = (
        ".rc-calendar-picker input:visible",
        "input[placeholder*='Start date']:visible",
        "input[placeholder*='End date']:visible",
        "input[placeholder*='开始日期']:visible",
        "input[placeholder*='结束日期']:visible",
        "input[placeholder*='日期']:visible",
    )
    inputs: list[Any] = []
    for selector in selectors:
        try:
            locator = page.locator(selector)
            for index in range(min(locator.count(), 4)):
                item = locator.nth(index)
                if item.is_visible() and all(item != existing for existing in inputs):
                    inputs.append(item)
        except Exception:
            continue
    if not inputs:
        return False
    value = target_date.isoformat()
    for item in inputs[:2]:
        try:
            item.fill(value)
        except Exception:
            try:
                item.click()
                item.press("Control+A")
                item.type(value)
            except Exception:
                return False
    try:
        inputs[min(len(inputs), 2) - 1].press("Enter")
    except Exception:
        pass
    return True


def click_apply_button(page: Page) -> None:
    patterns = (
        re.compile(r"^(?:Apply|Confirm|OK|Search)$", re.I),
        re.compile(r"^(?:应用|套用|确定|确认|查询|搜索)$"),
    )
    for pattern in patterns:
        try:
            buttons = page.get_by_role("button", name=pattern)
            for index in range(min(buttons.count(), 10)):
                button = buttons.nth(index)
                if button.is_visible() and button.is_enabled():
                    button.click(timeout=5_000)
                    return
        except Exception:
            continue


def select_ad_report_date(page: Page, target_date: date) -> None:
    triggers = (
        ".rc-calendar-picker:visible",
        "[class*='date-picker']:visible",
        "[class*='datePicker']:visible",
        "[class*='range-picker']:visible",
        "[class*='rangePicker']:visible",
        "input[placeholder*='Date']:visible",
        "input[placeholder*='日期']:visible",
    )
    trigger = first_visible(page, triggers, timeout_ms=20_000)
    if trigger is None:
        raise WorkflowError("广告报表未找到日期选择器")
    trigger.click(timeout=5_000)
    page.wait_for_timeout(500)

    selected = click_date_cell(page, target_date)
    if selected:
        page.wait_for_timeout(300)
        # rc-calendar-range 需要依次选择开始和结束日期；同日点击两次。
        click_date_cell(page, target_date)
    else:
        selected = fill_visible_date_inputs(page, target_date)
    if not selected:
        raise WorkflowError(
            f"广告报表日期面板未找到 {target_date.isoformat()}"
        )
    click_apply_button(page)
    page.wait_for_timeout(1_500)
    start = page.locator("input[placeholder*='Start date']:visible, input[placeholder*='开始日期']:visible").first
    end = page.locator("input[placeholder*='End date']:visible, input[placeholder*='结束日期']:visible").first
    if not start.count() or not end.count():
        raise WorkflowError("广告报表无法回验开始和结束日期")
    values = (start.input_value(), end.input_value())
    if values != (target_date.isoformat(), target_date.isoformat()):
        raise WorkflowError(f"广告日期回验不一致：目标 {target_date}，实际 {values}")
    try:
        page.locator(".ant-spin-spinning:visible").first.wait_for(state="hidden", timeout=15000)
    except PlaywrightTimeoutError as exc:
        raise WorkflowError("广告报表仍在加载，未读取金额") from exc


def extract_table_value_by_header(
    page: Page,
    label_pattern: re.Pattern[str],
) -> Decimal | None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            headers = page.get_by_text(label_pattern)
            for index in range(min(headers.count(), 40)):
                label = headers.nth(index)
                if not label.is_visible():
                    continue
                header = label.locator("xpath=ancestor::th[1]")
                if header.count() < 1:
                    continue
                column_index = header.evaluate(
                    "el => Array.from(el.parentElement.children).indexOf(el)"
                )
                table = header.locator("xpath=ancestor::table[1]")
                rows = table.locator("tbody tr")
                eligible = [
                    rows.nth(row_index) for row_index in range(rows.count())
                    if rows.nth(row_index).locator("td").count() > column_index
                ]
                totals = [
                    row for row in eligible if re.search(
                        r"(?:^|\s)(?:Total|Grand\s*Total|合计|总计|總計)(?:\s|$)",
                        row.inner_text(), re.I,
                    )
                ]
                if len(eligible) > 1 and not totals:
                    raise WorkflowError("广告报表有多条明细但没有总计行，不能用首行作为全店花费")
                for row in totals[:1] or eligible[:1]:
                    cells = row.locator("td")
                    if cells.count() <= column_index:
                        continue
                    text = cells.nth(column_index).inner_text(timeout=2_000).strip()
                    if text in {"-", "—", "–"}:
                        return None
                    try:
                        return parse_amount(text)
                    except WorkflowError:
                        continue
        except WorkflowError:
            raise
        except Exception:
            pass
        found, value = extract_metric_near_visible_label(page, label_pattern)
        if found:
            return value
        page.wait_for_timeout(750)
    raise WorkflowError("Lazada 广告报表未找到 Spend(THB)/花费(泰铢)指标")


def collect_performance(
    page: Page,
    task: ShopTask,
    target_date: date,
    navigation_timeout_ms: int,
) -> Decimal | None:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            LOGGER.info(f"{task.shop_name} 采集业绩，第 {attempt}/3 次")
            page = open_lazada_data_page(
                page, task.shop_name,
                dashboard_url(task.shop_name, target_date),
                navigation_timeout_ms,
            )
            dismiss_lazada_popups(page)
            if target_date == date.today():
                value = extract_realtime_revenue(page, target_date)
            else:
                verify_dashboard_date(page, target_date)
                value = extract_revenue(page)
            if value is None:
                LOGGER.warning(f"{task.shop_name} 当日业绩显示为 '-'，本次不写入")
            else:
                LOGGER.info(f"{task.shop_name} 当日业绩={decimal_text(value)}")
            return value
        except Exception as exc:
            last_error = exc
            LOGGER.warning(
                f"{task.shop_name} 业绩第 {attempt}/3 次采集失败：{exc}"
            )
            if isinstance(exc, LoginRequiredError):
                break
            if attempt < 3 and not page.is_closed():
                page.wait_for_timeout(attempt * 2_000)
    raise WorkflowError(f"业绩采集失败：{last_error}")


def collect_advertising(
    page: Page,
    task: ShopTask,
    target_date: date,
    navigation_timeout_ms: int,
) -> Decimal | None:
    if target_date == date.today():
        raise WorkflowError(
            f"Lazada 广告数据报告明确不支持今日数据（{target_date}），需次日补采"
        )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            LOGGER.info(f"{task.shop_name} 采集广告费，第 {attempt}/3 次")
            page = open_lazada_data_page(
                page, task.shop_name, ad_report_url(task.shop_name), navigation_timeout_ms,
            )
            dismiss_lazada_popups(page)
            select_ad_report_date(page, target_date)
            value = extract_table_value_by_header(page, SPEND_LABEL)
            if value is None:
                LOGGER.warning(f"{task.shop_name} 当日广告费显示为 '-'，本次不写入")
            else:
                LOGGER.info(f"{task.shop_name} 当日广告费={decimal_text(value)}")
            return value
        except Exception as exc:
            last_error = exc
            LOGGER.warning(
                f"{task.shop_name} 广告费第 {attempt}/3 次采集失败：{exc}"
            )
            if isinstance(exc, LoginRequiredError):
                break
            if attempt < 3 and not page.is_closed():
                page.wait_for_timeout(attempt * 2_000)
    raise WorkflowError(f"广告费采集失败：{last_error}")
