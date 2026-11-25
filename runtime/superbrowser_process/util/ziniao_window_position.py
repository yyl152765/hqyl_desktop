from __future__ import annotations

"""Non-activating, identity-bound positioning of an existing ZiNiao window.

No browser is opened, closed, minimized, or made foreground here.  A caller must
restore its token in ``finally`` (``restore_all`` also covers a failed move), or
explicitly retire a prepared minimized token after confirming its owned profile
was closed and its original HWND no longer exists.
Only an exact title in the actual ``ziniaobrowser.exe`` process is eligible.
The native adapter is lazy, so unit tests can inject a fake on any platform.
"""

import ctypes
import math
import ntpath
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Protocol, Sequence


class WindowPositionError(RuntimeError):
    """A fixed, non-sensitive error code; never includes native exception text."""


_DISPLAY_STATE_CHECKPOINTS = frozenset({
    "move_before", "move_after", "restore_before", "restore_after_placement",
    "restore_after_position", "minimized_close_prepare", "minimized_close_validate",
})


def safe_window_state_diagnostic(
    checkpoint: object, saved_show_cmd: object, observed_show_cmd: object,
) -> tuple[str, int | str, int | str]:
    """Return only fixed enums; never stringify caller/window-derived values."""
    safe_checkpoint = (
        checkpoint if type(checkpoint) is str and checkpoint in _DISPLAY_STATE_CHECKPOINTS
        else "unknown"
    )
    safe_saved = saved_show_cmd if type(saved_show_cmd) is int and saved_show_cmd in (1, 2, 3) else "unknown"
    safe_observed = observed_show_cmd if type(observed_show_cmd) is int and observed_show_cmd in (1, 2, 3) else "unknown"
    return safe_checkpoint, safe_saved, safe_observed


@dataclass(frozen=True)
class WindowRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def intersects(self, other: WindowRect) -> bool:
        return (
            self.left < other.right and self.right > other.left
            and self.top < other.bottom and self.bottom > other.top
        )


@dataclass(frozen=True)
class WindowIdentity:
    hwnd: int
    pid: int
    title: str = field(repr=False)
    process_name: str
    process_created_at: int
    visible: bool = True
    top_level: bool = True


@dataclass(frozen=True)
class WindowPlacement:
    flags: int
    show_cmd: int
    min_position: tuple[int, int]
    max_position: tuple[int, int]
    normal_rect: WindowRect


@dataclass(eq=False)
class WindowRestoreToken:
    identity: WindowIdentity = field(repr=False)
    rect: WindowRect
    placement: WindowPlacement
    restored: bool = False
    restore_started: bool = False
    close_pending: bool = False
    retired_after_confirmed_close: bool = False


class WindowApi(Protocol):
    def find_windows(self, exact_title: str) -> Sequence[WindowIdentity]: ...
    def identity(self, hwnd: int) -> WindowIdentity: ...
    def window_exists(self, hwnd: int) -> bool: ...
    def rect(self, hwnd: int) -> WindowRect: ...
    def placement(self, hwnd: int) -> WindowPlacement: ...
    def virtual_screen(self) -> WindowRect: ...
    def position_no_activate(self, hwnd: int, rect: WindowRect) -> None: ...
    def restore_placement_no_activate(self, hwnd: int, placement: WindowPlacement) -> None: ...


