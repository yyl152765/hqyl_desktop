"""Review and conditionally repair historical VN/ID advertising worksheets.

The CLI is always dry-run and only reads local snapshots. Live use requires an
explicitly injected adapter; this module contains no authentication or network
client. A repair never restores the payment-ready status.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Protocol


PLAN_VERSION = "1.0.0"
PENDING_STATUS = "待核验"
MAX_RECORDS_PER_APPLY = 30
_SITE_DIGITS = {"id": -4, "vn": -2}
_VERIFIED = {
    ("id", "印尼049"): {
        "old_amount": "790.329", "updated_at": "2026-09-10", "amount": 790329,
        "start_date": "2026-09-03", "end_date": "2026-09-09", "timezone": "GMT+7",
        "evidence": "../id_ads_audit_20260911/verified_findings.json",
    },
    ("vn", "越南072"): {
        "old_amount": "248.21", "updated_at": "2026-09-09", "amount": 6471781,
        "start_date": "2026-09-02", "end_date": "2026-09-08", "timezone": "GMT+7",
        "evidence": "vn072_source_20260902_08.json",
    },
    ("id", "印尼081"): {
        "old_amount": "857.414", "updated_at": "2026-09-10", "amount": 857414,
        "start_date": "2026-09-03", "end_date": "2026-09-09", "timezone": "GMT+7",
        "evidence": "../vn_id_ads_repair_20260911/historical_id/id081_verified.json",
    },
    ("id", "印尼175"): {
        "old_amount": "4.363", "updated_at": "2026-09-10", "amount": 4363,
        "start_date": "2026-09-03", "end_date": "2026-09-09", "timezone": "GMT+7",
        "evidence": "../vn_id_ads_repair_20260911/historical_id/id175_verified.json",
    },
    ("id", "印尼262"): {
        "old_amount": "462.911", "updated_at": "2026-09-10", "amount": 462911,
        "start_date": "2026-09-03", "end_date": "2026-09-09", "timezone": "GMT+7",
        "evidence": "../vn_id_ads_repair_20260911/historical_id/id262.json",
    },
    ("id", "印尼266"): {
        "old_amount": "856.619", "updated_at": "2026-09-10", "amount": 856619,
        "start_date": "2026-09-03", "end_date": "2026-09-09", "timezone": "GMT+7",
        "evidence": "../vn_id_ads_repair_20260911/historical_id/id266.json",
    },
    ("id", "印尼270"): {
        "old_amount": "858.074", "updated_at": "2026-09-10", "amount": 858074,
        "start_date": "2026-09-03", "end_date": "2026-09-09", "timezone": "GMT+7",
        "evidence": "../vn_id_ads_repair_20260911/historical_id/id270.json",
    },
}


class SheetRepairError(ValueError):
    """A repair plan or adapter result is not safe to apply."""


class SheetAdapter(Protocol):
    def read_cells(self, doc_id: str, sheet_id: str, cells: list[str]) -> dict[str, dict]:
        """Return {address: {'value': raw_value, 'formula': formula_or_empty}}."""

    def write_cell(self, doc_id: str, sheet_id: str, cell: str, **content: Any) -> None:
        """Write exactly one value= or formula=; a value write clears a formula."""

    def write_cells(self, doc_id: str, sheet_id: str, cells: dict[str, dict]) -> None:
        """Write only specified cells, each containing value OR formula.

        An adapter may combine contiguous cells into ranges, but must never
        include an unspecified cell to bridge a gap between requested cells.
        """


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float, Decimal)) and isinstance(right, (int, float, Decimal)):
        lnum, rnum = _decimal(left), _decimal(right)
        return lnum is not None and rnum is not None and lnum == rnum
    return left == right


def _column(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def snapshot_cells(snapshot: dict) -> dict[str, dict]:
    """Convert the audit's values/formulas matrices into adapter-shaped cells."""
    result = {}
    values, formulas = snapshot["values"], snapshot["formulas"]
    for row in range(max(len(values), len(formulas))):
        row_values = values[row] if row < len(values) else []
        row_formulas = formulas[row] if row < len(formulas) else []
        for col in range(max(25, len(row_values), len(row_formulas))):
            result[f"{_column(col + 1)}{row + 1}"] = {
                "value": row_values[col] if col < len(row_values) else "",
                "formula": row_formulas[col] if col < len(row_formulas) else "",
            }
    return result


