"""Test doubles shared by the DoorLoop sync tests.

* :class:`FakeDoorLoop`: answers the client's GETs from a synthetic account,
  paged by ``page_number``/``page_size`` with the total in the body
  (``{"total": n, "data": [...]}``), the way the Pagination page documents. It
  refuses any key but :data:`API_KEY` with a 401, can fail a list, lie about a
  total, or ignore the tasks' ``filter_type``, and records every request, so a
  test asserts what would have left the process.
* :class:`FakePool`: records every statement and answers like asyncpg would,
  including the bind contract (``tests/unit/_pg_bind_contract.py``): a fake
  that accepts what the driver refuses is an inverted test. It answers the
  credentials read from a sequence, so a test can disconnect mid-pass.

Every value is synthetic; ids are 24-hex ObjectIds, as DoorLoop's are.
"""

from __future__ import annotations

import contextlib
import copy
from collections.abc import AsyncIterator, Mapping
from datetime import date
from typing import Any

import httpx
from cryptography.fernet import Fernet

from helixis.security import CredentialCipher
from helixis.settings import Settings
from tests.unit._pg_bind_contract import reject_unbindable, reject_untyped_mirror_bind

COMPANY = "00000000-0000-4000-8000-0000000000d7"
API_KEY = "dl-synthetic-sync-key-never-log-me-0007"
TEST_KEY = Fernet.generate_key().decode()
TODAY = date(2026, 10, 6)
API_PREFIX = "/api"


def oid(n: int) -> str:
    """A synthetic ObjectId: 24 hex characters."""
    return f"64b0c0ffee{n:014x}"


PROP_1, PROP_2 = oid(1), oid(2)
UNIT_1, UNIT_2, UNIT_3 = oid(11), oid(12), oid(13)
LEASE_1, LEASE_2 = oid(21), oid(22)
TENANT_1, TENANT_2, TENANT_3, PROSPECT = oid(31), oid(32), oid(33), oid(34)
#: A lead still at a showing, and an applicant interested in two places.
LEAD, APPLICANT_2 = oid(35), oid(36)
OWNER_1 = oid(41)
VENDOR_1 = oid(51)
WORK_ORDER_1, TODO_1, TENANT_REQUEST_1, OWNER_REQUEST_1 = oid(71), oid(72), oid(73), oid(74)
BILL_1, BILL_2 = oid(121), oid(122)
ACCT_BANK, ACCT_REPAIRS, ACCT_RENT, ACCT_OFFICE = oid(131), oid(132), oid(133), oid(134)


