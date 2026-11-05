"""Go-live readiness: combine data issues + configuration checklist into a go/no-go,
an ETA against the promised go-live time, and alerts *before* the deadline slips."""
from __future__ import annotations

import re
from datetime import datetime

from dateutil import parser as dateparser
from pydantic import BaseModel

from .. import llm
from ..db import now
from .schemas import gstin_valid

REQUIRED_WA_TEMPLATES = {"appointment_reminder": "Appointment reminders", "lab_report": "Lab report delivery",
                         "bill": "Bill / receipt", "prescription": "Prescription delivery"}


def _check(cid, area, ok, severity, title, detail, owner, effort_min=0, wait_h=0.0):
    return {"id": cid, "area": area, "status": "pass" if ok else ("fail" if severity == "blocker" else "warn"),
            "severity": severity, "title": title, "detail": detail, "owner": owner,
            "effort_min": 0 if ok else effort_min, "external_wait_h": 0 if ok else wait_h}


def assess(profile: dict, files: list[dict], at: datetime | None = None) -> dict:
    at = at or now()
    modules = set(profile.get("modules") or ["opd", "ipd", "pharmacy", "lab", "insurance"])
    by_entity = {f["entity"]: f for f in files}
    checks = []

    ok, why = gstin_valid(profile.get("gstin"))
    checks.append(_check("gstin", "Compliance", ok, "blocker", "GSTIN valid", why if not ok else profile.get("gstin"),
                         "Hospital accounts", 10))
    hfr = (profile.get("hfr_id") or "").strip().upper()
    checks.append(_check("hfr", "Compliance", bool(re.fullmatch(r"IN\d{10}", hfr)), "warning",
                         "ABDM Health Facility Registry ID", "Needed for ABHA linking / ABDM records" if not hfr else hfr,
                         "Hospital admin", 30))
    depts = [d for d in profile.get("departments") or [] if d]
    checks.append(_check("departments", "Configuration", bool(depts), "blocker", "Departments configured",
                         f"{len(depts)} configured", "Onboarding team", 15))

    staff = by_entity.get("staff")
    checks.append(_check("staff_file", "Data", bool(staff), "blocker", "Staff list imported",
                         "Upload staff list" if not staff else f"{staff['validation']['rows_total']} staff", "Hospital admin", 30))
    if staff:
        rows = staff["validation"]["clean_rows"]
        roles = {r["role"] for r in rows}
        checks.append(_check("has_doctor", "Data", "doctor" in roles, "blocker", "At least one doctor account", "", "Hospital admin", 10))
        checks.append(_check("has_front_desk", "Data", bool(roles & {"receptionist", "billing"}), "warning",
                             "Front desk / billing user exists", "", "Hospital admin", 10))
        unknown = sorted({r["department"] for r in rows if r.get("department") and r["department"] not in depts})
        checks.append(_check("staff_depts", "Configuration", not unknown or not depts, "warning", "Staff departments exist in setup",
                             f"Not configured: {', '.join(unknown[:6])}" if unknown else "", "Onboarding team", 5 * len(unknown)))
        if "pharmacy" in modules:
            checks.append(_check("pharmacist", "Data", "pharmacist" in roles, "warning", "Registered pharmacist account",
                                 "Pharmacy sales need a registered pharmacist user", "Hospital admin", 10))

    checks.append(_check("tariff_file", "Data", "tariff" in by_entity, "blocker", "Tariff / rate card imported",
                         "Billing cannot start without rates", "Hospital accounts", 60))
    if "pharmacy" in modules:
        checks.append(_check("drug_file", "Data", "drug_master" in by_entity, "blocker", "Drug master & opening stock imported",
                             "", "Pharmacy in-charge", 90))
    if "lab" in modules:
        checks.append(_check("lab_file", "Data", "lab_tests" in by_entity, "blocker", "Lab test master imported",
                             "1,700+ default templates can be used, but prices must be set", "Lab in-charge", 45))

    for f in files:
        b = [i for i in f["validation"]["issues"] if i["severity"] == "blocker"]
        w = [i for i in f["validation"]["issues"] if i["severity"] == "warning"]
        checks.append(_check(f"data_{f['entity']}", "Data quality", not b, "blocker",
                             f"{f['label']}: no blocking data errors",
                             f"{len(b)} blocking / {len(w)} warnings in {f['filename']}", "Hospital staff (fix-list sent)",
                             effort_min=2 * len(b) + 10))

    checks.append(_check("letterhead", "Configuration", bool(profile.get("letterhead_uploaded")), "warning",
                         "Letterhead / logo uploaded for prints", "", "Hospital admin", 10))
    checks.append(_check("printer", "Configuration", bool(profile.get("printer_tested")), "warning",
                         "Bill & prescription printer tested", "", "Onboarding team", 15))
    templates = profile.get("whatsapp_templates") or {}
    for key, label in REQUIRED_WA_TEMPLATES.items():
        st = (templates.get(key) or "missing").lower()
        sev = "warning" if st == "pending" else "blocker"
        checks.append(_check(f"wa_{key}", "WhatsApp", st == "approved", sev, f"WhatsApp template approved: {label}",
                             f"status: {st}" + (" - resubmit with compliant wording" if st == "rejected" else ""),
                             "Onboarding team", 15 if st != "pending" else 0, 24.0 if st in ("pending", "rejected", "missing") else 0))
    if "insurance" in modules:
        tpas = profile.get("tpa_empanelments") or []
        checks.append(_check("tpa", "Insurance", bool(tpas), "warning", "TPA / insurer empanelments recorded",
                             f"{len(tpas)} recorded - portal logins can be added to ClaimPilot" if tpas else "Needed for cashless claims",
                             "TPA desk", 20))

    blockers = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    passed = sum(c["status"] == "pass" for c in checks)
    effort_h = sum(c["effort_min"] for c in checks if c["status"] != "pass") / 60
    wait_h = max([c["external_wait_h"] for c in checks if c["status"] != "pass"] or [0])
    eta_h = round(max(effort_h, wait_h), 1)

    hours_left = None
    if profile.get("go_live_at"):
        try:
            target = dateparser.parse(profile["go_live_at"])
            hours_left = round((target - at).total_seconds() / 3600, 1)
        except (ValueError, TypeError):
            pass
    if not blockers:
        verdict = "go"
    elif hours_left is None:
        verdict = "no_go"
    elif eta_h <= hours_left * 0.8:
        verdict = "on_track"
    elif eta_h <= hours_left:
        verdict = "at_risk"
    else:
        verdict = "will_miss"
    return {"score": round(100 * passed / len(checks)), "verdict": verdict, "eta_hours": eta_h, "hours_to_go_live": hours_left,
            "blockers": len(blockers), "warnings": len(warns), "checks": checks}


