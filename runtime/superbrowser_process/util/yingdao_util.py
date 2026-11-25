import pandas as pd
import shutil
import os
import time
from datetime import datetime,timedelta
import socket
from collections import Counter
import math
import requests
import hashlib
import re
from urllib.parse import urlparse


def parse_amount(text):
    """
    解析金额格式，兼容小数单位和千位分隔符。

    示例：
    - 1.4m / Rp1.4m -> 1400000
    - 35.6K -> 35600
    - 3.6万 / 3.6w -> 36000
    - 1.500.000 -> 1500000
    - 1,234.56 -> 1234.56
    """
    if not text:
        return 0

    text = str(text).replace(",", "").replace(" ", "").strip().lower()
    for symbol in ("฿", "rm", "rp", "idr", "₫", "vnd", "php", "₱", "sgd"):
        text = text.replace(symbol, "")

    has_suffix = bool(re.search(r"[kmw万]", text))
    num_match = re.findall(r"[\d\.]+", text)
    if not num_match:
        return 0

    num_str = num_match[0]
    if has_suffix:
        num = float(num_str)
    else:
        dot_count = num_str.count(".")
        if dot_count >= 2:
            num = float(num_str.replace(".", ""))
        elif dot_count == 1:
            left, right = num_str.split(".", 1)
            if len(right) == 3 and len(left) >= 4:
                num = float(num_str.replace(".", ""))
            else:
                num = float(num_str)
        else:
            num = float(num_str)

    if "万" in text or "w" in text:
        num *= 10000
    elif "k" in text:
        num *= 1000
    elif "m" in text:
        num *= 1000000

    return float(num)

def _safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        safe_args = [str(arg).encode("ascii", errors="replace").decode("ascii") for arg in args]
        print(*safe_args, **kwargs)

def get_last_week_dates(today=datetime.today()):
    """
    返回上周一和上周日日期，格式 yyyymmdd
    :return: (上周一, 上周日)
    """
    weekday = today.weekday()  # 周一=0, 周日=6

    last_monday = today - timedelta(days=weekday + 7)
    last_sunday = today - timedelta(days=weekday + 1)

    return last_monday.strftime("%Y%m%d"), last_sunday.strftime("%Y%m%d")


def col_index_to_letter(index):
    """将列下标（0开始）转换为 Excel 列字母"""
    letters = ""
    while index >= 0:
        index, remainder = divmod(index, 26)
        letters = chr(65 + remainder) + letters  # 65 = 'A'
        index -= 1
    return letters

