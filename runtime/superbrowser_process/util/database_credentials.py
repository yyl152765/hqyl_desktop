"""Runtime credential injection shared by desktop and standalone database users."""
from __future__ import annotations

import os
from typing import Callable

_desktop_provider: Callable[[bool], str] | None = None


def set_desktop_password_provider(provider: Callable[[bool], str]) -> None:
    global _desktop_provider
    _desktop_provider = provider


def resolve_password(fallback: str = "", *, guard: bool = False) -> str:
    if _desktop_provider is not None:
        return _desktop_provider(guard)
    if guard and os.environ.get("HQYL_GUARD_DB_PASSWORD"):
        return os.environ["HQYL_GUARD_DB_PASSWORD"]
    return os.environ.get("PGPASSWORD") or fallback
