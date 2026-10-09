"""Apply a saved, scoped VN/ID tier-removal plan; default only plans offline."""
import argparse
import json
from pathlib import Path
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from scripts.remove_vn_id_ads_tiers import build_removal_plan, apply_removal_batch, apply_source_table_removal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", choices=("vn", "id"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("plan", "rows", "source"), default="plan")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--max-records", type=int, default=1)
    parser.add_argument("--all-batches", action="store_true")
    args = parser.parse_args()
    plan_path = args.output_dir / f"{args.site}_removal_plan.json"
    try:
        if args.phase == "plan":
            if plan_path.exists():
                raise ValueError("saved_plan_already_exists")
            snapshot = json.loads((args.output_dir / f"{args.site}_before.json").read_text(encoding="utf-8"))
            if snapshot["site"] != args.site:
                raise ValueError("site_mismatch")
            plan = build_removal_plan(snapshot)
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"site": args.site, "mode": "dry_run", **plan["summary"]}, ensure_ascii=False))
            return 0
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if plan["site"] != args.site:
            raise ValueError("site_mismatch")
        from scripts.apply_vn_id_ads_repair import ApplicationSheetAdapter
        adapter = ApplicationSheetAdapter(args.site)
        index = args.start_index
        while True:
            if args.phase == "rows":
                result = apply_removal_batch(plan, adapter, start_index=index, max_records=args.max_records)
            else:
                result = apply_source_table_removal(plan, adapter)
            result.update(site=args.site, api_calls=adapter.api_calls)
            path = args.output_dir / f"{args.site}_{args.phase}_{index}_{time.time_ns()}.json"
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: v for k, v in result.items() if k not in ("attempted_cells", "rows")}, ensure_ascii=False), flush=True)
            if result["stopped"]:
                return 2
            index = result.get("next_index", len(plan["records"]))
            if args.phase != "rows" or not args.all_batches or index >= len(plan["records"]):
                return 0
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "action": "stopped_without_payment"}))
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
