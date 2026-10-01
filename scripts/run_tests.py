"""Run the complete suite with accidental in-process network connections blocked."""
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
# Match unittest discovery's imports when launched as a file from any working directory.
sys.path.insert(0, str(ROOT))


def main():
    with patch.object(socket.socket, "connect", side_effect=OSError("Network disabled during tests")), \
         patch.object(socket.socket, "connect_ex", side_effect=OSError("Network disabled during tests")), \
         patch.object(socket, "create_connection", side_effect=OSError("Network disabled during tests")):
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        if suite.countTestCases() == 0:
            print("No tests discovered; refusing to report success.", file=sys.stderr)
            return 1
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
