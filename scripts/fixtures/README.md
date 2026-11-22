# Cross-source test corpus

Occupella answers from Buildium, Gmail, Drive and Calendar at once. Each of
those surfaces can be individually right and still disagree with the others,
and the disagreement is the interesting case: an agent that averages its
sources gets it wrong, an agent that knows which source is authoritative gets
it right.

**Buildium is the system of record.** Every contradiction below resolves in
Buildium's favour. An answer that sides with Gmail or Calendar against
Buildium is a failure even when it sounds confident.

## `occupella_calendar_seed.ics`

Nine events. Import once into the Occupella Google account
(Calendar → Settings → Import & export → Import). Re-importing is safe: each
event carries a stable `UID`, so Google updates in place rather than
duplicating.

⚠ **The event descriptions read as genuine calendar entries and say nothing
about being test data.** That is deliberate — the agent reads this calendar,
and an event captioned "this contradicts Buildium" would hand it the answer.
The map below is the only record of what each event is for, which is why it
lives here and not in the `.ics`.

⚠ **The calendar is UTC.** SOP-01's 24-hour entry notice and SOP-02's TCPA
08:00–21:00 windows are both *local-time* rules, so an agent reasoning about
them off these timestamps is reasoning off the wrong clock. That is a real
product question, not a corpus defect — left in deliberately.

| Event | What it contradicts |
|---|---|
| Ceiling stain work orders — closed out | **The one that matters.** Corroborates Marcus's email claim that he closed the six ceiling work orders. Gmail and Calendar now *agree with each other* and both contradict Buildium, where all six are still open. Two sources against one, and the lone dissenter is the system of record. |
| Premier Restoration — 2C follow-up | Premier's own email says they cannot return until an electrician clears the circuit, and their COI lapsed 2026-08-14. The event books them anyway. |
| 74 Grove — renewal decision review | Scheduled 2026-09-15. The lease ends 2026-08-31 — the review is 15 days *after* expiry. |
| Garden Row 1C — entry for inspection | Booked today for 08:00 tomorrow. Under 24 hours' notice, which SOP-01 §5 forbids. |
| Maple Court 12 — hot water | Maple Court does not exist in Buildium. Neither does the tenant who reported it. |
| Brightline Roofing — roof replacement | Brightline is PENDING / do-not-dispatch with a W-9 outstanding, and the $2,400 was never approved. The event rests on Tom's claimed verbal go-ahead. |
| Owner call / Garden Row inspection | A straight double-booking — same slot, both need someone present. |
| Aged work order review | Presupposes the aged-WO list the ops meeting notes promised. It was never pulled. |

## What good looks like

The agent should name Buildium as authoritative, say plainly where the other
sources are wrong, and **not** resolve a conflict by counting how many
surfaces agree. Siding with the two-against-one on the ceiling work orders is
the headline failure this corpus exists to catch.
