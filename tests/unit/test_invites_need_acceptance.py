"""No code path makes an existing account a member of a company without their acceptance.

THE HOLE (read-only audit, confirmed on helixis-test 2026-11-11): any account holder
(everyone admins their own sign-up company) could add any existing user to their
company by email. ``POST /company/invite`` inserted the membership at once, the
backend and the web app make the sign-up company LAST, so the inviter's company
became the victim's active one, and the live policy "admins can add company members"
let the same insert through PostgREST.

Two sides, read together (rule 15):

* **The code.** Every SQL string in ``src/`` and ``scripts/`` that writes
  ``company_users`` (INSERT, UPDATE, MERGE, COPY) sits in a function on
  ``ALLOWED_WRITERS``, each with why it can't add somebody else without their say. A
  new writer fails here until someone states that. ``company_member_invites`` (where a
  pending invite waits) is read and written by one module, so nothing that grants
  access can see a pending row; the books' verifier and the web app read
  ``company_users`` only.
* **The database.** Every migration walked in order: ``company_users`` ends with its
  SELECT policy alone and no write grant to anon or authenticated (0112), the invites
  table ends with no policy and no grant, the RLS helpers read ``company_users`` and
  never the invites, and the only SQL function inserting a membership is
  ``handle_new_user``, inserting the new user into their own company.

Statement-level and AST-level, never a substring search over a file: a comment that
explains the rule quotes the defect (gotchas 40, 63).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "supabase" / "migrations"
_SCANNED = (REPO / "src" / "helixis", REPO / "scripts")

_WRITE = re.compile(
    r"\b(?:insert\s+into|update|merge\s+into|copy)\s+(?:public\.)?\"?company_users\b", re.I
)
_INVITES = re.compile(r"\bcompany_member_invites\b", re.I)
_REST_MEMBERSHIPS = re.compile(r"rest/v1/company_users\b")

#: ``(path, function): why it cannot make someone else a member without their say``.
ALLOWED_WRITERS: dict[tuple[str, str], str] = {
    ("src/helixis/api/routes/auth.py", "bootstrap_company"): (
        "The caller's OWN sign-up company: user_id and company_id are both the token's "
        "subject, the self-heal of handle_new_user."
    ),
    ("src/helixis/api/routes/company.py", "_add_fresh_invitee"): (
        "An account GoTrue created on this very request, after _invite_one looked the "
        "address up and found none: it has nothing to lose and the emailed link is the "
        "only way into it, so following the link is the acceptance."
    ),
    ("src/helixis/db/repositories/member_invites.py", "accept"): (
        "The invitee's own acceptance: the caller's user id, from an invite addressed "
        "to them, taken in the same transaction."
    ),
    ("src/helixis/db/repositories/members.py", "set_member_role"): (
        "UPDATE ... SET role of a row that already exists, behind the admin check: it "
        "changes no user_id or company_id."
    ),
    ("scripts/create_user.py", "_ensure_membership"): (
        "Operator script that creates the account itself, run by hand with the service "
        "connection; never reachable from the API."
    ),
    ("scripts/seed.py", "seed_company_and_users"): (
        "Operator seed of synthetic users it creates itself; never reachable from the API."
    ),
}

#: The one module that reads or writes the invites table.
INVITES_MODULE = "src/helixis/db/repositories/member_invites.py"


def _docstrings(tree: ast.AST) -> set[int]:
    """Docstrings explain the rule and so quote the defect (gotcha 40): never SQL."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _sql_strings(tree: ast.AST) -> list[tuple[ast.AST, str]]:
    """Each string literal but docstrings, an f-string whole (its pieces skipped:
    gotcha 63's trap)."""
    inner: set[int] = _docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            inner.update(id(v) for v in node.values)
    found: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else " ? "
                for v in node.values
            )
            found.append((node, text))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in inner:
                found.append((node, node.value))
    return found


def _concatenated(tree: ast.AST) -> list[tuple[ast.AST, str]]:
    """Implicit concatenation is one Constant already; ``"a" + "b"`` is joined here."""
    joined: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            parts = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant)]
            if parts and all(isinstance(p, str) for p in parts):
                joined.append((node, "".join(str(p) for p in parts)))
    return joined


def _enclosing(tree: ast.AST) -> dict[int, str]:
    """Each node's innermost enclosing function name ('<module>' when none)."""
    owner: dict[int, str] = {}

    def visit(node: ast.AST, name: str) -> None:
        for child in ast.iter_child_nodes(node):
            here = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else name
            owner[id(child)] = here
            visit(child, here)

    visit(tree, "<module>")
    return owner


def _python_files() -> list[Path]:
    return sorted(p for root in _SCANNED for p in root.rglob("*.py"))


