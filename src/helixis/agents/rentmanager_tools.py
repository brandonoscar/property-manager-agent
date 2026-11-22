"""Rent Manager's agent writes (design R4), through ``pms.gated_write``.

Two tools: create a work order, and change a work order's status. The third
write the design lists, a note, is not built: the help scrape names no
HistoryNotes path or fields for it (RM-15), and documented endpoints only.

Each tool delegates to an ``_<name>_impl`` coroutine, which does three
things and nothing else: refuses while the writes are switched off, opens a
client on THIS company's Rent Manager credentials (no env fallback anywhere:
gotcha 70), and makes ONE ``gated_write`` call. The gate runs the whole order
(switch, company, role floor, the read and the plan, the money refusal, the
card, the idempotency claim, the re-read, the one send, the read-back), and
``test_write_confirm_gate`` holds this module to calling it and never
``run_guarded`` itself. What is sent, and what each card and read-back says,
is ``rentmanager/writes.py``, where the work list's markers are.

Shown to the model only on a Rent Manager run with Rent Manager's writes
switched on: ``agents.tool_registries.connector_tools`` sets that on copies
of these tools, so this module never sets ``is_enabled`` itself.

A 403 on a write is remembered on the credentials row (``pms.refusals``, the
design's ``capabilities`` column), so the next attempt within a day explains
instead of asking somebody to approve a change Rent Manager will refuse again.
Saving the connection again clears it (``api/routes/rentmanager.py``).
"""

from __future__ import annotations

import logging
from typing import Any

from agents import function_tool

from helixis.db.engine import get_pool
from helixis.memory.context import current_memory_context
from helixis.pms import refusals, registry
from helixis.pms.errors import Failure, PmsApiError
from helixis.pms.gated_write import Send, WritePlan, gated_write, refusal
from helixis.pms.http import PmsHttpClient, PmsResponse
from helixis.rentmanager import writes
from helixis.rentmanager.client import NAME, PMS, build_client
from helixis.rentmanager.credentials import TABLE, CredentialsUnusableError, load_credentials

logger = logging.getLogger(__name__)

#: ``pms.gated_write``'s own words for a switched-off connector, said before
#: any credential is read: "Connect it" would send the person to a page that
#: refuses to save while the switch is off (rule 16).
#: ``test_rentmanager_writes`` holds the two to the same sentence.
SWITCHED_OFF = (
    f"Changing records in {NAME} from Occupella isn't switched on, so I didn't change "
    f"anything. Make the change in {NAME} directly."
)
NOT_CONNECTED = (
    f"{NAME} isn't connected for this workspace, so there's nothing for me to change. "
    f"Connect {NAME} on the Connectors page (Settings → Connectors)."
)
UNUSABLE = (
    f"This workspace's saved {NAME} connection can't be read, so I didn't change anything "
    f"in {NAME}. Reconnect it on the Connectors page (Settings → Connectors): enter the "
    "details again and click Save and test."
)
UNREADABLE = (
    f"I couldn't read this workspace's {NAME} connection just now, so I didn't change "
    f"anything in {NAME}. Try again in a minute."
)


#: The failure ``gated_write`` words a 403 from, for recognising its sentence.
_FORBIDDEN = PmsApiError(403, Failure.PERMISSION, False, "")


def _company_id() -> str:
    ctx = current_memory_context()
    return ctx.company_id if ctx else ""


def remembered_refusal(doing: str) -> str:
    """The reply when Rent Manager refused this write within the day: no card,
    because approving it would only be refused again (``pms.refusals``)."""
    return (
        f"I couldn't {doing}: {NAME} refused it the last time, within the past day, because "
        f"the {NAME} user Occupella signs in as isn't allowed to do it. That is changed in "
        f"{NAME}'s user permissions; after that, enter the connection details again on the "
        f"Connectors page (Settings → Connectors), click Save and test, and ask again. Nothing "
        f"was changed in {NAME}."
    )


async def _refused_before(operation: str, doing: str) -> str | None:
    """:func:`remembered_refusal` when this write was refused within the day,
    else ``None``. Best effort: an unreadable memory means "try"."""
    try:
        when = await refusals.refused_at(get_pool(), TABLE, _company_id(), operation)
    except Exception:  # noqa: BLE001 — the memory only saves a card that would fail
        logger.warning("rentmanager write: reading the remembered refusals failed", exc_info=True)
        return None
    return remembered_refusal(doing) if refusals.still_refused(when) else None


async def _remember_if_refused(reply: str, operation: str, doing: str) -> str:
    """Remember a 403 on this write, so the next attempt within the day explains
    instead of showing a card. ``gated_write``'s own 403 sentence is recognised
    by equality, never by its words; the reply itself is unchanged."""
    if reply == refusal(_FORBIDDEN, action=doing, name=NAME):
        try:
            await refusals.remember_refused(get_pool(), TABLE, _company_id(), operation)
        except Exception:  # noqa: BLE001 — the refusal stands without the memory
            logger.warning("rentmanager write: remembering a refused write failed", exc_info=True)
    return reply


async def _open_client() -> PmsHttpClient | str:
    """A client on THIS company's Rent Manager account, or the reply that says
    why there is none. Nothing is sent to Rent Manager here."""
    if not registry.writes_enabled(PMS):
        return SWITCHED_OFF
    company_id = _company_id()
    if not company_id:
        return NOT_CONNECTED
    try:
        creds = await load_credentials(get_pool(), company_id)
    except CredentialsUnusableError:
        return UNUSABLE
    except Exception:  # noqa: BLE001 — a reply, never a crash mid-turn
        logger.exception("rentmanager write: reading company %s's credentials failed", company_id)
        return UNREADABLE
    if creds is None:
        return NOT_CONNECTED
    return build_client(creds)


