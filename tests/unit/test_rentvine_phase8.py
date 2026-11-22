"""Guards for the Phase 8 harness — `scripts/rentvine_phase8.py`.

⚠ WHAT IS ACTUALLY AT RISK HERE, because it is not the arithmetic.
    This harness is the thing that will decide whether Phase 8 passed, against
    an account nobody here can reach. Its failure mode is therefore NOT a
    crash — it is reporting RECONCILED on a comparison it did not make. Three
    shapes produce that, and each has a test below:

      1. A family the sync writes and this harness never counts. The
         reconciler compares the union of both dicts, so a family missing from
         BOTH sides is simply absent and its row never appears — a silent pass.
      2. A failed API read counted as ``0``. ``rentvine_reconcile``'s whole
         header is about 0 == 0 reading as RECONCILED; this file is where that
         guarantee is most easily destroyed from outside.
      3. A ``--check`` or a missing-credential run exiting 0. A harness that
         did not run is not one that validated anything (LED-013's skip-floor).
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "rentvine_phase8.py"


def _load() -> Any:
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location("rentvine_phase8", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


p8 = _load()


class _FakeCollection:
    def __init__(self, rows: list[dict[str, Any]], documented: bool = True) -> None:
        self.rows = rows
        self.stop_was_documented = documented


class _FakeClient:
    """Answers the two read shapes the harness uses, and NOTHING else.

    It deliberately has no ``post``: a harness that reached for one would fail
    with AttributeError here rather than in front of a customer's account.
    """

    def __init__(
        self,
        paged: dict[str, Any] | None = None,
        rows: dict[str, Any] | None = None,
        leases: dict[int, Any] | None = None,
    ) -> None:
        self._paged = paged or {}
        self._rows = rows or {}
        self._leases = leases or {}
        self.calls: list[str] = []

    async def get(self, template: str, *, path_params: Any = None, **_kw: Any) -> Any:
        """``getLease`` only: a body per lease id, default an empty history."""
        self.calls.append(template)
        lease_id = (path_params or {}).get("leaseID")
        value = self._leases.get(lease_id, {"lease": {"leaseID": lease_id}, "transactions": []})
        if isinstance(value, Exception):
            raise value
        return value

    async def iter_all(self, template: str, **_kw: Any) -> Any:
        self.calls.append(template)
        value = self._paged.get(template, _FakeCollection([]))
        if isinstance(value, Exception):
            raise value
        return value

    async def get_rows(self, template: str, **_kw: Any) -> Any:
        self.calls.append(template)
        value = self._rows.get(template, [])
        if isinstance(value, Exception):
            raise value
        return value


def _paged_ok(n: int = 1) -> dict[str, Any]:
    from helixis.rentvine import sync

    return {path: _FakeCollection([{"id": i + 1} for i in range(n)]) for _t, path in sync.SOURCES}


# ---------------------------------------------------------------------------
# 1. Every family the sync writes is a family this harness compares.
# ---------------------------------------------------------------------------


def test_every_family_the_sync_writes_is_compared() -> None:
    """A seventh source added to the sync must not vanish from the comparison.

    This is the load-bearing guard in the file. The reconciler compares the
    UNION of the two dicts, so a family absent from both sides produces no row
    at all — the mirror could be missing an entire entity type and the verdict
    would still read RECONCILED.
    """
    from helixis.rentvine import sync

    # Re-pointed 2026-10-01: every table the sync WRITES, which now includes
    # the lease-tenant links (they ride on the lease export's rows).
    written = set(sync.SYNC_STATE_STAGE)
    assert written >= {t for t, _ in sync.SOURCES} | {
        "helixis_leases",
        "helixis_units",
        "helixis_bills",
    }
    assert set(p8._families()) == written


def test_the_family_map_is_derived_rather_than_retyped() -> None:
    """It reads ``sync.SOURCES``; it does not carry its own copy of the list.

    A second hand-typed list is the drift gotcha 25 exists to stop, and here it
    drifts in the silent direction (a family nobody compares).
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_families")
    names = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert "SOURCES" in names, "the family map must be read from sync.SOURCES"


