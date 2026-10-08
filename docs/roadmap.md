# ViceKrack roadmap

ViceKrack has three tracks. Each step was built on its own branch, reviewed as a pull
request and merged into `main`. The content track uses GTA VI as its first profile, but
its contracts stay subject-neutral. See [Subsystem boundaries](subsystems.md).

| Track | Purpose | Code |
|---|---|---|
| **Shared infrastructure** | Orchestration, providers, persistence, checks and safety rules used by both subsystems | `vicekrack/` core modules, `scripts/`, CI |
| **Content** | Research → verify → select → write → plan → preview → produce → quality-check short vertical videos | `vicekrack/` content modules |
| **Trading** | Paper-only risk foundation: contracts, limits, persistent paper state, journal, offline market data, replay, descriptive indicators and research-only signals, a deterministic research-agent workflow, an offline execution simulator, read-only analytics, controlled end-to-end research sessions and an opt-in historical data adapter | `vicekrack/trading/` |

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
| 32 | Shared | ViceKrack Living HQ: local read-only visual Command Center over Step 31 timelines; two-floor isometric house (trading upstairs, content downstairs, shared operations lobby/lounge/kitchen) with distinct bots; status-first replay (play/pause/scrub/speed), recorded vs reconstructed vs live vs demo labelling, honest partial/interrupted display, supported-only handoffs, decorative idle behaviour; inspector, Timeline/Text views, presentation mode, reduced motion and keyboard access; loopback-only GET API with Host/Origin checks and strict CSP; deterministic demo | #32 |
| 33 | Shared | Accurate agent activity: rooms show only their actual roles (Step 5 Researcher/Analyst/Reviewer, production Creator); automated stages and controllers as labelled operations stations; optional event recording for the Step 5 workflow, production pipeline and quality report with explicit resume handling (`stage_reused`, attempt numbers, shared correlation), fail-closed recording that keeps committed results and never repeats paid calls; read-only reconstruction of saved workflow runs; department timeline filters, attempt grouping, honest movement labels and idle-spot occupancy | #33 |
| 34 | Shared | Read-only trading results desk in the Living HQ: saved Step 29 runs and Step 30 reports correlated with timelines by validated IDs and hashes (mismatches rejected, missing data explicit); portfolio at a replay position built server-side from events up to that position only (no future fills, final P&L or end-of-run statistics), or `unavailable` when the timeline cannot support it; separate completed-run summary with `unavailable` metrics kept; equity/drawdown charts, accessible tables, limitations, simulator-station integration, presentation mode; labelled demo results | #34 |
| 35 | Shared | Read-only content results desk in the Living HQ: saved productions with stages, attempts, failures and reuse; Story Brief, sources (text links, explicit open only), verification limits, script beats, scene plan with posters, local preview player with optional narration, Step 22 findings; artifacts re-verified by path, hash, contract and chain (tampered/mismatched/missing rejected); replay view shows only what the timeline proves existed, otherwise historical viewing unavailable; quality reports shown as unverified or stale, never current; opaque-ID media route with bounded byte ranges; Creator room and production stations open it; dedicated real-browser CI job for both desks | #35 |
| 36 | Content | Verifiable content artifacts: preview manifest 1.1 with a SHA-256 and size for every scene poster (video hashing kept), every file, path, size and hash validated before publication; quality report 1.1 binds the exact artifacts and configuration it inspected (safe IDs and hashes only) from one snapshot, detects changes during inspection, never hashes reports (no circularity); legacy manifests and reports stay readable as `not hash-bound` and `unverified`, never upgraded; `quality-binding REPORT_ID` re-checks a saved report read-only; Living HQ shows separate artifact binding, technical result and evidence freshness labels and re-validates on load and when serving | #36 |
| 37 | Content | Human review decisions for content previews: append-only `content_review` records (`approved_for_preview`, `changes_requested`, `rejected`) tied to a quality report's ID and hash and the exact binding digest, with a self-declared reviewer label, time, acknowledgments and bounded notes; matching binding required, approval blocked on `fail` or unknown evidence freshness, explicit acknowledgment of needs_review, unavailable checks, draft restrictions and stale evidence; re-validated under the production lock before saving, refused on changes during review; explicit supersession, concurrent writers refused, atomic exclusive writes, corrupted history visible; applicability and evidence freshness computed on every read; `review-record`, `review-list`, `review-inspect`; read-only review history in the Content Results Desk with replay visibility rules | #37 |
| 38 | Content | Portable, locally viewable preview packages: `export-preview` (purposes `review_copy` and `approved_preview`) copies an allowlisted set (preview MP4, posters, script, derived provenance summary, bound quality report, derived review snapshot) plus a static offline HTML page (escaped, relative links, no JavaScript or remote resources) and a versioned manifest of every payload file's path, size and SHA-256; gates on matching binding, no technical fail, intact review history and present unchanged artifacts, and a current approval for `approved_preview`; reviewer labels and notes excluded unless explicitly included; consistent snapshot under the production lock, copied bytes re-verified, concurrent changes refused, atomic publish without overwrite; `export-verify` checks a moved package without the production (inventory, sizes, hashes, schemas, references); hashes are consistency checks, not signatures | #38 |
| 39 | Trading | Controlled end-to-end trading research session: `trading-session-start/-resume/-list/-inspect` run the existing components in a fixed five-stage order (validate dataset → research agents → simulation → analytics → HQ summary); records dataset, configuration snapshots, component versions and artifact hashes; research labelled with its exact simulated as-of time and never passed to the simulator (identical to a standalone run); fixed stage limit, explicit failures, no automatic retries, attempt limit; atomic checkpoints with a hash chain, artifacts published once, OS lock against concurrent runs, crash recovery that adopts published artifacts instead of re-running them, resume refused on tampered artifacts, changed configuration or component versions; append-only registration in the existing stores; optional event recording with the session's correlation ID; versioned session manifest and a read-only Living HQ Sessions view linking research rooms, simulator station and analytics desk without merging timelines; synthetic demo session | #39 |
| 40 | Trading | Opt-in historical market data from Alpaca: `market-fetch` (explicit `--allow-network`, credentials only from `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`) downloads completed historical bars for one symbol, interval and bounded date range through a provider adapter on the Step 25 interface; fixed official HTTPS endpoint, bounded pages/bytes/bars/time, no redirects, no retries or feed fallback; explicit feed and adjustment; exact decimals; Step 25 validation (order, duplicates, alignment, gaps, availability); provenance with requested range, coverage, retrieval time (not market time), request and response hashes and no credentials; atomic publish only after full validation; sanitized auth, entitlement, rate-limit, timeout and malformed-response errors; datasets usable by Step 39 sessions offline; provenance and coverage in the Living HQ Sessions view; mocked tests only (live access unverified) | this PR |

