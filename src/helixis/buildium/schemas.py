"""Minimal pydantic schemas for Buildium responses.

We only type the fields we actively consume. Unknown fields are allowed
so Buildium adding columns won't break deserialization. When a handler
needs a specific new field, extend the relevant model.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BuildiumModel(BaseModel):
    """Base — permissive with unknown fields."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Address(BuildiumModel):
    address_line1: str | None = Field(default=None, alias="AddressLine1")
    address_line2: str | None = Field(default=None, alias="AddressLine2")
    city: str | None = Field(default=None, alias="City")
    state: str | None = Field(default=None, alias="State")
    postal_code: str | None = Field(default=None, alias="PostalCode")
    country: str | None = Field(default=None, alias="Country")


class PhoneNumber(BuildiumModel):
    number: str | None = Field(default=None, alias="Number")
    type: str | None = Field(default=None, alias="Type")


class Property(BuildiumModel):
    id: int = Field(alias="Id")
    name: str | None = Field(default=None, alias="Name")
    rental_sub_type: str | None = Field(default=None, alias="RentalSubType")
    address: Address | None = Field(default=None, alias="Address")
    rental_owner_ids: list[int] | None = Field(default=None, alias="RentalOwnerIds")
    # Sandbox probe 2026-07-05: present on every /rentals row — promoted to a
    # typed mirror column for cockpit active-property filtering (0039).
    is_active: bool | None = Field(default=None, alias="IsActive")


class Unit(BuildiumModel):
    id: int = Field(alias="Id")
    # Buildium can return units with PropertyId omitted on rare draft / soft-
    # deleted rows we've observed in the sandbox. Loosened so a single odd
    # row doesn't 500 the entire list call.
    property_id: int | None = Field(default=None, alias="PropertyId")
    unit_number: str | None = Field(default=None, alias="UnitNumber")
    address: Address | None = Field(default=None, alias="Address")
    market_rent: float | None = Field(default=None, alias="MarketRent")
    # Sandbox probe 2026-07-05: Buildium's own occupancy flag — promoted so
    # the cockpit can cross-check the active-lease heuristic (0039).
    is_occupied: bool | None = Field(default=None, alias="IsUnitOccupied")


class TenantLeaseSummary(BuildiumModel):
    """Mini-lease object embedded inside a Tenant payload."""

    id: int = Field(alias="Id")
    status: str | None = Field(default=None, alias="Status")


class Tenant(BuildiumModel):
    id: int = Field(alias="Id")
    first_name: str | None = Field(default=None, alias="FirstName")
    last_name: str | None = Field(default=None, alias="LastName")
    email: str | None = Field(default=None, alias="Email")
    alternate_email: str | None = Field(default=None, alias="AlternateEmail")
    phone_numbers: list[PhoneNumber] | None = Field(default=None, alias="PhoneNumbers")
    address: Address | None = Field(default=None, alias="Address")
    leases: list[TenantLeaseSummary] | None = Field(default=None, alias="Leases")


class RentalOwner(BuildiumModel):
    id: int = Field(alias="Id")
    first_name: str | None = Field(default=None, alias="FirstName")
    last_name: str | None = Field(default=None, alias="LastName")
    company_name: str | None = Field(default=None, alias="CompanyName")
    email: str | None = Field(default=None, alias="Email")
    property_ids: list[int] | None = Field(default=None, alias="PropertyIds")


class CurrentTenant(BuildiumModel):
    """Mini-tenant object embedded inside a Lease payload's CurrentTenants."""

    id: int = Field(alias="Id")
    first_name: str | None = Field(default=None, alias="FirstName")
    last_name: str | None = Field(default=None, alias="LastName")
    email: str | None = Field(default=None, alias="Email")


class LeaseTenantStatus(BuildiumModel):
    """Mini-tenant object embedded inside a Lease payload's Tenants list."""

    id: int = Field(alias="Id")
    status: str | None = Field(default=None, alias="Status")
    move_in_date: datetime | None = Field(default=None, alias="MoveInDate")


class LeaseAccountDetails(BuildiumModel):
    """Money block nested inside a Buildium Lease payload.

    Per Buildium's developer docs, ``/v1/leases`` and ``/v1/leases/{id}``
    return ``Rent`` and ``SecurityDeposit`` inside an ``AccountDetails``
    object — NOT at the top level. Pre-PR-#74 we declared ``rent`` as a
    top-level ``Field(alias='Rent')`` which silently parsed as ``None``
    against every real response. That's why the rent roll rendered
    ``$0.00`` for 21 active leases in Brandon's sandbox test."""

    rent: float | None = Field(default=None, alias="Rent")
    security_deposit: float | None = Field(default=None, alias="SecurityDeposit")


