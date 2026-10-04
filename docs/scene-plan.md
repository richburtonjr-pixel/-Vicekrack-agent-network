# Scene plans (Step 12, contract 1.0)

The pure `build_scene_plan(script, capability_config=None, draft=False)` function creates
a deterministic JSON plan from a valid Short Script. `validate_scene_plan(plan)` checks
its schema and reconstructs its expected decisions, rejecting altered hashes, timings,
capabilities, method selection or production flags. Neither function opens providers,
fetches sources, renders video, creates audio or modifies input.

## Contract

- `script` is a deep copy of the entire validated source: claims, sources, narration,
  captions, audio, disclosures, on-screen text and provenance remain intact.
- `script_id` and optional `parent_task_id` link back to the source.
- `frame` selects 1080 by 1920 pixels for the existing 9:16 format; `duration_seconds` is 15.
- `scenes` retains the four exact beats, order and timing. Each has `selected_method`,
  `considered_methods` and `assets_produced: false`.
- `considered_methods` records the preferred method, then fallbacks, stopping at the first
  available method. Entries say selected/configured_available or skipped/not_configured_available.
  These are planning decisions, not attempted external requests or produced assets.
- `capabilities.available_methods` is normalized into sorted order. Defaults match
  `config/visual-capabilities.json`: motion_graphics and text_card. Empty lists are legal
  configuration but produce no_visual_method for every current script. Unknown names,
  duplicate names, nonlists or extra configuration fields are rejected.
- `input_sha256` hashes UTF-8 JSON with sorted keys, compact separators, preserved Unicode,
  finite numbers and original array order. `plan_id` hashes the complete plan excluding
  plan_id itself. Changing script or decisions changes identity. These hashes detect
  accidental mismatches; they are not signatures or proof of trustworthy sources.
- No generated timestamp/random ID appears in the plan content. Identical inputs, policy
  and contract produce identical plans. Unique publication filenames keep repeated
  exports separate without changing their deterministic identity.

The plan schema embeds the Short Script v1 structure and its definitions so it can be
validated standalone. If that contract changes, update/version the embedded structure
and semantic validator together. Core script validation is also performed during replay.

## Drafts and production gate

Normal planning requires all claims to be declared verified with source references.
This only checks the supplied declarations; no source is fetched or fact-checked and
media rights are not evaluated. A model/human verification label is not factual proof
or authorization to publish. Even a production-mode plan says assets_produced=false.

Explicit --draft permits unverified claims and always sets blocked_for_production=true,
even when all claims happen to be verified. The plan lists every unverified claim ID
(the source validator requires each claim to be used by a beat). Draft planning never
changes claim statuses. Future consumers must enforce the block and revalidate plans.

The GTA example remains unverified: normal planning rejects it. The cooking fixture is
synthetic test data with declared verified claims, not externally verified or approved
content. It demonstrates subject neutrality and a successful offline production gate.

## CLI (from the checkout containing Step 12)

```powershell
python -m vicekrack validate-short-script examples/short-script-gta.json
python -m vicekrack validate-short-script examples/short-script-gta.json --require-verified
python -m vicekrack plan-short examples/short-script-gta.json --draft
python -m vicekrack plan-short examples/short-script-cooking.json
python -m vicekrack plan-short examples/short-script-cooking.json --capabilities config/visual-capabilities.json
```

The second command deliberately exits 1 with unverified_claims. Successful commands exit
0; input/configuration/storage errors exit 1; argparse usage errors exit 2. CLI diagnostics
return fixed error codes, never source text or raw exceptions. plan-short prints only
location, plan ID, mode, blocked flag, scene count and assets_produced=false. To inspect
unverified IDs, open the local plan explicitly. Validation reports that no external
fact-checking occurred. There is no provider selection, account check or network call.

## Storage and scope

Plans are unencrypted and may contain sensitive user content. Files stay under ignored
runtime/plans; do not put credentials in scripts or capability files. The existing
credential rejection runs before output. It is a heuristic, not a universal secret scan.
Publication uses a same-directory temporary file, flush/fsync and exclusive hard linking;
it cannot overwrite an existing filename and requires a local filesystem with hard-link
support. An interrupted publish leaves either no final file or a complete final file.
Temporary leftovers are never promoted. Atomic publication does not promise power-loss
durability on every filesystem. Source scripts are never rewritten.

Tests cover the complete planner/CLI, defaults and mixed capabilities, selection priority,
no available method, source immutability, tampering, invalid input, no-overwrite storage,
secret rejection, explicit draft gating and no provider/network calls. Existing workflow
routing, persistence and providers are unchanged. No Step 13 or media renderer is included.
