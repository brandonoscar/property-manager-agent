"""Writes a vendor refused with a 403, remembered on the company's credentials row.

Each connector's credentials table carries ``capabilities jsonb`` (migration
0107, design "built correctly"). It holds one thing: ``{REFUSED_KEY: {operation
key: when}}``, the writes the vendor refused with a 403 for this company. A
write tool reads it before showing a card, so an attempt within
:data:`REFUSAL_REMEMBERED` of a refusal explains instead of asking somebody to
approve a change the vendor will refuse again. Saving the connection again
clears the column (each connector's save route), because a permission granted
in the vendor's own screens is something Occupella cannot see.

One copy for every connector: Propertyware's tools used to hold the only one,
and Rent Manager's design promised the same memory with nothing writing it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

#: How long a refused write is answered from memory. Bounded, because a
#: permission granted in the vendor's screens is not something Occupella can
#: see; saving the connection again clears it at once.
REFUSAL_REMEMBERED = timedelta(hours=24)

#: The ``capabilities`` key that holds ``{operation key: when it was refused}``.
REFUSED_KEY = "write_refused"


def _table(table: str) -> str:
    """A credentials table's name, checked: it is written into the SQL."""
    if not table.isidentifier():
        raise ValueError(f"not a table name: {table!r}")
    return table


def _as_dict(value: Any) -> dict[str, Any]:
    """A ``jsonb`` value as asyncpg hands it over (text without a codec)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def refused_at(pool: Any, table: str, company_id: str, operation: str) -> datetime | None:
    """When the vendor last refused ``operation`` (``"POST /workorders"``) for
    this company with a 403, or ``None``. Unreadable is ``None``: the write is
    then tried, and a refusal answered as it comes."""
    row = await pool.fetchrow(
        f"SELECT capabilities FROM public.{_table(table)} WHERE company_id = $1::uuid",  # noqa: S608 — a checked module constant
        company_id,
    )
    if not row:
        return None
    refused = _as_dict(_as_dict(row.get("capabilities")).get(REFUSED_KEY))
    return _instant(refused.get(operation))


def still_refused(when: datetime | None, *, now: datetime | None = None) -> bool:
    """Whether a refusal at ``when`` is still answered from memory."""
    if when is None:
        return False
    return (now or datetime.now(UTC)) - when < REFUSAL_REMEMBERED


async def remember_refused(pool: Any, table: str, company_id: str, operation: str) -> None:
    """Record that the vendor refused ``operation`` for this company (a 403),
    keeping every other key the column holds."""
    await pool.execute(
        f"""
        UPDATE public.{_table(table)}
           SET capabilities = coalesce(capabilities, '{{}}'::jsonb)
               || jsonb_build_object(
                    '{REFUSED_KEY}',
                    coalesce(capabilities -> '{REFUSED_KEY}', '{{}}'::jsonb)
                    || jsonb_build_object($2::text, to_jsonb(now()))
                  )
         WHERE company_id = $1::uuid
        """,  # noqa: S608 — a checked module constant; the operation key is a bound value
        company_id,
        operation,
    )


__all__ = [
    "REFUSAL_REMEMBERED",
    "REFUSED_KEY",
    "refused_at",
    "remember_refused",
    "still_refused",
]
