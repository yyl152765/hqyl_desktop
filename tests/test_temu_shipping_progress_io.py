from __future__ import annotations

import copy
import errno
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import call, patch

from backend.services import temu_shipping_channel as service
from tests.test_temu_shipping_channel import FakeGateway, PASSWORD, USERNAME, channel, workbook


RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


def windows_error(code, message="checkpoint replacement failed"):
    error = OSError(errno.EACCES, message)
    error.winerror = code
    return error


class TemuShippingProgressIOTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="temu-progress-io-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = service.TemuShippingRunStore(self.directory)
        self.key = service.account_key(USERNAME)
        gateway = FakeGateway([channel(1)])
        with patch.object(service, "read_temu_shipping_workbook", return_value=workbook(1)):
            preview = service.preview_temu_shipping(
                username=USERNAME, password=PASSWORD, input_file="daily.xlsx",
                store=self.store, gateway_factory=gateway.factory,
            )
        self.run_id = preview["run_id"]
        self.target = self.directory / f"{self.run_id}.json"
        self.record = self.store.load(self.run_id, self.key)

    def changed_record(self):
        record = copy.deepcopy(self.record)
        record["message"] = "新的保存进度"
        record["rows"][0]["message"] = "等待核验"
        return record

    def test_stores_for_same_normalized_directory_share_one_reentrant_io_lock(self):
        other = service.TemuShippingRunStore(self.directory / ".")
        different = service.TemuShippingRunStore(self.directory / "other")
        self.assertIs(self.store._io_lock, other._io_lock)
        self.assertIsNot(self.store._io_lock, different._io_lock)
        with self.store._io_lock:
            self.assertTrue(other._io_lock.acquire(blocking=False))
            other._io_lock.release()

    def test_save_load_and_latest_all_wait_for_shared_io_lock(self):
        other = service.TemuShippingRunStore(self.directory)
        actions = (
            lambda: other.save(self.changed_record()),
            lambda: other.load(self.run_id, self.key),
            lambda: other.latest(self.key),
        )
        started = [threading.Event() for _ in actions]
        completed = [threading.Event() for _ in actions]
        errors = []

        def worker(index, action):
            started[index].set()
            try:
                action()
            except Exception as exc:
                errors.append(exc)
            finally:
                completed[index].set()

        threads = [threading.Thread(target=worker, args=(index, action), daemon=True)
                   for index, action in enumerate(actions)]
        with self.store._io_lock:
            for thread in threads:
                thread.start()
            for event in started:
                self.assertTrue(event.wait(2))
            self.assertFalse(any(event.wait(0.03) for event in completed))
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(all(event.is_set() for event in completed))

    def test_batch_operation_lock_does_not_block_progress_reads_or_saves(self):
        other = service.TemuShippingRunStore(self.directory)
        completed = threading.Event()
        errors = []

        def read_and_write():
            try:
                self.assertEqual(other.latest(self.key)["run_id"], self.run_id)
                self.assertEqual(other.load(self.run_id, self.key)["run_id"], self.run_id)
                other.save(self.changed_record())
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()

        with self.store.operation(self.key):
            thread = threading.Thread(target=read_and_write, daemon=True)
            thread.start()
            finished_while_operation_locked = completed.wait(3)
        thread.join(3)
        self.assertTrue(finished_while_operation_locked)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_latest_can_reenter_load_without_deadlocking(self):
        completed = threading.Event()
        results = []
        errors = []

        def read_latest():
            try:
                results.append(self.store.latest(self.key))
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()

        with patch.object(self.store, "load", wraps=self.store.load) as load:
            thread = threading.Thread(target=read_latest, daemon=True)
            thread.start()
            self.assertTrue(completed.wait(3), "latest -> load must use a reentrant lock")
            thread.join(1)
            load.assert_called_once_with(self.run_id, self.key)
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["run_id"], self.run_id)

    def test_windows_transient_replace_errors_retry_exact_delays_then_commit(self):
        real_replace = os.replace
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                failures = [windows_error(code) for _ in RETRY_DELAYS]

                def flaky_replace(source, target):
                    if failures:
                        raise failures.pop(0)
                    return real_replace(source, target)

                record = self.changed_record()
                with patch.object(service.os, "replace", side_effect=flaky_replace) as replace, patch.object(service.time, "sleep") as sleep:
                    self.store.save(record)
                self.assertEqual(replace.call_count, 6)
                self.assertEqual(sleep.call_args_list, [call(delay) for delay in RETRY_DELAYS])
                self.assertEqual(self.store.load(self.run_id, self.key)["message"], "新的保存进度")
                self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_persistent_transient_failure_keeps_old_json_and_cleans_temporary_file(self):
        original_bytes = self.target.read_bytes()
        original_error = windows_error(5)
        with patch.object(service.os, "replace", side_effect=original_error) as replace, patch.object(service.time, "sleep") as sleep:
            with self.assertRaises(service.TemuShippingProgressError) as caught:
                self.store.save(self.changed_record())
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertIs(caught.exception.__cause__, original_error)
        self.assertEqual(replace.call_count, 6)
        self.assertEqual(sleep.call_args_list, [call(delay) for delay in RETRY_DELAYS])
        self.assertEqual(self.target.read_bytes(), original_bytes)
        self.assertEqual(self.store.load(self.run_id, self.key)["run_id"], self.run_id)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_nontransient_replace_errors_are_not_retried(self):
        for error in (OSError(errno.ENOSPC, "disk full"), PermissionError(errno.EACCES, "permission denied"), windows_error(87)):
            with self.subTest(error=error):
                original_bytes = self.target.read_bytes()
                with patch.object(service.os, "replace", side_effect=error) as replace, patch.object(service.time, "sleep") as sleep:
                    with self.assertRaises(service.TemuShippingProgressError) as caught:
                        self.store.save(self.changed_record())
                self.assertIs(caught.exception.__cause__, error)
                replace.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(self.target.read_bytes(), original_bytes)
                self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_write_failure_is_wrapped_and_does_not_replace_old_checkpoint(self):
        original_bytes = self.target.read_bytes()
        original_error = OSError(errno.ENOSPC, "flush failed")
        with patch.object(service.os, "fsync", side_effect=original_error), patch.object(service.os, "replace") as replace:
            with self.assertRaises(service.TemuShippingProgressError) as caught:
                self.store.save(self.changed_record())
        self.assertIs(caught.exception.__cause__, original_error)
        replace.assert_not_called()
        self.assertEqual(self.target.read_bytes(), original_bytes)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_cleanup_error_does_not_mask_original_replace_error(self):
        original_bytes = self.target.read_bytes()
        original_error = windows_error(5, "original replacement error")
        cleanup_error = PermissionError("temporary cleanup error")
        with patch.object(service.os, "replace", side_effect=original_error), patch.object(service.time, "sleep"), patch.object(Path, "unlink", side_effect=cleanup_error):
            with self.assertRaises(service.TemuShippingProgressError) as caught:
                self.store.save(self.changed_record())
        self.assertIs(caught.exception.__cause__, original_error)
        self.assertNotIn("temporary cleanup", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), original_bytes)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8"))["run_id"], self.run_id)

    def test_cleanup_error_cannot_turn_successful_commit_into_failure(self):
        with patch.object(Path, "unlink", side_effect=PermissionError("cleanup failed")):
            self.store.save(self.changed_record())
        self.assertEqual(self.store.load(self.run_id, self.key)["message"], "新的保存进度")

    def test_multiple_store_writers_and_readers_finish_without_partial_json_or_deadlock(self):
        stores = [service.TemuShippingRunStore(self.directory) for _ in range(5)]
        barrier = threading.Barrier(len(stores))
        errors = []
        read_count = []

        def worker(index, store):
            try:
                barrier.wait(timeout=3)
                for iteration in range(12):
                    if index < 2:
                        record = self.changed_record()
                        record["message"] = f"writer {index}, version {iteration}"
                        store.save(record)
                    else:
                        record = store.latest(self.key) if iteration % 2 else store.load(self.run_id, self.key)
                        self.assertEqual(record["run_id"], self.run_id)
                        self.assertEqual(record["rows"][0]["length"], "17")
                        read_count.append(1)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index, store), daemon=True)
                   for index, store in enumerate(stores)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(8)
            self.assertFalse(thread.is_alive(), "Concurrent checkpoint I/O deadlocked")
        self.assertEqual(errors, [])
        self.assertEqual(len(read_count), 36)
        self.assertEqual(self.store.load(self.run_id, self.key)["run_id"], self.run_id)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows sharing violations require a real Windows file handle")
    def test_real_windows_read_handle_is_retried_and_released_before_commit(self):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        # Permit readers/writers while denying FILE_SHARE_DELETE, as an external
        # progress poller or file scanner can do when it holds the previous JSON.
        handle = kernel32.CreateFileW(str(self.target), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        retry_started = threading.Event()
        closed = threading.Event()
        release_errors = []
        observed_errors = []
        real_replace = os.replace
        real_sleep = service.time.sleep

        def release_handle():
            try:
                retry_started.wait(3)
                if not kernel32.CloseHandle(handle):
                    raise ctypes.WinError(ctypes.get_last_error())
            except Exception as exc:
                release_errors.append(exc)
            finally:
                closed.set()

        def replace_with_real_sharing_violation(source, target):
            try:
                return real_replace(source, target)
            except OSError as exc:
                observed_errors.append(getattr(exc, "winerror", None))
                raise

        def wait_and_release(delay):
            retry_started.set()
            real_sleep(delay)

        thread = threading.Thread(target=release_handle, daemon=True)
        thread.start()
        try:
            with patch.object(service.os, "replace", side_effect=replace_with_real_sharing_violation) as replace, patch.object(service.time, "sleep", side_effect=wait_and_release) as sleep:
                self.store.save(self.changed_record())
            self.assertGreaterEqual(replace.call_count, 2)
            self.assertTrue(observed_errors)
            self.assertTrue(all(code in {5, 32, 33} for code in observed_errors))
            self.assertEqual(sleep.call_args_list, [call(delay) for delay in RETRY_DELAYS[:sleep.call_count]])
            self.assertEqual(self.store.load(self.run_id, self.key)["message"], "新的保存进度")
        finally:
            retry_started.set()
            thread.join(4)
            self.assertTrue(closed.is_set())
        self.assertEqual(release_errors, [])


if __name__ == "__main__":
    unittest.main()
