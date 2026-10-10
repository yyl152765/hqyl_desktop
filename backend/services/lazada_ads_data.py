"""Collect Thai Lazada advertising/performance into a selected DingTalk sheet.

Credentials are supplied by the application bridge. Browser, sheet and page
actions are injectable so the complete runner can be exercised offline.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from backend.services.lazada_ads_page import (
    PlaywrightLazadaAdsPageActions, ShopTask, WorkflowError, decimal_text,
    is_pending_value, parse_amount, values_equal,
)
from backend.services.lazada_monthly_report import ZiniaoPlaywrightRuntime, safe_store_filename

DEFAULT_WORKBOOK_ID = "oP0MALyR8k77eL4OhNLR1j4N83bzYmDO"
_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True)
class LazadaAdsDataQuery:
    company: str
    username: str
    password: str = field(repr=False)
    sheet_name: str
    target_date: str
    store_names: tuple[str, ...]
    dingtalk_app_key: str = field(repr=False)
    dingtalk_app_secret: str = field(repr=False)
    dingtalk_user_id: str = field(repr=False)
    workbook_id: str = DEFAULT_WORKBOOK_ID
    browser_window_mode: str = "normal"
    client_path: str = ""
    webdriver_path: str = ""
    output_root: str = ""
    socket_port: int = 16851


def default_sheet_name(today: date | None = None) -> str:
    current = today or date.today()
    return f"{current:%y}年{current.month}月"


def default_target_date(today: date | None = None) -> str:
    return (today or date.today()).isoformat()


def normalize_workbook_id(value: Any) -> str:
    text = str(value or DEFAULT_WORKBOOK_ID).strip()
    if not text.lower().startswith(("https://", "http://")):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
            raise ValueError("钉钉工作簿 ID 格式无效")
        return text
    parsed = urlsplit(text)
    query = {key.lower(): item for key, item in parse_qs(parsed.query).items()}
    candidate = (query.get("dockey") or [""])[0].strip()
    if not candidate:
        match = re.search(r"/(?:spreadsheetv2|i/nodes)/([^/?#]+)", parsed.path, re.I)
        candidate = match.group(1) if match else ""
    if not candidate:
        candidate = (query.get("dentrykey") or [""])[0].strip()
    if not candidate or not re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
        raise ValueError("钉钉表格链接中未找到有效的工作簿 ID")
    return candidate


def _identity(value: Any) -> str:
    # Exact names after width/case/outer-whitespace normalization: no substring,
    # punctuation removal or fuzzy alias can accidentally select a different shop.
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def validate_lazada_ads_payload(
    payload: dict[str, Any], *, default_output_root: str = "", today: date | None = None,
) -> LazadaAdsDataQuery:
    request = dict(payload or {})
    current = today or date.today()
    username = str(request.get("username") or "").strip()
    password = str(request.get("password") or "")
    if not username or not password:
        raise ValueError("请先绑定并选择紫鸟账号")
    credentials = {
        name: str(request.get(name) or "").strip()
        for name in ("dingtalk_app_key", "dingtalk_app_secret", "dingtalk_user_id")
    }
    if not all(credentials.values()):
        raise ValueError("请在设置中配置钉钉应用凭证和操作人")
    raw_date = str(request.get("target_date") or "").strip() or default_target_date(current)
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
            raise ValueError
        target = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise ValueError("指定日期格式必须为 YYYY-MM-DD，且为有效日期") from exc
    if target > current:
        raise ValueError("指定日期不能晚于今天")
    sheet_name = str(request.get("sheet_name") or "").strip() or default_sheet_name(current)
    if not sheet_name or len(sheet_name) > 100 or any(ord(ch) < 32 for ch in sheet_name):
        raise ValueError("指定 Sheet 名称无效")
    raw_names = request.get("store_names") or []
    if isinstance(raw_names, str):
        raw_names = raw_names.splitlines()
    if not isinstance(raw_names, (list, tuple)):
        raise ValueError("请输入店铺名称，每行一个")
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_names:
        name = str(raw_name or "").strip()
        key = _identity(name)
        if key and key not in seen:
            names.append(name)
            seen.add(key)
    if not names:
        raise ValueError("请输入店铺名称，每行一个")
    window_mode = str(request.get("browser_window_mode") or "normal").strip().lower()
    if window_mode not in {"normal", "background"}:
        raise ValueError("浏览器窗口模式无效")
    try:
        socket_port = int(request.get("socket_port") or 16851)
    except (TypeError, ValueError) as exc:
        raise ValueError("紫鸟端口必须为整数") from exc
    if not 1 <= socket_port <= 65535:
        raise ValueError("紫鸟端口必须在 1～65535 之间")
    return LazadaAdsDataQuery(
        company=str(request.get("company") or "").strip(), username=username,
        password=password, sheet_name=sheet_name, target_date=target.isoformat(),
        store_names=tuple(names), workbook_id=normalize_workbook_id(request.get("workbook_id")),
        browser_window_mode=window_mode,
        client_path=str(request.get("client_path") or "").strip(),
        webdriver_path=str(request.get("webdriver_path") or "").strip(),
        output_root=str(request.get("output_root") or request.get("output_dir") or default_output_root).strip(),
        socket_port=socket_port, **credentials,
    )


def excel_column(number: int) -> str:
    if number < 1:
        raise ValueError("Excel 列号必须大于 0")
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def column_layout(target: date) -> tuple[str, str]:
    return excel_column(target.day * 3 + 10), excel_column(target.day * 3 + 11)


def _first_value(rows: Any) -> Any:
    return rows[0][0] if rows and rows[0] else None


def verify_sheet_layout(sheet: Any, target: date) -> None:
    advertising, performance = column_layout(target)
    headers = sheet.read(f"{advertising}1:{performance}2")
    raw = _first_value(headers)
    try:
        if isinstance(raw, datetime):
            actual = raw.date()
        elif isinstance(raw, date):
            actual = raw
        elif re.fullmatch(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", str(raw or "")):
            parts = re.split(r"[-/.]", str(raw))
            actual = date(*(int(part) for part in parts))
        else:
            actual = date(1899, 12, 30) + timedelta(days=int(float(raw)))
    except (TypeError, ValueError, OverflowError) as exc:
        raise WorkflowError("指定 Sheet 的日期表头无法识别，已停止写入") from exc
    labels = [str(value).strip() for value in headers[1][:2]] if len(headers) > 1 else []
    if actual != target or labels != ["广告费", "业绩"]:
        raise WorkflowError(f"指定 Sheet 表头不匹配：目标日期 {target}，实际 {actual}，指标 {labels}")


class DingTalkAdsSheet:
    """Legacy doc_1_0 adapter using per-run tokens from configured credentials."""
    def __init__(self, query: LazadaAdsDataQuery):
        from backend.core import dingtalk_workbook as workbook
        self.query = query
        self.access_token = ""
        self.operator_id = ""
        self.expires_at = 0.0
        self._refresh_token()
        meta = workbook.get_sheet_meta_by_name(
            self.access_token, self.operator_id, query.workbook_id, query.sheet_name,
            app_key=query.dingtalk_app_key, app_secret=query.dingtalk_app_secret,
        )
        if not meta or not meta.get("id"):
            raise WorkflowError(f"钉钉工作簿中没有指定 Sheet：{query.sheet_name}")
        self.sheet_id = str(meta["id"])
        # doc_1_0 returns a zero-based index; A1 reads use one-based row numbers.
        last_row_index = meta.get("lastNonEmptyRow")
        self.last_non_empty_row = int(last_row_index) + 1 if last_row_index is not None else 0

    def _refresh_token(self) -> None:
        import httpx
        if self.access_token and time.time() < self.expires_at - 60:
            return
        response = httpx.get(
            "https://oapi.dingtalk.com/gettoken",
            params={"appkey": self.query.dingtalk_app_key, "appsecret": self.query.dingtalk_app_secret},
            timeout=20,
        )
        if response.status_code != 200:
            raise WorkflowError(f"获取钉钉应用令牌失败（HTTP {response.status_code}）")
        data = response.json()
        if data.get("errcode") != 0 or not data.get("access_token"):
            raise WorkflowError("获取钉钉应用令牌失败，请检查设置中的应用凭证")
        self.access_token = str(data["access_token"])
        self.expires_at = time.time() + int(data.get("expires_in") or 7200)
        response = httpx.post(
            "https://oapi.dingtalk.com/topapi/v2/user/get",
            params={"access_token": self.access_token},
            json={"userid": self.query.dingtalk_user_id, "language": "zh_CN"}, timeout=20,
        )
        if response.status_code != 200:
            raise WorkflowError(f"读取钉钉操作人失败（HTTP {response.status_code}）")
        data = response.json()
        self.operator_id = str((data.get("result") or {}).get("unionid") or "")
        if data.get("errcode") != 0 or not self.operator_id:
            raise WorkflowError("无法获取钉钉操作人 unionId，请检查设置中的操作人")

    def read(self, cell_range: str) -> list[list[Any]]:
        from backend.core.dingtalk_workbook import read_sheet_range
        self._refresh_token()
        return read_sheet_range(
            self.access_token, self.operator_id, self.query.workbook_id, self.sheet_id, cell_range,
            app_key=self.query.dingtalk_app_key, app_secret=self.query.dingtalk_app_secret,
        )

    def update(self, cell_range: str, values: list[list[str]]) -> None:
        from backend.core.dingtalk_workbook import _get_sdk_client, _runtime_options
        from alibabacloud_dingtalk.doc_1_0 import models
        self._refresh_token()
        client = _get_sdk_client(self.query.dingtalk_app_key, self.query.dingtalk_app_secret)
        client.update_range_with_options(
            self.query.workbook_id, self.sheet_id, cell_range,
            models.UpdateRangeRequest(operator_id=self.operator_id, values=values, number_format="General"),
            models.UpdateRangeHeaders(x_acs_dingtalk_access_token=self.access_token), _runtime_options(),
        )


def _sheet_rows(sheet: Any) -> dict[str, list[tuple[int, str]]]:
    index: dict[str, list[tuple[int, str]]] = {}
    for start in range(3, sheet.last_non_empty_row + 1, 400):
        end = min(start + 399, sheet.last_non_empty_row)
        for offset, row in enumerate(sheet.read(f"K{start}:K{end}") or []):
            name = str(row[0] or "").strip() if row else ""
            if name:
                index.setdefault(_identity(name), []).append((start + offset, name))
    return index


def _write_pending_cell(sheet: Any, task: ShopTask, target: date, cell: str, value: Decimal) -> bool:
    with _WRITE_LOCK:
        verify_sheet_layout(sheet, target)
        if _identity(_first_value(sheet.read(f"K{task.row}:K{task.row}"))) != _identity(task.shop_name):
            raise WorkflowError("钉钉店铺行已发生变化，已停止写入")
        if not is_pending_value(_first_value(sheet.read(cell))):
            return False
        sheet.update(cell, [[decimal_text(value)]])
        # A write is never retried: after an uncertain response a fresh run reads
        # the destination again, rather than potentially overwriting newer data.
        for attempt in range(3):
            actual = _first_value(sheet.read(cell))
            if values_equal(actual, value):
                return True
            if attempt < 2:
                time.sleep(0.5)
        raise WorkflowError(f"{cell} 写入后回读不一致，请核对表格")


def _redact(value: Any, query: LazadaAdsDataQuery) -> str:
    text = str(value)
    for secret in (query.password, query.dingtalk_app_secret, query.dingtalk_app_key, query.dingtalk_user_id):
        if secret:
            text = text.replace(secret, "[已隐藏]")
    text = re.sub(r"(?i)((?:access[_-]?token|x-acs-dingtalk-access-token|appsecret)[\"']?\s*[=:]\s*[\"']?)[^\s&\"'<>]+", r"\1[已隐藏]", text)
    return text


class LazadaAdsBrowserRuntime(ZiniaoPlaywrightRuntime):
    def close_browser(self, opened: Any) -> None:
        errors = []
        if opened.connection is not None:
            try:
                opened.connection.close()
            except Exception:
                errors.append("关闭 Playwright 连接失败")
        if self.client is not None and opened.browser_oauth:
            try:
                self.client.close_browser(opened.browser_oauth)
            except Exception:
                errors.append("关闭紫鸟店铺失败")
        if errors:
            raise WorkflowError("；".join(errors))


def run_lazada_ads_data(
    query: LazadaAdsDataQuery, progress: Callable[[str], None] | None = None, *,
    runtime_factory: Callable[..., Any] | None = None,
    sheet_factory: Callable[..., Any] | None = None,
    page_actions: Any = None,
) -> dict[str, Any]:
    root = Path(query.output_root or Path.cwd() / "outputs")
    run_dir = root / "lazada_ads_data" / f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_file = run_dir / "run.log"
    result_file = run_dir / "result.json"
    logger = logging.getLogger(f"lazada_ads_data.{uuid.uuid4().hex}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    class ProgressHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            message = _redact(record.getMessage(), query)
            with log_file.open("a", encoding="utf-8") as stream:
                stream.write(f"{datetime.now():%H:%M:%S} {message}\n")
            if progress:
                progress(message)

    handler = ProgressHandler()
    logger.addHandler(handler)
    results: list[dict[str, Any]] = []
    runtime = None
    matched_count = 0
    target = date.fromisoformat(query.target_date)
    advertising_col, performance_col = column_layout(target)
    actions = page_actions or PlaywrightLazadaAdsPageActions(logger)
    try:
        logger.info("开始泰国 Lazada 广告数据：Sheet=%s，日期=%s，店铺=%s", query.sheet_name, query.target_date, len(query.store_names))
        logger.info("连接钉钉工作簿：%s，Sheet=%s", query.workbook_id, query.sheet_name)
        sheet = (sheet_factory or DingTalkAdsSheet)(query)
        verify_sheet_layout(sheet, target)
        rows = _sheet_rows(sheet)
        pending: list[tuple[dict[str, Any], ShopTask]] = []
        for requested in query.store_names:
            result = {
                "requested_store_name": requested, "store_name": requested, "row": None,
                "status": "failed", "message": "", "advertising": "", "performance": "", "written_cells": [],
            }
            results.append(result)
            candidates = rows.get(_identity(requested), [])
            if len(candidates) != 1:
                result["message"] = "钉钉 K 列未精确匹配店铺" if not candidates else "钉钉 K 列店铺名重复，无法确定写入行"
                logger.warning("%s：%s", requested, result["message"])
                continue
            row_number, name = candidates[0]
            result.update(row=row_number, store_name=name)
            values = sheet.read(f"{advertising_col}{row_number}:{performance_col}{row_number}")
            values = values[0] if values else []
            advertising = values[0] if values else None
            performance = values[1] if len(values) > 1 else None
            task = ShopTask(row_number, name, existing_advertising=advertising, existing_performance=performance,
                            need_advertising=is_pending_value(advertising), need_performance=is_pending_value(performance))
            result.update(advertising=advertising if advertising is not None else "", performance=performance if performance is not None else "")
            if not task.need_advertising and not task.need_performance:
                result.update(status="skipped", message="广告费和业绩已有值，已跳过")
                logger.info("%s：%s", name, result["message"])
            else:
                pending.append((result, task))
        if pending:
            runtime = (runtime_factory or LazadaAdsBrowserRuntime)(query, logger)
            runtime.start()
            browser_index: dict[str, list[dict[str, Any]]] = {}
            for browser in runtime.list_browsers():
                name = str(browser.get("browserName") or browser.get("name") or "").strip()
                if name:
                    browser_index.setdefault(_identity(name), []).append(browser)
            for result, task in pending:
                browsers = browser_index.get(_identity(task.shop_name), [])
                if len(browsers) != 1:
                    result["message"] = "紫鸟未精确匹配店铺" if not browsers else "紫鸟存在多个同名店铺，已停止采集"
                    logger.warning("%s：%s", task.shop_name, result["message"])
                    continue
                matched_count += 1
                opened = None
                try:
                    logger.info("%s：打开店铺，采集 %s 数据", task.shop_name, target)
                    opened = runtime.open_browser(browsers[0], run_dir / "_browser_downloads" / safe_store_filename(task.shop_name))
                    metrics = actions.collect(opened.page, task, target)
                    errors = [_redact(error, query) for error in metrics.get("errors", [])]
                    for key, needed, col in (("advertising", task.need_advertising, advertising_col), ("performance", task.need_performance, performance_col)):
                        if not needed:
                            continue
                        raw = metrics.get(key)
                        if raw is None:
                            if not errors:
                                errors.append(f"{key} 未取得有效数据")
                            continue
                        value = parse_amount(raw)
                        if not value.is_finite():
                            raise WorkflowError("采集金额不是有效数字")
                        result[key] = decimal_text(value)
                        cell = f"{col}{task.row}:{col}{task.row}"
                        if _write_pending_cell(sheet, task, target, cell, value):
                            result["written_cells"].append(cell)
                            logger.info("%s：%s 写入并回读验证通过", task.shop_name, cell)
                        else:
                            result[key] = _first_value(sheet.read(cell))
                            logger.info("%s：%s 已有值，保留现有数据", task.shop_name, cell)
                    result.update(status="failed" if errors else "success", message="；".join(errors) if errors else "采集完成，待填数据已回读验证")
                except Exception as exc:
                    result.update(status="failed", message=_redact(exc, query))
                finally:
                    if opened is not None:
                        try:
                            runtime.close_browser(opened)
                        except Exception as exc:
                            result.update(status="failed", message=f"{result['message']}；关闭店铺失败：{_redact(exc, query)}")
                            raise WorkflowError("上一店铺未完成关闭，已停止后续店铺") from exc
                    logger.info("%s：%s", task.shop_name, result["message"])
    except Exception as exc:
        message = _redact(exc, query)
        if getattr(exc, "code", "") == "invalidRequest.resource.notFound" or "invalidRequest.resource.notFound" in message:
            message = (
                f"钉钉未找到请求的资源（工作簿 ID={query.workbook_id}，Sheet={query.sheet_name}），"
                f"请核对链接和操作人访问权限。原始错误：{message}"
            )
        logger.error("任务失败：%s", message)
        by_name = {item["requested_store_name"]: item for item in results}
        for requested in query.store_names:
            if requested not in by_name:
                results.append({"requested_store_name": requested, "store_name": requested, "row": None,
                                "status": "failed", "message": message, "advertising": "", "performance": "", "written_cells": []})
            elif not by_name[requested]["message"]:
                by_name[requested].update(status="failed", message=message)
    finally:
        if runtime is not None:
            try:
                runtime.shutdown()
            except Exception as exc:
                logger.warning("清理紫鸟运行时失败：%s", _redact(exc, query))
    success_count = sum(item["status"] == "success" for item in results)
    skipped_count = sum(item["status"] == "skipped" for item in results)
    failed_count = len(results) - success_count - skipped_count
    message = f"处理完成：成功 {success_count}，已有数据跳过 {skipped_count}，失败 {failed_count}"
    payload = {
        "success": failed_count == 0, "is_complete": failed_count == 0,
        "message": message, "completion_message": message,
        "workbook_id": query.workbook_id,
        "sheet_name": query.sheet_name, "target_date": query.target_date,
        "input_store_count": len(query.store_names), "matched_store_count": matched_count,
        "success_store_count": success_count, "skipped_store_count": skipped_count,
        "failed_store_count": failed_count, "output_dir": str(run_dir),
        "output_file": str(result_file), "log_file": str(log_file), "stores": results, "results": results,
    }
    try:
        temporary = result_file.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        os.replace(temporary, result_file)
        logger.info(message)
        return payload
    finally:
        logger.removeHandler(handler)
        handler.close()
