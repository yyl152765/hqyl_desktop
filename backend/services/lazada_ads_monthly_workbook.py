"""Excel workbook generator for Lazada Ads Monthly Report.

Generates the monthly report workbook strictly adhering to specification:
Columns A to H:
  A: 店铺名称 (Text)
  B: 广告金额统计 (Numeric or blank)
  C: 广告截图 (Embedded image)
  D: 联盟花费 (Numeric or blank)
  E: 联盟截图 (Embedded image)
  F: 优惠金额 (Numeric coupon recharge amount sum or verified 0)
  G: 优惠券截图 (Embedded image)
  H: 真实花费 (Numeric ads - affiliate - coupon, or blank)
Columns I to O:
  I: 站点
  J: 币种
  K: 统计月份
  L: 状态
  M: 采集时间
  N: 说明
  O: 批次号
"""
from __future__ import annotations

import os
from uuid import uuid4
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.drawing.image import Image
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
from openpyxl.drawing.xdr import XDRPositiveSize2D
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.utils.units import pixels_to_EMU

HEADERS = [
    "店铺名称",
    "广告金额统计",
    "广告截图",
    "联盟花费",
    "联盟截图",
    "优惠金额",
    "优惠券截图",
    "真实花费",
    "站点",
    "币种",
    "统计月份",
    "状态",
    "采集时间",
    "说明",
    "批次号",
]

IMAGE_COLUMNS = {3, 5, 7}  # C, E, G
NUMERIC_COLUMNS = {2, 4, 6, 8}  # B, D, F, H

STATUS_NAMES = {
    "success": "成功",
    "partial": "部分完成",
    "failed": "失败",
    "blocked": "已阻塞",
    "cancelled": "已取消",
    "pending": "待处理",
    "running": "采集中",
}


def _set_text_cell(sheet, row: int, col: int, value: Any) -> None:
    cell = sheet.cell(row, col, str(value if value is not None else ""))
    cell.data_type = "s"
    cell.alignment = Alignment(vertical="center", wrap_text=True)


def _set_numeric_cell(sheet, row: int, col: int, value: Any) -> None:
    if value is None or value == "":
        return
    if isinstance(value, bool):
        raise ValueError("金额必须为有效数字")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"金额必须为有效数字: {value}") from exc
    if not amount.is_finite():
        raise ValueError("金额必须为有限数字")
    cell = sheet.cell(row, col)
    # Amount columns must remain numeric; never silently turn them into text.
    if len(amount.as_tuple().digits) > 15:
        raise ValueError('金额超过 Excel 15 位有效精度')
    else:
        cell.value = float(amount)
        places = max(2, -amount.as_tuple().exponent)
        fmt = '#,##0.' + '0' * places
        cell.number_format = f'{fmt};[Red]-{fmt};{fmt}'
        cell.alignment = Alignment(vertical="center", horizontal="right")


def _embed_image(sheet, row: int, col: int, image_path: str | Path | None) -> None:
    if not image_path:
        return
    path = Path(image_path)
    if not path.is_file():
        return
    try:
        img = Image(str(path))
    except Exception:
        return

    max_width, max_height = 320, 200
    scale = min(max_width / img.width, max_height / img.height, 1.0)
    display_w = img.width * scale
    display_h = img.height * scale

    img.anchor = OneCellAnchor(
        _from=AnchorMarker(
            col=col - 1,
            row=row - 1,
            colOff=pixels_to_EMU(4),
            rowOff=pixels_to_EMU(4),
        ),
        ext=XDRPositiveSize2D(pixels_to_EMU(display_w), pixels_to_EMU(display_h)),
    )
    sheet.add_image(img)
    sheet.column_dimensions[get_column_letter(col)].width = 47
    current_h = sheet.row_dimensions[row].height or 24
    sheet.row_dimensions[row].height = max(current_h, display_h * 0.75 + 10)