def _quota_report(site: str, cells: dict) -> dict:
    """Retain retired tier data only as historical evidence and replay guards."""
    columns, end_row = (("T", "U", "V"), 7) if site == "id" else (("W", "X", "Y"), 6)
    addresses = [f"{col}{row}" for row in range(1, end_row + 1) for col in columns]
    table = [
        {"row": row, "threshold": cells[f"{columns[0]}{row}"]["value"],
         "weekly": cells[f"{columns[1]}{row}"]["value"],
         "monthly": cells[f"{columns[2]}{row}"]["value"]}
        for row in range(2, end_row + 1)
    ]
    thresholds = [_decimal(item["threshold"]) for item in table]
    valid = all(item is not None and item >= 0 for item in thresholds)
    valid = valid and all(a < b for a, b in zip(thresholds, thresholds[1:]))
    return {
        "table_range": f"{columns[0]}2:{columns[2]}{end_row}", "rows": table,
        "thresholds_complete_and_increasing": valid,
        "minimum_threshold": str(thresholds[0]) if valid else None,
        "action": "tiers_retired_no_payment_dependency",
        "expected_before": {cell: copy.deepcopy(cells[cell]) for cell in addresses},
    }


def build_patch_plan(snapshot: dict, site: str | None = None) -> dict:
    """Build reviewable per-cell patches, never extrapolating suspect amounts."""
    site = str(site or snapshot.get("site") or "").lower()
    if site not in _SITE_DIGITS:
        raise SheetRepairError("Unsupported repair site")
    cells = snapshot_cells(snapshot)
    quota = _quota_report(site, cells)
    store_counts = Counter(
        str(values[0]).strip() for values in snapshot["values"][1:]
        if len(values) >= 3 and str(values[0]).strip() and str(values[2]).upper() == site.upper()
    )
    records = []
    for row, values in enumerate(snapshot["values"], 1):
        if row == 1 or len(values) < 3 or not str(values[0]).strip() or str(values[2]).upper() != site.upper():
            continue
        name = str(values[0]).strip()
        before = {f"{col}{row}": copy.deepcopy(cells[f"{col}{row}"]) for col in "ABCDEFGHIJKLMNOPQR"}
        raw = lambda col: before[f"{col}{row}"]["value"]
        d, n = _decimal(raw("D")), _decimal(raw("N"))
        reasons = []
        if store_counts[name] != 1:
            reasons.append("duplicate_store_registration_requires_review")
        if d is None:
            reasons.append("expense_missing_or_invalid")
        if n is None:
            reasons.append("balance_missing_or_invalid")
        if not str(raw("Q") or "").strip():
            reasons.append("collection_date_missing")
        if (d is not None and d < 0) or (n is not None and n < 0):
            reasons.append("negative_amount_requires_review")
        if d is not None and 0 < d < 1000 and d != d.to_integral_value():
            reasons.append("expense_review_candidate_not_verified_by_scaling")
        if n is not None and 0 < n < 1000:
            reasons.append("balance_review_candidate_not_confirmed_error")
        patches = []
        def add(col: str, reason: str, **content: Any) -> None:
            patches.append({"cell": f"{col}{row}", "expected_before": copy.deepcopy(before[f"{col}{row}"]),
                            "reason": reason, **content})

        add("P", "hold_payment_until_fresh_collection_and_review", value=PENDING_STATUS)
        verified = _VERIFIED.get((site, name))
        accepted_verification = None
        if verified:
            if (store_counts[name] == 1 and d == Decimal(verified["old_amount"])
                    and raw("Q") == verified["updated_at"] and not before[f"D{row}"]["formula"]
                    and not before[f"R{row}"]["formula"]):
                accepted_verification = dict(verified)
                add("D", "verified_historical_expense_only", value=verified["amount"],
                    conditions={f"D{row}": copy.deepcopy(before[f"D{row}"]), f"Q{row}": copy.deepcopy(before[f"Q{row}"])})
                d = Decimal(verified["amount"])
                note = (
                    f"[2026-09-11广告审计] 按原更新时间{verified['updated_at']}对应的前七天窗口现场重查："
                    f"{verified['start_date']}至{verified['end_date']} ({verified['timezone']})，"
                    f"Expense={verified['amount']} {'IDR' if site == 'id' else 'VND'}；依据{verified['evidence']}。"
                    "历史当时实际选择窗口未记录；仅回填本次核实的历史消耗，未用当前余额替换历史余额。待新采集核验。"
                )
                add("R", "append_historical_verification_evidence", value=str(raw("R") or "") + "\n" + note)
                reasons.append("verified_historical_expense_backfill_remains_pending")
            else:
                reasons.append("verified_backfill_precondition_does_not_match_snapshot")

        desired_e = f"=D{row}/7"
        desired_f = f"=ROUND(E{row}*10,{_SITE_DIGITS[site]})"
        expected_daily = d / 7 if d is not None else Decimal(0)
        expected_budget = (expected_daily * 10).quantize(Decimal(1).scaleb(-_SITE_DIGITS[site]), rounding=ROUND_HALF_UP)
        if before[f"E{row}"]["formula"] != desired_e:
            add("E", "seven_complete_days", formula=desired_e,
                expected_after_value=str(expected_daily))
        if before[f"F{row}"]["formula"] != desired_f:
            add("F", "preserve_site_budget_rounding", formula=desired_f, expected_after_value=str(expected_budget))
        if not before[f"O{row}"]["formula"] and raw("O") not in (None, ""):
            reasons.append("manual_recharge_override_preserved")
        records.append({
            "row": row, "store": name, "reasons": reasons, "expected_before": before,
            "patches": patches, "verified_historical_expense": accepted_verification,
            "expected_derived_after": {
                f"E{row}": {"formula": desired_e, "value": str(expected_daily)},
                f"F{row}": {"formula": desired_f, "value": str(expected_budget)},
            },
            "protected_cells": [f"N{row}", f"O{row}", f"Q{row}", f"I{row}", f"J{row}", f"K{row}", f"L{row}", f"M{row}"],
            "postcondition": "payment_status_must_remain_pending",
        })
    return {
        "plan_version": PLAN_VERSION, "mode": "dry_run", "site": site,
        "doc_id": snapshot["doc_id"], "sheet_id": snapshot["meta"]["sheetid"],
        "sheet_name": snapshot["meta"]["sheet_name"], "source_retrieved_at": snapshot.get("retrieved_at"),
        "quota_review": quota, "records": records,
        "summary": {"registered_rows": len(records), "patch_cells": sum(len(r["patches"]) for r in records),
                    "reason_counts": dict(Counter(reason for r in records for reason in r["reasons"])),
                    "verified_backfills": [r["store"] for r in records if r["verified_historical_expense"]],
                    "payment_reenabled_rows": 0},
    }


