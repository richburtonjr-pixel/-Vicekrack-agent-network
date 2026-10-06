"""Research-signal engine (Step 27): one bounded replay pass over closed bars only.

SignalConsumer is a Step 25 replay consumer. For every step it first lets the Step 26
IndicatorConsumer process the newly closed bars (gap policy is always `reset`), then
evaluates each strategy on each new bar in sequence order using only that bar, earlier
bars and indicator values already published for them. Results:

- `triggered`: the rule's transition happened on this bar and no cooldown was active.
  Exactly one research_signal record is created, with a deterministic ID.
- `not_triggered`: inputs were ready but the transition did not happen (or happened inside
  the cooldown: reason `cooldown_active`).
- `not_ready`: inputs were missing, unavailable or invalidated by a gap.

Cooldown: after a trigger on bar t, the next `cooldown_bars` processed closed bars cannot
trigger. Research signals carry `authorization_possible: false`; this module never imports
paper-account, risk, order or journal code and cannot reach them.
"""

from collections import deque

from ..contracts import sha256, validate_schema
from ..errors import TradingError
from ..indicators.engine import IndicatorConsumer, build_settings
from ..market.bars import available_at, from_utc_text, missing_between, utc_text
from ..market.replay import drive
from ..money import parse
from .strategies import RULES, history_needed, required_indicators

SIGNAL_NOTICE = ("Research signal from historical bars: a rule matched on closed data. Not advice, not a prediction or "
                 "profitability claim, not an order, and it cannot authorize anything.")
RUN_NOTICE = ("Research-only rule evaluations on closed historical bars (simulation clock). Not advice, not a "
              "profitability claim, no orders, no account access.")
PASSTHROUGH = {"indicator_window_too_small", "indicator_too_many_points", "signal_too_many_evaluations"}


# ---------------------------------------------------------------- configuration
def load_strategies(names, signal_config):
    """Validated [(name, definition, config_sha256)] for the requested named strategies."""
    validate_schema("research_signal_config", signal_config)
    limits = signal_config["limits"]
    if not names:
        raise TradingError("invalid_strategy_config", "Choose at least one strategy.")
    if len(names) != len(set(names)) or len(names) > limits["max_strategies"]:
        raise TradingError("invalid_strategy_config", "Strategies must be unique and within max_strategies.")
    chosen = []
    for name in names:
        definition = signal_config["strategies"].get(name)
        if definition is None:
            raise TradingError("unknown_strategy", "No strategy with that name in config/research-signals.json.")
        params = definition["params"]
        if definition["strategy"] == "ema_crossover" and not params["fast"] < params["slow"]:
            raise TradingError("invalid_strategy_config", "EMA crossover needs fast < slow.")
        if definition["strategy"] == "breakout" and params["volume_filter"] is not None:
            if parse(params["volume_filter"]["multiplier"]) <= 0:
                raise TradingError("invalid_strategy_config", "The volume filter multiplier must be greater than 0.")
        chosen.append((name, definition, sha256(definition)))
    sessions = {sha256(d["params"]["session"]) for _, d, _ in chosen if d["strategy"] == "vwap_reclaim"}
    if len(sessions) > 1:
        raise TradingError("invalid_strategy_config", "All VWAP strategies in one run must use the same session.")
    return chosen


def indicator_settings_for(dataset, strategies, indicator_config):
    ema, volume, vwap, session = set(), set(), False, None
    for _, definition, _ in strategies:
        for kind, period in required_indicators(definition).values():
            if kind == "ema":
                ema.add(period)
            elif kind == "volume_sma":
                volume.add(period)
            else:
                vwap, session = True, definition["params"]["session"]
    try:
        return build_settings(dataset, config=indicator_config, ema=sorted(ema), volume_sma=sorted(volume), vwap=vwap,
                              vwap_session=session, gap_policy="reset", allow_empty=True)
    except TradingError as error:
        if error.code == "invalid_indicator_settings":
            raise TradingError("invalid_strategy_config", "These strategies need indicator settings that are not valid "
                                                         "for this dataset (VWAP needs intraday bars).") from None
        raise


def _keys(definition):
    mapping = {}
    for role, (kind, period) in required_indicators(definition).items():
        mapping[role] = "vwap_session" if kind == "vwap" else f"{kind}_{period}"
    return mapping


