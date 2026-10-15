"""Mobile Command Center requests: one function from a request to a response (like the HQ's api.py).

Who may connect
- Host must be this server on loopback or the configured private Tailscale address (blocks DNS rebinding).
- Requests that come through Tailscale must carry the owner's Tailscale login (Tailscale Serve sets that
  header itself and removes any copy a client sends). Remote access stays off until setup saved it.
- Every POST: Origin must be this app, the phone's pairing cookie must be valid, and the
  X-VK-Mobile-CSRF header must match. Bodies are JSON objects of at most 16 KB.

What the phone may do
- Read: status, its session, and an allowlist of HQ Studio reads.
- Act: an allowlist of HQ Studio actions, forwarded through HQClient (the HQ's own rules still apply).
- Paid actions also need the PIN, a cost estimate the HQ itself reports, and room under the daily cap.
  Retrying an uncertain paid request (possible double billing) and narration purchases stay PC-only.
- Every action is written to the audit log (no PIN, tokens or free text).
"""

import hmac
import json
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

from ..errors import NetworkError
from . import security

STATIC = Path(__file__).resolve().parent / "static"
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/static/pair.js": ("pair.js", "text/javascript; charset=utf-8"),
         "/static/pair.css": ("pair.css", "text/css; charset=utf-8")}
MAX_BODY = 16 * 1024
COOKIE = "vk_mobile"
CSRF_HEADER = "x-vk-mobile-csrf"
LOGIN_HEADER = "tailscale-user-login"
REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
NAME = re.compile(r"^[^\x00-\x1f<>\\`{}]{1,40}$")
HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "media-src 'self'; connect-src 'self'; manifest-src 'self'; base-uri 'none'; "
                                "form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
    "Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
STATUS_TEXT = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
               405: "Method Not Allowed", 409: "Conflict", 413: "Content Too Large", 422: "Unprocessable Content",
               429: "Too Many Requests", 500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable"}

# HQ Studio reads the phone may make (GET /m/api/hq/<name>?<query>; the HQ validates the query itself)
HQ_READS = {"overview", "productions", "production", "workflow", "research/jobs", "research/job"}
# HQ Studio actions the phone may forward. paid -> PIN + estimate + daily cap.
ACTIONS = {
    "research/prepare": False, "research/submit": True, "research/recover": False, "research/make-video": True,
    "workflow/start": False, "workflow/resume": False, "workflow/submit": True, "workflow/submit-all": True,
    "workflow/retry-scene": False, "workflow/export": False, "review/record": False,
}
PC_ONLY_FLAGS = ("retry_uncertain", "acknowledge_duplicate_billing")
# HQ refusals that happen before anything is sent to a provider: the cap reservation is given back.
RELEASE_STATUSES = {400, 403, 404, 409, 422}
ERROR_STATUS = {"pairing_code_wrong": 403, "pairing_code_expired": 403, "pin_wrong": 403, "pin_locked": 429,
                "pin_not_set": 409, "daily_cap_reached": 409, "cost_unknown": 409, "pc_only": 403,
                "hq_offline": 503, "hq_no_answer": 502, "hq_bad_response": 502, "hq_actions_off": 503,
                "hq_response_too_large": 502}
ID_FIELDS = ("job_id", "production_id", "workflow_id", "story", "scene", "purpose", "decision")


def _json(status, document, extra=None):
    body = json.dumps(document, ensure_ascii=True, allow_nan=False).encode("ascii")
    return status, {**HEADERS, "Content-Type": "application/json; charset=utf-8", **(extra or {})}, body


def _error(status, code, message):
    return _json(status, {"error": {"code": code, "message": message}})


def _net_error(error):
    return _error(ERROR_STATUS.get(error.code, 400 if error.code.startswith("invalid") else 409), error.code, error.message)


def _cookie(headers):
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return None