class Lease(BuildiumModel):
    id: int = Field(alias="Id")
    property_id: int | None = Field(default=None, alias="PropertyId")
    unit_id: int | None = Field(default=None, alias="UnitId")
    unit_number: str | None = Field(default=None, alias="UnitNumber")
    lease_from_date: datetime | None = Field(default=None, alias="LeaseFromDate")
    lease_to_date: datetime | None = Field(default=None, alias="LeaseToDate")
    status: str | None = Field(default=None, alias="LeaseStatus")
    lease_type: str | None = Field(default=None, alias="LeaseType")
    term_type: str | None = Field(default=None, alias="TermType")
    # PR #74 — Rent moved off the top level. The nested
    # ``LeaseAccountDetails`` is where Buildium actually returns the
    # value; we keep the ``rent`` attribute on Lease as a derived
    # accessor so the 18 existing call sites (``lease.rent``,
    # ``getattr(lease, "rent", None)``) keep working without edits.
    account_details: LeaseAccountDetails | None = Field(default=None, alias="AccountDetails")
    is_eviction_pending: bool | None = Field(default=None, alias="IsEvictionPending")
    current_tenants: list[CurrentTenant] | None = Field(default=None, alias="CurrentTenants")
    cosigners: list[CurrentTenant] | None = Field(default=None, alias="Cosigners")
    tenants: list[LeaseTenantStatus] | None = Field(default=None, alias="Tenants")
    current_number_of_occupants: int | None = Field(default=None, alias="CurrentNumberOfOccupants")
    payment_due_day: int | None = Field(default=None, alias="PaymentDueDay")

    @property
    def rent(self) -> float | None:
        """Convenience accessor — Buildium nests Rent inside
        AccountDetails. Returns ``None`` when the block is missing
        (older API versions, partial responses)."""
        if self.account_details is None:
            return None
        return self.account_details.rent

    @property
    def security_deposit(self) -> float | None:
        """The deposit named on the LEASE AGREEMENT — nested beside Rent.

        ⚠ Not a confirmation the money was received, and not the balance held
        in trust; the ledger is the authority for what is actually held.
        Reporting an agreed figure as an amount held is a confident false
        claim about money somebody is owed back (gotchas 41/51).

        ⚠ ``0.0`` is a VALUE, not an absence. Live-verified 2026-08-29 across
        134 mirrored leases: Buildium populates this on every row, and one
        account reads 0.00 on all 75 of its leases while two others carry
        nonzero values up to $1,800. So a zero means "Buildium says zero",
        which is a different fact from "we do not know" — ``None`` is
        reserved for the second (the AccountDetails block absent entirely).
        """
        if self.account_details is None:
            return None
        return self.account_details.security_deposit


class LeaseTransaction(BuildiumModel):
    id: int = Field(alias="Id")
    lease_id: int | None = Field(default=None, alias="LeaseId")
    date: datetime | None = Field(default=None, alias="Date")
    transaction_type: str | None = Field(default=None, alias="TransactionType")
    total_amount: float | None = Field(default=None, alias="TotalAmount")


class LeaseMoveOut(BuildiumModel):
    # ``Id`` is NOT always present. Prod-observed 2026-07-25: live
    # ``/leases/{leaseId}/moveouts`` rows come back as
    # ``{TenantId, MoveOutDate, NoticeGivenDate}`` with no ``Id`` at all —
    # a move-out is keyed by ``TenantId`` (gotcha 22: the create body is
    # ``{TenantId, MoveOutDate}`` and the undo is ``DELETE …/moveouts/{tenantId}``).
    # Typed ``int`` (required) it 422'd every ``Lease.MoveOut.Created`` webhook
    # (Sentry PYTHON-FASTAPI-2Y), which then retried forever. Optional so list
    # rows parse cleanly — same class as the WorkOrder/Unit PropertyId loosening.
    id: int | None = Field(default=None, alias="Id")
    # ``LeaseId`` is included on the parent envelope but Buildium's
    # ``/leases/{leaseId}/moveouts`` rows don't always echo it — it's
    # implied by the URL. Optional so list rows parse cleanly.
    lease_id: int | None = Field(default=None, alias="LeaseId")
    tenant_id: int | None = Field(default=None, alias="TenantId")
    move_out_date: datetime | None = Field(default=None, alias="MoveOutDate")
    notice_given_date: datetime | None = Field(default=None, alias="NoticeGivenDate")


class WorkOrderTaskRef(BuildiumModel):
    """Mini-task object nested inside a WorkOrder payload.

    Sandbox probe 2026-07-05: real /workorders rows carry NULL top-level
    PropertyId/UnitId — the live linkage is ``Task.UnitId`` (with the
    property resolved through the unit). Typed minimally for that lookup."""

    id: int | None = Field(default=None, alias="Id")
    unit_id: int | None = Field(default=None, alias="UnitId")
    # Sandbox probe 2026-07-13: the nested Task carries DueDate (date only) —
    # it is the ONLY date the /workorders endpoint exposes (no created/updated).
    due_date: date | None = Field(default=None, alias="DueDate")
    # Prod dogfood 2026-07-26 (WO 9006): a work order's Priority is
    # task-derived and, after ``Tasks.update``, moves on the NESTED
    # ``WorkOrder.Task.Priority`` while the WO's TOP-LEVEL ``Priority`` stays
    # frozen at creation (gotcha 20). So the nested value is the only reliable
    # place to READ a priority change back — typed here for the update-WO verify.
    priority: str | None = Field(default=None, alias="Priority")


