"""Shared, evidence-backed preparation for ID/VN automatic ad recharges.

This module has no browser, credential or filesystem side effects on import.
The caller supplies the configured document client and balance reader.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from implement.shopee.ads_budget_guard import safe_write_ads_result, sync_verified_ads_budget
from util.hqyl_runtime import resolve_output_directory
from util.shopee_ads_money import parse_shopee_money


class AdsDataValidationError(RuntimeError):
    """The store must not proceed to automatic payment."""


def write_ads_result(*, dtd, access_token, union_id, doc_id, sheet_name,
                     row, store_name, site_code, message, log=None) -> str:
    """A log-write problem must never cause a completed payment to be retried."""
    result = safe_write_ads_result(
        dtd=dtd, access_token=access_token, union_id=union_id, doc_id=doc_id,
        sheet_name=sheet_name, row=row, store_name=store_name, site_code=site_code,
        values=[[datetime.now(timezone(timedelta(hours=7))).date().isoformat(), message]],
    )
    if result.get("written"):
        return message
    note = "表格执行结果写回未确认，请核验记录；不要据此重复充值"
    if log:
        log.warning(f"{store_name}: {note}")
    return f"{message}；{note}"


def mark_ads_pending_review(*, store_name, site_code, dtd, doc_config,
                            output_directory, log=None) -> None:
    """Best-effort invalidation when the wallet cannot be read at all."""
    try:
        sync_verified_ads_inputs(
            store_name=store_name, site_code=site_code, snapshot={}, balance=None,
            balance_text=None, dtd=dtd, doc_config=doc_config,
            output_directory=output_directory, collection_failure="WalletUnavailable",
        )
    except (AdsDataValidationError, OSError) as exc:
        if log:
            log.warning(f"{store_name}: 采集失败，禁止自动充值（{type(exc).__name__}）")


def _evidence_path(output_directory: str, site_code: str) -> Path:
    if site_code not in ("id", "vn"):
        raise AdsDataValidationError("不支持的广告采集站点")
    # Use the shared runtime markers; HQYL_CHROMEDRIVER_PATH is also a local
    # driver setting and does not, by itself, identify a managed attempt.
    root = resolve_output_directory(output_directory)
    folder = root / "ads_evidence"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{site_code}_{uuid4().hex}.json"


def _write_evidence(path: Path, value: dict) -> None:
    def encode(item):
        if isinstance(item, Decimal):
            return format(item, "f")
        raise TypeError(type(item).__name__)

    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=encode), encoding="utf-8")
    temporary.replace(path)


def sync_verified_ads_inputs(*, store_name, site_code, snapshot, balance,
                             balance_text, dtd, doc_config, output_directory,
                             log=None, collection_failure=None, collection_error_type=None) -> tuple:
    """The only document synchronization entry for automatic ID/VN budgets."""
    evidence_path = _evidence_path(output_directory, site_code)
    audit = {
        "store_name": store_name, "site_code": site_code,
        "snapshot": snapshot, "balance": balance, "balance_text": balance_text,
        "collection_failure": collection_failure,
        "collection_error_type": collection_error_type,
    }
    _write_evidence(evidence_path, audit)
    try:
        token = dtd.get_access_token(doc_config["app_key"], doc_config["app_secret"])
        union_id = dtd.get_union_id(token, doc_config["app_userid"])
        result = sync_verified_ads_budget(
            dtd=dtd, access_token=token, union_id=union_id,
            doc_id=doc_config["config_doc_id"], sheet_name=doc_config["config_sheet"],
            site_code=site_code, store_name=store_name, expense_snapshot=snapshot,
            balance=balance, balance_text=balance_text, collection_failure=collection_failure,
        )
    except Exception as exc:
        audit["sync_failure"] = type(exc).__name__
        _write_evidence(evidence_path, audit)
        raise AdsDataValidationError(f"{store_name}: 广告数据校验或写回失败，待核验（{type(exc).__name__}）") from None

    audit["budget_check"] = result
    try:
        _write_evidence(evidence_path, audit)
    except OSError:
        # A successful sheet write is insufficient if its evidence was lost.
        # Invalidate the plan without altering the collected monetary inputs.
        sync_verified_ads_budget(
            dtd=dtd, access_token=token, union_id=union_id,
            doc_id=doc_config["config_doc_id"], sheet_name=doc_config["config_sheet"],
            site_code=site_code, store_name=store_name, expense_snapshot={},
            balance=None, balance_text=None, collection_failure="EvidenceUnavailable",
        )
        raise AdsDataValidationError(f"{store_name}: 采集证据无法保存，停止自动充值。") from None
    if collection_failure or not result.get("ready"):
        reasons = result.get("reasons") or ["采集数据未通过校验"]
        message = "；".join(str(reason) for reason in reasons)
        raise AdsDataValidationError(f"{store_name}: 待核验，停止自动充值：{message}")
    if log:
        log.info(f"{store_name}: 七天消耗和预算校验通过，证据={evidence_path.name}")
    return (
        result["is_recharge"], result.get("recharge_amount"), token, union_id,
        doc_config["config_doc_id"], doc_config["config_sheet"], result["row"],
    )


def prepare_verified_ads_budget(
    *, webdriver_util, store_name: str, site_code: str,
    date_input_xpath, amount_xpath: str, read_balance,
    dtd, doc_config: dict, output_directory: str, log=None,
    collector=None,
) -> tuple:
    """Collect once, persist evidence, then reconcile the sheet before payment.

    A collection failure still reaches the sheet guard with an invalid snapshot
    so the uniquely matched row is marked pending instead of retaining a stale
    'no recharge' decision. No credentials are included in the audit record.
    """
    if collector is None:
        from implement.shopee.ads_expense_date_range import collect_previous_7_days_expense_snapshot
        collector = collect_previous_7_days_expense_snapshot

    snapshot = {}
    balance = None
    balance_text = None
    collection_failure = None
    collection_error_type = None
    phase = "ExpenseCollectionFailed"
    try:
        snapshot = collector(
            webdriver_util, store_name, date_input_xpath, log,
            amount_xpath=amount_xpath, site_code=site_code,
        )
        phase = "BalanceUnavailable"
        element, reported_balance, error = read_balance(webdriver_util, store_name, timeout=30)
        if error or element is None:
            raise AdsDataValidationError("广告余额未可靠识别")
        balance_text = str(element.text or "").strip()
        balance = parse_shopee_money(balance_text, site_code)
        if balance != parse_shopee_money(reported_balance, site_code):
            phase = "BalanceChanged"
            raise AdsDataValidationError("余额在读取期间变化")
        snapshot = dict(snapshot)
        snapshot["balance_captured_at"] = datetime.now(timezone.utc).isoformat()
        snapshot["store_name"] = store_name
    except Exception as exc:
        # Exception text from browser/document SDKs can contain session data.
        collection_failure = phase
        collection_error_type = type(exc).__name__
        snapshot = {}
        balance = None
        balance_text = None

    return sync_verified_ads_inputs(
        store_name=store_name, site_code=site_code, snapshot=snapshot,
        balance=balance, balance_text=balance_text, dtd=dtd,
        doc_config=doc_config, output_directory=output_directory,
        log=log, collection_failure=collection_failure, collection_error_type=collection_error_type,
    )
