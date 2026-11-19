"""Match the money in the bank to the claims it pays for.

TPAs and government trusts pay in lump sums: one NEFT of Rs 1,93,500 can settle two claims, and the bank
narration names the payer and a UTR, never the claims. Until each credit is tied to its claims, the
receivables ledger keeps showing paid claims as outstanding, and deductions go unnoticed.

    bank statement (CSV / XLSX)  ->  payer credits  --UTR-->  settlement letters  ->  ledger: paid, TDS, deduction + reasons
                                                    --amount + payer (no letter)-->  suggested claims, a person confirms

What it flags:
  * a letter says "paid" but no credit with its UTR is in a statement that covers the payment date;
  * money from a payer arrived with no letter and matches no open claim (ask for the remittance advice);
  * a UTR's letters do not add up to the credit, or a letter fails its own checks (settlement.py).

Only exact UTR matches with clean letters are applied automatically. Everything else waits for a person.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date, timedelta
from itertools import combinations

import pandas as pd

from ..alerts import raise_alert, resolve_alerts
from ..db import clean, get_db, now
from ..onboarding.schemas import is_blank, norm_date, norm_money, norm_text
from . import ledger
from .importer import map_columns

BANK_FIELDS: dict[str, tuple[str, ...]] = {
    "date": ("txn date", "transaction date", "date", "tran date", "posting date", "value date", "value dt"),
    "narration": ("narration", "description", "particulars", "transaction details", "transaction remarks", "remarks", "details"),
    "ref": ("chq ref no", "ref no", "chq no", "cheque no", "reference no", "utr no", "transaction id", "chq no ref no",
            "ref no cheque no", "cheque ref no"),
    "debit": ("withdrawal amt", "withdrawal", "withdrawals", "debit", "debit amount", "dr amount"),
    "credit": ("deposit amt", "deposit", "deposits", "credit", "credit amount", "cr amount"),
    "amount": ("amount", "transaction amount", "amount inr"),
    "drcr": ("dr cr", "cr dr", "transaction type", "type"),
}
GENERIC = {"tpa", "insurance", "health", "general", "company", "co", "ltd", "limited", "pvt", "private", "services", "service",
           "trust", "care", "the", "of", "and", "india", "scheme", "bharat", "employees", "government", "administrator",
           "administrators", "assurance", "medical", "hospital"}
PAYABLE = {"APPROVED", "PART_PAID"}  # what a payer can be paying for
TDS_MODES = {"gross": 0.0, "tds10": 0.10}
MAX_COMBO = 4
SETTLE_DAYS = 2  # NEFT / RTGS land the same or next working day


class StatementError(ValueError):
    """A file the matcher cannot use (no date / narration / credit columns)."""


def _norm_id(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _words(s, join: bool = False) -> list[str]:
    s = str(s or "").lower()
    if join:  # PM-JAY -> pmjay (payer names); narrations are matched both ways
        s = re.sub(r"(?<=[a-z0-9])[-.](?=[a-z0-9])", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).split()


def _grid(blob: bytes, filename: str) -> list[list]:
    if filename.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(blob), header=None, dtype=object).values.tolist()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = blob.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:4000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def parse_statement(blob: bytes, filename: str, *, shift_days: int = 0) -> dict:
    """Bank statement export -> every row's date, and the credits. Account details above the table are skipped."""
    grid = _grid(blob, filename)
    header, m = None, {}
    for i, row in enumerate(grid[:40]):
        cols = [str(c).strip() if not is_blank(c) else f"_{j}" for j, c in enumerate(row)]
        mm = map_columns(cols, BANK_FIELDS)
        if {"date", "narration"} <= mm.keys() and ({"credit"} <= mm.keys() or {"amount"} <= mm.keys()):
            header, m = i, {f: cols.index(c) for f, c in mm.items()}
            break
    if header is None:
        raise StatementError("Could not find the transactions table: need date, narration / description and credit / deposit columns.")

    def cell(row, f):
        j = m.get(f)
        return row[j] if j is not None and j < len(row) else None

    days, credits = [], []
    for k, row in enumerate(grid[header + 1:]):
        d = norm_date(cell(row, "date"))
        if not d:
            continue
        d = d + timedelta(days=shift_days)
        days.append(d)
        narration = norm_text(cell(row, "narration")) or ""
        if "credit" in m:
            amt = norm_money(cell(row, "credit"))
        else:
            amt = norm_money(cell(row, "amount"))
            kind = (norm_text(cell(row, "drcr")) or "").lower()
            if amt is not None and amt < 0 or kind.startswith("d"):
                amt = None
        if not amt or amt <= 0 or re.search(r"(?i)opening balance|closing balance", narration):
            continue
        ref = norm_text(cell(row, "ref")) or ""
        key = hashlib.sha1(f"{d}|{amt:.2f}|{_norm_id(narration)}|{_norm_id(ref)}".encode()).hexdigest()[:16]
        credits.append({"_id": key, "date": d.isoformat(), "narration": narration, "ref": ref, "amount": amt,
                        "row": header + k + 2})
    if not days:
        raise StatementError("No dated transactions found.")
    return {"filename": filename, "period_from": min(days).isoformat(), "period_to": max(days).isoformat(),
            "rows": len(days), "credits": credits}


