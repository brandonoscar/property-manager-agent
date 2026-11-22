"""Direct Buildium API tools exposed to the orchestrator.

These wrap our in-house :class:`BuildiumApi` so the orchestrator can answer
questions like "how many properties do I have" with a single API call,
instead of falling back to browser automation.

Each tool:

* Reads ``company_id`` from the per-request :class:`MemoryContext`
  (set by :func:`run_agent_streaming`), the same pattern used by
  ``search_memory`` and ``store_memory``.
* Loads + decrypts that company's Buildium credentials via
  :mod:`helixis.buildium.credentials`.
* Returns a small, agent-friendly summary — never a raw JSON dump.

If a company hasn't connected Buildium yet, every tool returns the
literal string starting with ``"Buildium is not connected"`` so the
orchestrator's instructions can pattern-match on it and tell the user
to visit the Connectors page.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from agents import function_tool

from helixis.agents.idempotency import SUCCESS_OUTCOMES, derive_key, pending_reply, run_guarded
from helixis.agents.tenancy import FORMER, INCOMING, is_current_tenancy, is_upcoming_tenancy
from helixis.buildium.api import BuildiumApi
from helixis.buildium.client import BuildiumClient
from helixis.buildium.credentials import load_buildium_settings_for_company
from helixis.buildium.errors import BuildiumApiError
from helixis.buildium.linkage import work_order_property_ids
from helixis.buildium.maintenance_status import OPEN_STATUSES, is_open
from helixis.buildium.money import bill_amount, bill_is_paid
from helixis.buildium.schemas import Unit
from helixis.memory.context import current_memory_context
from helixis.observability.reason_codes import record_reason_code

logger = logging.getLogger(__name__)

# Read-back verification bounds (cut 1: Buildium create_to_do_task). Reads only;
# small fixed cap + short backoff to absorb create→read lag without hanging.
# On exhaustion the verifier returns False / raises → needs-review (never a
# second create).
_VERIFY_MAX_ATTEMPTS = 3
_VERIFY_BACKOFF_SECONDS = 0.2


_NOT_CONNECTED = (
    "Buildium is not connected for this company. Ask the user to connect "
    "Buildium on the Connectors page (Settings → Connectors → Buildium)."
)


#: Appended to EVERY branch where the read FAILED, so the model cannot turn a
#: failure into an absence.
#:
#: ⚠ Only the auth branch carried this (401 and 403 shared one, until they were
#: split — they are different answers, see the 403 branch). A 500, a rate limit
#: or a rejected
#: request left the model with "I couldn't do that (logged for review)" and no
#: instruction — and the reply a model writes from that is "you have no open
#: work orders", which is this product's worst failure mode (gotcha 26) and the
#: exact thing golden 099 exists to catch. It was caught for ONE status class
#: and not the others.
#:
#: ⚠ Deliberately NOT on the 404 branch. There, absence IS the correct reading:
#: the record genuinely is not in Buildium, and forbidding the model from
#: saying so would make it hedge about a fact it actually has.
_NOT_AN_ABSENCE = (
    " This is a FAILED lookup, not an empty result — do NOT tell the user they "
    "have no records, and do not present this as a finished answer."
)


def _sanitize_buildium_error(exc: BuildiumApiError, action: str) -> str:
    """Convert a raw ``BuildiumApiError`` into a short, human-readable
    string safe to surface in chat (PR #76 Bug 3).

    Pre-PR-#76 every tool returned ``f"Buildium API error … : {exc}"``
    which dumped Buildium's raw English — field names, "EntityId when
    EntityType is provided", API-key paths — straight into the
    property manager's chat. The HARD PRESENTATION RULES paragraph
    only redacts IDs from successful results; it doesn't apply to
    error returns. This helper closes that hole:

    - The full raw error is logged server-side at WARNING (preserving
      status + path + Buildium's verbatim message for debugging).
    - The agent receives a one-sentence phrase appropriate to the
      HTTP status class — never the raw exception text, never an
      internal id, never a settings-page path.

    ``action`` is a short verb phrase like ``"list overdue balances"``
    or ``"create the task"``. It's spliced into the user-facing
    message so the agent knows what failed without parsing the raw
    exception.
    """
    # ``BuildiumApiError`` exposes ``status`` (an int) — see
    # ``helixis.buildium.errors``. The brief speculated ``status_code``;
    # we look up the right attribute name once here.
    status = getattr(exc, "status", None)
    logger.warning(
        "Buildium API error during %s (status=%s): %s",
        action,
        status,
        exc,
    )
    if isinstance(status, int):
        if 500 <= status < 600:
            return (
                f"Buildium is having trouble responding right now. "
                f"I couldn't {action} — give it a minute and try again." + _NOT_AN_ABSENCE
            )
        if status == 401:
            record_reason_code("buildium_auth_error", detail=f"status={status} during {action}")
            # ⚠ BYTE-IDENTICAL ON PURPOSE. Golden 099 hard-codes this exact
            # string in its `tool_mocks` twice, and nothing compares the mock to
            # this function — so moving one word leaves the case exercising a
            # string the tool no longer produces, silently (gotcha 41's drifted
            # mock). `test_the_401_text_is_what_golden_099_mocks` now reads the
            # JSON and compares, so the two cannot drift again.
            return (
                f"The Buildium connection failed authentication — the saved "
                f"credentials were rejected, so I couldn't {action}, and every "
                f"other live Buildium call will fail the same way until the "
                f"connection is fixed. Tell the user their Buildium connection "
                f"needs to be reconnected on the Connectors page "
                f"(Settings → Connectors → Buildium). Do NOT retry other live "
                f"Buildium tools, and do NOT describe this as having no records."
            )
        if status == 403:
            # ⚠ 401 AND 403 ARE DIFFERENT ANSWERS AND THIS BRANCH USED TO GIVE
            # THE 401 ONE. A 403 means Buildium read the credentials, accepted
            # them, and refused THIS request — so "the saved credentials were
            # rejected, reconnect on the Connectors page" sent that user to redo
            # a connection that works, and the blast-radius claim ("every other
            # live Buildium call will fail the same way") is false for a
            # per-endpoint permission denial.
            #
            # This repo already knew that in three other places and the
            # agent-facing message was the one that disagreed: `buildium/
            # sync.py`'s staff sync names the `Administration > Users — View`
            # toggle, `buildium/endpoints/administration.py`'s docstring says
            # the 403 is per-endpoint, and `agent/handlers/send_tenant_message`
            # treats it as scope. Gotcha 67's shape — a rule that reached one
            # caller and not the other.
            #
            # ⚠ THE PERMISSION PATH IS NAMED FIRST AND RECONNECTING SURVIVES AS
            # THE FALLBACK. Nothing in this repo has probed what Buildium
            # returns for a DISABLED key or a lapsed Open API entitlement, and
            # those plausibly do need a new credential — so this must not become
            # "check permissions" alone (gotcha 79: do not encode a guess about
            # a wire shape as fact).
            record_reason_code(
                "buildium_permission_denied", detail=f"status={status} during {action}"
            )
            return (
                f"Buildium accepted the credentials and refused this request, so "
                f"I couldn't {action}. That is a permission or plan limit on "
                f"Buildium's side rather than a broken connection — the usual "
                f"cause is the API key missing the permission this endpoint "
                f"needs (Buildium → Settings → Developer Tools → API Keys), and "
                f"a lapsed or suspended Buildium subscription looks the same. "
                f"Other Buildium tools may well still work, so keep using them; "
                f"only suggest reconnecting on the Connectors page if they fail "
                f"too. Do NOT tell the user they have no records."
            )
        if status == 404:
            return f"I couldn't find that in Buildium to {action}."
        if status == 429:
            # ⚠ The client ALREADY retries 429 with exponential backoff and
            # honours Retry-After (`_RETRY_STATUSES` in buildium/client.py), so
            # a 429 arriving here means those retries were EXHAUSTED. Saying
            # "try again" without that context invites an immediate retry into
            # the same limit; the honest instruction is to narrow the request.
            return (
                f"Buildium is rate-limiting us — I retried and still couldn't "
                f"{action}. Ask for a narrower slice (one property, a shorter "
                f"date range) or try again in a minute." + _NOT_AN_ABSENCE
            )
        if status == 400:
            return (
                f"Buildium rejected that request. I couldn't {action} — "
                f"the details have been logged so we can fix it." + _NOT_AN_ABSENCE
            )
    return f"I couldn't {action} in Buildium. (logged for review)" + _NOT_AN_ABSENCE


def _current_company_id() -> str:
    ctx = current_memory_context()
    return ctx.company_id if ctx else ""


async def _caller_role() -> str | None:
    """The requesting user's normalized team role (admin/manager/member) in the
    current company, or ``None`` when it can't be determined — no request
    context, no DB pool (tests/scripts), or no ``company_users`` row.

    A tool gating a sensitive write treats ``None`` as DENY (fail closed): for an
    irreversible financial action, "can't verify the role" must not mean "allow".
    """
    ctx = current_memory_context()
    if ctx is None or not ctx.company_id or not ctx.user_id:
        return None
    try:
        from helixis.db.engine import get_pool

        pool = get_pool()
    except Exception:  # noqa: BLE001 — pool not initialized (tests) → can't verify
        return None
    if pool is None:
        return None
    try:
        row = await pool.fetchrow(
            "SELECT role FROM public.company_users "
            "WHERE user_id = $1::uuid AND company_id = $2::uuid",
            ctx.user_id,
            ctx.company_id,
        )
    except Exception:  # noqa: BLE001 — a lookup failure must fail closed
        return None
    from helixis.api.roles import normalize_role

    return normalize_role(row["role"]) if row else None


async def _require_manager_role(action: str, *, alt: str) -> str | None:
    """Manager/Admin fail-closed floor for an irreversible write.

    Returns a user-facing refusal string when the caller is NOT Manager+ (or the
    role can't be verified — fail closed), else ``None`` to proceed. ``action``
    is the gerund phrase ("closing a work order"); ``alt`` the do-it-yourself
    hint ("close it in Buildium directly"). Mirrors the inline floor on
    ``record_payment`` (the immutable financial write) so the irreversible
    writes share one guard.
    """
    from helixis.api.roles import MANAGER, role_at_least

    role = await _caller_role()
    if role is None or not role_at_least(role, MANAGER):
        return (
            f"{action.capitalize()} needs a Manager or Admin on this workspace, so I didn't do "
            f"anything. Ask a teammate with that role, or {alt}."
        )
    return None


def _format_address(address: Any) -> str:
    """Render an Address-like object as a single line, gracefully."""
    if address is None:
        return ""
    parts = [
        getattr(address, "address_line1", None),
        getattr(address, "city", None),
        getattr(address, "state", None),
    ]
    return ", ".join(p for p in parts if p)


def _tenant_full_name(t: Any) -> str:
    first = getattr(t, "first_name", None) or ""
    last = getattr(t, "last_name", None) or ""
    name = f"{first} {last}".strip()
    # PR #75 Bug B — never fall back to a printable ID. If we can't
    # render a real name, we emit a neutral placeholder; the agent must
    # not say "Tenant 12345" to the user.
    return name or "Unnamed tenant"


def _first_phone(t: Any) -> str | None:
    phones = getattr(t, "phone_numbers", None) or []
    for p in phones:
        num = getattr(p, "number", None) if not isinstance(p, dict) else p.get("Number")
        if num:
            return str(num)
    return None


def _format_money(amount: Any) -> str:
    if amount is None:
        return ""
    try:
        return f"${float(amount):,.2f}"
    except (TypeError, ValueError):
        return str(amount)


def _format_date(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value.date().isoformat())
    except AttributeError:
        return str(value)


def _truncate_footer(total: int, limit: int, *, noun: str = "record", hint: str = "") -> str | None:
    """The footer for a truncated list — WITH the anti-absence warning.

    ⚠ This used to return a bare "… and N more not shown." for all five tools
    that share it (tenants, leases, owners, vendors, work orders), while
    ``buildium_list_tasks`` carried the full warning inline. So the one list
    that had been burned by the bug was protected and the other five were not.

    Battery v2 Q9 is the live instance: the agent read a truncated list, failed
    to find a task in it, and told the user it did not exist ANYWHERE in the
    portfolio — while "URGENT: water heater leaking into the closet" sat in the
    hidden tail. A truncated list is NEVER evidence of absence (gotcha 26), and
    the model needs to be told so in the result, because the result is all it
    sees.

    ⚠ ``hint`` is optional and deliberately not defaulted to "re-call with
    search=". Four of the five callers take NO arguments at all, so promising a
    narrowing parameter that does not exist would send the model to invent one.
    """
    if total <= limit:
        return None
    tail = f" {hint}" if hint else ""
    return (
        f"… and {total - limit} more not shown. This list is TRUNCATED — do NOT "
        f"conclude a {noun} is absent because it is not here; you have seen "
        f"{limit} of {total}.{tail}"
    )


def _balance_of(row: Any) -> float:
    """Extract the outstanding-balance number from a Buildium
    ``/leases/outstandingbalances`` row (PR #78).

    Reads ``total_balance`` (the spec field — see
    ``OutstandingBalance``) and falls back to the pre-PR-#78
    ``outstanding_balance`` legacy alias for any test fixture or
    cached payload still using that name. Non-numeric values
    (None, MagicMock auto-attributes, strings that don't parse)
    are silently treated as zero so a malformed row never poisons
    the sort.

    ``bool`` is rejected even though Python treats it as ``int`` —
    a boolean is never a balance amount and accepting it would mask
    real bugs in the schema upstream. MagicMock supports
    ``__float__`` (it answers ``1.0``), so we type-check first
    instead of relying on ``float()`` to fail.
    """
    for attr in ("total_balance", "outstanding_balance"):
        value = getattr(row, attr, None)
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return 0.0


async def _resolve_unit(api: BuildiumApi, query: str) -> tuple[Unit | None, list[Unit], str | None]:
    """Resolve a unit by id or by fuzzy match on unit_number / address.

    Returns ``(unit, candidates, error)``:
      - if ``unit`` is set, we found exactly one match
      - if ``candidates`` has 2+, the caller should ask the user to disambiguate
      - if ``error`` is set, no match at all
    """
    query = (query or "").strip()
    if not query:
        return None, [], "Please specify which unit (number or address)."

    if query.isdigit():
        try:
            unit = await api.rentals.get_unit(int(query))
            return unit, [unit], None
        except BuildiumApiError:
            pass

    units = await api.rentals.list_units()
    needle = query.lower()
    matches: list[Unit] = []
    for u in units:
        unit_number = (getattr(u, "unit_number", None) or "").lower()
        addr = _format_address(getattr(u, "address", None)).lower()
        if needle == unit_number or needle in addr:
            matches.append(u)

    if not matches:
        return None, [], f"Couldn't find a unit matching '{query}'."
    if len(matches) == 1:
        return matches[0], matches, None
    return None, matches, None


async def _open_api() -> tuple[BuildiumClient | None, BuildiumApi | None, str | None]:
    """Resolve the current company's BuildiumApi or return an error string."""
    company_id = _current_company_id()
    if not company_id:
        return None, None, _NOT_CONNECTED
    settings = await load_buildium_settings_for_company(company_id)
    if settings is None:
        return None, None, _NOT_CONNECTED
    client = BuildiumClient(settings)
    return client, BuildiumApi(client), None


async def _count_properties_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            properties = await api.rentals.list_properties()
        count = len(properties)
        return f"You have {count} {'property' if count == 1 else 'properties'} in Buildium."
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "count properties")


@function_tool
async def buildium_count_properties() -> str:
    """Count the total number of rental properties in the user's Buildium account.

    Use this to answer questions like:
      - "How many properties do I have?"
      - "How big is my portfolio?"
      - "How many rentals are we managing?"

    Returns a human-readable count. Prefer this over the browser agent
    for any portfolio-size question.
    """
    return await _count_properties_impl()


async def _list_properties_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            properties = await api.rentals.list_properties()
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list properties")

    if not properties:
        return "No properties found in Buildium for this account."

    limit = 25
    shown = properties[:limit]
    lines = [
        f"You have {len(properties)} "
        f"{'property' if len(properties) == 1 else 'properties'} in Buildium.",
        "",
    ]
    for p in shown:
        name = getattr(p, "name", None) or "Unnamed property"
        address = _format_address(getattr(p, "address", None))
        # Surface the id so the agent can pass it as ``property_id`` on a write;
        # it refers to the property by name/address to the user, never the id.
        pid = getattr(p, "id", None)
        suffix = f" (id: {pid})" if pid is not None else ""
        if address:
            lines.append(f"- {name} — {address}{suffix}")
        else:
            lines.append(f"- {name}{suffix}")
    if len(properties) > limit:
        lines.append("")
        lines.append(f"… and {len(properties) - limit} more not shown.")
    return "\n".join(lines)


@function_tool
async def buildium_list_properties() -> str:
    """List the user's rental properties from Buildium.

    Use this to answer questions like:
      - "What properties do I have?"
      - "List my properties / rentals."
      - "Show me everything in my portfolio."

    Returns up to the first 25 properties as a formatted bulleted list
    (id, name, and address). If there are more than 25, the response
    notes the total count so the user knows how many were truncated.
    """
    return await _list_properties_impl()


async def _count_units_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            units = await api.rentals.list_units()
        return f"You have {len(units)} {'unit' if len(units) == 1 else 'units'} in Buildium."
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "count units")


@function_tool
async def buildium_count_units() -> str:
    """Count the total number of rental units across all properties.

    Use this for questions like:
      - "How many units do I manage?"
      - "Total number of doors?"
    """
    return await _count_units_impl()


async def _count_tenants_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            tenants = await api.rentals.list_tenants()
        return (
            f"You have {len(tenants)} {'tenant' if len(tenants) == 1 else 'tenants'} in Buildium."
        )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "count tenants")


@function_tool
async def buildium_count_tenants() -> str:
    """Count the total number of tenants in Buildium.

    Use this for questions like:
      - "How many tenants do we have?"
      - "Total tenant count."
    """
    return await _count_tenants_impl()


# Buildium's API uses two distinct "open" states for a work order: "New"
# (just created, no one assigned yet) and "InProgress" (being worked on).
# The web UI's "open" filter is the union of both. The Status enum is
# case-sensitive per the OpenAPI spec, and the query parameter is the
# plural "statuses" (array), not "status".
# Source: https://developer.buildium.com  (GET /v1/workorders → statuses[])
def _only_for_unit(rows: list[Any], unit_id: Any) -> list[Any]:
    """Keep only the rows that actually belong to ``unit_id``.

    ⚠ ``?unitids`` is ignored the same way ``?propertyids`` is. Found by
    tracing battery v10-100 q4, whose specialist consults all died on turn
    exhaustion: ``_list_work_orders_for_unit_impl`` asked for one unit's work
    orders and got all 60 in the portfolio, then spent its remaining turns
    writing up the discrepancy instead of answering.

    And ``_find_tenant_for_unit_impl`` has the same shape with a nastier
    consequence: its multi-lease AMBIGUITY guard — added for the Whitfield
    misattribution — fires on the account-wide list, so a unit with exactly
    one tenant reports "50 active leases, I can't say which tenant". The guard
    is correct; it was being handed a fabricated ambiguity.
    """
    return _only_matching(rows, unit_id, field="unit_id")


def _only_for_property(rows: list[Any], property_id: Any) -> list[Any]:
    """Keep only the rows that actually belong to ``property_id``.

    ⚠ Buildium's ``?propertyids`` filter is SILENTLY IGNORED on some
    endpoints — it returns 200 with the whole account and no error (gotcha
    24, recorded for /tasks and /bills). Battery v10-100 q95 caught it on
    /leases too: a one-lease property was reported as having 50 active
    leases, which is the account total. A count that is 50x wrong reads
    exactly like a real number, and it was used to refuse a write.

    So every property-scoped list read re-filters here. When the server
    filter works this changes nothing; when it does not, it is the
    difference between a fact and a fabrication.

    Rows whose ``property_id`` is None are DROPPED. Keeping them would
    attribute an unidentifiable row to this property, which is the class of
    claim being removed — and ``Unit.property_id`` is already documented as
    occasionally absent on draft rows, so this is a live case, not a
    hypothetical.
    """
    return _only_matching(rows, property_id, field="property_id")


def _field_is_blind(rows: list[Any], field: str) -> bool:
    """True when NO row carries ``field``, so filtering on it cannot discriminate.

    ⚠ This is NOT the same as "the filter kept everything", and conflating the
    two is a live bug I wrote and a mock caught. A filter that keeps every row
    because every row genuinely matches is WORKING; one that keeps every row
    because the field is null everywhere is BLIND. Inferring blindness from an
    unchanged count fires the fallback on the healthy case too.

    Shared with ``_only_matching`` so its fail-open condition and any caller's
    "did that actually filter?" check can never drift into disagreeing.
    """
    return all(getattr(row, field, None) is None for row in rows)


def _only_matching(rows: list[Any], target_id: Any, *, field: str) -> list[Any]:
    """Shared scope filter. See the two wrappers above for why it exists.

    Rows whose ``field`` is None are DROPPED — an unattributable row counted
    toward this property/unit is the false claim being removed. But an
    unusable TARGET fails OPEN and returns the rows untouched: returning []
    there would manufacture a confident absence, which is worse than the
    over-count it replaces.
    """
    try:
        target = int(target_id)
    except (TypeError, ValueError):
        return rows

    # ⚠ IF NO ROW CARRIES THE FIELD, THE FIELD CANNOT DISCRIMINATE — return
    # everything rather than nothing.
    #
    # This is not defensive padding; it is the difference between two bugs of
    # very different severity. ``WorkOrder.unit_id`` is declared and Buildium
    # leaves it NULL: the mirror has it null on 60 of 60 rows. Filtering on it
    # turns "60 work orders, wrongly unfiltered" into "no work orders on
    # record" — an over-count is arguable and visible, a false absence is
    # confident and wrong, and gotcha 26 calls that this product's worst
    # failure mode. The first cut of this helper did exactly that and a unit
    # test caught it: "123 Main St, Unit 4B has no work orders on record."
    #
    # Same shape as the unusable-TARGET branch above. A guard that cannot see
    # must say so by declining to act, never by reporting emptiness.
    if _field_is_blind(rows, field):
        return rows

    kept = []
    for row in rows:
        value = getattr(row, field, None)
        if value is not None and int(value) == target:
            kept.append(row)
    return kept


#: ⚠ COMPUTED from the shared vocabulary, not written out. This was
#: ``("New", "InProgress")`` — omitting ``Deferred``, which the MIRROR counted
#: as open, so the same question got different numbers from
#: ``portfolio_overview`` than from ``buildium_open_maintenance_by_unit``.
#:
#: It stays an allowlist only because it is sent to Buildium as
#: ``?statuses=`` and you cannot hand a server a denylist. Every decision
#: about a row already in hand goes through ``is_open`` instead, which asks
#: "is this NOT closed" — so a status the filter did not know to request is
#: still counted correctly once the rows arrive. See
#: ``buildium/maintenance_status``.
_OPEN_WORK_ORDER_STATUSES = OPEN_STATUSES


def _work_order_location(w: Any) -> str:
    """``property/unit`` identity for one work-order row, as ids.

    Ids (not names) on purpose: they are what a downstream join keys on,
    and resolving names here would re-introduce the per-row fan-out this
    formatter exists to prevent.
    """
    property_id = getattr(w, "property_id", None)
    unit_id = getattr(w, "unit_id", None)
    parts: list[str] = []
    if isinstance(property_id, int):
        parts.append(f"property {property_id}")
    if isinstance(unit_id, int):
        parts.append(f"unit {unit_id}")
    return " / ".join(parts)


def _work_order_matches(work_order: Any, needle: str, *, location: str = "") -> bool:
    """Does this work order match the search phrase?

    ALL words must appear, in any order, across the work order's text AND its
    location — the same contract as ``_task_matches``, and for the same reason
    it was written that way: asked about "the water heater at Montgomery", the
    model folds the place into the phrase no matter what the docstring says, so
    the location belongs in the haystack rather than in an instruction the
    model is free to ignore. A verbatim-phrase match reports "no match" on a
    real record and states it searched everything — authoritative and wrong.
    """
    words = [w for w in re.split(r"\W+", needle.lower()) if w]
    if not words:
        return True
    haystack = " ".join(
        [str(getattr(work_order, field, "") or "") for field in ("title", "description")]
        + [location]
    ).lower()
    return all(word in haystack for word in words)


def _format_open_work_orders(
    work_orders: list[Any],
    today: date,
    *,
    scope: str = "",
    searched_total: int | None = None,
) -> str:
    """Render open work orders due-date-aware, most-overdue first.

    Pure (no I/O) so the overdue math + ordering are unit-testable. The
    /workorders endpoint only exposes ``DueDate`` (probe 2026-07-13), so
    "overdue" = DueDate < today and "open the longest" is answered as
    "furthest past its due date" — we cannot sort by opened-date here because
    the endpoint never returns a created date.

    ``scope`` names what was filtered on, and ``searched_total`` how many open
    work orders that filter ran over. ⚠ BOTH exist for the empty case, and it
    is the one that matters: an empty FILTERED result is the only honest "no"
    this tool can give, and only because the filter ran over every open work
    order rather than a page of them (gotcha 26 — a truncated list is never
    evidence of absence, and battery v2 q9 is what that costs).
    """
    if not work_orders:
        if scope and searched_total is not None:
            return f"No open work orders {scope} — searched all {searched_total} open in Buildium."
        return "No open work orders in Buildium right now."

    def _due(w: Any) -> date | None:
        # Only accept a real date/datetime — a stray value (or a test's
        # MagicMock) must not crash the sort; it just sorts as "no due date".
        for attr in ("effective_due_date", "due_date"):
            val = getattr(w, attr, None)
            if isinstance(val, datetime):
                return val.date()
            if isinstance(val, date):
                return val
        return None

    def _sort_key(w: Any) -> tuple[int, date]:
        # Overdue first (oldest due date first), then upcoming by due date,
        # then anything with no due date last (date.max as the tail sentinel).
        due = _due(w)
        if due is None:
            return (2, date.max)
        return (0 if due < today else 1, due)

    ordered = sorted(work_orders, key=_sort_key)
    overdue_count = 0
    for w in ordered:
        due = _due(w)
        if due is not None and due < today:
            overdue_count += 1

    # Portfolio-wide cap. Was 25 — too small to answer "which of these
    # tenants has open maintenance?" on a real book, which pushed the agent
    # into a per-unit fan-out instead (Langfuse trace f275d529, 2026-08-20:
    # 78 per-unit calls, 73% of a run that then died on the wall-clock
    # deadline). The cross-check needs the WHOLE open list, so this is the
    # count of open work orders a mid-size PM plausibly carries.
    limit = 200
    shown = ordered[:limit]
    header = f"{len(ordered)} open {'work order' if len(ordered) == 1 else 'work orders'}"
    if scope:
        header += f" {scope}"
    if overdue_count:
        header += f" — {overdue_count} overdue"
    lines = [f"{header}:", ""]
    for w in shown:
        title = getattr(w, "title", None) or "Untitled work order"
        priority = getattr(w, "priority", None)
        bits = [title]
        # WHERE the work order is — without this the list cannot be joined
        # against tenants/leases locally, which is exactly why the agent fell
        # back to calling the per-unit tool once per unit. property_id and
        # unit_id are on the schema; they were simply never printed.
        location = _work_order_location(w)
        if location:
            bits.append(location)
        if priority:
            bits.append(f"priority: {priority}")
        due = _due(w)
        if due is not None:
            days = (today - due).days
            if days > 0:
                bits.append(f"due {due.isoformat()} (overdue {days}d)")
            elif days == 0:
                bits.append(f"due {due.isoformat()} (due today)")
            else:
                bits.append(f"due {due.isoformat()} (in {-days}d)")
        # ``[ref:X]`` is the internal-only chain handle; orchestrator
        # instructions forbid echoing it to the user.
        bits.append(f"[ref:{w.id}]")
        lines.append("- " + " — ".join(bits))
    if len(ordered) > limit:
        lines.append("")
        # ⚠ The anti-absence warning, and the hint names a parameter that now
        # EXISTS. Gotcha 26's rule: a truncated list is never evidence of
        # absence, and the model only sees the result — so the result has to
        # say so. ``_truncate_footer``'s own note warns against promising a
        # narrowing parameter a tool does not have; this one has both.
        footer = _truncate_footer(
            len(ordered),
            limit,
            noun="open work order",
            hint="Re-call with search='<keyword>' or property_name='<name>' to search them all.",
        )
        if footer:
            lines.append(footer)
    return "\n".join(lines)


async def _list_open_work_orders_impl(search: str = "", property_name: str = "") -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    locations: dict[int, str] = {}
    property_id: int | None = None
    try:
        async with client:
            work_orders = list(await api.work_orders.list(statuses=list(_OPEN_WORK_ORDER_STATUSES)))
            if property_name.strip() or search.strip():
                # ONE bulk call, and only when filtering — the same shape
                # ``_list_tasks_impl`` uses. It resolves ``property_name`` AND
                # gives every row a location string for the search haystack;
                # without it a phrase carrying a place name can never match.
                for p in await api.rentals.list_properties():
                    pid = getattr(p, "id", None)
                    if pid is not None:
                        locations[int(pid)] = " ".join(
                            filter(
                                None,
                                [
                                    getattr(p, "name", None) or "",
                                    _format_address(getattr(p, "address", None)),
                                ],
                            )
                        )
            if property_name.strip():
                needle = property_name.strip().lower()
                matches = [pid for pid, label in locations.items() if needle in label.lower()]
                if not matches:
                    return f"I couldn't find a property matching '{property_name}'."
                property_id = matches[0]
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list open work orders")

    searched_total = len(work_orders)
    scope_bits: list[str] = []
    if property_id is not None:
        # Client-side, via the shared scope filter — production silently
        # IGNORES ``?propertyids`` on some endpoints (gotcha 24) and
        # ``_only_for_property`` FAILS OPEN when no row carries the field, so a
        # blind column over-reports visibly instead of manufacturing a
        # confident absence (gotcha 55 — ``WorkOrder.unit_id`` is exactly that,
        # null on 60 of 60, which is why this scopes on the property).
        work_orders = _only_for_property(work_orders, property_id)
        scope_bits.append(f"at {property_name.strip()}")
    if search.strip():
        work_orders = [
            w
            for w in work_orders
            if _work_order_matches(
                w,
                search.strip(),
                location=locations.get(getattr(w, "property_id", None) or -1, ""),
            )
        ]
        scope_bits.insert(0, f"matching '{search.strip()}'")

    return _format_open_work_orders(
        work_orders,
        datetime.now(UTC).date(),
        scope=" ".join(scope_bits),
        searched_total=searched_total if scope_bits else None,
    )


@function_tool
async def buildium_list_open_work_orders(search: str = "", property_name: str = "") -> str:
    """List EVERY open work order across the whole portfolio in one call.

    Answers "what's open in maintenance?" and "show me open tickets", and is
    ALSO the right tool for any question needing maintenance status across
    more than one unit — "who is behind on rent AND has open maintenance",
    "which expiring leases have unresolved tickets", triage, dispatch plans.
    Every row carries its ``property <id> / unit <id>`` location, so you can
    join this single result against tenants, leases, or balances yourself.
    Do NOT loop ``buildium_list_work_orders_for_unit`` over many units to
    build a portfolio picture — call this once and filter the rows; a live
    run that fanned out per-unit spent 73% of its time in that loop and
    never finished.

    Pass `search` to find one by keyword — it matches title and description
    across EVERY open work order, not just the page an unfiltered call shows
    ("is there anything open about the roof?"). Pass `property_name` to scope
    to one property; pass both to combine them.

    Only a filtered call that returns nothing is evidence that a work order
    does not exist. An unfiltered list is truncated, so never conclude one is
    absent because it isn't in it.

    Args:
        search: Optional keyword matched against title, description and the
            property label.
        property_name: Optional property name or address substring.
    """
    return await _list_open_work_orders_impl(search, property_name)


# ─────────────────────────────────────────────────────────────────────
# Tenant tools
# ─────────────────────────────────────────────────────────────────────


async def _list_tenants_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            tenants = await api.rentals.list_tenants()
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list tenants")

    if not tenants:
        return "No tenants found in Buildium for this account."

    limit = 25
    shown = tenants[:limit]
    lines = [
        f"You have {len(tenants)} {'tenant' if len(tenants) == 1 else 'tenants'} in Buildium.",
        "",
    ]
    for t in shown:
        bits = [_tenant_full_name(t)]
        if getattr(t, "email", None):
            bits.append(f"— {t.email}")
        phone = _first_phone(t)
        if phone:
            bits.append(f"— {phone}")
        lines.append("- " + " ".join(bits))
    footer = _truncate_footer(len(tenants), limit, noun="tenant")
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


@function_tool
async def buildium_list_tenants() -> str:
    """List tenants from Buildium (names, emails, phones).

    Use this to answer "who are my tenants?" or "list my tenants". Prefer
    `buildium_find_tenant_for_unit` or `buildium_get_tenant` for questions
    about a specific person or unit so you don't dump the full roster.
    """
    return await _list_tenants_impl()


def _format_tenant_profile(t: Any) -> str:
    # ⚠ THE TENSE GOES FIRST, above the contact details. Battery v13 q08 asked
    # for a former tenant's balance and got an answer that never said "former"
    # — because this renderer put the status in a bare ``Leases: Past
    # [ref:...]`` line at the BOTTOM, under the email, the alternate email,
    # every phone number and the home address. A model reading that writes "X
    # is a tenant at Y, contact ...", which is a false statement about a real
    # person made while handing over their contact details.
    #
    # That is gotcha 51 exactly, on the sibling surface: ``contact_lookup`` was
    # fixed for it and this was not, and q08 routed here rather than there. The
    # classification now lives in ONE place (``agents/tenancy.py``) so the two
    # renderers cannot disagree about what "Past" means — same remedy as
    # ``buildium/money.py::bill_amount``.
    leases = getattr(t, "leases", None) or []
    statuses = [
        (lease.get("Status") if isinstance(lease, dict) else getattr(lease, "status", None))
        for lease in leases
    ]
    # Current if ANY lease is active — a tenant who renewed into a new lease
    # legitimately carries a Past one alongside it, and reading the first row
    # would call them former.
    if not leases:
        tenure = "no lease on file"
    elif any(is_current_tenancy(s) for s in statuses):
        tenure = ""
    elif any(is_upcoming_tenancy(s) for s in statuses):
        # Moving in, not out: a Future lease (beside a Past one or alone) read
        # "FORMER TENANT" here.
        tenure = INCOMING
    else:
        tenure = FORMER
    header = _tenant_full_name(t)
    if tenure == FORMER:
        header = f"{header} — {FORMER} TENANT (no active lease)"
    elif tenure == INCOMING:
        header = f"{header} — {INCOMING} TENANT (lease not active yet)"
    elif tenure:
        header = f"{header} — {tenure}"
    lines = [header]
    if getattr(t, "email", None):
        lines.append(f"  Email: {t.email}")
    if getattr(t, "alternate_email", None):
        lines.append(f"  Alt email: {t.alternate_email}")
    phones = getattr(t, "phone_numbers", None) or []
    for p in phones:
        if isinstance(p, dict):
            num = p.get("Number")
            ptype = p.get("Type")
        else:
            num = getattr(p, "number", None)
            ptype = getattr(p, "type", None)
        if num:
            label = f" ({ptype})" if ptype else ""
            lines.append(f"  Phone{label}: {num}")
    address = _format_address(getattr(t, "address", None))
    if address:
        lines.append(f"  Address: {address}")
    leases = getattr(t, "leases", None) or []
    if leases:
        lease_summaries = []
        for lease in leases:
            if isinstance(lease, dict):
                lid = lease.get("Id")
                lstatus = lease.get("Status")
            else:
                lid = getattr(lease, "id", None)
                lstatus = getattr(lease, "status", None)
            # Status is the user-facing label; lid goes in [ref:X] only
            # so the agent can chain into buildium_get_lease if needed.
            parts: list[str] = []
            if lstatus:
                parts.append(str(lstatus))
            if lid is not None:
                parts.append(f"[ref:{lid}]")
            lease_summaries.append(" ".join(parts) or "—")
        lines.append(f"  Leases: {', '.join(lease_summaries)}")
    return "\n".join(lines)


async def _get_tenant_impl(tenant_id_or_query: str) -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    query = (tenant_id_or_query or "").strip()
    if not query:
        return "Please specify a tenant name or ID."

    try:
        async with client:
            if query.isdigit():
                try:
                    tenant = await api.rentals.get_tenant(int(query))
                    return _format_tenant_profile(tenant)
                except BuildiumApiError as exc:
                    return _sanitize_buildium_error(exc, "look up that tenant")

            tenants = await api.rentals.list_tenants()
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "look up that tenant")

    needle = query.lower().split()
    if not needle:
        return f"No tenant matches '{query}'. Try a different name or ID."

    scored: list[tuple[int, int, Any]] = []
    for t in tenants:
        full = _tenant_full_name(t).lower()
        tokens = full.split()
        overlap = sum(1 for n in needle if n in tokens)
        if overlap:
            scored.append((overlap, len(full), t))

    if not scored:
        return f"No tenant matches '{query}'. Try a different name or ID."

    scored.sort(key=lambda x: (-x[0], x[1]))
    best_overlap = scored[0][0]
    best = [s for s in scored if s[0] == best_overlap]

    if len(best) == 1:
        return _format_tenant_profile(best[0][2])

    return _format_tenant_ambiguity(query, [t for _, _, t in best[:10]])


def _format_tenant_ambiguity(query: str, candidates: list[Any]) -> str:
    """One SELF-CONTAINED line per candidate — name and address together.

    ⚠ **This shape is the fix for #10, and the old shape is why the bug
    happened.** During a live move-out probe the agent said "Buildium
    currently shows Marcus Whitfield as the active tenant in Unit 1 at
    1700 W Washington St, Phoenix". Marcus is in MONTGOMERY. Phoenix is
    Elena Vasquez — a third tenant who is not called Whitfield at all. The
    mirror was internally consistent across all 75 leases, so the data was
    right and the agent was wrong.

    It was wrong in a specific, predictable way. This list used to carry
    **name and email only**, so an agent that needed the address had to call
    ``buildium_list_tenants`` and match rows across two separate tool results
    by name. That cross-reference is the join it miswired: it took row 1's
    name and row 3's address and stated the pair with confidence.

    So the fix is not to instruct the model to join carefully — gotchas
    25/32/34/37 all record what happens to instructions aimed at a loop.
    It is to **delete the join**. Every fact needed to tell these people
    apart now travels on the same line as the name, so a candidate cannot be
    described by quoting one row and reading a field off another.

    The closing line is deliberately about the ANSWER rather than the lookup:
    refusing to write was never the failure here (the agent correctly
    refused). Justifying that refusal with a confident false claim was. A
    false premise that happens to produce a safe outcome is not safe
    behaviour — the same reasoning pointed the other way is a wrong write.
    """
    lines = [f"Multiple tenants match '{query}'. These are DIFFERENT people:"]
    for t in candidates:
        facts = [
            f
            for f in (
                _format_address(getattr(t, "address", None)),
                getattr(t, "email", None) or None,
            )
            if f
        ]
        detail = f" — {' — '.join(facts)}" if facts else ""
        lines.append(f"- {_tenant_full_name(t)}{detail}")
    lines.append(
        "Each line above is one person, complete. Do NOT combine a name from one "
        "line with an address from another, and do NOT look the address up "
        "separately — everything needed to tell them apart is already here. "
        "Until the user says which one they mean, name ALL of them and ask; "
        "stating one confidently would be a guess presented as a fact."
    )
    return "\n".join(lines)


@function_tool
async def buildium_get_tenant(tenant_id_or_query: str) -> str:
    """Look up ONE tenant by id or name.

    Use this for "show me Sarah Chen's contact info", "what's tenant
    #12345's email", etc. Pass either the numeric tenant id or a name to
    fuzzy-match against. If multiple tenants match the query, returns a
    disambiguation list — re-call with a more specific name or the id.
    """
    return await _get_tenant_impl(tenant_id_or_query)


def _format_unit_label(unit: Any) -> str:
    addr = _format_address(getattr(unit, "address", None))
    unit_number = getattr(unit, "unit_number", None)
    # PR #75 Bug B — Brandon's rule: places by property name + unit
    # number. ``#4B`` reads ambiguously alongside the banned ``#<id>``
    # style, so spell it out as ``Unit 4B``.
    if addr and unit_number:
        return f"{addr}, Unit {unit_number}"
    if unit_number:
        return f"Unit {unit_number}"
    return addr or "an unspecified unit"


async def _find_tenant_for_unit_impl(unit_query: str) -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    try:
        async with client:
            unit, candidates, lookup_err = await _resolve_unit(api, unit_query)
            if lookup_err:
                return lookup_err
            if unit is None:
                lines = [f"Multiple units match '{unit_query}'. Please be more specific:"]
                for u in candidates[:10]:
                    lines.append(f"- {_format_unit_label(u)}")
                return "\n".join(lines)

            leases = _only_for_unit(
                await api.leases.list(unitids=[unit.id], leasestatuses=["Active"]), unit.id
            )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "look up tenants for that unit")

    label = _format_unit_label(unit)
    if not leases:
        return f"{label} has no active lease."

    # ⚠ A unit with more than one ACTIVE lease is ambiguous, and taking
    # ``leases[0]`` silently picked one — the same shape as the bug this
    # function's unit lookup already avoids two lines up. Reporting one
    # tenant set as though it were the answer is how a confident false
    # tenant-to-place claim gets made (#10, the Whitfield misattribution),
    # and it lands on the reads a move-out is decided from.
    if len(leases) > 1:
        lines = [
            f"{label} has {len(leases)} active leases, so I can't say which tenant "
            "you mean. Please pick one:"
        ]
        for lz in leases[:10]:
            who = ", ".join(
                _tenant_full_name(t) for t in (getattr(lz, "current_tenants", None) or [])
            )
            start = _format_date(getattr(lz, "lease_from_date", None))
            since = f" since {start}" if start else ""
            lines.append(f"- {who or 'no current tenants on file'}{since} [ref:{lz.id}]")
        return "\n".join(lines)

    lease = leases[0]
    tenants = getattr(lease, "current_tenants", None) or []
    if not tenants:
        # ``[ref:X]`` is the internal chaining handle — orchestrator
        # instructions forbid echoing it in user-facing chat.
        return f"{label} has an active lease [ref:{lease.id}] but no current tenants on file."

    names = [_tenant_full_name(t) for t in tenants]
    start = _format_date(getattr(lease, "lease_from_date", None))
    suffix = f" since {start}" if start else ""
    if len(names) <= 2:
        joined = " and ".join(names)
    else:
        joined = ", ".join(names[:-1]) + f", and {names[-1]}"
    return f"{label} — leased to {joined}{suffix}."


