"""Every "contact support" a customer can read names an address they can write to.

Five backend messages (2026-09-16) said "contact support" and nothing else — a
409 on a multi-workspace account, a 403 with no company, a locked A2P
registration, the daily AI budget, and a Leasing gate that could not probe.
Every other message in the product spelled out ``team@occupella.com``. A
customer reading "contact support" on a screen with no address, no link and no
chat widget concludes there is no support — the founder's "looks like there was
no fallback" shape (gotchas 102/106/118: an instruction that cannot be carried
out is worse than none).

Three guards, because each failure is invisible to the other two:

* the CORPUS — every production string literal in ``src/helixis`` and
  ``frontend/src`` that tells somebody to contact support must also carry an
  address. The failure mode is "silently permits more" (a sixth message written
  tomorrow), so this reads the code rather than the fix (gotcha 45). It is an
  AST walk over string literals, never a line scan, because both homes quote
  the forbidden phrase in their docstrings in order to explain the rule
  (gotchas 40/59/91/114/124).
* the TWO HOMES agree — ``helixis.common.support.SUPPORT_EMAIL`` and
  ``frontend/src/lib/support.ts`` must name the same inbox. Two constants
  nothing compares is how they drift into two inboxes (gotcha 28).
* the CALL SITES — each of the six fixed messages reads the one home. A
  behavioural test of ``contact_support`` cannot see a route that stops
  calling it (gotchas 62/66/69/103).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from helixis.common.support import SUPPORT_EMAIL, contact_support

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "src" / "helixis"
FRONTEND = REPO / "frontend" / "src"
FRONTEND_HOME = FRONTEND / "lib" / "support.ts"

# The SHAPES of the defect, not one spelling: anything that tells the reader to
# get hold of support. A string that carries an "@" beside it has named the
# address and is fine.
_ASKS_FOR_SUPPORT = re.compile(
    r"\b(contact|reach out to|get in touch with|email|write to)\s+(our\s+)?support\b"
    r"|\bsupport team\b",
    re.IGNORECASE,
)

# The six sites this fix touched. Each entry: (path, function, what it must
# reference). A site that quietly reverts to a bare "contact support" literal
# fails the corpus guard; a site that keeps the words but stops reading the one
# home (a second hardcoded address) fails THIS one.
BACKEND_SITES = (
    ("api/auth.py", "require_unambiguous_company"),
    ("api/routes/buildium.py", "_require_company"),
    ("api/routes/a2p.py", "save_registration"),
    ("api/routes/agent.py", "run_agent"),
)
FRONTEND_SITES = (
    "components/SettingsPage.tsx",
    "components/LeasingComplianceGate.tsx",
)


# ── the helper ──────────────────────────────────────────────────────────


def test_contact_support_always_carries_the_address() -> None:
    assert SUPPORT_EMAIL in contact_support()
    assert SUPPORT_EMAIL in contact_support("if you need more.")
    assert "@" in SUPPORT_EMAIL, "the one home must be an email address, not a label"


def test_contact_support_appends_the_reason_after_the_address() -> None:
    line = contact_support("if you need more.")
    assert line.index(SUPPORT_EMAIL) < line.index("if you need more.")
    assert contact_support() == f"contact {SUPPORT_EMAIL}"


# ── the two homes agree ─────────────────────────────────────────────────


def _frontend_address() -> str:
    src = FRONTEND_HOME.read_text(encoding="utf-8")
    m = re.search(r"export const SUPPORT_EMAIL\s*=\s*['\"]([^'\"]+)['\"]", src)
    assert m, f"{FRONTEND_HOME} no longer exports SUPPORT_EMAIL"
    return m.group(1)


def test_backend_and_frontend_name_the_same_inbox() -> None:
    assert _frontend_address() == SUPPORT_EMAIL


# ── the corpus ──────────────────────────────────────────────────────────


def _python_string_literals(path: Path) -> list[tuple[int, str]]:
    """Every string LITERAL in a module, docstrings excluded, f-strings as
    templates (gotcha 63 — a JoinedStr's fragments are walked twice by
    ast.walk, so the template is judged and its pieces are skipped)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_ids.add(id(body[0].value))
    fragment_ids: set[int] = set()
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                    fragment_ids.add(id(v))
                else:
                    parts.append("{}")
            out.append((node.lineno, "".join(parts)))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
            and id(node) not in fragment_ids
        ):
            out.append((node.lineno, node.value))
    return out


_TS_LINE_COMMENT = re.compile(r"^\s*(//|\*|/\*)")


