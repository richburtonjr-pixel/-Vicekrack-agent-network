# Step 44: controlled video-production workflow

Step 44 joins the existing parts into one finite workflow that can be resumed. Step 43's Grok jobs,
controlled downloads, media renderer and production revisions are used unchanged, as are Step 36
quality binding, Step 37 review and Step 38 export. Nothing is duplicated:

```
approved story (selection run + record) or a completed production
  -> production        the existing Step 21 pipeline (or link a completed production)
  -> scene_plan        the production's verified scene plan, saved and hashed by the workflow
  -> jobs              four Step 43 Grok jobs are PREPARED (no request, no cost)
  -> generation        each job is submitted ONLY with its own consent phrase; status checks are bounded
  -> downloads         controlled Step 43 downloads into the workflow's media folder
  -> media_manifest    the four clips become a Step 42 media manifest
  -> preview_revision  the production's preview is re-rendered with the clips (older approvals stop applying)
  -> quality           a fresh, bound Step 36 quality report (a "fail" stops the workflow)
  -> review            waits for an explicit Step 37 HUMAN decision; the workflow never records one
  -> export            only `video-production-export` exports, through the unchanged Step 38 gates
```

Everything stays `publishable: false`, including approved exports. Draft labels are kept.
Packages built from generated footage carry an "ILLUSTRATIVE MEDIA" disclosure.

## Offline demo (no network, no API credits)

Needs FFmpeg (`python -m pip install -r requirements-render.txt`).

```powershell
python -m vicekrack video-production-demo
python scripts/step44_demo.py
```

Both commands do the same thing. Each run uses a fresh folder,
`runtime/video-production-demo/run-<time>-<id>/`, and never touches your real productions. Every
socket connection is blocked for the whole run, and the provider is a **mock**. Grok's clips are
replaced with **synthetic** test clips made locally by FFmpeg, so they show colour bars, not Grok's
visual quality. A placeholder (not a key) satisfies the `XAI_API_KEY` check and is removed again
afterwards.

The mock stories have fixed publish dates, so the demo runs on its own fixed clock, starting at
2026-10-04 13:00 UTC. This lets it work on any date. Folder names still use the real time.

The demo proves these behaviours and prints them under `proofs`:

- start and resume never submit;
- consent for another job is refused;
- a simulated timeout makes scene 2 uncertain, so the workflow pauses;
- a plain resubmit is refused, and a retry without `--acknowledge-duplicate-billing` is refused;
- the acknowledged retry runs;
- scene 3 is still pending on the first status check, so the workflow waits instead of polling;
- a resume during a held lock is refused;
- export before review is refused;
- a resume after export repeats nothing.

The review step is **simulated**. The reviewer is labelled `SIMULATED DEMO REVIEWER (not a person)`.
The demo records that review itself, not the workflow, and only inside the demo folder.

The output includes `video`, the path of the real rendered 1080x1920, 24 fps, 15-second MP4. It also
includes `export_package`, the path of the verified preview package.

## Real use (paid, explicit)

`XAI_API_KEY` must be set in your process environment. Never put it in files, commands or chat.

**1. Start.** Preparing the jobs costs nothing:

```powershell
python -m vicekrack video-production-start --selection SELECTION_RUN_ID --record RECORD_ID
python -m vicekrack video-production-start --production PRODUCTION_ID
```

Options:

- `--model grok-imagine-video-1.5-lite`, `--resolution 480p|720p|1080p` and `--audio-mode none|generated`;
- `--allow-draft-preview` for draft stories, which keep their warning.

The command prints the workflow ID (`vpw-…`), and it prints the four job IDs with each one's
consent phrase. Read each job first with `python -m vicekrack video-inspect JOB_ID`.

**2. Submit each scene.** Each submission is one paid request. The consent phrase must name that
exact job:

```powershell
python -m vicekrack video-production-submit WORKFLOW_ID --scene 1 --consent paid-generate:JOB_ID --allow-network
```

Repeat for scenes 2, 3 and 4. Nothing else ever submits.

**3. Collect the clips.** Resume makes at most 4 status checks and 4 downloads per run, and only
with `--allow-network`:

```powershell
python -m vicekrack video-production-resume WORKFLOW_ID --allow-network
```

If it reports `waiting_for_provider`, wait a while and run it again. There is no background
polling. Once all four clips are in, the same resume:

- writes the media manifest;
- re-renders the production preview;
- runs a quality report;
- stops at `waiting_for_review`.

**4. Human review** uses the Step 37 commands. `inspect` prints these exact lines:

```powershell
python -m vicekrack review-list PRODUCTION_ID
python -m vicekrack review-record PRODUCTION_ID --report REPORT_ID --binding DIGEST --decision approved_for_preview --reviewer "YOUR NAME"
python -m vicekrack video-production-resume WORKFLOW_ID
```