@function_tool
async def buildium_find_tenant_for_unit(unit_query: str) -> str:
    """Find the tenant(s) currently living in a specific unit.

    Use this for "who lives in 4B?", "who's the tenant at 123 Main #2?",
    etc. Resolves the unit by number or address, looks up the active
    lease, and returns the current tenants in one logical operation
    (one Buildium call set, no per-tenant follow-ups needed).
    """
    return await _find_tenant_for_unit_impl(unit_query)


# ─────────────────────────────────────────────────────────────────────
# Lease tools
# ─────────────────────────────────────────────────────────────────────


def _lease_unit_label(lease: Any) -> str:
    unit_number = getattr(lease, "unit_number", None)
    if unit_number:
        return f"unit {unit_number}"
    # PR #75 Bug B — never print a unit id when the unit_number is
    # missing; the agent has the property + unit elsewhere.
    return "an unspecified unit"


async def _list_active_leases_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            leases = await api.leases.list(leasestatuses=["Active"])
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list active leases")

    if not leases:
        return "No active leases found in Buildium."

    limit = 25
    shown = leases[:limit]
    lines = [
        f"{len(leases)} active {'lease' if len(leases) == 1 else 'leases'}:",
        "",
    ]
    for lease in shown:
        tenants = getattr(lease, "current_tenants", None) or []
        names = ", ".join(_tenant_full_name(t) for t in tenants) or "no tenants on file"
        rent = _format_money(getattr(lease, "rent", None))
        rent_part = f" — {rent}/mo" if rent else ""
        through = _format_date(getattr(lease, "lease_to_date", None))
        through_part = f" — through {through}" if through else ""
        unit_label = _lease_unit_label(lease)
        lines.append(f"- {names} at {unit_label}{rent_part}{through_part} [ref:{lease.id}]")
    footer = _truncate_footer(len(leases), limit, noun="lease")
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


@function_tool
async def buildium_list_active_leases() -> str:
    """List all active leases with tenants, unit, rent, and end date.

    Use this for "show me active leases", "current rent roll", etc.

    Returns fields per lease: lease_id, current_tenants (list of
    {first_name, last_name, email}), unit (property + unit number),
    Rent (monthly $), lease_to_date.

    CRITICAL: every row already includes Rent (monthly). Do NOT call
    ``buildium_get_lease`` per row to find the rent — it's already
    here. For "rank by rent" / "rent roll" / "total monthly rent"
    questions, this single call has everything you need. The dedicated
    ``buildium_get_rent_roll`` tool joins this with outstanding
    balances in one shot for the full rent-roll-with-delinquency view.
    """
    return await _list_active_leases_impl()


async def _get_lease_impl(lease_id: int) -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            lease = await api.leases.get(int(lease_id))
            property_name: str | None = None
            pid = getattr(lease, "property_id", None)
            if pid is not None:
                try:
                    prop = await api.rentals.get_property(int(pid))
                    property_name = (
                        getattr(prop, "name", None)
                        or _format_address(getattr(prop, "address", None))
                        or None
                    )
                except BuildiumApiError:
                    property_name = None
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "fetch that lease")

    unit_label = _lease_unit_label(lease)
    header = f"Lease at {unit_label}"
    if property_name:
        header = f"Lease at {property_name}, {unit_label}"
    lines = [f"{header} [ref:{lease.id}]"]
    start = _format_date(getattr(lease, "lease_from_date", None))
    end = _format_date(getattr(lease, "lease_to_date", None))
    if start or end:
        lines.append(f"  Term: {start or '?'} → {end or '?'}")
    term_type = getattr(lease, "term_type", None)
    if term_type:
        lines.append(f"  Term type: {term_type}")
    rent = _format_money(getattr(lease, "rent", None))
    if rent:
        lines.append(f"  Rent: {rent}/mo")
    status = getattr(lease, "status", None)
    if status:
        lines.append(f"  Status: {status}")
    if getattr(lease, "is_eviction_pending", False):
        lines.append("  ⚠ Eviction pending")
    tenants = getattr(lease, "current_tenants", None) or []
    if tenants:
        lines.append("  Current tenants:")
        for t in tenants:
            email = getattr(t, "email", None)
            email_part = f" — {email}" if email else ""
            # ⚠ The tenant ID is load-bearing, not decoration.
            # ``buildium_record_move_out`` requires ``tenant_id``, and this is
            # the tool that answers "who is on this lease". Rendering names
            # only meant the agent got something the write could not accept,
            # then burned its whole step budget hunting the id — a
            # reproducible dead end on a live write path (role-matrix
            # `ungated`, 2026-08-23). A read must emit the identifier its
            # write consumes.
            lines.append(f"    - {_tenant_full_name(t)} [ref:{getattr(t, 'id', '?')}]{email_part}")
    cosigners = getattr(lease, "cosigners", None) or []
    if cosigners:
        lines.append("  Cosigners:")
        for c in cosigners:
            lines.append(f"    - {_tenant_full_name(c)}")
    return "\n".join(lines)


@function_tool
async def buildium_get_lease(lease_id: int) -> str:
    """Get full detail on one lease — tenants, rent, term, eviction flag.

    Use this for "what's lease #5678 — who's on it and what's the rent?"
    """
    return await _get_lease_impl(lease_id)


# ─────────────────────────────────────────────────────────────────────
# Owner tools
# ─────────────────────────────────────────────────────────────────────


async def _list_owners_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            owners = await api.rentals.list_owners()
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list rental owners")

    if not owners:
        return "No rental owners found in Buildium."

    limit = 25
    shown = owners[:limit]
    lines = [
        f"{len(owners)} {'rental owner' if len(owners) == 1 else 'rental owners'}:",
        "",
    ]
    for o in shown:
        company = getattr(o, "company_name", None)
        first = getattr(o, "first_name", None) or ""
        last = getattr(o, "last_name", None) or ""
        person = f"{first} {last}".strip()
        name = company or person or "Unnamed owner"
        bits = [name]
        email = getattr(o, "email", None)
        if email:
            bits.append(f"— {email}")
        prop_ids = getattr(o, "property_ids", None)
        if prop_ids is not None:
            n = len(prop_ids)
            bits.append(f"— {n} {'property' if n == 1 else 'properties'}")
        lines.append("- " + " ".join(bits))
    footer = _truncate_footer(len(owners), limit, noun="owner")
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


@function_tool
async def buildium_list_owners() -> str:
    """List rental owners with their email and property count.

    Use this for "who owns these properties?" / "list my rental owners".
    """
    return await _list_owners_impl()


# ─────────────────────────────────────────────────────────────────────
# Vendor tools
# ─────────────────────────────────────────────────────────────────────


def _vendor_label(v: Any) -> str:
    """Human label for a vendor (PR #75 Bug B — never falls back to an id).

    PR #77 extends the fallback chain so individual vendors (where
    ``is_company`` is false) read with a real name instead of
    "Unnamed vendor" just because ``company_name`` is empty.
    """
    company = getattr(v, "company_name", None)
    if company:
        return str(company)
    name = getattr(v, "name", None)
    if name:
        return str(name)
    first = getattr(v, "first_name", None) or ""
    last = getattr(v, "last_name", None) or ""
    person = f"{first} {last}".strip()
    return person or "Unnamed vendor"


def _vendor_category_name(v: Any) -> str | None:
    """Pull the ``Category.Name`` off a vendor, surviving missing nodes."""
    cat = getattr(v, "category", None)
    if cat is None:
        return None
    name = getattr(cat, "name", None)
    return str(name) if name else None


def _vendor_best_phone(v: Any) -> str | None:
    """Pick the most reach-useful phone from a vendor's
    ``PhoneNumbers`` list. Buildium ships a list of ``{Number, Type}``
    entries (same shape as tenants). Preference order matches PR #77's
    original intent — Work/Office > Mobile/Cell > Home; Fax is excluded
    because nobody picks up a fax. ``Type`` is matched case-insensitively
    against a small set of synonyms because Buildium has shipped both
    ``"Work"``/``"Office"`` and ``"Mobile"``/``"Cell"`` over time."""
    phones = getattr(v, "phone_numbers", None) or []
    by_type: dict[str, str] = {}
    for entry in phones:
        num = getattr(entry, "number", None)
        kind = getattr(entry, "type", None)
        if not num or not kind:
            continue
        by_type.setdefault(str(kind).strip().lower(), str(num))
    for synonyms in (("work", "office"), ("mobile", "cell"), ("home",)):
        for key in synonyms:
            if key in by_type:
                return by_type[key]
    return None


async def _list_vendors_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            vendors = await api.vendors.list()
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list vendors")

    if not vendors:
        return "No vendors found in Buildium."

    active = [v for v in vendors if getattr(v, "is_active", True) is not False]

    limit = 25
    shown = active[:limit]
    lines = [
        f"{len(active)} active {'vendor' if len(active) == 1 else 'vendors'}:",
        "",
    ]
    for v in shown:
        # PR #77 — surface category alongside each name now that the
        # vendor schema actually parses ``Category``. Falls back to
        # "uncategorized" so the bullet list stays uniform.
        cat = _vendor_category_name(v) or "uncategorized"
        # Surface the id so the agent can pass it as ``vendor_id`` on a work
        # order; refer to the vendor by name to the user, never the id.
        lines.append(f"- {_vendor_label(v)} ({cat}, id: {getattr(v, 'id', '?')})")
    footer = _truncate_footer(len(active), limit, noun="vendor")
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


@function_tool
async def buildium_list_vendors() -> str:
    """List active vendors (plumbers, contractors, etc.) from Buildium.

    Use this for "who do we use for plumbing?" / "list my vendors".
    """
    return await _list_vendors_impl()


async def _get_vendor_impl(vendor_id_or_query: str) -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    query = (vendor_id_or_query or "").strip()
    if not query:
        return "Please specify a vendor name or ID."

    try:
        async with client:
            if query.isdigit():
                try:
                    vendor = await api.vendors.get(int(query))
                    return _format_vendor_profile(vendor)
                except BuildiumApiError as exc:
                    return _sanitize_buildium_error(exc, "look up that vendor")
            vendors = await api.vendors.list()
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "look up that vendor")

    needle = query.lower()
    matches = [v for v in vendors if needle in _vendor_label(v).lower()]
    if not matches:
        return f"No vendor matches '{query}'."
    if len(matches) == 1:
        return _format_vendor_profile(matches[0])

    lines = [f"Multiple vendors match '{query}'. Please be more specific:"]
    for v in matches[:10]:
        lines.append(f"- {_vendor_label(v)}")
    return "\n".join(lines)


def _format_vendor_profile(v: Any) -> str:
    lines = [_vendor_label(v)]
    is_active = getattr(v, "is_active", None)
    if is_active is not None:
        lines.append(f"  Active: {'yes' if is_active else 'no'}")
    return "\n".join(lines)


@function_tool
async def buildium_get_vendor(vendor_id_or_query: str) -> str:
    """Look up ONE vendor by id or name.

    Use this for "what's vendor #42's info?" or "find the plumber Acme".
    """
    return await _get_vendor_impl(vendor_id_or_query)


# ─────────────────────────────────────────────────────────────────────
# PR #77 — Vendor categories + targeted search
#
# Pre-PR-#77 the vendor schema was stripped to (id, name, company,
# is_active) so "find me a plumber" forced the agent to fall back on
# the user ("I'll list all vendors and you tell me which are
# plumbers"). The expanded schema + these two tools fix that.
# ─────────────────────────────────────────────────────────────────────


async def _list_vendor_categories_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            cats = await api.vendors.list_categories()
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list vendor categories")

    cat_list: list[Any] = list(cats or [])
    if not cat_list:
        return "No vendor categories are defined in Buildium for this account."

    seen: set[str] = set()
    for c in cat_list:
        name = getattr(c, "name", None)
        if name:
            seen.add(str(name))
    if not seen:
        return "Buildium returned vendor categories but none have names."
    return "Vendor categories: " + ", ".join(sorted(seen)) + "."


@function_tool
async def buildium_list_vendor_categories() -> str:
    """List vendor category names (e.g. Plumbing, HVAC, Electrical)
    defined in Buildium.

    Use this BEFORE ``buildium_search_vendors`` only when you're
    unsure of the exact category name in this account. The category
    list is small (typically <30 names) and stable; one call per
    session is enough.
    """
    return await _list_vendor_categories_impl()


async def _search_vendors_impl(
    category: str | None = None,
    name_contains: str | None = None,
    city: str | None = None,
    state: str | None = None,
    active_only: bool = True,
) -> str:
    """Search vendors by category, name, or location.

    ``category`` matches case-insensitively against ``Category.Name``
    (substring — "plumb" matches "Plumbing"). ``name_contains``
    matches against the vendor's display label. ``city``/``state``
    require exact (case-insensitive) match.
    """
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            vendors = await api.vendors.list()
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "search vendors")

    def _matches(v: Any) -> bool:
        if active_only and getattr(v, "is_active", True) is False:
            return False
        if category:
            cat_name = (_vendor_category_name(v) or "").lower()
            if category.lower() not in cat_name:
                return False
        if name_contains and name_contains.lower() not in _vendor_label(v).lower():
            return False
        if city or state:
            addr = getattr(v, "address", None)
            if addr is None:
                return False
            if city and (getattr(addr, "city", "") or "").lower() != city.lower():
                return False
            if state and (getattr(addr, "state", "") or "").lower() != state.lower():
                return False
        return True

    hits = [v for v in vendors if _matches(v)]
    if not hits:
        bits: list[str] = []
        if category:
            bits.append(f"category '{category}'")
        if name_contains:
            bits.append(f"name containing '{name_contains}'")
        if city:
            bits.append(f"city '{city}'")
        if state:
            bits.append(f"state '{state}'")
        suffix = f" matching {' and '.join(bits)}." if bits else "."
        return "No vendors found" + suffix

    # Cap at 15 to keep chat readable; the agent can refine with
    # extra filters if the user wants to drill further.
    limit = 15
    shown = hits[:limit]
    header = f"Found {len(hits)} vendor{'s' if len(hits) != 1 else ''}"
    if len(hits) > limit:
        header += f" (showing first {limit})"
    header += ":"
    lines: list[str] = [header]
    for v in shown:
        cat = _vendor_category_name(v) or "uncategorized"
        addr = getattr(v, "address", None)
        loc_bits: list[str] = []
        if addr is not None:
            if getattr(addr, "city", None):
                loc_bits.append(str(addr.city))
            if getattr(addr, "state", None):
                loc_bits.append(str(addr.state))
        loc = f", {', '.join(loc_bits)}" if loc_bits else ""
        phone = _vendor_best_phone(v)
        phone_part = f" · {phone}" if phone else ""
        # id surfaced for ``vendor_id`` on a work order; name-only to the user.
        lines.append(f"- {_vendor_label(v)} ({cat}{loc}, id: {getattr(v, 'id', '?')}){phone_part}")
    return "\n".join(lines)


@function_tool
async def buildium_search_vendors(
    category: str | None = None,
    name_contains: str | None = None,
    city: str | None = None,
    state: str | None = None,
    active_only: bool = True,
) -> str:
    """Search Buildium vendors by category, name, or location.

    Use this for "find me a plumber", "what HVAC vendors do we use",
    "vendors in Cleveland", "plumbers we can send to 150 Main Ave",
    etc. Returns vendor name + category + city/state + best phone.
    Internal vendor ids are never returned (HARD PRESENTATION RULES).

    Tip: ``category`` is matched as a substring so "plumb" finds
    "Plumbing". Call ``buildium_list_vendor_categories`` first ONLY
    if you're unsure of the exact category names in this account —
    don't pre-list categories on every search.
    """
    return await _search_vendors_impl(
        category=category,
        name_contains=name_contains,
        city=city,
        state=state,
        active_only=active_only,
    )


# ─────────────────────────────────────────────────────────────────────
# PR #77 — AP bills (money WE owe vendors)
#
# Distinct from the delinquency tool, which is money TENANTS owe US.
# Brandon asked "how many unpaid bills at gardens row" and the agent
# fired the delinquency tool — wrong category entirely. This tool
# closes that gap and can filter by property name (resolved via the
# rentals list) and days-overdue.
# ─────────────────────────────────────────────────────────────────────


# ``bill_amount`` is re-exported from ``buildium.money`` so the live tool, the
# mirror ingest and the webhook handler all read a bill's money by the same
# rule. Fixing it in only one of the three is what left the mirror reporting
# $0 after PR #852 (battery v2).


def _bill_property_ids(bill: Any) -> set[int]:
    """Every property a bill's lines are charged to.

    Reads :attr:`BillLine.resolved_property_id`, which tries the flat
    ``PropertyId`` first (so a future Buildium fix wins automatically) and
    falls back to ``AccountingEntity.Id`` on ``Rental`` lines — the key that
    is actually populated today. See gotcha 53.
    """
    return {
        pid
        for line in (getattr(bill, "lines", None) or [])
        if (pid := getattr(line, "resolved_property_id", None)) is not None
    }


def _bill_ownership_is_legible(bill: Any) -> bool:
    """Does this bill say ANYTHING about who its money belongs to?

    ⚠ Deliberately not ``bool(_bill_property_ids(bill))``, and a test caught
    the difference. A ``Company``-typed line resolves to no property, but that
    is a POSITIVE statement — the money is overhead and belongs to none — so a
    bill made entirely of overhead lines legitimately has no bills "at" a
    property and must report emptiness. A line carrying no entity at all is an
    ABSENCE of information, and scoping on it would manufacture a confident
    absence (gotcha 55).

    Collapsing the two would make an all-overhead account read as blind and
    return the whole ledger under a scoped question — the exact false claim
    the fail-open exists to prevent, arriving from the other side.
    """
    for line in getattr(bill, "lines", None) or []:
        if getattr(line, "resolved_property_id", None) is not None:
            return True
        entity = getattr(line, "accounting_entity", None)
        if entity is not None and getattr(entity, "id", None) is not None:
            return True  # typed and identified — e.g. Company overhead
    return False


def _bill_due_date(bill: Any) -> date | None:
    """A bill's due date as a plain ``date``, or None.

    Only a real date/datetime is accepted — a stray value (or a test's
    MagicMock) must not crash the sort; it sorts as "no due date", the same
    contract ``_format_open_work_orders._due`` holds.
    """
    val = getattr(bill, "due_date", None)
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    return None


