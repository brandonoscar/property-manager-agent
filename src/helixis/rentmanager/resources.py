"""Every Rent Manager collection the read side asks for, declared once.

One :class:`Resource` per collection: its path, its primary key, the
``fields`` it asks for and the ``embeds`` those fields need. The client's
operation index and the query each read sends come from here, and
``test_rentmanager_resources`` holds every field the mapper reads to the
fields declared here (a field never asked for would map to unknown forever).

⚠ AN EMBEDDED FIELD MUST ALSO BE IN ``fields``. Rent Manager's Overview (p.6)
says a field to be embedded "must also appear in the fields parameter or it
will not be included in the response", and the help scrape marks ``Balance``,
``OpenBalance`` and ``SecurityDepositHeld`` "Requires Embed": without the
embed they come back empty, which a reader would take as $0 (rule 14).
:class:`Resource` refuses to be built otherwise, and so does a field it says
needs an embed that it does not declare.

Sources (``docs/rentmanager/api-notes.md``): the names come from the help-site
scrape of one customer's ``/Help`` pages (T1c, "HS"), which lists each model's
fields. It does not document the embedded models' own fields (an address, an
ownership, an open receivable); those are guesses and say so where they are
read, in ``mapper.py``. Hand-written for now; generated from the customer's
``/help`` export once one exists (design: Module layout).

Three reads of a collection (``sync.py`` chooses which, design "Sync"): the
whole of it (:meth:`Resource.query`), only what changed at or after a time
(:meth:`Resource.changed_query`), and only its ids
(:meth:`Resource.ids_query`, the nightly reconcile).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType

# @unverified RM-7 UpdateDate is the field Rent Manager sets on every change
# to a record, and `UpdateDate,ge,<time>` returns the records changed at or
# after <time>. Settled by one read with the filter at now−1 day against one
# at now+1 day, as the design's RM-7 row says.
CHANGE_FIELD = "UpdateDate"


#: How a time is written in a filter: the format the founder's probe sends
#: when it checks ``UpdateDate,ge`` (``scripts/rentmanager_probe.py``, RM-7),
#: so what the probe settles is what the sync sends.
FILTER_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def filter_time(when: datetime) -> str:
    """``when`` as a filter value: in UTC, to the second, with no zone."""
    # @unverified RM-7 the filter value's format, and which zone Rent Manager
    # reads a value without one in (UTC, or its server's own). Settled by the
    # RM-7 read, with the value written in each zone; until then a read by
    # change starts far enough back to cover either (sync.ZONE_MARGIN).
    if when.tzinfo is None:
        raise ValueError("a filter time must carry a time zone")
    return when.astimezone(UTC).strftime(FILTER_TIME_FORMAT)


@dataclass(frozen=True)
class Resource:
    """One collection read: ``GET {path}?fields=...&embeds=...``, paged."""

    #: The collection path, e.g. ``"/Tenants"``.
    path: str
    #: The model's primary key (the help scrape's "Primary Key" field).
    key: str
    #: Every field asked for. The key, every embed, and every field that
    #: needs an embed are among them.
    fields: tuple[str, ...]
    #: The embeds asked for, each also in :attr:`fields`.
    embeds: tuple[str, ...] = ()
    #: ``field -> the embed it is empty without`` (help scrape: "Requires
    #: Embed (X)"). The field and its embed must both be declared.
    needs_embed: Mapping[str, str] = field(default_factory=dict)
    #: What an id-only read asks for beside the key: whatever decides that a
    #: record does not belong in the mirror at all (a reversed bill).
    id_extra: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        missing = [e for e in self.embeds if e not in self.fields]
        if missing:
            raise ValueError(f"{self.path}: embeds {missing} are not also in fields")
        for name, embed in self.needs_embed.items():
            if name not in self.fields:
                raise ValueError(f"{self.path}: {name} needs {embed} but is not in fields")
            if embed not in self.embeds:
                raise ValueError(f"{self.path}: {name} is empty without the {embed} embed")
        if self.key not in self.fields:
            raise ValueError(f"{self.path}: the primary key {self.key} is not in fields")
        if len(set(self.fields)) != len(self.fields):
            raise ValueError(f"{self.path}: a field is named twice")
        stray = [f for f in self.id_extra if f not in self.fields or f in self.embeds]
        if stray:
            raise ValueError(f"{self.path}: an id-only read asks for {stray}, not plain fields")

    @property
    def incremental(self) -> bool:
        """Whether it can be read by what changed: it carries
        :data:`CHANGE_FIELD`. A lookup does not, and is read whole every pass."""
        return CHANGE_FIELD in self.fields

    def query(self) -> dict[str, str]:
        """The query parameters every page of a whole read carries."""
        # @unverified RM-6 the comma-separated spelling of `fields` and
        # `embeds`, and dotted names for a nested embed (Contacts.PhoneNumbers).
        # Settled by one read of each resource with its declared query.
        out = {"fields": ",".join(self.fields)}
        if self.embeds:
            out["embeds"] = ",".join(self.embeds)
        return out

    def changed_query(self, since: datetime) -> dict[str, str]:
        """A whole read's query, narrowed to the records changed at or after
        ``since``: what an incremental pass reads."""
        if not self.incremental:
            raise ValueError(f"{self.path} carries no {CHANGE_FIELD}: it is read whole")
        # @unverified RM-6 `filters=<field>,<operator>,<value>` is the filter's
        # spelling. Settled by the filter canary (sync.py) on the probe account.
        return {**self.query(), "filters": f"{CHANGE_FIELD},ge,{filter_time(since)}"}

    def ids_query(self) -> dict[str, str]:
        """An id-only read: the key, and :attr:`id_extra`; nothing embedded."""
        # @unverified RM-8 `fields=<primary key>` answers one object per record
        # carrying only its key. Settled by one read per resource with it.
        return {"fields": ",".join((self.key, *self.id_extra))}


# @unverified RM-8 every primary key name below is the help scrape's; Bills'
# is `ID`, not the `BillID` the design guessed. Settled by one read per
# resource with fields=<key>, as the design's RM-8 row says.
PROPERTIES = Resource(
    path="/Properties",
    key="PropertyID",
    fields=("PropertyID", "Name", "IsActive", "PropertyType", "UpdateDate", "PrimaryAddress"),
    embeds=("PrimaryAddress",),
)

UNITS = Resource(
    path="/Units",
    key="UnitID",
    fields=(
        "UnitID",
        "PropertyID",
        "Name",
        "UpdateDate",
        "CurrentOccupants",
        "CurrentMarketRent",
    ),
    embeds=("CurrentOccupants", "CurrentMarketRent"),
    needs_embed=MappingProxyType({"CurrentMarketRent": "CurrentMarketRent"}),
)

TENANTS = Resource(
    path="/Tenants",
    key="TenantID",
    fields=(
        "TenantID",
        "Name",
        "FirstName",
        "LastName",
        "UpdateDate",
        "Contacts",
        "Contacts.PhoneNumbers",
        "Balance",
        "SecurityDepositHeld",
        "OpenReceivables",
    ),
    embeds=(
        "Contacts",
        "Contacts.PhoneNumbers",
        "Balance",
        "SecurityDepositHeld",
        "OpenReceivables",
    ),
    needs_embed=MappingProxyType(
        {"Balance": "Balance", "SecurityDepositHeld": "SecurityDepositHeld"}
    ),
)

LEASES = Resource(
    path="/Leases",
    key="LeaseID",
    fields=(
        "LeaseID",
        "TenantID",
        "UnitID",
        "PropertyID",
        "MoveInDate",
        "MoveOutDate",
        "ExpectedMoveOutDate",
        "StartDate",
        "EndDate",
        "UpdateDate",
        "Unit",
        "LeaseRenewals",
    ),
    embeds=("Unit", "LeaseRenewals"),
    # @unverified RM-21 the help scrape marks the term "Requires Embed
    # (LeaseRenewals)"; without it every lease would read with no dates.
    # Settled by one lease read with and without the embed.
    needs_embed=MappingProxyType({"StartDate": "LeaseRenewals", "EndDate": "LeaseRenewals"}),
)

OWNERS = Resource(
    path="/Owners",
    key="OwnerID",
    fields=("OwnerID", "Name", "UpdateDate", "Contact", "PhoneNumbers", "Ownerships"),
    embeds=("Contact", "PhoneNumbers", "Ownerships"),
)

VENDORS = Resource(
    path="/Vendors",
    key="VendorID",
    fields=("VendorID", "Name", "IsActive", "Category", "UpdateDate", "Contact", "PhoneNumbers"),
    embeds=("Contact", "PhoneNumbers"),
)

WORK_ORDERS = Resource(
    path="/ServiceManagerIssues",
    key="ServiceManagerIssueID",
    fields=(
        "ServiceManagerIssueID",
        "Title",
        "StatusID",
        "PriorityID",
        "CategoryID",
        "AssignedToUserID",
        "UpdateDate",
        "LinkedProperties",
        "LinkedUnits",
    ),
    embeds=("LinkedProperties", "LinkedUnits"),
)

#: Lookups: the words this customer uses for a work order's status and
#: priority. Re-read whole every pass: they carry no ``UpdateDate``
#: (api-notes §11).
WORK_ORDER_STATUSES = Resource(
    path="/ServiceManagerStatuses",
    key="ServiceManagerStatusID",
    fields=("ServiceManagerStatusID", "Name"),
)

WORK_ORDER_PRIORITIES = Resource(
    path="/ServiceManagerPriorities",
    key="ServiceManagerPriorityID",
    fields=("ServiceManagerPriorityID", "Name"),
)

BILLS = Resource(
    path="/Bills",
    key="ID",
    fields=(
        "ID",
        "Amount",
        "TransactionDate",
        "DueDate",
        "Reference",
        "IsFullyAllocated",
        "ReversalDate",
        "UpdateDate",
        "Vendor",
    ),
    embeds=("Vendor",),
    # A reversed bill is not money anybody owes, and is not mirrored: the
    # reconcile must not count one as a bill the mirror should keep.
    id_extra=("ReversalDate",),
)

GL_ACCOUNTS = Resource(
    path="/GLAccounts",
    key="GLAccountID",
    fields=(
        "GLAccountID",
        "ParentGLAccountID",
        "Name",
        "Reference",
        "GLAccountType",
        "IsActive",
        "UpdateDate",
    ),
)

#: Every collection one pass reads, by the name the sync and the mapper use.
#: The order is the order a pass reads them in.
RESOURCES: Mapping[str, Resource] = MappingProxyType(
    {
        "properties": PROPERTIES,
        "units": UNITS,
        "tenants": TENANTS,
        "leases": LEASES,
        "owners": OWNERS,
        "vendors": VENDORS,
        "work_order_statuses": WORK_ORDER_STATUSES,
        "work_order_priorities": WORK_ORDER_PRIORITIES,
        "work_orders": WORK_ORDERS,
        "bills": BILLS,
        "gl_accounts": GL_ACCOUNTS,
    }
)

__all__ = [
    "BILLS",
    "CHANGE_FIELD",
    "FILTER_TIME_FORMAT",
    "GL_ACCOUNTS",
    "LEASES",
    "OWNERS",
    "PROPERTIES",
    "RESOURCES",
    "TENANTS",
    "UNITS",
    "VENDORS",
    "WORK_ORDERS",
    "WORK_ORDER_PRIORITIES",
    "WORK_ORDER_STATUSES",
    "Resource",
    "filter_time",
]
