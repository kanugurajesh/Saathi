"""Normalise mapped rows and find the data problems that would block or poison a go-live.

Each issue carries the spreadsheet row number the hospital staff will recognise, the
offending value, and a plain-language fix - so the output can go straight back to the
hospital as a fix-list instead of a developer stack trace.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta

import pandas as pd
from rapidfuzz import fuzz

from .schemas import (ENTITIES, is_blank, norm_date, norm_gender, norm_gst, norm_int, norm_money, norm_phone,
                      norm_role, norm_text)

BLOCKER, WARNING = "blocker", "warning"
VALID_GST = {0.0, 5.0, 18.0, 40.0}  # slabs after GST rationalisation effective 22-Sep-2025
LEGACY_GST = {12.0, 28.0}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def _issue(issues, row, field, value, severity, code, message, fix):
    issues.append({"row": row, "field": field, "value": None if is_blank(value) else str(value)[:80],
                   "severity": severity, "code": code, "message": message, "fix": fix})


def apply_mapping(df: pd.DataFrame, mapping: list[dict]) -> list[dict]:
    col_for = {m["target"]: m["source"] for m in mapping if m.get("target")}
    src_cols = {str(c): c for c in df.columns}
    rows = []
    for i, rec in enumerate(df.to_dict(orient="records")):
        r = {"_row": i + 2}  # +1 header, +1 one-based -> matches Excel row numbers
        for field, src in col_for.items():
            r[field] = rec.get(src_cols.get(src, src))
        rows.append(r)
    return rows


def validate(entity: str, raw_rows: list[dict], mapping: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    issues: list[dict] = []
    fields = ENTITIES[entity]["fields"]
    mapped = {m["target"] for m in mapping if m.get("target")}
    for f, spec in fields.items():
        if spec.get("required") and f not in mapped:
            _issue(issues, None, f, None, BLOCKER, "missing_column",
                   f"Required column '{f}' not found in the file.", f"Add a '{f.replace('_', ' ')}' column or confirm the mapping.")
    validator = {"drug_master": _drugs, "lab_tests": _priced("test_name"), "tariff": _priced("service_name"),
                 "staff": _staff, "patients": _patients}[entity]
    clean_rows = validator(raw_rows, issues, today)
    blocked_rows = {i["row"] for i in issues if i["severity"] == BLOCKER and i["row"]}
    return {
        "rows_total": len(raw_rows),
        "rows_clean": sum(1 for r in clean_rows if r["_row"] not in blocked_rows),
        "rows_blocked": len(blocked_rows),
        "issues": issues,
        "summary": _summarise(issues),
        "clean_rows": clean_rows,
    }


def _summarise(issues):
    by = defaultdict(lambda: {"count": 0, "severity": None, "message": None, "fix": None})
    for i in issues:
        b = by[i["code"]]
        b["count"] += 1
        b.update(severity=i["severity"], message=i["message"], fix=i["fix"])
    return sorted(({"code": k, **v} for k, v in by.items()), key=lambda x: (x["severity"] != BLOCKER, -x["count"]))


def _drugs(rows, issues, today):
    out, seen = [], {}
    for r in rows:
        n = r["_row"]
        c = {"_row": n, "drug_name": norm_text(r.get("drug_name")), "generic_name": norm_text(r.get("generic_name")),
             "manufacturer": norm_text(r.get("manufacturer")), "hsn_code": norm_text(r.get("hsn_code")),
             "gst_rate": norm_gst(r.get("gst_rate")), "pack_size": norm_text(r.get("pack_size")),
             "batch_no": norm_text(r.get("batch_no")), "expiry_date": norm_date(r.get("expiry_date"), expiry=True),
             "mrp": norm_money(r.get("mrp")), "purchase_rate": norm_money(r.get("purchase_rate")),
             "stock_qty": norm_money(r.get("stock_qty")), "schedule": (norm_text(r.get("schedule")) or "").upper() or None}
        if not c["drug_name"]:
            _issue(issues, n, "drug_name", r.get("drug_name"), BLOCKER, "drug_name_missing", "Drug name is empty.", "Fill the item name or delete the row.")
        if c["mrp"] is None or c["mrp"] <= 0:
            _issue(issues, n, "mrp", r.get("mrp"), BLOCKER, "mrp_invalid", "MRP missing or not a number.", "Enter MRP as printed on the strip.")
        elif c["purchase_rate"] and c["purchase_rate"] > c["mrp"]:
            _issue(issues, n, "purchase_rate", r.get("purchase_rate"), BLOCKER, "rate_above_mrp",
                   f"Purchase rate {c['purchase_rate']:.2f} is higher than MRP {c['mrp']:.2f}.", "Check for swapped MRP / rate columns.")
        g = c["gst_rate"]
        if g is None:
            _issue(issues, n, "gst_rate", r.get("gst_rate"), BLOCKER, "gst_missing", "GST rate missing.", "Enter GST % from the purchase invoice.")
        elif g in LEGACY_GST:
            _issue(issues, n, "gst_rate", r.get("gst_rate"), WARNING, "gst_legacy_slab",
                   "Legacy 12% / 28% GST slab - removed in the GST rationalisation effective 22-Sep-2025 (most medicines moved to 5%).",
                   "Confirm the current rate with the hospital's CA before go-live.")
        elif g not in VALID_GST:
            _issue(issues, n, "gst_rate", r.get("gst_rate"), BLOCKER, "gst_invalid", f"{g:g}% is not a valid GST slab.", "Use 0, 5, 18 or 40.")
        h = re.sub(r"\D", "", c["hsn_code"] or "")
        if not h:
            _issue(issues, n, "hsn_code", r.get("hsn_code"), WARNING, "hsn_missing", "HSN code missing (required on GST invoices).", "Copy HSN from the supplier invoice.")
        elif len(h) not in (4, 6, 8):
            _issue(issues, n, "hsn_code", r.get("hsn_code"), WARNING, "hsn_format", "HSN should be 4, 6 or 8 digits.", "Correct the HSN code.")
        c["hsn_code"] = h or None
        exp = c["expiry_date"]
        if exp is None:
            _issue(issues, n, "expiry_date", r.get("expiry_date"), BLOCKER, "expiry_unreadable", "Expiry date missing or unreadable.", "Use MM/YY or DD/MM/YYYY.")
        elif exp < today:
            _issue(issues, n, "expiry_date", r.get("expiry_date"), BLOCKER, "expired_stock", f"Stock expired on {exp:%d-%b-%Y}.",
                   "Move to expired/return stock - do not import as saleable.")
        elif exp < today + timedelta(days=90):
            _issue(issues, n, "expiry_date", r.get("expiry_date"), WARNING, "near_expiry", f"Expires {exp:%d-%b-%Y} (< 90 days).", "Flag for return to supplier / priority sale.")
        q = c["stock_qty"]
        if q is not None and q < 0:
            _issue(issues, n, "stock_qty", r.get("stock_qty"), BLOCKER, "negative_stock", "Negative stock quantity.", "Do a physical count for this item.")
        if c["drug_name"]:
            key = (re.sub(r"[^a-z0-9]", "", c["drug_name"].lower()), (c["batch_no"] or "").lower())
            if key in seen:
                _issue(issues, n, "drug_name", c["drug_name"], BLOCKER, "duplicate_batch", f"Same item + batch as row {seen[key]}.", "Merge the two rows (add quantities).")
            else:
                seen[key] = n
        if exp:
            c["expiry_date"] = exp.isoformat()
        out.append(c)
    _fuzzy_dupes(out, "drug_name", issues, extra_key="mrp")
    return out


def _fuzzy_dupes(rows, field, issues, extra_key=None):
    names = [(r["_row"], r.get(field), r.get(extra_key) if extra_key else None) for r in rows if r.get(field)]
    flagged = set()
    for i in range(len(names)):
        for j in range(i + 1, min(len(names), i + 400)):
            (ri, a, ka), (rj, b, kb) = names[i], names[j]
            if rj in flagged or a.lower() == b.lower():
                continue
            if fuzz.token_sort_ratio(_canon(a), _canon(b)) >= 92 and (extra_key is None or ka == kb):
                flagged.add(rj)
                _issue(issues, rj, field, b, WARNING, "possible_duplicate", f"Looks like a duplicate of row {ri} ('{a}').", "Keep one spelling to avoid split stock / billing.")


def _canon(s: str) -> str:
    s = s.lower()
    for a, b in (("tablet", "tab"), ("tabs", "tab"), ("capsule", "cap"), ("injection", "inj"), ("syrup", "syp"), (" mg", "mg"), (" ml", "ml")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9 ]", " ", s)


def _priced(name_field):
    def run(rows, issues, today):
        out, seen = [], {}
        for r in rows:
            n = r["_row"]
            c = {k: (norm_money(v) if k in ("price", "insurance_price") else norm_int(v) if k == "tat_hours" else norm_text(v))
                 for k, v in r.items() if k != "_row"}
            c["_row"] = n
            if not c.get(name_field):
                _issue(issues, n, name_field, r.get(name_field), BLOCKER, "name_missing", "Name is empty.", "Fill or delete the row.")
            if c.get("price") is None or c["price"] <= 0:
                _issue(issues, n, "price", r.get("price"), BLOCKER, "price_invalid", "Price missing / zero / not a number.",
                       "Enter the cash price (billing cannot charge a blank rate).")
            cash, tpa = c.get("price"), c.get("insurance_price")
            if cash and tpa and not (0.2 <= cash / tpa <= 5):
                _issue(issues, n, "price", r.get("price"), WARNING, "cash_tpa_mismatch",
                       f"Cash rate {cash:,.0f} vs TPA rate {tpa:,.0f} differ {max(cash / tpa, tpa / cash):.0f}x.",
                       "Check for a missing / extra zero.")
            if c.get(name_field):
                k = c[name_field].lower()
                if k in seen:
                    _issue(issues, n, name_field, c[name_field], BLOCKER, "duplicate_name", f"Duplicate of row {seen[k]}.", "Keep one row; billing needs unique names.")
                seen.setdefault(k, n)
            out.append(c)
        prices = sorted(c["price"] for c in out if c.get("price"))
        if len(prices) >= 8:
            p95 = prices[int(len(prices) * 0.95) - 1]
            for c in out:
                if c.get("price") and c["price"] > p95 * 10:
                    _issue(issues, c["_row"], "price", c["price"], WARNING, "price_outlier", f"Price {c['price']:,.0f} is 10x above typical.", "Check for an extra zero.")
        _fuzzy_dupes(out, name_field, issues)
        return out
    return run


def _staff(rows, issues, today):
    out, phones = [], {}
    for r in rows:
        n = r["_row"]
        role = norm_role(r.get("role")) or norm_role(r.get("full_name"))
        c = {"_row": n, "full_name": norm_text(r.get("full_name")), "role": role, "role_raw": norm_text(r.get("role")),
             "department": norm_text(r.get("department")), "phone": norm_phone(r.get("phone")),
             "email": (norm_text(r.get("email")) or "").lower() or None, "registration_number": norm_text(r.get("registration_number"))}
        if not c["full_name"]:
            _issue(issues, n, "full_name", r.get("full_name"), BLOCKER, "name_missing", "Staff name empty.", "Fill the name.")
        if not role:
            _issue(issues, n, "role", r.get("role"), WARNING, "role_unknown", f"Could not map role '{r.get('role')}'.", "Pick a role: doctor / nurse / pharmacist / lab / reception / billing / admin.")
        if c["phone"] is None:
            _issue(issues, n, "phone", r.get("phone"), BLOCKER, "phone_invalid", "Mobile number invalid - needed for login OTP & WhatsApp.", "Enter a 10-digit Indian mobile number.")
        elif c["phone"] in phones:
            _issue(issues, n, "phone", c["phone"], BLOCKER, "phone_duplicate", f"Same mobile as row {phones[c['phone']]} - logins must be unique.", "Use each person's own number.")
        else:
            phones[c["phone"]] = n
        if c["email"] and not EMAIL_RE.match(c["email"]):
            _issue(issues, n, "email", r.get("email"), WARNING, "email_invalid", "Email looks invalid.", "Fix or leave blank.")
        if role == "doctor" and not c["registration_number"]:
            _issue(issues, n, "registration_number", None, BLOCKER, "doctor_reg_missing",
                   "Doctor without medical council registration number (printed on every prescription, needed for TPA claims).",
                   "Collect the State Medical Council / NMC registration number.")
        out.append(c)
    return out


def _patients(rows, issues, today):
    out, seen = [], {}
    for r in rows:
        n = r["_row"]
        dob = norm_date(r.get("dob"))
        c = {"_row": n, "uhid": norm_text(r.get("uhid")), "full_name": norm_text(r.get("full_name")),
             "gender": norm_gender(r.get("gender")), "age": norm_int(r.get("age")), "dob": dob.isoformat() if dob else None,
             "phone": norm_phone(r.get("phone")), "address": norm_text(r.get("address")),
             "abha_number": re.sub(r"\D", "", str(r.get("abha_number") or "")) or None}
        if not c["full_name"]:
            _issue(issues, n, "full_name", r.get("full_name"), BLOCKER, "name_missing", "Patient name empty.", "Fill or drop the row.")
        if c["phone"] is None and not is_blank(r.get("phone")):
            _issue(issues, n, "phone", r.get("phone"), WARNING, "phone_invalid", "Mobile invalid - WhatsApp reports/reminders won't reach.", "Correct the number.")
        if c["abha_number"] and len(c["abha_number"]) != 14:
            _issue(issues, n, "abha_number", r.get("abha_number"), WARNING, "abha_format", "ABHA number should be 14 digits.", "Re-check the ABHA number.")
            c["abha_number"] = None
        if dob and c["age"] is not None and abs((today.year - dob.year) - c["age"]) > 1:
            _issue(issues, n, "age", r.get("age"), WARNING, "age_dob_conflict", "Age and DOB disagree.", "Prefer DOB; fix age.")
        if c["full_name"] and c["phone"]:
            k = (c["full_name"].lower(), c["phone"])
            if k in seen:
                _issue(issues, n, "full_name", c["full_name"], WARNING, "duplicate_patient", f"Same name + mobile as row {seen[k]}.", "Merge records (keep oldest UHID).")
            seen.setdefault(k, n)
        out.append(c)
    return out