# ------------------------------------------------------------------ storage

def add_statement(blob: bytes, filename: str, *, shift_days: int = 0) -> dict:
    st = parse_statement(blob, filename, shift_days=shift_days)
    db = get_db()
    new = 0
    for c in st["credits"]:
        if not db.bank_credits.find_one({"_id": c["_id"]}):
            db.bank_credits.insert_one({**c, "statement": filename, "status": "open", "added_at": now()})
            new += 1
    db.bank_statements.insert_one({k: st[k] for k in ("filename", "period_from", "period_to", "rows")} |
                                  {"credits": len(st["credits"]), "new_credits": new, "at": now()})
    return {"filename": filename, "period_from": st["period_from"], "period_to": st["period_to"], "rows": st["rows"],
            "credits": len(st["credits"]), "new_credits": new}


def add_letter(parsed: dict) -> dict:
    """Store a parsed settlement letter. Re-uploading the same letter (claim + UTR) replaces it unless it is
    already reconciled."""
    db = get_db()
    f = parsed["fields"]
    key = f"{_norm_id(f.get('claim_ref')) or parsed['filename']}:{f.get('utr') or 'NOUTR'}"
    old = db.settlement_letters.find_one({"_id": key})
    if old and old["status"] == "reconciled":
        return {**clean(old), "duplicate": True}
    doc = {"_id": key, **parsed, "status": "review" if parsed["needs_review"] else "open", "uploaded_at": now()}
    db.settlement_letters.replace_one({"_id": key}, doc, upsert=True)
    return clean(doc)


# ------------------------------------------------------------------ matching

def _payer_index() -> list[tuple[str, str, set[str]]]:
    seen = {}
    for r in get_db().receivables.find({}, {"payer_type": 1, "payer": 1}):
        seen[(r["payer_type"], r["payer"])] = {w for w in _words(r["payer"], join=True) if len(w) >= 3 and w not in GENERIC}
    return [(pt, p, toks) for (pt, p), toks in seen.items() if toks]


def guess_payer(narration: str, index=None) -> tuple[str, str] | tuple[None, None]:
    """Which payer a credit came from, by the payer's distinctive name words in the bank narration.
    No guess when two payers match equally well."""
    words = set(_words(narration)) | set(_words(narration, join=True))
    hits = sorted(((len(toks & words), pt, p) for pt, p, toks in (index or _payer_index()) if toks & words),
                  key=lambda h: -h[0])
    if not hits or (len(hits) > 1 and hits[0][0] == hits[1][0]):
        return None, None
    return hits[0][1], hits[0][2]


def _find_receivable(claim_ref: str | None) -> dict | None:
    if not claim_ref:
        return None
    db = get_db()
    for pt in ledger.PAYER_TYPES:
        r = db.receivables.find_one({"_id": ledger.rid_for(pt, claim_ref)})
        if r:
            return r
    return None


