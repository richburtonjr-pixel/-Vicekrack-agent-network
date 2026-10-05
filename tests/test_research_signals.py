"""Step 27: rule-based research signals. Synthetic data only; no network, no credits.

Trigger examples are constructed by hand below (expected bars are worked out in comments),
not derived from the module under test.
"""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from vicekrack.trading.contracts import ROOT, validate_signal
from vicekrack.trading.errors import TradingError
from vicekrack.trading.indicators.store import load_indicator_config
from vicekrack.trading.market.store import MarketStore, load_market_config, make_adapter
from vicekrack.trading.signals import strategies as rules
from vicekrack.trading.signals.cli import main as signal_main
from vicekrack.trading.signals.engine import load_strategies, run_signals, validate_run, validate_signal_record
from vicekrack.trading.signals.store import SignalStore, load_signal_config
from vicekrack.trading.state import PaperAccount

NOW = "2026-10-05T12:00:00Z"
SESSION = {"timezone": "America/New_York", "start": "09:30", "end": "16:00"}


def ema(fast, slow, cooldown=0, expiry=1):
    return {"strategy": "ema_crossover", "version": "1.0",
            "params": {"fast": fast, "slow": slow, "cooldown_bars": cooldown, "expiry_bars": expiry}}


def vwap(cooldown=0, session=None):
    return {"strategy": "vwap_reclaim", "version": "1.0",
            "params": {"cooldown_bars": cooldown, "expiry_bars": 1, "session": session or SESSION}}


def breakout(lookback, cooldown=0, volume=None, expiry=1):
    return {"strategy": "breakout", "version": "1.0",
            "params": {"lookback": lookback, "volume_filter": volume, "cooldown_bars": cooldown, "expiry_bars": expiry}}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.market_config, _ = load_market_config()
        self.indicator_config, _ = load_indicator_config()
        self.store = MarketStore(self.root, clock=lambda: NOW)
        self.files = 0

    def dataset(self, bars, *, times=None, symbol="SYNTH1", start="2026-01-15T09:30:00"):
        """bars: (high, low, close, volume) strings; open = low. times: local New York (UTC-05:00) times."""
        self.files += 1
        lines = ["timestamp,open,high,low,close,volume"]
        first = datetime.fromisoformat(start)
        for i, (high, low, close, volume) in enumerate(bars):
            moment = datetime.fromisoformat(times[i]) if times else first + timedelta(minutes=5 * i)
            lines.append(f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}-05:00,{low},{high},{low},{close},{volume}")
        path = self.root / "inputs" / f"bars-{self.files}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        adapter = make_adapter("csv", config=self.market_config, file=str(path), symbol=symbol)
        return self.store.import_dataset(adapter, config=self.market_config, config_sha256="0" * 64, interval="5m",
                                         tz="America/New_York", label="synthetic")

    def flat(self, closes, volumes=None, **kwargs):
        volumes = volumes or ["100"] * len(closes)
        return self.dataset([(c, c, c, volumes[i]) for i, c in enumerate(closes)], **kwargs)

    def config(self, **definitions):
        return {"config_version": "1.0", "limits": {"max_strategies": 6, "max_evaluations": 100000, "max_cli_entries": 50},
                "strategies": {name.replace("_", "-"): d for name, d in definitions.items()}}

    def evaluate(self, dataset, config=None, names=None, market_config=None, **window):
        config = config or self.config(s=ema(2, 3))
        names = names or list(config["strategies"])
        return run_signals(dataset, load_strategies(names, config), market_config=market_config or self.market_config,
                           indicator_config=self.indicator_config, signal_config=config, created_at=NOW, **window)

    @staticmethod
    def outcomes(run, name=None):
        evaluation = run["evaluations"][0] if name is None else next(e for e in run["evaluations"] if e["strategy"] == name)
        return [(e["sequence"], e["outcome"], e["reason_codes"][0]) for e in evaluation["entries"]]

    @staticmethod
    def triggered(run, name=None):
        return [s for s, o, _ in Base.outcomes(run, name) if o == "triggered"]

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(TradingError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception)
        return caught.exception


# ---------------------------------------------------------------- pure rule boundaries
def item(close, high=None, volume="100", run=5, total=5, session="d", inside=True, points=None):
    return {"close": Decimal(close), "high": Decimal(high or close), "volume": Decimal(volume), "run": run,
            "total": total, "session": session, "inside": inside, "points": points or {}}


