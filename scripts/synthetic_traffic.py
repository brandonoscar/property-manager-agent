#!/usr/bin/env python3
"""AI-driven Buildium sandbox activity generator.

Picks one realistic property-management action per invocation and
executes it against the Buildium sandbox. Gemini is given the current
state (properties + open work orders) and asked to author one
realistic new work order; Helixis runs it.

Scope (Phase 1, CREATE-only narrow — temporary):
  * CREATE work orders ONLY. UPDATE and CLOSE are temporarily disabled
    so we can verify the end-to-end webhook pipeline before fixing the
    read-modify-write pattern Buildium's PUT /workorders requires.
    Buildium's CREATE-side requirements, discovered by trial against
    the sandbox:
      1. ``VendorId`` is required (PR #198 wires Gemini-picked vendors,
         with a fallback to a generic vendor row).
      2. ``EntryAllowed`` is required (PR #198 sends ``"Unknown"`` as
         the synthetic default).
      3. A parent Task is required — Buildium's data model is
         ``Task → WorkOrder``. POST /workorders must include either an
         embedded ``Task`` object or an existing ``TaskId``. We embed
         inline so the synthetic run stays single-call; the Task
         itself requires ``Title``, ``Description``, ``Priority``,
         ``Status``, and ``AssignedToUserId``. (The POST schema names
         this field ``Status`` even though the GET response calls the
         same field ``TaskStatus`` — see ``_DEFAULT_TASK_STATUS``.)
    The Phase 1.5 follow-up will re-enable UPDATE/CLOSE by fetching
    the current work order, merging the desired delta on top of the
    existing ``VendorId`` / ``EntryAllowed`` / parent-Task fields, and
    PUTing the full body back. Until that lands, only CREATE runs
    here, and it sends every required field explicitly (see
    ``_execute``).
  * Manual invocation only — `python scripts/synthetic_traffic.py`.
    No cron, no GitHub Actions workflow.
  * Reuses existing BuildiumClient + LiteLLM config.
  * Uses the same `HELIXIS_BUILDIUM__*` env vars Helixis already reads.

Hard safety rails:
  * Refuses to run if `HELIXIS_BUILDIUM__BASE_URL` does not contain
    "apisandbox" — belt-and-suspenders against pointing at prod.
  * One action per invocation. Exits after.
  * If there are already ``MAX_OPEN_WORK_ORDERS`` open work orders,
    exits 1 — this CREATE-only build cannot act when capped.
  * Malformed Gemini JSON → log and exit 1.
  * Non-2xx from Buildium → log body and exit 1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
from typing import Any

from helixis.buildium.api import BuildiumApi
from helixis.buildium.client import BuildiumClient
from helixis.buildium.errors import BuildiumApiError
from helixis.buildium.schemas import Property, Vendor, WorkOrder
from helixis.settings import Settings, get_settings

logger = logging.getLogger("synthetic_traffic")


# ── Constants ────────────────────────────────────────────────────────


MAX_OPEN_WORK_ORDERS = 50
"""Hard cap on open work orders. When the sandbox already holds this
many, the LLM is constrained to UPDATE or CLOSE — no new CREATE — so
a runaway loop can't balloon the sandbox state."""

# Statuses we treat as "open" for the cap + listing. Buildium's exact
# status vocabulary varies; this set is the conservative interpretation:
# anything not explicitly completed/closed/cancelled is in flight.
_TERMINAL_STATUSES = {"Completed", "Closed", "Cancelled"}

_VALID_ACTIONS = {"CREATE", "UPDATE", "CLOSE"}
_DEFAULT_PRIORITIES = {"Low", "Normal", "High"}

# Buildium requires ``VendorId`` on every work-order CREATE; if Gemini
# picks a vendor that isn't in the active set (hallucination or stale
# context), fall back to this account-wide generic vendor.
_FALLBACK_VENDOR_ID = 70239

