"""Keep the Rentvine webhook deliveries the receiver refused (migration 0106).

WHY
    Measured 2026-10-04 (Render request log): Rentvine delivers to
    ``/webhooks/rentvine/{account}?timestamp=…&signature=…&token=…``, so the
    signature travels in the URL. The receiver refuses every delivery until the
    scheme is derived, and a refused delivery was kept nowhere: deriving it
    needed a tunnel to the founder's machine, which dropped twice in one
    afternoon and never received a delivery. Recording each refusal here lets
    ``scripts/rentvine_webhook_capture.py --analyse-db`` derive the scheme from
    deliveries Occupella already received, with the signing key held in the
    operator's own shell.

BOUNDED THREE WAYS
    - the receiver refuses a body over ``MAX_BODY_BYTES`` before this runs;
    - the receiver records at most ``RECORDS_PER_HOUR`` per company, because
      its IP rate limit allows far more than a diagnostic table needs;
    - this keeps the newest ``KEEP_PER_COMPANY`` rows per company and deletes
      the rest in the same transaction.

⚠ NEVER RAISES. The receiver answers 401 whether or not the row was written: a
database problem must not turn a refusal into a 500, which spends the sender's
retry budget on a delivery we refused anyway.

⚠ The bodies ARE Rentvine data, so the table is in ``buildium/purge.PURGE_TABLES``
and goes with the company's mirror on disconnect.
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote_plus

logger = logging.getLogger(__name__)

#: Rows kept per company. One delivery derives the scheme; a handful lets the
#: tool check that different deliveries agree (``consensus_scheme``).
KEEP_PER_COMPANY = 20

#: Refusals recorded per company per hour, per process. Read by the receiver.
RECORDS_PER_HOUR = 20

#: Header names never stored. Rentvine sends none of these, but a stranger's
#: request to the same URL can, and this table outlives the request.
DROPPED_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})

#: Query parameters whose value is never stored. ⚠ Rentvine fills ``signature``
#: with the account's signing key ITSELF (measured 2026-10-04: identical on two
#: deliveries 71 minutes apart; the real signature is in the body, gotcha 203).
#: Named rather than detected because before a key is saved there is nothing to
#: compare it with, and that is exactly when the first deliveries arrive.
KEY_BEARING_PARAMS = frozenset({"signature"})

#: What a withheld value is stored as.
REDACTED = "[redacted]"

#: Longest refusal reason kept. ``SignatureUnverifiable`` messages are one
#: sentence; this only stops an unexpected one from growing the row.
_REASON_MAX = 500

_INSERT = """
INSERT INTO public.helixis_rentvine_webhook_deliveries
    (company_id, method, path, query, headers, body, refused_because)
VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6, $7)
"""

_PRUNE = """
DELETE FROM public.helixis_rentvine_webhook_deliveries
 WHERE company_id = $1::uuid
   AND id NOT IN (
       SELECT id
         FROM public.helixis_rentvine_webhook_deliveries
        WHERE company_id = $1::uuid
        ORDER BY id DESC
        LIMIT $2
   )
"""


def _is_key(value: str, secret: str | None) -> bool:
    return bool(secret) and hmac.compare_digest(
        value.strip().encode(), str(secret).strip().encode()
    )


def stored_headers(headers: Mapping[str, str], secret: str | None = None) -> dict[str, str]:
    """The headers as stored: names as sent, minus :data:`DROPPED_HEADERS`, and
    any value that is the signing key replaced by :data:`REDACTED`."""
    return {
        k: (REDACTED if _is_key(v, secret) else v)
        for k, v in headers.items()
        if k.lower() not in DROPPED_HEADERS
    }


def stored_query(query: str, secret: str | None = None) -> str:
    """The RAW query as stored, minus the signing key.

    Every parameter keeps its arrival order and encoding (a signature over the
    URL is over the characters as sent), except a :data:`KEY_BEARING_PARAMS`
    value or any value equal to the key, which becomes :data:`REDACTED`.
    """
    out = []
    for part in query.split("&"):
        name, eq, value = part.partition("=")
        if eq and (
            unquote_plus(name) in KEY_BEARING_PARAMS or _is_key(unquote_plus(value), secret)
        ):
            out.append(f"{name}={REDACTED}")
        else:
            out.append(part)
    return "&".join(out)


async def record_refused_delivery(
    pool: Any,
    *,
    company_id: str,
    method: str,
    path: str,
    query: str,
    headers: Mapping[str, str],
    body: bytes,
    reason: str,
    secret: str | None = None,
) -> bool:
    """Store one refused delivery verbatim and prune to the newest rows.

    Returns whether the row was written. ``query`` is the RAW query string and
    ``body`` the bytes that arrived: the signature covers them as sent.
    ``secret`` is the company's signing key, when one is saved, so a copy of it
    anywhere in the query or headers is withheld (:func:`stored_query`).
    """
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                _INSERT,
                company_id,
                method,
                path,
                stored_query(query, secret),
                json.dumps(stored_headers(headers, secret)),
                body,
                reason[:_REASON_MAX],
            )
            await conn.execute(_PRUNE, company_id, KEEP_PER_COMPANY)
    except Exception as exc:  # noqa: BLE001 — the receiver's 401 must not depend on this
        # The exception TYPE only. asyncpg's data errors quote the offending
        # parameter, and one of the parameters is a delivery body.
        logger.warning(
            "could not record a refused rentvine webhook for company=%s (%s)",
            company_id,
            type(exc).__name__,
        )
        return False
    return True


__all__ = [
    "DROPPED_HEADERS",
    "KEEP_PER_COMPANY",
    "KEY_BEARING_PARAMS",
    "RECORDS_PER_HOUR",
    "REDACTED",
    "record_refused_delivery",
    "stored_headers",
    "stored_query",
]
