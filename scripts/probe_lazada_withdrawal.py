"""Lazada 提现统计开发前探查工具。

本脚本只用于开发前验证紫鸟店铺列表和 Lazada 页面元素，不属于生产流程。
默认输出会过滤账号密码、店铺 OAuth、代理 IP 和调试端口。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from selenium.webdriver.common.by import By


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
SUPERBROWSER_ROOT = WORKSPACE_ROOT.parent / "superbrowser_process"
if str(SUPERBROWSER_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERBROWSER_ROOT))

from main.lazada import lazada_balance_withdrawal_operator_service as operator_service
from main.super_browser_desktop import LoadSuperBrowser
from implement.lazada import lazada_balance_withdrawal as withdrawal
from util.webdriver_util import WebdriverUtil


LOGGER = logging.getLogger("lazada_withdrawal_probe")


def _browser_platform_name(browser: dict[str, Any]) -> str:
    return str(
        browser.get("platform_name")
        or browser.get("platformName")
        or browser.get("platform")
        or ""
    ).strip()


def _is_lazada_browser(browser: dict[str, Any]) -> bool:
    name = str(browser.get("browserName") or "")
    platform = _browser_platform_name(browser)
    combined = f"{name} {platform}".casefold()
    return "lazada" in combined or "lz跨境" in combined


def _country_candidates(name: str) -> list[str]:
    text = str(name or "").casefold()
    result: list[str] = []
    keyword_map = {
        "PH": ("菲律宾", "philippines", "philippine"),
        "MY": ("马来", "malaysia"),
        "TH": ("泰国", "thailand", "thai"),
    }
    for country, keywords in keyword_map.items():
        if any(keyword in text for keyword in keywords):
            result.append(country)
    return result


def list_lazada_stores(
    *, country: str = "", limit: int = 0
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    process_config, global_config = operator_service.load_runtime_configs()
    loader = LoadSuperBrowser(
        func_dict={},
        pcfg=process_config,
        cfg=global_config,
        log=LOGGER,
    )
    try:
        stores: list[dict[str, Any]] = []
        for browser in loader.load_super_browser() or []:
            if not _is_lazada_browser(browser):
                continue
            name = str(browser.get("browserName") or "").strip()
            stores.append(
                {
                    "name": name,
                    "platform": _browser_platform_name(browser),
                    "country_candidates": _country_candidates(name),
                }
            )
        stores.sort(key=lambda item: (item["country_candidates"], item["name"]))
        counts = {
            code: sum(code in item["country_candidates"] for item in stores)
            for code in ("PH", "MY", "TH")
        }
        counts["unclassified"] = sum(not item["country_candidates"] for item in stores)
        if country:
            stores = [item for item in stores if country in item["country_candidates"]]
        if country and limit > 0:
            stores = stores[:limit]
        elif not country and limit > 0:
            stores = [
                item
                for code in ("PH", "MY", "TH")
                for item in [
                    entry
                    for entry in stores
                    if code in entry["country_candidates"]
                ][:limit]
            ]
        return stores, counts
    finally:
        try:
            loader.get_exit()
        except Exception:
            LOGGER.warning("退出紫鸟 WebDriver 服务失败", exc_info=True)


def _safe_filename(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|\t\r\n]+', "_", str(value or "").strip())
    return value.rstrip(". ") or "store"


def _select_store(browser_list: list[dict[str, Any]], store: str) -> dict[str, Any]:
    wanted = str(store or "").strip().casefold()
    exact = [
        item
        for item in browser_list
        if _is_lazada_browser(item)
        and str(item.get("browserName") or "").strip().casefold() == wanted
    ]
    if len(exact) == 1:
        return exact[0]
    contains = [
        item
        for item in browser_list
        if _is_lazada_browser(item)
        and wanted in str(item.get("browserName") or "").casefold()
    ]
    if len(contains) == 1:
        return contains[0]
    names = [str(item.get("browserName") or "") for item in (exact or contains)]
    raise RuntimeError(f"店铺必须唯一匹配，当前匹配 {len(names)} 家：{names[:20]}")


def _collect_page_state(driver: Any, stage: str) -> dict[str, Any]:
    script = r"""
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
const safeHref = (el) => {
  const href = el.href || el.getAttribute('href') || '';
  if (!href) return '';
  try {
    const url = new URL(href, location.href);
    return url.origin + url.pathname;
  } catch (_) {
    return clean(href).split('?')[0];
  }
};
const wanted = /(finance|my balance|balance transactions|withdrawal|withdraw|date|income statement|order details|财务|我的余额|余额流水|提现|日期|我的收入|收入账单|下载)/i;
const candidates = Array.from(document.querySelectorAll('a,button,li,[role="menuitem"],[role="tab"],[role="button"],input,span'));
const controls = [];
const seen = new Set();
for (const el of candidates) {
  if (!visible(el)) continue;
  const text = clean(el.innerText || el.textContent || el.value || el.placeholder);
  const href = safeHref(el);
  const probe = [text, href, el.getAttribute('data-spm'), el.getAttribute('aria-label')].join(' ');
  if (!wanted.test(probe)) continue;
  const item = {
    tag: el.tagName,
    text: text.slice(0, 160),
    href,
    role: clean(el.getAttribute('role')),
    ariaSelected: clean(el.getAttribute('aria-selected')),
    ariaPressed: clean(el.getAttribute('aria-pressed')),
    ariaLabel: clean(el.getAttribute('aria-label')).slice(0, 120),
    dataSpm: clean(el.getAttribute('data-spm')).slice(0, 120),
    className: clean(el.className).slice(0, 260),
  };
  const key = JSON.stringify(item);
  if (seen.has(key)) continue;
  seen.add(key);
  controls.push(item);
  if (controls.length >= 150) break;
}
const body = clean(document.body ? document.body.innerText : '');
const headers = Array.from(document.querySelectorAll('th,[role="columnheader"]'))
  .filter(visible)
  .map((el) => clean(el.innerText || el.textContent))
  .filter(Boolean)
  .slice(0, 30);
