"""A Rent Manager or Propertyware run is worded with Rentvine's measured text.

Rentvine's run wording (``run_pms.rentvine_wording`` and the two empty-mirror
messages in ``mirror_tools``) was found and fixed against the real agent
(2026-09-25: 7 of 10 questions on a Rentvine workspace ended in "connect
Buildium"). Rent Manager and Propertyware are read from the mirror the same
way, so they get the same text with their own name, through templates.

⚠ Rentvine's runs do NOT go through the templates: Brandon's plan leaves
Rentvine untouched. What proves the templates ARE Rentvine's text is that,
given "Rentvine", each one reproduces the Rentvine original byte for byte on
the live prompts. Change either side and this fails.
"""

from __future__ import annotations

import pytest

from helixis.agents import mirror_tools, run_pms
from helixis.agents.buildium_agent import create_buildium_agent
from helixis.agents.orchestrator import ORCHESTRATOR_INSTRUCTIONS
from helixis.settings import Settings


def _live_prompts() -> dict[str, str]:
    """The texts a run rewords: the PMS specialist and the orchestrator."""
    specialist = create_buildium_agent(Settings()).instructions
    assert isinstance(specialist, str)
    return {"specialist": specialist, "orchestrator": ORCHESTRATOR_INSTRUCTIONS}


@pytest.mark.parametrize("which", ["specialist", "orchestrator"])
def test_the_template_given_rentvine_is_rentvines_wording(which: str) -> None:
    text = _live_prompts()[which]
    assert run_pms.mirror_pms_wording(text, "Rentvine") == run_pms.rentvine_wording(text)


def test_the_section_templates_given_rentvine_are_rentvines_sections() -> None:
    assert run_pms._mirror_sections("Rentvine") == run_pms.RENTVINE_SECTIONS


def test_the_empty_mirror_templates_given_rentvine_are_rentvines_messages() -> None:
    assert mirror_tools._mirror_not_synced("Rentvine") == mirror_tools._RENTVINE_NOT_SYNCED
    assert (
        mirror_tools._mirror_empty_portfolio("Rentvine") == mirror_tools._RENTVINE_EMPTY_PORTFOLIO
    )


@pytest.mark.parametrize("name", ["Rent Manager", "Propertyware"])
def test_a_new_systems_specialist_never_hears_of_buildium(name: str) -> None:
    """The defect Rentvine hit: a prompt that names Buildium sends the model
    to Buildium's tools and the customer to Buildium's settings."""
    worded = run_pms.mirror_pms_wording(_live_prompts()["specialist"], name)
    assert "Buildium" not in worded
    assert f"{name} Agent" in worded or f"{name} specialist" in worded
    rentvine = run_pms.rentvine_wording(_live_prompts()["specialist"])
    # Everything but the name is Rentvine's text.
    assert worded.replace(name, "Rentvine") == rentvine


_NEW_SYSTEMS = [("rentmanager", "Rent Manager"), ("propertyware", "Propertyware")]


@pytest.mark.parametrize("key, name", _NEW_SYSTEMS)
def test_a_run_on_a_system_with_no_connector_yet_says_nothing_is_synced(
    key: str, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ Re-pointed 2026-10-05 (rule 19), not relaxed: each connector branch
    adds its own ``sync.RUN_NOTE``, so "no connector yet" is stood up the way a
    missing package reads, as a lookup that finds nothing."""
    from helixis.pms import registry

    monkeypatch.setattr(registry, "connector_attr", lambda *_args: None)
    note = run_pms.run_note_for(key)
    assert f"runs on {name}" in note
    assert "not available" in note
    assert "never say it is zero" in note
    assert "Buildium" not in note


def test_the_run_note_is_unchanged_for_buildium_rentvine_and_an_unresolved_run() -> None:
    assert run_pms.run_note_for("rentvine") == run_pms.RENTVINE_RUN_NOTE
    assert run_pms.run_note_for("buildium") == ""
    assert run_pms.run_note_for(None) == ""
    assert run_pms.run_note_for("appfolio") == ""


def test_a_connectors_own_run_note_replaces_the_not_built_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the connector exists, its ``sync.RUN_NOTE`` is what the run reads."""
    from helixis.pms import registry

    def _attr(key: str, module: str, attr: str) -> str | None:
        return "\n\nRENT MANAGER NOTE" if (module, attr) == ("sync", "RUN_NOTE") else None

    monkeypatch.setattr(registry, "connector_attr", _attr)
    assert run_pms.run_note_for("rentmanager") == "\n\nRENT MANAGER NOTE"
