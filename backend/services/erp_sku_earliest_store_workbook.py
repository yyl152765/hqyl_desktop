# ruff: noqa: DTZ007
"""Excel input/output for the ERP earliest-store query.

The source workbook is never modified: a new uniquely named ``.xlsx`` is written,
generated columns/sheets carry an ownership marker, and everything that holds an
identifier is written as text (no formula injection, no long-number precision loss).
"""
from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from backend.services import bigseller_claim_workbook as claim

try:
    from backend.core.legacy_excel import XLS_WARNING
    from backend.core.legacy_excel import read_legacy_workbook as _read_xls
except ImportError:
    from backend.services.bigseller_benchmark_workbook import XLS_WARNING, _read_xls
from backend.services.erp_sku_earliest_store_checkpoint import empty_row

MAX_SKUS = 10_000
HEADER_SEARCH_ROWS = 10
SKU_HEADERS = ("库存SKU编号", "库存SKU", "SKU")  # priority order
PREFERRED_SHEET = "动销总表"
GENERATED_AUTHOR = "HQYL ERP SKU earliest store"
GENERATED_MARKER = "HQYL_ERP_SKU_EARLIEST_V1"
OUTPUT_HEADERS = {
    "shop": "创建时间第一店铺",
    "time": "最早平台创建时间",
    "status": "创建时间对标状态",
    "note": "创建时间对标说明",
}
COLUMN_WIDTHS = {"shop": 30, "time": 24, "status": 20, "note": 60}
STATUS_LABELS = {"matched": "已匹配", "not_found": "ERP留存范围未找到", "incomplete": "数据待核验",
                 "failed": "查询失败", "not_run": "未执行"}
DETAIL_TITLE = "ERP创建时间对标明细"
SUMMARY_TITLE = "ERP创建时间对标汇总"
DETAIL_HEADERS = (
    "来源工作表", "来源行号", "输入SKU", "对标状态", "匹配SKU", "匹配来源", "Shop ID", "店名", "站点", "Item ID",
    "Model ID", "平台创建时间", "商品状态", "商品归档", "变体归档", "匹配商品数", "匹配店铺数", "并列店铺数",
    "并列店铺", "数据问题数", "基础采集时间", "变体采集时间", "查询时间", "说明",
)
DETAIL_WIDTHS = (20, 10, 28, 18, 28, 18, 16, 28, 8, 18, 18, 22, 16, 10, 10, 10, 10, 10, 40, 10, 22, 22, 22, 60)
TEXT_DETAIL_COLUMNS = {3, 5, 7, 8, 10, 11, 19}
TIME_DETAIL_COLUMNS = {12, 21, 22, 23}
DATE_FORMAT = "yyyy-mm-dd hh:mm:ss"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class SkuInput:
    source_path: Path | None
    sheet_name: str
    header_row: int
    sku_column: int
    skus: tuple[str, ...]
    row_numbers: dict[str, tuple[int, ...]]
    row_skus: dict[int, tuple[str, ...]]
    source_sha256: str = ""
    warnings: tuple[str, ...] = ()
    skipped_rows: tuple[dict[str, Any], ...] = ()
    input_kind: str = "file"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return len({number for numbers in self.row_numbers.values() for number in numbers})

    @property
    def fingerprint(self) -> str:
        return self.source_sha256


# ---------------------------------------------------------------- input


def _source_path(path: str | Path) -> Path:
    source = Path(path).expanduser().resolve()
    if source.suffix.lower() not in {".xlsx", ".xls"}:
        raise ValueError("仅支持 .xlsx 或 .xls；请用 Excel 或 WPS 另存为 .xlsx")
    if not source.is_file():
        raise ValueError("请选择存在的 Excel 文件")
    return source


def _open_book(source: Path, *, data_only: bool = False):
    if source.suffix.lower() == ".xls":
        try:
            return _read_xls(source)
        except Exception as exc:
            raise ValueError("无法读取 .xls，请确认文件未损坏、未加密，或另存为 .xlsx") from exc
    return claim._open_book(source, data_only=data_only)


def _find_header(sheet: Any) -> tuple[int, int]:
    candidates = [
        (SKU_HEADERS.index(claim._header_text(cell.value)), cell.row, cell.column)
        for row in sheet.iter_rows(max_row=min(sheet.max_row, HEADER_SEARCH_ROWS))
        for cell in row if claim._header_text(cell.value) in SKU_HEADERS
    ]
    if not candidates:
        raise ValueError(f"前 {HEADER_SEARCH_ROWS} 行未找到“库存SKU编号”“库存SKU”或“SKU”表头")
    _, row, column = min(candidates)
    return row, column


