# ruff: noqa: DTZ001, S110
"""ERP SKU -> store with the earliest platform creation time (read-only, Shopee, all statuses)."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.services.erp_sku_earliest_store_checkpoint import (
    CONFIRMED_STATUSES,
    Checkpoint,
    CheckpointError,
    create_checkpoint,
    empty_row,
    read_checkpoint,
)
from backend.services.erp_sku_earliest_store_repository import (
    APPLICATION_NAME,
    QUERY_CONTRACT,
    AuthorizedScope,
    CandidateRow,
    DatabaseSettings,
    ErpSkuRepository,
    QuerySpec,
    SkuQueryError,
    chunked,
)
from backend.services.erp_sku_earliest_store_workbook import (
    MAX_SKUS,
    SkuInput,
    export_workbook,
    load_input,
    parse_sku_text,
)

PREVIEW_ROW_LIMIT = 500
DEFAULT_BATCH_SIZE = 50
SHANGHAI = timezone(timedelta(hours=8))  # Asia/Shanghai has no DST; avoids a tzdata dependency
MIN_PLAUSIBLE_CREATE_TIME = datetime(2010, 1, 1)
FUTURE_TOLERANCE = timedelta(days=1)
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
SCOPE_TEXT = "Shopee / 全部商品状态 / 包含 ERP 留存的归档记录 / 完整 SKU 精确匹配（区分大小写）/ 商品平台创建时间最早"
TIME_TEXT = "时间为 ERP 存储的 Asia/Shanghai 业务本地时间，未额外换算；不是店铺账号创建时间，也不是商品同步到 ERP 的时间"
ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class SkuEarliestJob:
    input: SkuInput
    output_dir: Path
    scope: AuthorizedScope
    db_settings: DatabaseSettings = field(repr=False)
    countries: tuple[str, ...] = ()
    shop_ids: tuple[str, ...] = ()
    resume_checkpoint: Path | None = None
    batch_size: int = DEFAULT_BATCH_SIZE


def _listify(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    items = re.split(r"[\s,;，；]+", value) if isinstance(value, str) else list(value)
    return [str(item).strip() for item in items if str(item).strip()]


def checkpoint_identity(job: SkuEarliestJob) -> dict[str, Any]:
    source = job.input
    return {
        "input_kind": source.input_kind, "source_sha256": source.source_sha256, "sheet_name": source.sheet_name,
        "skus": list(source.skus), "platform": "shopee", "match_mode": "exact", "status_scope": "all",
        "archive_scope": "include", "countries": sorted(job.countries), "shop_ids": sorted(job.shop_ids),
        "scope_digest": hashlib.sha256(job.scope.digest_text.encode("utf-8")).hexdigest(),
        "query_contract": QUERY_CONTRACT,
    }


def validate_payload(
    payload: Mapping[str, Any], *,
    settings: DatabaseSettings | None = None, scope: AuthorizedScope | None = None,
) -> SkuEarliestJob:
    """Build a job from UI/CLI arguments. Raises ``ValueError`` (input) or ``SkuQueryError`` (configuration)."""
    sku_text = str(payload.get("sku_text") or "").strip()
    source_text = str(payload.get("source_file") or "").strip()
    if sku_text and source_text:
        raise ValueError("单个 SKU 输入与 Excel 文件只能二选一")
    if not sku_text and not source_text:
        raise ValueError("请输入 SKU 或选择包含 SKU 的 Excel 文件")
    if source_text:
        sku_input = load_input(Path(source_text).expanduser().resolve(), str(payload.get("sheet_name") or "").strip() or None)
    else:
        sku_input = parse_sku_text(sku_text)
    if not sku_input.skus or len(sku_input.skus) > MAX_SKUS:
        raise ValueError(f"每次支持 1 至 {MAX_SKUS} 个不同 SKU")
    countries = tuple(dict.fromkeys(item.upper() for item in _listify(payload.get("countries"))))
    if any(not re.fullmatch(r"[A-Z]{2}", item) for item in countries):
        raise ValueError("国家/站点请填写两位国家代码，例如 PH、MY")
    shop_ids = tuple(dict.fromkeys(_listify(payload.get("shop_ids"))))
    if any(not item.isdigit() for item in shop_ids):
        raise ValueError("店铺筛选请填写数字 Shop ID")
    output = str(payload.get("output_dir") or "").strip()
    if not output:
        raise ValueError("请选择输出目录")
    output_dir = Path(output).expanduser().resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError("输出目录不能是文件")
    settings = settings or DatabaseSettings.from_environment()
    scope = scope or AuthorizedScope.from_environment()
    if shop_ids and not scope.company_wide and not set(shop_ids) & set(scope.shop_ids):
        raise ValueError("所选店铺不在授权范围内")
    resume = str(payload.get("resume_checkpoint") or "").strip()
    job = SkuEarliestJob(
        input=sku_input, output_dir=output_dir, scope=scope, db_settings=settings, countries=countries,
        shop_ids=shop_ids, resume_checkpoint=Path(resume).expanduser().resolve() if resume else None,
    )
    if job.resume_checkpoint:
        read_checkpoint(job.resume_checkpoint, checkpoint_identity(job))
    return job


# ---------------------------------------------------------------- row building


def _fmt(value: datetime | None) -> str:
    return value.strftime(TIME_FORMAT) if value else ""


def _yes_no(value: bool | None) -> str:
    return "" if value is None else "是" if value else "否"


def build_row(candidate: CandidateRow, queried_at: str, now: datetime) -> dict[str, Any]:
    sku = candidate.input_sku
    if candidate.result_status == "NOT_FOUND":
        row = empty_row(sku, "ERP 留存且在授权/筛选范围内的 Shopee 商品中没有该 SKU 的精确匹配"
                             "（已含全部状态与归档记录）；不代表该 SKU 从未存在", "not_found")
        row["queried_at"] = queried_at
        return row
    row = empty_row(sku, "", "matched")
    with_model = candidate.match_source == "model_sku"
    row.update(
        candidate_shop_id=candidate.candidate_shop_id, candidate_shop_name=candidate.candidate_shop_name,
        region=candidate.region, item_id=candidate.item_id, model_id=candidate.model_id if with_model else "",
        matched_sku=candidate.matched_sku, create_time=_fmt(candidate.candidate_create_time),
        item_status=candidate.item_status, item_archived=_yes_no(candidate.item_archived),
        model_archived=_yes_no(candidate.model_archived), match_source=candidate.match_source,
        matched_item_count=candidate.matched_item_count, matched_shop_count=candidate.matched_shop_count,
        data_issue_count=candidate.data_issue_count, tied_shop_count=candidate.tied_shop_count,
        base_clean_time=_fmt(candidate.base_clean_time), model_clean_time=_fmt(candidate.model_clean_time),
        queried_at=queried_at,
        tied_shops="；".join(f"{shop_id} {name}".strip() for shop_id, name in
                            zip(candidate.tied_shop_ids, candidate.tied_shop_names)) if candidate.tied_shop_count > 1 else "",
    )
    problems = []
    if candidate.result_status == "INCOMPLETE_DATA":
        problems.append(f"{candidate.data_issue_count} 个匹配商品缺少基础信息、创建时间、店铺资料或变体标记")
    created = candidate.candidate_create_time
    if candidate.result_status == "MATCHED":
        if created is None or created < MIN_PLAUSIBLE_CREATE_TIME or created > now.replace(tzinfo=None) + FUTURE_TOLERANCE:
            problems.append("最早创建时间缺失或超出合理范围")
        if not (candidate.candidate_shop_id and candidate.candidate_shop_name and candidate.item_id):
            problems.append("获胜记录缺少店铺或商品编号")
        if candidate.matched_sku != sku:
            problems.append("匹配 SKU 与输入不一致")
    if problems:
        known = f"最早已知候选：{candidate.candidate_shop_name or '—'}（{row['create_time'] or '时间缺失'}）" \
            if candidate.candidate_shop_id or candidate.item_id else "无可用候选"
        row["status"] = "incomplete"
        row["message"] = f"数据待核验：{'；'.join(problems)}；{known}，未作为“创建时间第一店铺”回填"
        return row
    notes = []
    if candidate.tied_shop_count > 1:
        notes.append(f"有 {candidate.tied_shop_count} 家店铺创建时间并列最早，按 Shop ID、Item ID、Model ID 升序稳定展示一家，"
                     "不代表它比其他并列店铺更早（见明细）")
    if candidate.data_issue_count > 0:
        notes.append(f"另有 {candidate.data_issue_count} 条缺基础商品/时间/店铺资料的历史记录未参与排名")
    if not with_model:
        notes.append("无变体商品，按商品 SKU 匹配")
    row["message"] = "；".join(notes)
    return row


def _failed_row(sku: str, error: SkuQueryError, queried_at: str) -> dict[str, Any]:
    row = empty_row(sku, f"查询失败：{error.message}" + (f"（SQLSTATE {error.sqlstate}）" if error.sqlstate else ""), "failed")
    row["queried_at"] = queried_at
    return row


# ---------------------------------------------------------------- orchestration


def _query_resilient(repo: Any, make_spec: Callable[[tuple[str, ...]], QuerySpec],
                     skus: tuple[str, ...]) -> dict[str, CandidateRow | SkuQueryError]:
    """Query a batch; on timeout bisect so one heavy SKU cannot sink its neighbours."""
    try:
        return {row.input_sku: row for row in repo.query_batch(make_spec(skus))}
    except SkuQueryError as exc:
        if exc.code == SkuQueryError.CODE_TIMEOUT and len(skus) > 1:
            middle = len(skus) // 2
            result = _query_resilient(repo, make_spec, skus[:middle])
            result.update(_query_resilient(repo, make_spec, skus[middle:]))
            return result
        return {sku: exc for sku in skus}


def _write_text_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex[:6]}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run_erp_sku_earliest_store(
    job: SkuEarliestJob, progress: ProgressCallback, *,
    repository_factory: Callable[[], Any] | None = None,
    cancel: Callable[[], bool] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    clock = clock or (lambda: datetime.now(SHANGHAI))
    source = job.input
    if not source.skus or len(source.skus) > MAX_SKUS:
        raise ValueError(f"每次支持 1 至 {MAX_SKUS} 个不同 SKU")
    identity = checkpoint_identity(job)
    previous = read_checkpoint(job.resume_checkpoint, identity) if job.resume_checkpoint else {}
    results = {sku: dict(previous.get(sku) or empty_row(sku, "尚未查询，等待补查")) for sku in source.skus}
    resumed = sum(row["status"] in CONFIRMED_STATUSES for row in results.values())
    run_dir = job.output_dir / f"ERP创建时间对标_{clock():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
    log_lines: list[str] = []

    def log(message: str) -> None:
        log_lines.append(f"[{clock():%H:%M:%S}] {message}")
        progress(message)

    checkpoint: Checkpoint | None = None
    checkpoint_path, checkpoint_error, stop_reason = "", "", ""
    batch_times: list[str] = []
    log(f"读取 {source.total_rows} 行、{len(source.skus)} 个不同 SKU；{SCOPE_TEXT}")
    log("授权范围：" + ("公司级全部店铺" if job.scope.company_wide else f"{len(job.scope.shop_ids)} 家授权店铺")
        + (f"；站点筛选 {','.join(job.countries)}" if job.countries else "")
        + (f"；店铺筛选 {len(job.shop_ids)} 家" if job.shop_ids else ""))
    confirmed_progress = resumed
    repo = None
    try:
        checkpoint = create_checkpoint(run_dir, identity)
        checkpoint.save_results(results.values())
        checkpoint_path = str(checkpoint.path)
        log(f"进度逐批保存：{checkpoint_path}")
        pending = tuple(sku for sku in source.skus if results[sku]["status"] not in CONFIRMED_STATUSES)
        if resumed:
            log(f"沿用较早批次已确认的 {resumed} 个 SKU（查询时间见明细），本次补查 {len(pending)} 个")

        def make_spec(skus: tuple[str, ...]) -> QuerySpec:
            return QuerySpec(skus=skus, scope=job.scope, shop_ids=job.shop_ids, countries=job.countries)

        if pending:
            repo = repository_factory() if repository_factory else ErpSkuRepository(job.db_settings)
        for batch in chunked(pending, max(1, job.batch_size)):
            if cancel is not None and cancel():
                stop_reason = "用户取消，已保存已完成批次"
                break
            started = clock()
            queried_at = started.strftime(TIME_FORMAT)
            batch_times.append(queried_at)
            outcome = _query_resilient(repo, make_spec, batch)
            rows, stop_error = [], None
            for sku in batch:
                item = outcome[sku]
                if isinstance(item, SkuQueryError):
                    rows.append(_failed_row(sku, item, queried_at))
                    if item.fatal or item.code == SkuQueryError.CODE_CONNECTION:
                        stop_error = stop_error or item
                else:
                    rows.append(build_row(item, queried_at, started))
            checkpoint.save_results(rows)  # commit before reporting progress
            for row in rows:
                results[row["sku"]] = row
            confirmed_progress += sum(row["status"] in CONFIRMED_STATUSES for row in rows)
            log(f"[ERP进度 {confirmed_progress}/{len(source.skus)}] 批次 {len(batch)} 个 SKU，查询时间 {queried_at}")
            if stop_error is not None:
                stop_reason = stop_error.message
                break
    except CheckpointError as exc:
        checkpoint_error = str(exc)
    except SkuQueryError as exc:
        stop_reason = exc.message
    except Exception:  # noqa: BLE001 - never expose driver or path details
        stop_reason = "查询初始化失败，请检查数据库连接配置、授权范围和输出目录"
    finally:
        if repo is not None:
            try:
                repo.close()
            except Exception:  # noqa: BLE001
                pass
        if checkpoint is not None:
            try:
                if stop_reason and not checkpoint_error:
                    changed = []
                    for sku, row in results.items():
                        if row["status"] == "not_run":
                            results[sku] = empty_row(sku, f"未执行：{stop_reason}")
                            changed.append(results[sku])
                    if changed:
                        checkpoint.save_results(changed)
            except CheckpointError as exc:
                checkpoint_error = str(exc)
            finally:
                checkpoint.close()
    rows = [results[sku] for sku in source.skus]
    counts = {status: sum(row["status"] == status for row in rows)
              for status in ("matched", "not_found", "incomplete", "failed", "not_run")}
    confirmed = counts["matched"] + counts["not_found"] + counts["incomplete"]
    complete = not (counts["incomplete"] or counts["failed"] or counts["not_run"]) and not checkpoint_error
    parts = []
    if counts["failed"] or counts["not_run"]:
        parts.append(f"{counts['failed']} 个查询失败、{counts['not_run']} 个未执行，可从进度文件补查")
    if counts["incomplete"]:
        parts.append(f"{counts['incomplete']} 个 SKU 数据待核验，未回填第一店铺")
    message = "全部 SKU 已确认并回填" if complete else "结果不完整：" + "；".join(parts or ["存在未确认项"])
    if stop_reason or checkpoint_error:
        message += "；" + (checkpoint_error or stop_reason)
    query_times = sorted({row["queried_at"] for row in rows if row["queried_at"]})
    summary_items = [
        ("查询口径", SCOPE_TEXT), ("时间口径", TIME_TEXT), ("查询逻辑版本", QUERY_CONTRACT),
        ("授权范围", "公司级全部店铺" if job.scope.company_wide else f"{len(job.scope.shop_ids)} 家授权店铺"),
        ("站点筛选", ",".join(job.countries) or "无"), ("店铺筛选", ",".join(job.shop_ids) or "无"),
        ("输入指纹(SHA-256)", source.source_sha256), ("结果是否完整", "是" if complete else "否"),
        ("批次查询时间", "、".join(query_times) or "无"),
        ("沿用较早批次", f"{resumed} 个 SKU（恢复任务，并非同一时刻快照）" if resumed else "无"),
        ("数据时效说明", "各行基础/变体采集时间见明细；查询执行时间不代表平台数据刚刷新，最早商品的采集时间不代表全部候选"),
        ("覆盖范围提醒", "仅 ERP 当前留存且 SKU 能匹配的记录；物理删除、从未同步、SKU 已被改写的历史无法还原"),
    ]
    output_file, export_error = "", ""
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        output_file = str(export_workbook(source, rows, run_dir, {"is_complete": complete, "summary_items": summary_items}))
    except Exception:  # noqa: BLE001 - the checkpoint still allows re-export
        complete = False
        export_error = "结果导出失败，请检查输出目录权限及文件状态" + ("，可从进度文件恢复并重新导出" if checkpoint_path else "")
        message += "；" + export_error
    log(message)
    summary = {
        "query_scope": SCOPE_TEXT, "time_semantics": TIME_TEXT, "query_contract": QUERY_CONTRACT,
        "application_name": APPLICATION_NAME, "input_kind": source.input_kind, "input_sha256": source.source_sha256,
        "sku_count": len(rows), "total_rows": source.total_rows, **counts, "confirmed_count": confirmed,
        "is_complete": complete, "completion_message": message, "batch_query_times": batch_times,
        "resumed_count": resumed, "authorized_company_wide": job.scope.company_wide,
        "countries": list(job.countries), "shop_filter_count": len(job.shop_ids),
        "checkpoint_file": checkpoint_path, "output_file": output_file,
    }
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(run_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
        _write_text_atomic(run_dir / "process.log", "\n".join(log_lines) + "\n")
    except OSError:
        pass
    return {
        **{key: summary[key] for key in ("sku_count", "total_rows", "confirmed_count", "is_complete",
                                          "completion_message", "resumed_count")},
        "matched_count": counts["matched"], "not_found_count": counts["not_found"],
        "incomplete_count": counts["incomplete"], "failed_count": counts["failed"], "not_run_count": counts["not_run"],
        "rows": rows[:PREVIEW_ROW_LIMIT], "preview_limited": len(rows) > PREVIEW_ROW_LIMIT,
        "output_file": output_file, "output_dir": str(job.output_dir), "run_dir": str(run_dir),
        "source_file": str(source.source_path or ""), "sheet_name": source.sheet_name,
        "checkpoint_file": checkpoint_path, "checkpoint_error": checkpoint_error,
        "export_error": export_error, "warnings": list(source.warnings),
        "countries": list(job.countries), "shop_ids": list(job.shop_ids),
    }