# Buildium's POST /workorders requires the embedded ``Task`` object to
# carry an ``AssignedToUserId`` pointing at a Buildium staff user. We
# hardcode the sandbox's Maintenance Manager (Vernon Amerson, id 70271)
# whose role is scoped to "Tasks only" — the natural assignee for a
# maintenance work order. Hardcoding avoids a per-run GET /v1/users
# call; the synthetic traffic generator doesn't need user-routing
# realism, just a valid id.
_FALLBACK_ASSIGNED_USER_ID = 70271

# Seconds between events in a --count run: Buildium fires the webhook
# per create, and spacing keeps the receiver/planner from claiming one
# giant batch (more realistic arrival pattern, too).
_BETWEEN_EVENTS_SECONDS = 3.0

# Initial status for the embedded parent Task. The value ``"New"``
# matches what GET /v1/tasks shows on freshly-created rows and is
# accepted by the sandbox's CREATE endpoint.
#
# Read/write field-name asymmetry footgun — Buildium uses different
# JSON keys for the same field on read vs. write:
#   * GET  /v1/tasks      → ``"TaskStatus": "New"``
#   * POST /v1/workorders → expects ``"Status"`` inside ``Task`` (per
#                           the WorkOrderPostMessageTask schema)
# Sending ``"TaskStatus"`` on the POST body returns a single-field 422
# ``Task.Status — "The value is missing or invalid."`` (PR #200 hit
# this; PR #201 fixed it). Do NOT rename this constant — only the
# wire key inside ``body["Task"]`` is the POST-side name.
_DEFAULT_TASK_STATUS = "New"


# ── Safety gates ─────────────────────────────────────────────────────


def _assert_sandbox(settings: Settings) -> None:
    """Refuse to run against anything that isn't the Buildium sandbox.

    The base URL is the operator-facing signal that's hardest to typo
    into prod by accident — Buildium's sandbox host literally has
    ``apisandbox`` in the DNS name, so a substring check rejects every
    plausible prod URL.
    """
    base = (settings.buildium.base_url or "").lower()
    if "apisandbox" not in base:
        sys.stderr.write(
            f"synthetic_traffic: refusing to run — "
            f"HELIXIS_BUILDIUM__BASE_URL is {settings.buildium.base_url!r}, "
            "which does not contain 'apisandbox'. This script only runs "
            "against the Buildium sandbox.\n"
        )
        sys.exit(2)


def _assert_buildium_creds(settings: Settings) -> None:
    if not settings.buildium.client_id or not settings.buildium.client_secret:
        sys.stderr.write(
            "synthetic_traffic: HELIXIS_BUILDIUM__CLIENT_ID / CLIENT_SECRET must be set.\n"
        )
        sys.exit(2)


def _assert_gemini_key(settings: Settings) -> None:
    if not settings.llm.google_api_key:
        sys.stderr.write(
            "synthetic_traffic: HELIXIS_LLM__GOOGLE_API_KEY must be set "
            "(Gemini is the only LLM provider configured for this script).\n"
        )
        sys.exit(2)


# ── Buildium reads ───────────────────────────────────────────────────


def _is_open(wo: WorkOrder) -> bool:
    return (wo.status or "") not in _TERMINAL_STATUSES


async def _list_properties(api: BuildiumApi) -> list[Property]:
    return await api.rentals.list_properties()


async def _list_active_vendors(api: BuildiumApi) -> list[Vendor]:
    """Return ``IsActive=true`` vendors usable as ``VendorId`` on CREATE.

    Buildium's sandbox carries inactive/archived vendor rows that the
    work-orders endpoint will reject with 422; we filter in Python
    because the ``IsActive=`` query param is unreliable on the sandbox.
    """
    vendors = await api.vendors.list(limit=50)
    return [v for v in vendors if v.is_active]


async def _list_open_work_orders(api: BuildiumApi) -> list[WorkOrder]:
    """Return work orders Buildium considers in flight.

    Pulls every work order (paginated by the client) and filters in
    Python rather than relying on a server-side ``status=`` filter —
    Buildium's sandbox sometimes returns rows with null/odd statuses
    that the status filter drops silently.
    """
    work_orders = await api.work_orders.list()
    return [wo for wo in work_orders if _is_open(wo)]


# ── Prompt construction ──────────────────────────────────────────────