def _format_unpaid_bills(
    bills: list[Any],
    today: date,
    *,
    vendor_names: dict[int, str] | None = None,
    header: str,
) -> str:
    """Render unpaid bills as ROWS, most overdue first, under ``header``.

    ⚠ THIS TOOL RETURNED ONLY THE SUMMARY LINE UNTIL 2026-09-13, and that is
    gotcha 50's shape exactly: a rollup missing the fields the question needs
    is an N+1 loop with extra steps. "Which bills are overdue?" / "how much do
    we owe Redbud?" were unanswerable from ``"12 unpaid bills, $8,400 total."``,
    so the only way to them was one read per bill — the fan-out the per-record
    caps exist to refuse, which converts the loop into a blank rather than an
    answer. The fields were all already on the rows we had fetched and thrown
    away (gotchas 41/50/51's shape, now the thirteenth instance).

    The VENDOR NAME is why ``vendor_names`` exists. A row reading ``vendor 214``
    forces exactly the per-bill hydration this change removes, one level over —
    so the caller resolves names in ONE bulk call and passes them in. An absent
    or unresolvable id degrades to "vendor not recorded" rather than printing a
    bare number, because a number is what the model tries to look up.

    Pure (no I/O) so the overdue math, the ordering and the money handling are
    unit-testable.
    """
    names = vendor_names or {}

    def _sort_key(b: Any) -> tuple[int, date]:
        # Overdue first (oldest due date first), then upcoming by due date,
        # then anything with no due date last.
        due = _bill_due_date(b)
        if due is None:
            return (2, date.max)
        return (0 if due < today else 1, due)

    ordered = sorted(bills, key=_sort_key)

    # 200 to match the open-work-order list: the whole point of returning rows
    # is that a cross-check ("which of these do we owe this week?") can be done
    # locally, and a cap small enough to hide a row sends the model back to the
    # per-bill loop this replaces.
    limit = 200
    lines = [header, ""]
    for b in ordered[:limit]:
        vendor_id = getattr(b, "vendor_id", None)
        label = names.get(vendor_id) if isinstance(vendor_id, int) else None
        bits = [label or "vendor not recorded"]

        # ⚠ ``bill_amount``, never ``b.total_amount``. Production bills carry NO
        # top-level total (0 of 67, gotcha 25) — the money is in the lines, and
        # a missing amount renders as words rather than laundering into $0.00.
        amount = bill_amount(b)
        bits.append(f"${amount:,.2f}" if amount is not None else "amount not recorded")

        due = _bill_due_date(b)
        if due is not None:
            days = (today - due).days
            if days > 0:
                bits.append(f"due {due.isoformat()} (overdue {days}d)")
            elif days == 0:
                bits.append(f"due {due.isoformat()} (due today)")
            else:
                bits.append(f"due {due.isoformat()} (in {-days}d)")
        else:
            bits.append("no due date")

        ref_no = getattr(b, "reference_number", None)
        if ref_no:
            bits.append(f"ref# {ref_no}")
        # ``[ref:X]`` is the internal-only chain handle — spend it on
        # ``buildium_get_bill`` (the lines behind this total) or
        # ``buildium_list_bill_payments``. The orchestrator forbids echoing it.
        bits.append(f"[ref:{getattr(b, 'id', '?')}]")
        lines.append("- " + " — ".join(bits))

    footer = _truncate_footer(len(ordered), limit, noun="unpaid bill")
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


async def _list_unpaid_bills_impl(
    property_name: str | None = None,
    days_overdue: int | None = None,
) -> str:
    from datetime import UTC, datetime, timedelta

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    try:
        async with client:
            # ``paidstatus=Unpaid`` is REQUIRED, not just cheaper: an
            # unfiltered GET /v1/bills 422s (live-probed 2026-07-06 —
            # paid bills in the result demand a FromPaidDate filter),
            # so there is no fetch-everything fallback. Local is_paid
            # re-check kept as belt-and-braces.
            bills = await api.bills.list(paidstatus="Unpaid")
            # ⚠ ``IsPaid`` is present-but-NULL on every production bill
            # (live-verified across all 67 on account 695763, 2026-08-23).
            # ``is False`` therefore rejected EVERY row and the tool reported
            # $0 against a real $26,000 exposure — the same $0 gotcha 25 fixed
            # in the money helper, re-introduced one line earlier by the
            # filter. The ``True`` default never fired either, because the
            # attribute exists; only its value is missing.
            #
            # Null means "Buildium did not say", and the fetch already
            # constrained the result to paidstatus=Unpaid — so the only thing
            # this re-check may drop is a row Buildium positively marked paid.
            bills = [b for b in bills if getattr(b, "is_paid", None) is not True]
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list unpaid bills")

    # Property name → id resolution. Done inside its own try so the
    # error message points at the actual failing call.
    target_property_id: int | None = None
    if property_name:
        try:
            async with client:
                properties = await api.rentals.list_properties()
        except BuildiumApiError as exc:
            return _sanitize_buildium_error(exc, "resolve that property name")
        needle = property_name.strip().lower()
        for p in properties:
            label_bits = [
                str(getattr(p, "name", "") or ""),
                _format_address(getattr(p, "address", None)),
            ]
            haystack = " ".join(b for b in label_bits if b).lower()
            if needle in haystack:
                target_property_id = p.id
                break
        if target_property_id is None:
            return f"I couldn't find a property matching '{property_name}'."

    cutoff: datetime | None = None
    if days_overdue is not None:
        cutoff = datetime.now(UTC) - timedelta(days=max(0, int(days_overdue)))

    def _passes(b: Any) -> bool:
        if cutoff is not None:
            due = getattr(b, "due_date", None)
            if due is None:
                return False
            # Buildium returns naive datetimes; treat them as UTC.
            due_aware = due if due.tzinfo else due.replace(tzinfo=UTC)
            if due_aware > cutoff:
                return False
        if target_property_id is not None and scoping_works:
            if target_property_id not in _bill_property_ids(b):
                return False
        return True

    # ⚠ ``resolved_property_id``, never ``line.property_id`` — and this call
    # site is why the rule needs stating twice. Buildium sends ``PropertyId``
    # on every production bill line as a KEY and populates it on NONE of them
    # (131/131 present, 0/131 populated, live-verified 2026-08-27); the money
    # is attributed through ``AccountingEntity.Id``, and only when the entity
    # type is ``Rental`` (a ``Company`` line is overhead and belongs to no
    # property). ``entities.upsert_bill`` was fixed for gotcha 53 and this
    # LIVE agent tool — reading the API directly rather than the mirror — was
    # not, so "unpaid bills at <property>" matched `None` against a real id
    # for every bill and answered **"No unpaid bills found at <property>."**
    # A fabricated absence, about money, stated with total confidence.
    # Gotcha 25's rule pointed at the fix for gotcha 25.
    #
    # And the fail-open (gotcha 55): if NOT ONE bill line carries a resolvable
    # property, the field cannot discriminate, so scope nothing and say the
    # scope did not hold. An unfiltered list is arguable and visible; an empty
    # one is confident and wrong.
    scoping_works = target_property_id is None or any(_bill_ownership_is_legible(b) for b in bills)

    filtered = [b for b in bills if _passes(b)]
    unscoped = target_property_id is not None and not scoping_works

    if not filtered:
        bits: list[str] = []
        if property_name:
            bits.append(f"at {property_name}")
        if days_overdue is not None:
            bits.append(f"{days_overdue}+ days overdue")
        suffix = f" {' '.join(bits)}" if bits else ""
        return f"No unpaid bills found{suffix}."

    # Only sum bills that actually carry an amount. A null amount coerced to 0
    # would SILENTLY understate the total shown to the property manager
    # (they'd see a confidently-wrong "$X total"); instead exclude those bills
    # from the sum and say so, so the number is honest about what it covers.
    priced_amounts = [amt for b in filtered if (amt := bill_amount(b)) is not None]
    unpriced = len(filtered) - len(priced_amounts)
    total = sum(priced_amounts)
    noun = "bill" if len(filtered) == 1 else "bills"
    msg = f"{len(filtered)} unpaid {noun}, ${total:,.2f} total"
    if unpriced:
        s = "s" if unpriced != 1 else ""
        msg += f" (excludes {unpriced} bill{s} with no amount recorded)"
    if unscoped:
        # The count is the WHOLE account's, not this property's. Say so in the
        # result rather than letting the caller's own question supply the
        # scope — an unscoped number presented under a scoped question is the
        # confident false claim of gotcha 41, pointed at money.
        msg += (
            f". ⚠ NOT scoped to {property_name}: not one bill line carries a "
            "property attribution, so this covers the WHOLE account. Do not "
            "report it as that property's"
        )

    # One bulk vendor fetch so every row can name WHO we owe. In its own try:
    # a vendor lookup that fails must cost the names, never the bills (gotcha
    # 7 — an enhancement is never load-bearing). Degraded rows say "vendor not
    # recorded", which is true and unlookupable, rather than a bare id the
    # model would then hydrate one bill at a time.
    vendor_names: dict[int, str] = {}
    if any(isinstance(getattr(b, "vendor_id", None), int) for b in filtered):
        try:
            async with client:
                for v in await api.vendors.list():
                    vid = getattr(v, "id", None)
                    if isinstance(vid, int):
                        vendor_names[vid] = _vendor_label(v)
        except BuildiumApiError:
            logger.warning("unpaid bills: vendor names unavailable, rendering ids as unrecorded")

    return _format_unpaid_bills(
        filtered,
        datetime.now(UTC).date(),
        vendor_names=vendor_names,
        header=f"{msg}:",
    )


@function_tool
async def buildium_list_unpaid_bills(
    property_name: str | None = None,
    days_overdue: int | None = None,
) -> str:
    """List unpaid AP bills in Buildium.

    Use this for "how many unpaid bills do we have", "any overdue
    bills at gardens row?", "what bills are past due 30+ days?".

    IMPORTANT: this is Accounts Payable (money WE owe vendors), NOT
    tenant rent delinquency — use ``buildium_list_overdue_balances``
    for tenant rent. The two often get confused; pick by who owes
    money to whom.

    Every bill is listed, most overdue first, with the vendor, the amount, the
    due date and how many days past it — so "which bills are overdue?" and
    "how much do we owe <vendor>?" are answered from THIS one call. Do NOT open
    bills one at a time to total a vendor's exposure; filter these rows.

    Args:
        property_name: Optional property name or address substring
            to filter bills by their line-item property association.
        days_overdue: Optional minimum days past the due date.

    Returns: a summary line (``"{N} unpaid bills, $X total"``) followed by one
    row per bill, or an honest "no matches" / "couldn't find that property"
    message.
    """
    return await _list_unpaid_bills_impl(property_name, days_overdue)


# ─────────────────────────────────────────────────────────────────────
# Work-order tools (expand existing)
# ─────────────────────────────────────────────────────────────────────


async def _get_work_order_impl(work_order_id: int) -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            wo = await api.work_orders.get(int(work_order_id))
            vendor_label = ""
            vendor_id = getattr(wo, "vendor_id", None)
            if vendor_id:
                try:
                    vendor = await api.vendors.get(int(vendor_id))
                    vendor_label = _vendor_label(vendor)
                except BuildiumApiError:
                    vendor_label = "vendor on file"
            property_label = ""
            property_id = getattr(wo, "property_id", None)
            if property_id:
                try:
                    prop = await api.rentals.get_property(int(property_id))
                    property_label = (
                        getattr(prop, "name", None)
                        or _format_address(getattr(prop, "address", None))
                        or ""
                    )
                except BuildiumApiError:
                    property_label = ""
            unit_label = ""
            unit_id = getattr(wo, "unit_id", None)
            if unit_id:
                try:
                    unit = await api.rentals.get_unit(int(unit_id))
                    un = getattr(unit, "unit_number", None)
                    if un:
                        unit_label = f"Unit {un}"
                except BuildiumApiError:
                    unit_label = ""
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "fetch that work order")

    title = getattr(wo, "title", None) or "Untitled work order"
    lines = [f"{title} [ref:{wo.id}]"]
    status = getattr(wo, "status", None)
    if status:
        lines.append(f"  Status: {status}")
    priority = getattr(wo, "priority", None)
    if priority:
        lines.append(f"  Priority: {priority}")
    if property_label:
        lines.append(f"  Property: {property_label}")
    if unit_label:
        lines.append(f"  {unit_label}")
    if vendor_label:
        lines.append(f"  Vendor: {vendor_label}")
    description = getattr(wo, "description", None)
    if description:
        lines.append(f"  Description: {description}")
    return "\n".join(lines)


@function_tool
async def buildium_get_work_order(work_order_id: int) -> str:
    """Get full detail on one work order — status, vendor, description.

    Use this for "what happened with work order #123?".
    """
    return await _get_work_order_impl(work_order_id)


async def _list_work_orders_for_unit_impl(unit_query: str) -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            unit, candidates, lookup_err = await _resolve_unit(api, unit_query)
            if lookup_err:
                return lookup_err
            if unit is None:
                lines = [f"Multiple units match '{unit_query}'. Please be more specific:"]
                for u in candidates[:10]:
                    lines.append(f"- {_format_unit_label(u)}")
                return "\n".join(lines)
            rows = list(await api.work_orders.list(unitids=[unit.id]))
            work_orders = _only_for_unit(rows, unit.id)
            # ⚠ ``?unitids`` is ignored AND ``WorkOrder.unit_id`` is null on
            # every production row, so ``_only_for_unit`` correctly fails open
            # (a blind field must not manufacture an absence) and hands back
            # the WHOLE ACCOUNT. That is how "the most recent open work order
            # at 500 Woodlane St" became 60 tickets at a one-unit property,
            # stated as fact and theorised about as duplicate test data.
            #
            # The property IS resolvable, through the backing task -- the same
            # join the mirror has done since #14. One bulk task read replaces
            # an unanswerable list, and it is only paid when the unit filter
            # came back blind.
            # ⚠ The trigger is the field being BLIND, not the count being
            # unchanged. A unit filter that keeps every row because every row
            # really is this unit's is working perfectly; using "kept
            # everything" as the signal fires the fallback on that healthy case
            # too. I wrote it that way first and a mock caught it.
            scope = "unit"
            if rows and _field_is_blind(rows, "unit_id"):
                prop_id = getattr(unit, "property_id", None)
                if prop_id is not None:
                    by_property = work_order_property_ids(rows, await api.tasks.list())
                    # An id-less row resolves to nothing and is dropped, which
                    # is the same rule ``work_order_property_ids`` applies: an
                    # unattributable row must not be attributed (gotcha 55).
                    scoped = [
                        w
                        for w in rows
                        if isinstance(wid := getattr(w, "id", None), int)
                        and by_property.get(wid) == prop_id
                    ]
                    # Same fail-open rule as ``_only_matching``: if the join
                    # resolved nothing, report the over-count rather than
                    # inventing an absence (gotcha 55).
                    if scoped:
                        work_orders, scope = scoped, "property"
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list work orders for that unit")

    label = _format_unit_label(unit)
    if not work_orders:
        return f"{label} has no work orders on record."

    limit = 25
    shown = work_orders[:limit]
    # ⚠ Say which precision this actually is. Buildium gives no unit linkage on
    # a work order, so a "property" scope counts every unit in the building --
    # identical at a single-unit property and an over-claim anywhere else, and
    # gotcha 50 records that exact over-claim being invisible in a Blue Ridge
    # fixture where every property has one unit.
    where = label if scope == "unit" else f"{label} (all units at this property)"
    lines = [f"{len(work_orders)} work order(s) for {where}:", ""]
    for w in shown:
        title = getattr(w, "title", None) or "Untitled work order"
        status = getattr(w, "status", None) or "?"
        lines.append(f"- [{status}] {title} [ref:{w.id}]")
    footer = _truncate_footer(len(work_orders), limit, noun="work order")
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


@function_tool
async def buildium_list_work_orders_for_unit(unit_query: str) -> str:
    """List all work orders (open + closed) for ONE specific unit.

    Use this for a genuinely single-unit question: "what's broken in 4B?",
    "history of repairs at #2".

    ⚠ NOT for portfolio work. If you need maintenance status across several
    units — a collections cross-check, renewal review, or any "which of
    these tenants…" question — call ``buildium_list_open_work_orders`` ONCE
    (its rows carry property/unit ids) and filter locally. Calling this tool
    in a loop is the documented way to blow the run's time budget.
    """
    return await _list_work_orders_for_unit_impl(unit_query)


# ─────────────────────────────────────────────────────────────────────
# Financial tools
# ─────────────────────────────────────────────────────────────────────


async def _list_overdue_balances_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            # PR #74 / PR #75 / PR #76 — Buildium's
            # /leases/outstandingbalances does NOT default to Active
            # leases, so pin ``leasestatuses=["Active"]``. PR #75
            # added ``entitytype="Rental"`` here but that param REQUIRES
            # a matching ``entityid`` (Buildium 400s otherwise), and
            # ``entitytype`` is for per-property lookups, not list
            # filtering — see PR #76 brief. PR #76 drops that param.
            # If the filtered call returns empty we still try a second
            # pass with ``balanceduration="TotalBalance"`` because
            # Buildium's default duration is ambiguous and can skip
            # leases that aren't in the "past due now" bucket.
            raw_balances: list[Any] = list(
                await api.leases.list_outstanding_balances(
                    leasestatuses=["Active"],
                )
            )
            if not raw_balances:
                logger.info(
                    "Buildium outstandingbalances empty with"
                    " leasestatuses=[Active] — retrying with"
                    " balanceduration=TotalBalance"
                )
                raw_balances = list(
                    await api.leases.list_outstanding_balances(
                        leasestatuses=["Active"],
                        balanceduration="TotalBalance",
                    )
                )
            balances: list[Any] = [b for b in raw_balances if _balance_of(b) > 0]
            balances.sort(key=_balance_of, reverse=True)
            lease_ids = [b.lease_id for b in balances[:25]]
            tenant_lookup: dict[int, str] = {}
            for lid in lease_ids:
                try:
                    lease = await api.leases.get(lid)
                except BuildiumApiError:
                    continue
                tenants = getattr(lease, "current_tenants", None) or []
                if tenants:
                    tenant_lookup[lid] = ", ".join(_tenant_full_name(t) for t in tenants)
                else:
                    tenant_lookup[lid] = "an unidentified tenant"
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list overdue balances")

    if not balances:
        # PR #75 — honest failure. Pre-PR-#75 we returned
        # "everyone's paid up" any time the endpoint came back empty
        # — including the case where Brandon's sandbox showed 17
        # delinquent leases in the UI. Lying confidently is worse
        # than admitting we couldn't reconcile the data.
        return (
            "I queried Buildium for active leases with outstanding balances "
            "and got no rows back. This may mean (a) all active leases are "
            "current, OR (b) the Buildium account doesn't return outstanding "
            "balances via this endpoint. If you know specific tenants are "
            "behind, that's worth flagging."
        )

    limit = 25
    shown = balances[:limit]
    noun = "lease" if len(balances) == 1 else "leases"
    lines = [
        f"{len(balances)} {noun} with an outstanding balance:",
        "",
    ]
    for b in shown:
        # PR #78 — ``_balance_of`` reads ``total_balance`` (spec)
        # with a fallback to the legacy ``outstanding_balance`` and
        # ignores non-numeric values, so MagicMock auto-attributes
        # don't sneak into the rendered line.
        amount = _format_money(_balance_of(b)) or "$0"
        days = getattr(b, "days_late", None)
        days_part = f" ({days} days overdue)" if days else ""
        unit_number = getattr(b, "unit_number", None)
        unit_part = f" at unit {unit_number}" if unit_number else ""
        # PR #75 Bug B — when we don't have a tenant name, don't reveal
        # the internal lease id. Use a neutral placeholder; the
        # property-manager UI already has the per-lease balance in the
        # rent roll if they want to drill.
        tenant = tenant_lookup.get(b.lease_id, "an unidentified tenant")
        # PR #73 — aging buckets + eviction signals Buildium already
        # returns. Surfaces lets the model answer "who's 60+ days late"
        # and avoid the contradiction where pending evictions show up
        # but the balance line says everyone's paid up.
        aging_bits: list[str] = []
        for label, attr in (
            (">90d", "balance_over_90_days"),
            ("61-90d", "balance_61_to_90_days"),
            ("31-60d", "balance_31_to_60_days"),
            ("0-30d", "balance_0_to_30_days"),
        ):
            bucket = getattr(b, attr, None)
            if bucket and float(bucket) > 0:
                aging_bits.append(f"{label} {_format_money(bucket)}")
        aging_part = f" [{', '.join(aging_bits)}]" if aging_bits else ""
        eviction_date = getattr(b, "eviction_pending_date", None)
        evict_part = " — EVICTION PENDING" if eviction_date else ""
        notice = getattr(b, "is_notice_given", None)
        notice_part = " (notice given)" if notice else ""
        lines.append(
            f"- {tenant}{unit_part} — {amount} owed{days_part}"
            f"{aging_part}{evict_part}{notice_part} [ref:{b.lease_id}]"
        )
    footer = _truncate_footer(len(balances), limit)
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


@function_tool
async def buildium_list_overdue_balances() -> str:
    """List leases with money owed — "who's behind on rent?".

    Returns fields per row: tenant names (not just lease ids), unit,
    total_balance (the actual Buildium spec field — pre-PR-#78 this
    was misread as ``OutstandingBalance``), days_late, aging buckets
    (0-30d / 31-60d / 61-90d / >90d), eviction_pending_date,
    is_notice_given.

    Sorted highest-balance first. THIS endpoint is the one place to
    answer: 'who is behind / delinquent / late', '60+ day delinquencies',
    'who's about to be evicted'. Do NOT iterate ``buildium_get_lease``
    looking for unpaid rent — every value you need is on this single
    response. If this endpoint returns empty but the user thinks
    something must be owed (e.g. pending evictions exist), flag the
    contradiction per the orchestrator's sanity-check rule.
    """
    return await _list_overdue_balances_impl()


async def _list_recent_lease_transactions_impl(lease_id: int, limit: int = 10) -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            txns = await api.leases.list_transactions(int(lease_id))
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list transactions for that lease")

    if not txns:
        return "No transactions found for this lease."

    txns_sorted: list[Any] = sorted(
        txns,
        key=lambda t: getattr(t, "date", None) or "",
        reverse=True,
    )
    safe_limit = max(1, min(int(limit), 50))
    shown = txns_sorted[:safe_limit]
    lines = ["Recent transactions on this lease:", ""]
    for t in shown:
        date = _format_date(getattr(t, "date", None))
        ttype = getattr(t, "transaction_type", None) or "?"
        amount = _format_money(getattr(t, "total_amount", None))
        lines.append(f"- {date} — {ttype} — {amount}")
    return "\n".join(lines)


@function_tool
async def buildium_list_recent_lease_transactions(lease_id: int, limit: int = 10) -> str:
    """Get the most recent payments / charges on one lease.

    Use this for "show me the payment history on lease #5678".
    """
    return await _list_recent_lease_transactions_impl(lease_id, limit)


# ─────────────────────────────────────────────────────────────────────
# Property tools (expand existing)
# ─────────────────────────────────────────────────────────────────────


async def _get_property_impl(property_id_or_query: str) -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    query = (property_id_or_query or "").strip()
    if not query:
        return "Please specify a property name, address, or ID."

    try:
        async with client:
            prop = None
            if query.isdigit():
                try:
                    prop = await api.rentals.get_property(int(query))
                except BuildiumApiError:
                    prop = None

            if prop is None:
                properties = await api.rentals.list_properties()
                needle = query.lower()
                matches = []
                for p in properties:
                    name = (getattr(p, "name", None) or "").lower()
                    addr = _format_address(getattr(p, "address", None)).lower()
                    if needle == name or needle in name or needle in addr:
                        matches.append(p)
                if not matches:
                    return f"No property matches '{query}'."
                if len(matches) > 1:
                    lines = [f"Multiple properties match '{query}'. Please be more specific:"]
                    for p in matches[:10]:
                        addr = _format_address(getattr(p, "address", None))
                        pname = getattr(p, "name", None) or "Unnamed property"
                        lines.append(f"- {pname} — {addr}")
                    return "\n".join(lines)
                prop = matches[0]

            # ⚠ CLIENT-SIDE SCOPE, always — gotcha 24's own prescription, which
            # this tool was not following. Battery v10-100 q95 ("post a $450
            # charge to the lease at MT — 1301 E 6th Ave") was told "Buildium
            # shows 50 active leases attached to that single-unit property".
            # The mirror says ONE. Fifty is the whole account, so the
            # ``propertyids`` filter came back ignored on a live production
            # call — the same silent-ignore gotcha 24 records for /tasks and
            # /bills while asserting /leases was fine. It is not.
            #
            # Re-filtering is a NO-OP when the server filter works and a
            # correction when it does not, so it cannot make anything worse.
            # Rows with no PropertyId are dropped rather than kept: an
            # unattributable row counted toward THIS property is the false
            # statement we are removing, and Unit.property_id is already
            # documented as occasionally absent on draft rows.
            units = _only_for_property(await api.rentals.list_units(propertyids=[prop.id]), prop.id)
            owners_text = ""
            owner_ids = getattr(prop, "rental_owner_ids", None) or []
            owner_names: list[str] = []
            for oid in owner_ids[:5]:
                try:
                    owner = await api.rentals.get_owner(int(oid))
                except BuildiumApiError:
                    continue
                company = getattr(owner, "company_name", None)
                first = getattr(owner, "first_name", None) or ""
                last = getattr(owner, "last_name", None) or ""
                person = f"{first} {last}".strip()
                owner_names.append(company or person or "Unnamed owner")
            if owner_names:
                owners_text = ", ".join(owner_names)

            active_leases = _only_for_property(
                await api.leases.list(propertyids=[prop.id], leasestatuses=["Active"]),
                prop.id,
            )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "fetch that property")

    name = getattr(prop, "name", None) or "Unnamed property"
    lines = [name]
    addr = _format_address(getattr(prop, "address", None))
    if addr:
        lines.append(f"  Address: {addr}")
    lines.append(f"  Units: {len(units)}")
    lines.append(f"  Active leases: {len(active_leases)}")
    if owners_text:
        lines.append(f"  Owners: {owners_text}")
    return "\n".join(lines)


@function_tool
async def buildium_get_property(property_id_or_query: str) -> str:
    """Look up ONE property by id, name, or address.

    Returns name, address, unit count, active lease count, and owner
    names. Use this for "tell me about 123 Main Street" or "who owns
    the building at 100 Elm".
    """
    return await _get_property_impl(property_id_or_query)


# ─────────────────────────────────────────────────────────────────────
# PR #73 — high-leverage tools that close common iteration failures.
# Each one is the documented routing-map answer for a class of
# property-manager question. Bucket A from the PR brief.
# ─────────────────────────────────────────────────────────────────────


def _owner_search_text(owner: Any) -> str:
    """Everything about an owner a name query could reasonably match."""
    return " ".join(
        str(part).lower()
        for part in (
            getattr(owner, "company_name", None),
            getattr(owner, "first_name", None),
            getattr(owner, "last_name", None),
        )
        if part
    )


def _owners_matching(owners: list[Any], query: str) -> list[Any]:
    """Owners whose name best overlaps ``query``, best score only.

    Scored on WORDS, not on the whole string, because an owner's name is
    split across three fields and a user types it as one phrase. Returning
    only the best-scoring band keeps a two-word hit from being buried under
    every owner that happens to share a first name.
    """
    words = [w for w in query.lower().split() if w]
    if not words:
        return []
    scored: list[tuple[int, Any]] = []
    for owner in owners:
        haystack = _owner_search_text(owner)
        overlap = sum(1 for w in words if w in haystack)
        if overlap:
            scored.append((overlap, owner))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    return [owner for score, owner in scored if score == best]


async def _get_owner_impl(owner_id_or_query: str) -> str:
    """Resolve an owner by id or fuzzy match, return full contact card."""
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    query = (owner_id_or_query or "").strip()
    if not query:
        return "Please specify which owner (company name, last name, or id)."

    try:
        async with client:
            if query.isdigit():
                try:
                    owner = await api.rentals.get_owner(int(query))
                    return _format_owner_card(owner)
                except BuildiumApiError:
                    pass
            owners = await api.rentals.list_owners()
            # ⚠ A FULL NAME MATCHED NOTHING. This tested the whole query
            # against each field SEPARATELY — `"margaret whitlock" in
            # "whitlock"` is False, and so is `in "margaret"` — so the one
            # phrasing a user is most likely to type ("who is Margaret
            # Whitlock") returned "No rental owner matches". Every existing
            # test passed a single word, which is why it survived.
            #
            # Token overlap instead, the same shape ``_get_tenant_impl``
            # already uses: score each owner by how many query words appear in
            # its name, keep the best. A single word still works (overlap 1),
            # a full name now works (overlap 2), and a query with a stray word
            # degrades instead of failing outright.
            matches = _owners_matching(owners, query)
            if not matches:
                return f"No rental owner matches '{query}'."
            if len(matches) > 1:
                tops = matches[:5]
                lines_out: list[str] = []
                for o in tops:
                    name = (
                        getattr(o, "company_name", None)
                        or getattr(o, "last_name", "")
                        or "Unnamed owner"
                    )
                    email = getattr(o, "email", None)
                    email_part = f" — {email}" if email else ""
                    lines_out.append(f"- {name}{email_part}")
                bullets = "\n".join(lines_out)
                return (
                    f"{len(matches)} owners match '{query}'. Please be more specific "
                    f"(company name or last name):\n{bullets}"
                )
            owner = await api.rentals.get_owner(matches[0].id)
            return _format_owner_card(owner)
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "fetch that owner")


def _owner_phone_numbers(o: Any) -> list[Any]:
    """Phone-number entries off a rental owner.

    RentalOwner (unlike Tenant/Vendor) does NOT declare ``phone_numbers`` — the
    ``PhoneNumbers`` list rides along as an extra (BuildiumModel ``extra=allow``)
    under its Buildium alias, so a snake_case ``getattr`` silently misses it and
    the card showed no phone at all. Check both attribute spellings (and a plain
    dict) so owner contact cards actually render the number."""
    if isinstance(o, dict):
        return o.get("PhoneNumbers") or o.get("phone_numbers") or []
    return getattr(o, "phone_numbers", None) or getattr(o, "PhoneNumbers", None) or []


def _format_owner_card(o: Any) -> str:
    company = getattr(o, "company_name", None)
    first = getattr(o, "first_name", None) or ""
    last = getattr(o, "last_name", None) or ""
    person = f"{first} {last}".strip()
    name = company or person or "Unnamed owner"
    lines = [name]
    email = getattr(o, "email", None)
    if email:
        lines.append(f"Email: {email}")
    phones = _owner_phone_numbers(o)
    if phones:
        nums = [
            getattr(p, "number", None) if not isinstance(p, dict) else p.get("Number")
            for p in phones
        ]
        nums_str = ", ".join(n for n in nums if n)
        if nums_str:
            lines.append(f"Phone: {nums_str}")
    addr = _format_address(getattr(o, "address", None))
    if addr:
        lines.append(f"Mailing: {addr}")
    prop_ids = getattr(o, "property_ids", None) or []
    if prop_ids:
        lines.append(f"Owns {len(prop_ids)} {'property' if len(prop_ids) == 1 else 'properties'}")
    # ⚠ SAY when there is nothing to say. Silently omitting the contact lines
    # leaves the model to infer, and it infers wrongly in the direction that
    # costs most: an owner card with no Email line reads as a card whose email
    # simply wasn't worth printing, and the reply then offers to "reach out".
    #
    # ⚠ CONDITIONAL, not a constant. Half the owners in the product DO carry
    # contact details — live-verified 2026-08-28: Blue Ridge 0 of 4, My
    # Company 4 of 4. Hardcoding the line would print a fresh false claim for
    # every owner that has one, which is the same over-generalisation as
    # marking every tenant FORMER (gotcha 51).
    if not any(line.startswith(("Email:", "Phone")) for line in lines):
        lines.append(
            "No email or phone on file for this owner in Buildium — say so "
            "rather than offering to contact them."
        )
    return "\n".join(lines)


