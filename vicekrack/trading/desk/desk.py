"""The trade desk (Trading PR 3): where the eight trading agents check each other's work.

Every trade idea becomes a CASE FILE that each agent signs in order. Any veto stops it.

  Entry case (a possible buy), only when the research verdict is "sufficient":
    1 Market Scout     data available          (from the research run)
    2 Trend Agent      trend assessed          (from the research run)
    3 Strategy Agent   active signal claimed   (from the research run)
    4 Verifier         redoes the math from raw prices; veto if it disagrees
    5 Risk Review      research complete + practice account checks; veto if unsafe
    6 Executor         the ONLY code that asks the practice account to buy, and only when every
                       earlier signature is "approve". The account's own $80 guard still runs
                       first and can still refuse.

  Exit case (a possible sell), every cycle for every position THE AGENTS opened (yours are yours):
    7 Position Monitor stop-loss, held too long, or trend turned down -> asks to close
    6 Executor         closes the whole position. Exits need no approval: they reduce risk.

  8 Performance Reviewer reads the results after the cycle.

Case files are appended to runtime/trading/desk/cases-YYYY-MM-DD.jsonl (one line per case), so
the HQ and phone can show who approved, who vetoed and why. Simulated money only.
"""

import json
from pathlib import Path

from ..contracts import ROOT, sha256
from ..errors import TradingError
from ..money import parse
from ..practice import account as practice_book
from ..providers.kraken import interval_minutes
from . import agents
from . import plans, profit_trail, safety, settings
from .verifier import verify, verify_momentum

GOOD = {"market_scout": "data_available", "trend_agent": "trend_assessed",
        "strategy_agent": "active_research_signal", "risk_review": "sufficient_for_future_paper_evaluation"}


def _check(config):
    """Raise ValueError (or KeyError/TypeError...) if a desk config is not usable."""
    if config["config_version"] != "1.0" or not isinstance(config["auto_practice_trades"], bool):
        raise ValueError
    parse(config["order_dollars"], "money")
    parse(config["min_dollars_above_floor"], "money")
    for section, keys in (("verifier", ("max_bar_age_seconds", "gap_check_bars")),
                          ("risk", ("max_agent_positions", "max_entries_per_day", "reentry_wait_minutes",
                                    "pause_after_losses", "pause_minutes")),
                          ("monitor", ("max_hold_minutes", "ema_fast", "ema_slow"))):
        for key in keys:
            value = config[section][key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError
    parse(config["verifier"]["max_last_bar_move_pct"])
    stop = parse(config["monitor"]["stop_loss_pct"])
    if not 0 < stop < 50 or config["monitor"]["ema_fast"] >= config["monitor"]["ema_slow"]:
        raise ValueError
    if "take_profit_pct" in config["monitor"] or "trailing_stop_pct" in config["monitor"]:
        raise ValueError                                    # replaced by profit_trail (no fixed take-profit)
    if "profit_trail" in config["monitor"]:                 # optional; leave out to switch it off
        profit_trail.validate(config["monitor"]["profit_trail"])
    if "max_daily_loss_dollars" in config["risk"] and not parse(config["risk"]["max_daily_loss_dollars"], "money") > 0:
        raise ValueError
    sizing = config.get("sizing")
    if sizing is not None:
        if sizing.get("mode", "risk") not in ("risk", "fixed") or not 2 <= sizing["atr_bars"] <= 200:
            raise ValueError
        for key, low, high in (("risk_per_trade_pct", "0.05", "5"), ("max_position_pct", "1", "100"),
                               ("atr_multiple", "0.5", "10"), ("min_stop_pct", "0.2", "20"), ("max_stop_pct", "0.5", "30")):
            if not parse(low) <= parse(sizing[key]) <= parse(high):
                raise ValueError
        if parse(sizing["min_stop_pct"]) > parse(sizing["max_stop_pct"]):
            raise ValueError
        if not parse(sizing["min_order_dollars"], "money") <= parse(sizing["max_order_dollars"], "money"):
            raise ValueError
    exits = config["monitor"]
    for key in ("breakeven_at_r", "partial_at_r", "time_stop_min_r"):
        if key in exits and not parse("0") < parse(exits[key]) <= parse("20"):
            raise ValueError
    if "partial_fraction" in exits and not parse("0") < parse(exits["partial_fraction"]) < parse("1"):
        raise ValueError
    for key in ("min_hold_minutes", "trend_bar_minutes"):
        if key in exits and (not isinstance(exits[key], int) or isinstance(exits[key], bool) or exits[key] < 0):
            raise ValueError
    cooldown = config["risk"].get("loss_cooldown_minutes", 0)
    if not isinstance(cooldown, int) or isinstance(cooldown, bool) or cooldown < 0:
        raise ValueError
    momentum = config.get("momentum_entry")
    if momentum is not None and "max_stretch_pct" in momentum and not parse("0") < parse(momentum["max_stretch_pct"]) <= parse("50"):
        raise ValueError
    if momentum is not None:
        if not isinstance(momentum["enabled"], bool) or not 1 <= momentum["ema_fast"] < momentum["ema_slow"] <= 100:
            raise ValueError
        if parse(momentum["min_score"], "signed_decimal") < 0 or parse(momentum["min_rise_15m_pct"]) < 0:
            raise ValueError


def load_desk_config(path="config/trading-desk.json", root=ROOT, dashboard=True):
    """The desk settings file, with the limits changed in the app laid on top (see settings.py).

    If the app's changes would make the settings unusable they are ignored as a whole and the file's settings
    are used; `_dashboard_overrides_ignored` then says so."""
    root = Path(root).resolve()
    target = (root / path).resolve()
    try:
        if not target.is_relative_to(root):
            raise ValueError
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        _check(config)
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError, TradingError):
        raise TradingError("invalid_desk_config", "The trade desk settings are invalid.") from None
    if not dashboard:
        return config
    merged = settings.overlay(config, settings.load_overrides(root))
    try:
        _check(merged)
        return merged
    except (ValueError, KeyError, TypeError, AttributeError, TradingError, IndexError):
        return {**config, "_dashboard_overrides": {}, "_dashboard_overrides_ignored": True}


