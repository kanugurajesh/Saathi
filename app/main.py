"""HTTP API + dashboard.   uv run uvicorn app.main:app --port 8000"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from bson import ObjectId
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db as dbmod
from . import hms_webhook, llm
from .claims import completeness, drafting, service
from .claims.extraction import extract_preauth
from .config import ROOT, settings
from .db import clean, get_db, now
from .onboarding import service as onboarding
from .portals import runner
from .receivables import importer as rcv_import
from .receivables import ledger, whatsapp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app")
STATIC = Path(__file__).parent / "static"


def _background_loops():
    """SLA sweep every minute; optional scheduled portal sync."""
    last_sync = 0.0
    while True:
        try:
            service.sla_sweep()
            ledger.sync_from_claims()
            whatsapp.maybe_send_daily()
            hms_webhook.retry_pending()
            if settings.sync_interval_min and time.time() - last_sync > settings.sync_interval_min * 60:
                last_sync = time.time()
                runner.start_background(trigger="schedule")
        except Exception as exc:  # keep the loop alive
            log.warning("background loop error: %s", exc)
        time.sleep(60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    from scripts.seed import seed

    n = seed()
    log.info("DB backend: %s, seeded %d claims, LLM: %s", dbmod.backend, n, llm.engine_info())
    threading.Thread(target=_background_loops, daemon=True, name="loops").start()
    yield


app = FastAPI(title="HextGen Automation Suite - ClaimPilot + Onboarding Copilot + Receivables", lifespan=lifespan)


@app.middleware("http")
async def demo_password_gate(request: Request, call_next):
    """Hosted demo: when DEMO_PASSWORD is set, every page and API needs it (any username).
    /api/health stays open for the platform's health check."""
    if settings.demo_password and request.url.path != "/api/health":
        import base64
        import secrets

        pw = ""
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                pw = base64.b64decode(auth[6:]).decode().partition(":")[2]
            except (ValueError, UnicodeDecodeError):
                pw = ""
        if not secrets.compare_digest(pw.encode(), settings.demo_password.encode()):
            return Response("Password required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="ClaimPilot demo"'})
    return await call_next(request)


def _oid(s: str) -> ObjectId:
    try:
        return ObjectId(s)
    except Exception as exc:
        raise HTTPException(400, "bad id") from exc


# ------------------------------------------------------------------ system

@app.get("/api/health")
def health():
    portals_up = True
    try:
        httpx.get(f"{settings.portal_base_url}/admin/state", timeout=2)
    except httpx.HTTPError:
        portals_up = False
    return {"db": dbmod.backend, "llm": llm.engine_info(), "portal_base_url": settings.portal_base_url,
            "phone_url": "/demo/phone", "hms_demo": bool(settings.hms_demo_url),
            "mock_portals_up": portals_up, "sync_running": runner.is_running(),
            "sla_minutes": {"preauth": settings.sla_preauth_min, "discharge": settings.sla_discharge_min,
                            "query_reply": settings.sla_query_reply_min}}


@app.get("/api/metrics")
def metrics():
    db = get_db()
    claims = list(db.claims.find())
    by_status = Counter(c["status"] for c in claims)
    views = [service.claim_view(c) for c in claims]
    runs = list(db.portal_runs.find({"status": {"$ne": "running"}}))
    otp_auto = sum(1 for r in runs for s in r.get("steps", []) if "OTP received automatically" in s["msg"])
    otp_human = sum(1 for r in runs for s in r.get("steps", []) if "OTP provided by staff" in s["msg"])
    drafts = list(db.drafts.find({}, {"safety": 1, "kind": 1}))
    calls = list(db.llm_calls.find({}, {"latency_ms": 1}))
    return {
        "claims_total": len(claims), "by_status": by_status,
        "open_queries": by_status.get("QUERY_RAISED", 0),
        "sla_breached": sum(1 for v in views if v["sla"] and v["sla"]["breached"]),
        "sla_at_risk": sum(1 for v in views if v["sla"] and v["sla"]["level"] == "at_risk"),
        "pending_amount": sum((c.get("requested_amount") or 0) for c in claims if c["status"] in ("PREAUTH_SUBMITTED", "QUERY_RAISED", "ENHANCEMENT_REQUESTED")),
        "approved_amount": sum((c.get("approved_amount") or 0) for c in claims),
        "runs_total": len(runs), "runs_ok": sum(r["status"] == "success" for r in runs),
        "otp_auto": otp_auto, "otp_human": otp_human,
        "drafts_total": len(drafts), "drafts_ready": sum(d["safety"]["status"] == "ready_to_send" for d in drafts),
        "drafts_blocked_llm": sum(1 for d in drafts if d["safety"].get("rejected_llm_draft")),
        "llm_calls": len(calls), "llm_avg_latency_ms": round(sum(c["latency_ms"] for c in calls) / len(calls)) if calls else None,
        "open_alerts": db.alerts.count_documents({"resolved": False}),
    }


# ------------------------------------------------------------------ claims

@app.get("/api/claims")
def claims():
    return service.list_claims()


@app.get("/api/claims/{claim_id}")
def claim(claim_id: str):
    db = get_db()
    c = db.claims.find_one({"_id": claim_id})
    if not c:
        raise HTTPException(404)
    return {"claim": service.claim_view(c),
            "events": [clean(e) for e in db.claim_events.find({"claim_id": claim_id}).sort("at", -1)],
            "drafts": [clean(d) for d in db.drafts.find({"claim_id": claim_id}).sort("created_at", -1)]}


@app.post("/api/extract")
async def extract(files: list[UploadFile] = File(...), stage: str = Form("preauth"), sum_insured: int | None = Form(None)):
    """AI intake: the first clinical document is extracted; every file is classified by type."""
    blobs = [(await f.read(), f.filename or "upload") for f in files]
    results = [extract_preauth(b, n) for b, n in blobs]
    clinical = next((r for r in results if r["doc_type"] in ("preauth_form", "admission_note", "discharge_summary")), results[0])
    doc_types = sorted({r["doc_type"] for r in results} - {"other"})
    comp = completeness.check(clinical["fields"], doc_types, stage, sum_insured)
    return {"extraction": clinical, "documents": doc_types, "completeness": comp,
            "files": [{"filename": n, "doc_type": r["doc_type"]} for (_, n), r in zip(blobs, results)]}


@app.post("/api/completeness")
def check_completeness(payload: dict = Body(...)):
    return completeness.check(payload.get("fields", {}), payload.get("documents", []), payload.get("stage", "preauth"),
                              payload.get("sum_insured"))


@app.post("/api/claims")
def create(payload: dict = Body(...)):
    c = service.create_claim(fields=payload["fields"], documents=payload.get("documents", []), portal=payload.get("portal"),
                             portal_ref=payload.get("portal_ref") or None, hospital=payload.get("hospital") or "Godavari Demo Multispeciality Hospital, Nalgonda",
                             sum_insured=payload.get("sum_insured"), stage=payload.get("stage", "preauth"),
                             hms_ref=payload.get("hms_ref") or None)
    return service.claim_view(c)


@app.get("/api/claims/by-hms/{hms_ref}")
def claim_by_hms(hms_ref: str):
    """Look a claim up by the HMS's own reference (e.g. IP admission number)."""
    c = get_db().claims.find_one({"hms_ref": hms_ref})
    if not c:
        raise HTTPException(404)
    return service.claim_view(c)


@app.get("/api/hms/outbox")
def hms_outbox(undelivered: bool = False):
    """Claim events queued for the HMS webhook, newest first, with delivery state."""
    q = {"delivered": False} if undelivered else {}
    return [clean(d) for d in get_db().hms_outbox.find(q).sort("_id", -1).limit(100)]


@app.post("/api/claims/{claim_id}/documents")
def add_docs(claim_id: str, payload: dict = Body(...)):
    c = service.add_documents(claim_id, payload.get("documents", []))
    if not c:
        raise HTTPException(404)
    return service.claim_view(c)


@app.post("/api/claims/{claim_id}/draft-reply")
def draft_reply(claim_id: str, payload: dict = Body(default={})):
    c = get_db().claims.find_one({"_id": claim_id})
    if not c:
        raise HTTPException(404)
    text = payload.get("query_text") or (c.get("query") or {}).get("text")
    if not text:
        raise HTTPException(400, "no query text on this claim")
    d = drafting.draft_query_reply(text, {"fields": c["fields"], "documents": c.get("documents", []),
                                          "hospital": c.get("hospital"), "tpa_name": c.get("tpa_name")}, c.get("portal_ref") or claim_id)
    return clean(service.save_draft(claim_id, d))


@app.get("/api/drafts")
def drafts():
    return [clean(d) for d in get_db().drafts.find().sort("created_at", -1).limit(50)]


@app.post("/api/drafts/{draft_id}/mark-sent")
def mark_sent(draft_id: str, payload: dict = Body(default={})):
    db = get_db()
    d = db.drafts.find_one({"_id": _oid(draft_id)})
    if not d:
        raise HTTPException(404)
    if d["safety"]["status"] == "blocked":
        raise HTTPException(409, "blocked drafts cannot be sent")
    body = payload.get("body") or d["body"]
    if drafting.PLACEHOLDER_RE.search(body):
        raise HTTPException(409, "fill every [[placeholder]] before sending")
    db.drafts.update_one({"_id": d["_id"]}, {"$set": {"sent": True, "sent_at": now(), "final_body": body}})
    if d["kind"] == "query_reply":
        db.claims.update_one({"_id": d["claim_id"]}, {"$set": {"query.answered_at": now()}})
    service._event(d["claim_id"], "correspondence_sent", {"kind": d["kind"], "subject": d["subject"]})
    return {"ok": True}


# ------------------------------------------------------------------ portal automation

@app.post("/api/sync")
def sync(payload: dict = Body(default={})):
    started = runner.start_background(payload.get("portals"), trigger="dashboard")
    return {"started": started, "running": runner.is_running()}


@app.get("/api/sync/status")
def sync_status():
    runs = [clean(r) for r in get_db().portal_runs.find().sort("started_at", -1).limit(15)]
    return {"running": runner.is_running(), "runs": runs}


@app.get("/api/runs/{run_id}/screenshot")
def screenshot(run_id: str):
    r = get_db().portal_runs.find_one({"_id": run_id})
    if not r or not r.get("screenshot") or not Path(r["screenshot"]).exists():
        raise HTTPException(404)
    return FileResponse(r["screenshot"])


@app.get("/api/otp/pending")
def otp_pending():
    return [clean(o) for o in get_db().otp_requests.find({"status": "pending"}).sort("created_at", -1)]


@app.post("/api/otp/{req_id}")
def otp_submit(req_id: str, payload: dict = Body(...)):
    upd = {"status": "skipped"} if payload.get("skip") else {"status": "submitted", "otp": str(payload.get("otp", "")).strip()}
    res = get_db().otp_requests.update_one({"_id": _oid(req_id), "status": "pending"}, {"$set": {**upd, "answered_at": now()}})
    if not res.matched_count:
        raise HTTPException(409, "request no longer pending")
    return {"ok": True}


@app.post("/api/sms/inbound")
def sms_inbound(payload: dict = Body(...)):
    """Webhook for an SMS-forwarder app / SMS gateway on the TPA desk phone."""
    get_db().sms_inbox.insert_one({"phone": payload["phone"][-10:], "text": payload["text"], "received_ts": time.time(), "at": now()})
    return {"ok": True}


def _proxy_page(url: str) -> HTMLResponse:
    try:
        r = httpx.get(url, timeout=5)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"not reachable: {url}") from exc
    return HTMLResponse(r.text, status_code=r.status_code)


