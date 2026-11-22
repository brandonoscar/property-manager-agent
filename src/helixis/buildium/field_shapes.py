"""Does the field the code reads actually carry a value in production?

Four times in one month a Buildium field was **present as a key and null as a
value**, and every time the code read it as if it were populated:

* ``TotalAmount`` — null on all 67 production bills. Unpaid exposure reported
  **$0** against a real $26,000 (gotcha 25).
* ``Status`` on tasks — null on all 179 rows while ``TaskStatus`` carried the
  value, so completed tasks were classed as open (gotcha 26).
* bill ownership — not a top-level key at all; it lives in
  ``Lines[].AccountingEntity.Id`` (gotcha 24).
* ``IsPaid`` — null on every bill. ``getattr(b, "is_paid", True) is False``
  discarded **every** row and the tool reported $0 again (2026-08-23).

None of these are crashes. Each is a plausible value that is simply wrong, and
each survived unit tests because the tests supplied the field the code wanted.
Only a live row can settle it — which is the whole point of this module.

The registry is CLASSIFICATION-COMPLETE over the models it covers: every
declared field must carry an expectation, so adding one to a schema forces the
question "does production actually populate this?" rather than leaving it to be
discovered by a $0 report.

⚠ The pure core here takes ROWS, not a client. It is fully unit-tested without
credentials; only :func:`probe_live` needs a Buildium key. A checker that can
only run where the secrets are is a checker nobody runs.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Expect(StrEnum):
    """What production is expected to do with a field."""

    #: Non-null on every row. A single null is a finding.
    ALWAYS = "always"
    #: Non-null on MOST rows — see :data:`USUALLY_FLOOR`, which is what the
    #: judge measures that against. All-null is the loudest case (a field that
    #: is never populated is effectively absent, whatever the schema says) but
    #: it is not the only one.
    USUALLY = "usually"
    #: Legitimately sparse (``PaidDate`` on an unpaid bill). Never a finding.
    SOMETIMES = "sometimes"
    #: KNOWN absent in production. Code must not depend on it, and the
    #: registry records WHY so nobody re-adds the dependency.
    NEVER_TRUST = "never_trust"


@dataclass(frozen=True)
class FieldStat:
    alias: str
    total: int
    present: int
    non_null: int

    @property
    def non_null_rate(self) -> float:
        return (self.non_null / self.total) if self.total else 0.0


#: What "most rows" means for :attr:`Expect.USUALLY`, as a fraction.
#:
#: ⚠ **DERIVED FROM THE ENUM'S OWN WORD, NOT PICKED.** ``USUALLY`` is
#: documented as "non-null on most rows" and a majority is what "most" means.
#: The judge tested ``non_null == 0`` instead, so a field could fall from
#: 171/179 to 1/179 and this sweep would report green — and ``non_null_rate``,
#: which computes exactly the number that decides it, sat here unread.
#:
#: **A partial collapse is WORSE than a total one for the caller this matters
#: most to, which is why the gap is not cosmetic.** ``_only_for_property``
#: fails OPEN when NO row carries the scoping field (gotcha 55), so a field at
#: 0% degrades to "no filtering" and over-reports visibly. At 1% it filters,
#: matches the one populated row, and reports the other 178 as ABSENT — the
#: confident false absence gotcha 26 calls this product's worst failure mode.
#: So the one shape the old test could not see is the one that fails silently.
#:
#: Every rate the registry records clears this by a wide margin (``Priority``
#: 100%, ``Property`` 171/179 = 95.5%), so it cannot fire on measured data;
#: and ``UnitId``'s DELIBERATE 0/90 persistent finding is untouched, because
#: zero is below any floor.
#:
#: ⚠ A rate exactly ON the floor is NOT a finding. Half the rows is a tie, and
#: a check that cannot tell which way a tie falls should decline to assert
#: rather than manufacture a finding somebody has to close (gotchas 55/57, and
#: gotcha 52's rule that a guard which cries wolf is one people switch off).
USUALLY_FLOOR = 0.5


@dataclass(frozen=True)
class Finding:
    alias: str
    expected: Expect
    detail: str

    def __str__(self) -> str:
        return f"{self.alias}: expected {self.expected}, {self.detail}"


#: entity -> {alias: (expectation, why)}. Only aliases the CODE reads need an
#: entry; ``expected_aliases`` enforces completeness against the schema.
EXPECTATIONS: dict[str, dict[str, tuple[Expect, str]]] = {
    "bill": {
        "Id": (Expect.ALWAYS, "the primary key"),
        "VendorId": (Expect.USUALLY, "who we owe; drives vendor rollups"),
        "Date": (Expect.USUALLY, "aging is computed from it"),
        "DueDate": (Expect.SOMETIMES, "not every bill carries terms"),
        "TotalAmount": (
            Expect.NEVER_TRUST,
            "null on all 67 production bills (gotcha 25) — the money is in "
            "Lines[].Amount; buildium.money.bill_amount is the only correct reader",
        ),
        "IsPaid": (
            Expect.NEVER_TRUST,
            "null on every production bill (2026-08-23) — `is False` discarded "
            "all 67 rows and reported $0; filter with `is not True`",
        ),
        "PaidDate": (Expect.SOMETIMES, "absent by definition on an unpaid bill"),
        "ReferenceNumber": (Expect.SOMETIMES, "vendor-supplied, often blank"),
        "Lines": (
            Expect.ALWAYS,
            "the real payload — amount AND ownership both live here (gotcha 24)",
        ),
        "WorkOrderId": (Expect.SOMETIMES, "only bills raised from a work order"),
    },
    "task": {
        "Id": (Expect.ALWAYS, "the primary key"),
        "TaskStatus": (
            Expect.ALWAYS,
            "the REAL status field; `Status` is null on all 179 rows (gotcha 26)",
        ),
        "Status": (
            Expect.NEVER_TRUST,
            "null on every production task row — reading it classed completed "
            "tasks as open (gotcha 26); read TaskStatus",
        ),
        "Priority": (Expect.USUALLY, "present on every row probed; drives triage"),
        "Property": (Expect.USUALLY, "171/179 rows; scoping key, since ?propertyids is ignored"),
        "Title": (Expect.ALWAYS, "90/90 on the live mirror; also a required field on task PUT"),
        "Description": (Expect.ALWAYS, "90/90 on the live mirror; the enricher reads it"),
        "CreatedDateTime": (Expect.ALWAYS, "90/90; server-set"),
        "LastUpdatedDateTime": (Expect.ALWAYS, "90/90; server-set"),
        "DueDate": (Expect.SOMETIMES, "0/90 on the live mirror — genuinely optional on a task"),
        "UnitId": (
            Expect.USUALLY,
            "⚠ OPEN QUESTION. Gotcha 24 records flat UnitId as a task scoping key, but it "
            "is null on all 90 rows of the live mirror (measured 2026-08-24). Classified "
            "USUALLY deliberately so the live probe KEEPS REPORTING it: either the code "
            "scopes on a field production never populates, or this seed is unrepresentative "
            "of a real account. A persistent finding is the right outcome until one of "
            "those is settled against a customer's data — silence would bury the question.",
        ),
    },
}


def expected_aliases(model: type) -> set[str]:
    """Every alias a pydantic model declares — the set the code can read."""
    out: set[str] = set()
    for name, field in getattr(model, "model_fields", {}).items():
        out.add(getattr(field, "alias", None) or name)
    return out


def unclassified(entity: str, model: type) -> set[str]:
    """Declared aliases with no expectation. Non-empty = someone added a field
    without asking whether production populates it."""
    return expected_aliases(model) - set(EXPECTATIONS.get(entity, {}))


def analyse(rows: Sequence[Any], aliases: Iterable[str]) -> dict[str, FieldStat]:
    """Present/non-null counts per alias across live rows.

    Rows may be dicts (raw JSON) or pydantic models carrying ``model_extra``.
    Present-but-null is counted separately from absent, because that
    distinction IS the bug class — a defaulted ``getattr`` protects against
    absence and not against a null.
    """
    stats: dict[str, FieldStat] = {}
    for alias in aliases:
        present = non_null = 0
        for row in rows:
            raw = row if isinstance(row, dict) else _raw_of(row)
            if alias in raw:
                present += 1
                if raw[alias] is not None:
                    non_null += 1
        stats[alias] = FieldStat(alias, len(rows), present, non_null)
    return stats


def _raw_of(row: Any) -> dict[str, Any]:
    """Best-effort original-cased payload for a pydantic row."""
    if hasattr(row, "model_dump"):
        try:
            # ``row`` is Any, so model_dump() is Any — dict() both narrows it
            # for mypy and guarantees the declared return type at runtime,
            # since a duck-typed row could return anything at all.
            return dict(row.model_dump(by_alias=True))
        except Exception:  # noqa: BLE001 - an odd model must not kill the probe
            pass
    return dict(getattr(row, "model_extra", None) or {})


def verdict(entity: str, stats: dict[str, FieldStat]) -> list[Finding]:
    """Findings for one entity. Empty list = production matches expectations."""
    rules = EXPECTATIONS.get(entity, {})
    findings: list[Finding] = []
    for alias, stat in sorted(stats.items()):
        expect, why = rules.get(alias, (Expect.SOMETIMES, "unclassified"))
        if stat.total == 0:
            continue
        if expect is Expect.ALWAYS and stat.non_null < stat.total:
            findings.append(
                Finding(alias, expect, f"null on {stat.total - stat.non_null}/{stat.total} rows")
            )
        elif expect is Expect.USUALLY and stat.non_null_rate < USUALLY_FLOOR:
            # Two sentences, because the remedies differ. All-null means the
            # field is absent and the code should stop reading it; a minority
            # means it is still there and has stopped being dependable, which
            # is the case that used to pass silently.
            detail = (
                f"null on ALL {stat.total} rows — treat as absent"
                if stat.non_null == 0
                else (
                    f"populated on only {stat.non_null}/{stat.total} rows "
                    f"({stat.non_null_rate:.0%}) — USUALLY means most rows, so code "
                    f"reading this as dependable is wrong more often than not"
                )
            )
            findings.append(Finding(alias, expect, detail))
        elif expect is Expect.NEVER_TRUST and stat.non_null == stat.total and stat.total > 0:
            # The inverse finding, and the easy one to forget: a field we
            # learned to distrust has started arriving populated. That is good
            # news, but the workarounds built around its absence are now
            # unnecessary and possibly wrong — say so rather than stay silent.
            findings.append(
                Finding(
                    alias,
                    expect,
                    f"now populated on all {stat.total} rows ({why}) — "
                    "the workaround may be reviewable",
                )
            )
    return findings


async def probe_live(api: Any, *, limit: int = 200) -> dict[str, list[Finding]]:
    """Fetch real rows and report. The only part needing a Buildium key.

    Read-only. Failures per entity are reported, never raised — a probe that
    dies on the first endpoint tells you less than one that finishes.
    """
    out: dict[str, list[Finding]] = {}
    from helixis.buildium.schemas import Bill, Task

    for entity, fetch, model in (
        ("bill", lambda: api.bills.list(paidstatus="Unpaid"), Bill),
        ("task", lambda: api.tasks.list(), Task),
    ):
        try:
            rows = list(await fetch())[:limit]
        except Exception as exc:  # noqa: BLE001 - report, don't abort the sweep
            out[entity] = [Finding("<fetch>", Expect.ALWAYS, f"{type(exc).__name__}: {exc}")]
            continue
        out[entity] = verdict(entity, analyse(rows, expected_aliases(model)))
    return out