class Message(BaseModel):
    subject: str
    body: str


def draft_fix_request(profile: dict, readiness: dict, files: list[dict]) -> dict:
    """WhatsApp/email to the hospital's admin with exactly what they need to fix, grouped by owner."""
    todo = [c for c in readiness["checks"] if c["status"] != "pass"]
    todo.sort(key=lambda c: c["severity"] != "blocker")
    top_data = []
    for f in files:
        for s in f["validation"]["summary"][:3]:
            top_data.append(f"{f['filename']}: {s['count']} x {s['message']} -> {s['fix']}")
    name = profile.get("hospital_name") or "your hospital"

    def via_rules():
        lines = [f"Namaste! Go-live update for {name}.", "",
                 f"Readiness: {readiness['score']}% - {readiness['blockers']} blocking item(s), {readiness['warnings']} to review.", ""]
        groups: dict[str, list[str]] = {}
        for c in todo[:12]:
            groups.setdefault(c["owner"], []).append(f"{'[MUST]' if c['severity'] == 'blocker' else '[SHOULD]'} {c['title']}"
                                                     + (f" - {c['detail']}" if c["detail"] else ""))
        for owner, items in groups.items():
            lines.append(f"{owner}:")
            lines += [f"  - {i}" for i in items]
        if top_data:
            lines += ["", "Spreadsheet fixes (row-wise list attached):"] + [f"  - {t}" for t in top_data[:6]]
        lines += ["", "Reply here once done - we re-validate automatically. Thank you!", "- HextGen Onboarding Team"]
        return {"subject": f"{name}: {readiness['blockers']} item(s) needed before go-live", "body": "\n".join(lines)}

    def via_llm():
        prompt = ("Write a short, friendly WhatsApp-style message (max 180 words) from an HMS onboarding team to an Indian "
                  "hospital administrator, listing what they must fix before go-live, grouped by who should do it. "
                  "Use ONLY the facts below; do not add new tasks. Mark blockers as MUST and warnings as SHOULD.\n"
                  f"HOSPITAL: {name}\nREADINESS: {readiness['score']}% verdict={readiness['verdict']}\n"
                  f"OPEN ITEMS: {[{k: c[k] for k in ('title', 'detail', 'owner', 'severity')} for c in todo[:12]]}\n"
                  f"SPREADSHEET FIXES: {top_data[:6]}")
        return llm.generate_json(prompt, Message, task="onboarding_message")

    msg, engine = llm.run_with_fallback("onboarding_message", via_llm, via_rules)
    return {**msg, "engine": engine}
