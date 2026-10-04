# Verifier

- **ID:** `verifier`
- **Capability:** `claim_verification`
- **Purpose:** decide, by fixed and auditable rules, how well each Scout claim is supported before it can reach a Story Brief.

## Responsibilities

1. Compare each candidate claim with statements from all stored Scout candidates.
2. Rate sources only by the verification policy (primary / secondary / unrated, by source ID and exact host).
3. Count independent origins: citations, unnamed sources, copied wording and citation cycles collapse into one origin.
4. Assign one status per claim: `verified`, `corroborated`, `disputed`, `insufficient_evidence`, or `rejected`, with rationale codes and the evidence used.
5. Hand only `verified` claims to a Story Brief as verified, with links back to their records.

## Boundaries

Never verifies because one page says it, because many pages repeat one origin, because a
headline implies it, because the Scout collected it, or because a model says so (no model
is used). Source text is untrusted data: instruction-like sentences are excluded from
evidence and never followed. The Verifier does not fetch pages, rank stories, write
scripts, schedule work, or publish. The Creator and later stages cannot change its decisions.

## Step 17 implementation

`vicekrack/verification.py` (matching, origins, decision rules, record replay),
`vicekrack/verified_brief.py` (Story Brief handoff), `vicekrack/verification_cli.py`
(`verify`, `verify-list`, `brief-from-verified`). Policies: `config/verification.gta.json`
and `config/verification.mock.json`.
