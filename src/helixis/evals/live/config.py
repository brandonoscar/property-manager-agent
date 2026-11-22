"""Capability gating, stable identifiers, and the SKIP contract for live evals.

Every live suite starts by asking the same question: *do I have what I need to
run for real?* If not, it records SKIPPED with a concrete reason rather than a
fake pass — the eval-reports norm. This module centralises that decision so all
six suites answer it identically.

Nothing here touches the network or the DB at import time; the heavy modules
(orchestrator, buildium client, asyncpg) are imported lazily by the seeder /
driver, not here — so ``config`` stays cheap enough to import from unit tests.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from helixis.settings import Settings, get_settings

# ── Stable two-company identity ──────────────────────────────────────────────
# Fixed UUIDs so the seeder is idempotent and every suite/read-back agrees on
# which rows belong to which company WITHOUT threading state through a file. The
# recognisable a…a / b…b prefixes make a stray row obvious in the DB.
#
# Company A is LIVE-BACKED: real ``--credentialed`` provisioning + synthetic
# traffic against the one Buildium sandbox, so its mirror holds real work
# orders / tenants / bills synced from Buildium. Company B is a SHADOW company
# seeded directly into the helixis-test mirror with a disjoint, known dataset —
# because one Buildium sandbox account maps to a single ``AccountId`` and
# webhook sync routes by ``AccountId`` (it can't fan one account out to two
# companies). B existing only in our mirror is exactly what the cross-tenant
# suite needs: every B row is known, so "did any B row leak to A?" is decidable.
COMPANY_A_ID = "aaaaaaaa-0000-4000-8000-00000000000a"
COMPANY_B_ID = "bbbbbbbb-0000-4000-8000-00000000000b"
# The headless "user" driving turns. Must be a valid UUID — the orchestrator's
# run telemetry (orchestrator_runs) binds user_id as a uuid column, so a plain
# label like "eval-user" raises a DataError mid-turn (best-effort, but noisy).
EVAL_USER_ID = "eeeeeeee-0000-4000-8000-00000000000e"
COMPANY_A_NAME = "LiveEval Co A (Buildium-backed)"
COMPANY_B_NAME = "LiveEval Co B (shadow tenant)"

# Synthetic ``AccountId``s for the mirror rows. Deliberately NOT a real sandbox
# account id — the mirror is read by ``company_id``, so the value only has to be
# stable and distinct so a stray row is unmistakable and never reconciled by a
# real webhook. (Company A's live backfill overrides this with the real account
# id when the sandbox is reachable.)
COMPANY_A_ACCOUNT_ID = 999_000_001
COMPANY_B_ACCOUNT_ID = 999_000_002

# Buildium sandbox hosts always carry this token (the guard synthetic_traffic
# uses: helixis.scripts.synthetic_traffic._assert_sandbox). We refuse to treat
# a prod base_url as a sandbox — the whole point of the user's precondition.
_SANDBOX_HOST_TOKEN = "apisandbox"


@dataclass(frozen=True)
class Capabilities:
    """What this environment can actually do, resolved once from settings + env.

    Each flag gates a class of suite. A suite ANDs the flags it needs; a missing
    one yields a SKIP with the matching reason from :meth:`skip_reason`.
    """

    # ⚠ WAS ``google_key``, READING ``HELIXIS_LLM__GOOGLE_API_KEY`` — a key that
    # has not existed since the 2026-07-21 Anthropic migration (gotcha 18), which
    # settings.py itself annotates as dead. So every agent-driven live suite
    # (2, 8, 9, and suite 5's honesty leg) had gated on a deleted key for five
    # weeks and could ONLY ever report SKIPPED. Nothing failed and nothing was
    # logged: the report says "SKIPPED is not a pass", which is honest, but
    # nobody would notice it could never BECOME a pass. Same shape as gotcha 45,
    # failing CLOSED instead of open — a release gate that silently stopped
    # being runnable, discovered only by running it.
    #
    # Provider-agnostic now, like ``case_makes_billed_call`` after the same
    # lesson: ANY configured LLM key satisfies it, so the next provider swap
    # cannot reopen this.
    llm_key: bool  # any HELIXIS_LLM__*_API_KEY — something must drive the agent
    db_url: bool  # HELIXIS_SUPABASE__DB_URL — helixis-test (seed + read-back)
    buildium_creds: bool  # client_id + client_secret present
    buildium_sandbox: bool  # ...AND base_url is a sandbox host (never prod)
    browserbase: bool  # HELIXIS_BROWSER__BROWSERBASE_* — suite 4 redaction page
    security_extra: bool  # presidio installed — the REAL redaction path
    langfuse: bool  # Langfuse keys — dataset logging (best-effort, non-gating)

    def skip_reason(self, *needs: str) -> str | None:
        """Return a human reason if any required capability is missing, else None.

        ``needs`` names capabilities by attribute. ``buildium_sandbox`` is
        special-cased to give the actionable message (prod creds present but not
        a sandbox), because that is the precise precondition the operator has to
        fix.
        """
        missing: list[str] = []
        for need in needs:
            if need == "buildium_sandbox" and self.buildium_creds and not self.buildium_sandbox:
                missing.append(
                    "HELIXIS_BUILDIUM base_url is not a sandbox host "
                    f"(expected '{_SANDBOX_HOST_TOKEN}' in the URL) — refusing to "
                    "run write-heavy suites against a non-sandbox Buildium"
                )
                continue
            if not getattr(self, need, False):
                missing.append(_CAPABILITY_HINTS.get(need, need))
        if not missing:
            return None
        return "; ".join(missing)


_CAPABILITY_HINTS = {
    "llm_key": "no LLM API key is configured (HELIXIS_LLM__ANTHROPIC_API_KEY "
    "or another provider) — nothing can drive the agent",
    "db_url": "HELIXIS_SUPABASE__DB_URL is unset (cannot reach helixis-test)",
    "buildium_creds": "HELIXIS_BUILDIUM__CLIENT_ID/CLIENT_SECRET are unset",
    "buildium_sandbox": "no Buildium sandbox credentials",
    "browserbase": "HELIXIS_BROWSER__BROWSERBASE_API_KEY/PROJECT_ID are unset",
    "security_extra": "the [security] extra (presidio) is not installed — "
    "redact() would silently fall back to regex (gotcha 7)",
    "langfuse": "Langfuse keys unset (dataset logging skipped)",
}


#: Which settings fields count as "a key that can drive the agent". Read off the
#: LLM settings model rather than hardcoded here, so adding a provider needs no
#: edit and RETIRING one cannot silently disable a release gate. ``voyage`` is
#: excluded deliberately: it is the memory EMBEDDER and drives no turn.
_NON_DRIVING_KEYS = frozenset({"voyage_api_key"})


def _llm_key_fields(llm: object) -> list[str]:
    """Every ``*_api_key`` on the LLM settings that could drive a turn."""
    fields = getattr(type(llm), "model_fields", {}) or {}
    return sorted(
        name for name in fields if name.endswith("_api_key") and name not in _NON_DRIVING_KEYS
    )


def _any_llm_key(s: Settings) -> bool:
    return any(bool(getattr(s.llm, name, "")) for name in _llm_key_fields(s.llm))


def _presidio_installed() -> bool:
    try:
        return importlib.util.find_spec("presidio_analyzer") is not None
    except (ImportError, ValueError):
        return False


def resolve_capabilities(settings: Settings | None = None) -> Capabilities:
    """Inspect the resolved settings + env and report what we can do for real."""
    s = settings or get_settings()
    b = s.buildium
    has_creds = bool(b.client_id and b.client_secret)
    return Capabilities(
        llm_key=_any_llm_key(s),
        db_url=bool(s.supabase.db_url),
        buildium_creds=has_creds,
        buildium_sandbox=has_creds and _SANDBOX_HOST_TOKEN in (b.base_url or "").lower(),
        browserbase=bool(s.browser.browserbase_api_key and s.browser.browserbase_project_id),
        security_extra=_presidio_installed(),
        langfuse=bool(s.langfuse.enabled and s.langfuse.public_key and s.langfuse.secret_key),
    )


@lru_cache
def _git_sha() -> str:
    """Best-effort short git SHA of the checked-out tree. Empty on failure.

    Prefers an explicit override / CI SHA so the RC tag is authoritative; only
    shells out to git as a fallback. Cached so we shell out at most once.
    """
    for env_var in ("HELIXIS_EVAL_RC_SHA", "GITHUB_SHA"):
        val = os.environ.get(env_var)
        if val:
            return val.strip()[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def rc_sha() -> str:
    """The release-candidate git SHA this run is gating (short form).

    Every dataset run + report is tagged with it so a green/red verdict is
    pinned to an exact tree. ``"unknown"`` if we truly can't resolve one (still
    logged, just not pinnable) — never fabricated.
    """
    return _git_sha() or "unknown"
