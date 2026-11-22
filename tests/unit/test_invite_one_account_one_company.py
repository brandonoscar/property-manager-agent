"""One account, one company — and the invite must say whether mail went out.

Founder directive, 2026-09-03: "you only get one account... no one works for
two companies." That makes the shape of this simple, and it makes the previous
behaviour a bug rather than a design tradeoff.

WHAT WAS BROKEN
Signup ALWAYS auto-provisions a company, so inviting someone who had already
signed up left them in two. `_resolve_company_ids` then returns
``sorted(company_ids)`` and ~32 call sites take ``[0]`` — UUID order standing
in for user intent, with no switcher anywhere and the list never sent to the
client. Measured on helixis-test: a teammate account was added to My Company on
2026-08-31 and still resolved to their own empty shell, so the invite was
INERT. The admin saw them on the team roster; they saw an empty workspace.

Two halves, and they are independent:

* the DELETE, so the invitee ends up in exactly one company
* ``email_sent``, so the admin knows no mail is coming — Supabase refuses to
  invite an address that already has a login, so nothing was ever attempted.
  The toast deliberately does NOT say "ask them to log in": they already have
  an account, so signing in is the only thing they were ever going to do. The
  one fact the admin needs is that no delivery is on its way.

⚠ THE DELETE IS THE DANGEROUS HALF and the predicate is the whole safety
argument. "Auto-provisioned" is NOT "throwaway": EVERY company in this product
is created by the signup trigger with ``id = the founder's user id``, Blue
Ridge Properties included — 50 properties, 75 leases, 3 members. Keying the
delete on that would have destroyed a real customer's membership. The only
safe signal is EMPTY: no Buildium connection, no properties, no leases, and
nobody else in it. The tests below pin each of those four conditions
SEPARATELY, because a predicate that drops one of them still passes a test
that only checks the happy path.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROUTE = Path(__file__).resolve().parents[2] / "src" / "helixis" / "api" / "routes" / "company.py"


#: Every function the invite path runs through. The guard reads the UNION of
#: these, not one name.
#:
#: ⚠ It used to read `invite_member` alone, and on 2026-09-05 the per-address
#: work moved into `_invite_one` so that a BULK invite could not drift from a
#: single one. Every assertion below went red at once — which is the guard
#: working (gotchas 68/76: moving code re-points a source-keyed guard; re-point
#: it, never delete it) — but the failure mode to notice is the OTHER one:
#: `test_the_cleanup_runs_for_a_brand_new_invitee_too` asserts an absence, so it
#: went on passing against a function that no longer contained the code at all.
#: A name-keyed scan does not fail when its target moves; it goes blind. Hence
#: the union, and hence `_invite_source` asserting it found every name.
#: ⚠ Re-pointed again on 2026-11-11: an existing account now gets an invite that
#: waits for their acceptance (migration 0112), and the membership write and the
#: shell drop moved into `_add_fresh_invitee`, which only a brand-new account
#: reaches. The assertions below are unchanged.
_INVITE_PATH = ("invite_member", "_invite_one", "_add_fresh_invitee")


def _invite_source() -> str:
    """The source of every function on the invite path, joined.

    Scoped to those functions, not the whole file: this module's own docstrings
    and comments quote the predicate in order to explain it, and a whole-file
    scan would match the explanation rather than the code (gotcha 40/59).
    """
    text = ROUTE.read_text(encoding="utf-8")
    tree = ast.parse(text)
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name in _INVITE_PATH:
            found[node.name] = ast.get_source_segment(text, node) or ""
    missing = [name for name in _INVITE_PATH if name not in found]
    # ⚠ Assert the probe RAN (gotcha 33). A renamed function would otherwise
    # yield an empty string, and every `not in src` assertion here would pass
    # against code this guard can no longer see.
    assert not missing, (
        f"the invite path no longer contains {missing} — this guard is reading "
        "nothing. Re-point `_INVITE_PATH` at whatever the invite runs through "
        "now; do not delete the assertions."
    )
    return "\n".join(found[name] for name in _INVITE_PATH)


def _delete_statement() -> str:
    """The DELETE's SQL text, isolated from the rest of the handler."""
    src = _invite_source()
    assert "DELETE FROM public.company_users" in src, (
        "the invite route no longer removes the invitee's empty leftover company. "
        "Without it an invited user sits in two companies and `company_ids[0]` "
        "picks between them by UUID sort order — the defect that made the "
        "2026-08-31 invite inert."
    )
    start = src.index("DELETE FROM public.company_users")
    return src[start : src.index('"""', start)] if '"""' in src[start:] else src[start:]


