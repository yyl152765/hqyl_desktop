"""sample_registration_config 单元测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.services.sample_registration_config import (
    SampleGroup,
    SampleRegistrationConfig,
    clean_text,
    dedupe_preserve_order,
    load_config,
    parse_bool,
    parse_group_from_dict,
    resolve_field_value,
    resolve_register_date,
    safe_filename,
    save_config,
    validate_config,
    _config_to_dict,
)


class CleanTextTests(unittest.TestCase):
    def test_strips_zero_width_space(self):
        self.assertEqual(clean_text("hello​world"), "helloworld")

    def test_normalizes_nbsp(self):
        self.assertEqual(clean_text("a\xa0b"), "a b")

    def test_collapses_whitespace(self):
        self.assertEqual(clean_text("  a  b  c  "), "a b c")

    def test_empty_input(self):
        self.assertEqual(clean_text(None), "")
        self.assertEqual(clean_text(""), "")


class DedupePreserveOrderTests(unittest.TestCase):
    def test_deduplicates(self):
        self.assertEqual(dedupe_preserve_order(["a", "b", "a", "c"]), ["a", "b", "c"])

    def test_cleans_items(self):
        self.assertEqual(dedupe_preserve_order([" a ", " a"]), ["a"])

    def test_skips_empties(self):
        self.assertEqual(dedupe_preserve_order(["", "  ", "a"]), ["a"])


class SafeFilenameTests(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(safe_filename("张三.xlsx"))

    def test_rejects_directory_traversal(self):
        self.assertFalse(safe_filename("../secret.xlsx"))
        self.assertFalse(safe_filename("a/b.xlsx"))

    def test_rejects_illegal_chars(self):
        self.assertFalse(safe_filename("a:b.xlsx"))
        self.assertFalse(safe_filename("a*b.xlsx"))

    def test_rejects_empty(self):
        self.assertFalse(safe_filename(""))
        self.assertFalse(safe_filename("  "))


class ParseBoolTests(unittest.TestCase):
    def test_accepts_boolean_values(self):
        self.assertTrue(parse_bool(True))
        self.assertFalse(parse_bool(False, True))

    def test_accepts_string_values(self):
        self.assertTrue(parse_bool("true"))
        self.assertTrue(parse_bool("开启"))
        self.assertFalse(parse_bool("false", True))
        self.assertFalse(parse_bool("关闭", True))

    def test_uses_default_for_missing_or_unknown(self):
        self.assertTrue(parse_bool(None, True))
        self.assertFalse(parse_bool("unknown", False))


class SampleGroupTests(unittest.TestCase):
    def test_normalized_fills_defaults(self):
        g = SampleGroup(leader="  张三  ").normalized()
        self.assertEqual(g.leader, "张三")
        self.assertEqual(g.target_sheet, "张三")
        self.assertEqual(g.output_file_name, "张三.xlsx")

    def test_normalized_dedupes_stores(self):
        g = SampleGroup(leader="A", stores=["店1", "店2", "店1"]).normalized()
        self.assertEqual(g.stores, ["店1", "店2"])

    def test_normalized_drops_empty_leader(self):
        cfg = SampleRegistrationConfig(groups=[
            SampleGroup(leader=""),
            SampleGroup(leader="B", stores=["s"]),
        ]).normalized()
        self.assertEqual(len(cfg.groups), 1)
        self.assertEqual(cfg.groups[0].leader, "B")


class ValidateConfigTests(unittest.TestCase):
    def test_valid_config(self):
        cfg = SampleRegistrationConfig(
            target_doc_id="doc123",
            enable_target_update=True,
            dingtalk_operator_name="王小妹",
            groups=[SampleGroup(leader="A", target_sheet="A", stores=["s1"])],
        )
        self.assertEqual(validate_config(cfg), [])

    def test_requires_doc_id_when_update_enabled(self):
        cfg = SampleRegistrationConfig(
            enable_target_update=True,
            target_doc_id="",
            dingtalk_operator_name="王小妹",
            groups=[SampleGroup(leader="A", stores=["s1"])],
        )
        errors = validate_config(cfg)
        self.assertTrue(any("登记表格 ID" in e for e in errors))

    def test_requires_dingtalk_operator_name_when_update_enabled(self):
        cfg = SampleRegistrationConfig(
            enable_target_update=True,
            target_doc_id="doc123",
            groups=[SampleGroup(leader="A", target_sheet="A", stores=["s1"])],
        )
        errors = validate_config(cfg)
        self.assertTrue(any("操作人姓名" in e for e in errors))

    def test_requires_groups(self):
        cfg = SampleRegistrationConfig(groups=[])
        errors = validate_config(cfg)
        self.assertTrue(any("至少需要配置一个组长" in e for e in errors))

    def test_requires_stores(self):
        cfg = SampleRegistrationConfig(
            target_doc_id="doc123",
            groups=[SampleGroup(leader="A", stores=[])],
        )
        errors = validate_config(cfg)
        self.assertTrue(any("至少需要配置一个店铺" in e for e in errors))

    def test_detects_duplicate_leader(self):
        cfg = SampleRegistrationConfig(
            target_doc_id="doc123",
            groups=[
                SampleGroup(leader="A", stores=["s1"]),
                SampleGroup(leader="A", stores=["s2"]),
            ],
        )
        errors = validate_config(cfg)
        self.assertTrue(any("组长名重复" in e for e in errors))

    def test_detects_duplicate_filename(self):
        cfg = SampleRegistrationConfig(
            target_doc_id="doc123",
            groups=[
                SampleGroup(leader="A", output_file_name="out.xlsx", stores=["s1"]),
                SampleGroup(leader="B", output_file_name="out.xlsx", stores=["s2"]),
            ],
        )
        errors = validate_config(cfg)
        self.assertTrue(any("输出文件名" in e and "重复" in e for e in errors))


class ConfigRoundTripTests(unittest.TestCase):
    def test_save_and_load(self):
        cfg = SampleRegistrationConfig(
            target_doc_id="doc123",
            enable_target_update=False,
            dingtalk_operator_name="王小妹",
            groups=[
                SampleGroup(leader="A", target_sheet="SheetA", stores=["店1", "店2"]),
                SampleGroup(leader="B", stores=["店3"]),
            ],
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            save_config(cfg, p)
            loaded = load_config(p)
            self.assertEqual(loaded.target_doc_id, "doc123")
            self.assertFalse(loaded.enable_target_update)
            self.assertEqual(loaded.dingtalk_operator_name, "王小妹")
            self.assertEqual(len(loaded.groups), 2)
            self.assertEqual(loaded.groups[0].leader, "A")
            self.assertEqual(loaded.groups[0].target_sheet, "SheetA")
            self.assertEqual(loaded.groups[1].target_sheet, "B")  # fallback
            self.assertEqual(loaded.groups[1].output_file_name, "B.xlsx")

    def test_public_config_contains_operator_name(self):
        cfg = SampleRegistrationConfig(
            dingtalk_operator_name="王小妹",
        )
        public = _config_to_dict(cfg, reveal_secret=False)
        self.assertEqual(public["dingtalk_operator_name"], "王小妹")
        self.assertNotIn("dingtalk_app_secret", public)

    def test_load_missing_file(self):
        cfg = load_config(Path("/nonexistent/path.json"))
        self.assertEqual(cfg.groups, [])
        self.assertEqual(cfg.target_doc_id, "")
        self.assertFalse(cfg.enable_target_update)

    def test_load_corrupted_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("not valid json{{{", encoding="utf-8")
            cfg = load_config(p)
            self.assertEqual(cfg.groups, [])


class FieldAliasTests(unittest.TestCase):
    def test_resolve_exact(self):
        self.assertEqual(resolve_field_value({"订单号": "ORD-1"}, "订单号"), "ORD-1")

    def test_resolve_alias(self):
        self.assertEqual(resolve_field_value({"订单编号": "ORD-1"}, "订单号"), "ORD-1")

    def test_resolve_missing(self):
        self.assertEqual(resolve_field_value({}, "订单号"), "")

    def test_resolve_register_date(self):
        self.assertEqual(
            resolve_register_date({"寄样日期": "2026-06-23 10:30:00"}),
            "2026-06-23",
        )

    def test_resolve_register_date_from_alias(self):
        self.assertEqual(
            resolve_register_date({"付款时间": "2026-06-23 10:30:00"}),
            "2026-06-23",
        )


class ParseGroupFromDictTests(unittest.TestCase):
    def test_parse_from_dict(self):
        g = parse_group_from_dict({
            "leader": "  张三  ",
            "target_sheet": "Sheet1",
            "output_file_name": "out.xlsx",
            "stores": "店A\n店B\n店A",
        })
        self.assertEqual(g.leader, "张三")
        self.assertEqual(g.stores, ["店A", "店B"])

    def test_parse_stores_as_list(self):
        g = parse_group_from_dict({"leader": "A", "stores": ["s1", "s2"]})
        self.assertEqual(g.stores, ["s1", "s2"])


if __name__ == "__main__":
    unittest.main()
