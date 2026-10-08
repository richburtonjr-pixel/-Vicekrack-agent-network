# Historical market data from Alpaca (Step 40)

> **Opt-in, historical, one download.** `market-fetch` downloads completed historical
> bars for one US stock from Alpaca once, validates them with the Step 25 rules and
> stores them as an immutable dataset labelled `historical`.
> - There is no streaming, polling, scheduling, live quote, order, broker paper order or AI
>   call.
> - Research agents, the simulator, analytics and sessions never talk to Alpaca. They read
>   the stored dataset like any other.
>
> **Live access is unverified.** This step was built and tested only against mocked Alpaca
> responses, because no Alpaca credentials were available and no live request was
> authorized.

## What you need from Alpaca

- **An Alpaca account with Trading API keys.** Create them in Alpaca's dashboard (paper or
  live account keys). Market data uses the headers `APCA-API-KEY-ID` and
  `APCA-API-SECRET-KEY`.
- **The right subscription for the feed you ask for. Don't assume a feed is free or
  available.** As Alpaca documents it (October 2026):
  - The free **Basic** plan gives real-time data only from **IEX** (`--feed iex`). Its
    historical access to the full-market **SIP** feed (`--feed sip`) excludes the latest
    15 minutes.
  - **Algo Trader Plus** (paid) covers SIP without that restriction.
  - Stock history goes back to 2016.
  - Historical rate limits are 200 requests per minute on Basic and 10,000 on Algo Trader
    Plus.

  Plans, prices and entitlements can change, so check your own account. If the account
  is not entitled to a feed or range, Alpaca answers HTTP 403. ViceKrack then reports
  `provider_not_entitled`, saves nothing, and never tries another feed.