def _build_prompt(
    properties: list[Property],
    open_work_orders: list[WorkOrder],
    active_vendors: list[Vendor],
) -> str:
    """Compose the Gemini prompt for CREATE-only work-order generation.

    Temporary scope narrowing (see module docstring): this build only
    exercises CREATE. The LLM is asked to author one realistic new
    work order against one of the listed properties AND assign one of
    the listed active vendors. The list of currently-open work orders
    is passed as context so the model can avoid duplicating in-flight
    issues.
    """
    property_lines = [{"id": p.id, "name": p.name or f"Property #{p.id}"} for p in properties[:50]]
    open_lines = [
        {
            "id": wo.id,
            "title": wo.title or f"Work order #{wo.id}",
            "status": wo.status or "Unknown",
        }
        for wo in open_work_orders[:50]
    ]
    vendor_lines = [
        {
            "id": v.id,
            "company_name": v.company_name or v.name or f"Vendor #{v.id}",
            "category": v.category.name if v.category else None,
        }
        for v in active_vendors
    ]

    return (
        "You are simulating a busy property management day for a "
        "residential property manager. Author exactly ONE realistic "
        "maintenance work order against one of the listed properties, "
        "and assign one of the listed active vendors.\n\n"
        "Return a single JSON object, no prose, no markdown fences:\n"
        "{\n"
        '  "action": "CREATE",\n'
        '  "reasoning": "<one sentence explaining the maintenance issue>",\n'
        '  "work_order_data": {\n'
        '     "property_id": <int, must be one of the listed properties>,\n'
        '     "vendor_id": <int, must be one of the listed active vendors>,\n'
        '     "title": "<short, realistic, e.g. \'Leaking kitchen faucet\'>",\n'
        '     "description": "<one or two sentences of realistic detail>",\n'
        '     "priority": "Low" | "Normal" | "High"\n'
        "  }\n"
        "}\n\n"
        "Vendor selection rules — match the vendor to the problem type:\n"
        "  * HVAC / heating / cooling / AC issue → HVAC vendor\n"
        "  * Pest / rodents / insects / infestation → pest control vendor\n"
        "  * Drywall / wall damage / hole in wall → drywall vendor\n"
        "  * Anything else (plumbing, electrical, appliance, general) → "
        "the generic / fallback vendor (the property-management company "
        "or 'Buildium' generic row).\n\n"
        "Avoid duplicating issues already represented in the open work "
        "orders below; pick a fresh, plausible scenario.\n\n"
        f"Properties (id, name): {json.dumps(property_lines)}\n\n"
        f"Active vendors (id, company_name, category): {json.dumps(vendor_lines)}\n\n"
        "Currently open work orders (context, do not duplicate): "
        f"{json.dumps(open_lines)}\n"
    )


def _build_resident_request_prompt(
    properties: list[Property],
    open_work_orders: list[WorkOrder],
) -> str:
    """Compose the Gemini prompt for a resident-request to-do task.

    A resident request is tenant-voiced: the description reads like what
    a tenant actually reported ("Maria in 2B says the bathroom sink has
    been dripping all week…"), which is exactly the content the inbox
    planner needs to propose real actions (message the tenant, open a
    work order, note the pattern).
    """
    property_lines = [{"id": p.id, "name": p.name or f"Property #{p.id}"} for p in properties[:50]]
    open_lines = [
        {"id": wo.id, "title": wo.title or f"Work order #{wo.id}"} for wo in open_work_orders[:50]
    ]
    return (
        "You are simulating a busy property management day. Author exactly "
        "ONE realistic RESIDENT REQUEST — a maintenance or tenant-issue "
        "report as it would arrive from a tenant — against one of the "
        "listed properties.\n\n"
        "Return a single JSON object, no prose, no markdown fences:\n"
        "{\n"
        '  "reasoning": "<one sentence>",\n'
        '  "task_data": {\n'
        '     "property_id": <int, must be one of the listed properties>,\n'
        '     "title": "<short + specific, e.g. \'No hot water — unit 2B\'>",\n'
        '     "description": "<2-3 sentences in the TENANT\'S voice: who is '
        "reporting (invent a plausible first+last name and unit), what is "
        'wrong, how long it has been happening, any urgency cue>",\n'
        '     "priority": "Low" | "Normal" | "High"\n'
        "  }\n"
        "}\n\n"
        "Vary the scenario: plumbing, electrical, appliances, noise "
        "complaints, pests, locks/keys, heating/cooling. Avoid duplicating "
        "the open work orders below.\n\n"
        f"Properties (id, name): {json.dumps(property_lines)}\n\n"
        f"Currently open work orders (do not duplicate): {json.dumps(open_lines)}\n"
    )