def _frontend_prose_lines(path: Path) -> list[tuple[int, str]]:
    """Source lines with comment-only lines dropped. TSX prose lives in JSX
    text and string literals, which a JS parser would separate exactly; here a
    line that starts with a comment marker is the only exemption, because
    ``lib/support.ts``'s own JSDoc quotes the forbidden phrase to explain it."""
    out: list[tuple[int, str]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if _TS_LINE_COMMENT.match(line):
            continue
        out.append((i, line))
    return out


def _is_frontend_production(path: Path) -> bool:
    name = path.name
    return not (".test." in name or ".spec." in name or "/__tests__/" in str(path))


def test_every_backend_support_mention_names_an_address() -> None:
    examined = 0
    offenders: list[str] = []
    for path in sorted(BACKEND.rglob("*.py")):
        for lineno, text in _python_string_literals(path):
            examined += 1
            if _ASKS_FOR_SUPPORT.search(text) and "@" not in text:
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: {text!r}")
    # A scan that finds zero literals is a wrong glob, not a clean corpus
    # (gotchas 49/100/101).
    assert examined > 5000, f"only {examined} string literals examined — wrong root?"
    assert not offenders, (
        "a customer-facing message tells the reader to contact support without "
        "saying where. Use helixis.common.support.contact_support() or spell "
        "SUPPORT_EMAIL into the sentence:\n" + "\n".join(offenders)
    )


def test_every_frontend_support_mention_names_an_address() -> None:
    examined = 0
    offenders: list[str] = []
    for path in sorted(FRONTEND.rglob("*.ts*")):
        if not _is_frontend_production(path):
            continue
        for lineno, line in _frontend_prose_lines(path):
            examined += 1
            if _ASKS_FOR_SUPPORT.search(line) and "@" not in line and "SUPPORT_EMAIL" not in line:
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()!r}")
    assert examined > 20000, f"only {examined} lines examined — wrong root?"
    assert not offenders, (
        "a customer-facing message tells the reader to contact support without "
        "saying where. Render {SUPPORT_EMAIL} from lib/support:\n" + "\n".join(offenders)
    )


def test_the_corpus_scan_can_see_the_shape_it_guards() -> None:
    """The canary: the pattern must match the sentences the fix removed, and
    must NOT match the docstrings that quote them — otherwise a green run
    proves nothing (gotchas 40/59)."""
    assert _ASKS_FOR_SUPPORT.search("Sign in again or contact support.")
    assert _ASKS_FOR_SUPPORT.search("Contact support if something needs to change.")
    assert _ASKS_FOR_SUPPORT.search("if it keeps happening, contact support.")
    assert _ASKS_FOR_SUPPORT.search("reach out to our support team")
    # An address beside the words is the fixed shape, and the guard must let
    # it through — a guard that fires on the fix gets switched off.
    literals = dict(_python_string_literals(BACKEND / "common" / "support.py"))
    assert not any(_ASKS_FOR_SUPPORT.search(t) and "@" not in t for t in literals.values())
    # And the docstring that quotes the phrase is excluded, not merely tolerated.
    src = (BACKEND / "common" / "support.py").read_text(encoding="utf-8")
    assert '"contact support" with no address' in src, "the docstring moved — re-point"


# ── the call sites ──────────────────────────────────────────────────────


def _function(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found — re-point the guard")


def _reads_the_one_home(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == "SUPPORT_EMAIL":
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "contact_support"
        ):
            return True
    return False


def _imports_from_support(tree: ast.Module) -> bool:
    return any(
        isinstance(n, ast.ImportFrom) and n.module == "helixis.common.support"
        for n in ast.walk(tree)
    )


def test_each_fixed_backend_site_reads_the_one_home() -> None:
    for rel, fn_name in BACKEND_SITES:
        tree = ast.parse((BACKEND / rel).read_text(encoding="utf-8"))
        assert _imports_from_support(tree), f"{rel} no longer imports helixis.common.support"
        fn = _function(tree, fn_name)
        assert _reads_the_one_home(fn), (
            f"{rel}::{fn_name} builds its support sentence without SUPPORT_EMAIL / "
            "contact_support — a second copy of the address is the drift this guard exists for"
        )


def test_each_fixed_frontend_site_imports_the_one_home_and_keeps_no_copy() -> None:
    for rel in FRONTEND_SITES:
        src = (FRONTEND / rel).read_text(encoding="utf-8")
        assert re.search(r"import \{ SUPPORT_EMAIL \} from ['\"]\.\./lib/support['\"]", src), (
            f"{rel} no longer imports SUPPORT_EMAIL from lib/support"
        )
        assert not re.search(r"const SUPPORT_EMAIL\s*=", src), (
            f"{rel} carries its own SUPPORT_EMAIL — two homes drift into two inboxes"
        )
        assert re.search(r"[{$]\{?SUPPORT_EMAIL\}?", src), (
            f"{rel} imports SUPPORT_EMAIL and never renders it"
        )
