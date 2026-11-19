"""Deduction recovery: find the money a TPA cut that the rules do not allow, and ask for it back.

The discharge bill check (claims/bill_scrub.py) says what the TPA *should* cut. The settlement letter
(receivables/settlement.py) says what it *did* cut, reason by reason. Comparing the two, category by
category, finds the over-deductions:
  * a proportionate (room-rent) deduction applied to pharmacy, consumables, implants or investigations,
    which the rules exempt;
  * items cut as "non-payable" that are not on IRDAI's standard lists (a cannula, sutures, a medicine);
  * co-pay taken on the claimed amount instead of the admissible amount;
  * more cut under any rule than the bill check computes.
Without a bill check, the letter's own text and numbers are still checked (named items, co-pay arithmetic).

Every finding gets a verdict:
  dispute   a rule says the deduction is wrong; the amount is computed, not guessed
  resubmit  cut for a missing document; send it and ask for the amount back
  verify    needs a person (rate / MRP disputes, clinical justification, no bill to check against)
  valid     the deduction follows the rules
The reconsideration letter goes through the same safety gate as every other draft: it can only quote amounts
that are in the record, and documents nobody has attached stay as [[placeholders]].
"""
from __future__ import annotations

import re
from collections import defaultdict

from rapidfuzz import fuzz

from ..claims import bill_scrub as bs
from ..claims import drafting
from ..db import clean, get_db, now
from . import ledger

R_DOCS, R_OTHER = "Documents missing", "Other"
EXEMPT = ("pharmacy", "consumables", "implant", "investigation", "blood")  # never cut proportionately
_inr = bs._inr


def category(reason: str) -> str:
    r = (reason or "").lower()
    if re.search(r"proportionate|room rent|room category|eligible room", r):
        return bs.R_ROOM
    if re.search(r"co-?pay|sub-?limit|sum insured", r):
        return bs.R_POLICY
    if re.search(r"(not|non)[ -]?payable|irdai", r):
        return bs.R_NONPAY
    return ledger.categorize(reason) or R_OTHER


def _items(reason: str) -> list[str]:
    """Items a deduction names: '(gloves, IV cannula, sutures)', or the reason itself ('Physiotherapy not payable')."""
    inside = re.findall(r"\(([^)]*)\)", reason)
    if inside:
        parts = re.split(r",|;|/|\band\b", " ".join(inside))
    else:
        parts = re.split(r",|;|\band\b", re.sub(r"(?i)\b(not|non)[ -]?payable\b|\bas per\b.*|\bdisallowed\b", "", reason))
    generic = re.compile(r"(?i)^((non[- ]?)?medical )?(consumables?|items?|disposables?|expenses?)$|irdai|\blist\b")
    return [p.strip(" .-") for p in parts if len(p.strip(" .-")) > 2 and not generic.search(p.strip(" .-"))]


def _bill_line(item: str, lines: list[dict]) -> dict | None:
    n = bs._norm(item)
    best = max(lines, key=lambda x: fuzz.partial_ratio(n, bs._norm(x["description"])), default=None)
    return best if best and fuzz.partial_ratio(n, bs._norm(best["description"])) >= 85 else None


def _bill_check_for(claim_ref: str) -> tuple[dict | None, dict | None]:
    db = get_db()
    claim = db.claims.find_one({"portal_ref": claim_ref})
    if not claim:
        return None, None
    return claim, db.bill_checks.find_one({"claim_id": claim["_id"]}, sort=[("created_at", -1)])


def _tol(x: float) -> float:
    return max(2.0, 0.005 * abs(x))


# ------------------------------------------------------------------ text rules (also used when there is no bill)

