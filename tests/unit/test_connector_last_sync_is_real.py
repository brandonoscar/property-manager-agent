"""The Buildium card claimed a sync that had never happened.

``_connector_payload`` stamps every CONNECTED card with ``lastSyncAt =
now()`` — a placeholder, and the comment beside it says so, because no
per-company OAuth sync record exists to fill in yet. The Buildium branch then
*overwrote* it when a real ``helixis_mirror_sync_state`` stamp existed. So on
the one connector where the true value is available, absence of a real sync
rendered as **"last sync: just now"**.

That was harmless while nothing read the field — ``grep -rn lastSyncAt
frontend/src`` found zero consumers, so a fabricated timestamp and a real one
were equally invisible. It stopped being harmless the moment two screens
needed it to tell *"your Buildium account has no rental properties"* apart
from *"we have not looked yet"*: the getting-started checklist's "Sync your
portfolio" tick, and the properties grid's empty state. A placeholder answers
"synced" for everybody, which is the more expensive direction — it tells a
customer who has never synced that their portfolio is genuinely empty.

Same class as this repo keeps finding: a fact we happened to have standing in
for the fact we needed (gotchas 67/70, and ``test_buildium_connection_state``'s
own header). The fix REPLACES rather than overwrites — including with absence.
"""

from __future__ import annotations

import ast
import inspect
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.api.routes import connectors as route
from helixis.api.routes.connectors import _connector_payload

COMPANY = "5ca1ab1e-0000-4000-8000-000000000004"
_SYNCED_AT = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _request() -> MagicMock:
    req = MagicMock()
    req.app.state.composio_client = None
    return req


async def _rows(monkeypatch: pytest.MonkeyPatch, *, freshness: dict[str, datetime]) -> list[dict]:
    """Run the real route with Buildium credentials on file and a given
    per-table freshness map (empty = never synced)."""
    monkeypatch.setattr(route.connectors_limiter, "check_by_user", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "helixis.buildium.credentials.fetch_buildium_row",
        AsyncMock(return_value={"company_id": COMPANY, "last_tested_at": _SYNCED_AT}),
    )
    monkeypatch.setattr("helixis.db.engine.get_pool", lambda: MagicMock())
    monkeypatch.setattr(
        "helixis.db.repositories.mirror_sync_state.get_freshness",
        AsyncMock(return_value=freshness),
    )
    user = MagicMock()
    user.company_ids = [COMPANY]
    return await route.list_connectors(_request(), user=user)  # type: ignore[arg-type]


def _buildium(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return next(r for r in rows if r["id"] == "buildium")


# ── the defect ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_buildium_account_that_never_synced_reports_no_last_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE DEFECT. Credentials on file, zero rows in `helixis_mirror_sync_state`
    — nothing has ever synced, and the card said it had, moments ago."""
    rows = await _rows(monkeypatch, freshness={})

    assert "lastSyncAt" not in _buildium(rows), (
        "the Buildium card is reporting a sync timestamp for an account that "
        "has never synced — a page-load placeholder wearing the name of a fact"
    )


@pytest.mark.asyncio
async def test_a_real_sync_stamp_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction (gotcha 38), so the fix cannot be satisfied by
    dropping the field always: a company that HAS synced must still say so, or
    the checklist can never tick and the grid can never explain itself."""
    rows = await _rows(monkeypatch, freshness={"properties": _SYNCED_AT})

    assert _buildium(rows)["lastSyncAt"] == int(_SYNCED_AT.timestamp() * 1000)


@pytest.mark.asyncio
async def test_the_placeholder_survives_for_every_other_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ Scoped to Buildium ON PURPOSE. The other cards have no per-company
    sync record to replace the placeholder with, so removing it there would
    trade a cosmetic fiction for a blank field and settle nothing. This test
    is what stops a tidy-up from widening the fix into a regression.
    """
    monkeypatch.setattr(route.connectors_limiter, "check_by_user", AsyncMock(return_value=None))
    payload = _connector_payload(
        {
            "id": "gmail",
            "name": "Gmail",
            "category": "communication",
            "description": "",
            "authMethod": "oauth",
        },
        connected=True,
        needs_reauth=False,
    )
    assert "lastSyncAt" in payload


def test_the_buildium_branch_replaces_rather_than_overwrites() -> None:
    """A behavioural test cannot see the shape of the mistake being avoided.

    ``payload["lastSyncAt"] = real`` only when a real value exists LOOKS
    correct and leaves the placeholder standing in the one case that matters.
    The branch has to clear the key first, so this walks for the removal —
    scoped to the route function, since the comment beside it now quotes the
    old form in order to explain it (gotchas 40/59).
    """
    tree = ast.parse(inspect.getsource(route))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "list_connectors"
    )
    pops = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "pop"
        and any(isinstance(a, ast.Constant) and a.value == "lastSyncAt" for a in n.args)
    ]
    assert pops, (
        "list_connectors no longer clears the placeholder before writing the "
        "real Buildium stamp, so an account that has never synced reports one"
    )


def test_the_frontend_reads_the_pms_rows_specifically() -> None:
    """BOTH SIDES (gotcha 28) across a language boundary. The backend narrows
    this field for the PMS rows ALONE (Buildium, and Rentvine since
    2026-10-03); a client helper that asked "does any connector have a
    lastSyncAt" would answer yes for every account, since every other
    connected card still carries the placeholder.
    """
    src = (Path(__file__).resolve().parents[2] / "frontend/src/services/connectors.ts").read_text(
        encoding="utf-8"
    )
    helper = re.search(r"export function pmsLastSyncAt\(.*?\n\}", src, re.S)
    assert helper, "connectors.ts no longer exports pmsLastSyncAt"
    assert "isPmsConnector(r.id)" in helper.group(0), (
        "pmsLastSyncAt stopped selecting the PMS rows — every other "
        "connected card carries a page-load placeholder, so a generic lookup "
        "reports 'synced' for an account that never has"
    )
    pms_ids = {
        spec["id"]
        for spec in route.SUPPORTED_CONNECTORS
        if spec["category"] == "property_management"
    }
    declared = re.search(r"PMS_CONNECTOR_IDS: readonly string\[\] = \[([^\]]*)\]", src)
    assert declared and set(re.findall(r"'(\w+)'", declared.group(1))) == pms_ids
