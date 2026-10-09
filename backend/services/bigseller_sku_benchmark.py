from __future__ import annotations

import math
import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

import requests

from backend.services.bigseller_benchmark_workbook import (
    METRIC_LABELS,
    export_benchmark_workbook,
    load_workbook_input,
)
from backend.services.bigseller_benchmark_checkpoint import CheckpointError, create_checkpoint, read_checkpoint
from backend.services.bigseller_item_id_query import (
    LISTING_PATH,
    BigSellerAuthenticationError,
    BigSellerItemIdQueryJob,
    BigSellerPermissionError,
    BigSellerQueryError,
    BigSellerRateLimitError,
    BigSellerTransientError,
    build_listing_query,
    extract_listing_rows,
    login_for_query,
    query_process_config,
)
from backend.services.bigseller_sync import api_headers, load_bigseller_request_util


PREVIEW_ROW_LIMIT = 500
MAX_SKUS = 10000
MAX_PAGES = 500
PAGE_SIZE = 50
REQUEST_ATTEMPTS = 4
REQUEST_INTERVAL = 0.75
MAX_RETRY_WAIT = 30.0
RETRY_ROUND_DELAYS = (30.0, 60.0, 120.0)
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST = 0.0
_COOLDOWN_UNTIL = 0.0
ProgressCallback = Callable[[str], None]
CooldownCallback = Callable[[float], None]


@dataclass(frozen=True)
class BigSellerBenchmarkJob:
    username: str = field(repr=False)
    password: str = field(repr=False)
    source_file: Path
    sheet_name: str
    metric: str
    output_dir: Path
    captcha_username: str = field(default="", repr=False)
    captcha_password: str = field(default="", repr=False)
    resume_checkpoint: Path | None = None


class BenchmarkCooldownError(BigSellerRateLimitError):
    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = max(0.0, retry_after)


class BenchmarkCheckpointError(RuntimeError):
    pass


def validate_bigseller_benchmark_payload(payload: dict[str, Any]) -> BigSellerBenchmarkJob:
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password.strip():
        raise ValueError("请绑定并选择有效的 BigSeller 账号")
    metric = str(payload.get("metric") or "views")
    if metric not in METRIC_LABELS:
        raise ValueError("对标指标必须选择浏览量或销量")
    source = str(payload.get("source_file") or "").strip()
    if not source:
        raise ValueError("请选择源 Excel 文件")
    source_file = Path(source).expanduser().resolve()
    # Validate the selected sheet before login; execution reloads it to avoid a
    # stale file snapshot sitting in task context or account configuration.
    input_data = load_workbook_input(source_file, str(payload.get("sheet_name") or "").strip() or None)
    if len(input_data.skus) > MAX_SKUS:
        raise ValueError(f"单次最多对标 {MAX_SKUS} 个不同 SKU，请分表处理")
    output = str(payload.get("output_dir") or "").strip()
    if not output:
        raise ValueError("请选择输出目录")
    output_dir = Path(output).expanduser().resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError("输出目录不能是文件")
    resume_text = str(payload.get("resume_checkpoint") or "").strip()
    resume_checkpoint = Path(resume_text).expanduser().resolve() if resume_text else None
    job = BigSellerBenchmarkJob(
        username=username, password=password, source_file=source_file,
        sheet_name=input_data.sheet_name, metric=metric, output_dir=output_dir,
        captcha_username=str(payload.get("captcha_username") or "").strip(),
        captcha_password=str(payload.get("captcha_password") or ""),
        resume_checkpoint=resume_checkpoint,
    )
    if resume_checkpoint:
        read_checkpoint(resume_checkpoint, _checkpoint_identity(job, input_data))
    return job


def build_benchmark_query(sku: str, metric: str, page_no: int = 1) -> dict[str, Any]:
    if metric not in METRIC_LABELS:
        raise ValueError("对标指标必须选择浏览量或销量")
    return {**build_listing_query(sku), "orderBy": metric, "pageNo": page_no, "pageSize": PAGE_SIZE}


def metric_number(value: object) -> int:
    """A missing/unparseable metric must never turn into a zero-value winner."""
    if isinstance(value, bool) or value is None:
        raise BigSellerQueryError("商品指标缺失或格式异常，无法确认第一店铺")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.0+)?", value.strip()):
        number = int(value.strip().replace(",", "").split(".")[0])
    else:
        raise BigSellerQueryError("商品指标缺失或格式异常，无法确认第一店铺")
    if number < 0:
        raise BigSellerQueryError("商品指标为负数，无法确认第一店铺")
    return number