def _matches(pattern: re.Pattern[str]) -> set[tuple[str, str]]:
    hits: set[tuple[str, str]] = set()
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _enclosing(tree)
        for node, text in _sql_strings(tree) + _concatenated(tree):
            if pattern.search(text):
                hits.add((str(path.relative_to(REPO)), owner.get(id(node), "<module>")))
    return hits


def test_every_membership_writer_is_one_that_cannot_capture_an_account() -> None:
    writers = _matches(_WRITE)
    unexplained = sorted(writers - set(ALLOWED_WRITERS))
    assert not unexplained, (
        "Code that writes company_users and isn't on ALLOWED_WRITERS: "
        f"{unexplained}. An existing account becomes a member only by accepting an "
        "invite (member_invites.accept). If this writer can't add someone else "
        "without their say, add it with the reason."
    )


def test_no_allowed_writer_is_stale() -> None:
    """A dead entry reads as coverage while matching nothing."""
    stale = sorted(set(ALLOWED_WRITERS) - _matches(_WRITE))
    assert not stale, f"ALLOWED_WRITERS entries that write nothing: {stale}"


def test_the_scan_sees_the_writers_it_is_about() -> None:
    """Without this, a walk that matched nothing would pass both tests above."""
    writers = _matches(_WRITE)
    assert ("src/helixis/db/repositories/member_invites.py", "accept") in writers
    assert ("src/helixis/api/routes/company.py", "_add_fresh_invitee") in writers


def test_the_invite_of_an_existing_account_writes_only_the_invites_table() -> None:
    """``_invite_one`` itself holds no membership write: its existing-account path
    goes through ``member_invites.upsert``."""
    writers = _matches(_WRITE)
    assert ("src/helixis/api/routes/company.py", "_invite_one") not in writers
    assert ("src/helixis/api/routes/company.py", "invite_member") not in writers
    assert ("src/helixis/api/routes/company.py", "invite_members_bulk") not in writers


def test_only_one_module_touches_the_invites_table() -> None:
    """So no reader that grants access (the RLS helpers' callers, the books' verifier,
    ``_resolve_company_ids``) can ever count a pending invite as a membership."""
    files = {path for path, _fn in _matches(_INVITES)}
    assert files == {INVITES_MODULE}, files


def test_the_web_app_never_reads_the_invites_table() -> None:
    """It asks the API (``/api/v1/company/invites``); its company list reads
    ``company_users`` only (``services/auth.ts`` fetchCompanyIds)."""
    offenders = [
        str(p.relative_to(REPO))
        for p in (REPO / "frontend" / "src").rglob("*.ts*")
        if _INVITES.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_only_rest_read_of_memberships_is_a_read() -> None:
    """``_resolve_company_ids`` reads company_users over PostgREST with the service
    key; it must stay a GET, never a write."""
    hits = _matches(_REST_MEMBERSHIPS)
    assert hits == {("src/helixis/api/auth.py", "_resolve_company_ids")}
    tree = ast.parse((REPO / "src/helixis/api/auth.py").read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_resolve_company_ids"
    )
    methods = {
        n.func.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "get" in methods
    assert not methods & {"post", "patch", "put", "delete", "request"}


# ── The migrations ─────────────────────────────────────────────────────────────

API_ROLES = ("anon", "authenticated")
_WRITES = {"insert", "update", "delete", "truncate"}
_ALL_PRIVILEGES = {"select", "insert", "update", "delete", "truncate", "references", "trigger"}
_T = r"(?:public\.)?\"?([a-z0-9_]+)\"?"
_CREATE_TABLE = re.compile(rf"^create table (?:if not exists )?{_T}", re.I)
_CREATE_POLICY = re.compile(
    rf"^create policy (\"[^\"]+\"|\S+) on {_T}(?: as \w+)?"
    r"(?: for (all|select|insert|update|delete))?",
    re.I,
)
_DROP_POLICY = re.compile(rf"^drop policy (?:if exists )?(\"[^\"]+\"|\S+) on {_T}", re.I)
_REVOKE = re.compile(rf"^revoke (.+?) on (?:table )?{_T} from (.+)$", re.I | re.S)
_GRANT = re.compile(rf"^grant (.+?) on (?:table )?{_T} to (.+)$", re.I | re.S)
_FUNCTION = re.compile(
    r"create (?:or replace )?function\s+(?:public\.)?([a-z0-9_]+)\s*\((.*?)\$(\w*)\$(.*?)\$\3\$",
    re.I | re.S,
)


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _statements(sql: str) -> list[str]:
    """One file's top-level statements, function bodies removed first so a ``;`` inside
    one doesn't split it."""
    sql = _FUNCTION.sub(" ", _strip_comments(sql))
    return [" ".join(part.split()) for part in sql.split(";") if part.strip()]


def _privileges(text: str) -> set[str]:
    words = {w.strip().lower() for w in text.split(",")}
    if words & {"all", "all privileges"}:
        return set(_ALL_PRIVILEGES)
    return words


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def _final_state() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, set[str]]]]:
    """Walk every migration in order: each table's policies (name → command) and each
    API role's privileges on it. A table starts with every privilege for both roles,
    Supabase's default, until a migration revokes it."""
    policies: dict[str, dict[str, str]] = {}
    grants: dict[str, dict[str, set[str]]] = {}

    def table(name: str) -> dict[str, set[str]]:
        return grants.setdefault(name, {r: set(_ALL_PRIVILEGES) for r in API_ROLES})

    for path in _migration_files():
        for stmt in _statements(path.read_text(encoding="utf-8")):
            if m := _CREATE_TABLE.match(stmt):
                table(m[1])
            elif m := _CREATE_POLICY.match(stmt):
                policies.setdefault(m[2], {})[m[1].strip('"')] = (m[3] or "all").lower()
            elif m := _DROP_POLICY.match(stmt):
                policies.setdefault(m[2], {}).pop(m[1].strip('"'), None)
            elif m := _REVOKE.match(stmt):
                for role in {r.strip().strip('"').lower() for r in m[3].split(",")}:
                    if role in API_ROLES:
                        table(m[2])[role] -= _privileges(m[1])
            elif m := _GRANT.match(stmt):
                for role in {r.strip().strip('"').lower() for r in m[3].split(",")}:
                    if role in API_ROLES:
                        table(m[2])[role] |= _privileges(m[1])
    return policies, grants


