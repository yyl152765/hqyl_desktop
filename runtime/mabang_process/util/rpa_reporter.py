import json
import math
import os
import platform
import socket
import subprocess
import sys
import traceback
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from numbers import Number
from pathlib import Path

import yaml

try:
    import psycopg2
    from psycopg2.extras import Json
except Exception:
    psycopg2 = None
    Json = None


PROJECT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_DIR / "config" / "config.yaml"
SCHEMA = "rpa_metrics"


def _env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _debugger_attached():
    try:
        return sys.gettrace() is not None
    except Exception:
        return False


def _now():
    return datetime.now(timezone.utc)


def _safe_text(value, limit=None):
    if value is None:
        return ""
    text = str(value)
    if limit and len(text) > limit:
        return text[:limit]
    return text


def _load_yaml_config():
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as fp:
        return yaml.safe_load(fp) or {}


def _get_db_config():
    cfg = _load_yaml_config()
    database = (cfg.get("database") or {}).get("rpa_metrics") or {}
    return {
        "host": os.getenv("RPA_METRICS_DB_HOST") or database.get("host"),
        "port": int(os.getenv("RPA_METRICS_DB_PORT") or database.get("port") or 5432),
        "dbname": os.getenv("RPA_METRICS_DB_NAME") or database.get("dbname"),
        "user": os.getenv("RPA_METRICS_DB_USER") or database.get("user"),
        "password": os.getenv("RPA_METRICS_DB_PASSWORD") or database.get("password"),
    }


def _connect():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed")
    db = _get_db_config()
    missing = [key for key, value in db.items() if key != "port" and not value]
    if missing:
        raise RuntimeError(f"missing rpa_metrics database config: {', '.join(missing)}")
    return psycopg2.connect(connect_timeout=10, **db)


def _get_ip_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        return ""


def _get_mac_address():
    try:
        value = uuid.getnode()
        return ":".join(f"{(value >> offset) & 0xff:02x}" for offset in range(40, -1, -8))
    except Exception:
        return ""


def _git_value(args):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _runtime_snapshot():
    host_name = socket.gethostname()
    return {
        "device_code": os.getenv("RPA_DEVICE_CODE") or host_name,
        "device_name": os.getenv("RPA_DEVICE_NAME") or host_name,
        "host_name": host_name,
        "ip_address": _get_ip_address(),
        "mac_address": _get_mac_address(),
        "os_type": platform.system() or "Windows",
        "os_version": platform.platform(),
        "python_version": platform.python_version(),
        "work_directory": str(PROJECT_DIR),
        "git_branch": _git_value(["rev-parse", "--abbrev-ref", "HEAD"]),
        "git_commit": _git_value(["rev-parse", "--short", "HEAD"]),
    }


def _run_mode():
    return (os.getenv("RPA_RUN_MODE") or "prod").strip().lower()


def _reporter_enabled():
    if not _env_bool("RPA_REPORTER_ENABLED", False):
        return False
    if _debugger_attached() and not _env_bool("RPA_ALLOW_DEBUGGER_WRITE", False):
        return False
    mode = _run_mode()
    if mode == "debug" and not _env_bool("RPA_WRITE_DEBUG_RUNS", False):
        return False
    if mode == "dry_run" and not _env_bool("RPA_WRITE_DRY_RUNS", False):
        return False
    return True


def _is_test_mode():
    return _run_mode() in ("debug", "dry_run") or _env_bool("RPA_IS_TEST", False)


def _json_value(value):
    if value is None:
        value = {}
    if Json is None:
        return json.dumps(value, ensure_ascii=False)
    return Json(value)


def _normalize_metric_values(metric_value=None, metric_value_text="", metric_value_json=None):
    if metric_value is None:
        return metric_value, metric_value_text, metric_value_json
    if metric_value_text or metric_value_json is not None:
        return metric_value, metric_value_text, metric_value_json
    if isinstance(metric_value, bool):
        return None, str(metric_value), None
    if isinstance(metric_value, Number):
        if isinstance(metric_value, float) and not math.isfinite(metric_value):
            return None, str(metric_value), None
        return metric_value, "", None
    if isinstance(metric_value, (dict, list, tuple)):
        return None, "", metric_value
    return None, str(metric_value), None


