"""AI intake: turn admission notes / discharge summaries / pre-auth forms into structured
pre-auth data, then *verify* every extracted value against the source (grounding check).

Why the grounding check matters: a hallucinated policy number or diagnosis on a cashless
request is worse than a blank field — it causes a TPA query or a denial. So any value the
model produced that cannot be found in the document is flagged for human review instead of
being silently trusted.
"""
from __future__ import annotations

import io
import re
from datetime import date

from dateutil import parser as dateparser
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from .. import llm


class PreAuthExtraction(BaseModel):
    patient_name: str | None = None
    age: int | None = None
    gender: str | None = Field(None, description="Male / Female / Other")
    uhid: str | None = Field(None, description="Hospital patient ID / UHID / MRN / IP number")
    patient_phone: str | None = None
    insurer: str | None = None
    tpa: str | None = None
    policy_number: str | None = None
    member_id: str | None = Field(None, description="TPA / insurer member or card ID")
    admission_date: str | None = Field(None, description="ISO date YYYY-MM-DD")
    admission_type: str | None = Field(None, description="Emergency or Planned")
    treating_doctor: str | None = None
    doctor_registration_no: str | None = None
    provisional_diagnosis: str | None = None
    icd10_code: str | None = Field(None, description="ICD-10 code if written in the document; else best suggestion")
    line_of_treatment: str | None = Field(None, description="Medical / Surgical / Intensive care / Maternity / Day care")
    proposed_procedure: str | None = None
    duration_of_ailment: str | None = None
    past_history: str | None = Field(None, description="Pre-existing conditions as written, e.g. 'HTN x 5 yrs'")
    alcohol_history: str | None = Field(None, description="Alcohol / substance / smoking history as written")
    room_type: str | None = None
    expected_los_days: int | None = None
    estimated_cost: int | None = Field(None, description="Total estimate in INR as integer")
    is_rta: bool | None = Field(None, description="Road traffic accident / medico-legal case")


FIELDS = list(PreAuthExtraction.model_fields)

# ------------------------------------------------------------------ helpers

def document_text(data: bytes, filename: str) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    if name.endswith((".txt", ".md", ".csv")):
        return data.decode("utf-8-sig", errors="replace")
    return ""  # images: only the multimodal LLM path can read them


def mime_for(filename: str) -> str:
    n = filename.lower()
    for ext, mime in ((".pdf", "application/pdf"), (".png", "image/png"), (".jpg", "image/jpeg"),
                      (".jpeg", "image/jpeg"), (".webp", "image/webp")):
        if n.endswith(ext):
            return mime
    return "text/plain"


