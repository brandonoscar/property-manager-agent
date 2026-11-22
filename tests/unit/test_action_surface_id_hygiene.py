"""No Buildium internal identifiers in the action surface's prose.

The summarizer has been sanitized since PR #92; the action surface never was,
and it is just as user-facing. Pulled from the live Inbox 2026-08-25: 13
identifier leaks across the cards and **9 of them were here**.

The strings below are VERBATIM from those cards. A ``draft`` is the worst
place for a leak, because it is the pre-filled body of a note or a message —
an un-edited click sends "task 39503" to a vendor or a resident.
"""

from __future__ import annotations

import pytest

from helixis.agent.planner.id_hygiene import (
    action_surface_id_leaks,
    scrub_action_surface,
    scrub_surface_dict,
)
from helixis.agent.planner.models import (
    ActionSurface,
    FollowupOption,
    FollowupQuestion,
    PrimaryAction,
    ProposedAction,
)


def _leaky_surface() -> ActionSurface:
    """A surface carrying the exact prose the live cards shipped."""
    return ActionSurface(
        summary=(
            "Work order 9023, 'Water heater leaking into unit below', has no "
            "property or unit link and has sat in New status for 29 days."
        ),
        summary_highlights=["Work order 9023", "New status for 29 days"],
        freeform_placeholder="e.g. 'route work order 9023 to a plumber'",
        signals=[
            "Work order 9023 has property_id and unit_id both null",
            "unit 101 shows a payments_3mo figure of -900",
        ],
        actions=[
            ProposedAction(
                tool_id="add_internal_note",
                label="Flag task 39503 for dispatch",
                intent="Note that task 39503 was never dispatched to a vendor.",
                # ⚠ The ids the handler EXECUTES on. Must survive verbatim.
                parameters={"task_id": 39503, "property_id": 37975},
                rationale="Prior task 39503 is still status New after a week.",
                triggers=[
                    "Prior task 39503 'Kitchen faucet dripping constantly' is still New",
                    "Work order 9023 has property_id and unit_id both null",
                ],
                alternative="Considered updating task 39503 directly.",
                primary_action=PrimaryAction(
                    label="Add note to task 39503",
                    kind="create_note",
                    draft=(
                        "This is a follow-up on task 39503 ('Kitchen faucet dripping "
                        "constantly'), which was never dispatched to a vendor."
                    ),
                ),
                first_followup=FollowupQuestion(
                    prompt="Which vendor should take task 39503?",
                    options=[
                        FollowupOption(label="Acme Plumbing", parameter_patch={"vendor_id": 7}),
                        FollowupOption(label="Whoever took task 39503", parameter_patch={}),
                    ],
                ),
            )
        ],
    )


def test_the_fixture_actually_leaks() -> None:
    """Otherwise every assertion below is vacuous.

    A scrub test whose input was already clean passes against a scrubber that
    does nothing — the failure mode gotcha 35 names.
    """
    leaks = action_surface_id_leaks(_leaky_surface())
    paths = {path for path, _ in leaks}
    assert len(leaks) >= 8, f"fixture is not leaky enough: {leaks}"
    # Every prose surface the frontend renders is represented.
    assert "summary" in paths
    assert "freeform_placeholder" in paths
    assert any(p.startswith("signals[") for p in paths)
    assert any(p.endswith(".draft") for p in paths)
    assert any(".triggers[" in p for p in paths)
    assert any(".first_followup." in p for p in paths)


def test_scrub_removes_every_leak() -> None:
    """The inspector reads the same fields the scrubber writes, so a model
    field added and missed shows up here rather than shipping quietly."""
    scrubbed = scrub_action_surface(_leaky_surface())
    assert action_surface_id_leaks(scrubbed) == []


def test_parameters_keep_their_ids() -> None:
    """The asymmetry that makes this safe.

    ``add_internal_note`` finds the task by ``task_id``. Cleaning parameters
    would produce a beautiful card whose buttons do nothing. This is the one
    assertion that must never be "fixed" by making the scrub more thorough.
    """
    scrubbed = scrub_action_surface(_leaky_surface())
    assert scrubbed.actions[0].parameters == {"task_id": 39503, "property_id": 37975}
    # Same reasoning one level down: the click executes on the patch.
    followup = scrubbed.actions[0].first_followup
    assert followup is not None
    assert followup.options[0].parameter_patch == {"vendor_id": 7}