## Deferred work

### Content: human review (Step 37) and portable export package (Step 38), both implemented

First proposed as Step 23 and set aside when trading took that slot.
- **Human review: implemented in Step 37.** Explicit, append-only decisions
  (`approved_for_preview`, `changes_requested`, `rejected`) tied to a bound quality report
  and the exact artifacts it inspected (see [Human review](review.md)). Approval accepts the
  preview only; `publishable` stays `false`.
- **Portable preview package: implemented in Step 38.** `export-preview` writes a
  self-contained, offline-viewable package (preview, posters, script, provenance summary,
  bound quality report, review snapshot, static HTML page, hashed manifest), and
  `export-verify` checks it anywhere (see [Portable preview packages](export.md)). It is
  for local review and manual handling only: nothing is uploaded or published, and an
  approved preview is still not a rights clearance or permission to publish.

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

Steps 23–30 built the trading
foundation, and Steps 31–35 added shared execution events, the read-only Living HQ,
optional event recording for the existing content commands (outputs unchanged), and
read-only trading and content results desks. The content desk only displays saved
productions: it adds no review decision, export or publishing, so the human review and
export package below is still deferred. Step 36 made content artifacts verifiable
(poster hashes in new preview manifests, quality reports bound to the exact files they
inspected); it is integrity evidence only. Step 37 added explicit human review decisions on
top of that binding, and Step 38 portable, offline preview packages. There is still no
upload or publishing. All earlier content tests still pass.

### Trading: not started, and out of scope until explicitly requested

- Live, delayed or streaming market-data feeds, and more historical providers. Step 25 has
  offline synthetic and local CSV adapters; Step 40 adds one opt-in historical adapter
  (Alpaca, completed bars only, one download per command, no polling).
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
- More analytics: comparisons across runs, risk-adjusted ratios and benchmarks. Step 30
  reports are read-only descriptions of one run; Step 34 only displays them (with charts)
  in the Living HQ.
- Background or scheduled execution. Step 39 sessions run only when a person starts or
  resumes them, in the foreground, with no automatic retries.
- Comparing sessions, multi-dataset or multi-symbol sessions, and strategy optimization
  across sessions. A Step 39 session is one dataset and one fixed configuration.
- Trading controls of any kind. Step 34's results desk is display-only: it cannot run a
  simulation, generate analytics, engage or release a kill switch, or touch accounts.

Every one of these needs its own design step, with explicit consent and safety review.

### Shared: future Command Center

A read-only view across both subsystems: production states and quality reports for
content, journal timelines and account state for trading. It must not authorize orders,
change limits, release the kill switch or publish content without a separate design step.

Step 31 added its data layer: execution events, recorded and reconstructed timelines,
display states and a terminal replay (see [Execution events](events.md)). Step 32 added
the visual, read-only Living HQ on top of it (see [Living HQ](hq.md)). Step 33 added event
recording for the Step 5 workflow, content productions and quality reports, with
role-accurate rooms. Step 34 added a read-only trading results desk for saved simulations, and Step 35 a
read-only content results desk for saved productions. Step 36 lets that desk re-check
whether a saved quality report still matches the files it inspected, and Step 37 shows the
human review history there (read-only). Step 39 added a read-only Sessions view that links a
trading research session's research rooms, simulator station and analytics desk; it cannot
start or resume sessions.
Still not built:
- live event recording for the paper journal and the other commands;
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
