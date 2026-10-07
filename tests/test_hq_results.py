"""Step 34: Living HQ trading results desk. Local synthetic data only; no network beyond 127.0.0.1, no credits.

Expected cash, fees, realized and unrealized P&L and equity at every replay position are
computed here independently (exact Fractions, the documented next-open fill model and the
bars written in this file), never with the results module's helpers.
"""

import json
import os
import re
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from unittest.mock import patch

from test_analytics import QUANTITY, WIN_LOSS, fill
from test_events import EventBase, tree
from test_simulation import BARS, NOW, text
from vicekrack.errors import NetworkError
from vicekrack.events.cli import load_timeline
from vicekrack.events.contract import validate_event, event_id
from vicekrack.hq import api, results
from vicekrack.hq.demo import demo_events, demo_scene
from vicekrack.hq.results_demo import demo_inputs, demo_report
from vicekrack.hq.server import HQServer
from vicekrack.trading.analytics.report import build_report
from vicekrack.trading.analytics.store import AnalyticsStore, load_analytics_config
from vicekrack.trading.contracts import sha256
from vicekrack.trading.simulation.store import SimulationStore

PORT = 8765
HOST = {"host": f"127.0.0.1:{PORT}"}
INITIAL = Fraction(10000)
FIRST_CLOSE = datetime(2026, 1, 15, 14, 35, tzinfo=timezone.utc)    # bar 1 (09:30 New York) closes 14:35 UTC
SUMMARY_ONLY = ("ending_equity", "ending_cash", "net_return", "win_rate_percent", "profit_factor", "expectancy",
                "rejected_by_reason", "attribution", "exposure_percent", "closed_average_bars")


def get(target, root=None, method="GET", headers=None):
    status, response_headers, body = api.respond(method, target, dict(HOST, **(headers or {})), port=PORT, root=root)
    return status, response_headers, json.loads(body) if body.startswith(b"{") else body


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def last_close(bars, sim_time):
    """Close of the last bar that had closed by sim_time (bar k closes FIRST_CLOSE + 5(k-1) minutes)."""
    closed = [Fraction(bar[3]) for k, bar in enumerate(bars) if stamp(FIRST_CLOSE + timedelta(minutes=5 * k)) <= sim_time]
    return closed[-1] if closed else None


class ResultsBase(EventBase):
    def setUp(self):
        super().setUp()
        self.analytics_config, self.analytics_sha = load_analytics_config()

    def saved(self, bars=WIN_LOSS, record=True, analytics=True, policy=None):
        """Run, save and (optionally) record and analyse one simulation. Returns (run, timeline_id or None)."""
        recorder = self.recorder() if record else None
        run = self.simulate(self.dataset(bars), policy, events=recorder) if policy else self.sim(events=recorder, bars=bars)
        if recorder is not None:
            recorder.close("completed")
        SimulationStore(self.root).save(run)
        if analytics:
            AnalyticsStore(self.root).save(self.report_for(run, bars))
        return run, (recorder.timeline_id if recorder else None)

    def simulate(self, dataset, policy=None, kill_switch=(False, None), events=None, **window):
        from vicekrack.trading.simulation.engine import run_simulation
        return run_simulation(dataset, policy or self.policy, market_config=self.market_config,
                              indicator_config=self.indicator_config, signal_config=self.signal_config,
                              kill_switch=kill_switch, created_at=NOW, events=events, **window)

    def report_for(self, run, bars):
        return build_report(run, self.dataset(bars), market_config=self.market_config, analytics_config=self.analytics_config,
                            analytics_config_sha256=self.analytics_sha, created_at=NOW)

    def all_positions(self, timeline):
        index = results.results_index(timeline, self.root)
        return index, [results.results_at(timeline, n, self.root) for n in range(index["timeline"]["event_count"] + 1)]


