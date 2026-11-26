"""Restart only the selected ZiNiao V6 installation in this Windows session.

No process-name killing, shell commands, credentials, or profile deletion. The
native implementation rechecks each process identity against an open handle
before signalling it; tests inject a backend and never touch desktop processes.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import os
from pathlib import Path
import socket
import time


class ZiniaoStartupError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    ppid: int
    created: int
    exe: str
    session: int
    sid: str
    name: str = ''


def _path(value: str) -> str:
    return os.path.normcase(os.path.realpath(value)) if value else ''


def _client(value: str) -> str:
    try:
        candidate = Path(value).resolve(strict=True)
        if candidate.name.casefold() != 'ziniao.exe' or not candidate.is_file():
            raise ValueError()
        return _path(str(candidate))
    except (OSError, ValueError, TypeError):
        raise ZiniaoStartupError('未找到紫鸟 V6 客户端，请先安装紫鸟 V6 或重新选择 ziniao.exe') from None


def _select(records, client: str, current: tuple[str, int]):
    sid, session = current
    selected = {}
    for item in records:
        # An inaccessible apparent V6 process must not be silently treated as
        # absent. A different session, when known, is outside our scope.
        if (item.name.casefold() == 'ziniao.exe' and not item.exe
                and item.session in (-1, session)):
            raise ZiniaoStartupError('无法核验现有紫鸟进程，请检查当前 Windows 用户的进程访问权限')
        if _path(item.exe) == client and item.sid == sid and item.session == session:
            if not item.created:
                raise ZiniaoStartupError('无法核验紫鸟进程创建时间，已停止自动重启')
            selected[item.pid] = item
    while True:
        added = False
        for item in records:
            parent = selected.get(item.ppid)
            if item.pid in selected or parent is None:
                continue
            if item.session not in (-1, session) or (item.sid and item.sid != sid):
                continue
            if not item.exe or not item.sid or not item.created or item.session == -1:
                raise ZiniaoStartupError('无法核验紫鸟子进程，已停止自动重启')
            # Parent PID alone does not establish ancestry after PID reuse.
            if item.created >= parent.created:
                selected[item.pid] = item
                added = True
        if not added:
            return selected


def _owned_listeners(backend, selected, port):
    listeners = backend.listener_pids(port)
    if any(pid not in selected or not backend.alive(selected[pid]) for pid in listeners):
        raise ZiniaoStartupError(f'自动化端口 {port} 被其他程序占用或归属无法确认，请关闭占用程序后重试')
    return listeners


def _reject_unverified_orphans(records, known, selected, current):
    """Do not report a clean restart after an ancestor exits during WM_CLOSE.

    A new child with only a historical parent PID cannot safely be terminated:
    the parent may already have exited and its PID may have been reused. Stop
    with a useful error instead of silently leaving that store window behind.
    """
    sid, session = current
    for item in records:
        parent = known.get(item.ppid)
        if parent is None or item.pid in selected or known.get(item.pid) == item:
            continue
        if item.session not in (-1, session) or (item.sid and item.sid != sid):
            continue
        if not item.created or item.created >= parent.created:
            raise ZiniaoStartupError('关闭紫鸟期间发现新建店铺子进程且归属无法再次确认；请关闭残留店铺窗口后重试')


def ziniao_port_owned(client_path: str, *, port: int = 16851, _backend=None) -> bool:
    """False means no listener; unverified/foreign listeners raise, never pass."""
    client = _client(client_path)
    backend = _backend or _WindowsProcesses()
    try:
        selected = _select(backend.snapshot(), client, backend.current())
        return bool(_owned_listeners(backend, selected, port))
    except ZiniaoStartupError:
        raise
    except Exception:
        raise ZiniaoStartupError('无法核验紫鸟自动化端口归属，请检查当前 Windows 用户的进程访问权限') from None


def prepare_ziniao_client(client_path: str, *, port: int = 16851, progress=None,
                         _backend=None, _clock=time.monotonic, _sleep=time.sleep) -> int:
    """Close selected V6 processes once before a batch; return their count.

    The caller owns task exclusivity and starts the V6 automation client after
    this returns. Previews must not call this method. Call ziniao_port_owned
    while waiting for the newly started client, before sending account details.
    """
    client = _client(client_path)
    backend = _backend or _WindowsProcesses()
    try:
        current = backend.current()
        records = backend.snapshot()
        selected = _select(records, client, current)
        _owned_listeners(backend, selected, port)
        if progress:
            progress('正在准备紫鸟 V6：关闭本次客户端的现有店铺窗口并重新启动自动化模式')
        # Capture children before the parent exits, including store browsers
        # whose binaries live outside the main installation directory.
        for process in selected.values():
            if backend.alive(process):
                backend.request_close(process)

        def wait_for_exit(seconds):
            deadline = _clock() + seconds
            while True:
                alive = [item for item in selected.values() if backend.alive(item)]
                if not alive or _clock() >= deadline:
                    return alive
                _sleep(.1)

        wait_for_exit(2.0)
        # Refresh while roots are still identifiable to catch children created
        # during WM_CLOSE. Identity checks prevent mutating a reused PID.
        refreshed_records = backend.snapshot()
        refreshed = _select(refreshed_records, client, current)
        _reject_unverified_orphans(refreshed_records, selected, refreshed, current)
        for pid, item in refreshed.items():
            previous = selected.get(pid)
            if ((previous is not None and previous != item)
                    or (previous is None and _path(item.exe) == client and item.ppid not in selected)):
                raise ZiniaoStartupError('紫鸟客户端在清理期间重新启动，已停止本次任务，请避免同时运行其他紫鸟任务')
        selected.update(refreshed)
        alive = [item for item in selected.values() if backend.alive(item)]
        # Stop parents first so they cannot respawn their already known children.
        for item in sorted(alive, key=lambda record: record.created):
            backend.terminate(item)
        if wait_for_exit(5.0):
            raise ZiniaoStartupError('紫鸟进程未能退出，已停止本次启动；请关闭相关店铺窗口后重试')
        # Never kill newly discovered roots in an unbounded loop. A restart by
        # another app/user is a conflict, not permission to fight that process.
        final_records = backend.snapshot()
        remaining = _select(final_records, client, current)
        _reject_unverified_orphans(final_records, selected, remaining, current)
        if remaining:
            raise ZiniaoStartupError('紫鸟客户端在清理期间重新启动，已停止本次任务，请避免同时运行其他紫鸟任务')
        if backend.listener_pids(port):
            raise ZiniaoStartupError(f'自动化端口 {port} 尚未释放，已停止启动，请稍后重试')
        return len(selected)
    except ZiniaoStartupError:
        raise
    except Exception:
        raise ZiniaoStartupError('自动重启紫鸟失败：无法访问或结束已核验的客户端进程，请检查权限后重试') from None


class _PROCESSENTRY32(C.Structure):
    _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('th32ProcessID', W.DWORD),
                ('th32DefaultHeapID', C.c_size_t), ('th32ModuleID', W.DWORD),
                ('cntThreads', W.DWORD), ('th32ParentProcessID', W.DWORD),
                ('pcPriClassBase', W.LONG), ('dwFlags', W.DWORD), ('szExeFile', W.WCHAR * 260)]


class _SID_AND_ATTRIBUTES(C.Structure):
    _fields_ = [('Sid', C.c_void_p), ('Attributes', W.DWORD)]


class _TCP4(C.Structure):
    _fields_ = [(name, W.DWORD) for name in ('state', 'address', 'port', 'remote_address', 'remote_port', 'pid')]


class _TCP6(C.Structure):
    _fields_ = [('address', C.c_ubyte * 16), ('scope', W.DWORD), ('port', W.DWORD),
                ('remote_address', C.c_ubyte * 16), ('remote_scope', W.DWORD),
                ('remote_port', W.DWORD), ('state', W.DWORD), ('pid', W.DWORD)]


class _WindowsProcesses:
    """Win32 adapter. Read-only enumeration; mutation requires exact identity."""

    def __init__(self):
        if os.name != 'nt':
            raise ZiniaoStartupError('自动重启紫鸟目前仅支持 Windows')
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.advapi = C.WinDLL('advapi32', use_last_error=True)
        self.user = C.WinDLL('user32', use_last_error=True)
        self.ip = C.WinDLL('iphlpapi', use_last_error=True)
        self._bind(self.kernel, 'OpenProcess', W.HANDLE, [W.DWORD, W.BOOL, W.DWORD])
        self._bind(self.kernel, 'CloseHandle', W.BOOL, [W.HANDLE])
        self._bind(self.kernel, 'CreateToolhelp32Snapshot', W.HANDLE, [W.DWORD, W.DWORD])
        self._bind(self.kernel, 'Process32FirstW', W.BOOL, [W.HANDLE, C.POINTER(_PROCESSENTRY32)])
        self._bind(self.kernel, 'Process32NextW', W.BOOL, [W.HANDLE, C.POINTER(_PROCESSENTRY32)])
        self._bind(self.kernel, 'QueryFullProcessImageNameW', W.BOOL,
                   [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)])
        self._bind(self.kernel, 'GetProcessTimes', W.BOOL, [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4)
        self._bind(self.kernel, 'ProcessIdToSessionId', W.BOOL, [W.DWORD, C.POINTER(W.DWORD)])
        self._bind(self.kernel, 'WaitForSingleObject', W.DWORD, [W.HANDLE, W.DWORD])
        self._bind(self.kernel, 'TerminateProcess', W.BOOL, [W.HANDLE, W.UINT])
        self._bind(self.kernel, 'LocalFree', C.c_void_p, [C.c_void_p])
        self._bind(self.advapi, 'OpenProcessToken', W.BOOL, [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)])
        self._bind(self.advapi, 'GetTokenInformation', W.BOOL,
                   [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)])
        self._bind(self.advapi, 'ConvertSidToStringSidW', W.BOOL, [C.c_void_p, C.POINTER(W.LPWSTR)])
        self.callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        self._bind(self.user, 'EnumWindows', W.BOOL, [self.callback_type, W.LPARAM])
        self._bind(self.user, 'GetWindowThreadProcessId', W.DWORD, [W.HWND, C.POINTER(W.DWORD)])
        self._bind(self.user, 'PostMessageW', W.BOOL, [W.HWND, W.UINT, W.WPARAM, W.LPARAM])
        self._bind(self.ip, 'GetExtendedTcpTable', W.DWORD,
                   [C.c_void_p, C.POINTER(W.DWORD), W.BOOL, W.ULONG, C.c_int, W.ULONG])

    @staticmethod
    def _bind(library, name, restype, args):
        fn = getattr(library, name)
        fn.restype, fn.argtypes = restype, args

    def _session(self, pid):
        session = W.DWORD()
        if not self.kernel.ProcessIdToSessionId(pid, C.byref(session)):
            raise C.WinError(C.get_last_error())
        return session.value

    def _sid(self, handle):
        token = W.HANDLE()
        if not self.advapi.OpenProcessToken(handle, 8, C.byref(token)):
            raise C.WinError(C.get_last_error())
        try:
            size = W.DWORD()
            self.advapi.GetTokenInformation(token, 1, None, 0, C.byref(size))
            if not 0 < size.value <= 65536:
                raise OSError('token size')
            buffer = C.create_string_buffer(size.value)
            if not self.advapi.GetTokenInformation(token, 1, buffer, size, C.byref(size)):
                raise C.WinError(C.get_last_error())
            sid = C.cast(buffer, C.POINTER(_SID_AND_ATTRIBUTES)).contents.Sid
            text = W.LPWSTR()
            if not self.advapi.ConvertSidToStringSidW(sid, C.byref(text)):
                raise C.WinError(C.get_last_error())
            try:
                return text.value
            finally:
                self.kernel.LocalFree(C.cast(text, C.c_void_p))
        finally:
            self.kernel.CloseHandle(token)

    def _identity(self, handle, pid, ppid=0, name=''):
        created, exited, kernel_time, user_time = (W.FILETIME() for _ in range(4))
        if not self.kernel.GetProcessTimes(handle, C.byref(created), C.byref(exited),
                                           C.byref(kernel_time), C.byref(user_time)):
            raise C.WinError(C.get_last_error())
        capacity = W.DWORD(32768)
        path = C.create_unicode_buffer(capacity.value)
        if not self.kernel.QueryFullProcessImageNameW(handle, 0, path, C.byref(capacity)):
            raise C.WinError(C.get_last_error())
        return ProcessIdentity(pid, ppid, (created.dwHighDateTime << 32) | created.dwLowDateTime,
                               _path(path.value), self._session(pid), self._sid(handle), name)

    def current(self):
        handle = self.kernel.OpenProcess(0x1000, False, os.getpid())
        if not handle:
            raise C.WinError(C.get_last_error())
        try:
            return self._sid(handle), self._session(os.getpid())
        finally:
            self.kernel.CloseHandle(handle)

    def snapshot(self):
        snap = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if snap == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        try:
            entry = _PROCESSENTRY32()
            entry.dwSize = C.sizeof(entry)
            records = []
            has_entry = self.kernel.Process32FirstW(snap, C.byref(entry))
            if not has_entry:
                raise C.WinError(C.get_last_error())
            while has_entry:
                pid, ppid, name = entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile
                # SYNCHRONIZE allows a handle-based exit check if the process
                # disappears between enumeration and image/token inspection.
                handle = self.kernel.OpenProcess(0x1000 | 0x100000, False, pid)
                gone = not handle and C.get_last_error() in (87, 1168)
                try:
                    if gone:
                        record = None
                    elif not handle:
                        raise OSError('process inaccessible')
                    else:
                        record = self._identity(handle, pid, ppid, name)
                except OSError:
                    if handle and self.kernel.WaitForSingleObject(handle, 0) == 0:
                        record = None
                    else:
                        try:
                            session = self._session(pid)
                        except OSError:
                            session = -1
                        record = ProcessIdentity(pid, ppid, 0, '', session, '', name)
                finally:
                    if handle:
                        self.kernel.CloseHandle(handle)
                if record is not None:
                    records.append(record)
                has_entry = self.kernel.Process32NextW(snap, C.byref(entry))
            if C.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise C.WinError(C.get_last_error())
            return records
        finally:
            self.kernel.CloseHandle(snap)

    def _matching_handle(self, record, access=0):
        handle = self.kernel.OpenProcess(0x1000 | 0x100000 | access, False, record.pid)
        if not handle:
            if C.get_last_error() in (87, 1168):
                return None  # process disappeared
            raise C.WinError(C.get_last_error())
        try:
            if self.kernel.WaitForSingleObject(handle, 0) == 0:
                self.kernel.CloseHandle(handle)
                return None
            try:
                actual = self._identity(handle, record.pid)
            except OSError:
                # A process in the final part of exit can reject image/token
                # queries before its handle becomes signalled. Observe that
                # exact handle briefly; never reinterpret live access denial.
                if self.kernel.WaitForSingleObject(handle, 500) == 0:
                    self.kernel.CloseHandle(handle)
                    return None
                raise
            if ((actual.created, actual.exe, actual.session, actual.sid)
                    != (record.created, _path(record.exe), record.session, record.sid)):
                self.kernel.CloseHandle(handle)
                return None
            return handle
        except Exception:
            self.kernel.CloseHandle(handle)
            raise

    def alive(self, record):
        handle = self._matching_handle(record)
        if not handle:
            return False
        self.kernel.CloseHandle(handle)
        return True

    def request_close(self, record):
        errors = []

        @self.callback_type
        def visit(hwnd, _):
            pid = W.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, C.byref(pid))
            if pid.value == record.pid:
                try:
                    if self.alive(record):
                        if not self.user.PostMessageW(hwnd, 0x10, 0, 0):  # WM_CLOSE
                            error = C.get_last_error()
                            # A window may disappear after enumeration while its
                            # verified process exits. Continue the normal PID/port checks.
                            if error != 1400:  # ERROR_INVALID_WINDOW_HANDLE
                                raise C.WinError(error)
                except Exception as error:
                    errors.append(error)
                    return False
            return True

        result = self.user.EnumWindows(visit, 0)
        if errors:
            raise errors[0]
        if not result:
            raise C.WinError(C.get_last_error())

    def terminate(self, record):
        handle = self._matching_handle(record, access=1)
        if handle:
            try:
                if not self.kernel.TerminateProcess(handle, 0):
                    # Exiting between identity check and termination is fine.
                    error = C.get_last_error()
                    if self.kernel.WaitForSingleObject(handle, 500) != 0:
                        raise C.WinError(error)
                self.kernel.WaitForSingleObject(handle, 1000)
            finally:
                self.kernel.CloseHandle(handle)

    def listener_pids(self, port):
        owners = set()
        for family, row_type in ((2, _TCP4), (23, _TCP6)):
            size = W.DWORD()
            result = self.ip.GetExtendedTcpTable(None, C.byref(size), False, family, 3, 0)
            if result not in (0, 122) or size.value < 4 or size.value > 16 * 1024 * 1024:
                raise OSError('tcp table unavailable')
            # Table can grow between sizing and reading; retry a bounded number.
            for _ in range(3):
                buffer = C.create_string_buffer(size.value)
                result = self.ip.GetExtendedTcpTable(buffer, C.byref(size), False, family, 3, 0)
                if result != 122:
                    break
                if size.value > 16 * 1024 * 1024:
                    raise OSError('tcp table size')
            if result:
                raise OSError('tcp table unavailable')
            count = W.DWORD.from_buffer(buffer).value
            if 4 + count * C.sizeof(row_type) > len(buffer):
                raise OSError('tcp table invalid')
            for index in range(count):
                item = row_type.from_buffer(buffer, 4 + index * C.sizeof(row_type))
                if item.state == 2 and socket.ntohs(item.port & 0xffff) == port:
                    owners.add(item.pid)
        return owners
