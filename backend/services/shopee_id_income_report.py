"""ZiNiao WebDriver helper for Shopee Indonesia monthly Released Income Report XLSX."""
from __future__ import annotations

import calendar
from datetime import date, datetime
import hashlib
from pathlib import Path
import re
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit
import zipfile

from openpyxl import load_workbook


SHOPEE_ID_HOST = "seller.shopee.co.id"
INCOME_URL = "https://seller.shopee.co.id/portal/finance/income"
MY_REPORTS_QUERY_URL = "https://seller.shopee.co.id/portal/settings/shop/reports?type=ps_reports_income"
MY_REPORTS_INCOME_URL = "https://seller.shopee.co.id/portal/settings/shop/reports/income"

INCOME_REPORT_TYPE = "income_report"
INCOME_REPORT_LABEL = "Income Report"
INCOME_REPORT_FOLDER = "Income_Report"

REQUIRED_WORKBOOK_SHEETS: tuple[str, ...] = ("Summary", "Adjustment")
EXPECTED_HISTORY_HEADERS: tuple[str, ...] = (
    "report type",
    "request time",
    "request account",
    "report name",
    "action",
)

MAX_XLSX_BYTES = 64 * 1024 * 1024
MAX_UNCOMPRESSED_ZIP_BYTES = 256 * 1024 * 1024
MAX_HISTORY_PAGES = 100
GENERATION_TIMEOUT = 30 * 60.0
TEMP_DOWNLOAD_SUFFIXES = (".crdownload", ".tmp", ".part", ".download", ".partial")

_INCOME_FILENAME_RE = re.compile(
    r"^Income\.(?P<report_type>[A-Za-z0-9_-]+)\.(?P<country>[A-Za-z0-9_-]+)\.(?P<start_ymd>\d{8})_(?P<end_ymd>\d{8})\.xlsx$"
)


def month_date_bounds(month: str) -> dict[str, Any]:
    """Compute exact full-calendar-month bounds and Shopee Indonesia Released Income URL."""
    text = str(month or "").strip()
    match = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", text)
    if not match:
        raise ValueError(f"账单月份格式必须为 YYYY-MM：{text or '空'}")
    year, month_num = int(match.group(1)), int(match.group(2))
    if year < 2000 or year > 2100:
        raise ValueError(f"账单年份超出有效范围：{text}")
    last_day = calendar.monthrange(year, month_num)[1]
    start_dt = date(year, month_num, 1)
    end_dt = date(year, month_num, last_day)
    start_ymd = start_dt.strftime("%Y%m%d")
    end_ymd = end_dt.strftime("%Y%m%d")
    date_range_param = f"{start_ymd}{end_ymd}"
    expected_filename = f"Income.released.id.{start_ymd}_{end_ymd}.xlsx"
    return {
        "month": f"{year:04d}-{month_num:02d}",
        "year": year,
        "month_num": month_num,
        "last_day": last_day,
        "start_date": start_dt,
        "end_date": end_dt,
        "start_iso": start_dt.isoformat(),
        "end_iso": end_dt.isoformat(),
        "start_ymd": start_ymd,
        "end_ymd": end_ymd,
        "date_range_param": date_range_param,
        "display_dmy_range": f"{start_dt.strftime('%d/%m/%Y')} - {end_dt.strftime('%d/%m/%Y')}",
        "expected_filename": expected_filename,
        "income_url": f"{INCOME_URL}?type=2&dateRange={date_range_param}",
    }


def parse_income_report_filename(filename: Any) -> dict[str, Any] | None:
    """Parse an Income.<type>.<country>.<YYYYMMDD>_<YYYYMMDD>.xlsx report name."""
    cleaned = str(filename or "").strip()
    match = _INCOME_FILENAME_RE.fullmatch(cleaned)
    if not match:
        return None
    report_type = match.group("report_type")
    country = match.group("country")
    start_ymd = match.group("start_ymd")
    end_ymd = match.group("end_ymd")
    try:
        start_dt = datetime.strptime(start_ymd, "%Y%m%d").date()
        end_dt = datetime.strptime(end_ymd, "%Y%m%d").date()
    except ValueError:
        return None
    if start_dt > end_dt:
        return None
    last_day = calendar.monthrange(start_dt.year, start_dt.month)[1]
    is_full_month = (
        start_dt.day == 1
        and end_dt.year == start_dt.year
        and end_dt.month == start_dt.month
        and end_dt.day == last_day
    )
    month = f"{start_dt.year:04d}-{start_dt.month:02d}" if is_full_month else None
    return {
        "filename": cleaned,
        "report_type": report_type,
        "country": country,
        "start_ymd": start_ymd,
        "end_ymd": end_ymd,
        "start_iso": start_dt.isoformat(),
        "end_iso": end_dt.isoformat(),
        "is_full_month": is_full_month,
        "month": month,
        "is_released_id": report_type == "released" and country == "id",
    }


def is_matching_monthly_income_filename(filename: Any, target_month: str) -> bool:
    """Return True only if filename is the exact Released Indonesia full-month report for target_month."""
    bounds = month_date_bounds(target_month)
    parsed = parse_income_report_filename(filename)
    if not parsed:
        return False
    return (
        parsed["is_released_id"]
        and parsed["is_full_month"]
        and parsed["month"] == bounds["month"]
        and parsed["filename"] == bounds["expected_filename"]
    )


def _clean_cell_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def _normalize_cell_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _clean_cell_text(value)
    if not text:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?", text):
        return text.split()[0]
    return text


def _read_sheet_grid(ws: Any, *, max_rows: int = 15, max_cols: int = 10) -> dict[tuple[int, int], Any]:
    if hasattr(ws, "reset_dimensions"):
        ws.reset_dimensions()
    grid: dict[tuple[int, int], Any] = {}
    for row_idx, row in enumerate(
        ws.iter_rows(min_row=1, max_row=max_rows, min_col=1, max_col=max_cols, values_only=True),
        1,
    ):
        for col_idx, val in enumerate(row, 1):
            if val is not None:
                grid[(row_idx, col_idx)] = val
    return grid


