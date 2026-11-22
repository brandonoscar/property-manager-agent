"""What asyncpg accepts for a temporally-cast bind parameter.

⚠ **THIS EXISTS BECAUSE A FAKE POOL WAS MORE PERMISSIVE THAN THE DRIVER, AND
THE TEST THAT USED IT PASSED ON THE DEFECT.** ``GET /api/v1/activity`` typed
its keyset ``cursor`` as ``str`` and bound it to ``created_at <
$N::timestamptz``. asyncpg refuses that outright — ``DataError: invalid input
for query argument $2: '2026-08-27T07:46:34.797181+00:00' (expected a
datetime.date or datetime.datetime instance, got 'str')`` — so every "Load
more" click on the Activity page was a 500 (Sentry PYTHON-FASTAPI-3G, a real
user in Lititz on Chrome 151). The endpoint's own test exercised that exact
path and was green, because its fake did ``datetime.fromisoformat(arg)`` — it
required the string the driver rejects.

A fake that accepts what production refuses is not a weaker test, it is an
inverted one: it holds the defect in place (gotchas 68/74/93/110) while
reading as coverage.

**The rule, READ from the installed asyncpg 0.31.0 rather than recalled**
(gotcha 79 — an SDK's behaviour is not evidence until you look):

- ``pgproto/codecs/datetime.pyx::timestamptz_encode`` raises ``TypeError(
  'expected a datetime.date or datetime.datetime instance, got …')`` for
  anything that is neither, and ``prepared_stmt.pyx`` wraps it as a
  ``DataError`` naming the position.
- ``date_encode`` calls ``obj.toordinal()``, so a ``str`` dies there too.

Both reduce to the same bar: a temporally-cast parameter takes a
``datetime.date`` (``datetime`` subclasses it) or ``None``. Nothing else.

⚠ **Scoped to the TEMPORAL casts on purpose.** asyncpg's uuid codec *does*
accept a ``str``, so widening this to every ``$N::<type>`` would manufacture a
finding on ``$1::uuid``, which is correct code — a guard that flags correct
code is the one people switch off (gotchas 52/103/107).
"""

from __future__ import annotations

import datetime as _dt
import numbers as _numbers
import re
from typing import Any

#: ``$5::date`` — a cast applied to a BIND PARAMETER. Deliberately anchored on
#: the ``$N``: ``l.lease_to_date::date`` is a cast on a COLUMN and carries no
#: argument to check.
_TEMPORAL_BIND = re.compile(r"\$(\d+)::(timestamptz|timestamp|date)\b")


def temporal_bind_positions(sql: str) -> set[int]:
    """1-based parameter positions in ``sql`` carrying a temporal cast."""
    return {int(m.group(1)) for m in _TEMPORAL_BIND.finditer(sql)}


def reject_unbindable(sql: str, args: tuple[Any, ...] | list[Any]) -> None:
    """Raise the way asyncpg does when a temporal parameter gets a non-date.

    Call this at the top of every fake ``fetch``/``fetchrow``/``execute`` that
    stands in for a pool running SQL with a ``$N::timestamptz`` or ``$N::date``
    parameter. Silent on everything else, so it is safe to call unconditionally.
    """
    for position in sorted(temporal_bind_positions(sql)):
        if position > len(args):
            # The fake was handed fewer arguments than the SQL names. That is
            # its own bug, but it is not THIS bug — say nothing rather than
            # raise a confusing error from the wrong guard.
            continue
        value = args[position - 1]
        if value is None or isinstance(value, _dt.date):
            continue
        raise TypeError(
            f"invalid input for query argument ${position}: {value!r} "
            f"(expected a datetime.date or datetime.datetime instance, "
            f"got {type(value).__name__!r}) — asyncpg refuses this in "
            f"production; see tests/unit/_pg_bind_contract.py"
        )


