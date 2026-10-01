# Short Script contract (version 1.0)

A Short Script describes one short vertical video: what is said, when, what is shown,
and which verified facts it relies on. It is subject-neutral. GTA is the first
content profile, but the same contract works for any subject (`content_profile` is just a
label such as `gta`, `nba`, or `home-cooking`).

- Schema: `schemas/short-script.schema.json`
- Validator: `vicekrack/short_script.py` (`validate_short_script`)
- Example: `examples/short-script-gta.json`
- Tests: `tests/test_short_script.py`

The validator only checks data. It does not call any provider, open a network connection,
render media, or save anything. It is not yet connected to the orchestrator or to the
existing Researcher -> Analyst -> Reviewer workflow.

## Usage

```python
from vicekrack.short_script import validate_short_script

validate_short_script(script)                                 # drafting: unverified claims allowed
validate_short_script(script, require_verified_claims=True)   # production gate
```

Failures raise `NetworkError` with one of these codes:

| Code | Meaning |
| --- | --- |
| `invalid_short_script` | Structure, timing, fallback, or reference rule failed |
| `unverified_claims` | Production gate: at least one claim is not `verified` |
| `sensitive_state` | Credential-shaped text or an active API key value was found |

Messages name a field location and a fixed reason. They never repeat script content.

## Format: `vertical_short_15s`

9:16, 15 seconds, exactly four beats in this order:

| Beat | Seconds | Max narration words |
| --- | --- | --- |
| `hook` | 0-3 | 10 |
| `context` | 3-7 | 14 |
| `key_info` | 7-12 | 17 |
| `payoff` | 12-15 | 10 |

The word limit is 3.5 words per second (`MAX_WORDS_PER_SECOND`), rounded down. Words are
counted by spaces, so numbers such as `2026` count as one word even though they take
longer to say. New formats are added to the `FORMATS` table in the validator.

## Top-level fields

| Field | Purpose |
| --- | --- |
| `contract`, `version` | Always `short_script` and `1.0` |
| `script_id` | Stable ID; later outputs (scene plan, audio, video) point back to it |
| `parent_task_id` | Optional ID of the task that produced the script |
| `content_profile` | Subject label, for example `gta` |
| `format`, `aspect_ratio`, `duration_seconds` | Must match the format table |
| `language` | For example `en` or `en-US` |
| `title`, `angle` | Working title and the one-line angle of the video |
| `sources` | Where facts came from (`official`, `press`, `community`, `other`) |
| `claims` | Each fact the video states, with `verified` or `unverified` status |
| `beats` | The timed scenes |
| `captions` | Whether captions are shown and their style |
| `audio` | Whether there is a voiceover and an optional music mood |
| `disclosures` | Optional viewer-facing notes, such as "Fan-made" or "Some visuals are AI-generated" |
| `provenance` | Which agent and provider made the script, and when |

Source `url` values, when present, must be `https://` with a host and no embedded login.
Timestamps must be real UTC dates ending in `Z`. These two checks are done in Python
because the installed schema validator does not enforce URL or date-time formats.

## Beats

Each beat has `narration` (spoken text), optional `on_screen_text` (a short headline
overlay, separate from captions), `claim_ids` (facts the beat relies on), an optional
`sound_cue`, and a `visual` plan.

## Visual plan and fallbacks

AI-generated video is optional. Every scene names a preferred method and one or more
fallbacks, so production can continue when a service is unavailable.

| Method | Meaning |
| --- | --- |
| `ai_video_clip` | Generated video clip |
| `sourced_media` | Footage or screenshots from a listed source |
| `generated_image` | Generated still image |
| `animated_image` | Pan, zoom, or parallax on a still image |
| `motion_graphics` | Animated text and shapes, made locally |
| `text_card` | Plain background with text, made locally |

Rules:

1. At least one fallback; the preferred method is not repeated; no duplicates.
2. The final fallback must be `motion_graphics` or `text_card`, so every scene can always
   be produced without any external service.
3. `ai_video_clip` or `generated_image` anywhere in the plan requires a `generation_prompt`.
4. `sourced_media` requires `source_ids`.
5. `animated_image` requires a `generation_prompt` or `source_ids`.
6. `avoid` lists things the visual must not include (for example logos or real likenesses).

## Claims and verification

- Every claim referenced by a beat must exist, and every claim must be used by a beat,
  so the list of claims to verify is exactly what the video says.
- Every source referenced by a claim or a visual must exist. IDs must be unique.
- A `verified` claim must cite at least one source.
- The `key_info` beat must reference at least one claim.
- Drafts may contain `unverified` claims. The production gate
  (`require_verified_claims=True`) rejects them. Only the future Verification stage should
  mark claims `verified`.

## How it connects later (planned, not implemented)

Research/Scout fills `sources` and `claims`; Verification sets claim status; Creator writes
the beats and may only cite existing claims; the production gate runs before any media
spending; Video Producer tries each beat's preferred method, then its fallbacks, and records
what it used in a separate scene plan keyed by `script_id`; Voice reads narration within
each beat window and captions come from the narration; Renderer assembles the MP4 from the
beat timings, 9:16 frame, assets, voice, captions, and audio cues. The approved script is
never modified downstream.

## About the GTA example

`examples/short-script-gta.json` is a test fixture, not publishable content. Its facts come
from general knowledge, not a live check. The release-date claim is intentionally
`unverified`, so the example passes normal validation but fails the production gate.
Source URLs are omitted rather than guessed. Using official trailer frames raises a
usage-rights question that the future Publisher stage must handle.
