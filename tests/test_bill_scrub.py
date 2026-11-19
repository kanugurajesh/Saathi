"""Discharge bill check: IRDAI lists, room-rent proportionate deduction, policy limits, parsing, claim integration."""
import json

import pytest
from fastapi.testclient import TestClient

from app.claims import bill_scrub as b
from app.claims import service
from app.config import ROOT
from app.receivables import ledger

BILLS = ROOT / "samples" / "bills"


def L(desc, amount, **kw):
    return {"description": desc, "amount": amount, **kw}


# ------------------------------------------------------------------ IRDAI lists + classification
@pytest.mark.parametrize("desc,lst", [
    ("Surgical Gloves (pairs)", 1), ("Crepe Bandage 6 inch", 1), ("Knee Immobilizer", 1), ("Attendant Food", 1),
    ("Walker", 1), ("OT kit", 1), ("Admission Kit", 2), ("Miscellaneous charges", 2), ("Surgeon's cap", 2),
    ("Gauze and Cotton", 3), ("X-Ray Films", 3), ("Ortho Bundle (disposable)", 3), ("Eye Kit (disposable)", 3),
    ("Registration Charges", 4), ("Diet Charges", 4), ("Glucometer Strips", 4), ("Urine bag", 4),
])
def test_listed_items(desc, lst):
    assert b.nonpayable_list(desc) == lst


@pytest.mark.parametrize("desc", ["Inj. Ceftriaxone 1 g", "Silver sulfadiazine cream 1%", "CBC", "Laryngeal mask airway",
                                  "IV Cannula 20G", "Sutures (Vicryl 1-0)", "Nursing Charges", "Foldable IOL", "Physiotherapy"])
def test_payable_items_are_not_listed(desc):
    assert b.nonpayable_list(desc) is None


def test_classification_prefers_the_description_over_the_section_heading():
    assert b.classify("Nursing Charges", "ROOM CHARGES") == "nursing"
    assert b.classify("Dolo 650", "PHARMACY") == "pharmacy"
    assert b.classify("Orthopaedic Surgeon Visit") == "consultation"
    assert b.classify("Surgeon Fee - PFN") == "surgery"
    assert b.classify("ICU Bed Charges") == "icu"
    assert b.classify("PFN Implant - Titanium nail") == "implant"


# ------------------------------------------------------------------ money rules
def test_room_rent_above_limit_cuts_linked_charges_in_proportion_but_not_pharmacy_or_tests():
    lines = [L("Single Room Rent", 18000, qty=4, rate=4500), L("Nursing Charges", 3000), L("Surgeon Fee", 18000),
             L("Inj. Cefuroxime", 2000), L("CBC", 500), L("Sutures", 1000), L("Mesh implant", 9000)]
    r = b.scrub(lines, {"sum_insured": 300000, "room_rent_limit": "1%"})
    pay = {x["description"]: x["payable"] for x in r["lines"]}
    assert pay["Single Room Rent"] == 12000                      # 4 days x Rs 3,000 limit
    assert pay["Nursing Charges"] == 2000 and pay["Surgeon Fee"] == 12000  # x 3000/4500
    assert pay["Inj. Cefuroxime"] == 2000 and pay["CBC"] == 500 and pay["Sutures"] == 1000 and pay["Mesh implant"] == 9000
    assert r["room_rent"]["ratio"] == pytest.approx(2 / 3, abs=1e-4)
    assert r["expected_payable"] == 12000 + 2000 + 12000 + 2000 + 500 + 1000 + 9000
    assert r["by_reason"][0]["category"] == b.R_ROOM and r["by_reason"][0]["amount"] == 6000 + 1000 + 6000
    assert any(f["rule"] == "room_rent" for f in r["findings"])


def test_room_limit_as_rupees_per_day_and_no_proportionate_clause():
    lines = [L("Deluxe room", 10000, qty=2), L("Doctor visit", 2000)]
    r = b.scrub(lines, {"room_rent_limit": "3000/day", "proportionate_clause": False})
    assert r["expected_payable"] == 6000 + 2000


