"""The compliance sweep shipped complete and callable by nothing.

``run_compliance_sweep`` is written, tested, and its own docstring says "an
operator triggers it with a rule change". No operator could: there was no
surface to trigger it from, and zero findings have ever carried a
``compliance:*`` rule key. The capability existed in the same sense a keepalive
existed while emitting no bytes (gotcha 54).

⚠ **Preview is not a nicety.** The sweep never touches Buildium, but it is not
side-effect free: it persists findings, and those bridge into the Inbox as a
card. A rule written slightly wrong — "under $2,000" when you meant $1,200 — is
the normal case, so the operator sees who it catches before anything lands on
anyone's desk.
"""

from __future__ import annotations

import ast
import inspect

from helixis.api.routes import compliance


def _paths() -> list[str]:
    from helixis.main import app

    return [p for p in app.openapi()["paths"] if "compliance" in p]


# ── it is reachable at all ───────────────────────────────────────────


def test_the_sweep_now_has_a_trigger() -> None:
    """THE finding. A capability nothing can invoke is not a capability."""
    paths = _paths()
    assert "/api/v1/compliance/sweeps" in paths
    assert "/api/v1/compliance/sweeps/preview" in paths


def test_the_route_actually_calls_the_sweep() -> None:
    """⚠ Both sides (gotcha 28). A route that never reaches the function is
    the same unreachable capability wearing a URL — and this whole session
    keeps finding well-tested helpers whose callers drop them."""
    assert "run_compliance_sweep" in inspect.getsource(compliance.run_sweep)
    assert "sweep_leases" in inspect.getsource(compliance.preview_sweep)


def test_preview_persists_nothing() -> None:
    """The distinction the two verbs exist for. Preview must reach the PURE
    read (``sweep_leases``), never the persisting wrapper — otherwise "show me
    who this catches" quietly cards the whole company."""
    src = inspect.getsource(compliance.preview_sweep)
    assert "run_compliance_sweep" not in src, (
        "preview calls the persisting path — it would card everyone it previews"
    )


# ── the gate ─────────────────────────────────────────────────────────


def test_running_a_sweep_takes_manager_or_admin() -> None:
    """It puts a card on everybody's desk and creates findings that persist
    until the rule is retired. Backing that out is manual, which is the same
    reason the awkward-to-undo Buildium writes are floored."""
    assert "require_role" in inspect.getsource(compliance.run_sweep)


def test_the_gate_reads_company_users_not_the_jwt_role() -> None:
    """⚠ The bug the first cut of this route shipped.
    ``AuthenticatedUser.role`` is the SUPABASE role and is the literal string
    "authenticated" for every caller, so ``role_at_least(user.role, MANAGER)``
    is False for admins and members alike — the endpoint would have been
    unusable by anyone, which is a fail-closed bug and therefore the quiet
    kind: it looks like a working gate.

    ``require_role`` queries ``company_users``, which is where the role
    actually lives.
    """
    src = inspect.getsource(compliance.run_sweep)
    assert "require_role" in src
    tree = ast.parse(inspect.cleandoc(src))
    bad = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "role_at_least"
    ]
    assert not bad, "role_at_least on the JWT role denies everybody"


def test_preview_is_not_role_floored() -> None:
    """Both directions. Preview changes nothing, and flooring it would push
    people to run the real sweep to find out what it would do — making the
    safe path harder than the destructive one."""
    assert "require_role" not in inspect.getsource(compliance.preview_sweep)


# ── the inputs ───────────────────────────────────────────────────────


def test_a_label_must_be_a_usable_rule_id() -> None:
    """The label becomes a durable ``compliance:<label>`` rule key that
    re-running updates in place, and it shows on user-facing cards. A colon
    would collide with the key format; two spellings would look like two
    rules that each auto-resolve the other's findings."""
    for bad in ("", "ab", "has space", "UPPER", "colon:here", "x" * 60):
        assert not compliance._LABEL_RE.match(bad), f"{bad!r} should be refused"
    for good in ("late-fee-cap-2026", "addendum_q3", "rule1"):
        assert compliance._LABEL_RE.match(good), f"{good!r} should be accepted"


def test_an_impossible_rent_window_is_refused_not_silently_empty() -> None:
    """rent_min above rent_max matches nothing, and "0 leases affected" is
    exactly the answer that reads as good news. Say the rule is broken."""
    import pytest
    from fastapi import HTTPException

    body = compliance.SweepIn(label="ok-label", remediation="r", rent_min=2000.0, rent_max=1000.0)
    with pytest.raises(HTTPException) as exc:
        compliance._validated(body)
    assert exc.value.status_code == 422


# ── the output ───────────────────────────────────────────────────────


class _F:
    def __init__(self, lease_id: int) -> None:
        self.evidence = {"lease_id": lease_id, "unit_number": "3C", "rent": 1200.0}
        self.entity_id = lease_id
        self.property_id = 7


def test_a_truncated_sample_says_so_and_keeps_the_true_total() -> None:
    """⚠ A truncated list read as the whole set is this product's worst
    failure mode (gotcha 26). ``matched`` carries the real count so a
    portfolio-wide rule can never be under-reported by the display cap."""
    body = compliance.SweepIn(label="ok-label", remediation="r")
    out = compliance._out(body, [_F(i) for i in range(40)], persisted=False)
    assert out.matched == 40
    assert len(out.sample) == compliance._SAMPLE_LIMIT
    assert out.truncated is True


def test_an_untruncated_sample_is_not_flagged() -> None:
    """Both directions — a truncation warning on a complete list teaches the
    reader to ignore it."""
    body = compliance.SweepIn(label="ok-label", remediation="r")
    out = compliance._out(body, [_F(1), _F(2)], persisted=False)
    assert out.matched == 2
    assert out.truncated is False


def test_the_response_says_whether_anything_was_persisted() -> None:
    """The two verbs return the same shape, so the caller needs the flag to
    know whether a card was created — otherwise a preview and a real run are
    indistinguishable to whoever reads the result."""
    body = compliance.SweepIn(label="ok-label", remediation="r")
    assert compliance._out(body, [_F(1)], persisted=False).persisted is False
    assert compliance._out(body, [_F(1)], persisted=True).persisted is True


def test_each_endpoint_reports_its_own_truth_about_persistence() -> None:
    """⚠ The helper being right does not make the CALLERS right, and only the
    callers ship. The test above passes with ``preview_sweep`` hardcoding
    ``persisted=True`` — which would tell an operator a card was created when
    none was, the exact inversion of the flag's purpose. Mutation caught it;
    the assertion above could not, because it never reads the endpoints.

    Same shape as the recurring finding all through this work: a well-tested
    helper whose caller drops or inverts it.
    """
    for fn, expected in ((compliance.preview_sweep, False), (compliance.run_sweep, True)):
        tree = ast.parse(inspect.cleandoc(inspect.getsource(fn)))
        flags = [
            kw.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_out"
            for kw in node.keywords
            if kw.arg == "persisted"
        ]
        assert flags, f"{fn.__name__} does not pass persisted= explicitly"
        assert all(isinstance(f, ast.Constant) and f.value is expected for f in flags), (
            f"{fn.__name__} reports persisted={[ast.unparse(f) for f in flags]}, "
            f"expected {expected}"
        )
