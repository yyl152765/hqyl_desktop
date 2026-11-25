import pandas as pd
import shutil
import os
import sys
import time
from datetime import datetime,timedelta
import socket
from collections import Counter
import math
import win32com.client.gencache as win32_gencache
import json
import calendar
def get_last_month_start_end():
    today = datetime.today()
    year, month = today.year, today.month
    last_month_year = year if month > 1 else year - 1
    last_month = month - 1 if month > 1 else 12
    start = datetime(last_month_year, last_month, 1, 0, 0, 0)
    end = datetime(last_month_year, last_month, calendar.monthrange(last_month_year, last_month)[1], 23, 59, 59)
    return start.strftime('%Y-%m-%d %H:%M:%S'), end.strftime('%Y-%m-%d %H:%M:%S')
def parse_rate(series):
    s = series.astype(str).str.strip()

    # 情况 1：带 %
    if s.str.contains('%').any():
        return (
            s.str.replace('%', '', regex=False)
             .astype(float)
             / 100
        )

    # 情况 2 / 3：数值
    s = pd.to_numeric(s, errors='coerce')
    if s.max() > 1:
        return s / 100

    return s
def convert_cookie_json_file_to_dict(json_file_path):
    """
    将 BigSeller 导出的 JSON cookie 文件转换为 requests 可用的字典
    """
    with open(json_file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    cookie_list = data.get("cookies", [])

    # 转换为字典
    cookies_dict = {c["name"]: c["value"] for c in cookie_list if "name" in c and "value" in c}
    return cookies_dict

def get_last_month_first_day():
    """
    获取上个月第一天
    :return:
    """
    today = datetime.today()
    first_day_this_month = today.replace(day=1)
    last_day_last_month = first_day_this_month - timedelta(days=1)
    first_day_last_month = last_day_last_month.replace(day=1)
    return first_day_last_month.strftime("%Y-%m-%d")

def get_last_month_last_day():
    """
    获取上个月最后一天
    :return:
    """
    today = datetime.today()
    first_day_this_month = today.replace(day=1)
    last_day_last_month = first_day_this_month - timedelta(days=1)
    return last_day_last_month.strftime("%Y-%m-%d")

def _ensure_excel_dispatch():
    """Create an Excel COM object, rebuilding pywin32's cache if it is corrupt."""
    try:
        return win32_gencache.EnsureDispatch('Excel.Application')
    except AttributeError as exc:
        if (
            "CLSIDToClassMap" not in str(exc)
            and "MinorVersion" not in str(exc)
        ):
            raise

        gen_path = win32_gencache.GetGeneratePath()
        print(f"Excel COM cache is corrupt; rebuilding pywin32 cache: {gen_path}")
        shutil.rmtree(gen_path, ignore_errors=True)
        for module_name in list(sys.modules):
            if module_name == "win32com.gen_py" or module_name.startswith("win32com.gen_py."):
                sys.modules.pop(module_name, None)
        win32_gencache.Rebuild()
        return win32_gencache.EnsureDispatch('Excel.Application')


def xls_to_xlsx(xls_path):
    """使用 Excel COM 转换 xls -> xlsx"""
    if not os.path.exists(xls_path):
        raise FileNotFoundError(xls_path)
    if not xls_path.lower().endswith('.xls'):
        return xls_path  # 已是 xlsx

    xlsx_path = xls_path + 'x'
    excel = _ensure_excel_dispatch()
    excel.DisplayAlerts = False
    excel.Visible = False

    wb = None
    try:
        wb = excel.Workbooks.Open(xls_path)
        wb.SaveAs(xlsx_path, FileFormat=51)  # 51 = .xlsx
    finally:
        if wb is not None:
            wb.Close()
        excel.Quit()

    print(f"已转换: {xlsx_path}")
    return xlsx_path


def excel_to_dict(file_path, sheet_name=None):
    """
    将 Excel 第一列作为 key，第二列作为 value，生成字典

    :param file_path: Excel 文件路径
    :param sheet_name: 指定 sheet，不填则默认第一个
    :return: dict
    """
    wb = load_workbook(file_path, data_only=True)
    ws = wb[sheet_name] if sheet_name else wb.active

    result = {}

    for row in ws.iter_rows(min_row=1, values_only=True):
        key = row[0]
        value = row[1]
        if key is not None:   # key 一定要有
            result[key] = value

    return result

#"创建日期目录"
def create_directory(base_directory):
    current_date = datetime.now().strftime("%Y%m%d")

    directory_path = os.path.join(base_directory, current_date)

    # 创建目录
    try:
        os.makedirs(directory_path, exist_ok=True)
        print(f"目录 '{directory_path}' 创建成功。")
    except OSError as e:
        print(f"创建目录 '{directory_path}' 失败: {e}")
    return directory_path

def strip_strings(data):
    if isinstance(data, list):
        return [strip_strings(x) for x in data]
    elif isinstance(data, tuple):
        return tuple(strip_strings(x) for x in data)
    elif isinstance(data, dict):
        return {k: strip_strings(v) for k, v in data.items()}
    elif isinstance(data, str):
        return data.strip()
    elif data is None:
        return ""
    elif isinstance(data, float) and math.isnan(data):
        return ""
    else:
        return data

def extract_name_from_str(s: str) -> str:
    """
    提取店铺姓名
    :param s:
    :return:
    """
    clean = s.replace("「", "").replace("」", "").replace("'", "")
    if "-" not in clean:
        return ""  # 提取不到就返回原值
    return clean.split("-", 1)[0]

def split_list_by_batch_size(data_list, batch_size=500):
    """
    将列表按指定批次大小分组
    :param data_list: 原始列表
    :param batch_size: 每组最大数量，默认3000
    :return: 分组后的列表（每组是子列表）
    """
    return ['\n'.join(data_list[i:i + batch_size]) for i in range(0, len(data_list), batch_size)]

def is_today(date_str):
    """
    判断字符串日期是否为当天
    支持格式：YYYY-MM-DD HH:MM:SS
    空值或格式不对返回 False
    """
    if not date_str or not date_str.strip():  # None / 空字符串 / 空白都返回 False
        return False

    try:
        dt = datetime.strptime(date_str.strip(), "%Y-%m-%d %H:%M:%S")
        return dt.date() == datetime.today().date()
    except Exception:
        return False

def statistical_frequency_for_table2(table, column_index, top_n=3):
    """
    统计数据集中指定列出现频率前 n 的值（忽略大小写）

    Args:
        table: 二维数据集
        column_index: 要分析的列索引
        top_n: 返回频率最高的前 n 个值，默认 3

    Returns:
        list: 前 n 个出现频率最高的值及其次数，格式为 [[值, 次数], ...]
    """
    values = []
    original_map = {}  # 保存原始大小写映射
    for row in table:
        if not isinstance(row, list):
            continue
        if len(row) <= column_index:
            continue
        val = row[column_index]
        if val is None:
            continue
        val_clean = str(val).strip().lstrip("* -").strip()
        if val_clean and val_clean != "--":
            val_lower = val_clean.lower()
            values.append(val_lower)
            if val_lower not in original_map:
                original_map[val_lower] = val_clean

    if not values:
        return []

    count = Counter(values)
    top = count.most_common(top_n)  # 获取前 top_n 名

    ranking_list = []
    for i, (val_lower, cnt) in enumerate(top, 1):
        val_original = original_map[val_lower]
        print(f"第{i}名: '{val_original}' - 出现{cnt}次")
        ranking_list.append([val_original, cnt])

    return ranking_list
def statistical_frequency_for_table(table,column_index,top_index = 1):
    """
    统计数据集中指定列出现频率前n的值
    Args:
        data: 二维数据集
        column_index: 要分析的列索引，默认为第8列
    Returns:
        list: 前n个出现频率最高的值及其次数，格式为[(值, 次数), ...]
    """
    values = []
    for row in table:
        # 跳过非列表行
        if not isinstance(row, list):
            continue

        # 检查指定列是否存在
        if len(row) <= column_index:
            continue
        val = row[column_index]
        # 跳过空值
        if val is None:
            continue
        # 转换为字符串并清理
        val_str = str(val).strip()
        val_clean = val_str.lstrip("* -").strip()

        # 排除无效值
        if val_clean and val_clean != "--" and val_clean != "":
            values.append(val_clean)
    # 如果没有有效值，返回空列表
    if not values:
        return []

    # 统计频率并获取前三
    count = Counter(values)
    top = count.most_common(top_index)
    ranking_list = []
    for i, (value, count) in enumerate(top, 1):
        print(f"第{i}名: '{value}' - 出现{count}次")
        ranking_list.append([value,count])
        return ranking_list



def sum_split_multiple(vals):
    """累加多表的 '7/28/42' 字段"""
    total = [0, 0, 0]
    for v in vals:
        if isinstance(v, str) and '/' in v:
            parts = [int(x.replace(',', '')) for x in v.split('/')]
        else:
            parts = [0,0,0]
        total = [x+y for x,y in zip(total, parts)]
    return '/'.join(map(str, total))

def merge_chinese_name(row):
    for col in row.index:
        if '中文名称' in col and pd.notnull(row[col]):
            return row[col]
    return None

# 提取销量(7) 和 销量(42)
def extract_7(val):
    try:
        return int(val.split('/')[0]) if isinstance(val, str) and '/' in val else None
    except:
        return 0
def extract_28(val):
    try:
        return int(val.split('/')[1]) if isinstance(val, str) and '/' in val else None
    except:
        return 0

def extract_42(val):
    try:
        return int(val.split('/')[2]) if isinstance(val, str) and '/' in val else None
    except:
        return 0


def dict_values_to_str(data):
    """
    将列表中每个字典的所有值都转成字符串
    :param data_list: list[dict]
    :return: list[dict]
    """
    result = {k: str(v) if v is not None else "" for k, v in data.items()}
    return result

def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    ip = s.getsockname()[0]
    s.close()
    return ip


from openpyxl import load_workbook

def append_to_excel(filename, data):
    """
    将二维数组追加到Excel最后一行之后（默认第一个工作表）
    :param filename: Excel 文件路径
    :param data: 二维数组
    """
    wb = load_workbook(filename)
    ws = wb.active  # 默认工作表（第一个）

    # 找到最后一行的下一行
    start_row = ws.max_row + 1

    row_index = start_row
    for row in data:
        col_index = 1  # 从A列开始
        for value in row:
            ws.cell(row=row_index, column=col_index, value=value)
            col_index += 1
        row_index += 1

    wb.save(filename)
    print(f"数据已追加到 {ws.title}! 起始行: {start_row}")
def index_to_column(n):
    result = ""
    while n >= 0:
        n, remainder = divmod(n, 26)
        result = chr(65 + remainder) + result
        n -= 1
    return result
def outer_merge_multiple(dfs):
    merged = dfs[0]
    for i, df in enumerate(dfs[1:], start=1):
        merged = pd.merge(merged, df, on='库存SKU编号', how='outer', suffixes=('', f'_df{i}'))
    return merged

def left_merge(df_left, df_right):
    return pd.merge(df_left, df_right, on='SKU', how='left')


def write_excel_from_2d_array_str(data, file_path, headers=None):
    """
    用 pandas 将二维数组写入 Excel，所有单元格都转换为字符串

    :param data: 二维数组，每行是 list
    :param file_path: 输出文件路径，例如 "output.xlsx"
    :param headers: 可选表头列表
    """
    # 强制所有内容转成字符串
    data_str = [[str(cell) if cell is not None else "" for cell in row] for row in data]

    df = pd.DataFrame(data_str, columns=headers)
    df.to_excel(file_path, index=False)
    print(f"Excel 已保存到：{file_path}")
    return file_path


def get_dynamic_months(current_month=None, months_count=4):
    """
    根据当前月份动态生成接下来几个月的月份标签

    Args:
        current_month: 当前月份（1-12），如果为None则使用系统当前月份
        months_count: 需要生成的月份数量，默认为4

    Returns:
        list: 月份标签列表，如['1月', '2月', '3月', '4月']
    """
    if current_month is None:
        current_month = datetime.now().month

    months = []
    for i in range(months_count):
        month_num = (current_month + i - 1) % 12 + 1  # 计算月份（1-12循环）
        months.append(f"{month_num}月")

    return months

#合并文件
def hebing_excel(file_list,output_file):
    dfs = []
    status = False
    for file_path in file_list:
        try:
            df = pd.read_excel(file_path)
            dfs.append(df)
        except Exception as e:
            status = False

    if not dfs:
        status = False
    else:
        merged_df = pd.concat(dfs, ignore_index=True)
        # 如果路径不存在，则创建目录
        output_dir = os.path.dirname(output_file)
        os.makedirs(output_dir, exist_ok=True)
        merged_df.to_excel(output_file, index=False)
        status = True
# 复制文件
def copy_file(src_file,dest_dir,new_name):
    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir)

    file_name = new_name if new_name else os.path.basename(src_file)
    dest_path = os.path.join(dest_dir, file_name)

    shutil.copy2(src_file, dest_path)

    return dest_path