def test_room_within_limit_is_untouched():
    r = b.scrub([L("General ward", 6000, qty=3), L("Nursing", 1500)], {"sum_insured": 500000, "room_rent_limit": "1%"})
    assert r["expected_payable"] == 7500 and r["patient_share"] == 0 and not r["findings"]


def test_consumables_rider_pays_listed_items():
    lines = [L("Gloves", 600), L("Admission kit", 400), L("Surgeon fee", 10000)]
    assert b.scrub(lines, {})["expected_payable"] == 10000
    assert b.scrub(lines, {"consumables_rider": True})["expected_payable"] == 11000


def test_sublimit_then_copay_then_sum_insured():
    lines = [L("Surgeon fee", 50000), L("OT charges", 10000)]
    r = b.scrub(lines, {"sublimit_label": "Cataract", "sublimit_amount": 40000, "copay_pct": 10})
    assert [a["rule"] for a in r["adjustments"]] == ["sublimit", "copay"]
    assert r["expected_payable"] == 36000  # 40,000 cap, then 10% co-pay
    r = b.scrub(lines, {"sum_insured": 500000, "balance_sum_insured": 25000})
    assert r["expected_payable"] == 25000 and r["adjustments"][0]["rule"] == "sum_insured"


def test_package_rate_caps_charges_inside_the_package_but_not_the_implant():
    lines = [L("Surgeon fee", 60000), L("Room rent", 20000, qty=4), L("PFN implant", 40000)]
    r = b.scrub(lines, {"package_rate": 70000, "sum_insured": 300000, "room_rent_limit": "1%"})
    assert r["expected_payable"] == 70000 + 40000
    assert r["room_rent"] is None  # no proportionate deduction on a package
    assert r["by_reason"][0]["category"] == b.R_PACKAGE


def test_enhancement_flagged_when_expected_payable_exceeds_preauth():
    r = b.scrub([L("Surgeon fee", 50000)], {}, {"approved_amount": 40000})
    f = r["findings"][0]
    assert f["rule"] == "enhancement" and f["severity"] == "blocker" and f["amount"] == 10000


def test_line_and_total_amounts_reconcile():
    for p in json.loads((BILLS / "presets.json").read_text())["bills"]:
        parsed = b.parse_bill((BILLS / p["file"]).read_bytes(), p["file"])
        r = b.scrub(parsed["lines"], p["policy"], p["context"])
        assert r["bill_total"] == pytest.approx(sum(x["amount"] for x in r["lines"]))
        assert r["admissible"] == pytest.approx(sum(x["payable"] for x in r["lines"]), abs=0.05)
        assert r["patient_share"] == pytest.approx(sum(x["amount"] for x in r["by_reason"]), abs=0.05)
        assert r["patient_note"].count("Rs") >= 3


# ------------------------------------------------------------------ parsing HMS exports
def test_section_headings_and_total_rows():
    parsed = b.parse_bill((BILLS / "lscs_single_room.csv").read_bytes(), "lscs_single_room.csv")
    assert sum(x["amount"] for x in parsed["lines"]) == 74240  # = the bill's own Grand Total row, which is skipped
    nursing = next(x for x in parsed["lines"] if x["description"] == "Nursing Charges")
    assert nursing["department"] == "ROOM CHARGES" and nursing["qty"] == 4


def test_subtotals_skipped_and_amounts_with_commas():
    pfn = b.parse_bill((BILLS / "pfn_hip_fracture.csv").read_bytes(), "pfn.csv")
    assert sum(x["amount"] for x in pfn["lines"]) == 183770 and len(pfn["skipped"]) == 2
    cat = b.parse_bill((BILLS / "cataract_daycare.csv").read_bytes(), "cataract.csv")
    assert sum(x["amount"] for x in cat["lines"]) == 55450


def test_total_knee_replacement_is_not_a_total_row():
    csv = b"Particulars,Amount\nTotal Knee Replacement - surgeon fee,60000\nTotal,60000\n"
    assert [x["description"] for x in b.parse_bill(csv, "tkr.csv")["lines"]] == ["Total Knee Replacement - surgeon fee"]


def test_unusable_file_is_rejected():
    with pytest.raises(b.BillError):
        b.parse_bill(b"Name,Phone\nA,1\n", "staff.csv")


