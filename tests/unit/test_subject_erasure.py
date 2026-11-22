"""Erasing one person, and being honest that half of it cannot be done here.

Every eraser before this was company-scoped — the §18 mirror purge on
disconnect, and ``delete_company_memory``. Both are right for what they do,
and neither can serve a tenant asking for THEIR data to be deleted: the rights
GDPR and CCPA grant belong to a person, not an account. The only two available
answers were "wipe the whole company, destroying data about everyone else" and
"do nothing".

⚠ **The design turns on the mirror being a CACHE.** ``helixis_tenants`` and its
siblings are copies of Buildium's records; delete a row and the next sync
restores it. So the obvious implementation — DELETE everywhere the subject
appears — is worse than useless: it reports success, the data comes back within
the hour, and somebody has been told their legal request was honoured when it
was not. Buildium is the controller; the request has to go there. The report
therefore has two halves and the SECOND one is the deliverable.

⚠ **And memory is matched on identifiers, never on a name.** Two people share a
surname more often than not, and this repo already shipped one confident
cross-person mistake from that exact kind of loose join. Erasing on a name
would delete a different person's records to satisfy this one's request — an
irreversible privacy harm committed in the name of privacy.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.privacy import SUBJECT_TYPES, Subject, erase_subject

COMPANY = "5ca1ab1e-0000-4000-8000-000000000002"


def _pool(conn: Any) -> Any:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


def _conn(result: str = "DELETE 2") -> Any:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=result)
    return conn


class _Memory:
    def __init__(self, deleted: int = 3, boom: bool = False) -> None:
        self._deleted = deleted
        self._boom = boom
        self.calls: list[dict[str, Any]] = []

    async def delete_subject_memory(self, **kw: Any) -> int:
        self.calls.append(kw)
        if self._boom:
            raise RuntimeError("mem0 unreachable")
        return self._deleted


# ── the subject has to be identifiable ───────────────────────────────


def test_a_subject_with_no_identifier_is_refused() -> None:
    """A name is not an identifier. Constructing a subject from one would let
    the whole erasure run on a fuzzy match."""
    with pytest.raises(ValueError, match="buildium_id"):
        Subject(entity_type="tenant")


def test_an_unknown_entity_type_is_refused() -> None:
    """The type selects which rows are reachable; a typo would silently erase
    nothing and report success."""
    with pytest.raises(ValueError, match="entity_type"):
        Subject(entity_type="landlord", email="x@example.com")


def test_either_identifier_alone_is_enough() -> None:
    assert Subject(entity_type="tenant", buildium_id=1).buildium_id == 1
    assert Subject(entity_type="owner", email="a@b.c").email == "a@b.c"


# ── what it erases ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_it_erases_the_helixis_authored_rows() -> None:
    conn = _conn()
    report = await erase_subject(
        _pool(conn), COMPANY, Subject(entity_type="tenant", buildium_id=115937, email="a@b.c")
    )
    assert report.erased["helixis_entity_aliases"] == 2
    assert report.erased["helixis_risk_flags"] == 2
    assert report.erased["helixis_payment_promises"] == 2


@pytest.mark.asyncio
async def test_every_delete_is_company_scoped() -> None:
    """⚠ Gotcha 47 at its sharpest. An unscoped DELETE here does not return
    another tenant's rows — it DESTROYS them, in every company at once, from a
    path whose whole job is deleting things."""
    conn = _conn()
    await erase_subject(
        _pool(conn), COMPANY, Subject(entity_type="tenant", buildium_id=1, email="a@b.c")
    )
    assert conn.execute.await_count >= 5
    for call in conn.execute.await_args_list:
        sql = call.args[0]
        assert "company_id = $1::uuid" in sql, f"unscoped delete: {sql}"
        assert call.args[1] == COMPANY


@pytest.mark.asyncio
async def test_an_email_only_subject_skips_the_id_keyed_tables() -> None:
    """Both directions. A subject given only an email must not have its
    ``entity_id`` deletes run with a None — that would either error or, worse,
    match rows whose entity_id is NULL."""
    conn = _conn()
    report = await erase_subject(_pool(conn), COMPANY, Subject(entity_type="owner", email="a@b.c"))
    assert "helixis_entity_aliases" not in report.erased
    assert "helixis_payment_promises" in report.erased


@pytest.mark.asyncio
async def test_one_failing_table_does_not_abort_the_rest() -> None:
    """A partial erasure that names the part that failed is useful; one that
    aborts halfway leaving no record is not."""
    conn = MagicMock()
    conn.execute = AsyncMock(
        side_effect=[RuntimeError("boom"), "DELETE 1", "DELETE 1", "DELETE 1", "DELETE 1"]
    )
    report = await erase_subject(
        _pool(conn), COMPANY, Subject(entity_type="tenant", buildium_id=1, email="a@b.c")
    )
    assert report.failed, "the failure must be recorded"
    assert report.erased, "the surviving deletes must still have run"


# ── what it refuses to claim ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_mirror_is_reported_as_surviving() -> None:
    """⚠ THE point of the whole module. A DELETE against helixis_tenants would
    succeed and be undone by the next sync, so reporting it as erased tells an
    operator a legal request was satisfied when it was not."""
    report = await erase_subject(
        _pool(_conn()), COMPANY, Subject(entity_type="tenant", buildium_id=1)
    )
    assert "helixis_tenants" in report.not_erasable_here
    assert "helixis_tenants" not in report.erased
    reason = report.not_erasable_here["helixis_tenants"]
    assert "sync" in reason and "Buildium" in reason, (
        "the reason must say WHY and where the request has to go — 'some data "
        "remains' is not something an operator can act on"
    )


@pytest.mark.asyncio
async def test_complete_is_false_while_anything_survives() -> None:
    """⚠ Deliberately not "did the deletes succeed". For a Buildium-backed
    subject this is always False, and that is the honest answer — a caller
    wanting a cheerful one has to change the world, not the predicate."""
    report = await erase_subject(
        _pool(_conn()), COMPANY, Subject(entity_type="tenant", buildium_id=1)
    )
    assert report.erased, "deletes did run"
    assert not report.complete, "mirror rows survive, so the erasure is not complete"


@pytest.mark.asyncio
async def test_a_successful_memory_erasure_still_declares_the_name_only_gap() -> None:
    """⚠ Even the half that worked is partial. Memories referring to the
    subject only by name are not matched — deliberately — so an operator
    signing off on a legal request has to know that before they sign."""
    memory = _Memory(deleted=3)
    report = await erase_subject(
        _pool(_conn()),
        COMPANY,
        Subject(entity_type="tenant", buildium_id=1, email="a@b.c"),
        memory=memory,
    )
    assert report.erased["agent_memory"] == 3
    gap = next(k for k in report.not_erasable_here if k.startswith("agent_memory"))
    assert "name" in report.not_erasable_here[gap]


@pytest.mark.asyncio
async def test_a_missing_memory_backend_is_declared_not_skipped() -> None:
    """Silence here would be the worst version of this product's habit of
    degrading quietly (gotcha 17): the report would look complete while the
    single most sensitive store was never touched."""
    report = await erase_subject(
        _pool(_conn()), COMPANY, Subject(entity_type="tenant", buildium_id=1), memory=None
    )
    assert "agent_memory" in report.not_erasable_here


@pytest.mark.asyncio
async def test_a_memory_failure_is_recorded_rather_than_swallowed() -> None:
    """``delete_company_memory`` returns 0 on failure, which is right for a
    chat turn and wrong here: a silent 0 reads as "there was nothing to
    delete", the one wrong answer that looks like success."""
    report = await erase_subject(
        _pool(_conn()),
        COMPANY,
        Subject(entity_type="tenant", buildium_id=1),
        memory=_Memory(boom=True),
    )
    assert "agent_memory" in report.failed
    assert not report.complete


