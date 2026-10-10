"""Offline Step 47 demo: same as `python -m vicekrack video-production-demo --captions`.

No network, no API credits: mock xAI speech and video providers, SYNTHETIC test audio and timings
(not a Grok voice), synthetic clips. Needs ffmpeg/ffprobe (requirements-render.txt).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vicekrack.caption_demo import run_caption_demo  # noqa: E402


def main():
    print(json.dumps(run_caption_demo(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