def test_duplicates_and_lump_sum_pharmacy_are_flagged():
    r = b.scrub(b.parse_bill((BILLS / "pfn_hip_fracture.csv").read_bytes(), "pfn.csv")["lines"], {"copay_pct": 20})
    rules = {f["rule"] for f in r["findings"]}
    assert {"duplicates", "pharmacy_breakup", "copay", "irdai_list_1"} <= rules


# ------------------------------------------------------------------ claim integration
def _claim():
    return service.create_claim(
        fields={"patient_name": "Lakshmi Devi", "expected_los_days": 4, "insurer": "Godavari General Insurance"},
        documents=[], portal="nimbus", portal_ref="NB-TEST-1", hospital="Test Hospital", tpa_name="Nimbus TPA",
        sum_insured=300000)


def test_saving_on_a_claim_uses_its_terms_and_alerts_the_desk(fresh_db):
    c = _claim()
    fresh_db.claims.update_one({"_id": c["_id"]}, {"$set": {"approved_amount": 45000}})
    lines = b.parse_bill((BILLS / "lscs_single_room.csv").read_bytes(), "lscs.csv")["lines"]
    r = service.bill_check(lines, {"room_rent_limit": "1%"}, None, claim_id=c["_id"])
    assert r["policy"]["sum_insured"] == 300000 and r["context"]["patient_name"] == "Lakshmi Devi"
    assert r["context"]["los_days"] == 4 and "Lakshmi Devi" in r["patient_note"]
    saved = fresh_db.claims.find_one({"_id": c["_id"]})
    assert saved["bill_check"]["expected_payable"] == r["expected_payable"] and saved["policy"]["room_rent_limit"] == "1%"
    kinds = {a["kind"] for a in fresh_db.alerts.find({"resolved": False})}
    assert {"enhancement_needed", "patient_share"} <= kinds
    assert fresh_db.claim_events.find_one({"claim_id": c["_id"], "kind": "bill_checked"})

    # a re-check replaces the previous alerts instead of piling up
    service.bill_check(lines, {"room_rent_limit": "1%", "consumables_rider": True}, None, claim_id=c["_id"])
    assert fresh_db.alerts.count_documents({"kind": "patient_share", "resolved": False}) == 1


def test_receivable_carries_bill_total_and_predicted_deduction(fresh_db):
    c = _claim()
    lines = b.parse_bill((BILLS / "lscs_single_room.csv").read_bytes(), "lscs.csv")["lines"]
    r = service.bill_check(lines, {"room_rent_limit": "1%"}, None, claim_id=c["_id"])
    fresh_db.claims.update_one({"_id": c["_id"]}, {"$set": {"stage": "discharge", "status": "FINAL_APPROVED", "approved_amount": 45000}})
    ledger.sync_from_claims()
    rec = fresh_db.receivables.find_one({"claim_id": c["_id"]})
    assert rec["billed"] == 74240 and rec["predicted_deduction"] == r["predicted_deduction"]
    assert rec["deduction"] == 74240 - 45000


def test_api_upload_and_sample(fresh_db):
    from app.main import app

    client = TestClient(app)
    c = _claim()
    up = client.post("/api/bill-check/upload", files={"file": ("cataract.csv", (BILLS / "cataract_daycare.csv").read_bytes())},
                     data={"policy": json.dumps({"sum_insured": 300000, "sublimit_amount": 40000}),
                           "context": json.dumps({"approved_amount": 35000}), "claim_id": c["_id"]})
    assert up.status_code == 200, up.text
    assert up.json()["expected_payable"] == 40000 and up.json()["patient_share"] == 15450
    latest = client.get(f"/api/claims/{c['_id']}/bill-check").json()
    assert latest["expected_payable"] == 40000 and latest["parse"]["skipped"]

    preview = client.post("/api/bill-check/sample", json={"file": "pfn_hip_fracture.csv"})
    assert preview.status_code == 200 and preview.json()["claim_id"] is None
    assert client.post("/api/bill-check/upload", files={"file": ("x.csv", b"a,b\n1,2\n")}).status_code == 422
    assert client.post("/api/bill-check", json={"lines": [L("Gloves", 100)], "claim_id": "HX-NOPE"}).status_code == 404
