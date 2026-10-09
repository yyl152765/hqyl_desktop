"""Read the four Lazada balance figures from the active ZiNiao store browser.

The three paths below are Seller Center pages observed in the user procedure and
in live PH/ID/MY sessions. No payout, recharge, export, or upload action is used.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from backend.services.balance_evidence import capture_balance_evidence
from backend.services.lazada_balance_login import LazadaLoginError, ensure_lazada_page, is_lazada_login_url
from backend.services.lazada_balance_language import LazadaLanguageError, ensure_lazada_chinese


COUNTRY_DOMAINS = {
    "PH": "sellercenter.lazada.com.ph",
    "MY": "sellercenter.lazada.com.my",
    "TH": "sellercenter.lazada.co.th",
    "ID": "sellercenter.lazada.co.id",
    "VN": "sellercenter.lazada.vn",
    "SG": "sellercenter.lazada.sg",
}
CURRENCIES = {"PH": "PHP", "MY": "MYR", "TH": "THB", "ID": "IDR", "VN": "VND", "SG": "SGD"}
COUNTRY_ALIASES = {
    "菲律宾": "PH", "马来西亚": "MY", "马来": "MY", "泰国": "TH",
    "印度尼西亚": "ID", "印尼": "ID", "越南": "VN", "新加坡": "SG",
}
PATHS = {
    "income": "/portal/apps/finance/myIncome/index",
    "balance": "/apps/balance",
    "ads": "/sponsor/solutions/ads/productads/#!/overview",
}
FIELD_LABELS = {"income": "Income 待释放", "balance": "Balance 总计余额", "ads": "Ads 可用余额", "processing": "processing 提现状态"}
_AMOUNT_RE = re.compile(r"(?<!\w)[+\-−]?\s*\d[\d.,\s]*")


class LazadaBalanceError(ValueError):
    """A field cannot be read or verified safely."""


def _country(value: object) -> str:
    value = str(value or "").strip()
    if not value or value.upper() == "AUTO":
        return ""
    result = COUNTRY_ALIASES.get(value, value.upper())
    if result not in COUNTRY_DOMAINS:
        raise LazadaBalanceError("Lazada 站点须为 PH、MY、TH、ID、VN 或 SG")
    return result


def _country_from_url(url: str) -> str:
    host = (urlsplit(str(url or "")).hostname or "").lower()
    return next((code for code, domain in COUNTRY_DOMAINS.items() if host == domain), "")


def _country_hint(store: dict) -> str:
    """A shop label is only a navigation hint; page host and currency are checked later."""
    name = str(store.get("store_name") or store.get("name") or "")
    matches = {code for label, code in COUNTRY_ALIASES.items() if label in name}
    return next(iter(matches)) if len(matches) == 1 else ""


def _safe_url(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return f"{parsed.scheme}://{parsed.hostname}{parsed.path}"


def _page_text(driver) -> str:
    return str(driver.execute_script("return document.body ? document.body.innerText : ''") or "")


def _wait_until(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.35)
    return bool(predicate())


def _open_page(driver, country: str, field: str, login_state: dict) -> dict:
    target = f"https://{COUNTRY_DOMAINS[country]}{PATHS[field]}"
    ensure_lazada_page(driver, target, login_state)
    if field == 'ads':
        _dismiss_ads_guides(driver)
    try:
        language = ensure_lazada_chinese(driver)
    except LazadaLanguageError as exc:
        raise LazadaBalanceError(str(exc)) from None
    if _country_from_url(driver.current_url) != country:
        raise LazadaBalanceError("未进入所选 Lazada 站点的资金页面")
    return language


_METRIC_SCRIPT = r"""
/* lazada_balance_metric */
const field = arguments[0];
const visible = el => {
  if (!el || !el.getClientRects().length) return false;
  const style = getComputedStyle(el);
  return style.display !== 'none' && style.visibility !== 'hidden' && style.visibility !== 'collapse';
};
const txt = el => String(el?.innerText || el?.textContent || '').trim();
const loading = root => Array.from(root?.querySelectorAll('.next-loading-tip, [aria-busy="true"], [class*="skeleton" i], [class*="loading-icon" i]') || []).some(visible);
if (field === 'income') {
  const card = document.querySelector('.overview-card .release');
  const label = card?.querySelector('.special-title .title-text');
  const value = card?.querySelector(':scope > .amount-of-money');
  return {label: txt(label), text: visible(value) ? txt(value) : '', located: !!card, loading: loading(card)};
}
if (field === 'balance') {
  const card = document.querySelector('[data-spm="total_balance_card"]');
  const label = card?.querySelector('.title');
  const value = card?.querySelector('.amount-wrap');
  return {label: txt(label), text: visible(value) ? txt(value) : '', located: !!card, loading: loading(card)};
}
if (field === 'ads') {
  const labels = Array.from(document.querySelectorAll('[data-i18n-key*="Available_Balance"]')).filter(visible);
  const label = labels.length === 1 ? labels[0] : null;
  const value = label?.parentElement?.parentElement?.querySelector('[class*="accountBalanceValue"]');
  return {label: txt(label), text: visible(value) ? txt(value) : '', located: !!label, loading: loading(label?.parentElement?.parentElement)};
}
return {label:'', text:'', located:false};
"""


def _metric(driver, field: str) -> str:
    path = PATHS[field].split("#!", 1)[0]
    if path not in str(getattr(driver, "current_url", "")):
        raise LazadaBalanceError(f"{FIELD_LABELS[field]} 当前页面路径不匹配")
    result = {}
    signature = ""
    consecutive = 0
    began = time.monotonic()
    stable_since = began
    deadline = began + 20
    minimum_stability = 3 if field == "ads" else 2
    while time.monotonic() < deadline:
        result = driver.execute_script(_METRIC_SCRIPT, field) or {}
        value = str(result.get("text") or "")
        if result.get("located") and not result.get("loading") and _AMOUNT_RE.search(value) and "undefined" not in value.lower():
            if signature == value:
                consecutive += 1
            else:
                signature, consecutive = value, 1
                stable_since = time.monotonic()
            if consecutive >= 3 and time.monotonic() - stable_since >= minimum_stability:
                return value
        else:
            signature, consecutive = "", 0
            stable_since = time.monotonic()
        time.sleep(0.4)
    if not result.get("located") or not signature:
        raise LazadaBalanceError(f"{FIELD_LABELS[field]} 未加载或页面字段不匹配")
    raise LazadaBalanceError(f"{FIELD_LABELS[field]} 金额持续变化，无法截图")


def _feature_unavailable(driver) -> bool:
    url = str(driver.current_url or "").lower()
    if "/apps/balance/unaccessable" in url or "/apps/balance/unavailable" in url:
        return True
    text = _page_text(driver).lower()
    return (
        "feature is in pilot phase" in text and "not accessible" in text
    ) or ("我的余额" in text and ("功能尚未开放" in text or "暂不可用" in text))


def _decimal_amount(raw: str, country: str) -> Decimal:
    text = str(raw or "").replace("\u00a0", " ").replace("−", "-")
    found = _AMOUNT_RE.findall(text)
    if len(found) != 1:
        raise LazadaBalanceError("金额缺失或存在多个候选值")
    token = re.sub(r"\s+", "", found[0])
    if not token or token in {"-", "+"}:
        raise LazadaBalanceError("金额为空")
    if country in {"ID", "VN"}:
        # Indonesian portal values may use a comma as the decimal separator;
        # Vietnamese dong commonly shows grouping dots with no decimal digits.
        if "," in token and "." in token and token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        elif token.count(".") >= 1 and token.rsplit(".", 1)[1].isdigit() and len(token.rsplit(".", 1)[1]) == 3:
            token = token.replace(".", "").replace(",", "")
        elif "," in token and len(token.rsplit(",", 1)[1]) in {1, 2}:
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    else:
        token = token.replace(",", "")
    try:
        return Decimal(token)
    except InvalidOperation as exc:
        raise LazadaBalanceError("金额格式无法识别") from exc


def _currency_from_text(raw: str, country: str) -> str:
    text = str(raw or "").upper()
    names = {
        "PHP": ("PHP", "₱", "菲律宾比索"),
        "MYR": ("MYR", "RM", "马来西亚林吉特", "马币"),
        "THB": ("THB", "฿", "泰铢"),
        "IDR": ("IDR", "RP", "印尼盾"),
        "VND": ("VND", "₫", "越南盾"),
        "SGD": ("SGD", "S$", "新加坡元"),
    }
    seen = [code for code, markers in names.items() if any(marker.upper() in text for marker in markers)]
    if any(marker in text for marker in ("USD", "US$", "美元", "美金", "CNY", "RMB", "人民币", "EUR", "€")):
        raise LazadaBalanceError("页面币种与所选 Lazada 站点不一致")
    if len(seen) != 1 or seen[0] != CURRENCIES[country]:
        raise LazadaBalanceError("页面币种与所选 Lazada 站点不一致")
    return seen[0]


_WITHDRAWAL_TYPE_LABELS = frozenset({"提现", "withdrawal", "withdraw", "rút tiền"})


_TRANSACTIONS_SCRIPT = r"""
/* lazada_balance_transactions */
const card = Array.from(document.querySelectorAll('table')).find(t =>
  t.querySelectorAll('tbody tr').length && Array.from(t.querySelectorAll('tbody tr')).some(r => r.querySelectorAll('td').length >= 6)
) || document.querySelector('table');
const filter = Array.from(document.querySelectorAll('.cap-type button.button-groups'))
  .find(b => /^(提现|Withdrawal|Withdraw|Rút tiền)$/i.test((b.innerText || '').trim()));