def test_the_delete_requires_all_four_emptiness_conditions() -> None:
    """Each condition separately. Dropping any one of them destroys real data.

    This is the guard that matters. A predicate missing the members check
    would remove someone from a shared company; missing the properties or
    leases check would remove a real customer from their own books.
    """
    sql = _delete_statement()
    required = {
        "company_buildium_credentials": "a company with a live Buildium connection is not empty",
        "helixis_properties": "a company with mirrored properties is not empty",
        "helixis_leases": "a company with mirrored leases is not empty",
        "company_users o": "a company with ANOTHER member is somebody else's team",
    }
    missing = [f"{k} — {why}" for k, why in required.items() if k not in sql]
    assert not missing, (
        "the invite's DELETE no longer checks:\n  "
        + "\n  ".join(missing)
        + "\n\nEVERY company here is auto-provisioned at signup with id = the "
        "founder's user id, Blue Ridge Properties included (50 properties, 75 "
        "leases, 3 members). EMPTY is the only safe signal for 'nobody has used "
        "this'. Do not relax these."
    )
    assert "NOT EXISTS" in sql, "the emptiness conditions stopped being negative checks"


def test_the_delete_never_touches_the_company_being_invited_into() -> None:
    """Belt and braces on the obvious catastrophe.

    Without the ``company_id <> $2`` clause the route would add the invitee to
    the company and then, if that company happened to be empty, remove them
    again — an invite that silently undoes itself.
    """
    sql = _delete_statement()
    assert "cu.company_id <> $2" in sql, (
        "the DELETE no longer excludes the target company — an invite into a "
        "brand-new empty company would add the member and immediately remove them"
    )
    assert "cu.user_id = $1" in sql, "the DELETE is not scoped to the invited user"


def test_the_cleanup_runs_for_a_brand_new_invitee_too() -> None:
    """The hole in the first cut of this fix, and it was the common case.

    That version gated the cleanup on ``if not fresh_invite:``, reasoning that
    a brand-new invitee has nothing to clean up. Wrong: the signup trigger is
    ``after insert on auth.users`` and GoTrue's admin invite CREATES that row,
    so a first-time invitee is handed a personal workspace, then added to the
    inviting company, and lands in two exactly like an existing account. The
    gate meant the fix only covered people who had signed up before.

    A fresh invitee's shell is empty by construction, so running it there is
    if anything safer — and the target company is protected by ``<> $2``
    regardless, which is pinned separately above.
    """
    src = _invite_source()
    assert "if not fresh_invite:" not in src, (
        "the shell cleanup is gated on the invitee being an EXISTING account "
        "again. GoTrue's invite inserts into auth.users, which fires the signup "
        "trigger, so a brand-new invitee gets a personal workspace too — gating "
        "this leaves the common case in two companies."
    )


def test_the_admin_is_told_whether_mail_actually_went_out() -> None:
    """The half that fixes the reported symptom.

    `email_sent` is derived from `fresh_invite` — whether GoTrue accepted the
    invite — not from whether the membership row was written. Those differ in
    exactly the case that caused this: row created, no mail attempted.
    """
    src = _invite_source()
    assert "email_sent=fresh_invite" in src, (
        "email_sent is no longer tied to whether GoTrue actually sent mail. If it "
        "reports the membership INSERT instead, it says 'we emailed them' for the "
        "one case where nobody did."
    )


def test_the_response_model_defaults_to_mail_having_been_sent() -> None:
    """The safe default, and it is the opposite of what reads as 'careful'.

    A default of False would tell every admin on a stale client to chase a
    teammate who did receive an email. True matches both the pre-existing
    behaviour and the common path.
    """
    from helixis.api.routes.company import InviteResponse

    assert InviteResponse(email="a@b.co", role="member").email_sent is True
    assert InviteResponse(email="a@b.co", role="member", email_sent=False).email_sent is False