def _text_verdict(reason: str, amount: float) -> dict:
    r = reason.lower()
    if re.search(r"not (submitted|uploaded|enclosed|received)|missing|not provided|document|report|record|notes", r):
        return {"verdict": "resubmit", "basis": "Cut for a missing document.",
                "action": "Send the document with the reconsideration request; the amount is payable once it is on file."}
    if re.search(r"above mrp|mrp", r):
        return {"verdict": "verify", "basis": "Cut as charged above MRP.",
                "action": "Check the pharmacy bill against the printed MRP. If items were billed at or below MRP, dispute with the invoices."}
    if re.search(r"tariff|agreed rate|mou|package|ceiling", r):
        return {"verdict": "verify", "basis": "Cut to the TPA's agreed tariff / package rate.",
                "action": "Compare with the signed tariff / MoU for this TPA. If the bill follows it, dispute with the tariff page."}
    if re.search(r"not justified|unjustified|excess stay|not required|length of stay", r):
        return {"verdict": "verify", "basis": "Cut as not medically justified.",
                "action": "Reply with the treating doctor's justification and the supporting notes."}
    if re.search(r"attendant|food|laundry|telephone|toiletr|medical records?|certificate|admission|registration", r):
        return {"verdict": "valid", "basis": "Items on IRDAI's standard lists of non-payable items.", "action": "Collect from the patient."}
    return {"verdict": "verify", "basis": "No rule to check this reason against.", "action": "Read the reason and decide."}


def _copay_check(reason: str, amount: float, letter: dict, others: float) -> dict | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", reason)
    claimed = letter.get("claimed_amount")
    if not m or not claimed:
        return None
    pct = float(m.group(1))
    if abs(amount - claimed * pct / 100) <= _tol(amount) and others > 0:
        right = (claimed - others) * pct / 100
        return {"verdict": "dispute", "amount": round(amount - right),
                "basis": f"Co-pay of {pct:g}% was taken on the claimed amount ({_inr(claimed)}) instead of the admissible "
                         f"amount after the other deductions ({_inr(claimed - others)}).",
                "evidence": [f"{pct:g}% of {_inr(claimed - others)} = {_inr(right)}; the letter took {_inr(amount)}."]}
    return {"verdict": "valid", "amount": 0, "basis": f"Co-pay of {pct:g}% matches the letter's own figures.", "evidence": []}


# ------------------------------------------------------------------ analysis of one settlement letter

