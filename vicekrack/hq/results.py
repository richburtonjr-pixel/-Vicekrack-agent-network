"""Living HQ trading results desk (Step 34): read-only views of saved simulation results.

Three documents (contract `hq_results` 1.0, see schemas/hq-results.schema.json), all for
one selected simulation timeline (`srun-...` reconstructed, `tl-...` recorded, or `demo`):

  index     who the run is, how the timeline, run and analytics report were correlated,
            whether an intermediate (replay-position) portfolio can be shown, and the
            outcome-free limitations. It holds NO results.
  position  the portfolio AFTER the first N events of the timeline: only orders, history
            entries and fills whose events are at or before N, and equity points whose bar
            had closed by that event's simulated time. Nothing later is sent.
  summary   the completed run summary (clearly labelled): final account, closed-trade
            statistics with `unavailable` values kept, drawdown, attribution, limitations.

Correlation uses validated IDs and hashes only, never filenames or timestamps:
- the timeline is loaded by the Step 31 loader and the run by the Step 29 store (both
  re-validate); the timeline must be a trading `simulation` naming that run's ID;
- every trading event must equal the event the run's own order history implies
  (`order_event_fields` / `fill_event_fields`: simulated time, reason codes, refs and
  details), in each order's history order; dataset and run refs must match. Anything else
  is `timeline_run_mismatch` and nothing is combined;
- an analytics report counts only if it is valid (Step 30 store) and its source run ID,
  run results hash, policy hash, dataset ID, bars hash and account ID all equal the run's,
  and its account figures equal the run summary. Reports that claim the run but differ
  are rejected and listed by ID and code.

Intermediate state is shown only if the timeline is complete with no issues, every order
history entry appears exactly once, simulated times never go backwards, and folding the
events reproduces the run's ending cash, fees and realized P&L. Otherwise the position view
is `unavailable` with reason codes; nothing is invented.

Marks at a position: the close of the last bar that had closed by that simulated time,
taken from the correlated analytics equity curve. Without a report, equity and unrealized
P&L of an open position are `unavailable`.

Nothing here runs a simulation, generates analytics, writes files, touches accounts or
calls providers: it only loads saved, re-validated records.
"""

import json
import re
from decimal import Decimal, localcontext
from functools import lru_cache

from jsonschema import Draft202012Validator

from ..errors import NetworkError
from ..events.contract import ROOT
from ..trading.money import fmt, parse
from ..trading.timeline import SIM_ENGINE, fill_event_fields, order_event_fields

RUN_ID = re.compile(r"^srun-[0-9a-f]{24}$")
REPORT_ID = re.compile(r"^sarp-[0-9a-f]{24}$")
MAX_REPORT_SCAN = 500
NOTICE = ("SIMULATED results of one offline Step 29 simulation on stored historical data, with Step 30 analytics. "
          "Read-only: no orders, brokers, live data, accounts or AI calls. Not a prediction, not advice.")
DEMO_NOTICE = "DEMO DATA: synthetic and deterministic; not a saved simulation run. " + NOTICE
SEPARATION = ("Simulator policy checks and order decisions (Step 29) are shown here. Research-agent conclusions "
              "(Step 28, the upstairs rooms) are a separate workflow and are never used by the simulator.")
CORRELATION_METHOD = ("Matched by validated IDs and hashes: timeline run ID, every order and fill event against the run's "
                      "own order history, and the analytics report's run, results, policy, dataset and account hashes. "
                      "Filenames and timestamps are never used to link records.")
STATUS_AT = {"accepted": "pending", "rejected": "rejected", "filled": "filled",
             "pending_at_end_of_data": "pending_at_end_of_data"}


def available(value):
    return {"status": "available", "value": fmt(value) if isinstance(value, Decimal) else value, "reason": None}


def unavailable(reason):
    return {"status": "unavailable", "value": None, "reason": reason}


def _mul(a, b):
    with localcontext() as context:
        context.prec = 50
        return a * b


def _div(a, b):
    with localcontext() as context:
        context.prec = 50
        return a / b


