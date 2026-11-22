"""A2P registration API (api/routes/a2p.py) — the white-label application.

Offline: the DB "pool" is an in-memory fake that interprets the exact SQL
shapes the route issues (same approach as ``test_sms_routes.py``), so these
pin real semantics rather than SQL strings.

The properties that matter here are not CRUD. They are:

  * **Admin-or-refuse, fail closed.** Registering a brand is a legal
    attestation naming a real person as the authorized representative. An
    unverifiable role must refuse, not assume.
  * **Submit means QUEUED and the status must never run ahead of the row.**
    A response claiming the carriers have it, when the runner has not yet
    called anything, is gotcha 70's defect pointed at a customer instead of
    at a ledger.
  * **A blank EIN is ABSENT, not ``""``.** No EIN registers a sole-proprietor
    brand and an EIN registers standard; the carriers reject the wrong kind,
    so the distinction has to survive the round trip.
  * **Validation is strict at submit and lenient at save.** A rejected brand
    costs a fee and a 10-15 day wait to discover a typo.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.routes import a2p as a2p_routes
from helixis.comms import a2p_defaults

COMPANY_A = "11111111-1111-1111-1111-111111111111"

#: A registration with nothing missing — `a2p.validate` returns [] for it.
COMPLETE = {
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
    "opt_in_description": "Residents give their number on the lease and may reply STOP.",
    # ⚠ TWO. LOW_VOLUME refuses a campaign with fewer than two samples
    # (`a2p.MIN_SAMPLE_MESSAGES`, raised 2026-09-04 after the carrier said
    # so on the first live mock run). A one-sample fixture no longer means
    # "a complete registration" — it means one the carrier rejects.
    "sample_messages": [
        "Occupella: your work order is scheduled Tue. Reply STOP to opt out.",
        "Occupella: rent of $1,450 is due Mar 1. Reply STOP, HELP for help.",
    ],
}


class FakePool:
    """Interprets the route's SQL shapes against one in-memory row."""

    def __init__(self, row: dict[str, Any] | None = None, *, role: str | None = "admin") -> None:
        self.row = row
        self.role = role
        self.role_lookup_raises = False
        self.executed: list[str] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split())
        if "FROM public.company_users" in q:
            if self.role_lookup_raises:
                raise RuntimeError("pool down")
            return {"role": self.role} if self.role else None
        if "FROM public.helixis_a2p_registrations" in q:
            return dict(self.row) if self.row else None
        raise AssertionError(f"unexpected fetchrow SQL: {q}")

    async def execute(self, query: str, *args: Any) -> None:
        q = " ".join(query.split())
        self.executed.append(q)
        if q.startswith("INSERT INTO public.helixis_a2p_registrations"):
            cols = ["company_id"] + [c for c in a2p_routes._DETAIL_COLUMNS] + ["sample_messages"]
            # The route's INSERT lists company_id, then the detail columns in
            # a fixed order, then sample_messages.
            ordered = [
                "company_id",
                "legal_business_name",
                "ein",
                "business_type",
                "business_vertical",
                "website",
                "street",
                "city",
                "region",
                "postal_code",
                "iso_country",
                "rep_first_name",
                "rep_last_name",
                "rep_email",
                "rep_phone",
                "rep_job_position",
                "use_case",
                "campaign_description",
                "opt_in_description",
                "sample_messages",
            ]
            assert set(ordered) == set(cols)
            new = dict(zip(ordered, args, strict=True))
            base = {
                "status": (self.row or {}).get("status", "draft"),
                "failure_reason": (self.row or {}).get("failure_reason"),
                "submitted_at": (self.row or {}).get("submitted_at"),
                "brand_approved_at": None,
                "campaign_approved_at": None,
            }
            new.pop("company_id")
            self.row = {**base, **new}
            return
        if q.startswith("UPDATE public.helixis_a2p_registrations"):
            assert self.row is not None
            self.row["status"] = "submitted"
            self.row["submitted_at"] = args[1]
            self.row["failure_reason"] = None
            return
        raise AssertionError(f"unexpected execute SQL: {q}")


def _stored(**overrides: Any) -> dict[str, Any]:
    """A row as the DB would hold it (samples as a jsonb string)."""
    row: dict[str, Any] = {
        **{k: v for k, v in COMPLETE.items() if k != "sample_messages"},
        "sample_messages": json.dumps(COMPLETE["sample_messages"]),
        "status": "draft",
        "failure_reason": None,
        "submitted_at": None,
        "brand_approved_at": None,
        "campaign_approved_at": None,
    }
    row.update(overrides)
    return row


