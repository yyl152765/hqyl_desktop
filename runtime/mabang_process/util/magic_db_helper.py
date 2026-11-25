import os
import sys
import json
import yaml
import requests
from datetime import datetime, date, timezone
from email.utils import parsedate_to_datetime
import math
import time
import pandas as pd
import numpy as np
try:
    from psycopg2.extras import Json as PsycopgJson
except ImportError:
    PsycopgJson = None

process_config_path = os.path.dirname(__file__)
config_path = os.path.dirname(os.path.dirname(__file__))
sys.path.append(config_path)
_TABLE_COLUMN_CAST_CACHE = {}
_DEFAULT_RETRY_DELAYS_SECONDS = (5, 10, 20, 30)
_TRANSIENT_HTTP_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_TRANSIENT_REQUEST_EXCEPTIONS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
_PUBLIC_GROUP_SHOP_API_PATH = "/api/erp/groupShopNameByLeader"


class MagicApiTransientResponseError(RuntimeError):
    """A retryable API response whose write outcome may be ambiguous."""


class MagicApiConnection:
    def close(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def make_json_safe(obj):
    """
    把 datetime/date/NaN 等转换成 JSON 可传输格式
    """
    if PsycopgJson is not None and isinstance(obj, PsycopgJson):
        return make_json_safe(obj.adapted)

    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}

    if isinstance(obj, list):
        return [make_json_safe(v) for v in obj]

    if isinstance(obj, tuple):
        return [make_json_safe(v) for v in obj]

    if isinstance(obj, (datetime, date)):
        return obj.strftime("%Y-%m-%d %H:%M:%S") if isinstance(obj, datetime) else obj.strftime("%Y-%m-%d")

    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None

    if pd.isna(obj):
        return None

    if isinstance(obj, np.integer):
        return int(obj)

    if isinstance(obj, np.floating):
        return float(obj)

    return obj
def get_magic_api_config():
    with open(rf'{config_path}/config/config.yaml', 'r', encoding='utf-8') as config:
        cfg = yaml.safe_load(config)
    return cfg["magic_api"]


def _configured_retry_delays(cfg):
    raw_delays = cfg.get("retry_delays_seconds", _DEFAULT_RETRY_DELAYS_SECONDS)
    if not isinstance(raw_delays, (list, tuple)) or not raw_delays:
        raise ValueError("magic_api.retry_delays_seconds 必须是非空数字列表")

    delays = []
    for raw_delay in raw_delays:
        if isinstance(raw_delay, bool):
            raise ValueError("magic_api.retry_delays_seconds 不能包含布尔值")
        try:
            delay = float(raw_delay)
        except (TypeError, ValueError) as exc:
            raise ValueError("magic_api.retry_delays_seconds 必须是非负数字列表") from exc
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("magic_api.retry_delays_seconds 必须是非负有限数字列表")
        delays.append(int(delay) if delay.is_integer() else delay)
    return tuple(delays)


def _retry_after_seconds(response):
    headers = getattr(response, "headers", None)
    if not hasattr(headers, "get"):
        return None
    raw_value = headers.get("Retry-After")
    if raw_value is None:
        return None

    value = str(raw_value).strip()
    if not value:
        return None
    if value.isdecimal():
        try:
            return int(value)
        except ValueError:
            return None

    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at is None:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def _retry_delay_seconds(retry_delays, attempt, response=None):
    if response is not None:
        retry_after = _retry_after_seconds(response)
        if retry_after is not None:
            return retry_after
    return retry_delays[min(attempt, len(retry_delays) - 1)]


def _sleep_before_retry(retry_delays, attempt, response=None):
    time.sleep(_retry_delay_seconds(retry_delays, attempt, response))


def _is_ambiguous_write_failure(exc):
    if isinstance(exc, (MagicApiTransientResponseError,) + _TRANSIENT_REQUEST_EXCEPTIONS):
        return True
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        return getattr(response, "status_code", None) in _TRANSIENT_HTTP_STATUS_CODES
    return False


