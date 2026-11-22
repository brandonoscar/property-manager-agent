#!/usr/bin/env python3
"""Exercise every role gate against the DEPLOYED backend, as three real users.

Until 2026-08-23 every company in production had exactly one member and every
member was ``admin`` — zero ``manager`` rows, zero ``member`` rows, ever. So
every role floor we shipped had unit tests and **no production evidence**: a
gate that refuses everyone and a gate that refuses no one both looked fine.

That is the gap this closes. It runs the real HTTP surface with real JWTs, and
it asserts BOTH directions on every gate, because only one of them is usually
tested:

* a ``member`` must be REFUSED, and
* a ``manager``/``admin`` must be LET THROUGH.

A floor that blocks everybody is exactly as broken as one that blocks nobody,
and it fails much more quietly — nobody files a bug saying "the dangerous
thing correctly didn't happen".

⚠ **A gate can only be tested by a probe that is valid in every respect except
the one thing under test.** Both of this file's original failures broke that
rule: the invite probe sent an empty email, and the approval probe sent a bogus
confirm id. In each case a PRECONDITION -- Pydantic body validation, and the
confirm-row lookup -- was evaluated before authorization, so the probe came
back with a plausible status from code that never reached the floor. A 422 or a
404 produced by a precondition is indistinguishable from a verdict unless you
check WHICH LINE produced it. Both probes reported a pass on one row and proved
nothing on any.

Two properties here are subtler than the rest and are the reason this is a
script rather than a checklist:

**Reaching the confirm gate is the pass condition for a manager, not
completing the write.** These tools are confirm-gated; a manager who silently
executed would be a worse failure than one who was refused. So the assertion
is "a ``confirm_gate`` event arrived", never "the money moved". Nothing here
ever approves anything, so no write is ever performed.

**Approve and decline are deliberately asymmetric** (``api/routes/agent.py``).
Approving drives a side-effecting write, so it takes Manager+; declining stays
open to any member, because blocking someone from STOPPING an action would be
backwards. A test that only checked "member can't approve" would pass against
a build that had broken decline too.

Usage (nothing is written; every gated action is abandoned at the gate)::

    python scripts/role_matrix.py
    python scripts/role_matrix.py --only floors      # just the money writes
    python scripts/role_matrix.py --json report.json

Environment::

    HELIXIS_BATTERY_BASE_URL    optional; defaults to Render directly (see _DEFAULT_BASE)
    HELIXIS_SUPABASE_URL        https://abcdefghijklmnopqrst.supabase.co
    HELIXIS_SUPABASE_ANON_KEY   the public anon key
    OCCUPELLA_ADMIN_EMAIL / OCCUPELLA_ADMIN_PASSWORD
    OCCUPELLA_TEST_PASSWORD     password for the two seeded .test users
    HELIXIS_BATTERY_COMPANY_ID  Blue Ridge: 5ca1ab1e-0000-4000-8000-000000000002
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MEMBER_EMAIL = "member@occupella.test"
MANAGER_EMAIL = "manager@occupella.test"

#: Manager-or-above, verified from the code 2026-08-23. Each is confirm-gated
#: AND role-gated; the role floor must fire BEFORE the gate so nobody is asked
#: to approve something they are not permitted to do.
#: Every probe names ONE record by id.
#: All 50 seeded properties have a unit numbered "1", so "the tenant in unit 1"
#: names nothing -- the agent asks which one instead of acting, no gate opens,
#: and a member's non-refusal then reads as the floor working while a
#: manager's reads as the floor broken. Neither is a role verdict.
#: "the oldest open work order" has the same defect: it requires a ranking the
#: agent has to derive before it can act.
#: ⚠ Avoid lease 115937 -- Buildium's unit lookup and its lease record disagree
#: about where its tenant lives, so the agent correctly refuses to write against
#: it. That is a real data finding, parked separately; do not route other probes
#: around it silently.
#: Render DIRECTLY, not the canonical Vercel URL -- one less hop in a test
#: harness, and nothing more than that.
#:
#: ⚠ This comment previously asserted that Vercel's proxy DROPS these streams,
#: on the strength of one httpx.ReadError through your-app.vercel.app
#: beside one success straight to Render. That claim is WRONG and is recorded
#: here so nobody re-derives it. Two controlled rounds since (2026-08-24):
#:
#:      vercel  68.2s   571 chars  COMPLETED
#:      vercel 156.4s   797 chars  COMPLETED
#:      vercel 155.8s  1382 chars  COMPLETED
#:      render  85.1s   399 chars  COMPLETED   <- control
#:
#: Six long streams total, zero transport failures, two of them held open
#: nearly twice the control's duration and returning MORE content. The single
#: ReadError landed inside the window when every request was failing for an
#: unrelated reason; one sample per host, compared across time, is not a
#: controlled comparison -- which is exactly the mistake the paragraph above
#: used to enshrine. Same shape as the orchestrator prompt claiming a clock
#: tool existed: a diagnosis outliving its evidence and sending the next
#: reader after a bug that isn't there.
#:
#: ⚠ A zero-character response after ~250s is NOT a dropped stream. Three
#: distinct causes used to be indistinguishable at this seam; all three now
#: announce themselves, so read the message before blaming the network:
#:   * the 240s agent deadline  -> the stream itself says the turn had to stop
#:     early (a portfolio-wide question voids at ~248s on EVERY host, control
#:     included -- too large to finish anywhere, not a transport fault);
#:   * a provider spend cap     -> the honest capacity message (#878);
#:   * an actual request fault  -> BadRequestError in orchestrator_runs.
#: Check those three before changing this default for a transport reason.
_DEFAULT_BASE = "https://your-api.onrender.com"

#: ⚠ Every probe carries an ABSOLUTE date. The agent has no reliable "today",
#: so "effective next month" or an omitted date makes it ask a clarifying
#: question instead of acting -- correct behaviour, and it means the gate is
#: never reached. A member who is never refused because the tool was never
#: called reads as a member who walked past the floor.
GATED_ASKS = {
    "record_payment": (
        "Record a $50 check payment against lease 115934, dated 2026-08-23. "
        "Memo: role matrix probe. Do not ask me for more details."
    ),
    "post_charge": (
        "Post a $75 late fee charge to lease 115934, dated 2026-08-23. "
        "Do not ask me for more details."
    ),
    "close_work_order": "Close work order 11304.",
}

#: Deliberately NOT role-gated — reversible, so confirm-only (gotcha 22).
#: A member must still reach its gate; if the floor has leaked onto this one
#: that is a regression, and it would look like "extra safety".
#: Must name ONE lease unambiguously, by id.
#: "the tenant in unit 1" is ambiguous across all 50 seeded properties, so the
#: agent stopped to ask which tenant and no gate opened. Naming the tenant did
#: not help either: Buildium's unit lookup puts Marcus Whitfield at Unit 1
#: Phoenix while his own lease record says 600 Dexter Ave, Montgomery, and the
#: agent correctly refuses to file an irreversible write against contradictory
#: data. Both refusals are right and both measure the HARNESS, not the floor --
#: a probe that cannot reach the gate cannot test it. The lease id sidesteps
#: every lookup that disagrees.
#: ⚠ The DATE must be ABSOLUTE too. "the last day of next month" made the agent
#: stop and ask, and its reason is worth keeping: "none of my tools expose the
#: system clock directly." It cannot resolve a relative date, so a probe
#: carrying one measures that gap and never reaches the gate behind it.
UNGATED_ASK = (
    "Record a move-out on Buildium lease 115934, effective 2026-09-30. "
    "Use that lease id exactly; do not search for the tenant by name."
)

#: Role-refusal detection. Prose, so it is a heuristic -- but the first version
#: matched only "manager or admin" and the live agent said "Manager/Admin
#: access", so two floors that WORKED were reported as broken. A refusal read
#: as a hole is the expensive direction of this error: it sends someone
#: hunting a security bug that isn't there.
_REFUSAL_RE = re.compile(
    r"(managers?[\s/-]*(or|and|&)?[\s/-]*admins?"
    r"|admins?[\s/-]*(or|and|&)?[\s/-]*managers?"
    r"|manager\+"
    r"|requires? (a )?(manager|admin)"
    r"|(don'?t|does not|doesn'?t|not) have (the )?(permission|access|role))",
    re.IGNORECASE,
)

#: Invite probe. The email must be VALID or Pydantic rejects the body before
#: the ADMIN floor is reached; the role must be INVALID so an admin who gets
#: past the floor is stopped at INVITABLE_ROLES instead of sending real mail.
_INVITE_PROBE_EMAIL = "role-matrix-probe@occupella.test"
_INVITE_PROBE_ROLE = "not-a-real-role"


@dataclass
class Check:
    name: str
    role: str
    expected: str
    actual: str = ""
    passed: bool = False
    detail: str = ""
    #: NOT PROVEN. A check that could not run is neither a pass nor a fail, and
    #: rendering it as either is the false-positive class this file exists to
    #: catch -- hit twice already: an admin invite that "passed" on a 422 from
    #: body validation, and a member decline that "passed" on a 404 from a row
    #: lookup. Skipped checks print separately and never make a summary green.
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "expected": self.expected,
            "actual": self.actual,
            "passed": self.passed,
            "skipped": self.skipped,
            "detail": self.detail[:300],
        }


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, c: Check) -> Check:
        self.checks.append(c)
        flag = "SKIP" if c.skipped else ("PASS" if c.passed else "FAIL")
        print(f"  [{flag}] {c.role:<8} {c.name:<38} -> {c.actual}")
        return c

    def render(self) -> str:
        ran = [c for c in self.checks if not c.skipped]
        bad = [c for c in ran if not c.passed]
        skipped = [c for c in self.checks if c.skipped]
        lines = ["", "=" * 68]
        if bad:
            lines.append("FAILED " + str(len(bad)) + " of " + str(len(ran)) + " that RAN")
            for c in bad:
                lines.append("  " + c.role.ljust(8) + " " + c.name)
                lines.append("      expected " + c.expected + ", got " + c.actual)
                if c.detail:
                    lines.append("      " + c.detail[:200])
        elif ran:
            lines.append("all " + str(len(ran)) + " check(s) that RAN passed")
        else:
            lines.append("NOTHING RAN")
        if skipped:
            # Never fold these into the headline. "All passed" printed beside a
            # silent skip is how a gate nobody tested reads as a gate that works.
            lines.append("")
            lines.append("NOT PROVEN -- " + str(len(skipped)) + " check(s) SKIPPED:")
            for c in skipped:
                lines.append("  " + c.role.ljust(8) + " " + c.name + " -- " + c.actual)
        lines.append("=" * 68)
        return chr(10).join(lines)


# ── plumbing ──────────────────────────────────────────────────────────────


async def login(client: Any, sb_url: str, anon: str, email: str, password: str) -> str:
    r = await client.post(
        f"{sb_url.rstrip('/')}/auth/v1/token?grant_type=password",
        headers={"apikey": anon, "Content-Type": "application/json"},
        json={"email": email, "password": password},
    )
    if r.status_code != 200:
        raise SystemExit(f"login failed for {email}: {r.status_code} {r.text[:200]}")
    return str(r.json()["access_token"])


#: Not an HTTP status. A stream that dies mid-read produced NO verdict, and the
#: distinction is load-bearing: a drop and a role refusal are different facts,
#: but an empty answer looks like a refusal to any check that only reads text.
#: Same defect class as a 422 from body validation -- a plausible-looking result
#: from code that never reached the gate.
TRANSPORT_FAILED = -1


async def ask(
    client: Any, base: str, token: str, company_id: str, task: str
) -> tuple[str, list[str], int]:
    """Run one agent turn. Returns (text, event_types, http_status).

    Never approves anything: the stream is consumed and abandoned, so a
    confirm-gated tool times out harmlessly at the server's 300s gate rather
    than executing.
    """
    text: list[str] = []
    events: list[str] = []
    try:
        return await _ask_stream(client, base, token, company_id, task, text, events)
    except Exception as exc:  # noqa: BLE001 -- reported as its own outcome
        return (f"{type(exc).__name__}: {exc}", events, TRANSPORT_FAILED)


async def _ask_stream(
    client: Any,
    base: str,
    token: str,
    company_id: str,
    task: str,
    text: list[str],
    events: list[str],
) -> tuple[str, list[str], int]:
    async with client.stream(
        "POST",
        f"{base.rstrip('/')}/api/v1/agent/run",
        headers={"Authorization": f"Bearer {token}"},
        json={"task": task, "company_id": company_id, "source": "role_matrix"},
    ) as resp:
        if resp.status_code != 200:
            await resp.aread()
            return (resp.text[:400], [], resp.status_code)
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            body = line[6:].strip()
            if body == "[DONE]":
                break
            try:
                evt = json.loads(body)
            except json.JSONDecodeError:
                continue
            etype = str(evt.get("type", ""))
            events.append(etype)
            if etype in ("assistant_message_token", "token"):
                text.append(str(evt.get("data", {}).get("content", "")))
            if etype == "confirm_gate":
                break  # got what we came for; never approve
    return ("".join(text), events, 200)


async def ask_for_confirmation(
    client: Any, base: str, token: str, company_id: str, task: str
) -> tuple[str, list[str], str]:
    """Like ``ask``, but returns the live ``confirm_id`` when one is offered.

    ``confirm_gate`` carries only presentation fields (summary, labels); the id
    lives on the ``confirm`` event. Both are consumed here, and the stream is
    abandoned as soon as an id is in hand. Nothing is approved.
    """
    text: list[str] = []
    events: list[str] = []
    try:
        return await _confirm_stream(client, base, token, company_id, task, text, events)
    except Exception as exc:  # noqa: BLE001 -- reported as its own outcome
        return (f"TRANSPORT {type(exc).__name__}: {exc}", events, "")


async def _confirm_stream(
    client: Any,
    base: str,
    token: str,
    company_id: str,
    task: str,
    text: list[str],
    events: list[str],
) -> tuple[str, list[str], str]:
    confirm_id = ""
    async with client.stream(
        "POST",
        f"{base.rstrip('/')}/api/v1/agent/run",
        headers={"Authorization": f"Bearer {token}"},
        json={"task": task, "company_id": company_id, "source": "role_matrix"},
    ) as resp:
        if resp.status_code != 200:
            await resp.aread()
            return (resp.text[:400], [], "")
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            body = line[6:].strip()
            if body == "[DONE]":
                break
            try:
                evt = json.loads(body)
            except json.JSONDecodeError:
                continue
            etype = str(evt.get("type", ""))
            events.append(etype)
            data = evt.get("data") or {}
            if etype in ("assistant_message_token", "token"):
                text.append(str(data.get("content", "")))
            # The id rides on ``confirm_gate`` (orchestrator.py, PR #75 -- the
            # frontend posts it back to /agent/confirm). Nothing in the codebase
            # publishes the ``confirm`` event whose constructor also carries one,
            # so watching only for that name found an id that is never sent.
            if etype in ("confirm_gate", "confirm") and data.get("confirm_id"):
                confirm_id = str(data["confirm_id"])
                break
    return ("".join(text), events, confirm_id)


def _refused_on_role(text: str) -> bool:
    return bool(_REFUSAL_RE.search(text or ""))


# ── the matrix ────────────────────────────────────────────────────────────


async def check_read_parity(client, base, tokens, company_id, rep: Report) -> None:
    """A member who can see NOTHING is as broken as one who can see too much.
    RLS has only ever been exercised cross-company; this is two users inside
    one company, which is what every real customer looks like."""
    print("\n▸ read parity — every role sees the same company")
    for role, token in tokens.items():
        text, _, status = await ask(
            client, base, token, company_id, "How many properties do I have?"
        )
        if status == TRANSPORT_FAILED:
            rep.add(
                Check(
                    "sees the portfolio (50 properties)",
                    role,
                    "50",
                    "STREAM DROPPED -- no verdict",
                    False,
                    text[:200],
                    skipped=True,
                )
            )
            continue
        ok = status == 200 and "50" in text
        rep.add(
            Check(
                "sees the portfolio (50 properties)",
                role,
                "50",
                f"http {status}, {'found 50' if '50' in text else 'no 50 in reply'}",
                ok,
                text[:200],
            )
        )


async def check_money_floors(client, base, tokens, company_id, rep: Report) -> None:
    """BOTH directions on all three Manager+ money writes."""
    print("\n▸ Manager+ floors — member refused, manager/admin reach the gate")
    for tool, task in GATED_ASKS.items():
        for role, token in tokens.items():
            text, events, status = await ask(client, base, token, company_id, task)
            if status == TRANSPORT_FAILED:
                # NOT a refusal. A dropped stream yields no text, and "no
                # refusal marker" would otherwise read as the member sailing
                # past the floor -- inventing a security finding out of a
                # network error.
                rep.add(
                    Check(
                        tool,
                        role,
                        "a verdict",
                        "STREAM DROPPED",
                        False,
                        text[:200],
                        skipped=True,
                    )
                )
                continue
            refused = _refused_on_role(text)
            gated = "confirm_gate" in events
            if role == "member":
                ok, exp = (refused and not gated), "refused on role, no gate"
                actual = "refused" if refused else ("REACHED GATE" if gated else "no refusal")
            else:
                ok, exp = (gated and not refused), "reaches confirm gate"
                actual = "gate shown" if gated else ("REFUSED" if refused else "no gate")
            rep.add(Check(tool, role, exp, actual, ok, text[:200]))


async def check_ungated_write(client, base, tokens, company_id, rep: Report) -> None:
    """``record_move_out`` is confirm-only by design because it is reversible.
    A member must still reach its gate — a floor leaking onto it would look
    like extra safety and is actually a regression."""
    print("\n▸ ungated write — a member still reaches the move-out gate")
    text, events, status = await ask(client, base, tokens["member"], company_id, UNGATED_ASK)
    if status == TRANSPORT_FAILED:
        rep.add(
            Check(
                "record_move_out (no role floor)",
                "member",
                "reaches confirm gate",
                "STREAM DROPPED -- no verdict",
                False,
                text[:200],
                skipped=True,
            )
        )
        return
    gated = "confirm_gate" in events
    rep.add(
        Check(
            "record_move_out (no role floor)",
            "member",
            "reaches confirm gate",
            "gate shown" if gated else ("REFUSED ON ROLE" if _refused_on_role(text) else "no gate"),
            gated,
            text[:200],
        )
    )


async def check_route_gates(client, base, tokens, company_id, rep: Report) -> None:
    """Route-level floors, straight HTTP -- no model in the loop, so these are
    deterministic and the cheapest signal in the file.

    The invite probe carries a VALID email and an INVALID role. That ordering is
    the whole point: ``InviteRequest.email`` is a patterned field, so an empty
    email is rejected by Pydantic before ``invite_member`` is entered and
    ``require_role(..., ADMIN)`` never runs -- every role got the same 422 and
    the admin row "passed" because 422 is not 401/403. A valid email reaches the
    floor; the bogus role is then rejected at ``INVITABLE_ROLES``, which sits
    AFTER it. So 403 means the floor fired and 422 means it let the caller
    through -- and no invitation is ever created on either path.
    """
    print("")
    print("> route gates -- /company and /buildium")
    for role, token in tokens.items():
        r = await client.get(
            f"{base.rstrip('/')}/api/v1/company/members",
            headers={"Authorization": f"Bearer {token}"},
        )
        want = 403 if role == "member" else 200
        rep.add(
            Check(
                "GET /company/members",
                role,
                f"http {want}",
                f"http {r.status_code}",
                r.status_code == want,
                r.text[:200],
            )
        )

    probe = {"email": _INVITE_PROBE_EMAIL, "role": _INVITE_PROBE_ROLE}
    for role, token in tokens.items():
        r = await client.post(
            f"{base.rstrip('/')}/api/v1/company/invite",
            headers={"Authorization": f"Bearer {token}"},
            json=probe,
        )
        if role == "admin":
            # Allow-side: the floor must NOT block an admin. Reaching the role
            # validator (422) or succeeding outright both mean it let us past.
            ok = r.status_code not in (401, 403)
            exp = "not 401/403 (floor lets admin past)"
        else:
            ok = r.status_code == 403
            exp = "http 403 (floor blocks)"
        rep.add(Check("POST /company/invite", role, exp, f"http {r.status_code}", ok, r.text[:200]))
    if "admin" not in tokens:
        rep.add(
            Check(
                "POST /company/invite",
                "admin",
                "not 401/403 (floor lets admin past)",
                "no admin token -- pass --with-admin to prove the allow side",
                False,
                skipped=True,
            )
        )


async def check_approval_asymmetry(client, base, tokens, company_id, rep: Report) -> None:
    """Approve takes Manager+; DECLINE stays open to any member, because
    blocking someone from stopping an action would be backwards.

    ⚠ This needs a REAL confirm_id. The original version used a bogus one and
    proved nothing: ``agent.py`` looks the row up and raises 404 BEFORE it
    consults ``body.approved``, so both directions returned the same 404 from
    the same line and the decline row rendered as a pass. The role floor was
    never reached in either direction.

    So the id is borrowed from ``record_move_out`` -- deliberately ungated
    (reversible, gotcha 22), so a member can legitimately open it. If that gate
    does NOT open, both checks are SKIPPED rather than reported: without a live
    confirmation there is nothing to authorise and any status we got back would
    be measuring the harness.

    Approve is tested FIRST: a decline resolves the confirmation, and testing
    it first would leave the approve probe with a dead id -- 404 again, the
    original bug wearing the fix's clothes.
    """
    print("")
    print("> approval asymmetry -- member may decline, may not approve")
    member = tokens["member"]
    text, events, confirm_id = await ask_for_confirmation(
        client, base, member, company_id, UNGATED_ASK
    )
    if not confirm_id:
        why = (
            "move-out gate did not yield a confirm_id"
            + (" (REFUSED ON ROLE)" if _refused_on_role(text) else "")
            + (" [gate seen: " + ("yes" if "confirm_gate" in events else "no") + "]")
        )
        for label in ("member approves", "member declines"):
            rep.add(Check(label, "member", "needs a live confirm_id", why, False, skipped=True))
        return
    rep.add(
        Check(
            "move-out gate is open to a member",
            "member",
            "a live confirm_id",
            "got one",
            True,
            confirm_id,
        )
    )

    for approved, want, why in (
        (True, 403, "blocked by role floor"),
        (False, 200, "role OK, decline proceeds"),
    ):
        r = await client.post(
            f"{base.rstrip('/')}/api/v1/agent/confirm",
            headers={"Authorization": f"Bearer {member}"},
            json={"confirm_id": confirm_id, "approved": approved},
        )
        label = "member approves" if approved else "member declines"
        rep.add(
            Check(
                label,
                "member",
                f"http {want} ({why})",
                f"http {r.status_code}",
                r.status_code == want,
                r.text[:200],
            )
        )


async def check_cross_tenant(client, base, tokens, rep: Report) -> None:
    """A valid JWT for company A must not reach company B. The 42x golden
    cases cover the model's behaviour; this covers the HTTP boundary."""
    print("\n▸ cross-tenant — a valid token for another company is refused")
    other = "5ca1ab1e-0000-4000-8000-000000000004"  # "My Company"
    for role, token in tokens.items():
        _, _, status = await ask(client, base, token, other, "How many properties do I have?")
        ok = status in (401, 403, 404)
        rep.add(Check("foreign company_id refused", role, "401/403/404", f"http {status}", ok))


