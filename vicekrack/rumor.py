"""Rumor lane (Step 49): opt-in, clearly labelled stories without official confirmation.

The default editorial profiles stay verified-only. A profile that adds a `rumor_lane`
block lets Story Selection pick stories whose claims were never confirmed by a primary
source, under two labels:

  multiple_reports  >= N independent reputable press origins report it (Verification:
                    `corroborated`), but no official source confirms it
  single_report     exactly one reputable press origin reports it (Verification:
                    `insufficient_evidence` with `single_origin_only`); only when the
                    profile sets allow_single_report

Disputed, contradicted, rejected, unrated-only and instruction-like claims never qualify.
A rumor is never upgraded: its claims enter the Story Brief as `unverified`, so the
existing draft path applies (a watermarked draft that needs --allow-draft-preview, and a
quality report that needs human review). On top of that, the label is carried forward:

  - the brief records `editorial.lane = "rumor"` and its label, and must carry the
    profile's label disclosure (burned into every video frame);
  - every beat that narrates an unconfirmed claim must say so (a hedge word such as
    "reportedly", "rumored" or "unconfirmed"), checked before planning;
  - the top warning band reads "RUMOR - UNCONFIRMED" or "MULTIPLE REPORTS - UNCONFIRMED"
    instead of the generic draft label.

Nothing here contacts a network, changes a verification status or invents a fact.
"""

import re

from .errors import NetworkError

LABELS = ("multiple_reports", "single_report")
# The warning band text per label. A label disclosure must start with its band text so the
# renderer can find it in the script's disclosures (the plan carries no other rumor field).
BANDS = {"multiple_reports": "MULTIPLE REPORTS - UNCONFIRMED", "single_report": "RUMOR - UNCONFIRMED"}
REASON = {"multiple_reports": "rumor_multiple_reports", "single_report": "rumor_single_report"}
HEDGE_PREFIX = "Reportedly:"
HEDGE_WORDS = frozenset("""reportedly reported reports report rumor rumors rumored rumour rumours rumoured leak leaks
leaked leaker leakers insiders unconfirmed allegedly alleged claim claims claimed supposedly apparently could may
might""".split())
HEDGE_PHRASES = ("not confirmed", "not yet confirmed", "according to", "sources say", "insiders say")


def lane_config(profile):
    """The profile's rumor lane block when it is enabled, else None."""
    lane = profile.get("rumor_lane")
    return lane if lane and lane["enabled"] else None


def validate_lane(profile):
    """Extra rules the schema cannot express. Raises invalid_editorial_profile."""
    lane = profile.get("rumor_lane")
    if lane is None:
        return
    for label in LABELS:
        if not lane["disclosures"][label].startswith(BANDS[label]):
            raise NetworkError("invalid_editorial_profile",
                               f"Editorial profile rejected: the {label} disclosure must start with '{BANDS[label]}'.")
    if lane["select_min_score"] >= profile["thresholds"]["select_min_score"] + 40:
        raise NetworkError("invalid_editorial_profile",
                           "Editorial profile rejected: the rumor select score is unreachable for a rumor.")


def qualifying_claims(record, lane):
    """(label, claims) for a record with no verified claim, or (None, []) when it does not qualify."""
    claims = record["claims"]
    if any(c["status"] == "verified" for c in claims):
        return None, []
    multiple = [c for c in claims if c["status"] == "corroborated"]
    single = [c for c in claims if c["status"] == "insufficient_evidence"
              and "single_origin_only" in c["rationale_codes"]
              and "claim_contains_instructions" not in c["rationale_codes"]] if lane["allow_single_report"] else []
    if multiple:
        return "multiple_reports", multiple + single
    if single:
        return "single_report", single
    return None, []


def band_label(plan, default):
    """Top warning band text for a rendered scene: the rumor band when the script carries one."""
    if plan["blocked_for_production"]:
        for text in plan["script"].get("disclosures", []):
            for label in LABELS:
                if text.startswith(BANDS[label]):
                    return BANDS[label]
    return default


def is_hedged(text):
    """True when text signals that a claim is reported or unconfirmed, not established."""
    if not text:
        return False
    lowered = " " + " ".join(re.findall(r"[a-z]+", text.lower())) + " "
    return any(f" {word} " in lowered for word in HEDGE_WORDS) or any(f" {p} " in lowered for p in HEDGE_PHRASES)


def check_hedging(brief, script):
    """Every beat that narrates an unconfirmed claim of a rumor brief must say it is unconfirmed."""
    editorial = brief.get("editorial") or {}
    if editorial.get("lane") != "rumor":
        return
    unconfirmed = {c["claim_id"] for c in script["claims"] if c["status"] != "verified"}
    for index, beat in enumerate(script["beats"]):
        if set(beat["claim_ids"]) & unconfirmed and not (is_hedged(beat["narration"])
                                                        or is_hedged(beat["on_screen_text"])):
            raise NetworkError("rumor_not_labeled",
                               f"Beat {index + 1} states an unconfirmed claim without saying it is reported or "
                               "unconfirmed; the script must not present a rumor as fact.")