class WorkOrder(BuildiumModel):
    id: int = Field(alias="Id")
    # Buildium can return work orders without a PropertyId for unassigned
    # or draft items. Pre-PR-#71 this was required and any such row 500'd
    # the entire ``list_open_work_orders`` call — broke Brandon's
    # "how many work orders are open" smoke test against the sandbox.
    property_id: int | None = Field(default=None, alias="PropertyId")
    unit_id: int | None = Field(default=None, alias="UnitId")
    title: str | None = Field(default=None, alias="Title")
    description: str | None = Field(default=None, alias="Description")
    status: str | None = Field(default=None, alias="Status")
    priority: str | None = Field(default=None, alias="Priority")
    category_id: int | None = Field(default=None, alias="CategoryId")
    assigned_to_user_id: int | None = Field(default=None, alias="AssignedToUserId")
    vendor_id: int | None = Field(default=None, alias="VendorId")
    # Sandbox probe 2026-07-13: /workorders exposes DueDate (date only) at the
    # top level AND on the nested Task; there is NO created/opened/updated date
    # on this endpoint (those live only on /tasks/{Task.Id}). So "overdue" is
    # answerable here; "opened N days ago" would require a per-task fetch.
    due_date: date | None = Field(default=None, alias="DueDate")
    task: WorkOrderTaskRef | None = Field(default=None, alias="Task")

    @property
    def effective_unit_id(self) -> int | None:
        """Unit linkage that survives the null-top-level reality (probe
        2026-07-05): prefer the top-level UnitId, fall back to Task.UnitId."""
        if self.unit_id is not None:
            return self.unit_id
        return self.task.unit_id if self.task else None

    @property
    def effective_due_date(self) -> date | None:
        """Due date, top-level first then the nested Task (probe 2026-07-13:
        both carry it; either can be null)."""
        if self.due_date is not None:
            return self.due_date
        return self.task.due_date if self.task else None


class OutstandingBalance(BuildiumModel):
    """One row of /v1/leases/outstandingbalances — money owed by a lease.

    PR #78 — schema correction. Brandon pulled the published Buildium
    OpenAPI spec from the LeadSimple/buildium SDK and confirmed the
    actual field name is ``TotalBalance`` (NOT ``OutstandingBalance``,
    which is what PR #73 typed). With the wrong alias every parsed
    row had ``outstanding_balance is None``; the downstream filter
    ``(getattr(b, "outstanding_balance", 0) or 0) > 0`` then dropped
    every row silently — partially explaining why Brandon's account
    returned an empty delinquency list across PR #74-#77 even when
    real rows came back from Buildium.

    Field surface mirrors ``LeaseOutstandingBalanceMessage`` in
    Buildium's spec, plus the GL-account breakdown
    ``Balances: Array<OutstandingBalancesLineMessage>``. Fields
    marked "not in spec — kept defensively" (``UnitNumber``,
    ``Status``, ``DaysLate``, ``RentAmount``) are not present in
    the published OpenAPI message and were probably hallucinated
    by an earlier PR or copied from a different endpoint; they're
    left Optional so they don't break legacy data but no reader
    should rely on them landing.
    """

    lease_id: int = Field(alias="LeaseId")
    property_id: int | None = Field(default=None, alias="PropertyId")
    unit_id: int | None = Field(default=None, alias="UnitId")
    # PR #78 — the real balance field per Buildium's spec.
    total_balance: float | None = Field(default=None, alias="TotalBalance")
    # PR #73 — aging buckets + eviction signals Buildium already
    # returns. Surfaces them so the model can answer "60+ day
    # delinquencies" and sanity-check evictions ↔ balances.
    balance_0_to_30_days: float | None = Field(default=None, alias="Balance0To30Days")
    balance_31_to_60_days: float | None = Field(default=None, alias="Balance31To60Days")
    balance_61_to_90_days: float | None = Field(default=None, alias="Balance61To90Days")
    balance_over_90_days: float | None = Field(default=None, alias="BalanceOver90Days")
    # PR #78 — per-GL-account breakdown. Typed as a permissive list
    # so the agent can sum / inspect entries without a hardcoded
    # OutstandingBalancesLineMessage sub-model gating us; if we ever
    # need the structured shape, this becomes ``list[BalancesLine]``.
    balances: list[Any] | None = Field(default=None, alias="Balances")
    past_due_email_sent_date: datetime | None = Field(default=None, alias="PastDueEmailSentDate")
    eviction_pending_date: datetime | None = Field(default=None, alias="EvictionPendingDate")
    is_notice_given: bool | None = Field(default=None, alias="IsNoticeGiven")
    # ── Not in published Buildium OpenAPI spec — kept defensively ──
    # Earlier PRs added these aliases assuming they came back on the
    # endpoint. They don't. Removing them is a small follow-up once
    # PR #77's diag confirms no live response carries them; for now
    # they default to None so a removal at the SDK level wouldn't
    # break callers that still look them up via ``getattr``.
    unit_number: str | None = Field(default=None, alias="UnitNumber")
    status: str | None = Field(default=None, alias="Status")
    days_late: int | None = Field(default=None, alias="DaysLate")
    rent_amount: float | None = Field(default=None, alias="RentAmount")