- Sources: Alpaca's [historical bars reference](https://docs.alpaca.markets/reference/stockbars),
  [single-symbol bars reference](https://docs.alpaca.markets/reference/stockbarsingle-1) and
  [market data overview](https://docs.alpaca.markets/docs/about-market-data-api).

## Credentials (environment variables only)

ViceKrack reads `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` from the process environment,
only when `market-fetch` runs with `--allow-network`.
- **Never stored:** they are never written to config, datasets, logs or output, and never
  put in a file or chat.
- **Optional template:** `.env.example` lists the names with blank values. ViceKrack does
  not load `.env` files.

PowerShell (the keys are typed hidden, used for this window only, then removed):

```powershell
$env:APCA_API_KEY_ID = [System.Net.NetworkCredential]::new('', (Read-Host 'Alpaca key ID' -AsSecureString)).Password
$env:APCA_API_SECRET_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'Alpaca secret key' -AsSecureString)).Password
.\.venv\Scripts\python.exe -m vicekrack market-fetch --provider alpaca --symbol AAPL --interval 5m --start 2026-09-14 --end 2026-09-18 --feed iex --adjustment raw --allow-network
Remove-Item Env:APCA_API_KEY_ID, Env:APCA_API_SECRET_KEY
```

Bash:

```bash
read -r -s -p 'Alpaca key ID: ' APCA_API_KEY_ID; export APCA_API_KEY_ID; echo
read -r -s -p 'Alpaca secret key: ' APCA_API_SECRET_KEY; export APCA_API_SECRET_KEY; echo
.venv/bin/python -m vicekrack market-fetch --provider alpaca --symbol AAPL --interval 5m --start 2026-09-14 --end 2026-09-18 --feed iex --adjustment raw --allow-network
unset APCA_API_KEY_ID APCA_API_SECRET_KEY
```

## Using the dataset

```powershell
.\.venv\Scripts\python.exe -m vicekrack market-list
.\.venv\Scripts\python.exe -m vicekrack market-inspect DATASET_ID --bars 5
.\.venv\Scripts\python.exe -m vicekrack trading-session-start DATASET_ID --record-events
.\.venv\Scripts\python.exe -m vicekrack trading-session-inspect SESSION_ID
.\.venv\Scripts\python.exe -m vicekrack hq-serve        # open Sessions (or press S)
```

The session needs no network and no credentials: it runs offline on the stored dataset,
with the same simulation and account boundaries as Step 39. The Living HQ Sessions view
shows a **Historical data source** panel with:
- the adapter, provider, feed and adjustment;
- the requested dates;
- the retrieval time (labelled wall clock, not market time);
- the coverage, uncovered time and gaps.

## `market-fetch` options

| Option | Meaning |
|---|---|
| `--provider alpaca` | The only provider. |
| `--symbol` | One US stock symbol in capitals (`AAPL`, `BRK.B`). |
| `--interval` | `1m`, `5m`, `15m`, `30m`, `1h` or `1d` (Alpaca `1Min` … `1Day`). |
| `--start`, `--end` | New York dates, inclusive. `--end` must be **before today** (New York), so only completed days are fetched. The request covers New York midnight on `--start` to just before New York midnight after `--end`. |
| `--feed` | Required, no default: `iex` or `sip` (see entitlements above). |
| `--adjustment` | Required, no default: `raw`, `split`, `dividend` or `all` (Alpaca's corporate-action adjustment). |
| `--allow-network` | Required. Without it nothing is requested and no credential is read. |

Bounds, from `config/market-providers.json`:
- **Range length per interval:** 7 days for `1m`, 31 for `5m`, 62 for `15m`, 92 for
  `30m`, 183 for `1h` and 3660 for `1d`.
- **Per request:** at most 20 pages of up to 10,000 bars, 8 MB per response and 40 MB in
  total, at most 100,000 bars, and a 20-second timeout.
- **One HTTPS endpoint.** It is fixed: `https://data.alpaca.markets/v2/stocks/{symbol}/bars`,
  with certificate verification and no redirects.

## What is recorded (`market_dataset` 1.0, `source.provider`)

| Field | Meaning |
|---|---|
| `name`, `endpoint`, `feed`, `adjustment`, `timeframe` | The provider, the fixed endpoint and the request choices. |
| `requested` | The New York start and end dates, and the exact UTC bounds (`start_utc` inclusive, `end_before_utc` exclusive). |
| `coverage` | The first and last bar, the last bar's close, the bar count, and the time not covered before the first bar or after the last bar's close. `calendar: none`. |
| `retrieved_at`, `retrieval_meaning` | When this computer finished the download. This is **wall-clock time, not market time and not a quote time.** |
| `request_sha256`, `responses[]` | A hash of the request parameters (never credentials), plus the page number, SHA-256, size and bar count of every raw response. |
| `credentials_recorded` | Always `false`. |

The dataset's `file_sha256` is a hash of the request hash and all response hashes, so its
ID (`mds-…`) changes with the feed, the adjustment, the range or the data.
- **Same request, same data:** fetching it again is refused (`dataset_exists`); datasets
  are immutable.
- **Re-checked on every load:** all of this provenance is re-checked, together with the
  bars, gaps and hashes. A dataset whose provenance was edited is reported as
  `dataset_corrupt`.

## Time and availability

- **Bar times.** Each bar's `t` is its **start** (Alpaca confirmed this; daily bars start
  at New York midnight). Bars are converted to `America/New_York` and validated with the
  Step 25 rules: alignment, strict order, no duplicates, and OHLC consistency.
- **Availability.** A bar becomes available at its close (`available_at_utc`), exactly as
  for other datasets, so replay, research and simulation see a bar only after it closed.
  Any bar not closed at retrieval time is rejected (`unfinished_bar`).
- **Extended hours.** Alpaca intraday bars can include pre-market and after-hours trading.
- **No market calendar.** Nights, weekends and holidays are reported as gaps, and nothing
  is filled in.

## Prices and adjustments

- **Exact decimals.** Prices come from the response as exact decimals, never through binary
  floating point. A value with more than 8 decimal places is rejected
  (`provider_precision_exceeded`), never rounded.
- **One adjustment per dataset.** A dataset is either `raw` or one adjustment setting, and
  its provenance says which. Comparing sessions on different datasets is up to you;
  ViceKrack never combines datasets.

## Failures (nothing is saved in any of these cases)

| Code | When |
|---|---|
| `network_not_allowed` | `--allow-network` was not given. |
| `provider_credentials_missing` / `_invalid` | The environment variables are missing, or contain spaces or control characters. |
| `provider_auth_failed` | HTTP 401. |
| `provider_not_entitled` | HTTP 403. Another feed is never tried. |
| `provider_rate_limited` | HTTP 429. The `X-RateLimit-Reset` time is shown if valid; it is never retried. |
| `provider_bad_request` | HTTP 400, 404 or 422. |
| `provider_unavailable` / `provider_unexpected_status` | HTTP 5xx, or any other status including redirects. |
| `provider_timeout` / `provider_unreachable` | No answer within the timeout, or no connection. |
| `provider_malformed_response` | Invalid JSON, the wrong symbol or shape, bad fields, sub-second times or a malformed page token. |
| `provider_response_too_large`, `provider_too_many_bars`, `provider_page_limit`, `provider_pagination_loop` | A bound was exceeded; the download is incomplete. |
| `provider_bar_outside_request`, `unfinished_bar`, `duplicate_bar`, `bars_out_of_order`, `interval_misaligned`, `invalid_ohlc` | Bars that fail the rules. |
| `provider_no_bars` | No bars were returned for this symbol, range and feed. |

- **Fixed messages.** Error messages are fixed text. They never include response bodies,
  headers, URLs with credentials, or credential values.
- **Atomic.** All pages are collected and validated before anything is written. An
  interrupted download leaves no file.

## Limitations

- **Live access is unverified.** No real Alpaca request has been made. Run the first real
  download yourself, with your own keys, when you choose to.
- **One provider, US stocks only:** one symbol per dataset, no crypto or options, and the
  `boats` and `otc` feeds are not offered.
- **Symbol mapping:** this uses Alpaca's default (no `asof` parameter is sent).
- **No market calendar:** there is no corporate-action history beyond Alpaca's own
  `adjustment` setting.
- **Unchecked content:** the data is still `not_verified` for authenticity, completeness
  and licensing. Licensing and redistribution terms are yours to check.
