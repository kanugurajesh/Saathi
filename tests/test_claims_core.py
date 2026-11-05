from datetime import date, datetime, timedelta, timezone

from app.claims import completeness, drafting
from app.claims.extraction import extract_preauth, grounding_report, heuristic_extract, parse_date, parse_inr
from app.claims.status import Status, normalize, sla_state
from app.config import ROOT

DOCS = ROOT / "samples" / "documents"


# ------------------------------------------------------------------ status
def test_portal_vocabularies_map_to_one_lifecycle():
    assert normalize("nimbus", "Query Raised") == (Status.QUERY_RAISED, "exact")
    assert normalize("orbit", "PA-DEF") == (Status.QUERY_RAISED, "exact")
    assert normalize("lotus", "  Information   Required ") == (Status.QUERY_RAISED, "exact")
    assert normalize("lotus", "Authorised (Partial)")[0] == Status.PARTIALLY_APPROVED


def test_unseen_status_falls_back_to_keywords_then_unknown():
    assert normalize("nimbus", "Claim Repudiated")[0] == Status.DENIED
    assert normalize("nimbus", "Payment done - UTR 1234")[1] == "keyword"
    assert normalize("orbit", "PA-HOLD") == (Status.UNKNOWN, "unknown")


def test_irdai_clock():
    t0 = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    ok = sla_state("PREAUTH_SUBMITTED", t0, t0 + timedelta(minutes=10))
    risk = sla_state("PREAUTH_SUBMITTED", t0, t0 + timedelta(minutes=50))
    late = sla_state("DISCHARGE_SUBMITTED", t0, t0 + timedelta(minutes=200))
    assert ok["level"] == "ok" and risk["level"] == "at_risk"
    assert late["breached"] and late["owner"] == "insurer/TPA" and "3 hours" in late["basis"]
    assert sla_state("QUERY_RAISED", t0, t0)["owner"] == "hospital"
    assert sla_state("SETTLED", t0, t0) is None


# ------------------------------------------------------------------ extraction
def test_parsers():
    assert parse_inr("Rs. 85,000/-") == 85000
    assert parse_inr("Approx cost: 1.65 lakh") == 165000
    assert parse_inr("Rs 2,50,000") == 250000
    assert parse_date("04/11/2026") == "2026-11-04"  # day-first
    assert parse_date("4 Nov 2026, 06:10 hrs") == "2026-11-04"


def test_structured_form_extracts_every_field_exactly():
    r = extract_preauth((DOCS / "01_preauth_form_cholecystitis.txt").read_bytes(), "01.txt")
    f = r["fields"]
    assert f["policy_number"] == "DSG/HL/2026/0045121"
    assert f["admission_date"] == "2026-11-03" and f["estimated_cost"] == 85000
    assert f["age"] == 52 and f["gender"] == "Male" and f["patient_phone"] == "9848011223"
    assert f["alcohol_history"] is None  # absent in doc -> must stay empty
    assert r["needs_human_review"] == [] and r["doc_type"] == "preauth_form"


def test_two_column_layout():
    f = heuristic_extract((DOCS / "02_admission_note_stemi.txt").read_text(encoding="utf-8-sig"))
    assert f["uhid"] == "GDH-IP-26-0930" and f["member_id"] == "LTS-99310-01" and f["tpa"] == "Lotus Health TPA"
    assert f["policy_number"] == "DSG/HL/2026/0099310"


def test_grounding_catches_hallucinated_values():
    src = "Patient Name: Ramesh Goud\nPolicy No: DSG/HL/2026/0045121\nEstimate: Rs 85,000"
    rep = grounding_report({"patient_name": "Ramesh Goud", "policy_number": "DSG/HL/2026/0099999",
                            "estimated_cost": 85000, "alcohol_history": None}, src)
    assert rep["patient_name"] is True and rep["estimated_cost"] is True
    assert rep["policy_number"] is False  # invented digits are caught
    assert rep["alcohol_history"] is None


