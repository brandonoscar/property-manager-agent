"""Erase a company's Supabase Storage objects — the leg the database cascade
cannot reach.

`DELETE /api/v1/company` verifies a typed workspace name, purges the Buildium
mirror explicitly, deletes the company row and lets 46 foreign keys cascade.
It is careful, tested, and it has never touched a single uploaded file. The
same is true of `scripts/purge_company_data.py`, which ends by printing a
**proof of erasure** — its own docstring lists Storage under "WHAT THIS SCRIPT
DOES NOT DO, and nothing here will tell you", and `docs/runbooks/
account-deletion.md` says in as many words that "the database rows cascade;
the files do not" and that the proof "certifies the relational + memory legs
only".

So the gap was documented, honest, and load-bearing: a customer exercising a
GDPR/CCPA right got a confirmation, and their leases, browser screenshots and
CSV exports stayed in three buckets indefinitely. Every object is reachable by
an unauthenticated signed URL while one is live (documents 300s, screenshots
24h, artifacts 1h), and the objects outlive every row that recorded them.

**Three-valued, and that is the whole design.** Per bucket the answer is
*purged n* / *nothing found* / *could not enumerate*, and the third is NEVER a
pass. An erasure path that cannot tell "there were no files" from "I could not
look" produces a certificate that is wrong in the direction nobody checks —
the same decline-to-assert posture as the consent ledger and the role floor
(gotchas 55/57/70/89), pointed at a legal obligation.

⚠ **Failure must not block the delete, and that is deliberate.** Refusing to
delete a workspace because Supabase Storage hiccuped turns a transient error
into a customer who cannot exercise a right — unrecoverable from their side.
Proceeding leaves objects behind under a prefix that is *still the company id*,
so an operator can finish it by hand with the one fact this report gives them.
One direction is recoverable and the other is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from helixis.settings import Settings
from helixis.storage import StorageError, delete_object, list_objects
from helixis.storage_buckets import COMPANY_SCOPED_BUCKETS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BucketPurge:
    """What happened to one bucket. ``enumerated`` is the three-valued bit."""

    bucket: str

    #: Could we see what was in there at all? ``False`` means UNVERIFIED —
    #: never "empty".
    enumerated: bool

    #: Objects found under the company's prefix (0 when enumerated and clean).
    found: int = 0

    #: Objects confirmed deleted. Less than ``found`` means a partial sweep.
    deleted: int = 0

    #: Why enumeration or a delete failed, in the provider's own words where
    #: we have them — an operator finishing this by hand needs the reason, not
    #: a boolean (the `voyage_embedder` precedent, gotcha 84).
    error: str = ""


@dataclass(frozen=True)
class StoragePurgeReport:
    """The whole sweep. ``complete`` is what an erasure proof may rely on."""

    company_id: str
    buckets: list[BucketPurge] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True only when every bucket was enumerated AND everything found was
        deleted. A bucket we could not read makes this False, because the
        alternative is certifying an erasure nobody performed."""
        return all(b.enumerated and b.deleted == b.found for b in self.buckets)

    @property
    def deleted_total(self) -> int:
        return sum(b.deleted for b in self.buckets)

    @property
    def unfinished(self) -> list[BucketPurge]:
        """Buckets an operator still has to clear by hand."""
        return [b for b in self.buckets if not (b.enumerated and b.deleted == b.found)]

    def summary(self) -> str:
        """One line for a log or an API response — states the unfinished legs
        by name, because a report that only carries a count tells the reader
        nothing they can act on."""
        if self.complete:
            return (
                f"storage purged: {self.deleted_total} object(s) "
                f"across {len(self.buckets)} bucket(s)"
            )
        parts = []
        for b in self.unfinished:
            why = b.error or f"{b.deleted}/{b.found} deleted"
            parts.append(f"{b.bucket} ({why})")
        names = ", ".join(parts)
        return (
            f"storage PARTIALLY purged: {self.deleted_total} object(s) removed; "
            f"still holding data under prefix {self.company_id}/ in: {names}"
        )


async def purge_company_storage(
    *,
    company_id: str,
    settings: Settings,
    buckets: tuple[str, ...] = COMPANY_SCOPED_BUCKETS,
) -> StoragePurgeReport:
    """Delete every object under ``{company_id}/`` in each company-scoped bucket.

    Never raises. Storage is one leg of a multi-leg deletion and an exception
    here would abort the legs after it — the caller decides what an incomplete
    sweep means, which differs between the live route (report and proceed) and
    the erasure script (refuse to certify).

    ⚠ Deletes ONE OBJECT AT A TIME through the endpoint this repo has actually
    exercised in production. Supabase offers a bulk form and using it would be
    one fewer round trip and one more unprobed wire shape inside an erasure
    path (gotcha 79); a prefix delete that silently matches nothing is exactly
    the failure this module exists to stop.
    """
    prefix = str(company_id).strip().strip("/")
    results: list[BucketPurge] = []

    for bucket in buckets:
        try:
            paths = await list_objects(bucket=bucket, prefix=prefix, settings=settings)
        except StorageError as exc:
            logger.error(
                "storage purge: could NOT enumerate %s/%s/ — objects may remain: %s",
                bucket,
                prefix,
                exc,
            )
            results.append(BucketPurge(bucket=bucket, enumerated=False, error=str(exc)))
            continue

        deleted = 0
        first_error = ""
        for path in paths:
            try:
                await delete_object(bucket=bucket, path=path, settings=settings)
            except StorageError as exc:
                # Keep going: 9 of 10 files removed beats stopping at the first
                # transient failure and leaving the other 9 as well.
                if not first_error:
                    first_error = str(exc)
                logger.error("storage purge: failed to delete %s/%s: %s", bucket, path, exc)
                continue
            deleted += 1

        results.append(
            BucketPurge(
                bucket=bucket,
                enumerated=True,
                found=len(paths),
                deleted=deleted,
                error=first_error,
            )
        )

    report = StoragePurgeReport(company_id=prefix, buckets=results)
    log = logger.warning if report.complete else logger.error
    log("storage purge for company=%s — %s", prefix, report.summary())
    return report
