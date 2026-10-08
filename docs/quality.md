# Production quality report (Step 22)

`quality-report` runs **read-only technical checks** on one production run (Step 21) and
saves a structured report with an overall result:

| Result | Meaning |
| --- | --- |
| `pass` | Every check passed: the local artifacts are complete, consistent and as specified |
| `needs_review` | Nothing is broken, but something needs a person: stale evidence, draft content, a changed policy or narration file, a missing history entry, **or a check that could not run** |
| `fail` | At least one check failed: missing or altered artifacts, inconsistent manifests, wrong media, broken provenance, or an incomplete run |

**A `pass` is technical only.** Every report contains:

```json
"scope": {"technical_checks_only": true, "factual_accuracy_verified": false,
          "rights_cleared": false, "publishable": false, "permission_to_publish": false}
```

Claim status comes from the Verification stage, not from this report. Nothing here clears
image, footage, music or narration rights, and previews stay `publishable: false`.

## Commands

From the repository root (`.\.venv\Scripts\python.exe` on Windows, `.venv/bin/python` on
Linux/macOS):

```
python -m vicekrack production-list                         # find a PRODUCTION_ID
python -m vicekrack quality-report PRODUCTION_ID
python -m vicekrack quality-list
python -m vicekrack quality-binding REPORT_ID               # Step 36, read-only
```

- **`quality-report`:** prints the report file, result, all reason codes, each check's
  status and the scope block. Exit code 0 only for `pass`, 1 for `needs_review`, `fail`
  or an error.
- **`quality-list`:** lists saved reports, newest first.
- **`quality-binding`:** re-checks a saved report's artifact binding against the files as
  they are now, without running any check or writing anything. Prints the binding status
  (`matching`, `changed`, `legacy_unverified` or `unavailable`) with the roles that
  differ, the technical result and the evidence freshness recorded at check time. Exit
  code 0 only for `matching`.
- **Media measurement:** needs `requirements-render.txt` (bundled ffmpeg and Pillow).
  Without them, the media checks are reported as `unavailable` and the result is at
  best `needs_review`.

## Checks

| Check | What is verified | Typical reasons |
| --- | --- | --- |
| `state` | Production state is valid (Step 21 schema and rules), all 5 stages completed, result not publishable | `production_incomplete`, `production_state_invalid` |
| `artifacts` | Brief, script, plan, video and manifest exist inside the production folder, match their recorded SHA-256, and pass the existing validators (Story Brief, Short Script, scene plan). The plan embeds exactly the script | `creator_artifact_missing`, `plan_artifact_hash_mismatch`, `brief_artifact_outside_production`, `script_invalid` |
| `provenance` | Script claims/sources equal the brief's (the Creator changed no facts). The brief links to this production's verification record and selection run. Every claim matches its record claim text and status. Every cited source is backed by supporting, **non-superseded** evidence (first-hand primary for verified claims). The record still replays | `script_claims_differ_from_brief`, `claim_not_matching_record`, `source_not_backed_by_evidence`, `evidence_record_invalid`, `verification_policy_changed` |
| `evidence_freshness` | Record age at report time vs. the policy's `max_record_age_days` | `evidence_stale` (needs_review) |
| `draft_restrictions` | Draft flag consistent across script (unverified claims), validation, plan and manifest. Manifest `publishable: false`, `preview_only: true`. Draft previews were explicitly allowed. **The watermark band colour on every poster** matches draft (amber) or normal (teal) | `draft_content` (needs_review), `draft_rendered_without_consent`, `manifest_publishable_flags_invalid`, `watermark_missing_or_wrong` |
| `scene_timing` | Plan beats equal the format windows (0–3, 3–7, 7–12, 12–15 s) in order, the plan lasts 15 s, and manifest scenes (index, method, timing) equal the plan | `plan_timing_not_format`, `manifest_scenes_not_matching_plan` |
| `video` | Measured from the actual MP4: 1080×1920, 24 fps, 15 s (±0.05 s), and **360 frames decoded** | `video_dimensions_wrong`, `video_frame_count_wrong`, `video_decode_failed`, `media_probe_unavailable` |
| `audio` | An audio stream exists only when narration was configured. Decoded audio lasts 15 s (up to +0.1 s of AAC padding). Re-normalizing the narration file (Step 14) reproduces the manifest's audio hash and source duration | `audio_stream_unexpected`, `audio_stream_missing`, `audio_duration_wrong`, `narration_not_matching_manifest`, `narration_source_changed` |
| `manifest_consistency` | Manifest video hash equals the actual file and the saved state. Plan ID and input hash match. Declared size/fps/duration are as specified **and equal the measured media**. Audio flag matches the media. Every poster exists and is 1080×1920 | `manifest_video_hash_mismatch`, `manifest_not_matching_media`, `poster_missing` |
| `history` | The story's history entry is owned by this production and marked `produced` | `history_not_marked_produced`, `history_entry_missing` (needs_review) |