class FinancialDisplayTests(ResultsBase):
    def test_every_position_matches_an_independent_model(self):
        run, recorded = self.saved(WIN_LOSS)
        plan = {4: "buy", 6: "sell", 7: "buy", 9: "sell"}
        self.assertEqual({f["bar_sequence"]: f["side"] for f in run["fills"]}, plan)
        for timeline in (recorded, run["run_id"]):
            view = load_timeline(timeline, self.root)
            index, docs = self.all_positions(timeline)
            self.assertEqual(index["replay_state"], {"status": "available", "reasons": []})
            self.assertEqual(index["correlation"]["analytics"]["status"], "available")
            cash, fees, realized, shares, basis = INITIAL, Fraction(0), Fraction(0), 0, Fraction(0)
            for n, doc in enumerate(docs):
                if n:
                    event = view["events"][n - 1]
                    if event["event_type"] == "simulated_fill":
                        notional, fee = fill(WIN_LOSS, event["details"]["bar_sequence"], event["details"]["side"])
                        fees += fee
                        if event["details"]["side"] == "buy":
                            cash, shares, basis = cash - notional - fee, QUANTITY, notional + fee
                        else:
                            cash, shares = cash + notional - fee, 0
                            realized += notional - fee - basis
                if n == 0:
                    self.assertEqual((doc["status"], doc["portfolio"], doc["event"]), ("not_started", None, None))
                    continue
                sim_time = view["events"][n - 1]["sim_time_utc"]
                p = doc["portfolio"]
                self.assertEqual((doc["status"], doc["simulated_time_utc"]), ("available", sim_time))
                self.assertEqual((p["cash"], p["fees"], p["realized_pnl"], p["open_quantity"]),
                                 (text(cash), text(fees), text(realized), shares), (timeline, n))
                mark = last_close(WIN_LOSS, sim_time)
                if shares:
                    self.assertEqual(p["unrealized_pnl"]["value"], text(mark * shares - basis))
                    self.assertEqual(p["mark"]["price"], text(mark))
                self.assertEqual(p["equity"]["value"], text(cash + (mark * shares if shares else 0)))
            final = docs[-1]["portfolio"]
            self.assertEqual((final["cash"], final["fees"], final["realized_pnl"]),
                             (run["summary"]["ending_cash"], run["summary"]["fees_total"], run["summary"]["realized_pnl"]))
            self.assertEqual(len(final["closed_trades"]), 2)
            self.assertEqual([t["outcome"] for t in final["closed_trades"]], ["win", "loss"])

    def test_summary_matches_run_and_report_and_keeps_unavailable(self):
        run, recorded = self.saved(BARS)                     # one win, one position still open at the end
        summary = results.results_summary(recorded, self.root)
        self.assertEqual(summary["view"], "completed_run_summary")
        self.assertIn("Not tied to the replay position", summary["label"])
        account = summary["account"]
        for key in ("ending_cash", "ending_equity", "realized_pnl", "unrealized_pnl", "fees_total"):
            self.assertEqual(account[key], run["summary"][key], key)
        self.assertEqual(account["net_return"], text(Fraction(run["summary"]["ending_equity"]) - INITIAL))
        stats = summary["analytics"]["report"]["closed_trades"]
        for name in ("average_loss", "profit_factor"):
            self.assertEqual(stats[name], {"status": "unavailable", "value": None, "reason": "no_losing_trades"})
        self.assertIn("open_position_at_end", [row["code"] for row in summary["limitations"]])

    def test_no_closed_trades_shows_unavailable_not_zero(self):
        run, recorded = self.saved([("10", "10", "10", "10")] * 6)
        summary = results.results_summary(recorded, self.root)
        stats = summary["analytics"]["report"]["closed_trades"]
        self.assertEqual(stats["count"], 0)
        for name in ("win_rate_percent", "average_net", "expectancy", "average_win", "average_loss", "profit_factor"):
            self.assertEqual(stats[name]["status"], "unavailable", name)
            self.assertEqual(stats[name]["reason"], "no_closed_trades")


