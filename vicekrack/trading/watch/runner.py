"""The 24/7 watch loop (Trading PR 2): run a cycle, wait for the next bar to close, repeat.

- One loop at a time: an OS lock (runtime/trading/watch/watch.lock) refuses a second copy.
- Timing: cycles start a few seconds after each 5-minute bar closes, so research always uses the
  newest closed bar.
- A failed cycle is logged and the loop carries on; it never retries a cycle in a tight loop.
- Keep awake: on Windows, while the loop runs it asks Windows not to sleep (the screen may still
  turn off). Closing the window or pressing Ctrl+C stops the loop and lets the PC sleep again.
"""

import os
import time
from contextlib import contextmanager

from ..contracts import utc_now
from ..errors import TradingError
from .cycle import WatchFolder, run_cycle

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


@contextmanager
def keep_awake(enabled):
    """Windows only; elsewhere (and when disabled) it does nothing."""
    active = False
    if enabled and os.name == "nt":
        try:
            import ctypes
            active = bool(ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
        except (AttributeError, OSError):
            active = False
    try:
        yield active
    finally:
        if active:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


@contextmanager
def single_loop(folder):
    folder.base.mkdir(parents=True, exist_ok=True)
    stream = folder.lock_path.open("a+b")
    try:
        if stream.seek(0, 2) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise TradingError("watch_already_running", "The watch loop is already running in another window.") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def seconds_until_next(cycle_seconds, wait_after, now_epoch):
    """Seconds until `wait_after` seconds past the next multiple of cycle_seconds (UTC)."""
    next_boundary = (int(now_epoch) // cycle_seconds + 1) * cycle_seconds
    return max(1.0, next_boundary + wait_after - now_epoch)


def run_loop(config, *, root=None, max_cycles=None, sleep=time.sleep, clock=time.time, report=print, cycle=run_cycle):
    folder = WatchFolder(root)
    done = 0
    with single_loop(folder), keep_awake(config["keep_pc_awake"]) as awake:
        report(f"Watch loop started. Keeping the PC awake: {'yes' if awake else 'no (not Windows or not allowed)'}.")
        while max_cycles is None or done < max_cycles:
            started = utc_now()
            try:
                status = cycle(config, root=root, now=started)
                prices = ", ".join(f"{c['symbol']} {c['price'] or c['error']}" for c in status["coins"])
                guard = status["practice"]["guard"] or status["practice"]["error"]
                report(f"{started}  {prices}  |  guard: {guard}")
            except TradingError as error:
                report(f"{started}  cycle failed: {error.code}")
            except Exception:                                    # never let one bad cycle end the night
                report(f"{started}  cycle failed: unexpected_error")
            done += 1
            if max_cycles is not None and done >= max_cycles:
                break
            wait = seconds_until_next(config["cycle_seconds"], config["wait_after_bar_close_seconds"], clock())
            sleep(wait)
    return done
