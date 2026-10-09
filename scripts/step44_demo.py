"""Offline Step 44 demo. No network and no API credits."""
import json
from vicekrack.video_production import VideoProduction


def main():
    state = VideoProduction().demo()
    print(json.dumps({"workflow_id": state["workflow_id"], "publishable": state["publishable"],
                      "export": state["stages"]["export"], "live_generation": "unverified"}, indent=2))


if __name__ == "__main__":
    main()