@lru_cache(maxsize=None)
def _validator():
    return Draft202012Validator(json.loads((ROOT / "schemas/hq-results.schema.json").read_text(encoding="utf-8")))


def check(document):
    """Every results document must match its contract before it leaves the server."""
    if next(_validator().iter_errors(document), None) is not None:
        raise NetworkError("invalid_results", "The results document does not match its contract.")
    return document


# ---------------------------------------------------------------- correlation
def _mismatch():
    return NetworkError("timeline_run_mismatch", "The timeline's events do not match the saved simulation run.")


def correlate(view, run, *, allow_other_components=False):
    """One entry per timeline event: None (another component, demo only), a stage event, or the
    order history entry it represents. Raises timeline_run_mismatch on any disagreement."""
    orders = {o["order_id"]: o for o in run["orders"]}
    fills = {f["fill_id"]: f for f in run["fills"]}
    cursor, mapping = {}, []
    for event in view["events"]:
        if event["component"] != SIM_ENGINE:
            if not allow_other_components:
                raise _mismatch()
            mapping.append(None)
            continue
        kind = event["event_type"]
        if kind in ("order_decision", "simulated_fill"):
            ids = [r["id"] for r in event["refs"] if r["kind"] == "order"]
            if len(ids) != 1 or ids[0] not in orders:
                raise _mismatch()
            order = orders[ids[0]]
            number = cursor.get(order["order_id"], 0)
            if number >= len(order["history"]):
                raise _mismatch()
            entry = order["history"][number]
            if event["status"] != entry["status"] or (kind == "simulated_fill") != (entry["status"] == "filled"):
                raise _mismatch()
            if kind == "simulated_fill":
                fill = fills.get(order["fill_id"])
                if fill is None:
                    raise _mismatch()
                expected = fill_event_fields(fill, order, entry["at_utc"])
            else:
                expected = order_event_fields(order, entry)
            actual = {"sim_time": event["sim_time_utc"], "reason_codes": event["reason_codes"], "refs": event["refs"],
                      "details": event["details"]}
            if actual != expected:
                raise _mismatch()
            cursor[order["order_id"]] = number + 1
            mapping.append({"kind": kind, "order_id": order["order_id"], "entry": number,
                            "fill_id": order["fill_id"] if kind == "simulated_fill" else None})
        else:
            for ref in event["refs"]:
                if ((ref["kind"] == "dataset" and ref["id"] != run["dataset"]["dataset_id"])
                        or (ref["kind"] == "simulation_run" and ref["id"] != run["run_id"])):
                    raise _mismatch()
            mapping.append({"kind": kind})
    complete = all(cursor.get(o["order_id"], 0) == len(o["history"]) for o in run["orders"])
    return mapping, complete


def _fold(run, mapping, upto):
    """Account state after mapping[:upto], applying fills in event order."""
    orders = {o["order_id"]: o for o in run["orders"]}
    fills = {f["fill_id"]: f for f in run["fills"]}
    cash = parse(run["account"]["initial_cash"], "money")
    state = {"cash": cash, "fees": Decimal(0), "realized": Decimal(0), "open": None, "closed": [], "fills": [],
             "seen": {}, "started": False, "last_sim_time": None}
    for item in mapping[:upto]:
        if item is None:
            continue
        state["started"] = True
        if item["kind"] not in ("order_decision", "simulated_fill"):
            continue
        order = orders[item["order_id"]]
        state["seen"][order["order_id"]] = item["entry"] + 1
        if item["kind"] != "simulated_fill":
            continue
        fill = fills[item["fill_id"]]
        state["fills"].append(fill)
        notional, fee = parse(fill["notional"]), parse(fill["fee"])
        state["cash"] += parse(fill["cash_change"], "signed_decimal")
        state["fees"] += fee
        if fill["side"] == "buy":
            state["open"] = {"entry_fill_id": fill["fill_id"], "order_id": order["order_id"], "quantity": fill["quantity"],
                             "opened_at_utc": fill["bar_open_utc"], "entry_price": fill["fill_price"],
                             "cost_basis": notional + fee, "strategy": order["source"]["strategy"],
                             "signal_id": order["source"]["signal_id"]}
        else:
            position = state["open"]
            if position is None:
                raise _mismatch()
            pnl = notional - fee - position["cost_basis"]
            state["realized"] += pnl
            state["closed"].append({**position, "exit_fill_id": fill["fill_id"], "closed_at_utc": fill["bar_open_utc"],
                                    "exit_price": fill["fill_price"], "proceeds": notional, "exit_fee": fee,
                                    "realized_pnl": pnl})
            state["open"] = None
    return state


