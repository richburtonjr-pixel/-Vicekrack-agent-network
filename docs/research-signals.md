# Rule-based research signals (Step 27)

> **Research only.** A research signal records that a fixed rule matched on closed
> historical bars. It is not advice, a prediction or a profitability claim. It is not an
> order and can't become one: every record has `purpose: research_only`,
> `authorization_possible: false` and `account_access: false`, and no order proposal.
> Research signals are a different contract (`research_signal`, IDs `rsig-…`) from the
> order-type `trading_signal` (IDs `sig-…`) that paper authorization accepts. Nothing here
> reads or changes paper accounts, reserves exposure or creates intents.

## Commands

```powershell
.\.venv\Scripts\python.exe -m vicekrack market-import --adapter synthetic --fixture synth1-5m
.\.venv\Scripts\python.exe -m vicekrack market-list
.\.venv\Scripts\python.exe -m vicekrack signal-list --strategies
.\.venv\Scripts\python.exe -m vicekrack signal-run DATASET_ID --strategy vwap-reclaim --strategy ema-cross-3-5 --strategy breakout-3
.\.venv\Scripts\python.exe -m vicekrack signal-run DATASET_ID --strategy vwap-reclaim --strategy ema-cross-3-5 --strategy breakout-3 --save
.\.venv\Scripts\python.exe -m vicekrack signal-list
.\.venv\Scripts\python.exe -m vicekrack signal-list --signals
.\.venv\Scripts\python.exe -m vicekrack signal-inspect RUN_ID --strategy breakout-3 --outcome not_ready --entries 20
.\.venv\Scripts\python.exe -m vicekrack signal-inspect SIGNAL_ID
```

On Linux/macOS use `.venv/bin/python`. Strategies are chosen by name from
`config/research-signals.json`. `--entries` (0–50) limits the printed evaluations, and
`--start`, `--end` and `--step-seconds` set the replay window. With `synth1-5m`,
`vwap-reclaim` triggers on bar 3 and `breakout-3` on bar 11. Everything after the missing
10:00 bar is `not_ready` until the inputs warm up again.

## How a run works

`signal-run` is one bounded Step 25 replay on a simulation clock.
1. At each simulated time, the Step 26 indicator consumer processes the newly closed bars,
   always with gap policy `reset`.
2. Each strategy is then evaluated on each new bar, in order. It uses only that bar,
   earlier bars and indicator values already published for them.
3. Each evaluation is exactly one of:
   - **`triggered`:** the rule's transition happened on this bar and no cooldown was
     active. One research signal is created.
   - **`not_triggered`:** inputs were ready but the transition didn't happen. Reasons
     include `no_cross`, `already_above`, `at_or_below_vwap`, `already_above_vwap`,
     `below_or_at_level`, `already_above_level`, `volume_filter_failed` and
     `cooldown_active`.
   - **`not_ready`:** inputs were missing, unavailable or invalidated by a gap. Reasons
     include `insufficient_history`, `gap_invalidated`, `input_unavailable` (plus the
     indicator's own reason, for example `indicator_warming_up` or
     `indicator_session_gap`), `outside_session`, `no_prior_bar_in_session`,
     `volume_average_unavailable` and `volume_average_zero`. **A not-ready evaluation
     never produces a signal.**

Each evaluation stores the bar, `computed_at_sim_utc`, the outcome, reason codes and the
values it compared, so a future read-only Command Center can show why a strategy did or
didn't act.

## Exact rules

Comparisons use the bar's exact decimal prices and the **published** Step 26 indicator
values (rounded half-even to 8 places), so every decision can be re-checked from the
record. Bar t is the current bar and t−1 the previous one. Every rule needs t−1 and t to
be consecutive, with no missing interval between them.

| Strategy | Trigger | Comparisons | Needs |
|---|---|---|---|
| `vwap_reclaim` | close(t−1) **≤** VWAP(t−1) **and** close(t) **>** VWAP(t) | at/below before is inclusive; above now is strict | both bars in the same session and inside the session window, VWAP ready on both |
| `ema_crossover` | fast(t−1) **≤** slow(t−1) **and** fast(t) **>** slow(t) | inclusive before, strict now | `fast < slow` periods, both EMAs ready on both bars |
| `breakout` | close(t) **>** level(t) **and not** close(t−1) **>** level(t−1), where level(t) = max(high(t−N) … high(t−1)) | strict | N + 2 consecutive bars |
| breakout volume filter (optional) | volume(t) **≥** multiplier × volume_sma(M) at t−1 | inclusive | the average is ready and > 0 (it never includes bar t) |