**5. Export:**

```powershell
python -m vicekrack video-production-export WORKFLOW_ID --purpose approved_preview
```

`--purpose review_copy` is also allowed before approval. It does not complete the workflow.

**Inspect at any time.** These commands are read-only:

```powershell
python -m vicekrack video-production-inspect WORKFLOW_ID
python -m vicekrack video-production-list
```

`inspect` shows the status and every stage, scene, job and output. It also shows any integrity
problem and the exact next commands (`next`).

## Statuses and recovery

| Status | Meaning | What to do |
|---|---|---|
| `waiting_for_consent` | Jobs are prepared but not submitted | `video-production-submit … --consent paid-generate:JOB_ID --allow-network` per scene |
| `uncertain_submission` | A submit timed out or got an unclear reply; it **may have been billed** | First check your xAI usage. Then, only if you accept a possible second charge: `video-production-submit WORKFLOW_ID --scene N --consent paid-generate:JOB_ID --allow-network --retry-uncertain --acknowledge-duplicate-billing` |
| `waiting_for_provider` | Clips are still generating or not yet downloaded | `video-production-resume WORKFLOW_ID --allow-network` later |
| `failed` with `provider_job_failed` | xAI reports a scene job failed or expired | `video-production-retry-scene WORKFLOW_ID --scene N --model grok-imagine-video-1.5-lite` (or `--resolution`), then submit the NEW job with its own consent. Each scene can be replaced at most twice |
| `failed` (other code) | A stage failed (for example an invalid download) | Fix the cause and `video-production-resume WORKFLOW_ID`. A stage that fails 3 times blocks the workflow |
| `quality_failed` | The quality report failed | This preview cannot be approved or exported; start a new workflow after fixing the cause |
| `waiting_for_review` | A human decision is needed | Use the review commands above |
| `review_rejected` | The latest decision is `rejected` or `changes_requested` | Record a new decision, or start over |
| `ready_to_export` | There is a current approval | `video-production-export WORKFLOW_ID --purpose approved_preview` |
| `exported` | Done | Nothing; resuming again repeats nothing |
| `blocked` | Limits reached, or saved inputs changed | Start a new workflow (see below) |

**An interrupted command** (crash, Ctrl+C or a closed terminal) is safe to re-run with
`video-production-resume`:

- completed stages are never repeated;
- paid jobs are never resubmitted;
- a preview rendered just before the crash is adopted, not rendered twice.

**`workflow_busy`** means another command is running this workflow. Wait for it to finish.

**Changes are refused, never repaired silently.** Resume, submit and export stop with an error if
any of these changed after the workflow used it:

- `config/video-production.json` (`workflow_config_changed`);
- the production's scene plan (`production_changed`);
- the saved scene plan (`scene_plan_tampered`);
- a downloaded clip (`media_tampered`);
- the media manifest (`media_manifest_tampered`);
- the production's current preview (`production_preview_changed`).

The status turns `blocked` only when the error happens inside a stage. In each case, start a new
workflow.

## Limits (`config/video-production.json`)

| Limit | Value |
|---|---|
| Workflow steps (`max_steps`) | 80 |
| Attempts per stage | 3 |
| Status checks per resume | 4 |
| Downloads per resume | 4 |
| Replacements per scene | 2 |
| Trace entries kept | 200 |

The allowed models, resolutions and audio modes are also listed there.

## State and security

The state lives in `runtime/video-production/<workflow_id>/`, which Git ignores:

- `workflow.json` (contract `video_production_workflow` 1.0);
- `scene-plan.json`, `media-manifest.json` and `media/`;
- the `workflow.lock` OS lock.

The state is:

- schema-validated and self-hashed;
- replaced atomically under the lock;
- refused if it is unreadable, edited, or contains credential-like text or signed download URLs.

It records:

- the configuration and input hashes;
- stage outcomes and timestamps;
- job and provider request IDs, scene numbers and media hashes;
- fixed error codes only.

API keys, authentication headers, environment snapshots, raw provider exceptions and signed
download URLs are never stored or printed. Signed URLs stay inside the Step 43 job records.
Job records stay in the shared `runtime/video-jobs/`, so the Step 43 commands and workflows share
the same duplicate protection.

## Limitations

- Live xAI behaviour has not been tested with a paid request. Only mocked provider responses are
  tested.
- The demo's clips are synthetic colour bars, not Grok output.
- Assembly mutes source audio. There is no automatic voice, music or SFX mixing.
- Fallback media (stock, stills, motion graphics) is not chosen automatically when generation
  fails. Use `retry-scene` or the existing local text-card preview.
- Nothing is uploaded or published.
