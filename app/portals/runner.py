"""Portal sync orchestrator: one headless browser session per portal, full audit trail.

Every run is a Mongo document with timestamped steps, outcome, changes found and a
screenshot on failure - so when a portal breaks at 2am, someone can see exactly where.

CLI:  uv run python -m app.portals.runner [nimbus orbit lotus]
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

from ..alerts import raise_alert
from ..claims import service
from ..claims.status import TERMINAL
from ..config import settings
from ..db import clean, get_db, now
from .base import LayoutChanged, LoginFailed, OtpUnavailable, with_retries
from .lotus import LotusAdapter
from .nimbus import NimbusAdapter
from .orbit import OrbitAdapter
from .otp import OtpBroker

log = logging.getLogger(__name__)
ADAPTERS = {a.key: a for a in (NimbusAdapter, OrbitAdapter, LotusAdapter)}
_sync_lock = threading.Lock()


def run_portal(key: str, trigger: str = "manual") -> dict:
    db = get_db()
    run_id = f"run-{key}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')[:-3]}"
    db.portal_runs.insert_one({"_id": run_id, "portal": key, "trigger": trigger, "started_at": now(),
                               "status": "running", "steps": [], "changes": []})
    t0 = time.perf_counter()

    def step(msg: str) -> None:
        db.portal_runs.update_one({"_id": run_id}, {"$push": {"steps": {"at": now(), "msg": msg}}})
        log.info("[%s] %s", key, msg)

    refs = [c["portal_ref"] for c in db.claims.find({"portal": key, "status": {"$nin": [str(s) for s in TERMINAL]}},
                                                    {"portal_ref": 1})]
    outcome, error, snaps, shot = "success", None, [], None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=settings.headless)
            page = browser.new_context().new_page()
            page.set_default_timeout(15_000)
            adapter = ADAPTERS[key](settings.portal_base_url, settings.portal_credentials[key],
                                    OtpBroker(run_id, step), step)
            try:
                with_retries(lambda: adapter.login(page), log=step, what="login")
                snaps = with_retries(lambda: adapter.fetch(page, refs), log=step, what="fetch",
                                     on_session_expired=lambda: adapter.login(page))
            except Exception:
                settings.artifacts_dir.mkdir(exist_ok=True)
                shot = str(settings.artifacts_dir / f"{run_id}.png")
                try:
                    page.screenshot(path=shot, full_page=True)
                except Exception:
                    shot = None
                raise
            finally:
                browser.close()
    except OtpUnavailable as exc:
        outcome, error = "needs_manual_review", str(exc)
        raise_alert("portal", "warning", f"{key}: sync paused - OTP not available", f"{exc}. Claims on this portal need a manual check.",
                    dedupe_key=f"portal:{key}:otp")
    except LoginFailed as exc:
        outcome, error = "failed", str(exc)
        raise_alert("portal", "critical", f"{key}: login failed", f"{exc}. Credentials may have been rotated.",
                    dedupe_key=f"portal:{key}:login")
    except LayoutChanged as exc:
        outcome, error = "failed", str(exc)
        raise_alert("portal", "critical", f"{key}: portal layout changed", f"{exc}. Selectors need an update; screenshot saved.",
                    dedupe_key=f"portal:{key}:layout")
    except Exception as exc:  # transient errors that exhausted retries, browser crashes, ...
        outcome, error = "failed", f"{type(exc).__name__}: {exc}"
        raise_alert("portal", "critical", f"{key}: sync failed", error[:300], dedupe_key=f"portal:{key}:error")

    changes = []
    for s in snaps:
        if not s.get("portal_ref") or not s.get("raw_status"):
            continue
        r = service.apply_snapshot(key, s)
        if r["changed"]:
            changes.append(r)
    if snaps:
        step(f"Applied {len(snaps)} snapshots, {len(changes)} status change(s)")
    db.portal_runs.update_one({"_id": run_id}, {"$set": {
        "status": outcome, "error": error, "finished_at": now(), "duration_ms": int((time.perf_counter() - t0) * 1000),
        "claims_seen": len(snaps), "changes": changes, "screenshot": shot}})
    return clean(db.portal_runs.find_one({"_id": run_id}))


def run_sync(portals: list[str] | None = None, trigger: str = "manual") -> dict:
    if not _sync_lock.acquire(blocking=False):
        return {"error": "a sync is already running"}
    try:
        if sys.platform == "win32":  # Playwright needs subprocess support (Proactor loop) in this thread
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        runs = [run_portal(k, trigger) for k in (portals or list(ADAPTERS))]
        fired = service.sla_sweep()
        return {"runs": runs, "sla_alerts": len(fired)}
    finally:
        _sync_lock.release()


def is_running() -> bool:
    return _sync_lock.locked()


def start_background(portals: list[str] | None = None, trigger: str = "manual") -> bool:
    if is_running():
        return False
    threading.Thread(target=run_sync, args=(portals, trigger), daemon=True, name="portal-sync").start()
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from scripts.seed import seed

    seed()
    result = run_sync(sys.argv[1:] or None, trigger="cli")
    for r in result.get("runs", []):
        print(f"{r['portal']:7} {r['status']:20} seen={r.get('claims_seen')} changes={len(r.get('changes', []))} {r.get('error') or ''}")
