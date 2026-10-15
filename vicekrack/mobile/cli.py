"""Mobile Command Center commands (the double-click files in scripts/windows/ run these for you).

python -m vicekrack mobile-setup        PIN, daily cap, private Tailscale address, turn on the front door
python -m vicekrack mobile-save-key     save the xAI key in Windows Credential Manager
python -m vicekrack mobile-pair         show a 6-digit code to pair a phone (10 minutes)
python -m vicekrack mobile-start        start the HQ (Studio, with the saved key) AND the mobile server
python -m vicekrack mobile-serve        the mobile server only (the HQ must already be running)
python -m vicekrack mobile-phones       list paired phones   |   mobile-unpair-all   unpair every phone
python -m vicekrack mobile-status       settings (no secrets) and the last phone actions
"""

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..errors import NetworkError
from . import security
from .store import MobileStore

MOBILE_PORT, HQ_PORT = 8770, 8765
TAILSCALE_PATHS = (r"C:\Program Files\Tailscale\tailscale.exe", "/usr/bin/tailscale", "/usr/local/bin/tailscale",
                   "/Applications/Tailscale.app/Contents/MacOS/Tailscale")


def _say(text=""):
    print(text, flush=True)


def tailscale_exe():
    found = shutil.which("tailscale")
    if found:
        return found
    return next((p for p in TAILSCALE_PATHS if Path(p).exists()), None)


def tailscale_identity(run=subprocess.run):
    """(private https address, owner's Tailscale login) from `tailscale status --json`, or (None, None)."""
    exe = tailscale_exe()
    if not exe:
        return None, None
    try:
        result = run([exe, "status", "--json"], capture_output=True, text=True, timeout=20)
        status = json.loads(result.stdout)
        me = status["Self"]
        host = (me.get("DNSName") or "").rstrip(".").lower() or None
        user = (status.get("User") or {}).get(str(me.get("UserID"))) or {}
        return host, (user.get("LoginName") or "").lower() or None
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None, None


def _ask_pin():
    for _ in range(3):
        first = getpass.getpass("Choose a PIN for paid phone approvals (4-8 digits, typing is hidden): ")
        if not security.PIN.match(first):
            _say("The PIN must be 4 to 8 digits.")
            continue
        if getpass.getpass("Type the same PIN again: ") != first:
            _say("Those did not match. Try again.")
            continue
        return security.hash_pin(first)
    raise NetworkError("pin_not_set", "The PIN was not set.")


def setup(store, port):
    _say("ViceKrack Mobile Command Center setup\n")
    settings = store.settings()
    if settings.get("pin") and input("A PIN is already set. Change it? (y/N): ").strip().lower() != "y":
        pin = settings["pin"]
    else:
        pin = _ask_pin()
    cap_text = input(f"Daily limit for phone approvals in dollars [{settings['daily_cap_usd']:.2f}]: ").strip()
    cap = settings["daily_cap_usd"]
    if cap_text:
        try:
            cap = round(float(cap_text.lstrip("$")), 2)
        except ValueError:
            _say("Not a number; keeping the current limit.")
        if not 0 <= cap <= 100:
            _say("Keeping the limit between $0 and $100.")
            cap = min(max(cap, 0), 100)
    host, login = tailscale_identity()
    if host and login:
        _say(f"\nTailscale found. Private address: https://{host}\nOwner account: {login}")
    else:
        _say("\nTailscale was not found or is not signed in. Install it, sign in, then run this setup again.\n"
             "Everything else is saved; the phone just can't connect from outside yet.")
    store.save_settings(pin=pin, daily_cap_usd=cap, remote_host=host, owner_login=login)
    if host and login:
        exe = tailscale_exe()
        _say("\nTurning on the private front door (Tailscale Serve) for this PC only…")
        result = subprocess.run([exe, "serve", "--bg", str(port)], capture_output=True, text=True, timeout=60)
        _say((result.stdout or "").strip())
        if result.returncode != 0:
            _say((result.stderr or "").strip())
            _say("\nIf Tailscale shows a link to enable HTTPS, open it, click Enable, then run this setup again.")
        else:
            _say(f"\nOn your iPhone (Tailscale on), open: https://{host}")
    _say("\nSetup saved. Next: double-click \u201cPair my iPhone\u201d.")
    return 0


def pair(store):
    code = security.new_pairing_code(store)
    _say("\n  Pairing code for your iPhone:\n")
    _say(f"        {code[:3]} {code[3:]}\n")
    _say("  Type it in the ViceKrack app on your phone within 10 minutes.")
    _say("  It works once. Close this window when you're done.\n")
    return 0


