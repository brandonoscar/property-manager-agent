"""Erase one PERSON's data — the right that belongs to an individual.

Everything that existed before this was company-scoped: ``purge_buildium_mirror``
drops a company's mirror on disconnect (ToS §18), ``delete_company_memory``
wipes a company's whole memory pool. Both are correct for what they do, and
neither can serve a tenant who asks for THEIR data to be deleted — the rights
GDPR and CCPA grant are held by a person, not by an account. So the only two
available answers were "wipe the entire company, destroying data about
everyone else" or "do nothing".

⚠ **THE MIRROR IS A CACHE, AND DELETING FROM A CACHE IS NOT ERASURE.** Rows in
``helixis_tenants`` and its siblings are copies of Buildium's records; delete
one and the next sync puts it straight back. That makes the obvious
implementation — DELETE across every table naming the subject — worse than
useless: it reports success, the data returns within the hour, and we have told
someone their legal request was honoured when it was not. Buildium is the
controller for that data. The request has to go there.

So this returns a REPORT with two halves, and the second half is the point:

* ``erased`` — what Helixis itself authored about the subject and has now
  removed for good: agent memories, learned aliases, promise-to-pay rows,
  findings and reminders naming them.
* ``not_erasable_here`` — where the subject still appears, why we cannot make
  that stick, and what the operator must do instead. Silence here would be the
  confident false claim this codebase keeps relearning (gotchas 41/51/57),
  pointed at a legal obligation rather than at a rent figure.

Deliberately script-invoked (``scripts/erase_subject.py``), like its
company-level sibling, rather than exposed as an HTTP route: an irreversible
per-person delete needs an authorization design of its own, and inventing one
here without review would be the weaker half of the pattern.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Subject:
    """Who is being erased.

    At least one of ``buildium_id`` / ``email`` is required — they address
    different tables (``entity_id`` joins vs the email-keyed rows), and a
    subject given only one is erased from only the half that key reaches. The
    report says which, rather than implying completeness.
    """

    entity_type: str  # 'tenant' | 'applicant' | 'owner' | 'vendor'
    buildium_id: int | None = None
    email: str | None = None

    def __post_init__(self) -> None:
        if self.buildium_id is None and not (self.email or "").strip():
            raise ValueError("a subject needs a buildium_id, an email, or both")
        if self.entity_type not in SUBJECT_TYPES:
            raise ValueError(f"unknown entity_type {self.entity_type!r}")


SUBJECT_TYPES = ("tenant", "applicant", "owner", "vendor")


@dataclass
class ErasureReport:
    """What was removed, and — the load-bearing half — what was not."""

    subject: Subject
    company_id: str
    erased: dict[str, int] = field(default_factory=dict)
    #: table → why this appearance of the subject cannot be erased here.
    not_erasable_here: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """⚠ Deliberately NOT "did the deletes succeed".

        An erasure is only complete when nothing about the subject survives,
        and mirror rows always survive — so this is False whenever anything
        sits in ``not_erasable_here``, which for a Buildium-backed subject is
        always. That is the honest answer, and a caller that wants a cheerful
        one has to change the world rather than the predicate.
        """
        return not self.not_erasable_here and not self.failed


#: Helixis-authored, subject-keyed, and erased for good. Ordered children
#: before parents so the deletes hold under any FK configuration — the same
#: discipline as the §18 purge list.
#:
#: Each entry is (table, column carrying the subject's buildium id).
_ERASE_BY_ENTITY_ID: tuple[tuple[str, str], ...] = (
    ("helixis_entity_aliases", "entity_id"),
    ("helixis_entity_signals", "entity_id"),
    ("helixis_risk_flags", "entity_id"),
)

#: Same, for the rows Helixis keys by email rather than by Buildium id.
_ERASE_BY_EMAIL: tuple[tuple[str, str], ...] = (
    ("helixis_payment_promises", "email"),
    ("helixis_renewal_offers", "recipient_email"),
)

#: Where the subject still appears afterwards, and why this function cannot
#: make a delete stick there. Stated per table because "some data remains" is
#: not something an operator can act on.
_MIRROR_RESYNCS = (
    "helixis_tenants",
    "helixis_applicants",
    "helixis_owners",
    "helixis_vendors",
    "helixis_lease_tenants",
)

_RESYNC_REASON = (
    "mirror of a Buildium record — a delete here is undone by the next sync. "
    "Buildium is the controller for this data; the erasure request must be "
    "made there, and the mirror follows."
)


def _table_reason(table: str) -> str:
    if table in _MIRROR_RESYNCS:
        return _RESYNC_REASON
    return "not erasable from this path"


async def erase_subject(
    pool: Any,
    company_id: str,
    subject: Subject,
    *,
    memory: Any = None,
) -> ErasureReport:
    """Remove what Helixis authored about ``subject``; report what remains.

    Never raises for a per-table failure: a partial erasure that says which
    part failed is useful, and one that aborts halfway leaving no record is
    not. Failures land in ``report.failed``.
    """
    report = ErasureReport(subject=subject, company_id=company_id)

    async with pool.acquire() as conn:
        if subject.buildium_id is not None:
            for table, column in _ERASE_BY_ENTITY_ID:
                await _delete(
                    conn,
                    report,
                    table,
                    f"DELETE FROM {table} WHERE company_id = $1::uuid "
                    f"AND entity_type = $2 AND {column} = $3",
                    company_id,
                    subject.entity_type,
                    subject.buildium_id,
                )
        if subject.email:
            for table, column in _ERASE_BY_EMAIL:
                await _delete(
                    conn,
                    report,
                    table,
                    f"DELETE FROM {table} WHERE company_id = $1::uuid "
                    f"AND lower({column}) = lower($2)",
                    company_id,
                    subject.email,
                )

    # Memory is a separate store with its own failure semantics, and it is an
    # enhancement everywhere else in this product — but NOT here. An erasure
    # that silently skipped memory would be the worst possible place for the
    # degrade-quietly rule (gotcha 17), so a failure is recorded, not swallowed.
    if memory is not None:
        try:
            report.erased["agent_memory"] = await memory.delete_subject_memory(
                company_id=company_id,
                entity_type=subject.entity_type,
                buildium_id=subject.buildium_id,
                email=subject.email,
            )
            # ⚠ Even a SUCCESSFUL memory erasure is partial, and the report has
            # to say so. Matching is on identifiers — email and Buildium id —
            # never on a name, because two people share a surname more often
            # than not and deleting on one would erase a DIFFERENT person's
            # records to satisfy this request. So a memory that refers to the
            # subject only as "Marcus" survives, and an operator signing off on
            # a legal request needs to know that before they sign.
            report.not_erasable_here["agent_memory (name-only references)"] = (
                "memories are matched on email and Buildium id, never on a "
                "name — a name is not an identifier and erasing on one would "
                "delete other people's records. Any memory that names the "
                "subject without an identifier survives and needs a human read."
            )
        except (Exception, SystemExit) as exc:  # noqa: BLE001 — report, never abort
            report.failed["agent_memory"] = str(exc)[:300]
            logger.exception("subject erasure: memory delete failed")
    else:
        report.not_erasable_here["agent_memory"] = (
            "no memory backend was supplied to this call, so nothing was "
            "removed from it — re-run with one before reporting the request "
            "as satisfied."
        )

    for table in _MIRROR_RESYNCS:
        report.not_erasable_here[table] = _table_reason(table)

    logger.info(
        "subject erasure company=%s type=%s erased=%s remaining=%s failed=%s",
        company_id,
        subject.entity_type,
        report.erased,
        sorted(report.not_erasable_here),
        sorted(report.failed),
    )
    return report


async def _delete(conn: Any, report: ErasureReport, table: str, sql: str, *args: Any) -> None:
    try:
        result = await conn.execute(sql, *args)
        report.erased[table] = _rowcount(result)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — one table must not abort the rest
        report.failed[table] = str(exc)[:300]
        logger.exception("subject erasure: %s failed", table)


def _rowcount(result: Any) -> int:
    """asyncpg returns ``'DELETE 3'``; anything unparseable counts as 0.

    ⚠ Returns 0 rather than guessing on a shape it does not recognise — an
    invented count in an erasure report is a claim that specific data was
    removed, which is the one thing this file must never do loosely.
    """
    try:
        return int(str(result).split()[-1])
    except (ValueError, IndexError):
        return 0


__all__ = ["ErasureReport", "Subject", "SUBJECT_TYPES", "erase_subject"]
