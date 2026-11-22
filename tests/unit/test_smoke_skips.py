"""Every check that can SKIP must have said why, on purpose.

A skipped check does not fail the smoke, so a skip is the one status whose
failure mode is "silently permits more" — gotcha 45's rule. And it bit:
``check_memory`` returned ``skipped`` whenever ``HELIXIS_LLM__VOYAGE_API_KEY``
was absent, while ``config/default.yaml`` and ``config/production.yaml`` both
declare ``memory.enabled: true``. So the check written to stop memory rotting
silently (gotcha 17 — a 403 on the embedder ran 19 days undetected) went green
in exactly the dead-memory state it exists to alarm on, and the runner's own
comment asserted "Langfuse is the only one that opts into skip" while SEVEN
checks could.

⚠ This reads the CORPUS, not a function (gotcha 45). A test feeding
hand-written settings to ``check_memory`` proves that branch behaves as
written; only walking every skip site in the module shows which checks can
quietly stop running.

⚠ AST, not a substring scan (gotcha 40). The literal ``"skipped"`` appears in
the docstrings and comments that EXPLAIN each decision, so a grep flags the
prose justifying the code. A comment is never a ``Call`` node.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from helixis.settings import Settings
from helixis.smoke import checks as checks_mod
from helixis.smoke.checks import CheckResult, check_memory
from helixis.smoke.output import format_json, format_pretty

_SOURCE = pathlib.Path(checks_mod.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)

#: Checks that may legitimately return ``skipped``, each with the reason the
#: skip is honest rather than a green-forever hole. Adding a check here is the
#: deliberate act this file exists to force.
ALLOWED_SKIPS: dict[str, str] = {
    "composio_gmail": (
        "the smoke account may never have OAuth'd — a user-state issue, not a "
        "service outage; a CONNECTED account that degrades still fails"
    ),
    "composio_calendar": "same as composio_gmail",
    "composio_drive": "same as composio_gmail",
    "webhook_queue": (
        "no DB URL, or the queue read errored — supabase_postgres owns real DB "
        "health, so failing here would double-count one outage"
    ),
    "model_slots": (
        "no non-default slots configured, or no key for them — llm_router "
        "already covers the default model"
    ),
    "judge": (
        "the judge model's provider key is unset, which llm_router fails on "
        "first; skipping avoids reporting one outage twice"
    ),
    "langfuse": "optional observability — missing keys must not fail the smoke",
    "twilio_sms": "outbound SMS is dark until A2P carrier approval",
    "memory": (
        "memory switched OFF in settings has nothing to probe. ⚠ ENABLED with "
        "no embedder key is NOT in this category — that is a misconfiguration "
        "and fails; see test_enabled_memory_without_a_key_fails"
    ),
    "stripe": (
        "no HELIXIS_BILLING__STRIPE_SECRET_KEY, so there is no key, no price "
        "and no webhook endpoint to read — billing cannot take money at all, "
        "which the detail says in those words. ⚠ A key that IS present and "
        "fails is NOT in this category: a half-configured deployment is the "
        "dangerous state, and every branch past the key check FAILS"
    ),
    "billing_rows": (
        "no DB URL — supabase_postgres owns real DB health, so failing here "
        "would double-count one outage (the same reasoning as webhook_queue). "
        "⚠ A readable database with companies missing a billing row is NOT in "
        "this category and FAILS: gate.py serves a missing row unmetered "
        "forever (gotcha 81)"
    ),
    "buildium_api": (
        "the DEPLOYMENT-GLOBAL credential pair is absent, which is correct on a "
        "multi-tenant install: production opens every Buildium client through "
        "load_buildium_settings_for_company (per-company, Fernet-encrypted), so "
        "this pair is legacy single-tenant config nothing on the request path "
        "reads. ⚠ PRESENT-and-401 is NOT in this category — a configured "
        "credential that fails auth is a real break and FAILS; see "
        "test_a_configured_buildium_credential_that_401s_still_fails"
    ),
}


def _checks_that_can_skip() -> set[str]:
    """Every check name that constructs a ``CheckResult(..., "skipped", ...)``.

    Handles both call shapes the module uses — the first argument is either a
    string literal (``CheckResult("memory", "skipped", ...)``) or the ``name``
    parameter of a shared helper (``CheckResult(name, "skipped", ...)``), the
    latter being the Composio probe that serves three checks.
    """
    found: set[str] = set()
    for node in ast.walk(_TREE):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "CheckResult"
            and len(node.args) >= 2
        ):
            continue
        status = node.args[1]
        if not (isinstance(status, ast.Constant) and status.value == "skipped"):
            continue
        target = node.args[0]
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            found.add(target.value)
        elif isinstance(target, ast.Name):
            # A shared helper parameterised by check name — the Composio probe.
            # Credit every name the runner passes it rather than guessing.
            found.update({"composio_gmail", "composio_calendar", "composio_drive"})
        else:
            raise AssertionError(
                f"unrecognised skip target at line {node.lineno} — the scan cannot "
                "classify it, and an unclassifiable skip is exactly what this guards"
            )
    return found


def test_the_scan_finds_skip_sites_at_all() -> None:
    """A bad path or a refactor that renames ``CheckResult`` would collect zero
    and report green — the shape gotcha 49's encoding guard warns about."""
    assert len(_checks_that_can_skip()) >= 5