@app.get("/demo/phone", response_class=HTMLResponse)
def demo_phone():
    """The simulated TPA desk phone, served through the app so it works when the portals are only on localhost."""
    return _proxy_page(f"{settings.portal_base_url}/dev/phone")


@app.get("/demo/hms", response_class=HTMLResponse)
def demo_hms():
    """The demo HMS receiver's live event page (integrations/hms-node), proxied the same way."""
    if not settings.hms_demo_url:
        raise HTTPException(404, "HMS demo receiver not running")
    return _proxy_page(f"{settings.hms_demo_url}/")


def _mock(path: str, payload: dict | None = None):
    try:
        r = httpx.post(f"{settings.portal_base_url}{path}", json=payload, timeout=5)
        return r.json()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"mock portals not reachable at {settings.portal_base_url}: {exc}") from exc


@app.get("/api/sim/state")
def sim_state():
    try:
        return httpx.get(f"{settings.portal_base_url}/admin/state", timeout=3).json()
    except httpx.HTTPError as exc:
        raise HTTPException(502, "mock portals not reachable") from exc


@app.post("/api/sim/advance")
def sim_advance():
    return _mock("/admin/advance")


@app.post("/api/sim/config")
def sim_config(payload: dict = Body(...)):
    return _mock("/admin/config", payload)


