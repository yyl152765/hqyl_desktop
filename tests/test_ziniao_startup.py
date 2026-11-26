from __future__ import annotations

from dataclasses import replace
import ctypes as C
import os
from pathlib import Path
import socket
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from backend.core.ziniao_startup import (
    ProcessIdentity, ZiniaoStartupError, _WindowsProcesses,
    prepare_ziniao_client, ziniao_port_owned,
)


class FakeProcesses:
    def __init__(self, records=()):
        self.records = {item.pid: item for item in records}
        self.closed, self.terminated = [], []
        self.listeners = set()
        self.stuck = set()
        self.close_exits = False
        self.after_close = None
        self.after_terminate = None
        self.deny = False

    def current(self):
        return 'S-1-current', 7

    def snapshot(self):
        return list(self.records.values())

    def listener_pids(self, port):
        return self.listeners.intersection(self.records)

    def alive(self, record):
        return self.records.get(record.pid) == record

    def request_close(self, record):
        if self.deny:
            raise PermissionError('private-account-secret')
        if self.alive(record):
            self.closed.append(record.pid)
            if self.close_exits:
                self.records.pop(record.pid)
            if self.after_close:
                self.after_close(record)

    def terminate(self, record):
        if self.alive(record):
            self.terminated.append(record.pid)
            if record.pid not in self.stuck:
                self.records.pop(record.pid)
            if self.after_terminate:
                self.after_terminate(record)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.client = Path(self.temporary.name) / 'ziniao.exe'
        self.client.write_bytes(b'fixture, never executed')
        self.clock = 0
        self.root = ProcessIdentity(10, 1, 100, str(self.client), 7, 'S-1-current', 'ziniao.exe')

    def sleep(self, seconds):
        self.clock += seconds

    def prepare(self, backend, **kwargs):
        return prepare_ziniao_client(str(self.client), _backend=backend,
                                     _clock=lambda: self.clock, _sleep=self.sleep, **kwargs)

    def test_no_process_no_mutation_no_delay(self):
        backend = FakeProcesses()
        self.assertEqual(self.prepare(backend), 0)
        self.assertEqual(backend.closed + backend.terminated, [])
        self.assertEqual(self.clock, 0)

    def test_exact_path_and_verified_descendants_only(self):
        child = replace(self.root, pid=11, ppid=10, created=101, exe=str(self.client.with_name('chrome.exe')), name='chrome.exe')
        grandchild = replace(child, pid=12, ppid=11, created=102)
        same_name_elsewhere = replace(self.root, pid=20, exe=str(self.client.parent / 'another' / 'ziniao.exe'))
        same_folder_other_exe = replace(self.root, pid=21, exe=str(self.client.with_name('chrome.exe')), name='chrome.exe')
        other_user = replace(self.root, pid=22, sid='S-other')
        other_session = replace(self.root, pid=23, session=8)
        foreign_child = replace(child, pid=24, sid='S-other')
        stale_parent_pid = replace(child, pid=25, created=80)
        backend = FakeProcesses([grandchild, same_name_elsewhere, same_folder_other_exe, other_user,
                                 other_session, foreign_child, stale_parent_pid, child, self.root])
        backend.listeners = {10, 11}
        self.assertEqual(self.prepare(backend), 3)
        self.assertEqual(backend.terminated, [10, 11, 12])
        self.assertEqual(set(backend.records), {20, 21, 22, 23, 24, 25})

    def test_wm_close_success_does_not_force_terminate(self):
        backend = FakeProcesses([self.root])
        backend.close_exits = True
        self.assertEqual(self.prepare(backend), 1)
        self.assertEqual(backend.closed, [10])
        self.assertEqual(backend.terminated, [])

    def test_new_descendant_during_close_is_captured(self):
        backend = FakeProcesses([self.root])
        child = replace(self.root, pid=11, ppid=10, created=101, exe=str(self.client.with_name('chrome.exe')))
        backend.after_close = lambda _: backend.records.update({11: child})
        self.assertEqual(self.prepare(backend), 2)
        self.assertEqual(backend.terminated, [10, 11])

    def test_new_orphan_after_root_exits_is_not_silently_ignored(self):
        backend = FakeProcesses([self.root])
        child = replace(self.root, pid=11, ppid=10, created=101, exe=str(self.client.with_name('chrome.exe')))
        backend.close_exits = True
        backend.after_close = lambda _: backend.records.update({11: child})
        with self.assertRaisesRegex(ZiniaoStartupError, '新建店铺子进程'):
            self.prepare(backend)
        self.assertEqual(backend.terminated, [])

    def test_new_orphan_during_force_exit_is_not_silently_ignored(self):
        backend = FakeProcesses([self.root])
        child = replace(self.root, pid=11, ppid=10, created=101, exe=str(self.client.with_name('chrome.exe')))
        backend.after_terminate = lambda _: backend.records.update({11: child})
        with self.assertRaisesRegex(ZiniaoStartupError, '新建店铺子进程'):
            self.prepare(backend)
        self.assertEqual(backend.terminated, [10])

    def test_orphan_with_reused_known_child_pid_is_not_silently_ignored(self):
        child = replace(self.root, pid=11, ppid=10, created=101, exe=str(self.client.with_name('chrome.exe')))
        replacement = replace(child, created=999)
        backend = FakeProcesses([self.root, child])
        backend.close_exits = True
        def replace_after_close(record):
            if record.pid == 11:
                backend.records[11] = replacement
        backend.after_close = replace_after_close
        with self.assertRaisesRegex(ZiniaoStartupError, '新建店铺子进程'):
            self.prepare(backend)
        self.assertEqual(backend.terminated, [])

    def test_reused_pid_different_process_is_not_terminated(self):
        backend = FakeProcesses([self.root])
        reused = replace(self.root, created=999, exe=str(self.client.with_name('other.exe')), name='other.exe')
        backend.after_close = lambda _: backend.records.update({10: reused})
        self.assertEqual(self.prepare(backend), 1)
        self.assertEqual(backend.terminated, [])
        self.assertEqual(backend.records[10], reused)

    def test_client_restarts_during_termination_stops_without_killing_new_root(self):
        backend = FakeProcesses([self.root])
        restarted = replace(self.root, pid=50, created=999)
        backend.after_terminate = lambda _: backend.records.update({50: restarted})
        with self.assertRaisesRegex(ZiniaoStartupError, '重新启动'):
            self.prepare(backend)
        self.assertEqual(backend.terminated, [10])
        self.assertEqual(backend.records[50], restarted)

    def test_new_root_during_graceful_close_is_not_force_killed(self):
        for pid in (10, 50):
            with self.subTest(pid=pid):
                backend = FakeProcesses([self.root])
                restarted = replace(self.root, pid=pid, created=999)
                backend.after_close = lambda _: backend.records.update({pid: restarted})
                with self.assertRaisesRegex(ZiniaoStartupError, '重新启动'):
                    self.prepare(backend)
                self.assertEqual(backend.terminated, [])
                self.assertEqual(backend.records[pid], restarted)

    def test_unrelated_listener_rejects_before_any_close(self):
        foreign = replace(self.root, pid=40, exe=str(self.client.with_name('server.exe')))
        backend = FakeProcesses([self.root, foreign])
        backend.listeners = {40}
        with self.assertRaisesRegex(ZiniaoStartupError, '端口 16851'):
            self.prepare(backend)
        self.assertEqual(backend.closed + backend.terminated, [])

    def test_listener_reused_pid_never_passes(self):
        backend = FakeProcesses([self.root])
        backend.listener_pids = Mock(return_value={10})
        backend.alive = Mock(return_value=False)
        with self.assertRaisesRegex(ZiniaoStartupError, '归属无法确认'):
            self.prepare(backend)
        self.assertEqual(backend.closed + backend.terminated, [])

    def test_port_ownership_requires_real_current_client(self):
        backend = FakeProcesses([self.root])
        self.assertFalse(ziniao_port_owned(str(self.client), _backend=backend))
        backend.listeners = {10}
        self.assertTrue(ziniao_port_owned(str(self.client), _backend=backend))
        backend.records[10] = replace(self.root, session=8)
        with self.assertRaisesRegex(ZiniaoStartupError, '归属无法确认'):
            ziniao_port_owned(str(self.client), _backend=backend)

    def test_occupied_port_after_cleanup_is_not_success(self):
        backend = FakeProcesses([self.root])
        backend.listener_pids = Mock(side_effect=[set(), {999}])
        with self.assertRaisesRegex(ZiniaoStartupError, '尚未释放'):
            self.prepare(backend)

    def test_access_denied_redacts_exception_and_stops(self):
        backend = FakeProcesses([self.root])
        backend.deny = True
        with self.assertRaises(ZiniaoStartupError) as caught:
            self.prepare(backend)
        self.assertNotIn('private-account-secret', str(caught.exception))
        self.assertEqual(backend.terminated, [])

    def test_unverifiable_candidate_or_child_stops_before_any_close(self):
        candidates = [replace(self.root, exe='', created=0, sid=''),
                      replace(self.root, pid=11, ppid=10, exe='', created=0, sid='', name='chrome.exe')]
        for record in candidates:
            with self.subTest(record=record.pid):
                backend = FakeProcesses([self.root, record])
                with self.assertRaises(ZiniaoStartupError):
                    self.prepare(backend)
                self.assertEqual(backend.closed + backend.terminated, [])

    def test_inaccessible_other_session_root_is_left_alone(self):
        backend = FakeProcesses([replace(self.root, exe='', created=0, sid='', session=8)])
        self.assertEqual(self.prepare(backend), 0)

    def test_exit_timeout_is_bounded_and_no_second_forced_kill(self):
        backend = FakeProcesses([self.root])
        backend.stuck = {10}
        with self.assertRaisesRegex(ZiniaoStartupError, '未能退出'):
            self.prepare(backend)
        self.assertLess(self.clock, 7.3)
        self.assertEqual(backend.terminated, [10])

    def test_v5_and_missing_client_do_not_query_or_kill_processes(self):
        backend = Mock()
        v5 = self.client.with_name('SuperBrowser.exe')
        v5.write_bytes(b'not executed')
        for path in (v5, self.client.with_name('missing.exe')):
            with self.subTest(path=path.name), self.assertRaisesRegex(ZiniaoStartupError, '紫鸟 V6'):
                prepare_ziniao_client(str(path), _backend=backend)
        self.assertFalse(backend.mock_calls)

    def test_close_ignores_only_window_disappearing_during_enumeration(self):
        for code in (1400, 5):
            with self.subTest(code=code):
                adapter = object.__new__(_WindowsProcesses)
                adapter.user = Mock()
                adapter.callback_type = lambda callback: callback
                adapter.alive = Mock(return_value=True)
                def get_pid(hwnd, pointer):
                    pointer._obj.value = self.root.pid
                def post(*args):
                    C.set_last_error(code)
                    return False
                adapter.user.GetWindowThreadProcessId.side_effect = get_pid
                adapter.user.PostMessageW.side_effect = post
                adapter.user.EnumWindows.side_effect = lambda visit, arg: visit(123, arg)
                if code == 1400:
                    adapter.request_close(self.root)
                else:
                    with self.assertRaises(OSError) as error:
                        adapter.request_close(self.root)
                    self.assertEqual(error.exception.winerror, 5)

    def test_native_terminate_revalidates_handle_identity_and_pid_reuse(self):
        adapter = object.__new__(_WindowsProcesses)
        adapter.kernel = Mock()
        adapter.kernel.OpenProcess.return_value = 321
        adapter.kernel.WaitForSingleObject.return_value = 258
        adapter.kernel.TerminateProcess.return_value = True
        from backend.core.ziniao_startup import _path
        normalized = replace(self.root, exe=_path(self.root.exe))
        adapter._identity = Mock(return_value=normalized)
        adapter.terminate(self.root)
        adapter.kernel.OpenProcess.assert_called_once_with(0x1000 | 0x100000 | 1, False, 10)
        adapter.kernel.TerminateProcess.assert_called_once_with(321, 0)
        adapter.kernel.CloseHandle.assert_called_once_with(321)
        for changed in (replace(normalized, created=999), replace(normalized, session=8),
                        replace(normalized, sid='S-other'), replace(normalized, exe='unrelated.exe')):
            adapter.kernel.reset_mock()
            adapter._identity.return_value = changed
            adapter.terminate(self.root)
            adapter.kernel.TerminateProcess.assert_not_called()
            adapter.kernel.CloseHandle.assert_called_once_with(321)

    def snapshot_adapter(self):
        adapter = object.__new__(_WindowsProcesses)
        adapter.kernel = Mock()
        adapter.kernel.CreateToolhelp32Snapshot.return_value = 100
        adapter._session = Mock(return_value=7)
        adapter._identity = Mock(side_effect=PermissionError('private-path-canary'))
        def first(_handle, pointer):
            entry = pointer._obj
            entry.th32ProcessID = 10
            entry.th32ParentProcessID = 1
            entry.szExeFile = 'ziniao.exe'
            return True
        def end(_handle, _pointer):
            C.set_last_error(18)
            return False
        adapter.kernel.Process32FirstW.side_effect = first
        adapter.kernel.Process32NextW.side_effect = end
        return adapter

    def test_snapshot_omits_gone_process_but_preserves_access_denied(self):
        from backend.core.ziniao_startup import _select, _path
        for code in (87, 1168, 5):
            with self.subTest(code=code):
                adapter = self.snapshot_adapter()
                def failed_open(*_):
                    C.set_last_error(code)
                    return None
                adapter.kernel.OpenProcess.side_effect = failed_open
                records = adapter.snapshot()
                adapter._identity.assert_not_called()
                adapter.kernel.CloseHandle.assert_called_once_with(100)
                if code in (87, 1168):
                    self.assertEqual(records, [])
                    adapter._session.assert_not_called()
                else:
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0].exe, '')
                    with self.assertRaisesRegex(ZiniaoStartupError, '无法核验现有紫鸟进程'):
                        _select(records, _path(str(self.client)), ('S-1-current', 7))

    def test_snapshot_omits_only_signalled_identity_failure(self):
        from backend.core.ziniao_startup import _select, _path
        for state in (0, 258, 0xffffffff):
            with self.subTest(state=state):
                adapter = self.snapshot_adapter()
                adapter.kernel.OpenProcess.return_value = 321
                adapter.kernel.WaitForSingleObject.return_value = state
                records = adapter.snapshot()
                adapter.kernel.OpenProcess.assert_called_once_with(0x1000 | 0x100000, False, 10)
                adapter.kernel.WaitForSingleObject.assert_called_once_with(321, 0)
                self.assertEqual([item.args[0] for item in adapter.kernel.CloseHandle.call_args_list], [321, 100])
                if state == 0:
                    self.assertEqual(records, [])
                    adapter._session.assert_not_called()
                else:
                    self.assertEqual(len(records), 1)
                    with self.assertRaisesRegex(ZiniaoStartupError, '无法核验现有紫鸟进程'):
                        _select(records, _path(str(self.client)), ('S-1-current', 7))

    @unittest.skipUnless(os.name == 'nt', 'native read-only Win32 smoke')
    def test_native_snapshot_sid_and_tcp_read_only_no_process_mutation(self):
        backend = _WindowsProcesses()
        with (patch.object(backend, 'request_close', side_effect=AssertionError('must not close')),
              patch.object(backend, 'terminate', side_effect=AssertionError('must not terminate'))):
            sid, session = backend.current()
            me = next(item for item in backend.snapshot() if item.pid == os.getpid())
            self.assertEqual((me.sid, me.session), (sid, session))
            self.assertTrue(me.created and me.exe)
            self.assertTrue(backend.alive(me))
            with socket.socket() as server:
                server.bind(('127.0.0.1', 0))
                server.listen(1)
                self.assertIn(os.getpid(), backend.listener_pids(server.getsockname()[1]))

    @unittest.skipUnless(os.name == 'nt', 'native owned-fixture Win32 smoke')
    def test_native_restart_only_owned_synthetic_tree_and_releases_port(self):
        compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        if not compiler.is_file():
            self.skipTest('Local C# compiler unavailable')
        source = self.client.with_suffix('.cs')
        source.write_text('''
using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Threading;
class Fixture {
  static void Main(string[] args) {
    if (args[0] == "worker") { Thread.Sleep(60000); return; }
    var info = new ProcessStartInfo(args[1], "worker");
    info.UseShellExecute = false; info.CreateNoWindow = true;
    var child = Process.Start(info);
    var server = new TcpListener(IPAddress.Loopback, 0);
    server.Start();
    File.WriteAllText(args[2], child.Id + "\\n" + ((IPEndPoint)server.LocalEndpoint).Port);
    Thread.Sleep(60000);
  }
}''', encoding='utf-8')
        built = subprocess.run([str(compiler), '/nologo', '/target:exe', '/out:' + str(self.client), str(source)],
                               capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(built.returncode, 0, built.stdout.decode(errors='replace'))
        unrelated_folder = self.client.parent / 'unrelated'
        worker_folder = self.client.parent / 'store-core'
        unrelated_folder.mkdir()
        worker_folder.mkdir()
        unrelated_client = unrelated_folder / 'ziniao.exe'
        worker = worker_folder / 'worker.exe'
        shutil.copyfile(self.client, unrelated_client)
        shutil.copyfile(self.client, worker)
        ready = self.client.with_name('ready.txt')
        survivor = subprocess.Popen([str(unrelated_client), 'worker'], creationflags=subprocess.CREATE_NO_WINDOW)
        root = subprocess.Popen([str(self.client), 'root', str(worker), str(ready)],
                                creationflags=subprocess.CREATE_NO_WINDOW)
        native = _WindowsProcesses()
        owned_child = None
        try:
            deadline = time.monotonic() + 10
            while not ready.is_file() and time.monotonic() < deadline:
                self.assertIsNone(root.poll())
                time.sleep(.05)
            self.assertTrue(ready.is_file(), 'fixture failed to become ready')
            child_pid, port = (int(item) for item in ready.read_text().splitlines())
            records = native.snapshot()
            owned_child = next(item for item in records if item.pid == child_pid)
            self.assertEqual(owned_child.ppid, root.pid)
            self.assertTrue(ziniao_port_owned(str(self.client), port=port))
            try:
                count = prepare_ziniao_client(str(self.client), port=port)
            except ZiniaoStartupError as error:
                import traceback
                self.fail(''.join(traceback.format_exception(error.__context__ or error)))
            # Windows may create an additional console host under this tree.
            self.assertGreaterEqual(count, 2)
            root.wait(timeout=5)
            self.assertFalse(native.alive(owned_child))
            self.assertFalse(ziniao_port_owned(str(self.client), port=port))
            self.assertIsNone(survivor.poll(), 'unrelated same-name fixture must remain running')
        finally:
            # All three binaries belong to this test's TemporaryDirectory. Only
            # their captured identities/Popen handles can be stopped on failure.
            if owned_child is None:
                owned_child = next((item for item in native.snapshot()
                                    if item.ppid == root.pid and Path(item.exe) == worker), None)
            if owned_child is not None:
                native.terminate(owned_child)
            for owned in (root, survivor):
                if owned.poll() is None:
                    owned.terminate()
                owned.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
