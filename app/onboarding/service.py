"""Onboarding job orchestration: files in -> detected, mapped, validated, scored, messaged."""
from __future__ import annotations

import csv
import io

import pandas as pd
from pymongo import ReturnDocument

from ..alerts import raise_alert
from ..db import clean, get_db, now
from . import mapping, readiness, validate
from .schemas import ENTITIES


def read_table(data: bytes, filename: str) -> pd.DataFrame:
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        df = pd.read_excel(io.BytesIO(data), dtype=object)
    else:
        text = data.decode("utf-8-sig", errors="replace")
        df = pd.read_csv(io.StringIO(text), dtype=str, sep=None, engine="python")
    df = df.dropna(how="all").dropna(axis=1, how="all")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def process_file(data: bytes, filename: str, entity: str | None = None, mapping_override: list[dict] | None = None) -> dict:
    df = read_table(data, filename)
    detected, conf = mapping.detect_entity(df, filename)
    entity = entity or detected
    if mapping_override:
        cols, engine = mapping_override, "human-confirmed"
    else:
        cols, engine = mapping.map_columns(df, entity)
    raw = validate.apply_mapping(df, cols)
    result = validate.validate(entity, raw, cols)
    return {"filename": filename, "entity": entity, "label": ENTITIES[entity]["label"], "detected_entity": detected,
            "detection_confidence": conf, "mapping": cols, "mapping_engine": engine, "validation": result}


def run_job(files: list[tuple[bytes, str]], profile: dict) -> dict:
    db = get_db()
    processed = [process_file(d, n) for d, n in files]
    ready = readiness.assess(profile, processed)
    message = readiness.draft_fix_request(profile, ready, processed)
    seq = db.counters.find_one_and_update({"_id": "onboarding"}, {"$inc": {"seq": 1}}, upsert=True,
                                          return_document=ReturnDocument.AFTER)["seq"]
    job = {"_id": f"ONB-{seq:04d}", "hospital": profile.get("hospital_name"), "profile": profile, "files": processed,
           "readiness": ready, "message": message, "created_at": now()}
    db.onboarding_jobs.insert_one(job)
    if ready["verdict"] in ("at_risk", "will_miss"):
        raise_alert("onboarding", "critical" if ready["verdict"] == "will_miss" else "warning",
                    f"Go-live {ready['verdict'].replace('_', ' ')}: {profile.get('hospital_name')}",
                    f"{ready['blockers']} blockers, ETA {ready['eta_hours']}h vs {ready['hours_to_go_live']}h left.",
                    job_id=job["_id"], dedupe_key=f"onb:{job['_id']}")
    return clean(job)


def issues_csv(job: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["file", "row", "field", "value", "severity", "problem", "how to fix"])
    for f in job["files"]:
        for i in f["validation"]["issues"]:
            w.writerow([f["filename"], i["row"] or "", i["field"], i["value"] or "", i["severity"], i["message"], i["fix"]])
    return buf.getvalue()


def clean_csv(job: dict, entity: str) -> str | None:
    f = next((x for x in job["files"] if x["entity"] == entity), None)
    if not f:
        return None
    blocked = {i["row"] for i in f["validation"]["issues"] if i["severity"] == "blocker" and i["row"]}
    rows = [r for r in f["validation"]["clean_rows"] if r["_row"] not in blocked]
    cols = list(ENTITIES[entity]["fields"])
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()
