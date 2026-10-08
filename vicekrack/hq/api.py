"""Living HQ read-only API (Step 32): one pure function from a request to a response.

Routes (GET only; everything else is 405):
  /                         the app page
  /static/<fixed name>      four bundled files from an allowlist (no path joining)
  /api/status               {"read_only": true, ...}
  /api/timelines            demo + recorded + reconstructable timelines (bounded)
  /api/scene?timeline=ID    one HQ scene; ID is "demo" or matches tl-/rar-/srun-/prod- + 24 hex
  /api/results?timeline=ID                    Step 34 results desk index for a simulation timeline (no results)
  /api/results/at?timeline=ID&position=N      the portfolio after the first N events (nothing later)
  /api/results/summary?timeline=ID            the completed run summary (end-of-run results)

Boundaries:
- The Host header must be this loopback server (127.0.0.1 or localhost on its port);
  any Origin must be that same origin, and cross-site fetch metadata is refused. This
  blocks other websites and DNS-rebinding pages from reading local data.
- Paths are matched exactly; there is no file serving from user input, so traversal and
  encoded tricks simply do not match a route (404).
- Data comes only through the Step 31 loaders (re-validated) and the HQ scene contract.
  Errors carry fixed codes and messages: never exception text, paths or environment
  values. Every response has a strict Content-Security-Policy and no CORS headers.
- Nothing here writes, runs agents or simulations, generates analytics, sends messages or places
  orders. The results routes only load saved, re-validated runs and reports (`results.py`).
"""

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..errors import NetworkError
from .demo import demo_scene
from .results import results_at, results_index, results_summary
from .scene import scene_from_view

STATIC = Path(__file__).resolve().parent / "static"
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
         "/static/hq-core.js": ("hq-core.js", "text/javascript; charset=utf-8"),
         "/static/app.js": ("app.js", "text/javascript; charset=utf-8")}
TIMELINE = re.compile(r"^(demo|tl-[0-9a-f]{24}|rar-[0-9a-f]{24}|srun-[0-9a-f]{24}|prod-[0-9a-f]{24}|wfr-[0-9a-f]{32})$")
MAX_TIMELINES = 200
RESULTS_ROUTES = {"/api/results": {"timeline"}, "/api/results/at": {"timeline", "position"},
                  "/api/results/summary": {"timeline"}}
POSITION = re.compile(r"^(0|[1-9][0-9]{0,5})$")
# Fixed status and message per results code: never exception text, paths or values.
RESULTS_ERRORS = {
    "results_not_simulation": (422, "Trading results exist only for simulation timelines (srun- or a recorded sim-run)."),
    "results_run_missing": (404, "This timeline does not name a saved simulation run."),
    "sim_run_not_found": (404, "The simulation run was not saved (use sim-run DATASET_ID --save)."),
    "sim_run_corrupt": (409, "The saved simulation run failed validation, so no results are shown."),
    "timeline_run_mismatch": (409, "The timeline and the saved run do not match, so they are not combined."),
    "results_position_out_of_range": (400, "The replay position is outside this timeline."),
    "invalid_results": (500, "The results could not be shown safely."),
    "invalid_results_request": (400, "Use timeline=ID, plus position=N (a whole number) for /api/results/at."),
}
HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
    "Cache-Control": "no-store", "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
STATUS_TEXT = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
               409: "Conflict", 422: "Unprocessable Content", 500: "Internal Server Error"}


def _json(status, document):
    body = json.dumps(document, ensure_ascii=True, allow_nan=False).encode("ascii")
    return status, {**HEADERS, "Content-Type": "application/json; charset=utf-8"}, body


def _error(status, code, message):
    return _json(status, {"error": {"code": code, "message": message}})


def _allowed_origin(headers, port):
    """True only for requests addressed to this loopback server by its own page (or a direct visit)."""
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if headers.get("host", "").lower() not in hosts:
        return False
    origin = headers.get("origin")
    if origin is not None and origin.lower() not in {f"http://{h}" for h in hosts}:
        return False
    return headers.get("sec-fetch-site", "same-origin").lower() in {"same-origin", "none"}


