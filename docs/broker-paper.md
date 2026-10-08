# Alpaca paper broker (Step 41)

> **PAPER ONLY. No real money.** This adapter talks to an Alpaca **paper** trading
> account, which is Alpaca's own simulated brokerage.
> - **Only the paper endpoint.** It uses only `https://paper-api.alpaca.markets` (plus
>   `https://data.alpaca.markets` for one fresh quote). The real-money host is refused
>   before any request is built.
> - **A person drives every order.** Each order comes from an explicit command, and is sent
>   once, only after typed consent. Nothing is automatic: research signals, datasets,
>   sessions and simulations never become orders.
> - **Nothing runs in the background:** no polling, streaming or retries.
>
> **Live paper access is unverified.** Everything was built and tested against a mocked
> Alpaca, because no paper credentials were available and no live request was authorized.

## Three separate things

| | What it is | Where it lives |
|---|---|---|
| Offline simulator (Step 29) | ViceKrack's own replay of historical bars | `runtime/trading/simulation/` |
| Local paper account (Step 24) | ViceKrack's own risk bookkeeping; never sends anything | `runtime/trading/accounts/` |
| **Alpaca paper account (Step 41)** | A paper account at Alpaca; orders really go to Alpaca's paper system | `runtime/trading/broker-paper/` |

They share no code paths, storage or IDs: intents are `bpi-…` and client order IDs are
`vk-…`. A test checks that no other trading package imports the broker module. The Living
HQ shows paper-broker records only in their own **Paper broker** view, never in the house
or the simulation results desk.

## What you need

- **Keys:** an Alpaca account with a paper trading account, and that paper account's API
  keys. Paper and live accounts have different keys.
- **Market data:** paper-only accounts get IEX market data, which is used for the
  freshness check (`data_feed: iex` in the config).
- **Credentials:** set them in the environment only, under names deliberately different
  from the Step 40 data keys:

PowerShell (hidden input, removed afterwards):

```powershell
$env:ALPACA_PAPER_API_KEY_ID = [System.Net.NetworkCredential]::new('', (Read-Host 'Alpaca PAPER key ID' -AsSecureString)).Password
$env:ALPACA_PAPER_API_SECRET_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'Alpaca PAPER secret key' -AsSecureString)).Password
# ... run the commands below ...
Remove-Item Env:ALPACA_PAPER_API_KEY_ID, Env:ALPACA_PAPER_API_SECRET_KEY
```

Bash: `read -r -s -p 'Alpaca PAPER key ID: ' ALPACA_PAPER_API_KEY_ID; export ALPACA_PAPER_API_KEY_ID`.
Do the same for `ALPACA_PAPER_API_SECRET_KEY`, then `unset` both when done.

If `APCA_API_BASE_URL` or `ALPACA_PAPER_BASE_URL` is set to anything except the paper URL,
every command refuses (`endpoint_override_rejected`). The configuration has no endpoint
setting at all.

## Commands

```powershell
# 1. Connect and check the paper account (sanitized; no balances are saved)
.\.venv\Scripts\python.exe -m vicekrack broker-paper-check --allow-network

# 2. Prepare one order: prints the concrete proposal and saves an immutable intent (sends no order)
.\.venv\Scripts\python.exe -m vicekrack broker-paper-prepare --symbol AAPL --side buy --qty 1 --limit-price 190.10 --allow-network

# 3. Submit exactly that intent: the consent phrase must name its ID
.\.venv\Scripts\python.exe -m vicekrack broker-paper-submit INTENT_ID --consent paper-execute:INTENT_ID --allow-network

# 4. Refresh / reconcile its status (by client order ID); repeat whenever you want an update
.\.venv\Scripts\python.exe -m vicekrack broker-paper-status INTENT_ID --allow-network

# 5. Request cancellation (a request, not a confirmed cancellation); then refresh the status
.\.venv\Scripts\python.exe -m vicekrack broker-paper-cancel INTENT_ID --consent paper-cancel:INTENT_ID --allow-network

# Offline views and the kill switch (no network)
.\.venv\Scripts\python.exe -m vicekrack broker-paper-list
.\.venv\Scripts\python.exe -m vicekrack broker-paper-inspect INTENT_ID
.\.venv\Scripts\python.exe -m vicekrack broker-paper-kill-switch status
.\.venv\Scripts\python.exe -m vicekrack broker-paper-kill-switch engage
.\.venv\Scripts\python.exe -m vicekrack broker-paper-kill-switch release

# View (read-only): open the printed address, then "Paper broker" (or press B)
.\.venv\Scripts\python.exe -m vicekrack hq-serve
```

