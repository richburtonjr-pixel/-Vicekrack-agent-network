"""Step 26: deterministic offline indicators. Synthetic data only; no network, no credits.

Reference values are computed independently here with exact rational arithmetic
(fractions.Fraction) or by hand, not with the module under test.
"""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from vicekrack.trading.contracts import ROOT
from vicekrack.trading.errors import TradingError
from vicekrack.trading.indicators.cli import main as indicator_main
from vicekrack.trading.indicators.engine import build_settings, calculate, validate_result
from vicekrack.trading.indicators.store import IndicatorStore, load_indicator_config
from vicekrack.trading.market.store import MarketStore, load_market_config, make_adapter
from vicekrack.trading.state import PaperAccount

NOW = "2026-10-05T12:00:00Z"


# ---------------------------------------------------------------- independent references
def round8(value):
    """Exact half-even rounding of a Fraction to 8 places, formatted like the module (no trailing zeros)."""
    scaled = value * 10 ** 8
    whole = scaled.numerator // scaled.denominator
    remainder = scaled - whole
    if remainder > Fraction(1, 2) or (remainder == Fraction(1, 2) and whole % 2):
        whole += 1
    sign = "-" if whole < 0 else ""
    digits = str(abs(whole)).rjust(9, "0")
    text = f"{sign}{digits[:-8]}.{digits[-8:]}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def ref_ema(closes, n):
    out, value = [], None
    for i in range(len(closes)):
        if i + 1 < n:
            out.append(None)
            continue
        if i + 1 == n:
            value = sum(closes[:n], Fraction(0)) / n
        else:
            value = Fraction(2, n + 1) * closes[i] + (1 - Fraction(2, n + 1)) * value
        out.append(value)
    return out


def ref_rsi(closes, n):
    out, ag, al = [None], None, None
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    for k in range(1, len(closes)):
        if k < n:
            out.append(None)
            continue
        if k == n:
            ag, al = Fraction(sum(gains[:n]), n), Fraction(sum(losses[:n]), n)
        else:
            ag, al = (ag * (n - 1) + gains[k - 1]) / n, (al * (n - 1) + losses[k - 1]) / n
        out.append(None if al == 0 and ag == 0 else Fraction(100) if al == 0 else 100 - Fraction(100) / (1 + ag / al))
    return out


def ref_sma(values, n):
    return [None if i + 1 < n else sum(values[i + 1 - n:i + 1], Fraction(0)) / n for i in range(len(values))]


def ref_vwap(rows):
    pv = v = Fraction(0)
    out = []
    for h, low, c, vol in rows:
        pv += (h + low + c) / 3 * vol
        v += vol
        out.append(None if v == 0 else pv / v)
    return out


def values(result, key):
    return [p["value"] for p in next(s for s in result["series"] if s["key"] == key)["points"]]


def reasons(result, key):
    return [p["reason_codes"] for p in next(s for s in result["series"] if s["key"] == key)["points"]]


def points(result, key):
    return next(s for s in result["series"] if s["key"] == key)["points"]


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.market_config, _ = load_market_config()
        self.config, _ = load_indicator_config()
        self.store = MarketStore(self.root, clock=lambda: NOW)
        self.files = 0

    def dataset(self, bars, *, start="2026-01-15T09:30:00", interval_minutes=5, symbol="SYNTH1", interval="5m",
                times=None):
        """bars: list of (open, high, low, close, volume) strings; times: optional local NY times."""
        self.files += 1
        first = datetime.fromisoformat(start)
        lines = ["timestamp,open,high,low,close,volume"]
        for i, bar in enumerate(bars):
            moment = datetime.fromisoformat(times[i]) if times else first + timedelta(minutes=interval_minutes * i)
            lines.append(moment.strftime("%Y-%m-%dT%H:%M:%S") + "-05:00," + ",".join(bar))
        path = self.root / "inputs" / f"bars-{self.files}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        adapter = make_adapter("csv", config=self.market_config, file=str(path), symbol=symbol)
        return self.store.import_dataset(adapter, config=self.market_config, config_sha256="0" * 64, interval=interval,
                                         tz="America/New_York", label="synthetic")

    def closes_dataset(self, closes, volumes=None, **kwargs):
        volumes = volumes or ["100"] * len(closes)
        return self.dataset([(c, c, c, c, volumes[i]) for i, c in enumerate(closes)], **kwargs)

    def calc(self, dataset, config=None, market_config=None, start=None, end=None, step_seconds=None, **settings):
        config = config or self.config
        built = build_settings(dataset, config=config, **settings)
        return calculate(dataset, built, market_config=market_config or self.market_config, indicator_config=config,
                         created_at=NOW, start=start, end=end, step_seconds=step_seconds)

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(TradingError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception)
        return caught.exception


