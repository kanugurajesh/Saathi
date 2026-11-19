"""Deduction recovery: TPA cuts vs the bill check and IRDAI rules, verdicts, the drafted request, outcomes."""
import json

import pytest
from fastapi.testclient import TestClient

from app.claims import bill_scrub, drafting, service
from app.config import ROOT
from app.receivables import disputes as dp
from app.receivables import reconcile as rc
from app.receivables import settlement

LSCS = "NB26104902:UTIBN52026110404902"
PFN = "OR202655120:HDFCN52026110455120"


@pytest.fixture
def loaded(fresh_db):
    from scripts.seed import seed

    seed()
    for p in json.loads((ROOT / "samples" / "bills" / "presets.json").read_text())["bills"]:
        c = fresh_db.claims.find_one({"portal_ref": p["portal_ref"]})
        lines = bill_scrub.parse_bill((ROOT / "samples" / "bills" / p["file"]).read_bytes(), p["file"])["lines"]
        service.bill_check(lines, p["policy"], p["context"], claim_id=c["_id"])
    for d in ("settlements", "disputes"):
        for p in sorted((ROOT / "samples" / d).glob("*.txt")):
            rc.add_letter(settlement.read_letter(p.read_bytes(), p.name))
    return fresh_db


def _claim(v, ref):
    return next(c for c in v["claims"] if c["claim_ref"] == ref)


def test_reason_categories_and_named_items():
    assert dp.category("Room rent proportionate deduction 33.33% on total bill") == "Room rent / proportionate"
    assert dp.category("Co-pay 20% on claimed amount") == "Co-pay / policy limit"
    assert dp.category("Physiotherapy not payable") == "Non-payable items / consumables"
    assert dp.category("Newborn birth record not submitted") == "Documents missing"
    assert dp._items("Non-payable items as per IRDAI list (gloves, IV cannula, suture material)") == ["gloves", "IV cannula", "suture material"]
    assert dp._items("Attendant food, medical records and admission charges not payable") == ["Attendant food", "medical records", "admission charges"]
    assert dp._items("Non-payable consumables as per IRDAI list") == []  # generic: nothing named


def test_payable_items_cut_as_non_payable_and_copay_on_gross(loaded):
    c = _claim(dp.overview(), "OR-2026-55120")
    d = {f["category"]: f for f in c["findings"] if f["verdict"] == "dispute"}
    assert d["Non-payable items / consumables"]["amount"] == 440 + 260 + 1040 + 1500  # cannula, catheter, sutures, physio
    assert any("IV Cannula 18G" in e for e in d["Non-payable items / consumables"]["evidence"])
    assert d["Co-pay / policy limit"]["amount"] == 36754 - 34168  # 20% of claimed vs 20% of admissible
    assert c["totals"]["dispute"] == c["deducted"] - c["predicted"] == 5826
    assert c["totals"]["valid"] == c["predicted"]


def test_proportionate_cut_on_exempt_charges_and_missing_document(loaded):
    c = _claim(dp.overview(), "NB-26-104902")
    room = next(f for f in c["findings"] if f["verdict"] == "dispute")
    assert room["amount"] == 3150 and "exempt" in room["basis"]  # 1/3 of pharmacy + consumables + investigations (9,450)
    assert c["totals"]["resubmit"] == 1000 and c["totals"]["valid"] == 19367 + 6690


def test_letters_without_a_bill_check_raise_no_false_disputes(loaded):
    v = dp.overview()
    for ref in ("OR-2026-53310", "OR-2026-50877", "NB-26-101240", "NB-26-098812"):
        c = _claim(v, ref)
        assert c["totals"]["dispute"] == 0 and c["status"] == "nothing_to_claim", ref
    assert _claim(v, "OR-2026-50877")["totals"]["valid"] == 23000  # 10% co-pay, no other deduction: on admissible
    assert v["accuracy"]["claims"] == 2 and v["accuracy"]["actual"] - v["accuracy"]["predicted"] == 3150 + 1000 + 5826


def test_letter_only_rules():
    f = {"claimed_amount": 100000, "deduction_amount": 25000,
         "deductions": [{"reason": "Non-payable items (IV cannula, gloves)", "amount": 5000},
                        {"reason": "Co-pay 20% as per policy", "amount": 20000}]}
    a = dp.analyse({"_id": "x", "filename": "x.txt", "fields": f})
    by = {(x["category"], x["verdict"]): x for x in a["findings"]}
    assert by[("Co-pay / policy limit", "dispute")]["amount"] == 20000 - 19000  # 20% of (1,00,000 - 5,000)
    assert by[("Non-payable items / consumables", "dispute")]["amount"] is None and a["unquantified"] == 1


def test_draft_quotes_only_record_figures_and_flows_to_outcome(loaded):
    from app.main import app

    d = dp.draft(PFN)
    assert d["safety"]["status"] == "needs_review" and not d["safety"]["issues"]  # only the signatory placeholder
    assert "Rs 5,826" in d["body"] and "IV Cannula 18G" in d["body"] and "Rs 1,83,770" in d["body"]
    lscs = dp.draft(LSCS)
    assert "[[ATTACH:" in lscs["body"] and not lscs["safety"]["issues"]

    client = TestClient(app)
    assert client.post(f"/api/drafts/{d['_id']}/mark-sent", json={}).status_code == 409  # placeholder still there
    body = d["body"].replace("[[AUTHORISED SIGNATORY]]", "K. Ramesh, Billing Manager")
    assert client.post(f"/api/drafts/{d['_id']}/mark-sent", json={"body": body}).status_code == 200
    assert _claim(dp.overview(), "OR-2026-55120")["status"] == "sent"
    assert client.post(f"/api/disputes/{PFN}/outcome", json={"status": "recovered", "recovered": 99999}).status_code == 409
    v = client.post(f"/api/disputes/{PFN}/outcome", json={"status": "recovered", "recovered": 5826}).json()
    assert v["totals"]["recovered"] == 5826 and _claim(v, "OR-2026-55120")["status"] == "recovered"


def test_sample_endpoint_runs_bill_checks_first(fresh_db):
    from app.main import app

    from scripts.seed import seed

    seed()
    v = TestClient(app).post("/api/disputes/sample").json()
    assert v["totals"]["dispute"] == 5826 + 3150 and v["totals"]["resubmit"] == 1000


def test_safety_gate_amount_followed_by_a_comma():
    ctx = {"fields": {"total": "5000"}, "documents": []}
    assert not drafting.safety_check("s", "Item Rs 440, row 3. Total Rs 5,000, as agreed.", [], ctx)["issues"]
    assert drafting.safety_check("s", "Total Rs 7,000, as agreed.", [], ctx)["status"] == "blocked"