def _text(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


def _total_rows(page: dict[str, Any]) -> int | None:
    for key in ("totalSize", "totalCount", "total", "totalElements", "recordsTotal"):
        if key in page and page[key] is not None:
            try:
                return metric_number(page[key])
            except BigSellerQueryError:
                raise BigSellerQueryError("BigSeller 分页总数格式异常，无法确认完整结果") from None
    return None


def _retry_after(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        delay = float(text)
        if not math.isfinite(delay):
            return 0.0
        return max(0.0, delay)
    except ValueError:
        try:
            date = parsedate_to_datetime(text)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0.0


def wait_before_retry(seconds: float, progress: ProgressCallback, reason: str = "稍后继续查询") -> None:
    remaining = max(0.0, seconds)
    while remaining > 0:
        progress(f"等待 {math.ceil(remaining)} 秒：{reason}，已确认的结果已保留")
        step = min(10.0, remaining)
        time.sleep(step)
        remaining -= step


def _register_cooldown(seconds: float) -> None:
    global _COOLDOWN_UNTIL
    with _REQUEST_LOCK:
        _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic() + max(0.0, seconds))


def _post_listing(
    session: Any, util: Any, config: dict[str, Any], query: dict[str, Any],
    progress: ProgressCallback | None = None, *, on_cooldown: CooldownCallback | None = None,
) -> Any:
    global _LAST_REQUEST, _COOLDOWN_UNTIL
    # All benchmark jobs share a request gate. A ten-thread burst can cause
    # synchronized retries; this also prevents a second task recreating it.
    with _REQUEST_LOCK:
        now = time.monotonic()
        wait = max(REQUEST_INTERVAL - (now - _LAST_REQUEST), _COOLDOWN_UNTIL - now)
        if on_cooldown is not None and _COOLDOWN_UNTIL > now:
            on_cooldown(time.time() + (_COOLDOWN_UNTIL - now))
        if wait > 0:
            if wait > REQUEST_INTERVAL:
                wait_before_retry(wait, progress or (lambda _: None), "遵守 BigSeller 冷却时间")
            else:
                time.sleep(wait)
        _LAST_REQUEST = time.monotonic()
        response = session.post(
            util.build_api_url(LISTING_PATH), json=query,
            headers=api_headers(util, config, "shopee", content_type="application/json"),
            timeout=(10, 45), allow_redirects=False,
        )
        if response.status_code in (429, 503):
            delay = _retry_after(response.headers.get("Retry-After"))
            _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic() + delay)
        return response


