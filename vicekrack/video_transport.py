"""Bounded xAI HTTPS transport. No retries, redirects, cookies or ambient proxies."""
import json
import os
import re
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from .errors import NetworkError

GENERATE_URL = "https://api.x.ai/v1/videos/generations"
STATUS_URL = "https://api.x.ai/v1/videos/"
MAX_DOWNLOAD = 80 * 1024 * 1024


def check_download_url(url):
    try:
        parsed = urlsplit(url)
        valid = (isinstance(url, str) and len(url) <= 4096 and parsed.scheme == "https"
                 and parsed.hostname == "vidgen.x.ai" and parsed.port in (None, 443)
                 and not parsed.username and not parsed.password and not parsed.fragment)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise NetworkError("video_download_refused", "Download requires an approved HTTPS media host.")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transport(method, url, body, *, credential):
    download = method == "DOWNLOAD"
    if download:
        check_download_url(url)
        if credential:
            raise NetworkError("video_transport_refused", "Media downloads cannot carry credentials.")
    elif not credential or not ((method == "POST" and url == GENERATE_URL) or
            (method == "GET" and re.fullmatch(re.escape(STATUS_URL) + r"[A-Za-z0-9-]{1,100}", url))):
        raise NetworkError("video_transport_refused", "Unsupported provider request.")
    headers = {"Accept": "video/mp4" if download else "application/json"}
    if credential:
        key = os.environ.get("XAI_API_KEY", "").strip()
        if not key:
            raise NetworkError("missing_video_credential", "Set XAI_API_KEY locally.")
        headers["Authorization"] = "Bearer " + key
        headers["Content-Type"] = "application/json"
    limit = MAX_DOWNLOAD if download else 65536
    deadline = time.monotonic() + 60
    try:
        request = Request(url, data=json.dumps(body).encode() if body is not None else None,
                          headers=headers, method="GET" if download else method)
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=15) as response:
            if response.status != 200:
                raise ValueError
            length = response.headers.get("Content-Length")
            if length is not None and (int(length) < 0 or int(length) > limit):
                raise ValueError
            chunks, total = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError
                chunk = response.read(min(65536, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    raise ValueError
        data = b"".join(chunks)
        return data if download else json.loads(data)
    except Exception:
        # Do not expose HTTP bodies, signed URLs, headers or raw SDK errors.
        raise NetworkError("video_transport_failed", "Provider request failed or exceeded its limits.") from None
