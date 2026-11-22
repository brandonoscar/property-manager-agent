"""A view in `public` opts out of the RLS its base tables rely on.

Every `helixis_*` table in this project is RLS-fail-closed with no policy, and
the app reads through FastAPI on the service key. That is a good arrangement
with one sharp edge nobody had hit until 2026-09-02: **`enable row level
security` protects the TABLE, not a view someone later defines over it.**

A view created in `public` is owned by `postgres`, which carries `rolbypassrls`,
so without `security_invoker = true` it executes with the OWNER's rights and RLS
does not apply. Postgres additionally grants SELECT on a new view to `anon` and
`authenticated` by default, and the anon key is PUBLIC — it ships in the
deployed frontend bundle (`frontend/src/services/auth.ts`). Those three facts
compose into: any view added to `public` is, by default, a world-readable
window around RLS.

Measured on the live project before the fix: `companies` and `helixis_billing`
both have RLS ON and return ZERO rows to `anon` directly, while
`helixis_billing_missing` — added by 0088 the same afternoon — returned every
company's id, name and signup date to the same role. Supabase's own security
advisor rated it ERROR / EXTERNAL.

⚠ This guard reads the CORPUS, not a function (gotcha 45). The failure mode is
"silently permits more", and the loud half — a view that stops working — is the
half you would have noticed anyway. What has to be measured is every view the
migrations actually create.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"

#: `create [or replace] view public.<name>` / `create view <name>`.
_CREATE_VIEW = re.compile(
    r"create\s+(?:or\s+replace\s+)?(?:materialized\s+)?view\s+(?:public\.)?([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)


def _statements(path: Path) -> str:
    """Migration text with `--` comment lines stripped.

    Scoped to statements, never the whole file (gotchas 40/59): 0089's header
    quotes `security_invoker` and `revoke` in order to EXPLAIN them, so a
    file-wide scan would mark every view safe on the strength of the prose
    describing the guard.
    """
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    )


def _views_created() -> dict[str, list[Path]]:
    """view name -> the migrations that create it."""
    out: dict[str, list[Path]] = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        for name in _CREATE_VIEW.findall(_statements(path)):
            out.setdefault(name.lower(), []).append(path)
    return out


def _all_statements() -> str:
    """Every migration's statements, concatenated and lowercased.

    ⚠ The hardening for a view does NOT have to live in the migration that
    creates it, and requiring that was this guard's own first bug: 0088 creates
    `helixis_billing_missing` and 0089 secures it, which is the correct shape —
    you cannot retroactively edit an applied migration, so a fix always arrives
    later. Search the whole corpus for the ALTER and the REVOKE.
    """
    return "\n".join(_statements(p) for p in sorted(MIGRATIONS.glob("*.sql"))).lower()


def test_the_scan_finds_something() -> None:
    """A guard that silently matches nothing passes forever (gotcha 49).

    There is at least one view in the tree — `helixis_billing_missing`. If this
    fails, the regex or the glob broke, not the code under test.
    """
    assert MIGRATIONS.is_dir(), f"missing {MIGRATIONS}"
    assert _views_created(), "no CREATE VIEW found in any migration — the scanner is broken"


def test_every_public_view_declares_security_invoker() -> None:
    """Without it the view runs as `postgres`, which bypasses RLS entirely."""
    corpus = _all_statements()
    offenders = []
    for name, paths in _views_created().items():
        if not re.search(
            rf"alter\s+view\s+public\.{name}\s+set\s*\(\s*security_invoker\s*=\s*true\s*\)",
            corpus,
        ):
            offenders.append(f"{name} (created in {', '.join(p.name for p in paths)})")
    assert not offenders, (
        "views in `public` that never declare security_invoker: "
        f"{offenders}. Without it the view executes as its owner, which carries "
        "rolbypassrls, so RLS on the base tables does not apply to it. Add "
        "`alter view public.<name> set (security_invoker = true);` or move the "
        "view out of the PostgREST-exposed schema."
    )


def test_every_public_view_revokes_the_browser_roles() -> None:
    """Defence in depth, and the two lines fail independently.

    `security_invoker` makes the caller's RLS apply; the REVOKE removes the
    grant outright, so a Postgres default that changes under us cannot silently
    re-expose the view. The anon key is in the shipped bundle — assume any grant
    to `anon` is a grant to the internet.
    """
    corpus = _all_statements()
    offenders = []
    for name in _views_created():
        for role in ("anon", "authenticated"):
            if not re.search(rf"revoke\s+all\s+on\s+public\.{name}\s+from\s+{role}", corpus):
                offenders.append(f"{name} keeps its default grant to {role}")
    assert not offenders, (
        f"{offenders}. Postgres grants SELECT on a new view to anon and "
        "authenticated by default, and the anon key is public."
    )


def test_the_billing_missing_view_is_the_one_we_know_about() -> None:
    """Pins the specific view this rule was learned on.

    If it is ever dropped or renamed, this fails and whoever did it has to
    re-read the rule rather than quietly losing the standing check that
    `helixis_billing_missing` provides (0088's only confirmation, since
    migration_check.py cannot see a data-only backfill).
    """
    views = _views_created()
    assert "helixis_billing_missing" in views, (
        "helixis_billing_missing is gone — it is the standing check that every "
        "company has a billing row. If that is deliberate, replace the check "
        "before removing the view."
    )
