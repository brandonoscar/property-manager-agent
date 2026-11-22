"""The A2P ISV pipeline and its sweep.

Two modules, split so each is testable alone: ``a2p_pipeline`` talks to the
carrier and returns column updates (no database), ``a2p_cycle`` reads rows and
persists what comes back (no network).

The properties worth pinning are the expensive ones:

  * **Resumable, never restarted.** A stored SID SKIPS its step. A second
    brand costs another registration fee AND is rejected as a duplicate, so
    "did the retry re-POST it" is the question this file exists to answer.
  * **Money is guarded before the call, not after.** Brand registration and
    buying a number are the only two paid steps.
  * **An unknown carrier status changes nothing.** Inventing a state for an
    unrecognised response is how a pending registration reads as approved.
  * **`approved` is terminal and still has work left** — the number is bought
    after the final status, so a plain terminal skip strands it.
  * **Every attempt is stamped**, including failures, or the backoff is a
    decoration and a broken row is a carrier-API spend loop.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from helixis.comms import a2p, a2p_cycle, a2p_pipeline
from helixis.comms.a2p_pipeline import A2PCredentials

#: ⚠ ``primary_profile_sid`` is not optional decoration. ``advance_submission``
#: refuses to run without it (the carrier rejects a secondary profile that is
#: not assigned to an approved primary — "Primary customer profile bundle is
#: null"), so a fixture missing it tests the refusal and nothing else.
PRIMARY = "BUprimary00000000000000000000000"

CREDS = A2PCredentials(
    account_sid="ACtest",
    auth_token="tok",
    webhook_base_url="https://occupella.example",
    primary_profile_sid=PRIMARY,
)

COMPANY = "11111111-1111-1111-1111-111111111111"


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "company_id": COMPANY,
        "legal_business_name": "Blue Ridge Properties LLC",
        "ein": "12-3456789",
        "business_type": "Limited Liability Corporation",
        "business_vertical": "REAL_ESTATE",
        "website": "https://blueridge.example",
        "street": "200 E Colfax Ave",
        "city": "Denver",
        "region": "CO",
        "postal_code": "80203",
        "iso_country": "US",
        "rep_first_name": "Dana",
        "rep_last_name": "Ops",
        "rep_email": "dana@blueridge.example",
        "rep_phone": "+15551234567",
        "rep_job_position": "Owner",
        "use_case": "MIXED",
        "campaign_description": "Maintenance updates and rent reminders.",
        "opt_in_description": "Residents give their number on the lease; reply STOP to end.",
        # ⚠ TWO. LOW_VOLUME refuses a campaign with fewer than two samples
        # (`a2p.MIN_SAMPLE_MESSAGES`, raised 2026-09-04 after the carrier said
        # so on the first live mock run). A one-sample fixture no longer means
        # "a complete registration" — it means one the carrier rejects.
        "sample_messages": json.dumps(
            [
                "Occupella: your work order is Tue. Reply STOP.",
                "Occupella: rent of $1,450 is due Mar 1. Reply STOP, HELP for help.",
            ]
        ),
        "status": "submitted",
        "failure_reason": None,
        "customer_profile_sid": None,
        "trust_product_sid": None,
        "brand_sid": None,
        "messaging_service_sid": None,
        "campaign_sid": None,
        "step_sids": "{}",
        "last_attempt_at": None,
    }
    row.update(overrides)
    return row


class FakeTransport(httpx.AsyncBaseTransport):
    """Records every carrier request and replies from a per-path script."""

    def __init__(self, script: dict[str, Any] | None = None) -> None:
        self.script = script or {}
        self.calls: list[tuple[str, str]] = []
        #: Every ``ObjectSid`` posted to an EntityAssignments sub-resource, and
        #: every end-user ``Type`` created. Recorded from the WIRE rather than
        #: from the payload builders: the builders were right about
        #: MessageSamples too, and the encoding between them was what was
        #: wrong (gotcha 28 with a transport as the second side).
        self.assignments: list[str] = []
        self.end_user_types: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "POST":
            form = urllib.parse.parse_qs(request.content.decode() or "")
            if path.endswith("/EntityAssignments"):
                self.assignments.extend(form.get("ObjectSid", []))
            elif path.endswith("/EndUsers"):
                self.end_user_types.extend(form.get("Type", []))
        # ⚠ THE CAMPAIGN RESOURCE HAS TWO SHAPES AND A SUBSTRING MATCH SEES
        # ONE. `.../Compliance/Usa2p` is the LIST — a `{"compliance": [...]}`
        # wrapper with no top-level `campaign_status`; `.../Usa2p/{sid}` is the
        # instance, flat. A fake that answers the scripted flat body to both
        # cannot tell a poll that works from one that silently never detects
        # anything, which is how the missing SID survived (gotcha 79). Shapes
        # measured against the live API 2026-09-04.
        if "/Compliance/Usa2p" in path:
            scripted = next(
                (v for k, v in self.script.items() if k in path and isinstance(v, dict)), {}
            )
            if path.rstrip("/").endswith("/Usa2p"):
                return httpx.Response(200, json={"compliance": [scripted], "meta": {"page": 0}})
            return httpx.Response(200, json={**scripted, "sid": path.rsplit("/", 1)[-1]})
        for fragment, payload in self.script.items():
            if fragment in path:
                if isinstance(payload, int):
                    return httpx.Response(payload, json={"message": "carrier said no"})
                return httpx.Response(200, json=payload)
        # ⚠ An unscripted EVALUATION passes. Not laziness — the alternative is
        # every unrelated test in this file failing on a policy check it never
        # meant to exercise, which buries the tests that DO pin evaluation
        # behaviour. Compliance is asserted where it is the subject
        # (`test_a_noncompliant_bundle_is_never_submitted`), and the default
        # here keeps the other properties readable.
        if path.endswith("/Evaluations"):
            return httpx.Response(200, json={"sid": "EVfake", "status": "compliant"})
        # A generic created object. Every trust-hub POST returns a sid, so an
        # unscripted step still advances rather than exploding on a KeyError.
        return httpx.Response(200, json={"sid": f"XX{abs(hash(path)) % 10**6}"})


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    """Route every httpx client the pipeline opens through one fake, and make
    the rate-limit sleeps instant so the suite is not paced by a carrier."""
    t = FakeTransport()
    real = httpx.AsyncClient

    def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = t
        return real(*args, **kwargs)

    monkeypatch.setattr(a2p_pipeline.httpx, "AsyncClient", _client)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(a2p_pipeline.asyncio, "sleep", _no_sleep)
    return t


# ── resumability: the property that keeps money from being spent twice ──


@pytest.mark.asyncio
async def test_a_full_run_creates_the_chain_once(transport: FakeTransport) -> None:
    updates = await a2p_pipeline.advance_submission(_row(), CREDS)
    assert updates["customer_profile_sid"]
    assert updates["brand_sid"]
    assert updates["messaging_service_sid"]
    assert updates["status"] == "brand_pending"
    paths = [p for _, p in transport.calls]
    assert any("CustomerProfiles" in p for p in paths)
    assert any("BrandRegistrations" in p for p in paths)


@pytest.mark.asyncio
async def test_a_retry_never_registers_a_second_brand(transport: FakeTransport) -> None:
    """⚠ THE expensive one. A brand costs a registration fee and the registry
    rejects duplicates, so a resumed run must reuse the stored SID."""
    row = _row(
        customer_profile_sid="BUxxx",
        trust_product_sid="BUyyy",
        brand_sid="BNzzz",
        messaging_service_sid="MGaaa",
        step_sids=json.dumps(
            {
                "business_end_user": "IT1",
                "representative_end_user": "IT2",
                "address": "AD1",
                "supporting_document": "RD1",
                "assigned_business_end_user": "IT1",
                "assigned_representative_end_user": "IT2",
                "assigned_supporting_document": "RD1",
                "profile_submitted": "yes",
                "assigned_profile_to_trust": "BUxxx",
                "trust_submitted": "yes",
            }
        ),
    )
    updates = await a2p_pipeline.advance_submission(row, CREDS)
    assert "brand_sid" not in updates, "a stored brand must never be re-registered"
    assert not [p for _, p in transport.calls if "BrandRegistrations" in p]


@pytest.mark.asyncio
async def test_a_partial_run_resumes_rather_than_recreating(transport: FakeTransport) -> None:
    """A crash after the profile must not create a second profile, or a second
    pair of end users hanging off nothing."""
    row = _row(
        customer_profile_sid="BUxxx",
        step_sids=json.dumps({"business_end_user": "IT1", "address": "AD1"}),
    )
    await a2p_pipeline.advance_submission(row, CREDS)
    paths = [p for _, p in transport.calls]
    assert not [p for p in paths if p.endswith("/CustomerProfiles")], "profile recreated"
    # TWO, and WHICH two is the point: the representative (business_end_user is
    # already stored and must be skipped) and the A2P messaging profile end
    # user added 2026-09-04. Bumping this number without naming the new step is
    # how a re-created resource hides inside a passing test.
    assert len([p for p in paths if p.endswith("/EndUsers")]) == 2, (
        "only the MISSING end users should be created"
    )
    assert not [p for p in paths if "Addresses.json" in p], "address recreated"


@pytest.mark.asyncio
async def test_an_incomplete_row_is_refused_before_anything_is_created(
    transport: FakeTransport,
) -> None:
    """A row edited by hand must not spend a registration fee on a body the
    registry will reject — the cheap half of a check whose expensive half
    takes a fortnight."""
    with pytest.raises(a2p.A2PError) as exc:
        await a2p_pipeline.advance_submission(_row(legal_business_name=""), CREDS)
    assert exc.value.step == "validate"
    assert not transport.calls


@pytest.mark.asyncio
async def test_a_carrier_rejection_keeps_the_carriers_own_words(
    transport: FakeTransport,
) -> None:
    """⚠ Verbatim. "Address could not be validated" tells the customer exactly
    what to fix; "registration failed" generates a support ticket."""
    transport.script = {"CustomerProfiles": 400}
    with pytest.raises(a2p.A2PError) as exc:
        await a2p_pipeline.advance_submission(_row(), CREDS)
    assert exc.value.step == "customer_profile"
    assert exc.value.reason == "carrier said no"


# ── polling: an unknown status must change nothing ─────────────────────


@pytest.mark.asyncio
async def test_an_unrecognised_carrier_status_changes_nothing(
    transport: FakeTransport,
) -> None:
    """⚠ ``map_*_status`` returns None for anything it does not know, and this
    must surface as NO update. Inventing a state for an unknown response is
    how a pending registration starts reading as approved."""
    transport.script = {"BrandRegistrations": {"status": "SOMETHING_NEW"}}
    updates = await a2p_pipeline.poll_status(_row(status="brand_pending", brand_sid="BN1"), CREDS)
    assert updates == {}


@pytest.mark.asyncio
async def test_brand_approval_advances_and_stamps(transport: FakeTransport) -> None:
    transport.script = {"BrandRegistrations": {"status": "APPROVED"}}
    updates = await a2p_pipeline.poll_status(_row(status="brand_pending", brand_sid="BN1"), CREDS)
    assert updates["status"] == "brand_approved"
    assert updates["brand_approved_at"] is not None


@pytest.mark.asyncio
async def test_a_campaign_rejection_carries_the_registry_reasons(
    transport: FakeTransport,
) -> None:
    transport.script = {
        "Usa2p": {
            "campaign_status": "FAILED",
            "errors": [{"description": "Sample messages do not match the use case"}],
        }
    }
    updates = await a2p_pipeline.poll_status(
        _row(status="campaign_pending", campaign_sid="QE1", messaging_service_sid="MG1"), CREDS
    )
    assert updates["status"] == "failed"
    assert "Sample messages do not match" in updates["failure_reason"]


@pytest.mark.asyncio
async def test_the_campaign_is_polled_in_preference_to_the_brand(
    transport: FakeTransport,
) -> None:
    """Once a campaign exists it is the live question — the brand is already
    approved and re-reading it would overwrite a later state with an earlier
    one."""
    transport.script = {"Usa2p": {"campaign_status": "VERIFIED"}}
    updates = await a2p_pipeline.poll_status(
        _row(
            status="campaign_pending",
            brand_sid="BN1",
            campaign_sid="QE1",
            messaging_service_sid="MG1",
        ),
        CREDS,
    )
    assert updates["status"] == "approved"
    assert not [p for _, p in transport.calls if "BrandRegistrations" in p]


# ── the number: the second place money is spent ───────────────────────


@pytest.mark.asyncio
async def test_a_company_that_already_has_a_number_buys_nothing(
    transport: FakeTransport,
) -> None:
    """Two numbers for one company is a real cost AND an ambiguous outbound
    identity — replies land on whichever one the resolver did not pick."""
    bought = await a2p_pipeline.ensure_number(
        COMPANY, _row(status="approved"), CREDS, existing_numbers=1
    )
    assert bought is None
    assert not transport.calls


@pytest.mark.asyncio
async def test_no_number_is_bought_before_approval(transport: FakeTransport) -> None:
    """A number attached to an unapproved campaign cannot send, so buying
    early charges monthly for something dark."""
    bought = await a2p_pipeline.ensure_number(
        COMPANY, _row(status="campaign_pending"), CREDS, existing_numbers=0
    )
    assert bought is None
    assert not transport.calls


# ── backoff ───────────────────────────────────────────────────────────


def test_a_pending_registration_is_not_re_polled_every_cycle() -> None:
    """Campaign vetting is 10-15 DAYS. Polling every worker tick spends
    carrier calls on a status that cannot have moved."""
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    fresh = _row(status="campaign_pending", last_attempt_at=now - timedelta(minutes=30))
    stale = _row(status="campaign_pending", last_attempt_at=now - timedelta(hours=7))
    assert not a2p_pipeline.due(fresh, now=now)
    assert a2p_pipeline.due(stale, now=now)


def test_a_failed_registration_retries_sooner_than_a_pending_one_is_polled() -> None:
    """A failure is often OURS and fixable; a customer should not wait a
    quarter of a day to find out a redeploy fixed it."""
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    row = _row(
        status="brand_pending",
        failure_reason="transient",
        last_attempt_at=now - timedelta(hours=1),
    )
    assert a2p_pipeline.due(row, now=now)


def test_a_never_attempted_row_is_due_immediately() -> None:
    """NULL means "never tried", not "tried long ago" — a registration queued
    while the worker was down must start on the next cycle."""
    assert a2p_pipeline.due(_row(last_attempt_at=None))


def test_an_approved_company_without_a_number_is_still_swept() -> None:
    """⚠ ``approved`` is TERMINAL and still has work left: the number is
    bought AFTER the final status. A plain terminal skip means a customer is
    approved and never receives the number approval was for."""
    row = _row(status="approved", last_attempt_at=None)
    assert not a2p_pipeline.due(row, needs_number=False)
    assert a2p_pipeline.due(row, needs_number=True)


def test_a_failed_registration_is_never_swept_on_the_number_path() -> None:
    """`needs_number` reopens `approved` only. A failed row has nothing to buy
    and re-sweeping it would retry a rejection forever."""
    assert not a2p_pipeline.due(_row(status="failed"), needs_number=True)


# ── the sweep ─────────────────────────────────────────────────────────


class FakeCyclePool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, _query: str, *_args: Any) -> list[dict[str, Any]]:
        return self.rows

    async def execute(self, query: str, *args: Any) -> None:
        self.executed.append((" ".join(query.split()), args))


@pytest.mark.asyncio
async def test_a_deployment_with_no_credentials_does_nothing() -> None:
    """An unconfigured deployment is a normal pre-launch state, not an error —
    and it must not even query."""
    pool = FakeCyclePool([_row()])
    n = await a2p_cycle.run_a2p_cycle(pool, A2PCredentials(account_sid="", auth_token=""))
    assert n == 0
    assert not pool.executed


@pytest.mark.asyncio
async def test_one_failing_company_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One customer's rejected address is not a reason to leave every other
    registration unadvanced."""
    rows = [
        {**_row(status="submitted"), "company_id": "aaa", "number_count": 0},
        {**_row(status="submitted"), "company_id": "bbb", "number_count": 0},
    ]
    pool = FakeCyclePool(rows)
    seen: list[str] = []

    async def _advance(row: dict[str, Any], creds: Any, **_kw: Any) -> dict[str, Any]:
        # `row` is the dict the cycle passes through; company_id identifies it.
        who = str(row["company_id"])
        seen.append(who)
        if who == "aaa":
            raise RuntimeError("boom")
        return {"status": "brand_pending"}

    monkeypatch.setattr(a2p_cycle.a2p_pipeline, "advance_submission", _advance)
    await a2p_cycle.run_a2p_cycle(pool, CREDS)
    assert seen == ["aaa", "bbb"], "the second company must still be attempted"


