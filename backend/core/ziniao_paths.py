"""Discover an installed ZiNiao client without legacy projects or side effects."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None

_CLIENT_NAMES = ("ziniao.exe", "SuperBrowser.exe")
_UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
_APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"


def _registry_value(key, name: str) -> str:
    try:
        value, _ = winreg.QueryValueEx(key, name)
        return value.strip() if isinstance(value, str) else ""
    except OSError:
        return ""


def _registry_client_paths() -> list[str]:
    if winreg is None:
        return []
    candidates: list[str] = []
    views = dict.fromkeys((0, getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)))
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in views:
            access = winreg.KEY_READ | view
            for name in _CLIENT_NAMES:
                try:
                    with winreg.OpenKey(root, _APP_PATHS + "\\" + name, 0, access) as key:
                        candidates.extend(_registry_value(key, field) for field in ("", "Path"))
                except OSError:
                    continue
            try:
                with winreg.OpenKey(root, _UNINSTALL, 0, access) as uninstall:
                    index = 0
                    while True:
                        try:
                            name = winreg.EnumKey(uninstall, index)
                        except OSError:
                            break
                        index += 1
                        try:
                            with winreg.OpenKey(uninstall, name, 0, access) as key:
                                label = (_registry_value(key, "DisplayName") + " " + name).casefold()
                                if any(marker in label for marker in ("紫鸟", "ziniao", "superbrowser")):
                                    candidates.extend(_registry_value(key, field) for field in ("InstallLocation", "DisplayIcon"))
                        except OSError:
                            continue
            except OSError:
                continue
    return list(dict.fromkeys(value for value in candidates if value))


def _client_files(value: str | Path) -> list[Path]:
    text = re.sub(r",\s*-?\d+\s*$", "", str(value or "").strip()).strip().strip('"')
    if not text:
        return []
    try:
        path = Path(os.path.expandvars(text)).expanduser()
        if path.name.casefold() in {name.casefold() for name in _CLIENT_NAMES}:
            candidates = (path,)
        elif path.name.casefold() == "starter.exe":
            candidates = tuple(path.with_name(name) for name in _CLIENT_NAMES)
        elif path.is_dir():
            candidates = tuple(path / name for name in _CLIENT_NAMES)
        else:
            return []
        return [candidate for candidate in candidates if candidate.is_file()]
    except (OSError, ValueError):
        return []


def _preferred_client(values) -> str:
    files = [path for value in values for path in _client_files(value)]
    for name in _CLIENT_NAMES:  # Prefer V6 even if the registry lists V5 first.
        for path in files:
            if path.name.casefold() == name.casefold():
                return str(path)
    return ""


def resolve_ziniao_client_path() -> str:
    """Return a real client executable, or an empty string if none is found."""
    explicit = _preferred_client([os.environ.get("ZINIAO_CLIENT_PATH", "")])
    if explicit:
        return explicit
    registered = _preferred_client(_registry_client_paths())
    if registered:
        return registered
    app_root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    roots = [app_root]
    roots.extend(Path(os.environ[name]) for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA") if os.environ.get(name))
    folders = []
    for root in roots:
        folders.extend((root, root / "ziniao", root / "SuperBrowser",
                        root / "Programs" / "ziniao", root / "Programs" / "SuperBrowser"))
    return _preferred_client(folders)
