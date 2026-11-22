"""A customer pastes two keys; the subdomain is found and PROVEN, or refused.

Rentvine puts the account code in the HOSTNAME (``servers[0].url`` is
``https://{account}.rentvine.com/api/manager``) and documents no identity
endpoint among its 143 paths, so there is no server to ask "which account is
this key for?" until the answer is already known. Buildium is one host for
everyone, which is why keys alone suffice there and why "just do what Buildium
does" is not available here.

Three properties, and the first is a security one that predates the feature:

  - **A candidate cannot leave ``*.rentvine.com``.** The code is interpolated
    into the URL's AUTHORITY, so before discovery existed
    ``PUT /rentvine/credentials`` already accepted ``evil.example/x`` under a
    length bound alone and the stored ``base_url`` drives the UNATTENDED worker
    sync — a repeating outbound request from our infrastructure to an
    attacker-named host carrying ``Authorization: Basic``. Generating
    candidates from a customer-controlled company NAME would have widened that,
    so the guard is asserted in BOTH directions here (gotchas 38/51): the
    escapes it must refuse and the real subdomains it must spare.

  - **A hit is EVIDENCE, and two hits are a refusal.** Under one-credential-
    one-tenant two candidates cannot both authenticate, so if two do the model
    is wrong (a wildcard host routing on the credential alone is the likely
    shape) and picking one would store a guess while hiding that. Probing every
    candidate rather than stopping at the first is what makes it observable.

  - **Nothing unverified is stored.** No hit is an honest 422 naming one field,
    never a plausible wrong host written to the row the worker syncs from
    (gotchas 55/57 — a guard that cannot see declines to assert).

⚠ The route's ORDER is a fourth property and no behavioural test can see it:
``_require_connector_enabled()`` must precede ``_resolve_account_code``, because
discovery sends the customer's credential to Rentvine and a deployment with the
connector dark must never do that on their behalf. Both orders answer 503, so
the guard walks the statements (gotchas 97/114 — present in the source, absent
in effect).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from helixis.api.routes import rentvine as rentvine_routes
from helixis.rentvine import discovery, transport
from helixis.rentvine.sync import SOURCES
from tests.unit.test_rentvine_routes import (  # noqa: F401 — `fake_pool` is a fixture
    API_KEY,
    API_SECRET,
    TEST_DATA_KEY,
    _auth,
    _client,
    _settings,
    fake_pool,
)

# ── the host guard: what it must refuse ────────────────────────────────

#: ⚠ EVERY ONE OF THESE REACHED ``base_url`` FROM A JSON BODY. The first is the
#: measured escape (host became ``evil.example``); the rest are the shapes a
#: parser bug turns into the first one.
ESCAPES = [
    "evil.example/x",
    "evil.example",
    "a.b",
    "../x",
    "//evil.example/",
    "acme:8080",
    "acme@evil.example",
    "has space",
    "-leading",
    "trailing-",
    "under_score",
    "",
    "   ",
    "a" * 64,
]

#: The other direction. A guard that refuses everything passes a one-sided test
#: and makes the connector unreachable (gotchas 38/51).
REAL_CODES = [
    ("acme", "acme"),
    ("blue-ridge", "blue-ridge"),
    ("UPPER", "upper"),  # DNS is case-insensitive; refusing would be theatre.
    ("  acme  ", "acme"),
    # ⚠ A pasted address bar brings a trailing slash, and only the EDGES are
    # stripped — so `acme/` normalises while `evil.example/x` keeps its
    # internal slash and stays in ESCAPES above. That asymmetry is the whole
    # reason both lists exist.
    ("acme/", "acme"),
    ("/acme", "acme"),
    ("a", "a"),
    ("a" * 63, "a" * 63),
]


@pytest.mark.parametrize("bad", ESCAPES)
def test_an_account_code_that_could_move_the_host_is_refused(bad: str) -> None:
    with pytest.raises(transport.RentvineTransportError):
        transport.normalize_account_code(bad)


@pytest.mark.parametrize(("given", "expected"), REAL_CODES)
def test_a_real_subdomain_survives(given: str, expected: str) -> None:
    assert transport.normalize_account_code(given) == expected


@pytest.mark.parametrize("bad", ESCAPES)
def test_base_url_refuses_the_same_inputs(bad: str) -> None:
    """``base_url`` must delegate, not carry its own looser copy (gotcha 25)."""
    with pytest.raises(transport.RentvineTransportError):
        transport.base_url(bad)


@pytest.mark.parametrize(("given", "expected"), REAL_CODES)
def test_every_accepted_code_stays_inside_the_zone(given: str, expected: str) -> None:
    host = httpx.URL(transport.base_url(given)).host
    assert host == f"{expected}.rentvine.com"
    assert host.endswith(".rentvine.com")


def test_the_refusal_tells_the_customer_where_to_look() -> None:
    """The remedy is one glance at their own address bar, so it says that.

    A refusal naming a regex is an instruction nobody can carry out
    (gotchas 102/106/118/133).
    """
    with pytest.raises(transport.RentvineTransportError) as exc:
        transport.normalize_account_code("evil.example/x")
    assert "rentvine.com" in str(exc.value)
    assert "address bar" in str(exc.value).lower()


# ── candidates: what we are willing to ask about ───────────────────────


def test_the_joined_company_name_leads() -> None:
    got = discovery.candidate_codes(company_name="Blue Ridge Properties")
    assert got[0] == "blueridgeproperties"
    assert "blueridge" in got  # the noise-stripped form, which is very often it


def test_the_bare_first_word_is_never_a_candidate() -> None:
    """``blue`` is far likelier to be a STRANGER'S tenant than this customer's.

    Every candidate is an authentication attempt against somebody's host, so a
    guess that is mostly wrong is a cost paid by people who are not our
    customer.
    """
    got = discovery.candidate_codes(company_name="Blue Ridge Properties")
    assert "blue" not in got
    assert "ridge" not in got


def test_a_public_mail_host_is_not_a_candidate() -> None:
    got = discovery.candidate_codes(company_name="Acme", email="owner@gmail.com")
    assert "gmail" not in got
    assert "acme" in got


def test_a_company_mail_host_is_a_candidate() -> None:
    got = discovery.candidate_codes(company_name="Acme", email="owner@blueridgepm.com")
    assert "blueridgepm" in got


def test_the_candidate_list_is_capped() -> None:
    got = discovery.candidate_codes(
        company_name="One Two Three Four Five Six Seven Eight",
        email="owner@nine.example.com",
    )
    assert len(got) <= discovery.MAX_CANDIDATES


def test_the_cap_binds_when_it_can(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠ TODAY'S GENERATOR CANNOT REACH THE CAP AND THAT IS WHY THIS EXISTS.

    It emits at most five forms (joined, hyphenated, and the two noise-stripped
    variants, plus the mail label) against a ceiling of six, so the ``break``
    is a bound on a FUTURE generator rather than on this one — and a mutation
    deleting it changes no output for any company name (gotcha 58: check
    whether the mutant differs at all before pinning a distinction).

    The distinguishing input is therefore a cap low enough to bind, which is a
    real property — each candidate is an unauthenticated guess at somebody's
    hostname, and the honest fallback costs the customer one field.
    """
    monkeypatch.setattr(discovery, "MAX_CANDIDATES", 2)
    got = discovery.candidate_codes(
        company_name="Blue Ridge Properties", email="owner@blueridgepm.com"
    )
    assert len(got) == 2


