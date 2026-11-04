"""Preserve and merge TEMU global financial-detail workbooks.

Every store keeps its untouched platform download plus an enriched copy whose
first column on every non-empty sheet is the full Ziniao store name.  The final
workbook groups sheets by their original title and aligns columns by header
name, so stores may return the same sheets in a different column order or with
extra columns.  Source values are copied as-is; text that starts with ``=`` is
always written back as literal text.
"""
from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence
from uuid import uuid4
from xml.etree.ElementTree import iterparse

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter, range_boundaries


SHOP_HEADER = "店铺名"
MAX_DATA_ROWS = 1_048_575
MAX_COLUMNS = 16_384
HEADER_FILL = PatternFill("solid", fgColor="E7E6E6")


class FinancialWorkbookError(ValueError):
    """The downloaded workbook cannot be preserved without losing data."""


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _key(value) -> str:
    return str(value if value is not None else "").strip().casefold()


def _is_shop_header(value) -> bool:
    return _key(value) == _key(SHOP_HEADER)


def _reject_machine_cells(cells: Sequence, where: str) -> None:
    for cell in cells:
        if cell.data_type == "f":
            raise FinancialWorkbookError(f"{where}包含公式，无法确认原始数据")
        if cell.data_type == "e":
            raise FinancialWorkbookError(f"{where}包含错误值，请检查平台导出")


def _labels(cells: Sequence, where: str) -> tuple[str, ...]:
    values = [cell.value for cell in cells]
    used = [index for index, value in enumerate(values) if value is not None]
    if not used:
        return ()
    last = max(used)
    if last + 1 > MAX_COLUMNS:
        raise FinancialWorkbookError("工作表超过 Excel 列数上限")
    labels: list[str] = []
    seen: set[str] = set()
    for index in range(last + 1):
        value = values[index]
        label = str(value).strip() if value is not None else ""
        if not label:
            raise FinancialWorkbookError(f"{where}存在空白表头，无法安全对齐字段")
        key = _key(label)
        if key in seen:
            raise FinancialWorkbookError(f"{where}存在重复表头「{label}」，无法安全对齐字段")
        seen.add(key)
        labels.append(label)
    return tuple(labels)


@dataclass(frozen=True)
class SheetInfo:
    title: str
    headers: tuple[str, ...]
    row_count: int
    header_paths: tuple[tuple[str, ...], ...] = ()

    @property
    def header_depth(self) -> int:
        return max((len(path) for path in self.header_paths), default=1)

    @property
    def is_empty(self) -> bool:
        return not self.headers


@dataclass(frozen=True)
class WorkbookInfo:
    path: Path
    sha256: str
    sheets: tuple[SheetInfo, ...]

    @property
    def row_count(self) -> int:
        return sum(sheet.row_count for sheet in self.sheets)

    @property
    def sheet_order(self) -> tuple[str, ...]:
        return tuple(sheet.title for sheet in self.sheets)

    def sheet_map(self) -> dict[str, SheetInfo]:
        return {sheet.title: sheet for sheet in self.sheets}


def _header_merges(book, sheet):
    """Read merge metadata without loading all financial records into memory."""
    merges = []
    parents = []
    with book._archive.open(sheet._worksheet_path) as stream:
        for event, element in iterparse(stream, events=("start", "end")):
            if event == "start":
                parents.append(element)
                continue
            if element.tag.rsplit("}", 1)[-1] == "mergeCell":
                bounds = range_boundaries(element.attrib["ref"])
                if bounds[1] != 1 or bounds[3] > 2:
                    raise FinancialWorkbookError(f"工作表「{sheet.title}」包含不支持的数据区合并单元格")
                if bounds[2] > MAX_COLUMNS:
                    raise FinancialWorkbookError("工作表超过 Excel 列数上限")
                merges.append(bounds)
            parents.pop()
            element.clear()
            if parents:
                parents[-1].remove(element)
    return merges


