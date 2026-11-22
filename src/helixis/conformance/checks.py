"""Conformance probes for the ADR 0003 client↔backend contract.

These verify that the deployed backend exposes the exact API surface the
client repos (helixis-onboarding, helixis-sidebar) consume, and that CORS
allows the onboarding wizard's browser origin to reach it.

Two phases:

* **surface** (no credentials) — a CORS preflight from the wizard origin
  plus an unauthenticated request to every contract endpoint. We assert the
  route EXISTS and enforces auth: ``401``/``403`` (and ``422``, when body
  validation is reached) prove it's wired; ``404`` means the endpoint is
  missing and ``405`` means the method drifted from the contract. This phase
  is read-only and safe to run against production repeatedly.
* **authenticated** (optional, ``--token``) — calls the idempotent
  ``POST /auth/bootstrap`` with a real Supabase JWT and asserts it returns a
  ``company_id``.
* **credentialed** (opt-in, ``--credentialed`` + a Buildium key) — the
  state-MUTATING provisioning happy-path a real signup walks: save creds →
  ``POST /buildium/test`` → ``ok:true`` → read back that it persisted. This is
  the P0 launch gate that the sandbox env-var tooling bypasses (it never stores
  per-company creds nor triggers the account_id backfill). Off by default
  because it writes state and needs a real (sandbox) Buildium key; run it
  against helixis-test. See ``docs/operations/conformance-check.md``.

The pure classifiers (:func:`classify_endpoint_probe`, :func:`classify_cors`)
hold the decision logic so they're unit-testable without a network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

# The ADR 0003 contract surface, all under ``/api/v1``. ``(name, method,
# path)``. Every one of these is auth-protected on the backend, so an
# unauthenticated probe should be rejected (not 404/405).
CONTRACT_ENDPOINTS: list[tuple[str, str, str]] = [
    ("auth.bootstrap", "POST", "/api/v1/auth/bootstrap"),
    ("buildium.credentials.get", "GET", "/api/v1/buildium/credentials"),
    ("buildium.credentials.put", "PUT", "/api/v1/buildium/credentials"),
    ("buildium.test", "POST", "/api/v1/buildium/test"),
    ("buildium.webhook_secret.put", "PUT", "/api/v1/buildium/webhook-secret"),
    ("rentvine.credentials.get", "GET", "/api/v1/rentvine/credentials"),
    ("rentvine.credentials.put", "PUT", "/api/v1/rentvine/credentials"),
    ("rentvine.test", "POST", "/api/v1/rentvine/test"),
    # The wizard's Rent Manager and Propertyware options call the same three
    # each (api/routes/rentmanager.py, api/routes/propertyware.py; the wizard's
    # side is helixis-onboarding src/lib/pms.ts). Every system in pms.registry
    # must be here: test_conformance reads both.
    ("rentmanager.credentials.get", "GET", "/api/v1/rentmanager/credentials"),
    ("rentmanager.credentials.put", "PUT", "/api/v1/rentmanager/credentials"),
    ("rentmanager.test", "POST", "/api/v1/rentmanager/test"),
    ("propertyware.credentials.get", "GET", "/api/v1/propertyware/credentials"),
    ("propertyware.credentials.put", "PUT", "/api/v1/propertyware/credentials"),
    ("propertyware.test", "POST", "/api/v1/propertyware/test"),
    # DoorLoop's three (api/routes/doorloop.py). Its wizard option is not built
    # yet; the routes are here because every registry system's must be.
    ("doorloop.credentials.get", "GET", "/api/v1/doorloop/credentials"),
    ("doorloop.credentials.put", "PUT", "/api/v1/doorloop/credentials"),
    ("doorloop.test", "POST", "/api/v1/doorloop/test"),
    ("reports.first_scan", "GET", "/api/v1/reports/first-scan"),
    ("connectors.list", "GET", "/api/v1/connectors/"),
    ("connectors.connect", "POST", "/api/v1/connectors/gmail/connect"),
    ("company.patch", "PATCH", "/api/v1/company"),
    ("company.invite", "POST", "/api/v1/company/invite"),
]

# Every deployed BROWSER origin under ADR 0003. CORS must echo these back or
# that client is blocked on every cross-origin call to the Render backend.
#
# ⚠ RENAMED from CONTRACT_WIZARD_ORIGINS 2026-09-15, and the name was the
# defect. It described its first caller — the onboarding wizard — so the web
# APP's own origin was never probed by the one routine that exists to catch a
# CORS break (`conformance.yml`, every 6h). That is gotcha 67's shape: a
# constant documented by naming who reached for it first, invisible to the
# second caller who needed it just as much.
#
# ⚠ `app.occupella.com` is listed before anything serves it, matching
# CONTRACT_ORIGINS in api/middleware.py — the apex and setup.* are trading
# places, the web app moves to app.*, and this probe is what proves the
# backend will answer it. The probe sends an Origin HEADER to our own backend
# and never resolves that host, so it works before DNS exists.
DEFAULT_WIZARD_ORIGIN = "https://your-onboarding.vercel.app"
CONTRACT_CLIENT_ORIGINS = (
    DEFAULT_WIZARD_ORIGIN,
    "https://setup.occupella.com",
    "https://app.occupella.com",
)


@dataclass
class ProbeResult:
    name: str
    status: str  # "pass" | "fail" | "warn" | "skipped"
    detail: str
    http_status: int | None = None
    latency_ms: int | None = None

    @property
    def failed(self) -> bool:
        return self.status == "fail"


def classify_endpoint_probe(method: str, status: int) -> tuple[str, str]:
    """Map an UNAUTHENTICATED probe's HTTP status to (verdict, detail).

    The contract endpoints are all auth-protected, so the goal is to prove
    the route is wired with the right method — not to authenticate.
    """
    if status == 404:
        return "fail", "404 — route not found (endpoint missing or path drifted)"
    if status == 405:
        return "fail", f"405 — method {method} not allowed (contract mismatch)"
    if status in (401, 403):
        return "pass", f"{status} — wired and auth-protected"
    if status == 422:
        # Body validation ran before/without the auth dependency. The route
        # exists with the right method; auth still applies to a real call.
        return "pass", "422 — wired (request validation reached)"
    if 200 <= status < 300:
        return "warn", f"{status} — reachable but NOT rejected unauthenticated; verify auth"
    if status >= 500:
        return "fail", f"{status} — server error"
    return "warn", f"{status} — unexpected status"


def classify_cors(allow_origin: str | None, expected_origin: str) -> tuple[str, str]:
    """Decide whether the preflight's Access-Control-Allow-Origin permits
    the wizard origin."""
    if not allow_origin:
        return "fail", f"no Access-Control-Allow-Origin — {expected_origin} would be CORS-blocked"
    if allow_origin in (expected_origin, "*"):
        return "pass", f"allows {expected_origin}"
    return "fail", f"allows {allow_origin!r}, not {expected_origin!r}"


async def probe_cors(
    client: httpx.AsyncClient, base_url: str, origin: str = DEFAULT_WIZARD_ORIGIN
) -> ProbeResult:
    """Send a CORS preflight from ``origin`` for the bootstrap endpoint and
    check the allow-origin header. Exercises the #299 fix directly."""
    started = time.perf_counter()
    try:
        resp = await client.options(
            f"{base_url}/api/v1/auth/bootstrap",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
    except httpx.HTTPError as exc:
        return ProbeResult("cors.preflight", "fail", _net_detail(exc))
    if _is_egress_block(resp):
        return ProbeResult("cors.preflight", "fail", _egress_detail(base_url), resp.status_code)
    allow = resp.headers.get("access-control-allow-origin")
    verdict, detail = classify_cors(allow, origin)
    return ProbeResult(
        "cors.preflight",
        verdict,
        detail,
        http_status=resp.status_code,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


async def probe_endpoint(
    client: httpx.AsyncClient, base_url: str, name: str, method: str, path: str
) -> ProbeResult:
    """Unauthenticated probe of one contract endpoint (no Bearer token)."""
    started = time.perf_counter()
    try:
        resp = await client.request(method, f"{base_url}{path}")
    except httpx.HTTPError as exc:
        return ProbeResult(name, "fail", _net_detail(exc))
    if _is_egress_block(resp):
        return ProbeResult(
            name, "fail", f"{method} {path} → {_egress_detail(base_url)}", resp.status_code
        )
    verdict, detail = classify_endpoint_probe(method, resp.status_code)
    return ProbeResult(
        name,
        verdict,
        f"{method} {path} → {detail}",
        http_status=resp.status_code,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


async def probe_bootstrap_authenticated(
    client: httpx.AsyncClient, base_url: str, token: str
) -> ProbeResult:
    """Call the idempotent bootstrap with a real Supabase JWT; expect a
    ``company_id`` in the response (proves auth + provisioning end to end)."""
    started = time.perf_counter()
    try:
        resp = await client.post(
            f"{base_url}/api/v1/auth/bootstrap",
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as exc:
        return ProbeResult("auth.bootstrap.authenticated", "fail", _net_detail(exc))
    latency = int((time.perf_counter() - started) * 1000)
    if _is_egress_block(resp):
        return ProbeResult(
            "auth.bootstrap.authenticated",
            "fail",
            _egress_detail(base_url),
            resp.status_code,
            latency,
        )
    if resp.status_code == 401:
        return ProbeResult(
            "auth.bootstrap.authenticated",
            "fail",
            "401 — token rejected (is it minted by the helixis-test Supabase project?)",
            http_status=401,
            latency_ms=latency,
        )
    if resp.status_code != 200:
        return ProbeResult(
            "auth.bootstrap.authenticated",
            "fail",
            f"{resp.status_code} — expected 200",
            http_status=resp.status_code,
            latency_ms=latency,
        )
    try:
        company_id = resp.json().get("company_id")
    except ValueError:
        company_id = None
    if not company_id:
        return ProbeResult(
            "auth.bootstrap.authenticated",
            "fail",
            "200 but no company_id in body",
            http_status=200,
            latency_ms=latency,
        )
    return ProbeResult(
        "auth.bootstrap.authenticated",
        "pass",
        f"200 — company_id={company_id}",
        http_status=200,
        latency_ms=latency,
    )


def classify_credentials_put(status: int, configured: bool | None) -> tuple[str, str]:
    """Verdict for the ``PUT /buildium/credentials`` step of the credentialed
    provisioning probe."""
    if status == 401:
        return "fail", "401 — token rejected (minted by the helixis-test project?)"
    if status != 200:
        return "fail", f"{status} — expected 200 storing credentials"
    if not configured:
        return "fail", "200 but credentials not marked configured"
    return "pass", "200 — credentials stored (configured)"


def classify_buildium_test(status: int, ok: bool | None, error: str | None) -> tuple[str, str]:
    """Verdict for ``POST /buildium/test``. ``ok:true`` is THE gate: it proves
    the stored creds authenticate against Buildium and triggers the server-side
    ``account_id`` backfill that webhook routing depends on."""
    if status == 401:
        return "fail", "401 — token rejected"
    if status != 200:
        return "fail", f"{status} — expected 200 from /buildium/test"
    if ok is not True:
        return "fail", f"ok:false — Buildium rejected the credentials: {error or 'no detail'}"
    return "pass", "ok:true — Buildium accepted the credentials"


def classify_tested_persisted(status: int, last_tested_at: str | None) -> tuple[str, str]:
    """Verdict for the read-back: ``GET /buildium/credentials`` must now show a
    ``last_tested_at`` — the observable proof that ``POST /buildium/test``'s
    state mutation (timestamp + ``account_id`` backfill) actually persisted.
    ``account_id`` itself isn't exposed by the API, so this timestamp is the
    proxy for the whole side effect landing."""
    if status != 200:
        return "warn", f"{status} — could not read back credential status to verify persistence"
    if not last_tested_at:
        return "fail", "last_tested_at still null — /buildium/test side effect did not persist"
    return (
        "pass",
        f"last_tested_at={last_tested_at} — provisioning persisted (account_id backfill ran)",
    )


def _safe_json(resp: httpx.Response) -> dict[str, object]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def probe_buildium_credentialed(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    *,
    client_id: str,
    client_secret: str,
    environment: str = "sandbox",
) -> list[ProbeResult]:
    """The state-MUTATING provisioning happy-path a real signup walks:
    ``PUT /buildium/credentials`` → ``POST /buildium/test`` → ``ok:true``, then
    a read-back that the mutation persisted. This is what the sandbox env-var
    tooling (synthetic_traffic) bypasses — the per-company Fernet cred storage +
    account_id backfill + last_tested_at only fire on THIS path.

    Returns one ProbeResult per step; short-circuits (skips later steps) once a
    step fails, so the first real failure is the reported cause.
    """
    headers = {"Authorization": f"Bearer {token}"}
    results: list[ProbeResult] = []

    # 1. Store credentials.
    started = time.perf_counter()
    try:
        resp = await client.put(
            f"{base_url}/api/v1/buildium/credentials",
            headers=headers,
            json={
                "client_id": client_id,
                "client_secret": client_secret,
                "environment": environment,
            },
        )
    except httpx.HTTPError as exc:
        return [ProbeResult("buildium.credentials.put.authenticated", "fail", _net_detail(exc))]
    if _is_egress_block(resp):
        return [
            ProbeResult(
                "buildium.credentials.put.authenticated",
                "fail",
                _egress_detail(base_url),
                resp.status_code,
            )
        ]
    verdict, detail = classify_credentials_put(resp.status_code, _safe_json(resp).get("configured"))  # type: ignore[arg-type]
    results.append(
        ProbeResult(
            "buildium.credentials.put.authenticated",
            verdict,
            detail,
            http_status=resp.status_code,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    )
    if verdict == "fail":
        return results

    # 2. Test the connection (the ok:true gate + account_id backfill).
    started = time.perf_counter()
    try:
        resp = await client.post(f"{base_url}/api/v1/buildium/test", headers=headers)
    except httpx.HTTPError as exc:
        results.append(ProbeResult("buildium.test.authenticated", "fail", _net_detail(exc)))
        return results
    if _is_egress_block(resp):
        results.append(
            ProbeResult(
                "buildium.test.authenticated", "fail", _egress_detail(base_url), resp.status_code
            )
        )
        return results
    body = _safe_json(resp)
    verdict, detail = classify_buildium_test(resp.status_code, body.get("ok"), body.get("error"))  # type: ignore[arg-type]
    results.append(
        ProbeResult(
            "buildium.test.authenticated",
            verdict,
            detail,
            http_status=resp.status_code,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    )
    if verdict == "fail":
        return results

    # 3. Read back that the mutation persisted.
    try:
        resp = await client.get(f"{base_url}/api/v1/buildium/credentials", headers=headers)
    except httpx.HTTPError as exc:
        results.append(ProbeResult("buildium.provisioning.persisted", "warn", _net_detail(exc)))
        return results
    if _is_egress_block(resp):
        results.append(
            ProbeResult(
                "buildium.provisioning.persisted",
                "warn",
                _egress_detail(base_url),
                resp.status_code,
            )
        )
        return results
    verdict, detail = classify_tested_persisted(
        resp.status_code,
        _safe_json(resp).get("last_tested_at"),  # type: ignore[arg-type]
    )
    results.append(
        ProbeResult(
            "buildium.provisioning.persisted", verdict, detail, http_status=resp.status_code
        )
    )
    return results


def _net_detail(exc: httpx.HTTPError) -> str:
    """A network failure detail with a hint for the common egress-block case."""
    msg = str(exc) or type(exc).__name__
    if "host_not_allowed" in msg or "not in allowlist" in msg:
        msg += " (add the host to this environment's network egress allowlist)"
    return f"network error: {msg}"


def _is_egress_block(resp: httpx.Response) -> bool:
    """True when a 403 came from a network-egress proxy, not the backend.

    A restricted environment (e.g. a remote agent container) answers a
    disallowed host with its own ``403`` carrying ``x-deny-reason`` — which
    would otherwise be misread as a backend ``403`` (auth-protected). We must
    treat it as "host unreachable", never as a contract verdict.
    """
    if resp.status_code != 403:
        return False
    if resp.headers.get("x-deny-reason"):
        return True
    return "not in allowlist" in (resp.text or "").lower()


def _egress_detail(base_url: str) -> str:
    return (
        f"blocked by network egress policy (host for {base_url} not allowlisted) — "
        "add it to this environment's egress settings to probe the live backend"
    )
