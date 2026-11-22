"""Every id a WRITE requires must be emitted by some READ.

The seam, not either side. Both halves can be individually correct and still
not compose — which is exactly what happened on 2026-08-23:

* ``buildium_record_move_out`` requires ``tenant_id``. ``buildium_get_lease``
  — the tool that answers "who is on this lease" — rendered tenant NAMES and
  emails and no id. The agent got something the write could not accept, hunted
  for the id, and exhausted its step budget. Reproducible, on a live write
  path, and invisible to every unit test because nothing exercised
  read-then-write end to end.
* ``buildium_list_gl_accounts`` rendered ``#{account_number}`` while
  ``post_charge`` / ``record_payment`` / ``renew_lease`` all require
  ``gl_account_id``. Worse than the tenant case: an account NUMBER looks like
  an id, so the model had a plausible wrong value and no correct one — and
  gotcha 22 records that Buildium validates GL *presence*, not *existence*, so
  a wrong ``GlAccountId`` is ACCEPTED. Wrong account, real charge, no error.

Same family as gotcha 28 (a card string vs the EventType enum): a value
crossing a producer/consumer boundary needs a test that reads BOTH sides.

⚠ Two rendering idioms exist — ``[ref:123]`` and ``(id: 123)``. That
inconsistency is itself why no single grep found this; the check accepts
either, and ``_renders_id`` is the one place that knowledge lives.

This is CLASSIFICATION-COMPLETE, in the shape of ``test_buildium_purge.py``:
every write-required id must appear in ``ID_SOURCES`` with either a read tool
that emits it or an explicit reason it needs none. Adding a write tool with a
new id kind FAILS until someone decides which. That is the point — the tenant
bug shipped because nobody was asked the question.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re

import pytest

from helixis.agents import buildium_tools

_SRC = pathlib.Path(inspect.getfile(buildium_tools)).read_text()
_TREE = ast.parse(_SRC)
_LINES = _SRC.splitlines()

#: Write verbs, matching the negative-safety set in test_negative_should_not_act.
_WRITE_VERBS = ("create", "update", "close", "post", "record", "add", "upload", "share", "renew")

#: id kind -> the read impl that emits it, or None + a reason it needs no read.
ID_SOURCES: dict[str, tuple[str | None, str]] = {
    "lease_id": ("_get_lease_impl", "rendered as [ref:lease.id]"),
    "tenant_id": ("_get_lease_impl", "rendered as [ref:t.id] on each current tenant"),
    "gl_account_id": ("_list_gl_accounts_impl", "rendered as [ref:a.id]"),
    "work_order_id": ("_get_work_order_impl", "rendered as [ref:wo.id]"),
    "vendor_id": ("_list_vendors_impl", "rendered as (id: v.id)"),
    "assigned_to_user_id": ("_list_staff_impl", "rendered as (id: u.id)"),
    "task_id": ("_list_tasks_impl", "rendered as (id: t.id)"),
    # ── deliberately sourced from the USER or a prior tool result, not a read
    "property_id": (None, "resolved by name via _resolve_property, never typed by the model"),
    "unit_id": (None, "resolved by number/address via _resolve_unit, never typed by the model"),
    "target_id": (None, "polymorphic note target; the caller already holds the id it just read"),
    "entity_id": (None, "polymorphic upload target; paired with entity_type by the caller"),
    "file_id": (None, "returned by the upload that created it, in the same turn"),
    "applicant_id": (None, "applicant tools are DARK (fair-housing, gotcha in §5)"),
    "application_id": (None, "applicant tools are DARK (fair-housing, gotcha in §5)"),
}


def _write_impls() -> dict[str, list[str]]:
    """name -> required *_id params, for every write ``*_impl``."""
    out: dict[str, list[str]] = {}
    for n in ast.walk(_TREE):
        if not isinstance(n, ast.AsyncFunctionDef) or not n.name.endswith("_impl"):
            continue
        if not any(v in n.name for v in _WRITE_VERBS):
            continue
        ids = [a.arg for a in n.args.args + n.args.kwonlyargs if a.arg.endswith("_id")]
        if ids:
            out[n.name] = ids
    return out


def _body(name: str) -> str:
    for n in ast.walk(_TREE):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name:
            return "\n".join(_LINES[n.lineno - 1 : (n.end_lineno or n.lineno)])
    raise AssertionError(f"no such impl: {name}")


def _renders_id(body: str, var: str) -> bool:
    """Does this body emit ``var``'s id, in any live idiom?

    Four shapes exist in this file and all four are real:

        [ref:{lease.id}]                    [ref:{getattr(t, 'id', '?')}]
        (id: {getattr(v, 'id', '?')})       (id: {u.id})

    ⚠ The first cut of this matcher took a dotted ``x.id`` and missed every
    ``getattr`` form — it reported FIVE working renders as missing, including
    two I had just written. Accepting only one idiom is precisely how a real
    render reads as absent, which is the bug class this file exists for.
    Matching on the VARIABLE rather than a spelling is what makes it robust.
    """
    v = re.escape(var)
    attr = rf"(?:{v}\.id\b|getattr\(\s*{v}\s*,\s*['\"]id['\"])"
    return bool(
        re.search(rf"\[ref:\{{[^}}]*{attr}", body) or re.search(rf"id:\s*\{{[^}}]*{attr}", body)
    )


# ── the seam ──────────────────────────────────────────────────────────────


def test_every_write_required_id_is_classified() -> None:
    """A new write tool with an unclassified id kind fails HERE, not in
    production. The tenant bug shipped because nobody was asked."""
    required = {i for ids in _write_impls().values() for i in ids}
    unclassified = sorted(required - set(ID_SOURCES))
    assert not unclassified, (
        f"write tools require id kinds with no entry in ID_SOURCES: {unclassified}. "
        "Add each one: name the read tool that emits it, or state why no read is needed."
    )


def test_no_stale_classifications() -> None:
    """The inverse: an entry for an id no write needs any more is dead weight
    that makes the map look more complete than it is."""
    required = {i for ids in _write_impls().values() for i in ids}
    stale = sorted(set(ID_SOURCES) - required)
    assert not stale, f"ID_SOURCES entries no write requires: {stale}"


@pytest.mark.parametrize(
    "id_kind,source",
    [(k, v[0]) for k, v in ID_SOURCES.items() if v[0] is not None],
)
def test_the_named_read_actually_emits_that_id(id_kind: str, source: str) -> None:
    """Reads BOTH sides. Naming a source is a claim; this checks it.

    ``get_lease`` DID name itself the source of lease data while rendering no
    tenant id — a classification alone would have looked fine.
    """
    var = {
        "lease_id": "lease",
        "tenant_id": "t",
        "gl_account_id": "a",
        "work_order_id": "wo",
        "vendor_id": "v",
        "assigned_to_user_id": "u",
        "task_id": "t",
    }[id_kind]
    assert _renders_id(_body(source), var), (
        f"{source} is named as the source of {id_kind} but renders no id for `{var}`. "
        "A read that hides the identifier its write consumes is a dead end for the agent."
    )


def test_the_two_regressions_specifically() -> None:
    """Named, so a future refactor that drops either is unmistakable rather
    than a generic parametrised failure."""
    lease = _body("_get_lease_impl")
    assert "[ref:" in lease and "t, 'id'" in lease, (
        "get_lease must emit each current tenant's id — record_move_out needs it"
    )
    gl = _body("_list_gl_accounts_impl")
    assert "a, 'id'" in gl, (
        "list_gl_accounts must emit the account id, not only the account NUMBER — "
        "post_charge/record_payment/renew_lease need the id, and Buildium accepts "
        "a wrong one silently (gotcha 22)"
    )


# ── the guard must be able to fail (guard self-test) ──────────────────────


def test_the_seam_check_detects_a_missing_id() -> None:
    """Guard self-test. Three guards today passed against code they were
    written to catch — the dead E2B sandbox, an AST scan run on unfixed
    source, and a fixed-width window that read 0 as 'absent'. A guard nobody
    has watched fail is not evidence.

    Feeds ``_renders_id`` the pre-fix rendering and requires a NEGATIVE.
    """
    pre_fix_lease = 'lines.append(f"    - {_tenant_full_name(t)}{email_part}")'
    assert not _renders_id(pre_fix_lease, "t"), (
        "the seam check cannot see the tenant bug it was written for"
    )
    pre_fix_gl = 'lines.append(f"- {prefix}{name}{suffix}")'
    assert not _renders_id(pre_fix_gl, "a"), (
        "the seam check cannot see the GL-account bug it was written for"
    )
    # And it must accept BOTH live idioms, or a real render reads as missing.
    assert _renders_id('f"- {n} [ref:{lease.id}]"', "lease")
    assert _renders_id("f\"- {n} (id: {getattr(v, 'id', '?')})\"", "v")
    assert _renders_id("f\"- {n} [ref:{getattr(t, 'id', '?')}]\"", "t")
