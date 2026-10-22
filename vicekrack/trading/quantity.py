"""Order quantities for the offline simulator: whole units or fractions (HQ Phase 3b).

Stocks trade in whole shares, so their quantities stay JSON integers exactly as before (every
stock run, hash and report is unchanged). Crypto datasets (Alpaca provenance `asset_class:
"crypto"`) trade in fractions: their quantities are decimal strings with at most 8 decimal places,
like "0.00833333", always rounded DOWN so a sized order never exceeds its dollar budget.

A run is fractional when any of its quantities is a string; then every quantity it reports
(including totals such as an open position of 0) is a string too.
"""

from decimal import ROUND_DOWN, Decimal, localcontext

from .errors import TradingError
from .money import fmt, parse

STEP = Decimal("0.00000001")


def is_fractional_dataset(dataset):
    provider = dataset.get("source", {}).get("provider") or {}
    return provider.get("asset_class") == "crypto"


def to_decimal(quantity):
    """A stored quantity (int or decimal string) as an exact Decimal."""
    if isinstance(quantity, bool):
        raise TradingError("invalid_quantity", "A quantity must be a whole number or a decimal string.")
    if isinstance(quantity, int):
        return Decimal(quantity)
    return parse(quantity, "decimal", "invalid_quantity")


def to_json(value, fractional):
    """Decimal -> the stored form: a decimal string for fractional runs, else an int (must be whole)."""
    if fractional:
        return fmt(value)
    if value != value.to_integral_value():
        raise TradingError("invalid_quantity", "A whole-unit run produced a fractional quantity.")
    return int(value)


def round_down(value):
    with localcontext() as context:
        context.prec = 50
        return value.quantize(STEP, rounding=ROUND_DOWN)


def notional(price, quantity):
    """price x quantity, held at 8 decimal places (exact for whole units; cut, never rounded up, for fractions)."""
    with localcontext() as context:
        context.prec = 50
        return (price * to_decimal(quantity)).quantize(STEP, rounding=ROUND_DOWN)


def run_is_fractional(run):
    items = [o["quantity"] for o in run["orders"]] + [f["quantity"] for f in run["fills"]]
    items += [p["quantity"] for p in run["positions"]] + [run["summary"]["open_position_quantity"]]
    sizing = run["policy"]["sizing"]
    if sizing["method"] == "fixed_quantity":
        items.append(sizing["quantity"])
    return any(isinstance(q, str) for q in items)
