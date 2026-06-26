"""网红寄样登记 — 配置模型、清洗和校验。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_PROCESS_NAME = "网红寄样登记用户版"
DEFAULT_MABANG_BASE_URL = "https://900853.private.mabangerp.com"

DEFAULT_OUTPUT_COLUMNS: list[str] = [
    "付款时间", "订单编号", "店铺名", "SKU", "商品中文名称", "商品总成本", "重量",
]
DEFAULT_TARGET_FILL_COLUMNS: list[str] = [
    "寄样日期", "订单号", "店铺", "SKU", "中文名", "商品总成本", "重量",
]
DEFAULT_TARGET_DEDUPE_COLUMNS: list[str] = ["订单号", "SKU"]

_FIELD_VALUE_ALIASES: dict[str, tuple[str, ...]] = {
    "寄样日期": ("寄样日期", "付款时间", "创建时间"),
    "付款时间": ("付款时间", "寄样日期", "创建时间"),
    "创建时间": ("创建时间", "付款时间", "寄样日期"),
    "订单号": ("订单号", "订单编号"),
    "订单编号": ("订单编号", "订单号"),
    "店铺": ("店铺", "店铺名"),
    "店铺名": ("店铺名", "店铺"),
    "中文名": ("中文名", "商品中文名称"),
    "商品中文名称": ("商品中文名称", "中文名"),
    "商品总成本": ("商品总成本", "商品单个成本"),
    "商品单个成本": ("商品单个成本", "商品总成本"),
    "重量": ("重量", "订单重量"),
    "订单重量": ("订单重量", "重量"),
}

_SAFE_FILENAME_RE = re.compile(r'^[^\\/:*?"<>|\x00-\x1f]+$')


# ---------------------------------------------------------------------------
# 清洗工具
# ---------------------------------------------------------------------------

def clean_text(value: Any) -> str:
    """清洗文本：去除零宽字符、规范化空白。"""
    text = str(value or "")
    text = text.replace("​", "").replace("\xa0", " ")
    text = text.replace("\r", " ").replace("\n", " ")
    return " ".join(text.split()).strip()


def dedupe_preserve_order(values: list[str]) -> list[str]:
    """去重并保持顺序。"""
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        cleaned = clean_text(item)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def safe_filename(name: str) -> bool:
    """检查是否为安全文件名。"""
    if not name or not name.strip():
        return False
    if not _SAFE_FILENAME_RE.match(name):
        return False
    # 禁止目录穿越
    if ".." in name or "/" in name or "\\" in name:
        return False
    return True


def parse_bool(value: Any, default: bool = False) -> bool:
    """解析前端/配置里的布尔值，避免字符串 'false' 被 bool() 当成 True。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = clean_text(value).lower()
    if text in {"1", "true", "yes", "y", "on", "开启", "启用", "是"}:
        return True
    if text in {"0", "false", "no", "n", "off", "关闭", "禁用", "否"}:
        return False
    return default


def resolve_field_value(record: dict[str, str], target_col: str) -> str:
    """通过别名映射从 record 中解析目标列的值。"""
    aliases = _FIELD_VALUE_ALIASES.get(target_col, (target_col,))
    for alias in aliases:
        val = clean_text(record.get(alias, ""))
        if val:
            return val
    return ""


def resolve_register_date(record: dict[str, str]) -> str:
    """解析寄样日期，提取 YYYY-MM-DD 部分。"""
    raw = resolve_field_value(record, "寄样日期")
    if not raw:
        return ""
    # 提取日期部分
    match = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return match.group(1) if match else raw[:10]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SampleGroup:
    """单个组长配置。"""
    leader: str = ""
    target_sheet: str = ""
    output_file_name: str = ""
    stores: list[str] = field(default_factory=list)

    def normalized(self) -> SampleGroup:
        leader = clean_text(self.leader)
        target_sheet = clean_text(self.target_sheet) or leader
        output_file_name = clean_text(self.output_file_name)
        if not output_file_name and leader:
            output_file_name = f"{leader}.xlsx"
        stores = dedupe_preserve_order(self.stores)
        return SampleGroup(
            leader=leader,
            target_sheet=target_sheet,
            output_file_name=output_file_name,
            stores=stores,
        )


@dataclass
class SampleRegistrationConfig:
    """寄样登记完整配置。"""
    version: int = 1
    target_doc_id: str = ""
    enable_target_update: bool = False
    dingtalk_operator_name: str = ""
    output_dir: str = ""
    groups: list[SampleGroup] = field(default_factory=list)

    def normalized(self) -> SampleRegistrationConfig:
        groups = [g.normalized() for g in self.groups if clean_text(g.leader)]
        return SampleRegistrationConfig(
            version=self.version,
            target_doc_id=clean_text(self.target_doc_id),
            enable_target_update=self.enable_target_update,
            dingtalk_operator_name=clean_text(self.dingtalk_operator_name),
            output_dir=clean_text(self.output_dir),
            groups=groups,
        )