def repair_excel(input_file,output_file):

    temp_dir = input_file + "_unzip"
    output_base = output_file[:-5]
    archive_file = output_base + ".zip"
    shutil.rmtree(temp_dir, ignore_errors=True)
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    for old_file in (archive_file, output_file):
        if os.path.exists(old_file):
            os.remove(old_file)
    try:
        with zipfile.ZipFile(input_file, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
        ws_dir = os.path.join(temp_dir, "xl", "worksheets")
        for ws_file in os.listdir(ws_dir):
            ws_path = os.path.join(ws_dir, ws_file)
            with open(ws_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 删除非法 activePane 值
            content = re.sub(r'activePane="[^"]+"', '', content)

            with open(ws_path, "w", encoding="utf-8") as f:
                f.write(content)

        shutil.make_archive(output_base, 'zip', temp_dir)
        os.replace(archive_file, output_file)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    _safe_print("修复完成，生成文件：", output_file)
    return output_file



def is_within_one_day(time_str, fmt="%Y-%m-%d %H:%M:%S"):
    """
    判断给定时间字符串和当前时间的时间差是否在一天以内。
    如果时间格式错误或超过一天，返回 False。
    """
    try:
        time_obj = datetime.strptime(time_str, fmt)
    except Exception:
        return False

    return abs(datetime.now() - time_obj) <= timedelta(days=1)

def read_txt(file_path, encoding="utf-8"):
    """读取 txt 文件全部内容并返回字符串"""
    with open(file_path, "r", encoding=encoding) as f:
        return f.read()

def append_txt(file_path, text, encoding="utf-8"):
    """向 txt 追加内容，每次自动换行"""
    with open(file_path, "a", encoding=encoding) as f:
        f.write(text + "\n")

def create_txt(file_path, content="", encoding="utf-8"):
    """创建一个 txt 文件（如果存在会覆盖），并写入初始内容"""
    with open(file_path, "w", encoding=encoding) as f:
        f.write(content)

def get_domain(url):
    """从URL中提取域名（包含子域名）"""
    try:
        return urlparse(url).netloc if url else ""
    except Exception:
        return ""
import zipfile
def zip_files(file_paths, zip_path):
    """
    将多个全路径文件打包成 zip 文件

    :param file_paths: list，文件绝对路径列表
    :param zip_path: 输出 zip 文件路径，如 "D:/output/result.zip"
    """
    # 创建 ZIP 文件（写入模式，压缩方式为 ZIP_DEFLATED）
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file in file_paths:
            if os.path.exists(file):
                # 取文件名（不保留原目录结构）
                arcname = os.path.basename(file)

                # 若需要保留目录结构，改为：
                # arcname = file

                zipf.write(file, arcname)
                _safe_print(f"已添加 -> {file}")
            else:
                _safe_print(f"文件不存在 -> {file}")

    _safe_print(f"\n打包完成！ZIP 保存路径：{zip_path}")


def encrypt_sha1(fpath: str) -> str:
    """
    转化为sha1
    :param fpath:
    :return:
    """
    with open(fpath, 'rb') as f:
        return hashlib.new('sha1', f.read()).hexdigest()


def download_file(url, save_path):
    """
    Get请求下载文件
    :param url: 地址
    :param save_path: 保存路径
    :return:
    """
    # 发送GET请求获取文件内容
    response = requests.get(url, stream=True)
    # 检查请求是否成功
    if response.status_code == 200:
        # 创建一个本地文件并写入下载的内容（如果文件已存在，将被覆盖）
        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)
        _safe_print(f"文件已成功下载并保存到：{save_path}")
    else:
        _safe_print(f"下载失败，响应状态码为：{response.status_code}")



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
from dateutil.relativedelta import relativedelta
#"创建日期目录"
def create_directory(base_directory,dtype="day",name=""):
    current_date = datetime.now().strftime("%Y%m%d")
    if dtype =="last_month":
        current_date = f'{(datetime.today() - relativedelta(months=1)).month}月'
    elif dtype =="customize":
        current_date = name
    directory_path = os.path.join(base_directory, current_date)
    # create directory
    try:
        os.makedirs(directory_path, exist_ok=True)
        _safe_print("Directory created successfully.")
    except OSError as e:
        _safe_print(f"Failed to create directory: {e}")
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
        _safe_print(f"第{i}名: '{val_original}' - 出现{cnt}次")
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
        _safe_print(f"第{i}名: '{value}' - 出现{count}次")
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
    _safe_print(f"数据已追加到 {ws.title}! 起始行: {start_row}")

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
    _safe_print(f"Excel 已保存到：{file_path}")
    return file_path

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
def get_dowmlaod_file_list(path=r'C:\Users\Administrator\Downloads',target_count=1,timeout=120,interval=3):
    start_time = time.time()
    excel_extensions = {'.xls', '.xlsx', '.csv'}
    while True:
        files = [f for f in os.listdir(path) if
                 os.path.isfile(os.path.join(path, f)) and os.path.splitext(f)[1].lower() in excel_extensions]
        if len(files) >= target_count:
            file_list = [os.path.join(path, f) for f in files]
            break
        if time.time() - start_time > timeout:
            file_list = []  # 超时未检测到足够文件
            break
        time.sleep(interval)

    return file_list

def list_files_in_dir_recursive(dir_path):
    """
    获取目录及子目录下所有文件的完整路径
    """
    files = []
    for root, dirs, filenames in os.walk(dir_path):
        for f in filenames:
            files.append(os.path.join(root, f))
    return files

def is_convertible_to_float(value):
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False


# 清空目录
def clean_directory(path=r'C:\Users\Administrator\Downloads'):
    os.makedirs(path, exist_ok=True)
    for name in os.listdir(path):
        full_path = os.path.join(path, name)
        (shutil.rmtree if os.path.isdir(full_path) else os.remove)(full_path)