const rows = card ? Array.from(card.querySelectorAll('tbody tr')).map(row =>
  Array.from(row.querySelectorAll('td')).map(cell => (cell.innerText || '').trim())
).filter(cells => cells.length >= 6) : [];
const section = card?.closest('[class*="balance"], [class*="transaction"]') || card?.parentElement;
const sectionText = String(section?.innerText || '');
const bodyText = String(document.body?.innerText || '');
const totalText = String(document.querySelector('.next-pagination-total')?.innerText || '');
const inputs = Array.from(document.querySelectorAll('input[placeholder]'));
const start = inputs.find(el => /起始日期|start date|Ngày bắt đầu/i.test(el.placeholder));
const end = inputs.find(el => /结束日期|end date|Ngày kết thúc/i.test(el.placeholder));
const currentPage = document.querySelector('.next-pagination-list .next-current, .next-pagination-list [aria-current="page"]');
return {
  filter_present: !!filter,
  filter_active: !!filter && /next-btn-primary|active|selected/.test(filter.className),
  rows,
  empty: /暂无数据|没有数据|暂无记录|No Data|No Records|No transactions/i.test(sectionText)
    || (rows.length === 0 && /暂无数据|没有数据|暂无记录|No Data|No Records|No transactions/i.test(bodyText)
        && /(?:总计|Total)\s*:?\s*0\b/i.test(totalText)),
  loading: !!document.querySelector('.next-loading-tip, [aria-busy="true"]'),
  date_start: start?.value || '', date_end: end?.value || '',
  page_number: Number((currentPage?.innerText || '').trim()) || 0,
  total_count: Number((totalText.match(/\d+/) || [])[0]) || 0
};
"""


def _transaction_snapshot(driver) -> dict:
    return driver.execute_script(_TRANSACTIONS_SCRIPT) or {}


def _select_withdrawal_filter(driver) -> dict:
    candidates = []
    before = {}

    def filter_ready() -> bool:
        nonlocal candidates, before
        candidates = driver.find_elements("css selector", ".cap-type button.button-groups")
        candidates = [item for item in candidates if item.is_displayed() and item.text.strip().lower() in _WITHDRAWAL_TYPE_LABELS]
        if len(candidates) != 1:
            return False
        before = _transaction_snapshot(driver)
        if before.get("loading"):
            return False
        try:
            _verify_transaction_scope(before)
        except LazadaBalanceError:
            return False
        return True

    if not _wait_until(filter_ready, 18):
        raise LazadaBalanceError("余额流水的提现筛选或日期范围未加载")
    if not before.get("filter_active"):
        candidates[0].click()
    result = {}
    previous_signature = None
    stable = 0

    def settled() -> bool:
        nonlocal result, previous_signature, stable
        result = _transaction_snapshot(driver)
        rows = result.get("rows") or []
        valid = bool(result.get("filter_active") and not result.get("loading") and (rows or result.get("empty")) and all(str(row[2]).strip().lower() in _WITHDRAWAL_TYPE_LABELS for row in rows))
        if not valid:
            stable, previous_signature = 0, None
            return False
        signature = (tuple(tuple(row) for row in rows), result.get("empty"), result.get("total_count"))
        stable = stable + 1 if signature == previous_signature else 1
        previous_signature = signature
        return stable >= 2

    if not _wait_until(settled, 18):
        raise LazadaBalanceError("提现筛选后流水未稳定，无法判断最新提现")
    _verify_transaction_scope(result)
    if (before.get("date_start"), before.get("date_end")) != (result.get("date_start"), result.get("date_end")):
        raise LazadaBalanceError("提现筛选改变了默认日期范围")
    return result


def _verify_transaction_scope(snapshot: dict) -> None:
    if snapshot.get("page_number") != 1 and not (
        snapshot.get("page_number") == 0 and snapshot.get("empty") and snapshot.get("total_count") == 0
    ):
        raise LazadaBalanceError("余额流水未停留在第 1 页")
    try:
        start = date.fromisoformat(str(snapshot.get("date_start") or ""))
        end = date.fromisoformat(str(snapshot.get("date_end") or ""))
    except ValueError as exc:
        raise LazadaBalanceError("余额流水的日期范围不可确认") from exc
    today = date.today()
    if start > today - timedelta(days=170) or end < today - timedelta(days=2) or end < start:
        raise LazadaBalanceError("余额流水日期范围被缩小，无法确认最新提现")


def _transaction_time(raw: str) -> datetime:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    for pattern in ("%d %b %Y %H:%M:%S", "%d %B %Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    raise LazadaBalanceError("提现交易时间无法识别")


def _withdrawal_status(remarks: str) -> str:
    first = re.split(r"[|\n\r]", str(remarks or ""), maxsplit=1)[0].strip().lower()
    if re.search(r"失败|拒绝|驳回|取消|failed|rejected|cancelled|canceled", first):
        return "pending"
    # The VN balance page uses this exact status before its bank-reference
    # separator. Explanatory text below it does not determine payment status.
    if first == "đã thanh toán":
        return "success"
    if re.search(r"提现成功|提领成功|withdrawal successful|withdrawal succeeded|withdrawal completed|^success(?:ful)?$|^completed$", first):
        return "success"
    if re.search(r"发起提现|处理中|待处理|待审核|提现中|提领中|processing|pending|initiated|in progress|submitted", first):
        return "pending"
    raise LazadaBalanceError("最新提现状态未知，不能自动决定 processing")


def _latest_withdrawal(snapshot: dict, country: str) -> tuple[str | None, str]:
    rows = snapshot.get("rows") or []
    if not rows:
        if snapshot.get("empty") and snapshot.get("filter_active"):
            return None, "no_withdrawal"
        raise LazadaBalanceError("提现列表为空但未显示明确的无记录状态")
    dated = [(_transaction_time(row[1]), row) for row in rows]
    times = [item[0] for item in dated]
    if times != sorted(times, reverse=True):
        raise LazadaBalanceError("提现流水未按最新交易时间排序")
    row = dated[0][1]
    status = _withdrawal_status(row[5])
    if status == "success":
        return None, "withdrawal_success"
    amount = _decimal_amount(row[4], country)
    return format(-abs(amount), "f"), "pending"


_SCROLL_SCRIPT = r"""
/* lazada_balance_evidence_scroll */
const field = arguments[0];
const text = el => String(el?.innerText || el?.textContent || '').trim();
const adLabels = Array.from(document.querySelectorAll('[data-i18n-key*="Available_Balance"]'));
const adLabel = adLabels.length === 1 ? adLabels[0] : null;
const balanceCard = document.querySelector('[data-spm="total_balance_card"]');
const balanceCurrency = balanceCard?.querySelector('.amount-wrap .currency');
const balanceAmount = balanceCard?.querySelector('.amount-wrap .amount');
const row = Array.from(document.querySelectorAll('table tbody tr'))
  .find(el => el.querySelectorAll('td').length >= 6);