class ReplayIntegrityTests(ResultsBase):
    def test_positions_never_include_later_information(self):
        run, recorded = self.saved(WIN_LOSS)
        view = load_timeline(recorded, self.root)
        _, docs = self.all_positions(recorded)
        for n, doc in enumerate(docs):
            encoded = json.dumps(doc)
            for key in SUMMARY_ONLY:
                self.assertNotIn(f'"{key}"', encoded, (n, key))
            later = view["events"][n:]
            earlier = view["events"][:n]
            known = {r["id"] for e in earlier for r in e["refs"]}
            for event in later:
                for ref in event["refs"]:
                    if ref["kind"] in ("fill", "order") and ref["id"] not in known:
                        self.assertNotIn(ref["id"], encoded, (n, ref))          # future fills and orders never appear
            if doc["portfolio"] is None:
                continue
            now = doc["simulated_time_utc"]
            self.assertTrue(all(point["at_utc"] <= now for point in doc["portfolio"]["equity_curve"]))
            for order in doc["portfolio"]["orders"]:
                self.assertTrue(all(entry["at_utc"] <= now for entry in order["history"]))
            fills_so_far = sum(1 for e in earlier if e["event_type"] == "simulated_fill")
            self.assertEqual(len(doc["portfolio"]["fills"]), fills_so_far)

    def test_pending_order_does_not_reveal_its_fill(self):
        _, recorded = self.saved(WIN_LOSS)
        view = load_timeline(recorded, self.root)
        accepted = next(i for i, e in enumerate(view["events"]) if e["event_type"] == "order_decision" and e["status"] == "accepted")
        doc = results.results_at(recorded, accepted + 1, self.root)
        order = doc["portfolio"]["orders"][-1]
        self.assertEqual((order["status"], order["fill_id"], len(order["history"])), ("pending", None, 1))
        self.assertEqual(doc["portfolio"]["fills"], [])

    def test_position_bounds_and_api_seek(self):
        _, recorded = self.saved(WIN_LOSS)
        count = results.results_index(recorded, self.root)["timeline"]["event_count"]
        self.assertEqual(get(f"/api/results/at?timeline={recorded}&position={count}", self.root)[0], 200)
        status, _, body = get(f"/api/results/at?timeline={recorded}&position={count + 1}", self.root)
        self.assertEqual((status, body["error"]["code"]), (400, "results_position_out_of_range"))
        first = get(f"/api/results/at?timeline={recorded}&position=3", self.root)[2]
        get(f"/api/results/at?timeline={recorded}&position={count}", self.root)
        self.assertEqual(get(f"/api/results/at?timeline={recorded}&position=3", self.root)[2], first)  # seeking back is stateless

    def fake_view(self, recorded, change):
        view = deepcopy(load_timeline(recorded, self.root))
        change(view)
        return patch("vicekrack.events.cli.load_timeline", return_value=view)

    def test_intermediate_state_unavailable_when_ordering_cannot_support_it(self):
        _, recorded = self.saved(WIN_LOSS)
        cases = {
            "timeline_not_complete": lambda v: v.update(completeness="partial"),
            "timeline_has_issues": lambda v: v.update(issues=["missing_events"]),
            "timeline_missing_trade_events": lambda v: v["events"].pop(
                max(i for i, e in enumerate(v["events"]) if e["event_type"] == "simulated_fill")),
        }

        def swap(view):                                   # two different orders' events, earlier time moved later
            events = view["events"]
            for i in range(len(events) - 1):
                a, b = events[i], events[i + 1]
                order = lambda e: [r["id"] for r in e["refs"] if r["kind"] == "order"]
                if order(a) and order(b) and order(a) != order(b) and a["sim_time_utc"] < b["sim_time_utc"]:
                    events[i], events[i + 1] = b, a
                    return
            raise AssertionError("no swappable pair")
        cases["timeline_not_chronological"] = swap
        for reason, change in cases.items():
            with self.subTest(reason=reason), self.fake_view(recorded, change):
                index = results.results_index(recorded, self.root)
                self.assertEqual(index["replay_state"]["status"], "unavailable")
                self.assertIn(reason, index["replay_state"]["reasons"])
                doc = results.results_at(recorded, 3, self.root)
                self.assertEqual((doc["status"], doc["portfolio"]), ("unavailable", None))
                self.assertIn(reason, doc["reasons"])
                self.assertEqual(results.results_summary(recorded, self.root)["view"], "completed_run_summary")

    def test_simulated_and_recorded_time_are_distinct(self):
        _, recorded = self.saved(WIN_LOSS)
        doc = results.results_at(recorded, 2, self.root)
        self.assertIsNotNone(doc["event"]["recorded_at"])
        self.assertEqual(doc["simulated_time_utc"], doc["event"]["sim_time_utc"])
        run_id = load_timeline(recorded, self.root)["run_id"]
        reconstructed = results.results_at(run_id, 2, self.root)
        self.assertIsNone(reconstructed["event"]["recorded_at"])            # reconstructed: no times invented


