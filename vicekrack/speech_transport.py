"""Step 46: bounded xAI text-to-speech transport (POST https://api.x.ai/v1/tts).

Official reference (docs.x.ai, Text to Speech, October 2026): a synchronous request whose
successful response body is the raw audio (Content-Type audio/wav, audio/mpeg, ...). No request
ID is documented for this endpoint, so none is read or invented, and there is nothing to poll.

One request, fixed URL, no redirects, no ambient proxies, no retries. The key is read from
XAI_API_KEY at call time and sent only in the Authorization header; it is never returned,
logged or stored. Failures are reported as fixed codes only:

  SpeechRejected (speech_rejected)   the API answered with a 4xx status: the request was refused,
                                     so nothing was generated (the status number is kept)
  speech_not_sent                    the connection was never made (DNS failure, refused)
  speech_transport_failed            anything else (timeout, reset, 5xx, oversize): the outcome
                                     is UNKNOWN and the caller must treat it as possibly billed
"""

import json
import os
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .errors import NetworkError

TTS_URL = "https://api.x.ai/v1/tts"
REJECTED_STATUSES = (400, 401, 403, 404, 413, 422, 429)


class SpeechRejected(NetworkError):
    def __init__(self, http_status):
        super().__init__("speech_rejected", f"xAI refused the speech request (HTTP {http_status}); nothing was generated.")
        self.http_status = http_status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transport(body, *, timeout_seconds, max_bytes):
    """Send one speech request. Returns {"content_type": str, "audio": bytes}."""
    key = os.environ.get("XAI_API_KEY", "").strip()
    if not key:
        raise NetworkError("missing_speech_credential", "Set XAI_API_KEY in your local environment.")
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "audio/*"}
    deadline = time.monotonic() + timeout_seconds
    try:
        request = Request(TTS_URL, data=json.dumps(body).encode(), headers=headers, method="POST")
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=min(30, timeout_seconds)) as response:
            if response.status != 200:
                raise ValueError
            length = response.headers.get("Content-Length")
            if length is not None and (int(length) < 0 or int(length) > max_bytes):
                raise ValueError
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()[:100]
            chunks, total = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError
                chunk = response.read(min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError
        return {"content_type": content_type, "audio": b"".join(chunks)}
    except HTTPError as error:
        status = error.code
        try:
            error.close()                             # the body is never read: it may echo the request
        except Exception:
            pass
        if status in REJECTED_STATUSES:
            raise SpeechRejected(status) from None
        raise NetworkError("speech_transport_failed", "The speech request failed; its outcome is unknown.") from None
    except URLError as error:
        if isinstance(error.reason, (socket.gaierror, ConnectionRefusedError)):
            raise NetworkError("speech_not_sent", "Could not connect to xAI; the request was not sent.") from None
        raise NetworkError("speech_transport_failed", "The speech request failed; its outcome is unknown.") from None
    except Exception:
        # Never expose response bodies, headers or raw exceptions.
        raise NetworkError("speech_transport_failed", "The speech request failed; its outcome is unknown.") from None
