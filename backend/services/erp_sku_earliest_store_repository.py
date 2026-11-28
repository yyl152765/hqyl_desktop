# ruff: noqa: S110
"""Read-only PostgreSQL access for "SKU -> store with the earliest platform creation time".

Design notes (see docs/ERP_SKU创建时间第一店铺开发说明.md):

* Only the three base tables are read; no wide table, no writes, no DDL.
* Every statement runs in a read-only autocommit session with explicit timeouts.
* TLS is decided by configuration (``ERP_SKU_DB_SSLMODE``); a failed TLS
  handshake is never silently downgraded.
* Errors are reduced to a safe code, a business message and the SQLSTATE.
  Raw driver text (which can echo hosts, users or SQL) is never propagated.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

QUERY_CONTRACT = "erp-sku-earliest-store-v2"
APPLICATION_NAME = "hqyl_shopee_sku_earliest_store"
ENV_PREFIX = "ERP_SKU_DB_"
ALLOWED_SSL_MODES = frozenset({"require", "verify-ca", "verify-full", "disable"})

BUILTIN_AUTHORIZED_SHOPS = "*"

CORE_SQL = """
WITH requested AS (
    SELECT DISTINCT unnest(%(skus)s::text[]) AS input_sku
), raw_candidates AS (
    SELECT q.input_sku, m.model_sku AS matched_sku,
           m.shop_id, m.item_id::text AS item_id, m.model_id,
           b.create_time, b.item_status, b.has_model,
           b.archived AS item_archived, m.archived AS model_archived,
           b.clean_time AS base_clean_time, m.clean_time AS model_clean_time,
           (b.item_id IS NOT NULL) AS base_found,
           'model_sku'::text AS match_source, 0 AS source_priority
    FROM requested q
    JOIN biz_shopee.shopee_model_list m ON m.model_sku = q.input_sku
    LEFT JOIN biz_shopee.shopee_item_base_info b
      ON b.shop_id = m.shop_id AND b.item_id = m.item_id::text
    UNION ALL
    SELECT q.input_sku, b.item_sku,
           b.shop_id, b.item_id, 0::bigint,
           b.create_time, b.item_status, b.has_model,
           b.archived, NULL::boolean,
           b.clean_time, NULL::timestamp, TRUE,
           CASE WHEN b.has_model IS FALSE THEN 'item_sku_no_model'
                ELSE 'unknown_model_flag' END, 1
    FROM requested q
    JOIN biz_shopee.shopee_item_base_info b ON b.item_sku = q.input_sku
    WHERE b.has_model IS NOT TRUE
), items AS (
    SELECT DISTINCT ON (input_sku, shop_id, item_id) *
    FROM raw_candidates
    ORDER BY input_sku, shop_id, item_id, source_priority, model_id
), enriched AS (
    SELECT i.*, s.region,
           COALESCE(NULLIF(btrim(s.custom_name), ''),
                    NULLIF(btrim(s.shop_name), '')) AS shop_name,
           CASE WHEN i.create_time IS NOT NULL AND isfinite(i.create_time)
                THEN i.create_time END AS rank_time,
           (NOT i.base_found OR i.create_time IS NULL
            OR NOT isfinite(i.create_time)
            OR i.match_source = 'unknown_model_flag'
            OR s.shop_id IS NULL
            OR COALESCE(NULLIF(btrim(s.custom_name), ''),
                        NULLIF(btrim(s.shop_name), '')) IS NULL) AS data_issue
    FROM items i
    LEFT JOIN biz_shopee.api_shop_info s ON s.shop_id = i.shop_id
    WHERE (%(all_shops)s OR i.shop_id::text = ANY(%(shop_ids)s::text[]))
      AND (NOT %(filter_regions)s OR upper(btrim(s.region)) = ANY(%(regions)s::text[]))
), annotated AS (
    SELECT e.*,
           count(*) OVER (PARTITION BY input_sku) AS matched_item_count,
           count(*) FILTER (WHERE data_issue)
             OVER (PARTITION BY input_sku) AS data_issue_count,
           row_number() OVER (
             PARTITION BY input_sku, shop_id
             ORDER BY data_issue ASC, rank_time ASC NULLS LAST, item_id, model_id
           ) AS shop_rn
    FROM enriched e
), shop_best AS (
    SELECT * FROM annotated WHERE shop_rn = 1
), ranked AS (
    SELECT sb.*,
           count(*) FILTER (WHERE NOT data_issue) OVER (PARTITION BY input_sku) AS matched_shop_count,
           count(*) FILTER (WHERE NOT data_issue) OVER (PARTITION BY input_sku, rank_time) AS tied_shop_count,
           row_number() OVER (
             PARTITION BY input_sku
             ORDER BY data_issue ASC, rank_time ASC NULLS LAST, shop_id, item_id, model_id
           ) AS overall_rn
    FROM shop_best sb
)
SELECT q.input_sku,
       CASE WHEN r.input_sku IS NULL THEN 'NOT_FOUND'
            WHEN r.data_issue THEN 'INCOMPLETE_DATA'
            ELSE 'MATCHED' END AS result_status,
       r.shop_id::text AS candidate_shop_id, r.shop_name AS candidate_shop_name,
       r.region, r.item_id, r.model_id::text AS model_id, r.matched_sku,
       r.rank_time AS candidate_create_time, r.item_status,
       r.item_archived, r.model_archived, r.match_source,
       COALESCE(r.matched_item_count, 0) AS matched_item_count,
       COALESCE(r.matched_shop_count, 0) AS matched_shop_count,
       COALESCE(r.data_issue_count, 0) AS data_issue_count,
       CASE WHEN NOT COALESCE(r.data_issue, TRUE) AND r.rank_time IS NOT NULL THEN r.tied_shop_count ELSE 0 END AS tied_shop_count,
       r.base_clean_time, r.model_clean_time,
       ties.tied_shop_ids, ties.tied_shop_names