def test_candidates_are_deduplicated_and_ordered() -> None:
    got = discovery.candidate_codes(company_name="Acme", email="owner@acme.com")
    assert list(got) == sorted(set(got), key=list(got).index)  # order preserved
    assert len(got) == len(set(got))


@pytest.mark.parametrize(
    "hostile",
    [
        "evil.example/x",
        "../../etc",
        "Acme evil.example",
        "!!!",
        "",
        # ⚠ THE DISTINGUISHING INPUT, and the reason the normalise call inside
        # the generator is not merely defence in depth. `_WORD_RE` already
        # strips every character that could move a host, so the punctuation
        # cases above survive a generator that skipped the guard. A single
        # token longer than a DNS label does not: it is made only of
        # characters the word split keeps, and it is what the guard refuses.
        "a" * 70,
    ],
)
def test_no_candidate_can_leave_the_zone_whatever_the_company_is_called(hostile: str) -> None:
    """The company NAME is customer-controlled, which is why this is the guard.

    A candidate generator that did not normalise would feed the host-injection
    the transport guard exists to stop, from a field an admin types — and, in
    the reachable case above, would hand ``_probe`` a code that raises out of
    ``asyncio.gather`` and 500s the route.
    """
    for code in discovery.candidate_codes(company_name=hostile, email=f"a@{hostile}"):
        assert httpx.URL(transport.base_url(code)).host.endswith(".rentvine.com")


