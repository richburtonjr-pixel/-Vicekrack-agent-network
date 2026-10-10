"""Step 48: offline Video Studio demo (`python -m vicekrack hq-serve --studio-demo`).

The HQ runs on a FRESH demo folder (runtime/studio-demo/run-<time>-<id>/), so demo productions,
approvals and exports never mix with real ones. For the whole server lifetime:

- every outbound socket connection is blocked (the server only accepts local connections);
- XAI_API_KEY is replaced by a placeholder (not a key), so a real key is not even in the process;
- the xAI video and speech APIs are MOCKS: video clips are synthetic colour-bar clips made locally by
  FFmpeg, and "speech" is SYNTHETIC TEST AUDIO (tone bursts), not a Grok voice;
- the clock is the fixed demo clock that matches the dated mock stories.

Rendering, quality checks, review gates and exports are the real ones. One mock story is selected
and produced (offline mock Creator, real local renderer) so the Studio has a production to work on.
"""

import base64
import json
import secrets
import tempfile
from datetime import datetime, timezone

from ..orchestrator import ROOT

NOTICE = ("STUDIO DEMO - mock xAI providers, synthetic clips and synthetic test audio (not a Grok voice), no network, "
          "no credits. Demo data lives in its own folder; reviews here are demo reviews and never touch real "
          "productions.")


class DemoSpeech:
    """Mock POST /v1/tts: raw synthetic audio, or the documented timestamped JSON envelope."""

    def __init__(self, folder, seconds=10.5):
        self.folder, self.seconds, self.calls = folder, seconds, 0

    def __call__(self, body, *, timeout_seconds, max_bytes):
        from ..speech_demo import synthetic_speech_wav
        self.calls += 1
        audio = synthetic_speech_wav(self.folder, self.seconds, body["output_format"]["sample_rate"])
        if not body.get("with_timestamps"):
            return {"content_type": "audio/wav", "audio": audio}
        text, span = body["text"], self.seconds - 0.5
        times = [[round(0.2 + span * i / len(text), 3), round(0.2 + span * (i + 1) / len(text), 3)]
                 for i in range(len(text))]
        envelope = {"audio": base64.b64encode(audio).decode("ascii"), "content_type": "audio/wav",
                    "duration": self.seconds, "audio_timestamps": {"graph_chars": list(text), "graph_times": times}}
        return {"content_type": "application/json", "audio": json.dumps(envelope).encode("utf-8")}


def demo_studio(stack, output=None):
    """(Studio, folder) with mocks and safety contexts entered on `stack` (left when the server stops)."""
    from ..production import Pipeline
    from ..video_production_demo import (MockGrok, _story, demo_clock, network_blocked, placeholder_credential,
                                         review_time)
    from .studio import Studio
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = ROOT / "runtime" / "studio-demo" / f"run-{stamp}-{secrets.token_hex(3)}"
    output.mkdir(parents=True, exist_ok=False)
    folder = output.resolve()
    clock = demo_clock()
    stack.enter_context(network_blocked())
    stack.enter_context(placeholder_credential())
    stack.enter_context(review_time(clock))
    clips = stack.enter_context(tempfile.TemporaryDirectory(prefix="vk-studio-demo-"))
    from ..preview import dependencies
    dependencies()                                       # fail early, clearly, without FFmpeg/Pillow
    run_id, record_id = _story(folder, clock)
    Pipeline(root=folder, clock=clock).produce(run_id, record_id)
    studio = Studio(folder, demo=True, clock=clock, notice=NOTICE,
                    video_transport=MockGrok(clips, fail_first_post_numbers=(), pending_once=("demo-request-3",)),
                    speech_transport=DemoSpeech(clips))
    return studio, folder
