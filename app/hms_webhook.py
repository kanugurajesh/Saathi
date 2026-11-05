"""Outbound claim events to the hospital's HMS (e.g. HextGen IPD billing).

Every claim event (created, status_changed, documents_added, correspondence_sent) is written to the
`hms_outbox` collection. When HMS_WEBHOOK_URL is set, it is POSTed there as JSON, signed with
HMAC-SHA256 over the raw body:

    X-ClaimPilot-Event:     status_changed
    X-ClaimPilot-Signature: sha256=<hex digest of body using HMS_WEBHOOK_SECRET>

Failed deliveries stay in the outbox and are retried by the background loop with backoff, so a
brief HMS outage loses nothing. The receiver should treat `event_id` as an idempotency key.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import timedelta

import httpx
from bson import ObjectId

from .config import settings
from .db import get_db, now

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 8


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(doc: dict) -> dict:
    return {"event_id": str(doc["_id"]), "kind": doc["kind"], "claim_id": doc["claim_id"], "hms_ref": doc.get("hms_ref"),
            "status": doc.get("status"), "approved_amount": doc.get("approved_amount"), "data": doc["data"],
            "at": doc["at"].isoformat()}


def _deliver(doc: dict) -> bool:
    body = json.dumps(_payload(doc), default=str, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "X-ClaimPilot-Event": doc["kind"]}
    if settings.hms_webhook_secret:
        headers["X-ClaimPilot-Signature"] = sign(body, settings.hms_webhook_secret)
    attempts = doc.get("attempts", 0) + 1
    try:
        r = httpx.post(settings.hms_webhook_url, content=body, headers=headers, timeout=3)
        ok, err = 200 <= r.status_code < 300, None if r.status_code < 300 else f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        ok, err = False, str(exc) or type(exc).__name__
    update = {"attempts": attempts, "delivered": ok, "last_error": err, "last_attempt_at": now()}
    if not ok:
        update["next_attempt_at"] = now() + timedelta(seconds=min(30 * 2 ** (attempts - 1), 3600))
        log.warning("HMS webhook %s for %s failed (attempt %d): %s", doc["kind"], doc["claim_id"], attempts, err)
    get_db().hms_outbox.update_one({"_id": doc["_id"]}, {"$set": update})
    return ok


def _blocked(claim_id: str, before: ObjectId) -> bool:
    """An older event for this claim is still undelivered: hold this one so the HMS sees events in order."""
    return get_db().hms_outbox.find_one({"claim_id": claim_id, "delivered": False, "attempts": {"$lt": MAX_ATTEMPTS},
                                         "_id": {"$lt": before}}) is not None


def emit(claim_id: str, kind: str, data: dict) -> None:
    """Record a claim event for the HMS and try to deliver it once, right away (unless an older one is stuck)."""
    db = get_db()
    claim = db.claims.find_one({"_id": claim_id}) or {}
    ts = now()
    doc = {"_id": ObjectId(), "claim_id": claim_id, "hms_ref": claim.get("hms_ref"), "kind": kind, "data": data,
           "status": claim.get("status"), "approved_amount": claim.get("approved_amount"), "at": ts,
           "delivered": False, "attempts": 0, "next_attempt_at": ts}
    db.hms_outbox.insert_one(doc)
    if settings.hms_webhook_url and not _blocked(claim_id, doc["_id"]):
        _deliver(doc)


def retry_pending() -> int:
    """Called from the background loop: redeliver held or failed events, oldest first, in order per claim."""
    if not settings.hms_webhook_url:
        return 0
    q = {"delivered": False, "attempts": {"$lt": MAX_ATTEMPTS}, "next_attempt_at": {"$lte": now()}}
    sent, stuck = 0, set()
    for doc in list(get_db().hms_outbox.find(q).sort("_id", 1).limit(200)):
        if doc["claim_id"] in stuck or _blocked(doc["claim_id"], doc["_id"]):
            stuck.add(doc["claim_id"])
            continue
        if _deliver(doc):
            sent += 1
        else:
            stuck.add(doc["claim_id"])
    return sent