class TaskPropertyRef(BuildiumModel):
    """The property object nested in a /v1/tasks row.

    Production IGNORES ``?propertyids`` on /tasks (gotcha 24), so scoping
    tasks to a property is a client-side filter on this id — which means it
    has to be typed, not inferred.
    """

    id: int | None = Field(default=None, alias="Id")
    href: str | None = Field(default=None, alias="Href")


class Task(BuildiumModel):
    id: int = Field(alias="Id")
    title: str | None = Field(default=None, alias="Title")
    description: str | None = Field(default=None, alias="Description")
    status: str | None = Field(default=None, alias="Status")
    # The /v1/tasks endpoint (list + detail) carries the fields the nested
    # WorkOrder.Task does NOT — the created/updated timestamps and a flat
    # UnitId (sandbox probe 2026-07-13: {"UnitId": 30662, "TaskStatus":
    # "InProgress", "DueDate": "2025-04-07", "CreatedDateTime":
    # "2026-04-29T02:35:02Z", "LastUpdatedDateTime": "2026-04-29T02:35:02Z"}).
    # These are the ONLY source of a maintenance item's opened/updated date,
    # so history-by-unit / "open longest" reads go through tasks, not
    # /workorders. ``TaskStatus`` is the tasks-endpoint status name (the nested
    # work-order task uses "Status"); both kept so either shape parses.
    task_status: str | None = Field(default=None, alias="TaskStatus")
    # Live-verified across all 179 production task rows (2026-08-21): every
    # one carries Priority, and 171/179 carry a nested Property.Id — while
    # top-level ``Status`` is NULL on every single row. Typed here because
    # untyped extras are stored under their ORIGINAL casing (``Priority``,
    # not ``priority``), so reading them by the snake_case name silently
    # missed. "Is this task's priority right?" is unanswerable without these.
    priority: str | None = Field(default=None, alias="Priority")
    property: TaskPropertyRef | None = Field(default=None, alias="Property")
    unit_id: int | None = Field(default=None, alias="UnitId")
    due_date: date | None = Field(default=None, alias="DueDate")
    created_date_time: datetime | None = Field(default=None, alias="CreatedDateTime")
    last_updated_date_time: datetime | None = Field(default=None, alias="LastUpdatedDateTime")


# ── Vendor sub-models (PR #77) ────────────────────────────────────


class VendorCategory(BuildiumModel):
    """A category like "Plumbing", "HVAC", "Electrical". Buildium
    nests this on every vendor row AND exposes a separate
    ``/vendors/categories`` catalog for resolution."""

    id: int = Field(alias="Id")
    name: str | None = Field(default=None, alias="Name")


class VendorAddress(BuildiumModel):
    """Street address used by vendor responses. Distinct from the
    generic ``Address`` model only because Buildium ships a slightly
    richer shape on vendors (AddressLine3 + Country); kept separate
    to avoid retrofitting every Address-consumer in the codebase."""

    address_line_1: str | None = Field(default=None, alias="AddressLine1")
    address_line_2: str | None = Field(default=None, alias="AddressLine2")
    address_line_3: str | None = Field(default=None, alias="AddressLine3")
    city: str | None = Field(default=None, alias="City")
    state: str | None = Field(default=None, alias="State")
    postal_code: str | None = Field(default=None, alias="PostalCode")
    country: str | None = Field(default=None, alias="Country")


class VendorPhoneNumber(BuildiumModel):
    """One phone-number entry on a vendor.

    Buildium ships ``PhoneNumbers`` as a JSON **list** of
    ``{Number, Type}`` objects — same shape as the tenant schema — not
    the fixed ``{Home, Work, Mobile, Fax}`` object this module
    originally modeled. Empty lists are valid for vendors without any
    phone on file. ``Type`` is a free-form Buildium-side string
    (observed: ``"Office"``, ``"Cell"``, ``"Fax"``, ``"Home"``…) so
    we keep it unconstrained.
    """

    number: str | None = Field(default=None, alias="Number")
    type: str | None = Field(default=None, alias="Type")


class Vendor(BuildiumModel):
    """Vendor record.

    PR #77 — expanded from the previous (id, name, company_name,
    is_active) shape because the agent needs the full surface to
    answer "find me a plumber in Cleveland". Buildium's response
    includes ``Category`` (id + name like "Plumbing"), full
    ``Address``, ``PhoneNumbers`` (a list of ``{Number, Type}``
    entries, matching the tenant schema), and multiple emails — all
    stripped pre-PR-#77 so the "list vendors" tool only had names to
    work with. Extra fields stay permissive via ``BuildiumModel``'s
    ``extra="allow"``.
    """

    id: int = Field(alias="Id")
    name: str | None = Field(default=None, alias="Name")
    is_company: bool | None = Field(default=None, alias="IsCompany")
    first_name: str | None = Field(default=None, alias="FirstName")
    last_name: str | None = Field(default=None, alias="LastName")
    company_name: str | None = Field(default=None, alias="CompanyName")
    is_active: bool | None = Field(default=None, alias="IsActive")
    category: VendorCategory | None = Field(default=None, alias="Category")
    address: VendorAddress | None = Field(default=None, alias="Address")
    phone_numbers: list[VendorPhoneNumber] | None = Field(default=None, alias="PhoneNumbers")
    primary_email: str | None = Field(default=None, alias="PrimaryEmail")
    alternate_email: str | None = Field(default=None, alias="AlternateEmail")
    website: str | None = Field(default=None, alias="Website")
    tax_id: str | None = Field(default=None, alias="TaxId")
    account_number: str | None = Field(default=None, alias="AccountNumber")
    comments: str | None = Field(default=None, alias="Comments")