def _read_headers(book, sheet, rows):
    """Preserve the observed one-row or merged two-row TEMU header layout."""
    where = f"工作表「{sheet.title}」"
    merges = _header_merges(book, sheet)
    depth = max((bounds[3] for bounds in merges), default=1)
    first_rows = [list(next(rows, ())) for _ in range(depth)]
    for number, cells in enumerate(first_rows, 1):
        _reject_machine_cells(cells, f"{where}第 {number} 行")
    if depth == 1:
        labels = _labels(first_rows[0], where)
        if merges:
            raise FinancialWorkbookError(f"{where}合并表头缺少可唯一识别的子表头")
        return labels, tuple((label,) for label in labels), depth
    width = max(
        [col + 1 for row in first_rows for col, cell in enumerate(row) if cell.value is not None]
        + [bounds[2] for bounds in merges], default=0,
    )
    if width > MAX_COLUMNS:
        raise FinancialWorkbookError("工作表超过 Excel 列数上限")
    grid = [[str(row[col].value).strip() if col < len(row) and row[col].value is not None else ""
             for col in range(width)] for row in first_rows]
    occupied = set()
    for left, top, right, bottom in merges:
        anchor = grid[top - 1][left - 1]
        if not anchor:
            raise FinancialWorkbookError(f"{where}合并表头存在空白标题")
        for row in range(top - 1, bottom):
            for col in range(left - 1, right):
                if (row, col) in occupied:
                    raise FinancialWorkbookError(f"{where}合并表头范围重叠")
                occupied.add((row, col))
                grid[row][col] = anchor
    paths = []
    for col in range(width):
        if not grid[0][col] or not grid[1][col]:
            raise FinancialWorkbookError(f"{where}存在空白表头，无法安全对齐字段")
        paths.append((grid[0][col],) if grid[0][col] == grid[1][col]
                     else (grid[0][col], grid[1][col]))
    labels = tuple("\n".join(path) for path in paths)
    if len({_key(label) for label in labels}) != len(labels):
        raise FinancialWorkbookError(f"{where}存在重复表头，无法安全对齐字段")
    return labels, tuple(paths), depth


def _scan_sheet(book, sheet, *, require_shop_header: bool) -> SheetInfo:
    where = f"工作表「{sheet.title}」"
    sheet.reset_dimensions()
    rows = sheet.iter_rows()
    try:
        headers, paths, depth = _read_headers(book, sheet, rows)
        if require_shop_header and headers and not _is_shop_header(headers[0]):
            raise FinancialWorkbookError(f"{where}第一列不是「{SHOP_HEADER}」")
        row_count = 0
        for row_number, row in enumerate(rows, depth + 1):
            cells = list(row)
            _reject_machine_cells(cells, f"{where}第 {row_number} 行")
            used = [index for index, cell in enumerate(cells) if cell.value is not None]
            if not used:
                continue
            if not headers:
                raise FinancialWorkbookError(f"{where}首行不是表头")
            if any(index >= len(headers) for index in used):
                raise FinancialWorkbookError(f"{where}第 {row_number} 行存在无表头数据列")
            row_count += 1
            if row_count > MAX_DATA_ROWS + 1 - depth:
                raise FinancialWorkbookError("工作表超过 Excel 单表行数上限，请缩小日期范围")
    finally:
        rows.close()
    return SheetInfo(str(sheet.title), headers, row_count, paths)