# ── LLM call ─────────────────────────────────────────────────────────


async def _call_gemini(settings: Settings, prompt: str) -> dict[str, Any]:
    """Send the prompt to Gemini and parse the JSON response.

    Uses ``litellm.acompletion`` directly. No tool use, no streaming —
    a single structured request. The model name resolution mirrors
    ``helixis.llm.router.get_reasoning_model``: read the configured
    reasoning model from settings, fall back to ``llm.default_model``.
    The Helixis-policy gate (Gemini-only) is enforced upstream at app
    boot via ``assert_no_openai_default``; this script trusts settings.
    """
    import litellm

    reasoning_cfg = settings.llm.models.get("reasoning") or {}
    model_name = reasoning_cfg.get("model") or settings.llm.default_model

    response = await litellm.acompletion(
        model=model_name,
        api_key=settings.llm.google_api_key,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.7,
    )
    content = response.choices[0].message.content or ""
    logger.info("gemini raw response: %s", content)

    try:
        parsed: dict[str, Any] = json.loads(content)
        return parsed
    except json.JSONDecodeError as exc:
        logger.error("gemini returned malformed JSON: %s — raw: %r", exc, content)
        sys.exit(1)


# ── Decision validation ──────────────────────────────────────────────


def _validate_decision(
    decision: dict[str, Any],
    *,
    property_ids: set[int],
    open_work_order_ids: set[int],
    active_vendor_ids: set[int],
    forbid_create: bool,
) -> None:
    """Type/shape-check the LLM's choice. Exit 1 on any violation.

    The LLM is generally reliable, but we still verify before sending
    anything to Buildium — a malformed body to ``POST /workorders``
    burns API quota and noises up the sandbox.
    """
    action = decision.get("action")
    if action not in _VALID_ACTIONS:
        logger.error("invalid action %r (expected one of %s)", action, _VALID_ACTIONS)
        sys.exit(1)

    if action == "CREATE" and forbid_create:
        logger.error(
            "model chose CREATE despite the forbid_create gate — refusing",
        )
        sys.exit(1)

    data = decision.get("work_order_data") or {}
    if not isinstance(data, dict):
        logger.error("work_order_data must be an object; got %r", type(data).__name__)
        sys.exit(1)

    if action == "CREATE":
        pid = data.get("property_id")
        if not isinstance(pid, int) or pid not in property_ids:
            logger.error("CREATE requires a property_id from the listed set; got %r", pid)
            sys.exit(1)
        if not data.get("title") or not data.get("description"):
            logger.error("CREATE requires non-empty title + description")
            sys.exit(1)
        vid = data.get("vendor_id")
        if not isinstance(vid, int) or vid not in active_vendor_ids:
            logger.warning(
                "vendor_id %r not in active vendor set — falling back to %d",
                vid,
                _FALLBACK_VENDOR_ID,
            )
            data["vendor_id"] = _FALLBACK_VENDOR_ID
        priority = data.get("priority")
        if priority and priority not in _DEFAULT_PRIORITIES:
            logger.warning(
                "priority %r not in %s — passing through to Buildium anyway",
                priority,
                _DEFAULT_PRIORITIES,
            )
        return

    # UPDATE / CLOSE
    wo_id = data.get("work_order_id")
    if not isinstance(wo_id, int) or wo_id not in open_work_order_ids:
        logger.error(
            "%s requires a work_order_id from the listed open set; got %r",
            action,
            wo_id,
        )
        sys.exit(1)
    new_status = data.get("new_status")
    if not isinstance(new_status, str) or not new_status:
        logger.error("%s requires a non-empty new_status", action)
        sys.exit(1)
    if action == "CLOSE" and new_status not in _TERMINAL_STATUSES:
        # Force the close to actually close even if the LLM picked
        # something weird — the action keyword is the source of truth.
        logger.warning("CLOSE picked new_status=%r; overriding to 'Completed'", new_status)
        data["new_status"] = "Completed"


