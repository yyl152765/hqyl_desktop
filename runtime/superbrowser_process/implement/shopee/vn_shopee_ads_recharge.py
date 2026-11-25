import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime
from functools import lru_cache

import yaml
from selenium.common import WebDriverException
from selenium.common.exceptions import InvalidSessionIdException, NoSuchWindowException
from selenium.webdriver.remote.remote_connection import RemoteConnection
from selenium.webdriver import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

process_config_path = os.path.dirname(os.path.abspath(__file__))
project_path = os.path.dirname(os.path.dirname(process_config_path))
if project_path not in sys.path:
    sys.path.insert(0, project_path)

from selenium.webdriver.common.by import By

from util.log import setup_logger
from util.shopee_util import shopee_check_and_login_version1, shopee_check_and_login_version2
from util.sms_otp_task_api import (
    SmsOtpTaskError,
    create_sms_otp_task,
    get_sms_otp_task,
    mark_sms_sent,
    wait_for_sms_otp_code,
)
from util.webdriver_util import WebdriverUtil
from util.yingdao_util import append_txt, create_directory, create_txt, read_txt
from util import dingtalk_doc_api as dtd
from util.shopee_ads_money import parse_shopee_money, ShopeeMoneyParseError
from implement.shopee.ads_verified_collection import (
    AdsDataValidationError, mark_ads_pending_review,
    prepare_verified_ads_budget, sync_verified_ads_inputs, write_ads_result,
)
from implement.shopee.ads_expense_date_range import select_previous_7_days_expense_range
from implement.shopee.ads_recharge_rules import (
    format_minimum_recharge_skip_message,
    is_recharge_amount_below_minimum,
)

PROCESS_NAME = "Shopee越南广告充值"
CHANNEL = "Shopee"
SITE_CODE = "vn"
DEFAULT_ADS_WALLET_URL = "https://banhang.shopee.vn/portal/marketing/pas/index"


def _clean_currency_text(value):
    text = str(value or "")
    for token in ("฿", "RM", "Rp", "IDR", "₫", "VND", "PHP", "₱"):
        text = text.replace(token, "")
    return text.strip()


def _load_yaml_or_default(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    return loaded if isinstance(loaded, dict) else default


def _safe_makedirs(path):
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except OSError:
        return False


def _safe_create_txt(path):
    parent = os.path.dirname(path)
    if parent and not _safe_makedirs(parent):
        return False
    try:
        if not os.path.exists(path):
            create_txt(path)
        return True
    except OSError:
        return False


def _resolve_initial_output_path(configured_path):
    configured_path = str(configured_path or "").strip()
    fallback_path = os.path.join(
        os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
        "ShopeeVietnamAdsRecharge",
    )
    if configured_path:
        probe_path = os.path.join(configured_path, CHANNEL, PROCESS_NAME)
        if _safe_makedirs(probe_path):
            return configured_path
    _safe_makedirs(fallback_path)
    return fallback_path


_default_output_path = os.path.join(
    os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
    "ShopeeVietnamAdsRecharge",
)
cfg = _load_yaml_or_default(
    f"{project_path}/config/config.yaml",
    {
        "ziniao": {"script": {"output_path": _default_output_path}},
        "email_config": {"userinfo": {}},
    },
)
pcfg = _load_yaml_or_default(
    f"{project_path}/main/shopee/config/{PROCESS_NAME}.yaml",
    {
        "shopee": {"ads_wallet_url": DEFAULT_ADS_WALLET_URL},
        "dingtalk": {"doc_config": {}},
    },
)

output_path = _resolve_initial_output_path(
    cfg.get("ziniao", {}).get("script", {}).get("output_path", _default_output_path) or _default_output_path
)
email_name = cfg.get("email_config", {}).get("userinfo", {}).get("email_name", "")
email_auth = cfg.get("email_config", {}).get("userinfo", {}).get("email_auth", "")
ads_wallet_url = pcfg.get("shopee", {}).get("ads_wallet_url", "") or DEFAULT_ADS_WALLET_URL
security_verify_config = pcfg.get("shopee", {}).get("security_verify", {})
PAYMENT_RESULT_TIMEOUT_SECONDS = 150

doc_config = pcfg.get("dingtalk", {}).get("doc_config", {})


pro_directory = rf"{output_path}\{CHANNEL}\{PROCESS_NAME}"
output_directory = create_directory(rf"{pro_directory}\output")
error_directory = create_directory(rf"{pro_directory}\error")
log_directory = rf"{pro_directory}\logs"
error_shop_file = rf"{error_directory}\{PROCESS_NAME}_失败店铺_{datetime.today().month}.{datetime.today().day}.txt"
success_shop_file = rf"{error_directory}\{PROCESS_NAME}_成功店铺_{datetime.today().month}.{datetime.today().day}.txt"
if not _safe_create_txt(error_shop_file):
    output_path = _default_output_path
    pro_directory = rf"{output_path}\{CHANNEL}\{PROCESS_NAME}"
    output_directory = create_directory(rf"{pro_directory}\output")
    error_directory = create_directory(rf"{pro_directory}\error")
    log_directory = rf"{pro_directory}\logs"
    error_shop_file = rf"{error_directory}\{PROCESS_NAME}_失败店铺_{datetime.today().month}.{datetime.today().day}.txt"
    success_shop_file = rf"{error_directory}\{PROCESS_NAME}_成功店铺_{datetime.today().month}.{datetime.today().day}.txt"
    _safe_create_txt(error_shop_file)
_safe_create_txt(success_shop_file)
_safe_makedirs(log_directory)

log = setup_logger(
    PROCESS_NAME,
    rf"{log_directory}\{PROCESS_NAME}_{datetime.today().strftime('%Y%m%d')}.log",
)

_write_lock = threading.Lock()



def _write_error(store_name, message):
    log.info(message)
    with _write_lock:
        if not os.path.exists(error_shop_file):
            create_txt(error_shop_file)
        error_txt = read_txt(error_shop_file)
        if store_name not in error_txt:
            append_txt(error_shop_file, message)

def _write_success(store_name, message):
    log.info(message)
    with _write_lock:
        if not os.path.exists(success_shop_file):
            create_txt(success_shop_file)
        error_txt = read_txt(success_shop_file)
        if store_name not in error_txt:
            append_txt(success_shop_file, message)


def _ensure_login(webdriver_util, store_name):
    current_url = ""
    try:
        current_url = webdriver_util.driver.current_url
    except Exception:
        pass

    if "仓发" in store_name:
        return shopee_check_and_login_version2(
            webdriver_util,
            store_name,
            error_shop_file,
            email_name,
            email_auth,
            timeout=5,
        )

    return shopee_check_and_login_version1(webdriver_util, timeout=5)

def ansy_dingtalk_doc(ads_credit, sales, shop_name, *, expense_snapshot=None, balance_text=None):
    """Compatibility entry: an unverified amount can no longer authorize payment."""
    if not expense_snapshot or expense_snapshot.get("amount") != sales:
        raise AdsDataValidationError("缺少经过校验的七天广告采集快照")
    return sync_verified_ads_inputs(
        store_name=shop_name, site_code=SITE_CODE, snapshot=expense_snapshot,
        balance=ads_credit, balance_text=balance_text, dtd=dtd,
        doc_config=doc_config, output_directory=output_directory, log=log,
    )


"""Ads Credit xPATH"""
_ADS_CREDIT_LABEL_MATCH = (
    "contains(normalize-space(.), 'Ads Credit') or "
    "contains(normalize-space(.), 'Số dư quảng cáo') or "
    "contains(normalize-space(.), '广告余额')"
)
ADS_CREDIT_XPATH = (
    "//*[@data-testid='account-panel']"
    "//div[contains(concat(' ', normalize-space(@class), ' '), ' credit-expense-label-wrapper ')]"
    f"[.//*[contains(concat(' ', normalize-space(@class), ' '), ' light-text ') and ({_ADS_CREDIT_LABEL_MATCH})]]"
    "//div[contains(concat(' ', normalize-space(@class), ' '), ' credit-value-line ')]"
    "//div[contains(concat(' ', normalize-space(@class), ' '), ' currency-text ')]"
    "//div[contains(concat(' ', normalize-space(@class), ' '), ' single ')]"
)
ADS_CREDIT_FALLBACKS = [
    ADS_CREDIT_XPATH,
    (
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' credit-expense-label-wrapper ')]"
        f"[./*[contains(concat(' ', normalize-space(@class), ' '), ' light-text ') and ({_ADS_CREDIT_LABEL_MATCH})]]"
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' credit-value-line ')]"
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' currency-text ')]"
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' single ')]"
    ),
    (
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' summary-primary ')]"
        f"[.//*[contains(concat(' ', normalize-space(@class), ' '), ' summary-label ') and ({_ADS_CREDIT_LABEL_MATCH})]]"
        "/*[contains(concat(' ', normalize-space(@class), ' '), ' summary-amount ')]"
    ),
]
_ADS_CREDIT_AMOUNT_PATTERN = re.compile(
    r"^\s*(?:(?:VND|₫)\s*)?"
    r"(?P<amount>(?:\d{1,3}(?:[.,]\d{3})+|\d+))"
    r"\s*(?:VND|₫)?\s*$",
    re.IGNORECASE,
)
"""sales xPATH"""
SALES_XPATH = "//div[contains(@class, 'metric-item')]//span[text()='Expense' or text()='花费']/ancestor::div[contains(@class, 'statistic-item')]//span[contains(@class, 'text')]"
"""TOP_UP xPATH"""
TOP_UP_BUTTON = "//button[@data-testid='topup-entry']"
TOP_UP_BUTTON_FALLBACKS = [
    TOP_UP_BUTTON,
    "//button[.//*[normalize-space()='Top Up' or normalize-space()='充值' or normalize-space()='เติมเงิน'] or normalize-space()='Top Up' or normalize-space()='充值' or normalize-space()='เติมเงิน']",
    "//*[self::button or @role='button'][contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'top up')]",
    "//*[self::button or @role='button'][contains(normalize-space(.), '充值') or contains(normalize-space(.), 'เติมเงิน')]",
]
"""Enter the Top-up Amount"""
ENTER_THE_TOP_UP_AMOUNT = "//div[@data-testid='topup-custom-amount']//button"
ENTER_THE_TOP_UP_AMOUNT_FALLBACKS = [
    ENTER_THE_TOP_UP_AMOUNT,
    "//*[contains(@data-testid, 'topup-custom')]//*[self::button or @role='button']",
    "//*[self::button or @role='button'][contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'enter')]",
    "//*[self::button or @role='button'][contains(normalize-space(.), '确定') or contains(normalize-space(.), 'ยืนยัน')]",
]

