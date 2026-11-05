import json
from datetime import date

import pandas as pd

from app.config import ROOT
from app.onboarding import mapping, service
from app.onboarding.schemas import gstin_checksum, gstin_valid, norm_date, norm_gst, norm_money, norm_phone, norm_role

SAMPLES = ROOT / "samples" / "onboarding"


def test_normalisers_handle_indian_formats():
    assert norm_date("03/27", expiry=True) == date(2027, 3, 31)
    assert norm_date("Mar-28", expiry=True) == date(2028, 3, 31)
    assert norm_date("04/11/2026") == date(2026, 11, 4)
    assert norm_date(46000) == date(2025, 12, 9)  # Excel serial
    assert norm_date("abc") is None
    assert norm_phone("+91 98480 22110") == "9848022110" and norm_phone("09848044332") == "9848044332"
    assert norm_phone("12345") is None
    assert norm_money("₹ 1,22,500/-") == 122500 and norm_gst("0.05") == 5 and norm_gst("12%") == 12
    assert norm_role("Consultant Surgeon") == "doctor" and norm_role("Staff Nurse (GNM)") == "nurse"
    assert norm_role("Ward Boy") is None


def test_gstin_checksum():
    assert gstin_valid("27AAPFU0939F1ZV") == (True, "ok")
    assert gstin_valid("27AAPFU0939F1ZX")[0] is False
    g = "36AAECG4821K1Z"
    assert gstin_valid(g + gstin_checksum(g))[0]


def test_messy_headers_map_with_value_sniffing():
    df = pd.DataFrame({"Item": ["Dolo 650"], "Exp.": ["03/27"], "Tax %": ["5%"], "M.R.P": [33], "Bal Qty": [5]})
    ent, _ = mapping.detect_entity(df, "export.csv")
    assert ent == "drug_master"
    m = {x["source"]: x["target"] for x in mapping.heuristic_mapping(df, ent)}
    assert m == {"Item": "drug_name", "Exp.": "expiry_date", "Tax %": "gst_rate", "M.R.P": "mrp", "Bal Qty": "stock_qty"}


def test_sample_job_finds_every_planted_problem():
    files = [(p.read_bytes(), p.name) for p in SAMPLES.iterdir() if p.suffix in (".csv", ".xlsx")]
    job = service.run_job(files, json.loads((SAMPLES / "hospital_profile.json").read_text(encoding="utf-8")))
    codes = {f["entity"]: {i["code"] for i in f["validation"]["issues"]} for f in job["files"]}
    assert {"expired_stock", "rate_above_mrp", "negative_stock", "duplicate_batch", "expiry_unreadable", "mrp_invalid",
            "gst_legacy_slab", "near_expiry", "hsn_missing", "possible_duplicate"} <= codes["drug_master"]
    assert {"doctor_reg_missing", "phone_duplicate", "phone_invalid", "email_invalid", "role_unknown"} <= codes["staff"]
    assert {"price_invalid", "duplicate_name"} <= codes["lab_tests"]
    assert {"price_invalid", "cash_tpa_mismatch"} <= codes["tariff"]
    r = job["readiness"]
    assert r["verdict"] in ("at_risk", "will_miss", "no_go") and r["blockers"] >= 4
    failing = {c["id"] for c in r["checks"] if c["status"] == "fail"}
    assert "wa_prescription" in failing and "gstin" not in failing
    assert "MUST" in job["message"]["body"]
    csv_text = service.issues_csv(job)
    assert "pharmacy_stock_marg_export.xlsx" in csv_text and "how to fix" in csv_text
    clean = service.clean_csv(job, "drug_master")
    assert "Ringer Lactate" not in clean.split("\n", 2)[1]  # blocked rows excluded
