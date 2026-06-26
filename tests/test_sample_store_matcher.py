"""sample_store_matcher 单元测试。"""

from __future__ import annotations

import unittest

from backend.services.sample_registration_config import SampleGroup
from backend.services.sample_store_matcher import (
    MabangShopCandidate,
    StoreMatchSummary,
    build_shop_candidates,
    build_shop_indexes,
    format_unresolved_message,
    match_configured_groups,
    normalize_store_key,
    normalize_store_scope_key,
)


class NormalizeStoreKeyTests(unittest.TestCase):
    def test_strips_prefix_before_dash(self):
        result = normalize_store_key("TK泰国本土-ABC店001")
        self.assertEqual(result, "ABC店001")

    def test_removes_spaces(self):
        result = normalize_store_key("ABC 店 001")
        self.assertEqual(result, "ABC店001")

    def test_collapses_leading_zeros(self):
        # After splitting on "-", "ABC001" has no known prefix, so zeros are preserved
        result = normalize_store_key("TK泰国本土-ABC001")
        self.assertEqual(result, "ABC001")

    def test_uppercases(self):
        result = normalize_store_key("abc-shop")
        self.assertEqual(result, "SHOP")

    def test_no_dash(self):
        result = normalize_store_key("ABC店")
        self.assertEqual(result, "ABC店")


class NormalizeStoreScopeKeyTests(unittest.TestCase):
    def test_extracts_tk_segment(self):
        result = normalize_store_scope_key("TK泰国本土-ABC店001-something")
        self.assertEqual(result, "ABC店001")

    def test_removes_enterprise(self):
        result = normalize_store_scope_key("TK泰国1企业-ABC店")
        self.assertEqual(result, "ABC店")

    def test_no_tk_prefix(self):
        result = normalize_store_scope_key("some-prefix-ABC店")
        self.assertEqual(result, "ABC店")


class BuildShopCandidatesTests(unittest.TestCase):
    def test_from_dict_list(self):
        shops = [
            {"shopId": "1", "shopName": "Shop A"},
            {"shopId": "2", "shopName": "Shop B"},
        ]
        candidates = build_shop_candidates(shops)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0].shop_id, "1")

    def test_deduplicates_by_id(self):
        shops = [
            {"shopId": "1", "shopName": "Shop A"},
            {"shopId": "1", "shopName": "Shop A Again"},
        ]
        candidates = build_shop_candidates(shops)
        self.assertEqual(len(candidates), 1)

    def test_skips_empty(self):
        shops = [
            {"shopId": "", "shopName": "Shop A"},
            {"shopId": "1", "shopName": ""},
        ]
        candidates = build_shop_candidates(shops)
        self.assertEqual(len(candidates), 0)


class MatchConfiguredGroupsTests(unittest.TestCase):
    SHOPS = [
        {"shopId": "100", "shopName": "TK泰国本土-ABC店001"},
        {"shopId": "200", "shopName": "TK跨境泰国-XYZ店002"},
        {"shopId": "300", "shopName": "普通店铺"},
    ]

    def test_exact_match(self):
        groups = [SampleGroup(leader="组长A", stores=["普通店铺"])]
        summary = match_configured_groups(groups, self.SHOPS)
        self.assertEqual(summary.resolved_count, 1)
        self.assertEqual(summary.unresolved_count, 0)
        self.assertEqual(summary.group_shop_ids["组长A"], ["300"])

    def test_normalized_match(self):
        groups = [SampleGroup(leader="组长A", stores=["ABC店001"])]
        summary = match_configured_groups(groups, self.SHOPS)
        self.assertEqual(summary.resolved_count, 1)
        self.assertEqual(summary.group_shop_ids["组长A"], ["100"])

    def test_no_match(self):
        groups = [SampleGroup(leader="组长A", stores=["不存在的店铺"])]
        summary = match_configured_groups(groups, self.SHOPS)
        self.assertEqual(summary.resolved_count, 0)
        self.assertEqual(summary.unresolved_count, 1)
        self.assertIn("未匹配到马帮店铺", summary.unresolved_rows[0]["原因"])

    def test_multi_match(self):
        # 两个店铺规范化后都匹配到同一个 key
        shops = [
            {"shopId": "100", "shopName": "TK泰国本土-ABC"},
            {"shopId": "200", "shopName": "TK跨境泰国-ABC"},
        ]
        groups = [SampleGroup(leader="组长A", stores=["ABC"])]
        summary = match_configured_groups(groups, shops)
        # 精确匹配不命中，规范化匹配命中多个
        self.assertEqual(summary.unresolved_count, 1)
        self.assertIn("多个", summary.unresolved_rows[0]["原因"])

    def test_cross_group_shop_ids(self):
        groups = [
            SampleGroup(leader="组长A", stores=["普通店铺"]),
            SampleGroup(leader="组长B", stores=["普通店铺"]),
        ]
        summary = match_configured_groups(groups, self.SHOPS)
        self.assertEqual(summary.group_shop_ids["组长A"], ["300"])
        self.assertEqual(summary.group_shop_ids["组长B"], ["300"])

    def test_dedupes_shop_ids_within_group(self):
        # 同组两个不同写法但匹配到同一店铺
        shops = [{"shopId": "100", "shopName": "TK泰国本土-ABC店001"}]
        groups = [SampleGroup(leader="组长A", stores=["ABC店001", "TK泰国本土-ABC店001"])]
        summary = match_configured_groups(groups, shops)
        self.assertEqual(summary.group_shop_ids["组长A"], ["100"])


class FormatUnresolvedMessageTests(unittest.TestCase):
    def test_formats_message(self):
        summary = StoreMatchSummary(unresolved_rows=[
            {"组长": "A", "用户填写店铺": "店1", "原因": "未匹配到马帮店铺", "候选马帮店铺": ""},
            {"组长": "B", "用户填写店铺": "店2", "原因": "匹配到多个马帮店铺", "候选马帮店铺": "X(1); Y(2)"},
        ])
        msg = format_unresolved_message(summary)
        self.assertIn("店1", msg)
        self.assertIn("店2", msg)
        self.assertIn("X(1)", msg)

    def test_empty_when_no_unresolved(self):
        summary = StoreMatchSummary()
        self.assertEqual(format_unresolved_message(summary), "")


if __name__ == "__main__":
    unittest.main()
