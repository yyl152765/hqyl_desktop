"""Synthetic Shopee workbook; no production account or financial data."""
from pathlib import Path
from openpyxl import Workbook
from backend.services.shopee_id_income_report import month_date_bounds, validate_income_xlsx


def income_file(folder: Path, month: str = "2026-10", **changes) -> dict:
    bounds = month_date_bounds(month)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / bounds["expected_filename"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    for cell, value in {
        "A1": "Income Report", "A5": "Report Details", "A6": "Username (Seller)",
        "B6": "synthetic-merchant", "A7": "From", "B7": bounds["start_iso"],
        "A8": "to", "B8": bounds["end_iso"], "D10": "Rp",
    }.items():
        ws[cell] = value
    for cell, value in changes.items():
        ws[cell] = value
    adj = wb.create_sheet("Adjustment")
    for cell, value in {"A3": "From", "B3": bounds["start_iso"], "C3": "to",
                        "D3": bounds["end_iso"], "A4": "Rp"}.items():
        adj[cell] = value
    fee = wb.create_sheet("Seller Fee")
    fee.append([])
    fee.append(["Sequence No.", "Order ID", "Platform Fee"])
    detail = wb.create_sheet("Income")
    detail["A1"], detail["K1"] = "Order Info", "Released Amount Details"
    wb.save(path)
    wb.close()
    if changes:
        return {"path": str(path)}
    return {**validate_income_xlsx(path, month), "invoice_type": "income_report",
            "row_index": 0, "report_source": "history", "source": "history"}