def _compare(expected: dict, actual: dict, *, formula_values_may_recalculate: bool = False) -> list[str]:
    mismatches = []
    for cell, old in expected.items():
        current = actual.get(cell)
        if not isinstance(current, dict) or not {"value", "formula"} <= current.keys() or current["formula"] != old.get("formula", ""):
            mismatches.append(cell)
        elif not (formula_values_may_recalculate and old.get("formula")) and not _same_value(current.get("value", ""), old.get("value", "")):
            mismatches.append(cell)
    return mismatches


def _validate_record(record: dict, site: str) -> None:
    row = record["row"]
    patches = record["patches"]
    if not patches or patches[0].get("cell") != f"P{row}" or patches[0].get("value") != PENDING_STATUS:
        raise SheetRepairError("Repair must first hold the payment status")
    if len({p["cell"] for p in patches}) != len(patches):
        raise SheetRepairError("Duplicate repair cell")
    if not {f"{col}{row}" for col in "ABCDEFGHIJKLMNOPQR"} <= record["expected_before"].keys():
        raise SheetRepairError("Incomplete row conditions")
    for patch in patches:
        if not re.fullmatch(rf"[DEFPR]{row}", patch["cell"]):
            raise SheetRepairError("Protected cell cannot be repaired")
        if ("value" in patch) == ("formula" in patch):
            raise SheetRepairError("Repair must contain one value or formula")
        if patch["expected_before"] != record["expected_before"].get(patch["cell"]):
            raise SheetRepairError("Cell condition does not match row condition")
        if patch["cell"] == f"E{row}" and patch.get("formula") != f"=D{row}/7":
            raise SheetRepairError("Unexpected daily-budget formula")
        if patch["cell"] == f"F{row}" and patch.get("formula") != f"=ROUND(E{row}*10,{_SITE_DIGITS[site]})":
            raise SheetRepairError("Unexpected ten-day-budget formula")
        if patch["cell"] == f"D{row}":
            verified = _VERIFIED.get((site, record["store"]))
            before = record["expected_before"]
            if (not verified or patch.get("value") != verified["amount"]
                    or _decimal(before[f"D{row}"]["value"]) != Decimal(verified["old_amount"])
                    or before[f"Q{row}"]["value"] != verified["updated_at"]
                    or patch.get("conditions") != {f"D{row}": before[f"D{row}"], f"Q{row}": before[f"Q{row}"]}):
                raise SheetRepairError("Unverified historical expense cannot be written")