"""输入金额"""
RECHARGE_INPUT = "//div[@data-testid='topup-custom-amount']//input[@type='text']"
RECHARGE_INPUT_FALLBACKS = [
    RECHARGE_INPUT,
    "//*[contains(@data-testid, 'topup-custom')]//input",
    "//input[@type='text' or @type='number']",
]
"""校验金额"""
VERIFY_AMOUNT = "//div[@data-testid='topup-product-price']"

"""校验金额"""
VERIFY_AMOUNT2 = "//div[contains(@class, 'product-price') and contains(@class, 'price-item')]/div[@class='value']"
VERIFY_AMOUNT2_FALLBACKS = [
    VERIFY_AMOUNT2,
    "//div[contains(@class, 'product-price')]//*[contains(@class, 'value')]",
    "//*[contains(@class, 'price-item')][.//*[contains(text(), 'Top-up') or contains(text(), '充值')]]//*[contains(@class, 'value')]",
    "(//div[contains(@class, 'price-item')]//*[contains(@class, 'value')])[1]",
]

"""Confirm"""
CONFIRM_BTN = "//div[contains(@class, 'button-wrap')]//button[contains(@class, 'eds-button--primary')]"

"""提交按钮"""
RECHARGE_SUBMIT = "//button[@data-testid='topup-checkout']"
"""关闭弹框按钮"""
CLOSE_ALERT = (
    "//i[contains(@class, 'eds-modal__close')]"
    " | //button[contains(normalize-space(), 'Got It')]"
    " | //*[self::button or @role='button'][contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'close')]"
    " | //*[self::button or @role='button'][contains(translate(@title, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'close')]"
    " | //*[contains(translate(@data-testid, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'close')]"
    " | //*[contains(translate(@class, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'modal')"
    "       and contains(translate(@class, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'close')]"
    " | //*[self::button or @role='button'][normalize-space()='×' or normalize-space()='X'"
    "       or normalize-space()='关闭' or normalize-space()='關閉' or normalize-space()='Đóng']"
)


_DISMISS_VISIBLE_PROMO_SCRIPT = r"""
const visible = (element) => {
    if (!element || !element.isConnected) return false;
    const style = window.getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
    const rect = element.getBoundingClientRect();
    return rect.width >= 4 && rect.height >= 4 && rect.bottom > 0 && rect.right > 0;
};
const description = (element) => [
    element.getAttribute('aria-label'),
    element.getAttribute('title'),
    element.getAttribute('data-testid'),
    element.getAttribute('class'),
    element.textContent,
].filter(Boolean).join(' ').trim().toLowerCase();
const click = (element, reason) => {
    const target = element.closest('button,a,[role="button"]') || element;
    target.click();
    return {clicked: true, reason, overlayVisible: true};
};

for (const candidate of document.querySelectorAll(
    'button,a,[role="button"],[aria-label],[title],[data-testid],i,svg'
)) {
    if (!visible(candidate)) continue;
    const rect = candidate.getBoundingClientRect();
    if (rect.width > 120 || rect.height > 120) continue;
    const text = description(candidate);
    if (/(^|[\s_-])(close|dismiss|got[\s_-]*it)([\s_-]|$)|đóng|关闭|關閉/.test(text)
        || /^[x×✕✖]$/i.test((candidate.textContent || '').trim())) {
        return click(candidate, 'semantic-close');
    }
}

const roots = Array.from(document.querySelectorAll(
    '[role="dialog"],[aria-modal="true"],[class*="modal" i],[class*="popup" i],[class*="dialog" i]'
)).filter((root) => {
    if (!visible(root)) return false;
    const rect = root.getBoundingClientRect();
    const style = window.getComputedStyle(root);
    return rect.width * rect.height >= 40000
        && (style.position === 'fixed' || root.getAttribute('role') === 'dialog' || root.getAttribute('aria-modal') === 'true');
});

for (const root of roots) {
    const images = Array.from(root.querySelectorAll('img')).filter(visible);
    images.sort((left, right) => {
        const l = left.getBoundingClientRect();
        const r = right.getBoundingClientRect();
        return (r.width * r.height) - (l.width * l.height);
    });
    const image = images[0];
    if (!image) continue;
    const imageRect = image.getBoundingClientRect();
    if (imageRect.width * imageRect.height < 30000) continue;
    const candidates = Array.from(root.querySelectorAll(
        'button,a,[role="button"],[aria-label],[title],[data-testid],i,svg'
    )).filter(visible);
    for (const candidate of candidates) {
        const rect = candidate.getBoundingClientRect();
        if (rect.width < 8 || rect.height < 8 || rect.width > 80 || rect.height > 80) continue;
        const centerX = rect.left + rect.width / 2;
        const centerY = rect.top + rect.height / 2;
        const nearPromoTopRight = Math.abs(centerX - imageRect.right) <= 100
            && centerY >= imageRect.top - 90
            && centerY <= imageRect.top + 120;
        if (nearPromoTopRight) return click(candidate, 'promo-top-right');
    }
}

return {clicked: false, reason: '', overlayVisible: roots.length > 0};
"""
"""税费"""
VAT = "//div[contains(@class, 'tax-rate') and contains(@class, 'price-item')]/div[@class='value']"