def analyse(letter: dict) -> dict:
    f = letter["fields"]
    lines = [{"reason": d["reason"], "amount": float(d["amount"]), "category": category(d["reason"])} for d in f.get("deductions") or []]
    claimed = f.get("claimed_amount")
    deducted = f.get("deduction_amount") or sum(x["amount"] for x in lines)
    claim, bc = _bill_check_for(f.get("claim_ref") or "")
    findings: list[dict] = []
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for x in lines:
        by_cat[x["category"]].append(x)

    def add(verdict, cat, amount, basis, xs, evidence=(), action=None):
        findings.append({"verdict": verdict, "category": cat, "amount": round(amount) if amount is not None else None,
                         "basis": basis, "evidence": list(evidence), "action": action,
                         "letter_lines": [{"reason": x["reason"], "amount": round(x["amount"])} for x in xs]})

    predicted = {}
    if bc:
        predicted = {r["category"]: r["amount"] for r in bc["by_reason"]}
        bill = bc["lines"]
        for cat in (bs.R_ROOM, bs.R_NONPAY, bs.R_POLICY):
            xs = by_cat.pop(cat, [])
            actual, pred = sum(x["amount"] for x in xs), predicted.get(cat, 0)
            if not xs:
                continue
            excess = actual - pred
            if excess <= _tol(actual):
                add("valid", cat, actual, f"Matches the bill check ({_inr(pred)} predicted).", xs)
                continue
            if pred:
                add("valid", cat, pred, f"The part the rules allow ({_inr(pred)}, per the bill check).", xs)
            if cat == bs.R_ROOM:
                rr = bc.get("room_rent") or {}
                ratio = rr.get("ratio", 1)
                exempt = sum(x["payable"] for x in bill if x["category"] in EXEMPT)
                ev = [f"Room limit {_inr(rr['limit_per_day'])}/day; proportionate ratio {ratio * 100:.1f}% (bill check)."] if rr else []
                if ratio < 1 and abs(exempt * (1 - ratio) - excess) <= _tol(excess) + 2:
                    basis = (f"The proportionate deduction was applied to pharmacy, consumables, implants and investigations "
                             f"({_inr(exempt)}), which are exempt from it; only nursing, doctor visits, surgeon / OT fees "
                             "and other linked charges move with the room category.")
                    ev.append(f"{(1 - ratio) * 100:.1f}% of {_inr(exempt)} = {_inr(exempt * (1 - ratio))}.")
                elif not rr or rr.get("room_excess", 0) == 0:
                    basis = "The room was within the policy's room-rent limit, so no room-rent or proportionate deduction applies."
                else:
                    basis = f"More was cut for room rent than the policy's limit allows ({_inr(pred)} per the bill check)."
                add("dispute", cat, excess, basis, xs, ev)
            elif cat == bs.R_NONPAY:
                wrong, ev = 0.0, []
                for x in xs:
                    for item in _items(x["reason"]):
                        ln = _bill_line(item, bill)
                        listed = (ln or {}).get("irdai_list") or bs.nonpayable_list(item)
                        if ln and not listed:
                            wrong += ln["amount"]
                            ev.append(f"'{ln['description']}' ({_inr(ln['amount'])}, bill row {ln['row']}) is not on IRDAI's lists.")
                found = min(wrong, excess)
                if found > 0:
                    add("dispute", cat, found, "Payable items were cut as non-payable: they are not on IRDAI's standard "
                                               "lists of non-payable items.", xs, ev)
                if excess - found > _tol(excess):
                    add("verify", cat, excess - found, "More was cut as non-payable than the IRDAI-listed items on the bill.",
                        xs, [], "Ask the TPA for the item-wise list of what it treated as non-payable.")
            else:  # co-pay / sub-limit / sum insured
                adj = {a["rule"]: a["amount"] for a in bc.get("adjustments", [])}
                pct = (bc.get("policy") or {}).get("copay_pct")
                ev = [f"Admissible per the bill check: {_inr(bc['admissible'])}."]
                if pct and claimed and any(abs(x["amount"] - claimed * pct / 100) <= _tol(x["amount"]) for x in xs):
                    basis = (f"Co-pay of {pct:g}% was taken on the claimed amount ({_inr(claimed)}) instead of the "
                             f"admissible amount.")
                    ev.append(f"{pct:g}% co-pay on the admissible amount = {_inr(adj.get('copay', 0))}.")
                else:
                    basis = f"More was cut under policy limits than the policy terms allow ({_inr(pred)} per the bill check)."
                add("dispute", cat, excess, basis, xs, ev)
    for cat, xs in by_cat.items():
        for x in xs:
            if cat == bs.R_POLICY:
                others = deducted - x["amount"]
                c = _copay_check(x["reason"], x["amount"], f, others)
                if c:
                    if c["verdict"] == "dispute":
                        add("valid", cat, x["amount"] - c["amount"], "The part of the co-pay the policy allows.", [x])
                    add(c["verdict"], cat, c["amount"] if c["verdict"] == "dispute" else x["amount"], c["basis"], [x], c["evidence"])
                    continue
            if cat == bs.R_ROOM and re.search(r"(?i)total bill|entire bill|whole bill|all charges|pharmacy|medicine|consumable|implant|investigation", x["reason"]):
                add("dispute", cat, None, "The proportionate deduction was applied to charges that are exempt from it "
                                          "(pharmacy, consumables, implants, investigations).", [x], [],
                    "Run the discharge bill check on this claim to put a figure on it.")
                continue
            if cat == bs.R_NONPAY:
                bad = [i for i in _items(x["reason"]) if not bs.nonpayable_list(i)]
                if bad:
                    add("dispute", cat, None, f"Named as non-payable but not on IRDAI's lists: {', '.join(bad)}.", [x], [],
                        "Run the discharge bill check on this claim to put a figure on it.")
                    continue
                add("valid" if _items(x["reason"]) else "verify", cat, x["amount"],
                    "Items on IRDAI's standard lists of non-payable items." if _items(x["reason"])
                    else "The letter does not name the items.", [x], [],
                    None if _items(x["reason"]) else "Ask the TPA for the item-wise list, or run the discharge bill check.")
                continue
            if cat == bs.R_ROOM:
                add("verify", cat, x["amount"], "Room-rent deduction: cannot be checked without the final bill.", [x], [],
                    "Run the discharge bill check on this claim.")
                continue
            t = _text_verdict(x["reason"], x["amount"])
            add(t["verdict"], cat, x["amount"], t["basis"], [x], [], t["action"])

    tot = defaultdict(float)
    for fd in findings:
        tot[fd["verdict"]] += fd["amount"] or 0
    order = {"dispute": 0, "resubmit": 1, "verify": 2, "valid": 3}
    findings.sort(key=lambda x: (order[x["verdict"]], -(x["amount"] or 0)))
    return {
        "letter_id": letter["_id"], "claim_ref": f.get("claim_ref"), "patient_name": f.get("patient_name"), "payer": f.get("payer"),
        "claim_id": claim["_id"] if claim else None, "bill_check": bool(bc), "claimed": claimed, "approved": f.get("approved_amount"),
        "deducted": round(deducted), "utr": f.get("utr"), "payment_date": f.get("payment_date"), "letter_file": letter["filename"],
        "predicted": round(bc["bill_total"] - bc["expected_payable"]) if bc else None,
        "predicted_by_reason": {k: round(v) for k, v in predicted.items()},
        "actual_by_reason": {k: round(sum(x["amount"] for x in lines if x["category"] == k)) for k in {x["category"] for x in lines}},
        "findings": findings, "totals": {k: round(tot[k]) for k in order},
        "unquantified": sum(1 for x in findings if x["verdict"] == "dispute" and x["amount"] is None),
    }