def test_company_users_ends_with_its_read_policy_alone() -> None:
    policies, _grants = _final_state()
    assert policies["company_users"] == {"members can read company memberships": "select"}


@pytest.mark.parametrize("role", API_ROLES)
def test_company_users_ends_with_no_write_grant_to_an_api_role(role: str) -> None:
    _policies, grants = _final_state()
    assert grants["company_users"][role] & _WRITES == set(), grants["company_users"]


def test_anon_reads_no_membership_either() -> None:
    _policies, grants = _final_state()
    assert grants["company_users"]["anon"] == set()


def test_the_signed_in_web_app_still_reads_its_companies() -> None:
    """The web app's company list is a SELECT with the user's own token."""
    _policies, grants = _final_state()
    assert "select" in grants["company_users"]["authenticated"]


def test_the_invites_table_is_the_backends_alone() -> None:
    policies, grants = _final_state()
    assert policies.get("company_member_invites", {}) == {}
    assert grants["company_member_invites"] == {role: set() for role in API_ROLES}


def test_the_walk_sees_the_write_policies_0034_created() -> None:
    """Without this, a parser that matched nothing would pass the tests above."""
    seen: dict[str, str] = {}
    for stmt in _statements((MIGRATIONS / "0034_reconcile_team_roles.sql").read_text()):
        if (m := _CREATE_POLICY.match(stmt)) and m[2] == "company_users":
            seen[m[1].strip('"')] = (m[3] or "all").lower()
    assert seen == {
        "admins can add company members": "insert",
        "admins can update company members": "update",
        "admins can remove company members": "delete",
    }


def _latest_functions() -> dict[str, str]:
    """Each SQL function's body as the last migration to define it left it."""
    bodies: dict[str, str] = {}
    for path in _migration_files():
        for m in _FUNCTION.finditer(_strip_comments(path.read_text(encoding="utf-8"))):
            bodies[m[1].lower()] = m[4]
    return bodies


def test_the_rls_helper_reads_memberships_never_invites() -> None:
    """``user_company_ids_with_role`` backs every membership policy (0052).

    Its sibling ``user_belongs_to_company`` was created out of band and no migration
    defines it (0026's header), so it can't be read here. Its live body, read
    2026-11-11: ``select exists (select 1 from public.company_users where company_id =
    check_company_id and user_id = auth.uid())``: company_users only.
    """
    body = _latest_functions()["user_company_ids_with_role"]
    assert re.search(r"\bcompany_users\b", body, re.I)
    assert not _INVITES.search(body)


def test_the_only_sql_function_writing_a_membership_is_the_new_users_own() -> None:
    writers = {name for name, body in _latest_functions().items() if _WRITE.search(body)}
    assert writers == {"handle_new_user"}
    body = _latest_functions()["handle_new_user"]
    insert = re.search(
        r"insert into public\.company_users\s*\(user_id, company_id, role\)\s*"
        r"values\s*\(([^)]*)\)",
        body,
        re.I,
    )
    assert insert is not None
    user_id, company_id = (v.strip().lower() for v in insert[1].split(",")[:2])
    assert (user_id, company_id) == ("new.id", "new.id")
