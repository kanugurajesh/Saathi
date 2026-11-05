"""Receivables ledger: money owed to the hospital, per payer, by how long it has been unpaid.

Where the numbers come from (and the dashboard says so per payer):
  * Private TPAs / insurers: derived from ClaimPilot's bot-synced claims once they reach the discharge stage,
    plus an opening balance imported from the HMS billing ledger.
  * Aarogyasri, EHS, PM-JAY: CSV/XLSX exports uploaded by the billing desk, or manual entry. The bots do NOT
    log into these government portals, so these figures are only as fresh as the last upload; every payer
    carries a last-updated time and the daily summary warns when one is stale.

Amounts are kept exact (rules, not LLM): this is money the owner will act on.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from ..db import clean, get_db, now

PAYER_TYPES = {"private_tpa": "Private TPAs / insurers", "aarogyasri": "Aarogyasri", "ehs": "EHS", "pmjay": "PM-JAY"}
DEFAULT_PAYER = {"private_tpa": "Private TPA", "aarogyasri": "Aarogyasri Health Care Trust",
                 "ehs": "EHS (Employees Health Scheme)", "pmjay": "PM-JAY (Ayushman Bharat)"}
BOT_FED = {"private_tpa"}  # the only payer type the portal bots can update
STATUSES = ("PENDING", "QUERY", "APPROVED", "PART_PAID", "PAID", "DENIED")
OPEN = {"PENDING", "QUERY", "APPROVED", "PART_PAID"}
BUCKETS = [("0-30", 0, 30), ("31-60", 31, 60), ("61-90", 61, 90), ("91-180", 91, 180), ("180+", 181, None)]
STALE_AFTER = timedelta(hours=26)  # a daily export that is older than this is flagged
IST = timezone(timedelta(hours=5, minutes=30))  # no DST, so a fixed offset is exact

# Why money was cut or refused. First match wins, so the more specific groups come first.
REASON_CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("Room rent / proportionate", ("room rent", "proportionate", "room category", "eligible room")),
    ("Non-payable items / consumables", ("non payable", "non-payable", "nonpayable", "consumable", "gloves", "drapes",
                                         "admin charge", "registration charge")),
    ("Stay or treatment not justified", ("not justified", "unjustified", "excess stay", "length of stay", "not required")),
    ("Package / tariff rate", ("package", "tariff", "rate difference", "agreed rate", "mou", "ceiling", "capped",
                               "capping", "above mrp")),
    ("Co-pay / policy limit", ("co-pay", "copay", "co pay", "sub-limit", "sublimit", "sum insured", "limit exhausted")),
    ("Eligibility / exclusion", ("not eligible", "eligibility", "exclusion", "waiting period", "pre-existing", "card not",
                                 "not active", "not covered", "beneficiary not")),
    ("Duplicate / submitted late", ("duplicate", "already settled", "beyond", "late submission", "time barred", "delay in submission")),
    ("Documents missing", ("document", "report", "not uploaded", "not submitted", "not enclosed", "missing", "photograph",
                           "photo", "sticker", "invoice", "x-kub", "summary")),
]


def today_ist(at: datetime | None = None) -> date:
    return (at or now()).astimezone(IST).date()


def categorize(reason: str | None) -> str | None:
    if not reason:
        return None
    r = reason.lower()
    for cat, words in REASON_CATEGORIES:
        if any(w in r for w in words):
            return cat
    return "Other"


def bucket_for(days: int | None) -> str | None:
    if days is None:
        return None
    for label, lo, hi in BUCKETS:
        if days >= lo and (hi is None or days <= hi):
            return label
    return BUCKETS[0][0]  # future-dated rows count as current


def amounts(r: dict) -> dict:
    """outstanding = what the payer still owes; deduction = billed minus what the payer agreed or paid.
    TDS withheld by the payer counts as settled (it is a tax credit, not a loss)."""
    billed = r.get("billed") or 0
    approved = r.get("approved")
    settled = (r.get("paid") or 0) + (r.get("tds") or 0)
    explicit = r.get("deduction_explicit")  # a deduction column in the export, if any (kept apart from the computed field)
    if r["status"] == "DENIED":
        return {"outstanding": 0, "deduction": 0, "denied_amount": billed}
    if r["status"] == "PAID":
        ded = explicit if explicit is not None else max(billed - settled, 0)
        return {"outstanding": 0, "deduction": ded, "denied_amount": 0}
    if approved is not None:
        expected, ded = approved, (explicit if explicit is not None else max(billed - approved, 0))
    else:
        ded = explicit or 0
        expected = billed - ded
    return {"outstanding": max(expected - settled, 0), "deduction": ded, "denied_amount": 0}


def rid_for(payer_type: str, claim_ref: str) -> str:
    return f"{payer_type}:{claim_ref.strip().upper()}"


_VOLATILE = {"updated_at", "source", "source_file"}


def upsert(rec: dict, *, source: str, source_file: str | None = None, claim_id: str | None = None,
           baseline: bool = False) -> dict:
    """Insert or update one receivable and record what changed (payment, denial, new deduction...).

    Blank cells never erase known values, so a thinner export cannot wipe data. baseline=True loads
    an opening state without producing "since yesterday" events.
    """
    db = get_db()
    rid = rid_for(rec["payer_type"], rec["claim_ref"])
    old = db.receivables.find_one({"_id": rid})
    ts = now()
    doc = {**(old or {}), **{k: v for k, v in rec.items() if v is not None}}
    doc.update({"_id": rid, "claim_ref": rec["claim_ref"].strip(), "source": source,
                "source_file": source_file or doc.get("source_file"), "claim_id": claim_id or doc.get("claim_id")})
    doc.setdefault("payer", DEFAULT_PAYER[doc["payer_type"]])
    doc.setdefault("submitted_on", today_ist().isoformat())
    if doc["status"] == "PAID" and not doc.get("paid_on"):
        doc["paid_on"] = today_ist().isoformat()
    doc.update(amounts(doc))
    show_reason = doc["deduction"] > 0 or doc["status"] in ("DENIED", "QUERY")
    doc["reason_category"] = categorize(doc.get("remarks")) if show_reason else None

    if old and {k: v for k, v in old.items() if k not in _VOLATILE | {"created_at"}} == \
            {k: v for k, v in doc.items() if k not in _VOLATILE | {"created_at"}}:
        return {"id": rid, "created": False, "changed": False, "events": []}
    doc["updated_at"] = ts
    doc.setdefault("created_at", ts)
    db.receivables.replace_one({"_id": rid}, doc, upsert=True)

    events = [] if baseline else _diff(old, doc)
    for e in events:
        db.receivable_events.insert_one({**e, "receivable_id": rid, "payer_type": doc["payer_type"], "payer": doc["payer"],
                                         "claim_ref": doc["claim_ref"], "patient_name": doc.get("patient_name"),
                                         "remarks": doc.get("remarks"), "reason_category": doc.get("reason_category"),
                                         "source": source, "at": ts})
    return {"id": rid, "created": old is None, "changed": True, "events": events}


def _diff(old: dict | None, new: dict) -> list[dict]:
    if old is None:
        ev = [{"kind": "new", "amount": new["outstanding"] or new.get("billed") or 0}]
        if new["status"] == "DENIED":
            ev.append({"kind": "denied", "amount": new["denied_amount"]})
        return ev
    ev = []
    paid_delta = ((new.get("paid") or 0) + (new.get("tds") or 0)) - ((old.get("paid") or 0) + (old.get("tds") or 0))
    if paid_delta > 0:
        ev.append({"kind": "payment", "amount": paid_delta})
    if new["status"] == "DENIED" and old["status"] != "DENIED":
        ev.append({"kind": "denied", "amount": new["denied_amount"]})
    if new["deduction"] > (old.get("deduction") or 0):
        ev.append({"kind": "deduction", "amount": new["deduction"] - (old.get("deduction") or 0)})
    if new["status"] != old["status"] and not ev:
        ev.append({"kind": "status", "amount": 0, "from": old["status"], "to": new["status"]})
    if new["status"] == "QUERY" and old["status"] != "QUERY":
        ev.append({"kind": "query", "amount": new["outstanding"]})
    return ev


def record_import(payer_type: str, filename: str, report: dict, at: datetime | None = None) -> None:
    get_db().receivable_imports.insert_one({"payer_type": payer_type, "filename": filename, "at": at or now(),
                                            **{k: report.get(k, 0) for k in ("rows_total", "created", "updated", "skipped")}})


# ------------------------------------------------------------------ private TPAs from ClaimPilot

CLAIM_TO_RECEIVABLE = {"DISCHARGE_SUBMITTED": "PENDING", "QUERY_RAISED": "QUERY", "FINAL_APPROVED": "APPROVED",
                       "SETTLED": "PAID", "DENIED": "DENIED"}


def sync_from_claims() -> int:
    """A cashless claim becomes a receivable at discharge. Idempotent: unchanged claims write nothing.
    Billed = the claim's requested amount (in production, the HMS final bill)."""
    db = get_db()
    n = 0
    for c in db.claims.find({"stage": "discharge", "portal_ref": {"$type": "string"}}):
        st = CLAIM_TO_RECEIVABLE.get(c["status"])
        if not st:
            continue
        f = c.get("fields") or {}
        approved = c.get("approved_amount") if st in ("APPROVED", "PAID") else None
        exists = db.receivables.find_one({"_id": rid_for("private_tpa", c["portal_ref"])}, {"_id": 1})
        rec = {"payer_type": "private_tpa", "payer": c.get("tpa_name") or c.get("portal"), "claim_ref": c["portal_ref"],
               "patient_name": f.get("patient_name"), "uhid": f.get("uhid"),
               "package": f.get("proposed_procedure") or f.get("provisional_diagnosis"),
               "billed": c.get("requested_amount") or approved, "approved": approved,
               "paid": approved if st == "PAID" else None, "status": st,
               "submitted_on": None if exists else today_ist(c.get("status_since")).isoformat()}
        if rec["billed"] is None:
            continue
        n += upsert(rec, source="portal_bot", claim_id=c["_id"])["changed"]
    return n


