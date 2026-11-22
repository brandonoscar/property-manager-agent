"""The daily smoke must be handed the credentials its checks read.

``check_storage_buckets`` needs the Supabase REST url + service key. The
workflow passed only ``__DB_URL``, so the check reported "Supabase URL or
service key not set" every morning and filed a ``[smoke]`` issue — for days
(#833, #841). Both buckets existed the whole time; nothing was down.

That is the gotcha-23 failure mode with a different mask: a check that can
only ever fail is not a check, and a canary that has been red for a week is
not a release gate. `release-check.yml` runs this same smoke as the "do not
release" gate, so a permanently-red check quietly removes the gate.

A unit test is the right place for this because the cron is the ONLY host
that ever runs the real thing — nothing else would catch a missing secret
until the morning after.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
#: Both run the same smoke. release-check.yml is the "do not release" gate, and
#: it was missing four of these rows, so it could never pass (audit 2026-09-29)
#: while this file read only smoke.yml.
_SMOKE_RUNNERS = ("smoke.yml", "release-check.yml")

#: Settings the smoke checks read, mapped to the env var that fills each.
#: Add a row here when a check starts reading a new credential — the point is
#: that adding the check and forgetting the secret becomes a test failure
#: instead of a week of morning issues.
_REQUIRED_ENV = {
    # ⚠ check_buildium SKIPS when these are absent (2026-09-05) — the pair is
    # legacy single-tenant config that no production path reads. The rows stay
    # because the workflow still passes them and a single-tenant operator's
    # credential must still be probed; they are no longer a red-every-morning
    # risk, which is what the rest of this file is about.
    "HELIXIS_BUILDIUM__CLIENT_ID": "check_buildium (single-tenant path only)",
    "HELIXIS_BUILDIUM__CLIENT_SECRET": "check_buildium (single-tenant path only)",
    "HELIXIS_SUPABASE__URL": "check_storage_buckets",
    "HELIXIS_SUPABASE__SERVICE_KEY": "check_storage_buckets",
    "HELIXIS_SUPABASE__DB_URL": "check_memory / db checks",
    "HELIXIS_LLM__ANTHROPIC_API_KEY": "check_llm",
    "HELIXIS_LLM__VOYAGE_API_KEY": "check_memory (embedder)",
    "HELIXIS_WEB__TAVILY_API_KEY": "check_web_search",
}


@pytest.mark.parametrize("workflow", _SMOKE_RUNNERS)
@pytest.mark.parametrize(("env_var", "used_by"), sorted(_REQUIRED_ENV.items()))
def test_smoke_workflow_passes_every_credential_its_checks_read(
    workflow: str, env_var: str, used_by: str
) -> None:
    text = (_WORKFLOWS / workflow).read_text(encoding="utf-8")
    assert re.search(rf"^\s*{re.escape(env_var)}:\s*\$\{{\{{\s*secrets\.", text, re.M), (
        f"{env_var} is not passed to the smoke job in {workflow}, but {used_by} reads it. "
        f"The check will fail every morning with a 'not set' message that reads "
        f"like an outage."
    )


def test_the_storage_check_still_reads_the_two_settings_this_pins() -> None:
    """Guards the guard: if the check stops reading these, the rows above are
    stale and this file is testing nothing."""
    src = (
        Path(__file__).resolve().parents[2] / "src" / "helixis" / "smoke" / "checks.py"
    ).read_text(encoding="utf-8")
    body = src[src.index("async def check_storage_buckets") :][:1200]
    assert "settings.supabase.url" in body
    assert "settings.supabase.service_key" in body
