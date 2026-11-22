"""``scripts/rentvine_seed_sandbox.py`` writes to a real Rentvine account, and
Rentvine documents no rate limit. These pin, against a fake Rentvine, what the
script promises before anyone runs it for real:

* the plan is counted before the first request and refused over budget;
* requests are at least one second apart, whatever ``--interval`` says;
* the first 429 stops the run without a retry, the next run resumes, and a
  ``Retry-After`` binds the NEXT run too (terms rule 1);
* the property and all its units are ONE request, and nothing it sends can
  email anybody;
* ``--smoke`` exercises every create the full run makes, then reads each record
  back and FAILS when Rentvine didn't keep what was sent;
* ``--teardown`` removes everything a manifest records, in dependency order,
  and understands how Rentvine says "not found" (a 400, not a 404);
* it refuses any account but the one named, and writes nothing unless asked.

The fake answers in the envelopes the captured spec's own examples show
(``docs/rentvine/vendor/docs.rentvine.com/openapi.json``): vendors and tenants
read back as ``{"contact": ...}``, work orders as ``{"workOrder": ...}``.
"""

from __future__ import annotations

import base64
import email.utils
import importlib.util
import json
import pathlib
import re
import sys
import time
from typing import Any

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "rentvine_seed_sandbox.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("rentvine_seed_sandbox", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["rentvine_seed_sandbox"] = module
    spec.loader.exec_module(module)
    return module


seed = _load()

ACCOUNT = "demoaccount41"
KEY = "key-value"
SECRET = "s3cr3t-value"

#: kind → (collection path, the name Rentvine's "not found" error uses).
_KINDS = {
    "property": ("/properties", "Property"),
    "vendor": ("/vendors", "Vendor"),
    "tenant": ("/tenants", "Tenant"),
    "lease": ("/leases", "Lease"),
    "work_order": ("/maintenance/work-orders", "Work order"),
}
_KIND_OF = {path: kind for kind, (path, _label) in _KINDS.items()}
#: The spec's own refusal examples (tenant/vendor: a stand-in, none documented).
_REFUSAL = {
    "property": {"transactions": ["Cannot delete a property that has accounting transactions"]},
    "lease": {"error": "Cannot delete a lease that has accounting transactions"},
    "work_order": {"error": "Cannot delete work order - 2 bills are associated."},
    "tenant": {"error": "Cannot delete a tenant with activity"},
    "vendor": {"error": "Cannot delete a vendor with activity"},
}
_STATUS_PRIMARY = {"11": 2, "14": 4}
#: Shaped like demoaccount41 (measured 2026-09-24): the FIRST property is a
#: single-unit type, and a multi-unit one comes later. Type "1" cannot carry
#: units, and Rentvine refused the first live smoke run for sending it.
_PROPERTIES = [
    {"property": {"propertyID": "1", "propertyTypeID": "1", "isMultiUnit": 0, "portfolioID": "9"}},
    {
        "property": {
            "propertyID": "7",
            "propertyTypeID": "2",
            "isMultiUnit": 1,
            "portfolioID": "416",
            "city": "Estero",
            "stateID": "FL",
        }
    },
]
_MULTI_UNIT_TYPES = {"2", "6"}