def _is_generated_sheet(sheet: Any) -> bool:
    comment = sheet["A1"].comment
    return bool(comment and comment.author == GENERATED_AUTHOR and comment.text.startswith(GENERATED_MARKER + "\n"))


def _marker(kind: str, sheet_name: str, key: str = "") -> str:
    return f"{GENERATED_MARKER}\n{kind}\n{sheet_name}\n{key}"


def _owned_comment(comment: Any, *, kind: str, sheet_name: str, key: str = "") -> bool:
    return bool(comment and comment.author == GENERATED_AUTHOR and comment.text == _marker(kind, sheet_name, key))


def _sheet_input(source: Path, sheet: Any, cached_sheet: Any, fingerprint: str = "") -> SkuInput:
    header_row, sku_column = _find_header(sheet)
    row_numbers: dict[str, list[int]] = {}
    row_skus: dict[int, tuple[str, ...]] = {}
    skipped: list[dict[str, Any]] = []
    for cells in sheet.iter_rows(min_row=header_row + 1, min_col=sku_column, max_col=sku_column):
        cell = cells[0]
        try:
            text = claim._cell_text(cell, cached_sheet.cell(cell.row, sku_column))
        except (TypeError, ValueError) as exc:
            skipped.append({"row_number": cell.row, "value": str(cell.value or ""), "kind": "invalid", "reason": str(exc)})
            continue
        tokens = list(dict.fromkeys(part.strip() for part in text.splitlines() if part.strip()))
        valid = []
        for token in tokens:
            reason = claim._skip_reason(token)
            if reason:
                skipped.append({"row_number": cell.row, "value": token, "kind": reason[0], "reason": reason[1]})
                continue
            valid.append(token)
            row_numbers.setdefault(token, []).append(cell.row)
            if len(row_numbers) > MAX_SKUS:
                raise ValueError(f"不同 SKU 超过 {MAX_SKUS:,} 个，请分批导入")
        if valid:
            row_skus[cell.row] = tuple(valid)
    if not row_numbers:
        raise ValueError("所选工作表没有可查询的 SKU，请检查表头、批次标题或异常单元格")
    warnings = []
    if source.suffix.lower() == ".xls":
        warnings.append(XLS_WARNING)
    invalid = sum(item["kind"] == "invalid" for item in skipped)
    if invalid:
        warnings.append(f"有 {invalid} 条异常内容未查询，导出后请查看说明和明细")
    multi = sum(len(skus) > 1 for skus in row_skus.values())
    if multi:
        warnings.append(f"有 {multi} 个单元格包含多个换行 SKU，将分别查询并按 SKU 标注结果")
    spaced = sum(bool(re.search(r"\s", sku)) for sku in row_numbers)
    if spaced:
        warnings.append(f"有 {spaced} 个 SKU 含空格，将按完整文本精确匹配，请核对原表")
    return SkuInput(
        source_path=source, sheet_name=sheet.title, header_row=header_row, sku_column=sku_column,
        skus=tuple(row_numbers), row_numbers={sku: tuple(numbers) for sku, numbers in row_numbers.items()},
        row_skus=row_skus, source_sha256=fingerprint, warnings=tuple(warnings), skipped_rows=tuple(skipped),
    )


def inspect_workbook(path: str | Path) -> dict[str, Any]:
    source = _source_path(path)
    book, cached = _open_book(source), _open_book(source, data_only=True)
    try:
        details = []
        for sheet in book.worksheets:
            if _is_generated_sheet(sheet):
                continue
            item: dict[str, Any] = {"name": sheet.title, "sku_count": 0, "row_count": 0, "error": "", "warnings": []}
            try:
                data = _sheet_input(source, sheet, cached[sheet.title])
                item.update(sku_count=len(data.skus), row_count=data.total_rows, warnings=list(data.warnings))
            except ValueError as exc:
                item["error"] = str(exc)
            details.append(item)
        valid = [item for item in details if not item["error"]]
        selected = next((item for item in valid if item["name"] == PREFERRED_SHEET), valid[0] if valid else None)
        return {"path": str(source), "sheets": [item["name"] for item in details],
                "sheet_name": selected["name"] if selected else "", "sheet_details": details,
                "warnings": selected["warnings"] if selected else []}
    finally:
        book.close()
        cached.close()