class BillLineAccountingEntity(BuildiumModel):
    """Where a bill line's money is charged. THE property association.

    ``AccountingEntityType`` is ``Rental`` for a property, ``Company`` for an
    overhead line; only the former resolves to a mirrored property.
    """

    id: int | None = Field(default=None, alias="Id")
    accounting_entity_type: str | None = Field(default=None, alias="AccountingEntityType")


class BillLineGLAccountRef(BuildiumModel):
    """The ledger account a bill line is booked to — the nested object that
    actually carries it.

    Buildium sends BOTH ``GLAccountId`` (flat) and ``GLAccount`` (nested) on
    every line, populates only the nested one, and the flat one is what the
    importer read. Typed minimally: the id is the join key, the name is what a
    manager reads ("Repairs", "Insurance").
    """

    id: int | None = Field(default=None, alias="Id")
    name: str | None = Field(default=None, alias="Name")


class BillLine(BuildiumModel):
    """One line on a bill.

    ⚠ **``PropertyId`` IS PRESENT ON EVERY PRODUCTION LINE AND NULL ON EVERY
    ONE OF THEM.** Live-verified across all 131 mirrored lines (2026-08-27):
    the key exists 131/131, carries a value 0/131, while
    ``AccountingEntity.Id`` carries one 131/131. So a presence check answers
    "yes, we have PropertyId" and the value is always None — the worst version
    of gotcha 25, because the field looks supported right up until you read it.

    This class's own docstring used to claim it "carries the PropertyId
    association needed for 'unpaid bills at <property>' filtering (PR #77)".
    That has never been true against production. ``entities.upsert_bill`` wrote
    ``line.property_id`` straight into ``helixis_bill_lines.property_id``, so
    that column was NULL on 100% of rows, and ``unpaid_bills``' property join —
    correct code — silently produced nothing. The visible cost was battery
    v10-100 q48 ("which property is costing me the most in repairs?"), which
    ran 177s across six tool calls and concluded *"the bills aren't linked to
    specific property addresses"*. They are. We were reading the wrong key.

    :attr:`resolved_property_id` is what callers must use. ``PropertyId`` is
    kept as a fallback rather than deleted: it is in the SDK contract, and if
    Buildium ever starts populating it a hard-coded switch to the other field
    would then be the bug.
    """

    id: int | None = Field(default=None, alias="Id")
    property_id: int | None = Field(default=None, alias="PropertyId")
    unit_id: int | None = Field(default=None, alias="UnitId")
    amount: float | None = Field(default=None, alias="Amount")
    memo: str | None = Field(default=None, alias="Memo")
    gl_account_id: int | None = Field(default=None, alias="GLAccountId")
    #: ⚠ SAME TRAP AS ``PropertyId``, ONE COLUMN OVER — read
    #: :attr:`resolved_gl_account_id`, never ``gl_account_id``.
    #: Live-verified across all 131 mirrored bill lines (2026-08-28):
    #: ``GLAccountId`` present 131/131, populated **0/131**; the nested
    #: ``GLAccount.Id`` populated **131/131**. So every line went into the
    #: mirror with a NULL category and ``spending_report``'s GL grouping
    #: returned one row labelled "Uncategorized" for the whole book.
    gl_account: BillLineGLAccountRef | None = Field(default=None, alias="GLAccount")
    accounting_entity: BillLineAccountingEntity | None = Field(
        default=None, alias="AccountingEntity"
    )

    @property
    def resolved_property_id(self) -> int | None:
        """The property this line is charged to, from whichever key carries it.

        ``PropertyId`` first so a future Buildium fix wins automatically;
        ``AccountingEntity.Id`` — the one that actually works today — second,
        and only when the entity is a ``Rental`` (a ``Company`` line is
        overhead and belongs to no property; treating its id as a property id
        would silently attribute overhead to whatever property shares that
        number).
        """
        if self.property_id is not None:
            return self.property_id
        entity = self.accounting_entity
        if entity is None or entity.id is None:
            return None
        if (entity.accounting_entity_type or "").strip().lower() != "rental":
            return None
        return entity.id

    @property
    def resolved_gl_account_id(self) -> int | None:
        """The ledger account this line is booked to, from whichever key
        carries it.

        Flat ``GLAccountId`` first so a future Buildium fix wins automatically;
        the nested ``GLAccount.Id`` — the one populated today — second. Exactly
        the shape of :attr:`resolved_property_id`, and for the same reason.

        ⚠ No entity-type guard here, unlike the property resolver. A GL account
        is a GL account whether the line is charged to a rental or to company
        overhead — the ``Rental``-only rule exists because a Company id is not
        a property id, and there is no equivalent confusion for a ledger code.
        """
        if self.gl_account_id is not None:
            return self.gl_account_id
        return self.gl_account.id if self.gl_account else None

    @property
    def resolved_gl_account_name(self) -> str | None:
        """The human-readable category ("Repairs", "Insurance"), when Buildium
        sends it. Only the nested object carries a name at all."""
        return self.gl_account.name if self.gl_account else None