# ── Action execution ─────────────────────────────────────────────────


async def _execute(api: BuildiumApi, decision: dict[str, Any]) -> tuple[int, str]:
    """Run the chosen action. Returns ``(work_order_id, action_str)``."""
    action = decision["action"]
    data = decision["work_order_data"]

    if action == "CREATE":
        body: dict[str, Any] = {
            "PropertyId": data["property_id"],
            "Title": data["title"],
            "Description": data["description"],
            "VendorId": data["vendor_id"],
            # Buildium rejects CREATE with 422 if EntryAllowed is
            # missing. "Unknown" is the safe default for synthetic
            # traffic — a real PM would gather this from the tenant.
            "EntryAllowed": "Unknown",
            # Buildium's data model is Task → WorkOrder; the work order
            # has no independent existence. POST /workorders requires
            # either an embedded ``Task`` object or an existing
            # ``TaskId``. We embed inline so the run stays single-call.
            # Title/Description/Priority/PropertyId mirror the work
            # order's values; TaskStatus + AssignedToUserId come from
            # synthetic defaults.
            "Task": {
                "Title": data["title"],
                "Description": data["description"],
                "Priority": data.get("priority", "Normal"),
                "Status": _DEFAULT_TASK_STATUS,
                "AssignedToUserId": _FALLBACK_ASSIGNED_USER_ID,
                "PropertyId": data["property_id"],
            },
        }
        if data.get("priority"):
            body["Priority"] = data["priority"]
        result = await api.work_orders.create(body)
        return result.id, "CREATE"

    # UPDATE / CLOSE
    wo_id = data["work_order_id"]
    body = {"Status": data["new_status"]}
    result = await api.work_orders.update(wo_id, body)
    return result.id, action


# ── Main ─────────────────────────────────────────────────────────────


async def main(*, count: int = 1, kind: str = "mix") -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings = get_settings()
    _assert_sandbox(settings)
    _assert_buildium_creds(settings)
    _assert_gemini_key(settings)

    logger.info(
        "starting against base_url=%s account_id=%s",
        settings.buildium.base_url,
        settings.buildium.account_id,
    )

    async with BuildiumClient(settings.buildium) as client:
        api = BuildiumApi(client)

        properties = await _list_properties(api)
        if not properties:
            logger.error("no properties in sandbox — cannot generate traffic")
            return 1
        active_vendors = await _list_active_vendors(api)
        if not active_vendors:
            logger.error(
                "no active vendors in sandbox — Buildium rejects CREATE with "
                "422 unless VendorId is set. Add at least one IsActive=true "
                "vendor before re-running.",
            )
            return 1
        open_work_orders = await _list_open_work_orders(api)
        logger.info(
            "state snapshot: properties=%d active_vendors=%d open_work_orders=%d",
            len(properties),
            len(active_vendors),
            len(open_work_orders),
        )

        if len(open_work_orders) >= MAX_OPEN_WORK_ORDERS:
            # CREATE-only build cannot fall back to UPDATE/CLOSE — exit
            # with an operator-actionable message instead of acting.
            logger.error(
                "open work order cap reached (%d >= %d) — this CREATE-only "
                "build cannot act. Close work orders manually, or wait for "
                "the Phase 1.5 PR that re-enables UPDATE/CLOSE.",
                len(open_work_orders),
                MAX_OPEN_WORK_ORDERS,
            )
            return 1

        # Random property suggestion is implicit — Gemini sees the full
        # list (truncated to 50 to keep the prompt cheap) and picks one.
        # If the list is longer, we shuffle so a different subset is
        # visible each invocation, reducing model bias toward the same
        # first-N properties.
        if len(properties) > 50:
            properties = random.sample(properties, 50)

        created = 0
        for i in range(count):
            if i > 0:
                await asyncio.sleep(_BETWEEN_EVENTS_SECONDS)
            event_kind = kind if kind != "mix" else _pick_mix_kind()
            logger.info("event %d/%d: kind=%s", i + 1, count, event_kind)

            if event_kind == "resident-request":
                ok = await _run_resident_request(api, settings, properties, open_work_orders)
            elif event_kind == "applicant":
                ok = await _run_applicant(api, settings, properties)
            elif event_kind == "bill":
                ok = await _run_bill(api, settings, properties, active_vendors)
            elif event_kind == "property":
                ok = await _run_property(api, settings)
            else:
                ok = await _run_work_order(
                    api, settings, properties, open_work_orders, active_vendors
                )
            if ok:
                created += 1

        logger.info("done: %d/%d events created", created, count)
        return 0 if created == count else 1