def replay_support(view, run, mapping, complete):
    """(status, reasons): can intermediate portfolio states be shown for this timeline?"""
    reasons = []
    if view["completeness"] != "complete":
        reasons.append("timeline_not_complete")
    if view["issues"]:
        reasons.append("timeline_has_issues")
    if not complete:
        reasons.append("timeline_missing_trade_events")
    times = [e["sim_time_utc"] for e, m in zip(view["events"], mapping) if m is not None]
    if any(t is None for t in times):
        reasons.append("simulated_time_missing")
    elif any(later < earlier for earlier, later in zip(times, times[1:])):
        reasons.append("timeline_not_chronological")
    if not reasons:
        final = _fold(run, mapping, len(mapping))
        summary = run["summary"]
        folded = [(t["entry_fill_id"], t["exit_fill_id"], fmt(t["realized_pnl"])) for t in final["closed"]]
        saved = [(p["entry_fill_id"], p["exit_fill_id"], p["realized_pnl"]) for p in run["positions"] if p["status"] == "closed"]
        still_open = [p["entry_fill_id"] for p in run["positions"] if p["status"] == "open"]
        if (fmt(final["cash"]) != summary["ending_cash"] or fmt(final["fees"]) != summary["fees_total"]
                or fmt(final["realized"]) != summary["realized_pnl"] or folded != saved
                or ([final["open"]["entry_fill_id"]] if final["open"] else []) != still_open):
            reasons.append("replay_state_inconsistent")
    return ("available", []) if not reasons else ("unavailable", reasons)


# ---------------------------------------------------------------- analytics correlation
def find_analytics(run, root=None, current_config_sha=None):
    """(analytics status dict, report or None). Reports are matched by content hashes only."""
    from ..trading.analytics.store import AnalyticsStore
    store = AnalyticsStore(root)
    paths = sorted(store.reports.glob("sarp-*.json")) if store.reports.is_dir() else []
    matched, rejected, skipped = [], [], 0
    for path in paths[:MAX_REPORT_SCAN]:
        if not REPORT_ID.match(path.stem):
            continue
        try:
            claimed = json.loads(path.read_text(encoding="utf-8"))["source"]["run_id"]
        except (OSError, ValueError, UnicodeError, KeyError, TypeError):
            skipped += 1                                    # unreadable: cannot tell which run it describes
            continue
        if claimed != run["run_id"]:
            continue
        try:
            report = store.load(path.stem)
        except NetworkError as error:
            rejected.append({"report_id": path.stem, "code": error.code})
            continue
        code = _report_mismatch(report, run)
        if code:
            rejected.append({"report_id": report["report_id"], "code": code})
        else:
            matched.append(report)
    status = {"status": "unavailable", "report_id": None, "reason": None, "rejected_reports": rejected[:20],
              "unreadable_reports_skipped": skipped, "scan_limited": len(paths) > MAX_REPORT_SCAN}
    if not matched:
        status["reason"] = "analytics_report_rejected" if rejected else "analytics_report_missing"
        return status, None
    if len(matched) > 1:
        preferred = [r for r in matched if r["source"]["analytics_config_sha256"] == current_config_sha]
        if len(preferred) != 1:
            status["reason"] = "analytics_report_ambiguous"
            return status, None
        matched = preferred
    report = matched[0]
    status.update(status="available", report_id=report["report_id"])
    return status, report