def load_input(source_file: str | Path, sheet_name: str | None = None) -> SkuInput:
    source = _source_path(source_file)
    if not sheet_name:
        sheet_name = inspect_workbook(source)["sheet_name"]
        if not sheet_name:
            raise ValueError("工作簿中没有包含有效 SKU 的工作表")
    fingerprint = claim._sha256(source)
    book, cached = _open_book(source), _open_book(source, data_only=True)
    try:
        if sheet_name not in book.sheetnames or _is_generated_sheet(book[sheet_name]):
            raise ValueError("请选择存在的来源工作表")
        data = _sheet_input(source, book[sheet_name], cached[sheet_name], fingerprint)
        if claim._sha256(source) != fingerprint:
            raise ValueError("源工作簿在读取过程中已变化，请重新选择文件")
        return data
    finally:
        book.close()
        cached.close()


def parse_sku_text(text: str) -> SkuInput:
    """Single/bulk SKU entry. Only surrounding whitespace is removed; case and suffix are kept."""
    import hashlib

    skus = list(dict.fromkeys(part.strip() for part in re.split(r"[\r\n,，;；\t]+", str(text or "")) if part.strip()))
    if not skus:
        raise ValueError("请输入至少一个 SKU")
    if len(skus) > MAX_SKUS:
        raise ValueError(f"不同 SKU 超过 {MAX_SKUS:,} 个，请分批输入")
    row_numbers = {sku: (index + 2,) for index, sku in enumerate(skus)}
    row_skus = {index + 2: (sku,) for index, sku in enumerate(skus)}
    fingerprint = hashlib.sha256("\n".join(skus).encode("utf-8")).hexdigest()
    return SkuInput(source_path=None, sheet_name="SKU查询", header_row=1, sku_column=1, skus=tuple(skus),
                    row_numbers=row_numbers, row_skus=row_skus, source_sha256=fingerprint, input_kind="text")


# --------------------------------------------------------------- output


def _write(cell: Any, value: Any) -> None:
    if isinstance(value, str):
        cell.value = claim.ILLEGAL_CHARACTERS_RE.sub("", value)[:32767]
        cell.data_type = "s"  # keeps "=cmd" / "+1" literal text, never a formula
    else:
        cell.value = value


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), TIME_FORMAT)
    except ValueError:
        return None


def _normalize(sku: str, row: Mapping[str, Any] | None) -> dict[str, Any]:
    if not row or row.get("sku") != sku:
        data = empty_row(sku, "本次未取得查询结果，请重新查询", "failed")
    else:
        data = dict(row)
    if data["status"] not in STATUS_LABELS:
        data = empty_row(sku, "查询结果状态异常，请重新查询", "failed")
    if data["status"] == "matched" and _time(data["create_time"]) is None:
        data = empty_row(sku, "结果缺少有效创建时间，请重新查询", "failed")
    return data


def _style_header(cell: Any) -> None:
    cell.font = Font(name="微软雅黑", bold=True, color="FFFFFF")
    cell.fill = PatternFill("solid", fgColor="275D87")
    cell.alignment = Alignment(vertical="center", wrap_text=True)


def _output_columns(sheet: Any, header_row: int) -> dict[str, int]:
    columns: dict[str, int] = {}
    for key, title in OUTPUT_HEADERS.items():
        owned = [cell for cell in sheet[header_row]
                 if cell.value == title and _owned_comment(cell.comment, kind="column", sheet_name=sheet.title, key=key)]
        if len(owned) > 1:
            raise ValueError(f"检测到多列重复的模块标记：{title}，请保留一列后重试")
        # A header cell that is empty but has content below is never reused: always append.
        cell = owned[0] if owned else sheet.cell(header_row, sheet.max_column + 1)
        _write(cell, title)
        cell.comment = Comment(_marker("column", sheet.title, key), GENERATED_AUTHOR)
        _style_header(cell)
        sheet.column_dimensions[get_column_letter(cell.column)].width = COLUMN_WIDTHS[key]
        columns[key] = cell.column
    return columns