class CorrelationTests(ResultsBase):
    def test_recorded_and_reconstructed_timelines_agree(self):
        run, recorded = self.saved(WIN_LOSS)
        _, a = self.all_positions(recorded)
        _, b = self.all_positions(run["run_id"])
        strip = lambda d: {k: v for k, v in d.items() if k not in ("timeline_id", "event")}
        self.assertEqual(strip(a[-1]), strip(b[-1]))

    def test_timeline_of_another_run_is_rejected(self):
        run_a, recorded_a = self.saved(WIN_LOSS)
        run_b, _ = self.saved(BARS, record=False)
        view = deepcopy(load_timeline(recorded_a, self.root))
        view["run_id"] = run_b["run_id"]                     # claims run B but carries run A's events
        with patch("vicekrack.events.cli.load_timeline", return_value=view):
            status, _, body = get(f"/api/results?timeline={recorded_a}", self.root)
        self.assertEqual((status, body["error"]["code"]), (409, "timeline_run_mismatch"))
        for field, value in (("sim_time_utc", "2030-01-01T00:00:00Z"), ("reason_codes", ["insufficient_cash"])):
            altered = deepcopy(load_timeline(recorded_a, self.root))
            target = next(e for e in altered["events"] if e["event_type"] == "order_decision")
            target[field] = value
            with self.subTest(field=field), patch("vicekrack.events.cli.load_timeline", return_value=altered):
                self.assertEqual(get(f"/api/results?timeline={recorded_a}", self.root)[2]["error"]["code"], "timeline_run_mismatch")

    def test_unsaved_run_and_other_departments(self):
        recorder = self.recorder()
        self.sim(events=recorder, bars=WIN_LOSS)               # recorded but never saved
        recorder.close("completed")
        status, _, body = get(f"/api/results?timeline={recorder.timeline_id}", self.root)
        self.assertEqual((status, body["error"]["code"]), (404, "sim_run_not_found"))
        agents = self.agents()
        from vicekrack.trading.agents.store import AgentRunStore
        AgentRunStore(self.root).save(agents)
        status, _, body = get(f"/api/results?timeline={agents['run_id']}", self.root)
        self.assertEqual((status, body["error"]["code"]), (422, "results_not_simulation"))

    def test_tampered_run_is_rejected(self):
        run, recorded = self.saved(WIN_LOSS)
        path = self.root / "runtime/trading/simulation/runs" / f"{run['run_id']}.json"
        path.write_text(path.read_text(encoding="utf-8").replace('"fees_total": "', '"fees_total": "1', 1), encoding="utf-8")
        for route in ("/api/results", "/api/results/summary"):
            status, _, body = get(f"{route}?timeline={recorded}", self.root)
            self.assertEqual((status, body["error"]["code"]), (409, "sim_run_corrupt"))
            self.assertNotIn(str(self.root), json.dumps(body))


