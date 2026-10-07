# ViceKrack roadmap

ViceKrack has three tracks. Each step was built on its own branch, reviewed as a pull
request and merged into `main`. The content track uses GTA VI as its first profile, but
its contracts stay subject-neutral. See [Subsystem boundaries](subsystems.md).

| Track | Purpose | Code |
|---|---|---|
| **Shared infrastructure** | Orchestration, providers, persistence, checks and safety rules used by both subsystems | `vicekrack/` core modules, `scripts/`, CI |
| **Content** | Research → verify → select → write → plan → preview → produce → quality-check short vertical videos | `vicekrack/` content modules |
| **Trading** | Paper-only risk foundation: contracts, limits, persistent paper state, journal, offline market data, replay, descriptive indicators and research-only signals, a deterministic research-agent workflow and an offline execution simulator | `vicekrack/trading/` |

## Completed steps

| Step | Track | What it added | PR |
|---|---|---|---|
| 1 | Shared | Project foundation, task contract, local CLI | #1 |
| 2 | Shared | Local orchestrator with a mock Researcher | #2 |
| 3 | Shared | Configurable OpenAI research provider | #3 |
| 4 | Shared | Configurable Anthropic (Claude) provider | #4 |
| 5 | Shared | Bounded Researcher → Analyst → Reviewer workflow | #5 |
| 6 | Shared | Atomic local workflow persistence and explicit resume | #6 |
| 7 | Shared | Agent Manager and bounded recovery | #7 |
| 8 | Shared | Local CLI dashboard of saved runs | #8 |
| 9 | Shared | Automatic repository checks (CI, network-blocked tests, credential audit) | #9 |
| 10 | Shared | Prepare your own workflow task | #10 |
| 11 | Content | Subject-neutral 15-second Short Script contract | #11 |
| 12 | Content | Deterministic offline scene planning | #12 |
| 13 | Content | Bounded local silent 9:16 video previews | #13 |
| 14 | Content | Optional local narration for previews | #14 |
| 15 | Content | Creator: Story Brief → Short Script drafts | #15 |
| 16 | Content | Scout: bounded research intake from approved sources | #16 |
| 17 | Content | Verification layer between Scout and Story Brief | #17 |
| 18 | Content | Story Selection and editorial ranking | #18 |
| 19 | Content | Dated official statements supersede older ones | #19 |
| 20 | Content | Bounded article fetching as opt-in verification evidence | #20 |
| 21 | Content | Controlled production pipeline with checkpointed resume | #21 |
| 22 | Content | Production quality report (pass / needs_review / fail) | #22 |
| 23 | Trading | Paper-only trading foundation: contracts, exact decimals, config limits, kill switch, risk engine, journal, synthetic demo | #23 |
| 24 | Trading | Persistent paper risk state: processed signals, authorized intents, daily counters, pending exposure reservations, one locked operation, crash recovery, cancellation, roadmap | #24 |
| 25 | Trading | Offline market data: provider-neutral adapters (synthetic fixtures, local CSV), `ohlcv_bar`/`market_dataset` contracts, provenance, explicit gaps, bounded simulation-clock replay without future bars, kept separate from paper accounts | #25 |
| 26 | Trading | Deterministic offline indicators on closed replay bars: EMA, Wilder RSI, volume average, session VWAP; exact decimals, explicit warm-up/flat/zero-volume/gap handling, versioned results with provenance; no signals or account access | #26 |
| 27 | Trading | Rule-based research signals (VWAP reclaim, EMA crossover, breakout with optional volume filter) on closed bars and indicators: exact comparisons, not-ready handling, cooldown, deterministic IDs, bounded evaluations; research only, `authorization_possible: false` | #27 |
| 28 | Trading | Bounded deterministic research-agent workflow: Market Scout, Trend Agent, Strategy Agent and Risk Review stages with validated handoffs over evidence frozen at a simulated time; fixed four-stage controller, explicit failure states, interface for a future AI analysis layer; research only | #28 |
| 29 | Trading | Bounded offline paper-execution simulation: explicit simulation policy, isolated in-run account, next-open fills with documented slippage/fees, risk limits and kill switch, opposite-EMA and max-holding exits, exact cash/position/P&L accounting, versioned simulated contracts | #29 |
| 30 | Trading | Read-only analytics of saved simulations: equity rebuilt by replay from validated fills, closed-trade statistics, P&L and net return (never annualized), drawdown, holding, exposure, order outcomes, strategy attribution with shared-account explanation; `unavailable` metrics with reasons, tamper-checked inputs | #30 |
| 31 | Shared | Structured execution events (`execution_event` 1.0) and bounded timelines: optional recording in the research-agent workflow and simulator (stage started/completed/blocked/failed, order decisions, simulated fills), recorded vs reconstructed timelines with no invented times, read-only adapters for saved trading runs and content productions, display-state transition rules, atomic per-department storage with duplicate/concurrency/retention rules, explicit failure on persistence errors, list/inspect/replay CLI | #31 |
| 32 | Shared | ViceKrack Living HQ: local read-only visual Command Center over Step 31 timelines; two-floor isometric house (trading upstairs, content downstairs, shared operations lobby/lounge/kitchen) with distinct bots; status-first replay (play/pause/scrub/speed), recorded vs reconstructed vs live vs demo labelling, honest partial/interrupted display, supported-only handoffs, decorative idle behaviour; inspector, Timeline/Text views, presentation mode, reduced motion and keyboard access; loopback-only GET API with Host/Origin checks and strict CSP; deterministic demo | this PR |