async def _run_work_order(
    api: BuildiumApi,
    settings: Settings,
    properties: list[Property],
    open_work_orders: list[WorkOrder],
    active_vendors: list[Vendor],
) -> bool:
    prompt = _build_prompt(properties, open_work_orders, active_vendors)
    decision = await _call_gemini(settings, prompt)
    # Defense in depth: this build only exercises CREATE. Force the
    # action regardless of what the LLM emitted; if the LLM ignored
    # the prompt and returned an UPDATE/CLOSE-shaped payload, the
    # downstream CREATE validation will fail loudly.
    decision["action"] = "CREATE"
    logger.info("gemini work order: reasoning=%r", decision.get("reasoning"))

    _validate_decision(
        decision,
        property_ids={p.id for p in properties},
        open_work_order_ids={wo.id for wo in open_work_orders},
        active_vendor_ids={v.id for v in active_vendors},
        forbid_create=False,
    )

    try:
        wo_id, action = await _execute(api, decision)
    except BuildiumApiError as exc:
        # ``str(exc)`` includes the formatted "Buildium <status> <path>:
        # <message>" line that the client populates with the raw
        # response body — that's the diagnostic we want surfaced.
        logger.error(
            "buildium %s failed: status=%s request_id=%s detail=%s",
            decision["action"],
            exc.status,
            exc.request_id,
            str(exc),
        )
        return False

    logger.info("buildium ok: action=%s work_order_id=%s", action, wo_id)
    return True


async def _run_resident_request(
    api: BuildiumApi,
    settings: Settings,
    properties: list[Property],
    open_work_orders: list[WorkOrder],
) -> bool:
    """Author + create one tenant-voiced to-do task (resident request)."""
    prompt = _build_resident_request_prompt(properties, open_work_orders)
    decision = await _call_gemini(settings, prompt)
    data = decision.get("task_data") or {}
    logger.info("gemini resident request: reasoning=%r", decision.get("reasoning"))

    pid = data.get("property_id")
    if not isinstance(pid, int) or pid not in {p.id for p in properties}:
        logger.error("resident request requires a listed property_id; got %r", pid)
        return False
    if not data.get("title") or not data.get("description"):
        logger.error("resident request requires non-empty title + description")
        return False
    priority = data.get("priority")
    if priority not in _DEFAULT_PRIORITIES:
        priority = "Normal"

    try:
        result = await api.tasks.create(
            fields={
                "Title": str(data["title"]),
                "Description": str(data["description"]),
                "Priority": priority,
                "TaskStatus": _DEFAULT_TASK_STATUS,
                "AssignedToUserId": _FALLBACK_ASSIGNED_USER_ID,
                "PropertyId": pid,
            }
        )
    except BuildiumApiError as exc:
        logger.error(
            "buildium resident-request CREATE failed: status=%s request_id=%s detail=%s",
            exc.status,
            exc.request_id,
            str(exc),
        )
        return False

    logger.info("buildium ok: resident request task_id=%s", result.get("Id"))
    return True


def _pick_mix_kind() -> str:
    """Weighted mix — mostly requests/work orders (the inbox stars),
    with a sprinkle of applicants, bills, and the occasional property."""
    return random.choices(
        ["work-order", "resident-request", "applicant", "bill", "property"],
        weights=[30, 30, 15, 15, 10],
    )[0]


