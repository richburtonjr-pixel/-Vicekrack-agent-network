"""Step 25: offline market-data ingestion and bounded replay. Synthetic data only; no network."""

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from vicekrack.trading.contracts import ROOT
from vicekrack.trading.errors import TradingError
from vicekrack.trading.market import replay as replay_module
from vicekrack.trading.market.bars import expand_bar, parse_timestamp, utc_text, validate_bar
from vicekrack.trading.market.cli import main as market_main
from vicekrack.trading.market.replay import FutureBarAccess, plan, run_replay
from vicekrack.trading.market.store import MarketStore, load_market_config, make_adapter
from vicekrack.trading.state import PaperAccount

HEADER = "timestamp,open,high,low,close,volume"
NOW = "2026-10-05T12:00:00Z"


def row(ts, o="50.00", h="50.10", low="49.90", c="50.05", v="100"):
    return f"{ts},{o},{h},{low},{c},{v}"


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config, self.config_sha = load_market_config()
        self.store = MarketStore(self.root, clock=lambda: NOW)
        self.counter = 0

    def write(self, lines, name=None, raw=None):
        self.counter += 1
        path = self.root / "inputs" / (name or f"input-{self.counter}.csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw if raw is not None else ("\n".join(lines) + "\n").encode("utf-8"))
        return path

    def csv(self, lines=None, *, raw=None, symbol="SYNTH1", interval="5m", tz="America/New_York", config=None, **kwargs):
        config = config or self.config
        path = self.write(lines or [], raw=raw)
        adapter = make_adapter("csv", config=config, file=str(path), symbol=symbol)
        return self.store.import_dataset(adapter, config=config, config_sha256=self.config_sha, interval=interval,
                                         tz=tz, **kwargs)

    def fixture(self, name="synth1-5m", store=None):
        store = store or self.store
        return store.import_dataset(make_adapter("synthetic", config=self.config, fixture=name),
                                    config=self.config, config_sha256=self.config_sha)

    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(TradingError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception)
        return caught.exception


class CsvFormatTests(Base):
    def test_valid_csv(self):
        dataset = self.csv([HEADER, row("2026-01-15T09:30:00-05:00"), row("2026-01-15T09:35:00-05:00")])
        self.assertEqual((dataset["bar_count"], dataset["data_label"], dataset["gaps"]["gap_count"]), (2, "unknown", 0))
        self.assertEqual(dataset["bars"][0]["start_utc"], "2026-01-15T14:30:00Z")

    def test_malformed_csv(self):
        cases = {
            "csv_malformed": [[], [HEADER, row("2026-01-15T09:30:00-05:00") + ",1"], [HEADER, "", row("2026-01-15T09:30:00-05:00")],
                              [HEADER, "2026-01-15T09:30:00-05:00, 50.00,50.10,49.90,50.05,100"],
                              [HEADER, '"2026-01-15T09:30:00-05:00,50.00,50.10,49.90,50.05,100']],
            "csv_header_invalid": [["timestamp,open,high,low,close"], [HEADER + ",extra"], [HEADER + ",open"], ["a,b,c"]],
        }
        for code, variants in cases.items():
            for lines in variants:
                with self.subTest(code=code, lines=lines):
                    self.assertCode(code, self.csv, lines if lines else None, raw=b"" if not lines else None)
        self.assertCode("csv_encoding", self.csv, raw=(HEADER + "\n").encode() + b"\xff\xfe\n")
        self.assertCode("csv_malformed", self.csv, raw=(HEADER + "\n").encode() + b"2026\x00\n")
        self.assertCode("symbol_mismatch", self.csv, ["timestamp,symbol,open,high,low,close,volume",
                                                      "2026-01-15T09:30:00-05:00,OTHER,50.00,50.10,49.90,50.05,100"])

    def test_header_only_is_rejected_as_empty(self):
        with self.assertRaises(TradingError) as caught:
            self.csv([HEADER])
        self.assertIn(caught.exception.code, {"dataset_empty", "csv_malformed"})

    def test_symbol_column_and_bom(self):
        raw = ("﻿timestamp,symbol,open,high,low,close,volume\n"
               "2026-01-15T09:30:00-05:00,SYNTH1,50.00,50.10,49.90,50.05,100\n").encode("utf-8")
        self.assertEqual(self.csv(raw=raw)["bar_count"], 1)

    def test_missing_or_directory_file(self):
        adapter = make_adapter("csv", config=self.config, file=str(self.root / "nope.csv"), symbol="SYNTH1")
        self.assertCode("csv_not_found", self.store.import_dataset, adapter, config=self.config,
                        config_sha256=self.config_sha, interval="5m", tz="UTC")
        adapter = make_adapter("csv", config=self.config, file=str(self.root), symbol="SYNTH1")
        self.assertCode("csv_not_found", adapter.read)


class DecimalAndOhlcTests(Base):
    def test_decimal_precision(self):
        dataset = self.csv([HEADER, row("2026-01-15T09:30:00-05:00", "50.12345678", "50.12345679", "50.12345677",
                                        "50.12345678", "0")])
        self.assertEqual(dataset["bars"][0]["open"], "50.12345678")
        self.assertEqual(expand_bar(dataset, 0)["volume"], "0")
        for field, bad in (("o", "50.123456789"), ("o", "5e1"), ("o", "-50"), ("o", '"1,000"'), ("o", "050.00"),
                           ("o", "0"), ("o", "NaN"), ("v", "-1"), ("v", "1.5e3")):
            values = {"o": "50.00", "h": "51.00", "low": "49.00", "c": "50.00", "v": "100", field: bad}
            code = "invalid_volume" if field == "v" else "invalid_price"
            with self.subTest(field=field, bad=bad):
                error = self.assertCode(code, self.csv, [HEADER, row("2026-01-15T09:30:00-05:00", **values)])
                self.assertNotIn(bad.strip('"'), str(error))                # values never echoed

    def test_invalid_ohlc_relationships(self):
        for values in ({"h": "49.99"}, {"low": "50.06"}, {"h": "49.00", "low": "49.50"}, {"c": "50.20"}, {"low": "50.01"}):
            with self.subTest(values):
                self.assertCode("invalid_ohlc", self.csv, [HEADER, row("2026-01-15T09:30:00-05:00", **values)])


class TimeTests(Base):
    def test_offsets_and_utc(self):
        self.assertEqual(utc_text(parse_timestamp("2026-01-15T14:30:00Z")), "2026-01-15T14:30:00Z")
        self.assertEqual(utc_text(parse_timestamp("2026-01-15T15:30:00+01:00")), "2026-01-15T14:30:00Z")
        for bad in ("2026-01-15", "2026-02-30T09:30:00Z", "2026-01-15T09:30Z", "2026-01-15T09:30:00+25:00", "tomorrow"):
            self.assertRaises(TradingError, parse_timestamp, bad)

    def test_same_instant_different_offsets_is_duplicate(self):
        self.assertCode("duplicate_bar", self.csv, [HEADER, row("2026-01-15T09:30:00-05:00"), row("2026-01-15T14:30:00Z")])

    def test_naive_timestamps_need_explicit_timezone(self):
        lines = [HEADER, row("2026-01-15T09:30:00"), row("2026-01-15T09:35:00")]
        self.assertCode("timestamp_without_timezone", self.csv, lines)
        dataset = self.csv(lines, naive_timezone="America/New_York")
        self.assertEqual(dataset["bars"][0]["start_utc"], "2026-01-15T14:30:00Z")
        self.assertEqual(dataset["import_settings"]["naive_timezone"], "America/New_York")

    def test_dst_nonexistent_and_ambiguous_local_times(self):
        self.assertCode("timestamp_nonexistent", self.csv, [HEADER, row("2026-03-08T02:30:00")],
                        naive_timezone="America/New_York")
        self.assertCode("timestamp_ambiguous", self.csv, [HEADER, row("2026-11-01T01:30:00")],
                        naive_timezone="America/New_York")
        # With explicit offsets both 01:30 instants on 2026-11-01 are distinct and contiguous (30m bars).
        dataset = self.csv([HEADER, row("2026-11-01T01:00:00-04:00"), row("2026-11-01T01:30:00-04:00"),
                            row("2026-11-01T01:00:00-05:00"), row("2026-11-01T01:30:00-05:00")], interval="30m")
        self.assertEqual((dataset["bar_count"], dataset["gaps"]["gap_count"]), (4, 0))
        self.assertEqual(expand_bar(dataset, 2)["timestamp"], "2026-11-01T01:00:00-05:00")

    def test_hourly_across_spring_forward_has_no_false_gap(self):
        dataset = self.csv([HEADER, row("2026-03-08T00:00:00-05:00"), row("2026-03-08T01:00:00-05:00"),
                            row("2026-03-08T03:00:00-04:00")], interval="1h")
        self.assertEqual(dataset["gaps"]["gap_count"], 0)

    def test_daily_bars_across_dst(self):
        dataset = self.fixture("synth1-1d-dst")
        self.assertEqual(dataset["gaps"]["entries"], [{"after_start_utc": "2026-03-06T05:00:00Z",
                                                       "before_start_utc": "2026-03-09T04:00:00Z", "missing_intervals": 2}])
        before, after = expand_bar(dataset, 1), expand_bar(dataset, 2)
        self.assertEqual((before["timestamp"], before["available_at_utc"]), ("2026-03-06T00:00:00-05:00", "2026-03-07T05:00:00Z"))
        self.assertEqual((after["timestamp"], after["available_at_utc"]), ("2026-03-09T00:00:00-04:00", "2026-03-10T04:00:00Z"))
        self.assertCode("interval_misaligned", self.csv, [HEADER, row("2026-07-01T00:00:00Z")], interval="1d")

    def test_interval_alignment(self):
        self.assertCode("interval_misaligned", self.csv, [HEADER, row("2026-01-15T09:32:00-05:00")])
        self.assertCode("interval_misaligned", self.csv, [HEADER, row("2026-01-15T09:30:30-05:00")])
        self.assertCode("interval_not_allowed", self.csv, [HEADER, row("2026-01-15T09:30:00-05:00")],
                        config=dict(self.config, allowed_intervals=["1d"]))
        self.assertCode("invalid_timezone", self.csv, [HEADER, row("2026-01-15T09:30:00-05:00")], tz="Mars/Base")


class OrderGapDuplicateTests(Base):
    def test_out_of_order_and_duplicates(self):
        self.assertCode("bars_out_of_order", self.csv, [HEADER, row("2026-01-15T09:35:00-05:00"), row("2026-01-15T09:30:00-05:00")])
        self.assertCode("duplicate_bar", self.csv, [HEADER, row("2026-01-15T09:30:00-05:00"), row("2026-01-15T09:30:00-05:00")])

    def test_gaps_reported_never_filled(self):
        dataset = self.csv([HEADER, row("2026-01-15T09:30:00-05:00"), row("2026-01-15T09:45:00-05:00"),
                            row("2026-01-15T09:50:00-05:00"), row("2026-01-15T10:30:00-05:00")])
        self.assertEqual(dataset["bar_count"], 4)
        self.assertEqual(dataset["gaps"]["gap_count"], 2)
        self.assertEqual(dataset["gaps"]["missing_intervals"], 2 + 7)
        self.assertEqual(dataset["gaps"]["calendar"], "none")
        self.assertEqual([b["start_utc"] for b in dataset["bars"]],
                         ["2026-01-15T14:30:00Z", "2026-01-15T14:45:00Z", "2026-01-15T14:50:00Z", "2026-01-15T15:30:00Z"])

    def test_gap_list_truncation_is_explicit(self):
        config = deepcopy(self.config)
        config["limits"]["max_gap_entries"] = 1
        dataset = self.csv([HEADER, row("2026-01-15T09:30:00-05:00"), row("2026-01-15T09:45:00-05:00"),
                            row("2026-01-15T10:30:00-05:00")], config=config)
        self.assertEqual((dataset["gaps"]["gap_count"], len(dataset["gaps"]["entries"]), dataset["gaps"]["truncated"]), (2, 1, True))

    def test_duplicate_imports_rejected_consistently(self):
        lines = [HEADER, row("2026-01-15T09:30:00-05:00")]
        first = self.csv(lines)
        self.assertCode("dataset_exists", self.csv, lines)                           # same bytes, renamed copy
        self.assertCode("dataset_exists", self.csv, lines, label="historical")       # settings don't change identity
        other = self.csv(lines, interval="30m")                                      # different interval = new dataset
        self.assertNotEqual(first["dataset_id"], other["dataset_id"])
        self.fixture()
        self.assertCode("dataset_exists", self.fixture)
        self.assertEqual(len(self.store.list_datasets()), 3)


class LimitsAndProvenanceTests(Base):
    def test_file_limits(self):
        config = deepcopy(self.config)
        config["limits"].update(max_file_bytes=100)
        lines = [HEADER] + [row(f"2026-01-15T{9 + (30 + 5 * i) // 60:02d}:{(30 + 5 * i) % 60:02d}:00-05:00") for i in range(4)]
        self.assertCode("file_too_large", self.csv, lines, config=config)
        config = deepcopy(self.config)
        config["limits"].update(max_rows=3)
        self.assertCode("too_many_rows", self.csv, lines, config=config)
        config = deepcopy(self.config)
        config["limits"].update(max_field_length=8)
        self.assertCode("field_too_long", self.csv, lines, config=config)

    def test_provenance_and_source_unchanged(self):
        path = self.write([HEADER, row("2026-01-15T09:30:00-05:00")], name="My Data (2026).csv")
        before = (path.read_bytes(), os.stat(path).st_mtime_ns)
        adapter = make_adapter("csv", config=self.config, file=str(path), symbol="SYNTH1", source_name="Vendor export")
        dataset = self.store.import_dataset(adapter, config=self.config, config_sha256=self.config_sha, interval="5m",
                                            tz="America/New_York", label="historical")
        self.assertEqual((path.read_bytes(), os.stat(path).st_mtime_ns), before)
        self.assertEqual(dataset["source"]["file_sha256"], hashlib.sha256(before[0]).hexdigest())
        self.assertEqual(dataset["source"]["file_name"], "My_Data__2026_.csv")
        self.assertEqual(dataset["import_settings"]["config_sha256"], self.config_sha)
        stored = (self.root / "runtime/trading/market/datasets" / f"{dataset['dataset_id']}.json").read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), stored)                                # no local folder paths
        self.assertEqual(dataset["verification"]["authentic"], "not_verified")
        self.assertEqual(dataset["verification"]["licensed"], "not_verified")

    def test_labels(self):
        self.assertEqual(self.fixture()["data_label"], "synthetic")
        self.assertCode("label_not_allowed", self.store.import_dataset,
                        make_adapter("synthetic", config=self.config, fixture="synth1-1d-dst"),
                        config=self.config, config_sha256=self.config_sha, label="historical")
        self.assertCode("fixture_settings_mismatch", self.store.import_dataset,
                        make_adapter("synthetic", config=self.config, fixture="synth1-1d-dst"),
                        config=self.config, config_sha256=self.config_sha, symbol="OTHER")
        for number, label in enumerate(("historical", "delayed", "unknown", "synthetic")):
            self.assertEqual(self.csv([HEADER, row("2026-01-15T09:30:00-05:00", v=str(number))], label=label)["data_label"], label)
        self.assertCode("unknown_adapter", make_adapter, "live", config=self.config)
        self.assertCode("unknown_fixture", make_adapter, "synthetic", config=self.config, fixture="../config")
        self.assertCode("unknown_fixture", make_adapter("synthetic", config=self.config, fixture="no-such-fixture").read)

    def test_credentials_and_sanitized_errors(self):
        path = self.write([HEADER, row("2026-01-15T09:30:00-05:00")])
        adapter = make_adapter("csv", config=self.config, file=str(path), symbol="SYNTH1",
                               source_name="sk-" + "abcdefghij" * 3)  # built at runtime; not a real key
        self.assertCode("sensitive_state", self.store.import_dataset, adapter, config=self.config,
                        config_sha256=self.config_sha, interval="5m", tz="UTC")
        self.assertCode("invalid_source_name", make_adapter, "csv", config=self.config, file=str(path), symbol="S",
                        source_name="../../etc")
        error = self.assertCode("csv_not_found", make_adapter("csv", config=self.config, file="/secret/place/x.csv",
                                                               symbol="SYNTH1").read)
        self.assertNotIn("/secret/place", str(error))