def _make_app(pool: FakePool, monkeypatch: pytest.MonkeyPatch, *, authed: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(a2p_routes.router, prefix="/api/v1")
    monkeypatch.setattr(a2p_routes, "get_pool", lambda: pool)
    if authed:
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
            user_id="user-a", email="a@example.com", company_ids=[COMPANY_A]
        )
    return app


# ── the admin floor ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path"),
    [("PUT", "/api/v1/a2p/registration"), ("POST", "/api/v1/a2p/registration/submit")],
)
def test_a_member_cannot_register_the_business(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    """Registering names a real person as the authorized representative and
    attests to the business. That is a strictly stronger act than managing
    your own company's consent rows."""
    app = _make_app(FakePool(_stored(), role="member"), monkeypatch)
    resp = TestClient(app).request(method, path, json=COMPLETE)
    assert resp.status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [("PUT", "/api/v1/a2p/registration"), ("POST", "/api/v1/a2p/registration/submit")],
)
def test_an_unverifiable_role_refuses(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    """FAIL CLOSED. A role lookup that errors is not an admin — the same
    posture as ``buildium_tools._require_manager_role``."""
    pool = FakePool(_stored())
    pool.role_lookup_raises = True
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).request(method, path, json=COMPLETE)
    assert resp.status_code == 403


def test_any_member_may_read_the_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Everyone needs to see why Leasing is closed, so the READ is open. Only
    the writes carry the admin floor."""
    app = _make_app(FakePool(_stored(), role="member"), monkeypatch)
    resp = TestClient(app).get("/api/v1/a2p/registration")
    assert resp.status_code == 200
    assert resp.json()["status"] == "draft"


# ── the empty state ───────────────────────────────────────────────────


def test_a_company_with_no_row_reads_as_an_empty_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """NOT a 404. "Not started" is a real state of this flow and the form's
    job is to be fillable; a 404 makes the client invent the empty shape."""
    app = _make_app(FakePool(None), monkeypatch)
    body = TestClient(app).get("/api/v1/a2p/registration").json()
    assert body["status"] == "draft"
    assert body["editable"] is True
    assert body["problems"], "an empty draft must report what it still needs"
    # The defaults come from BusinessDetails, so they cannot drift from the
    # field contract (gotcha 25 — one home per rule).
    assert body["details"]["iso_country"] == "US"
    # The tier is a PRICE decision that lives in one place (gotcha 77) — read it
    # rather than restating it, or a literal here silently outranks the constant.
    assert body["details"]["use_case"] == a2p_defaults.USE_CASE


# ── the compliance text is SERVER-FILLED, not merely defaulted ────────


def test_a_client_cannot_set_the_fields_carriers_reject_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ THE POINT OF THE SHRINK. The campaign description, opt-in text,
    samples and vertical are facts about what THIS SOFTWARE does — identical
    for every customer, and the three fields the form's own hint called "the
    most common reason a registration is rejected".

    A default a caller can override is a field that can be wrong. This posts
    hostile values for all four and requires the stored row to carry OURS —
    not a merge, not a preference.
    """
    pool = FakePool(None)
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).put(
        "/api/v1/a2p/registration",
        json={
            **COMPLETE,
            "business_vertical": "GAMBLING",
            "campaign_description": "we text whoever we like about anything",
            "opt_in_description": "they gave us their number",
            "sample_messages": ["buy now!!! http://bit.ly/x"],
            "use_case": "MARKETING",
            "consent_attested": True,
        },
    )
    assert resp.status_code == 200
    details = resp.json()["details"]
    assert details["business_vertical"] == a2p_defaults.BUSINESS_VERTICAL
    assert details["campaign_description"] == a2p_defaults.CAMPAIGN_DESCRIPTION
    assert details["use_case"] == a2p_defaults.USE_CASE
    assert "they gave us their number" not in details["opt_in_description"]
    assert resp.json()["sample_messages"] == a2p_defaults.sample_messages(
        COMPLETE["legal_business_name"]
    )


