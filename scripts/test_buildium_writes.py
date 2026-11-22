"""Exercise every registered Occupella -> Buildium WRITE end to end.

Drives the real product path for each write tool: POST /agent/run (SSE), wait
for the ``confirm_gate``, answer it via POST /agent/confirm, read the run to
completion. So it tests routing, the gate, the payload the approver actually
sees, the role floor, and the write itself -- not just the API.

!! THESE ARE REAL WRITES TO A REAL BUILDIUM ACCOUNT. Several cannot be undone
by any API, so the harness is tiered and REFUSES BY DEFAULT:

  reversible   (default) update task status, reassign a work-order vendor,
               record + undo a move-out. Every one has a probed undo.
  additive     notes and created records. Cannot be deleted via the API, but
               they only ADD -- nothing existing is altered.
  irreversible post a charge (NOT deletable), record a payment (IMMUTABLE --
               no delete, no edit), close a work order (ONE-WAY: the status
               LATCHES at Completed and there is no API reopen).

Tiers are cumulative: ``--tier additive`` runs reversible + additive.

!! WITHOUT ``--execute`` NOTHING IS WRITTEN. The default run DECLINES every
gate, which still exercises routing, the gate, the role floor and the request
body -- everything except the write. Start there. A decline-mode pass that
reaches a correct gate for all N tools is strong evidence before you spend a
single irreversible write.

WHY PYTHON AND NOT POWERSHELL
-----------------------------
The first cut was a ``.ps1``. It required PowerShell 7 (5.1 has no workable
SSE story) and the only Windows machine that runs this has 5.1 -- where
``#requires -Version 7.0`` is a hard refusal, so the harness could not start
at all. Before that it failed to PARSE on 5.1 because a BOM-less ``.ps1``
containing an em dash is read as CP1252 and ``0x94`` closes a string.

Both failures are the shell, not the harness. Python is what every other
script in this directory already uses on that machine (the live battery runs
there), and ``PYTHONUTF8=1`` plus ``force_utf8_console`` settles the encoding
question for good -- see gotcha 49.

USAGE
-----
    set HELIXIS_WRITE_TEST_TOKEN=<a helixis-test JWT>

    # 1. Prove the whole path with no writes at all.
    python scripts/test_buildium_writes.py --company-id <id> \
        --fixtures scripts/buildium_write_fixtures.example.json

    # 2. Then the writes that can be undone.
    python scripts/test_buildium_writes.py --company-id <id> \
        --fixtures ... --execute

    # 3. Additive records, once you accept they stay in the account.
    python scripts/test_buildium_writes.py --company-id <id> \
        --fixtures ... --execute --tier additive

    # 4. Money + close. Read the warnings first.
    python scripts/test_buildium_writes.py --company-id <id> \
        --fixtures ... --execute --tier irreversible --i-accept-irreversible

Role floor: post_charge / record_payment / close_work_order require Manager or
Admin and FAIL CLOSED. If the JWT's ``company_users`` role is ``member`` those
three refuse before touching Buildium -- correct behaviour, reported as
REFUSED-BY-DESIGN, not a bug.

``send_text_message`` is deliberately NOT covered: Twilio ships dark
(``twilio.enabled=false``), so it refuses before any gate and there is nothing
to exercise until A2P approval.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _console import force_utf8_console  # noqa: E402 -- must follow the path setup

#: The token NEVER comes from argv. A JWT on the command line lands in the
#: process list and in shell history; an env var does neither.
_TOKEN_ENV = "HELIXIS_WRITE_TEST_TOKEN"

_TIER_RANK = {"reversible": 0, "additive": 1, "irreversible": 2}

#: Every verdict that means the confirm gate was reached -- the ROUTING
#: question. ⚠ WROTE-CONTRADICTED belongs here: the gate worked perfectly and
#: the WRITE is what failed, so counting it as "never gated" would point an
#: operator at the wrong half of the system.
_GATE_REACHED = (
    "WROTE",
    "WROTE-VERIFIED",
    "WROTE-CONTRADICTED",
    "WROTE-UNVERIFIED",
    "WROTE-UNCONFIRMED",
    "GATED-DECLINED",
)


def build_cases(fx: dict[str, str]) -> list[dict[str, Any]]:
    """The write surface, tiered by whether the write can be taken back.

    Reversibility is not a judgement call -- each entry cites the probe that
    established it. See CLAUDE.md gotchas 20 (work-order close latches),
    21 (charge non-deletable / payment immutable), 22 (move-out reversible).
    """
    prop, unit, vendor, tenant = fx["property"], fx["unit"], fx["vendor"], fx["tenant"]
    return [
        {
            "tier": "reversible",
            "tool": "buildium_update_task_status",
            "prompt": f"Set the status of the oldest open to-do task at {prop} to In Progress.",
            "undo": "Set that same task back to New.",
            # ⚠ No read-back verifier for THIS undo, deliberately.
            # `verify_update_task_status` asserts InProgress -- pointing it at
            # the undo would report False for a CORRECT restore. A verifier
            # that answers the wrong question is worse than none.
            "undo_tool": None,
            "note": "Reversible: TaskStatus can be set back (gotcha 20).",
        },
        {
            "tier": "reversible",
            "tool": "buildium_update_work_order",
            "prompt": f"Reassign the most recent open work order at {prop} to vendor {vendor}.",
            "undo": None,
            "note": (
                "VendorId is the ONE meaningfully-mutable work-order PUT field; "
                "reassigning again restores it (gotcha 20)."
            ),
        },
        {
            "tier": "reversible",
            "tool": "buildium_record_move_out",
            "prompt": f"Record a move-out for {tenant} at {prop} {unit}, effective today.",
            "undo": f"Undo the move-out you just recorded for {tenant}.",
            "undo_tool": "buildium_undo_move_out",
            "note": (
                "DELETE /leases/{id}/moveouts/{tenantId} is probed and verified "
                "empty on re-GET (gotcha 22). The gate MUST name the CURRENT "
                "tenant -- a former resident lives at the same address."
            ),
        },
        {
            "tier": "additive",
            "tool": "buildium_add_vendor_note",
            "prompt": f"Add a note to vendor {vendor} saying we verified their insurance today.",
            "undo": None,
            "note": "POST /vendors/{id}/notes is the one real append-only note endpoint.",
        },
        {
            "tier": "additive",
            "tool": "buildium_add_work_order_note",
            "prompt": f"Add a note to the most recent open work order at {prop}: parts ordered.",
            "undo": None,
            "note": "Rides WorkDetails on a full-replace PUT -- read-merge-write (gotcha 20).",
        },
        {
            "tier": "additive",
            "tool": "buildium_add_task_note",
            "prompt": f"Add a note to the oldest open task at {prop}: tenant contacted.",
            "undo": None,
            "note": "Rides the Message field of a full-replace PUT, landing as task history.",
        },
        {
            "tier": "additive",
            "tool": "buildium_create_to_do_task",
            "prompt": f"Create a to-do task at {prop} to check the smoke detectors, low priority.",
            "undo": None,
            "note": "Additive: a new task. No API delete.",
        },
        {
            "tier": "additive",
            "tool": "buildium_create_work_order",
            "prompt": f"Create a work order at {prop} {unit} for a dripping kitchen tap.",
            "undo": None,
            "note": "Additive, and the close case below needs it to exist first.",
        },
        {
            "tier": "irreversible",
            "tool": "buildium_post_charge",
            "prompt": f"Post a $1.00 late fee charge to the active lease at {prop} {unit}.",
            "undo": None,
            "note": (
                "!! NOT DELETABLE. Editable via PUT, but DELETE .../charges/{id} "
                "404s (gotcha 21). Manager+ role floor, fails closed."
            ),
        },
        {
            "tier": "irreversible",
            "tool": "buildium_record_payment",
            "prompt": f"Record a $1.00 check payment on the active lease at {prop} {unit}.",
            "undo": None,
            "note": (
                "!! IMMUTABLE -- no delete, no edit, reversal only (gotcha 21). "
                "The strongest guard in the write set: confirm gate + Manager+."
            ),
        },
        {
            "tier": "irreversible",
            "tool": "buildium_close_work_order",
            "prompt": f"Close the work order you created for the dripping kitchen tap at {prop}.",
            "undo": None,
            "note": (
                "!! ONE-WAY. Reverting the backing task does NOT reopen it -- the "
                "WO Status latches at Completed and only the Buildium UI can "
                "reopen (gotcha 20)."
            ),
        },
    ]


class TurnResult:
    """What one agent turn produced."""

    def __init__(self) -> None:
        self.gate_seen = False
        self.confirm_id: str | None = None
        self.summary: str | None = None
        self.request_body: str | None = None
        self.answered: bool | None = None
        self.final_text = ""
        #: The ``done`` event's summary. ⚠ NOT redundant with ``final_text``.
        #: When a run hits the soft deadline, ``deadline_synthesis`` writes the
        #: answer and it ships as ``done_event(...)`` -- it never streams as
        #: tokens. Reading tokens alone reported two real 1,000+ character
        #: answers as an empty reply, which reads as a delivery loss and sent
        #: the operator hunting the wrong bug.
        self.done_summary = ""
        #: ``done.truncated`` -- the run marked this a deadline SALVAGE rather
        #: than the answer it meant to give (see ``common/events.done_event``).
        self.salvaged = False
        self.events: list[str] = []
        self.error: str | None = None

    @property
    def answer(self) -> str:
        """What the user actually saw. Tokens when they streamed, else the
        ``done`` summary -- never an empty string when either exists."""
        return self.final_text or self.done_summary


async def run_turn(
    *, base_url: str, token: str, company_id: str, task: str, approve: bool, timeout_s: float
) -> TurnResult:
    """POST the task, answer the gate mid-stream, read to completion."""
    import httpx

    r = TurnResult()
    headers = {"Authorization": f"Bearer {token}"}
    base = base_url.rstrip("/")

    # !! httpx's `timeout` is PER-READ, not per-request. The server sends a
    # keepalive comment every 15s, and each one resets that clock -- so a
    # stream delivering nothing could never time out. The wall clock is
    # enforced over the whole read loop instead, where keepalives cannot
    # touch it. (Battery v11 q96 sat on a socket for 49 minutes this way.)
    deadline = time.monotonic() + timeout_s
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            async with client.stream(
                "POST",
                f"{base}/api/v1/agent/run",
                headers=headers,
                # `company_id` is REQUIRED by AgentRequest and is what
                # require_company_access authorises against. Omitting it does
                # NOT fall back to the token's own company -- FastAPI rejects
                # the body with 422 before the route runs.
                json={"task": task, "company_id": company_id, "source": "chat"},
            ) as resp:
                if resp.status_code in (401, 403):
                    r.error = (
                        f"HTTP {resp.status_code} -- ${_TOKEN_ENV} is expired or "
                        "belongs to another company. Re-grab it; do NOT read the "
                        "empty result as a routing failure."
                    )
                    return r
                if resp.status_code >= 400:
                    r.error = f"HTTP {resp.status_code} on /agent/run"
                    return r

                async for line in resp.aiter_lines():
                    if time.monotonic() > deadline:
                        r.error = f"wall clock: no completion within {timeout_s:.0f}s"
                        break
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        evt = json.loads(body)
                    except json.JSONDecodeError:
                        continue

                    etype = evt.get("type")
                    data = evt.get("data") or {}
                    if etype:
                        r.events.append(etype)

                    if etype == "confirm_gate":
                        r.gate_seen = True
                        r.confirm_id = data.get("confirm_id")
                        r.summary = data.get("summary")
                        preview = data.get("payload_preview") or {}
                        if preview:
                            r.request_body = json.dumps(preview, ensure_ascii=True)[:4000]
                        # !! Answer from a SEPARATE request while this stream
                        # stays open. The run is BLOCKED inside the tool until
                        # this POST lands (gotcha 4), so reading to completion
                        # first would deadlock until the 300s gate timeout.
                        async with httpx.AsyncClient(timeout=30.0) as answerer:
                            ack = await answerer.post(
                                f"{base}/api/v1/agent/confirm",
                                headers=headers,
                                json={"confirm_id": r.confirm_id, "approved": approve},
                            )
                        if ack.status_code >= 400:
                            r.error = f"HTTP {ack.status_code} on /agent/confirm"
                        else:
                            r.answered = approve
                    # !! The key is `content`, and there are TWO token events.
                    # Verified against common/events.py rather than assumed: a
                    # wrong key reads as "the agent said nothing", which would
                    # misclassify a role-floor refusal as NO-GATE.
                    elif etype in ("token", "assistant_message_token"):
                        r.final_text += data.get("content") or ""
                    elif etype == "done":
                        r.done_summary = data.get("summary") or ""
                        r.salvaged = bool(data.get("truncated"))
                    elif etype == "error":
                        r.error = json.dumps(data, ensure_ascii=True)[:2000]
    except Exception as exc:  # noqa: BLE001 -- any transport failure is a result, not a crash
        r.error = f"{type(exc).__name__}: {exc}"
    return r


_LEDGER_WARNED: set[str] = set()


def _ledger_unavailable(reason: str) -> None:
    """Say WHY the ledger could not answer -- once per distinct reason.

    A verdict that silently degrades to prose is a verdict nobody can size.
    Printed to stderr so it survives a redirect of stdout.
    """
    if reason in _LEDGER_WARNED:
        return
    _LEDGER_WARNED.add(reason)
    print(
        f"!! ledger unreadable ({reason}) -- verdicts fall back to the reply text", file=sys.stderr
    )


async def guard_outcome(db_url: str, company_id: str, action_type: str, since: Any) -> str | None:
    """The guard's OWN record of whether the write landed. Authoritative.

    ⚠ This exists because matching the model's prose could not carry the
    weight, and I shipped a version that tried. The first cut looked for
    "could not confirm"; the tool said "the write could not be confirmed as
    successful" -- one word apart -- so three unconfirmed writes were labelled
    WROTE and the non-zero exit that exists so this cannot be skimmed past
    never fired. Free-form text generated by a model is not a status field.

    `run_guarded` writes the real status: `succeeded`, `abandoned_unknown`
    (ambiguous -- never retried), `aborted` (declined), `blocked_*`. Reading it
    is the difference between asking the model how it went and asking the
    ledger.

    Returns None when unavailable, and the caller then falls back to prose --
    FAIL-CLOSED, because an unverifiable write must never read as a clean one.
    """
    try:
        import asyncpg
    except ImportError:
        # Found by this file's own guard: without asyncpg every verdict quietly
        # degraded to prose and nothing said so.
        _ledger_unavailable("asyncpg is not installed")
        return None
    try:
        conn = await asyncpg.connect(db_url, statement_cache_size=0)
    except Exception as exc:  # noqa: BLE001 -- a missing read must not lose the run
        _ledger_unavailable(f"connect failed: {type(exc).__name__}")
        return None
    try:
        row = await conn.fetchrow(
            # Company-scoped (gotcha 47).
            """
            SELECT status FROM helixis_action_idempotency
             WHERE company_id = $1 AND action_type = $2 AND updated_at >= $3
             ORDER BY updated_at DESC LIMIT 1
            """,
            company_id,
            action_type,
            since,
        )
        return str(row["status"]) if row else None
    except Exception as exc:  # noqa: BLE001
        # ⚠ A BROKEN QUERY AND AN UNREACHABLE DATABASE MUST NOT LOOK THE SAME.
        # The first cut cast `company_id = $1::uuid`; the column is TEXT, so
        # Postgres raised `operator does not exist: text = uuid`, this handler
        # swallowed it, None meant "ledger unavailable", and every verdict fell
        # back to blanket fail-closed doubt. The run reported UNCONFIRMED for
        # eight cases including two that had genuinely SUCCEEDED, and nothing
        # said why. Silence is the wrong answer to a bug in our own SQL.
        _ledger_unavailable(f"query failed: {type(exc).__name__}: {exc}")
        return None
    finally:
        await conn.close()


class _BuildiumReadBack:
    """A Buildium client opened with the COMPANY's own credentials, for
    re-GETting what a write claims to have done.

    ⚠ This exists because ``succeeded`` in ``helixis_action_idempotency``
    means Buildium returned 2xx -- and on 2026-08-30 a ``record_move_out``
    returned 2xx, logged ``succeeded``, and produced no move-out (gotcha 70).
    The ledger answers "did the call complete"; only a re-GET answers "did the
    thing the tool is NAMED for happen".

    Fails soft in one direction only: if the client cannot be opened, every
    verdict is UNVERIFIABLE, which is never a pass.
    """

    def __init__(self, company_id: str) -> None:
        self.company_id = company_id
        self.api: Any = None
        self.source: str | None = None
        self.reason: str | None = None
        self._client: Any = None
        self._pool: Any = None

    async def open(self) -> None:
        try:
            from helixis.buildium.api import BuildiumApi
            from helixis.buildium.client import BuildiumClient
            from helixis.buildium.credentials import load_buildium_settings_for_company
            from helixis.db.engine import init_pool
            from helixis.settings import Settings
        except Exception as exc:  # noqa: BLE001 -- the harness runs without the package installed
            self.reason = f"helixis is not importable here ({type(exc).__name__})"
            return

        settings = Settings()
        creds = None
        # ⚠ Per-COMPANY credentials need a DB POOL open first. Without it
        # `load_buildium_settings_for_company` raises "Postgres pool not
        # initialized", the except reads it as "no stored row", and the
        # read-back silently probes whatever account the env points at --
        # which is the sandbox. Same bug paid_bills_probe.py shipped once.
        dsn = getattr(settings.supabase, "db_url", None)
        if dsn:
            try:
                self._pool = await init_pool(dsn)
                creds = await load_buildium_settings_for_company(self.company_id)
            except Exception as exc:  # noqa: BLE001 -- a missing row is a real answer
                self.reason = f"no stored Buildium credentials ({type(exc).__name__})"
        else:
            self.reason = "HELIXIS_SUPABASE__DB_URL is not set"

        # ⚠ Deliberately NO env fallback. The env pair points at
        # apisandbox.buildium.com -- a DIFFERENT account -- so a read-back
        # against it would confidently verify the wrong books, which is worse
        # than not verifying at all.
        if creds is None or not (
            getattr(creds, "client_id", None) and getattr(creds, "client_secret", None)
        ):
            self.reason = self.reason or "the company has no stored Buildium credentials"
            await self.close()
            return

        try:
            self._client = BuildiumClient(creds)
            await self._client.__aenter__()
            self.api = BuildiumApi(self._client)
            self.source = getattr(creds, "base_url", None) or "(default)"
        except Exception as exc:  # noqa: BLE001
            self.reason = f"could not open a Buildium client ({type(exc).__name__})"
            self.api = None
            await self.close()

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception:  # noqa: BLE001
                pass
            self._pool = None

    async def check(self, tool: str, fx: dict[str, str]) -> tuple[bool | None, str]:
        if self.api is None:
            return None, self.reason or "read-back is unavailable"
        import write_verify

        return await write_verify.verify(tool, self.api, fx)


def verdict_for(*, result: TurnResult, execute: bool) -> str:
    """Classify one turn. A role-floor refusal is CORRECT, not a failure.

    ⚠ Three DIFFERENT things used to collapse into ``NO-GATE``, and only one
    of them is a gating question:

    * the role floor refused before any gate -- correct behaviour;
    * the run hit its deadline and never reached the write tool -- a
      performance finding, and the write surface is untested for that case;
    * the tool ran and raised no gate -- the serious one, the reason this
      pass exists.

    Reporting all three identically sent an operator looking for a gate bypass
    in a run that had spent 174 seconds failing to identify a record. A verdict
    that cannot distinguish those is not worth printing.
    """
    if result.error:
        return "ERROR"
    if not result.gate_seen:
        low = result.answer.lower()
        if any(w in low for w in ("manager", "admin", "permission", "not enabled")):
            return "REFUSED-BY-DESIGN"
        if result.salvaged:
            return "DEADLINE-NO-WRITE"
        return "NO-GATE"
    if not execute:
        return "GATED-DECLINED"
    # ⚠ WROTE USED TO MEAN "a gate appeared and we approved it" -- never that
    # the write landed. On 2026-08-30 all three reversible writes printed WROTE
    # while the guard had reaped them `abandoned_unknown / ambiguous`, the
    # agents said in plain words they could not confirm, and two of three undos
    # failed. A manifest that overstates exactly where accuracy matters most is
    # worse than no manifest.
    #
    # The reply is the only channel the harness has -- these strings are the
    # tools' own `blocked_needs_review` / verify-failed wording.
    # ⚠ FAIL CLOSED. The previous cut listed the phrases that mean FAILURE and
    # defaulted to WROTE -- so a phrasing one word outside the list ("the write
    # could not be confirmed as successful" vs "could not confirm") read as a
    # clean write. Three did, on a live run, and the non-zero exit never fired.
    #
    # An unverifiable write must never read as a confirmed one, so the default
    # is now UNCONFIRMED and only a POSITIVE confirmation upgrades it. The
    # asymmetry is the point: over-reporting doubt costs a manual check;
    # over-reporting success is how an immutable payment gets retried.
    low = result.answer.lower()
    confirmed = any(
        p in low
        for p in (
            "confirmed in buildium",
            "went through",
            "in buildium.",
            "has been added",
            "was created",
            "was updated",
        )
    )
    doubted = any(
        p in low
        for p in (
            "confirm",  # every "can't/couldn't/could not be confirmed" shape
            "flagging it for review",
            "didn't go through",
            "did not go through",
            "connection error",
            "couldn't safely process",
            "blocked",
        )
    )
    return "WROTE" if (confirmed and not doubted) else "WROTE-UNCONFIRMED"


async def main_async(args: argparse.Namespace, token: str) -> int:
    fx_path = Path(args.fixtures)
    if not fx_path.exists():
        print(f"Fixtures file not found: {fx_path}", file=sys.stderr)
        return 2
    fx = json.loads(fx_path.read_text(encoding="utf-8"))
    missing = [k for k in ("property", "unit", "vendor", "tenant") if not fx.get(k)]
    if missing:
        print(f"Fixtures missing required key(s): {missing}", file=sys.stderr)
        return 2

    cases = build_cases(fx)
    selected = [c for c in cases if _TIER_RANK[c["tier"]] <= _TIER_RANK[args.tier]]
    if args.only:
        selected = [c for c in selected if c["tool"] in args.only]
    if not selected:
        print(f"No cases selected for tier '{args.tier}'.", file=sys.stderr)
        return 2

    mode = (
        "EXECUTE -- gates will be APPROVED and Buildium WILL be written"
        if args.execute
        else "dry run -- every gate will be DECLINED, nothing is written"
    )
    print()
    print("Occupella -> Buildium write path")
    print(f"  tier   : {args.tier} ({len(selected)} case(s))")
    print(f"  mode   : {mode}")
    print(f"  target : {args.base_url}  company {args.company_id}")
    print()

    if args.execute and args.tier == "irreversible":
        print("!! IRREVERSIBLE TIER. This posts a real charge, records an IMMUTABLE")
        print("   payment, and CLOSES a work order one-way. None can be undone")
        print("   through the API.")
        if args.i_accept_irreversible:
            print("   --i-accept-irreversible was passed; proceeding without a prompt.")
        elif not sys.stdin.isatty():
            # Say WHY rather than reading EOF and aborting with a bare
            # "Aborted." Nothing has been written at this point: this check
            # sits before the write loop, deliberately.
            print()
            print("   stdin is not a terminal, so the confirmation cannot be answered.")
            print("   NOTHING HAS BEEN WRITTEN -- this check runs before the first case.")
            print("   Either run this in a real terminal, or pass --i-accept-irreversible.")
            return 2
        else:
            if input("   Type EXACTLY 'i accept' to continue: ").strip() != "i accept":
                print("Aborted -- nothing was written.")
                return 2

    # Optional but strongly preferred: with it, the verdict comes from the
    # guard's ledger instead of the model's prose. Same variable the battery
    # already uses on this machine.
    db_url = os.environ.get("HELIXIS_SUPABASE__DB_URL", "").strip()
    if args.execute and not db_url:
        print("!! HELIXIS_SUPABASE__DB_URL is not set, so WROTE/WROTE-UNCONFIRMED")
        print("   falls back to reading the model's wording. It fails CLOSED, but")
        print("   set it and the verdict comes from the write ledger itself.")
        print()

    turn = dict(
        base_url=args.base_url,
        token=token,
        company_id=args.company_id,
        timeout_s=float(args.timeout),
    )
    # The read-back. Opened ONCE for the run: it is the same company every
    # case, and a per-case client would pay the credential decrypt each time.
    readback: _BuildiumReadBack | None = None
    if args.execute and not args.no_verify:
        readback = _BuildiumReadBack(args.company_id)
        await readback.open()
        if readback.api is None:
            print(f"!! read-back unavailable: {readback.reason}")
            print("   Writes will report WROTE-UNVERIFIED -- the ledger can say the")
            print("   call returned 2xx, but NOT that the record changed (gotcha 70).")
            print()
        else:
            print(f"read-back: {readback.source}")
            print()

    results: list[dict[str, Any]] = []
    try:
        rc = await _run_cases(
            selected=selected,
            args=args,
            turn=turn,
            fx=fx,
            db_url=db_url,
            readback=readback,
            results=results,
            mode=mode,
        )
    finally:
        if readback is not None:
            await readback.close()
    return rc


async def _run_cases(
    *,
    selected: list[dict[str, Any]],
    args: argparse.Namespace,
    turn: dict[str, Any],
    fx: dict[str, str],
    db_url: str,
    readback: _BuildiumReadBack | None,
    results: list[dict[str, Any]],
    mode: str,
) -> int:
    for c in selected:
        print(f"[{c['tool']}] ", end="", flush=True)
        started = datetime.now(UTC)
        r = await run_turn(task=c["prompt"], approve=args.execute, **turn)
        v = verdict_for(result=r, execute=args.execute)
        # The guard's own status OVERRIDES the prose reading when we can see
        # it -- asking the ledger beats asking the model how it went.
        ledger_status: str | None = None
        if args.execute and db_url and v in ("WROTE", "WROTE-UNCONFIRMED"):
            ledger_status = await guard_outcome(db_url, args.company_id, c["tool"], started)
            if ledger_status == "succeeded":
                v = "WROTE"
            elif ledger_status is not None:
                v = "WROTE-UNCONFIRMED"

        # The re-GET. Runs on WROTE only: an UNCONFIRMED write is already
        # flagged for a human, and a CONTRADICTED verdict is a stronger claim
        # than the ledger can support, so it must come from a read of the
        # record itself.
        verified: bool | None = None
        verify_detail: str | None = None
        if readback is not None and v == "WROTE":
            verified, verify_detail = await readback.check(c["tool"], fx)
            if verified is True:
                v = "WROTE-VERIFIED"
            elif verified is False:
                v = "WROTE-CONTRADICTED"
            else:
                v = "WROTE-UNVERIFIED"

        print(v + (f"  [{ledger_status}]" if ledger_status else ""), flush=True)
        if verify_detail:
            print(f"  read-back: {verify_detail}", flush=True)

        row: dict[str, Any] = {
            "tool": c["tool"],
            "tier": c["tier"],
            "verdict": v,
            "prompt": c["prompt"],
            "note": c["note"],
            "gate_summary": r.summary,
            "request_body": r.request_body,
            "final_text": r.answer,
            "streamed_tokens": bool(r.final_text),
            "salvaged_at_deadline": r.salvaged,
            "guard_status": ledger_status,
            # ⚠ `guard_status` is HTTP-level ("Buildium returned 2xx");
            # `verified` is semantic ("the record actually says so"). They
            # disagreed once and the disagreement was the whole finding.
            "verified": verified,
            "verify_detail": verify_detail,
            "error": r.error,
            "events": sorted(set(r.events)),
        }
        results.append(row)

        # Undo what has a probed undo IMMEDIATELY -- not at the end, so a
        # crash mid-run leaves the smallest possible residue.
        # ⚠ Only undo a CONFIRMED write. Undoing one we cannot prove landed is a
        # second unverified write on top of an unknown state -- which is
        # what turned three ambiguous writes into six on 2026-08-30.
        # ⚠ Undo only what is KNOWN to have landed. WROTE-CONTRADICTED means
        # nothing was written, so an undo would be a fresh unverified write on
        # top of an unknown state -- which is how three ambiguous writes became
        # six on 2026-08-30. UNVERIFIED is the same fail-closed rule the
        # ledger's UNCONFIRMED already had.
        landed = v == "WROTE-VERIFIED" or (readback is None and v == "WROTE")
        if args.execute and c["undo"] and landed:
            print("  undo: ", end="", flush=True)
            u = await run_turn(task=c["undo"], approve=True, **turn)
            ok = u.gate_seen and not u.error
            undo_detail: str | None = None
            # An undo is a write too, and "the gate appeared" is exactly the
            # claim gotcha 70 caught being wrong. Re-GET it where a verifier
            # for the undo direction exists.
            if ok and readback is not None and c.get("undo_tool"):
                undo_verified, undo_detail = await readback.check(c["undo_tool"], fx)
                if undo_verified is False:
                    ok = False
                row["undo_verified"] = undo_verified
            print("ok" if ok else "FAILED -- clean up by hand", flush=True)
            if undo_detail:
                print(f"    read-back: {undo_detail}", flush=True)
            row["undo_ok"] = ok
            row["undo_detail"] = undo_detail

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    manifest = out_dir / f"run-{stamp}.json"
    # encoding= is not optional on Windows: the default is cp1252 and a crash
    # here lands AFTER every write has happened (gotcha 49).
    manifest.write_text(
        json.dumps(
            {
                "mode": mode,
                "tier": args.tier,
                "base_url": args.base_url,
                "company_id": args.company_id,
                "results": results,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print()
    print(f"manifest: {manifest}")
    if args.execute:
        print("!! Records created by this run are listed in the manifest. Anything in")
        print("   the additive or irreversible tiers stays in Buildium -- remove it")
        print("   in the UI.")
    gated = [r for r in results if r["verdict"] in _GATE_REACHED]
    verified = [r for r in results if r["verdict"] == "WROTE-VERIFIED"]
    contradicted = [r for r in results if r["verdict"] == "WROTE-CONTRADICTED"]
    unverified = [r for r in results if r["verdict"] == "WROTE-UNVERIFIED"]
    unconfirmed = [r for r in results if r["verdict"] == "WROTE-UNCONFIRMED"]
    bad = [r for r in results if r["verdict"] in ("ERROR", "NO-GATE")]
    stalled = [r for r in results if r["verdict"] == "DEADLINE-NO-WRITE"]
    print()
    print(f"{len(gated)}/{len(results)} reached a confirm gate.")
    if args.execute and not args.no_verify:
        # ⚠ The headline number this pass exists for. "Reached a confirm gate"
        # is a ROUTING claim; this is the semantic one -- the record itself was
        # re-read and says what the tool is named for.
        print(f"{len(verified)}/{len(results)} verified by re-reading the record.")
    if contradicted:
        print()
        print("!! WROTE BUT THE RECORD DISAGREES -- the call returned 2xx and the")
        print("   ledger logged success, but re-reading Buildium shows the change")
        print("   did NOT happen. This is the gotcha-70 defect. Nothing was undone")
        print("   (there is nothing to undo); do NOT re-run to 'fix' it:")
        for r in contradicted:
            print(f"  - {r['tool']}: {r['verify_detail']}")
    if unverified:
        print()
        print("Approved and logged, NOT read back -- no verifier could answer, so")
        print("this is neither a pass nor a failure. Check the record by hand:")
        for r in unverified:
            print(f"  - {r['tool']}: {r['verify_detail']}")
    if stalled:
        # Deliberately NOT counted with `bad`: the write surface is untested
        # here, not broken. Saying so is the difference between "fix the gate"
        # and "fix the lookup that ran out of time before reaching it".
        print()
        print("Ran out of time before reaching the write (the run answered, but")
        print("the gate was never exercised -- a lookup problem, not a gate one):")
        for r in stalled:
            print(f"  - {r['tool']}")
    if unconfirmed:
        # Not counted in `bad` -- the gate worked. The WRITE is the unknown,
        # and `abandoned_unknown` is never retried by design, so a human has
        # to look at the record itself.
        print()
        print("APPROVED BUT NOT CONFIRMED -- the tool could not verify the write")
        print("landed. Check these records in Buildium directly; the harness")
        print("did NOT undo them, and re-running would write a second time:")
        for r in unconfirmed:
            print(f"  - {r['tool']}")
    if bad:
        print()
        print("Did NOT gate (routing or tool problem -- investigate before trusting")
        print("the rest):")
        for r in bad:
            print(f"  - {r['tool']}: {r['verdict']} {r['error'] or ''}")
    return 1 if (bad or stalled or unconfirmed or contradicted or unverified) else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--base-url", default="https://your-app.vercel.app")
    p.add_argument("--company-id", required=True)
    p.add_argument("--fixtures", required=True)
    p.add_argument("--tier", choices=sorted(_TIER_RANK), default="reversible")
    p.add_argument("--execute", action="store_true", help="approve gates and WRITE")
    p.add_argument(
        "--i-accept-irreversible",
        action="store_true",
        help="non-interactive consent for the irreversible tier",
    )
    p.add_argument(
        "--no-verify",
        action="store_true",
        help=(
            "skip the read-back re-GET after each write. Only for a run where "
            "the company's Buildium credentials are unavailable -- WROTE then "
            "means 'the call returned 2xx', not 'the record changed'."
        ),
    )
    p.add_argument("--out", default="buildium-write-tests")
    p.add_argument("--only", nargs="*", help="restrict to these tool names")
    p.add_argument("--timeout", type=int, default=300)
    return p


def main(argv: list[str] | None = None) -> int:
    force_utf8_console()
    args = build_parser().parse_args(argv)
    token = os.environ.get(_TOKEN_ENV, "").strip()
    if not token:
        print(
            f"No token. Set {_TOKEN_ENV} to a helixis-test JWT (DevTools -> "
            "Application -> Local Storage). It is deliberately NOT a command-line "
            "argument: argv is visible in the process list and in shell history.",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(main_async(args, token))


if __name__ == "__main__":
    raise SystemExit(main())