class AnalyticsCorrelationTests(ResultsBase):
    def reports(self):
        return AnalyticsStore(self.root).reports

    def forge(self, report, change):
        forged = deepcopy(report)
        change(forged)
        body = {k: forged[k] for k in ("source", "period", "account", "closed_trades", "open_positions", "equity_curve",
                                       "drawdown", "holding", "exposure", "orders", "attribution")}
        forged["results_sha256"] = sha256(body)              # internally consistent: passes the store's own checks
        forged["report_id"] = "sarp-" + sha256({"forged": forged["results_sha256"]})[:24]
        return forged

    def test_missing_analytics_is_explicit(self):
        run, recorded = self.saved(BARS, analytics=False)
        index = results.results_index(recorded, self.root)
        self.assertEqual((index["correlation"]["analytics"]["status"], index["correlation"]["analytics"]["reason"]),
                         ("unavailable", "analytics_report_missing"))
        view = load_timeline(recorded, self.root)
        after_buy = next(i for i, e in enumerate(view["events"]) if e["event_type"] == "simulated_fill") + 1
        doc = results.results_at(recorded, after_buy, self.root)
        p = doc["portfolio"]
        self.assertEqual(p["equity"], {"status": "unavailable", "value": None, "reason": "analytics_report_missing"})
        self.assertEqual(p["unrealized_pnl"]["status"], "unavailable")
        self.assertEqual(p["equity_curve"], [])
        self.assertEqual(p["equity_curve_status"]["reason"], "analytics_report_missing")
        self.assertNotEqual(p["cash"], None)                 # what the run itself proves is still shown
        summary = results.results_summary(recorded, self.root)
        self.assertIsNone(summary["analytics"]["report"])
        self.assertIn("analytics_report_missing", [row["code"] for row in summary["limitations"]])
        self.assertEqual(summary["account"]["ending_equity"], run["summary"]["ending_equity"])

    def test_tampered_and_mismatched_reports_are_rejected_by_id(self):
        run, recorded = self.saved(WIN_LOSS)
        good = AnalyticsStore(self.root).load(next(self.reports().glob("sarp-*.json")).stem)
        store = AnalyticsStore(self.root)
        mismatch = self.forge(good, lambda r: r["source"].update(run_results_sha256="0" * 64))
        store.save(mismatch)
        inconsistent = self.forge(good, lambda r: r["account"].update(fees_total="999"))
        store.save(inconsistent)
        index = results.results_index(recorded, self.root)
        analytics = index["correlation"]["analytics"]
        self.assertEqual((analytics["status"], analytics["report_id"]), ("available", good["report_id"]))
        self.assertEqual(sorted((r["report_id"], r["code"]) for r in analytics["rejected_reports"]),
                         sorted([(mismatch["report_id"], "analytics_report_mismatch"),
                                 (inconsistent["report_id"], "analytics_report_inconsistent")]))
        # Edit the genuine report on disk: its hash breaks, so nothing is shown from it.
        path = self.reports() / f"{good['report_id']}.json"
        path.write_text(path.read_text(encoding="utf-8").replace('"net_return": "', '"net_return": "5', 1), encoding="utf-8")
        analytics = results.results_index(recorded, self.root)["correlation"]["analytics"]
        self.assertEqual((analytics["status"], analytics["reason"]), ("unavailable", "analytics_report_rejected"))
        self.assertIn((good["report_id"], "report_corrupt"), [(r["report_id"], r["code"]) for r in analytics["rejected_reports"]])
        summary = results.results_summary(recorded, self.root)
        self.assertIsNone(summary["analytics"]["report"])

    def test_reports_link_only_by_content_not_filename(self):
        run_a, recorded_a = self.saved(WIN_LOSS)
        run_b, _ = self.saved(BARS, record=False)
        report_b = AnalyticsStore(self.root).load(
            next(p.stem for p in self.reports().glob("sarp-*.json")
                 if json.loads(p.read_text(encoding="utf-8"))["source"]["run_id"] == run_b["run_id"]))
        report_a = next(p for p in self.reports().glob("sarp-*.json")
                        if json.loads(p.read_text(encoding="utf-8"))["source"]["run_id"] == run_a["run_id"])
        report_a.unlink()
        # Run B's report, saved under a name that looks like it might belong anywhere, is never used for run A.
        analytics = results.results_index(recorded_a, self.root)["correlation"]["analytics"]
        self.assertEqual((analytics["status"], analytics["reason"]), ("unavailable", "analytics_report_missing"))
        self.assertNotEqual(report_b["report_id"], analytics["report_id"])
        (self.reports() / "sarp-not-a-real-id.json").write_text("{", encoding="utf-8")
        (self.reports() / ("sarp-" + "f" * 24 + ".json")).write_text("not json", encoding="utf-8")
        analytics = results.results_index(recorded_a, self.root)["correlation"]["analytics"]
        self.assertEqual(analytics["unreadable_reports_skipped"], 1)

    def test_two_matching_reports_prefer_the_current_config(self):
        run, recorded = self.saved(WIN_LOSS)
        other_config = deepcopy(self.analytics_config)
        other_config["limits"]["max_cli_items"] = 7
        other = build_report(run, self.dataset(WIN_LOSS), market_config=self.market_config, analytics_config=other_config,
                             analytics_config_sha256=sha256(other_config), created_at=NOW)
        AnalyticsStore(self.root).save(other)
        analytics = results.results_index(recorded, self.root)["correlation"]["analytics"]
        self.assertEqual(analytics["status"], "available")
        self.assertNotEqual(analytics["report_id"], other["report_id"])
        with patch("vicekrack.trading.analytics.store.load_analytics_config", return_value=(other_config, "9" * 64)):
            analytics = results.results_index(recorded, self.root)["correlation"]["analytics"]
        self.assertEqual((analytics["status"], analytics["reason"]), ("unavailable", "analytics_report_ambiguous"))


