"""Step 48: Video Studio HTTP routes. The only routes in the HQ that change anything.

Off unless the server is started with `hq-serve --studio` (real) or `hq-serve --studio-demo` (mock
providers, isolated folder). Without them, GET /api/studio/session answers {"actions_enabled": false}
and every other studio route is refused; the rest of the HQ is unchanged and read-only.

Read routes (GET):
  /api/studio/session                 options, mode and a CSRF token; sets the HttpOnly session cookie
  /api/studio/productions             productions with eligibility explained
  /api/studio/production?id=prod-...  script, scene plan, speech jobs, caption tracks, workflows
  /api/studio/workflow?id=vpw-...     saved progress, prepared requests, quality, review, export, actions
  /api/studio/export?workflow=vpw-...&package=pkg-...   a zip of a verified export package (download)

Action routes (POST, JSON body, every one explicitly user-triggered):
  /api/studio/speech/prepare, /speech/submit, /speech/recover, /captions/prepare,
  /api/studio/workflow/start, /workflow/resume, /workflow/submit, /workflow/retry-scene,
  /workflow/export, /review/record

Every POST must pass, in order:
  1. the server's loopback Host and an Origin header equal to this server (no cross-site, no missing Origin);
  2. Sec-Fetch-Site same-origin when the browser sends it;
  3. the session cookie AND the X-VK-Studio-CSRF header from /api/studio/session (constant-time compare);
  4. Content-Type application/json, a body of at most 16 KB, a JSON object with exactly the allowed keys,
     every value type- and pattern-checked (IDs only; never paths, commands or free-form options);
  5. a request_id: a repeated request_id returns the first answer again instead of acting twice;
  6. one action at a time: a second action while one runs gets 409 studio_busy.
Errors carry fixed codes and the services' own fixed messages; never exception text, paths or keys. A
final guard refuses any response that would contain the XAI_API_KEY value.
"""

import hmac
import json
import os
import re
import secrets
import threading
from collections import OrderedDict
from urllib.parse import parse_qs, urlsplit

from ..errors import NetworkError

PREFIX = "/api/studio/"
MAX_BODY = 16 * 1024
COOKIE = "vk_studio"
CSRF_HEADER = "x-vk-studio-csrf"
ID = {"production": re.compile(r"^prod-[0-9a-f]{24}$"), "workflow": re.compile(r"^vpw-[0-9a-f]{24}$"),
      "speech": re.compile(r"^sp-[0-9a-f]{24}$"), "caption": re.compile(r"^cap-[0-9a-f]{24}$"),
      "package": re.compile(r"^pkg-[0-9a-f]{24}$"), "review": re.compile(r"^rev-[0-9a-f]{24}$"),
      "request": re.compile(r"^[A-Za-z0-9-]{8,64}$"), "digest": re.compile(r"^[0-9a-f]{12,64}$"),
      "consent": re.compile(r"^paid-(generate:vid|speech:sp)-[0-9a-f]{24}$"), "voice": re.compile(r"^[A-Za-z]{2,20}$")}
CHOICE = {"timing": ("provider", "estimated"), "narration": ("silent", "speech"),
          "purpose": ("review_copy", "approved_preview"),
          "decision": ("approved_for_preview", "changes_requested", "rejected"),
          "resolution": ("480p", "720p", "1080p"), "model": ("grok-imagine-video-1.5", "grok-imagine-video-1.5-lite")}
ACKS = ("needs_review_result", "unavailable_checks", "draft_restrictions", "stale_evidence")