class FakeRentvine:
    """A small Rentvine that keeps what the script creates, reads it back in
    the spec's envelopes, and deletes it. Records every request."""

    def __init__(
        self,
        *,
        units_listed: int | None = None,
        fail_on: int | None = None,
        fail_status: int = 429,
        fail_body: str = "slow down",
        retry_after: str | None = None,
        echo_vendor: bool = True,
        honour_status: bool = True,
        refuse_delete: tuple[str, ...] = (),
        properties: list[dict[str, Any]] | None = None,
    ) -> None:
        self.requests: list[tuple[str, str, Any]] = []
        self.properties = _PROPERTIES if properties is None else properties
        self.units_listed = units_listed
        self.fail_on = fail_on
        self.fail_status = fail_status
        self.fail_body = fail_body
        self.retry_after = retry_after
        self.echo_vendor = echo_vendor
        self.honour_status = honour_status
        self.refuse_delete = refuse_delete
        self.store: dict[str, dict[str, dict[str, Any]]] = {k: {} for k in _KINDS}
        self.units: dict[str, list[str]] = {}
        self.created: list[tuple[str, str]] = []
        self.deactivated: list[tuple[str, str]] = []
        self.next_id = 1000

    def _id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, str, str | None]:
        path = url.split("/api/manager", 1)[1]
        payload = json.loads(body) if body else None
        self.requests.append((method, path, payload))
        if self.fail_on is not None and len(self.requests) == self.fail_on:
            return self.fail_status, self.fail_body, self.retry_after
        status, answer = self._answer(method, path, payload)
        return status, json.dumps(answer), None

    def _create(self, kind: str, rec: dict[str, Any]) -> dict[str, Any]:
        rid = self._id()
        rec[
            {"property": "propertyID", "lease": "leaseID", "work_order": "workOrderID"}.get(
                kind, "contactID"
            )
        ] = rid
        self.store[kind][rid] = rec
        self.created.append((kind, rid))
        return rec

    def _answer(self, method: str, path: str, payload: Any) -> tuple[int, Any]:
        if method == "GET" and path.startswith("/properties?"):
            return 200, self.properties
        if method == "GET" and path == "/leases":
            listed = [{"lease": {"leaseID": "1", "startDate": "2025-01-01"}}]
            return 200, listed + [{"lease": r} for r in self.store["lease"].values()]
        if method == "GET" and path == "/maintenance/work-order/statuses":
            return 200, [
                {"workOrderStatus": {"workOrderStatusID": s, "primaryWorkOrderStatusID": p}}
                for s, p in _STATUS_PRIMARY.items()
            ]

        if method == "POST" and path == "/properties":
            prop = payload["property"]
            if prop.get("isMultiUnit") and prop.get("propertyTypeID") not in _MULTI_UNIT_TYPES:
                return 400, {"propertyTypeID": ["This property type cannot have multiple units"]}
            # The second live refusal: every unit needs its own address.
            unit_errors = {
                f"unitContract.unit.{k}": [f"{k} is required"]
                for u in payload["units"]
                for k in ("address", "city", "stateID")
                if not u.get(k)
            }
            if unit_errors:
                return 400, unit_errors
        if method == "POST" and path in _KIND_OF:
            return 200, self._post(_KIND_OF[path], payload)

        m = re.fullmatch(r"/properties/(\d+)/units", path)
        if method == "GET" and m:
            units = self.units.get(m[1], [])
            if self.units_listed is not None:
                units = units[: self.units_listed]
            return 200, [{"unit": {"unitID": u}} for u in units]
        m = re.fullmatch(r"/leases/(\d+)/tenants", path)
        if method == "GET" and m and m[1] in self.store["lease"]:
            return 200, [
                {
                    "leaseTenant": {"leaseTenantID": f"9{t}", "contactID": t},
                    "contact": {"contactID": t},
                }
                for t in self.store["lease"][m[1]]["tenants"]
            ]
        m = re.fullmatch(r"(/.+)/(\d+)/deactivate", path)
        if method == "POST" and m and m[1] in _KIND_OF:
            kind = _KIND_OF[m[1]]
            if m[2] not in self.store[kind]:
                return self._not_found(kind, m[2])
            self.store[kind][m[2]]["isActive"] = False
            self.deactivated.append((kind, m[2]))
            return 200, self.store[kind][m[2]]
        m = re.fullmatch(r"(/.+)/(\d+)", path)
        if m and m[1] in _KIND_OF:
            kind, rid = _KIND_OF[m[1]], m[2]
            if rid not in self.store[kind]:
                return self._not_found(kind, rid)
            if method == "GET":
                return 200, self._read_back(kind, rid)
            if method == "DELETE":
                if kind in self.refuse_delete:
                    return 400, _REFUSAL[kind]
                if kind == "property":
                    self.units.pop(rid, None)
                return 200, self.store[kind].pop(rid)
        return 404, {"error": f"no route {method} {path}"}

    def _post(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == "property":
            rec = self._create("property", {"name": payload["property"]["name"]})
            self.units[rec["propertyID"]] = [self._id() for _ in payload["units"]]
            return {"property": rec}
        if kind in ("vendor", "tenant"):
            return {"contact": self._create(kind, {"name": payload["name"]})}
        if kind == "lease":
            tenants = [t["contactID"] for t in payload["tenants"]]
            rec = {"unitID": payload["lease"]["unitID"], "tenants": tenants}
            return {"lease": self._create("lease", rec)}
        sid = payload["workOrderStatusID"] if self.honour_status else "11"
        rec = {"workOrderStatusID": sid, "primaryWorkOrderStatusID": _STATUS_PRIMARY[sid]}
        if self.echo_vendor and "vendorContactID" in payload:
            rec["vendorContactID"] = payload["vendorContactID"]
        return {"workOrder": self._create("work_order", rec)}

    def _read_back(self, kind: str, rid: str) -> Any:
        rec = self.store[kind][rid]
        if kind == "tenant":
            return {"contact": rec, "leases": [], "portalAccounts": [], "portalInvitations": []}
        if kind == "vendor":
            return {"contact": rec}
        if kind == "work_order":
            return {"workOrder": rec, "schedulingStatusID": None}
        return {kind: rec}

    def _not_found(self, kind: str, rid: str) -> tuple[int, Any]:
        # The spec: a 404 on work orders only; everything else says it with a 400.
        return (404 if kind == "work_order" else 400), {
            "error": f"{_KINDS[kind][1]} {rid} not found"
        }

    def posts(self, path: str) -> list[Any]:
        return [b for m, p, b in self.requests if m == "POST" and p == path]

    def sent(self, method: str) -> list[str]:
        return [p for m, p, _b in self.requests if m == method]

    def left_behind(self) -> dict[str, list[str]]:
        """Every record the script created that still exists and is active."""
        return {
            kind: [r for r, rec in recs.items() if rec.get("isActive", True)]
            for kind, recs in self.store.items()
            if any(rec.get("isActive", True) for rec in recs.values())
        }


def _template(path: str) -> str:
    return re.sub(r"/\d+", "/{id}", path.split("?", 1)[0])


@pytest.fixture(autouse=True)
def _creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENTVINE_ACCOUNT_CODE", ACCOUNT)
    monkeypatch.setenv("RENTVINE_API_KEY", KEY)
    monkeypatch.setenv("RENTVINE_API_SECRET", SECRET)
    # Pacing is tested with a fake clock; the end-to-end runs must not sleep.
    monkeypatch.setattr(seed.time, "sleep", lambda _s: None)


def _run(fake: FakeRentvine, manifest: pathlib.Path, *extra: str) -> int:
    return seed.main(
        ["--confirm-account", ACCOUNT, "--manifest", str(manifest), *extra], transport=fake
    )


WRITE = ("--execute", "--i-accept-the-write")
SMALL = ("--units", "6", "--leases", "4", "--vendors", "2", "--work-orders", "4")


@pytest.fixture
def m(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "m.json"


def _manifest(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text())


# ── what it refuses ─────────────────────────────────────────────────


def test_a_dry_run_reads_three_lookups_and_writes_nothing(m: pathlib.Path) -> None:
    fake = FakeRentvine()
    assert _run(fake, m) == 0
    assert [meth for meth, _p, _b in fake.requests] == ["GET", "GET", "GET"]
    assert not m.exists()


def test_it_refuses_any_other_account(m: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENTVINE_ACCOUNT_CODE", "real-customer")
    fake = FakeRentvine()
    assert _run(fake, m, *WRITE) == 2
    assert fake.requests == []


def test_execute_alone_is_not_enough(m: pathlib.Path) -> None:
    fake = FakeRentvine()
    assert _run(fake, m, "--execute") == 2
    assert fake.requests == []


def test_credentials_can_only_come_from_the_environment() -> None:
    """argv reaches shell history and process listings."""
    import argparse

    actions: list[argparse.Action] = []
    real_parse = argparse.ArgumentParser.parse_args

    def capture(self: argparse.ArgumentParser, *a: Any, **k: Any) -> Any:
        actions.extend(self._actions)
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = capture  # type: ignore[method-assign]
    try:
        with pytest.raises(SystemExit):
            seed.main(["--confirm-account", ACCOUNT])
    finally:
        argparse.ArgumentParser.parse_args = real_parse  # type: ignore[method-assign]
    flags = {opt for a in actions for opt in a.option_strings}
    assert flags and not any("key" in f or "secret" in f for f in flags), flags


# ── the call budget and the pace ────────────────────────────────────


def test_a_plan_over_budget_is_refused_before_any_request(m: pathlib.Path) -> None:
    fake = FakeRentvine()
    assert _run(fake, m, *WRITE, "--leases", "200") == 2
    assert fake.requests == []


def test_the_defaults_fit_the_default_budget() -> None:
    plan = seed.Plan(
        seed.DEFAULT_UNITS, seed.DEFAULT_LEASES, seed.DEFAULT_VENDORS, seed.DEFAULT_WORK_ORDERS
    )
    assert plan.calls() <= seed.DEFAULT_MAX_CALLS


def test_a_full_run_never_makes_more_calls_than_it_planned(m: pathlib.Path) -> None:
    fake = FakeRentvine()
    assert _run(fake, m, *WRITE, *SMALL) == 0
    assert len(fake.requests) <= seed.Plan(6, 4, 2, 4).calls()


def test_the_client_refuses_the_call_past_its_budget() -> None:
    client = seed.PacedClient(seed.Creds(), FakeRentvine(), interval=1, max_calls=1)
    client.call("GET", "/leases")
    with pytest.raises(seed.SeedStopError, match="budget"):
        client.call("GET", "/leases")


def test_requests_are_at_least_a_second_apart_whatever_interval_says() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    client = seed.PacedClient(
        seed.Creds(), FakeRentvine(), interval=0.05, max_calls=10, sleep=sleep, clock=lambda: now[0]
    )
    for _ in range(3):
        client.call("GET", "/leases")
    assert client.interval == seed.MIN_INTERVAL_S
    assert slept == [seed.MIN_INTERVAL_S, seed.MIN_INTERVAL_S]


# ── stopping, and resuming ──────────────────────────────────────────


def test_the_first_429_stops_the_run_without_a_retry(m: pathlib.Path) -> None:
    fake = FakeRentvine(fail_on=7)  # three lookups, property, units, one vendor, then 429
    assert _run(fake, m, *WRITE, *SMALL) == 1
    assert len(fake.requests) == 7  # the 429'd request was not sent again
    manifest = _manifest(m)
    assert manifest["property_id"] is not None and len(manifest["vendor_ids"]) == 1


def test_the_next_run_resumes_instead_of_creating_twice(m: pathlib.Path) -> None:
    first = FakeRentvine(fail_on=7, fail_status=500)
    _run(first, m, *WRITE, *SMALL)
    second = FakeRentvine()
    assert _run(second, m, *WRITE, *SMALL) == 0
    assert second.posts("/properties") == []  # the property already exists
    assert len(second.posts("/vendors")) == 1  # only the vendor still missing
    manifest = _manifest(m)
    assert len(manifest["leases"]) == 4 and len(manifest["work_order_ids"]) == 4


def test_a_manifest_from_another_account_is_refused(m: pathlib.Path) -> None:
    m.write_text(json.dumps({"account": "someone-else"}))
    fake = FakeRentvine()
    assert _run(fake, m, *WRITE, *SMALL) == 2
    assert fake.requests == []


# ── Retry-After binds the next run (terms rule 1) ──────────────────


def test_a_retry_after_is_saved_and_the_next_run_is_refused_before_it(m: pathlib.Path) -> None:
    before = time.time()
    assert _run(FakeRentvine(fail_on=7, retry_after="120"), m, *WRITE, *SMALL) == 1
    not_before = _manifest(m)["not_before"]
    assert before + 120 <= not_before <= time.time() + 120

    again = FakeRentvine()
    assert _run(again, m, *WRITE, *SMALL) == 2
    assert again.requests == []  # nothing at all is sent inside the wait


def test_a_429_that_names_no_wait_still_holds_the_next_run(m: pathlib.Path) -> None:
    before = time.time()
    assert _run(FakeRentvine(fail_on=7), m, *WRITE, *SMALL) == 1
    assert _manifest(m)["not_before"] >= before + seed.UNREADABLE_RETRY_AFTER_S


def test_an_http_date_retry_after_counts(m: pathlib.Path) -> None:
    later = time.time() + 300
    stamp = email.utils.formatdate(later, usegmt=True)
    assert _run(FakeRentvine(fail_on=7, retry_after=stamp), m, *WRITE, *SMALL) == 1
    assert _manifest(m)["not_before"] >= later - 2  # the date has one-second resolution


def test_a_5xx_without_retry_after_does_not_hold_the_next_run(m: pathlib.Path) -> None:
    assert _run(FakeRentvine(fail_on=7, fail_status=503), m, *WRITE, *SMALL) == 1
    assert not _manifest(m)["not_before"]


def test_a_dry_run_that_is_throttled_still_records_the_wait(m: pathlib.Path) -> None:
    """Even the three read-only lookups are bound by a Retry-After."""
    assert _run(FakeRentvine(fail_on=2, retry_after="90"), m) == 1
    manifest = _manifest(m)
    assert manifest["not_before"] >= time.time() + 80
    assert manifest["property_id"] is None  # and nothing else was recorded


def test_once_the_wait_has_passed_the_run_goes_ahead_and_clears_it(m: pathlib.Path) -> None:
    _run(FakeRentvine(fail_on=7, retry_after="120"), m, *WRITE, *SMALL)
    manifest = _manifest(m)
    manifest["not_before"] = time.time() - 1
    m.write_text(json.dumps(manifest))
    assert _run(FakeRentvine(), m, *WRITE, *SMALL) == 0
    assert _manifest(m)["not_before"] is None


def test_teardown_is_held_by_a_retry_after_too(m: pathlib.Path) -> None:
    fake = FakeRentvine()
    _run(fake, m, "--smoke", *WRITE)
    manifest = _manifest(m)
    manifest["not_before"] = time.time() + 60
    m.write_text(json.dumps(manifest))
    before = len(fake.requests)
    assert _run(fake, m, "--smoke", "--teardown", *WRITE) == 2
    assert len(fake.requests) == before


# ── what it sends ───────────────────────────────────────────────────


def test_the_property_and_every_unit_are_one_request(m: pathlib.Path) -> None:
    fake = FakeRentvine()
    _run(fake, m, *WRITE, *SMALL)
    (body,) = fake.posts("/properties")
    assert len(body["units"]) == 6
    assert body["property"]["portfolioID"] == "416"  # copied from the account's own property


def test_settings_come_from_a_multi_unit_property_not_the_first_one(m: pathlib.Path) -> None:
    """The first live smoke run copied propertyTypeID "1" (single-unit) off the
    account's first property and Rentvine refused the Multi Unit create."""
    fake = FakeRentvine()
    assert _run(fake, m, "--smoke", *WRITE) == 0
    (body,) = fake.posts("/properties")
    assert body["property"]["propertyTypeID"] == "2"
    assert body["property"]["isMultiUnit"] is True


def test_no_multi_unit_property_stops_before_any_write(m: pathlib.Path) -> None:
    """No documented endpoint lists property types, so there is nothing to
    fall back on: stop, never guess an id."""
    fake = FakeRentvine(properties=_PROPERTIES[:1])
    assert _run(fake, m, "--smoke", *WRITE) == 1
    assert fake.sent("POST") == []
    assert len(fake.requests) == 3  # the three lookups, nothing else


def test_the_template_lookup_is_still_one_call(m: pathlib.Path) -> None:
    fake = FakeRentvine()
    _run(fake, m, "--smoke")
    (lookup,) = [p for p in fake.sent("GET") if p.startswith("/properties?")]
    assert f"pageSize={seed._TEMPLATE_PAGE_SIZE}" in lookup


def test_nothing_it_sends_can_email_anybody(m: pathlib.Path) -> None:
    fake = FakeRentvine()
    _run(fake, m, *WRITE, *SMALL)
    for wo in fake.posts("/maintenance/work-orders"):
        assert wo["sendEmail"] is False
        for field in seed.NEVER_ECHOED:
            assert wo[field] is False, field
    for lease in fake.posts("/leases"):
        assert lease["sendPortalInvitations"] is False
    for contact in fake.posts("/tenants") + fake.posts("/vendors"):
        assert not any("mail" in k.lower() for k in contact), contact


def test_every_record_it_creates_is_tagged(m: pathlib.Path) -> None:
    fake = FakeRentvine()
    _run(fake, m, *WRITE, *SMALL)
    names = [b["property"]["name"] for b in fake.posts("/properties")]
    names += [b["name"] for b in fake.posts("/tenants") + fake.posts("/vendors")]
    names += [b["description"] for b in fake.posts("/maintenance/work-orders")]
    assert names and all(n.startswith(seed.PREFIX) for n in names), names


def test_it_assigns_vendors_and_puts_some_work_on_hold(m: pathlib.Path) -> None:
    fake = FakeRentvine()
    _run(fake, m, *WRITE, *SMALL)
    orders = fake.posts("/maintenance/work-orders")
    assert any("vendorContactID" in wo for wo in orders)
    assert any(wo["workOrderStatusID"] == "14" for wo in orders)  # On Hold


def test_a_short_units_list_is_recorded_as_a_finding(m: pathlib.Path) -> None:
    fake = FakeRentvine(units_listed=5)
    _run(fake, m, *WRITE, *SMALL)
    assert any("5 of 6 units" in f for f in _manifest(m)["findings"])


def test_an_error_body_never_prints_the_secret() -> None:
    def leaky(*_a: Any) -> tuple[int, str, str | None]:
        return 401, f"bad credentials {SECRET}", None

    client = seed.PacedClient(seed.Creds(), leaky, interval=1, max_calls=5)
    with pytest.raises(seed.SeedStopError) as exc:
        client.call("GET", "/leases")
    assert SECRET not in str(exc.value) and "***" in str(exc.value)
    assert SECRET not in exc.value.body


def test_nothing_the_script_prints_carries_a_credential(
    m: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every mode, including the stop path, with Rentvine echoing both halves."""
    basic = base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
    echo = f"denied for {KEY}:{SECRET} ({basic})"
    _run(FakeRentvine(), m, "--smoke")
    _run(FakeRentvine(), m, "--smoke", *WRITE)
    _run(
        FakeRentvine(fail_on=3, fail_status=401, fail_body=echo), m, "--smoke", "--teardown", *WRITE
    )
    out = capsys.readouterr()
    printed = out.out + out.err + m.read_text()
    assert "denied for" in printed  # the error was shown ...
    for secret in (KEY, SECRET, basic):
        assert secret not in printed  # ... with nothing that authenticates


# ── --smoke: one of each record, read back ─────────────────────────


_SMOKE_CHECKS = {
    "POST /properties",
    "units (Multi Unit create)",
    "POST /vendors",
    "POST /tenants",
    "POST /leases",
    "lease → tenant link",
    "work order → vendorContactID",
    "work order On Hold",
}


def test_a_smoke_run_reads_back_every_record_and_passes(m: pathlib.Path) -> None:
    fake = FakeRentvine()
    assert _run(fake, m, "--smoke", *WRITE) == 0
    checks = _manifest(m)["checks"]
    assert {c["path"] for c in checks} == _SMOKE_CHECKS
    assert all(c["ok"] for c in checks), checks
    # Each read-back asked for a record this run created, never a guessed id.
    created = {rid for _kind, rid in fake.created}
    read_ids = {p.rsplit("/", 1)[-1] for p in fake.sent("GET") if re.search(r"/\d+$", p)}
    assert read_ids and read_ids <= created


def test_the_smoke_run_makes_exactly_the_calls_it_planned(m: pathlib.Path) -> None:
    fake = FakeRentvine()
    _run(fake, m, "--smoke", *WRITE)
    assert len(fake.requests) == seed.Plan(smoke=True, **seed.SMOKE_PLAN_COUNTS).calls()


def test_the_smoke_run_makes_every_kind_of_request_the_full_run_makes(
    tmp_path: pathlib.Path,
) -> None:
    """The point of the smoke run: if it passes, no request the full run sends
    is being sent for the first time."""
    smoke, full = FakeRentvine(), FakeRentvine()
    _run(smoke, tmp_path / "smoke.json", "--smoke", *WRITE)
    _run(full, tmp_path / "full.json", *WRITE, *SMALL)

    def kinds(fake: FakeRentvine) -> set[tuple[str, str]]:
        return {(meth, _template(p)) for meth, p, _b in fake.requests}

    assert kinds(full) <= kinds(smoke)
    # And both work-order shapes go out: one to a vendor, one On Hold.
    orders = smoke.posts("/maintenance/work-orders")
    assert any("vendorContactID" in wo for wo in orders)
    assert any(wo["workOrderStatusID"] == "14" for wo in orders)


def test_a_work_order_that_drops_its_vendor_fails_the_smoke_run(m: pathlib.Path) -> None:
    """The vendorContactID question: a 2xx that didn't keep the vendor is a FAIL."""
    assert _run(FakeRentvine(echo_vendor=False), m, "--smoke", *WRITE) == 1
    failed = {c["path"] for c in _manifest(m)["checks"] if not c["ok"]}
    assert failed == {"work order → vendorContactID"}


def test_a_work_order_that_is_not_on_hold_fails_the_smoke_run(m: pathlib.Path) -> None:
    assert _run(FakeRentvine(honour_status=False), m, "--smoke", *WRITE) == 1
    failed = {c["path"] for c in _manifest(m)["checks"] if not c["ok"]}
    assert failed == {"work order On Hold"}


def test_a_short_units_list_fails_the_smoke_run(m: pathlib.Path) -> None:
    assert _run(FakeRentvine(units_listed=1), m, "--smoke", *WRITE) == 1
    failed = {c["path"] for c in _manifest(m)["checks"] if not c["ok"]}
    assert failed == {"units (Multi Unit create)"}


def test_the_smoke_run_keeps_its_own_manifest_and_property(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fake = FakeRentvine()
    assert seed.main(["--confirm-account", ACCOUNT, "--smoke", *WRITE], transport=fake) == 0
    assert (tmp_path / seed.DEFAULT_SMOKE_MANIFEST).exists()
    assert not (tmp_path / seed.DEFAULT_MANIFEST).exists()
    (body,) = fake.posts("/properties")
    assert body["property"]["name"] != seed.Plan(1, 1, 1, 1).property_name


# ── --teardown ──────────────────────────────────────────────────────


def _smoked(m: pathlib.Path, **fake_kwargs: Any) -> FakeRentvine:
    fake = FakeRentvine(**fake_kwargs)
    assert _run(fake, m, "--smoke", *WRITE) == 0
    fake.requests.clear()
    return fake


def test_a_dry_teardown_sends_nothing_and_keeps_the_manifest(m: pathlib.Path) -> None:
    fake = _smoked(m)
    assert _run(fake, m, "--smoke", "--teardown") == 0
    assert fake.requests == []
    assert m.exists()


def test_teardown_removes_everything_in_dependency_order(m: pathlib.Path) -> None:
    fake = _smoked(m)
    assert _run(fake, m, "--smoke", "--teardown", *WRITE) == 0
    order = [_KIND_OF[_template(p).rsplit("/{id}", 1)[0]] for p in fake.sent("DELETE")]
    assert order == ["work_order", "work_order", "lease", "tenant", "vendor", "property"]
    assert fake.left_behind() == {}


def test_a_full_run_torn_down_leaves_nothing_behind(m: pathlib.Path) -> None:
    fake = FakeRentvine()
    assert _run(fake, m, *WRITE, *SMALL) == 0
    assert _run(fake, m, "--teardown", *WRITE) == 0
    assert fake.left_behind() == {}


def test_a_finished_teardown_retires_the_manifest(m: pathlib.Path) -> None:
    fake = _smoked(m)
    _run(fake, m, "--smoke", "--teardown", *WRITE)
    assert not m.exists()  # so the next seed starts clean, not "resuming"
    (retired,) = m.parent.glob("m.torn-down-*.json")
    assert len(_manifest(retired)["removed"]) == 6


def test_a_teardown_with_no_manifest_is_refused(m: pathlib.Path) -> None:
    fake = FakeRentvine()
    assert _run(fake, m, "--teardown", *WRITE) == 2
    assert fake.requests == []
    assert list(m.parent.iterdir()) == []


def test_rentvine_s_400_not_found_counts_as_already_gone(m: pathlib.Path) -> None:
    """Someone cleaned up by hand in Rentvine. Tenants, vendors, leases and
    properties then answer 400 "... not found"; work orders 404."""
    fake = _smoked(m)
    for recs in fake.store.values():
        recs.clear()
    assert _run(fake, m, "--smoke", "--teardown", *WRITE) == 0
    assert len(fake.sent("DELETE")) == 6 and fake.sent("POST") == []


def test_a_refused_property_delete_deactivates_it_instead(m: pathlib.Path) -> None:
    fake = _smoked(m, refuse_delete=("property",))
    assert _run(fake, m, "--smoke", "--teardown", *WRITE) == 0
    assert fake.sent("POST") == [f"/properties/{_manifest_id(m, 'property')}/deactivate"]
    assert fake.left_behind() == {}


def test_a_refused_lease_delete_stops_the_teardown(m: pathlib.Path) -> None:
    """A lease has no documented fallback, and the tenant and property behind
    it depend on it: stop, and say what is left."""
    fake = _smoked(m, refuse_delete=("lease",))
    assert _run(fake, m, "--smoke", "--teardown", *WRITE) == 1
    kinds = [_KIND_OF[_template(p).rsplit("/{id}", 1)[0]] for p in fake.sent("DELETE")]
    assert kinds == ["work_order", "work_order", "lease"]
    assert fake.sent("POST") == []
    assert [r.split(":")[0] for r in _manifest(m)["removed"]] == ["work_order", "work_order"]


@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_refusal_is_never_mistaken_for_gone_or_deactivated(
    m: pathlib.Path, status: int
) -> None:
    fake = _smoked(m)
    fake.fail_on, fake.fail_status, fake.fail_body = 6, status, '{"error": "Forbidden"}'
    assert _run(fake, m, "--smoke", "--teardown", *WRITE) == 1
    assert fake.sent("POST") == []
    assert "property:" not in " ".join(_manifest(m)["removed"])


def test_an_interrupted_teardown_resumes_where_it_stopped(m: pathlib.Path) -> None:
    fake = _smoked(m)
    fake.fail_on, fake.fail_status = 3, 500
    assert _run(fake, m, "--smoke", "--teardown", *WRITE) == 1
    first = fake.sent("DELETE")
    fake.fail_on = None
    fake.requests.clear()
    assert _run(fake, m, "--smoke", "--teardown", *WRITE) == 0
    assert not set(first[:2]) & set(fake.sent("DELETE"))  # the two done are not re-sent
    assert fake.left_behind() == {}


def test_the_teardown_budget_counts_every_fallback_call(m: pathlib.Path) -> None:
    fake = _smoked(m)
    need = seed.teardown_calls(seed.teardown_steps(_manifest(m)))
    assert need == 6 + 3  # six deletes, and a deactivate for tenant, vendor, property
    assert _run(fake, m, "--smoke", "--teardown", *WRITE, "--max-calls", str(need - 1)) == 2
    assert fake.requests == []
    assert _run(fake, m, "--smoke", "--teardown", *WRITE, "--max-calls", str(need)) == 0


def _manifest_id(m: pathlib.Path, kind: str) -> str:
    (retired,) = m.parent.glob("m.torn-down-*.json")
    return str(_manifest(retired)[f"{kind}_id"])


def test_every_unit_carries_the_property_s_address(m: pathlib.Path) -> None:
    """The second live smoke run was refused: Rentvine does not give units the
    property's address, and requires address, city and stateID on each."""
    fake = FakeRentvine()
    assert _run(fake, m, "--smoke", *WRITE) == 0
    (body,) = fake.posts("/properties")
    for unit in body["units"]:
        for field in ("address", "city", "stateID"):
            assert unit[field] == body["property"][field], (field, unit)


def test_a_template_without_a_city_stops_before_any_write(m: pathlib.Path) -> None:
    no_city = [
        {"property": {k: v for k, v in r["property"].items() if k != "city"}} for r in _PROPERTIES
    ]
    fake = FakeRentvine(properties=no_city)
    assert _run(fake, m, "--smoke", *WRITE) == 1
    assert fake.sent("POST") == []


def test_a_short_units_list_says_fewer_leases_were_made(m: pathlib.Path) -> None:
    """20 units listed on the live run meant 20 leases, not 105, silently."""
    fake = FakeRentvine(units_listed=3)
    assert _run(fake, m, *WRITE, *SMALL) == 0
    manifest = _manifest(m)
    assert len(manifest["leases"]) == 3
    assert any("only 3 leases can be created, not 4" in f for f in manifest["findings"])