def inspect_workbook(path: str | Path, *, require_shop_header: bool = False) -> WorkbookInfo:
    path = Path(path).resolve()
    if path.suffix.lower() != ".xlsx" or not path.is_file() or not path.stat().st_size:
        raise FinancialWorkbookError("未获得完整可读的 .xlsx 财务明细文件")
    sha256 = file_sha256(path)
    stream = path.open("rb")
    try:
        book = load_workbook(stream, read_only=True, data_only=False)
    except Exception as exc:  # openpyxl raises several format-specific errors
        stream.close()
        raise FinancialWorkbookError("财务明细 Excel 无法读取，文件可能损坏或下载未完成") from exc
    try:
        if not book.worksheets:
            raise FinancialWorkbookError("财务明细 Excel 没有工作表")
        sheets = tuple(_scan_sheet(book, sheet, require_shop_header=require_shop_header) for sheet in book.worksheets)
    except FinancialWorkbookError:
        raise
    except Exception as exc:
        raise FinancialWorkbookError("读取财务明细 Excel 失败，文件可能损坏") from exc
    finally:
        book.close()
        stream.close()
    if file_sha256(path) != sha256:
        raise FinancialWorkbookError("财务明细文件在读取过程中发生变化")
    return WorkbookInfo(path, sha256, sheets)


@contextmanager
def _stream_sheet(path: Path, title: str, expected_headers: tuple[str, ...]):
    """Yield the validated header and a stream of non-empty data rows."""
    stream = path.open("rb")
    book = None
    rows = None
    data = None
    try:
        book = load_workbook(stream, read_only=True, data_only=False)
        if title not in book.sheetnames:
            raise FinancialWorkbookError(f"工作表「{title}」在汇总前消失")
        sheet = book[title]
        sheet.reset_dimensions()
        rows = sheet.iter_rows()
        headers, _, depth = _read_headers(book, sheet, rows)
        if tuple(headers) != tuple(expected_headers):
            raise FinancialWorkbookError(f"工作表「{title}」表头在汇总前发生变化")

        def data_rows() -> Iterator[list]:
            for row_number, row in enumerate(rows, depth + 1):
                cells = list(row)
                _reject_machine_cells(cells, f"工作表「{title}」第 {row_number} 行")
                used = [index for index, cell in enumerate(cells) if cell.value is not None]
                if not used:
                    continue
                if any(index >= len(headers) for index in used):
                    raise FinancialWorkbookError(f"工作表「{title}」第 {row_number} 行存在无表头数据列")
                yield cells

        data = data_rows()
        yield headers, data
    finally:
        if data is not None:
            data.close()
        if rows is not None:
            rows.close()
        if book is not None:
            book.close()
        stream.close()


def _write_cell(sheet, value, number_format: str | None = None) -> WriteOnlyCell:
    cell = WriteOnlyCell(sheet, value)
    # Platform text is untrusted; "=" must stay text, never become a formula.
    if isinstance(value, str):
        cell.data_type = "s"
    if number_format and number_format != "General":
        cell.number_format = number_format
    return cell


def _append_header(sheet, labels: Sequence[str]) -> None:
    cells = []
    for label in labels:
        cell = _write_cell(sheet, label)
        cell.font = Font(name="Microsoft YaHei", size=10, bold=True)
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cells.append(cell)
    sheet.append(cells)


def _append_headers(sheet, paths: Sequence[tuple[str, ...]]) -> None:
    depth = max((len(path) for path in paths), default=1)
    top = [path[0] if not (index and len(path) == 2 and len(paths[index - 1]) == 2
                           and paths[index - 1][0] == path[0]) else None
           for index, path in enumerate(paths)]
    _append_header(sheet, top)
    if depth == 1:
        return
    _append_header(sheet, [path[1] if len(path) == 2 else None for path in paths])
    col = 0
    while col < len(paths):
        path = paths[col]
        letter = get_column_letter(col + 1)
        if len(path) == 1:
            sheet.merged_cells.add(f"{letter}1:{letter}2")
            col += 1
            continue
        end = col + 1
        while end < len(paths) and len(paths[end]) == 2 and paths[end][0] == path[0]:
            end += 1
        if end > col + 1:
            sheet.merged_cells.add(f"{letter}1:{get_column_letter(end)}1")
        col = end


def _prepare_sheet(sheet, labels: Sequence[str], *, depth=1) -> None:
    if len(labels) > MAX_COLUMNS:
        raise FinancialWorkbookError("工作表超过 Excel 列数上限")
    sheet.freeze_panes = f"B{depth + 1}"
    for index, label in enumerate(labels, 1):
        sheet.column_dimensions[get_column_letter(index)].width = 40 if _is_shop_header(label) else 18
    for row in range(1, depth + 1):
        sheet.row_dimensions[row].height = 28