def test_prose_stays_readable() -> None:
    """Redaction keeps the noun, so sentences still parse.

    Dropping the whole reference would turn "task 39503 is still New" into
    " is still New". The reader loses the number, not the meaning.
    """
    scrubbed = scrub_action_surface(_leaky_surface())
    action = scrubbed.actions[0]
    assert "39503" not in (action.rationale or "")
    assert "task" in (action.rationale or "").lower()
    assert "still status New" in (action.rationale or "")

    draft = action.primary_action.draft if action.primary_action else ""
    assert draft and "39503" not in draft
    assert "Kitchen faucet dripping constantly" in draft, (
        "redaction ate the quoted title, which is the human-readable anchor that replaces the id"
    )


def test_orphaned_summary_highlights_are_dropped() -> None:
    """⚠ ``summary_highlights`` are EXACT substrings of ``summary``.

    The inbox wraps only these phrases in violet. Redacting the summary
    changes the text, so a highlight whose span contained the identifier no
    longer occurs — the frontend then highlights nothing and the emphasis
    silently disappears. A stale highlight is worse than none, because it
    looks intentional.
    """
    scrubbed = scrub_action_surface(_leaky_surface())
    assert scrubbed.summary_highlights is not None
    for highlight in scrubbed.summary_highlights:
        assert highlight in scrubbed.summary, (
            f"{highlight!r} no longer occurs in the redacted summary; the "
            "frontend would render no emphasis for it"
        )
    # The surviving one is the highlight that never contained an id.
    assert "New status for 29 days" in scrubbed.summary_highlights


def test_a_clean_surface_is_returned_unchanged() -> None:
    """The scrub must be a no-op on prose that was already fine.

    Most surfaces are. A scrubber that rewrites clean text is a scrubber that
    will eventually mangle a legitimate address or unit number.
    """
    clean = ActionSurface(
        summary=(
            "Robert Kim at 118 Crescent Avenue Unit 4B reported a kitchen leak; "
            "it is the third plumbing ticket at this property this quarter."
        ),
        summary_highlights=["Robert Kim", "Unit 4B"],
        freeform_placeholder="e.g. 'schedule a plumber for Tuesday'",
        signals=["3 similar tickets at this property this quarter"],
        actions=[
            ProposedAction(
                tool_id="add_internal_note",
                label="Flag as repeat plumbing",
                intent="Note this is the third plumbing ticket at 118 Crescent Avenue.",
                parameters={"task_id": 39504},
                rationale="The property at 118 Crescent Avenue has 3 open plumbing tickets.",
                triggers=["Unit 4B has a $1,950 balance"],
            )
        ],
    )
    assert scrub_action_surface(clean) == clean


@pytest.mark.parametrize(
    "prose",
    [
        "The property at 160 East End Avenue needs a roof inspection.",
        "The property at 1600 Pennsylvania Avenue has two open work orders.",
        "Unit 204 reported a loud crashing sound.",
        "unit 4B is $1,950 past due as of 2026-05-19.",
    ],
)
def test_addresses_and_unit_numbers_survive(prose: str) -> None:
    """The anchors that REPLACE identifiers must not themselves be scrubbed.

    Removing the number from "Unit 204" or "160 East End Avenue" would leave
    the card unable to say where anything is — strictly worse than the leak.
    """
    surface = ActionSurface(
        summary=prose,
        freeform_placeholder="e.g. 'schedule a visit'",
        actions=[
            ProposedAction(
                tool_id="add_internal_note",
                label="Add a note",
                intent=prose,
                parameters={},
                rationale=prose,
            )
        ],
    )
    scrubbed = scrub_action_surface(surface)
    assert scrubbed.summary == prose
    assert scrubbed.actions[0].rationale == prose


def test_a_field_that_is_only_an_identifier_keeps_its_original() -> None:
    """Redaction must never empty a field with a ``min_length``.

    ``freeform_placeholder`` requires 5 characters. A surface that fails
    re-validation costs the whole plan, which is the opposite of the point —
    a stray number beats a dropped card.
    """
    surface = ActionSurface(
        summary="A task update came through for this property this evening.",
        freeform_placeholder="#17435",
        actions=[],
    )
    scrubbed = scrub_action_surface(surface)
    assert scrubbed.freeform_placeholder.strip()
    ActionSurface.model_validate(scrubbed.model_dump())


