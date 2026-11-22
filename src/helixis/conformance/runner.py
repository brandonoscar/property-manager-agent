"""Orchestrates the ADR 0003 conformance probes against a deployed backend.

The ``surface`` phase (CORS preflight + unauthenticated endpoint surface) is
read-only and runs unconditionally. The ``authenticated`` phase runs only when
a Supabase JWT is supplied (``token``); without one it's reported as skipped.
"""

from __future__ import annotations

import httpx

from helixis.conformance import checks as _checks
from helixis.conformance.checks import ProbeResult
from helixis.conformance.output import format_json, format_pretty

DEFAULT_BASE_URL = "https://your-api.onrender.com"
_TIMEOUT_SECONDS = 15.0


async def run_conformance(
    *,
    base_url: str = DEFAULT_BASE_URL,
    origin: str = _checks.DEFAULT_WIZARD_ORIGIN,
    token: str | None = None,
    json_output: bool = False,
    credentialed: bool = False,
    buildium_client_id: str | None = None,
    buildium_client_secret: str | None = None,
    buildium_environment: str = "sandbox",
) -> bool:
    """Run all probes against ``base_url``; print results; return overall pass.

    Returns ``True`` when no probe failed (``warn``/``skipped`` don't fail the
    run). A network error on every probe (e.g. egress-blocked host) surfaces as
    failures with a hint to allowlist the host.

    When ``credentialed`` is set, ALSO runs the state-mutating Buildium
    provisioning probe (the P0 launch gate) — it needs ``token`` plus a Buildium
    ``client_id``/``client_secret`` and writes state, so it's opt-in.
    """
    base_url = base_url.rstrip("/")
    results: list[ProbeResult] = []

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=False) as client:
        # Probe the requested origin plus every other contract CLIENT origin —
        # the wizard and the web app both, and through the occupella.com
        # domain swap every one of them must pass preflight independently.
        cors_origins = [origin] + [o for o in _checks.CONTRACT_CLIENT_ORIGINS if o != origin]
        for cors_origin in cors_origins:
            result = await _checks.probe_cors(client, base_url, cors_origin)
            if cors_origin != origin:
                result = ProbeResult(
                    f"cors.preflight[{cors_origin}]",
                    result.status,
                    result.detail,
                    result.http_status,
                    result.latency_ms,
                )
            results.append(result)
        for name, method, path in _checks.CONTRACT_ENDPOINTS:
            results.append(await _checks.probe_endpoint(client, base_url, name, method, path))

        if token:
            results.append(await _checks.probe_bootstrap_authenticated(client, base_url, token))
        else:
            results.append(
                ProbeResult(
                    "auth.bootstrap.authenticated",
                    "skipped",
                    "no --token / HELIXIS_CONFORMANCE_TOKEN supplied",
                )
            )

        if credentialed:
            if not token:
                results.append(
                    ProbeResult(
                        "buildium.provisioning",
                        "fail",
                        "--credentialed needs --token (a helixis-test Supabase JWT)",
                    )
                )
            elif not (buildium_client_id and buildium_client_secret):
                results.append(
                    ProbeResult(
                        "buildium.provisioning",
                        "fail",
                        "--credentialed needs a Buildium client_id + client_secret "
                        "(HELIXIS_BUILDIUM__CLIENT_ID / HELIXIS_BUILDIUM__CLIENT_SECRET)",
                    )
                )
            else:
                results.extend(
                    await _checks.probe_buildium_credentialed(
                        client,
                        base_url,
                        token,
                        client_id=buildium_client_id,
                        client_secret=buildium_client_secret,
                        environment=buildium_environment,
                    )
                )
        else:
            results.append(
                ProbeResult(
                    "buildium.provisioning",
                    "skipped",
                    "credentialed provisioning probe off (pass --credentialed to run it)",
                )
            )

    passed = not any(r.failed for r in results)
    print(
        format_json(base_url, passed, results)
        if json_output
        else format_pretty(base_url, passed, results)
    )
    return passed