class StorageTests(Base):
    def test_interrupted_write_stores_nothing(self):
        with patch("vicekrack.trading.market.store.os.link", side_effect=OSError("disk full /private")):
            error = self.assertCode("market_write_failed", self.fixture)
        self.assertNotIn("/private", str(error))
        folder = self.root / "runtime/trading/market/datasets"
        self.assertEqual(list(folder.iterdir()), [])                       # no partial dataset, no temp file
        dataset = self.fixture()                                           # retry succeeds
        report = run_replay(dataset, config=self.config, created_at=NOW)
        with patch("vicekrack.trading.market.store.os.link", side_effect=OSError("x")):
            self.assertCode("market_write_failed", self.store.save_replay, report)
        self.assertEqual(self.store.list_replays(), [])
        self.store.save_replay(report)
        self.assertCode("replay_exists", self.store.save_replay, report)

    def test_corrupted_dataset_detected(self):
        dataset = self.fixture()
        path = self.root / "runtime/trading/market/datasets" / f"{dataset['dataset_id']}.json"
        for change in (lambda d: d["bars"][3].update(close="99.00"),
                       lambda d: d["gaps"].update(gap_count=0),
                       lambda d: d.update(data_label="historical"),
                       lambda d: d["bars"].reverse()):
            tampered = deepcopy(dataset)
            change(tampered)
            path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.subTest(change):
                self.assertCode("dataset_corrupt", self.store.load, dataset["dataset_id"])
        path.write_text("{", encoding="utf-8")
        self.assertCode("dataset_corrupt", self.store.load, dataset["dataset_id"])
        self.assertFalse(self.store.list_datasets()[0]["readable"])
        self.assertCode("invalid_dataset_id", self.store.load, "../x")
        self.assertCode("dataset_not_found", self.store.load, "mds-" + "0" * 24)


