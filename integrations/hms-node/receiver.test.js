import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";

import { admissionToClaim } from "./claimpilot-client.js";
import { handle, verify } from "./webhook-receiver.js";

test("signature: accepts ClaimPilot's HMAC, rejects tampering and missing header", () => {
  const body = Buffer.from('{"kind":"status_changed"}');
  const sig = "sha256=" + createHmac("sha256", "k").update(body).digest("hex");
  assert.equal(verify(body, sig, "k"), true);
  assert.equal(verify(Buffer.from('{"kind":"x"}'), sig, "k"), false);
  assert.equal(verify(body, undefined, "k"), false);
});

test("status events map to IPD billing actions", () => {
  const msg = handle({ kind: "status_changed", hms_ref: "IP-1", status: "DENIED", data: { from: "QUERY_RAISED" } });
  assert.match(msg, /IP-1: QUERY_RAISED -> DENIED => switch the IP bill to self-pay/);
});

test("admission maps to a claim keyed by IP number", () => {
  const c = admissionToClaim({
    ipNumber: "IP-9", hospitalName: "H", admittedOn: "2026-11-05", provisionalDiagnosis: "Dengue", estimatedCost: 40000,
    patient: { name: "A", age: 30, gender: "M" }, doctor: { name: "Dr B" },
    insurance: { portal: "orbit", policyNumber: "P", tpaName: "Orbit", sumInsured: 200000 },
  });
  assert.equal(c.hms_ref, "IP-9");
  assert.equal(c.portal, "orbit");
  assert.equal(c.fields.estimated_cost, 40000);
});
