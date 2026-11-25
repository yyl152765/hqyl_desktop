"""Parse exact Shopee monetary values without guessing from display fragments.

This module is deliberately independent of browser, configuration and database
code. Compact display amounts (``1.4m``) cannot represent an exact payment or
balance and must be replaced with the full amount by the caller.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal
from types import MappingProxyType


MONEY_PARSER_VERSION = "1.0.0"
SITE_CURRENCY = MappingProxyType(
    {"id": "IDR", "vn": "VND", "my": "MYR", "th": "THB", "ph": "PHP"}
)
SITE_CURRENCY_DECIMALS = MappingProxyType(
    {"id": 0, "vn": 0, "my": 2, "th": 2, "ph": 2}
)
_CURRENCY_MARKERS = {
    "id": ("IDR", "Rp"),
    "vn": ("VND", "₫", "đ"),
    "my": ("MYR", "RM"),
    "th": ("THB", "฿"),
    "ph": ("PHP", "₱"),
}
_HORIZONTAL_SPACE = " \t\u00a0\u202f"
_SPACE_RE = r"[ \t\u00a0\u202f]*"
_INTEGER_RE = re.compile(r"[0-9]+")
_GROUPED_INTEGER_RE = re.compile(r"[0-9]{1,3}(?P<separator>[.,])[0-9]{3}(?:(?P=separator)[0-9]{3})*")


class ShopeeMoneyParseError(ValueError):
    """The supplied value is not an exact amount for the selected site.

    Messages are fixed diagnostics; they never include the supplied value.
    """


def _amount_token(text: str, site_code: str) -> str:
    markers = "|".join(re.escape(marker) for marker in _CURRENCY_MARKERS[site_code])
    # A single currency marker is optional, before OR after the whole amount.
    # Internal whitespace, two markers, signs and any explanatory text fail.
    number = r"(?P<amount>[0-9][0-9.,]*)"
    for pattern in (
        rf"(?:(?:{markers}){_SPACE_RE})?{number}",
        rf"{number}{_SPACE_RE}(?:{markers})",
    ):
        match = re.fullmatch(pattern, text, flags=re.IGNORECASE)
        if match is not None:
            return match.group("amount")
    raise ShopeeMoneyParseError("Invalid money text or currency")


def _parse_number_token(token: str, decimal_places: int) -> Decimal:
    if _INTEGER_RE.fullmatch(token):
        return Decimal(token)
    if _GROUPED_INTEGER_RE.fullmatch(token):
        return Decimal(token.replace(",", "").replace(".", ""))

    if decimal_places:
        # With both separator types, only consistently grouped thousands plus
        # a final decimal part are accepted. The decimal separator is the last
        # separator; an ungrouped decimal accepts one or two fractional digits.
        for decimal_sep, grouping_sep in ((".", ","), (",", ".")):
            ds, gs = re.escape(decimal_sep), re.escape(grouping_sep)
            plain = rf"[0-9]+{ds}[0-9]{{1,{decimal_places}}}"
            grouped = rf"[0-9]{{1,3}}(?:{gs}[0-9]{{3}})+{ds}[0-9]{{1,{decimal_places}}}"
            if re.fullmatch(rf"(?:{plain}|{grouped})", token):
                return Decimal(token.replace(grouping_sep, "").replace(decimal_sep, "."))
    raise ShopeeMoneyParseError("Invalid money grouping or precision")


def _validate_numeric_amount(amount: Decimal, decimal_places: int) -> Decimal:
    if not amount.is_finite():
        raise ShopeeMoneyParseError("Money must be finite")
    if amount.is_signed():
        raise ShopeeMoneyParseError("Money must be nonnegative")
    # Inspect digits instead of quantizing so very large exact integers are not
    # rounded by the caller's current Decimal precision/context.
    _, digits, exponent = amount.as_tuple()
    excess_places = -exponent - decimal_places
    if excess_places > 0 and any(digits[-excess_places:]):
        raise ShopeeMoneyParseError("Invalid money precision")
    return amount


def parse_shopee_money(value: object, site_code: str) -> Decimal:
    """Return an exact, nonnegative monetary amount for a supported site.

    IDR/VND accept whole units only; MYR/THB/PHP allow up to two decimal
    places. A string such as ``790.329`` denotes grouped whole units, whereas
    the numeric value ``Decimal('790.329')`` retains its fractional meaning.
    Integer-valued floats are accepted without rounding their numeric value.
    """
    if not isinstance(site_code, str):
        raise ShopeeMoneyParseError("Unsupported Shopee site")
    site = site_code.strip().lower()
    if site not in SITE_CURRENCY:
        raise ShopeeMoneyParseError("Unsupported Shopee site")
    decimal_places = SITE_CURRENCY_DECIMALS[site]

    if isinstance(value, bool):
        raise ShopeeMoneyParseError("Unsupported money value type")
    if isinstance(value, str):
        text = value.strip(_HORIZONTAL_SPACE)
        if not text:
            raise ShopeeMoneyParseError("Money text is empty")
        amount = _parse_number_token(_amount_token(text, site), decimal_places)
    elif isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ShopeeMoneyParseError("Money must be finite")
        if not value.is_integer():
            raise ShopeeMoneyParseError("Money float must be integral")
        if math.copysign(1.0, value) < 0:
            raise ShopeeMoneyParseError("Money must be nonnegative")
        amount = Decimal(int(value))
    else:
        raise ShopeeMoneyParseError("Unsupported money value type")
    return _validate_numeric_amount(amount, decimal_places)
