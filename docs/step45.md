# Step 45: optional narration in the video-production workflow

> **Numbering.** "Step 45" is the development step in [the roadmap](roadmap.md). It is not a GitHub
> pull-request number. GitHub PR #45 was the Step 44 repair; this step arrives in a later PR.

Step 45 lets the [Step 44 workflow](step44.md) mix **your own local recording** into the
generated-footage preview. The same narrated video then goes through quality checks, human
review and export. Without narration, the workflow behaves exactly as in Step 44 (silent output).

No voice service is used, and nothing is generated: you supply a WAV file. Nothing is uploaded.

## What changes

```
start --narration voice.wav
  -> the WAV is validated (Step 14 rules) and must not be all silence
  -> the exact bytes are copied to <workflow>/narration/narration.wav and their SHA-256 recorded
  ... jobs, consented generation, downloads, media manifest (unchanged) ...
  -> preview_revision   the clips are assembled with their own sound MUTED and the narration mixed in
  -> quality            the report binds the narrated video AND the narration bytes
  -> review / export    unchanged gates, bound to that exact narrated video
```

Reused, not duplicated: Step 14 narration validation and muxing (`narration.py`, `preview.py`),
the Step 42/43 media renderer and production revision (`media_render.py`,
`media_production.py`), Step 36 quality binding, Step 37 review and Step 38 export.

## Use it

Use the Step 44 commands and add `--narration` to `start`:

```powershell
python -m vicekrack video-production-start --selection SELECTION_RUN_ID --record RECORD_ID --narration voice.wav
python -m vicekrack video-production-start --production PRODUCTION_ID --narration voice.wav
```

Everything after that is the same as [Step 44](step44.md): submit each scene with its own consent
phrase, resume, record a human review, export. `video-production-inspect` shows a `narration`
section (hash, durations and the duration policy). The original file path is never stored or
printed.

Accepted audio (the Step 14 rules): uncompressed **16-bit PCM WAV**, mono or stereo, 8–48 kHz, at
most **15 seconds** and **12 MB**. Metadata chunks are removed before mixing.

## Duration policy

The preview is always **15 seconds** (four scenes, fixed by the scene plan).

| Narration | What happens |
|---|---|
| Shorter than 15 s | Accepted. It starts at 0 s and is **padded with silence** to 15 s. |
| Exactly 15 s | Accepted as is. |
| Longer than 15 s (even by one sample) | **Refused at start** with `narration_too_long`. Nothing is created and nothing is cut off. Shorten the recording and start again. |

Speech is never silently truncated. The quality report also fails (`audio_duration_wrong`) if the
rendered audio is not 15 seconds long.

## Errors

All of these are refused **before** a workflow folder, a job or any request exists:

| Code | Cause |
|---|---|
| `narration_not_found` | The file does not exist |
| `narration_unreadable` | Not a regular readable file (for example a folder) |
| `narration_empty` | Empty file, or a WAV with no samples |
| `narration_too_large` | Larger than 12 MB |
| `narration_unsupported_format` | Not 16-bit PCM WAV, wrong channel count or sample rate |
| `narration_corrupt` | Truncated or malformed WAV |
| `narration_too_long` | Longer than 15 seconds |
| `narration_silent` | Every sample is silence (new in Step 45) |

Messages are fixed text. They never include the path or file contents.

## Integrity and approvals

- **The managed copy is the authority.** Editing or deleting your original file after `start` has
  no effect. Every resume, submit, retry-scene and export re-hashes
  `narration/narration.wav`; a missing or changed copy stops with `narration_tampered`
  (restore the exact bytes, or start a new workflow).
- **Narration is part of the revision's identity.** The same clips with different (or no)
  narration are a different video: they are rendered again and need a **fresh quality report and
  a fresh human approval**. An approval of a silent preview never authorizes a narrated one, and
  vice versa.
- **The narration is bound.** The production keeps the narration bytes beside the rendered package
  (`previews/<package>/narration.wav`, recorded as `narration_path` and `narration_sha256`). The
  quality report binds that file and checks it against the manifest's normalized-audio hash. If it
  changes afterwards, the report no longer matches, so the approval stops applying and
  Step 38 export is refused.
- **The narration source is never exported.** The export carries `preview.mp4` exactly as reviewed
  (narration already mixed in).
- **Different narration means a different workflow.** The narration hash is part of the workflow
  ID. Silent workflows keep their Step 44 IDs.

## Resume and reuse (no repeated paid work)

- Resume never submits, never re-downloads a clip it has, and never re-renders a preview it has
  already rendered (a narrated preview rendered just before a crash is adopted).
- **Adding narration after a silent run costs nothing extra.** Step 43 jobs are shared by request,
  so a narrated workflow for the same story and settings reuses the four paid jobs. Their clips
  are copied from the earlier workflow's folder only if byte-identical to the recorded hash
  (`reused_clip_scene_N` in the trace). If such a clip is missing or changed, the stage fails with
  `clip_unavailable`; it is **never downloaded or paid for again** automatically.
- Rendering the narrated preview makes it the production's current preview, so the earlier silent
  workflow's export is then refused (`production_preview_changed`), as in Step 44.

## Offline demo (no network, no API credits)

Needs FFmpeg (`python -m pip install -r requirements-render.txt`).

```powershell
python -m vicekrack video-production-demo --narrated
python scripts/step45_demo.py
```

Both do the same thing in a fresh folder, `runtime/video-production-demo/run-<time>-<id>/`, with every
socket connection blocked and a **mock** provider. It is the Step 44 demo plus:

- a **synthetic** narration (`inputs/synthetic-narration.wav`, 11.5 s of tone bursts written by
  Python; **not a voice**);
- synthetic clips that carry their own loud 440 Hz tone, standing in for provider-generated sound;
- proofs that a 16-second narration and an all-silent narration are refused and create nothing;
- a proof that a changed managed copy is refused on resume (`narration_tampered`), then restored;
- measurements of the real exported MP4: 15 s of AAC audio, loud where the narration plays and
  silent after it, which shows the clips' own tone was muted.

The printed `video` is the real 1080x1920, 24 fps, 15-second narrated MP4; `export_package` is
the verified package and `narration` holds the measurements. The review is **simulated** and
labelled `SIMULATED DEMO REVIEWER (not a person)`.

## Limitations

- Live Grok generation has **not** been tested with a paid request; only mocked provider responses.
- The demo's narration is synthetic tones and its clips are synthetic colour bars.
- One narration track, starting at 0 s. No per-scene timing, ducking, music, SFX or lip sync.
- No automatic voice (no text-to-speech); you supply the recording and are responsible for voice
  consent and rights. Narration rights are not verified.
- The preview length stays fixed at 15 seconds; longer narration is refused rather than extending
  the video.
- Output stays `publishable: false`; nothing is uploaded or published.
