"""How many tokens does each agent's TOOL ARRAY cost, per model call?

WHY THIS EXISTS
---------------
The Anthropic cache breakpoint is stamped on ``tools[-1]``
(``llm/prompt_cache.py``), which means **the tool array IS the cached prefix**
and every agent has its own array — therefore its own cache entry, therefore
its own cache WRITE. Measured on the live company 2026-08-25/26, cache writes
were **35-53% of all model spend**, the largest single line item.

The in-repo half of that surface is measurable offline and was: 103 tools,
~21,099 tokens, with ``buildium_agent`` alone carrying 74 tools / ~16,104
tokens. The COMPOSIO half is not — ``tools.get()`` needs a live key, so a dev
sandbox measures zero and would happily report "no problem here".

That gap matters more than everything measured, because ``get_tools`` applies
**no allowlist** (``connectors/composio_client.py``: ``tools_accessor.get(
user_id=..., toolkits=slugs, limit=200)``) and ``drive_agent`` does a bare
``tools.extend(drive_tools)``. From the committed slug dump that is
GOOGLEDRIVE 90 + GOOGLEDOCS 43 + GOOGLESHEETS 53 = **186 tool schemas on every
one of that specialist's model calls**, against a 16-turn ceiling.

So: run this where the Composio key lives.

⚠ **AND THE ALLOWLIST IS THE LEVER, NOT THE ORDERING.** The obvious reading of
the paragraph above is "the volatile block leads, so reorder it and cache the
stable prefix". Measured 2026-08-29, the in-repo tail each connector agent
appends is 991 (drive) / 1,121 (gmail) / 931 (calendar) tokens against
Anthropic's **1024-token minimum cacheable block** — so that reorder buys a
mark the provider ignores on two of three, and 9% of margin on the third,
using a tokenizer that is not Anthropic's. The 186 Composio schemas are
per-user by construction and no ordering makes them shareable. Trimming them
is the change that moves the number. Recorded in
``tests/unit/test_stable_tool_block_cacheability.py``, which re-measures and
fails if the stable block ever grows enough to flip the answer.

⚠ MEASURES, CHANGES NOTHING. Read-only: it builds tool arrays and counts bytes.

⚠ TOKEN COUNTS ARE APPROXIMATE and the output names the method. ``tiktoken``'s
``cl100k`` is OpenAI's tokenizer, not Anthropic's, so treat these as sizing and
ranking rather than as billing. Without tiktoken it falls back to bytes/4 and
says so. A fabricated precision here would be the ``$0.00`` bill of gotcha 25
wearing a different hat.

For calibration against real billing: the production ledger puts buildium's
FULL prefix (tools + instructions + system) at **32,953 tokens**, solved from
turn 2026-08-26 19:27's two cache reads. This script measures the tools alone
at ~16.8k, so instructions and the system message roughly double it, and
Anthropic tokenizes schema-dense JSON less efficiently than cl100k does.

Usage:
    python scripts/measure_tool_payload.py                 # table
    python scripts/measure_tool_payload.py --json out.json # machine-readable
    python scripts/measure_tool_payload.py --user <id>     # a specific company
"""

from __future__ import annotations

import argparse
import json
from typing import Any

#: Anthropic cache-write rate, $/token. A tool array sits inside the cached
#: prefix, so its cost is paid on WRITE (once per distinct prefix per TTL
#: window), not on every call — which is why a big array is expensive in a way
#: a per-call view understates.
_WRITE_1H = 4.0e-6


def _count_tokens(text: str) -> tuple[int, str]:
    """Token count of the REAL text, and the METHOD used so a reader can weight it.

    ⚠ Must receive the actual serialized schema. The first cut of this script
    passed ``"x" * total_bytes`` — a run of a single repeated character, which
    a BPE tokenizer packs far denser than JSON — and reported 8,082 tokens for
    a payload tiktoken puts at more than double that. Measuring a stand-in of
    the right LENGTH is not measuring the thing; it is gotcha 33's "an attack
    that never runs is an attack that never succeeds", pointed at a cost
    number.
    """
    try:
        import tiktoken

        # cl100k is OpenAI's, not Anthropic's — close enough for ranking and
        # sizing, and named here so nobody reads it as exact Anthropic billing.
        return len(tiktoken.get_encoding("cl100k_base").encode(text)), "tiktoken cl100k (≈)"
    except Exception:  # noqa: BLE001 — offline sandboxes cannot fetch the encoding
        return len(text) // 4, "bytes/4 ESTIMATE (±15%)"