def ready(value):
    return {"status": "ready", "value": value, "reason_codes": []}


UNREADY = {"status": "unavailable", "value": None, "reason_codes": ["warming_up"]}


class RuleBoundaryTests(unittest.TestCase):
    keys = {"fast": "f", "slow": "s"}

    def cross(self, previous, current):
        history = [item("1", points={"f": ready(previous[0]), "s": ready(previous[1])}),
                   item("1", points={"f": ready(current[0]), "s": ready(current[1])})]
        return rules.ema_crossover({}, history, self.keys)[:2]

    def test_ema_comparisons(self):
        self.assertEqual(self.cross(("9", "10"), ("10.00000001", "10")), ("triggered", ["crossed_above"]))
        self.assertEqual(self.cross(("10", "10"), ("10.1", "10")), ("triggered", ["crossed_above"]))   # equal before: inclusive
        self.assertEqual(self.cross(("9", "10"), ("10", "10")), ("not_triggered", ["no_cross"]))        # equal now: strict
        self.assertEqual(self.cross(("11", "10"), ("12", "10")), ("not_triggered", ["already_above"]))
        history = [item("1", points={"f": UNREADY, "s": ready("1")}), item("1", points={"f": ready("2"), "s": ready("1")})]
        self.assertEqual(rules.ema_crossover({}, history, self.keys)[:2], ("not_ready", ["input_unavailable", "indicator_warming_up"]))

    def reclaim(self, previous, current, **current_kwargs):
        keys = {"vwap": "v"}
        history = [item(previous[0], points={"v": ready(previous[1])}),
                   item(current[0], points={"v": ready(current[1])}, **current_kwargs)]
        return rules.vwap_reclaim({}, history, keys)[:2]

    def test_vwap_comparisons(self):
        self.assertEqual(self.reclaim(("9", "10"), ("10.01", "10")), ("triggered", ["reclaimed_vwap"]))
        self.assertEqual(self.reclaim(("10", "10"), ("10.01", "10")), ("triggered", ["reclaimed_vwap"]))  # at VWAP before
        self.assertEqual(self.reclaim(("9", "10"), ("10", "10")), ("not_triggered", ["at_or_below_vwap"]))
        self.assertEqual(self.reclaim(("11", "10"), ("12", "10")), ("not_triggered", ["already_above_vwap"]))
        self.assertEqual(self.reclaim(("9", "10"), ("11", "10"), session="other"), ("not_ready", ["no_prior_bar_in_session"]))
        self.assertEqual(self.reclaim(("9", "10"), ("11", "10"), inside=False), ("not_ready", ["outside_session"]))
        self.assertEqual(self.reclaim(("9", "10"), ("11", "10"), run=1), ("not_ready", ["gap_invalidated"]))

    def test_breakout_comparisons(self):
        params = {"lookback": 2, "volume_filter": None}
        base = [item("9", "10"), item("10", "11"), item("10", "10.5")]           # run/total default to 5
        self.assertEqual(rules.breakout(params, base + [item("11.01", "12")], {})[:2], ("triggered", ["closed_above_level"]))
        self.assertEqual(rules.breakout(params, base + [item("11", "12")], {})[:2], ("not_triggered", ["below_or_at_level"]))
        self.assertEqual(rules.breakout(params, base[:2] + [item("11.5", "12")] + [item("12.5", "13")], {})[:2],
                         ("not_triggered", ["already_above_level"]))
        self.assertEqual(rules.breakout(params, base + [item("12", run=3, total=3)], {})[:2], ("not_ready", ["insufficient_history"]))
        self.assertEqual(rules.breakout(params, base + [item("12", run=3, total=9)], {})[:2], ("not_ready", ["gap_invalidated"]))

    def test_volume_filter_boundaries(self):
        params = {"lookback": 2, "volume_filter": {"period": 2, "multiplier": "1.5"}}
        keys = {"volume": "vol"}

        def check(volume, average):
            history = [item("9", "10"), item("10", "11"), item("10", "10.5", points={"vol": average}),
                       item("11.01", "12", volume=volume)]
            return rules.breakout(params, history, keys)[:2]
        self.assertEqual(check("150", ready("100")), ("triggered", ["closed_above_level", "volume_filter_passed"]))  # inclusive
        self.assertEqual(check("149.99999999", ready("100")), ("not_triggered", ["volume_filter_failed"]))
        self.assertEqual(check("500", ready("0")), ("not_ready", ["volume_average_zero"]))
        self.assertEqual(check("500", UNREADY), ("not_ready", ["volume_average_unavailable", "indicator_warming_up"]))