def test_the_consent_checkbox_is_the_one_thing_the_customer_still_decides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Their own paperwork is a fact we can neither enforce nor verify, so it
    is theirs to attest. Unticked must BLOCK rather than quietly registering a
    campaign with no consent basis in their name."""
    pool = FakePool(None)
    app = _make_app(pool, monkeypatch)
    client = TestClient(app)

    unticked = client.put(
        "/api/v1/a2p/registration", json={**COMPLETE, "consent_attested": False}
    ).json()
    assert unticked["details"]["opt_in_description"] == ""
    assert unticked["problems"], "an unticked attestation must block the submission"

    ticked = client.put(
        "/api/v1/a2p/registration", json={**COMPLETE, "consent_attested": True}
    ).json()
    assert ticked["details"]["opt_in_description"]
    assert not ticked["problems"], f"a complete form still reports problems: {ticked['problems']}"


# ── save is lenient, submit is strict ─────────────────────────────────


def test_a_partial_draft_saves_and_reports_what_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool(None)
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).put(
        "/api/v1/a2p/registration", json={"legal_business_name": "Half Filled LLC"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "draft", "saving a draft must never advance the status"
    assert body["problems"], "a half-filled draft must say what it still needs"
    assert body["details"]["legal_business_name"] == "Half Filled LLC"


def test_submitting_an_incomplete_registration_is_refused_and_does_not_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ The expensive half of this check takes a fortnight. A rejected brand
    costs a registration fee and 10-15 days to discover a typo, so the cheap
    half runs before anything is queued."""
    pool = FakePool(_stored(legal_business_name="", rep_email=""))
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).post("/api/v1/a2p/registration/submit")
    assert resp.status_code == 422
    assert resp.json()["detail"]["problems"]
    assert pool.row["status"] == "draft", "a refused submit must not advance the row"
    assert not any(q.startswith("UPDATE") for q in pool.executed)


def test_submitting_a_complete_registration_queues_it(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = FakePool(_stored())
    app = _make_app(pool, monkeypatch)
    body = TestClient(app).post("/api/v1/a2p/registration/submit").json()
    assert body["status"] == "submitted"
    assert body["submitted_at"] is not None
    assert body["editable"] is False


def test_submitting_twice_does_not_queue_a_second_brand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ A brand costs money and the registry rejects duplicates, so a
    double-click returns the CURRENT state rather than an error the user
    cannot act on — they asked for it to be submitted, and it is."""
    pool = FakePool(_stored(status="submitted", submitted_at=datetime(2026, 8, 30, tzinfo=UTC)))
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).post("/api/v1/a2p/registration/submit")
    assert resp.status_code == 200
    assert resp.json()["status"] == "submitted"
    assert not any(q.startswith("UPDATE") for q in pool.executed)


def test_a_queued_registration_cannot_be_edited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Letting the details drift from what the runner is about to submit would
    mean the customer confirmed one registration and we sent another."""
    pool = FakePool(_stored(status="submitted"))
    app = _make_app(pool, monkeypatch)
    resp = TestClient(app).put("/api/v1/a2p/registration", json=COMPLETE)
    assert resp.status_code == 409
    assert not any(q.startswith("INSERT") for q in pool.executed)


def test_a_rejected_registration_can_be_fixed_and_resubmitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`failed` is editable on purpose — a rejection is the one state where
    the customer has something to do, and re-submitting is free."""
    pool = FakePool(_stored(status="failed", failure_reason="Address could not be validated"))
    app = _make_app(pool, monkeypatch)
    body = TestClient(app).get("/api/v1/a2p/registration").json()
    assert body["editable"] is True
    # The carrier's own words, verbatim -- a paraphrase is unactionable.
    assert body["failure_reason"] == "Address could not be validated"
    assert TestClient(app).put("/api/v1/a2p/registration", json=COMPLETE).status_code == 200


# ── the EIN distinction ───────────────────────────────────────────────


def test_a_blank_ein_is_stored_as_absent_not_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ Absence is MEANINGFUL. No EIN registers a sole-proprietor brand and
    an EIN registers standard; the registry rejects the wrong kind. So `""`
    off an HTML form must not be stored as a value — that would make a sole
    proprietor look like a business that supplied a tax ID."""
    pool = FakePool(None)
    app = _make_app(pool, monkeypatch)
    TestClient(app).put("/api/v1/a2p/registration", json={**COMPLETE, "ein": "   "})
    assert pool.row["ein"] is None


def test_a_supplied_ein_survives_the_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction — dropping a real EIN would register the wrong
    brand kind just as surely (gotcha 38: test both sides of an exclusion)."""
    pool = FakePool(None)
    app = _make_app(pool, monkeypatch)
    body = TestClient(app).put("/api/v1/a2p/registration", json=COMPLETE).json()
    assert pool.row["ein"] == "12-3456789"
    assert body["details"]["ein"] == "12-3456789"


# ── the claim the response is allowed to make ─────────────────────────


def test_no_response_string_claims_a_carrier_has_the_registration() -> None:
    """⚠ Submit QUEUES it. The runner does the carrier calls, and until it
    lands nothing may say otherwise.

    This is gotcha 70 pointed at a customer instead of at a ledger: reporting
    an outcome that has not happened. The status vocabulary is the row's, so
    the guard is that no user-facing string in the module asserts delivery to
    a third party.

    ⚠ **AST, and DOCSTRINGS ARE EXCLUDED STRUCTURALLY.** The module explains
    this rule by quoting the claim it forbids — a source-wide substring scan
    fires on the sentence documenting the guard, which is gotcha 40 and is
    exactly what the first version of this test did. Excluding docstrings by
    position (a string that IS the first statement of a module, class or
    function) needs no comment handling and cannot be defeated by reflowing:
    a comment is never a `Constant` node, and a docstring's position is a
    structural fact rather than a textual one.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(a2p_routes))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    literals = [
        n.value.lower()
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]
    assert literals, "no string literals found -- this scan is not reading the module"

    for claim in ("sent to the carrier", "submitted to the carrier", "carriers have"):
        offenders = [s for s in literals if claim in s]
        assert not offenders, (
            f"a string claims {claim!r}, which has not happened at that point: {offenders}"
        )


