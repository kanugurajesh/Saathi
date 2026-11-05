"""Detect what a legacy spreadsheet is, and map its columns to the canonical schema.

Hospitals send exports from Marg, tally sheets, old HMS dumps, hand-made Excel. Headers like
"Exp", "P.Rate", "Mfr", "Reg No." are the norm. Heuristic matching (synonyms + fuzzy +
value sniffing) handles most of it; Gemini handles the weird remainder (it sees headers
*and* sample values). Low-confidence mappings are surfaced, never silently applied.
"""
from __future__ import annotations

import json
import re

import pandas as pd
from pydantic import BaseModel
from rapidfuzz import fuzz

from .. import llm
from .schemas import ENTITIES, norm_date, norm_gst, norm_phone

MIN_CONFIDENCE = 0.6


def _clean_header(h) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_\-]+", " ", str(h))).strip().lower()


def _header_score(header: str, field: str, spec: dict) -> float:
    h = _clean_header(header)
    cands = [field.replace("_", " ")] + spec["syn"]
    best = 0.0
    for c in cands:
        if h == c:
            return 100.0
        best = max(best, fuzz.ratio(h, c) * 0.95, fuzz.token_set_ratio(h, c) * 0.9 if len(h) > 3 else 0)
    return best


def _value_profile(series: pd.Series) -> dict:
    vals = [v for v in series.dropna().tolist()[:40] if str(v).strip()]
    if not vals:
        return {}
    n = len(vals)
    strs = [str(v) for v in vals]
    return {
        "phone": sum(norm_phone(v) is not None for v in vals) / n,
        "email": sum("@" in s for s in strs) / n,
        "gst": sum((g := norm_gst(v)) is not None and g in (0, 5, 12, 18, 28, 40) for v in vals) / n,
        "date": sum(norm_date(v, expiry=True) is not None and not re.fullmatch(r"\d{1,6}(\.0)?", s) for v, s in zip(vals, strs)) / n,
        "reg": sum(bool(re.search(r"(?i)(tsmc|apmc|mci|nmc|kmc|mmc|reg)|\b\d{4,6}/\d{2,4}\b", s)) for s in strs) / n,
        "hsn": sum(bool(re.fullmatch(r"\d{4}(\d{2}){0,2}(\.0)?", s)) for s in strs) / n,
    }


VALUE_HINTS = {"phone": "phone", "email": "email", "gst_rate": "gst", "expiry_date": "date", "dob": "date",
               "registration_number": "reg", "hsn_code": "hsn"}


def detect_entity(df: pd.DataFrame, filename: str) -> tuple[str, float]:
    scores = {}
    for ent, spec in ENTITIES.items():
        total, req_hit = 0.0, 0
        for field, fspec in spec["fields"].items():
            best = max((_header_score(h, field, fspec) for h in df.columns), default=0)
            if best >= 80:
                total += 1
                req_hit += fspec.get("required", False)
        req = sum(f.get("required", False) for f in spec["fields"].values())
        s = 0.6 * (total / len(spec["fields"])) + 0.4 * (req_hit / req)
        if any(k in filename.lower() for k in spec["filename_hints"]):
            s += 0.25
        scores[ent] = s
    ent = max(scores, key=scores.get)
    return ent, round(min(scores[ent], 1.0), 2)


def heuristic_mapping(df: pd.DataFrame, entity: str) -> list[dict]:
    fields = ENTITIES[entity]["fields"]
    cands = []
    for col in df.columns:
        prof = _value_profile(df[col])
        for field, spec in fields.items():
            s = _header_score(col, field, spec)
            hint = VALUE_HINTS.get(field)
            if hint and prof.get(hint, 0) >= 0.7:
                s = max(s, 70) + 15 * prof[hint]  # values strongly look like this field
            elif hint and prof and prof.get(hint, 0) < 0.2 and s < 100:
                s -= 15  # header says so, values disagree
            cands.append((s, col, field))
    cands.sort(reverse=True)
    used_cols, used_fields, out = set(), set(), {}
    for s, col, field in cands:
        if col in used_cols or field in used_fields or s < 55:
            continue
        used_cols.add(col)
        used_fields.add(field)
        out[col] = {"source": str(col), "target": field, "confidence": round(min(s, 100) / 100, 2), "reason": "header/value match"}
    return [out.get(c, {"source": str(c), "target": None, "confidence": 0.0, "reason": "no confident match"}) for c in df.columns]


class ColumnMap(BaseModel):
    source: str
    target: str | None
    confidence: float
    reason: str


class MappingResult(BaseModel):
    columns: list[ColumnMap]


def map_columns(df: pd.DataFrame, entity: str) -> tuple[list[dict], str]:
    fields = ENTITIES[entity]["fields"]

    def via_llm():
        sample = df.head(6).astype(str).to_dict(orient="records")
        prompt = (f"Map legacy hospital spreadsheet columns to the target schema '{entity}'.\n"
                  f"TARGET FIELDS: {json.dumps({k: v['syn'][:4] for k, v in fields.items()})}\n"
                  f"SOURCE COLUMNS: {list(map(str, df.columns))}\nSAMPLE ROWS: {json.dumps(sample, ensure_ascii=False)[:6000]}\n"
                  "Return one entry per source column. target must be one of the target field names or null. "
                  "Each target used at most once. confidence 0-1. Use sample VALUES, not just headers.")
        res = llm.generate_json(prompt, MappingResult, task="onboarding_mapping")
        cols = res["columns"]
        valid = set(fields)
        seen = set()
        for c in cols:  # never trust the model blindly: enforce schema + uniqueness
            if c["target"] not in valid or c["target"] in seen:
                c["target"] = None
            seen.add(c["target"])
        by_src = {c["source"]: c for c in cols}
        return [by_src.get(str(col), {"source": str(col), "target": None, "confidence": 0.0, "reason": "omitted by model"})
                for col in df.columns]

    mapping, engine = llm.run_with_fallback("onboarding_mapping", via_llm, lambda: heuristic_mapping(df, entity))
    for m in mapping:
        m["needs_confirmation"] = m["target"] is not None and m["confidence"] < MIN_CONFIDENCE
    return mapping, engine
