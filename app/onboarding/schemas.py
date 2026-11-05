"""Target (canonical) import schemas + value normalisers for hospital onboarding data.

NOTE: these are *assumed* HMS import schemas modelled on typical Indian HMS masters
(pharmacy, lab, tariff, staff, patients). Swapping in HextGen's real schema is a config change.
"""
from __future__ import annotations

import math
import re
from calendar import monthrange
from datetime import date, datetime, timedelta

from dateutil import parser as dateparser

F = dict  # brevity

ENTITIES: dict[str, dict] = {
    "drug_master": {
        "label": "Pharmacy drug master & opening stock",
        "filename_hints": ("drug", "pharma", "medicine", "stock", "inventory", "item"),
        "fields": {
            "drug_name": F(required=True, syn=["item name", "product name", "medicine name", "drug name", "item", "product", "medicine", "description", "brand name"]),
            "generic_name": F(syn=["generic", "generic name", "composition", "salt", "molecule", "content"]),
            "manufacturer": F(syn=["mfr", "manufacturer", "company", "mfg", "make", "mfg co"]),
            "hsn_code": F(syn=["hsn", "hsn code", "hsn sac", "hsn/sac"]),
            "gst_rate": F(required=True, syn=["gst", "gst %", "gst%", "tax %", "gst rate", "tax", "igst", "tax rate"]),
            "pack_size": F(syn=["pack", "packing", "pack size", "uom", "unit"]),
            "batch_no": F(syn=["batch", "batch no", "batch number", "lot", "lot no"]),
            "expiry_date": F(required=True, syn=["exp", "expiry", "exp date", "expiry date", "exp dt", "exp."]),
            "mrp": F(required=True, syn=["mrp", "m.r.p", "m r p", "max retail price", "mrp rs", "mrp (rs)"]),
            "purchase_rate": F(syn=["p.rate", "p rate", "purchase rate", "ptr", "cost", "purchase price", "landing cost", "rate"]),
            "stock_qty": F(syn=["qty", "quantity", "stock", "closing stock", "balance", "opening stock", "bal qty"]),
            "schedule": F(syn=["schedule", "sch", "drug schedule", "category"]),
        },
    },
    "lab_tests": {
        "label": "Lab test master",
        "filename_hints": ("lab", "test", "investigation", "pathology"),
        "fields": {
            "test_name": F(required=True, syn=["test name", "test", "investigation", "test description", "name of test", "parameter"]),
            "test_code": F(syn=["code", "test code", "short code", "loinc"]),
            "department": F(syn=["department", "dept", "section", "lab section"]),
            "sample_type": F(syn=["sample", "sample type", "specimen", "container"]),
            "price": F(required=True, syn=["price", "rate", "charges", "amount", "mrp", "cost", "fee"]),
            "tat_hours": F(syn=["tat", "turnaround", "tat hrs", "reporting time"]),
        },
    },
    "tariff": {
        "label": "Hospital tariff / rate card",
        "filename_hints": ("tariff", "rate", "charges", "price list", "service"),
        "fields": {
            "service_name": F(required=True, syn=["service", "service name", "particulars", "description", "item", "procedure"]),
            "department": F(syn=["department", "dept", "speciality", "specialty"]),
            "category": F(syn=["category", "type", "head", "service type", "group"]),
            "price": F(required=True, syn=["price", "rate", "charges", "amount", "tariff", "general ward", "cash rate"]),
            "insurance_price": F(syn=["insurance rate", "tpa rate", "corporate rate", "insurance price", "tpa"]),
        },
    },
    "staff": {
        "label": "Staff & user accounts",
        "filename_hints": ("staff", "user", "employee", "doctor", "team"),
        "fields": {
            "full_name": F(required=True, syn=["name", "full name", "staff name", "employee name", "doctor name"]),
            "role": F(required=True, syn=["role", "designation", "position", "type", "user type", "post"]),
            "department": F(syn=["department", "dept", "speciality", "specialty", "unit"]),
            "phone": F(required=True, syn=["phone", "mobile", "mobile no", "contact", "contact no", "cell", "whatsapp"]),
            "email": F(syn=["email", "e-mail", "mail id", "email id"]),
            "registration_number": F(syn=["reg no", "registration no", "registration number", "mci", "nmc", "tsmc", "council reg", "medical reg no", "reg. no"]),
        },
    },
    "patients": {
        "label": "Patient registry (legacy)",
        "filename_hints": ("patient", "registr", "mrd", "uhid"),
        "fields": {
            "uhid": F(syn=["uhid", "mrn", "patient id", "reg no", "op no", "hospital no"]),
            "full_name": F(required=True, syn=["patient name", "name", "full name", "pt name"]),
            "gender": F(syn=["gender", "sex", "m/f"]),
            "age": F(syn=["age", "age (yrs)", "age yrs"]),
            "dob": F(syn=["dob", "date of birth", "birth date"]),
            "phone": F(required=True, syn=["phone", "mobile", "contact", "mobile no", "contact no"]),
            "address": F(syn=["address", "city", "place", "village", "location"]),
            "abha_number": F(syn=["abha", "abha no", "abha number", "health id"]),
        },
    },
}

