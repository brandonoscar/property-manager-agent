"""Make the Rentvine sandbox big enough to settle the paging questions.

WHY THIS EXISTS
    Phase 8 reconciled every record family against ``demoaccount41``, and the
    open ``@unverified`` list did not move, because the account is too small to
    answer it (``scripts/rentvine_unverified.py`` prints the list):

      * what an unpaged ``GET /properties/{id}/units`` returns for a property
        with more units than any plausible server default;
      * what an unpaged ``GET /leases`` returns on an account with more leases
        than one page;
      * whether Rentvine work orders name their vendor in ``vendorContactID``
        (``db/repositories/operations.py``: taken from the spec, never seen on
        a live row, because the sandbox had no work orders at all).

THREE MODES
    ``--smoke``     ONE of every record this script creates (a small property
                    with 2 units, a vendor, a tenant, a lease, two work orders:
                    one vendor-assigned, one On Hold), then READS EACH ONE BACK
                    by id and prints PASS/FAIL per path. About 20 calls. Its own
                    manifest and its own property, so it never touches the full
                    run's. Run this first: it proves the connection and every
                    body shape, and it settles the vendorContactID question.
    (default)       The full seed: one property with ``--units`` units, ``--leases``
                    leases each with its own tenant, vendors and work orders.
                    Then ``scripts/rentvine_phase8.py --execute`` does the reading.
    ``--teardown``  Deletes what a manifest records, in dependency order (work
                    orders, leases, tenants, vendors, then the property; a
                    property Rentvine will not delete is deactivated instead).
                    Add ``--smoke`` to tear down the smoke run.

    Every mode is a DRY RUN until ``--execute --i-accept-the-write``.

RENTVINE'S TERMS AND LIMITS — WHY THE SCRIPT IS SHAPED THIS WAY
    ``design/rentvine-terms-rules.md`` binds this script. Rentvine documents NO
    rate limit, so the script sets its own, in code:

      * **Documented endpoints only** (the captured spec), rule 5.
      * **Bulk where the API allows it.** The property and all its units are ONE
        call (the spec's "Multi Unit" create), not one call per unit.
      * **A hard call budget** (``--max-calls``, default :data:`DEFAULT_MAX_CALLS`).
        The plan is counted BEFORE the first request and refused if it does not
        fit; the client also refuses the call that would exceed it.
      * **A pace floor of one request per second** (:data:`MIN_INTERVAL_S`).
        ``--interval`` can make it slower, never faster.
      * **No retries, and Retry-After is binding across runs** (rule 1). The
        first 429, 5xx or transport error stops the run. When Rentvine names a
        wait, or sends a 429 without one, the manifest records the earliest
        time the next run may start, and the next run refuses before then.
      * **Nothing emails anyone.** Work orders go out with ``sendEmail`` and
        every ``NEVER_ECHOED`` flag false, leases with ``sendPortalInvitations``
        false, and no tenant or vendor gets an email address.
      * **Nothing it creates may become an eval input** (rules 17-19). The
        records are synthetic, but they live in a Rentvine account.

WHAT IT REFUSES
    * Any account but the one named on the command line (``--confirm-account``),
      so an environment left over from another session cannot seed a real
      customer's account.
    * Writing anything without ``--execute --i-accept-the-write``.
    * Starting before a Retry-After recorded by an earlier run has passed.

CREDENTIALS
    Environment only, never arguments (argv reaches shell history and process
    listings). The same three names ``rentvine_probe.py`` uses:

      $env:RENTVINE_ACCOUNT_CODE = "demoaccount41"
      $s = Read-Host "RENTVINE_API_KEY" -AsSecureString
      $env:RENTVINE_API_KEY      = [Net.NetworkCredential]::new("", $s).Password
      $s = Read-Host "RENTVINE_API_SECRET" -AsSecureString
      $env:RENTVINE_API_SECRET   = [Net.NetworkCredential]::new("", $s).Password

USAGE
    python scripts/rentvine_seed_sandbox.py --confirm-account demoaccount41 --smoke
    python scripts/rentvine_seed_sandbox.py --confirm-account demoaccount41 --smoke \\
        --execute --i-accept-the-write
    python scripts/rentvine_seed_sandbox.py --confirm-account demoaccount41 --smoke \\
        --teardown --execute --i-accept-the-write
    python scripts/rentvine_seed_sandbox.py --confirm-account demoaccount41 \\
        --execute --i-accept-the-write

EXIT CODES
    0  the plan ran (or, dry, was printed) to the end, and every smoke check passed
    1  stopped part-way, or a smoke check FAILED: the manifest says what exists
    2  could not run (missing credentials, wrong account, a plan over budget,
       or Rentvine's Retry-After has not passed yet)
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from helixis.rentvine.client import (  # noqa: E402
    UNREADABLE_RETRY_AFTER_S,
    _retry_after_seconds,
)
from helixis.rentvine.transport import page_params  # noqa: E402
from helixis.rentvine.work_orders import NEVER_ECHOED, STATUSES_PATH  # noqa: E402

_BASE = "https://{account}.rentvine.com/api/manager"

PREFIX = "OCC-SEED"
DEFAULT_MANIFEST = "docs/rentvine/seed-manifest.json"
DEFAULT_SMOKE_MANIFEST = "docs/rentvine/seed-smoke-manifest.json"

#: The pace floor. ``--interval`` may raise it, never lower it.
MIN_INTERVAL_S = 1.0
#: The most requests one run may make, lookups and verification included.
DEFAULT_MAX_CALLS = 300

#: More units than our own page size and any common server default (20/25/50/100).
DEFAULT_UNITS = 120
#: More leases than one page of 100.
DEFAULT_LEASES = 105
DEFAULT_VENDORS = 3
DEFAULT_WORK_ORDERS = 8

#: The smoke run: one of everything, two work orders (one per question).
SMOKE_PLAN_COUNTS = {"units": 2, "leases": 1, "vendors": 1, "work_orders": 2}

#: Three lookups (a property, a lease, the work-order statuses) and two
#: verification reads (the units just created, the lease list).
_FIXED_CALLS = 5
#: The smoke run's read-backs: property, vendor, tenant, lease, the lease's
#: tenants, and each of the two work orders.
_SMOKE_READBACK_CALLS = 7

#: When Rentvine answers 429 without saying how long to wait, the next run
#: waits at least this long. Never zero (rule 1).
_UNNAMED_WAIT_S: float = UNREADABLE_RETRY_AFTER_S

#: T1 ``primary_statuses``: 1 Pending, 2 Open, 3 Closed, 4 On Hold. These are
#: Rentvine's fixed PRIMARY ids. Each account names its own statuses on top
#: (``workOrderStatusID``), so the one sent is looked up per run: On Hold was
#: ``workOrderStatusID`` 5 on demoaccount41 (measured 2026-09-24), primary 4.
_PRIMARY_OPEN = 2
_PRIMARY_ON_HOLD = 4
#: T1 work-order priority 1 is Low (``mapper.WORK_ORDER_PRIORITY``).
_PRIORITY_LOW = "1"


class SeedStopError(Exception):
    """The run must stop here. Never retried.

    ``status`` is the HTTP status when there was one, and ``body`` the (redacted)
    error text Rentvine sent with it. ``wait_s`` is how long Rentvine asked us
    to leave it alone, when it did (or a 429 implied it).
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str = "",
        wait_s: float | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.body = body
        self.wait_s = wait_s


