"""Run the offline VN ads recovery reproductions and save a JSON evidence report.

The assertions characterize current defects, not desired fixed behavior.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import threading
import traceback

import repro_vn_ads_balance as balance
import repro_vn_ads_batch as batch
import repro_vn_ads_payment_wait as payment


def run():
    blocked_io = []

    def audit(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system", "os.posix_spawn"} or event.startswith("os.exec"):
            blocked_io.append(event)
            raise RuntimeError(f"Offline reproduction blocked external I/O: {event}")

    sys.addaudithook(audit)
    sources = {balance.GUARD, balance.SERVICE, balance.LOADER, balance.LOCK, balance.RULES, payment.SOURCE}
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    initial_threads = set(threading.enumerate())
    report = {
        "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "scope": "offline execution of source-extracted production recovery functions; fake browser/payment/transport/database",
        "limitations": [
            "No real Electron process or modal dialog was reproduced.",
            "Injected WebDriver errors are not proof that the photographed Electron error caused session loss.",
            "Source code is the local working copy, not a verified copy of the affected machine's installed build.",
            "Synthetic payment counters are not real transactions or proof of duplicate charges.",
            "Timing is accelerated and blocking is injected; this establishes scheduling behavior, not its real-world frequency.",
            "The injected balance-read gate intentionally survives stopBrowser; a real browser close may abort an old remote call sooner. Overlapping checks do not imply concurrent startBrowser requests.",
        ],
        "source_sha256_before": before,
    }
    try:
        report["balance"] = balance.run()
        report["batch"] = batch.run()
        report["payment_wait"] = payment.run()

        # Connect two real source layers: observe success, lose the session
        # during confirmation, then execute the real balance recovery logic.
        namespace, _, _ = payment.load_real_functions()
        namespace["time"] = payment.Clock()
        driver = payment.Driver(namespace, error=payment.InvalidSessionIdException("invalid session id after Payment Successful"), error_after_success=True)
        status, message = namespace["_wait_for_payment_result"](driver, timeout=6)
        assert status == "processing" and driver.success_observed
        recovered = balance.case(fail_close=True, confirmed=False, initial_status=status, initial_message=message)
        assert recovered["open_calls"] == 2 and recovered["result_status"] == "processing"
        report["cross_layer_case"] = {
            "name": "success_observed_then_session_loss_then_close_timeouts",
            "passed": True, "success_observed": driver.success_observed,
            "payment_wait_status": status, "recovery": recovered,
        }
        report["test_count"] = len(report["balance"]["cases"]) + len(report["batch"]["cases"]) + len(report["payment_wait"]["cases"]) + 1
        assert not blocked_io, blocked_io
        report["passed"] = True
    except BaseException:
        report["passed"] = False
        report["error"] = traceback.format_exc()
    finally:
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
        report["production_sources_unchanged"] = before == after
        report["blocked_external_io_attempts"] = blocked_io
        for worker in set(threading.enumerate()) - initial_threads:
            worker.join(3)
        report["leftover_threads"] = [worker.name for worker in set(threading.enumerate()) - initial_threads if worker.is_alive()]
        if report["leftover_threads"] or before != after or blocked_io:
            report["passed"] = False
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "test_count": result.get("test_count"),
                      "production_sources_unchanged": result["production_sources_unchanged"],
                      "blocked_external_io_attempts": result["blocked_external_io_attempts"],
                      "leftover_threads": result["leftover_threads"], "report": str(args.output.resolve()),
                      "error": result.get("error")}, ensure_ascii=False))
    sys.exit(0 if result["passed"] else 1)
