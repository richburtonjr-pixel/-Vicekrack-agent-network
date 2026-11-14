"""Your own practice trades (Trading PR 4). SIMULATED MONEY ONLY: no broker, no real order.

The Trade tab asks for "buy $20 of SOL" or "sell all my DOGE". This module:
  1. fetches a FRESH Kraken price on the PC (never a price sent by the phone),
  2. re-prices every coin you hold from the same request, so the $80 guard sees today's equity,
  3. buys at Kraken's ask or sells at the bid (plus the usual slippage and 0.25% fee),
  4. records it in the practice ledger as placed by "you".

Your trades skip the agents' trade desk (you are the decision-maker) but never the $80 shutdown,
never leverage, and never more than the cash you have.
"""

import time
from decimal import Decimal

from ..contracts import utc_now
from ..errors import TradingError
from ..providers.kraken_market import Catalog, tickers, to_price
from ..providers.alpaca import urllib_transport
from . import account as book
from .store import PracticeStore, load_config



def _amount(value):
    if value == "all":
        return "all"
    if not isinstance(value, str) or not value.replace(".", "", 1).isdigit() or len(value) > 12:
        raise TradingError("invalid_amount", "Enter a dollar amount like 20 or 12.50.")
    amount = Decimal(value).quantize(Decimal("0.01"))
    if amount <= 0:
        raise TradingError("invalid_amount", "Enter an amount above $0.")
    return format(amount, "f")


def place_order(symbol, side, amount, *, root=None, transport=urllib_transport, clock=time.time, now=None):
    """-> {"ok", "fill" or "refused", "account"}. Raises TradingError for bad requests or no price."""
    if side not in ("buy", "sell"):
        raise TradingError("invalid_side", "Choose buy or sell.")
    amount = _amount(amount)
    rooted = {"root": root} if root is not None else {}
    config = load_config(**rooted)
    store = PracticeStore(root)
    state = store.load(config)
    catalog = Catalog(root, transport=transport, clock=clock)
    catalog.get(symbol)
    wanted = sorted(set(state["positions"]) | {symbol})
    prices = tickers(catalog, transport, symbols=wanted)
    row = prices.get(symbol)
    if row is None:
        raise TradingError("no_price", "Kraken didn't return a price for that coin. Nothing was traded.")
    if not row["tradeable"]:
        raise TradingError("not_tradeable", "That coin's price is too small to practice-trade accurately.")
    now = now or utc_now()
    marks = {}
    for held in state["positions"]:
        price = to_price((prices.get(held) or {}).get("last"))
        if price is not None and held != symbol:
            marks[held] = {"price": price, "at": now}
    if side == "buy":
        state, verdict, refusal = store.apply(config, "buy", symbol, amount, to_price(row["ask"]), now=now,
                                              by="you", marks=marks)
    else:
        state, verdict, refusal = store.apply(config, "sell", symbol, to_price(row["bid"]), now=now,
                                              by="you", dollars=amount, marks=marks)
    fills = [line for line in store.ledger(5) if line.get("kind") == "fill" and line.get("at") == now
             and line.get("by") == "you"]
    return {"ok": refusal is None, "simulated": True, "refused": refusal.as_dict() if refusal else None,
            "fill": fills[-1] if fills and refusal is None else None,
            "account": book.summary(state, config, now)}
