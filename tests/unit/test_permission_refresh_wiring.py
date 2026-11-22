"""Every screen that caches the caller's role listens for a 403.

⚠ **This is the half a behavioural test cannot see.** `usePermissionRefresh`
is unit-tested in `frontend/src/hooks/usePermissionRefresh.test.tsx` and those
tests pass whether or not any component calls it — the `comms_triage` failure
this repo already paid for (gotcha 62: a module complete, correct and wired to
nothing).

⚠ **It lives in pytest rather than vitest**, following
`test_roster_invite_pending.py`, which reads the same frontend tree from here.
Reading files from a vitest test needs `@types/node`, which the frontend
tsconfig does not carry — and vitest does not typecheck, so that version went
GREEN while `tsc` stayed red (gotcha 72). Moving the scan is what made both
honest.

THE DEFECT
----------
`SettingsPage` and `LeasingComplianceGate` each read `GET /company/me` on
mount and keep the answer (a third, `InviteTeamBanner`, was deleted when the
invite moved into the Getting started checklist). An admin demoted to member went
on seeing admin affordances until something remounted that screen, which on a
tab left open all day is never. The backend was never wrong — `require_role`
reads `company_users` on every request — so the button 403s with a real
sentence when clicked. The only defect is that it was still there to click.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
_COMPONENTS = _FRONTEND / "components"
_HOOK = _FRONTEND / "hooks" / "usePermissionRefresh.ts"

#: The screens known to cache a role. Kept as a literal so the test below can
#: compare it against what the directory actually contains — a list that
#: derives itself from the scan would agree with the scan by construction
#: (gotcha 45).
_ROLE_CACHERS = (
    "SettingsPage.tsx",
    "LeasingComplianceGate.tsx",
)

#: A CALL, not the identifier. `import { usePermissionRefresh } from ...`
#: contains the bare name, so a bare-substring check passes a file that
#: imports the hook and never calls it — which is exactly what a mutation
#: deleting the call site produced. Gotcha 40's lesson without an AST: pick a
#: discriminator the import cannot satisfy.
_CALL = "usePermissionRefresh("


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _components() -> list[Path]:
    files = [p for p in sorted(_COMPONENTS.glob("*.tsx")) if not p.name.endswith(".test.tsx")]
    assert files, f"no components found under {_COMPONENTS} — wrong path or empty glob"
    return files


def test_the_hook_exists_and_is_the_one_thing_that_subscribes() -> None:
    """A rename that leaves the components calling a dead import."""
    source = _read(_HOOK)
    assert "onPermissionDenied" in source, "the hook must subscribe to the api signal"
    assert "export function usePermissionRefresh" in source

    # The import alone must not satisfy the call check below.
    assert _CALL not in "import { usePermissionRefresh } from '../hooks/usePermissionRefresh';"


@pytest.mark.parametrize("name", _ROLE_CACHERS)
def test_each_role_caching_screen_subscribes(name: str) -> None:
    assert _CALL in _read(_COMPONENTS / name), (
        f"{name} caches the caller's role and does not re-read it when the backend "
        "says permissions changed. A demoted admin keeps seeing admin affordances "
        "until the screen remounts."
    )


def test_no_fourth_screen_caches_a_role_unguarded() -> None:
    """Reads the DIRECTORY, not the list.

    The failure mode here is "silently permits more" (gotcha 45): a new screen
    that calls `getMyRole` and forgets to subscribe is exactly the defect, and
    a test that only checked the known files would never see it.
    """
    cachers = {p.name for p in _components() if "getMyRole(" in _read(p)}
    unguarded = {n for n in cachers if _CALL not in _read(_COMPONENTS / n)}
    assert not unguarded, (
        f"these screens read the caller's role and never refresh it on a 403: {sorted(unguarded)}"
    )
    assert cachers == set(_ROLE_CACHERS), (
        "the set of role-caching screens changed — update _ROLE_CACHERS deliberately "
        f"(found {sorted(cachers)})"
    )


def test_the_api_client_actually_announces_a_403() -> None:
    """The producer side of the same wire (gotcha 28 — read BOTH sides).

    Every component could subscribe correctly and the signal never fire.
    """
    api = _read(_FRONTEND / "services" / "api.ts")
    assert "notifyPermissionDenied" in api
    assert "res.status === 403" in api, "the 403 branch that fires the signal is gone"


def test_the_leasing_gate_clears_the_cached_role_rather_than_only_reloading() -> None:
    """Its probe is guarded by `if (companyRole === null)`.

    So after a demotion the store still holds the OLD role, and a plain reload
    skips exactly the read that matters — a refresh that refreshes everything
    except the thing that changed. The general "is it subscribed" check above
    passes either way, which is why this is pinned separately.
    """
    source = _read(_COMPONENTS / "LeasingComplianceGate.tsx")
    call = source[source.index("usePermissionRefresh(") :]
    assert "setCompanyRole(null)" in call[:400], (
        "LeasingComplianceGate must clear the cached role inside its refresh callback"
    )