def test_the_save_upsert_never_writes_the_status_column() -> None:
    """⚠ Saving a draft must not queue it, and the FAKE POOL CANNOT SEE THAT.

    Found by mutation: adding ``status = 'submitted'`` to the upsert's SET
    clause left all 17 behavioural tests green, because the in-memory pool
    interprets the statement's SHAPE and carries the prior row's status
    forward rather than executing the assignment. In real Postgres that
    mutation queues a registration every time somebody presses "Save for
    later" — a customer would be submitted without ever pressing submit.

    So this is the compensating control, and it is gotcha 58 answered the
    OTHER way: the mutation does change behaviour, the test was blind. The
    two are distinguishable and the distinction decides whether you delete
    the branch or write this test.

    ⚠ Scoped to the STATEMENT, never the file (gotcha 59). This module's
    prose says the word "status" constantly — including the sentences
    explaining this rule — so a whole-source scan fires on its own
    documentation.
    """
    import ast
    import inspect
    import re

    src = inspect.getsource(a2p_routes)
    upserts = [
        n.value
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and "INSERT INTO public.helixis_a2p_registrations" in n.value
    ]
    assert len(upserts) == 1, f"expected exactly one upsert statement, found {len(upserts)}"
    set_clause = upserts[0].split("DO UPDATE SET", 1)[1]
    assert "EXCLUDED" in set_clause, "the SET clause did not parse -- this scan is not reading it"

    for column in ("status", "submitted_at", "brand_approved_at", "campaign_approved_at"):
        # `\b<col>\s*=` is an assignment; the column appearing as part of
        # `EXCLUDED.<col>` on a right-hand side is not.
        assigned = re.search(rf"(?<!\.)\b{column}\s*=", set_clause)
        assert not assigned, (
            f"the save upsert assigns {column!r}. Saving a draft would advance the "
            "registration's lifecycle -- only submit may do that."
        )


def test_editable_statuses_are_a_subset_of_the_status_machine() -> None:
    """The route and ``comms/a2p.py`` must agree on the vocabulary — a status
    the machine does not know would make ``editable`` unreachable or
    permanent, and nothing would fail loudly (gotcha 28: read both sides)."""
    from helixis.comms import a2p as a2p_core

    known = set(a2p_core.STATUSES) | {"failed"}
    assert set(a2p_routes.EDITABLE_STATUSES) <= known


def test_the_detail_columns_cover_every_required_field() -> None:
    """``REQUIRED_FIELDS`` is what ``validate`` checks; ``_DETAIL_COLUMNS`` is
    what the route stores. A required field the route never persists would be
    permanently missing and the form could never be completed."""
    from helixis.comms import a2p as a2p_core

    missing = sorted(set(a2p_core.REQUIRED_FIELDS) - set(a2p_routes._DETAIL_COLUMNS))
    assert not missing, f"required field(s) the route never stores: {missing}"
