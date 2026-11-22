"""Reverse lookup: a phone number → the Buildium contact it belongs to.

The linchpin of the click-to-call/text feature. Every inbound call/SMS and
every "who am I about to dial" carries a phone number; this turns that number
into a *known* tenant / owner / vendor so the AI has context (and so an
auto-created task can attribute the request to the right resident). Without it,
every number is a stranger.

**Matching strategy.** The mirror stores phone in human format (see
``db/repositories/entities.py`` — ``helixis_tenants/owners/vendors.phone`` are
Cell-preferred but un-normalized). Rather than add a normalized column +
backfill (a migration with the no-runner caveat, gotcha 8, and a re-backfill on
every mirror change), we normalize on read: the inbound number is reduced to
its 10 national-significant digits (``phone.phone_match_key``) and compared
against the same reduction of each stored number, computed SQL-side with
``regexp_replace(phone,'\\D','','g')``. Always reflects current mirror data; no
migration to keep in sync. Mirrors are one company's contacts (hundreds of
rows), so the per-lookup scan is cheap.

**Precedence.** A single number can, in principle, belong to more than one
contact (an owner who also rents a unit). We return the best single match by a
fixed precedence — tenant → owner → vendor — because resident comms is the
primary use case, and expose ``ambiguous`` + the full match list so a caller
(UI or agent) can disambiguate when it matters. Everything here is
company-scoped: a number is only ever matched inside the caller's own company.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from helixis.comms.phone import normalize_e164, phone_match_key

logger = logging.getLogger(__name__)

# tenant beats owner beats vendor when one number matches more than one.
_KIND_RANK = {"tenant": 1, "owner": 2, "vendor": 3}

# One round-trip: reduce every stored number to its 10 national digits SQL-side
# and compare against the inbound match key ($2). ``right(...,10)`` on a stored
# number with <10 digits yields a short string that can't equal a 10-digit key,
# so a 7-digit local number never false-matches.
_RESOLVE_SQL = r"""
WITH matches AS (
    SELECT 'tenant' AS kind, buildium_id,
           trim(coalesce(first_name, '') || ' ' || coalesce(last_name, '')) AS name,
           phone
      FROM helixis_tenants
     WHERE company_id = $1::uuid AND phone IS NOT NULL
       AND right(regexp_replace(phone, '\D', '', 'g'), 10) = $2
    UNION ALL
    SELECT 'owner' AS kind, buildium_id,
           coalesce(
               nullif(company_name, ''),
               trim(coalesce(first_name, '') || ' ' || coalesce(last_name, ''))
           ) AS name,
           phone
      FROM helixis_owners
     WHERE company_id = $1::uuid AND phone IS NOT NULL
       AND right(regexp_replace(phone, '\D', '', 'g'), 10) = $2
    UNION ALL
    SELECT 'vendor' AS kind, buildium_id,
           coalesce(
               nullif(name, ''),
               nullif(company_name, ''),
               trim(coalesce(first_name, '') || ' ' || coalesce(last_name, ''))
           ) AS name,
           phone
      FROM helixis_vendors
     WHERE company_id = $1::uuid AND phone IS NOT NULL
       AND right(regexp_replace(phone, '\D', '', 'g'), 10) = $2
)
SELECT kind, buildium_id, name, phone FROM matches
"""


@dataclass(frozen=True)
class ResolvedContact:
    """A Buildium contact a phone number resolved to.

    ``kind`` is ``"tenant" | "owner" | "vendor"``; ``buildium_id`` is that
    entity's Buildium id (the key every downstream tool — task creation, memory
    attribution — uses); ``phone`` is the stored (human-format) number that
    matched, not the normalized inbound one.
    """

    kind: str
    buildium_id: int
    name: str
    phone: str | None
    company_id: str


async def resolve_all_contacts_by_phone(
    pool: asyncpg.Pool, *, company_id: str, phone: str | None
) -> list[ResolvedContact]:
    """Every Buildium contact matching ``phone`` in this company, ordered by
    precedence (tenant → owner → vendor, then by buildium_id). Empty list when
    the number is unparseable or matches nobody."""
    key = phone_match_key(phone)
    if key is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(_RESOLVE_SQL, company_id, key)

    contacts = [
        ResolvedContact(
            kind=row["kind"],
            buildium_id=row["buildium_id"],
            name=(row["name"] or "").strip() or f"{row['kind'].title()} #{row['buildium_id']}",
            phone=row["phone"],
            company_id=company_id,
        )
        for row in rows
    ]
    contacts.sort(key=lambda c: (_KIND_RANK.get(c.kind, 99), c.buildium_id))
    return contacts


async def resolve_contact_by_phone(
    pool: asyncpg.Pool, *, company_id: str, phone: str | None
) -> ResolvedContact | None:
    """The single best Buildium contact for ``phone`` (tenant beats owner beats
    vendor). ``None`` when the number is unparseable or matches nobody.

    When more than one distinct contact matches, this returns the
    highest-precedence one and logs the ambiguity; callers that must
    disambiguate should use ``resolve_all_contacts_by_phone``.
    """
    contacts = await resolve_all_contacts_by_phone(pool, company_id=company_id, phone=phone)
    if not contacts:
        return None
    distinct = {(c.kind, c.buildium_id) for c in contacts}
    if len(distinct) > 1:
        logger.info(
            "phone resolved to %d distinct Buildium contacts; returning %s #%s by precedence",
            len(distinct),
            contacts[0].kind,
            contacts[0].buildium_id,
        )
    return contacts[0]


# ── outbound direction: a Buildium contact → a dial-ready number ──────────
# The other half of "one-click call/text ANYONE in Buildium": the inbound
# resolver above answers "who is this number?"; this answers "what number do I
# dial for this tenant/owner/vendor?" so the UI button and the agent tool have
# an E.164 ``to`` without the human copy-pasting from Buildium.

# kind → (mirror table, SQL name expression). Whitelisted so ``kind`` can never
# reach the query as raw text.
_CONTACT_SOURCES: dict[str, tuple[str, str]] = {
    "tenant": ("helixis_tenants", "trim(coalesce(first_name,'') || ' ' || coalesce(last_name,''))"),
    "owner": (
        "helixis_owners",
        "coalesce(nullif(company_name,''), trim(coalesce(first_name,'') || ' ' "
        "|| coalesce(last_name,'')))",
    ),
    "vendor": (
        "helixis_vendors",
        "coalesce(nullif(name,''), nullif(company_name,''), "
        "trim(coalesce(first_name,'') || ' ' || coalesce(last_name,'')))",
    ),
}


@dataclass(frozen=True)
class ContactCallTarget:
    """A Buildium contact resolved for an OUTBOUND call/text.

    ``phone_e164`` is the dial-ready number (``None`` when the contact has no
    usable number on file — the caller shows "no phone on file for X" rather
    than dialing garbage). ``phone_raw`` is the stored human-format value, kept
    for display alongside the normalized one.
    """

    kind: str
    buildium_id: int
    name: str
    phone_raw: str | None
    phone_e164: str | None

    @property
    def is_dialable(self) -> bool:
        return self.phone_e164 is not None


async def resolve_contact_call_target(
    pool: asyncpg.Pool, *, company_id: str, kind: str, buildium_id: int
) -> ContactCallTarget | None:
    """The dial target for a specific Buildium contact. ``None`` when the
    contact isn't in the mirror (or ``kind`` is unknown); a target with
    ``phone_e164=None`` when the contact exists but has no usable number.

    Company-scoped: a contact is only ever resolved inside the caller's own
    company.
    """
    source = _CONTACT_SOURCES.get(kind)
    if source is None:
        return None
    table, name_expr = source
    query = (
        f"SELECT buildium_id, {name_expr} AS name, phone "
        f"FROM {table} WHERE company_id = $1::uuid AND buildium_id = $2"
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, company_id, buildium_id)
    if row is None:
        return None
    return ContactCallTarget(
        kind=kind,
        buildium_id=row["buildium_id"],
        name=(row["name"] or "").strip() or f"{kind.title()} #{buildium_id}",
        phone_raw=row["phone"],
        phone_e164=normalize_e164(row["phone"]),
    )
