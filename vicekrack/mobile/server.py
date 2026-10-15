"""The Mobile Command Center's own small web server: 127.0.0.1 only (Tailscale Serve is the private front
door that forwards the phone's https requests here). Every request goes through `Gateway.respond`."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .gateway import MAX_BODY, STATUS_TEXT

HOST = "127.0.0.1"


class Handler(BaseHTTPRequestHandler):
    server_version = "ViceKrackMobile/1.0"
    sys_version = ""
    timeout = 30

    def _dispatch(self):
        headers = {key.lower(): value for key, value in self.headers.items()}
        body = b""
        if self.command == "POST":
            try:
                length = int(headers.get("content-length", ""))
            except ValueError:
                self._plain(411)
                return
            if length < 0 or length > MAX_BODY:
                self._plain(413)
                return
            body = self.rfile.read(length)
        status, response_headers, data = self.server.gateway.respond(self.command, self.path, headers, body)
        self.send_response(status, STATUS_TEXT.get(status, "Error"))
        for key, value in response_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _dispatch

    def _plain(self, code):
        body = json.dumps({"error": {"code": "bad_request", "message": "The request was not understood."}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        try:
            self._plain(code if code in (400, 404, 405, 408, 413, 414, 431, 501) else 400)
        except OSError:
            pass

    def log_message(self, format, *args):           # noqa: A002 - keep paths, codes and cookies out of logs
        return


class MobileServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, gateway, port):
        if not isinstance(port, int) or not (port == 0 or 1024 <= port <= 65535):
            raise ValueError("port")
        self.gateway = gateway
        super().__init__((HOST, port), Handler)