def _build(store, port, hq_port):
    from .gateway import Gateway
    from .hq_client import HQClient
    from .server import MobileServer
    return MobileServer(Gateway(store, HQClient(hq_port), port=port), port)


def serve(store, port, hq_port):
    try:
        server = _build(store, port, hq_port)
    except (OSError, ValueError):
        _say(json.dumps({"error": {"code": "mobile_port_unavailable",
                                   "message": "Another program is using the mobile port. Is it already running?"}}))
        return 1
    settings = store.settings()
    where = f"https://{settings['remote_host']}" if settings["remote_host"] else "(remote access not set up yet)"
    _say(f"ViceKrack Mobile Command Center on http://127.0.0.1:{port}/  ·  phone: {where}  ·  Ctrl+C to stop")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _hq_running(hq_port):
    from .hq_client import HQClient
    try:
        status, _ = HQClient(hq_port).get("/api/status")
        return status == 200
    except NetworkError:
        return False


def start(store, port, hq_port, demo=False):
    """Start the HQ (Video Studio on) and keep it running, then run the mobile server in this window."""
    env = dict(os.environ)
    if not demo:
        try:
            from .winkey import load_key
            key = load_key()
        except NetworkError:
            key = None
        if key:
            env["XAI_API_KEY"] = key
        elif not env.get("XAI_API_KEY"):
            _say("No saved xAI key: paid actions will be refused until you run \u201cSave my xAI key\u201d.")
    if _hq_running(hq_port):
        _say(f"Using the ViceKrack HQ that is already running on port {hq_port}.")
        return serve(store, port, hq_port)
    command = [sys.executable, "-m", "vicekrack", "hq-serve", "--studio-demo" if demo else "--studio",
               "--port", str(hq_port)]
    stopping = threading.Event()
    state = {"process": None}

    def keep_hq_running():
        restarts = []
        while not stopping.is_set():
            state["process"] = subprocess.Popen(command, env=env, cwd=str(Path(__file__).resolve().parents[2]))
            state["process"].wait()
            if stopping.is_set():
                return
            now = time.monotonic()
            restarts = [t for t in restarts if now - t < 600] + [now]
            if len(restarts) > 5:
                _say("The HQ stopped 5 times in 10 minutes, so it was not restarted again. Check the HQ window.")
                return
            _say("The HQ stopped; restarting it in 5 seconds…")
            stopping.wait(5)

    threading.Thread(target=keep_hq_running, daemon=True).start()
    try:
        return serve(store, port, hq_port)
    finally:
        stopping.set()
        if state["process"] and state["process"].poll() is None:
            state["process"].terminate()


def main(argv=None, root=None):
    parser = argparse.ArgumentParser(prog="python -m vicekrack", description="ViceKrack Mobile Command Center")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("mobile-serve", "mobile-start", "mobile-setup"):
        command = commands.add_parser(name)
        command.add_argument("--port", type=int, default=MOBILE_PORT)
        if name != "mobile-setup":
            command.add_argument("--hq-port", type=int, default=HQ_PORT)
        if name == "mobile-start":
            command.add_argument("--demo", action="store_true", help="start the HQ with mock providers (no credits)")
    for name in ("mobile-pair", "mobile-save-key", "mobile-phones", "mobile-unpair-all", "mobile-status"):
        commands.add_parser(name)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    store = MobileStore(root)
    try:
        if args.command == "mobile-setup":
            return setup(store, args.port)
        if args.command == "mobile-pair":
            return pair(store)
        if args.command == "mobile-serve":
            return serve(store, args.port, args.hq_port)
        if args.command == "mobile-start":
            return start(store, args.port, args.hq_port, demo=args.demo)
        if args.command == "mobile-save-key":
            from .winkey import save_key
            save_key(getpass.getpass("Paste your xAI API key (it stays hidden) and press Enter: "))
            _say("Saved in Windows Credential Manager. ViceKrack can now start by itself.")
            return 0
        if args.command == "mobile-phones":
            _say(json.dumps({"phones": security.list_devices(store)}, indent=2))
            return 0
        if args.command == "mobile-unpair-all":
            _say(f"Unpaired {security.revoke(store)} phone(s).")
            return 0
        settings = store.settings()
        _say(json.dumps({"remote_host": settings["remote_host"], "owner_login": settings["owner_login"],
                         "daily_cap_usd": settings["daily_cap_usd"], "pin_set": bool(settings["pin"]),
                         "spent_today_usd": security.spent_today(store), "phones": security.list_devices(store),
                         "recent_actions": store.audit(10)}, indent=2))
        return 0
    except NetworkError as error:
        _say(json.dumps({"error": error.as_dict()}))
        return 1
    except (EOFError, KeyboardInterrupt):
        _say("\nCancelled. Nothing else was changed.")
        return 1