@app.post("/api/sim/expire-sessions")
def sim_expire():
    return _mock("/admin/expire-sessions")


@app.post("/api/sim/reset")
def sim_reset():
    from scripts.seed import seed

    _mock("/admin/reset")
    return {"seeded": seed(force=True)}


# ------------------------------------------------------------------ alerts / SLA

@app.get("/api/alerts")
def alerts(include_resolved: bool = False):
    q = {} if include_resolved else {"resolved": False}
    return [clean(a) for a in get_db().alerts.find(q).sort("created_at", -1).limit(100)]


@app.post("/api/alerts/{alert_id}/resolve")
def resolve(alert_id: str):
    get_db().alerts.update_one({"_id": _oid(alert_id)}, {"$set": {"resolved": True, "resolved_at": now()}})
    return {"ok": True}


@app.post("/api/sla/sweep")
def sweep():
    return service.sla_sweep()


# ------------------------------------------------------------------ receivables + daily WhatsApp summary

RCV_SAMPLES = ROOT / "samples" / "receivables"


@app.get("/api/receivables/summary")
def receivables_summary():
    ledger.sync_from_claims()
    return ledger.summary()


@app.get("/api/receivables")
def receivables(payer_type: str | None = None, open_only: bool = False):
    ledger.sync_from_claims()
    return ledger.list_receivables(payer_type, open_only)


