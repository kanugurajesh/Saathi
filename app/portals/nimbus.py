"""Nimbus TPA: server-rendered table + pagination + short sessions.

Robustness choices:
  * columns are located by *header text*, not position (survives column re-ordering)
  * every navigation checks for a bounce to /login -> SessionExpired -> runner re-logs in
"""
from __future__ import annotations

from playwright.sync_api import Page

from .base import LayoutChanged, LoginFailed, PortalAdapter, SessionExpired, parse_amount

HEADERS = {"ref": ("claim no", "claim number"), "patient": ("patient",), "policy": ("policy",),
           "status": ("status",), "approved": ("approved",)}


class NimbusAdapter(PortalAdapter):
    key, name = "nimbus", "Nimbus TPA"

    def login(self, page: Page) -> None:
        self.log("Nimbus: opening login page")
        page.goto(f"{self.base}/nimbus/login")
        page.fill("#username", self.creds["username"])
        page.fill("#password", self.creds["password"])
        page.click("button[type=submit]")
        page.wait_for_load_state("domcontentloaded")
        if "/login" in page.url:
            raise LoginFailed("Nimbus rejected the credentials")
        self.log("Nimbus: logged in")

    def _guard(self, page: Page) -> None:
        if "/login" in page.url:
            raise SessionExpired("bounced to login")

    def _column_index(self, page: Page) -> dict[str, int]:
        heads = [h.strip().lower() for h in page.locator("table#claims thead th").all_inner_texts()]
        if not heads:
            raise LayoutChanged("Nimbus claims table not found")
        idx = {}
        for key, names in HEADERS.items():
            pos = next((i for i, h in enumerate(heads) if any(n in h for n in names)), None)
            if pos is None and key in ("ref", "status"):
                raise LayoutChanged(f"Nimbus: column for '{key}' missing (headers={heads})")
            idx[key] = pos
        return idx

    def fetch(self, page: Page, refs: list[str]) -> list[dict]:
        page.goto(f"{self.base}/nimbus/claims")
        self._guard(page)
        snaps: list[dict] = []
        page_no = 1
        while True:
            col = self._column_index(page)
            for row in page.locator("table#claims tbody tr").all():
                cells = [c.strip() for c in row.locator("td").all_inner_texts()]
                get = lambda k: cells[col[k]] if col.get(k) is not None and col[k] < len(cells) else None  # noqa: E731
                link = row.locator("td a")
                snaps.append({
                    "portal_ref": get("ref"), "raw_status": get("status"), "patient_name": get("patient"),
                    "policy_number": get("policy"), "approved_amount": parse_amount(get("approved")),
                    "_detail": link.first.get_attribute("href") if link.count() else None,
                })
            self.log(f"Nimbus: read page {page_no} ({len(snaps)} rows so far)")
            nxt = page.locator("a.next")
            if not nxt.count():
                break
            nxt.first.click()
            page.wait_for_load_state("domcontentloaded")
            self._guard(page)
            page_no += 1

        for s in snaps:
            href = s.pop("_detail")
            if href:
                page.goto(f"{self.base}{href}")
                self._guard(page)
                s["query_text"] = page.locator("pre.query-text").inner_text().strip()
                self.log(f"Nimbus: fetched query letter for {s['portal_ref']}")
        return snaps