def _open_amount(r: dict) -> float:
    return r.get("outstanding") or 0


def _combos(credit: dict, payer_type: str, payer: str, exclude: set[str]) -> list[dict]:
    """Sets of open claims for this payer whose outstanding (gross, or net of 10% TDS) adds up to the credit."""
    db = get_db()
    cands = [r for r in db.receivables.find({"payer_type": payer_type, "payer": payer, "status": {"$in": sorted(PAYABLE)}})
             if _open_amount(r) > 0 and r["_id"] not in exclude]
    cands = sorted(cands, key=lambda r: r.get("submitted_on") or "")[:18]
    found = []
    for mode, tds in TDS_MODES.items():
        for k in range(1, min(MAX_COMBO, len(cands)) + 1):
            for combo in combinations(cands, k):
                total = sum(round(_open_amount(r) * (1 - tds)) for r in combo)
                if abs(total - credit["amount"]) <= k:
                    found.append({"mode": mode, "receivable_ids": [r["_id"] for r in combo],
                                  "claims": [{"id": r["_id"], "claim_ref": r["claim_ref"], "patient_name": r.get("patient_name"),
                                              "outstanding": _open_amount(r)} for r in combo], "total": total})
    return found


def _covered(d: str) -> str | None:
    """The statement period that covers a payment date (+ settlement days), if any."""
    pay = date.fromisoformat(d)
    for s in get_db().bank_statements.find():
        if date.fromisoformat(s["period_from"]) <= pay and date.fromisoformat(s["period_to"]) >= pay + timedelta(days=SETTLE_DAYS):
            return s["period_to"]
    return None


def reconcile() -> dict:
    """Apply every exact UTR match with clean letters; work out what needs a person. Idempotent."""
    db = get_db()
    letters = list(db.settlement_letters.find({"status": {"$ne": "reconciled"}}))
    credits = list(db.bank_credits.find({"status": "open"}))
    review, applied_now = [], []
    used_letters: set[str] = set()
    for c in credits:
        hay = _norm_id(c["narration"]) + "|" + _norm_id(c["ref"])
        ls = [x for x in letters if x["fields"].get("utr") and x["fields"]["utr"] in hay]
        if not ls:
            continue
        used_letters |= {x["_id"] for x in ls}
        total = sum(x["fields"].get("net_paid") or 0 for x in ls)
        if any(x["status"] == "review" for x in ls):
            review.append({"kind": "letter_review", "credit": clean(c), "letters": [clean(x) for x in ls], "letters_total": total})
        elif abs(total - c["amount"]) > len(ls):
            review.append({"kind": "amount_mismatch", "credit": clean(c), "letters": [clean(x) for x in ls], "letters_total": total})
        else:
            applied_now.append(_apply_letters(c, ls))
    # Credits with no letter: match by payer + amount against open claims (suggestion only).
    index = _payer_index()
    expected = {r["_id"] for x in letters if (r := _find_receivable(x["fields"].get("claim_ref")))}
    others, unexplained = [], []
    for c in db.bank_credits.find({"status": "open"}):
        if any(c["_id"] == r["credit"]["_id"] for r in review):
            continue
        pt, payer = guess_payer(c["narration"], index)
        if not pt:
            others.append(clean(c))
            continue
        opts = _combos(c, pt, payer, expected)
        item = {"credit": clean(c), "payer_type": pt, "payer": payer, "options": opts[:5]}
        if len(opts) == 1:
            review.append({"kind": "suggested", **item})
        elif opts:
            review.append({"kind": "ambiguous", **item})
        else:
            unexplained.append(item)
            raise_alert("unexplained_credit", "warning", f"{_inr(c['amount'])} from {payer} with no remittance advice",
                        f"Bank credit on {c['date']} ({c['narration'][:80]}) matches no letter and no open claim. "
                        "Ask the payer which claims it settles.", dedupe_key=f"recon:credit:{c['_id']}")
    # Letters whose money is not in the bank.
    missing, waiting = [], []
    for x in db.settlement_letters.find({"status": "open"}):
        if x["_id"] in used_letters:
            continue
        f = x["fields"]
        until = _covered(f["payment_date"]) if f.get("payment_date") else None
        (missing if until else waiting).append({**clean(x), "statement_to": until})
        if until:
            raise_alert("payment_missing", "critical",
                        f"TPA says paid, money not in bank - {f.get('patient_name') or ''} ({f.get('claim_ref')})",
                        f"{f.get('payer') or 'The TPA'} reports {_inr(f.get('net_paid') or 0)} paid on {f['payment_date']} "
                        f"by UTR {f.get('utr')}, but no credit with that UTR is in the bank statement up to {until}. "
                        "Ask the TPA to confirm the beneficiary account and the bank to trace the UTR.",
                        dedupe_key=f"recon:missing:{x['_id']}")
    review += [{"kind": "letter_review", "credit": None, "letters": [clean(x)], "letters_total": x["fields"].get("net_paid")}
               for x in db.settlement_letters.find({"status": "review"}) if x["_id"] not in used_letters]
    return view(review=review, unexplained=unexplained, missing=missing, waiting=waiting, others=others, applied_now=applied_now)


