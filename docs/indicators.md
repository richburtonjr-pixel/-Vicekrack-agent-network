# Technical indicators on closed bars (Step 26)

> **Descriptive values only.** Indicators describe stored historical bars. They are not
> signals or advice. They place no orders and never touch paper accounts
> (`account_access: false`, `authorization_possible: false`). The data underneath is
> still **not verified** as authentic, current or licensed (see [Market data](market-data.md)).

## Commands

```powershell
.\.venv\Scripts\python.exe -m vicekrack market-import --adapter synthetic --fixture synth1-5m
.\.venv\Scripts\python.exe -m vicekrack market-list
.\.venv\Scripts\python.exe -m vicekrack indicator-calc DATASET_ID --ema 3 --rsi 3 --volume-sma 2 --vwap
.\.venv\Scripts\python.exe -m vicekrack indicator-calc DATASET_ID --ema 3 --rsi 3 --volume-sma 2 --vwap --save
.\.venv\Scripts\python.exe -m vicekrack indicator-calc DATASET_ID --ema 3 --gap-policy continue --points 12
.\.venv\Scripts\python.exe -m vicekrack indicator-calc DATASET_ID --vwap --vwap-timezone America/New_York --vwap-start 09:30 --vwap-end 16:00
.\.venv\Scripts\python.exe -m vicekrack indicator-list
.\.venv\Scripts\python.exe -m vicekrack indicator-inspect RESULT_ID --key rsi_3 --points 12
```

On Linux/macOS use `.venv/bin/python`. The flags work like this:
- `--ema`, `--rsi` and `--volume-sma` can be repeated, for example `--ema 12 --ema 26`.
- `--points` (0–50) limits how many of the latest points are printed for each indicator.
- `--save` writes the full result to `runtime/trading/indicators/<result_id>.json`.
- `--start`, `--end` and `--step-seconds` choose the replay window, as in `market-replay`.

## How values are calculated

All maths uses Python `Decimal` with **50 significant digits** and **ROUND_HALF_EVEN**.
Internal state is never rounded beyond that. Each published value is rounded **half-even
to 8 decimal places** and written as a plain decimal string, with trailing zeros removed
and no exponent.

| Indicator | Key | Periods | Formula | Ready at |
|---|---|---|---|---|
| EMA(n) | `ema_n` | 1–500 | Seed = mean of the first n closes. Then EMA = a·close + (1−a)·EMA_prev, with a = 2/(n+1) | bar n |
| Wilder RSI(n) | `rsi_n` | 2–500 | d = close − close_prev, gain = max(d,0), loss = max(−d,0). The first averages are means of n gains and n losses. Then avg = (avg_prev·(n−1) + x)/n. RSI = 100 − 100/(1 + avg_gain/avg_loss) | bar n+1 |
| Volume average(n) | `volume_sma_n` | 1–500 | mean of the last n volumes | bar n |
| Session VWAP | `vwap_session` | – | Σ(tp·volume)/Σ(volume) over the session's bars so far, with tp = (high+low+close)/3 | first in-session bar with volume > 0 |

Special cases are explicit and never replaced with 0:

| Case | Result |
|---|---|
| Not enough bars yet | `unavailable`, `warming_up` |
| RSI: losses average 0, gains average > 0 | `100`, `no_losses` |
| RSI: both averages are 0 (flat prices) | `unavailable`, `flat_prices` |
| RSI: gains average 0, losses > 0 | `0`, a real value |
| VWAP: cumulative session volume is 0 | `unavailable`, `zero_volume` |
| Volume average of zero volumes | `0`, a real average |

`(high + low + close) / 3` is a documented **approximation** of where a bar traded. Real
VWAP needs trade-level data, which ViceKrack doesn't have.

## Sessions (VWAP)

- A session is a clock window in an IANA timezone. The default comes from
  `config/indicators.json` (America/New_York, 09:30–16:00), or you can set
  `--vwap-timezone`, `--vwap-start` and `--vwap-end` (24-hour `HH:MM`).
- A bar is **in session** when its start is at or after `start`, its close is at or
  before `end`, and both fall on the same local date. Other bars are `outside_session`.
- Each new local date starts a new session. The first in-session bar carries
  `session_start`, and the sums restart.
- **No exchange calendar is used.** There are no holidays, half-days or early closes;
  the window is the same clock times every day.
- VWAP needs intraday bars. It is rejected for `1d` datasets.

## Gaps

A gap is a missing interval between consecutive bars, found with the same rule as Step 25
(`missing_between`). The `--gap-policy` setting decides what happens:

| Policy | EMA / RSI / volume average | VWAP |
|---|---|---|
| `reset` (default) | Warm-up restarts at the first bar after the gap (`bars_since_reset` goes back to 1) | `unavailable`, `session_gap`, for the rest of that session; the next session starts fresh |
| `continue` | Calculations continue as if the bars were consecutive. Every value from the gap onward carries `gap_ignored` | Continues. The rest of that session carries `gap_ignored` |

Because there's no calendar, the overnight break in intraday data **is** a gap. Under
`reset`, intraday EMA, RSI and volume averages therefore restart every day, and VWAP
resets at each session anyway.

## Closed bars only

`indicator-calc` runs a bounded Step 25 replay (`market.replay.drive`) with an indicator
consumer:
- At each simulated time it sees only the read-only view of bars whose close has passed,
  and it processes them in sequence order.
- Every point records `computed_at_sim_utc`, which is never before the bar's
  `available_at_utc`. The summary reports `future_access_attempts`.
- If one step exposes more new bars than `max_window_bars`, the run fails with
  `indicator_window_too_small`. It never skips bars; use a smaller `--step-seconds`.
- The same dataset, settings and window always give the same `result_id` and
  `results_sha256`. Values for a bar don't change when later bars are added (this is
  tested).

## Result contract (`indicator_result` 1.0)

- **Dataset provenance:** `dataset_id`, `bars_sha256`, `source_file_sha256`, `symbol`,
  `interval`, `timezone` and `data_label`.
- **Settings:** the indicators, `gap_policy`, `vwap_session` and `rounding`, plus
  `settings_sha256`.
- **Replay window:** `start_utc`, `end_utc`, `step_seconds` and `steps`, on the simulation
  clock.
- **Series:** one per indicator. Each point holds `sequence`, `timestamp` (local),
  `timestamp_utc`, `available_at_utc`, `computed_at_sim_utc`, `status` (`ready` or
  `unavailable`), `value`, `reason_codes` and `bars_since_reset`.
- **Summary:** bars processed, gaps detected, warm-up resets, sessions and future-access
  attempts.

Each calculation is isolated. Separate symbols, intervals, datasets and parameter sets
never share state, and the result ID includes the dataset, settings hash and replay
window. Saving the same result twice gives `result_exists`. Saved results are written
atomically to ignored storage and re-validated on read, so tampered values give
`result_corrupt`.

## Limitations

- **Four indicators only:** EMA, Wilder RSI, the volume average and session VWAP, on
  closes or volumes. There are no other indicators or price sources.
- **No calendar or adjustments:** no exchange calendar, holidays or corporate-action
  adjustments, and VWAP uses the typical-price approximation.
- **Offline replay only:** no live or streaming updates. Step 27's research signals read
  these values, but nothing feeds them into risk checks or paper accounts
  ([Research signals](research-signals.md)).
