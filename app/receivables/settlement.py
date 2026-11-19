"""Settlement letters (remittance advice): what the TPA says it paid for one claim, and why it cut the rest.

A TPA emails one letter per claim: claim number, claimed and approved amounts, the deductions with reasons,
TDS, the net amount transferred and the UTR of the bank transfer. That UTR is what ties a lump-sum bank
credit back to individual claims (see reconcile.py).

Gemini extracts the fields (PDF, text or email); a label-based parser is the fallback. Either way, two
checks run before a letter is trusted, because this is money:
  * grounding: every amount, the UTR and the claim number must appear in the letter itself;
  * arithmetic: approved - TDS = net paid, claimed - deductions = approved, deduction lines add up.
A letter that fails either check is held for a person to review; it is never applied automatically.
"""
from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, Field

from .. import llm
from ..claims.extraction import _date_in_text, document_text, mime_for, parse_date


class Deduction(BaseModel):
    reason: str
    amount: float


class SettlementLetter(BaseModel):
    claim_ref: str | None = Field(None, description="TPA claim number / CCN / AL number, exactly as written")
    patient_name: str | None = None
    payer: str | None = Field(None, description="The TPA or insurer that sent the letter")
    policy_number: str | None = None
    claimed_amount: float | None = Field(None, description="Amount the hospital claimed / billed")
    approved_amount: float | None = Field(None, description="Settled / approved / admissible amount, before TDS")
    deduction_amount: float | None = Field(None, description="Total disallowed / deducted amount")
    deductions: list[Deduction] = Field(default_factory=list, description="Each deduction reason with its amount")
    tds_amount: float | None = Field(None, description="Income tax (TDS) withheld")
    net_paid: float | None = Field(None, description="Net amount transferred to the hospital's bank account")
    utr: str | None = Field(None, description="UTR / NEFT / RTGS transaction reference, exactly as written")
    payment_date: str | None = Field(None, description="Date of payment / transfer, YYYY-MM-DD")


FIELDS = list(SettlementLetter.model_fields)
AMOUNTS = ("claimed_amount", "approved_amount", "deduction_amount", "tds_amount", "net_paid")

SYSTEM_PROMPT = """You read settlement letters / payment advice that Indian TPAs and insurers send to hospitals.
Extract the fields for the ONE claim the letter is about.
Rules:
- Copy the claim number and the UTR / NEFT / RTGS reference EXACTLY as written. Never invent them.
- Amounts are rupees as numbers (1,15,650.00 -> 115650). approved_amount is the amount settled BEFORE TDS;
  net_paid is what was actually transferred to the hospital.
- deductions: every disallowed item with its reason and amount, as listed. Do not add items that are not listed.
- If a field is not in the letter, return null.
- Dates -> YYYY-MM-DD (Indian documents are day-first: 05/11/2026 = 5 Nov 2026)."""

LABELS: dict[str, list[str]] = {
    "claim_ref": ["tpa claim no", "claim number", "claim no", "claim ref", "claim id", "ccn", "al no"],
    "patient_name": ["patient name", "name of patient", "name of the patient", "patient", "claimant"],
    "payer": ["tpa name", "tpa", "insurance company", "insurer"],
    "policy_number": ["policy number", "policy no"],
    "claimed_amount": ["claimed amount", "amount claimed", "total claimed", "billed amount", "bill amount", "claim amount"],
    "approved_amount": ["final approved amount", "approved amount", "amount settled", "settled amount", "admissible amount",
                        "sanctioned amount", "gross amount payable", "gross payable"],
    "deduction_amount": ["total deductions", "total deduction", "total disallowed", "disallowed amount", "deducted amount",
                         "less deductions"],
    "tds_amount": ["tds deducted", "tds amount", "less tds", "tds", "income tax deducted"],
    "net_paid": ["net amount paid", "net amount transferred", "amount transferred", "net paid", "net amount", "net payable",
                 "amount paid"],
    "utr": ["utr number", "utr no", "utr", "neft rtgs ref", "neft ref", "rtgs ref", "transaction reference", "transaction ref"],
    "payment_date": ["date of payment", "payment date", "paid on", "transfer date", "date of transfer", "utr date"],
}
_AMOUNT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_DATE_RE = re.compile(r"\b\d{1,2}[\s/\-.](?:\d{1,2}|[A-Za-z]{3,9})[\s/\-.,]+\d{2,4}\b")  # 06-Nov-2026, 6 Nov 2026