## Supported orders

- **Order type:** limit orders only, in **whole shares**, with `time_in_force: day` and
  `extended_hours: false`. They are sent only while Alpaca's clock says the regular
  session is open, and not within `min_seconds_before_close` of the close.
- **Long only.** `buy` opens or adds to a long position. `sell` only reduces an existing
  long position, never beyond the shares available. Short positions block buys.
- **Eligible stocks:** active, tradable `us_equity` assets, not on OTC.
- **Limit prices:** greater than 0 and at least `min_limit_price`, with at most 2 decimals
  at $1.00 or more (4 below $1.00, as Alpaca requires). No exponents or floats.
- **Quantity:** at most `max_quantity`, and quantity × limit price at most
  `max_order_notional`.

### How the limit bounds exposure

A buy limit order never pays more than its limit price per share, so its worst-case cost is
**quantity × limit price** (`max_notional` in the intent). That figure is checked:
- against `max_order_notional`;
- against the account's buying power;
- against `max_position_notional`, together with the position's market value and every open
  buy order for the symbol (remaining quantity × its own limit price).

Alpaca also reduces buying power by open buy orders until they fill or are canceled. So
pending orders reserve buying power on the broker's side too.

## Checks immediately before submission (all must pass)

The submission records every check; any failure blocks it, and nothing is sent.

| Check | Blocks when |
|---|---|
| `kill_switch_released` | The broker-paper kill switch is engaged (config or `broker-paper-kill-switch engage`). It is checked before any network call. |
| `intent_not_expired` | The intent is older than `intent_ttl_seconds`. |
| `local_state_reconciled` | Any earlier intent has an unknown outcome or a mismatch with the broker. |
| `daily_submission_limit` | `max_submissions_per_day` (New York date) has been reached. |
| `same_paper_account` | The account fingerprint differs from the one at preparation. |
| `account_paper_active` | The account is not `ACTIVE`, not USD, or trading, account or user-suspended is blocked. |
| `market_regular_session_open`, `not_too_close_to_close` | The market is closed, or the close is too near (Alpaca clock). |
| `clock_in_sync` | This computer's clock differs from Alpaca's by more than `max_clock_skew_seconds`. |
| `asset_eligible` | The asset is missing, inactive, not tradable, not a US equity, or on OTC. |
| `quote_fresh` | The latest quote is missing, has no bid or ask, or is older than `max_quote_age_seconds`. |
| `spread_within_limit` | The bid/ask spread is over `max_spread_bps`. |
| `limit_within_band` | A buy's limit is above ask + `max_limit_above_ask_bps` or below bid − `max_limit_below_bid_bps`; mirrored for sells. |
| `long_only`, `sell_reduces_long_only` | A short position exists, or a sell would exceed the long shares available. |
| `open_orders_within_limit` | `max_open_orders` are already open. |
| `position_within_limit`, `buying_power_sufficient` | See *How the limit bounds exposure*. |
| `broker_orders_known_locally` | Alpaca has an open `vk-…` order this computer doesn't know about. |
| `local_open_orders_current` | A locally open order is no longer open at Alpaca; refresh it first. |

Missing or stale inputs block; they are never assumed.

## Uncertainty, at-most-once and reconciliation