@function_tool
async def buildium_get_owner(owner_id_or_query: str) -> str:
    """Get a rental owner's full contact card by id or fuzzy name match.

    Resolves an id (int) or a human-readable query ("Ocean Properties
    LLC", "ocean", "smith") against the owner list, then returns the
    matched owner's contact details.

    Returns fields: company_name, first_name, last_name, email,
    phone_numbers, address, property_ids (list), is_active.

    Use this whenever the user asks about a specific rental owner —
    contact info, properties owned. Do NOT call ``buildium_list_owners``
    and ask the user to pick if the partial name is unambiguous; this
    tool fuzzy-matches for you. Only ask to disambiguate when 2+
    owners match.
    """
    return await _get_owner_impl(owner_id_or_query)


async def _list_upcoming_lease_expirations_impl(within_days: int) -> str:
    """Active leases ending in the next ``within_days`` days."""
    from datetime import UTC, datetime, timedelta

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    today = datetime.now(UTC).date()
    cutoff = today + timedelta(days=max(1, within_days))
    try:
        async with client:
            leases = await api.leases.list(
                leasestatuses=["Active"],
                leasetodatefrom=today.isoformat(),
                leasetodateto=cutoff.isoformat(),
            )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list upcoming lease expirations")

    if not leases:
        return f"No active leases expiring in the next {within_days} days. Renewals can wait."

    # Sort by soonest-expiring first.
    def _to_date(lease: Any) -> Any:
        d = getattr(lease, "lease_to_date", None)
        return getattr(d, "date", lambda: d)() if d else cutoff

    leases.sort(key=_to_date)
    limit = 25
    shown = leases[:limit]
    lines = [
        f"{len(leases)} active {'lease' if len(leases) == 1 else 'leases'} "
        f"expiring in the next {within_days} days:",
        "",
    ]
    for lease in shown:
        tenants = getattr(lease, "current_tenants", None) or []
        names = ", ".join(_tenant_full_name(t) for t in tenants) or "no tenants on file"
        through = _format_date(getattr(lease, "lease_to_date", None))
        rent = _format_money(getattr(lease, "rent", None))
        rent_part = f" — {rent}/mo" if rent else ""
        d = _to_date(lease)
        try:
            days_left = (d - today).days if hasattr(d, "year") else None
        except TypeError:
            days_left = None
        days_part = f" ({days_left}d)" if days_left is not None else ""
        unit_label = _lease_unit_label(lease)
        lines.append(
            f"- {names} at {unit_label}{rent_part} — through {through}{days_part} [ref:{lease.id}]"
        )
    footer = _truncate_footer(len(leases), limit)
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


@function_tool
async def buildium_list_upcoming_lease_expirations(within_days: int = 90) -> str:
    """List active leases that will expire within the next N days.

    Returns one row per expiring lease: lease_id, current_tenants
    (names), unit (property + unit number), Rent (monthly $),
    lease_to_date, days_remaining. Sorted soonest-expiring first.

    Use this for 'what leases are ending soon' / 'renewals due' / 'who
    needs a renewal letter' questions. Do NOT iterate
    ``buildium_get_lease`` — Buildium's list endpoint supports
    date-range filters and this tool wires them up.
    """
    return await _list_upcoming_lease_expirations_impl(within_days)


async def _list_vacant_units_impl() -> str:
    """All units with no active lease, sorted longest-vacant-first.

    Buildium doesn't have a "vacant units" endpoint, so this tool
    joins units + active leases inside one tool call (so the model
    sees ONE round-trip from its perspective). Returns up to 25 rows.
    """
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            units = await api.rentals.list_units()
            active_leases = await api.leases.list(leasestatuses=["Active"])
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list vacant units")

    leased_unit_ids = {
        getattr(lease, "unit_id", None)
        for lease in active_leases
        if getattr(lease, "unit_id", None)
    }
    vacant = [u for u in units if u.id not in leased_unit_ids]
    if not vacant:
        return "No vacant units — every unit has an active lease."

    limit = 25
    shown = vacant[:limit]
    lines = [
        f"{len(vacant)} vacant {'unit' if len(vacant) == 1 else 'units'}:",
        "",
    ]
    for u in shown:
        unit_num = getattr(u, "unit_number", None) or "Unnumbered unit"
        addr = _format_address(getattr(u, "address", None))
        rent = _format_money(getattr(u, "market_rent", None))
        rent_part = f" — market rent {rent}" if rent else ""
        addr_part = f" — {addr}" if addr else ""
        lines.append(f"- Unit {unit_num}{addr_part}{rent_part}")
    footer = _truncate_footer(len(vacant), limit)
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


@function_tool
async def buildium_list_vacant_units() -> str:
    """List all units with no active lease.

    Returns fields per row: unit_id, unit_number, property address,
    market_rent. Sorted by unit id (Buildium doesn't expose vacancy
    duration; longest-vacant-first would need historical lease
    queries).

    Use this for 'what's vacant' / 'vacancy report' questions. Buildium
    surfaces this directly via a single tool call — do NOT derive by
    listing all units and cross-referencing leases manually; that's
    what this tool's implementation already does inside one call.
    """
    return await _list_vacant_units_impl()


async def _get_rent_roll_impl() -> str:
    """Active leases + outstanding-balance join in one tool call.

    Implementation detail: Buildium doesn't have a single "rent roll"
    endpoint, so this tool joins ``list_active_leases`` (which
    returns ``Rent``) with ``list_outstanding_balances`` (aging
    buckets + eviction status) and presents one row per lease. The
    model sees ONE call — the iteration that PR #73's brief flagged
    as the rent-ranking bug is impossible from this entry point.
    """
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            leases = await api.leases.list(leasestatuses=["Active"])
            # PR #74 / PR #75 — same fix as
            # ``_list_overdue_balances_impl``: pin
            # ``leasestatuses=["Active"]``; if the first call returns
            # empty, retry with ``balanceduration="TotalBalance"``.
            # PR #76 drops the PR #75 ``entitytype="Rental"`` param —
            # Buildium 400s without a matching ``entityid`` and that
            # parameter is for per-property lookups, not list filtering.
            balances: list[Any] = list(
                await api.leases.list_outstanding_balances(
                    leasestatuses=["Active"],
                )
            )
            if not balances:
                logger.info(
                    "Buildium rent-roll outstandingbalances empty with"
                    " leasestatuses=[Active] — retrying with"
                    " balanceduration=TotalBalance"
                )
                balances = list(
                    await api.leases.list_outstanding_balances(
                        leasestatuses=["Active"],
                        balanceduration="TotalBalance",
                    )
                )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "build the rent roll")

    # ``list_outstanding_balances`` returns ``list[OutstandingBalance]`` —
    # cast to a plain Iterable so mypy doesn't trip on the generic shape
    # from the dynamically-attributed endpoint surface.
    balances_iter: list[Any] = list(balances) if balances else []
    bal_by_lease: dict[int, Any] = {b.lease_id: b for b in balances_iter}
    if not leases:
        return "No active leases — rent roll is empty."

    # Sort by rent desc so "rank by rent" answers come out right
    # without the model having to sort itself.
    def _rent_of(lease: Any) -> float:
        return float(getattr(lease, "rent", None) or 0)

    leases_sorted = sorted(leases, key=_rent_of, reverse=True)
    limit = 25
    shown = leases_sorted[:limit]
    total_rent = sum(_rent_of(lease) for lease in leases_sorted)
    lines = [
        f"Rent roll: {len(leases_sorted)} active leases, "
        f"total monthly rent {_format_money(total_rent)}:",
        "",
    ]
    for lease in shown:
        rent = _format_money(getattr(lease, "rent", None))
        unit_label = _lease_unit_label(lease)
        tenants = getattr(lease, "current_tenants", None) or []
        names = ", ".join(_tenant_full_name(t) for t in tenants) or "no tenants"
        bal = bal_by_lease.get(lease.id)
        bal_part = ""
        if bal is not None:
            # PR #78 — ``_balance_of`` reads ``total_balance`` (spec)
            # with a fallback to the legacy ``outstanding_balance``.
            outstanding = _balance_of(bal)
            if outstanding > 0:
                eviction = getattr(bal, "eviction_pending_date", None)
                evict_part = " — EVICTION PENDING" if eviction else ""
                bal_part = f" — owes {_format_money(outstanding)}{evict_part}"
        lines.append(f"- {names} at {unit_label} — {rent}/mo{bal_part} [ref:{lease.id}]")
    footer = _truncate_footer(len(leases_sorted), limit)
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


@function_tool
async def buildium_get_rent_roll() -> str:
    """Get the current rent roll — every active lease with rent +
    outstanding balance in one shot.

    Returns fields per row: lease_id, current_tenants, unit (property
    + unit number), Rent (monthly $), total_balance owed (the actual
    Buildium spec field — PR #78 fixed the wrong-alias bug),
    eviction_pending flag. Sorted by Rent descending so "rank by rent"
    answers come out right without the model sorting itself. Tail line
    has the total monthly rent across the portfolio.

    Use this for 'rank by rent' / 'total monthly rent' / 'rent roll'
    questions. This is ONE call from your perspective even though the
    implementation joins two endpoints. DO NOT iterate
    ``buildium_get_lease`` per lease — that's the exact failure
    mode PR #73 was scoped to kill.
    """
    return await _get_rent_roll_impl()


# ─────────────────────────────────────────────────────────────────────
# PR #76 — Task creation
#
# Pre-PR-#76 the agent told the user "Buildium's API does not support
# task management" because no create-task tool was wired. Buildium has
# a full Tasks API; ``api.tasks.create(...)`` POSTs to
# ``/v1/tasks/todorequests``. This tool is the first user-write surface
# in the Buildium tool set, so it MUST go through the confirm gate —
# the orchestrator instructions call this out explicitly.
# ─────────────────────────────────────────────────────────────────────


def _confirm_details_for_task(
    title: str,
    description: str | None,
    assigned_to_user_id: int | None,
    property_id: int | None,
    unit_id: int | None,
    due_date: str | None,
    priority: str = "Normal",
) -> tuple[str, list[str]]:
    """Build the human-readable confirm preview (details + items) for a
    task-creation write. Kept separate so the gate text is easy to read
    and to assert in tests."""
    items = [f"Title: {title}", f"Priority: {priority}"]
    if description:
        items.append(f"Description: {description}")
    if assigned_to_user_id is not None:
        items.append(f"Assigned to user: {assigned_to_user_id}")
    if property_id is not None:
        items.append(f"Property: {property_id}")
    if unit_id is not None:
        items.append(f"Unit: {unit_id}")
    if due_date:
        items.append(f"Due: {due_date}")
    return f'Create a new To Do task in Buildium: "{title}".', items


async def _create_to_do_task_impl(
    title: str,
    description: str | None = None,
    assigned_to_user_id: int | None = None,
    property_id: int | None = None,
    unit_id: int | None = None,
    due_date: str | None = None,
    priority: str = "Normal",
) -> str:
    """Create a To Do task in Buildium.

    Confirmation AND idempotency are enforced IN CODE via ``run_guarded``: the
    write runs only on explicit approval, and an approved create is claimed by
    ``(company_id, idem_key)`` so a duplicate (double-approval / retry /
    restart) replays the prior result instead of POSTing twice. Because the
    gate lives inside the tool body, the write cannot execute regardless of
    model tool-ordering.
    """
    from datetime import timedelta

    # Check the connection FIRST (a side-effect-free credential load) so we
    # never prompt the user to approve a write that can't run anyway.
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    # ⚠ AssignedToUserId is REQUIRED by Buildium, and this signature made it
    # optional. Live 2026-08-30, Blue Ridge: POST /tasks/todorequests returned
    # 422 {"Key":"AssignedToUserId","Value":"AssignedToUserId must be greater
    # than 0."} on every create the write harness attempted. The 422 is raised
    # inside ``_do_create``, so ``run_guarded`` reaps the claim
    # ``abandoned_unknown`` -- which is NEVER retried by design, because a
    # duplicate write in a landlord's records is worse than a missing one. So
    # one absent field did not fail the call; it killed the action permanently.
    #
    # Checked HERE, beside the connection check above and for the same stated
    # reason: never prompt for approval of a write that cannot run. This is a
    # PRE-side-effect return -- nothing has been claimed and nothing POSTed --
    # so the agent can resolve an assignee and try again, which the
    # abandoned_unknown path forbids. The 2026-07-05 write-probe added Priority
    # and TaskStatus for this same 422 and missed the third required field.
    if assigned_to_user_id is None:
        return (
            "Buildium requires an assignee to create a To Do task, and none was "
            "given. Call buildium_list_staff to get the staff user ids, then "
            "call this tool again with assigned_to_user_id set. Nothing was "
            "created."
        )

    # Then gate the actual write. The await lives inside the tool, so the
    # POST below cannot run without an explicit approval. The confirm gate and
    # the idempotency claim are both owned by ``run_guarded`` — a duplicate
    # approved create (double-approval / retry / restart) cannot fire twice.
    details, items = _confirm_details_for_task(
        title, description, assigned_to_user_id, property_id, unit_id, due_date, priority
    )

    # Write-probe 2026-07-05: Buildium 422s POST /tasks/todorequests without
    # Priority AND TaskStatus ("missing or invalid" — the API's own words).
    # Every real confirm would have failed. "New" matches the value the
    # work-order create already sends for its embedded parent Task.
    payload: dict[str, Any] = {
        "Title": title,
        "Priority": priority or "Normal",
        "TaskStatus": "New",
    }
    if description:
        payload["Description"] = description
    if assigned_to_user_id is not None:
        payload["AssignedToUserId"] = assigned_to_user_id
    if property_id is not None:
        payload["PropertyId"] = property_id
    if unit_id is not None:
        payload["UnitId"] = unit_id
    if due_date:
        payload["DueDate"] = due_date

    # Dedup key from the FULL task fields (no truncation/masking — sha256 keeps
    # them private). 24h window: an identical task within a day collapses to one.
    company_id = _current_company_id()
    key = derive_key(
        company_id,
        "buildium_create_to_do_task",
        {
            "title": title,
            "unit_id": unit_id,
            "due_date": due_date,
            "assigned_to_user_id": assigned_to_user_id,
            "description": description,
            "priority": priority,
        },
    )

    create_error: dict[str, str] = {}
    # The created task id is captured here transiently for the read-back verify
    # ONLY. PR #75: it must NEVER reach the user-facing string, the
    # model-visible ``guard.result`` (fresh or replay), or ``_log_activity`` —
    # and cut 1 persists it nowhere (not ``result``, no column, no audit row).
    created_id: dict[str, int] = {}

    async def _do_create() -> str:
        # Only the Buildium POST is "ambiguous"; everything above already ran.
        try:
            async with client:
                created = await api.tasks.create(fields=payload)
        except BuildiumApiError as exc:
            # Surface a sanitized message; the HTTP write may or may not have
            # landed, so this is NOT a pre-side-effect error — let the guard
            # mark it abandoned_unknown rather than retryable.
            create_error["msg"] = _sanitize_buildium_error(exc, "create the task")
            raise
        # Capture the id transiently for verify; never returned/leaked.
        if isinstance(created, dict):
            raw_id = created.get("Id") or created.get("id")
            if isinstance(raw_id, int):
                created_id["id"] = raw_id
        # PR #75 HARD PRESENTATION RULES — never leak the Buildium task id.
        return f'Task created in Buildium: "{title}".'

    async def _verify_created(_result: str) -> bool:
        """Read the task back to confirm it actually landed. READ-ONLY: GETs
        only, never re-creates. Bounded retries to absorb create→read lag.
          * found (id present) → True
          * definitive 404 after retries → False (→ verify_failed:not_found)
          * keeps erroring/timing out through exhaustion → raise (verify_error)
        """
        import asyncio

        task_id = created_id.get("id")
        if task_id is None:
            # Create returned no id we could read back — can't confirm.
            return False
        last_exc: Exception | None = None
        for attempt in range(_VERIFY_MAX_ATTEMPTS):
            try:
                async with client:
                    task = await api.tasks.get(task_id)
                if task is not None and getattr(task, "id", None) == task_id:
                    return True
            except BuildiumApiError as exc:
                if exc.status == 404:
                    # Definitive not-found — but allow a couple of retries for
                    # read-after-write lag before declaring it missing.
                    last_exc = None
                    if attempt < _VERIFY_MAX_ATTEMPTS - 1:
                        await asyncio.sleep(_VERIFY_BACKOFF_SECONDS * (attempt + 1))
                        continue
                    return False
                last_exc = exc
            except Exception as exc:  # noqa: BLE001 — transient read failure
                last_exc = exc
            if attempt < _VERIFY_MAX_ATTEMPTS - 1:
                await asyncio.sleep(_VERIFY_BACKOFF_SECONDS * (attempt + 1))
        if last_exc is not None:
            # Read kept failing through exhaustion → verifier error (needs review).
            raise last_exc
        return False

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="buildium_create_to_do_task",
            confirm={
                "action": "buildium_create_task",
                "title": "Create a task in Buildium?",
                "details": details,
                "items": items,
                # The ACTUAL Buildium POST fields (approval-pipeline PR).
                "request_body": payload,
            },
            execute=_do_create,
            verify=_verify_created,
            window=timedelta(hours=24),
        )
    except BuildiumApiError as exc:
        # The sanitized message was captured inside _do_create before the raise;
        # fall back to sanitizing the caught exception if somehow absent.
        return create_error.get("msg") or _sanitize_buildium_error(exc, "create the task")

    if guard.outcome == "not_approved":
        return "Okay, I didn't create the task. Nothing was changed in Buildium."
    if guard.outcome == "blocked_store_unavailable":
        # Fail-closed BEFORE the write ran — NOTHING was created. Must not claim
        # an action that never happened (issue #227).
        return (
            "I couldn't safely process this right now, so I did not create the task — "
            "nothing was changed in Buildium. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        # Covers BOTH "already created, can't safely re-confirm" (idempotency)
        # AND "created but the read-back couldn't confirm it" (verify). Honest,
        # id-free, and never a false success.
        return (
            "I created the task in Buildium but couldn't confirm it went through, "
            "so I'm flagging it for review. Please check Buildium before retrying."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="create the task", system="Buildium")
    # executed or replayed → the user-facing success line (replay returns the
    # same stored string, so a duplicate request reports success without a 2nd POST).
    if guard.result is not None:
        return str(guard.result)
    return f'Task created in Buildium: "{title}".'


@function_tool
async def buildium_create_to_do_task(
    title: str,
    description: str | None = None,
    assigned_to_user_id: int | None = None,
    property_id: int | None = None,
    unit_id: int | None = None,
    due_date: str | None = None,
    priority: str = "Normal",
) -> str:
    """Create a To Do task in Buildium.

    Use this when the user says "create a task for X to do Y" or
    similar. Task creation is a write; confirmation is enforced in code
    by the tool itself (``confirm_or_abort`` inside the impl) — you do
    NOT need a separate confirmation step. If the user declines, the
    tool reports that nothing was changed.

    Args:
        title: Short task title (e.g. "Send lease to Ryan").
        description: Optional longer description / notes.
        assigned_to_user_id: REQUIRED by Buildium despite being
            optional in this signature. Buildium 422s a create with no
            assignee ("AssignedToUserId must be greater than 0"), so it
            does NOT create the task unassigned — an earlier version of
            this docstring said it did, and that instruction is what
            made every create fail. If you don't know the id, call
            buildium_list_staff first and pass one.
        property_id: Optional Buildium property id to attach the task to.
        unit_id: Optional Buildium unit id to attach the task to.
        due_date: Optional ISO date string ``YYYY-MM-DD``.
        priority: Buildium task priority — "Low", "Normal", or "High".
            Defaults to "Normal" (required by Buildium's API).

    Returns:
        On success: ``'Task created in Buildium: "<title>".'``. The
        Buildium task id is NOT surfaced to the user (HARD PRESENTATION
        RULES). On failure: a short sanitized error string — never
        Buildium's raw error text.
    """
    return await _create_to_do_task_impl(
        title=title,
        description=description,
        assigned_to_user_id=assigned_to_user_id,
        property_id=property_id,
        unit_id=unit_id,
        due_date=due_date,
        priority=priority,
    )


def _confirm_details_for_work_order(
    *,
    title: str,
    description: str,
    property_id: int,
    vendor_id: int,
    assigned_to_user_id: int,
    priority: str,
    unit_id: int | None,
) -> tuple[str, list[str]]:
    """Human-readable confirm preview for a work-order create. Uses the
    agent-supplied display labels (so the user sees "Apex Plumbing", not a raw
    id); falls back to the id only if no label was provided."""
    items = [f"Title: {title}", f"Description: {description}"]
    # ⚠ The *_label args are DELIBERATELY IGNORED (see _gate_label): a card must
    # not render the model's belief. This helper is sync and holds no API client,
    # so it shows the ID form — honest and checkable against Buildium, unlike a
    # confidently wrong name. Deriving here needs a getter these kinds lack.
    items.append(f"Property: {property_id}")
    items.append(f"Vendor: {vendor_id}")
    items.append(f"Assigned to: {assigned_to_user_id}")
    items.append(f"Priority: {priority}")
    if unit_id is not None:
        items.append(f"Unit: {unit_id}")
    return f'Create a work order in Buildium: "{title}".', items


async def _create_work_order_impl(
    title: str,
    description: str,
    property_id: int,
    vendor_id: int,
    assigned_to_user_id: int,
    priority: str = "Normal",
    unit_id: int | None = None,
    entry_allowed: str = "Unknown",
    property_label: str | None = None,
    vendor_label: str | None = None,
    assignee_label: str | None = None,
) -> str:
    """Create a work order in Buildium.

    Mirrors ``_create_to_do_task_impl`` exactly: the write runs only on explicit
    approval (confirm gate), is single-execution per ``(company_id, idem_key)``
    (idempotency), and is read back to confirm it landed (verify) — all owned by
    ``run_guarded``. Buildium's data model is Task → WorkOrder, so the POST body
    embeds a parent ``Task`` and requires ``VendorId`` + ``EntryAllowed`` (the
    shape proven against the sandbox in scripts/synthetic_traffic.py).
    """
    from datetime import timedelta

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    details, items = _confirm_details_for_work_order(
        title=title,
        description=description,
        property_id=property_id,
        vendor_id=vendor_id,
        assigned_to_user_id=assigned_to_user_id,
        priority=priority,
        unit_id=unit_id,
    )

    # Buildium work-order CREATE body. Required: PropertyId, Title, Description,
    # VendorId, EntryAllowed, and an embedded parent Task (Title/Description/
    # Priority/Status/AssignedToUserId). See #200/#201.
    task_body: dict[str, Any] = {
        "Title": title,
        "Description": description,
        "Priority": priority,
        "Status": "New",
        "AssignedToUserId": assigned_to_user_id,
        "PropertyId": property_id,
    }
    body: dict[str, Any] = {
        "PropertyId": property_id,
        "Title": title,
        "Description": description,
        "VendorId": vendor_id,
        "EntryAllowed": entry_allowed,
        "Priority": priority,
        "Task": task_body,
    }
    if unit_id is not None:
        body["UnitId"] = unit_id
        task_body["UnitId"] = unit_id

    company_id = _current_company_id()
    key = derive_key(
        company_id,
        "buildium_create_work_order",
        {
            "title": title,
            "description": description,
            "property_id": property_id,
            "unit_id": unit_id,
            "vendor_id": vendor_id,
            "assigned_to_user_id": assigned_to_user_id,
        },
    )

    create_error: dict[str, str] = {}
    created_id: dict[str, int] = {}

    async def _do_create() -> str:
        try:
            async with client:
                created = await api.work_orders.create(body)
        except BuildiumApiError as exc:
            create_error["msg"] = _sanitize_buildium_error(exc, "create the work order")
            raise
        wo_id = getattr(created, "id", None)
        if isinstance(wo_id, int):
            created_id["id"] = wo_id
        # Never leak the Buildium work-order id to the user.
        return f'Work order created in Buildium: "{title}".'

    async def _verify_created(_result: str) -> bool:
        """Read the work order back to confirm it landed. READ-ONLY GETs,
        bounded retries; mirrors the create-task verifier."""
        import asyncio

        wo_id = created_id.get("id")
        if wo_id is None:
            return False
        last_exc: Exception | None = None
        for attempt in range(_VERIFY_MAX_ATTEMPTS):
            try:
                async with client:
                    wo = await api.work_orders.get(wo_id)
                if wo is not None and getattr(wo, "id", None) == wo_id:
                    return True
            except BuildiumApiError as exc:
                if exc.status == 404:
                    last_exc = None
                    if attempt < _VERIFY_MAX_ATTEMPTS - 1:
                        await asyncio.sleep(_VERIFY_BACKOFF_SECONDS * (attempt + 1))
                        continue
                    return False
                last_exc = exc
            except Exception as exc:  # noqa: BLE001 — transient read failure
                last_exc = exc
            if attempt < _VERIFY_MAX_ATTEMPTS - 1:
                await asyncio.sleep(_VERIFY_BACKOFF_SECONDS * (attempt + 1))
        if last_exc is not None:
            raise last_exc
        return False

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="buildium_create_work_order",
            confirm={
                "action": "buildium_create_work_order",
                "title": "Create a work order in Buildium?",
                "details": details,
                "items": items,
                # The ACTUAL Buildium POST body — the approver sees what will
                # really be sent (approval-pipeline PR, thin-preview fix).
                "request_body": body,
            },
            execute=_do_create,
            verify=_verify_created,
            window=timedelta(hours=24),
        )
    except BuildiumApiError as exc:
        return create_error.get("msg") or _sanitize_buildium_error(exc, "create the work order")

    if guard.outcome == "not_approved":
        return "Okay, I didn't create the work order. Nothing was changed in Buildium."
    if guard.outcome == "blocked_store_unavailable":
        return (
            "I couldn't safely process this right now, so I did not create the work order — "
            "nothing was changed in Buildium. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        return (
            "I created the work order in Buildium but couldn't confirm it went through, "
            "so I'm flagging it for review. Please check Buildium before retrying."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="create the work order", system="Buildium")
    if guard.result is not None:
        return str(guard.result)
    return f'Work order created in Buildium: "{title}".'


@function_tool
async def buildium_create_work_order(
    title: str,
    description: str,
    property_id: int,
    vendor_id: int,
    assigned_to_user_id: int,
    priority: str = "Normal",
    unit_id: int | None = None,
    entry_allowed: str = "Unknown",
    property_label: str | None = None,
    vendor_label: str | None = None,
    assignee_label: str | None = None,
) -> str:
    """Create a maintenance work order in Buildium (a write — confirm-gated).

    Use this for "open a work order for <property> — <issue> — assign <vendor>".
    Confirmation is enforced in code; you do NOT add a separate confirmation step.

    Buildium REQUIRES all of: ``property_id``, ``vendor_id``, and
    ``assigned_to_user_id``. Resolve these from the read tools FIRST
    (``buildium_list_properties`` for the property, ``buildium_search_vendors``
    for the vendor, ``buildium_list_staff`` for the assignee) — their results
    include the ids. If you are missing any of them, ASK the user rather than
    guessing. Always also pass the matching ``*_label`` (the human name/address)
    so the confirm card and your reply show names, never raw ids.

    Args:
        title: Short work-order title (e.g. "No hot water - 7C").
        description: What needs doing.
        property_id: Buildium property id (required).
        vendor_id: Buildium vendor id to assign (required).
        assigned_to_user_id: Buildium staff user id to own the task (required).
        priority: "Low" | "Normal" | "High" (default "Normal").
        unit_id: Optional Buildium unit id.
        entry_allowed: Entry permission ("Unknown" default; "Yes"/"No" if known).
        property_label: Property name/address to show the user (recommended).
        vendor_label: Vendor name to show the user (recommended).
        assignee_label: Staff member's name to show the user (recommended).

    Returns:
        On success: ``'Work order created in Buildium: "<title>".'`` (no raw id).
        On failure/decline: a short sanitized message.
    """
    return await _create_work_order_impl(
        title=title,
        description=description,
        property_id=property_id,
        vendor_id=vendor_id,
        assigned_to_user_id=assigned_to_user_id,
        priority=priority,
        unit_id=unit_id,
        entry_allowed=entry_allowed,
        property_label=property_label,
        vendor_label=vendor_label,
        assignee_label=assignee_label,
    )


async def _list_staff_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            users = await api.administration.list_users(usertypes=["Staff"], status="Active")
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list staff")

    if not users:
        return "No active staff users found in Buildium."

    lines = [f"{len(users)} active staff:", ""]
    for u in users:
        first = getattr(u, "first_name", None) or ""
        last = getattr(u, "last_name", None) or ""
        name = f"{first} {last}".strip() or getattr(u, "email", None) or "Unnamed user"
        # Surface the id so the agent can pass it as ``assigned_to_user_id`` on a
        # write — it must refer to the person by NAME to the user, never the id.
        lines.append(f"- {name} (id: {getattr(u, 'id', '?')})")
    return "\n".join(lines)


@function_tool
async def buildium_list_staff() -> str:
    """List active Buildium STAFF users — use this to assign a task or work order.

    Each line is ``- <Name> (id: <N>)``. Use the id as ``assigned_to_user_id``
    when creating a work order or task. Refer to the person by NAME in your reply
    to the user — never show the raw id.
    """
    return await _list_staff_impl()


# Valid to-do task statuses (mirrors agent/handlers/update_buildium_task.py).
_TASK_STATUSES = ("New", "InProgress", "Completed", "Deferred", "Closed")
_OPEN_TASK_STATUSES = OPEN_STATUSES


def _task_matches(task: Any, needle: str, *, location: str = "") -> bool:
    """Does this task match the search phrase?

    ALL words must appear, in any order, across the task's text AND its
    location — not the phrase verbatim. The whole-phrase version shipped in
    #853 and failed the case it was written for: asked about "the water heater
    task in Montgomery", the agent searched ``"Montgomery water heater"``,
    which is not a substring of "URGENT: water heater leaking into the closet",
    so the tool reported no match and stated it had "searched all 90 tasks" —
    authoritative and wrong, which is the exact failure mode this whole search
    was added to end.

    The model will fold the location into the phrase no matter what the
    docstring says, so the location belongs in the haystack rather than in an
    instruction the model is free to ignore.
    """
    words = [w for w in re.split(r"\W+", needle.lower()) if w]
    if not words:
        return True
    haystack = " ".join(
        [str(getattr(task, field, "") or "") for field in ("title", "description")] + [location]
    ).lower()
    return all(word in haystack for word in words)


def _task_status(task: Any) -> str:
    """A task's status, from the field the endpoint ACTUALLY populates.

    Live-verified across all 179 production rows: ``Status`` is NULL on every
    one and ``TaskStatus`` carries the value. Reading ``status or "New"``
    therefore called every task — completed ones included — open.
    """
    return str(getattr(task, "task_status", None) or getattr(task, "status", None) or "New")


