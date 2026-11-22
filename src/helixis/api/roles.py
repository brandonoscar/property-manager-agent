"""Team-role vocabulary + hierarchy (ADR 0005).

The reconciled roles are **Admin / Manager / Member** (no "Owner" — that word
is reserved for rental Owners in Buildium, and collided with the team role in
the UI). This module is the single source of truth for:

- the canonical role names (:data:`ADMIN` / :data:`MANAGER` / :data:`MEMBER`),
- :func:`normalize_role`, which lowercases and folds anything unrecognised onto
  :data:`MEMBER` (least privilege), and
- :func:`role_at_least`, the ordering used by ``require_role``.

Enforcement order (ADR 0005): the API's ``require_role`` is the *primary* gate;
the ``company_users`` write-RLS policies are defense-in-depth. Roles gate the
VISIBILITY of Settings screens in the frontend — there are no per-user
overrides.
"""

from __future__ import annotations

ADMIN = "admin"
MANAGER = "manager"
MEMBER = "member"

#: The canonical, assignable roles, highest privilege first.
ROLES: tuple[str, ...] = (ADMIN, MANAGER, MEMBER)

# ⚠ THE 'owner' → ADMIN ALIAS IS GONE, DELIBERATELY (2026-08-29, founder call).
#
# It was a deploy→migrate-window shim: the pre-ADR-0005 team vocabulary was
# 'owner'/'manager'/'employee', and migration 0034 reconciled the rows. That
# window closed. Live-verified against helixis-test: the CHECK constraint now
# reads ``role = ANY (ARRAY['admin','manager','member'])`` — the database
# REFUSES to store 'owner' — and the live rows are 11 admin, 1 manager,
# 1 member, with zero 'owner' among them. So the alias could only ever fire on
# a value that cannot exist in the column it was written for.
#
# Leaving it was not harmless. "Owner" in this product means a RENTAL OWNER —
# a Buildium concept, a person who owns a property and has no Occupella login
# at all; they live on Buildium's own portal. The word appears as a contact
# kind in ``mirror_tools`` (tenant/owner/vendor) and as an entity type in
# ``buildium_tools`` (``"owner": "RentalOwner"``), which is exactly the
# collision ADR 0005 removed from the team vocabulary. An alias reading that
# word as the HIGHEST team privilege is a latent escalation: any future code
# that reaches for ``normalize_role`` on a Buildium-sourced field would hand
# out admin.
#
# Removal fails in the safe direction. ``normalize_role`` defaults anything
# unrecognised to MEMBER, so 'owner' now resolves to least privilege — which
# is also the truth about what a rental owner may do here: nothing.
# 'employee' went with it; it mapped DOWN to member, so dropping it changes no
# outcome at all, and one dead shim is easier to reason about than two.

# Privilege rank — lower number = more privilege. Used by ``role_at_least``.
_RANK = {ADMIN: 0, MANAGER: 1, MEMBER: 2}


def normalize_role(role: str | None) -> str:
    """Fold a stored/requested role onto the canonical vocabulary.

    Lowercases, and defaults anything unknown or absent to :data:`MEMBER` —
    least privilege, fail closed. There are no aliases: a value outside
    ``ADMIN``/``MANAGER``/``MEMBER`` is not a team role, and 'owner' in
    particular is a Buildium rental owner with no access here (see above).
    """
    if not role:
        return MEMBER
    normalized = role.strip().lower()
    if normalized not in _RANK:
        return MEMBER
    return normalized


def role_at_least(role: str | None, minimum: str) -> bool:
    """True when ``role`` is at least as privileged as ``minimum``.

    Both are normalized first, so an unrecognised role is compared as MEMBER.
    """
    return _RANK[normalize_role(role)] <= _RANK[normalize_role(minimum)]


__all__ = [
    "ADMIN",
    "MANAGER",
    "MEMBER",
    "ROLES",
    "normalize_role",
    "role_at_least",
]
