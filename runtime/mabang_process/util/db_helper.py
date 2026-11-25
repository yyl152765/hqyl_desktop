# db_helper.py
import json
import sys, os
process_config_path = os.path.dirname(__file__)
config_path = os.path.dirname(os.path.dirname(__file__))
sys.path.append(config_path)
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
import yaml
HQYL_MARKERS = (
    "HQYL_ATTEMPT_ID",
    "HQYL_JOB_DIR",
    "HQYL_OUTPUT_DIR",
    "HQYL_INPUT_FILE",
    "HQYL_PARAMETERS_JSON",
    "HQYL_ASSETS_JSON",
)


def _is_hqyl_runtime():
    return any(str(os.environ.get(name) or "").strip() for name in HQYL_MARKERS)


# pgsql数据库连接信息
def get_pg_connection(db_type='rpa', database=None):
    if database is None:
        raw_database = str(os.environ.get("DATABASE_CONFIG_JSON") or "").strip()
        if raw_database:
            database = json.loads(raw_database)
        elif _is_hqyl_runtime():
            raise ValueError(
                "HQYL mode requires explicit database config or DATABASE_CONFIG_JSON"
            )
        else:
            with open(rf'{config_path}/config/config.yaml', 'r', encoding='utf-8') as config:
                cfg = yaml.safe_load(config)
            database = cfg['database'][db_type]
    required = ("host", "port", "dbname", "user", "password")
    missing = [name for name in required if not str(database.get(name) or "").strip()]
    if missing:
        raise ValueError("database config missing: " + ", ".join(missing))
    return psycopg2.connect(
        host=database['host'],
        port=database['port'],
        dbname=database['dbname'],
        user=database['user'],
        password=database['password'],
        connect_timeout=int(database.get("connect_timeout") or 10),
    )


# 同步数据库表数据
def upsert_batch_to_pg(conn, table_name, data_list, conflict_keys):
    """
    批量 UPSERT（插入或更新）
    :param conn: 数据库连接
    :param table_name: 表名
    :param data_list: list[dict]，每个 dict 是一条记录
    :param conflict_keys: list[str]，用于判断冲突（主键或唯一约束）
    """
    if not data_list:
        return


    keys = list(data_list[0].keys())
    values = [[row.get(k) for k in keys] for row in data_list]

    insert_stmt = sql.SQL("""
        INSERT INTO {table} ({fields})
        VALUES %s
        ON CONFLICT ({conflict_fields})
        DO UPDATE SET {updates}
    """).format(
        table=sql.Identifier(table_name),
        fields=sql.SQL(', ').join(map(sql.Identifier, keys)),
        conflict_fields=sql.SQL(', ').join(map(sql.Identifier, conflict_keys)),
        updates=sql.SQL(', ').join([
            sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(k)) for k in keys if k not in conflict_keys
        ])
    )

    with conn.cursor() as cur:
        execute_values(cur, insert_stmt, values)
    conn.commit()


def truncate_table(conn, table_name):
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("TRUNCATE TABLE {table}").format(
                table=sql.Identifier(table_name),
            )
        )
    conn.commit()


def insert_batch_to_pg(conn, table_name, data_list):
    """
    批量插入数据
    :param conn: 数据库连接
    :param table_name: 表名
    :param data_list: list[dict]，每个 dict 是一条记录
    :return: 插入条数
    """
    if not data_list:
        return 0

    keys = list(data_list[0].keys())
    values = [[row.get(k) for k in keys] for row in data_list]

    insert_stmt = sql.SQL("""
        INSERT INTO {table} ({fields})
        VALUES %s
    """).format(
        table=sql.Identifier(table_name),
        fields=sql.SQL(', ').join(map(sql.Identifier, keys)),
    )

    with conn.cursor() as cur:
        execute_values(cur, insert_stmt, values)
    conn.commit()
    return len(values)


def fetch_column_from_db(conn, table_name, column_name, where_clause=None, params=None):
    """
    通用函数：从数据库表查询某列，返回该列所有值列表
    :param conn: 数据库连接
    :param table_name: 表名，字符串
    :param column_name: 需要查询的列名，字符串
    :param where_clause: SQL条件（不含WHERE），字符串，例如 "is_valid = true"
    :param params: 条件参数，元组或列表，用于防止SQL注入，默认None
    :return: list，查询列所有值
    """
    query = sql.SQL("SELECT {column} FROM {table}").format(
        column=sql.Identifier(column_name),
        table=sql.Identifier(table_name),
    )
    if where_clause:
        query = query + sql.SQL(" WHERE ") + sql.SQL(where_clause)

    with conn.cursor() as cur:
        if params:
            cur.execute(query, params)
        else:
            cur.execute(query)
        rows = cur.fetchall()
    return [row[0] for row in rows]

def get_process_code_byname(name,conn):
    process_code_list = fetch_column_from_db(
        conn,
        "dingtalk_process_template",
        "process_code",
        "name = %s",
        (name,),
    )
    return process_code_list[0]



"""" 查询tiktok店铺代码"""
def get_tiktok_shop_code(name,conn):
    process_code_list = fetch_column_from_db(
        conn,
        "tiktok_shop",
        "shop_code",
        "shop_name = %s",
        (name,),
    )
    return process_code_list[0]