## Deferred work

### Content: human review and export package (deferred)

This was first proposed as Step 23 and set aside when trading took that slot. It is not
built. A future content step would:
- collect a finished production, its quality report, its verification record and its
  sources into one reviewable package;
- record an explicit human decision (approve / request changes / reject), with reviewer
  notes;
- export approved packages for manual upload. There would be **no automatic
  publishing**: previews stay `publishable: false` until a human approves them, and
  approval is still not a rights clearance.

Other content work that is deferred and not scheduled:
- Voice generation beyond Step 14's optional local narration file, and music or sound
  effects.
- Sourced or generated media for scenes, beyond the local silent previews. This needs
  rights tracking per asset.
- A Publisher (platform upload) and an Analyst (content performance metrics; Step 30's
  trading analytics is unrelated). Both need explicit
  consent, credentials handled through environment variables, and the human review step
  above first.
- More content profiles beyond GTA VI, using the same subject-neutral contracts.

The content track has had no new steps since Step 22. Steps 23–30 built the trading
foundation, and Steps 31–32 added shared execution events and the read-only Living HQ,
which only reads saved content productions. Nothing in these steps changed content
behaviour; all content tests still pass.

### Trading: not started, and out of scope until explicitly requested

- Live, delayed or vendor market-data feeds. Step 25 has only offline synthetic and local
  CSV adapters.
- Market calendars, corporate actions and multi-symbol datasets.
- More indicators, price sources (for example open or typical price for EMA) and adjusted
  data. Step 26 has EMA, Wilder RSI, the volume average and session VWAP only.
- Turning research signals (Step 27) into order-type `trading_signal` proposals or paper
  intents, and any connection from indicators or research signals to the risk engine or
  paper accounts.
- More research rules (short or exit rules, rule combinations). Step 30 describes saved
  simulation runs only; there are no multi-run backtests, parameter sweeps, risk-adjusted
  or annualized statistics.
- Connecting replayed or imported data to paper authorization. This would need a
  separate, freshness-safe design.
- AI-generated signals or AI trading agents. Step 28's agents are deterministic local
  rules; only an interface for advisory AI commentary exists, with no provider calls.
- Any path from the research-agent verdict to paper authorization or intents.
- Broker connections and order submission.
- Real fills or positions. Step 29 simulates fills, positions and P&L only inside its
  own offline simulator; nothing connects simulation results to Step 24 paper accounts.
- More simulation features: short selling, limit and stop orders, partial fills,
  multiple symbols, liquidity or market-impact models, and strategy optimization.
- More analytics: comparisons across runs, risk-adjusted ratios, benchmarks and charts.
  Step 30 reports are read-only descriptions of one run.
- Background or scheduled execution.
- A trading dashboard.

Every one of these needs its own design step, with explicit consent and safety review.

### Shared: future Command Center

A read-only view across both subsystems: production states and quality reports for
content, journal timelines and account state for trading. It must not authorize orders,
change limits, release the kill switch or publish content without a separate design step.

Step 31 added its data layer: execution events, recorded and reconstructed timelines,
display states and a terminal replay (see [Execution events](events.md)). Step 32 added
the visual, read-only Living HQ on top of it (see [Living HQ](hq.md)). Still not built:
- live event recording for content productions, the Step 5 shared workflow, the paper
  journal and other commands;
- liveness across machines, and streaming instead of polling;
- multi-user access;
- any control actions (trading controls, publishing, retries).

## Rules that apply to every step

- **Workflow:** inspect and test the merged steps first, preserve working behaviour, and
  build one branch and one PR per step, which is never merged automatically.
- **Credentials:** they come only from environment variables. They never go in code,
  logs, runtime files or errors, and every step runs the credential audit.
- **Tests:** they block the network and use mock or synthetic data, so they spend no API
  credits.
- **Local storage:** runtime data stays in ignored `runtime/` and is written atomically.
