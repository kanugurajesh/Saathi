// HMS -> ClaimPilot: what HextGen's IPD module would call when an insured patient is admitted.
// Zero dependencies (Node 18+ global fetch). Safe to retry: the IP admission number is the idempotency key.
//
//   CLAIMPILOT_URL=http://127.0.0.1:8000 node claimpilot-client.js

const BASE = process.env.CLAIMPILOT_URL || "http://127.0.0.1:8000";

async function call(method, path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(5000),
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${method} ${path} -> HTTP ${res.status}: ${await res.text()}`);
  return res.json();
}

/** Map an HMS IP admission (shape is illustrative) to a ClaimPilot pre-auth claim. */
export function admissionToClaim(adm) {
  return {
    hms_ref: adm.ipNumber,
    hospital: adm.hospitalName,
    portal: adm.insurance.portal, // which TPA portal the bots should watch, e.g. "nimbus"
    portal_ref: adm.insurance.preauthRef || null, // set once the desk submits on the portal
    sum_insured: adm.insurance.sumInsured,
    stage: "preauth",
    fields: {
      // field names follow the pre-auth schema in app/claims/extraction.py
      patient_name: adm.patient.name,
      age: adm.patient.age,
      gender: adm.patient.gender,
      uhid: adm.patient.uhid,
      policy_number: adm.insurance.policyNumber,
      member_id: adm.insurance.memberId,
      tpa: adm.insurance.tpaName,
      treating_doctor: adm.doctor.name,
      doctor_registration_no: adm.doctor.registrationNo,
      admission_date: adm.admittedOn,
      admission_type: adm.admissionType,
      room_type: adm.roomType,
      provisional_diagnosis: adm.provisionalDiagnosis,
      estimated_cost: adm.estimatedCost,
    },
    documents: adm.uploadedDocuments || [],
  };
}

export const createClaimFromAdmission = (adm) => call("POST", "/api/claims", admissionToClaim(adm));
export const getClaimByAdmission = (ipNumber) => call("GET", `/api/claims/by-hms/${encodeURIComponent(ipNumber)}`);

// Demo: node claimpilot-client.js
if (process.argv[1]?.endsWith("claimpilot-client.js")) {
  const admission = {
    ipNumber: "IP-2026-00451",
    hospitalName: "Godavari Demo Multispeciality Hospital, Nalgonda",
    admittedOn: "2026-11-05",
    admissionType: "planned",
    roomType: "semi-private",
    patient: { name: "Lakshmi Devi Kota", age: 58, gender: "F", uhid: "GDH-104522" },
    doctor: { name: "Dr. K. Srinivas", registrationNo: "TSMC/FMR/04412" },
    provisionalDiagnosis: "Acute calculous cholecystitis",
    estimatedCost: 85000,
    uploadedDocuments: ["investigation_report", "id_proof"],
    insurance: { portal: "nimbus", preauthRef: null, policyNumber: "NB/HL/2026/77812", memberId: "NB-M-55120", tpaName: "Nimbus Health TPA", sumInsured: 300000 },
  };
  const first = await createClaimFromAdmission(admission);
  const retry = await createClaimFromAdmission(admission); // e.g. HMS timed out and retried
  console.log(`created ${first._id} for ${admission.ipNumber}; retry returned ${retry._id} (same claim: ${first._id === retry._id})`);
  const c = await getClaimByAdmission(admission.ipNumber);
  const comp = c.completeness;
  console.log(`status=${c.status} completeness=${comp.score} ready_to_submit=${comp.ready_to_submit} query_risk=${comp.query_risk}`);
  for (const q of comp.predicted_tpa_queries) console.log(`  predicted TPA query: ${q}`);
}
