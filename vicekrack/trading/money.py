"""Exact decimal handling for prices, quantities and money.

All monetary and price values travel as JSON *strings* and are parsed with Decimal.
Floats, integers-as-money, exponents, NaN, infinity, signs where not allowed and excess
decimal places are rejected. Arithmetic uses a local high-precision context; results are
compared exactly and rendered as plain fixed-point strings.
"""

import re
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext

from .errors import TradingError

PATTERNS = {
    "decimal": re.compile(r"^(0|[1-9][0-9]{0,17})(\.[0-9]{1,8})?$"),
    "signed_decimal": re.compile(r"^-?(0|[1-9][0-9]{0,17})(\.[0-9]{1,8})?$"),
    "money": re.compile(r"^(0|[1-9][0-9]{0,15})(\.[0-9]{1,2})?$"),
    "signed_money": re.compile(r"^-?(0|[1-9][0-9]{0,15})(\.[0-9]{1,2})?$"),
}
EIGHT_PLACES = Decimal("0.00000001")
PRECISION = 50


def parse(value, kind="decimal", code="invalid_decimal"):
    """Strict string -> Decimal. Raises TradingError(code) for anything else."""
    if isinstance(value, bool) or not isinstance(value, str) or not PATTERNS[kind].match(value):
        raise TradingError(code, "A decimal value is missing or not a plain decimal string.")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise TradingError(code, "A decimal value could not be parsed.") from None


def multiply(left, right):
    with localcontext() as context:
        context.prec = PRECISION
        return left * right


def add(left, right):
    with localcontext() as context:
        context.prec = PRECISION
        return left + right


def fmt(value):
    """Plain fixed-point string with at most 8 decimal places (banker's rounding), no exponent."""
    with localcontext() as context:
        context.prec = PRECISION
        quantized = value.quantize(EIGHT_PLACES, rounding=ROUND_HALF_EVEN)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text