@dataclass
class RpaRun:
    run_id: str
    process_code: str
    process_name: str = ""
    enabled: bool = True
    started_at: datetime = None
    finished: bool = False

    def finish(
        self,
        processed_count=0,
        success_count=0,
        failed_count=0,
        output_file_count=0,
        time_saved_minutes=None,
        status=None,
        log_file_path="",
        output_directory="",
    ):
        finish_run(
            self.run_id,
            processed_count=processed_count,
            success_count=success_count,
            failed_count=failed_count,
            output_file_count=output_file_count,
            time_saved_minutes=time_saved_minutes,
            status=status,
            log_file_path=log_file_path,
            output_directory=output_directory,
        )
        self.finished = True

    def fail(self, exc, log_file_path="", output_directory=""):
        fail_run(self.run_id, exc, log_file_path=log_file_path, output_directory=output_directory)
        self.finished = True

    def skip(self, reason=""):
        skip_run(self.run_id, reason)
        self.finished = True

    def heartbeat(self):
        heartbeat_run(self.run_id)

    def add_metric(self, metric_key, metric_name, metric_value=None, metric_unit="", metric_value_text="", metric_value_json=None):
        metric_value, metric_value_text, metric_value_json = _normalize_metric_values(
            metric_value,
            metric_value_text,
            metric_value_json,
        )
        add_metric(
            self.run_id,
            metric_key,
            metric_name,
            metric_value=metric_value,
            metric_unit=metric_unit,
            metric_value_text=metric_value_text,
            metric_value_json=metric_value_json,
        )


class report_run:
    def __init__(
        self,
        process_code,
        process_name="",
        country="",
        platform_name="",
        category="",
        script_path="",
        processed_count_unit="",
        estimated_minutes_saved_per_item=None,
        trigger_type="",
        operator_name="",
        biz_date=None,
        biz_start_date=None,
        biz_end_date=None,
        run_context=None,
    ):
        self.kwargs = {
            "process_code": process_code,
            "process_name": process_name,
            "country": country,
            "platform_name": platform_name,
            "category": category,
            "script_path": script_path,
            "processed_count_unit": processed_count_unit,
            "estimated_minutes_saved_per_item": estimated_minutes_saved_per_item,
            "trigger_type": trigger_type,
            "operator_name": operator_name,
            "biz_date": biz_date,
            "biz_start_date": biz_start_date,
            "biz_end_date": biz_end_date,
            "run_context": run_context or {},
        }
        self.run = None

    def __enter__(self):
        self.run = start_run(**self.kwargs)
        return self.run

    def __exit__(self, exc_type, exc, tb):
        if exc is not None and self.run and not self.run.finished:
            self.run.fail(exc)
        elif self.run and not self.run.finished:
            self.run.finish()
        return False


def _execute(callback):
    if not _reporter_enabled():
        return None
    conn = None
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                result = callback(cur)
        return result
    except Exception as exc:
        print(f"[rpa_reporter] 上报失败: {exc}", file=sys.stderr)
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _ensure_process(cur, process_code, process_name="", country="", platform_name="", category="", script_path="", processed_count_unit="", estimated_minutes_saved_per_item=None):
    estimate = estimated_minutes_saved_per_item
    if estimate is None:
        estimate = 0
    cur.execute(
        f"""
        INSERT INTO {SCHEMA}.rpa_process (
            process_code, process_name, country, platform, category, script_path,
            processed_count_unit, estimated_minutes_saved_per_item, remark
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'auto_created_by_reporter')
        ON CONFLICT (process_code) DO UPDATE SET
            process_name = COALESCE(NULLIF(EXCLUDED.process_name, ''), {SCHEMA}.rpa_process.process_name),
            country = COALESCE(NULLIF(EXCLUDED.country, ''), {SCHEMA}.rpa_process.country),
            platform = COALESCE(NULLIF(EXCLUDED.platform, ''), {SCHEMA}.rpa_process.platform),
            category = COALESCE(NULLIF(EXCLUDED.category, ''), {SCHEMA}.rpa_process.category),
            script_path = COALESCE(NULLIF(EXCLUDED.script_path, ''), {SCHEMA}.rpa_process.script_path),
            processed_count_unit = COALESCE(NULLIF(EXCLUDED.processed_count_unit, ''), {SCHEMA}.rpa_process.processed_count_unit)
        """,
        (
            process_code,
            process_name or process_code,
            country,
            platform_name,
            category,
            script_path,
            processed_count_unit,
            estimate,
        ),
    )