"""支付总额"""
PAYMENT_TOTAL = "//div[contains(@class, 'final-price') and contains(@class, 'price-item')]/*[@class='value']"
PAYMENT_TOTAL_FALLBACKS = [
    PAYMENT_TOTAL,
    "//div[contains(@class, 'final-price')]//*[contains(@class, 'value')]",
    "(//div[contains(@class, 'price-item')]//*[contains(@class, 'value')])[last()]",
]
"""信用卡支付方式"""
CREDIT_CARD_LABELS = [
    "Credit / Debit Card",
    "Credit/Debit Card",
    "Debit / Credit Card",
    "Debit/Credit Card",
    "Credit Card",
    "credit card",
    "International Credit Card",
    "international credit card",
    "信用卡",
    "信用卡/银行卡",
    "信用卡 / 银行卡",
    "国际信用卡",
    "國際信用卡",
    "国际信用卡/金融卡",
    "國際信用卡/金融卡",
    "信用卡/VISA金融卡",
    "信用卡 / VISA金融卡",
    "信用卡／VISA金融卡",
    "VISA金融卡",
    "Kartu Kredit/Debit",
    "Kartu Kredit / Debit",
    "Kartu Debit/Kredit",
    "Kartu Debit / Kredit",
    "Kartu Kredit",
    "Kartu Debit",
    "บัตรเครดิต/เดบิต",
    "บัตรเครดิต / เดบิต",
    "บัตรเครดิต",
    "บัตรเดบิต",
    "Thẻ tín dụng/ghi nợ",
    "Thẻ tín dụng / ghi nợ",
    "Thẻ Tín dụng/Ghi nợ",
    "Thẻ tín dụng",
    "Thẻ ghi nợ",
    "Kad Kredit/Debit",
    "Kad Kredit / Debit",
    "Kad Debit/Kredit",
    "Kad Debit / Kredit",
    "Kad Kredit",
    "Kad Debit",
]
CREDIT_CARD_TEXT_MATCH = " or ".join(
    f"contains(normalize-space(.), '{label}')" for label in CREDIT_CARD_LABELS
)
CREDIT_CARD_OWN_TEXT_MATCH = " or ".join(
    f"text()[contains(normalize-space(.), '{label}')]" for label in CREDIT_CARD_LABELS
)
CREDIT_CARD_CLICKABLE_CLASS_MATCH = (
    "contains(normalize-space(@class), 'radio') or "
    "contains(normalize-space(@class), 'option') or "
    "contains(normalize-space(@class), 'method') or "
    "contains(normalize-space(@class), 'channel') or "
    "contains(normalize-space(@class), 'item') or "
    "contains(normalize-space(@class), 'payment')"
)
CREDIT_CARD_SELECTED = [
    f"//*[contains(concat(' ', normalize-space(@class), ' '), ' payment-channel-list-item ') and contains(concat(' ', normalize-space(@class), ' '), ' selected ')][.//*[{CREDIT_CARD_TEXT_MATCH}]]",
    f"//*[contains(concat(' ', normalize-space(@class), ' '), ' selected ') and ({CREDIT_CARD_TEXT_MATCH})]",
    f"//*[contains(concat(' ', normalize-space(@class), ' '), ' active ') and ({CREDIT_CARD_TEXT_MATCH})]",
    f"//*[contains(concat(' ', normalize-space(@class), ' '), ' checked ') and ({CREDIT_CARD_TEXT_MATCH})]",
    f"//*[@aria-selected='true' and ({CREDIT_CARD_TEXT_MATCH})]",
    f"//*[@aria-checked='true' and ({CREDIT_CARD_TEXT_MATCH})]",
]
CREDIT_CARD_CHANNEL = [
    f"//*[contains(concat(' ', normalize-space(@class), ' '), ' payment-channel-list-item ') and not(contains(concat(' ', normalize-space(@class), ' '), ' disabled '))][.//*[{CREDIT_CARD_TEXT_MATCH}]]",
    f"(//*[not(contains(concat(' ', normalize-space(@class), ' '), ' disabled ')) and (self::button or @role='button') and ({CREDIT_CARD_TEXT_MATCH})])[1]",
    f"(//*[not(contains(concat(' ', normalize-space(@class), ' '), ' disabled ')) and (self::label or @role='radio') and ({CREDIT_CARD_TEXT_MATCH})])[1]",
    f"(//*[not(contains(concat(' ', normalize-space(@class), ' '), ' disabled ')) and (contains(concat(' ', normalize-space(@class), ' '), ' button ') or contains(concat(' ', normalize-space(@class), ' '), ' item ') or contains(concat(' ', normalize-space(@class), ' '), ' method ') or contains(concat(' ', normalize-space(@class), ' '), ' channel ')) and ({CREDIT_CARD_TEXT_MATCH})])[1]",
    f"(//*[not(contains(concat(' ', normalize-space(@class), ' '), ' disabled ')) and (contains(concat(' ', normalize-space(@class), ' '), ' button ') or contains(concat(' ', normalize-space(@class), ' '), ' item ') or contains(concat(' ', normalize-space(@class), ' '), ' method ') or contains(concat(' ', normalize-space(@class), ' '), ' channel '))][.//*[{CREDIT_CARD_TEXT_MATCH}]])[1]",
    f"(//*[not(self::script or self::style) and ({CREDIT_CARD_OWN_TEXT_MATCH})]/ancestor-or-self::*[self::label or self::button or @role='radio' or @role='button' or {CREDIT_CARD_CLICKABLE_CLASS_MATCH}][1])[1]",
    f"(//*[not(self::script or self::style) and ({CREDIT_CARD_OWN_TEXT_MATCH})])[1]",
]
"""选中"""
BANK_NUMBER = [
    "(//label[contains(concat(' ', normalize-space(@class), ' '), ' eds-radio ') and .//*[contains(normalize-space(.), 'PAYONEER EUROPE')]])[1]",
    "(//*[contains(normalize-space(.), 'PAYONEER EUROPE')]/ancestor-or-self::*[self::label or @role='radio' or contains(normalize-space(@class), 'radio') or contains(normalize-space(@class), 'option') or contains(normalize-space(@class), 'card') or contains(normalize-space(@class), 'bank')][1])[1]",
    "(//*[contains(normalize-space(.), 'PAYONEER EUROPE')])[1]",
]

SUCCESS_XPATH = "//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'fail') or contains(text(), '失败') or contains(text(), 'unsuccessful') or contains(text(), 'error') or contains(text(), 'cancelled')]"
STATUS_XPATH = "//div[@id='payment-fe-popup-title']"
FAIL_PATH = "//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'success') or contains(text(), '成功') or contains(text(), 'completed')]"
SECURITY_MODAL_CSS = "#Cardinal-Modal, #Cardinal-ElementContainer"
SECURITY_IFRAME_CSS = "#Cardinal-CCA-IFrame"
SECURITY_INPUT_CSS = "input[type='tel'], input[type='text'], input[type='password'], input:not([type])"
PAYONEER_DYNAMIC_CODE_INPUT_ID = "inputID"
PAYONEER_PERSONAL_CODE_INPUT_ID = "sqInputID"
PAYONEER_CONFIRM_BUTTON_ID = "sendOtpsq"

DATE_INPUT = '//div[contains(@class, "eds-date-picker__input")]/div[contains(@class, "eds-selector")][contains(@class, "eds-selector--normal")]'

PAYMENT_SUCCESS_XPATH = "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'payment successful') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'completed') or contains(normalize-space(.), '支付成功') or contains(normalize-space(.), '成功')]"
PAYMENT_OK_BUTTON_XPATH = "//button[normalize-space()='好' or normalize-space()='OK' or normalize-space()='Ok' or .//*[normalize-space()='好' or normalize-space()='OK' or normalize-space()='Ok']]"

def _is_security_verify_enabled():
    return bool(security_verify_config.get("enabled", True))