class Creds:
    """The same three environment variables ``rentvine_probe.py`` reads."""

    def __init__(self) -> None:
        self.account = os.environ.get("RENTVINE_ACCOUNT_CODE", "").strip()
        self.key = os.environ.get("RENTVINE_API_KEY", "").strip()
        self.secret = os.environ.get("RENTVINE_API_SECRET", "").strip()

    @property
    def missing(self) -> list[str]:
        names = {
            "RENTVINE_ACCOUNT_CODE": self.account,
            "RENTVINE_API_KEY": self.key,
            "RENTVINE_API_SECRET": self.secret,
        }
        return [n for n, v in names.items() if not v]

    def _token(self) -> str:
        return base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()

    def header(self) -> str:
        return "Basic " + self._token()

    def redact(self, text: str) -> str:
        # The Basic token first: it authenticates on its own, and it contains
        # neither half verbatim, so masking the key and secret would miss it.
        token = self._token() if (self.key or self.secret) else ""
        for secret in (token, self.secret, self.key):
            if secret:
                text = text.replace(secret, "***")
        return text


#: ``(method, url, headers, body) -> (status, text, retry_after_header)``.
#: Injected so tests run offline.
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, str, str | None]]


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, str, str | None]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return (
                resp.status,
                resp.read().decode("utf-8", "replace"),
                resp.headers.get("Retry-After"),
            )
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), exc.headers.get("Retry-After")


def _asked_wait(status: int, retry_after: str | None) -> float | None:
    """How long Rentvine asked us to wait, read the same way the product does.

    The header goes through ``rentvine.client._retry_after_seconds``, so the
    script and the product cannot disagree about what a Retry-After means. A
    429 that names no wait still asks for one: :data:`_UNNAMED_WAIT_S`.
    """
    seconds: float | None = (
        _retry_after_seconds(httpx.Headers({"Retry-After": retry_after}))
        if retry_after is not None
        else None
    )
    if seconds is None and status == 429:
        return _UNNAMED_WAIT_S
    return seconds