class CaseFiles:
    def __init__(self, root=None):
        self.base = Path(root if root is not None else ROOT) / "runtime/trading/desk"

    def path_for(self, now):
        return self.base / f"cases-{now[:10]}.jsonl"

    def append(self, case):
        self.base.mkdir(parents=True, exist_ok=True)
        with self.path_for(case["opened_at"]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(case, ensure_ascii=False, allow_nan=False) + "\n")

    def read_day(self, now):
        path = self.path_for(now)
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue                      # a damaged line is skipped, never guessed
        return out

    def recent(self, limit=50):
        files = sorted(self.base.glob("cases-*.jsonl")) if self.base.is_dir() else []
        lines = []
        for path in files[-2:]:
            lines += path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def _history(cases_today, ledger, account_id, now):
    """What the Risk Manager needs to know about today. The day is the UTC day, like the case files."""
    entries = sum(1 for c in cases_today if c["kind"] == "entry" and c["outcome"] == "executed"
                  and c.get("account_id") == account_id)
    last_exit = {}
    for case in cases_today:
        if case["kind"] == "exit" and case["outcome"] == "executed" and case.get("account_id") == account_id:
            last_exit[case["symbol"]] = case["opened_at"]
    streak, last_loss, loss_by_symbol = 0, None, {}
    for line in ledger:
        if line.get("account_id") != account_id or line.get("kind") != "fill" or line.get("side") != "sell":
            continue
        if line.get("realized_pnl") is not None and parse(line["realized_pnl"], "signed_decimal") < 0:
            streak, last_loss = streak + 1, line["at"]
            if line.get("by") == "agents":
                loss_by_symbol[line["symbol"]] = line["at"]
        else:
            streak = 0
    pnl_today = sum((parse(l["realized_pnl"], "signed_decimal") for l in ledger
                     if l.get("account_id") == account_id and l.get("kind") == "fill" and l.get("by") == "agents"
                     and l.get("realized_pnl") is not None and str(l.get("at", ""))[:10] == now[:10]),
                    parse("0", "signed_decimal"))
    return {"entries_today": entries, "last_exit_at": last_exit, "losing_streak": streak, "last_loss_at": last_loss,
            "last_loss_at_by_symbol": loss_by_symbol, "agents_pnl_today": format(pnl_today, "f")}