#: Mirror columns whose Postgres type refuses a ``str``, and what they take.
#:
#: ⚠ **AN UNCAST BIND IS TYPED BY THE TARGET COLUMN, which is why
#: :func:`reject_unbindable` alone cannot see this.** That one anchors on an
#: explicit ``$N::timestamptz`` in the SQL. ``rentvine/sync.py::_upsert`` writes
#: a plain ``$8`` into ``helixis_leases.lease_from_date``, so Postgres reports
#: the parameter's type from the INSERT target and asyncpg picks the codec off
#: that OID — the same refusal, with nothing in the query text to match on.
#: Measured: the real ``sync_company`` raised on the first lease, and the units
#: stage that runs on the next line was never reached.
#:
#: ⚠ **The types are DECLARED here rather than parsed out of the schema**, and
#: the guard that owns this reads the schema to check them. A scan building its
#: own map missed ``market_rent`` outright, because that column is created in
#: ``supabase/migrations/0038`` while the others live in
#: ``db/schema/helixis_entities.sql`` — one file read, a clean result, and a
#: real defect underneath it (gotchas 49/100/101: a scan that finds nothing is
#: a claim to check).
MIRROR_COLUMN_TYPES: dict[str, str] = {
    "lease_from_date": "timestamptz",
    "lease_to_date": "timestamptz",
    # helixis_lease_tenants (0038), written by the Rentvine lease pass since
    # 2026-10-01 from `leaseTenant.moveInDate` — a spec date STRING.
    "move_in_date": "timestamptz",
    "market_rent": "numeric",
    "rent": "numeric",
    "security_deposit": "numeric",
    # helixis_bills, synced from Rentvine since 2026-10-01 (sync._sync_bills).
    "bill_date": "date",
    "due_date": "date",
    "total_amount": "numeric",
    # helixis_lease_balances, synced from Rentvine since 2026-10-01.
    "total_balance": "numeric",
    "balance_0_30": "numeric",
    "balance_31_60": "numeric",
    "balance_61_90": "numeric",
    "balance_over_90": "numeric",
    "snapshot_at": "timestamptz",
    # helixis_work_orders (0105), Rentvine's `dateTimeModified`, a spec STRING.
    "pms_updated_at": "timestamptz",
}

_INSERT_COLUMNS = re.compile(r"INSERT INTO \w+\s*\(([^)]*)\)", re.IGNORECASE)


def reject_untyped_mirror_bind(sql: str, args: tuple[Any, ...] | list[Any]) -> None:
    """Raise the way asyncpg does when a MIRROR column gets a wrong Python type.

    The companion to :func:`reject_unbindable` for the uncast case: it reads the
    INSERT's own column list and positionally checks the ones
    :data:`MIRROR_COLUMN_TYPES` names. Silent on every other statement, so it is
    safe to call unconditionally at the top of a fake ``execute``.

    ⚠ **A fake that accepts what the driver refuses is not a weaker test, it is
    an inverted one.** ``test_rentvine_sync.py``'s pool was a bare ``AsyncMock``
    that accepted anything, so a full green suite sat on top of a sync that
    aborted on its first lease in production (gotcha 123's shape, second
    instance).
    """
    match = _INSERT_COLUMNS.search(sql)
    if match is None:
        return
    columns = [c.strip() for c in match.group(1).split(",")]
    for position, column in enumerate(columns):
        expected = MIRROR_COLUMN_TYPES.get(column)
        if expected is None or position >= len(args):
            continue
        value = args[position]
        if value is None:
            continue
        if expected in {"timestamptz", "date"} and not isinstance(value, _dt.date):
            raise TypeError(
                f"invalid input for query argument ${position + 1} ({column}): "
                f"{value!r} (expected a datetime.date or datetime.datetime "
                f"instance, got {type(value).__name__!r}) — asyncpg refuses "
                f"this in production; see tests/unit/_pg_bind_contract.py"
            )
        if expected == "numeric" and not isinstance(value, _numbers.Number):
            raise TypeError(
                f"invalid input for query argument ${position + 1} ({column}): "
                f"{value!r} is not a number and the column is numeric(12, 2) "
                f"— see tests/unit/_pg_bind_contract.py"
            )


__all__ = [
    "MIRROR_COLUMN_TYPES",
    "reject_unbindable",
    "reject_untyped_mirror_bind",
    "temporal_bind_positions",
]
