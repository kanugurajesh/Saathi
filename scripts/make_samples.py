"""Generate messy-but-realistic legacy onboarding files (the kind hospitals actually send).
Every planted problem is intentional and listed in the comments, so tests can assert on them."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone

from openpyxl import Workbook

from app.config import ROOT
from app.onboarding.schemas import gstin_checksum

OUT = ROOT / "samples" / "onboarding"


def pharmacy():
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock"
    ws.append(["Item Name", "Mfr", "HSN", "GST%", "Batch", "Exp", "MRP (Rs)", "P.Rate", "Bal Qty", "Sch"])
    rows = [
        ["Paracetamol 500mg Tab", "Micro Labs", "30049099", "5%", "PCM2401", "03/28", "₹ 22.50", 14.2, 400, "H"],
        ["PARACETAMOL 500 MG TABLET", "Micro Labs", "30049099", 5, "PCM2402", "Mar-28", 22.5, 14.2, 120, "H"],   # fuzzy duplicate
        ["Pantoprazole 40mg Tab", "Alkem", "30049039", "12%", "PN0931", "08/27", 145, 92, 300, "H"],            # legacy 12% slab
        ["Amoxicillin + Clav 625 Tab", "Cipla", "30041030", "5%", "AC1102", "08/26", 210, 150, 60, "H"],          # EXPIRED
        ["Ceftriaxone 1g Inj", "Lupin", "30042019", 0.05, "CF7781", "12/26", 68, 41, 200, "H"],                  # near expiry, 0.05 GST
        ["Ondansetron 4mg Inj", "Zydus", "30049099", "5", "ON5512", "31/12/2027", 22, 30, 150, "H"],              # rate > MRP
        ["Metformin 500mg Tab", "USV", "", "5%", "MF2201", "06/28", 35, 21, 500, ""],                              # missing HSN
        ["Atorvastatin 10mg Tab", "Sun Pharma", "30049079", "5%", "AT1010", datetime(2028, 1, 31), 98, 61, 250, "H"],
        ["Ringer Lactate 500ml", "Baxter", "30049099", "5%", "RL5001", "11/27", 45, 28, -12, ""],                  # negative stock
        ["Ringer Lactate 500ml", "Baxter", "30049099", "5%", "RL5001", "11/27", 45, 28, 40, ""],                   # duplicate batch
        ["Insulin Glargine 100IU", "Biocon", "30043110", "5%", "IG3300", "09/27", 730, 540, 25, "H"],
        ["Azithromycin 500mg Tab", "Alembic", "30042099", "5%", "AZ5050", "02/28", 119, 76, 180, "H1"],
        ["Diclofenac 75mg Inj", "Troikaa", "300490", "18", "DC7575", "07/28", 18, 9.5, 300, "H"],                 # 18% (valid slab, unusual)
        ["Surgical Gloves 7.5", "Kanam", "40151900", "5%", "SG7501", "05/29", 25, 15, 1000, ""],
        ["Normal Saline 100ml", "Baxter", "30049099", "5%", "NS1001", "abc", 30, 18, 400, ""],                     # unreadable expiry
        ["Cefixime 200mg Tab", "Macleods", "30042019", "5%", "CX2002", "04/28", "", 60, 90, "H"],                 # missing MRP
        ["Dolo 650", "Micro Labs", "30049099", "5%", "DL6501", "10/28", 33.6, 21, 600, "H"],
        ["Rabeprazole 20mg Tab", "Intas", "30049039", "28%", "RB2020", "01/28", 160, 98, 140, "H"],               # legacy 28%
    ]
    for r in rows:
        ws.append(r)
    wb.save(OUT / "pharmacy_stock_marg_export.xlsx")


def staff():
    rows = [
        ["S.No", "Name", "Designation", "Dept", "Mobile No", "Mail ID", "Reg. No"],
        [1, "Dr. K. Srinivas", "Consultant Surgeon", "General Surgery", "+91 98480 22110", "srinivas.k@example.in", "TSMC/FMR/04512"],
        [2, "Dr. P. Madhavi", "Consultant Gynaecologist", "Obstetrics & Gynaecology", "9848033221", "madhavi@example.in", "TSMC/FMR/07781"],
        [3, "Dr. R. Fareed", "Physician", "General Medicine", "09848044332", "fareed@example", ""],                 # doctor no reg, bad email
        [4, "Sister Mary Joseph", "Staff Nurse (GNM)", "Nursing", "9848055443", "", ""],
        [5, "Ravi Kumar", "Front Office", "Reception", "9848066554", "ravi.fo@example.in", ""],
        [6, "Swathi", "B.Pharm - Pharmacist", "Pharmacy", "9848066554", "", ""],                                      # duplicate phone
        [7, "Naresh", "Lab Technician (DMLT)", "Laboratory", "98480-77665", "", ""],
        [8, "Shiva", "Ward Boy", "Housekeeping", "12345", "", ""],                                                    # unknown role + bad phone
        [9, "Dr. A. Raghavendra", "Neurosurgeon", "Neurosurgery", "9848088776", "raghu@example.in", "TSMC/FMR/03317"],
        [10, "Lavanya", "Billing Executive", "Accounts", "9848099887", "billing@example.in", ""],
    ]
    with open(OUT / "staff_list.csv", "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)


def lab():
    rows = [["Test Description", "Section", "Specimen", "Charges", "TAT"],
            ["Complete Blood Picture (CBP)", "Haematology", "EDTA Blood", "Rs. 350/-", "4"],
            ["Liver Function Test", "Biochemistry", "Serum", "650", "6"],
            ["Renal Function Test", "Biochemistry", "Serum", "600", "6"],
            ["Dengue NS1 Antigen", "Serology", "Serum", "900", "4"],
            ["Serum Amylase", "Biochemistry", "Serum", "", "6"],                    # missing price
            ["HbA1c", "Biochemistry", "EDTA Blood", "550", "24"],
            ["Urine Routine", "Clinical Pathology", "Urine", "150", "2"],
            ["Liver Function Test", "Biochemistry", "Serum", "650", "6"],          # duplicate
            ["Thyroid Profile (T3 T4 TSH)", "Immunoassay", "Serum", "700", "24"],
            ["Widal Test", "Serology", "Serum", "250", "4"]]
    with open(OUT / "lab_rate_list.csv", "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)


def tariff():
    wb = Workbook()
    ws = wb.active
    ws.append(["Particulars", "Speciality", "Head", "Cash Rate", "TPA Rate"])
    for r in [["General Ward (per day)", "", "Room", 1500, 1800], ["Semi-Private Room (per day)", "", "Room", 2500, 3000],
              ["Single Private Room (per day)", "", "Room", 4000, 4500], ["ICU (per day)", "", "Room", 6000, 7000],
              ["OP Consultation - Specialist", "All", "Consultation", 500, 600], ["Laparoscopic Cholecystectomy (package)", "General Surgery", "Package", 65000, 75000],
              ["LSCS (package)", "OBG", "Package", 45000, 52000], ["Normal Delivery (package)", "OBG", "Package", 25000, 30000],
              ["Phaco with foldable IOL (package)", "Ophthalmology", "Package", 28000, 32000], ["ECG", "Cardiology", "Investigation", 300, 350],
              ["2D Echo", "Cardiology", "Investigation", 18000, 2000],                       # extra zero on cash rate
              ["Nebulisation", "", "Procedure", 150, 200], ["Dressing - small", "", "Procedure", "", 300]]:  # missing price
        ws.append(r)
    wb.save(OUT / "hospital_tariff.xlsx")


def profile():
    first14 = "36AAECG4821K1Z"
    ist = timezone(timedelta(hours=5, minutes=30))
    go_live = (datetime.now(ist) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    data = {"hospital_name": "Godavari Demo Multispeciality Hospital, Nalgonda", "gstin": first14 + gstin_checksum(first14),
            "hfr_id": "IN3610000456", "modules": ["opd", "ipd", "pharmacy", "lab", "insurance"],
            "departments": ["General Medicine", "General Surgery", "Obstetrics & Gynaecology", "Pharmacy", "Laboratory",
                            "Reception", "Nursing", "Accounts"],
            "letterhead_uploaded": True, "printer_tested": False,
            "whatsapp_templates": {"appointment_reminder": "approved", "lab_report": "pending", "bill": "approved",
                                   "prescription": "rejected"},
            "tpa_empanelments": ["Nimbus TPA", "Orbit Insurance", "Lotus Health TPA"], "go_live_at": go_live.isoformat()}
    (OUT / "hospital_profile.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    pharmacy(), staff(), lab(), tariff(), profile()
    print("samples written to", OUT)