# ---------------------------------------------------------------------------
# 2. An unread count is None, never zero.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_api_read_counts_as_unread_not_as_zero() -> None:
    """The whole subject of the reconciler, at the layer that feeds it.

    A family whose fetch raised must be ``None``. Counting it ``0`` against an
    empty mirror is 0 == 0, which the reconciler would — correctly, on the
    input it was handed — report as RECONCILED.
    """
    from helixis.rentvine import sync

    paged = _paged_ok()
    paged["/vendors"] = RuntimeError("502 from Rentvine")
    client = _FakeClient(paged, {sync.UNITS_PATH: []})

    counts, provenance = await p8._api_counts(client)

    assert counts["helixis_vendors"] is None
    assert provenance["helixis_vendors"]["read"] is False
    assert "502" in provenance["helixis_vendors"]["error"]
    # and the reconciler must refuse it rather than call it a match
    import rentvine_reconcile as rec

    out = rec.reconcile({"helixis_vendors": None}, {"helixis_vendors": 0})
    assert out.ok is False


@pytest.mark.asyncio
async def test_units_are_unread_when_the_property_fetch_failed() -> None:
    """No work-list means the question was never asked.

    Units are fetched per property (the sync's deliberate N+1), so a failed
    property read leaves zero properties to walk — and walking zero properties
    produces a count of 0 that is indistinguishable from an account with no
    units. It must be ``None``.
    """

    paged = _paged_ok()
    paged["/properties"] = RuntimeError("timeout")
    client = _FakeClient(paged)

    counts, provenance = await p8._api_counts(client)

    assert counts["helixis_properties"] is None
    assert counts["helixis_units"] is None
    assert "work-list" in provenance["helixis_units"]["error"]


@pytest.mark.asyncio
async def test_a_unit_fetch_that_fails_midway_is_unread_not_a_partial_count() -> None:
    """A partial walk is a smaller number, which reads as a mirror that OVER-holds.

    That is the worse direction: ``reconcile`` reports EXTRA, which its own
    wording calls "not the benign direction: duplicate upserts, or rows from
    another company or PMS reaching a count that forgot to scope" — sending an
    operator after a scoping bug that does not exist.
    """
    from helixis.rentvine import sync

    paged = _paged_ok(n=2)
    client = _FakeClient(paged, {sync.UNITS_PATH: RuntimeError("boom")})

    counts, provenance = await p8._api_counts(client)

    assert counts["helixis_units"] is None
    assert provenance["helixis_units"]["read"] is False


@pytest.mark.asyncio
async def test_a_clean_read_reports_counts_and_the_stop_provenance() -> None:
    """The positive direction, so a harness that refused everything cannot pass.

    A one-sided test (only the failure cases) is satisfied by an ``_api_counts``
    that returns ``None`` for every family — which would make every Phase 8 run
    report NOT RECONCILED forever (gotchas 38/51).
    """
    from helixis.rentvine import sync

    paged = _paged_ok(n=3)
    paged[sync.LEASES_EXPORT_PATH] = _FakeCollection(
        [
            {
                "leaseID": 1,
                "tenants": [{"contactID": 5, "isActive": 1}, {"contactID": 6, "isActive": 0}],
                # Carried beside the lease by the client (optional_companions).
                "balances": {"unpaidTotalAmount": 250},
                "unpaidCharges": None,
            }
        ]
    )
    history = {
        "lease": {"leaseID": 1},
        "transactions": [
            # One bare, signed by balance effect; one wrapped, unsigned.
            {"transactionID": 11, "transactionTypeID": 2, "amount": "-800.00", "isVoided": 0},
            {"transaction": {"transactionID": 12, "transactionTypeID": 1, "amount": "800.00"}},
        ],
    }
    client = _FakeClient(paged, {sync.UNITS_PATH: [{"id": 9}]}, leases={1: history})

    counts, provenance = await p8._api_counts(client)
    assert counts["helixis_lease_balances"] == 1  # it owes, so it is in the snapshot
    # Lease history, from getLease, and what its rows looked like (counts only).
    assert counts["helixis_lease_transactions"] == 2
    txn = provenance["helixis_lease_transactions"]
    assert txn["item_shape"] == {"wrapped": 1, "bare": 1}
    assert txn["negative_amounts_by_type"] == {"2": 1}

    assert counts["helixis_properties"] == 3
    assert counts["helixis_leases"] == 1
    assert counts["helixis_lease_tenants"] == 1  # the ACTIVE tenant only
    assert counts["helixis_units"] == 3  # one per property, three properties
    assert provenance["helixis_properties"]["stop_was_documented"] is True
    assert all(v is not None for v in counts.values())


