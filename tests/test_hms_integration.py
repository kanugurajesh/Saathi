"""HMS integration: hms_ref linking, idempotent create, signed webhook, ordered retry."""
import json
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient

from app import hms_webhook
from app.claims import service
from app.claims.status import Status
from app.db import now
from tests.conftest import set_setting


def _payload(**kw):
    return {"hms_ref": "IP-2026-00451", "portal": "nimbus", "portal_ref": "NB-77", "stage": "preauth",
            "fields": {"patient_name": "Lakshmi Devi", "policy_number": "P-9", "estimated_cost": 85000}, **kw}


def _client():
    from app.main import app
    return TestClient(app)  # no context manager: skips seeding + background loop


def test_create_by_hms_ref_is_idempotent_and_lookup_works(fresh_db):
    c = _client()
    a = c.post("/api/claims", json=_payload()).json()
    b = c.post("/api/claims", json=_payload()).json()  # HMS retried after a timeout
    assert a["_id"] == b["_id"] and fresh_db.claims.count_documents({}) == 1
    assert c.get("/api/claims/by-hms/IP-2026-00451").json()["_id"] == a["_id"]
    assert c.get("/api/claims/by-hms/IP-NOPE").status_code == 404


def test_events_recorded_without_url_and_signed_when_configured(fresh_db, monkeypatch):
    set_setting("hms_webhook_url", "")
    claim = service.create_claim(fields={}, documents=[], portal="nimbus", portal_ref="NB-1", hospital="H",
                                 status=Status.PREAUTH_SUBMITTED, hms_ref="IP-1")
    assert fresh_db.hms_outbox.count_documents({"claim_id": claim["_id"], "delivered": False}) == 1

    sent = []

    def fake_post(url, content, headers, timeout):
        sent.append((url, content, headers))
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "post", fake_post)
    set_setting("hms_webhook_url", "https://hms.example/claimpilot")
    set_setting("hms_webhook_secret", "s3cret")
    try:
        hms_webhook.retry_pending()  # the event held while no URL was set goes out now
        service.apply_snapshot("nimbus", {"portal_ref": "NB-1", "raw_status": "Approved", "approved_amount": 80000})
    finally:
        set_setting("hms_webhook_url", "")
        set_setting("hms_webhook_secret", "")

    kinds = [json.loads(body)["kind"] for _, body, _ in sent]
    assert kinds == ["created", "status_changed"]
    url, body, headers = sent[-1]
    ev = json.loads(body)
    assert ev["hms_ref"] == "IP-1" and ev["status"] == "PREAUTH_APPROVED" and ev["approved_amount"] == 80000
    assert headers["X-ClaimPilot-Event"] == "status_changed"
    assert headers["X-ClaimPilot-Signature"] == hms_webhook.sign(body, "s3cret")
    assert fresh_db.hms_outbox.count_documents({"delivered": False}) == 0


def test_failed_delivery_holds_later_events_and_retries_in_order(fresh_db, monkeypatch):
    up = {"ok": False}
    sent = []

    def fake_post(url, content, headers, timeout):
        if not up["ok"]:
            raise httpx.ConnectError("HMS down")
        sent.append(json.loads(content)["kind"])
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "post", fake_post)
    set_setting("hms_webhook_url", "https://hms.example/claimpilot")
    try:
        claim = service.create_claim(fields={}, documents=[], portal="nimbus", portal_ref="NB-2", hospital="H",
                                     status=Status.PREAUTH_SUBMITTED, hms_ref="IP-2")
        up["ok"] = True  # HMS is back, but the 'created' event is in backoff
        service.apply_snapshot("nimbus", {"portal_ref": "NB-2", "raw_status": "Query Raised"})
        assert sent == []  # held: must not overtake the undelivered 'created' event

        fresh_db.hms_outbox.update_many({}, {"$set": {"next_attempt_at": now() - timedelta(seconds=1)}})
        hms_webhook.retry_pending()
    finally:
        set_setting("hms_webhook_url", "")
    assert sent == ["created", "status_changed"]
    failed = fresh_db.hms_outbox.find_one({"claim_id": claim["_id"], "kind": "created"})
    assert failed["delivered"] and failed["attempts"] == 2