def account() -> dict[str, list[dict[str, Any]]]:
    """A small DoorLoop account, by list path."""
    return {
        "/properties": [
            {
                "id": PROP_1,
                "name": "Redbud Court",
                "type": "RESIDENTIAL_MULTI_FAMILY",
                "address": {"street1": "1 Synthetic Way", "city": "Testville", "zip": "00001"},
                "active": True,
                "owners": [{"owner": OWNER_1, "ownershipPercentage": 100}],
            },
            {"id": PROP_2, "name": "Garden Row", "active": True, "owners": []},
        ],
        "/units": [
            {"id": UNIT_1, "property": PROP_1, "name": "1A", "marketRent": 1200},
            {"id": UNIT_2, "property": PROP_1, "name": "1B", "marketRent": 1250},
            {"id": UNIT_3, "property": PROP_2, "name": "4", "marketRent": 900},
        ],
        "/leases": [
            {
                "id": LEASE_1,
                "property": PROP_1,
                "units": [UNIT_1],
                "status": "ACTIVE",
                "start": "2026-01-01",
                "end": "2026-12-31",
                "totalRecurringRent": 1200,
                "totalDepositsHeld": 1200,
                "totalBalancedue": 150,
            },
            {
                "id": LEASE_2,
                "property": PROP_2,
                "units": [UNIT_3],
                "status": "INACTIVE",
                "start": "2024-01-01",
                "end": "2024-12-31",
                "totalRecurringRent": 900,
                "totalDepositsHeld": 0,
                "totalBalancedue": 0,
            },
        ],
        # LEASE_1: one co-tenant left in March and the other gave notice for
        # the end of the month, so the lease turns over then. LEASE_2's last
        # tenant left in 2024, long past the turnover lookback.
        "/leases/tenants": [
            {
                "id": oid(61),
                "lease": LEASE_1,
                "tenant": {"id": TENANT_1},
                "status": "CURRENT",
                "moveOutNoticeAt": "2026-09-30",
                "moveOutAt": "2026-10-31",
            },
            {
                "id": oid(62),
                "lease": LEASE_1,
                "tenant": {"id": TENANT_2},
                "status": "PAST",
                "moveOutAt": "2026-03-01",
            },
            {
                "id": oid(63),
                "lease": LEASE_2,
                "tenant": {"id": TENANT_3},
                "status": "PAST",
                "moveOutAt": "2024-12-31",
            },
        ],
        "/tenants": [
            {"id": TENANT_1, "firstName": "Jane", "lastName": "Doe", "type": "LEASE_TENANT"},
            {"id": TENANT_2, "firstName": "Bob", "lastName": "Roe", "type": "LEASE_TENANT"},
            {"id": TENANT_3, "firstName": "Ann", "lastName": "Poe", "type": "LEASE_TENANT"},
            {
                "id": PROSPECT,
                "firstName": "Pat",
                "lastName": "Moe",
                "type": "PROSPECT_TENANT",
                "emails": [{"type": "Primary", "address": "pat.moe@example.test"}],
                "prospectInfo": {
                    "status": "APPLICATION_SUBMITTED",
                    "interests": [{"property": PROP_1, "unit": UNIT_2}],
                },
            },
            {
                "id": LEAD,
                "firstName": "Lee",
                "lastName": "Shaw",
                "type": "PROSPECT_TENANT",
                "prospectInfo": {"status": "SHOWING_SCHEDULED", "interests": []},
            },
            {
                "id": APPLICANT_2,
                "firstName": "Ada",
                "lastName": "Ng",
                "type": "PROSPECT_TENANT",
                "prospectInfo": {
                    "status": "LEASE_OFFER_ACCEPTED",
                    "interests": [{"property": PROP_1}, {"property": PROP_2}],
                },
            },
        ],
        "/owners": [{"id": OWNER_1, "companyName": "Synthetic Holdings LLC"}],
        "/vendors": [{"id": VENDOR_1, "name": "Fix-It Co", "active": True}],
        "/tasks": [
            {
                "id": WORK_ORDER_1,
                "type": "WORK_ORDER",
                "subject": "Leaking faucet",
                "status": "IN_PROGRESS",
                "priority": "HIGH",
                "property": PROP_1,
                "unit": UNIT_1,
                "updatedAt": 1_759_700_000_000,
                "workOrder": {"assignedToVendor": VENDOR_1, "billed": True, "vendorBill": BILL_1},
            },
            {
                "id": TODO_1,
                "type": "INTERNAL_TASK",
                "subject": "Renew the boiler permit",
                "status": "NOT_STARTED",
                "priority": "LOW",
                "dueDate": "2026-10-20",
                "property": PROP_2,
            },
            {
                "id": TENANT_REQUEST_1,
                "type": "TENANT_REQUEST",
                "subject": "Spare key, please",
                "status": "RECEIVED",
                "priority": "MEDIUM",
                "property": PROP_1,
                "unit": UNIT_1,
                "requestedByTenant": TENANT_1,
            },
            {
                "id": OWNER_REQUEST_1,
                "type": "OWNER_REQUEST",
                "subject": "Send the September statement",
                "status": "COMPLETED",
                "property": PROP_1,
                "requestedByOwner": OWNER_1,
            },
        ],
        # BILL_1 is the work order's, unpaid; BILL_2 is paid, with one line
        # charged to no property (the company's own spend).
        "/vendor-bills": [
            {
                "id": BILL_1,
                "vendor": VENDOR_1,
                "date": "2026-09-28",
                "dueDate": "2026-10-28",
                "reference": "INV-1001",
                "totalAmount": 300,
                "totalBalance": 300,
                "lines": [
                    {
                        "id": oid(141),
                        "account": ACCT_REPAIRS,
                        "amount": 300,
                        "balance": 300,
                        "memo": "Faucet cartridge",
                        "linkedToResourceType": "property",
                        "linkedToResourceId": PROP_1,
                    }
                ],
            },
            {
                "id": BILL_2,
                "vendor": VENDOR_1,
                "date": "2026-08-15",
                "dueDate": "2026-09-14",
                "reference": "INV-0980",
                "totalAmount": 120,
                "totalBalance": 0,
                "lines": [
                    {
                        "id": oid(142),
                        "account": ACCT_REPAIRS,
                        "amount": 80,
                        "balance": 0,
                        "linkedToResourceType": "property",
                        "linkedToResourceId": PROP_2,
                    },
                    {"id": oid(143), "account": ACCT_OFFICE, "amount": 40, "balance": 0},
                ],
            },
        ],
        "/accounts": [
            {"id": ACCT_BANK, "name": "Operating account", "type": "ASSET_BANK", "active": True},
            {"id": ACCT_REPAIRS, "name": "Repairs", "type": "EXPENSE_EXPENSE", "active": True},
            {"id": ACCT_RENT, "name": "Rent income", "type": "REVENUE_INCOME", "active": True},
            {"id": ACCT_OFFICE, "name": "Office", "type": "EXPENSE_OTHER_EXPENSE", "active": False},
        ],
        "/lease-charges": [
            {
                "id": oid(81),
                "lease": LEASE_1,
                "date": "2026-09-30",
                "lines": [{"amount": 1200, "balance": 100}],
            },
            {
                "id": oid(82),
                "lease": LEASE_1,
                "date": "2026-06-01",
                "lines": [{"amount": 50, "balance": 50}],
            },
        ],
        "/lease-credits": [
            {"id": oid(91), "lease": LEASE_1, "date": "2026-09-01", "lines": [{"amount": 25}]},
        ],
        "/lease-payments": [
            {"id": oid(101), "lease": LEASE_1, "date": "2026-10-01", "amountReceived": 1100},
        ],
        "/lease-reversed-payments": [],
    }


