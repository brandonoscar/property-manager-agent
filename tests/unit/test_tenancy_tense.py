"""A former tenant must never be described as a current one — on ANY surface.

Battery v10-100 q10 produced *"Harlan Archuleta is a tenant living at 120 4th
St. Contact on file: harlan.archuleta.prior@example.com"* for a man whose lease
ended 2026-03-13 (gotcha 51). That was fixed in ``contact_lookup``.

Battery v13 q08 ("What's Gwen Haugen's balance?") then routed to
``buildium_agent`` instead — a different tool, a different renderer — and the
reply never said "former" either. ``_format_tenant_profile`` emitted a bare
``Leases: Past [ref:...]`` line at the BOTTOM of the profile, under the email,
the alternate email, the phone numbers and the home address.

Two renderers, one judgement, fixed in one place and not the other: gotcha 25's
rule applied to the fix for gotcha 51.

⚠ GUARD BOTH DIRECTIONS. Marking every tenant FORMER passes a one-sided test
and is a fresh false claim about every current resident (gotcha 38). Each
assertion below has a partner asserting the opposite case.
"""

from __future__ import annotations

from types import SimpleNamespace

from helixis.agents.buildium_tools import _format_tenant_profile
from helixis.agents.tenancy import (
    FORMER,
    INCOMING,
    is_current_tenancy,
    is_upcoming_tenancy,
    tenancy_is_unknown,
    tenancy_label,
)


def _tenant(*, name: str = "Gwen Haugen", statuses: list[str] | None = None) -> SimpleNamespace:
    first, _, last = name.partition(" ")
    return SimpleNamespace(
        first_name=first,
        last_name=last,
        email=f"{first.lower()}.{last.lower()}@example.com",
        alternate_email=None,
        phone_numbers=[],
        address=None,
        leases=[{"Id": 900 + i, "Status": s} for i, s in enumerate(statuses or [])],
    )


# ── the shared classifier ────────────────────────────────────────────────


def test_active_is_current_and_everything_else_is_not() -> None:
    assert is_current_tenancy("Active")
    assert is_current_tenancy("active")
    assert not is_current_tenancy("Past")
    assert not is_current_tenancy("Cancelled")


def test_an_unrecognised_status_is_not_current() -> None:
    """The allowlist direction matters. A denylist would call an unseen status
    CURRENT, and that is the failure that costs a false claim about a person —
    Buildium has changed a status vocabulary before (gotchas 20/26)."""
    assert not is_current_tenancy("SomeNewBuildiumStatus")
    assert not is_current_tenancy("Evicted")


def test_no_status_is_unknown_not_former() -> None:
    """Three states, not two. A tenant with no lease row has not moved out;
    saying FORMER asserts an ending that may not have happened (gotcha 26)."""
    assert tenancy_is_unknown(None) and tenancy_is_unknown("") and tenancy_is_unknown("  ")
    assert not tenancy_is_unknown("Past")
    assert tenancy_label(None) == "no lease on file"
    assert tenancy_label("Past") == FORMER
    assert tenancy_label("Active") == ""


def test_a_lease_not_active_yet_is_incoming_and_nothing_else_is() -> None:
    """The third tense. Buildium's and Rent Manager's ``Future`` and Rentvine's
    ``Pending`` used to read FORMER, an ending that had not happened. Allowlisted
    like current: a status never seen still reads FORMER."""
    for status in ("Future", "Pending", "pending"):
        assert is_upcoming_tenancy(status) and not is_current_tenancy(status)
        assert tenancy_label(status) == INCOMING
    for status in ("Active", "Past", "Closed", "SomeNewBuildiumStatus", None, ""):
        assert not is_upcoming_tenancy(status)
    assert tenancy_label("Closed") == FORMER
    assert INCOMING.isupper()


def test_former_stays_uppercase() -> None:
    """Not decoration. A model reads this and writes prose; a lowercase
    mid-row qualifier is what gets smoothed away in summary (gotcha 51)."""
    assert FORMER == "FORMER" and FORMER.isupper()


# ── the Buildium profile renderer (the v13 q08 surface) ──────────────────


def test_a_past_only_tenant_is_marked_former_in_the_first_line() -> None:
    """⚠ FIRST LINE, not a trailing field. The pre-fix renderer did carry the
    word "Past" — at the bottom, under the contact details — and the reply
    still called her a tenant. Position is the fix, not presence."""
    out = _format_tenant_profile(_tenant(statuses=["Past"]))
    first = out.splitlines()[0]
    assert FORMER in first, out
    # And it must precede the contact details it qualifies.
    assert out.index(FORMER) < out.index("Email:"), out


def test_an_active_tenant_is_not_marked_former() -> None:
    """The other direction. Hedging every tenant is a fresh false claim about
    every current resident (gotcha 38)."""
    out = _format_tenant_profile(_tenant(statuses=["Active"]))
    assert FORMER not in out, out
    assert "no lease on file" not in out, out


def test_a_renewed_tenant_with_a_past_lease_alongside_is_still_current() -> None:
    """⚠ ANY active lease means current. Reading only the first row would call
    a renewed tenant former — and a renewal legitimately leaves the old term
    on the record."""
    out = _format_tenant_profile(_tenant(statuses=["Past", "Active"]))
    assert FORMER not in out, out


def test_a_tenant_moving_in_is_incoming_not_former() -> None:
    """A Future lease alone, or beside the Past one they are leaving, is a move
    IN. Both read "FORMER TENANT" before."""
    for statuses in (["Future"], ["Past", "Future"]):
        first = _format_tenant_profile(_tenant(statuses=statuses)).splitlines()[0]
        assert f"{INCOMING} TENANT" in first and FORMER not in first, first


def test_an_active_lease_outranks_an_upcoming_one() -> None:
    """The other direction: someone renewing into a Future lease lives there now."""
    out = _format_tenant_profile(_tenant(statuses=["Active", "Future"]))
    assert INCOMING not in out and FORMER not in out, out


def test_a_tenant_with_no_leases_says_so_rather_than_asserting_either() -> None:
    out = _format_tenant_profile(_tenant(statuses=[]))
    assert "no lease on file" in out, out
    assert FORMER not in out, out


def test_lease_objects_work_as_well_as_dicts() -> None:
    """The Buildium SDK hands back objects here and dicts there; the renderer
    already handled both for the ref line and must for the status too."""
    t = _tenant()
    t.leases = [SimpleNamespace(id=901, status="Past")]
    assert FORMER in _format_tenant_profile(t).splitlines()[0]