# 获取文件下载列表
def get_dowmlaod_file_list(path=r'D:\Download_File',target_count=1,timeout=120,interval=3):
    start_time = time.time()
    excel_extensions = {'.xls', '.xlsx', '.csv'}
    while True:
        files = [f for f in os.listdir(path) if
                 os.path.isfile(os.path.join(path, f)) and os.path.splitext(f)[1].lower() in excel_extensions and '.crdownload' not in os.path.splitext(f)[1].lower()]
        if len(files) >= target_count:
            file_list = [os.path.join(path, f) for f in files]
            break
        if time.time() - start_time > timeout:
            file_list = []  # 超时未检测到足够文件
            break
        time.sleep(interval)

    return file_list

def get_download_file_list_version2(path=r'D:\Download_File',
                           target_count=1,
                           timeout=120,
                           interval=1):
    start_time = time.time()
    excel_extensions = {'.xls', '.xlsx', '.csv'}

    while True:
        files = os.listdir(path)

        # ① 过滤掉临时下载文件
        real_files = [
            f for f in files
            if os.path.isfile(os.path.join(path, f))
            and not f.endswith('.crdownload')
            and os.path.splitext(f)[1].lower() in excel_extensions
        ]

        # ② 文件数量满足
        if len(real_files) >= target_count:
            file_list = []

            # ③ 等待文件大小稳定
            for f in real_files:
                full_path = os.path.join(path, f)

                size1 = os.path.getsize(full_path)
                time.sleep(1)
                size2 = os.path.getsize(full_path)

                if size1 == size2 and size2 > 0:
                    file_list.append(full_path)

            if len(file_list) >= target_count:
                return file_list

        # ④ 超时退出
        if time.time() - start_time > timeout:
            return []

        time.sleep(interval)

def list_files_in_dir_recursive(dir_path):
    """
    获取目录及子目录下所有文件的完整路径
    """
    files = []
    for root, dirs, filenames in os.walk(dir_path):
        for f in filenames:
            files.append(os.path.join(root, f))
    return files




# 清空目录
def clean_directory(path=r'D:\Download_File'):
    for name in os.listdir(path):
        full_path = os.path.join(path, name)
        (shutil.rmtree if os.path.isdir(full_path) else os.remove)(full_path)
