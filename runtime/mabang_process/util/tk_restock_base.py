"""TK 备货纯业务处理：新旧库存兼容、销量筛选与工作簿契约。"""
import os
import sys
import re
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

config_path = os.path.dirname(os.path.dirname(__file__))
if config_path not in sys.path:
    sys.path.append(config_path)


from philippines.hqyl_runtime import RuntimeConfigurationError
from util.mabang_inventory import normalize_inventory_sales


SALES_COLUMN_ALIASES = {
    "SKU": ("SKU", "库存SKU", "库存SKU编号"),
    "销售数量": ("销售数量", "销售量", "销量"),
}

INVENTORY_COLUMN_ALIASES = {
    "库存SKU": ("库存SKU", "库存SKU编号"),
    "库存SKU中文名": ("库存SKU中文名", "中文名称"),
    "仓库名称": ("仓库名称", "仓库"),
    "仓库可用库存量": ("仓库可用库存量", "可用库存量"),
    "仓库总在途量": ("仓库总在途量", "在途量"),
    "销量(7/28/42)": ("销量(7/28/42)",),
}


def _rename_alias_columns(df, aliases):
    """按业务标准名兼容马帮新旧表头，并返回重命名后的副本。"""
    normalized_columns = {str(column).strip(): column for column in df.columns}
    rename_map = {}
    missing = []
    for canonical_name, candidates in aliases.items():
        source_name = next(
            (normalized_columns[candidate] for candidate in candidates if candidate in normalized_columns),
            None,
        )
        if source_name is None:
            missing.append(canonical_name)
        elif source_name != canonical_name:
            rename_map[source_name] = canonical_name
    if missing:
        raise ValueError(f"数据缺少必需列: {', '.join(missing)}；实际表头: {', '.join(normalized_columns)}")
    return df.rename(columns=rename_map).copy()


def build_date_ranges(run_time, week_days=7, month_days=28):
    """
    计算近 N 天的开始日期和结束日期（截至昨日完整自然日）。
    """
    end_date = run_time - timedelta(days=1)
    weekly_start = run_time - timedelta(days=week_days)
    monthly_start = run_time - timedelta(days=month_days)
    return {
        "weekly": {
            "start": weekly_start.strftime("%Y-%m-%d"),
            "end": end_date.strftime("%Y-%m-%d"),
        },
        "monthly": {
            "start": monthly_start.strftime("%Y-%m-%d"),
            "end": end_date.strftime("%Y-%m-%d"),
        },
    }