def _momentum_idea(coin, research, momentum):
    """A momentum idea: the scanner ranked the coin (score >= min_score), and the research found clean data and a
    trend but no textbook signal on the very last bar. Anything else (bad data, research errors) is not an idea."""
    radar = coin.get("radar")
    if not radar or radar.get("score") is None:
        return False
    try:
        if parse(radar["score"], "signed_decimal") < parse(momentum["min_score"], "signed_decimal"):
            return False
    except TradingError:
        return False
    roles = {a["role"]: a for a in research["agents"]}
    return (roles.get("market_scout", {}).get("conclusion") == GOOD["market_scout"]
            and roles.get("trend_agent", {}).get("conclusion") == GOOD["trend_agent"]
            and all(a["status"] == "completed" for a in research["agents"]))


def _momentum_signatures(coin, research):
    radar = coin["radar"]
    signals = ", ".join(s.replace("_", " ") for s in radar.get("signals") or ["streak"])
    return [
        {"agent": "market_scout", "decision": "approve", "reasons": ["data_available", "scanner_pick"],
         "summary": f"Scanner pick: {signals}; score {radar['score']}; +{radar['rise_15m_pct']}% in 15 min. "
                    + research["summaries"].get("market_scout", "")},
        {"agent": "trend_agent", "decision": "approve", "reasons": ["trend_assessed"],
         "summary": research["summaries"].get("trend_agent", "")},
        {"agent": "strategy_agent", "decision": "approve", "reasons": ["momentum_signal"],
         "summary": f"Momentum idea from the scanner ({signals}); no textbook signal on the last bar, so the "
                    "Verifier checks the climb itself."},
        {"agent": "risk_review", "decision": "approve", "reasons": ["momentum_entry"],
         "summary": "Momentum entry allowed by settings; the account checks below still apply."},
    ]


def _execute(store, practice_config, case, operation, args, now, **options):
    """The Executor: the only place a desk case reaches the practice account. Always trades as "agents"."""
    try:
        state, _, refusal = store.apply(practice_config, operation, *args, now=now, by="agents", **options)
    except TradingError as error:
        refusal = error
        state = None
    if refusal is not None:
        case["signatures"].append({"agent": "executor", "decision": "refused", "reasons": [refusal.code],
                                   "summary": f"The practice account refused: {refusal.code}."})
        case["outcome"] = "refused"
    else:
        case["signatures"].append({"agent": "executor", "decision": "executed", "reasons": [f"{operation}_done"],
                                   "summary": f"Simulated {operation} recorded in the practice account."})
        case["outcome"] = "executed"
    return state


