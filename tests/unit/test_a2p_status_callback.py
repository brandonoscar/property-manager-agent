"""The A2P Trust Hub status callback — a URL that had no handler.

WHAT HAPPENED
-------------
``a2p_cycle._callback_url`` told the carrier to post profile status to
``/api/v1/twilio/status``. That is the SMS **delivery-receipt** route, whose
first act is::

    message_sid = (params.get("MessageSid") or params.get("SmsSid") or "").strip()
    if not message_sid:
        logger.warning("sms status callback without a MessageSid — ignored")
        return Response(status_code=204)

A Trust Hub bundle callback carries neither key. So every profile status the
carrier ever posted was warned about and dropped — and the field it exists to
deliver is ``failure_reason``, which migration 0084's own column comment calls
*"the single most common one … address could not be validated"*. A customer
whose registration stalled would be told nothing, forever, while
``_callback_url``'s docstring asserted we treat the callback as required.

Gotcha 68 (two handlers, one URL, whichever you wire the other silently does
nothing) meeting gotcha 48 (a comment cannot fail, so it went on describing a
guarantee the deployment did not have).

⚠ WHY THE ROUTE CAN ONLY EVER MOVE A ROW BACKWARDS
---------------------------------------------------
``a2p.APPROVED`` is the one value that opens outbound SMS for a company
(gotchas 72/73). A callback that could promote would turn one misparsed,
replayed or forged POST into carrier approval — the pre-approval send the whole
compliance gate exists to prevent. The poller's authoritative GET promotes;
this records the reason and, at most, ``failed``. Under-trusting costs a poll
cycle. Over-trusting is the violation.

⚠ THE SID KEY IS NOT LIVE-PROBED
---------------------------------
twilio.com is egress-blocked from the build container (gotcha 75), so
``_A2P_SID_KEYS`` is a SET of plausible spellings rather than one confirmed
name — and an unresolved callback logs its whole parameter set instead of being
dropped, because the first real callback is the only thing that can settle it
and it can only settle it if we wrote down what arrived. That is the same
mistake this file exists to fix, so it must not be repeated in the fix.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from helixis.comms import a2p
from helixis.comms.a2p_cycle import _callback_url
from helixis.comms.a2p_pipeline import A2PCredentials
from helixis.main import create_app
from helixis.settings import Settings

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
AUTH_TOKEN = "super-secret-twilio-token-000"
BASE_URL = "https://your-api.onrender.com"
PATH = "/api/v1/twilio/a2p-status"
PROFILE_SID = "BUa83ea4c0000000000000000000000000"


def _sign(url: str, params: dict[str, str]) -> str:
    payload = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(
        hmac.new(AUTH_TOKEN.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()


def _settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        memory={"enabled": False},
        twilio={
            "enabled": True,
            "account_sid": "ACtest",
            "auth_token": AUTH_TOKEN,
            "from_number": "+15550000000",
            "webhook_base_url": BASE_URL,
        },
    )


class FakePool:
    """Records the UPDATE the route issues, and answers with a row count."""

    def __init__(self, *, matches: bool = True) -> None:
        self.matches = matches
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append((" ".join(query.split()), args))
        return "UPDATE 1" if self.matches else "UPDATE 0"


def _post(data: dict[str, str], *, pool: Any | None = None, signature: str | None = "auto") -> Any:
    headers: dict[str, str] = {}
    if signature == "auto":
        headers["X-Twilio-Signature"] = _sign(f"{BASE_URL}{PATH}", data)
    elif signature is not None:
        headers["X-Twilio-Signature"] = signature
    settings = _settings()
    pool = pool if pool is not None else FakePool()
    with (
        patch("helixis.api.routes.twilio.get_pool", return_value=pool),
        patch("helixis.main.get_settings", return_value=settings),
    ):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(PATH, data=data, headers=headers)
    resp.pool = pool  # type: ignore[attr-defined]
    return resp


# ── the wiring defect itself ────────────────────────────────────────────────


def test_the_carrier_is_pointed_at_the_handler_that_records_the_reason() -> None:
    """⚠ The whole bug in one assertion.

    A behavioural test of the route cannot see that nothing sends the carrier
    there (gotcha 62), and the delivery-receipt route answers 204 to anything —
    so pointing at the wrong one is invisible from both ends.
    """
    url = _callback_url(
        A2PCredentials(account_sid="ACx", auth_token="t", webhook_base_url=BASE_URL)
    )
    assert url == f"{BASE_URL}{PATH}", (
        "the A2P status callback is pointed somewhere other than its own handler. "
        "It was aimed at /api/v1/twilio/status — the SMS delivery-receipt route — which "
        "reads MessageSid, finds none on a Trust Hub payload, and drops the rejection "
        "reason the customer needs in order to fix their registration."
    )


def test_an_empty_base_url_still_yields_no_callback_rather_than_a_bare_path() -> None:
    """Unchanged behaviour, pinned because the fix edits that same return."""
    assert (
        _callback_url(A2PCredentials(account_sid="ACx", auth_token="t", webhook_base_url="")) == ""
    )


# ── authentication (gotchas 68/74: no bypass on a real webhook) ─────────────


def test_an_unsigned_callback_is_refused() -> None:
    resp = _post({"BundleSid": PROFILE_SID, "Status": "twilio-rejected"}, signature=None)
    assert resp.status_code == 403


def test_a_forged_signature_is_refused() -> None:
    resp = _post({"BundleSid": PROFILE_SID, "Status": "twilio-rejected"}, signature="not-the-hmac")
    assert resp.status_code == 403
    assert resp.pool.calls == [], "a forged callback reached the database"


# ── what it records ─────────────────────────────────────────────────────────


def test_a_rejection_records_the_carriers_verbatim_words_and_fails_the_row() -> None:
    reason = "The address could not be validated: no match for 12 Nowhere St."
    pool = FakePool()
    resp = _post(
        {"BundleSid": PROFILE_SID, "Status": "twilio-rejected", "FailureReason": reason},
        pool=pool,
    )
    assert resp.status_code == 204
    assert len(pool.calls) == 1
    query, args = pool.calls[0]
    assert "helixis_a2p_registrations" in query
    assert args[0] == PROFILE_SID
    assert args[1] is True, "a twilio-rejected bundle did not mark the row failed"
    assert args[2] == reason, (
        "the reason was paraphrased or dropped — 0084's column comment says a "
        "paraphrased rejection is unactionable, and this is the only place the text "
        "ever appears"
    )


def test_a_reason_with_no_rejection_status_is_still_recorded() -> None:
    """The carrier can attach a reason without a vocabulary we recognise, and
    losing it is the exact failure this route exists for."""
    pool = FakePool()
    resp = _post(
        {"BundleSid": PROFILE_SID, "Status": "something-new", "FailureReason": "why"}, pool=pool
    )
    assert resp.status_code == 204
    assert len(pool.calls) == 1
    assert pool.calls[0][1][1] is False, "an unrecognised status must not fail the row"
    assert pool.calls[0][1][2] == "why"


def test_an_approval_callback_cannot_promote_the_registration() -> None:
    """⚠ The load-bearing refusal.

    ``a2p.APPROVED`` is the single value that opens outbound SMS for a company.
    One misparsed or replayed POST must not be able to reach it — the poller's
    authoritative GET is what promotes (gotchas 55/72/73).
    """
    pool = FakePool()
    resp = _post({"BundleSid": PROFILE_SID, "Status": "twilio-approved"}, pool=pool)
    assert resp.status_code == 204
    assert pool.calls == [], (
        "an approval callback wrote to the registration. A forged or replayed POST could "
        f"then reach {a2p.APPROVED!r}, which is what lets a company text before its "
        "carriers have cleared it — the violation the compliance gate exists to prevent."
    )


def test_the_update_never_sets_a_status_other_than_failed() -> None:
    """Source-level, because the behavioural test above only proves it for the
    statuses it happens to send. The SQL must be structurally incapable of
    writing any other value."""
    pool = FakePool()
    _post({"BundleSid": PROFILE_SID, "Status": "twilio-rejected", "FailureReason": "x"}, pool=pool)
    query = pool.calls[0][0]
    assert "SET status = CASE WHEN $2 THEN 'failed' ELSE status END" in query, (
        "the status write is no longer a one-way move to 'failed' — any other shape "
        "lets a callback advance a registration"
    )
    for forward in a2p.STATUSES:
        if forward in ("draft",):
            continue
        assert f"'{forward}'" not in query, (
            f"the callback's UPDATE can write {forward!r}; only the poller may move a "
            "registration forward"
        )


# ── the shapes we cannot resolve, which must be LOUD ────────────────────────


def test_a_callback_with_no_recognisable_sid_is_logged_with_its_parameters(caplog: Any) -> None:
    """⚠ twilio.com is egress-blocked (gotcha 75), so the SID key spelling is
    inferred. The first real callback is the only thing that settles it — and
    it can only settle it if the parameters reach a log. Dropping them silently
    is exactly the defect this route replaces."""
    pool = FakePool()
    with caplog.at_level("WARNING"):
        resp = _post({"Status": "twilio-rejected", "Mystery": "BU123"}, pool=pool)
    assert resp.status_code == 204
    assert pool.calls == []
    assert any("Mystery" in r.getMessage() for r in caplog.records), (
        "an unrecognisable callback vanished. The parameter set is the evidence that "
        "names the right key, and this route exists because that evidence was thrown away."
    )


def test_an_unknown_sid_warns_with_its_parameters_rather_than_vanishing(caplog: Any) -> None:
    pool = FakePool(matches=False)
    with caplog.at_level("WARNING"):
        resp = _post(
            {"BundleSid": "BUnothing", "Status": "twilio-rejected", "FailureReason": "x"},
            pool=pool,
        )
    assert resp.status_code == 204
    assert any("BUnothing" in r.getMessage() for r in caplog.records)


def test_every_declared_sid_key_actually_resolves() -> None:
    """Both sides (gotcha 28): a key in the set that the handler never reads
    would be a spelling we believe is covered and is not."""
    from helixis.api.routes.twilio import _A2P_SID_KEYS

    for key in _A2P_SID_KEYS:
        pool = FakePool()
        _post({key: PROFILE_SID, "Status": "twilio-rejected", "FailureReason": "x"}, pool=pool)
        assert pool.calls, f"the handler ignores {key!r}, which _A2P_SID_KEYS claims to cover"
        assert pool.calls[0][1][0] == PROFILE_SID


# ── a 2xx always, so the carrier does not retry ─────────────────────────────


def test_it_always_answers_2xx_once_authenticated() -> None:
    """Twilio retries on a non-2xx, and none of the soft cases here merit one —
    same contract as the delivery-receipt route beside it."""
    for data in (
        {"BundleSid": PROFILE_SID, "Status": "twilio-rejected", "FailureReason": "x"},
        {"BundleSid": PROFILE_SID, "Status": "twilio-approved"},
        {"Status": "twilio-rejected"},
        {},
    ):
        assert _post(data).status_code == 204, data
