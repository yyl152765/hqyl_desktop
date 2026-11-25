"""Verify one Shopee ads snapshot and its sheet budget before allowing payment.

The injected ``dtd`` adapter uses the existing DingTalk ``safe_*_range`` API.
This module never imports credentials, opens a browser, or makes a payment.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from util.shopee_ads_money import parse_shopee_money


SITE_TIMEZONE = timezone(timedelta(hours=7))
MAX_SNAPSHOT_AGE = timedelta(minutes=10)
MAX_CLOCK_SKEW = timedelta(seconds=30)
READBACK_ATTEMPTS = 5
READBACK_INTERVAL_SECONDS = 1.0
PARSER_GUARD_VERSION = "ads-budget-v2"
_SITES = {"vn": ("VND", -2), "id": ("IDR", -4)}
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


class AdsBudgetValidationError(ValueError):
    """A safe, actionable explanation for withholding automatic payment."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _name(value) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def _name_tokens(value) -> set[str]:
    normalized = _name(value)
    if not normalized:
        raise AdsBudgetValidationError("店铺名称为空")
    return {normalized, *(_name(part) for part in re.split(r"[-‐‑‒–—]", normalized))} - {""}


def _row(values, label: str) -> list:
    if not isinstance(values, (list, tuple)) or not values:
        raise AdsBudgetValidationError(f"无法读取{label}")
    value = values[0]
    if not isinstance(value, (list, tuple)):
        raise AdsBudgetValidationError(f"{label}格式无效")
    return list(value) + [""] * max(0, 18 - len(value))


