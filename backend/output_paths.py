"""Resolve output roots independently for each supported tool and country."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from backend import config_store


COUNTRY_NAMES = {"TH": "泰国", "PH": "菲律宾", "MY": "马来西亚", "ID": "印尼", "VN": "越南"}
SUPPORTED_TOOLS = frozenset({"lazada_ads_data", "lazada_bill_detail", "lz_warehouse_inventory"})


def _scope(tool: str, country: str = "TH") -> tuple[str, str]:
    tool = str(tool or "").strip()
    if tool not in SUPPORTED_TOOLS:
        raise ValueError("不支持该功能的独立输出目录")
    if tool == "lz_warehouse_inventory":
        return tool, ""
    if tool == "lazada_ads_data":
        return tool, "TH"
    raw_country = str(country or "TH").strip()
    code = raw_country.upper()
    if code not in COUNTRY_NAMES:
        code = next((key for key, name in COUNTRY_NAMES.items() if raw_country == name), "")
    if not code:
        raise ValueError("请选择支持的账单国家")
    return tool, code


def directory_scope_key(tool: str, country: str = "TH", kind: str = "output") -> str:
    tool, code = _scope(tool, country)
    if kind not in {"output", "screenshot"} or (kind == "screenshot" and tool != "lazada_bill_detail"):
        raise ValueError("该功能不支持所选目录类型")
    return ":".join(part for part in (tool, code, kind) if part)


def default_output_directories(tool: str, country: str = "TH") -> dict[str, str]:
    tool, code = _scope(tool, country)
    root = Path(config_store.desktop_path()) / "log"
    if tool == "lazada_ads_data":
        return {"output_dir": str(root / "lz泰国广告数据")}
    if tool == "lz_warehouse_inventory":
        return {"output_dir": str(root / "lz各仓库存")}
    name = COUNTRY_NAMES[code]
    return {
        "output_dir": str(root / f"Lazada{name}账单明细"),
        "screenshot_root": str(root / f"Lazada{name}账单明细截图"),
    }


def resolve_output_directories(settings: Any, tool: str, country: str = "TH") -> dict[str, str]:
    defaults = default_output_directories(tool, country)
    overrides = getattr(settings, "tool_output_dirs", {})
    overrides = overrides if isinstance(overrides, dict) else {}
    result: dict[str, str] = {}
    for field, default in defaults.items():
        kind = "screenshot" if field == "screenshot_root" else "output"
        key = directory_scope_key(tool, country, kind)
        result[field] = str(overrides.get(key) or "").strip() or default
        result[f"default_{field}"] = default
    return result
