"""Marking a reminder handled has to leave a record naming who said so.

WHAT WAS WRONG
--------------
Two defects in one `write_activity` call, both found 2026-09-16 while giving
the reminder card its missing verbs (gotcha 140).

**1. The row was gated on the NOTE, and no caller ever sent one.** The route
wrote its audit entry only `if note:` — and `handledReminder(id, note='')` was
the only way in, so `action_type="reminder_handled"` could never appear in the
feed. `grep -rn reminder_handled` found exactly one hit in the whole repo: the
route that emits it. Unreachable in the same way the snooze and handled verbs
themselves were.

That is also the wrong CONDITION, not merely the wrong default. "I dealt with
this outside Occupella" is a claim about the world — `reminders/aging.py`
exists because such a claim can turn out to be wrong and the sweep has to
reopen the card. The act is what is worth recording; the note is detail on it.
A teammate asking "who dealt with the Garden Row renewal?" got nothing.

**2. It did not name the actor.** `ActivityEntry` carries `user_id`, `crm.py`
passes it at five call sites, and this route was the one that did not — on the
single entry whose entire value is that it is attributable.

⚠ The audit row stays BEST-EFFORT. A feed write that fails must never cost the
user the action they took (gotcha 7: an enhancement is never load-bearing), so
the `except` stays and the test below pins that the 200 survives it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
from fastapi.testclient import TestClient

from helixis.db.repositories.reminders import ReminderRow
from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
ME_USER_ID = "00000000-0000-0000-0000-000000000001"
REMINDER_ID = "rem-1"


def _make_token(sub: str = ME_USER_ID) -> str:
    return jwt.encode(
        {
            "sub": sub,
            "email": "brandon@example.com",
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": TEST_ISSUER,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


def _settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )


def _row() -> ReminderRow:
    return ReminderRow(
        id=REMINDER_ID,
        producer="renewal_window",
        entity_key="property:700",
        status="handled",
        severity="high",
        title="2 leases at Garden Row are in the renewal window",
        body="Soonest ends in 28 days.",
        link_path=None,
        payload={},
        snoozed_until=None,
        escalation=0,
        first_fired_at=None,
        last_fired_at=None,
        handled_at=datetime.now(UTC),
    )


def _post_handled(write_activity: AsyncMock, body: dict | None = None):
    """Drive the real route with the repo and the feed mocked."""
    settings = _settings()
    with (
        patch("helixis.api.routes.reminders.get_pool", return_value=MagicMock()),
        patch(
            "helixis.api.routes.reminders.reminders_repo.set_status",
            AsyncMock(return_value=_row()),
        ),
        patch("helixis.api.routes.reminders.write_activity", write_activity),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as client:
            return client.post(
                f"/api/v1/reminders/{REMINDER_ID}/handled",
                headers={"Authorization": f"Bearer {_make_token()}"},
                json=body if body is not None else {},
            )


def test_a_handled_claim_reaches_the_feed_even_with_no_note() -> None:
    """The defect, stated as the property it broke.

    ⚠ The empty body is the WHOLE POINT. It is what every caller sends — the
    UI has no note field — so a test that supplied one would have passed
    against the gated version and proved nothing (gotchas 38/51: the input a
    guard needs is the one the two implementations disagree on).
    """
    write_activity = AsyncMock()
    resp = _post_handled(write_activity)

    assert resp.status_code == 200
    write_activity.assert_awaited_once()
    entry = write_activity.await_args.args[1]
    assert entry.action_type == "reminder_handled"
    assert entry.company_id == COMPANY_ID
    assert "Garden Row" in entry.description


def test_the_row_names_who_made_the_claim() -> None:
    # An audit entry saying somebody claimed they dealt with it, which cannot
    # say who, is the one row where the actor is the point.
    write_activity = AsyncMock()
    _post_handled(write_activity)
    assert write_activity.await_args.args[1].user_id == ME_USER_ID


def test_a_note_still_rides_along_when_one_is_sent() -> None:
    # The note was the only thing that used to reach the feed; widening the
    # condition must not drop it.
    write_activity = AsyncMock()
    resp = _post_handled(write_activity, {"note": "Called the owner, renewing"})

    assert resp.status_code == 200
    assert "Called the owner, renewing" in write_activity.await_args.args[1].description


def test_a_feed_failure_does_not_cost_the_user_their_action() -> None:
    # The status write already succeeded by this point. Letting an audit write
    # turn that into a 500 would make the card look un-handled while the
    # database says otherwise — worse than the missing row (gotcha 7).
    write_activity = AsyncMock(side_effect=RuntimeError("feed down"))
    resp = _post_handled(write_activity)

    assert resp.status_code == 200
    assert resp.json()["status"] == "handled"