class OffscreenWindowManager:
    """Park one precisely identified window, preserving size and display state.

    Normal and maximized windows are supported without a show-state transition.
    Already minimized/hidden windows are rejected.  A later identity, display
    state change fails closed rather than moving a possibly
    unrelated window or overriding a user's new display state.
    """

    def __init__(
        self,
        *,
        api: WindowApi | None = None,
        margin: int = 64,
        ready_timeout_seconds: float = 6.0,
        ready_poll_interval_seconds: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        diagnostic_callback: Callable[[str, int | str, int | str], None] | None = None,
    ) -> None:
        try:
            timeout = float(ready_timeout_seconds)
            interval = float(ready_poll_interval_seconds)
            valid = (
                math.isfinite(timeout) and 0 <= timeout <= 6.0
                and math.isfinite(interval) and 0 < interval <= 1.0
                and callable(clock) and callable(sleeper)
            )
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            raise WindowPositionError("ziniao_window_wait_config_invalid") from None
        self._api = api if api is not None else Win32WindowApi()
        self._margin = max(32, int(margin))
        self._ready_timeout = timeout
        self._ready_poll_interval = interval
        self._clock = clock
        self._sleep = sleeper
        self._diagnostic_callback = diagnostic_callback
        self._pending: dict[int, WindowRestoreToken] = {}

    @staticmethod
    def _eligible(identity: WindowIdentity, exact_title: str) -> bool:
        return (
            identity.hwnd > 0 and identity.pid > 0
            and identity.process_created_at > 0
            and identity.visible and identity.top_level
            and identity.title == exact_title
            and identity.process_name.casefold() == "ziniaobrowser.exe"
        )

    def _report_display_state_change(
        self, checkpoint: str, saved_show_cmd: object, observed_show_cmd: object,
    ) -> None:
        if self._diagnostic_callback is None:
            return
        try:
            self._diagnostic_callback(*safe_window_state_diagnostic(
                checkpoint, saved_show_cmd, observed_show_cmd,
            ))
        except Exception:
            # A failed diagnostic sink must not replace the validation failure.
            # BaseException cancellation deliberately propagates to cleanup.
            pass

    def _validate_identity(self, token: WindowRestoreToken) -> None:
        matches = [item for item in self._api.find_windows(token.identity.title)
                   if self._eligible(item, token.identity.title)]
        if len(matches) != 1:
            raise WindowPositionError("ziniao_window_identity_ambiguous")
        if matches[0] != token.identity:
            raise WindowPositionError("ziniao_window_identity_changed")
        if self._api.identity(token.identity.hwnd) != token.identity:
            raise WindowPositionError("ziniao_window_identity_changed")

    def _validate(self, token: WindowRestoreToken, *, checkpoint: str) -> None:
        self._validate_identity(token)
        observed_show_cmd = self._api.placement(token.identity.hwnd).show_cmd
        if observed_show_cmd != token.placement.show_cmd:
            self._report_display_state_change(checkpoint, token.placement.show_cmd, observed_show_cmd)
            raise WindowPositionError("ziniao_window_display_state_changed")

    def _require_pending_token(self, token: WindowRestoreToken) -> None:
        if (
            not isinstance(token, WindowRestoreToken)
            or token.restored
            or token.retired_after_confirmed_close
            or self._pending.get(token.identity.hwnd) is not token
        ):
            raise WindowPositionError("ziniao_window_restore_token_invalid")

    @staticmethod
    def _minimized_close_error_code(error: Exception, default: str) -> str:
        # Public close helpers have the same redaction boundary as restore, but
        # never trust text supplied by an injected/native adapter.
        allowed = {
            "ziniao_window_restore_token_invalid", "ziniao_window_identity_ambiguous",
            "ziniao_window_identity_changed", "ziniao_window_display_state_changed",
            "ziniao_window_close_not_confirmed", "ziniao_window_enumeration_failed",
            "ziniao_window_native_call_failed", "ziniao_window_dpi_context_failed",
            "ziniao_window_dpi_context_restore_failed",
        }
        if (
            isinstance(error, WindowPositionError) and len(error.args) == 1
            and type(error.args[0]) is str and error.args[0] in allowed
        ):
            return error.args[0]
        return default

    def prepare_minimized_close(self, token: WindowRestoreToken) -> bool:
        """Read-only classification before the first restore attempt.

        The caller must independently prove that it opened this shop and that
        a terminal business outcome was durably saved. Only a saved-normal or
        saved-maximized, now-minimized exact window may be closed without first
        changing its display state. This method neither closes the window nor
        discards its pending token; ordinary restore remains fail-closed.
        """
        try:
            self._require_pending_token(token)
            self._validate_identity(token)
            observed_show_cmd = self._api.placement(token.identity.hwnd).show_cmd
            if token.restore_started:
                return False
            if not (
                type(token.placement.show_cmd) is int and token.placement.show_cmd in (1, 3)
                and type(observed_show_cmd) is int and observed_show_cmd == 2
            ):
                return False
            if not token.close_pending:
                self._report_display_state_change("minimized_close_prepare", token.placement.show_cmd, 2)
                token.close_pending = True
            return True
        except Exception as exc:
            raise WindowPositionError(self._minimized_close_error_code(
                exc, "ziniao_window_minimized_close_check_failed",
            )) from None

    def validate_minimized_close(self, token: WindowRestoreToken) -> None:
        """Recheck the precise pending minimized window immediately before close."""
        try:
            self._require_pending_token(token)
            if (
                not token.close_pending or token.restore_started
                or type(token.placement.show_cmd) is not int or token.placement.show_cmd not in (1, 3)
            ):
                raise WindowPositionError("ziniao_window_restore_token_invalid")
            self._validate_identity(token)
            observed_show_cmd = self._api.placement(token.identity.hwnd).show_cmd
            if type(observed_show_cmd) is not int or observed_show_cmd != 2:
                self._report_display_state_change(
                    "minimized_close_validate", token.placement.show_cmd, observed_show_cmd,
                )
                raise WindowPositionError("ziniao_window_display_state_changed")
        except Exception as exc:
            raise WindowPositionError(self._minimized_close_error_code(
                exc, "ziniao_window_minimized_close_check_failed",
            )) from None

    def retire_closed_minimized(self, token: WindowRestoreToken) -> None:
        """Retire after the caller confirms profile closure and HWND is absent.

        This is not a successful position restore. A still-existing HWND,
        including a reused one, or an unreadable result keeps the token pending.
        """
        try:
            self._require_pending_token(token)
            if (
                not token.close_pending or token.restore_started
                or type(token.placement.show_cmd) is not int or token.placement.show_cmd not in (1, 3)
            ):
                raise WindowPositionError("ziniao_window_restore_token_invalid")
            if self._api.window_exists(token.identity.hwnd) is not False:
                raise WindowPositionError("ziniao_window_close_not_confirmed")
            token.retired_after_confirmed_close = True
            token.close_pending = False
            del self._pending[token.identity.hwnd]
        except Exception as exc:
            raise WindowPositionError(self._minimized_close_error_code(
                exc, "ziniao_window_close_confirmation_failed",
            )) from None

    def _wait_for_window(self, exact_title: str) -> Sequence[WindowIdentity]:
        """Wait read-only for creation, never retry an open, move, or task.

        Only a completely absent title may be polled.  An existing candidate,
        including an ineligible one, immediately returns for strict validation.
        Native failures and cancellation propagate without a polling retry.
        """
        deadline = self._clock() + self._ready_timeout
        while True:
            candidates = self._api.find_windows(exact_title)
            if candidates:
                return candidates
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise WindowPositionError("ziniao_window_ready_timeout")
            self._sleep(min(self._ready_poll_interval, remaining))

    def move_offscreen(self, exact_title: str) -> WindowRestoreToken:
        """Move outside the entire virtual desktop, or roll back a failed move.

        If a native error also prevents rollback, the token remains owned by this
        manager and ``restore_all`` can perform the same guarded cleanup later.
        All public errors contain fixed codes, never titles, paths, or API text.
        """
        token = None
        try:
            if not isinstance(exact_title, str) or not exact_title.strip():
                raise WindowPositionError("ziniao_window_title_required")
            matches = [item for item in self._wait_for_window(exact_title)
                       if self._eligible(item, exact_title)]
            if len(matches) != 1:
                code = "ambiguous" if matches else "not_found"
                raise WindowPositionError(f"ziniao_window_{code}")
            identity = matches[0]
            if identity.hwnd in self._pending:
                raise WindowPositionError("ziniao_window_already_parked")
            rect = self._api.rect(identity.hwnd)
            placement = self._api.placement(identity.hwnd)
            if placement.show_cmd not in (1, 3):
                raise WindowPositionError("ziniao_window_state_not_supported")
            desktop = self._api.virtual_screen()
            if min(rect.width, rect.height, desktop.width, desktop.height) <= 0:
                raise WindowPositionError("ziniao_window_geometry_invalid")
            # The complete rectangle is beyond the left-most monitor, including
            # arrangements with negative monitor coordinates.  Keep its size.
            right = desktop.left - self._margin
            target = WindowRect(right - rect.width, desktop.top, right,
                                desktop.top + rect.height)
            if min(target.left, target.top) < -(2 ** 31) or max(target.right, target.bottom) >= 2 ** 31:
                raise WindowPositionError("ziniao_window_geometry_invalid")
            token = WindowRestoreToken(identity, rect, placement)
            self._validate(token, checkpoint="move_before")
            # Record before the first mutation, including for partial failure.
            self._pending[identity.hwnd] = token
            self._api.position_no_activate(identity.hwnd, target)
            self._validate(token, checkpoint="move_after")
            observed = self._api.rect(identity.hwnd)
            if observed.intersects(self._api.virtual_screen()) or (
                observed.width, observed.height
            ) != (rect.width, rect.height):
                raise WindowPositionError("ziniao_window_move_not_confirmed")
            return token
        except Exception as exc:
            if token is not None and self._pending.get(token.identity.hwnd) is token:
                try:
                    self.restore(token)
                except WindowPositionError:
                    raise WindowPositionError("ziniao_window_move_and_restore_failed") from None
            code = str(exc) if isinstance(exc, WindowPositionError) else "ziniao_window_move_failed"
            raise WindowPositionError(code) from None

    def restore(self, token: WindowRestoreToken) -> None:
        """Restore only the very same window; successful cleanup is idempotent."""
        try:
            if not isinstance(token, WindowRestoreToken):
                raise WindowPositionError("ziniao_window_restore_token_invalid")
            if token.restored:
                return
            if self._pending.get(token.identity.hwnd) is not token:
                raise WindowPositionError("ziniao_window_restore_token_invalid")
            if token.close_pending:
                # Once the caller has selected the no-state-change close path,
                # even a later unminimize must not turn cleanup into a restore.
                # Keep the token pending until closure is independently proved.
                raise WindowPositionError("ziniao_window_minimized_close_pending")
            token.restore_started = True
            self._validate(token, checkpoint="restore_before")
            # WINDOWPLACEMENT uses workspace coordinates; only feed it back to
            # SetWindowPlacement, never treat rcNormalPosition as screen pixels.
            self._api.restore_placement_no_activate(token.identity.hwnd, token.placement)
            self._validate(token, checkpoint="restore_after_placement")
            self._api.position_no_activate(token.identity.hwnd, token.rect)
            self._validate(token, checkpoint="restore_after_position")
            if self._api.rect(token.identity.hwnd) != token.rect:
                raise WindowPositionError("ziniao_window_restore_not_confirmed")
            if self._api.placement(token.identity.hwnd) != token.placement:
                raise WindowPositionError("ziniao_window_restore_state_not_confirmed")
            token.restored = True
            del self._pending[token.identity.hwnd]
        except Exception as exc:
            code = str(exc) if isinstance(exc, WindowPositionError) else "ziniao_window_restore_failed"
            raise WindowPositionError(code) from None

    def restore_all(self) -> None:
        """Attempt each owned token even if another window is no longer valid."""
        failed = False
        for token in list(self._pending.values()):
            try:
                self.restore(token)
            except WindowPositionError:
                failed = True
        if failed:
            raise WindowPositionError("ziniao_window_cleanup_incomplete")


