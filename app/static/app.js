/* HextGen Automation Suite - dashboard (vanilla JS, no build step). All portal/LLM text is escaped. */
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = (n) => (n === null || n === undefined || n === "" ? "–" : "₹" + Number(n).toLocaleString("en-IN"));
const ago = (iso) => { if (!iso) return "–"; const m = Math.round((Date.now() - new Date(iso)) / 60000); return m < 1 ? "just now" : m < 60 ? `${m}m ago` : `${Math.floor(m / 60)}h ${m % 60}m ago`; };
const time = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
let state = { view: "command", draftId: null, health: null, lastJob: null };

async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers = { "Content-Type": "application/json" }; delete o.json; }
  const r = await fetch(path, o);
  if (!r.ok) { let d = await r.text(); try { d = JSON.parse(d).detail || d; } catch {} throw new Error(d); }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}
function toast(msg, ms = 2800) { const t = $("#toast"); t.textContent = msg; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => (t.style.display = "none"), ms); }
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast("⚠ " + e.message, 5000); } };

/* ------------------------------------------------------------------ navigation */
document.querySelectorAll("#nav button[data-view]").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
function show(v) {
  state.view = v;
  if (v === "bots") api("/api/sim/state").then((s) => { $("#otpMode").value = s.config.otp_delivery; $("#flaky").value = s.config.flaky_rate; }).catch(() => {});
  document.querySelectorAll("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  $("#nav button.active")?.scrollIntoView({ block: "nearest", inline: "nearest" }); // keep the active tab visible on the phone tab strip
  document.querySelectorAll(".view").forEach((s) => s.classList.toggle("active", s.id === "view-" + v));
  refresh();
}

const STATUS_CHIP = { PREAUTH_SUBMITTED: "info", QUERY_RAISED: "warn", PREAUTH_APPROVED: "ok", PARTIALLY_APPROVED: "warn",
  ENHANCEMENT_REQUESTED: "info", DISCHARGE_SUBMITTED: "info", FINAL_APPROVED: "ok", DENIED: "bad", SETTLED: "ok", UNKNOWN: "bad", DRAFT: "" };
const statusChip = (s) => `<span class="chip ${STATUS_CHIP[s] ?? ""}">${esc(String(s).replaceAll("_", " ").toLowerCase())}</span>`;
const PORTAL = { nimbus: "Nimbus TPA", orbit: "Orbit Insurance", lotus: "Lotus Health TPA" };

function slaCell(sla) {
  if (!sla) return '<span class="muted small">–</span>';
  const m = Math.round(Math.abs(sla.minutes_left));
  const txt = sla.breached ? `${m} min overdue` : `${m} min left`;
  return `<div class="sla ${sla.level}">${txt}</div><div class="small muted">${esc(sla.owner === "hospital" ? "our reply" : "TPA decision")}</div>`;
}

/* ------------------------------------------------------------------ top status */
async function loadHealth() {
  const h = await api("/api/health"); state.health = h;
  const llm = h.llm.engine === "gemini" ? `<span class="pill ok"><span class="dot"></span>${esc(h.llm.model)} · ${esc(h.llm.mode)}</span>`
    : `<span class="pill warn" title="${esc(h.llm.reason)}"><span class="dot"></span>LLM: deterministic fallback</span>`;
  $("#topStatus").innerHTML = llm + `<span class="pill ok"><span class="dot"></span>${esc(h.db)}</span>` +
    `<span class="pill ${h.mock_portals_up ? "ok" : "bad"}"><span class="dot"></span>TPA portals ${h.mock_portals_up ? "reachable" : "offline"}</span>` +
    (h.sync_running ? `<span class="pill warn"><span class="spin"></span>bots running</span>` : "");
  $("#phoneLink").href = h.portal_base_url + "/dev/phone";
}

/* ------------------------------------------------------------------ command center */
async function loadCommand() {
  const [m, claims, alerts] = await Promise.all([api("/api/metrics"), api("/api/claims"), api("/api/alerts")]);
  const otpTotal = m.otp_auto + m.otp_human;
  $("#kpis").innerHTML = [
    ["Claims tracked", m.claims_total, `${m.runs_total} bot runs · ${m.runs_ok} ok`],
    ["Queries awaiting reply", m.open_queries, `${m.drafts_total} drafts auto-written`, m.open_queries ? "warn" : ""],
    ["IRDAI TAT breached", m.sla_breached, `${m.sla_at_risk} at risk`, m.sla_breached ? "bad" : ""],
    ["Awaiting decision", inr(m.pending_amount), "pre-auth / query / enhancement"],
    ["Approved so far", inr(m.approved_amount), "across portals"],
    ["OTPs handled", otpTotal ? `${m.otp_auto}/${otpTotal} auto` : "–", `${m.otp_human} needed a human`],
  ].map(([l, v, h, cls]) => `<div class="card kpi ${cls || ""}"><div class="label">${l}</div><div class="val">${v}</div><div class="hint">${h}</div></div>`).join("");

  $("#claimsTable").innerHTML = `<thead><tr><th>Claim</th><th>Patient</th><th>Portal</th><th>Status</th><th>Clock</th><th class="num">Requested</th><th class="num">Approved</th><th>Pre-submit score</th><th>Synced</th></tr></thead><tbody>` +
    (claims.map((c) => {
      const sc = c.completeness?.score ?? 0;
      return `<tr class="click" onclick="openClaim('${esc(c._id)}')"><td><b>${esc(c._id)}</b><div class="small muted mono">${esc(c.portal_ref || "not submitted")}</div></td>
      <td>${esc(c.fields?.patient_name)}<div class="small muted">${esc(c.fields?.provisional_diagnosis || "")}</div></td>
      <td class="small" data-label="Portal">${esc(PORTAL[c.portal] || c.portal || "–")}</td><td>${statusChip(c.status)}${c.status_mapping === "keyword" || c.status_mapping === "unknown" ? `<div class="small muted">raw: ${esc(c.status_raw)}</div>` : ""}</td>
      <td data-label="Clock">${slaCell(c.sla)}</td><td class="num" data-label="Requested">${inr(c.requested_amount)}</td><td class="num" data-label="Approved">${inr(c.approved_amount)}</td>
      <td><div class="bar ${sc < 50 ? "bad" : sc < 80 ? "warn" : ""}"><i style="width:${sc}%"></i></div><div class="small muted">${sc}/100 · ${c.completeness?.blockers?.length ?? 0} blockers</div></td>
      <td class="small muted nowrap">${ago(c.last_synced_at)}</td></tr>`;
    }).join("") || `<tr><td colspan="9" class="empty">No claims</td></tr>`) + "</tbody>";

  $("#alertCount").textContent = `${alerts.length} open`;
  $("#alerts").innerHTML = alerts.map((a) => `<div class="alert ${esc(a.severity)}"><div class="sev"></div><div style="flex:1;min-width:0">
      <div class="t">${esc(a.title)}</div><div class="d">${esc(a.detail)}</div>
      <div class="row small muted" style="margin-top:4px">${time(a.created_at)}${a.claim_id ? ` · <a href="#" onclick="openClaim('${esc(a.claim_id)}');return false">${esc(a.claim_id)}</a>` : ""}
      <span class="spacer"></span><button class="btn sm" onclick="resolveAlert('${esc(a._id)}')">Resolve</button></div></div></div>`).join("") || `<div class="empty">All clear ✓</div>`;
}
const resolveAlert = guard(async (id) => { await api(`/api/alerts/${id}/resolve`, { method: "POST" }); refresh(); });

/* ------------------------------------------------------------------ claim drawer */
const openClaim = guard(async (id) => {
  const { claim: c, events, drafts } = await api(`/api/claims/${id}`);
  const f = c.fields || {};
  const fieldOrder = ["patient_name", "age", "gender", "uhid", "insurer", "tpa", "policy_number", "member_id", "admission_date", "treating_doctor",
    "doctor_registration_no", "provisional_diagnosis", "icd10_code", "line_of_treatment", "proposed_procedure", "duration_of_ailment", "past_history",
    "alcohol_history", "room_type", "expected_los_days", "estimated_cost"];
  const comp = c.completeness || { blockers: [], warnings: [] };
  $("#drawer").innerHTML = `<header><div><b>${esc(c._id)}</b> ${statusChip(c.status)}<div class="small muted">${esc(PORTAL[c.portal] || "")} · <span class="mono">${esc(c.portal_ref || "")}</span></div></div>
    <span class="spacer"></span><div>${slaCell(c.sla)}</div><button class="btn sm" onclick="closeDrawer()" aria-label="Close">✕</button></header>
  <div class="body">
    ${c.query?.text ? `<div class="card"><h3>TPA query (fetched by bot)</h3><div class="preview">${esc(c.query.text)}</div>
      <div class="row" style="margin-top:10px"><button class="btn primary" onclick="draftFor('${esc(c._id)}')">✍ Draft reply</button>
      ${c.query.answered_at ? `<span class="chip ok">answered ${ago(c.query.answered_at)}</span>` : ""}</div></div>` : ""}
    <div class="card"><h3>Pre-auth data</h3><div class="kv">${fieldOrder.map((k) => `<div><span>${k.replaceAll("_", " ")}</span><b>${f[k] === null || f[k] === undefined || f[k] === "" ? '<span class="muted">–</span>' : esc(k === "estimated_cost" ? inr(f[k]) : f[k])}</b></div>`).join("")}</div></div>
    <div class="card"><div class="row"><h3 style="margin:0">Query-risk screen</h3><span class="chip ${comp.query_risk === "high" ? "bad" : comp.query_risk === "medium" ? "warn" : "ok"}">risk: ${esc(comp.query_risk)}</span>
      <span class="spacer"></span><span class="small muted">Documents: ${(c.documents || []).map((d) => `<span class="chip">${esc(d)}</span>`).join(" ") || "none"}</span></div>
      <div style="margin-top:10px">${findings(comp)}</div></div>
    <div class="card"><h3>Correspondence</h3>${drafts.map((d) => `<div class="row" style="padding:6px 0;border-bottom:1px solid var(--line)"><span class="chip brand">${esc(d.kind)}</span>
      ${safetyChip(d.safety.status)}<span class="small">${esc(d.subject)}</span><span class="spacer"></span><button class="btn sm" onclick="closeDrawer();openDraft('${esc(d._id)}')">Open</button></div>`).join("") || '<div class="muted small">None yet</div>'}</div>
    <div class="card"><h3>Audit trail</h3><ul class="timeline">${events.map((e) => `<li><b>${esc(e.kind.replaceAll("_", " "))}</b>
      ${e.data?.from ? `${esc(e.data.from)} → ${esc(e.data.to)} <span class="muted small">(portal said "${esc(e.data.raw)}")</span>` : esc(e.data?.status || e.data?.subject || (e.data?.types || []).join(", ") || "")}
      <span class="muted small"> · ${new Date(e.at).toLocaleString()}</span></li>`).join("")}</ul></div>
  </div>`;
  $("#drawer").classList.add("open"); $("#drawerBg").classList.add("open");
});
function closeDrawer() { $("#drawer").classList.remove("open"); $("#drawerBg").classList.remove("open"); }
const draftFor = guard(async (id) => { const d = await api(`/api/claims/${id}/draft-reply`, { method: "POST", json: {} }); closeDrawer(); show("drafts"); openDraft(d._id); });

function findings(comp) {
  const all = [...(comp.blockers || []), ...(comp.warnings || [])];
  if (!all.length) return '<div class="chip ok">No gaps found - ready to submit</div>';
  return all.map((x) => `<div class="finding ${esc(x.severity)}"><b>${esc(x.message)}</b>
    ${x.likely_tpa_query ? `<div class="q">TPA would ask: “${esc(x.likely_tpa_query)}”</div>` : ""}${x.fix ? `<div class="fx">Fix: ${esc(x.fix)}</div>` : ""}</div>`).join("");
}
const safetyChip = (s) => `<span class="chip ${s === "ready_to_send" ? "ok" : s === "blocked" ? "bad" : "warn"}">${esc(String(s).replaceAll("_", " "))}</span>`;

/* ------------------------------------------------------------------ bots */
async function loadBots() {
  const [st, otps] = await Promise.all([api("/api/sync/status"), api("/api/otp/pending")]);
  $("#syncState").innerHTML = st.running ? '<span class="spin"></span> bots running…' : "";
  const openRuns = new Set([...document.querySelectorAll("#runs details[open]")].map((d) => d.dataset.run));
  const otpKey = otps.map((o) => o._id).join();
  if (otpKey !== state.otpKey) { state.otpKey = otpKey; $("#otpQueue").innerHTML = otps.map((o) => `<div class="otp-card"><div class="row"><b>🔐 ${esc(PORTAL[o.portal] || o.portal)} needs an OTP</b>
      <span class="muted small">sent to ${esc(o.phone_masked)} · expires ${time(o.expires_at)}</span></div>
      <div class="row" style="margin-top:8px"><input id="otp-${esc(o._id)}" maxlength="6" placeholder="••••••" inputmode="numeric">
      <button class="btn primary" onclick="sendOtp('${esc(o._id)}')">Submit OTP</button><button class="btn" onclick="skipOtp('${esc(o._id)}')">Skip → manual review</button>
      <a class="small" target="_blank" href="${esc(state.health?.portal_base_url)}/dev/phone">open desk phone</a></div></div>`).join("");
    if (otps.length) $("#otp-" + otps[0]._id)?.focus(); }
  $("#runs").innerHTML = st.runs.map((r) => {
    const cls = r.status === "success" ? "ok" : r.status === "running" ? "info" : r.status === "needs_manual_review" ? "warn" : "bad";
    return `<div class="card"><div class="row"><b>${esc(PORTAL[r.portal] || r.portal)}</b><span class="chip ${cls}">${esc(r.status.replaceAll("_", " "))}</span>
      <span class="small muted">${new Date(r.started_at).toLocaleTimeString()} · ${esc(r.trigger)}${r.duration_ms ? ` · ${(r.duration_ms / 1000).toFixed(1)}s` : ""}</span><span class="spacer"></span>
      ${r.claims_seen !== undefined ? `<span class="small">${r.claims_seen} claims read · <b>${(r.changes || []).length} status changes</b></span>` : ""}
      ${r.screenshot ? `<a class="small" target="_blank" href="/api/runs/${esc(r._id)}/screenshot">📸 failure screenshot</a>` : ""}</div>
      ${r.error ? `<div class="small" style="color:var(--bad);margin-top:6px">${esc(r.error)}</div>` : ""}
      ${(r.steps || []).length ? `<details data-run="${esc(r._id)}" ${r.status === "running" || openRuns.has(r._id) ? "open" : ""}><summary>${r.steps.length} steps</summary><div class="steps">${r.steps.map((s) => `<div><span class="ts">${time(s.at)}</span>${esc(s.msg)}</div>`).join("")}</div></details>` : '<div class="small muted" style="margin-top:6px"><span class="spin"></span> launching browser…</div>'}</div>`;
  }).join("") || '<div class="card empty">No runs yet - press “Sync all portals”.</div>';
}
const startSync = guard(async (portals) => { const r = await api("/api/sync", { method: "POST", json: { portals } }); toast(r.started ? "Bots started" : "A sync is already running"); if (state.view === "command") show("bots"); else refresh(); });
const sendOtp = guard(async (id) => { await api(`/api/otp/${id}`, { method: "POST", json: { otp: $("#otp-" + id).value } }); toast("OTP handed to the bot"); refresh(); });
const skipOtp = guard(async (id) => { await api(`/api/otp/${id}`, { method: "POST", json: { skip: true } }); refresh(); });
const simAdvance = guard(async () => { const r = await api("/api/sim/advance", { method: "POST" }); toast(`TPAs moved ${r.moved.length} claims. Run the bots to pick it up.`); });
const simConfig = guard(async () => { await api("/api/sim/config", { method: "POST", json: { otp_delivery: $("#otpMode").value, flaky_rate: Number($("#flaky").value) } }); toast("Simulator updated"); });
const simReset = guard(async () => { if (!confirm("Reset portals and claim store to the seeded demo state?")) return; await api("/api/sim/reset", { method: "POST" }); toast("Demo reset"); refresh(); });

/* ------------------------------------------------------------------ intake */
const FIELD_ORDER = ["patient_name", "age", "gender", "uhid", "patient_phone", "insurer", "tpa", "policy_number", "member_id", "admission_date", "admission_type",
  "treating_doctor", "doctor_registration_no", "provisional_diagnosis", "icd10_code", "line_of_treatment", "proposed_procedure", "duration_of_ailment",
  "past_history", "alcohol_history", "room_type", "expected_los_days", "estimated_cost", "is_rta"];
let lastExtract = null;
function wireDrop(dropId, inputId, onFiles) {
  const d = $(dropId), i = $(inputId);
  d.addEventListener("click", () => i.click());
  i.addEventListener("change", () => i.files.length && onFiles([...i.files]));
  d.addEventListener("dragover", (e) => { e.preventDefault(); d.classList.add("over"); });
  d.addEventListener("dragleave", () => d.classList.remove("over"));
  d.addEventListener("drop", (e) => { e.preventDefault(); d.classList.remove("over"); onFiles([...e.dataTransfer.files]); });
}
const runExtract = guard(async (files) => {
  const fd = new FormData(); files.forEach((f) => fd.append("files", f)); fd.append("stage", $("#stageSel").value);
  if ($("#siIn").value) fd.append("sum_insured", $("#siIn").value);
  $("#extractCard").style.display = "block"; $("#extractCard").innerHTML = '<div class="empty"><span class="spin"></span> Extracting…</div>';
  lastExtract = await api("/api/extract", { method: "POST", body: fd });
  renderExtract();
});
const extractSample = guard(async () => { const n = $("#sampleSel").value; const b = await (await fetch("/samples/documents/" + encodeURIComponent(n))).blob(); runExtract([new File([b], n)]); });
function renderExtract() {
  const { extraction: x, completeness: comp, files } = lastExtract;
  const g = (k) => x.grounding[k] === true ? '<span class="gnd y" title="value found in the source document">✓ in doc</span>' : x.grounding[k] === false
    ? `<span class="gnd n" title="not found in the document - verify">${k === "icd10_code" ? "⚠ inferred" : "⚠ verify"}</span>` : '<span class="gnd u">–</span>';
  $("#extractCard").innerHTML = `<div class="row"><h2 style="margin:0">Extracted pre-auth fields</h2><span class="chip brand">${esc(x.engine)}</span>
      <span class="chip">fill ${Math.round(x.fill_rate * 100)}%</span>${x.needs_human_review.length ? `<span class="chip bad">${x.needs_human_review.length} need review</span>` : '<span class="chip ok">all values grounded</span>'}
      <span class="spacer"></span>${files.map((f) => `<span class="chip">${esc(f.filename)} → ${esc(f.doc_type)}</span>`).join(" ")}</div>
    <div class="scroll" style="margin-top:12px"><table><thead><tr><th>Field</th><th>Value (editable)</th><th>Grounding</th></tr></thead><tbody>
    ${FIELD_ORDER.map((k) => `<tr><td class="small">${k.replaceAll("_", " ")}</td><td><input type="text" data-k="${k}" value="${esc(x.fields[k] ?? "")}" onchange="recheck()"></td><td>${g(k)}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="row" style="margin-top:12px"><select id="newPortal" style="width:auto"><option value="">Portal…</option>${Object.entries(PORTAL).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
      <input type="text" id="newRef" placeholder="portal claim ref (optional)" style="width:220px"><span class="spacer"></span>
      <button class="btn primary" onclick="createClaim()">Save as claim</button></div>`;
  renderComp(comp);
}
function editedFields() {
  const f = {};
  document.querySelectorAll("#extractCard input[data-k]").forEach((i) => {
    const k = i.dataset.k, v = i.value.trim();
    f[k] = v === "" ? null : ["age", "expected_los_days", "estimated_cost"].includes(k) ? Number(v.replace(/[^\d]/g, "")) || null : k === "is_rta" ? v === "true" : v;
  });
  return f;
}
function renderComp(comp) {
  $("#compCard").innerHTML = `<div class="row"><h2 style="margin:0">Query-risk screen</h2><span class="spacer"></span>
    <span class="chip ${comp.ready_to_submit ? "ok" : "bad"}">${comp.ready_to_submit ? "ready to submit" : "not ready"}</span>
    <span class="chip ${comp.query_risk === "high" ? "bad" : comp.query_risk === "medium" ? "warn" : "ok"}">query risk: ${esc(comp.query_risk)}</span><b>${comp.score}/100</b></div>
    <div style="margin-top:12px">${findings(comp)}</div>`;
}
const recheck = guard(async () => { renderComp(await api("/api/completeness", { method: "POST", json: { fields: editedFields(), documents: lastExtract.documents, stage: $("#stageSel").value, sum_insured: Number($("#siIn").value) || null } })); });
const createClaim = guard(async () => {
  const c = await api("/api/claims", { method: "POST", json: { fields: editedFields(), documents: lastExtract.documents, portal: $("#newPortal").value || null,
    portal_ref: $("#newRef").value.trim() || null, stage: $("#stageSel").value, sum_insured: Number($("#siIn").value) || null } });
  toast(`Saved ${c._id}`); show("command"); openClaim(c._id);
});

/* ------------------------------------------------------------------ drafts */
async function loadDrafts() {
  const ds = await api("/api/drafts");
  $("#draftList").innerHTML = ds.map((d) => `<div class="alert" style="cursor:pointer;${d._id === state.draftId ? "background:var(--brand-soft)" : ""}" onclick="openDraft('${esc(d._id)}')">
     <div style="flex:1;min-width:0"><div class="row"><span class="chip brand">${esc(d.kind.replaceAll("_", " "))}</span>${safetyChip(d.safety.status)}${d.sent ? '<span class="chip ok">sent</span>' : ""}</div>
     <div class="small" style="margin-top:4px">${esc(d.subject)}</div><div class="small muted">${esc(d.claim_id)} · ${esc(d.engine)} · ${ago(d.created_at)}</div></div></div>`).join("") || '<div class="empty">No drafts yet - run the bots; queries are drafted automatically.</div>';
  if (state.draftId) { const d = ds.find((x) => x._id === state.draftId); if (d) renderDraft(d); }
}
const openDraft = guard(async (id) => { state.draftId = id; if (state.view !== "drafts") show("drafts"); else loadDrafts(); });
function highlight(t) { return esc(t).replace(/\[\[[^\]]+\]\]/g, (m) => `<span class="ph">${m}</span>`); }
function renderDraft(d) {
  const s = d.safety;
  $("#draftView").innerHTML = `<div class="row"><h2 style="margin:0">${esc(d.subject)}</h2></div>
    <div class="row small muted" style="margin:6px 0 12px">${esc(d.claim_id)} · engine: ${esc(d.engine)} · ${safetyChip(s.status)} ${s.placeholders.length ? `· ${s.placeholders.length} placeholder(s) to fill` : ""}</div>
    ${s.rejected_llm_draft ? `<div class="finding blocker"><b>The LLM draft was rejected by the safety gate</b> and replaced by the deterministic composer.<div class="fx">${s.rejected_llm_draft.issues.map((i) => esc(i.detail)).join("<br>")}</div></div>` : ""}
    ${s.issues.length ? s.issues.map((i) => `<div class="finding blocker"><b>${esc(i.type)}</b><div class="fx">${esc(i.detail)}</div></div>`).join("") : ""}
    ${d.asks?.length ? `<h3>Point-wise resolution</h3><table style="margin-bottom:12px"><thead><tr><th>TPA asked</th><th>How</th><th>Response</th></tr></thead><tbody>
      ${d.asks.map((a) => `<tr><td class="small">${esc(a.ask)}</td><td><span class="chip ${a.resolution === "attach" || a.resolution === "answer" ? "ok" : "warn"}">${esc(a.resolution.replaceAll("_", " "))}</span></td><td class="small">${highlight(a.response)}</td></tr>`).join("")}</tbody></table>` : ""}
    <h3>Preview</h3><div class="preview" id="draftPreview">${highlight(d.final_body || d.body)}</div>
    <h3 style="margin-top:12px">Edit &amp; send</h3><textarea id="draftBody" style="min-height:220px" oninput="$('#draftPreview').innerHTML=highlight(this.value)">${esc(d.final_body || d.body)}</textarea>
    <div class="row end" style="margin-top:10px"><span class="small muted">Sending is simulated (would go via Gmail/SES or the portal's reply box).</span>
      <button class="btn primary" ${d.sent || s.status === "blocked" ? "disabled" : ""} onclick="markSent('${esc(d._id)}')">${d.sent ? "Sent ✓" : "Mark as sent"}</button></div>`;
}
const markSent = guard(async (id) => { await api(`/api/drafts/${id}/mark-sent`, { method: "POST", json: { body: $("#draftBody").value } }); toast("Marked as sent - claim's query clock stopped"); loadDrafts(); });

/* ------------------------------------------------------------------ receivables */
const RCV_STATUS = { PENDING: ["info", "pending"], QUERY: ["warn", "query raised"], APPROVED: ["brand", "approved, unpaid"], PART_PAID: ["warn", "part paid"],
  PAID: ["ok", "paid"], DENIED: ["bad", "denied"] };
const rcvChip = (s) => { const [c, t] = RCV_STATUS[s] || ["", s]; return `<span class="chip ${c}">${esc(t)}</span>`; };
const SOURCE = { portal_bot: ["brand", "🤖 bot"], csv: ["", "CSV"], manual: ["warn", "manual"] };
const srcChip = (s) => { const [c, t] = SOURCE[s] || ["", s]; return `<span class="chip ${c}">${esc(t)}</span>`; };
const lakh = (n) => (n >= 1e7 ? `₹${(n / 1e7).toFixed(2)} Cr` : n >= 1e5 ? `₹${(n / 1e5).toFixed(1)} L` : inr(n));
function freshCell(f) {
  if (!f || !f.at) return '<span class="chip bad">no data yet</span>';
  const how = f.how === "portal bots" ? "🤖 bots" : "CSV";
  return `<span class="small ${f.stale ? "" : "muted"}" title="${esc(f.detail || "")}">${f.stale ? '<span class="chip bad">⚠ stale</span> ' : ""}${how} · ${ago(f.at)}</span>`;
}
function heat(v, n, max, label) {
  if (!v) return `<td class="heat muted">–</td>`;
  const pct = Math.round(8 + 34 * (v / max));
  return `<td class="heat" style="background:color-mix(in srgb, var(--brand) ${pct}%, transparent)" title="${esc(label)}: ${inr(v)} on ${n} claim(s)">${lakh(v)}<div class="small muted">${n}</div></td>`;
}
function reasonList(title, rows, hint) {
  if (!rows.length) return `<h3>${title}</h3><div class="muted small">None</div>`;
  const max = Math.max(...rows.map((r) => r.amount));
  return `<h3>${title} <span class="muted small">${hint}</span></h3>` + rows.map((r) => `<div class="reason" title="${esc(r.category)}: ${inr(r.amount)} on ${r.count} claim(s)">
    <span>${esc(r.category)}</span><div class="bar"><i style="width:${Math.max(4, Math.round(100 * r.amount / max))}%"></i></div><span class="num">${inr(r.amount)}</span></div>`).join("");
}
async function loadReceivables() {
  const [s, wa, box] = await Promise.all([api("/api/receivables/summary"), api("/api/receivables/whatsapp/preview"), api("/api/receivables/whatsapp/outbox")]);
  const t = s.totals, ch = s.changes;
  $("#rcvKpis").innerHTML = [
    ["Outstanding", inr(t.outstanding), `${t.count} unpaid claims`],
    ["Over 90 days", inr(t.over_90), `${s.over_90_pct}% of outstanding`, s.over_90_pct >= 40 ? "bad" : s.over_90_pct >= 20 ? "warn" : ""],
    ["Received since last summary", inr(ch.received_amount), `${ch.received_count} claim(s) · since ${ago(ch.since)}`],
    ["Deductions", inr(t.deductions), `${s.deduction_pct}% of billed · ${t.deduction_count} claims`, t.deductions ? "warn" : ""],
    ["Denied", inr(t.denied_amount), `${t.denied_count} claims`, t.denied_count ? "bad" : ""],
  ].map(([l, v, h, cls]) => `<div class="card kpi ${cls || ""}"><div class="label">${l}</div><div class="val">${v}</div><div class="hint">${h}</div></div>`).join("");

  const cells = [...s.by_payer_type, ...s.by_payer].flatMap((p) => s.buckets.map((b) => p.buckets[b]));
  const max = Math.max(1, ...cells);
  const row = (p, name, cls, fresh) => `<tr class="click ${cls}" onclick="rcvFilterTo('${esc(p.payer_type)}')"><td>${name}${fresh ? `<div class="fresh-m">${fresh}</div>` : ""}</td><td>${fresh}</td>
    ${s.buckets.map((b) => heat(p.buckets[b], p.bucket_counts[b], max, `${p.payer || p.label} · ${b} days`)).join("")}
    <td class="num"><b>${inr(p.outstanding)}</b><div class="small muted">${p.count} claim${p.count === 1 ? "" : "s"}${p.queries ? ` · ${p.queries} query` : ""}</div></td>
    <td class="num">${p.deductions ? inr(p.deductions) : "–"}</td><td class="num">${p.denied_count ? `${inr(p.denied_amount)}<div class="small muted">${p.denied_count}</div>` : "–"}</td></tr>`;
  let body = "";
  for (const g of s.by_payer_type) {
    const subs = s.by_payer.filter((p) => p.payer_type === g.payer_type);
    const many = subs.length > 1;
    body += row(g, `<b>${esc(g.label)}</b>${many ? "" : subs[0] && subs[0].payer !== g.label ? `<div class="small muted">${esc(subs[0].payer)}</div>` : ""}`, many ? "grp" : "", freshCell(g.freshness));
    if (many) body += subs.map((p) => row(p, esc(p.payer), "sub", "")).join("");
  }
  body += `<tr class="total"><td>Total</td><td></td>${s.buckets.map((b) => `<td class="num">${lakh(t.buckets[b])}</td>`).join("")}
    <td class="num">${inr(t.outstanding)}</td><td class="num">${inr(t.deductions)}</td><td class="num">${inr(t.denied_amount)}</td></tr>`;
  $("#agingTable").innerHTML = `<thead><tr><th>Payer</th><th>Data as of</th>${s.buckets.map((b) => `<th class="num">${b} d</th>`).join("")}<th class="num">Outstanding</th><th class="num">Deductions</th><th class="num">Denied</th></tr></thead><tbody>${body}</tbody>`;
  $("#dedReasons").innerHTML = reasonList("Why money was deducted", s.top_deductions, "(billed − approved/paid)");
  $("#denReasons").innerHTML = reasonList("Why claims were denied", s.top_denials, "");

  const md = (x) => esc(x).replace(/\*([^*\n]+)\*/g, "<b>$1</b>").replace(/(^|\s)_([^_\n]+)_/g, "$1<i>$2</i>");
  const m = wa.mode;
  $("#waCard").innerHTML = `<div class="row"><h2 style="margin:0">WhatsApp summary</h2><span class="spacer"></span>
      <span class="chip ${m.live ? "ok" : "warn"}" title="${m.live ? "" : "Set WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_OWNER_TO to send for real"}">${m.live ? "live · Cloud API" : "dry run · outbox only"}</span></div>
    <div class="small muted" style="margin:6px 0 10px">To ${esc(m.to || "owner (number not set)")} · ${m.schedule_ist ? `daily at ${esc(m.schedule_ist)} IST` : "no schedule set (RECEIVABLES_SUMMARY_AT)"}</div>
    <div class="wa-phone"><div class="wa-head"><div class="av">✚</div><b>HextGen</b><span class="muted">· preview of today's message</span></div>
      <div class="wa-bubble">${md(wa.text)}<div class="meta">${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</div></div></div>
    <div class="row" style="margin-top:10px"><button class="btn primary" onclick="waSend()">Send now</button><span class="spacer"></span><a class="small" href="#" onclick="loadReceivables();return false">↻ refresh</a></div>
    <details style="margin-top:10px"><summary>What is actually sent (approved template <span class="mono">${esc(wa.template.name)}</span>)</summary>
      <p class="small muted">A daily message is business-initiated, so WhatsApp only delivers it as a pre-approved template, and template values can't contain line breaks.
        The template carries the headline numbers below; the full breakdown above is in the dashboard and in a reply once the owner messages back.</p>
      <table class="small"><tbody>${wa.params.map((p, i) => `<tr><td class="mono">{{${i + 1}}}</td><td>${esc(p)}</td></tr>`).join("")}</tbody></table></details>
    <h3 style="margin-top:14px">Outbox</h3>${box.map((d) => `<div class="row small" style="padding:5px 0;border-bottom:1px solid var(--line)">
      <span class="chip ${d.status === "sent" ? "ok" : d.status === "failed" ? "bad" : "warn"}">${esc(d.status.replace("_", " "))}</span>
      <span>${esc(d.summary_date)} · ${esc(d.trigger)}</span><span class="spacer"></span><span class="muted" title="${esc(d.detail || "")}">${ago(d.created_at)}</span></div>`).join("") || '<div class="muted small">Nothing sent yet.</div>'}`;
  await loadRcvList();
}
async function loadRcvList() {
  const q = new URLSearchParams(); const pt = $("#rcvFilter").value; if (pt) q.set("payer_type", pt); if ($("#rcvOpen").checked) q.set("open_only", "true");
  const rows = await api("/api/receivables?" + q);
  $("#rcvTable").innerHTML = `<thead><tr><th>Claim</th><th>Patient</th><th>Payer</th><th>Status</th><th class="num">Age</th><th class="num">Billed</th><th class="num">Approved</th><th class="num">Paid</th><th class="num">Outstanding</th><th>Deduction / denial / query</th></tr></thead><tbody>` +
    (rows.map((r) => `<tr><td class="nowrap"><b class="mono">${esc(r.claim_ref)}</b><div>${srcChip(r.source)}${r.claim_id ? ` <a class="small" href="#" onclick="show('command');openClaim('${esc(r.claim_id)}');return false">${esc(r.claim_id)}</a>` : ""}</div></td>
      <td>${esc(r.patient_name || "–")}<div class="small muted">${esc(r.package || "")}</div></td><td class="small" data-label="Payer">${esc(r.payer)}</td>
      <td>${rcvChip(r.status)}${r.status_raw ? `<div class="small muted">${esc(r.status_raw)}</div>` : ""}</td>
      <td class="num" data-label="Age">${r.age_days ?? "–"}${r.age_days !== null ? '<div class="small muted">days</div>' : ""}</td>
      <td class="num" data-label="Billed">${inr(r.billed)}</td><td class="num" data-label="Approved">${inr(r.approved)}</td><td class="num" data-label="Paid">${inr(r.paid)}${r.tds ? `<div class="small muted">+ TDS ${inr(r.tds)}</div>` : ""}</td>
      <td class="num" data-label="Outstanding"><b>${r.outstanding ? inr(r.outstanding) : "–"}</b></td>
      <td class="small">${r.deduction ? `<b>${inr(r.deduction)}</b> ` : r.denied_amount ? `<b>${inr(r.denied_amount)}</b> ` : ""}${r.reason_category ? `<span class="chip">${esc(r.reason_category)}</span>` : ""}<div class="muted">${esc(r.remarks || "")}</div></td></tr>`).join("")
      || `<tr><td colspan="10" class="empty">No claims</td></tr>`) + "</tbody>";
}
function rcvFilterTo(pt) { $("#rcvFilter").value = pt; loadRcvList(); $("#rcvTable").scrollIntoView({ behavior: "smooth", block: "start" }); }
function renderImport(reps) {
  const KIND = { payment: ["ok", "paid"], denied: ["bad", "denied"], deduction: ["warn", "deduction"], new: ["info", "new"], query: ["warn", "query"], status: ["", "status"] };
  $("#rcvImport").innerHTML = reps.map((r) => `<div class="finding"><div class="row"><b>${esc(r.filename)}</b><span class="chip brand">${esc(r.payer_label)}</span><span class="spacer"></span>
      <span class="small">${r.created} new · ${r.updated} updated · ${r.unchanged} unchanged${r.skipped ? ` · <span style="color:var(--bad)">${r.skipped} skipped</span>` : ""}</span></div>
    ${r.events.length ? `<table class="small" style="margin-top:8px"><tbody>${r.events.map((e) => { const [c, t] = KIND[e.kind] || ["", e.kind];
      return `<tr><td><span class="chip ${c}">${t}</span></td><td class="mono">${esc(e.claim_ref)}</td><td>${esc(e.patient_name || "")}</td><td class="num">${e.amount ? inr(e.amount) : ""}</td></tr>`; }).join("")}</tbody></table>` : '<div class="small muted" style="margin-top:6px">No changes since the last upload.</div>'}
    ${r.issues.length ? `<details style="margin-top:6px"><summary>${r.issues.length} row issue(s)</summary>${r.issues.map((i) => `<div class="small">Row ${esc(i.row)}${i.ref ? ` (${esc(i.ref)})` : ""}: ${esc(i.message)}</div>`).join("")}</details>` : ""}
    <details style="margin-top:6px"><summary>Column mapping</summary><div class="small">${r.mapping.map((m) => `<span class="mono">${esc(m.source)}</span> → ${m.field ? esc(m.field) : '<span class="muted">ignored</span>'}`).join(" · ")}</div></details></div>`).join("");
}
const rcvUpload = guard(async (files) => {
  const fd = new FormData(); files.forEach((f) => fd.append("files", f)); fd.append("payer_type", $("#rcvPayer").value);
  $("#rcvImport").innerHTML = '<div class="empty"><span class="spin"></span> Importing…</div>';
  try { renderImport(await api("/api/receivables/import", { method: "POST", body: fd })); } catch (e) { $("#rcvImport").innerHTML = ""; throw e; }
  loadReceivables();
});
const rcvSampleToday = guard(async () => { renderImport([await api("/api/receivables/sample-today", { method: "POST" })]); toast("Imported today's Aarogyasri export"); loadReceivables(); });
const rcvManual = guard(async () => {
  const p = {}; document.querySelectorAll("#rcvForm [name]").forEach((i) => { if (i.value !== "") p[i.name] = i.value; });
  const r = await api("/api/receivables", { method: "POST", json: p });
  toast(`${r.created ? "Added" : "Updated"} ${r.receivable.claim_ref}`); document.querySelectorAll("#rcvForm input").forEach((i) => (i.value = "")); loadReceivables();
});
const waSend = guard(async () => { const d = await api("/api/receivables/whatsapp/send", { method: "POST" }); toast(d.status === "sent" ? "Summary sent on WhatsApp" : d.status === "dry_run" ? "Dry run: written to the outbox (WhatsApp not configured)" : "⚠ " + d.detail, 5000); loadReceivables(); });

/* ------------------------------------------------------------------ onboarding */
const runOnboardingSample = guard(async () => { $("#obResult").innerHTML = '<div class="card empty"><span class="spin"></span> Processing…</div>'; renderJob(await api("/api/onboarding/sample", { method: "POST" })); });
const runOnboarding = guard(async (files) => {
  const fd = new FormData(); files.forEach((f) => fd.append("files", f)); fd.append("profile", $("#obProfile").value || "{}");
  $("#obResult").innerHTML = '<div class="card empty"><span class="spin"></span> Processing…</div>';
  renderJob(await api("/api/onboarding/jobs", { method: "POST", body: fd }));
});
const VERDICT = { go: ["ok", "GO - ready for go-live"], on_track: ["info", "On track"], at_risk: ["warn", "At risk"], will_miss: ["bad", "Will miss go-live"], no_go: ["bad", "No-go"] };
function renderJob(j) {
  state.lastJob = j;
  const r = j.readiness, [vc, vt] = VERDICT[r.verdict] || ["", r.verdict];
  const color = r.score >= 85 ? "var(--ok)" : r.score >= 60 ? "#e8a33c" : "var(--bad)";
  const areas = [...new Set(r.checks.map((c) => c.area))];
  $("#obResult").innerHTML = `<div class="grid g-2">
    <div class="card"><div class="gauge"><div class="ring" style="--p:${r.score};--c:${color}"><div>${r.score}%</div></div>
      <div><div class="chip ${vc}" style="font-size:13px">${esc(vt)}</div><h2 style="margin:8px 0 4px">${esc(j.hospital || "")}</h2>
      <div class="small">${r.blockers} blockers · ${r.warnings} warnings · job ${esc(j._id)}</div>
      <div class="small muted">ETA to ready: <b>${r.eta_hours} h</b>${r.hours_to_go_live !== null ? ` · go-live in <b>${r.hours_to_go_live} h</b>` : ""}</div>
      <div class="row" style="margin-top:10px"><a class="btn sm" href="/api/onboarding/jobs/${esc(j._id)}/issues.csv">⬇ Row-wise fix list (CSV)</a>
      ${j.files.map((f) => `<a class="btn sm" href="/api/onboarding/jobs/${esc(j._id)}/clean/${esc(f.entity)}.csv">⬇ ${esc(f.entity)} import-ready</a>`).join("")}</div></div></div>
      <div style="margin-top:14px">${areas.map((a) => `<h3 style="margin-top:12px">${esc(a)}</h3>` + r.checks.filter((c) => c.area === a).map((c) => `<div class="check ${c.status}">
        <div class="ic">${c.status === "pass" ? "✓" : c.status === "fail" ? "✕" : "!"}</div><div style="flex:1"><div>${esc(c.title)}</div>
        <div class="small muted">${esc(c.detail)}${c.status !== "pass" ? ` · owner: ${esc(c.owner)}` : ""}${c.external_wait_h ? ` · external wait ~${c.external_wait_h}h` : ""}</div></div></div>`).join("")).join("")}</div></div>
    <div class="card"><div class="row"><h2 style="margin:0">Message to hospital admin</h2><span class="chip brand">${esc(j.message.engine)}</span></div>
      <div class="small muted" style="margin:6px 0">${esc(j.message.subject)}</div><div class="preview">${esc(j.message.body)}</div>
      <div class="small muted" style="margin-top:8px">Ready for WhatsApp Business API / email; nothing is sent automatically.</div></div></div>
  ${j.files.map((f) => {
    const v = f.validation;
    return `<div class="card" style="margin-top:16px"><div class="row"><h2 style="margin:0">${esc(f.filename)}</h2><span class="chip brand">${esc(f.label)}</span>
      <span class="chip">detected with ${Math.round(f.detection_confidence * 100)}% confidence</span><span class="chip">mapping: ${esc(f.mapping_engine)}</span><span class="spacer"></span>
      <span class="small"><b>${v.rows_clean}</b>/${v.rows_total} rows import-ready · <span style="color:var(--bad)">${v.rows_blocked} blocked</span></span></div>
      <div class="grid g-2" style="margin-top:12px"><div><h3>Column mapping</h3><table><thead><tr><th>Legacy column</th><th>→ Field</th><th>Confidence</th></tr></thead><tbody>
        ${f.mapping.map((m) => `<tr><td class="mono">${esc(m.source)}</td><td>${m.target ? esc(m.target) : '<span class="muted">ignored</span>'}${m.needs_confirmation ? ' <span class="chip warn">confirm</span>' : ""}</td>
        <td><div class="bar ${m.confidence < 0.6 ? "warn" : ""}"><i style="width:${Math.round(m.confidence * 100)}%"></i></div></td></tr>`).join("")}</tbody></table></div>
      <div><h3>Issues</h3>${v.summary.map((s) => `<div class="finding ${esc(s.severity)}"><b>${s.count} × ${esc(s.message)}</b><div class="fx">${esc(s.fix)}</div></div>`).join("") || '<div class="chip ok">No issues</div>'}
        ${v.issues.length ? `<details><summary>All ${v.issues.length} row-level issues</summary><table class="small"><thead><tr><th>Row</th><th>Field</th><th>Value</th><th>Problem</th></tr></thead><tbody>
        ${v.issues.map((i) => `<tr><td>${esc(i.row ?? "–")}</td><td>${esc(i.field)}</td><td class="mono">${esc(i.value ?? "")}</td><td>${esc(i.message)}</td></tr>`).join("")}</tbody></table></details>` : ""}</div></div></div>`;
  }).join("")}`;
}

/* ------------------------------------------------------------------ boot + polling */
async function refresh() {
  try {
    await loadHealth();
    if (state.view === "command") await loadCommand();
    else if (state.view === "bots") await loadBots();
    else if (state.view === "drafts") await loadDrafts();
    else if (state.view === "receivables") await loadReceivables();
  } catch (e) { console.error(e); }
}
(async function boot() {
  wireDrop("#drop", "#fileIn", runExtract);
  wireDrop("#obDrop", "#obIn", runOnboarding);
  wireDrop("#rcvDrop", "#rcvIn", rcvUpload);
  const samples = await api("/api/samples/documents");
  $("#sampleSel").innerHTML = samples.map((s) => `<option>${esc(s)}</option>`).join("");
  $("#obProfile").value = JSON.stringify(await api("/api/onboarding/sample-profile"), null, 2);
  const hv = location.hash.slice(1);
  if (hv && $("#view-" + hv)) show(hv); else refresh();
  setInterval(() => { if (["command", "bots"].includes(state.view) && !document.hidden && !$("#drawer").classList.contains("open")) refresh(); }, 3000);
})();
