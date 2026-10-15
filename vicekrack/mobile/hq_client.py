"""The private hallway to the HQ: a client for the HQ's existing Studio API on 127.0.0.1.

It behaves like the HQ's own page: same loopback Host and Origin, the HQ session cookie and the
X-VK-Studio-CSRF token from /api/studio/session. Every rule the HQ enforces (exact consent per paid
item, one action at a time, repeated request IDs answered once) still applies. If the HQ restarted, the
client gets a fresh session and tries once more (the HQ refuses a stale session before doing anything).
"""

import http.client
import json
import threading

from ..errors import NetworkError

GET_TIMEOUT, POST_TIMEOUT = 15, 600
MAX_RESPONSE = 32 * 1024 * 1024


def http_transport(port):
    def send(method, path, headers, body, timeout):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            connection.request(method, path, body=body or None, headers=headers)
            response = connection.getresponse()
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise NetworkError("hq_response_too_large", "The HQ answer was too large.")
            return response.status, {k.lower(): v for k, v in response.getheaders()}, data
        finally:
            connection.close()
    return send


class HQClient:
    def __init__(self, port=8765, transport=None):
        self.port = port
        self.origin = f"http://127.0.0.1:{port}"
        self.transport = transport or http_transport(port)
        self.lock = threading.Lock()
        self.cookie = self.csrf = None
        self.session_info = None

    def _send(self, method, path, headers=None, body=b"", timeout=GET_TIMEOUT):
        base = {"Host": f"127.0.0.1:{self.port}", "Accept": "application/json"}
        try:
            return self.transport(method, path, {**base, **(headers or {})}, body, timeout)
        except NetworkError:
            raise
        except ConnectionRefusedError:                  # nothing reached the HQ
            raise NetworkError("hq_offline", "ViceKrack HQ is not running on the PC.") from None
        except (OSError, http.client.HTTPException):    # it may have reached the HQ: treat as uncertain
            raise NetworkError("hq_no_answer", "The HQ did not answer in time. Check the result before "
                                               "trying again.") from None

    @staticmethod
    def _decode(status, data):
        try:
            document = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise NetworkError("hq_bad_response", "The HQ sent an answer that could not be read.") from None
        if not isinstance(document, dict):
            raise NetworkError("hq_bad_response", "The HQ sent an answer that could not be read.")
        return status, document

    def refresh_session(self):
        status, headers, data = self._send("GET", "/api/studio/session")
        status, document = self._decode(status, data)
        cookie = None
        for part in headers.get("set-cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "vk_studio":
                cookie = value
        with self.lock:
            self.cookie, self.csrf = cookie, document.get("csrf")
            self.session_info = {k: v for k, v in document.items() if k != "csrf"}
        return self.session_info

    def get(self, path):
        """(status, document) for a read-only HQ route."""
        status, _, data = self._send("GET", path)
        return self._decode(status, data)

    def get_raw(self, path, headers=None):
        """(status, headers, bytes) for a file route (video byte ranges, export zips)."""
        return self._send("GET", path, headers)

    def post(self, action, document):
        """(status, document) for one Studio action, e.g. post("research/prepare", {...})."""
        body = json.dumps(document, ensure_ascii=True, allow_nan=False).encode("ascii")
        for attempt in (1, 2):
            if self.cookie is None or self.csrf is None:
                self.refresh_session()
            if self.cookie is None or self.csrf is None:
                raise NetworkError("hq_actions_off", "The HQ is running without Video Studio actions.")
            headers = {"Origin": self.origin, "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin",
                       "Cookie": f"vk_studio={self.cookie}", "X-VK-Studio-CSRF": self.csrf}
            status, _, data = self._send("POST", "/api/studio/" + action, headers, body, POST_TIMEOUT)
            status, answer = self._decode(status, data)
            code = (answer.get("error") or {}).get("code")
            if status == 403 and code == "studio_session_invalid" and attempt == 1:
                self.cookie = self.csrf = None          # the HQ restarted: nothing was done, start a new session
                continue
            return status, answer
        return status, answer