const dateInputs = Array.from(document.querySelectorAll('input'))
  .filter(visible)
  .map((el) => ({
    value: clean(el.value),
    placeholder: clean(el.placeholder),
    ariaLabel: clean(el.getAttribute('aria-label')),
    className: clean(el.className).slice(0, 180),
  }))
  .filter((item) => /\d{4}-\d{2}-\d{2}|date|time|日期|yyyy/i.test(Object.values(item).join(' ')))
  .slice(0, 12);
return {
  url: location.origin + location.pathname,
  title: document.title,
  controls,
  headers,
  dateInputs,
  markers: {
    finance: /finance|财务/i.test(body),
    myBalance: /my balance|我的余额/i.test(body),
    balanceTransactions: /balance transactions|余额流水/i.test(body),
    withdrawal: /withdrawal|提现/i.test(body),
    incomeStatement: /income statement|收入账单/i.test(body),
    orderDetails: /order details/i.test(body),
    noData: /no data|no transaction|暂无数据|无数据|没有记录/i.test(body),
  },
  viewport: {width: window.innerWidth, height: window.innerHeight, scrollY: window.scrollY},
};
"""
    state = driver.execute_script(script) or {}
    state["stage"] = stage
    state["captured_at"] = datetime.now().astimezone().isoformat()
    return state


def _dismiss_balance_update_modal(driver: Any) -> bool:
    js_code = r"""
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
const dialog = Array.from(document.querySelectorAll('[role="dialog"],.next-dialog,.next-overlay-inner'))
  .find((el) => visible(el) && /seller balance updated/i.test(el.innerText || el.textContent || ''));
if (!dialog) return false;
const action = Array.from(dialog.querySelectorAll('button,a,[role="button"]'))
  .find((el) => visible(el) && /checkout it out|got it|close|关闭|知道了/i.test(clean(el.innerText || el.textContent || el.getAttribute('aria-label'))));