# ------------------------------------------------------------------ normalisers

def is_blank(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or (isinstance(v, str) and v.strip() in ("", "-", "NA", "N/A", "nan", "None"))


def norm_text(v) -> str | None:
    if is_blank(v):
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def norm_money(v) -> float | None:
    if is_blank(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"(?i)(rs\.?|inr|₹|/-|,|\s)", "", str(v))
    try:
        return float(s)
    except ValueError:
        return None


def norm_gst(v) -> float | None:
    if is_blank(v):
        return None
    s = str(v).lower().replace("gst", "").replace("%", "").strip()
    try:
        x = float(s)
    except ValueError:
        return None
    return round(x * 100, 2) if 0 < x < 1 else x


def norm_int(v) -> int | None:
    m = norm_money(v)
    if m is None:
        return None
    return int(m) if float(m).is_integer() else None


def norm_phone(v) -> str | None:
    if is_blank(v):
        return None
    d = re.sub(r"\D", "", str(v).split(".")[0] if isinstance(v, float) else str(v))
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return d if re.fullmatch(r"[6-9]\d{9}", d) else None


def norm_date(v, *, expiry: bool = False) -> date | None:
    """Indian day-first dates, Excel serials, and MM/YY or Mon-YY expiry formats (-> month end)."""
    if is_blank(v):
        return None
    if isinstance(v, datetime):
        d = v.date()
        return _month_end(d) if expiry and d.day == 1 else d
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and 20000 < float(v) < 80000:  # Excel serial
        return date(1899, 12, 30) + timedelta(days=int(v))
    s = str(v).strip()
    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{2}|\d{4})", s)  # 03/27, 3-2027
    if m:
        mon, yr = int(m.group(1)), int(m.group(2))
        yr = yr + 2000 if yr < 100 else yr
        if 1 <= mon <= 12:
            return _month_end(date(yr, mon, 1))
        return None
    m = re.fullmatch(r"([A-Za-z]{3,9})[\s\-/']*(\d{2}|\d{4})", s)  # Mar-27, March 2027
    if m:
        try:
            d = dateparser.parse(f"1 {m.group(1)} {m.group(2) if len(m.group(2)) == 4 else '20' + m.group(2)}").date()
            return _month_end(d) if expiry else d
        except (ValueError, OverflowError):
            return None
    try:
        return dateparser.parse(s, dayfirst=True).date()
    except (ValueError, OverflowError):
        return None


def _month_end(d: date) -> date:
    return date(d.year, d.month, monthrange(d.year, d.month)[1])


ROLE_MAP = [
    ("doctor", r"\b(dr|doctor|consultant|physician|surgeon|rmo|duty doctor|md|ms|mbbs|anaesth|anesth|paediatrician|gyn)\b"),
    ("nurse", r"\b(nurse|gnm|anm|staff nurse|sister|matron|bsc nursing|nursing)\b"),
    ("pharmacist", r"\b(pharmacist|pharmacy|b\.?pharm|d\.?pharm)\b"),
    ("lab_technician", r"\b(lab|technician|dmlt|phlebotom|pathology)\b"),
    ("receptionist", r"\b(reception|front office|front desk|registration|frontdesk)\b"),
    ("billing", r"\b(billing|cashier|accounts|accountant|insurance desk|tpa)\b"),
    ("admin", r"\b(admin|administrator|manager|owner|director|md office|hr)\b"),
]


def norm_role(v) -> str | None:
    s = norm_text(v)
    if not s:
        return None
    low = s.lower()
    return next((role for role, pat in ROLE_MAP if re.search(pat, low)), None)


def norm_gender(v) -> str | None:
    s = (norm_text(v) or "").lower()
    return {"m": "Male", "male": "Male", "f": "Female", "female": "Female", "o": "Other", "other": "Other",
            "t": "Other", "tg": "Other"}.get(s)


# ------------------------------------------------------------------ GSTIN

_GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_checksum(first14: str) -> str:
    factor, total = 2, 0
    for ch in reversed(first14):
        addend = factor * _GST_CHARS.index(ch)
        factor = 1 if factor == 2 else 2
        total += addend // 36 + addend % 36
    return _GST_CHARS[(36 - total % 36) % 36]


def gstin_valid(gstin: str | None) -> tuple[bool, str]:
    g = (gstin or "").strip().upper()
    if not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", g):
        return False, "format must be 2-digit state + PAN + entity + 'Z' + checksum"
    if not 1 <= int(g[:2]) <= 38 and g[:2] != "97":
        return False, f"invalid state code {g[:2]}"
    if gstin_checksum(g[:14]) != g[14]:
        return False, "checksum digit does not match (typo?)"
    return True, "ok"
