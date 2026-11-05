"""OTP broker: get the OTP automatically when possible, ask a human when not, give up cleanly.

Production wiring: an SMS-forwarder app on the TPA-desk phone (or a WhatsApp Business /
SMS gateway) POSTs inbound messages to /api/sms/inbound -> db.sms_inbox. In the demo the
mock portal also exposes /sms-gateway/inbox which we poll as a second source.

  1. automatic  - poll the inbox(es) for a fresh 6-digit code      (seconds)
  2. human      - create an otp_request; the dashboard shows a box (minutes)
  3. give up    - raise OtpUnavailable -> claim routed to manual review
"""
from __future__ import annotations

import re
import time
from datetime import timedelta

import httpx

from ..alerts import raise_alert
from ..config import settings
from ..db import get_db, now
from .base import OtpUnavailable

OTP_RE = re.compile(r"\b(\d{6})\b")


class OtpBroker:
    def __init__(self, run_id: str, log, *, gateway_url: str | None = None):
        self.run_id = run_id
        self.log = log
        self.gateway_url = gateway_url if gateway_url is not None else settings.portal_base_url

    def _from_sources(self, phone: str, since: float) -> str | None:
        db = get_db()
        msg = db.sms_inbox.find_one({"phone": phone, "received_ts": {"$gt": since}, "used": {"$ne": True}},
                                    sort=[("received_ts", -1)])
        if msg and (m := OTP_RE.search(msg["text"])):
            db.sms_inbox.update_one({"_id": msg["_id"]}, {"$set": {"used": True}})
            return m.group(1)
        if self.gateway_url:
            try:
                rows = httpx.get(f"{self.gateway_url}/sms-gateway/inbox", params={"phone": phone, "since": since}, timeout=3).json()
                for row in reversed(rows):
                    if m := OTP_RE.search(row["text"]):
                        return m.group(1)
            except (httpx.HTTPError, ValueError):
                pass
        return None

    def get_otp(self, portal: str, phone: str, since: float) -> str:
        deadline = time.time() + settings.otp_auto_wait_s
        while time.time() < deadline:
            if otp := self._from_sources(phone, since):
                self.log(f"OTP received automatically via SMS forwarder (******{phone[-4:]})")
                return otp
            time.sleep(0.8)

        self.log("OTP not seen by the SMS forwarder - asking a human on the dashboard")
        db = get_db()
        req = {"portal": portal, "phone_masked": f"******{phone[-4:]}", "run_id": self.run_id, "status": "pending",
               "created_at": now(), "expires_at": now() + timedelta(seconds=settings.otp_human_wait_s)}
        req_id = db.otp_requests.insert_one(req).inserted_id
        raise_alert("otp_needed", "warning", f"OTP needed for {portal} portal",
                    f"Enter the OTP sent to {req['phone_masked']} on the dashboard within {int(settings.otp_human_wait_s)}s.",
                    dedupe_key=f"otp:{req_id}")
        deadline = time.time() + settings.otp_human_wait_s
        while time.time() < deadline:
            r = db.otp_requests.find_one({"_id": req_id})
            if r["status"] == "submitted" and r.get("otp"):
                db.otp_requests.update_one({"_id": req_id}, {"$set": {"status": "used", "used_at": now()}})
                self.log("OTP provided by staff via dashboard")
                return r["otp"]
            if r["status"] == "skipped":
                break
            time.sleep(0.8)
        db.otp_requests.update_one({"_id": req_id, "status": "pending"}, {"$set": {"status": "expired"}})
        raise OtpUnavailable(f"No OTP for {portal} within the allowed window")