class HandCalculatedTests(Base):
    def test_ema_by_hand(self):
        result = self.calc(self.closes_dataset(["1", "2", "3", "4", "5", "6"]), ema=[3])
        self.assertEqual(values(result, "ema_3"), [None, None, "2", "3", "4", "5"])

    def test_wilder_rsi_by_hand(self):
        # changes +1, -0.5, +1, -0.5 -> RS 2, 6, 1.2
        result = self.calc(self.closes_dataset(["10", "11", "10.5", "11.5", "11"]), rsi=[2])
        self.assertEqual(values(result, "rsi_2"), [None, None, "66.66666667", "85.71428571", "54.54545455"])

    def test_vwap_and_volume_average_by_hand(self):
        result = self.calc(self.dataset([("9", "10", "8", "9", "100"), ("11", "12", "10", "11", "300"),
                                         ("11", "11", "11", "11", "500")]), vwap=True, volume_sma=[2])
        # tp 9, 11, 11 -> (900 + 3300) / 400 = 10.5 ; (4200 + 5500) / 900 = 10.77777778
        self.assertEqual(values(result, "vwap_session"), ["9", "10.5", "10.77777778"])
        self.assertEqual(values(result, "volume_sma_2"), [None, "200", "400"])


class ReferenceTests(Base):
    def series(self, count=40):
        seed, rows = 12345, []
        price = Fraction(5000, 100)
        for i in range(count):
            seed = (seed * 1103515245 + 12345) % 2 ** 31
            step = Fraction(seed % 2001 - 1000, 10 ** (2 + seed % 7))           # 2-8 decimal places
            close = max(price + step, Fraction(1, 100))
            spread = Fraction(seed % 97, 10 ** 4)
            high, low = max(price, close) + spread, min(price, close) - spread / 2
            volume = Fraction(seed % 5000)
            rows.append((price, high, low, close, volume))
            price = close
        return rows

    def test_matches_independent_fraction_reference(self):
        rows = self.series()
        dataset = self.dataset([tuple(_decimal_text(x) for x in row) for row in rows])
        result = self.calc(dataset, ema=[5], rsi=[14], volume_sma=[4], vwap=True)
        closes = [r[3] for r in rows]
        expected = {
            "ema_5": ref_ema(closes, 5), "rsi_14": ref_rsi(closes, 14), "volume_sma_4": ref_sma([r[4] for r in rows], 4),
            "vwap_session": ref_vwap([(r[1], r[2], r[3], r[4]) for r in rows]),
        }
        for key, reference in expected.items():
            with self.subTest(key):
                self.assertEqual(values(result, key), [None if v is None else round8(v) for v in reference])


def _decimal_text(value):
    """Exact decimal text of a Fraction with a terminating decimal expansion (test helper)."""
    value = Fraction(value)
    for places in range(0, 21):
        scaled = value * 10 ** places
        if scaled.denominator == 1:
            digits = str(scaled.numerator).rjust(places + 1, "0")
            return digits if places == 0 else f"{digits[:-places]}.{digits[-places:]}"
    raise AssertionError("not a finite decimal")