# ------------------------------------------------------------------ read models

def view(r: dict, as_of: date) -> dict:
    v = clean(r)
    sub = date.fromisoformat(r["submitted_on"]) if r.get("submitted_on") else None
    v["age_days"] = (as_of - sub).days if sub and r["status"] in OPEN else None
    v["bucket"] = bucket_for(v["age_days"])
    return v


def list_receivables(payer_type: str | None = None, open_only: bool = False, at: datetime | None = None) -> list[dict]:
    q: dict = {}
    if payer_type:
        q["payer_type"] = payer_type
    if open_only:
        q["status"] = {"$in": sorted(OPEN)}
    d0 = today_ist(at)
    rows = [view(r, d0) for r in get_db().receivables.find(q)]
    return sorted(rows, key=lambda r: (r["status"] not in OPEN, -(r["age_days"] or 0), -r["outstanding"]))


def _agg(rows: list[dict]) -> dict:
    a = {"outstanding": 0, "count": 0, "buckets": {b[0]: 0 for b in BUCKETS}, "bucket_counts": {b[0]: 0 for b in BUCKETS},
         "over_90": 0, "oldest_days": None, "queries": 0, "billed": 0, "deductions": 0, "deduction_count": 0,
         "denied_amount": 0, "denied_count": 0}
    for r in rows:
        a["billed"] += r.get("billed") or 0
        if r["deduction"]:
            a["deductions"] += r["deduction"]
            a["deduction_count"] += 1
        if r["status"] == "DENIED":
            a["denied_amount"] += r["denied_amount"]
            a["denied_count"] += 1
        if r["status"] in OPEN and r["outstanding"] > 0:
            a["outstanding"] += r["outstanding"]
            a["count"] += 1
            a["buckets"][r["bucket"]] += r["outstanding"]
            a["bucket_counts"][r["bucket"]] += 1
            if (r["age_days"] or 0) > 90:
                a["over_90"] += r["outstanding"]
            a["oldest_days"] = max(a["oldest_days"] or 0, r["age_days"] or 0)
            a["queries"] += r["status"] == "QUERY"
    return a