def request_benchmark_page(
    session: Any, util: Any, config: dict[str, Any], sku: str, metric: str,
    page_no: int, progress: ProgressCallback, *, on_cooldown: CooldownCallback | None = None,
    query: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int | None]:
    for attempt in range(REQUEST_ATTEMPTS):
        delay = 0.0
        try:
            response = _post_listing(session, util, config, query if query is not None else build_benchmark_query(sku, metric, page_no), progress,
                                     on_cooldown=on_cooldown)
            try:
                status = response.status_code
                if status == 401 or 300 <= status < 400:
                    raise BigSellerAuthenticationError("BigSeller 登录已失效")
                if status == 403:
                    raise BigSellerPermissionError("BigSeller 无权读取商品列表，请检查账号权限")
                if status in (429, 503):
                    delay = _retry_after(response.headers.get("Retry-After"))
                if status == 503 and delay > MAX_RETRY_WAIT:
                    raise BenchmarkCooldownError("BigSeller 服务要求较长冷却时间，等待后补查", delay)
                if status == 429:
                    raise BigSellerRateLimitError("BigSeller 限制访问频率，请稍后重试失败的 SKU")
                if status >= 500:
                    raise BigSellerTransientError("BigSeller 服务暂时不可用")
                if not 200 <= status < 300:
                    raise BigSellerQueryError("BigSeller 商品请求失败，请检查账号权限或网络")
                try:
                    payload = response.json()
                except ValueError:
                    raise BigSellerQueryError("BigSeller 返回非 JSON 内容，无法确认匹配结果") from None
                if isinstance(payload, dict) and str(payload.get("code")) != "0":
                    message = str(payload.get("msg") or payload.get("message") or "").lower()
                    if any(word in message for word in ("频繁", "限流", "too many", "rate limit")):
                        raise BigSellerRateLimitError("BigSeller 限制访问频率，请稍后重试失败的 SKU")
                rows = extract_listing_rows(payload)
                return rows, _total_rows(payload["data"]["page"])
            finally:
                response.close()
        except BigSellerRateLimitError as exc:
            delay = max(delay, getattr(exc, "retry_after", 0.0), 2.0 * (2 ** attempt))
            _register_cooldown(delay)
            if on_cooldown is not None:
                on_cooldown(time.time() + delay)
            if attempt == REQUEST_ATTEMPTS - 1 or delay > MAX_RETRY_WAIT:
                raise BenchmarkCooldownError("BigSeller 暂时限制访问，等待冷却后补查", delay) from None
            progress(f"访问频率受限，等待 {delay:g} 秒后重试（{attempt + 1}/{REQUEST_ATTEMPTS - 1}）")
        except (requests.Timeout, requests.ConnectionError, BigSellerTransientError):
            if delay > 0:
                _register_cooldown(delay)
                if on_cooldown is not None:
                    on_cooldown(time.time() + delay)
            if attempt == REQUEST_ATTEMPTS - 1:
                if delay > 0:
                    raise BenchmarkCooldownError("BigSeller 服务尚未恢复，等待冷却后补查", delay) from None
                raise BigSellerQueryError("BigSeller 网络或服务异常，本轮重试未成功，等待下一轮补查") from None
            delay = max(delay, 1.0 * (2 ** attempt))
            if delay > MAX_RETRY_WAIT:
                raise BigSellerQueryError("BigSeller 服务要求稍后再试，本次查询未完成") from None
            progress(f"网络或服务暂时异常，等待 {delay:g} 秒后重试")
        except requests.RequestException:
            raise BigSellerQueryError("BigSeller 网络请求失败，请检查网络") from None
        wait_before_retry(delay, progress, "重试当前请求")
    raise BigSellerQueryError("BigSeller 商品查询未完成")


def query_benchmark_sku(
    session: Any, util: Any, config: dict[str, Any], sku: str, metric: str,
    progress: ProgressCallback, *, on_cooldown: CooldownCallback | None = None,
) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    seen_items: set[tuple[str, str]] = set()
    seen_pages: set[tuple[tuple[str, ...], ...]] = set()
    processed = 0
    expected_total = None
    for page_no in range(1, MAX_PAGES + 1):
        rows, total = request_benchmark_page(session, util, config, sku, metric, page_no, progress,
                                             on_cooldown=on_cooldown)
        if expected_total is not None and total is not None and total != expected_total:
            raise BigSellerQueryError("商品列表在分页期间发生变化，请重新查询该 SKU")
        if total is not None:
            expected_total = total
        fingerprint = tuple(tuple(_text(row.get(k)) for k in ("id", "itemId", "shopId", "shopName", metric)) for row in rows)
        if rows and fingerprint in seen_pages:
            raise BigSellerQueryError("BigSeller 重复返回同一页，无法确认完整店铺结果")
        seen_pages.add(fingerprint)
        for row in rows:
            shop_name = _text(row.get("shopName"))
            if not shop_name:
                raise BigSellerQueryError("匹配商品缺少店铺名称，无法确认第一店铺")
            shop_id = _text(row.get("shopId"))
            key = "id:" + shop_id if shop_id else "name:" + shop_name
            # shopSkuId is a listing/variation identifier, not a store ID.
            item_id = _text(row.get("itemId"))
            listing_id = _text(row.get("id")) or item_id
            if listing_id:
                identity = (key, listing_id)
                if identity in seen_items:
                    raise BigSellerQueryError("BigSeller 分页出现重复商品，请重新查询该 SKU")
                seen_items.add(identity)
            candidate = {"shop_name": shop_name, "shop_id": shop_id, "metric_value": metric_number(row.get(metric)), "item_id": item_id}
            current = grouped.get(key)
            if current is None or (-candidate["metric_value"], item_id) < (-current["metric_value"], current["item_id"]):
                grouped[key] = candidate
        processed += len(rows)
        if expected_total is not None:
            if processed > expected_total:
                raise BigSellerQueryError("BigSeller 商品数量与分页总数不一致，请重新查询")
            if processed == expected_total:
                break
            if not rows:
                raise BigSellerQueryError("BigSeller 分页提前结束，店铺结果不完整")
        elif len(rows) < PAGE_SIZE:
            break
    else:
        raise BigSellerQueryError("该 SKU 匹配商品过多，未完成全部分页，请使用更具体的 SKU")
    if not grouped:
        return {"sku": sku, "shop_name": "", "item_id": "", "metric_value": None, "shop_count": 0,
                "status": "not_found", "message": "当前账号可见的 Shopee 在售商品中未找到匹配 SKU"}
    winner = min(grouped.values(), key=lambda row: (-row["metric_value"], row["shop_name"], row["shop_id"]))
    return {"sku": sku, **winner, "shop_count": len(grouped), "status": "matched", "message": ""}


