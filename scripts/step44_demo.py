"""Offline Step 44 demo: same as `python -m vicekrack video-production-demo`.

No network, no API credits: a mock Grok provider and synthetic local clips. Needs ffmpeg/ffprobe.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vicekrack.video_production_demo import run_demo  # noqa: E402


def main():
    summary = run_demo()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