def overview() -> dict:
    db = get_db()
    saved = {d["_id"]: d for d in db.disputes.find()}
    claims = []
    for x in db.settlement_letters.find({"status": {"$ne": "review"}}).sort("uploaded_at", -1):
        if not (x["fields"].get("deductions") or x["fields"].get("deduction_amount")):
            continue
        a = analyse(x)
        s = saved.get(x["_id"], {})
        a.update({k: s.get(k) for k in ("status", "draft_id", "recovered", "sent_at", "closed_at")})
        a["status"] = a["status"] or ("to_draft" if a["totals"]["dispute"] or a["totals"]["resubmit"] or a["unquantified"] else "nothing_to_claim")
        claims.append(a)
    rank = {"to_draft": 0, "drafted": 1, "sent": 2, "recovered": 3, "rejected": 4, "nothing_to_claim": 5}
    claims.sort(key=lambda c: (rank.get(c["status"], 9), -(c["totals"]["dispute"] + c["totals"]["resubmit"])))
    open_ = [c for c in claims if c["status"] in ("to_draft", "drafted", "sent")]
    acc_rows = [c for c in claims if c["predicted"] is not None]
    by_payer = defaultdict(lambda: {"claims": 0, "predicted": 0, "actual": 0, "over": 0})
    for c in acc_rows:
        p = by_payer[c["payer"] or "Unknown"]
        p["claims"] += 1
        p["predicted"] += c["predicted"]
        p["actual"] += c["deducted"]
        p["over"] += c["totals"]["dispute"]
    cats = sorted({k for c in acc_rows for k in (*c["predicted_by_reason"], *c["actual_by_reason"])})
    return {
        "claims": claims,
        "totals": {"deducted": sum(c["deducted"] for c in claims), "letters": len(claims),
                   "dispute": sum(c["totals"]["dispute"] for c in open_), "resubmit": sum(c["totals"]["resubmit"] for c in open_),
                   "verify": sum(c["totals"]["verify"] for c in claims if c["status"] not in ("recovered", "rejected")),
                   "recovered": sum(c.get("recovered") or 0 for c in claims),
                   "sent": sum(1 for c in claims if c["status"] == "sent")},
        "accuracy": {
            "claims": len(acc_rows), "predicted": sum(c["predicted"] for c in acc_rows), "actual": sum(c["deducted"] for c in acc_rows),
            "by_payer": [{"payer": k, **v} for k, v in sorted(by_payer.items())],
            "by_reason": [{"category": k, "predicted": sum(c["predicted_by_reason"].get(k, 0) for c in acc_rows),
                           "actual": sum(c["actual_by_reason"].get(k, 0) for c in acc_rows)} for k in cats],
        },
    }


# ------------------------------------------------------------------ the reconsideration request

class DisputeError(ValueError):
    pass