# ── the memory matcher ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_matches_identifiers_and_never_a_name() -> None:
    """⚠ THE cross-person guard. Erasing on a name would delete a different
    person's records to satisfy this one's request — the Whitfield
    misattribution (gotcha 41) turned into an irreversible privacy harm."""
    from helixis.memory.manager import MemoryManager

    mgr = MemoryManager.__new__(MemoryManager)
    rows = [
        {"id": "1", "memory": "reach Dana at dana@example.com", "metadata": {}},
        {"id": "2", "memory": "tenant 115937 prefers morning visits", "metadata": {}},
        {"id": "3", "memory": "Dana prefers morning visits", "metadata": {}},  # name only
        {"id": "4", "memory": "Priya asked about parking", "metadata": {}},  # someone else
        # ⚠ Row 5 exists because of a mutation that got through without it.
        # Widening the needles to include the ENTITY TYPE ("tenant") passed
        # every assertion above, since the only other row containing that word
        # already matched on its Buildium id — so the fixture could not tell a
        # correct matcher from one that erases any memory mentioning a tenant.
        # A guard can only see the defect if it is handed the input the two
        # implementations disagree on (gotcha 50's 1-tuple, in another costume).
        {"id": "5", "memory": "the tenant in 4B asked about parking", "metadata": {}},
    ]
    mgr.get_all = AsyncMock(return_value=rows)  # type: ignore[method-assign]
    deleted: list[str] = []

    async def _delete(mem_id: str, company_id: str) -> bool:
        deleted.append(mem_id)
        return True

    mgr.delete = _delete  # type: ignore[method-assign]
    # ``available`` is a computed property (``_initialized and _mem0``), so it
    # is satisfied through its backing fields rather than assigned.
    mgr._initialized = True
    mgr._mem0 = MagicMock()

    import helixis.memory.manager as mm

    original = mm.write_audit
    mm.write_audit = AsyncMock()  # type: ignore[assignment]
    try:
        count = await mgr.delete_subject_memory(
            company_id=COMPANY,
            entity_type="tenant",
            buildium_id=115937,
            email="dana@example.com",
        )
    finally:
        mm.write_audit = original  # type: ignore[assignment]

    assert sorted(deleted) == ["1", "2"], (
        "must erase the identifier matches and ONLY those — row 3 names the "
        "subject without an identifier, row 4 is a different person"
    )
    assert count == 2