def settings(*, enabled: bool = True) -> Settings:
    """Settings with the DoorLoop switch set and a test data key."""
    base = Settings()
    switches = base.doorloop.model_copy(update={"enabled": enabled})
    return base.model_copy(update={"data_key": TEST_KEY, "doorloop": switches})


def creds_row(*, company_id: str = COMPANY, api_key: str = API_KEY) -> dict[str, Any]:
    """A ``company_doorloop_credentials`` row, encrypted with the test key."""
    cipher = CredentialCipher(settings())
    return {"company_id": company_id, "encrypted_api_key": cipher.encrypt(api_key)}


class FakeDoorLoop:
    """DoorLoop's documented list behaviour over a synthetic account."""

    def __init__(
        self,
        rows: Mapping[str, list[dict[str, Any]]] | None = None,
        *,
        fail: Mapping[str, int] | None = None,
        totals: Mapping[str, int] | None = None,
        filters: bool = True,
        key: str = API_KEY,
    ):
        source = rows if rows is not None else account()
        self.rows = {k: copy.deepcopy(list(v)) for k, v in source.items()}
        self.fail = dict(fail or {})
        self.totals = dict(totals or {})
        #: Whether ``filter_type`` is honoured (documented on ``GET /tasks``).
        self.filters = filters
        #: The one key answered; any other is a 401.
        self.key = key
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"bearer {self.key}":
            return httpx.Response(401, json={"message": "Unauthorized"})
        path = request.url.path.removeprefix(API_PREFIX)
        if request.method != "GET" or path not in self.rows:
            return httpx.Response(404, json={"message": "Not found"})
        if path in self.fail:
            return httpx.Response(self.fail[path], json={"message": "synthetic failure"})
        params = request.url.params
        rows = list(self.rows[path])
        if path == "/tasks" and "filter_type" in params and self.filters:
            rows = [r for r in rows if r.get("type") == params["filter_type"]]
        page = int(params.get("page_number", "1"))
        size = int(params.get("page_size", "50"))
        if size > 1000:
            return httpx.Response(400, json={"message": "page_size above 1000"})
        start = (page - 1) * size
        body = {"total": self.totals.get(path, len(rows)), "data": rows[start : start + size]}
        return httpx.Response(200, json=body)

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def paths(self) -> list[str]:
        return [r.url.path.removeprefix(API_PREFIX) for r in self.requests]


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class FakeConn:
    def __init__(self, pool: FakePool):
        self.pool = pool

    async def execute(self, sql: str, *args: Any) -> str:
        reject_unbindable(sql, args)
        reject_untyped_mirror_bind(sql, args)
        if self.pool.fail_on and self.pool.fail_on in sql:
            raise RuntimeError("synthetic database failure")
        self.pool.log.append((_norm(sql), args))
        return "DELETE 0" if sql.lstrip().upper().startswith("DELETE") else "INSERT 0 1"

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        reject_unbindable(sql, args)
        self.pool.log.append((_norm(sql), args))
        if "INSERT INTO helixis_turnovers" in sql:
            # open_turnover_if_new: a row only when new or its date moved.
            lease_id, move_out = args[1], args[4]
            if self.pool.turnovers.get(lease_id) == move_out:
                return None
            self.pool.turnovers[lease_id] = move_out
            return {"id": f"turnover-{lease_id}"}
        return None

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        reject_unbindable(sql, args)
        self.pool.log.append((_norm(sql), args))
        return []

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield


