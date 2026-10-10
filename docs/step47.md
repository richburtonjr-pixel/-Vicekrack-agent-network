# Step 47: narration-aligned captions

> **Numbering.** "Step 47" is the development step in [the roadmap](roadmap.md), not a GitHub
> pull-request number. PR #47 delivered Step 46.

Step 47 adds readable captions to the generated vertical videos. The captions repeat the approved
narration **exactly**: they are built only from the validated ShortScript behind a completed
[Step 46](step46.md) Grok speech job. Their timing is either the provider's own character
timestamps or an explicitly chosen, clearly labelled estimate.

The captions are:

- **burned into** the preview video;
- bound into the production revision and its quality report;
- reviewed by a person;
- exported with **optional** SRT and WebVTT sidecar copies.

Nothing here makes a request: no new paid speech, no transcription service and no alignment
model.

```
completed speech job (+ its saved script, audio and, optionally, provider timestamps)
  -> captions-prepare    phrase cues, timing, style -> runtime/captions/<cap-id>/ (track + SRT + WebVTT)
  -> video-production-start --production P --speech JOB --captions CAP
                         paid clips and speech reused; captions burned into the narrated preview
  -> fresh quality report (binds the track and both sidecars) -> explicit human review
  -> video-production-export   video + captions/captions.srt|.vtt|.json, hashed and verified
```

## Commands

**1. Speech with provider timing (optional, PAID).** xAI documents per-character timestamps for
`POST /v1/tts` with `with_timestamps: true`. Request them when preparing the speech job:

```powershell
python -m vicekrack speech-prepare --production PRODUCTION_ID --with-timestamps
python -m vicekrack speech-submit SPEECH_JOB_ID --consent paid-speech:SPEECH_JOB_ID --allow-network
```

This is a **different request**, so it is a different job with its own review and consent. A job
prepared without `--with-timestamps` (every Step 46 job) can only use estimated timing. Nothing is
ever re-requested automatically to get timestamps.

**2. Prepare captions (offline, free).** You must choose a timing method:

```powershell
python -m vicekrack captions-prepare --speech SPEECH_JOB_ID --timing provider
python -m vicekrack captions-prepare --speech SPEECH_JOB_ID --timing estimated
python -m vicekrack captions-prepare --speech SPEECH_JOB_ID --timing estimated --script examples/short-script-gta.json
```

A speech job made from a script **file** needs that same file with `--script`. A job made from a
production uses the production's saved script.

**3. Inspect:**

```powershell
python -m vicekrack captions-inspect CAPTION_ID
python -m vicekrack captions-inspect CAPTION_ID --sidecar srt
python -m vicekrack captions-list
```

**4. Render.** The workflow renders the captions:

```powershell
python -m vicekrack video-production-start --production PRODUCTION_ID --speech SPEECH_JOB_ID --captions CAPTION_ID
```

The four paid clips are reused if already downloaded, and the speech audio is reused. Resume,
submit and review work exactly as in Steps 44–46. `video-production-inspect` shows a `captions`
section.

**5. Export.** The command is unchanged:

```powershell
python -m vicekrack video-production-export WORKFLOW_ID --purpose approved_preview
python -m vicekrack export-verify runtime/exports/PACKAGE_ID
```

## Text

- Cues are short phrases: at most **2 lines** of at most **26 characters** (`config/captions.json`).
  They break after sentences where possible and never span two beats.
- The words, their order and their qualifiers are exactly the script's narration beats, the same
  text that was spoken. Titles, on-screen text and directions are never captioned.
- Nothing is dropped or replaced silently. These are refused:
  - a word longer than one line (`caption_word_too_long`);
  - a character the burn-in font cannot draw, such as accented letters or emoji
    (`caption_unsupported_character`);
  - `<`, `>`, `-->` or control characters (`caption_unsafe_text`);
  - a line wider than the box when measured in pixels at render time (`caption_overflow`).
- WebVTT escapes `&`, `<` and `>`. SRT is plain text, and the characters that are unsafe in it are
  refused.

## Timing: what each method means

| Method | Source | Labelled |
|---|---|---|
| `provider_character_timestamps` | xAI's per-character `[start, end]` seconds saved with the speech job. Each cue starts at its first word's first character and ends at its last word's last character, then is held up to 0.6 s, never past the next cue or the narration. | `word_synchronized: false`, `cue_boundaries: provider_word_boundaries`. Phrase captions with provider-timed boundaries, not karaoke-style word highlighting. |
| `estimated_phrase` | Cues spread over the **measured** narration duration in proportion to their length. | `requires_manual_timing_review: true`. The burned-in box shows "CAPTION TIMING ESTIMATED - CHECK BEFORE APPROVAL". The quality report is `needs_review` (`caption_timing_estimated`), so approval requires the `needs_review_result` acknowledgment. |

Provider timestamps are validated before use. Any problem is refused, never repaired:

| Problem | Error code |
|---|---|
| The characters do not equal the spoken text | `caption_timing_text_mismatch` |
| Times are out of order | `caption_timing_out_of_order` |
| A time is negative | `caption_timing_negative` |
| A time is past the measured audio (50 ms tolerance), or the provider's duration differs by more than 250 ms | `caption_timing_out_of_range` |
| A time ends before it starts, or the data is malformed | `caption_timing_invalid` |