def create_ads_monthly_workbook(rows: list[dict[str, Any]], run_meta: dict[str, Any] | None = None) -> Workbook:
    """Create an openpyxl Workbook for Lazada monthly ads report."""
    wb = Workbook()
    sheet = wb.active
    sheet.title = "广告报表"

    # Header styling
    for col_idx, header_text in enumerate(HEADERS, 1):
        _set_text_cell(sheet, 1, col_idx, header_text)
        cell = sheet.cell(1, col_idx)
        cell.fill = PatternFill("solid", fgColor="214C68")
        cell.font = Font(color="FFFFFF", bold=True)
        sheet.column_dimensions[get_column_letter(col_idx)].width = 20

    sheet.row_dimensions[1].height = 30
    sheet.freeze_panes = "B2"
    sheet.sheet_view.showGridLines = True

    for row_idx, data in enumerate(rows, start=2):
        if data.get('status') == 'success':
            from PIL import Image as PILImage
            for key in ('evidence_ads', 'evidence_affiliate', 'evidence_coupon'):
                if not data.get(key):
                    raise ValueError('成功记录必须包含三张原始截图')
                with PILImage.open(data[key]) as proof:
                    proof.verify()
            values = [Decimal(str(data.get(k))) for k in ('ads_spend', 'affiliate_spend', 'coupon_amount', 'actual_spend')]
            if not all(v.is_finite() for v in values) or values[0] - values[1] - values[2] != values[3]:
                raise ValueError('真实花费与 B-D-F 不一致')
        sheet.row_dimensions[row_idx].height = 28

        # A: 店铺名称
        _set_text_cell(sheet, row_idx, 1, data.get("store_name") or data.get("requested_name") or "")

        # B: 广告金额统计
        ads_spend = data.get("ads_spend")
        _set_numeric_cell(sheet, row_idx, 2, ads_spend)

        # C: 广告截图
        _embed_image(sheet, row_idx, 3, data.get("evidence_ads"))

        # D: 联盟花费
        affiliate_spend = data.get("affiliate_spend")
        _set_numeric_cell(sheet, row_idx, 4, affiliate_spend)

        # E: 联盟截图
        _embed_image(sheet, row_idx, 5, data.get("evidence_affiliate"))

        # F: 优惠金额
        coupon_amount = data.get("coupon_amount")
        _set_numeric_cell(sheet, row_idx, 6, coupon_amount)

        # G: 优惠券截图
        _embed_image(sheet, row_idx, 7, data.get("evidence_coupon"))

        # H: 真实花费
        actual_spend = data.get("actual_spend") if data.get('status') == 'success' else None
        _set_numeric_cell(sheet, row_idx, 8, actual_spend)

        # I: 站点
        _set_text_cell(sheet, row_idx, 9, data.get("country") or "")

        # J: 币种
        _set_text_cell(sheet, row_idx, 10, data.get("currency") or "")

        # K: 统计月份
        _set_text_cell(sheet, row_idx, 11, data.get("month") or "")

        # L: 状态
        status_raw = data.get("status") or "pending"
        _set_text_cell(sheet, row_idx, 12, STATUS_NAMES.get(status_raw, status_raw))

        # M: 采集时间
        _set_text_cell(sheet, row_idx, 13, data.get("captured_at") or "")

        # N: 说明
        note = data.get("note") or data.get("message") or ""
        _set_text_cell(sheet, row_idx, 14, note)

        # O: 批次号
        run_id = data.get("run_id") or (run_meta.get("run_id") if run_meta else "") or ""
        _set_text_cell(sheet, row_idx, 15, run_id)

    return wb


def export_ads_monthly_workbook(
    target_path: str | Path,
    rows: list[dict[str, Any]],
    run_meta: dict[str, Any] | None = None,
) -> Path:
    """Save workbook atomically with temp file replacement."""
    target = Path(target_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(target.stem + '.' + uuid4().hex + '.tmp.xlsx')
    wb = None
    try:
        wb = create_ads_monthly_workbook(rows, run_meta=run_meta)
        wb.save(str(temp_path))
        wb.close()
        from openpyxl import load_workbook
        check = load_workbook(temp_path)
        try:
            if check.active.max_row != len(rows) + 1:
                raise ValueError('报表回读行数不一致')
        finally:
            check.close()
        os.replace(temp_path, target)
    except PermissionError as exc:
        raise PermissionError(f"目标文件正被其他程序打开，无法写入: {target}") from exc
    finally:
        if wb is not None:
            wb.close()
        temp_path.unlink(missing_ok=True)

    return target
