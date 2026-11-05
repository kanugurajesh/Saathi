"""Lotus Health TPA: hostile-ish modern portal.

  * input names/ids are randomised on every load -> locate by accessible *label*, not CSS id
  * list is rendered client-side from a JSON API -> wait for the render, not for page load
  * API intermittently returns 503 -> surface as TransientPortalError so the runner backs off
  * query letters live behind a link -> fetch with the page's authenticated request context
"""
from __future__ import annotations

from playwright.sync_api import Page

from .base import LoginFailed, PortalAdapter, SessionExpired, TransientPortalError, parse_amount


class LotusAdapter(PortalAdapter):
    key, name = "lotus", "Lotus Health TPA"

    def login(self, page: Page) -> None:
        self.log("Lotus: opening login page (dynamic field names)")
        page.goto(f"{self.base}/lotus/login")
        page.get_by_label("User ID").fill(self.creds["username"])
        page.get_by_label("Password").fill(self.creds["password"])
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_load_state("domcontentloaded")
        if "/login" in page.url:
            raise LoginFailed("Lotus rejected the credentials")
        self.log("Lotus: logged in")

    def fetch(self, page: Page, refs: list[str]) -> list[dict]:
        page.goto(f"{self.base}/lotus/app")
        if "/login" in page.url:
            raise SessionExpired("bounced to Lotus login")
        page.wait_for_selector("#list[data-loaded='1'], .banner-error", timeout=10_000)
        if page.locator(".banner-error").count():
            raise TransientPortalError("Lotus: 'Service temporarily unavailable'")
        snaps = []
        for card in page.locator("article.claim").all():
            ref = card.get_attribute("data-claim-ref")
            snap = {"portal_ref": ref, "raw_status": card.locator(".st").inner_text().strip(),
                    "patient_name": card.locator("h3").inner_text().strip(),
                    "approved_amount": parse_amount(card.locator(".amt").inner_text())}
            letter = card.locator("a.letter")
            if letter.count():
                resp = page.request.get(f"{self.base}{letter.get_attribute('href')}")
                if resp.status == 401:
                    raise SessionExpired("letter fetch unauthorised")
                snap["query_text"] = resp.text().strip()
                self.log(f"Lotus: fetched query letter for {ref}")
            snaps.append(snap)
        self.log(f"Lotus: {len(snaps)} cases read")
        return snaps
