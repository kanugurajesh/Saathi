"""Canonical claim lifecycle + per-portal vocabulary mapping + SLA clocks.

Every TPA/insurer portal speaks its own dialect ("PA-DEF", "Information Required",
"Query Raised" all mean the same thing). We normalise them into one lifecycle so the
hospital sees one queue, and so SLA timers work regardless of which portal a claim lives on.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from ..config import settings


class Status(StrEnum):
    DRAFT = "DRAFT"
    PREAUTH_SUBMITTED = "PREAUTH_SUBMITTED"
    QUERY_RAISED = "QUERY_RAISED"
    PREAUTH_APPROVED = "PREAUTH_APPROVED"
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED"
    ENHANCEMENT_REQUESTED = "ENHANCEMENT_REQUESTED"
    DISCHARGE_SUBMITTED = "DISCHARGE_SUBMITTED"
    FINAL_APPROVED = "FINAL_APPROVED"
    DENIED = "DENIED"
    SETTLED = "SETTLED"
    UNKNOWN = "UNKNOWN"


TERMINAL = {Status.DENIED, Status.SETTLED}

# Raw portal strings (lower-cased, whitespace-collapsed) -> canonical status.
PORTAL_STATUS_MAP: dict[str, dict[str, Status]] = {
    "nimbus": {
        "pre-auth pending": Status.PREAUTH_SUBMITTED,
        "query raised": Status.QUERY_RAISED,
        "approved": Status.PREAUTH_APPROVED,
        "partially approved": Status.PARTIALLY_APPROVED,
        "enhancement under process": Status.ENHANCEMENT_REQUESTED,
        "final bill under process": Status.DISCHARGE_SUBMITTED,
        "final approved": Status.FINAL_APPROVED,
        "rejected": Status.DENIED,
        "settled": Status.SETTLED,
    },
    "orbit": {
        "pa-init": Status.PREAUTH_SUBMITTED,
        "pa-def": Status.QUERY_RAISED,
        "pa-auth": Status.PREAUTH_APPROVED,
        "pa-part": Status.PARTIALLY_APPROVED,
        "pa-enh": Status.ENHANCEMENT_REQUESTED,
        "fb-recd": Status.DISCHARGE_SUBMITTED,
        "fb-auth": Status.FINAL_APPROVED,
        "pa-deny": Status.DENIED,
        "cl-paid": Status.SETTLED,
    },
    "lotus": {
        "in review": Status.PREAUTH_SUBMITTED,
        "information required": Status.QUERY_RAISED,
        "authorised (full)": Status.PREAUTH_APPROVED,
        "authorised (partial)": Status.PARTIALLY_APPROVED,
        "enhancement review": Status.ENHANCEMENT_REQUESTED,
        "discharge review": Status.DISCHARGE_SUBMITTED,
        "discharge authorised": Status.FINAL_APPROVED,
        "declined": Status.DENIED,
        "payment released": Status.SETTLED,
    },
}

# Cross-portal keyword fallback for strings we have never seen before.
_KEYWORDS: list[tuple[tuple[str, ...], Status]] = [
    (("settled", "paid", "payment released", "utr"), Status.SETTLED),
    (("reject", "declin", "deny", "denied", "repudiat"), Status.DENIED),
    (("query", "deficien", "information required", "shortfall", "additional document"), Status.QUERY_RAISED),
    (("partial",), Status.PARTIALLY_APPROVED),
    (("enhanc",), Status.ENHANCEMENT_REQUESTED),
    (("final approv", "discharge approv", "discharge authori", "final authori"), Status.FINAL_APPROVED),
    (("final bill", "discharge review", "discharge"), Status.DISCHARGE_SUBMITTED),
    (("approv", "authori", "sanction"), Status.PREAUTH_APPROVED),
    (("pending", "review", "process", "submitted", "received"), Status.PREAUTH_SUBMITTED),
]


def _norm(raw: str) -> str:
    return " ".join((raw or "").lower().split())


def normalize(portal: str, raw: str) -> tuple[Status, str]:
    """Map a raw portal status to the canonical lifecycle.

    Returns (status, how) where how is "exact", "keyword" or "unknown". Anything that is not
    an exact match should be surfaced for a human to confirm and then added to the map.
    """
    key = _norm(raw)
    exact = PORTAL_STATUS_MAP.get(portal, {}).get(key)
    if exact:
        return exact, "exact"
    for words, status in _KEYWORDS:
        if any(w in key for w in words):
            return status, "keyword"
    return Status.UNKNOWN, "unknown"


# ---------------------------------------------------------------- SLA clocks
SLA_RULES = {
    # status: (minutes, whose court the ball is in, regulatory basis)
    Status.PREAUTH_SUBMITTED: (lambda: settings.sla_preauth_min, "insurer/TPA",
                               "IRDAI: cashless decision within 1 hour"),
    Status.ENHANCEMENT_REQUESTED: (lambda: settings.sla_preauth_min, "insurer/TPA",
                                   "IRDAI: cashless decision within 1 hour"),
    Status.DISCHARGE_SUBMITTED: (lambda: settings.sla_discharge_min, "insurer/TPA",
                                 "IRDAI: final authorisation within 3 hours of discharge request"),
    Status.QUERY_RAISED: (lambda: settings.sla_query_reply_min, "hospital",
                          "Internal target: answer TPA queries within 2 hours"),
}


def sla_state(status: str, status_since: datetime | None, at: datetime) -> dict | None:
    rule = SLA_RULES.get(Status(status)) if status in Status.__members__ else None
    if not rule or status_since is None:
        return None
    minutes_fn, owner, basis = rule
    due = status_since + timedelta(minutes=minutes_fn())
    left = (due - at).total_seconds() / 60
    return {
        "owner": owner,
        "basis": basis,
        "due_at": due,
        "minutes_left": round(left, 1),
        "breached": left < 0,
        "level": "breached" if left < 0 else ("at_risk" if left < minutes_fn() * 0.25 else "ok"),
    }

