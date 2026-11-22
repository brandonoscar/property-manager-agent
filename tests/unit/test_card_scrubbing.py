"""Identifiers reach the screen through cards, and nothing was scrubbing them.

``scrub_reply_text`` has guarded the PROSE answer since #897. It never touched
the card payloads, because while ``show_table`` was the only renderer the
tables were small and mostly numeric. #936 registered four more renderers on
the router, and a measured turn now puts **2,730 characters on screen against
380 in the stored prose** (gotcha 56) — so the cards are the answer, and the
one surface with an identifier rule was the smaller half.

Two properties are pinned here, deliberately separately:

1. **The transform.** ``scrub_card_payload`` walks a nested payload, scrubs
   string VALUES, and leaves keys, ints and floats alone. A scrubbed key
   silently renames a column the frontend switches on; a scrubbed number is a
   changed figure, which is worse than a leaked id.

2. **The wiring — every drain that publishes a card.** Gotcha 25's rule
   ("fix a field-shape bug at EVERY layer that reads it") applied to a
   transform: the orchestrator drains cards in TWO places (visuals and the
   generic card queue), and the eval harness drains them in a THIRD. A scrub
   on one of the three is a leak on the other two, and looks identical in
   review. AST, not substrings — ``ruff format`` reflows these call sites
   (gotcha 40).

⚠ The eval drain is in the list on purpose. An eval that reads the RAW queue
measures bytes no client ever receives: a ``card_must_not_contain`` would pass
against an unwired scrubber, and — the direction that ships bugs — a
``card_must_contain`` would fail on a value the live path keeps. Golden case
140 asserts on card content; it can only mean anything if the harness reads the
side the user is on.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from helixis.agents.reply_hygiene import scrub_card_payload

# ── 1. the transform ─────────────────────────────────────────────────────


def test_a_string_value_is_scrubbed() -> None:
    out = scrub_card_payload({"title": "Delinquency for Lease #99797"})
    assert "99797" not in out["title"]


def test_keys_are_never_scrubbed() -> None:
    """⚠ The frontend switches on these names. A key that looks like an
    identifier — and column keys routinely do — must survive verbatim or the
    card renders blank, which is a worse outcome than the leak."""
    out = scrub_card_payload({"lease_99797": "ok", "title": "fine"})
    assert set(out) == {"lease_99797", "title"}


def test_numbers_pass_through_untouched() -> None:
    """A balance is a fact the manager acts on. Rewriting one to hide an id
    would be inventing a figure — this product's worst failure mode pointed at
    money (gotchas 25/41)."""
    out = scrub_card_payload({"balance": 1240.0, "count": 99797, "flag": True})
    assert out == {"balance": 1240.0, "count": 99797, "flag": True}


def test_it_recurses_through_lists_and_dicts() -> None:
    out = scrub_card_payload({"rows": [{"who": "Desmond Achebe (Lease #99797)", "amount": 412.75}]})
    row = out["rows"][0]
    assert "99797" not in row["who"]
    assert "Desmond Achebe" in row["who"]
    assert row["amount"] == 412.75


def test_a_real_person_and_a_real_address_survive() -> None:
    """The other direction (gotcha 38). A scrubber that eats the answer is not
    a safer scrubber — it is a different bug wearing the same badge."""
    out = scrub_card_payload({"rows": [{"tenant": "Marisol Vega", "where": "Honolulu, HI 96813"}]})
    row = out["rows"][0]
    assert row["tenant"] == "Marisol Vega"
    assert "96813" in row["where"], "a ZIP is not a record number"


# ── 1b. the round trip over every real builder ───────────────────────────
#
# ⚠ THIS IS THE TEST THAT FOUND THE BUG, and the hand-written cases above
# would never have. The first cut of ``scrub_card_payload`` scrubbed every
# string value, and ``scrub_reply_text`` de-jargonises snake_case for prose —
# so ``{"kind": "agent_table"}`` became ``"agent table"`` and every table
# would have shipped as a card the frontend cannot render. Building the REAL
# payloads and diffing them is what surfaced it; asserting on payloads I
# typed out myself only ever proves I remembered the fields I was thinking
# about (gotcha 45: measure the corpus, not the function).


def _payloads() -> dict[str, dict]:
    """One of every card that can reach a drain, carrying identifiers in the
    places the mirror rollups actually put them (``Lease #…`` labels, a
    ``Bill #…`` footnote)."""
    from helixis.agents import visual_tools as vt

    return {
        "table": vt.build_table_payload(
            "Delinquency (Lease #99797)",
            ["Tenant", "Total_Spend"],
            [["Marisol Vega", "$1,240.00"]],
        ),
        "detail_card": vt.build_detail_card_payload(
            "Lease #99797",
            "Redbud Court",
            "Past due",
            "warn",
            [["Balance", "$1,240.00"]],
            "Bill #12345",
        ),
        "chart": vt.build_chart_payload("Spend for Lease #99797", "bar", [["Apex", "18450"]], "$"),
        "schedule": vt.build_schedule_payload(
            "Visits", "2026-09-01", [["9am", "Redbud", "proposed"]]
        ),
        "checklist": vt.build_checklist_payload(
            "Steps", [["Call the vendor about Lease #99797", "false"]]
        ),
        "next_steps": vt.build_next_steps_payload(
            [["Send the notice", "Draft a notice for Lease #99797"]]
        ),
    }


# ⚠ THESE ASSERT AGAINST THE FRONTEND'S CONTRACTS, NEVER AGAINST
# ``_STRUCTURAL_KEYS``. My first version of this block collected "every value
# under a structural key" by importing that set from the module under test —
# so emptying the set (which IS the bug: restoring it makes ``kind`` read
# ``"agent table"`` and every table unrenderable) made the collector find
# nothing on either side and compare ``[] == []``. It passed the mutation at
# full confidence. A guard that reads its own definition of what to check from
# the thing it is checking cannot fail; gotcha 45's rule, arriving inside a
# test rather than a cost gate. Each check below names a value the CLIENT
# matches, so the denylist has to actually work for them to hold.


def test_the_table_discriminator_and_cell_kinds_survive() -> None:
    """``ConversationThread.tsx`` switches on ``kind``. ``"agent table"`` is
    not a case it has, so the card renders as nothing at all — the loudest
    possible break, arriving silently from the server's point of view."""
    scrubbed = scrub_card_payload(_payloads()["table"])
    assert scrubbed["kind"] == "agent_table", scrubbed["kind"]
    for row in scrubbed["rows"]:
        for cell in row.values():
            assert cell["kind"] == "text", cell


def test_the_chart_kind_stays_one_the_renderer_knows() -> None:
    scrubbed = scrub_card_payload(_payloads()["chart"])
    assert scrubbed["kind"] in {"bar", "line"}, scrubbed["kind"]


def test_tone_and_slot_state_stay_inside_their_own_enums() -> None:
    """Both are validated at BUILD time against these sets, so a value outside
    them can only have arrived after the builder — i.e. from the scrub."""
    from helixis.agents.visual_tools import _CARD_TONES, _SLOT_STATES

    detail = scrub_card_payload(_payloads()["detail_card"])
    assert detail["tone"] in _CARD_TONES, detail["tone"]
    for slot in scrub_card_payload(_payloads()["schedule"])["slots"]:
        assert slot["state"] in _SLOT_STATES, slot


def test_a_next_step_chip_still_sends_what_it_promised() -> None:
    """The chip's ``prompt`` is the text clicking it SENDS. Scrubbed, "Draft a
    notice for Lease #99797" becomes "…for Lease", which is a follow-up
    question nothing can answer."""
    raw = _payloads()["next_steps"]
    scrubbed = scrub_card_payload(raw)
    assert [s["prompt"] for s in scrubbed["steps"]] == [s["prompt"] for s in raw["steps"]]


def test_every_card_still_resolves_its_own_columns() -> None:
    """The table's property, stated rather than assumed: the frontend does
    ``row[col.key]``, so every declared column key must still be a key in
    every row after the scrub."""
    scrubbed = scrub_card_payload(_payloads()["table"])
    keys = [c["key"] for c in scrubbed["columns"]]
    assert keys, "no columns survived"
    for row in scrubbed["rows"]:
        for key in keys:
            assert key in row, f"column {key!r} no longer resolves against {sorted(row)}"


def test_the_identifiers_are_still_gone_from_every_card() -> None:
    """The other half — the denylist must not have bought structure by giving
    up the scrub. Every card above carries a ``Lease #99797``-shaped label in
    a PROSE field, and none of them may keep it."""
    import json

    for name, payload in _payloads().items():
        rendered = json.dumps(scrub_card_payload(payload))
        if name == "next_steps":
            # The chip's ``prompt`` keeps it deliberately — see the
            # _STRUCTURAL_KEYS note. Its visible ``label`` must still be clean.
            labels = [s["label"] for s in scrub_card_payload(payload)["steps"]]
            assert not any("99797" in x for x in labels), labels
            continue
        assert "99797" not in rendered, f"{name} still carries the lease id"
        assert "12345" not in rendered, f"{name} still carries the bill id"


def test_it_is_idempotent() -> None:
    """The drain can run twice over the same queue on a retry."""
    for name, payload in _payloads().items():
        once = scrub_card_payload(payload)
        assert scrub_card_payload(once) == once, name


# ── 2. the wiring ────────────────────────────────────────────────────────


def _scrubbed_call_count(func) -> int:
    """How many times ``scrub_card_payload`` is CALLED in this function.

    AST, not a substring scan: ``ruff format`` wraps these call sites across
    lines, and a comment that names the function is not a call (gotcha 40).
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "scrub_card_payload"
    )


def test_the_orchestrator_scrubs_both_of_its_card_drains() -> None:
    """⚠ TWO drains, not one. ``run_agent_streaming`` publishes visuals from
    ``drain_pending_visuals`` and the wave-2 cards from ``drain_pending_cards``
    — different queues, different event types, the same screen. Scrubbing one
    is a leak that reviews clean."""
    from helixis.agents.orchestrator import run_agent_streaming

    count = _scrubbed_call_count(run_agent_streaming)
    assert count >= 2, (
        f"run_agent_streaming scrubs {count} of its 2 card drains — a card "
        "published from the unscrubbed one carries raw Buildium identifiers "
        "to the client"
    )


def test_the_eval_harness_reads_the_same_bytes_the_client_does() -> None:
    """Golden case 140 asserts on card content. If the harness drains the raw
    queue it is measuring a payload that never existed: the prohibition passes
    with the scrubber unwired, and a ``card_must_contain`` fails on a value the
    live path keeps."""
    from helixis.evals.agent_flow import run_agent_flow_case

    count = _scrubbed_call_count(run_agent_flow_case)
    assert count >= 2, (
        f"the eval card drain scrubs {count} of its 2 queues — golden card "
        "assertions would be measuring bytes no client receives"
    )