async def _create_work_order_impl(
    property: str,  # noqa: A002 — the model's word for it
    title: str,
    description: str = "",
    unit: str | None = None,
    priority: str | None = None,
    category: str | None = None,
) -> str:
    """Create one Rent Manager work order. See :func:`rentmanager_create_work_order`.

    The property and unit come by NAME: no tool on a Rent Manager run shows
    their ids, so the names are resolved against this company's synced
    properties and units, and Rent Manager is then read by id for the card."""
    text = " ".join((title or "").split())
    if not text:
        return (
            f"A work order needs a title saying what the problem is. Nothing was created in {NAME}."
        )
    about = (description or "").strip()
    opened = await _open_client()
    if isinstance(opened, str):
        return opened
    async with opened as client:

        async def prepare() -> WritePlan | str:
            before = await _refused_before(writes.CREATE_OPERATION, writes.CREATE_DOING)
            if before is not None:
                return before
            place = await writes.find_place(get_pool(), _company_id(), property, unit)
            if isinstance(place, str):
                return place
            return await writes.plan_create(
                client,
                property_id=place[0],
                unit_id=place[1],
                title=text,
                description=about,
                priority=priority,
                category=category,
            )

        async def verify(plan: WritePlan, response: PmsResponse) -> bool:
            return await writes.verify_create(client, plan, response)

        reply = await gated_write(client=client, prepare=prepare, verify=verify)
    return await _remember_if_refused(reply, writes.CREATE_OPERATION, writes.CREATE_DOING)


async def _update_work_order_status_impl(work_order_id: int, status: str) -> str:
    """Change one Rent Manager work order's status. See
    :func:`rentmanager_update_work_order_status`."""
    wanted = " ".join((status or "").split())
    if not wanted:
        return f"Which status should the work order have? Nothing was changed in {NAME}."
    opened = await _open_client()
    if isinstance(opened, str):
        return opened
    issue_id = int(work_order_id)
    async with opened as client:
        planned: dict[str, writes.StatusChange] = {}

        async def prepare() -> WritePlan | str:
            before = await _refused_before(writes.STATUS_OPERATION, writes.STATUS_DOING)
            if before is not None:
                return before
            change = await writes.plan_status(client, issue_id=issue_id, status=wanted)
            if isinstance(change, str):
                return change
            planned["change"] = change
            return change.plan

        async def recheck(_plan: WritePlan) -> Send | None:
            return writes.status_recheck(
                planned["change"], await writes.read_issue(client, issue_id)
            )

        async def verify(_plan: WritePlan, _response: PmsResponse) -> bool:
            return writes.status_took(
                await writes.read_issue(client, issue_id), planned["change"].new_status_id
            )

        # Design: a 409 re-reads, and resends once under the same approval only
        # if everything the approver saw is unchanged (the concurrency value is
        # the plan's ``volatile``). The gate bounds it to two sends.
        reply = await gated_write(
            client=client,
            prepare=prepare,
            verify=verify,
            recheck=recheck,
            resend_after_conflict=True,
        )
    return await _remember_if_refused(reply, writes.STATUS_OPERATION, writes.STATUS_DOING)


@function_tool
async def rentmanager_create_work_order(
    property: str,  # noqa: A002 — the name the model is shown
    title: str,
    description: str = "",
    unit: str | None = None,
    priority: str | None = None,
    category: str | None = None,
) -> str:
    """Create a work order in Rent Manager (a write — confirm-gated).

    Use for "open a work order for the leak in C-101" when the company is on
    RENT MANAGER. Confirmation is enforced in code; you do NOT add a separate
    confirmation step, and only a Manager or Admin may approve it.
    ``property`` is the property's NAME as the other tools show it
    (``property_snapshot``, ``portfolio_overview``); ``unit`` is the unit's
    name at that property ("C-101"), left out for a problem with the whole
    property. If either does not match, the reply lists what does.
    ``title`` is a short summary; ``description`` the detail.

    ``priority`` and ``category`` are the NAMES this account uses in Rent
    Manager; leave either out to let Rent Manager apply its default. If a name
    is not one of the account's, the reply lists the ones it has. The work
    order starts in Rent Manager's own starting status. This tool does not
    assign a vendor, notify anyone or change any other record.
    """
    return await _create_work_order_impl(property, title, description, unit, priority, category)


@function_tool
async def rentmanager_update_work_order_status(work_order_id: int, status: str) -> str:
    """Change a Rent Manager work order's status (a write — confirm-gated).

    Use for "mark the faucet work order Closed" when the company is on RENT
    MANAGER. Confirmation is enforced in code; you do NOT add a separate
    confirmation step, and only a Manager or Admin may approve it.

    ``status`` is the status NAME as this account has it in Rent Manager
    ("Closed", "Waiting on Parts"), not a number; if the account has no such
    status, the reply lists the ones it has. Only the status changes: the
    title, description, priority and everything else stay as they are.
    """
    return await _update_work_order_status_impl(work_order_id, status)


def rentmanager_tools() -> list[Any]:
    """Rent Manager's agent writes. ``agents.tool_registries`` finds this
    factory and shows copies only on a Rent Manager run with writes on."""
    return [rentmanager_create_work_order, rentmanager_update_work_order_status]


__all__ = [
    "NOT_CONNECTED",
    "SWITCHED_OFF",
    "UNREADABLE",
    "UNUSABLE",
    "rentmanager_create_work_order",
    "rentmanager_tools",
    "rentmanager_update_work_order_status",
]
