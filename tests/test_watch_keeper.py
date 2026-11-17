"""mobile-start keeps the Trading Watch running (agents 24/7 while the PC is on). No real processes."""

import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vicekrack.mobile import cli  # noqa: E402


class FakeChild:
    def __init__(self, code, on_wait=None):
        self.code, self.on_wait, self.ended = code, on_wait, False

    def wait(self):
        if self.on_wait:
            self.on_wait()
        self.ended = True
        return self.code

    def poll(self):
        return self.code if self.ended else None


class FakeEvent(threading.Event):
    """stopping.wait() returns at once, so the test never really sleeps; records each pause."""
    def __init__(self):
        super().__init__()
        self.pauses = []

    def wait(self, timeout=None):
        self.pauses.append(timeout)
        return self.is_set()


class WatchKeeper(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.stopping, self.state, self.calls, self.said = FakeEvent(), {}, [], []
        self.now = [0.0]

    def run_keeper(self, watch_runs, practice_ok=True):
        """watch_runs: list of (exit code, seconds it ran). The keeper is stopped after the last one."""
        runs = list(watch_runs)

        def popen(command, **kwargs):
            self.calls.append(command[3:])
            if command[3] == "practice-status":
                return FakeChild(0 if practice_ok else 1)
            if command[3] == "practice-start":
                return FakeChild(0)
            code, seconds = runs.pop(0)

            def ran():
                self.now[0] += seconds
                if not runs:
                    self.stopping.set()                   # ViceKrack closing: the last run ends with the window
            self.assertIs(kwargs["stdout"].closed, False)    # output goes to the log file, not the window
            return FakeChild(code, ran)

        return cli.keep_watch_running(self.stopping, self.state, root=self.root, popen=popen,
                                      say=self.said.append, clock=lambda: self.now[0])

    def test_restarts_the_watch_whenever_it_stops(self):
        starts = self.run_keeper([(1, 3600), (1, 7200), (0, 50)])
        self.assertEqual(starts, 3)
        self.assertEqual([c for c in self.calls if c == ["watch-run"]], [["watch-run"]] * 3)
        self.assertEqual(self.stopping.pauses, [5, 5])        # long runs that stop restart after 5 seconds

    def test_waits_longer_when_the_watch_stops_right_away(self):
        self.run_keeper([(1, 2), (0, 3600)])                  # e.g. another watch window holds the lock
        self.assertEqual(self.stopping.pauses, [120])
        self.assertIn("another watch window may be open", self.said[-1])

    def test_never_gives_up(self):
        starts = self.run_keeper([(1, 1)] * 12 + [(0, 10)])
        self.assertEqual(starts, 13)

    def test_opens_the_practice_account_only_if_missing(self):
        self.run_keeper([(0, 10)], practice_ok=True)
        self.assertNotIn(["practice-start"], self.calls)
        self.calls.clear()
        self.stopping = FakeEvent()
        self.run_keeper([(0, 10)], practice_ok=False)
        self.assertIn(["practice-start"], self.calls)

    def test_writes_the_log_under_runtime(self):
        self.run_keeper([(0, 10)])
        self.assertTrue((self.root / "runtime/trading/watch/watch.log").is_file())

    def test_stop_ends_children(self):
        running = FakeChild(None)
        state = {"process": None, "watch": running}
        ended = []
        running.terminate = lambda: ended.append(True)
        cli._stop(state)
        self.assertEqual(ended, [True])

    def test_no_watch_option(self):
        args = []
        original = cli.start
        cli.start = lambda store, port, hq_port, demo=False, watch=True: args.append(watch) or 0
        try:
            cli.main(["mobile-start", "--no-watch"], root=self.root)
            cli.main(["mobile-start"], root=self.root)
        finally:
            cli.start = original
        self.assertEqual(args, [False, True])


if __name__ == "__main__":
    unittest.main()