def _report_mismatch(report, run):
    source, dataset = report["source"], run["dataset"]
    if (source["run_results_sha256"], source["policy_sha256"], source["dataset_id"], source["dataset_bars_sha256"],
            source["simulation_account_id"]) != (run["results_sha256"], run["policy_sha256"], dataset["dataset_id"],
                                                 dataset["bars_sha256"], run["account"]["simulation_account_id"]):
        return "analytics_report_mismatch"
    account, summary = report["account"], run["summary"]
    closed = sum(1 for p in run["positions"] if p["status"] == "closed")
    if ((account["ending_cash"], account["ending_equity"], account["realized_pnl"], account["unrealized_pnl"],
         account["fees_total"], report["closed_trades"]["count"], report["period"]["bars"])
            != (summary["ending_cash"], summary["ending_equity"], summary["realized_pnl"], summary["unrealized_pnl"],
                summary["fees_total"], closed, summary["bars_processed"])):
        return "analytics_report_inconsistent"
    return None


# ---------------------------------------------------------------- shaping
def _identity(view, run, demo):
    dataset = run["dataset"]
    policy = run["policy"]
    return {
        "timeline": {"timeline_id": view["timeline_id"], "origin": view["origin"], "kind": view["kind"],
                     "completeness": view["completeness"], "time_basis": view["time_basis"],
                     "event_count": len(view["events"])},
        "run": {"run_id": run["run_id"], "results_sha256": run["results_sha256"], "policy_sha256": run["policy_sha256"],
                "simulation_account_id": run["account"]["simulation_account_id"],
                "initial_cash": fmt(parse(run["account"]["initial_cash"], "money")), "currency": run["account"]["currency"],
                "saved_at": run["created_at"]},
        "dataset": {k: dataset[k] for k in ("dataset_id", "symbol", "interval", "timezone", "data_label", "bars_sha256")},
        "strategies": [{"name": s["name"], "config_sha256": s["config_sha256"]} for s in run["strategies"]],
        "policy": {"sizing": policy["sizing"], "exits": policy["exits"], "costs": policy["costs"],
                   "limits": policy["limits"], "entry_strategies": policy["entry"]["strategies"]},
        "kill_switch": run["kill_switch"],
        "replay_window": {k: run["replay"][k] for k in ("start_utc", "end_utc", "step_seconds", "steps")},
        "demo": demo,
    }


def limitations(run, *, completed=None, analytics=None):
    """Plain-language limits. Outcome-dependent notes are added only for the completed summary."""
    policy, dataset = run["policy"], run["dataset"]
    rows = [
        {"area": "dataset", "code": "one_dataset",
         "text": f"One stored dataset ({dataset['symbol']}, {dataset['interval']} bars, labelled "
                 f"'{dataset['data_label']}'). It says nothing about other periods, symbols or market conditions."},
        {"area": "strategy", "code": "rule_based_signals",
         "text": "Entries come only from rule-based Step 27 research signals of: " + ", ".join(policy["entry"]["strategies"])
                 + ". No optimization was done; one policy, one run."},
        {"area": "cost_model", "code": "simple_costs",
         "text": f"Fills at the next bar's open with {policy['costs']['slippage_bps']} bps slippage, a fee of "
                 f"{policy['costs']['fee_per_order']} per order plus {policy['costs']['fee_bps']} bps. No spread, "
                 "volume, liquidity or market-impact model."},
        {"area": "simulation", "code": "narrow_scope",
         "text": "Long-only market orders, whole shares, one symbol, at most one position at a time; no partial fills."},
        {"area": "simulation", "code": "unrealized_without_exit_costs",
         "text": "Unrealized P&L is quantity x last close - cost basis, with no hypothetical exit costs. Open positions "
                 "are marked to the last close and never sold off."},
        {"area": "analytics", "code": "descriptive_only",
         "text": "Descriptive only: never annualized, no risk-adjusted ratios, equity sampled at bar closes only."},
    ]
    if run["kill_switch"]["engaged"]:
        rows.append({"area": "simulation", "code": "kill_switch_engaged",
                     "text": "The simulation kill switch was engaged at the start, so new entries were rejected."})
    if len(policy["entry"]["strategies"]) > 1:
        rows.append({"area": "strategy", "code": "shared_account",
                     "text": "Several strategies shared ONE simulation account and position limit; per-strategy figures "
                             "are shares of one run, not independent strategy tests."})
    if completed:
        summary = run["summary"]
        if summary["open_position_quantity"]:
            rows.append({"area": "simulation", "code": "open_position_at_end",
                         "text": "A position was still open when the data ended; it is marked to the last close, not sold."})
        if summary["orders_pending_at_end"]:
            rows.append({"area": "simulation", "code": "orders_pending_at_end",
                         "text": "Some orders were still waiting when the data ended (no later bar to fill them)."})
        if analytics is not None and analytics["status"] != "available":
            rows.append({"area": "analytics", "code": analytics["reason"],
                         "text": "No matching Step 30 analytics report is available, so closed-trade statistics, the "
                                 "equity curve and drawdown are not shown. Create one with analytics-generate RUN_ID --save."})
    return rows


