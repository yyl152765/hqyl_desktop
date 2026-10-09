from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import call, patch

from backend.services.bigseller_benchmark_checkpoint import (
    CheckpointError,
    create_checkpoint,
    read_checkpoint,
)


class _CountingConnection:
    """Keep real SQLite transactions while observing attempted batch writes."""

    def __init__(self, connection, error=None):
        self.connection = connection
        self.error = error
        self.write_count = 0

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *exc):
        return self.connection.__exit__(*exc)

    def executemany(self, *args, **kwargs):
        self.write_count += 1
        if self.error is not None:
            raise self.error
        return self.connection.executemany(*args, **kwargs)


class BenchmarkCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.identity = {
            "source_file": str(self.root / "源表.xlsx"),
            "source_sha256": hashlib.sha256(b"source").hexdigest(),
            "sheet_name": "滞销SKU", "metric": "views",
            "account_key": hashlib.sha256(b"account").hexdigest(),
            "skus": ["sku-a", "sku-b", "sku-c"],
        }

    def matched(self, sku="sku-a", value=42):
        return {"sku": sku, "shop_name": "店铺甲", "shop_id": "shop-1", "item_id": "item-1",
                "metric_value": value, "shop_count": 3, "status": "matched", "message": "",
                "attempts": 2, "queried_at": "2026-09-04T12:00:00+08:00"}

    def failed(self, sku="sku-b"):
        return {"sku": sku, "status": "failed", "message": "等待网络恢复", "attempts": 4,
                "retry_not_before": 2000000000.25}

    def write(self, rows=None):
        with create_checkpoint(self.root, self.identity) as checkpoint:
            checkpoint.save_results(rows if rows is not None else [self.matched()])
            return checkpoint.path

    def tamper_row(self, path, row):
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("UPDATE results SET row_json=?", (json.dumps(row),))

    def test_committed_results_survive_connection_close_without_final_export(self):
        checkpoint = create_checkpoint(self.root, self.identity)
        checkpoint.save_result(self.matched())
        checkpoint.save_result(self.failed())
        path = checkpoint.path
        # An abrupt process exit closes its connection without any final export
        # or call to the Checkpoint finalization API.
        checkpoint._connection.close()
        rows = read_checkpoint(path, self.identity)
        self.assertEqual(set(rows), {"sku-a", "sku-b"})
        self.assertEqual(rows["sku-a"]["metric_value"], 42)
        self.assertEqual(rows["sku-b"]["retry_not_before"], 2000000000.25)

    def test_every_save_is_visible_to_a_separate_reader(self):
        with create_checkpoint(self.root, self.identity) as checkpoint:
            checkpoint.save_result(self.matched())
            self.assertEqual(read_checkpoint(checkpoint.path, self.identity)["sku-a"]["metric_value"], 42)
            checkpoint.save_result(self.matched(value=99))
            rows = read_checkpoint(checkpoint.path, self.identity)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows["sku-a"]["metric_value"], 99)

    def test_process_crash_during_next_transaction_preserves_prior_commits(self):
        path = self.write()
        # Force dirty pages into the database before abruptly terminating; this
        # leaves a real hot journal instead of an orderly connection rollback.
        code = "\n".join([
            "import os, sqlite3, sys",
            "connection = sqlite3.connect(sys.argv[1])",
            "connection.execute('PRAGMA cache_size=1')",
            "connection.execute('BEGIN IMMEDIATE')",
            "connection.execute('UPDATE results SET row_json=?', ('x' * 100000,))",
            "os._exit(0)",
        ])
        subprocess.run([sys.executable, "-c", code, str(path)], check=True, capture_output=True)
        journal = Path(str(path) + "-journal")
        self.assertTrue(journal.exists())
        before, journal_before = path.read_bytes(), journal.read_bytes()
        restored = read_checkpoint(path, self.identity)
        self.assertEqual(restored["sku-a"]["metric_value"], 42)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(journal.read_bytes(), journal_before)

    def test_all_identity_dimensions_must_match(self):
        path = self.write()
        changed = {
            "source_file": str(self.root / "另一份源表.xlsx"),
            "source_sha256": "0" * 64, "sheet_name": "另一张表", "metric": "sales",
            "account_key": "1" * 64, "skus": ["sku-b", "sku-a", "sku-c"],
        }
        for key, value in changed.items():
            with self.subTest(key=key):
                identity = {**self.identity, key: value}
                with self.assertRaisesRegex(CheckpointError, "不一致"):
                    read_checkpoint(path, identity)

    def test_invalid_identity_and_duplicate_input_are_rejected(self):
        for key, value in (("source_file", "relative.xlsx"), ("source_sha256", "bad"),
                           ("account_key", "username"), ("metric", "revenue"),
                           ("skus", ["sku-a", "sku-a"]), ("skus", [True]), ("sheet_name", "")):
            with self.subTest(key=key, value=value), self.assertRaises(CheckpointError):
                create_checkpoint(self.root, {**self.identity, key: value})

    def test_unknown_version_corrupt_database_and_missing_file_are_rejected(self):
        path = self.write()
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("UPDATE metadata SET value='999' WHERE key='version'")
        with self.assertRaisesRegex(CheckpointError, "版本"):
            read_checkpoint(path, self.identity)
        corrupt = self.root / "损坏.sqlite3"
        corrupt.write_text("private-password-is-never-echoed", encoding="utf-8")
        for invalid in (corrupt, self.root / "missing.sqlite3", self.root):
            with self.subTest(path=invalid), self.assertRaises(CheckpointError) as error:
                read_checkpoint(invalid, self.identity)
            self.assertNotIn("private-password", str(error.exception))

    def test_invalid_rows_rejected_on_save_and_read(self):
        mutations = [
            {"sku": "not-in-source"}, {"status": "pending"}, {"status": []}, {"shop_name": " "},
            {"metric_value": -1}, {"metric_value": True}, {"metric_value": 1.5},
            {"metric_value": None}, {"shop_count": -1}, {"attempts": True},
            {"queried_at": 123}, {"retry_not_before": -1}, {"retry_not_before": True},
            {"retry_not_before": float("inf")}, {"retry_not_before": float("nan")},
            {"retry_not_before": 10 ** 1000},
            {"status": "not_found"}, {"status": "failed"},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                row = {**self.matched(), **mutation}
                with create_checkpoint(self.root, self.identity) as checkpoint:
                    with self.assertRaises(CheckpointError):
                        checkpoint.save_result(row)
                path = self.write()
                self.tamper_row(path, row)
                with self.assertRaises(CheckpointError):
                    read_checkpoint(path, self.identity)

    def test_valid_zero_match_not_found_and_future_cooldown_round_trip(self):
        rows = [self.matched(value=0), {"sku": "sku-b", "status": "not_found"}, self.failed("sku-c")]
        restored = read_checkpoint(self.write(rows), self.identity)
        self.assertEqual(restored["sku-a"]["metric_value"], 0)
        self.assertIsNone(restored["sku-b"]["metric_value"])
        self.assertEqual(restored["sku-b"]["shop_name"], "")
        self.assertEqual(restored["sku-c"]["retry_not_before"], 2000000000.25)

    def test_credentials_and_other_extra_fields_never_persist(self):
        secrets = {"username": "private-user-sentinel", "password": "private-password-sentinel",
                   "cookies": "private-cookie-sentinel", "other": "private-other-sentinel"}
        with create_checkpoint(self.root, {**self.identity, **secrets}) as checkpoint:
            checkpoint.save_result({**self.matched(), **secrets})
            path = checkpoint.path
        content = path.read_bytes()
        for key, value in secrets.items():
            self.assertNotIn(value.encode(), content)
            self.assertNotIn(key, read_checkpoint(path, self.identity)["sku-a"])
        # Additional fields in older or externally edited files are also never
        # exposed to callers, so resuming cannot copy them to a new checkpoint.
        self.tamper_row(path, {**self.matched(), **secrets})
        for key in secrets:
            self.assertNotIn(key, read_checkpoint(path, self.identity)["sku-a"])

    def test_new_runs_have_independent_files_and_reads_do_not_modify_source(self):
        first = self.write()
        original = first.read_bytes()
        old_mtime = first.stat().st_mtime_ns
        restored = read_checkpoint(first, self.identity)
        with create_checkpoint(self.root, self.identity) as second:
            second.save_results(restored.values())
            second.save_result(self.matched(value=88))
            second_path = second.path
        self.assertNotEqual(first, second_path)
        self.assertEqual(first.parent.name, "BigSeller对标进度")
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(first.stat().st_mtime_ns, old_mtime)
        self.assertEqual(read_checkpoint(first, self.identity)["sku-a"]["metric_value"], 42)
        self.assertEqual(read_checkpoint(second_path, self.identity)["sku-a"]["metric_value"], 88)

    def test_batch_validation_does_not_partially_save(self):
        with create_checkpoint(self.root, self.identity) as checkpoint:
            checkpoint.save_result(self.matched())
            for batch in ([self.matched(value=99), {"sku": "outside", "status": "failed"}],
                          [self.matched(value=99), self.matched(value=100)]):
                with self.subTest(batch=batch), self.assertRaises(CheckpointError):
                    checkpoint.save_results(batch)
                self.assertEqual(read_checkpoint(checkpoint.path, self.identity)["sku-a"]["metric_value"], 42)

    def test_persistent_database_lock_retries_three_writes_and_preserves_commits(self):
        with create_checkpoint(self.root, self.identity) as checkpoint:
            checkpoint.save_result(self.matched())
            checkpoint._connection.execute("PRAGMA busy_timeout=1")
            tracked = _CountingConnection(checkpoint._connection)
            checkpoint._connection = tracked
            with closing(sqlite3.connect(checkpoint.path)) as locker:
                locker.execute("BEGIN EXCLUSIVE")
                try:
                    with patch("backend.services.bigseller_benchmark_checkpoint.time.sleep") as sleep:
                        with self.assertRaises(CheckpointError) as error:
                            checkpoint.save_results([self.matched(value=99), self.matched("sku-b")])
                    self.assertIn("占用", str(error.exception))
                    self.assertIn("SQLITE_BUSY", str(error.exception))
                    self.assertEqual(tracked.write_count, 3)
                    self.assertEqual(sleep.call_args_list, [call(0.25), call(0.5)])
                    self.assertFalse(tracked.in_transaction)
                finally:
                    locker.rollback()
            restored = read_checkpoint(checkpoint.path, self.identity)
            self.assertEqual(set(restored), {"sku-a"})
            self.assertEqual(restored["sku-a"]["metric_value"], 42)

    def test_transient_database_lock_releases_during_first_wait_and_saves_batch(self):
        with create_checkpoint(self.root, self.identity) as checkpoint:
            checkpoint.save_result(self.matched())
            checkpoint._connection.execute("PRAGMA busy_timeout=1")
            tracked = _CountingConnection(checkpoint._connection)
            checkpoint._connection = tracked
            with closing(sqlite3.connect(checkpoint.path)) as locker:
                locker.execute("BEGIN EXCLUSIVE")
                with patch("backend.services.bigseller_benchmark_checkpoint.time.sleep",
                           side_effect=lambda _delay: locker.rollback()) as sleep:
                    checkpoint.save_results([self.matched(value=99), self.matched("sku-b")])
            self.assertEqual(tracked.write_count, 2)
            sleep.assert_called_once_with(0.25)
            self.assertFalse(tracked.in_transaction)
            restored = read_checkpoint(checkpoint.path, self.identity)
            self.assertEqual(set(restored), {"sku-a", "sku-b"})
            self.assertEqual(restored["sku-a"]["metric_value"], 99)

    def test_database_full_never_retries_and_rolls_back_current_batch(self):
        with create_checkpoint(self.root, self.identity) as checkpoint:
            checkpoint.save_result(self.matched())
            pages = checkpoint._connection.execute("PRAGMA page_count").fetchone()[0]
            checkpoint._connection.execute(f"PRAGMA max_page_count={pages}")
            tracked = _CountingConnection(checkpoint._connection)
            checkpoint._connection = tracked
            large_row = {**self.matched("sku-b"), "message": "x" * 1_000_000}
            with patch("backend.services.bigseller_benchmark_checkpoint.time.sleep") as sleep:
                with self.assertRaises(CheckpointError) as error:
                    checkpoint.save_results([self.matched(value=99), large_row])
            self.assertIn("磁盘空间不足", str(error.exception))
            self.assertIn("SQLITE_FULL", str(error.exception))
            self.assertEqual(tracked.write_count, 1)
            sleep.assert_not_called()
            self.assertFalse(tracked.in_transaction)
            restored = read_checkpoint(checkpoint.path, self.identity)
            self.assertEqual(set(restored), {"sku-a"})
            self.assertEqual(restored["sku-a"]["metric_value"], 42)

    def test_unpaired_surrogate_reports_encoding_without_echoing_row_contents(self):
        for surrogate in ("\ud83d", "\ude00"):
            with self.subTest(surrogate=repr(surrogate)), create_checkpoint(self.root, self.identity) as checkpoint:
                checkpoint.save_result(self.matched())
                tracked = _CountingConnection(checkpoint._connection)
                checkpoint._connection = tracked
                invalid_row = {**self.matched("sku-b"), "shop_name": "private-shop-sentinel" + surrogate}
                with patch("backend.services.bigseller_benchmark_checkpoint.time.sleep") as sleep:
                    with self.assertRaises(CheckpointError) as error:
                        checkpoint.save_results([self.matched(value=99), invalid_row])
                self.assertIn("编码异常", str(error.exception))
                self.assertNotIn("private-shop-sentinel", str(error.exception))
                self.assertNotIn(surrogate, str(error.exception))
                self.assertEqual(tracked.write_count, 1)
                sleep.assert_not_called()
                self.assertFalse(tracked.in_transaction)
                restored = read_checkpoint(checkpoint.path, self.identity)
                self.assertEqual(set(restored), {"sku-a"})
                self.assertEqual(restored["sku-a"]["metric_value"], 42)

    def test_unknown_sqlite_failure_does_not_leak_original_message_or_retry(self):
        with create_checkpoint(self.root, self.identity) as checkpoint:
            checkpoint.save_result(self.matched())
            cause = sqlite3.OperationalError("private-password-sentinel database detail")
            cause.sqlite_errorname = "private-error-name-sentinel"
            tracked = _CountingConnection(checkpoint._connection, error=cause)
            checkpoint._connection = tracked
            with patch("backend.services.bigseller_benchmark_checkpoint.time.sleep") as sleep:
                with self.assertRaises(CheckpointError) as error:
                    checkpoint.save_result(self.matched(value=99))
            self.assertIn("对标进度保存失败", str(error.exception))
            self.assertNotIn("private-password-sentinel", str(error.exception))
            self.assertNotIn("private-error-name-sentinel", str(error.exception))
            self.assertNotIn("database detail", str(error.exception))
            self.assertEqual(tracked.write_count, 1)
            sleep.assert_not_called()
            self.assertFalse(tracked.in_transaction)
            restored = read_checkpoint(checkpoint.path, self.identity)
            self.assertEqual(restored["sku-a"]["metric_value"], 42)

    def test_duplicate_persisted_skus_and_key_mismatch_are_rejected(self):
        path = self.write()
        self.tamper_row(path, self.matched("sku-b"))
        with self.assertRaisesRegex(CheckpointError, "编号不一致"):
            read_checkpoint(path, self.identity)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("DROP TABLE results")
            connection.execute("CREATE TABLE results(sku TEXT, row_json TEXT)")
            connection.executemany("INSERT INTO results VALUES (?, ?)",
                                   [("sku-a", json.dumps(self.matched()))] * 2)
        with self.assertRaisesRegex(CheckpointError, "重复"):
            read_checkpoint(path, self.identity)

    def test_bad_json_and_duplicate_json_keys_are_rejected(self):
        path = self.write()
        for encoded in ("not-json", '{"sku":"sku-a","sku":"sku-b","status":"failed"}', "[]"):
            with self.subTest(encoded=encoded):
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute("UPDATE results SET row_json=?", (encoded,))
                with self.assertRaises(CheckpointError):
                    read_checkpoint(path, self.identity)

    def test_close_is_idempotent_and_closed_writes_fail(self):
        checkpoint = create_checkpoint(self.root, copy.deepcopy(self.identity))
        checkpoint.close()
        checkpoint.close()
        with self.assertRaisesRegex(CheckpointError, "已关闭"):
            checkpoint.save_result(self.matched())


if __name__ == "__main__":
    unittest.main()
