"""Every property-management system Occupella knows, in one place.

WHY
    Before this module, "which PMS is this company on" was answered in about a
    dozen places as "Rentvine, else Buildium" (design/connector-shared-layer.md
    lists them). A third system would have been resolved to Buildium: shown
    Buildium's live tools (each answering "connect Buildium"), given Buildium's
    full table coverage (so every money detector would run over tables it never
    fills), and left out of the one-PMS trigger. Those places now ask here.

FROZEN AFTER PHASE 0
    Brandon's plan locks the shared layer first, then builds the connectors in
    parallel, and two branches editing shared files is exactly what the plan
    exists to prevent. So a connector never edits this file to make itself
    known. Its package exposes what shared code needs (`sync.SYNC_STATE_STAGE`,
    `sync.DERIVED_TABLES`, `sync.RUN_NOTE`), and `connector_attr` finds them
    lazily. Before the package exists the lookup returns None, which every
    caller reads as "fills nothing, says nothing": the fail-closed answer.
    The deployment switches (:func:`enabled`, :func:`writes_enabled`) are read
    here at call time, from ``Settings.<key>`` (design P4).

⚠ Buildium is the DEFAULT as well as an entry. A company with no credentials
row at all is treated as Buildium (`db.company_pms.DEFAULT_PMS`), so Buildium
has no `WHEN` in that query and no kill switch here.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

BUILDIUM = "buildium"
RENTVINE = "rentvine"
RENTMANAGER = "rentmanager"
PROPERTYWARE = "propertyware"
DOORLOOP = "doorloop"


@dataclass(frozen=True)
class PmsSpec:
    """One property-management system."""

    #: The value stored in every mirror row's `pms` column (migration 0101) and
    #: bound for a run (`agents.run_pms`).
    key: str
    #: How the system is named to a customer.
    name: str
    #: The per-company credentials table. A row there is what puts a company on
    #: this system, and the one-PMS trigger refuses a second system's row.
    credentials_table: str
    #: True only for Buildium: live API read tools exist. Every other system is
    #: read from the mirror only, and a run on it hides Buildium's live tools.
    live_tools: bool
    #: The connector's package, imported lazily by `connector_attr`.
    package: str


#: Buildium first; the order is otherwise the order the systems were added.
PMS: Mapping[str, PmsSpec] = MappingProxyType(
    {
        BUILDIUM: PmsSpec(
            BUILDIUM, "Buildium", "company_buildium_credentials", True, "helixis.buildium"
        ),
        RENTVINE: PmsSpec(
            RENTVINE, "Rentvine", "company_rentvine_credentials", False, "helixis.rentvine"
        ),
        RENTMANAGER: PmsSpec(
            RENTMANAGER,
            "Rent Manager",
            "company_rentmanager_credentials",
            False,
            "helixis.rentmanager",
        ),
        PROPERTYWARE: PmsSpec(
            PROPERTYWARE,
            "Propertyware",
            "company_propertyware_credentials",
            False,
            "helixis.propertyware",
        ),
        # Added 2026-10-06 (design/doorloop-connector.md), before its package
        # has a sync module: until it does, `connector_attr` answers None and
        # every reader takes DoorLoop as "fills nothing, says nothing".
        DOORLOOP: PmsSpec(
            DOORLOOP, "DoorLoop", "company_doorloop_credentials", False, "helixis.doorloop"
        ),
    }
)


def spec(key: str | None) -> PmsSpec | None:
    """The system for `key`, or None for no key or an unknown one."""
    return PMS.get(key) if key else None


def is_mirror_pms(key: str | None) -> bool:
    """A known system that is read from the mirror only (every PMS but Buildium)."""
    found = spec(key)
    return found is not None and not found.live_tools


def name_of(key: str | None) -> str | None:
    """The customer-facing name, or None when `key` is not a known system."""
    found = spec(key)
    return found.name if found is not None else None


def _switches(key: str) -> Any:
    """This system's deployment switches (``Settings.<key>``), or ``None``."""
    from helixis.settings import get_settings

    return getattr(get_settings(), key, None)


def enabled(key: str) -> bool:
    """Is this system's connector switched on in this deployment?

    Read at call time, so a test or a redeploy is never cached away. Buildium
    has no switch and is always on; an unknown key is off. Read by
    ``pms.sync_cycle``.
    """
    if key == BUILDIUM:
        return True
    if spec(key) is None:
        return False
    return bool(getattr(_switches(key), "enabled", False))


def writes_enabled(key: str) -> bool:
    """Are this system's write tools switched on in this deployment?

    Only the connectors built on ``helixis.pms`` have the switch (design P4);
    for the rest this is False, and nothing asks it: Buildium's and Rentvine's
    writes have their own gates. Off whenever the connector itself is off.
    Read by ``pms.gated_write`` and ``agents.run_pms.pms_write_tool_enabled``.
    """
    if key in (BUILDIUM, RENTVINE) or not enabled(key):
        return False
    return bool(getattr(_switches(key), "writes_enabled", False))


def connector_attr(key: str | None, module: str, attr: str) -> Any | None:
    """`<package>.<module>.<attr>` for a system, or None while it does not exist.

    ⚠ Only a MISSING connector module reads as None. An import that fails INSIDE
    an existing module is a bug and is raised: hiding it would make a working
    connector read as "fills nothing", and every reader would decline in silence.
    """
    found = spec(key)
    if found is None:
        return None
    target = f"{found.package}.{module}"
    try:
        loaded = importlib.import_module(target)
    except ModuleNotFoundError as exc:
        if exc.name in (target, found.package):
            return None
        raise
    return getattr(loaded, attr, None)


__all__ = [
    "BUILDIUM",
    "DOORLOOP",
    "PMS",
    "PROPERTYWARE",
    "RENTMANAGER",
    "RENTVINE",
    "PmsSpec",
    "connector_attr",
    "enabled",
    "is_mirror_pms",
    "name_of",
    "spec",
    "writes_enabled",
]