class BoundaryTests(ResultsBase):
    def test_read_only_and_runs_nothing(self):
        run, recorded = self.saved(WIN_LOSS)
        before = tree(self.root)
        guards = [patch(target, side_effect=AssertionError("must not run")) for target in (
            "vicekrack.trading.simulation.engine.run_simulation", "vicekrack.trading.analytics.report.build_report",
            "vicekrack.trading.analytics.store.AnalyticsStore.save", "vicekrack.trading.simulation.store.SimulationStore.save",
            "vicekrack.trading.simulation.store.SimulationStore.set_kill_switch", "vicekrack.trading.state.PaperAccount.__init__",
            "vicekrack.events.store.EventStore.open", "socket.socket.connect")]
        for guard in guards:
            guard.start()
            self.addCleanup(guard.stop)
        count = results.results_index(recorded, self.root)["timeline"]["event_count"]
        for timeline in (recorded, run["run_id"], "demo"):
            self.assertEqual(get(f"/api/results?timeline={timeline}", self.root)[0], 200)
            self.assertEqual(get(f"/api/results/summary?timeline={timeline}", self.root)[0], 200)
            for n in range(count + 1):
                self.assertEqual(get(f"/api/results/at?timeline={timeline}&position={n}", self.root)[0], 200)
        self.assertEqual(tree(self.root), before)

    def test_requests_are_allowlisted(self):
        _, recorded = self.saved(WIN_LOSS)
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
            self.assertEqual(get(f"/api/results?timeline={recorded}", self.root, method=method)[0], 405)
        self.assertEqual(get("/api/results?timeline=demo", headers={"origin": "http://evil.example"})[0], 403)
        self.assertEqual(get("/api/results?timeline=demo", headers={"sec-fetch-site": "cross-site"})[0], 403)
        for target in ("/api/results", "/api/results?timeline=../x", "/api/results?timeline=demo&timeline=demo",
                       "/api/results?timeline=demo&position=1", "/api/results/at?timeline=demo",
                       "/api/results/at?timeline=demo&position=1.5", "/api/results/at?timeline=demo&position=-1",
                       "/api/results/at?timeline=demo&position=0001", "/api/results/at?timeline=demo&position=9999999",
                       "/api/results/summary?timeline=demo&x=1", "/api/results?timeline=srun-" + "A" * 24):
            status, _, body = get(target, self.root)
            self.assertEqual(status, 400, target)
            self.assertIn(body["error"]["code"], ("invalid_results_request", "invalid_request"), target)
        for target in ("/api/results/", "/api/results/at/1", "/api/results/../scene?timeline=demo", "/api/resultsx"):
            self.assertEqual(get(target, self.root)[0], 404, target)
        status, _, body = get("/api/results?timeline=srun-" + "a" * 24, self.root)
        self.assertEqual((status, body["error"]["code"]), (404, "sim_run_not_found"))

    def test_errors_never_leak(self):
        secret = "sk-" + "b" * 30
        for name in ("results_index", "results_at", "results_summary"):
            with self.subTest(name=name), patch(f"vicekrack.hq.api.{name}", side_effect=RuntimeError(f"{secret} /home/x")):
                route = {"results_index": "/api/results?timeline=demo", "results_at": "/api/results/at?timeline=demo&position=1",
                         "results_summary": "/api/results/summary?timeline=demo"}[name]
                status, headers, body = get(route)
                self.assertEqual(status, 500)
                self.assertNotIn(secret, json.dumps(body))
                self.assertNotIn("/home", json.dumps(body))
                self.assertIn("script-src 'self'", headers["Content-Security-Policy"])

    def test_documents_hold_no_credentials_or_paths(self):
        run, recorded = self.saved(WIN_LOSS)
        for doc in (results.results_index(recorded, self.root), results.results_at(recorded, 4, self.root),
                    results.results_summary(recorded, self.root)):
            encoded = json.dumps(doc)
            self.assertNotIn(str(self.root), encoded)
            self.assertIsNone(re.search(r"sk-[A-Za-z0-9]{16,}", encoded))
            self.assertNotIn("api_key", encoded.lower())
            self.assertTrue(doc["simulated"] and doc["read_only"])