# ---------------------------------------------------------------- end-to-end examples
class TriggerExampleTests(Base):
    def test_ema_crossover_example(self):
        # closes 10, 9, 8, 9, 11 -> EMA2: -, 9.5, 8.5, 8.8333.., 10.2777..; EMA3: -, -, 9, 9, 10.
        # bar 4: 8.83 <= 9 and 8.83 <= 9 (no cross); bar 5: prev 8.83 <= 9 and now 10.28 > 10 -> trigger.
        result = self.evaluate(self.flat(["10", "9", "8", "9", "11"]))
        self.assertEqual(self.outcomes(result), [(1, "not_ready", "insufficient_history"), (2, "not_ready", "input_unavailable"),
                                                 (3, "not_ready", "input_unavailable"), (4, "not_triggered", "no_cross"),
                                                 (5, "triggered", "crossed_above")])
        signal = result["signals"][0]
        self.assertEqual(signal["supporting_values"], {"fast": "10.27777778", "slow": "10", "previous_fast": "8.83333333",
                                                       "previous_slow": "9"})
        self.assertEqual((signal["event"], signal["bar"]["sequence"]), ("ema_cross_above", 5))
        # bar 5 starts 09:50 New York (14:50Z), closes 14:55Z; expiry_bars 1 -> valid until 15:00Z.
        self.assertEqual((signal["bar"]["available_at_utc"], signal["expires_at_utc"]), ("2026-01-15T14:55:00Z", "2026-01-15T15:00:00Z"))
        self.assertFalse(signal["expired_when_detected"])

    def test_vwap_reclaim_example(self):
        # h = l = c so typical price = close; equal volumes -> VWAP = running mean.
        # closes 10, 8, 10: VWAP 10, 9, 9.333..; bar 2: 8 <= 9 (below); bar 3: prev 8 <= 9 and 10 > 9.33 -> trigger.
        result = self.evaluate(self.flat(["10", "8", "10", "11"]), self.config(v=vwap()))
        self.assertEqual(self.outcomes(result), [(1, "not_ready", "no_prior_bar_in_session"), (2, "not_triggered", "at_or_below_vwap"),
                                                 (3, "triggered", "reclaimed_vwap"), (4, "not_triggered", "already_above_vwap")])
        self.assertEqual(result["signals"][0]["supporting_values"]["vwap"], "9.33333333")

    def test_breakout_example(self):
        # lookback 2: bar 4 level = max(11, 10.5) = 11, close 11.5 > 11; bar 3 close 10 <= max(10, 11) -> trigger.
        bars = [("10", "9", "9", "100"), ("11", "10", "10", "100"), ("10.5", "10", "10", "100"),
                ("12", "11", "11.5", "100"), ("12.6", "12", "12.5", "100")]
        result = self.evaluate(self.dataset(bars), self.config(b=breakout(2)))
        self.assertEqual(self.outcomes(result), [(1, "not_ready", "insufficient_history"), (2, "not_ready", "insufficient_history"),
                                                 (3, "not_ready", "insufficient_history"), (4, "triggered", "closed_above_level"),
                                                 (5, "not_triggered", "already_above_level")])
        self.assertEqual(result["signals"][0]["supporting_values"]["level"], "11")

    def test_breakout_volume_filter_end_to_end(self):
        bars = [("10", "9", "9", "100"), ("11", "10", "10", "100"), ("10.5", "10", "10", "100"),
                ("12", "11", "11.5", "149")]
        failed = self.evaluate(self.dataset(bars), self.config(b=breakout(2, volume={"period": 2, "multiplier": "1.5"})))
        self.assertEqual(self.outcomes(failed)[-1], (4, "not_triggered", "volume_filter_failed"))
        passed = self.evaluate(self.dataset(bars[:3] + [("12", "11", "11.5", "150")]),
                          self.config(b=breakout(2, volume={"period": 2, "multiplier": "1.5"})))
        self.assertEqual(self.outcomes(passed)[-1], (4, "triggered", "closed_above_level"))
        self.assertEqual(passed["signals"][0]["supporting_values"]["volume_threshold"], "150")
        zero = self.evaluate(self.dataset([(h, low, c, "0") for h, low, c, _ in bars]),
                        self.config(b=breakout(2, volume={"period": 2, "multiplier": "1.5"})))
        self.assertEqual(self.outcomes(zero)[-1], (4, "not_ready", "volume_average_zero"))