def _close_writer(book):
    for sheet in book.worksheets:
        if not sheet.closed:
            sheet.close()
        if sheet._writer is not None:
            try:
                sheet._writer.cleanup()
            except FileNotFoundError:
                pass
    book.close()


def build_header_mapping(header_sets: Iterable[Mapping[str, Sequence[str]]]) -> dict[str, dict[str, str]]:
    """Rename an existing source ``店铺名`` column without ambiguity.

    The replacement name is chosen against every source header of that sheet
    across the whole batch, so two stores can never assign different original
    fields to the same merged column.
    """
    union: dict[str, list[str]] = {}
    order: list[str] = []
    for headers_by_sheet in header_sets:
        for title, headers in headers_by_sheet.items():
            key = str(title)
            if key not in union:
                union[key] = []
                order.append(key)
            known = {_key(item) for item in union[key]}
            for header in headers:
                if _key(header) not in known:
                    union[key].append(str(header))
                    known.add(_key(header))
    mapping: dict[str, dict[str, str]] = {}
    shop_key = _key(SHOP_HEADER)
    for title in order:
        keys = {_key(item) for item in union[title]}
        if shop_key not in keys:
            mapping[title] = {}
            continue
        suffix = 1
        target = f"{SHOP_HEADER}（原表）"
        while _key(target) in keys:
            suffix += 1
            target = f"{SHOP_HEADER}（原表{suffix}）"
        mapping[title] = {shop_key: target}
    return mapping


def enrich_store(
    source: str | Path,
    destination: str | Path,
    store_name: str,
    header_mapping: Mapping[str, Mapping[str, str]],
) -> dict:
    """Write a separate workbook with ``店铺名`` first on every non-empty sheet."""
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    store = str(store_name or "").strip()
    if not store:
        raise FinancialWorkbookError("缺少完整紫鸟店铺名，无法生成逐店工作簿")
    if source == destination:
        raise FinancialWorkbookError("输出路径不能覆盖原始财务明细")
    if source.suffix.lower() != ".xlsx" or destination.suffix.lower() != ".xlsx":
        raise FinancialWorkbookError("逐店财务明细仅支持 .xlsx")
    if destination.exists():
        raise FinancialWorkbookError("逐店工作簿已存在，拒绝覆盖已有结果")
    info = inspect_workbook(source)
    expected_headers: dict[str, tuple[str, ...]] = {}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{uuid4().hex}.xlsx")
    try:
        book = Workbook(write_only=True)
        try:
            for sheet_info in info.sheets:
                out_sheet = book.create_sheet(sheet_info.title)
                mapping = header_mapping.get(sheet_info.title, {})
                if not sheet_info.headers:
                    expected_headers[sheet_info.title] = ()
                    continue
                headers = (SHOP_HEADER, *(mapping.get(_key(name), name) for name in sheet_info.headers))
                expected_headers[sheet_info.title] = headers
                paths = ((SHOP_HEADER,), *( (mapping[_key(name)],) if _key(name) in mapping else path
                                           for name, path in zip(sheet_info.headers, sheet_info.header_paths)))
                _prepare_sheet(out_sheet, headers, depth=sheet_info.header_depth)
                _append_headers(out_sheet, paths)
                count = 0
                if sheet_info.row_count:
                    with _stream_sheet(source, sheet_info.title, sheet_info.headers) as (source_headers, rows):
                        positions = {_key(name): index for index, name in enumerate(source_headers)}
                        for cells in rows:
                            cells_by_key = {
                                _key(name): cells[positions[_key(name)]]
                                for name in source_headers
                                if positions[_key(name)] < len(cells)
                            }
                            output = [_write_cell(out_sheet, store)]
                            for name in sheet_info.headers:
                                cell = cells_by_key.get(_key(name))
                                output.append(_write_cell(out_sheet, cell.value if cell else None,
                                                          cell.number_format if cell else None))
                            out_sheet.append(output)
                            count += 1
                if count != sheet_info.row_count:
                    raise FinancialWorkbookError(f"工作表「{sheet_info.title}」逐店行数不一致")
            book.save(temporary)
        finally:
            _close_writer(book)

        check = inspect_workbook(temporary, require_shop_header=True)
        if tuple(sheet.title for sheet in check.sheets) != info.sheet_order:
            raise FinancialWorkbookError("逐店工作簿工作表顺序或名称发生变化")
        if check.row_count != info.row_count:
            raise FinancialWorkbookError("逐店工作簿总行数与原始文件不一致")
        for source_sheet, output_sheet in zip(info.sheets, check.sheets):
            if output_sheet.row_count != source_sheet.row_count:
                raise FinancialWorkbookError(f"工作表「{source_sheet.title}」逐店行数不一致")
            if source_sheet.headers:
                if tuple(output_sheet.headers) != expected_headers[source_sheet.title]:
                    raise FinancialWorkbookError(f"工作表「{source_sheet.title}」逐店表头不一致")
            elif output_sheet.headers:
                raise FinancialWorkbookError(f"空工作表「{source_sheet.title}」被写入了表头")
        if file_sha256(source) != info.sha256:
            raise FinancialWorkbookError("原始财务明细在生成逐店工作簿时发生变化")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "row_count": check.row_count,
        "sheet_count": len(check.sheets),
        "sheets": {
            sheet.title: {"headers": list(sheet.headers), "row_count": sheet.row_count}
            for sheet in check.sheets
        },
    }