def _workbook_sheet_family(names: tuple[str, ...], base: str) -> list[str]:
    """Shopee splits larger reports into consecutively numbered worksheets."""
    numbered = {}
    for name in names:
        match = re.fullmatch(re.escape(base) + r" - ([1-9]\d*)", name)
        if match:
            numbered[int(match[1])] = name
    if base in names:
        if numbered:
            raise ValueError(f"收入报表同时包含 {base} 和编号分表，无法确认完整性")
        return [base]
    if not numbered or set(numbered) != set(range(1, len(numbered) + 1)):
        raise ValueError(f"收入报表缺少 {base} 工作表或编号分表不连续")
    return [numbered[i] for i in sorted(numbered)]


def validate_income_xlsx(path: str | Path, target_month: str) -> dict[str, Any]:
    """Validate that a downloaded file is a complete Shopee ID Released monthly income XLSX."""
    bounds = month_date_bounds(target_month)
    target = Path(path).resolve()
    if not target.is_file():
        raise ValueError("下载的月度收入报表 XLSX 文件不存在")
    lower_name = target.name.casefold()
    if any(lower_name.endswith(suffix) for suffix in TEMP_DOWNLOAD_SUFFIXES):
        raise ValueError("下载文件仍为未完成的临时文件")
    if target.suffix.casefold() != ".xlsx":
        raise ValueError(f"下载文件不是 XLSX 格式：{target.name}")
    size = target.stat().st_size
    if size <= 0:
        raise ValueError("下载的月度收入报表 XLSX 文件为空")
    if size > MAX_XLSX_BYTES:
        raise ValueError("下载的月度收入报表 XLSX 超过 64 MiB 大小限制")

    with target.open("rb") as stream:
        head = stream.read(512)
        stripped_head = head.lstrip()
        if (
            stripped_head.lower().startswith((b"<!doctype", b"<html", b"<?xml"))
            or b"<html" in stripped_head.lower()
        ):
            raise ValueError(f"下载文件为 HTML/网页内容而非 Excel 工作簿：{target.name}")
        if not head.startswith(b"PK\x03\x04"):
            raise ValueError(f"下载文件缺少有效的 XLSX (ZIP) 文件头：{target.name}")
        stream.seek(0)
        digest = hashlib.sha256()
        while chunk := stream.read(65536):
            digest.update(chunk)

    parsed_name = parse_income_report_filename(target.name)
    if parsed_name is None:
        raise ValueError(
            f"收入报表文件名格式无效（应为 {bounds['expected_filename']}）：{target.name}"
        )
    if parsed_name["report_type"] != "released":
        raise ValueError(
            f"收入报表类型不是 Released（当前为 {parsed_name['report_type']}）：{target.name}"
        )
    if parsed_name["country"] != "id":
        raise ValueError(
            f"收入报表站点不是印尼 id（当前为 {parsed_name['country']}）：{target.name}"
        )
    if not parsed_name["is_full_month"]:
        raise ValueError(
            f"收入报表文件名日期区间不是完整自然月（{parsed_name['start_iso']} 至 {parsed_name['end_iso']}）：{target.name}"
        )
    if parsed_name["month"] != bounds["month"] or target.name != bounds["expected_filename"]:
        raise ValueError(
            f"收入报表文件名月份（{parsed_name['month']}）与所选月份 {bounds['month']} 不一致：{target.name}"
        )

    try:
        with zipfile.ZipFile(target, "r") as archive:
            infos = archive.infolist()
            names = {info.filename for info in infos}
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise ValueError(f"收入报表 XLSX 缺少核心工作簿结构：{target.name}")
            if not any(name.startswith("xl/worksheets/") and name.endswith(".xml") for name in names):
                raise ValueError(f"收入报表 XLSX 缺少工作表数据：{target.name}")
            total_uncompressed = sum(max(int(info.file_size or 0), 0) for info in infos)
            if total_uncompressed > MAX_UNCOMPRESSED_ZIP_BYTES:
                raise ValueError("收入报表 XLSX 解压后大小超出安全限制")
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ValueError(f"收入报表 XLSX 压缩包 CRC 校验失败：{bad_member}")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"收入报表 XLSX 压缩包损坏或不完整：{target.name}") from exc

    workbook = None
    try:
        workbook = load_workbook(target, read_only=True, data_only=True)
        sheet_names = tuple(workbook.sheetnames)
        missing_sheets = [name for name in REQUIRED_WORKBOOK_SHEETS if name not in sheet_names]
        if missing_sheets:
            raise ValueError(f"收入报表 XLSX 缺少必要工作表：{'、'.join(missing_sheets)}")

        summary_grid = _read_sheet_grid(workbook["Summary"], max_rows=15, max_cols=10)
        if _clean_cell_text(summary_grid.get((1, 1))).casefold() != "income report":
            raise ValueError("收入报表 Summary 工作表缺少 Income Report 标题（A1）")
        if _clean_cell_text(summary_grid.get((5, 1))).casefold() != "report details":
            raise ValueError("收入报表 Summary 工作表缺少 Report Details 区块（A5）")
        if _clean_cell_text(summary_grid.get((6, 1))).casefold() != "username (seller)":
            raise ValueError("收入报表 Summary 工作表缺少 Username (Seller) 标签（A6）")

        from_label = _clean_cell_text(summary_grid.get((7, 1)))
        from_val = _normalize_cell_date(summary_grid.get((7, 2)))
        to_label = _clean_cell_text(summary_grid.get((8, 1)))
        to_val = _normalize_cell_date(summary_grid.get((8, 2)))
        if from_label.casefold() != "from" or to_label.casefold() != "to":
            raise ValueError("收入报表 Summary 工作表缺少 From / to 账期标签（A7:A8）")
        if from_val != bounds["start_iso"] or to_val != bounds["end_iso"]:
            raise ValueError(
                f"收入报表 Summary 内部账期（{from_val or '空'} 至 {to_val or '空'}）"
                f"与所选月份 {bounds['month']}（{bounds['start_iso']} 至 {bounds['end_iso']}）不一致"
            )

        summary_currency = _clean_cell_text(summary_grid.get((10, 4)))
        if summary_currency != "Rp":
            raise ValueError(
                f"收入报表 Summary 币种不是印尼盾 Rp（D10 当前为 {summary_currency or '空'}）"
            )

        adj_grid = _read_sheet_grid(workbook["Adjustment"], max_rows=10, max_cols=8)
        adj_from_label = _clean_cell_text(adj_grid.get((3, 1)))
        adj_from_val = _normalize_cell_date(adj_grid.get((3, 2)))
        adj_to_label = _clean_cell_text(adj_grid.get((3, 3)))
        adj_to_val = _normalize_cell_date(adj_grid.get((3, 4)))
        adj_currency = _clean_cell_text(adj_grid.get((4, 1)))
        if (
            adj_from_label.casefold() != "from"
            or adj_to_label.casefold() != "to"
            or adj_from_val != bounds["start_iso"]
            or adj_to_val != bounds["end_iso"]
        ):
            raise ValueError(
                f"收入报表 Adjustment 内部账期（{adj_from_val or '空'} 至 {adj_to_val or '空'}）"
                f"与所选月份 {bounds['month']}（{bounds['start_iso']} 至 {bounds['end_iso']}）不一致"
            )
        if adj_currency != "Rp":
            raise ValueError(
                f"收入报表 Adjustment 币种不是印尼盾 Rp（A4 当前为 {adj_currency or '空'}）"
            )

        for name in _workbook_sheet_family(sheet_names, "Income"):
            grid = _read_sheet_grid(workbook[name], max_rows=1, max_cols=30)
            labels = {_clean_cell_text(v) for v in grid.values()}
            if not {"Order Info", "Released Amount Details"}.issubset(labels):
                raise ValueError(f"收入报表 {name} 明细缺少 Order Info / Released Amount Details 表头")
        for name in _workbook_sheet_family(sheet_names, "Seller Fee"):
            grid = _read_sheet_grid(workbook[name], max_rows=2, max_cols=10)
            labels = {_clean_cell_text(v) for v in grid.values()}
            if not {"Sequence No.", "Order ID"}.issubset(labels):
                raise ValueError(f"收入报表 {name} 缺少费用明细表头")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"解析收入报表 XLSX 失败：{exc}") from exc
    finally:
        if workbook is not None:
            try:
                workbook.close()
            except Exception:
                pass

    return {
        "path": str(target),
        "filename": target.name,
        "size": size,
        "sha256": digest.hexdigest(),
        "month": bounds["month"],
        "period_start": bounds["start_iso"],
        "period_end": bounds["end_iso"],
        "currency": "Rp",
    }


