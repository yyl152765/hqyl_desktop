"""
Lazada 跨境店铺余额提现统计 - 页面自动化模块
职责: Lazada 页面跳转、弹窗关闭、日期选择、金额采集、截图
"""

import csv
import logging
import os
import re
import threading
import time
import traceback
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from util.dingtalk_doc_api import APP_KEY, APP_SECRET, USER_ID
from util.dingtalk_drive_util import DingDriveClient
from util.yingdao_util import copy_file
from util.webdriver_util import WebdriverUtil

# 流程名称
PROCESS_NAME = "Lazada跨境余额提现统计"
file_name = os.path.splitext(os.path.basename(__file__))[0]
THAILAND_DING_DRIVE_SPACE_ID = "28932857521"
THAILAND_DING_DRIVE_ROOT_FOLDER_ID = "225456777657"

# 日志
log = logging.getLogger(PROCESS_NAME)

# 运行时路径
output_directory = ""
error_directory = ""
log_directory = ""
record_file_path = ""
detail_file_path = ""

# 弹窗关闭关键词
POPUP_DISMISS_TEXTS = [
    "got it", "Got it", "GOT IT",
    "OK", "ok", "Ok",
    "I know", "I Know", "i know",
    "我知道了", "知道了", "关闭",
    "Confirm", "confirm",
    "I Understand", "I understand",
    "Dismiss", "dismiss",
    "Close", "close",
]

# 财务菜单关键词
FINANCE_MENU_KEYWORDS = [
    "Finance", "finance",
    "财务",
    "Account Statement", "account statement",
    "账户对账单",
    "Balance", "balance",
    "My Balance", "my balance",
    "Manage My Balance",
    "Withdrawal", "withdrawal",
    "Withdraw", "withdraw",
    "提现",
    "余额",
]

# 金额关键词
AMOUNT_KEYWORDS = [
    "Withdrawable amount", "withdrawable amount",
    "Available Balance", "available balance",
    "Amount", "amount",
    "Balance", "balance",
    "Withdrawal Amount", "withdrawal amount",
    "Withdraw Amount", "withdraw amount",
    "可提现余额", "余额", "金额", "提现金额",
]

# 币种符号映射
CURRENCY_SYMBOLS = {
    "₱": "PHP",
    "PHP": "PHP",
    "RM": "MYR",
    "MYR": "MYR",
    "₫": "VND",
    "VND": "VND",
}

CONFIRMED_NO_WITHDRAWAL_REMARK = "[已确认无提现]"
BALANCE_COLLECTION_MAX_PAGES = 100
BALANCE_COLLECTION_MAX_SCROLL_STEPS = 100
BALANCE_PAGE_CHANGE_TIMEOUT_SECONDS = 12
BALANCE_COLLECTION_DEADLINE_SECONDS = 90
RESULT_CSV_FIELDNAMES = [
    "detail_key", "store_name", "country", "status", "trade_time", "amount", "currency",
    "remark", "collection_outcome", "collection_complete", "empty_confirmed",
    "record_count", "candidate_count", "observed_amount",
    "detail_consistent", "detail_consistency_message",
    "message", "started_at", "finished_at",
    "query_screenshot", "result_screenshot", "diagnostic_screenshot", "screenshot",
    "download_count", "downloaded_files", "bill_periods",
    "drive_upload_count", "drive_uploaded_files", "drive_upload_message",
]
DETAIL_CSV_FIELDNAMES = [
    "detail_key", "store_name", "country", "start_date", "end_date", "trade_time",
    "status", "collection_outcome", "collection_complete", "empty_confirmed",
    "record_count", "candidate_count", "detail_consistent", "detail_consistency_message", "record_index",
    "transaction_number", "transaction_time", "raw_amount", "amount_abs",
    "currency", "remark", "raw_remark",
    "query_screenshot", "result_screenshot", "diagnostic_screenshot", "screenshot", "message",
    "sheet_write_status", "sheet_write_reason",
]
_RECORD_CSV_LOCK = threading.Lock()
_DETAIL_CSV_LOCK = threading.Lock()


def configure_runtime(
    *,
    output_dir=None,
    error_dir=None,
    log_dir=None,
    record_file=None,
    detail_file=None,
    logger=None,
):
    """配置运行时路径"""
    global output_directory, error_directory, log_directory
    global record_file_path, detail_file_path, log

    if output_dir is not None:
        output_directory = output_dir
    if error_dir is not None:
        error_directory = error_dir
    if log_dir is not None:
        log_directory = log_dir
    if record_file is not None:
        record_file_path = record_file
        if detail_file is None:
            # An older caller that only configures the summary CSV must not
            # accidentally reuse another run's detail path.
            detail_file_path = ""
    if detail_file is not None:
        detail_file_path = detail_file
    if logger is not None:
        log = logger

    for directory in (output_directory, error_directory, log_directory):
        if directory:
            os.makedirs(directory, exist_ok=True)


# ========== 主流程入口 ==========