class ReplayTests(Base):
    def test_deterministic_replay(self):
        other = MarketStore(Path(tempfile.mkdtemp()), clock=lambda: "2030-01-01T00:00:00Z")
        self.addCleanup(shutil.rmtree, other.base.parent.parent.parent, ignore_errors=True)
        one = run_replay(self.fixture(), config=self.config, created_at=NOW)
        two = run_replay(self.fixture(store=other), config=self.config, created_at="2030-01-01T00:00:00Z")
        for key in ("replay_id", "results_sha256", "steps", "summary", "consumers", "simulation"):
            self.assertEqual(one[key], two[key])
        self.assertEqual(one["summary"], {"bars_delivered": 12, "steps_without_new_bar": 2,
                                          "future_access_attempts": 14, "max_visible_bars": 12})
        self.assertEqual(one["consumers"][1]["final"], {"attempts": 14, "refused": 14, "leaked": 0})

    def test_consumers_never_see_future_bars(self):
        dataset = self.fixture()
        seen = []

        class Spy:
            name = "spy"

            def on_step(self, view):
                visible = view.bars()
                seen.append((view.now, [b["available_at_utc"] for b in visible], view.visible_count))
                with self_test.assertRaises(FutureBarAccess):
                    view.bar(view.visible_count + 1)
                if view.visible_count < dataset["bar_count"]:
                    with self_test.assertRaises(FutureBarAccess):
                        view.bar(dataset["bar_count"])      # the last bar, before it has closed
                if visible:
                    with self_test.assertRaises(TypeError):
                        visible[-1]["close"] = "1"    # read-only
                    self_test.assertEqual(view.bar(view.visible_count)["sequence"], view.visible_count)
                self_test.assertFalse(hasattr(view, "__dict__"))

            def final(self):
                return {}
        self_test = self
        with patch.dict(replay_module.CONSUMERS, {"spy": Spy}):
            run_replay(dataset, config=self.config, created_at=NOW, consumers=["spy"])
        self.assertTrue(seen)
        for now, available, count in seen:
            self.assertTrue(all(a <= now for a in available))
            self.assertEqual(len(available), count)
        self.assertEqual(seen[0][2], 0)                       # at the first bar's start nothing has closed yet

    def test_window_limits_history_and_custom_range(self):
        config = deepcopy(self.config)
        config["replay"]["max_window_bars"] = 3
        dataset = self.fixture()
        windows = []

        class Spy:
            name = "spy"

            def on_step(self, view):
                windows.append(len(view.bars()))
                if view.visible_count > 3:
                    with test.assertRaises(TradingError):
                        view.bar(1)

            def final(self):
                return {}
        test = self
        with patch.dict(replay_module.CONSUMERS, {"spy": Spy}):
            report = run_replay(dataset, config=config, created_at=NOW, consumers=["spy"],
                                start="2026-01-15T14:40:00Z", end="2026-01-15T15:10:00Z", step_seconds=600)
        self.assertEqual(max(windows), 3)
        self.assertEqual(report["simulation"]["steps"], 4)
        self.assertEqual([s["visible_bars"] for s in report["steps"]], [2, 4, 6, 7])  # the 10:00 bar is missing (gap)

    def test_replay_bounds(self):
        dataset = self.fixture()
        self.assertCode("replay_too_long", plan, dataset, step_seconds=1, max_steps=100)
        self.assertCode("invalid_replay_window", plan, dataset, start="2026-01-15T16:00:00Z", end="2026-01-15T15:00:00Z")
        self.assertCode("invalid_replay_window", plan, dataset, start="yesterday")
        self.assertCode("invalid_replay_window", plan, dataset, step_seconds=0)
        self.assertCode("unknown_consumer", run_replay, dataset, config=self.config, created_at=NOW, consumers=["trader"])

    def test_consumer_failure_is_sanitized(self):
        class Broken:
            name = "broken"

            def on_step(self, view):
                raise RuntimeError("/private/detail")

            def final(self):
                return {}
        with patch.dict(replay_module.CONSUMERS, {"broken": Broken}):
            error = self.assertCode("replay_consumer_failed", run_replay, self.fixture(), config=self.config,
                                    created_at=NOW, consumers=["broken"])
        self.assertNotIn("/private", str(error))

    def test_expanded_bars_validate(self):
        dataset = self.fixture()
        bar = expand_bar(dataset, 0)
        validate_bar(bar)
        self.assertEqual((bar["timestamp"], bar["available_at_utc"], bar["data_label"]),
                         ("2026-01-15T09:30:00-05:00", "2026-01-15T14:35:00Z", "synthetic"))
        self.assertRaises(TradingError, validate_bar, dict(bar, available_at_utc="2026-01-15T14:30:00Z"))
        self.assertRaises(TradingError, validate_bar, dict(bar, timestamp="2026-01-15T09:30:00-04:00"))


