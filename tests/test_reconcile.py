"""Payment matching: settlement letters, bank statements, UTR / amount matching, ledger updates, safety holds."""
import pytest
from fastapi.testclient import TestClient

from app.config import ROOT
from app.receivables import reconcile as rc
from app.receivables import settlement

S = ROOT / "samples" / "settlements"


def _letter(name):
    return settlement.read_letter((S / name).read_bytes(), name)


# ------------------------------------------------------------------ letters
def test_three_letter_layouts_read_exactly():
    a = _letter("nimbus_NB-26-101240.txt")["fields"]
    assert (a["claim_ref"], a["approved_amount"], a["tds_amount"], a["net_paid"], a["utr"], a["payment_date"]) == \
        ("NB-26-101240", 86500, 8650, 77850, "UTIBN52026110541873", "2026-11-01")
    assert a["payer"].startswith("NIMBUS TPA") and [d["amount"] for d in a["deductions"]] == [6500, 1500, 1500]
    b = _letter("orbit_OR-2026-50877.txt")  # pipe table, 02-Nov-2026
    assert b["fields"]["net_paid"] == 186300 and b["fields"]["payment_date"] == "2026-11-02" and not b["issues"]
    assert b["fields"]["deductions"] == [{"reason": "Co-pay 10% as per policy terms", "amount": 23000.0}]
    c = _letter("lotus_LT-87102.txt")  # "Label: Rs. 1,12,000" email
    assert c["fields"]["approved_amount"] == 112000 and c["fields"]["payer"].startswith("Lotus Health TPA")


def test_crlf_and_lf_letters_read_the_same():
    raw = (S / "nimbus_NB-26-101240.txt").read_bytes().replace(b"\r\n", b"\n")
    lf = settlement.read_letter(raw, "lf.txt")["fields"]
    crlf = settlement.read_letter(raw.replace(b"\n", b"\r\n"), "crlf.txt")["fields"]
    assert lf == crlf and crlf["payer"].startswith("NIMBUS TPA")


def test_a_letter_that_does_not_add_up_is_held():
    r = _letter("lotus_LT-87102.txt")
    assert r["needs_review"] and "100,800" in r["issues"][0] and "108,800" in r["issues"][0]


def test_a_value_not_in_the_letter_is_flagged():
    text = "Claim No : NB-1\nApproved Amount : 50,000\nTDS : 5,000\nNet Amount Paid : 45,000\nUTR No : ABCD1234567890\n"
    llm_output = {"claim_ref": "NB-1", "approved_amount": 50000, "tds_amount": 5000, "net_paid": 45000, "utr": "ABCD9999999999"}
    g = settlement.grounding(llm_output, text)
    assert g["utr"] is False and g["net_paid"] is True and g["claim_ref"] is True


def test_net_paid_derived_when_missing():
    f, issues = settlement.check({"claim_ref": "X", "approved_amount": 10000, "tds_amount": 1000, "utr": "U1234567890"})
    assert f["net_paid"] == 9000 and f["net_derived"] and not issues


# ------------------------------------------------------------------ bank statement
def test_statement_skips_account_details_and_keeps_only_credits():
    st = rc.parse_statement((S / "bank_statement_current_account.csv").read_bytes(), "stmt.csv")
    assert (st["period_from"], st["period_to"], st["rows"]) == ("2026-10-28", "2026-11-03", 12)
    assert sorted(c["amount"] for c in st["credits"]) == [12500, 14200, 25000, 48000, 100800, 186300, 193500, 262000]


def test_statement_with_amount_and_dr_cr_columns():
    csv = b"Tran Date;Description;Amount;Dr/Cr\n05-11-2026;NEFT ORBIT INSURANCE UTR HDFCN1234567890;50000;CR\n05-11-2026;Rent;9000;DR\n"
    st = rc.parse_statement(csv, "s.csv")
    assert [c["amount"] for c in st["credits"]] == [50000]


def test_unusable_statement_rejected():
    with pytest.raises(rc.StatementError):
        rc.parse_statement(b"Name,Phone\nA,1\n", "x.csv")


# ------------------------------------------------------------------ end to end on the seeded ledger
@pytest.fixture
def seeded(fresh_db):
    from scripts.seed import seed

    seed()
    for p in sorted(S.glob("*.txt")):
        rc.add_letter(settlement.read_letter(p.read_bytes(), p.name))
    rc.add_statement((S / "bank_statement_current_account.csv").read_bytes(), "stmt.csv")
    return fresh_db


