"""2FA binds the browser and not the API — and the obvious fix locks people out.

`TwoFactorGate` stops a person at a screen, which is the threat 2FA is bought
for. It cannot stop a crafted request carrying a valid **aal1** token, because
`AuthenticatedUser` carries no `aal` claim and no route has a policy about one.
The gate's own docstring names that follow-up.

⚠ **THIS FILE EXISTS FOR THE HALF THE GATE DOES NOT NAME.** The obvious
spelling of that follow-up — "a user with a verified factor must present
aal2" — refuses exactly the people on the RECOVERY path. Supabase ships no
recovery codes (measured in `services/mfa.ts`), so ours live outside its AAL
model: `POST /mfa/backup-codes/redeem` admits the person while the Supabase
session stays aal1, and nothing records that the second factor was satisfied.
Lost phone → redeemed a code → locked out of the API → cannot reach Settings
to turn 2FA off. Worse than the gap it closes, and invisible until a real
person loses a real phone.

So this is a TRIPWIRE, not a test of behaviour that exists. It passes today
because the antecedent is false, and fires on the exact change it guards —
the shape gotcha 33's containment contract uses: record the bar, fail when
somebody crosses it unsafely. It is deliberately NOT a test that 2FA is
enforced; asserting that would be asserting a thing this product does not do.
"""

from __future__ import annotations

import re
from pathlib import Path

_AUTH = Path("src/helixis/api/auth.py")
_MFA_ROUTE = Path("src/helixis/api/routes/mfa.py")
_GATE = Path("frontend/src/components/TwoFactorGate.tsx")

#: Any of these in the auth layer means somebody started enforcing assurance
#: level server-side. Matched on CODE, never on the prose that explains the
#: trap — the docstrings here and in `auth.py` both spell "aal2" in order to
#: warn about it, and a whole-file scan would fire on its own explanation
#: (gotchas 40/59, and gotcha 114's guard that failed against correct code).
_ENFORCEMENT = re.compile(r"\baal\b|\baal2\b|assurance_level", re.I)

#: What an accommodation has to look like: something that can answer "did THIS
#: session satisfy the second factor by some accepted means". The names are
#: deliberately broad — the point is to force a deliberate look, not to dictate
#: one spelling.
_ACCOMMODATION = re.compile(r"backup_code|second_factor|session_satisfied|mfa_satisfied", re.I)


def _code_only(path: Path) -> str:
    """The file with comments and docstrings stripped.

    ⚠ Scoping matters more than usual here. `auth.py`'s own docstring now
    explains the aal trap at length, so a raw substring scan would report the
    warning as the violation — the exact failure gotcha 114 hit when a guard
    fired on the comment that explained it.
    """
    src = path.read_text(encoding="utf-8")
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"^\s*#.*$", "", src, flags=re.M)
    return src


def test_the_backend_still_does_not_enforce_assurance_level() -> None:
    """A statement of today's reality, so the tripwire below cannot pass
    vacuously forever without anyone noticing which side of it we are on.

    ⚠ If this fails, that is not a regression — it means somebody implemented
    backend 2FA enforcement, which is wanted. Read the companion test: it is
    the one that says whether they did it safely, and then DELETE this one,
    because a test asserting a gap must not outlive the gap (gotchas 93/103:
    an assertion holds a defect in place as firmly as it catches one).
    """
    assert not _ENFORCEMENT.search(_code_only(_AUTH)), (
        "api/auth.py now references assurance level — backend 2FA enforcement "
        "has started. That is good. Check the companion tripwire, then delete "
        "this test: it asserts the ABSENCE of a control and must not survive "
        "the control arriving."
    )


def test_enforcing_aal_without_a_backup_code_accommodation_is_a_lockout() -> None:
    """THE TRIPWIRE. Fires the day the claim is read without the recovery path.

    A blanket aal2 requirement locks out every customer who lost their phone
    and used a backup code — the one population 2FA recovery exists for. The
    accommodation needs session-scoped server-side state (*aal2 OR this
    session redeemed a code*), keyed on the SESSION and never the user: a
    user-keyed marker would let one redemption bless every aal1 token they
    ever hold.
    """
    auth_code = _code_only(_AUTH)
    if not _ENFORCEMENT.search(auth_code):
        return  # antecedent false — nothing is being enforced yet

    assert _ACCOMMODATION.search(auth_code), (
        "api/auth.py reads assurance level but names no backup-code "
        "accommodation. Supabase issues aal1 for a backup-code redemption "
        "because it has no recovery-code concept at all, so this rule refuses "
        "exactly the customers on the recovery path: lost phone -> redeemed a "
        "code -> locked out of the API -> cannot reach Settings to disable "
        "2FA. Read AuthenticatedUser's docstring before relaxing this."
    )


def test_the_redeem_route_exists_and_is_what_the_accommodation_must_cover() -> None:
    """Pins the precondition the tripwire's reasoning rests on.

    ⚠ If the redemption endpoint is ever removed, the lockout argument above
    stops applying and this file's advice becomes stale in the direction that
    misleads (gotcha 48: a comment cannot fail). Better to break here than to
    go on warning about a path that no longer exists.
    """
    route = _MFA_ROUTE.read_text(encoding="utf-8")
    assert "backup-codes/redeem" in route, (
        "the backup-code redemption route is gone — re-read this file's "
        "premise before trusting its warning about aal enforcement"
    )


def test_the_gate_still_admits_on_a_redeemed_backup_code() -> None:
    """The other half of the precondition, on the client.

    The lockout only bites because the gate lets a redeemed code through while
    the Supabase session stays aal1. A gate that stopped doing that would
    change the argument, so it is pinned rather than assumed.
    """
    gate = _GATE.read_text(encoding="utf-8")
    assert "redeemBackupCode" in gate and "onPassed" in gate, (
        "TwoFactorGate no longer admits on a redeemed backup code — the "
        "lockout reasoning in this file needs re-checking"
    )
