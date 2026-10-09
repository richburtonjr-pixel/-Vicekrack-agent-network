# Step 43: explicit Grok video jobs and local assembly

This implements the xAI asynchronous video request boundary and repairs Step 42's
missing media renderer. It does not publish videos or run paid requests automatically.
No new dependency is required beyond `requirements-render.txt`.

## Offline setup and demonstration

```powershell
python -m pip install -r requirements-render.txt
python scripts/step42_demo.py
python scripts/run_tests.py
$env:RUN_LOCAL_RENDER_TESTS="1"
python scripts/run_tests.py
```

The demo writes synthetic image/video fixtures, a scene plan, a media manifest, and
a decoded 1080x1920, 24fps, 15-second MP4 under `runtime/step42-demo/`. These are
test colors, not a demonstration of Grok's visual quality. No network or API credits
are used. Existing text-card rendering remains available without `--media`.

## Explicit paid generation

Use a validated local scene plan. The demo produces one at the path below. Configure
`XAI_API_KEY` securely in your local process environment; `.env.example` is only a
template and this application does not automatically load `.env`. Never paste keys
into prompts, source files, Git, or terminal commands saved in shared transcripts.

```powershell
python -m vicekrack video-prepare runtime/step42-demo/scene-plan.json 1 --resolution 720p
python -m vicekrack video-inspect JOB_ID
python -m vicekrack video-submit JOB_ID --allow-network --consent paid-generate:JOB_ID
python -m vicekrack video-status JOB_ID --allow-network
python -m vicekrack video-download JOB_ID --allow-network --media-root runtime/grok-media
```

Replace `JOB_ID` with the ID returned by prepare. Read its prompt, duration, model
and resolution before submitting. Each submission is a paid scene, not a free chat
subscription action. Repeat status manually if pending; it makes one GET request and
never submits again. Repeat prepare/submit/status/download for scene numbers 2, 3, 4.
Preparing the same request again returns the existing record without resetting it.
`--model grok-imagine-video-1.5-lite` selects the alternative supported model.

```powershell
python -m vicekrack video-manifest runtime/step42-demo/scene-plan.json JOB_1 JOB_2 JOB_3 JOB_4 --media-root runtime/grok-media --output runtime/grok-media/manifest.json
python -m vicekrack render-preview runtime/step42-demo/scene-plan.json --media runtime/grok-media/manifest.json --media-root runtime/grok-media
```

Optional `--narration path/to/local.wav` uses the existing bounded local narration
contract. `video-prepare --audio-mode generated` requests provider sound in the source
clip, but **assembly still mutes source audio**. There is no automatic speech/music
mixing. The default requests silent footage. Draft plans still require
`render-preview --allow-draft-preview` and retain their visible draft warning.

## Recovery and boundaries

Job JSON lives under ignored `runtime/video-jobs/`. Writes use staging, fsync and atomic
replacement; one OS lock serializes video operations. An interrupted submission is
reported as uncertain. Timeouts, malformed acknowledgments and unknown responses never
automatically spend credits again. If you deliberately accept possible duplicate billing:

```powershell
python -m vicekrack video-submit JOB_ID --allow-network --consent paid-generate:JOB_ID --retry-uncertain
```

At most ten explicit attempts are allowed. Known provider IDs are checked, never
resubmitted. This is not exactly-once execution. Keep the runtime directory: deleting
records also deletes local duplicate protection. Corrupt/legacy unfinished job records
are refused, not reset. Do not delete uncertain jobs to bypass that protection.

Downloads allow only HTTPS `vidgen.x.ai` on the standard port; redirects and ambient
proxies are disabled. Credentials go only to the fixed xAI API endpoint. Downloads
have byte/time bounds and undergo real decoding and dimension/duration validation.
Existing media files are not overwritten. A crash after media was saved but before its
job record was updated requires manual inspection; the command refuses an overwrite.

Generated media is illustrative, not factual evidence or verified rights clearance.
The local renderer snapshots and hashes source bytes, checks assignments and trim ranges,
adds overlays and preview labels, discards clip sound, verifies the final package, and
keeps `publishable: false`. Saved prompts, signed download URLs and outputs can contain
sensitive content. Runtime files stay out of Git; CLI output excludes signed URLs.

## Saved production integration

For a saved production, prepare the four jobs using **that production's saved scene
plan**, rather than the demo plan. After downloading and building its media manifest:

```powershell
python -m vicekrack video-render-production PRODUCTION_ID --media runtime/grok-media/manifest.json --media-root runtime/grok-media
python -m vicekrack quality-report PRODUCTION_ID
```

The command requires a completed production, unchanged configuration and source artifacts,
and fresh evidence. It takes the existing production lock, renders locally, and atomically
updates the current preview reference only after package verification. Previous packages
remain on disk. Old reports/approvals become stale because their artifact/state bindings
no longer match. The new quality report binds the media provenance manifest too; follow
the existing [review](review.md) and [export](export.md) commands using that new report.
No approval is created automatically. Duplicate attachment of the same manifest is refused.
A failed render leaves the previous production intact; a crash before pointer commit may
leave an unused preview package, never an automatically approved replacement.

Official API reference checked for this implementation:
[xAI video generation](https://docs.x.ai/developers/model-capabilities/video/generation).
The live paid API was not exercised during development.
