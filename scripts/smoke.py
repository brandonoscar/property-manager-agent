#!/usr/bin/env python3
"""End-to-end smoke test for the Helixis backend.

Hits each critical surface and reports per-check status. Designed to run
against a local backend (``make dev``) or any deployed environment via
``HELIXIS_SMOKE_BASE_URL``.

Severity model:
  REQUIRED endpoints — failure exits 1.
  SOFT endpoints     — failure logs as TODO and does not flip exit code.
                       Used for endpoints that don't exist yet (Phase 4
                       billing, Phase 2 memory REST surface, Phase 4
                       webhook test surface).

Usage:
    HELIXIS_SMOKE_BASE_URL=http://localhost:8000 \\
    HELIXIS_SMOKE_TOKEN=<service-role-jwt> \\
      python3 scripts/smoke.py

Exit codes:
    0 — every required check passed
    1 — at least one required check failed (or HELIXIS_SMOKE_TOKEN missing
        when a required check needs auth)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass

import httpx

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_COMPANY_ID = "demo-company"


@dataclass
class CheckResult:
    name: str
    method: str
    path: str
    status: int | None
    elapsed_ms: int
    ok: bool
    required: bool
    note: str = ""

    def render(self) -> str:
        icon = "✅" if self.ok else ("⚠️ " if not self.required else "❌")
        status_str = str(self.status) if self.status is not None else "—"
        suffix = f" {self.note}" if self.note else ""
        return f"{icon} {self.method} {self.path} {status_str} in {self.elapsed_ms}ms{suffix}"


# ─────────────────────────────────────────────────────────────────────
# Individual checks
# ─────────────────────────────────────────────────────────────────────


async def check_health(client: httpx.AsyncClient) -> CheckResult:
    started = time.perf_counter()
    path = "/api/v1/health"
    try:
        resp = await client.get(path)
        elapsed = int((time.perf_counter() - started) * 1000)
        body = (
            resp.json()
            if resp.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        ok = resp.status_code == 200 and body.get("status") == "ok"
        return CheckResult("health", "GET", path, resp.status_code, elapsed, ok, required=True)
    except Exception as e:  # noqa: BLE001
        elapsed = int((time.perf_counter() - started) * 1000)
        return CheckResult(
            "health", "GET", path, None, elapsed, False, required=True, note=f"({e})"
        )


async def check_health_ready(client: httpx.AsyncClient) -> CheckResult:
    started = time.perf_counter()
    path = "/api/v1/health/ready"
    try:
        resp = await client.get(path)
        elapsed = int((time.perf_counter() - started) * 1000)
        return CheckResult(
            "health/ready",
            "GET",
            path,
            resp.status_code,
            elapsed,
            resp.status_code == 200,
            required=True,
        )
    except Exception as e:  # noqa: BLE001
        elapsed = int((time.perf_counter() - started) * 1000)
        return CheckResult(
            "health/ready", "GET", path, None, elapsed, False, required=True, note=f"({e})"
        )


async def check_agent_run(client: httpx.AsyncClient, *, token: str, company_id: str) -> CheckResult:
    """POST /api/v1/agent/run with a deterministic prompt.

    Sends the request in stream-mode and inspects the response status
    only. We deliberately do not consume the body — the agent run can
    take many seconds and a smoke test should stay fast. The raw
    ``send(stream=True)`` + immediate ``aclose`` pattern avoids httpx's
    ``client.stream()`` context-manager assertions, which trip up
    MockTransport in our unit tests.
    """
    started = time.perf_counter()
    path = "/api/v1/agent/run"
    request = client.build_request(
        "POST",
        path,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "task": "list open work orders",
            "company_id": company_id,
        },
        timeout=30.0,
    )
    resp: httpx.Response | None = None
    try:
        resp = await client.send(request, stream=True)
        elapsed = int((time.perf_counter() - started) * 1000)
        ok = 200 <= resp.status_code < 300
        return CheckResult(
            "agent/run",
            "POST",
            path,
            resp.status_code,
            elapsed,
            ok,
            required=True,
        )
    except Exception as e:  # noqa: BLE001
        elapsed = int((time.perf_counter() - started) * 1000)
        return CheckResult(
            "agent/run",
            "POST",
            path,
            None,
            elapsed,
            False,
            required=True,
            note=f"({type(e).__name__}: {e})",
        )
    finally:
        if resp is not None:
            await resp.aclose()


async def check_optional_endpoint(
    client: httpx.AsyncClient,
    *,
    name: str,
    method: str,
    path: str,
    json_body: dict | None = None,
    headers: dict | None = None,
    todo_phase: str,
) -> CheckResult:
    """Soft check for endpoints that may not exist yet.

    A 404 is recorded but doesn't flip the exit code — it logs as a
    TODO for the phase that's expected to ship the surface.
    """
    started = time.perf_counter()
    try:
        resp = await client.request(method, path, json=json_body, headers=headers, timeout=10.0)
        elapsed = int((time.perf_counter() - started) * 1000)
        if 200 <= resp.status_code < 300:
            return CheckResult(name, method, path, resp.status_code, elapsed, True, required=False)
        if resp.status_code == 404:
            return CheckResult(
                name,
                method,
                path,
                404,
                elapsed,
                True,
                required=False,
                note=f"(soft-fail: TODO {todo_phase})",
            )
        return CheckResult(
            name,
            method,
            path,
            resp.status_code,
            elapsed,
            False,
            required=False,
        )
    except Exception as e:  # noqa: BLE001
        elapsed = int((time.perf_counter() - started) * 1000)
        return CheckResult(name, method, path, None, elapsed, False, required=False, note=f"({e})")


# ─────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────


async def run_smoke(*, base_url: str, token: str | None, company_id: str) -> int:
    if not token:
        sys.stderr.write(
            "smoke: HELIXIS_SMOKE_TOKEN is required (service-role JWT for the demo company).\n"
            "       Set it and re-run.\n"
        )
        return 1

    started = time.perf_counter()
    async with httpx.AsyncClient(base_url=base_url) as client:
        results = []
        results.append(await check_health(client))
        results.append(await check_health_ready(client))
        results.append(await check_agent_run(client, token=token, company_id=company_id))
        # Soft checks for endpoints that don't exist yet on main.
        results.append(
            await check_optional_endpoint(
                client,
                name="webhooks/test",
                method="POST",
                path="/api/v1/webhooks/test",
                json_body={"event": "demo"},
                headers={"Authorization": f"Bearer {token}"},
                todo_phase="Phase 4 (webhook test surface)",
            )
        )
        results.append(
            await check_optional_endpoint(
                client,
                name="memory/search",
                method="POST",
                path="/api/v1/memory/search",
                json_body={"query": "vendor", "company_id": company_id},
                headers={"Authorization": f"Bearer {token}"},
                todo_phase="Phase 2 (memory REST surface)",
            )
        )
        results.append(
            await check_optional_endpoint(
                client,
                name="billing/usage",
                method="GET",
                path="/api/v1/billing/usage",
                headers={"Authorization": f"Bearer {token}"},
                todo_phase="Phase 4 (budgets + billing)",
            )
        )

    total_ms = int((time.perf_counter() - started) * 1000)
    for r in results:
        print(r.render())
    print(f"\nfinished in {total_ms}ms — {sum(1 for r in results if r.ok)}/{len(results)} ok")
    failed_required = [r for r in results if r.required and not r.ok]
    return 0 if not failed_required else 1


def main(argv: list[str] | None = None) -> int:
    base_url = os.environ.get("HELIXIS_SMOKE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    token = os.environ.get("HELIXIS_SMOKE_TOKEN")
    company_id = os.environ.get("HELIXIS_SMOKE_COMPANY_ID", DEFAULT_COMPANY_ID)
    return asyncio.run(run_smoke(base_url=base_url, token=token, company_id=company_id))


if __name__ == "__main__":
    sys.exit(main())
