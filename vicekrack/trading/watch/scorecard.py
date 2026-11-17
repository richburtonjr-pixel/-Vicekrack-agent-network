"""The early-or-late scorecard (autonomous-trading step 3). Plain code, no AI, no trades.

Question it answers: when the scanner flags a coin, is it EARLY (the big move is still ahead) or LATE
(the move already happened)? It measures, it doesn't predict.

Every cycle:
  1. each new radar pick is recorded (a symbol at most once per `repick_hours`) with its price, signals
     and score, plus Bitcoin's price at that moment (the benchmark);
  2. every open pick is followed with the radar's whole-market prices: highest and lowest price since the
     pick, the price 1 h, 4 h and 24 h later, and its best rank among the day's top gainers (coins with
     enough volume, stablecoins left out, ranked by change since 00:00 UTC);
  3. after 24 h the pick is closed with a verdict:
       early  the price later rose at least `early_gain_pct` above the pick price,
       late   it never rose more than `late_max_gain_pct` and was lower 1 h later (the move was over),
       mixed  anything in between.

Files: runtime/trading/watch/picks-open.json (being followed) and picks.jsonl (closed, one line each).
The summary compares the picks' average 1 h / 4 h / 24 h change with Bitcoin's over the same windows.
"""

import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..contracts import ROOT

SETTINGS = {"repick_hours": 4, "follow_hours": 24, "early_gain_pct": "3", "late_max_gain_pct": "1",
            "top_n": 10, "min_volume_24h_usd": "1000000", "benchmark": "BTCUSD",
            "skip_bases": ["USDT", "USDC", "DAI", "PYUSD", "USDG", "TUSD", "FDUSD", "USDE", "RLUSD", "USDQ", "USDR",
                           "EURT", "EUR", "GBP", "AUD", "CAD", "CHF", "JPY"]}
CHECKPOINTS = (("1h", 3600), ("4h", 14400), ("24h", 86400))
Q = Decimal("0.01")


def _pct(new, old):
    try:
        old = Decimal(old)
        return None if old == 0 else (Decimal(new) - old) / old * 100
    except (InvalidOperation, TypeError):
        return None


def _fmt(value):
    return None if value is None else format(value.quantize(Q), "f")


def gainer_ranks(market, settings=SETTINGS):
    """{symbol: rank} among today's gainers (1 = biggest % rise since 00:00 UTC)."""
    skip = set(settings["skip_bases"])
    floor = Decimal(settings["min_volume_24h_usd"])
    rows = []
    for symbol, row in market.items():
        try:
            if row["base"] in skip or row.get("change_today_pct") is None or Decimal(row["volume_24h_usd"]) < floor:
                continue
            rows.append((-Decimal(row["change_today_pct"]), symbol))
        except (InvalidOperation, KeyError, TypeError):
            continue
    rows.sort()
    return {symbol: index + 1 for index, (_, symbol) in enumerate(rows)}


