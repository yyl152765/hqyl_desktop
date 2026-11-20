"""Connect the Windows tax client workflow to the desktop task runner."""
from __future__ import annotations

import csv
import logging
import os
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from backend.services import tax_declaration


def _default_settings(local_data_dir: Path, now: datetime) -> tax_declaration.Settings:
    desktop = tax_declaration.system_desktop_directory()
    settings = tax_declaration.Settings(
        workbook=desktop / tax_declaration.DEFAULT_WORKBOOK.name,
        app_path=Path(os.getenv("TAX_APP_PATH") or tax_declaration.DEFAULT_APP_PATH),
        resource_root=local_data_dir / "tax_rpa_assets",
        result_file=desktop / tax_declaration.DEFAULT_RESULT_FILE.name,
        export_start_month=f"{now.year}-01",
        export_end_month=now.strftime("%Y-%m"),
        export_records_only=True,
    )
    return replace(settings, declaration_month=tax_declaration.declaration_archive_month(settings, now=now))


def get_defaults(local_data_dir: Path) -> dict[str, str]:
    now = datetime.now()
    settings = _default_settings(local_data_dir, now)
    return {
        "workbook": str(settings.workbook),
        "app_path": str(settings.app_path),
        "export_start_month": settings.export_start_month or "",
        "export_end_month": settings.export_end_month or "",
        "declaration_month": settings.declaration_month or "",
        "export_dir": str(tax_declaration.declaration_export_directory(settings, now=now)),
    }


def _existing_file(value: Any, suffixes: set[str], label: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f"请选择{label}")
    path = Path(str(value).strip()).expanduser()
    if not path.is_absolute() or path.suffix.lower() not in suffixes or not path.is_file():
        raise ValueError(f"请选择有效的{label}完整路径")
    return path.resolve(strict=True)


def validate_request(payload: dict[str, Any], local_data_dir: Path) -> tax_declaration.Settings:
    if not isinstance(payload, dict):
        raise ValueError("申报参数格式不正确")
    mode = payload.get("mode", "export_only")
    if mode not in ("export_only", "declare"):
        raise ValueError("请选择有效的执行模式")
    if mode == "declare" and payload.get("confirm_declaration") is not True:
        raise ValueError("请先确认正式申报会向电子税务局提交数据")
    now = datetime.now()
    defaults = _default_settings(local_data_dir, now)
    workbook = _existing_file(payload.get("workbook", defaults.workbook), {".xlsx", ".xlsm"}, "账号表")
    app_path = _existing_file(payload.get("app_path", defaults.app_path), {".exe"}, "税务客户端")
    if app_path.name.casefold() != tax_declaration.DEFAULT_APP_PATH.name.casefold():
        raise ValueError(f"请选择税务客户端启动程序 {tax_declaration.DEFAULT_APP_PATH.name}")
    max_rows = None
    raw_max_rows = payload.get("max_rows")
    if raw_max_rows not in (None, ""):
        count = str(raw_max_rows).strip()
        if not count.isascii() or not count.isdecimal() or len(count) > 7 or not int(count):
            raise ValueError("执行家数必须为正整数")
        max_rows = int(count)
    settings = replace(
        defaults,
        workbook=workbook,
        app_path=app_path,
        max_rows=max_rows,
        export_records_only=mode == "export_only",
        export_start_month=payload.get("export_start_month") or defaults.export_start_month,
        export_end_month=payload.get("export_end_month") or defaults.export_end_month,
    )
    start, end = tax_declaration.declaration_export_period(settings, now=now)
    export_dir = tax_declaration.declaration_export_directory(settings, now=now)
    run_dir = export_dir / "运行结果" / now.strftime("%Y%m%d_%H%M%S_%f")
    result_file = run_dir / defaults.result_file.name
    return replace(
        settings, export_start_month=start, export_end_month=end, export_dir=export_dir,
        result_file=result_file, log_file=result_file.with_suffix(".log"),
    )


def run_tax_declaration(
    settings: tax_declaration.Settings, progress: Callable[[str], None],
) -> dict[str, Any]:
    progress("仅导出记录，不发送申报" if settings.export_records_only else "已确认正式申报模式")
    progress("请保持桌面解锁，任务结束前不要操作税务客户端或鼠标")
    log_path = settings.log_file or settings.result_file.with_suffix(".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger = tax_declaration.LOGGER
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        _ensure_client_closed()
        result = tax_declaration.execute(
            settings, configure_run_logging=False,
            on_progress=lambda completed, total: progress(f"已处理 {completed}/{total} 家公司"),
        )
    except Exception:
        logger.exception("电子税务局任务未完成")
        progress(f"任务未完成，详细日志：{log_path}")
        raise
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    export_failed = sum("申报记录导出失败：" in item["detail"] for item in result["results"])
    completion = f"处理 {result['total']} 家，失败 {result['failed']} 家，已申报但导出失败 {export_failed} 家，跳过 {result['skipped']} 家"
    progress(completion)
    return {
        **{key: result[key] for key in ("output_file", "total", "successful", "failed", "skipped")},
        "log_file": str(log_path),
        "export_dir": str(settings.export_dir),
        "export_failed": export_failed,
        "is_complete": not (result["failed"] or export_failed),
        "completion_message": completion,
    }


def _ensure_client_closed() -> None:
    if os.name != "nt":
        raise tax_declaration.AutomationError("电子税务局流程仅支持 Windows 桌面")
    try:
        processes = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="mbcs", errors="replace",
            timeout=5, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise tax_declaration.AutomationError("无法确认税务客户端是否已关闭，未启动流程") from exc
    client_names = {tax_declaration.PROCESS_IMAGE.casefold(), tax_declaration.DEFAULT_APP_PATH.name.casefold()}
    if any(row and row[0].casefold() in client_names for row in csv.reader(processes.stdout.splitlines())):
        raise tax_declaration.AutomationError("税务客户端已在运行，请保存当前工作并关闭后再启动流程")