class SeparationTests(Base):
    def test_replay_cannot_touch_accounts_or_authorization(self):
        account = PaperAccount("market-guard", root=self.root, clock=lambda: NOW)
        account.initialize()
        state_path = self.root / "runtime/trading/accounts/acct-market-guard/state.json"
        before = state_path.read_bytes()
        dataset = self.fixture()
        report = self.store.save_replay(run_replay(dataset, config=self.config, created_at=NOW))
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual((report["account_access"], report["authorization_possible"], report["mode"]),
                         (False, False, "historical_replay"))
        self.assertEqual(sorted(p.name for p in (self.root / "runtime/trading/accounts").iterdir()), ["acct-market-guard"])

    def test_market_package_imports_no_account_or_risk_code(self):
        for path in (ROOT / "vicekrack/trading/market").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("state import", ".state", "risk import", ".risk", ".orders", ".journal", "PaperAccount",
                              "socket", "urllib", "requests", "http.client", "subprocess", "threading", "while True"):
                with self.subTest(file=path.name, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)

    def test_snapshots_still_only_accept_synthetic_fixture_sources(self):
        schema = json.loads((ROOT / "schemas/trading/market-snapshot.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["source"]["properties"]["kind"]["enum"], ["synthetic_fixture"])


class CliTests(Base):
    def run_cli(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = market_main(list(argv), root=self.root)
        return code, json.loads(output.getvalue())

    def test_commands(self):
        code, imported = self.run_cli("market-import", "--adapter", "synthetic", "--fixture", "synth1-5m")
        self.assertEqual((code, imported["imported"]), (0, True))
        self.assertIn("NOT verified", imported["notice"])
        dataset_id = imported["dataset_id"]
        self.assertEqual(self.run_cli("market-import", "--adapter", "synthetic", "--fixture", "synth1-5m")[1]["error"]["code"],
                         "dataset_exists")
        csv_path = ROOT / "examples/trading/market/synthetic-synth2-5m.csv"
        code, csv_import = self.run_cli("market-import", "--adapter", "csv", "--file", str(csv_path), "--symbol", "SYNTH2",
                                        "--interval", "5m", "--timezone", "America/New_York", "--label", "synthetic")
        self.assertEqual((code, csv_import["source"]["adapter"]), (0, "local_csv"))
        self.assertEqual(len(self.run_cli("market-list")[1]["datasets"]), 2)
        code, shown = self.run_cli("market-inspect", dataset_id, "--bars", "2")
        self.assertEqual(len(shown["bars"]), 2)
        code, replayed = self.run_cli("market-replay", dataset_id, "--show-steps")
        self.assertEqual((code, replayed["account_access"], replayed["summary"]["bars_delivered"]), (0, False, 12))
        self.assertEqual(self.run_cli("market-replay", dataset_id)[1]["error"]["code"], "replay_exists")
        self.assertEqual(self.run_cli("market-list", "--replays")[1]["replays"][0]["replay_id"], replayed["replay_id"])
        self.assertEqual(self.run_cli("market-inspect", replayed["replay_id"])[1]["replay_id"], replayed["replay_id"])
        self.assertEqual(self.run_cli("market-import", "--adapter", "csv", "--fixture", "x")[1]["error"]["code"],
                         "import_settings_missing")
        self.assertEqual(self.run_cli("market-inspect", "mds-bad")[1]["error"]["code"], "invalid_dataset_id")
        self.assertEqual(self.run_cli("market-inspect", dataset_id, "--bars", "99")[1]["error"]["code"], "invalid_bar_count")


if __name__ == "__main__":
    unittest.main()
