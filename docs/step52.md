# Step 52: One approval for all clips

> **Numbering.** "Step 52" is the development step in [the roadmap](roadmap.md), not a GitHub
> pull-request number.

Video Studio used to show one paid box per scene (four boxes for a 15-second video). Now it shows one:

```
Generate all 4 clips (15 s of video in total, about $0.30). [x] I approve these 4 paid xAI clip requests
[Approve and send (paid)]
```

- The estimate uses the per-second price of each clip's model (as of 2026-10-13: lite $0.02/s,
  standard $0.08/s). It is only an estimate; xAI bills what it bills.
- The consent phrase is `paid-generate-all:vpw-...`, specific to this workflow, and checked by the server.
- The clips are still sent one after another through the same per-job path as before (each has its own
  saved request and job record). Sending **stops** at the first clip whose outcome is unknown or that
  fails, so a problem never fans out; that clip then shows the existing "may bill twice" box and the
  rest stay waiting.
- A second click, or a replayed request, cannot pay again: once sent, nothing is waiting for approval
  (`nothing_to_generate`).
- With only one clip waiting (for example after replacing a failed scene) the single box appears as before.

Files: `vicekrack/hq/studio.py` (`workflow_submit_all`, `VIDEO_PRICES`), `vicekrack/hq/studio_api.py`
(`/api/studio/workflow/submit-all`), `vicekrack/hq/static/studio.js`.