def _checkpoint_identity(job: BigSellerBenchmarkJob, input_data: Any) -> dict[str, Any]:
    return {
        "source_file": str(input_data.source_path), "source_sha256": input_data.source_sha256,
        "sheet_name": input_data.sheet_name, "metric": job.metric,
        "account_key": hashlib.sha256(job.username.encode("utf-8")).hexdigest(),
        "skus": list(input_data.skus),
    }


def _pending_row(sku: str, *, attempts: int = 0) -> dict[str, Any]:
    return {"sku": sku, "shop_name": "", "metric_value": None, "shop_count": 0, "item_id": "",
            "status": "failed", "message": "尚未查询，等待补查", "attempts": attempts,
            "queried_at": "", "retry_not_before": 0.0}


def _verified_outcome(sku: str, result: Any) -> dict[str, Any]:
    if not isinstance(result, dict) or result.get("sku") != sku:
        raise BigSellerQueryError("查询结果与输入 SKU 不一致，等待补查")
    row = _pending_row(sku)
    status = result.get("status")
    if status == "matched":
        shop = _text(result.get("shop_name"))
        count = metric_number(result.get("shop_count"))
        if not shop or count < 1:
            raise BigSellerQueryError("查询结果缺少店铺信息，等待补查")
        row.update(shop_name=shop, shop_id=_text(result.get("shop_id")), shop_count=count,
                   metric_value=metric_number(result.get("metric_value")), item_id=_text(result.get("item_id")))
    elif status == "not_found":
        if result.get("shop_name") or result.get("metric_value") is not None or result.get("item_id") or result.get("shop_count") != 0:
            raise BigSellerQueryError("未匹配结果存在矛盾数据，等待补查")
    else:
        raise BigSellerQueryError("查询结果状态异常，等待补查")
    row.update(status=status, message="" if status == "matched" else "已确认当前账号可见的 Shopee 在售商品中没有匹配 SKU")
    return row