**Warm-up:**
- EMA crossover needs the slow EMA ready on the previous bar, which takes slow + 1 bars.
- VWAP reclaim needs the second in-session bar of a session.
- Breakout needs N + 2 consecutive bars, plus M + 1 bars for the volume filter.

Until then the result is `not_ready`.

**Sessions:**
- VWAP reclaim uses the strategy's session (timezone, start and end; there is **no
  exchange calendar**). The first bar of a session is always
  `not_ready / no_prior_bar_in_session`, so a reclaim can't span two sessions.
- Bars outside the window are `outside_session`.
- All VWAP strategies in one run must share the same session. VWAP strategies are
  rejected for daily datasets.
- EMA crossover and breakout ignore sessions, but the overnight break in intraday data is
  a gap.

**Gaps:**
- A gap resets the consecutive-bar count. Any rule whose required window includes the gap
  is `not_ready / gap_invalidated` until enough new consecutive bars have closed.
- The indicators also reset their warm-up (Step 26 `reset` policy), and VWAP becomes
  unavailable for the rest of that session.
- A price pattern that only exists across a gap never triggers.

**Cooldown (`cooldown_bars`):** after a signal on bar t, a transition on any of the next
`cooldown_bars` processed closed bars is `not_triggered / cooldown_active`. Cooldown counts
from the last emitted signal, not from suppressed ones.

**Expiry (`expiry_bars`):**
- `expires_at_utc` is the triggering bar's close, advanced by `expiry_bars` intervals
  (for `1d`, by local midnights).
- If a coarse replay step first sees the bar after that time, the record is kept with
  `expired_when_detected: true`.

## Records and identity

**`research_signal` 1.0** contains:
- `signal_id`;
- the strategy's name, type, version 1.0, params and `config_sha256`;
- dataset provenance (dataset ID, bars hash, source file hash, symbol, interval, timezone,
  label);
- the bar's sequence and local, UTC and close timestamps;
- the `event`;
- `detected_at_sim_utc`, `expires_at_utc` and `expired_when_detected`;
- the `supporting_values` compared, and the `reason_codes`;
- `content_sha256`, which covers the whole record.

**Deterministic ID:** `rsig-` + SHA-256 of the strategy name, its configuration hash, the
dataset ID, the bars hash and the bar's sequence and time. The same strategy,
configuration, dataset and bar always give the same ID. A different name, any parameter
change, or a different dataset gives a different ID.

**`research_signal_run` 1.0** contains:
- the run ID, which comes from the dataset, the strategies and the replay window;
- dataset provenance and the strategies with their hashes;
- the indicator settings hash and the replay window;
- the bounded evaluations per strategy;
- the signals;
- a summary (`bars_evaluated`, `triggered`, `not_triggered`, `not_ready`,
  `cooldown_suppressed`, `future_access_attempts`) and `results_sha256`.

## Storage and duplicates

Everything is stored in ignored `runtime/trading/signals/`.

- **Record files:** `records/<signal_id>.json`, one per signal, written once with an
  exclusive link. A later run that finds the same signal keeps the stored record
  (first detection wins) and reports it under `already_recorded`. Any other difference is
  `signal_conflict`.
- **Run files:** `runs/<run_id>.json`. Saving the same run again gives `run_exists`.
- **Order:** records are written before the run file. After a crash in between, re-saving
  reports those records as already recorded.
- **Checks on read:** every file is re-validated. Edited records give `signal_corrupt`;
  edited runs give `signal_run_corrupt`; inconsistent provenance gives
  `invalid_signal_run`. A tampered input dataset is rejected before evaluation
  (`dataset_corrupt`).

## Limitations

- **Three long-only rules:** no short or exit rules, and no combinations of rules.
- **Fixed sources:** comparisons use published 8-place indicator values; sources and
  formulas are fixed.
- **No calendar:** no exchange calendar or holidays, so overnight breaks are gaps.
- **Offline only:** a research signal is never connected to risk checks, paper accounts or
  intents. Turning research signals into paper order intents would need its own design
  step.
- **No performance claims:** there is no backtest, P&L or performance statistic.
- **Step 28** reviews these signals in a deterministic research-agent workflow
  ([Research agents](research-agents.md)), which is still research only.
