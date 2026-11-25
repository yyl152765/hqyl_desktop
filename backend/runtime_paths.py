"""Resolve runtime code and templates owned by this desktop application."""
from __future__ import annotations

import sys
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _source_runtime(name: str) -> Path:
    root = project_root().resolve()
    runtime = (root / "runtime" / name).resolve()
    if not runtime.is_relative_to(root):
        raise RuntimeError("桌面运行组件必须位于本项目 runtime 目录内。")
    return runtime


def _frozen_root() -> Path:
    payload = getattr(sys, "_MEIPASS", None)
    return Path(payload) if payload else Path(sys.executable).resolve().parent / "_internal"


def superbrowser_root() -> Path:
    if getattr(sys, "frozen", False):
        return _frozen_root()
    return _source_runtime("superbrowser_process")


def mabang_root() -> Path:
    if getattr(sys, "frozen", False):
        return _frozen_root() / "mabang_process"
    return _source_runtime("mabang_process")


def ensure_superbrowser_import_path() -> str:
    # In the executable these modules live in PyInstaller's archive. Importing
    # main to probe for them can accidentally run launcher/main.py in source use.
    if getattr(sys, "frozen", False):
        return "<bundled>"
    runtime = superbrowser_root()
    if not (runtime / "main").is_dir():
        raise ModuleNotFoundError(f"缺少桌面内置运行组件，请检查本项目 runtime/superbrowser_process：{runtime}")
    for name in ("main", "implement", "util", "config"):
        loaded = sys.modules.get(name)
        locations = list(getattr(loaded, "__path__", ()) or ())
        filename = getattr(loaded, "__file__", None)
        if filename:
            locations.append(filename)
        if any(not Path(location).resolve().is_relative_to(runtime) for location in locations):
            raise ModuleNotFoundError(f"检测到其他项目的 {name} 模块已加载，请从本桌面项目重新启动。")
    path = str(runtime)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
    return path
