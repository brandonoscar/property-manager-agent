"""Who on the team screen has never actually arrived.

⚠ **The membership row is created UP FRONT at invite time** — see
``routes/company._invite_one``, which writes ``company_users`` before the
invitee has done anything. So an admin who invites three people sees three
ordinary rows, identical to colleagues who set up weeks ago, and the product
cannot answer *"did Sam ever get in?"*. Measured on the live project the day
this shipped: **2 of 15 accounts had been invited and never signed in**, both
rendering as full members.

It costs money as well as clarity. ``invite_member`` calls ``sync_company_seats``
on the invite, and its own comment says why: *"A seat is counted at INVITE
because that is when the company_users row is created."* So an unopened
invitation is a billed seat that nothing on screen identifies.

The resend button that would naturally go beside this is **deliberately not
shipped**, and the reason is in ``_gotrue_invite``: it returns ``None`` on a 422
for an address that already has an account — which every pending invitee does,
because GoTrue created their row when the first invite went out. A second POST
to ``/auth/v1/invite`` would therefore send nothing and report success, which is
the FeedbackDialog failure exactly: a person told their action worked when it
did not. Resending needs a GoTrue path nobody here has probed.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.db.repositories.members import list_members

_ROOT = Path(__file__).resolve().parents[2]
_REPO = _ROOT / "src" / "helixis" / "db" / "repositories" / "members.py"
_ROUTES = _ROOT / "src" / "helixis" / "api" / "routes" / "company.py"
_SERVICE = _ROOT / "frontend" / "src" / "services" / "company.ts"
_SETTINGS = _ROOT / "frontend" / "src" / "components" / "SettingsPage.tsx"

COMPANY = "11111111-1111-1111-1111-111111111111"


def _row(**over: Any) -> dict[str, Any]:
    base = {
        "user_id": "u1",
        "email": "sam@example.com",
        "display_name": "Sam",
        "role": "member",
        "created_at": 1,
        "invite_pending": False,
    }
    base.update(over)
    return base


def _pool(rows: list[dict[str, Any]]) -> MagicMock:
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=rows)
    return pool


@pytest.mark.asyncio
async def test_an_invitee_who_never_signed_in_is_marked_pending() -> None:
    admin = _row(user_id="a1", role="admin", email="boss@example.com")
    waiting = _row(user_id="u2", email="sam@example.com", invite_pending=True)
    members = await list_members(_pool([admin, waiting]), company_id=COMPANY)
    by_id = {m.user_id: m for m in members}
    assert by_id["u2"].invite_pending is True
    assert by_id["a1"].invite_pending is False


@pytest.mark.asyncio
async def test_a_colleague_who_uses_the_product_is_not_marked() -> None:
    """The direction that would be worse than the gap. Marking everybody
    pending tells an admin their whole team never arrived, and it would look
    exactly as authoritative as the true version."""
    members = await list_members(_pool([_row(invite_pending=False)]), company_id=COMPANY)
    assert members[0].invite_pending is False


@pytest.mark.asyncio
async def test_a_missing_auth_row_is_not_called_pending() -> None:
    """The roster LEFT JOINs auth.users, so the flag can arrive NULL. An absent
    identity is a different problem — calling it "invitation pending" points an
    admin at resending an email to somebody with no address on file."""
    members = await list_members(_pool([_row(invite_pending=None, email=None)]), company_id=COMPANY)
    assert members[0].invite_pending is False


def test_the_query_reads_both_columns_not_either() -> None:
    """⚠ Either column alone is wrong, in opposite directions, and both look
    reasonable in a diff.

    ``invited_at`` stays set forever after somebody accepts, so on its own it
    marks long-standing colleagues as pending. ``last_sign_in_at IS NULL`` alone
    catches accounts created some other way that were never invited. Only the
    pair means "was sent an invitation and has never used it" — the same lesson
    as gotcha 59: pin the operator, not just the columns.
    """
    sql = " ".join(_REPO.read_text(encoding="utf-8").split())
    assert "u.invited_at IS NOT NULL AND u.last_sign_in_at IS NULL" in sql, (
        "the pending signal is no longer both halves — one alone mislabels "
        "either every colleague or every non-invited account"
    )


def test_the_flag_survives_the_whole_way_to_the_screen() -> None:
    """Both sides of every boundary it crosses (gotcha 28). The producer and
    each consumer are individually plausible while the value is dropped
    somewhere between them, and the symptom is simply that no row is ever
    marked — indistinguishable from a team where everyone has signed in."""
    routes = _ROUTES.read_text(encoding="utf-8")
    assert "invite_pending: bool = False" in routes, "MemberOut no longer carries it"
    assert "invite_pending=m.invite_pending" in routes, "the route no longer passes it through"

    service = _SERVICE.read_text(encoding="utf-8")
    assert "invite_pending?: boolean" in service, "the client type no longer reads it"

    settings = _SETTINGS.read_text(encoding="utf-8")
    assert "m.invite_pending" in settings, "the team screen no longer renders it"
    assert "Invite pending" in settings


def test_no_resend_button_was_shipped_on_an_unprobed_path() -> None:
    """⚠ A guard on a DELIBERATE ABSENCE, because the absence looks like an
    oversight and the obvious fix is broken.

    ``_gotrue_invite`` returns None on a 422 — "already registered" — and every
    pending invitee already has a GoTrue row from the first invite. So a resend
    built on it sends no email and reports success. If somebody adds the button,
    this fails and sends them to read why first.
    """
    routes = _ROUTES.read_text(encoding="utf-8")
    tree = ast.parse(routes)
    paths = [
        arg.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in [None]
        for arg in node.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]
    offenders = [p for p in paths if "resend" in p.lower()]
    assert not offenders, (
        f"a resend route appeared ({offenders}). _gotrue_invite answers 422 -> None "
        "for an address that already has an account, which every pending invitee "
        "does — so the obvious implementation sends nothing and says it worked."
    )
