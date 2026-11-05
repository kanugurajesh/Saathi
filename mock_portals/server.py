"""Three deliberately *different* fictional TPA / insurer portals, for exercising the bots.

  /nimbus  Nimbus TPA        - classic server-rendered table, pagination, short sessions
  /orbit   Orbit Insurance   - login + SMS OTP, no list view (search one claim at a time)
  /lotus   Lotus Health TPA  - randomised input names every load, JS-rendered list,
                               flaky API (503s), query letters behind a link

Plus /sms-gateway (simulated SMS-forwarder inbox), /dev/phone (simulated desk phone for the
manual-OTP path) and /admin (advance claim lifecycles, tune flakiness/OTP mode for demos).

Run:  uv run uvicorn mock_portals.server:app --port 8100
"""
from __future__ import annotations

import html
import json
import os
import random
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

SEED_FILE = Path(__file__).resolve().parent.parent / "samples" / "seed_claims.json"
rng = random.Random(int(os.getenv("MOCK_SEED", "7")))

USERS = {
    "nimbus": {"godavari_tpa": "demo@123"},
    "orbit": {("HSP-TS-0420", "tpadesk"): "demo@123"},
    "lotus": {"godavari.billing": "demo@123"},
}
OTP_PHONE = "9876500011"


class State:
    def __init__(self):
        self.reset()

    def reset(self):
        data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
        self.claims = {c["portal_ref"]: {**c, "idx": c.get("portal_start", 0)} for c in data["claims"]}
        self.sessions: dict[str, dict] = {}
        self.pending_otp: dict[str, dict] = {}
        self.sms: list[dict] = []
        self.lotus_forms: dict[str, dict] = {}
        self.config = {"otp_delivery": "auto", "flaky_rate": float(os.getenv("MOCK_FLAKY_RATE", "0.3")),
                       "session_ttl_s": 900, "fail_next": 0}
        self.hits = {"nimbus_login": 0, "orbit_login": 0, "lotus_login": 0, "lotus_503": 0}

    def step(self, ref: str) -> dict:
        c = self.claims[ref]
        return c["portal_script"][c["idx"]]

    def portal_claims(self, portal: str) -> list[dict]:
        return [c for c in self.claims.values() if c["portal"] == portal]

    def new_session(self, portal: str) -> str:
        sid = secrets.token_hex(12)
        self.sessions[sid] = {"portal": portal, "at": time.time()}
        return sid

    def valid(self, request: Request, portal: str) -> bool:
        sid = request.cookies.get(f"{portal}_sid")
        s = self.sessions.get(sid or "")
        return bool(s and s["portal"] == portal and time.time() - s["at"] < self.config["session_ttl_s"])


S = State()
app = FastAPI(title="Mock TPA portals (fictional)")