def _sheet_number(value, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise AdsBudgetValidationError(f"{field}不是有效数值")
    text = str(value).strip()
    if not _NUMBER.fullmatch(text):
        raise AdsBudgetValidationError(f"{field}不是有效数值")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise AdsBudgetValidationError(f"{field}不是有效数值") from None
    if not number.is_finite():
        raise AdsBudgetValidationError(f"{field}不是有限数值")
    return number


def _money(value, site: str, field: str) -> Decimal:
    try:
        result = parse_shopee_money(value, site)
        result = Decimal(str(result))
    except (ValueError, TypeError, InvalidOperation):
        raise AdsBudgetValidationError(f"{field}金额格式无效") from None
    if not result.is_finite() or result < 0 or result != result.to_integral_value():
        raise AdsBudgetValidationError(f"{field}必须是非负整数货币金额")
    if result > 2**53 - 1:
        raise AdsBudgetValidationError(f"{field}超过表格可精确表示的整数范围")
    return result


def _timestamp(value, field: str) -> datetime:
    if not isinstance(value, str):
        raise AdsBudgetValidationError(f"缺少{field}时区时间")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AdsBudgetValidationError(f"{field}时间格式无效") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise AdsBudgetValidationError(f"{field}时间缺少时区")
    return result


def _snapshot(snapshot, balance, balance_text, site: str, expected_store_name: str) -> dict:
    if not isinstance(snapshot, Mapping) or not snapshot:
        raise AdsBudgetValidationError("缺少经过校验的消耗快照")
    currency, rounding = _SITES[site]
    if snapshot.get("site_code") != site or snapshot.get("currency") != currency:
        raise AdsBudgetValidationError("快照站点或币种不匹配")
    if "store_name" in snapshot and expected_store_name not in _name_tokens(snapshot["store_name"]):
        raise AdsBudgetValidationError("消耗快照店铺与目标店铺不匹配")
    if snapshot.get("timezone") != "GMT+7":
        raise AdsBudgetValidationError("快照统计时区不是GMT+7")
    if not isinstance(snapshot.get("parser_version"), str) or not snapshot["parser_version"].strip():
        raise AdsBudgetValidationError("快照缺少金额解析版本")
    evidence = snapshot.get("refresh_evidence")
    if not isinstance(evidence, Mapping) or evidence.get("verified") is not True:
        raise AdsBudgetValidationError("快照缺少目标统计窗口的刷新确认")
    collected = _timestamp(snapshot.get("collected_at"), "消耗采集")
    balance_time = _timestamp(snapshot.get("balance_captured_at"), "余额采集")
    now = _now()
    for value, label in ((collected, "消耗"), (balance_time, "余额")):
        if now - value > MAX_SNAPSHOT_AGE or value - now > MAX_CLOCK_SKEW:
            raise AdsBudgetValidationError(f"{label}采集时间已过期或位于未来")
    if balance_time + MAX_CLOCK_SKEW < collected:
        raise AdsBudgetValidationError("余额不是消耗采集后同次获取的数据")
    try:
        start = date.fromisoformat(snapshot.get("start_date", ""))
        end = date.fromisoformat(snapshot.get("end_date", ""))
    except (ValueError, TypeError):
        raise AdsBudgetValidationError("统计起止日期无效") from None
    expected_end = collected.astimezone(SITE_TIMEZONE).date() - timedelta(days=1)
    if end != expected_end or start != end - timedelta(days=6):
        raise AdsBudgetValidationError("消耗窗口不是采集时点之前七个完整自然日")
    amount = _money(snapshot.get("amount"), site, "七天消耗")
    raw_amount = _money(snapshot.get("text"), site, "七天消耗原文")
    if amount != raw_amount:
        raise AdsBudgetValidationError("消耗原文与快照金额不一致")
    actual_balance = _money(balance, site, "广告余额")
    raw_balance = balance_text if balance_text is not None else snapshot.get("balance_text")
    if actual_balance != _money(raw_balance, site, "广告余额原文"):
        raise AdsBudgetValidationError("余额原文与余额金额不一致")
    daily = amount / Decimal(7)
    budget = (daily * 10).quantize(Decimal(1).scaleb(-rounding), rounding=ROUND_HALF_UP)
    signed_recharge = (budget - actual_balance).quantize(Decimal("1E4"), rounding=ROUND_HALF_UP)
    return {
        "amount": amount,
        "daily": daily,
        "budget": budget,
        "balance": actual_balance,
        "signed_recharge": signed_recharge,
        "recharge": max(Decimal(0), signed_recharge),
        "date": collected.astimezone(SITE_TIMEZONE).date().isoformat(),
        "rounding": rounding,
        "currency": currency,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }


def _formula(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _recharge_formula_matches(value, row_number: int) -> bool:
    normalized = _formula(value)
    return normalized in {
        f"=ROUND(F{row_number}-N{row_number},-4)",
        f"=ROUND((F{row_number}-N{row_number}),-4)",
    }


def sync_verified_ads_budget(
    *, dtd, access_token, union_id, doc_id, sheet_name, site_code,
    store_name, expense_snapshot, balance, balance_text=None, collection_failure=None,
) -> dict:
    """Write verified inputs and release only a fully reconciled automatic plan.

    Any error returns ``is_recharge=False, recharge_amount=None`` and, when a
    unique same-site row remains identifiable, leaves its status at 待核验.
    Manual O values are preserved and always require explicit human review.
    Retired G:M tier columns and threshold tables do not participate in payment.
    """
    result = {
        "row": None, "is_recharge": False, "recharge_amount": None,
        "ready": False, "needs_review": True, "status": "待核验",
        "reasons": [], "audit": {"guard_version": PARSER_GUARD_VERSION},
    }
    credentials = (access_token, union_id, doc_id, sheet_name)
    site = str(site_code).strip().lower()
    matched_name = None
    row_number = None

    def read(cell_range, select="values"):
        return dtd.safe_read_range(*credentials, cell_range, select=select)

    def current_row(select="values"):
        return _row(read(f"A{row_number}:R{row_number}", select), "店铺行")

    def assert_identity(values):
        if _name(values[0]) != matched_name or str(values[2]).strip().lower() != site:
            raise AdsBudgetValidationError("店铺行或站点在写入期间发生变化")

    def write(cell_range, values, number_format="@"):
        assert_identity(current_row())
        dtd.safe_update_range(*credentials, cell_range, values, number_format=number_format)

    def pending(reason):
        if row_number is not None:
            try:
                write(f"P{row_number}:P{row_number}", [["待核验"]])
                write(f"R{row_number}:R{row_number}", [[f"待核验：{reason}"]])
            except Exception:
                result["status_write_failed"] = True

    try:
        if site not in _SITES:
            raise AdsBudgetValidationError("只支持越南和印尼广告预算")
        tokens = _name_tokens(store_name)
        total_rows = dtd.get_sheet_row_count(*credentials)
        if isinstance(total_rows, bool) or not isinstance(total_rows, int) or total_rows < 2:
            raise AdsBudgetValidationError("配置表没有有效店铺行")
        values = read(f"A2:R{total_rows}")
        formulas = read(f"A2:R{total_rows}", "formulas")
        if not isinstance(values, (list, tuple)) or not isinstance(formulas, (list, tuple)):
            raise AdsBudgetValidationError("无法读取配置表数值或公式")
        matches = []
        for index, candidate in enumerate(values):
            if isinstance(candidate, (list, tuple)) and candidate and _name(candidate[0]) in tokens:
                matches.append((index + 2, list(candidate) + [""] * max(0, 18 - len(candidate))))
        if len(matches) != 1:
            raise AdsBudgetValidationError("店铺名称没有唯一精确匹配的配置行")
        candidate_row, initial = matches[0]
        if str(initial[2]).strip().lower() != site:
            raise AdsBudgetValidationError("配置表店铺站点与采集站点不一致")
        matched_name = _name(initial[0])
        row_number = candidate_row
        result["row"] = row_number
        result["audit"].update(store_name=matched_name, site_code=site, row=row_number)
        if len(formulas) <= row_number - 2:
            raise AdsBudgetValidationError("无法读取店铺公式，禁止使用未知人工覆盖")
        initial_formulas = _row([formulas[row_number - 2]], "店铺公式")
        write(f"P{row_number}:P{row_number}", [["待核验"]])
        if current_row()[15] != "待核验":
            raise AdsBudgetValidationError("无法确认充值已进入待核验状态")
        if collection_failure:
            reason = {
                "ExpenseCollectionFailed": "七天广告消耗采集失败，未取得目标日期的商品广告报表与页面金额一致的证据",
                "WalletUnavailable": "广告钱包余额未可靠识别，请检查广告页加载和余额显示",
                "BalanceUnavailable": "七天消耗采集后的广告余额未可靠识别",
                "BalanceChanged": "广告余额在读取期间发生变化，请重新采集核验",
                "EvidenceUnavailable": "采集证据无法保存，禁止自动充值",
            }.get(collection_failure, "广告数据采集失败，未取得有效快照")
            raise AdsBudgetValidationError(reason)
        plan = _snapshot(expense_snapshot, balance, balance_text, site, matched_name)
        daily_formula = f"=D{row_number}/7"
        budget_formula = f"=ROUND(E{row_number}*10,{plan['rounding']})"
        result.update(local_budget=int(plan["budget"]), expected_recharge=int(plan["recharge"]))
        result["audit"].update({
            "store_name": matched_name, "site_code": site, "currency": plan["currency"],
            "row": row_number, "start_date": plan["start_date"], "end_date": plan["end_date"],
            "timezone": "GMT+7", "collected_at": expense_snapshot["collected_at"],
            "balance_captured_at": expense_snapshot["balance_captured_at"],
            "expense_text": expense_snapshot["text"], "balance_text": balance_text if balance_text is not None else expense_snapshot.get("balance_text"),
            "expense": str(plan["amount"]), "balance": str(plan["balance"]),
            "budget": str(plan["budget"]), "signed_recharge": str(plan["signed_recharge"]),
            "recharge": str(plan["recharge"]), "parser_version": expense_snapshot["parser_version"],
            "refresh_evidence": dict(expense_snapshot["refresh_evidence"]),
        })
        manual_override = not str(initial_formulas[14] or "").startswith("=")
        result["audit"]["manual_override"] = manual_override
        # O/P are deliberately not filled together with N: O may contain a
        # human decision and P must remain blocked throughout the update.
        # DingTalk UpdateRangeRequest requires List[List[str]], including
        # numeric cells. number_format keeps these strings numeric in-sheet;
        # reconcile still checks their actual numeric values independently.
        write(f"D{row_number}:F{row_number}", [[format(plan["amount"], "f"), daily_formula, budget_formula]], "0.###############")
        write(f"N{row_number}:N{row_number}", [[format(plan["balance"], "f")]], "0")
        write(f"Q{row_number}:Q{row_number}", [[plan["date"]]])

        def reconcile():
            checked = current_row()
            assert_identity(checked)
            checked_formulas = current_row("formulas")
            if _formula(checked_formulas[4]) != daily_formula or _formula(checked_formulas[5]) != budget_formula:
                raise AdsBudgetValidationError("七天日均或十天预算公式读回不一致")
            for column, expected, label in ((3, plan["amount"], "七天消耗"), (5, plan["budget"], "十天预算"), (13, plan["balance"], "广告余额")):
                if _sheet_number(checked[column], label) != expected:
                    raise AdsBudgetValidationError(f"{label}写入读回或重新计算未完成")
            if abs(_sheet_number(checked[4], "日均消耗") - plan["daily"]) > max(Decimal("0.000001"), abs(plan["daily"]) * Decimal("1E-12")):
                raise AdsBudgetValidationError("日均消耗计算与本地结果不一致")
            if str(checked[16]).lstrip("'") != plan["date"]:
                raise AdsBudgetValidationError("采集日期写入读回不一致")
            if _formula(checked_formulas[14]) != _formula(initial_formulas[14]):
                raise AdsBudgetValidationError("充值金额公式或人工覆盖在写入期间发生变化")
            if manual_override:
                if checked[14] != initial[14]:
                    raise AdsBudgetValidationError("人工充值金额在写入期间发生变化")
            elif not _recharge_formula_matches(checked_formulas[14], row_number):
                raise AdsBudgetValidationError("充值金额公式不是受支持的余额差额规则")
            elif _sheet_number(checked[14], "表格充值金额") != plan["signed_recharge"]:
                raise AdsBudgetValidationError("表格充值金额与本地重算不一致")
            return checked

        for attempt in range(READBACK_ATTEMPTS):
            try:
                reconcile()
                break
            except AdsBudgetValidationError:
                if attempt + 1 == READBACK_ATTEMPTS:
                    raise
                time.sleep(READBACK_INTERVAL_SECONDS)
        if manual_override:
            raise AdsBudgetValidationError("O列存在人工充值金额或缺少公式，需显式人工审核后才可付款")
        # Revalidate age after all remote sheet operations and re-read after
        # changing P: a partial write or concurrent manual edit cannot pay.
        _snapshot(expense_snapshot, balance, balance_text, site, matched_name)
        status = "需充值" if plan["recharge"] > 0 else "无需充值"
        summary = f"已核验：{plan['start_date']}至{plan['end_date']} GMT+7；七天消耗{plan['amount']} {plan['currency']}；十天预算{int(plan['budget'])}"
        write(f"R{row_number}:R{row_number}", [[summary]])
        write(f"P{row_number}:P{row_number}", [[status]])
        final = reconcile()
        if final[15] != status:
            raise AdsBudgetValidationError("充值放行状态写入读回不一致")
        _snapshot(expense_snapshot, balance, balance_text, site, matched_name)
        result.update(ready=True, needs_review=False, status=status, is_recharge=plan["recharge"] > 0, recharge_amount=int(plan["recharge"]))
    except AdsBudgetValidationError as exc:
        result["reasons"].append(str(exc))
        pending(str(exc))
    except Exception as exc:
        # Adapter errors may contain request headers: expose only their type.
        reason = f"广告预算同步失败（{type(exc).__name__}），禁止自动充值"
        result["reasons"].append(reason)
        pending(reason)
    result["audit"].update(status=result["status"], ready=result["ready"], reasons=list(result["reasons"]))
    return result


def safe_write_ads_result(
    *, dtd, access_token, union_id, doc_id, sheet_name, row,
    store_name, site_code, values,
) -> dict:
    """Write payment Q:R once after resolving the current unique shop row.

    ``row`` is a previously verified row hint, not permission to write that
    index blindly. A sort before this call is handled by fresh resolution.
    A sort/edit during the write is reported as an uncertain write and is never
    followed by another write or payment. The sheet API has no conditional
    update primitive, so callers must not treat this check as a sheet lock.
    """
    result = {
        "written": False, "row": None, "relocated": False,
        "needs_review": True, "reasons": [],
    }
    credentials = (access_token, union_id, doc_id, sheet_name)
    site = str(site_code).strip().lower()

    def read(cell_range):
        return dtd.safe_read_range(*credentials, cell_range, select="values")

    def resolve(tokens):
        total = dtd.get_sheet_row_count(*credentials)
        if isinstance(total, bool) or not isinstance(total, int) or total < 2:
            raise AdsBudgetValidationError("结果写回时配置表没有有效店铺行")
        sheet = read(f"A2:C{total}")
        if not isinstance(sheet, (list, tuple)):
            raise AdsBudgetValidationError("结果写回时无法读取店铺身份")
        matches = []
        for number, candidate in enumerate(sheet, 2):
            if isinstance(candidate, (list, tuple)) and candidate and _name(candidate[0]) in tokens:
                matches.append((number, list(candidate) + [""] * max(0, 3 - len(candidate))))
        if len(matches) != 1:
            raise AdsBudgetValidationError("结果写回时店铺没有唯一精确匹配行")
        number, candidate = matches[0]
        if str(candidate[2]).strip().lower() != site:
            raise AdsBudgetValidationError("结果写回时店铺站点不匹配")
        return number, _name(candidate[0])

    try:
        if site not in _SITES:
            raise AdsBudgetValidationError("结果写回只支持越南和印尼站点")
        if isinstance(row, bool) or not isinstance(row, int) or row < 2:
            raise AdsBudgetValidationError("缺少之前经过核验的店铺行号")
        if (not isinstance(values, (list, tuple)) or len(values) != 1
                or not isinstance(values[0], (list, tuple)) or len(values[0]) != 2
                or any(not isinstance(value, str) or not value.strip() for value in values[0])):
            raise AdsBudgetValidationError("充值结果必须是单行日期及状态文本")
        expected_date = values[0][0].lstrip("'")
        try:
            date.fromisoformat(expected_date)
        except ValueError:
            raise AdsBudgetValidationError("充值结果日期格式无效") from None
        tokens = _name_tokens(store_name)
        current, canonical_name = resolve(tokens)
        result.update(row=current, relocated=current != row)
        identity = _row(read(f"A{current}:C{current}"), "结果写回店铺身份")
        if _name(identity[0]) != canonical_name or str(identity[2]).strip().lower() != site:
            raise AdsBudgetValidationError("结果写回前店铺行已移动，未执行写入")
        result["write_attempted"] = True
        dtd.safe_update_range(*credentials, f"Q{current}:R{current}", [list(values[0])], number_format="@")
        observed, observed_name = resolve(tokens)
        result["observed_row"] = observed
        if observed != current or observed_name != canonical_name:
            raise AdsBudgetValidationError("结果写回期间店铺行已移动，写回结果待人工核验；不得重试付款")
        written = _row(read(f"Q{current}:R{current}"), "充值结果")
        identity_after = _row(read(f"A{current}:C{current}"), "结果写回店铺身份")
        if _name(identity_after[0]) != canonical_name or str(identity_after[2]).strip().lower() != site:
            raise AdsBudgetValidationError("结果读回期间店铺行已移动，写回结果待人工核验；不得重试付款")
        if str(written[0]).lstrip("'") != expected_date or written[1] != values[0][1]:
            raise AdsBudgetValidationError("充值结果写入读回不一致，需人工核验；不得重试付款")
        result.update(written=True, needs_review=False)
    except AdsBudgetValidationError as exc:
        result["reasons"].append(str(exc))
    except Exception as exc:
        result["reasons"].append(f"充值结果写回失败（{type(exc).__name__}），需人工核验；不得重试付款")
    return result
