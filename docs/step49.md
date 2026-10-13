# Step 49: the rumor lane (opt-in, always labelled)

> **Numbering.** "Step 49" is the development step in [the roadmap](roadmap.md), not a GitHub
> pull-request number.

Until now Story Selection only picked stories that an official, first-hand source confirmed.
On a slow news day that can mean nothing to make. Step 49 adds an **opt-in rumor lane**: an
editorial profile can let through stories that reputable outlets report but nobody official
has confirmed, as long as they are **labelled as rumors from start to finish**.

The rule of the lane: **hype in the delivery, honesty in the label.** A rumor is never turned
into a fact.

## Turning it on

The default profiles (`config/editorial.gta.json`, `config/editorial.mock.json`) are unchanged
and stay verified-only. Two new profiles add a `rumor_lane` block:

| Profile | Use |
|---|---|
| `config/editorial.gta.rumors.json` | GTA VI, real use |
| `config/editorial.mock.rumors.json` | offline fixtures and tests |

```json
"rumor_lane": {
  "enabled": true,
  "allow_single_report": true,
  "select_min_score": 40,
  "angle": "Unconfirmed reports, not yet confirmed by Rockstar Games or Take-Two.",
  "disclosures": {
    "multiple_reports": "MULTIPLE REPORTS - UNCONFIRMED. Not confirmed by Rockstar Games or Take-Two.",
    "single_report": "RUMOR - UNCONFIRMED. One outlet reports this; not confirmed by Rockstar or Take-Two."
  }
}
```

Each disclosure must start with its label (`MULTIPLE REPORTS - UNCONFIRMED` or
`RUMOR - UNCONFIRMED`); the profile is refused otherwise.

## Commands

```powershell
python -m vicekrack scout --sources config/scout-sources.gta.json --live
python -m vicekrack verify --all --policy config/verification.gta.json
python -m vicekrack select-stories --all --policy config/verification.gta.json --profile config/editorial.gta.rumors.json
python -m vicekrack produce SELECTION_RUN_ID RECORD_ID --policy config/verification.gta.json --profile config/editorial.gta.rumors.json --allow-draft-preview
```

`produce` refuses a rumor without `--allow-draft-preview` (`rumor_requires_draft_preview`)
before it creates any state. The finished production then appears in the Video Studio like
any other.

## What qualifies

Verification is unchanged. The lane reads the statuses it already produces:

| Label | Verification status of the story's claims | Needs |
|---|---|---|
| `multiple_reports` | `corroborated`: at least 2 independent reputable press origins, no official source | the lane |
| `single_report` | `insufficient_evidence` with `single_origin_only`: one reputable press origin | the lane and `allow_single_report` |

Never in the lane:

- a story with any verified claim (it goes through the normal, verified path);
- disputed or contradicted claims (held or rejected as before);
- unrated sources only, no matching evidence, or instruction-like text;
- stale, future-dated, off-topic or duplicate stories (the usual gates still apply);
- a score below `select_min_score` (`below_rumor_score`, rejected).

Scoring is the usual formula with rumor claims counted weakly: a corroborated claim counts half
and a single report a quarter toward confidence, and authority is 0.5 for corroborated and 0.25
for a single report. So two outlets outrank one, and both rank below an official confirmation.

The Selection Report marks lane entries with `"lane": "rumor"` and `"rumor_label"`, and the
reason `rumor_multiple_reports` or `rumor_single_report` ("RUMOR LANE: ... not officially
confirmed"). Entries outside the lane are byte-for-byte what they were.

## How the label is carried

| Stage | What happens |
|---|---|
| Brief | Rumor claims enter as `unverified`. `editorial.lane = "rumor"` and its label are recorded. The label disclosure is added first; "Presenting unconfirmed reports as fact" is added to the things to avoid. The angle is the lane's angle. The Story Brief validator refuses a rumor brief with a verified claim, without its label disclosure, or a single-report claim outside the lane. |
| Creator | The request gains a `rumor` block. Writers are told every beat that cites a claim must say it is reported or unconfirmed. The offline mock prefixes "Reportedly:" when the claim does not already hedge. |
| Validate | Every beat that cites an unconfirmed claim must contain a hedge ("reportedly", "rumored", "unconfirmed", "leakers claim", "according to", ...). Otherwise the production stops with `rumor_not_labeled`. |
| Plan and preview | The plan is a draft (unchanged rules). The top warning band reads **RUMOR - UNCONFIRMED** or **MULTIPLE REPORTS - UNCONFIRMED** instead of "DRAFT - NOT FOR PRODUCTION", on storyboards and on Grok clip videos. The label disclosure is burned into the bottom of every frame. |
| Quality | `draft_restrictions` adds `rumor_content` (needs review), records the label, and fails with `rumor_label_missing` or `rumor_not_labeled` if either was lost. Approval therefore needs the needs-review acknowledgment. |
| History | Rumor claims are fingerprinted in their own namespace, so the same rumor is not made twice, while a later official confirmation still counts as a new story. |

Everything stays `publishable: false`. Posting is still up to you.

## Limitations

- The hedge check looks for words, not meaning. It stops a bare statement of fact, not every
  misleading sentence; the human review is still the final check.
- "Independent" is Verification's origin rule. Outlets repeating one leaker count as one origin.
- No new sources were added. A rumor must still come from a reputable source in the policy.
