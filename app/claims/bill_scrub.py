"""Discharge bill check: predict what the TPA will cut from the final bill *before* it is submitted.

Most cashless short-payments in India are not judgement calls. They follow from the policy terms and
IRDAI's standard lists, and can be computed in advance:
  * items on IRDAI's standard lists of non-payable items (gloves, admission kit, crepe bandage...);
  * room rent above the policy's limit, which also cuts the "associated" charges in the same ratio
    (proportionate deduction);
  * an agreed package rate, a disease sub-limit, co-pay, and the balance sum insured.

Knowing this before discharge lets the desk raise an enhancement in time, clean up lines a TPA will
disallow, and tell the family their share up front instead of at the billing counter.

Deliberately rules, not LLM: this is money, and every rupee must trace back to a line and a rule.
The keyword lists below follow IRDAI's standard lists (Lists I-IV of non-payable / subsumed items in the
health-insurance standardisation guidelines) and the common policy-wording rules on proportionate
deduction. They have not been checked against a hospital TPA desk yet (see docs/field_validation.md).
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..onboarding.schemas import is_blank, norm_date, norm_money, norm_text

# Same names as receivables.ledger.REASON_CATEGORIES, so a predicted deduction can be compared with the
# deduction the TPA actually makes once the claim settles.
R_NONPAY = "Non-payable items / consumables"
R_ROOM = "Room rent / proportionate"
R_PACKAGE = "Package / tariff rate"
R_POLICY = "Co-pay / policy limit"

LIST_LABEL = {
    1: "IRDAI List I: not payable",
    2: "IRDAI List II: part of room charges",
    3: "IRDAI List III: part of procedure charges",
    4: "IRDAI List IV: part of treatment cost",
}
LIST_FIX = {
    1: "Not payable under any standard policy. Collect it from the patient at discharge and tell the family now.",
    2: "The TPA treats this as part of the room charge and disallows it as a separate line. If your room tariff "
       "already includes it, remove the line; otherwise it is the patient's share.",
    3: "The TPA treats this as part of the surgery / procedure charge and disallows it as a separate line. If your "
       "procedure tariff already includes it, remove the line; otherwise it is the patient's share.",
    4: "The TPA treats this as part of the cost of treatment and disallows it as a separate line. If your tariff "
       "already includes it, remove the line; otherwise it is the patient's share.",
}

# Regex fragments, matched on whole words against a lower-cased, punctuation-free description.
NON_PAYABLE: dict[int, tuple[str, ...]] = {
    1: (r"baby food", r"baby (utilities|oil|soap|powder)", r"beauty services?", r"belts?", r"braces?", r"buds",
        r"cold pack", r"hot pack", r"carry bags?", r"e ?mail", r"internet charges?", r"attendant food", r"food charges?",
        r"guest (food|services?)", r"canteen", r"leggings?", r"laundry", r"mineral water", r"sanitary pads?",
        r"telephone( charges?)?", r"crepe bandages?", r"diapers?", r"eyelet collar", r"slings?", r"arm ?slings?",
        r"donor (blood )?grouping", r"service charges?", r"television|tv charges?", r"surcharges?", r"attendant charges?",
        r"extra diet", r"birth certificate", r"certificate charges?", r"medical certificate", r"courier( charges?)?",
        r"conveyance( charges?)?", r"medical records?( charges?)?", r"photo ?cop(y|ies)( charges?)?", r"xerox",
        r"mortuary( charges?)?", r"walking aids?", r"walkers?", r"spacer", r"spirometers?", r"nebuli[sz](er|ation) kit",
        r"steam inhaler", r"thermometers?", r"cervical collar", r"splints?", r"diabetic foot ?wear", r"knee braces?",
        r"(knee|shoulder) immobili[sz]er", r"lumbo ?sacral belt", r"(water|air) bed", r"abdominal binder",
        r"private nurs(e|es|ing)", r"special nursing", r"sugar free", r"toiletr(y|ies)",
        r"(body|face|cold|moisturi[sz]ing|fairness) (creams?|lotions?)", r"(talcum|body) powder", r"ecg electrodes?", r"gloves?", r"kidney tray", r"(oxygen|o2) mask", r"ounce glass", r"pelvic traction belt",
        r"pan can", r"trolley cover", r"urometer", r"urine jug", r"vasofix", r"(ot|surgical|procedure|delivery|dressing|consumables?) kit"),
    2: (r"baby charges?", r"hand ?wash", r"shoe covers?", r"(head|surgical|surgeons?) caps?", r"cradle( charges?)?", r"combs?",
        r"eau de cologne", r"room fresheners?", r"foot covers?", r"gowns?", r"slippers?", r"tissue papers?",
        r"tooth ?paste", r"tooth ?brush(es)?", r"bed ?pans?", r"face masks?", r"flexi masks?", r"hand holder",
        r"sputum cups?", r"disinfectant lotions?", r"luxury tax", r"hvac", r"house ?keeping( charges?)?",
        r"air condition(er|ing)( charges?)?", r"ac charges?", r"(im|iv|im iv) injection charges?", r"injection charges?",
        r"clean sheets?", r"(warmer )?blankets?", r"admission kit", r"diabetic chart( charges?)?",
        r"documentation charges?", r"administrative (charges?|expenses?)", r"admin charges?", r"discharge (procedure )?charges?",
        r"daily chart( charges?)?", r"(entrance|visitors?) pass", r"prescription on discharge", r"file (opening )?charges?",
        r"incidental( charges?| expenses?)?", r"misc(ellaneous)?( charges?| expenses?)?", r"sundr(y|ies)",
        r"identification band", r"id band", r"name tag", r"pulse ?oxi ?meter( charges?)?"),
    3: (r"hair removal cream", r"razors?", r"disposable razors?", r"eye pads?", r"eye shields?", r"camera covers?",
        r"(dvd|cd) charges?", r"gauze( soft| pieces?| swabs?)?", r"(ward and )?theatre booking( charges?)?",
        r"(arthroscopy|endoscopy) instruments?", r"microscope covers?", r"surgical blades?", r"harmonic scalpel",
        r"shaver", r"surgical drill( charges?)?", r"eye kit", r"eye drapes?", r"drapes?", r"x ?ray films?",
        r"boyles? apparatus( charges?)?", r"cotton( bandages?| rolls?| wool)?", r"surgical tape", r"micropore",
        r"aprons?", r"tourniquet", r"(ortho|gynae?c|ot) bundle"),
    4: (r"admission charges?", r"admission fees?", r"registration charges?", r"registration fees?", r"urine containers?",
        r"blood reservation( charges?)?", r"ante ?natal booking( charges?)?", r"bipap( machine)?( charges?)?",
        r"cpap( machine)?( charges?)?", r"capd( equipment)?", r"infusion pump( charges?)?", r"hydrogen peroxide", r"spirit",
        r"disinfectants?", r"nutrition planning( charges?)?", r"dietici?an( charges?)?", r"diet charges?", r"hiv kit",
        r"(antiseptic )?mouth ?wash", r"lozenges?", r"mouth paint", r"vaccination( charges?)?", r"alcohol swabs?",
        r"scrub solution", r"sterillium", r"glucometer( strips?)?", r"gluco ?strips?", r"urine bags?"),
}
# Things that contain a listed word but are payable (checked first).
NOT_LISTED = re.compile(r"\b(laryngeal mask|lma|hip brace|implant|stent|lens|iol)\b")
_LIST_RE = {n: re.compile(r"(?<![a-z0-9])(" + "|".join(p) + r")(?![a-z0-9])") for n, p in NON_PAYABLE.items()}

# Line categories, first match wins (so the more specific ones come first).
CATEGORY_RULES: list[tuple[str, re.Pattern]] = [(c, re.compile(p)) for c, p in [
    ("discount", r"\b(discount|concession|waiver|rebate)\b"),
    ("icu", r"\b(icu|iccu|ccu|hdu|nicu|picu|micu|sicu|itu)\b|intensive care|critical care"),
    ("implant", r"\b(implants?|stents?|iol|intra ?ocular lens|prosthes[ie]s|pacemaker|bone cement)\b"),
    ("consultation", r"\b(visits?|consult\w*)\b"),  # "Surgeon visit" is a doctor visit, not surgery
    ("surgery", r"\b(surgeons?|surgery|surgical charges?|ot|operation theat(re|er)|theatre|anaesthe\w*|anesthe\w*|"
                r"procedures?|procedure charges?|operation charges?|assistant surgeon|professional fees?|c ?arm)\b"),
    # Device abbreviations only count when no surgery word is present ("Surgeon fee - PFN" is a fee, "PFN nail" a device).
    ("implant", r"\b(pfn|dhs|nails?|plates?|screws?|mesh|lens)\b"),
    ("room", r"\b(room|rooms|bed charges?|ward|accommodation|bed)\b"),
    ("nursing", r"\bnursing\b"),
    ("consultation", r"\b(consult\w*|visits?|physician|rmo|doctors?|specialist|paediatrician|pediatrician|obstetrician)\b"),
    ("blood", r"\b(blood (bag|transfusion|components?)|prbc|ffp|platelet concentrate|sdp)\b"),
    ("investigation", r"\b(lab|laboratory|patholog\w*|radiolog\w*|x ?ray|usg|ultrasound|ultrasonograph\w*|sonograph\w*|"
                      r"ct|mri|ecg|echo\w*|cbc|lft|rft|kft|culture|biopsy|histopath\w*|investigations?|tests?|serum|"
                      r"urine routine|ha?emogram|blood sugar|hb|platelet count|doppler|angiograph\w*|crp|electrolytes|"
                      r"biometry|keratometry|a ?scan|grouping)\b"),
    ("pharmacy", r"\b(pharmacy|medicines?|drugs?|tab|tabs|tablets?|inj|injections?|syrup|cap|caps|capsules?|iv fluids?|"
                 r"infusion|ns|rl|dns|dextrose|antibiotics?|ointment|drops)\b"),
    ("consumables", r"\b(consumables?|disposables?|syringes?|cannula|catheter|sutures?|gloves?|gauze|dressings?|bandages?|"
                    r"drapes?|tubing|line set|needles?|electrodes?)\b"),
]]
CATEGORY_LABEL = {"room": "Room", "icu": "ICU", "nursing": "Nursing", "consultation": "Doctor visits", "surgery": "Surgery / OT",
                  "investigation": "Investigations", "pharmacy": "Pharmacy", "consumables": "Consumables", "implant": "Implant",
                  "blood": "Blood", "discount": "Discount", "other": "Other"}
# Charges that move with the room category, so they are cut in the same ratio when the room is above the limit.
# Pharmacy, consumables, implants and diagnostics are excluded from proportionate deduction, and ICU charges are
# capped by the ICU limit only.
ASSOCIATED = {"nursing", "consultation", "surgery", "other"}
OUTSIDE_PACKAGE = {"implant", "discount"}

TOTAL_ROW = re.compile(r"^(sub ?total|grand total|net payable|amount payable|amount in words|advance|deposit|balance due|"
                       r"total( amount| bill( amount)?| charges)?$|total [(:-])")


def _norm(s) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", str(s or "").lower().replace("'", ""))
    return re.sub(r"\s+", " ", s).strip()


def nonpayable_list(description: str) -> int | None:
    """Which IRDAI list a bill line falls under, or None when it is payable."""
    d = _norm(description)
    if not d or NOT_LISTED.search(d):
        return None
    for n, rx in _LIST_RE.items():
        if rx.search(d):
            return n
    return None


def classify(description: str, department: str | None = None) -> str:
    """The description decides; the department / section heading is the fallback (a 'ROOM CHARGES' heading
    often sits above the nursing line too)."""
    for text in (description, department):
        d = _norm(text)
        if d:
            for cat, rx in CATEGORY_RULES:
                if rx.search(d):
                    return cat
    return "other"


def _money(x: float) -> int | float:
    x = round(float(x) + 0.0, 2)
    return int(x) if x.is_integer() else x


def _limit(spec, sum_insured: float | None) -> tuple[float | None, str | None]:
    """'1%' -> 1% of sum insured per day; 3000 -> Rs 3,000 per day; blank -> no limit."""
    if is_blank(spec) or spec == 0:
        return None, None
    s = re.sub(r"(?i)\s*(/\s*day|per\s*day|a\s*day|of\s*si|of\s*sum\s*insured)\s*$", "", str(spec).strip())
    if s.endswith("%"):
        pct = float(s[:-1])
        if not sum_insured:
            return None, f"{s} of sum insured (sum insured not given, so not applied)"
        return sum_insured * pct / 100, f"{s} of the {_inr(sum_insured)} sum insured"
    v = norm_money(s)
    return (v, f"{_inr(v)} per day") if v else (None, None)


def _inr(x: float) -> str:
    """Indian digit grouping (12,34,567) for messages."""
    neg, x = x < 0, abs(round(x))
    s = str(int(x))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join(re.findall(r"\d{1,2}", head[::-1]))[::-1]
        s = f"{head},{tail}"
    return ("-" if neg else "") + "Rs " + s


# ------------------------------------------------------------------ parsing an HMS bill export

BILL_FIELDS: dict[str, tuple[str, ...]] = {
    "description": ("particulars", "description", "item description", "service name", "service description", "item name",
                    "charge description", "item", "service", "details", "test name", "name"),
    "category": ("department", "service group", "category", "bill head", "charge head", "head", "group", "dept",
                 "service type", "type"),
    "date": ("date", "service date", "bill date", "charge date", "txn date", "transaction date"),
    "qty": ("qty", "quantity", "units", "no of days", "days", "nos", "unit"),
    "rate": ("rate", "unit price", "unit rate", "rate per unit", "price", "tariff"),
    "amount": ("amount", "net amount", "total amount", "amount rs", "gross amount", "bill amount", "value", "total", "net"),
}


class BillError(ValueError):
    """A file the bill check cannot use at all (no description or amount column)."""


def parse_bill(blob: bytes, filename: str) -> dict:
    """An itemised final-bill export (CSV / XLSX) -> bill lines. Sub-total and total rows are skipped;
    a row with a description but no amount is read as a section heading ("PHARMACY") for the rows below."""
    from ..receivables.importer import map_columns, read_table

    df = read_table(blob, filename).dropna(how="all")
    m = map_columns(df.columns, BILL_FIELDS)
    if "description" not in m or "amount" not in m:
        raise BillError(f"Need an item / particulars column and an amount column. Found headers: {', '.join(map(str, df.columns))}")
    lines, skipped, section = [], [], None
    for i, row in enumerate(df.to_dict("records")):
        n = i + 2
        desc = norm_text(row[m["description"]])
        amount = norm_money(row[m["amount"]])
        dept = norm_text(row[m["category"]]) if "category" in m else None
        if not desc:
            if amount is not None:
                skipped.append({"row": n, "reason": "amount without a description"})
            continue
        if TOTAL_ROW.search(_norm(desc)):
            skipped.append({"row": n, "reason": f"total row ({desc})"})
            continue
        if amount is None:
            section = desc  # a heading such as "ROOM CHARGES" or "PHARMACY"
            continue
        d = norm_date(row[m["date"]]) if "date" in m else None
        lines.append({"row": n, "date": d.isoformat() if d else None, "description": desc,
                      "department": dept or section, "qty": norm_money(row[m["qty"]]) if "qty" in m else None,
                      "rate": norm_money(row[m["rate"]]) if "rate" in m else None, "amount": amount})
    if not lines:
        raise BillError("No bill lines with an amount were found.")
    return {"lines": lines, "skipped": skipped,
            "mapping": [{"source": str(c), "field": next((f for f, col in m.items() if col == str(c)), None)} for c in df.columns]}


# ------------------------------------------------------------------ the check

def scrub(lines: list[dict], policy: dict | None = None, context: dict | None = None) -> dict:
    """lines: [{description, amount, department?, qty?, rate?, date?}]
    policy: {sum_insured, balance_sum_insured, room_rent_limit ('1%' or Rs/day), icu_limit, copay_pct,
             sublimit_label, sublimit_amount, package_rate, consumables_rider, proportionate_clause}
    context: {patient_name, payer, hospital, los_days, approved_amount}
    Returns line-level payable / deduction with the rule behind each, claim-level adjustments, findings
    for the desk, and a plain-language note for the family."""
    p, ctx = dict(policy or {}), dict(context or {})
    si = norm_money(p.get("sum_insured"))
    rider = bool(p.get("consumables_rider"))
    proportionate_clause = p.get("proportionate_clause", True) is not False
    package = norm_money(p.get("package_rate"))
    los = norm_money(ctx.get("los_days"))
    assumptions: list[str] = []
    findings: list[dict] = []

    out = []
    for i, ln in enumerate(lines):
        amt = norm_money(ln.get("amount"))
        if amt is None:
            continue
        desc = norm_text(ln.get("description")) or ""
        cat = classify(desc, ln.get("department"))
        lst = None if cat in ("implant", "discount") else nonpayable_list(desc)
        out.append({"row": ln.get("row", i + 1), "date": ln.get("date"), "description": desc, "department": ln.get("department"),
                    "category": cat, "qty": norm_money(ln.get("qty")), "rate": norm_money(ln.get("rate")), "amount": amt,
                    "payable": amt, "deduction": 0.0, "reason": None, "rule": None, "irdai_list": lst})
    bill_total = sum(x["amount"] for x in out)

    def cut(x: dict, amount: float, reason: str, rule: str) -> None:
        amount = min(max(amount, 0.0), x["payable"])
        if amount <= 0:
            return
        x["payable"] -= amount
        x["deduction"] += amount
        x["reason"] = x["reason"] or reason
        x["rule"] = f"{x['rule']}, {rule}" if x["rule"] else rule

    # 1. IRDAI standard lists
    listed = [x for x in out if x["irdai_list"]]
    if rider and listed:
        assumptions.append("Consumables rider on the policy: items on IRDAI Lists I-IV are treated as payable.")
    elif listed:
        for x in listed:
            cut(x, x["amount"], R_NONPAY, LIST_LABEL[x["irdai_list"]])
        for n in sorted({x["irdai_list"] for x in listed}):
            xs = [x for x in listed if x["irdai_list"] == n]
            amt = sum(x["amount"] for x in xs)
            names = ", ".join(dict.fromkeys(x["description"] for x in xs))
            findings.append({"rule": f"irdai_list_{n}", "severity": "warning", "amount": _money(amt),
                             "message": f"{LIST_LABEL[n]}: {len(xs)} line(s), {_inr(amt)} ({names}).", "fix": LIST_FIX[n]})

    # 2. room rent limit + proportionate deduction (not applied to package claims: the package is the price)
    room_cap, room_basis = _limit(p.get("room_rent_limit"), si)
    room = [x for x in out if x["category"] == "room" and x["payable"] > 0]
    room_info = None
    if room_basis and not room_cap:
        assumptions.append(f"Room-rent limit {room_basis}.")
    if room_cap and room and not package:
        eligible = actual = 0.0
        for x in room:
            days = x["qty"] if x["qty"] and x["qty"] > 0 else None
            if days is None:
                days = los or 1
                assumptions.append(f"'{x['description']}' has no day count; assumed {days:g} day(s)"
                                   + (" from the expected length of stay." if los else "."))
            rate = x["rate"] if x["rate"] and x["rate"] > 0 else x["amount"] / days
            ok = min(rate, room_cap) * days
            eligible += min(ok, x["amount"])
            actual += x["amount"]
            x["per_day"], x["days"] = _money(rate), days
            cut(x, x["amount"] - ok, R_ROOM, f"room rent above {_inr(room_cap)}/day")
        ratio = eligible / actual if actual else 1.0
        assoc = [x for x in out if x["category"] in ASSOCIATED and x["payable"] > 0]
        prop_amt = 0.0
        if ratio < 1 and proportionate_clause:
            for x in assoc:
                before = x["payable"]
                cut(x, before * (1 - ratio), R_ROOM, f"proportionate deduction ({(1 - ratio) * 100:.1f}%)")
                prop_amt += before - x["payable"]
        excess = actual - eligible
        room_info = {"limit_per_day": _money(room_cap), "basis": room_basis, "actual_room": _money(actual),
                     "eligible_room": _money(eligible), "ratio": round(ratio, 4), "room_excess": _money(excess),
                     "proportionate": _money(prop_amt), "associated_base": _money(sum(x["amount"] for x in assoc)),
                     "proportionate_clause": proportionate_clause}
        if excess > 0:
            per_day = max(x["per_day"] for x in room)
            msg = (f"Room at {_inr(per_day)}/day is above the policy limit of {_inr(room_cap)}/day ({room_basis}). "
                   f"Room-rent excess {_inr(excess)}")
            if proportionate_clause and prop_amt:
                msg += (f", plus a proportionate deduction of {(1 - ratio) * 100:.1f}% on {_inr(room_info['associated_base'])} "
                        f"of linked charges (nursing, doctor visits, surgeon / OT fees): {_inr(prop_amt)}")
            findings.append({"rule": "room_rent", "severity": "warning", "amount": _money(excess + prop_amt),
                             "message": msg + ".",
                             "fix": f"A room at or under {_inr(room_cap)}/day would have avoided {_inr(excess + prop_amt)}. "
                                    "Pharmacy, consumables, implants and investigations are not cut. Keep the family's "
                                    "written consent for the higher room and tell them this amount before discharge."})
    elif not room_cap and any(x["category"] == "room" for x in out) and not package:
        assumptions.append("No room-rent limit given, so no room-rent or proportionate deduction is applied.")

    # 3. ICU limit (ICU charges are capped on their own; no proportionate deduction)
    icu_cap, icu_basis = _limit(p.get("icu_limit"), si)
    icu = [x for x in out if x["category"] == "icu" and x["payable"] > 0
           and not re.search(r"nursing|visit|consult", x["description"].lower())]
    if icu_cap and icu and not package:
        over = 0.0
        for x in icu:
            days = x["qty"] if x["qty"] and x["qty"] > 0 else (los or 1)
            rate = x["rate"] if x["rate"] and x["rate"] > 0 else x["amount"] / days
            before = x["payable"]
            cut(x, x["amount"] - min(rate, icu_cap) * days, R_ROOM, f"ICU charge above {_inr(icu_cap)}/day")
            over += before - x["payable"]
        if over > 0:
            findings.append({"rule": "icu_limit", "severity": "warning", "amount": _money(over),
                             "message": f"ICU charges are above the policy's ICU limit of {_inr(icu_cap)}/day ({icu_basis}): {_inr(over)} will be cut.",
                             "fix": "Tell the family this amount before discharge."})

    # 4. agreed package rate: everything inside the package is paid at the package price
    if package:
        inside = [x for x in out if x["category"] not in OUTSIDE_PACKAGE and x["payable"] > 0]
        in_total = sum(x["payable"] for x in inside)
        if in_total > package:
            share = (in_total - package) / in_total
            for x in inside:
                cut(x, x["payable"] * share, R_PACKAGE, "above the agreed package rate")
            findings.append({"rule": "package", "severity": "warning", "amount": _money(in_total - package),
                             "message": f"Charges inside the package come to {_inr(in_total)} against the agreed package rate of "
                                        f"{_inr(package)}: {_inr(in_total - package)} will be cut.",
                             "fix": "Bill at the package rate. Anything outside the package (extra days, a complication) needs its "
                                    "own justification and an enhancement request."})
        assumptions.append(f"Package claim at {_inr(package)}: implants are billed outside the package; room-rent "
                           "proportionate deduction does not apply.")

    # 5. claim-level: sub-limit, co-pay, balance sum insured
    admissible = max(sum(x["payable"] for x in out), 0.0)
    adjustments: list[dict] = []
    running = admissible
    sub_amt = norm_money(p.get("sublimit_amount"))
    sub_label = norm_text(p.get("sublimit_label")) or "Sub-limit"
    if sub_amt and running > sub_amt:
        adjustments.append({"rule": "sublimit", "reason": R_POLICY, "label": f"{sub_label} capped at {_inr(sub_amt)}",
                            "amount": _money(running - sub_amt)})
        findings.append({"rule": "sublimit", "severity": "warning", "amount": _money(running - sub_amt),
                         "message": f"{sub_label}: the policy pays at most {_inr(sub_amt)}; admissible charges are {_inr(running)}.",
                         "fix": "Tell the family the difference is theirs to pay. Check the e-card / policy schedule for the exact sub-limit."})
        running = sub_amt
    copay = norm_money(p.get("copay_pct"))
    if copay:
        c = running * copay / 100
        adjustments.append({"rule": "copay", "reason": R_POLICY, "label": f"Co-pay {copay:g}%", "amount": _money(c)})
        findings.append({"rule": "copay", "severity": "info", "amount": _money(c),
                         "message": f"Co-pay of {copay:g}% on the admissible amount: {_inr(c)} is the patient's share.",
                         "fix": "Collect the co-pay at discharge; it is not a deduction the desk can fix."})
        running -= c
    balance = norm_money(p.get("balance_sum_insured")) or si
    if balance and running > balance:
        adjustments.append({"rule": "sum_insured", "reason": R_POLICY, "label": f"Balance sum insured {_inr(balance)}",
                            "amount": _money(running - balance)})
        findings.append({"rule": "sum_insured", "severity": "warning", "amount": _money(running - balance),
                         "message": f"Only {_inr(balance)} of the sum insured is left; {_inr(running - balance)} is above it.",
                         "fix": "Check for a top-up or a second policy before discharge; otherwise it is the patient's share."})
        running = balance
    expected = max(running, 0.0)

    # 6. desk findings that cost time rather than money
    approved = norm_money(ctx.get("approved_amount"))
    if approved and expected > approved + 1:
        findings.append({"rule": "enhancement", "severity": "blocker", "amount": _money(expected - approved),
                         "message": f"Expected payable {_inr(expected)} is above the pre-auth approval of {_inr(approved)}.",
                         "fix": f"Raise an enhancement request for {_inr(expected - approved)} with the final bill before discharge, "
                                "or the difference becomes a dispute after the patient has left."})
    pharm = [x for x in out if x["category"] == "pharmacy"]
    if len(pharm) == 1 and pharm[0]["amount"] > 5000:
        findings.append({"rule": "pharmacy_breakup", "severity": "warning", "amount": 0,
                         "message": f"Pharmacy is billed as one line ({_inr(pharm[0]['amount'])}).",
                         "fix": "Attach the item-wise pharmacy bill; TPAs raise a query on lump-sum pharmacy charges."})
    seen = defaultdict(list)
    for x in (x for x in out if x["date"]):  # undated repeats (daily visits) are normal
        seen[(_norm(x["description"]), x["date"], x["amount"])].append(x["row"])
    dups = [rows for rows in seen.values() if len(rows) > 1]
    if dups:
        findings.append({"rule": "duplicates", "severity": "warning", "amount": 0,
                         "message": f"{len(dups)} possible duplicate line(s): same item, date and amount (rows "
                                    + "; ".join(", ".join(map(str, r)) for r in dups) + ").",
                         "fix": "Confirm each was really given twice; TPAs cut duplicates as a matter of course."})
    room_days = sum(x["qty"] or 0 for x in out if x["category"] in ("room", "icu"))
    if los and room_days > los + 1:
        findings.append({"rule": "room_days", "severity": "warning", "amount": 0,
                         "message": f"{room_days:g} room/ICU days billed against an expected stay of {los:g} days.",
                         "fix": "Justify the extra days in the discharge summary, or correct the bill."})
    other = [x for x in out if x["category"] == "other" and not x["irdai_list"]]
    if other:
        assumptions.append(f"{len(other)} line(s) not recognised ({', '.join(x['description'] for x in other[:4])}"
                           f"{'...' if len(other) > 4 else ''}); treated as other medical charges.")

    by_reason: dict[str, dict] = {}
    for x in out:
        if x["deduction"] > 0.005:
            r = by_reason.setdefault(x["reason"], {"category": x["reason"], "amount": 0.0, "count": 0})
            r["amount"] += x["deduction"]
            r["count"] += 1
    for a in adjustments:
        r = by_reason.setdefault(a["reason"], {"category": a["reason"], "amount": 0.0, "count": 0})
        r["amount"] += a["amount"]
        r["count"] += 1
    reasons = sorted(({**r, "amount": _money(r["amount"])} for r in by_reason.values()), key=lambda r: -r["amount"])

    for x in out:
        x["payable"], x["deduction"] = _money(x["payable"]), _money(x["deduction"])
    sev = {"blocker": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (sev[f["severity"]], -f["amount"]))
    result = {
        "engine": "rules", "bill_total": _money(bill_total), "admissible": _money(admissible),
        "expected_payable": _money(expected), "patient_share": _money(bill_total - expected),
        "predicted_deduction": _money(bill_total - expected),
        "deduction_pct": round(100 * (bill_total - expected) / bill_total, 1) if bill_total else 0,
        "by_reason": reasons, "adjustments": adjustments, "room_rent": room_info, "findings": findings,
        "by_category": [{"category": c, "label": CATEGORY_LABEL[c], "amount": _money(sum(x["amount"] for x in out if x["category"] == c))}
                        for c in CATEGORY_LABEL if any(x["category"] == c for x in out)],
        "lines": out, "assumptions": list(dict.fromkeys(assumptions)), "policy": p, "context": ctx,
    }
    result["patient_note"] = patient_note(result)
    return result


def patient_note(r: dict) -> str:
    """Plain-language estimate for the family, given before discharge. Amounts only from the check above."""
    ctx = r.get("context") or {}
    who = ctx.get("patient_name") or "the patient"
    payer = ctx.get("payer") or "the insurer / TPA"
    lines = [f"Dear family of {who},", "",
             f"Estimated final bill: {_inr(r['bill_total'])}",
             f"Expected to be paid by {payer} (cashless): {_inr(r['expected_payable'])}",
             f"Estimated amount payable by you at discharge: {_inr(r['patient_share'])}"]
    if r["patient_share"] > 0:
        lines += ["", "Why the insurance will not cover all of it:"]
        what = {R_NONPAY: "Items the policy does not pay for (IRDAI standard list: gloves, kits, bandages and similar)",
                R_ROOM: "Room above the policy's room-rent limit, which also reduces linked charges in the same ratio",
                R_PACKAGE: "Charges above the agreed package rate",
                R_POLICY: "Policy terms (co-pay, sub-limit or sum insured)"}
        for rr in r["by_reason"]:
            lines.append(f"- {what.get(rr['category'], rr['category'])}: {_inr(rr['amount'])}")
    lines += ["", "This is an estimate. The insurer / TPA decides the final amount, and we will tell you at once if it changes.",
              f"Billing desk{', ' + ctx['hospital'] if ctx.get('hospital') else ''}"]
    return "\n".join(lines)
