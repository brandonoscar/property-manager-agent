"""Every outbound-dispatch call site must be a known, guarded path.

The write-path inventory (docs/research_investigation.md §1.1) found the CRM
SMS/call/email routes calling providers with no confirm gate and no
idempotency guard — a double-click double-sent. Writing THIS test then found
a third unguarded lane (renewals ``send_offer``) before it even ran. That
class of gap is what this test makes structural: it AST-scans ``src/helixis``
for call sites of the outbound-dispatch PRIMITIVES (the functions that
actually hit a provider) and fails on any call site not in the allowlist.

A new call site legitimately requires adding an allowlist entry — the diff
review then asks the only question that matters: is the new site guarded
(``guarded_action`` / ``run_guarded`` / ``gated_invoke``), or is it read-only?
Never allowlist a send site without one of those wrappers.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "helixis"

# The provider-hitting primitives, matched by called-name. ``execute_tool``
# covers ALL manual Composio execution (sends AND read-only fetches — the
# allowlist distinguishes them).
PRIMITIVES = frozenset({"send_sms", "create_call", "send_gmail_email", "execute_tool"})

# (module path relative to src/helixis) → allowed enclosing function names.
# "*" = every function in the file. Each entry documents WHY it is safe.
ALLOWED: dict[str, set[str]] = {
    # ── thin provider wrapper (not a dispatch decision) ──
    # (the primitives' own definitions — twilio_client, composio_client —
    # contain no primitive CALLS, so they need no entry)
    "crm/email_send.py": {"send_gmail_email"},  # wrapper; body calls execute_tool
    # ── guarded dispatch sites (each wrapped in guarded_action) ──
    "api/routes/crm.py": {"_send_sms", "_send_email", "_place_call"},
    "api/routes/renewals.py": {"_send_offer_email"},
    # Inbox planner handler — reachable ONLY through the execute endpoint,
    # which wraps every handler in guarded_action (buildium.py::
    # _execute_action_once). The handler itself is the ``execute`` callable.
    "agent/handlers/reply_to_email.py": {"*"},
    # Manager reminder digest (SMS Stage 3) — the callee IS the guard stack:
    # comms/sms.send_sms enforces dark-flag, consent (fail closed), quiet
    # hours, and templates-only; the digest adds its own idempotency guard
    # (at most one account_notice per phone per UTC day, read from
    # helixis_sms_log before every send). Worker-driven, no user click to
    # double-fire; unit-tested in test_reminders_sms_digest.py.
    "reminders/sms_digest.py": {"send_reminder_digests"},
    # Agent text tool (SMS Stage 4) — the ONE agent-facing send_sms call site.
    # The call lives in the ``_do_send`` execute closure passed to run_guarded
    # (confirm gate showing the rendered message + durable idempotency claim);
    # before that the tool refuses while dark, resolves the recipient against
    # the live consent ledger (fail closed), and renders strictly. send_sms
    # itself re-runs the dark/consent/quiet-hours/templates guards. Unit-tested
    # in test_sms_tool.py; surface pinned in test_release_safety_gates.py.
    "agents/sms_tools.py": {"_do_send"},
    # ── read-only manual execution (fetch/inspect, no send) ──
    "webhooks/email_poll.py": {"*"},  # GMAIL_FETCH_EMAILS
    "webhooks/crm_email_poll.py": {"*"},  # lead-email fetch
    "webhooks/email_photo_triage.py": {"*"},  # GMAIL_GET_ATTACHMENT
    "reminders/outbound.py": {"*"},  # in:sent query probe
}


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _call_sites(path: Path) -> list[tuple[str, int]]:
    """(enclosing function name, lineno) for every primitive call in a file.

    Nested defs report the INNERMOST function — that's the granularity the
    allowlist uses (``_send_sms`` inside ``reply_to_lead``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sites: list[tuple[str, int]] = []

    def walk(node: ast.AST, func: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if isinstance(child, ast.Call) and _called_name(child) in PRIMITIVES:
                sites.append((func, child.lineno))
            walk(child, name)

    walk(tree, "<module>")
    return sites


def test_every_dispatch_call_site_is_allowlisted() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        sites = _call_sites(path)
        if not sites:
            continue
        allowed = ALLOWED.get(rel)
        for func, lineno in sites:
            if allowed is not None and ("*" in allowed or func in allowed):
                continue
            violations.append(f"{rel}:{lineno} in {func}()")
    assert not violations, (
        "Unallowlisted outbound-dispatch call site(s):\n  "
        + "\n  ".join(violations)
        + "\nWrap the new site in guarded_action / run_guarded / gated_invoke "
        "(or confirm it is read-only), then add it to ALLOWED with a comment "
        "saying why it is safe."
    )


def test_allowlist_has_no_stale_entries() -> None:
    """An allowlist entry whose file no longer calls a primitive is stale —
    prune it so the list stays an accurate dispatch inventory."""
    stale = []
    for rel in ALLOWED:
        path = SRC / rel
        if not path.exists() or not _call_sites(path):
            stale.append(rel)
    assert not stale, f"Stale ALLOWED entries (no primitive calls): {stale}"
