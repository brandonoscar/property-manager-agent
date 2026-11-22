"""Public SMS privacy/terms pages — the URLs a campaign registration cites.

⚠ **Deliberately UNAUTHENTICATED.** Twilio and the carrier reviewer fetch
these with no credentials; a page behind a JWT is a page they read as missing,
which is the 30933/30908 rejection this exists to prevent. That is also why
there is no rate-limit exemption to add and nothing here reads a secret: the
only data on the page is a legal business name, which is public record and is
already on the brand registration the reviewer is holding.

⚠ **Keyed by ``company_id``, and that is a considered choice.** The alternative
is a random slug column, which is one migration away if the enumeration
surface ever matters. Today the id is already present in every client API call,
the page discloses nothing the registry does not publish, and a UUID is not
guessable — so a slug would buy tidiness, not privacy.

A company with no registration row 404s rather than rendering a blank page: an
empty policy fetched by a carrier is a CONTENT rejection, which sends the next
reader looking at the wording instead of at the missing row.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response

from helixis.comms import trust_pages
from helixis.db.engine import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(tags=["trust"])

# The legal name is the ONLY field these pages render. Scoped by company_id,
# which is this table's unique key (migration 0084).
_BUSINESS_NAME_SQL = (
    "SELECT legal_business_name FROM public.helixis_a2p_registrations WHERE company_id = $1::uuid"
)


async def _business_name(company_id: str) -> str:
    try:
        row = await get_pool().fetchrow(_BUSINESS_NAME_SQL, company_id)
    except Exception:
        # A DB blip must not render a policy page with no company on it —
        # a carrier reads that as a non-compliant document, not an outage.
        logger.exception("trust page lookup failed for company=%s", company_id)
        raise HTTPException(status_code=503, detail="Temporarily unavailable.") from None
    name = str((row or {}).get("legal_business_name") or "").strip()
    if not name:
        raise HTTPException(status_code=404, detail="No messaging program is registered here.")
    return name


@router.get("/c/{company_id}/{document}")
async def trust_page(company_id: str, document: str, request: Request) -> Response:
    """One customer's SMS privacy policy or terms, as public HTML."""
    if document not in (trust_pages.PRIVACY, trust_pages.TERMS):
        raise HTTPException(status_code=404, detail="Not found.")
    name = await _business_name(company_id)
    return Response(
        content=trust_pages.render(document, name),
        media_type="text/html; charset=utf-8",
        # Cheap to regenerate and read rarely, but a carrier may fetch it more
        # than once during vetting; a short cache keeps a retry off the DB
        # without making a corrected policy take a day to propagate.
        headers={"Cache-Control": "public, max-age=300"},
    )
