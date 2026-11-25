import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from functools import lru_cache

import yaml
from selenium.common import WebDriverException
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
from util.yingdao_util import append_txt, create_directory, create_txt, read_txt,parse_amount
from util import dingtalk_doc_api as dtd
from implement.shopee.ads_expense_date_range import select_previous_7_days_expense_range
from implement.shopee.ads_recharge_rules import (
    format_minimum_recharge_skip_message,
    is_recharge_amount_below_minimum,
)

PROCESS_NAME = "Shopee马来广告充值"
CHANNEL = "Shopee"
SITE_CODE = "my"
DEFAULT_ADS_WALLET_URL = "https://seller.shopee.com.my/portal/marketing/pas/index"


def _clean_currency_text(value):
    text = str(value or "")
    for token in ("฿", "RM", "MYR", "Rp", "IDR", "₫", "VND", "PHP", "₱"):
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
        "ShopeeMalaysiaAdsRecharge",
    )
    if configured_path:
        probe_path = os.path.join(configured_path, CHANNEL, PROCESS_NAME)
        if _safe_makedirs(probe_path):
            return configured_path
    _safe_makedirs(fallback_path)
    return fallback_path


_default_output_path = os.path.join(
    os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
    "ShopeeMalaysiaAdsRecharge",
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
world_card_sms_config = pcfg.get("shopee", {}).get("world_card_sms", {})
payment_card_type = "payoneer"
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


def _remove_store_result(path, store_name):
    if not path or not os.path.exists(path):
        return
    try:
        content = read_txt(path)
        lines = str(content or "").splitlines()
        remaining = [line for line in lines if store_name not in line]
        if len(remaining) == len(lines):
            return
        with open(path, "w", encoding="utf-8") as file:
            if remaining:
                file.write("\n".join(remaining) + "\n")
    except OSError as exc:
        log.warning(f"{store_name}: 清理旧充值结果失败：{exc}")



def _write_error(store_name, message):
    log.info(message)
    with _write_lock:
        _remove_store_result(success_shop_file, store_name)
        if not os.path.exists(error_shop_file):
            create_txt(error_shop_file)
        error_txt = read_txt(error_shop_file)
        if store_name not in error_txt:
            append_txt(error_shop_file, message)

def _write_success(store_name, message):
    log.info(message)
    with _write_lock:
        _remove_store_result(error_shop_file, store_name)
        if not os.path.exists(success_shop_file):
            create_txt(success_shop_file)
        error_txt = read_txt(success_shop_file)
        if store_name not in error_txt:
            append_txt(success_shop_file, message)


def _ensure_login(webdriver_util, store_name):
    current_url = ""
    try:
        with _TemporaryRemoteTimeout(8):
            current_url = str(webdriver_util.driver.current_url or "")
    except Exception as exc:
        log.warning(f"{store_name}: 获取当前 Shopee URL 超时或失败，继续登录检测：{exc}")

    lower_url = current_url.lower()
    if "seller.shopee." in lower_url and not any(token in lower_url for token in ("signin", "/login", "account/signin")):
        log.info(f"{store_name}: 已在 Shopee Seller Centre，视为已登录：{current_url}")
        return True

    try:
        with _TemporaryRemoteTimeout(15):
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
    except Exception as exc:
        try:
            current_url = str(webdriver_util.driver.current_url or current_url)
        except Exception:
            pass
        log.warning(f"{store_name}: Shopee 登录检测超时或失败：{exc}，current_url={current_url}")
        pass
    return False

def ansy_dingtalk_doc(ads_credit,sales,shop_name):
    app_key = doc_config['app_key']
    app_secret = doc_config['app_secret']
    app_userid = doc_config['app_userid']
    config_doc_id = doc_config['config_doc_id']
    config_sheet = doc_config['config_sheet']
    access_token = dtd.get_access_token(app_key, app_secret)
    """计算店铺所在行"""
    union_id = dtd.get_union_id(access_token, app_userid)
    total_rows = dtd.get_sheet_row_count(access_token, union_id, config_doc_id, config_sheet)
    recharge_plan_data = dtd.safe_read_range(access_token, union_id, config_doc_id, config_sheet, f"A2:A{total_rows}")
    row_count = -1
    for i, row in enumerate(recharge_plan_data):
        doc_shop_name = row[0].strip()
        if doc_shop_name in shop_name:
            row_count = i + 2
            break
    """写入广告余额"""
    dtd.safe_update_range(access_token, union_id, config_doc_id, config_sheet, f"N{row_count}:N{row_count}",[[str(ads_credit)]], number_format='#.##')
    dtd.safe_update_range(access_token, union_id, config_doc_id, config_sheet, f"D{row_count}:D{row_count}",[[str(sales)]],number_format='#.##')
    """ 更新token 刷新表格 """
    # access_token = dtd.get_access_token(app_key, app_secret)
    """读取是否充值及充值金额"""
    recharge_status =  dtd.safe_read_range(access_token, union_id, config_doc_id, config_sheet, f"P{row_count}:P{row_count}")[0][0]
    if recharge_status !='需充值':
        log.info(f'{shop_name} {recharge_status}')
        dtd.safe_update_range(access_token, union_id, config_doc_id, config_sheet, f"Q{row_count}:R{row_count}",[["'"+str(datetime.today().strftime('%Y-%m-%d')),'']], number_format='@')
        return False,None,access_token, union_id, config_doc_id, config_sheet,row_count
    recharge_amount = dtd.safe_read_range(access_token, union_id, config_doc_id, config_sheet,f"O{row_count}:O{row_count}")
    recharge_amount = recharge_amount[0][0]
    log.info(f'{shop_name} {recharge_status} 金额：{recharge_amount}')
    return True, recharge_amount,access_token, union_id, config_doc_id, config_sheet,row_count


"""Ads Credit xPATH"""
_ADS_CREDIT_LABEL_MATCH = (
    "contains(normalize-space(.), 'Ads Credit') or "
    "contains(normalize-space(.), 'Kredit Iklan') or "
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
    r"^\s*(?:(?:MYR|RM)\s*)?"
    r"(?P<amount>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"\s*(?:MYR|RM)?\s*$",
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
CLOSE_ALERT = "//i[contains(@class, 'eds-modal__close')] | //button[contains(normalize-space(), 'Got It')]"
"""税费"""
VAT = "//div[contains(@class, 'tax-rate') and contains(@class, 'price-item')]/div[@class='value']"
GST =  '//form//div[@data-testid="topup-gst-value"]'

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
WORLD_CARD_NUMBER = [
    "(//label[contains(concat(' ', normalize-space(@class), ' '), ' eds-radio ') and .//*[not(self::script or self::style) and text()[starts-with(translate(normalize-space(.), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'WORLD')]]])[1]",
    "(//*[not(self::script or self::style) and text()[starts-with(translate(normalize-space(.), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'WORLD')]]/ancestor-or-self::*[self::label or @role='radio' or contains(normalize-space(@class), 'radio') or contains(normalize-space(@class), 'option') or contains(normalize-space(@class), 'card') or contains(normalize-space(@class), 'bank')][1])[1]",
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
WORLDFIRST_SMS_BUTTON_XPATHS = [
    "//*[self::button or @role='button'][contains(normalize-space(.), '获取短信验证码')]",
    "//*[self::button or @role='button'][contains(normalize-space(.), '獲取短信驗證碼')]",
    "//*[contains(@class, 'button') or contains(@class, 'btn')][contains(normalize-space(.), '获取短信验证码') or contains(normalize-space(.), '獲取短信驗證碼')]",
    "//*[self::button or @role='button'][contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sms')]",
    "//*[normalize-space(.)='获取短信验证码' and not(.//*[normalize-space(.)='获取短信验证码'])]",
    "//*[normalize-space(.)='獲取短信驗證碼' and not(.//*[normalize-space(.)='獲取短信驗證碼'])]",
    "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sms') and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'code') and not(contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'email'))]",
]
WORLDFIRST_OTP_INPUT_XPATH = (
    "//input[not(@disabled) and not(@type='hidden') and ("
    "contains(translate(@class, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'otp') or "
    "contains(translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'otp') or "
    "contains(translate(@id, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'otp') or "
    "@autocomplete='one-time-code' or "
    "contains(@placeholder, '验证码') or contains(@placeholder, '驗證碼') or "
    "contains(translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'verification code') or "
    "(@inputmode='numeric' and (@maxlength='1' or @maxlength='4' or @maxlength='6' or @maxlength='8'))"
    ")]"
)
WORLDFIRST_MAX_IFRAME_DEPTH = 8
WORLDFIRST_SMS_CLICK_MAX_ATTEMPTS = 3
WORLDFIRST_SMS_CLICK_VERIFY_TIMEOUT = 8
WORLDFIRST_CONFIRM_BUTTON_XPATHS = [
    "//*[self::button or @role='button'][normalize-space()='提交' or normalize-space()='确认' or normalize-space()='確認' or normalize-space()='完成' or normalize-space()='验证' or normalize-space()='驗證' or normalize-space()='继续' or normalize-space()='繼續']",
    "//*[self::button or @role='button'][contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'submit') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'verify') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]",
]
WORLDFIRST_EMAIL_HINTS = (
    "电子邮件",
    "電子郵件",
    "邮箱",
    "郵箱",
    "email",
    "e-mail",
    "mailbox",
)
WORLDFIRST_MASKED_EMAIL_PATTERN = re.compile(
    r"[A-Z0-9._%+*\-]+@[A-Z0-9*\-.]+\.[A-Z*]{2,}",
    re.IGNORECASE,
)

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


def close_alert(webdriver_util:WebdriverUtil):
    webdriver_util.driver.implicitly_wait(1)
    close_btn_list = webdriver_util.find_elements(By.XPATH,CLOSE_ALERT)
    for close_btn in close_btn_list:
        log.info(f'关闭弹框')
        try:
            close_btn.click()
        except WebDriverException as e:
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
    normalized = str(text or "").replace("\xa0", " ").strip()
    match = _ADS_CREDIT_AMOUNT_PATTERN.fullmatch(normalized)
    if match is None:
        return None
    return parse_amount(match.group("amount"))


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


def _element_has_selected_state(element):
    try:
        aria_checked = str(element.get_attribute("aria-checked") or "").strip().lower()
        aria_selected = str(element.get_attribute("aria-selected") or "").strip().lower()
        if aria_checked == "true" or aria_selected == "true":
            return True
        class_name = str(element.get_attribute("class") or "").strip().lower()
    except Exception:
        return False

    if not class_name:
        return False
    class_tokens = set(re.split(r"\s+", class_name))
    if {"selected", "checked", "active"} & class_tokens:
        return True
    return any(
        token in class_name
        for token in (
            "eds-radio--checked",
            "radio--checked",
            "is-checked",
            "is_checked",
        )
    )


def _is_radio_label_selected(driver, label_element):
    try:
        return bool(
            driver.execute_script(
                """
                const root = arguments[0];
                const selectedClass = /(^|[\\s_-])(selected|checked|active)([\\s_-]|$)|eds-radio--checked|radio--checked|is-checked|is_checked/i;
                function hasSelectedState(element) {
                    if (!element) {
                        return false;
                    }
                    if (element.checked === true || element.selected === true) {
                        return true;
                    }
                    const ariaChecked = String(element.getAttribute && element.getAttribute('aria-checked') || '').toLowerCase();
                    const ariaSelected = String(element.getAttribute && element.getAttribute('aria-selected') || '').toLowerCase();
                    if (ariaChecked === 'true' || ariaSelected === 'true') {
                        return true;
                    }
                    const className = String(element.getAttribute && element.getAttribute('class') || '');
                    return selectedClass.test(className);
                }

                const candidates = [root];
                if (root.querySelectorAll) {
                    candidates.push(...root.querySelectorAll(
                        'input[type="radio"], [role="radio"], [aria-checked], [aria-selected], [class*="selected"], [class*="checked"], [class*="active"]'
                    ));
                }
                for (const item of candidates) {
                    if (hasSelectedState(item)) {
                        return true;
                    }
                    const input = item && item.matches && item.matches('input[type="radio"]')
                        ? item
                        : item && item.querySelector && item.querySelector('input[type="radio"]');
                    if (hasSelectedState(input)) {
                        return true;
                    }
                }
                return false;
                """,
                label_element,
            )
        )
    except WebDriverException:
        try:
            if _element_has_selected_state(label_element):
                return True
            radio_inputs = label_element.find_elements(By.XPATH, ".//input[@type='radio']")
            if any(item.is_selected() or _element_has_selected_state(item) for item in radio_inputs):
                return True
            state_elements = label_element.find_elements(
                By.XPATH,
                ".//*[@aria-checked='true' or @aria-selected='true' or contains(concat(' ', normalize-space(@class), ' '), ' selected ') or contains(concat(' ', normalize-space(@class), ' '), ' checked ') or contains(concat(' ', normalize-space(@class), ' '), ' active ') or contains(@class, 'eds-radio--checked') or contains(@class, 'radio--checked') or contains(@class, 'is-checked')]",
            )
            return bool(state_elements)
        except Exception:
            return False


def _resolve_payment_card_click_target(driver, card_element):
    try:
        return (
            driver.execute_script(
                """
                const start = arguments[0];
                const primarySelectors = [
                    'label',
                    '[role="radio"]',
                    '.eds-radio',
                    '[class*="radio"]'
                ];
                const fallbackSelectors = [
                    'button',
                    '[class*="option"]',
                    '[class*="card"]',
                    '[class*="bank"]'
                ];
                function closestMatch(selectors) {
                    for (const selector of selectors) {
                        for (let element = start, depth = 0; element && depth < 8; element = element.parentElement, depth += 1) {
                            if (!element.matches) {
                                continue;
                            }
                        try {
                            if (element.matches(selector)) {
                                return element;
                            }
                        } catch (error) {}
                    }
                }
                    return null;
                }
                return closestMatch(primarySelectors) || closestMatch(fallbackSelectors) || start;
                """,
                card_element,
            )
            or card_element
        )
    except WebDriverException:
        return card_element


def _has_radio_input(label_element):
    try:
        return bool(label_element.find_elements(By.XPATH, ".//input[@type='radio']"))
    except Exception:
        return False


def _ensure_credit_card_payment_selected(webdriver_util, store_name, card_xpaths=None):
    driver = webdriver_util.driver
    card_xpaths = card_xpaths or BANK_NUMBER
    if _wait_for_visible_element_by_xpaths(driver, CREDIT_CARD_SELECTED, timeout=2):
        log.info(f"{store_name}: 已选中 Credit / Debit Card/信用卡支付方式")
        return True, ""

    if _wait_for_visible_element_by_xpaths(driver, card_xpaths, timeout=2):
        log.info(f"{store_name}: 已显示目标银行卡，视为已选中信用卡支付方式")
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
    if _wait_for_visible_element_by_xpaths(driver, card_xpaths, timeout=10):
        return True, ""

    page_context = _short_page_context(driver)
    return False, f"{store_name}:点击后未检测到 Credit / Debit Card/信用卡 被选中，已停止提交支付。{page_context}"


def _select_first_payoneer_card(webdriver_util, store_name):
    driver = webdriver_util.driver
    card_element = _wait_for_visible_element_by_xpaths(driver, BANK_NUMBER, timeout=15)
    if card_element is None:
        page_context = _short_page_context(driver)
        return False, f"{store_name}:Credit / Debit Card 下未找到 PAYONEER EUROPE 卡，已停止提交支付。{page_context}"

    card_control = _resolve_payment_card_click_target(driver, card_element)
    card_text = re.sub(
        r"\s+",
        " ",
        getattr(card_control, "text", "") or getattr(card_element, "text", ""),
    ).strip() or "PAYONEER EUROPE"
    if _is_radio_label_selected(driver, card_control):
        log.info(f"{store_name}: 已选中银行卡：{card_text}")
        return True, ""

    try:
        log.info(f"{store_name}: 选择第一张 PAYONEER EUROPE 卡：{card_text}")
        webdriver_util.move_to_click(card_control)
    except WebDriverException as exc:
        return False, f"{store_name}:点击 PAYONEER EUROPE 卡失败，已停止提交支付：{exc}"

    deadline = time.time() + 5
    while time.time() < deadline:
        refreshed_card = _wait_for_visible_element_by_xpaths(driver, BANK_NUMBER, timeout=1)
        if refreshed_card is not None:
            refreshed_control = _resolve_payment_card_click_target(driver, refreshed_card)
            if _is_radio_label_selected(driver, refreshed_control):
                return True, ""
        time.sleep(0.3)

    page_context = _short_page_context(driver)
    return False, f"{store_name}:点击后未检测到 PAYONEER EUROPE 卡被选中，已停止提交支付。{page_context}"


def _select_first_world_card(webdriver_util, store_name):
    driver = webdriver_util.driver
    card_element = _wait_for_visible_element_by_xpaths(driver, WORLD_CARD_NUMBER, timeout=15)
    if card_element is None:
        page_context = _short_page_context(driver)
        return False, f"{store_name}:Credit / Debit Card 下未找到 WORLD 开头银行卡，已停止提交支付。{page_context}"

    card_control = _resolve_payment_card_click_target(driver, card_element)
    card_text = re.sub(
        r"\s+",
        " ",
        getattr(card_control, "text", "") or getattr(card_element, "text", ""),
    ).strip() or "WORLD"
    if _is_radio_label_selected(driver, card_control):
        log.info(f"{store_name}: 已选中 WORLD 银行卡：{card_text}")
        return True, ""

    try:
        log.info(f"{store_name}: 选择第一张 WORLD 银行卡：{card_text}")
        webdriver_util.move_to_click(card_control)
    except WebDriverException as exc:
        return False, f"{store_name}:点击 WORLD 银行卡失败，已停止提交支付：{exc}"

    deadline = time.time() + 5
    while time.time() < deadline:
        refreshed_card = _wait_for_visible_element_by_xpaths(driver, WORLD_CARD_NUMBER, timeout=1)
        if refreshed_card is not None:
            refreshed_control = _resolve_payment_card_click_target(driver, refreshed_card)
            if _is_radio_label_selected(driver, refreshed_control):
                return True, ""
        time.sleep(0.3)

    page_context = _short_page_context(driver)
    return False, f"{store_name}:点击后未检测到 WORLD 银行卡被选中，已停止提交支付。{page_context}"


def _select_payment_card(webdriver_util, store_name, requested_card_type=None):
    card_type = str(requested_card_type or payment_card_type or "payoneer").strip().lower()
    if card_type == "world":
        return _select_first_world_card(webdriver_util, store_name)
    return _select_first_payoneer_card(webdriver_util, store_name)


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
    try:
        with _TemporaryRemoteTimeout(5):
            text = _get_visible_body_text(driver)
    except Exception as exc:
        text = f"页面文本读取失败：{exc}"
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > limit:
        text = text[:limit] + "..."
    return f"url={url}; text={text}"


def _find_visible_elements_current_context(driver, xpath, manage_implicit_wait=True):
    old_wait = None
    if manage_implicit_wait:
        try:
            old_wait = driver.timeouts.implicit_wait
        except Exception:
            pass
        try:
            driver.implicitly_wait(0)
        except Exception:
            pass
    try:
        return [item for item in driver.find_elements(By.XPATH, xpath) if item.is_displayed()]
    except Exception:
        return []
    finally:
        if manage_implicit_wait and old_wait is not None:
            try:
                driver.implicitly_wait(old_wait)
            except Exception:
                pass


def _reset_worldfirst_scan_diagnostics(diagnostics=None):
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.clear()
    diagnostics.update(
        {
            "window_count": 0,
            "contexts_scanned": 0,
            "iframes_seen": 0,
            "max_depth_seen": 0,
            "depth_limit_reached": False,
            "found_depth": None,
            "found_frame_path": "",
            "last_url": "",
            "last_error": "",
        }
    )
    return diagnostics


def _find_worldfirst_element_recursive(
    driver,
    xpaths,
    diagnostics,
    depth=0,
    frame_path=(),
    max_depth=WORLDFIRST_MAX_IFRAME_DEPTH,
    implicit_wait_already_zero=False,
):
    diagnostics["contexts_scanned"] += 1
    diagnostics["max_depth_seen"] = max(diagnostics["max_depth_seen"], depth)

    for xpath in xpaths:
        elements = _find_visible_elements_current_context(
            driver,
            xpath,
            manage_implicit_wait=not implicit_wait_already_zero,
        )
        if elements:
            diagnostics["found_depth"] = depth
            diagnostics["found_frame_path"] = "/".join(str(index) for index in frame_path) or "top"
            return elements[0]

    frames = _find_visible_elements_current_context(
        driver,
        "//iframe",
        manage_implicit_wait=not implicit_wait_already_zero,
    )
    diagnostics["iframes_seen"] += len(frames)
    if depth >= max_depth:
        diagnostics["depth_limit_reached"] = bool(frames) or diagnostics["depth_limit_reached"]
        return None

    # 每次进入同级 iframe 前都重新获取元素，降低页面动态刷新导致 stale element 的概率。
    for frame_index in range(len(frames)):
        current_frames = _find_visible_elements_current_context(
            driver,
            "//iframe",
            manage_implicit_wait=not implicit_wait_already_zero,
        )
        if frame_index >= len(current_frames):
            break
        found = None
        switched = False
        try:
            driver.switch_to.frame(current_frames[frame_index])
            switched = True
            found = _find_worldfirst_element_recursive(
                driver,
                xpaths,
                diagnostics,
                depth=depth + 1,
                frame_path=frame_path + (frame_index,),
                max_depth=max_depth,
                implicit_wait_already_zero=implicit_wait_already_zero,
            )
            if found is not None:
                return found
        except Exception as exc:
            diagnostics["last_error"] = str(exc)[:300]
        finally:
            if switched and found is None:
                try:
                    driver.switch_to.parent_frame()
                except Exception as exc:
                    diagnostics["last_error"] = f"restore parent frame failed: {exc}"[:300]
                    raise
    return None


def _wait_worldfirst_element(driver, xpaths, timeout=30, diagnostics=None):
    diagnostics = _reset_worldfirst_scan_diagnostics(diagnostics)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with _TemporaryRemoteTimeout(5):
                handles = list(driver.window_handles)
        except Exception:
            handles = []
        if not handles:
            handles = [None]
        diagnostics["window_count"] = max(diagnostics["window_count"], len(handles))

        for handle in reversed(handles):
            try:
                with _TemporaryRemoteTimeout(5):
                    if handle is not None:
                        driver.switch_to.window(handle)
                    driver.switch_to.default_content()
                    try:
                        diagnostics["last_url"] = str(driver.current_url or "")[:300]
                    except Exception:
                        pass
                    old_wait = None
                    implicit_wait_already_zero = False
                    try:
                        old_wait = driver.timeouts.implicit_wait
                        driver.implicitly_wait(0)
                        implicit_wait_already_zero = True
                    except Exception:
                        pass
                    try:
                        element = _find_worldfirst_element_recursive(
                            driver,
                            xpaths,
                            diagnostics,
                            implicit_wait_already_zero=implicit_wait_already_zero,
                        )
                    finally:
                        if implicit_wait_already_zero and old_wait is not None:
                            try:
                                driver.implicitly_wait(old_wait)
                            except Exception:
                                pass
                    if element is not None:
                        return element
            except Exception as exc:
                diagnostics["last_error"] = str(exc)[:300]
                continue
        time.sleep(0.5)
    return None


def _format_worldfirst_scan_diagnostics(diagnostics):
    parts = [
        f"windows={diagnostics.get('window_count', 0)}",
        f"contexts={diagnostics.get('contexts_scanned', 0)}",
        f"iframes={diagnostics.get('iframes_seen', 0)}",
        f"max_depth={diagnostics.get('max_depth_seen', 0)}/{WORLDFIRST_MAX_IFRAME_DEPTH}",
    ]
    if diagnostics.get("depth_limit_reached"):
        parts.append("depth_limit_reached=true")
    if diagnostics.get("last_url"):
        parts.append(f"url={diagnostics['last_url']}")
    if diagnostics.get("last_error"):
        parts.append(f"last_error={diagnostics['last_error']}")
    return ", ".join(parts)


def _resolve_worldfirst_click_target(driver, element):
    try:
        target = driver.execute_script(
            """
            let node = arguments[0];
            const selector = "button,[role='button'],a,input[type='button'],input[type='submit'],[onclick],[tabindex]";
            for (let depth = 0; node && depth <= 6; depth += 1, node = node.parentElement) {
              const style = window.getComputedStyle(node);
              if ((node.matches && node.matches(selector)) || style.cursor === 'pointer') {
                return node;
              }
            }
            return arguments[0];
            """,
            element,
        )
        return target or element
    except Exception:
        return element


def _click_worldfirst_sms_button(driver, element):
    target = _resolve_worldfirst_click_target(driver, element)
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target)
    except Exception:
        pass

    native_error = ""
    try:
        target.click()
        return True, ""
    except Exception as exc:
        native_error = str(exc)

    try:
        driver.execute_script("arguments[0].click();", target)
        return True, ""
    except Exception as exc:
        return False, f"native={native_error}; script={exc}"


def _find_worldfirst_otp_inputs(driver):
    inputs = _find_visible_elements_current_context(driver, WORLDFIRST_OTP_INPUT_XPATH)
    return [item for item in inputs if item.is_enabled()]


def _send_worldfirst_input_value(driver, input_box, value):
    try:
        driver.execute_script("arguments[0].focus();", input_box)
    except Exception:
        pass
    try:
        input_box.click()
    except Exception:
        pass
    try:
        input_box.clear()
    except Exception:
        pass
    input_box.send_keys(value)
    try:
        driver.execute_script(
            """
            const el = arguments[0];
            const value = arguments[1];
            if (el.value !== value) {
              el.value = value;
            }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            """,
            input_box,
            value,
        )
    except Exception:
        pass


def _mask_otp_code(code):
    return "*" * len(str(code or ""))


def _world_sms_int(value, default, minimum=1, maximum=600):
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _world_sms_amount_keywords(payment_total_content):
    text = str(payment_total_content or "").upper().replace(",", "")
    text = text.replace("MYR", "").replace("RM", "").strip()
    amount = parse_amount(text)
    if not amount:
        return []
    amount_float = float(amount)
    amount_2 = f"{amount_float:.2f}"
    amount_trimmed = amount_2.rstrip("0").rstrip(".")
    keywords = {
        f"MYR {amount_2}",
        f"MYR{amount_2}",
        f"{amount_2}MYR",
        f"MYR {amount_trimmed}",
        f"MYR{amount_trimmed}",
        f"{amount_trimmed}MYR",
    }
    return [item.upper() for item in keywords if item]


def _world_sms_merchant_keywords(cfg):
    raw_values = []
    configured_values = cfg.get("merchant_keywords")
    if isinstance(configured_values, (list, tuple, set)):
        raw_values.extend(configured_values)
    elif configured_values:
        raw_values.append(configured_values)

    legacy_keyword = cfg.get("merchant_keyword")
    if legacy_keyword:
        raw_values.append(legacy_keyword)

    # New WorldFirst transaction SMS templates use the shorter ``SHOPEE``
    # merchant label, while older messages use the three more specific labels.
    raw_values.extend(["MY SHOPEE MARKETPLACE", "SHOPEE MY MARKETPLACE", "FOR SHOPEE", "SHOPEE"])
    keywords = []
    seen = set()
    for raw in raw_values:
        for item in re.split(r"[,，;\n|]+", str(raw or "")):
            keyword = item.strip().upper()
            if keyword and keyword not in seen:
                keywords.append(keyword)
                seen.add(keyword)
    return keywords


def _extract_world_otp_code(row):
    for key in ("otp_code", "possible_code"):
        code = str(row.get(key) or "").strip()
        if re.fullmatch(r"\d{4,8}", code):
            return code
    message = str(row.get("message") or "")
    match = re.search(r"验证码为[:：]?\s*(\d{4,8})", message)
    if match:
        return match.group(1)
    return ""


def _classify_worldfirst_otp_delivery_text(text):
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    lower_text = normalized.lower()
    masked_match = WORLDFIRST_MASKED_EMAIL_PATTERN.search(normalized)
    masked_email = masked_match.group(0) if masked_match else ""
    has_email_hint = any(hint.lower() in lower_text for hint in WORLDFIRST_EMAIL_HINTS)
    if has_email_hint:
        return {"channel": "email", "masked_email": masked_email, "text": normalized}
    return {"channel": "sms", "masked_email": "", "text": normalized}


def _worldfirst_otp_delivery_context(driver):
    try:
        page_text = _get_visible_body_text(driver)
    except Exception:
        page_text = ""
    return _classify_worldfirst_otp_delivery_text(page_text)


def _wait_world_card_sms_code(store_name, payment_total_content, request_started_at):
    cfg = world_card_sms_config or {}
    sim_number = str(cfg.get("sim_number") or "").strip()
    if not sim_number:
        raise ValueError("请先配置此站点的 WORLD 卡验证手机号。")
    platform_name = str(cfg.get("platform_name") or "万里汇").strip()
    merchant_keywords = _world_sms_merchant_keywords(cfg)
    timeout = _world_sms_int(cfg.get("timeout"), 180)
    poll_interval = _world_sms_int(cfg.get("poll_interval"), 3)
    amount_keywords = _world_sms_amount_keywords(payment_total_content)
    start_at = request_started_at - timedelta(seconds=30)
    deadline = time.time() + timeout
    last_seen_id = ""
    last_error = ""

    import psycopg2
    from psycopg2.extras import RealDictCursor

    from backend.ads_database import connect_runtime_database

    sql = """
        SELECT id, received_at, created_at, otp_code, possible_code, message
        FROM shopee_ads.world_card_payment_messages
        WHERE sim_number = %s
          AND platform_name = %s
          AND is_incomplete = false
          AND received_at >= %s
          AND (
            otp_code <> ''
            OR possible_code <> ''
            OR message ~ '验证码为[:：]?\\s*\\d{4,8}'
          )
        ORDER BY received_at DESC, created_at DESC
        LIMIT 10
    """

    while time.time() < deadline:
        try:
            with connect_runtime_database() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql, (sim_number, platform_name, start_at))
                    rows = cur.fetchall()
            for row in rows:
                last_seen_id = str(row.get("id") or last_seen_id)
                message = str(row.get("message") or "").upper().replace(",", "")
                if merchant_keywords and not any(keyword in message for keyword in merchant_keywords):
                    continue
                if amount_keywords and not any(keyword in message for keyword in amount_keywords):
                    continue
                code = _extract_world_otp_code(row)
                if code:
                    log.info(f"{store_name}: 已从 sms_receiver_message 获取 WORLD 短信验证码：{_mask_otp_code(code)}，短信ID：{row.get('id')}")
                    return code
        except Exception as exc:
            last_error = str(exc)
            log.warning(f"{store_name}: 查询 WORLD 短信验证码失败，继续等待：{exc}")

        time.sleep(poll_interval)

    detail = f"last_sms_id={last_seen_id}" if last_seen_id else "未查询到候选短信"
    if last_error:
        detail += f"，last_error={last_error}"
    raise SmsOtpTaskError(f"{store_name}: WORLD 短信验证码等待超时：{detail}")


def _wait_world_card_email_code(store_name, recharge_amount, payment_total_content, masked_email=""):
    cfg = security_verify_config or {}
    display_email = str(masked_email or "页面显示邮箱").strip()
    template = str(
        cfg.get("world_email_code_system_name")
        or "【{shop_name}】WORLD 邮箱验证码接收（{masked_email}）"
    )
    try:
        system_name = template.format(
            shop_name=store_name,
            recharge_amount=recharge_amount,
            payment_total=payment_total_content,
            masked_email=display_email,
        )
    except Exception:
        system_name = f"【{store_name}】WORLD 邮箱验证码接收（{display_email}）"

    log.info(f"{store_name}: 检测到 WorldFirst 邮箱验证码页面，创建邮箱验证码接码任务")
    otp_code, _task_id = _request_security_code(
        store_name,
        recharge_amount,
        payment_total_content,
        "world_email",
        "WORLD 邮箱验证码",
        system_name,
    )
    code = str(otp_code or "").strip()
    if not re.fullmatch(r"\d{4,8}", code):
        raise SmsOtpTaskError(f"{store_name}: WorldFirst 邮箱验证码格式无效")
    return code


def _fill_worldfirst_otp_code(driver, otp_code):
    input_element = _wait_worldfirst_element(driver, [WORLDFIRST_OTP_INPUT_XPATH], timeout=20)
    if input_element is None:
        raise SmsOtpTaskError("WorldFirst OTP input not found")

    inputs = _find_worldfirst_otp_inputs(driver)
    code = str(otp_code or "").strip()
    if len(inputs) >= len(code) >= 4:
        for input_box, digit in zip(inputs, code):
            _send_worldfirst_input_value(driver, input_box, digit)
    else:
        _send_worldfirst_input_value(driver, input_element, code)

    confirm_button = _wait_worldfirst_element(driver, WORLDFIRST_CONFIRM_BUTTON_XPATHS, timeout=10)
    if confirm_button is not None:
        try:
            driver.execute_script("arguments[0].click();", confirm_button)
        except WebDriverException:
            confirm_button.click()
    driver.switch_to.default_content()


def _handle_worldfirst_id_check(webdriver_util, store_name, recharge_amount, payment_total_content):
    driver = webdriver_util.driver
    cfg = world_card_sms_config or {}
    if not bool(cfg.get("enabled", True)):
        log.info(f"{store_name}: WORLD 验证码接码已关闭，跳过 WorldFirst ID Check 接码流程")
        return True, "WORLD 验证码接码已跳过"
    auth_button_timeout = _world_sms_int(cfg.get("auth_button_timeout"), 180, minimum=30, maximum=600)

    # 轮询：同时检测 OTP 输入框（验证码自动发送）和获取短信按钮（需手动点击）。
    started_at = time.time()
    deadline = started_at + auth_button_timeout
    next_progress_log_at = started_at + 10
    sms_click_attempts = 0
    first_sms_click_at = None
    last_click_error = ""
    otp_input = None
    scan_diagnostics = {}
    request_started_at = datetime.now().astimezone() - timedelta(seconds=30)

    while time.time() < deadline:
        # 先检测 OTP 输入框是否已出现
        otp_input = _wait_worldfirst_element(
            driver,
            [WORLDFIRST_OTP_INPUT_XPATH],
            timeout=2,
            diagnostics=scan_diagnostics,
        )
        if otp_input is not None:
            if sms_click_attempts == 0:
                log.info(f"{store_name}: WorldFirst OTP 输入框已出现，验证码已自动发送")
            else:
                log.info(
                    f"{store_name}: WorldFirst OTP 输入框已出现，验证码发送流程已生效，"
                    f"frame={scan_diagnostics.get('found_frame_path') or 'top'}"
                )
            break

        # OTP 未出现，检测是否需要点击获取短信按钮
        if sms_click_attempts < WORLDFIRST_SMS_CLICK_MAX_ATTEMPTS:
            sms_button = _wait_worldfirst_element(
                driver,
                WORLDFIRST_SMS_BUTTON_XPATHS,
                timeout=2,
                diagnostics=scan_diagnostics,
            )
            if sms_button is not None:
                sms_click_attempts += 1
                frame_path = scan_diagnostics.get("found_frame_path") or "top"
                log.info(
                    f"{store_name}: 检测到 WorldFirst 获取短信验证码控件，"
                    f"尝试点击（{sms_click_attempts}/{WORLDFIRST_SMS_CLICK_MAX_ATTEMPTS}），frame={frame_path}"
                )
                try:
                    with _TemporaryRemoteTimeout(8):
                        clicked, click_error = _click_worldfirst_sms_button(driver, sms_button)
                except Exception as exc:
                    clicked, click_error = False, str(exc)

                if not clicked:
                    last_click_error = click_error
                    log.warning(
                        f"{store_name}: WorldFirst 短信验证码控件点击失败，继续重试：{click_error}"
                    )
                else:
                    if first_sms_click_at is None:
                        first_sms_click_at = datetime.now().astimezone()
                        request_started_at = first_sms_click_at
                    verify_timeout = min(
                        WORLDFIRST_SMS_CLICK_VERIFY_TIMEOUT,
                        max(0.5, deadline - time.time()),
                    )
                    otp_input = _wait_worldfirst_element(
                        driver,
                        [WORLDFIRST_OTP_INPUT_XPATH],
                        timeout=verify_timeout,
                        diagnostics=scan_diagnostics,
                    )
                    if otp_input is not None:
                        log.info(
                            f"{store_name}: WorldFirst 短信控件点击后已检测到 OTP 输入框，"
                            f"frame={scan_diagnostics.get('found_frame_path') or 'top'}"
                        )
                        break
                    last_click_error = "点击后未检测到 OTP 输入框或页面状态变化"
                    log.warning(
                        f"{store_name}: WorldFirst 短信控件点击后 {verify_timeout:.1f} 秒内未出现 OTP 输入框，"
                        f"将重新定位后重试"
                    )

        now = time.time()
        if now >= next_progress_log_at:
            elapsed = int(now - started_at)
            log.info(
                f"{store_name}: 仍在等待 WorldFirst 短信验证页面，elapsed={elapsed}s，"
                f"click_attempts={sms_click_attempts}，{_format_worldfirst_scan_diagnostics(scan_diagnostics)}"
            )
            next_progress_log_at = now + 10

        time.sleep(1)

    if otp_input is None:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        page_context = _short_page_context(driver)
        click_detail = f"; last_click_error={last_click_error}" if last_click_error else ""
        return (
            False,
            f"{store_name}: WORLD 支付等待 WorldFirst 验证控件超时，"
            f"click_attempts={sms_click_attempts}; {_format_worldfirst_scan_diagnostics(scan_diagnostics)}"
            f"{click_detail}。{page_context}",
        )

    delivery = _worldfirst_otp_delivery_context(driver)
    if delivery.get("channel") == "email":
        otp_code = _wait_world_card_email_code(
            store_name,
            recharge_amount,
            payment_total_content,
            delivery.get("masked_email", ""),
        )
        success_message = "WORLD 邮箱验证码已回填"
    else:
        if cfg.get("verification_mode") == "bank_page":
            return True, "请在银行页面输入短信验证码并提交，随后会核对支付结果。"
        otp_code = _wait_world_card_sms_code(store_name, payment_total_content, request_started_at)
        success_message = "WORLD 短信验证码已回填"
    log.info(f"{store_name}: WORLD 验证码已获取，准备回填：{_mask_otp_code(otp_code)}")
    _fill_worldfirst_otp_code(driver, otp_code)
    return True, success_message


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
    """点击top_up"""
    close_alert(webdriver_util)
    top_up_btn_element = _wait_for_visible_element_by_xpaths(webdriver_util.driver, TOP_UP_BUTTON_FALLBACKS, timeout=30)
    if top_up_btn_element is None:
        page_context = _short_page_context(webdriver_util.driver)
        return False, f"{store_name}:未找到广告充值 Top Up 入口，可能页面未进入 Ads 钱包页、店铺无广告余额入口或页面结构变化。{page_context}"
    webdriver_util.move_to_click(top_up_btn_element)
    recharge_amount = round(float(recharge_amount))
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
    verify_amount = parse_amount(_clean_currency_text(verify_amount_content))
    verify_amount = int(verify_amount)
    """校验金额是否相等"""
    if verify_amount != recharge_amount:
        return False, f"{store_name}:充值广告费用一次校验异常。"
    gst_content = _get_optional_currency_text(webdriver_util.driver, GST, default="-")
    submit_btn = webdriver_util.find_element(By.XPATH, RECHARGE_SUBMIT)
    webdriver_util.move_to_click(submit_btn)
    time.sleep(2)
    """等待提交 """
    verify_amount_content2 = _wait_for_visible_text_by_xpaths(webdriver_util.driver, VERIFY_AMOUNT2_FALLBACKS, timeout=25)
    if not verify_amount_content2:
        page_context = _short_page_context(webdriver_util.driver)
        return False, f"{store_name}:未检测到付款确认页金额，可能确认页未加载或页面结构变化。{page_context}"
    payment_total_content = _wait_for_visible_text_by_xpaths(webdriver_util.driver, PAYMENT_TOTAL_FALLBACKS, timeout=10)
    if not payment_total_content:
        payment_total_content = "-"
    else:
        payment_total_content = _clean_currency_text(payment_total_content).strip()
    verify_amount2 = parse_amount(_clean_currency_text(verify_amount_content2))
    """校验金额是否相等2"""
    if verify_amount2 != recharge_amount:
        return False,f"{store_name}:充值广告费用二次校验异常。"

    card_type = str(payment_card_type or "payoneer").strip().lower()
    target_card_xpaths = WORLD_CARD_NUMBER if card_type == "world" else BANK_NUMBER
    payment_method_ok, payment_method_msg = _ensure_credit_card_payment_selected(
        webdriver_util,
        store_name,
        card_xpaths=target_card_xpaths,
    )
    if not payment_method_ok:
        return False, payment_method_msg

    log.info(f"{store_name}: 付款卡配置为 {card_type.upper()}，开始精确选择并校验银行卡")
    card_ok, card_msg = _select_payment_card(webdriver_util, store_name, card_type)
    if not card_ok:
        return False, card_msg

    log.info(f"{store_name}: 开始提交 WORLD 支付" if card_type == "world" else f"{store_name}: 开始提交支付")
    try:
        with _TemporaryRemoteTimeout(15 if card_type == "world" else 30):
            submit_btn = webdriver_util.find_element(By.XPATH, CONFIRM_BTN)
    except Exception as exc:
        return False, f"{store_name}: 未能读取支付提交按钮，已停止支付：{exc}"

    if card_type == "world":
        try:
            with _TemporaryRemoteTimeout(20):
                webdriver_util.move_to_click(submit_btn)
        except Exception as exc:
            log.warning(f"{store_name}: WORLD 支付提交点击返回超时或异常，继续探测验证页面：{exc}")
    else:
        webdriver_util.move_to_click(submit_btn)
    if card_type == "world":
        world_ok, world_msg = _handle_worldfirst_id_check(
            webdriver_util,
            store_name,
            recharge_amount,
            payment_total_content,
        )
        if world_msg:
            log.info(world_msg)
        if not world_ok:
            return False, world_msg
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

    return True, f"支付成功！【{store_name}】:脚本充值金额：{verify_amount2},GST:{gst_content},支付总额：{payment_total_content}。更新时间：{str(datetime.today().strftime('%Y-%m-%d'))}"






def my_shopee_ads_recharge(driver, store_name: str, output_path: str = ""):
    log.info(f"正在处理店铺: {store_name}")
    webdriver_util = WebdriverUtil(driver)

    if not _ensure_login(webdriver_util, store_name):
        _write_error(store_name, f"{store_name}: Shopee 登录异常")
        return
    if '仓发' not in store_name:
        # 跳转到 Shopee Ads
        if ads_wallet_url:
            webdriver_util.go_to(ads_wallet_url)
            time.sleep(5)
        # 等待 Ads Credit 出现
        ads_credit_element, ads_credit, ads_credit_error = _wait_for_ads_credit(webdriver_util, store_name, timeout=30)
        if ads_credit_error:
            _write_error(store_name, ads_credit_error)
            return {"status": "failed", "message": ads_credit_error}

        """选择时间"""
        log.info('选择时间')
        select_previous_7_days_expense_range(webdriver_util, store_name, DATE_INPUT, log, amount_xpath=SALES_XPATH)
        log.info('时间选择完毕')

        sales_element = webdriver_util.find_element(By.XPATH,SALES_XPATH)

        sales = parse_amount(_clean_currency_text(sales_element.text.strip()))
        is_recharges,recharge_amount,access_token, union_id, config_doc_id, config_sheet,row_count = ansy_dingtalk_doc(ads_credit, sales, store_name)
        if is_recharges:
            """开始执行充值操作"""
            if is_recharge_amount_below_minimum(recharge_amount, SITE_CODE):
                msg = format_minimum_recharge_skip_message(SITE_CODE, store_name, recharge_amount)
                dtd.safe_update_range(access_token, union_id, config_doc_id, config_sheet, f"Q{row_count}:R{row_count}",[["'" + str(datetime.today().strftime('%Y-%m-%d')), msg]], number_format='@')
                log.info(msg)
                return {"status": "skipped", "message": msg}
            is_recharge,msg = recharge_ads(webdriver_util, store_name, recharge_amount)
            dtd.safe_update_range(access_token, union_id, config_doc_id, config_sheet, f"Q{row_count}:R{row_count}",[["'" + str(datetime.today().strftime('%Y-%m-%d')), msg]], number_format='@')
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
            _write_error(store_name, ads_credit_error)
            return {"status": "failed", "message": ads_credit_error}

        """选择时间"""
        log.info('选择时间')
        select_previous_7_days_expense_range(webdriver_util, store_name, DATE_INPUT, log, amount_xpath=SALES_XPATH)
        log.info('时间选择完毕')

        sales_element = webdriver_util.find_element(By.XPATH, SALES_XPATH)

        sales = parse_amount(_clean_currency_text(sales_element.text.strip()))
        is_recharges, recharge_amount,access_token, union_id, config_doc_id, config_sheet,row_count = ansy_dingtalk_doc(ads_credit, sales, store_name)
        if is_recharges:
            """开始执行充值操作"""
            if is_recharge_amount_below_minimum(recharge_amount, SITE_CODE):
                msg = format_minimum_recharge_skip_message(SITE_CODE, store_name, recharge_amount)
                dtd.safe_update_range(access_token, union_id, config_doc_id, config_sheet, f"Q{row_count}:R{row_count}",[["'" + str(datetime.today().strftime('%Y-%m-%d')), msg]], number_format='@')
                log.info(msg)
                return {"status": "skipped", "message": msg}
            is_recharge,msg = recharge_ads(webdriver_util, store_name, recharge_amount)
            dtd.safe_update_range(access_token, union_id, config_doc_id, config_sheet, f"Q{row_count}:R{row_count}",[["'" + str(datetime.today().strftime('%Y-%m-%d')), msg]], number_format='@')
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