def _request_magic_api(
    api_path,
    payload=None,
    timeout=120,
    retries=1,
    *,
    base_url_config_key="base_url",
    include_authorization=True,
):
    """
    统一请求 magic-api
    兼容 magic-api 外层 code=1，内部业务 code=200 的返回结构
    """
    cfg = get_magic_api_config()

    base_url = cfg[base_url_config_key].rstrip("/")
    token = cfg.get("token", "")

    url = f"{base_url}{api_path}"

    headers = {
        "Content-Type": "application/json"
    }

    if include_authorization and token:
        headers["Authorization"] = token

    retry_count = max(1, int(retries or 1))
    retry_delays = _configured_retry_delays(cfg)
    last_error = None
    for attempt in range(retry_count):
        try:
            resp = requests.post(
                url,
                json=make_json_safe(payload or {}),
                headers=headers,
                timeout=timeout
            )
        except _TRANSIENT_REQUEST_EXCEPTIONS as exc:
            last_error = exc
            if attempt >= retry_count - 1:
                raise
            _sleep_before_retry(retry_delays, attempt)
            continue
        except requests.RequestException:
            raise

        status_code = resp.status_code
        if status_code in _TRANSIENT_HTTP_STATUS_CODES:
            if attempt >= retry_count - 1:
                resp.raise_for_status()
            _sleep_before_retry(retry_delays, attempt, resp)
            continue
        resp.raise_for_status()

        result = resp.json()
        if not isinstance(result, dict):
            raise RuntimeError("magic-api响应结构错误: 外层响应必须是对象")

        # magic-api 的 code=-1 表示服务端内部异常，属于可重试故障。
        # 旧实现只重试网络异常，遇到这种短暂抖动会立即让业务流程整组跳过。
        outer_code = result.get("code")
        if outer_code not in (1, 200):
            error_type = MagicApiTransientResponseError if outer_code == -1 else RuntimeError
            last_error = error_type(
                "magic-api外层请求失败: code=-1"
                if outer_code == -1
                else "magic-api外层请求失败: 非成功状态"
            )
            if outer_code == -1 and attempt < retry_count - 1:
                _sleep_before_retry(retry_delays, attempt, resp)
                continue
            raise last_error

        outer_data = result.get("data")

        # 你接口里面 return 的结构：{code: 200, msg: ..., data: ...}
        if isinstance(outer_data, dict) and "code" in outer_data:
            if outer_data.get("code") != 200:
                raise RuntimeError("magic-api业务执行失败: 非成功状态")

            return outer_data.get("data")

        # 兼容直接 return db.erp.select(...) 的情况
        return outer_data

    raise last_error or RuntimeError(f"magic-api请求失败: {api_path}")


def request_magic_api(api_path, payload=None, timeout=120, retries=1):
    """Call the authenticated/general Magic API; retries are opt-in."""

    return _request_magic_api(
        api_path,
        payload,
        timeout,
        retries,
        base_url_config_key="base_url",
        include_authorization=True,
    )


def request_magic_api_public_group_shop(
    api_path,
    payload=None,
    timeout=120,
    retries=3,
):
    """Call only the public, read-only ERP group-shop endpoint."""

    if api_path != _PUBLIC_GROUP_SHOP_API_PATH:
        raise ValueError("public Magic API 仅允许查询 ERP 组长店铺接口")
    return _request_magic_api(
        api_path,
        payload,
        timeout,
        retries,
        base_url_config_key="group_shop_public_base_url",
        include_authorization=False,
    )


def get_pg_connection(database=None):
    """
    兼容旧代码。
    原来需要 conn，现在改成 magic-api 后不再需要真实数据库连接。
    """
    return MagicApiConnection()


def query_list(sql, params=None):
    return request_magic_api("/api/db/queryList", {
        "sql": sql,
        "params": params or {}
    }, retries=3)


def query_one(sql, params=None):
    data = query_list(sql, params)
    return data[0] if data else None


def execute(sql, params=None):
    return request_magic_api("/api/db/execute", {
        "sql": sql,
        "params": params or {}
    }, retries=1)


def _quote_identifier(identifier):
    return f'"{str(identifier).replace(chr(34), chr(34) + chr(34))}"'


