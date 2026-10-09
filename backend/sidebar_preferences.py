from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any


_PREFERENCES_LOCK = threading.RLock()
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,80}", re.ASCII)
_SCHEMA_VERSION = 1


def _validated_preferences(preferences: Any) -> dict[str, list[str]]:
    if not isinstance(preferences, dict):
        raise ValueError("侧栏偏好必须是对象")

    result: dict[str, list[str]] = {}
    for key, label, limit in (
        ("favorites", "常用模块", 5),
        ("expandedGroups", "展开分组", 32),
    ):
        items = preferences.get(key)
        if not isinstance(items, list):
            raise ValueError(f"{label}必须是列表")
        unique: list[str] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, str) or _IDENTIFIER.fullmatch(item) is None:
                raise ValueError(f"{label}标识格式不正确")
            if item not in seen:
                seen.add(item)
                unique.append(item)
            if len(unique) > limit:
                raise ValueError(f"{label}最多允许 {limit} 项")
        result[key] = unique
    return result


class SidebarPreferencesStore:
    """Persist navigation preferences separately from business/account settings."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, list[str]] | None:
        with _PREFERENCES_LOCK:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except FileNotFoundError:
                return None
            except (ValueError, UnicodeError):
                return None

            if not isinstance(payload, dict):
                return None
            version = payload.get("schema_version")
            if type(version) is not int or version != _SCHEMA_VERSION:
                return None
            try:
                return _validated_preferences(payload.get("preferences"))
            except ValueError:
                return None

    def save(self, preferences: Any) -> dict[str, list[str]]:
        normalized = _validated_preferences(preferences)
        payload = {"schema_version": _SCHEMA_VERSION, "preferences": normalized}
        with _PREFERENCES_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=".sidebar-", suffix=".tmp", delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    json.dump(payload, temporary, ensure_ascii=False, indent=2)
                    temporary.write("\n")
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_path, self.path)
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError:
                        pass
        return normalized