def run_desk(coins, datasets, *, store, practice_config, desk_config, signal_config, now, root=None):
    """One desk round. `coins`: watch reports; `datasets`: {symbol: dataset} for coins fetched this cycle."""
    files = CaseFiles(root)
    state = store.load(practice_config)
    account_id = state["account_id"]
    summary = practice_book.summary(state, practice_config, now)
    history = _history(files.read_day(now), store.ledger(500), account_id, now)
    emergency = safety.EmergencySwitch(root).state()
    history["emergency"] = emergency["engaged"]
    peaks = safety.Peaks(root).update(account_id, summary["positions"], state["marks"], now)
    costs = practice_config["costs"]
    side_cost_bps = parse(costs["slippage_bps"]) + parse(costs["fee_bps"])
    round_trip_pct = format(side_cost_bps * 2 / 100, "f")            # both sides, as % (35 bps a side -> 0.7)
    # Profit % is measured after the estimated cost of selling (slippage + fee), so "+14%" means about +14% kept.
    monitor = {**desk_config["monitor"], "exit_cost_bps": format(side_cost_bps, "f")}
    plan_book = plans.Plans(root)
    position_plans = plan_book.load(account_id)
    by_symbol = {c["symbol"]: c for c in coins}
    cases = []

    def new_case(kind, symbol, coin):
        return {"contract": "trade_desk_case", "version": "1.0", "simulated": True, "account_id": account_id,
                "case_id": "case-" + sha256({"kind": kind, "symbol": symbol, "at": now, "account": account_id})[:16],
                "kind": kind, "symbol": symbol, "opened_at": now, "price": coin["price"] if coin else None,
                "price_at": coin["price_at"] if coin else None,
                "research_run_id": (coin.get("research") or {}).get("run_id") if coin else None,
                "signatures": [], "outcome": None}

    # ---- exits first: the Position Monitor checks every open position
    for symbol, position in sorted(summary["positions"].items()):
        if position.get("opened_by", "agents") != "agents":
            continue                                   # the Position Monitor never sells your own trades
        coin = by_symbol.get(symbol)
        dataset = datasets.get(symbol)
        plan = position_plans.get(symbol)
        if plan is not None and plan.get("opened_at") != position["opened_at"]:
            plan = None
        mark = state["marks"].get(symbol)
        if plan is not None and mark is not None and not plan["breakeven_done"] and monitor.get("breakeven_at_r"):
            r = plans.r_multiple(plan, mark["price"])
            if r is not None and r >= parse(monitor["breakeven_at_r"]):
                plan["breakeven_done"] = True            # from now on the stop sits at break-even (plus costs)
        reasons = agents.position_monitor(symbol, position, dataset["bars"] if dataset else [],
                                          mark, now, monitor, peak=peaks.get(symbol),
                                          emergency=emergency["engaged"], plan=plan)
        if not reasons and agents.partial_due(plan, mark):
            case = new_case("trim", symbol, coin)
            case["signatures"].append({"agent": "position_monitor", "decision": "close", "reasons": ["partial_profit"],
                                       "summary": f"Up {plan['partial_at_r']}x the planned risk: selling "
                                                  f"{format(parse(plan['partial_fraction']) * 100, '.0f')}% and letting the rest run."})
            if not coin or not coin["ok"]:
                case["signatures"].append({"agent": "executor", "decision": "waiting", "reasons": ["no_fresh_price"],
                                           "summary": "No fresh price this cycle; will try again next cycle."})
                case["outcome"] = "waiting"
            else:
                bid = coin.get("bid") or coin["price"]
                dollars = format((parse(position["quantity"]) * parse(bid) * parse(plan["partial_fraction"]))
                                 .quantize(parse("0.01")), "f")
                state = _execute(store, practice_config, case, "sell", (symbol, bid), now, dollars=dollars) or state
                if case["outcome"] == "executed":
                    plan["partial_done"] = plan["breakeven_done"] = True
            cases.append(case)
            files.append(case)
            continue
        if not reasons:
            continue
        case = new_case("exit", symbol, coin)
        case["signatures"].append({"agent": "position_monitor", "decision": "close", "reasons": reasons,
                                   "summary": "Asked to close: " + ", ".join(reasons) + "."
                                   + (f" Plan: stop {plan['stop_pct']}% (${plan['risk_dollars']} at risk)." if plan else "")})
        if not coin or not coin["ok"]:
            case["signatures"].append({"agent": "executor", "decision": "waiting", "reasons": ["no_fresh_price"],
                                       "summary": "No fresh price this cycle; will try again next cycle."})
            case["outcome"] = "waiting"
        else:
            state = _execute(store, practice_config, case, "sell", (symbol, coin.get("bid") or coin["price"]), now) or state
            if case["outcome"] == "executed":
                history["last_exit_at"][symbol] = now
        cases.append(case)
        files.append(case)

    state = store.load(practice_config)
    summary = practice_book.summary(state, practice_config, now)

    # ---- entries: one case per coin whose research verdict is "sufficient"
    momentum = desk_config.get("momentum_entry") or {"enabled": False}
    for coin in coins:
        research = coin.get("research") or {}
        if not coin["ok"] or not research.get("agents"):
            continue
        idea = "signal" if research.get("verdict") == GOOD["risk_review"] else \
            "momentum" if momentum["enabled"] and _momentum_idea(coin, research, momentum) else None
        if idea is None:
            continue
        symbol = coin["symbol"]
        case = new_case("entry", symbol, coin)
        case["idea"] = idea
        dataset = datasets[symbol]
        if idea == "signal":
            for agent in research["agents"]:
                ok = agent["status"] == "completed" and agent["conclusion"] == GOOD[agent["role"]]
                case["signatures"].append({"agent": agent["role"], "decision": "approve" if ok else "veto",
                                           "reasons": [agent["conclusion"] or agent["status"]],
                                           "summary": research["summaries"].get(agent["role"], "")})
            verifier = verify(dataset["bars"], interval_minutes(dataset["interval"]), research.get("strategy_findings"),
                              signal_config, now, desk_config["verifier"])
        else:
            case["signatures"] += _momentum_signatures(coin, research)
            verifier = verify_momentum(dataset["bars"], interval_minutes(dataset["interval"]), now,
                                       desk_config["verifier"], momentum)
        case["signatures"].append(verifier)
        # The research Risk Review signed above; its account checks are a second, separate signature.
        verdict = research["verdict"] if idea == "signal" else GOOD["risk_review"]
        risk = agents.risk_manager(symbol, verdict, summary, history, now,
                                   {**desk_config["risk"], "min_dollars_above_floor": desk_config["min_dollars_above_floor"]})
        sizing = desk_config.get("sizing")
        sized = None
        if sizing is not None:
            sized = plans.size_entry(dataset["bars"], summary.get("equity") or "0", sizing, round_trip_pct,
                                     desk_config["order_dollars"])
            if "skip" in sized:
                risk = {**risk, "decision": "veto", "reasons": [*(risk["reasons"] if risk["decision"] == "veto" else []),
                                                                sized["skip"]],
                        "summary": "Trade would be too small to beat its own costs."}
            elif risk["decision"] == "approve":
                risk["summary"] += (f" Plan: stop {format(sized['stop_pct'].quantize(parse('0.01')), 'f')}% below cost, "
                                    f"${sized['dollars']} trade, about ${sized['risk_dollars']} at risk.")
        risk["stage"] = "account_check"
        case["signatures"].append(risk)
        if any(s["decision"] == "veto" for s in case["signatures"]):
            case["outcome"] = "vetoed"
        elif not desk_config["auto_practice_trades"]:
            case["signatures"].append({"agent": "executor", "decision": "paused", "reasons": ["auto_trades_off"],
                                       "summary": "Every agent approved, but automatic practice trades are switched off."})
            case["outcome"] = "approved_not_executed"
        else:
            dollars = sized["dollars"] if sized else desk_config["order_dollars"]
            _execute(store, practice_config, case, "buy", (symbol, dollars, coin.get("ask") or coin["price"]), now)
            if case["outcome"] == "executed":
                history["entries_today"] += 1
                state = store.load(practice_config)
                summary = practice_book.summary(state, practice_config, now)
                if sized and symbol in state["positions"]:
                    position_plans[symbol] = plans.new_plan(state["positions"][symbol], sized, desk_config["monitor"],
                                                            case["case_id"], idea, round_trip_pct)
                    case["plan"] = position_plans[symbol]
        cases.append(case)
        files.append(case)

    state = store.load(practice_config)
    summary = practice_book.summary(state, practice_config, now)
    plan_book.save(account_id, position_plans, state["positions"])
    return {"auto_practice_trades": desk_config["auto_practice_trades"], "emergency_stop": emergency,
            "agents_pnl_today": history["agents_pnl_today"],
            "cases": [{"case_id": c["case_id"], "kind": c["kind"], "symbol": c["symbol"], "outcome": c["outcome"],
                       "vetoed_by": [s["agent"] for s in c["signatures"] if s["decision"] == "veto"],
                       "reasons": [r for s in c["signatures"] if s["decision"] in ("veto", "refused", "close")
                                   for r in s["reasons"]]} for c in cases],
            "performance": agents.performance(store.ledger(500), account_id, summary)}
