# Scout

- **ID:** `scout`
- **Capability:** `research_intake`
- **Purpose:** collect possible stories for a content profile from an explicit list of approved feeds.

## Responsibilities

1. Read only the RSS/Atom feeds named in the selected source list, once per run, in order.
2. Keep only items whose headline or summary mentions a configured keyword and whose link
   points to that source's approved hosts over https.
3. Record each story's provenance: source, URL, headline, publish time, retrieval time,
   cleaned excerpt, and up to three candidate claims quoted from the source.
4. Skip stories already collected, by cleaned URL and by headline within a source.
5. Report each source's outcome with fixed error codes; one failing source never stops the run.

## Boundaries

The Scout does **not** verify anything. Every candidate and claim is `unverified` by
contract, and candidates are not Story Briefs, so they cannot reach the Creator directly.
It does not follow article links, crawl, search, call a model, rank stories, schedule
itself, or publish. Network access happens only for http-mode source lists with `--live`.
Feed text is untrusted data, never instructions.

## Step 16 implementation

`vicekrack/scout.py` (config, fetchers, parsing, candidates) and `vicekrack/scout_cli.py`
(`scout-sources`, `scout`, `scout-list`). Source lists: `config/scout-sources.mock.json`
(offline fixtures, default) and `config/scout-sources.gta.json` (live GTA VI profile).
Candidates and run reports are saved to ignored `runtime/scout/`.
