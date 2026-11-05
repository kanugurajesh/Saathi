"""Alerting: persisted in Mongo (dashboard feed) + optional Slack/Google-Chat style webhook.
Deduplicated so a stuck claim produces one alert, not one per sync cycle."""
from __future__ import annotations

import logging

import httpx

from .config import settings
from .db import get_db, now

log = logging.getLogger(__name__)
SEVERITIES = ("info", "warning", "critical")


def raise_alert(kind: str, severity: str, title: str, detail: str = "", *, claim_id: str | None = None,
                job_id: str | None = None, dedupe_key: str | None = None) -> dict | None:
    db = get_db()
    if dedupe_key and db.alerts.find_one({"dedupe_key": dedupe_key, "resolved": False}):
        return None
    doc = {"kind": kind, "severity": severity, "title": title, "detail": detail, "claim_id": claim_id,
           "job_id": job_id, "dedupe_key": dedupe_key, "resolved": False, "created_at": now()}
    db.alerts.insert_one(doc)
    log.info("ALERT [%s] %s - %s", severity, title, detail)
    if settings.alert_webhook_url and severity != "info":
        try:
            httpx.post(settings.alert_webhook_url, json={"text": f"[{severity.upper()}] {title}\n{detail}"}, timeout=3)
        except httpx.HTTPError as exc:
            log.warning("alert webhook failed: %s", exc)
    return doc


def resolve_alerts(dedupe_prefix: str) -> None:
    get_db().alerts.update_many({"dedupe_key": {"$regex": f"^{dedupe_prefix}"}, "resolved": False},
                                {"$set": {"resolved": True, "resolved_at": now()}})