# action path -> (studio method, {body key: (kind, required)}); kind is an ID name, a CHOICE name or a type
ACTIONS = {
    "speech/prepare": ("speech_prepare", {"production_id": ("production", True), "voice": ("voice", False),
                                          "with_timestamps": ("bool", False)}),
    "speech/submit": ("speech_submit", {"job_id": ("speech", True), "consent": ("consent", True),
                                        "retry_uncertain": ("bool", False),
                                        "acknowledge_duplicate_billing": ("bool", False)}),
    "speech/recover": ("speech_recover", {"job_id": ("speech", True)}),
    "captions/prepare": ("captions_prepare", {"speech_job_id": ("speech", True), "timing": ("timing", True)}),
    "workflow/start": ("workflow_start", {"production_id": ("production", True), "narration": ("narration", True),
                                          "speech_job_id": ("speech", False), "caption_id": ("caption", False),
                                          "model": ("model", False), "resolution": ("resolution", False)}),
    "workflow/resume": ("workflow_resume", {"workflow_id": ("workflow", True), "allow_network": ("bool", False)}),
    "workflow/submit": ("workflow_submit", {"workflow_id": ("workflow", True), "scene": ("scene", True),
                                            "consent": ("consent", True), "retry_uncertain": ("bool", False),
                                            "acknowledge_duplicate_billing": ("bool", False)}),
    "workflow/retry-scene": ("workflow_retry_scene", {"workflow_id": ("workflow", True), "scene": ("scene", True),
                                                      "model": ("model", False), "resolution": ("resolution", False)}),
    "workflow/export": ("workflow_export", {"workflow_id": ("workflow", True), "purpose": ("purpose", True)}),
    "review/record": ("review_record", {"workflow_id": ("workflow", True), "decision": ("decision", True),
                                        "reviewer": ("reviewer", True), "binding": ("digest", True),
                                        "acknowledgments": ("acks", False), "notes": ("notes", False),
                                        "supersedes": ("review", False)}),
}
ERROR_STATUS = {"studio_busy": 409, "duplicate_request_in_progress": 409}


class StudioGate:
    """Session secrets, de-duplication and the single-action lock for one running server."""

    def __init__(self, studio):
        self.studio = studio
        self.session = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.answers = OrderedDict()                       # request_id -> (status, document)
        self.answers_lock = threading.Lock()


def _json(status, document, extra=None):
    from .api import HEADERS
    body = json.dumps(document, ensure_ascii=True, allow_nan=False).encode("ascii")
    key = os.environ.get("XAI_API_KEY", "").strip()
    if key and key.encode() in body:                        # last line of defence: never send the key
        body = json.dumps({"error": {"code": "studio_error", "message": "The response was withheld."}}).encode()
        status = 500
    return status, {**HEADERS, "Content-Type": "application/json; charset=utf-8", **(extra or {})}, body


def _error(status, code, message):
    return _json(status, {"error": {"code": code, "message": message}})


def _status_for(code):
    if code in ERROR_STATUS:
        return ERROR_STATUS[code]
    if code.endswith("not_found"):
        return 404
    if code.startswith("invalid") or code.endswith("required") or code.startswith("missing"):
        return 400
    return 409


def _cookie(headers):
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return None


def _origin_ok(headers, port):
    allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    origin = headers.get("origin")
    if origin is None or origin.lower() not in allowed:
        return False
    return headers.get("sec-fetch-site", "same-origin").lower() == "same-origin"


def _check_value(key, kind, value):
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "scene":
        return type(value) is int and 1 <= value <= 4
    if kind == "reviewer":
        return isinstance(value, str) and 1 <= len(value.strip()) <= 80 and not re.search(r"[\x00-\x1f<>]", value)
    if kind == "notes":
        return isinstance(value, str) and len(value) <= 2000 and not re.search(r"[\x00-\x08\x0b-\x1f]", value)
    if kind == "acks":
        return isinstance(value, list) and len(value) <= 4 and all(a in ACKS for a in value) and len(set(value)) == len(value)
    if kind in CHOICE:
        return value in CHOICE[kind]
    return isinstance(value, str) and bool(ID[kind].match(value))


def _parse(path, headers, body):
    name = path[len(PREFIX):]
    if name not in ACTIONS:
        raise NetworkError("studio_action_not_found", "No such studio action.")
    if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise NetworkError("invalid_studio_request", "Send JSON (Content-Type: application/json).")
    try:
        document = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise NetworkError("invalid_studio_request", "The request body is not valid JSON.") from None
    method, spec = ACTIONS[name]
    if not isinstance(document, dict) or not set(document) <= set(spec) | {"request_id"} \
            or not _check_value("request_id", "request", document.get("request_id")):
        raise NetworkError("invalid_studio_request", "The request has missing, unknown or malformed fields.")
    arguments = {}
    for key, (kind, required) in spec.items():
        if key not in document or document[key] is None:
            if required:
                raise NetworkError("invalid_studio_request", "The request has missing, unknown or malformed fields.")
            continue
        if not _check_value(key, kind, document[key]):
            raise NetworkError("invalid_studio_request", "The request has missing, unknown or malformed fields.")
        arguments[key] = document[key]
    return document["request_id"], method, arguments


