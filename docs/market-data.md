# Market data ingestion and offline replay (Step 25)

> **Not verified.** A successful import only means the file passed format and consistency
> checks. It does **not** show that the data is authentic, current, complete or licensed
> for any use. Every dataset says `authentic`, `current` and `licensed`: `not_verified`.
> There are no live feeds.

## Commands

```powershell
.\.venv\Scripts\python.exe -m vicekrack market-import --adapter synthetic --fixture synth1-5m
.\.venv\Scripts\python.exe -m vicekrack market-import --adapter synthetic --fixture synth1-1d-dst
.\.venv\Scripts\python.exe -m vicekrack market-import --adapter csv --file examples\trading\market\synthetic-synth2-5m.csv --symbol SYNTH2 --interval 5m --timezone America/New_York --label synthetic
.\.venv\Scripts\python.exe -m vicekrack market-list
.\.venv\Scripts\python.exe -m vicekrack market-inspect DATASET_ID --bars 5
.\.venv\Scripts\python.exe -m vicekrack market-replay DATASET_ID
.\.venv\Scripts\python.exe -m vicekrack market-replay DATASET_ID --start 2026-01-15T14:40:00Z --end 2026-01-15T15:10:00Z --step-seconds 600 --show-steps
.\.venv\Scripts\python.exe -m vicekrack market-list --replays
.\.venv\Scripts\python.exe -m vicekrack market-inspect REPLAY_ID
```

On Linux/macOS use `.venv/bin/python` and forward slashes. `market-list` prints the
`dataset_id` values (`mds-…`), and `market-replay` prints a `replay_id` (`rpl-…`).

## Your own CSV file

Your file is read once, read-only, and is never modified or copied. Only its file name
(sanitized), size, row count and SHA-256 are recorded, never its folder path.

```
timestamp,open,high,low,close,volume
2026-01-15T09:30:00-05:00,25.10,25.25,25.08,25.22,1000
2026-01-15T09:35:00-05:00,25.22,25.25,25.12,25.14,1037
```

- **Columns:** exactly `timestamp, open, high, low, close, volume` in any order, plus an
  optional `symbol` column that must match `--symbol`. The file is UTF-8 (a BOM is
  allowed), comma-separated, with no blank lines and no spaces around values.
- **Timestamps:** the bar **start**, in ISO 8601 with an offset (`-05:00`, `Z`). Times
  without an offset are rejected unless you pass `--naive-timezone America/New_York`.
  Then a local time that doesn't exist (for example `2026-03-08T02:30:00`) or happens
  twice (for example `2026-11-01T01:30:00`) at a daylight-saving change is rejected.
- **Prices:** plain decimals with up to 8 places, above 0. There is no rounding, so
  floats, exponents, thousands separators and leading zeros are rejected.
- **OHLC rules:** high ≥ open, close and low; low ≤ open and close. Volume is a decimal
  ≥ 0.
- **Order:** strictly increasing. A repeated start time is rejected as `duplicate_bar`,
  even when it's written with a different offset. Bars out of order give
  `bars_out_of_order`.
- **Alignment:** bars must align to `--interval` (`1m`, `5m`, `15m`, `30m`, `1h`, `1d`) in
  `--timezone`. Daily bars start at local midnight.
- **Label:** `--label historical|delayed|unknown|synthetic` records what you declare
  (default `unknown`). It is never verified. The synthetic-fixture adapter only allows
  `synthetic`.
- **Limits** (`config/market-data.json`): `max_file_bytes`, `max_rows`,
  `max_field_length`, `max_gap_entries`, and replay `max_steps`, `max_window_bars` and
  `max_report_steps`.

Errors name the line and column but never echo values or paths, for example
`{"error": {"code": "invalid_ohlc", "message": "Row 7: high must be >= open, close and low, ..."}}`.

## Gaps

Missing intervals between consecutive bars are listed in `gaps.entries`, with
`gap_count`, `missing_intervals` and `truncated`. Prices are **never** filled in or
invented. No market calendar is applied (`calendar: "none"`), so nights, weekends and
holidays show up as gaps. The `synth1-5m` fixture deliberately skips its 10:00 bar, and
`synth1-1d-dst` shows a weekend as a 2-day gap across the 2026-03-08 daylight-saving
change.

## Contracts (`schemas/trading/`)

- **`market_dataset` 1.0:**
  - identity: `dataset_id`;
  - market: `symbol`, `interval`, `timezone`, `currency` and `data_label`;
  - provenance: `source` (adapter, name, file name, SHA-256, bytes, rows) and
    `import_settings` (including the config SHA-256);
  - content: compact `bars` with `bars_sha256`;
  - `gaps` and `verification` (all `not_verified`).

  It is re-validated completely every time it is read.
- **`ohlcv_bar` 1.0:** a self-contained bar as handed to replay consumers. It has the
  symbol, interval, timezone, local `timestamp` with offset, `timestamp_utc`,
  `available_at_utc` (the bar close), exact decimal OHLCV, currency, source and
  `data_label`.
- **`market_replay_report` 1.0:** the simulation window, what each consumer finally saw,
  a summary, per-step visibility and `results_sha256`. It also has
  `account_access: false` and `authorization_possible: false`.

## Storage and duplicates

Files go in `runtime/trading/market/datasets/` and `runtime/trading/market/replays/`,
which git ignores. Each is written to a temporary file, fsynced and published with an
exclusive link, so a crash leaves nothing partial and nothing is overwritten. The dataset
ID comes from the adapter, source SHA-256, symbol and interval, so re-importing the same
bytes for the same symbol and interval always fails with `dataset_exists`, even with a
different label or a renamed copy. The same replay twice fails with `replay_exists`.

## Replay

`market-replay` runs a bounded loop over a **simulation clock**:
- **Window:** it starts at the first bar's start and ends at the last bar's close by
  default, stepping by one interval. Use `--start`, `--end` and `--step-seconds` to
  choose; the final step lands exactly on the end.
- **What consumers see:** at each simulated time, consumers get a read-only view holding
  only bars whose `available_at_utc` (close) has passed. That is at most the last
  `max_window_bars`. Future bars are not in the view at all.
- **Asking for the future:** `view.bar(n)` for a later bar raises `future_bar_access`.
  The `future_probe` consumer tries this every step and reports `refused` and `leaked: 0`.
- **No decisions:** the `bar_recorder` consumer only records what it saw. Nothing
  calculates indicators, makes signals or places orders.
- **Repeatable:** the same dataset and window always give the same `replay_id` and
  `results_sha256`.

### Separate from paper accounts

Historical replay can't bypass freshness checks or change an account:
- `vicekrack/trading/market/` never imports the account state, risk engine, order or
  journal modules.
- Paper authorization still accepts only `market_snapshot` documents whose source is a
  `synthetic_fixture`. Datasets can't be turned into snapshots in this step.
- Replay reports always say `account_access: false`.

## Limitations

- **Two adapters only:** synthetic fixtures and local CSV. There are no live, delayed or
  vendor feeds and no network access.
- **No calendars or corporate actions:** there are no market calendars, holidays,
  splits, dividends or currency conversion, and only one symbol per dataset.
- **Labels are not checked:** they are the importer's declaration and are never
  verified. Licensing and authenticity are the user's responsibility.
- **Observation only:** replay consumers only observe. There are no indicators, signals,
  fills or account updates.
