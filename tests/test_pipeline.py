"""Event engine + API (no browser)."""
from datetime import timedelta

from fastapi.testclient import TestClient

from app.claims import service
from app.claims.status import Status
from app.config import ROOT
from app.db import now
from scripts.seed import seed


def _claim(**kw):
    return service.create_claim(fields={"patient_name": "Syed Imran", "policy_number": "P1", "estimated_cost": 120000,
                                        "provisional_diagnosis": "Acute pancreatitis"},
                                documents=["investigation_report"], portal="nimbus", portal_ref="NB-1", hospital="H",
                                status=Status.PREAUTH_SUBMITTED, **kw)


def test_query_raised_triggers_event_alert_and_draft(fresh_db):
    c = _claim()
    r = service.apply_snapshot("nimbus", {"portal_ref": "NB-1", "raw_status": "Query Raised",
                                          "query_text": "Please provide:\n1. Alcohol history\n2. Serum amylase report"})
    assert r["changed"] and r["status"] == "QUERY_RAISED"
    ev = fresh_db.claim_events.find_one({"claim_id": c["_id"], "kind": "status_changed"})
    assert ev["data"]["from"] == "PREAUTH_SUBMITTED" and ev["data"]["to"] == "QUERY_RAISED"
    draft = fresh_db.drafts.find_one({"claim_id": c["_id"]})
    assert draft["kind"] == "query_reply" and draft["attachments"] == ["investigation_report"]
    assert fresh_db.alerts.find_one({"kind": "query_raised"})
    # same snapshot again: no new event / draft (idempotent sync)
    assert not service.apply_snapshot("nimbus", {"portal_ref": "NB-1", "raw_status": "Query Raised"})["changed"]
    assert fresh_db.drafts.count_documents({}) == 1


def test_unknown_status_and_discovered_claims_alert(fresh_db):
    _claim()
    service.apply_snapshot("nimbus", {"portal_ref": "NB-1", "raw_status": "Kept in abeyance"})
    service.apply_snapshot("nimbus", {"portal_ref": "NB-404", "raw_status": "Approved", "patient_name": "New"})
    kinds = {a["kind"] for a in fresh_db.alerts.find()}
    assert {"unmapped_status", "discovered_claim"} <= kinds


def test_sla_sweep_escalates_once(fresh_db):
    _claim(status_since=now() - timedelta(minutes=90))
    first = service.sla_sweep()
    second = service.sla_sweep()
    assert len(first) == 1 and second == []
    d = fresh_db.drafts.find_one({"kind": "sla_escalation"})
    assert "IRDAI" in d["body"] and d["safety"]["status"] == "ready_to_send"


def test_api_smoke(fresh_db):
    from app.main import app

    with TestClient(app) as cl:
        assert cl.get("/api/health").json()["llm"]["engine"] == "deterministic"
        assert len(cl.get("/api/claims").json()) == 9
        doc = (ROOT / "samples" / "documents" / "05_maternity_preauth_lscs.txt").read_bytes()
        r = cl.post("/api/extract", files={"files": ("lscs.txt", doc, "text/plain")}).json()
        assert r["extraction"]["fields"]["policy_number"] == "GGI-FL-7781230"
        assert any(w["rule"] == "maternity_waiting_period" for w in r["completeness"]["warnings"])
        created = cl.post("/api/claims", json={"fields": r["extraction"]["fields"], "documents": r["documents"]}).json()
        assert created["_id"] == "HX-CLM-0010"
        job = cl.post("/api/onboarding/sample").json()
        assert cl.get(f"/api/onboarding/jobs/{job['_id']}/issues.csv").status_code == 200
        # a draft with placeholders cannot be sent
        cid = "HX-CLM-0001"
        d = cl.post(f"/api/claims/{cid}/draft-reply", json={"query_text": "1. Alcohol history\n2. MLC copy"}).json()
        assert cl.post(f"/api/drafts/{d['_id']}/mark-sent", json={}).status_code == 409


def test_seed_is_idempotent(fresh_db):
    assert seed() == 9 and seed() == 0
