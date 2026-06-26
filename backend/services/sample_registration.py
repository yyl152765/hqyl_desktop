"""网红寄样登记 — 核心服务。

无全局状态，所有函数显式接收 job、客户端和 progress。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from backend.core.mabang_client import MabangClient
from backend.core.mabang_order_client import (
    OrderItemRecord,
    OrderRecord,
    build_output_record,
    fetch_order_items,
    fetch_orders_with_retry,
    is_sample_order,
    list_order_shops,
)
from backend.services.sample_registration_config import (
    SampleGroup,
    SampleRegistrationConfig,
    clean_text,
    resolve_field_value,
    resolve_register_date,
    validate_config,
)
from backend.services.sample_store_matcher import (
    StoreMatchSummary,
    format_unresolved_message,
    match_configured_groups,
)

ProgressCallback = Callable[[str], None]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_MABANG_BASE_URL = "https://900853.private.mabangerp.com"
OUTPUT_COLUMNS = ["付款时间", "订单编号", "店铺名", "SKU", "商品中文名称", "商品总成本", "重量"]
TARGET_FILL_COLUMNS = ["寄样日期", "订单号", "店铺", "SKU", "中文名", "商品总成本", "重量"]
TARGET_DEDUPE_COLUMNS = ["订单号", "SKU"]
DINGTALK_RANGE_CELL_LIMIT = 30000
DINGTALK_HEADER_SCAN_RANGE = "A1:Z1"
DINGTALK_EXISTING_SCAN_FALLBACK_ROWS = 10000
DINGTALK_EXISTING_SCAN_MAX_ROWS = 100000


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SampleRegistrationJob:
    username: str
    password: str
    start_date: str
    end_date: str
    output_dir: Path
    target_doc_id: str
    enable_target_update: bool
    groups: tuple[SampleGroup, ...]


@dataclass(slots=True)
class SampleRegistrationResult:
    processed_count: int = 0
    online_append_count: int = 0
    unmatched_store_count: int = 0
    output_directory: str = ""
    resolved_count: int = 0
    group_count: int = 0
    output_files: list[str] = field(default_factory=list)


class UnmatchedStoreError(RuntimeError):
    def __init__(self, summary: StoreMatchSummary) -> None:
        self.summary = summary
        super().__init__(format_unresolved_message(summary))


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _build_output_dir(base_dir: Path, date_label: str) -> Path:
    """构建输出目录：base_dir/网红寄样登记/YYYYMMDD-HHmmss_label/"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dirname = f"{ts}_{date_label}" if date_label else ts
    output = base_dir / "网红寄样登记" / dirname
    output.mkdir(parents=True, exist_ok=True)
    return output


def _safe_filename(name: str) -> str:
    """清洗文件名。"""
    name = clean_text(name)
    if not name:
        return "unnamed.xlsx"
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    if not name.endswith(".xlsx"):
        name += ".xlsx"
    return name