const target = action || dialog.querySelector('.next-dialog-close,[aria-label="Close"],[aria-label="close"]');
if (!target) return false;
target.click();
return true;
"""
    try:
        dismissed = bool(driver.execute_script(js_code))
        if dismissed:
            time.sleep(1)
        return dismissed
    except Exception:
        return False


def _scroll_to_balance_transactions_probe(driver: Any) -> bool:
    js_code = r"""
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,div,span')).filter(visible);
const heading = nodes.find((el) => /^(balance transactions|余额流水)$/i.test(clean(el.innerText || el.textContent)))
  || nodes.find((el) => /balance transactions|余额流水/i.test(clean(el.innerText || el.textContent)));
if (!heading) return false;
heading.scrollIntoView({block: 'start', inline: 'nearest'});
window.scrollBy(0, -220);
return true;
"""
    try:
        moved = bool(driver.execute_script(js_code))
        if moved:
            time.sleep(0.8)
        return moved
    except Exception:
        return False


def _withdrawal_button_state(driver: Any) -> dict[str, Any]:
    return driver.execute_script(
        r"""
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
const button = Array.from(document.querySelectorAll('button,[role="button"]'))
  .find((el) => visible(el) && /^(Withdrawal|提现)$/i.test(clean(el.innerText || el.textContent)));
if (!button) return {found: false};
const style = window.getComputedStyle(button);
return {
  found: true,
  text: clean(button.innerText || button.textContent),
  className: clean(button.className),
  ariaPressed: clean(button.getAttribute('aria-pressed')),
  ariaSelected: clean(button.getAttribute('aria-selected')),
  backgroundColor: style.backgroundColor,
  color: style.color,
};
"""
    ) or {"found": False}


def _select_withdrawal_filter_probe(driver: Any) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    _scroll_to_balance_transactions_probe(driver)
    before = _withdrawal_button_state(driver)
    clicked = bool(
        driver.execute_script(
            r"""
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
const button = Array.from(document.querySelectorAll('button,[role="button"]'))
  .find((el) => visible(el) && /^(Withdrawal|提现)$/i.test(clean(el.innerText || el.textContent)));
if (!button) return false;
button.scrollIntoView({block: 'center', inline: 'nearest'});
button.click();
return true;
"""
        )
    )
    if clicked:
        time.sleep(1.5)
    after = _withdrawal_button_state(driver)
    selected = clicked and (
        after.get("ariaPressed") == "true"
        or after.get("ariaSelected") == "true"
        or "primary" in str(after.get("className") or "").casefold()
        or before.get("backgroundColor") != after.get("backgroundColor")
    )
    return selected, before, after


def _collect_withdrawal_table_probe(driver: Any) -> dict[str, Any]:
    """只读采集 PH/MY 流水表结构、可见行和分页候选元素。"""
    return driver.execute_script(
        r"""
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,div,section')).filter(visible);
const heading = nodes.find((el) => /^(balance transactions|余额流水)$/i.test(clean(el.innerText || el.textContent)))
  || nodes.find((el) => /balance transactions|余额流水/i.test(clean(el.innerText || el.textContent)));
let container = heading;
while (container && container !== document.body) {
  const text = clean(container.innerText || container.textContent);
  const rect = container.getBoundingClientRect();
  if (rect.width >= 650 && /withdrawal|提现/i.test(text) && container.querySelector('table,[role="table"],.next-table')) break;
  container = container.parentElement;
}
if (!container || container === document.body) return {found: false};
const table = container.querySelector('table') || container.querySelector('[role="table"]') || container.querySelector('.next-table');
const headerElements = table ? Array.from(table.querySelectorAll('thead th,[role="columnheader"]')) : [];
const headers = headerElements.map((el, index) => ({
  index,
  text: clean(el.innerText || el.textContent),
  className: clean(el.className),
  dataIndex: clean(el.getAttribute('data-index') || el.getAttribute('data-field') || el.getAttribute('data-column-key')),
}));
const rowElements = table ? Array.from(table.querySelectorAll('tbody tr')).filter(visible) : [];
const rows = rowElements.map((row, rowIndex) => ({
  rowIndex,
  cells: Array.from(row.querySelectorAll('td')).map((cell, cellIndex) => ({
    cellIndex,
    text: clean(cell.innerText || cell.textContent),
    rawText: String(cell.innerText || cell.textContent || '').trim(),
    className: clean(cell.className),
    dataIndex: clean(cell.getAttribute('data-index') || cell.getAttribute('data-field') || cell.getAttribute('data-column-key')),
  })),
  html: row.outerHTML.slice(0, 2500),
}));
const pagination = Array.from(container.querySelectorAll('button,a,li,[role="button"]'))
  .filter((el) => visible(el))
  .map((el) => ({
    text: clean(el.innerText || el.textContent),
    ariaLabel: clean(el.getAttribute('aria-label')),
    title: clean(el.getAttribute('title')),
    className: clean(el.className),
    disabled: Boolean(el.disabled || el.getAttribute('disabled') !== null || /disabled/i.test(String(el.className || ''))),
  }))
  .filter((item) => /next|previous|page|下一页|上一页|pagination|pager/i.test(`${item.text} ${item.ariaLabel} ${item.title} ${item.className}`));