def _quote_table_name(table_name):
    return ".".join(_quote_identifier(part) for part in str(table_name).split("."))


def _table_column_casts(table_name):
    cache_key = str(table_name)
    if cache_key in _TABLE_COLUMN_CAST_CACHE:
        return _TABLE_COLUMN_CAST_CACHE[cache_key]

    parts = cache_key.split(".")
    if len(parts) == 2:
        schema_name, plain_table_name = parts
    else:
        schema_name, plain_table_name = "public", cache_key

    rows = query_list("""
        select column_name, udt_name
        from information_schema.columns
        where table_schema = #{schema_name}
          and table_name = #{table_name}
    """, {
        "schema_name": schema_name,
        "table_name": plain_table_name
    })

    casts = {}
    for row in rows:
        udt_name = row.get("udt_name")
        if udt_name in ("json", "jsonb"):
            casts[row["column_name"]] = udt_name
        elif udt_name in ("timestamp", "timestamptz"):
            casts[row["column_name"]] = "timestamp"
        elif udt_name == "date":
            casts[row["column_name"]] = "date"
        elif isinstance(udt_name, str) and udt_name.startswith("_"):
            casts[row["column_name"]] = f"{udt_name[1:]}[]"

    _TABLE_COLUMN_CAST_CACHE[cache_key] = casts
    return casts


def _column_casts(table_name, data_list, keys):
    casts = {key: value for key, value in _table_column_casts(table_name).items() if key in keys}

    for key in keys:
        if key in casts:
            continue
        for row in data_list:
            value = row.get(key)
            if PsycopgJson is not None and isinstance(value, PsycopgJson):
                casts[key] = "jsonb"
                break
            if isinstance(value, (dict, list)):
                casts[key] = "jsonb"
                break
            if isinstance(value, datetime):
                casts[key] = "timestamp"
                break
            if isinstance(value, date):
                casts[key] = "date"
                break
    return casts


def _placeholder(column_name, casts=None):
    placeholder = "#{" + str(column_name) + "}"
    cast_type = (casts or {}).get(column_name)
    if cast_type:
        return f"{placeholder}::{cast_type}"
    return placeholder


def _placeholder_for_param(param_name, cast_type=None):
    placeholder = "#{" + str(param_name) + "}"
    if cast_type:
        return f"{placeholder}::{cast_type}"
    return placeholder


def _prepare_sql_params(row, casts):
    params = {}
    for key, value in row.items():
        if casts.get(key) == "jsonb":
            if value is None:
                params[key] = None
                continue
            if PsycopgJson is not None and isinstance(value, PsycopgJson):
                value = value.adapted
            if isinstance(value, str):
                try:
                    json.loads(value)
                except (TypeError, ValueError):
                    params[key] = json.dumps(value, ensure_ascii=False)
                else:
                    params[key] = value
            else:
                params[key] = json.dumps(make_json_safe(value), ensure_ascii=False)
        elif str(casts.get(key, "")).endswith("[]"):
            if value is None:
                params[key] = None
            elif isinstance(value, (list, tuple)):
                params[key] = "{" + ",".join(str(item) for item in value if item is not None) + "}"
            else:
                params[key] = value
        else:
            params[key] = value
    return params


def _iter_chunks(data_list, chunk_size=10):
    for index in range(0, len(data_list), chunk_size):
        yield data_list[index:index + chunk_size]


def _build_batch_values_sql(chunk, keys, casts):
    value_groups = []
    params = {}

    for row_index, row in enumerate(chunk):
        prepared_row = _prepare_sql_params(row, casts)
        placeholders = []
        for key in keys:
            param_name = f"{key}_{row_index}"
            params[param_name] = prepared_row.get(key)
            placeholders.append(_placeholder_for_param(param_name, casts.get(key)))
        value_groups.append("(" + ", ".join(placeholders) + ")")

    return ", ".join(value_groups), params