FROM requested q
LEFT JOIN ranked r ON r.input_sku = q.input_sku AND r.overall_rn = 1
LEFT JOIN LATERAL (
    SELECT array_agg(t.shop_id::text ORDER BY t.shop_id, t.item_id) AS tied_shop_ids,
           array_agg(COALESCE(t.shop_name, '') ORDER BY t.shop_id, t.item_id) AS tied_shop_names
    FROM ranked t
    WHERE NOT COALESCE(r.data_issue, TRUE) AND r.rank_time IS NOT NULL
      AND NOT t.data_issue AND t.input_sku = r.input_sku AND t.rank_time = r.rank_time
) ties ON TRUE
ORDER BY q.input_sku
"""


class SkuQueryError(RuntimeError):
    """Sanitised failure. ``code`` is one of the CODE_* constants below."""

    CODE_CONFIG = "config"
    CODE_AUTH = "auth"
    CODE_CONNECTION = "connection"
    CODE_TIMEOUT = "timeout"
    CODE_SQL = "sql"
    CODE_DATABASE = "database"
    CODE_CONTRACT = "contract"

    def __init__(self, code: str, message: str, sqlstate: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.sqlstate = sqlstate

    @property
    def retryable_connection(self) -> bool:
        return self.code == self.CODE_CONNECTION

    @property
    def fatal(self) -> bool:
        """Errors for which later batches cannot succeed either."""
        return self.code in {self.CODE_CONFIG, self.CODE_AUTH, self.CODE_SQL, self.CODE_CONTRACT}


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    database: str
    username: str
    password: str = field(repr=False)
    port: int = 5432
    sslmode: str = "require"
    connect_timeout_seconds: int = 10
    statement_timeout_ms: int = 15000
    lock_timeout_ms: int = 1000
    idle_in_transaction_timeout_ms: int = 10000

    def __repr__(self) -> str:  # never print host/user/secret
        return f"DatabaseSettings(database={self.database!r}, sslmode={self.sslmode!r}, <redacted>)"

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> DatabaseSettings:
        def pick(name: str) -> str:
            return str(values.get(ENV_PREFIX + name) or "").strip()

        has_env_override = any(
            ENV_PREFIX + name in values
            for name in ("HOST", "PORT", "DATABASE", "USERNAME", "PASSWORD", "SSLMODE")
        )
        if not has_env_override:
            builtin = builtin_database_settings()
            if builtin is None:
                raise SkuQueryError(SkuQueryError.CODE_CONFIG, "未配置 ERP 只读数据库连接，请安装包含授权连接配置的版本或联系管理员配置 ERP_SKU_DB_*。")
            return builtin

        missing = [name for name in ("HOST", "DATABASE", "USERNAME", "PASSWORD") if not pick(name)]
        if missing:
            raise SkuQueryError(
                SkuQueryError.CODE_CONFIG,
                "未配置 ERP 只读数据库连接，请联系管理员配置 " + "、".join(ENV_PREFIX + name for name in missing),
            )
        port_text = pick("PORT") or "5432"
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise SkuQueryError(SkuQueryError.CODE_CONFIG, "ERP 数据库端口配置无效")
        host = pick("HOST")
        database = pick("DATABASE")
        sslmode = (pick("SSLMODE") or "require").lower()
        if sslmode not in ALLOWED_SSL_MODES:
            raise SkuQueryError(SkuQueryError.CODE_CONFIG, "ERP 数据库 SSL 模式配置无效")
        return cls(host=host, database=database, username=pick("USERNAME"),
                   password=str(values.get(ENV_PREFIX + "PASSWORD") or ""), port=int(port_text), sslmode=sslmode)

    @classmethod
    def from_environment(cls) -> DatabaseSettings:
        return cls.from_mapping(os.environ)


def builtin_database_settings(path: Path | None = None) -> DatabaseSettings | None:
    """Load a private release resource; public source contains no connection values."""
    source = path if path is not None else Path(__file__).resolve().parents[1] / "resources" / "erp_sku_database.json"
    try:
        values = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError):
        raise SkuQueryError(SkuQueryError.CODE_CONFIG, "安装包内置 ERP 连接配置无效，请重新安装已校验的版本。") from None
    required = {"host", "database", "username", "password"}
    if (not isinstance(values, dict) or not required.issubset(values)
            or set(values) - required - {"port", "sslmode"}
            or any(not isinstance(v, str) and not (k == "port" and type(v) is int) for k, v in values.items())):
        raise SkuQueryError(SkuQueryError.CODE_CONFIG, "安装包内置 ERP 连接配置无效，请重新安装已校验的版本。")
    return DatabaseSettings.from_mapping({ENV_PREFIX + key.upper(): str(value) for key, value in values.items()})


@dataclass(frozen=True)
class AuthorizedScope:
    """Trusted store/site boundary applied *before* ranking.

    ``company_wide`` must be granted explicitly; an empty scope fails closed.
    """

    company_wide: bool = False
    shop_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.company_wide and not self.shop_ids:
            raise SkuQueryError(SkuQueryError.CODE_CONFIG, "未取得可信的店铺授权范围，已拒绝查询")

    @property
    def digest_text(self) -> str:
        return "*" if self.company_wide else ",".join(sorted(self.shop_ids))

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> AuthorizedScope:
        mapping = environ if environ is not None else os.environ
        if "ERP_SKU_AUTHORIZED_SHOPS" not in mapping:
            text = BUILTIN_AUTHORIZED_SHOPS
        else:
            text = str(mapping.get("ERP_SKU_AUTHORIZED_SHOPS") or "").strip()
        if text == "*":
            return cls(company_wide=True)
        shops = tuple(dict.fromkeys(part for part in re.split(r"[\s,;，；]+", text) if part))
        if not shops or not all(part.isdigit() for part in shops):
            raise SkuQueryError(
                SkuQueryError.CODE_CONFIG,
                "未配置店铺授权范围 ERP_SKU_AUTHORIZED_SHOPS（* 表示公司级，或填写授权 Shop ID），已拒绝查询",
            )
        return cls(shop_ids=shops)


@dataclass(frozen=True)
class QuerySpec:
    skus: tuple[str, ...]
    scope: AuthorizedScope
    shop_ids: tuple[str, ...] = ()   # user filter; intersected with the scope
    countries: tuple[str, ...] = ()  # user filter on api_shop_info.region

    def effective_shop_filter(self) -> tuple[bool, list[str]]:
        """Return (all_shops, shop_ids) after intersecting user filter with the authorised scope."""
        if self.scope.company_wide:
            return (not self.shop_ids), list(self.shop_ids)
        allowed = set(self.scope.shop_ids)
        chosen = [shop for shop in self.shop_ids if shop in allowed] if self.shop_ids else sorted(allowed)
        return False, chosen


@dataclass(frozen=True)
class CandidateRow:
    input_sku: str
    result_status: str
    candidate_shop_id: str
    candidate_shop_name: str
    region: str
    item_id: str
    model_id: str
    matched_sku: str
    candidate_create_time: datetime | None
    item_status: str
    item_archived: bool | None
    model_archived: bool | None
    match_source: str
    matched_item_count: int
    matched_shop_count: int
    data_issue_count: int
    tied_shop_count: int
    base_clean_time: datetime | None
    model_clean_time: datetime | None
    tied_shop_ids: tuple[str, ...]
    tied_shop_names: tuple[str, ...]


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _optional_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _row_from_mapping(raw: Mapping[str, Any]) -> CandidateRow:
    return CandidateRow(
        input_sku=_text(raw["input_sku"]), result_status=_text(raw["result_status"]),
        candidate_shop_id=_text(raw.get("candidate_shop_id")), candidate_shop_name=_text(raw.get("candidate_shop_name")),
        region=_text(raw.get("region")), item_id=_text(raw.get("item_id")), model_id=_text(raw.get("model_id")),
        matched_sku=_text(raw.get("matched_sku")), candidate_create_time=_datetime(raw.get("candidate_create_time")),
        item_status=_text(raw.get("item_status")), item_archived=_optional_bool(raw.get("item_archived")),
        model_archived=_optional_bool(raw.get("model_archived")), match_source=_text(raw.get("match_source")),
        matched_item_count=int(raw.get("matched_item_count") or 0),
        matched_shop_count=int(raw.get("matched_shop_count") or 0),
        data_issue_count=int(raw.get("data_issue_count") or 0), tied_shop_count=int(raw.get("tied_shop_count") or 0),
        base_clean_time=_datetime(raw.get("base_clean_time")), model_clean_time=_datetime(raw.get("model_clean_time")),
        tied_shop_ids=tuple(_text(item) for item in (raw.get("tied_shop_ids") or ())),
        tied_shop_names=tuple(_text(item) for item in (raw.get("tied_shop_names") or ())),
    )


def classify_database_error(exc: BaseException, *, connecting: bool = False) -> SkuQueryError:
    """Map a driver exception to a sanitised ``SkuQueryError`` without leaking its text."""
    if isinstance(exc, SkuQueryError):
        return exc
    sqlstate = _text(getattr(exc, "pgcode", ""))
    name = type(exc).__name__
    lowered = _text(exc).casefold()
    if sqlstate in {"57014", "55P03"}:
        return SkuQueryError(SkuQueryError.CODE_TIMEOUT, "ERP 数据库查询超时，已记为查询失败，不代表未找到", sqlstate)
    if sqlstate.startswith("28") or sqlstate == "42501" or (
            connecting and ("authentication failed" in lowered or "password" in lowered and "fail" in lowered)):
        return SkuQueryError(SkuQueryError.CODE_AUTH, "ERP 数据库认证或权限失败，请联系管理员检查只读账号", sqlstate)
    if sqlstate.startswith(("42", "22")):
        return SkuQueryError(SkuQueryError.CODE_SQL, "ERP 数据库查询结构或参数错误，请联系开发人员", sqlstate)
    if (sqlstate.startswith("08") or sqlstate in {"57P01", "57P02", "57P03"}
            or isinstance(exc, (OSError, ConnectionError)) or name in {"OperationalError", "InterfaceError"}):
        return SkuQueryError(SkuQueryError.CODE_CONNECTION,
                             "无法连接或中断连接 ERP 数据库，请检查网络、TLS 与连接配置", sqlstate)
    return SkuQueryError(SkuQueryError.CODE_DATABASE, "ERP 数据库查询失败，请稍后重试", sqlstate)


def _default_connection_factory(settings: DatabaseSettings) -> Any:
    import psycopg2  # imported lazily so unit tests never need a database driver

    options = " ".join((
        "-c default_transaction_read_only=on",
        f"-c statement_timeout={int(settings.statement_timeout_ms)}",
        f"-c lock_timeout={int(settings.lock_timeout_ms)}",
        f"-c idle_in_transaction_session_timeout={int(settings.idle_in_transaction_timeout_ms)}",
        "-c TimeZone=Asia/Shanghai",
    ))
    return psycopg2.connect(
        host=settings.host, port=settings.port, dbname=settings.database, user=settings.username,
        password=settings.password, sslmode=settings.sslmode, connect_timeout=settings.connect_timeout_seconds,
        application_name=APPLICATION_NAME, options=options,
    )


class ErpSkuRepository:
    """Single-connection repository; always use as a context manager or call ``close``."""

    def __init__(self, settings: DatabaseSettings,
                 connection_factory: Callable[[DatabaseSettings], Any] | None = None) -> None:
        self._settings = settings
        self._factory = connection_factory or _default_connection_factory
        self._connection: Any = None

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - cleanup must never mask the real outcome
                pass

    def _connect(self) -> Any:
        if self._connection is None:
            try:
                connection = self._factory(self._settings)
            except Exception as exc:  # noqa: BLE001
                raise classify_database_error(exc, connecting=True) from None
            try:
                connection.autocommit = True
            except Exception:  # noqa: BLE001
                pass
            self._connection = connection
        return self._connection

    def _execute(self, spec: QuerySpec) -> list[dict[str, Any]]:
        all_shops, shop_ids = spec.effective_shop_filter()
        regions = [country.strip().upper() for country in spec.countries if country.strip()]
        params = {"skus": list(spec.skus), "all_shops": all_shops, "shop_ids": shop_ids,
                  "filter_regions": bool(regions), "regions": regions}
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(CORE_SQL, params)
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, record)) for record in cursor.fetchall()]
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001
                pass

    def query_batch(self, spec: QuerySpec) -> list[CandidateRow]:
        """Return exactly one row per distinct input SKU, or raise ``SkuQueryError``."""
        if not spec.skus:
            return []
        if (not spec.scope.company_wide and not spec.effective_shop_filter()[1]):
            raise SkuQueryError(SkuQueryError.CODE_CONFIG, "所选店铺不在授权范围内，已拒绝查询")
        last_error: SkuQueryError | None = None
        for attempt in range(2):  # one controlled reconnect for recoverable read failures
            try:
                records = self._execute(spec)
                break
            except SkuQueryError as exc:
                last_error = exc
            except Exception as exc:  # noqa: BLE001
                last_error = classify_database_error(exc)
            self.close()
            if not last_error.retryable_connection or attempt == 1:
                raise last_error
        else:  # pragma: no cover - loop always breaks or raises
            raise last_error or SkuQueryError(SkuQueryError.CODE_DATABASE, "ERP 数据库查询失败")
        try:
            rows = [_row_from_mapping(record) for record in records]
        except (KeyError, TypeError, ValueError):
            raise SkuQueryError(SkuQueryError.CODE_CONTRACT, "ERP 查询返回结构异常，已停止") from None
        expected = set(spec.skus)
        returned = [row.input_sku for row in rows]
        if len(returned) != len(set(returned)) or set(returned) != expected:
            raise SkuQueryError(SkuQueryError.CODE_CONTRACT, "ERP 查询结果与输入 SKU 不一致，已停止")
        return rows


def chunked(values: Sequence[str], size: int) -> Iterable[tuple[str, ...]]:
    for start in range(0, len(values), size):
        yield tuple(values[start:start + size])
