import copy
import json
import tempfile
import unittest
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from scripts.repair_vn_id_ads_sheet import (
    PENDING_STATUS, SheetRepairError, apply_patch_batch, apply_patch_plan, build_patch_plan, main, snapshot_cells,
)


def fixture(site="id", store="印尼049", spend=790.329, updated="2026-09-10", balance=1508227):
    values = [[""] * 25 for _ in range(8)]
    formulas = [[""] * 25 for _ in range(8)]
    values[1][:18] = [store, "负责人", site.upper(), spend, 0, 0, 0.001, 0, "#N/A", "#N/A", 400, 1600,
                       "#N/A", balance, 5000000, "需充值", updated, "original payment history"]
    formulas[1][4] = "=D2/6"
    formulas[1][5] = "=ROUND(E2*10,-4)" if site == "id" else "=ROUND(E2*10,-2)"
    formulas[1][15] = '=IF(O2<=0,"无需充值","需充值")'
    if site == "id":
        for row in range(1, 7):
            values[row][20:22] = [400, 1600]
    else:
        for row, threshold in enumerate([0.01, 50.01, 100.01, 200.01, 300.01], 1):
            values[row][22:25] = [threshold, 400, 1600]
    return {"site": site, "doc_id": "test-doc", "meta": {"sheetid": "test-sheet", "sheet_name": site},
            "retrieved_at": "2026-09-11T11:00:00", "values": values, "formulas": formulas}


class FakeAdapter:
    def __init__(self, snapshot):
        self.cells = snapshot_cells(snapshot)
        self.writes = []
        self.fail_cell = None
        self.corrupt_cell = None
        self.concurrent_after_hold = None
        self.prevent_recalculation = False
        self.read_calls = 0
        self.write_sets = []
        self.concurrent_after_repair = None

    def read_cells(self, doc_id, sheet_id, cells):
        self.read_calls += 1
        return {cell: copy.deepcopy(self.cells.get(cell, {"value": "", "formula": ""})) for cell in cells}

    def write_cells(self, doc_id, sheet_id, cells):
        self.write_sets.append(copy.deepcopy(cells))
        for cell, content in cells.items():
            self.write_cell(doc_id, sheet_id, cell, **content)
        if any(not cell.startswith("P") for cell in cells) and self.concurrent_after_repair:
            self.cells.update(copy.deepcopy(self.concurrent_after_repair))

    def write_cell(self, doc_id, sheet_id, cell, **content):
        if cell == self.fail_cell:
            raise RuntimeError("credential-must-never-be-echoed")
        self.writes.append((cell, dict(content)))
        if "formula" in content:
            formula = content["formula"]
            row = int(cell[1:])
            d = Decimal(str(self.cells[f"D{row}"]["value"] or 0))
            value = d / 7 if cell.startswith("E") else (d / 7 * 10).quantize(Decimal("1E4") if "-4" in formula else Decimal("1E2"), rounding=ROUND_HALF_UP)
            self.cells[cell] = {"formula": formula, "value": float(value)}
        else:
            self.cells[cell] = {"formula": "", "value": content["value"]}
        if not self.prevent_recalculation:
            row = int(cell[1:])
            d = Decimal(str(self.cells[f"D{row}"]["value"] or 0))
            eformula = self.cells[f"E{row}"]["formula"]
            if eformula:
                daily = d / Decimal(eformula.split("/")[-1])
                self.cells[f"E{row}"]["value"] = float(daily)
                fformula = self.cells[f"F{row}"]["formula"]
                if fformula:
                    quantum = Decimal("1E4") if "-4" in fformula else Decimal("1E2")
                    self.cells[f"F{row}"]["value"] = int((daily * 10).quantize(quantum, rounding=ROUND_HALF_UP))
        if cell == self.corrupt_cell:
            self.cells[cell]["value"] = "unexpected"
        if cell.startswith("P") and self.concurrent_after_hold:
            self.cells.update(copy.deepcopy(self.concurrent_after_hold))


