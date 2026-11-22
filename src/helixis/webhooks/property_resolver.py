"""Extract the Buildium property id from a webhook envelope (PR #80).

Each Buildium event family carries the property id in a slightly
different place — Lease events under ``PropertyId`` on the body,
Bill events under ``Lines[].PropertyId`` in the body, etc. This
module is the single point where those paths are encoded so the
routing pipeline has a uniform call.

Events that aren't tied to a property (vendor created, rental owner
updated, general task with no property association) return ``None``,
which signals the router to fall back to the "(no property)" default
row in ``property_assignments``.

Implementation policy: NEVER guess. If an event family hasn't been
verified against an actual Buildium envelope or the published spec,
this module returns ``None`` so the router falls back cleanly. A
future PR can add resolver entries as new event families are
encountered live.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def extract_property_id(event_name: str, envelope: dict[str, Any]) -> int | None:
    """Return the Buildium property id this event is about, or ``None``.

    ``None`` means "no property association on this event" — the
    router treats that as a signal to use the company-wide default
    row in ``property_assignments`` (the NULL property_id row).

    The envelope may carry the entity body either at the top level
    (older Buildium webhook shape) or nested under ``Body`` (newer
    shape). We check both so neither layout breaks resolution.
    """
    body = _envelope_body(envelope)

    # Rental.* and RentalUnit.* — the property id IS the entity id
    # for Rental events; for RentalUnit events it's nested.
    if event_name.startswith("Rental.") and not event_name.startswith("RentalUnit."):
        return _first_int(body, ("PropertyId", "Id"))
    if event_name.startswith("RentalUnit."):
        return _first_int(body, ("PropertyId",))

    # Lease family — every lease-attached event carries PropertyId.
    if event_name.startswith(("Lease.", "LeaseTenant.", "LeaseTransaction.", "LeaseRenewal.")):
        return _first_int(body, ("PropertyId",))

    # Maintenance — work orders + tenant maintenance requests.
    if event_name.startswith(("WorkOrder.", "TenantRequest.")):
        return _first_int(body, ("PropertyId",))

    # Tasks — Buildium emits PropertyId on tasks that are scoped to
    # a property; tasks without PropertyId return None and route via
    # the default row.
    if event_name.startswith("Task."):
        return _first_int(body, ("PropertyId",))

    # Listings + applicants — leasing-side events with PropertyId.
    if event_name.startswith(("Listing.", "Applicant.", "ApplicantApplication.")):
        return _first_int(body, ("PropertyId",))

    # Bills — the bill body itself has no PropertyId; the per-GL-line
    # records under ``Lines[]`` do. We return the first non-null
    # PropertyId we find so routing groups multi-line bills under
    # their primary property. (A future PR could fan out to multiple
    # rows if that becomes the right shape.)
    if event_name.startswith("Bill."):
        lines = body.get("Lines") or []
        if isinstance(lines, list):
            for line in lines:
                if isinstance(line, dict):
                    pid = _first_int(line, ("PropertyId",))
                    if pid is not None:
                        return pid
        return None

    # Vendors, rental owners, owner contributions, vendor payments,
    # tenant identity events — no property tie. Route via the
    # default row.
    return None


def _envelope_body(envelope: dict[str, Any]) -> dict[str, Any]:
    body = envelope.get("Body")
    if isinstance(body, dict):
        return body
    return envelope


def _first_int(d: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for k in keys:
        value = d.get(k)
        if isinstance(value, bool):
            # ``isinstance(True, int)`` is True; reject explicitly so a
            # boolean field can never masquerade as a property id.
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


__all__ = ["extract_property_id"]