# ---------------------------------------------------------------- consumer
class SignalConsumer:
    name = "research_signals"

    def __init__(self, dataset, strategies, indicator_settings, indicator_config, max_evaluations):
        self.dataset = dataset
        self.indicators = IndicatorConsumer(dataset, indicator_settings, indicator_config["limits"]["max_points"])
        self.strategies = [{"name": n, "definition": d, "config_sha256": c, "keys": _keys(d), "last_trigger": None,
                            "entries": []} for n, d, c in strategies]
        self.history = deque(maxlen=max(history_needed(d) for _, d, _ in strategies))
        self.vwap = any(d["strategy"] == "vwap_reclaim" for _, d, _ in strategies)
        self.processed = 0
        self.max_evaluations = max_evaluations
        self.evaluations = 0
        self.signals = []
        self.cooldown_suppressed = 0

    def on_step(self, view):
        before = self.indicators.last_sequence
        self.indicators.on_step(view)                    # validates the window and processes new closed bars
        for sequence in range(before + 1, self.indicators.last_sequence + 1):
            bar = view.bar(sequence)                     # visible by construction; later bars are refused
            if bar["available_at_utc"] > view.now:
                raise TradingError("future_bar_leak", "A future bar reached the signal engine.")
            self._process(bar, view.now)

    def final(self):
        return {"bars_evaluated": self.processed}

    def _process(self, bar, now):
        index = self.processed
        self.processed += 1
        start = from_utc_text(bar["timestamp_utc"])
        previous = self.history[-1] if self.history else None
        gap = previous is not None and missing_between(previous["start"], start, self.dataset["interval"],
                                                       self.dataset["timezone"], "signal") > 0
        session, inside = (self.indicators._session_of(bar["timestamp_utc"], bar["available_at_utc"])
                           if self.vwap else (None, True))
        item = {
            "sequence": bar["sequence"], "start": start, "close": parse(bar["close"]), "high": parse(bar["high"]),
            "volume": parse(bar["volume"]), "run": 1 if previous is None or gap else previous["run"] + 1,
            "total": self.processed, "session": session, "inside": inside,
            "points": {key: series[index] for key, series in self.indicators.series.items()},
        }
        self.history.append(item)
        history = list(self.history)
        for strategy in self.strategies:
            self.evaluations += 1
            if self.evaluations > self.max_evaluations:
                raise TradingError("signal_too_many_evaluations", "This run exceeds max_evaluations in config/research-signals.json.")
            definition = strategy["definition"]
            outcome, reasons, values, event = RULES[definition["strategy"]](definition["params"], history, strategy["keys"])
            signal_id = None
            if outcome == "triggered":
                last = strategy["last_trigger"]
                if last is not None and index - last <= definition["params"]["cooldown_bars"]:
                    outcome, reasons = "not_triggered", reasons + ["cooldown_active"]
                    self.cooldown_suppressed += 1
                else:
                    strategy["last_trigger"] = index
                    record = self._signal(strategy, bar, now, event, values, reasons)
                    self.signals.append(record)
                    signal_id = record["signal_id"]
            strategy["entries"].append({"sequence": bar["sequence"], "timestamp_utc": bar["timestamp_utc"],
                                        "computed_at_sim_utc": now, "outcome": outcome, "reason_codes": reasons,
                                        "values": values, "signal_id": signal_id})

    def _signal(self, strategy, bar, now, event, values, reasons):
        expires = from_utc_text(bar["available_at_utc"])
        for _ in range(strategy["definition"]["params"]["expiry_bars"]):
            expires = available_at(expires, self.dataset["interval"], self.dataset["timezone"])
        record = {
            "contract": "research_signal", "version": "1.0", "signal_id": None, "purpose": "research_only",
            "authorization_possible": False, "account_access": False,
            "strategy": strategy_ref(strategy), "dataset": provenance(self.dataset),
            "bar": {k: bar[k] for k in ("sequence", "timestamp", "timestamp_utc", "available_at_utc")},
            "event": event, "detected_at_sim_utc": now, "expires_at_utc": utc_text(expires),
            "expired_when_detected": now >= utc_text(expires),
            "supporting_values": values, "reason_codes": reasons, "notice": SIGNAL_NOTICE,
        }
        record["signal_id"] = signal_id_for(record)
        record["content_sha256"] = content_hash(record)
        validate_signal_record(record)
        return record


def strategy_ref(strategy):
    definition = strategy["definition"]
    return {"name": strategy["name"], "strategy": definition["strategy"], "version": definition["version"],
            "params": definition["params"], "config_sha256": strategy["config_sha256"]}


def provenance(dataset):
    return {"dataset_id": dataset["dataset_id"], "bars_sha256": dataset["bars_sha256"],
            "source_file_sha256": dataset["source"]["file_sha256"], "symbol": dataset["symbol"],
            "interval": dataset["interval"], "timezone": dataset["timezone"], "data_label": dataset["data_label"]}


def signal_id_for(record):
    """Same strategy name + configuration + dataset + bar -> same ID, always."""
    return "rsig-" + sha256({"strategy": record["strategy"]["name"], "config_sha256": record["strategy"]["config_sha256"],
                             "dataset_id": record["dataset"]["dataset_id"], "bars_sha256": record["dataset"]["bars_sha256"],
                             "sequence": record["bar"]["sequence"], "timestamp_utc": record["bar"]["timestamp_utc"]})[:24]


def content_hash(record):
    """Integrity of everything except the hash itself (any edit to values or reasons is detected)."""
    return sha256({k: v for k, v in record.items() if k != "content_sha256"})


