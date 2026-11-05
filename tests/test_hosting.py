"""Hosted-demo plumbing: optional password gate and the proxied demo pages."""
import base64

import httpx
from fastapi.testclient import TestClient

from tests.conftest import set_setting


def _client():
    from app.main import app
    return TestClient(app)  # no lifespan: no seeding, no background loop


def _basic(pw: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"anyone:{pw}".encode()).decode()}


def test_password_gate(fresh_db, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
    c = _client()
    set_setting("demo_password", "open-sesame")
    try:
        assert c.get("/api/health").status_code == 200  # platform health check stays open
        r = c.get("/api/claims")
        assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
        assert c.get("/api/claims", headers=_basic("wrong")).status_code == 401
        assert c.get("/api/claims", headers={"Authorization": "Basic !!notbase64"}).status_code == 401
        assert c.get("/api/claims", headers=_basic("open-sesame")).status_code == 200
    finally:
        set_setting("demo_password", "")
    assert c.get("/api/claims").status_code == 200  # no password configured: open, as in local dev


def test_demo_pages_are_proxied(fresh_db, monkeypatch):
    seen = []

    def fake_get(url, timeout):
        seen.append(url)
        return httpx.Response(200, text="<h2>page</h2>")

    monkeypatch.setattr(httpx, "get", fake_get)
    c = _client()
    assert c.get("/demo/phone").text == "<h2>page</h2>" and seen[-1].endswith("/dev/phone")
    assert c.get("/demo/hms").status_code == 404  # receiver not configured
    set_setting("hms_demo_url", "http://127.0.0.1:8300")
    try:
        assert c.get("/demo/hms").status_code == 200 and seen[-1] == "http://127.0.0.1:8300/"
        h = c.get("/api/health").json()
        assert h["phone_url"] == "/demo/phone" and h["hms_demo"] is True
    finally:
        set_setting("hms_demo_url", "")
