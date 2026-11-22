"""The founder's read-only access to his own Rent Manager account.

Shared by ``rentmanager_probe.py`` and ``rentmanager_phase8.py``: the
credentials (environment only), a client that cannot write, and an observer
that records the SHAPE of every answer and never a value.

READ-ONLY BY CONSTRUCTION, NOT BY CARE
    The client is the connector's own (``helixis.rentmanager.client``): its
    config, its session login, its rate holds and circuit breaker, its
    scrubbed errors. Two things are replaced, and each alone stops a write:

    * the operation index holds GETs only, plus the one login POST the
      session token needs (``read_operations``); and
    * ``write_methods`` is EMPTY, so ``send_once`` refuses every verb before
      anything is sent, even after the writes slice adds POST to the
      connector's own config.

    ``tests/unit/test_rentmanager_live_scripts.py`` walks these files for any
    write verb, any client built with one, and any SQL that is not a SELECT.

CREDENTIALS
    Environment only, never arguments: argv reaches shell history and process
    listings (CLAUDE.md rule 3). Nothing here prints a username, a password or
    a token, and a check says only set or unset. The lines to type in
    PowerShell 5.1 are :data:`POWERSHELL_LINES`; the password and the partner
    token are read with ``Read-Host -AsSecureString``, so they are neither
    echoed nor saved in PowerShell's history (a typed ``$env:X = "..."`` line
    is saved there).

WHAT IS RECORDED
    :class:`Observer` keeps, per response: the method, the path TEMPLATE (a
    record id becomes ``{id}``), the status, whether the body began with a
    UTF-8 BOM, the body's JSON type and its key NAMES, the header names, and
    the values of the paging and quota headers only (counts and quota
    numbers). Never a body value, a query value, a token or a credential.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:  # operator tooling: runnable from a clone
    sys.path.insert(0, str(REPO / "src"))

import httpx  # noqa: E402

from helixis.observability.pms_calls import template_of  # noqa: E402
from helixis.pms.auth import header_value, host_label  # noqa: E402
from helixis.pms.http import ClientConfig, PmsHttpClient, operation_key  # noqa: E402
from helixis.rentmanager import client as rm_client  # noqa: E402
from helixis.rentmanager.credentials import RentManagerCredentials  # noqa: E402
from helixis.rentmanager.resources import RESOURCES  # noqa: E402

CORP_VAR = "RENTMANAGER_CORP_ID"
LOCATION_VAR = "RENTMANAGER_LOCATION_ID"
USERNAME_VAR = "RENTMANAGER_USERNAME"
PASSWORD_VAR = "RENTMANAGER_PASSWORD"
PARTNER_VAR = "RENTMANAGER_PARTNER_TOKEN"

#: Required for every live run.
REQUIRED_VARS = (CORP_VAR, LOCATION_VAR, USERNAME_VAR, PASSWORD_VAR)
#: Only if Rent Manager issued a partner token (design R1).
OPTIONAL_VARS = (PARTNER_VAR,)

#: What to type in PowerShell 5.1, in order. The secrets are prompted for
#: (``-AsSecureString``: not echoed), never typed into a command line, which
#: PowerShell would save in its history file.
POWERSHELL_LINES = (
    '$env:PYTHONUTF8 = "1"',
    '$env:RENTMANAGER_CORP_ID     = "<the part before .api.rentmanager.com>"',
    '$env:RENTMANAGER_LOCATION_ID = "<the location number, such as 1>"',
    '$env:RENTMANAGER_USERNAME    = Read-Host "Rent Manager API username"',
    '$s = Read-Host "Rent Manager API password" -AsSecureString',
    '$env:RENTMANAGER_PASSWORD    = [Net.NetworkCredential]::new("", $s).Password',
    "## Only if Rent Manager issued a partner token:",
    '$s = Read-Host "Rent Manager partner token" -AsSecureString',
    '$env:RENTMANAGER_PARTNER_TOKEN = [Net.NetworkCredential]::new("", $s).Password',
)

#: When the run is over: the secrets leave this window's environment.
POWERSHELL_CLEANUP = (
    "Remove-Item Env:RENTMANAGER_USERNAME, Env:RENTMANAGER_PASSWORD, "
    "Env:RENTMANAGER_PARTNER_TOKEN -ErrorAction SilentlyContinue; "
    "Remove-Variable s -ErrorAction SilentlyContinue"
)

#: The company the credentials stand in for. A founder's run belongs to no
#: Occupella company: the session token is keyed by it in this process only.
_RUN_LABEL = "founder-run"

#: The largest location id a Postgres ``integer`` holds (migration 0107).
_MAX_LOCATION_ID = 2_147_483_647

#: Every query parameter a probe or Phase 8 read may carry: wider than the
#: sync's, because the filter canary and the UpdateDate filter are what the
#: probe exists to try. GET only.
READ_PARAMS = frozenset({"fields", "embeds", "pageSize", "pageNumber", "filters"})


def env_is_set(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def check_lines(required: Iterable[str] = (), optional: Iterable[str] = ()) -> list[str]:
    """``NAME  set`` / ``NAME  UNSET`` for every variable, the shared ones and
    a script's own (``required``, ``optional``). Never a value."""
    optional_names = [*OPTIONAL_VARS, *optional]
    names = [*REQUIRED_VARS, *required, *optional_names]
    width = max(len(n) for n in names) + 2
    lines = []
    for name in names:
        state = "set" if env_is_set(name) else "UNSET"
        label = " (optional)" if name in optional_names else ""
        lines.append(f"  {name:<{width}}{state}{label}")
    return lines


