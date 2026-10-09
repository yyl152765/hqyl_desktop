"""Authenticated maintenance adapter for the reviewed VN/ID repair plan.

Default mode only snapshots the configured application worksheets. --apply
requires an existing reviewed plan and never enables or executes payments.
Credentials remain in memory and SDK diagnostics are suppressed.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import re
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
SUPERBROWSER = PROJECT.parent / "superbrowser_process"
sys.path.insert(0, str(PROJECT))
from backend.config_store import ConfigStore
from scripts.repair_vn_id_ads_sheet import apply_patch_batch, build_patch_plan


def quiet_call(function, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return function(*args, **kwargs)


class ApplicationSheetAdapter:
    def __init__(self, site):
        settings = ConfigStore().load().dingtalk
        sys.path.insert(0, str(SUPERBROWSER))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            import yaml
            from util import dingtalk_doc_api
        self.api = dingtalk_doc_api
        title = {"vn": "越南", "id": "印尼"}[site]
        config = yaml.safe_load((SUPERBROWSER / "main" / "shopee" / "config" / f"Shopee{title}广告充值.yaml").read_text(encoding="utf-8"))["dingtalk"]["doc_config"]
        self.site, self.title = site, title
        self.doc_id = config["config_doc_id"]
        self.token = quiet_call(self.api.get_access_token, settings.app_key, settings.app_secret)
        self.union_id = quiet_call(self.api.get_union_id, self.token, config["app_userid"])
        self.meta = quiet_call(self.api.get_sheet_content, self.token, self.union_id, self.doc_id, title)
        self.sheet_id = self.meta["sheetid"]
        self.api_calls = 0

    def _read(self, cell_range, select):
        self.api_calls += 1
        return quiet_call(self.api.read_range, self.doc_id, self.sheet_id, self.union_id, self.token, cell_range, select)

    def snapshot(self):
        count = max(int(self.meta.get("last_non_empty_row", 0)), int(self.meta.get("row_count", 0)))
        return {"site": self.site, "title": self.title, "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "doc_id": self.doc_id, "meta": self.meta,
                "values": self._read(f"A1:Y{count}", "values"),
                "formulas": self._read(f"A1:Y{count}", "formulas")}

    def read_cells(self, doc_id, sheet_id, cells):
        if doc_id != self.doc_id or sheet_id != self.sheet_id:
            raise ValueError("repair_document_identity_mismatch")
        by_row = {}
        for cell in cells:
            match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", cell)
            if not match:
                raise ValueError("invalid_cell")
            col = 0
            for char in match[1]: col = col * 26 + ord(char) - 64
            by_row.setdefault(int(match[2]), []).append((col, cell))
        # Merge adjacent rows that request the same contiguous column bands.
        bands = {}
        for row, items in by_row.items():
            columns = sorted(set(col for col, _ in items))
            runs = []
            for col in columns:
                if runs and runs[-1][1] + 1 == col: runs[-1][1] = col
                else: runs.append([col, col])
            for first, last in runs: bands.setdefault((first, last), []).append(row)
        result = {}
        for (first, last), rows in bands.items():
            ranges = []
            for row in sorted(rows):
                if ranges and ranges[-1][1] + 1 == row: ranges[-1][1] = row
                else: ranges.append([row, row])
            for start, end in ranges:
                cell_range = f"{column(first)}{start}:{column(last)}{end}"
                values = self._read(cell_range, "values")
                formulas = self._read(cell_range, "formulas")
                for row in range(start, end + 1):
                    for col in range(first, last + 1):
                        key = f"{column(col)}{row}"
                        result[key] = {"value": value_at(values, row-start, col-first),
                                       "formula": value_at(formulas, row-start, col-first)}
        return {cell: result[cell] for cell in cells}

    def write_cell(self, doc_id, sheet_id, cell, **content):
        if doc_id != self.doc_id or sheet_id != self.sheet_id:
            raise ValueError("repair_document_identity_mismatch")
        self.api_calls += 1
        value = content.get("formula", content.get("value"))
        quiet_call(self.api.update_range, self.doc_id, self.sheet_id, self.union_id, self.token,
                   f"{cell}:{cell}", [[str(value)]], number_format="General")

    def write_cells(self, doc_id, sheet_id, patches):
        if doc_id != self.doc_id or sheet_id != self.sheet_id:
            raise ValueError("repair_document_identity_mismatch")
        by_column = {}
        for cell, content in patches.items():
            match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", cell)
            if not match or set(content) not in ({"value"}, {"formula"}):
                raise ValueError("invalid_cell_patch")
            by_column.setdefault(match[1], []).append((int(match[2]), next(iter(content.values()))))
        for col, items in sorted(by_column.items()):
            groups = []
            for row, value in sorted(items):
                if groups and groups[-1][-1][0] + 1 == row:
                    groups[-1].append((row, value))
                else:
                    groups.append([(row, value)])
            for group in groups:
                self.api_calls += 1
                quiet_call(self.api.update_range, self.doc_id, self.sheet_id, self.union_id, self.token,
                           f"{col}{group[0][0]}:{col}{group[-1][0]}", [[str(value)] for _, value in group],
                           number_format="General")


def value_at(rows, row, col):
    return rows[row][col] if row < len(rows) and col < len(rows[row]) else ""


def column(index):
    result = ""
    while index:
        index, rem = divmod(index-1, 26)
        result = chr(65+rem) + result
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", choices=("vn", "id"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--max-records", type=int, default=1)
    parser.add_argument("--all-batches", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        adapter = ApplicationSheetAdapter(args.site)
        plan_path = args.output_dir / f"{args.site}_live_patch_plan.json"
        if not args.apply:
            snapshot = adapter.snapshot()
            path = args.output_dir / f"{args.site}_before.json"
            if path.exists() or plan_path.exists():
                raise ValueError("refuse_to_overwrite_existing_backup")
            path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            plan = build_patch_plan(snapshot)
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"mode": "snapshot", "site": args.site, "records": len(plan["records"]), "api_calls": adapter.api_calls}))
            return 0
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        index = args.start_index
        while index < len(plan["records"]):
            outcome = apply_patch_batch(plan, adapter, start_index=index, max_records=args.max_records)
            outcome.update(site=args.site, api_calls=adapter.api_calls, elapsed_seconds=round(time.monotonic()-started, 2))
            path = args.output_dir / f"{args.site}_apply_{index}_{time.time_ns()}.json"
            path.write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: v for k, v in outcome.items() if k not in ("results", "attempted_cells")}, ensure_ascii=False), flush=True)
            if outcome["stopped"]:
                print(json.dumps(outcome, ensure_ascii=False), flush=True)
                return 2
            index = outcome["next_index"]
            if not args.all_batches:
                break
        return 0
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "action": "stopped_without_payment"}))
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
