"""Connection settings shared by Shopee recharge controls and store synchronization."""
from __future__ import annotations

import os
import re
from contextlib import closing

import psycopg2


MISSING_PASSWORD = "广告充值数据库连接配置不完整，请联系管理员运行安装目录 tools 下的 configure_ads_database.cmd。"
GUARD_TABLES = (
    "shopee_ads_recharge_ip_whitelist", "shopee_ads_recharge_site_quota",
    "shopee_ads_recharge_run_log", "shopee_ads_recharge_store_log", "shop_browsers",
    "recharge_cutover", "recharge_site_settings",
)
RETIRED_HOSTS = {"rpa.whhqyl.com.cn", "47.112.20.68"}


def resolve_password(guard: bool = True) -> str:
    for name in ("HQYL_GUARD_DB_PASSWORD", "PGPASSWORD"):
        if os.getenv(name):
            return os.environ[name]
    raise ValueError(MISSING_PASSWORD)


def connection_parameters(password: str) -> dict:
    host = os.getenv("HQYL_GUARD_DB_HOST", "").strip()
    database = os.getenv("HQYL_GUARD_DB_NAME", "wh_rpa").strip()
    user = os.getenv("HQYL_GUARD_DB_USER", "").strip()
    if not all((host, database, user, password)):
        raise ValueError(MISSING_PASSWORD)
    if any(part.strip().casefold().rstrip(".") in RETIRED_HOSTS for part in host.split(",")):
        raise ValueError("广告充值仍配置为旧数据库，请联系管理员设置新库地址。")
    if database != "wh_rpa":
        raise ValueError("广告充值的目标数据库必须为 wh_rpa。")
    if not re.fullmatch(r"[A-Za-z0-9.:_-]+", host):
        raise ValueError("广告充值数据库主机名无效。")
    port = os.getenv("HQYL_GUARD_DB_PORT", "5432")
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise ValueError("广告充值数据库端口无效。")
    schema = os.getenv("HQYL_GUARD_DB_SCHEMA", "shopee_ads").strip()
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
        raise ValueError("广告充值数据库 schema 无效。")
    mode = os.getenv("HQYL_GUARD_DB_SSLMODE", "require").lower()
    if mode not in {"require", "verify-ca", "verify-full"}:
        if mode != "disable" or os.getenv("HQYL_GUARD_DB_PRIVATE_NETWORK") != "1":
            raise ValueError("请使用加密连接或管理员批准的专用网络。")
    return {"host": host, "port": port,
            "dbname": database, "user": user, "password": password, "connect_timeout": 8,
            "sslmode": mode, "require_auth": "scram-sha-256",
            "options": f"-c search_path={schema},pg_catalog"}


def connect_runtime_database(guard: bool = True):
    return psycopg2.connect(**connection_parameters(resolve_password(guard)))


def validate_connection(password: str) -> None:
    params = connection_parameters(password)
    params["options"] += " -c default_transaction_read_only=on -c statement_timeout=5000"
    connection = None
    try:
        connection = psycopg2.connect(**params)
        with connection.cursor() as cursor:
            # Prevent an unprepared target from silently recreating empty quota
            # and payment history. Store changes only start after this succeeds.
            cursor.execute("SELECT current_database(), bool_and(to_regclass(t) IS NOT NULL) "
                           "FROM unnest(%s::text[]) AS names(t)", (list(GUARD_TABLES),))
            if cursor.fetchone() != (params["dbname"], True):
                raise ValueError("新充值数据库缺少充值风控或店铺资料表，请先完成迁移。")
            cursor.execute("SELECT active FROM recharge_cutover WHERE singleton")
            if cursor.fetchone() != (True,):
                raise ValueError("充值数据尚未完成切换，请联系管理员。")
    except psycopg2.Error:
        raise ValueError("广告充值数据库连接失败，请检查新库设置及网络。") from None
    finally:
        if connection is not None:
            connection.close()


def prepare_runtime_database() -> None:
    validate_connection(resolve_password())
    from backend.services.shopee_ads_recharge import _ensure_superbrowser_path
    _ensure_superbrowser_path()
    from main.shopee import ads_recharge_guard
    if getattr(ads_recharge_guard, "BUILTIN_DB_PASSWORD", None) != "" or ads_recharge_guard.GUARD_DB_CONFIG:
        raise ValueError("当前源码仍加载旧版充值模块，请先运行发布准备脚本。")


def authorized_machine(context, site_code, mode):
    with closing(connect_runtime_database()) as conn, conn.cursor() as cur:
        cur.execute("SELECT allowed_sites, allow_dingtalk_recharge, allow_custom_recharge "
                    "FROM shopee_ads_recharge_ip_whitelist WHERE ip=%s AND enabled", (context["ip"],))
        row = cur.fetchone()
    if row:
        sites = {s.lower() for s in re.split(r"[,，;\s]+", row[0]) if s}
        if ("all" in sites or site_code.lower() in sites) and (
                (mode == "custom" and row[2]) or (mode == "sheet" and row[1])):
            return dict(context, allowed_sites=row[0], allow_dingtalk_recharge=row[1], allow_custom_recharge=row[2])
    raise PermissionError("当前电脑或充值方式未获得授权，请联系管理员。")


def private_site_settings(site_code):
    with closing(connect_runtime_database()) as conn, conn.cursor() as cur:
        cur.execute("SELECT settings FROM recharge_site_settings WHERE site_code=%s", (site_code,))
        row = cur.fetchone()
    return row[0] if row else {}


def ensure_daily_quotas(currencies, daily_limit) -> None:
    from datetime import date

    with closing(connect_runtime_database()) as conn, conn, conn.cursor() as cur:
        for site, currency in currencies.items():
            cur.execute(
                "INSERT INTO shopee_ads_recharge_site_quota "
                "(site_code, quota_date, currency, daily_limit, used_amount, reserved_amount, enabled, remark) "
                "VALUES (%s, %s, %s, %s, 0, 0, TRUE, 'daily quota') "
                "ON CONFLICT (site_code, quota_date) DO NOTHING",
                (site, date.today(), currency, daily_limit(site)),
            )


def self_check() -> dict:
    """Check both release consumers without accessing the real database or stores."""
    from unittest.mock import Mock, patch
    from backend.services.shopee_ads_recharge import _ensure_superbrowser_path
    _ensure_superbrowser_path()
    from main.shopee import ads_recharge_guard
    from util import db_helper

    config = {"HQYL_GUARD_DB_HOST": "example.invalid", "HQYL_GUARD_DB_NAME": "wh_rpa",
              "HQYL_GUARD_DB_USER": "test_user", "HQYL_GUARD_DB_PASSWORD": "self-check-only",
              "PGPASSWORD": "", "HQYL_GUARD_DB_PORT": "5432", "HQYL_GUARD_DB_SCHEMA": "shopee_ads"}
    with patch.dict(os.environ, config), patch.object(psycopg2, "connect", return_value=Mock()) as connect:
        ads_recharge_guard.get_guard_connection().close()
        db_helper.get_pg_connection().close()
        if len(connect.call_args_list) != 2 or any(
            c.kwargs.get("host") != "example.invalid" or c.kwargs.get("password") != "self-check-only"
            for c in connect.call_args_list
        ):
            raise RuntimeError("充值或店铺同步未使用配置的新库")
    return {"status": "passed", "builtin_credential": False, "guard_and_sync_injection": True,
            "real_database_connections": 0}
