"""LLM-drafted business correspondence for the TPA desk, with a hard safety gate.

  * reply to a TPA query letter (break the query into asks -> attach / answer / escalate)
  * follow-up / escalation when the insurer breaches the IRDAI turnaround time

Safety contract (enforced *after* generation, for both engines):
  1. Every clinical statement must come from the claim record - never from the model.
  2. Anything unknown becomes a visible [[PLACEHOLDER]] and the draft is marked needs_review.
  3. Listed attachments must actually be on file.
  4. "Nil / denies / no history" claims are blocked unless the record says so.
  5. Rupee amounts must exist in the record.
"""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel

from .. import llm

DOC_LABELS = {
    "preauth_form": "Pre-authorisation form", "id_proof": "Patient ID proof", "policy_card": "Insurance e-card",
    "admission_note": "Admission note / indoor case papers", "investigation_report": "Investigation reports",
    "discharge_summary": "Discharge summary", "final_bill": "Final itemised bill", "implant_invoice": "Implant invoice & sticker",
    "mlc_fir": "MLC / FIR copy", "prior_records": "Previous consultation / treatment records",
}

DOC_KEYWORDS = [
    ("mlc_fir", r"\b(mlc|fir|medico[- ]legal)\b"),
    ("implant_invoice", r"(implant|sticker|barcode)"),
    ("id_proof", r"(id proof|aadhaar|aadhar|pan card|kyc)"),
    ("policy_card", r"(e-?card|policy copy|health card)"),
    ("discharge_summary", r"discharge summary"),
    ("final_bill", r"(final bill|itemi[sz]ed bill|bill break ?up)"),
    ("prior_records", r"(first consultation|previous consultation|prior (treatment )?records|treatment records|old records|past records)"),
    ("admission_note", r"(indoor case papers|icp|case sheet|admission note)"),
    ("investigation_report", r"\b(reports?|usg|ultrasound|ct|cect|mri|x-?ray|lab|investigations?|histopath\w*|ecg|2d echo|echo|"
                             r"platelet|cbp|cbc|amylase|lipase|serum|ns1|igm|culture|trend)\b"),
]

FIELD_KEYWORDS = [
    ("doctor_registration_no", r"registration (number|no)|reg\.? no"),
    ("alcohol_history", r"(alcohol|smok|tobacco|substance|addiction)"),
    ("duration_of_ailment", r"(duration|since when|onset)"),
    ("past_history", r"(past history|pre-?existing|ped\b|co-?morbid|diabet|hypertens|\bdm\b|\bhtn\b)"),
    ("expected_los_days", r"(length of stay|\blos\b|number of days|stay)"),
    ("proposed_procedure", r"(procedure|surgery|line of treatment|indication)"),
    ("estimated_cost", r"(estimate|\bcost\b|tariff|package)"),
]

FIELD_LABELS = {
    "doctor_registration_no": "Treating doctor's registration number",
    "alcohol_history": "Alcohol / substance history", "duration_of_ailment": "Duration of present ailment",
    "past_history": "Past history / pre-existing conditions", "expected_los_days": "Expected length of stay",
    "proposed_procedure": "Line of treatment / procedure", "estimated_cost": "Estimated cost",
}


class Ask(BaseModel):
    ask: str
    category: Literal["document", "clinical", "financial", "administrative"]
    resolution: Literal["attach", "answer", "needs_doctor_input", "needs_document", "needs_billing_input"]
    response: str
    attachment: str | None = None


class QueryReplyDraft(BaseModel):
    asks: list[Ask]
    subject: str
    body: str


class SimpleDraft(BaseModel):
    subject: str
    body: str


# ------------------------------------------------------------ query splitting

_MARKER = r"(?:\d{1,2}[.)]|\(?[ivx]{1,4}[.)]|[-*•])\s+"
_NOISE = re.compile(r"(?i)^(dear|regards|thanking|sincerely|yours|sub(ject)?:|claim no|patient:|policy:|note:)")
_ASK_WORDS = re.compile(r"(?i)(provide|submit|send|attach|clarif|confirm|justify|explain|mention|furnish|copy|history|report|details|records)")


def split_query(text: str) -> list[str]:
    """Break a TPA query letter into individual asks.

    Enumerated items (1. / i) / - bullets, on their own lines or inline) are *all* asks.
    Only un-enumerated prose falls back to sentence splitting + an ask-verb filter.
    """
    m = re.search(r"(?i)(following|below)[^:\n]*:", text)
    body = text[m.end():] if m else text
    body = re.sub(r"[ \t]+(?=(?:\d{1,2}|[ivx]{1,4})\)\s)", "\n", body)  # "submit: i) A ii) B" -> one per line
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    items = [re.sub(rf"^{_MARKER}", "", ln).strip() for ln in lines if re.match(_MARKER, ln)]
    if not items:
        prose = re.sub(r"\s+", " ", body)
        items = [s for s in re.split(r"(?<=[.?])\s+(?=[A-Z])", prose) if _ASK_WORDS.search(s)]
    return [i.rstrip(" .;") for i in items if i and not _NOISE.search(i)][:12]


