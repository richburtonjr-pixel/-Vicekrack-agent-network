"""Offline Step 45 demo: same as `python -m vicekrack video-production-demo --narrated`.

No network, no API credits: a mock Grok provider, synthetic local clips and a synthetic narration
WAV (tone bursts, not a voice). Produces a real, playable narrated MP4. Needs ffmpeg
(`python -m pip install -r requirements-render.txt`).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vicekrack.video_production_demo import run_demo  # noqa: E402


def main():
    summary = run_demo(narrated=True)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