class Bill(BuildiumModel):
    id: int = Field(alias="Id")
    vendor_id: int | None = Field(default=None, alias="VendorId")
    date: datetime | None = Field(default=None, alias="Date")
    due_date: datetime | None = Field(default=None, alias="DueDate")
    total_amount: float | None = Field(default=None, alias="TotalAmount")
    # ``IsPaid`` is PRESENT as a key on every production bill and NULL on
    # every one of them (live-verified across all 131 mirrored rows,
    # 2026-08-21). ``PaidDate`` is the field that actually carries the
    # answer: null = unpaid, set = paid. Use ``paid_status`` rather than
    # reading ``is_paid`` directly — a NULL there means "unknown", and
    # treating unknown as unpaid counted every paid bill as outstanding.
    is_paid: bool | None = Field(default=None, alias="IsPaid")
    # ``datetime``, not ``date``: this class has a FIELD named ``date``, which
    # shadows the type inside the class body and makes ``date | None`` resolve
    # to a FieldInfo at annotation-eval time. Matches the sibling date fields.
    paid_date: datetime | None = Field(default=None, alias="PaidDate")
    reference_number: str | None = Field(default=None, alias="ReferenceNumber")
    # PR #77 — Buildium returns ``Lines`` on every bill. Surfacing it
    # lets the agent answer "unpaid bills at 150 Main Ave" by filtering
    # on Lines[].PropertyId.
    lines: list[BillLine] | None = Field(default=None, alias="Lines")


class BillPayment(BuildiumModel):
    """One payment applied to a bill.

    Minimal — just enough to keep ``list_payments`` typed without
    locking down a schema that varies by environment. Extend when a
    caller needs more fields.
    """

    id: int = Field(alias="Id")
    bill_id: int | None = Field(default=None, alias="BillId")
    date: datetime | None = Field(default=None, alias="Date")
    total_amount: float | None = Field(default=None, alias="TotalAmount")


class BankAccount(BuildiumModel):
    """Buildium ``BankAccountMessage``.

    Field set verified against the LeadSimple SDK ``bank_account_message.rb``
    attribute map on 2026-07-06. The wire ALSO carries ``AccountNumber``,
    ``AccountNumberUnmasked``, ``RoutingNumber``, ``CheckPrintingInfo`` and
    ``ElectronicPayments`` — deliberately untyped here, and the mirror strips
    them from ``raw`` before persist (no bank credentials at rest).
    ``BankAccountType`` enum: Checking | Savings.
    """

    id: int = Field(alias="Id")
    name: str | None = Field(default=None, alias="Name")
    description: str | None = Field(default=None, alias="Description")
    bank_account_type: str | None = Field(default=None, alias="BankAccountType")
    country: str | None = Field(default=None, alias="Country")
    is_active: bool | None = Field(default=None, alias="IsActive")
    balance: float | None = Field(default=None, alias="Balance")


class File(BuildiumModel):
    id: int = Field(alias="Id")
    file_name: str | None = Field(default=None, alias="FileName")
    title: str | None = Field(default=None, alias="Title")
    size: int | None = Field(default=None, alias="Size")


class Applicant(BuildiumModel):
    """Buildium ``ApplicantMessage``.

    Field set verified against the LeadSimple/buildium SDK
    ``applicant_message.rb`` attribute map on 2026-07-05 — wire JSON is
    PascalCase. ``Status`` enum (verbatim from the SDK): Unknown, Undecided,
    Approved, Rejected, AddedToLease, Cancelled, Deferred, New, Draft,
    AddedToDraftLease. ``Applications`` also ships on the wire but is left
    untyped (permissive extras keep it in ``raw``) — Helixis doesn't consume
    per-application detail yet.
    """

    id: int = Field(alias="Id")
    applicant_group_id: int | None = Field(default=None, alias="ApplicantGroupId")
    property_id: int | None = Field(default=None, alias="PropertyId")
    unit_id: int | None = Field(default=None, alias="UnitId")
    tenant_id: int | None = Field(default=None, alias="TenantId")
    first_name: str | None = Field(default=None, alias="FirstName")
    last_name: str | None = Field(default=None, alias="LastName")
    email: str | None = Field(default=None, alias="Email")
    phone_numbers: list[PhoneNumber] | None = Field(default=None, alias="PhoneNumbers")
    status: str | None = Field(default=None, alias="Status")


# ── Administration / Users (PR #81) ──────────────────────────────