@pytest.mark.asyncio
async def test_memory_erasure_with_no_identifier_deletes_nothing() -> None:
    """Fail closed. An empty needle list must not degenerate into a substring
    match on "" — which is in every string, and would wipe the pool."""
    from helixis.memory.manager import MemoryManager

    mgr = MemoryManager.__new__(MemoryManager)
    mgr.get_all = AsyncMock(return_value=[{"id": "1", "memory": "x", "metadata": {}}])  # type: ignore[method-assign]
    mgr.delete = AsyncMock(return_value=True)  # type: ignore[method-assign]
    # ``available`` is a computed property (``_initialized and _mem0``), so it
    # is satisfied through its backing fields rather than assigned.
    mgr._initialized = True
    mgr._mem0 = MagicMock()

    assert (await mgr.delete_subject_memory(company_id=COMPANY, entity_type="tenant")) == 0
    mgr.delete.assert_not_awaited()


# ── the caller ───────────────────────────────────────────────────────


def test_the_primitive_has_a_caller() -> None:
    """An erasure primitive nothing can invoke is the pattern this whole audit
    kept finding — built, tested, reachable from nowhere. It is script-invoked
    like its company-level sibling rather than exposed over HTTP, because an
    irreversible per-person delete needs an authorization design of its own."""
    import pathlib

    script = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "erase_subject.py"
    assert script.exists()
    src = script.read_text(encoding="utf-8")
    assert "erase_subject(" in src
    assert "--confirm" in src and "--dry-run" in src, "a destructive script needs both"


def test_the_script_exits_non_zero_while_anything_survives() -> None:
    """The operator's signal. A clean exit on a partial erasure is how somebody
    ends up certifying a request that was not satisfied."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2] / "scripts" / "erase_subject.py").read_text(
        encoding="utf-8"
    )
    assert "return 0 if report.complete else 2" in src


def test_the_subject_types_are_the_ones_the_tables_use() -> None:
    """Registration checked separately from resolution (gotcha 35). A type that
    no table stores would erase nothing and report success."""
    assert set(SUBJECT_TYPES) == {"tenant", "applicant", "owner", "vendor"}
