# db_helper.py

import json

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
from util import database_credentials


# pgsql数据库连接信息
import os

def get_pg_connection():
    from backend.ads_database import connect_runtime_database
    return connect_runtime_database(guard=False)

def query_mobile_list(name):
    conn = get_pg_connection()
    mobiles = fetch_column_from_db(conn,'dingtalk_employee','mobile',f"name ='{name}'")
    conn.close()
    if len(mobiles) ==0:
        return None
    return mobiles
def query_auth_code(name):
    conn = get_pg_connection()
    mobiles = fetch_column_from_db(conn, "shop_browsers", "browser_oauth", "browser_name LIKE %s AND length(browser_oauth) <> 32", (f"%{name}%",))
    conn.close()
    if len(mobiles) ==0:
        return None
    return mobiles
def query_shop_name(auth):
    conn = get_pg_connection()
    mobiles = fetch_column_from_db(conn, "shop_browsers", "browser_name", "browser_oauth LIKE %s", (f"%{auth}%",))
    conn.close()
    if len(mobiles) ==0:
        return None
    return mobiles

# 同步数据库表数据
def upsert_batch_to_pg(conn, table_name, data_list, conflict_keys):
    """
    批量 UPSERT（插入或更新）
    :param conn: 数据库连接
    :param table_name: 表名
    :param data_list: list[dict]，每个 dict 是一条记录
    :param conflict_keys: list[str]，用于判断冲突（主键或唯一约束）
    """
    if table_name == "shop_browsers":
        from backend.ads_database import sync_shop_browsers
        return sync_shop_browsers(conn, data_list)
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
    if table_name == "shop_browsers":
        table_name = "tbl_fin_ads_shop_browser"
        scope = "tenant_id = 1 AND NOT archived"
        where_clause = f"{scope} AND ({where_clause})" if where_clause else scope
    query = f"SELECT {column_name} FROM {table_name}"
    if where_clause:
        query += f" WHERE {where_clause}"

    with conn.cursor() as cur:
        if params:
            cur.execute(query, params)
        else:
            cur.execute(query)
        rows = cur.fetchall()
    return [row[0] for row in rows]

def get_process_code_byname(name,conn):
    process_code_list = fetch_column_from_db(conn, "dingtalk_process_template", "process_code", f"name = '{name}'")
    return process_code_list[0]



"""" 查询tiktok店铺代码"""
def get_tiktok_shop_code(name,conn):
    process_code_list = fetch_column_from_db(conn, "tiktok_shop", "shop_code", f"shop_name = '{name}'")
    return process_code_list[0]

"""根据映射获取同步数据库数据 """
def get_ansy_datalist(browser_list,FIELD_MAPPING,dimtype_arr):
    data_list = []
    for item in browser_list:
        row = {}
        for k, v in item.items():
            col = FIELD_MAPPING.get(k)
            if col:
                # tags字段转换为JSON字符串
                if col == 'tags':
                    row[col] = json.dumps(v, ensure_ascii=False)
                # platform_id 和 site_id 转成整数
                elif col in dimtype_arr:
                    row[col] = int(v)
                else:
                    row[col] = v
        data_list.append(row)
    return data_list