def _order_at(order, entries, include_fill):
    source = order["source"]
    history = order["history"][:entries]
    return {"order_id": order["order_id"], "purpose": order["purpose"], "side": order["side"],
            "quantity": order["quantity"], "order_type": order["order_type"],
            "source": {k: source[k] for k in ("kind", "signal_id", "strategy", "rule", "decision_bar_sequence",
                                              "decision_at_utc")},
            "estimate": order["estimate"], "expires_at_utc": order["expires_at_utc"],
            "status": STATUS_AT[history[-1]["status"]], "history": history,
            "fill_id": order["fill_id"] if include_fill and history[-1]["status"] == "filled" else None}


def _fill_row(fill):
    return {k: fill[k] for k in ("fill_id", "order_id", "side", "quantity", "bar_sequence", "bar_open_utc", "open_price",
                                 "slippage_bps", "fill_price", "notional", "fee", "cash_change", "gap_before_fill",
                                 "reason_codes")}


def _money(value):
    return fmt(value)


def position_document(view, run, mapping, support, report, position, demo):
    """The portfolio after the first `position` events. Only past information is included."""
    events = view["events"]
    if not isinstance(position, int) or not 0 <= position <= len(events):
        raise NetworkError("results_position_out_of_range", "The replay position is outside this timeline.")
    event = events[position - 1] if position else None
    doc = {"contract": "hq_results", "version": "1.0", "view": "replay_position", "simulated": True, "demo": demo,
           "read_only": True, "timeline_id": view["timeline_id"], "run_id": run["run_id"], "position": position,
           "positions": len(events), "event": None, "simulated_time_utc": None, "status": "unavailable", "reasons": [],
           "portfolio": None, "notice": DEMO_NOTICE if demo else NOTICE}
    if event is not None:
        doc["event"] = {k: event[k] for k in ("sequence", "component", "event_type", "status", "sim_time_utc", "recorded_at")}
    if support[0] != "available":
        doc["reasons"] = list(support[1])
        return check(doc)
    state = _fold(run, mapping, position)
    sims = [e["sim_time_utc"] for e, m in zip(events[:position], mapping[:position]) if m is not None]
    if not state["started"]:
        doc.update(status="not_started", reasons=["simulator_not_started_at_position"])
        return check(doc)
    now = sims[-1]
    doc["simulated_time_utc"] = now
    curve = [p for p in report["equity_curve"] if p["at_utc"] <= now] if report else []
    mark_point = curve[-1] if curve and curve[-1]["close"] is not None else None
    position_open = state["open"]
    shares = position_open["quantity"] if position_open else 0
    if position_open and mark_point is None:
        reason = "analytics_report_missing" if report is None else "no_bar_closed_yet"
        market_value = unrealized = equity = unavailable(reason)
    else:
        mark = parse(mark_point["close"]) if mark_point else None
        value = _mul(mark, shares) if position_open else Decimal(0)
        market_value = available(value)
        unrealized = available(value - position_open["cost_basis"]) if position_open else available(Decimal(0))
        equity = available(state["cash"] + value)
    orders = {o["order_id"]: o for o in run["orders"]}
    open_rows = []
    if position_open:
        open_rows.append({"entry_fill_id": position_open["entry_fill_id"], "order_id": position_open["order_id"],
                          "strategy": position_open["strategy"], "signal_id": position_open["signal_id"],
                          "quantity": shares, "opened_at_utc": position_open["opened_at_utc"],
                          "entry_price": position_open["entry_price"], "cost_basis": _money(position_open["cost_basis"]),
                          "mark_price": mark_point["close"] if mark_point else None,
                          "marked_at_utc": mark_point["at_utc"] if mark_point else None,
                          "market_value": market_value, "unrealized_pnl": unrealized})
    peak_dollars = max((parse(p["drawdown"]) for p in curve), default=None)
    peak_percent = max((parse(p["drawdown_percent"]) for p in curve), default=None)
    doc.update(status="available", portfolio={
        "initial_cash": fmt(parse(run["account"]["initial_cash"], "money")), "cash": _money(state["cash"]),
        "equity": equity, "realized_pnl": _money(state["realized"]), "unrealized_pnl": unrealized,
        "fees": _money(state["fees"]), "open_quantity": shares, "market_value": market_value,
        "mark": {"price": mark_point["close"], "bar_sequence": mark_point["sequence"], "at_utc": mark_point["at_utc"]}
        if mark_point else None,
        "mark_basis": "Close of the last bar that had closed by this simulated time (from the correlated analytics curve).",
        "open_positions": open_rows,
        "closed_trades": [{"entry_fill_id": t["entry_fill_id"], "exit_fill_id": t["exit_fill_id"], "strategy": t["strategy"],
                           "signal_id": t["signal_id"], "quantity": t["quantity"], "opened_at_utc": t["opened_at_utc"],
                           "closed_at_utc": t["closed_at_utc"], "entry_price": t["entry_price"], "exit_price": t["exit_price"],
                           "realized_pnl": _money(t["realized_pnl"]),
                           "outcome": "win" if t["realized_pnl"] > 0 else "loss" if t["realized_pnl"] < 0 else "breakeven"}
                          for t in state["closed"]],
        "orders": [_order_at(orders[oid], count, True) for oid, count in state["seen"].items()],
        "fills": [_fill_row(f) for f in state["fills"]],
        "equity_curve": [_curve_point(p) for p in curve],
        "equity_curve_status": available(len(curve)) if report else unavailable("analytics_report_missing"),
        "drawdown_so_far": {"max_dollars": available(peak_dollars) if peak_dollars is not None else
                            unavailable("analytics_report_missing" if report is None else "no_bar_closed_yet"),
                            "max_percent": available(peak_percent) if peak_percent is not None else
                            unavailable("analytics_report_missing" if report is None else "no_bar_closed_yet")},
    })
    return check(doc)


