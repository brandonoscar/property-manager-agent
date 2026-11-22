"""Pure core of the bias / disparate-impact spot-test (scripts/bias_disparate_impact_eval.py).

Covers the matched-pair corpus integrity, the advisory divergence heuristics,
and the renderer. The live model run is not exercised here (no key, no LLM).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MOD = Path(__file__).resolve().parents[2] / "scripts" / "bias_disparate_impact_eval.py"
_spec = importlib.util.spec_from_file_location("bias_disparate_impact_eval", _MOD)
assert _spec and _spec.loader
bde = importlib.util.module_from_spec(_spec)
sys.modules["bias_disparate_impact_eval"] = bde
_spec.loader.exec_module(bde)


# ── corpus integrity: the matched-pair invariant ─────────────────────────────


def test_corpus_nonempty_and_covers_multiple_axes() -> None:
    assert len(bde.CORPUS) >= 4
    axes = {s.axis for s in bde.CORPUS}
    assert len(axes) == len(bde.CORPUS)  # every scenario probes a distinct axis
    ids = [s.scenario_id for s in bde.CORPUS]
    assert len(ids) == len(set(ids))  # unique ids


def test_every_scenario_is_a_matched_pair() -> None:
    """The template has exactly one {v} slot and >= 2 variants, so only the
    protected signal differs between the prompts that get compared."""
    for s in bde.CORPUS:
        assert s.template.count("{v}") == 1, s.scenario_id
        assert len(s.variants) >= 2, s.scenario_id
        # Prompts differ ONLY by the substituted value — the scaffolding is
        # byte-identical once the slot is removed.
        scaffolds = {s.prompt_for(v).replace(v.value, "\x00") for v in s.variants}
        assert len(scaffolds) == 1, f"{s.scenario_id}: non-protected text differs"


# ── heuristics ───────────────────────────────────────────────────────────────


def test_valence_score_direction() -> None:
    assert bde.valence_score("A strong, reliable, low-risk applicant.") > 0
    assert bde.valence_score("Some concern here; I'd be careful and verify.") < 0


def test_protected_terms_introduced_detects_editorializing() -> None:
    assert bde.protected_terms_introduced("Likely a Hispanic applicant.") == ["hispanic"]
    assert bde.protected_terms_introduced("A well-qualified applicant.") == []


def test_name_axis_flags_protected_term_introduction() -> None:
    """Name axis (signal NOT in prompt): a reply naming an ethnicity it inferred
    from the name is flagged."""
    scenario = next(s for s in bde.CORPUS if not s.signal_in_prompt)
    labels = [v.label for v in scenario.variants]
    replies = {lbl: "A well-qualified applicant." for lbl in labels}
    replies[labels[1]] = "Likely an immigrant; I'd scrutinize this one more."
    row = bde.analyze_scenario(scenario, replies)
    assert row.flagged
    assert any("protected term" in r for r in row.reasons)


def test_valence_spread_flags_divergence() -> None:
    scenario = next(s for s in bde.CORPUS if s.signal_in_prompt)
    labels = [v.label for v in scenario.variants]
    replies = {lbl: "A neutral, professional note." for lbl in labels}
    replies[labels[0]] = "Excellent, strong, low-risk, reliable, ideal applicant."
    replies[labels[1]] = "Some concern; I'd be careful, verify, and scrutinize the risk."
    row = bde.analyze_scenario(scenario, replies)
    assert row.flagged
    assert any("valence spread" in r for r in row.reasons)


def test_identical_replies_are_clean() -> None:
    scenario = bde.CORPUS[0]
    replies = {
        v.label: "Qualified applicant; proceed with standard screening." for v in scenario.variants
    }
    row = bde.analyze_scenario(scenario, replies)
    assert not row.flagged
    assert row.reasons == []


def test_signal_in_prompt_operational_mention_not_flagged() -> None:
    """When the protected signal is IN the prompt (voucher/wheelchair), an
    operational mention in a matched, same-valence reply is NOT flagged."""
    scenario = next(s for s in bde.CORPUS if s.signal_in_prompt and "voucher" in s.axis)
    replies = {
        v.label: "Thanks for applying — here are the next steps to move forward."
        for v in scenario.variants
    }
    row = bde.analyze_scenario(scenario, replies)
    assert not row.flagged


# ── renderer ─────────────────────────────────────────────────────────────────


def _row(flagged: bool) -> bde.ScenarioRow:
    return bde.ScenarioRow(
        scenario_id="bias_x",
        axis="race",
        description="d",
        signal_in_prompt=False,
        variants=[bde.VariantRow(label="A", prompt="p", reply="ok", valence=0, words=1)],
        flagged=flagged,
        reasons=["valence spread 3 across matched variants (>= 2)"] if flagged else [],
    )


def test_render_clean_verdict() -> None:
    out = bde.render_bias_report([_row(False)], date_str="2026-08-07", sha="abc")
    assert "**CLEAN**" in out
    assert "`abc`" in out
    assert "flagged for review 0/1" in out


def test_render_review_verdict_lists_flagged() -> None:
    out = bde.render_bias_report([_row(True)], date_str="2026-08-07", sha="rc9")
    assert "**REVIEW**" in out
    assert "1 scenario(s) flagged for review" in out
    assert "valence spread 3" in out


def test_render_skipped_when_no_key() -> None:
    row = bde.ScenarioRow(
        scenario_id="bias_x", axis="race", description="d", signal_in_prompt=False, skipped=True
    )
    out = bde.render_bias_report([row], date_str="2026-08-07", sha="local")
    assert "SKIPPED (no model key)" in out
    assert "— skipped" in out
