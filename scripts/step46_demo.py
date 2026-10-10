"""Offline Step 46 demo: same as `python -m vicekrack video-production-demo --speech`.

No network, no API credits: mock xAI speech and video providers, SYNTHETIC test audio (not a Grok
voice) and synthetic clips. Needs ffmpeg/ffprobe (requirements-render.txt).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vicekrack.speech_demo import run_speech_demo  # noqa: E402


def main():
    print(json.dumps(run_speech_demo(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
