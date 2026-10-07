"""Living HQ local server (Step 32): started explicitly, loopback only, read-only.

python -m vicekrack hq-serve [--port 8765]

Binds to 127.0.0.1 only (never all interfaces) and delegates every request to
`api.respond`, which allows GET on a fixed set of routes. No background work runs: the
server only answers requests while it is running, and stops with Ctrl+C.
"""

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .api import STATUS_TEXT, respond

HOST = "127.0.0.1"


class Handler(BaseHTTPRequestHandler):
    server_version = "ViceKrackHQ/1.0"
    sys_version = ""
    timeout = 10
    root = None

    def _dispatch(self):
        headers = {key.lower(): value for key, value in self.headers.items()}
        status, response_headers, body = respond(self.command, self.path, headers, port=self.server.server_address[1],
                                                 root=self.server.data_root)
        self.send_response(status, STATUS_TEXT.get(status, "Error"))
        for key, value in response_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _dispatch

    def send_error(self, code, message=None, explain=None):
        """Malformed or unsupported requests get a fixed JSON body (no reflected input)."""
        body = json.dumps({"error": {"code": "bad_request", "message": "The request was not understood."}}).encode()
        try:
            self.send_response(code if code in (400, 404, 405, 408, 414, 431, 501) else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, format, *args):           # noqa: A002 - keep request paths out of logs
        return


class HQServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, root=None):
        if not isinstance(port, int) or not (port == 0 or 1024 <= port <= 65535):
            raise ValueError("port")
        self.data_root = root
        super().__init__((HOST, port), Handler)


def main(argv=None, root=None):
    parser = argparse.ArgumentParser(prog="python -m vicekrack", description="ViceKrack Living HQ (local, read-only)")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("hq-serve", help="Serve the Living HQ on 127.0.0.1 until Ctrl+C")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        server = HQServer(args.port, root)
    except (OSError, ValueError):
        print(json.dumps({"error": {"code": "hq_port_unavailable",
                                    "message": "Choose a free port between 1024 and 65535."}}))
        return 1
    print(f"ViceKrack Living HQ (read-only) at http://{HOST}:{server.server_address[1]}/  -  press Ctrl+C to stop")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