1. **Before the request.** The client order ID (`vk-…`) is fixed at preparation, and
   `submission.json` is written exclusively **before** the order request. An intent can be
   submitted at most once (`already_submitted`), and the OS lock refuses concurrent
   commands (`broker_busy`).
2. **Unknown outcomes.** A timeout, dropped connection, 5xx or unreadable answer is
   recorded as `unknown`, and is never resent. A crash after the attempt was recorded has
   the same result: the attempt exists and has no answer.
3. **Resolving them.** `broker-paper-status` looks the order up **by client order ID**.
   - If found, the order is reconciled with the broker's data. A different quantity,
     price, side or type is a `mismatch`, which blocks.
   - If not found, the state stays `unknown` until `not_found_settle_seconds` have passed
     since the attempt. After that it becomes `not_placed`. You then prepare a new intent;
     the old one is never resent.
4. **No new exposure meanwhile.** New submissions stay blocked while anything is unknown,
   mismatched, or out of date with the broker.

This gives **at-most-once submission per intent with explicit reconciliation**. It is not,
and does not claim to be, exactly-once execution.

## States (from broker-confirmed data only)

| State | From the broker |
|---|---|
| `accepted` | `new`, `accepted`, `pending_new`, `accepted_for_bidding` |
| `partially_filled`, `filled`, `canceled`, `expired`, `rejected` | Same names |
| `cancel_pending` | `pending_cancel` |
| `done_for_day` | `done_for_day` |
| `other_broker_state` | Anything else (for example `held` or `suspended`); this blocks new exposure |

Local-only states:
- `prepared` / `prepared_expired`: not submitted yet.
- `unknown`: the attempt began but has no confirmed answer.
- `mismatch`: the broker's order differs from the intent.
- `not_placed`: confirmed absent after settling.
- `cancel_requested`: a cancel request was accepted, but no later status shows the result
  yet.

`filled`, `canceled`, `expired`, `rejected` and `not_placed` are final. Filled quantity and
average price come only from the broker.

## Cancellation

`broker-paper-cancel` needs the consent `paper-cancel:INTENT_ID` and an order the broker has
confirmed. Alpaca's 204 answer only means the cancel **request** was accepted, so the state
becomes `cancel_requested`, never `canceled`.
- **Fills can race the cancel.** A later status refresh may show `filled`, or `canceled`
  with a partial fill.
- **Other answers:** `422` means not cancelable (usually already final); a timeout is
  recorded as `unknown`.
- **Kill switch:** status refreshes and cancel requests are allowed while it is engaged.

## What is stored (and what is not)

- **Stored:**
  - the intent (the proposal, a quote snapshot, preview checks and a config hash);
  - the submission (the checks at submission) and the outcome (state, HTTP status, Alpaca's
    numeric error code);
  - broker-confirmed order observations (the order's sanitized fields);
  - cancel requests;
  - sanitized events.

  Every file is atomic and never overwritten, and is re-validated on every read.
- **Never stored:**
  - keys or headers;
  - raw responses or error messages;
  - the Alpaca account ID or number (only a one-way fingerprint `pacct-…`);
  - cash, buying power or equity. These are used in memory for checks only.

## Limitations

- **Live paper access is unverified.** No real request, paper or otherwise, was made. Run
  your first `broker-paper-check` yourself when you choose to.
- **One paper account and one order shape:** whole-share day limit orders, regular session
  only.
- **No more order types:** no market, stop, bracket, fractional, extended-hours, GTC or
  short orders, no order replacement, and no options or crypto.
- **Freshness relies on the IEX quote** and Alpaca's clock. Paper fills are Alpaca's
  simulation (for example, random partial fills), not real execution.
- **Status updates only when you ask.** It changes only when you run `broker-paper-status`;
  nothing polls or streams, and the HQ shows the last refreshed state.
- **Orders placed elsewhere:** open orders placed outside ViceKrack count towards the
  limits, but are not tracked as intents.