def _column_name(index: int) -> str:
    if index < 0:
        raise ValueError("column index must be non-negative")
    name = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def _sheet_existing_max_row(meta: dict[str, Any]) -> int:
    for key in ("lastNonEmptyRow", "last_non_empty_row"):
        try:
            value = int(meta.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    for key in ("rowCount", "row_count"):
        try:
            value = int(meta.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return min(value, DINGTALK_EXISTING_SCAN_MAX_ROWS)
    return DINGTALK_EXISTING_SCAN_FALLBACK_ROWS


def _extract_dedupe_key(row: list[Any], key_indices: list[int], min_index: int) -> str:
    parts: list[str] = []
    for idx in key_indices:
        local_idx = idx - min_index
        value = row[local_idx] if 0 <= local_idx < len(row) else ""
        parts.append(clean_text(str(value or "")).upper())
    if not any(parts):
        return ""
    return "||".join(parts)


def _read_existing_dingtalk_keys(
    read_sheet_range_func: Callable[..., list[list[Any]]],
    token: str,
    union_id: str,
    target_doc_id: str,
    sheet_id: str,
    meta: dict[str, Any],
    *,
    app_key: str,
    app_secret: str,
) -> set[str]:
    """Read existing dedupe keys without exceeding DingTalk's 30000-cell range limit."""
    header_rows = read_sheet_range_func(
        token,
        union_id,
        target_doc_id,
        sheet_id,
        DINGTALK_HEADER_SCAN_RANGE,
        app_key=app_key,
        app_secret=app_secret,
    )
    if not header_rows:
        return set()
    header = [clean_text(str(c or "")) for c in (header_rows[0] or [])]
    key_indices: list[int] = []
    for col in TARGET_DEDUPE_COLUMNS:
        try:
            key_indices.append(header.index(col))
        except ValueError:
            continue
    if not key_indices:
        return set()

    min_index = min(key_indices)
    max_index = max(key_indices)
    width = max_index - min_index + 1
    batch_rows = max(1, DINGTALK_RANGE_CELL_LIMIT // max(width, 1))
    start_col = _column_name(min_index)
    end_col = _column_name(max_index)
    max_row = max(1, _sheet_existing_max_row(meta))

    existing_keys: set[str] = set()
    start_row = 2
    while start_row <= max_row:
        end_row = min(max_row, start_row + batch_rows - 1)
        cell_range = f"{start_col}{start_row}:{end_col}{end_row}"
        rows = read_sheet_range_func(
            token,
            union_id,
            target_doc_id,
            sheet_id,
            cell_range,
            app_key=app_key,
            app_secret=app_secret,
        )
        if not rows:
            break
        for row in rows:
            key = _extract_dedupe_key(row, key_indices, min_index)
            if key:
                existing_keys.add(key)
        start_row = end_row + 1
    return existing_keys


# ---------------------------------------------------------------------------
# 验证 Job
# ---------------------------------------------------------------------------

def validate_job(config: SampleRegistrationConfig, start_date: str, end_date: str) -> list[str]:
    """验证完整运行参数。"""
    errors = validate_config(config)
    parsed_start: datetime | None = None
    parsed_end: datetime | None = None
    if not start_date:
        errors.append("请选择开始日期")
    else:
        try:
            parsed_start = datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            errors.append(f"开始日期格式错误，应为 YYYY-MM-DD，当前值：{start_date}")
    if not end_date:
        errors.append("请选择结束日期")
    else:
        try:
            parsed_end = datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            errors.append(f"结束日期格式错误，应为 YYYY-MM-DD，当前值：{end_date}")
    if parsed_start and parsed_end and parsed_start > parsed_end:
        errors.append("开始日期不能晚于结束日期")
    return errors


# ---------------------------------------------------------------------------
# 店铺匹配
# ---------------------------------------------------------------------------

def check_store_matches(
    config: SampleRegistrationConfig,
    username: str,
    password: str,
    *,
    base_url: str = DEFAULT_MABANG_BASE_URL,
    progress: ProgressCallback | None = None,
) -> StoreMatchSummary:
    """登录马帮，获取店铺列表并执行匹配。"""
    _emit(progress, "正在登录马帮...")
    with MabangClient(base_url, verify_ssl=False, timeout_seconds=240) as client:
        client.login(username, password)
        _emit(progress, "正在获取马帮店铺列表...")
        shops = list_order_shops(client)
        _emit(progress, f"获取到 {len(shops)} 个马帮店铺")

    mabang_shops = [
        {"shopId": s.shop_id, "shopName": s.shop_name, "platformId": s.platform_id, "platformName": s.platform_name}
        for s in shops
    ]
    summary = match_configured_groups(list(config.groups), mabang_shops)
    _emit(progress, f"匹配完成：成功 {summary.resolved_count} 个，失败 {summary.unresolved_count} 个")
    return summary


def write_match_outputs(output_dir: Path, summary: StoreMatchSummary) -> None:
    """写入匹配结果 Excel。"""
    import pandas as pd

    if summary.resolved_rows:
        df = pd.DataFrame(summary.resolved_rows)
        df.to_excel(str(output_dir / "店铺匹配结果.xlsx"), index=False)
    if summary.unresolved_rows:
        df = pd.DataFrame(summary.unresolved_rows)
        df.to_excel(str(output_dir / "未匹配店铺.xlsx"), index=False)


# ---------------------------------------------------------------------------
# 主运行函数
# ---------------------------------------------------------------------------

def run_registration(
    config: SampleRegistrationConfig,
    start_date: str,
    end_date: str,
    *,
    allow_unmatched: bool = False,
    base_url: str = DEFAULT_MABANG_BASE_URL,
    username: str = "",
    password: str = "",
    dingtalk_app_key: str = "",
    dingtalk_app_secret: str = "",
    dingtalk_user_id: str = "",
    progress: ProgressCallback | None = None,
) -> SampleRegistrationResult:
    """执行网红寄样登记。"""
    # 1. 校验
    errors = validate_job(config, start_date, end_date)
    if errors:
        raise ValueError("配置校验失败：\n" + "\n".join(errors))

    cfg = config.normalized()
    groups = list(cfg.groups)
    if cfg.enable_target_update:
        missing: list[str] = []
        if not clean_text(dingtalk_app_key):
            missing.append("钉钉 AppKey")
        if not clean_text(dingtalk_app_secret):
            missing.append("钉钉 AppSecret")
        if not clean_text(dingtalk_user_id):
            missing.append("钉钉操作人 UserID")
        if missing:
            raise ValueError("钉钉同步参数缺失，请先到设置页配置：" + "、".join(missing))

    # 2. 构建输出目录
    date_label = start_date.replace("-", "")
    if start_date != end_date:
        date_label = f"{start_date.replace('-','')}_{end_date.replace('-','')}"
    output_dir = _build_output_dir(Path(cfg.output_dir) if cfg.output_dir else Path.home() / "Desktop", date_label)
    _emit(progress, f"输出目录：{output_dir}")

    # 3. 登录马帮
    _emit(progress, "正在登录马帮...")
    with MabangClient(base_url, verify_ssl=False, timeout_seconds=240) as client:
        client.login(username, password)
        _emit(progress, "登录成功")

        # 4. 获取店铺列表
        _emit(progress, "正在获取马帮店铺列表...")
        shops = list_order_shops(client)
        _emit(progress, f"获取到 {len(shops)} 个马帮店铺")

        # 5. 店铺匹配
        mabang_shops = [
            {"shopId": s.shop_id, "shopName": s.shop_name, "platformId": s.platform_id, "platformName": s.platform_name}
            for s in shops
        ]
        summary = match_configured_groups(groups, mabang_shops)
        write_match_outputs(output_dir, summary)

        if summary.has_unresolved:
            if not allow_unmatched:
                raise UnmatchedStoreError(summary)
            _emit(progress, f"警告：{summary.unresolved_count} 个店铺未匹配，将跳过")

        # 6. 逐组处理
        start_time = f"{start_date} 00:00:00"
        end_time = f"{end_date} 23:59:59"

        all_records: list[dict[str, str]] = []
        group_frames: dict[str, list[dict[str, str]]] = {}

        for group in groups:
            shop_ids = summary.group_shop_ids.get(group.leader, [])
            if not shop_ids:
                _emit(progress, f"组长「{group.leader}」无匹配店铺，跳过")
                group_frames[group.leader] = []
                continue

            _emit(progress, f"组长「{group.leader}」：{len(shop_ids)} 个店铺")

            # 查询订单
            orders = fetch_orders_with_retry(
                client, shop_ids, start_time, end_time,
                progress=lambda msg: _emit(progress, f"  [{group.leader}] {msg}"),
            )

            # 过滤样品订单
            sample_orders = [o for o in orders if is_sample_order(o)]
            _emit(progress, f"  样品订单：{len(sample_orders)}/{len(orders)}")

            # 获取商品明细
            items = fetch_order_items(
                client, sample_orders,
                progress=lambda msg: _emit(progress, f"  [{group.leader}] {msg}"),
            )

            # 构建输出记录
            records = [build_output_record(item) for item in items]
            group_frames[group.leader] = records
            all_records.extend(records)
            _emit(progress, f"  [{group.leader}] 导出 {len(records)} 条")

        # 7. 导出 Excel
        import pandas as pd

        for group in groups:
            records = group_frames.get(group.leader, [])
            filename = _safe_filename(group.output_file_name or f"{group.leader}.xlsx")
            filepath = output_dir / filename
            if records:
                df = pd.DataFrame(records)
                # 排序
                if "排序时间" in df.columns:
                    df = df.sort_values(["排序时间", "订单编号", "SKU"], ascending=[False, False, True], kind="stable")
                    df = df.drop(columns=["排序时间"])
                df.to_excel(str(filepath), index=False, sheet_name="Recovered_Sheet1")
            else:
                # 空文件也要生成
                df = pd.DataFrame(columns=OUTPUT_COLUMNS)
                df.to_excel(str(filepath), index=False, sheet_name="Recovered_Sheet1")
            _emit(progress, f"已导出：{filename}")

        # 执行汇总
        summary_records = []
        for group in groups:
            records = group_frames.get(group.leader, [])
            summary_records.append({
                "组长": group.leader,
                "店铺数": len(summary.group_shop_ids.get(group.leader, [])),
                "导出行数": len(records),
            })
        pd.DataFrame(summary_records).to_excel(str(output_dir / "执行汇总.xlsx"), index=False)

    # 8. 钉钉在线写入
    online_append_count = 0
    if cfg.enable_target_update and cfg.target_doc_id:
        _emit(progress, "正在同步到钉钉在线表格...")
        try:
            online_append_count = _sync_to_dingtalk(
                cfg.target_doc_id,
                groups,
                group_frames,
                progress,
                app_key=dingtalk_app_key,
                app_secret=dingtalk_app_secret,
                user_id=dingtalk_user_id,
            )
        except Exception as exc:
            from backend.core.dingtalk_workbook import DingTalkCredentialsError
            if isinstance(exc, DingTalkCredentialsError):
                _emit(progress, f"钉钉凭证未配置，已跳过在线同步：{exc}")
            else:
                _emit(progress, f"钉钉同步失败：{exc}")
                raise RuntimeError(f"钉钉同步失败：{exc}") from exc
    elif cfg.enable_target_update and not cfg.target_doc_id:
        _emit(progress, "已勾选在线同步，但未填写登记表格 ID，已跳过在线同步")

    # 9. 汇总
    result = SampleRegistrationResult(
        processed_count=len(all_records),
        online_append_count=online_append_count,
        unmatched_store_count=summary.unresolved_count,
        output_directory=str(output_dir),
        resolved_count=summary.resolved_count,
        group_count=len(groups),
        output_files=[str(output_dir / _safe_filename(g.output_file_name or f"{g.leader}.xlsx")) for g in groups],
    )
    _emit(progress, f"任务完成：共 {result.processed_count} 条记录，在线追加 {result.online_append_count} 条")
    return result


# ---------------------------------------------------------------------------
# 钉钉同步
# ---------------------------------------------------------------------------

def _sync_to_dingtalk(
    target_doc_id: str,
    groups: list[SampleGroup],
    group_frames: dict[str, list[dict[str, str]]],
    progress: ProgressCallback | None,
    *,
    app_key: str,
    app_secret: str,
    user_id: str,
) -> int:
    """同步数据到钉钉在线表格。"""
    from backend.core.dingtalk_workbook import (
        DingTalkCredentialsError,
        get_access_token,
        get_union_id,
        read_sheet_range,
        append_sheet_rows,
        get_sheet_meta_by_name,
    )

    if not (clean_text(app_key) and clean_text(app_secret) and clean_text(user_id)):
        raise DingTalkCredentialsError("缺少钉钉 AppKey/AppSecret/UserID，请先在本流程配置中填写并保存")
    token = get_access_token(app_key, app_secret)
    union_id = get_union_id(token, user_id)

    total_appended = 0

    for group in groups:
        records = group_frames.get(group.leader, [])
        if not records:
            continue

        target_sheet = group.target_sheet or group.leader
        _emit(progress, f"同步组长「{group.leader}」到 Sheet「{target_sheet}」...")

        # 检查 Sheet 是否存在
        meta = get_sheet_meta_by_name(
            token,
            union_id,
            target_doc_id,
            target_sheet,
            app_key=app_key,
            app_secret=app_secret,
        )
        if not meta:
            raise RuntimeError(f"Sheet「{target_sheet}」不存在")
        sheet_id = clean_text(meta.get("id") or meta.get("sheetId"))
        if not sheet_id:
            raise RuntimeError(f"Sheet「{target_sheet}」缺少 sheet id")

        # 读取已有数据用于去重
        existing_keys: set[str] = set()
        try:
            existing_keys = _read_existing_dingtalk_keys(
                read_sheet_range,
                token,
                union_id,
                target_doc_id,
                sheet_id,
                meta,
                app_key=app_key,
                app_secret=app_secret,
            )
            _emit(progress, f"  已读取已有去重键 {len(existing_keys)} 个")
        except Exception as exc:
            _emit(progress, f"  读取已有数据失败：{exc}")
            raise RuntimeError(f"读取 Sheet「{target_sheet}」已有数据失败：{exc}") from exc

        # 构建新行
        new_rows: list[list[Any]] = []
        pending_keys: set[str] = set()
        for record in records:
            # 构建去重 key
            key_parts = []
            for col in TARGET_DEDUPE_COLUMNS:
                val = resolve_field_value(record, col)
                key_parts.append(clean_text(val).upper())
            dedupe_key = "||".join(key_parts)
            if dedupe_key in existing_keys or dedupe_key in pending_keys:
                continue
            pending_keys.add(dedupe_key)

            # 构建行数据
            row: list[Any] = []
            for col in TARGET_FILL_COLUMNS:
                if col == "寄样日期":
                    val = resolve_register_date(record)
                else:
                    val = resolve_field_value(record, col)
                row.append(val)
            new_rows.append(row)

        if new_rows:
            # 分批写入
            batch_size = 500
            appended_count = 0
            for i in range(0, len(new_rows), batch_size):
                batch = new_rows[i:i + batch_size]
                append_sheet_rows(
                    token,
                    union_id,
                    target_doc_id,
                    sheet_id,
                    batch,
                    app_key=app_key,
                    app_secret=app_secret,
                )
                appended_count += len(batch)
            total_appended += appended_count
            _emit(progress, f"  新增 {appended_count} 条（去重 {len(records) - len(new_rows)} 条）")
        else:
            _emit(progress, f"  无新增数据（全部已存在）")

    return total_appended
