// ClaimPilot -> HMS: receives signed claim events and turns them into IPD billing actions.
// Zero dependencies (node:http + node:crypto). In HextGen this would be one Express route.
//
//   HMS_WEBHOOK_SECRET=s3cret PORT=8300 node webhook-receiver.js
//   then run ClaimPilot with HMS_WEBHOOK_URL=http://127.0.0.1:8300/claimpilot HMS_WEBHOOK_SECRET=s3cret

import { createHmac, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";

const SECRET = process.env.HMS_WEBHOOK_SECRET || "";
const PORT = Number(process.env.PORT || 8300);
const seen = new Set(); // use a unique index on event_id in MongoDB in production

export function verify(rawBody, header, secret = SECRET) {
  if (!secret) return true; // signing disabled (local demo only)
  if (!header?.startsWith("sha256=")) return false;
  const expected = Buffer.from("sha256=" + createHmac("sha256", secret).update(rawBody).digest("hex"));
  const got = Buffer.from(header);
  return got.length === expected.length && timingSafeEqual(got, expected);
}

// What the IPD bill / dashboard should do for each lifecycle status (see app/claims/status.py).
const ACTIONS = {
  PREAUTH_SUBMITTED: "mark pre-auth as submitted on the IP bill",
  QUERY_RAISED: "flag the IP bill: TPA query open, reply draft waiting in ClaimPilot",
  PREAUTH_APPROVED: "record approved cashless limit; patient pays only the difference",
  PARTIALLY_APPROVED: "record approved limit and alert billing about the shortfall",
  ENHANCEMENT_REQUESTED: "mark enhancement as pending",
  DENIED: "switch the IP bill to self-pay / reimbursement and inform the patient",
  DISCHARGE_SUBMITTED: "hold discharge until final authorisation",
  FINAL_APPROVED: "release the patient; move the insurer share to receivables",
  SETTLED: "close the insurer share on the bill",
};

export function handle(ev) {
  if (ev.kind === "status_changed") {
    const action = ACTIONS[ev.status] || "show the new status on the IP bill";
    const amt = ev.approved_amount != null ? ` (approved Rs ${ev.approved_amount})` : "";
    return `${ev.hms_ref || ev.claim_id}: ${ev.data.from} -> ${ev.status}${amt} => ${action}`;
  }
  return `${ev.hms_ref || ev.claim_id}: ${ev.kind}`;
}

const log = []; // newest first, for the demo page at GET /
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function page() {
  const rows = log.map((e) => `<tr class="${e.ev.status === "DENIED" ? "bad" : e.ev.kind === "status_changed" ? "chg" : ""}">
    <td>${esc(new Date(e.at).toLocaleTimeString())}</td><td><b>${esc(e.ev.hms_ref || e.ev.claim_id)}</b></td><td>${esc(e.ev.kind)}</td>
    <td>${esc(e.ev.status || "")}</td><td>${esc(e.line.split("=> ")[1] || "")}</td><td class="ok">✓ verified</td></tr>`).join("");
  return `<!doctype html><meta charset="utf-8"><title>HMS · ClaimPilot events</title><meta http-equiv="refresh" content="2">
<style>body{font:15px system-ui,sans-serif;margin:0;background:#f6f7fb;color:#1c2330}header{background:#1e3a8a;color:#fff;padding:16px 28px;font-size:18px}
header small{opacity:.75;margin-left:10px;font-size:13px}main{padding:20px 28px}table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px #0002}
th,td{padding:9px 12px;border-bottom:1px solid #e7e9f0;text-align:left;font-size:14px}th{background:#eef1f8;font-size:12px;text-transform:uppercase;color:#56607a}
tr.chg td{background:#f3f8ff}tr.bad td{background:#fff1f1}.ok{color:#0a7a3f;font-weight:600}.empty{padding:30px;text-align:center;color:#778}</style>
<header>🏥 HMS · IPD billing <small>signed claim events received from ClaimPilot (HMAC-SHA256 ${SECRET ? "on" : "off"}) · ${log.length} events</small></header>
<main><table><thead><tr><th>Time</th><th>IP no. / claim</th><th>Event</th><th>Status</th><th>Billing action</th><th>Signature</th></tr></thead>
<tbody>${rows || '<tr><td colspan="6" class="empty">Waiting for events…</td></tr>'}</tbody></table></main>`;
}

if (process.argv[1]?.endsWith("webhook-receiver.js")) {
  createServer((req, res) => {
    if (req.method === "GET" && req.url === "/") return res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" }).end(page());
    if (req.method !== "POST" || req.url !== "/claimpilot") return res.writeHead(404).end();
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks); // verify the exact bytes, before JSON parsing
      if (!verify(raw, req.headers["x-claimpilot-signature"])) {
        console.log("rejected: bad signature");
        return res.writeHead(401).end();
      }
      const ev = JSON.parse(raw);
      if (seen.has(ev.event_id)) return res.writeHead(200).end(); // duplicate delivery: already applied
      seen.add(ev.event_id);
      const line = handle(ev);
      log.unshift({ at: Date.now(), ev, line });
      log.length = Math.min(log.length, 200);
      console.log(line);
      res.writeHead(204).end();
    });
  }).listen(PORT, () => console.log(`HMS webhook receiver on http://127.0.0.1:${PORT}/claimpilot (signing ${SECRET ? "on" : "off"})`));
}
