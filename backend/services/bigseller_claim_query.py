"""Read-only BS claim query: earliest BS creation, with times from that same listing."""
from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.services.bigseller_claim_checkpoint import (
    ClaimCheckpointError,
    create_claim_checkpoint,
    read_claim_checkpoint,
)
from backend.services.bigseller_claim_workbook import (
    export_claim_workbook,
    load_claim_input,
)
from backend.services.bigseller_item_id_query import (
    BigSellerAuthenticationError,
    BigSellerItemIdQueryJob,
    BigSellerPermissionError,
    BigSellerQueryError,
    build_listing_query,
    login_for_query,
    query_process_config,
)
from backend.services.bigseller_sku_benchmark import (
    BenchmarkCooldownError,
    request_benchmark_page,
    wait_before_retry,
)
from backend.services.bigseller_sync import load_bigseller_request_util

MAX_SKUS = 10000
MAX_PAGES = 500
PAGE_SIZE = 50
PREVIEW_ROW_LIMIT = 500
SITES = {"all", "ID", "PH", "MY", "VN", "TH", "SG", "TW"}
ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class BigSellerClaimJob:
    username: str = field(repr=False)
    password: str = field(repr=False)
    source_file: Path
    sheet_name: str
    output_dir: Path
    site: str = "all"
    listing_scope: str = "live"
    captcha_username: str = field(default="", repr=False)
    captcha_password: str = field(default="", repr=False)
    resume_checkpoint: Path | None = None


def checkpoint_identity(job: BigSellerClaimJob, source: Any) -> dict[str, Any]:
    return {
        "source_file": str(source.source_path), "source_sha256": source.source_sha256,
        "sheet_name": source.sheet_name, "site": job.site, "listing_scope": job.listing_scope,
        "account_key": hashlib.sha256(job.username.strip().encode("utf-8")).hexdigest(),
        "skus": list(source.skus), "query_contract": "parentSku-exact-earliest-create_time-v2",
    }


def validate_bigseller_claim_payload(payload: dict[str, Any]) -> BigSellerClaimJob:
    username, password = str(payload.get("username") or "").strip(), str(payload.get("password") or "")
    if not username or not password.strip():
        raise ValueError("请绑定并选择有效的 BigSeller 账号")
    site_text = str(payload.get("site") or "all").strip()
    site = "all" if site_text.lower() == "all" else site_text.upper()
    if site not in SITES:
        raise ValueError("请选择有效的 Shopee 站点")
    scope = str(payload.get("listing_scope") or "live")
    if scope != "live":
        raise ValueError("当前仅支持 Shopee 在售商品范围")
    source_text = str(payload.get("source_file") or "").strip()
    if not source_text:
        raise ValueError("请选择新品认领 Excel 文件")
    source_file = Path(source_text).expanduser().resolve()
    source = load_claim_input(source_file, str(payload.get("sheet_name") or "").strip() or None)
    if not source.skus or len(source.skus) > MAX_SKUS:
        raise ValueError(f"每次支持 1 至 {MAX_SKUS} 个不同 SKU，请检查所选工作表")
    output = str(payload.get("output_dir") or "").strip()
    if not output:
        raise ValueError("请选择输出目录")
    output_dir = Path(output).expanduser().resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError("输出目录不能是文件")
    resume = str(payload.get("resume_checkpoint") or "").strip()
    job = BigSellerClaimJob(
        username=username, password=password, source_file=source_file, sheet_name=source.sheet_name,
        output_dir=output_dir, site=site, listing_scope=scope,
        captcha_username=str(payload.get("captcha_username") or "").strip(),
        captcha_password=str(payload.get("captcha_password") or ""),
        resume_checkpoint=Path(resume).expanduser().resolve() if resume else None,
    )
    if job.resume_checkpoint:
        read_claim_checkpoint(job.resume_checkpoint, checkpoint_identity(job, source))
    return job


def build_claim_query(sku: str, page_no: int = 1) -> dict[str, Any]:
    # No unverified server-side site filter: inspect each returned listing's site.
    return {**build_listing_query(sku), "searchType": "parentSku", "inquireType": 2,
            "orderBy": "create_time", "desc": False, "pageNo": page_no, "pageSize": PAGE_SIZE}


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""


