from __future__ import annotations

import os
import re
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from main.shopee import ads_recharge_guard
from util import dingtalk_doc_api as dtd
from util.dingtalk_message_util import send_app_text_msg, send_file_dingtalk


SUCCESS_LOG_DOC_ID = ""
SUCCESS_LOG_DOC_URL = ""
SUCCESS_LOG_SHEET_NAME_FORMAT = "%Y-%m-%d"
SUCCESS_STATUSES = {"success", "corrected_success", "completed"}
SUCCESS_HEADERS = [
    "序号",
    "记录时间",
    "运行批次ID",
    "站点",
    "站点代码",
    "店铺名称",
    "充值方式",
    "币种",
    "计划充值金额",
    "实际充值金额",
    "税费类型",
    "税费金额",
    "支付总额",
    "充值前余额",
    "充值后余额",
    "余额增量",
    "成功类型",
    "支付状态",
    "操作账号",
    "支付结果消息",
    "备注",
]

_success_log_lock = threading.Lock()
_success_log_keys: set[tuple[str, str, str, str]] = set()


def is_success_status(status: Any) -> bool:
    return str(status or "").strip() in SUCCESS_STATUSES


def _safe_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _format_amount(value: Any) -> str:
    amount = _safe_decimal(value)
    if amount is None:
        return "" if value in (None, "") else str(value)
    normalized = amount.normalize()
    return format(normalized, "f")


def _balance_delta(balance_before: Any, balance_after: Any) -> str:
    before = _safe_decimal(balance_before)
    after = _safe_decimal(balance_after)
    if before is None or after is None:
        return ""
    return _format_amount(after - before)


def _extract_message_value(message: str, labels: list[str]) -> str:
    text = str(message or "")
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*[:：]\s*([^,，。;\n]+)", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _tax_type_for_site(site_code: str) -> str:
    if site_code == "id":
        return "PPN"
    if site_code == "my":
        return "GST"
    return "VAT"


def _success_type(status: str) -> str:
    if status == "corrected_success":
        return "余额复查成功"
    return "页面成功"


def _get_doc_config(pcfg: dict) -> dict:
    return pcfg.get("dingtalk", {}).get("doc_config", {}) or {}


def _get_success_doc_id(doc_config: dict) -> str:
    return str(doc_config.get("success_log_doc_id") or SUCCESS_LOG_DOC_ID).strip()


def _get_success_sheet_name(doc_config: dict, now: datetime | None = None) -> str:
    fmt = str(doc_config.get("success_log_sheet_name_format") or SUCCESS_LOG_SHEET_NAME_FORMAT)
    return (now or datetime.now()).strftime(fmt)


def _ensure_daily_success_sheet(log, access_token: str, union_id: str, doc_id: str, sheet_name: str) -> None:
    sheets = dtd.get_all_sheets(access_token, union_id, doc_id)
    if not any(item.get("sheet_name") == sheet_name for item in sheets):
        created = dtd.create_sheet(access_token, union_id, doc_id, sheet_name)
        if not created.get("sheetid"):
            raise RuntimeError(f"创建成功充值日期子表失败：{sheet_name}")
        log.info(f"已创建成功充值日期子表：{sheet_name}")

    current_headers = dtd.safe_read_range(access_token, union_id, doc_id, sheet_name, "A1:U1")
    header_row = list(current_headers[0]) if current_headers else []
    if header_row[: len(SUCCESS_HEADERS)] != SUCCESS_HEADERS:
        dtd.safe_update_range(
            access_token,
            union_id,
            doc_id,
            sheet_name,
            "A1:U1",
            [SUCCESS_HEADERS],
            number_format="@",
        )
        log.info(f"已写入成功充值表头：{sheet_name}")