class DemoResultsTests(unittest.TestCase):
    def test_demo_events_still_validate_and_correlate(self):
        events = demo_events()
        for event in events:                                  # same payload rules as real events
            validate_event({"contract": "execution_event", "version": "1.0", "event_id": event_id("rtl-" + "0" * 24, event["sequence"]),
                            "origin": "reconstructed", "timeline_id": "rtl-" + "0" * 24, "correlation_id": "cor-" + "0" * 24,
                            **{k: event[k] for k in ("run_id", "department", "component", "stage", "sequence", "event_type",
                                                     "status", "sim_time_utc", "recorded_at", "reason_codes", "refs", "details")}})
        view, run = demo_inputs()
        mapping, complete = results.correlate(view, run, allow_other_components=True)
        self.assertTrue(complete)
        self.assertEqual(results.replay_support(view, run, mapping, complete), ("available", []))
        self.assertEqual(len(demo_scene()["frames"]), len(events))
        with self.assertRaises(NetworkError):                 # real timelines may hold simulator events only
            results.correlate(view, run)

    def test_demo_figures_follow_the_documented_cost_model(self):
        price = Fraction("101.90") * (1 + Fraction(5, 10000))   # 101.95095: exact at 8 places
        notional, fee = price * 10, Fraction(1)
        cash = Fraction(10000) - notional - fee
        mark = Fraction("101.60")
        summary = results.results_summary("demo")
        self.assertTrue(summary["demo"])
        self.assertIn("DEMO DATA", summary["notice"])
        self.assertEqual((summary["account"]["ending_cash"], summary["account"]["ending_equity"],
                          summary["account"]["unrealized_pnl"]),
                         (text(cash), text(cash + mark * 10), text(mark * 10 - notional - fee)))
        report = demo_report()
        self.assertEqual(report["drawdown"]["max_dollars"], text(Fraction(10000) - (cash + Fraction("101.10") * 10)))
        self.assertEqual(report["closed_trades"]["win_rate_percent"]["reason"], "no_closed_trades")
        self.assertEqual(results.results_summary("demo"), summary)            # deterministic

    def test_demo_positions_do_not_leak(self):
        frames = len(demo_events())
        fill_frame = next(i for i, e in enumerate(demo_events()) if e["event_type"] == "simulated_fill")
        before = results.results_at("demo", fill_frame)
        self.assertEqual(before["portfolio"]["fills"], [])
        self.assertEqual(before["portfolio"]["orders"][0]["status"], "pending")
        self.assertNotIn(demo_report()["equity_curve"][-1]["equity"], json.dumps(before["portfolio"]["equity_curve"]))
        self.assertEqual(results.results_at("demo", 0)["status"], "not_started")
        self.assertEqual(results.results_at("demo", frames)["portfolio"]["cash"], results.results_summary("demo")["account"]["ending_cash"])