const text = clean(container.innerText || container.textContent);
return {
  found: true,
  headers,
  rowCount: rows.length,
  rows,
  pagination,
  noData: /no data|no records|暂无数据|没有数据|无数据/i.test(text),
};
"""
    ) or {"found": False}


def _open_probe_driver(
    loader: LoadSuperBrowser,
    browser: dict[str, Any],
    country: str,
) -> tuple[Any, dict[str, Any], str]:
    store_name = str(browser.get("browserName") or "").strip()
    store_id = str(browser.get("browserOauth") or browser.get("browserId") or "")
    opened = loader.open_store(store_id)
    running_store_id = str(opened.get("browserOauth") or opened.get("browserId") or store_id)
    driver = loader.get_driver(opened)
    if driver is None:
        raise RuntimeError("紫鸟店铺已打开，但 Selenium 连接失败")
    driver.set_page_load_timeout(90)
    driver.implicitly_wait(2)
    try:
        driver.set_window_size(1500, 950)
    except Exception:
        pass
    driver._lazada_target_country = country

    ip_page = opened.get("ipDetectionPage")
    if ip_page and not loader.open_ip_check(driver, ip_page):
        LOGGER.warning("IP 检测未识别为成功，继续页面探查")
    loader.open_launcher_page(driver, opened.get("launcherPage"))
    withdrawal._ensure_lazada_login(WebdriverUtil(driver))
    return driver, opened, running_store_id


def _matching_thailand_row(
    rows: list[dict[str, Any]], start_date: str, end_date: str
) -> dict[str, Any] | None:
    target_start = withdrawal._parse_iso_date(start_date)
    target_end = withdrawal._parse_iso_date(end_date)
    for row in rows:
        period = row.get("period_range")
        if period and withdrawal._date_ranges_intersect(
            period[0], period[1], target_start, target_end
        ):
            return row
    return None


def _collect_export_modal(driver: Any, timeout: int = 90) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = driver.execute_script(
            r"""
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
const links = Array.from(document.querySelectorAll('a'))
  .filter((el) => visible(el) && clean(el.innerText || el.textContent) === 'Download Result File');
const dialog = Array.from(document.querySelectorAll('[role="dialog"],.next-dialog,.ant-modal,.next-overlay-inner'))
  .find((el) => visible(el) && /导出记录|Download Result File/i.test(clean(el.innerText || el.textContent)));