async def _list_tasks_impl(search: str = "", property_name: str = "") -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    property_id: int | None = None
    locations: dict[int, str] = {}
    try:
        async with client:
            tasks = await api.tasks.list()
            if property_name.strip() or search.strip():
                # ONE bulk call, and only when filtering. It resolves the
                # property_name AND gives every task a location string for the
                # search haystack — without it, a search phrase carrying a
                # place name ("Montgomery water heater") can never match.
                properties = await api.rentals.list_properties()
                for p in properties:
                    pid = getattr(p, "id", None)
                    if pid is not None:
                        label = " ".join(
                            filter(
                                None,
                                [
                                    getattr(p, "name", None) or "",
                                    _format_address(getattr(p, "address", None)),
                                ],
                            )
                        )
                        locations[int(pid)] = label
            if property_name.strip():
                needle = property_name.strip().lower()
                matches = [pid for pid, label in locations.items() if needle in label.lower()]
                if not matches:
                    return f"I couldn't find a property matching '{property_name}'."
                property_id = matches[0]
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list tasks")

    def _location_of(task: Any) -> str:
        pid = getattr(getattr(task, "property", None), "id", None)
        return locations.get(int(pid), "") if pid is not None else ""

    open_tasks = [t for t in tasks if is_open(_task_status(t))]
    scope = "open task(s)"
    if property_id is not None:
        # Client-side: production silently IGNORES ?propertyids on /tasks and
        # returns the whole account (gotcha 24). Rows carry nested Property.Id.
        open_tasks = [
            t
            for t in open_tasks
            if getattr(getattr(t, "property", None), "id", None) == property_id
        ]
        scope = f"open task(s) at {property_name.strip()}"
    if search.strip():
        open_tasks = [
            t for t in open_tasks if _task_matches(t, search.strip(), location=_location_of(t))
        ]
        scope = f"open task(s) matching '{search.strip()}'" + (
            f" at {property_name.strip()}" if property_id is not None else ""
        )

    if not open_tasks:
        # An EMPTY filtered result is the one honest "no" this tool can give,
        # because the filter ran over EVERY open task rather than a page of
        # them. Say which set was searched so the answer can't be read as a
        # portfolio-wide absence when it wasn't one.
        return f"No {scope} — searched all {len(tasks)} tasks in Buildium."

    limit = 50
    shown = open_tasks[:limit]
    lines = [f"{len(open_tasks)} {scope}:", ""]
    for t in shown:
        title = getattr(t, "title", None) or "Untitled task"
        status = getattr(t, "status", None) or "New"
        priority = getattr(t, "priority", None)
        # Surface the id so the agent can pass it as ``task_id`` on a status
        # update; refer to the task by title to the user, never the id.
        priority_part = f" priority {priority}" if priority else ""
        lines.append(f"- {title} [{status}]{priority_part} (id: {getattr(t, 'id', '?')})")
    if len(open_tasks) > limit:
        lines.append("")
        # An explicit anti-absence warning. Battery v2 Q9: the agent saw a
        # truncated list, failed to find a task in it, and told the user the
        # task did not exist ANYWHERE — while task 39431 "URGENT: water heater
        # leaking into the closet" sat at priority Low. A truncated list is
        # never evidence of absence; re-call with `search` instead.
        lines.append(
            f"… and {len(open_tasks) - limit} more not shown. This list is TRUNCATED — "
            f"do NOT conclude a task is absent because it isn't here. "
            f"Re-call with search='<keyword>' to search all {len(tasks)} tasks."
        )
    return "\n".join(lines)


@function_tool
async def buildium_list_tasks(search: str = "", property_name: str = "") -> str:
    """List OPEN to-do tasks from Buildium, optionally filtered.

    Pass `search` to find a task by keyword — it matches title and description
    across EVERY open task, not just the page an unfiltered call shows. Use it
    for "is there a water heater task?", "any tasks about the roof?". Pass
    `property_name` to scope to one property; pass both to combine them.

    Only a filtered call that returns nothing is evidence that a task does not
    exist. An unfiltered list is truncated, so never conclude a task is absent
    because it isn't in one.

    Each line is `- <Title> [<Status>] priority <P> (id: <N>)`. Use the id as
    `task_id` when updating a task's status; refer to the task by title to the
    user, never the id.

    Args:
        search: Optional keyword matched against task title and description.
        property_name: Optional property name or address substring.
    """
    return await _list_tasks_impl(search, property_name)


def _rank_units_by_task_activity(tasks: list[Any], cutoff: datetime) -> list[tuple[int, int]]:
    """Group tasks CREATED on/after ``cutoff`` by unit → (unit_id, count),
    sorted by count desc then unit_id asc.

    Pure so the windowing + ranking are unit-testable without the API. Tasks
    with no unit or no created date are skipped. Created timestamps come from
    the /v1/tasks endpoint (the /workorders rows don't carry one — probe
    2026-07-13), so this reads tasks and filters client-side rather than
    trusting an unverified server-side date-filter param name (gotcha #15).
    """
    counts: dict[int, int] = {}
    for t in tasks:
        created = getattr(t, "created_date_time", None)
        unit_id = getattr(t, "unit_id", None)
        if created is None or unit_id is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created < cutoff:
            continue
        counts[unit_id] = counts.get(unit_id, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _format_unit_task_history(ranked: list[tuple[int, int]], days: int) -> str:
    """Render the unit ranking. Pure. Unit ids are Buildium internal ids —
    the agent can resolve one to its property/unit number on demand."""
    if not ranked:
        return (
            f"No maintenance activity tied to a unit in the last {days} days "
            "(or the tasks carry no created date / unit)."
        )
    limit = 15
    total = sum(c for _, c in ranked)
    lines = [
        f"Maintenance activity by unit — last {days} days "
        f"({total} item(s) across {len(ranked)} unit(s)):",
        "",
    ]
    for rank, (unit_id, count) in enumerate(ranked[:limit], start=1):
        lines.append(f"{rank}. Unit {unit_id} — {count} maintenance item(s)")
    if len(ranked) > limit:
        lines.append("")
        lines.append(f"… and {len(ranked) - limit} more unit(s) not shown.")
    return "\n".join(lines)


async def _maintenance_history_by_unit_impl(days: int) -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            tasks = await api.tasks.list()
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list maintenance tasks")

    window = max(1, days)
    cutoff = datetime.now(UTC) - timedelta(days=window)
    ranked = _rank_units_by_task_activity(list(tasks), cutoff)
    return _format_unit_task_history(ranked, window)


@function_tool
async def buildium_maintenance_history_by_unit(days: int = 90) -> str:
    """Rank units by how much maintenance activity they've had recently.

    Answers "which unit has the most maintenance history in the last N days?"
    / "which units are our biggest maintenance headaches?". Reads the Buildium
    tasks feed and groups items CREATED in the window by unit, most-active
    first (default 90 days).

    Returns Buildium unit ids with counts; if the user wants a unit's address
    or number, resolve it with a property/unit lookup tool. Note: work-order
    rows don't carry a created date, so this uses the tasks endpoint — the
    authoritative source for a maintenance item's opened date.
    """
    return await _maintenance_history_by_unit_impl(days)


async def _update_task_status_impl(
    task_id: int,
    status: str,
    task_label: str | None = None,
) -> str:
    """Update a to-do task's status in Buildium (a write).

    Mirrors the create writes' safety: confirm gate + idempotency claim +
    read-back verify, all via ``run_guarded``. This passes only the
    ``{"TaskStatus": status}`` delta; ``Tasks.update`` does the full
    read-merge-write PUT under the hood (Buildium has no partial task PATCH — a
    minimal body 422s, gotcha 20). The read-back verify reads ``TaskStatus`` off
    the re-GET (the tasks endpoint returns ``TaskStatus``, not ``Status``).
    """
    from datetime import timedelta

    if status not in _TASK_STATUSES:
        return f"Status must be one of {', '.join(_TASK_STATUSES)} — got {status!r}."

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    # ⚠ task_label is DELIBERATELY IGNORED — see _gate_label.
    label = await _gate_label(client, api, "task", task_id)
    details = f'Update {label} status to "{status}" in Buildium.'
    items = [f"Task: {label}", f"New status: {status}"]

    company_id = _current_company_id()
    key = derive_key(
        company_id,
        "buildium_update_task_status",
        {"task_id": task_id, "status": status},
    )

    async def _do_update() -> str:
        async with client:
            await api.tasks.update(task_id, fields={"TaskStatus": status})
        return f'Updated {label} status to "{status}" in Buildium.'

    async def _verify_updated(_result: str) -> bool:
        """Read the task back and confirm the new status stuck. READ-ONLY GETs,
        bounded retries for read-after-write lag."""
        import asyncio

        last_exc: Exception | None = None
        for attempt in range(_VERIFY_MAX_ATTEMPTS):
            try:
                async with client:
                    task = await api.tasks.get(task_id)
                # ⚠ ``GET /tasks/{id}`` returns the status under ``TaskStatus``
                # (→ ``.task_status``), NOT ``Status`` — prod-verified 2026-07-26
                # (``.status`` came back None while ``.task_status`` == "Completed").
                # Checking only ``.status`` made this verify ALWAYS fail, so every
                # successful status change wrongly returned ``blocked_needs_review``
                # ("I updated it but couldn't confirm"). Prefer task_status, fall
                # back to status for any shape that populates it.
                current = getattr(task, "task_status", None) or getattr(task, "status", None) or ""
                if task is not None and current.lower() == status.lower():
                    return True
            except BuildiumApiError as exc:
                if exc.status == 404:
                    return False
                last_exc = exc
            except Exception as exc:  # noqa: BLE001 — transient read failure
                last_exc = exc
            if attempt < _VERIFY_MAX_ATTEMPTS - 1:
                await asyncio.sleep(_VERIFY_BACKOFF_SECONDS * (attempt + 1))
        if last_exc is not None:
            raise last_exc
        return False

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="buildium_update_task_status",
            confirm={
                "action": "buildium_update_task_status",
                "title": "Update a task's status in Buildium?",
                "details": details,
                "items": items,
                # The ACTUAL Buildium PUT fields (approval-pipeline PR).
                "request_body": {"task_id": task_id, "TaskStatus": status},
            },
            execute=_do_update,
            verify=_verify_updated,
            # Short window: absorbs double-approval / retry / restart, but a
            # status set is idempotent and legitimately re-appliable later.
            window=timedelta(minutes=10),
        )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "update the task status")

    if guard.outcome == "not_approved":
        return "Okay, I didn't change the task status. Nothing was changed in Buildium."
    if guard.outcome == "blocked_store_unavailable":
        return (
            "I couldn't safely process this right now, so I did not change the task status — "
            "nothing was changed in Buildium. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        return (
            "I updated the task status in Buildium but couldn't confirm it went through, "
            "so I'm flagging it for review. Please check Buildium before retrying."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="change the task status", system="Buildium")
    if guard.result is not None:
        return str(guard.result)
    return f'Updated {label} status to "{status}" in Buildium.'


@function_tool
async def buildium_update_task_status(
    task_id: int,
    status: str,
    task_label: str | None = None,
) -> str:
    """Update a Buildium to-do task's status (a write — confirm-gated).

    Use for "mark <task> in progress / completed / closed". Confirmation is
    enforced in code; you do NOT add a separate confirmation step.

    Resolve ``task_id`` from ``buildium_list_tasks`` first (its results include
    ids). Always pass ``task_label`` (the task's title) so the confirm card and
    your reply show the title, never a raw id.

    Args:
        task_id: Buildium to-do task id (required).
        status: One of New, InProgress, Completed, Deferred, Closed.
        task_label: The task's title to show the user (recommended).

    Returns:
        On success: a short confirmation (no raw id). On decline/failure: a
        short sanitized message.
    """
    return await _update_task_status_impl(task_id=task_id, status=status, task_label=task_label)


async def _close_work_order_impl(
    work_order_id: int,
    work_order_label: str | None = None,
) -> str:
    """Close a Buildium work order — mark it Completed (a write).

    A work order's status is DERIVED from its backing to-do task: the
    ``/workorders`` update model (``WorkOrderPutMessage``) has NO status field,
    so ``PUT /workorders/{id}`` with a ``Status`` silently no-ops (200,
    unchanged — prod-confirmed 2026-07-25, gotcha 20). To close, resolve the
    WO's ``Task.Id`` and set that task's ``TaskStatus='Completed'`` via the
    read-merge-write ``Tasks.update``; the WO ``Status`` then follows.

    Same safety as the other writes: confirm gate + idempotency claim +
    read-back verify (on the WO, which is where the visible status lives), all
    via ``run_guarded`` — plus a Manager/Admin role floor (fail-closed):
    closing a WO is a ONE-WAY latch (no API reopen, gotcha 20), so only Manager+
    may do it.
    """
    from datetime import timedelta

    denied = await _require_manager_role(
        "closing a work order", alt="close it in Buildium directly"
    )
    if denied:
        return denied

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    # Resolve the backing to-do task id — the WO's status lives on the task.
    try:
        async with client:
            wo = await api.work_orders.get(int(work_order_id))
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "look up the work order")

    # ⚠ work_order_label is DELIBERATELY IGNORED (see _gate_label). It used to
    # win over `wo.title` — the record was already in hand and the card showed
    # the model's string instead. No extra call is needed to be correct here.
    label = (wo.title if wo else None) or f"work order {work_order_id}"
    task_id = wo.task.id if (wo and wo.task) else None
    if task_id is None:
        return (
            f"I couldn't close {label} — Buildium didn't return a backing task for it, and a "
            "work order's status is set through its task. Please close it in Buildium directly."
        )
    if (getattr(wo, "status", None) or "").strip().lower() == "completed":
        return f"{label} is already Completed in Buildium — nothing to do."

    details = f"Close {label} (mark it Completed) in Buildium."
    items = [f"Work order: {label}", "New status: Completed"]

    company_id = _current_company_id()
    key = derive_key(
        company_id,
        "buildium_close_work_order",
        {"work_order_id": int(work_order_id), "task_id": task_id},
    )

    async def _do_close() -> str:
        async with client:
            await api.tasks.update(task_id, fields={"TaskStatus": "Completed"})
        return f"Closed {label} in Buildium (marked Completed)."

    async def _verify_closed(_result: str) -> bool:
        """RE-GET the work order and confirm its Status is Completed. READ-ONLY,
        bounded retries for read-after-write lag."""
        import asyncio

        last_exc: Exception | None = None
        for attempt in range(_VERIFY_MAX_ATTEMPTS):
            try:
                async with client:
                    re = await api.work_orders.get(int(work_order_id))
                if re is not None and (getattr(re, "status", None) or "").strip().lower() == (
                    "completed"
                ):
                    return True
            except BuildiumApiError as exc:
                if exc.status == 404:
                    return False
                last_exc = exc
            except Exception as exc:  # noqa: BLE001 — transient read failure
                last_exc = exc
            if attempt < _VERIFY_MAX_ATTEMPTS - 1:
                await asyncio.sleep(_VERIFY_BACKOFF_SECONDS * (attempt + 1))
        if last_exc is not None:
            raise last_exc
        return False

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="buildium_close_work_order",
            confirm={
                "action": "buildium_close_work_order",
                "title": "Close this work order in Buildium?",
                "details": details,
                "items": items,
                # The ACTUAL write: the backing task's status flip.
                "request_body": {
                    "work_order_id": int(work_order_id),
                    "task_id": task_id,
                    "TaskStatus": "Completed",
                },
            },
            execute=_do_close,
            verify=_verify_closed,
            window=timedelta(minutes=10),
        )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "close the work order")

    if guard.outcome == "not_approved":
        return "Okay, I didn't close the work order. Nothing was changed in Buildium."
    if guard.outcome == "blocked_store_unavailable":
        return (
            "I couldn't safely process this right now, so I did not close the work order — "
            "nothing was changed in Buildium. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        return (
            "I marked the work order Completed in Buildium but couldn't confirm it went "
            "through, so I'm flagging it for review. Please check Buildium before retrying."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="close the work order", system="Buildium")
    if guard.result is not None:
        return str(guard.result)
    return f"Closed {label} in Buildium (marked Completed)."


@function_tool
async def buildium_close_work_order(
    work_order_id: int,
    work_order_label: str | None = None,
) -> str:
    """Close a Buildium work order — mark it Completed (a write — confirm-gated).

    Use for "close / complete / mark done work order <X>". Confirmation is
    enforced in code; you do NOT add a separate confirmation step.

    Resolve ``work_order_id`` from ``buildium_list_open_work_orders`` (its
    results include ids). Always pass ``work_order_label`` (the WO title) so the
    confirm card and your reply show the title, never a raw id.

    A work order's status is set through its backing task, so this closes the WO
    by completing that task — calling ``buildium_update_task_status`` with the
    *work-order* id would NOT work. ⚠ There is no API path to REOPEN a completed
    work order (reopening needs the Buildium UI), so only use this when the work
    is genuinely done.

    Args:
        work_order_id: Buildium work order id (required).
        work_order_label: The WO's title to show the user (recommended).

    Returns:
        On success: a short confirmation (no raw id). On decline/failure: a
        short sanitized message.
    """
    return await _close_work_order_impl(
        work_order_id=work_order_id, work_order_label=work_order_label
    )


_WO_PRIORITIES = ("Low", "Normal", "High")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


async def _update_work_order_impl(
    work_order_id: int,
    *,
    vendor_id: int | None = None,
    vendor_label: str | None = None,
    priority: str | None = None,
    due_date: str | None = None,
    work_order_label: str | None = None,
) -> str:
    """Update a Buildium work order's vendor / priority / due date (a write).

    ⚠ The fields split across TWO Buildium endpoints (gotcha 20, prod-probed):
    ``VendorId`` is the only meaningfully-mutable field on
    ``PUT /workorders/{id}``; ``Priority`` and ``DueDate`` are task-derived and
    silently no-op there, so they go through the backing task
    (``PUT /tasks/todorequests/{Task.Id}``). This tool routes by field —
    vendor→WO put, priority/due-date→task put — so a caller never gets a
    200-but-lie. Confirm gate + idempotency claim + read-back verify (on the WO,
    where all three surface) via ``run_guarded``.
    """
    from datetime import timedelta

    if vendor_id is None and priority is None and due_date is None:
        return "Tell me what to change — a vendor, a priority, or a due date."
    if priority is not None and priority not in _WO_PRIORITIES:
        return f"Priority must be one of {', '.join(_WO_PRIORITIES)} — got {priority!r}."
    if due_date is not None and not _DATE_RE.match(due_date):
        return f"Due date must be YYYY-MM-DD — got {due_date!r}."

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    try:
        async with client:
            wo = await api.work_orders.get(int(work_order_id))
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "look up the work order")

    # ⚠ work_order_label ignored — the record is already in hand.
    label = (wo.title if wo else None) or f"work order {work_order_id}"
    task_id = wo.task.id if (wo and wo.task) else None
    needs_task = priority is not None or due_date is not None
    if needs_task and task_id is None:
        return (
            f"I couldn't change the priority/due date on {label} — Buildium didn't return a "
            "backing task for it (those live on the task). Please update it in Buildium directly."
        )

    items = [f"Work order: {label}"]
    if vendor_id is not None:
        items.append(f"Reassign to vendor: {await _gate_label(client, api, 'vendor', vendor_id)}")
    if priority is not None:
        items.append(f"Priority: {priority}")
    if due_date is not None:
        items.append(f"Due date: {due_date}")
    details = f"Update {label} in Buildium."

    company_id = _current_company_id()
    key = derive_key(
        company_id,
        "buildium_update_work_order",
        {
            "work_order_id": int(work_order_id),
            "vendor_id": vendor_id,
            "priority": priority,
            "due_date": due_date,
        },
    )

    async def _do_update() -> str:
        async with client:
            if vendor_id is not None:
                await api.work_orders.reassign_vendor(int(work_order_id), int(vendor_id))
            if needs_task:
                assert task_id is not None  # guaranteed by the needs_task guard above
                fields: dict[str, Any] = {}
                if priority is not None:
                    fields["Priority"] = priority
                if due_date is not None:
                    fields["DueDate"] = due_date
                await api.tasks.update(task_id, fields=fields)
        return f"Updated {label} in Buildium."

    async def _verify_updated(_result: str) -> bool:
        """RE-GET the WO and confirm each requested change landed. READ-ONLY,
        bounded retries for read-after-write lag."""
        import asyncio

        last_exc: Exception | None = None
        for attempt in range(_VERIFY_MAX_ATTEMPTS):
            try:
                async with client:
                    re_wo = await api.work_orders.get(int(work_order_id))
            except BuildiumApiError as exc:
                if exc.status == 404:
                    return False
                last_exc = exc
            except Exception as exc:  # noqa: BLE001 — transient read failure
                last_exc = exc
            else:
                ok = True
                if vendor_id is not None:
                    # Vendor lives on the WO itself (PUT /workorders) — top-level.
                    ok = ok and getattr(re_wo, "vendor_id", None) == int(vendor_id)
                if priority is not None:
                    # ⚠ Priority is task-derived: after Tasks.update the change
                    # lands on the NESTED WorkOrder.Task.Priority; the WO's
                    # top-level Priority stays frozen at creation (prod dogfood
                    # 2026-07-26, gotcha 20). Verifying the top-level always
                    # false-negatived. Prefer the nested value, fall back to top.
                    nested_task = getattr(re_wo, "task", None)
                    seen_priority = (
                        getattr(nested_task, "priority", None)
                        or getattr(re_wo, "priority", None)
                        or ""
                    )
                    ok = ok and seen_priority.lower() == priority.lower()
                if due_date is not None:
                    # DueDate is also task-derived (gotcha 20). Accept it on the
                    # top-level OR the nested Task (``effective_due_date`` folds
                    # both) so a frozen top-level can't false-negative a real
                    # change the way priority did.
                    seen_due = getattr(re_wo, "effective_due_date", None) or getattr(
                        re_wo, "due_date", None
                    )
                    ok = ok and str(seen_due or "") == due_date
                if ok:
                    return True
            if attempt < _VERIFY_MAX_ATTEMPTS - 1:
                await asyncio.sleep(_VERIFY_BACKOFF_SECONDS * (attempt + 1))
        if last_exc is not None:
            raise last_exc
        return False

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="buildium_update_work_order",
            confirm={
                "action": "buildium_update_work_order",
                "title": "Update this work order in Buildium?",
                "details": details,
                "items": items,
                "request_body": {
                    "work_order_id": int(work_order_id),
                    "VendorId": vendor_id,
                    "Priority": priority,
                    "DueDate": due_date,
                },
            },
            execute=_do_update,
            verify=_verify_updated,
            window=timedelta(minutes=10),
        )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "update the work order")

    if guard.outcome == "not_approved":
        return "Okay, I didn't change the work order. Nothing was changed in Buildium."
    if guard.outcome == "blocked_store_unavailable":
        return (
            "I couldn't safely process this right now, so I did not change the work order — "
            "nothing was changed in Buildium. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        return (
            "I updated the work order in Buildium but couldn't confirm every change went "
            "through, so I'm flagging it for review. Please check Buildium before retrying."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="change the work order", system="Buildium")
    if guard.result is not None:
        return str(guard.result)
    return f"Updated {label} in Buildium."


@function_tool
async def buildium_update_work_order(
    work_order_id: int,
    vendor_id: int | None = None,
    vendor_label: str | None = None,
    priority: str | None = None,
    due_date: str | None = None,
    work_order_label: str | None = None,
) -> str:
    """Update a work order — reassign vendor, change priority or due date (a write — confirm-gated).

    Use for "reassign WO <X> to <vendor>", "set work order <X> to high priority",
    "push the due date on <X> to <date>". Confirmation is enforced in code; you
    do NOT add a separate confirmation step. To CLOSE a work order, use
    ``buildium_close_work_order`` instead (status is set through the task).

    Resolve ids first: ``work_order_id`` from ``buildium_list_open_work_orders``,
    and ``vendor_id`` from ``buildium_search_vendors`` when reassigning. Pass the
    matching ``work_order_label`` / ``vendor_label`` so the confirm card shows
    names, not numbers.

    Args:
        work_order_id: Buildium work order id (required).
        vendor_id: New vendor id to reassign to (optional).
        vendor_label: The new vendor's name to show the user (recommended with vendor_id).
        priority: One of Low, Normal, High (optional).
        due_date: New due date as YYYY-MM-DD (optional).
        work_order_label: The WO's title to show the user (recommended).

    Returns:
        On success: a short confirmation (no raw id). On decline/failure: a
        short sanitized message.
    """
    return await _update_work_order_impl(
        work_order_id=work_order_id,
        vendor_id=vendor_id,
        vendor_label=vendor_label,
        priority=priority,
        due_date=due_date,
        work_order_label=work_order_label,
    )


# Buildium lease-payment methods (CreatePaymentRequest.PaymentMethod enum).
_PAYMENT_METHODS = (
    "Cash",
    "Check",
    "MoneyOrder",
    "CashiersCheck",
    "DirectDeposit",
    "CreditCard",
    "ElectronicPayment",
    "RetailCash",
)


async def _post_charge_impl(
    lease_id: int,
    amount: float,
    gl_account_id: int,
    *,
    date: str,
    memo: str | None = None,
    lease_label: str | None = None,
    gl_account_label: str | None = None,
) -> str:
    """Post a single-line charge to a lease ledger (a write).

    ⚠ Financial. Charges are editable via PUT but NOT deletable (gotcha 21), so
    this runs behind the confirm gate + idempotency claim (a double-approval /
    retry cannot double-post) + a Manager/Admin role floor (fail-closed) — a
    non-deletable financial write only Manager+ may post. No read-back verify:
    the POST returns the created transaction, which is the confirmation.
    """
    from datetime import timedelta

    if amount is None or amount <= 0:
        return "A charge amount must be a positive number of dollars."
    if not _DATE_RE.match(date or ""):
        return f"The charge date must be YYYY-MM-DD — got {date!r}."

    denied = await _require_manager_role("posting a charge", alt="post it in Buildium directly")
    if denied:
        return denied

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    # ⚠ *_label params are accepted for back-compat and DELIBERATELY IGNORED
    # — the card must never render the model's belief (see _gate_label).
    lease_disp = await _gate_label(client, api, "lease", lease_id)
    gl_disp = await _gate_label(client, api, "gl_account", gl_account_id)
    line_memo = memo or "Charge posted via Occupella"
    details = f"Post a {_format_money(amount)} charge to {lease_disp} ({gl_disp}) in Buildium."
    items = [f"Lease: {lease_disp}", f"Amount: {_format_money(amount)}", f"GL account: {gl_disp}"]
    if memo:
        items.append(f"Memo: {memo}")

    company_id = _current_company_id()
    key = derive_key(
        company_id,
        "buildium_post_charge",
        {"lease_id": int(lease_id), "gl": int(gl_account_id), "amount": amount, "date": date},
    )

    async def _do_charge() -> str:
        async with client:
            await api.leases.post_charge(
                int(lease_id),
                date=date,
                lines=[{"GlAccountId": int(gl_account_id), "Amount": amount, "Memo": line_memo}],
                memo=memo,
            )
        return f"Posted a {_format_money(amount)} charge to {lease_disp} in Buildium."

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="buildium_post_charge",
            confirm={
                "action": "buildium_post_charge",
                "title": "Post this charge in Buildium?",
                "details": details,
                "items": items,
                "request_body": {
                    "lease_id": int(lease_id),
                    "Date": date,
                    "Lines": [{"GlAccountId": int(gl_account_id), "Amount": amount}],
                },
            },
            execute=_do_charge,
            window=timedelta(minutes=10),
        )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "post the charge")

    if guard.outcome == "not_approved":
        return "Okay, I didn't post the charge. Nothing was changed in Buildium."
    if guard.outcome == "blocked_store_unavailable":
        return (
            "I couldn't safely process this right now, so I did not post the charge — "
            "nothing was changed in Buildium. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        return (
            "I tried to post the charge but couldn't confirm it went through, so I'm flagging "
            "it for review. Please check the lease ledger in Buildium before retrying."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="post the charge", system="Buildium")
    if guard.result is not None:
        return str(guard.result)
    return f"Posted a {_format_money(amount)} charge to {lease_disp} in Buildium."


@function_tool
async def buildium_post_charge(
    lease_id: int,
    amount: float,
    gl_account_id: int,
    date: str,
    memo: str | None = None,
    lease_label: str | None = None,
    gl_account_label: str | None = None,
) -> str:
    """Post a charge to a lease ledger — e.g. a late fee or a rent charge (a write — confirm-gated).

    ⚠ Financial. Confirmation is enforced in code; you do NOT add a separate
    confirmation step. Charges can be EDITED but not deleted, so get the amount
    and GL account right.

    ⚠ SOME CHARGES ARE UNLAWFUL, and this is the one that has actually been
    measured. A pet fee or pet deposit for an ASSISTANCE ANIMAL — an emotional
    support animal or a service animal — is prohibited under the Fair Housing
    Act: an assistance animal is not a pet, and no pet fee, pet deposit or pet
    rent may be charged for one. Refuse that charge and say why. You MAY still
    bill for actual damage the animal caused, which is a damage charge and not
    a pet fee. The same rule applies to any fee whose reason is a protected
    class (race, colour, national origin, religion, sex, familial status,
    disability): do not post it, whoever asked.

    Resolve ids first: ``lease_id`` from ``buildium_get_lease`` /
    ``buildium_list_active_leases``, and ``gl_account_id`` from
    ``buildium_list_gl_accounts`` (use an INCOME account for a charge). Pass
    ``lease_label`` (tenant/unit) and ``gl_account_label`` so the confirm card
    shows names, not ids.

    Args:
        lease_id: Buildium lease id (required).
        amount: Charge amount in dollars, positive (required).
        gl_account_id: Income GL account id to charge against (required).
        date: Charge date as YYYY-MM-DD (required).
        memo: Optional line memo (e.g. "August late fee").
        lease_label: Tenant/unit label to show the user (recommended).
        gl_account_label: GL account name to show the user (recommended).

    Returns:
        On success: a short confirmation (no raw id). On decline/failure: a
        short sanitized message.
    """
    return await _post_charge_impl(
        lease_id=lease_id,
        amount=amount,
        gl_account_id=gl_account_id,
        date=date,
        memo=memo,
        lease_label=lease_label,
        gl_account_label=gl_account_label,
    )