def view(**parts) -> dict:
    db = get_db()
    matched = [clean(c) for c in db.bank_credits.find({"status": "reconciled"}).sort("date", -1)]
    payer_credits = matched + [r["credit"] for r in parts["review"] if r.get("credit")] + [u["credit"] for u in parts["unexplained"]]
    return {
        "totals": {
            "received": sum(c["amount"] for c in payer_credits), "received_count": len(payer_credits),
            "matched": sum(c["amount"] for c in matched), "matched_count": len(matched),
            "needs_decision": len(parts["review"]),
            "missing_amount": sum(x["fields"].get("net_paid") or 0 for x in parts["missing"]), "missing_count": len(parts["missing"]),
            "unexplained_amount": sum(u["credit"]["amount"] for u in parts["unexplained"]), "unexplained_count": len(parts["unexplained"]),
        },
        "matched": matched, **parts,
        "statements": [clean(s) for s in db.bank_statements.find().sort("at", -1).limit(5)],
        "letters": [clean(x) for x in db.settlement_letters.find().sort("uploaded_at", -1)],
    }


# ------------------------------------------------------------------ applying a match to the ledger

def _inr(x: float) -> str:
    from ..claims.bill_scrub import _inr as fmt

    return fmt(x)


def _apply_letters(credit: dict, letters: list[dict], how: str = "utr") -> dict:
    db = get_db()
    applied = []
    for x in letters:
        f = x["fields"]
        old = _find_receivable(f["claim_ref"])
        approved, tds, net = f.get("approved_amount"), f.get("tds_amount") or 0, f.get("net_paid") or 0
        reasons = "; ".join(d["reason"] for d in f.get("deductions") or [])
        rec = {"payer_type": old["payer_type"] if old else "private_tpa", "claim_ref": f["claim_ref"],
               "payer": (old or {}).get("payer") or f.get("payer"), "patient_name": (old or {}).get("patient_name") or f.get("patient_name"),
               "billed": (old or {}).get("billed") or f.get("claimed_amount"), "approved": approved, "paid": net, "tds": tds,
               "deduction_explicit": f.get("deduction_amount"), "remarks": reasons or (old or {}).get("remarks"),
               "status": "PAID" if approved is None or net + tds >= approved - 1 else "PART_PAID",
               "paid_on": credit["date"], "utr": f.get("utr"), "status_raw": f"Settled by {how.upper()} {f.get('utr') or ''}".strip()}
        res = ledger.upsert(rec, source="settlement", source_file=x["filename"])
        r = db.receivables.find_one({"_id": res["id"]})
        db.settlement_letters.update_one({"_id": x["_id"]}, {"$set": {"status": "reconciled", "credit_id": credit["_id"],
                                                                       "receivable_id": res["id"], "reconciled_at": now()}})
        resolve_alerts(f"recon:missing:{x['_id']}")
        if r.get("claim_id"):
            from ..claims import service

            service._event(r["claim_id"], "payment_reconciled", {"utr": f.get("utr"), "net_paid": net, "tds": tds,
                                                                  "deduction": r.get("deduction"), "reasons": reasons})
        applied.append({"receivable_id": res["id"], "claim_ref": f["claim_ref"], "patient_name": r.get("patient_name"),
                        "approved": approved, "net_paid": net, "tds": tds, "deduction": r.get("deduction"), "reasons": reasons,
                        "predicted_deduction": r.get("predicted_deduction"), "letter": x["filename"], "new_in_ledger": old is None})
    db.bank_credits.update_one({"_id": credit["_id"]}, {"$set": {"status": "reconciled", "method": how, "matches": applied,
                                                                 "reconciled_at": now()}})
    resolve_alerts(f"recon:credit:{credit['_id']}")
    return {"credit_id": credit["_id"], "amount": credit["amount"], "matches": applied}


