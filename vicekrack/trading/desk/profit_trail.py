"""Profit trail: let winners run, protect profits, limit losses. Plain code, no AI. Simulated money only.

There is NO fixed take-profit. Instead the Position Monitor tracks the BEST PROFIT reached since the
agents bought a coin and sells when the profit falls back a set number of PERCENTAGE POINTS from that best.

  Profit %      (price after estimated selling costs) / (what we paid per coin, buy fee included) - 1, x 100
  Best profit   the highest profit % seen since the buy (from the highest price seen)
  Give-back     how many points of profit we let go before selling. It depends on how big the best
                profit got, through `tiers`: the tier with the largest `from_profit_pct` that the best
                profit has reached. Below the first tier the trail is not active yet.
  Locked        best profit - give-back, and never below `min_lock_profit_pct` once active.
  Sell          when the profit now is at or below the locked level.

Example with tiers 3%->3 points, 10%->6 points: a coin that peaks at +20% has 6 points of give-back,
so it is sold when profit falls to +14% (a drop of 6 POINTS of return, not a 6% drop in price). If it
keeps climbing to +30%, the lock rises to +24%; to +40%, +34%.

Losers are handled separately: the plain stop-loss (`stop_loss_pct`) sells a coin that falls that far
below the buy price, whether or not it was ever profitable.

A sell is decided at the price the PC saw; the actual fill is the next bid with slippage and fees, and a
fast drop between checks can land below the locked level. The lock is a trigger, not a guarantee.
"""

from decimal import Decimal, localcontext


def _ctx(context):
    context.prec = 50


def validate(settings):
    """Raise ValueError if the profit_trail settings are not usable."""
    tiers = settings["tiers"]
    if not isinstance(tiers, list) or not 1 <= len(tiers) <= 10:
        raise ValueError
    last = None
    for tier in tiers:
        start, giveback = Decimal(tier["from_profit_pct"]), Decimal(tier["giveback_points"])
        if not (0 < start < 1000 and 0 < giveback < 100) or (last is not None and start <= last):
            raise ValueError
        last = start
    lock = Decimal(settings.get("min_lock_profit_pct", "0"))
    if not 0 <= lock < Decimal(tiers[0]["from_profit_pct"]):
        raise ValueError
    if not isinstance(settings.get("let_winners_run", True), bool):
        raise ValueError


def profit_pct(price, cost_each, exit_cost_bps="0"):
    with localcontext() as context:
        _ctx(context)
        net = Decimal(price) * (1 - Decimal(exit_cost_bps) / 10000)
        return (net / Decimal(cost_each) - 1) * 100


def giveback_for(best_pct, tiers):
    """Give-back points for this best profit, or None while the trail isn't active."""
    points = None
    for tier in tiers:
        if best_pct >= Decimal(tier["from_profit_pct"]):
            points = Decimal(tier["giveback_points"])
    return points


def check(cost_each, price, peak_price, settings, exit_cost_bps="0"):
    """Where a position stands. Returns profit_pct, best_pct, active, locked_pct (None if inactive), sell."""
    with localcontext() as context:
        _ctx(context)
        now = profit_pct(price, cost_each, exit_cost_bps)
        best = profit_pct(max(Decimal(peak_price), Decimal(price)), cost_each, exit_cost_bps)
        points = giveback_for(best, settings["tiers"])
        if points is None:
            return {"profit_pct": now, "best_pct": best, "active": False, "locked_pct": None, "sell": False}
        locked = max(best - points, Decimal(settings.get("min_lock_profit_pct", "0")))
        locked = min(locked, best)
        return {"profit_pct": now, "best_pct": best, "active": True, "locked_pct": locked, "sell": now <= locked}


def simulate(path, cost_each, settings, stop_loss_pct, exit_cost_bps="0"):
    """Replay a recorded price path (oldest first) under these settings: (exit index or None, profit % there).

    settings=None means 'no trail' (hold to the end of the path); a dict with "fixed_take_profit_pct"
    replays a plain fixed take-profit for comparison."""
    with localcontext() as context:
        _ctx(context)
        stop = Decimal(cost_each) * (1 - Decimal(stop_loss_pct) / 100)
        peak = None
        for index, price in enumerate(path):
            price = Decimal(price)
            peak = price if peak is None or price > peak else peak
            if price <= stop:
                return index, profit_pct(price, cost_each, exit_cost_bps)
            if settings is None:
                continue
            if "fixed_take_profit_pct" in settings:
                if profit_pct(price, cost_each, exit_cost_bps) >= Decimal(settings["fixed_take_profit_pct"]):
                    return index, profit_pct(price, cost_each, exit_cost_bps)
                continue
            if check(cost_each, price, peak, settings, exit_cost_bps)["sell"]:
                return index, profit_pct(price, cost_each, exit_cost_bps)
        return (None, profit_pct(path[-1], cost_each, exit_cost_bps)) if path else (None, Decimal(0))


PRESETS = {
    "tight": {"tiers": [{"from_profit_pct": "2", "giveback_points": "1.5"}, {"from_profit_pct": "10", "giveback_points": "4"}],
              "min_lock_profit_pct": "0.5"},
    "loose": {"tiers": [{"from_profit_pct": "4", "giveback_points": "4"}, {"from_profit_pct": "10", "giveback_points": "8"}],
              "min_lock_profit_pct": "0.5"},
    "fixed_3pct_take_profit": {"fixed_take_profit_pct": "3"},
    "no_trail_hold_to_end": None,
}


def compare(records, current, stop_loss_pct, exit_cost_bps="0"):
    """Replay every recorded position under the current settings and the presets. Paper evidence only."""
    variants = {"current": current, **PRESETS}
    out = []
    for name, settings in variants.items():
        results = [simulate(r["path"], r["cost_each"], settings, stop_loss_pct, exit_cost_bps)[1] for r in records
                   if r.get("path")]
        with localcontext() as context:
            _ctx(context)
            total = sum(results, Decimal(0))
            out.append({"name": name, "positions": len(results),
                        "average_profit_pct": None if not results else format(round(total / len(results), 2), "f"),
                        "wins": sum(1 for r in results if r > 0), "losses": sum(1 for r in results if r <= 0)})
    return out