Check statuses are `pass`, `needs_review`, `fail` or **`unavailable`** (the check could not
run, with a reason such as `media_probe_unavailable`, `artifacts_unavailable` or
`requires_completed_production`). Any `unavailable` check prevents an overall `pass`.
For an incomplete or invalid production, only `state` runs and everything else is
`unavailable`.

## How media is measured

The bundled ffmpeg from `imageio-ffmpeg` runs as a subprocess:
- an argument list, no shell, no stdin
- an allowlisted environment that excludes provider keys
- a 60-second timeout

Three runs:
1. Read the container header (codec, size, fps, duration, audio stream).
2. Fully decode the video to count frames.
3. Decode the audio to measure its length.

Raw tool output is parsed in memory and never stored. Posters are read with Pillow (size
and two watermark sample pixels). No system `ffprobe` is needed.

## Storage and privacy

Reports are saved as `runtime/quality/<report_id>.json` (ignored by Git), schema
`schemas/quality-report.schema.json`.
- **Never overwritten:** every run gets its own report ID and file, written with an
  exclusive hard link.
- **Locked during checks:** the production's lock is held while checking, so a concurrent
  `production-resume` cannot change files mid-check.
- **Never modified:** the production folder itself is not changed.

Reports contain IDs, statuses, fixed reason codes and numeric/boolean measurements only.
They never contain claim or script text, prompts, credentials, environment values or raw
exceptions, and they pass the existing credential check before saving. The command
never repairs, retries, re-renders, re-verifies or publishes.

## Step 36: artifact binding

New reports are version **1.1** and carry a `binding` object (see
`schemas/quality-report.schema.json`):

| Field | Meaning |
|---|---|
| `status` | `bound`, `changed_during_inspection` or `incomplete` (something could not be read, or the production was not complete) |
| `artifacts` | role, safe reference inside the production folder, record ID (verification record only), poster index, SHA-256, size and read status for: production state, brief, script, scene plan, preview manifest, video, every poster, the verification record and the narration input |
| `configuration` | name, project-relative path (`config/*.json`) and SHA-256 of the verification policy, editorial profile, Creator and capabilities files the checks used |
| `integrity_only` | always `true` |
| `note` | binding is byte identity only |

- **One snapshot.** Every file is read once; JSON is parsed from those exact bytes, and the
  media tools are given the same file, which is re-hashed afterwards. After the checks,
  still under the production lock, every snapshotted file is hashed again. Any change,
  disappearance or new file makes the binding `changed_during_inspection`; a `pass`
  becomes `needs_review` with `artifacts_changed_during_inspection`. Such a report is
  never shown as bound.
- **Safe references only.** References are relative, have at most four parts and no `.` or
  `..` part. The narration input is recorded by hash only (its path is the user's). No
  credentials, environment values or absolute paths are stored.
- **No circularity.** The report hashes the manifest; the manifest never names a report.
  `runtime/quality` is never part of a snapshot, so a report never hashes itself or another
  report.
- **Not bound:** the selection history file read by the `history` check (it is shared,
  append-only state, and its entry is checked by ID).
- **Separate things.** `binding` (integrity of the inspected bytes), `result` (technical
  checks) and the `evidence_freshness` check are reported separately. Matching hashes do
  not prove facts, fresh evidence, rights clearance or permission to publish.

**Older reports (1.0)** have no binding. They stay readable, are shown as
`legacy_unverified`, and are never rewritten or given hashes afterwards. To get a bound
report for any production, run `quality-report PRODUCTION_ID` again: it writes a new 1.1
report and leaves the old one unchanged. For a production whose preview manifest is 1.0,
the new report still records each poster's hash as it saw it, but the manifest itself
stays unbound (see [Preview](preview.md#step-36-preview-manifest-11)).

## Limitations

- **Not content review.** The watermark check samples the band colour; it cannot read the
  watermark text. There is no OCR, image comparison or audio content analysis (speech,
  loudness, clipping).
- **Consistency, not authenticity.** Hashes catch changes made after production; someone
  who rewrites artifacts **and** all recorded hashes consistently is caught only where
  real media measurement or validators disagree.
- **Narration source required.** Narration is compared against the original file. If that
  file is moved or edited, the audio check can only report `needs_review`.
- **Fixed format.** Expected format values come from the single supported 15-second
  vertical format.
- **Unreachable draft path.** Selection only passes verified claims, so draft productions
  are not reachable through normal use. The draft checks are covered by tests.

To view a saved production and its quality reports in the Living HQ, see the [content results desk](hq.md#content-results-desk-step-35) (Step 35). It is read-only: it
never resumes, renders or checks anything.