def normalize_sku(value):
    """
    SKU 归一化：
    1. 空值转为空字符串并清理首尾空白。
    2. 纯数字 SKU 删除全部空白。
    3. 非纯数字 SKU 只清理首尾空白。
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip()
    if not s:
        return ""
    no_space = re.sub(r"\s+", "", s)
    if no_space.isdigit():
        return no_space
    return s


def clean_sales_dataframe(df):
    """
    清洗销量 DataFrame:
    1. 校验至少存在 SKU 和 销售数量 两列。
    2. SKU 归一化。
    3. 销售数量 转数值，非法值记 0。
    4. 保留 SKU 非空 且 销售数量 >= 1。
    """
    try:
        df = _rename_alias_columns(df, SALES_COLUMN_ALIASES)
    except ValueError as exc:
        raise ValueError(f"销量{exc}") from exc
    df["SKU"] = df["SKU"].apply(normalize_sku)

    original_qty = df["销售数量"]
    parsed_qty = pd.to_numeric(original_qty, errors="coerce")
    invalid_mask = parsed_qty.isna() & original_qty.notna() & (original_qty.astype(str).str.strip() != "")
    invalid_qty_count = int(invalid_mask.sum())
    df["销售数量"] = parsed_qty.fillna(0)

    mask = (df["SKU"] != "") & (df["销售数量"] >= 1)
    cleaned = df[mask].copy()

    return cleaned, invalid_qty_count


def _read_excel_with_text_skus(file_path: Path) -> pd.DataFrame:
    with pd.ExcelFile(file_path) as workbook:
        # Check original headers before pandas adds suffixes to duplicate names.
        header = pd.read_excel(workbook, header=None, nrows=1)
        if header.empty:
            raise RuntimeConfigurationError(f"{file_path}: 数据缺少表头")
        columns = pd.Index([str(value).strip() for value in header.iloc[0]])
        duplicates = columns[columns.duplicated()].tolist()
        if duplicates:
            raise RuntimeConfigurationError(f"{file_path}: 数据存在重复列: {duplicates}")
        sku_dtypes = {
            original: str
            for original, normalized in zip(header.iloc[0], columns)
            if normalized in SALES_COLUMN_ALIASES["SKU"]
        }
        # Keep SKU identity while retaining numeric types in other report fields.
        frame = pd.read_excel(
            workbook, dtype=sku_dtypes, keep_default_na=False, na_values=[""],
        )
    frame.columns = [str(column).strip() for column in frame.columns]
    return frame


def load_and_filter_inventory(inventory_files, weekly_skus, required_columns):
    """
    加载全部仓库库存文件, 归一化 SKU, 筛选近 7 天 SKU, 并按指定字段输出。
    """
    all_dfs = []
    warehouse_count = 0

    for file_path in inventory_files:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"仓库库存文件不存在: {file_path}")
        df = _read_excel_with_text_skus(Path(file_path))
        try:
            df = normalize_inventory_sales(df, source=str(file_path))
            df = _rename_alias_columns(df, INVENTORY_COLUMN_ALIASES)
        except ValueError as exc:
            raise ValueError(f"仓库库存文件 {file_path} {exc}") from exc

        df = df[required_columns].copy()
        warehouse_count += 1
        if df.empty:
            continue
        df["_normalized_sku"] = df["库存SKU"].apply(normalize_sku)
        all_dfs.append(df)

    if not all_dfs:
        return pd.DataFrame(columns=required_columns), 0, warehouse_count

    combined = pd.concat(all_dfs, ignore_index=True)
    combined = combined[combined["_normalized_sku"].isin(weekly_skus)].copy()

    before_dedup = len(combined)
    combined = combined.drop_duplicates(subset=["_normalized_sku", "仓库名称"], keep="first")
    duplicate_count = before_dedup - len(combined)

    numeric_cols = ["仓库可用库存量", "仓库总在途量"]
    for col in numeric_cols:
        if col in combined.columns:
            combined[col] = pd.to_numeric(combined[col], errors="coerce").fillna(0)

    combined = combined.drop(columns=["_normalized_sku"])
    return combined, duplicate_count, warehouse_count


def get_stock_label_names(process_config):
    """读取需要导出的库存自定义分类配置。"""
    configured_names = process_config.get("stock_labels", [])
    if not isinstance(configured_names, list) or not configured_names:
        raise ValueError("stock_labels 必须是非空列表")
    return [str(name).strip() for name in configured_names]


def load_stock_label_dataframe(file_path):
    """读取库存自定义分类导出文件。"""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"库存自定义分类导出文件不存在: {file_path}")

    label_df = pd.read_excel(file_path, dtype=str)
    label_df.columns = [str(column).strip() for column in label_df.columns]
    if "库存SKU" not in label_df.columns:
        raise ValueError(
            f"库存自定义分类导出文件 {file_path} 缺少必需列: 库存SKU；"
            f"实际表头: {', '.join(label_df.columns)}"
        )

    result = label_df[["库存SKU"]].copy()
    result["库存SKU"] = result["库存SKU"].apply(normalize_sku)
    return result[result["库存SKU"] != ""].reset_index(drop=True)


def get_core_sheet_names(country):
    """根据国家动态生成前3个核心 Sheet 名称。"""
    return (
        f"TK{country}近一周销量",
        f"TK{country}近一月销量",
        f"TK{country}近一周销量sku库存情况",
    )


def write_output_workbook(
    country,
    weekly_df,
    monthly_df,
    inventory_df,
    output_file,
    stock_label_dfs,
    stock_label_names,
):
    """
    将销量、库存及库存自定义分类 DataFrame 写入 Excel 工作簿。
    先写入临时文件，校验通过后原子替换。
    """
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    core_sheet_names = get_core_sheet_names(country)

    sheets = {
        core_sheet_names[0]: weekly_df.copy(),
        core_sheet_names[1]: monthly_df.copy(),
        core_sheet_names[2]: inventory_df.copy(),
        **{
            sheet_name: stock_label_dfs[sheet_name].copy()
            for sheet_name in stock_label_names
        },
    }

    for df in sheets.values():
        for col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].fillna("").astype(str)
                df[col] = df[col].replace({"nan": "", "inf": "", "-inf": "", "None": ""})

    tmp_fd, tmp_path = tempfile.mkstemp(
        suffix=".xlsx",
        dir=str(output_path.parent),
    )
    os.close(tmp_fd)

    try:
        with pd.ExcelWriter(tmp_path, engine="openpyxl") as writer:
            for sheet_name, df in sheets.items():
                df.to_excel(writer, index=False, sheet_name=sheet_name)

        _format_workbook(tmp_path)
        validate_output_workbook(country, tmp_path, stock_label_names)
        os.replace(tmp_path, str(output_path))
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def _format_workbook(file_path):
    """美化工作簿: 冻结首行, 开启筛选, 表头加粗并上色, 合理列宽。"""
    wb = load_workbook(file_path)
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font_color = Font(bold=True, color="FFFFFF")
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for ws in wb.worksheets:
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

        for cell in ws[1]:
            cell.font = header_font_color
            cell.fill = header_fill
            cell.alignment = center_align

        for col in ws.columns:
            max_len = 0
            col_letter = col[0].column_letter
            for cell in col:
                try:
                    val = str(cell.value) if cell.value is not None else ""
                    char_len = sum(2 if ord(c) > 127 else 1 for c in val)
                    if char_len > max_len:
                        max_len = char_len
                except Exception:
                    pass
            ws.column_dimensions[col_letter].width = min(max(max_len + 4, 10), 50)

    wb.save(file_path)
    wb.close()


def validate_output_workbook(country, file_path, stock_label_names):
    """校验输出工作簿结构、表头及数据约束。"""
    core_sheet_names = get_core_sheet_names(country)
    expected_sheets = [*core_sheet_names, *stock_label_names]

    wb = load_workbook(file_path, read_only=True, data_only=True)
    try:
        actual_sheets = wb.sheetnames
        if actual_sheets != expected_sheets:
            raise ValueError(
                f"工作表名称或顺序不匹配。期望: {expected_sheets}, 实际: {actual_sheets}"
            )

        for sheet_name in stock_label_names:
            header = next(
                wb[sheet_name].iter_rows(min_row=1, max_row=1, values_only=True),
                (),
            )
            if "库存SKU" not in header:
                raise ValueError(f"工作表 [{sheet_name}] 缺少必需列: 库存SKU")

        for sheet_name in [core_sheet_names[0], core_sheet_names[1]]:
            ws = wb[sheet_name]
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            header = list(rows[0])
            if "销售数量" in header:
                qty_idx = header.index("销售数量")
                for row_idx, row in enumerate(rows[1:], start=2):
                    val = row[qty_idx]
                    if val is not None:
                        try:
                            float_val = float(val)
                        except (TypeError, ValueError):
                            continue
                        if float_val < 1:
                            raise ValueError(
                                f"工作表 [{sheet_name}] 第 {row_idx} 行存在销售数量 < 1 的记录: {val}"
                            )

        weekly_ws = wb[core_sheet_names[0]]
        weekly_rows = list(weekly_ws.iter_rows(values_only=True))
        weekly_skus = set()
        if weekly_rows:
            w_header = list(weekly_rows[0])
            if "SKU" in w_header:
                sku_idx = w_header.index("SKU")
                for row in weekly_rows[1:]:
                    sku_val = normalize_sku(row[sku_idx])
                    if sku_val:
                        weekly_skus.add(sku_val)

        inv_ws = wb[core_sheet_names[2]]
        inv_rows = list(inv_ws.iter_rows(values_only=True))
        if inv_rows and weekly_skus:
            inv_header = list(inv_rows[0])
            if "库存SKU" in inv_header:
                inv_sku_idx = inv_header.index("库存SKU")
                for row_idx, row in enumerate(inv_rows[1:], start=2):
                    inv_sku = normalize_sku(row[inv_sku_idx])
                    if inv_sku and inv_sku not in weekly_skus:
                        raise ValueError(
                            f"库存表第 {row_idx} 行 SKU [{inv_sku}] 不在近 7 天唯一 SKU 集合中"
                        )
    finally:
        wb.close()


def validate_config(process_config):
    """Validate public business settings; delivery credentials are separate."""
    if not str((process_config.get("sales_label") or {}).get("label_id") or "").strip():
        raise ValueError("配置缺少 sales_label.label_id")
    periods = process_config.get("periods") or {}
    for key in ("week_days", "month_days"):
        if not isinstance(periods.get(key), int) or isinstance(periods.get(key), bool) or periods[key] <= 0:
            raise ValueError(f"periods.{key} 必须是正整数")
    names = get_stock_label_names(process_config)
    if any(not name for name in names) or len(set(names)) != len(names):
        raise ValueError("stock_labels 包含空值或重复值")
    return names
