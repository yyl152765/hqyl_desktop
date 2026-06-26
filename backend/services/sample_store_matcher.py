"""网红寄样登记 — 店铺匹配器。

将用户填写的店铺名与马帮店铺列表进行三级匹配：
1. 精确匹配（清洗后完全一致）
2. 规范化匹配（去除前缀、空格、前导零后一致）
3. 作用域匹配（TK前缀段匹配，去除本土/企业后一致）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from backend.services.sample_registration_config import SampleGroup, clean_text


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MabangShopCandidate:
    shop_id: str
    shop_name: str
    platform_id: str = ""
    platform_name: str = ""


@dataclass(slots=True)
class StoreMatchSummary:
    group_shop_ids: dict[str, list[str]] = field(default_factory=dict)
    resolved_rows: list[dict[str, str]] = field(default_factory=list)
    unresolved_rows: list[dict[str, str]] = field(default_factory=list)

    @property
    def has_unresolved(self) -> bool:
        return len(self.unresolved_rows) > 0

    @property
    def resolved_count(self) -> int:
        return len(self.resolved_rows)

    @property
    def unresolved_count(self) -> int:
        return len(self.unresolved_rows)


# ---------------------------------------------------------------------------
# 店铺名规范化
# ---------------------------------------------------------------------------

_KNOWN_PREFIXES = ("TK泰国本土", "TK跨境泰国", "TK印尼本土", "TK跨境菲律宾", "TK泰国")


def _collapse_leading_zeros(text: str, prefixes: tuple[str, ...] = _KNOWN_PREFIXES) -> str:
    """在已知前缀后去除前导零。"""
    for prefix in prefixes:
        if text.startswith(prefix):
            rest = text[len(prefix):]
            rest = re.sub(r'^0+(\d)', r'\1', rest)
            return prefix + rest
    return text


def normalize_store_key(store_name: str) -> str:
    """规范化店铺名：去除前缀段、空格，规范化前导零，大写。"""
    text = clean_text(store_name)
    if not text:
        return ""
    # 去除 - 前的前缀
    if "-" in text:
        text = text.split("-", 1)[1]
    text = text.replace(" ", "")
    text = _collapse_leading_zeros(text)
    return text.upper()


def normalize_store_scope_key(store_name: str) -> str:
    """作用域规范化：提取主要段，去除本土/企业，规范化前导零，大写。"""
    text = clean_text(store_name)
    if not text:
        return ""
    upper = text.upper()
    tk_idx = upper.find("TK")

    if tk_idx >= 0:
        # 有 TK 前缀：取 TK 前缀段之后的内容
        after_tk = text[tk_idx:]
        if "-" in after_tk:
            # 取第一个 - 后面的部分（跳过 TK前缀段-）
            text = after_tk.split("-", 1)[1]
        else:
            text = after_tk[len("TK"):]
    elif "-" in text:
        # 无 TK 前缀：取最后一个 - 后面的部分
        text = text.rsplit("-", 1)[1]
    # else: 无分隔符，使用原文本

    text = text.replace(" ", "")
    # 取第一个 - 分隔段（如果还有 -）
    if "-" in text:
        text = text.split("-", 1)[0]
    text = _collapse_leading_zeros(text)
    text = text.replace("本土", "").replace("企业", "")
    return text.upper()


# ---------------------------------------------------------------------------
# 构建候选索引
# ---------------------------------------------------------------------------

def build_shop_candidates(mabang_shops: list[Any]) -> list[MabangShopCandidate]:
    """从马帮店铺列表构建候选集合。"""
    candidates: list[MabangShopCandidate] = []
    seen_ids: set[str] = set()
    for shop in mabang_shops:
        if isinstance(shop, dict):
            shop_id = clean_text(shop.get("shopId") or shop.get("shop_id") or shop.get("id") or "")
            shop_name = clean_text(shop.get("shopName") or shop.get("shop_name") or shop.get("name") or "")
            platform_id = clean_text(shop.get("platformId") or shop.get("platform_id") or "")
            platform_name = clean_text(shop.get("platformName") or shop.get("platform_name") or "")
        else:
            shop_id = clean_text(getattr(shop, "shopId", "") or getattr(shop, "shop_id", "") or getattr(shop, "id", ""))
            shop_name = clean_text(getattr(shop, "shopName", "") or getattr(shop, "shop_name", "") or getattr(shop, "name", ""))
            platform_id = clean_text(getattr(shop, "platformId", "") or getattr(shop, "platform_id", ""))
            platform_name = clean_text(getattr(shop, "platformName", "") or getattr(shop, "platform_name", ""))
        if not shop_id or not shop_name or shop_id in seen_ids:
            continue
        seen_ids.add(shop_id)
        candidates.append(MabangShopCandidate(
            shop_id=shop_id,
            shop_name=shop_name,
            platform_id=platform_id,
            platform_name=platform_name,
        ))
    return candidates


def build_shop_indexes(
    candidates: list[MabangShopCandidate],
) -> tuple[dict[str, list[MabangShopCandidate]], dict[str, list[MabangShopCandidate]], dict[str, list[MabangShopCandidate]]]:
    """构建三级索引：精确、规范化、作用域。"""
    exact: dict[str, list[MabangShopCandidate]] = {}
    normalized: dict[str, list[MabangShopCandidate]] = {}
    scope: dict[str, list[MabangShopCandidate]] = {}

    for c in candidates:
        key_exact = clean_text(c.shop_name)
        if key_exact:
            exact.setdefault(key_exact, []).append(c)

        key_norm = normalize_store_key(c.shop_name)
        if key_norm:
            normalized.setdefault(key_norm, []).append(c)

        key_scope = normalize_store_scope_key(c.shop_name)
        if key_scope:
            scope.setdefault(key_scope, []).append(c)

    return exact, normalized, scope


# ---------------------------------------------------------------------------
# 匹配逻辑
# ---------------------------------------------------------------------------

def _resolve_store(
    store_name: str,
    exact_index: dict[str, list[MabangShopCandidate]],
    normalized_index: dict[str, list[MabangShopCandidate]],
    scope_index: dict[str, list[MabangShopCandidate]],
) -> tuple[str, list[MabangShopCandidate]]:
    """三级解析单个店铺名。"""
    # 1. 精确匹配
    key = clean_text(store_name)
    matches = exact_index.get(key, [])
    if len(matches) == 1:
        return "exact", matches

    # 2. 规范化匹配
    key = normalize_store_key(store_name)
    matches = normalized_index.get(key, [])
    if len(matches) == 1:
        return "normalized", matches

    # 3. 作用域匹配
    key = normalize_store_scope_key(store_name)
    matches = scope_index.get(key, [])
    if len(matches) == 1:
        return "scope", matches

    # 返回所有候选（0 或多个）
    all_matches = matches if matches else []
    return "none", all_matches


def match_configured_groups(
    groups: list[SampleGroup],
    mabang_shops: list[Any],
) -> StoreMatchSummary:
    """对配置中的所有组长和店铺执行匹配。"""
    candidates = build_shop_candidates(mabang_shops)
    exact_idx, norm_idx, scope_idx = build_shop_indexes(candidates)

    summary = StoreMatchSummary()

    for group in groups:
        if not group.leader:
            continue
        seen_stores: set[str] = set()
        for store_name in group.stores:
            cleaned = clean_text(store_name)
            if not cleaned or cleaned in seen_stores:
                continue
            seen_stores.add(cleaned)

            method, matches = _resolve_store(store_name, exact_idx, norm_idx, scope_idx)

            if len(matches) == 1:
                m = matches[0]
                summary.resolved_rows.append({
                    "组长": group.leader,
                    "用户填写店铺": cleaned,
                    "马帮店铺": m.shop_name,
                    "店铺ID": m.shop_id,
                    "匹配方式": method,
                })
                shop_ids = summary.group_shop_ids.setdefault(group.leader, [])
                if m.shop_id not in shop_ids:
                    shop_ids.append(m.shop_id)
            elif len(matches) > 1:
                summary.unresolved_rows.append({
                    "组长": group.leader,
                    "用户填写店铺": cleaned,
                    "原因": "匹配到多个马帮店铺",
                    "候选马帮店铺": "; ".join(f"{m.shop_name}({m.shop_id})" for m in matches),
                })
            else:
                summary.unresolved_rows.append({
                    "组长": group.leader,
                    "用户填写店铺": cleaned,
                    "原因": "未匹配到马帮店铺",
                    "候选马帮店铺": "",
                })

    return summary


def format_unresolved_message(summary: StoreMatchSummary, max_rows: int = 30) -> str:
    """格式化未匹配店铺的可读消息。"""
    if not summary.has_unresolved:
        return ""
    lines = ["以下店铺未能匹配到马帮店铺："]
    for i, row in enumerate(summary.unresolved_rows[:max_rows]):
        leader = row.get("组长", "")
        store = row.get("用户填写店铺", "")
        reason = row.get("原因", "")
        candidates = row.get("候选马帮店铺", "")
        line = f"  {leader} / {store} — {reason}"
        if candidates:
            line += f"（候选：{candidates}）"
        lines.append(line)
    remaining = len(summary.unresolved_rows) - max_rows
    if remaining > 0:
        lines.append(f"  ... 还有 {remaining} 个未匹配店铺")
    return "\n".join(lines)
