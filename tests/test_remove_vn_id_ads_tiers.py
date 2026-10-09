"""Offline fake-adapter tests for the narrowly scoped tier removal plan."""

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.remove_vn_id_ads_tiers import (
    TierRemovalError, apply_removal_batch, apply_source_table_removal,
    build_removal_plan, main,
)


def snapshot(site="vn", shops=3):
    size = max(9, shops + 3)
    values, formulas = [[""] * 25 for _ in range(size)], [[""] * 25 for _ in range(size)]
    values[0][:18] = ["" if site == "vn" else "店铺名", "店长", "站点", "过去7天消耗金额",
                       "平均日消耗", "预估10天广告费", "平均日消耗美金", "月充值美金",
                       "实际7天限额", "实际月限额", "当前7天限额", "当前月限额", "判断是否提档",
                       "广告余额（运营填写）", "实际充值", "判断是否充值", "更新时间(RPA)", "执行状态(RPA)"]
    for index in range(shops):
        number = index + 2
        name = ("越南" if site == "vn" else "印尼") + f"{index + 1:03d}"
        values[number - 1][:18] = [name, "运营", site.upper(), 10000, 10000 / 7, 14300,
                                  1, 30, 400, 1600, 300, 1200, "需提档", 2000,
                                  5000000 if index == 0 else 10000, "待核验", "2026-09-11", "历史支付日志，必须保留"]
        for col, formula in {
            4: f"=D{number}/7", 5: f"=ROUND(E{number}*10,-2)",
            6: f"=E{number}/26338", 8: f"=LOOKUP(G{number},W$2:W$6,X$2:X$6)",
            9: f"=LOOKUP(G{number},W$2:W$6,Y$2:Y$6)",
            12: f'=IF(I{number}>K{number},"需提档","无需提档")',
        }.items():
            formulas[number - 1][col] = formula
    source_start = 22 if site == "vn" else 19
    values[0][source_start:source_start + 3] = ["日消耗区间", "7天", "每月"]
    for index in range(1, 7):
        values[index][source_start:source_start + 3] = [index * 50 + 0.01, 400, 1600]
    if site == "vn":
        values[6][source_start:source_start + 3] = ["", 2100, 10000]
    else:
        for index in range(1, 7):
            values[index][source_start] = ""
    # Cells outside registered rows must remain, including other-country data.
    values[-2][8:10] = ["未注册行用户数据", "不要清空"]
    values[-2][12] = "用户备注"
    values[-1][:3] = ["其他国家店铺", "运营", "ID" if site == "vn" else "VN"]
    values[-1][8:10] = [123, 456]
    values[-1][12] = "其他国家规则"
    return {"site": site, "title": "越南" if site == "vn" else "印尼", "doc_id": "doc",
            "meta": {"sheetid": "sheet", "row_count": size, "last_non_empty_row": size},
            "retrieved_at": "2026-09-11T17:25:00+08:00", "values": values, "formulas": formulas}


class FakeAdapter:
    def __init__(self, original):
        self.cells = {}
        for number, row in enumerate(original["values"], 1):
            for col in range(25):
                self.cells[f"{chr(65 + col)}{number}"] = {
                    "value": copy.deepcopy(row[col]), "formula": original["formulas"][number - 1][col],
                }
        self.reads = []
        self.writes = []
        self.before_read = {}
        self.fail_write = None

    def read_cells(self, doc_id, sheet_id, cells):
        if (doc_id, sheet_id) != ("doc", "sheet"):
            raise AssertionError("Wrong document identity")
        self.reads.append(list(cells))
        callback = self.before_read.get(len(self.reads))
        if callback:
            callback(self)
        return {cell: copy.deepcopy(self.cells.get(cell, {"value": "", "formula": ""})) for cell in cells}

    def write_cells(self, doc_id, sheet_id, patches):
        if (doc_id, sheet_id) != ("doc", "sheet"):
            raise AssertionError("Wrong document identity")
        self.writes.append(copy.deepcopy(patches))
        for index, (cell, content) in enumerate(patches.items()):
            if cell.startswith("P"):
                raise AssertionError("Payment status may never be written")
            if content != {"value": ""}:
                raise AssertionError("Only explicit clearing is supported")
            self.cells[cell] = {"value": "", "formula": ""}
            if cell.startswith(("I", "J")):
                dependent = "M" + cell[1:]
                if self.cells[dependent]["formula"]:
                    self.cells[dependent]["value"] = "重算后的结果"
            if self.fail_write == len(self.writes) and index == 0:
                raise RuntimeError("SDK request could contain secret-token")


