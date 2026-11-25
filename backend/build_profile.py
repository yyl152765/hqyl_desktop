"""Capabilities of the source build and the immutable profile shipped in an EXE."""
from __future__ import annotations

from functools import wraps
import json
import os
from pathlib import Path
import sys


UNAVAILABLE_MESSAGE = "精简版不包含此功能，请使用完整版。"
INTERNAL_TOOLS = {
    "superbrowser_process": (
        "shopee_ads", "lazada_withdrawal_statistics", "temu_fee_refund_export",
        "temu_financial_export", "temu_on_sale_export", "temu_activity_export",
        "temu_balance_statistics", "lazada_balance_statistics",
    ),
    "mabang_process": (
        "bigseller_sync", "bigseller_item_id_query", "bigseller_sku_benchmark",
        "bigseller_claim_query", "bigseller_link_query",
    ),
}


def profile_details(name: str) -> dict:
    if name not in ("full", "limited"):
        raise ValueError(f"Unknown build profile: {name}")
    limited = name == "limited"
    return {
        "name": name,
        "label": "精简版" if limited else "完整版",
        "omitted_dependencies": list(INTERNAL_TOOLS) if limited else [],
        "unavailable_tools": [tool for tools in INTERNAL_TOOLS.values() for tool in tools] if limited else [],
    }


def current_profile() -> dict:
    if getattr(sys, "frozen", False):
        # A packaged capability cannot be enabled by changing an environment variable.
        path = Path(sys._MEIPASS) / "build_profile.json"
        name = json.loads(path.read_text("utf-8"))["name"]
    else:
        name = os.environ.get("HQYL_BUILD_PROFILE", "full")
    return profile_details(name)


def require_dependency(name: str) -> None:
    if name in current_profile()["omitted_dependencies"]:
        raise RuntimeError(UNAVAILABLE_MESSAGE)


def requires_internal_runtime(function):
    """Reject unavailable bridge operations before reading accounts or starting jobs."""
    @wraps(function)
    def guarded(*args, **kwargs):
        if current_profile()["name"] == "limited":
            return {"ok": False, "unavailable": True, "error": UNAVAILABLE_MESSAGE}
        return function(*args, **kwargs)
    return guarded


def annotate_app_info(info: dict) -> dict:
    profile = current_profile()
    info["app"]["build_profile"] = profile
    for tool in info["tools"]:
        if tool["key"] in profile["unavailable_tools"]:
            tool.update(status="unavailable", unavailable_reason=UNAVAILABLE_MESSAGE)
    return info