def download_income_xlsx(
    browser: Any,
    store_id: str,
    selector: str,
    download_dir: str | Path,
    *,
    target_month: str,
    timeout: float = 120.0,
    poll_interval: float = 0.2,
) -> dict[str, Any]:
    """Set dedicated empty CDP download folder, click once, and validate one monthly income XLSX."""
    directory = Path(download_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("本次收入报表下载目录非空，已拒绝混用旧文件")
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
        raise ValueError("当前浏览器驱动不支持设置独立收入报表下载目录")

    try:
        browser.click(store_id, selector)
        deadline = time.monotonic() + max(float(timeout), 0.0)
        last_completed_size: dict[Path, int] = {}
        while True:
            files = [path for path in directory.iterdir() if path.is_file()]
            if any(path.stat().st_size > MAX_XLSX_BYTES for path in files):
                raise ValueError("下载的月度收入报表 XLSX 超过 64 MiB 大小限制")
            pending = [
                path
                for path in files
                if any(path.name.casefold().endswith(suffix) for suffix in TEMP_DOWNLOAD_SUFFIXES)
            ]
            completed = [path for path in files if path not in pending]
            if len(completed) > 1:
                raise ValueError("单次点击产生了多个下载文件，无法确认唯一月度收入报表 XLSX")
            if len(completed) == 1 and not pending:
                source = completed[0]
                current_size = source.stat().st_size
                try:
                    verified = validate_income_xlsx(source, target_month)
                except ValueError:
                    if time.monotonic() >= deadline or last_completed_size.get(source) == current_size:
                        raise
                else:
                    if last_completed_size.get(source) == current_size:
                        return verified
                last_completed_size[source] = current_size
            if time.monotonic() >= deadline:
                raise ValueError("等待月度收入报表 XLSX 下载完成超时")
            time.sleep(min(max(float(poll_interval), 0.01), max(0.0, deadline - time.monotonic())))
    finally:
        try:
            driver.execute_cdp_cmd(command, {"behavior": "default"})
        except Exception:
            pass


# Read-only DOM snapshot covering My Reports > Income history grid and My Income > Released exporter.
# Only a boolean password autofill state crosses the browser boundary; never return a password.
INCOME_PAGE_STATE = r"""(() => {
  const visible = e => Boolean(
    e && e.getClientRects && e.getClientRects().length > 0 &&
    getComputedStyle(e).visibility !== 'hidden' &&
    getComputedStyle(e).display !== 'none'
  );
  // Shopee uses display:contents for grid rows: the wrapper has no rectangle,
  // while its cells are visible. Do not treat these real rows as absent.
  const visibleRow = e => Boolean(e && (visible(e) || [...e.children].some(visible)));
  const clean = v => String(v ?? '').replace(/\s+/g, ' ').trim();
  const isDisabled = e => Boolean(
    !e || e.disabled === true || e.matches(':disabled') ||
    e.getAttribute('disabled') !== null ||
    e.getAttribute('aria-disabled') === 'true' ||
    e.classList.contains('is-disabled') ||
    e.classList.contains('disabled') ||
    e.classList.contains('eds-button--disabled') ||
    e.closest('[disabled],[aria-disabled="true"],.is-disabled,.eds-button--disabled')
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
    e => visible(e) && /^verify$/i.test(clean(e.innerText || e.textContent))
  );
  let verifySelector = '';
  if (verifyButtons.length === 1) {
    verifySelector = document.querySelectorAll('button.eds-button--primary.action').length === 1
      ? 'button.eds-button--primary.action'
      : uniquePath(verifyButtons[0]);
  }

  const challengeDialogs = [...document.querySelectorAll('[role="dialog"],.eds-modal,.eds-dialog,iframe[src*="captcha"]')].filter(visible);
  const challenge = challengeDialogs.some(e => {
    if (e.tagName === 'IFRAME') return true;
    if (e.querySelector('input[type="password"]')) return false;
    const text = clean(e.innerText || e.textContent);
    return /captcha|验证码|安全验证|滑块|otp|verification\s*code|kode\s*verifikasi|verifikasi\s*keamanan/i.test(text);
  });

  // A report's Processing spinner is a row status, not a loading overlay.
  const busy = [...document.querySelectorAll('.eds-loading,.eds-spin,.eds-table__loading,[aria-busy="true"],.loading-mask')]
    .some(e => visible(e) && !e.closest('.grid-table-row .operation,.report-exporter-container .status'));
  const loginPage = /^\/(?:account\/(?:signin|login)|buyer\/login)(?:\/|$)/i.test(location.pathname || '');

  // 1. Full My Reports > Income history grid (.grid-table)
  const gridTables = [...document.querySelectorAll('.grid-table')].filter(visible);
  let headerColumns = [];
  let historyRows = [];
  let historyBodyPresent = false;
  let emptyStateConfirmed = false;
  if (gridTables.length === 1) {
    const grid = gridTables[0];
    const headerRow = grid.querySelector('.grid-table-header .grid-table-row');
    if (visibleRow(headerRow)) {
      headerColumns = [...headerRow.children].map(c => clean(c.innerText || c.textContent));
    }
    const body = grid.querySelector('.grid-table-body');
    historyBodyPresent = Boolean(body && visible(body));
    const rowNodes = body
      ? [...body.querySelectorAll(':scope > .grid-table-row, .grid-table-row')].filter(visibleRow)
      : [];
    historyRows = rowNodes.map((rowNode, idx) => {
      const rowIndex = idx + 1;
      const childIndex = rowNode.parentElement
        ? [...rowNode.parentElement.children].indexOf(rowNode) + 1
        : rowIndex;
      const cells = [...rowNode.children];
      const reportType = cells[0] ? clean(cells[0].innerText || cells[0].textContent) : '';
      const requestTime = cells[1] ? clean(cells[1].innerText || cells[1].textContent) : '';
      const reportName = cells[3] ? clean(cells[3].innerText || cells[3].textContent) : '';
      const actionCell = cells[4] || rowNode;
      const actionText = cells[4] ? clean(cells[4].innerText || cells[4].textContent) : '';
      const downloadButtons = [...actionCell.querySelectorAll('.operation .download button')].filter(visible);
      const dlBtn = downloadButtons.length === 1 ? downloadButtons[0] : null;
      let downloadSelector = '';
      if (dlBtn) {
        const directSel = `.grid-table-body .grid-table-grid > .grid-table-row:nth-child(${childIndex}) .operation .download button`;
        if (document.querySelectorAll(directSel).length === 1 && document.querySelector(directSel) === dlBtn) {
          downloadSelector = directSel;
        } else {
          downloadSelector = uniquePath(dlBtn);
        }
      }
      return {
        row_index: rowIndex,
        dom_child_index: childIndex,
        cell_count: cells.length,
        report_type: reportType,
        request_time: requestTime,
        report_name: reportName,
        action_text: actionText,
        has_download_button: Boolean(dlBtn),
        download_text: dlBtn ? clean(dlBtn.innerText || dlBtn.textContent) : '',
        download_disabled: dlBtn ? isDisabled(dlBtn) : true,
        download_selector: downloadSelector,
      };
    });
    const emptyNodes = [...grid.querySelectorAll('.eds-empty,.empty-state,.no-data,[class*="empty"]')].filter(visible);
    const gridText = clean(grid.innerText || grid.textContent);
    emptyStateConfirmed = historyRows.length === 0 && (
      emptyNodes.length > 0 ||
      /\b(?:no\s+reports?|no\s+data|no\s+records?|no\s+result|belum\s+ada\s+laporan|tidak\s+ada\s+data)\b/i.test(gridText)
    );
  }

  const activePages = [...document.querySelectorAll('.eds-pager__page.active')].filter(visible);
  const nextButtons = [...document.querySelectorAll('button.eds-pager__button-next')].filter(visible);
  const prevButtons = [...document.querySelectorAll('button.eds-pager__button-prev')].filter(visible);
  let nextSelector = '';
  if (nextButtons.length === 1) {
    nextSelector = document.querySelectorAll('button.eds-pager__button-next').length === 1
      ? 'button.eds-pager__button-next'
      : uniquePath(nextButtons[0]);
  }

  // 2. My Income > Released page controls & remote export history popover
  const activeTabs = [...document.querySelectorAll('.eds-tabs__nav-tab.active')].filter(visible);
  const activeTabText = activeTabs.length === 1 ? clean(activeTabs[0].innerText || activeTabs[0].textContent) : '';
  const dateRanges = [...document.querySelectorAll('.date-range-value')].filter(visible);
  const dateRangeText = dateRanges.length === 1 ? clean(dateRanges[0].innerText || dateRanges[0].textContent) : '';

  const exportButtons = [...document.querySelectorAll('button.report-exporter-button')].filter(visible);
  let exportButtonSelector = '';
  if (exportButtons.length === 1) {
    exportButtonSelector = document.querySelectorAll('button.report-exporter-button').length === 1
      ? 'button.report-exporter-button'
      : uniquePath(exportButtons[0]);
  }
  const toggleButtons = [...document.querySelectorAll('button.report-exporter-toggle')].filter(visible);
  let toggleButtonSelector = '';
  if (toggleButtons.length === 1) {
    toggleButtonSelector = document.querySelectorAll('button.report-exporter-toggle').length === 1
      ? 'button.report-exporter-toggle'
      : uniquePath(toggleButtons[0]);
  }

  const exporterContainers = [...document.querySelectorAll('.report-exporter-container')].filter(visible);
  let popoverItems = [];
  let containerTestId = '';
  let containerSection = '';
  if (exporterContainers.length === 1) {
    const container = exporterContainers[0];
    containerTestId = container.getAttribute('data-testid') || '';
    containerSection = container.getAttribute('data-section') || '';
    const itemNodes = [...container.querySelectorAll('.list > .list-item:not(.list-head)')].filter(visible);
    popoverItems = itemNodes.map((node, idx) => {
      const itemIndex = idx + 1;
      const childIndex = node.parentElement
        ? [...node.parentElement.children].indexOf(node) + 1
        : itemIndex + 1;
      const nameNode = node.querySelector('.name');
      const statusNode = node.querySelector('.status');
      const btn = [...(statusNode || node).querySelectorAll('button')].find(visible) || null;
      let downloadSelector = '';
      if (btn) {
        const directSel = `.report-exporter-container .list > .list-item:nth-child(${childIndex}) button`;
        if (document.querySelectorAll(directSel).length === 1 && document.querySelector(directSel) === btn) {
          downloadSelector = directSel;
        } else {
          downloadSelector = uniquePath(btn);
        }
      }
      return {
        item_index: itemIndex,
        dom_child_index: childIndex,
        name: nameNode ? clean(nameNode.innerText || nameNode.textContent) : '',
        status_text: statusNode ? clean(statusNode.innerText || statusNode.textContent) : '',
        has_button: Boolean(btn),
        button_text: btn ? clean(btn.innerText || btn.textContent) : '',
        button_disabled: btn ? isDisabled(btn) : true,
        download_selector: downloadSelector,
      };
    });
  }

  return JSON.stringify({
    url: location.href,
    title: document.title,
    login_page: loginPage,
    challenge,
    busy,
    password_dialog: {
      password_visible: passwordInputs.length > 0,
      password_count: passwordInputs.length,
      password_filled: passwordInputs.length === 1 && Boolean(passwordInputs[0].value && passwordInputs[0].value.length > 0),
      verify_count: verifyButtons.length,
      verify_disabled: verifyButtons.length === 1 ? isDisabled(verifyButtons[0]) : true,
      verify_selector: verifySelector,
    },
    history_grid: {
      grid_count: gridTables.length,
      header_columns: headerColumns,
      body_present: historyBodyPresent,
      empty_state_confirmed: emptyStateConfirmed,
      rows: historyRows,
      pagination: {
        active_page_count: activePages.length,
        active_page_text: activePages.length === 1 ? clean(activePages[0].innerText || activePages[0].textContent) : '',
        next_count: nextButtons.length,
        prev_count: prevButtons.length,
        next_disabled: nextButtons.length === 1 ? isDisabled(nextButtons[0]) : true,
        next_selector: nextSelector,
      },
    },
    income_page: {
      active_tab_count: activeTabs.length,
      active_tab_text: activeTabText,
      date_range_count: dateRanges.length,
      date_range_text: dateRangeText,
      export_button_count: exportButtons.length,
      export_button_text: exportButtons.length === 1 ? clean(exportButtons[0].innerText || exportButtons[0].textContent) : '',
      export_button_disabled: exportButtons.length === 1 ? isDisabled(exportButtons[0]) : true,
      export_button_selector: exportButtonSelector,
      toggle_button_count: toggleButtons.length,
      toggle_button_disabled: toggleButtons.length === 1 ? isDisabled(toggleButtons[0]) : true,
      toggle_button_selector: toggleButtonSelector,
      exporter_container: {
        count: exporterContainers.length,
        data_testid: containerTestId,
        data_section: containerSection,
        items: popoverItems,
      },
    },
  });
})()"""


def _navigate_url(browser: Any, store_id: str, url: str) -> None:
    driver_getter = getattr(browser, "_driver", None)
    if not callable(driver_getter):
        raise ValueError("当前浏览器会话不支持 WebDriver 页面跳转控制")
    driver = driver_getter(store_id)
    get_fn = getattr(driver, "get", None)
    if not callable(get_fn):
        raise ValueError("当前浏览器驱动不支持 get(url) 页面跳转")
    for attempt in range(2):
        try:
            get_fn(url)
            return
        except Exception as exc:
            if not any(code in str(exc) for code in ("net::ERR_CONNECTION_CLOSED", "net::ERR_CONNECTION_RESET", "net::ERR_TIMED_OUT")):
                raise
            if attempt:
                raise ValueError("Shopee 页面连接中断，重新加载仍未成功，请稍后重试（已下载文件保留）") from exc
            # Only repeat navigation to a known page; never retry Export or Download clicks.
            time.sleep(0.5)


def _raw_income_state(browser: Any, store_id: str) -> dict[str, Any]:
    result = browser.evaluate(store_id, INCOME_PAGE_STATE)
    if not isinstance(result, dict) or ("history_grid" not in result and "income_page" not in result):
        raise ValueError("无法识别 Shopee 印尼收入报表页面结构")
    hostname = (urlsplit(str(result.get("url") or "")).hostname or "").casefold()
    if hostname != SHOPEE_ID_HOST:
        raise ValueError("当前页面不是 Shopee 印尼卖家中心（seller.shopee.co.id），已拒绝操作")
    if result.get("challenge"):
        raise ValueError("店铺触发人工验证或验证码（OTP/Captcha），请在紫鸟窗口完成验证后重试")
    if result.get("login_page"):
        raise ValueError("店铺未登录 Shopee 印尼卖家中心，请在紫鸟中完成登录后重试")
    return result


def _is_my_reports_income_url(url: str) -> bool:
    parsed = urlsplit(str(url or ""))
    path = parsed.path.rstrip("/")
    if path == "/portal/settings/shop/reports/income":
        return True
    if path == "/portal/settings/shop/reports":
        query = parse_qs(parsed.query)
        return query.get("type") == ["ps_reports_income"]
    return False


def _validate_history_headers(header_columns: list[Any]) -> bool:
    normalized = tuple(" ".join(str(col or "").strip().split()).casefold() for col in header_columns)
    return normalized == EXPECTED_HISTORY_HEADERS


def _history_rows_signature(rows: list[dict[str, Any]]) -> tuple[Any, ...]:
    return tuple(
        (
            row.get("row_index"),
            row.get("report_type"),
            row.get("request_time"),
            row.get("report_name"),
            row.get("action_text"),
            row.get("has_download_button"),
            row.get("download_text"),
            row.get("download_disabled"),
        )
        for row in rows
    )


def _classify_history_row_status(row: dict[str, Any]) -> str:
    action_text = str(row.get("action_text") or "").strip()
    dl_text = str(row.get("download_text") or "").strip()
    if (
        row.get("has_download_button")
        and not row.get("download_disabled")
        and row.get("download_selector")
        and bool(re.fullmatch(r"download", dl_text or action_text, re.IGNORECASE))
    ):
        return "download"
    if re.search(r"processing|generating|exporting|pending|in\s*progress", action_text, re.IGNORECASE):
        return "processing"
    return action_text.casefold() or "unknown"


def _classify_popover_item_status(item: dict[str, Any]) -> str:
    status_text = str(item.get("status_text") or "").strip()
    btn_text = str(item.get("button_text") or "").strip()
    if (
        item.get("has_button")
        and not item.get("button_disabled")
        and item.get("download_selector")
        and re.fullmatch(r"download", btn_text or status_text, re.IGNORECASE)
    ):
        return "download"
    if re.search(r"processing|generating|exporting|pending|in\s*progress", status_text, re.IGNORECASE):
        return "processing"
    if re.fullmatch(r"downloaded", status_text, re.IGNORECASE):
        return "downloaded"
    return status_text.casefold() or "unknown"


def _wait_settled_history_page(
    browser: Any,
    store_id: str,
    *,
    expected_page: int,
    previous_rows_sig: tuple[Any, ...] | None,
    timeout: float,
    poll_interval: float,
    verify_password: Callable[[], bool] | None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout), 0.0)
    settled_sig: tuple[Any, ...] | None = None
    last_issue = f"等待收入报表历史第 {expected_page} 页加载稳定超时"

    while True:
        state = _raw_income_state(browser, store_id)
        dialog = state.get("password_dialog") or {}
        if dialog.get("password_visible"):
            if callable(verify_password):
                verify_password()
                settled_sig = None
                continue
            raise ValueError("页面弹出 Shopee 密码验证弹窗，但未提供自动验证处理器")

        url = str(state.get("url") or "")
        path = urlsplit(url).path.rstrip("/")
        if path.startswith("/portal/settings/shop/reports/") and not _is_my_reports_income_url(url):
            raise ValueError(f"当前报表历史分类不是收入报表（{path}），已拒绝抓取其他分类")

        grid = state.get("history_grid") or {}
        grid_count = int(grid.get("grid_count") or 0)
        headers = list(grid.get("header_columns") or [])
        rows = list(grid.get("rows") or [])
        empty_confirmed = bool(grid.get("empty_state_confirmed"))
        pagination = grid.get("pagination") or {}
        active_page_count = int(pagination.get("active_page_count") or 0)
        active_page_text = str(pagination.get("active_page_text") or "").strip()
        next_count = int(pagination.get("next_count") or 0)

        if state.get("busy"):
            settled_sig = None
            last_issue = f"收入报表历史第 {expected_page} 页仍在加载中"
        elif not _is_my_reports_income_url(url):
            settled_sig = None
            last_issue = "等待跳转到 My Reports 收入报表历史页面"
        elif grid_count > 1:
            raise ValueError("收入报表历史页面存在多个 .grid-table 表格，无法唯一确认")
        elif grid_count != 1 or not headers:
            settled_sig = None
            last_issue = "等待收入报表历史 .grid-table 渲染完成"
        elif not _validate_history_headers(headers):
            raise ValueError(f"收入报表历史表头与预期不符：{headers}")
        elif not rows and not empty_confirmed:
            settled_sig = None
            last_issue = "收入报表历史仅渲染表头或暂存空表，尚未确认真实数据或空状态"
        elif rows and any(int(r.get("cell_count") or 0) < 5 for r in rows):
            settled_sig = None
            last_issue = "收入报表历史数据行单元格未渲染完整"
        else:
            if rows:
                if active_page_count != 1 or not active_page_text.isdigit():
                    settled_sig = None
                    last_issue = "收入报表历史缺少唯一激活页码（.eds-pager__page.active）"
                elif int(active_page_text) != expected_page:
                    settled_sig = None
                    last_issue = f"收入报表历史当前页码为 {active_page_text}，等待切换到第 {expected_page} 页"
                elif next_count != 1:
                    settled_sig = None
                    last_issue = "收入报表历史分页下一页按钮（button.eds-pager__button-next）不唯一或缺失"
                else:
                    rows_sig = _history_rows_signature(rows)
                    if previous_rows_sig is not None and rows_sig == previous_rows_sig:
                        settled_sig = None
                        last_issue = f"收入报表历史第 {expected_page} 页数据尚未从上一页刷新"
                    else:
                        current_sig = (active_page_text, bool(pagination.get("next_disabled")), rows_sig)
                        if current_sig == settled_sig:
                            return state
                        settled_sig = current_sig
            else:
                # Explicitly confirmed empty state on page 1.
                if expected_page != 1:
                    settled_sig = None
                    last_issue = f"收入报表历史第 {expected_page} 页返回空行"
                else:
                    current_sig = ("empty", empty_confirmed)
                    if current_sig == settled_sig:
                        return state
                    settled_sig = current_sig

        if time.monotonic() >= deadline:
            raise ValueError(last_issue)
        time.sleep(min(max(float(poll_interval), 0.01), max(0.0, deadline - time.monotonic())))


