#!/usr/bin/env python3
"""Live end-to-end memory check: store a fact in one chat turn, recall it in a
second — against the DEPLOYED backend, driven entirely over the API so nobody
has to type into Helixis.

What it proves: the real `/agent/run` chat path stores a fact to Mem0 and
surfaces it on a later turn (the loop #324 fixed — embedder + spaCy SystemExit).
This is a SINGLE-USER store→recall: "My Company" has one teammate, so the
cross-EMPLOYEE guarantee can't be exercised live — that's covered at the memory
layer by tests/integration/test_memory_cross_employee_recall.py (#326).

Auth: mints a short-lived HS256 token for the user's OWN account, signed with
the same secret the backend verifies (`settings.auth.jwt_secret`). The secret is
read from the environment and never printed. The backend checks the token's
issuer against its own Supabase URL (`api/auth.py`), so the token names
`{HELIXIS_SUPABASE__URL}/auth/v1`. The project itself signs ES256 (measured
2026-11-11), so this works only while the backend still has
HELIXIS_AUTH__JWT_SECRET set; without it HS256 is off and this gets a 401.

Run (local session that has the keys):
    HELIXIS_AUTH__JWT_SECRET=...  \
    python scripts/verify_live_memory.py

Env (all optional except the secret):
    HELIXIS_AUTH__JWT_SECRET   required — the project JWT secret (HS256)
    HELIXIS_SUPABASE__URL      default https://abcdefghijklmnopqrst.supabase.co (the
                               token's issuer is this plus /auth/v1)
    HELIXIS_VERIFY_BASE_URL    backend base, default https://your-app.vercel.app
                               (point at the direct Render URL to avoid any
                                SSE buffering through the Vercel→Render proxy)
    HELIXIS_VERIFY_USER_ID     default 5ca1ab1e-0000-4000-8000-000000000005
    HELIXIS_VERIFY_COMPANY_ID  default 5ca1ab1e-0000-4000-8000-000000000004
    HELIXIS_VERIFY_EMAIL       default jane.q.public@gmail.com
    HELIXIS_VERIFY_NO_CLEANUP  set to keep the test memory (default: clean up)

Exit 0 = recalled. Exit 1 = not recalled / error.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

import httpx
import jwt

BASE_URL = os.environ.get("HELIXIS_VERIFY_BASE_URL", "https://your-app.vercel.app").rstrip(
    "/"
)
USER_ID = os.environ.get("HELIXIS_VERIFY_USER_ID", "5ca1ab1e-0000-4000-8000-000000000005")
COMPANY_ID = os.environ.get("HELIXIS_VERIFY_COMPANY_ID", "5ca1ab1e-0000-4000-8000-000000000004")
EMAIL = os.environ.get("HELIXIS_VERIFY_EMAIL", "jane.q.public@gmail.com")
RUN_PATH = "/api/v1/agent/run"
SUPABASE_URL = os.environ.get(
    "HELIXIS_SUPABASE__URL", "https://abcdefghijklmnopqrst.supabase.co"
).rstrip("/")

# A unique, Buildium-shaped fact. The phone number doubles as a PII-keep check —
# redact_for_memory() (#321) must KEEP phones/emails, only stripping SSN/CC/IBAN.
MARKER = uuid.uuid4().hex[:8]
VENDOR = "Acme Elevator"
WO = "5501"
PHONE = "555-0142"
STORE_TURN = (
    f"Please remember for the team (ref {MARKER}): the elevator maintenance "
    f"vendor for Maple Court is {VENDOR}, Buildium work order #{WO}, "
    f"their dispatcher is {PHONE}."
)
RECALL_TURN = (
    f"Who is the elevator maintenance vendor for Maple Court, and what is the "
    f"Buildium work order number? (looking for ref {MARKER})"
)


def _mint_token() -> str:
    secret = os.environ.get("HELIXIS_AUTH__JWT_SECRET", "")
    if not secret:
        print("ERROR: HELIXIS_AUTH__JWT_SECRET is not set (cannot mint a token).", file=sys.stderr)
        sys.exit(1)
    now = int(time.time())
    claims = {
        "sub": USER_ID,
        "email": EMAIL,
        "role": "authenticated",
        "aud": "authenticated",
        "iat": now - 120,  # backdate to absorb client/server clock skew (avoids 401 not-yet-valid)
        "exp": now + 600,  # 10 minutes — short-lived
        "iss": f"{SUPABASE_URL}/auth/v1",  # the backend refuses any other issuer
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def _run_turn(token: str, task: str, *, label: str) -> tuple[str, bool]:
    """POST one turn, read the SSE stream, return (reply_text, saw_memory_recall)."""
    body = {"task": task, "company_id": COMPANY_ID, "session_id": str(uuid.uuid4())}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    reply: list[str] = []
    saw_recall = False
    with httpx.Client(timeout=httpx.Timeout(120.0, read=120.0)) as client:
        with client.stream("POST", BASE_URL + RUN_PATH, json=body, headers=headers) as resp:
            if resp.status_code != 200:
                resp.read()
                print(f"  [{label}] HTTP {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
                return "", False
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    break
                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type")
                data = evt.get("data", {})
                if etype in ("token", "assistant_message_token"):
                    reply.append(data.get("content", ""))
                elif etype == "memory_recall":
                    saw_recall = True
                elif etype == "done":
                    break
                elif etype == "error":
                    print(f"  [{label}] error event: {data}", file=sys.stderr)
                    break
    return "".join(reply), saw_recall


def _cleanup() -> None:
    """Delete ONLY this run's test memory (targeted — never company-wide)."""
    import asyncio

    async def _go() -> None:
        from helixis.memory.manager import MemoryManager
        from helixis.settings import Settings

        mgr = MemoryManager(Settings())
        await mgr.initialize()
        if not mgr.available:
            return
        for mem in await mgr.get_all(company_id=COMPANY_ID):
            text = str(mem.get("memory", mem.get("content", "")))
            if MARKER in text or (VENDOR in text and WO in text):
                mid = mem.get("id")
                if mid:
                    await mgr.delete(str(mid))

    try:
        asyncio.run(_go())
    except Exception as e:  # noqa: BLE001
        print(f"  (cleanup skipped: {e})", file=sys.stderr)