class PacedClient:
    """Every request goes through here: paced, counted, never retried."""

    def __init__(
        self,
        creds: Creds,
        transport: Transport,
        *,
        interval: float,
        max_calls: int,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.creds = creds
        self.transport = transport
        self.interval = max(interval, MIN_INTERVAL_S)
        self.max_calls = max_calls
        self.calls = 0
        # Resolved at call time, not bound as defaults at import.
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))
        self._clock = clock or (lambda: time.monotonic())
        self._last: float | None = None

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if self.calls >= self.max_calls:
            raise SeedStopError(f"call budget of {self.max_calls} reached before {method} {path}")
        if self._last is not None:
            wait = self.interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self.calls += 1
        self._last = self._clock()
        url = _BASE.format(account=self.creds.account) + path
        headers = {"Authorization": self.creds.header(), "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, text, retry_after = self.transport(method, url, headers, data)
        except Exception as exc:  # noqa: BLE001 — any transport failure stops the run
            raise SeedStopError(f"{method} {path}: {type(exc).__name__}") from None
        if status >= 400:
            asked = _asked_wait(status, retry_after)
            if status == 429:
                raise SeedStopError(
                    f"{method} {path}: 429 Too Many Requests — stopped, not retried",
                    status=status,
                    wait_s=asked,
                )
            error_text = self.creds.redact(text)[:500]
            raise SeedStopError(
                f"{method} {path}: HTTP {status}: {error_text}",
                status=status,
                body=error_text,
                wait_s=asked,
            )
        try:
            return json.loads(text) if text else None
        except json.JSONDecodeError:
            raise SeedStopError(f"{method} {path}: HTTP {status}, body not JSON") from None


# ── reading what Rentvine sends back ───────────────────────────────


def rows_of(payload: Any) -> list[dict[str, Any]]:
    """A collection's rows, whether the server sends a list or ``{"data": [...]}``."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def record(row: Any, key: str) -> dict[str, Any]:
    """The record inside ``{"property": {...}}``, or the row itself if bare."""
    if isinstance(row, dict):
        inner = row.get(key)
        return inner if isinstance(inner, dict) else row
    return {}


def single_record(payload: Any, key: str) -> dict[str, Any]:
    """A single-record GET's record: ``{key: {...}}``, any one-key wrapper, or bare.

    The single-record envelopes are documented only by example, so each shape
    is accepted; which one arrived does not change what is checked.
    """
    if isinstance(payload, dict):
        inner = payload.get(key)
        if isinstance(inner, dict):
            return inner
        if len(payload) == 1:
            (only,) = payload.values()
            if isinstance(only, dict):
                return only
        return payload
    return {}


def find_id(payload: Any, key: str) -> str | None:
    """``key`` at the top level or one wrapper down: create responses are
    documented only by example, so both shapes are accepted and the id is
    required. ``None`` stops the run rather than guessing."""
    if isinstance(payload, dict):
        if payload.get(key) not in (None, ""):
            return str(payload[key])
        for value in payload.values():
            if isinstance(value, dict) and value.get(key) not in (None, ""):
                return str(value[key])
    return None


def same_date_format(template: str | None, year: int, month: int, day: int) -> str:
    """A date in whatever format the account's own lease uses."""
    if template and re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", template):
        return f"{year:04d}-{month:02d}-{day:02d}"
    return f"{month:02d}/{day:02d}/{year:04d}"


# ── the plan ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Plan:
    units: int
    leases: int
    vendors: int
    work_orders: int
    smoke: bool = False

    @property
    def property_name(self) -> str:
        return f"{PREFIX} {'Smoke Test' if self.smoke else 'Paging Test Apartments'}"

    def calls(self) -> int:
        """Every request a full run makes. One property call carries all units."""
        base = _FIXED_CALLS + 1 + self.vendors + 2 * self.leases + self.work_orders
        return base + (_SMOKE_READBACK_CALLS if self.smoke else 0)

    def work_order_shape(self, n: int) -> tuple[bool, bool]:
        """``(assigned_to_a_vendor, on_hold)`` for work order ``n`` (1-based).

        Odd ones are vendor-assigned (the vendorContactID question). The smoke
        run puts its second one On Hold; the full run every fourth.
        """
        return n % 2 == 1, n % (2 if self.smoke else 4) == 0


#: One page of properties is read to find a template. Rentvine's ``GET
#: /properties`` has no multi-unit filter, so the page must be big enough to
#: include one: one call either way.
_TEMPLATE_PAGE_SIZE = 100


def _is_multi_unit(prop: dict[str, Any]) -> bool:
    # Measured on demoaccount41 (2026-09-24): the flag arrives as 0/1.
    return str(prop.get("isMultiUnit")).strip().lower() in ("1", "true")


def multi_unit_template(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The first existing MULTI-UNIT property, to copy settings from.

    ``propertyTypeID`` is per-account configuration and decides whether a
    property may carry units: Rentvine refused the first live smoke run
    (2026-09-24) because the template, the account's first property, was a
    single-unit type (demoaccount41: type 1 is single-unit on all 9 of its
    properties; types 2 and 6 carry the multi-unit ones). The spec documents
    no endpoint that lists property types, so the id is copied from a
    property already of the right kind, never guessed.
    """
    for row in rows:
        prop = record(row, "property")
        if _is_multi_unit(prop) and prop.get("propertyTypeID") not in (None, ""):
            return prop
    raise SeedStopError(
        f"no multi-unit property among the first {_TEMPLATE_PAGE_SIZE} on this account "
        "to copy propertyTypeID from. Create one multi-unit property in Rentvine by hand, "
        "then re-run."
    )


#: Rentvine's validator requires these on every unit (measured 2026-09-24).
_UNIT_REQUIRED = ("address", "city", "stateID")
#: Copied from the property onto each unit: the required three, plus the
#: rest of the address the spec's `unit` schema declares, when present.
_UNIT_ADDRESS = (*_UNIT_REQUIRED, "postalCode", "countryID")


def property_body(template: dict[str, Any], units: int, name: str) -> dict[str, Any]:
    """The spec's "Multi Unit" create: the property and every unit in one call."""
    copied = (
        "portfolioID",
        "propertyTypeID",
        "managementFeeSettingID",
        "city",
        "stateID",
        "countryID",
        "postalCode",
        "leaseReturnChargeAmount",
    )
    prop = {k: template[k] for k in copied if template.get(k) not in (None, "")}
    prop.update({"name": name, "address": "1 Seed Test Way", "isMultiUnit": True})
    missing = [k for k in _UNIT_REQUIRED if k not in prop]
    if missing:
        raise SeedStopError(
            f"the template property has no {', '.join(missing)}, and every unit needs one. "
            "Fill them in on that property in Rentvine, then re-run."
        )
    # Each unit carries its own address: Rentvine does not inherit the
    # property's (the second live smoke run, 2026-09-24, was refused with
    # "unitContract.unit.address/city/stateID ... is required"). The fields are
    # the spec's own `unit` schema; only the Multi Unit example omits them.
    where = {k: prop[k] for k in _UNIT_ADDRESS if k in prop}
    return {
        "property": prop,
        "units": [
            {"name": f"{PREFIX} {i:03d}", "rent": "1000.00", **where} for i in range(1, units + 1)
        ],
    }


def tenant_body(n: int) -> dict[str, Any]:
    # No email: a seeded tenant must never receive a portal invitation.
    return {"name": f"{PREFIX} Tenant {n:03d}"}


def vendor_body(n: int) -> dict[str, Any]:
    return {"name": f"{PREFIX} Vendor {n}"}


def lease_body(template: dict[str, Any], unit_id: str, tenant_id: str) -> dict[str, Any]:
    start = same_date_format(template.get("startDate"), 2026, 1, 1)
    end = same_date_format(template.get("startDate"), 2026, 12, 31)
    lease: dict[str, Any] = {
        "unitID": unit_id,
        "moveInDate": start,
        "startDate": start,
        "endDate": end,
        "sendStatements": False,
    }
    for k in ("lateFeeSettingID", "leaseReturnChargeAmount", "statementSettingID"):
        if template.get(k) not in (None, ""):
            lease[k] = template[k]
    return {
        "lease": lease,
        "tenants": [{"contactID": tenant_id}],
        "sendPortalInvitations": False,
        "chargeLeaseFee": False,
    }


def work_order_body(
    property_id: str, unit_id: str, status_id: str, vendor_id: str | None, n: int
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "propertyID": property_id,
        "unitID": unit_id,
        "workOrderStatusID": status_id,
        "priorityID": _PRIORITY_LOW,
        "isOwnerApproved": False,
        "isVacant": False,
        "description": f"{PREFIX} work order {n}",
        "isSharedWithTenant": "0",
        "isSharedWithOwner": False,
        # ⚠ Always false: each of these mails somebody.
        "sendEmail": False,
        **{field: False for field in NEVER_ECHOED},
        "sendVendorNotification": False,
    }
    if vendor_id is not None:
        body["vendorContactID"] = vendor_id
    return body


def status_ids(payload: Any) -> dict[int, str]:
    """``{primaryWorkOrderStatusID: workOrderStatusID}`` from the account's list."""
    out: dict[int, str] = {}
    for row in rows_of(payload):
        rec = record(row, "workOrderStatus")
        primary, sid = rec.get("primaryWorkOrderStatusID"), rec.get("workOrderStatusID")
        if primary is not None and sid is not None:
            out.setdefault(int(primary), str(sid))
    return out


# ── the manifest: what exists, so a stopped run resumes ─────────────


def load_manifest(path: pathlib.Path, account: str) -> dict[str, Any]:
    if path.exists():
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if data.get("account") != account:
            owner = data.get("account")
            raise SeedStopError(f"{path} belongs to account {owner!r}, not {account!r}")
        data.setdefault("work_orders", [])
        data.setdefault("removed", [])
        data.setdefault("checks", [])
        return data
    return {
        "account": account,
        "property_id": None,
        "unit_ids": [],
        "vendor_ids": [],
        "leases": [],
        "pending_tenant_id": None,
        "work_order_ids": [],
        #: ``{"id", "vendor_id", "on_hold"}`` per work order, for the read-back.
        "work_orders": [],
        "findings": [],
        "checks": [],
        "removed": [],
        #: Epoch seconds before which no run may start (Retry-After, rule 1).
        "not_before": None,
    }


def save_manifest(path: pathlib.Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


# ── creating ────────────────────────────────────────────────────────


def run(client: PacedClient, plan: Plan, manifest: dict[str, Any], *, execute: bool) -> None:
    """Create what the manifest doesn't have yet. Dry: read the lookups, print."""
    first = urllib.parse.urlencode(page_params(1, _TEMPLATE_PAGE_SIZE))
    prop_rows = rows_of(client.call("GET", f"/properties?{first}"))
    lease_rows = rows_of(client.call("GET", "/leases"))
    statuses = status_ids(client.call("GET", STATUSES_PATH))
    if not prop_rows or not lease_rows:
        raise SeedStopError("need one existing property and one lease to copy settings from")
    prop_tpl, lease_tpl = multi_unit_template(prop_rows), record(lease_rows[0], "lease")
    if _PRIMARY_OPEN not in statuses or _PRIMARY_ON_HOLD not in statuses:
        raise SeedStopError(f"no Open / On Hold work-order status on this account: {statuses}")
    manifest["on_hold_status_id"] = statuses[_PRIMARY_ON_HOLD]

    if not execute:
        _print_dry(plan, prop_tpl, lease_tpl, statuses)
        return

    if manifest["property_id"] is None:
        body = property_body(prop_tpl, plan.units, plan.property_name)
        manifest["property_id"] = find_id(client.call("POST", "/properties", body), "propertyID")
        if manifest["property_id"] is None:
            raise SeedStopError("the property create returned no propertyID")
    pid = manifest["property_id"]

    if not manifest["unit_ids"]:
        # ⚠ THIS READ IS THE FIRST FINDING: an unpaged units call on a property
        # with more units than any plausible server default.
        unit_rows = rows_of(client.call("GET", f"/properties/{pid}/units"))
        manifest["unit_ids"] = [
            str(record(r, "unit")["unitID"]) for r in unit_rows if record(r, "unit").get("unitID")
        ]
        if len(manifest["unit_ids"]) != plan.units:
            manifest["findings"].append(
                f"GET /properties/{pid}/units returned {len(manifest['unit_ids'])} of "
                f"{plan.units} units created in one call — an unpaged list that is short"
            )
    units = manifest["unit_ids"]
    if not units:
        raise SeedStopError("no units came back for the seeded property")

    while len(manifest["vendor_ids"]) < plan.vendors:
        n = len(manifest["vendor_ids"]) + 1
        vid = find_id(client.call("POST", "/vendors", vendor_body(n)), "contactID")
        if vid is None:
            raise SeedStopError("a vendor create returned no contactID")
        manifest["vendor_ids"].append(vid)

    if len(units) < plan.leases:
        # One lease per unit, and only the listed units are known by id. On
        # demoaccount41 (2026-09-24) the list stopped at 20, so 20 leases were
        # made instead of 105 and nothing said so.
        note = (
            f"only {len(units)} leases can be created, not {plan.leases}: "
            f"GET /properties/{pid}/units listed {len(units)} unit ids"
        )
        if note not in manifest["findings"]:
            manifest["findings"].append(note)
    while len(manifest["leases"]) < min(plan.leases, len(units)):
        n = len(manifest["leases"]) + 1
        if manifest["pending_tenant_id"] is None:
            tid = find_id(client.call("POST", "/tenants", tenant_body(n)), "contactID")
            if tid is None:
                raise SeedStopError("a tenant create returned no contactID")
            manifest["pending_tenant_id"] = tid
        unit_id = units[n - 1]
        lease = client.call(
            "POST", "/leases", lease_body(lease_tpl, unit_id, manifest["pending_tenant_id"])
        )
        lid = find_id(lease, "leaseID")
        if lid is None:
            raise SeedStopError("a lease create returned no leaseID")
        manifest["leases"].append(
            {"lease_id": lid, "tenant_id": manifest["pending_tenant_id"], "unit_id": unit_id}
        )
        manifest["pending_tenant_id"] = None

    while len(manifest["work_order_ids"]) < plan.work_orders:
        n = len(manifest["work_order_ids"]) + 1
        assigned, on_hold = plan.work_order_shape(n)
        vendors = manifest["vendor_ids"]
        vendor = vendors[(n // 2) % len(vendors)] if assigned else None
        status = statuses[_PRIMARY_ON_HOLD if on_hold else _PRIMARY_OPEN]
        body = work_order_body(pid, units[(n - 1) % len(units)], status, vendor, n)
        wid = find_id(client.call("POST", "/maintenance/work-orders", body), "workOrderID")
        if wid is None:
            raise SeedStopError("a work-order create returned no workOrderID")
        manifest["work_order_ids"].append(wid)
        manifest["work_orders"].append({"id": wid, "vendor_id": vendor, "on_hold": on_hold})

    # ⚠ THE SECOND FINDING: an unpaged lease list on an account over one page.
    listed = len(rows_of(client.call("GET", "/leases")))
    expected = len(lease_rows) + len(manifest["leases"])
    manifest["leases_listed"] = listed
    if listed < expected:
        manifest["findings"].append(
            f"GET /leases returned {listed} rows; the account has at least {expected}"
        )


# ── reading every created record back (the smoke run) ──────────────


def _check(manifest: dict[str, Any], path: str, ok: bool, detail: str) -> bool:
    manifest["checks"].append({"path": path, "ok": ok, "detail": detail})
    return ok


def verify_smoke(client: PacedClient, plan: Plan, manifest: dict[str, Any]) -> bool:
    """Read every record the smoke run created back by id. A 2xx is not the
    write (gotcha 70): each create counts only once the record reads back
    with what was sent. Returns True when every check passed."""
    manifest["checks"] = []
    ok = True
    pid = manifest["property_id"]
    prop = single_record(client.call("GET", f"/properties/{pid}"), "property")
    ok &= _check(
        manifest,
        "POST /properties",
        prop.get("name") == plan.property_name,
        f"read back name {prop.get('name')!r}",
    )
    ok &= _check(
        manifest,
        "units (Multi Unit create)",
        len(manifest["unit_ids"]) == plan.units,
        f"{len(manifest['unit_ids'])} of {plan.units} units listed",
    )

    # Vendors and tenants are both contacts: the spec's examples wrap each
    # single-record GET in ``{"contact": ...}`` (a tenant adds ``leases`` and
    # portal keys beside it, so the one-key fallback would not find it).
    vid = manifest["vendor_ids"][0]
    vendor = single_record(client.call("GET", f"/vendors/{vid}"), "contact")
    ok &= _check(
        manifest,
        "POST /vendors",
        vendor.get("name") == vendor_body(1)["name"],
        f"read back name {vendor.get('name')!r}",
    )

    lease = manifest["leases"][0]
    tenant = single_record(client.call("GET", f"/tenants/{lease['tenant_id']}"), "contact")
    ok &= _check(
        manifest,
        "POST /tenants",
        tenant.get("name") == tenant_body(1)["name"],
        f"read back name {tenant.get('name')!r}",
    )
    got = single_record(client.call("GET", f"/leases/{lease['lease_id']}"), "lease")
    ok &= _check(
        manifest,
        "POST /leases",
        str(got.get("unitID")) == str(lease["unit_id"]),
        f"read back unitID {got.get('unitID')!r}",
    )
    linked = [
        str(record(r, "contact").get("contactID") or r.get("contactID"))
        for r in rows_of(client.call("GET", f"/leases/{lease['lease_id']}/tenants"))
    ]
    ok &= _check(
        manifest,
        "lease → tenant link",
        str(lease["tenant_id"]) in linked,
        f"lease tenants {linked}",
    )

    for wo in manifest["work_orders"]:
        rec = single_record(client.call("GET", f"/maintenance/work-orders/{wo['id']}"), "workOrder")
        if wo["vendor_id"] is not None:
            # ⚠ THE THIRD QUESTION, on a live row: does a work order name its
            # vendor in vendorContactID? (db/repositories/operations.py)
            seen = rec.get("vendorContactID")
            ok &= _check(
                manifest,
                "work order → vendorContactID",
                str(seen) == str(wo["vendor_id"]),
                f"vendorContactID read back {seen!r}, sent {wo['vendor_id']!r}",
            )
        if wo["on_hold"]:
            sid = rec.get("workOrderStatusID")
            primary = rec.get("primaryWorkOrderStatusID")
            ok &= _check(
                manifest,
                "work order On Hold",
                str(sid) == str(manifest.get("on_hold_status_id")) or str(primary) == "4",
                f"workOrderStatusID {sid!r}, primaryWorkOrderStatusID {primary!r}",
            )
    return bool(ok)


# ── undoing ─────────────────────────────────────────────────────────


def teardown_steps(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """``(kind, id)`` for everything a manifest records, in dependency order:
    work orders and leases first, then tenants and vendors, the property last."""
    steps: list[tuple[str, str]] = [("work_order", w) for w in manifest["work_order_ids"]]
    steps += [("lease", ls["lease_id"]) for ls in manifest["leases"]]
    tenants = [ls["tenant_id"] for ls in manifest["leases"]]
    if manifest.get("pending_tenant_id"):
        tenants.append(manifest["pending_tenant_id"])
    steps += [("tenant", t) for t in tenants]
    steps += [("vendor", v) for v in manifest["vendor_ids"]]
    if manifest.get("property_id"):
        steps.append(("property", manifest["property_id"]))
    done = set(manifest.get("removed", []))
    return [(kind, i) for kind, i in steps if f"{kind}:{i}" not in done]


_DELETE_PATH = {
    "work_order": "/maintenance/work-orders/{id}",
    "lease": "/leases/{id}",
    "tenant": "/tenants/{id}",
    "vendor": "/vendors/{id}",
    "property": "/properties/{id}",
}

#: The spec's documented fallback when Rentvine refuses a delete. Work orders
#: and leases have none: a refusal there stops the teardown, because what
#: follows (the tenant, the property) depends on it.
_DEACTIVATE_PATH = {
    "tenant": "/tenants/{id}/deactivate",
    "vendor": "/vendors/{id}/deactivate",
    "property": "/properties/{id}/deactivate",
}


def teardown_calls(steps: list[tuple[str, str]]) -> int:
    """The most requests a teardown of ``steps`` can make: one delete each,
    plus a deactivate for each record that has the fallback."""
    return len(steps) + sum(1 for kind, _ in steps if kind in _DEACTIVATE_PATH)


def _already_gone(exc: SeedStopError) -> bool:
    """Rentvine says "not found" as a 400 (``{"error": "Tenant 2793 not
    found"}``) on tenants, vendors, leases and properties, and a 404 only on
    work orders (the spec's own examples). Either means an earlier teardown
    got there first."""
    return exc.status == 404 or (exc.status == 400 and "not found" in exc.body.lower())


def teardown(client: PacedClient, manifest: dict[str, Any], *, execute: bool) -> None:
    steps = teardown_steps(manifest)
    if not execute:
        print(
            f"  DRY RUN — would remove {len(steps)} record(s), "
            f"at most {teardown_calls(steps)} calls:"
        )
        for kind, i in steps:
            print(f"    DELETE {_DELETE_PATH[kind].format(id=i)}")
        print("  Re-run with --execute --i-accept-the-write.")
        return
    for kind, i in steps:
        try:
            client.call("DELETE", _DELETE_PATH[kind].format(id=i))
            how = "deleted"
        except SeedStopError as exc:
            if _already_gone(exc):
                how = "already gone"
            elif kind in _DEACTIVATE_PATH and exc.status == 400:
                # Rentvine refused the delete (a property with transactions, a
                # tenant still on a lease). Deactivate instead: one documented
                # call that leaves the record inert. Nothing else is tried.
                client.call("POST", _DEACTIVATE_PATH[kind].format(id=i), {})
                how = f"deactivated (delete refused: {exc.body[:120]})"
            else:
                raise
        manifest["removed"].append(f"{kind}:{i}")
        print(f"  {kind} {i}: {how}")


# ── output ──────────────────────────────────────────────────────────


def _print_dry(
    plan: Plan, prop_tpl: dict[str, Any], lease_tpl: dict[str, Any], statuses: dict[int, str]
) -> None:
    print("  DRY RUN — three lookups read, nothing written.")
    print(
        f"  settings copied from property {prop_tpl.get('propertyID')!r} "
        f"(multi-unit, propertyTypeID {prop_tpl.get('propertyTypeID')!r})"
    )
    print(f"  would make {plan.calls()} calls at most, at least {MIN_INTERVAL_S:.0f}s apart")
    body = property_body(prop_tpl, plan.units, plan.property_name)
    print(f"  POST /properties (1 call, {plan.units} units):")
    print(f"    property: {json.dumps(body['property'])}")
    print(f"    units[0]: {json.dumps(body['units'][0])}")
    print(f"  POST /vendors x{plan.vendors}: {json.dumps(vendor_body(1))}")
    print(f"  POST /tenants x{plan.leases}: {json.dumps(tenant_body(1))}")
    lease = lease_body(lease_tpl, "<unitID>", "<contactID>")
    print(f"  POST /leases x{plan.leases}: {json.dumps(lease)}")
    wo = work_order_body("<propertyID>", "<unitID>", statuses[_PRIMARY_OPEN], "<vendorID>", 1)
    print(f"  POST /maintenance/work-orders x{plan.work_orders}: {json.dumps(wo)}")
    if plan.smoke:
        print(f"  then {_SMOKE_READBACK_CALLS} reads, each created record back by id.")
    print()
    print("  Re-run with --execute --i-accept-the-write.")


def _utc(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).strftime("%H:%M:%S UTC")


def main(argv: list[str] | None = None, transport: Transport = urllib_transport) -> int:
    parser = argparse.ArgumentParser(
        description="Seed the Rentvine sandbox for the Phase 8 paging questions."
    )
    parser.add_argument("--confirm-account", required=True, help="must equal RENTVINE_ACCOUNT_CODE")
    parser.add_argument("--execute", action="store_true", help="write (default: dry run)")
    parser.add_argument("--i-accept-the-write", action="store_true")
    parser.add_argument("--smoke", action="store_true", help="one of each record, read back")
    parser.add_argument("--teardown", action="store_true", help="remove what a manifest records")
    parser.add_argument("--units", type=int, default=DEFAULT_UNITS)
    parser.add_argument("--leases", type=int, default=DEFAULT_LEASES)
    parser.add_argument("--vendors", type=int, default=DEFAULT_VENDORS)
    parser.add_argument("--work-orders", type=int, default=DEFAULT_WORK_ORDERS)
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument(
        "--interval", type=float, default=MIN_INTERVAL_S, help="seconds between calls (min 1)"
    )
    parser.add_argument("--manifest", default=None)
    args = parser.parse_args(argv)

    creds = Creds()
    if creds.missing:
        print(f"missing environment variables: {', '.join(creds.missing)}", file=sys.stderr)
        return 2
    if creds.account != args.confirm_account.strip():
        print(
            f"RENTVINE_ACCOUNT_CODE is {creds.account!r}, not {args.confirm_account!r}. Refusing.",
            file=sys.stderr,
        )
        return 2
    if args.execute and not args.i_accept_the_write:
        print("--execute needs --i-accept-the-write too.", file=sys.stderr)
        return 2

    if args.smoke:
        plan = Plan(smoke=True, **SMOKE_PLAN_COUNTS)
    else:
        plan = Plan(args.units, args.leases, args.vendors, args.work_orders)
    if min(plan.units, plan.leases, plan.vendors, plan.work_orders) < 1:
        print("every count must be at least 1.", file=sys.stderr)
        return 2

    path = pathlib.Path(
        args.manifest or (DEFAULT_SMOKE_MANIFEST if args.smoke else DEFAULT_MANIFEST)
    )
    if args.teardown and not path.exists():
        hint = "" if args.smoke else " (the smoke run's is torn down with --smoke --teardown)"
        print(f"no manifest at {path}, so nothing is recorded to tear down{hint}.", file=sys.stderr)
        return 2
    try:
        manifest = load_manifest(path, creds.account)
    except SeedStopError as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        return 2

    # ⚠ Rentvine's Retry-After binds the NEXT run too (terms rule 1).
    not_before = manifest.get("not_before")
    if not_before and time.time() < not_before:
        print(
            f"Rentvine asked us to wait: no run before {_utc(not_before)} "
            f"({int(not_before - time.time()) + 1}s from now). Nothing was sent.",
            file=sys.stderr,
        )
        return 2

    budget = teardown_calls(teardown_steps(manifest)) if args.teardown else plan.calls()
    if budget > args.max_calls:
        print(
            f"the plan needs up to {budget} calls and the budget is {args.max_calls}. "
            "Lower the counts or raise --max-calls deliberately.",
            file=sys.stderr,
        )
        return 2

    client = PacedClient(creds, transport, interval=args.interval, max_calls=args.max_calls)
    passed = True
    try:
        if args.teardown:
            teardown(client, manifest, execute=args.execute)
        else:
            run(client, plan, manifest, execute=args.execute)
            if plan.smoke and args.execute:
                passed = verify_smoke(client, plan, manifest)
    except SeedStopError as exc:
        if exc.wait_s:
            manifest["not_before"] = time.time() + exc.wait_s
        if args.execute or exc.wait_s:
            save_manifest(path, manifest)
        print(f"STOPPED after {client.calls} call(s): {exc}", file=sys.stderr)
        if exc.wait_s:
            print(
                f"  Rentvine asked us to wait: the next run is refused before "
                f"{_utc(manifest['not_before'])}.",
                file=sys.stderr,
            )
        if args.execute:
            print(f"  what exists is in {path}; the next run resumes from it.", file=sys.stderr)
        return 1

    if not args.execute:
        return 0
    manifest["not_before"] = None
    if args.teardown:
        # A finished teardown retires its manifest, so the next seed starts clean
        # instead of "resuming" onto records that no longer exist.
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        retired = path.with_name(f"{path.stem}.torn-down-{stamp}{path.suffix}")
        save_manifest(retired, manifest)
        path.unlink(missing_ok=True)
        print(f"torn down in {client.calls} call(s). Record kept at {retired}")
        return 0
    save_manifest(path, manifest)
    print(f"done in {client.calls} call(s). Manifest: {path}")
    for check in manifest.get("checks", []):
        print(f"  {'PASS' if check['ok'] else 'FAIL'}  {check['path']}: {check['detail']}")
    for finding in manifest["findings"]:
        print(f"  FINDING: {finding}")
    if not passed:
        print("  A smoke check FAILED: fix that before the full run.", file=sys.stderr)
        return 1
    print(
        "  Next: tear this down with --smoke --teardown, then the full run."
        if plan.smoke
        else "  Next: python scripts/rentvine_phase8.py --execute"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
