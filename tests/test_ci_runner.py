import contextlib
import io
import socket
import unittest
from unittest.mock import patch
from scripts import run_tests


class CIRunnerTests(unittest.TestCase):
    def test_empty_suite_is_failure(self):
        with patch.object(unittest.defaultTestLoader, "discover", return_value=unittest.TestSuite()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_tests.main(), 1)

    def test_connections_are_blocked_and_guard_is_restored(self):
        original = socket.create_connection
        def probe():
            with self.assertRaisesRegex(OSError, "Network disabled"):
                socket.create_connection(("127.0.0.1", 1))
            with socket.socket() as stream:
                with self.assertRaisesRegex(OSError, "Network disabled"):
                    stream.connect(("127.0.0.1", 1))
                with self.assertRaisesRegex(OSError, "Network disabled"):
                    stream.connect_ex(("127.0.0.1", 1))
        suite = unittest.TestSuite([unittest.FunctionTestCase(probe)])
        with patch.object(unittest.defaultTestLoader, "discover", return_value=suite), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_tests.main(), 0)
        self.assertIs(socket.create_connection, original)

    def test_failed_test_returns_failure(self):
        def fail():
            raise AssertionError("Deliberate runner self-test")
        suite = unittest.TestSuite([unittest.FunctionTestCase(fail)])
        with patch.object(unittest.defaultTestLoader, "discover", return_value=suite), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_tests.main(), 1)