def test_no_check_skips_without_a_recorded_reason() -> None:
    can_skip = _checks_that_can_skip()
    undeclared = can_skip - ALLOWED_SKIPS.keys()
    assert not undeclared, (
        f"these checks can silently stop running with no stated justification: "
        f"{sorted(undeclared)}. A skip does not fail the smoke, so it is the one "
        f"status that can hide a dead dependency behind a green run."
    )


def test_the_allowlist_has_no_stale_entries() -> None:
    """Both directions (gotcha 38). An entry for a check that can no longer
    skip is a justification for a decision nobody is making any more, and it
    would silently re-permit the skip if the branch came back."""
    stale = ALLOWED_SKIPS.keys() - _checks_that_can_skip()
    assert not stale, f"allowlisted but no longer able to skip: {sorted(stale)}"


# ── the specific defect ──────────────────────────────────────────────


def _settings(*, memory_enabled: bool, voyage_key: str) -> Settings:
    s = Settings()
    s.memory.enabled = memory_enabled
    s.llm.voyage_api_key = voyage_key
    return s


@pytest.mark.asyncio
async def test_enabled_memory_without_a_key_fails() -> None:
    """The defect. Memory is on in every shipped config, so a missing embedder
    key means every recall returns ``[]`` through the by-design swallow in
    ``manager.search`` — dead memory, reported green."""
    result = await check_memory(_settings(memory_enabled=True, voyage_key=""))
    assert result.status == "fail", (
        "memory is enabled and unprovisioned — the daily smoke would go green "
        "on exactly the silent rot this check exists to catch"
    )
    assert "VOYAGE" in result.detail


@pytest.mark.asyncio
async def test_disabled_memory_still_skips() -> None:
    """The other direction. Memory switched OFF has nothing to probe, and
    failing on it would red the cron forever for a feature nobody enabled
    (gotcha 23) — which is how a real signal gets ignored."""
    result = await check_memory(_settings(memory_enabled=False, voyage_key=""))
    assert result.status == "skipped"


# ── the Buildium pair is legacy config, and the split has to hold ────


@pytest.mark.asyncio
async def test_absent_buildium_credentials_skip_rather_than_fail() -> None:
    """⚠ Measured 2026-09-05, and the measurement is the justification.

    Every production Buildium client opens through
    ``load_buildium_settings_for_company``. The only readers of
    ``settings.buildium.client_id`` are this check, ``worker.process_one`` (a
    replay helper with zero callers), a health-payload boolean and the eval
    harness — so on a white-label install the pair is correctly ABSENT, and
    failing on it reds the cron every morning for a credential the product does
    not use (gotcha 23's red-forever shape).
    """
    from helixis.smoke.checks import check_buildium

    result = await check_buildium(Settings(env="test", memory={"enabled": False}))
    assert result.status == "skipped"
    assert "per-company" in result.detail, (
        "the skip must say WHY it is honest — a bare 'not set' reads as an "
        "outage and teaches the reader to discount the line"
    )


@pytest.mark.asyncio
async def test_a_configured_buildium_credential_that_401s_still_fails() -> None:
    """The other direction, and it is what stops the skip becoming a hole.

    A single-tenant operator who DID configure the pair has a genuinely broken
    credential when it 401s. Skipping that would be gotcha 67's "a fixture named
    live that is actually dark" — a state whose name disagrees with what it
    means.
    """
    import helixis.buildium.client as client_mod
    from helixis.smoke.checks import check_buildium

    class _Boom:
        def __init__(self, *_a: object, **_kw: object) -> None: ...

        async def __aenter__(self) -> _Boom:
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

        async def request(self, *_a: object, **_kw: object) -> None:
            raise RuntimeError("Buildium 401 /rentals: Authentication failed.")

    original = client_mod.BuildiumClient
    client_mod.BuildiumClient = _Boom  # type: ignore[misc, assignment]
    try:
        result = await check_buildium(
            Settings(
                env="test",
                memory={"enabled": False},
                buildium={"client_id": "id", "client_secret": "secret"},
            )
        )
    finally:
        client_mod.BuildiumClient = original  # type: ignore[misc]

    assert result.failed
    assert "401" in result.detail


# ── skips have to be visible in the output ───────────────────────────


def _results() -> list[CheckResult]:
    return [
        CheckResult("buildium_api", "pass", 10, "ok"),
        CheckResult("memory", "skipped", 1, "memory disabled in settings"),
        CheckResult("twilio_sms", "skipped", 1, "dark until A2P approval"),
    ]


def test_the_pretty_summary_names_what_did_not_run() -> None:
    """ "PASS — 1/3 checks green" reads as success to anyone not holding §4's
    "all 15 checks green" bar in mind. Name the scope rather than leaving the
    reader to infer it from a subtraction (gotcha 26's footer reasoning)."""
    out = format_pretty("2026-08-29T00:00:00Z", True, 1.0, _results())
    assert "did NOT run" in out
    assert "memory" in out.rsplit("did NOT run", 1)[1]
    assert "twilio_sms" in out.rsplit("did NOT run", 1)[1]


def test_the_json_carries_the_skipped_checks() -> None:
    """The daily workflow gates on ``passed`` alone, so without this a run
    where a check never executed is byte-indistinguishable from one where it
    passed."""
    import json

    payload = json.loads(format_json("2026-08-29T00:00:00Z", True, 1.0, _results()))
    assert payload["skipped_checks"] == ["memory", "twilio_sms"]
    assert payload["failed_checks"] == []
