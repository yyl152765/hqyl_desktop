"""Validate the dedicated ERP proxy without changing global network policy."""
from __future__ import annotations

from collections.abc import Mapping
from ipaddress import IPv6Address
import os
from urllib.parse import urlsplit


ERP_PROXY_ENVIRONMENT = "RPA_MABANG_ERP_PROXY_URL"
_INVALID_PROXY = "Invalid RPA_MABANG_ERP_PROXY_URL"


def normalize_erp_proxy_url(value: str | None) -> str | None:
    """Accept only an HTTP(S) proxy authority; never echo a rejected value."""
    if value is None or value == "":
        return None
    if (not isinstance(value, str)
            or any(not character.isprintable() or character.isspace() for character in value)
            or any(character in value for character in ("\\", "?", "#"))):
        raise ValueError(_INVALID_PROXY)

    valid = False
    try:
        parsed = urlsplit(value)
        hostname, port = parsed.hostname, parsed.port
        valid = (parsed.scheme in {"http", "https"} and bool(hostname)
                 and parsed.username is None and parsed.password is None
                 and not parsed.path and not parsed.query and not parsed.fragment
                 and (port is None or 1 <= port <= 65535))
        authority = parsed.netloc
        if valid and authority.startswith("["):
            closing = authority.index("]")
            IPv6Address(authority[1:closing])
            suffix = authority[closing + 1:]
            valid = (not suffix or (suffix.startswith(":") and suffix[1:].isascii()
                                   and suffix[1:].isdecimal() and port is not None))
        elif valid:
            valid = (all(character.isalnum() or character in "-._" for character in hostname)
                     and (":" not in authority or (authority.count(":") == 1
                          and authority.rsplit(":", 1)[1].isascii()
                          and authority.rsplit(":", 1)[1].isdecimal() and port is not None)))
    except (TypeError, ValueError):
        # urllib/IP parsers can include the supplied host in their exception.
        # Raise outside this handler so that no sensitive context is retained.
        valid = False
    if not valid:
        raise ValueError(_INVALID_PROXY)
    return value


def current_erp_proxy_url(environment: Mapping[str, str] | None = None) -> str | None:
    values = os.environ if environment is None else environment
    return normalize_erp_proxy_url(values.get(ERP_PROXY_ENVIRONMENT))