class UserRole(BuildiumModel):
    """One row from ``UserMessageUserRole`` — the role an admin user
    is assigned to in Buildium.

    Verified against the LeadSimple/buildium SDK
    ``UserMessageUserRole.md`` spec on 2026-05-17 — Buildium ships
    ``Id``/``Name``/``Description``/``NumberOfUsers`` and nothing else.
    Helixis only reads ``Id`` + ``Name`` today; the rest are typed
    for future surfaces but not consumed."""

    id: int = Field(alias="Id")
    name: str | None = Field(default=None, alias="Name")
    description: str | None = Field(default=None, alias="Description")
    number_of_users: int | None = Field(default=None, alias="NumberOfUsers")


class User(BuildiumModel):
    """One row from ``GET /v1/users`` (Buildium ``UserMessage``).

    Verified field set per ``UserMessage.md`` from the LeadSimple
    Ruby SDK on 2026-05-17. Fields the spec does NOT list (``Title``,
    ``Department``, ``IsAdmin``, ``Permissions``, ``IsOwner``, etc.)
    are intentionally absent — PR #78's lesson is that inventing
    aliases breaks parsing silently.

    Wire JSON is PascalCase even though Buildium's Ruby SDK exposes
    snake_case — the alias map below pins the wire shape.
    """

    id: int = Field(alias="Id")
    user_types: list[str] | None = Field(default=None, alias="UserTypes")
    is_active: bool | None = Field(default=None, alias="IsActive")
    last_login: datetime | None = Field(default=None, alias="LastLogin")
    first_name: str | None = Field(default=None, alias="FirstName")
    last_name: str | None = Field(default=None, alias="LastName")
    company_name: str | None = Field(default=None, alias="CompanyName")
    email: str | None = Field(default=None, alias="Email")
    alternate_email: str | None = Field(default=None, alias="AlternateEmail")
    phone_numbers: list[PhoneNumber] | None = Field(default=None, alias="PhoneNumbers")
    user_role: UserRole | None = Field(default=None, alias="UserRole")
    is_company: bool | None = Field(default=None, alias="IsCompany")


# ── Request tasks (read-only) ───────────────────────────────────────────
# Buildium splits "requests" into four task subtypes, each with its own
# endpoint under /tasks: resident requests, rental-owner requests, contact
# requests, and to-do requests. They share a common task shape; we type the
# fields we render and let extra fields ride along (extra=allow). See
# endpoints/requests.py.


class RequestTaskCategory(BuildiumModel):
    id: int | None = Field(default=None, alias="Id")
    name: str | None = Field(default=None, alias="Name")


class RequestTask(BuildiumModel):
    """A Buildium request task (resident / owner / contact / to-do)."""

    id: int = Field(alias="Id")
    title: str | None = Field(default=None, alias="Title")
    description: str | None = Field(default=None, alias="Description")
    # Buildium uses ``TaskStatus`` on request subtypes (New / InProgress /
    # Completed / Deferred / Closed) rather than the bare ``Status`` on the
    # generic /tasks shape.
    task_status: str | None = Field(default=None, alias="TaskStatus")
    priority: str | None = Field(default=None, alias="Priority")
    category: RequestTaskCategory | None = Field(default=None, alias="Category")
    unit_number: str | None = Field(default=None, alias="UnitNumber")
    assigned_to_user_id: int | None = Field(default=None, alias="AssignedToUserId")
    due_date: datetime | None = Field(default=None, alias="DueDate")
    created_date_time: datetime | None = Field(default=None, alias="CreatedDateTime")


# ── General Ledger (read-only) ──────────────────────────────────────────
# Buildium API group "General Ledger": chart of accounts, per-account
# balances, and ledger transactions. See endpoints/general_ledger.py.


class GLAccount(BuildiumModel):
    """A general-ledger account (chart-of-accounts row)."""

    id: int = Field(alias="Id")
    account_number: str | None = Field(default=None, alias="AccountNumber")
    name: str | None = Field(default=None, alias="Name")
    description: str | None = Field(default=None, alias="Description")
    # Asset / Liability / Equity / Income / Expense
    type: str | None = Field(default=None, alias="Type")
    sub_type: str | None = Field(default=None, alias="SubType")
    is_active: bool | None = Field(default=None, alias="IsActive")
    is_bank_account: bool | None = Field(default=None, alias="IsBankAccount")
    parent_gl_account_id: int | None = Field(default=None, alias="ParentGLAccountId")
    cash_flow_classification: str | None = Field(default=None, alias="CashFlowClassification")


class GLAccountRef(BuildiumModel):
    """The lightweight GL-account reference embedded in a balance row."""

    id: int | None = Field(default=None, alias="Id")
    account_number: str | None = Field(default=None, alias="AccountNumber")
    name: str | None = Field(default=None, alias="Name")
    type: str | None = Field(default=None, alias="Type")


class GLAccountBalance(BuildiumModel):
    """A GL account's balance as of a date (``/glaccounts/balances``)."""

    total_balance: float | None = Field(default=None, alias="TotalBalance")
    gl_account: GLAccountRef | None = Field(default=None, alias="GLAccount")


