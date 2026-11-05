"""Daily receivables summary for the hospital owner, over WhatsApp.

A daily summary is a *business-initiated* message, so the WhatsApp Cloud API only delivers it as a
pre-approved template (utility category). Template parameters cannot contain line breaks, so the
template carries the headline numbers; the full multi-line breakdown below is what the dashboard
shows, and what a free-form reply can carry once the owner has messaged within the 24-hour window.

Without WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_OWNER_TO the message is written to the
`whatsapp_outbox` collection only (dry run), so the demo never messages a real phone.

Template to register in WhatsApp Manager (name: receivables_daily_summary, category: utility, language: en):
  Receivables for {{1}}: {{2}} outstanding across {{3}} claims. Over 90 days: {{4}}.
  Received since the last summary: {{5}}. New denials: {{6}}. Largest payer: {{7}}. {{8}}
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, time, timedelta

import httpx

from ..alerts import raise_alert
from ..config import settings
from ..db import clean, get_db, now
from . import ledger

log = logging.getLogger(__name__)


def inr(n: float | int | None) -> str:
    """Indian digit grouping: 1234567 -> ₹12,34,567."""
    n = round(n or 0)
    s, neg = str(abs(n)), n < 0
    head, tail = s[:-3], s[-3:]
    head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
    return ("-" if neg else "") + "₹" + (f"{head},{tail}" if head else tail)


def inr_short(n: float | int | None) -> str:
    n = n or 0
    if abs(n) >= 1e7:
        return f"₹{n / 1e7:.2f} Cr"
    if abs(n) >= 1e5:
        return f"₹{n / 1e5:.1f} L"
    return inr(n)


def _ago(hours: float | None) -> str:
    if hours is None:
        return "never"
    return f"{hours:.0f} h ago" if hours < 48 else f"{hours / 24:.0f} days ago"


def compose(at: datetime | None = None) -> dict:
    at = at or now()
    ledger.sync_from_claims()
    s = ledger.summary(at)
    t, ch = s["totals"], s["changes"]
    local = at.astimezone(ledger.IST)
    when = f"{local:%a} {local.day} {local:%b %Y}"
    lines = [f"*Receivables · {settings.hospital_name}*", f"{when}, {local:%I:%M %p} IST", "",
             f"*{inr(t['outstanding'])} outstanding* across {t['count']} claims",
             f"Over 90 days: *{inr(t['over_90'])}* ({s['over_90_pct']}%)", "", "*By payer*"]
    for p in sorted(s["by_payer_type"], key=lambda x: -x["outstanding"]):
        if not p["count"]:
            continue
        old = next((b for b in reversed(s["buckets"]) if p["buckets"][b]), None)
        lines.append(f"• {p['label']}: {inr_short(p['outstanding'])} ({p['count']})"
                     + (f", {old} days: {inr_short(p['buckets'][old])}" if old and old != "0-30" else ""))
    lines += ["", "*Since the last summary*"]
    if ch["received_count"]:
        by = ", ".join(f"{x['payer']} {inr_short(x['amount'])}" for x in ch["received_by_payer"])
        lines.append(f"✅ Received {inr(ch['received_amount'])} on {ch['received_count']} claim(s) ({by})")
    else:
        lines.append("✅ No payments received")
    for d in ch["denials"][:3]:
        lines.append(f"❌ Denied: {ledger.PAYER_TYPES[d['payer_type']]} {d['claim_ref']}, {inr(d['amount'])}"
                     + (f" ({d['reason_category'].lower()})" if d.get("reason_category") else ""))
    if len(ch["denials"]) > 3:
        lines.append(f"❌ …and {len(ch['denials']) - 3} more denial(s)")
    if ch["deduction_count"]:
        lines.append(f"✂️ New deductions: {inr(ch['deduction_amount'])} on {ch['deduction_count']} claim(s)")
    if ch["new_count"]:
        lines.append(f"🆕 {ch['new_count']} claim(s) added, {inr(ch['new_amount'])}")

    actions = []
    for p in s["by_payer_type"]:
        if p["queries"]:
            actions.append(f"• {p['queries']} {p['label']} claim(s) waiting on our reply to a query")
    if s["top_deductions"]:
        d = s["top_deductions"][0]
        actions.append(f"• Top deduction reason: {d['category'].lower()} ({inr_short(d['amount'])} on {d['count']} claims)")
    if actions:
        lines += ["", "*Needs action*", *actions]

    stale = [p for p in s["by_payer_type"] if p["count"] and p["freshness"]["stale"]]
    if stale:
        lines.append("")
        for p in stale:
            f = p["freshness"]
            lines.append(f"⚠️ {p['label']} figures are from a {f['how'] or 'upload'} {_ago(f.get('age_hours'))}"
                         + ("" if p["payer_type"] in ledger.BOT_FED else ". Upload today's export to refresh."))
    lines += ["", f"Full aging: {settings.dashboard_url}"]
    text = "\n".join(lines)

    biggest = max(s["by_payer_type"], key=lambda x: x["outstanding"])
    den = f"{len(ch['denials'])} ({inr(ch['denied_amount'])})" if ch["denials"] else "none"
    params = [when, inr(t["outstanding"]), str(t["count"]), f"{inr(t['over_90'])} ({s['over_90_pct']}%)",
              f"{inr(ch['received_amount'])} on {ch['received_count']} claims" if ch["received_count"] else "nothing",
              den, f"{biggest['label']} {inr(biggest['outstanding'])}",
              ("Stale data: " + ", ".join(p["label"] for p in stale) + ".") if stale else "All payer data is current."]
    params = [_template_safe(p) for p in params]
    return {"summary_date": local.date().isoformat(), "text": text, "params": params, "summary": s,
            "template": {"name": settings.whatsapp_template, "language": {"code": settings.whatsapp_template_lang},
                         "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]},
            "mode": mode()}


def _template_safe(p: str) -> str:
    # Cloud API rejects template parameters with newlines, tabs or more than 4 consecutive spaces.
    return re.sub(r" {4,}", "   ", re.sub(r"[\n\t]+", " ", p)).strip() or "-"


def configured() -> bool:
    return bool(settings.whatsapp_token and settings.whatsapp_phone_number_id and settings.whatsapp_owner_to)


def mode() -> dict:
    to = settings.whatsapp_owner_to
    return {"live": configured(), "to": f"+{to[:2]} ••••• {to[-4:]}" if to else None,
            "schedule_ist": settings.receivables_summary_at or None, "template": settings.whatsapp_template}


def send(trigger: str = "manual", at: datetime | None = None) -> dict:
    msg = compose(at)
    doc = {"summary_date": msg["summary_date"], "trigger": trigger, "text": msg["text"], "params": msg["params"],
           "template": msg["template"]["name"], "to": mode()["to"], "created_at": now()}
    if not configured():
        doc.update(status="dry_run", detail="WhatsApp Cloud API not configured; stored in outbox only")
    else:
        url = f"https://graph.facebook.com/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages"
        payload = {"messaging_product": "whatsapp", "to": settings.whatsapp_owner_to, "type": "template",
                   "template": msg["template"]}
        try:
            r = httpx.post(url, json=payload, headers={"Authorization": f"Bearer {settings.whatsapp_token}"}, timeout=10)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text[:300]}
            if r.status_code < 300:
                doc.update(status="sent", message_id=(body.get("messages") or [{}])[0].get("id"))
            else:
                err = body.get("error", {})
                doc.update(status="failed", detail=f"HTTP {r.status_code}: {err.get('message') or body}")
        except httpx.HTTPError as exc:
            doc.update(status="failed", detail=f"{type(exc).__name__}: {exc}")
        if doc["status"] == "failed":
            raise_alert("whatsapp_failed", "warning", "Daily receivables summary not delivered on WhatsApp", doc["detail"],
                        dedupe_key=f"wa:{msg['summary_date']}")
    get_db().whatsapp_outbox.insert_one(doc)
    log.info("receivables summary %s (%s)", doc["status"], trigger)
    return clean(doc)


def maybe_send_daily(at: datetime | None = None) -> dict | None:
    """Called every minute by the background loop: sends once per IST day at RECEIVABLES_SUMMARY_AT."""
    if not settings.receivables_summary_at:
        return None
    at = at or now()
    local = at.astimezone(ledger.IST)
    try:
        hh, mm = (int(x) for x in settings.receivables_summary_at.split(":"))
        due = time(hh, mm)
    except ValueError:
        log.warning("RECEIVABLES_SUMMARY_AT must be HH:MM, got %r", settings.receivables_summary_at)
        return None
    if local.time() < due:
        return None
    day = local.date().isoformat()
    box = get_db().whatsapp_outbox
    if box.find_one({"summary_date": day, "trigger": "schedule", "status": {"$in": ["sent", "dry_run"]}}):
        return None
    if box.find_one({"summary_date": day, "trigger": "schedule", "status": "failed", "created_at": {"$gt": at - timedelta(minutes=15)}}):
        return None  # back off instead of hitting the API every minute while it is failing
    return send("schedule", at)


def outbox(limit: int = 20) -> list[dict]:
    return [clean(d) for d in get_db().whatsapp_outbox.find().sort("created_at", -1).limit(limit)]
