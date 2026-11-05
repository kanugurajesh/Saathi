"""CSV / XLSX intake for payers the bots cannot reach (Aarogyasri, EHS, PM-JAY), plus the HMS billing
opening balance for private TPAs.

Export layouts differ by payer and change without notice, so columns are matched by synonym + fuzzy
match. Status text is mapped onto one small settlement lifecycle, and a status we have never seen is
reported back instead of being silently guessed. The column names below are *assumed* export headers;
they are matched loosely, so variants work, but they should be checked against real exports.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta

import pandas as pd
from rapidfuzz import fuzz

from ..onboarding.schemas import is_blank, norm_date, norm_money, norm_text
from . import ledger

FIELDS: dict[str, tuple[str, ...]] = {
    "claim_ref": ("case no", "case id", "case number", "claim id", "claim no", "claim number", "claim ref", "tpa claim no",
                  "ccn", "al no", "registration no"),
    "patient_name": ("patient name", "beneficiary name", "patient", "name of patient", "beneficiary", "name"),
    "uhid": ("uhid", "ip no", "ipd no", "mrn"),
    "card_no": ("card no", "health card no", "pmjay id", "abha id", "beneficiary id", "ehs card no", "member id",
                "policy no"),
    "payer": ("tpa", "tpa name", "payer", "insurer", "insurance company"),
    "package": ("procedure name", "package name", "procedure", "package", "therapy", "surgery"),
    "status": ("claim status", "status", "case status", "current status"),
    "billed": ("claim amount", "claimed amount", "bill amount", "billed amount", "claim amt", "amount claimed", "net bill",
               "final bill amount"),
    "approved": ("approved amount", "sanctioned amount", "claim approved amount", "approved amt", "admissible amount",
                 "final approved amount"),
    "paid": ("paid amount", "amount paid", "settled amount", "payment amount", "amount released", "released amount",
             "paid amt"),
    "tds": ("tds", "tds amount", "tds deducted"),
    "deduction": ("deduction amount", "deducted amount", "disallowed amount", "deduction"),
    "remarks": ("remarks", "deduction remarks", "deduction reason", "rejection reason", "denial reason", "reason",
                "rejection deduction reason", "query reason", "trust remarks"),
    "submitted_on": ("claim submitted date", "claim submission date", "claim initiated date", "submitted on",
                     "discharge date", "date of discharge", "bill date", "claim date"),
    "paid_on": ("payment date", "paid date", "paid on", "settlement date", "utr date", "date of payment"),
}
MIN_HEADER_SCORE = 86


def _clean_header(h) -> str:
    h = re.sub(r"\(.*?\)", " ", str(h).lower())  # "Claim Amount (Rs)" -> "claim amount"
    return re.sub(r"\s+", " ", re.sub(r"[_\-/.:#]+", " ", h)).strip()


def map_columns(columns) -> dict[str, str]:
    """field -> source column. Best-scoring pairs are assigned first; each column is used once."""
    scored = []
    for col in columns:
        h = _clean_header(col)
        for field, syns in FIELDS.items():
            s = 100 - syns.index(h) * 0.1 if h in syns else max(fuzz.ratio(h, x) for x in syns)  # earlier synonym wins ties
            if s >= MIN_HEADER_SCORE:
                scored.append((s, -list(FIELDS).index(field), str(col), field))
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for _, _, col, field in sorted(scored, reverse=True):
        if field not in mapping and col not in used:
            mapping[field] = col
            used.add(col)
    return mapping


def map_status(raw: str | None, *, paid: float | None, approved: float | None, billed: float | None) -> tuple[str, str]:
    """Settlement-stage status text -> (status, how). how is 'text', 'amounts' or 'unknown'."""
    s = " ".join((raw or "").lower().split())
    if s:
        if any(w in s for w in ("reject", "denied", "declin", "repudiat", "not admissible", "closed without payment")):
            return "DENIED", "text"
        if any(w in s for w in ("part paid", "partially paid", "partial payment", "part payment")):
            return "PART_PAID", "text"
        unpaid = any(w in s for w in ("unpaid", "not paid", "payment pending", "pending for payment", "payment awaited",
                                      "awaiting payment", "to be paid"))
        if not unpaid and any(w in s for w in ("paid", "payment done", "payment released", "settled", "utr", "amount released")):
            return "PAID", "text"
        if any(w in s for w in ("query", "clarification", "deficien", "pending with hospital", "returned to hospital",
                                "pending from hospital")):
            return "QUERY", "text"
        if unpaid or (any(w in s for w in ("approv", "sanction", "admissible", "authori"))
                      and not any(w in s for w in ("for approval", "pending approval", "awaiting approval"))):
            return "APPROVED", "text"
        if any(w in s for w in ("pending", "process", "review", "submitted", "initiated", "received", "scrutiny")):
            return "PENDING", "text"
    expected = approved if approved else billed
    if paid and expected and paid >= expected:
        return "PAID", "amounts"
    if paid:
        return "PART_PAID", "amounts"
    return "PENDING", "unknown" if s else "amounts"


def detect_payer_type(filename: str, columns) -> str | None:
    f = filename.lower()
    if re.search(r"a+rogyasri|arogya ?sri", f):
        return "aarogyasri"
    if re.search(r"(^|[^a-z])ehs([^a-z]|$)", f):
        return "ehs"
    if re.search(r"pm.?jay|ayushman|(^|[^a-z])tms([^a-z]|$)", f):
        return "pmjay"
    if re.search(r"tpa|insur", f):
        return "private_tpa"
    heads = {_clean_header(c) for c in columns}
    if heads & {"pmjay id", "abha id"}:
        return "pmjay"
    if "ehs card no" in heads:
        return "ehs"
    if heads & {"tpa", "tpa name", "insurer"}:
        return "private_tpa"
    return None


def read_table(blob: bytes, filename: str) -> pd.DataFrame:
    if filename.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(blob), dtype=object)
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = blob.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return pd.read_csv(io.StringIO(text), dtype=str, sep=None, engine="python", keep_default_na=False)


class IntakeError(ValueError):
    """A file the importer cannot use at all (unknown payer, no claim-number column...)."""


def import_file(blob: bytes, filename: str, payer_type: str = "auto", *, shift_days: int = 0,
                imported_at: datetime | None = None, baseline: bool = False) -> dict:
    """shift_days moves every date by N days (used only to keep the bundled samples' ages stable)."""
    df = read_table(blob, filename)
    df = df.dropna(how="all")
    pt = payer_type if payer_type in ledger.PAYER_TYPES else detect_payer_type(filename, df.columns)
    if not pt:
        raise IntakeError("Could not tell which payer this file is from. Choose Aarogyasri, EHS, PM-JAY or private TPA.")
    m = map_columns(df.columns)
    if "claim_ref" not in m or not ({"billed", "approved"} & m.keys()):
        raise IntakeError(f"Need a claim / case number column and a claim amount column. Found headers: {', '.join(map(str, df.columns))}")

    def cell(row, field):
        return row[m[field]] if field in m else None

    def day(v) -> str | None:
        d = norm_date(v)
        return (d + timedelta(days=shift_days)).isoformat() if d else None

    report = {"filename": filename, "payer_type": pt, "payer_label": ledger.PAYER_TYPES[pt], "rows_total": len(df),
              "created": 0, "updated": 0, "unchanged": 0, "skipped": 0, "issues": [], "events": [],
              "mapping": [{"source": str(c), "field": next((f for f, col in m.items() if col == str(c)), None)} for c in df.columns]}
    today = ledger.today_ist()
    for i, row in enumerate(df.to_dict("records")):
        n = i + 2  # spreadsheet row number (header is row 1)
        ref = norm_text(cell(row, "claim_ref"))
        if not ref:
            report["skipped"] += 1
            report["issues"].append({"row": n, "severity": "blocker", "message": "No claim / case number - row skipped"})
            continue
        billed, approved = norm_money(cell(row, "billed")), norm_money(cell(row, "approved"))
        paid, tds, ded = norm_money(cell(row, "paid")), norm_money(cell(row, "tds")), norm_money(cell(row, "deduction"))
        if billed is None and approved is None:
            report["skipped"] += 1
            report["issues"].append({"row": n, "ref": ref, "severity": "blocker", "message": "No claim or approved amount - row skipped"})
            continue
        raw_status = norm_text(cell(row, "status"))
        status, how = map_status(raw_status, paid=paid, approved=approved, billed=billed)
        if how == "unknown":
            report["issues"].append({"row": n, "ref": ref, "severity": "warning",
                                     "message": f"Status '{raw_status}' not recognised - counted as pending. Add it to the vocabulary."})
        if status in ("PENDING", "QUERY") or (approved == 0 and status != "DENIED"):
            approved = None  # an unprocessed claim's blank/zero approval is not a decision
        submitted = day(cell(row, "submitted_on"))
        if submitted is None:
            submitted = today.isoformat()
            report["issues"].append({"row": n, "ref": ref, "severity": "warning",
                                     "message": "No readable submission / discharge date - aged from today"})
        if paid and billed and paid > billed * 1.001:
            report["issues"].append({"row": n, "ref": ref, "severity": "warning", "message": "Paid amount is more than the claim amount - check"})
        rec = {"payer_type": pt, "payer": norm_text(cell(row, "payer")), "claim_ref": ref,
               "patient_name": norm_text(cell(row, "patient_name")), "uhid": norm_text(cell(row, "uhid")),
               "card_no": norm_text(cell(row, "card_no")), "package": norm_text(cell(row, "package")),
               "billed": _num(billed if billed is not None else approved), "approved": _num(approved), "paid": _num(paid),
               "tds": _num(tds), "deduction_explicit": _num(ded), "status": status, "status_raw": raw_status,
               "remarks": norm_text(cell(row, "remarks")), "submitted_on": submitted, "paid_on": day(cell(row, "paid_on"))}
        res = ledger.upsert(rec, source="csv", source_file=filename, baseline=baseline)
        key = "created" if res["created"] else "updated" if res["changed"] else "unchanged"
        report[key] += 1
        report["events"] += [{**e, "claim_ref": ref, "patient_name": rec["patient_name"]} for e in res["events"]]
    ledger.record_import(pt, filename, report, at=imported_at)
    return report


def _num(v: float | None) -> int | float | None:
    if v is None or is_blank(v):
        return None
    return int(v) if float(v).is_integer() else round(v, 2)


def manual_entry(payload: dict) -> dict:
    """One claim typed in by the billing desk (e.g. read off the Aarogyasri portal screen)."""
    pt = payload.get("payer_type")
    if pt not in ledger.PAYER_TYPES:
        raise IntakeError("payer_type must be one of: " + ", ".join(ledger.PAYER_TYPES))
    ref = norm_text(payload.get("claim_ref"))
    billed = norm_money(payload.get("billed"))
    if not ref or billed is None:
        raise IntakeError("claim_ref and billed amount are required")
    status = payload.get("status") or "PENDING"
    if status not in ledger.STATUSES:
        raise IntakeError("status must be one of: " + ", ".join(ledger.STATUSES))
    sub = norm_date(payload.get("submitted_on")) if payload.get("submitted_on") else None
    if isinstance(sub, date) and sub > ledger.today_ist():
        raise IntakeError("submitted_on is in the future")
    approved = norm_money(payload.get("approved"))
    rec = {"payer_type": pt, "payer": norm_text(payload.get("payer")), "claim_ref": ref,
           "patient_name": norm_text(payload.get("patient_name")), "package": norm_text(payload.get("package")),
           "billed": _num(billed), "approved": _num(approved) if status not in ("PENDING", "QUERY") else None,
           "paid": _num(norm_money(payload.get("paid"))), "status": status, "remarks": norm_text(payload.get("remarks")),
           "submitted_on": sub.isoformat() if sub else None, "paid_on": None}
    # Deliberately not recorded as an import: one typed-in claim does not make the whole payer's figures fresh.
    return ledger.upsert(rec, source="manual", source_file="manual entry")