def timelines(root=None):
    """Demo, recorded and reconstructable timelines. Recorded timelines of one run share a correlation
    ID (Step 33). Attempts of the same command on the same run (same correlation and kind) get
    `attempt` k of `attempts` n, ordered by start time (ties: the order in which their timelines
    were created on disk)."""
    from ..content_events import correlation_for
    from ..events.cli import reconstructable
    from ..events.store import EventStore
    rows = [{"id": "demo", "label": "Demo HQ (synthetic)", "origin": "demo", "department": "both", "kind": "hq_demo",
             "readable": True, "correlation_id": None}]
    recorded = []
    for row in EventStore(root).list():
        recorded.append({"id": row["timeline_id"], "label": row.get("kind", "timeline"), "origin": "recorded",
                         "department": row["department"], "kind": row.get("kind"), "readable": row["readable"],
                         "completeness": row.get("completeness"), "outcome": row.get("outcome"), "live": row.get("live"),
                         "started_at": row.get("started_at"), "event_count": row.get("event_count"),
                         "run_id": row.get("run_id"), "correlation_id": row.get("correlation_id"),
                         "created_ns": row.get("created_ns", 0)})
    groups = {}
    for row in sorted(recorded, key=lambda r: (r.get("started_at") or "", r.pop("created_ns", 0), r["id"])):
        if row["correlation_id"]:                       # attempts: same run AND same kind (a quality check is not a retry)
            groups.setdefault((row["correlation_id"], row["kind"]), []).append(row)
    for members in groups.values():
        for number, row in enumerate(members, start=1):
            row.update(attempt=number, attempts=len(members))
    rows.extend(recorded)
    for row in reconstructable(None, root):
        rows.append({"id": row["source_id"], "label": row["kind"], "origin": "reconstructed",
                     "department": row["department"], "kind": row["kind"], "readable": row["readable"],
                     "completeness": row.get("completeness"), "outcome": row.get("outcome"),
                     "event_count": row.get("event_count"), "correlation_id": correlation_for(row["source_id"])})
    return {"items": rows[:MAX_TIMELINES], "total": len(rows), "shown_limit": MAX_TIMELINES}


def scene(identifier, root=None):
    if identifier == "demo":
        return demo_scene()
    from ..events.cli import load_timeline
    return scene_from_view(load_timeline(identifier, root))


def _results(path, query_text, root):
    query = parse_qs(query_text, max_num_fields=3, keep_blank_values=True)
    if set(query) != RESULTS_ROUTES[path] or any(len(v) != 1 for v in query.values()) or not TIMELINE.match(query["timeline"][0]):
        raise NetworkError("invalid_results_request", "Use timeline=ID (and position=N for /api/results/at).")
    timeline = query["timeline"][0]
    if path == "/api/results":
        return results_index(timeline, root)
    if path == "/api/results/summary":
        return results_summary(timeline, root)
    if not POSITION.match(query["position"][0]):
        raise NetworkError("invalid_results_request", "position must be a whole number.")
    return results_at(timeline, int(query["position"][0]), root)


def respond(method, target, headers, *, port, root=None):
    """(status, headers, body) for one request. `headers` keys must be lower-case."""
    if method != "GET":
        return _error(405, "method_not_allowed", "The HQ is read-only; only GET is allowed.")
    if not _allowed_origin(headers, port):
        return _error(403, "forbidden_origin", "Requests must come from the local HQ page itself.")
    if len(target) > 300 or any(ch in target for ch in ("\\", "\x00")):
        return _error(404, "not_found", "No such page.")
    parts = urlsplit(target)
    path = parts.path
    if path in FILES:
        if parts.query:
            return _error(404, "not_found", "No such page.")
        name, content_type = FILES[path]
        try:
            body = (STATIC / name).read_bytes()
        except OSError:
            return _error(500, "hq_unavailable", "The HQ page could not be read.")
        return 200, {**HEADERS, "Content-Type": content_type}, body
    try:
        if path == "/api/status":
            return _json(200, {"read_only": True, "service": "vicekrack_living_hq", "version": "1.0"})
        if path == "/api/timelines":
            return _json(200, timelines(root))
        if path == "/api/scene":
            query = parse_qs(parts.query, max_num_fields=2)
            values = query.get("timeline", [])
            if len(values) != 1 or set(query) != {"timeline"} or not TIMELINE.match(values[0]):
                return _error(400, "invalid_timeline_id", "Use demo or a tl-, rar-, srun-, prod- or wfr- ID.")
            return _json(200, scene(values[0], root))
        if path in RESULTS_ROUTES:
            return _json(200, _results(path, parts.query, root))
    except NetworkError as error:
        if error.code in RESULTS_ERRORS:
            status, message = RESULTS_ERRORS[error.code]
            return _error(status, error.code, message)
        status = 404 if error.code.endswith("not_found") else 422 if error.code.startswith("invalid") else 409
        return _error(status, error.code, "The timeline could not be loaded safely.")
    except ValueError:
        return _error(400, "invalid_request", "The request could not be read.")
    except Exception:                                        # noqa: BLE001 - never leak details to the browser
        return _error(500, "hq_error", "The HQ could not load that data.")
    return _error(404, "not_found", "No such page.")