def test_utr_matches_split_a_lump_sum_and_update_the_ledger(seeded):
    v = rc.reconcile()
    applied = {m["claim_ref"]: m for a in v["applied_now"] for m in a["matches"]}
    assert set(applied) == {"NB-26-098812", "NB-26-101240", "OR-2026-50877"}  # 1,93,500 = two Nimbus claims
    mahesh = seeded.receivables.find_one({"_id": "private_tpa:NB-26-101240"})
    assert mahesh["status"] == "PAID" and mahesh["paid"] == 77850 and mahesh["tds"] == 8650 and mahesh["outstanding"] == 0
    assert mahesh["deduction"] == 9500 and "Attendant charges" in mahesh["remarks"]  # was 8,000 before the letter
    assert seeded.receivable_events.find_one({"claim_ref": "NB-26-101240", "kind": "payment", "amount": 77850 + 8650})
    assert seeded.receivable_events.find_one({"claim_ref": "NB-26-101240", "kind": "deduction", "amount": 1500})


def test_everything_else_waits_for_a_person(seeded):
    v = rc.reconcile()
    kinds = {r["kind"]: r for r in v["review"]}
    assert set(kinds) == {"letter_review", "suggested"}
    assert kinds["letter_review"]["credit"]["amount"] == 100800  # Lotus: letter says 1,08,800
    assert kinds["suggested"]["options"][0]["receivable_ids"] == ["aarogyasri:AS/2026/041104", "aarogyasri:AS/2026/041108"]
    assert seeded.receivables.find_one({"_id": "private_tpa:LT-87102"})["status"] == "APPROVED"  # untouched
    assert [m["fields"]["claim_ref"] for m in v["missing"]] == ["OR-2026-53310"]
    assert [u["credit"]["amount"] for u in v["unexplained"]] == [14200]
    assert sorted(c["amount"] for c in v["others"]) == [12500, 25000, 48000]  # patient UPI + cash: not payer money
    alerts = {a["kind"] for a in seeded.alerts.find({"resolved": False})}
    assert {"payment_missing", "unexplained_credit"} <= alerts


def test_confirm_and_approve_then_idempotent(seeded):
    v = rc.reconcile()
    sug = next(r for r in v["review"] if r["kind"] == "suggested")
    with pytest.raises(rc.MatchError):
        rc.confirm(sug["credit"]["_id"], ["aarogyasri:AS/2026/041104"], "gross")  # 62,000 is not 2,62,000
    rc.confirm(sug["credit"]["_id"], sug["options"][0]["receivable_ids"], "gross")
    assert seeded.receivables.find_one({"_id": "aarogyasri:AS/2026/041108"})["status"] == "PAID"

    rc.approve_letter("LT87102:SBINN52026110633221", {"net_paid": 100800})
    v = rc.reconcile()
    assert not v["review"] and v["totals"]["matched_count"] == 4
    assert seeded.receivables.find_one({"_id": "private_tpa:LT-87102"})["paid"] == 100800

    # re-uploading the same statement and letters changes nothing
    before = seeded.receivable_events.count_documents({})
    rc.add_statement((S / "bank_statement_current_account.csv").read_bytes(), "stmt.csv")
    for p in sorted(S.glob("*.txt")):
        rc.add_letter(settlement.read_letter(p.read_bytes(), p.name))
    v2 = rc.reconcile()
    assert v2["totals"] == v["totals"] and seeded.receivable_events.count_documents({}) == before


def test_api_sample_flow(fresh_db):
    from app.main import app

    from scripts.seed import seed

    seed()
    c = TestClient(app)
    v = c.post("/api/reconcile/sample").json()
    assert v["totals"]["matched_count"] == 2 and v["totals"]["missing_count"] == 1
    lid = next(r for r in v["review"] if r["kind"] == "letter_review")["letters"][0]["_id"]
    r = c.post(f"/api/reconcile/letters/{lid}/approve", json={"corrections": {"net_paid": 100800}})
    assert r.status_code == 200 and r.json()["totals"]["matched_count"] == 3
    assert c.post("/api/reconcile/statement", files={"file": ("x.csv", b"a,b\n1,2\n")}).status_code == 422
    assert c.post("/api/reconcile/confirm", json={"credit_id": "nope", "receivable_ids": ["x"]}).status_code == 409
