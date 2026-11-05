"""MongoDB access. Uses a real MongoDB when MONGODB_URI is set, otherwise mongomock
so the whole system runs on a laptop with zero infrastructure."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING, MongoClient

from .config import settings

log = logging.getLogger(__name__)
_lock = threading.Lock()
_db = None
backend = "unset"


def now() -> datetime:
    return datetime.now(timezone.utc)


def get_db():
    global _db, backend
    with _lock:
        if _db is not None:
            return _db
        if settings.mongodb_uri:
            client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=3000, tz_aware=True)
            client.admin.command("ping")
            backend = "mongodb"
        else:
            import mongomock

            client = mongomock.MongoClient(tz_aware=True)
            backend = "mongomock (in-memory)"
        _db = client[settings.mongodb_db]
        _ensure_indexes(_db)
        log.info("Database ready: %s", backend)
        return _db


def reset_db_for_tests():
    global _db
    with _lock:
        _db = None


def _ensure_indexes(db):
    # partial (not sparse): claims drafted before submission have portal_ref = null and must not collide
    db.claims.create_index([("portal", ASCENDING), ("portal_ref", ASCENDING)], unique=True,
                           partialFilterExpression={"portal_ref": {"$type": "string"}})
    db.claims.create_index([("status", ASCENDING)])
    db.claim_events.create_index([("claim_id", ASCENDING), ("at", DESCENDING)])
    db.portal_runs.create_index([("started_at", DESCENDING)])
    db.otp_requests.create_index([("status", ASCENDING), ("created_at", DESCENDING)])
    db.alerts.create_index([("created_at", DESCENDING)])
    db.drafts.create_index([("claim_id", ASCENDING), ("created_at", DESCENDING)])
    db.onboarding_jobs.create_index([("created_at", DESCENDING)])
    db.receivables.create_index([("payer_type", ASCENDING), ("status", ASCENDING)])
    db.receivable_events.create_index([("at", DESCENDING)])
    db.receivable_imports.create_index([("payer_type", ASCENDING), ("at", DESCENDING)])
    db.whatsapp_outbox.create_index([("summary_date", ASCENDING), ("trigger", ASCENDING)])


def clean(doc: dict | None) -> dict | None:
    """Make a Mongo document JSON-friendly (ObjectId -> str, datetimes -> iso)."""
    if doc is None:
        return None

    def conv(v):
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            return v.isoformat()
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, list):
            return [conv(x) for x in v]
        if type(v).__name__ == "ObjectId":
            return str(v)
        return v

    return conv(doc)
