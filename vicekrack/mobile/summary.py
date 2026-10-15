"""What the phone's Home and System screens show, built only from the HQ's own Studio reads (free; no
provider is contacted) and the mobile server's records. Nothing is estimated, simulated or invented: when
the HQ cannot be reached the screen says so instead of showing numbers.
"""

from datetime import timedelta

from ..errors import NetworkError
from . import security

RESEARCH = {"prepared": "waiting", "submitting": "running", "received": "running", "completed": "done",
            "rejected": "problem", "uncertain": "problem", "processing_failed": "problem"}
WORKFLOW = {"waiting_for_consent": "waiting", "uncertain_submission": "problem",
            "waiting_for_provider": "running", "running": "running",
            "waiting_for_review": "waiting", "review_rejected": "done", "ready_to_export": "done", "exported": "done",
            "failed": "problem", "quality_failed": "problem", "blocked": "problem"}
FINISHED = {"waiting_for_review", "review_rejected", "ready_to_export", "exported"}
MAX_PRODUCTIONS, MAX_DETAILS = 25, 6


def _ok(hq, path):
    status, document = hq.get(path)
    if status != 200:
        raise NetworkError("hq_read_failed", "The HQ could not load that.")
    return document


def _estimate(flow):
    offer = next((a for a in flow.get("actions") or [] if a.get("action") == "workflow_submit_all"), None)
    if offer and isinstance(offer.get("estimate_usd"), (int, float)):
        return offer["estimate_usd"], len(offer.get("scenes") or [])
    paid = [a for a in flow.get("actions") or [] if a.get("action") == "workflow_submit" and a.get("paid")]
    return None, len(paid)


def _progress(flow):
    stages = flow.get("stages") or []
    done = sum(1 for s in stages if s.get("status") in ("completed", "skipped"))
    current = next((s.get("name") for s in stages if s.get("status") not in ("completed", "skipped")), None)
    return {"done": done, "total": len(stages), "stage": current}


def home(hq, store, clock):
    """The Home dashboard. `hq_ok` is False (and lists are empty) when the HQ cannot be read."""
    now = clock()
    settings = store.settings()
    cap, spent = float(settings["daily_cap_usd"]), security.spent_today(store, now)
    doc = {"contract": "mobile_home", "hq_ok": False, "generated_at": security._iso(now),
           "spending": {"today_usd": spent, "cap_usd": cap},
           "counts": {"running": 0, "waiting": 0, "finished": 0, "problems": 0},
           "needs_you": [], "live": [], "recent": []}
    try:
        jobs = _ok(hq, "/api/studio/research/jobs").get("jobs") or []
        productions = _ok(hq, "/api/studio/productions").get("productions") or []
    except NetworkError:
        return doc
    doc["hq_ok"] = True
    counts = doc["counts"]
    for job in jobs:
        kind = RESEARCH.get(job.get("status"))
        label = ", ".join(job.get("topics") or []) or "Research"
        if kind == "waiting":
            counts["waiting"] += 1
            doc["needs_you"].append({"kind": "research_approval", "title": label, "job_id": job["job_id"],
                                     "estimate_usd": None, "at": job.get("created_at")})
        elif kind == "running":
            counts["running"] += 1
            doc["live"].append({"kind": "research", "title": label, "job_id": job["job_id"], "progress": None})
        elif kind == "problem":
            counts["problems"] += 1
    with_flows = sorted((p for p in productions if p.get("workflows")), key=lambda p: p.get("updated_at") or "",
                        reverse=True)[:MAX_PRODUCTIONS]
    details = 0
    for production in with_flows:
        try:
            flows = _ok(hq, f"/api/studio/production?id={production['production_id']}").get("workflows") or []
        except NetworkError:
            continue
        title = production.get("title") or "Untitled video"
        for row in flows:
            status = row.get("status")
            kind = WORKFLOW.get(status)
            if status in FINISHED:
                counts["finished"] += 1
            if kind == "problem":
                counts["problems"] += 1
            if kind not in ("waiting", "running"):
                continue
            counts[kind] += 1
            flow = None
            if details < MAX_DETAILS:
                details += 1
                try:
                    flow = _ok(hq, f"/api/studio/workflow?id={row['workflow_id']}")
                except NetworkError:
                    flow = None
            if kind == "running":
                doc["live"].append({"kind": "video", "title": title, "workflow_id": row["workflow_id"],
                                    "progress": _progress(flow) if flow else None})
            elif status == "waiting_for_review":
                doc["needs_you"].append({"kind": "video_review", "title": title, "workflow_id": row["workflow_id"],
                                         "estimate_usd": None, "at": row.get("updated_at")})
            else:
                estimate, clips = _estimate(flow) if flow else (None, 0)
                doc["needs_you"].append({"kind": "clips_approval", "title": title, "workflow_id": row["workflow_id"],
                                         "clips": clips, "estimate_usd": estimate, "at": row.get("updated_at")})
    for item in doc["needs_you"]:
        if item["kind"] == "research_approval" and details < MAX_DETAILS:
            details += 1
            try:
                estimate = _ok(hq, f"/api/studio/research/job?id={item['job_id']}").get("estimate_usd") or {}
                item["estimate_usd"] = estimate.get("high")
            except NetworkError:
                pass
    doc["needs_you"].sort(key=lambda i: i.get("at") or "", reverse=True)
    try:
        doc["recent"] = (_ok(hq, "/api/studio/overview").get("activity") or [])[:5]
    except NetworkError:
        pass
    return doc


def system(store, clock):
    """Phones, safety settings and the phone's recent actions (from the audit log; no secrets in it)."""
    now = clock()
    settings = store.settings()
    actions = []
    for row in store.audit(15):
        actions.append({k: row.get(k) for k in ("at", "device", "action", "estimate_usd", "outcome")})
    phones = security.list_devices(store)
    guard = store.read("pin_guard.json", {})
    locked = guard.get("locked_until")
    return {"contract": "mobile_system", "phones": phones, "recent_actions": actions,
            "pin": {"set": bool(settings.get("pin")),
                    "locked": bool(locked and security._parse(locked) > now)},
            "spending": {"today_usd": security.spent_today(store, now), "cap_usd": float(settings["daily_cap_usd"]),
                         "timezone": settings["timezone"]},
            "remote": {"address": settings["remote_host"], "ready": bool(settings["remote_host"] and settings["owner_login"])},
            "phone_expiry_days": security.DEVICE_DAYS,
            "soon": [p for p in phones if security._parse(p["expires_at"]) - now < timedelta(days=7)]}