class WarmupGapSessionTests(Base):
    def test_never_triggers_while_not_ready(self):
        # Bars 1-3: EMA3 is not ready on the previous bar -> not_ready. Bar 4: prev 3.5 <= 4, now 7.17 > 6.5 -> trigger.
        result = self.evaluate(self.flat(["5", "4", "3", "9"]), self.config(s=ema(2, 3)))
        self.assertEqual([o for _, o, _ in self.outcomes(result)], ["not_ready", "not_ready", "not_ready", "triggered"])
        # A rule whose inputs never become ready never triggers, however the prices move.
        never = self.evaluate(self.flat(["5", "4", "3", "9", "20", "1", "30"]), self.config(s=ema(8, 9)))
        self.assertEqual(self.triggered(never), [])

    def test_gap_invalidates_inputs(self):
        # A breakout pattern that only exists across the missing 09:45 bar must not trigger.
        times = ["2026-01-15T09:30:00", "2026-01-15T09:35:00", "2026-01-15T09:40:00", "2026-01-15T09:50:00",
                 "2026-01-15T09:55:00", "2026-01-15T10:00:00", "2026-01-15T10:05:00"]
        bars = [("10", "9", "9", "100"), ("10", "9", "9", "100"), ("10", "9", "9", "100"), ("12", "11", "11.5", "100"),
                ("12", "11", "11", "100"), ("12", "11", "11", "100"), ("13", "12", "12.5", "100")]
        result = self.evaluate(self.dataset(bars, times=times), self.config(b=breakout(2), e=ema(1, 2)))
        self.assertEqual(self.outcomes(result, "b")[3:], [(4, "not_ready", "gap_invalidated"), (5, "not_ready", "gap_invalidated"),
                                                          (6, "not_ready", "gap_invalidated"), (7, "triggered", "closed_above_level")])
        self.assertEqual(self.outcomes(result, "e")[3], (4, "not_ready", "gap_invalidated"))
        self.assertNotIn(4, self.triggered(result, "b"))

    def test_session_resets_and_outside_bars(self):
        times = ["2026-01-15T09:25:00", "2026-01-15T09:30:00", "2026-01-15T09:35:00", "2026-01-16T09:30:00",
                 "2026-01-16T09:35:00"]
        result = self.evaluate(self.flat(["20", "8", "10", "9", "12"], times=times), self.config(v=vwap()))
        self.assertEqual(self.outcomes(result), [(1, "not_ready", "outside_session"), (2, "not_ready", "no_prior_bar_in_session"),
                                                 (3, "triggered", "reclaimed_vwap"), (4, "not_ready", "no_prior_bar_in_session"),
                                                 (5, "triggered", "reclaimed_vwap")])

    def test_custom_session_and_daily_rejection(self):
        times = ["2026-01-15T08:00:00", "2026-01-15T08:05:00", "2026-01-15T08:10:00"]
        early = {"timezone": "America/New_York", "start": "08:00", "end": "08:15"}
        dataset = self.flat(["10", "8", "10"], times=times)
        result = self.evaluate(dataset, self.config(v=vwap(session=early)))
        self.assertEqual(self.triggered(result), [3])
        default = self.evaluate(dataset, self.config(v=vwap()))
        self.assertEqual({o for _, o, _ in self.outcomes(default)}, {"not_ready"})
        daily = self.store.import_dataset(make_adapter("synthetic", config=self.market_config, fixture="synth1-1d-dst"),
                                          config=self.market_config, config_sha256="0" * 64)
        self.assertCode("invalid_strategy_config", self.evaluate, daily, self.config(v=vwap()))
        self.assertEqual(len(self.evaluate(daily, self.config(b=breakout(1)))["evaluations"][0]["entries"]), 5)