#: Which role tokens each group actually exercises. Keep in step with the check
#: functions -- listing a group against a role it does not use reinstates the
#: coupling this map exists to remove.
_ROLES_FOR: dict[str, tuple[str, ...]] = {
    "routes": ("member", "manager", "admin"),
    "approval": ("member",),
    "crosstenant": ("member", "manager", "admin"),
    "reads": ("member", "manager", "admin"),
    "floors": ("member", "manager", "admin"),
    "ungated": ("member",),
}


async def _admin_token(client, sb: str, anon: str) -> str:
    """The admin credential, by whichever route is available.

    A pre-issued token is accepted so the admin's PASSWORD never has to be
    handed to this process (or typed into someone else's terminal) just to
    prove that admins are allowed through. Same token the battery harness
    already uses via HELIXIS_JWT_FILE.
    """
    tok = os.environ.get("OCCUPELLA_ADMIN_TOKEN", "").strip()
    if tok:
        return tok
    f = os.environ.get("OCCUPELLA_ADMIN_TOKEN_FILE", "").strip()
    if f:
        return Path(f).read_text(encoding="utf-8").strip()
    return await login(
        client,
        sb,
        anon,
        os.environ["OCCUPELLA_ADMIN_EMAIL"],
        os.environ["OCCUPELLA_ADMIN_PASSWORD"],
    )