class SheetRepairTests(unittest.TestCase):
    def test_verified_history_uses_date_and_amount_conditions_and_preserves_balance_and_manual_o(self):
        snapshot = fixture()
        plan = build_patch_plan(snapshot)
        record = plan["records"][0]
        patches = {p["cell"]: p for p in record["patches"]}
        self.assertEqual(patches["D2"]["value"], 790329)
        self.assertEqual(patches["D2"]["conditions"]["Q2"]["value"], "2026-09-10")
        self.assertIn("2026-09-03至2026-09-09", patches["R2"]["value"])
        self.assertTrue(patches["R2"]["value"].startswith("original payment history"))
        self.assertEqual(patches["E2"]["formula"], "=D2/7")
        self.assertNotIn("N2", patches)
        self.assertNotIn("O2", patches)
        self.assertIn("manual_recharge_override_preserved", record["reasons"])
        self.assertFalse(plan["quota_review"]["thresholds_complete_and_increasing"])
        self.assertEqual(plan["quota_review"]["action"], "tiers_retired_no_payment_dependency")
        self.assertFalse(any(reason.startswith("quota_") for reason in record["reasons"]))
        self.assertFalse(any(p["cell"].startswith(("I", "J", "K", "L", "M")) for p in record["patches"]))

    def test_vn_verified_amount_is_not_candidate_times_1000(self):
        plan = build_patch_plan(fixture("vn", "越南072", 248.21, "2026-09-09", 299.331))
        patches = {p["cell"]: p for p in plan["records"][0]["patches"]}
        self.assertEqual(patches["D2"]["value"], 6471781)
        self.assertNotEqual(patches["D2"]["value"], 248210)
        self.assertTrue(plan["quota_review"]["thresholds_complete_and_increasing"])
        self.assertEqual(plan["quota_review"]["action"], "tiers_retired_no_payment_dependency")
        self.assertFalse(any(reason.startswith("quota_") for reason in plan["records"][0]["reasons"]))

    def test_retired_tier_clearing_rejects_old_plan_and_new_plan_preserves_empty_cells(self):
        for site in ("id", "vn"):
            for apply in (apply_patch_plan, apply_patch_batch):
                with self.subTest(site=site, apply=apply.__name__):
                    snapshot = fixture(site, "印尼100" if site == "id" else "越南100", 7000000)
                    old_plan = build_patch_plan(snapshot)
                    cleared = copy.deepcopy(snapshot)
                    retired_cells = set(old_plan["quota_review"]["expected_before"]) | {"I2", "J2", "M2"}
                    for cell in retired_cells:
                        column, row = ord(cell[0]) - ord("A"), int(cell[1:]) - 1
                        cleared["values"][row][column] = ""
                        cleared["formulas"][row][column] = ""
                    adapter = FakeAdapter(cleared)
                    conflict = apply(old_plan, adapter)
                    self.assertEqual(conflict["applied_records"], 0)
                    self.assertEqual(adapter.writes, [])

                    plan = build_patch_plan(cleared)
                    self.assertEqual(plan["quota_review"]["action"], "tiers_retired_no_payment_dependency")
                    self.assertEqual(set(plan["quota_review"]["expected_before"]),
                                     set(old_plan["quota_review"]["expected_before"]))
                    self.assertFalse(any(reason.startswith("quota_") for reason in plan["records"][0]["reasons"]))
                    self.assertNotIn("manual_configuration_required", json.dumps(plan))
                    result = apply(plan, adapter)
                    self.assertEqual(result["applied_records"], 1)
                    self.assertEqual(result["payment_reenabled_rows"], 0)
                    self.assertEqual(adapter.cells["P2"]["value"], PENDING_STATUS)
                    self.assertTrue(all(adapter.cells[cell] == {"value": "", "formula": ""}
                                        for cell in retired_cells))
                    self.assertEqual(adapter.cells["K2"]["value"], 400)
                    self.assertEqual(adapter.cells["L2"]["value"], 1600)

    def test_changed_snapshot_date_or_spend_disables_backfill(self):
        for snapshot in (fixture(updated="2026-09-11"), fixture(spend=790.330)):
            record = build_patch_plan(snapshot)["records"][0]
            self.assertFalse(any(p["cell"].startswith("D") for p in record["patches"]))
            self.assertIn("verified_backfill_precondition_does_not_match_snapshot", record["reasons"])

    def test_additional_verified_id_history_keeps_low_real_amount_and_date_guard(self):
        for store, old, verified in (("印尼081", 857.414, 857414), ("印尼175", 4.363, 4363),
                                     ("印尼262", 462.911, 462911), ("印尼266", 856.619, 856619),
                                     ("印尼270", 858.074, 858074)):
            with self.subTest(store=store):
                snapshot = fixture(store=store, spend=old)
                adapter = FakeAdapter(snapshot)
                result = apply_patch_batch(build_patch_plan(snapshot), adapter)
                self.assertEqual(result["applied_records"], 1)
                self.assertEqual(adapter.cells["D2"]["value"], verified)
                self.assertEqual(adapter.cells["P2"]["value"], PENDING_STATUS)
                self.assertEqual(adapter.cells["N2"]["value"], 1508227)
                changed = build_patch_plan(fixture(store=store, spend=old, updated="2026-09-11"))
                self.assertFalse(any(p["cell"] == "D2" for p in changed["records"][0]["patches"]))

    def test_duplicate_registration_does_not_receive_verified_backfill(self):
        snapshot = fixture()
        snapshot["values"][2][:18] = list(snapshot["values"][1][:18])
        snapshot["formulas"][2][:18] = list(snapshot["formulas"][1][:18])
        records = build_patch_plan(snapshot)["records"]
        self.assertEqual(len(records), 2)
        self.assertTrue(all(not any(p["cell"].startswith("D") for p in r["patches"]) for r in records))

    def test_candidates_and_missing_values_hold_without_scaling_and_zero_is_not_missing(self):
        for amount in ("", 640.23, 0):
            record = build_patch_plan(fixture(store="印尼253", spend=amount, updated="", balance=""))["records"][0]
            self.assertEqual(record["patches"][0]["value"], PENDING_STATUS)
            self.assertFalse(any(p["cell"].startswith(("D", "N", "O")) for p in record["patches"]))
            self.assertIn("balance_missing_or_invalid", record["reasons"])
            self.assertEqual("expense_missing_or_invalid" in record["reasons"], amount == "")

    def test_apply_compares_all_inputs_holds_first_and_reads_back(self):
        snapshot = fixture()
        adapter = FakeAdapter(snapshot)
        result = apply_patch_plan(build_patch_plan(snapshot), adapter)
        self.assertEqual(result["applied_records"], 1)
        self.assertEqual(adapter.writes[0], ("P2", {"value": PENDING_STATUS}))
        self.assertEqual(adapter.cells["D2"]["value"], 790329)
        self.assertEqual(adapter.cells["P2"]["value"], PENDING_STATUS)
        self.assertEqual(adapter.cells["O2"]["value"], 5000000)
        self.assertEqual(adapter.cells["N2"]["value"], 1508227)
        self.assertEqual(result["payment_reenabled_rows"], 0)

    def test_any_concurrent_row_or_quota_change_prevents_first_write(self):
        for cell in ("D2", "Q2", "O2", "A2", "U3"):
            snapshot = fixture()
            adapter = FakeAdapter(snapshot)
            adapter.cells[cell]["value"] = "changed"
            result = apply_patch_plan(build_patch_plan(snapshot), adapter)
            self.assertEqual(result["results"][0]["status"], "conflict")
            self.assertEqual(adapter.writes, [])

    def test_concurrent_change_after_hold_cannot_overwrite_new_date_or_expense(self):
        snapshot = fixture()
        adapter = FakeAdapter(snapshot)
        adapter.concurrent_after_hold = {"Q2": {"value": "2026-09-12", "formula": ""}}
        result = apply_patch_plan(build_patch_plan(snapshot), adapter)
        self.assertEqual(result["results"][0]["status"], "failed_pending")
        self.assertEqual(len(adapter.writes), 1)
        self.assertEqual(adapter.cells["D2"]["value"], 790.329)
        self.assertEqual(adapter.cells["Q2"]["value"], "2026-09-12")

    def test_partial_failure_never_rolls_back_or_restores_payment_status(self):
        snapshot = fixture()
        adapter = FakeAdapter(snapshot)
        adapter.fail_cell = "R2"
        result = apply_patch_plan(build_patch_plan(snapshot), adapter)
        self.assertEqual(result["results"][0]["status"], "failed_pending")
        self.assertFalse(result["rollback_attempted"])
        self.assertEqual(adapter.cells["D2"]["value"], 790329)
        self.assertEqual(adapter.cells["P2"]["value"], PENDING_STATUS)
        self.assertNotIn("credential-must-never-be-echoed", json.dumps(result))

    def test_existing_budget_formula_must_also_recalculate_before_success(self):
        snapshot = fixture()
        adapter = FakeAdapter(snapshot)
        adapter.prevent_recalculation = True
        result = apply_patch_plan(build_patch_plan(snapshot), adapter)
        self.assertEqual(result["results"][0]["status"], "failed_pending")
        self.assertEqual(adapter.cells["P2"]["value"], PENDING_STATUS)

    def test_failed_hold_or_readback_is_reported_and_stops(self):
        for mode in ("write", "readback"):
            snapshot = fixture()
            adapter = FakeAdapter(snapshot)
            if mode == "write": adapter.fail_cell = "P2"
            else: adapter.corrupt_cell = "P2"
            result = apply_patch_plan(build_patch_plan(snapshot), adapter)
            self.assertEqual(result["results"][0]["status"], "failed_hold_unconfirmed")
            self.assertFalse(any(cell == "D2" for cell, _ in adapter.writes))

    def test_batch_limits_and_protected_cell_plan_validation(self):
        snapshot = fixture()
        plan = build_patch_plan(snapshot)
        for maximum in (0, 31):
            with self.assertRaises(SheetRepairError):
                apply_patch_plan(plan, FakeAdapter(snapshot), max_records=maximum)
        patch = {"cell": "O2", "expected_before": plan["records"][0]["expected_before"]["O2"], "value": 1}
        plan["records"][0]["patches"].append(patch)
        with self.assertRaises(SheetRepairError):
            apply_patch_plan(plan, FakeAdapter(snapshot))

    def test_mutated_plan_cannot_scale_unverified_expense(self):
        snapshot = fixture()
        plan = build_patch_plan(snapshot)
        for patch in plan["records"][0]["patches"]:
            if patch["cell"] == "D2":
                patch["value"] = 790329000
        with self.assertRaises(SheetRepairError):
            apply_patch_plan(plan, FakeAdapter(snapshot))

    def test_cli_only_writes_local_dry_run_report(self):
        with tempfile.TemporaryDirectory() as directory:
            src, dst = Path(directory) / "snapshot.json", Path(directory) / "review.json"
            src.write_text(json.dumps(fixture()), encoding="utf-8")
            self.assertEqual(main(["--snapshot", str(src), "--output", str(dst)]), 0)
            result = json.loads(dst.read_text(encoding="utf-8"))
            self.assertEqual(result["mode"], "dry_run")
            self.assertEqual(result["plans"][0]["summary"]["payment_reenabled_rows"], 0)