def parse_location(text: str) -> int | None:
    """A location id as typed: a whole number from 1 to 2^31-1, else None."""
    value = text.strip()
    if not (value.isascii() and value.isdigit()):
        return None
    number = int(value)
    return number if 0 < number <= _MAX_LOCATION_ID else None


def credentials_from_env() -> RentManagerCredentials:
    """The credentials the environment names, or ``ValueError`` saying which
    variable is missing or not usable. The message never carries a value."""
    missing = [name for name in REQUIRED_VARS if not env_is_set(name)]
    if missing:
        raise ValueError("missing " + ", ".join(missing))
    try:
        corp = host_label(os.environ[CORP_VAR], what=CORP_VAR)
    except ValueError:
        raise ValueError(
            f"{CORP_VAR} must be the part before .api.rentmanager.com: letters, digits "
            "and hyphens only"
        ) from None
    location = parse_location(os.environ[LOCATION_VAR])
    if location is None:
        raise ValueError(f"{LOCATION_VAR} must be a whole number above 0, such as 1")
    partner: str | None = None
    if env_is_set(PARTNER_VAR):
        try:
            partner = header_value(os.environ[PARTNER_VAR], what=PARTNER_VAR)
        except ValueError:
            raise ValueError(
                f"{PARTNER_VAR} contains a line break or a control character"
            ) from None
    return RentManagerCredentials(
        company_id=_RUN_LABEL,
        corp_id=corp,
        location_id=location,
        username=os.environ[USERNAME_VAR].strip(),
        # Not stripped: a space can be part of a password.
        password=os.environ[PASSWORD_VAR],
        partner_token=partner,
        version=_RUN_LABEL,
    )


def at_location(creds: RentManagerCredentials, location_id: int) -> RentManagerCredentials:
    """The same login, for another location (the probe's RM-10 check)."""
    return dataclasses.replace(creds, location_id=location_id)


def read_operations(paths: Iterable[str] = ()) -> Mapping[str, frozenset[str]]:
    """GET every collection the sync reads, the current location and
    ``paths``; plus the ONE login POST the session token needs."""
    ops: dict[str, frozenset[str]] = {
        operation_key("GET", r.path): READ_PARAMS for r in RESOURCES.values()
    }
    ops[operation_key("GET", rm_client.CURRENT_LOCATION_PATH)] = frozenset()
    for path in paths:
        ops[operation_key("GET", path)] = READ_PARAMS
    # The session's own login (``rm_client.session_auth``), never a write: it
    # is sent by ``PmsHttpClient.login`` only, which ``send_once`` is not.
    ops[operation_key("POST", rm_client.LOGIN_PATH)] = frozenset()
    return MappingProxyType(ops)


def read_only_config(
    creds: RentManagerCredentials, operations: Mapping[str, frozenset[str]]
) -> ClientConfig:
    """The connector's own config with a GET-only index and NO write verb."""
    return dataclasses.replace(
        rm_client.client_config(creds),
        operations=operations,
        write_methods=frozenset(),
    )


# ── what an answer looked like, never what it said ────────────────────────

#: Headers whose values are recorded: counts and quota numbers.
_NUMERIC_HEADERS = (
    "x-total-results",
    "x-ratelimit",
    "x-rateremaining",
    "x-ratetimeleft",
    "retry-after",
)
_REL = re.compile(r'rel="?([A-Za-z]+)"?')
_BOM = b"\xef\xbb\xbf"
_LOGIN_PATH = "/" + rm_client.LOGIN_PATH.lstrip("/")


