"""A route that takes a company id from the CALLER must verify they belong to it.

⚠ THE GAP THIS FILLS, and why the existing guard cannot see it.

``test_tenant_scoping_corpus`` (gotcha 63) proves every query against a
``helixis_*`` table carries ``company_id``. That is the SQL layer, and it says
nothing about **whose** company id got bound. A route that reads the value off
the request body and passes it straight into a perfectly-scoped query is a
clean cross-tenant read that the corpus guard rates as compliant — the query is
scoped, just to somebody else's workspace.

So this is the layer above: for every HTTP handler that can receive a company id
from client input — a path param, a query param, or a field on its request
model — either the caller's membership is checked, or the company is derived
from the token and the client's value is ignored.

Measured 2026-09-04 over ``api/routes/``: **9** handlers take a client-supplied
company id, **8** verify membership, and the ninth is the deliberately-public
trust page (below). Zero defects. The guard's job is to hold that line.

⚠ A FALSE POSITIVE COST A DIAGNOSTIC STEP AND IS PRESERVED HERE. The first cut
of this scan looked only for ``require_company_access`` and reported all seven
``company.py`` routes as unverified. They are not: they delegate to
``_resolve_target_company``, which raises 403 on a company the caller does not
belong to. **A verifier set that misses a delegating resolver manufactures
findings on correct code**, which is gotcha 83's shape and how a guard gets
switched off. Both were checked by reading the helper before anything was
claimed (gotcha 47).

⚠ AND IT CAUGHT ITS AUTHOR ON THE FIRST RUN. I seeded the verifier set with
``_company_id_for_user``, a plausible-sounding helper that does not exist
anywhere in this codebase. Nothing would have failed: a dead name matches no
call, so it simply sits there reading as coverage. That is why
``test_the_verifier_set_names_functions_that_actually_exist`` is in here — the
same reason gotcha 63's allowlist needs a stale-entry test.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_ROUTES = pathlib.Path(__file__).resolve().parents[2] / "src/helixis/api/routes"

#: Anything that establishes the caller may act on the company in question.
#: Two families, both acceptable:
#:
#: * VERIFIERS — take a client-supplied id and 403 unless the caller belongs
#:   (``require_company_access``), or refuse when the account spans more than
#:   one workspace (``require_unambiguous_company``, for the irreversible
#:   actions that cannot afford a guess).
#: * RESOLVERS — derive the company from the token, ignoring or validating
#:   whatever the client sent (``_resolve_target_company`` does both: it
#:   defaults to the caller's own and 403s on an explicit foreign one).
#:
#: ⚠ Adding a name here WIDENS what counts as safe. A new entry must actually
#: raise on a company the caller does not belong to — not merely mention one.
_ESTABLISHES_SCOPE = frozenset(
    {
        "require_company_access",
        "require_unambiguous_company",
        "_resolve_target_company",
        "_require_company",
        "_primary_company_id",
    }
)

#: Handlers that legitimately serve a company id nobody authenticated.
#: ``(module, handler): reason`` — a reason, not a rubber stamp.
_PUBLIC_BY_DESIGN: dict[tuple[str, str], str] = {
    ("trust.py", "trust_page"): (
        "The SMS privacy policy and terms, fetched by a CARRIER REVIEWER who has "
        "no credentials and no knowledge of our /api/v1 prefix — a page they "
        "cannot read is the same rejection as no page (gotcha 77). It returns "
        "generated policy text plus the legal business name the customer "
        "themselves filed with that carrier, and nothing else: no tenants, no "
        "books, no contact details. The company id is a UUID, so this is not an "
        "enumeration surface in practice."
    ),
}


def _request_models_with_company(tree: ast.Module) -> set[str]:
    """Classes in this module carrying a ``company_id`` field."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                target = getattr(stmt, "target", None)
                if isinstance(stmt, ast.AnnAssign) and getattr(target, "id", "") == "company_id":
                    out.add(node.name)
    return out