def draft(letter_id: str) -> dict:
    db = get_db()
    letter = db.settlement_letters.find_one({"_id": letter_id})
    if not letter:
        raise DisputeError("settlement letter not found")
    a = analyse(letter)
    claim_items = [x for x in a["findings"] if x["verdict"] in ("dispute", "resubmit")]
    if not claim_items:
        raise DisputeError("nothing to dispute or resubmit on this claim")
    claim = db.claims.find_one({"_id": a["claim_id"]}) if a["claim_id"] else None
    hospital = (claim or {}).get("hospital") or "[[HOSPITAL NAME]]"
    subject = f"Request for reconsideration of deductions - Claim {a['claim_ref']} - {a['patient_name'] or ''}".strip(" -")
    out = [f"Dear {a['payer'] or 'Claims'} Team,", "",
           f"With reference to your settlement of claim {a['claim_ref']} for {a['patient_name'] or 'the patient'}"
           + (f" (UTR {a['utr']}, {a['payment_date']})" if a["utr"] else "") + f", in which {_inr(a['deducted'])} was deducted "
           f"from the claimed {_inr(a['claimed'] or 0)}, we request you to reconsider the following:", ""]
    total, docs = 0, []
    for i, x in enumerate(claim_items, 1):
        cut = "; ".join(f"\"{l['reason']}\" ({_inr(l['amount'])})" for l in x["letter_lines"])
        amt = f"{_inr(x['amount'])}" if x["amount"] is not None else "[[AMOUNT - run the discharge bill check]]"
        out.append(f"{i}. {x['category']}: {amt}")
        out.append(f"   Your letter: {cut}.")
        out.append(f"   {x['basis']}")
        out += [f"   {e}" for e in x["evidence"]]
        if x["verdict"] == "resubmit":
            out.append(f"   The document is enclosed: [[ATTACH: document asked for in \"{x['letter_lines'][0]['reason']}\"]].")
            docs.append(f"the document for item {i}")
        out.append("")
        total += x["amount"] or 0
    out += [f"We request you to release {_inr(total)}" + (" plus the amounts marked above" if a["unquantified"] else "")
            + " to the hospital's account.", "",
            "Enclosures: final itemised bill; your settlement letter" + "".join(f"; {d}" for d in docs) + ".", "",
            "Regards,", "TPA Desk", hospital, "[[AUTHORISED SIGNATORY]]"]
    body = "\n".join(out)
    # The safety gate may only see figures that are in the record: the letter, the bill and the computed findings.
    # (Numbers are joined with " | " because the gate strips commas and spaces before collecting digit runs.)
    nums = lambda xs: " | ".join(str(round(v)) for v in xs)  # noqa: E731
    _, bc = _bill_check_for(a["claim_ref"] or "")
    figures = {"letter": nums(v for v in letter["fields"].values() if isinstance(v, (int, float)) and not isinstance(v, bool)),
               "lines": nums(l["amount"] for x in a["findings"] for l in x["letter_lines"]),
               "findings": nums(x["amount"] for x in a["findings"] if x["amount"] is not None), "total": str(round(total)),
               "computed": " | ".join([e for x in a["findings"] for e in x["evidence"]] + [x["basis"] for x in a["findings"]]),
               "bill": nums([x["amount"] for x in bc["lines"]] + [bc["admissible"], bc["bill_total"]]) if bc else ""}
    safety = drafting.safety_check(subject, body, [], {"fields": figures, "documents": []})
    doc = {"claim_id": a["claim_id"] or f"letter:{letter_id}", "kind": "deduction_dispute", "engine": "rules",
           "subject": subject, "body": body, "attachments": [], "safety": safety, "requested": round(total),
           "letter_id": letter_id, "created_at": now(), "sent": False}
    db.drafts.insert_one(doc)
    db.disputes.update_one({"_id": letter_id}, {"$set": {"status": "drafted", "draft_id": str(doc["_id"]), "requested": round(total),
                                                         "analysis": a, "drafted_at": now()}}, upsert=True)
    return clean(doc)


def mark_sent(letter_id: str) -> None:
    get_db().disputes.update_one({"_id": letter_id}, {"$set": {"status": "sent", "sent_at": now()}})


def outcome(letter_id: str, status: str, recovered: float | None = None) -> dict:
    """The TPA answered: 'recovered' (with the amount it agreed to pay back) or 'rejected'."""
    if status not in ("recovered", "rejected"):
        raise DisputeError("status must be recovered or rejected")
    db = get_db()
    d = db.disputes.find_one({"_id": letter_id})
    if not d or d.get("status") not in ("drafted", "sent"):
        raise DisputeError("draft and send the reconsideration request first")
    amt = float(recovered or 0) if status == "recovered" else 0.0
    if status == "recovered" and not 0 < amt <= (d.get("requested") or 0) + 1:
        raise DisputeError(f"recovered amount must be between 1 and the {d.get('requested'):,} requested")
    db.disputes.update_one({"_id": letter_id}, {"$set": {"status": status, "recovered": round(amt), "closed_at": now()}})
    return clean(db.disputes.find_one({"_id": letter_id}))
