"""Golden cases that do not run while their system is on hold.

Rent Manager is on hold since 2026-10-06 (``docs/decisions.md``): its API
access costs $3,000 a year, paid upfront, and Brandon will not pay it yet.
Asked whether its golden cases should keep running, per PR or weekly, he
answered "none because we aren't using it yet". Each is a paid model call
against a connector that is switched off everywhere, and case 522's
over-broad pattern turned main red on 7cd31b87 for a system nobody uses.

A case is parked when its input names a parked system, as the company's
(``input.pms``) or as one the run switches on (``input.switched_on``). The
second covers 526, a Buildium run that switches Rent Manager on. The
connector's unit tests are free and keep running, so its code on main does
not rot.

⚠ Unparking is deleting the system's line, and it comes BEFORE the connector
is switched on anywhere: a system switched on with its cases parked has never
been measured against the live model.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: System key (``pms.registry``) → why its cases do not run.
PARKED: dict[str, str] = {
    "rentmanager": "Rent Manager is on hold (docs/decisions.md, 2026-10-06)",
}


def parked_reason(case: Mapping[str, Any]) -> str | None:
    """Why ``case`` does not run, or ``None`` when it runs."""
    block = case.get("input")
    if not isinstance(block, Mapping):
        return None
    switched_on = block.get("switched_on")
    named = [block.get("pms"), *(switched_on if isinstance(switched_on, list) else [])]
    for key in named:
        if isinstance(key, str) and key in PARKED:
            return PARKED[key]
    return None


__all__ = ["PARKED", "parked_reason"]