return {
  ready: links.length > 0,
  dialogText: dialog ? clean(dialog.innerText || dialog.textContent).slice(0, 5000) : '',
  rows: dialog ? Array.from(dialog.querySelectorAll('tr')).slice(1).map((row) => {
    const cells = Array.from(row.querySelectorAll('td')).map((cell) => clean(cell.innerText || cell.textContent));
    return {
      exportId: cells[0] || '',
      exportTime: cells[1] || '',
      fileName: cells[2] || '',
      status: cells[3] || '',
      hasDownloadLink: Boolean(Array.from(row.querySelectorAll('a')).find((link) => clean(link.innerText || link.textContent) === 'Download Result File')),
    };
  }).filter((row) => /^\d+$/.test(row.exportId)) : [],
  links: links.map((link, index) => {
    const row = link.closest('tr,[role="row"],li') || link.parentElement;
    return {
      index,
      rowText: clean(row ? row.innerText || row.textContent : '').slice(0, 1200),
      hrefPath: (() => {
        try {
          const url = new URL(link.href || link.getAttribute('href') || '', location.href);
          return url.origin + url.pathname;
        } catch (_) {
          return '';
        }
      })(),
      className: clean(link.className).slice(0, 260),
    };
  }),
};
"""
        ) or {}
        if result.get("ready"):
            return result
        time.sleep(1)
    return {"ready": False, "dialogText": "", "links": []}


def _export_rows(driver: Any) -> list[dict[str, Any]]:
    return driver.execute_script(
        r"""
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
const dialog = Array.from(document.querySelectorAll('[role="dialog"],.next-dialog,.ant-modal,.next-overlay-inner'))
  .find((el) => visible(el) && /导出记录|Download Result File/i.test(clean(el.innerText || el.textContent)));
if (!dialog) return [];
return Array.from(dialog.querySelectorAll('tr')).slice(1).map((row) => {
  const cells = Array.from(row.querySelectorAll('td')).map((cell) => clean(cell.innerText || cell.textContent));
  const link = Array.from(row.querySelectorAll('a')).find((item) => clean(item.innerText || item.textContent) === 'Download Result File');
  return {
    exportId: cells[0] || '',
    exportTime: cells[1] || '',
    fileName: cells[2] || '',
    status: cells[3] || '',
    hasDownloadLink: Boolean(link),
  };
}).filter((row) => /^\d+$/.test(row.exportId));
"""
    ) or []


def _click_export_link_by_id(driver: Any, export_id: str) -> bool:
    return bool(
        driver.execute_script(
            r"""