class CooldownDuplicateIsolationTests(Base):
    # lookback 1: transitions at bars 3, 5 and 7 (close > previous high, previous close not above its level).
    BARS = [("10", "10", "10", "1"), ("10", "9", "9", "1"), ("11", "11", "11", "1"), ("11", "10", "10", "1"),
            ("12", "12", "12", "1"), ("12", "11", "11", "1"), ("13", "13", "13", "1")]

    def test_cooldown_counts_closed_bars(self):
        dataset = self.dataset(self.BARS)
        self.assertEqual(self.triggered(self.evaluate(dataset, self.config(b=breakout(1)))), [3, 5, 7])
        cooled = self.evaluate(dataset, self.config(b=breakout(1, cooldown=2)))
        self.assertEqual(self.triggered(cooled), [3, 7])
        self.assertEqual(self.outcomes(cooled)[4], (5, "not_triggered", "closed_above_level"))
        self.assertIn("cooldown_active", cooled["evaluations"][0]["entries"][4]["reason_codes"])
        self.assertEqual(cooled["summary"]["cooldown_suppressed"], 1)
        self.assertEqual(self.triggered(self.evaluate(dataset, self.config(b=breakout(1, cooldown=4)))), [3])

    def test_duplicate_prevention(self):
        dataset = self.dataset(self.BARS)
        config = self.config(b=breakout(1))
        one, two = self.evaluate(dataset, config), self.evaluate(dataset, config)
        self.assertEqual([s["signal_id"] for s in one["signals"]], [s["signal_id"] for s in two["signals"]])
        store = SignalStore(self.root)
        saved = store.save_run(one)
        self.assertEqual(len(saved["new_signals"]), 3)
        self.assertCode("run_exists", store.save_run, two)
        coarse = self.evaluate(dataset, config, step_seconds=900)                 # different run, same bars
        again = store.save_run(coarse)
        self.assertEqual((again["new_signals"], sorted(again["already_recorded"])), ([], sorted(saved["new_signals"])))
        self.assertEqual(len(store.list_signals()), 3)

    def test_configuration_isolation(self):
        dataset = self.flat(["10", "9", "8", "9", "11", "10", "12", "13"])
        alone = self.evaluate(dataset, self.config(a=ema(2, 3)))
        mixed = self.evaluate(dataset, self.config(a=ema(2, 3), b=ema(1, 2), c=breakout(1)))
        self.assertEqual(alone["evaluations"][0], mixed["evaluations"][0])
        renamed = self.evaluate(dataset, self.config(z=ema(2, 3)))
        self.assertNotEqual(alone["signals"][0]["signal_id"], renamed["signals"][0]["signal_id"])
        tweaked = self.evaluate(dataset, self.config(a=ema(2, 3, cooldown=1)))
        self.assertNotEqual(alone["signals"][0]["signal_id"], tweaked["signals"][0]["signal_id"])
        other = self.flat(["10", "9", "8", "9", "11", "10", "12", "13"], symbol="SYNTH2")
        self.assertNotEqual(self.evaluate(other, self.config(a=ema(2, 3)))["signals"][0]["signal_id"], alone["signals"][0]["signal_id"])

    def test_config_validation(self):
        dataset = self.flat(["1", "2"])
        self.assertCode("unknown_strategy", load_strategies, ["nope"], self.config(a=ema(2, 3)))
        self.assertCode("invalid_strategy_config", load_strategies, ["a", "a"], self.config(a=ema(2, 3)))
        self.assertCode("invalid_strategy_config", load_strategies, ["a"], self.config(a=ema(3, 3)))
        self.assertCode("invalid_strategy_config", load_strategies, ["a"],
                        self.config(a=breakout(2, volume={"period": 2, "multiplier": "0"})))
        self.assertCode("invalid_strategy_config", load_strategies, ["a", "b"],
                        self.config(a=vwap(), b=vwap(session={"timezone": "UTC", "start": "14:30", "end": "21:00"})))
        bad = self.config(a=ema(2, 3))
        bad["strategies"]["a"]["params"]["cooldown_bars"] = -1
        self.assertRaises(TradingError, load_strategies, ["a"], bad)
        self.assertCode("invalid_strategy_config", load_strategies, [], self.config(a=ema(2, 3)))
        self.assertEqual(len(self.evaluate(dataset)["signals"]), 0)