def shape_of(raw: bytes) -> dict[str, Any]:
    """A body's JSON type and key names. Never a value."""
    if not raw.strip():
        return {"type": "empty"}
    try:
        body = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return {"type": "not json"}
    return shape_of_json(body)


def shape_of_json(body: Any) -> dict[str, Any]:
    if isinstance(body, list):
        keys: set[str] = set()
        for item in body:
            if isinstance(item, dict):
                keys.update(item)
        return {"type": "list", "items": len(body), "item_keys": sorted(keys)}
    if isinstance(body, dict):
        return {"type": "object", "keys": sorted(body)}
    if isinstance(body, bool):
        return {"type": "bool"}
    if isinstance(body, (int, float)):
        return {"type": "number"}
    if isinstance(body, str):
        return {"type": "string"}
    return {"type": "null"}


@dataclass
class Observer:
    """Every response's shape, in order (an httpx response hook)."""

    #: How many responses are kept whole; the rest only counted.
    keep: int = 60
    responses: list[dict[str, Any]] = field(default_factory=list)
    #: Every login answer, however many came before it: status, BOM and the
    #: body's JSON type (never the token).
    login_answers: list[dict[str, Any]] = field(default_factory=list)
    seen: int = 0
    with_bom: int = 0

    async def record(self, response: httpx.Response) -> None:
        await response.aread()
        raw = response.content
        self.seen += 1
        bom = raw.startswith(_BOM)
        self.with_bom += bom
        if response.request.method == "POST" and response.request.url.path == _LOGIN_PATH:
            self.login_answers.append(
                {"status": response.status_code, "bom": bom, "body": shape_of(raw)}
            )
        if len(self.responses) >= self.keep:
            return
        headers = {k.lower(): v for k, v in response.headers.items()}
        numeric = {
            name: headers[name].strip()
            for name in _NUMERIC_HEADERS
            if name in headers and re.fullmatch(r"-?\d+(\.\d+)?", headers[name].strip())
        }
        self.responses.append(
            {
                "method": response.request.method,
                "endpoint": template_of(response.request.url.path),
                "status": response.status_code,
                "bom": bom,
                "body": shape_of(raw),
                "content_type": headers.get("content-type"),
                "header_names": sorted(headers),
                "numeric_headers": numeric,
                "link_rels": sorted(set(_REL.findall(headers.get("link", "")))),
            }
        )


@asynccontextmanager
async def read_only_client(
    creds: RentManagerCredentials,
    operations: Mapping[str, frozenset[str]],
    observer: Observer,
) -> AsyncIterator[PmsHttpClient]:
    """A client on the founder's account that can only read.

    The HTTP layer under it carries the observer's response hook and the
    connector's own headers (``Accept``, and the partner token when one is
    set). Every request still goes through ``PmsHttpClient``: the operation
    index, the waits Rent Manager asks for, and the breaker.
    """
    config = read_only_config(creds, operations)
    transport = httpx.AsyncClient(
        timeout=config.timeout_seconds,
        headers=dict(config.extra_headers),
        event_hooks={"response": [observer.record]},
    )
    try:
        yield PmsHttpClient(config, rm_client.session_auth(creds), client=transport)
    finally:
        await transport.aclose()


def write_report(path: str, report: Mapping[str, Any]) -> None:
    """Write the report, creating its folder first: a missing folder on the
    LAST line of a live run would lose every answer (gotcha 49). UTF-8 always:
    the Windows console default cannot encode the report."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")


def utf8_console() -> None:
    """Windows console: the output prints ⚠ (gotcha 49)."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — cosmetic
        pass


__all__ = [
    "CORP_VAR",
    "LOCATION_VAR",
    "OPTIONAL_VARS",
    "PARTNER_VAR",
    "PASSWORD_VAR",
    "POWERSHELL_CLEANUP",
    "POWERSHELL_LINES",
    "READ_PARAMS",
    "REQUIRED_VARS",
    "USERNAME_VAR",
    "Observer",
    "at_location",
    "check_lines",
    "credentials_from_env",
    "env_is_set",
    "parse_location",
    "read_only_client",
    "read_only_config",
    "read_operations",
    "shape_of",
    "shape_of_json",
    "utf8_console",
    "write_report",
]