def _build_success_row(
    record: dict[str, Any],
    *,
    row_number: int,
    run_id: str,
    site_code: str,
    username: str,
    recharge_mode: str,
) -> list[str]:
    status = str(record.get("status") or "").strip()
    message = str(record.get("message") or "")
    tax_type = _tax_type_for_site(site_code)
    recharge_amount = record.get("recharge_amount")
    actual_recharge_amount = _extract_message_value(message, ["脚本充值金额", "充值金额"]) or _format_amount(recharge_amount)
    tax_amount = _extract_message_value(message, [tax_type])
    payment_total = _extract_message_value(message, ["支付总额", "Payment Total", "Total"])
    site_name = ads_recharge_guard.SITE_NAMES.get(site_code, site_code)
    currency = ads_recharge_guard.SITE_CURRENCIES.get(site_code, "")
    mode_text = "自定义金额" if recharge_mode == "custom" else "按钉钉表格"

    return [
        str(row_number),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        str(run_id or ""),
        site_name,
        site_code,
        str(record.get("store_name") or ""),
        mode_text,
        currency,
        _format_amount(recharge_amount),
        _format_amount(actual_recharge_amount),
        tax_type,
        _format_amount(tax_amount),
        _format_amount(payment_total),
        _format_amount(record.get("balance_before")),
        _format_amount(record.get("balance_after")),
        _balance_delta(record.get("balance_before"), record.get("balance_after")),
        _success_type(status),
        status,
        str(username or ""),
        message,
        "",
    ]


def _existing_success_keys(access_token: str, union_id: str, doc_id: str, sheet_name: str, total_rows: int) -> set[tuple[str, str, str, str]]:
    if total_rows <= 1:
        return set()
    values = dtd.safe_read_range(access_token, union_id, doc_id, sheet_name, f"C2:J{total_rows}")
    keys = set()
    for row in values:
        run_id = str(row[0]).strip() if len(row) > 0 else ""
        site_code = str(row[2]).strip() if len(row) > 2 else ""
        store_name = str(row[3]).strip() if len(row) > 3 else ""
        actual_amount = str(row[7]).strip() if len(row) > 7 else ""
        if run_id and site_code and store_name:
            keys.add((run_id, site_code, store_name, actual_amount))
    return keys


def append_success_recharge_log(
    log,
    pcfg: dict,
    record: dict[str, Any],
    *,
    run_id: str,
    site_code: str,
    username: str,
    recharge_mode: str,
) -> str:
    status = str(record.get("status") or "").strip()
    if not is_success_status(status):
        return ""

    doc_config = _get_doc_config(pcfg)
    app_key = str(doc_config.get("app_key") or "").strip()
    app_secret = str(doc_config.get("app_secret") or "").strip()
    user_id = str(doc_config.get("app_userid") or "").strip()
    doc_id = _get_success_doc_id(doc_config)
    if not all([app_key, app_secret, user_id, doc_id]):
        log.warning(f"{record.get('store_name')}: 成功充值登记配置不完整，跳过登记")
        return ""

    sheet_name = _get_success_sheet_name(doc_config)
    actual_amount = _format_amount(
        _extract_message_value(str(record.get("message") or ""), ["脚本充值金额", "充值金额"])
        or record.get("recharge_amount")
    )
    dedupe_key = (str(run_id or ""), site_code, str(record.get("store_name") or "").strip(), actual_amount)

    with _success_log_lock:
        if dedupe_key in _success_log_keys:
            log.info(f"{record.get('store_name')}: 成功充值记录已登记，跳过重复写入")
            return sheet_name

        access_token = dtd.get_access_token(app_key, app_secret)
        union_id = dtd.get_union_id(access_token, user_id)
        _ensure_daily_success_sheet(log, access_token, union_id, doc_id, sheet_name)

        total_rows = dtd.get_sheet_row_count(access_token, union_id, doc_id, sheet_name)
        if dedupe_key in _existing_success_keys(access_token, union_id, doc_id, sheet_name, total_rows):
            _success_log_keys.add(dedupe_key)
            log.info(f"{record.get('store_name')}: 成功充值记录已存在于 {sheet_name}，跳过重复写入")
            return sheet_name

        next_row = max(total_rows + 1, 2)
        row_values = _build_success_row(
            record,
            row_number=next_row - 1,
            run_id=run_id,
            site_code=site_code,
            username=username,
            recharge_mode=recharge_mode,
        )
        dtd.safe_update_range(
            access_token,
            union_id,
            doc_id,
            sheet_name,
            f"A{next_row}:U{next_row}",
            [row_values],
            number_format="@",
        )
        _success_log_keys.add(dedupe_key)
        log.info(f"{record.get('store_name')}: 已登记成功充值记录到 {sheet_name}")
        return sheet_name