class FakePool:
    """Records every statement; answers the credentials and timezone reads.

    ``creds`` is one row (or ``None``) for every read, or a list answered in
    order, the last repeated: ``[row, None]`` is a disconnect after the first.
    """

    def __init__(
        self,
        *,
        creds: dict[str, Any] | list[dict[str, Any] | None] | None = None,
        timezone: str | None = "America/Chicago",
        fail_on: str | None = None,
    ):
        self.creds = creds if isinstance(creds, list) else [creds]
        self.timezone = timezone
        self.fail_on = fail_on
        self.log: list[tuple[str, tuple[Any, ...]]] = []
        self.creds_reads = 0
        #: ``helixis_turnovers``: ``{lease mirror id: move-out date}``.
        self.turnovers: dict[int, date] = {}

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        reject_unbindable(sql, args)
        self.log.append((_norm(sql), args))
        if "company_doorloop_credentials" in sql:
            row = self.creds[min(self.creds_reads, len(self.creds) - 1)]
            self.creds_reads += 1
            return row
        if "FROM public.companies" in sql:
            return {"timezone": self.timezone}
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        reject_unbindable(sql, args)
        self.log.append((_norm(sql), args))
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        reject_unbindable(sql, args)
        self.log.append((_norm(sql), args))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("synthetic database failure")
        return "UPDATE 1"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        reject_unbindable(sql, args)
        self.log.append((_norm(sql), args))
        return []

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakeConn]:
        yield FakeConn(self)

    # ── reading the log ───────────────────────────────────────────────

    def statements(self, starts: str) -> list[tuple[str, tuple[Any, ...]]]:
        return [(s, a) for s, a in self.log if s.upper().startswith(starts.upper())]

    def inserts(self, table: str) -> list[tuple[str, tuple[Any, ...]]]:
        return self.statements(f"INSERT INTO {table} ")

    def deletes(self, table: str) -> list[tuple[str, tuple[Any, ...]]]:
        return self.statements(f"DELETE FROM {table} ")

    def stamped(self) -> set[str]:
        """The freshness stages ``record_sync`` stamped."""
        return {args[1] for _sql, args in self.inserts("helixis_mirror_sync_state")}

    def writes(self) -> list[str]:
        """Every statement that changes a mirror table."""
        return [
            s
            for s, _a in self.log
            if s.upper().startswith(
                ("INSERT INTO HELIXIS_", "DELETE FROM HELIXIS_", "UPDATE HELIXIS_")
            )
            and "helixis_mirror_sync_state" not in s
        ]


def inserted_rows(pool: FakePool, table: str) -> list[dict[str, Any]]:
    """Each upsert into ``table`` as ``{column: value}``, read back from the
    statement's own column list."""
    out = []
    for sql, args in pool.inserts(table):
        columns = sql.split("(", 1)[1].split(")", 1)[0]
        names = [c.strip() for c in columns.split(",")]
        out.append(dict(zip(names, args, strict=False)))
    return out