def _top_reasons(rows: list[dict], amount_key: str) -> list[dict]:
    acc: dict[str, list] = defaultdict(lambda: [0, 0])
    for r in rows:
        if r.get(amount_key):
            acc[r.get("reason_category") or "Reason not captured"][0] += r[amount_key]
            acc[r.get("reason_category") or "Reason not captured"][1] += 1
    return [{"category": k, "amount": v[0], "count": v[1]} for k, v in sorted(acc.items(), key=lambda kv: -kv[1][0])]


def last_summary_at() -> datetime | None:
    d = get_db().whatsapp_outbox.find_one({"status": {"$in": ["sent", "dry_run"]}}, sort=[("created_at", -1)])
    return d["created_at"] if d else None


def changes_since(since: datetime) -> dict:
    evs = list(get_db().receivable_events.find({"at": {"$gt": since}}).sort("at", 1))
    pick = lambda k: [e for e in evs if e["kind"] == k]  # noqa: E731
    pay, den, ded, new, qry = pick("payment"), pick("denied"), pick("deduction"), pick("new"), pick("query")
    return {"since": since, "received_amount": sum(e["amount"] for e in pay), "received_count": len(pay),
            "received_by_payer": _sum_by(pay), "denials": [clean(e) for e in den], "denied_amount": sum(e["amount"] for e in den),
            "deduction_amount": sum(e["amount"] for e in ded), "deduction_count": len(ded),
            "new_count": len(new), "new_amount": sum(e["amount"] for e in new), "new_queries": len(qry)}