async def _record_payment_impl(
    lease_id: int,
    amount: float,
    gl_account_id: int,
    *,
    date: str,
    payment_method: str = "Check",
    memo: str | None = None,
    lease_label: str | None = None,
) -> str:
    """Record a resident payment against a lease (a write).

    ⚠⚠ Financial AND IRREVERSIBLE — a recorded payment is IMMUTABLE (no delete,
    no edit; gotcha 21). LIVE, confirm-gated + a Manager-or-Admin role floor
    (fail-closed). Idempotency claim; no read-back verify (the POST returns the
    created transaction). Note the casing: payment lines use ``GLAccountId``
    (capital-L), unlike charge lines (``GlAccountId``).
    """
    from datetime import timedelta

    if amount is None or amount <= 0:
        return "A payment amount must be a positive number of dollars."
    if not _DATE_RE.match(date or ""):
        return f"The payment date must be YYYY-MM-DD — got {date!r}."
    if payment_method not in _PAYMENT_METHODS:
        return (
            f"Payment method must be one of {', '.join(_PAYMENT_METHODS)} — got {payment_method!r}."
        )

    # Role floor for the one IRREVERSIBLE financial write: only Manager+ may
    # record a payment; a Member cannot. Fail closed — if the role can't be
    # verified, refuse rather than risk a permanent, unrecoverable write.
    from helixis.api.roles import MANAGER, role_at_least

    role = await _caller_role()
    if role is None or not role_at_least(role, MANAGER):
        return (
            "Recording a payment needs a Manager or Admin on this workspace, so I "
            "didn't record anything. Ask a teammate with that role, or record the "
            "payment in Buildium directly."
        )

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    # ⚠ *_label params are accepted for back-compat and DELIBERATELY IGNORED
    # — the card must never render the model's belief (see _gate_label).
    lease_disp = await _gate_label(client, api, "lease", lease_id)
    details = (
        f"Record a {_format_money(amount)} {payment_method} payment on {lease_disp} in Buildium. "
        "This CANNOT be undone."
    )
    # ⚠ THE LEDGER ACCOUNT IS ON THE CARD. It decides where the money lands
    # and the approver cannot check it against anything if it isn't shown —
    # the charge gate beside this one has always shown it, which is what makes
    # the omission a plain inconsistency rather than a judgement call. It
    # matters more here, not less: a charge can be edited afterwards and a
    # payment cannot (gotcha 21).
    #
    # Resolved through ``_gate_label`` like every other id on this card, so the
    # gate renders the RECORD's name and never the model's belief about it
    # (the reason *_label params are accepted and ignored above).
    gl_disp = await _gate_label(client, api, "gl_account", gl_account_id)
    items = [
        f"Lease: {lease_disp}",
        f"Amount: {_format_money(amount)}",
        f"Method: {payment_method}",
        f"GL account: {gl_disp}",
        "⚠ Payments are permanent — they cannot be edited or deleted.",
    ]

    company_id = _current_company_id()
    key = derive_key(
        company_id,
        "buildium_record_payment",
        {"lease_id": int(lease_id), "gl": int(gl_account_id), "amount": amount, "date": date},
    )

    async def _do_payment() -> str:
        async with client:
            await api.leases.record_payment(
                int(lease_id),
                date=date,
                payment_method=payment_method,
                lines=[{"GLAccountId": int(gl_account_id), "Amount": amount}],
                send_email_receipt=False,
                memo=memo,
            )
        return f"Recorded a {_format_money(amount)} payment on {lease_disp} in Buildium."

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="buildium_record_payment",
            confirm={
                "action": "buildium_record_payment",
                "title": "Record this payment in Buildium? (permanent)",
                "details": details,
                "items": items,
                "request_body": {
                    "lease_id": int(lease_id),
                    "Date": date,
                    "PaymentMethod": payment_method,
                    "Lines": [{"GLAccountId": int(gl_account_id), "Amount": amount}],
                },
            },
            execute=_do_payment,
            window=timedelta(minutes=10),
        )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "record the payment")

    if guard.outcome == "not_approved":
        return "Okay, I didn't record the payment. Nothing was changed in Buildium."
    if guard.outcome == "blocked_store_unavailable":
        return (
            "I couldn't safely process this right now, so I did not record the payment — "
            "nothing was changed in Buildium. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        return (
            "I tried to record the payment but couldn't confirm it went through. Because "
            "payments are permanent, please check the lease ledger in Buildium before retrying."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="record the payment", system="Buildium")
    if guard.result is not None:
        return str(guard.result)
    return f"Recorded a {_format_money(amount)} payment on {lease_disp} in Buildium."


@function_tool
async def buildium_record_payment(
    lease_id: int,
    amount: float,
    gl_account_id: int,
    date: str,
    payment_method: str = "Check",
    memo: str | None = None,
    lease_label: str | None = None,
) -> str:
    """Record a resident payment on a lease (a write — confirm-gated, PERMANENT).

    ⚠⚠ Financial and IRREVERSIBLE — a recorded payment cannot be edited or
    deleted. Confirmation is enforced in code. Resolve ``lease_id`` and an
    ``gl_account_id`` (the cash/undeposited-funds account) first; pass
    ``lease_label``. ``payment_method`` is one of Cash, Check, MoneyOrder,
    CashiersCheck, DirectDeposit, CreditCard, ElectronicPayment, RetailCash.

    LIVE (2026-07-26): confirm-gated AND floored to Manager-or-Admin (fail
    closed — a Member, or any caller whose role can't be verified, is refused).
    A payment is immutable, so this carries the strongest guard of the write set.
    """
    return await _record_payment_impl(
        lease_id=lease_id,
        amount=amount,
        gl_account_id=gl_account_id,
        date=date,
        payment_method=payment_method,
        memo=memo,
        lease_label=lease_label,
    )


# Buildium lease types (CreateLeaseRenewalRequest.LeaseType).
_LEASE_TYPES = ("Fixed", "FixedWithRollover", "AtWill")


async def _renew_lease_impl(
    lease_id: int,
    lease_type: str,
    rent_amount: float,
    gl_account_id: int,
    next_due_date: str,
    *,
    cycle: str = "Monthly",
    lease_label: str | None = None,
) -> str:
    """Renew a lease (a write — DARK: field floor now confirmed, but IRREVERSIBLE).

    Kept dark until pilot NOT for contract uncertainty — the field floor is
    live-confirmed (prod-probed 2026-07-26, lease 99797): required
    ``{LeaseType, SendWelcomeEmail, Rent:{Cycle, Charges:[{Amount, GlAccountId
    (lower-l), NextDueDate}]}}``; ``LeaseFromDate``/``LeaseToDate`` are NOT
    required. It stays dark because a renewal creates a real lease term with NO
    clean undo (unlike a move-out) — the hardest gate, dark-until-pilot like the
    immutable payment write. Confirm gate + idempotency claim; no read-back verify.
    """
    from datetime import timedelta

    if lease_type not in _LEASE_TYPES:
        return f"Lease type must be one of {', '.join(_LEASE_TYPES)} — got {lease_type!r}."
    if rent_amount is None or rent_amount <= 0:
        return "The renewal rent must be a positive number of dollars."
    if not _DATE_RE.match(next_due_date or ""):
        return f"The next-due date must be YYYY-MM-DD — got {next_due_date!r}."

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    # ⚠ *_label params are accepted for back-compat and DELIBERATELY IGNORED
    # — the card must never render the model's belief (see _gate_label).
    lease_disp = await _gate_label(client, api, "lease", lease_id)
    details = (
        f"Renew {lease_disp} as a {lease_type} lease at {_format_money(rent_amount)}/{cycle} "
        f"in Buildium, next due {next_due_date}."
    )
    items = [
        f"Lease: {lease_disp}",
        f"Type: {lease_type}",
        f"Rent: {_format_money(rent_amount)} / {cycle}",
        f"Next due: {next_due_date}",
    ]
    company_id = _current_company_id()
    key = derive_key(
        company_id,
        "buildium_renew_lease",
        {"lease_id": int(lease_id), "amount": rent_amount, "next_due": next_due_date},
    )

    async def _do_renew() -> str:
        async with client:
            await api.leases.renew_lease(
                int(lease_id),
                lease_type=lease_type,
                rent={
                    "Cycle": cycle,
                    "Charges": [
                        {
                            "Amount": rent_amount,
                            "GlAccountId": int(gl_account_id),
                            "NextDueDate": next_due_date,
                        }
                    ],
                },
                send_welcome_email=False,
            )
        return f"Renewed {lease_disp} in Buildium."

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="buildium_renew_lease",
            confirm={
                "action": "buildium_renew_lease",
                "title": "Renew this lease in Buildium?",
                "details": details,
                "items": items,
                "request_body": {
                    "lease_id": int(lease_id),
                    "LeaseType": lease_type,
                    "Rent": {"Cycle": cycle, "Amount": rent_amount},
                },
            },
            execute=_do_renew,
            window=timedelta(minutes=10),
        )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "renew the lease")

    if guard.outcome == "not_approved":
        return "Okay, I didn't renew the lease. Nothing was changed in Buildium."
    if guard.outcome in ("blocked_store_unavailable", "blocked_needs_review"):
        return (
            "I couldn't safely complete the renewal, so nothing was changed in Buildium. "
            "Please check the lease in Buildium before retrying."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="renew the lease", system="Buildium")
    if guard.result is not None:
        return str(guard.result)
    return f"Renewed {lease_disp} in Buildium."


@function_tool
async def buildium_renew_lease(
    lease_id: int,
    lease_type: str,
    rent_amount: float,
    gl_account_id: int,
    next_due_date: str,
    cycle: str = "Monthly",
    lease_label: str | None = None,
) -> str:
    """Renew a lease with a new term + rent (a write — confirm-gated).

    Resolve ``lease_id`` and an income ``gl_account_id`` first; pass
    ``lease_label``. ``lease_type`` is Fixed / FixedWithRollover / AtWill;
    ``next_due_date`` is YYYY-MM-DD.

    NOTE: registered DARK (not offered to the agent) until pilot — the field
    floor was live-probed 2026-07-26, but a renewal creates a real lease term
    with NO clean undo (gotcha 22).
    """
    return await _renew_lease_impl(
        lease_id,
        lease_type,
        rent_amount,
        gl_account_id,
        next_due_date,
        cycle=cycle,
        lease_label=lease_label,
    )


async def _gate_label(
    client: Any, api: Any, kind: str, entity_id: Any, *, lease: Any = None
) -> str:
    """Display text for a confirm card, derived from the RECORD — never the model.

    ⚠ The gate is the human's last line of defence, and until 2026-08-24 it
    rendered the model's BELIEF. Every confirm-gated write took both an id
    (what executes) and an optional model-supplied ``*_label`` (what the human
    reads), and nothing checked they agreed::

        tenant_disp = tenant_label or f"tenant {tenant_id}"

    So a model that believed "Marcus Whitfield, Phoenix" could pass that string
    beside Elena Vasquez's ``tenant_id``: the card said Marcus, the write hit
    Elena. The Whitfield investigation ruled out the data (75 leases, 0
    disagreements) and the tool (``_resolve_unit`` returned all 50 candidates);
    the model held both and collapsed them anyway. Gotchas 25/32/34 each end
    with *only a structural stop works* after prose failed four times. This is
    the structural stop: the card is made INCAPABLE of showing something the
    record does not say.

    Two rules, both load-bearing:

    * **The model's label is never rendered.** Not preferred-over, not
      verified-against — not rendered. Verifying would need a matching
      heuristic, and a heuristic is one more thing that can be wrong.
    * **Every failure path falls back to the ID form**, never to the supplied
      string. An unfamiliar ``tenant 4410`` is checkable against Buildium; a
      confidently wrong name is deceptive. Honest-but-terse beats friendly-but-
      false, and the fallback is where a "prefer the label when the fetch
      fails" instinct would quietly reintroduce the whole bug.

    Only four getters exist today (leases / tasks / vendors / work_orders), so
    the rest resolve to the ID form. That is a friendliness gap, NOT a safety
    gap — adding a getter later improves the text, never the guarantee.
    """
    fallback = f"{kind.replace('_', ' ')} {entity_id}"
    if entity_id is None:
        return fallback
    try:
        if kind == "lease":
            if lease is None:
                async with client:
                    lease = await api.leases.get(int(entity_id))
            tenants = getattr(lease, "current_tenants", None) or []
            names = ", ".join(_tenant_full_name(t) for t in tenants) or "no current tenant"
            return f"{names} at {_lease_unit_label(lease)}"
        if kind == "tenant":
            # A tenant is only meaningful in its lease context, and reading the
            # id off the LEASE is what makes the Whitfield case impossible: the
            # name shown is whichever tenant the write actually targets.
            if lease is None:
                return fallback
            for t in getattr(lease, "current_tenants", None) or []:
                if str(getattr(t, "id", "")) == str(entity_id):
                    return _tenant_full_name(t)
            return fallback
        if kind == "task":
            async with client:
                task = await api.tasks.get(int(entity_id))
            title = getattr(task, "title", None)
            # Title only, no id: the identifier-hygiene rule (gotcha 10) applies
            # to the reply prose this label also feeds. The id remains the
            # FALLBACK, so an unresolvable record is still checkable.
            return str(title) if title else fallback
        if kind == "work_order":
            async with client:
                wo = await api.work_orders.get(int(entity_id))
            title = getattr(getattr(wo, "task", None), "title", None) or getattr(wo, "title", None)
            return str(title) if title else fallback
        if kind == "vendor":
            async with client:
                return _vendor_label(await api.vendors.get(int(entity_id)))
    except Exception:  # noqa: BLE001 — a label must never fail a write path
        logger.warning("gate label lookup failed for %s %s; using the id", kind, entity_id)
    return fallback


def _move_out_accepted(tenant_disp: str, lease_disp: str) -> str:
    """What this tool actually knows after a 2xx: that Buildium ACCEPTED it.

    ⚠ THIS USED TO SAY "Recorded a move-out … in Buildium." and that sentence
    was live-disproven. Gotcha 70, 2026-08-31: the call returned 2xx, the
    idempotency ledger logged ``succeeded``, and lease 115938 still read
    ``LeaseStatus: Active`` with ``MoveOutData: []`` — no move-out existed. The
    only observable effect was ``LeaseToDate`` moving. The agent then told the
    user the tenant was "flagged as a former tenant effective today… no further
    action needed": three claims, all contradicted by the account.

    ``scripts/write_verify.py`` fixed the HARNESS half of that. This is the
    TOOL half, and it is the half the model reads. A tool that asserts
    completion it cannot know hands the model a false premise, and the model
    is then blamed for repeating it — which is exactly what golden case 344
    catches. Removing the false claim is the structural fix; steering the model
    to distrust its own tools with prose is the one this repo has watched fail
    five times (gotchas 25/32/34/37/50).

    ⚠ The wording is deliberately an ACCEPTANCE, not a completion, and it names
    the read-back as the next step. It must never contain "recorded",
    "successfully", or "former tenant" — 344's regexes are affirmation-shaped
    and a tool string carrying one would put the phrase in the transcript for
    the model to echo.
    """
    return (
        f"Buildium accepted a move-out for {tenant_disp} on {lease_disp}. "
        "That is the API's acknowledgement, NOT confirmation it took effect — "
        "a 2xx here has previously come back with no move-out on the lease. "
        "Read the lease back before telling anyone it is done, and if the "
        "lease still shows no move-out, say so plainly rather than reporting "
        "success. Do NOT re-send the write to 'make it take' — that is how a "
        "record ends up duplicated."
    )


async def _record_move_out_impl(
    lease_id: int,
    tenant_id: int,
    move_out_date: str,
    *,
    tenant_label: str | None = None,
    lease_label: str | None = None,
) -> str:
    """Record a tenant move-out (a write — LIVE, confirm-gated).

    Prod-probed 2026-07-26 (lease 99797): ``POST /leases/{id}/moveouts`` body
    ``{TenantId, MoveOutDate}`` (NoticeGivenDate optional), reversed cleanly via
    ``DELETE /leases/{id}/moveouts/{tenantId}`` (verified empty on re-GET). A
    move-out is REVERSIBLE at the API, unlike a WO close or a payment — which
    is why it ships with a confirm gate + idempotency claim rather than the
    Manager+ floor the three hard-to-undo writes carry (gotcha 21).

    ⚠ **That reversibility is not reachable from inside Occupella.**
    ``Leases.undo_move_out`` exists on the endpoint class and has ZERO
    callers — no agent tool, no API route, no test. So the property that
    justifies the lighter guard is true of Buildium and false of this product:
    a manager who approves a move-out by mistake has no path back except the
    Buildium UI. Gotcha 48's shape — a justification whose evidence does not
    hold where it is being cited — pointed at a safety gate rather than an
    eval gate.

    Measured 2026-08-29: this tool has been called ONCE in production across
    529 runs, so the gap is real but not yet costly. Registering
    ``buildium_undo_move_out`` is the honest fix and it is deliberately NOT
    done here: a new write on the live surface changes what all 32 negative-
    safety golden cases must forbid, which is live-LLM eval spend and risk two
    days from release. Brandon's call — the note is the deliverable, not a
    silent omission.
    """
    from datetime import timedelta

    if not _DATE_RE.match(move_out_date or ""):
        return f"The move-out date must be YYYY-MM-DD — got {move_out_date!r}."

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    # ⚠ tenant_label / lease_label are accepted for backward compatibility and
    # DELIBERATELY IGNORED — see _gate_label. Reading the tenant name off the
    # fetched lease is what makes the Whitfield misattribution impossible here:
    # the card names whichever tenant `tenant_id` actually points at.
    _lease_rec = None
    try:
        async with client:
            _lease_rec = await api.leases.get(int(lease_id))
    except Exception:  # noqa: BLE001 — fall back to ids, never to the model's text
        _lease_rec = None
    tenant_disp = await _gate_label(client, api, "tenant", tenant_id, lease=_lease_rec)
    lease_disp = await _gate_label(client, api, "lease", lease_id, lease=_lease_rec)
    details = (
        f"Record a move-out for {tenant_disp} on {lease_disp} as of {move_out_date} in Buildium."
    )
    items = [f"Lease: {lease_disp}", f"Tenant: {tenant_disp}", f"Move-out date: {move_out_date}"]

    company_id = _current_company_id()
    key = derive_key(
        company_id,
        "buildium_record_move_out",
        {"lease_id": int(lease_id), "tenant_id": int(tenant_id), "date": move_out_date},
    )

    async def _do_move_out() -> str:
        async with client:
            await api.leases.record_move_out(
                int(lease_id), tenant_id=int(tenant_id), move_out_date=move_out_date
            )
        return _move_out_accepted(tenant_disp, lease_disp)

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="buildium_record_move_out",
            confirm={
                "action": "buildium_record_move_out",
                "title": "Record this move-out in Buildium?",
                "details": details,
                "items": items,
                "request_body": {
                    "lease_id": int(lease_id),
                    "TenantId": int(tenant_id),
                    "MoveOutDate": move_out_date,
                },
            },
            execute=_do_move_out,
            window=timedelta(minutes=10),
        )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "record the move-out")

    if guard.outcome == "not_approved":
        return "Okay, I didn't record the move-out. Nothing was changed in Buildium."
    if guard.outcome in ("blocked_store_unavailable", "blocked_needs_review"):
        return (
            "I couldn't safely record the move-out, so nothing was changed in Buildium. "
            "Please check the lease in Buildium before retrying."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="record the move-out", system="Buildium")
    if guard.result is not None:
        return str(guard.result)
    return f"Recorded a move-out for {tenant_disp} on {lease_disp} in Buildium."


@function_tool
async def buildium_record_move_out(
    lease_id: int,
    tenant_id: int,
    move_out_date: str,
    tenant_label: str | None = None,
    lease_label: str | None = None,
) -> str:
    """Record a tenant move-out on a lease (a write — confirm-gated).

    Resolve ``lease_id`` + ``tenant_id`` first; pass ``lease_label`` +
    ``tenant_label``. ``move_out_date`` is YYYY-MM-DD. A move-out is reversible
    in Buildium if recorded in error.

    NOTE: registered LIVE (confirm-gated) — the move-out contract was
    live-probed 2026-07-26 and is REVERSIBLE via ``Leases.undo_move_out``
    (gotcha 22).
    """
    return await _record_move_out_impl(
        lease_id, tenant_id, move_out_date, tenant_label=tenant_label, lease_label=lease_label
    )


async def _undo_move_out_impl(lease_id: int, tenant_id: int) -> str:
    """Undo a recorded move-out (a write — confirm-gated).

    ⚠ **This exists because ``buildium_record_move_out`` shipped live on the
    strength of it.** Every other hard write in this product is gated on how
    hard it is to take back: a payment is immutable and a work-order close
    latches one-way, so both carry a Manager+ floor (gotchas 20/21), while the
    move-out was registered confirm-only with the reason written into its own
    docstring — *"REVERSIBLE via ``Leases.undo_move_out``"*.

    ``Leases.undo_move_out`` was wrapped, tested and reachable from nothing.
    So the justification for the weaker gate was an undo path the product did
    not expose: record a move-out on the wrong tenant and the only remedy was
    the Buildium UI. A safety argument that rests on a capability nobody can
    invoke is the same shape as a docstring promising a keepalive that never
    reached the wire (gotcha 54) — stated, load-bearing, and absent.

    Confirm-gated and NOT role-floored, deliberately: undoing is the recovery
    direction. Putting a Manager+ floor on the correction while the mistake
    itself needs only a confirm would make the damage easier to do than to
    repair, which is exactly backwards.
    """
    from datetime import timedelta

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    # Same discipline as the record path: the card names whichever tenant
    # `tenant_id` actually points at, read off the fetched lease rather than
    # off anything the model wrote (the Whitfield misattribution, gotcha 41).
    _lease_rec = None
    try:
        async with client:
            _lease_rec = await api.leases.get(int(lease_id))
    except Exception:  # noqa: BLE001 — fall back to ids, never to the model's text
        _lease_rec = None
    tenant_disp = await _gate_label(client, api, "tenant", tenant_id, lease=_lease_rec)
    lease_disp = await _gate_label(client, api, "lease", lease_id, lease=_lease_rec)

    company_id = _current_company_id()
    key = derive_key(
        company_id,
        "buildium_undo_move_out",
        {"lease_id": int(lease_id), "tenant_id": int(tenant_id)},
    )

    async def _do_undo_move_out() -> str:
        async with client:
            await api.leases.undo_move_out(int(lease_id), int(tenant_id))
        return f"Removed the move-out for {tenant_disp} on {lease_disp} in Buildium."

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="buildium_undo_move_out",
            confirm={
                "action": "buildium_undo_move_out",
                "title": "Remove this move-out from Buildium?",
                "details": (
                    f"Remove the recorded move-out for {tenant_disp} on {lease_disp} "
                    "in Buildium, putting the lease back the way it was."
                ),
                "items": [f"Lease: {lease_disp}", f"Tenant: {tenant_disp}"],
                "request_body": {"lease_id": int(lease_id), "TenantId": int(tenant_id)},
            },
            execute=_do_undo_move_out,
            window=timedelta(minutes=10),
        )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "remove the move-out")

    if guard.outcome == "not_approved":
        return "Okay, I left the move-out in place. Nothing was changed in Buildium."
    if guard.outcome in ("blocked_store_unavailable", "blocked_needs_review"):
        return (
            "I couldn't safely remove the move-out, so nothing was changed in Buildium. "
            "Please check the lease in Buildium before retrying."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="remove the move-out", system="Buildium")
    if guard.result is not None:
        return str(guard.result)
    return f"Removed the move-out for {tenant_disp} on {lease_disp} in Buildium."


@function_tool
async def buildium_undo_move_out(lease_id: int, tenant_id: int) -> str:
    """Remove a move-out recorded in error, restoring the lease (a write —
    confirm-gated).

    Use this ONLY to correct a mistake — a move-out recorded for the wrong
    tenant, the wrong lease, or one that was called off. It is the reversal of
    ``buildium_record_move_out``, not a way to change a move-out DATE: to move
    the date, undo and record again.

    Resolve ``lease_id`` + ``tenant_id`` first. Buildium keys a move-out by
    tenant, not by a move-out id (gotcha 22).
    """
    return await _undo_move_out_impl(lease_id, tenant_id)


async def _add_note_impl(*, kind: str, target_id: int, note: str, label: str | None) -> str:
    """Add a staff note to a task or work order (a write).

    Append-only and low-stakes, so it runs through ``run_guarded`` for the
    confirm gate + idempotency claim (no double-post of the same note) but skips
    the read-back verify — a note has no clean get-by-id read-back, and the POST
    succeeding is the signal.
    """
    from datetime import timedelta

    note = (note or "").strip()
    if not note:
        return "I need the note text to add — what should the note say?"

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    noun = "work order" if kind == "work_order" else "task"
    # ⚠ `label` is model-supplied and DELIBERATELY IGNORED (see _gate_label).
    # `kind` is "task" or "work_order" — both have getters, so the note card
    # names the record it will actually be attached to.
    display = await _gate_label(client, api, kind, target_id)
    details = f"Add a note to {display} in Buildium."
    items = [f"{noun.capitalize()}: {display}", f"Note: {note}"]

    company_id = _current_company_id()
    key = derive_key(
        company_id,
        f"buildium_add_{kind}_note",
        {"target_id": target_id, "note": note},
    )

    async def _do_add() -> str:
        async with client:
            if kind == "work_order":
                await api.work_orders.add_note(target_id, note=note)
            else:
                await api.tasks.add_note(target_id, note=note)
        return f"Added your note to {display} in Buildium."

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type=f"buildium_add_{kind}_note",
            confirm={
                "action": f"buildium_add_{kind}_note",
                "title": f"Add a note to this {noun}?",
                "details": details,
                "items": items,
                # The ACTUAL note payload (approval-pipeline PR).
                "request_body": {"kind": kind, "target_id": target_id, "Note": note},
            },
            execute=_do_add,
            window=timedelta(minutes=10),
        )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, f"add the {noun} note")

    if guard.outcome == "not_approved":
        return "Okay, I didn't add the note. Nothing was changed in Buildium."
    if guard.outcome == "blocked_store_unavailable":
        return (
            "I couldn't safely process this right now, so I did not add the note — "
            "nothing was changed in Buildium. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        return (
            "I tried to add the note but couldn't confirm it went through, so I'm "
            "flagging it for review. Please check Buildium before retrying."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="add the note", system="Buildium")
    if guard.result is not None:
        return str(guard.result)
    return f"Added your note to {display} in Buildium."


@function_tool
async def buildium_add_task_note(
    task_id: int,
    note: str,
    task_label: str | None = None,
) -> str:
    """Add a staff note to a Buildium to-do task (a write — confirm-gated).

    Resolve ``task_id`` from ``buildium_list_tasks`` (results include ids). Pass
    ``task_label`` (the title) so the confirm card shows the title, not the id.

    Args:
        task_id: Buildium to-do task id (required).
        note: The note text to add (required).
        task_label: The task's title to show the user (recommended).
    """
    return await _add_note_impl(kind="task", target_id=task_id, note=note, label=task_label)


@function_tool
async def buildium_add_work_order_note(
    work_order_id: int,
    note: str,
    work_order_label: str | None = None,
) -> str:
    """Add a staff note to a Buildium work order (a write — confirm-gated).

    Resolve ``work_order_id`` from ``buildium_list_open_work_orders`` (results
    include the id as ``[ref:N]``). Pass ``work_order_label`` (the title) so the
    confirm card shows the title, not the id.

    Args:
        work_order_id: Buildium work-order id (required).
        note: The note text to add (required).
        work_order_label: The work order's title to show the user (recommended).
    """
    return await _add_note_impl(
        kind="work_order", target_id=work_order_id, note=note, label=work_order_label
    )


# ── Request tasks (read-only) ───────────────────────────────────────────

# kind -> (BuildiumApi.requests method name, human label)
_REQUEST_LISTERS: dict[str, tuple[str, str]] = {
    "resident": ("list_resident", "resident requests"),
    "rental_owner": ("list_rental_owner", "rental-owner requests"),
    "contact": ("list_contact", "contact requests"),
}


def _format_request_tasks(tasks: list[Any], label: str) -> str:
    if not tasks:
        return f"No {label} found in Buildium."
    limit = 40
    lines: list[str] = []
    for t in tasks[:limit]:
        title = getattr(t, "title", None) or "Untitled request"
        status = getattr(t, "task_status", None) or "—"
        priority = getattr(t, "priority", None)
        unit = getattr(t, "unit_number", None)
        due = _format_date(getattr(t, "due_date", None))
        category = getattr(t, "category", None)
        cat_name = getattr(category, "name", None) if category else None
        extras = " · ".join(
            p
            for p in (
                f"{priority} priority" if priority else None,
                cat_name,
                f"Unit {unit}" if unit else None,
                f"due {due}" if due else None,
            )
            if p
        )
        tail = f" — {extras}" if extras else ""
        lines.append(f"- {title} [{status}]{tail}")
    footer = _truncate_footer(len(tasks), limit)
    if footer:
        lines.append(footer)
    return f"{label.capitalize()}:\n" + "\n".join(lines)


async def _list_request_tasks_impl(kind: str, status: str = "") -> str:
    method_name, label = _REQUEST_LISTERS[kind]
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    filters: dict[str, Any] = {}
    wanted = status.strip()
    if wanted:
        # Buildium's task-status enum is PascalCase:
        # New / InProgress / Completed / Deferred / Closed.
        filters["taskstatus"] = wanted
    try:
        async with client:
            lister = getattr(api.requests, method_name)
            tasks = await lister(**filters)
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, f"list {label}")
    return _format_request_tasks(tasks, label)


@function_tool
async def buildium_list_resident_requests(status: str = "") -> str:
    """List resident (tenant-submitted) requests from Buildium.

    Args:
        status: Optional Buildium task status — ``New``, ``InProgress``,
            ``Completed``, ``Deferred``, or ``Closed``. Empty = all.
    """
    return await _list_request_tasks_impl("resident", status)


@function_tool
async def buildium_list_rental_owner_requests(status: str = "") -> str:
    """List rental-owner-submitted requests from Buildium.

    Args:
        status: Optional Buildium task status — ``New``, ``InProgress``,
            ``Completed``, ``Deferred``, or ``Closed``. Empty = all.
    """
    return await _list_request_tasks_impl("rental_owner", status)


@function_tool
async def buildium_list_contact_requests(status: str = "") -> str:
    """List contact requests (prospect / general inquiries) from Buildium.

    Args:
        status: Optional Buildium task status — ``New``, ``InProgress``,
            ``Completed``, ``Deferred``, or ``Closed``. Empty = all.
    """
    return await _list_request_tasks_impl("contact", status)


# ── General Ledger (read-only) ──────────────────────────────────────────


async def _list_gl_accounts_impl(account_type: str = "") -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        filters: dict[str, Any] = {}
        wanted = account_type.strip().title()
        if wanted:
            filters["accounttypes"] = [wanted]
        async with client:
            accounts = await api.general_ledger.list_accounts(**filters)
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list general-ledger accounts")

    if not accounts:
        scope = f" of type {wanted}" if account_type.strip() else ""
        return f"No general-ledger accounts{scope} found in Buildium."

    limit = 60
    lines: list[str] = []
    for a in accounts[:limit]:
        name = getattr(a, "name", None) or "Unnamed account"
        number = getattr(a, "account_number", None)
        type_ = getattr(a, "sub_type", None) or getattr(a, "type", None) or ""
        prefix = f"#{number} " if number else ""
        suffix = f" — {type_}" if type_ else ""
        # ⚠ ``#{number}`` is the human-facing ACCOUNT NUMBER, not the id, and
        # it looks exactly like one. ``post_charge`` / ``record_payment`` /
        # ``renew_lease`` all require ``gl_account_id``, so a model reading
        # this had a plausible wrong value and no correct one. Worse than the
        # tenant case: gotcha 22 records that Buildium validates GL PRESENCE
        # and not EXISTENCE, so a wrong GlAccountId is ACCEPTED — a real
        # charge against the wrong account, with no error to notice.
        lines.append(f"- {prefix}{name}{suffix} [ref:{getattr(a, 'id', '?')}]")
    footer = _truncate_footer(len(accounts), limit)
    if footer:
        lines.append(footer)
    return "Chart of accounts:\n" + "\n".join(lines)


