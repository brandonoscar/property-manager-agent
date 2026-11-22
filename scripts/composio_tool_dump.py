"""Dump every Composio tool slug per toolkit, so the read/write gate can be validated.

WHY THIS EXISTS
---------------
``connectors/tool_gate.py`` decides which Composio tools are READS (run freely)
and which are WRITES (confirm-gated before they fire). It decides by pattern —
verb segments in the slug — and its own TODO records that the patterns were
derived from Composio's *published* slugs, never from a live ``tools.get``:

    TODO(composio-live-dump): the read set above is derived from Composio's
    published slugs, NOT a live ``tools.get`` dump (no HELIXIS_COMPOSIO__API_KEY
    in the build sandbox) ... the read side still needs validation against a
    live per-toolkit dump for GMAIL, GOOGLECALENDAR, GOOGLEDRIVE — and now
    GOOGLEDOCS + GOOGLESHEETS, added without a dump of their own

The load-bearing check in that list is #2: *assert NO slug carrying a write
verb lands in the read set — the fail-closed invariant, would be a real leak
if violated*. Over-gating a read is noise. Under-gating a WRITE means a Google
action — sending mail, deleting a file, moving an event — firing without the
confirmation card the user is entitled to.

That check cannot be written until the real slug list exists. This produces it.

WHY IT IS A SCRIPT AND NOT A TEST
---------------------------------
It needs ``HELIXIS_COMPOSIO__API_KEY`` and network egress to Composio, and CI
has neither. So: a human runs this once, commits the fixture, and the
validation test then runs offline forever against real data. Same shape as
every other live seam here — the probe is manual, the assertion is permanent.

⚠ Do NOT rotate the Composio API key to run this (gotcha 1). Deleting or
rotating a key can orphan every user's existing OAuth connection, and there is
no bulk re-link — each user has to re-authorize by hand. Use the key already
in Render.

USAGE
-----
    export HELIXIS_COMPOSIO__API_KEY=<the key already in Render>
    python scripts/composio_tool_dump.py                  # writes the fixture
    python scripts/composio_tool_dump.py --print          # stdout only

Then commit ``tests/fixtures/composio_tool_slugs.json`` and the gate
validation test will start asserting against it.

The dump records slugs and one-line descriptions ONLY — no arguments, no user
data, no connection ids. It is a list of capability names, safe to commit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import sys
from typing import Any

logger = logging.getLogger("composio_tool_dump")

#: Every toolkit a specialist can be given. Keep in step with the
#: Per-toolkit page size. Well above the largest toolkit (OUTLOOK, 305);
#: the SDK's own default of 200 truncated it silently. ``_fetch_toolkit``
#: cross-checks the returned count against the API's total_items regardless,
#: so raising this is a convenience and not the guard.
_PAGE_LIMIT = 1000

#: ``composio_client.get_tools(toolkits=[...])`` call sites in the
#: orchestrator — a toolkit missing here is a toolkit whose gate is unproven.
TOOLKITS: tuple[str, ...] = (
    "GMAIL",
    "GOOGLECALENDAR",
    "GOOGLEDRIVE",
    "GOOGLEDOCS",
    "GOOGLESHEETS",
    "OUTLOOK",
)

FIXTURE = pathlib.Path("tests/fixtures/composio_tool_slugs.json")


def _fetch_toolkit(api_key: str, toolkit: str) -> list[dict[str, Any]]:
    """Every tool definition for one toolkit, with truncation made impossible.

    Raises rather than returning a short list: a silently-capped page is the
    failure this function exists to prevent, and a dump that under-reports is
    indistinguishable from a toolkit that is genuinely small.
    """
    import httpx

    r = httpx.get(
        "https://backend.composio.dev/api/v3.1/tools",
        params={"limit": _PAGE_LIMIT, "toolkit_slug": toolkit, "toolkit_versions": "latest"},
        headers={"x-api-key": api_key},
        timeout=120.0,
    )
    r.raise_for_status()
    body = r.json()
    items = body.get("items") or body.get("data") or []
    total = body.get("total_items") or body.get("total")
    if isinstance(total, int) and len(items) < total:
        raise RuntimeError(
            f"{toolkit}: got {len(items)} of {total} tools — the page was capped. "
            f"Raise _PAGE_LIMIT; a partial dump reads as coverage."
        )
    return list(items)


def _describe(tool: Any) -> str:
    """One-line description, clipped. Best-effort across SDK shapes."""
    for attr in ("description", "__doc__"):
        text = getattr(tool, attr, None)
        if isinstance(text, str) and text.strip():
            return " ".join(text.split())[:200]
    if isinstance(tool, dict):
        fn = tool.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("description"), str):
            return " ".join(fn["description"].split())[:200]
    return ""


def _slug(tool: Any) -> str | None:
    """The tool's slug across SDK shapes. ``None`` when it cannot be read —
    counted and reported rather than silently dropped, because a slug we
    cannot see is a slug the gate cannot classify."""
    # ⚠ ``slug`` FIRST, and it is not interchangeable with ``name``. On the
    # REST tool objects ``name`` is the human label ("Modify email labels")
    # while ``slug`` is the identifier the gate matches
    # ("GMAIL_ADD_LABEL_TO_EMAIL"). Reading ``name`` first filled the fixture
    # with prose, and the coverage check then reported 0% classified — the
    # gate looked broken when the dump was simply reading the wrong field.
    if isinstance(tool, dict):
        slug = tool.get("slug")
        if slug:
            return str(slug)
        fn = tool.get("function")
        name = (fn or {}).get("name") if isinstance(fn, dict) else tool.get("name")
        return str(name) if name else None
    for attr in ("slug", "name"):
        value = getattr(tool, attr, None)
        if value:
            return str(value)
    return None


async def dump() -> dict[str, Any]:
    from helixis.connectors.composio_client import ComposioClient
    from helixis.settings import get_settings

    settings = get_settings()
    if not getattr(settings.composio, "api_key", None):
        raise SystemExit(
            "HELIXIS_COMPOSIO__API_KEY is not set. Export the key that is already in "
            "Render — do NOT create a new one (gotcha 1: rotating the key orphans "
            "every user's OAuth connections and there is no bulk re-link)."
        )

    api_key = settings.composio.api_key
    client = ComposioClient(settings)
    await client.initialize()
    if not client.available:
        raise SystemExit("Composio client did not become available — check the key and egress.")

    # No user scope is needed: tool DEFINITIONS are account-level, and the
    # direct fetch reads them without binding an OAuth session. Nothing
    # user-specific is read or written here.

    out: dict[str, Any] = {"toolkits": {}, "unreadable": {}}
    for toolkit in TOOLKITS:
        try:
            # ⚠ NOT client.get_tools. That wrapper builds OAuth-scoped tool
            # objects through the SDK, whose page size is 200 — and OUTLOOK has
            # 305, so the first dump silently captured 200 and the fixture read
            # as FULL COVERAGE while hiding 105 slugs. A partial dump
            # validating a gate is worse than none: every missing slug is one
            # the write-check never saw, reported as if it had been checked.
            #
            # A slug fixture needs names and descriptions, not bound tools, so
            # this reads the tools endpoint directly and asserts the page was
            # not capped against the API's own total_items.
            tools = await asyncio.to_thread(_fetch_toolkit, api_key, toolkit)
        except Exception as exc:  # noqa: BLE001 — one bad toolkit must not lose the rest
            logger.warning("toolkit %s failed: %s", toolkit, exc)
            out["toolkits"][toolkit] = None  # distinct from "returned nothing"
            continue

        entries: list[dict[str, str]] = []
        unreadable = 0
        for tool in tools or []:
            slug = _slug(tool)
            if slug is None:
                unreadable += 1
                continue
            entries.append({"slug": slug, "description": _describe(tool)})
        entries.sort(key=lambda e: e["slug"])
        out["toolkits"][toolkit] = entries
        if unreadable:
            out["unreadable"][toolkit] = unreadable
        print(
            f"  {toolkit:16} {len(entries):4} tools"
            + (f"  ({unreadable} unreadable)" if unreadable else "")
        )

    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print", action="store_true", help="write to stdout instead of the fixture"
    )
    args = parser.parse_args()

    print("Dumping Composio tool slugs per toolkit…")
    result = asyncio.run(dump())

    failed = [k for k, v in result["toolkits"].items() if v is None]
    if failed:
        # Loud, because a partial dump validating a gate is worse than none:
        # the missing toolkit's slugs would silently never be checked.
        print(f"\n⚠ these toolkits did NOT dump and are NOT covered: {failed}", file=sys.stderr)

    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.print:
        print(payload)
        return 1 if failed else 0

    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(payload, encoding="utf-8")
    total = sum(len(v) for v in result["toolkits"].values() if v)
    print(f"\nWrote {FIXTURE} — {total} slugs across {len(TOOLKITS)} toolkits.")
    print("Commit it, then the tool_gate validation test asserts against real slugs.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