class ReplayIntegrityTests(Base):
    def test_deterministic(self):
        dataset = self.flat(["10", "9", "8", "9", "11", "10", "12", "13", "12", "14"])
        config = self.config(e=ema(2, 3), b=breakout(2), v=vwap())
        one, two = self.evaluate(dataset, config), self.evaluate(dataset, config)
        self.assertEqual((one["run_id"], one["results_sha256"]), (two["run_id"], two["results_sha256"]))
        coarse = self.evaluate(dataset, config, step_seconds=1500)                # five bars arrive per step
        for name in ("e", "b", "v"):
            self.assertEqual(self.outcomes(coarse, name), self.outcomes(one, name))
        self.assertEqual([s["signal_id"] for s in coarse["signals"]], [s["signal_id"] for s in one["signals"]])
        # Seen late by a coarse step: same signal, honestly flagged as already expired on detection.
        self.assertFalse(any(s["expired_when_detected"] for s in one["signals"]))
        self.assertTrue(any(s["expired_when_detected"] for s in coarse["signals"]))

    def test_no_future_data(self):
        closes = ["10", "9", "8", "9", "11", "10", "12", "13", "12", "14", "11", "15"]
        config = self.config(e=ema(2, 3), b=breakout(2), v=vwap())
        full = self.evaluate(self.flat(closes), config)
        for cut in (5, 8, 10):
            partial = self.evaluate(self.flat(closes[:cut]), config)
            for name in ("e", "b", "v"):
                with self.subTest(cut=cut, name=name):
                    self.assertEqual(self.outcomes(partial, name), self.outcomes(full, name)[:cut])
        for record in full["signals"]:
            self.assertLessEqual(record["bar"]["available_at_utc"], record["detected_at_sim_utc"])
        for evaluation in full["evaluations"]:
            for entry in evaluation["entries"]:
                self.assertLess(entry["timestamp_utc"], entry["computed_at_sim_utc"])
        self.assertEqual(full["summary"]["future_access_attempts"], 0)

    def test_window_too_small_stops(self):
        market = deepcopy(self.market_config)
        market["replay"]["max_window_bars"] = 2
        dataset = self.flat([str(i) for i in range(1, 9)])
        self.assertCode("indicator_window_too_small", self.evaluate, dataset, market_config=market, step_seconds=900)


class TamperAndBoundaryTests(Base):
    def saved(self):
        dataset = self.dataset(CooldownDuplicateIsolationTests.BARS)
        run = self.evaluate(dataset, self.config(b=breakout(1)))
        SignalStore(self.root).save_run(run)
        return dataset, run

    def test_tampered_records_and_runs_rejected(self):
        dataset, run = self.saved()
        store = SignalStore(self.root)
        record_path = store.records / f"{run['signals'][0]['signal_id']}.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["supporting_values"]["close"] = "99"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        self.assertCode("signal_corrupt", store.load_signal, record["signal_id"])
        run_path = store.runs / f"{run['run_id']}.json"
        changed = deepcopy(run)
        changed["evaluations"][0]["entries"][0]["outcome"] = "triggered"
        run_path.write_text(json.dumps(changed), encoding="utf-8")
        self.assertCode("signal_run_corrupt", store.load_run, run["run_id"])
        forged = deepcopy(run)
        forged["signals"][0]["dataset"]["symbol"] = "OTHER"
        self.assertRaises(TradingError, validate_run, forged)
        self.assertRaises(TradingError, validate_signal_record, dict(run["signals"][0], authorization_possible=True))
        self.assertCode("signal_conflict", store.save_run, self._conflicting(run, dataset))

    def _conflicting(self, run, dataset):
        """A new run (different window) whose already-stored signal has different content."""
        other = self.evaluate(dataset, self.config(b=breakout(1)), step_seconds=900)
        store = SignalStore(self.root)
        path = store.records / f"{other['signals'][0]['signal_id']}.json"
        stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if stored is not None:
            from vicekrack.trading.signals.engine import content_hash
            stored["reason_codes"] = ["closed_above_level", "volume_filter_passed"]
            stored["content_sha256"] = content_hash(stored)
            path.write_text(json.dumps(stored), encoding="utf-8")
        return other

    def test_tampered_dataset_rejected_by_cli(self):
        dataset, _ = self.saved()
        path = self.root / "runtime/trading/market/datasets" / f"{dataset['dataset_id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["bars"][2]["close"] = "10.5"
        path.write_text(json.dumps(data), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            signal_main(["signal-run", dataset["dataset_id"], "--strategy", "breakout-3"], root=self.root)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "dataset_corrupt")

    def test_interrupted_writes(self):
        dataset = self.dataset(CooldownDuplicateIsolationTests.BARS)
        run = self.evaluate(dataset, self.config(b=breakout(1)))
        store = SignalStore(self.root)
        with patch("vicekrack.trading.signals.store.os.link", side_effect=OSError("disk /private")):
            error = self.assertCode("signal_write_failed", store.save_run, run)
        self.assertNotIn("/private", str(error))
        self.assertEqual((store.list_runs(), store.list_signals()), ([], []))
        real_link = __import__("os").link

        def records_only(source, target):
            if "/runs/" in str(target).replace("\\", "/"):
                raise OSError("crash before the run file")
            return real_link(source, target)
        with patch("vicekrack.trading.signals.store.os.link", side_effect=records_only):
            self.assertCode("signal_write_failed", store.save_run, run)
        self.assertEqual(len(store.list_signals()), 3)
        result = store.save_run(run)                                         # rerun after the crash
        self.assertEqual((result["new_signals"], len(result["already_recorded"])), ([], 3))

    def test_research_signals_cannot_authorize_or_touch_accounts(self):
        PaperAccount("sig-guard", root=self.root, clock=lambda: NOW).initialize()
        state = self.root / "runtime/trading/accounts/acct-sig-guard/state.json"
        before = state.read_bytes()
        dataset, run = self.saved()
        self.assertEqual(state.read_bytes(), before)
        record = run["signals"][0]
        self.assertEqual((record["authorization_possible"], record["account_access"], record["purpose"]),
                         (False, False, "research_only"))
        self.assertRaises(TradingError, validate_signal, record)              # not an order-type trading_signal
        self.assertNotIn("proposal", record)
        for path in (ROOT / "vicekrack/trading/signals").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in (".state", ".risk", ".orders", ".journal", "PaperAccount", "build_intent", "authorize(",
                              "socket", "urllib", "requests", "http.client", "subprocess", "threading", "while True"):
                with self.subTest(file=path.name, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)


