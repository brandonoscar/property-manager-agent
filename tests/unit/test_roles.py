"""Unit tests for the team-role vocabulary (ADR 0005).

Pins the pure helpers in ``helixis.api.roles``: normalization folds the legacy
'owner'/'employee' vocabulary onto admin/member, unknown/blank fails closed to
member, and the hierarchy orders admin > manager > member.
"""

from __future__ import annotations

from helixis.api.roles import (
    ADMIN,
    MANAGER,
    MEMBER,
    ROLES,
    normalize_role,
    role_at_least,
)


def test_canonical_roles() -> None:
    assert ROLES == (ADMIN, MANAGER, MEMBER)


def test_owner_is_not_a_team_role_and_never_grants_admin() -> None:
    """⚠ REVERSED 2026-08-29, founder call, and the reversal is the point.

    This used to assert ``normalize_role("owner") == ADMIN`` — a
    deploy->migrate-window shim from before migration 0034 reconciled the team
    vocabulary. That window is closed: live-verified against helixis-test, the
    CHECK constraint reads ``role = ANY (ARRAY['admin','manager','member'])``
    so the column REFUSES 'owner', and the live rows are 11 admin / 1 manager /
    1 member with zero 'owner' among them.

    "Owner" in this product means a RENTAL OWNER — a Buildium concept, someone
    who owns a property and has no Occupella login at all. The word is already
    a contact kind in ``mirror_tools`` (tenant/owner/vendor) and an entity type
    in ``buildium_tools`` (``"owner": "RentalOwner"``), which is the exact
    collision ADR 0005 removed from the team vocabulary. An alias reading it as
    the highest team privilege is a latent escalation: any future code reaching
    for ``normalize_role`` on a Buildium-sourced field would hand out admin.

    Least privilege is also the truthful answer about what a rental owner may
    do here, which is nothing.
    """
    assert normalize_role("owner") == MEMBER
    assert not role_at_least("owner", ADMIN)
    assert not role_at_least("owner", MANAGER)
    # 'employee' mapped DOWN to member, so dropping it changes no outcome —
    # asserted so the removal is deliberate rather than incidental.
    assert normalize_role("employee") == MEMBER


def test_normalize_canonical_passthrough() -> None:
    assert normalize_role("admin") == ADMIN
    assert normalize_role("manager") == MANAGER
    assert normalize_role("member") == MEMBER


def test_normalize_case_and_whitespace() -> None:
    assert normalize_role("  Admin ") == ADMIN
    assert normalize_role("ADMIN") == ADMIN
    # Casing must not smuggle the retired alias back in.
    assert normalize_role("  Owner ") == MEMBER


def test_normalize_fails_closed() -> None:
    # Unknown / blank / None all collapse to the least-privilege role.
    assert normalize_role(None) == MEMBER
    assert normalize_role("") == MEMBER
    assert normalize_role("superadmin") == MEMBER


def test_role_at_least_hierarchy() -> None:
    assert role_at_least(ADMIN, ADMIN)
    assert role_at_least(ADMIN, MANAGER)
    assert role_at_least(ADMIN, MEMBER)

    assert not role_at_least(MANAGER, ADMIN)
    assert role_at_least(MANAGER, MANAGER)
    assert role_at_least(MANAGER, MEMBER)

    assert not role_at_least(MEMBER, ADMIN)
    assert not role_at_least(MEMBER, MANAGER)
    assert role_at_least(MEMBER, MEMBER)


def test_role_at_least_normalizes_both_sides() -> None:
    # Casing and whitespace on EITHER side must not change the verdict.
    assert role_at_least("  Admin ", ADMIN)
    assert role_at_least(ADMIN, "  MANAGER ")
    assert not role_at_least("MEMBER", MANAGER)
    # An unrecognised role is compared as MEMBER, so it clears nothing above it.
    assert not role_at_least("employee", MANAGER)
    assert not role_at_least("owner", ADMIN)


def test_the_code_vocabulary_matches_the_databases_check_constraint() -> None:
    """⚠ BOTH SIDES (gotcha 28), and this is the guard the role floor never had.

    Every existing test of ``_require_manager_role`` patches ``_caller_role``,
    so the DECISION is covered on both branches and the LOOKUP behind it has
    never run — the gap audit was right about that. What it read as a
    "never exercised end to end" risk is mostly refuted: the failure direction
    is fail-closed (an unverifiable role refuses), so the worst case is a
    Manager being told to do it in Buildium, not an escaped write. And the
    lookup was verified live: ``public.company_users`` has the ``role`` column
    the SQL names, and the stored vocabulary is admin/manager/member.

    The real exposure is drift. If a migration adds or renames a role and
    ``_RANK`` does not follow, ``normalize_role`` folds it to MEMBER and every
    Manager+ write silently starts refusing — with no test failing anywhere,
    because they all patch past the lookup. So pin the two together by reading
    the migration that owns the constraint.
    """
    import re
    from pathlib import Path

    from helixis.api.roles import _RANK

    migration = (
        Path(__file__).resolve().parents[2]
        / "supabase"
        / "migrations"
        / "0034_reconcile_team_roles.sql"
    ).read_text()
    check = re.search(r"check\s*\(role in \(([^)]*)\)\)", migration, re.IGNORECASE)
    assert check, "0034 no longer declares the role CHECK this guard reads"
    db_roles = {v.strip().strip("'\"") for v in check.group(1).split(",")}
    assert db_roles == set(_RANK), (
        f"the database accepts {sorted(db_roles)} and the code ranks "
        f"{sorted(_RANK)} — a role the DB stores but _RANK omits normalizes to "
        "MEMBER, so every Manager+ write refuses for that role and no existing "
        "test can see it (they all patch _caller_role)"
    )
    assert "owner" not in db_roles, (
        "'owner' is back in the team-role constraint — it means a Buildium "
        "RENTAL owner in this product and must never be a team privilege"
    )


def test_the_role_lookup_reads_the_columns_the_schema_actually_has() -> None:
    """The other half of the same gap: the SQL string itself. A renamed column
    lands in ``_caller_role``'s bare ``except`` and returns None — fail closed,
    so it presents as 'nobody can record a payment any more' with no error
    anywhere. Live-verified 2026-08-29: company_users has id, company_id,
    user_id, role, created_at."""
    import inspect

    from helixis.agents import buildium_tools

    sql = inspect.getsource(buildium_tools._caller_role)
    for column in ("role", "company_users", "user_id", "company_id"):
        assert column in sql, f"_caller_role no longer references {column!r}"
