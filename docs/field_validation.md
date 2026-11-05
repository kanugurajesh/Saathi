# Field validation: checking ClaimPilot against a real TPA desk

The portals, query rules and export layouts in this repo come from public material and educated guesses.
This page checks them against people who do the work every day. **Status: not yet done.** The results table
is filled in after each conversation.

## Who to talk to

One or two people for 15 minutes each:
* the **TPA / insurance coordinator** at a 30–150 bed private hospital in Hyderabad or Telangana (they handle
  cashless pre-auth and discharge every day);
* if possible, the **billing head**, who chases receivables from Aarogyasri, EHS, PM-JAY and the TPAs.

Ask permission before taking notes. Record no patient names, policy numbers or screenshots of real portals.

## Questions (15 minutes)

**Portals and daily routine (5 min)**
1. Which insurer and TPA portals do you log into on a normal day? How many in total?
2. Which ones send an OTP, and to whose phone? What happens when that person is off?
3. How do you track statuses today: Excel, a register, the HMS, WhatsApp groups?
4. How often do you check each portal? What makes you miss a status change?

**Queries (5 min)**
5. What are the five queries you get most often? (Let them list first, then show ours below.)
6. Which queries are you *sure* you could have prevented if the file had been complete?
7. How long does a typical query reply take you? What takes the longest: finding the document, getting the doctor to answer, or typing?
8. Would you trust a drafted reply if every unknown was left as a `[[DOCTOR TO CONFIRM]]` placeholder?

**Money and timelines (3 min)**
9. Do insurers actually decide within 1 hour for cashless pre-auth and within 3 hours at discharge? What do you do when they don't?
10. What are the most common deductions? (Room rent, consumables, package rates...?)
11. For Aarogyasri, EHS and PM-JAY: what export or report can you download, and what are its columns?

**Close (2 min)**
12. If one thing in your day could be automated, what would it be?

## Our assumptions to confirm

| Assumption in the code | Where | Confirmed? | Notes |
|---|---|---|---|
| Desk uses 5–10 portals a day | README | | |
| OTP goes to a desk phone shared by the team | `portals/otp.py` | | |
| Top queries: duration of ailment, past history, alcohol history, MLC/FIR copy, implant sticker, signed pre-auth form | `claims/completeness.py` | | |
| Alcohol history is asked for pancreatitis, liver disease and RTA cases | `claims/completeness.py` | | |
| Room-rent proportionate deduction is common | `receivables/ledger.py` | | |
| IRDAI 1 h / 3 h timelines are often missed | `claims/status.py` | | |
| Aarogyasri/EHS/PM-JAY exports have the columns in `samples/receivables/` | `receivables/importer.py` | | |

## Results

| Date | Role, hospital size | Key findings | Changes made in the code |
|---|---|---|---|
| | | | |