def completion_recipients(doc_config: dict, fallback_names: list[str] | None = None) -> list[str]:
    raw = str(doc_config.get("completion_recipient_names") or "").strip()
    if raw:
        return [name.strip() for name in raw.replace("，", ",").split(",") if name.strip()]
    return list(fallback_names or [])


def build_completion_summary(
    *,
    process_name: str,
    site_code: str,
    run_id: str,
    ip: str,
    start_time: datetime,
    records: list[dict[str, Any]],
    target_count: int,
    recharge_mode: str,
    success_sheet_name: str = "",
) -> str:
    success_count = sum(1 for item in records if str(item.get("status") or "") in SUCCESS_STATUSES)
    corrected_count = sum(1 for item in records if str(item.get("status") or "") == "corrected_success")
    failed_count = sum(1 for item in records if str(item.get("status") or "") == "failed")
    skipped_count = sum(1 for item in records if str(item.get("status") or "").startswith("skipped"))
    processing_count = sum(1 for item in records if str(item.get("status") or "") == "processing")
    total_amount = sum(
        ads_recharge_guard.decimal_amount(item.get("recharge_amount"))
        for item in records
        if str(item.get("status") or "") in SUCCESS_STATUSES
    )
    currency = ads_recharge_guard.SITE_CURRENCIES.get(site_code, "")
    site_name = ads_recharge_guard.SITE_NAMES.get(site_code, site_code)
    mode_text = "自定义金额" if recharge_mode == "custom" else "按钉钉表格"
    end_time = datetime.now()
    duration_seconds = round((end_time - start_time).total_seconds(), 1)
    if success_sheet_name:
        sheet_line = success_sheet_name
    elif success_count > 0:
        sheet_line = "有成功记录，但成功登记表未写入或未返回子表名"
    else:
        sheet_line = "无成功记录，未创建/写入日期子表"
    return (
        f"Shopee广告充值执行完成\n"
        f"任务：{process_name}\n"
        f"站点：{site_name}\n"
        f"运行批次：{run_id}\n"
        f"电脑IP：{ip or '-'}\n"
        f"开始时间：{start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"结束时间：{end_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"耗时秒：{duration_seconds}\n"
        f"充值方式：{mode_text}\n"
        f"总店铺数：{target_count}\n"
        f"成功：{success_count}\n"
        f"余额复查成功：{corrected_count}\n"
        f"失败：{failed_count}\n"
        f"跳过：{skipped_count}\n"
        f"处理中：{processing_count}\n"
        f"成功登记表：{sheet_line}\n"
        f"总成功充值金额：{_format_amount(total_amount)} {currency}"
    )


def send_completion_text(log, recipients: list[str], message: str) -> None:
    if not recipients:
        return
    try:
        response = send_app_text_msg(message, recipients)
        log.info(f"完成汇总已发送给 {recipients}: {response}")
    except Exception as exc:
        log.error(f"发送完成汇总失败: {exc}")


def send_completion_files(log, recipients: list[str], file_paths: list[str]) -> None:
    if not recipients:
        return
    for file_path in file_paths:
        if not file_path or not os.path.isfile(file_path) or os.path.getsize(file_path) <= 0:
            continue
        try:
            send_file_dingtalk(file_path, recipients)
            log.info(f"完成结果文件已发送给 {recipients}: {file_path}")
        except Exception as exc:
            log.error(f"发送完成结果文件失败: {file_path}: {exc}")
