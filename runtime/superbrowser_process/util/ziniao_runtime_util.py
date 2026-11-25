from __future__ import annotations

import os
import sys


def get_runtime_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _existing_file(path: str | None) -> str | None:
    if path and os.path.isfile(path):
        return os.path.abspath(path)
    return None


def _existing_dir(path: str | None) -> str | None:
    if path and os.path.isdir(path):
        return os.path.abspath(path)
    return None


def _normalize_client_executable(path: str | None) -> str | None:
    text = str(path or "").strip().strip('"')
    if not text:
        return None
    if "," in text:
        text = text.split(",", 1)[0].strip().strip('"')

    candidates: list[str] = []
    if os.path.isfile(text):
        candidates.append(text)
    elif os.path.isdir(text):
        for name in ("ziniao.exe", "SuperBrowser.exe", "starter.exe"):
            candidates.append(os.path.join(text, name))

    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        filename = os.path.basename(candidate).lower()
        if filename == "starter.exe":
            parent = os.path.dirname(candidate)
            for sibling in ("ziniao.exe", "SuperBrowser.exe"):
                sibling_path = os.path.join(parent, sibling)
                if os.path.isfile(sibling_path):
                    return os.path.abspath(sibling_path)
            continue
        return os.path.abspath(candidate)
    return None


def _iter_registry_client_paths() -> list[str]:
    try:
        import winreg
    except Exception:
        return []

    registry_paths = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    results: list[str] = []
    keywords = ("紫鸟", "ziniao", "superbrowser")

    for root, subkey in registry_paths:
        try:
            with winreg.OpenKey(root, subkey) as uninstall_key:
                key_count = winreg.QueryInfoKey(uninstall_key)[0]
                for index in range(key_count):
                    try:
                        child_name = winreg.EnumKey(uninstall_key, index)
                        with winreg.OpenKey(uninstall_key, child_name) as child_key:
                            display_name = str(_safe_reg_query(child_key, "DisplayName") or "").lower()
                            if not any(keyword in display_name for keyword in keywords):
                                continue

                            display_icon = str(_safe_reg_query(child_key, "DisplayIcon") or "")
                            install_location = str(_safe_reg_query(child_key, "InstallLocation") or "")

                            for raw_path in (display_icon, install_location):
                                normalized = _normalize_registry_path(raw_path)
                                if normalized:
                                    results.append(normalized)
                    except Exception:
                        continue
        except Exception:
            continue

    return results


def _safe_reg_query(key, value_name: str):
    try:
        import winreg

        value, _ = winreg.QueryValueEx(key, value_name)
        return value
    except Exception:
        return None


def _normalize_registry_path(value: str) -> str | None:
    return _normalize_client_executable(value)


def resolve_ziniao_client_path(configured_path: str | None, version: str = "v6") -> str:
    runtime_root = get_runtime_root()
    env_path = os.environ.get("ZINIAO_CLIENT_PATH", "")
    preferred_exe = "SuperBrowser.exe" if str(version).lower() == "v5" else "ziniao.exe"

    candidate_files = [
        configured_path,
        env_path,
        os.path.join(runtime_root, preferred_exe),
        os.path.join(runtime_root, "ziniao", "ziniao.exe"),
        os.path.join(runtime_root, "SuperBrowser", "SuperBrowser.exe"),
        os.path.join(runtime_root, "紫鸟浏览器V6", "ziniao", "ziniao.exe"),
        os.path.join(runtime_root, "SuperBrowser V6", "ziniao", "ziniao.exe"),
    ]

    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA"):
        base_dir = os.environ.get(env_name, "")
        if not base_dir:
            continue
        candidate_files.extend(
            [
                os.path.join(base_dir, "ziniao", "ziniao.exe"),
                os.path.join(base_dir, "SuperBrowser", "SuperBrowser.exe"),
                os.path.join(base_dir, "SuperBrowser V6", "ziniao", "ziniao.exe"),
                os.path.join(base_dir, "紫鸟浏览器V6", "ziniao", "ziniao.exe"),
            ]
        )

    for drive in ("C:\\", "D:\\", "E:\\"):
        candidate_files.extend(
            [
                os.path.join(drive, "ziniao", "ziniao.exe"),
                os.path.join(drive, "SuperBrowser", "SuperBrowser.exe"),
                os.path.join(drive, "SuperBrowser V6", "ziniao", "ziniao.exe"),
                os.path.join(drive, "紫鸟浏览器V6", "ziniao", "ziniao.exe"),
            ]
        )

    candidate_files.extend(_iter_registry_client_paths())

    for candidate in candidate_files:
        resolved = _normalize_client_executable(candidate)
        if resolved:
            return resolved

    configured_resolved = _normalize_client_executable(configured_path)
    return configured_resolved or ""


def resolve_webdriver_path(configured_path: str | None) -> str:
    runtime_root = get_runtime_root()
    env_path = os.environ.get("ZINIAO_WEBDRIVER_PATH", "")

    candidate_dirs = [
        env_path,
        configured_path,
        os.path.join(runtime_root, "ziniaodriver"),
        os.path.join(runtime_root, "_internal", "ziniaodriver"),
        os.path.join(runtime_root, "drivers", "ziniaodriver"),
        os.path.join(runtime_root, "webdriver"),
        os.path.join(os.getcwd(), "ziniaodriver"),
    ]

    for candidate in candidate_dirs:
        resolved = _existing_dir(candidate)
        if resolved:
            return resolved

    fallback_dir = os.path.join(runtime_root, "ziniaodriver")
    os.makedirs(fallback_dir, exist_ok=True)
    return fallback_dir
