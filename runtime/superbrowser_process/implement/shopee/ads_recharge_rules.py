from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


SITE_BALANCE_SUCCESS_TOLERANCE = {
    "th": Decimal("500"),
    "ph": Decimal("500"),
    "id": Decimal("40000"),
    "vn": Decimal("300000"),
    "my": Decimal("50"),
}

SITE_MIN_RECHARGE_AMOUNT = {
    "th": Decimal("500"),
    "ph": Decimal("500"),
    "vn": Decimal("300000"),
    "my": Decimal("50"),
}


def decimal_amount(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return Decimal("0")


def format_amount(value: Any) -> str:
    amount = decimal_amount(value)
    if amount == amount.to_integral():
        return str(amount.to_integral())
    return format(amount.normalize(), "f")


def get_min_recharge_amount(site_code: str) -> Decimal | None:
    return SITE_MIN_RECHARGE_AMOUNT.get(str(site_code or "").lower())


def is_recharge_amount_below_minimum(recharge_amount: Any, site_code: str) -> bool:
    minimum = get_min_recharge_amount(site_code)
    return minimum is not None and decimal_amount(recharge_amount) < minimum


def format_minimum_recharge_skip_message(site_code: str, store_name: str, recharge_amount: Any) -> str:
    minimum = get_min_recharge_amount(site_code)
    return (
        f"{store_name}: 充值金额 {format_amount(recharge_amount)} "
        f"小于最小充值额 {format_amount(minimum)}，不执行充值"
    )