def clear_all_rows(plan, adapter):
    index = 0
    while index < len(plan["records"]):
        result = apply_removal_batch(plan, adapter, start_index=index)
        if result["stopped"]:
            raise AssertionError(result)
        index = result["next_index"]


class TierRemovalTests(unittest.TestCase):
    def test_plan_preserves_original_snapshot_and_known_site_bounds(self):
        for site, source_range in (("vn", "W1:Y7"), ("id", "T1:V7")):
            with self.subTest(site=site):
                original = snapshot(site)
                before = copy.deepcopy(original)
                plan = build_removal_plan(original)
                self.assertEqual(before, original)
                self.assertEqual(before, plan["source_snapshot"])
                self.assertEqual(3, len(plan["records"]))
                self.assertEqual(source_range, plan["source_table"]["range"])
                self.assertEqual(21, len(plan["source_table"]["clear_cells"]))
                self.assertEqual(["I1", "J1", "M1"], plan["header_clear_cells"])
                self.assertEqual({f"{c}2" for c in "ABCDEFGHIJKLMNOPQR"}, set(plan["records"][0]["expected_before"]))
                self.assertEqual({"I2", "J2", "M2"}, set(plan["records"][0]["clear_cells"]))

    def test_unrecognized_headers_or_source_content_are_rejected(self):
        for col, value in ((8, "实际平台额度"), (12, "用户备注"), (22, "任意用户表"), (23, "其他数据")):
            with self.subTest(col=col):
                original = snapshot()
                original["values"][0][col] = value
                with self.assertRaises(TierRemovalError):
                    build_removal_plan(original)
        for value in ("用户姓名", -1):
            original = snapshot()
            original["values"][1][22] = value
            with self.assertRaises(TierRemovalError):
                build_removal_plan(original)
        original = snapshot()
        original["formulas"][1][22] = "=D2"
        with self.assertRaises(TierRemovalError):
            build_removal_plan(original)

    def test_incomplete_formula_snapshot_and_wrong_site_are_rejected(self):
        original = snapshot()
        original["formulas"].pop()
        with self.assertRaises(TierRemovalError):
            build_removal_plan(original)
        original = snapshot()
        original["site"] = "th"
        with self.assertRaises(TierRemovalError):
            build_removal_plan(original)

    def test_three_reads_two_writes_clear_decision_before_its_inputs(self):
        original = snapshot()
        plan, adapter = build_removal_plan(original), FakeAdapter(original)
        before = copy.deepcopy(adapter.cells)
        result = apply_removal_batch(plan, adapter)
        self.assertFalse(result["stopped"], result)
        self.assertEqual(3, result["read_calls"])
        self.assertEqual(2, result["write_set_calls"])
        self.assertTrue(all(cell.startswith("M") for cell in adapter.writes[0]))
        self.assertTrue(all(cell.startswith(("I", "J")) for cell in adapter.writes[1]))
        changed = {cell for cell in before if before[cell] != adapter.cells[cell]}
        self.assertEqual({f"{col}{row}" for col in "IJM" for row in range(2, 5)}, changed)
        for record in plan["records"]:
            for col in "DE FKLNOPQR".replace(" ", ""):
                cell = f"{col}{record['row']}"
                self.assertEqual(before[cell], adapter.cells[cell])

    def test_batches_cannot_exceed_thirty_registered_rows(self):
        original = snapshot(shops=35)
        plan, adapter = build_removal_plan(original), FakeAdapter(original)
        first = apply_removal_batch(plan, adapter)
        self.assertEqual(30, first["applied_records"])
        self.assertEqual(30, first["next_index"])
        premature = apply_source_table_removal(plan, adapter)
        self.assertTrue(premature["stopped"])
        self.assertEqual(0, premature["write_set_calls"])
        final = apply_removal_batch(plan, adapter, start_index=30)
        self.assertEqual(5, final["applied_records"])
        for bound in (0, 31, True):
            with self.assertRaises(TierRemovalError):
                apply_removal_batch(plan, adapter, max_records=bound)

    def test_preflight_protects_every_registered_a_to_r_value_and_formula(self):
        for column in "ABCDEFGHIJKLMNOPQR":
            for field in ("value", "formula"):
                with self.subTest(column=column, field=field):
                    original = snapshot()
                    plan, adapter = build_removal_plan(original), FakeAdapter(original)
                    adapter.cells[f"{column}2"][field] = "concurrent edit"
                    result = apply_removal_batch(plan, adapter)
                    self.assertTrue(result["stopped"])
                    self.assertEqual([], adapter.writes)

    def test_mid_batch_conflict_stops_without_compensation_or_payment_write(self):
        original = snapshot()
        plan, adapter = build_removal_plan(original), FakeAdapter(original)
        adapter.before_read[2] = lambda fake: fake.cells["O2"].update(value=9000000)
        result = apply_removal_batch(plan, adapter)
        self.assertTrue(result["stopped"])
        self.assertEqual(1, len(adapter.writes))
        self.assertTrue(result["may_be_partially_applied"])
        self.assertFalse(result["rollback_attempted"])
        self.assertEqual(9000000, adapter.cells["O2"]["value"])
        self.assertEqual("待核验", adapter.cells["P2"]["value"])

    def test_partial_sdk_failure_is_not_retried_and_does_not_expose_details(self):
        original = snapshot()
        plan, adapter = build_removal_plan(original), FakeAdapter(original)
        adapter.fail_write = 1
        result = apply_removal_batch(plan, adapter)
        self.assertTrue(result["stopped"])
        self.assertEqual(1, len(adapter.writes))
        self.assertEqual(0, result["applied_records"])
        self.assertNotIn("secret-token", str(result))
        self.assertFalse(result["rollback_attempted"])

    def test_source_table_cannot_be_removed_before_registered_rows(self):
        original = snapshot()
        plan, adapter = build_removal_plan(original), FakeAdapter(original)
        result = apply_source_table_removal(plan, adapter)
        self.assertTrue(result["stopped"])
        self.assertEqual([], adapter.writes)

    def test_source_removed_last_including_vietnam_seventh_row_residue(self):
        for site in ("vn", "id"):
            with self.subTest(site=site):
                original = snapshot(site)
                plan, adapter = build_removal_plan(original), FakeAdapter(original)
                before = copy.deepcopy(adapter.cells)
                clear_all_rows(plan, adapter)
                result = apply_source_table_removal(plan, adapter)
                self.assertFalse(result["stopped"], result)
                self.assertEqual(3, result["read_calls"])
                self.assertEqual(2, result["write_set_calls"])
                allowed = set(plan["header_clear_cells"]) | set(plan["source_table"]["clear_cells"])
                allowed.update(cell for record in plan["records"] for cell in record["clear_cells"])
                for cell, old in before.items():
                    if cell in allowed:
                        self.assertEqual({"value": "", "formula": ""}, adapter.cells[cell], cell)
                    else:
                        self.assertEqual(old, adapter.cells[cell], cell)
                if site == "vn":
                    self.assertEqual("", adapter.cells["X7"]["value"])
                    self.assertEqual("", adapter.cells["Y7"]["value"])

    def test_source_or_protected_data_change_blocks_source_removal(self):
        for cell in ("W2", "X7", "K2", "L2", "P2", "R2"):
            with self.subTest(cell=cell):
                original = snapshot()
                plan, adapter = build_removal_plan(original), FakeAdapter(original)
                clear_all_rows(plan, adapter)
                adapter.cells[cell]["value"] = "concurrent edit"
                write_count = len(adapter.writes)
                result = apply_source_table_removal(plan, adapter)
                self.assertTrue(result["stopped"])
                self.assertEqual(write_count, len(adapter.writes))

    def test_new_registration_in_known_blank_row_blocks_source_removal(self):
        original = snapshot()
        plan, adapter = build_removal_plan(original), FakeAdapter(original)
        clear_all_rows(plan, adapter)
        adapter.cells["A7"]["value"] = "越南999"
        adapter.cells["C7"]["value"] = "VN"
        result = apply_source_table_removal(plan, adapter)
        self.assertTrue(result["stopped"])
        self.assertEqual(0, result["write_set_calls"])

    def test_tampered_scope_conditions_or_hash_never_reaches_adapter(self):
        for mutate in (
            lambda p: p["records"][0]["clear_cells"].append("K2"),
            lambda p: p["records"][0]["expected_before"]["D2"].update(value=0),
            lambda p: p["source_table"]["clear_cells"].append("P2"),
            lambda p: p.update(source_snapshot_sha256="tampered"),
        ):
            original = snapshot()
            plan, adapter = build_removal_plan(original), FakeAdapter(original)
            mutate(plan)
            with self.assertRaises(TierRemovalError):
                apply_removal_batch(plan, adapter)
            self.assertEqual([], adapter.reads)
            self.assertEqual([], adapter.writes)

    def test_cli_is_dryrun_preserves_backup_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "snapshot.json", Path(directory) / "plan.json"
            original = snapshot()
            source.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            saved = source.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, main(["--snapshot", str(source), "--output", str(output)]))
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("dry_run", report["mode"])
            self.assertEqual(original, report["plans"][0]["source_snapshot"])
            self.assertEqual(saved, source.read_bytes())
            with self.assertRaises(TierRemovalError):
                main(["--snapshot", str(source), "--output", str(output)])


if __name__ == "__main__":
    unittest.main()