def apply_patch_plan(plan: dict, adapter: SheetAdapter, *, start_index: int = 0,
                     max_records: int = MAX_RECORDS_PER_APPLY, readback_attempts: int = 3) -> dict:
    """Apply at most 30 conditional rows; stop on the first conflict/failure.

    All row and quota conditions are checked before the first write. Subsequent
    checks allow formula result caches to recalculate but still protect every
    input value, identity, formula and already-written cell. Adapter errors are
    not echoed because they may contain credentials. No rollback is attempted.
    """
    if not 1 <= max_records <= MAX_RECORDS_PER_APPLY or start_index < 0 or readback_attempts < 1:
        raise SheetRepairError("Invalid repair batch bounds")
    if plan.get("plan_version") != PLAN_VERSION or plan.get("site") not in _SITE_DIGITS:
        raise SheetRepairError("Unsupported repair plan")
    selected = plan["records"][start_index:start_index + max_records]
    for record in selected:
        _validate_record(record, plan["site"])
    doc_id, sheet_id = plan["doc_id"], plan["sheet_id"]
    results = []
    for record in selected:
        expected = copy.deepcopy(plan["quota_review"]["expected_before"])
        expected.update(copy.deepcopy(record["expected_before"]))
        written = []
        attempted = []
        payment_block_confirmed = False
        outcome = {"row": record["row"], "store": record["store"], "written_cells": written, "attempted_cells": attempted}
        try:
            current = adapter.read_cells(doc_id, sheet_id, list(expected))
            mismatches = _compare(expected, current)
            if mismatches:
                pcell = f"P{record['row']}"
                payment_block_confirmed = not _compare({pcell: {"value": PENDING_STATUS, "formula": ""}}, current)
                outcome.update(status="conflict", conflicting_cells=mismatches, payment_block_confirmed=payment_block_confirmed)
                results.append(outcome)
                break
            for patch in record["patches"]:
                cell = patch["cell"]
                if written:
                    mismatches = _compare(expected, adapter.read_cells(doc_id, sheet_id, list(expected)), formula_values_may_recalculate=True)
                    if mismatches:
                        raise SheetRepairError("Concurrent worksheet change after payment hold")
                content = {"formula": patch["formula"]} if "formula" in patch else {"value": patch["value"]}
                attempted.append(cell)
                adapter.write_cell(doc_id, sheet_id, cell, **content)
                written.append(cell)
                expected[cell] = {"formula": patch.get("formula", ""), "value": patch.get("value", "")}
                verified = False
                for _ in range(readback_attempts):
                    actual = adapter.read_cells(doc_id, sheet_id, [cell]).get(cell, {})
                    if actual.get("formula", "") != patch.get("formula", ""):
                        continue
                    if "value" in patch:
                        verified = _same_value(actual.get("value", ""), patch["value"])
                    else:
                        number, target = _decimal(actual.get("value")), _decimal(patch.get("expected_after_value"))
                        verified = number is not None and target is not None and abs(number - target) <= Decimal("0.000001")
                    if verified:
                        expected[cell] = dict(actual)
                        break
                if not verified:
                    raise SheetRepairError("Worksheet write could not be verified")
                if cell == f"P{record['row']}":
                    payment_block_confirmed = True
            final = adapter.read_cells(doc_id, sheet_id, list(expected))
            mismatches = _compare(expected, final, formula_values_may_recalculate=True)
            if mismatches:
                raise SheetRepairError("Worksheet changed before repair completed")
            derived_ok = False
            for attempt in range(readback_attempts):
                if attempt:
                    final = adapter.read_cells(doc_id, sheet_id, list(expected))
                if _compare(expected, final, formula_values_may_recalculate=True):
                    raise SheetRepairError("Worksheet changed during derived-value verification")
                derived_ok = True
                for cell, target in record["expected_derived_after"].items():
                    actual = final.get(cell, {})
                    number = _decimal(actual.get("value"))
                    if actual.get("formula", "") != target["formula"] or number is None or abs(number - Decimal(target["value"])) > Decimal("0.000001"):
                        derived_ok = False
                        break
                if derived_ok:
                    break
            if not derived_ok:
                raise SheetRepairError("Worksheet derived values have not recalculated")
            outcome.update(status="applied_pending", payment_block_confirmed=True)
        except Exception:
            # No compensation writes: that could overwrite another operator's
            # changes. Report whether the hold is still present, then stop.
            try:
                pcell = f"P{record['row']}"
                actual = adapter.read_cells(doc_id, sheet_id, [pcell])
                payment_block_confirmed = not _compare({pcell: {"value": PENDING_STATUS, "formula": ""}}, actual)
            except Exception:
                payment_block_confirmed = False
            outcome.update(status="failed_pending" if payment_block_confirmed else "failed_hold_unconfirmed",
                           payment_block_confirmed=payment_block_confirmed, error="Conditional repair stopped; inspect current cells before retry")
        results.append(outcome)
        if outcome["status"] != "applied_pending":
            break
    applied = sum(result["status"] == "applied_pending" for result in results)
    return {"results": results, "requested_records": len(selected), "applied_records": applied,
            "next_index": start_index + applied, "stopped": applied != len(selected),
            "payment_reenabled_rows": 0, "rollback_attempted": False}