def _revalidate_history_download_before_click(
    browser: Any,
    store_id: str,
    target_month: str,
    expected_page: int,
    expected_row: dict[str, Any],
    verify_password: Callable[[], bool] | None,
) -> str:
    bounds = month_date_bounds(target_month)
    state = _raw_income_state(browser, store_id)
    dialog = state.get("password_dialog") or {}
    if dialog.get("password_visible"):
        if not callable(verify_password):
            raise ValueError("下载前弹出 Shopee 密码验证弹窗，但未提供自动验证处理器")
        verify_password()
        state = _raw_income_state(browser, store_id)
    if state.get("busy"):
        raise ValueError("下载前复核失败：收入报表历史页面正在加载中")
    if not _is_my_reports_income_url(str(state.get("url") or "")):
        raise ValueError("下载前复核失败：页面已离开 My Reports 收入报表历史页")
    grid = state.get("history_grid") or {}
    if int(grid.get("grid_count") or 0) != 1 or not _validate_history_headers(list(grid.get("header_columns") or [])):
        raise ValueError("下载前复核失败：收入报表历史表格结构发生变化")
    pagination = grid.get("pagination") or {}
    if str(pagination.get("active_page_text") or "").strip() != str(expected_page):
        raise ValueError(f"下载前复核失败：历史页码已变化，不再是第 {expected_page} 页")

    row_index = int(expected_row.get("row_index") or 0)
    live_rows = list(grid.get("rows") or [])
    live_row = next((r for r in live_rows if int(r.get("row_index") or 0) == row_index), None)
    if live_row is None or str(live_row.get("report_name") or "").strip() != bounds["expected_filename"]:
        live_row = next(
            (
                r
                for r in live_rows
                if str(r.get("report_name") or "").strip() == bounds["expected_filename"]
                and _classify_history_row_status(r) == "download"
            ),
            None,
        )
    if live_row is None:
        raise ValueError(f"下载前复核失败：未找到 {bounds['expected_filename']} 的可下载行")
    report_type = str(live_row.get("report_type") or "").strip()
    if report_type and report_type.casefold() != "released":
        raise ValueError(f"下载前复核失败：历史记录类型不是 released（当前为 {report_type}）")
    if not is_matching_monthly_income_filename(live_row.get("report_name"), target_month):
        raise ValueError(f"下载前复核失败：历史记录文件名 {live_row.get('report_name')} 与 {target_month} 不匹配")
    if _classify_history_row_status(live_row) != "download" or not live_row.get("download_selector"):
        raise ValueError(f"下载前复核失败：{bounds['expected_filename']} 的 Download 按钮不可操作")
    return str(live_row["download_selector"])