def validate_signal_record(record):
    validate_schema("research_signal", record)
    strategy = record["strategy"]
    definition = {"strategy": strategy["strategy"], "version": strategy["version"], "params": strategy["params"]}
    validate_schema("research_signal_config", {"config_version": "1.0", "limits": {"max_strategies": 1, "max_evaluations": 1,
                                               "max_cli_entries": 1}, "strategies": {strategy["name"]: definition}})
    if sha256(definition) != strategy["config_sha256"]:
        raise TradingError("invalid_research_signal", "Research signal strategy configuration hash mismatch.")
    if signal_id_for(record) != record["signal_id"]:
        raise TradingError("invalid_research_signal", "Research signal ID does not match its content.")
    if content_hash(record) != record["content_sha256"]:
        raise TradingError("invalid_research_signal", "Research signal content hash mismatch.")
    if not record["bar"]["available_at_utc"] <= record["detected_at_sim_utc"]:
        raise TradingError("invalid_research_signal", "Research signal detected before its bar closed.")
    if not record["bar"]["available_at_utc"] < record["expires_at_utc"]:
        raise TradingError("invalid_research_signal", "Research signal expires before its bar closed.")
    if record["expired_when_detected"] != (record["detected_at_sim_utc"] >= record["expires_at_utc"]):
        raise TradingError("invalid_research_signal", "Research signal expiry flag does not match its timestamps.")


# ---------------------------------------------------------------- run
def run_signals(dataset, strategies, *, market_config, indicator_config, signal_config, created_at,
                start=None, end=None, step_seconds=None):
    settings = indicator_settings_for(dataset, strategies, indicator_config)
    consumer = SignalConsumer(dataset, strategies, settings, indicator_config, signal_config["limits"]["max_evaluations"])
    simulation, summary, _ = drive(dataset, [consumer], config=market_config, start=start, end=end,
                                   step_seconds=step_seconds, passthrough=PASSTHROUGH)
    outcomes = [e["outcome"] for s in consumer.strategies for e in s["entries"]]
    body = {
        "dataset": provenance(dataset),
        "strategies": [strategy_ref(s) for s in consumer.strategies],
        "indicator_settings_sha256": settings["settings_sha256"], "replay": simulation,
        "evaluations": [{"strategy": s["name"], "entries": s["entries"]} for s in consumer.strategies],
        "signals": consumer.signals,
        "summary": {"bars_evaluated": consumer.processed, "triggered": outcomes.count("triggered"),
                    "not_triggered": outcomes.count("not_triggered"), "not_ready": outcomes.count("not_ready"),
                    "cooldown_suppressed": consumer.cooldown_suppressed,
                    "future_access_attempts": summary["future_access_attempts"]},
    }
    run = {
        "contract": "research_signal_run", "version": "1.0",
        "run_id": "rsr-" + sha256({"dataset_id": dataset["dataset_id"], "bars": dataset["bars_sha256"],
                                   "strategies": [(s["name"], s["config_sha256"]) for s in consumer.strategies],
                                   "replay": simulation})[:24],
        "mode": "historical_replay", "purpose": "research_only", "authorization_possible": False, "account_access": False,
        **body, "results_sha256": sha256(body), "created_at": created_at, "notice": RUN_NOTICE,
    }
    validate_run(run)
    return run


def validate_run(run):
    """Schema, hashes, provenance and internal consistency of a stored or new run."""
    validate_schema("research_signal_run", run)
    body = {k: run[k] for k in ("dataset", "strategies", "indicator_settings_sha256", "replay", "evaluations",
                                "signals", "summary")}
    if sha256(body) != run["results_sha256"]:
        raise TradingError("invalid_signal_run", "Signal run hash mismatch.")
    strategies = {s["name"]: s for s in run["strategies"]}
    for strategy in run["strategies"]:
        definition = {k: strategy[k] for k in ("strategy", "version", "params")}
        if sha256(definition) != strategy["config_sha256"]:
            raise TradingError("invalid_signal_run", "Strategy configuration hash mismatch.")
    if [e["strategy"] for e in run["evaluations"]] != list(strategies):
        raise TradingError("invalid_signal_run", "Evaluations do not match the strategies.")
    triggered = {}
    for evaluation in run["evaluations"]:
        for entry in evaluation["entries"]:
            if (entry["outcome"] == "triggered") != (entry["signal_id"] is not None):
                raise TradingError("invalid_signal_run", "Only triggered evaluations carry a signal ID.")
            if entry["signal_id"]:
                triggered[entry["signal_id"]] = (evaluation["strategy"], entry["sequence"])
    if len(triggered) != len(run["signals"]):
        raise TradingError("invalid_signal_run", "Signals and triggered evaluations disagree.")
    for record in run["signals"]:
        validate_signal_record(record)
        name = record["strategy"]["name"]
        if (record["dataset"] != run["dataset"] or record["strategy"] != strategies.get(name)
                or triggered.get(record["signal_id"]) != (name, record["bar"]["sequence"])):
            raise TradingError("invalid_signal_run", "A signal's provenance does not match its run.")
