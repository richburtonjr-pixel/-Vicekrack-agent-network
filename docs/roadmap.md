# ViceKrack roadmap

ViceKrack has three tracks. Each step was built on its own branch, reviewed as a pull
request and merged into `main`. The content track uses GTA VI as its first profile, but
its contracts stay subject-neutral. See [Subsystem boundaries](subsystems.md).

| Track | Purpose | Code |
|---|---|---|
| **Shared infrastructure** | Orchestration, providers, persistence, checks and safety rules used by both subsystems | `vicekrack/` core modules, `scripts/`, CI |
| **Content** | Research → verify → select → write → plan → preview → produce → quality-check short vertical videos | `vicekrack/` content modules |
| **Trading** | Paper-only risk foundation: contracts, limits, persistent paper state, journal, offline market data, replay, descriptive indicators and research-only signals, a deterministic research-agent workflow, an offline execution simulator, read-only analytics, controlled end-to-end research sessions and an opt-in historical data adapter and an explicit Alpaca paper-broker adapter | `vicekrack/trading/` |

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
| 40 | Trading | Opt-in historical market data from Alpaca: `market-fetch` (explicit `--allow-network`, credentials only from `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`) downloads completed historical bars for one symbol, interval and bounded date range through a provider adapter on the Step 25 interface; fixed official HTTPS endpoint, bounded pages/bytes/bars/time, no redirects, no retries or feed fallback; explicit feed and adjustment; exact decimals; Step 25 validation (order, duplicates, alignment, gaps, availability); provenance with requested range, coverage, retrieval time (not market time), request and response hashes and no credentials; atomic publish only after full validation; sanitized auth, entitlement, rate-limit, timeout and malformed-response errors; datasets usable by Step 39 sessions offline; provenance and coverage in the Living HQ Sessions view; mocked tests only (live access unverified) | #40 |
| 41 | Trading | Alpaca PAPER brokerage adapter: fixed paper endpoint only (real-money host, overrides and redirects refused), separate `ALPACA_PAPER_*` credentials; `broker-paper-check/-prepare/-submit/-status/-cancel/-list/-inspect/-kill-switch`; immutable intents with a concrete proposal, explicit `paper-execute:<id>` consent, full revalidation before submission (account, clock/regular session, asset eligibility, fresh quote, spread and price band, positions, open orders, limits, kill switch, local reconciliation); long-only whole-share day limit orders with worst-case exposure = quantity x limit; stable client order IDs, attempt saved before the request, at-most-once per intent, OS lock; unknown outcomes reconciled by client order ID, never resubmitted; broker-confirmed states including partial fills; cancel requests distinct from confirmed cancellation; sanitized events and records (no keys, raw responses, account numbers or balances); read-only Paper broker view in the Living HQ separate from simulation; mocked tests only (live paper access unverified) | this PR |
| HQ 1 | Shared | One department registry (`config/departments.json`): departments (Headquarters, Content Production, Trading paper-only), agents per department and 3D HQ rooms; schema plus consistency checks, read-only `GET /api/departments`, the Trading terminal reads its agents from it, and a test fails if the 3D HQ drifts from it. Display data only: no permissions, limits or live status. See [Departments](departments.md) | #63 |
| HQ 3a | Trading | Opt-in historical crypto bars from Alpaca (BTC/USD, ETH/USD) through `market-fetch`: fixed v1beta3 US crypto endpoint, same keys and bounds as Step 40, UTC and 24/7 (no gaps over nights or weekends), intraday intervals only (daily refused until Alpaca's daily start is confirmed), stored as `BTCUSD`/`ETHUSD` with the pair in provenance, prices never rounded, extra volume decimals cut and counted; tamper-checked crypto provenance, stock datasets cannot claim crypto fields; usable by research sessions and the HQ offline; mocked tests only. Fractional simulation is Phase 3b | this PR |

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
- Real-money broker connections and orders, and any automatic or signal-driven order
  submission. Step 41 adds only an Alpaca PAPER adapter with explicit, consented,
  person-driven commands.
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
human review history there (read-only). Step 41 added a read-only Paper broker view of saved Alpaca paper orders (no order controls).
Step 39 added a read-only Sessions view that links a
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
# Step 43 implementation checkpoint

Implemented: explicit persisted xAI video jobs, uncertainty/duplicate protection,
bounded downloads, four-scene media manifest assembly, and repaired local media rendering.
Offline synthetic demo and mocked provider tests require no API credits.
Implemented production attachment: preserve old previews, atomically revise the current
preview, invalidate old approvals, and require fresh quality/review/export bindings.
Live xAI behavior has not been paid-tested.
Trading and the deferred farmhouse visualization are unchanged.

# Step 44 implementation checkpoint

Implemented: a controlled, resumable video-production workflow (`video-production-*` commands)
that connects these existing parts:

- approved story;
- scene plan;
- four consented Grok jobs;
- bounded status and download;
- media manifest;
- revised preview;
- quality;
- explicit human review;
- gated export.

Uncertain submissions pause, and a retry needs an explicit duplicate-billing acknowledgment.
Configuration, plan, media and preview changes are refused. Stale approvals cannot export.

An offline demo renders a real MP4 using a mock provider and synthetic clips. Live xAI behaviour
remains untested with paid requests.

Deferred: automatic fallback media selection, voice/music mixing, publishing, farmhouse
visualization. Trading is unchanged.

# Step 45 implementation checkpoint

Numbering note: development steps and GitHub pull requests are counted separately. GitHub PR #45
was the Step 44 repair; Step 45 is delivered in a later PR.

Implemented: optional local narration in the Step 44 video-production workflow
(`video-production-start --narration FILE`, [docs/step45.md](step45.md)):

- validation with the Step 14 rules plus a new all-silence refusal, before anything is created;
- a managed, hash-recorded copy inside the workflow, re-verified on every resume, submit and export;
- narration mixed into the media-backed preview with generated source audio muted;
- duration policy: shorter is padded with silence to 15 s, longer is refused, never truncated;
- narration bound into the quality report; silent and narrated previews need separate approvals;
- reuse of already-paid clips when narration is added after a silent run (hash-checked, no requests).

Silent output is unchanged when narration is omitted. The offline demo
(`video-production-demo --narrated`) renders a real narrated MP4 with synthetic audio and clips.
Live xAI behaviour remains untested with paid requests.

Deferred: text-to-speech, per-scene narration timing, music/SFX mixing, publishing, farmhouse
visualization. Trading, Bot House, music and publishing are unchanged.

# Step 46 implementation checkpoint

Numbering note: PR #45 was the Step 44 repair, PR #46 delivered Step 45, and Step 46 arrives in a
later PR.

Implemented: Grok-generated narration ([docs/step46.md](step46.md)). It covers:

- a separate xAI text-to-speech adapter, with stock voices and documented output settings only;
- a reviewable request built from the script's narration beats and bound to the script hash and
  settings;
- `speech-prepare`, `-inspect`, `-list`, `-submit` and `-recover`;
- explicit per-job paid consent, with the intent saved before sending;
- no automatic retry of uncertain requests, and duplicate and concurrency protection;
- bounded requests and responses;
- local conversion to a metadata-free 16-bit PCM WAV, refusing malformed or silent audio;
- the 15-second rule: longer speech is kept but refused, never truncated or sped up;
- `video-production-start --speech JOB`, with the script binding verified against the
  production's saved script before any paid video submission;
- reuse of already-paid clips;
- fresh quality checks and human review for the narrated video.

The offline demo (`video-production-demo --speech`) uses mock providers and synthetic test audio.
The live xAI speech API remains untested.

Deferred: captions, music/SFX, per-scene narration timing, voice cloning, publishing, farmhouse
visualization, Step 47. Trading and Bot House are unchanged.

# Step 47 implementation checkpoint

Numbering note: PR #47 delivered Step 46, and Step 47 arrives in a later PR.

Implemented: narration-aligned captions ([docs/step47.md](step47.md)). It covers:

- the exact narration text from the speech job's script, with script and audio bindings verified;
- optional provider timing from documented xAI character timestamps (`speech-prepare
  --with-timestamps`), validated for order, overlaps, bounds and text correspondence;
- an explicit estimated mode, labelled, burned in with a visible notice, and `needs_review`;
- a `caption_track` contract with SRT/WebVTT sidecars in managed storage;
- burned-in captions on narrated media previews, with no collisions with the warnings and no
  silent text loss;
- binding into the production revision, quality report, review and export;
- caption changes that invalidate earlier approvals;
- reuse of paid speech and clips.

The offline demo is `video-production-demo --captions`. The live xAI timestamp output remains
untested.

Deferred: word-by-word highlighting, captions for local recordings, translations and other fonts,
music/SFX, publishing, farmhouse visualization, Step 48. Trading is unchanged.

# Step 48 implementation checkpoint

Numbering note: PR #48 delivered Step 47, and Step 48 arrives in a later PR.

Implemented: the Video Studio in the Living HQ ([docs/step48.md](step48.md)):

- `hq-serve --studio` and `--studio-demo`;
- production eligibility, explained in plain words;
- narration and caption choices with their limits;
- the exact paid requests, each approved explicitly;
- saved progress with the allowed recovery actions;
- playback, quality findings, explicit review and verified-export download.

The action routes are narrow and validated: loopback only, Host and Origin checks, session and
CSRF, size limits, de-duplication and one action at a time. The read-only HQ is unchanged without
the flag. The offline demo uses mocks and a separate folder.

Deferred: farmhouse redesign, trading changes, music, social publishing, Step 49.

# Step 49 implementation checkpoint

Implemented: the opt-in rumor lane ([docs/step49.md](step49.md)):

- `rumor_lane` in the editorial profile (`config/editorial.gta.rumors.json`,
  `config/editorial.mock.rumors.json`); default profiles unchanged;
- `multiple_reports` (corroborated) and `single_report` (one reputable origin) labels;
- rumor claims enter briefs as unverified, with the label disclosure, which the validator
  protects;
- the script must hedge every unconfirmed claim (`rumor_not_labeled`);
- a "RUMOR - UNCONFIRMED" / "MULTIPLE REPORTS - UNCONFIRMED" warning band on previews and videos;
- `rumor_content` needs review in the quality report;
- rumor fingerprints in story history.

Deferred: non-news "cool clips" content, new rumor sources, Studio badge for rumor productions.

# Step 50 implementation checkpoint

Implemented: Research to Video from the app ([docs/step50.md](step50.md)):

- a Research view: any topic, one paid Grok web-search request on approval, with an estimate first;
- ViceKrack opens every source page itself; only page text becomes evidence;
- any-topic verification policy and editorial profile (`open`) with the rumor lane;
- labelled story cards with sources, dates and plain reasons;
- the Grok script writer (`xai` Creator adapter) and Make video into Video Studio;
- demo mode with mock Grok and fixture pages.

Recorded: the approved headquarters visual direction ([docs/visual-direction.md](visual-direction.md)).

Deferred: the headquarters redesign, ChatGPT as an optional writer/second opinion, official sources
for non-GTA topics, publishing.

# Step 51 implementation checkpoint

Implemented: looser research, you pick ([docs/step51.md](step51.md)):

- "Use anyway" on any story ViceKrack could open a real page for (old, undated, low score or unlisted
  site), labelled on screen; the `user_pick` profile `config/editorial.open.pick.json`;
- a third rumor label, `unrated_source` ("UNVERIFIED SOURCE - UNCONFIRMED");
- page dates read the way news sites actually write them; topic nicknames from Grok (Sixers for 76ers);
- source pages opened in parallel; the card reason is the deciding one;
- Make video selects the story again at click time, so yesterday's cards still work.

# Step 52 implementation checkpoint

Implemented: one approval for all of a video's clips in Video Studio ([docs/step52.md](step52.md)),
with a total-cost estimate; sending stops at the first problem.

# Step 53 fix: real Grok clips download

The xAI status check returned the finished clip's URL (`https://vidgen.x.ai/xai-vidgen-bucket/xai-video-<id>.mp4`),
but the job could not be saved: the generic credential check read `xai-video-...` as an xAI key
(`sensitive_state`), so every real clip stayed "waiting for provider" and never downloaded. The media URL is
now checked on its own (approved host `vidgen.x.ai`, no active key inside it); everything else in the job
record keeps the generic check. Tests and the demo now use the real URL shape.

# HQ 2.0, Phase 1 checkpoint

Implemented: an explorable 3D headquarters ([docs/hq3d.md](hq3d.md)) over the existing app: a two-level
glass atrium building with nine departments, third-person walking and stairs, real-data department
panels and in-world screens from the new read-only `GET /api/studio/overview`, eight marked
workstations ("character pending reference"), and links into the existing Research and Video Studio
screens. Three.js r169 is vendored and pinned. The app opens on the HQ when the Studio is on; the classic
house stays available.

Deferred: furniture and materials pass, character models (reference → model → rig → animation),
gamepad support, any agent movement.

# Step 54 implementation checkpoint

Implemented: Auto Studio ([docs/auto-studio.md](auto-studio.md)): one approval turns a research story into a
finished, verified MP4 of 15 s, 30 s, 60 s or 2 min. Grok writes a scene-by-scene script for the length,
type (news, cinematic, social) and audio style (narrator, dialogue, both); xAI text to speech voices every
line; each xAI scene clip is sized to its voiceover (up to 15 s); a background worker sends, checks,
downloads and retries clips within the approved spending cap; ViceKrack composes original music; FFmpeg
assembles intro, scenes, captions, labels, ducked music and outro; the exported file itself is checked
(length, format, scene order, voices at their times). The new Finished videos library plays, downloads,
stops and retries videos and keeps them after restarts.