def _wait_history_row_ready_and_download(
    browser: Any,
    store_id: str,
    target_month: str,
    expected_page: int,
    download_dir: Path,
    *,
    generation_timeout: float,
    download_timeout: float,
    poll_interval: float,
    verify_password: Callable[[], bool] | None,
    timeout: float = 45.0,
    max_history_pages: int = MAX_HISTORY_PAGES,
    refresh_interval: float = 20.0,
    source: str = "history",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    bounds = month_date_bounds(target_month)
    deadline = time.monotonic() + max(float(generation_timeout), 0.0)
    next_refresh = time.monotonic()
    next_notice = time.monotonic()
    while True:
        now = time.monotonic()
        if now >= deadline:
            raise ValueError(f"等待 {bounds['expected_filename']} 生成超时（未重复点击 Export）；可稍后重试复用历史记录")
        if now >= next_refresh:
            # My Reports may retain a stale Processing status. Refresh only the history,
            # then locate the filename again: concurrent exports can move it to another page.
            _navigate_url(browser, store_id, MY_REPORTS_INCOME_URL)
            try:
                found = _find_history_report(
                    browser, store_id, target_month, timeout=min(timeout, max(deadline - now, 0.0)),
                    poll_interval=poll_interval, max_history_pages=max_history_pages,
                    verify_password=verify_password,
                )
            except ValueError:
                if time.monotonic() >= deadline:
                    raise ValueError(f"等待 {bounds['expected_filename']} 生成超时（未重复点击 Export）；可稍后重试复用历史记录") from None
                raise
            if found:
                row, expected_page = found
                status = _classify_history_row_status(row)
                if status == "download":
                    selector = _revalidate_history_download_before_click(
                        browser, store_id, target_month, expected_page, row, verify_password
                    )
                    verified = download_income_xlsx(
                        browser, store_id, selector, download_dir, target_month=target_month,
                        timeout=download_timeout, poll_interval=poll_interval,
                    )
                    return {**verified, "source": source, "history_page": expected_page}
                if status != "processing":
                    raise ValueError(f"目标月报状态不可下载：{row.get('action_text') or status}（未重复导出）")
            next_refresh = time.monotonic() + max(refresh_interval, 0.01)
        if progress and time.monotonic() >= next_notice:
            progress(f"{target_month} 收入报表仍在生成，正在等待 Download（不会重复导出）")
            next_notice = time.monotonic() + 60.0
        if time.monotonic() >= deadline:
            raise ValueError(
                f"等待 {bounds['expected_filename']} 生成超时（未重复点击 Export）；可稍后重试复用历史记录"
            )
        time.sleep(min(1.0, max(next_refresh - time.monotonic(), 0.01), max(0.0, deadline - time.monotonic())))


def _find_history_report(
    browser: Any, store_id: str, target_month: str, *, timeout: float,
    poll_interval: float, max_history_pages: int,
    verify_password: Callable[[], bool] | None,
) -> tuple[dict[str, Any], int] | None:
    """Search the complete income category. None means a confirmed last/empty page."""
    previous_sig = None
    for page in range(1, max_history_pages + 1):
        state = _wait_settled_history_page(
            browser, store_id, expected_page=page, previous_rows_sig=previous_sig,
            timeout=timeout, poll_interval=poll_interval, verify_password=verify_password,
        )
        grid = state["history_grid"]
        rows = grid.get("rows") or []
        matches = [r for r in rows if is_matching_monthly_income_filename(r.get("report_name"), target_month)]
        for row in matches:
            if str(row.get("report_type") or "").strip().casefold() != "released":
                raise ValueError("整月报表文件名与历史记录的 Released 类型不一致，已停止导出")
        if matches:
            # If duplicate historical requests exist, prefer a completed file on this page.
            return next((r for r in matches if _classify_history_row_status(r) == "download"), matches[0]), page
        pagination = grid.get("pagination") or {}
        if not rows or pagination.get("next_disabled"):
            return None
        if page == max_history_pages:
            raise ValueError(f"收入报表历史已扫描 {max_history_pages} 页仍未到达末页，已停止导出")
        selector = pagination.get("next_selector")
        if not selector:
            raise ValueError("收入报表历史缺少下一页按钮，已停止导出")
        previous_sig = _history_rows_signature(rows)
        browser.click(store_id, selector)
    raise ValueError("收入报表历史页数限制无效")


def _wait_income_export_page_ready(
    browser: Any,
    store_id: str,
    target_month: str,
    *,
    timeout: float,
    poll_interval: float,
    verify_password: Callable[[], bool] | None,
) -> dict[str, Any]:
    bounds = month_date_bounds(target_month)
    deadline = time.monotonic() + max(float(timeout), 0.0)
    settled_sig: tuple[Any, ...] | None = None
    last_issue = f"等待 Shopee 印尼 Income > Released（{bounds['display_dmy_range']}）页面就绪超时"

    while True:
        state = _raw_income_state(browser, store_id)
        dialog = state.get("password_dialog") or {}
        if dialog.get("password_visible"):
            if not callable(verify_password):
                raise ValueError("Income 页面弹出 Shopee 密码验证弹窗，但未提供自动验证处理器")
            verify_password()
            settled_sig = None
            continue

        url = str(state.get("url") or "")
        parsed_url = urlsplit(url)
        path = parsed_url.path.rstrip("/")
        query = parse_qs(parsed_url.query)
        income_page = state.get("income_page") or {}
        active_tab_count = int(income_page.get("active_tab_count") or 0)
        active_tab_text = str(income_page.get("active_tab_text") or "").strip()
        date_range_count = int(income_page.get("date_range_count") or 0)
        date_range_text = str(income_page.get("date_range_text") or "").strip()
        export_btn_count = int(income_page.get("export_button_count") or 0)
        export_btn_text = str(income_page.get("export_button_text") or "").strip()
        export_btn_disabled = bool(income_page.get("export_button_disabled"))
        export_btn_selector = str(income_page.get("export_button_selector") or "").strip()

        if state.get("busy"):
            settled_sig = None
            last_issue = "Income 页面仍在加载中"
        elif path != "/portal/finance/income":
            settled_sig = None
            last_issue = f"当前页面路径为 {path or '空'}，尚未进入 /portal/finance/income"
        elif query.get("type") != ["2"]:
            settled_sig = None
            last_issue = f"Income 页面 URL 参数未确认 type=2（当前 URL：{parsed_url.path}?{parsed_url.query}）"
        elif query.get("dateRange") != [bounds["date_range_param"]]:
            settled_sig = None
            last_issue = (
                f"Income 页面 URL dateRange 参数（{query.get('dateRange')}）"
                f"与目标月份 {bounds['date_range_param']} 不一致"
            )
        elif active_tab_count != 1 or active_tab_text.casefold() != "released":
            settled_sig = None
            last_issue = f"Income 页面当前激活标签为 {active_tab_text or '空'}，未确认 Released 标签"
        elif date_range_count != 1 or date_range_text != bounds["display_dmy_range"]:
            settled_sig = None
            last_issue = (
                f"Income 页面日期选择器显示为 {date_range_text or '空'}，"
                f"与目标自然月 {bounds['display_dmy_range']} 不一致"
            )
        elif export_btn_count > 1:
            raise ValueError("Income 页面存在多个 Export 按钮（button.report-exporter-button），无法唯一确认")
        elif export_btn_count != 1 or export_btn_text.casefold() != "export":
            settled_sig = None
            last_issue = "等待 Income 页面远程导出组件（button.report-exporter-button）加载完成"
        elif export_btn_disabled or not export_btn_selector:
            settled_sig = None
            last_issue = "Income 页面 Export 按钮当前处于禁用状态"
        else:
            current_sig = (
                parsed_url.path,
                tuple(query.get("type") or ()),
                tuple(query.get("dateRange") or ()),
                active_tab_text,
                date_range_text,
                export_btn_text,
                export_btn_disabled,
            )
            if current_sig == settled_sig:
                return state
            settled_sig = current_sig

        if time.monotonic() >= deadline:
            raise ValueError(last_issue)
        time.sleep(min(max(float(poll_interval), 0.01), max(0.0, deadline - time.monotonic())))


def export_monthly_income_report(
    browser: Any,
    store_id: str,
    target_month: str,
    download_dir: str | Path,
    *,
    timeout: float = 45.0,
    generation_timeout: float = GENERATION_TIMEOUT,
    download_timeout: float = 120.0,
    poll_interval: float = 0.25,
    max_history_pages: int = MAX_HISTORY_PAGES,
    verify_password: Callable[[], bool] | None = None,
    refresh_interval: float = 20.0,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Reuse the exact Released ID calendar-month report; generate once only if absent."""
    bounds = month_date_bounds(target_month)
    page_limit = max(int(max_history_pages), 1)
    target_dir = Path(download_dir).resolve()
    initial_state = _raw_income_state(browser, store_id)
    pagination = (initial_state.get("history_grid") or {}).get("pagination") or {}
    if (not _is_my_reports_income_url(str(initial_state.get("url") or ""))
            or str(pagination.get("active_page_text") or "1") != "1"):
        _navigate_url(browser, store_id, MY_REPORTS_INCOME_URL)
    if progress:
        progress(f"核对 {target_month} 收入报表的完整导出历史")
    found = _find_history_report(
        browser, store_id, target_month, timeout=timeout, poll_interval=poll_interval,
        max_history_pages=page_limit, verify_password=verify_password,
    )
    source = "history"
    page = 1
    if found:
        row, page = found
        status = _classify_history_row_status(row)
        if status == "download":
            if progress:
                progress(f"历史第 {page} 页已有 {target_month} 整月收入报表，直接下载")
            selector = _revalidate_history_download_before_click(
                browser, store_id, target_month, page, row, verify_password
            )
            verified = download_income_xlsx(
                browser, store_id, selector, target_dir, target_month=target_month,
                timeout=download_timeout, poll_interval=poll_interval,
            )
            return {**verified, "source": source, "history_page": page}
        if status != "processing":
            raise ValueError(f"整月收入报表已存在但不可下载：{row.get('action_text') or status}；未重复导出")
    else:
        source = "generated"
        _navigate_url(browser, store_id, bounds["income_url"])
        ready = _wait_income_export_page_ready(
            browser, store_id, target_month, timeout=timeout,
            poll_interval=poll_interval, verify_password=verify_password,
        )
        if progress:
            progress(f"历史中没有 {target_month} 整月收入报表，确认 Released 和日期后提交一次 Export")
        browser.click(store_id, ready["income_page"]["export_button_selector"])
        # Allow the asynchronous export request to acknowledge before leaving the page.
        # An uncertain click/response must never lead to a second Export.
        acknowledgement_deadline = time.monotonic() + min(max(timeout, 0.0), 15.0)
        while True:
            state = _raw_income_state(browser, store_id)
            if (state.get("password_dialog") or {}).get("password_visible"):
                if not callable(verify_password):
                    raise ValueError("导出后需要 Shopee 密码验证，请验证后重试历史下载")
                verify_password()
            items = ((state.get("income_page") or {}).get("exporter_container") or {}).get("items") or []
            if any(is_matching_monthly_income_filename(item.get("name"), target_month) for item in items):
                break
            if time.monotonic() >= acknowledgement_deadline:
                break
            time.sleep(min(max(poll_interval, 0.01), max(acknowledgement_deadline - time.monotonic(), 0.0)))
    return _wait_history_row_ready_and_download(
        browser, store_id, target_month, page, target_dir,
        generation_timeout=generation_timeout, download_timeout=download_timeout,
        poll_interval=poll_interval, verify_password=verify_password,
        timeout=timeout, max_history_pages=page_limit, refresh_interval=refresh_interval,
        source=source, progress=progress,
    )