class MatchError(ValueError):
    pass


def confirm(credit_id: str, receivable_ids: list[str], mode: str = "gross") -> dict:
    """A person confirms which open claims a letter-less credit pays. The amounts must still add up."""
    db = get_db()
    c = db.bank_credits.find_one({"_id": credit_id, "status": "open"})
    if not c:
        raise MatchError("credit not found or already matched")
    if mode not in TDS_MODES or not receivable_ids:
        raise MatchError("choose the claims and gross / tds10")
    rs = [db.receivables.find_one({"_id": rid}) for rid in receivable_ids]
    if not all(rs) or any(r["status"] not in PAYABLE for r in rs):
        raise MatchError("every claim must be open (approved, unpaid)")
    nets = [round(_open_amount(r) * (1 - TDS_MODES[mode])) for r in rs]
    if abs(sum(nets) - c["amount"]) > len(rs):
        raise MatchError(f"these claims add up to {sum(nets):,.0f}, not {c['amount']:,.0f}")
    applied = []
    for r, net in zip(rs, nets):
        tds = _open_amount(r) - net
        res = ledger.upsert({"payer_type": r["payer_type"], "claim_ref": r["claim_ref"], "status": "PAID",
                             "paid": (r.get("paid") or 0) + net, "tds": (r.get("tds") or 0) + tds, "paid_on": c["date"],
                             "status_raw": "Matched to bank credit by amount (no remittance advice)"},
                            source="bank_match", source_file=c.get("statement"))
        applied.append({"receivable_id": res["id"], "claim_ref": r["claim_ref"], "patient_name": r.get("patient_name"),
                        "approved": r.get("approved"), "net_paid": net, "tds": tds, "deduction": r.get("deduction"),
                        "reasons": r.get("remarks"), "predicted_deduction": r.get("predicted_deduction")})
    db.bank_credits.update_one({"_id": credit_id}, {"$set": {"status": "reconciled", "method": f"amount ({mode}), confirmed",
                                                             "matches": applied, "reconciled_at": now()}})
    resolve_alerts(f"recon:credit:{credit_id}")
    return {"credit_id": credit_id, "matches": applied}


def approve_letter(letter_id: str, corrections: dict | None = None) -> dict:
    """A person has checked a held letter (and corrected a misread amount, if any); it can now be matched."""
    db = get_db()
    x = db.settlement_letters.find_one({"_id": letter_id})
    if not x or x["status"] == "reconciled":
        raise MatchError("letter not found or already reconciled")
    f = dict(x["fields"])
    for k, v in (corrections or {}).items():
        if k in ("net_paid", "approved_amount", "tds_amount", "deduction_amount", "claimed_amount") and v not in (None, ""):
            f[k] = float(v)
        elif k in ("utr", "claim_ref") and v:
            f[k] = _norm_id(v) if k == "utr" else str(v).strip()
    if not f.get("claim_ref") or f.get("net_paid") is None:
        raise MatchError("a claim number and the net paid amount are needed")
    db.settlement_letters.update_one({"_id": letter_id}, {"$set": {"fields": f, "status": "open", "reviewed_at": now(),
                                                                    "corrections": corrections or {}}})
    return clean(db.settlement_letters.find_one({"_id": letter_id}))