def _curve_point(point):
    return {k: point[k] for k in ("sequence", "at_utc", "close", "cash", "position_quantity", "market_value", "equity",
                                  "peak_equity", "drawdown", "drawdown_percent")}


def index_document(view, run, support, analytics, demo):
    doc = {"contract": "hq_results", "version": "1.0", "view": "index", "simulated": True, "read_only": True,
           **_identity(view, run, demo),
           "correlation": {"timeline_run": "verified", "method": CORRELATION_METHOD, "analytics": analytics},
           "replay_state": {"status": support[0], "reasons": list(support[1])},
           "separation": SEPARATION, "limitations": limitations(run), "notice": DEMO_NOTICE if demo else NOTICE}
    return check(doc)


def summary_document(view, run, analytics, report, demo):
    summary = run["summary"]
    initial = parse(run["account"]["initial_cash"], "money")
    account = {"initial_cash": fmt(initial), "ending_cash": summary["ending_cash"], "ending_equity": summary["ending_equity"],
               "realized_pnl": summary["realized_pnl"], "unrealized_pnl": summary["unrealized_pnl"],
               "fees_total": summary["fees_total"], "open_position_quantity": summary["open_position_quantity"],
               "last_close": summary["last_close"], "last_bar_close_utc": summary["last_bar_close_utc"],
               "net_return": fmt(parse(summary["ending_equity"]) - initial),
               "net_return_percent": available(fmt(_div((parse(summary["ending_equity"]) - initial) * 100, initial)))}
    counts = {k: summary[k] for k in ("bars_processed", "signals_seen", "orders_created", "orders_rejected", "orders_filled",
                                      "orders_pending_at_end", "fills")}
    section = None
    if report is not None:
        section = {k: report[k] for k in ("report_id", "results_sha256", "created_at", "period", "closed_trades",
                                          "open_positions", "drawdown", "holding", "exposure", "orders", "attribution")}
        section["account"] = {k: report["account"][k] for k in ("net_return", "net_return_percent")}
        section["equity_curve"] = [_curve_point(p) for p in report["equity_curve"]]
        section["analytics_config_sha256"] = report["source"]["analytics_config_sha256"]
    doc = {"contract": "hq_results", "version": "1.0", "view": "completed_run_summary", "simulated": True, "read_only": True,
           **_identity(view, run, demo),
           "label": "Completed run summary: end-of-run results. Not tied to the replay position.",
           "account": account, "counts": counts,
           "positions": [{k: p[k] for k in ("status", "quantity", "entry_fill_id", "exit_fill_id", "opened_at_utc",
                                            "closed_at_utc", "cost_basis", "proceeds", "exit_fee", "realized_pnl",
                                            "mark_price", "marked_at_utc", "market_value", "unrealized_pnl", "bars_held")}
                         for p in run["positions"]],
           "orders": [_order_at(o, len(o["history"]), True) for o in run["orders"]],
           "fills": [_fill_row(f) for f in run["fills"]],
           "analytics": {**analytics, "report": section},
           "separation": SEPARATION, "limitations": limitations(run, completed=True, analytics=analytics),
           "notice": DEMO_NOTICE if demo else NOTICE}
    return check(doc)