def _confined(root: Path, value: str | Path) -> Path:
    path = Path(value)
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not resolved.is_relative_to((root / "results").resolve()):
        raise FinancialWorkbookError("汇总来源文件超出本次导出目录")
    if not resolved.is_file():
        raise FinancialWorkbookError("汇总来源文件不存在")
    return resolved


def merge_store_workbooks(
    root: str | Path,
    entries: Sequence[Mapping],
    destination: str | Path,
    *,
    incomplete: bool,
) -> dict:
    """Merge enriched store workbooks grouped by original sheet name."""
    root = Path(root).resolve()
    destination = Path(destination).resolve()
    if destination.exists():
        raise FinancialWorkbookError("合并总表已存在，请创建新批次，避免覆盖已有结果")
    if not destination.is_relative_to((root / "results").resolve()):
        raise FinancialWorkbookError("合并总表路径超出本次导出目录")
    if not entries:
        raise FinancialWorkbookError("没有成功店铺的工作簿可合并")

    infos: list[tuple[Mapping, WorkbookInfo]] = []
    for entry in entries:
        source = _confined(root, entry["file"])
        info = inspect_workbook(source, require_shop_header=True)
        if info.row_count != entry.get("row_count"):
            raise FinancialWorkbookError(f"{entry.get('store_name')} 的逐店工作簿行数与清单不一致")
        infos.append((entry, info))

    labels_by_sheet: dict[str, list[str]] = {}
    positions_by_sheet: dict[str, dict[str, int]] = {}
    paths_by_sheet: dict[str, list[tuple[str, ...]]] = {}
    sheet_order: list[str] = []
    for _, info in infos:
        for sheet in info.sheets:
            if sheet.title not in labels_by_sheet:
                labels_by_sheet[sheet.title] = []
                positions_by_sheet[sheet.title] = {}
                paths_by_sheet[sheet.title] = []
                sheet_order.append(sheet.title)
            if not sheet.headers:
                continue
            if not _is_shop_header(sheet.headers[0]):
                raise FinancialWorkbookError(f"工作表「{sheet.title}」第一列不是「{SHOP_HEADER}」")
            labels = labels_by_sheet[sheet.title]
            positions = positions_by_sheet[sheet.title]
            for header, path in zip(sheet.headers, sheet.header_paths):
                key = _key(header)
                if key not in positions:
                    positions[key] = len(labels)
                    labels.append(header)
                    paths_by_sheet[sheet.title].append(path)
                elif paths_by_sheet[sheet.title][positions[key]] != path:
                    raise FinancialWorkbookError(f"工作表「{sheet.title}」同名字段的表头层级不一致")
            if len(labels) > MAX_COLUMNS:
                raise FinancialWorkbookError("合并总表超过 Excel 列数上限")

    counts = {title: 0 for title in sheet_order}
    for _, info in infos:
        for sheet in info.sheets:
            counts[sheet.title] += sheet.row_count
            depth = max((len(path) for path in paths_by_sheet[sheet.title]), default=1)
            if counts[sheet.title] > MAX_DATA_ROWS + 1 - depth:
                raise FinancialWorkbookError("合并总表超过 Excel 单表行数上限，请缩小日期或店铺范围")

    temporary = destination.with_name(f".{uuid4().hex}.xlsx")
    book = Workbook(write_only=True)
    try:
        for title in sheet_order:
            out_sheet = book.create_sheet(title)
            labels = labels_by_sheet[title]
            if not labels:
                continue
            paths = paths_by_sheet[title]
            depth = max((len(path) for path in paths), default=1)
            _prepare_sheet(out_sheet, labels, depth=depth)
            _append_headers(out_sheet, paths)
            total = 0
            for entry, info in infos:
                sheet = info.sheet_map().get(title)
                if not sheet or not sheet.headers:
                    continue
                source = _confined(root, entry["file"])
                with _stream_sheet(source, title, sheet.headers) as (source_headers, rows):
                    positions = {_key(name): index for index, name in enumerate(source_headers)}
                    if not _is_shop_header(source_headers[0]):
                        raise FinancialWorkbookError(f"工作表「{title}」第一列不是「{SHOP_HEADER}」")
                    store_name = str(entry.get("store_name") or "").strip()
                    count = 0
                    for cells in rows:
                        shop_cell = cells[0] if cells else None
                        if str(shop_cell.value if shop_cell else "").strip() != store_name:
                            raise FinancialWorkbookError("逐店工作簿店铺名与本次清单不一致")
                        output = []
                        for label in labels:
                            index = positions.get(_key(label))
                            cell = cells[index] if index is not None and index < len(cells) else None
                            output.append(_write_cell(out_sheet, cell.value if cell else None,
                                                      cell.number_format if cell else None))
                        out_sheet.append(output)
                        count += 1
                    if count != sheet.row_count:
                        raise FinancialWorkbookError(f"工作表「{title}」实际行数与逐店清单不一致")
                    total += count
            if total != counts[title]:
                raise FinancialWorkbookError(f"工作表「{title}」合并行数不一致")
            if depth == 1:
                out_sheet.auto_filter.ref = f"A1:{get_column_letter(len(labels))}{total + 1}"
        book.save(temporary)

        check = inspect_workbook(temporary, require_shop_header=True)
        if check.sheet_order != tuple(sheet_order):
            raise FinancialWorkbookError("合并总表工作表顺序或名称校验失败")
        for sheet in check.sheets:
            if sheet.headers != tuple(labels_by_sheet[sheet.title]):
                raise FinancialWorkbookError(f"合并总表工作表「{sheet.title}」表头校验失败")
            if sheet.row_count != counts[sheet.title]:
                raise FinancialWorkbookError(f"合并总表工作表「{sheet.title}」行数校验失败")
        os.replace(temporary, destination)
    finally:
        _close_writer(book)
        temporary.unlink(missing_ok=True)

    return {
        "status": "partial" if incomplete else "success",
        "file": str(destination.relative_to(root)),
        "row_counts": counts,
        "row_count": sum(counts.values()),
        "sheet_count": len(sheet_order),
        "sheet_names": sheet_order,
        "store_count": len(infos),
    }
