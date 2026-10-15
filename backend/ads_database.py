"""Built-in desktop database configuration and read-only startup validation."""
from __future__ import annotations

import os

MISSING_PASSWORD = "广告充值数据库连接配置不完整，请联系管理员。"


def resolve_password(guard: bool = True) -> str:
    names = ("HQYL_GUARD_DB_PASSWORD", "PGPASSWORD") if guard else ("PGPASSWORD", "HQYL_GUARD_DB_PASSWORD")
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    value = _guard_module().BUILTIN_DB_PASSWORD
    if not value:
        raise ValueError(MISSING_PASSWORD)
    return value


def _guard_module():
    from backend.services.shopee_ads_recharge import _ensure_superbrowser_path
    _ensure_superbrowser_path()
    from main.shopee import ads_recharge_guard
    return ads_recharge_guard


def validate_connection(password: str) -> None:
    if not password:
        raise ValueError(MISSING_PASSWORD)
    import psycopg2
    guard = _guard_module()
    config = dict(guard.GUARD_DB_CONFIG)
    config.update(password=password, connect_timeout=8,
                  options="-c default_transaction_read_only=on -c statement_timeout=5000")
    connection = None
    try:
        try:
            connection = psycopg2.connect(**config)
        except psycopg2.OperationalError as exc:
            message = str(exc).casefold()
            fallback = str(guard.GUARD_DB_FALLBACK_HOST or "").strip()
            if not fallback or fallback == config.get("host") or not any(term in message for term in ("could not translate host name", "name or service not known")):
                raise
            config["host"] = fallback
            connection = psycopg2.connect(**config)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone() != (1,):
                raise ValueError("数据库连接验证未返回预期结果。")
    except psycopg2.Error:
        raise ValueError("广告充值数据库连接失败，请检查数据库密码及网络；未启动充值。") from None
    finally:
        if connection is not None:
            connection.close()



def prepare_runtime_database() -> None:
    validate_connection(resolve_password())
    from util.database_credentials import set_desktop_password_provider
    set_desktop_password_provider(resolve_password)


def self_check() -> dict:
    """Verify the built-in password reaches both bundled consumers without setup."""
    import psycopg2
    guard = _guard_module()
    from util import database_credentials, db_helper
    original_connect = psycopg2.connect
    original_provider = database_credentials._desktop_provider
    names = ("HQYL_GUARD_DB_PASSWORD", "PGPASSWORD")
    old_environment = {name: os.environ.pop(name, None) for name in names}
    calls = []

    class FakeConnection:
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql):
            if sql != "SELECT 1":
                raise RuntimeError("Self-check attempted a database write")
        def fetchone(self): return (1,)
        def close(self): pass

    def connect(**config):
        calls.append(config)
        return FakeConnection()

    try:
        psycopg2.connect = connect
        prepare_runtime_database()
        guard.get_guard_connection().close()
        db_helper.get_pg_connection().close()
        if len(calls) != 3 or not guard.BUILTIN_DB_PASSWORD or not all(item.get("password") == guard.BUILTIN_DB_PASSWORD for item in calls):
            raise RuntimeError("Built-in database credential injection failed")
        return {"status": "passed", "builtin_credential": True, "guard_and_sync_injection": True,
                "works_without_user_configuration": True, "real_database_connections": 0}
    finally:
        psycopg2.connect = original_connect
        database_credentials._desktop_provider = original_provider
        for name, value in old_environment.items():
            if value is not None: os.environ[name] = value
            else: os.environ.pop(name, None)
