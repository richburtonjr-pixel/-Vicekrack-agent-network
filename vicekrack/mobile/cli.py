"""Mobile Command Center commands (the double-click files in scripts/windows/ run these for you).

python -m vicekrack mobile-setup        PIN, daily cap, private Tailscale address, turn on the front door
python -m vicekrack mobile-save-key     save the xAI key in Windows Credential Manager
python -m vicekrack mobile-pair         show a 6-digit code to pair a phone (10 minutes)
python -m vicekrack mobile-start        start the HQ (Studio, with the saved key), the Trading Watch AND the mobile server
                                        (add --no-watch to leave the Trading Watch off)
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


WATCH_LOG_LIMIT = 5 * 1024 * 1024


def keep_watch_running(stopping, state, *, root=None, popen=subprocess.Popen, say=None, clock=time.monotonic):
    """Keep the Trading Watch (python -m vicekrack watch-run) running for as long as ViceKrack runs, so the agents
    keep checking the market without a separate window. It never gives up: if the watch stops it is started again
    (5 seconds later, or 2 minutes later if it stopped right after starting, e.g. because a separate
    "Start Trading Watch" window already holds the one-watch-at-a-time lock). Its lines go to
    runtime/trading/watch/watch.log. Practice money only: the watch's own rules (every agent approves, the $80
    shutdown) are unchanged."""
    say = say or _say
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    log_path = base / "runtime" / "trading" / "watch" / "watch.log"
    command = [sys.executable, "-m", "vicekrack", "watch-run"]
    starts = 0
    while not stopping.is_set():
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if log_path.is_file() and log_path.stat().st_size > WATCH_LOG_LIMIT:
                log_path.replace(log_path.with_suffix(".old.log"))
            if starts == 0:
                # Same as the "Start Trading Watch" window: open the $100 practice account if there isn't one yet.
                if popen([sys.executable, "-m", "vicekrack", "practice-status"], cwd=str(base),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).wait() != 0:
                    popen([sys.executable, "-m", "vicekrack", "practice-start"], cwd=str(base),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).wait()
            with log_path.open("ab") as log:
                began = clock()
                state["watch"] = popen(command, cwd=str(base), stdout=log, stderr=subprocess.STDOUT)
                if starts == 0:
                    say("AI agents' Trading Watch started (practice money only). Its log: runtime/trading/watch/watch.log")
                starts += 1
                code = state["watch"].wait()
        except OSError:
            code, began = None, clock()
        if stopping.is_set():
            return starts
        quick = clock() - began < 60
        say("The Trading Watch stopped" + (" right after starting (another watch window may be open)" if quick else "")
            + "; starting it again in " + ("2 minutes" if quick else "5 seconds") + "…")
        stopping.wait(120 if quick else 5)
    return starts


def _start_watch(stopping, state, enabled):
    if enabled:
        threading.Thread(target=keep_watch_running, args=(stopping, state), daemon=True).start()


def _stop(state):
    for name in ("process", "watch"):
        child = state.get(name)
        if child is not None and child.poll() is None:
            child.terminate()


def start(store, port, hq_port, demo=False, watch=True):
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
    stopping = threading.Event()
    state = {"process": None, "watch": None}
    if _hq_running(hq_port):
        _say(f"Using the ViceKrack HQ that is already running on port {hq_port}.")
        _start_watch(stopping, state, watch)
        try:
            return serve(store, port, hq_port)
        finally:
            stopping.set()
            _stop(state)
    command = [sys.executable, "-m", "vicekrack", "hq-serve", "--studio-demo" if demo else "--studio",
               "--port", str(hq_port)]

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
    _start_watch(stopping, state, watch)
    try:
        return serve(store, port, hq_port)
    finally:
        stopping.set()
        _stop(state)


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
            command.add_argument("--no-watch", action="store_true", help="don't start the Trading Watch")
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
            return start(store, args.port, args.hq_port, demo=args.demo, watch=not args.no_watch)
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