const cells = row ? Array.from(row.querySelectorAll('td')) : [];
const targets = field === 'income'
  ? [document.querySelector('.overview-card .release > .special-title'), document.querySelector('.overview-card .release > .amount-of-money')]
  : field === 'balance'
    ? [balanceCurrency, balanceAmount]
    : field === 'ads'
      ? [adLabel, adLabel?.parentElement?.parentElement?.querySelector('[class*="accountBalanceValue"]')]
      : [cells[1], cells[2], cells[4], cells[5]?.querySelector('.msg') || cells[5]];
const anchor = targets[0];
if (!anchor || targets.some(el => !el)) return false;
anchor.scrollIntoView({block:'center', inline:'nearest'});
const inside = el => {
  const rect = el.getBoundingClientRect();
  if (rect.width < 1 || rect.height < 1 || rect.left < 0 || rect.top < 0 ||
      rect.right > window.innerWidth || rect.bottom > window.innerHeight) return false;
  const points = [
    [0.5, 0.5], [0.15, 0.15], [0.85, 0.15], [0.15, 0.85], [0.85, 0.85]
  ];
  return points.every(([rx, ry]) => {
    const top = document.elementFromPoint(rect.left + rect.width * rx, rect.top + rect.height * ry);
    return !!top && (el === top || el.contains(top) || top.contains(el));
  });
};
return targets.every(inside);
"""


def _dismiss_ads_guides(driver, timeout: float = 5) -> None:
    """Close observed Ads overlays through verified native close controls."""
    from backend.services.lazada_balance_overlays import LazadaAdsOverlayError, dismiss_lazada_ads_overlays
    try:
        dismiss_lazada_ads_overlays(driver, timeout=timeout)
    except LazadaAdsOverlayError as exc:
        raise LazadaBalanceError(str(exc)) from None


def _evidence(driver, evidence_dir: Path, store: dict, field: str, stamp: str) -> str:
    store_name = str(store.get("store_name") or store.get("name") or "").strip()
    if not store_name:
        raise LazadaBalanceError("缺少紫鸟店铺名称，无法验证截图")
    maximize = getattr(driver, "maximize_window", None)
    if callable(maximize):
        try:
            maximize()
        except Exception:
            pass
    identifier = re.sub(r"[^A-Za-z0-9_-]+", "_", str(store.get("store_id") or "store"))
    path = evidence_dir / f"lazada_{identifier}_{stamp}_{field}.png"
    for attempt in range(2):
        if field == "ads":
            _dismiss_ads_guides(driver)
        stable = 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if driver.execute_script(_SCROLL_SCRIPT, field) is True:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
                if field == "ads":
                    _dismiss_ads_guides(driver, timeout=2)
            time.sleep(0.35)
        if stable < 2:
            raise LazadaBalanceError(f"{FIELD_LABELS[field]} 的标签、金额或状态未完整显示，不能截图")
        captured = capture_balance_evidence(driver, path, store_name, FIELD_LABELS[field])
        if driver.execute_script(_SCROLL_SCRIPT, field) is True:
            return captured
        if field != "ads" or attempt:
            break
    raise LazadaBalanceError(f"{FIELD_LABELS[field]} 在截图期间被遮挡或移出窗口")


def collect_lazada_balance(driver, store: dict, evidence_dir: Path, progress=None) -> dict:
    """Return verified current values and source-window evidence for one store.

    Missing/uncertain fields remain ``None`` with a field error. A successful or
    absent latest withdrawal is intentionally blank with a named exemption.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    country = _country(store.get("country")) or _country_from_url(getattr(driver, "current_url", "")) or _country_hint(store)
    result = {
        "country": country,
        "currency": CURRENCIES.get(country),
        "values": {name: None for name in ("income", "balance", "ads", "processing")},
        "evidence": {}, "evidence_exemptions": {}, "captured_at": datetime.now(timezone.utc).isoformat(),
        "notes": [], "field_errors": {}, "source_urls": {},
    }
    if not country:
        result["field_errors"] = {field: "无法确定 Lazada 站点，请选择国家" for field in result["values"]}
        return result
    maximize = getattr(driver, "maximize_window", None)
    if callable(maximize):
        try:
            maximize()
        except Exception:
            # The evidence gate below still rejects clipped or covered fields.
            pass
    crossborder = str(store.get("is_cross_border", False)).strip().lower() in {"true", "1", "yes"} or (
        "全球" in str(store.get("platform_name") or "") or "跨境" in str(store.get("store_name") or "")
    )
    login_state: dict[str, bool] = {}
    balance_available = False
    evidence_dir = Path(evidence_dir)
    for field in ("income", "balance", "ads"):
        try:
            if progress:
                progress(f"读取 Lazada {FIELD_LABELS[field]}")
            language = _open_page(driver, country, field, login_state)
            if language.get('fallback'):
                result['notes'].append(f'{FIELD_LABELS[field]}：{language["reason"]}')
            result["source_urls"][field] = _safe_url(driver.current_url)
            if field == "balance" and _feature_unavailable(driver):
                if not crossborder:
                    raise LazadaBalanceError("我的余额功能未开放；该店未标记为跨境店")
                result["values"][field] = "0.00"
                result["evidence_exemptions"][field] = "cross_border_unavailable"
                result["notes"].append("跨境店我的余额明确显示功能未开放，按流程填 0")
                continue
            value_text = _metric(driver, field)
            _currency_from_text(value_text, country)
            value = _decimal_amount(value_text, country)
            result["values"][field] = format(value, "f")
            if field == "balance":
                balance_available = True
            evidence = _evidence(driver, evidence_dir, store, field, stamp)
            check_text = _metric(driver, field)
            if _currency_from_text(check_text, country) != CURRENCIES[country] or _decimal_amount(check_text, country) != value:
                raise LazadaBalanceError(f"{FIELD_LABELS[field]} 在截图期间变化，请重试")
            result["evidence"][field] = evidence
        except Exception as exc:
            result["field_errors"][field] = str(exc) if isinstance(exc, (LazadaBalanceError, LazadaLoginError)) else f"{FIELD_LABELS[field]} 读取或截图失败"
            if login_state.get("blocked") or is_lazada_login_url(getattr(driver, "current_url", "")):
                result["field_errors"].update({remaining: "店铺尚未登录" for remaining in ("balance", "ads", "processing") if remaining not in result["field_errors"]})
                return result
    if balance_available:
        try:
            if progress:
                progress("核对 Lazada 最新提现")
            language = _open_page(driver, country, "balance", login_state)
            if language.get('fallback'):
                result['notes'].append(f'{FIELD_LABELS["processing"]}：{language["reason"]}')
            result["source_urls"]["processing"] = _safe_url(driver.current_url)
            snapshot = _select_withdrawal_filter(driver)
            value, status = _latest_withdrawal(snapshot, country)
            if status in {"withdrawal_success", "no_withdrawal"}:
                result["evidence_exemptions"]["processing"] = status
                result["notes"].append("最新提现成功，无需填 processing" if status == "withdrawal_success" else
                                       f"余额流水 {snapshot.get('date_start')} 至 {snapshot.get('date_end')} 内无提现记录")
            else:
                result["values"]["processing"] = value
                evidence = _evidence(driver, evidence_dir, store, "processing", stamp)
                verify_snapshot = _transaction_snapshot(driver)
                if not verify_snapshot.get("filter_active") or verify_snapshot.get("rows", [])[:1] != snapshot.get("rows", [])[:1]:
                    raise LazadaBalanceError("最新提现在截图期间变化，请重试")
                result["evidence"]["processing"] = evidence
        except Exception as exc:
            result["field_errors"]["processing"] = str(exc) if isinstance(exc, (LazadaBalanceError, LazadaLoginError)) else "最新提现读取或截图失败"
    else:
        result["field_errors"]["processing"] = "我的余额未验证，无法读取最新提现"
    return result