class WarmupFlatZeroTests(Base):
    def test_warmup_is_unavailable_never_zero(self):
        result = self.calc(self.closes_dataset(["5"] * 4), ema=[3], rsi=[3], volume_sma=[3])
        for key in ("ema_3", "rsi_3", "volume_sma_3"):
            first = points(result, key)[0]
            self.assertEqual((first["status"], first["value"], first["reason_codes"]), ("unavailable", None, ["warming_up"]))
        self.assertEqual([p["bars_since_reset"] for p in points(result, "ema_3")], [1, 2, 3, 4])
        self.assertNotIn("0", [v for v in values(result, "ema_3") if v is not None])

    def test_flat_prices_and_zero_loss_rsi(self):
        flat = self.calc(self.closes_dataset(["7.25"] * 5), rsi=[2], ema=[2])
        self.assertEqual(values(flat, "rsi_2"), [None] * 5)
        self.assertEqual(reasons(flat, "rsi_2")[2:], [["flat_prices"]] * 3)
        self.assertEqual(values(flat, "ema_2")[1:], ["7.25"] * 4)
        rising = self.calc(self.closes_dataset(["1", "2", "3", "4"]), rsi=[2])
        self.assertEqual(values(rising, "rsi_2"), [None, None, "100", "100"])
        self.assertEqual(reasons(rising, "rsi_2")[2], ["no_losses"])
        falling = self.calc(self.closes_dataset(["4", "3", "2", "1"]), rsi=[2])
        self.assertEqual(values(falling, "rsi_2")[2:], ["0", "0"])           # a real 0 (all losses), not a substitute
        self.assertEqual(reasons(falling, "rsi_2")[2:], [[], []])

    def test_zero_volume(self):
        result = self.calc(self.closes_dataset(["10", "11", "12"], volumes=["0", "0", "50"]), vwap=True, volume_sma=[2])
        self.assertEqual(values(result, "vwap_session"), [None, None, "12"])
        self.assertEqual(reasons(result, "vwap_session")[:2], [["zero_volume", "session_start"], ["zero_volume"]])
        self.assertEqual(values(result, "volume_sma_2"), [None, "0", "25"])     # an average of zeros is a real 0


class GapAndSessionTests(Base):
    def gap_dataset(self):
        times = ["2026-01-15T09:30:00", "2026-01-15T09:35:00", "2026-01-15T09:40:00",
                 "2026-01-15T09:50:00", "2026-01-15T09:55:00", "2026-01-15T10:00:00"]            # 09:45 missing
        rows = [(c, c, c, c, "100") for c in ("1", "2", "3", "4", "5", "6")]
        return self.dataset(rows, times=times)

    def test_reset_policy(self):
        result = self.calc(self.gap_dataset(), ema=[2], vwap=True)
        self.assertEqual(values(result, "ema_2"), [None, "1.5", "2.5", None, "4.5", "5.5"])
        self.assertEqual(points(result, "ema_2")[3]["bars_since_reset"], 1)
        self.assertEqual(values(result, "vwap_session")[3:], [None] * 3)
        self.assertEqual(reasons(result, "vwap_session")[3:], [["session_gap"]] * 3)
        self.assertEqual((result["summary"]["gaps_detected"], result["summary"]["warmup_resets"]), (1, 1))

    def test_continue_policy_flags_gap(self):
        result = self.calc(self.gap_dataset(), ema=[2], vwap=True, gap_policy="continue")
        closes = [Fraction(c) for c in range(1, 7)]
        self.assertEqual(values(result, "ema_2"), [None if v is None else round8(v) for v in ref_ema(closes, 2)])
        self.assertEqual(reasons(result, "ema_2")[3:], [["gap_ignored"]] * 3)
        self.assertEqual(reasons(result, "ema_2")[:3], [["warming_up"], [], []])
        self.assertEqual(values(result, "vwap_session")[-1], round8(Fraction(1 + 2 + 3 + 4 + 5 + 6, 6)))
        self.assertEqual(reasons(result, "vwap_session")[3:], [["gap_ignored"]] * 3)

    def test_sessions_reset_daily_and_exclude_outside_bars(self):
        times = ["2026-01-15T09:25:00", "2026-01-15T09:30:00", "2026-01-15T15:55:00", "2026-01-15T16:00:00",
                 "2026-01-16T09:30:00", "2026-01-16T09:35:00"]
        rows = [(c, c, c, c, "100") for c in ("10", "20", "30", "40", "50", "60")]
        result = self.calc(self.dataset(rows, times=times), vwap=True, ema=[2])
        self.assertEqual(values(result, "vwap_session"), [None, "20", None, None, "50", "55"])
        self.assertEqual(reasons(result, "vwap_session"),
                         [["outside_session"], ["session_start"], ["session_gap"], ["outside_session"],
                          ["session_start"], []])
        self.assertEqual(result["summary"]["sessions"], 2)
        # No calendar: the overnight break is a gap, so EMA warm-up restarts under `reset`.
        # 15:55 -> 16:00 is consecutive (no gap), so EMA continues there even though VWAP is outside the session.
        self.assertEqual(values(result, "ema_2"), [None, "15", None, "35", None, "55"])

    def test_custom_session_window(self):
        times = ["2026-01-15T08:00:00", "2026-01-15T08:05:00", "2026-01-15T08:10:00"]
        rows = [(c, c, c, c, "100") for c in ("10", "20", "30")]
        dataset = self.dataset(rows, times=times)
        result = self.calc(dataset, vwap=True, vwap_session={"timezone": "America/New_York", "start": "08:05", "end": "08:10"})
        self.assertEqual(reasons(result, "vwap_session"), [["outside_session"], ["session_start"], ["outside_session"]])
        utc = self.calc(dataset, vwap=True, vwap_session={"timezone": "UTC", "start": "13:00", "end": "13:15"})
        self.assertEqual(values(utc, "vwap_session"), ["10", "15", "20"])


