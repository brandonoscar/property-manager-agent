"""Every refusal reason the server can emit exists in the client's union.

⚠ THIS GUARD WAS CLAIMED AND DID NOT EXIST. `BillingBlock.reason`'s docstring
said *"`test_billing_plans.py` reads both sides so a reason added here cannot
go unrepresented there (gotcha 28)"*. That file compares PLAN KEYS against the
client and has never looked at a reason code. So the sentence invoking the
both-sides rule was itself gotcha 48's shape — a claim in prose, which cannot
fail — and it went on being believed until somebody added a reason.

`subscription_unpaid` is that somebody, which is the argument for the file: the
missing guard was found by the exact change it existed to catch, and only
because the reason list happened to be read on the way past.

The union is a TYPE, not a renderer, so drift does not blank a screen — the
message text is server-owned. What it costs is quieter and worse: a client
comparing against a literal the union does not carry is a `tsc` error on
correct code, and a union that silently under-describes the server is
documentation that lies to whoever writes the next panel.

⚠ AST on the Python side (gotcha 40), and the reason is concrete here: two of
these strings appear in `gate.py` COMMENTS explaining why a reason is not used
for a given status, so a substring scan reads the explanation as a call site.
A comment is never a `Call` node.

⚠ The TypeScript side cannot be parsed. `typescript` at this version is the
Go-native port and exports only `version`; no other parser is installed. So the
union is read with a scoped regex, and the block comments INSIDE the union —
which name other reasons in prose — are stripped first. The stripper is tested
in both directions, which is what gotcha 40 actually objects to: not regexes,
but a text scan nobody proved can say no.
"""

from __future__ import annotations

import ast
import pathlib
import re

_GATE = pathlib.Path("src/helixis/billing/gate.py")
_CLIENT = pathlib.Path("frontend/src/services/billing.ts")


def _server_reasons() -> set[str]:
    """Every literal first argument to a `BillingBlock(...)` call in gate.py."""
    tree = ast.parse(_GATE.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "BillingBlock" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.add(first.value)
    return found


def _strip_block_comments(text: str) -> str:
    """Remove `/* … */`, so prose naming a reason is not read as a member."""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _client_reasons() -> set[str]:
    """The members of the exported `BillingBlock` union in billing.ts."""
    src = _CLIENT.read_text(encoding="utf-8")
    # Scoped to the DECLARATION, never the file (gotcha 59's rule pointed at
    # TypeScript): every one of these strings appears elsewhere in the module's
    # own documentation.
    match = re.search(r"export type BillingBlock\s*=(.*?);", src, flags=re.DOTALL)
    assert match, "the BillingBlock union has moved or been renamed in billing.ts"
    return set(re.findall(r"'([a-z_]+)'", _strip_block_comments(match.group(1))))


def test_the_scanners_find_something() -> None:
    """A bad path or a renamed symbol collects zero and every check below
    passes vacuously — the failure mode gotcha 45 names, arriving at the guard
    rather than at the code."""
    server, client = _server_reasons(), _client_reasons()
    assert len(server) >= 5, f"gate.py yielded {server} — the AST walk is not finding call sites"
    assert len(client) >= 5, f"billing.ts yielded {client} — the union scan is not finding members"


def test_every_server_reason_exists_in_the_client_union() -> None:
    missing = _server_reasons() - _client_reasons()
    assert not missing, (
        f"gate.py can emit {sorted(missing)} and frontend/src/services/billing.ts's "
        "BillingBlock union does not carry it. The union is what the client is "
        "allowed to compare against, so an unlisted reason is a tsc error on "
        "correct code and a type that under-describes the server."
    )


def test_the_client_union_invents_no_reason_the_server_cannot_send() -> None:
    """The other direction, which is the one that rots quietly. A member left
    behind after a reason is retired reads as a state the product can reach."""
    extra = _client_reasons() - _server_reasons()
    assert not extra, (
        f"billing.ts declares {sorted(extra)} and gate.py never emits it — either "
        "the reason was retired and the union kept it, or it is spelled "
        "differently on one side."
    )


def test_the_unpaid_reason_is_distinct_from_a_cancellation() -> None:
    """The specific defect this pass fixed, pinned by NAME.

    `unpaid` means Stripe stopped collecting and left an OPEN INVOICE on a
    subscription that still exists. Folding it into `subscription_canceled`
    sends the customer to the plan picker, which mints a second subscription —
    and `create_checkout` 409s on exactly that, so the instruction fails. It
    needs its own reason because it needs a different action.
    """
    reasons = _server_reasons()
    assert "subscription_unpaid" in reasons
    assert "subscription_canceled" in reasons


def test_the_comment_stripper_can_say_no_in_both_directions() -> None:
    """Guard-the-guard. A stripper that removes everything passes the union
    check by finding nothing; one that removes nothing reads the doc comments'
    prose as members. Both are silent."""
    kept = _strip_block_comments("| 'a'\n/** mentions 'b' here */\n| 'c'")
    assert "'a'" in kept and "'c'" in kept, "the stripper is eating real members"
    assert "'b'" not in kept, "the stripper is leaving comment prose behind"
