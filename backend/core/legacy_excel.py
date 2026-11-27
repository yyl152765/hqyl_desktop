"""Read legacy Excel values and basic formatting into an openpyxl workbook."""
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

XLS_WARNING = ".xls 输出会转换为 .xlsx，仅保留单元格值和基本格式；公式采用缓存值。需要完整保留公式和格式时，请先用 Excel 另存为 .xlsx。"


def read_legacy_workbook(source: Path, *, max_dimensions: tuple[int, int] | None = None) -> Workbook:
    """Explicit, value-based legacy conversion; callers surface XLS_WARNING."""
    import xlrd

    original = xlrd.open_workbook(str(source), formatting_info=True)
    book = Workbook()
    book.remove(book.active)
    try:
        for old_sheet in original.sheets():
            if max_dimensions and (old_sheet.nrows > max_dimensions[0] or old_sheet.ncols > max_dimensions[1]):
                raise ValueError(f"工作表「{old_sheet.name}」超过 {max_dimensions[0]:,} 行或 {max_dimensions[1]} 列")
            sheet = book.create_sheet(old_sheet.name)
            if old_sheet.visibility:
                sheet.sheet_state = "hidden" if old_sheet.visibility == 1 else "veryHidden"
            for row_index in range(old_sheet.nrows):
                for col_index in range(old_sheet.ncols):
                    old_cell = old_sheet.cell(row_index, col_index)
                    value = old_cell.value
                    if old_cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
                        value = None
                    elif old_cell.ctype == xlrd.XL_CELL_DATE:
                        value = xlrd.xldate.xldate_as_datetime(value, original.datemode)
                    elif old_cell.ctype == xlrd.XL_CELL_BOOLEAN:
                        value = bool(value)
                    elif old_cell.ctype == xlrd.XL_CELL_ERROR:
                        value = xlrd.error_text_from_code.get(value, "#VALUE!")
                    cell = sheet.cell(row_index + 1, col_index + 1, value)
                    if old_cell.ctype == xlrd.XL_CELL_TEXT:
                        cell.data_type = "s"
                    # Minimal BIFF files can omit style records.
                    if old_cell.xf_index is None or not 0 <= old_cell.xf_index < len(original.xf_list):
                        continue
                    xf = original.xf_list[old_cell.xf_index]
                    number_format = original.format_map.get(xf.format_key)
                    if number_format:
                        cell.number_format = number_format.format_str
                    if not 0 <= xf.font_index < len(original.font_list):
                        continue
                    old_font = original.font_list[xf.font_index]
                    cell.font = Font(name=old_font.name, size=old_font.height / 20,
                                     bold=bool(old_font.bold), italic=bool(old_font.italic))
            for index, info in old_sheet.colinfo_map.items():
                sheet.column_dimensions[get_column_letter(index + 1)].width = info.width / 256
                sheet.column_dimensions[get_column_letter(index + 1)].hidden = bool(info.hidden)
            for index, info in old_sheet.rowinfo_map.items():
                sheet.row_dimensions[index + 1].height = info.height / 20
                sheet.row_dimensions[index + 1].hidden = bool(info.hidden)
            for low_row, high_row, low_col, high_col in old_sheet.merged_cells:
                sheet.merge_cells(start_row=low_row + 1, end_row=high_row,
                                  start_column=low_col + 1, end_column=high_col)
    finally:
        original.release_resources()
    return book