@pytest.mark.asyncio
async def test_a_lease_whose_history_cannot_be_read_makes_the_count_unknown() -> None:
    from helixis.rentvine import sync, transport
    from helixis.rentvine.client import RentvineAPIError

    paged = _paged_ok(n=1)
    paged[sync.LEASES_EXPORT_PATH] = _FakeCollection([{"leaseID": 1}, {"leaseID": 2}])
    leases = {2: RentvineAPIError(500, transport.Failure.UNAVAILABLE, True, "boom")}
    counts, provenance = await p8._api_counts(_FakeClient(paged, {}, leases=leases))
    assert counts["helixis_lease_transactions"] is None
    assert "lease 2" in provenance["helixis_lease_transactions"]["error"]
    # A lease answered without a list is unknown too, never zero.
    blank = {1: {"lease": {"leaseID": 1}}}
    counts, _ = await p8._api_counts(_FakeClient(paged, {}, leases=blank))
    assert counts["helixis_lease_transactions"] is None


@pytest.mark.asyncio
async def test_a_guessed_stop_is_reported_rather_than_hidden() -> None:
    """gotcha 149: whether the END was a server fact is the thing to carry out.

    The harness must not flatten it — a collection the loop guessed its way to
    the end of may be short, and that is a Phase 8 finding about the spec's
    pagination headers rather than a sync fault.
    """

    paged = _paged_ok()
    paged["/tenants"] = _FakeCollection([{"id": 1}], documented=False)
    client = _FakeClient(paged)

    _counts, provenance = await p8._api_counts(client)

    assert provenance["helixis_tenants"]["stop_was_documented"] is False
    assert provenance["helixis_properties"]["stop_was_documented"] is True


@pytest.mark.asyncio
async def test_a_lease_export_with_no_server_named_end_is_unread() -> None:
    """2026-10-01: leases are counted from the export, and only when Rentvine
    named its end. A guessed stop is a NUMBER that may be short, and a short
    lease count is what made the reconciler call real leases "extra"."""
    from helixis.rentvine import sync

    paged = _paged_ok()
    paged[sync.LEASES_EXPORT_PATH] = _FakeCollection([{"leaseID": 1}], documented=False)
    counts, provenance = await p8._api_counts(_FakeClient(paged))
    assert counts["helixis_leases"] is None
    assert counts["helixis_lease_tenants"] is None
    assert "guess" in provenance["helixis_leases"]["error"]


# ---------------------------------------------------------------------------
# 3. The total that idempotency subtracts.
# ---------------------------------------------------------------------------


def test_an_unreadable_family_makes_the_total_unreadable() -> None:
    """Idempotency subtracts two totals; a dropped family hides an insert.

    If one family is silently omitted from both totals the subtraction still
    balances, so a re-run that inserted rows into exactly that table reports as
    idempotent.
    """
    assert p8._total({"a": 1, "b": 2}) == 3
    assert p8._total({"a": 1, "b": None}) is None

    import rentvine_reconcile as rec

    assert rec.idempotency(None, 5).ok is False