@unittest.skipUnless(os.environ.get("RUN_LOCAL_BROWSER_TESTS") == "1", "set RUN_LOCAL_BROWSER_TESTS=1 (needs Playwright + Chromium)")
class ResultsBrowserTests(ResultsBase):
    def test_results_desk_in_a_real_browser(self):
        from playwright.sync_api import sync_playwright
        run, recorded = self.saved(WIN_LOSS)
        server = HQServer(0, self.root)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for width, height in ((1600, 900), (390, 844)):
                    page = browser.new_page(viewport={"width": width, "height": height})
                    problems, posts = [], []
                    page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
                    page.on("pageerror", lambda e: problems.append(str(e)))
                    page.on("request", lambda r: posts.append(r.url) if r.method != "GET" else None)
                    page.goto(url)
                    page.wait_for_selector(".bot[data-bot='creator']")
                    page.click("#btn-play")                                   # pause the looping demo
                    page.click(".station-btn:has-text('Simulator')")          # the simulator station opens the desk
                    page.wait_for_selector("#results-view:not([hidden]) .facts")
                    self.assertEqual(page.inner_text("#results-badge"), "DEMO DATA · SYNTHETIC · SIMULATED")
                    page.keyboard.press("Home")
                    for _ in range(21):
                        page.click("#btn-forward")                            # just after the accepted order decision
                    page.wait_for_selector("text=No fills yet.")
                    self.assertIn("pending", page.inner_text("#results-body table:has(caption:has-text('orders so far'))"))
                    for _ in range(3):
                        page.click("#btn-forward")                            # event 24: the simulated fill
                    page.wait_for_selector("text=$8,979.4905")
                    page.click("#res-tab-summary")
                    page.wait_for_selector(".summary-label")
                    self.assertIn("Unavailable (no closed trades)", page.inner_text("#results-body"))
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    page.keyboard.press("p")                                  # presentation keeps the desk and the badge
                    self.assertTrue(page.is_visible("#results-view"))
                    self.assertTrue(page.is_visible("#mode-badge"))
                    page.keyboard.press("Escape")
                    page.click(".view-btn[data-view='timeline']")
                    page.click(f".timeline-item:has-text('{recorded}')")
                    page.wait_for_selector(f"#timeline-name:has-text('{recorded}')")
                    page.keyboard.press("r")
                    page.wait_for_selector("#results-badge:has-text('SAVED SIMULATION')")
                    page.click("#res-tab-position")
                    page.keyboard.press("End")
                    page.wait_for_selector("text=" + "$" + "{:,}".format(int(run["summary"]["ending_cash"].split(".")[0])))
                    self.assertIn("recorded", page.inner_text("#results-where"))
                    self.assertIn("simulated market time", page.inner_text("#results-where"))
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    self.assertEqual((problems, posts), ([], []))
                    page.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