def test_the_planner_actually_calls_the_scrub() -> None:
    """A scrubber nothing invokes cleans nothing.

    Every assertion above exercises ``scrub_action_surface`` directly, so all
    of them stay green if the call site is deleted. Gotcha 28's rule: read
    BOTH sides. This pins the wiring, and its position -- the scrub must run
    AFTER the mirror-fact merge, because mirror facts are generated from
    records and are perfectly capable of carrying "work order 9023"
    themselves.
    """
    import inspect

    from helixis.agent.planner import action_planner

    src = inspect.getsource(action_planner)
    assert "scrub_action_surface(parsed)" in src, (
        "the planner never scrubs its output; identifiers ship to the card"
    )
    assert src.index("_merge_mirror_signals(parsed, mirror_facts)") < src.index(
        "scrub_action_surface(parsed)"
    ), "the scrub runs before the mirror merge, so merged-in facts go unscrubbed"


# ── the write boundary ──────────────────────────────────────────────────────


def test_the_risk_floor_shape_is_cleaned_though_the_model_rejects_it() -> None:
    """Five producers, one boundary.

    ``scrub_action_surface`` covers the PLANNER. It cannot cover the risk
    bridge, email triage, comms triage or the routing surface builder, because
    those write shapes the model rejects outright: the risk floor carries
    ``kind`` and ``risk`` keys and ``ActionSurface`` is ``extra="forbid"``.

    Verbatim from a live card (event a5f35d83), whose signals came from the
    analyst detector's finding titles and were copied into the surface
    untouched.
    """
    from helixis.agent.planner.id_hygiene import scrub_surface_dict
    from helixis.agent.planner.models import ActionSurface

    floor = {
        "kind": "risk",
        "risk": {"count": 4, "rule_key": "analyst", "total_amount": 0},
        "actions": [],
        "signals": [
            "Three units at property 33377 expire within days amid open water heater leak",
            "Entire property 33375 shows unpaid charges plus two open repair tickets",
        ],
        "summary": "Hidden risks in the books — 4 findings",
        "schema_version": 1,
        "freeform_placeholder": "or tell me how you want this handled…",
    }
    with pytest.raises(Exception):
        ActionSurface.model_validate(floor)  # the reason the dict walker exists

    scrubbed = scrub_surface_dict(floor)
    assert "33377" not in str(scrubbed)
    assert "33375" not in str(scrubbed)
    # Structure and non-prose values survive untouched.
    assert scrubbed["kind"] == "risk"
    assert scrubbed["risk"] == {"count": 4, "rule_key": "analyst", "total_amount": 0}
    assert scrubbed["schema_version"] == 1
    # The sentence still reads.
    assert "expire within days" in scrubbed["signals"][0]


def test_parameter_prose_is_cleaned_but_parameter_ids_are_not() -> None:
    """``parameters`` is not a blanket skip, and that nuance is load-bearing.

    It holds BOTH the ids the handler executes on AND ``note_body`` /
    ``description``, which the frontend pre-fills the editor from and which
    get written into Buildium verbatim. Skipping the whole dict would leave
    "follow-up on task 39503" in a note a resident can read; cleaning the
    whole dict would break the handler.
    """
    from helixis.agent.planner.id_hygiene import scrub_surface_dict

    scrubbed = scrub_surface_dict(
        {
            "actions": [
                {
                    "tool_id": "add_internal_note",
                    "parameters": {
                        "task_id": 39503,
                        "property_id": 37975,
                        "note_body": "Follow-up on task 39503, never dispatched.",
                    },
                }
            ]
        }
    )
    params = scrubbed["actions"][0]["parameters"]
    assert params["task_id"] == 39503, "the handler can no longer find the task"
    assert params["property_id"] == 37975
    assert "39503" not in params["note_body"]
    assert "Follow-up on task" in params["note_body"]


def test_every_queue_writer_scrubs_before_persisting() -> None:
    """The boundary has THREE writers and a fix that reaches one is not a fix.

    ``store_enrichment``, ``store_routing_decision`` and
    ``coalesce_action_surface`` each serialize the surface with their own
    expression, so a single search-and-replace silently covers one of them.
    Gotcha 25's rule, applied to the write path.
    """
    import inspect

    from helixis.webhooks import queue

    src = inspect.getsource(queue)
    for var in ("action_surface", "deterministic_surface", "chosen"):
        assert f"json.dumps({var})" not in src, (
            f"{var} is persisted without scrub_surface_dict; identifiers reach the card"
        )
    assert src.count("scrub_surface_dict(") >= 3


