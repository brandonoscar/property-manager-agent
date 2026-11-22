"""The one declaration of every Supabase Storage bucket this backend writes to.

Two questions get asked about buckets and they were being answered by two
hand-typed lists that disagreed:

* *"which buckets must EXIST?"* — `smoke/checks.py::_REQUIRED_BUCKETS`, whose
  own comment says "every private bucket the backend writes to" and named
  **two** of the three. `browser_screenshots` is written live by
  `agents/browser_tools._upload_screenshot_to_storage`, and that path returns
  ``None`` on any failure so the agent reports ``screenshot_failed`` — the
  silent degradation the smoke check exists to catch, on the one bucket it
  was not watching.
* *"which buckets hold a company's objects?"* — nothing asked this at all
  until the deletion path needed it, which is the gap this module exists for:
  `DELETE /api/v1/company` purged the database and left every uploaded PDF,
  screenshot and export sitting in Storage forever.

Both answers are COMPUTED from the single table below, so adding a bucket
forces you to answer both rather than remembering a second list exists — the
`buildium/maintenance_status.OPEN_STATUSES` precedent (a derived set cannot
drift from the thing it is derived from).

⚠ **`company_prefixed` is a claim about the WRITE path, not a wish.** Every
entry here is keyed ``{company_id}/…`` by the code that uploads it, and the
purge deletes by that prefix. A bucket whose objects are NOT keyed that way
must set it false: the purge would otherwise enumerate nothing and report a
clean sweep it never performed, which is worse than not purging at all.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StorageBucket:
    """One private bucket, and the two facts anything needs to know about it."""

    name: str

    #: What writes to it, so a reader can check the path shape at the source
    #: rather than trusting this file (gotcha 48 — a comment cannot fail).
    written_by: str

    #: Is EVERY object keyed ``{company_id}/…``? Decides whether a company's
    #: objects can be found — and therefore erased — by prefix.
    company_prefixed: bool


BUCKETS: tuple[StorageBucket, ...] = (
    StorageBucket(
        name="company_documents",
        written_by=(
            "api/routes/documents.py::upload_document "
            "({company_id}/{doc_id}.pdf) and agents/buildium_tools.py"
        ),
        company_prefixed=True,
    ),
    StorageBucket(
        name="browser_screenshots",
        written_by=(
            "agents/browser_tools.py::_upload_screenshot_to_storage "
            "({company_id}/{session_id}/{step}.png)"
        ),
        company_prefixed=True,
    ),
    StorageBucket(
        # ⚠ NOT dark. CLAUDE.md gotcha 33 cites this bucket holding "0 objects,
        # ever" as corroboration that HELIXIS_CODEEXEC__ENABLED is false — true
        # of the codeexec path, and `agents/export_tools.export_table` is
        # registered on the orchestrator (orchestrator.py imports it) and writes
        # here on every CSV export. So it accumulates real customer data today.
        name="codeexec_artifacts",
        written_by=(
            "agents/export_tools.py::export_table and "
            "codeexec/storage_io.py::persist_artifacts ({company_id}/{run}/{name})"
        ),
        company_prefixed=True,
    ),
)

#: Buckets that must exist in Supabase — a bucket referenced in code and absent
#: there fails only at the first real upload (unit tests mock the client), so
#: the daily smoke is the tripwire.
ALL_BUCKET_NAMES: tuple[str, ...] = tuple(b.name for b in BUCKETS)

#: Buckets a company's objects can be erased from by prefix. Read by
#: ``storage_purge.purge_company_storage``.
COMPANY_SCOPED_BUCKETS: tuple[str, ...] = tuple(b.name for b in BUCKETS if b.company_prefixed)
