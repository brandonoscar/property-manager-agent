"""Where each Rentvine collection puts the record. GENERATED -- do not edit.

Regenerate with ``python scripts/rentvine_row_envelopes.py --write``.
``tests/unit/test_rentvine_row_envelopes.py`` fails if this drifts from the
captured spec.

``ROW_ENVELOPE[key]`` is the property holding the record, or ``None`` when the
array's items ARE the record and must NOT be unwrapped. A key ABSENT from the
map is a third state: that operation does not return a collection.

⚠ THE VALUES IN ``LIVE_OVERRIDDEN`` DO NOT COME FROM THE SPEC. For those
operations the captured document declares a bare record and the server returns
a wrapper, so a live measurement outranks the capture. The generator's
``LIVE_OVERRIDES`` carries the run that settled each one; do not "correct" them
back by reading the spec.

⚠ @unverified — where a row carries several record-shaped properties the
FIRST-DECLARED one is taken as the record, and JSON Schema does not make
property order meaningful, so that is inferred rather than documented. It
agrees with the entity the path names wherever the path names one, and
``ROW_COMPANIONS`` records the alternatives so a wrong pick is one line to
correct. Phase 8 settles it in one call per collection: read a row and check
the chosen key carries the id the mapper reads.

⚠ THAT RULE IS RIGHT 27 TIMES OUT OF 28 AND ``MEASURED_SUBJECT`` IS THE 28th.
Being right almost always is what let it ship; the one exception is
``/properties/units/export``, a UNITS export that declares ``property`` first.
Those entries are NOT ``LIVE_OVERRIDDEN``: the spec describes that row
correctly and the tie-break picked the wrong record off it.

⚠ AND AN ENVELOPE CANNOT EXPRESS A JOIN. ``/properties/units/export`` carries
two records and this map names one, so unwrapping it yields a unit with no
``propertyID`` -- ``unit_export`` does not carry the column, the link is on the
sibling ``property``. A caller that needs both must read the row whole rather
than through ``client._rows_from``, which is why ``sync`` still fetches units
per property.

⚠ A WRONG KEY NAME IS LOUD AND A WRONG ``None`` IS SILENT — they are not the
same defect and this file used to claim they were. ``client._rows_from``
RAISES on a row missing its DECLARED key, so a mis-picked name names itself on
the first live call. A wrong ``None`` has no declared key to be missing: the
wrapper passes through untouched, ``mapper`` finds no id on it, and
``sync._write_rows`` skips every row — a complete, successful sync of an empty
table. That is what ``LIVE_OVERRIDES`` exists because of, and it is why
``sync``'s skip warning prints the row's KEYS.

Source: docs/rentvine/vendor/docs.rentvine.com/openapi.json
sha256 9c3049fea3d3dc2d0b347034c959088b9ff2f486014a451f87974096285e3802
"""

from __future__ import annotations

SPEC_SHA256 = "9c3049fea3d3dc2d0b347034c959088b9ff2f486014a451f87974096285e3802"


ROW_ENVELOPE: dict[str, str | None] = {
    "GET /accounting/accounts": "account",
    "GET /accounting/accounts/search": "account",
    "GET /accounting/bills": "bill",
    "GET /accounting/deposits/search": "deposit",
    "GET /accounting/deposits/transactions/undeposited": "transaction",
    "GET /accounting/diagnostics/bank-account-reconciliation-lapse": None,
    "GET /accounting/diagnostics/credit-debit-mismatch": None,
    "GET /accounting/diagnostics/escrow-mismatch": None,
    "GET /accounting/diagnostics/manager-ledger-balance": None,
    "GET /accounting/diagnostics/negative-bank-accounts": None,
    "GET /accounting/diagnostics/prepayment-mismatch": None,
    "GET /accounting/diagnostics/reserve-not-met": None,
    "GET /accounting/diagnostics/suppressed-fee-balance-mismatch": None,
    "GET /accounting/diagnostics/unused-prepayments": None,
    "GET /accounting/diagnostics/unused-vendor-credits": None,
    "GET /accounting/diagnostics/vendor-insurance-lapse": None,
    "GET /accounting/invoices": "invoice",
    "GET /accounting/journal-entries/search": "transaction",
    "GET /accounting/ledger-transfers/search": "transaction",
    "GET /accounting/ledgers": "ledger",
    "GET /accounting/ledgers/search": "ledger",
    "GET /accounting/payables/search": "bill",
    "GET /accounting/recurring-journal-entries/search": "recurringJournalEntry",
    "GET /accounting/recurring-ledger-transfers/search": "recurringLedgerTransfer",
    "GET /accounting/transactions/entries/search": "transactionEntry",
    "GET /accounting/transactions/search": "transaction",
    "GET /associations": "contact",
    "GET /associations/search": "contact",
    "GET /chat/messages": None,
    "GET /contacts/conversations": None,
    "GET /custom-field-category-object-types": None,
    "GET /custom-field-category-object-types/{customFieldCategoryObjectTypeID}": None,
    "GET /custom-fields/values/{objectTypeID}/{objectID}": None,
    "GET /leases": "lease",
    "GET /leases/export": "lease",
    "GET /leases/statuses": "leaseStatus",
    "GET /leases/{leaseID}/recurring-charges": "recurringCharge",
    "GET /leases/{leaseID}/resident-services": "leaseResidentService",
    "GET /leases/{leaseID}/tenants": "leaseTenant",
    "GET /maintenance-technicians": "contact",
    "GET /maintenance-technicians/search": "contact",
    "GET /maintenance/inspections": None,
    "GET /maintenance/labor-rates": "laborRate",
    "GET /maintenance/materials": "material",
    "GET /maintenance/vendor-trades": None,
    "GET /maintenance/work-order/statuses": "workOrderStatus",
    "GET /maintenance/work-orders": "workOrder",
    "GET /messages/texts/phone/{phoneNumber}": None,
    "GET /owners": "contact",  # live-measured; the spec says bare
    "GET /owners/search": "contact",
    "GET /portfolios": "portfolio",
    "GET /portfolios/search": "portfolio",
    "GET /portfolios/statements/batches/search": "statementBatch",
    "GET /portfolios/statements/search": "statement",
    "GET /portfolios/{portfolioID}/owners": "owner",
    "GET /portfolios/{portfolioID}/statements": "statement",
    "GET /properties": "property",
    "GET /properties/export": "property",
    "GET /properties/listings/search": "listing",
    "GET /properties/search": "property",
    "GET /properties/units/export": "unit",  # measured subject, not first-declared
    "GET /properties/{propertyID}/units": "unit",  # live-measured; the spec says bare
    "GET /resident/packages": "package",
    "GET /resident/services": "service",
    "GET /screening/applications": "application",
    "GET /screening/applications/export": "application",
    "GET /tenants": "contact",
    "GET /tenants/search": "contact",
    "GET /vendors": "contact",
    "GET /vendors/search": "contact",
}