Every track is also checked cue by cue. Cues must be in order, must not overlap, must not be
negative, must last at least 0.4 s, and must end within both the narration and the 15-second video.

The audio is **never** stretched, truncated, regenerated or re-timed to fit the captions. Clip audio
stays muted, and the format stays 15 seconds.

## Rendering

Captions are burned only into **media-backed, narrated** previews, and the caption-free rendering
is unchanged:

- each cue is a transparent overlay, shown only while `start <= t < end`, so no two cues share a
  frame;
- white 56 px text on a near-black box (alpha 235), centred, with 60 px side margins;
- the box sits above the bottom title box (it ends at y = 1400; that box starts at 1420) and well
  below the top warning band. The layout is checked against both reserved areas, so draft and
  preview warnings and the "illustrative media" disclosure stay visible;
- the frame count, the 1080x1920 size and the audio are unchanged.

## Integrity, approvals and resume

- **Storage.** Tracks are stored in `runtime/captions/<cap-id>/`: `track.json` (contract
  `caption_track` 1.0), `captions.srt` and `captions.vtt`. They are written once and atomically.
  - The caption ID is derived from the content.
  - Every load re-checks the sidecars against the track (`captions_tampered`).
- **Workflow copies.** The workflow keeps its own copies in `captions/` and re-hashes them on every
  resume, submit and export (`captions_tampered`, which blocks the workflow).
- **Bindings checked:**

  | What must match | Error code |
  |---|---|
  | The captions were made from the same speech job and script | `captions_script_mismatch` |
  | The captions were timed against the same narration bytes | `captions_narration_mismatch` |
  | Captions are used together with Grok speech narration | `captions_need_speech_narration` |

- **Revision identity.** The track hash is part of the workflow ID and of the revision identity.
  The same clips and narration with different (or no) captions are a different video. It is
  rendered again and gets a fresh quality report. **Old approvals stop applying** and their export
  is refused.
- **Quality binding.** The production keeps the track and sidecars beside the rendered package. The
  quality report binds them (roles `captions`, `captions_srt` and `captions_vtt`) and checks that:
  - the sidecars are exactly what the track produces;
  - the manifest names this track;
  - the script and narration bindings hold;
  - no cue extends past the measured video or audio.
- **Resume.** Resume never re-renders a preview that was rendered before a crash (it is adopted),
  and never re-requests speech or video.

## Export

The package adds three files, each listed with its size and SHA-256 and bound by the quality
report:

- `captions/captions.json`, the track;
- `captions/captions.srt` and `captions/captions.vtt`, the sidecars.

The package manifest's `captions` section and the review page say plainly that the captions are
**burned into `media/preview.mp4`**. That is what the reviewer watched, and it cannot be turned off.
The SRT and WebVTT files are **optional sidecar copies** of the same cues.

`export-verify` checks the sidecars against the track and the manifest, and reports a missing or
extra caption file. The exported video is byte-identical to the reviewed one, and everything stays
`publishable: false`.

## Offline demo (no network, no API credits)

Needs FFmpeg (`python -m pip install -r requirements-render.txt`).

```powershell
python -m vicekrack video-production-demo --captions
python scripts/step47_demo.py
```

It runs in a fresh folder, `runtime/video-production-demo/captions-<time>-<id>/`, with sockets
blocked and **mock** providers.

The speech reply is the documented timestamped JSON envelope carrying **synthetic test audio** (tone
bursts) and **synthetic character timings**. It is **not a Grok voice and not real alignment**. The
review is simulated and labelled.

The demo:

- renders a playable captioned MP4 (`captioned-preview.mp4`);
- exports a verified package with SRT and WebVTT sidecars;
- writes representative frames to `frames/` (three cues, one frame after the narration, and one
  estimated-timing cue);
- proves that a timing change, another script and a changed sidecar are refused;
- proves that paid work is reused;
- proves that a caption change invalidates the earlier approval and that estimated timing is
  `needs_review`.

## Recovery

| Problem | What to do |
|---|---|
| `provider_timing_unavailable` | Use `--timing estimated` (labelled), or prepare a NEW speech job with `--with-timestamps` and approve that paid request yourself. |
| `caption_timing_*` | The saved provider timing cannot be trusted. Use `--timing estimated`. |
| `caption_word_too_long`, `caption_unsupported_character`, `caption_unsafe_text`, `caption_overflow` | Edit the script, then make a new production and speech job. The text is never altered automatically. |
| `captions_tampered` | Restore the exact files, or prepare the track again and start a new workflow. |
| `production_preview_changed` after a caption change | Expected. Review the new captioned video (or re-run the workflow you want as the current preview). |

## Limitations

- **The live xAI timestamp output has not been tested.** Only the documented format is used, with
  mocked replies. Real voices may place characters differently: check every cue in review.
- Caption timing marks phrase boundaries; words are not highlighted one by one.
- Captions are English-only (the preview font).
- Captions work only with Grok speech narration, not with a local recording (Step 45), because
  only a speech job ties the audio to the script's exact text.
- There are no per-platform styles, positions or translations, no music or SFX, and no publishing.
