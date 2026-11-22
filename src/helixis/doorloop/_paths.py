"""Every operation in DoorLoop's reference. GENERATED -- do not edit.

Regenerate with ``python scripts/doorloop_paths.py --write``. Its ``--check``
fails when this file and the spec differ, and ``tests/unit/test_doorloop_paths.py``
rebuilds it from the spec.

Source: docs/doorloop/vendor/api.doorloop.com/openapi.json
sha256 9f1fba0a59a35270f578c4efcc0f9434a68109b3b2c4c12159aee979b466aaa6
"""

from __future__ import annotations

SPEC_SHA256 = "9f1fba0a59a35270f578c4efcc0f9434a68109b3b2c4c12159aee979b466aaa6"

#: The documented root (the spec's one server).
BASE_URL = "https://app.doorloop.com/api"

#: The largest ``page_size`` (``components.parameters.page_size``).
MAX_PAGE_SIZE = 1000

#: The paging and sorting parameters every list takes (the Pagination and
#: Sorting pages). Most list operations do not declare them.
LIST_PARAMS: frozenset[str] = frozenset({"page_number", "page_size", "sort_by", "sort_descending"})

#: ``"<METHOD> <path>"`` -> the query parameters it declares.
QUERY_PARAMS: dict[str, frozenset[str]] = {
    "DELETE /accounts/{accountId}": frozenset(),
    "DELETE /communications/{communicationId}": frozenset(),
    "DELETE /expenses/{expenseId}": frozenset(),
    "DELETE /files/{fileId}": frozenset(),
    "DELETE /lease-charges/{leaseChargeId}": frozenset(),
    "DELETE /lease-credits/{leaseCreditId}": frozenset(),
    "DELETE /lease-payments/{leasePaymentId}": frozenset(),
    "DELETE /lease-reversed-payments/{leaseReversedPaymentId}": frozenset(),
    "DELETE /notes/{noteId}": frozenset(),
    "DELETE /owners/{ownerId}": frozenset(),
    "DELETE /tasks/{taskId}": frozenset(),
    "DELETE /tenants/{tenantId}": frozenset(),
    "DELETE /vendor-bills/{vendorBillId}": frozenset(),
    "DELETE /vendor-credits/{vendorCreditId}": frozenset(),
    "DELETE /vendors/{vendorId}": frozenset(),
    "GET /accounts": frozenset(
        {
            "filter_DefaultAccountFor",
            "filter_ancestorAccount",
            "filter_leaseChargeItem",
            "filter_parentAccount",
            "filter_type",
            "page_number",
            "page_size",
        }
    ),
    "GET /accounts/{accountId}": frozenset(),
    "GET /communications": frozenset(
        {
            "filter_participantLinkedToId",
            "filter_participantLinkedToType",
            "filter_sentAt_from",
            "filter_sentAt_to",
            "filter_status",
            "filter_threadId",
            "filter_type",
        }
    ),
    "GET /communications/{communicationId}": frozenset(),
    "GET /expenses": frozenset(
        {
            "filter_date_from",
            "filter_date_to",
            "filter_group",
            "filter_owner",
            "filter_payFromAccount",
            "filter_property",
            "filter_tenant",
            "filter_vendor",
        }
    ),
    "GET /expenses/{expenseId}": frozenset(),
    "GET /files": frozenset(
        {
            "filter_createdAt_from",
            "filter_createdAt_to",
            "filter_createdBy",
            "filter_resourceId",
            "filter_tags",
            "filter_text",
        }
    ),
    "GET /files/{fileId}": frozenset(),
    "GET /files/{fileId}/download": frozenset(),
    "GET /lease-charges": frozenset(
        {
            "filter_accounts",
            "filter_date_from",
            "filter_date_to",
            "filter_excludeAutomaticLateFeeCharges",
            "filter_lateFeeCharges",
            "filter_lease",
            "filter_owner",
            "filter_property",
            "filter_totalBalanceGreaterThan",
            "filter_unit",
            "page_number",
            "page_size",
        }
    ),
    "GET /lease-charges/{leaseChargeId}": frozenset(),
    "GET /lease-credits": frozenset(
        {
            "filter_date_from",
            "filter_date_to",
            "filter_lease",
            "filter_owner",
            "filter_property",
        }
    ),
    "GET /lease-credits/{leaseCreditId}": frozenset(),
    "GET /lease-payments": frozenset(
        {
            "filter_date_from",
            "filter_date_to",
            "filter_lease",
            "filter_owner",
            "filter_paymentMethod",
            "filter_property",
        }
    ),
    "GET /lease-payments/{leasePaymentId}": frozenset(),
    "GET /lease-reversed-payments": frozenset(
        {
            "filter_date_from",
            "filter_date_to",
            "filter_lease",
            "filter_owner",
            "filter_paymentMethod",
            "filter_property",
        }
    ),
    "GET /lease-reversed-payments/{leaseReversedPaymentId}": frozenset(),
    "GET /leases": frozenset(
        {
            "filter_calculatedStatus",
            "filter_end_date_from",
            "filter_end_date_to",
            "filter_evictionPending",
            "filter_group",
            "filter_outstandingBalanceGreaterThan",
            "filter_owner",
            "filter_property",
            "filter_propertyClass",
            "filter_situation",
            "filter_start_date_from",
            "filter_start_date_to",
            "filter_status",
            "filter_tenant",
            "filter_term",
            "filter_text",
            "filter_unit",
            "page_number",
            "page_size",
        }
    ),
    "GET /leases/tenants": frozenset(
        {
            "filter_group",
            "filter_lease",
            "filter_movedInAt_from",
            "filter_movedInAt_to",
            "filter_movedOutAt_To",
            "filter_movedOutAt_from",
            "filter_property",
            "filter_status",
            "filter_text",
        }
    ),
    "GET /leases/{leaseId}": frozenset(),
    "GET /notes": frozenset(
        {
            "filter_createdAt_from",
            "filter_createdAt_to",
            "filter_createdBy",
            "filter_resourceId",
            "filter_tags",
            "filter_text",
        }
    ),
    "GET /notes/{noteId}": frozenset(),
    "GET /owners": frozenset(
        {
            "filter_active",
            "filter_createdAt_from",
            "filter_createdAt_to",
            "filter_group",
            "filter_management_ends_before",
            "filter_property",
            "filter_text",
        }
    ),
    "GET /owners/{ownerId}": frozenset(),
    "GET /properties": frozenset({"filter_class", "filter_group", "filter_owner", "filter_text"}),
    "GET /properties/{propertyId}": frozenset(),
    "GET /property-groups": frozenset(),
    "GET /property-groups/{portfolioId}": frozenset(),
    "GET /reports/balance-sheet-summary": frozenset(
        {
            "filter_accountingMethod",
            "filter_date_from",
            "filter_date_to",
            "filter_group",
            "filter_property",
            "filter_propertyOwner",
        }
    ),
    "GET /reports/cash-flow-statement": frozenset(
        {
            "filter_accountingMethod",
            "filter_date_from",
            "filter_date_to",
            "filter_group",
            "filter_property",
            "filter_propertyOwner",
        }
    ),
    "GET /reports/profit-and-loss-summary": frozenset(
        {
            "filter_accountingMethod",
            "filter_date_from",
            "filter_date_to",
            "filter_group",
            "filter_property",
            "filter_propertyOwner",
        }
    ),
    "GET /reports/rent-roll": frozenset(
        {
            "filter_asOfDate",
            "filter_group",
            "filter_owner",
            "filter_property",
        }
    ),
    "GET /tasks": frozenset(
        {
            "filter_assignedToUser",
            "filter_completedAt_from",
            "filter_completedAt_to",
            "filter_createdAt_from",
            "filter_createdAt_to",
            "filter_dueDate_from",
            "filter_dueDate_to",
            "filter_property",
            "filter_propertyGroup",
            "filter_requestedById",
            "filter_status",
            "filter_text",
            "filter_type",
            "filter_unit",
        }
    ),
    "GET /tasks/{taskId}": frozenset(),
    "GET /tenants": frozenset(
        {
            "filter_group",
            "filter_lease",
            "filter_property",
            "filter_text",
            "filter_type",
            "filter_unit",
        }
    ),
    "GET /tenants/{tenantId}": frozenset(),
    "GET /units": frozenset(
        {
            "filter_active",
            "filter_group",
            "filter_owner",
            "filter_property",
            "filter_text",
        }
    ),
    "GET /units/{unitId}": frozenset(),
    "GET /users": frozenset(),
    "GET /users/me": frozenset(),
    "GET /users/{userId}": frozenset(),
    "GET /vendor-bills": frozenset(
        {
            "filter_date_from",
            "filter_date_to",
            "filter_due_date_from",
            "filter_due_date_to",
            "filter_group",
            "filter_openBills",
            "filter_property",
            "filter_vendor",
        }
    ),
    "GET /vendor-bills/{vendorBillId}": frozenset(),
    "GET /vendor-credits": frozenset(
        {
            "filter_date_from",
            "filter_date_to",
            "filter_due_date_from",
            "filter_due_date_to",
            "filter_group",
            "filter_openBills",
            "filter_property",
            "filter_vendor",
        }
    ),
    "GET /vendor-credits/{vendorCreditId}": frozenset(),
    "GET /vendors": frozenset(
        {
            "filter_active",
            "filter_createdAt_from",
            "filter_createdAt_to",
            "filter_insurance_expires_before",
            "filter_openBalance",
            "filter_property",
        }
    ),
    "GET /vendors/{vendorId}": frozenset(),
    "POST /accounts": frozenset(),
    "POST /communications": frozenset(),
    "POST /expenses": frozenset(),
    "POST /files": frozenset(),
    "POST /lease-charges": frozenset(),
    "POST /lease-credits": frozenset(),
    "POST /lease-payments": frozenset(),
    "POST /lease-reversed-payments": frozenset(),
    "POST /leases/full": frozenset(),
    "POST /leases/move-in": frozenset(),
    "POST /leases/move-out": frozenset(),
    "POST /notes": frozenset(),
    "POST /owners": frozenset(),
    "POST /tasks": frozenset(),
    "POST /tasks/update": frozenset(),
    "POST /tenants": frozenset(),
    "POST /vendor-bills": frozenset(),
    "POST /vendor-credits": frozenset(),
    "POST /vendors": frozenset(),
    "PUT /accounts/{accountId}": frozenset(),
    "PUT /communications/{communicationId}": frozenset(),
    "PUT /expenses/{expenseId}": frozenset(),
    "PUT /files/{fileId}": frozenset(),
    "PUT /lease-charges/{leaseChargeId}": frozenset(),
    "PUT /lease-credits/{leaseCreditId}": frozenset(),
    "PUT /lease-payments/{leasePaymentId}": frozenset(),
    "PUT /notes/{noteId}": frozenset(),
    "PUT /owners/{ownerId}": frozenset(),
    "PUT /tasks/{taskId}": frozenset(),
    "PUT /tenants/{tenantId}": frozenset(),
    "PUT /units/{unitId}": frozenset(),
    "PUT /vendor-bills/{vendorBillId}": frozenset(),
    "PUT /vendor-credits/{vendorCreditId}": frozenset(),
    "PUT /vendors/{vendorId}": frozenset(),
}

#: The lists: a 2xx answer of ``{"total": ..., "data": [...]}``.
LISTS: frozenset[str] = frozenset(
    {
        "GET /accounts",
        "GET /accounts/{accountId}",
        "GET /communications",
        "GET /expenses",
        "GET /files",
        "GET /lease-charges",
        "GET /lease-credits",
        "GET /lease-payments",
        "GET /lease-reversed-payments",
        "GET /leases",
        "GET /leases/tenants",
        "GET /notes",
        "GET /owners",
        "GET /properties",
        "GET /tasks",
        "GET /tenants",
        "GET /units",
        "GET /users",
        "GET /vendor-bills",
        "GET /vendor-credits",
        "GET /vendors",
    }
)
