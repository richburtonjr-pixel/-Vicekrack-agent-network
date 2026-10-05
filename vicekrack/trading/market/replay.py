"""Bounded offline replay on a simulation clock (Step 25).

At each simulated time a consumer receives a ReplayView. The view is built from a slice
containing ONLY bars whose `available_at_utc` (bar close) is at or before the simulated
time, so future bars are simply not present in anything the consumer can reach. Asking
for a later bar through the view raises `future_bar_access` and is counted.

Historical replay is separate from paper-account authorization: this module never imports
the account state, risk engine, order or journal modules, never reads the wall clock for
decisions, and cannot produce a market_snapshot. A replay report says
`account_access: false` and `authorization_possible: false`.

Consumers here only observe. There are no indicators, signals or trades.
"""

from datetime import timedelta
from types import MappingProxyType

from ..contracts import sha256
from ..errors import TradingError
from .bars import INTERVAL_SECONDS, expand_bar, from_utc_text, utc_text

NOTICE = ("Offline historical replay of stored data on a simulation clock. Not current data, not a signal, "
          "no orders, no account access.")


class FutureBarAccess(TradingError):
    pass


def _freeze(bar):
    return MappingProxyType({k: MappingProxyType(dict(v)) if isinstance(v, dict) else v for k, v in bar.items()})


class SimulationClock:
    """Advances only when told to; never reads real time."""

    def __init__(self, start, step_seconds):
        self._now = start
        self._step = timedelta(seconds=step_seconds)

    @property
    def now(self):
        return self._now

    def advance(self):
        self._now += self._step


class ReplayView:
    """Read-only view of the bars available at one simulated time."""
    __slots__ = ("_now", "_window", "_visible", "_denied")

    def __init__(self, now_text, window, visible_count):
        self._now = now_text
        self._window = window              # tuple of read-only bars, all already closed
        self._visible = visible_count
        self._denied = 0

    @property
    def now(self):
        return self._now

    @property
    def visible_count(self):
        return self._visible

    def bars(self):
        return self._window

    def latest(self):
        return self._window[-1] if self._window else None

    def bar(self, sequence):
        """Bar by 1-based sequence number; later bars are refused, never returned."""
        if not isinstance(sequence, int) or sequence < 1:
            raise TradingError("invalid_bar_sequence", "Bar sequence numbers start at 1.")
        if sequence > self._visible:
            self._denied += 1
            raise FutureBarAccess("future_bar_access", "That bar is not available yet at this simulated time.")
        offset = sequence - (self._visible - len(self._window)) - 1
        if offset < 0:
            raise TradingError("bar_outside_window", "That bar is older than the replay window.")
        return self._window[offset]


class BarRecorder:
    """Observes what it is shown: counts, first/latest bar. No calculations or decisions."""
    name = "bar_recorder"

    def __init__(self):
        self.seen, self.first, self.latest, self.latest_close, self.latest_label = 0, None, None, None, None

    def on_step(self, view):
        latest = view.latest()
        if latest is None:
            return
        if latest["available_at_utc"] > view.now:      # defensive: must never happen
            raise TradingError("future_bar_leak", "A future bar reached a consumer.")
        self.seen = view.visible_count
        self.first = self.first or view.bars()[0]["timestamp_utc"]
        self.latest, self.latest_close, self.latest_label = latest["timestamp_utc"], latest["close"], latest["data_label"]

    def final(self):
        return {"bars_seen": self.seen, "first_seen_start_utc": self.first, "latest_start_utc": self.latest,
                "latest_close": self.latest_close, "data_label": self.latest_label}


class FutureProbe:
    """Tries to read the next (not yet available) bar every step to prove it is refused."""
    name = "future_probe"

    def __init__(self):
        self.attempts, self.refused, self.leaked = 0, 0, 0

    def on_step(self, view):
        self.attempts += 1
        try:
            view.bar(view.visible_count + 1)
            self.leaked += 1
        except FutureBarAccess:
            self.refused += 1

    def final(self):
        return {"attempts": self.attempts, "refused": self.refused, "leaked": self.leaked}


CONSUMERS = {"bar_recorder": BarRecorder, "future_probe": FutureProbe}