async def _run_applicant(
    api: BuildiumApi,
    settings: Settings,
    properties: list[Property],
) -> bool:
    """Create one realistic rental applicant (fires Applicant.Created).

    Wire shape grounded on the LeadSimple ``CreateApplicantRequest``
    doc (2026-07-08): FirstName + LastName + SendRentalApplicationEmail
    are required; UnitId/Email optional. SendRentalApplicationEmail is
    PINNED false — synthetic applicants must never be emailed — and the
    email domain is always example.com (reserved, undeliverable).
    """
    prop = random.choice(properties)
    unit_id: int | None = None
    try:
        units = await api.rentals.list_units(propertyids=[prop.id], limit=20)
        if units:
            unit_id = random.choice(units).id
    except BuildiumApiError as exc:
        logger.warning("unit lookup for applicant failed (continuing without): %s", exc)

    prompt = (
        "Invent ONE realistic rental applicant (plausible US name). Return "
        'a single JSON object, no prose: {"first_name": "...", "last_name": "..."}'
    )
    decision = await _call_gemini(settings, prompt)
    first = str(decision.get("first_name") or "").strip() or "Alex"
    last = str(decision.get("last_name") or "").strip() or "Rivera"

    body: dict[str, Any] = {
        "FirstName": first[:50],
        "LastName": last[:50],
        "Email": f"{first}.{last}@example.com".lower().replace(" ", ""),
        "SendRentalApplicationEmail": False,
    }
    if unit_id is not None:
        body["UnitId"] = unit_id

    try:
        result = await api.client.request("POST", "/applicants", body=body)
    except BuildiumApiError as exc:
        logger.error("buildium applicant CREATE failed: status=%s detail=%s", exc.status, str(exc))
        return False
    applicant_id = result.get("Id") if isinstance(result, dict) else None
    logger.info("buildium ok: applicant id=%s (%s %s)", applicant_id, first, last)
    return True


async def _run_bill(
    api: BuildiumApi,
    settings: Settings,
    properties: list[Property],
    active_vendors: list[Vendor],
) -> bool:
    """Create one realistic vendor bill (fires Bill.Created).

    Body shape mirrors the shipped ``buildium_create_bill`` agent tool
    (the exact wire contract chat uses): Date/DueDate/VendorId + one
    line with AccountingEntity(Rental)/GlAccountId/Amount.
    """
    from datetime import UTC, date, datetime, timedelta

    try:
        gl_accounts = await api.general_ledger.list_accounts(accounttypes=["Expense"])
    except BuildiumApiError as exc:
        logger.error("GL account lookup failed — cannot create a bill: %s", exc)
        return False
    gl_active = [g for g in gl_accounts if g.is_active is not False]
    if not gl_active:
        logger.error("no active Expense GL accounts — cannot create a bill")
        return False

    prop = random.choice(properties)
    vendor = random.choice(active_vendors)
    prompt = (
        "Invent ONE realistic property-maintenance vendor bill. Return a "
        "single JSON object, no prose: "
        '{"amount": <float 40-900, realistic e.g. 185.00>, '
        '"memo": "<what the bill was for, one short phrase>"}'
    )
    decision = await _call_gemini(settings, prompt)
    try:
        amount = round(float(decision.get("amount") or 0), 2)
    except (TypeError, ValueError):
        amount = 0.0
    if not 1 <= amount <= 2000:
        amount = round(random.uniform(60, 450), 2)
    memo = str(decision.get("memo") or "Service call")[:240]

    today: date = datetime.now(UTC).date()
    body: dict[str, Any] = {
        "Date": today.isoformat(),
        "DueDate": (today + timedelta(days=30)).isoformat(),
        "VendorId": vendor.id,
        "Memo": memo,
        "Lines": [
            {
                "AccountingEntity": {"Id": prop.id, "AccountingEntityType": "Rental"},
                "GlAccountId": random.choice(gl_active).id,
                "Amount": amount,
                "Memo": memo,
            }
        ],
    }
    try:
        result = await api.bills.create(body)
    except BuildiumApiError as exc:
        logger.error("buildium bill CREATE failed: status=%s detail=%s", exc.status, str(exc))
        return False
    logger.info("buildium ok: bill id=%s amount=%.2f", result.get("Id"), amount)
    return True


