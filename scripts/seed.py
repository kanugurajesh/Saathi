"""Seed the HMS-side claim store from samples/seed_claims.json, and the receivables ledger from
samples/receivables/*.csv (idempotent)."""
from __future__ import annotations

import json
from datetime import date, timedelta

from app.claims import service
from app.claims.status import Status
from app.config import ROOT
from app.db import get_db, now

SEED = ROOT / "samples" / "seed_claims.json"
RECEIVABLES = ROOT / "samples" / "receivables"
# The receivables samples are dated as exported on this day. Seeding shifts every date by (today - this),
# so the demo's aging buckets look the same whenever it is run.
RECEIVABLES_AS_OF = date(2026, 11, 4)
# (file, payer type, hours since that export was "uploaded"). PM-JAY is deliberately 3 days old,
# so the dashboard and the WhatsApp summary show the stale-data warning.
RECEIVABLE_FILES = [("tpa_outstanding_hms_billing.csv", "private_tpa", 20), ("aarogyasri_claims_export.csv", "aarogyasri", 20),
                    ("ehs_claims_export.csv", "ehs", 21), ("pmjay_tms_export.csv", "pmjay", 72)]
TODAY_SAMPLE = "aarogyasri_claims_export_today.csv"
RECEIVABLE_COLLS = ("receivables", "receivable_events", "receivable_imports", "whatsapp_outbox")


def sample_shift_days() -> int:
    from app.receivables.ledger import today_ist

    return (today_ist() - RECEIVABLES_AS_OF).days


def seed_receivables() -> int:
    from app.receivables.importer import import_file

    n = 0
    for name, payer_type, hours_old in RECEIVABLE_FILES:
        rep = import_file((RECEIVABLES / name).read_bytes(), name, payer_type, shift_days=sample_shift_days(),
                          imported_at=now() - timedelta(hours=hours_old), baseline=True)
        n += rep["created"]
    return n


def seed(force: bool = False) -> int:
    db = get_db()
    if db.claims.count_documents({}) and not force:
        return 0
    if force:
        for coll in ("claims", "claim_events", "drafts", "alerts", "portal_runs", "otp_requests", "counters", "sms_inbox",
                     *RECEIVABLE_COLLS):
            db[coll].delete_many({})
    data = json.loads(SEED.read_text(encoding="utf-8"))
    n = 0
    for c in data["claims"]:
        if c.get("in_hms") is False:
            continue
        service.create_claim(fields=c["fields"], documents=c.get("documents", []), portal=c["portal"],
                             portal_ref=c["portal_ref"], hospital=data["hospital"], tpa_name=c.get("tpa_name"),
                             sum_insured=c.get("sum_insured"), status=Status.PREAUTH_SUBMITTED,
                             status_since=now() + timedelta(minutes=c.get("hms_status_since_min", 0)))
        n += 1
    if not db.receivables.count_documents({}):
        seed_receivables()
    return n


if __name__ == "__main__":
    print(f"seeded {seed(force=True)} claims")
