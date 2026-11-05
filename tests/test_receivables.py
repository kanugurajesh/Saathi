"""Receivables ledger, CSV intake, ClaimPilot -> receivables, and the daily WhatsApp summary."""
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from app.claims import service
from app.claims.status import Status
from app.receivables import importer, ledger, whatsapp
from scripts.seed import RECEIVABLES, TODAY_SAMPLE, sample_shift_days, seed

from .conftest import set_setting


def _today_file():
    return importer.import_file((RECEIVABLES / TODAY_SAMPLE).read_bytes(), TODAY_SAMPLE, "auto", shift_days=sample_shift_days())


# ------------------------------------------------------------------ vocab + rules
def test_scheme_status_vocabulary():
    m = lambda raw, **kw: importer.map_status(raw, paid=kw.get("paid"), approved=kw.get("approved"), billed=kw.get("billed", 1000))[0]  # noqa: E731
    assert m("Claim Approved - Payment Pending") == "APPROVED"
    assert m("Final approved - payment awaited") == "APPROVED"
    assert m("Claim Paid") == "PAID" and m("Payment Released - UTR 99812") == "PAID"
    assert m("Pending For Clarification") == "QUERY" and m("Query Raised by CPD") == "QUERY"
    assert m("Pending for approval") == "PENDING" and m("Pending at CPD") == "PENDING"
    assert m("Claim Rejected") == "DENIED" and m("Partially Paid") == "PART_PAID"
    assert importer.map_status("Kept in abeyance", paid=None, approved=None, billed=1000) == ("PENDING", "unknown")
    assert importer.map_status("", paid=1000, approved=None, billed=1000) == ("PAID", "amounts")


def test_reason_categories_and_buckets():
    assert ledger.categorize("Room rent proportionate deduction") == "Room rent / proportionate"
    assert ledger.categorize("ICU stay beyond package days not justified") == "Stay or treatment not justified"
    assert ledger.categorize("Claim submitted beyond 15 days of discharge") == "Duplicate / submitted late"
    assert ledger.categorize("Card not active on date of admission") == "Eligibility / exclusion"
    assert [ledger.bucket_for(d) for d in (0, 30, 31, 90, 91, 180, 181)] == ["0-30", "0-30", "31-60", "61-90", "91-180", "91-180", "180+"]


def test_amounts_tds_is_not_a_deduction():
    a = ledger.amounts({"status": "PAID", "billed": 61000, "approved": 61000, "paid": 54900, "tds": 6100})
    assert a == {"outstanding": 0, "deduction": 0, "denied_amount": 0}
    assert ledger.amounts({"status": "APPROVED", "billed": 142000, "approved": 128500})["deduction"] == 13500
    assert ledger.amounts({"status": "DENIED", "billed": 54000})["denied_amount"] == 54000


def test_inr_formatting():
    assert whatsapp.inr(2182100) == "₹21,82,100" and whatsapp.inr(999) == "₹999"
    assert whatsapp.inr_short(2182100) == "₹21.8 L" and whatsapp.inr_short(12345678) == "₹1.23 Cr"


# ------------------------------------------------------------------ seeded ledger
def test_seeded_aging_per_payer(fresh_db):
    seed()
    s = ledger.summary()
    by = {p["payer_type"]: p for p in s["by_payer_type"]}
    assert s["totals"]["outstanding"] == 2182100 and s["totals"]["over_90"] == 1270100 and s["over_90_pct"] == 58
    assert by["aarogyasri"]["outstanding"] == 884600 and by["aarogyasri"]["buckets"]["180+"] == 243600
    assert by["aarogyasri"]["queries"] == 2 and by["pmjay"]["queries"] == 1
    assert by["private_tpa"]["deductions"] == 52500 and by["private_tpa"]["denied_count"] == 1
    assert by["pmjay"]["freshness"]["stale"] and not by["aarogyasri"]["freshness"]["stale"]
    assert {p["payer"] for p in s["by_payer"] if p["payer_type"] == "private_tpa"} == {"Nimbus TPA", "Orbit Insurance", "Lotus Health TPA"}
    assert s["changes"]["new_count"] == 0  # the opening balance is a baseline, not "news"


def test_todays_export_records_payments_denials_and_is_idempotent(fresh_db):
    seed()
    rep = _today_file()
    assert rep["payer_type"] == "aarogyasri" and (rep["created"], rep["updated"], rep["skipped"]) == (1, 5, 0)
    kinds = [(e["kind"], e["amount"]) for e in rep["events"]]
    assert sum(a for k, a in kinds if k == "payment") == 243600
    assert ("denied", 150000) in kinds and ("deduction", 2000) in kinds and ("new", 32000) in kinds
    again = _today_file()
    assert again["events"] == [] and again["unchanged"] == 15
    ch = ledger.summary()["changes"]
    assert ch["received_amount"] == 243600 and ch["received_count"] == 3 and len(ch["denials"]) == 1


def test_bad_files_and_rows(fresh_db):
    csv = b"Case No,Claim Amount,Claim Status,Claim Submitted Date\n,5000,Claim Submitted,01/10/2026\nX-1,,Claim Submitted,01/10/2026\nX-2,7000,Kept in abeyance,\n"
    rep = importer.import_file(csv, "aarogyasri_export.csv")
    assert rep["skipped"] == 2 and rep["created"] == 1
    msgs = " | ".join(i["message"] for i in rep["issues"])
    assert "not recognised" in msgs and "aged from today" in msgs
    try:
        importer.import_file(b"Foo,Bar\n1,2\n", "aarogyasri.csv")
        raise AssertionError("expected IntakeError")
    except importer.IntakeError as exc:
        assert "claim / case number" in str(exc)


