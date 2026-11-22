"""``scripts/rentvine_test_records.py``: one payment and one move-out in the
sandbox, so the payment and turnover sync paths meet real rows.

Offline: a fake transport stands in for Rentvine. All rows are synthetic.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from typing import Any

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "rentvine_test_records", REPO / "scripts" / "rentvine_test_records.py"
)
assert _spec and _spec.loader
tr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tr)

ACCOUNT = "demoaccount41"

#: An export row per lease: 1 owes nothing and has a notice already, 2 owes
#: $450, 3 has moved out, 4 owes nothing and has neither (so: pay 2, move 4).
EXPORT = [
    {
        "lease": {"leaseID": 1, "expectedMoveOutDate": "2026-11-01"},
        "balances": {"unpaidTotalAmount": "0.00"},
    },
    {"lease": {"leaseID": 2}, "balances": {"unpaidTotalAmount": "450.00"}},
    {"lease": {"leaseID": 3, "moveOutDate": "2026-09-01"}, "balances": {}},
    {"lease": {"leaseID": 4}, "balances": {"unpaidTotalAmount": "0.00"}},
]
LEASE_4 = {
    "leaseID": 4,
    "startDate": "2026-01-01",
    "endDate": "2026-12-31",
    "assignedToUserID": "7",
    "sendStatements": 1,
    "noticeDate": None,
    "expectedMoveOutDate": None,
    "rentAmount": "1450.00",  # not an updateLease field: must not be sent
}


class _Fake:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.lease4 = dict(LEASE_4)

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, str, str | None]:
        path = url.split("/api/manager", 1)[1]
        sent = json.loads(body) if body else None
        self.calls.append((method, path, sent))
        if path.startswith("/leases/export"):
            return 200, json.dumps(EXPORT if "page=1" in path else []), None
        if method == "GET" and path.startswith("/leases/2"):
            tenants = [
                {"leaseTenant": {"leaseTenantID": "40", "isActive": "0"}, "contact": {}},
                {"leaseTenant": {"leaseTenantID": "41", "isActive": "1"}, "contact": {}},
            ]
            return 200, json.dumps({"lease": {"leaseID": 2}, "tenants": tenants}), None
        if path == "/leases/4" and method == "GET":
            return 200, json.dumps({"lease": self.lease4}), None
        if path == "/leases/4" and method == "POST":
            self.lease4.update(sent)
            return 200, json.dumps({"lease": self.lease4}), None
        if path == "/accounting/leases/2/payments":
            return 200, json.dumps({"transaction": {"transactionID": 9001}}), None
        return 404, '{"error": "not found"}', None


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("RENTVINE_ACCOUNT_CODE", ACCOUNT)
    monkeypatch.setenv("RENTVINE_API_KEY", "k")
    monkeypatch.setenv("RENTVINE_API_SECRET", "s")
    monkeypatch.setattr(tr.seed.time, "sleep", lambda _s: None)
    monkeypatch.chdir(tmp_path)


def _run(fake: _Fake, *extra: str) -> int:
    return tr.main(["--confirm-account", ACCOUNT, *extra], transport=fake)


WRITE = ("--execute", "--i-accept-the-write")


def test_the_dry_run_only_reads():
    fake = _Fake()
    assert _run(fake) == 0
    assert {m for m, _p, _b in fake.calls} == {"GET"}


def test_the_payment_goes_to_a_lease_that_owes_and_emails_nobody():
    fake = _Fake()
    assert _run(fake, *WRITE) == 0
    ((_m, path, body),) = [c for c in fake.calls if c[0] == "POST" and "payments" in c[1]]
    assert path == "/accounting/leases/2/payments"
    assert body["leaseTenantID"] == "41"  # the ACTIVE tenant, not the first
    assert body["emailReceipt"] is False
    assert body["applyAutomatically"] is True
    assert body["amount"] == "100.00"
    manifest = json.loads(pathlib.Path(tr.DEFAULT_MANIFEST).read_text())
    assert manifest["payment"]["transaction_id"] == "9001"


def test_the_move_out_is_read_merge_write_on_a_lease_without_one():
    fake = _Fake()
    assert _run(fake, *WRITE) == 0
    ((_m, path, body),) = [c for c in fake.calls if c[0] == "POST" and c[1] == "/leases/4"]
    # Every accepted field the lease carried goes back unchanged...
    assert body["startDate"] == "2026-01-01" and body["endDate"] == "2026-12-31"
    assert body["assignedToUserID"] == "7" and body["sendStatements"] == 1
    # ...a field updateLease does not accept is not sent...
    assert "rentAmount" not in body
    # ...and only the two dates change.
    assert body["noticeDate"] and body["expectedMoveOutDate"]
    assert body["expectedMoveOutDate"] > body["noticeDate"]
    manifest = json.loads(pathlib.Path(tr.DEFAULT_MANIFEST).read_text())
    assert manifest["move_out"]["before"] == {"noticeDate": None, "expectedMoveOutDate": None}


def test_undo_puts_the_move_out_dates_back():
    fake = _Fake()
    assert _run(fake, *WRITE) == 0
    assert _run(fake, "--undo", *WRITE) == 0
    *_, (_m, path, body) = [c for c in fake.calls if c[0] == "POST"]
    assert path == "/leases/4"
    assert body["noticeDate"] is None and body["expectedMoveOutDate"] is None
    assert body["startDate"] == "2026-01-01"


def test_a_second_write_is_refused_while_a_run_is_recorded():
    fake = _Fake()
    assert _run(fake, *WRITE) == 0
    posts = sum(1 for c in fake.calls if c[0] == "POST")
    assert _run(fake, *WRITE) == 2
    assert sum(1 for c in fake.calls if c[0] == "POST") == posts


@pytest.mark.parametrize(
    "args",
    [
        ("--confirm-account", "someone-else"),
        ("--confirm-account", ACCOUNT, "--execute"),
        ("--confirm-account", ACCOUNT, "--amount", "-5"),
    ],
)
def test_it_refuses_before_sending_anything(args: tuple[str, ...]):
    fake = _Fake()
    assert tr.main(list(args), transport=fake) == 2
    assert fake.calls == []


def test_nothing_owes_enough_stops_without_writing():
    fake = _Fake()
    assert _run(fake, "--amount", "5000.00", *WRITE) == 1
    assert not [c for c in fake.calls if c[0] == "POST"]


# ── --rent-lease: one rent change ────────────────────────────────────────────


class _RentFake(_Fake):
    def __init__(self, charges: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.charges = (
            charges
            if charges is not None
            else [
                # the move-in charge happens to equal the rent: must be skipped
                {"leaseRecurringChargeID": "70", "amount": "800.00", "isMoveInCharge": "1"},
                {"leaseRecurringChargeID": "71", "amount": "800.00", "isMoveInCharge": "0"},
                {"leaseRecurringChargeID": "72", "amount": "25.00", "isMoveInCharge": "0"},
            ]
        )

    def __call__(self, method, url, headers, body):  # type: ignore[no-untyped-def]
        path = url.split("/api/manager", 1)[1]
        if path.startswith("/leases/export"):
            self.calls.append((method, path, None))
            lease_6 = {"lease": {"leaseID": 6, "dateTimeModified": "2026-10-01 04:18:30"}}
            rows = [*EXPORT, lease_6]
            return 200, json.dumps(rows if "page=1" in path else []), None
        if path == "/leases/6" and method == "GET":
            self.calls.append((method, path, None))
            # getLease's stamp DIFFERS from the export's (measured): never recorded.
            lease = {"leaseID": 6, "rentAmount": "800.00", "dateTimeModified": "2026-09-08"}
            return 200, json.dumps({"lease": lease}), None
        if path == "/leases/6/recurring-charges":
            self.calls.append((method, path, None))
            rows = [{"recurringCharge": c, "account": {}} for c in self.charges]
            return 200, json.dumps(rows), None
        if path.startswith("/leases/6/recurring-charges/"):
            self.calls.append((method, path, json.loads(body) if body else None))
            return 200, json.dumps({"recurringCharge": {}}), None
        return super().__call__(method, url, headers, body)


def test_the_rent_change_sends_only_the_amount_to_the_rent_charge():
    fake = _RentFake()
    assert _run(fake, "--rent-lease", "6", *WRITE) == 0
    ((_m, path, body),) = [c for c in fake.calls if c[0] == "POST"]
    assert path == "/leases/6/recurring-charges/71"  # not the move-in charge (70)
    assert body == {"amount": "825.00"}  # partial update: nothing else is sent
    rc = json.loads(pathlib.Path(tr.DEFAULT_MANIFEST).read_text())["rent_change"]
    assert rc["before"] == "800.00"
    assert rc["modified_before"] == "2026-10-01 04:18:30"  # the export's, not getLease's


def test_an_explicit_rent_amount_is_used():
    fake = _RentFake()
    assert _run(fake, "--rent-lease", "6", "--rent-amount", "850.00", *WRITE) == 0
    ((_m, _p, body),) = [c for c in fake.calls if c[0] == "POST"]
    assert body == {"amount": "850.00"}


def test_an_ambiguous_rent_charge_stops_without_writing():
    two = [
        {"leaseRecurringChargeID": "71", "amount": "800.00", "isMoveInCharge": "0"},
        {"leaseRecurringChargeID": "73", "amount": "800.00", "isMoveInCharge": "0"},
    ]
    fake = _RentFake(two)
    assert _run(fake, "--rent-lease", "6", *WRITE) == 1
    assert not [c for c in fake.calls if c[0] == "POST"]


def test_undo_puts_the_rent_back():
    fake = _RentFake()
    assert _run(fake, "--rent-lease", "6", *WRITE) == 0
    assert _run(fake, "--undo", *WRITE) == 0
    *_, (_m, path, body) = [c for c in fake.calls if c[0] == "POST"]
    assert path == "/leases/6/recurring-charges/71" and body == {"amount": "800.00"}


def test_a_rent_change_runs_beside_an_earlier_payment_run():
    """The other session's manifest already records the payment run; the rent
    change is its own kind, so it must not be refused for that."""
    fake = _RentFake()
    assert _run(fake, *WRITE) == 0
    assert _run(fake, "--rent-lease", "6", *WRITE) == 0
    assert _run(fake, "--rent-lease", "6", *WRITE) == 2  # but not twice