def main() -> None:
    print(f"Backend: {BASE_URL}")
    print(f"Company: {COMPANY_ID}  User: {USER_ID}  Marker: {MARKER}")
    token = _mint_token()

    print("\n[1/2] STORE turn …")
    store_reply, _ = _run_turn(token, STORE_TURN, label="store")
    if not store_reply:
        print("FAIL: store turn produced no reply (auth/stream problem?).")
        sys.exit(1)
    print(f"  assistant: {store_reply[:160]!r}")

    # Mem0 extraction + embed is eventually consistent; give it a moment.
    print("\nwaiting 8s for extraction to settle …")
    time.sleep(8)

    print("\n[2/2] RECALL turn (new session) …")
    recall_reply, saw_recall = _run_turn(token, RECALL_TURN, label="recall")
    print(f"  memory_recall event: {saw_recall}")
    print(f"  assistant: {recall_reply[:300]!r}")

    hit = (VENDOR.lower() in recall_reply.lower()) or (WO in recall_reply)

    if not os.environ.get("HELIXIS_VERIFY_NO_CLEANUP"):
        print("\ncleaning up test memory …")
        _cleanup()

    print()
    if hit:
        print(f"PASS: recall surfaced the stored fact ({VENDOR} / #{WO}).")
        if saw_recall:
            print("      (memory_recall event fired — fact was injected from Mem0.)")
        sys.exit(0)
    print("FAIL: the recall turn did not surface the stored fact.")
    print("      Check: backend on post-#324 build? Mem0 initialised? memory.enabled?")
    sys.exit(1)


if __name__ == "__main__":
    main()