def plan(dataset, start=None, end=None, step_seconds=None, max_steps=5000):
    """Validated (start, end, step, steps) for a replay of this dataset."""
    interval_seconds = INTERVAL_SECONDS[dataset["interval"]] or 86400
    try:
        start_at = from_utc_text(start or dataset["first_start_utc"])
        end_at = from_utc_text(end or dataset["last_available_utc"])
    except (TypeError, ValueError):
        raise TradingError("invalid_replay_window", "Replay start and end are UTC times like 2026-01-15T14:30:00Z.") from None
    step = interval_seconds if step_seconds is None else step_seconds
    if not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= 31622400:
        raise TradingError("invalid_replay_window", "The replay step must be a whole number of seconds.")
    if end_at < start_at:
        raise TradingError("invalid_replay_window", "Replay end must not be before its start.")
    span = int((end_at - start_at).total_seconds())
    steps = -(-span // step) + 1                   # the final step is clamped to `end`
    if steps > max_steps:
        raise TradingError("replay_too_long", "This replay needs more steps than replay.max_steps allows.")
    return start_at, end_at, step, steps


def run_replay(dataset, *, config, created_at, start=None, end=None, step_seconds=None, consumers=None):
    replay_config = config["replay"]
    start_at, end_at, step, steps = plan(dataset, start, end, step_seconds, replay_config["max_steps"])
    names = list(consumers or CONSUMERS)
    if not names or len(names) > 10 or len(set(names)) != len(names) or any(n not in CONSUMERS for n in names):
        raise TradingError("unknown_consumer", "Replay consumers: bar_recorder, future_probe.")
    instances = [CONSUMERS[n]() for n in names]
    bars = tuple(_freeze(expand_bar(dataset, i)) for i in range(dataset["bar_count"]))
    window_size = replay_config["max_window_bars"]

    clock, visible, records = SimulationClock(start_at, step), 0, []
    delivered = quiet = max_visible = denied = 0
    for _ in range(steps):
        now = utc_text(min(clock.now, end_at))
        before = visible
        while visible < len(bars) and bars[visible]["available_at_utc"] <= now:
            visible += 1
        view = ReplayView(now, bars[max(0, visible - window_size):visible], visible)
        for consumer in instances:
            try:
                consumer.on_step(view)
            except TradingError as error:
                if error.code == "future_bar_leak":
                    raise
                raise TradingError("replay_consumer_failed", "A replay consumer failed.") from None
            except Exception:
                raise TradingError("replay_consumer_failed", "A replay consumer failed.") from None
        denied += view._denied
        delivered += visible - before
        quiet += visible == before
        max_visible = max(max_visible, visible)
        if len(records) < replay_config["max_report_steps"]:
            records.append({"sim_time_utc": now, "visible_bars": visible, "new_bars": visible - before,
                            "latest_start_utc": view.latest()["timestamp_utc"] if visible else None})
        clock.advance()

    simulation = {"start_utc": utc_text(start_at), "end_utc": utc_text(end_at), "step_seconds": step, "steps": steps,
                  "clock": "simulation"}
    finals = [{"name": c.name, "final": c.final()} for c in instances]
    summary = {"bars_delivered": delivered, "steps_without_new_bar": quiet, "future_access_attempts": denied,
               "max_visible_bars": max_visible}
    results = {"dataset_bars_sha256": dataset["bars_sha256"], "simulation": simulation, "consumers": finals,
               "summary": summary, "steps": records}
    replay_id = "rpl-" + sha256({"dataset_id": dataset["dataset_id"], "bars": dataset["bars_sha256"],
                                 "simulation": simulation, "consumers": names})[:24]
    return {
        "contract": "market_replay_report", "version": "1.0", "replay_id": replay_id, "mode": "historical_replay",
        "account_access": False, "authorization_possible": False, "dataset_id": dataset["dataset_id"],
        "dataset_bars_sha256": dataset["bars_sha256"], "symbol": dataset["symbol"], "interval": dataset["interval"],
        "data_label": dataset["data_label"], "simulation": simulation, "consumers": finals, "summary": summary,
        "steps": records, "steps_truncated": steps > len(records), "results_sha256": sha256(results),
        "created_at": created_at, "notice": NOTICE,
    }