# ── the DELETE only reaches an account THIS invite created ──────────────
#
# ⚠ The four emptiness conditions above cannot tell a throwaway shell from a
# REAL company that is merely new. A customer who signed up an hour ago and has
# not connected Buildium has no credentials, no properties, no leases and no
# colleagues — they satisfy every one. Everyone is admin of their own signup
# company, `_gotrue_invite` takes an arbitrary email, and the fallback lookup is
# a GLOBAL `SELECT id FROM auth.users`. So before the `fresh_invite` gate, any
# account holder could invite that customer's address and delete their
# membership in their own workspace, leaving them in the attacker's.


def test_the_delete_only_runs_for_an_account_this_invite_created() -> None:
    sql = _delete_statement()
    assert "$3::boolean" in sql, (
        "the invite's DELETE is no longer gated on `fresh_invite`.\n\n"
        "Without that gate it deletes a membership belonging to somebody who "
        "ALREADY had an account — and a brand-new customer's real company is "
        "indistinguishable from an unused shell by the emptiness conditions "
        "alone. `fresh_invite` means GoTrue created the account on THIS "
        "request, so the shell it triggered is seconds old and nobody has ever "
        "used it. That is the one case where removing another person's "
        "membership is safe."
    )


def test_the_delete_only_reaches_a_self_named_signup_shell() -> None:
    """Second, independent condition: the signup trigger creates every
    auto-provisioned company with `companies.id = auth.users.id`, so
    `cu.company_id = cu.user_id` identifies the invitee's OWN shell. A company
    somebody else created can never match, whatever its contents."""
    sql = _delete_statement()
    assert "cu.company_id = cu.user_id" in sql, (
        "the DELETE no longer restricts itself to the invitee's own "
        "auto-provisioned shell (companies.id = auth.users.id, per the signup "
        "trigger). Without it the predicate can reach a company created by "
        "somebody else."
    )


def test_the_fresh_invite_flag_is_what_is_actually_bound() -> None:
    """⚠ Reads BOTH sides (gotcha 28). The two tests above pin the SQL TEXT;
    this pins that the third parameter carries `fresh_invite` and not, say, a
    literal True — which would satisfy every text-level assertion above and
    re-open the hole exactly.

    Walks the AST rather than slicing the source: the argument list is a Call
    node no reflow can split, whereas an index-from-the-DELETE-string scan
    breaks the first time ruff moves a line (gotcha 40).
    """
    tree = ast.parse(
        (Path(__file__).resolve().parents[2] / "src/helixis/api/routes/company.py").read_text(
            encoding="utf-8"
        )
    )
    deletes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and "DELETE FROM public.company_users" in node.args[0].value
    ]
    assert len(deletes) == 1, f"expected one shell-purge DELETE call, found {len(deletes)}"
    names = [a.id for a in deletes[0].args[1:] if isinstance(a, ast.Name)]
    assert "fresh_invite" in names, (
        "the DELETE's parameters are "
        f"{names} — `fresh_invite` is not among them. The SQL can say "
        "`$3::boolean` and still be handed a literal True, which passes every "
        "text-level guard here and restores the original defect."
    )


def test_the_resolver_ordering_is_the_other_half() -> None:
    """⚠ An existing-account invitee now KEEPS both memberships, which is the
    a teammate account state. That is survivable only because `_resolve_company_ids`
    orders the caller's own shell last. These two changes are one fix; removing
    the ordering silently re-breaks invites for every existing account."""
    auth_src = (Path(__file__).resolve().parents[2] / "src/helixis/api/auth.py").read_text(
        encoding="utf-8"
    )
    assert "cid == user_id" in auth_src, (
        "`_resolve_company_ids` no longer de-prioritises the caller's own "
        "signup shell. The invite DELETE was narrowed on the assumption that "
        "it does — an existing-account invitee keeps two memberships and would "
        "resolve to the empty one by UUID sort, which is the exact defect that "
        "made a real customer's workspace blank for weeks."
    )