def _generated_sheet(book: Any, kind: str, base_title: str, source_sheet: str, headers: Sequence[str] = ()) -> Any:
    matches = [sheet for sheet in book.worksheets
               if _owned_comment(sheet["A1"].comment, kind=kind, sheet_name=source_sheet)]
    if len(matches) > 1:
        raise ValueError(f"检测到重复的{base_title}，请保留一份后重试")
    if matches:
        sheet = matches[0]
        sheet.delete_rows(1, sheet.max_row)
    else:
        title, suffix = base_title, 1
        while title in book.sheetnames:  # never overwrite a user sheet with the same name
            suffix += 1
            title = f"{base_title}_{suffix}"
        sheet = book.create_sheet(title)
    for index, title in enumerate(headers, 1):
        _write(sheet.cell(1, index), title)
        _style_header(sheet.cell(1, index))
    sheet["A1"].comment = Comment(_marker(kind, source_sheet), GENERATED_AUTHOR)
    return sheet


def _append(sheet: Any, number: int, values: Sequence[Any], text_columns: set[int], time_columns: set[int]) -> None:
    for column, value in enumerate(values, 1):
        cell = sheet.cell(number, column)
        _write(cell, value)
        cell.number_format = DATE_FORMAT if column in time_columns else "@" if column in text_columns else "General"
        cell.alignment = Alignment(vertical="top", wrap_text=True)


def _detail_values(sheet_name: str, number: int, sku: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        sheet_name, number, sku, STATUS_LABELS[row["status"]], row["matched_sku"], row["match_source"],
        row["candidate_shop_id"], row["candidate_shop_name"], row["region"], row["item_id"], row["model_id"],
        _time(row["create_time"]), row["item_status"], row["item_archived"], row["model_archived"],
        row["matched_item_count"], row["matched_shop_count"], row["tied_shop_count"], row["tied_shops"],
        row["data_issue_count"], _time(row["base_clean_time"]), _time(row["model_clean_time"]),
        _time(row["queried_at"]), row["message"],
    )


def _fill_result_cells(sheet: Any, number: int, key: str, column: int, lines_for: Sequence[tuple[str, dict]],
                       skipped: Sequence[Mapping[str, Any]]) -> None:
    cell = sheet.cell(number, column)
    multiple = len(lines_for) + len(skipped) > 1
    if multiple:
        lines = []
        for sku, row in lines_for:
            if key == "shop":
                shown = row["candidate_shop_name"] if row["status"] == "matched" else ""
            elif key == "time":
                shown = row["create_time"] if row["status"] == "matched" else ""
            elif key == "status":
                shown = STATUS_LABELS[row["status"]]
            else:
                shown = row["message"]
            lines.append(f"{sku}：{shown or '—'}")
        if key in {"status", "note"}:
            lines.extend(f"{item['value']}：{'未查询' if key == 'status' else item['reason']}" for item in skipped)
        _write(cell, "\n".join(lines))
        cell.number_format = "@"
    elif lines_for:
        _, row = lines_for[0]
        if key == "shop":
            _write(cell, row["candidate_shop_name"] if row["status"] == "matched" else None)
            cell.number_format = "@"
        elif key == "time":
            _write(cell, _time(row["create_time"]) if row["status"] == "matched" else None)
            cell.number_format = DATE_FORMAT
        elif key == "status":
            _write(cell, STATUS_LABELS[row["status"]])
        else:
            _write(cell, row["message"])
    elif skipped:
        _write(cell, "未查询" if key == "status" else skipped[0]["reason"] if key == "note" else None)
    cell.alignment = Alignment(vertical="top", wrap_text=True)