def test_candidate_generation_cannot_see_the_credential() -> None:
    """A subdomain is resolved through DNS, which is PLAINTEXT.

    Seeding a candidate from a prefix of the API key would put key material
    into a query that travels in the clear, so the function must not be able
    to reach it at all.
    """
    params = set(inspect.signature(discovery.candidate_codes).parameters)
    assert params == {"company_name", "email"}


# ── the probe: one hit is an answer, two are a refusal ─────────────────


def _probing_client(answer) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """An AsyncClient whose transport records every request it is given."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        auth=(API_KEY, API_SECRET),
    )
    return client, seen


def _only(code: str):
    def answer(request: httpx.Request) -> httpx.Response:
        ok = request.url.host == f"{code}.rentvine.com"
        return httpx.Response(200 if ok else 404, json={"properties": []})

    return answer


@pytest.mark.asyncio
async def test_exactly_one_hit_identifies_the_account() -> None:
    client, seen = _probing_client(_only("blueridge"))
    async with client:
        got = await discovery.discover_account_code(
            api_key=API_KEY,
            api_secret=API_SECRET,
            candidates=("blueridgeproperties", "blueridge", "brp"),
            client=client,
        )
    assert got.found == "blueridge"
    assert got.ambiguous == ()
    assert got.tried == ("blueridgeproperties", "blueridge", "brp")


@pytest.mark.asyncio
async def test_no_hit_declines_rather_than_guessing() -> None:
    client, _seen = _probing_client(lambda _r: httpx.Response(404))
    async with client:
        got = await discovery.discover_account_code(
            api_key=API_KEY,
            api_secret=API_SECRET,
            candidates=("acme", "acmepm"),
            client=client,
        )
    assert got.found is None
    assert got.ambiguous == ()


@pytest.mark.asyncio
async def test_two_hits_are_reported_as_ambiguous_and_nothing_is_picked() -> None:
    """Both directions matter: no answer AND the fact that there were two.

    Returning the first would store a guess and hide that the one-credential-
    one-tenant model this module rests on does not hold.
    """
    client, _seen = _probing_client(lambda _r: httpx.Response(200, json={}))
    async with client:
        got = await discovery.discover_account_code(
            api_key=API_KEY,
            api_secret=API_SECRET,
            candidates=("acme", "acmepm"),
            client=client,
        )
    assert got.found is None
    assert got.ambiguous == ("acme", "acmepm")


@pytest.mark.asyncio
async def test_every_candidate_is_probed_even_after_a_hit() -> None:
    """Stopping at the first hit is what makes ambiguity unobservable."""
    client, seen = _probing_client(lambda _r: httpx.Response(200, json={}))
    async with client:
        await discovery.discover_account_code(
            api_key=API_KEY,
            api_secret=API_SECRET,
            candidates=("acme", "acmepm", "acmerentals"),
            client=client,
        )
    assert len(seen) == 3
    assert {r.url.host for r in seen} == {
        "acme.rentvine.com",
        "acmepm.rentvine.com",
        "acmerentals.rentvine.com",
    }


@pytest.mark.asyncio
async def test_a_candidate_that_does_not_answer_does_not_stop_the_others() -> None:
    """A miss has never been observed (``*.rentvine.com`` is egress-blocked from
    the build container, ADR 0010 blocker B1), so it may arrive as DNS failure,
    a TLS mismatch, a 404 or a 401. Every one must mean "not this one" rather
    than ending the search.
    """

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "acme.rentvine.com":
            raise httpx.ConnectError("no such host", request=request)
        if request.url.host == "acmepm.rentvine.com":
            return httpx.Response(200, json={})
        return httpx.Response(401)

    client, seen = _probing_client(answer)
    async with client:
        got = await discovery.discover_account_code(
            api_key=API_KEY,
            api_secret=API_SECRET,
            candidates=("acme", "acmepm", "acmerentals"),
            client=client,
        )
    assert got.found == "acmepm"
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_the_probe_reads_the_path_the_sync_reads() -> None:
    """gotcha 153/85: a check that exercises a path the product never uses
    proves the credentials parse and nothing about whether the product works.
    """
    client, seen = _probing_client(lambda _r: httpx.Response(404))
    async with client:
        await discovery.discover_account_code(
            api_key=API_KEY,
            api_secret=API_SECRET,
            candidates=("acme",),
            client=client,
        )
    assert seen[0].url.path.endswith(SOURCES[0][1])


@pytest.mark.asyncio
async def test_no_candidates_means_no_requests() -> None:
    client, seen = _probing_client(lambda _r: httpx.Response(200))
    async with client:
        got = await discovery.discover_account_code(
            api_key=API_KEY,
            api_secret=API_SECRET,
            candidates=(),
            client=client,
        )
    assert got.found is None
    assert got.tried == ()
    assert seen == []


@pytest.mark.asyncio
async def test_a_probe_without_a_credential_is_a_programming_error() -> None:
    """An unauthenticated GET tells us nothing about ownership, so a 200 from
    one would be a hit that means nothing at all.
    """
    with pytest.raises(ValueError):
        await discovery.discover_account_code(
            api_key="",
            api_secret=API_SECRET,
            candidates=("acme",),
        )


# ── the route ──────────────────────────────────────────────────────────


@pytest.fixture
def pool(fake_pool):  # noqa: F811 — the imported fixture is the base
    """``fake_pool`` plus the company-name read discovery makes.

    ⚠ The name is what discovery is DERIVED from, so a fixture that left it
    absent would exercise a state no deployment is in (gotchas 96/107/138).
    """

    async def fetchval(sql: str, *_args):
        if "FROM public.companies" in " ".join(sql.split()):
            return fake_pool._state.get("company_name", "Blue Ridge Properties")
        return None

    fake_pool.fetchval = AsyncMock(side_effect=fetchval)
    return fake_pool


@pytest.fixture
def client(pool):
    yield from _client(_settings(), pool)


@pytest.fixture
def dark_client(pool):
    yield from _client(_settings(rentvine_enabled=False), pool)


def _put_keys_only(c, **overrides):
    """What the customer actually sends now: two keys and nothing else."""
    body = {"api_key": API_KEY, "api_secret": API_SECRET}
    body.update(overrides)
    return c.put("/api/v1/rentvine/credentials", headers=_auth(), json=body)


def test_a_supplied_account_code_is_used_as_given(client, pool):
    """A customer reading their OWN address bar must never be overruled."""
    with patch.object(rentvine_routes.discovery, "discover_account_code") as probe:
        resp = _put_keys_only(client, account_code="TheirOwnCode")
    assert resp.status_code == 200
    probe.assert_not_called()
    assert pool._state["rows"]["company-A"]["account_code"] == "theirowncode"
    assert pool._state["rows"]["company-A"]["base_url"].startswith(
        "https://theirowncode.rentvine.com"
    )


def test_a_supplied_account_code_that_could_move_the_host_is_a_422(client, pool):
    resp = _put_keys_only(client, account_code="evil.example/x")
    assert resp.status_code == 422
    assert pool._state["rows"] == {}


def test_keys_alone_store_the_discovered_code_and_a_matching_base_url(client, pool):
    found = discovery.Discovery(found="blueridge", tried=("blueridgeproperties", "blueridge"))
    with patch.object(
        rentvine_routes.discovery,
        "discover_account_code",
        new=AsyncMock(return_value=found),
    ):
        resp = _put_keys_only(client)

    assert resp.status_code == 200
    stored = pool._state["rows"]["company-A"]
    assert stored["account_code"] == "blueridge"
    assert stored["base_url"] == transport.base_url("blueridge")
    assert resp.json()["account_code"] == "blueridge"


def test_discovery_is_handed_the_company_name_and_the_admin_email(client, pool):
    """The two non-secret facts we have. Anything else would be a guess about
    somebody's hostname made from material that must not leave the process.
    """
    pool._state["company_name"] = "Redbud Property Group"
    seen: dict[str, object] = {}

    def candidates(*, company_name: str = "", email: str = ""):
        seen["company_name"] = company_name
        seen["email"] = email
        return ("redbud",)

    found = discovery.Discovery(found="redbud", tried=("redbud",))
    with (
        patch.object(rentvine_routes.discovery, "candidate_codes", side_effect=candidates),
        patch.object(
            rentvine_routes.discovery,
            "discover_account_code",
            new=AsyncMock(return_value=found),
        ),
    ):
        assert _put_keys_only(client).status_code == 200

    assert seen["company_name"] == "Redbud Property Group"
    assert seen["email"] == "test@example.com"


def test_nothing_found_refuses_and_writes_no_row(client, pool):
    """The whole point: a wrong host in that row is synced from, unattended."""
    nothing = discovery.Discovery(found=None, tried=("acme", "acmepm"))
    with patch.object(
        rentvine_routes.discovery,
        "discover_account_code",
        new=AsyncMock(return_value=nothing),
    ):
        resp = _put_keys_only(client)

    assert resp.status_code == 422
    assert pool._state["rows"] == {}
    detail = resp.json()["detail"]
    assert "rentvine.com" in detail
    assert "address bar" in detail.lower()


def test_two_matching_subdomains_refuse_rather_than_pick_one(client, pool):
    ambiguous = discovery.Discovery(
        found=None, tried=("acme", "acmepm"), ambiguous=("acme", "acmepm")
    )
    with patch.object(
        rentvine_routes.discovery,
        "discover_account_code",
        new=AsyncMock(return_value=ambiguous),
    ):
        resp = _put_keys_only(client)

    assert resp.status_code == 422
    assert pool._state["rows"] == {}


def test_a_dark_deployment_never_sends_the_credential_anywhere(dark_client, pool):
    """503 BEFORE discovery, and no row left behind.

    A refusal that has already posted the customer's key to a third party is
    not a refusal.
    """
    with patch.object(rentvine_routes.discovery, "discover_account_code") as probe:
        resp = _put_keys_only(dark_client)

    assert resp.status_code == 503
    probe.assert_not_called()
    assert pool._state["rows"] == {}


def test_the_dark_check_precedes_discovery_in_the_source() -> None:
    """Both orders answer 503, so only the statements distinguish them.

    gotchas 97/114: order is the safety property and every behavioural test is
    blind to it.
    """
    src = Path(inspect.getfile(rentvine_routes)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "upsert_credentials"
    )

    def line_of(predicate) -> int:
        hits = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call) and predicate(n)]
        assert len(hits) == 1, f"expected exactly one call site, found {len(hits)}"
        return hits[0]

    gate = line_of(
        lambda n: isinstance(n.func, ast.Name) and n.func.id == "_require_connector_enabled"
    )
    resolve = line_of(
        lambda n: isinstance(n.func, ast.Name) and n.func.id == "_resolve_account_code"
    )
    assert gate < resolve, (
        "the dark-connector refusal must come first — discovery sends the "
        "customer's credential to Rentvine"
    )


def test_the_stored_base_url_is_always_derived_through_the_one_template() -> None:
    """A second ``f"https://{code}.rentvine.com"`` in the route would be a copy
    of the host guard that does not carry it (gotcha 25).
    """
    src = Path(inspect.getfile(rentvine_routes)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    formatted = [
        "".join(p.value for p in node.values if isinstance(p, ast.Constant))
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
    ]
    # The docstrings quote the shape in order to explain it, so the scan is on
    # STRINGS THAT COULD BE A URL rather than the file (gotchas 40/59).
    for text in [*literals, *formatted]:
        assert not text.startswith("https://"), f"hand-built Rentvine URL: {text!r}"


def test_the_module_imports_both_halves_it_needs() -> None:
    """A presence check is not a wiring check, so this is the weakest of the
    guards here and exists only to name the dependency (gotchas 94/128/141).
    """
    assert rentvine_routes.discovery is discovery
    assert rentvine_routes.transport is transport


def test_the_data_key_fixture_is_the_one_the_routes_encrypt_with() -> None:
    """Imported for the encryption assertions elsewhere; pinned so a rename in
    the sibling module fails loudly rather than silently weakening them.
    """
    assert TEST_DATA_KEY