def run_bigseller_sku_benchmark(job: BigSellerBenchmarkJob, progress: ProgressCallback) -> dict[str, Any]:
    input_data = load_workbook_input(job.source_file, job.sheet_name)
    if len(input_data.skus) > MAX_SKUS:
        raise ValueError(f"单次最多对标 {MAX_SKUS} 个不同 SKU，请分表处理")
    identity = _checkpoint_identity(job, input_data)
    previous = read_checkpoint(job.resume_checkpoint, identity) if job.resume_checkpoint else {}
    results = {sku: dict(previous.get(sku) or _pending_row(sku)) for sku in input_data.skus}
    resumed_count = sum(row["status"] != "failed" for row in results.values())
    recovered_count = 0
    retry_rounds_used = 0
    session = None
    stop_reason = ""
    checkpoint_error = ""
    refreshed = False
    checkpoint = None
    # A failed initial copy must still point back to the untouched source
    # checkpoint, whose confirmed rows remain recoverable.
    checkpoint_path = str(job.resume_checkpoint) if job.resume_checkpoint else ""

    def persist_cooldown(deadline: float) -> None:
        unfinished = [dict(row, retry_not_before=max(row.get("retry_not_before", 0.0), deadline))
                      for row in results.values() if row["status"] == "failed"]
        try:
            checkpoint.save_results(unfinished)
        except Exception:
            # A disk failure must stop requests, rather than masquerade as a
            # retriable query error after losing the server's recovery deadline.
            raise BenchmarkCheckpointError("无法保存服务冷却进度，已停止查询；请检查输出磁盘") from None
        results.update((row["sku"], row) for row in unfinished)

    try:
        checkpoint = create_checkpoint(job.output_dir, identity)
        if not checkpoint_path:
            checkpoint_path = str(checkpoint.path)
        checkpoint.save_results(results.values())
        checkpoint_path = str(checkpoint.path)
        for warning in input_data.warnings:
            progress(warning)
        progress(f"源表 {input_data.sheet_name}：{input_data.total_rows} 行，{len(input_data.skus)} 个不同 SKU")
        progress(f"进度文件已建立：{checkpoint_path}；中断后可选择此文件继续补查")
        progress(f"[BS对标进度 {resumed_count}/{len(input_data.skus)}] 已确认 {resumed_count} 个 SKU，剩余只查询未完成项")
        progress(f"对标{METRIC_LABELS[job.metric]}：Shopee 在售商品，SKU 模糊搜索，完整分页；每店取单商品最大值")
        pending = [sku for sku, row in results.items() if row["status"] == "failed"]
        if pending:
            try:
                config = query_process_config(BigSellerItemIdQueryJob(
                    username=job.username, password=job.password, skus=input_data.skus, output_dir=job.output_dir,
                    captcha_username=job.captcha_username, captcha_password=job.captcha_password,
                ))
                config["process_name"] = "滞销SKU爆款对标"
                util = load_bigseller_request_util()
                session = login_for_query(util, config, progress)
            except BigSellerAuthenticationError as exc:
                stop_reason = str(exc)
            except Exception:
                stop_reason = "BigSeller 登录准备失败，请检查账号、验证码服务和网络后继续补查"

        if pending and not stop_reason:
            # The checkpoint carries server cooldowns across an application restart.
            delay = max((results[sku].get("retry_not_before", 0.0) - time.time() for sku in pending), default=0.0)
            wait_before_retry(delay, progress, "沿用上次服务要求的冷却时间")
            for round_index in range(len(RETRY_ROUND_DELAYS) + 1):
                if round_index:
                    retry_rounds_used = round_index
                    progress(f"[BS补查 第{round_index}轮] 只补查 {len(pending)} 个未完成 SKU")
                cooldown_until = 0.0
                for sku in pending:
                    prior = results[sku]
                    attempts = prior.get("attempts", 0) + 1
                    row = _pending_row(sku, attempts=attempts)
                    hit_cooldown = False
                    try:
                        try:
                            outcome = query_benchmark_sku(session, util, config, sku, job.metric, progress,
                                                          on_cooldown=persist_cooldown)
                        except BigSellerAuthenticationError:
                            if refreshed:
                                raise
                            refreshed = True
                            progress("登录状态失效，重新登录一次并从第一页重查当前 SKU")
                            session.close()
                            session = None
                            session = login_for_query(util, config, progress)
                            outcome = query_benchmark_sku(session, util, config, sku, job.metric, progress,
                                                          on_cooldown=persist_cooldown)
                        row = _verified_outcome(sku, outcome)
                        row["attempts"] = attempts
                    except BenchmarkCheckpointError:
                        raise
                    except (BigSellerAuthenticationError, BigSellerPermissionError) as exc:
                        stop_reason = str(exc)
                        row["message"] = stop_reason
                    except BigSellerRateLimitError as exc:
                        hit_cooldown = True
                        cooldown_until = time.time() + max(0.0, getattr(exc, "retry_after", 0.0))
                        row.update(message="BigSeller 尚在限流或服务冷却中，等待补查", retry_not_before=cooldown_until)
                    except BigSellerQueryError as exc:
                        row["message"] = str(exc)
                    except Exception:
                        row["message"] = "商品查询异常，等待补查；若持续失败请联系维护人员"
                    row["queried_at"] = datetime.now(timezone.utc).isoformat()
                    # Publish a confirmed row only after its transaction commits.
                    # If storage refuses this row, recovery must query it again.
                    checkpoint.save_result(row)
                    results[sku] = row
                    if row["status"] != "failed" and prior.get("attempts", 0):
                        recovered_count += 1
                    confirmed = sum(item["status"] != "failed" for item in results.values())
                    detail = f"{row['shop_name']}（{METRIC_LABELS[job.metric]} {row['metric_value']}，{row['shop_count']} 家店铺）" if row["status"] == "matched" else row["message"]
                    progress(f"[BS对标进度 {confirmed}/{len(input_data.skus)}] {sku} — {detail}")
                    if stop_reason or hit_cooldown:
                        # Never run the next SKU against a server that requested a cooldown.
                        break
                pending = [sku for sku, row in results.items() if row["status"] == "failed"]
                if cooldown_until:
                    persist_cooldown(cooldown_until)
                if not pending or stop_reason or round_index == len(RETRY_ROUND_DELAYS):
                    break
                delay = max(RETRY_ROUND_DELAYS[round_index], cooldown_until - time.time())
                progress(f"本轮结束，已确认 {len(input_data.skus) - len(pending)} 个 SKU，剩余 {len(pending)} 个等待补查")
                wait_before_retry(delay, progress, f"第 {round_index + 1} 轮补查")
        if stop_reason:
            progress(f"暂停原因：{stop_reason}")
            final_rows = [dict(row) for row in results.values()]
            for row in final_rows:
                if row["status"] == "failed" and not row.get("attempts"):
                    row["message"] = "未执行：" + stop_reason
            checkpoint.save_results(final_rows)
            results.update((row["sku"], row) for row in final_rows)
            progress("账号、权限或登录配置需要处理，进度已保存；处理后可继续补查")
        else:
            checkpoint.save_results(results.values())
    except CheckpointError as exc:
        # This exception contains only the checkpoint layer's fixed, safe
        # diagnostic. Do not discard committed rows by failing the whole task.
        checkpoint_error = str(exc)
        progress(f"查询已停止：{checkpoint_error}")
        if checkpoint_path:
            progress(f"已提交的进度仍保留：{checkpoint_path}；本次未保存的 SKU 需要补查")
        else:
            progress("本次尚未建立可恢复进度，处理保存问题后可重新开始")
    finally:
        try:
            if session is not None:
                session.close()
        finally:
            if checkpoint is not None:
                checkpoint.close()

    rows = [results[sku] for sku in input_data.skus]
    counts = {name + "_count": sum(row["status"] == name for row in rows) for name in ("matched", "not_found", "failed")}
    confirmed = counts["matched_count"] + counts["not_found_count"]
    query_complete = confirmed == len(input_data.skus) and counts["failed_count"] == 0
    is_complete = query_complete and not checkpoint_error
    summary = {"is_complete": is_complete, "total_rows": input_data.total_rows, "sku_count": len(rows),
               **counts, "confirmed_count": confirmed, "retry_rounds_used": retry_rounds_used,
               "recovered_count": recovered_count, "resumed_count": resumed_count}
    output_file = ""
    message = "所有 SKU 均已有明确查询结果" if is_complete else f"结果不完整：仍有 {counts['failed_count']} 个 SKU 待补查，已保存成功结果和进度"
    if checkpoint_error:
        recovery = "已提交的结果仍保留，可在处理保存问题后从进度文件继续" if checkpoint_path else "尚未建立可恢复进度"
        message = f"任务未完成：{checkpoint_error}；{recovery}"
    try:
        # Workbook completeness describes the confirmed rows themselves. A
        # failed final metadata save still makes the task incomplete above.
        export_summary = {**summary, "is_complete": query_complete}
        output_file = str(export_benchmark_workbook(input_data, rows, job.metric, job.output_dir, summary=export_summary))
    except ValueError as exc:
        is_complete = False
        export_error = f"结果导出未完成：{exc}"
        message = f"{message}；{export_error}" if checkpoint_error else f"{export_error}；查询进度已保存"
    except Exception:
        is_complete = False
        export_error = "结果导出失败，请检查输出目录权限及文件状态"
        message = f"{message}；{export_error}" if checkpoint_error else f"{export_error}；查询进度已保存，可从进度文件恢复后重新导出"
    progress(f"{message}；命中 {counts['matched_count']}，确认未匹配 {counts['not_found_count']}，待补查 {counts['failed_count']}")
    if output_file:
        progress(f"Excel 已保存：{output_file}")
    return {
        "metric": job.metric, "metric_label": METRIC_LABELS[job.metric],
        "source_file": str(job.source_file), "sheet_name": input_data.sheet_name, **summary,
        "is_complete": is_complete, "completion_message": message,
        "checkpoint_error": checkpoint_error,
        "rows": rows[:PREVIEW_ROW_LIMIT], "preview_limited": len(rows) > PREVIEW_ROW_LIMIT,
        "failed_skus": [row["sku"] for row in rows if row["status"] == "failed"],
        "checkpoint_file": checkpoint_path, "output_file": output_file, "output_dir": str(job.output_dir),
        "warnings": list(input_data.warnings),
    }