class PrecisionAndValidationTests(Base):
    def test_decimal_rounding_is_half_even_at_eight_places(self):
        result = self.calc(self.closes_dataset(["0.00000001", "0.00000002", "0.00000003"]), ema=[2])
        # seed (1e-8 + 2e-8) / 2 = 1.5e-8 -> 2e-8 (half-even); then 2/3*3e-8 + 1/3*1.5e-8 = 2.5e-8 -> 2e-8
        self.assertEqual(values(result, "ema_2"), [None, "0.00000002", "0.00000002"])
        many = self.calc(self.closes_dataset(["1.12345678", "2.87654321", "3.00000001"]), ema=[3])
        expected = (Fraction("1.12345678") + Fraction("2.87654321") + Fraction("3.00000001")) / 3
        self.assertEqual(values(many, "ema_3")[-1], round8(expected))
        for point in points(many, "ema_3"):
            self.assertNotIn("e", (point["value"] or "").lower())

    def test_parameter_validation(self):
        dataset = self.closes_dataset(["1", "2"])
        bad = [dict(ema=[0]), dict(rsi=[1]), dict(volume_sma=[501]), dict(ema=[True]), dict(ema=["5"]),
               dict(ema=[3, 3]), dict(), dict(gap_policy="skip", ema=[2]),
               dict(vwap=True, vwap_session={"timezone": "Mars/Base", "start": "09:30", "end": "16:00"}),
               dict(vwap=True, vwap_session={"timezone": "UTC", "start": "16:00", "end": "09:30"}),
               dict(vwap=True, vwap_session={"timezone": "UTC", "start": "9:30", "end": "16:00"}),
               dict(vwap=True, vwap_session={"timezone": "UTC", "start": "09:30"}),
               dict(ema=list(range(2, 12)))]
        for settings in bad:
            with self.subTest(settings):
                with self.assertRaises(TradingError) as caught:
                    build_settings(dataset, config=self.config, **settings)
                self.assertIn(caught.exception.code, {"invalid_indicator_settings", "invalid_timezone"})
        daily = self.store.import_dataset(make_adapter("synthetic", config=self.market_config, fixture="synth1-1d-dst"),
                                          config=self.market_config, config_sha256="0" * 64)
        self.assertCode("invalid_indicator_settings", build_settings, daily, config=self.config, vwap=True)
        self.assertEqual(values(self.calc(daily, ema=[2]), "ema_2")[:2], [None, round8((Fraction("80.12") + Fraction("80.04")) / 2)])

    def test_point_limit(self):
        config = deepcopy(self.config)
        config["limits"]["max_points"] = 5
        self.assertCode("indicator_too_many_points", self.calc, self.closes_dataset(["1", "2", "3"]), config=config,
                        ema=[2], rsi=[2])