class Scorecard:
    def __init__(self, root=None, settings=None):
        base = Path(root if root is not None else ROOT) / "runtime/trading/watch"
        self.open_path = base / "picks-open.json"
        self.closed_path = base / "picks.jsonl"
        self.settings = {**SETTINGS, **(settings or {})}

    # ---------------------------------------------------------------- storage
    def _load_open(self):
        try:
            data = json.loads(self.open_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError, UnicodeError):
            return []

    def _save_open(self, picks):
        self.open_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.open_path.parent, suffix=".tmp", delete=False) as out:
            json.dump(picks, out, separators=(",", ":"))
            temporary = out.name
        os.replace(temporary, self.open_path)

    def closed(self, limit=2000):
        if not self.closed_path.is_file():
            return []
        out = []
        for line in self.closed_path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    # ---------------------------------------------------------------- the cycle
    def update(self, candidates, market, now, now_epoch):
        """Record new picks, follow open ones, close finished ones. Returns {"recorded", "closed", "open"}."""
        s = self.settings
        picks = self._load_open()
        ranks = gainer_ranks(market, s)
        bench = (market.get(s["benchmark"]) or {}).get("last")
        recent = {p["symbol"]: p["picked_epoch"] for p in picks}
        for p in self.closed(500):
            recent[p["symbol"]] = max(recent.get(p["symbol"], 0), p["picked_epoch"])
        recorded = 0
        for c in candidates:
            row = market.get(c["symbol"])
            if row is None or now_epoch - recent.get(c["symbol"], 0) < s["repick_hours"] * 3600:
                continue
            picks.append({"symbol": c["symbol"], "base": c.get("base"), "picked_at": now, "picked_epoch": now_epoch,
                          "price": row["last"], "change_today_pct": row.get("change_today_pct"),
                          "signals": c.get("signals") or ["streak"], "score": c.get("score"),
                          "rank_at_pick": ranks.get(c["symbol"]), "benchmark_price": bench,
                          "high": row["last"], "low": row["last"], "best_rank": ranks.get(c["symbol"]),
                          "checkpoints": {}, "benchmark_checkpoints": {}})
            recent[c["symbol"]] = now_epoch
            recorded += 1
        still_open, closed = [], 0
        for p in picks:
            row = market.get(p["symbol"])
            age = now_epoch - p["picked_epoch"]
            if row is not None and age > 0:
                price = Decimal(row["last"])
                if price > Decimal(p["high"]):
                    p["high"] = row["last"]
                if price < Decimal(p["low"]):
                    p["low"] = row["last"]
                rank = ranks.get(p["symbol"])
                if rank is not None and (p["best_rank"] is None or rank < p["best_rank"]):
                    p["best_rank"] = rank
                for name, seconds in CHECKPOINTS:
                    if age >= seconds and name not in p["checkpoints"]:
                        p["checkpoints"][name] = row["last"]
                        if bench is not None:
                            p["benchmark_checkpoints"][name] = bench
            if age >= s["follow_hours"] * 3600:
                self._close(p)
                closed += 1
            else:
                still_open.append(p)
        self._save_open(still_open)
        return {"recorded": recorded, "closed": closed, "open": len(still_open)}

    def _close(self, p):
        s = self.settings
        max_gain = _pct(p["high"], p["price"])
        later = {name: _pct(price, p["price"]) for name, price in p["checkpoints"].items()}
        bench = {name: _pct(price, p["benchmark_price"]) for name, price in p["benchmark_checkpoints"].items()
                 if p.get("benchmark_price")}
        if max_gain is not None and max_gain >= Decimal(s["early_gain_pct"]):
            verdict = "early"
        elif max_gain is not None and max_gain <= Decimal(s["late_max_gain_pct"]) and (later.get("1h") or 0) < 0:
            verdict = "late"
        else:
            verdict = "mixed"
        record = {key: p[key] for key in ("symbol", "base", "picked_at", "picked_epoch", "price", "change_today_pct",
                                          "signals", "score", "rank_at_pick", "best_rank")}
        record.update({"max_gain_pct": _fmt(max_gain), "max_drop_pct": _fmt(_pct(p["low"], p["price"])),
                       "change_pct": {k: _fmt(v) for k, v in later.items()},
                       "benchmark_change_pct": {k: _fmt(v) for k, v in bench.items()},
                       "became_top_gainer": p["best_rank"] is not None and p["best_rank"] <= s["top_n"],
                       "verdict": verdict})
        self.closed_path.parent.mkdir(parents=True, exist_ok=True)
        with self.closed_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---------------------------------------------------------------- the summary
    def summary(self):
        """Totals over every closed pick, by verdict and by signal, vs Bitcoin over the same windows."""
        records = self.closed()
        out = {"contract": "scanner_scorecard", "closed_picks": len(records), "open_picks": len(self._load_open()),
               "settings": {k: self.settings[k] for k in ("early_gain_pct", "late_max_gain_pct", "top_n", "follow_hours")},
               "verdicts": {"early": 0, "mixed": 0, "late": 0}, "became_top_gainer": 0,
               "average_change_pct": {}, "benchmark_average_change_pct": {}, "by_signal": {},
               "note": "Measures what happened after each pick. A few days of picks prove nothing."}
        for r in records:
            out["verdicts"][r["verdict"]] = out["verdicts"].get(r["verdict"], 0) + 1
            out["became_top_gainer"] += 1 if r.get("became_top_gainer") else 0
        for name, _ in CHECKPOINTS:
            for key, field in (("average_change_pct", "change_pct"), ("benchmark_average_change_pct", "benchmark_change_pct")):
                values = [Decimal(r[field][name]) for r in records if (r.get(field) or {}).get(name) is not None]
                out[key][name] = _fmt(sum(values, Decimal(0)) / len(values)) if values else None
        for r in records:
            for signal in r.get("signals") or []:
                group = out["by_signal"].setdefault(signal, {"picks": 0, "early": 0, "max_gain_total": Decimal(0)})
                group["picks"] += 1
                group["early"] += 1 if r["verdict"] == "early" else 0
                group["max_gain_total"] += Decimal(r["max_gain_pct"] or "0")
        for group in out["by_signal"].values():
            group["average_max_gain_pct"] = _fmt(group.pop("max_gain_total") / group["picks"])
        return out