class Win32WindowApi:
    """Minimal ctypes adapter; no third-party GUI libraries or global input."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise WindowPositionError("ziniao_window_windows_only")
        from ctypes import wintypes

        self._w = wintypes
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class Placement(ctypes.Structure):
            _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT),
                        ("showCmd", wintypes.UINT), ("ptMinPosition", wintypes.POINT),
                        ("ptMaxPosition", wintypes.POINT), ("rcNormalPosition", wintypes.RECT)]

        self._placement_type = Placement
        self._enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        bindings = [
            (self._user, "EnumWindows", [self._enum_callback, wintypes.LPARAM], wintypes.BOOL),
            (self._user, "IsWindow", [wintypes.HWND], wintypes.BOOL),
            (self._user, "IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
            (self._user, "GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
            (self._user, "GetWindowTextLengthW", [wintypes.HWND], ctypes.c_int),
            (self._user, "GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self._user, "GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            (self._user, "GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            (self._user, "GetWindowPlacement", [wintypes.HWND, ctypes.POINTER(Placement)], wintypes.BOOL),
            (self._user, "SetWindowPlacement", [wintypes.HWND, ctypes.POINTER(Placement)], wintypes.BOOL),
            (self._user, "GetSystemMetrics", [ctypes.c_int], ctypes.c_int),
            (self._user, "SetWindowPos", [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
            (self._kernel, "OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            (self._kernel, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            (self._kernel, "QueryFullProcessImageNameW", [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                         ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (self._kernel, "GetProcessTimes", [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4,
             wintypes.BOOL),
        ]
        for library, name, arguments, result in bindings:
            function = getattr(library, name)
            function.argtypes = arguments
            function.restype = result
        if not hasattr(self._user, "SetThreadDpiAwarenessContext"):
            raise WindowPositionError("ziniao_window_dpi_context_unavailable")
        self._user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        self._user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p

    @contextmanager
    def _physical_pixels(self):
        # Thread-local only; never change the browser/process DPI configuration.
        previous = self._user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous:
            raise WindowPositionError("ziniao_window_dpi_context_failed")
        try:
            yield
        finally:
            if not self._user.SetThreadDpiAwarenessContext(previous):
                raise WindowPositionError("ziniao_window_dpi_context_restore_failed")

    @staticmethod
    def _require(ok) -> None:
        if not ok:
            raise WindowPositionError("ziniao_window_native_call_failed")

    def _title(self, hwnd: int) -> str:
        length = self._user.GetWindowTextLengthW(hwnd)
        if length < 0 or length > 32767:
            raise WindowPositionError("ziniao_window_title_invalid")
        buffer = ctypes.create_unicode_buffer(length + 1)
        # Other applications may change titles between these two calls.  The
        # returned text is enough to discard unrelated windows; a matching
        # candidate is read and revalidated separately by ``identity``.
        self._user.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def _pid(self, hwnd: int) -> int:
        pid = self._w.DWORD()
        self._require(self._user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)))
        return int(pid.value)

    def identity(self, hwnd: int) -> WindowIdentity:
        self._require(self._user.IsWindow(hwnd))
        pid, title = self._pid(hwnd), self._title(hwnd)
        handle = self._kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        self._require(handle)
        try:
            length = self._w.DWORD(32768)
            path = ctypes.create_unicode_buffer(length.value)
            self._require(self._kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)))
            times = [self._w.FILETIME() for _ in range(4)]
            self._require(self._kernel.GetProcessTimes(handle, *[ctypes.byref(item) for item in times]))
            created_at = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            process_name = ntpath.basename(path.value).casefold()
        finally:
            self._kernel.CloseHandle(handle)
        if not self._user.IsWindow(hwnd) or self._pid(hwnd) != pid or self._title(hwnd) != title:
            raise WindowPositionError("ziniao_window_identity_changed")
        return WindowIdentity(hwnd, pid, title, process_name, created_at,
                              bool(self._user.IsWindowVisible(hwnd)),
                              self._user.GetAncestor(hwnd, 2) == hwnd)

    def window_exists(self, hwnd: int) -> bool:
        # IsWindow returning zero means absent, not a native call failure.
        # Never query or mutate an unrelated replacement window here.
        return bool(self._user.IsWindow(hwnd))

    def find_windows(self, exact_title: str) -> list[WindowIdentity]:
        matches: list[WindowIdentity] = []
        errors: list[Exception] = []

        @self._enum_callback
        def callback(hwnd, _):
            try:
                # Include an exact-title hidden window as an ineligible signal:
                # only a genuinely absent title may enter readiness polling.
                if self._title(hwnd) == exact_title:
                    matches.append(self.identity(int(hwnd)))
                return True
            except Exception as exc:
                errors.append(exc)
                return False

        ok = self._user.EnumWindows(callback, 0)
        if errors:
            raise WindowPositionError("ziniao_window_enumeration_failed") from None
        self._require(ok)
        return matches

    def rect(self, hwnd: int) -> WindowRect:
        native = self._w.RECT()
        with self._physical_pixels():
            self._require(self._user.GetWindowRect(hwnd, ctypes.byref(native)))
        return WindowRect(native.left, native.top, native.right, native.bottom)

    def placement(self, hwnd: int) -> WindowPlacement:
        native = self._placement_type()
        native.length = ctypes.sizeof(native)
        with self._physical_pixels():
            self._require(self._user.GetWindowPlacement(hwnd, ctypes.byref(native)))
        normal = native.rcNormalPosition
        return WindowPlacement(int(native.flags), int(native.showCmd),
                               (native.ptMinPosition.x, native.ptMinPosition.y),
                               (native.ptMaxPosition.x, native.ptMaxPosition.y),
                               WindowRect(normal.left, normal.top, normal.right, normal.bottom))

    def virtual_screen(self) -> WindowRect:
        with self._physical_pixels():
            left, top, width, height = [self._user.GetSystemMetrics(index) for index in (76, 77, 78, 79)]
        return WindowRect(left, top, left + width, top + height)

    def position_no_activate(self, hwnd: int, rect: WindowRect) -> None:
        with self._physical_pixels():
            # NOACTIVATE | NOZORDER | NOOWNERZORDER; never foreground/show/hide.
            self._require(self._user.SetWindowPos(hwnd, None, rect.left, rect.top,
                                                  rect.width, rect.height, 0x0010 | 0x0004 | 0x0200))

    def restore_placement_no_activate(self, hwnd: int, placement: WindowPlacement) -> None:
        native = self._placement_type()
        native.length = ctypes.sizeof(native)
        native.flags = placement.flags
        native.showCmd = 8  # SW_SHOWNA preserves current normal/maximized state.
        native.ptMinPosition = self._w.POINT(*placement.min_position)
        native.ptMaxPosition = self._w.POINT(*placement.max_position)
        normal = placement.normal_rect
        native.rcNormalPosition = self._w.RECT(normal.left, normal.top, normal.right, normal.bottom)
        with self._physical_pixels():
            self._require(self._user.SetWindowPlacement(hwnd, ctypes.byref(native)))