class ReplayIntegrityTests(Base):
    def test_deterministic(self):
        dataset = self.closes_dataset([str(10 + i % 5) for i in range(20)])
        one, two = self.calc(dataset, ema=[3], rsi=[5]), self.calc(dataset, ema=[3], rsi=[5])
        self.assertEqual((one["result_id"], one["results_sha256"]), (two["result_id"], two["results_sha256"]))
        coarse = self.calc(dataset, ema=[3], rsi=[5], step_seconds=900)           # 3 bars per step
        self.assertEqual(values(coarse, "ema_3"), values(one, "ema_3"))
        self.assertEqual(values(coarse, "rsi_5"), values(one, "rsi_5"))
        self.assertNotEqual(coarse["result_id"], one["result_id"])

    def test_no_lookahead(self):
        closes = [str(20 + (i * 7) % 11) for i in range(30)]
        full = self.calc(self.closes_dataset(closes), ema=[4], rsi=[6], volume_sma=[3], vwap=True)
        for cut in (8, 15, 22):
            partial = self.calc(self.closes_dataset(closes[:cut]), ema=[4], rsi=[6], volume_sma=[3], vwap=True)
            for key in ("ema_4", "rsi_6", "volume_sma_3", "vwap_session"):
                with self.subTest(cut=cut, key=key):
                    self.assertEqual([(p["value"], p["reason_codes"]) for p in points(partial, key)],
                                     [(p["value"], p["reason_codes"]) for p in points(full, key)][:cut])
        for series in full["series"]:
            for point in series["points"]:
                self.assertLessEqual(point["available_at_utc"], point["computed_at_sim_utc"])
        self.assertEqual(full["summary"]["future_access_attempts"], 0)

    def test_window_too_small_stops_instead_of_skipping(self):
        market = deepcopy(self.market_config)
        market["replay"]["max_window_bars"] = 2
        dataset = self.closes_dataset([str(i) for i in range(1, 10)])
        self.assertCode("indicator_window_too_small", self.calc, dataset, market_config=market, ema=[2], step_seconds=900)
        self.assertEqual(len(values(self.calc(dataset, market_config=market, ema=[2]), "ema_2")), 9)

    def test_isolation(self):
        a = self.closes_dataset(["1", "2", "3", "4", "5"], symbol="SYNTH1")
        b = self.closes_dataset(["50", "40", "30", "20", "10"], symbol="SYNTH2", interval="5m")
        alone = self.calc(a, ema=[2])
        combined = self.calc(a, ema=[2, 3], rsi=[2], volume_sma=[2])
        self.calc(b, ema=[2])
        again = self.calc(a, ema=[2])
        self.assertEqual(values(alone, "ema_2"), values(combined, "ema_2"))
        self.assertEqual(alone["results_sha256"], again["results_sha256"])
        self.assertNotEqual(alone["result_id"], combined["result_id"])
        self.assertNotEqual(alone["result_id"], self.calc(b, ema=[2])["result_id"])

    def test_replay_window(self):
        dataset = self.closes_dataset([str(i) for i in range(1, 9)])
        result = self.calc(dataset, ema=[2], start="2026-01-15T14:45:00Z", end="2026-01-15T15:00:00Z")
        self.assertEqual([p["sequence"] for p in points(result, "ema_2")], [1, 2, 3, 4, 5, 6])