def _tool_wire_text(tool: Any) -> str:
    """Serialize one tool the way the SDK actually sends it.

    Goes through the real converter rather than ``str(tool)`` — a tool's wire
    form is name + description + JSON schema, and guessing at that is how you
    measure something adjacent to the thing you care about.
    """
    try:
        from agents.models.chatcmpl_converter import Converter

        return json.dumps(Converter.tool_to_openai(tool))
    except Exception:  # noqa: BLE001 — fall back to the parts we can reach
        return "".join(
            [
                str(getattr(tool, "name", "")),
                str(getattr(tool, "description", "")),
                json.dumps(getattr(tool, "params_json_schema", {}) or {}),
            ]
        )


def _measure(label: str, tools: list[Any]) -> dict[str, Any]:
    wire = [(str(getattr(t, "name", "?")), _tool_wire_text(t)) for t in tools]
    per_tool = sorted(((n, len(s)) for n, s in wire), key=lambda kv: -kv[1])
    tokens, method = _count_tokens("".join(s for _, s in wire))
    return {
        "agent": label,
        "tool_count": len(tools),
        "bytes": sum(b for _, b in per_tool),
        "tokens": tokens,
        "token_method": method,
        "largest": per_tool[:10],
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--user", default="", help="Composio user/company id to fetch tools for")
    p.add_argument("--json", default="", help="write the full result here")
    args = p.parse_args()

    from helixis.settings import Settings

    settings = Settings()

    rows: list[dict[str, Any]] = []
    notes: list[str] = []

    # ── the Composio half: the whole point of running this with a key ──────
    try:
        from helixis.connectors.composio_client import ComposioClient

        client = ComposioClient(settings)
        if not client.available:
            notes.append(
                "COMPOSIO UNAVAILABLE — no key or no egress. The Composio rows below are "
                "EMPTY and that is a harness artifact, NOT a finding. Re-run where "
                "HELIXIS_COMPOSIO__API_KEY is set."
            )
        for label, toolkits in (
            ("drive_agent", ["GOOGLEDRIVE", "GOOGLEDOCS", "GOOGLESHEETS"]),
            ("gmail_agent", ["GMAIL"]),
            ("calendar_agent", ["GOOGLECALENDAR"]),
        ):
            fetched = (
                client.get_tools(toolkits=toolkits, user_id=args.user) if client.available else []
            )
            row = _measure(f"{label} (composio: {'+'.join(toolkits)})", fetched)
            row["toolkits"] = toolkits
            rows.append(row)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Composio measurement failed: {type(exc).__name__}: {exc}")

    # ── the in-repo half: measurable anywhere ──────────────────────────────
    try:
        from helixis.agents.buildium_agent import create_buildium_agent

        rows.append(
            _measure("buildium_agent (in-repo)", list(create_buildium_agent(settings).tools))
        )
    except Exception as exc:  # noqa: BLE001
        notes.append(f"buildium_agent could not be built: {type(exc).__name__}: {exc}")

    rows.sort(key=lambda r: -r["bytes"])

    print(f"\n{'agent':<52} {'tools':>6} {'bytes':>9} {'~tokens':>9}  cache-write $ (1h)")
    print("-" * 100)
    for r in rows:
        cost = r["tokens"] * _WRITE_1H
        print(
            f"{r['agent']:<52} {r['tool_count']:>6} {r['bytes']:>9,} {r['tokens']:>9,}  ${cost:.4f}"
        )
    if rows:
        print(f"\ntoken method: {rows[0]['token_method']}")

    print("\nWhat a number here MEANS: this array is re-sent on every model call in that")
    print("agent's chain, and a cache MISS re-writes all of it. A specialist that runs to")
    print("its turn ceiling multiplies this by that ceiling (agents/specialist_limits.py).")

    for r in rows:
        if r["largest"]:
            print(f"\n  {r['agent']} — 10 largest:")
            for name, b in r["largest"]:
                print(f"      {name:<52} {b:>7,} B")

    for n in notes:
        print(f"\n⚠ {n}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"rows": rows, "notes": notes}, fh, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