def _fetch_process(cur, process_code):
    cur.execute(
        f"""
        SELECT process_name, project_name, script_path, estimated_minutes_saved_per_item
        FROM {SCHEMA}.rpa_process
        WHERE process_code = %s
        """,
        (process_code,),
    )
    row = cur.fetchone()
    if not row:
        return {
            "process_name": process_code,
            "project_name": "mabang_process",
            "script_path": "",
            "estimated_minutes_saved_per_item": 0,
        }
    return {
        "process_name": row[0],
        "project_name": row[1],
        "script_path": row[2] or "",
        "estimated_minutes_saved_per_item": float(row[3] or 0),
    }


def _ensure_device(cur, runtime):
    cur.execute(
        f"""
        INSERT INTO {SCHEMA}.rpa_runner_device (
            device_code, device_name, host_name, ip_address, mac_address,
            os_type, os_version, python_version, work_directory,
            online_status, last_seen_at, last_heartbeat_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'online', NOW(), NOW())
        ON CONFLICT (device_code) DO UPDATE SET
            device_name = EXCLUDED.device_name,
            host_name = EXCLUDED.host_name,
            ip_address = EXCLUDED.ip_address,
            mac_address = EXCLUDED.mac_address,
            os_type = EXCLUDED.os_type,
            os_version = EXCLUDED.os_version,
            python_version = EXCLUDED.python_version,
            work_directory = EXCLUDED.work_directory,
            online_status = 'online',
            last_seen_at = NOW(),
            last_heartbeat_at = NOW()
        """,
        (
            runtime["device_code"],
            runtime["device_name"],
            runtime["host_name"],
            runtime["ip_address"],
            runtime["mac_address"],
            runtime["os_type"],
            runtime["os_version"],
            runtime["python_version"],
            runtime["work_directory"],
        ),
    )


def start_run(
    process_code,
    process_name="",
    country="",
    platform_name="",
    category="",
    script_path="",
    processed_count_unit="",
    estimated_minutes_saved_per_item=None,
    trigger_type="",
    operator_name="",
    biz_date=None,
    biz_start_date=None,
    biz_end_date=None,
    run_context=None,
):
    started_at = _now()
    run_id = f"{process_code}_{started_at.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
    enabled = _reporter_enabled()
    runtime = _runtime_snapshot()

    def callback(cur):
        _ensure_process(
            cur,
            process_code,
            process_name=process_name,
            country=country,
            platform_name=platform_name,
            category=category,
            script_path=script_path,
            processed_count_unit=processed_count_unit,
            estimated_minutes_saved_per_item=estimated_minutes_saved_per_item,
        )
        process = _fetch_process(cur, process_code)
        _ensure_device(cur, runtime)
        cur.execute(
            f"""
            INSERT INTO {SCHEMA}.rpa_run_log (
                run_id, process_code, device_code, process_name, project_name,
                status, run_mode, is_test, started_at, heartbeat_at,
                biz_date, biz_start_date, biz_end_date, trigger_type,
                runner_host, runner_ip, operator_name, script_path,
                git_branch, git_commit, python_version, run_context
            )
            VALUES (
                %s, %s, %s, %s, %s,
                'running', %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            """,
            (
                run_id,
                process_code,
                runtime["device_code"],
                process_name or process["process_name"],
                process["project_name"],
                _run_mode(),
                _is_test_mode(),
                started_at,
                started_at,
                _date_value(biz_date),
                _date_value(biz_start_date),
                _date_value(biz_end_date),
                trigger_type,
                runtime["host_name"],
                runtime["ip_address"],
                operator_name,
                script_path or process["script_path"],
                runtime["git_branch"],
                runtime["git_commit"],
                runtime["python_version"],
                _json_value(run_context or {}),
            ),
        )
        return process_name or process["process_name"]

    resolved_name = _execute(callback) if enabled else process_name or process_code
    return RpaRun(
        run_id=run_id,
        process_code=process_code,
        process_name=resolved_name or process_name or process_code,
        enabled=enabled,
        started_at=started_at,
    )


