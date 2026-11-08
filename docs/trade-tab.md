# Trade tab and whole-market radar (Trading PR 4)

> **Practice money only.** No broker, no real orders. The $80 shutdown covers your trades and the agents'.

Think of it as an exchange app (like Kraken's) for pretend money, with an eight-person trading desk working beside you.

## On your phone (and the PC browser): Mobile Command Center -> Trade

| Screen | What you see |
|---|---|
| Trade | Portfolio value, profit/loss since $100, cash, the $80 fuse bar, your coins (marked You or Agents), every Kraken USD coin with search and All / Gainers / Losers / Agents' radar filters |
| Coin page | Live price, today's change, chart (1H 1D 1W 1M 1Y, drag to read prices), buy (ask) and sell (bid) prices, 24h high/low/volume, your holding and P&L, what the agents saw, Buy and Sell |
| Buy / Sell | Dollar amount with $5 / $10 / $25 / Max (buy) or 25% / 50% / All (sell). Priced on the PC from a fresh Kraken request when you confirm |
| Agent desk | The radar (coins scanned, streaks found), scoreboard (you vs agents), every case file with each agent's signature |
| Activity | Every buy, sell and shutdown, and who did it |

Your trades: any coin, up to all your cash, no borrowing, and you can buy more of your own coins or sell part of them. The agents never sell your coins. If the agents hold a coin, you can sell theirs but not add to it.

## The agents now watch the whole market, every minute

1. **Radar:** one Kraken request prices every USD coin. The Market Scout keeps 30 minutes of history and flags coins on a steady climb: up 0.3%+ in 5 minutes and 0.6%+ in 15, rising in at least 3 of the last 5 minutes, with at least $250K of daily volume. Stablecoins, currencies and coins too cheap to price accurately are skipped. The first 15 minutes after starting are a warm-up.
2. **Workup:** the top 8 streaks get 1-minute bars and the full chain: Trend, Strategy, Verifier, Risk Review. The agents buy $25 at the ask price, hold at most 2 coins, and make at most 10 buys a day.
3. **Fuse:** every coin you or the agents hold is re-priced every minute.

Settings: `config/watch.json` (radar), `config/trading-desk.json` (agent limits), `config/practice-account.json` (account).

## Honest expectations

A round trip costs about 0.7% (0.25% fee each way plus slippage), so a streak has to keep going past that to make money. That's what practice is for.
