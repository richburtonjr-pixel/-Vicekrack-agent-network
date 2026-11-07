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

  Exit case (a possible sell), every cycle for every open position:
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
from .verifier import verify

GOOD = {"market_scout": "data_available", "trend_agent": "trend_assessed",
        "strategy_agent": "active_research_signal", "risk_review": "sufficient_for_future_paper_evaluation"}


def load_desk_config(path="config/trading-desk.json", root=ROOT):
    root = Path(root).resolve()
    target = (root / path).resolve()
    try:
        if not target.is_relative_to(root):
            raise ValueError
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        if config["config_version"] != "1.0" or not isinstance(config["auto_practice_trades"], bool):
            raise ValueError
        parse(config["order_dollars"], "money")
        parse(config["min_dollars_above_floor"], "money")
        for section, keys in (("verifier", ("max_bar_age_seconds", "gap_check_bars")),
                              ("risk", ("max_entries_per_day", "reentry_wait_minutes", "pause_after_losses",
                                        "pause_minutes")),
                              ("monitor", ("max_hold_minutes", "ema_fast", "ema_slow"))):
            for key in keys:
                value = config[section][key]
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise ValueError
        parse(config["verifier"]["max_last_bar_move_pct"])
        stop = parse(config["monitor"]["stop_loss_pct"])
        if not 0 < stop < 50 or config["monitor"]["ema_fast"] >= config["monitor"]["ema_slow"]:
            raise ValueError
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError, TradingError):
        raise TradingError("invalid_desk_config", "The trade desk settings are invalid.") from None
    return config


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
    entries = sum(1 for c in cases_today if c["kind"] == "entry" and c["outcome"] == "executed"
                  and c.get("account_id") == account_id)
    last_exit = {}
    for case in cases_today:
        if case["kind"] == "exit" and case["outcome"] == "executed" and case.get("account_id") == account_id:
            last_exit[case["symbol"]] = case["opened_at"]
    streak, last_loss = 0, None
    for line in ledger:
        if line.get("account_id") != account_id or line.get("kind") != "fill" or line.get("side") != "sell":
            continue
        if line.get("realized_pnl") is not None and parse(line["realized_pnl"], "signed_decimal") < 0:
            streak, last_loss = streak + 1, line["at"]
        else:
            streak = 0
    return {"entries_today": entries, "last_exit_at": last_exit, "losing_streak": streak, "last_loss_at": last_loss}


def _execute(store, practice_config, case, operation, args, now):
    """The Executor: the only place a desk case reaches the practice account."""
    try:
        state, _, refusal = store.apply(practice_config, operation, *args, now=now)
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
        coin = by_symbol.get(symbol)
        dataset = datasets.get(symbol)
        reasons = agents.position_monitor(symbol, position, dataset["bars"] if dataset else [],
                                          state["marks"].get(symbol), now, desk_config["monitor"])
        if not reasons:
            continue
        case = new_case("exit", symbol, coin)
        case["signatures"].append({"agent": "position_monitor", "decision": "close", "reasons": reasons,
                                   "summary": "Asked to close: " + ", ".join(reasons) + "."})
        if not coin or not coin["ok"]:
            case["signatures"].append({"agent": "executor", "decision": "waiting", "reasons": ["no_fresh_price"],
                                       "summary": "No fresh price this cycle; will try again next cycle."})
            case["outcome"] = "waiting"
        else:
            state = _execute(store, practice_config, case, "sell", (symbol, coin["price"]), now) or state
            if case["outcome"] == "executed":
                history["last_exit_at"][symbol] = now
        cases.append(case)
        files.append(case)

    state = store.load(practice_config)
    summary = practice_book.summary(state, practice_config, now)

    # ---- entries: one case per coin whose research verdict is "sufficient"
    for coin in coins:
        research = coin.get("research") or {}
        if not coin["ok"] or research.get("verdict") != GOOD["risk_review"]:
            continue
        symbol = coin["symbol"]
        case = new_case("entry", symbol, coin)
        for agent in research["agents"]:
            ok = agent["status"] == "completed" and agent["conclusion"] == GOOD[agent["role"]]
            case["signatures"].append({"agent": agent["role"], "decision": "approve" if ok else "veto",
                                       "reasons": [agent["conclusion"] or agent["status"]],
                                       "summary": research["summaries"].get(agent["role"], "")})
        strategy = research.get("strategy_findings")
        dataset = datasets[symbol]
        verifier = verify(dataset["bars"], interval_minutes(dataset["interval"]), strategy, signal_config, now,
                          desk_config["verifier"])
        case["signatures"].append(verifier)
        # The research Risk Review signed above; its account checks are a second, separate signature.
        risk = agents.risk_manager(symbol, research["verdict"], summary, history, now,
                                   {**desk_config["risk"], "min_dollars_above_floor": desk_config["min_dollars_above_floor"]})
        risk["stage"] = "account_check"
        case["signatures"].append(risk)
        if any(s["decision"] == "veto" for s in case["signatures"]):
            case["outcome"] = "vetoed"
        elif not desk_config["auto_practice_trades"]:
            case["signatures"].append({"agent": "executor", "decision": "paused", "reasons": ["auto_trades_off"],
                                       "summary": "Every agent approved, but automatic practice trades are switched off."})
            case["outcome"] = "approved_not_executed"
        else:
            _execute(store, practice_config, case, "buy", (symbol, desk_config["order_dollars"], coin["price"]), now)
            if case["outcome"] == "executed":
                history["entries_today"] += 1
                state = store.load(practice_config)
                summary = practice_book.summary(state, practice_config, now)
        cases.append(case)
        files.append(case)

    state = store.load(practice_config)
    summary = practice_book.summary(state, practice_config, now)
    return {"auto_practice_trades": desk_config["auto_practice_trades"],
            "cases": [{"case_id": c["case_id"], "kind": c["kind"], "symbol": c["symbol"], "outcome": c["outcome"],
                       "vetoed_by": [s["agent"] for s in c["signatures"] if s["decision"] == "veto"],
                       "reasons": [r for s in c["signatures"] if s["decision"] in ("veto", "refused", "close")
                                   for r in s["reasons"]]} for c in cases],
            "performance": agents.performance(store.ledger(500), account_id, summary)}