@function_tool
async def buildium_list_gl_accounts(account_type: str = "") -> str:
    """List the general-ledger chart of accounts.

    Args:
        account_type: Optional filter — one of ``Asset``, ``Liability``,
            ``Equity``, ``Income``, ``Expense``. Leave empty for all accounts.
    """
    return await _list_gl_accounts_impl(account_type)


async def _gl_account_balances_impl(as_of_date: str = "") -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    as_of = as_of_date.strip() or datetime.now(UTC).date().isoformat()
    try:
        async with client:
            balances = await api.general_ledger.list_account_balances(asofdate=as_of)
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "read general-ledger balances")

    if not balances:
        return f"No general-ledger balances found as of {as_of}."

    # Largest absolute balances first — that's what a PM scans for.
    def _abs(row: Any) -> float:
        amount = getattr(row, "total_balance", None)
        return abs(amount) if isinstance(amount, (int, float)) else 0.0

    ranked = sorted(balances, key=_abs, reverse=True)
    limit = 50
    lines: list[str] = []
    for b in ranked[:limit]:
        ref = getattr(b, "gl_account", None)
        name = getattr(ref, "name", None) or "Unnamed account"
        number = getattr(ref, "account_number", None)
        prefix = f"#{number} " if number else ""
        lines.append(f"- {prefix}{name}: {_format_money(getattr(b, 'total_balance', None))}")
    footer = _truncate_footer(len(balances), limit)
    if footer:
        lines.append(footer)
    return f"Account balances as of {as_of}:\n" + "\n".join(lines)


@function_tool
async def buildium_get_gl_account_balances(as_of_date: str = "") -> str:
    """Show general-ledger account balances as of a date.

    The LEDGER view, and the only one that answers "as of": every GL account —
    income, expense, asset, liability — at any date you name, not just the
    banks. Use it when the question says general ledger, names a GL account, or
    asks what something held on a DATE.

    For "what's in the operating account right now",
    ``buildium_list_bank_accounts`` is the banking view and the better answer.

    Args:
        as_of_date: Balance date in ``YYYY-MM-DD`` form. Leave empty for today.
    """
    return await _gl_account_balances_impl(as_of_date)


async def _list_gl_transactions_impl(start_date: str, end_date: str) -> str:
    start = start_date.strip()
    end = end_date.strip()
    if not start or not end:
        return (
            "Please give a date range (start and end, YYYY-MM-DD) for the "
            "general-ledger transactions you want to see."
        )
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            txns = await api.general_ledger.list_transactions(startdate=start, enddate=end)
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list general-ledger transactions")

    if not txns:
        return f"No general-ledger transactions between {start} and {end}."

    limit = 40
    lines: list[str] = []
    for t in txns[:limit]:
        date = _format_date(getattr(t, "date", None))
        type_ = getattr(t, "transaction_type", None) or "Transaction"
        amount = _format_money(getattr(t, "total_amount", None))
        journal = getattr(t, "journal", None)
        memo = getattr(journal, "memo", None) if journal else None
        unit = getattr(t, "unit_number", None)
        extras = " · ".join(p for p in (memo, f"Unit {unit}" if unit else None) if p)
        tail = f" · {extras}" if extras else ""
        lines.append(f"- {date} · {type_} · {amount}{tail}")
    footer = _truncate_footer(len(txns), limit)
    if footer:
        lines.append(footer)
    return f"General-ledger transactions {start} → {end}:\n" + "\n".join(lines)


@function_tool
async def buildium_list_gl_transactions(start_date: str, end_date: str) -> str:
    """List general-ledger transactions within a date range.

    Buildium requires both dates for this report.

    Args:
        start_date: Range start, ``YYYY-MM-DD`` (required).
        end_date: Range end, ``YYYY-MM-DD`` (required).
    """
    return await _list_gl_transactions_impl(start_date, end_date)


# ── Communications (read-only) ──────────────────────────────────────────


def _party_name(party: Any) -> str | None:
    if party is None:
        return None
    name = getattr(party, "name", None)
    if name:
        return str(name)
    first = getattr(party, "first_name", None) or ""
    last = getattr(party, "last_name", None) or ""
    full = f"{first} {last}".strip()
    return full or None


async def _list_announcements_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            rows = await api.communications.list_announcements()
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list announcements")
    if not rows:
        return "No announcements found in Buildium."
    limit = 30
    lines: list[str] = []
    for a in rows[:limit]:
        subject = getattr(a, "subject", None) or "Untitled announcement"
        date = _format_date(getattr(a, "announcement_date", None))
        channels = getattr(a, "channels", None) or []
        chan = ", ".join(str(c) for c in channels) if channels else ""
        extras = " · ".join(p for p in (date, chan) if p)
        tail = f" — {extras}" if extras else ""
        lines.append(f"- {subject}{tail}")
    footer = _truncate_footer(len(rows), limit)
    if footer:
        lines.append(footer)
    return "Announcements:\n" + "\n".join(lines)


@function_tool
async def buildium_list_announcements() -> str:
    """List announcements posted to tenants/owners in Buildium."""
    return await _list_announcements_impl()


async def _list_sent_emails_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            rows = await api.communications.list_emails()
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list sent emails")
    if not rows:
        return "No sent emails found in Buildium."
    limit = 30
    lines: list[str] = []
    for e in rows[:limit]:
        subject = getattr(e, "subject", None) or "(no subject)"
        date = _format_date(getattr(e, "sent_date_time", None))
        sender = _party_name(getattr(e, "sender", None))
        extras = " · ".join(p for p in (date, sender) if p)
        tail = f" — {extras}" if extras else ""
        lines.append(f"- {subject}{tail}")
    footer = _truncate_footer(len(rows), limit)
    if footer:
        lines.append(footer)
    return "Sent emails:\n" + "\n".join(lines)


@function_tool
async def buildium_list_sent_emails() -> str:
    """List emails Buildium has sent (subject, sender, date) — the email log."""
    return await _list_sent_emails_impl()


async def _list_phone_logs_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            rows = await api.communications.list_phone_logs()
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list phone logs")
    if not rows:
        return "No phone logs found in Buildium."
    limit = 30
    lines: list[str] = []
    for p in rows[:limit]:
        subject = getattr(p, "subject", None) or "Call"
        date = _format_date(getattr(p, "call_date_time", None))
        who = _party_name(getattr(p, "participant", None))
        extras = " · ".join(x for x in (date, who) if x)
        tail = f" — {extras}" if extras else ""
        lines.append(f"- {subject}{tail}")
    footer = _truncate_footer(len(rows), limit)
    if footer:
        lines.append(footer)
    return "Phone logs:\n" + "\n".join(lines)


@function_tool
async def buildium_list_phone_logs() -> str:
    """List logged phone calls (subject, participant, date) from Buildium."""
    return await _list_phone_logs_impl()


# ── Listings (read-only) ────────────────────────────────────────────────


async def _list_listings_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            rows = await api.listings.list()
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list listings")
    if not rows:
        return "No active listings found in Buildium."
    limit = 40
    lines: list[str] = []
    for listing in rows[:limit]:
        unit = getattr(listing, "unit", None)
        prop = getattr(listing, "property", None)
        unit_no = getattr(unit, "unit_number", None) if unit else None
        prop_name = getattr(prop, "name", None) if prop else None
        where = " · ".join(p for p in (prop_name, f"Unit {unit_no}" if unit_no else None) if p)
        where = where or "Listing"
        rent = _format_money(getattr(listing, "rent", None))
        available = _format_date(getattr(listing, "available_date", None))
        extras = " · ".join(
            p
            for p in (
                f"{rent}/mo" if rent else None,
                f"available {available}" if available else None,
            )
            if p
        )
        tail = f" — {extras}" if extras else ""
        lines.append(f"- {where}{tail}")
    footer = _truncate_footer(len(rows), limit)
    if footer:
        lines.append(footer)
    return "Active listings:\n" + "\n".join(lines)


@function_tool
async def buildium_list_listings() -> str:
    """List the active rental listings in Buildium — your leasing pipeline /
    advertised vacancies (unit, rent, available date)."""
    return await _list_listings_impl()


# ── Files: upload a company document + share it (writes) ───────────────
#
# Spec-grounded 2026-07-05 (see buildium/endpoints/files.py header): upload
# is a two-step presigned-S3 flow with async ingest; sharing is a per-section
# GET-merge-PUT where only the file's own entity-type sections are populated.

# What chat may attach a file to — the rentals-focused subset of Buildium's
# 12-value EntityType enum (association types are out of product scope).
_FILE_ATTACH_TYPES: dict[str, str] = {
    "rental": "Rental",
    "property": "Rental",
    "unit": "RentalUnit",
    "rentalunit": "RentalUnit",
    "lease": "Lease",
    "tenant": "Tenant",
    "vendor": "Vendor",
    "owner": "RentalOwner",
    "rentalowner": "RentalOwner",
}

# UpdateFileSharingSettingRequest's per-section keys (wire names).
_SHARING_SECTIONS: tuple[str, ...] = (
    "Account",
    "Rental",
    "RentalUnit",
    "Lease",
    "Tenant",
    "RentalOwner",
    "Association",
    "AssociationUnit",
    "OwnershipAccount",
    "AssociationOwner",
    "Vendor",
    "Committee",
)


async def _pick_file_category(api: BuildiumApi) -> tuple[int | None, str]:
    """CategoryId is REQUIRED on upload. Prefer the account's 'Uncategorized'
    category, else the first one; ``(None, "")`` when the account has none."""
    categories = await api.files.list_categories()
    if not categories:
        return None, ""
    for cat in categories:
        if str(cat.get("Name") or "").strip().lower() == "uncategorized":
            return cat.get("Id"), str(cat.get("Name") or "")
    first = categories[0]
    return first.get("Id"), str(first.get("Name") or "")


async def _upload_document_impl(
    *,
    document_name: str,
    attach_to: str,
    entity_id: int,
    entity_label: str | None,
    title: str,
    description: str,
) -> str:
    from helixis.db.engine import get_pool
    from helixis.db.repositories import docs_links as docs_repo
    from helixis.settings import get_settings
    from helixis.storage import StorageError, download_object

    entity_type = _FILE_ATTACH_TYPES.get(attach_to.strip().lower().replace(" ", ""))
    if entity_type is None:
        options = ", ".join(sorted(set(_FILE_ATTACH_TYPES.values())))
        return f"I can attach files to one of: {options}. Which should this go on?"

    company_id = _current_company_id()
    if not company_id:
        return "I couldn't determine which company you belong to — please re-open chat."

    # Resolve the document from the company Documents library.
    docs = await docs_repo.list_documents(get_pool(), company_id=company_id)
    if not docs:
        return (
            "Your Documents library is empty, so there is no PDF for me to "
            "attach. Do NOT point them at a place to upload one — the library "
            "has no upload screen in Occupella today."
        )
    needle = document_name.strip().lower()
    matches = [d for d in docs if d.name.strip().lower() == needle]
    if not matches:
        matches = [d for d in docs if needle in d.name.lower()]
    if not matches:
        names = ", ".join(d.name for d in docs[:10])
        return f'No document matches "{document_name}". Your library has: {names}.'
    if len(matches) > 1:
        names = ", ".join(d.name for d in matches[:10])
        return f"Multiple documents match: {names}. Which one?"
    doc = matches[0]

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    # ⚠ entity_label ignored — polymorphic target, no getter; ID form is honest.
    display_entity = f"{entity_type} {entity_id}"
    file_title = (title or doc.name).strip()[:255]
    file_name = f"{doc.name}.pdf" if not doc.name.lower().endswith(".pdf") else doc.name

    key = derive_key(
        company_id,
        "buildium_upload_document",
        {
            "document_id": doc.id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "title": file_title,
        },
    )

    async def _do_upload() -> str:
        try:
            content = await download_object(
                bucket="company_documents",
                path=doc.storage_path,
                settings=get_settings(),
            )
        except StorageError:
            return (
                f'I couldn\'t read "{doc.name}" from your Documents library, so '
                "nothing was uploaded to Buildium. Please try again shortly."
            )
        async with client:
            category_id, category_name = await _pick_file_category(api)
            if category_id is None:
                return (
                    "Your Buildium account has no file categories yet — add one "
                    "in Buildium (Files → Categories), then ask me again. "
                    "Nothing was uploaded."
                )
            record = await api.files.upload(
                entity_type=entity_type,
                entity_id=entity_id,
                file_name=file_name[:255],
                title=file_title,
                category_id=int(category_id),
                content=content,
                description=(description or "").strip()[:1000] or None,
            )
        if record is not None and record.get("Id"):
            return (
                f'Uploaded "{doc.name}" to Buildium and attached it to '
                f"{display_entity} (file #{record['Id']}, category "
                f"{category_name or category_id})."
            )
        return (
            f'Uploaded "{doc.name}" to Buildium for {display_entity} — Buildium '
            "accepted it and is still processing; it will appear under Files "
            "in a moment."
        )

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="buildium_upload_document",
            confirm={
                "action": "buildium_upload_document",
                "title": "Upload this document to Buildium?",
                "details": f'Upload "{doc.name}" and attach it to {display_entity}.',
                "items": [
                    f"Document: {doc.name} ({doc.size_bytes / 1024:.0f} KB)",
                    f"Attach to: {display_entity}",
                    f"Title in Buildium: {file_title}",
                ],
                # The ACTUAL upload parameters (approval-pipeline PR).
                "request_body": {
                    "file_name": doc.name,
                    "file_title": file_title,
                    "attach_to": display_entity,
                    "size_bytes": doc.size_bytes,
                },
            },
            execute=_do_upload,
            window=timedelta(minutes=10),
        )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "upload the document")

    if guard.outcome == "not_approved":
        return "Okay, I didn't upload it. Nothing was sent to Buildium."
    if guard.outcome == "blocked_store_unavailable":
        return (
            "I couldn't safely process this right now, so I did not upload the "
            "document — nothing was sent to Buildium. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        return (
            "I tried to upload the document but couldn't confirm it went through, "
            "so I'm flagging it for review. Please check Buildium's Files before retrying."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="upload the document", system="Buildium")
    if guard.result is not None:
        return str(guard.result)
    return f'Uploaded "{doc.name}" to Buildium for {display_entity}.'


@function_tool
async def buildium_upload_document(
    document_name: str,
    attach_to: str,
    entity_id: int,
    entity_label: str | None = None,
    title: str = "",
    description: str = "",
) -> str:
    """Upload a PDF from the company Documents library to Buildium and attach
    it to a record (a write — confirm-gated).

    The document must already be in the company Documents library. Resolve
    ``entity_id`` from the matching list tool first (properties, leases,
    tenants, vendors — their results include ids).

    Args:
        document_name: Name of the document in the Documents library (required).
        attach_to: What to attach it to — one of Rental, RentalUnit, Lease,
            Tenant, Vendor, RentalOwner (required).
        entity_id: The Buildium id of that record (required).
        entity_label: Human label for the confirm card, e.g. the property
            name (recommended).
        title: Title to show in Buildium (defaults to the document name).
        description: Optional description (max 1000 chars).
    """
    return await _upload_document_impl(
        document_name=document_name,
        attach_to=attach_to,
        entity_id=entity_id,
        entity_label=entity_label,
        title=title,
        description=description,
    )


async def _share_file_impl(
    *,
    file_id: int,
    share_with_tenants: bool | None,
    share_with_rental_owners: bool | None,
    file_label: str | None,
) -> str:
    if share_with_tenants is None and share_with_rental_owners is None:
        return "Tell me what to change — share (or unshare) with tenants, rental owners, or both?"

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    company_id = _current_company_id()
    # ⚠ file_label ignored — see _gate_label.
    display = f"file {file_id}"

    key = derive_key(
        company_id,
        "buildium_share_file",
        {
            "file_id": file_id,
            "tenants": share_with_tenants,
            "rental_owners": share_with_rental_owners,
        },
    )

    changes: list[str] = []
    if share_with_tenants is not None:
        changes.append(f"tenants: {'shared' if share_with_tenants else 'not shared'}")
    if share_with_rental_owners is not None:
        changes.append(f"rental owners: {'shared' if share_with_rental_owners else 'not shared'}")

    async def _do_share() -> str:
        async with client:
            current = await api.files.get_sharing(file_id)
            # Only the file's own entity-type sections are populated; carry
            # those over untouched and apply the requested flags where the
            # section actually has that knob.
            body: dict[str, Any] = {}
            touched = False
            for section in _SHARING_SECTIONS:
                value = current.get(section)
                if not isinstance(value, dict):
                    continue
                value = dict(value)
                if share_with_tenants is not None and "Tenants" in value:
                    value["Tenants"] = share_with_tenants
                    touched = True
                if share_with_rental_owners is not None and "RentalOwners" in value:
                    value["RentalOwners"] = share_with_rental_owners
                    touched = True
                body[section] = value
            if not body:
                return (
                    f"Buildium returned no share settings for {display} — I didn't change anything."
                )
            if not touched:
                return (
                    f"{display} doesn't support tenant/owner sharing (its file "
                    "type has different share options) — nothing was changed."
                )
            await api.files.put_sharing(file_id, body)
        return f"Updated sharing for {display} in Buildium ({'; '.join(changes)})."

    async def _verify_share(_result: str) -> bool:
        """Read the settings back and confirm the requested flags stuck.
        READ-ONLY GET — safe to run after the write."""
        async with client:
            after = await api.files.get_sharing(file_id)
        for section in _SHARING_SECTIONS:
            value = after.get(section)
            if not isinstance(value, dict):
                continue
            if (
                share_with_tenants is not None
                and "Tenants" in value
                and bool(value.get("Tenants")) != share_with_tenants
            ):
                return False
            if (
                share_with_rental_owners is not None
                and "RentalOwners" in value
                and bool(value.get("RentalOwners")) != share_with_rental_owners
            ):
                return False
        return True

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="buildium_share_file",
            confirm={
                "action": "buildium_share_file",
                "title": "Change this file's sharing in Buildium?",
                "details": f"Update sharing for {display}.",
                "items": [f"File: {display}", *[f"Set {c}" for c in changes]],
            },
            execute=_do_share,
            verify=_verify_share,
            window=timedelta(minutes=10),
        )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "update the file's sharing")

    if guard.outcome == "not_approved":
        return "Okay, I left the file's sharing unchanged."
    if guard.outcome == "blocked_store_unavailable":
        return (
            "I couldn't safely process this right now, so the file's sharing was "
            "not changed. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        return (
            "I updated the sharing but couldn't confirm it stuck, so I'm flagging "
            "it for review — please check the file in Buildium."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="change the file's sharing", system="Buildium")
    if guard.result is not None:
        return str(guard.result)
    return f"Updated sharing for {display} in Buildium."


@function_tool
async def buildium_share_file(
    file_id: int,
    share_with_tenants: bool | None = None,
    share_with_rental_owners: bool | None = None,
    file_label: str | None = None,
) -> str:
    """Share (or unshare) a Buildium file with tenants and/or rental owners
    (a write — confirm-gated).

    Works on the file's existing share settings — only the flags you pass
    change; everything else is carried over. Resolve ``file_id`` from the
    upload tool's reply or Buildium's Files list.

    Args:
        file_id: Buildium file id (required).
        share_with_tenants: True to share with tenants, False to unshare.
        share_with_rental_owners: True to share with rental owners, False to
            unshare.
        file_label: The file's title for the confirm card (recommended).
    """
    return await _share_file_impl(
        file_id=file_id,
        share_with_tenants=share_with_tenants,
        share_with_rental_owners=share_with_rental_owners,
        file_label=file_label,
    )


# ── Bills + vendor notes (writes) — the maintenance money loop ─────────
#
# Spec-grounded 2026-07-05 (LeadSimple SDK BillsApi/CreateBillRequest/
# BillLinePostMessage + VendorsApi): POST /bills requires Date, DueDate,
# VendorId, Lines[{AccountingEntity{Id, AccountingEntityType, UnitId?},
# GlAccountId, Amount}]; WorkOrderId links the bill to a work order.
# Vendors have a REAL note endpoint (POST /vendors/{id}/notes) — unlike
# tasks/work orders.


def _parse_iso_date(value: str) -> str | None:
    """Return the normalized YYYY-MM-DD string, or None when unparseable."""
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


async def _create_bill_impl(
    *,
    vendor_id: int,
    amount: float,
    gl_account_id: int,
    property_id: int,
    due_date: str,
    bill_date: str,
    memo: str,
    unit_id: int | None,
    work_order_id: int | None,
    reference_number: str,
    vendor_label: str | None,
    property_label: str | None,
) -> str:
    if amount <= 0:
        return "The bill amount must be greater than zero — what should it be?"
    due = _parse_iso_date(due_date)
    if due is None:
        return "I need the due date as YYYY-MM-DD — when is this bill due?"
    if bill_date.strip():
        billed = _parse_iso_date(bill_date)
        if billed is None:
            return "The bill date must be YYYY-MM-DD (or leave it out for today)."
    else:
        billed = datetime.now(UTC).date().isoformat()

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    company_id = _current_company_id()
    # ⚠ *_label ignored — see _gate_label. Vendor derives from the record;
    # property has no getter yet, so it shows the checkable ID form.
    vendor_display = await _gate_label(client, api, "vendor", vendor_id)
    property_display = f"property {property_id}"

    line: dict[str, Any] = {
        "AccountingEntity": {"Id": property_id, "AccountingEntityType": "Rental"},
        "GlAccountId": gl_account_id,
        "Amount": amount,
    }
    if unit_id is not None:
        line["AccountingEntity"]["UnitId"] = unit_id
    if memo:
        line["Memo"] = memo[:240]
    body: dict[str, Any] = {
        "Date": billed,
        "DueDate": due,
        "VendorId": vendor_id,
        "Lines": [line],
    }
    if memo:
        body["Memo"] = memo[:245]
    if work_order_id is not None:
        body["WorkOrderId"] = work_order_id
    if reference_number.strip():
        body["ReferenceNumber"] = reference_number.strip()[:40]

    key = derive_key(
        company_id,
        "buildium_create_bill",
        {
            "vendor_id": vendor_id,
            "amount": amount,
            "gl_account_id": gl_account_id,
            "property_id": property_id,
            "due_date": due,
            "work_order_id": work_order_id,
            "reference_number": reference_number.strip(),
        },
    )

    async def _do_create() -> str:
        async with client:
            created = await api.bills.create(body)
        bill_id = created.get("Id")
        wo_part = f", linked to work order {work_order_id}" if work_order_id else ""
        return (
            f"Created the ${amount:,.2f} bill from {vendor_display} for "
            f"{property_display} in Buildium (bill #{bill_id}, due {due}{wo_part})."
        )

    async def _verify_created(_result: str) -> bool:
        """Read the newest matching bill back. READ-ONLY GET.

        ``paidstatus=Unpaid`` is required — a vendorid-only query still
        includes paid bills, which 422s without a FromPaidDate (live-
        probed 2026-07-06). A just-created bill is always unpaid.
        """
        async with client:
            bills = await api.bills.list(vendorid=vendor_id, paidstatus="Unpaid")
        return bool(bills)

    items = [
        f"Vendor: {vendor_display}",
        f"Amount: ${amount:,.2f}",
        f"Charged to: {property_display}" + (f" · unit {unit_id}" if unit_id else ""),
        f"Due date: {due}",
    ]
    if work_order_id is not None:
        items.append(f"Linked work order: {work_order_id}")
    if reference_number.strip():
        items.append(f"Reference #: {reference_number.strip()[:40]}")
    if memo:
        items.append(f"Memo: {memo[:100]}")

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="buildium_create_bill",
            confirm={
                "action": "buildium_create_bill",
                "title": "Create this vendor bill in Buildium?",
                "details": f"Record a ${amount:,.2f} bill from {vendor_display} "
                f"against {property_display}.",
                "items": items,
            },
            execute=_do_create,
            verify=_verify_created,
            window=timedelta(minutes=10),
        )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "create the bill")

    if guard.outcome == "not_approved":
        return "Okay, I didn't create the bill. Nothing was recorded in Buildium."
    if guard.outcome == "blocked_store_unavailable":
        return (
            "I couldn't safely process this right now, so I did not create the "
            "bill — nothing was recorded in Buildium. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        return (
            "I created the bill but couldn't confirm it on read-back, so I'm "
            "flagging it for review — please check Buildium's bills before retrying."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="create the bill", system="Buildium")
    if guard.result is not None:
        return str(guard.result)
    return f"Created the bill from {vendor_display} in Buildium."


@function_tool
async def buildium_create_bill(
    vendor_id: int,
    amount: float,
    gl_account_id: int,
    property_id: int,
    due_date: str,
    memo: str = "",
    bill_date: str = "",
    unit_id: int | None = None,
    work_order_id: int | None = None,
    reference_number: str = "",
    vendor_label: str | None = None,
    property_label: str | None = None,
) -> str:
    """Record a vendor bill in Buildium (a MONEY write — confirm-gated).

    Resolve ids first: vendor via ``buildium_search_vendors``, property via
    ``buildium_list_properties``, GL expense account via
    ``buildium_list_gl_accounts`` (e.g. "Repairs & Maintenance"). When the
    bill pays for a completed work order, pass ``work_order_id`` so Buildium
    links them. Ask the user for any amount/date you don't have — never
    guess money fields.

    Args:
        vendor_id: Buildium vendor id (required).
        amount: Bill amount in dollars, > 0 (required).
        gl_account_id: Expense GL account id (required).
        property_id: Property the expense is charged to (required).
        due_date: Payment due date, YYYY-MM-DD (required).
        memo: What the bill is for (recommended).
        bill_date: Bill/invoice date YYYY-MM-DD; defaults to today.
        unit_id: Unit within the property, when applicable.
        work_order_id: Work order this bill pays for, when applicable.
        reference_number: The vendor's invoice number (max 40 chars).
        vendor_label: Vendor name for the confirm card (recommended).
        property_label: Property name for the confirm card (recommended).
    """
    return await _create_bill_impl(
        vendor_id=vendor_id,
        amount=amount,
        gl_account_id=gl_account_id,
        property_id=property_id,
        due_date=due_date,
        bill_date=bill_date,
        memo=memo,
        unit_id=unit_id,
        work_order_id=work_order_id,
        reference_number=reference_number,
        vendor_label=vendor_label,
        property_label=property_label,
    )


_APPLICATION_STATUSES = ("Undecided", "Approved", "Rejected", "Cancelled", "Deferred")


async def _update_applicant_status_impl(
    applicant_id: int,
    status: str,
    applicant_label: str | None = None,
    application_id: int | None = None,
) -> str:
    """Set an applicant's application status in Buildium (a write).

    Spec-grounded 2026-07-06: status lives on the APPLICATION, not the
    applicant — ``PUT /applicants/{id}/applications/{appId}`` with exactly
    ``{"ApplicationStatus": <enum>}``. The API has NO delete operation on
    applications, so "Occupella never deletes applications" holds
    structurally. Mirror + CRM auto-move freshness rides the
    ApplicantApplication.Updated / Applicant.Updated webhooks that this
    write triggers — no inline mirror work needed here.
    """
    from datetime import timedelta

    if status not in _APPLICATION_STATUSES:
        return f"Status must be one of {', '.join(_APPLICATION_STATUSES)} — got {status!r}."

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    # ⚠ applicant_label ignored — see _gate_label.
    label = f"applicant {applicant_id}"

    # Resolve which application to update. Most applicants have exactly
    # one; when there are several and none was specified, ask instead of
    # guessing — this is a decision write.
    try:
        async with client:
            applications = await api.applicants.list_applications(applicant_id)
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "look up that applicant's applications")
    if not applications:
        return f"{label} has no applications on file in Buildium — nothing to update."
    app_ids = [app_id for a in applications if isinstance(app_id := a.get("Id"), int)]
    if application_id is not None:
        if application_id not in app_ids:
            return (
                f"Application {application_id} isn't on {label}. "
                f"Their applications: {', '.join(str(i) for i in app_ids)}."
            )
        target_id = application_id
    elif len(app_ids) == 1:
        target_id = app_ids[0]
    else:
        lines = ", ".join(
            f"#{a.get('Id')} ({a.get('ApplicationStatus') or 'unknown status'})"
            for a in applications
        )
        return (
            f"{label} has {len(app_ids)} applications: {lines}. "
            "Which one should I update? Pass its id as application_id."
        )
    current = next(
        (a.get("ApplicationStatus") for a in applications if a.get("Id") == target_id),
        None,
    )
    if current == status:
        return f'{label}\'s application is already "{status}" in Buildium — nothing to change.'

    company_id = _current_company_id()
    key = derive_key(
        company_id,
        "buildium_update_applicant_status",
        {"applicant_id": applicant_id, "application_id": target_id, "status": status},
    )
    details = f'Set {label}\'s application status to "{status}" in Buildium.'
    items = [
        f"Applicant: {label}",
        f"Application: #{target_id}",
        f"Status: {current or 'unknown'} → {status}",
    ]

    async def _do_update() -> str:
        async with client:
            await api.applicants.update_application_status(applicant_id, target_id, status)
        return f'Set {label}\'s application status to "{status}" in Buildium.'

    async def _verify_updated(_result: str) -> bool:
        """Read the application back; bounded retries for read-after-write lag."""
        import asyncio

        last_exc: Exception | None = None
        for attempt in range(_VERIFY_MAX_ATTEMPTS):
            try:
                async with client:
                    app = await api.applicants.get_application(applicant_id, target_id)
                if isinstance(app, dict) and app.get("ApplicationStatus") == status:
                    return True
            except BuildiumApiError as exc:
                if exc.status == 404:
                    return False
                last_exc = exc
            except Exception as exc:  # noqa: BLE001 — transient read failure
                last_exc = exc
            if attempt < _VERIFY_MAX_ATTEMPTS - 1:
                await asyncio.sleep(_VERIFY_BACKOFF_SECONDS * (attempt + 1))
        if last_exc is not None:
            raise last_exc
        return False

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="buildium_update_applicant_status",
            confirm={
                "action": "buildium_update_applicant_status",
                "title": "Update an applicant's status in Buildium?",
                "details": details,
                "items": items,
            },
            execute=_do_update,
            verify=_verify_updated,
            window=timedelta(minutes=10),
        )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "update the applicant status")

    if guard.outcome == "not_approved":
        return "Okay, I didn't change the applicant's status. Nothing was changed in Buildium."
    if guard.outcome == "blocked_store_unavailable":
        return (
            "I couldn't safely process this right now, so I did not change the applicant's "
            "status — nothing was changed in Buildium. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        return (
            "I updated the applicant's status in Buildium but couldn't confirm it went "
            "through, so I'm flagging it for review. Please check Buildium before retrying."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="change the applicant's status", system="Buildium")
    if guard.result is not None:
        return str(guard.result)
    return f'Set {label}\'s application status to "{status}" in Buildium.'