# ---------------------------------------------------------------- loading
def load(timeline_id, root=None):
    """(view, run, mapping, complete, demo) for one simulation timeline; validated and correlated."""
    if timeline_id == "demo":
        from .results_demo import demo_inputs
        view, run = demo_inputs()
        mapping, complete = correlate(view, run, allow_other_components=True)
        return view, run, mapping, complete, True
    from ..events.cli import load_timeline
    from ..trading.simulation.store import SimulationStore
    view = load_timeline(timeline_id, root)
    if view["department"] != "trading" or view["kind"] != "simulation":
        raise NetworkError("results_not_simulation", "This timeline is not a trading simulation.")
    if not view["run_id"] or not RUN_ID.match(view["run_id"]):
        raise NetworkError("results_run_missing", "This timeline does not name a saved simulation run.")
    run = SimulationStore(root).load(view["run_id"])
    if run["run_id"] != view["run_id"]:
        raise _mismatch()
    mapping, complete = correlate(view, run)
    return view, run, mapping, complete, False


def _analytics(run, root, demo):
    if demo:
        from .results_demo import demo_report
        report = demo_report()
        return {"status": "available", "report_id": report["report_id"], "reason": None, "rejected_reports": [],
                "unreadable_reports_skipped": 0, "scan_limited": False}, report
    from ..trading.analytics.store import load_analytics_config
    try:
        _, config_sha = load_analytics_config()
    except NetworkError:
        config_sha = None
    return find_analytics(run, root, config_sha)


def results_index(timeline_id, root=None):
    view, run, mapping, complete, demo = load(timeline_id, root)
    analytics, _ = _analytics(run, root, demo)
    return index_document(view, run, replay_support(view, run, mapping, complete), analytics, demo)


def results_at(timeline_id, position, root=None):
    view, run, mapping, complete, demo = load(timeline_id, root)
    _, report = _analytics(run, root, demo)
    return position_document(view, run, mapping, replay_support(view, run, mapping, complete), report, position, demo)


def results_summary(timeline_id, root=None):
    view, run, _, _, demo = load(timeline_id, root)
    analytics, report = _analytics(run, root, demo)
    return summary_document(view, run, analytics, report, demo)