class CliTests(Base):
    def cli(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = signal_main(list(argv), root=self.root)
        return code, json.loads(output.getvalue())

    def test_commands(self):
        dataset = self.store.import_dataset(make_adapter("synthetic", config=self.market_config, fixture="synth1-5m"),
                                            config=self.market_config, config_sha256="0" * 64)
        code, run = self.cli("signal-run", dataset["dataset_id"], "--strategy", "vwap-reclaim", "--strategy", "breakout-3",
                             "--entries", "2")
        self.assertEqual((code, run["saved"], run["authorization_possible"]), (0, False, False))
        self.assertEqual(len(run["evaluations"][0]["latest"]), 2)
        self.assertEqual(self.cli("signal-list")[1]["runs"], [])
        code, saved = self.cli("signal-run", dataset["dataset_id"], "--strategy", "vwap-reclaim", "--strategy", "breakout-3", "--save")
        self.assertEqual((code, len(saved["saved"]["new_signals"])), (0, 2))
        self.assertEqual(self.cli("signal-run", dataset["dataset_id"], "--strategy", "vwap-reclaim", "--strategy",
                                  "breakout-3", "--save")[1]["error"]["code"], "run_exists")
        code, shown = self.cli("signal-inspect", saved["run_id"], "--strategy", "breakout-3", "--outcome", "triggered")
        self.assertEqual((code, shown["evaluations"][0]["matching_entries"]), (0, 1))
        signal_id = self.cli("signal-list", "--signals")[1]["signals"][0]["signal_id"]
        self.assertEqual(self.cli("signal-inspect", signal_id)[1]["signal_id"], signal_id)
        self.assertIn("vwap-reclaim", self.cli("signal-list", "--strategies")[1]["strategies"])
        self.assertEqual(self.cli("signal-run", dataset["dataset_id"], "--strategy", "nope")[1]["error"]["code"], "unknown_strategy")
        self.assertEqual(self.cli("signal-run", dataset["dataset_id"], "--strategy", "breakout-3", "--entries", "999")[1]["error"]["code"],
                         "invalid_entry_count")
        self.assertEqual(self.cli("signal-inspect", "rsr-bad")[1]["error"]["code"], "invalid_run_id")

    def test_shipped_config_is_valid(self):
        config, _ = load_signal_config()
        self.assertEqual(len(load_strategies(list(config["strategies"])[:5], config)), 5)


if __name__ == "__main__":
    unittest.main()