def parse_listing_time(value: Any) -> datetime:
    text = _text(value)
    if not re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?", text):
        raise BigSellerQueryError("商品创建时间缺失或格式异常，无法确认最早商品")
    try:
        parsed = datetime.fromisoformat(text.replace("/", "-").replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
        return parsed
    except ValueError:
        raise BigSellerQueryError("商品创建时间缺失或格式异常，无法确认最早商品") from None


def empty_row(sku: str, message: str = "尚未查询，等待补查", status: str = "failed") -> dict[str, str]:
    return {"sku": sku, "shop_name": "", "item_id": "", "created_time": "", "listed_time": "",
            "status": status, "message": message}


def _creation_milliseconds(value: Any) -> int | None:
    # Compare original precision only. Its timezone relationship to the display
    # string is not assumed, and it is never converted into an output date.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    return int(text) if re.fullmatch(r"[0-9]{13}", text) else None


def query_claim_sku(session: Any, util: Any, config: dict[str, Any], sku: str, site: str,
                    progress: ProgressCallback, *, on_cooldown=None) -> dict[str, str]:
    candidates = []
    seen_items, seen_pages = set(), set()
    expected_total, processed = None, 0
    for page in range(1, MAX_PAGES + 1):
        rows, total = request_benchmark_page(
            session, util, config, sku, "views", page, progress,
            query=build_claim_query(sku, page), on_cooldown=on_cooldown,
        )
        if expected_total is not None and total is not None and total != expected_total:
            raise BigSellerQueryError("商品数量在分页期间变化，请重新查询该 SKU")
        if total is not None:
            expected_total = total
        fingerprint = tuple(tuple(_text(row.get(key)) for key in (
            "id", "itemId", "shopId", "itemSku", "site", "createTimeStr")) for row in rows)
        if rows and fingerprint in seen_pages:
            raise BigSellerQueryError("BigSeller 重复返回同一页，无法确认完整结果")
        seen_pages.add(fingerprint)
        for row in rows:
            # Official Shopee table declares Parent SKU as dataIndex="itemSku".
            parent_sku = _text(row.get("itemSku"))
            if not parent_sku:
                raise BigSellerQueryError("匹配商品缺少主 SKU（itemSku），无法核实精确匹配")
            if parent_sku != sku:
                continue
            row_site = _text(row.get("site")).upper()
            if site != "all":
                if not row_site:
                    raise BigSellerQueryError("匹配商品缺少站点，无法核实查询范围")
                if row_site != site:
                    continue
            item_id = _text(row.get("itemId"))
            if not re.fullmatch(r"[0-9]+", item_id) or not item_id.lstrip("0") or len(item_id) > 100:
                raise BigSellerQueryError("匹配商品缺少有效 Item ID，无法确认最早商品")
            identity = (_text(row.get("shopId")), _text(row.get("id")) or item_id)
            if identity in seen_items:
                raise BigSellerQueryError("BigSeller 分页出现重复商品，请重新查询该 SKU")
            seen_items.add(identity)
            created = parse_listing_time(row.get("createTimeStr"))
            shop_name = _text(row.get("shopName"))
            if not shop_name:
                raise BigSellerQueryError("匹配商品缺少店铺名称，无法确认最早店铺")
            candidates.append((created, int(item_id), row_site, shop_name, item_id, row))
        processed += len(rows)
        if expected_total is not None:
            if processed > expected_total or (processed < expected_total and not rows):
                raise BigSellerQueryError("BigSeller 分页结果不完整，无法确认最早商品")
            if processed == expected_total:
                break
        elif len(rows) < PAGE_SIZE:
            break
    else:
        raise BigSellerQueryError("匹配结果超出分页上限，无法确认最早商品")
    if not candidates:
        return empty_row(sku, "当前账号可见的所选站点 Shopee 在售商品中没有精确主 SKU 匹配", "not_found")
    earliest_display = min(candidate[0] for candidate in candidates)
    finalists = [candidate for candidate in candidates if candidate[0] == earliest_display]
    precise_tie = False
    if len(finalists) > 1:
        raw_times = [_creation_milliseconds(candidate[5].get("createTime")) for candidate in finalists]
        if all(value is not None for value in raw_times):
            earliest_raw = min(raw_times)
            finalists = [candidate for candidate, raw in zip(finalists, raw_times) if raw == earliest_raw]
            precise_tie = True
    winner = min(finalists, key=lambda candidate: candidate[1:5])
    created, _, _, shop_name, item_id, listing = winner
    messages = []
    ties = len(finalists)
    if ties > 1:
        if precise_tie:
            messages.append(f"有 {ties} 条商品原始创建时间并列最早，按 Item ID 升序选取")
        else:
            messages.append(f"有 {ties} 条商品按 BS 显示时间精度并列最早，原始时间缺失或无效，按 Item ID 升序选取")
    listed_time = ""
    try:
        listed_time = parse_listing_time(listing.get("platformCreateTime")).strftime("%Y-%m-%d %H:%M:%S")
    except BigSellerQueryError:
        messages.append("BS 平台创建时间缺失或格式异常，上架时间留空")
    return {"sku": sku, "shop_name": shop_name, "item_id": item_id,
            "created_time": created.strftime("%Y-%m-%d %H:%M:%S"), "listed_time": listed_time,
            "status": "matched", "message": "；".join(messages)}


def run_bigseller_claim_query(job: BigSellerClaimJob, progress: ProgressCallback) -> dict[str, Any]:
    source = load_claim_input(job.source_file, job.sheet_name)
    if not source.skus or len(source.skus) > MAX_SKUS:
        raise ValueError(f"每次支持 1 至 {MAX_SKUS} 个不同 SKU")
    if job.site not in SITES or job.listing_scope != "live":
        raise ValueError("无效的站点或商品范围")
    identity = checkpoint_identity(job, source)
    previous, deadline = read_claim_checkpoint(job.resume_checkpoint, identity) if job.resume_checkpoint else ({}, 0.0)
    results = {sku: dict(previous.get(sku) or empty_row(sku)) for sku in source.skus}
    checkpoint, session = None, None
    checkpoint_path = str(job.resume_checkpoint or "")
    checkpoint_error, stop_reason = "", ""
    resumed_count = sum(row["status"] != "failed" for row in results.values())
    confirmed_progress = resumed_count
    progress(f"读取 {source.total_rows} 个 SKU 数据行，去重后 {len(source.skus)} 个主 SKU；Shopee 在售，站点 {job.site}")
    progress("按 BS 创建时间选最早商品；上架时间按同一商品的 BS 平台创建时间，非独立历史上架记录")
    try:
        checkpoint = create_claim_checkpoint(job.output_dir, identity)
        checkpoint.save_results(results.values())
        checkpoint.save_deadline(deadline)
        checkpoint_path = str(checkpoint.path)
        progress(f"查询进度逐项保存：{checkpoint_path}")
        pending = [sku for sku in source.skus if results[sku]["status"] == "failed"]
        if pending:
            remaining = max(0.0, deadline - time.time())
            if remaining:
                wait_before_retry(remaining, progress, "遵守已保存的 BigSeller 冷却时间")
            config = query_process_config(BigSellerItemIdQueryJob(
                username=job.username, password=job.password, skus=source.skus, output_dir=job.output_dir,
                captcha_username=job.captcha_username, captcha_password=job.captcha_password,
            ))
            util = load_bigseller_request_util()
            session = login_for_query(util, config, progress)
            refreshed = False
            for sku in source.skus:
                if results[sku]["status"] != "failed":
                    continue
                try:
                    try:
                        row = query_claim_sku(session, util, config, sku, job.site, progress,
                                              on_cooldown=checkpoint.save_deadline)
                    except BigSellerAuthenticationError:
                        if refreshed:
                            raise
                        refreshed = True
                        session.close()
                        session = None
                        session = login_for_query(util, config, progress)
                        row = query_claim_sku(session, util, config, sku, job.site, progress,
                                              on_cooldown=checkpoint.save_deadline)
                except BenchmarkCooldownError as exc:
                    stop_reason = str(exc)
                    checkpoint.save_deadline(time.time() + exc.retry_after)
                    row = empty_row(sku, stop_reason)
                except (BigSellerAuthenticationError, BigSellerPermissionError) as exc:
                    stop_reason = str(exc)
                    row = empty_row(sku, stop_reason)
                except BigSellerQueryError as exc:
                    row = empty_row(sku, str(exc))
                except ClaimCheckpointError:
                    raise
                except Exception:  # noqa: BLE001 - never expose credentials in third-party exception messages
                    row = empty_row(sku, "商品查询异常，请检查网络后从进度文件补查")
                if row.get("sku") != sku:
                    raise ClaimCheckpointError("查询结果与当前 SKU 不一致，查询已停止")
                # Commit before advancing; never claim a result persisted before its transaction succeeds.
                checkpoint.save_result(row)
                results[sku] = row
                confirmed_progress += row["status"] in {"matched", "not_found"}
                progress(f"[BS认领进度 {confirmed_progress}/{len(source.skus)}] {sku} — {row['shop_name'] or row['message']}")
                if stop_reason:
                    break
    except ClaimCheckpointError as exc:
        checkpoint_error = str(exc)
    except BigSellerQueryError as exc:
        stop_reason = str(exc)
    except Exception:  # noqa: BLE001 - preserve partial results without exposing login/configuration secrets
        stop_reason = "查询初始化失败，请检查 BigSeller 账号、验证码服务、网络及输出目录"
    finally:
        if session is not None:
            session.close()
        if checkpoint is not None:
            try:
                if stop_reason and not checkpoint_error:
                    changed = []
                    for sku, row in results.items():
                        if row["status"] == "failed" and row["message"] == "尚未查询，等待补查":
                            results[sku] = empty_row(sku, f"未执行：{stop_reason}")
                            changed.append(results[sku])
                    if changed:
                        checkpoint.save_results(changed)
            except ClaimCheckpointError as exc:
                checkpoint_error = str(exc)
            finally:
                checkpoint.close()
    rows = [results[sku] for sku in source.skus]
    counts = {name + "_count": sum(row["status"] == name for row in rows)
              for name in ("matched", "not_found", "failed")}
    confirmed = counts["matched_count"] + counts["not_found_count"]
    complete = confirmed == len(rows) and not checkpoint_error
    message = "全部 SKU 已完成查询" if complete else f"仍有 {counts['failed_count']} 个 SKU 待补查"
    if stop_reason or checkpoint_error:
        message += "；" + (checkpoint_error or stop_reason)
    output_file = ""
    metadata = {"site": job.site, "listing_scope": job.listing_scope, "is_complete": complete,
                "sku_count": len(rows), "confirmed_count": confirmed, **counts,
                "checkpoint_file": checkpoint_path,
                "time_semantics": "上架时间按 BS 平台创建时间；仅当前账号可见的 Shopee 在售商品"}
    try:
        output_file = str(export_claim_workbook(source, rows, job.output_dir, metadata=metadata))
    except Exception:  # noqa: BLE001 - retain the checkpoint for recovery from any workbook export failure
        complete = False
        message += "；结果导出失败，请检查输出目录权限及文件状态"
        if checkpoint_path:
            message += "，可从进度文件恢复并重新导出"
    progress(message)
    return {"sku_count": len(rows), "total_rows": source.total_rows, **counts, "confirmed_count": confirmed,
            "is_complete": complete, "completion_message": message,
            "rows": rows[:PREVIEW_ROW_LIMIT], "preview_limited": len(rows) > PREVIEW_ROW_LIMIT,
            "output_file": output_file, "output_dir": str(job.output_dir), "source_file": str(source.source_path),
            "sheet_name": source.sheet_name, "site": job.site, "listing_scope": job.listing_scope,
            "checkpoint_file": checkpoint_path, "checkpoint_error": checkpoint_error,
            "resumed_count": resumed_count, "warnings": list(source.warnings)}