@pytest.mark.asyncio
async def test_every_attempt_is_stamped_even_when_nothing_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ Without the stamp the backoff never advances and an unchanged poll is
    re-issued every cycle forever — a carrier-API spend loop nothing reports."""
    pool = FakeCyclePool([{**_row(status="brand_pending"), "number_count": 0}])

    async def _poll(_row: dict[str, Any], _creds: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(a2p_cycle.a2p_pipeline, "poll_status", _poll)
    await a2p_cycle.run_a2p_cycle(pool, CREDS)
    assert pool.executed, "an unchanged poll must still record the attempt"
    assert "last_attempt_at" in pool.executed[0][0]


@pytest.mark.asyncio
async def test_a_carrier_rejection_is_written_to_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakeCyclePool([{**_row(status="submitted"), "number_count": 0}])

    async def _boom(*_a: Any, **_kw: Any) -> dict[str, Any]:
        raise a2p.A2PError("no", step="address", reason="Address could not be validated")

    monkeypatch.setattr(a2p_cycle.a2p_pipeline, "advance_submission", _boom)
    await a2p_cycle.run_a2p_cycle(pool, CREDS)
    sql, args = pool.executed[0]
    assert "status" in sql and "failure_reason" in sql
    assert "Address could not be validated" in args


@pytest.mark.asyncio
async def test_approval_and_its_number_can_land_in_one_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The number rides on the row AFTER this cycle's status change, so a
    customer is not made to wait a whole backoff window for the last step."""
    pool = FakeCyclePool([{**_row(status="campaign_pending"), "number_count": 0}])

    async def _poll(_row: dict[str, Any], _creds: Any) -> dict[str, Any]:
        return {"status": "approved"}

    bought: list[str] = []

    async def _ensure(company_id: str, row: dict[str, Any], _creds: Any, **_kw: Any) -> str:
        assert row["status"] == "approved", "ensure_number must see the NEW status"
        bought.append(company_id)
        return "+15550001111"

    monkeypatch.setattr(a2p_cycle.a2p_pipeline, "poll_status", _poll)
    monkeypatch.setattr(a2p_cycle.a2p_pipeline, "ensure_number", _ensure)
    await a2p_cycle.run_a2p_cycle(pool, CREDS)
    assert bought == [COMPANY]
    assert any("helixis_twilio_numbers" in sql for sql, _ in pool.executed)