#: The operations above whose value was MEASURED on a live call rather
#: than read from the spec. See ``LIVE_OVERRIDES`` in the generator for
#: the run that settled each one.
LIVE_OVERRIDDEN: frozenset[str] = frozenset(
    {
        "GET /owners",
        "GET /properties/{propertyID}/units",
    }
)

#: The operations whose value is the MEASURED subject rather than the
#: first-declared record. The spec is right about the row's shape here and
#: the generator's tie-break was wrong, which is a different claim from
#: ``LIVE_OVERRIDDEN`` -- see ``SUBJECT_OF`` in the generator.
MEASURED_SUBJECT: frozenset[str] = frozenset(
    {
        "GET /properties/units/export",
    }
)

#: The other record-shaped properties on the same row, in declared order.
#: Not noise -- ``/leases`` carries ``unit`` and ``/properties`` carries
#: ``portfolio``, which is relationship material a later phase may want.
ROW_COMPANIONS: dict[str, tuple[str, ...]] = {
    "GET /accounting/accounts/search": ("accountCategory",),
    "GET /accounting/bills": ("contact",),
    "GET /accounting/deposits/search": (
        "account",
        "reconciliation",
    ),
    "GET /accounting/deposits/transactions/undeposited": (
        "ledger",
        "payer",
        "property",
        "unit",
        "createdByUser",
    ),
    "GET /accounting/invoices": (
        "invoiceFile",
        "contact",
        "property",
        "workOrder",
        "bill",
        "submittedBy",
        "createdInvoice",
    ),
    "GET /accounting/journal-entries/search": (
        "ledger",
        "property",
        "portfolio",
        "unit",
        "transactionEntries",
    ),
    "GET /accounting/ledger-transfers/search": (
        "ledger",
        "property",
        "portfolio",
        "unit",
        "transactionEntries",
    ),
    "GET /accounting/ledgers/search": ("unit",),
    "GET /accounting/payables/search": (
        "transaction",
        "ledger",
        "portfolio",
        "property",
        "unit",
        "contact",
    ),
    "GET /accounting/recurring-journal-entries/search": ("ledger",),
    "GET /accounting/transactions/entries/search": (
        "transaction",
        "ledger",
        "primaryLedger",
        "property",
        "portfolio",
        "unit",
    ),
    "GET /accounting/transactions/search": (
        "ledger",
        "property",
        "portfolio",
        "unit",
        "transactionEntries",
    ),
    "GET /leases": ("unit",),
    "GET /leases/export": (
        "balances",
        "unpaidCharges",
        "property",
        "unit",
        "portfolio",
    ),
    "GET /leases/{leaseID}/recurring-charges": ("account",),
    "GET /leases/{leaseID}/resident-services": ("residentService",),
    "GET /leases/{leaseID}/tenants": ("contact",),
    "GET /maintenance/work-orders": ("contact",),
    "GET /portfolios/search": (
        "statementSetting",
        "lastStatement",
    ),
    "GET /portfolios/statements/batches/search": ("statementSetting",),
    "GET /portfolios/statements/search": (
        "portfolio",
        "statementSetting",
    ),
    "GET /portfolios/{portfolioID}/statements": ("statementSetting",),
    "GET /properties/export": ("portfolio",),
    "GET /properties/listings/search": (
        "unit",
        "property",
        "propertyGroups",
        "portfolio",
        "propertyManager",
        "image",
    ),
    "GET /properties/search": (
        "portfolio",
        "lease",
        "propertyGroup",
        "primaryImage",
    ),
    "GET /properties/units/export": ("property",),
    "GET /screening/applications": (
        "status",
        "unit",
        "property",
        "lease",
    ),
    "GET /screening/applications/export": (
        "unit",
        "lease",
    ),
}