def export_workbook(input_data: SkuInput, rows: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]],
                    output_dir: str | Path, metadata: Mapping[str, Any] | None = None) -> Path:
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists() and not destination.is_dir():
        raise ValueError("输出目录不能是文件")
    lookup = dict(rows) if isinstance(rows, Mapping) else {str(row.get("sku", "")): row for row in rows}
    results = {sku: _normalize(sku, lookup.get(sku)) for sku in input_data.skus}
    metadata = dict(metadata or {})
    source = input_data.source_path
    if source is not None:
        source = _source_path(source)
        if input_data.source_sha256 and claim._sha256(source) != input_data.source_sha256:
            raise ValueError("源工作簿在查询期间已变化，请重新选择文件，避免结果写入错误行")
        book = _open_book(source)
        stem = re.sub(r'[\\/:*?"<>|]', "_", source.stem)[:70]
    else:
        book = Workbook()
        book.active.title = input_data.sheet_name
        _write(book.active.cell(1, 1), "SKU")
        for number, sku in ((n, s[0]) for n, s in sorted(input_data.row_skus.items())):
            cell = book.active.cell(number, 1)
            _write(cell, sku)
            cell.number_format = "@"
        book.active.column_dimensions["A"].width = 30
        stem = "SKU查询"
    temporary: Path | None = None
    try:
        if input_data.sheet_name not in book.sheetnames:
            raise ValueError("来源工作表已变化，请重新选择文件")
        sheet = book[input_data.sheet_name]
        if _find_header(sheet) != (input_data.header_row, input_data.sku_column):
            raise ValueError("来源表头已变化，请重新选择文件")
        columns = _output_columns(sheet, input_data.header_row)
        detail = _generated_sheet(book, "detail", DETAIL_TITLE, sheet.title, DETAIL_HEADERS)
        for index, width in enumerate(DETAIL_WIDTHS, 1):
            detail.column_dimensions[get_column_letter(index)].width = width
        detail.freeze_panes = "D2"
        skipped_by_row: dict[int, list[dict[str, Any]]] = {}
        for item in input_data.skipped_rows:
            skipped_by_row.setdefault(item["row_number"], []).append(item)
        for number in range(input_data.header_row + 1, sheet.max_row + 1):  # clear only our own columns
            for column in columns.values():
                sheet.cell(number, column).value = None
        next_row = 2
        for number in sorted(set(input_data.row_skus) | set(skipped_by_row)):
            skus = input_data.row_skus.get(number, ())
            skipped = skipped_by_row.get(number, [])
            pairs = [(sku, results[sku]) for sku in skus]
            for key, column in columns.items():
                _fill_result_cells(sheet, number, key, column, pairs, skipped)
            for sku, row in pairs:
                _append(detail, next_row, _detail_values(sheet.title, number, sku, row), TEXT_DETAIL_COLUMNS, TIME_DETAIL_COLUMNS)
                next_row += 1
            for item in skipped:
                blank = empty_row(item["value"], item["reason"], "not_run")
                _append(detail, next_row, _detail_values(sheet.title, number, item["value"], blank),
                        TEXT_DETAIL_COLUMNS, TIME_DETAIL_COLUMNS)
                next_row += 1
        detail.auto_filter.ref = f"A1:{get_column_letter(len(DETAIL_HEADERS))}{max(detail.max_row, 1)}"
        summary = _generated_sheet(book, "summary", SUMMARY_TITLE, sheet.title)
        summary.column_dimensions["A"].width = 26
        summary.column_dimensions["B"].width = 100
        counts = {status: sum(row["status"] == status for row in results.values()) for status in STATUS_LABELS}
        items = [("来源工作表", sheet.title), ("不同 SKU 数", len(results)), ("来源 SKU 行数", input_data.total_rows),
                 *((label, counts[status]) for status, label in STATUS_LABELS.items()),
                 ("未查询内容数", len(input_data.skipped_rows))]
        items.extend(tuple(item) for item in metadata.get("summary_items", ()))
        for index, (label, value) in enumerate(items, 1):
            _write(summary.cell(index, 1), str(label))
            summary.cell(index, 1).font = Font(name="微软雅黑", bold=True)
            _write(summary.cell(index, 2), value if isinstance(value, (int, float)) else str(value))
            summary.cell(index, 2).alignment = Alignment(vertical="top", wrap_text=True)
        summary["A1"].comment = Comment(_marker("summary", sheet.title), GENERATED_AUTHOR)
        if source is not None and input_data.source_sha256 and claim._sha256(source) != input_data.source_sha256:
            raise ValueError("源工作簿在导出期间已变化，请重新选择文件")
        destination.mkdir(parents=True, exist_ok=True)
        incomplete = metadata.get("is_complete") is False or any(
            row["status"] in {"failed", "not_run", "incomplete"} for row in results.values())
        filename = (f"{stem}_ERP创建时间对标_{datetime.now().astimezone():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
                    f"{'_结果不完整' if incomplete else ''}.xlsx")
        output_path = destination / filename
        with tempfile.NamedTemporaryFile(prefix=".erp-sku-", suffix=".xlsx", dir=destination, delete=False) as stream:
            temporary = Path(stream.name)
        book.save(temporary)
        claim._preserve_empty_strings(temporary)
        os.replace(temporary, output_path)  # atomic within the same directory; name is unique
        temporary = None
        return output_path
    finally:
        book.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