async def main_async(only: str | None, out: str | None, with_admin: bool) -> int:
    import httpx

    base = os.environ.get("HELIXIS_BATTERY_BASE_URL") or _DEFAULT_BASE
    sb = os.environ.get("HELIXIS_SUPABASE_URL", "https://abcdefghijklmnopqrst.supabase.co")
    anon = os.environ["HELIXIS_SUPABASE_ANON_KEY"]
    company_id = os.environ["HELIXIS_BATTERY_COMPANY_ID"]
    test_pw = os.environ["OCCUPELLA_TEST_PASSWORD"]

    rep = Report()
    async with httpx.AsyncClient(timeout=400.0) as client:
        run = lambda n: only is None or only == n  # noqa: E731

        # Log in ONLY the roles the selected groups actually use. Live-found
        # 2026-08-23: an eager three-role login meant two unusable test
        # accounts took down --only approval, which uses the member token
        # alone and would have run fine. A broken account must not mask a
        # check that never needed it.
        needed: set[str] = set()
        for group, roles in _ROLES_FOR.items():
            if run(group):
                needed |= set(roles)
        # Manager and admin sit on the same side of every gate except
        # POST /company/invite, so the default run needs no admin password and
        # no expiring admin token. The one allow-side that genuinely needs an
        # admin is reported SKIPPED -- never passed.
        if not with_admin:
            needed.discard("admin")
        print("roles needed: " + (", ".join(sorted(needed)) or "none"))
        tokens: dict[str, str] = {}
        for role in ("member", "manager", "admin"):
            if role not in needed:
                continue
            if role == "admin":
                tokens[role] = await _admin_token(client, sb, anon)
            else:
                email = MEMBER_EMAIL if role == "member" else MANAGER_EMAIL
                tokens[role] = await login(client, sb, anon, email, test_pw)
        print("got " + str(len(tokens)) + " token(s)")
        if run("routes"):
            await check_route_gates(client, base, tokens, company_id, rep)
        if run("approval"):
            await check_approval_asymmetry(client, base, tokens, company_id, rep)
        if run("crosstenant"):
            await check_cross_tenant(client, base, tokens, rep)
        if run("reads"):
            await check_read_parity(client, base, tokens, company_id, rep)
        if run("floors"):
            await check_money_floors(client, base, tokens, company_id, rep)
        if run("ungated"):
            await check_ungated_write(client, base, tokens, company_id, rep)

    print(rep.render())
    if out:
        with open(out, "w") as fh:
            json.dump([c.to_dict() for c in rep.checks], fh, indent=2)
        print(f"wrote {out}")
    # Three outcomes, three codes. A SKIP is not a failure and must not be
    # silently green either: exiting 0 on "not proven" is the same
    # false-positive this file keeps catching, one level up in the CI report.
    if any(not c.passed and not c.skipped for c in rep.checks):
        return 1
    if any(c.skipped for c in rep.checks):
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Role-gate matrix against the deployed backend.")
    p.add_argument(
        "--only",
        choices=["routes", "approval", "crosstenant", "reads", "floors", "ungated"],
        help="run one group (routes/approval/crosstenant are fast and model-free)",
    )
    p.add_argument("--json", dest="out", help="write machine-readable results here")
    p.add_argument(
        "--with-admin",
        action="store_true",
        help=(
            "also authenticate the admin (OCCUPELLA_ADMIN_TOKEN[_FILE] or password). "
            "Manager and admin sit on the same side of every gate except "
            "POST /company/invite, so the default member+manager run covers all but "
            "that one allow-side -- which is reported SKIPPED, never passed."
        ),
    )
    args = p.parse_args(argv)
    missing = [
        v
        for v in (
            "HELIXIS_SUPABASE_ANON_KEY",
            "HELIXIS_BATTERY_COMPANY_ID",
            "OCCUPELLA_TEST_PASSWORD",
        )
        if not os.environ.get(v)
    ]
    # The admin credential may arrive as a password OR a pre-issued token.
    if args.with_admin and not any(
        os.environ.get(v)
        for v in (
            "OCCUPELLA_ADMIN_TOKEN",
            "OCCUPELLA_ADMIN_TOKEN_FILE",
            "OCCUPELLA_ADMIN_PASSWORD",
        )
    ):
        missing.append("OCCUPELLA_ADMIN_PASSWORD (or OCCUPELLA_ADMIN_TOKEN[_FILE])")
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)}")
    return asyncio.run(main_async(args.only, args.out, args.with_admin))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
