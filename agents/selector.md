# Story Selector

- **ID:** `selector`
- **Capability:** `story_selection`
- **Purpose:** decide which verified stories are worth turning into content, and keep ViceKrack from making the same story twice.

## Responsibilities

1. Read Verification Records (re-validated by replay) and apply hard gates: no verified
   claim, contradicted, disputed, unconfirmed rumor, irrelevant, stale, duplicate, or
   instruction-like text.
2. Score the rest 0–100 with the editorial profile's fixed weights: confidence, authority,
   corroboration, recency, relevance, significance.
3. Give every story an explicit `select`, `hold` or `reject` disposition with reason codes.
4. Compare against a bounded story history to tell duplicates, near-duplicates and genuine
   updates apart.
5. Hand a `select` story to the Step 17 brief builder and record it in history.

## Boundaries

Never invents facts, edits claims, or changes verification statuses. Only verified claims
reach a brief. Audience or popularity data is not used and is recorded as unavailable,
never estimated. Research text is untrusted data. No model, network access, scheduling or
publishing.

## Step 18 implementation

`vicekrack/selection.py` and `vicekrack/selection_cli.py` (`select-stories`,
`selection-history`, `brief-from-selection`). Profiles: `config/editorial.gta.json`,
`config/editorial.mock.json`.
