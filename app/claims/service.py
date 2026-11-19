"""Claim store + the event engine that turns portal snapshots into actions.

apply_snapshot() is the heart of the pipeline:
    portal row  ->  normalise status  ->  diff against Mongo  ->  event log  ->  triggers
Triggers: query raised -> fetch letter + auto-draft reply; partial approval -> shortfall alert;
denial -> critical alert; unknown vocabulary -> 'teach me' alert.  sla_sweep() runs the
IRDAI turnaround clocks and drafts escalations when the insurer is late.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pymongo import ReturnDocument

from .. import hms_webhook
from ..alerts import raise_alert, resolve_alerts
from ..db import clean, get_db, now
from . import bill_scrub, completeness, drafting
from .status import TERMINAL, Status, normalize, sla_state

log = logging.getLogger(__name__)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def next_claim_id() -> str:
    c = get_db().counters.find_one_and_update({"_id": "claim"}, {"$inc": {"seq": 1}}, upsert=True,
                                              return_document=ReturnDocument.AFTER)
    return f"HX-CLM-{c['seq']:04d}"


def create_claim(*, fields: dict, documents: list[str], portal: str | None, portal_ref: str | None,
                 hospital: str, tpa_name: str | None = None, sum_insured: int | None = None, stage: str = "preauth",
                 status: str = Status.DRAFT, status_since: datetime | None = None, hms_ref: str | None = None) -> dict:
    """hms_ref links the claim to the HMS record (e.g. the IP admission number). Creating a claim with an
    hms_ref that already exists returns the existing claim, so the HMS can safely retry its call."""
    db = get_db()
    if hms_ref and (existing := db.claims.find_one({"hms_ref": hms_ref})):
        return existing
    ts = now()
    doc = {
        "_id": next_claim_id(), "hms_ref": hms_ref, "fields": fields, "documents": sorted(set(documents)), "portal": portal,
        "portal_ref": portal_ref, "hospital": hospital, "tpa_name": tpa_name or fields.get("tpa"),
        "sum_insured": sum_insured, "stage": stage, "status": str(status), "status_raw": None,
        "status_since": status_since or ts, "requested_amount": fields.get("estimated_cost"),
        "approved_amount": None, "query": None, "created_at": ts, "updated_at": ts, "last_synced_at": None,
    }
    doc["completeness"] = completeness.check(fields, doc["documents"], stage, sum_insured)
    db.claims.insert_one(doc)
    _event(doc["_id"], "created", {"status": doc["status"]})
    return doc


def _event(claim_id: str, kind: str, data: dict) -> None:
    get_db().claim_events.insert_one({"claim_id": claim_id, "kind": kind, "data": data, "at": now()})
    hms_webhook.emit(claim_id, kind, data)


def add_documents(claim_id: str, doc_types: list[str]) -> dict | None:
    db = get_db()
    claim = db.claims.find_one({"_id": claim_id})
    if not claim:
        return None
    docs = sorted(set(claim.get("documents", [])) | set(doc_types))
    comp = completeness.check(claim["fields"], docs, claim.get("stage", "preauth"), claim.get("sum_insured"))
    db.claims.update_one({"_id": claim_id}, {"$set": {"documents": docs, "completeness": comp, "updated_at": now()}})
    _event(claim_id, "documents_added", {"types": doc_types})
    return db.claims.find_one({"_id": claim_id})


# ------------------------------------------------------------------ sync engine

def apply_snapshot(portal: str, snap: dict) -> dict:
    """snap = {portal_ref, raw_status, approved_amount?, query_text?, patient_name?, policy_number?}"""
    db = get_db()
    status, how = normalize(portal, snap["raw_status"])
    claim = db.claims.find_one({"portal": portal, "portal_ref": snap["portal_ref"]})
    if claim is None:
        # Claim exists on the portal but not in the HMS: register it so nothing is invisible.
        claim = create_claim(fields={"patient_name": snap.get("patient_name"), "policy_number": snap.get("policy_number")},
                             documents=[], portal=portal, portal_ref=snap["portal_ref"],
                             hospital="(discovered on portal)", status=Status.UNKNOWN)
        raise_alert("discovered_claim", "warning", f"Claim {snap['portal_ref']} found on {portal} but not in HMS",
                    "Link it to the IP admission.", claim_id=claim["_id"], dedupe_key=f"disc:{portal}:{snap['portal_ref']}")

    ts = now()
    prev_status = claim["status"]
    update: dict = {"last_synced_at": ts, "status_raw": snap["raw_status"], "status_mapping": how}
    if snap.get("approved_amount") is not None:
        update["approved_amount"] = snap["approved_amount"]
    changed = status != claim["status"]
    if changed:
        update.update({"status": str(status), "status_since": ts, "updated_at": ts})
        if status in (Status.DISCHARGE_SUBMITTED, Status.FINAL_APPROVED, Status.SETTLED):
            update["stage"] = "discharge"
    if snap.get("query_text") and status == Status.QUERY_RAISED:
        update["query"] = {"text": snap["query_text"], "raised_at": ts if changed else (claim.get("query") or {}).get("raised_at", ts),
                           "answered_at": None}
    db.claims.update_one({"_id": claim["_id"]}, {"$set": update})
    claim = db.claims.find_one({"_id": claim["_id"]})

    if how == "unknown":
        raise_alert("unmapped_status", "warning", f"Unmapped status '{snap['raw_status']}' on {portal}",
                    "Add it to PORTAL_STATUS_MAP so the lifecycle stays accurate.", claim_id=claim["_id"],
                    dedupe_key=f"unmapped:{portal}:{snap['raw_status'].lower()}")
    if changed:
        _event(claim["_id"], "status_changed", {"from": prev_status, "to": str(status),
                                                "raw": snap["raw_status"], "portal": portal})
        resolve_alerts(f"sla:{claim['_id']}:")
        _on_status_change(claim, status)
    return {"claim_id": claim["_id"], "status": str(status), "changed": changed, "mapping": how}


def _on_status_change(claim: dict, status: Status) -> None:
    f, ref, cid = claim.get("fields", {}), claim.get("portal_ref"), claim["_id"]
    who = f"{f.get('patient_name') or 'Unknown patient'} ({ref})"
    if status == Status.QUERY_RAISED:
        q = (claim.get("query") or {}).get("text")
        if q:
            draft = drafting.draft_query_reply(q, {"fields": f, "documents": claim.get("documents", []),
                                                   "hospital": claim.get("hospital"), "tpa_name": claim.get("tpa_name")}, ref)
            save_draft(cid, draft)
            todo = len(draft["safety"]["placeholders"])
            raise_alert("query_raised", "warning", f"TPA query on {who}",
                        f"Reply auto-drafted ({draft['safety']['status']}, {todo} item(s) need input). Hospital SLA: 2h.",
                        claim_id=cid, dedupe_key=f"query:{cid}:{ref}")
        else:
            raise_alert("query_raised", "warning", f"TPA query on {who}", "Query letter could not be fetched - open portal.",
                        claim_id=cid, dedupe_key=f"query:{cid}:{ref}")
    elif status == Status.PARTIALLY_APPROVED:
        req, appr = claim.get("requested_amount") or 0, claim.get("approved_amount") or 0
        raise_alert("partial_approval", "warning", f"Partial approval on {who}",
                    f"Approved Rs {appr:,} of Rs {req:,} (shortfall Rs {max(req - appr, 0):,}). Inform patient / raise enhancement.",
                    claim_id=cid, dedupe_key=f"partial:{cid}")
    elif status == Status.DENIED:
        raise_alert("denied", "critical", f"Cashless DENIED for {who}", "Inform patient; switch to reimbursement or appeal.",
                    claim_id=cid, dedupe_key=f"denied:{cid}")
    elif status in (Status.PREAUTH_APPROVED, Status.FINAL_APPROVED):
        raise_alert("approved", "info", f"{status.replace('_', ' ').title()} - {who}",
                    f"Approved amount Rs {(claim.get('approved_amount') or 0):,}.", claim_id=cid)


def save_draft(claim_id: str, draft: dict) -> dict:
    doc = {"claim_id": claim_id, **draft, "created_at": now(), "sent": False}
    get_db().drafts.insert_one(doc)
    return doc


def sla_sweep(at: datetime | None = None) -> list[dict]:
    at = at or now()
    db = get_db()
    fired = []
    for claim in db.claims.find({"status": {"$nin": [str(s) for s in TERMINAL]}}):
        sla = sla_state(claim["status"], _as_utc(claim.get("status_since")), at)
        if not sla or sla["level"] == "ok":
            continue
        key = f"sla:{claim['_id']}:{claim['status']}:{sla['level']}"
        who = f"{claim['fields'].get('patient_name')} ({claim.get('portal_ref')})"
        if sla["owner"] == "hospital":
            title = f"Reply to TPA query overdue - {who}" if sla["breached"] else f"TPA query reply due soon - {who}"
            a = raise_alert("sla", "critical" if sla["breached"] else "warning", title, sla["basis"],
                            claim_id=claim["_id"], dedupe_key=key)
        else:
            a = raise_alert("sla", "critical" if sla["breached"] else "warning",
                            f"{'IRDAI TAT breached' if sla['breached'] else 'IRDAI TAT at risk'} - {who}",
                            f"{sla['basis']}. {abs(sla['minutes_left']):.0f} min {'overdue' if sla['breached'] else 'left'}.",
                            claim_id=claim["_id"], dedupe_key=key)
            if a and sla["breached"]:
                save_draft(claim["_id"], drafting.draft_followup(claim, sla))
        if a:
            fired.append(clean(a))
    return fired


# ------------------------------------------------------------------ discharge bill check

def _given(d: dict | None) -> dict:
    return {k: v for k, v in (d or {}).items() if v not in (None, "")}


def bill_check(lines: list[dict], policy: dict | None = None, context: dict | None = None, *,
               claim_id: str | None = None, source_file: str | None = None, parse: dict | None = None) -> dict | None:
    """Run the bill check. With a claim_id, policy and context default from the claim (sum insured, pre-auth
    approval, expected stay...), and the result is saved on the claim: audit event, HMS webhook, alerts.
    Returns None when the claim does not exist."""
    db = get_db()
    claim = db.claims.find_one({"_id": claim_id}) if claim_id else None
    if claim_id and not claim:
        return None
    pol, ctx = _given(policy), _given(context)
    if claim:
        f = claim.get("fields") or {}
        pol = {**_given({"sum_insured": claim.get("sum_insured")}), **_given(claim.get("policy")), **pol}
        ctx = {**_given({"patient_name": f.get("patient_name"), "payer": claim.get("tpa_name") or f.get("insurer"),
                         "hospital": claim.get("hospital"), "los_days": f.get("expected_los_days"),
                         "approved_amount": claim.get("approved_amount")}), **ctx}
    result = bill_scrub.scrub(lines, pol, ctx)
    result.update({"source_file": source_file, "parse": parse, "claim_id": claim_id})
    if not claim:
        return result

    ts = now()
    doc = {**result, "created_at": ts}
    db.bill_checks.insert_one(doc)
    result["_id"] = str(doc["_id"])
    summary = {"check_id": result["_id"], "at": ts, "source_file": source_file,
               **{k: result[k] for k in ("bill_total", "expected_payable", "patient_share", "predicted_deduction",
                                         "deduction_pct", "by_reason")},
               "blockers": sum(x["severity"] == "blocker" for x in result["findings"])}
    db.claims.update_one({"_id": claim_id}, {"$set": {"bill_check": summary, "policy": pol, "updated_at": ts}})
    _event(claim_id, "bill_checked", {k: result[k] for k in ("bill_total", "expected_payable", "patient_share")})

    inr = bill_scrub._inr
    who = f"{ctx.get('patient_name') or 'Unknown patient'} ({claim.get('portal_ref') or claim_id})"
    resolve_alerts(f"bill:{claim_id}:")  # a re-check replaces the previous check's alerts
    enh = next((x for x in result["findings"] if x["rule"] == "enhancement"), None)
    if enh:
        raise_alert("enhancement_needed", "critical", f"Raise an enhancement before discharge - {who}", enh["message"],
                    claim_id=claim_id, dedupe_key=f"bill:{claim_id}:enhancement")
    if result["patient_share"] > 0:
        why = ", ".join(f"{r['category'].lower()} {inr(r['amount'])}" for r in result["by_reason"][:3])
        raise_alert("patient_share", "warning", f"Patient pays ~{inr(result['patient_share'])} at discharge - {who}",
                    f"Tell the family before discharge ({why}). The note for the family is drafted in Discharge bill check.",
                    claim_id=claim_id, dedupe_key=f"bill:{claim_id}:share")
    return result


def latest_bill_check(claim_id: str) -> dict | None:
    return get_db().bill_checks.find_one({"claim_id": claim_id}, sort=[("created_at", -1)])


# ------------------------------------------------------------------ read models

def claim_view(claim: dict, at: datetime | None = None) -> dict:
    at = at or now()
    c = clean(claim)
    c["sla"] = clean(sla_state(claim["status"], _as_utc(claim.get("status_since")), at))
    c["age_min"] = round((at - _as_utc(claim["status_since"])).total_seconds() / 60) if claim.get("status_since") else None
    return c


def list_claims() -> list[dict]:
    at = now()
    return [claim_view(c, at) for c in get_db().claims.find().sort("updated_at", -1)]