@pytest.mark.asyncio
async def test_a_failed_purchase_never_loses_the_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ The registration IS approved. Dropping that because a purchase failed
    would re-poll next cycle and keep reporting the customer as pending when
    they are not."""
    pool = FakeCyclePool([{**_row(status="campaign_pending"), "number_count": 0}])

    async def _poll(_row: dict[str, Any], _creds: Any) -> dict[str, Any]:
        return {"status": "approved"}

    async def _ensure(*_a: Any, **_kw: Any) -> str:
        raise RuntimeError("no numbers available")

    monkeypatch.setattr(a2p_cycle.a2p_pipeline, "poll_status", _poll)
    monkeypatch.setattr(a2p_cycle.a2p_pipeline, "ensure_number", _ensure)
    await a2p_cycle.run_a2p_cycle(pool, CREDS)
    sql, args = pool.executed[0]
    assert "status" in sql
    assert "approved" in args


@pytest.mark.asyncio
async def test_a_draft_is_never_touched_by_the_sweep() -> None:
    """The customer is still typing. The SQL excludes drafts, and that has to
    stay true — running the chain on a half-filled form spends a fee."""
    assert "status <> 'draft'" in " ".join(a2p_cycle._SELECT_ACTIVE.split())


# ── wiring (gotcha 62: a helper nothing calls is the real bug) ─────────


def test_the_worker_actually_runs_the_sweep() -> None:
    """⚠ A behavioural test of the cycle cannot see that nothing calls it.
    ``comms_triage`` shipped complete, unit-tested and unreachable; this is
    the AST guard that would have caught it."""
    import ast
    from pathlib import Path

    from helixis.webhooks import worker

    # From the MODULE, not by counting parent directories — a hand-built path
    # is one refactor away from pointing at nothing, and a guard that cannot
    # find its target reports the same red as a guard that failed.
    tree = ast.parse(Path(worker.__file__).read_text(encoding="utf-8"))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "run_a2p_cycle" in called, (
        "worker.py never calls run_a2p_cycle -- the whole pipeline would be "
        "complete, tested and unreachable"
    )


# ── REST paths, not SDK class names ─────────────────────────────────────────
#
# Every endpoint path in this module is documented rather than live-probed, and
# on 2026-09-01 `scripts/a2p_mock_run.py` ran the free tier against the real
# carrier API for the first time. It got through the customer profile, both end
# users, the address and the supporting document, then:
#
#   404  The requested resource /v1/CustomerProfiles/BUa83ea4.../
#        CustomerProfilesEntityAssignments was not found
#
# `CustomerProfilesEntityAssignments` and `TrustProductsEntityAssignments` are
# the twilio-python SDK's CLASS names. The REST sub-resource is plain
# `EntityAssignments`. Probed on AC01833f... the same day:
#
#   /CustomerProfiles/{sid}/CustomerProfilesEntityAssignments -> 404
#   /CustomerProfiles/{sid}/EntityAssignments                 -> 200
#   /TrustProducts/{sid}/TrustProductsEntityAssignments       -> 404
#   /TrustProducts/{sid}/EntityAssignments                    -> 200
#
# This blocked the WHOLE chain: no customer could finish A2P, so no company
# could ever send an SMS. A string that is wrong in a way the type system and
# every mock accept is exactly what a source-level guard is for -- a behavioural
# test passes against a fake that answers whatever path it is handed.


def test_entity_assignment_paths_are_rest_names_not_sdk_class_names() -> None:
    """The SDK class name 404s. Pin the REST path in the source."""
    import inspect

    from helixis.comms import a2p_pipeline

    src = inspect.getsource(a2p_pipeline)
    # Comments in this module quote the broken forms to explain them, so the
    # check is on the f-string URLs, not on the file's prose.
    urls = [
        line for line in src.splitlines() if "_TRUSTHUB" in line and "EntityAssignments" in line
    ]
    # FOUR assignments now, each of which 404s under its SDK class name:
    #   1. the customer profile's end users + document (the loop)
    #   2. the PRIMARY profile onto the secondary       (added 2026-09-04)
    #   3. the customer profile onto the trust product
    #   4. the A2P messaging profile end user onto it   (added 2026-09-04)
    assert len(urls) == 4, f"expected 4 entity-assignment URLs, found {len(urls)}"
    for line in urls:
        assert "CustomerProfilesEntityAssignments" not in line, (
            "SDK class name used as a REST path — this 404s (2026-09-01 probe)"
        )
        assert "TrustProductsEntityAssignments" not in line, (
            "SDK class name used as a REST path — this 404s (2026-09-01 probe)"
        )
        assert "/EntityAssignments" in line


# ── the mock/free harness: validating the chain without spending ──────
#
# `scripts/a2p_mock_run.py` exists because every path in this module is
# documented, not live-probed. These pin the two properties that make running
# it safe: the free tier cannot reach a paid step, and a mock brand that the
# carrier does not CONFIRM as mock is a refusal rather than a green run.


@pytest.mark.asyncio
async def test_the_free_tier_never_reaches_a_paid_step(transport: FakeTransport) -> None:
    """⚠ Steps 1-5 are free and are where a wrong field name actually lives.
    The whole point of the tier is that it CANNOT charge, so this asserts on
    the requests made rather than on the return value."""
    await a2p_pipeline.advance_submission(_row(), CREDS, stop_before_paid=True)
    paths = [path for _method, path in transport.calls]
    assert not [p for p in paths if "BrandRegistrations" in p], (
        "the free tier POSTed a brand registration — that is the paid step"
    )
    assert not [p for p in paths if p.endswith("/Services")], (
        "the free tier created a messaging service, which is past the money"
    )
    # …and it still did the work it claims to validate.
    assert [p for p in paths if "CustomerProfiles" in p]
    assert [p for p in paths if "TrustProducts" in p]


@pytest.mark.asyncio
async def test_a_mock_run_sends_the_mock_flag(transport: FakeTransport) -> None:
    sent: dict[str, str] = {}
    real_post = a2p_pipeline._post

    async def _capture(
        client: Any, url: str, form: dict[str, Any], creds: Any, *, step: str
    ) -> Any:
        if step == "brand_registration":
            sent.update({k: str(v) for k, v in form.items()})
        return await real_post(client, url, form, creds, step=step)

    a2p_pipeline._post = _capture  # type: ignore[assignment]
    try:
        with pytest.raises(a2p.A2PError):
            # The generic fake reply says nothing about mock, so this correctly
            # refuses — which is the NEXT test. Here we only want the form.
            await a2p_pipeline.advance_submission(_row(), CREDS, mock=True)
    finally:
        a2p_pipeline._post = real_post  # type: ignore[assignment]
    assert sent.get(a2p_pipeline._MOCK_BRAND_PARAM) == "true"


@pytest.mark.asyncio
async def test_a_brand_not_confirmed_mock_refuses_and_names_the_sid(
    transport: FakeTransport,
) -> None:
    """⚠ THE ONE THAT MATTERS. If the mock parameter name is wrong the API
    IGNORES it, returns 200, and registers a REAL brand at full fee. A harness
    that reported that as free would be worse than no harness — so an
    unconfirmed response is a refusal, and it names the SID to go delete."""
    transport.script = {"BrandRegistrations": {"sid": "BNreal123", "status": "PENDING"}}
    with pytest.raises(a2p.A2PError) as exc:
        await a2p_pipeline.advance_submission(_row(), CREDS, mock=True)
    assert exc.value.step == "brand_registration"
    assert "BNreal123" in str(exc.value.reason), "the refusal must name the brand to delete"
    assert a2p_pipeline._MOCK_BRAND_PARAM in str(exc.value.reason)


@pytest.mark.asyncio
async def test_a_confirmed_mock_brand_proceeds(transport: FakeTransport) -> None:
    transport.script = {"BrandRegistrations": {"sid": "BNmock1", "status": "PENDING", "mock": True}}
    updates = await a2p_pipeline.advance_submission(_row(), CREDS, mock=True)
    assert updates["brand_sid"] == "BNmock1"


@pytest.mark.asyncio
async def test_production_never_sends_the_mock_flag(transport: FakeTransport) -> None:
    """⚠ The inverse, and it is the dangerous direction: a real customer
    registered as a mock gets a brand that expires in 30 days and can never
    send. Default off, and pinned."""
    sent: dict[str, str] = {}
    real_post = a2p_pipeline._post

    async def _capture(
        client: Any, url: str, form: dict[str, Any], creds: Any, *, step: str
    ) -> Any:
        if step == "brand_registration":
            sent.update({k: str(v) for k, v in form.items()})
        return await real_post(client, url, form, creds, step=step)

    a2p_pipeline._post = _capture  # type: ignore[assignment]
    try:
        await a2p_pipeline.advance_submission(_row(), CREDS)
    finally:
        a2p_pipeline._post = real_post  # type: ignore[assignment]
    assert sent, "the brand step did not run"
    assert a2p_pipeline._MOCK_BRAND_PARAM not in sent


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"mock": True}, True),
        ({"mock": False}, False),
        ({"mock": "true"}, True),
        ({"is_mock": True}, True),
        ({}, False),
        ({"mock": "yes"}, False),
        ({"status": "PENDING"}, False),
    ],
)
def test_an_unreadable_answer_is_not_a_mock(body: dict[str, Any], expected: bool) -> None:
    """⚠ Never defaults True on a shape it does not recognise. "Probably free"
    is not a thing to report about a fee — and the response shape here is
    unprobed, so an unrecognised answer is the LIKELY case, not the edge."""
    assert a2p_pipeline._is_mock_brand(body) is expected


# ── the three evaluation defects, 2026-09-04 ──────────────────────────
#
# All three were found by the FIRST --tier mock run that got far enough to be
# refused, and all three had been live in this module since it was written.
# The order below is the order they have to be fixed in: without the first,
# the other two report success either way.


@pytest.mark.asyncio
async def test_a_noncompliant_bundle_is_never_submitted(transport: FakeTransport) -> None:
    """THE INSTRUMENT. A bundle that fails the policy must never be submitted.

    ⚠ This is the test that gives every other green in this file its meaning.
    Before it, the chain checked only that Twilio ACCEPTED each POST: all five
    steps returned 2xx, both bundles were `twilio-rejected` seconds later, and
    the harness still exited 0. A 2xx is proof of receipt, never of validity.
    """
    transport.script = {
        "/Evaluations": {
            "sid": "EV1",
            "status": "noncompliant",
            "results": [
                {
                    "friendly_name": "Primary Customer Profile Bundle",
                    "passed": False,
                    "failure_reason": "Primary customer profile bundle is null",
                    "fields": [
                        {
                            "friendly_name": "Supporting Bundle Status",
                            "object_field": "bundle_status",
                            "passed": False,
                            "failure_reason": "The status of the Primary Customer "
                            "Profile must be in an approved state or in-review state",
                        }
                    ],
                }
            ],
        }
    }
    with pytest.raises(a2p.A2PError) as caught:
        await a2p_pipeline.advance_submission(_row(), CREDS, stop_before_paid=True)

    # ⚠ The submission must NOT have happened. Evaluating after submitting is
    # what left six `twilio-rejected` profiles on the account: the status write
    # is one-shot and a rejected bundle cannot be reused.
    submits = [
        (m, p) for m, p in transport.calls if m == "POST" and p.endswith("/CustomerProfiles/BU")
    ]
    assert not submits, "the bundle was submitted despite failing its evaluation"
    assert caught.value.step == "evaluate_profile"
    # Verbatim, both levels — the requirement AND the field inside it. A
    # paraphrase here is a support ticket; "bundle is null" names the fix.
    assert "Primary customer profile bundle is null" in caught.value.reason
    assert "must be in an approved state" in caught.value.reason


@pytest.mark.asyncio
async def test_the_secondary_profile_is_assigned_to_the_primary(transport: FakeTransport) -> None:
    """ISV guide step 1.9, absent from this module until 2026-09-04.

    Without it every secondary profile is rejected with "Primary customer
    profile bundle is null" — so no customer could ever have completed A2P.
    """
    await a2p_pipeline.advance_submission(_row(), CREDS, stop_before_paid=True)
    # The primary's SID must appear as an ObjectSid on the SECONDARY profile.
    assert transport.assignments, "no entity assignments were made at all"
    assert PRIMARY in transport.assignments, (
        "the primary customer profile bundle was never assigned to the secondary — "
        "the carrier rejects that bundle as null"
    )


@pytest.mark.asyncio
async def test_the_a2p_messaging_profile_end_user_is_created(transport: FakeTransport) -> None:
    """`company_type`, which the trust product's evaluation demands.

    Reported by the carrier as "The Company Type is invalid." — it was MISSING,
    not malformed, and the module built only two end user types.
    """
    await a2p_pipeline.advance_submission(_row(), CREDS, stop_before_paid=True)
    types = transport.end_user_types
    assert "us_a2p_messaging_profile_information" in types, (
        f"the A2P messaging profile end user was never created; built {sorted(types)}"
    )


def test_company_type_is_derived_from_the_brand_kind_and_not_re_derived() -> None:
    """One source for "what kind of business is this", not two.

    ⚠ A second derivation from `ein` would agree today and drift the first time
    either changed (gotcha 28). The mapping is keyed on `brand_kind`'s output,
    so a new brand kind raises rather than inheriting a plausible default.
    """
    from helixis.comms.a2p import _COMPANY_TYPE_BY_BRAND_KIND

    details = a2p_pipeline._details_from_row(_row())
    assert a2p.company_type(details) == _COMPANY_TYPE_BY_BRAND_KIND[a2p.brand_kind(details)]
    sole = a2p_pipeline._details_from_row(_row(ein=None))
    assert a2p.brand_kind(sole) == "SOLE_PROPRIETOR"
    assert a2p.company_type(sole) == _COMPANY_TYPE_BY_BRAND_KIND["SOLE_PROPRIETOR"]
    assert set(_COMPANY_TYPE_BY_BRAND_KIND) == {"STANDARD", "SOLE_PROPRIETOR"}, (
        "a new brand kind must state its company type here, not inherit one"
    )


@pytest.mark.asyncio
async def test_a_missing_primary_profile_refuses_before_creating_anything() -> None:
    """Naming the env var beats building a bundle guaranteed to be rejected."""
    creds = A2PCredentials(
        account_sid="ACtest", auth_token="tok", webhook_base_url="https://occupella.example"
    )
    with pytest.raises(a2p.A2PError) as caught:
        await a2p_pipeline.advance_submission(_row(), creds, stop_before_paid=True)
    assert caught.value.step == "primary_profile"
    assert "HELIXIS_TWILIO__PRIMARY_PROFILE_SID" in caught.value.reason


# ── the campaign poll, 2026-09-04 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_the_campaign_poll_asks_for_the_campaign_it_holds(transport: FakeTransport) -> None:
    """The SID belongs IN the URL, exactly as the brand poll has always done.

    ⚠ Without it the call lands on the LIST endpoint, which answers 200 with a
    `{"compliance": [...]}` wrapper. Nothing raises and nothing logs —
    `campaign_status` is simply absent at the top level, `map_campaign_status`
    declines to invent a state, and `poll_status` returns `{}` every time. The
    campaign QE2c6890da… was already VERIFIED while the poller reported no
    change, and `ensure_number` only runs at `approved`, so the registration
    stalls one step from done with nothing to show for it.
    """
    transport.script = {"Usa2p": {"campaign_status": "VERIFIED"}}
    row = _row(status="campaign_pending", campaign_sid="QE1", messaging_service_sid="MG1")
    updates = await a2p_pipeline.poll_status(row, CREDS)

    polled = [p for m, p in transport.calls if m == "GET" and "/Compliance/Usa2p" in p]
    assert polled, "the campaign was never polled at all"
    assert polled[0].endswith("/Usa2p/QE1"), (
        f"polled the LIST endpoint, not the campaign: {polled[0]} — a 200 with a "
        "wrapper body means this stalls silently forever"
    )
    assert updates.get("status") == "approved"
    assert updates.get("campaign_approved_at") is not None


# ── the job title enum, 2026-09-04 ────────────────────────────────────


def test_the_job_title_the_form_suggested_is_coerced_not_sent() -> None:
    """ "Owner" is what the form's placeholder said, and the carrier rejects it.

    ⚠ Measured, not assumed: the API reports `job_position` as a bare String
    (`GET /EndUserTypes/authorized_representative_1`), so each value was put
    through the policy evaluation. "Owner", "Manager", "President" and
    "General Manager" all come back "Job Level is invalid."; the seven in
    `JOB_POSITIONS` pass. Six green harness runs missed this because the
    fixture says "Director" — the one value that works.
    """
    payload = a2p.representative_end_user_payload(
        a2p_pipeline._details_from_row(_row(rep_job_position="Owner"))
    )
    attrs = payload["Attributes"]
    # The customer's own words survive on the free-text field...
    assert attrs["business_title"] == "Owner"
    # ...while the enum field carries something the carrier accepts.
    assert attrs["job_position"] == "Other"
    assert attrs["job_position"] in a2p.JOB_POSITIONS


def test_a_valid_job_title_is_passed_through_untouched() -> None:
    """Coercion must not flatten every answer to "Other"."""
    for good in a2p.JOB_POSITIONS:
        payload = a2p.representative_end_user_payload(
            a2p_pipeline._details_from_row(_row(rep_job_position=good))
        )
        assert payload["Attributes"]["job_position"] == good
    # Case-insensitively, because "ceo" is what people type.
    assert a2p.job_position("ceo") == "CEO"
    assert a2p.job_position("  director ") == "Director"
    # ⚠ Never empty: an unanswered field must still send a valid member.
    assert a2p.job_position(None) == "Other"
    assert a2p.job_position("") == "Other"
