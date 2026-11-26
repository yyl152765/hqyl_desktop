"""Atomic JSON checkpoints with bounded recovery from Windows sharing conflicts."""
from contextlib import suppress
import json
import os
from pathlib import Path
import time
from uuid import uuid4


REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 0.8, 0.8)


def write_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    serialized = False
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        serialized = True
        for delay in (*REPLACE_RETRY_DELAYS, None):
            try:
                os.replace(temp, path)
                return
            except PermissionError as exc:
                if delay is None:
                    raise PermissionError(
                        f"清单文件持续被占用或没有写入权限，有限重试后仍无法保存；"
                        f"原清单未覆盖，最新临时清单已保留：{temp}"
                    ) from exc
                time.sleep(delay)
    finally:
        # Keep a fully serialized checkpoint if replacement fails permanently.
        # Cleanup of an incomplete temporary file must not mask the original error.
        if not serialized:
            with suppress(OSError):
                temp.unlink(missing_ok=True)