class GLTransactionJournal(BuildiumModel):
    memo: str | None = Field(default=None, alias="Memo")


class GLTransaction(BuildiumModel):
    """A single general-ledger transaction (``/generalledger/transactions``)."""

    id: int = Field(alias="Id")
    date: datetime | None = Field(default=None, alias="Date")
    transaction_type: str | None = Field(default=None, alias="TransactionType")
    total_amount: float | None = Field(default=None, alias="TotalAmount")
    check_number: str | None = Field(default=None, alias="CheckNumber")
    unit_number: str | None = Field(default=None, alias="UnitNumber")
    journal: GLTransactionJournal | None = Field(default=None, alias="Journal")


# ── Communications (read-only) ──────────────────────────────────────────
# Buildium API group "Communications": announcements, sent-email logs, phone
# logs, templates. We type the fields we render. See endpoints/communications.py.


class CommunicationParty(BuildiumModel):
    """A lightweight sender / participant reference on a communication."""

    first_name: str | None = Field(default=None, alias="FirstName")
    last_name: str | None = Field(default=None, alias="LastName")
    name: str | None = Field(default=None, alias="Name")


class Announcement(BuildiumModel):
    id: int = Field(alias="Id")
    subject: str | None = Field(default=None, alias="Subject")
    body: str | None = Field(default=None, alias="Body")
    announcement_date: datetime | None = Field(default=None, alias="AnnouncementDate")
    expiration_date: datetime | None = Field(default=None, alias="ExpirationDate")
    channels: list[str] | None = Field(default=None, alias="Channels")


class EmailLog(BuildiumModel):
    """A record of an email Buildium sent (``/communications/emails``)."""

    id: int = Field(alias="Id")
    subject: str | None = Field(default=None, alias="Subject")
    sent_date_time: datetime | None = Field(default=None, alias="SentDateTime")
    sender: CommunicationParty | None = Field(default=None, alias="Sender")


class PhoneLog(BuildiumModel):
    id: int = Field(alias="Id")
    subject: str | None = Field(default=None, alias="Subject")
    description: str | None = Field(default=None, alias="Description")
    call_date_time: datetime | None = Field(default=None, alias="CallDateTime")
    participant: CommunicationParty | None = Field(default=None, alias="Participant")


class CommunicationTemplate(BuildiumModel):
    id: int = Field(alias="Id")
    name: str | None = Field(default=None, alias="Name")


# ── Listings (read-only) ────────────────────────────────────────────────
# Buildium models rental listings under /rentals/units/listings. A listing is
# keyed by its unit; the payload nests lightweight property + unit refs. See
# endpoints/listings.py.


class ListingUnitRef(BuildiumModel):
    id: int | None = Field(default=None, alias="Id")
    unit_number: str | None = Field(default=None, alias="UnitNumber")
    address: Address | None = Field(default=None, alias="Address")


class ListingPropertyRef(BuildiumModel):
    id: int | None = Field(default=None, alias="Id")
    name: str | None = Field(default=None, alias="Name")


class Listing(BuildiumModel):
    """A rental listing (``/rentals/units/listings``)."""

    id: int | None = Field(default=None, alias="Id")
    listing_date: datetime | None = Field(default=None, alias="ListingDate")
    available_date: datetime | None = Field(default=None, alias="AvailableDate")
    rent: float | None = Field(default=None, alias="Rent")
    deposit: float | None = Field(default=None, alias="Deposit")
    lease_terms: str | None = Field(default=None, alias="LeaseTerms")
    is_managed_externally: bool | None = Field(default=None, alias="IsManagedExternally")
    property: ListingPropertyRef | None = Field(default=None, alias="Property")
    unit: ListingUnitRef | None = Field(default=None, alias="Unit")


# ⚠ `RentalAppliance` lived here until 2026-09-22 and was removed with the
# rest of the appliance surface (see buildium/api.py). Meter readings
# (endpoints/meter_readings.py) return raw dicts — no schema, the shape is
# property-specific.


__all__ = [
    "Address",
    "Announcement",
    "Applicant",
    "BankAccount",
    "Bill",
    "BillLine",
    "BillPayment",
    "BuildiumModel",
    "CommunicationParty",
    "CommunicationTemplate",
    "CurrentTenant",
    "EmailLog",
    "File",
    "GLAccount",
    "GLAccountBalance",
    "GLAccountRef",
    "GLTransaction",
    "GLTransactionJournal",
    "Lease",
    "LeaseMoveOut",
    "LeaseTenantStatus",
    "LeaseTransaction",
    "Listing",
    "ListingPropertyRef",
    "ListingUnitRef",
    "OutstandingBalance",
    "PhoneLog",
    "PhoneNumber",
    "Property",
    "RentalOwner",
    "RequestTask",
    "RequestTaskCategory",
    "Task",
    "Tenant",
    "TenantLeaseSummary",
    "Unit",
    "User",
    "UserRole",
    "Vendor",
    "VendorAddress",
    "VendorCategory",
    "VendorPhoneNumber",
    "WorkOrder",
]