def lazada_balance_withdrawal(driver, store_name, output_path=''):
    """
    主流程入口：采集 Lazada 店铺余额提现金额
    返回: {"status": "success"/"failed", "record": {...}}
    """
    log.info(f"正在处理店铺: {store_name}")
    webdriver_util = WebdriverUtil(driver)
    try:
        # LoadSuperBrowser uses a long implicit wait for generic scripts. This flow
        # performs many optional probes, so keep misses short or a single page can
        # stall for many minutes.
        driver.implicitly_wait(2)
    except Exception:
        pass

    # 从 operator_config 读取参数
    loader = getattr(driver, "_ziniao_loader", None)
    operator = getattr(loader, "operator_config", {}) if loader else {}
    start_date = str(operator.get("start_date", "") or "")
    end_date = str(operator.get("end_date", "") or "")

    # 从 _lazada_country_map 读取国家（线程安全：不同 store_name 不冲突）
    store_name_key = getattr(driver, "_ziniao_store_name", store_name)
    country_map = getattr(loader, "_lazada_country_map", {}) if loader else {}
    country = str(country_map.get(store_name_key, "") or "")
    try:
        driver._lazada_target_country = country
    except Exception:
        pass

    # 截图目录
    screenshot_dir = os.path.join(output_directory, "screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)
    safe_name = re.sub(r"[\\/:*?\"<>|]+", "_", store_name)
    screenshot_stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    result = {
        "store_name": store_name,
        "country": country,
        "status": "running",
        "detail_key": f"{safe_name}|{screenshot_stamp}",
        "start_date": start_date,
        "end_date": end_date,
        "trade_time": f"{start_date}~{end_date}" if start_date and end_date else "",
        "amount": "",
        "currency": "",
        "remark": "",
        "collection_outcome": "",
        "collection_complete": False,
        "empty_confirmed": False,
        "record_count": 0,
        "candidate_count": 0,
        "detail_consistent": True,
        "detail_consistency_message": "",
        "message": "",
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": "",
        "query_screenshot": "",
        "result_screenshot": "",
        "diagnostic_screenshot": "",
        "screenshot": "",
        "transaction_records": [],
        "records": [],
    }

    def capture_screenshot(field_name, label, *, position_for_evidence=False):
        screenshot_path = os.path.join(
            screenshot_dir,
            f"{safe_name}_{label}_{screenshot_stamp}.png",
        )
        try:
            if position_for_evidence and not position_balance_transactions_for_evidence_screenshot(driver):
                raise RuntimeError("无法将日期、Withdrawal 筛选和结果区域定位到截图视口")
            saved = driver.save_screenshot(screenshot_path)
            if saved is False:
                raise RuntimeError("WebDriver 返回截图失败")
            if not os.path.isfile(screenshot_path) or os.path.getsize(screenshot_path) <= 0:
                raise RuntimeError("截图文件未生成或为空")
            if field_name:
                result[field_name] = screenshot_path
            result["screenshot"] = screenshot_path
            log.info("%s截图已保存: %s", label, screenshot_path)
            return screenshot_path
        except Exception as exc:
            log.warning("%s截图失败: %s", label, exc)
            return ""

    def capture_diagnostic_screenshot():
        return capture_screenshot("diagnostic_screenshot", "诊断")

    try:
        # 1. 检查登录状态
        _ensure_lazada_login(webdriver_util)

        # 泰国账单下载入口暂时停用；保留下载函数供以后恢复。
        # TH 与 PH/MY/VN 共用下方的 My Balance -> Withdrawal 采集流程。

        # 2. 进入财务/余额页面
        if not open_lazada_finance_page(webdriver_util):
            if _is_lazada_balance_unavailable(driver):
                raise RuntimeError(
                    "当前 Lazada 店铺未开放 My Balance 功能（平台仍处于试点阶段），无法采集余额提现数据"
                )
            raise RuntimeError("无法进入 Lazada 财务/余额页面")

        # 3. 关闭弹窗
        close_lazada_popups(driver)

        # 4. 验证余额交易页面；统计流程不能点击发起提现的按钮。
        if not open_lazada_withdrawal_page(webdriver_util):
            raise RuntimeError("未确认目标国家的余额交易页面，已停止采集")

        # 5. 关闭弹窗
        close_lazada_popups(driver)

        scroll_to_balance_transactions(driver)
        if not select_withdrawal_filter(driver):
            result["status"] = "need_manual_review"
            result["collection_outcome"] = "parse_failed"
            result["message"] = "Balance Transactions 未确认切换到 Withdrawal，已停止采集"
            log.warning(result["message"])
            capture_diagnostic_screenshot()
            return {"status": result["status"], "record": result}

        # 7. 设置交易时间范围
        if start_date and end_date:
            if not set_trade_date_range(driver, start_date, end_date):
                result["status"] = "need_manual_review"
                result["collection_outcome"] = "parse_failed"
                result["message"] = "日期范围未提交成功，已停止采集，避免写入错误金额"
                log.warning(result["message"])
                capture_diagnostic_screenshot()
                return {"status": result["status"], "record": result}

        # Evidence screenshot: dismiss overlays first, then place the date/filter
        # controls and table header in the viewport. This is deliberately after
        # Withdrawal selection and the submitted/stable date-range proof.
        close_lazada_popups(driver)
        query_screenshot = capture_screenshot(
            "query_screenshot",
            "查询结果",
            position_for_evidence=True,
        )
        if not query_screenshot:
            result["status"] = "need_manual_review"
            result["collection_outcome"] = "parse_failed"
            result["message"] = "查询结果截图失败，已停止采集和写表"
            log.warning("%s: %s", store_name, result["message"])
            capture_diagnostic_screenshot()
            return {"status": result["status"], "record": result}

        # 8. 采集金额。结果必须携带完整性状态，不能再用“金额非空”推断成功。
        collection = collect_withdrawal_transaction_details(driver, country)
        result["amount"] = str(collection.get("amount", "") or "")
        result["currency"] = str(collection.get("currency", "") or "")
        result["remark"] = str(collection.get("remark", "") or "")
        result["collection_outcome"] = str(collection.get("outcome", "") or "")
        result["collection_complete"] = bool(collection.get("collection_complete"))
        result["empty_confirmed"] = bool(collection.get("empty_confirmed"))
        result["record_count"] = int(collection.get("record_count", 0) or 0)
        result["candidate_count"] = int(collection.get("candidate_count", 0) or 0)
        transaction_records = list(
            collection.get("transaction_records")
            or collection.get("records")
            or []
        )
        result["transaction_records"] = transaction_records
        result["records"] = transaction_records
        details_consistent, detail_reason = validate_transaction_record_consistency(result)
        if not details_consistent:
            result["detail_consistent"] = False
            result["detail_consistency_message"] = detail_reason
            result["collection_outcome"] = "parse_failed"
            result["collection_complete"] = False
            result["empty_confirmed"] = False
            collection["message"] = detail_reason
        if collection.get("observed_amount"):
            result["observed_amount"] = str(collection.get("observed_amount"))

        # 9. 完整遍历后的最终状态截图，与查询阶段截图分开保留。
        result_screenshot = capture_screenshot(
            "result_screenshot",
            "最终状态",
            position_for_evidence=True,
        )
        if not result_screenshot:
            result["status"] = "need_manual_review"
            result["collection_outcome"] = "parse_failed"
            result["collection_complete"] = False
            result["empty_confirmed"] = False
            result["message"] = "最终状态截图失败，已停止写表"
            log.warning("%s: %s", store_name, result["message"])
            capture_diagnostic_screenshot()
            return {"status": result["status"], "record": result}

        # 10. 写入结果状态
        outcome = result["collection_outcome"]
        if outcome == "complete":
            result["status"] = "success"
            result["message"] = "完整采集成功"
            log.info(
                "%s: 完整采集 %s 笔，金额=%s %s",
                store_name,
                result["record_count"],
                result["amount"],
                result["currency"],
            )
        elif outcome == "confirmed_empty":
            result["status"] = "no_withdrawal"
            result["message"] = "已确认所选日期范围内无 Withdrawal 记录"
            log.info("%s: %s", store_name, result["message"])
        elif outcome == "partial":
            result["status"] = "partial_success"
            result["message"] = str(collection.get("message", "") or "提现记录仅部分采集，未写入金额")
            log.warning("%s: %s", store_name, result["message"])
        else:
            result["status"] = "need_manual_review"
            result["message"] = str(collection.get("message", "") or "金额未识别，请人工复核截图")
            log.warning("%s: %s", store_name, result["message"])

        return {"status": result["status"], "record": result}

    except Exception as exc:
        result["status"] = "failed"
        result["message"] = str(exc)
        if _is_webdriver_disconnected_error(exc):
            result["status"] = "reopen_store"
            result["message"] = "WebDriver 与紫鸟店铺浏览器连接断开，准备重开店铺重试"
            log.warning(f"{store_name}: {result['message']}: {exc}")
            capture_diagnostic_screenshot()
            return {"status": "reopen_store", "record": result}
        # 失败截图
        capture_diagnostic_screenshot()
        log.error(f"{store_name}: 处理失败: {traceback.format_exc()}")
        return {"status": "failed", "record": result}

    finally:
        result["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if result.get("status") != "reopen_store":
            append_record(result)


# ========== 登录检查 ==========


def _ensure_lazada_login(util):
    """检查 Lazada Seller Center 是否已登录"""
    driver = util.driver
    current_url = str(driver.current_url or "")
    log.info(f"登录检查: 当前URL={current_url}")

    if _is_lazada_logged_in(driver):
        log.info("登录检查通过")
        return

    if (
        _target_lazada_country(driver) == "TH"
        and _is_login_url(current_url)
        and urlsplit(current_url).hostname == "sellercenter.lazada.co.th"
    ):
        # Thai cross-border credentials belong to the global seller login;
        # submitting the regional login form rejects those saved credentials.
        driver.get("https://gsp.lazada-seller.cn/portal/home")
        _wait_page_ready(driver)
        time.sleep(2)
        current_url = str(driver.current_url or "")
        if _is_lazada_logged_in(driver) or _is_global_login_handoff_page(driver):
            return

    if _is_global_login_handoff_page(driver):
        return

    if _is_login_url(current_url):
        path = urlsplit(current_url).path.rstrip("/").lower()
        if path == "/apps/register" or path.startswith("/apps/register/"):
            raise RuntimeError("Lazada 跳转到注册页，尚未确认登录；请人工完成登录后重试")
        login_xpaths = [
            "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login')]",
            "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in')]",
            "//button[contains(normalize-space(.), '登录')]",
            "//button[contains(@class, 'login') or contains(@class, 'submit')]",
            "//input[@type='submit']",
        ]
        clicked = False
        for xpath in login_xpaths:
            buttons = driver.find_elements(By.XPATH, xpath)
            for btn in buttons:
                if _is_visible(btn):
                    safe_click(driver, btn)
                    log.info("已点击登录按钮")
                    clicked = True
                    break
            if clicked:
                break

        if not clicked:
            raise RuntimeError("当前疑似登录页，但未找到登录按钮，请人工确认")

        if _wait_lazada_logged_in(driver, timeout=75):
            log.info("登录后已进入 Lazada/GSP 页面")
            return
        raise RuntimeError("点击登录后仍未进入 Lazada Seller Center/GSP 首页，请检查紫鸟店铺登录态")

    if _wait_lazada_logged_in(driver, timeout=20):
        log.info("登录检查通过")
        return
    raise RuntimeError("未识别到 Lazada 已登录页面，请人工确认当前店铺状态")


def _is_login_url(url):
    lower_url = str(url or "").lower()
    path = urlsplit(lower_url).path.rstrip("/")
    return (
        "login" in lower_url or "signin" in lower_url or "sign-in" in lower_url
        or path == "/apps/register" or path.startswith("/apps/register/")
    )


def _is_global_login_handoff_page(driver):
    """A global 404 may follow SSO; country balance must still be verified next."""
    parsed = urlsplit(str(getattr(driver, "current_url", "") or ""))
    expected_handoff = (
        _target_lazada_country(driver) in {"PH", "MY", "TH", "VN"}
        and parsed.hostname in {"gsp.lazada.com", "gsp.lazada-seller.cn"}
        and parsed.path.rstrip("/") == "/portal/404"
    )
    if not expected_handoff:
        return False
    try:
        login_controls = driver.find_elements(By.CSS_SELECTOR, "button.login-button, input[type='password']")
        return not any(_is_visible(control) for control in login_controls)
    except WebDriverException:
        return False


def _is_lazada_logged_in(driver):
    current_url = str(driver.current_url or "").lower()
    if _is_login_url(current_url):
        return False
    if "sellercenter.lazada" in current_url:
        return True
    if "gsp.lazada.com/portal/home" in current_url:
        return True
    if "gsp.lazada-seller.cn/portal/home" in current_url:
        return True
    page_text = _safe_page_text(driver, limit=5000).lower()
    if "balance transactions" in page_text or "余额流水" in page_text:
        return True
    if "finance" in page_text and ("my balance" in page_text or "seller center" in page_text):
        return True
    return False


def _wait_lazada_logged_in(driver, timeout=60):
    deadline = time.time() + timeout
    last_url = ""
    while time.time() < deadline:
        try:
            driver.execute_script("return document.readyState")
        except Exception:
            pass
        current_url = str(driver.current_url or "")
        if current_url != last_url:
            log.info(f"等待登录跳转: 当前URL={current_url}")
            last_url = current_url
        if _is_lazada_logged_in(driver) or _is_global_login_handoff_page(driver):
            return True
        time.sleep(2)
    return False


# ========== 弹窗处理 ==========


def _is_webdriver_disconnected_error(exc):
    message = f"{type(exc).__name__}: {exc}".lower()
    disconnected_keywords = (
        "connection reset",
        "connection aborted",
        "winerror 10054",
        "maxretryerror",
        "invalid session",
        "disconnected",
        "target window already closed",
        "no such window",
        "web view not found",
        "chrome not reachable",
        "devtools",
        "连接已断开",
        "浏览器连接",
    )
    return any(keyword in message for keyword in disconnected_keywords)


def _find_elements_checked(driver, by, value):
    try:
        return driver.find_elements(by, value)
    except Exception as exc:
        if _is_webdriver_disconnected_error(exc):
            raise RuntimeError("WebDriver 与紫鸟店铺浏览器连接已断开") from exc
        raise


def _dismiss_lazada_balance_welcome(driver):
    """Dismiss only the known Seller Balance announcement, never a payout dialog."""
    dismissed = 0
    for dialog in _find_elements_checked(driver, By.CSS_SELECTOR, '[role="alertdialog"]'):
        if not _is_visible(dialog):
            continue
        text = re.sub(r"\s+", " ", str(dialog.text or "")).lower()
        if "seller balance updated" not in text or "track your earnings in real time" not in text:
            continue
        buttons = dialog.find_elements(By.CSS_SELECTOR, ".next-dialog-body button.next-btn-primary")
        candidates = [button for button in buttons if _is_visible(button) and _is_enabled(button)
                      and re.fullmatch(r"check(?:out)? it out", str(button.text or "").strip(), re.I)]
        if len(candidates) == 1:
            safe_click(driver, candidates[0])
            dismissed += 1
    return dismissed


def _dismiss_lazada_payment_reminder(driver):
    """Close the negative-balance reminder through its X, never its payment CTA."""
    reminders = []
    for dialog in _find_elements_checked(driver, By.CSS_SELECTOR, '[role="dialog"], [role="alertdialog"]'):
        if not _is_visible(dialog):
            continue
        text = re.sub(r"\s+", " ", str(dialog.text or "")).strip()
        classes = str(dialog.get_attribute("class") or "").split()
        if "negative-balance-dialog" in classes or re.match(r"^(?:付款提醒|payment reminder)(?:\s|$)", text, re.I):
            reminders.append(dialog)
    if not reminders:
        return 0
    if len(reminders) != 1:
        raise RuntimeError("存在多个付款提醒弹窗，无法唯一定位关闭控件，已停止采集")
    dialog = reminders[0]
    candidates = [element for element in dialog.find_elements(
        By.CSS_SELECTOR,
        'a.next-dialog-close[role="button"][aria-label="关闭"], '
        'a.next-dialog-close[role="button"][aria-label="Close"], '
        'a.next-dialog-close[role="button"][aria-label="close"]',
    ) if _is_visible(element) and _is_enabled(element)]
    if len(candidates) != 1:
        raise RuntimeError("付款提醒没有唯一安全关闭控件，已停止采集；不会点击付款或确认按钮")
    safe_click(driver, candidates[0])
    for _ in range(10):
        if not _is_visible(dialog):
            return 1
        time.sleep(0.2)
    raise RuntimeError("付款提醒未成功关闭，已停止采集；不会点击付款或确认按钮")


def close_lazada_popups(driver, max_rounds=2):
    """
    循环关闭 Lazada 页面中的提示弹窗
    返回: 关闭的弹窗数量
    """
    current_url = str(getattr(driver, "current_url", "") or "").lower()
    if "gsp.lazada.com/portal/home" in current_url:
        return 0

    total_closed = 0
    try:
        driver.implicitly_wait(0)
    except Exception:
        pass

    try:
        for round_idx in range(max_rounds):
            # A payment reminder must never reach the generic Confirm/OK
            # dismissers. An ambiguous or ineffective X is a hard stop.
            closed_this_round = _dismiss_lazada_payment_reminder(driver)
            try:
                closed_this_round += _dismiss_lazada_balance_welcome(driver)
            except StaleElementReferenceException:
                pass

            # Lazada My Balance may show a multi-step guided tour. Only click an
            # exact Skip button when its own overlay also contains a step counter
            # and an exact Next button, so table pagination is never mistaken for it.
            try:
                skipped_tour = driver.execute_script(r"""
/* lazada_close_guided_tour */
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
const skips = Array.from(document.querySelectorAll('button,a,span')).filter((el) => visible(el) && /^skip$/i.test(textOf(el)));
for (const skip of skips) {
  let scope = skip.parentElement;
  for (let depth = 0; scope && scope !== document.body && depth < 7; depth += 1) {
    const text = textOf(scope);
    const hasStep = /\b\d+\s*\/\s*\d+\b/.test(text);
    const hasNext = Array.from(scope.querySelectorAll('button,a,span')).some((el) => visible(el) && /^next$/i.test(textOf(el)));
    if (hasStep && hasNext) {
      (skip.closest('button,a') || skip).click();
      return true;
    }
    scope = scope.parentElement;
  }
}
return false;
""")
                if skipped_tour:
                    closed_this_round += 1
                    time.sleep(0.5)
            except Exception as exc:
                log.debug("关闭 Lazada 引导浮层失败: %s", exc)

            # 策略1: 查找包含关闭文本的按钮
            for text in POPUP_DISMISS_TEXTS:
                xpath = (
                    f"//button[contains(translate(normalize-space(.), "
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                    f"'{text.lower()}')]"
                    f"|//a[contains(translate(normalize-space(.), "
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                    f"'{text.lower()}')]"
                    f"|//span[contains(translate(normalize-space(.), "
                    f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                    f"'{text.lower()}')]/parent::button"
                )
                for element in _find_elements_checked(driver, By.XPATH, xpath):
                    try:
                        if _is_visible(element) and _is_enabled(element):
                            safe_click(driver, element)
                            closed_this_round += 1
                            time.sleep(0.5)
                    except StaleElementReferenceException:
                        continue

            # 策略2: 关闭模态框的 X 按钮
            close_selectors = [
                "[aria-label='Close']",
                "[aria-label='close']",
                "[class*='modal'] [class*='close']",
                "[class*='dialog'] [class*='close']",
                "[class*='modal'] [aria-label='Close']",
                "[class*='popup'] [class*='close']",
                ".ant-modal-close",
                ".lzd-modal-close",
            ]
            for selector in close_selectors:
                for element in _find_elements_checked(driver, By.CSS_SELECTOR, selector):
                    try:
                        if _is_visible(element):
                            safe_click(driver, element)
                            closed_this_round += 1
                            time.sleep(0.5)
                    except StaleElementReferenceException:
                        continue

            # 策略3: 按 Escape 键
            if closed_this_round == 0:
                try:
                    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                    time.sleep(0.3)
                except Exception:
                    pass

            total_closed += closed_this_round
            if closed_this_round == 0:
                break
            time.sleep(0.5)
    finally:
        try:
            driver.implicitly_wait(2)
        except Exception:
            pass

    if total_closed > 0:
        log.info(f"共关闭 {total_closed} 个弹窗")
    return total_closed


# ========== 财务页面导航 ==========


def open_lazada_finance_page(util):
    """
    优先打开目标国家已验证的 My Balance 路由，菜单仅作兼容兜底。
    """
    driver = util.driver
    _wait_page_ready(driver)
    time.sleep(2)

    country = _target_lazada_country(driver)
    finance_origin = _seller_center_origin_by_country(country).rstrip("/")
    if finance_origin:
        expected_host = urlsplit(finance_origin).hostname
        same_host = urlsplit(str(driver.current_url or "")).hostname == expected_host
        if same_host and _is_lazada_balance_unavailable(driver):
            return _recheck_lazada_balance_unavailable(driver)
        if same_host and _is_lazada_balance_page(driver):
            return True
        try:
            # Global SSO already ran above. Going straight to this country's
            # balance route avoids treating an income-statement landing page as
            # the only available finance feature (notably for Thai stores).
            driver.get(finance_origin + "/apps/balance")
            _wait_page_ready(driver)
            time.sleep(5)
            if urlsplit(str(driver.current_url or "")).hostname == expected_host:
                if _is_lazada_balance_unavailable(driver):
                    return _recheck_lazada_balance_unavailable(driver)
                if _is_expected_lazada_country_page(driver, "My Balance") and _is_lazada_balance_page(driver):
                    return True
        except Exception as exc:
            log.debug("直接打开目标国家 My Balance 失败，尝试菜单: %s", exc)

    menu_opened = _click_my_balance_menu(driver)
    if menu_opened:
        log.info("通过左侧菜单进入 My Balance 页面")
        _wait_page_ready(driver)
        time.sleep(3)
    if (
        _is_expected_lazada_country_page(driver, "Finance -> My Balance")
        and _is_lazada_balance_unavailable(driver)
    ):
        return _recheck_lazada_balance_unavailable(driver)
    if menu_opened:
        return (
            _is_expected_lazada_country_page(driver, "Finance -> My Balance")
            and _is_lazada_balance_page(driver)
        )

    # 部分新版 Seller Center 只有进入 My Income 后才渲染 Finance 子菜单。
    if finance_origin:
        bootstrap_url = finance_origin + "/portal/apps/finance/myIncome/index"
        try:
            log.info("My Balance 子菜单尚未渲染，先进入 My Income: %s", bootstrap_url)
            driver.get(bootstrap_url)
            _wait_page_ready(driver)
            time.sleep(5)
            menu_opened = _click_my_balance_menu(driver)
            if (
                _is_expected_lazada_country_page(driver, "Finance -> My Balance")
                and _is_lazada_balance_unavailable(driver)
            ):
                return _recheck_lazada_balance_unavailable(driver)
            if menu_opened:
                return (
                    _is_expected_lazada_country_page(driver, "Finance -> My Balance")
                    and _is_lazada_balance_page(driver)
                )
        except Exception as exc:
            log.debug("通过 My Income 初始化 Finance 子菜单失败: %s", exc)

    # 兜底：新 Seller Center 的 My Balance 通用路径。
    for target_url in _candidate_balance_urls(driver):
        try:
            log.info(f"菜单点击失败，尝试 My Balance URL: {target_url}")
            driver.get(target_url)
            _wait_page_ready(driver)
            time.sleep(5)
            if not _is_expected_lazada_country_page(driver, target_url):
                continue
            if _is_lazada_balance_unavailable(driver):
                return _recheck_lazada_balance_unavailable(driver)
            if _is_lazada_balance_page(driver):
                return True
        except Exception as e:
            log.debug(f"跳转 {target_url} 失败: {e}")
            continue

    current_url = str(driver.current_url or "")
    page_text = re.sub(r"\s+", " ", _safe_page_text(driver, limit=1000)).strip()
    log.error(
        "无法进入 Lazada My Balance 页面: 当前URL=%s, 页面摘要=%s",
        current_url,
        page_text[:300],
    )
    return False


def _recheck_lazada_balance_unavailable(driver):
    """Reopen the same country's balance page once to confirm access denial."""
    country = _target_lazada_country(driver)
    origin = _seller_center_origin_by_country(country).rstrip("/")
    if not origin or not _is_expected_lazada_country_page(driver, "My Balance 访问复核"):
        return False
    store_name = str(getattr(driver, "_ziniao_store_name", "") or "").strip()
    store_prefix = f"{store_name}：" if store_name else ""
    target_url = origin + "/apps/balance"
    log.warning(
        "%sMy Balance 返回未开放提示，重新打开同国家页面复核一次: %s",
        store_prefix,
        target_url,
    )
    try:
        driver.get(target_url)
        _wait_page_ready(driver)
        time.sleep(5)
    except Exception as exc:
        log.debug("%sMy Balance 访问复核失败: %s", store_prefix, exc)
        return False
    current_host = (urlsplit(str(driver.current_url or "")).hostname or "").lower()
    expected_host = (urlsplit(origin).hostname or "").lower()
    if (
        current_host != expected_host
        or not _is_expected_lazada_country_page(driver, "My Balance 访问复核")
    ):
        return False
    if _is_lazada_balance_unavailable(driver):
        log.error(
            "%s当前店铺的 Lazada My Balance 功能仍处于试点阶段，平台未开放访问",
            store_prefix,
        )
        return False
    return _is_lazada_balance_page(driver)


def _is_lazada_balance_page(driver):
    """Recognize old and new localized Lazada balance-page variants."""
    if _is_lazada_balance_unavailable(driver):
        return False
    current_url = str(driver.current_url or "").lower()
    page_text = re.sub(r"\s+", " ", _safe_page_text(driver, limit=5000)).strip().lower()
    strong_markers = (
        "withdrawable amount",
        "available balance",
        "balance transactions",
        "manage my balance",
        "可提现余额",
        "可用余额",
        "余额交易",
        "余额流水",
        "余额明细",
    )
    if any(marker in page_text for marker in strong_markers):
        return True
    # A visible My Balance sidebar item is also present on unrelated pages.
    # Without balance content, require the balance route before accepting it.
    return "balance" in current_url and any(
        marker in page_text for marker in ("balance", "withdraw", "余额", "提现")
    )


def _is_lazada_balance_unavailable(driver):
    current_url = str(driver.current_url or "").lower()
    page_text = re.sub(r"\s+", " ", _safe_page_text(driver, limit=3000)).strip().lower()
    if "/apps/balance/unaccessable" in current_url or "/apps/balance/unavailable" in current_url:
        return True
    return (
        "feature is in pilot phase" in page_text
        and "not accessible" in page_text
    )


def _click_my_balance_menu(driver):
    """点击新 Seller Center 左侧菜单 Finance -> My Balance。"""
    try:
        current_url = str(driver.current_url or "")
        country = _target_lazada_country(driver)
        current_country = _country_from_current_url(driver)
        current_host = (urlsplit(current_url).hostname or "").lower()
        keep_vn_gsp_menu = country == "VN" and current_host in {
            "gsp.lazada.com", "gsp.lazada-seller.cn"
        }
        needs_country_switch = (
            ("sellercenter.lazada" not in current_url and not keep_vn_gsp_menu)
            or bool(country and current_country and current_country != country)
        )
        if needs_country_switch and country:
            log.info(f"当前不在 Seller Center，准备打开 {country or '默认'} Seller Center")
            driver.get(_seller_center_origin_by_country(country))
            _wait_page_ready(driver)
            time.sleep(5)
    except Exception:
        pass

    balance_xpaths = (
        "//a[normalize-space()='My Balance']",
        "//*[normalize-space()='My Balance' and (self::a or self::li or self::span)]",
        "//a[normalize-space()='我的余额']",
        "//*[normalize-space()='我的余额' and (self::a or self::li or self::span)]",
        "//a[contains(@href, '/apps/balance') and not(contains(@href, 'marketing-balance'))]",
    )

    def click_visible_balance():
        for xpath in balance_xpaths:
            for element in driver.find_elements(By.XPATH, xpath):
                try:
                    if _is_visible(element):
                        log.info("点击 Finance -> My Balance")
                        safe_click(driver, element)
                        time.sleep(5)
                        _wait_page_ready(driver)
                        return (
                            _is_expected_lazada_country_page(driver, "Finance -> My Balance")
                            and not _is_lazada_balance_unavailable(driver)
                            and _is_lazada_balance_page(driver)
                        )
                except StaleElementReferenceException:
                    continue
        return None

    # If Finance is already expanded, clicking its parent can hide the target.
    balance_result = click_visible_balance()
    if balance_result is not None:
        return balance_result

    finance_xpaths = (
        "//li[normalize-space()='Finance' or @title='Finance']",
        "//*[normalize-space()='Finance']/ancestor::li[1]",
        "//*[normalize-space()='财务']/ancestor::li[1]",
        "//*[normalize-space()='Finance']",
    )
    finance_found = False
    for xpath in finance_xpaths:
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if _is_visible(element):
                    if str(element.get_attribute("aria-expanded") or "").strip().lower() != "true":
                        log.info("点击左侧 Finance 菜单")
                        safe_click(driver, element)
                    finance_found = True
                    time.sleep(1.5)
                    break
            except StaleElementReferenceException:
                continue
        if finance_found:
            break

    return bool(click_visible_balance())


def open_lazada_withdrawal_page(util):
    """Verify the read-only history page without clicking a payout action."""
    driver = util.driver
    _wait_page_ready(driver)
    # Withdrawal is both a history filter and a real payment action. The
    # filter is selected separately, inside Balance Transactions only.
    return (
        _is_expected_lazada_country_page(driver, "余额交易页面")
        and _is_lazada_balance_page(driver)
    )


# ========== 日期范围设置 ==========


def scroll_to_balance_transactions(driver):
    """Scroll the My Balance page to the Balance Transactions block."""
    js_code = """
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
const heading = nodes.find(el => {
  const text = textOf(el);
  return /^balance\\s+transactions$/i.test(text) || /^余额(?:交易|流水)$/.test(text);
}) || nodes.find(el => textOf(el).toLowerCase().includes('balance transactions'));
if (!heading) return false;
heading.scrollIntoView({block: 'center', inline: 'nearest'});
return true;
"""
    try:
        if driver.execute_script(js_code):
            time.sleep(0.8)
            return True
    except Exception as e:
        log.debug(f"定位 Balance Transactions 失败: {e}")
    return False


def position_balance_transactions_for_evidence_screenshot(driver):
    """Place the selected filter, submitted dates, and result header in view."""
    js_code = r"""
/* lazada_balance_evidence_view */
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
const heading = nodes.find((el) => {
  const text = textOf(el);
  return /^balance\s+transactions$/i.test(text) || /^余额(?:交易|流水)$/.test(text);
}) || nodes.find((el) => textOf(el).toLowerCase().includes('balance transactions'));
if (!heading) return {heading_found: false, date_found: false, table_found: false};
const headingY = heading.getBoundingClientRect().top + window.scrollY;
const dateControl = document.querySelector(
  '#transactionDateRange,.next-range-picker,.ant-picker-range,[class*="range-picker" i]'
)
  || Array.from(document.querySelectorAll('input')).filter(visible).find((el) => /^\d{4}-\d{2}-\d{2}$/.test(String(el.value || '').trim()))
  || nodes.find((el) => {
    const y = el.getBoundingClientRect().top + window.scrollY;
    return y >= headingY - 20 && y <= headingY + 900
      && /\d{4}-\d{2}-\d{2}\s*(?:-|~|至|to)\s*\d{4}-\d{2}-\d{2}/i.test(textOf(el));
  });
const tableSelectors = 'table,[role="table"],.next-table,.ant-table,[class*="table-wrapper" i],[class*="table-container" i]';
const table = Array.from(document.querySelectorAll(tableSelectors)).filter(visible).find((el) => {
  const y = el.getBoundingClientRect().top + window.scrollY;
  return y >= headingY - 20 && y <= headingY + 2500;
});
window.scrollTo({top: Math.max(0, headingY - 24), behavior: 'instant'});
return {heading_found: true, date_found: Boolean(dateControl), table_found: Boolean(table)};
"""
    try:
        evidence = driver.execute_script(js_code)
        if isinstance(evidence, dict):
            withdrawal_selected = _is_withdrawal_filter_selected(driver)
            log.info(
                "查询截图视口定位: Withdrawal=%s, 日期=%s, 结果表=%s",
                "已选中" if withdrawal_selected else "未确认",
                "可见" if evidence.get("date_found") else "未识别",
                "可见" if evidence.get("table_found") else "未识别",
            )
            positioned = (
                evidence.get("heading_found") is True
                and withdrawal_selected
                and evidence.get("date_found") is True
                and evidence.get("table_found") is True
            )
        else:
            positioned = bool(evidence)
        if positioned:
            time.sleep(0.5)
        return positioned
    except Exception as exc:
        log.debug("定位 Balance Transactions 查询证据视口失败: %s", exc)
        return False


def select_withdrawal_filter(driver):
    """Select Type = Withdrawal inside Balance Transactions."""
    scroll_to_balance_transactions(driver)
    previous_signature = _balance_transactions_page_signature(driver)
    js_code = """
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
const heading = nodes.find(el => {
  const text = textOf(el);
  return /^balance\\s+transactions$/i.test(text) || /^余额(?:交易|流水)$/.test(text);
}) || nodes.find(el => textOf(el).toLowerCase().includes('balance transactions'));
const headingY = heading ? heading.getBoundingClientRect().top + window.scrollY : 0;
const candidates = nodes.filter(el => {
  const text = textOf(el);
  if (!/^(withdrawal|提现)$/i.test(text)) return false;
  if (el.closest('tr,[role="row"],table,.next-table-row,.ant-table-row,[class*="table-row" i]')) return false;
  const y = el.getBoundingClientRect().top + window.scrollY;
  return y >= headingY - 20 && y <= headingY + 900;
});
for (const el of candidates) {
  const target = el.closest('label,button,a,[role="tab"],[class*="tab" i]') || el;
  target.scrollIntoView({block: 'center', inline: 'nearest'});
  target.click();
  return true;
}
return false;
"""
    try:
        if driver.execute_script(js_code):
            if _wait_for_withdrawal_filter_applied(driver, previous_signature):
                log.info("已在 Balance Transactions 中选择并确认 Withdrawal")
                return True
            log.warning("已点击 Withdrawal，但未确认筛选状态或表格刷新")
            return False
    except Exception as e:
        log.debug(f"选择 Withdrawal 失败: {e}")

    for element in driver.find_elements(
        By.XPATH,
        "//*[normalize-space()='Withdrawal' or normalize-space()='提现']",
    ):
        try:
            is_filter_control = driver.execute_script(
                "return !arguments[0].closest('tr,[role=\"row\"],table,.next-table-row,.ant-table-row,[class*=\"table-row\" i]');",
                element,
            )
            if is_filter_control and _is_visible(element):
                safe_click(driver, element)
                if _wait_for_withdrawal_filter_applied(driver, previous_signature):
                    log.info("已点击并确认 Withdrawal")
                    return True
                log.warning("已点击 Withdrawal，但未确认筛选状态或表格刷新")
                return False
        except StaleElementReferenceException:
            continue
    return False


def _wait_for_withdrawal_filter_applied(driver, previous_signature, timeout=10):
    deadline = time.monotonic() + max(float(timeout), 0.0)
    while time.monotonic() < deadline:
        if _is_withdrawal_filter_selected(driver):
            time.sleep(0.35)
            return True
        time.sleep(0.25)
    return False


def _is_withdrawal_filter_selected(driver):
    js_code = r"""
/* lazada_withdrawal_filter_selected */
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
const heading = nodes.find((el) => /^balance\s+transactions$/i.test(textOf(el)) || /^\u4f59\u989d(?:\u4ea4\u6613|\u6d41\u6c34)$/.test(textOf(el)));
if (!heading) return false;
const headingY = heading.getBoundingClientRect().top + window.scrollY;
const candidates = nodes.filter((el) => {
  if (!/^(withdrawal|\u63d0\u73b0)$/i.test(textOf(el))) return false;
  if (el.closest('tr,[role="row"],table,.next-table-row,.ant-table-row,[class*="table-row" i]')) return false;
  const y = el.getBoundingClientRect().top + window.scrollY;
  return y >= headingY - 20 && y <= headingY + 900;
});
return candidates.some((element) => {
  const control = element.closest('label,button,a,[role="tab"],[class*="tab" i]') || element;
  let node = control;
  for (let depth = 0; node && node !== document.body && depth < 5; depth += 1) {
    if (node.closest('tr,[role="row"],table')) return false;
    const className = String(node.className || '');
    if (
      node.tagName === 'BUTTON'
      && /(^|\s)next-btn-primary(\s|$)/i.test(className)
      && /(^|\s)button-groups(\s|$)/i.test(className)
    ) return true;
    const input = node.matches('input') ? node : node.querySelector('input');
    if (input && input.checked) return true;
    const selectedValues = [
      node.getAttribute('aria-selected'), node.getAttribute('aria-checked'), node.getAttribute('data-selected')
    ];
    if (selectedValues.some((value) => String(value || '').toLowerCase() === 'true')) return true;
    if (/(^|[\s_-])(active|selected|checked)([\s_-]|$)/i.test(className)) return true;
    node = node.parentElement;
  }
  return false;
});
"""
    try:
        return bool(driver.execute_script(js_code))
    except Exception:
        return False


def set_trade_date_range(driver, start_date, end_date):
    """
    设置交易时间范围
    返回: 是否设置成功
    """
    log.info(f"设置交易时间范围: {start_date} ~ {end_date}")
    scroll_to_balance_transactions(driver)
    if not _wait_for_balance_transactions_stable(driver):
        log.warning("日期操作前 Balance Transactions 尚未稳定，停止设置日期")
        return False
    previous_range = _get_balance_date_range_text(driver)
    previous_signature = _balance_transactions_page_signature(driver)
    if not previous_signature:
        log.warning("日期操作前无法取得 Balance Transactions 基线，停止设置日期")
        return False

    log.info(
        "Balance Transactions 日期预检: 当前=%s, 目标=%s~%s, 表格基线=%s",
        previous_range or "未读取到",
        start_date,
        end_date,
        "已取得" if previous_signature else "未取得",
    )
    if _date_range_text_matches(previous_range, start_date, end_date):
        log.info(
            "Balance Transactions 日期范围原本已是目标值，复用已稳定表格: %s ~ %s",
            start_date,
            end_date,
        )
        return True

    unchanged_result_evidence = _capture_unchanged_result_equivalence_evidence(
        driver,
        previous_range,
        start_date,
        end_date,
        previous_signature,
    )
    if unchanged_result_evidence:
        fingerprint = unchanged_result_evidence.get("fingerprint", {})
        log.info(
            "Balance Transactions 已在日期提交前取得严格等价基线: 旧范围=%s, 记录数=%s, 空态=%s",
            previous_range,
            len(fingerprint.get("records", ())),
            bool(fingerprint.get("empty")),
        )

    def attempt(setter):
        _install_balance_transactions_refresh_probe(driver)
        try:
            if not setter():
                return False
            return _verify_submitted_balance_date_range(
                driver,
                start_date,
                end_date,
                previous_range,
                previous_signature,
                unchanged_result_evidence,
            )
        finally:
            _clear_balance_transactions_refresh_probe(driver)

    if attempt(lambda: _set_balance_transaction_date_range(driver, start_date, end_date)):
        return True

    # 策略1: 查找日期输入框并直接输入
    date_inputs = _find_date_inputs(driver)
    if date_inputs and len(date_inputs) >= 2:
        if attempt(lambda: _input_date_range(
            driver, date_inputs[0], date_inputs[1], start_date, end_date
        )):
            return True

    # 策略2: 查找日期范围选择器组件
    if attempt(lambda: _handle_date_picker_component(driver, start_date, end_date)):
        return True

    # 策略3: 通过 JavaScript 设置日期值
    if attempt(lambda: _js_set_date_range(driver, start_date, end_date)):
        return True

    # 策略4: 查找并点击日期范围预设按钮（如 "Last 30 days" 等）
    if attempt(lambda: _select_date_preset(driver, start_date, end_date)):
        return True

    log.warning("未能设置日期范围")
    return False


def _verify_submitted_balance_date_range(
    driver,
    start_date,
    end_date,
    previous_range="",
    previous_signature="",
    unchanged_result_evidence=None,
):
    time.sleep(0.8)
    _wait_page_ready(driver, timeout=10)
    if _verify_balance_date_range(driver, start_date, end_date):
        if _date_range_text_matches(previous_range, start_date, end_date):
            return _wait_for_balance_transactions_stable(driver)
        if _wait_for_balance_transactions_refresh(driver, previous_signature):
            return True
        if _prove_unchanged_result_equivalence(
            driver,
            start_date,
            end_date,
            previous_signature,
            unchanged_result_evidence,
        ):
            log.info(
                "Balance Transactions 内容未变，但已证明旧范围完整结果与目标范围等价: %s ~ %s",
                start_date,
                end_date,
            )
            return True
        log.warning("日期显示已更新，但未观察到 Balance Transactions 数据刷新，停止写入")
        return False
    visible = _get_balance_date_range_text(driver)
    log.warning(
        "日期操作未通过最终校验：期望=%s~%s，页面=%s",
        start_date,
        end_date,
        visible or "未读取到",
    )
    return False


def _set_balance_transaction_date_range(driver, start_date, end_date):
    """Set Lazada Balance Transactions date range and verify the visible value."""
    try:
        if _open_balance_date_picker(driver) and _set_range_picker_by_calendar_clicks(driver, start_date, end_date):
            log.info(f"已通过日历格子选择日期范围: {start_date} ~ {end_date}")
        else:
            inputs = _find_balance_date_inputs(driver)
            if len(inputs) >= 2:
                _set_input_value(driver, inputs[0], start_date)
                _set_input_value(driver, inputs[1], end_date)
            elif len(inputs) == 1:
                _set_input_value(driver, inputs[0], f"{start_date} - {end_date}")
            else:
                log.debug("未找到 Balance Transactions 日期输入框")
                return False

        if not _click_date_picker_confirm(driver):
            try:
                driver.switch_to.active_element.send_keys(Keys.ENTER)
            except Exception:
                pass
        if not _wait_date_picker_closed(driver, timeout=5):
            log.warning("日期选择面板未关闭，日期范围尚未提交")
            return False
        time.sleep(1)
        _wait_page_ready(driver, timeout=10)

        if _verify_balance_date_range(driver, start_date, end_date):
            log.info(f"Balance Transactions 日期范围已设置并校验通过: {start_date} ~ {end_date}")
            return True

        visible = _get_balance_date_range_text(driver)
        log.warning(f"日期范围设置后页面显示未匹配，当前显示: {visible or '未读取到'}")
        return False
    except Exception as e:
        log.debug(f"Balance Transactions 日期范围设置失败: {e}")
        return False


def _find_balance_date_range_box(driver):
    try:
        element = driver.find_element(By.CSS_SELECTOR, "#transactionDateRange")
        if _is_visible(element):
            return element
    except Exception:
        pass
    js_code = """
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
const direct = document.querySelector('#transactionDateRange');
if (direct && visible(direct)) return direct;
const heading = nodes.find(el => {
  const text = textOf(el);
  return /^balance\\s+transactions$/i.test(text) || /^余额(?:交易|流水)$/.test(text);
}) || nodes.find(el => textOf(el).toLowerCase().includes('balance transactions'));
const headingY = heading ? heading.getBoundingClientRect().top + window.scrollY : 0;
const candidates = nodes.filter(el => {
  const text = textOf(el);
  if (!/\\d{4}-\\d{2}-\\d{2}\\s*-\\s*\\d{4}-\\d{2}-\\d{2}/.test(text)) return false;
  const rect = el.getBoundingClientRect();
  const y = rect.top + window.scrollY;
  return y >= headingY - 20 && y <= headingY + 900 && rect.width >= 120 && rect.height >= 20;
});
candidates.sort((a, b) => (a.getBoundingClientRect().width * a.getBoundingClientRect().height) - (b.getBoundingClientRect().width * b.getBoundingClientRect().height));
return candidates[0] || null;
"""
    try:
        return driver.execute_script(js_code)
    except Exception:
        return None


def _open_balance_date_picker(driver):
    js_code = """
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const root = document.querySelector('#transactionDateRange');
if (!root || !visible(root)) return false;
const trigger = root.querySelector('input[placeholder="Start Date"]')
  || root.querySelector('input')
  || root.querySelector('.next-range-picker-trigger')
  || root;
trigger.scrollIntoView({block: 'center', inline: 'center'});
trigger.click();
return true;
"""
    try:
        if not driver.execute_script(js_code):
            return False
        return _wait_date_picker_open(driver, timeout=5)
    except Exception as e:
        log.debug(f"打开日期选择面板失败: {e}")
        return False


def _set_range_picker_by_calendar_clicks(driver, start_date, end_date):
    try:
        if not _click_range_picker_date(driver, start_date):
            return False
        if not _click_range_picker_date(driver, end_date):
            return False
        visible = _get_balance_date_range_text(driver)
        log.info(f"日历格子选择后当前日期范围: {visible or '未读取到'}")
        return True
    except Exception as e:
        log.debug(f"日历格子选择日期失败: {e}")
        return False


def _click_range_picker_date(driver, date_str):
    if not _ensure_range_picker_date_visible(driver, date_str):
        log.debug(f"日期格子不可见: {date_str}, 当前可见={_get_visible_range_picker_titles(driver)[:4]}")
        return False
    js_code = """
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const date = arguments[0];
const cells = Array.from(document.querySelectorAll('.next-range-picker-body td[title]')).filter(visible);
const cell = cells.find(td => td.getAttribute('title') === date && td.getAttribute('aria-disabled') !== 'true');
if (!cell) return false;
cell.scrollIntoView({block: 'center', inline: 'center'});
cell.click();
return true;
"""
    try:
        if driver.execute_script(js_code, date_str):
            time.sleep(0.6)
            return True
    except Exception as e:
        log.debug(f"点击日期格子失败 {date_str}: {e}")
    return False


def _ensure_range_picker_date_visible(driver, date_str):
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return False
    for _ in range(24):
        titles = _get_visible_range_picker_titles(driver)
        if date_str in titles:
            return True
        dates = []
        for title in titles:
            try:
                dates.append(datetime.strptime(title, "%Y-%m-%d").date())
            except ValueError:
                continue
        if not dates:
            return False
        if target > max(dates):
            _click_range_picker_month_nav(driver, 1)
        elif target < min(dates):
            _click_range_picker_month_nav(driver, -1)
        else:
            return False
    return False


def _get_visible_range_picker_titles(driver):
    js_code = """
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
return Array.from(document.querySelectorAll('.next-range-picker-body td[title]'))
  .filter(visible)
  .map(td => td.getAttribute('title'))
  .filter(Boolean);
"""
    try:
        return list(driver.execute_script(js_code) or [])
    except Exception:
        return []


def _click_range_picker_month_nav(driver, direction):
    selector = (
        ".next-range-picker-body .next-calendar-btn-next-month"
        if direction > 0
        else ".next-range-picker-body .next-calendar-btn-prev-month"
    )
    try:
        driver.execute_script(
            "const btn = document.querySelector(arguments[0]); if (btn) btn.click();",
            selector,
        )
        time.sleep(0.35)
        return True
    except Exception:
        return False


def _find_balance_date_inputs(driver):
    js_code = """
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
const heading = nodes.find(el => {
  const text = textOf(el);
  return /^balance\\s+transactions$/i.test(text) || /^余额(?:交易|流水)$/.test(text);
}) || nodes.find(el => textOf(el).toLowerCase().includes('balance transactions'));
const headingY = heading ? heading.getBoundingClientRect().top + window.scrollY : 0;
const rangeBody = Array.from(document.querySelectorAll('.next-range-picker-body')).find(visible);
if (rangeBody) {
  const panelInputs = Array.from(rangeBody.querySelectorAll('input')).filter(visible);
  if (panelInputs.length >= 2) return panelInputs.slice(0, 2);
}
const overlayInputs = Array.from(document.querySelectorAll(
  '.next-overlay-wrapper input, .next-date-picker input, .ant-picker-dropdown input, [class*="date-picker"] input, [class*="datepicker"] input'
)).filter(visible);
if (overlayInputs.length >= 2) {
  overlayInputs.sort((a, b) => {
    const ra = a.getBoundingClientRect();
    const rb = b.getBoundingClientRect();
    return (ra.top - rb.top) || (ra.left - rb.left);
  });
  return overlayInputs.slice(0, 2);
}
let inputs = Array.from(document.querySelectorAll('input')).filter(visible).filter(el => {
  const rect = el.getBoundingClientRect();
  const y = rect.top + window.scrollY;
  const text = [el.value, el.placeholder, textOf(el.closest('div') || el)].join(' ');
  if (y < headingY - 30 || y > headingY + 900) return false;
  if (/transaction\\s*number/i.test(text)) return false;
  return /\\d{4}-\\d{2}-\\d{2}|date|time|yyyy/i.test(text);
});
if (inputs.length === 0) {
  inputs = Array.from(document.querySelectorAll('.next-overlay-wrapper input, .next-date-picker input, .ant-picker-dropdown input, [class*="date"] input')).filter(visible);
}
inputs.sort((a, b) => {
  const ra = a.getBoundingClientRect();
  const rb = b.getBoundingClientRect();
  return (ra.top - rb.top) || (ra.left - rb.left);
});
return inputs.slice(0, 2);
"""
    try:
        result = driver.execute_script(js_code) or []
        return [item for item in result if item]
    except Exception:
        return []


def _set_input_value(driver, element, value):
    safe_click(driver, element)
    time.sleep(0.2)
    driver.execute_script(
        """
const el = arguments[0];
const value = arguments[1];
const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
el.focus();
setter.call(el, value);
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
el.dispatchEvent(new Event('blur', {bubbles: true}));
""",
        element,
        value,
    )


def _set_range_picker_panel_values(driver, start_date, end_date):
    js_code = """
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const body = Array.from(document.querySelectorAll('.next-range-picker-body')).find(visible);
if (!body) return {ok: false, reason: 'range body not found'};
const inputs = Array.from(body.querySelectorAll('input')).filter(visible);
if (inputs.length < 2) return {ok: false, reason: 'panel inputs not found', count: inputs.length};
const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
function setValue(el, value) {
  el.focus();
  setter.call(el, '');
  el.dispatchEvent(new Event('input', {bubbles: true}));
  setter.call(el, value);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
}
setValue(inputs[0], arguments[0]);
setValue(inputs[1], arguments[1]);
inputs[1].dispatchEvent(new Event('blur', {bubbles: true}));
return {ok: true, values: [inputs[0].value, inputs[1].value]};
"""
    try:
        result = driver.execute_script(js_code, start_date, end_date) or {}
        if result.get("ok"):
            log.info(f"日期弹层输入框已设置: {result.get('values')}")
            return True
        log.debug(f"日期弹层输入框设置失败: {result}")
    except Exception as e:
        log.debug(f"日期弹层输入框设置异常: {e}")
    return False


def _click_date_picker_confirm(driver):
    js_code = """
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
const rangeBody = Array.from(document.querySelectorAll('.next-range-picker-body')).find(visible);
if (rangeBody) {
  const ok = Array.from(rangeBody.querySelectorAll('button, .next-btn')).filter(visible)
    .find(btn => /^OK$/i.test(textOf(btn)) || String(btn.className || '').includes('next-btn-primary'));
  if (ok) {
    ok.scrollIntoView({block: 'center', inline: 'center'});
    ok.click();
    return true;
  }
}
const overlays = Array.from(document.querySelectorAll(
  '.next-overlay-wrapper, .next-date-picker, .ant-picker-dropdown, [class*="date-picker"], [class*="datepicker"]'
)).filter(visible);
const overlay = overlays.find(el => Array.from(el.querySelectorAll('input')).filter(visible).length >= 2)
  || overlays.sort((a, b) => b.getBoundingClientRect().width * b.getBoundingClientRect().height - a.getBoundingClientRect().width * a.getBoundingClientRect().height)[0];
const root = overlay || document;
const buttons = Array.from(root.querySelectorAll('button, [role="button"], .next-btn')).filter(visible);
const okButton = buttons.find(btn => /^OK$/i.test(textOf(btn)))
  || buttons.find(btn => /confirm|apply|确定|确认/i.test(textOf(btn)));
if (!okButton) return false;
okButton.scrollIntoView({block: 'center', inline: 'center'});
okButton.click();
return true;
"""
    try:
        if driver.execute_script(js_code):
            log.info("已点击日期选择面板 OK")
            time.sleep(0.5)
            return True
    except Exception as e:
        log.debug(f"JS 点击日期 OK 失败: {e}")

    keywords = ("OK", "Ok", "Confirm", "Apply", "确定", "确认")
    for keyword in keywords:
        xpath = f"//button[normalize-space()='{keyword}' or .//*[normalize-space()='{keyword}']]"
        for button in driver.find_elements(By.XPATH, xpath):
            try:
                if _is_visible(button) and _is_enabled(button):
                    safe_click(driver, button)
                    time.sleep(0.5)
                    return True
            except StaleElementReferenceException:
                continue
    return False


def _wait_date_picker_closed(driver, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_date_picker_open(driver):
            return True
        time.sleep(0.3)
    return not _is_date_picker_open(driver)


def _wait_date_picker_open(driver, timeout=4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_date_picker_open(driver):
            return True
        time.sleep(0.2)
    return _is_date_picker_open(driver)


def _is_date_picker_open(driver):
    js_code = """
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
if (Array.from(document.querySelectorAll('.next-range-picker-body')).some(visible)) return true;
const overlays = Array.from(document.querySelectorAll(
  '.next-overlay-wrapper, .next-date-picker, .ant-picker-dropdown, [class*="date-picker"], [class*="datepicker"]'
)).filter(visible);
return overlays.some(el => Array.from(el.querySelectorAll('input')).filter(visible).length >= 2);
"""
    try:
        return bool(driver.execute_script(js_code))
    except Exception:
        return False


def _get_balance_date_range_text(driver):
    js_code = """
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
const rangeRoot = document.querySelector('#transactionDateRange');
if (rangeRoot && visible(rangeRoot)) {
  const values = Array.from(rangeRoot.querySelectorAll('input'))
    .filter(visible)
    .map(el => String(el.value || '').trim())
    .filter(value => /^\\d{4}-\\d{2}-\\d{2}$/.test(value));
  if (values.length >= 2) return values[0] + ' - ' + values[1];
}
const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
const heading = nodes.find(el => {
  const text = textOf(el);
  return /^balance\\s+transactions$/i.test(text) || /^余额(?:交易|流水)$/.test(text);
}) || nodes.find(el => textOf(el).toLowerCase().includes('balance transactions'));
const headingY = heading ? heading.getBoundingClientRect().top + window.scrollY : 0;
const texts = [];
for (const el of nodes) {
  const rect = el.getBoundingClientRect();
  const y = rect.top + window.scrollY;
  if (y < headingY - 30 || y > headingY + 900) continue;
  const text = [el.value, textOf(el)].filter(Boolean).join(' ');
  const match = text.match(/\\d{4}-\\d{2}-\\d{2}\\s*-\\s*\\d{4}-\\d{2}-\\d{2}/);
  if (match) texts.push(match[0]);
}
return texts[0] || '';
"""
    try:
        return str(driver.execute_script(js_code) or "")
    except Exception:
        return ""


def _verify_balance_date_range(driver, start_date, end_date):
    visible = _get_balance_date_range_text(driver)
    return _date_range_text_matches(visible, start_date, end_date)


def _date_range_text_matches(value, start_date, end_date):
    endpoints = re.findall(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    return len(endpoints) >= 2 and tuple(endpoints[:2]) == (str(start_date), str(end_date))


def _parse_exact_balance_date_range(value):
    endpoints = re.findall(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    if len(endpoints) != 2:
        return None
    try:
        start = datetime.strptime(endpoints[0], "%Y-%m-%d").date()
        end = datetime.strptime(endpoints[1], "%Y-%m-%d").date()
    except ValueError:
        return None
    if start > end:
        return None
    return start, end


def _parse_withdrawal_transaction_date(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    candidates = (
        (r"\b\d{4}-\d{2}-\d{2}\b", ("%Y-%m-%d",)),
        (r"\b\d{4}/\d{2}/\d{2}\b", ("%Y/%m/%d",)),
        (r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b", ("%d %b %Y", "%d %B %Y")),
    )
    for pattern, formats in candidates:
        match = re.search(pattern, text)
        if not match:
            continue
        for fmt in formats:
            try:
                return datetime.strptime(match.group(0), fmt).date()
            except ValueError:
                continue
    return None


def _complete_single_page_result_fingerprint(driver, target_start, target_end):
    """Return a result fingerprint only when the scoped table is provably complete."""
    if _is_balance_transactions_loading(driver):
        return None
    if not _balance_transactions_single_page_evidence(driver):
        return None

    records, metadata = _collect_all_withdrawal_transaction_records(driver)
    candidate_count = int(metadata.get("candidate_count", 0) or 0)
    if not metadata.get("complete") or int(metadata.get("pages", 0) or 0) != 1:
        return None
    if metadata.get("root_found") is not True or metadata.get("used_body_fallback"):
        return None
    if any(
        int(metadata.get(key, 0) or 0) != 0
        for key in ("unparsed_candidate_count", "conflict_count", "weak_identity_count")
    ):
        return None
    records = list(records or [])
    if candidate_count != len(records):
        return None

    if not records:
        if candidate_count or not _confirm_balance_transactions_empty_state(driver):
            return None
        return {"empty": True, "candidate_count": 0, "records": ()}

    identities = []
    fingerprints = []
    for record in records:
        identity = _withdrawal_record_identity(record)
        if not identity or identity[0] not in ("transaction", "bank_reference"):
            return None
        transaction_date = _parse_withdrawal_transaction_date(record.get("time", ""))
        if transaction_date is None or not (target_start <= transaction_date <= target_end):
            return None
        identities.append(identity)
        fingerprints.append(repr((identity, _withdrawal_record_payload(record))))
    if len(set(identities)) != len(identities):
        return None
    return {
        "empty": False,
        "candidate_count": candidate_count,
        "records": tuple(sorted(fingerprints)),
    }


def _capture_unchanged_result_equivalence_evidence(
    driver,
    previous_range,
    start_date,
    end_date,
    previous_signature,
):
    """Capture a fail-closed proof that an unchanged old result is valid for the target."""
    previous_dates = _parse_exact_balance_date_range(previous_range)
    target_dates = _parse_exact_balance_date_range(f"{start_date} - {end_date}")
    if not previous_dates or not target_dates or previous_dates == target_dates:
        return None
    previous_start, previous_end = previous_dates
    target_start, target_end = target_dates
    if not (previous_start <= target_start <= target_end <= previous_end):
        return None
    if not previous_signature or not _is_withdrawal_filter_selected(driver):
        return None
    if _parse_exact_balance_date_range(_get_balance_date_range_text(driver)) != previous_dates:
        return None
    if _balance_transactions_page_signature(driver) != previous_signature:
        return None

    fingerprint = _complete_single_page_result_fingerprint(driver, target_start, target_end)
    if fingerprint is None:
        return None
    if _is_balance_transactions_loading(driver) or not _is_withdrawal_filter_selected(driver):
        return None
    if _balance_transactions_page_signature(driver) != previous_signature:
        return None
    if _parse_exact_balance_date_range(_get_balance_date_range_text(driver)) != previous_dates:
        return None
    return {
        "previous_dates": previous_dates,
        "target_dates": target_dates,
        "signature": str(previous_signature),
        "fingerprint": fingerprint,
    }


def _prove_unchanged_result_equivalence(
    driver,
    start_date,
    end_date,
    previous_signature,
    evidence,
):
    """Re-attest the exact complete result after an unobservable date submission."""
    if not isinstance(evidence, dict):
        return False
    target_dates = _parse_exact_balance_date_range(f"{start_date} - {end_date}")
    if not target_dates or evidence.get("target_dates") != target_dates:
        return False
    if str(evidence.get("signature", "")) != str(previous_signature or ""):
        return False
    if not _is_withdrawal_filter_selected(driver):
        return False
    if not _verify_balance_date_range(driver, start_date, end_date):
        return False
    if _is_balance_transactions_loading(driver):
        return False
    if _balance_transactions_page_signature(driver) != str(previous_signature or ""):
        return False
    if not _wait_for_balance_transactions_stable(driver):
        return False

    current = _complete_single_page_result_fingerprint(driver, *target_dates)
    if current is None or current != evidence.get("fingerprint"):
        return False
    return (
        not _is_balance_transactions_loading(driver)
        and _is_withdrawal_filter_selected(driver)
        and _verify_balance_date_range(driver, start_date, end_date)
        and _balance_transactions_page_signature(driver) == str(previous_signature or "")
    )


def _install_balance_transactions_refresh_probe(driver):
    """Observe mutations scoped to the canonical Balance Transactions table."""
    js_code = r"""
/* lazada_balance_refresh_probe_install */
try {
  const prior = window.__lazadaBalanceRefreshProbe;
  if (prior && prior.observer) prior.observer.disconnect();
  delete window.__lazadaBalanceRefreshProbe;
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const textOf = (el) => String(el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
  const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
  const heading = nodes.find((el) => /^balance\s+transactions$/i.test(textOf(el)) || /^\u4f59\u989d(?:\u4ea4\u6613|\u6d41\u6c34)$/.test(textOf(el)));
  if (!heading) return false;
  const headingY = heading.getBoundingClientRect().top + window.scrollY;
  const rootSelectors = 'table,[role="table"],.next-table,.ant-table,[class*="table-wrapper" i],[class*="table-container" i],[class*="data-table" i]';
  const bodySelector = 'tbody,[role="rowgroup"],.next-table-body,.ant-table-tbody,[class*="table-body" i]';
  const rowSelector = 'tbody tr,[role="rowgroup"] [role="row"],.next-table-row,.ant-table-row,[class*="table-row" i],[class*="TableRow"]';
  const loadingSelector = '[aria-busy="true"],.next-loading,.ant-spin-spinning,[class*="loading" i],[class*="spinner" i],[class*="skeleton" i]';
  const headerPattern = /^(transaction\s*(number|no\.?|id|time)|type|sub\s*type|amount|remarks?|\u4ea4\u6613\u53f7|\u4ea4\u6613\u65f6\u95f4|\u6d41\u6c34\u7f16\u53f7|\u7c7b\u578b|\u5b50\u7c7b\u578b|\u91d1\u989d|\u5907\u6ce8)$/i;
  const emptyPattern = /(^|\s)(no\s+(data|records?|results?)(\s+found)?|\u6682\u65e0\u6570\u636e|\u6ca1\u6709\u6570\u636e)(\s|$)/i;
  const roots = Array.from(document.querySelectorAll(rootSelectors)).filter(visible).filter((root) => {
    const y = root.getBoundingClientRect().top + window.scrollY;
    if (y < headingY - 20 || y > headingY + 2500) return false;
    const headers = Array.from(root.querySelectorAll('th,[role="columnheader"]')).map(textOf).filter((text) => headerPattern.test(text));
    const hasSchema = headers.some((text) => /^(type|\u7c7b\u578b)$/i.test(text))
      && headers.some((text) => /^(amount|\u91d1\u989d)$/i.test(text))
      && headers.some((text) => /^(transaction\s*(number|no\.?|id|time)|\u4ea4\u6613\u53f7|\u4ea4\u6613\u65f6\u95f4|\u6d41\u6c34\u7f16\u53f7)$/i.test(text));
    const hasBodyState = Boolean(root.querySelector('tbody td,[role="rowgroup"] [role="cell"],.next-table-body .next-table-cell,.ant-table-tbody .ant-table-cell,[class*="table-body" i] [class*="table-cell" i]'))
      || emptyPattern.test(textOf(root))
      || Boolean(root.querySelector(loadingSelector));
    return hasSchema && hasBodyState;
  });
  const canonical = roots.filter((candidate) => !roots.some((other) => other !== candidate && candidate.contains(other)));
  if (canonical.length !== 1) return false;
  const root = canonical[0];
  const observedRoot = root.parentElement || root;
  const state = {mutation_count: 0, body_changed: false, loading_seen: false, root_replaced: false};
  const elementOf = (node) => node && (node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement);
  const containsRoot = (node) => {
    const element = elementOf(node);
    return Boolean(element && (element === root || element.contains(root)));
  };
  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      const target = elementOf(mutation.target);
      const removed = Array.from(mutation.removedNodes || []);
      if (!root.isConnected || removed.some(containsRoot)) {
        state.root_replaced = true;
        state.mutation_count += 1;
        continue;
      }
      if (!target || !(target === root || root.contains(target))) continue;
      if (mutation.type === 'childList') {
        const targetInBody = Boolean(target.matches(bodySelector) || target.closest(bodySelector));
        if (targetInBody) {
          state.body_changed = true;
          state.mutation_count += 1;
        }
        const changed = [...mutation.addedNodes, ...mutation.removedNodes].map(elementOf).filter(Boolean);
        if (changed.some((node) => node.matches(loadingSelector) || node.querySelector(loadingSelector))) {
          state.loading_seen = true;
          state.mutation_count += 1;
        }
      } else if (mutation.type === 'characterData') {
        if (target.closest(bodySelector) && (target.closest(rowSelector) || emptyPattern.test(textOf(target)))) {
          state.body_changed = true;
          state.mutation_count += 1;
        }
      } else if (mutation.type === 'attributes') {
        const oldValue = String(mutation.oldValue || '');
        const newValue = String(target.getAttribute(mutation.attributeName) || '');
        if (mutation.attributeName === 'aria-busy' || /loading|spinner|skeleton|spin/i.test(oldValue + ' ' + newValue)) {
          state.loading_seen = true;
          state.mutation_count += 1;
        }
      }
    }
  });
  observer.observe(observedRoot, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeOldValue: true,
    attributeFilter: ['class', 'aria-busy']
  });
  window.__lazadaBalanceRefreshProbe = {observer, root, state};
  return true;
} catch (error) {
  return false;
}
"""
    try:
        return bool(driver.execute_script(js_code))
    except Exception:
        return False


def _balance_transactions_refresh_probe_evidence(driver):
    js_code = r"""
/* lazada_balance_refresh_probe_read */
const probe = window.__lazadaBalanceRefreshProbe;
if (!probe || !probe.state) return {installed: false, mutation_count: 0};
return {
  installed: true,
  mutation_count: Number(probe.state.mutation_count || 0),
  body_changed: Boolean(probe.state.body_changed),
  loading_seen: Boolean(probe.state.loading_seen),
  root_replaced: Boolean(probe.state.root_replaced || !probe.root || !probe.root.isConnected)
};
"""
    try:
        value = driver.execute_script(js_code)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _clear_balance_transactions_refresh_probe(driver):
    js_code = r"""
/* lazada_balance_refresh_probe_clear */
const probe = window.__lazadaBalanceRefreshProbe;
if (probe && probe.observer) probe.observer.disconnect();
delete window.__lazadaBalanceRefreshProbe;
return true;
"""
    try:
        driver.execute_script(js_code)
    except Exception:
        pass


def _wait_for_balance_transactions_refresh(driver, previous_signature, timeout=12):
    """Require changed result content after submitting a different date range.

    A table can re-render its old rows for unrelated reasons.  An unchanged
    signature is accepted only by the separate, complete-result equivalence
    proof in ``_prove_unchanged_result_equivalence``.
    """
    previous = str(previous_signature or "")
    deadline = time.monotonic() + max(float(timeout), 0.0)
    while time.monotonic() < deadline:
        if _is_balance_transactions_loading(driver):
            time.sleep(0.25)
            continue
        current = _balance_transactions_page_signature(driver)
        if previous and current and current != previous:
            return _wait_for_balance_transactions_stable(
                driver,
                timeout=max(deadline - time.monotonic(), 0.5),
            )
        time.sleep(0.25)
    probe = _balance_transactions_refresh_probe_evidence(driver)
    if probe.get("installed") and probe.get("mutation_count"):
        log.info("Balance Transactions DOM 有变动但结果签名未变，仍需完整结果等价证明")
    return False


def _find_date_inputs(driver):
    """查找页面中的日期输入框"""
    selectors = [
        "input[type='date']",
        "input[type='text'][placeholder*='date' i]",
        "input[type='text'][placeholder*='Date' i]",
        "input[type='text'][placeholder*='开始' i]",
        "input[type='text'][placeholder*='结束' i]",
        "input[type='text'][placeholder*='start' i]",
        "input[type='text'][placeholder*='end' i]",
        "input[class*='date' i]",
        "input[class*='DatePicker' i]",
        "input[placeholder*='YYYY' i]",
        ".ant-picker input",
        ".date-picker input",
    ]
    for selector in selectors:
        inputs = driver.find_elements(By.CSS_SELECTOR, selector)
        visible_inputs = [inp for inp in inputs if _is_visible(inp)]
        if len(visible_inputs) >= 2:
            return visible_inputs[:2]
    return []


def _input_date_range(driver, start_input, end_input, start_date, end_date):
    """直接在日期输入框中输入日期"""
    try:
        # 清空并输入开始日期
        safe_click(driver, start_input)
        time.sleep(0.3)
        start_input.clear()
        time.sleep(0.2)
        _type_text(start_input, start_date)
        time.sleep(0.5)

        # 清空并输入结束日期
        safe_click(driver, end_input)
        time.sleep(0.3)
        end_input.clear()
        time.sleep(0.2)
        _type_text(end_input, end_date)
        time.sleep(0.5)

        # 点击查询/应用按钮
        _click_apply_button(driver)
        time.sleep(2)
        _wait_page_ready(driver)

        if _verify_balance_date_range(driver, start_date, end_date):
            log.info("日期范围已通过输入框设置并校验通过")
            return True
        log.warning(f"输入框设置日期后页面显示未匹配，当前显示: {_get_balance_date_range_text(driver) or '未读取到'}")
        return False
    except Exception as e:
        log.debug(f"输入框设置日期失败: {e}")
        return False


def _handle_date_picker_component(driver, start_date, end_date):
    """处理日期选择器组件"""
    # 查找日期选择器触发器
    trigger_selectors = [
        ".ant-picker",
        ".date-picker",
        "[class*='date-range']",
        "[class*='DateRange']",
        "[class*='datepicker']",
    ]
    for selector in trigger_selectors:
        triggers = driver.find_elements(By.CSS_SELECTOR, selector)
        visible_triggers = [t for t in triggers if _is_visible(t)]
        if visible_triggers:
            try:
                safe_click(driver, visible_triggers[0])
                time.sleep(1)

                # 在弹出的日历中查找输入框
                date_inputs = driver.find_elements(By.CSS_SELECTOR, ".ant-picker-input input, .date-picker input")
                visible_inputs = [inp for inp in date_inputs if _is_visible(inp)]
                if len(visible_inputs) >= 2:
                    return _input_date_range(driver, visible_inputs[0], visible_inputs[1], start_date, end_date)
            except Exception as e:
                log.debug(f"日期选择器组件处理失败: {e}")
    return False


def _js_set_date_range(driver, start_date, end_date):
    """通过 JavaScript 设置日期值"""
    js_code = """
    function setDateInputs(startDate, endDate) {
        // 查找所有日期相关输入框
        var inputs = document.querySelectorAll(
            'input[type="date"], input[class*="date" i], input[placeholder*="date" i], ' +
            'input[placeholder*="Date" i], input[placeholder*="YYYY" i], ' +
            'input[placeholder*="开始" i], input[placeholder*="结束" i]'
        );
        var dateInputs = [];
        for (var i = 0; i < inputs.length; i++) {
            if (inputs[i].offsetParent !== null) {
                dateInputs.push(inputs[i]);
            }
        }
        if (dateInputs.length < 2) return false;

        function triggerEvents(el, value) {
            var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            nativeInputValueSetter.call(el, value);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new Event('blur', {bubbles: true}));
        }

        triggerEvents(dateInputs[0], startDate);
        triggerEvents(dateInputs[1], endDate);
        return true;
    }
    return setDateInputs(arguments[0], arguments[1]);
    """
    try:
        result = driver.execute_script(js_code, start_date, end_date)
        if result:
            time.sleep(1)
            _click_apply_button(driver)
            time.sleep(2)
            if _verify_balance_date_range(driver, start_date, end_date):
                log.info("日期范围已通过 JavaScript 设置并校验通过")
                return True
            log.warning(f"JavaScript 设置日期后页面显示未匹配，当前显示: {_get_balance_date_range_text(driver) or '未读取到'}")
            return False
    except Exception as e:
        log.debug(f"JS 设置日期失败: {e}")
    return False


def _select_date_preset(driver, start_date, end_date):
    """尝试选择日期范围预设按钮"""
    preset_keywords = [
        "Last 30 days", "Last 7 days", "Last 90 days",
        "This month", "Last month",
        "过去30天", "过去7天", "过去90天",
        "本月", "上月",
    ]
    for keyword in preset_keywords:
        xpath = (
            f"//button[contains(translate(normalize-space(.), "
            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{keyword.lower()}')]"
            f"|//span[contains(translate(normalize-space(.), "
            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{keyword.lower()}')]"
            f"/parent::button"
            f"|//a[contains(translate(normalize-space(.), "
            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{keyword.lower()}')]"
        )
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if _is_visible(element):
                    safe_click(driver, element)
                    time.sleep(2)
                    _wait_page_ready(driver)
                    log.info(f"选择了日期预设: {keyword}")
                    return True
            except StaleElementReferenceException:
                continue
    return False


def _click_apply_button(driver):
    """点击 Apply/Search/查询 按钮"""
    apply_keywords = ["Apply", "apply", "Search", "search", "查询", "确定", "确认"]
    for keyword in apply_keywords:
        xpath = (
            f"//button[contains(translate(normalize-space(.), "
            f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{keyword.lower()}')]"
        )
        for btn in driver.find_elements(By.XPATH, xpath):
            try:
                if _is_visible(btn) and _is_enabled(btn):
                    safe_click(driver, btn)
                    return True
            except StaleElementReferenceException:
                continue
    return False


# ========== 金额采集 ==========


def extract_withdrawal_transaction_amount(driver, country):
    """
    Backward-compatible amount-only wrapper around the complete collector.
    Returns (amount, currency). Amount is a positive total.
    """
    details = collect_withdrawal_transaction_details(driver, country)
    return str(details.get("amount", "") or ""), str(details.get("currency", "") or "")


def extract_withdrawal_transaction_details(driver, country):
    """
    Backward-compatible tuple wrapper around the complete collector.
    Returns (amount, currency, remark). Amount is a positive total.
    """
    # Preserve the legacy contract that an immediately visible wrong-country row
    # raises, even when the full traversal later fails closed for another reason.
    visible_records = _extract_withdrawal_transaction_records_from_dom(driver, country)
    if visible_records:
        _ensure_transaction_country_matches(visible_records, country)
    details = collect_withdrawal_transaction_details(driver, country)
    return (
        str(details.get("amount", "") or ""),
        str(details.get("currency", "") or ""),
        str(details.get("remark", "") or ""),
    )


def _serialize_withdrawal_transaction_records(records, currency):
    """Convert internal Decimal-bearing rows into stable JSON/CSV-safe records."""
    serialized = []
    for record in records or []:
        amount_abs = record.get("amount_abs", "")
        if amount_abs not in (None, ""):
            try:
                amount_abs = _format_decimal_money(Decimal(str(amount_abs)))
            except (InvalidOperation, ValueError):
                amount_abs = str(amount_abs)
        serialized.append({
            "transaction_number": str(record.get("transaction_number", "") or ""),
            "time": str(record.get("time", "") or ""),
            "raw_amount": str(record.get("raw_amount", "") or ""),
            "amount_abs": str(amount_abs or ""),
            "currency": str(record.get("currency", "") or currency or ""),
            "remark": str(record.get("remark", "") or ""),
            "raw_remark": str(record.get("raw_remark", "") or ""),
        })
    return serialized


def validate_transaction_record_consistency(record):
    """Prove summary counts and exported transaction rows describe one result."""
    if not isinstance(record, dict):
        return False, "采集结果不是字典"
    if record.get("detail_consistent") is False:
        return False, str(
            record.get("detail_consistency_message", "")
            or "汇总与逐笔明细一致性校验失败"
        )
    outcome = str(record.get("collection_outcome", "") or "").strip().lower()
    records_value = record.get("transaction_records")
    if records_value is None:
        records_value = record.get("records")
    if not isinstance(records_value, list):
        return False, "逐笔明细不是列表"
    try:
        record_count = int(record.get("record_count", 0) or 0)
    except (TypeError, ValueError):
        return False, "逐笔明细记录数量无效"

    scope = None
    if outcome in ("complete", "confirmed_empty"):
        start_text = str(record.get("start_date", "") or "").strip()
        end_text = str(record.get("end_date", "") or "").strip()
        if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in (start_text, end_text)):
            return False, "完整采集结果缺少有效查询日期范围"
        try:
            start = datetime.strptime(start_text, "%Y-%m-%d").date()
            end = datetime.strptime(end_text, "%Y-%m-%d").date()
        except ValueError:
            return False, "查询日期范围无效"
        if start > end or str(record.get("trade_time", "") or "").strip() != f"{start_text}~{end_text}":
            return False, "汇总交易时间与查询日期范围不一致"
        scope = (start, end)

    if outcome in ("complete", "partial"):
        if record_count != len(records_value):
            return False, f"汇总记录数 {record_count} 与逐笔明细数 {len(records_value)} 不一致"
        required = {
            "transaction_number", "time", "raw_amount", "amount_abs",
            "currency", "remark", "raw_remark",
        }
        if any(not isinstance(item, dict) or not required.issubset(item) for item in records_value):
            return False, "逐笔明细字段不完整"
        if outcome == "complete" and record_count <= 0:
            return False, "完整采集结果没有逐笔明细"
        if outcome == "complete":
            currency = str(record.get("currency", "") or "").strip().upper()
            if not currency:
                return False, "完整采集结果缺少汇总币种"
            total = Decimal("0")
            seen_transaction_numbers = set()
            for item in records_value:
                transaction_number = re.sub(
                    r"\s+", "", str(item.get("transaction_number", "") or "")
                ).casefold()
                if not transaction_number or transaction_number in seen_transaction_numbers:
                    return False, "逐笔明细交易号缺失或重复"
                seen_transaction_numbers.add(transaction_number)
                transaction_date = _parse_withdrawal_transaction_date(item.get("time", ""))
                if transaction_date is None or not (scope[0] <= transaction_date <= scope[1]):
                    return False, "逐笔明细交易日期不在查询范围内"
                if str(item.get("currency", "") or "").strip().upper() != currency:
                    return False, "逐笔明细币种与汇总币种不一致"
                try:
                    amount = Decimal(str(item.get("amount_abs", "") or ""))
                except (InvalidOperation, ValueError):
                    return False, "逐笔明细金额无效"
                if not amount.is_finite() or amount <= 0:
                    return False, "逐笔明细金额不是有效正数"
                total += amount
            try:
                summary_amount = Decimal(str(record.get("amount", "") or ""))
            except (InvalidOperation, ValueError):
                return False, "汇总金额无效"
            if not summary_amount.is_finite() or summary_amount != total:
                return False, "逐笔明细金额合计与汇总金额不一致"
    elif outcome == "confirmed_empty":
        if record_count != 0 or records_value:
            return False, "已确认无提现但逐笔明细或记录数不为 0"
    return True, ""


def collect_withdrawal_transaction_details(driver, country):
    """Collect every Withdrawal row and return an explicit completeness outcome."""
    log.info("Collecting all Balance Transactions / Withdrawal rows")
    scroll_to_balance_transactions(driver)
    time.sleep(0.8)
    currency = _currency_by_country(country)

    if not _wait_for_balance_transactions_stable(driver):
        message = "Balance Transactions 表格未稳定加载，已停止写入"
        log.warning(message)
        return {
            "outcome": "parse_failed",
            "amount": "",
            "currency": currency,
            "remark": "",
            "record_count": 0,
            "candidate_count": 0,
            "collection_complete": False,
            "empty_confirmed": False,
            "transaction_records": [],
            "records": [],
            "message": message,
        }

    records, metadata = _collect_all_withdrawal_transaction_records(driver, country=country)
    record_count = len(records)
    candidate_count = int(metadata.get("candidate_count", 0) or 0)
    collection_complete = bool(metadata.get("complete"))
    unparsed_candidate_count = int(metadata.get("unparsed_candidate_count", 0) or 0)
    transaction_records = _serialize_withdrawal_transaction_records(records, currency)

    if records and collection_complete and unparsed_candidate_count == 0:
        _ensure_transaction_country_matches(records, country)
        total = sum((record["amount_abs"] for record in records), Decimal("0.00"))
        if total <= Decimal("0.00"):
            return {
                "outcome": "parse_failed",
                "amount": "",
                "currency": currency,
                "remark": "",
                "record_count": record_count,
                "candidate_count": candidate_count,
                "collection_complete": False,
                "empty_confirmed": False,
                "transaction_records": transaction_records,
                "records": transaction_records,
                "message": "Withdrawal 记录金额合计不大于 0，已停止写入",
            }
        amount = _format_decimal_money(total)
        remark = _combine_transaction_remarks(records)
        log.info(
            "Parsed all %s Withdrawal transaction(s) across %s page(s), total=%s %s",
            record_count,
            metadata.get("pages", 1),
            amount,
            currency,
        )
        for record in records[:20]:
            log.info(
                f"Withdrawal row: time={record.get('time', '')}, "
                f"amount={record['raw_amount']}, remark={record.get('remark', '')}"
            )
        if len(records) > 20:
            log.info(f"Only logged first 20 Withdrawal rows; {len(records) - 20} additional rows included in total")
        return {
            "outcome": "complete",
            "amount": amount,
            "currency": currency,
            "remark": remark,
            "record_count": record_count,
            "candidate_count": candidate_count,
            "collection_complete": True,
            "empty_confirmed": False,
            "transaction_records": transaction_records,
            "records": transaction_records,
            "message": "完整采集成功",
        }

    if records:
        observed_total = sum((record["amount_abs"] for record in records), Decimal("0.00"))
        message = (
            f"仅采集到 {record_count}/{candidate_count or record_count} 条 Withdrawal 记录，"
            "列表未完整遍历或存在无法解析的行，已停止写入"
        )
        log.warning(message)
        return {
            "outcome": "partial",
            "amount": "",
            "observed_amount": _format_decimal_money(observed_total),
            "currency": currency,
            "remark": "",
            "record_count": record_count,
            "candidate_count": candidate_count,
            "collection_complete": False,
            "empty_confirmed": False,
            "transaction_records": transaction_records,
            "records": transaction_records,
            "message": message,
        }

    if collection_complete and candidate_count == 0 and _confirm_balance_transactions_empty_state(driver):
        log.info("Balance Transactions table explicitly confirms no Withdrawal rows")
        return {
            "outcome": "confirmed_empty",
            "amount": "0.00",
            "currency": currency,
            "remark": CONFIRMED_NO_WITHDRAWAL_REMARK,
            "record_count": 0,
            "candidate_count": 0,
            "collection_complete": True,
            "empty_confirmed": True,
            "transaction_records": [],
            "records": [],
            "message": "已确认所选日期范围内无 Withdrawal 记录",
        }

    if candidate_count or unparsed_candidate_count:
        message = (
            f"发现 {candidate_count} 条 Withdrawal 候选记录，但有 "
            f"{unparsed_candidate_count or candidate_count} 条无法解析，已停止写入"
        )
    elif not collection_complete:
        message = "Balance Transactions 列表未能完整遍历，已停止写入"
    else:
        message = "未在 Balance Transactions 表内确认空态，也未解析到 Withdrawal 记录"
    log.warning(message)
    return {
        "outcome": "parse_failed",
        "amount": "",
        "currency": currency,
        "remark": "",
        "record_count": 0,
        "candidate_count": candidate_count,
        "collection_complete": False,
        "empty_confirmed": False,
        "transaction_records": [],
        "records": [],
        "message": message,
    }


def _collect_all_withdrawal_transaction_records(
    driver,
    max_pages=BALANCE_COLLECTION_MAX_PAGES,
    max_scroll_steps=BALANCE_COLLECTION_MAX_SCROLL_STEPS,
    country="",
):
    """Traverse virtual scrolling and pagination, returning de-duplicated rows plus metadata."""
    records_by_key = {}
    candidate_keys = set()
    unparsed_candidate_keys = set()
    conflict_keys = set()
    weak_identity_keys = set()
    seen_page_signatures = set()
    pages = 0
    complete = True
    root_found = True
    used_body_fallback = False
    deadline = time.monotonic() + BALANCE_COLLECTION_DEADLINE_SECONDS

    page_reset_ok, initial_page_snapshot = _reset_and_wait_balance_transactions_scroll(
        driver,
        country=country,
        deadline=deadline,
    )
    reset_failed = not page_reset_ok
    while pages < max_pages:
        if time.monotonic() >= deadline:
            complete = False
            log.warning("Balance Transactions collection exceeded the global deadline")
            break
        pages += 1
        seen_scroll_positions = set()
        current_page_candidate_keys = set()
        last_scroll_status = ""
        pending_snapshot = initial_page_snapshot
        initial_page_snapshot = None
        for _ in range(max_scroll_steps):
            if time.monotonic() >= deadline:
                complete = False
                log.warning("Balance Transactions collection exceeded the global deadline")
                break
            snapshot = pending_snapshot or _extract_withdrawal_transaction_dom_snapshot(driver, country)
            pending_snapshot = None
            if snapshot.get("root_found") is False:
                root_found = False
                complete = False
                log.warning("Balance Transactions table root was not found")
                break
            if snapshot.get("candidate_keys") and not page_reset_ok:
                complete = False
                log.warning("Balance Transactions rows exist but the table scroll origin was not established")
                break
            for key in snapshot.get("candidate_keys", []):
                candidate_keys.add(key)
                current_page_candidate_keys.add(key)
            for key in snapshot.get("unparsed_candidate_keys", []):
                unparsed_candidate_keys.add(key)
            for record in snapshot.get("records", []):
                identity = _withdrawal_record_identity(record)
                if identity[0] not in ("transaction", "bank_reference"):
                    weak_identity_keys.add(identity)
                existing = records_by_key.get(identity)
                if existing is not None and _withdrawal_record_payload(existing) != _withdrawal_record_payload(record):
                    conflict_keys.add(identity)
                else:
                    records_by_key.setdefault(identity, record)

            scroll_result = _advance_balance_transactions_virtual_scroll(driver)
            scroll_status = str(
                scroll_result.get("status", "") if isinstance(scroll_result, dict) else scroll_result or ""
            ).lower()
            last_scroll_status = scroll_status
            if scroll_status == "moved":
                position = str(scroll_result.get("position", "") if isinstance(scroll_result, dict) else "")
                if position and position in seen_scroll_positions:
                    complete = False
                    log.warning("Balance Transactions virtual scroll repeated position=%s", position)
                    break
                if position:
                    seen_scroll_positions.add(position)
                if isinstance(scroll_result, dict) and scroll_result.get("requires_row_change"):
                    changed, pending_snapshot = _wait_for_balance_transaction_rows_after_scroll(
                        driver,
                        snapshot.get("candidate_keys", []),
                        country=country,
                        deadline=deadline,
                    )
                    if not changed:
                        complete = False
                        log.warning("Balance Transactions virtual rows did not change after scrolling")
                        break
                else:
                    time.sleep(0.15)
                continue
            if scroll_status in ("end", "not-scrollable"):
                break

            complete = False
            log.warning("Balance Transactions virtual scroll returned unsafe status=%s", scroll_status)
            break
        else:
            complete = False
            log.warning("Balance Transactions virtual scroll exceeded %s steps", max_scroll_steps)

        if not complete:
            break

        previous_signature = _balance_transactions_page_signature(driver)
        if previous_signature:
            if previous_signature in seen_page_signatures:
                complete = False
                log.warning("Balance Transactions pagination repeated a page signature")
                break
            seen_page_signatures.add(previous_signature)
        next_result = _click_next_balance_transactions_page(driver)
        next_status = str(
            next_result.get("status", "") if isinstance(next_result, dict) else next_result or ""
        ).lower()
        if next_status == "clicked":
            if not _wait_for_balance_transactions_page_change(
                driver,
                previous_signature,
                previous_candidate_keys=current_page_candidate_keys,
            ):
                complete = False
                log.warning("Balance Transactions next page did not finish loading")
                break
            page_reset_ok, initial_page_snapshot = _reset_and_wait_balance_transactions_scroll(
                driver,
                country=country,
                deadline=deadline,
            )
            reset_failed = reset_failed or not page_reset_ok
            if not page_reset_ok:
                complete = False
                log.warning("Balance Transactions next page scroll origin was not established")
                break
            continue
        if next_status == "not_found" and not (
            pages == 1
            and last_scroll_status in ("end", "not-scrollable")
            and _balance_transactions_single_page_evidence(driver)
        ):
            complete = False
            log.warning("Balance Transactions did not provide proof that page %s is the final page", pages)
            break
        if next_status in ("", "end", "not_found", "disabled", "false"):
            break

        complete = False
        log.warning("Balance Transactions pagination returned unsafe status=%s", next_status)
        break
    else:
        complete = False
        log.warning("Balance Transactions pagination exceeded %s pages", max_pages)

    # Text fallback is only used when the table DOM yielded no transaction rows at all.
    # It is diagnostic only: unscoped page text can never prove a complete result.
    if not candidate_keys:
        try:
            body_text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
        except Exception:
            body_text = ""
        body_records = _parse_withdrawal_transaction_records(body_text, expected_country=country)
        body_candidate_count = _count_withdrawal_transaction_candidates(body_text)
        used_body_fallback = bool(body_records or body_candidate_count)
        for index in range(body_candidate_count):
            candidate_keys.add(f"body-candidate:{index}")
        for record in body_records:
            identity = _withdrawal_record_identity(record)
            if identity[0] not in ("transaction", "bank_reference"):
                weak_identity_keys.add(identity)
            existing = records_by_key.get(identity)
            if existing is not None and _withdrawal_record_payload(existing) != _withdrawal_record_payload(record):
                conflict_keys.add(identity)
            else:
                records_by_key.setdefault(identity, record)
        missing = max(body_candidate_count - len(body_records), 0)
        for index in range(missing):
            unparsed_candidate_keys.add(f"body-unparsed:{index}")
        if used_body_fallback:
            complete = False

    if records_by_key and reset_failed:
        complete = False

    if conflict_keys:
        complete = False
        log.warning("Detected %s conflicting duplicate Withdrawal key(s)", len(conflict_keys))
    if weak_identity_keys:
        complete = False
        log.warning(
            "Detected %s Withdrawal row identity/identities without transaction number or bank reference",
            len(weak_identity_keys),
        )

    return list(records_by_key.values()), {
        "complete": complete,
        "pages": pages,
        "candidate_count": len(candidate_keys),
        "unparsed_candidate_count": len(unparsed_candidate_keys),
        "conflict_count": len(conflict_keys),
        "weak_identity_count": len(weak_identity_keys),
        "root_found": root_found,
        "used_body_fallback": used_body_fallback,
    }


def _wait_for_balance_transaction_rows_after_scroll(
    driver,
    previous_candidate_keys,
    *,
    country="",
    deadline=None,
    timeout=3,
    previous_window_signature="",
):
    """Wait until a virtualized table renders a different, stable row window."""
    previous = tuple(str(key) for key in (previous_candidate_keys or []))
    previous_window = str(previous_window_signature or "").strip()
    stop_at = time.monotonic() + max(float(timeout), 0.0)
    if deadline is not None:
        stop_at = min(stop_at, float(deadline))
    last_keys = None
    stable_count = 0
    last_snapshot = None
    while time.monotonic() < stop_at:
        if _is_balance_transactions_loading(driver):
            last_keys = None
            stable_count = 0
            time.sleep(0.15)
            continue
        snapshot = _extract_withdrawal_transaction_dom_snapshot(driver, country)
        last_snapshot = snapshot
        if snapshot.get("root_found") is False:
            return False, snapshot
        keys = tuple(str(key) for key in snapshot.get("candidate_keys", []))
        current_window = str(snapshot.get("window_signature", "") or "").strip()
        changed = (
            bool(current_window) and current_window != previous_window
            if previous_window
            else bool(keys) and keys != previous
        )
        stable_marker = current_window if previous_window else keys
        if changed:
            if stable_marker == last_keys:
                stable_count += 1
            else:
                last_keys = stable_marker
                stable_count = 1
            if stable_count >= 2:
                return True, snapshot
        else:
            last_keys = None
            stable_count = 0
        time.sleep(0.15)
    return False, last_snapshot


def _withdrawal_record_identity(record):
    transaction_number = _extract_transaction_number_from_text(
        str(record.get("transaction_number", "") or "")
    )
    if transaction_number:
        return ("transaction", transaction_number)
    bank_reference = _extract_bank_reference(
        str(record.get("raw_remark", "") or "")
        or str(record.get("_source_key", "") or "")
    )
    if bank_reference:
        return ("bank_reference", bank_reference.lower())
    source_key = str(record.get("_source_key", "") or "").strip()
    if source_key:
        return ("source", re.sub(r"\s+", " ", source_key).lower())
    return (
        "record",
        re.sub(r"\s+", " ", str(record.get("time", "") or "")).strip().lower(),
        str(record.get("raw_amount", "") or "").replace(" ", "").lower(),
        re.sub(r"\s+", " ", str(record.get("remark", "") or "")).strip().lower(),
    )


def _withdrawal_record_payload(record):
    return (
        re.sub(r"\s+", " ", str(record.get("time", "") or "")).strip().lower(),
        str(record.get("amount_abs", "") or ""),
        re.sub(r"\s+", " ", str(record.get("raw_remark", "") or record.get("remark", "") or "")).strip().lower(),
    )


def _extract_bank_reference(text):
    match = re.search(
        r"(?i)bank\s+(?:transaction\s+)?(?:reference|ref)\b\.?\s*(?:no\.?\s*)?[:#-]?\s*([A-Z0-9][A-Z0-9-]{5,})",
        str(text or ""),
    )
    return match.group(1) if match else ""


def _extract_transaction_number_from_text(text):
    match = re.search(r"\b\d{12,}\b", str(text or ""))
    return match.group(0) if match else ""


def _count_withdrawal_transaction_candidates(text):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    count = 0
    for index, line in enumerate(lines):
        if not _is_withdrawal_line(line):
            continue
        before = lines[max(0, index - 6):index]
        after = lines[index + 1:index + 8]
        if any(_is_transaction_time_line(candidate) for candidate in before + after):
            count += 1
    return count


def _reset_balance_transactions_virtual_scroll(driver):
    js_code = r"""
/* lazada_balance_virtual_scroll_reset */
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
const heading = nodes.find((el) => /^balance\s+transactions$/i.test(textOf(el)) || /^\u4f59\u989d(?:\u4ea4\u6613|\u6d41\u6c34)$/.test(textOf(el)));
if (!heading) return false;
const headingY = heading.getBoundingClientRect().top + window.scrollY;
const rootSelectors = 'table,[role="table"],.next-table,.ant-table,[class*="table-wrapper" i],[class*="table-container" i],[class*="data-table" i]';
const headerPattern = /^(transaction\s*(number|no\.?|id|time)|type|sub\s*type|amount|remarks?|\u4ea4\u6613\u53f7|\u6d41\u6c34\u7f16\u53f7|\u4ea4\u6613\u65f6\u95f4|\u7c7b\u578b|\u5b50\u7c7b\u578b|\u91d1\u989d|\u5907\u6ce8)$/i;
const roots = Array.from(document.querySelectorAll(rootSelectors)).filter(visible).filter((root) => {
  const y = root.getBoundingClientRect().top + window.scrollY;
  if (y < headingY - 20 || y > headingY + 2500) return false;
  const headers = Array.from(root.querySelectorAll('th,[role="columnheader"]')).map(textOf).filter((text) => headerPattern.test(text));
  const hasSchema = headers.some((text) => /^(type|\u7c7b\u578b)$/i.test(text))
    && headers.some((text) => /^(amount|\u91d1\u989d)$/i.test(text))
    && headers.some((text) => /^(transaction\s*(number|no\.?|id|time)|\u4ea4\u6613\u53f7|\u6d41\u6c34\u7f16\u53f7|\u4ea4\u6613\u65f6\u95f4)$/i.test(text));
  const hasBodyState = Boolean(root.querySelector('tbody td,[role="rowgroup"] [role="cell"],.next-table-body .next-table-cell,.ant-table-tbody .ant-table-cell,[class*="table-body" i] [class*="table-cell" i]'))
    || /(^|\s)(no\s+(data|records?|results?)(\s+found)?|\u6682\u65e0\u6570\u636e|\u6ca1\u6709\u6570\u636e)(\s|$)/i.test(textOf(root))
    || Boolean(root.querySelector('[aria-busy="true"],.next-loading,.ant-spin-spinning,[class*="loading" i],[class*="spinner" i],[class*="skeleton" i]'));
  return hasSchema && hasBodyState;
});
roots.sort((a, b) => {
  const ay = a.getBoundingClientRect().top + window.scrollY;
  const by = b.getBoundingClientRect().top + window.scrollY;
  return (ay - by) || ((a.getBoundingClientRect().width * a.getBoundingClientRect().height) - (b.getBoundingClientRect().width * b.getBoundingClientRect().height));
});
const canonicalRoots = roots.filter((candidate) => !roots.some((other) => other !== candidate && candidate.contains(other)));
const root = canonicalRoots.length === 1 ? canonicalRoots[0] : null;
if (!root) return {status: 'not_found'};
const rowSelector = 'tbody tr,[role="rowgroup"] [role="row"],.next-table-row,.ant-table-row,[class*="table-row" i],[class*="TableRow"]';
const cellSelector = 'td,[role="cell"],.next-table-cell,.ant-table-cell,[class*="table-cell" i],[class*="TableCell"]';
const beforeWindowSignature = Array.from(root.querySelectorAll(rowSelector)).filter(visible)
  .filter((row) => Array.from(row.querySelectorAll(cellSelector)).some((cell) => /^(withdrawal|\u63d0\u73b0)$/i.test(textOf(cell))))
  .map(textOf).join('||');
const scrollNodes = [root, ...Array.from(root.querySelectorAll('*'))];
let scrollAncestor = root.parentElement;
while (scrollAncestor && scrollAncestor !== document.body && !scrollAncestor.contains(heading)) {
  scrollNodes.push(scrollAncestor);
  scrollAncestor = scrollAncestor.parentElement;
}
const scrollers = Array.from(new Set(scrollNodes)).filter((node) => {
  const style = window.getComputedStyle(node);
  return node.scrollHeight > node.clientHeight + 4 && /(auto|scroll)/i.test(style.overflowY || '');
});
if (scrollers.length) {
  const node = scrollers[0];
  const before = Number(node.scrollTop || 0);
  node.scrollTop = 0;
  node.dispatchEvent(new Event('scroll', {bubbles: true}));
  root.scrollIntoView({block: 'start', inline: 'nearest'});
  return {
    status: before > 2 ? 'moved' : 'ready',
    before_window_signature: beforeWindowSignature,
    position: String(Math.round(before))
  };
}
root.scrollIntoView({block: 'start', inline: 'nearest'});
return {status: 'ready', before_window_signature: beforeWindowSignature};
"""
    try:
        result = driver.execute_script(js_code)
        if isinstance(result, dict):
            return result
        return {"status": "ready" if result else "not_found"}
    except Exception as exc:
        log.debug("Balance Transactions virtual scroll reset failed: %s", exc)
        return {"status": "error"}


def _reset_and_wait_balance_transactions_scroll(driver, *, country="", deadline=None):
    """Reset a virtual table and prove that a moved row window has re-rendered."""
    result = _reset_balance_transactions_virtual_scroll(driver)
    if isinstance(result, dict):
        status = str(result.get("status", "") or "").strip().lower()
    else:
        status = "ready" if result else "not_found"
    if status == "ready":
        return True, None
    if status != "moved":
        return False, None

    previous_window_signature = str(
        result.get("before_window_signature", "") if isinstance(result, dict) else ""
    ).strip()
    if not previous_window_signature:
        return False, None
    changed, snapshot = _wait_for_balance_transaction_rows_after_scroll(
        driver,
        [],
        country=country,
        deadline=deadline,
        previous_window_signature=previous_window_signature,
    )
    return bool(changed), snapshot


def _advance_balance_transactions_virtual_scroll(driver):
    js_code = r"""
/* lazada_balance_virtual_scroll_advance */
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
const heading = nodes.find((el) => /^balance\s+transactions$/i.test(textOf(el)) || /^\u4f59\u989d(?:\u4ea4\u6613|\u6d41\u6c34)$/.test(textOf(el)));
if (!heading) return {status: 'not_found'};
const headingY = heading.getBoundingClientRect().top + window.scrollY;
const rootSelectors = 'table,[role="table"],.next-table,.ant-table,[class*="table-wrapper" i],[class*="table-container" i],[class*="data-table" i]';
const headerPattern = /^(transaction\s*(number|no\.?|id|time)|type|sub\s*type|amount|remarks?|\u4ea4\u6613\u53f7|\u4ea4\u6613\u65f6\u95f4|\u7c7b\u578b|\u5b50\u7c7b\u578b|\u91d1\u989d|\u5907\u6ce8)$/i;
const roots = Array.from(document.querySelectorAll(rootSelectors)).filter(visible).filter((root) => {
  const y = root.getBoundingClientRect().top + window.scrollY;
  if (y < headingY - 20 || y > headingY + 2500) return false;
  const headers = Array.from(root.querySelectorAll('th,[role="columnheader"]')).map(textOf).filter((text) => headerPattern.test(text));
  const hasSchema = headers.some((text) => /^(type|\u7c7b\u578b)$/i.test(text))
    && headers.some((text) => /^(amount|\u91d1\u989d)$/i.test(text))
    && headers.some((text) => /^(transaction\s*(number|no\.?|id|time)|\u4ea4\u6613\u53f7|\u4ea4\u6613\u65f6\u95f4)$/i.test(text));
  const hasBodyState = Boolean(root.querySelector('tbody td,[role="rowgroup"] [role="cell"],.next-table-body .next-table-cell,.ant-table-tbody .ant-table-cell,[class*="table-body" i] [class*="table-cell" i]'))
    || /(^|\s)(no\s+(data|records?|results?)(\s+found)?|\u6682\u65e0\u6570\u636e|\u6ca1\u6709\u6570\u636e)(\s|$)/i.test(textOf(root))
    || Boolean(root.querySelector('[aria-busy="true"],.next-loading,.ant-spin-spinning,[class*="loading" i],[class*="spinner" i],[class*="skeleton" i]'));
  return hasSchema && hasBodyState;
});
roots.sort((a, b) => {
  const ay = a.getBoundingClientRect().top + window.scrollY;
  const by = b.getBoundingClientRect().top + window.scrollY;
  return (ay - by) || ((a.getBoundingClientRect().width * a.getBoundingClientRect().height) - (b.getBoundingClientRect().width * b.getBoundingClientRect().height));
});
const canonicalRoots = roots.filter((candidate) => !roots.some((other) => other !== candidate && candidate.contains(other)));
const root = canonicalRoots.length === 1 ? canonicalRoots[0] : null;
if (!root) return {status: 'not_found'};
const scrollNodes = [root, ...Array.from(root.querySelectorAll('*'))];
let scrollAncestor = root.parentElement;
while (scrollAncestor && scrollAncestor !== document.body && !scrollAncestor.contains(heading)) {
  scrollNodes.push(scrollAncestor);
  scrollAncestor = scrollAncestor.parentElement;
}
const scrollers = Array.from(new Set(scrollNodes)).filter((node) => {
  const style = window.getComputedStyle(node);
  return node.scrollHeight > node.clientHeight + 4 && /(auto|scroll)/i.test(style.overflowY || '');
});
if (!scrollers.length) return {status: 'not-scrollable', all_rows_rendered: true};
const node = scrollers[0];
const rowSelector = 'tbody tr,[role="rowgroup"] [role="row"],.next-table-row,.ant-table-row,[class*="table-row" i],[class*="TableRow"]';
const rows = Array.from(root.querySelectorAll(rowSelector)).filter(visible).filter((row) => row.querySelector('td,[role="cell"],.next-table-cell,.ant-table-cell,[class*="table-cell" i],[class*="TableCell"]'));
const declaredNode = root.matches('[aria-rowcount]') ? root : root.querySelector('[aria-rowcount]');
const declaredCount = declaredNode ? Number(declaredNode.getAttribute('aria-rowcount') || 0) : 0;
const virtualHint = /virtual/i.test(String(root.className || '') + ' ' + String(node.className || ''))
  || (declaredCount > 0 && declaredCount > rows.length);
const nodeRect = node.getBoundingClientRect();
const scrollTop = Number(node.scrollTop || 0);
const lastContentBottom = rows.reduce((maximum, row) => Math.max(
  maximum,
  Number(row.getBoundingClientRect().bottom || 0) - Number(nodeRect.top || 0) + scrollTop
), 0);
const allRowsRendered = !virtualHint && rows.length > 0 && scrollTop <= 2
  && lastContentBottom >= Number(node.scrollHeight || 0) - 4;
if (allRowsRendered) return {status: 'end', position: String(Math.round(Number(node.scrollTop || 0))), all_rows_rendered: true};
const before = Number(node.scrollTop || 0);
const maximum = Math.max(Number(node.scrollHeight || 0) - Number(node.clientHeight || 0), 0);
if (before >= maximum - 2) return {status: 'end', position: String(Math.round(before)), all_rows_rendered: false};
node.scrollTop = Math.min(maximum, before + Math.max(Number(node.clientHeight || 0) * 0.8, 120));
node.dispatchEvent(new Event('scroll', {bubbles: true}));
const after = Number(node.scrollTop || 0);
if (after <= before + 1) return {status: 'error', position: String(Math.round(after))};
return {status: 'moved', position: String(Math.round(after)), requires_row_change: true};
"""
    try:
        result = driver.execute_script(js_code)
        return result if isinstance(result, dict) else {"status": "not_found"}
    except Exception as exc:
        log.debug("Balance Transactions virtual scroll failed: %s", exc)
        return {"status": "error"}


def _balance_transactions_page_signature(driver):
    js_code = r"""
/* lazada_balance_page_signature */
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
const heading = nodes.find((el) => /^balance\s+transactions$/i.test(textOf(el)) || /^\u4f59\u989d(?:\u4ea4\u6613|\u6d41\u6c34)$/.test(textOf(el)));
if (!heading) return '';
const headingY = heading.getBoundingClientRect().top + window.scrollY;
const rootSelectors = 'table,[role="table"],.next-table,.ant-table,[class*="table-wrapper" i],[class*="table-container" i],[class*="data-table" i]';
const headerPattern = /^(transaction\s*(number|no\.?|id|time)|type|sub\s*type|amount|remarks?|\u4ea4\u6613\u53f7|\u4ea4\u6613\u65f6\u95f4|\u7c7b\u578b|\u5b50\u7c7b\u578b|\u91d1\u989d|\u5907\u6ce8)$/i;
const roots = Array.from(document.querySelectorAll(rootSelectors)).filter(visible).filter((root) => {
  const y = root.getBoundingClientRect().top + window.scrollY;
  if (y < headingY - 20 || y > headingY + 2500) return false;
  const headers = Array.from(root.querySelectorAll('th,[role="columnheader"]')).map(textOf).filter((text) => headerPattern.test(text));
  const hasSchema = headers.some((text) => /^(type|\u7c7b\u578b)$/i.test(text))
    && headers.some((text) => /^(amount|\u91d1\u989d)$/i.test(text))
    && headers.some((text) => /^(transaction\s*(number|no\.?|id|time)|\u4ea4\u6613\u53f7|\u4ea4\u6613\u65f6\u95f4)$/i.test(text));
  const hasBodyState = Boolean(root.querySelector('tbody td,[role="rowgroup"] [role="cell"],.next-table-body .next-table-cell,.ant-table-tbody .ant-table-cell,[class*="table-body" i] [class*="table-cell" i]'))
    || /(^|\s)(no\s+(data|records?|results?)(\s+found)?|\u6682\u65e0\u6570\u636e|\u6ca1\u6709\u6570\u636e)(\s|$)/i.test(textOf(root))
    || Boolean(root.querySelector('[aria-busy="true"],.next-loading,.ant-spin-spinning,[class*="loading" i],[class*="spinner" i],[class*="skeleton" i]'));
  return hasSchema && hasBodyState;
});
roots.sort((a, b) => (a.getBoundingClientRect().top - b.getBoundingClientRect().top)
  || ((a.getBoundingClientRect().width * a.getBoundingClientRect().height) - (b.getBoundingClientRect().width * b.getBoundingClientRect().height)));
const canonicalRoots = roots.filter((candidate) => !roots.some((other) => other !== candidate && candidate.contains(other)));
const root = canonicalRoots.length === 1 ? canonicalRoots[0] : null;
if (!root) return '';
const rows = Array.from(root.querySelectorAll('tr,[role="row"],.next-table-row,.ant-table-row,[class*="table-row" i],[class*="TableRow"]'))
  .filter(visible).map(textOf).filter((text) => /(^|\s)(withdrawal|\u63d0\u73b0)(\s|$)/i.test(text));
const empty = !rows.length && /(^|\s)(no\s+(data|records?|results?)(\s+found)?|\u6682\u65e0\u6570\u636e|\u6ca1\u6709\u6570\u636e)(\s|$)/i.test(textOf(root));
let section = root;
while (section.parentElement && section.parentElement !== document.body && !section.contains(heading)) section = section.parentElement;
if (!section.contains(heading)) return '';
const pagerSelectors = '[class*="pagination" i],[class*="pager" i],nav[aria-label*="pagination" i],[role="navigation"]';
const allPagers = Array.from(section.querySelectorAll(pagerSelectors)).filter(visible);
const pagers = allPagers.filter((pager) => !allPagers.some((other) => other !== pager && other.contains(pager)));
if (pagers.length > 1) return '';
const activeRoot = pagers.length === 1 ? pagers[0] : section;
const active = Array.from(activeRoot.querySelectorAll('[aria-current="page"],.next-pagination-item.current,.next-pagination-item.next-current,.ant-pagination-item-active'))
  .filter(visible).map(textOf).join('|');
if (!active && !rows.length && !empty) return '';
return active + '::' + (empty ? 'EMPTY' : rows.join('||'));
"""
    try:
        return str(driver.execute_script(js_code) or "")
    except Exception:
        return ""


def _single_page_evidence_is_safe(evidence):
    """Decide whether structured pager evidence proves one complete result page."""
    if not isinstance(evidence, dict):
        return False
    if evidence.get("root_found") is not True or evidence.get("scroll_complete") is not True:
        return False
    has_rows = evidence.get("has_rows") is True
    has_empty = evidence.get("has_empty") is True
    if has_rows == has_empty:
        return False
    try:
        pager_count = int(evidence.get("pager_count", -1))
        active_count = int(evidence.get("active_count", 0) or 0)
        active_page = int(evidence.get("active_page", 0) or 0)
        previous_count = int(evidence.get("previous_count", 0) or 0)
        next_count = int(evidence.get("next_count", 0) or 0)
    except (TypeError, ValueError):
        return False

    # A canonical, explicitly empty table does not need pagination controls.
    if has_empty and pager_count == 0:
        return True
    # Non-empty results must prove that page 1 is also the final page.
    if pager_count != 1:
        return False
    return (
        active_count == 1
        and active_page == 1
        and previous_count == 1
        and evidence.get("previous_disabled") is True
        and next_count == 1
        and evidence.get("next_disabled") is True
    )


def _balance_transactions_single_page_evidence(driver):
    """Prove the scoped table is on its first and only fully traversable page."""
    js_code = r"""
/* lazada_balance_single_page_evidence */
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
const heading = nodes.find((el) => /^balance\s+transactions$/i.test(textOf(el)) || /^\u4f59\u989d(?:\u4ea4\u6613|\u6d41\u6c34)$/.test(textOf(el)));
if (!heading) return {root_found: false};
const headingY = heading.getBoundingClientRect().top + window.scrollY;
const tableSelectors = 'table,[role="table"],.next-table,.ant-table,[class*="table-wrapper" i],[class*="table-container" i],[class*="data-table" i]';
const headerPattern = /^(transaction\s*(number|no\.?|id|time)|type|sub\s*type|amount|remarks?|\u4ea4\u6613\u53f7|\u6d41\u6c34\u7f16\u53f7|\u4ea4\u6613\u65f6\u95f4|\u7c7b\u578b|\u5b50\u7c7b\u578b|\u91d1\u989d|\u5907\u6ce8)$/i;
const tables = Array.from(document.querySelectorAll(tableSelectors)).filter(visible).filter((element) => {
  const y = element.getBoundingClientRect().top + window.scrollY;
  if (y < headingY - 20 || y > headingY + 2500) return false;
  const headers = Array.from(element.querySelectorAll('th,[role="columnheader"]')).map(textOf).filter((text) => headerPattern.test(text));
  const hasSchema = headers.some((text) => /^(type|\u7c7b\u578b)$/i.test(text))
    && headers.some((text) => /^(amount|\u91d1\u989d)$/i.test(text))
    && headers.some((text) => /^(transaction\s*(number|no\.?|id|time)|\u4ea4\u6613\u53f7|\u6d41\u6c34\u7f16\u53f7|\u4ea4\u6613\u65f6\u95f4)$/i.test(text));
  const hasBodyState = Boolean(element.querySelector('tbody td,[role="rowgroup"] [role="cell"],.next-table-body .next-table-cell,.ant-table-tbody .ant-table-cell,[class*="table-body" i] [class*="table-cell" i]'))
    || /(^|\s)(no\s+(data|records?|results?)(\s+found)?|\u6682\u65e0\u6570\u636e|\u6ca1\u6709\u6570\u636e)(\s|$)/i.test(textOf(element))
    || Boolean(element.querySelector('[aria-busy="true"],.next-loading,.ant-spin-spinning,[class*="loading" i],[class*="spinner" i],[class*="skeleton" i]'));
  return hasSchema && hasBodyState;
});
tables.sort((left, right) => (left.getBoundingClientRect().top - right.getBoundingClientRect().top)
  || ((left.getBoundingClientRect().width * left.getBoundingClientRect().height) - (right.getBoundingClientRect().width * right.getBoundingClientRect().height)));
const canonicalTables = tables.filter((candidate) => !tables.some((other) => other !== candidate && candidate.contains(other)));
const table = canonicalTables.length === 1 ? canonicalTables[0] : null;
if (!table) return {root_found: false};
const tableText = textOf(table);
const hasRows = /(^|\s)(withdrawal|\u63d0\u73b0)(\s|$)/i.test(tableText);
const hasEmpty = /(^|\s)(no\s+(data|records?|results?)(\s+found)?|\u6682\u65e0\u6570\u636e|\u6ca1\u6709\u6570\u636e)(\s|$)/i.test(tableText);
if (!hasRows && !hasEmpty) return {root_found: true, has_rows: false, has_empty: false, scroll_complete: false, pager_count: 0};

const scrollNodes = [table, ...Array.from(table.querySelectorAll('*'))];
let scrollAncestor = table.parentElement;
while (scrollAncestor && scrollAncestor !== document.body && !scrollAncestor.contains(heading)) {
  scrollNodes.push(scrollAncestor);
  scrollAncestor = scrollAncestor.parentElement;
}
let scrollComplete = true;
for (const node of Array.from(new Set(scrollNodes))) {
  const style = window.getComputedStyle(node);
  if (node.scrollHeight > node.clientHeight + 4 && /(auto|scroll)/i.test(style.overflowY || '')) {
    const rowSelector = 'tbody tr,[role="rowgroup"] [role="row"],.next-table-row,.ant-table-row,[class*="table-row" i],[class*="TableRow"]';
    const renderedRows = Array.from(table.querySelectorAll(rowSelector)).filter(visible).filter((row) => row.querySelector('td,[role="cell"],.next-table-cell,.ant-table-cell,[class*="table-cell" i],[class*="TableCell"]'));
    const declaredNode = table.matches('[aria-rowcount]') ? table : table.querySelector('[aria-rowcount]');
    const declaredCount = declaredNode ? Number(declaredNode.getAttribute('aria-rowcount') || 0) : 0;
    const virtualHint = /virtual/i.test(String(table.className || '') + ' ' + String(node.className || ''))
      || (declaredCount > 0 && declaredCount > renderedRows.length);
    const nodeRect = node.getBoundingClientRect();
    const scrollTop = Number(node.scrollTop || 0);
    const lastContentBottom = renderedRows.reduce((maximum, row) => Math.max(
      maximum,
      Number(row.getBoundingClientRect().bottom || 0) - Number(nodeRect.top || 0) + scrollTop
    ), 0);
    const allRowsRendered = !virtualHint && renderedRows.length > 0 && scrollTop <= 2
      && lastContentBottom >= Number(node.scrollHeight || 0) - 4;
    if (allRowsRendered) continue;
    const maximum = Math.max(Number(node.scrollHeight || 0) - Number(node.clientHeight || 0), 0);
    if (Number(node.scrollTop || 0) < maximum - 2) scrollComplete = false;
  }
}

const pagerSelectors = '[class*="pagination" i],[class*="pager" i],nav[aria-label*="pagination" i],[role="navigation"]';
let section = table;
while (section.parentElement && section.parentElement !== document.body && !section.contains(heading)) section = section.parentElement;
if (!section.contains(heading)) return {root_found: false};
const allPagers = Array.from(section.querySelectorAll(pagerSelectors)).filter(visible);
const pagers = allPagers.filter((pager) => !allPagers.some((other) => other !== pager && other.contains(pager)));
const result = {
  root_found: true,
  has_rows: hasRows,
  has_empty: hasEmpty,
  scroll_complete: scrollComplete,
  pager_count: pagers.length,
  active_count: 0,
  active_page: 0,
  previous_count: 0,
  previous_disabled: false,
  next_count: 0,
  next_disabled: false
};
if (pagers.length === 1) {
  const pager = pagers[0];
  const activeCandidates = Array.from(pager.querySelectorAll(
    '[aria-current="page"],.next-pagination-item.current,.next-pagination-item.next-current,.ant-pagination-item-active'
  )).filter(visible);
  const activeMatch = activeCandidates.length === 1 ? textOf(activeCandidates[0]).match(/\d+/) : null;
  const previousSelectors = [
    '.next-pagination-item.prev', '.next-pagination-item.next-prev', '.ant-pagination-prev',
    '.eds-react-pagination-pager__button-prev',
    'button[aria-label*="prev" i]', 'a[aria-label*="prev" i]',
    'button[title*="prev" i]', 'a[title*="prev" i]',
    'button[aria-label*="\u4e0a\u4e00\u9875"]', 'a[aria-label*="\u4e0a\u4e00\u9875"]'
  ];
  const nextSelectors = [
    '.next-pagination-item.next', '.next-pagination-item.next-next', '.ant-pagination-next',
    '.eds-react-pagination-pager__button-next',
    'button[aria-label*="next" i]', 'a[aria-label*="next" i]',
    'button[title*="next" i]', 'a[title*="next" i]',
    'button[aria-label*="\u4e0b\u4e00\u9875"]', 'a[aria-label*="\u4e0b\u4e00\u9875"]'
  ];
  const previousCandidates = Array.from(new Set(
    Array.from(pager.querySelectorAll(previousSelectors.join(','))).filter(visible)
  ));
  const nextCandidates = Array.from(new Set(
    Array.from(pager.querySelectorAll(nextSelectors.join(','))).filter(visible)
  ));
  const disabled = (control) => control.disabled || control.getAttribute('aria-disabled') === 'true'
    || /(^|\s)(disabled|next-disabled|ant-pagination-disabled)(\s|$)/i.test(String(control.className || ''));
  result.active_count = activeCandidates.length;
  result.active_page = activeMatch ? Number(activeMatch[0]) : 0;
  result.previous_count = previousCandidates.length;
  result.previous_disabled = previousCandidates.length === 1 && disabled(previousCandidates[0]);
  result.next_count = nextCandidates.length;
  result.next_disabled = nextCandidates.length === 1 && disabled(nextCandidates[0]);
}
return result;
"""
    try:
        return _single_page_evidence_is_safe(driver.execute_script(js_code))
    except Exception as exc:
        log.debug("Balance Transactions single-page evidence failed: %s", exc)
        return False


def _click_next_balance_transactions_page(driver):
    js_code = r"""
/* lazada_balance_next_page */
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
const heading = nodes.find((el) => /^balance\s+transactions$/i.test(textOf(el)) || /^\u4f59\u989d(?:\u4ea4\u6613|\u6d41\u6c34)$/.test(textOf(el)));
if (!heading) return {status: 'not_found'};
const headingY = heading.getBoundingClientRect().top + window.scrollY;
const rootSelectors = 'table,[role="table"],.next-table,.ant-table,[class*="table-wrapper" i],[class*="table-container" i],[class*="data-table" i]';
const headerPattern = /^(transaction\s*(number|no\.?|id|time)|type|sub\s*type|amount|remarks?|\u4ea4\u6613\u53f7|\u4ea4\u6613\u65f6\u95f4|\u7c7b\u578b|\u5b50\u7c7b\u578b|\u91d1\u989d|\u5907\u6ce8)$/i;
const roots = Array.from(document.querySelectorAll(rootSelectors)).filter(visible).filter((root) => {
  const y = root.getBoundingClientRect().top + window.scrollY;
  if (y < headingY - 20 || y > headingY + 2500) return false;
  const headers = Array.from(root.querySelectorAll('th,[role="columnheader"]')).map(textOf).filter((text) => headerPattern.test(text));
  const hasSchema = headers.some((text) => /^(type|\u7c7b\u578b)$/i.test(text))
    && headers.some((text) => /^(amount|\u91d1\u989d)$/i.test(text))
    && headers.some((text) => /^(transaction\s*(number|no\.?|id|time)|\u4ea4\u6613\u53f7|\u4ea4\u6613\u65f6\u95f4)$/i.test(text));
  const hasBodyState = Boolean(root.querySelector('tbody td,[role="rowgroup"] [role="cell"],.next-table-body .next-table-cell,.ant-table-tbody .ant-table-cell,[class*="table-body" i] [class*="table-cell" i]'))
    || /(^|\s)(no\s+(data|records?|results?)(\s+found)?|\u6682\u65e0\u6570\u636e|\u6ca1\u6709\u6570\u636e)(\s|$)/i.test(textOf(root))
    || Boolean(root.querySelector('[aria-busy="true"],.next-loading,.ant-spin-spinning,[class*="loading" i],[class*="spinner" i],[class*="skeleton" i]'));
  return hasSchema && hasBodyState;
});
roots.sort((a, b) => (a.getBoundingClientRect().top - b.getBoundingClientRect().top)
  || ((a.getBoundingClientRect().width * a.getBoundingClientRect().height) - (b.getBoundingClientRect().width * b.getBoundingClientRect().height)));
const canonicalRoots = roots.filter((candidate) => !roots.some((other) => other !== candidate && candidate.contains(other)));
const root = canonicalRoots.length === 1 ? canonicalRoots[0] : null;
if (!root) return {status: 'not_found'};
let section = root;
while (section.parentElement && section.parentElement !== document.body && !section.contains(heading)) section = section.parentElement;
if (!section.contains(heading)) return {status: 'not_found'};
const selectors = [
  '.next-pagination-item.next', '.next-pagination-item.next-next', '.ant-pagination-next',
  '.eds-react-pagination-pager__button-next',
  'button[aria-label*="next" i]', 'a[aria-label*="next" i]',
  'button[title*="next" i]', 'a[title*="next" i]',
  'button[aria-label*="\u4e0b\u4e00\u9875"]', 'a[aria-label*="\u4e0b\u4e00\u9875"]'
];
const pagerSelectors = '[class*="pagination" i],[class*="pager" i],nav[aria-label*="pagination" i],[role="navigation"]';
const allPagers = Array.from(section.querySelectorAll(pagerSelectors)).filter(visible);
const pagers = allPagers.filter((pager) => !allPagers.some((other) => other !== pager && other.contains(pager)));
if (pagers.length > 1) return {status: 'ambiguous'};
if (!pagers.length) return {status: 'not_found'};
const candidates = Array.from(new Set(Array.from(pagers[0].querySelectorAll(selectors.join(','))).filter(visible)));
if (candidates.length > 1) return {status: 'ambiguous'};
if (!candidates.length) return {status: 'not_found'};
const next = candidates[0];
const disabled = next.disabled
  || next.getAttribute('aria-disabled') === 'true'
  || /(^|\s)(disabled|next-disabled|ant-pagination-disabled)(\s|$)/i.test(String(next.className || ''));
if (disabled) return {status: 'end'};
next.scrollIntoView({block: 'center', inline: 'nearest'});
next.click();
return {status: 'clicked'};
"""
    try:
        result = driver.execute_script(js_code)
        return result if isinstance(result, dict) else {"status": "not_found"}
    except Exception as exc:
        log.debug("Balance Transactions next page click failed: %s", exc)
        return {"status": "error"}


def _wait_for_balance_transactions_page_change(
    driver,
    previous_signature,
    previous_candidate_keys=None,
    timeout=BALANCE_PAGE_CHANGE_TIMEOUT_SECONDS,
):
    if not previous_signature:
        return False
    deadline = time.monotonic() + max(float(timeout), 0.0)
    while time.monotonic() < deadline:
        time.sleep(0.25)
        current_signature = _balance_transactions_page_signature(driver)
        if current_signature and current_signature != previous_signature:
            if not _wait_for_balance_transactions_stable(
                driver, timeout=max(deadline - time.monotonic(), 0.5)
            ):
                return False
            snapshot = _extract_withdrawal_transaction_dom_snapshot(driver)
            current_keys = {
                str(key) for key in snapshot.get("candidate_keys", []) if str(key)
            }
            previous_keys = {
                str(key) for key in (previous_candidate_keys or []) if str(key)
            }
            return (
                bool(current_keys)
                and bool(current_keys - previous_keys)
                and snapshot.get("root_found") is not False
            )
    return False


def _wait_for_balance_transactions_stable(driver, timeout=12, stable_samples=3, interval=0.4):
    """Wait for a non-loading table state to remain unchanged across several samples."""
    if not hasattr(driver, "execute_script"):
        return False
    deadline = time.monotonic() + max(float(timeout), 0.0)
    previous_state = None
    stable_count = 0
    while time.monotonic() < deadline:
        if _is_balance_transactions_loading(driver):
            previous_state = None
            stable_count = 0
            time.sleep(max(float(interval), 0.0))
            continue

        signature = _balance_transactions_page_signature(driver)
        snapshot = _extract_withdrawal_transaction_dom_snapshot(driver)
        candidate_keys = tuple(sorted(str(key) for key in snapshot.get("candidate_keys", [])))
        empty = _is_balance_transactions_empty_state(driver)
        observable = bool(candidate_keys or empty)
        current_state = (signature, candidate_keys, empty)
        if observable and current_state == previous_state:
            stable_count += 1
        elif observable:
            previous_state = current_state
            stable_count = 1
        else:
            previous_state = None
            stable_count = 0

        if stable_count >= max(int(stable_samples), 1):
            return True
        time.sleep(max(float(interval), 0.0))
    return False


def _is_balance_transactions_empty_state(driver):
    """Confirm an explicit empty marker scoped to the Balance Transactions section."""
    js_code = r"""
/* lazada_balance_scoped_empty_state */
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
const heading = nodes.find((el) => {
  const text = textOf(el);
  return /^balance\s+transactions$/i.test(text) || /^\u4f59\u989d(?:\u4ea4\u6613|\u6d41\u6c34)$/.test(text);
});
if (!heading) return false;
const headingY = heading.getBoundingClientRect().top + window.scrollY;
const tableSelectors = 'table,[role="table"],.next-table,.ant-table,[class*="table-wrapper" i],[class*="table-container" i],[class*="data-table" i]';
const headerPattern = /^(transaction\s*(number|no\.?|id|time)|type|sub\s*type|amount|remarks?|\u4ea4\u6613\u53f7|\u4ea4\u6613\u65f6\u95f4|\u7c7b\u578b|\u5b50\u7c7b\u578b|\u91d1\u989d|\u5907\u6ce8)$/i;
const tableScopes = Array.from(document.querySelectorAll(tableSelectors)).filter(visible).filter((element) => {
  const y = element.getBoundingClientRect().top + window.scrollY;
  if (y < headingY - 20 || y > headingY + 2500) return false;
  const headers = Array.from(element.querySelectorAll('th,[role="columnheader"]')).map(textOf).filter((text) => headerPattern.test(text));
  const hasSchema = headers.some((text) => /^(type|\u7c7b\u578b)$/i.test(text))
    && headers.some((text) => /^(amount|\u91d1\u989d)$/i.test(text))
    && headers.some((text) => /^(transaction\s*(number|no\.?|id|time)|\u4ea4\u6613\u53f7|\u4ea4\u6613\u65f6\u95f4)$/i.test(text));
  const hasBodyState = Boolean(element.querySelector('tbody td,[role="rowgroup"] [role="cell"],.next-table-body .next-table-cell,.ant-table-tbody .ant-table-cell,[class*="table-body" i] [class*="table-cell" i]'))
    || /(^|\s)(no\s+(data|records?|results?)(\s+found)?|\u6682\u65e0\u6570\u636e|\u6ca1\u6709\u6570\u636e)(\s|$)/i.test(textOf(element))
    || Boolean(element.querySelector('[aria-busy="true"],.next-loading,.ant-spin-spinning,[class*="loading" i],[class*="spinner" i],[class*="skeleton" i]'));
  return hasSchema && hasBodyState;
});
tableScopes.sort((left, right) => (left.getBoundingClientRect().top - right.getBoundingClientRect().top)
  || ((left.getBoundingClientRect().width * left.getBoundingClientRect().height) - (right.getBoundingClientRect().width * right.getBoundingClientRect().height)));
const canonicalScopes = tableScopes.filter((candidate) => !tableScopes.some((other) => other !== candidate && candidate.contains(other)));
const scope = canonicalScopes.length === 1 ? canonicalScopes[0] : null;
if (!scope) return false;
const emptyPatterns = [
  /^no\s+data$/i, /^no\s+records?$/i, /^no\s+records?\s+found$/i,
  /^no\s+results?$/i, /^no\s+results?\s+found$/i,
  /^\u6682\u65e0\u6570\u636e$/, /^\u6ca1\u6709\u6570\u636e$/
];
const isEmptyText = (text) => emptyPatterns.some((pattern) => pattern.test(text));
const dataRows = Array.from(scope.querySelectorAll('tbody tr,[role="rowgroup"] [role="row"],.next-table-row,.ant-table-row,[class*="table-row" i],[class*="TableRow"]'))
  .filter(visible)
  .filter((row) => {
    const text = textOf(row);
    const hasDataCell = Boolean(row.querySelector('td,[role="cell"],.next-table-cell,.ant-table-cell,[class*="table-cell" i],[class*="TableCell"]'));
    return hasDataCell && text && !isEmptyText(text);
  });
if (dataRows.length) return false;
return Array.from(scope.querySelectorAll('*')).filter(visible).some((element) => {
  const text = textOf(element);
  return Boolean(text && text.length <= 80 && isEmptyText(text));
});
"""
    try:
        return bool(driver.execute_script(js_code))
    except Exception as exc:
        log.debug("Balance Transactions scoped empty-state check failed: %s", exc)
        return False


def _is_balance_transactions_loading(driver):
    js_code = r"""
/* lazada_balance_loading_state */
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
const heading = nodes.find((el) => /^balance\s+transactions$/i.test(textOf(el)) || /^\u4f59\u989d(?:\u4ea4\u6613|\u6d41\u6c34)$/.test(textOf(el)));
if (!heading) return false;
const headingY = heading.getBoundingClientRect().top + window.scrollY;
const tableSelectors = 'table,[role="table"],.next-table,.ant-table,[class*="table-wrapper" i],[class*="table-container" i],[class*="data-table" i]';
const headerPattern = /^(transaction\s*(number|no\.?|id|time)|type|sub\s*type|amount|remarks?|\u4ea4\u6613\u53f7|\u4ea4\u6613\u65f6\u95f4|\u7c7b\u578b|\u5b50\u7c7b\u578b|\u91d1\u989d|\u5907\u6ce8)$/i;
const tableScopes = Array.from(document.querySelectorAll(tableSelectors)).filter(visible).filter((element) => {
  const y = element.getBoundingClientRect().top + window.scrollY;
  if (y < headingY - 20 || y > headingY + 2500) return false;
  const headers = Array.from(element.querySelectorAll('th,[role="columnheader"]')).map(textOf).filter((text) => headerPattern.test(text));
  const hasSchema = headers.some((text) => /^(type|\u7c7b\u578b)$/i.test(text))
    && headers.some((text) => /^(amount|\u91d1\u989d)$/i.test(text))
    && headers.some((text) => /^(transaction\s*(number|no\.?|id|time)|\u4ea4\u6613\u53f7|\u4ea4\u6613\u65f6\u95f4)$/i.test(text));
  const hasBodyState = Boolean(element.querySelector('tbody td,[role="rowgroup"] [role="cell"],.next-table-body .next-table-cell,.ant-table-tbody .ant-table-cell,[class*="table-body" i] [class*="table-cell" i]'))
    || /(^|\s)(no\s+(data|records?|results?)(\s+found)?|\u6682\u65e0\u6570\u636e|\u6ca1\u6709\u6570\u636e)(\s|$)/i.test(textOf(element))
    || Boolean(element.querySelector('[aria-busy="true"],.next-loading,.ant-spin-spinning,[class*="loading" i],[class*="spinner" i],[class*="skeleton" i]'));
  return hasSchema && hasBodyState;
});
tableScopes.sort((left, right) => (left.getBoundingClientRect().top - right.getBoundingClientRect().top)
  || ((left.getBoundingClientRect().width * left.getBoundingClientRect().height) - (right.getBoundingClientRect().width * right.getBoundingClientRect().height)));
const canonicalScopes = tableScopes.filter((candidate) => !tableScopes.some((other) => other !== candidate && candidate.contains(other)));
const scope = canonicalScopes.length === 1 ? canonicalScopes[0] : null;
if (!scope) return false;
const selectors = [
  '[aria-busy="true"]', '.next-loading', '.ant-spin-spinning',
  '[class*="loading" i]', '[class*="spinner" i]', '[class*="skeleton" i]'
];
return Array.from(scope.querySelectorAll(selectors.join(','))).some(visible);
"""
    try:
        return bool(driver.execute_script(js_code))
    except Exception:
        return False


def _confirm_balance_transactions_empty_state(driver, samples=4, interval=0.5):
    """Require a stable, non-loading, table-scoped empty state before returning zero."""
    if not _is_withdrawal_filter_selected(driver):
        return False
    for index in range(max(int(samples), 1)):
        if _is_balance_transactions_loading(driver):
            return False
        if not _is_balance_transactions_empty_state(driver):
            return False
        if index + 1 < max(int(samples), 1):
            time.sleep(max(float(interval), 0.0))
    return True


def _extract_withdrawal_transaction_records_from_dom(driver, country=""):
    return _extract_withdrawal_transaction_dom_snapshot(driver, country).get("records", [])


def _extract_withdrawal_transaction_dom_snapshot(driver, country=""):
    js_code = r"""
/* lazada_balance_dom_snapshot */
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = (el) => String(el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
const lineTextOf = (el) => String(el && (el.innerText || el.textContent) || '')
  .split(/\n+/)
  .map((item) => item.replace(/\s+/g, ' ').trim())
  .filter(Boolean);
const nodes = Array.from(document.querySelectorAll('*')).filter(visible);
const heading = nodes.find((el) => {
  const text = textOf(el);
  return /^balance\s+transactions$/i.test(text) || /^\u4f59\u989d(?:\u4ea4\u6613|\u6d41\u6c34)$/.test(text);
});
if (!heading) return {rootFound: false, rows: []};
const headingY = heading.getBoundingClientRect().top + window.scrollY;
const rowSelectors = [
  'tr',
  '[role="row"]',
  '.next-table-row',
  '.ant-table-row',
  '[class*="table-row" i]',
  '[class*="TableRow"]'
].join(',');
const cellSelectors = [
  'th',
  'td',
  '[role="columnheader"]',
  '[role="cell"]',
  '.next-table-cell',
  '.ant-table-cell',
  '[class*="table-cell" i]',
  '[class*="TableCell"]'
].join(',');
const isWithdrawal = (text) => {
  const value = String(text || '').trim().toLowerCase();
  return value === 'withdrawal' || value === '\u63d0\u73b0';
};
const isTime = (text) => (
  /\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\s+\d{2}:\d{2}:\d{2}/.test(text)
  || /\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}:\d{2}/.test(text)
);
const rootSelectors = 'table,[role="table"],.next-table,.ant-table,[class*="table-wrapper" i],[class*="table-container" i],[class*="data-table" i]';
const headerPattern = /^(transaction\s*(number|no\.?|id|time)|type|sub\s*type|amount|remarks?|\u4ea4\u6613\u53f7|\u6d41\u6c34\u7f16\u53f7|\u4ea4\u6613\u65f6\u95f4|\u7c7b\u578b|\u5b50\u7c7b\u578b|\u91d1\u989d|\u5907\u6ce8)$/i;
const roots = Array.from(document.querySelectorAll(rootSelectors)).filter(visible).filter((root) => {
  const y = root.getBoundingClientRect().top + window.scrollY;
  if (y < headingY - 20 || y > headingY + 2500) return false;
  const headers = Array.from(root.querySelectorAll('th,[role="columnheader"]')).map(textOf).filter((text) => headerPattern.test(text));
  const hasSchema = headers.some((text) => /^(type|\u7c7b\u578b)$/i.test(text))
    && headers.some((text) => /^(amount|\u91d1\u989d)$/i.test(text))
    && headers.some((text) => /^(transaction\s*(number|no\.?|id|time)|\u4ea4\u6613\u53f7|\u6d41\u6c34\u7f16\u53f7|\u4ea4\u6613\u65f6\u95f4)$/i.test(text));
  const hasBodyState = Boolean(root.querySelector('tbody td,[role="rowgroup"] [role="cell"],.next-table-body .next-table-cell,.ant-table-tbody .ant-table-cell,[class*="table-body" i] [class*="table-cell" i]'))
    || /(^|\s)(no\s+(data|records?|results?)(\s+found)?|\u6682\u65e0\u6570\u636e|\u6ca1\u6709\u6570\u636e)(\s|$)/i.test(textOf(root))
    || Boolean(root.querySelector('[aria-busy="true"],.next-loading,.ant-spin-spinning,[class*="loading" i],[class*="spinner" i],[class*="skeleton" i]'));
  return hasSchema && hasBodyState;
});
roots.sort((a, b) => {
  const ay = a.getBoundingClientRect().top + window.scrollY;
  const by = b.getBoundingClientRect().top + window.scrollY;
  return (ay - by) || ((a.getBoundingClientRect().width * a.getBoundingClientRect().height) - (b.getBoundingClientRect().width * b.getBoundingClientRect().height));
});
const canonicalRoots = roots.filter((candidate) => !roots.some((other) => other !== candidate && candidate.contains(other)));
const queryRoot = canonicalRoots.length === 1 ? canonicalRoots[0] : null;
if (!queryRoot) return {rootFound: false, rows: []};
const headerRows = Array.from(queryRoot.querySelectorAll('thead tr,[role="row"]')).filter((row) => row.querySelector('th,[role="columnheader"]'));
const headers = headerRows.length
  ? Array.from(headerRows[headerRows.length - 1].querySelectorAll('th,[role="columnheader"]')).map(textOf)
  : Array.from(queryRoot.querySelectorAll('th,[role="columnheader"]')).map(textOf);
const headerIndex = (patterns) => headers.findIndex((header) => patterns.some((pattern) => pattern.test(header)));
const transactionNumberIndex = headerIndex([/^transaction\s*(number|no\.?|id)$/i, /^\u4ea4\u6613\u53f7$/, /^\u6d41\u6c34\u7f16\u53f7$/]);
const timeIndexFromHeader = headerIndex([/^transaction\s*time$/i, /^\u4ea4\u6613\u65f6\u95f4$/]);
const typeIndex = headerIndex([/^type$/i, /^\u7c7b\u578b$/]);
const amountIndexFromHeader = headerIndex([/^amount$/i, /^\u91d1\u989d$/]);
const remarkIndex = headerIndex([/^remarks?$/i, /^\u5907\u6ce8$/]);
const logicalCells = (row) => {
  const candidates = Array.from(row.querySelectorAll(cellSelectors)).filter(visible);
  return candidates.filter((candidate) => !candidates.some(
    (other) => other !== candidate && other.contains(candidate)
  ));
};
const rows = Array.from(queryRoot.querySelectorAll(rowSelectors)).filter(visible).filter((row) => {
  const cells = logicalCells(row);
  if (typeIndex >= 0 && cells[typeIndex]) return isWithdrawal(textOf(cells[typeIndex]));
  return cells.some((cell) => isWithdrawal(textOf(cell)));
});
const windowSignature = rows.map(textOf).join('||');
const results = [];
for (const row of rows) {
  const cells = logicalCells(row)
    .map((cell) => lineTextOf(cell).join(' '))
    .map((text) => text.replace(/\s+/g, ' ').trim());
  const lines = cells.length >= 3 ? cells : lineTextOf(row);
  if (!lines.some(isWithdrawal)) continue;
  const timeIndex = timeIndexFromHeader >= 0 ? timeIndexFromHeader : lines.findIndex(isTime);
  const amountIndex = amountIndexFromHeader;
  const remarkLines = remarkIndex >= 0 && lines[remarkIndex]
    ? [lines[remarkIndex]]
    : (amountIndex >= 0 ? lines.slice(amountIndex + 1) : []);
  results.push({
    transactionNumber: transactionNumberIndex >= 0 ? (cells[transactionNumberIndex] || '') : '',
    time: timeIndex >= 0 ? lines[timeIndex] : '',
    amount: amountIndex >= 0 ? lines[amountIndex] : '',
    remark: remarkLines.join(' '),
    lines
  });
}
return {rootFound: true, rows: results, headers, amountColumnFound: amountIndexFromHeader >= 0, windowSignature};
"""
    try:
        payload = driver.execute_script(js_code) or []
    except Exception as exc:
        log.debug("Balance Transactions DOM parsing failed: %s", exc)
        return {
            "records": [],
            "candidate_keys": [],
            "unparsed_candidate_keys": [],
            "root_found": False,
        }

    if isinstance(payload, dict):
        rows = payload.get("rows") or []
        root_found = payload.get("rootFound") is True
        window_signature = str(payload.get("windowSignature", "") or "").strip()
    else:
        # Compatibility with injectable fakes and older probes.
        rows = payload or []
        root_found = True
        window_signature = ""

    records = []
    candidate_keys = []
    unparsed_candidate_keys = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        lines = [str(line or "").strip() for line in (item.get("lines") or []) if str(line or "").strip()]
        candidate_key = re.sub(r"\s+", " ", " | ".join(lines)).strip().lower()
        if not candidate_key:
            candidate_key = re.sub(r"\s+", " ", str(item or "")).strip().lower()
        candidate_keys.append(candidate_key)
        amount_text = str(item.get("amount", "") or "").strip()
        parsed = _parse_transaction_amount_line(amount_text, expected_country=country)
        amount_index = _find_line_index(lines, amount_text) if amount_text else -1
        if parsed is None:
            unparsed_candidate_keys.append(candidate_key)
            continue
        raw_amount, amount_value = parsed
        transaction_time = str(item.get("time", "") or "").strip()
        if not transaction_time:
            transaction_time = _find_first_transaction_time(lines)
        raw_remark = (
            str(item.get("remark", "") or "").strip()
            or _extract_transaction_remark_from_lines(lines, amount_index)
        )
        remark = _clean_transaction_remark(raw_remark)
        records.append({
            "transaction_number": str(item.get("transactionNumber", "") or "").strip(),
            "time": transaction_time,
            "raw_amount": raw_amount,
            "amount_abs": abs(amount_value),
            "remark": remark,
            "raw_remark": raw_remark,
            "_source_key": candidate_key,
        })
    return {
        "records": records,
        "candidate_keys": candidate_keys,
        "unparsed_candidate_keys": unparsed_candidate_keys,
        "root_found": root_found,
        "window_signature": window_signature or "||".join(candidate_keys),
    }


def _parse_withdrawal_transaction_records(text, expected_country=""):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    records = []
    for idx, line in enumerate(lines):
        if not _is_withdrawal_line(line):
            continue
        transaction_time = _find_nearby_transaction_time(lines, idx)
        if not transaction_time:
            continue
        amount_text = ""
        amount_value = None
        amount_index = -1
        for candidate in lines[idx + 1: idx + 8]:
            parsed = _parse_transaction_amount_line(candidate, expected_country=expected_country)
            if parsed is None:
                continue
            amount_text, amount_value = parsed
            amount_index = _find_line_index(lines, candidate, start=idx + 1)
            break
        if amount_value is None:
            continue
        records.append({
            "transaction_number": _extract_transaction_number_from_text(
                " | ".join(lines[max(0, idx - 6):idx + 1])
            ),
            "time": transaction_time,
            "raw_amount": amount_text,
            "amount_abs": abs(amount_value),
            "remark": _extract_transaction_remark_from_lines(lines, amount_index),
            "raw_remark": " ".join(lines[amount_index + 1:min(len(lines), amount_index + 12)]),
            "_source_key": " | ".join(
                lines[max(0, idx - 6):min(len(lines), max(amount_index + 12, idx + 8))]
            ),
        })
    return records


def _is_withdrawal_line(line):
    text = str(line or "").strip().lower()
    return text == "withdrawal" or text == "\u63d0\u73b0"


def _find_line_index(lines, target, start=0):
    target_text = str(target or "").strip()
    if not target_text:
        return -1
    for idx in range(max(start, 0), len(lines)):
        if str(lines[idx] or "").strip() == target_text:
            return idx
    return -1


def _find_first_transaction_time(lines):
    for candidate in lines:
        if _is_transaction_time_line(candidate):
            return candidate
    return ""


def _is_transaction_time_line(line):
    text = str(line or "").strip()
    date_patterns = (
        r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\s+\d{2}:\d{2}:\d{2}",
        r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}",
        r"\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}",
    )
    return any(re.search(pattern, text) for pattern in date_patterns)


def _extract_transaction_remark_from_lines(lines, amount_index):
    if amount_index < 0:
        return ""
    remark_lines = []
    for line in lines[amount_index + 1: amount_index + 12]:
        if _is_transaction_time_line(line):
            break
        if _is_withdrawal_line(line):
            break
        if _parse_transaction_amount_line(line) is not None:
            break
        if _is_transaction_header_line(line):
            continue
        remark_lines.append(line)
    return _clean_transaction_remark(" ".join(remark_lines))


def _is_transaction_header_line(line):
    text = re.sub(r"\s+", " ", str(line or "").strip()).lower()
    headers = {
        "transaction time", "type", "sub type", "subtype", "amount", "remarks", "remark",
        "\u4ea4\u6613\u65f6\u95f4", "\u7c7b\u578b", "\u5b50\u7c7b\u578b", "\u91d1\u989d", "\u5907\u6ce8",
    }
    return text in headers


def _clean_transaction_remark(remark):
    text = re.sub(r"\s+", " ", str(remark or "")).strip()
    if not text:
        return ""
    usd_note = _extract_usd_conversion_note(text)
    if usd_note:
        return usd_note
    parts = []
    seen = set()
    for part in re.split(r"\s{2,}|(?<=\))\s+(?=[A-Za-z0-9])", text):
        clean = part.strip()
        if not clean or _is_transaction_header_line(clean):
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        parts.append(clean)
    return " ".join(parts)


def _extract_usd_conversion_note(text):
    matches = re.findall(
        r"(?<![\d.,])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*USD\s*\([^)]*\)",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    notes = []
    seen = set()
    for match in matches:
        note = re.sub(r"\s+", " ", match).strip()
        key = note.lower()
        if key in seen:
            continue
        seen.add(key)
        notes.append(note)
    return "; ".join(notes)


def _combine_transaction_remarks(records):
    remarks = []
    for record in records:
        parts = []
        transaction_time = re.sub(r"\s+", " ", str(record.get("time", "") or "")).strip()
        transaction_number = _extract_transaction_number_from_text(
            str(record.get("transaction_number", "") or "")
        )
        raw_remark = str(record.get("raw_remark", "") or record.get("remark", "") or "")
        bank_reference = _extract_bank_reference(raw_remark)
        cleaned_remark = _clean_transaction_remark(raw_remark)
        if transaction_time:
            parts.append(transaction_time)
        if transaction_number:
            parts.append(f"Tx {transaction_number}")
        if bank_reference:
            parts.append(f"Ref {bank_reference}")
        if cleaned_remark:
            parts.append(cleaned_remark)
        if parts:
            remarks.append(" | ".join(parts))
    return "; ".join(remarks)


def _ensure_transaction_country_matches(records, expected_country):
    expected = str(expected_country or "").strip().upper()
    if not expected:
        return
    actual = _infer_transaction_country_from_records(records)
    if not actual:
        return
    if actual == "MIXED":
        raise RuntimeError(
            f"交易记录包含多个国家币种/银行参考号，目标国家={expected}，已停止写入"
        )
    if actual != expected:
        raise RuntimeError(
            f"交易记录国家与目标国家不一致：目标={expected}，实际={actual}，已停止写入"
        )


def _infer_transaction_country_from_records(records):
    countries = set()
    for record in records or []:
        for value in (record.get("remark", ""), record.get("raw_amount", "")):
            country = infer_lazada_transaction_country_from_text(value)
            if country and country != "MIXED":
                countries.add(country)
            elif country == "MIXED":
                return "MIXED"
    if len(countries) == 1:
        return next(iter(countries))
    if len(countries) > 1:
        return "MIXED"
    return ""


def infer_lazada_transaction_country_from_text(text):
    value = re.sub(r"\s+", " ", str(text or "")).upper()
    if not value:
        return ""
    countries = set()
    if re.search(r"\b1\s*PHP\b|\bPHP\b|BANK\s+(?:REF(?:ERENCE)?|TRANSACTION\s+REFERENCE)\.?\s*PH", value):
        countries.add("PH")
    if re.search(r"\b1\s*MYR\b|\bMYR\b|\bRM\b|BANK\s+(?:REF(?:ERENCE)?|TRANSACTION\s+REFERENCE)\.?\s*MY", value):
        countries.add("MY")
    if re.search(r"\b1\s*THB\b|\bTHB\b|BANK\s+(?:REF(?:ERENCE)?|TRANSACTION\s+REFERENCE)\.?\s*TH", value):
        countries.add("TH")
    if re.search(r"\b1\s*VND\b|\bVND\b|BANK\s+(?:REF(?:ERENCE)?|TRANSACTION\s+REFERENCE)\.?\s*VN", value) or re.search(r"(?:\d\s*[₫đĐ]|[₫đĐ]\s*\d)", value):
        countries.add("VN")
    if len(countries) == 1:
        return next(iter(countries))
    if len(countries) > 1:
        return "MIXED"
    return ""


def _parse_transaction_amount_line(line, expected_country=""):
    text = re.sub(r"\s+", " ", str(line or "").replace("\u00a0", " ")).strip()
    if not text:
        return None
    original = text
    explicit_currencies = set()
    upper = text.upper()
    for token, currency in (("PHP", "PH"), ("MYR", "MY"), ("RM", "MY"), ("THB", "TH"), ("VND", "VN"), ("USD", "USD")):
        if re.search(rf"(?<![A-Z]){token}(?![A-Z])", upper):
            explicit_currencies.add(currency)
    if "\u20b1" in text:
        explicit_currencies.add("PH")
    if "\u0e3f" in text:
        explicit_currencies.add("TH")
    if re.search(r"[₫đĐ]", text):
        explicit_currencies.add("VN")
    if "USD" in explicit_currencies or len(explicit_currencies) > 1:
        return None
    expected = str(expected_country or "").strip().upper()
    if expected in ("PH", "MY", "TH", "VN") and explicit_currencies and explicit_currencies != {expected}:
        return None
    accounting_negative = text.startswith("(") and text.endswith(")")
    if accounting_negative:
        text = text[1:-1].strip()
    text = re.sub(r"[\u2212\u2013\u2014]", "-", text)
    text = re.sub(
        r"(?i)(?<![A-Z])(?:PHP|MYR|THB|VND|USD|RM)(?![A-Z])|[\u20b1\u0e3f\u20ab\u0111\u0110]",
        "",
        text,
    )
    text = re.sub(r"\s+", "", text)
    if re.search(r"[A-Za-z]", text):
        return None
    if expected == "VN" or explicit_currencies == {"VN"}:
        # VND is commonly shown without cents. The portal can group thousands
        # with commas or dots; accept only unambiguous complete numeric forms.
        if re.fullmatch(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?", text):
            normalized = text.replace(",", "")
        elif re.fullmatch(r"[-+]?\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?", text):
            normalized = text.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"[-+]?\d+(?:\.\d{1,2})?", text):
            normalized = text
        else:
            return None
        try:
            value = Decimal(normalized)
        except (InvalidOperation, ValueError):
            return None
        if accounting_negative and value > 0:
            value = -value
        return original, value
    match = re.fullmatch(r"([-+]?\d{1,3}(?:,\d{3})*\.\d{2}|[-+]?\d+\.\d{2})", text)
    if not match:
        return None
    raw_number = match.group(1)
    try:
        value = Decimal(raw_number.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    if accounting_negative and value > 0:
        value = -value
    return original, value


def _find_nearby_transaction_time(lines, withdrawal_index):
    date_patterns = (
        r"\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}:\d{2}",
        r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}",
        r"\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}",
    )
    for candidate in reversed(lines[max(0, withdrawal_index - 6): withdrawal_index]):
        if any(re.search(pattern, candidate) for pattern in date_patterns):
            return candidate
    return ""


def _format_decimal_money(value):
    quantized = Decimal(value).quantize(Decimal("0.01"))
    return f"{quantized:,.2f}"


def download_thailand_income_statement(util, store_name, download_path, start_date="", end_date=""):
    """Download all Thailand income statement bills whose bill period intersects the date range."""
    driver = util.driver
    target_start = _parse_iso_date(start_date)
    target_end = _parse_iso_date(end_date)
    if not target_start or not target_end:
        raise RuntimeError("Thailand income statement flow requires start_date and end_date")
    if target_start > target_end:
        raise RuntimeError(f"Invalid Thailand date range: {start_date} > {end_date}")

    store_download_path = download_path or os.path.join(output_directory, "_browser_downloads", _safe_filename(store_name))
    os.makedirs(store_download_path, exist_ok=True)
    dest_dir = os.path.join(output_directory, "ThailandIncomeStatements", _safe_filename(store_name))
    os.makedirs(dest_dir, exist_ok=True)

    if not open_lazada_income_statement_page(util):
        raise RuntimeError("Could not open Thailand Lazada My Income > Income Statement page")
    close_lazada_popups(driver)
    if not open_lazada_income_statement_tab(driver):
        raise RuntimeError("Could not open Thailand Income Statement tab")

    results = []
    seen_statement_numbers = set()
    max_pages = 80
    page_index = 1
    while page_index <= max_pages:
        rows = wait_for_thailand_income_statement_rows(driver, timeout=35)
        if not rows:
            log.warning("Thailand income statement page %s has no visible bill rows", page_index)
            break

        parsed_periods = []
        matched_on_page = 0
        for row in rows:
            period = row.get("period_range")
            if period:
                parsed_periods.append(period)
            if not period or not _date_ranges_intersect(period[0], period[1], target_start, target_end):
                continue
            statement_number = row.get("statement_number") or ""
            if statement_number and statement_number in seen_statement_numbers:
                continue
            if statement_number:
                seen_statement_numbers.add(statement_number)
            matched_on_page += 1
            try:
                output_file = _download_thailand_income_statement_row_with_retry(
                    driver, row, store_download_path, dest_dir
                )
                result_item = dict(row)
                result_item.pop("element", None)
                _normalize_thailand_statement_result_item(result_item)
                result_item["status"] = "success"
                result_item["file"] = output_file
                results.append(result_item)
                log.info(
                    "Thailand statement downloaded: %s %s -> %s",
                    statement_number,
                    row.get("period"),
                    output_file,
                )
            except Exception as exc:
                result_item = dict(row)
                result_item.pop("element", None)
                _normalize_thailand_statement_result_item(result_item)
                result_item["status"] = "failed"
                result_item["message"] = str(exc)
                results.append(result_item)
                log.warning(
                    "Thailand statement download failed: %s %s: %s",
                    statement_number,
                    row.get("period"),
                    exc,
                )
                try:
                    _close_thailand_export_modal(driver)
                except Exception:
                    pass

        if parsed_periods and all(period_end < target_start for _, period_end in parsed_periods):
            log.info("Thailand statement pagination stopped at page %s; rows are older than target range", page_index)
            break
        if not click_thailand_income_statement_next_page(driver):
            break
        page_index += 1
        _wait_page_ready(driver, timeout=15)
        time.sleep(2)
        log.info("Moved to Thailand income statement page %s; matched_on_previous_page=%s", page_index, matched_on_page)

    return results


def _download_thailand_income_statement_row_with_retry(
    driver, row, store_download_path, dest_dir, max_attempts=2
):
    """Retry a transient export failure with a fresh statement-row element."""
    max_attempts = max(int(max_attempts), 1)
    statement_number = str(row.get("statement_number") or "")
    current_row = row
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return download_thailand_income_statement_row(
                driver, current_row, store_download_path, dest_dir
            )
        except Exception as exc:
            last_error = exc
            if attempt >= max_attempts:
                break
            log.warning(
                "Thailand statement export attempt %s/%s failed for %s: %s; retrying",
                attempt,
                max_attempts,
                statement_number,
                exc,
            )
            try:
                _close_thailand_export_modal(driver)
            except Exception:
                pass
            time.sleep(2)
            fresh_rows = get_thailand_income_statement_rows(driver)
            current_row = next(
                (
                    candidate
                    for candidate in fresh_rows
                    if candidate.get("statement_number") == statement_number
                ),
                current_row,
            )
    raise RuntimeError(
        f"statement export failed after {max_attempts} attempt(s): {last_error}"
    ) from last_error


def open_lazada_income_statement_page(util):
    """Open the target country's Finance > My Income page."""
    driver = util.driver
    if _is_lazada_my_income_page(driver):
        return True
    for target_url in _candidate_income_statement_urls(driver):
        try:
            log.info("Trying Lazada %s My Income URL: %s", _target_lazada_country(driver), target_url)
            driver.get(target_url)
            _wait_page_ready(driver)
            if _wait_for_lazada_my_income_page(driver):
                return True
        except Exception as exc:
            log.debug("Lazada My Income URL failed %s: %s", target_url, exc)

    if _click_my_income_menu(driver):
        _wait_page_ready(driver)
        return _wait_for_lazada_my_income_page(driver)
    return False


def _is_lazada_my_income_page(driver):
    if not _is_expected_lazada_country_page(driver, "My Income"):
        return False
    try:
        return bool(driver.execute_script(r"""
/* lazada-my-income-page */
const visible = el => {
  const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = el => String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
const labels = ['收入账单', '收入賬單', 'income statement'];
const tabs = Array.from(document.querySelectorAll('[role="tab"], .next-tabs-tab, [data-spm="d_income_statement"]')).filter(visible);
return tabs.some(el => labels.includes(textOf(el)));
"""))
    except Exception:
        return False


def _wait_for_lazada_my_income_page(driver, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_lazada_my_income_page(driver):
            return True
        if _is_login_url(getattr(driver, "current_url", "")):
            return False
        time.sleep(0.5)
    return False


def _click_my_income_menu(driver):
    country = _target_lazada_country(driver)
    if country not in {"PH", "MY", "TH"}:
        return False
    try:
        if _country_from_current_url(driver) != country:
            driver.get(_seller_center_origin_by_country(country))
            _wait_page_ready(driver)
            time.sleep(2)
    except Exception:
        return False
    if not _is_expected_lazada_country_page(driver, "My Income菜单"):
        return False

    income_xpaths = (
        "//a[contains(@href, '/portal/apps/finance/myIncome/index')]",
        "//a[normalize-space()='我的收入']",
        "//*[normalize-space()='我的收入']/ancestor::a[1]",
        "//a[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='my income']",
    )
    def click_income():
        for xpath in income_xpaths:
            for element in driver.find_elements(By.XPATH, xpath):
                try:
                    if _is_visible(element):
                        element.click()
                        return True
                except Exception:
                    continue
        return False

    if click_income():
        return True
    # Finance is a toggle. Click only one visible match, then retry the submenu.
    finance_xpaths = (
        "//*[normalize-space()='财务' or normalize-space()='財務' or normalize-space()='Finance']/ancestor::li[1]",
        "//*[normalize-space()='财务' or normalize-space()='財務' or normalize-space()='Finance']",
    )
    for xpath in finance_xpaths:
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if _is_visible(element):
                    element.click()
                    time.sleep(1)
                    return click_income()
            except Exception:
                continue
    return False


def open_lazada_income_statement_tab(driver):
    """Select Income Statement and verify its table or explicit empty state."""
    if not _is_expected_lazada_country_page(driver, "Income Statement"):
        return False
    js_code = """
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const tabs = Array.from(document.querySelectorAll('[role="tab"], .next-tabs-tab, [data-spm="d_income_statement"]')).filter(visible);
const target = tabs.find((el) => {
  const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
  return ['收入账单', '收入賬單', 'income statement'].includes(text.toLowerCase());
});
if (!target) return false;
target.click();
return true;
"""
    try:
        if not driver.execute_script(js_code):
            return False
    except Exception as exc:
        log.debug("Income statement tab JS click failed: %s", exc)
        return False
    deadline = time.time() + 40
    while time.time() < deadline:
        state = _get_lazada_income_statement_state(driver)
        if state.get("status") in {"ready", "empty"}:
            return True
        if state.get("status") in {"error", "wrong_country"}:
            return False
        time.sleep(1)
    return False


def _get_lazada_income_statement_state(driver):
    """Distinguish a loaded statement table from an unloaded/failed page."""
    if not _is_expected_lazada_country_page(driver, "Income Statement表格"):
        return {"status": "wrong_country"}
    try:
        state = driver.execute_script(r"""
/* lazada-income-statement-state */
const visible = el => {
  if (!el) return false;
  const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const textOf = el => String(el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
const scope = Array.from(document.querySelectorAll('.income-statement')).find(visible);
if (!scope) return {status: 'missing'};
const text = textOf(scope);
const loading = Array.from(scope.querySelectorAll('[aria-busy="true"], .next-loading.next-open, .next-loading-tip, .ant-spin-spinning, .next-table-loading')).some(visible);
if (loading) return {status: 'loading'};
const error = Array.from(scope.querySelectorAll('[role="alert"], .next-message-error, .ant-alert-error')).filter(visible).map(textOf).join(' ');
if (error || /failed to load|loading failed|network error|加载失败|載入失敗|网络异常|網絡異常|系统异常|系統異常/i.test(text)) return {status: 'error', message: error || text.slice(0, 300)};
const hasPeriod = /bill(?:ing)?\s*period|statement\s*period|账期|賬期/i.test(text);
const hasAmount = /released\s*amount|amount\s*released|已释放金额|已釋放金額/i.test(text);
const hasStatement = /view\s*statement|statement\s*(?:number|no\.?|id)|查看账单|查看賬單|账单号|賬單號/i.test(text);
if (!hasPeriod || !hasAmount || !hasStatement) return {status: 'missing'};
const rowElements = Array.from(scope.querySelectorAll('tbody tr, tr.next-table-row')).filter(visible);
const rows = rowElements.filter(el => /\bdownload\b|下载|下載/i.test(textOf(el)));
if (rows.length) return {status: 'ready', row_count: rows.length};
const emptyElements = Array.from(scope.querySelectorAll('.next-table-empty, .next-empty, .ant-empty, [class*="empty"]')).filter(visible);
const isEmptyText = value => /^(?:no\s+(?:data|records?|results?)(?:\s+(?:available|found))?|no statements?(?:\s+(?:available|found))?|暂无数据|暫無數據|暂无记录|暫無記錄|没有数据|沒有數據|无数据|無數據|没有账单|沒有賬單)[.!。]?$/i.test(value.trim());
const empty = emptyElements.some(el => isEmptyText(textOf(el)));
return empty ? {status: 'empty', row_count: 0} : {status: 'loading'};
""")
        return state if isinstance(state, dict) else {"status": "missing"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def get_thailand_income_statement_rows(driver):
    """Return target-country statement rows; the historical name stays compatible."""
    if not _is_expected_lazada_country_page(driver, "Income Statement账单行"):
        raise RuntimeError("Income Statement page country does not match the target store")
    country = _target_lazada_country(driver)
    rows = []
    for element in driver.find_elements(By.CSS_SELECTOR, ".income-statement tr.next-table-row, .income-statement tr"):
        try:
            if not _is_visible(element):
                continue
            text = driver.execute_script(
                "return String(arguments[0].innerText || arguments[0].textContent || '')",
                element,
            )
            text = re.sub(r"\s+", " ", text or "").strip()
        except StaleElementReferenceException:
            continue
        except Exception:
            text = re.sub(r"\s+", " ", element.text or "").strip()
        if not text or not re.search(r"下载|下載|\bdownload\b", text, re.IGNORECASE):
            continue
        statement_match = re.search(r"\b((PH|MY|TH)[A-Z0-9]+-\d{4}-\d+)\b", text, re.IGNORECASE)
        period_match = _find_thailand_period_match(text)
        if not statement_match or not period_match:
            continue
        if country and statement_match.group(2).upper() != country:
            raise RuntimeError("Income Statement bill country does not match the target store")
        period_start, period_end = _parse_lazada_bill_period(period_match.group(0))
        if not period_start or not period_end or period_start > period_end:
            raise RuntimeError(f"Income Statement bill period could not be parsed: {period_match.group(0)}")
        rows.append({
            "element": element,
            "statement_number": statement_match.group(1),
            "period": period_match.group(0),
            "period_range": (period_start, period_end) if period_start and period_end else None,
            "text": text,
        })
    return rows


def wait_for_thailand_income_statement_rows(driver, timeout=30):
    """Return [] only after explicit no-data evidence, never after a timeout."""
    deadline = time.time() + timeout
    last_state = {"status": "missing"}
    empty_since = None
    while time.time() < deadline:
        last_state = _get_lazada_income_statement_state(driver)
        if last_state.get("status") in {"error", "wrong_country"}:
            raise RuntimeError(f"Income Statement did not load: {last_state.get('message') or last_state['status']}")
        rows = get_thailand_income_statement_rows(driver)
        if rows and last_state.get("status") == "ready":
            if int(last_state.get("row_count", len(rows)) or 0) != len(rows):
                raise RuntimeError("Income Statement contains bill rows that could not be parsed completely")
            return rows
        if last_state.get("status") == "empty" and not rows:
            now = time.time()
            if empty_since is None:
                empty_since = now
            elif now - empty_since >= 2:
                return []
        else:
            empty_since = None
        time.sleep(1)
    raise RuntimeError(f"Income Statement table did not become ready: {last_state.get('status', 'unknown')}")


def download_thailand_income_statement_row(driver, row, store_download_path, dest_dir):
    before_snapshot = _snapshot_download_files(store_download_path)
    element = row.get("element")
    if element is None:
        raise RuntimeError("statement row element is missing")
    if not _open_thailand_statement_download_menu(driver, element):
        raise RuntimeError("could not open statement download menu")
    if not _click_thailand_order_details_excel(driver):
        raise RuntimeError("could not click Order Details (excel)")
    source_file = _click_export_result_and_wait(driver, store_download_path, before_snapshot, timeout=180)
    if not source_file:
        raise RuntimeError("download result file did not appear")
    statement_number = _safe_filename(row.get("statement_number") or "statement")
    period_start, period_end = row.get("period_range") or (None, None)
    period_part = _format_bill_period_part(period_start, period_end)
    ext = os.path.splitext(source_file)[1] or ".xlsx"
    output_name = f"{statement_number}_{period_part}_Order_Details{ext}"
    output_file = copy_file(source_file, dest_dir, output_name)
    _close_thailand_export_modal(driver)
    return output_file


def upload_thailand_income_statement_files_to_dingtalk_drive(
    store_name, file_paths, start_date="", end_date="", operator=None
):
    operator = operator or {}
    space_id = str(operator.get("thailand_drive_space_id") or THAILAND_DING_DRIVE_SPACE_ID)
    root_folder_id = str(operator.get("thailand_drive_root_folder_id") or THAILAND_DING_DRIVE_ROOT_FOLDER_ID)
    client = DingDriveClient(APP_KEY, APP_SECRET, USER_ID, space_id, root_folder_id)

    results = []
    for file_path in file_paths:
        file_path = str(file_path or "").strip()
        file_name = os.path.basename(file_path)
        try:
            if not os.path.isfile(file_path):
                raise FileNotFoundError(file_path)
            month_folder = _thailand_drive_month_folder_for_file(file_path, start_date, end_date)
            target_folder = client.ensure_path([month_folder, store_name], parent_id=root_folder_id)
            dentry = client.upload_file(file_path, target_folder.id, file_name=file_name, overwrite=True)
            drive_path = f"{month_folder}/{store_name}/{file_name}"
            results.append({
                "status": "success",
                "file": file_path,
                "drive_id": dentry.id,
                "drive_path": drive_path,
            })
            log.info("Thailand statement uploaded to DingDrive: %s -> %s", file_path, drive_path)
        except Exception as exc:
            message = f"{file_name}: {exc}"
            results.append({
                "status": "failed",
                "file": file_path,
                "drive_path": "",
                "message": message,
            })
            log.warning("Thailand statement DingDrive upload failed: %s", message)
    return results


def _thailand_drive_month_folder_for_file(file_path, start_date="", end_date=""):
    target_start = _parse_iso_date(start_date)
    target_end = _parse_iso_date(end_date)
    if target_start and target_end and target_start.year == target_end.year and target_start.month == target_end.month:
        return f"{target_start.month}月"

    file_name = os.path.basename(str(file_path or ""))
    match = re.search(r"_(\d{4})(\d{2})\d{2}_", file_name)
    if match:
        return f"{int(match.group(2))}月"
    if target_start:
        return f"{target_start.month}月"
    return f"{datetime.now().month}月"


def _normalize_thailand_statement_result_item(item):
    period_range = item.pop("period_range", None)
    if period_range:
        period_start, period_end = period_range
        item["period_start"] = period_start.strftime("%Y-%m-%d") if period_start else ""
        item["period_end"] = period_end.strftime("%Y-%m-%d") if period_end else ""


def _open_thailand_statement_download_menu(driver, row_element):
    buttons = row_element.find_elements(
        By.CSS_SELECTOR,
        'button[data-spm="income_statement_download"], button.next-menu-btn, button',
    )
    buttons = [
        button for button in buttons
        if _is_visible(button) and re.search(r"下载|下載|\bdownload\b", button.text or "", re.IGNORECASE)
    ]
    if not buttons:
        return False
    button = buttons[-1]
    for click_method in ("selenium", "action"):
        try:
            if click_method == "selenium":
                button.click()
            else:
                ActionChains(driver).move_to_element(button).pause(0.2).click().perform()
            if _wait_for_thailand_download_menu(driver, timeout=5):
                return True
        except Exception as exc:
            log.debug("Thailand statement download menu click failed (%s): %s", click_method, exc)
    return False


def _wait_for_thailand_download_menu(driver, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _lazada_order_details_excel_menu_items(driver):
            return True
        time.sleep(0.5)
    return False


def _lazada_order_details_excel_menu_items(driver):
    items = []
    for element in driver.find_elements(By.CSS_SELECTOR, 'li, [role="menuitem"]'):
        try:
            text = re.sub(r"\s+", " ", element.text or "").strip()
            if _is_visible(element) and re.fullmatch(
                r"(?:order\s*details|订单明细|訂單明細|订单详情|訂單詳情)\s*[(（]\s*excel\s*[)）]",
                text,
                re.IGNORECASE,
            ):
                items.append(element)
        except StaleElementReferenceException:
            continue
    return items


def _click_thailand_order_details_excel(driver):
    for element in _lazada_order_details_excel_menu_items(driver):
        try:
            if _is_visible(element):
                ActionChains(driver).move_to_element(element).pause(0.2).click().perform()
                return True
        except Exception as exc:
            log.debug("ActionChains click Order Details (excel) failed: %s", exc)
            try:
                element.click()
                return True
            except Exception:
                continue
    return False


def _click_export_result_and_wait(driver, store_download_path, before_snapshot, timeout=180):
    deadline = time.time() + timeout
    clicked = False
    while time.time() < deadline:
        page_text = _safe_page_text(driver, limit=5000)
        if not clicked and re.search(r"导出记录|匯出記錄|export\s*(?:records?|history)", page_text, re.IGNORECASE) and re.search(
            r"download\s*result\s*file|下载结果文件|下載結果檔案|下载文件|下載文件", page_text, re.IGNORECASE
        ):
            if _click_first_download_result_file(driver):
                clicked = True
        if clicked:
            new_file = _wait_for_new_download_file(store_download_path, before_snapshot, timeout=3)
            if new_file:
                return new_file
        time.sleep(1)
    return ""


def _click_first_download_result_file(driver):
    candidates = []
    for element in driver.find_elements(By.XPATH, "//a"):
        try:
            if _is_visible(element) and re.fullmatch(
                r"download\s*result\s*file|下载结果文件|下載結果檔案|下载文件|下載文件",
                re.sub(r"\s+", " ", element.text or "").strip(),
                re.IGNORECASE,
            ):
                candidates.append(element)
        except StaleElementReferenceException:
            continue
    if not candidates:
        return False
    try:
        ActionChains(driver).move_to_element(candidates[0]).pause(0.2).click().perform()
    except Exception:
        try:
            candidates[0].click()
        except Exception:
            return False
    return True


def _close_thailand_export_modal(driver):
    close_xpaths = (
        "//button[.//span[normalize-space()='OK'] or normalize-space()='OK']",
        "//button[.//span[normalize-space()='确定'] or normalize-space()='确定']",
        "//button[.//span[normalize-space()='取消'] or normalize-space()='取消']",
        "//*[contains(@class,'next-dialog-close') or contains(@class,'ant-modal-close')]",
    )
    for xpath in close_xpaths:
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if _is_visible(element):
                    element.click()
                    time.sleep(1)
                    return True
            except Exception:
                continue
    return False


def click_thailand_income_statement_next_page(driver):
    if not _is_expected_lazada_country_page(driver, "Income Statement分页"):
        raise RuntimeError("Income Statement page country does not match the target store")
    before_numbers = tuple(row["statement_number"] for row in get_thailand_income_statement_rows(driver))
    js_code = """
const visible = (el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
};
const disabled = (el) => {
  let node = el;
  for (let i = 0; node && i < 4; i += 1, node = node.parentElement) {
    const cls = String(node.className || '').toLowerCase();
    if (node.disabled || node.getAttribute('disabled') !== null || node.getAttribute('aria-disabled') === 'true' || cls.includes('disabled')) return true;
  }
  return false;
};
const root = Array.from(document.querySelectorAll('.income-statement')).find(visible);
if (!root) return 'missing';
const candidates = Array.from(root.querySelectorAll('.next-pagination button, .next-pagination a, .next-pagination [role="button"], .next-pagination-next, .next-next')).filter((el) => {
  if (!visible(el)) return false;
  const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
  const label = String(el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
  return /^(下一页|下一頁|next|next page)$/i.test(text) || /^(下一页|下一頁|next|next page)$/i.test(label) || el.classList.contains('next-pagination-next') || el.classList.contains('next-next');
});
if (!candidates.length) return 'missing';
const enabled = candidates.filter(el => !disabled(el));
if (!enabled.length) return 'end';
let target = enabled[0];
for (const candidate of enabled) {
  const button = candidate.closest('button,a');
  if (button && visible(button) && !disabled(button)) {
    target = button;
    break;
  }
}
target.click();
return 'clicked';
"""
    try:
        action = driver.execute_script(js_code)
    except Exception as exc:
        raise RuntimeError(f"Income Statement next page could not be clicked: {exc}") from exc
    if action == "end":
        return False
    if action != "clicked":
        raise RuntimeError("Income Statement next-page control could not be verified")
    deadline = time.time() + 15
    while time.time() < deadline:
        state = _get_lazada_income_statement_state(driver)
        if state.get("status") in {"error", "wrong_country", "empty"}:
            raise RuntimeError("Income Statement next page did not load successfully")
        if state.get("status") == "ready":
            rows = get_thailand_income_statement_rows(driver)
            after_numbers = tuple(row["statement_number"] for row in rows)
            if after_numbers and after_numbers != before_numbers:
                return True
        time.sleep(0.5)
    raise RuntimeError("Income Statement next-page rows did not change")


def _find_thailand_period_match(text):
    date_pattern = r"(?:\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日)"
    return re.search(
        rf"(?P<period_start>{date_pattern})\s*[-–—~至]\s*(?P<period_end>{date_pattern})",
        str(text or ""),
    )


def _parse_lazada_bill_period(period_text):
    match = _find_thailand_period_match(period_text)
    if not match:
        return None, None
    return _parse_lazada_date(match.group("period_start")), _parse_lazada_date(match.group("period_end"))


def _parse_lazada_date(value):
    text = re.sub(r"\s+", " ", str(value or "").strip())
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_iso_date(value):
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _date_ranges_intersect(left_start, left_end, right_start, right_end):
    return bool(left_start and left_end and right_start and right_end and left_start <= right_end and left_end >= right_start)


def _format_bill_period_part(period_start, period_end):
    if period_start and period_end:
        return f"{period_start.strftime('%Y%m%d')}_{period_end.strftime('%Y%m%d')}"
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _snapshot_download_files(download_dir):
    snapshot = {}
    if not os.path.isdir(download_dir):
        return snapshot
    for name in os.listdir(download_dir):
        path = os.path.join(download_dir, name)
        if os.path.isfile(path):
            try:
                snapshot[name] = os.path.getmtime(path)
            except OSError:
                snapshot[name] = 0
    return snapshot


def _wait_for_new_download_file(download_dir, before_snapshot, timeout=120):
    deadline = time.time() + timeout
    excel_extensions = {".xls", ".xlsx", ".csv"}
    while time.time() < deadline:
        if not os.path.isdir(download_dir):
            time.sleep(1)
            continue
        candidates = []
        for name in os.listdir(download_dir):
            path = os.path.join(download_dir, name)
            ext = os.path.splitext(name)[1].lower()
            if not os.path.isfile(path) or ext not in excel_extensions:
                continue
            if name.endswith(".crdownload") or name.endswith(".tmp"):
                continue
            try:
                modified = os.path.getmtime(path)
                size = os.path.getsize(path)
            except OSError:
                continue
            if size <= 0:
                continue
            if name not in before_snapshot or modified > before_snapshot.get(name, 0) + 0.5:
                candidates.append(path)
        if candidates:
            candidates.sort(key=lambda item: os.path.getmtime(item), reverse=True)
            latest = candidates[0]
            try:
                size_a = os.path.getsize(latest)
                time.sleep(1)
                size_b = os.path.getsize(latest)
                if size_a == size_b:
                    return latest
            except OSError:
                pass
        time.sleep(1)
    return ""


def _currency_by_country(country):
    if country == "TH":
        return "THB"
    if country == "MY":
        return "MYR"
    if country == "PH":
        return "PHP"
    if country == "VN":
        return "VND"
    return ""


def extract_withdrawal_amount(driver, country):
    """
    从页面采集提现/余额金额
    返回: (amount_str, currency_str)
    """
    log.info("开始采集金额")

    amount, currency = _extract_withdrawable_amount(driver, country)
    if amount:
        log.info(f"采集到 Withdrawable amount: {amount} {currency}")
        return amount, currency

    # 策略1: 按关键词查找金额元素
    for keyword in AMOUNT_KEYWORDS:
        amount, currency = _find_amount_near_keyword(driver, keyword, country)
        if amount:
            log.info(f"通过关键词 '{keyword}' 采集到金额: {amount} {currency}")
            return amount, currency

    # 策略2: 查找页面中所有包含货币符号的文本
    amount, currency = _find_amount_by_currency_symbol(driver, country)
    if amount:
        log.info(f"通过货币符号采集到金额: {amount} {currency}")
        return amount, currency

    # 策略3: 查找大号数字（可能是金额展示）
    amount, currency = _find_large_number_element(driver, country)
    if amount:
        log.info(f"通过大号数字采集到金额: {amount} {currency}")
        return amount, currency

    log.warning("未能采集到金额")
    return "", ""


def _find_amount_near_keyword(driver, keyword, country):
    """在关键词附近查找金额"""
    keyword_lower = keyword.lower()
    xpath = (
        f"//*[contains(translate(normalize-space(.), "
        f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{keyword_lower}')]"
    )
    for element in driver.find_elements(By.XPATH, xpath):
        try:
            if not _is_visible(element):
                continue
            text = element.text.strip()
            if not text:
                continue

            # 在元素文本中查找金额
            amount, currency = _parse_amount_from_text(text, country)
            if amount:
                return amount, currency

            # 在父元素或兄弟元素中查找
            parent = element.find_element(By.XPATH, "./..")
            parent_text = parent.text.strip()
            amount, currency = _parse_amount_from_text(parent_text, country)
            if amount:
                return amount, currency
        except (StaleElementReferenceException, NoSuchElementException):
            continue
    return "", ""


def _find_amount_by_currency_symbol(driver, country):
    """通过货币符号查找金额"""
    symbols = ["₱", "RM", "PHP", "MYR"]
    for symbol in symbols:
        xpath = f"//*[contains(normalize-space(.), '{symbol}')]"
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if not _is_visible(element):
                    continue
                text = element.text.strip()
                amount, currency = _parse_amount_from_text(text, country)
                if amount:
                    return amount, currency
            except StaleElementReferenceException:
                continue
    return "", ""


def _find_large_number_element(driver, country):
    """查找页面中可能是金额的大号数字"""
    # 查找 class 中包含 amount/balance/price 的元素
    selectors = [
        "[class*='amount' i]",
        "[class*='balance' i]",
        "[class*='price' i]",
        "[class*='total' i]",
        "[class*='money' i]",
    ]
    for selector in selectors:
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if not _is_visible(element):
                    continue
                text = element.text.strip()
                amount, currency = _parse_amount_from_text(text, country)
                if amount:
                    return amount, currency
            except StaleElementReferenceException:
                continue
    return "", ""


def _extract_withdrawable_amount(driver, country):
    """优先读取 My Balance 卡片里的 Withdrawable amount。"""
    try:
        body_text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    except Exception:
        body_text = ""
    normalized = re.sub(r"\s+", " ", str(body_text)).strip()
    direct_patterns = (
        r"Withdrawable amount\s*:?\s*(?:PHP|MYR|RM|₱)?\s*([\d,]+(?:\.\d{1,2})?)",
        r"可提现余额\s*:?\s*(?:PHP|MYR|RM|₱)?\s*([\d,]+(?:\.\d{1,2})?)",
    )
    for pattern in direct_patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            currency = "PHP" if country == "PH" else "MYR" if country == "MY" else ""
            if "MYR" in match.group(0).upper() or "RM" in match.group(0):
                currency = "MYR"
            elif "PHP" in match.group(0).upper() or "₱" in match.group(0):
                currency = "PHP"
            return match.group(1), currency

    js_code = r"""
const labels = ['Withdrawable amount', 'Available Balance', '可提现余额'];
function visible(el) {
  const rect = el.getBoundingClientRect();
  const style = getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
}
function textOf(el) {
  return String(el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
}
function hasMoney(text) {
  return /(PHP|MYR|RM|₱)\s*[\d,]+(?:\.\d+)?|[\d,]+\.\d{1,2}/i.test(text);
}
function nearestMoneyAfter(labelEl) {
  const root = labelEl.closest('.card, .panel, .next-card, .a-card, section, div') || document.body;
  const all = Array.from(root.querySelectorAll('*')).filter(visible);
  const start = all.indexOf(labelEl);
  const scan = start >= 0 ? all.slice(start, Math.min(all.length, start + 30)) : all;
  for (const el of scan) {
    const text = textOf(el);
    if (text && hasMoney(text) && !/Transaction Number/i.test(text)) return text;
  }
  return textOf(root);
}
for (const el of Array.from(document.querySelectorAll('*')).filter(visible)) {
  const text = textOf(el);
  if (!text) continue;
  if (labels.some(label => text.toLowerCase() === label.toLowerCase() || text.toLowerCase().includes(label.toLowerCase()))) {
    return nearestMoneyAfter(el);
  }
}
return '';
"""
    try:
        text = driver.execute_script(js_code) or ""
    except Exception:
        text = ""
    return _parse_amount_from_text(str(text), country)


def _parse_amount_from_text(text, country):
    """
    从文本中解析金额和币种
    返回: (amount_str, currency_str)
    """
    if not text:
        return "", ""

    # 检测币种
    currency = ""
    if "₱" in text or "PHP" in text.upper():
        currency = "PHP"
    elif "RM" in text or "MYR" in text.upper():
        currency = "MYR"
    elif country == "PH":
        currency = "PHP"
    elif country == "MY":
        currency = "MYR"

    patterns = [
        r"(?:PHP|MYR)\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)",
        r"(?:₱|RM)\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)",
        r"(?<!\\d)(\d{1,3}(?:,\d{3})+\\.\\d{1,2})(?!\\d)",
        r"(?<!\\d)(\d+\\.\\d{1,2})(?!\\d)",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text)
        if matches:
            # 取最大的数字（通常是总金额）
            amounts = []
            for m in matches:
                clean = m.replace(",", "")
                try:
                    if len(clean.split(".")[0]) > 9:
                        continue
                    amounts.append((float(clean), m))
                except ValueError:
                    continue
            if amounts:
                amounts.sort(key=lambda x: x[0], reverse=True)
                return amounts[0][1], currency

    return "", ""


# ========== 通用工具函数 ==========


def _get_seller_origin(driver):
    """获取当前 Lazada Seller Center 的域名 origin"""
    current = driver.current_url
    match = re.match(r"^(https?://[^/]+)", current)
    if match:
        return match.group(1)
    return ""


def _normalize_lazada_country(country):
    value = str(country or "").strip().upper()
    aliases = {
        "PHILIPPINES": "PH",
        "FILIPINO": "PH",
        "MALAYSIA": "MY",
        "THAILAND": "TH",
        "VIETNAM": "VN",
    }
    value = aliases.get(value, value)
    return value if value in {"PH", "MY", "TH", "VN"} else ""


def _target_lazada_country(driver):
    return _normalize_lazada_country(
        getattr(driver, "_lazada_target_country", "")
        or _country_from_current_url(driver)
        or ""
    )


def _is_expected_lazada_country_page(driver, context=""):
    if _is_login_url(getattr(driver, "current_url", "")):
        return False
    expected = _normalize_lazada_country(getattr(driver, "_lazada_target_country", ""))
    if not expected:
        return True
    actual = _country_from_current_url(driver)
    if expected == "VN" and actual != "VN":
        log.warning("%s 未进入已确认的越南 Seller Center 页面", context or "Lazada页面")
        return False
    if not actual or actual == expected:
        return True
    log.warning(
        "%s 当前页面国家=%s，与目标国家=%s 不一致，跳过该页面",
        context or "Lazada页面",
        actual,
        expected,
    )
    return False


def _country_from_current_url(driver):
    current = str(driver.current_url or "").lower()
    if urlsplit(current).hostname == "sellercenter.lazada.vn" or "sellercenter-vn" in current:
        return "VN"
    if ".co.th" in current or "sellercenter-th" in current:
        return "TH"
    if ".com.ph" in current or "sellercenter-ph" in current:
        return "PH"
    if ".com.my" in current or "sellercenter-my" in current:
        return "MY"
    return ""


def _seller_center_origin_by_country(country):
    country = _normalize_lazada_country(country)
    if country == "TH":
        return "https://sellercenter.lazada.co.th/"
    if country == "MY":
        return "https://sellercenter.lazada.com.my/"
    if country == "PH":
        return "https://sellercenter.lazada.com.ph/"
    if country == "VN":
        return "https://sellercenter.lazada.vn/"
    return ""


def _candidate_balance_urls(driver):
    country = _target_lazada_country(driver)
    origins = []
    if country:
        origin = _seller_center_origin_by_country(country).rstrip("/")
        if origin:
            origins.append(origin)
    else:
        current_origin = (_get_seller_origin(driver) or "").rstrip("/")
        if "sellercenter.lazada" in current_origin.lower():
            origins.append(current_origin)
        origins.extend([
            "https://sellercenter.lazada.com.ph",
            "https://sellercenter.lazada.com.my",
            "https://sellercenter.lazada.co.th",
        ])
    seen = set()
    for origin in origins:
        if origin in seen:
            continue
        seen.add(origin)
        yield origin + "/apps/balance"


def _candidate_income_statement_urls(driver):
    country = _target_lazada_country(driver)
    if country not in {"PH", "MY", "TH"}:
        return
    origin = _seller_center_origin_by_country(country).rstrip("/")
    paths = (
        "/portal/apps/finance/myIncome/index",
        "/apps/finance/account-statement",
        "/apps/account-statement",
        "/apps/statement",
        "/apps/finance/income-statement",
    )
    for path in paths:
        yield origin + path


def _safe_filename(value):
    safe = re.sub(r"[\\/:*?\"<>|]+", "_", str(value or "").strip())
    safe = re.sub(r"\s+", " ", safe).strip(" .")
    return safe or "store"


def _format_statement_date_part(start_date, end_date):
    start = re.sub(r"[^\d]", "", str(start_date or ""))
    end = re.sub(r"[^\d]", "", str(end_date or ""))
    if start and end:
        return f"{start}_{end}"
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe_page_text(driver, limit=1000):
    try:
        text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    except Exception:
        text = ""
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text[:limit]


def _is_visible(element):
    """检查元素是否可见"""
    try:
        return element.is_displayed() and element.size.get("width", 0) > 0
    except StaleElementReferenceException:
        return False


def _is_enabled(element):
    """检查元素是否可用"""
    try:
        return element.is_enabled()
    except StaleElementReferenceException:
        return False


def safe_click(driver, element):
    """安全点击：先滚动到元素，再通过 JS 点击"""
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'center'});", element
        )
        time.sleep(0.2)
    except Exception:
        pass
    try:
        driver.execute_script("arguments[0].click();", element)
    except Exception:
        try:
            element.click()
        except Exception:
            pass


def _type_text(element, text):
    """逐字符输入文本"""
    for char in text:
        element.send_keys(char)
        time.sleep(0.05)


def _wait_page_ready(driver, timeout=30):
    """等待页面加载完成"""
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        log.warning("页面加载等待超时，继续尝试执行")


def append_record(record):
    """将处理结果追加写入 CSV 记录文件"""
    if not record_file_path:
        return
    os.makedirs(os.path.dirname(record_file_path), exist_ok=True)
    with _RECORD_CSV_LOCK:
        exists = os.path.exists(record_file_path)
        with open(record_file_path, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=RESULT_CSV_FIELDNAMES, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerow(record)


def update_record_result(record):
    """Rewrite the current run row after a later local-evidence gate changes status."""
    if not record_file_path or not os.path.exists(record_file_path):
        return False
    detail_key = str(record.get("detail_key", "") or "").strip()
    if not detail_key:
        return False
    with _RECORD_CSV_LOCK:
        with open(record_file_path, "r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        matching = [index for index, row in enumerate(rows) if row.get("detail_key", "") == detail_key]
        if len(matching) != 1:
            return False
        target = dict(rows[matching[0]])
        for field_name in RESULT_CSV_FIELDNAMES:
            if field_name in record:
                target[field_name] = record[field_name]
        rows[matching[0]] = target
        temp_path = f"{record_file_path}.tmp"
        with open(temp_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=RESULT_CSV_FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_path, record_file_path)
        return True


def append_detail_records(record, sheet_write_status="", sheet_write_reason=""):
    """Append one row per transaction, or one evidence row for a zero-row result."""
    if not detail_file_path:
        return 0
    country = str(record.get("country", "") or "").strip().upper()
    if country not in ("PH", "MY", "TH", "VN"):
        return 0

    directory = os.path.dirname(detail_file_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    transaction_records = list(
        record.get("transaction_records")
        or record.get("records")
        or []
    )
    rows = transaction_records or [None]
    common = {
        "detail_key": str(
            record.get("detail_key", "")
            or "|".join((
                str(record.get("store_name", "") or ""),
                str(record.get("started_at", "") or ""),
                str(record.get("query_screenshot", "") or ""),
            ))
        ),
        "store_name": str(record.get("store_name", "") or ""),
        "country": country,
        "start_date": str(record.get("start_date", "") or ""),
        "end_date": str(record.get("end_date", "") or ""),
        "trade_time": str(record.get("trade_time", "") or ""),
        "status": str(record.get("status", "") or ""),
        "collection_outcome": str(record.get("collection_outcome", "") or ""),
        "collection_complete": bool(record.get("collection_complete")),
        "empty_confirmed": bool(record.get("empty_confirmed")),
        "record_count": int(record.get("record_count", 0) or 0),
        "candidate_count": int(record.get("candidate_count", 0) or 0),
        "detail_consistent": record.get("detail_consistent") is not False,
        "detail_consistency_message": str(record.get("detail_consistency_message", "") or ""),
        "query_screenshot": str(record.get("query_screenshot", "") or ""),
        "result_screenshot": str(record.get("result_screenshot", "") or ""),
        "diagnostic_screenshot": str(record.get("diagnostic_screenshot", "") or ""),
        "screenshot": str(record.get("screenshot", "") or ""),
        "message": str(record.get("message", "") or ""),
        "sheet_write_status": str(sheet_write_status or ""),
        "sheet_write_reason": str(sheet_write_reason or ""),
    }

    with _DETAIL_CSV_LOCK:
        exists = os.path.exists(detail_file_path) and os.path.getsize(detail_file_path) > 0
        with open(detail_file_path, "a", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=DETAIL_CSV_FIELDNAMES,
                extrasaction="ignore",
            )
            if not exists:
                writer.writeheader()
            for index, transaction in enumerate(rows, start=1):
                detail = dict(common)
                detail["record_index"] = index if transaction is not None else ""
                if transaction is not None:
                    detail.update({
                        "transaction_number": str(transaction.get("transaction_number", "") or ""),
                        "transaction_time": str(transaction.get("time", "") or ""),
                        "raw_amount": str(transaction.get("raw_amount", "") or ""),
                        "amount_abs": str(transaction.get("amount_abs", "") or ""),
                        "currency": str(transaction.get("currency", "") or record.get("currency", "") or ""),
                        "remark": str(transaction.get("remark", "") or ""),
                        "raw_remark": str(transaction.get("raw_remark", "") or ""),
                    })
                else:
                    detail["currency"] = str(record.get("currency", "") or "")
                writer.writerow(detail)
    return len(rows)


def update_detail_records_sheet_outcome(record, sheet_write_status, sheet_write_reason=""):
    """Update the provisional sheet outcome for one already-exported store result."""
    if not detail_file_path or not os.path.exists(detail_file_path):
        return 0
    detail_key = str(
        record.get("detail_key", "")
        or "|".join((
            str(record.get("store_name", "") or ""),
            str(record.get("started_at", "") or ""),
            str(record.get("query_screenshot", "") or ""),
        ))
    )
    with _DETAIL_CSV_LOCK:
        with open(detail_file_path, "r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        updated = 0
        for row in rows:
            if str(row.get("detail_key", "") or "") != detail_key:
                continue
            row["sheet_write_status"] = str(sheet_write_status or "")
            row["sheet_write_reason"] = str(sheet_write_reason or "")
            updated += 1
        if not updated:
            return 0
        temp_path = f"{detail_file_path}.tmp"
        with open(temp_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=DETAIL_CSV_FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_path, detail_file_path)
        return updated
