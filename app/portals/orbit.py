"""Orbit Insurance: credentials + SMS OTP, and no list view - claims are searched one by one.

Only claims the HMS already knows about can be synced here (we must know the reference),
so the runner passes the open claim refs in.
"""
from __future__ import annotations

import time
from urllib.parse import quote

from playwright.sync_api import Page

from .base import LayoutChanged, LoginFailed, OtpUnavailable, PortalAdapter, SessionExpired, parse_amount


class OrbitAdapter(PortalAdapter):
    key, name = "orbit", "Orbit Insurance"

    def login(self, page: Page) -> None:
        self.log("Orbit: opening login page")
        page.goto(f"{self.base}/orbit/")
        page.fill("input[name=hospital_code]", self.creds["hospital_code"])
        page.fill("input[name=username]", self.creds["username"])
        page.fill("input[name=password]", self.creds["password"])
        requested_at = time.time() - 1
        page.get_by_role("button", name="Send OTP").click()
        page.wait_for_load_state("domcontentloaded")
        if "error" in page.url or "/otp" not in page.url:
            raise LoginFailed("Orbit rejected hospital code / credentials")
        self.log("Orbit: OTP requested")
        otp = self.otp.get_otp("orbit", self.creds["otp_phone"], requested_at)
        page.fill("input[name=otp]", otp)
        page.get_by_role("button", name="Verify").click()
        page.wait_for_load_state("domcontentloaded")
        if "/otp" in page.url:
            raise OtpUnavailable("Orbit rejected the OTP (expired or mistyped)")
        self.log("Orbit: logged in with OTP")

    def fetch(self, page: Page, refs: list[str]) -> list[dict]:
        snaps = []
        for ref in refs:
            page.goto(f"{self.base}/orbit/search?claim_ref={quote(ref)}")
            if page.url.rstrip("/").endswith("/orbit"):
                raise SessionExpired("bounced to Orbit login")
            if page.locator(".notfound").count():
                self.log(f"Orbit: {ref} not found on portal")
                continue
            detail = page.locator("#claim-detail")
            if not detail.count():
                raise LayoutChanged("Orbit: claim detail block missing")

            def val(k: str) -> str | None:
                loc = detail.locator(f"[data-k={k}]")
                return loc.inner_text().strip() if loc.count() else None

            snaps.append({"portal_ref": val("ref") or ref, "raw_status": val("status"), "patient_name": val("patient"),
                          "approved_amount": parse_amount(val("sanctioned")), "query_text": val("deficiency")})
            self.log(f"Orbit: {ref} -> {val('status')}")
        return snaps
