# Step 50: Research to Video (any topic, from the app)

> **Numbering.** "Step 50" is the development step in [the roadmap](roadmap.md), not a GitHub
> pull-request number.

Type any topic in the new **Research** view, approve one Grok research request, and ViceKrack shows
labelled story cards. Pick one, approve one Grok script request, and the story opens in **Video
Studio**. No Command Prompt.

```
Research tab: "Jordan 4 release, Eagles trade rumors"  -> Go (free: shows the exact request + estimate)
  -> Approve and research (PAID, one xAI Responses request with web_search; about 2-13 cents per topic)
  -> ViceKrack opens every source page itself (free) -> Scout candidates -> Verification -> Selection
  -> story cards: MULTIPLE REPORTS / RUMOR / CONFIRMED / ON HOLD / NOT USABLE, with sources and dates
  -> Make video -> Approve and write script (PAID, Grok writer, under 1 cent)
  -> production pipeline (brief, script, validate, plan, preview) -> Open in Video Studio
```

## Start it

Real use: the **Start ViceKrack REAL** icon, or

```powershell
$env:XAI_API_KEY = "..."      # in this window only; never in the page, files or chat
python -m vicekrack hq-serve --studio
```

Offline demo (mock Grok, fixture pages, no network, no credits): `python -m vicekrack hq-serve --studio-demo`.
In the demo every story is marked **DEMO** and is not real news.

## Who decides what

| Step | Who | Notes |
|---|---|---|
| Finding stories and URLs | Grok (`grok-4.3`, web_search) | Structured JSON: topic, headline, summary, up to 3 article URLs per story |
| What a source says | **ViceKrack**, by opening the page | Grok's words never become evidence. Only the page's own title, text and date are used |
| Mentions the topic? | ViceKrack | The page must contain the typed topic (or its distinctive words) |
| Trusted outlet? | `config/verification.open.json` | 39 widely recognized outlets; others are "unlisted" |
| Label | Verification + Selection with `config/editorial.open.json` | The rumor lane from Step 49 |
| Script | Grok writer (`config/creator.xai.json`) | Claims and sources are copied from the brief; the rumor wording check still applies |

Labels on the cards:

| Label | Meaning | Make video |
|---|---|---|
| MULTIPLE REPORTS | 2+ independent trusted outlets report it; not officially confirmed | yes, labelled on screen |
| RUMOR | one trusted outlet reports it | yes, labelled on screen |
| CONFIRMED | an official source says it (profiles with official sources) | yes |
| ON HOLD | for example another card covers the same story, or it is over a week old | no |
| NOT USABLE | for example only unlisted sites, page couldn't be opened, too old, contradicted | no, with the reason |
| YOUR CALL | Step 51: old, undated, low score or unlisted site, see [step51.md](step51.md) | Use anyway, labelled on screen |

Outlets that copy the same wire story count as **one** origin, exactly as in Verification.

## Costs (prices as of 2026-10-13, docs.x.ai)

| Request | Estimate shown before approval |
|---|---|
| Research, per topic | about 2-13 cents (tokens plus 2-10 web searches) |
| Script | under 1 cent |

After the research the card list shows the cost computed from the usage xAI reported. Prices are in
`config/research.json` and are only used for these estimates.

## Safety

- Both paid requests use the Studio's existing gate: loopback only, Origin and CSRF checks, one action
  at a time, request IDs, and a job-specific consent phrase (`paid-research:rsj-...`,
  `paid-script:prod-...`) that the server checks again.
- A research request is saved as `submitting` **before** it is sent. If the outcome is unknown it
  becomes `uncertain` and is never resent without the duplicate-billing acknowledgment. A refused
  request (`xai_rejected`, `xai_not_sent`) was not billed and can simply be retried.
- A made story is never written twice: the production ID is fixed per story, and **Make video** on a
  finished story opens it instead.
- Page fetching reuses the Step 20 article rules: public HTTPS only, the source's own host, no query
  strings, bounded size, time and redirects, HTML only, scripts ignored, instruction-like sentences
  dropped.
- The key is read only by the transport; research jobs refuse to save anything containing it, and the
  Studio's last-line guard still applies to every response.
- Everything stays `publishable: false`.

## Files

| File | What |
|---|---|
| `vicekrack/research_desk.py` | prepare, submit, free checks, story cards, make video |
| `vicekrack/xai_responses.py` | bounded xAI Responses transport, structured-output parsing, the Grok writer |
| `vicekrack/research_demo.py` | mock Grok research and writer, fixture pages |
| `vicekrack/hq/static/research.js` | the Research view |
| `config/research.json`, `config/verification.open.json`, `config/editorial.open.json`, `config/creator.xai.json` | settings |

Runs are saved in `runtime/research/rsj-*.json`.

## Limitations

- The live xAI web_search response has not been tested from this environment; it follows the
  documented Responses format and is fully tested with mocks. The first real run is a test.
- Grok may return URLs that are paywalled, blocked or need query strings; those sources show
  "couldn't open the page" and are not used.
- Any-topic research has no official sources configured, so stories are MULTIPLE REPORTS or RUMOR,
  not CONFIRMED. GTA keeps its official sources in its own profile.
- Topic relevance is a keyword check, not understanding.
