"""ZiNiao WebDriver gateway for Shopee Indonesia monthly My Tax Invoices."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlsplit

from backend.core.ziniao_browser import ZiniaoBrowserError
from backend.services.shopee_id_income_report import (
    INCOME_REPORT_FOLDER,
    INCOME_REPORT_LABEL,
    INCOME_REPORT_TYPE,
    MAX_HISTORY_PAGES,
    GENERATION_TIMEOUT,
    MY_REPORTS_INCOME_URL,
    export_monthly_income_report,
    validate_empty_income_evidence,
    _navigate_url,
)


INCOME_URL = "https://seller.shopee.co.id/portal/finance/income"
INVOICE_URL = "https://seller.shopee.co.id/portal/finance/income/invoice"
SHOPEE_ID_HOST = "seller.shopee.co.id"

REQUIRED_ACTIONS: tuple[str, ...] = ("master_invoice", "tax_invoice")
ACTION_LABELS: dict[str, str] = {
    "master_invoice": "Master Invoice",
    "tax_invoice": "Tax Invoice",
}
ACTION_FOLDERS: dict[str, str] = {
    "master_invoice": "Master_Invoice",
    "tax_invoice": "Tax_Invoice",
}
IGNORED_ACTION_LABELS: frozenset[str] = frozenset({
    "itemised billing",
    "itemized billing",
})

MAX_PDF_BYTES = 64 * 1024 * 1024
TEMP_DOWNLOAD_SUFFIXES = (".crdownload", ".tmp", ".part", ".download", ".partial")

# Explicit English and Indonesian month mapping (locale-independent).
MONTH_NAME_TO_NUMBER: dict[str, int] = {
    "january": 1,
    "jan": 1,
    "januari": 1,
    "february": 2,
    "feb": 2,
    "februari": 2,
    "march": 3,
    "mar": 3,
    "maret": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "mei": 5,
    "june": 6,
    "jun": 6,
    "juni": 6,
    "july": 7,
    "jul": 7,
    "juli": 7,
    "august": 8,
    "aug": 8,
    "agustus": 8,
    "agu": 8,
    "agt": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "oktober": 10,
    "okt": 10,
    "november": 11,
    "nov": 11,
    "nopember": 11,
    "nop": 11,
    "december": 12,
    "dec": 12,
    "desember": 12,
    "des": 12,
}

_MONTH_WORDS_PATTERN = "|".join(
    sorted((re.escape(word) for word in MONTH_NAME_TO_NUMBER), key=len, reverse=True)
)
_MONTH_YEAR_RE = re.compile(
    rf"(?<![A-Za-z])(?P<month>{_MONTH_WORDS_PATTERN})\.?(?:[\s,./-]+(?P<day>\d{{1,2}}))?[\s,./-]+(?P<year>20\d{{2}})(?!\d)",
    re.IGNORECASE,
)
_DAY_MONTH_YEAR_RE = re.compile(
    rf"(?<!\d)(?P<day>\d{{1,2}})[\s,./-]+(?P<month>{_MONTH_WORDS_PATTERN})\.?[\s,./-]+(?P<year>20\d{{2}})(?!\d)",
    re.IGNORECASE,
)
_YEAR_MONTH_WORD_RE = re.compile(
    rf"(?<!\d)(?P<year>20\d{{2}})[\s,./-]+(?P<month>{_MONTH_WORDS_PATTERN})\.?(?![A-Za-z])",
    re.IGNORECASE,
)
_ISO_YEAR_MONTH_RE = re.compile(
    r"(?<!\d)(?P<year>20\d{2})[-/](?P<month>0[1-9]|1[0-2])(?:[-/](?:0[1-9]|[12]\d|3[01]))?(?!\d)"
)
_DMY_NUMERIC_RE = re.compile(
    r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])[-/](?P<month>0[1-9]|1[0-2])[-/](?P<year>20\d{2})(?!\d)"
)
_EXACT_OPTION_MONTH_RE = re.compile(
    rf"^\s*(?P<month>{_MONTH_WORDS_PATTERN})\.?\s+(?P<year>20\d{{2}})\s*$",
    re.IGNORECASE,
)
_EXACT_ISO_MONTH_RE = re.compile(r"^\s*(?P<year>20\d{2})[-/](?P<month>0[1-9]|1[0-2])\s*$")


class InvoiceExportError(ValueError):
    """Export error preserving any validated PDFs already downloaded for the store."""

    def __init__(
        self,
        message: str,
        *,
        partial_files: list[dict[str, Any]] | None = None,
        partial_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.partial_files = list(partial_files or [])
        self.partial_rows = list(partial_rows or [])


def safe_slug(name: str, limit: int = 40) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "")).strip(" .")[:limit] or "item"


def parse_month_label(text: Any) -> str | None:
    """Parse a month selector or dropdown option label into YYYY-MM."""
    cleaned = " ".join(str(text or "").strip().split())
    if not cleaned:
        return None
    exact = _EXACT_OPTION_MONTH_RE.match(cleaned)
    if exact:
        month_num = MONTH_NAME_TO_NUMBER[exact.group("month").casefold()]
        year_num = int(exact.group("year"))
        return f"{year_num:04d}-{month_num:02d}"
    iso = _EXACT_ISO_MONTH_RE.match(cleaned)
    if iso:
        return f"{int(iso.group('year')):04d}-{int(iso.group('month')):02d}"
    months = extract_months_from_text(cleaned)
    return next(iter(months)) if len(months) == 1 else None


def extract_months_from_text(text: Any) -> set[str]:
    """Extract all YYYY-MM references from row descriptions, dates, or labels."""
    raw = " ".join(str(text or "").strip().split())
    if not raw:
        return set()
    found: set[str] = set()
    for pattern in (_DAY_MONTH_YEAR_RE, _MONTH_YEAR_RE, _YEAR_MONTH_WORD_RE):
        for match in pattern.finditer(raw):
            month_num = MONTH_NAME_TO_NUMBER.get(match.group("month").casefold())
            year_num = int(match.group("year"))
            if month_num:
                found.add(f"{year_num:04d}-{month_num:02d}")
    for match in _ISO_YEAR_MONTH_RE.finditer(raw):
        found.add(f"{int(match.group('year')):04d}-{int(match.group('month')):02d}")
    for match in _DMY_NUMERIC_RE.finditer(raw):
        found.add(f"{int(match.group('year')):04d}-{int(match.group('month')):02d}")
    return found


def validate_pdf(path: str | Path) -> dict[str, Any]:
    """Validate that a downloaded file is a complete non-empty PDF with %PDF- and %%EOF."""
    target = Path(path).resolve()
    if not target.is_file():
        raise ValueError("下载的税票 PDF 文件不存在")
    lower_name = target.name.casefold()
    if any(lower_name.endswith(suffix) for suffix in TEMP_DOWNLOAD_SUFFIXES):
        raise ValueError("下载文件仍为未完成的临时文件")
    if target.suffix.casefold() != ".pdf":
        raise ValueError(f"下载文件不是 PDF 格式：{target.name}")
    size = target.stat().st_size
    if size <= 0:
        raise ValueError("下载的税票 PDF 文件为空")
    if size > MAX_PDF_BYTES:
        raise ValueError("下载的税票 PDF 超过 64 MiB 大小限制")
    with target.open("rb") as stream:
        header = stream.read(16)
        if not header.startswith(b"%PDF-"):
            raise ValueError(f"下载文件缺少有效的 %PDF- 文件头：{target.name}")
        tail_offset = max(0, size - 2048)
        stream.seek(tail_offset)
        tail = stream.read(2048)
        if b"%%EOF" not in tail:
            raise ValueError(f"下载的 PDF 文件不完整，末尾缺少 %%EOF 结束标记：{target.name}")
        stream.seek(0)
        digest = hashlib.sha256()
        while chunk := stream.read(65536):
            digest.update(chunk)
    return {
        "path": str(target),
        "filename": target.name,
        "size": size,
        "sha256": digest.hexdigest(),
    }


def download_pdf(
    browser: Any,
    store_id: str,
    selector: str,
    download_dir: str | Path,
    *,
    timeout: float = 120,
    poll_interval: float = 0.2,
) -> dict[str, Any]:
    """Set dedicated CDP download folder, click once, and validate one new PDF."""
    directory = Path(download_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("本次税票下载目录非空，已拒绝混用旧文件")
    driver_getter = getattr(browser, "_driver", None)
    if not callable(driver_getter):
        raise ValueError("当前浏览器会话不支持 WebDriver 下载控制")
    driver = driver_getter(store_id)
    command = None
    for candidate in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
        try:
            driver.execute_cdp_cmd(
                candidate,
                {"behavior": "allow", "downloadPath": str(directory)},
            )
            command = candidate
            break
        except Exception:
            continue
    if command is None:
        raise ValueError("当前浏览器驱动不支持设置独立 PDF 下载目录")

    try:
        browser.click(store_id, selector)
        deadline = time.monotonic() + max(float(timeout), 0.0)
        last_completed_size: dict[Path, int] = {}
        while True:
            files = [path for path in directory.iterdir() if path.is_file()]
            if any(path.stat().st_size > MAX_PDF_BYTES for path in files):
                raise ValueError("下载的税票 PDF 超过 64 MiB 大小限制")
            pending = [
                path
                for path in files
                if any(path.name.casefold().endswith(suffix) for suffix in TEMP_DOWNLOAD_SUFFIXES)
            ]
            completed = [path for path in files if path not in pending]
            if len(completed) > 1:
                raise ValueError("单次点击产生了多个下载文件，无法确认唯一税票 PDF")
            if len(completed) == 1 and not pending:
                source = completed[0]
                if source.suffix.casefold() != ".pdf":
                    raise ValueError(f"下载文件不是 PDF 格式：{source.name}")
                current_size = source.stat().st_size
                try:
                    verified = validate_pdf(source)
                except ValueError:
                    if time.monotonic() >= deadline or last_completed_size.get(source) == current_size:
                        raise
                else:
                    if last_completed_size.get(source) == current_size:
                        return verified
                last_completed_size[source] = current_size
            if time.monotonic() >= deadline:
                raise ValueError("等待税票 PDF 下载完成超时")
            time.sleep(min(max(float(poll_interval), 0.01), max(0.0, deadline - time.monotonic())))
    finally:
        try:
            driver.execute_cdp_cmd(command, {"behavior": "default"})
        except Exception:
            pass


# Observe Shopee Indonesia My Tax Invoices DOM in a single read-only script.
# Only a boolean autofill state crosses the browser boundary; never return a password.
PAGE_STATE = r"""(() => {
  const visible = e => Boolean(
    e && e.getClientRects && e.getClientRects().length > 0 &&
    getComputedStyle(e).visibility !== 'hidden' &&
    getComputedStyle(e).display !== 'none'
  );
  const clean = v => String(v ?? '').replace(/\s+/g, ' ').trim();
  const isDisabled = e => Boolean(
    !e || e.matches(':disabled') ||
    e.getAttribute('aria-disabled') === 'true' ||
    e.classList.contains('is-disabled') ||
    e.classList.contains('eds-button--disabled') ||
    e.closest('[disabled],[aria-disabled="true"],.is-disabled')
  );
  const uniquePath = e => {
    if (!e || e.nodeType !== 1) return '';
    if (e.id) {
      const idSel = '#' + CSS.escape(e.id);
      if (document.querySelectorAll(idSel).length === 1) return idSel;
    }
    const parts = [];
    let cur = e;
    while (cur && cur.nodeType === 1) {
      const peers = cur.parentElement
        ? [...cur.parentElement.children].filter(x => x.tagName === cur.tagName)
        : [cur];
      const idx = peers.indexOf(cur) + 1;
      parts.unshift(cur.tagName.toLowerCase() + ':nth-of-type(' + idx + ')');
      const candidate = parts.join(' > ');
      if (document.querySelectorAll(candidate).length === 1 && document.querySelector(candidate) === e) {
        return candidate;
      }
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  };

  const passwordInputs = [...document.querySelectorAll('input[type="password"]')].filter(visible);
  const verifyButtons = [...document.querySelectorAll('button.eds-button--primary.action')].filter(
    e => visible(e) && /^(?:verify|verifikasi)$/i.test(clean(e.innerText || e.textContent))
  );
  let verifySelector = '';
  if (verifyButtons.length === 1) {
    if (document.querySelectorAll('button.eds-button--primary.action').length === 1) {
      verifySelector = 'button.eds-button--primary.action';
    } else {
      verifySelector = uniquePath(verifyButtons[0]);
    }
  }

  const challengeDialogs = [...document.querySelectorAll('[role="dialog"],.eds-modal,.eds-dialog,iframe[src*="captcha"]')].filter(visible);
  const challenge = challengeDialogs.some(e => {
    if (e.tagName === 'IFRAME') return true;
    if (e.querySelector('input[type="password"]')) return false;
    const text = clean(e.innerText || e.textContent);
    return /captcha|验证码|安全验证|滑块|otp|verification\s*code|kode\s*verifikasi|verifikasi\s*keamanan/i.test(text);
  });

  const moreButtons = [...document.querySelectorAll('[data-section="my_tax_invoice"] .more-btn')].filter(visible);
  let moreSelector = '';
  if (moreButtons.length === 1) {
    moreSelector = document.querySelectorAll('[data-section="my_tax_invoice"] .more-btn').length === 1
      ? '[data-section="my_tax_invoice"] .more-btn'
      : uniquePath(moreButtons[0]);
  }

  const selectors = [...document.querySelectorAll('.eds-select .eds-selector')].filter(visible);
  let monthSelector = '';
  let selectedMonthText = '';
  if (selectors.length === 1) {
    const inner = selectors[0].querySelector('.eds-selector__inner');
    selectedMonthText = clean((inner || selectors[0]).innerText || (inner || selectors[0]).textContent);
    monthSelector = document.querySelectorAll('.eds-select .eds-selector').length === 1
      ? '.eds-select .eds-selector'
      : uniquePath(selectors[0]);
  }

  const optionContainers = [...document.querySelectorAll('.eds-popper .eds-select__options, .eds-select__options')]
    .filter(e => visible(e) && e.querySelector('.eds-option'));
  const uniqueContainers = optionContainers.filter(
    e => !optionContainers.some(other => other !== e && other.contains(e))
  );
  let options = [];
  if (uniqueContainers.length === 1) {
    const container = uniqueContainers[0];
    const optionNodes = [...container.querySelectorAll(':scope > .eds-option')].filter(visible);
    options = optionNodes.map((node, i) => {
      const nth = i + 1;
      const directSel = `.eds-select__options > .eds-option:nth-child(${nth})`;
      const sel = (document.querySelectorAll(directSel).length === 1 && document.querySelector(directSel) === node)
        ? directSel
        : uniquePath(node);
      return {
        index: nth,
        text: clean(node.innerText || node.textContent),
        is_empty_month: node.classList.contains('is-empty-month'),
        disabled: isDisabled(node),
        selector: sel,
      };
    });
  }

  const edsTables = [...document.querySelectorAll('.eds-table')].filter(visible);
  const rawTables = [...document.querySelectorAll('table')].filter(visible);
  const tableCount = edsTables.length > 0 ? edsTables.length : (rawTables.length > 0 ? 1 : 0);
  const paginationCount = [...document.querySelectorAll('.eds-pagination,[class*="eds-pagination"]')].filter(visible).length;

  const rowNodes = [...document.querySelectorAll('tr.eds-table__row')].filter(visible);
  const rows = rowNodes.map((tr, idx) => {
    const rowIndex = idx + 1;
    const cells = [...tr.querySelectorAll(':scope > td')];
    const description = cells[0] ? clean(cells[0].innerText || cells[0].textContent) : '';
    const amount = cells[1] ? clean(cells[1].innerText || cells[1].textContent) : '';
    const dateText = cells[2] ? clean(cells[2].innerText || cells[2].textContent) : '';
    const downloadRow = cells[3] ? cells[3].querySelector('.download-row') : null;
    const popovers = downloadRow ? [...downloadRow.querySelectorAll(':scope > .eds-popover')] : [];
    const actions = [];
    popovers.forEach((pop, popIdx) => {
      const actionIndex = popIdx + 1;
      const btn = pop.querySelector('.eds-popover__ref > button, button');
      if (!btn || !visible(btn)) return;
      const exactSel = `tr.eds-table__row:nth-child(${rowIndex}) .download-row > .eds-popover:nth-child(${actionIndex}) button`;
      const selector = (document.querySelectorAll(exactSel).length === 1 && document.querySelector(exactSel) === btn)
        ? exactSel
        : uniquePath(btn);
      actions.push({
        action_index: actionIndex,
        text: clean(btn.innerText || btn.textContent),
        disabled: isDisabled(btn),
        selector,
      });
    });
    return {
      row_index: rowIndex,
      cell_count: cells.length,
      description,
      amount,
      date: dateText,
      download_row_present: Boolean(downloadRow),
      actions,
    };
  });

  const bodyText = clean(document.body ? (document.body.innerText || document.body.textContent) : '');
  const noReceiptFound = /\b(?:no\s+receipt\s+found|tidak\s+ada\s+faktur)\b/i.test(bodyText);
  const busy = [...document.querySelectorAll('.eds-loading,.eds-spin,.eds-table__loading,[aria-busy="true"],.loading-mask')].some(visible);
  const loginPage = /^\/(?:account\/(?:signin|login)|buyer\/login)(?:\/|$)/i.test(location.pathname || '');

  return JSON.stringify({
    url: location.href,
    title: document.title,
    login_page: loginPage,
    challenge,
    busy,
    more_button_selector: moreSelector,
    password_dialog: {
      password_visible: passwordInputs.length > 0,
      password_count: passwordInputs.length,
      password_filled: passwordInputs.length === 1 && Boolean(passwordInputs[0].value && passwordInputs[0].value.length > 0),
      verify_count: verifyButtons.length,
      verify_disabled: verifyButtons.length === 1 ? isDisabled(verifyButtons[0]) : true,
      verify_selector: verifySelector,
    },
    month_selector: {
      count: selectors.length,
      selector: monthSelector,
      selected_text: selectedMonthText,
    },
    month_options: {
      popper_count: uniqueContainers.length,
      options,
    },
    table_count: tableCount,
    pagination_count: paginationCount,
    no_receipt_found: noReceiptFound,
    rows,
  });
})()"""


class ShopeeIdInvoiceGateway:
    """WebDriver runtime adapter for Shopee Indonesia My Tax Invoices."""

    def __init__(
        self,
        profile: str,
        *,
        browser: Any,
        timeout: float = 45.0,
        download_timeout: float = 120.0,
        income_generation_timeout: float | None = None,
        max_history_pages: int = MAX_HISTORY_PAGES,
        progress: Any = None,
        autofill_timeout: float = 5.0,
        verify_wait_timeout: float = 8.0,
        max_verify_attempts: int = 3,
        poll_interval: float = 0.25,
    ) -> None:
        if browser is None or getattr(browser, "profile", None) != profile:
            raise ValueError("请使用本次所选紫鸟账号创建浏览器会话")
        self.browser = browser
        self.profile = profile
        self.timeout = timeout
        self.download_timeout = download_timeout
        self.income_generation_timeout = (
            float(income_generation_timeout)
            if income_generation_timeout is not None
            else GENERATION_TIMEOUT
        )
        self.progress = progress
        self.max_history_pages = max(int(max_history_pages), 1)
        self.autofill_timeout = autofill_timeout
        self.verify_wait_timeout = verify_wait_timeout
        self.max_verify_attempts = max(int(max_verify_attempts), 1)
        self.poll_interval = max(float(poll_interval), 0.01)
        self.current_stores: dict[str, dict[str, Any]] = {}

    @property
    def auth_failed(self) -> bool:
        return bool(getattr(self.browser, "auth_failed", False))

    @property
    def halted(self) -> bool:
        return self.auth_failed or getattr(self.browser, "cleanup_failed", False) is True

    @property
    def halt_reason(self) -> str:
        if getattr(self.browser, "cleanup_failed", False) is True:
            return "上一店铺未确认关闭，已暂停后续店铺，请关闭窗口后重试"
        return "紫鸟认证已失效，请恢复后重试"

    def preflight(self, stores: list[dict[str, Any]]) -> None:
        if all(store.get("status") in {"success", "empty_month"} for store in stores):
            return
        self.browser.ready()
        self.current_stores = {
            store["store_id"]: store for store in self.browser.list_stores()
        }

    def _raw_state(self, store_id: str, *, require_invoice_fields: bool = True) -> dict[str, Any]:
        result = self.browser.evaluate(store_id, PAGE_STATE)
        if not isinstance(result, dict):
            raise ValueError("无法识别 Shopee 印尼税票页面结构")
        if require_invoice_fields and ("month_selector" not in result or "rows" not in result):
            raise ValueError("无法识别 Shopee 印尼税票页面结构")
        if not require_invoice_fields and "password_dialog" not in result:
            raise ValueError("无法识别 Shopee 印尼密码验证弹窗结构")
        page_url = urlsplit(str(result.get("url") or ""))
        hostname = (page_url.hostname or "").casefold()
        if hostname == "accounts.shopee.co.id" and page_url.path.rstrip("/") == "/seller/login":
            raise ValueError("店铺 Shopee 登录已失效，请在紫鸟中重新登录该店铺后重试；其他店铺继续处理")
        if hostname != SHOPEE_ID_HOST:
            raise ValueError("当前页面不是 Shopee 印尼卖家中心（seller.shopee.co.id），已拒绝操作")
        if result.get("challenge"):
            raise ValueError("店铺触发人工验证或验证码（OTP/Captcha），请在紫鸟窗口完成验证后重试")
        if result.get("login_page") and not (result.get("password_dialog") or {}).get("password_visible"):
            raise ValueError("店铺未登录 Shopee 印尼卖家中心，请在紫鸟中完成登录后重试")
        return result

    def _complete_password_verification(self, store_id: str) -> bool:
        state = self._raw_state(store_id, require_invoice_fields=False)
        dialog = state.get("password_dialog") or {}
        if not dialog.get("password_visible"):
            return False

        # Wait briefly for ZiNiao autofill without reading the secret value.
        autofill_deadline = time.monotonic() + max(self.autofill_timeout, 0.0)
        while not dialog.get("password_filled"):
            if dialog.get("password_count", 0) > 1:
                raise ValueError("页面存在多个密码输入框，无法唯一确认 Shopee 密码验证弹窗")
            if time.monotonic() >= autofill_deadline:
                raise ValueError("Shopee 密码验证弹窗未检测到紫鸟自动填充密码，请检查账号自动填充或手动验证后重试")
            time.sleep(min(self.poll_interval, max(0.0, autofill_deadline - time.monotonic())))
            state = self._raw_state(store_id, require_invoice_fields=False)
            dialog = state.get("password_dialog") or {}
            if not dialog.get("password_visible"):
                return True

        for _attempt in range(self.max_verify_attempts):
            state = self._raw_state(store_id, require_invoice_fields=False)
            dialog = state.get("password_dialog") or {}
            if not dialog.get("password_visible"):
                return True
            if not dialog.get("password_filled"):
                raise ValueError("Shopee 密码验证弹窗密码未填充，无法自动点击 Verify")
            if (
                dialog.get("verify_count") != 1
                or dialog.get("verify_disabled")
                or not dialog.get("verify_selector")
            ):
                raise ValueError("无法唯一定位可用的 Shopee 密码验证 Verify 按钮")
            self.browser.click(store_id, dialog["verify_selector"])
            wait_deadline = time.monotonic() + max(self.verify_wait_timeout, 0.0)
            while True:
                state = self._raw_state(store_id, require_invoice_fields=False)
                dialog = state.get("password_dialog") or {}
                if not dialog.get("password_visible"):
                    return True
                if time.monotonic() >= wait_deadline:
                    break
                time.sleep(min(self.poll_interval, max(0.0, wait_deadline - time.monotonic())))

        raise ValueError("Shopee 密码验证弹窗在点击 Verify 后未关闭，请在紫鸟窗口手动验证后重试")

    def _ensure_invoice_page(self, store_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + max(self.timeout, 0.0)
        clicked_more = False
        while True:
            state = self._raw_state(store_id)
            dialog = state.get("password_dialog") or {}
            if dialog.get("password_visible"):
                self._complete_password_verification(store_id)
                state = self._raw_state(store_id)
                dialog = state.get("password_dialog") or {}
            path = urlsplit(str(state.get("url") or "")).path.rstrip("/")
            if (
                path == "/portal/finance/income"
                and not clicked_more
                and state.get("more_button_selector")
                and not state.get("busy")
            ):
                self.browser.click(store_id, state["more_button_selector"])
                clicked_more = True
                continue
            if (
                path == "/portal/finance/income/invoice"
                and not state.get("busy")
                and not dialog.get("password_visible")
            ):
                selector_count = (state.get("month_selector") or {}).get("count", 0)
                if selector_count == 1:
                    return state
                if selector_count > 1:
                    raise ValueError("税票页面存在多个月份下拉选择器，无法唯一确认")
            if time.monotonic() >= deadline:
                raise ValueError("等待 Shopee 印尼 My Tax Invoices 页面就绪超时")
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _validate_layout_guards(state: dict[str, Any]) -> None:
        if (state.get("month_selector") or {}).get("count") != 1:
            raise ValueError("税票页面月份选择器不唯一或缺失")
        if int(state.get("table_count") or 0) > 1:
            raise ValueError("税票页面存在多个数据表格，无法唯一确认下载范围")
        if int(state.get("pagination_count") or 0) > 0:
            raise ValueError("检测到分页控件（.eds-pagination），当前不支持分页税票列表，已停止以防遗漏数据")

    @staticmethod
    def _validate_row_actions_and_month(row: dict[str, Any], target_month: str) -> dict[str, dict[str, Any]]:
        row_index = row.get("row_index")
        if int(row.get("cell_count") or 0) < 4 or not row.get("download_row_present"):
            raise ValueError(f"第 {row_index} 行结构异常，缺少描述、日期或下载操作区")
        description = str(row.get("description") or "").strip()
        date_text = str(row.get("date") or "").strip()
        if not description and not date_text:
            raise ValueError(f"第 {row_index} 行缺少描述和日期信息")
        row_months = extract_months_from_text(f"{description} {date_text}")
        if row_months != {target_month}:
            found_text = "、".join(sorted(row_months)) if row_months else "无法识别月份"
            raise ValueError(
                f"第 {row_index} 行（{description or date_text}）月份为 {found_text}，与所选 {target_month} 不一致"
            )
        by_type: dict[str, list[dict[str, Any]]] = {"master_invoice": [], "tax_invoice": []}
        for action in row.get("actions") or []:
            label = str(action.get("text") or "").strip()
            lower = label.casefold()
            if lower in IGNORED_ACTION_LABELS:
                continue
            if lower == "master invoice":
                by_type["master_invoice"].append(action)
            elif lower == "tax invoice":
                by_type["tax_invoice"].append(action)
            else:
                raise ValueError(f"第 {row_index} 行出现未识别的下载按钮：{label or '空'}")
        selected_actions: dict[str, dict[str, Any]] = {}
        for key, expected_label in ACTION_LABELS.items():
            matches = by_type[key]
            if key == "tax_invoice" and not matches:
                continue
            if len(matches) != 1:
                raise ValueError(
                    f"第 {row_index} 行（{description}）缺少或存在重复的 {expected_label} 下载按钮"
                )
            candidate = matches[0]
            if candidate.get("disabled") or not candidate.get("selector"):
                raise ValueError(
                    f"第 {row_index} 行（{description}）的 {expected_label} 按钮不可操作"
                )
            selected_actions[key] = candidate
        return selected_actions

    @staticmethod
    def _settled_snapshot_signature(state: dict[str, Any]) -> tuple[Any, ...]:
        selected_text = (state.get("month_selector") or {}).get("selected_text", "")
        no_receipt = bool(state.get("no_receipt_found"))
        rows_sig = tuple(
            (
                row.get("row_index"),
                row.get("description"),
                row.get("amount"),
                row.get("date"),
                tuple((a.get("action_index"), a.get("text"), a.get("disabled")) for a in (row.get("actions") or [])),
            )
            for row in (state.get("rows") or [])
        )
        return (selected_text, no_receipt, rows_sig)

    def _wait_settled_month_state(
        self, store_id: str, target_month: str, *, allow_empty: bool = False
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(self.timeout, 0.0)
        previous_signature: tuple[Any, ...] | None = None
        last_issue = f"等待 {target_month} 税票列表加载稳定超时"
        while True:
            state = self._raw_state(store_id)
            dialog = state.get("password_dialog") or {}
            if dialog.get("password_visible"):
                self._complete_password_verification(store_id)
                previous_signature = None
                continue
            self._validate_layout_guards(state)
            selected_text = (state.get("month_selector") or {}).get("selected_text", "")
            selected_month = parse_month_label(selected_text)
            popper_count = (state.get("month_options") or {}).get("popper_count", 0)
            rows = state.get("rows") or []
            no_receipt = bool(state.get("no_receipt_found"))

            if state.get("busy") or popper_count > 0:
                previous_signature = None
                last_issue = f"月份 {target_month} 页面仍在加载中"
            elif selected_month != target_month:
                previous_signature = None
                last_issue = f"月份选择器当前为 {selected_text or '空'}，尚未切换为 {target_month}"
            elif no_receipt and rows:
                previous_signature = None
                last_issue = "页面同时出现数据行与 No receipt found 空状态，列表尚未稳定"
            elif no_receipt and not allow_empty:
                # Switching away from an empty month can leave its placeholder
                # visible while the new request is pending, without a spinner.
                previous_signature = None
                last_issue = "所选月份标记有账单，但页面仍显示无账单；等待新月份数据加载"
            elif not no_receipt and not rows:
                previous_signature = None
                last_issue = "页面未返回税票数据行，也未确认 No receipt found 空状态"
            else:
                # If rows are present, check whether they already belong to target_month.
                # During transition from an initial month (e.g. November 2026 -> October 2026),
                # old rows may briefly remain mounted; do not accept until rows match target_month.
                row_months_ok = True
                for row in rows:
                    row_months = extract_months_from_text(
                        f"{row.get('description') or ''} {row.get('date') or ''}"
                    )
                    if row_months != {target_month}:
                        row_months_ok = False
                        found_text = "、".join(sorted(row_months)) if row_months else "未识别月份"
                        last_issue = (
                            f"第 {row.get('row_index')} 行（{row.get('description') or row.get('date')}）"
                            f"月份为 {found_text}，与所选 {target_month} 不一致"
                        )
                        break
                if row_months_ok:
                    signature = self._settled_snapshot_signature(state)
                    if signature == previous_signature:
                        return state
                    previous_signature = signature
                else:
                    previous_signature = None

            if time.monotonic() >= deadline:
                raise ValueError(last_issue)
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))

    def _select_month(self, store_id: str, target_month: str) -> tuple[dict[str, Any], dict[str, Any]]:
        state = self._ensure_invoice_page(store_id)
        self._validate_layout_guards(state)
        selector_info = state["month_selector"]
        self.browser.click(store_id, selector_info["selector"])

        deadline = time.monotonic() + max(self.timeout, 0.0)
        options: list[dict[str, Any]] = []
        while True:
            state = self._raw_state(store_id)
            month_options = state.get("month_options") or {}
            popper_count = int(month_options.get("popper_count") or 0)
            if popper_count > 1:
                raise ValueError("页面出现多个月份下拉选项浮层（.eds-popper），无法唯一确认")
            if popper_count == 1 and month_options.get("options"):
                options = list(month_options["options"])
                break
            if time.monotonic() >= deadline:
                raise ValueError("点击月份选择器后未弹出月份选项列表（.eds-popper）")
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))

        matched_options = [
            opt for opt in options if parse_month_label(opt.get("text")) == target_month
        ]
        if not matched_options:
            available = [str(opt.get("text") or "").strip() for opt in options if str(opt.get("text") or "").strip()]
            available_hint = f"（当前可选：{'、'.join(available[:12])}）" if available else ""
            raise ValueError(f"页面月份下拉列表中未提供 {target_month}{available_hint}，该月份不可用")
        if len(matched_options) > 1:
            raise ValueError(f"页面月份下拉列表中存在多个 {target_month} 选项，无法唯一确认")

        chosen = matched_options[0]
        if chosen.get("disabled") or not chosen.get("selector"):
            raise ValueError(f"月份 {target_month} 选项已禁用或不可点击")

        self.browser.click(store_id, chosen["selector"])
        settled_state = self._wait_settled_month_state(
            store_id, target_month, allow_empty=bool(chosen.get("is_empty_month"))
        )
        return settled_state, chosen

    def _revalidate_before_click(
        self,
        store_id: str,
        target_month: str,
        expected_row_count: int,
        expected_row: dict[str, Any],
        action_key: str,
    ) -> str:
        state = self._raw_state(store_id)
        dialog = state.get("password_dialog") or {}
        if dialog.get("password_visible"):
            self._complete_password_verification(store_id)
            state = self._raw_state(store_id)
        if state.get("busy"):
            state = self._wait_settled_month_state(store_id, target_month)
        self._validate_layout_guards(state)
        selected_text = (state.get("month_selector") or {}).get("selected_text", "")
        if parse_month_label(selected_text) != target_month:
            raise ValueError(f"下载前复核失败：页面当前月份变为 {selected_text or '空'}，与 {target_month} 不一致")
        if state.get("no_receipt_found"):
            raise ValueError("下载前复核失败：页面状态变为 No receipt found")
        live_rows = state.get("rows") or []
        if len(live_rows) != expected_row_count:
            raise ValueError("下载前复核失败：税票表格行数发生变化")
        row_index = int(expected_row["row_index"])
        live_row = next((r for r in live_rows if int(r.get("row_index") or 0) == row_index), None)
        if live_row is None:
            raise ValueError(f"下载前复核失败：未找到第 {row_index} 行")
        if (
            live_row.get("description") != expected_row.get("description")
            or live_row.get("date") != expected_row.get("date")
        ):
            raise ValueError(f"下载前复核失败：第 {row_index} 行的描述或日期已发生变化")
        validated_actions = self._validate_row_actions_and_month(live_row, target_month)
        planned_actions = self._validate_row_actions_and_month(expected_row, target_month)
        if set(validated_actions) != set(planned_actions):
            raise ValueError(f"下载前复核失败：第 {row_index} 行可用文件类型发生变化")
        live_action = validated_actions[action_key]
        expected_label = ACTION_LABELS[action_key]
        if str(live_action.get("text") or "").strip().casefold() != expected_label.casefold():
            raise ValueError(f"下载前复核失败：第 {row_index} 行按钮文本不是 {expected_label}")
        return str(live_action["selector"])

    def export(self, store: dict[str, Any], month: str, store_dir: str | Path) -> dict[str, Any]:
        result = None
        failure = None
        try:
            result = self._export(store, month, Path(store_dir).resolve())
        except Exception as exc:
            failure = exc
        finally:
            if getattr(self.browser, "cleanup_failed", False) is not True:
                close = getattr(self.browser, "close_store", None)
                if callable(close):
                    try:
                        close(store["store_id"])
                    except Exception as exc:
                        raise InvoiceExportError(
                            f"店铺关闭未确认：{exc}",
                            partial_files=(result or {}).get("files") or getattr(failure, "partial_files", []),
                            partial_rows=(result or {}).get("rows") or getattr(failure, "partial_rows", []),
                        ) from None
        if failure is not None:
            raise failure
        return result

    def _navigate_to_invoice_page(self, store_id: str) -> None:
        _navigate_url(self.browser, store_id, INVOICE_URL)

    def _export(self, store: dict[str, Any], month: str, store_dir: Path) -> dict[str, Any]:
        store_id = str(store.get("store_id") or "").strip()
        actual = self.current_stores.get(store_id)
        if (
            not actual
            or actual.get("store_name") != store.get("store_name")
            or actual.get("platform_name") != store.get("platform_name")
            or not any(
                token in str(actual.get("platform_name") or "").casefold()
                for token in ("shopee", "虾皮")
            )
        ):
            raise ValueError("该店的紫鸟权限、名称或 Shopee 平台信息发生变化，请重新匹配")

        self.browser.open_store(store, url=MY_REPORTS_INCOME_URL)

        downloaded_files: list[dict[str, Any]] = []
        row_records: list[dict[str, Any]] = []

        try:
            income_dir = store_dir / INCOME_REPORT_FOLDER
            income_info = export_monthly_income_report(
                self.browser,
                store_id,
                month,
                income_dir,
                timeout=self.timeout,
                generation_timeout=self.income_generation_timeout,
                download_timeout=self.download_timeout,
                poll_interval=self.poll_interval,
                max_history_pages=self.max_history_pages,
                verify_password=lambda: self._complete_password_verification(store_id),
                progress=self.progress,
            )
            income_empty = income_info.get("no_data") is True
            if income_empty and not validate_empty_income_evidence(income_info.get("empty_evidence"), month):
                raise ValueError("收入报表无数据证据无效，不能跳过下载")
            income_summary = {
                "filename": income_info.get("filename", ""),
                "period_start": income_info["period_start"], "period_end": income_info["period_end"],
                "source": income_info["source"], "no_data": income_empty,
                "currency": income_info.get("currency", "Rp"),
                "history_page": income_info.get("history_page"),
            }
            income_evidence = {
                "income_report_count": 0 if income_empty else 1,
                "income_report_source": income_info["source"],
                "income_report_period": f"{income_info['period_start']}_{income_info['period_end']}",
            }
            if income_empty:
                income_evidence["income_no_data"] = income_info["empty_evidence"]
            else:
                income_entry = {
                    "row_index": 0,
                    "description": f"Income Report ({income_info['period_start']} to {income_info['period_end']})",
                    "amount": "",
                    "date": month,
                    "invoice_type": INCOME_REPORT_TYPE,
                    "invoice_label": INCOME_REPORT_LABEL,
                    "report_period": f"{income_info['period_start']}_{income_info['period_end']}",
                    "period_start": income_info["period_start"],
                    "period_end": income_info["period_end"],
                    "currency": income_info.get("currency", "Rp"),
                    "report_source": income_info["source"],
                    "filename": income_info["filename"],
                    "path": income_info["path"],
                    "size": income_info["size"],
                    "sha256": income_info["sha256"],
                }
                downloaded_files.append(income_entry)

            self._navigate_to_invoice_page(store_id)
            settled_state, chosen_option = self._select_month(store_id, month)
            selected_text = (settled_state.get("month_selector") or {}).get("selected_text", "")

            if settled_state.get("no_receipt_found") and not settled_state.get("rows"):
                return {
                    "no_data": True,
                    "month": month,
                    "selected_month_text": selected_text,
                    "row_count": 0,
                    "file_count": len(downloaded_files),
                    "income_report_count": 0 if income_empty else 1,
                    "master_invoice_count": 0,
                    "tax_invoice_count": 0,
                    "files": downloaded_files,
                    "rows": [],
                    "income_report": income_summary,
                    "evidence": {
                        "selected_month": selected_text,
                        "empty_text": "No receipt found",
                        "is_empty_month_option": bool(chosen_option.get("is_empty_month")),
                        **income_evidence,
                    },
                }

            rows = settled_state.get("rows") or []
            if not rows:
                raise ValueError("税票页面未显示数据行且未确认 No receipt found 空状态")

            # Missing Tax Invoice is an observed platform availability state, not a failed click.
            unavailable_documents = []
            for row in rows:
                actions = self._validate_row_actions_and_month(row, month)
                if "tax_invoice" not in actions:
                    unavailable_documents.append({
                        "row_index": int(row["row_index"]),
                        "invoice_type": "tax_invoice",
                        "reason": "not_provided",
                        "observed_actions": [str(a.get("text") or "").strip() for a in row["actions"]],
                    })

            expected_row_count = len(rows)
            for row in rows:
                row_index = int(row["row_index"])
                row_files: dict[str, str] = {}
                for action_key in self._validate_row_actions_and_month(row, month):
                    live_selector = self._revalidate_before_click(
                        store_id, month, expected_row_count, row, action_key
                    )
                    action_folder = ACTION_FOLDERS[action_key]
                    target_dir = store_dir / action_folder / f"row_{row_index:02d}"
                    file_info = download_pdf(
                        self.browser,
                        store_id,
                        live_selector,
                        target_dir,
                        timeout=self.download_timeout,
                        poll_interval=self.poll_interval,
                    )
                    entry = {
                        "row_index": row_index,
                        "description": row.get("description", ""),
                        "amount": row.get("amount", ""),
                        "date": row.get("date", ""),
                        "invoice_type": action_key,
                        "invoice_label": ACTION_LABELS[action_key],
                        "filename": file_info["filename"],
                        "path": file_info["path"],
                        "size": file_info["size"],
                        "sha256": file_info["sha256"],
                    }
                    downloaded_files.append(entry)
                    row_files[action_key] = file_info["path"]
                row_records.append({
                    "row_index": row_index,
                    "description": row.get("description", ""),
                    "amount": row.get("amount", ""),
                    "date": row.get("date", ""),
                    "master_invoice_file": row_files["master_invoice"],
                    "tax_invoice_file": row_files.get("tax_invoice", ""),
                })
        except InvoiceExportError:
            raise
        except ZiniaoBrowserError as exc:
            error = InvoiceExportError(
                str(exc),
                partial_files=downloaded_files,
                partial_rows=row_records,
            )
            error.__cause__ = exc
            raise error from None
        except Exception as exc:
            raise InvoiceExportError(
                str(exc),
                partial_files=downloaded_files,
                partial_rows=row_records,
            ) from exc

        income_count = sum(1 for item in downloaded_files if item["invoice_type"] == INCOME_REPORT_TYPE)
        master_count = sum(1 for item in downloaded_files if item["invoice_type"] == "master_invoice")
        tax_count = sum(1 for item in downloaded_files if item["invoice_type"] == "tax_invoice")
        if income_count != (0 if income_empty else 1) or master_count != expected_row_count or tax_count != expected_row_count - len(unavailable_documents):
            raise InvoiceExportError(
                f"下载数量不完整：收入报表 {income_count} 份，税票共 {expected_row_count} 行（Master Invoice {master_count} 份，Tax Invoice {tax_count} 份）",
                partial_files=downloaded_files,
                partial_rows=row_records,
            )

        return {
            "no_data": False,
            "month": month,
            "selected_month_text": selected_text,
            "row_count": expected_row_count,
            "file_count": len(downloaded_files),
            "income_report_count": income_count,
            "master_invoice_count": master_count,
            "tax_invoice_count": tax_count,
            "files": downloaded_files,
            "rows": row_records,
            "income_report": income_summary,
            "evidence": {
                "unavailable_documents": unavailable_documents,
                "selected_month": selected_text,
                "row_count": expected_row_count,
                **income_evidence,
                "master_invoice_count": master_count,
                "tax_invoice_count": tax_count,
            },
        }