_RESOLUTION_RANK = {"attach": 0, "answer": 0, "needs_billing_input": 1, "needs_document": 2, "needs_doctor_input": 2}


def _resolve(ask: str, ctx: dict) -> dict:
    """One ask can need a fact *and* a document ("past history with treatment records")."""
    fields, docs = ctx["fields"], set(ctx["documents"])
    low = ask.lower()
    fkey = next((k for k, p in FIELD_KEYWORDS if re.search(p, low)), None)
    dkey = next((k for k, p in DOC_KEYWORDS if re.search(p, low)), None)
    parts: list[str] = []
    resolutions: list[str] = []
    attachment = None
    category = "administrative"

    if fkey:
        val, label = fields.get(fkey), FIELD_LABELS[fkey]
        category = "financial" if fkey == "estimated_cost" else "clinical"
        if val not in (None, ""):
            shown = f"Rs {val:,}" if fkey == "estimated_cost" and isinstance(val, int) else val
            parts.append(f"{label}: {shown} (as documented in the case records).")
            resolutions.append("answer")
        else:
            who = "BILLING" if category == "financial" else "TREATING DOCTOR"
            parts.append(f"{label}: [[{who} TO CONFIRM - {label.lower()}]]")
            resolutions.append("needs_billing_input" if category == "financial" else "needs_doctor_input")
    if dkey:
        label = DOC_LABELS[dkey]
        if not fkey:
            category = "document"
        if dkey in docs:
            parts.append(f"Please find enclosed: {label}.")
            resolutions.append("attach")
            attachment = dkey
        else:
            parts.append(f"[[DOCUMENT REQUIRED - {label}; upload before sending]]")
            resolutions.append("needs_document")
    if not parts:
        return {"category": "administrative", "resolution": "needs_doctor_input",
                "response": "[[TPA DESK TO ANSWER - could not map this request automatically]]", "attachment": None}
    resolution = max(resolutions, key=lambda r: _RESOLUTION_RANK[r])  # worst outstanding item wins
    if _RESOLUTION_RANK[resolution] == 0:
        resolution = "attach" if attachment else "answer"
    return {"category": category, "resolution": resolution, "response": " ".join(parts), "attachment": attachment}


def _compose(ctx: dict, query_ref: str, resolved: list[dict]) -> tuple[str, str]:
    f = ctx["fields"]
    subject = (f"Re: Query on cashless claim {query_ref} - {f.get('patient_name') or '[[PATIENT]]'} - "
               f"Policy {f.get('policy_number') or f.get('member_id') or '[[POLICY]]'}")
    lines = [f"Dear {ctx.get('tpa_name') or f.get('tpa') or 'Claims'} Team,", "",
             f"With reference to your query on claim {query_ref} for {f.get('patient_name') or '[[PATIENT]]'} "
             f"(admitted on {f.get('admission_date') or '[[DOA]]'}, diagnosis: {f.get('provisional_diagnosis') or '[[DIAGNOSIS]]'}), "
             "please find our point-wise response below:", ""]
    for i, r in enumerate(resolved, 1):
        lines.append(f"{i}. {r['ask']}")
        lines.append(f"   Response: {r['response']}")
    atts = [DOC_LABELS[r["attachment"]] for r in resolved if r.get("attachment")]
    if atts:
        lines += ["", "Enclosures:"] + [f"  - {a}" for a in dict.fromkeys(atts)]
    lines += ["", "Kindly process the cashless request at the earliest so that the patient's treatment / discharge is not delayed.",
              "", "Regards,", "TPA Desk", ctx.get("hospital") or "[[HOSPITAL NAME]]"]
    return subject, "\n".join(lines)


# ------------------------------------------------------------ safety gate

PLACEHOLDER_RE = re.compile(r"\[\[[^\]]+\]\]")
NEGATION_RE = re.compile(r"(?i)\b(no (known |significant |past )?history|nil|denies|not applicable|non[- ]alcoholic|no alcohol|teetotal)\b")


def safety_check(subject: str, body: str, attachments: list[str], ctx: dict) -> dict:
    fields, docs = ctx["fields"], set(ctx["documents"])
    record_blob = json.dumps(fields, default=str).lower()
    issues: list[dict] = []
    placeholders = PLACEHOLDER_RE.findall(subject + body)
    for a in attachments:
        if a not in docs:
            issues.append({"type": "phantom_attachment", "detail": f"Draft encloses '{a}' which is not on file", "blocking": True})
    for m in NEGATION_RE.finditer(body):
        window = body[max(0, m.start() - 160): m.end() + 40].lower()
        if PLACEHOLDER_RE.search(window):
            continue
        if m.group(0).lower() not in record_blob:
            issues.append({"type": "unsupported_negative_claim",
                           "detail": f"'{m.group(0)}' is asserted but not present in the clinical record", "blocking": True})
    record_digits = set(re.findall(r"\d{4,}", re.sub(r"[,\s]", "", record_blob)))
    for amt in re.findall(r"(?i)(?:rs\.?|inr|₹)\s*([\d,]{4,})", body):
        if amt.replace(",", "") not in record_digits:
            issues.append({"type": "unsupported_amount", "detail": f"Amount Rs {amt} not found in the record", "blocking": True})
    blocked = any(i["blocking"] for i in issues)
    return {
        "placeholders": placeholders,
        "issues": issues,
        "status": "blocked" if blocked else ("needs_review" if placeholders else "ready_to_send"),
    }