def _sum_by(evs: list[dict]) -> list[dict]:
    acc: dict[str, int] = defaultdict(int)
    for e in evs:
        acc[PAYER_TYPES[e["payer_type"]]] += e["amount"]
    return [{"payer": k, "amount": v} for k, v in sorted(acc.items(), key=lambda kv: -kv[1])]


def freshness(at: datetime | None = None) -> dict:
    """When each payer's figures were last refreshed as a whole, and how (portal bots or CSV upload)."""
    at = at or now()
    db = get_db()
    out = {}
    for pt in PAYER_TYPES:
        imp = db.receivable_imports.find_one({"payer_type": pt}, sort=[("at", -1)])
        cand = []
        if imp:
            cand.append((imp["at"], "CSV upload", imp["filename"]))
        if pt in BOT_FED:
            run = db.portal_runs.find_one({"status": "success"}, sort=[("finished_at", -1)])
            if run and run.get("finished_at"):
                cand.append((run["finished_at"], "portal bots", run.get("portal")))
        if not cand:
            out[pt] = {"at": None, "how": None, "detail": None, "stale": True, "bot_fed": pt in BOT_FED}
            continue
        ts, how, detail = max(cand, key=lambda c: _utc(c[0]))
        out[pt] = {"at": clean({"t": _utc(ts)})["t"], "how": how, "detail": detail,
                   "age_hours": round((at - _utc(ts)).total_seconds() / 3600, 1),
                   "stale": at - _utc(ts) > STALE_AFTER, "bot_fed": pt in BOT_FED}
    return out


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def summary(at: datetime | None = None, since: datetime | None = None) -> dict:
    at = at or now()
    rows = list_receivables(at=at)
    by_type, by_payer = defaultdict(list), defaultdict(list)
    for r in rows:
        by_type[r["payer_type"]].append(r)
        by_payer[(r["payer_type"], r["payer"])].append(r)
    fresh = freshness(at)
    since = since or last_summary_at() or at - timedelta(hours=24)
    total = _agg(rows)
    return {
        "as_of": today_ist(at).isoformat(), "generated_at": clean({"t": at})["t"],
        "buckets": [b[0] for b in BUCKETS], "totals": total,
        "over_90_pct": round(100 * total["over_90"] / total["outstanding"]) if total["outstanding"] else 0,
        "deduction_pct": round(100 * total["deductions"] / total["billed"], 1) if total["billed"] else 0,
        "by_payer_type": [{"payer_type": pt, "label": PAYER_TYPES[pt], "freshness": fresh[pt], **_agg(by_type.get(pt, []))}
                          for pt in PAYER_TYPES],
        "by_payer": sorted([{"payer_type": pt, "payer": p, **_agg(rs)} for (pt, p), rs in by_payer.items()],
                           key=lambda x: (list(PAYER_TYPES).index(x["payer_type"]), -x["outstanding"])),
        "top_deductions": _top_reasons(rows, "deduction"), "top_denials": _top_reasons(rows, "denied_amount"),
        "changes": clean(changes_since(since)),
    }