def apply_patch_batch(plan: dict, adapter: SheetAdapter, *, start_index: int = 0,
                      max_records: int = MAX_RECORDS_PER_APPLY, readback_attempts: int = 3) -> dict:
    """Conditionally repair at most 30 rows with three reads and two write sets.

    Successful batches use one full preflight read, one full read after the
    payment hold, and one full final read. Only delayed formula recalculation
    adds final reads. On failure no compensation writes or payment releases are
    attempted. A write-set exception may mean only some specified cells were
    written; callers must stop and inspect the reported batch before retrying.
    """
    if (not isinstance(max_records, int) or isinstance(max_records, bool)
            or not 1 <= max_records <= MAX_RECORDS_PER_APPLY
            or not isinstance(start_index, int) or isinstance(start_index, bool) or start_index < 0
            or not isinstance(readback_attempts, int) or isinstance(readback_attempts, bool) or readback_attempts < 1):
        raise SheetRepairError("Invalid repair batch bounds")
    if plan.get("plan_version") != PLAN_VERSION or plan.get("site") not in _SITE_DIGITS:
        raise SheetRepairError("Unsupported repair plan")
    selected = plan["records"][start_index:start_index + max_records]
    if len({record["row"] for record in selected}) != len(selected):
        raise SheetRepairError("Duplicate repair row")
    for record in selected:
        _validate_record(record, plan["site"])
        if set(record.get("expected_derived_after", {})) != {f"E{record['row']}", f"F{record['row']}"}:
            raise SheetRepairError("Incomplete derived-value conditions")

    result = {
        "results": [], "requested_records": len(selected), "applied_records": 0,
        "next_index": start_index, "stopped": False, "payment_reenabled_rows": 0,
        "rollback_attempted": False, "attempted_cells": [], "read_calls": 0,
        "write_set_calls": 0, "may_be_partially_applied": False,
    }
    if not selected:
        return result
    doc_id, sheet_id = plan["doc_id"], plan["sheet_id"]
    expected = copy.deepcopy(plan["quota_review"]["expected_before"])
    for record in selected:
        expected.update(copy.deepcopy(record["expected_before"]))
    addresses = list(expected)
    holds = {f"P{record['row']}": {"value": PENDING_STATUS} for record in selected}
    other_patches = {
        patch["cell"]: ({"formula": patch["formula"]} if "formula" in patch else {"value": patch["value"]})
        for record in selected for patch in record["patches"][1:]
    }
    current: dict = {}
    phase = "preflight"

    def read(cells: list[str]) -> dict:
        result["read_calls"] += 1
        return adapter.read_cells(doc_id, sheet_id, cells)

    def write(cells: dict[str, dict]) -> None:
        result["attempted_cells"].extend(cells)
        result["write_set_calls"] += 1
        adapter.write_cells(doc_id, sheet_id, cells)

    def is_held(record: dict, snapshot: dict) -> bool:
        cell = f"P{record['row']}"
        return not _compare({cell: {"value": PENDING_STATUS, "formula": ""}}, snapshot)

    try:
        current = read(addresses)
        mismatches = _compare(expected, current)
        if mismatches:
            result.update(stopped=True, status="conflict", conflicting_cells=mismatches)
            result["results"] = [
                {"row": record["row"], "store": record["store"], "status": "batch_conflict",
                 "payment_block_confirmed": is_held(record, current), "written_cells": [], "attempted_cells": []}
                for record in selected
            ]
            return result

        phase = "payment_hold"
        write(holds)
        for cell in holds:
            expected[cell] = {"value": PENDING_STATUS, "formula": ""}
        # This read both verifies every hold and checks that the original D/Q,
        # identity, balance, manual overrides and quota table remain unchanged.
        current = read(addresses)
        mismatches = _compare(expected, current, formula_values_may_recalculate=True)
        if mismatches:
            result["conflicting_cells"] = mismatches
            raise SheetRepairError("Batch changed after payment hold")

        phase = "repair_write"
        if other_patches:
            write(other_patches)
            for cell, content in other_patches.items():
                expected[cell] = {"value": content.get("value", ""), "formula": content.get("formula", "")}

        phase = "final_readback"
        derived_ok = False
        for _ in range(readback_attempts):
            current = read(addresses)
            mismatches = _compare(expected, current, formula_values_may_recalculate=True)
            if mismatches:
                result["conflicting_cells"] = mismatches
                raise SheetRepairError("Batch protected cells or written cells changed")
            derived_ok = True
            for record in selected:
                for cell, target in record["expected_derived_after"].items():
                    actual = current.get(cell, {})
                    number = _decimal(actual.get("value"))
                    if (actual.get("formula", "") != target["formula"] or number is None
                            or abs(number - Decimal(target["value"])) > Decimal("0.000001")):
                        derived_ok = False
                        break
                if not derived_ok:
                    break
            if derived_ok:
                break
        if not derived_ok:
            raise SheetRepairError("Batch derived values have not recalculated")

        result.update(status="applied_pending", applied_records=len(selected), next_index=start_index + len(selected))
        result["results"] = [
            {"row": record["row"], "store": record["store"], "status": "applied_pending",
             "payment_block_confirmed": True,
             "written_cells": [patch["cell"] for patch in record["patches"]],
             "attempted_cells": [patch["cell"] for patch in record["patches"]]}
            for record in selected
        ]
        return result
    except Exception:
        # The adapter may have committed only a prefix before raising. Read the
        # holds, report uncertainty, and leave every current cell untouched.
        if result["write_set_calls"]:
            try:
                current = read(list(holds))
            except Exception:
                current = {}
        all_held = all(is_held(record, current) for record in selected)
        result.update(
            status="failed_pending" if all_held else "failed_hold_unconfirmed", stopped=True,
            phase=phase, may_be_partially_applied=bool(result["write_set_calls"]),
            error="Conditional batch repair stopped; inspect current cells before retry",
        )
        attempted = set(result["attempted_cells"])
        result["results"] = [
            {"row": record["row"], "store": record["store"],
             "status": "failed_pending" if is_held(record, current) else "failed_hold_unconfirmed",
             "payment_block_confirmed": is_held(record, current), "written_cells": [],
             "attempted_cells": [patch["cell"] for patch in record["patches"] if patch["cell"] in attempted]}
            for record in selected
        ]
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    plans = [build_patch_plan(json.loads(path.read_text(encoding="utf-8-sig"))) for path in args.snapshot]
    report = {"mode": "dry_run", "plan_version": PLAN_VERSION, "plans": plans,
              "warning": "Unverified candidates are not scaled. No historical balances or manual recharge amounts are changed. No payments are enabled."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"mode": "dry_run", "output": str(args.output), "sites": [plan["site"] for plan in plans]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