# ------------------------------------------------------------ public API

def draft_query_reply(query_text: str, ctx: dict, query_ref: str = "") -> dict:
    """ctx = {fields: {...extraction fields}, documents: [doc types on file], hospital, tpa_name}"""
    asks = split_query(query_text)

    def via_rules():
        resolved = [{"ask": a, **_resolve(a, ctx)} for a in asks] or [
            {"ask": query_text.strip()[:300], **_resolve(query_text, ctx)}]
        subject, body = _compose(ctx, query_ref, resolved)
        return {"asks": resolved, "subject": subject, "body": body}

    def via_llm():
        prompt = f"""You draft replies from an Indian hospital's TPA desk to an insurer/TPA query letter.

CLAIM RECORD (the ONLY source of facts you may use):
{json.dumps(ctx['fields'], default=str, indent=1)}

DOCUMENTS ON FILE (attachment keys): {sorted(ctx['documents'])}
HOSPITAL: {ctx.get('hospital') or '[[HOSPITAL NAME]]'}    CLAIM REF: {query_ref}

TPA QUERY LETTER:
\"\"\"{query_text}\"\"\"

Instructions:
- Split the letter into individual asks. For each: category, resolution, a one-line response.
- resolution=attach only if the needed document key is in DOCUMENTS ON FILE; set attachment to that key.
- resolution=answer only if the CLAIM RECORD contains the fact. Quote it, do not embellish.
- Otherwise use needs_doctor_input / needs_document / needs_billing_input and write a visible placeholder
  like [[TREATING DOCTOR TO CONFIRM - alcohol history]]. NEVER write 'nil', 'no history', 'denies' unless the record says so.
- Write a courteous, concise point-wise email (subject + body) signed 'TPA Desk, <hospital>'. Plain text."""
        return llm.generate_json(prompt, QueryReplyDraft, task="query_reply_draft")

    draft, engine = llm.run_with_fallback("query_reply_draft", via_llm, via_rules)
    attachments = [a["attachment"] for a in draft["asks"] if a.get("attachment")]
    safety = safety_check(draft["subject"], draft["body"], attachments, ctx)
    if safety["status"] == "blocked" and engine != "deterministic":
        # LLM draft violated the contract: fall back to the deterministic composer, keep the evidence.
        rejected = {"engine": engine, "issues": safety["issues"]}
        draft = via_rules()
        attachments = [a["attachment"] for a in draft["asks"] if a.get("attachment")]
        safety = safety_check(draft["subject"], draft["body"], attachments, ctx)
        engine = "deterministic (llm draft rejected by safety gate)"
        safety["rejected_llm_draft"] = rejected
    return {"kind": "query_reply", "engine": engine, **draft, "attachments": attachments, "safety": safety}


def draft_followup(claim: dict, sla: dict) -> dict:
    f = claim.get("fields", {})
    ctx_fields = {"patient_name": f.get("patient_name"), "policy_number": f.get("policy_number"),
                  "portal_ref": claim.get("portal_ref"), "status": claim.get("status"),
                  "minutes_overdue": round(-sla["minutes_left"]), "basis": sla["basis"]}

    def via_rules():
        subject = f"Escalation: cashless decision overdue - {claim.get('portal_ref')} - {f.get('patient_name')}"
        body = "\n".join([
            f"Dear {claim.get('tpa_name') or 'Claims'} Team,", "",
            f"The cashless request {claim.get('portal_ref')} for {f.get('patient_name')} (policy {f.get('policy_number')}) "
            f"has been pending for {ctx_fields['minutes_overdue']} minutes beyond the permitted turnaround.",
            f"As per the IRDAI Master Circular on Health Insurance (29-May-2024) - {sla['basis']}.", "",
            "The patient is waiting at the hospital. We request you to share the decision immediately, or let us know "
            "if any further information is required from our side.", "",
            "Regards,", "TPA Desk", claim.get("hospital") or "[[HOSPITAL NAME]]"])
        return {"subject": subject, "body": body}

    def via_llm():
        prompt = ("Write a firm but polite escalation email from an Indian hospital TPA desk to the TPA because a cashless "
                  "decision is overdue. Use ONLY these facts, cite the IRDAI basis, under 140 words, plain text, sign as "
                  f"'TPA Desk, {claim.get('hospital') or '[[HOSPITAL NAME]]'}'.\nFACTS: {json.dumps(ctx_fields)}")
        return llm.generate_json(prompt, SimpleDraft, task="followup_draft")

    draft, engine = llm.run_with_fallback("followup_draft", via_llm, via_rules)
    safety = safety_check(draft["subject"], draft["body"], [], {"fields": {**f, **ctx_fields}, "documents": []})
    return {"kind": "sla_escalation", "engine": engine, **draft, "attachments": [], "safety": safety}