def _date_value(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def finish_run(
    run_id,
    processed_count=0,
    success_count=0,
    failed_count=0,
    output_file_count=0,
    time_saved_minutes=None,
    status=None,
    log_file_path="",
    output_directory="",
):
    ended_at = _now()

    def callback(cur):
        cur.execute(
            f"""
            SELECT started_at, process_code
            FROM {SCHEMA}.rpa_run_log
            WHERE run_id = %s
            """,
            (run_id,),
        )
        row = cur.fetchone()
        if not row:
            return
        started_at, process_code = row
        duration = (ended_at - started_at).total_seconds()
        final_status = status or ("partial_success" if int(failed_count or 0) > 0 else "success")
        saved = time_saved_minutes
        if saved is None:
            cur.execute(
                f"""
                SELECT estimated_minutes_saved_per_item
                FROM {SCHEMA}.rpa_process
                WHERE process_code = %s
                """,
                (process_code,),
            )
            estimate_row = cur.fetchone()
            estimate = float((estimate_row or [0])[0] or 0)
            saved = float(processed_count or 0) * estimate
        cur.execute(
            f"""
            UPDATE {SCHEMA}.rpa_run_log
            SET status = %s,
                ended_at = %s,
                duration_seconds = %s,
                processed_count = %s,
                success_count = %s,
                failed_count = %s,
                output_file_count = %s,
                time_saved_minutes = %s,
                log_file_path = COALESCE(NULLIF(%s, ''), log_file_path),
                output_directory = COALESCE(NULLIF(%s, ''), output_directory)
            WHERE run_id = %s
            """,
            (
                final_status,
                ended_at,
                duration,
                int(processed_count or 0),
                int(success_count or 0),
                int(failed_count or 0),
                int(output_file_count or 0),
                saved,
                log_file_path,
                output_directory,
                run_id,
            ),
        )

    _execute(callback)


def fail_run(run_id, exc, log_file_path="", output_directory=""):
    ended_at = _now()
    error_type = type(exc).__name__
    error_message = _safe_text(exc, 2000)
    error_traceback = traceback.format_exc()

    def callback(cur):
        cur.execute(
            f"SELECT started_at FROM {SCHEMA}.rpa_run_log WHERE run_id = %s",
            (run_id,),
        )
        row = cur.fetchone()
        duration = (ended_at - row[0]).total_seconds() if row else 0
        cur.execute(
            f"""
            UPDATE {SCHEMA}.rpa_run_log
            SET status = 'failed',
                ended_at = %s,
                duration_seconds = %s,
                error_type = %s,
                error_message = %s,
                error_traceback = %s,
                log_file_path = COALESCE(NULLIF(%s, ''), log_file_path),
                output_directory = COALESCE(NULLIF(%s, ''), output_directory)
            WHERE run_id = %s
            """,
            (
                ended_at,
                duration,
                error_type,
                error_message,
                error_traceback,
                log_file_path,
                output_directory,
                run_id,
            ),
        )

    _execute(callback)


def skip_run(run_id, reason=""):
    ended_at = _now()

    def callback(cur):
        cur.execute(
            f"SELECT started_at FROM {SCHEMA}.rpa_run_log WHERE run_id = %s",
            (run_id,),
        )
        row = cur.fetchone()
        duration = (ended_at - row[0]).total_seconds() if row else 0
        cur.execute(
            f"""
            UPDATE {SCHEMA}.rpa_run_log
            SET status = 'skipped',
                ended_at = %s,
                duration_seconds = %s,
                error_message = %s
            WHERE run_id = %s
            """,
            (ended_at, duration, _safe_text(reason, 2000), run_id),
        )

    _execute(callback)


def heartbeat_run(run_id):
    runtime = _runtime_snapshot()

    def callback(cur):
        cur.execute(
            f"""
            UPDATE {SCHEMA}.rpa_run_log
            SET heartbeat_at = NOW()
            WHERE run_id = %s
            """,
            (run_id,),
        )
        cur.execute(
            f"""
            UPDATE {SCHEMA}.rpa_runner_device
            SET online_status = 'online',
                last_seen_at = NOW(),
                last_heartbeat_at = NOW()
            WHERE device_code = %s
            """,
            (runtime["device_code"],),
        )

    _execute(callback)


def add_metric(
    run_id,
    metric_key,
    metric_name,
    metric_value=None,
    metric_unit="",
    metric_value_text="",
    metric_value_json=None,
):
    metric_value, metric_value_text, metric_value_json = _normalize_metric_values(
        metric_value,
        metric_value_text,
        metric_value_json,
    )

    def callback(cur):
        cur.execute(
            f"SELECT process_code FROM {SCHEMA}.rpa_run_log WHERE run_id = %s",
            (run_id,),
        )
        row = cur.fetchone()
        if not row:
            return
        cur.execute(
            f"""
            INSERT INTO {SCHEMA}.rpa_run_metric (
                run_id, process_code, metric_key, metric_name,
                metric_value, metric_value_text, metric_value_json, metric_unit
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                row[0],
                metric_key,
                metric_name,
                metric_value,
                metric_value_text,
                _json_value(metric_value_json) if metric_value_json is not None else None,
                metric_unit,
            ),
        )

    _execute(callback)