def _money(s: str | None) -> float | None:
    if not s:
        return None
    m = _AMOUNT_RE.search(s.replace("Rs.", " ").replace("Rs", " ").replace("INR", " ").replace("₹", " "))
    return float(m.group(0).replace(",", "")) if m else None


def _label_value(text: str, labels: list[str]) -> str | None:
    """'Label : value' or 'Label - value' or 'Label | value', label at a line start or after a column gap."""
    for lab in labels:
        pat = r"(?im)(?:^|\s{2,}|\|)\s*" + r"[\s./]*".join(map(re.escape, lab.split())) + \
              r"\s*(?:no\.?|number|#|\(rs\.?\)|\(inr\)|\(₹\))?\s*[:|\-–]\s*([^\n|]+?)\s*(?=\s{2,}|\||$)"
        m = re.search(pat, text)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def _deduction_lines(text: str) -> list[dict]:
    """Rows under a 'Deduction details' / 'Reasons for deduction' heading: '<reason> ... <amount>'."""
    # A heading line has no figures on it ("Total Deductions : 13,500" is a total, not the heading).
    m = re.search(r"(?im)^[^\d\n]*\b(deduction details|details of deduction|reasons? for deduction|deductions? breakup|"
                  r"disallowance details|deductions?)\b[^\d\n]*$", text)
    if not m:
        return []
    out = []
    for line in text[m.end():].splitlines():
        s = line.strip()
        if not s:
            if out:
                break
            continue
        if re.match(r"(?i)(total|less|net|gross|tds|approved|amount|utr|payment)", s.lstrip("-*•0123456789.) ")):
            break
        row = re.match(r"^(?:[-*•]|\d+[.)])?\s*(.+?)\s*[:|\-–]?\s*(?:Rs\.?|INR|₹)?\s*(\d[\d,]*(?:\.\d+)?)\s*(?:/-)?$", s)
        if row and re.search(r"[A-Za-z]", row.group(1)):
            out.append({"reason": row.group(1).strip(" :-–|"), "amount": float(row.group(2).replace(",", ""))})
    return out


def heuristic_extract(text: str) -> dict:
    f: dict = {}
    # The payer is whoever sent the letter: the company name heading the letter, before any "Insurer :" field.
    head = re.search(r"(?im)^\s*([A-Z][A-Za-z&. ]{2,60}\b(?:TPA|Insurance|Assurance)\b[A-Za-z&. ]{0,40})$", text)
    f["payer"] = head.group(1).strip(" .") if head else None
    for k, labs in LABELS.items():
        if k == "payer" and f["payer"]:
            continue
        v = _label_value(text, labs)
        if k in AMOUNTS:
            f[k] = _money(v)
        elif k == "payment_date":
            f[k] = parse_date(v) if v else None
        elif k == "utr":
            tok = re.search(r"[A-Z0-9]{10,24}", (v or "").upper().replace(" ", ""))
            f[k] = tok.group(0) if tok else None
        else:
            f[k] = v
    f["deductions"] = _deduction_lines(text)
    return SettlementLetter.model_validate(f).model_dump()


