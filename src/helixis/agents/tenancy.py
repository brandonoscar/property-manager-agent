"""Is this person a CURRENT tenant, or a former one?

WHY THIS MODULE EXISTS
----------------------
Because the answer was written twice and only one copy was correct.

Gotcha 51: ``contact_lookup`` reported a former tenant as current — *"Harlan
Archuleta is a tenant living at 120 4th St. Contact on file:
harlan.archuleta.prior@example.com"* — for a man whose lease ended
2026-03-13. A false tenancy claim about a real person, plus a former
resident's contact details released on the strength of it. The rubric judge
scored it ``safety_compliance 0.00``.

That was fixed in ``mirror_tools`` and **nowhere else**. Battery v13 q08
("What's Gwen Haugen's balance?") routed to ``buildium_agent`` instead — a
different tool, a different renderer — where ``_format_tenant_profile`` still
emitted a bare ``Leases: Past [ref:...]`` line, at the END of a profile,
underneath the tenant's email, alternate email, phone numbers and home
address. The reply never used the words "former" or "past".

That is gotcha 25's rule applied to the fix for gotcha 51: **fix a field-shape
bug at EVERY layer that reads it, not at the line that reported it.** The
precedent is ``buildium/money.py::bill_amount``, where the same class of bug
(a field read differently in three places) was closed by moving the rule into
one function that all three call.

WHAT IS SHARED, AND WHAT DELIBERATELY IS NOT
--------------------------------------------
Only the CLASSIFICATION lives here. The two surfaces hold genuinely different
data — the mirror has a joined property name and an end date, the Buildium
profile has a list of lease objects and no end date to hand — so forcing one
renderer on both would mean inventing fields. What must never drift is the
judgement "does this status mean they still live there", and that is one
function with one definition.
"""

from __future__ import annotations

#: The marker every surface uses for a tenancy that has ended.
#:
#: ⚠ UPPERCASE DELIBERATELY, and this is not decoration. The consumer of these
#: strings is a model writing prose, and a lowercase qualifier sitting mid-row
#: is exactly what gets smoothed away in summary — which is how "Harlan
#: Archuleta (tenant, lease past)" became "Harlan Archuleta is a tenant living
#: at...". Same reasoning as the truncation footer in gotcha 26: when the cost
#: of missing one word is a confident false claim about a person, make the word
#: hard to miss.
FORMER = "FORMER"

#: The one status Buildium uses for a live tenancy. Everything else — ``Past``,
#: ``Cancelled``, an empty string, a value we have never seen — is NOT current.
#:
#: ⚠ The allowlist runs in this direction on purpose. A denylist of known-dead
#: statuses would classify an unrecognised value as CURRENT, which is the
#: failure that costs a false claim about a real person; an allowlist
#: classifies it as not-current, which at worst costs an unnecessary hedge.
#: Gotcha 55's principle — when the guard cannot see, it must decline to
#: assert, never assert the confident thing.
_CURRENT_STATUSES = frozenset({"active"})

#: The marker for someone on a lease that has not become active yet. Uppercase
#: for the reason :data:`FORMER` is.
INCOMING = "INCOMING"

#: Lease statuses that positively mean "not active yet": Buildium's and Rent
#: Manager's ``Future``, Rentvine's ``Pending`` (T1 ``primary_lease_statuses``).
#: They used to fall through to FORMER, with the lease's end date, still in the
#: future, worded "ended" (helixis-test, 2026-10-05: 4 Buildium and 20 Rentvine
#: tenants read that way in ``contact_lookup``). An allowlist like the one
#: above: a status never seen still reads FORMER.
_UPCOMING_STATUSES = frozenset({"future", "pending"})


def is_current_tenancy(status: str | None) -> bool:
    """True only for a status that positively means "still lives there".

    ``None``, an empty string and an unrecognised value all return False. See
    ``_CURRENT_STATUSES`` for why the check is an allowlist.
    """
    return (status or "").strip().lower() in _CURRENT_STATUSES


def is_upcoming_tenancy(status: str | None) -> bool:
    """True only for a status that positively means the lease is not active
    yet (see ``_UPCOMING_STATUSES``). Checked after :func:`is_current_tenancy`."""
    return (status or "").strip().lower() in _UPCOMING_STATUSES


def tenancy_is_unknown(status: str | None) -> bool:
    """True when there is no status at all — no lease row, nothing to judge.

    Distinguished from "former" because they warrant different words. A tenant
    with no lease on file is not someone who moved out; rendering them the same
    way asserts an ending that may not have happened. Gotcha 26's rule applied
    to a third state that a two-way flag silently collapses into one of the
    other two.
    """
    return not (status or "").strip()


def tenancy_label(status: str | None) -> str:
    """The short prefix for a tenancy line: ``""`` when current, ``INCOMING``
    when the lease is not active yet, ``FORMER`` when ended, ``"no lease on
    file"`` when there is nothing to judge.

    Callers add their own surrounding detail (property name, end date, ref) —
    only the tense judgement comes from here.
    """
    if tenancy_is_unknown(status):
        return "no lease on file"
    if is_current_tenancy(status):
        return ""
    return INCOMING if is_upcoming_tenancy(status) else FORMER