def _security_task_id(store_name, code_type="dynamic"):
    clean_name = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", str(store_name)).strip("_")
    clean_name = clean_name[:60] or "shop"
    return f"shopee_ads_recharge_{code_type}_{clean_name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"


def _format_security_system_name(template, store_name, recharge_amount, code_name):
    return str(template).format(
        shop_name=store_name,
        recharge_amount=recharge_amount,
        code_name=code_name,
    )


def _has_security_verify_modal(driver):
    old_wait = driver.timeouts.implicit_wait
    driver.implicitly_wait(0)
    try:
        modal_elements = driver.find_elements(By.CSS_SELECTOR, SECURITY_MODAL_CSS)
        if any(item.is_displayed() for item in modal_elements):
            return True
        iframe_elements = driver.find_elements(By.CSS_SELECTOR, SECURITY_IFRAME_CSS)
        return any(item.is_displayed() for item in iframe_elements)
    finally:
        driver.implicitly_wait(old_wait)


def _find_element_in_current_frame_tree(driver, by, value, max_depth=4):
    elements = driver.find_elements(by, value)
    for element in elements:
        if element.is_displayed() and element.is_enabled():
            return element

    if max_depth <= 0:
        return None

    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for frame in frames:
        driver.switch_to.frame(frame)
        found = _find_element_in_current_frame_tree(driver, by, value, max_depth=max_depth - 1)
        if found:
            return found
        driver.switch_to.parent_frame()
    return None


