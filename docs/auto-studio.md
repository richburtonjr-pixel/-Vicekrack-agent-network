# Auto Studio: approve once, get a finished video (Step 54)

> **Numbering.** "Step 54" is the development step in [the roadmap](roadmap.md), not a GitHub
> pull-request number.

## Why the old videos were 3-5 second clips

The earlier pipeline (Video Studio, Steps 43-53) writes every video as a **15-second short with four
beats** (about 3, 4, 5 and 3 seconds) and asks xAI for one clip per beat. xAI can make clips of up to
**15 seconds**, and the clips were never stitched together automatically (until Step 53 they could not
even be downloaded). Auto Studio replaces that path for new videos; the old Video Studio still works for
existing productions.

## How you use it

1. **Research** -> type a topic -> approve the research (as before).
2. On a story card press **Make video**.
3. Pick the **length** (15 s / 30 s / 60 s / 2 min), the **type** (News report / Cinematic entertainment /
   Social media short), the **audio** (Professional narrator / Character dialogue / Narration and
   dialogue) and **background music** on or off.
4. **Get the exact estimate (free)** shows the expected cost and the most it can ever spend.
5. Tick the box and press **Approve and make the video**. That is the only paid approval.
6. Watch it happen in **Finished videos**. When it says **Ready to watch**, press play or **Download MP4**.
   Every finished video stays in the library, also after a restart.

You can press **Stop** at any time (nothing new is sent). A video that fails shows what went wrong and
a **Retry** button (it keeps every finished part and asks you to approve a little more spending).

## What the agents do on their own

```
Script (Grok)  ->  Voices (xAI text to speech, one request per line, per speaker)
  -> Timing (each scene's clip is sized to its voiceover: 4-15 s; a longer scene becomes two clips)
  -> Scene clips (xAI video, sent together, checked every few seconds, downloaded and checked;
     a failed or timed-out clip is retried automatically, at most 2 times)
  -> Music (an original instrumental bed composed by ViceKrack with FFmpeg's synthesizer)
  -> Assembly (FFmpeg: ViceKrack intro, clips in order with short fades, word-timed captions,
     scene titles, the story's label, an "AI-generated visuals" tag, outro with sources;
     voices at their exact times, music ducked under speech, loudness -14 LUFS)
  -> Check the file (see below)  ->  Finished videos
```

If the voices run too long for the chosen length, the script is rewritten shorter automatically (at
most twice). Speech is never cut, sped up or rewritten after it is recorded.

## The file check (nothing is "done" until the MP4 itself passes)

| Check | What is measured |
|---|---|
| Decodes | The whole MP4 decodes without errors |
| Format | 1080x1920, 30 fps, H.264 video, AAC 48 kHz audio |
| Length | Matches the timeline and the chosen length (within 12%) |
| Scene order | A frame from the middle of each scene in the finished video matches that scene's own clip better than any other clip |
| No overlap | Spoken lines never overlap |
| Voices present | Every spoken line is audible at its planned time |
| Soundtrack / music | The audio is not silent; music plays under the intro |

## Costs (xAI prices as of 2026-10-13; lite model, 720p, the default)

| Length | Scenes | Expected | At most (cap) |
|---|---|---|---|
| 15 s | 2 | about $0.43 | $0.88 |
| 30 s | 3 | about $0.82 | $1.31 |
| 60 s | 7 | about $1.91 | $3.06 |
| 2 min | 13 | about $3.93 | $6.29 |

Video is $0.02/s at 480p and $0.03/s at 720p on `grok-imagine-video-1.5-lite` ($0.08 and $0.14 on the
full model). Voices are $15 per million characters (under a cent per video). The script is one Grok
request (about a cent). The estimate is shown before you approve; the cap is enforced before every paid
request, and requests whose outcome is unclear are counted as spent.

## Settings

`config/auto-studio.json`: lengths, types, audio styles, stock voices (narrator `rex`; characters
`eve`, `ara`, `leo`, `sal`), video model and resolution (lite, 720p), timing, mix levels, prices and
limits (retries, cap multiplier, polling). No new accounts or keys: it uses your existing xAI key, and
FFmpeg and Pillow from `requirements-render.txt`.

## Honesty and safety

- The script may only use the story's checked claims; unconfirmed claims must be said as reported, and
  rumor-lane videos carry their label on every frame.
- Every video says its visuals are AI-generated, its voices are AI text to speech, and lists its sources.
- Music is original (generated locally); no downloaded songs.
- Nothing is published. Paid requests happen only inside an approved cap.

## Files

| File | What |
|---|---|
| `vicekrack/autostudio/engine.py` | prepare, approve, stop, retry and every automatic step |
| `vicekrack/autostudio/planner.py` | scenes per length, word budgets, timeline from real voice lengths, estimate |
| `vicekrack/autostudio/script.py` | the Grok script writer and its rules |
| `vicekrack/autostudio/voice.py` | xAI text to speech per line with word timings |
| `vicekrack/autostudio/clips.py` | xAI video clips: send, check, download, verify |
| `vicekrack/autostudio/music.py` | original music beds |
| `vicekrack/autostudio/assemble.py` | FFmpeg assembly |
| `vicekrack/autostudio/verify.py` | checks on the finished file |
| `vicekrack/hq/static/videos.js` | the Finished videos screen |

Saved in `runtime/auto-studio/asp-*/` (state, script, voices, clips, final.mp4, thumbnail, scene stills).

## Limitations

- xAI's lip-synced native audio is not used yet (`native_audio: false`): voices come from text to speech
  so captions can be timed exactly. It can be turned on later per type.
- Characters in dialogue use stock xAI voices with everyday names; the eight HQ agents are not used as
  on-screen characters yet.
- Your own licensed music tracks are not supported yet (only ViceKrack's original beds).