class Gateway:
    def __init__(self, store, hq, port=8770, clock=None):
        self.store, self.hq, self.port = store, hq, port
        self.clock = clock or security.utcnow

    # ------------------------------------------------------------ who is asking
    def _hosts(self, settings):
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if settings["remote_host"]:
            hosts.add(settings["remote_host"])
        return hosts

    def _origins(self, settings):
        origins = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}
        if settings["remote_host"]:
            origins.add(f"https://{settings['remote_host']}")
        return origins

    def _gate(self, method, headers, settings):
        """None if the request may continue, else an error response."""
        host = headers.get("host", "").lower().rstrip(".")
        if host not in self._hosts(settings):
            return _error(403, "forbidden_host", "This address is not the ViceKrack Mobile Command Center.")
        login = headers.get(LOGIN_HEADER)
        remote = login is not None or (settings["remote_host"] is not None and host == settings["remote_host"])
        if remote:
            owner = (settings["owner_login"] or "").lower()
            if not owner or not settings["remote_host"]:
                return _error(403, "remote_not_set_up", "Remote access is off until setup finishes on the PC.")
            if (login or "").lower() != owner:
                return _error(403, "not_owner", "Only the owner's Tailscale account can use this.")
        origin = headers.get("origin")
        fetch_site = headers.get("sec-fetch-site", "same-origin").lower()
        if method == "POST":
            if origin is None or origin.lower() not in self._origins(settings) or fetch_site != "same-origin":
                return _error(403, "forbidden_origin", "Actions must come from the ViceKrack app itself.")
        elif (origin is not None and origin.lower() not in self._origins(settings)) or fetch_site not in ("same-origin", "none"):
            return _error(403, "forbidden_origin", "Requests must come from the ViceKrack app itself.")
        return None

    def _device(self, headers):
        return security.authenticate(self.store, _cookie(headers), self.clock())

    def _csrf_ok(self, headers):
        token = _cookie(headers)
        return token is not None and hmac.compare_digest(headers.get(CSRF_HEADER, ""), security.csrf_for(token))

    @staticmethod
    def _body(headers, body):
        if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise NetworkError("invalid_request", "Send JSON.")
        if len(body) > MAX_BODY:
            raise NetworkError("invalid_request", "The request is too large.")
        try:
            document = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeError):
            raise NetworkError("invalid_request", "The request is not valid JSON.") from None
        if not isinstance(document, dict):
            raise NetworkError("invalid_request", "The request must be a JSON object.")
        return document

    # ------------------------------------------------------------ entry point
    def respond(self, method, target, headers, body=b""):
        """(status, headers, body). `headers` keys must be lower-case."""
        try:
            settings = self.store.settings()
            refused = self._gate(method, headers, settings)
            if refused:
                return refused
            if len(target) > 400 or any(ch in target for ch in ("\\", "\x00")):
                return _error(404, "not_found", "No such page.")
            parts = urlsplit(target)
            path = parts.path
            if method == "GET" and path in FILES and not parts.query:
                name, content_type = FILES[path]
                return 200, {**HEADERS, "Content-Type": content_type}, (STATIC / name).read_bytes()
            if method == "GET" and path == "/m/api/status" and not parts.query:
                return self._status(headers, settings)
            if method == "GET" and path == "/m/api/session" and not parts.query:
                device = self._device(headers)
                if device is None:
                    return _error(401, "not_paired", "Pair this phone first.")
                return _json(200, {"contract": "mobile_session", "device": device,
                                   "csrf": security.csrf_for(_cookie(headers))})
            if method == "GET" and path.startswith("/m/api/hq/"):
                return self._hq_read(headers, path[len("/m/api/hq/"):], parts.query)
            if method == "POST" and path == "/m/api/pair":
                return self._pair(headers, body)
            if method == "POST" and (path == "/m/api/unpair" or path.startswith("/m/api/action/")):
                device = self._device(headers)
                if device is None:
                    return _error(401, "not_paired", "Pair this phone first.")
                if not self._csrf_ok(headers):
                    return _error(403, "session_invalid", "Reload the app and try again.")
                if path == "/m/api/unpair":
                    security.revoke(self.store, device["device_id"])
                    self._audit(device, "unpair", {}, None, "ok")
                    return _json(200, {"unpaired": True},
                                 {"Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0"})
                return self._action(device, path[len("/m/api/action/"):], self._body(headers, body))
            if method not in ("GET", "POST"):
                return _error(405, "method_not_allowed", "Use GET or POST.")
            return _error(404, "not_found", "No such page.")
        except NetworkError as error:
            return _net_error(error)
        except Exception:                                    # noqa: BLE001 - never leak details
            return _error(500, "mobile_error", "Something went wrong on the PC. Nothing was retried.")

    # ------------------------------------------------------------ routes
    def _status(self, headers, settings):
        device = self._device(headers)
        if device is None:
            return _json(200, {"contract": "mobile_status", "paired": False})
        hq = {"reachable": False, "mode": "offline", "actions_enabled": False, "xai_key": False}
        try:
            info = self.hq.refresh_session()
            enabled = bool(info.get("actions_enabled"))
            hq = {"reachable": True, "actions_enabled": enabled,
                  "mode": ("demo" if info.get("demo") else "real") if enabled else "read_only",
                  "xai_key": bool((info.get("credentials") or {}).get("xai_configured"))}
        except NetworkError:
            pass
        cap = float(settings["daily_cap_usd"])
        spent = security.spent_today(self.store, self.clock())
        return _json(200, {"contract": "mobile_status", "paired": True, "device": device,
                           "pc": {"online": True, "checked_at": security._iso(self.clock())}, "hq": hq,
                           "spending": {"today_usd": spent, "cap_usd": cap, "left_usd": round(max(0.0, cap - spent), 4),
                                        "basis": "estimates of phone-approved paid actions"},
                           "pin_set": bool(settings.get("pin")),
                           "connection": {"via": "tailscale" if headers.get(LOGIN_HEADER) else "this_pc",
                                          "remote_ready": bool(settings["remote_host"] and settings["owner_login"])}})

    def _pair(self, headers, body):
        document = self._body(headers, body)
        name = document.get("name") or "iPhone"
        if set(document) - {"code", "name"} or not isinstance(name, str) or not NAME.match(name):
            raise NetworkError("invalid_request", "Send the 6-digit code and a short phone name.")
        token, device = security.redeem_pairing_code(self.store, document.get("code"), name.strip(), self.clock())
        self._audit(device, "pair", {}, None, "ok")
        cookie = f"{COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={security.DEVICE_DAYS * 86400}"
        return _json(200, {"contract": "mobile_session", "device": device, "csrf": security.csrf_for(token)},
                     {"Set-Cookie": cookie})

    def _hq_read(self, headers, name, query):
        if self._device(headers) is None:
            return _error(401, "not_paired", "Pair this phone first.")
        if name not in HQ_READS or len(query) > 200:
            return _error(404, "not_found", "No such page.")
        status, document = self.hq.get("/api/studio/" + name + (f"?{query}" if query else ""))
        return _json(status if status in STATUS_TEXT else 502, document)

    # ------------------------------------------------------------ actions
    def _estimate(self, action, document):
        """The HQ's own estimate (USD, high end) for a paid action, or None when it reports none."""
        def number(value):
            if isinstance(value, dict):
                value = value.get("high")
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None

        if action in ("research/submit", "research/make-video"):
            status, job = self.hq.get(f"/api/studio/research/job?id={quote(str(document.get('job_id', '')), safe='')}")
            if status != 200:
                return None
            if action == "research/submit":
                return number(job.get("estimate_usd"))
            cards, story = job.get("cards") or [], document.get("story")
            if type(story) is not int or not 0 <= story < len(cards):
                return None
            return number((cards[story].get("make_video") or {}).get("estimate_usd"))
        status, flow = self.hq.get(f"/api/studio/workflow?id={quote(str(document.get('workflow_id', '')), safe='')}")
        if status != 200:
            return None
        if action == "workflow/submit-all":
            offer = next((a for a in flow.get("actions") or [] if a.get("action") == "workflow_submit_all"), None)
            return number(offer.get("estimate_usd")) if offer else None
        scene = next((s for s in flow.get("scenes") or [] if s.get("scene_index") == document.get("scene")), None)
        request = (scene or {}).get("request") or {}
        if not isinstance(request.get("duration"), (int, float)):
            return None
        from ..hq.studio import VIDEO_PRICES                 # read-only: the same price table the HQ shows
        return number(request["duration"] * VIDEO_PRICES.get(request.get("model"), max(VIDEO_PRICES.values())))

    def _action(self, device, action, document):
        if action not in ACTIONS:
            return _error(404, "not_found", "The phone cannot do that action.")
        request_id = document.get("request_id")
        if not isinstance(request_id, str) or not REQUEST_ID.match(request_id):
            raise NetworkError("invalid_request", "Each action needs a request_id.")
        refs = {k: document[k] for k in ID_FIELDS if isinstance(document.get(k), (str, int)) and not isinstance(document.get(k), bool)}
        if any(document.get(flag) for flag in PC_ONLY_FLAGS):
            self._audit(device, action, refs, None, "pc_only")
            raise NetworkError("pc_only", "Retrying a request that may already be billed is only allowed on the PC.")
        pin = document.pop("pin", None)
        forward = {k: v for k, v in document.items() if k not in PC_ONLY_FLAGS}
        if not ACTIONS[action]:
            return self._forward(device, action, refs, forward, None)
        try:
            security.check_pin(self.store, pin, self.clock())
        except NetworkError as error:
            self._audit(device, action, refs, None, error.code)
            raise
        estimate = self._estimate(action, forward)
        if estimate is None:
            self._audit(device, action, refs, None, "cost_unknown")
            raise NetworkError("cost_unknown", "ViceKrack could not show a cost for this, so approve it on the PC.")
        try:
            outcome, _ = security.reserve(self.store, request_id, device=device, action=action,
                                          ref=refs, estimate_usd=estimate, now=self.clock())
        except NetworkError as error:
            self._audit(device, action, refs, estimate, error.code)
            raise
        return self._forward(device, action, refs, forward, estimate, reserved=(outcome == "ok"), request_id=request_id)

    def _forward(self, device, action, refs, document, estimate, reserved=False, request_id=None):
        try:
            status, answer = self.hq.post(action, document)
        except NetworkError as error:
            if reserved:
                security.settle(self.store, request_id, "released" if error.code == "hq_offline" else "kept")
            self._audit(device, action, refs, estimate, error.code)
            raise
        code = (answer.get("error") or {}).get("code")
        if reserved:
            give_back = status in RELEASE_STATUSES and code and "uncertain" not in code \
                and code != "duplicate_request_in_progress"
            security.settle(self.store, request_id, "released" if give_back else "kept")
        self._audit(device, action, refs, estimate, code or "ok")
        return _json(status if status in STATUS_TEXT else 502, answer)

    def _audit(self, device, action, refs, estimate, outcome):
        self.store.append_audit({"at": security._iso(self.clock()), "device_id": device["device_id"],
                                 "device": device["name"], "action": action, "refs": refs,
                                 "estimate_usd": estimate, "outcome": outcome})