async def _run_property(api: BuildiumApi, settings: Settings) -> bool:
    """Create one realistic rental property with 2-4 units.

    Wire shape grounded on the LeadSimple ``CreateRentalPropertyRequest``
    doc (2026-07-08): Name + Address{AddressLine1, Country} +
    RentalSubType + OperatingBankAccountId required; Units optional
    (max 100).
    """
    try:
        bank_accounts = await api.bank_accounts.list(limit=10)
    except BuildiumApiError as exc:
        logger.error("bank account lookup failed — cannot create a property: %s", exc)
        return False
    if not bank_accounts:
        logger.error("no bank accounts in sandbox — POST /rentals requires one")
        return False

    prompt = (
        "Invent ONE realistic small US rental property. Return a single "
        "JSON object, no prose:\n"
        "{\n"
        '  "name": "<street-style name, e.g. \'218 Birchwood Court\'>",\n'
        '  "address_line1": "<street address matching the name>",\n'
        '  "city": "<US city>", "state": "<2-letter state>",\n'
        '  "postal_code": "<5-digit zip>",\n'
        '  "sub_type": "CondoTownhome" | "MultiFamily" | "SingleFamily",\n'
        '  "year_built": <int 1950-2020>,\n'
        '  "units": [{"unit_number": "<e.g. 1A>", "market_rent": <int 800-2500>}, ...2-4 units]\n'
        "}"
    )
    decision = await _call_gemini(settings, prompt)
    name = str(decision.get("name") or "").strip()
    line1 = str(decision.get("address_line1") or "").strip()
    if not name or not line1:
        logger.error("property generation missing name/address — skipping")
        return False
    sub_type = decision.get("sub_type")
    if sub_type not in {"CondoTownhome", "MultiFamily", "SingleFamily"}:
        sub_type = "MultiFamily"
    units_in = decision.get("units") or []
    units: list[dict[str, Any]] = []
    for u in units_in[:4]:
        if isinstance(u, dict) and u.get("unit_number"):
            unit: dict[str, Any] = {"UnitNumber": str(u["unit_number"])[:20]}
            rent = u.get("market_rent")
            if isinstance(rent, (int, float)) and 0 < rent < 20000:
                unit["MarketRent"] = rent
            units.append(unit)

    body: dict[str, Any] = {
        "Name": name[:127],
        "Address": {
            "AddressLine1": line1[:100],
            "City": str(decision.get("city") or "Springfield")[:64],
            "State": str(decision.get("state") or "OH")[:32],
            "PostalCode": str(decision.get("postal_code") or "45501")[:10],
            "Country": "UnitedStates",
        },
        "RentalSubType": sub_type,
        "OperatingBankAccountId": bank_accounts[0].id,
    }
    year = decision.get("year_built")
    if isinstance(year, int) and 1000 <= year <= 2026:
        body["YearBuilt"] = year
    if units:
        body["Units"] = units

    try:
        result = await api.client.request("POST", "/rentals", body=body)
    except BuildiumApiError as exc:
        logger.error("buildium property CREATE failed: status=%s detail=%s", exc.status, str(exc))
        return False
    prop_id = result.get("Id") if isinstance(result, dict) else None
    logger.info("buildium ok: property id=%s name=%r units=%d", prop_id, name, len(units))
    return True


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fire realistic sandbox events (work orders + resident "
            "requests) at the webhook pipeline."
        )
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="How many events to create (default 1, max 10 per run).",
    )
    parser.add_argument(
        "--kind",
        choices=["work-order", "resident-request", "applicant", "bill", "property", "mix"],
        default="mix",
        help="Event type to create; 'mix' randomizes per event.",
    )
    args = parser.parse_args()
    args.count = max(1, min(args.count, 10))
    return args


if __name__ == "__main__":
    _args = _parse_args()
    sys.exit(asyncio.run(main(count=_args.count, kind=_args.kind)))
