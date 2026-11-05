"""Pre-submission completeness check + TPA query-risk prediction.

Most TPA queries are predictable: missing duration of ailment, undeclared past history,
alcohol history on GI/trauma cases, MLC copy on accident cases, implant stickers on ortho
cases. Every query adds hours to a patient's discharge. This module catches those gaps
*before* the hospital submits, and tells the desk exactly what the TPA would have asked.

Deliberately rule-based (not LLM): it must be explainable, deterministic and auditable.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date

BLOCKER, WARNING = "blocker", "warning"

IMPLANT_RE = re.compile(r"(?i)\b(orif|plating|nailing|arthroplasty|tkr|thr|knee replacement|hip replacement|ptca|stent|angioplasty|pacemaker|hemiarthroplasty|dhs|k-?wire|mesh repair|hernioplasty|iol)\b")
ALCOHOL_SENSITIVE_RE = re.compile(r"(?i)(pancreatitis|liver|hepat|cirrhosis|gastritis|varice|upper gi bleed|haematemesis|hematemesis|head injury|fracture|rta|road traffic|seizure|cardiomyopathy)")
MATERNITY_RE = re.compile(r"(?i)\b(lscs|caesarean|cesarean|delivery|pregnan|g\d\s*p\d|antenatal)\b")
PREMIUM_ROOM_RE = re.compile(r"(?i)(single|private|deluxe|suite|sharing\s*-\s*1)")

REQUIRED_DOCS = {
    "preauth": {
        "preauth_form": "Signed pre-authorisation request form",
        "id_proof": "Patient photo ID (Aadhaar / PAN)",
        "policy_card": "Insurance e-card / policy copy",
        "admission_note": "Treating doctor's admission note / consultation paper",
    },
    "discharge": {
        "discharge_summary": "Discharge summary signed by treating doctor",
        "final_bill": "Final itemised bill with breakup",
        "investigation_report": "All investigation reports supporting the diagnosis",
    },
}


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    likely_tpa_query: str | None = None
    fix: str | None = None


def check(fields: dict, documents: list[str], stage: str = "preauth", sum_insured: int | None = None,
          today: date | None = None) -> dict:
    today = today or date.today()
    f = {k: (v.strip() if isinstance(v, str) else v) for k, v in fields.items()}
    docs = set(documents)
    out: list[Finding] = []
    dx = " ".join(str(f.get(k) or "") for k in ("provisional_diagnosis", "proposed_procedure"))

    def missing(key: str) -> bool:
        v = f.get(key)
        return v is None or v == "" or (isinstance(v, str) and v.lower() in {"na", "n/a", "-", "nil?"})

    # 1. identity & coverage basics
    for key, label in [("patient_name", "Patient name"), ("admission_date", "Date of admission"),
                       ("treating_doctor", "Treating doctor"), ("provisional_diagnosis", "Provisional diagnosis")]:
        if missing(key):
            out.append(Finding(f"missing_{key}", BLOCKER, f"{label} is missing.", fix=f"Add {label.lower()} from the case sheet."))
    if missing("policy_number") and missing("member_id"):
        out.append(Finding("missing_policy", BLOCKER, "Neither policy number nor member/e-card ID is present.",
                           "Please provide valid policy number / member ID.", "Scan the e-card or ask the patient's attendant."))
    if missing("insurer") and missing("tpa"):
        out.append(Finding("missing_payer", BLOCKER, "Insurer / TPA not identified - cannot route the request.",
                           fix="Pick the TPA from the e-card."))
    if stage == "preauth" and not f.get("estimated_cost"):
        out.append(Finding("missing_estimate", BLOCKER, "Estimated cost is missing.",
                           "Please provide detailed cost estimate.", "Generate the estimate from the tariff/package master."))

    # 2. the classic, predictable TPA queries
    if missing("icd10_code"):
        out.append(Finding("missing_icd", WARNING, "ICD-10 code missing; adjudication will be slower.",
                           fix="Accept the suggested ICD-10 code after doctor review."))
    if missing("duration_of_ailment"):
        out.append(Finding("missing_duration", WARNING, "Duration of present ailment not stated.",
                           "Please provide duration of present ailment with first consultation papers.",
                           "Ask the doctor to add 'Duration of ailment: ...' to the admission note."))
    if missing("past_history"):
        out.append(Finding("missing_past_history", WARNING, "Past history / pre-existing diseases not declared.",
                           "Please confirm past history of DM/HTN/other illness with duration and treatment records.",
                           "Record 'Past history: Nil' or the actual history - never leave it blank."))
    if missing("alcohol_history") and ALCOHOL_SENSITIVE_RE.search(dx):
        out.append(Finding("missing_alcohol_history", BLOCKER,
                           "Diagnosis is alcohol-sensitive (GI/liver/trauma) but alcohol history is not documented.",
                           "Please provide history of alcohol consumption, if any, with duration and quantity.",
                           "Doctor to document alcohol / substance history explicitly."))
    if missing("doctor_registration_no"):
        out.append(Finding("missing_doctor_reg", WARNING, "Treating doctor's registration number missing.",
                           fix="Pull it from the doctor master (Onboarding Copilot flags this at go-live)."))

    # 3. case-type specific
    if f.get("is_rta") and "mlc_fir" not in docs:
        out.append(Finding("rta_without_mlc", BLOCKER, "Accident case without MLC / FIR copy.",
                           "Please submit MLC / FIR copy or a declaration that no MLC was registered.",
                           "Attach MLC copy from casualty."))
    if IMPLANT_RE.search(dx):
        if stage == "discharge" and "implant_invoice" not in docs:
            out.append(Finding("implant_docs", BLOCKER, "Implant procedure: implant invoice and sticker not attached.",
                               "Please submit implant invoice and sticker / barcode.", "Attach invoice + sticker sheet from OT."))
        elif stage == "preauth":
            out.append(Finding("implant_cost_breakup", WARNING, "Implant procedure: show implant cost separately in the estimate.",
                               "Please provide implant type, make and cost breakup.", "Add an implant line to the estimate."))
    if MATERNITY_RE.search(dx) or (f.get("line_of_treatment") or "").lower() == "maternity":
        out.append(Finding("maternity_waiting_period", WARNING,
                           "Maternity claim: most policies have a 9 month - 4 year waiting period / sub-limit.",
                           "Please provide LMP, EDD, gravida/para status and policy maternity cover details.",
                           "Verify maternity cover on the e-card before submitting."))
    if (f.get("line_of_treatment") or "").lower() == "surgical" and missing("proposed_procedure"):
        out.append(Finding("surgical_without_procedure", BLOCKER, "Surgical line of treatment but no procedure named.",
                           "Please specify the planned procedure.", "Add the planned surgery name."))

    # 4. financial
    if sum_insured and f.get("room_type") and PREMIUM_ROOM_RE.search(str(f["room_type"])):
        out.append(Finding("room_rent_capping", WARNING,
                           f"Premium room on a Rs {sum_insured:,} policy: if room rent exceeds ~1% of sum insured/day "
                           "the insurer may apply proportionate deduction to the entire bill.",
                           fix="Inform the attendant in writing or move to an eligible room category."))
    los = f.get("expected_los_days")
    if isinstance(los, int) and los > 10 and (f.get("line_of_treatment") or "").lower() == "medical":
        out.append(Finding("long_los", WARNING, f"Expected stay of {los} days for medical management.",
                           "Please justify the expected length of stay.", "Add clinical justification for LOS."))

    # 5. timing
    if f.get("admission_date"):
        try:
            adm = date.fromisoformat(f["admission_date"])
            if adm > today:
                out.append(Finding("future_admission", WARNING, "Admission date is in the future (planned admission?).",
                                   fix="Mark admission type as Planned."))
            elif (today - adm).days > 2 and stage == "preauth":
                out.append(Finding("late_intimation", WARNING, f"Cashless request {(today - adm).days} days after admission.",
                                   "Please explain the delay in intimation.", "Add a delay-justification note."))
        except ValueError:
            pass

    # 6. documents
    required = dict(REQUIRED_DOCS.get(stage, {}))
    if stage == "preauth" and "admission_note" not in docs and "investigation_report" in docs:
        required.pop("admission_note")
    for doc, label in required.items():
        if doc not in docs:
            out.append(Finding(f"doc_{doc}", BLOCKER if doc != "admission_note" else WARNING,
                               f"Missing document: {label}.", f"Please submit {label.lower()}.", f"Upload {label.lower()}."))

    blockers = [x for x in out if x.severity == BLOCKER]
    warnings = [x for x in out if x.severity == WARNING]
    score = max(0, 100 - 20 * len(blockers) - 6 * len(warnings))
    queries = [x.likely_tpa_query for x in out if x.likely_tpa_query]
    return {
        "stage": stage,
        "score": score,
        "ready_to_submit": not blockers,
        "query_risk": "high" if blockers or len(queries) >= 3 else ("medium" if queries else "low"),
        "blockers": [asdict(x) for x in blockers],
        "warnings": [asdict(x) for x in warnings],
        "predicted_tpa_queries": queries,
    }