# ------------------------------------------------------------------ ClaimPilot -> receivables
def test_bot_claims_become_receivables_at_discharge(fresh_db):
    c = service.create_claim(fields={"patient_name": "Ramesh Goud", "estimated_cost": 85000}, documents=[], portal="nimbus",
                             portal_ref="NB-9", hospital="H", tpa_name="Nimbus TPA", status=Status.PREAUTH_SUBMITTED)
    service.apply_snapshot("nimbus", {"portal_ref": "NB-9", "raw_status": "Approved", "approved_amount": 80000})
    assert ledger.sync_from_claims() == 0  # pre-auth approval is not yet money owed
    service.apply_snapshot("nimbus", {"portal_ref": "NB-9", "raw_status": "Final Approved", "approved_amount": 78500})
    assert ledger.sync_from_claims() == 1 and ledger.sync_from_claims() == 0
    r = fresh_db.receivables.find_one({"_id": "private_tpa:NB-9"})
    assert (r["status"], r["outstanding"], r["deduction"], r["source"], r["claim_id"]) == ("APPROVED", 78500, 6500, "portal_bot", c["_id"])
    service.apply_snapshot("nimbus", {"portal_ref": "NB-9", "raw_status": "Settled", "approved_amount": 78500})
    ledger.sync_from_claims()
    assert fresh_db.receivable_events.find_one({"kind": "payment", "claim_ref": "NB-9"})["amount"] == 78500


# ------------------------------------------------------------------ WhatsApp
def test_summary_message_and_template(fresh_db):
    seed()
    _today_file()
    m = whatsapp.compose()
    assert "₹18,18,500 outstanding" in m["text"] and "Received ₹2,43,600 on 3 claim(s)" in m["text"]
    assert "PM-JAY figures are from a CSV upload" in m["text"] and "AS/2026/041109" in m["text"]
    assert len(m["params"]) == 8 and all("\n" not in p and "\t" not in p for p in m["params"])
    assert len(m["text"]) < 4096


def test_dry_run_then_daily_schedule_sends_once(fresh_db):
    seed()
    assert whatsapp.send()["status"] == "dry_run"
    set_setting("receivables_summary_at", "08:00")
    try:
        early = datetime(2026, 11, 4, 2, 0, tzinfo=timezone.utc)  # 07:30 IST
        assert whatsapp.maybe_send_daily(early) is None
        later = early + timedelta(hours=1)
        assert whatsapp.maybe_send_daily(later)["trigger"] == "schedule"
        assert whatsapp.maybe_send_daily(later + timedelta(minutes=5)) is None
    finally:
        set_setting("receivables_summary_at", "")
    # since-window restarts at the last summary
    assert ledger.summary()["changes"]["received_count"] == 0


def test_cloud_api_payload_and_failure_alert(fresh_db, monkeypatch):
    seed()
    sent = {}

    def fake_post(url, json, headers, timeout):
        sent.update(url=url, json=json, headers=headers)
        return httpx.Response(200, json={"messages": [{"id": "wamid.TEST"}]})

    for k, v in {"whatsapp_token": "t0k", "whatsapp_phone_number_id": "1234", "whatsapp_owner_to": "919876500011"}.items():
        set_setting(k, v)
    try:
        monkeypatch.setattr(httpx, "post", fake_post)
        d = whatsapp.send()
        assert d["status"] == "sent" and d["message_id"] == "wamid.TEST"
        assert sent["url"].endswith("/1234/messages") and sent["headers"]["Authorization"] == "Bearer t0k"
        body = sent["json"]
        assert body["messaging_product"] == "whatsapp" and body["type"] == "template" and body["to"] == "919876500011"
        assert body["template"]["name"] == "receivables_daily_summary"
        monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(400, json={"error": {"message": "Template not found"}}))
        assert whatsapp.send()["status"] == "failed"
        assert fresh_db.alerts.find_one({"kind": "whatsapp_failed"})
    finally:
        for k in ("whatsapp_token", "whatsapp_phone_number_id", "whatsapp_owner_to"):
            set_setting(k, "")


def test_receivables_api(fresh_db, monkeypatch):
    import scripts.seed
    from app.main import app

    # Seeding shifts sample dates to "today"; pin the shift so re-importing the raw file is a true no-op on any date.
    monkeypatch.setattr(scripts.seed, "sample_shift_days", lambda: 0)
    with TestClient(app) as cl:
        s = cl.get("/api/receivables/summary").json()
        assert s["totals"]["outstanding"] == 2182100
        assert cl.post("/api/receivables/sample-today").json()["created"] == 1
        up = cl.post("/api/receivables/import", data={"payer_type": "pmjay"},
                     files={"files": ("pm.csv", (RECEIVABLES / "pmjay_tms_export.csv").read_bytes(), "text/csv")}).json()
        assert up[0]["unchanged"] == 8
        assert cl.post("/api/receivables/import", files={"files": ("x.csv", b"a,b\n1,2\n", "text/csv")}).status_code == 422
        assert cl.post("/api/receivables", json={"payer_type": "ehs"}).status_code == 400
        r = cl.post("/api/receivables", json={"payer_type": "ehs", "claim_ref": "EH/2026/00999", "billed": "45000",
                                              "status": "QUERY", "remarks": "Discharge summary not uploaded"}).json()
        assert r["created"] and r["receivable"]["reason_category"] == "Documents missing"
        assert len(cl.get("/api/receivables?payer_type=ehs&open_only=true").json()) == 6
        assert cl.get("/api/receivables/aging.csv").text.startswith("payer_type,payer,claim_ref")
        assert "₹" in cl.get("/api/receivables/whatsapp/preview").json()["text"]
        assert cl.post("/api/receivables/whatsapp/send").json()["status"] == "dry_run"
        assert len(cl.get("/api/receivables/whatsapp/outbox").json()) == 1