@pytest.mark.parametrize(
    ("leaky", "must_not_contain"),
    [
        # Verbatim from the canary card fired AFTER the first fix deployed.
        ("cross-reference with open tap leak tickets 39503/39504.", "39504"),
        (
            "two unresolved kitchen faucet/tap leak tickets (39503, 39504) — "
            "worth checking if the unit above this bedroom is the same unit",
            "39503",
        ),
    ],
)
def test_the_leak_moved_to_a_synonym_and_a_bracket(leaky: str, must_not_contain: str) -> None:
    """The first fix covered task / work order / bill. The model reached for
    "ticket" and put the ids behind a bracket.

    Three separate blind spots in one string:

    * ``ticket`` was not a noun the rule knew;
    * ``[:\\s]*`` between noun and digits could not cross an opening paren, so
      a noun sitting right next to its ids read as clean;
    * only the FIRST number was matched, leaving "/39504" and ", 39504" in the
      sentence even when the leading one was caught.

    ⚠ The verification query I used to declare the first fix clean reused the
    same noun list, so it reported zero leaks on a card that had two. A checker
    that shares the fix's blind spot cannot find what the fix missed — the
    reason these strings are pinned here verbatim.
    """
    from helixis.agent.planner.id_hygiene import scrub_surface_dict

    scrubbed = scrub_surface_dict({"actions": [{"primary_action": {"draft": leaky}}]})
    draft = scrubbed["actions"][0]["primary_action"]["draft"]
    assert must_not_contain not in draft
    assert "39503" not in draft and "39504" not in draft
    assert "ticket" in draft.lower(), "the noun should survive; only the ids go"


@pytest.mark.parametrize(
    "counting",
    [
        "3 similar tickets at this property this quarter",
        "it is the fourth maintenance ticket at the property this quarter",
        "Property already has two unresolved kitchen faucet leak tickets",
    ],
)
def test_counts_of_tickets_are_not_identifiers(counting: str) -> None:
    """Adding ``ticket`` to the noun list must not eat the counts.

    "3 similar tickets" is the pattern signal the whole history fetch exists to
    produce. Scrubbing it would remove the most valuable sentence on the card
    to protect against an id that is not there.
    """
    from helixis.agent.planner.id_hygiene import scrub_surface_dict

    scrubbed = scrub_surface_dict({"summary": counting})
    assert scrubbed["summary"] == counting


def test_the_inbox_catches_the_shapes_the_chat_fix_added() -> None:
    """⚠ The sharing was HALF DONE, which is worse than not sharing at all.

    #897 found the chat reply leaking three shapes ``redact_id_references``
    cannot see — a plural ``IDs 11314, 11320`` heading a run, a bare
    parenthesised ``(11362, 11369)``, and a lone ``11314`` in prose. That fix
    landed in ``agents.reply_hygiene`` and never reached the action surface,
    so the inbox ran the older rule while chat ran the newer one and the same
    card could be clean in one surface and leaking in the other.

    Gotcha 25's rule applied to the rule itself: fix a field-shape bug at
    EVERY layer that reads it. Verified by reverting ``_clean`` to
    ``redact_id_references`` — every assertion below fails against it.
    """
    surface = scrub_surface_dict(
        {
            "summary": "Marcus closed them (IDs 11314, 11320, 11325).",
            "signals": ["the remaining 2 (11362, 11369) are still open"],
            "actions": [
                {
                    "rationale": "they all show 11314 as open",
                    "primary_action": {"label": "Follow up", "draft": "Re 11314 and 11320."},
                    # executed on — must survive untouched
                    "parameters": {"task_id": 39503, "note_body": "follow-up on 11314"},
                }
            ],
        }
    )

    action = surface["actions"][0]
    for leaked in ("11314", "11320", "11325", "11362", "11369"):
        assert leaked not in surface["summary"]
        assert leaked not in surface["signals"][0]
        assert leaked not in action["rationale"]
        assert leaked not in action["primary_action"]["draft"]
        assert leaked not in action["parameters"]["note_body"]

    # ⚠ The asymmetry that makes the card still WORK: the handler finds the
    # task by this id. Cleaning it yields a beautiful card whose buttons do
    # nothing.
    assert action["parameters"]["task_id"] == 39503


def test_an_address_in_a_card_keeps_its_postcode() -> None:
    """The inbox inherits the postal guard along with the stronger rule — a
    draft that loses a ZIP is a note someone sends to the wrong place."""
    surface = scrub_surface_dict(
        {"summary": "Unit at 415 S Beretania St, Honolulu, HI 96813 needs attention."}
    )
    assert "96813" in surface["summary"]