# ---------------------------------------------------------------------------
# 4. A run that did not run is not a pass.
# ---------------------------------------------------------------------------


def test_check_exits_two_when_anything_is_missing(monkeypatch: Any, capsys: Any) -> None:
    for name in (*p8.CRED_VARS, p8.DB_VAR):
        monkeypatch.delenv(name, raising=False)

    assert p8.main(["--check"]) == 2
    out = capsys.readouterr().out
    assert "UNSET" in out
    # set/unset only — a harness that printed a secret would put it in the
    # operator's shell history and in any log they paste
    assert "no values are printed" in out


def test_check_exits_zero_only_when_everything_is_set(monkeypatch: Any) -> None:
    for name in p8.CRED_VARS:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv(p8.DB_VAR, "postgres://ignored")
    assert p8.main(["--check"]) == 0


def test_missing_credentials_exit_two_rather_than_running(monkeypatch: Any, capsys: Any) -> None:
    for name in (*p8.CRED_VARS, p8.DB_VAR):
        monkeypatch.delenv(name, raising=False)

    assert p8.main(["--company-id", "c1"]) == 2
    assert "not a pass" in capsys.readouterr().out


def test_a_run_without_a_company_id_is_refused(monkeypatch: Any, capsys: Any) -> None:
    """Both the mirror count and the sync are scoped by it.

    An unscoped count reaches another tenant's rows, which makes the two
    numbers look CLOSER rather than further apart — gotcha 47, hiding as a pass.
    """
    for name in p8.CRED_VARS:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv(p8.DB_VAR, "postgres://ignored")

    assert p8.main([]) == 2
    assert "--company-id is required" in capsys.readouterr().out


def test_the_harness_does_not_flip_the_dark_flag_for_itself() -> None:
    """``HELIXIS_RENTVINE__ENABLED`` defaults false and the harness must respect it.

    ``sync_company`` raises ``RentvineSyncDisabled`` while the connector is
    dark, and the tempting fix is to hand it a ``Settings`` with the flag
    flipped. That would let a validation run exercise a path the deployment
    refuses — and then report RECONCILED about behaviour no customer can
    reach, which is the one thing a Phase 8 verdict must never do. It names the
    variable and stops.

    The check is on what it CONSTRUCTS: the harness builds a ``RentvineSettings``
    (the per-company credential shape, where ``enabled=True`` only says "these
    credentials are usable") and never an app-wide ``Settings``, which is the
    object ``sync_company`` reads the deployment flag from.
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    constructed = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "Settings" not in constructed, (
        "the harness must not build an app Settings — that is where the dark "
        "flag lives, and forging it would run a path the deployment refuses"
    )
    src = SCRIPT.read_text(encoding="utf-8")
    assert "RentvineSyncDisabled" in src, "the disabled state must be caught and explained"


# ---------------------------------------------------------------------------
# 5. Read-only against Rentvine, by construction rather than by promise.
# ---------------------------------------------------------------------------


def test_the_harness_never_calls_the_one_write_method() -> None:
    """``RentvineClient.post`` is the only verb that can change the account.

    The module docstring claims this harness is read-only against Rentvine, and
    a docstring cannot fail (gotcha 48). This is the check that can.
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    called = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "post" not in called


def test_the_mirror_count_sql_comes_from_the_reconciler() -> None:
    """Both of its predicates are load-bearing and one hides when omitted.

    Without ``pms`` the count includes Buildium's rows and can never match;
    without ``company_id`` it reads another tenant's. Re-typing the SQL here is
    how one of them goes missing, so the harness calls the reconciler's own
    builder — which states the shape in one place.
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_mirror_counts"
    )
    called = {
        n.func.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "mirror_count_sql" in called

    sql = p8.rec.mirror_count_sql("helixis_properties")
    assert "company_id = $1" in sql and "pms = $2" in sql