@app.post("/api/receivables")
def receivable_manual(payload: dict = Body(...)):
    try:
        res = rcv_import.manual_entry(payload)
    except rcv_import.IntakeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**res, "receivable": clean(get_db().receivables.find_one({"_id": res["id"]}))}


@app.post("/api/receivables/import")
async def receivables_import(files: list[UploadFile] = File(...), payer_type: str = Form("auto")):
    out = []
    for f in files:
        blob = await f.read()
        try:
            out.append(rcv_import.import_file(blob, f.filename or "upload.csv", payer_type))
        except rcv_import.IntakeError as exc:
            raise HTTPException(422, f"{f.filename}: {exc}") from exc
    return out


@app.post("/api/receivables/sample-today")
def receivables_sample_today():
    """Demo: the billing desk uploads today's Aarogyasri export (3 claims paid, 1 denied, 1 new)."""
    from scripts.seed import TODAY_SAMPLE, sample_shift_days

    return rcv_import.import_file((RCV_SAMPLES / TODAY_SAMPLE).read_bytes(), TODAY_SAMPLE, "aarogyasri",
                                  shift_days=sample_shift_days())


@app.get("/api/receivables/aging.csv", response_class=PlainTextResponse)
def receivables_csv():
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["payer_type", "payer", "claim_ref", "patient_name", "package", "status", "submitted_on", "age_days", "bucket",
                "billed", "approved", "paid", "tds", "outstanding", "deduction", "denied_amount", "reason_category", "remarks", "source"])
    for r in ledger.list_receivables():
        w.writerow([r.get(k) for k in ("payer_type", "payer", "claim_ref", "patient_name", "package", "status", "submitted_on",
                                       "age_days", "bucket", "billed", "approved", "paid", "tds", "outstanding", "deduction",
                                       "denied_amount", "reason_category", "remarks", "source")])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="receivables_aging.csv"'})


@app.get("/api/receivables/whatsapp/preview")
def whatsapp_preview():
    m = whatsapp.compose()
    return {k: m[k] for k in ("summary_date", "text", "params", "template", "mode")}


@app.post("/api/receivables/whatsapp/send")
def whatsapp_send():
    return whatsapp.send("manual")


@app.get("/api/receivables/whatsapp/outbox")
def whatsapp_outbox():
    return whatsapp.outbox()


# ------------------------------------------------------------------ onboarding

SAMPLES = ROOT / "samples" / "onboarding"


@app.post("/api/onboarding/jobs")
async def onboarding_job(files: list[UploadFile] = File(...), profile: str = Form("{}")):
    blobs = [(await f.read(), f.filename or "upload.csv") for f in files]
    return onboarding.run_job(blobs, json.loads(profile or "{}"))


@app.post("/api/onboarding/sample")
def onboarding_sample():
    files = [(p.read_bytes(), p.name) for p in sorted(SAMPLES.iterdir()) if p.suffix in (".csv", ".xlsx")]
    return onboarding.run_job(files, json.loads((SAMPLES / "hospital_profile.json").read_text(encoding="utf-8")))


@app.get("/api/onboarding/sample-profile")
def sample_profile():
    return json.loads((SAMPLES / "hospital_profile.json").read_text(encoding="utf-8"))


@app.get("/api/onboarding/jobs")
def onboarding_jobs():
    return [clean(j) for j in get_db().onboarding_jobs.find({}, {"files.validation.clean_rows": 0}).sort("created_at", -1).limit(20)]


@app.get("/api/onboarding/jobs/{job_id}/issues.csv", response_class=PlainTextResponse)
def onboarding_issues(job_id: str):
    j = get_db().onboarding_jobs.find_one({"_id": job_id})
    if not j:
        raise HTTPException(404)
    return PlainTextResponse(onboarding.issues_csv(j), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{job_id}_fix_list.csv"'})


@app.get("/api/onboarding/jobs/{job_id}/clean/{entity}.csv", response_class=PlainTextResponse)
def onboarding_clean(job_id: str, entity: str):
    j = get_db().onboarding_jobs.find_one({"_id": job_id})
    out = onboarding.clean_csv(j, entity) if j else None
    if out is None:
        raise HTTPException(404)
    return PlainTextResponse(out, media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{job_id}_{entity}_import_ready.csv"'})


@app.get("/api/samples/documents")
def sample_documents():
    return sorted(p.name for p in (ROOT / "samples" / "documents").iterdir() if p.is_file())


app.mount("/samples", StaticFiles(directory=ROOT / "samples"), name="samples")
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