@function_tool
async def buildium_update_applicant_status(
    applicant_id: int,
    status: str,
    applicant_label: str | None = None,
    application_id: int | None = None,
) -> str:
    """Set an applicant's APPLICATION status in Buildium (a decision write —
    confirm-gated). Allowed: Undecided, Approved, Rejected, Cancelled,
    Deferred. Occupella can NEVER delete an application.

    Use for "approve Priya's application" / "reject applicant X" /
    "defer that application". Confirmation is enforced in code. Resolve the
    applicant via the mirror or live tools first and ALWAYS pass
    ``applicant_label`` (their name) so the confirm card reads like a human
    decision, never a raw id. If the applicant has several applications the
    tool lists them and asks — pass ``application_id`` to pick one.

    Args:
        applicant_id: Buildium applicant id (required).
        status: One of Undecided | Approved | Rejected | Cancelled | Deferred.
        applicant_label: Applicant's name for the confirm card (recommended).
        application_id: Specific application when the applicant has several.
    """
    return await _update_applicant_status_impl(
        applicant_id=applicant_id,
        status=status,
        applicant_label=applicant_label,
        application_id=application_id,
    )


async def _add_vendor_note_impl(*, vendor_id: int, note: str, vendor_label: str | None) -> str:
    note = (note or "").strip()
    if not note:
        return "I need the note text to add — what should the note say?"

    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED

    company_id = _current_company_id()
    # ⚠ vendor_label ignored — derived from the record instead.
    display = await _gate_label(client, api, "vendor", vendor_id)

    key = derive_key(
        company_id,
        "buildium_add_vendor_note",
        {"vendor_id": vendor_id, "note": note},
    )

    async def _do_add() -> str:
        async with client:
            created = await api.vendors.add_note(vendor_id, note=note)
        # Vendors have a real note endpoint; the response echoes the note.
        if isinstance(created, dict) and created.get("Id"):
            return f"Added your note to {display} in Buildium (note #{created['Id']})."
        return f"Added your note to {display} in Buildium."

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=key,
            action_type="buildium_add_vendor_note",
            confirm={
                "action": "buildium_add_vendor_note",
                "title": "Add a note to this vendor?",
                "details": f"Add a note to {display} in Buildium.",
                "items": [f"Vendor: {display}", f"Note: {note}"],
            },
            execute=_do_add,
            window=timedelta(minutes=10),
        )
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "add the vendor note")

    if guard.outcome == "not_approved":
        return "Okay, I didn't add the note. Nothing was changed in Buildium."
    if guard.outcome == "blocked_store_unavailable":
        return (
            "I couldn't safely process this right now, so I did not add the note — "
            "nothing was changed in Buildium. Please try again shortly."
        )
    if guard.outcome == "blocked_needs_review":
        return (
            "I tried to add the note but couldn't confirm it went through, so I'm "
            "flagging it for review. Please check the vendor in Buildium."
        )
    if guard.outcome not in SUCCESS_OUTCOMES:
        return pending_reply(guard, action="add the note", system="Buildium")
    if guard.result is not None:
        return str(guard.result)
    return f"Added your note to {display} in Buildium."


@function_tool
async def buildium_add_vendor_note(
    vendor_id: int,
    note: str,
    vendor_label: str | None = None,
) -> str:
    """Add a note to a Buildium vendor (a write — confirm-gated).

    Resolve ``vendor_id`` from ``buildium_search_vendors`` (results include
    ids). Pass ``vendor_label`` (the vendor's name) so the confirm card
    shows the name, not the id.

    Args:
        vendor_id: Buildium vendor id (required).
        note: The note text to add (required).
        vendor_label: The vendor's name to show the user (recommended).
    """
    return await _add_vendor_note_impl(vendor_id=vendor_id, note=note, vendor_label=vendor_label)


# ─────────────────────────────────────────────────────────────────────
# Portfolio-wide open maintenance, grouped by unit (the N+1 killer)
# ─────────────────────────────────────────────────────────────────────
#
# Battery v3, Q5: "before recommending a late notice on anyone, check
# whether they have open maintenance at their unit." Twenty delinquent
# tenants means twenty per-unit lookups, which does not fit a 14-call
# budget — so the run ALWAYS died on the 240s wall clock, having found the
# right twenty tenants and run out of time before writing a word. That is
# arithmetic, not slowness: raising the budget just moves the wall.
#
# This answers the same question for every unit at once, in two bulk calls.
# The agent then joins locally against whatever list it already has.
#
# It covers TASKS **and** WORK ORDERS. Q11 answered "probably not
# duplicates" after reading only work orders and then generalising to
# "maintenance requests" — the same word covering two Buildium entities is
# exactly how a confident wrong answer gets made, so a tool named for the
# concept must span both.


def _maintenance_index(
    work_orders: list[Any],
    tasks: list[Any],
    vendors: dict[int, str] | None = None,
    wo_property: dict[int, int] | None = None,
) -> dict[tuple[int | None, int | None], list[str]]:
    """(property_id, unit_id) → short descriptions of what's open there.

    ⚠ ``vendors`` (buildium_id → label) puts WHO IS HANDLING IT on the row.
    Without it, "who's on the leak at 4B?" reads the bulk result, finds no
    vendor, and hydrates record by record — the eighth instance of gotchas
    41/50/51/53, where the field was one join away and the row dropped it.
    Live-verified 2026-08-28: all 60 of Blue Ridge's open work orders carry a
    ``VendorId`` and all 60 resolve to a name (3 distinct vendors), so the
    join is one extra portfolio-wide call replacing N per-record ones.
    Tasks are NOT vendor-assigned in Buildium and deliberately say nothing.

    ⚠ ``wo_property`` (work-order id → property id, from
    ``work_order_property_ids``) is how a work order gets a property AT ALL.
    ``WorkOrder.property_id`` is null on every production row, so keying on it
    filed all 60 of Blue Ridge's open work orders under "unassigned" — this
    rollup, the one built to remove the per-record N+1, was reporting the
    whole book as property-less. The mirror already resolved it through the
    backing task and the live tool did not: gotcha 25's every-layer rule.
    """
    index: dict[tuple[int | None, int | None], list[str]] = {}
    vendors = vendors or {}
    wo_property = wo_property or {}
    for w in work_orders:
        wo_id = getattr(w, "id", None)
        prop = wo_property.get(wo_id) if isinstance(wo_id, int) else None
        if prop is None:
            prop = getattr(w, "property_id", None)
        key = (prop, getattr(w, "unit_id", None))
        title = getattr(w, "title", None) or "Untitled work order"
        status = getattr(w, "status", None) or "?"
        # An unresolvable vendor id prints NOTHING rather than "vendor 1234":
        # an identifier is not a name (PR #75 Bug B), and silence lets the
        # model say it does not know instead of reading a number aloud.
        vid = getattr(w, "vendor_id", None)
        who = vendors.get(int(vid)) if isinstance(vid, int) else None
        assigned = f", {who}" if who else ""
        index.setdefault(key, []).append(f"work order: {title} [{status}{assigned}]")
    for t in tasks:
        prop = getattr(getattr(t, "property", None), "id", None)
        key = (prop, getattr(t, "unit_id", None))
        title = getattr(t, "title", None) or "Untitled task"
        priority = getattr(t, "priority", None)
        suffix = f", priority {priority}" if priority else ""
        index.setdefault(key, []).append(f"task: {title} [{_task_status(t)}{suffix}]")
    return index


async def _open_maintenance_by_unit_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            work_orders = list(await api.work_orders.list(statuses=list(_OPEN_WORK_ORDER_STATUSES)))
            all_tasks = list(await api.tasks.list())
            properties = {
                int(p.id): (
                    getattr(p, "name", None) or _format_address(getattr(p, "address", None)) or ""
                )
                for p in await api.rentals.list_properties()
                if getattr(p, "id", None) is not None
            }
            # ONE portfolio-wide vendor read so every work-order row can name
            # who is handling it. The alternative is the model calling
            # buildium_get_work_order per row to find out — the N+1 this tool
            # exists to remove, promoted one field over (gotcha 32: removing
            # one fan-out just promotes the next one behind it).
            vendors = {
                int(v.id): _vendor_label(v)
                for v in await api.vendors.list()
                if getattr(v, "id", None) is not None
            }
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list open maintenance")

    # ⚠ EVERY WORK ORDER IS ALSO A TASK, so merging the two lists lists each
    # work order twice — once as itself and once as the to-do item Buildium
    # backs it with. Live-verified 2026-08-28: 93 of 93 work orders carry a
    # Task.Id and every one has a matching task row. The mirror rollup made
    # the identical mistake and rendered 150 open items where 90 exist.
    #
    # ⚠ MUST match the mirror's rule exactly or the same question answers
    # differently depending on which tool the router reaches — gotcha 50 already
    # records two disagreeing spellings of "open" here, and a third would be
    # worse than either. ``work_orders`` is ALREADY the open set (the API call
    # above filters on status), so excluding every task it backs is precisely
    # the mirror's "tasks behind an OPEN work order" and no wider: a task left
    # open behind a work order whose status latched at Completed (gotcha 20)
    # still counts, which is what stops this becoming a false zero.
    backed_by_open_work_order = {
        tid
        for w in work_orders
        if (tid := getattr(getattr(w, "task", None), "id", None)) is not None
    }
    tasks = [
        t
        for t in all_tasks
        if is_open(_task_status(t)) and getattr(t, "id", None) not in backed_by_open_work_order
    ]
    # Resolved from the tasks ALREADY fetched above -- no extra Buildium call.
    index = _maintenance_index(
        work_orders, tasks, vendors, work_order_property_ids(work_orders, all_tasks)
    )
    if not index:
        return (
            f"No open maintenance anywhere in the portfolio — checked all "
            f"{len(work_orders)} open work orders and {len(tasks)} open tasks."
        )

    lines = [
        f"Open maintenance by location ({len(work_orders)} work orders, "
        f"{len(tasks)} tasks across {len(index)} location(s)):",
        "",
    ]
    for (property_id, unit_id), items in sorted(
        index.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))
    ):
        name = properties.get(property_id or -1) or (
            f"property {property_id}" if property_id else "unassigned"
        )
        where = f"{name} (unit {unit_id})" if unit_id else name
        lines.append(f"- {where}: {len(items)} open")
        lines.extend(f"    · {item}" for item in items[:5])
        if len(items) > 5:
            lines.append(f"    · … and {len(items) - 5} more here")
    lines.append("")
    lines.append(
        "This is the WHOLE portfolio in one call. Join it against your tenant "
        "or lease list locally — do NOT re-query per unit."
    )
    return "\n".join(lines)


@function_tool
async def buildium_open_maintenance_by_unit() -> str:
    """Every open maintenance item in the portfolio, grouped by property and unit.

    Covers BOTH work orders and to-do tasks, because "maintenance" in a
    question means both and answering from only one is how a wrong answer
    gets made.

    Use this whenever you need maintenance status for MORE THAN ONE unit —
    "check whether each of these tenants has open maintenance", "which of
    the expiring leases has something unresolved", "who's behind AND has a
    repair open". One call replaces one lookup per unit, which is what makes
    those questions finish instead of running out of time.

    For a single named unit, `buildium_list_work_orders_for_unit` is still
    the narrower read.

    Returns: locations with their open items, busiest first.
    """
    return await _open_maintenance_by_unit_impl()


# ── Read-back for the writes the agent already makes ──────────────────
#
# ⚠ **The surface was built WRITE-FIRST, per workflow, and the reads were never
# added.** A census over ``buildium/endpoints/`` against this module's
# ``api.*.*`` call sites (``tests/unit/test_read_coverage_census.py``) found
# the agent could write a vendor note and never read one, change a task's
# status without seeing who last changed it or from what, and record a move-out
# it could not read back.
#
# That asymmetry is not cosmetic — it is the engine behind gotchas 41/50/51.
# When the row the agent needs is unreachable it cross-references two other
# results by hand, or asserts. Whitfield's address came from exactly that
# manoeuvre.
#
# ⚠ Every one of these is registered in ``call_budget.PER_RECORD_CAPS``. A new
# per-record read that skips that registry re-opens the N+1 loop gotchas
# 25/32/34/37 took four PRs to bound — the cap is the only thing that has ever
# stopped it, and prose never has.


async def _list_vendor_notes_impl(vendor_id: int) -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            notes = await api.vendors.list_notes(int(vendor_id))
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "read that vendor's notes")
    if not notes:
        # ⚠ States the SCOPE, not just the emptiness (gotcha 26). "No notes"
        # about the wrong vendor reads identically to "no notes" about the
        # right one.
        return f"No notes on file for vendor #{vendor_id}."
    lines = [f"{len(notes)} note(s) on vendor #{vendor_id}, newest first:"]
    for note in notes[:25]:
        when = str(note.get("CreatedDateTime") or note.get("LastUpdatedDateTime") or "")[:10]
        body = str(note.get("Note") or "").strip()
        lines.append(f"- {when or 'undated'}: {body[:300]}")
    if len(notes) > 25:
        lines.append(
            f"…and {len(notes) - 25} older note(s) not shown. This is a PARTIAL "
            "list — do not conclude anything is absent from it."
        )
    return "\n".join(lines)


async def _task_history_impl(task_id: int) -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            history = await api.tasks.list_history(int(task_id))
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "read that task's history")
    if not history:
        return f"No recorded history for task #{task_id}."
    lines = [f"{len(history)} history entr(ies) for task #{task_id}:"]
    for entry in history[:25]:
        when = str(entry.get("CreatedDateTime") or "")[:16]
        who = str(entry.get("CreatedByUser") or entry.get("LastUpdatedByUser") or "").strip()
        status = str(entry.get("TaskStatus") or entry.get("Status") or "").strip()
        note = str(entry.get("Message") or entry.get("Note") or "").strip()
        bits = [b for b in (when or None, who or None, status or None) if b]
        lines.append(f"- {' · '.join(bits)}" + (f" — {note[:200]}" if note else ""))
    if len(history) > 25:
        lines.append(
            f"…and {len(history) - 25} older entr(ies) not shown. This is a "
            "PARTIAL list — do not conclude anything is absent from it."
        )
    return "\n".join(lines)


async def _list_move_outs_impl(lease_id: int) -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            move_outs = await api.leases.list_move_outs(int(lease_id))
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "read move-outs for that lease")
    if not move_outs:
        return f"No move-outs recorded on lease #{lease_id}."
    lines = [f"{len(move_outs)} move-out(s) recorded on lease #{lease_id}:"]
    for mo in move_outs:
        tenant = getattr(mo, "tenant_id", None)
        date = getattr(mo, "move_out_date", None)
        notice = getattr(mo, "notice_given_date", None)
        lines.append(
            f"- tenant #{tenant}: moving out {date or 'date not recorded'}"
            + (f" (notice given {notice})" if notice else "")
        )
    # ⚠ This line said "Occupella cannot undo a move-out — that is done in
    # Buildium directly" for weeks AFTER PR #943 registered
    # `buildium_undo_move_out`. A stale docstring costs a reader an hour; a
    # stale line in a TOOL RESULT costs the user the capability, because the
    # model believes the tool over its own registry and declines. Gotcha 48's
    # rule, on the one surface where it reaches a customer.
    lines.append(
        "A move-out recorded in error can be undone with buildium_undo_move_out (confirm-gated)."
    )
    return "\n".join(lines)


async def _get_unit_impl(unit_id: int) -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            unit = await api.rentals.get_unit(int(unit_id))
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "look up that unit")
    number = getattr(unit, "unit_number", None) or f"#{unit_id}"
    address = _format_address(getattr(unit, "address", None))
    beds = getattr(unit, "unit_bedrooms", None)
    baths = getattr(unit, "unit_bathrooms", None)
    size = getattr(unit, "unit_size", None)
    rent = getattr(unit, "market_rent", None)
    bits = [f"Unit {number}"]
    if address:
        bits.append(address)
    detail = [
        f"{beds} bed" if beds else "",
        f"{baths} bath" if baths else "",
        f"{size} sq ft" if size else "",
        f"market rent ${float(rent):,.2f}" if rent is not None and rent != "" else "",
    ]
    tail = ", ".join(d for d in detail if d)
    return " — ".join(bits) + (f"\n{tail}" if tail else "")


async def _list_bill_payments_impl(bill_id: int) -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            payments = await api.bills.list_payments(int(bill_id))
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "read payments on that bill")
    if not payments:
        return f"No payments recorded against bill #{bill_id}."
    lines = [f"{len(payments)} payment(s) on bill #{bill_id}:"]
    for pay in payments[:25]:
        date = getattr(pay, "date", None) or getattr(pay, "entry_date", None)
        amount = getattr(pay, "total_amount", None)
        method = getattr(pay, "payment_method", None)
        # A missing amount renders as unknown, never as $0.00 — gotcha 25's
        # rule: an absent number must not launder into a real-looking figure.
        money = (
            f"${float(amount):,.2f}"
            if amount is not None and amount != ""
            else "amount not recorded"
        )
        lines.append(f"- {date or 'undated'}: {money}" + (f" ({method})" if method else ""))
    if len(payments) > 25:
        lines.append(f"…and {len(payments) - 25} more not shown.")
    return "\n".join(lines)


@function_tool
async def buildium_list_vendor_notes(vendor_id: int) -> str:
    """Read the notes recorded against one vendor in Buildium.

    Use this before adding a note, and to answer "what have we said about this
    vendor before?" or "did anyone log the last dispute with them?".

    Args:
        vendor_id: The Buildium vendor id.
    """
    return await _list_vendor_notes_impl(vendor_id)


@function_tool
async def buildium_get_task_history(task_id: int) -> str:
    """Read one task's status history — who changed it, when, and to what.

    Use this to answer "who last touched this?", "how long has it been sitting
    at In Progress?", or before changing a task's status, so the change is made
    with knowledge of what happened already.

    Args:
        task_id: The Buildium task id.
    """
    return await _task_history_impl(task_id)


@function_tool
async def buildium_list_lease_departures(lease_id: int) -> str:
    """Read the move-outs recorded on one lease.

    ⚠ Named "departures" rather than "move_outs" ON PURPOSE. The stress
    battery's write-verb net matches whole TOKENS, and ``move`` is a write verb
    — so ``buildium_list_move_outs`` was flagged as an unreviewed write and
    failed ``test_live_write_surface_is_exactly_the_reviewed_set``. The net is
    right: a read one token away from ``buildium_record_move_out`` is exactly
    the shape that should trip a safety check, and widening the net to admit it
    would weaken the thing that catches an unauthorized send. The test's own
    guidance is "tighten the tool name", so the name moved instead.

    Use this to confirm a move-out landed, or to answer "is anyone moving out
    of this unit?". A move-out recorded in error is undone with
    ``buildium_undo_move_out`` (registered live in PR #943, confirm-gated).

    ⚠ Confirming it LANDED is the point: on 2026-08-30 ``record_move_out``
    returned 2xx and created no move-out row (gotcha 70), so this read is the
    only thing that can tell the difference between a write and a receipt.

    Args:
        lease_id: The Buildium lease id.
    """
    return await _list_move_outs_impl(lease_id)


@function_tool
async def buildium_get_unit(unit_id: int) -> str:
    """Look up ONE unit's details — number, address, beds/baths, size, rent.

    For every unit at a property use `buildium_get_property`; for vacancies use
    `buildium_list_vacant_units`. This is the single-record read.

    Args:
        unit_id: The Buildium unit id.
    """
    return await _get_unit_impl(unit_id)


@function_tool
async def buildium_list_bill_payments(bill_id: int) -> str:
    """Read the payments applied to one bill.

    ⚠ This is the ONLY way to see whether a bill was paid — the local mirror
    carries no paid bills at all, so `unpaid_bills` cannot answer "when did we
    last pay this?" and says so. Use this for a specific bill; it is a
    per-record read, so do not loop it over a whole vendor's history.

    Args:
        bill_id: The Buildium bill id.
    """
    return await _list_bill_payments_impl(bill_id)


def _format_bill_detail(bill: Any) -> str:
    """Render one bill with the lines behind its total. Pure, so the money
    rules below are unit-testable without a Buildium client.

    ⚠ EVERY money read here goes through ``bill_amount`` / ``bill_is_paid``
    rather than the raw attribute, and the reason is that the raw attributes
    LOOK populated and are not. ``TotalAmount`` is absent on every production
    bill and ``IsPaid`` is present-and-NULL on all 131 mirrored rows (gotcha
    25). A tool that reads them directly renders ``$0.00`` and ``unpaid`` for
    a paid bill — which is the shape this whole drill-down exists to stop.
    """
    bits: list[str] = [f"Bill #{bill.id}"]
    if (vendor_id := getattr(bill, "vendor_id", None)) is not None:
        bits.append(f"vendor {vendor_id}")
    if (date_ := getattr(bill, "date", None)) is not None:
        bits.append(f"dated {_format_date(date_)}")
    if (due := getattr(bill, "due_date", None)) is not None:
        bits.append(f"due {_format_date(due)}")
    if ref := getattr(bill, "reference_number", None):
        bits.append(f"ref {ref}")

    # ⚠ TWO-VALUED, and the third branch I first wrote here was DEAD CODE.
    # ``bill_is_paid`` is annotated ``bool | None`` and cannot return None: it
    # falls back to "does it have a PaidDate", which is a yes or a no. A
    # "paid status not recorded" arm read as careful and could never fire, and
    # per gotcha 58 the answer to a branch that cannot change any output is to
    # delete it rather than pin a distinction that does not exist. The
    # decline-to-assert this tool DOES owe is on the amount, below.
    if bill_is_paid(bill):
        when = getattr(bill, "paid_date", None)
        bits.append(f"PAID{f' {_format_date(when)}' if when is not None else ''}")
    else:
        bits.append("UNPAID")

    total = bill_amount(bill)
    money = f"${total:,.2f}" if total is not None else "amount not recorded"
    header = " — ".join(bits) + f" — {money}"

    lines = getattr(bill, "lines", None) or []
    if not lines:
        # An absent ``Lines`` is not an empty bill: it is a bill whose lines
        # this response did not carry. Saying "no lines" would be gotcha 26's
        # absence claim, and the total above is DERIVED from those lines.
        return header + "\n(no line detail on this record)"

    out = [header, f"{len(lines)} line(s):"]
    for line in lines:
        lb: list[str] = []
        amt = getattr(line, "amount", None)
        lb.append(f"${float(amt):,.2f}" if isinstance(amt, (int, float)) else "unpriced")
        # ⚠ ``resolved_*``, never the raw alias. Buildium sends ``GLAccountId``
        # and ``PropertyId`` on every line as KEYS and populates NEITHER
        # (131/131 present, 0/131 populated — gotcha 53); the values live on
        # the nested ``GLAccount.Id`` / ``AccountingEntity.Id``.
        # The NAME when the wire carried one — "GL Repairs & Maintenance" is
        # an answer, "GL 4120" is a number the model has to go and resolve,
        # which is the per-record hop this drill-down exists to remove
        # (gotchas 41/50: make the line self-contained).
        if gl_name := getattr(line, "resolved_gl_account_name", None):
            lb.append(f"GL {gl_name}")
        elif (gl := getattr(line, "resolved_gl_account_id", None)) is not None:
            lb.append(f"GL {gl}")
        if (prop := getattr(line, "resolved_property_id", None)) is not None:
            lb.append(f"property {prop}")
        if (unit := getattr(line, "unit_id", None)) is not None:
            lb.append(f"unit {unit}")
        if memo := getattr(line, "memo", None):
            lb.append(str(memo))
        out.append("  - " + " · ".join(lb))
    out.append("")
    out.append(
        f"For what has been paid against this bill, call "
        f"`buildium_list_bill_payments` with bill_id={bill.id}."
    )
    return "\n".join(out)


async def _get_bill_impl(bill_id: int) -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            bill = await api.bills.get(int(bill_id))
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "read that bill")
    return _format_bill_detail(bill)


@function_tool
async def buildium_get_bill(bill_id: int) -> str:
    """Open ONE bill and show the line items behind its total.

    This is the drill-down for any "why is this bill this much?", "what GL
    account did that hit?", "which property does this bill belong to?" or
    "show me the detail on bill 4821" question. Every line carries its amount,
    GL account, property and unit, so a total can be traced to its parts.

    Get the bill id from `buildium_list_unpaid_bills` (each row ends with the
    id) or from `unpaid_bills`, the mirror rollup. For payments applied to the
    bill use `buildium_list_bill_payments`. This is a single-record read — for
    payables across the portfolio use the list tools, do not loop this.

    Args:
        bill_id: The Buildium bill id.
    """
    return await _get_bill_impl(bill_id)


def _format_bank_accounts(accounts: list[Any]) -> str:
    """Render the operating/trust accounts. Pure, so the money + masking
    rules are unit-testable.

    ⚠ NO ACCOUNT OR ROUTING NUMBER IS RENDERED, and that is not an oversight
    to correct later. ``BankAccount`` deliberately leaves ``AccountNumber``,
    ``AccountNumberUnmasked``, ``RoutingNumber``, ``CheckPrintingInfo`` and
    ``ElectronicPayments`` UNTYPED, and the mirror strips them from ``raw``
    before persisting — no bank credentials at rest. A reply is a surface
    those values must never reach, so this renders identity and balance only.
    """
    if not accounts:
        return "No bank accounts found in Buildium for this account."

    active = [a for a in accounts if getattr(a, "is_active", None) is not False]
    hidden = len(accounts) - len(active)
    shown = active or accounts

    out = [f"{len(shown)} bank account(s):"]
    for acct in shown:
        bits: list[str] = [str(getattr(acct, "name", None) or f"account {acct.id}")]
        if kind := getattr(acct, "bank_account_type", None):
            bits.append(str(kind))
        bal = getattr(acct, "balance", None)
        # Same rule as every other money read here: an absent balance is
        # stated, never rendered as $0.00 (gotcha 25).
        if isinstance(bal, (int, float)):
            bits.append(f"${float(bal):,.2f}")
        else:
            bits.append("balance not reported")
        if getattr(acct, "is_active", None) is False:
            bits.append("INACTIVE")
        bits.append(f"[ref:{acct.id}]")
        out.append("- " + " — ".join(bits))
    if hidden:
        out.append(f"\n({hidden} inactive account(s) not shown.)")
    return "\n".join(out)


async def _list_bank_accounts_impl() -> str:
    client, api, err = await _open_api()
    if err or api is None or client is None:
        return err or _NOT_CONNECTED
    try:
        async with client:
            accounts = await api.bank_accounts.list()
    except BuildiumApiError as exc:
        return _sanitize_buildium_error(exc, "list bank accounts")
    return _format_bank_accounts(list(accounts))


@function_tool
async def buildium_list_bank_accounts() -> str:
    """List the company's bank accounts with their current balances.

    Answers "what's in the operating account?", "how much cash do we have?"
    and "which trust account does this property use?". Returns every ACTIVE
    account in one call — this is a portfolio-wide read, not a per-record one.

    ⚠ Balances are Buildium's, as of its last reconciliation — this is not a
    live bank feed, and it is not a substitute for the bank's own statement.
    Account and routing numbers are deliberately never returned.

    A question that says GENERAL LEDGER, names a GL account, or asks for a
    balance AS OF a date wants ``buildium_get_gl_account_balances`` instead —
    that is the ledger view and it covers every account, not just the banks.
    """
    return await _list_bank_accounts_impl()


def buildium_tools() -> list[Any]:
    """Return the full set of Buildium tools to attach to the API agent."""
    return [
        # Counts
        buildium_count_properties,
        buildium_count_units,
        buildium_count_tenants,
        # Properties
        buildium_list_properties,
        buildium_get_property,
        # Tenants
        buildium_list_tenants,
        buildium_get_tenant,
        buildium_find_tenant_for_unit,
        # Leases
        buildium_list_active_leases,
        buildium_get_lease,
        buildium_list_recent_lease_transactions,
        buildium_list_upcoming_lease_expirations,  # PR #73
        buildium_get_rent_roll,  # PR #73
        # Units
        buildium_list_vacant_units,  # PR #73
        # Owners
        buildium_list_owners,
        buildium_get_owner,  # PR #73
        # Vendors
        buildium_list_vendors,
        buildium_get_vendor,
        # Read-back for writes the agent already makes (the census).
        buildium_list_vendor_notes,
        buildium_get_task_history,
        buildium_list_lease_departures,
        buildium_get_unit,
        buildium_list_bill_payments,
        buildium_list_vendor_categories,  # PR #77
        buildium_search_vendors,  # PR #77
        # Work orders
        buildium_list_open_work_orders,
        buildium_get_work_order,
        buildium_list_work_orders_for_unit,
        buildium_maintenance_history_by_unit,  # #7 wo-history
        buildium_open_maintenance_by_unit,  # portfolio-wide, one call (N+1 killer)
        # Staff (read — surfaces user ids for assignment)
        buildium_list_staff,
        # Tasks (read — surfaces task ids for status updates)
        buildium_list_tasks,
        # Requests (read — Buildium task subtypes)
        buildium_list_resident_requests,
        buildium_list_rental_owner_requests,
        buildium_list_contact_requests,
        # Communications (read — announcements, sent emails, phone logs)
        buildium_list_announcements,
        buildium_list_sent_emails,
        buildium_list_phone_logs,
        # Listings (read — leasing pipeline / advertised vacancies)
        buildium_list_listings,
        # Financial
        buildium_list_overdue_balances,
        buildium_list_unpaid_bills,  # PR #77 — AP (vendor bills), not rent delinquency
        buildium_get_bill,  # the drill-down: one bill's lines behind its total
        buildium_list_bank_accounts,  # operating/trust balances (no acct numbers)
        # General Ledger (read-only) — chart of accounts, balances, transactions
        buildium_list_gl_accounts,
        buildium_get_gl_account_balances,
        buildium_list_gl_transactions,
        # Writes — self-gated via run_guarded (confirm + idempotency + verify)
        buildium_create_to_do_task,  # PR #76
        buildium_create_work_order,  # work-order create
        buildium_update_task_status,  # status update
        buildium_close_work_order,  # close a work order (via its backing task)
        buildium_update_work_order,  # reassign vendor / priority / due date
        buildium_post_charge,  # post a charge to a lease ledger (financial write)
        buildium_record_move_out,  # file a tenant move-out (REVERSIBLE via undo)
        buildium_undo_move_out,  # …and the undo that reversibility claim rests on
        buildium_record_payment,  # record a resident payment (IMMUTABLE — role floor)
        # buildium_renew_lease also DARK until pilot — renewal creates a real
        # lease term with NO clean undo (irreversible), even though the field
        # floor is now live-confirmed (gotcha 22).
        buildium_add_task_note,  # add note (task)
        buildium_add_work_order_note,  # add note (work order)
        buildium_upload_document,  # upload a company document to Buildium (PR6)
        buildium_share_file,  # share/unshare a file with tenants/owners (PR6)
        # buildium_create_bill deliberately NOT registered (2026-07-06
        # surface-model decision): Occupella doesn't create bills for now.
        # The tool + impl + endpoint stay dormant for easy re-enable.
        buildium_add_vendor_note,  # add a note to a vendor
        # buildium_update_applicant_status deliberately DARK (founder directive
        # 2026-07-26): Occupella does NOT accept/reject/defer housing applications
        # at release. An AI that executes a screening DECISION is the SafeRent-
        # class Fair-Housing exposure (Louis v. SafeRent, $2.275M) — the vendor
        # itself can be an FHA defendant. The tool + impl stay defined but
        # unregistered; re-enable ONLY behind mandatory human-in-the-loop +
        # fair-housing controls + counsel sign-off (tasks #49/#56).
    ]