# ---------------------------------------------------------------------------
# 配置存储
# ---------------------------------------------------------------------------

def _config_dir() -> Path:
    return Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming") / "HQYLAutomation"


def config_path() -> Path:
    return _config_dir() / "sample_registration.json"


def load_config(path: Path | None = None) -> SampleRegistrationConfig:
    """加载配置，文件不存在时返回空配置。"""
    target = path or config_path()
    if not target.exists():
        return SampleRegistrationConfig()
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except Exception:
        return SampleRegistrationConfig()
    if not isinstance(data, dict):
        return SampleRegistrationConfig()
    return _config_from_dict(data)


def save_config(config: SampleRegistrationConfig, path: Path | None = None) -> Path:
    """保存配置，原子替换。"""
    normalized = config.normalized()
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _config_to_dict(normalized)
    # 写入临时文件后原子替换
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(target))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def _config_from_dict(data: dict[str, Any]) -> SampleRegistrationConfig:
    groups_raw = data.get("groups") or []
    groups: list[SampleGroup] = []
    for item in groups_raw:
        if not isinstance(item, dict):
            continue
        stores_raw = item.get("stores") or []
        if isinstance(stores_raw, str):
            stores = [s for s in stores_raw.splitlines() if s.strip()]
        elif isinstance(stores_raw, list):
            stores = [str(s) for s in stores_raw]
        else:
            stores = []
        groups.append(SampleGroup(
            leader=str(item.get("leader") or ""),
            target_sheet=str(item.get("target_sheet") or ""),
            output_file_name=str(item.get("output_file_name") or ""),
            stores=stores,
        ))
    return SampleRegistrationConfig(
        version=int(data.get("version") or 1),
        target_doc_id=str(data.get("target_doc_id") or ""),
        enable_target_update=parse_bool(data.get("enable_target_update"), False),
        dingtalk_operator_name=str(
            data.get("dingtalk_operator_name")
            or data.get("dingtalk_user_name")
            or data.get("operator_name")
            or ""
        ),
        output_dir=str(data.get("output_dir") or ""),
        groups=groups,
    ).normalized()


def _config_to_dict(config: SampleRegistrationConfig, *, reveal_secret: bool = True) -> dict[str, Any]:
    return asdict(config)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def validate_config(config: SampleRegistrationConfig) -> list[str]:
    """校验配置，返回错误列表（空 = 合法）。"""
    errors: list[str] = []
    cfg = config.normalized()

    if cfg.enable_target_update and not cfg.target_doc_id:
        errors.append("开启在线写入时必须填写登记表格 ID")
    if cfg.enable_target_update and not cfg.dingtalk_operator_name:
        errors.append("开启同步到钉钉在线表格时，需要填写操作人姓名")

    if not cfg.groups:
        errors.append("至少需要配置一个组长")
        return errors

    leaders_seen: set[str] = set()
    filenames_seen: set[str] = set()

    for i, group in enumerate(cfg.groups, 1):
        prefix = f"组长「{group.leader}」" if group.leader else f"第 {i} 个组长"

        if not group.leader:
            errors.append(f"{prefix}：组长名不能为空")
            continue

        if group.leader in leaders_seen:
            errors.append(f"{prefix}：组长名重复")
        leaders_seen.add(group.leader)

        if not group.stores:
            errors.append(f"{prefix}：至少需要配置一个店铺")

        if cfg.enable_target_update and not group.target_sheet:
            errors.append(f"{prefix}：开启在线写入时必须填写目标 Sheet")

        if group.output_file_name:
            if not safe_filename(group.output_file_name):
                errors.append(f"{prefix}：输出文件名包含非法字符「{group.output_file_name}」")
            elif group.output_file_name in filenames_seen:
                errors.append(f"{prefix}：输出文件名「{group.output_file_name}」重复")
            filenames_seen.add(group.output_file_name)

    return errors


def parse_group_from_dict(value: dict[str, Any]) -> SampleGroup:
    """从 dict 解析并规范化单个组长配置。"""
    stores_raw = value.get("stores") or []
    if isinstance(stores_raw, str):
        stores = [s for s in stores_raw.splitlines() if s.strip()]
    elif isinstance(stores_raw, list):
        stores = [str(s) for s in stores_raw]
    else:
        stores = []
    return SampleGroup(
        leader=str(value.get("leader") or ""),
        target_sheet=str(value.get("target_sheet") or ""),
        output_file_name=str(value.get("output_file_name") or ""),
        stores=stores,
    ).normalized()