def batch_fixture(count=2):
    snapshot = fixture(store="印尼100", spend=7000000)
    while len(snapshot["values"]) < count + 2:
        snapshot["values"].append([""] * 25)
        snapshot["formulas"].append([""] * 25)
    template = list(snapshot["values"][1][:18])
    for row in range(2, count + 2):
        snapshot["values"][row - 1][:18] = list(template)
        snapshot["values"][row - 1][0] = f"印尼{row + 100}"
        snapshot["formulas"][row - 1][4] = f"=D{row}/6"
        snapshot["formulas"][row - 1][5] = f"=ROUND(E{row}*10,-4)"
        snapshot["formulas"][row - 1][15] = f'=IF(O{row}<=0,"无需充值","需充值")'
    return snapshot


class SheetRepairBatchTests(unittest.TestCase):
    def test_batch_uses_three_reads_and_two_write_sets(self):
        snapshot = batch_fixture(3)
        adapter = FakeAdapter(snapshot)
        result = apply_patch_batch(build_patch_plan(snapshot), adapter)
        self.assertEqual(result["status"], "applied_pending")
        self.assertEqual(result["applied_records"], 3)
        self.assertEqual(result["read_calls"], 3)
        self.assertEqual(adapter.read_calls, 3)
        self.assertEqual(len(adapter.write_sets), 2)
        self.assertEqual(set(adapter.write_sets[0]), {"P2", "P3", "P4"})
        self.assertEqual(set(adapter.write_sets[1]), {"E2", "E3", "E4"})
        self.assertTrue(all(adapter.cells[f"P{row}"]["value"] == PENDING_STATUS for row in (2, 3, 4)))

    def test_one_preflight_conflict_stops_entire_batch_before_writing(self):
        for cell in ("A3", "C3", "D3", "N3", "O3", "Q3", "U5"):
            snapshot = batch_fixture()
            adapter = FakeAdapter(snapshot)
            adapter.cells[cell]["value"] = "changed"
            result = apply_patch_batch(build_patch_plan(snapshot), adapter)
            self.assertEqual(result["status"], "conflict")
            self.assertEqual(result["read_calls"], 1)
            self.assertEqual(adapter.write_sets, [])
            self.assertEqual(result["applied_records"], 0)

    def test_batch_rechecks_all_inputs_after_payment_hold(self):
        snapshot = fixture()
        adapter = FakeAdapter(snapshot)
        adapter.concurrent_after_hold = {"Q2": {"value": "2026-09-12", "formula": ""}}
        result = apply_patch_batch(build_patch_plan(snapshot), adapter)
        self.assertEqual(result["status"], "failed_pending")
        self.assertEqual(len(adapter.write_sets), 1)
        self.assertEqual(adapter.cells["D2"]["value"], 790.329)
        self.assertEqual(adapter.cells["Q2"]["value"], "2026-09-12")

    def test_batch_final_check_detects_protected_input_changes(self):
        for cell in ("A2", "C2", "N2", "O2", "Q2"):
            snapshot = batch_fixture()
            adapter = FakeAdapter(snapshot)
            adapter.concurrent_after_repair = {cell: {"value": "concurrent change", "formula": ""}}
            result = apply_patch_batch(build_patch_plan(snapshot), adapter)
            self.assertEqual(result["status"], "failed_pending")
            self.assertEqual(adapter.cells[cell]["value"], "concurrent change")
            self.assertEqual(result["next_index"], 0)
            self.assertFalse(result["rollback_attempted"])

    def test_partial_batch_failure_keeps_holds_and_does_not_rollback(self):
        snapshot = batch_fixture()
        adapter = FakeAdapter(snapshot)
        adapter.fail_cell = "E3"
        result = apply_patch_batch(build_patch_plan(snapshot), adapter)
        self.assertEqual(result["status"], "failed_pending")
        self.assertEqual(adapter.cells["E2"]["formula"], "=D2/7")
        self.assertEqual(adapter.cells["E3"]["formula"], "=D3/6")
        self.assertEqual(adapter.cells["P2"]["value"], PENDING_STATUS)
        self.assertEqual(adapter.cells["P3"]["value"], PENDING_STATUS)
        self.assertTrue(result["may_be_partially_applied"])
        self.assertIn("E3", result["attempted_cells"])
        self.assertNotIn("credential-must-never-be-echoed", json.dumps(result))

    def test_partial_payment_hold_does_not_start_other_repairs(self):
        snapshot = batch_fixture()
        adapter = FakeAdapter(snapshot)
        adapter.fail_cell = "P3"
        result = apply_patch_batch(build_patch_plan(snapshot), adapter)
        self.assertEqual(result["status"], "failed_hold_unconfirmed")
        self.assertEqual(len(adapter.write_sets), 1)
        self.assertTrue(result["results"][0]["payment_block_confirmed"])
        self.assertFalse(result["results"][1]["payment_block_confirmed"])
        self.assertFalse(any(cell.startswith("E") for cell, _ in adapter.writes))

    def test_batch_rechecks_two_verified_amounts_without_touching_balances_or_o(self):
        for snapshot, expected in ((fixture(), 790329), (fixture("vn", "越南072", 248.21, "2026-09-09"), 6471781)):
            adapter = FakeAdapter(snapshot)
            result = apply_patch_batch(build_patch_plan(snapshot), adapter)
            self.assertEqual(result["applied_records"], 1)
            self.assertEqual(adapter.cells["D2"]["value"], expected)
            self.assertEqual(adapter.cells["O2"]["value"], 5000000)
            self.assertEqual(adapter.cells["N2"]["value"], 1508227)

    def test_batch_bound_is_thirty_and_next_index_skips_only_verified_batch(self):
        snapshot = batch_fixture(32)
        plan = build_patch_plan(snapshot)
        adapter = FakeAdapter(snapshot)
        result = apply_patch_batch(plan, adapter)
        self.assertEqual(result["applied_records"], 30)
        self.assertEqual(result["next_index"], 30)
        self.assertEqual(len(adapter.write_sets[0]), 30)
        self.assertEqual(adapter.cells["P32"]["value"], "需充值")
        result2 = apply_patch_batch(plan, adapter, start_index=30)
        self.assertEqual(result2["applied_records"], 2)
        for maximum in (0, 31, True):
            with self.assertRaises(SheetRepairError):
                apply_patch_batch(plan, adapter, max_records=maximum)

    def test_batch_rejects_unrecalculated_existing_budget_formula(self):
        snapshot = batch_fixture()
        adapter = FakeAdapter(snapshot)
        adapter.prevent_recalculation = True
        result = apply_patch_batch(build_patch_plan(snapshot), adapter)
        self.assertEqual(result["status"], "failed_pending")
        self.assertGreater(result["read_calls"], 3)
        self.assertTrue(result["stopped"])


if __name__ == "__main__":
    unittest.main()