def _execute_upsert_chunk(table_name, chunk, keys, fields, conflict_fields, conflict_action, casts):
    values, params = _build_batch_values_sql(chunk, keys, casts)
    sql = (
        f"insert into {_quote_table_name(table_name)} ({fields}) "
        f"values {values} "
        f"on conflict ({conflict_fields}) {conflict_action}"
    )
    try:
        execute(sql, params)
    except Exception as exc:
        if len(chunk) <= 1:
            raise Exception(
                f"upsert_batch_to_pg failed: table={table_name}, "
                f"rows={len(chunk)}, sql={sql[:500]}, params={params}, "
                f"cause={exc}"
            ) from exc
        middle = len(chunk) // 2
        _execute_upsert_chunk(table_name, chunk[:middle], keys, fields, conflict_fields, conflict_action, casts)
        _execute_upsert_chunk(table_name, chunk[middle:], keys, fields, conflict_fields, conflict_action, casts)


def _execute_insert_chunk(table_name, chunk, keys, fields, casts):
    values, params = _build_batch_values_sql(chunk, keys, casts)
    sql = f"insert into {_quote_table_name(table_name)} ({fields}) values {values}"
    try:
        execute(sql, params)
    except Exception as exc:
        # A transport/transient response can arrive after the server committed.
        # Do not bisect and replay a non-idempotent INSERT in that case.
        if _is_ambiguous_write_failure(exc):
            raise
        if len(chunk) <= 1:
            raise Exception(
                f"insert_batch_to_pg failed: table={table_name}, "
                f"rows={len(chunk)}, sql={sql[:500]}"
            ) from exc
        middle = len(chunk) // 2
        _execute_insert_chunk(table_name, chunk[:middle], keys, fields, casts)
        _execute_insert_chunk(table_name, chunk[middle:], keys, fields, casts)


def upsert_batch_to_pg(conn, table_name, data_list, conflict_keys):
    """
    批量 UPSERT
    conn 参数保留，为了兼容旧代码。
    """
    if not data_list:
        return 0

    if not conflict_keys:
        raise ValueError("conflict_keys cannot be empty")

    keys = list(data_list[0].keys())
    update_keys = [key for key in keys if key not in conflict_keys]
    casts = _column_casts(table_name, data_list, keys)

    fields = ", ".join(_quote_identifier(key) for key in keys)
    conflict_fields = ", ".join(_quote_identifier(key) for key in conflict_keys)

    if update_keys:
        update_sql = ", ".join(
            f"{_quote_identifier(key)} = excluded.{_quote_identifier(key)}"
            for key in update_keys
        )
        conflict_action = f"do update set {update_sql}"
    else:
        conflict_action = "do nothing"

    for chunk in _iter_chunks(data_list):
        _execute_upsert_chunk(table_name, chunk, keys, fields, conflict_fields, conflict_action, casts)

    return len(data_list)


def truncate_table(conn, table_name):
    """
    清空表
    conn 参数保留，为了兼容旧代码。
    """
    return request_magic_api("/api/db/truncateTable", {
        "table_name": table_name
    }, retries=1)


def insert_batch_to_pg(conn, table_name, data_list):
    """
    批量插入
    conn 参数保留，为了兼容旧代码。
    """
    if not data_list:
        return 0

    keys = list(data_list[0].keys())
    casts = _column_casts(table_name, data_list, keys)
    fields = ", ".join(_quote_identifier(key) for key in keys)
    for chunk in _iter_chunks(data_list):
        _execute_insert_chunk(table_name, chunk, keys, fields, casts)

    return len(data_list)


def fetch_column_from_db(conn, table_name, column_name, where_clause=None, params=None):
    """
    通用查询某一列。
    注意：where_clause 里面用 #{参数名}
    """
    sql = f"select {column_name} from {table_name}"

    if where_clause:
        sql += f" where {where_clause}"

    rows = query_list(sql, params or {})

    return [row.get(column_name) for row in rows]


def get_process_code_byname(name, conn=None):
    row = query_one("""
        select process_code
        from dingtalk_process_template
        where name = #{name}
        limit 1
    """, {
        "name": name
    })

    return row["process_code"] if row else None


def get_tiktok_shop_code(name, conn=None):
    row = query_one("""
        select shop_code
        from tiktok_shop
        where shop_name = #{shop_name}
        limit 1
    """, {
        "shop_name": name
    })

    return row["shop_code"] if row else None