class StorageTests(Base):
    def test_save_load_duplicates_and_failures(self):
        result = self.calc(self.closes_dataset(["1", "2", "3"]), ema=[2])
        store = IndicatorStore(self.root)
        with patch("vicekrack.trading.indicators.store.os.link", side_effect=OSError("disk /private")):
            error = self.assertCode("indicator_write_failed", store.save, result)
        self.assertNotIn("/private", str(error))
        self.assertEqual(list(store.folder.iterdir()), [])
        store.save(result)
        self.assertCode("result_exists", store.save, result)
        self.assertEqual(store.load(result["result_id"]), result)
        path = store.folder / f"{result['result_id']}.json"
        tampered = deepcopy(result)
        tampered["series"][0]["points"][-1]["value"] = "9"
        path.write_text(json.dumps(tampered), encoding="utf-8")
        self.assertCode("result_corrupt", store.load, result["result_id"])
        path.write_text("{", encoding="utf-8")
        self.assertCode("result_corrupt", store.load, result["result_id"])
        self.assertFalse(store.list()[0]["readable"])
        self.assertCode("invalid_result_id", store.load, "../x")

    def test_result_validation(self):
        result = self.calc(self.closes_dataset(["1", "2", "3"]), ema=[2])
        broken = deepcopy(result)
        broken["series"][0]["points"][0]["value"] = "0"                           # unavailable with a value
        self.assertRaises(TradingError, validate_result, broken)
        early = deepcopy(result)
        early["series"][0]["points"][1]["computed_at_sim_utc"] = "2026-01-15T14:30:00Z"
        self.assertRaises(TradingError, validate_result, early)


class SeparationTests(Base):
    def test_no_account_risk_order_or_network_code(self):
        for path in (ROOT / "vicekrack/trading/indicators").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in (".state", ".risk", ".orders", ".journal", "PaperAccount", "validate_signal", "build_intent",
                              "socket", "urllib",
                              "requests", "http.client", "subprocess", "threading", "while True"):
                with self.subTest(file=path.name, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)

    def test_accounts_untouched(self):
        PaperAccount("ind-guard", root=self.root, clock=lambda: NOW).initialize()
        state = self.root / "runtime/trading/accounts/acct-ind-guard/state.json"
        before = state.read_bytes()
        result = self.calc(self.closes_dataset(["1", "2", "3"]), ema=[2])
        IndicatorStore(self.root).save(result)
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual((result["account_access"], result["authorization_possible"]), (False, False))


class CliTests(Base):
    def run_cli(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = indicator_main(list(argv), root=self.root)
        return code, json.loads(output.getvalue())

    def test_commands(self):
        dataset = self.store.import_dataset(make_adapter("synthetic", config=self.market_config, fixture="synth1-5m"),
                                            config=self.market_config, config_sha256="0" * 64)
        code, calc = self.run_cli("indicator-calc", dataset["dataset_id"], "--ema", "3", "--rsi", "3", "--vwap",
                                  "--points", "2")
        self.assertEqual((code, calc["saved"], len(calc["series"][0]["latest"])), (0, False, 2))
        self.assertEqual(self.run_cli("indicator-list")[1]["results"], [])
        code, saved = self.run_cli("indicator-calc", dataset["dataset_id"], "--ema", "3", "--rsi", "3", "--vwap", "--save")
        self.assertEqual((code, saved["saved"]), (0, True))
        self.assertEqual(self.run_cli("indicator-calc", dataset["dataset_id"], "--ema", "3", "--rsi", "3", "--vwap",
                                      "--save")[1]["error"]["code"], "result_exists")
        code, shown = self.run_cli("indicator-inspect", saved["result_id"], "--key", "rsi_3", "--points", "1")
        self.assertEqual((code, shown["series"][0]["key"], len(shown["series"][0]["latest"])), (0, "rsi_3", 1))
        self.assertEqual(self.run_cli("indicator-inspect", saved["result_id"], "--key", "ema_99")[1]["error"]["code"],
                         "unknown_indicator_key")
        self.assertEqual(self.run_cli("indicator-calc", dataset["dataset_id"], "--rsi", "1")[1]["error"]["code"],
                         "invalid_indicator_settings")
        self.assertEqual(self.run_cli("indicator-calc", dataset["dataset_id"], "--ema", "3", "--points", "999")[1]["error"]["code"],
                         "invalid_point_count")
        self.assertEqual(self.run_cli("indicator-calc", dataset["dataset_id"], "--vwap", "--vwap-start", "09:30")[1]["error"]["code"],
                         "invalid_indicator_settings")
        self.assertEqual(self.run_cli("indicator-calc", "mds-" + "0" * 24, "--ema", "3")[1]["error"]["code"], "dataset_not_found")


if __name__ == "__main__":
    unittest.main()
