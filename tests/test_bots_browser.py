"""Real Chromium against the mock portals. Run: uv run pytest -m browser"""
import re
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.config import settings
from scripts.seed import seed
from tests.conftest import set_setting

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def portals():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    from mock_portals.server import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(base + "/admin/state", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    old = (settings.portal_base_url, settings.otp_auto_wait_s, settings.otp_human_wait_s)
    set_setting("portal_base_url", base)
    yield base
    set_setting("portal_base_url", old[0])
    set_setting("otp_auto_wait_s", old[1])
    set_setting("otp_human_wait_s", old[2])
    server.should_exit = True


def admin(base, path, json=None):
    return httpx.post(base + path, json=json, timeout=5).json()


@pytest.fixture(autouse=True)
def reset(portals, fresh_db):
    admin(portals, "/admin/reset")
    admin(portals, "/admin/config", {"flaky_rate": 0.0})
    seed()


def test_nimbus_pagination_and_query_letter(fresh_db):
    from app.portals.runner import run_portal

    r = run_portal("nimbus")
    assert r["status"] == "success" and r["claims_seen"] == 3
    c = fresh_db.claims.find_one({"portal_ref": "NB-26-104871"})
    assert c["status"] == "QUERY_RAISED" and "USG abdomen" in c["query"]["text"]
    assert fresh_db.drafts.count_documents({"claim_id": c["_id"]}) == 1


def test_lotus_dynamic_fields_and_503_retries(portals, fresh_db):
    from app.portals.runner import run_portal

    # 3 forced 503s: one is absorbed by the post-login redirect render, two hit fetch() -> two backoff retries
    admin(portals, "/admin/config", {"fail_next": 3})
    r = run_portal("lotus")
    assert r["status"] == "success"
    assert sum("transient error" in s["msg"] for s in r["steps"]) == 2
    assert fresh_db.claims.find_one({"portal_ref": "LT-88277"})["status"] == "DENIED"
    assert fresh_db.claims.find_one({"portal_ref": "LT-88301"})  # discovered claim registered


def test_orbit_auto_otp(fresh_db):
    from app.portals.runner import run_portal

    r = run_portal("orbit")
    assert r["status"] == "success"
    assert any("OTP received automatically" in s["msg"] for s in r["steps"])


def test_orbit_human_otp_via_dashboard(portals, fresh_db):
    from app.portals.runner import run_portal

    admin(portals, "/admin/config", {"otp_delivery": "manual"})
    set_setting("otp_auto_wait_s", 1.0)
    set_setting("otp_human_wait_s", 20.0)

    def staff_types_otp():
        for _ in range(100):
            req = fresh_db.otp_requests.find_one({"status": "pending"})
            if req:  # staff reads the desk phone and types the OTP into the dashboard
                page = httpx.get(portals + "/dev/phone").text
                otp = re.search(r"(\d{6}) is your Orbit", page).group(1)
                fresh_db.otp_requests.update_one({"_id": req["_id"]}, {"$set": {"status": "submitted", "otp": otp}})
                return
            time.sleep(0.2)

    t = threading.Thread(target=staff_types_otp)
    t.start()
    r = run_portal("orbit")
    t.join()
    assert r["status"] == "success"
    assert any("OTP provided by staff" in s["msg"] for s in r["steps"])


def test_orbit_otp_timeout_routes_to_manual_review(portals, fresh_db):
    from app.portals.runner import run_portal

    admin(portals, "/admin/config", {"otp_delivery": "manual"})
    set_setting("otp_auto_wait_s", 0.5)
    set_setting("otp_human_wait_s", 1.0)
    r = run_portal("orbit")
    assert r["status"] == "needs_manual_review"
    assert fresh_db.otp_requests.find_one()["status"] == "expired"
    assert fresh_db.alerts.find_one({"dedupe_key": "portal:orbit:otp"})


def test_bad_credentials_fail_fast_with_screenshot(fresh_db):
    from app.portals.runner import run_portal

    creds = settings.portal_credentials["nimbus"]
    old = creds["password"]
    creds["password"] = "wrong"
    try:
        r = run_portal("nimbus")
    finally:
        creds["password"] = old
    assert r["status"] == "failed" and "credentials" in r["error"]
    assert r["screenshot"] and fresh_db.alerts.find_one({"dedupe_key": "portal:nimbus:login"})