def handle(gate, method, target, headers, body, port):
    """(status, headers, body) for a /api/studio/ request. `gate` is None when the studio is off."""
    parts = urlsplit(target)
    path = parts.path
    if gate is None:
        if method == "GET" and path == PREFIX + "session" and not parts.query:
            return _json(200, {"contract": "hq_studio_session", "actions_enabled": False,
                               "notice": "The Video Studio is off. Start the HQ with: python -m vicekrack hq-serve "
                                         "--studio (real, paid requests only on approval) or --studio-demo (offline demo)."})
        if method != "GET":
            return _error(405, "method_not_allowed", "The HQ is read-only; start it with --studio to enable actions.")
        return _error(403, "studio_disabled", "The Video Studio is off; start the HQ with --studio or --studio-demo.")
    try:
        if method == "GET":
            return _read(gate, path, parts.query)
        if method != "POST":
            return _error(405, "method_not_allowed", "Studio actions use POST; reads use GET.")
        if not _origin_ok(headers, port):
            return _error(403, "forbidden_origin", "Actions must come from the local HQ page itself.")
        cookie, token = _cookie(headers), headers.get(CSRF_HEADER, "")
        if cookie is None or not hmac.compare_digest(cookie, gate.session) or not hmac.compare_digest(token, gate.csrf):
            return _error(403, "studio_session_invalid", "Reload the Studio page to start a new session.")
        if len(body) > MAX_BODY:
            return _error(413, "studio_request_too_large", "The request is too large.")
        request_id, name, arguments = _parse(path, headers, body)
        with gate.answers_lock:
            if request_id in gate.answers:
                answer = gate.answers[request_id]
                if answer is None:
                    raise NetworkError("duplicate_request_in_progress", "This request is already being handled.")
                status, document = answer
                return _json(status, dict(document, replayed=True))
            gate.answers[request_id] = None
            while len(gate.answers) > 300:
                gate.answers.popitem(last=False)
        if not gate.lock.acquire(blocking=False):
            with gate.answers_lock:
                gate.answers.pop(request_id, None)
            raise NetworkError("studio_busy", "Another studio action is still running; wait for it to finish.")
        try:
            try:
                document = getattr(gate.studio, name)(**arguments)
                status = 200
            except NetworkError as error:
                status, document = _status_for(error.code), {"error": {"code": error.code, "message": error.message}}
            except Exception:                                # noqa: BLE001 - never leak details
                status, document = 500, {"error": {"code": "studio_error", "message": "The action failed; nothing "
                                                   "was retried. Reload to see the saved state."}}
        finally:
            gate.lock.release()
        with gate.answers_lock:
            gate.answers[request_id] = (status, document)
        return _json(status, document)
    except NetworkError as error:
        return _error(_status_for(error.code), error.code, error.message)
    except Exception:                                        # noqa: BLE001
        return _error(500, "studio_error", "The Studio could not handle that request.")


def _one(query, key, kind):
    values = query.get(key, [])
    if len(values) != 1 or not ID[kind].match(values[0]):
        raise NetworkError("invalid_studio_request", "The request has missing or malformed parameters.")
    return values[0]


def _read(gate, path, query_text):
    query = parse_qs(query_text, max_num_fields=3, keep_blank_values=True)
    studio = gate.studio
    try:
        if path == PREFIX + "session" and not query:
            doc = dict(studio.session_info(), csrf=gate.csrf)
            cookie = f"{COOKIE}={gate.session}; Path=/api/studio/; HttpOnly; SameSite=Strict"
            return _json(200, doc, {"Set-Cookie": cookie})
        if path == PREFIX + "productions" and not query:
            return _json(200, studio.productions())
        if path == PREFIX + "production" and set(query) == {"id"}:
            return _json(200, studio.production(_one(query, "id", "production")))
        if path == PREFIX + "workflow" and set(query) == {"id"}:
            return _json(200, studio.workflow(_one(query, "id", "workflow")))
        if path == PREFIX + "export" and set(query) == {"workflow", "package"}:
            workflow, package = _one(query, "workflow", "workflow"), _one(query, "package", "package")
            from .api import HEADERS
            data = studio.export_zip(workflow, package)
            return 200, {**HEADERS, "Content-Type": "application/zip",
                         "Content-Disposition": f'attachment; filename="{package}.zip"'}, data
    except NetworkError as error:
        return _error(_status_for(error.code), error.code, error.message)
    except Exception:                                        # noqa: BLE001
        return _error(500, "studio_error", "The Studio could not load that data.")
    return _error(404, "not_found", "No such page.")