# ------------------------------------------------------------------ completeness
BASE = {"patient_name": "X", "admission_date": date.today().isoformat(), "treating_doctor": "Dr Y",
        "policy_number": "P1", "insurer": "I", "estimated_cost": 1000, "icd10_code": "K85.9",
        "duration_of_ailment": "2 days", "past_history": "Nil", "doctor_registration_no": "R1"}
ALL_DOCS = ["preauth_form", "id_proof", "policy_card", "admission_note"]


def rules(res):
    return {x["rule"] for x in res["blockers"] + res["warnings"]}


def test_clean_claim_is_ready():
    r = completeness.check({**BASE, "provisional_diagnosis": "Dengue fever"}, ALL_DOCS)
    assert r["ready_to_submit"] and r["query_risk"] == "low"


def test_alcohol_sensitive_diagnosis_requires_history():
    r = completeness.check({**BASE, "provisional_diagnosis": "Acute pancreatitis"}, ALL_DOCS)
    assert "missing_alcohol_history" in {b["rule"] for b in r["blockers"]}
    assert any("alcohol" in q for q in r["predicted_tpa_queries"])


def test_rta_needs_mlc_and_implants_need_invoice_at_discharge():
    rta = completeness.check({**BASE, "provisional_diagnosis": "Head injury", "alcohol_history": "Nil", "is_rta": True}, ALL_DOCS)
    assert "rta_without_mlc" in rules(rta)
    ortho = {**BASE, "provisional_diagnosis": "Fracture", "alcohol_history": "Nil", "proposed_procedure": "ORIF with plating"}
    assert "implant_cost_breakup" in rules(completeness.check(ortho, ALL_DOCS))
    assert "implant_docs" in rules(completeness.check(ortho, ["discharge_summary", "final_bill", "investigation_report"], "discharge"))


def test_room_rent_capping_warning():
    r = completeness.check({**BASE, "provisional_diagnosis": "Dengue", "room_type": "Single private"}, ALL_DOCS, sum_insured=200000)
    assert "room_rent_capping" in rules(r)


# ------------------------------------------------------------------ drafting
def test_split_query_handles_numbered_inline_and_bullets():
    assert len(drafting.split_query("please provide the following:\n1. A report\n2. B history\n3. C\nRegards,\nTPA")) == 3
    assert drafting.split_query("Please submit: i) MLC copy / FIR ii) Alcohol history iii) CT brain report") == [
        "MLC copy / FIR", "Alcohol history", "CT brain report"]
    assert len(drafting.split_query("Info required:\n- ECG report\n- Past history\n- Stent cost\nLotus TPA - Pre-auth cell")) == 3


CTX = {"fields": {"patient_name": "Ramesh Goud", "policy_number": "P/1", "duration_of_ailment": "Pain x 3 days",
                  "admission_date": "2026-11-03", "provisional_diagnosis": "Cholecystitis",
                  "past_history": "HTN x 4 yrs", "alcohol_history": None},
       "documents": ["investigation_report"], "hospital": "Demo Hospital"}


def test_query_reply_answers_from_record_and_flags_unknowns():
    q = "Please provide the following:\n1. Duration of ailment\n2. USG abdomen report\n3. Alcohol history, if any"
    d = drafting.draft_query_reply(q, CTX, "NB-1")
    res = {a["ask"]: a["resolution"] for a in d["asks"]}
    assert res == {"Duration of ailment": "answer", "USG abdomen report": "attach", "Alcohol history, if any": "needs_doctor_input"}
    assert "Pain x 3 days" in d["body"] and d["attachments"] == ["investigation_report"]
    assert d["safety"]["status"] == "needs_review" and len(d["safety"]["placeholders"]) == 1
    assert "nil" not in d["body"].lower() and "no history" not in d["body"].lower()


def test_safety_gate_blocks_fabrication():
    sc = drafting.safety_check
    assert sc("s", "Patient denies alcohol.", [], CTX)["status"] == "blocked"
    assert sc("s", "Estimate is Rs 99,000.", [], CTX)["status"] == "blocked"
    assert sc("s", "Enclosed MLC.", ["mlc_fir"], CTX)["status"] == "blocked"
    assert sc("s", "All good, see attached.", ["investigation_report"], CTX)["status"] == "ready_to_send"