def _called_names(fn: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name:
                names.add(name)
    return names


def _handlers_taking_a_client_company(
    source: str, filename: str
) -> list[tuple[str, str, list[str], set[str]]]:
    """``(module, handler, how the id arrives, names it calls)`` for each route."""
    tree = ast.parse(source)
    models = _request_models_with_company(tree)
    found: list[tuple[str, str, list[str], set[str]]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        decorators = [ast.unparse(d) for d in fn.decorator_list]
        if not any(d.startswith("router.") for d in decorators):
            continue
        arrives: list[str] = []
        for arg in [*fn.args.args, *fn.args.kwonlyargs]:
            annotation = ast.unparse(arg.annotation) if arg.annotation else ""
            if arg.arg == "company_id":
                arrives.append("path/query param")
            elif annotation in models:
                arrives.append(f"body field on {annotation}")
        if arrives:
            found.append((filename, fn.name, arrives, _called_names(fn)))
    return found


def _scan() -> list[tuple[str, str, list[str], set[str]]]:
    out: list[tuple[str, str, list[str], set[str]]] = []
    for path in sorted(_ROUTES.glob("*.py")):
        out += _handlers_taking_a_client_company(path.read_text(encoding="utf-8"), path.name)
    return out


def test_the_scan_finds_the_routes_it_is_meant_to_check() -> None:
    """⚠ A bad glob or a changed decorator style collects ZERO handlers and
    reports green — an empty scan is indistinguishable from a clean one
    (gotcha 49). Pin a floor, and pin a route we know is in the set."""
    found = _scan()
    assert len(found) >= 8, (
        f"only {len(found)} handler(s) matched — the scan has probably stopped "
        "recognising route decorators or request models, not the codebase "
        "stopped taking company ids"
    )
    assert any(f[0] == "company.py" and f[1] == "invite_member" for f in found), (
        "invite_member takes company_id on its request body and is the canonical "
        "member of this set; the scan no longer sees it"
    )


def test_every_route_taking_a_client_company_establishes_scope() -> None:
    """THE ONE. A handler here decides which workspace's data the caller reads.

    ⚠ Failing this does NOT mean "add the name to the allowlist". It means the
    route accepts a company id from whoever is calling and hands back that
    company's data — the shape of a complete cross-tenant read against a
    perfectly scoped query.
    """
    offenders = [
        (module, handler, arrives)
        for module, handler, arrives, calls in _scan()
        if not (calls & _ESTABLISHES_SCOPE) and (module, handler) not in _PUBLIC_BY_DESIGN
    ]
    assert not offenders, "\n".join(
        f"{m}::{h} takes a company id from the client ({', '.join(a)}) and never "
        f"checks the caller belongs to it. Call require_company_access(user, id), "
        f"or derive the company from the token and ignore what was sent."
        for m, h, a in offenders
    )


@pytest.mark.parametrize(("module", "handler"), sorted(_PUBLIC_BY_DESIGN))
def test_no_stale_exemption(module: str, handler: str) -> None:
    """⚠ An allowlist needs this, and gotcha 63 records it catching its author.

    A stale exemption is a licence nobody needs that makes the list read as
    more permissive than the code is — and it stops being a decision the next
    reader can weigh, because the route it named is gone.
    """
    found = _scan()
    assert any(f[0] == module and f[1] == handler for f in found), (
        f"{module}::{handler} is exempted but the scan no longer finds it — the "
        "route was renamed, removed, or stopped taking a company id. Delete the "
        "entry rather than leaving a permission for something that does not exist."
    )


def test_the_verifier_set_names_functions_that_actually_exist() -> None:
    """A verifier list that drifts from the code silently widens what passes.

    ⚠ THIS IS THE FAILURE MODE THAT MATTERS, and it is invisible: renaming
    ``require_company_access`` would leave the old name in the set matching
    nothing, every route would look unguarded, and the natural fix — adding the
    NEW name — is right. The dangerous direction is the mirror: a name that
    stops existing while routes still appear to call something.
    """
    source = "\n".join(
        p.read_text(encoding="utf-8") for p in [*_ROUTES.glob("*.py"), _ROUTES.parent / "auth.py"]
    )
    defined = {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    missing = sorted(n for n in _ESTABLISHES_SCOPE if n not in defined)
    assert not missing, (
        f"these names count as establishing company scope but no longer exist: "
        f"{missing}. Either re-point them at the current helper or drop them — "
        "a dead entry cannot vouch for anything."
    )