def parse_inr(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(lakh|lac|l\b|k\b)?", text.lower().replace("rs.", " ").replace("₹", " "))
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    unit = m.group(2)
    if unit in ("lakh", "lac", "l"):
        value *= 100_000
    elif unit == "k":
        value *= 1_000
    return int(round(value))


def parse_date(text: str | None) -> str | None:
    if not text:
        return None
    try:
        return dateparser.parse(text, dayfirst=True, fuzzy=True).date().isoformat()
    except (ValueError, OverflowError):
        return None


# Small, deliberately conservative ICD-10 suggestion table (heuristic engine only).
ICD10_HINTS = {
    "appendicitis": "K35.80", "cholelithiasis": "K80.20", "gall stone": "K80.20", "gallstone": "K80.20",
    "cholecystitis": "K81.0", "dengue": "A90", "typhoid": "A01.00", "enteric fever": "A01.00",
    "pneumonia": "J18.9", "myocardial infarction": "I21.9", "stemi": "I21.3", "inguinal hernia": "K40.90",
    "urinary tract infection": "N39.0", "uti": "N39.0", "cataract": "H25.9", "fracture neck of femur": "S72.00",
    "intertrochanteric fracture": "S72.10", "fracture shaft of femur": "S72.30", "lscs": "O82",
    "caesarean": "O82", "normal delivery": "O80", "acute gastroenteritis": "A09", "age with": "A09",
    "acute pancreatitis": "K85.90", "cellulitis": "L03.90", "copd": "J44.1", "malaria": "B54",
    "renal calculus": "N20.0", "ureteric calculus": "N20.1", "head injury": "S06.9",
}

LABELS: dict[str, list[str]] = {
    "patient_name": ["patient name", "name of patient", "name of the patient", "pt. name", "pt name", "patient", "name"],
    "uhid": ["uhid", "mrn", "ip no", "ip number", "hospital id", "reg no", "registration no"],
    "patient_phone": ["mobile", "phone", "contact no", "contact number", "mobile no"],
    "insurer": ["insurance company", "insurer", "insurance co", "name of insurer"],
    "tpa": ["tpa", "tpa name", "third party administrator"],
    "policy_number": ["policy number", "policy no", "policy no.", "policy #"],
    "member_id": ["member id", "card no", "tpa id", "id card no", "member/card id", "ecard no"],
    "admission_date": ["date of admission", "admission date", "doa", "admitted on"],
    "admission_type": ["type of admission", "admission type"],
    "treating_doctor": ["treating doctor", "consultant", "treating consultant", "surgeon", "under care of", "doctor"],
    "doctor_registration_no": ["doctor reg no", "medical council reg", "reg. no. (doctor)", "tsmc reg", "mci reg", "nmc reg", "doctor registration"],
    "provisional_diagnosis": ["provisional diagnosis", "diagnosis", "final diagnosis", "working diagnosis"],
    "icd10_code": ["icd-10", "icd 10", "icd code", "icd10"],
    "line_of_treatment": ["line of treatment", "treatment type"],
    "proposed_procedure": ["proposed procedure", "plan / procedure", "procedure", "surgery planned", "planned surgery", "name of surgery"],
    "duration_of_ailment": ["duration of ailment", "duration of present ailment", "duration of illness", "since"],
    "past_history": ["past history", "past medical history", "pmh", "co-morbidities", "comorbidities", "k/c/o"],
    "alcohol_history": ["alcohol", "alcohol history", "h/o alcohol", "personal history", "addiction"],
    "room_type": ["room type", "room category", "ward", "room"],
    "expected_los_days": ["expected length of stay", "expected los", "expected stay", "no. of days", "los"],
    "estimated_cost": ["estimated cost", "total estimate", "estimate", "approx cost", "estimated amount", "amount requested"],
}


def _find_label(text: str, labels: list[str]) -> str | None:
    for label in labels:  # ordered from most to least specific
        pat = rf"(?im)^[ \t\-\*\d\.\)]*{re.escape(label)}\s*(?:\([^)]*\))?\s*[:\-–=]\s*(.+?)\s*$"
        m = re.search(pat, text)
        if m and m.group(1).strip(" .:-"):
            return m.group(1).strip(" .")
    return None


def heuristic_extract(text: str) -> dict:
    # Case sheets are often two-column ("Name: X        IP No: Y"): split wide gaps into lines.
    text = re.sub(r"[ \t]{3,}", "\n", text)
    out: dict = {f: None for f in FIELDS}
    for field_name, labels in LABELS.items():
        out[field_name] = _find_label(text, labels)

    # Age / sex in their many Indian-hospital spellings: "Age/Sex: 45/M", "45 Y / Male"
    m = re.search(r"(?i)age\s*/\s*(?:sex|gender)\s*[:\-]\s*(\d{1,3})\s*(?:y(?:rs|ears)?)?\s*/\s*([mfo]|male|female|other)", text)
    if m:
        out["age"], g = int(m.group(1)), m.group(2).lower()
    else:
        a = re.search(r"(?im)^\W*age\s*[:\-]\s*(\d{1,3})", text)
        out["age"] = int(a.group(1)) if a else None
        s = re.search(r"(?im)^\W*(?:sex|gender)\s*[:\-]\s*(male|female|other|m|f)\b", text)
        g = s.group(1).lower() if s else None
    out["gender"] = {"m": "Male", "male": "Male", "f": "Female", "female": "Female", "o": "Other", "other": "Other"}.get(g or "")

    out["admission_date"] = parse_date(out["admission_date"])
    out["estimated_cost"] = parse_inr(out["estimated_cost"])
    if out["expected_los_days"]:
        d = re.search(r"\d+", out["expected_los_days"])
        out["expected_los_days"] = int(d.group()) if d else None
    if out["patient_phone"]:
        digits = re.sub(r"\D", "", out["patient_phone"])[-10:]
        out["patient_phone"] = digits if len(digits) == 10 else None

    # ICD: only accept something that looks like a code; otherwise suggest from diagnosis.
    code_re = r"\b([A-TV-Z]\d{2}(?:\.\d{1,2})?)\b"
    if out["icd10_code"]:
        c = re.search(code_re, out["icd10_code"])
        out["icd10_code"] = c.group(1) if c else None
    if not out["icd10_code"] and out["provisional_diagnosis"]:
        dx = out["provisional_diagnosis"].lower()
        out["icd10_code"] = next((code for k, code in ICD10_HINTS.items() if k in dx), None)

    low = text.lower()
    if out["admission_type"]:
        out["admission_type"] = "Emergency" if "emerg" in out["admission_type"].lower() else "Planned"
    elif "emergency" in low:
        out["admission_type"] = "Emergency"

    if not out["line_of_treatment"]:
        if any(k in low for k in ("lscs", "delivery", "caesarean", "antenatal")):
            out["line_of_treatment"] = "Maternity"
        elif out["proposed_procedure"] and not re.search(r"(?i)\b(nil|none|na|conservative)\b", out["proposed_procedure"]):
            out["line_of_treatment"] = "Surgical"
        elif "icu" in low:
            out["line_of_treatment"] = "Intensive care"
        else:
            out["line_of_treatment"] = "Medical"
    out["is_rta"] = bool(re.search(r"(?i)\b(rta|road traffic accident|mlc|medico[- ]legal|fir)\b", text))
    return out


# ------------------------------------------------------------------ grounding

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def grounding_report(fields: dict, source_text: str) -> dict:
    """For each extracted value, is there evidence for it in the source document?"""
    if not source_text.strip():
        return {k: None for k in fields}  # image-only input: cannot verify textually
    src = _norm(source_text)
    src_digits = re.sub(r"\D", "", source_text)
    report: dict[str, bool | None] = {}
    for k, v in fields.items():
        if v is None or v == "" or isinstance(v, bool):
            report[k] = None
        elif k == "admission_date":
            report[k] = _date_in_text(v, source_text)
        elif k in ("estimated_cost", "age", "expected_los_days"):
            report[k] = str(v) in src_digits or _inr_variants_present(int(v), source_text)
        elif k == "gender":
            report[k] = True if re.search(rf"(?i)\b({v}|{v[0]})\b", source_text) else False
        elif k == "patient_phone":
            report[k] = str(v)[-10:] in src_digits
        else:
            nv = _norm(str(v))
            report[k] = bool(nv) and (nv in src or fuzz.partial_ratio(nv, src) >= 90)
    return report


def _inr_variants_present(n: int, text: str) -> bool:
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(lakh|lac|l\b)", text.lower()):
        if int(float(m.group(1).replace(",", "")) * 100_000) == n:
            return True
    return False


def _date_in_text(iso: str, text: str) -> bool:
    try:
        target = date.fromisoformat(iso)
    except ValueError:
        return False
    for m in re.finditer(r"\b(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b", text):
        if parse_date(m.group(1)) == target.isoformat():
            return True
    return False


# ------------------------------------------------------------------ public API

SYSTEM_PROMPT = """You are a cashless-claims desk assistant at an Indian hospital.
Extract pre-authorisation fields from the clinical document(s).
Rules:
- Copy identifiers (policy number, member ID, UHID, registration numbers) EXACTLY as written.
- If a field is not present, return null. Never invent policy numbers, IDs, histories or amounts.
- Dates -> YYYY-MM-DD (Indian documents are day-first: 04/11/2026 = 4 Nov 2026).
- estimated_cost -> integer rupees (1.2 lakh = 120000).
- icd10_code: use the code written in the document if any; otherwise give your best ICD-10 suggestion.
- past_history / alcohol_history: copy the wording used in the document (e.g. "Denies alcohol", "K/C/O DM2 x 6 yrs")."""


def extract_preauth(data: bytes, filename: str) -> dict:
    text = document_text(data, filename)

    def via_llm():
        if text.strip():
            return llm.generate_json(f"{SYSTEM_PROMPT}\n\nDOCUMENT:\n{text[:30000]}", PreAuthExtraction,
                                     task="preauth_extraction")
        return llm.generate_json(SYSTEM_PROMPT, PreAuthExtraction, task="preauth_extraction",
                                 files=[(data, mime_for(filename))])

    def via_rules():
        if not text.strip():
            raise ValueError("Image/scanned input needs the Gemini engine (set GEMINI_API_KEY or Vertex AI).")
        return PreAuthExtraction.model_validate(heuristic_extract(text)).model_dump()

    fields, engine = llm.run_with_fallback("preauth_extraction", via_llm, via_rules)
    grounding = grounding_report(fields, text)
    ungrounded = [k for k, ok in grounding.items() if ok is False]
    # ICD codes are often *inferred* (not printed on the document) - that is allowed but must be reviewed.
    needs_review = [k for k in ungrounded if k != "icd10_code"]
    filled = sum(v not in (None, "") for v in fields.values())
    return {
        "engine": engine,
        "fields": fields,
        "grounding": grounding,
        "ungrounded_fields": ungrounded,
        "needs_human_review": needs_review,
        "icd10_inferred": "icd10_code" in ungrounded,
        "fill_rate": round(filled / len(fields), 2),
        "doc_type": classify_document(text, filename),
        "source_chars": len(text),
    }


DOC_TYPE_RULES = [
    ("implant_invoice", ("implant", "sticker", "stent", "prosthesis", "plate and screw")),
    ("id_proof", ("aadhaar", "aadhar", "pan card", "voter id", "passport")),
    ("policy_card", ("e-card", "health card", "member card", "policy card")),
    ("investigation_report", ("haemoglobin", "hemoglobin", "ultrasound", "usg", "ct scan", "mri", "x-ray", "lab report", "ns1", "widal")),
    ("discharge_summary", ("discharge summary", "condition at discharge", "date of discharge")),
    ("final_bill", ("final bill", "bill no", "net payable", "invoice no")),
    ("mlc_fir", ("medico legal", "mlc no", "fir no", "first information report")),
    ("preauth_form", ("pre-authorization", "pre-authorisation", "preauth", "request for cashless", "cashless request")),
    ("admission_note", ("admission note", "chief complaint", "history of present illness", "provisional diagnosis")),
]


def classify_document(text: str, filename: str) -> str:
    hay = f"{filename} {text[:4000]}".lower()
    for doc_type, keys in DOC_TYPE_RULES:
        if any(k in hay for k in keys):
            return doc_type
    return "other"
