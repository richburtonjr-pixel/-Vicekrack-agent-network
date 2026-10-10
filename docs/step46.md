# Step 46: Grok-generated narration (xAI text to speech)

> **Numbering.** "Step 46" is the development step in [the roadmap](roadmap.md). It is not a GitHub
> pull-request number: PR #45 was the Step 44 repair and PR #46 delivered Step 45.

Step 46 turns the narration of an existing, validated ShortScript into spoken audio with xAI's
text-to-speech API. That audio then becomes the narration of the [Step 44](step44.md)
/ [Step 45](step45.md) video-production workflow.

Speech is a **separate** component. Video generation, the workflow, local narration, quality
checks, review and export are reused unchanged:

```
validated ShortScript (a file, or a completed production's saved script)
  -> speech-prepare   offline: exact spoken text + voice/settings, bound to the script's SHA-256
  -> speech-inspect   read the text, voice, settings and consent phrase
  -> speech-submit    PAID, one request, explicit consent; intent saved before sending
  -> local conversion managed 16-bit PCM WAV, metadata stripped, duration measured
  -> video-production-start --production P --speech JOB
                      the script binding is checked against the production's saved script
  -> clips (reused if already paid) -> narrated preview -> fresh quality -> human review -> export
```

## Commands

`XAI_API_KEY` must be set in your process environment. Never put it in files, commands or chat.

**1. Prepare (offline, free).** Use either the production's saved script (recommended: the binding
then matches by construction) or a ShortScript file:

```powershell
python -m vicekrack speech-prepare --production PRODUCTION_ID
python -m vicekrack speech-prepare --script examples/short-script-gta.json --voice leo
```

| Option | Values |
|---|---|
| `--voice` | A stock voice from `config/speech.json`. The default is `eve`; others include `ara`, `leo`, `rex` and `sal`. Custom or cloned voices are refused. |
| `--language` | Default `en`. Also `auto` and the other documented codes. |
| `--codec` | `wav` (default, decoded in Python) or `mp3` (needs FFmpeg from `requirements-render.txt`). |
| `--sample-rate` | `8000`, `16000`, `22050`, `24000` (default), `44100` or `48000`. |
| `--bit-rate` | mp3 only: `32000` to `192000` (default `128000`). |

Speed is never changed: no speed parameter is sent, so the provider's default (1.0) is used.

**2. Review the request.** Read the exact text before paying:

```powershell
python -m vicekrack speech-inspect SPEECH_JOB_ID
python -m vicekrack speech-list
```

How the spoken text is built:

- It is the script's narration beats, in order, one beat per line, with the words and qualifiers
  unchanged.
- Titles, on-screen text, visual directions, sound cues and claim IDs are never included.
- If a beat contains `[`, `]`, `<` or `>`, prepare is refused (`speech_text_has_markup`), because
  xAI reads those as speech directions. Scripts are never rewritten automatically.

The same script with the same settings always gives the same job ID, so preparing again never
resets a paid job. Any change to the text or a setting gives a **new job**, which needs its own
review and its own consent.

**3. Submit (PAID).** This makes one request, with consent for this exact job:

```powershell
python -m vicekrack speech-submit SPEECH_JOB_ID --consent paid-speech:SPEECH_JOB_ID --allow-network
```

**4. Use it in the workflow.** Only a `completed` job can be used:

```powershell
python -m vicekrack video-production-start --production PRODUCTION_ID --speech SPEECH_JOB_ID
python -m vicekrack video-production-start --selection RUN_ID --record RECORD_ID --speech SPEECH_JOB_ID
```

After that, everything works as in Steps 44/45:

- submit each scene with its own consent;
- resume;
- record a human review;
- export.

`video-production-inspect` shows `narration.speech`: the job, voice, language and script hash.

## Speech job statuses and recovery

| Status | Meaning | What to do |
|---|---|---|
| `prepared` | Ready, nothing sent | Inspect it, then `speech-submit … --consent paid-speech:JOB --allow-network` |
| `submitting` | The intent was saved, then the command was interrupted | `speech-recover JOB`. It marks the job `uncertain` and **never resends** |
| `uncertain` | Timeout, connection reset, server error or unreadable reply; **may have been billed** | First check your xAI usage. Only if you accept a possible second charge: `speech-submit JOB --consent paid-speech:JOB --allow-network --retry-uncertain --acknowledge-duplicate-billing` |
| `rejected` | xAI answered 400/401/403/404/413/422/429, so nothing was generated (the status number is kept) | Fix the cause (key, voice, rate limit), then submit again with consent |
| `received` | The audio was saved, but local conversion has not finished (for example, an mp3 without FFmpeg) | Install `requirements-render.txt`, then `speech-recover JOB`. This converts the saved audio locally and sends no request |
| `completed` | A usable, managed WAV of at most 15 s | `video-production-start … --speech JOB` |
| `too_long` | The speech is longer than 15 s. The audio is **kept**, never cut or sped up | Shorten the script's narration and prepare a **new** job. Nothing is regenerated automatically |
| `invalid_audio` | The reply was malformed or all silence | Prepare a job with another voice or codec, or retry with `--retry-uncertain --acknowledge-duplicate-billing` (PAID) |

