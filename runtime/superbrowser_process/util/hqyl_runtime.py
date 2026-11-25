from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


class HqylRuntimeError(RuntimeError):
    """Raised when the managed-runtime contract is incomplete."""


def is_hqyl_runtime(environment: Mapping[str, str] | None = None) -> bool:
    """Return whether this process was launched by an HQYL executor.

    Any HQYL marker opts the process into strict managed mode.  This is
    intentionally fail-closed: a partially populated executor environment must
    not make the process read credentials from a developer's AppData or source
    configuration.
    """

    values = os.environ if environment is None else environment
    return any(str(name).upper().startswith("HQYL_") for name in values)


def required_environment_value(
    name: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    values = os.environ if environment is None else environment
    value = str(values.get(name, "") or "").strip()
    if not value:
        raise HqylRuntimeError(f"HQYL 运行环境缺少必填变量：{name}")
    return value


def resolve_output_directory(
    local_default: str | os.PathLike[str],
    *,
    requested: str | os.PathLike[str] | None = None,
    managed: bool | None = None,
    environment: Mapping[str, str] | None = None,
    create: bool = True,
) -> Path:
    """Resolve the only output root the business process may write to.

    In managed mode ``HQYL_OUTPUT_DIR`` always wins and is mandatory.  A local
    CLI override is therefore unable to redirect an executor run outside the
    attempt output directory.
    """

    values = os.environ if environment is None else environment
    # An explicit local-mode hint must not override an executor marker.
    strict_managed = is_hqyl_runtime(values) or bool(managed)
    if strict_managed:
        selected = required_environment_value("HQYL_OUTPUT_DIR", values)
    else:
        selected = str(requested or local_default).strip()
        if not selected:
            raise HqylRuntimeError("本地输出目录不能为空")

    path = Path(selected).expanduser().resolve()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def environment_override(
    *names: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    values = os.environ if environment is None else environment
    for name in names:
        value = str(values.get(name, "") or "").strip()
        if value:
            return value
    return ""