def page(title: str, body: str, css: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{margin:0;font-family:system-ui,Segoe UI,Arial}} .demo{{background:#222;color:#ffd54f;font-size:12px;padding:4px 12px}}
{css}</style></head><body><div class="demo">DEMO - fictional portal for automation testing. No real insurer/TPA.</div>{body}</body></html>""")


def money(v) -> str:
    return f"{v:,}" if isinstance(v, int) else "-"


@app.get("/", response_class=HTMLResponse)
def index():
    return page("Mock portals", """<div style="padding:24px"><h1>Mock TPA portals</h1><ul>
<li><a href="/nimbus/login">Nimbus TPA</a></li><li><a href="/orbit/">Orbit Insurance hospital portal</a></li>
<li><a href="/lotus/login">Lotus Health TPA</a></li><li><a href="/dev/phone">Desk phone (SMS)</a></li>
<li><a href="/admin/state">Admin state</a></li></ul></div>""", "")


# ===================================================================== NIMBUS
NIMBUS_CSS = """header{background:#0b4f8a;color:#fff;padding:14px 24px;font-size:20px} main{padding:24px}
table{border-collapse:collapse;width:100%} td,th{border:1px solid #c7d3e0;padding:8px;text-align:left} th{background:#e8f0f8}
.alert-error{background:#fde2e2;color:#8a0b0b;padding:8px;margin-bottom:10px} form{max-width:320px} input{display:block;width:100%;margin:6px 0 12px;padding:8px}"""


@app.get("/nimbus/login", response_class=HTMLResponse)
def nimbus_login_page(error: str = "", expired: str = ""):
    msg = '<div class="alert-error">Invalid username or password</div>' if error else (
        '<div class="alert-error">Session expired. Please login again.</div>' if expired else "")
    return page("Nimbus TPA - Provider Login", f"""<header>Nimbus TPA &middot; Provider Portal</header><main>
<h2>Hospital login</h2>{msg}<form method="post" action="/nimbus/login">
<label for="username">Username</label><input id="username" name="username">
<label for="password">Password</label><input id="password" name="password" type="password">
<button type="submit">Login</button></form></main>""", NIMBUS_CSS)


@app.post("/nimbus/login")
def nimbus_login(username: str = Form(""), password: str = Form("")):
    S.hits["nimbus_login"] += 1
    if USERS["nimbus"].get(username) != password:
        return RedirectResponse("/nimbus/login?error=1", status_code=303)
    r = RedirectResponse("/nimbus/claims", status_code=303)
    r.set_cookie("nimbus_sid", S.new_session("nimbus"))
    return r


@app.get("/nimbus/claims", response_class=HTMLResponse)
def nimbus_claims(request: Request):
    if not S.valid(request, "nimbus"):
        return RedirectResponse("/nimbus/login?expired=1", status_code=303)
    p = max(1, int(request.query_params.get("page", "1") or 1))
    rows_all = S.portal_claims("nimbus")
    size = 2
    rows = rows_all[(p - 1) * size: p * size]
    trs = ""
    for c in rows:
        st = S.step(c["portal_ref"])
        ref = html.escape(c["portal_ref"])
        ref_cell = f'<a href="/nimbus/claims/{ref}">{ref}</a>' if st.get("query") else ref
        trs += (f"<tr><td class='ref'>{ref_cell}</td><td>{html.escape(c['fields']['patient_name'])}</td>"
                f"<td>{html.escape(c['fields'].get('policy_number') or '')}</td><td><span class='status'>{st['status']}</span></td>"
                f"<td class='amt'>{money(st.get('approved'))}</td><td>{time.strftime('%d-%m-%Y %H:%M')}</td></tr>")
    nxt = f'<a class="next" href="/nimbus/claims?page={p + 1}">Next &rsaquo;</a>' if p * size < len(rows_all) else ""
    return page("Nimbus TPA - Claims", f"""<header>Nimbus TPA &middot; Provider Portal</header><main><h2>Claims Dashboard</h2>
<table id="claims"><thead><tr><th>Claim No</th><th>Patient</th><th>Policy No</th><th>Status</th><th>Approved (Rs)</th><th>Last Updated</th></tr></thead>
<tbody>{trs}</tbody></table><p>Page {p} {nxt}</p></main>""", NIMBUS_CSS)


@app.get("/nimbus/claims/{ref}", response_class=HTMLResponse)
def nimbus_claim(request: Request, ref: str):
    if not S.valid(request, "nimbus"):
        return RedirectResponse("/nimbus/login?expired=1", status_code=303)
    st = S.step(ref)
    return page("Nimbus TPA - Claim", f"""<header>Nimbus TPA</header><main><h2>Claim {html.escape(ref)}</h2>
<p>Status: <b class="status">{st['status']}</b></p><h3>Query from medical team</h3>
<pre class="query-text">{html.escape(st.get('query', ''))}</pre></main>""", NIMBUS_CSS)


# ===================================================================== ORBIT
ORBIT_CSS = """.top{background:linear-gradient(90deg,#5b2c83,#8e44ad);color:#fff;padding:18px 32px} .box{max-width:420px;margin:32px auto;
background:#faf7fc;border:1px solid #ddd;border-radius:10px;padding:24px} input{width:100%;padding:9px;margin:4px 0 12px;box-sizing:border-box}
.err{color:#b00020} .kv{display:flex;justify-content:space-between;border-bottom:1px dashed #ccc;padding:6px 0} .k{color:#666}"""


@app.get("/orbit/", response_class=HTMLResponse)
def orbit_home(error: str = ""):
    err = '<p class="err">Authentication failed</p>' if error else ""
    return page("Orbit Insurance - Network Hospital Login", f"""<div class="top">Orbit General Insurance &middot; Network Hospital Desk</div>
<div class="box"><h3>Sign in</h3>{err}<form method="post" action="/orbit/login">
Hospital Code<input name="hospital_code" placeholder="HSP-XX-0000">User Name<input name="username">
Password<input name="password" type="password"><button>Send OTP</button></form></div>""", ORBIT_CSS)


@app.post("/orbit/login")
def orbit_login(hospital_code: str = Form(""), username: str = Form(""), password: str = Form("")):
    S.hits["orbit_login"] += 1
    if USERS["orbit"].get((hospital_code, username)) != password:
        return RedirectResponse("/orbit/?error=1", status_code=303)
    otp = f"{rng.randint(0, 999999):06d}"
    token = secrets.token_hex(8)
    S.pending_otp[token] = {"otp": otp, "at": time.time(), "tries": 0}
    msg = {"phone": OTP_PHONE, "from": "OR-ORBITI", "text": f"{otp} is your Orbit hospital portal OTP. Valid 5 min. Do not share.",
           "received_at": time.time(), "delivered_to_forwarder": S.config["otp_delivery"] == "auto"}
    S.sms.append(msg)
    r = RedirectResponse("/orbit/otp", status_code=303)
    r.set_cookie("orbit_pending", token)
    return r


@app.get("/orbit/otp", response_class=HTMLResponse)
def orbit_otp_page(error: str = ""):
    err = '<p class="err">Invalid OTP</p>' if error else ""
    return page("Orbit - Verify OTP", f"""<div class="top">Orbit General Insurance &middot; Network Hospital Desk</div>
<div class="box"><h3>Verify OTP</h3><p>An OTP has been sent to ******{OTP_PHONE[-4:]}</p>{err}
<form method="post" action="/orbit/otp"><input name="otp" maxlength="6" autocomplete="one-time-code"><button>Verify</button></form></div>""", ORBIT_CSS)


@app.post("/orbit/otp")
def orbit_verify(request: Request, otp: str = Form("")):
    token = request.cookies.get("orbit_pending", "")
    p = S.pending_otp.get(token)
    if not p or time.time() - p["at"] > 300:
        return RedirectResponse("/orbit/?error=1", status_code=303)
    if otp.strip() != p["otp"]:
        p["tries"] += 1
        return RedirectResponse("/orbit/otp?error=1", status_code=303)
    S.pending_otp.pop(token, None)
    r = RedirectResponse("/orbit/search", status_code=303)
    r.set_cookie("orbit_sid", S.new_session("orbit"))
    return r


@app.get("/orbit/search", response_class=HTMLResponse)
def orbit_search(request: Request, claim_ref: str = ""):
    if not S.valid(request, "orbit"):
        return RedirectResponse("/orbit/", status_code=303)
    result = ""
    if claim_ref:
        c = S.claims.get(claim_ref.strip())
        if not c or c["portal"] != "orbit":
            result = '<div class="notfound">No claim found for the given reference.</div>'
        else:
            st = S.step(c["portal_ref"])
            defi = f'<div class="kv"><span class="k">Deficiency</span></div><pre data-k="deficiency">{html.escape(st["query"])}</pre>' if st.get("query") else ""
            result = f"""<div id="claim-detail">
<div class="kv"><span class="k">Claim Ref</span><span class="v" data-k="ref">{c['portal_ref']}</span></div>
<div class="kv"><span class="k">Insured</span><span class="v" data-k="patient">{html.escape(c['fields']['patient_name'])}</span></div>
<div class="kv"><span class="k">Status Code</span><span class="v" data-k="status">{st['status']}</span></div>
<div class="kv"><span class="k">Sanctioned (INR)</span><span class="v" data-k="sanctioned">{money(st.get('approved'))}</span></div>{defi}</div>"""
    return page("Orbit - Claim Search", f"""<div class="top">Orbit General Insurance &middot; Network Hospital Desk</div>
<div class="box"><form method="get" action="/orbit/search">Claim / Pre-auth reference<input name="claim_ref" value="{html.escape(claim_ref)}">
<button>Search</button></form>{result}</div>""", ORBIT_CSS)


@app.get("/sms-gateway/inbox")
def sms_inbox(phone: str, since: float = 0):
    """What an SMS-forwarder app on the TPA desk phone would POST to us. Only 'auto' deliveries appear."""
    return [m for m in S.sms if m["phone"] == phone and m["received_at"] > since and m["delivered_to_forwarder"]]


@app.get("/dev/phone", response_class=HTMLResponse)
def dev_phone():
    items = "".join(f"<li><b>{m['from']}</b> {html.escape(m['text'])} <small>{time.strftime('%H:%M:%S', time.localtime(m['received_at']))}</small></li>"
                    for m in reversed(S.sms[-10:]))
    return page("Desk phone", f"<div style='padding:20px;max-width:480px'><h2>📱 TPA desk phone ({OTP_PHONE})</h2><ul>{items or '<li>No messages</li>'}</ul></div>"
                "<script>setTimeout(()=>location.reload(),3000)</script>", "")


# ===================================================================== LOTUS
LOTUS_CSS = """nav{background:#0f766e;color:#fff;padding:12px 28px;display:flex;justify-content:space-between} .wrap{padding:28px;display:grid;
grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px} article{border:1px solid #cde;border-radius:12px;padding:14px;box-shadow:0 1px 3px #0001}
.st{display:inline-block;background:#e0f2f1;padding:2px 8px;border-radius:99px} .banner-error{background:#fff3cd;padding:12px 28px}
.login{max-width:340px;margin:40px auto} .login input{width:100%;padding:8px;margin:4px 0 14px;box-sizing:border-box}"""


@app.get("/lotus/login", response_class=HTMLResponse)
def lotus_login_page(error: str = ""):
    # Field names AND ids change on every load; only the visible labels are stable.
    tok = secrets.token_hex(6)
    u, p = f"f_{secrets.token_hex(3)}", f"f_{secrets.token_hex(3)}"
    S.lotus_forms[tok] = {"u": u, "p": p}
    err = '<p style="color:#b91c1c">Login failed</p>' if error else ""
    order = [(f'<label for="i{u}">User ID</label><input id="i{u}" name="{u}">'),
             (f'<label for="i{p}">Password</label><input id="i{p}" name="{p}" type="password">')]
    return page("Lotus Health TPA", f"""<nav><b>Lotus Health TPA</b><span>Provider Connect</span></nav><div class="login">{err}
<form method="post" action="/lotus/login"><input type="hidden" name="form_token" value="{tok}">{''.join(order)}
<button type="submit">Sign in</button></form></div>""", LOTUS_CSS)


@app.post("/lotus/login")
async def lotus_login(request: Request):
    S.hits["lotus_login"] += 1
    form = await request.form()
    names = S.lotus_forms.pop(str(form.get("form_token", "")), None)
    if not names or USERS["lotus"].get(str(form.get(names["u"], ""))) != str(form.get(names["p"], "")):
        return RedirectResponse("/lotus/login?error=1", status_code=303)
    r = RedirectResponse("/lotus/app", status_code=303)
    r.set_cookie("lotus_sid", S.new_session("lotus"))
    return r


@app.get("/lotus/app", response_class=HTMLResponse)
def lotus_app(request: Request):
    if not S.valid(request, "lotus"):
        return RedirectResponse("/lotus/login", status_code=303)
    js = """
fetch('/lotus/api/claims').then(r=>{ if(!r.ok) throw new Error(r.status); return r.json(); }).then(rows=>{
  const w=document.getElementById('list');
  w.innerHTML = rows.map(c=>`<article class="claim" data-claim-ref="${c.ref}"><h3>${c.patient}</h3>
   <div>Ref <code>${c.ref}</code></div><div>Policy ${c.policy}</div><p><span class="st">${c.status}</span></p>
   <div>Authorised: <span class="amt">${c.approved ?? '-'}</span></div>
   ${c.has_letter ? `<a class="letter" href="/lotus/api/letters/${c.ref}">View letter</a>` : ''}</article>`).join('');
  w.setAttribute('data-loaded','1');
}).catch(e=>{ document.getElementById('msg').innerHTML='<div class="banner-error" role="alert">Service temporarily unavailable. Please retry.</div>'; });
"""
    return page("Lotus - My cases", f"""<nav><b>Lotus Health TPA</b><span>Provider Connect &middot; My cases</span></nav>
<div id="msg"></div><div class="wrap" id="list"><p>Loading cases…</p></div><script>{js}</script>""", LOTUS_CSS)


@app.get("/lotus/api/claims")
def lotus_api(request: Request):
    if not S.valid(request, "lotus"):
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    if S.config.get("fail_next", 0) > 0 or rng.random() < S.config["flaky_rate"]:
        S.config["fail_next"] = max(0, S.config.get("fail_next", 0) - 1)
        S.hits["lotus_503"] += 1
        return JSONResponse({"error": "maintenance"}, status_code=503)
    out = []
    for c in S.portal_claims("lotus"):
        st = S.step(c["portal_ref"])
        out.append({"ref": c["portal_ref"], "patient": c["fields"]["patient_name"], "policy": c["fields"].get("policy_number"),
                    "status": st["status"], "approved": money(st["approved"]) if st.get("approved") else None,
                    "has_letter": bool(st.get("query"))})
    return out


@app.get("/lotus/api/letters/{ref}", response_class=PlainTextResponse)
def lotus_letter(request: Request, ref: str):
    if not S.valid(request, "lotus"):
        return PlainTextResponse("unauthorised", status_code=401)
    return S.step(ref).get("query", "")


# ===================================================================== ADMIN

@app.post("/admin/advance")
def admin_advance(ref: str | None = None):
    moved = []
    for c in S.claims.values():
        if ref and c["portal_ref"] != ref:
            continue
        if c["idx"] < len(c["portal_script"]) - 1:
            c["idx"] += 1
            moved.append({"ref": c["portal_ref"], "status": S.step(c["portal_ref"])["status"]})
    return {"moved": moved}


@app.post("/admin/reset")
def admin_reset():
    S.reset()
    return {"ok": True}


@app.post("/admin/config")
async def admin_config(request: Request):
    S.config.update(await request.json())
    return S.config


@app.post("/admin/expire-sessions")
def admin_expire():
    S.sessions.clear()
    return {"ok": True}


@app.get("/admin/state")
def admin_state():
    return {"config": S.config, "hits": S.hits,
            "claims": {r: {"portal": c["portal"], "status": S.step(r)["status"]} for r, c in S.claims.items()}}