def _find_payoneer_element(driver, by, value, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        driver.switch_to.default_content()
        cardinal_frames = driver.find_elements(By.CSS_SELECTOR, SECURITY_IFRAME_CSS)
        for cardinal_frame in cardinal_frames:
            driver.switch_to.frame(cardinal_frame)
            found = _find_element_in_current_frame_tree(driver, by, value)
            if found:
                return found
            driver.switch_to.default_content()

        found = _find_element_in_current_frame_tree(driver, by, value)
        if found:
            return found
        time.sleep(0.5)
    raise SmsOtpTaskError(f"Payoneer security element not found: {by}={value}")


def _fill_payoneer_security_codes(driver, dynamic_code, personal_code=None, timeout=60):
    try:
        dynamic_input = _find_payoneer_element(driver, By.ID, PAYONEER_DYNAMIC_CODE_INPUT_ID, timeout=timeout)
        dynamic_input.clear()
        dynamic_input.send_keys(str(dynamic_code).strip())

        if personal_code is not None:
            personal_input = _find_payoneer_element(driver, By.ID, PAYONEER_PERSONAL_CODE_INPUT_ID, timeout=timeout)
            personal_input.clear()
            personal_input.send_keys(str(personal_code).strip())

        confirm_button = _find_payoneer_element(driver, By.ID, PAYONEER_CONFIRM_BUTTON_ID, timeout=timeout)
        confirm_button.click()
    finally:
        driver.switch_to.default_content()


def _create_security_code_task(store_name, recharge_amount, payment_total_content, code_type, code_name, system_name):
    base_url = security_verify_config.get("base_url", "").strip()
    phone = str(security_verify_config.get("phone", "")).strip()
    user_id = str(security_verify_config.get("user_id", "")).strip()
    if not base_url or not phone or not user_id:
        raise SmsOtpTaskError(f"{store_name}: security_verify 配置缺少 base_url/phone/user_id")

    task_id = _security_task_id(store_name, code_type)
    requester = security_verify_config.get("requester", security_verify_config.get("user_name", "RPA"))
    metadata = {
        "user_name": security_verify_config.get("user_name", requester),
        "shop_name": store_name,
        "recharge_amount": recharge_amount,
        "payment_total": payment_total_content,
        "code_type": code_type,
        "code_name": code_name,
    }

    log.info(f"{store_name}: 创建{code_name}钉钉任务 {task_id}")
    create_sms_otp_task(
        base_url=base_url,
        task_id=task_id,
        system_name=system_name,
        phone=phone,
        user_id=user_id,
        requester=requester,
        metadata=metadata,
    )
    return base_url, task_id


def _wait_security_code(base_url, task_id):
    timeout = int(security_verify_config.get("timeout", 300))
    poll_interval = int(security_verify_config.get("poll_interval", 5))
    return wait_for_sms_otp_code(base_url, task_id, timeout=timeout, poll_interval=poll_interval)


def _mark_sms_sent_after_user_ready(base_url, task_id):
    timeout = int(security_verify_config.get("confirm_timeout", security_verify_config.get("timeout", 300)))
    poll_interval = int(security_verify_config.get("poll_interval", 5))
    deadline = time.time() + timeout
    last_status = ""

    while time.time() < deadline:
        data = get_sms_otp_task(base_url, task_id)
        task = data.get("task") or {}
        last_status = task.get("status", "")
        if last_status == "waiting_code":
            return
        if last_status == "ready_to_send":
            mark_sms_sent(base_url, task_id)
            return
        if last_status in {"cancelled", "expired", "failed", "finished"}:
            raise SmsOtpTaskError(f"SMS OTP task ended before code card, status={last_status}: {task}")
        time.sleep(poll_interval)

    raise SmsOtpTaskError(f"SMS OTP task confirm timeout, last_status={last_status}, task_id={task_id}")


def _request_security_code(store_name, recharge_amount, payment_total_content, code_type, code_name, system_name):
    base_url, task_id = _create_security_code_task(
        store_name,
        recharge_amount,
        payment_total_content,
        code_type,
        code_name,
        system_name,
    )
    return _wait_security_code(base_url, task_id), task_id


def _fill_security_code(driver, otp_code, timeout=60):
    _fill_payoneer_security_codes(driver, otp_code, timeout=timeout)


def _handle_security_verify_legacy(webdriver_util, store_name, recharge_amount, payment_total_content):
    if not _is_security_verify_enabled():
        return True, ""

    driver = webdriver_util.driver
    if not _has_security_verify_modal(driver):
        return True, ""

    try:
        completed_tasks = []
        if security_verify_config.get("personal_code_enabled", True):
            personal_template = security_verify_config.get(
                "personal_code_system_name",
                "{shop_name}充值金额：{recharge_amount}，个人安全码接收",
            )
            personal_system_name = _format_security_system_name(
                personal_template,
                store_name,
                recharge_amount,
                "个人安全码",
            )
            personal_code, personal_task_id = _request_security_code(
                store_name,
                recharge_amount,
                payment_total_content,
                "personal",
                "个人安全码",
                personal_system_name,
            )
            log.info(f"{store_name}: 已获取个人安全码，准备回填")
            _fill_security_code(driver, personal_code)
            completed_tasks.append(personal_task_id)
            time.sleep(5)

        dynamic_template = security_verify_config.get(
            "dynamic_code_system_name",
            security_verify_config.get("system_name", "{shop_name}充值金额：{recharge_amount}，动态验证码接收"),
        )
        dynamic_system_name = _format_security_system_name(
            dynamic_template,
            store_name,
            recharge_amount,
            "动态验证码",
        )
        dynamic_code, dynamic_task_id = _request_security_code(
            store_name,
            recharge_amount,
            payment_total_content,
            "dynamic",
            "动态验证码",
            dynamic_system_name,
        )
        log.info(f"{store_name}: 已获取动态验证码，准备回填")
        _fill_security_code(driver, dynamic_code)
        completed_tasks.append(dynamic_task_id)
        time.sleep(20)
        return True, f"已完成安全验证任务：{','.join(completed_tasks)}"
    except Exception as e:
        return False, f"{store_name}: 安全验证处理失败：{e}"


def _handle_security_verify(webdriver_util, store_name, recharge_amount, payment_total_content):
    if not _is_security_verify_enabled():
        return True, ""

    driver = webdriver_util.driver
    if not _has_security_verify_modal(driver):
        return True, ""

    try:
        completed_tasks = []
        personal_task = None
        if security_verify_config.get("personal_code_enabled", True):
            personal_template = security_verify_config.get(
                "personal_code_system_name",
                "{shop_name}充值金额：{recharge_amount}，个人安全码接收",
            )
            personal_system_name = _format_security_system_name(
                personal_template,
                store_name,
                recharge_amount,
                "个人安全码",
            )
            personal_base_url, personal_task_id = _create_security_code_task(
                store_name,
                recharge_amount,
                payment_total_content,
                "personal",
                "个人安全码",
                personal_system_name,
            )
            personal_task = (personal_base_url, personal_task_id)
            completed_tasks.append(personal_task_id)

        dynamic_template = security_verify_config.get(
            "dynamic_code_system_name",
            security_verify_config.get("system_name", "{shop_name}充值金额：{recharge_amount}，动态验证码接收"),
        )
        dynamic_system_name = _format_security_system_name(
            dynamic_template,
            store_name,
            recharge_amount,
            "动态验证码",
        )
        dynamic_base_url, dynamic_task_id = _create_security_code_task(
            store_name,
            recharge_amount,
            payment_total_content,
            "dynamic",
            "动态验证码",
            dynamic_system_name,
        )
        completed_tasks.append(dynamic_task_id)
        log.info(f"{store_name}: 已创建安全验证任务，等待用户提交：{','.join(completed_tasks)}")

        if security_verify_config.get("auto_mark_sent", True):
            if personal_task:
                _mark_sms_sent_after_user_ready(personal_task[0], personal_task[1])
            _mark_sms_sent_after_user_ready(dynamic_base_url, dynamic_task_id)

        dynamic_code = _wait_security_code(dynamic_base_url, dynamic_task_id)
        personal_code = None
        if personal_task:
            personal_code = _wait_security_code(personal_task[0], personal_task[1])

        log.info(f"{store_name}: 已获取安全验证信息，准备回填 Payoneer 表单")
        _fill_payoneer_security_codes(driver, dynamic_code, personal_code)
        time.sleep(20)
        return True, f"已完成安全验证任务：{','.join(completed_tasks)}"
    except Exception as e:
        return False, f"{store_name}: 安全验证处理失败：{e}"


def close_alert(webdriver_util: WebdriverUtil):
    """关闭当前可见的 Shopee 弹框，包括远程 unified-popup 活动弹层。"""
    driver = webdriver_util.driver
    try:
        old_wait = driver.timeouts.implicit_wait
    except Exception:
        old_wait = 60

    closed = False
    try:
        driver.implicitly_wait(0)
        try:
            candidates = driver.find_elements(By.XPATH, CLOSE_ALERT)
        except Exception:
            candidates = []

        for close_btn in candidates:
            try:
                if not close_btn.is_displayed():
                    continue
                try:
                    driver.execute_script("arguments[0].click();", close_btn)
                except WebDriverException:
                    close_btn.click()
                closed = True
                log.info("已关闭 Shopee 可见弹框")
                time.sleep(0.2)
            except (WebDriverException, AttributeError):
                continue

        # 首页的远程活动弹层由 HomePageModal/unified-popup 下发，
        # 类名不固定；用语义特征和活动图片右上角的位置作为兜底。
        try:
            result = driver.execute_script(_DISMISS_VISIBLE_PROMO_SCRIPT) or {}
        except WebDriverException:
            result = {}
        if result.get("clicked"):
            closed = True
            log.info(f"已关闭 Shopee 远程活动弹框：{result.get('reason') or 'unknown'}")
            time.sleep(0.2)
        elif result.get("overlayVisible"):
            try:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                closed = True
                log.info("已尝试使用 ESC 关闭 Shopee 活动弹框")
                time.sleep(0.2)
            except WebDriverException:
                pass
        return closed
    finally:
        try:
            driver.implicitly_wait(old_wait)
        except Exception:
            pass

def _find_visible_elements(driver, xpath):
    old_wait = driver.timeouts.implicit_wait
    driver.implicitly_wait(0)
    try:
        return [item for item in driver.find_elements(By.XPATH, xpath) if item.is_displayed()]
    except Exception:
        return []
    finally:
        driver.implicitly_wait(old_wait)


def _click_payment_ok_if_present(driver):
    candidate_xpaths = [
        PAYMENT_OK_BUTTON_XPATH,
        "//button[contains(@class, 'shopee-button') and normalize-space()='OK']",
        "//*[self::button or @role='button'][normalize-space()='OK' or .//*[normalize-space()='OK']]",
    ]
    for xpath in candidate_xpaths:
        for button in _find_visible_elements(driver, xpath):
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
                driver.execute_script("arguments[0].click();", button)
                return True
            except WebDriverException:
                try:
                    button.click()
                    return True
                except WebDriverException:
                    continue
    return False


def _wait_payment_popup_closed(driver, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        popup_text = _get_payment_popup_text(driver)
        body_text = _get_visible_body_text(driver)
        if not _text_has_payment_success(popup_text or body_text):
            return True
        if _click_payment_ok_if_present(driver):
            time.sleep(0.8)
            continue
        time.sleep(0.5)
    return False


def _confirm_payment_success(driver, message="Payment Successful"):
    with _TemporaryRemoteTimeout(3):
        _click_payment_ok_if_present(driver)
        _wait_payment_popup_closed(driver, timeout=4)
    return True, message


def _get_payment_popup_text(driver):
    status_elements = _find_visible_elements(driver, STATUS_XPATH)
    if status_elements:
        return status_elements[0].text.strip()
    return ""


def _get_visible_body_text(driver):
    old_wait = driver.timeouts.implicit_wait
    driver.implicitly_wait(0)
    try:
        script_text = driver.execute_script("return document.body ? document.body.innerText : '';")
        if script_text:
            return str(script_text).strip()
        body_elements = driver.find_elements(By.TAG_NAME, "body")
        if body_elements:
            return body_elements[0].text.strip()
    except Exception:
        return ""
    finally:
        driver.implicitly_wait(old_wait)
    return ""


def _find_visible_text_by_xpaths(driver, xpaths):
    for xpath in xpaths:
        for element in _find_visible_elements(driver, xpath):
            text = element.text.strip()
            if text:
                return text
    return ""


def _wait_for_visible_text_by_xpaths(driver, xpaths, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = _find_visible_text_by_xpaths(driver, xpaths)
        if text:
            return text
        time.sleep(0.5)
    return ""


def _wait_for_visible_element_by_xpaths(driver, xpaths, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for xpath in xpaths:
            elements = _find_visible_elements(driver, xpath)
            if elements:
                return elements[0]
        time.sleep(0.5)
    return None


def _parse_ads_credit_amount(text):
    try:
        return parse_shopee_money(text, SITE_CODE)
    except ShopeeMoneyParseError:
        return None


def _find_ads_credit_candidate(driver, xpaths):
    last_text = ""
    for xpath in xpaths:
        for element in _find_visible_elements(driver, xpath):
            text = (element.text or "").strip()
            if not text:
                continue
            last_text = text
            amount = _parse_ads_credit_amount(text)
            if amount is None:
                continue
            return element, amount, text
    return None, None, last_text


def _wait_for_amount_element_by_xpaths(driver, xpaths, timeout=30, allow_zero=True):
    deadline = time.time() + timeout
    last_text = ""
    last_candidate = None
    stable_reads = 0
    while time.time() < deadline:
        element, amount, text = _find_ads_credit_candidate(driver, xpaths)
        if text:
            last_text = text
        if element is None:
            last_candidate = None
            stable_reads = 0
        else:
            candidate = (amount, text)
            if candidate == last_candidate:
                stable_reads += 1
            else:
                last_candidate = candidate
                stable_reads = 1
            if stable_reads >= 2 and (amount > 0 or allow_zero):
                return element, amount, text
        time.sleep(0.5)
    return None, 0, last_text


def _is_radio_label_selected(driver, label_element):
    try:
        return bool(
            driver.execute_script(
                """
                const label = arguments[0];
                const input = label.querySelector('input[type="radio"]');
                return !!(input && input.checked);
                """,
                label_element,
            )
        )
    except WebDriverException:
        try:
            radio_inputs = label_element.find_elements(By.XPATH, ".//input[@type='radio']")
            return any(item.is_selected() for item in radio_inputs)
        except Exception:
            return False


def _has_radio_input(label_element):
    try:
        return bool(label_element.find_elements(By.XPATH, ".//input[@type='radio']"))
    except Exception:
        return False


def _ensure_credit_card_payment_selected(webdriver_util, store_name):
    driver = webdriver_util.driver
    if _wait_for_visible_element_by_xpaths(driver, CREDIT_CARD_SELECTED, timeout=2):
        log.info(f"{store_name}: 已选中 Credit / Debit Card/信用卡支付方式")
        return True, ""

    if _wait_for_visible_element_by_xpaths(driver, BANK_NUMBER, timeout=2):
        log.info(f"{store_name}: 已显示 PAYONEER 卡，视为已选中信用卡支付方式")
        return True, ""

    card_channel = _wait_for_visible_element_by_xpaths(driver, CREDIT_CARD_CHANNEL, timeout=10)
    if card_channel is None:
        page_context = _short_page_context(driver)
        return False, f"{store_name}:付款确认页未找到可用的 Credit / Debit Card/信用卡/银行卡 支付方式，已停止提交支付。{page_context}"

    try:
        log.info(f"{store_name}: 当前默认支付方式不是 Credit / Debit Card/信用卡，切换到信用卡支付")
        webdriver_util.move_to_click(card_channel)
    except WebDriverException as exc:
        return False, f"{store_name}:点击 Credit / Debit Card/信用卡 支付方式失败，已停止提交支付：{exc}"

    if _wait_for_visible_element_by_xpaths(driver, CREDIT_CARD_SELECTED, timeout=10):
        return True, ""
    if _wait_for_visible_element_by_xpaths(driver, BANK_NUMBER, timeout=10):
        return True, ""

    page_context = _short_page_context(driver)
    return False, f"{store_name}:点击后未检测到 Credit / Debit Card/信用卡 被选中，已停止提交支付。{page_context}"


def _select_first_payoneer_card(webdriver_util, store_name):
    driver = webdriver_util.driver
    card_element = _wait_for_visible_element_by_xpaths(driver, BANK_NUMBER, timeout=15)
    if card_element is None:
        page_context = _short_page_context(driver)
        return False, f"{store_name}:Credit / Debit Card 下未找到 PAYONEER EUROPE 卡，已停止提交支付。{page_context}"

    card_text = re.sub(r"\s+", " ", card_element.text or "").strip() or "PAYONEER EUROPE"
    if _is_radio_label_selected(driver, card_element):
        log.info(f"{store_name}: 已选中银行卡：{card_text}")
        return True, ""

    try:
        log.info(f"{store_name}: 选择第一张 PAYONEER EUROPE 卡：{card_text}")
        webdriver_util.move_to_click(card_element)
    except WebDriverException as exc:
        return False, f"{store_name}:点击 PAYONEER EUROPE 卡失败，已停止提交支付：{exc}"

    deadline = time.time() + 5
    while time.time() < deadline:
        refreshed_card = _wait_for_visible_element_by_xpaths(driver, BANK_NUMBER, timeout=1)
        if refreshed_card is not None and _is_radio_label_selected(driver, refreshed_card):
            return True, ""
        if refreshed_card is not None and not _has_radio_input(refreshed_card):
            log.info(f"{store_name}: PAYONEER 卡已点击且保持可见，页面未暴露标准 radio checked 状态")
            return True, ""
        time.sleep(0.3)

    page_context = _short_page_context(driver)
    return False, f"{store_name}:点击后未检测到 PAYONEER EUROPE 卡被选中，已停止提交支付。{page_context}"


def _get_optional_visible_text(driver, xpath, default=""):
    return _find_visible_text_by_xpaths(driver, [xpath]) or default


def _get_optional_currency_text(driver, xpath, default="-"):
    text = _get_optional_visible_text(driver, xpath, default="")
    return _clean_currency_text(text).strip() if text else default


def _short_page_context(driver, limit=800):
    try:
        url = _safe_current_url(driver)
    except Exception:
        url = ""
    text = _get_visible_body_text(driver)
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > limit:
        text = text[:limit] + "..."
    return f"url={url}; text={text}"


def _wait_for_ads_credit(webdriver_util, store_name, timeout=30):
    close_alert(webdriver_util)
    element, amount, text = _wait_for_amount_element_by_xpaths(
        webdriver_util.driver,
        ADS_CREDIT_FALLBACKS,
        timeout=timeout,
        allow_zero=True,
    )
    if element is None:
        page_context = _short_page_context(webdriver_util.driver)
        candidate_note = f"最后候选文本={text!r}。" if text else ""
        return None, 0, f"{store_name}:未可靠识别 Ads Credit 广告余额，已停止充值。{candidate_note}{page_context}"

    retry_count = 0
    while amount <= 0 and retry_count < 10:
        time.sleep(1)
        refreshed_element, refreshed_amount, refreshed_text = _wait_for_amount_element_by_xpaths(
            webdriver_util.driver,
            ADS_CREDIT_FALLBACKS,
            timeout=2,
            allow_zero=True,
        )
        if refreshed_element is not None:
            element = refreshed_element
            amount = refreshed_amount
            text = refreshed_text
        retry_count += 1

    log.info(f"{store_name}: Ads Credit 广告余额：{text}")
    return element, amount, ""


def _safe_current_url(driver):
    try:
        with _TemporaryRemoteTimeout(3):
            return driver.current_url
    except Exception as exc:
        log.warning(f"读取当前页面 URL 失败：{exc}")
        return ""


_REMOTE_TIMEOUT_LOCK = threading.Lock()
_REMOTE_TIMEOUT_RESTORE_VALUE = None
_REMOTE_TIMEOUT_USE_COUNT = 0


class _TemporaryRemoteTimeout:
    def __init__(self, timeout):
        self.timeout = timeout

    def __enter__(self):
        global _REMOTE_TIMEOUT_RESTORE_VALUE, _REMOTE_TIMEOUT_USE_COUNT
        with _REMOTE_TIMEOUT_LOCK:
            if _REMOTE_TIMEOUT_USE_COUNT == 0:
                try:
                    _REMOTE_TIMEOUT_RESTORE_VALUE = RemoteConnection.get_timeout()
                except Exception:
                    _REMOTE_TIMEOUT_RESTORE_VALUE = None
                try:
                    RemoteConnection.set_timeout(self.timeout)
                except Exception as exc:
                    log.warning(f"设置 Selenium 命令短超时失败：{exc}")
            _REMOTE_TIMEOUT_USE_COUNT += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        global _REMOTE_TIMEOUT_RESTORE_VALUE, _REMOTE_TIMEOUT_USE_COUNT
        with _REMOTE_TIMEOUT_LOCK:
            _REMOTE_TIMEOUT_USE_COUNT = max(_REMOTE_TIMEOUT_USE_COUNT - 1, 0)
            if _REMOTE_TIMEOUT_USE_COUNT == 0 and _REMOTE_TIMEOUT_RESTORE_VALUE is not None:
                try:
                    RemoteConnection.set_timeout(_REMOTE_TIMEOUT_RESTORE_VALUE)
                except Exception as restore_exc:
                    log.warning(f"恢复 Selenium 命令超时失败：{restore_exc}")
                _REMOTE_TIMEOUT_RESTORE_VALUE = None


def _text_has_payment_success(text):
    text = (text or "").lower()
    return "payment successful" in text or "支付成功" in text or "completed" in text


def _text_has_payment_fail(text):
    text = (text or "").lower()
    if _text_has_payment_success(text):
        return False
    fail_words = ["payment unsuccessful", "payment failed", "unsuccessful", "cancelled", "支付失败", "失败"]
    return any(word in text for word in fail_words)


def _text_has_payment_processing(text):
    text = (text or "").lower()
    processing_words = [
        "payment processing",
        "unable to get your payment confirmation",
        "check back on the payment status",
        "next 10 minutes",
        "payment status",
        "支付处理中",
        "待确认",
    ]
    return any(word in text for word in processing_words)


def _wait_for_payment_result(driver, timeout=180):
    deadline = time.time() + timeout
    last_url = ""
    last_error = ""
    last_text = ""
    while time.time() < deadline:
        try:
            handles = list(driver.window_handles)
        except Exception as e:
            last_error = str(e)
            if isinstance(e, (InvalidSessionIdException, NoSuchWindowException)) or any(
                marker in last_error.lower()
                for marker in ("invalid session id", "no such window", "disconnected")
            ):
                log.warning(f"支付后浏览器会话已失效，立即转入重新开店余额复查：{e}")
                return "processing", f"支付已提交，浏览器会话已失效，等待重新开店余额复查：{e}"
            log.warning(f"支付结果检测读取窗口失败，继续等待：{e}")
            time.sleep(1)
            continue

        for handle in reversed(handles):
            try:
                driver.switch_to.window(handle)
                current_url = _safe_current_url(driver)
                last_url = current_url

                popup_text = _get_payment_popup_text(driver)
                body_text = _get_visible_body_text(driver)
                result_text = popup_text or body_text
                if result_text:
                    last_text = result_text

                success_elements = _find_visible_elements(driver, PAYMENT_SUCCESS_XPATH)
                if success_elements:
                    return _confirm_payment_success(
                        driver,
                        success_elements[0].text.strip() or "Payment Successful",
                    )

                if _text_has_payment_success(result_text):
                    return _confirm_payment_success(driver, result_text or "Payment Successful")

                if _text_has_payment_fail(result_text):
                    return False, f"支付失败：{result_text}"

                # Payment Processing is not treated as a final result. Keep waiting
                # until the user configured payment wait time is exhausted.
            except WebDriverException as exc:
                last_error = str(exc)
                if isinstance(exc, (InvalidSessionIdException, NoSuchWindowException)) or any(
                    marker in last_error.lower()
                    for marker in ("invalid session id", "no such window", "disconnected")
                ):
                    log.warning(f"支付后浏览器会话已失效，立即转入重新开店余额复查：{exc}")
                    return "processing", f"支付已提交，浏览器会话已失效，等待重新开店余额复查：{exc}"
                continue
            except Exception as exc:
                last_error = str(exc)
                log.warning(f"读取支付结果时 WebDriver 异常：{exc}")
                continue

        time.sleep(2)

    details = []
    if last_url:
        details.append(f"最后 URL: {last_url}")
    if last_error:
        details.append(f"最后错误: {last_error}")
    if last_text:
        short_text = re.sub(r"\s+", " ", last_text).strip()
        if len(short_text) > 300:
            short_text = short_text[:300] + "..."
        details.append(f"最后页面文本: {short_text}")
    suffix = "；" + "；".join(details) if details else ""
    return False, f"未在 {timeout} 秒内出现支付成功弹窗，按支付失败处理{suffix}"


import math
"""充值操作"""
def recharge_ads(webdriver_util:WebdriverUtil,store_name,recharge_amount):
    try:
        recharge_amount = int(parse_shopee_money(recharge_amount, SITE_CODE))
    except ShopeeMoneyParseError:
        return False, f"{store_name}: 充值金额格式无效，停止充值。"
    if recharge_amount <= 0:
        return False, f"{store_name}: 充值金额必须大于零。"
    """点击top_up"""
    close_alert(webdriver_util)
    top_up_btn_element = _wait_for_visible_element_by_xpaths(webdriver_util.driver, TOP_UP_BUTTON_FALLBACKS, timeout=30)
    if top_up_btn_element is None:
        page_context = _short_page_context(webdriver_util.driver)
        return False, f"{store_name}:未找到广告充值 Top Up 入口，可能页面未进入 Ads 钱包页、店铺无广告余额入口或页面结构变化。{page_context}"
    webdriver_util.move_to_click(top_up_btn_element)
    if is_recharge_amount_below_minimum(recharge_amount, SITE_CODE):
        return False, format_minimum_recharge_skip_message(SITE_CODE, store_name, recharge_amount)
    """点击 Enter the Top-up Amount"""
    enter_the_top_up = _wait_for_visible_element_by_xpaths(webdriver_util.driver, ENTER_THE_TOP_UP_AMOUNT_FALLBACKS, timeout=20)
    if enter_the_top_up is None:
        page_context = _short_page_context(webdriver_util.driver)
        return False, f"{store_name}:未找到自定义充值金额入口。{page_context}"
    webdriver_util.move_to_click(enter_the_top_up)
    time.sleep(1)
    """输入金额 键盘操作"""
    recharge_input = _wait_for_visible_element_by_xpaths(webdriver_util.driver, RECHARGE_INPUT_FALLBACKS, timeout=15)
    if recharge_input is None:
        page_context = _short_page_context(webdriver_util.driver)
        return False, f"{store_name}:未找到充值金额输入框。{page_context}"
    ctrl_cmd = Keys.COMMAND if sys.platform == 'darwin' else Keys.CONTROL
    recharge_input.send_keys(ctrl_cmd, "a")  # 全选
    recharge_input.send_keys(Keys.BACKSPACE)  # 删除
    webdriver_util.move_to_send_key(recharge_input,str(recharge_amount))
    time.sleep(2)
    """点击 Enter the Top-up Amount"""
    enter_the_top_up = _wait_for_visible_element_by_xpaths(webdriver_util.driver, ENTER_THE_TOP_UP_AMOUNT_FALLBACKS, timeout=15)
    if enter_the_top_up is None:
        page_context = _short_page_context(webdriver_util.driver)
        return False, f"{store_name}:输入金额后未找到确认金额按钮。{page_context}"
    webdriver_util.move_to_click(enter_the_top_up)
    time.sleep(2)
    """获取校验金额"""
    verify_amount_content =webdriver_util.find_element(By.XPATH, VERIFY_AMOUNT).text
    try:
        verify_amount = parse_shopee_money(verify_amount_content, SITE_CODE)
    except ShopeeMoneyParseError:
        return False, f"{store_name}: 付款确认金额格式无效，停止充值。"
    verify_amount = int(verify_amount)
    """校验金额是否相等"""
    if verify_amount != recharge_amount:
        return False, f"{store_name}:充值广告费用一次校验异常。"
    submit_btn = webdriver_util.find_element(By.XPATH, RECHARGE_SUBMIT)
    webdriver_util.move_to_click(submit_btn)
    time.sleep(2)
    """等待提交 """
    verify_amount_content2 = _wait_for_visible_text_by_xpaths(webdriver_util.driver, VERIFY_AMOUNT2_FALLBACKS, timeout=25)
    if not verify_amount_content2:
        page_context = _short_page_context(webdriver_util.driver)
        return False, f"{store_name}:未检测到付款确认页金额，可能确认页未加载或页面结构变化。{page_context}"
    vat_content = _get_optional_currency_text(webdriver_util.driver, VAT, default="-")
    payment_total_content = _wait_for_visible_text_by_xpaths(webdriver_util.driver, PAYMENT_TOTAL_FALLBACKS, timeout=10)
    if not payment_total_content:
        payment_total_content = "-"
    else:
        payment_total_content = _clean_currency_text(payment_total_content).strip()
    try:
        verify_amount2 = parse_shopee_money(verify_amount_content2, SITE_CODE)
    except ShopeeMoneyParseError:
        return False, f"{store_name}: 付款确认金额格式无效，停止充值。"
    """校验金额是否相等2"""
    if verify_amount2 != recharge_amount:
        return False,f"{store_name}:充值广告费用二次校验异常。"

    payment_method_ok, payment_method_msg = _ensure_credit_card_payment_selected(webdriver_util, store_name)
    if not payment_method_ok:
        return False, payment_method_msg

    card_ok, card_msg = _select_first_payoneer_card(webdriver_util, store_name)
    if not card_ok:
        return False, card_msg

    print('开始提交')
    submit_btn = webdriver_util.find_element(By.XPATH, CONFIRM_BTN)
    webdriver_util.move_to_click(submit_btn)
    # time.sleep(60)
    # security_ok, security_msg = _handle_security_verify(webdriver_util, store_name, recharge_amount, payment_total_content)
    # if security_msg:
    #     log.info(security_msg)
    #     time.sleep(15)
    # if not security_ok:
    #     return False, security_msg
    payment_timeout = max(int(PAYMENT_RESULT_TIMEOUT_SECONDS or 60), 1)
    log.info(f"{store_name}: 等待支付结果，最长 {payment_timeout} 秒")
    with _TemporaryRemoteTimeout(8):
        payment_ok, payment_msg = _wait_for_payment_result(webdriver_util.driver, timeout=payment_timeout)
        try:
            py_list = webdriver_util.find_elements(By.XPATH,PAYMENT_OK_BUTTON_XPATH)
        except Exception as exc:
            log.warning(f"{store_name}: 支付结果后检查 OK 按钮异常：{exc}")
            py_list = []
    if payment_ok == "processing":
        return "processing", f"支付处理中！【{store_name}】:充值金额：{verify_amount2}，支付总额：{payment_total_content}。请 10 分钟后到 Shopee 支付状态确认，不要立即重复支付。更新时间：{str(datetime.today().strftime('%Y-%m-%d'))}"
    if not payment_ok:
        if len(py_list) > 0:
            return True, f"支付成功！【{store_name}】:充值金额：{verify_amount2}，支付总额：{payment_total_content}。更新时间：{str(datetime.today().strftime('%Y-%m-%d'))}"
        return False, f"支付失败！【{store_name}】:充值金额：{verify_amount2}，支付总额：{payment_total_content}。更新时间：{str(datetime.today().strftime('%Y-%m-%d'))}"

    return True, f"支付成功！【{store_name}】:脚本充值金额：{verify_amount2},VAT:{vat_content},支付总额：{payment_total_content}。更新时间：{str(datetime.today().strftime('%Y-%m-%d'))}"






def vn_shopee_ads_recharge(driver, store_name: str, output_path: str = ""):
    log.info(f"正在处理店铺: {store_name}")
    webdriver_util = WebdriverUtil(driver)

    if not _ensure_login(webdriver_util, store_name):
        _write_error(store_name, f"{store_name}: Shopee 登录异常")
        return
    # 首页活动弹层会盖住页面并让运营人员误以为脚本停住，跳转前先清理。
    close_alert(webdriver_util)
    if '仓发' not in store_name:
        # 跳转到 Shopee Ads
        if ads_wallet_url:
            webdriver_util.go_to(ads_wallet_url)
            time.sleep(5)
        # 等待 Ads Credit 出现
        ads_credit_element, ads_credit, ads_credit_error = _wait_for_ads_credit(webdriver_util, store_name, timeout=30)
        if ads_credit_error:
            mark_ads_pending_review(
                store_name=store_name, site_code=SITE_CODE, dtd=dtd,
                doc_config=doc_config, output_directory=output_directory, log=log,
            )
            _write_error(store_name, ads_credit_error)
            return {"status": "failed", "message": ads_credit_error}

        try:
            is_recharges, recharge_amount, access_token, union_id, config_doc_id, config_sheet, row_count = prepare_verified_ads_budget(
                webdriver_util=webdriver_util, store_name=store_name, site_code=SITE_CODE,
                date_input_xpath=DATE_INPUT, amount_xpath=SALES_XPATH,
                read_balance=_wait_for_ads_credit, dtd=dtd, doc_config=doc_config,
                output_directory=output_directory, log=log,
            )
        except (AdsDataValidationError, OSError) as exc:
            message = str(exc) if isinstance(exc, AdsDataValidationError) else f"{store_name}: 采集证据无法保存，停止自动充值。"
            _write_error(store_name, message)
            return {"status": "failed", "message": message, "needs_review": True}
        if is_recharges:
            """开始执行充值操作"""
            if is_recharge_amount_below_minimum(recharge_amount, SITE_CODE):
                msg = format_minimum_recharge_skip_message(SITE_CODE, store_name, recharge_amount)
                msg = write_ads_result(dtd=dtd, access_token=access_token, union_id=union_id, doc_id=config_doc_id, sheet_name=config_sheet, row=row_count, store_name=store_name, site_code=SITE_CODE, message=msg, log=log)
                log.info(msg)
                return {"status": "skipped", "message": msg}
            is_recharge,msg = recharge_ads(webdriver_util, store_name, recharge_amount)
            msg = write_ads_result(dtd=dtd, access_token=access_token, union_id=union_id, doc_id=config_doc_id, sheet_name=config_sheet, row=row_count, store_name=store_name, site_code=SITE_CODE, message=msg, log=log)
            if is_recharge == "processing":
                log.info(msg)
                return {"status": "processing", "message": msg}
            if is_recharge:
                return {"status": "success", "message": msg}
            _write_error(store_name, msg)
            return {"status": "failed", "message": msg}

        else:
            message = f"{store_name}: 不需要充值"
            _write_error(store_name, message)
            return {"status": "skipped", "message": message}
    else:
        # 跳转到 Shopee Ads
        if ads_wallet_url:
            cangfa_ads_wallet_url = 'https://seller.shopee.cn/portal/marketing/pas/index'
            webdriver_util.go_to(cangfa_ads_wallet_url)
            time.sleep(5)
        # 等待 Ads Credit 出现
        ads_credit_element, ads_credit, ads_credit_error = _wait_for_ads_credit(webdriver_util, store_name, timeout=30)
        if ads_credit_error:
            mark_ads_pending_review(
                store_name=store_name, site_code=SITE_CODE, dtd=dtd,
                doc_config=doc_config, output_directory=output_directory, log=log,
            )
            _write_error(store_name, ads_credit_error)
            return {"status": "failed", "message": ads_credit_error}

        try:
            is_recharges, recharge_amount, access_token, union_id, config_doc_id, config_sheet, row_count = prepare_verified_ads_budget(
                webdriver_util=webdriver_util, store_name=store_name, site_code=SITE_CODE,
                date_input_xpath=DATE_INPUT, amount_xpath=SALES_XPATH,
                read_balance=_wait_for_ads_credit, dtd=dtd, doc_config=doc_config,
                output_directory=output_directory, log=log,
            )
        except (AdsDataValidationError, OSError) as exc:
            message = str(exc) if isinstance(exc, AdsDataValidationError) else f"{store_name}: 采集证据无法保存，停止自动充值。"
            _write_error(store_name, message)
            return {"status": "failed", "message": message, "needs_review": True}
        if is_recharges:
            """开始执行充值操作"""
            if is_recharge_amount_below_minimum(recharge_amount, SITE_CODE):
                msg = format_minimum_recharge_skip_message(SITE_CODE, store_name, recharge_amount)
                msg = write_ads_result(dtd=dtd, access_token=access_token, union_id=union_id, doc_id=config_doc_id, sheet_name=config_sheet, row=row_count, store_name=store_name, site_code=SITE_CODE, message=msg, log=log)
                log.info(msg)
                return {"status": "skipped", "message": msg}
            is_recharge,msg = recharge_ads(webdriver_util, store_name, recharge_amount)
            msg = write_ads_result(dtd=dtd, access_token=access_token, union_id=union_id, doc_id=config_doc_id, sheet_name=config_sheet, row=row_count, store_name=store_name, site_code=SITE_CODE, message=msg, log=log)
            if is_recharge == "processing":
                log.info(msg)
                return {"status": "processing", "message": msg}
            if is_recharge:
                return {"status": "success", "message": msg}
            _write_error(store_name, msg)
            return {"status": "failed", "message": msg}

        else:
            message = f"{store_name}: 不需要充值"
            _write_error(store_name, message)
            return {"status": "skipped", "message": message}
