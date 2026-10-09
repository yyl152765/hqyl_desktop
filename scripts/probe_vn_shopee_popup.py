from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPERBROWSER_ROOT = PROJECT_ROOT.parent / "superbrowser_process"
for candidate in (PROJECT_ROOT, SUPERBROWSER_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from backend.config_store import BoundAccount, ConfigStore  # noqa: E402
from backend.core.ziniao_client import (  # noqa: E402
    ZiniaoBrowserSession,
    ZiniaoClient,
    ZiniaoClientError,
    ZiniaoCredentials,
)
from util.ziniao_runtime_util import (  # noqa: E402
    resolve_webdriver_path,
    resolve_ziniao_client_path,
)


DEFAULT_STORE = "史相涣-越南035-VN002"
DEFAULT_ADS_URL = "https://seller.shopee.vn/portal/marketing/pas/index"
LEGACY_CLOSE_XPATH = (
    "//i[contains(@class, 'eds-modal__close')]"
    " | //button[contains(normalize-space(), 'Got It')]"
)

DOM_PROBE_SCRIPT = r"""
const isVisible = (el) => {
  if (!el || !(el instanceof Element)) return false;
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== 'none'
    && style.visibility !== 'hidden'
    && Number(style.opacity || 1) > 0
    && rect.width > 0
    && rect.height > 0;
};
const short = (value, limit = 500) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, limit);
const describe = (el, htmlLimit = 1200) => {
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return {
    tag: el.tagName.toLowerCase(),
    id: el.id || '',
    className: short(typeof el.className === 'string' ? el.className : el.getAttribute('class'), 300),
    role: el.getAttribute('role') || '',
    ariaLabel: el.getAttribute('aria-label') || '',
    title: el.getAttribute('title') || '',
    text: short(el.innerText || el.textContent, 500),
    rect: {
      x: Math.round(rect.x), y: Math.round(rect.y),
      width: Math.round(rect.width), height: Math.round(rect.height),
    },
    position: style.position,
    zIndex: style.zIndex,
    html: short(el.outerHTML, htmlLimit),
  };
};

const modalSelectors = [
  '[role="dialog"]',
  '[aria-modal="true"]',
  '[class*="modal" i]',
  '[class*="popup" i]',
  '[class*="mask" i]',
  '[class*="overlay" i]',
];
const modalNodes = Array.from(document.querySelectorAll(modalSelectors.join(',')))
  .filter(isVisible)
  .filter((el, index, all) => all.indexOf(el) === index)
  .sort((a, b) => Number(getComputedStyle(b).zIndex || 0) - Number(getComputedStyle(a).zIndex || 0))
  .slice(0, 60);

const closePattern = /close|dismiss|cancel|xmark|times|đóng|关闭|關閉/i;
const clickableNodes = Array.from(document.querySelectorAll(
  'button, a, [role="button"], i, svg, [class*="close" i], [aria-label], [title]'
))
  .filter(isVisible)
  .filter((el) => {
    const haystack = [
      el.id, typeof el.className === 'string' ? el.className : el.getAttribute('class'),
      el.getAttribute('aria-label'), el.getAttribute('title'), el.innerText, el.textContent,
    ].join(' ');
    if (closePattern.test(haystack)) return true;
    const rect = el.getBoundingClientRect();
    return modalNodes.some((modal) => {
      const box = modal.getBoundingClientRect();
      return rect.width <= 80 && rect.height <= 80
        && rect.x >= box.right - 100 && rect.x <= box.right + 40
        && rect.y >= box.top - 40 && rect.y <= box.top + 100;
    });
  })
  .slice(0, 60);

const resources = performance.getEntriesByType('resource')
  .map((entry) => entry.name)
  .filter((url) => /popup|modal|banner|webinar|canva/i.test(url))
  .slice(-80);

return {
  url: location.href,
  title: document.title,
  readyState: document.readyState,
  bodyText: short(document.body && document.body.innerText, 2500),
  modalCandidates: modalNodes.map((el) => describe(el)),
  closeCandidates: clickableNodes.map((el) => describe(el, 700)),
  iframes: Array.from(document.querySelectorAll('iframe')).filter(isVisible).map((el) => describe(el, 500)),
  popupResources: resources,
};
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读探查 Shopee 越南首页/广告页弹窗 DOM")
    parser.add_argument("--store", default=DEFAULT_STORE, help="紫鸟店铺名或唯一子串")
    parser.add_argument("--wait", type=int, default=15, help="每个页面等待异步弹窗秒数")
    parser.add_argument("--output-dir", default="", help="证据输出目录")
    parser.add_argument("--client-path", default="", help="紫鸟 V6 客户端路径")
    parser.add_argument("--webdriver-path", default="", help="紫鸟 ChromeDriver 目录")
    parser.add_argument(
        "--test-date-range",
        action="store_true",
        help="只读验证 Ads Credit 与前 7 天广告消耗日期筛选",
    )
    return parser.parse_args()


def credentials_for(account: BoundAccount) -> ZiniaoCredentials:
    return ZiniaoCredentials(
        company=str(account.extra.get("company") or account.name).strip(),
        username=account.username,
        password=account.password,
    )


def ordered_ziniao_accounts() -> list[BoundAccount]:
    settings = ConfigStore().load()
    accounts = [account for account in settings.accounts if account.vendor == "ziniao"]
    active_id = settings.active_account_ids.get("ziniao", "")
    return sorted(accounts, key=lambda account: account.id != active_id)


def find_store_and_client(
    accounts: list[BoundAccount],
    client_path: str,
    store_query: str,
) -> tuple[ZiniaoClient, BoundAccount, dict[str, Any]]:
    if not accounts:
        raise RuntimeError("没有已绑定的紫鸟账号")

    starter = ZiniaoClient(credentials_for(accounts[0]), client_path, port=16851)
    starter.ensure_started(core_timeout=300)
    for account in accounts:
        client = ZiniaoClient(credentials_for(account), client_path, port=16851)
        matches = [
            browser
            for browser in client.list_browsers()
            if store_query.casefold() in str(browser.get("browserName") or "").casefold()
        ]
        if len(matches) == 1:
            client.started_by_client = starter.started_by_client
            starter.started_by_client = False
            return client, account, matches[0]
        if len(matches) > 1:
            names = [str(item.get("browserName") or "") for item in matches]
            raise RuntimeError(f"店铺关键字不唯一：{names}")

    starter.exit_if_started()
    raise RuntimeError(f"所有已绑定紫鸟账号都未找到店铺：{store_query}")


def start_readonly_browser(
    client: ZiniaoClient,
    browser: dict[str, Any],
    output_dir: Path,
) -> tuple[dict[str, Any], ZiniaoBrowserSession]:
    browser_oauth = str(browser.get("browserOauth") or "").strip()
    if not browser_oauth:
        raise RuntimeError("目标店铺缺少 browserOauth")

    result = client.request(
        "startBrowser",
        browserOauth=browser_oauth,
        isWaitPluginUpdate=0,
        isHeadless=0,
        isWebDriverReadOnlyMode=1,
        cookieTypeLoad=0,
        cookieTypeSave=1,
        runMode="1",
        isLoadUserPlugin=False,
        pluginIdType=1,
        privacyMode=0,
        notPromptForDownload=1,
        forceDownloadPath=str(output_dir),
    )
    if str(result.get("statusCode")) != "0":
        message = str(result.get("err") or result.get("LastError") or "未知错误")
        raise ZiniaoClientError(f"打开店铺失败：{message}")

    try:
        debugging_port = int(result.get("debuggingPort"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("紫鸟未返回有效 debuggingPort") from exc
    session = ZiniaoBrowserSession(
        browser_oauth=browser_oauth,
        browser_name=str(browser.get("browserName") or ""),
        debugging_port=debugging_port,
        launcher_page=str(result.get("launcherPage") or ""),
        download_path=output_dir,
    )
    return result, session


def attach_selenium(
    start_result: dict[str, Any],
    debugging_port: int,
    configured_webdriver_path: str,
) -> webdriver.Chrome:
    core_version = str(start_result.get("core_version") or start_result.get("coreVersion") or "")
    if not core_version:
        raise RuntimeError("紫鸟未返回 core_version")
    major = core_version.split(".", 1)[0]
    driver_dir = Path(resolve_webdriver_path(configured_webdriver_path))
    driver_path = driver_dir / f"chromedriver{major}.exe"
    if not driver_path.is_file():
        raise RuntimeError(f"缺少匹配紫鸟内核 {core_version} 的驱动：{driver_path}")

    options = Options()
    options.add_argument("--log-level=3")
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{debugging_port}")
    driver = webdriver.Chrome(service=Service(str(driver_path)), options=options)
    driver.set_page_load_timeout(45)
    driver.set_script_timeout(20)
    try:
        driver.set_window_position(-32000, -32000)
    except WebDriverException:
        pass
    return driver


def safe_get(driver: webdriver.Chrome, url: str) -> None:
    try:
        driver.get(url)
    except TimeoutException:
        try:
            driver.execute_script("window.stop();")
        except WebDriverException:
            pass


def legacy_close_matches(driver: webdriver.Chrome) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for element in driver.find_elements(By.XPATH, LEGACY_CLOSE_XPATH):
        try:
            matches.append(
                {
                    "displayed": element.is_displayed(),
                    "tag": element.tag_name,
                    "className": element.get_attribute("class") or "",
                    "text": (element.text or "").strip()[:200],
                }
            )
        except WebDriverException as exc:
            matches.append({"error": str(exc)})
    return matches


def capture_state(
    driver: webdriver.Chrome,
    label: str,
    output_dir: Path,
) -> dict[str, Any]:
    evidence = driver.execute_script(DOM_PROBE_SCRIPT)
    evidence["label"] = label
    evidence["capturedAt"] = datetime.now().isoformat(timespec="seconds")
    evidence["windowHandles"] = list(driver.window_handles)
    evidence["legacyCloseXpath"] = LEGACY_CLOSE_XPATH
    evidence["legacyCloseMatches"] = legacy_close_matches(driver)
    screenshot_path = output_dir / f"{label}.png"
    driver.save_screenshot(str(screenshot_path))
    evidence["screenshot"] = str(screenshot_path)
    return evidence


def test_ads_readonly_flow(driver: webdriver.Chrome, store_name: str) -> dict[str, Any]:
    from implement.shopee import vn_shopee_ads_recharge as recharge_module
    from util.webdriver_util import WebdriverUtil

    webdriver_util = WebdriverUtil(driver)
    logger = logging.getLogger("vn-shopee-popup-probe")
    logger.setLevel(logging.INFO)
    started = time.monotonic()
    result: dict[str, Any] = {}

    try:
        _, ads_credit, ads_credit_error = recharge_module._wait_for_ads_credit(
            webdriver_util,
            store_name,
            timeout=30,
        )
        result["adsCredit"] = str(ads_credit)
        result["adsCreditError"] = ads_credit_error
        if ads_credit_error:
            result["status"] = "failed"
            return result

        start_date, end_date = recharge_module.select_previous_7_days_expense_range(
            webdriver_util,
            store_name,
            recharge_module.DATE_INPUT,
            logger,
            amount_xpath=recharge_module.SALES_XPATH,
        )
        sales_element = webdriver_util.find_element(By.XPATH, recharge_module.SALES_XPATH)
        result.update(
            {
                "status": "ok",
                "startDate": start_date,
                "endDate": end_date,
                "expenseText": (sales_element.text or "").strip(),
            }
        )
        return result
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        result["durationSeconds"] = round(time.monotonic() - started, 2)


def main() -> int:
    args = parse_args()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else PROJECT_ROOT / "probe_outputs" / f"vn_shopee_popup_{stamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    configured_client_path = args.client_path
    configured_webdriver_path = args.webdriver_path
    if not configured_client_path or not configured_webdriver_path:
        from main.shopee.vn_shopee_ads_recharge_operator_service import (
            build_settings_from_config,
            load_runtime_configs,
        )

        process_config, global_config, _ = load_runtime_configs()
        runtime_settings = build_settings_from_config(process_config, global_config)
        configured_client_path = configured_client_path or runtime_settings.client_path
        configured_webdriver_path = configured_webdriver_path or runtime_settings.webdriver_path

    client_path = resolve_ziniao_client_path(configured_client_path, "v6")
    if not client_path:
        raise RuntimeError("未找到紫鸟 V6 客户端，请通过 --client-path 指定")
    accounts = ordered_ziniao_accounts()
    client: ZiniaoClient | None = None
    browser_oauth = ""
    driver: webdriver.Chrome | None = None
    report: dict[str, Any] = {
        "storeQuery": args.store,
        "outputDir": str(output_dir),
        "states": [],
    }

    try:
        client, account, browser = find_store_and_client(accounts, client_path, args.store)
        report["matchedAccountName"] = account.name
        report["matchedStoreName"] = str(browser.get("browserName") or "")
        start_result, session = start_readonly_browser(client, browser, output_dir)
        browser_oauth = session.browser_oauth
        report["coreVersion"] = str(start_result.get("core_version") or start_result.get("coreVersion") or "")
        report["launcherPage"] = session.launcher_page
        driver = attach_selenium(
            start_result,
            session.debugging_port,
            configured_webdriver_path,
        )

        home_url = session.launcher_page or "https://banhang.shopee.vn/"
        safe_get(driver, home_url)
        time.sleep(max(args.wait, 0))
        report["states"].append(capture_state(driver, "home", output_dir))

        safe_get(driver, DEFAULT_ADS_URL)
        time.sleep(max(args.wait, 0))
        report["states"].append(capture_state(driver, "ads", output_dir))

        if args.test_date_range:
            report["adsReadonlyFlow"] = test_ads_readonly_flow(
                driver,
                session.browser_name,
            )
            report["states"].append(capture_state(driver, "ads_after_date_range", output_dir))

        report["status"] = "ok"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report_path = output_dir / "probe.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if driver is not None:
            try:
                driver.quit()
            except WebDriverException:
                pass
        if client is not None and browser_oauth:
            try:
                client.close_browser(browser_oauth)
            except ZiniaoClientError:
                pass
        if client is not None:
            client.exit_if_started()
        print(json.dumps({"status": report.get("status"), "probe": str(report_path)}, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
