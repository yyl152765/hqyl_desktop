"""Build offline, narrowly scoped plans to remove VN/ID advertising tiers.

The CLI is dry-run only. Applying a reviewed plan requires an explicitly
injected read_cells/write_cells adapter; this module does not authenticate,
import an application client, touch a browser, or change payment status.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol


PLAN_VERSION = "vn-id-tier-removal-v1"
MAX_RECORDS_PER_APPLY = 30
_ROW_COLUMNS = "ABCDEFGHIJKLMNOPQR"
_TIER_COLUMNS = "IJM"
_SOURCE_COLUMNS = {"id": "TUV", "vn": "WXY"}
_HEADERS = {
    "C": {"站点"},
    "D": {"过去7天消耗金额", "过去7天消耗金额（运营填写）"},
    "F": {"预估10天广告费"},
    "I": {"实际7天限额"}, "J": {"实际月限额"},
    "K": {"当前7天限额"}, "L": {"当前月限额"},
    "M": {"判断是否提档"}, "N": {"广告余额（运营填写）"},
    "O": {"实际充值"}, "P": {"判断是否充值"}, "Q": {"更新时间(RPA)"},
}


class TierRemovalError(ValueError):
    """The snapshot, plan, or worksheet is not safe for this scoped removal."""


class SheetAdapter(Protocol):
    def read_cells(self, doc_id: str, sheet_id: str, cells: list[str]) -> dict:
        """Return {address: {'value': value, 'formula': formula_or_empty}}."""

    def write_cells(self, doc_id: str, sheet_id: str, patches: dict) -> None:
        """Write only the specified cells; {value: ''} clears their formula."""


def _number(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _same_value(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float, Decimal)) and isinstance(right, (int, float, Decimal)):
        return _number(left) is not None and _number(left) == _number(right)
    return left == right


def _cell(snapshot, col: str, row: int) -> dict:
    column = ord(col) - ord("A")
    result = {}
    for field, matrix_name in (("value", "values"), ("formula", "formulas")):
        matrix = snapshot[matrix_name]
        values = matrix[row - 1] if row <= len(matrix) else []
        result[field] = copy.deepcopy(values[column] if column < len(values) else "")
    if not isinstance(result["formula"], str):
        raise TierRemovalError("Snapshot contains an unknown formula representation")
    return result


def _cells(snapshot, columns, rows):
    return {f"{col}{row}": _cell(snapshot, col, row) for row in rows for col in columns}


def _snapshot_hash(snapshot):
    try:
        serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        raise TierRemovalError("Snapshot must contain finite JSON data") from None
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_removal_plan(snapshot: dict) -> dict:
    """Preserve the original snapshot and propose only recognized tier cells."""
    if not isinstance(snapshot, dict):
        raise TierRemovalError("Snapshot must be an object")
    site = str(snapshot.get("site", "")).lower()
    if site not in _SOURCE_COLUMNS:
        raise TierRemovalError("Only VN/ID tier removal is supported")
    for field in ("values", "formulas"):
        matrix = snapshot.get(field)
        if not isinstance(matrix, list) or not matrix or any(not isinstance(row, list) for row in matrix):
            raise TierRemovalError("Snapshot must contain values and formulas matrices")
    if len(snapshot["formulas"]) < len(snapshot["values"]):
        raise TierRemovalError("Formula snapshot does not cover all value rows")
    meta = snapshot.get("meta", {})
    doc_id, sheet_id = snapshot.get("doc_id"), meta.get("sheetid")
    if not isinstance(doc_id, str) or not doc_id or not isinstance(sheet_id, str) or not sheet_id:
        raise TierRemovalError("Snapshot document identity is incomplete")
    for col, accepted in _HEADERS.items():
        header = _cell(snapshot, col, 1)
        if header["formula"] or header["value"] not in accepted:
            raise TierRemovalError(f"Unrecognized advertising worksheet header {col}1")
    source_columns = _SOURCE_COLUMNS[site]
    for col, label in zip(source_columns, ("日消耗区间", "7天", "每月")):
        if _cell(snapshot, col, 1) != {"value": label, "formula": ""}:
            raise TierRemovalError("Source tier table headers do not match the known site layout")
    source_before = _cells(snapshot, source_columns, range(1, 8))
    for row in range(2, 8):
        for col in source_columns:
            content = source_before[f"{col}{row}"]
            number = _number(content["value"])
            if content["formula"] or (content["value"] not in ("", None) and (number is None or number < 0)):
                raise TierRemovalError("Source tier table contains unrelated text or formulas")
    records = []
    for row_number, values in enumerate(snapshot["values"], 1):
        if row_number == 1 or len(values) < 3:
            continue
        if not isinstance(values[0], str) or not values[0].strip() or str(values[2]).strip().lower() != site:
            continue
        records.append({
            "row": row_number, "store": values[0].strip(),
            "expected_before": _cells(snapshot, _ROW_COLUMNS, [row_number]),
            "clear_cells": [f"{col}{row_number}" for col in _TIER_COLUMNS],
        })
    if not records:
        raise TierRemovalError("No registered same-site shop rows were found")
    # A fresh apply must detect new registrations in the known physical sheet
    # before removing the shared source table. No cells in this scope are
    # written unless they are already a registered row's I/J/M cell.
    scope_end = max(len(snapshot["values"]), len(snapshot["formulas"]), int(meta.get("row_count", 0)), int(meta.get("last_non_empty_row", 0)))
    if not 7 <= scope_end <= 20000:
        raise TierRemovalError("Snapshot row coverage is invalid")
    return {
        "mode": "dry_run", "plan_version": PLAN_VERSION, "site": site,
        "doc_id": doc_id, "sheet_id": sheet_id,
        "source_snapshot_sha256": _snapshot_hash(snapshot),
        "source_snapshot": copy.deepcopy(snapshot),
        "records": records,
        "header_expected_before": _cells(snapshot, _ROW_COLUMNS, [1]),
        "header_clear_cells": [f"{col}1" for col in _TIER_COLUMNS],
        "registration_expected_before": _cells(snapshot, "ABC", range(2, scope_end + 1)),
        "source_table": {
            "range": f"{source_columns[0]}1:{source_columns[-1]}7",
            "expected_before": source_before,
            "clear_cells": list(source_before),
        },
        "summary": {
            "registered_rows": len(records), "row_tier_cells": len(records) * 3,
            "header_cells": 3, "source_table_cells": 21,
            "maximum_rows_per_batch": MAX_RECORDS_PER_APPLY,
            "preserved_columns": "A:H,K:L,N:R and every unlisted cell",
            "payment_status_writes": 0, "source_table_must_be_last": True,
        },
    }


def _validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("plan_version") != PLAN_VERSION:
        raise TierRemovalError("Unsupported tier-removal plan")
    # Rebuild from the retained source evidence so even a hand-edited plan
    # cannot introduce an arbitrary address or a weakened compare condition.
    if build_removal_plan(plan.get("source_snapshot")) != plan:
        raise TierRemovalError("Plan does not exactly match its retained snapshot and allowed scope")


def _compare(expected, actual):
    if not isinstance(actual, dict):
        return list(expected)
    return [cell for cell, before in expected.items()
            if not isinstance(actual.get(cell), dict)
            or set(actual[cell]) != {"value", "formula"}
            or actual[cell]["formula"] != before["formula"]
            or not _same_value(actual[cell]["value"], before["value"])]


def _blank(expected, addresses):
    for cell in addresses:
        expected[cell] = {"value": "", "formula": ""}


def _apply_clear_sets(plan, adapter, expected, write_sets, *, phase):
    result = {
        "status": "not_started", "stopped": False, "phase": phase,
        "read_calls": 0, "write_set_calls": 0, "attempted_cells": [],
        "may_be_partially_applied": False, "rollback_attempted": False,
        "payment_reenabled_rows": 0, "payment_status_writes": 0,
    }
    expected = copy.deepcopy(expected)
    addresses = list(expected)
    try:
        for index in range(len(write_sets) + 1):
            result["read_calls"] += 1
            actual = adapter.read_cells(plan["doc_id"], plan["sheet_id"], addresses)
            mismatch = _compare(expected, actual)
            if mismatch:
                result.update(status="conflict" if index == 0 else "partial_conflict",
                              stopped=True, conflicting_cells=mismatch,
                              may_be_partially_applied=bool(result["write_set_calls"]))
                return result
            if index == len(write_sets):
                result["status"] = "applied"
                return result
            patches = {cell: {"value": ""} for cell in write_sets[index]}
            # Every patch has already been regenerated and allowlisted by
            # _validate_plan. Never fill a rectangular gap between columns.
            result["attempted_cells"].extend(patches)
            result["write_set_calls"] += 1
            adapter.write_cells(plan["doc_id"], plan["sheet_id"], patches)
            _blank(expected, patches)
    except Exception as exc:
        result.update(status="failed", stopped=True,
                      error_type=type(exc).__name__,
                      error="Conditional removal stopped; inspect the current worksheet before retrying",
                      may_be_partially_applied=bool(result["write_set_calls"]))
        return result


def apply_removal_batch(plan: dict, adapter: SheetAdapter, *, start_index=0, max_records=30) -> dict:
    """Clear at most 30 registered rows with three reads and two write sets.

    Clear M before I/J so the removed decision formulas cannot recalculate
    during the intermediate comparison. No payment status is ever written.
    """
    _validate_plan(plan)
    if (not isinstance(start_index, int) or isinstance(start_index, bool) or start_index < 0
            or not isinstance(max_records, int) or isinstance(max_records, bool)
            or not 1 <= max_records <= MAX_RECORDS_PER_APPLY):
        raise TierRemovalError("Invalid removal batch bounds")
    selected = plan["records"][start_index:start_index + max_records]
    if not selected:
        return {"status": "no_rows", "stopped": False, "next_index": start_index,
                "requested_records": 0, "applied_records": 0, "payment_status_writes": 0,
                "read_calls": 0, "write_set_calls": 0, "rollback_attempted": False}
    expected = copy.deepcopy(plan["header_expected_before"])
    expected.update(copy.deepcopy(plan["source_table"]["expected_before"]))
    for record in selected:
        expected.update(copy.deepcopy(record["expected_before"]))
    decision_cells = [f"M{record['row']}" for record in selected]
    amount_cells = [f"{col}{record['row']}" for record in selected for col in "IJ"]
    result = _apply_clear_sets(plan, adapter, expected, [decision_cells, amount_cells], phase="registered_rows")
    count = len(selected) if result["status"] == "applied" else 0
    result.update(requested_records=len(selected), applied_records=count,
                  next_index=start_index + count,
                  rows=[record["row"] for record in selected])
    return result


def apply_source_table_removal(plan: dict, adapter: SheetAdapter) -> dict:
    """Clear tier headers and source table only after all shop tiers are gone.

    All protected shop fields, known registrations, original headers and source
    cells are compared first. Source values/formulas must exactly match the
    backup; the final read verifies them blank and all other protected fields
    unchanged. Conflict or partial failure stops without compensation writes.
    """
    _validate_plan(plan)
    expected = copy.deepcopy(plan["registration_expected_before"])
    expected.update(copy.deepcopy(plan["header_expected_before"]))
    expected.update(copy.deepcopy(plan["source_table"]["expected_before"]))
    for record in plan["records"]:
        expected.update(copy.deepcopy(record["expected_before"]))
        _blank(expected, record["clear_cells"])
    result = _apply_clear_sets(
        plan, adapter, expected,
        [plan["header_clear_cells"], plan["source_table"]["clear_cells"]],
        phase="source_table_last",
    )
    result["registered_rows_verified"] = len(plan["records"]) if result["status"] == "applied" else 0
    result["source_range"] = plan["source_table"]["range"]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists() or any(args.output.resolve() == path.resolve() for path in args.snapshot):
        raise TierRemovalError("Refuse to overwrite an existing plan or source snapshot")
    plans = [build_removal_plan(json.loads(path.read_text(encoding="utf-8-sig"))) for path in args.snapshot]
    report = {"mode": "dry_run", "plan_version": PLAN_VERSION, "plans": plans}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"mode": "dry_run", "sites": [plan["site"] for plan in plans], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