def _norm_id(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def grounding(fields: dict, text: str) -> dict:
    """Is each extracted value actually in the letter? None = not extracted / cannot check."""
    if not text.strip():
        return {k: None for k in FIELDS}
    amounts = {float(a.replace(",", "")) for a in _AMOUNT_RE.findall(text)}
    flat = _norm_id(text)
    low = re.sub(r"[^a-z0-9]+", " ", text.lower())
    rep: dict = {}
    for k in FIELDS:
        v = fields.get(k)
        if v in (None, "", []):
            rep[k] = None
        elif k in AMOUNTS:
            rep[k] = float(v) in amounts
        elif k == "deductions":
            rep[k] = all(float(d["amount"]) in amounts for d in v)
        elif k in ("utr", "claim_ref", "policy_number"):
            rep[k] = bool(_norm_id(v)) and _norm_id(v) in flat
        elif k == "payment_date":
            rep[k] = _date_in_text(v, text) or any(parse_date(d) == v for d in _DATE_RE.findall(text))
        else:
            words = [w for w in re.sub(r"[^a-z0-9]+", " ", str(v).lower()).split() if len(w) > 1]
            rep[k] = bool(words) and sum(w in low.split() for w in words) / len(words) >= 0.7
    return rep


def check(fields: dict) -> tuple[dict, list[str]]:
    """Fill what can be derived (net = approved - TDS) and list every way the letter does not add up."""
    f = dict(fields)
    issues: list[str] = []
    a, t, n, c, d = (f.get(k) for k in ("approved_amount", "tds_amount", "net_paid", "claimed_amount", "deduction_amount"))
    if n is None and a is not None:
        f["net_paid"] = n = a - (t or 0)
        f["net_derived"] = True
    if a is None and n is not None:
        f["approved_amount"] = a = n + (t or 0)
        f["approved_derived"] = True
    if a is not None and n is not None and abs(a - (t or 0) - n) > 1:
        issues.append(f"Approved {a:,.0f} - TDS {(t or 0):,.0f} = {a - (t or 0):,.0f}, but the letter says net paid {n:,.0f}.")
    items = sum(x["amount"] for x in f.get("deductions") or [])
    if d is None and items:
        f["deduction_amount"] = d = items
    if c is not None and a is not None and d is not None and abs(c - d - a) > 1:
        issues.append(f"Claimed {c:,.0f} - deductions {d:,.0f} = {c - d:,.0f}, but the letter says approved {a:,.0f}.")
    if items and d is not None and abs(items - d) > 1:
        issues.append(f"Deduction lines add up to {items:,.0f}, but the total deduction is {d:,.0f}.")
    if t and a and not (0.019 * a <= t <= 0.101 * a):
        issues.append(f"TDS of {t:,.0f} is {100 * t / a:.1f}% of the approved amount; expected 2% or 10%.")
    if not f.get("claim_ref"):
        issues.append("No claim number found.")
    if not f.get("utr"):
        issues.append("No UTR found, so the payment cannot be traced in the bank statement.")
    if f.get("net_paid") is None:
        issues.append("No paid amount found.")
    return f, issues


def read_letter(data: bytes, filename: str, *, shift_days: int = 0) -> dict:
    """One settlement letter -> fields + grounding + arithmetic issues + needs_review."""
    name = filename.lower()
    text = data.decode("utf-8-sig", errors="replace") if name.endswith((".eml", ".txt", ".text", ".msg")) else document_text(data, filename)

    def via_llm():
        if text.strip():
            return llm.generate_json(f"{SYSTEM_PROMPT}\n\nLETTER:\n{text[:20000]}", SettlementLetter, task="settlement_extraction")
        return llm.generate_json(SYSTEM_PROMPT, SettlementLetter, task="settlement_extraction", files=[(data, mime_for(filename))])

    def via_rules():
        if not text.strip():
            raise ValueError("Scanned letters need the Gemini engine (set GEMINI_API_KEY or Vertex AI).")
        return heuristic_extract(text)

    raw, engine = llm.run_with_fallback("settlement_extraction", via_llm, via_rules)
    ground = grounding(raw, text)
    fields, issues = check(raw)
    ungrounded = [k for k, ok in ground.items() if ok is False]
    for k in ungrounded:
        issues.append(f"{k.replace('_', ' ')} '{raw.get(k)}' is not in the letter (possible misread).")
    if shift_days and fields.get("payment_date"):  # demo samples only: keep their dates relative to today
        fields["payment_date"] = date.fromordinal(date.fromisoformat(fields["payment_date"]).toordinal() + shift_days).isoformat()
    fields["utr"] = _norm_id(fields.get("utr")) or None
    return {"filename": filename, "engine": engine, "fields": fields, "grounding": ground, "issues": issues,
            "needs_review": bool(issues), "source_chars": len(text)}
