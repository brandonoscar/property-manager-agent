"""Phase 8's reconciliation: does the mirror hold what the API holds?

Phase 8 passes when "the mirror's row count must equal the same count read
back from the API, and a re-run must be idempotent". This is the judgement
half of that — the pure comparison, with no network and no database, so it can
be tested exhaustively and cannot be shaped to agree with a particular sync.

⚠ WHY THIS IS WRITTEN BEFORE THE SYNC EXISTS, DELIBERATELY
    A reconciliation written by whoever wrote the sync tends to measure what
    the sync DOES rather than what is true: the same assumptions go into both
    sides, and the check passes because it shares the bug. Written first, it
    has nothing to agree with.

⚠ THE FAILURE THIS FILE IS MOSTLY ABOUT: 0 == 0
    The obvious implementation compares two integers and calls equality a
    pass. But a failed API read and an empty account both produce zero, and
    "0 == 0" is then reported as RECONCILED — a green light derived from
    having learned nothing. That is this product's most-recorded failure shape
    (gotchas 26/55/57: a fabricated absence reads exactly like a real one).

    So a count is three-valued here. ``None`` means NOT READ and can never
    reconcile; ``0`` means read, and genuinely zero. The distinction is the
    whole design, and every branch below turns on it.

⚠ AND THE MIRROR SIDE MUST BE SCOPED
    A count that forgets ``pms = 'rentvine'`` includes the Buildium rows and
    can never match; a count that forgets ``company_id`` reaches another
    tenant's rows (gotcha 47). The SQL lives with the sync, but
    ``mirror_count_sql`` states the shape the harness expects, and a
    reconciliation run against an unscoped count is refused rather than
    reported.

USE
    Phase 8 wires the two readers and calls ``reconcile`` / ``idempotency``.
    The counting itself is not here: reading a paginated Rentvine collection
    has no documented stop condition, so it belongs with the client that
    already carries that rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "Verdict",
    "FamilyResult",
    "Reconciliation",
    "reconcile",
    "idempotency",
    "mirror_count_sql",
]


class Verdict(Enum):
    RECONCILED = "reconciled"
    #: The mirror holds fewer rows than the API.
    #:
    #: ⚠ **THIS MODULE CANNOT SEE WHY AND MUST NOT PICK.** It used to name
    #: paging as "the likeliest cause". The first live Phase 8 run
    #: (2026-09-23) produced two MISSING families and paging was not involved
    #: in either: the rows were fetched and then SKIPPED by the writer,
    #: because the row envelope said a wrapper was a record. An operator
    #: reading the old sentence would have gone after the paging loop. The
    #: sync's own log separates the two; this verdict names both.
    MISSING = "missing"
    #: The mirror holds MORE than the API. Not the benign direction: it means
    #: duplicates, or rows from another scope leaking into the count.
    EXTRA = "extra"
    #: One side could not be read. NEVER a pass.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FamilyResult:
    family: str
    api: int | None
    mirror: int | None
    verdict: Verdict
    detail: str

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.RECONCILED


@dataclass(frozen=True)
class Reconciliation:
    families: tuple[FamilyResult, ...]

    @property
    def ok(self) -> bool:
        """Every family reconciled. An empty run is NOT a pass.

        A reconciliation over zero families has checked nothing, and ``all([])``
        is True — the vacuous green this repo has been bitten by often enough
        to name (a scan that finds zero is a claim to check, not a result).
        """
        return bool(self.families) and all(f.ok for f in self.families)

    @property
    def unresolved(self) -> tuple[FamilyResult, ...]:
        return tuple(f for f in self.families if not f.ok)


def _one(family: str, api: int | None, mirror: int | None) -> FamilyResult:
    if api is None or mirror is None:
        which = []
        if api is None:
            which.append("the API")
        if mirror is None:
            which.append("the mirror")
        return FamilyResult(
            family,
            api,
            mirror,
            Verdict.UNKNOWN,
            f"could not read {' and '.join(which)} — this is NOT a match. A failed "
            f"read and an empty account both look like zero, and reporting the "
            f"difference as reconciled is a green light derived from nothing.",
        )
    if api == mirror:
        zero_note = (
            " Both are zero, and that is a real answer here BECAUSE the API was "
            "read successfully — an unread API would have been UNKNOWN above."
            if api == 0
            else ""
        )
        return FamilyResult(
            family, api, mirror, Verdict.RECONCILED, f"{api} rows both sides.{zero_note}"
        )
    if mirror < api:
        return FamilyResult(
            family,
            api,
            mirror,
            Verdict.MISSING,
            f"mirror is short by {api - mirror} of {api}. Three causes, and "
            f"this comparison cannot tell them apart — read the sync's result "
            f"and log, which can: (a) the family is in `failed`, so part of it "
            f"was never fetched (units are per-property, and one property's "
            f"403 leaves the rest of that table stale — gotcha 160); (b) rows "
            f"arrived and were SKIPPED by the writer (check skipped_no_id and "
            f"the skip warning, which prints the first skipped row's keys — a "
            f"wrapper there is a row-envelope bug); or (c) the paging loop "
            f"stopped early, since Rentvine documents no stop condition on "
            f"some collections. Check (a) FIRST — it is decisive and free. "
            f"(a) is the customer's key, (b) is ours, (c) is theirs.",
        )
    return FamilyResult(
        family,
        api,
        mirror,
        Verdict.EXTRA,
        f"mirror holds {mirror - api} MORE than the API returned. Not the benign "
        f"direction: duplicate upserts, or rows from another company or PMS "
        f"reaching a count that forgot to scope.",
    )


def reconcile(
    api_counts: dict[str, int | None], mirror_counts: dict[str, int | None]
) -> Reconciliation:
    """Compare per-entity-family counts.

    A family present on one side and absent on the other is read as UNKNOWN,
    not as zero: a family the sync never attempted has not been shown to be
    empty.
    """
    families = sorted(set(api_counts) | set(mirror_counts))
    return Reconciliation(
        tuple(
            _one(family, api_counts.get(family), mirror_counts.get(family)) for family in families
        )
    )


@dataclass(frozen=True)
class Idempotency:
    inserted: int | None
    ok: bool
    detail: str


def idempotency(before: int | None, after: int | None) -> Idempotency:
    """Did a second sync insert anything? It must not.

    ⚠ Row count is the right instrument and its limit is worth stating: it
    measures INSERTS, not writes. An upsert that re-UPDATES every row leaves
    the count flat and passes here, which is correct for this gate — Phase 8
    asks whether a re-run inserts, not whether it touches. A churn check is a
    different question and needs a different measurement.
    """
    if before is None or after is None:
        return Idempotency(
            None,
            False,
            "a count could not be read, so the re-run proved nothing — not a pass",
        )
    delta = after - before
    if delta == 0:
        return Idempotency(
            0, True, f"second sync inserted nothing ({before} rows before and after)"
        )
    if delta > 0:
        return Idempotency(
            delta,
            False,
            f"second sync inserted {delta} row(s) — the upsert key does not match "
            f"the record's identity, so every sync will grow the mirror",
        )
    return Idempotency(
        delta,
        False,
        f"second sync REMOVED {-delta} row(s) — a re-run is deleting data it "
        f"previously stored, which is worse than duplicating it",
    )


def mirror_count_sql(table: str) -> str:
    """The shape a mirror count must have, stated so it cannot be forgotten.

    ⚠ Both predicates are load-bearing and they fail in opposite directions.
    Without ``pms`` the count includes the Buildium rows and can never match
    the Rentvine API. Without ``company_id`` it reaches another tenant's rows,
    which is gotcha 47's cross-tenant read — and it would make the numbers
    LOOK closer, not further apart, so it is the one that hides.
    """
    return f"SELECT count(*) FROM {table} WHERE company_id = $1 AND pms = $2"


def render(result: Reconciliation, idem: Idempotency | None = None) -> str:
    lines = []
    for f in result.families:
        mark = "OK  " if f.ok else "FAIL"
        api = "unread" if f.api is None else f.api
        mirror = "unread" if f.mirror is None else f.mirror
        lines.append(
            f"  {mark} {f.family:<16} api={api!s:<8} mirror={mirror!s:<8} {f.verdict.value}"
        )
        if not f.ok:
            lines.append(f"       {f.detail}")
    if not result.families:
        lines.append("  no families compared — nothing was checked, which is not a pass")
    if idem is not None:
        lines.append(f"  {'OK  ' if idem.ok else 'FAIL'} idempotency      {idem.detail}")
    verdict = "RECONCILED" if result.ok and (idem is None or idem.ok) else "NOT RECONCILED"
    lines.append(f"\nVERDICT: {verdict}")
    return "\n".join(lines)
