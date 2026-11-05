"""Record the ~3-minute product demo as a captioned video (no voiceover).

    uv run python -m scripts.record_demo                 # deterministic engine (no API quota needed)
    uv run python -m scripts.record_demo --gemini        # use the Gemini key from .env for the AI shots

Starts fresh mock portals (:8101), the Node HMS receiver (:8301) and the app (:8001), drives the dashboard with
Playwright, and writes prep/demo.mp4 (plus the raw .webm). Ports must be free: stale servers keep old portal state.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "prep"
APP_PORT, PORTAL_PORT, HMS_PORT = 8001, 8101, 8301  # own ports, so a dev server already on 8000/8100 is left alone
APP, PORTALS, HMS = (f"http://127.0.0.1:{n}" for n in (APP_PORT, PORTAL_PORT, HMS_PORT))
W, H = 1440, 900
SECRET = "demo-secret"

CAPTION_JS = """([text, sub]) => {
  let el = document.getElementById('__cap');
  if (!el) {
    el = document.createElement('div'); el.id = '__cap';
    el.style.cssText = 'position:fixed;left:50%;bottom:28px;transform:translateX(-50%);max-width:1100px;z-index:99999;' +
      'background:rgba(15,23,42,.92);color:#fff;padding:14px 22px;border-radius:12px;font:500 19px/1.4 system-ui,sans-serif;' +
      'box-shadow:0 8px 30px rgba(0,0,0,.35);text-align:center;transition:opacity .3s';
    document.body.appendChild(el);
  }
  el.innerHTML = text + (sub ? '<div style="font-size:15px;opacity:.75;margin-top:4px">' + sub + '</div>' : '');
  el.style.display = text ? 'block' : 'none';
}"""

RING_JS = """([x, y]) => {
  const r = document.createElement('div');
  r.style.cssText = `position:fixed;left:${x - 22}px;top:${y - 22}px;width:44px;height:44px;border-radius:50%;` +
    'border:4px solid #f43f5e;background:rgba(244,63,94,.18);z-index:99998;pointer-events:none;transition:all .6s ease-out';
  document.body.appendChild(r);
  setTimeout(() => { r.style.transform = 'scale(1.6)'; r.style.opacity = '0'; }, 350);
  setTimeout(() => r.remove(), 1000);
}"""

CARD_HTML = """<!doctype html><meta charset="utf-8"><body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:linear-gradient(135deg,#0d7a6f,#0f172a);color:#fff;font-family:system-ui,sans-serif">
<div style="max-width:1050px;padding:40px">{body}</div></body>"""


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def wait_http(url: str, timeout: float = 60) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up")


def start_servers(use_gemini: bool) -> list[subprocess.Popen]:
    busy = [p for p in (APP_PORT, PORTAL_PORT, HMS_PORT) if not port_free(p)]
    if busy:
        sys.exit(f"Ports in use: {busy}. Stop those servers first (stale portal state spoils the demo).")
    logs = OUT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HMS_WEBHOOK_URL": f"{HMS}/claimpilot", "HMS_WEBHOOK_SECRET": SECRET, "PYTHONUNBUFFERED": "1",
           "PORTAL_BASE_URL": PORTALS, "DASHBOARD_URL": f"{APP}/#receivables"}
    if not use_gemini:
        env["LLM_DISABLED"] = "1"
    py = [sys.executable, "-m", "uvicorn"]
    procs = [
        subprocess.Popen([*py, "mock_portals.server:app", "--port", str(PORTAL_PORT)], cwd=ROOT, env=env,
                         stdout=open(logs / "portals.log", "w"), stderr=subprocess.STDOUT),
        subprocess.Popen(["node", "webhook-receiver.js"], cwd=ROOT / "integrations" / "hms-node",
                         env={**env, "PORT": str(HMS_PORT)}, stdout=open(logs / "hms.log", "w"), stderr=subprocess.STDOUT),
        subprocess.Popen([*py, "app.main:app", "--port", str(APP_PORT)], cwd=ROOT, env=env,
                         stdout=open(logs / "app.log", "w"), stderr=subprocess.STDOUT),
    ]
    for url in (f"{PORTALS}/admin/state", HMS, f"{APP}/api/health"):
        wait_http(url)
    return procs


class Demo:
    def __init__(self, page: Page):
        self.p = page

    def cap(self, text: str, sub: str = "", hold: float = 0) -> None:
        self.p.evaluate(CAPTION_JS, [text, sub])
        if hold:
            self.p.wait_for_timeout(int(hold * 1000))

    def click(self, selector: str, pause: float = 0.6) -> None:
        loc = self.p.locator(selector).first
        loc.scroll_into_view_if_needed()
        box = loc.bounding_box()
        if box:
            self.p.evaluate(RING_JS, [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
            self.p.wait_for_timeout(450)
        loc.click()
        self.p.wait_for_timeout(int(pause * 1000))

    def view(self, name: str, settle: float = 1.2) -> None:
        self.cap("")  # a caption never outlives the screen it describes
        self.click(f"#nav button[data-view='{name}']", pause=settle)

    def scroll(self, y: int, wait: float = 1.0) -> None:
        self.p.evaluate(f"window.scrollTo({{top:{y},behavior:'smooth'}})")
        self.p.wait_for_timeout(int(wait * 1000))

    def card(self, body: str, hold: float) -> None:
        self.p.set_content(CARD_HTML.replace("{body}", body))
        self.p.wait_for_timeout(int(hold * 1000))

    def wait_sync_done(self, timeout: float = 240) -> None:
        self.p.wait_for_timeout(1500)
        end = time.time() + timeout
        while time.time() < end:
            if not httpx.get(f"{APP}/api/sync/status", timeout=5).json()["running"]:
                return
            self.p.wait_for_timeout(1000)
        raise RuntimeError("sync did not finish")


def run(d: Demo) -> None:
    p = d.p
    # ---------------------------------------------------------------- 0. title
    d.card("""<div style="font-size:15px;letter-spacing:.12em;text-transform:uppercase;opacity:.7">Prototype · AI Automation Intern application</div>
      <h1 style="font-size:54px;margin:14px 0 10px">ClaimPilot</h1>
      <div style="font-size:24px;opacity:.9;line-height:1.5">TPA portal bots · AI pre-auth intake · signed webhooks into the HMS<br>
      plus Receivables and an Onboarding Copilot</div>
      <div style="margin-top:30px;font-size:16px;opacity:.65">All portals, hospitals and patients in this demo are fictional · runs locally</div>""", 5)

    # The HMS's IPD module creates a claim from an admission (the Node.js reference client).
    subprocess.run(["node", "claimpilot-client.js"], cwd=ROOT / "integrations" / "hms-node",
                   env={**os.environ, "CLAIMPILOT_URL": APP}, check=True, capture_output=True)

    # ---------------------------------------------------------------- 1. command center
    p.goto(APP)
    p.wait_for_selector("#claimsTable tr")
    p.wait_for_timeout(1200)
    d.cap("A hospital TPA desk logs into 5–10 insurer portals a day and copies statuses by hand.",
          "ClaimPilot puts every cashless claim in one queue, with IRDAI's 1-hour and 3-hour clocks.", 5.5)
    d.cap("HX-CLM-0010 was just created by the HMS itself: one REST call from the IPD admission.",
          "The IP number is the idempotency key, so a retried call returns the same claim.", 5)

    # ---------------------------------------------------------------- 2. sync
    d.cap("Sync: Playwright bots log into three deliberately different portals.", "", 1.5)
    d.click("#view-command button:has-text('Sync all portals')", pause=1)
    d.cap("Pagination and session expiry · an SMS OTP · randomised field names and random 503s.",
          "Every bot step is audited; failures retry with backoff or go to manual review.")
    d.wait_sync_done()
    p.wait_for_timeout(1500)
    p.evaluate("document.querySelector('#runs details')?.setAttribute('open', '')")
    p.wait_for_timeout(3500)
    d.view("command")
    d.cap("Statuses are normalised into one lifecycle: query raised, partially approved, denied…",
          "A TPA query triggers an auto-drafted reply; a denial raises a critical alert.", 6)

    # ---------------------------------------------------------------- 3. HMS side
    p.goto(HMS)
    p.wait_for_timeout(800)
    d.cap("Meanwhile, in the HMS: every change arrives as a signed webhook and becomes a billing action.",
          "Node.js receiver · HMAC-SHA256 verified · deduped by event id · ordered per claim", 7)

    # ---------------------------------------------------------------- 4. human OTP
    p.goto(APP)
    p.wait_for_selector("#claimsTable tr")
    d.view("bots")
    d.cap("What if the OTP only reaches a human's phone?", "", 1.5)
    p.locator("#otpMode").select_option("manual")
    p.wait_for_timeout(800)
    d.click("button:has-text('Advance TPA activity')", pause=1)
    d.click("#view-bots button:has-text('Orbit (OTP)')", pause=1)
    d.cap("The bot asks on the dashboard and waits. No answer in time → the claim goes to manual review.")
    p.wait_for_selector("#otpQueue .otp-card", timeout=60000)
    p.wait_for_timeout(2500)
    p.goto(f"{PORTALS}/dev/phone")
    d.cap("The OTP arrives on the TPA desk phone…", "", 2.5)
    code = re.search(r"\b(\d{6})\b", p.inner_text("body")).group(1)
    p.goto(APP)
    p.wait_for_selector("#claimsTable tr")
    d.view("bots")
    otp_input = p.locator("#otpQueue input").first
    otp_input.wait_for()
    d.cap("…staff type it in, and the bot carries on.", "", 0.5)
    otp_input.click()
    otp_input.type(code, delay=180)
    d.click("#otpQueue button:has-text('Submit OTP')", pause=1)
    d.wait_sync_done()
    p.wait_for_timeout(2500)

    # ---------------------------------------------------------------- 5. correspondence
    claims = httpx.get(f"{APP}/api/claims", timeout=5).json()
    ramesh = next(c for c in claims if c["fields"].get("patient_name") == "Ramesh Goud")
    draft = next(x for x in httpx.get(f"{APP}/api/drafts", timeout=5).json()
                 if x["claim_id"] == ramesh["_id"] and x["kind"] == "query_reply")
    d.view("drafts")
    p.evaluate(f"openDraft('{draft['_id']}')")
    p.wait_for_selector("#draftPreview")
    p.wait_for_timeout(800)
    d.cap("The TPA's query on Ramesh Goud, answered point by point from the record. The USG report is attached.",
          "", 6)
    d.scroll(380, 1.5)
    d.cap("Alcohol history isn't in the record, so it becomes a placeholder for the treating doctor.",
          "The AI never invents a clinical fact, amount or attachment: a safety gate checks every draft.", 6)
    d.cap("Try to send it anyway…", "", 1)
    d.click("#draftView button:has-text('Mark as sent')", pause=0.3)
    d.cap("…refused: every placeholder must be filled first.", "", 4.5)
    d.scroll(0, 0.5)

    # ---------------------------------------------------------------- 6. AI intake
    d.view("intake")
    p.locator("#sampleSel").select_option("06_casualty_rta_head_injury.txt")
    p.wait_for_timeout(600)
    d.click("#view-intake button:has-text('Extract')", pause=0.5)
    engine = "Gemini 2.5 Flash" if "gemini" in str(httpx.get(f"{APP}/api/health", timeout=5).json()["llm"]).lower()         and not os.environ.get("LLM_DISABLED") else "the rules engine (Gemini 2.5 Flash when a key is set)"
    d.cap(f"AI pre-auth intake: 24 fields extracted from a casualty case sheet by {engine}.",
          "Every value is then checked against the source document before anyone trusts it.")
    p.wait_for_selector("#extractCard", state="visible", timeout=90000)
    p.wait_for_timeout(2500)
    d.cap("The query-risk screen flags what the TPA will ask: this road-accident case has no alcohol history and no MLC copy.",
          "Fix it before submitting, not after a query costs a day.", 6.5)
    d.scroll(520, 1.5)
    p.wait_for_timeout(3000)
    d.scroll(0, 0.5)

    # ---------------------------------------------------------------- 7. receivables
    d.view("receivables", settle=2)
    d.cap("Receivables: money owed by every payer (private TPAs, Aarogyasri, EHS, PM-JAY), grouped by age.",
          "Payers whose data is stale are flagged, so a 3-day-old figure never passes as today's.", 6)
    d.click("button:has-text(\"Upload today's sample Aarogyasri export\")", pause=2)
    d.cap("Today's Aarogyasri export: 3 claims paid, 1 denied, a new deduction. Diffed against yesterday's.", "", 4)
    p.locator("#waCard").scroll_into_view_if_needed()
    p.wait_for_timeout(800)
    d.cap("The owner gets it every morning as a WhatsApp summary instead of finding out at month-end.", "", 5.5)
    d.scroll(0, 0.5)

    # ---------------------------------------------------------------- 8. onboarding
    d.view("onboarding")
    d.click("button:has-text('Run with sample legacy files')", pause=1)
    d.cap("Onboarding Copilot: messy Marg / Excel exports in, import-ready masters and a row-wise fix list out.")
    p.wait_for_function("document.querySelector('#obResult') && document.querySelector('#obResult').innerText.length > 50",
                        timeout=60000)
    p.wait_for_timeout(1500)
    p.locator("#obResult").scroll_into_view_if_needed()
    d.cap("Expired stock, a doctor without a registration number, an extra zero in a tariff…",
          "Go-live readiness is scored against the 24-hour target, with alerts before the deadline slips.", 7)

    # ---------------------------------------------------------------- 9. close
    d.card("""<h1 style="font-size:40px;margin:0 0 22px">What's real, what's mocked</h1>
      <div style="font-size:21px;line-height:1.75">
      ✅ Bot engine, OTP fallback, status normalisation, safety gate, HMS webhooks: built and tested (50 + 3 tests)<br>
      ✅ Gemini 2.5 Flash extraction: run live, 0% hallucination on the gold set<br>
      🧪 The three TPA portals are fictional mocks of real failure patterns<br>
      🔜 Next: validate with a hospital TPA desk, then point an adapter at a real portal</div>
      <div style="margin-top:30px;font-size:17px;opacity:.7">Code, tests and the full list are in the README</div>""", 8)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gemini", action="store_true", help="use the Gemini key from .env for AI shots")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    raw_dir = OUT / "raw_video"
    shutil.rmtree(raw_dir, ignore_errors=True)
    procs = start_servers(args.gemini)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(raw_dir),
                                      record_video_size={"width": W, "height": H}, device_scale_factor=1)
            page = ctx.new_page()
            # recording only: toasts go top-centre so the caption bar never hides them
            page.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
              const st = document.createElement('style');
              st.textContent = '.toast{top:64px!important;bottom:auto!important;font-size:17px;padding:12px 20px!important;box-shadow:0 8px 24px rgba(0,0,0,.3)}';
              document.head.appendChild(st); });""")
            page.on("dialog", lambda dlg: dlg.accept())
            try:
                run(Demo(page))
            finally:
                video = page.video
                ctx.close()
                browser.close()
            webm = Path(video.path())
    finally:
        for pr in procs:
            pr.terminate()
        for pr in procs:
            try:
                pr.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pr.kill()

    final_webm = OUT / "demo.webm"
    shutil.move(str(webm), final_webm)
    shutil.rmtree(raw_dir, ignore_errors=True)
    mp4 = OUT / "demo.mp4"
    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(final_webm), "-c:v", "libx264", "-preset", "slow",
                        "-crf", "22", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)], check=True)
        print(f"wrote {mp4}")
    print(f"wrote {final_webm}")


if __name__ == "__main__":
    main()