const wanted = String(arguments[0] || '');
const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
for (const row of Array.from(document.querySelectorAll('tr'))) {
  const cells = Array.from(row.querySelectorAll('td'));
  if (!cells.length || clean(cells[0].innerText || cells[0].textContent) !== wanted) continue;
  const link = Array.from(row.querySelectorAll('a')).find((item) => clean(item.innerText || item.textContent) === 'Download Result File');
  if (!link) return false;
  link.click();
  return true;
}
return false;
""",
            export_id,
        )
    )


def _download_thailand_statement_row_probe(
    driver: Any,
    row: dict[str, Any],
    browser_download: str,
    destination: Path,
    timeout: int = 180,
) -> tuple[str, dict[str, Any]]:
    before_snapshot = withdrawal._snapshot_download_files(browser_download)
    if not withdrawal._open_thailand_statement_download_menu(driver, row.get("element")):
        raise RuntimeError("无法打开泰国账单下载菜单")
    if not withdrawal._click_thailand_order_details_excel(driver):
        raise RuntimeError("无法点击 Order Details (excel)")

    deadline = time.time() + timeout
    export_id = ""
    export_file_name = ""
    while time.time() < deadline:
        rows = _export_rows(driver)
        if rows and not export_id:
            export_id = str(rows[0].get("exportId") or "")
            export_file_name = str(rows[0].get("fileName") or "")
        target = next(
            (item for item in rows if str(item.get("exportId") or "") == export_id),
            None,
        )
        if target:
            export_file_name = str(target.get("fileName") or export_file_name)
            if (
                "finished" in str(target.get("status") or "").casefold()
                and target.get("hasDownloadLink")
                and _click_export_link_by_id(driver, export_id)
            ):
                source_file = withdrawal._wait_for_new_download_file(
                    browser_download, before_snapshot, timeout=120
                )
                if not source_file:
                    raise RuntimeError(f"导出记录 {export_id} 已完成，但下载文件未落地")
                destination.mkdir(parents=True, exist_ok=True)
                statement_number = withdrawal._safe_filename(
                    row.get("statement_number") or "statement"
                )
                period_start, period_end = row.get("period_range") or (None, None)
                period_part = withdrawal._format_bill_period_part(period_start, period_end)
                extension = Path(source_file).suffix or ".xlsx"
                output_name = f"{statement_number}_{period_part}_Order_Details{extension}"
                output_file = withdrawal.copy_file(
                    source_file, str(destination), output_name
                )
                withdrawal._close_thailand_export_modal(driver)
                return output_file, {
                    "export_id": export_id,
                    "export_file_name": export_file_name,
                }
        time.sleep(1)
    raise RuntimeError(f"等待本次导出记录完成超时，export_id={export_id or '未识别'}")


def probe_store_page(
    *,
    country: str,
    store: str,
    start_date: str,
    end_date: str,
    download_one: bool = False,
    inspect_export_modal: bool = False,
) -> Path:
    process_config, global_config = operator_service.load_runtime_configs()
    loader = LoadSuperBrowser(
        func_dict={},
        pcfg=process_config,
        cfg=global_config,
        log=LOGGER,
    )
    driver = None
    running_store_id = ""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = (
        WORKSPACE_ROOT
        / "probe_outputs"
        / f"lazada_withdrawal_{country.lower()}_{_safe_filename(store)}_{timestamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "country": country,
        "requested_store": store,
        "start_date": start_date,
        "end_date": end_date,
        "started_at": datetime.now().astimezone().isoformat(),
        "stages": [],
    }
    try:
        browser_list = loader.load_super_browser() or []
        browser = _select_store(browser_list, store)
        store_name = str(browser.get("browserName") or "").strip()
        result["store_name"] = store_name
        result["platform"] = _browser_platform_name(browser)
        driver, opened, running_store_id = _open_probe_driver(loader, browser, country)

        target_origin = withdrawal._seller_center_origin_by_country(country)
        driver.get(target_origin)
        withdrawal._wait_page_ready(driver, timeout=30)
        time.sleep(5)
        result["stages"].append(_collect_page_state(driver, "seller_center_home"))

        if country in ("PH", "MY"):
            util = WebdriverUtil(driver)
            result["balance_page_opened"] = bool(
                withdrawal.open_lazada_finance_page(util)
            )
            withdrawal.close_lazada_popups(driver)
            withdrawal.open_lazada_withdrawal_page(util)
            withdrawal.close_lazada_popups(driver)
            result["balance_update_modal_dismissed"] = _dismiss_balance_update_modal(driver)
            _scroll_to_balance_transactions_probe(driver)
            time.sleep(2)
            result["stages"].append(_collect_page_state(driver, "balance_transactions"))

            selected, before_state, after_state = _select_withdrawal_filter_probe(driver)
            result["withdrawal_selected"] = selected
            result["withdrawal_button_before"] = before_state
            result["withdrawal_button_after"] = after_state
            result["date_applied"] = bool(
                withdrawal.set_trade_date_range(driver, start_date, end_date)
            )
            _dismiss_balance_update_modal(driver)
            result["visible_date_range"] = withdrawal._get_balance_date_range_text(driver)
            _scroll_to_balance_transactions_probe(driver)
            time.sleep(3)
            result["table_probe"] = _collect_withdrawal_table_probe(driver)
            result["stages"].append(
                _collect_page_state(driver, "withdrawal_date_applied")
            )
            screenshot = output_dir / "withdrawal_page.png"
            driver.save_screenshot(str(screenshot))
            result["screenshot"] = str(screenshot)
        else:
            util = WebdriverUtil(driver)
            result["income_page_opened"] = bool(
                withdrawal.open_lazada_income_statement_page(util)
            )
            withdrawal.close_lazada_popups(driver)
            result["income_statement_opened"] = bool(
                withdrawal.open_lazada_income_statement_tab(driver)
            )
            rows = withdrawal.wait_for_thailand_income_statement_rows(driver, timeout=35)
            result["statement_rows"] = [
                {
                    "statement_number": row.get("statement_number") or "",
                    "period": row.get("period") or "",
                }
                for row in rows
            ]
            result["stages"].append(_collect_page_state(driver, "income_statement"))
            screenshot = output_dir / "income_statement_page.png"
            driver.save_screenshot(str(screenshot))
            result["screenshot"] = str(screenshot)

            if download_one or inspect_export_modal:
                row = _matching_thailand_row(rows, start_date, end_date)
                if row is None:
                    result["download_probe"] = {
                        "status": "no_matching_statement",
                        "message": "当前页没有与统计日期相交的账单",
                    }
                elif inspect_export_modal:
                    if not withdrawal._open_thailand_statement_download_menu(
                        driver, row.get("element")
                    ):
                        raise RuntimeError("无法打开泰国账单下载菜单")
                    if not withdrawal._click_thailand_order_details_excel(driver):
                        raise RuntimeError("无法点击 Order Details (excel)")
                    export_modal = _collect_export_modal(driver, timeout=90)
                    export_modal["requested_statement_number"] = row.get("statement_number") or ""
                    export_modal["requested_period"] = row.get("period") or ""
                    result["export_modal_probe"] = export_modal
                    modal_screenshot = output_dir / "export_modal.png"
                    driver.save_screenshot(str(modal_screenshot))
                    result["export_modal_screenshot"] = str(modal_screenshot)
                    withdrawal._close_thailand_export_modal(driver)
                else:
                    browser_download = str(opened.get("downloadPath") or "").strip()
                    if not browser_download:
                        raise RuntimeError("紫鸟未返回店铺下载目录，无法验证文件落地")
                    destination = output_dir / "downloads"
                    destination.mkdir(parents=True, exist_ok=True)
                    downloaded, export_info = _download_thailand_statement_row_probe(
                        driver,
                        row,
                        browser_download,
                        destination,
                    )
                    result["download_probe"] = {
                        "status": "success",
                        "statement_number": row.get("statement_number") or "",
                        "period": row.get("period") or "",
                        "file": str(downloaded),
                        "file_size": Path(downloaded).stat().st_size,
                        **export_info,
                    }

        result["status"] = "success"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        LOGGER.exception("页面探查失败")
        if driver is not None:
            try:
                error_screenshot = output_dir / "error.png"
                driver.save_screenshot(str(error_screenshot))
                result["error_screenshot"] = str(error_screenshot)
            except Exception:
                pass
        raise
    finally:
        result["finished_at"] = datetime.now().astimezone().isoformat()
        result_file = output_dir / "probe.json"
        with result_file.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        if driver is not None:
            loader.quit_driver_safely(driver, result.get("store_name") or store)
        if running_store_id:
            try:
                loader.close_store(running_store_id)
            except Exception:
                LOGGER.warning("关闭探查店铺失败", exc_info=True)
        try:
            loader.get_exit()
        except Exception:
            LOGGER.warning("退出紫鸟 WebDriver 服务失败", exc_info=True)
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Lazada 提现统计开发前探查工具")
    parser.add_argument("command", choices=("list", "page"), help="探查动作")
    parser.add_argument("--country", choices=("PH", "MY", "TH"), default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--store", default="", help="page 模式使用的唯一店铺名称")
    parser.add_argument("--start-date", default="2026-06-01")
    parser.add_argument("--end-date", default="2026-06-30")
    parser.add_argument("--download-one", action="store_true")
    parser.add_argument("--inspect-export-modal", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    if args.command == "list":
        stores, counts = list_lazada_stores(
            country=args.country,
            limit=max(args.limit, 0),
        )
        payload = {"counts": counts, "stores": stores}
        print("LAZADA_STORE_LIST_BEGIN")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("LAZADA_STORE_LIST_END")
        return 0
    if args.command == "page":
        if not args.country:
            parser.error("page 模式必须指定 --country")
        if not args.store:
            parser.error("page 模式必须指定 --store")
        output_dir = probe_store_page(
            country=args.country,
            store=args.store,
            start_date=args.start_date,
            end_date=args.end_date,
            download_one=args.download_one,
            inspect_export_modal=args.inspect_export_modal,
        )
        print(f"LAZADA_PAGE_PROBE_OUTPUT={output_dir}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