Only `prepared` and `rejected` jobs can be submitted without the retry flags. If xAI could not be
reached at all (DNS failure or connection refused), the job keeps its earlier status. A job allows
at most 5 attempts (`config/speech.json`).

## How the audio is handled

Request limits:

- one HTTPS request to `https://api.x.ai/v1/tts`;
- no redirects, no proxies and no retries;
- 90 s total;
- a response of at most 16 MB.

The endpoint is synchronous and documents no request ID. So `provider_request_id` is always
`null`, and nothing is polled.

The reply is stored as received in `provider-audio-N.wav|mp3`, with its hash. It is never exported.

It is then converted locally into the existing narration format: a **canonical 16-bit PCM WAV**
(`narration.wav`). Conversion keeps only the format and samples, so metadata chunks (LIST/INFO,
ID3 tags) are removed. WAV is parsed in Python. MP3, or a WAV encoding the parser cannot read, is
decoded with the local FFmpeg.

**Duration** is measured from the samples:

| Speech | Result |
|---|---|
| 15 s or shorter | Usable. The workflow pads it with silence to the 15-second preview (Step 45 rule). |
| Longer than 15 s | Kept, marked `too_long` and refused by the workflow (`speech_narration_too_long`). It is not truncated, not sped up and not paid for again. |

## Binding and integrity

- **Script binding.** A job records the canonical SHA-256 of the script it was made from. The
  workflow compares it with the production's actual saved script, and stops with
  `speech_script_mismatch` if they differ:
  - at start, for `--production`;
  - right after the production stage, for `--selection`. This happens before any video job is
    prepared, so before any paid video submission is possible;
  - again on every resume, submit and export.
- **Audio integrity.**
  - The job's `narration.wav` is re-hashed when it is used (`speech_audio_tampered`).
  - The workflow keeps its own managed copy, re-hashed on every command (`narration_tampered`, as
    in Step 45).
- **Clip reuse.** Paid clips are reused when the story and settings match. A speech workflow started
  after a silent run reuses its four paid clips, after checking each one's hash, and makes no new
  video request.
- **Fresh checks and approvals.** The narrated video is a new revision. It gets a fresh quality
  report and needs its own human approval. An approval of the silent (or any other) version no
  longer authorizes anything: its export is refused.
- **Never published.** Everything stays `publishable: false`. Nothing is approved, uploaded or
  published automatically.

## Offline demo (no network, no API credits)

Needs FFmpeg (`python -m pip install -r requirements-render.txt`).

```powershell
python -m vicekrack video-production-demo --speech
python scripts/step46_demo.py
```

It runs in a fresh folder, `runtime/video-production-demo/speech-<time>-<id>/`, with every socket
connection blocked. The speech and video providers are **mocks**.

The "speech" is **SYNTHETIC TEST AUDIO**: tone bursts written by Python, with an extra metadata
chunk. It is **not a Grok voice sample**. The review is **simulated** and labelled
`SIMULATED DEMO REVIEWER (not a person)`.

The demo proves (see `proofs` in the output):

- prepare is offline, and the spoken text is the beats;
- wrong consent is refused, and so is a submit without `--allow-network`;
- a timeout leaves the job uncertain. A plain resubmit is refused, and so is a retry without the
  duplicate-billing acknowledgment. The acknowledged retry converts the audio, with its metadata
  stripped;
- a 16.5 s reply is kept but refused as too long;
- a job made from another script is refused;
- changed managed audio is refused;
- the narrated workflow reuses the four paid clips, with no new video request;
- the silent approval no longer authorizes export;
- after the simulated review, the verified export carries 15 s of AAC audio: loud during the
  narration, silent after it.

The output's `video` is the real 1080x1920, 15-second narrated MP4, and `export_package` is the
verified package.

## Limitations

- **The live xAI speech API has not been called.** Only mocked responses are tested, so the real
  voice quality, the exact WAV variant returned and the error behaviour are unverified.
- The docs list no rate limits or prices for this endpoint, so the tool does not estimate cost.
- Narration longer than 15 s is refused rather than extending the video. MP3 decoding can add a
  few milliseconds of encoder padding, so prefer the default `wav`.
- One narration track, starting at 0 s. There is no per-scene timing, captions, music, SFX, voice
  cloning or lip sync.
- The export page does not yet carry a separate "AI-generated voice" banner. The workflow's
  `narration.output` and the speech job's notice say it.
- Voice and narration rights are not verified, and nothing is uploaded or published.
