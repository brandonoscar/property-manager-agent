"""The smoke probe must exercise EXTRACTION, not only read-back.

``health_probe`` was built for gotcha 17 — an embedder 403 that ran ~19 days
undetected because ``search`` swallows every failure by design. It covered the
embedder (query-embed) and the store (vector search). Both READ-side.

On 2026-08-23 the third surface broke and nothing noticed: mem0's
fact-extraction LLM raised ``Messages.create() got an unexpected keyword
argument 'temperature'`` on every post-turn write. The assistant silently
stopped learning while recall kept working perfectly — so the probe, the smoke
check, and every dashboard stayed green. Sentry caught it; the health check
did not.

Same silent-degradation class the probe exists for, one path further along.
"""

from __future__ import annotations

import inspect

import pytest

from helixis.memory.manager import MemoryManager


def _probe_src() -> str:
    return inspect.getsource(MemoryManager.health_probe)


def test_the_probe_exercises_the_extraction_path() -> None:
    """``add`` is what runs the fact-extraction LLM. Read-only checks cannot
    see an extraction failure — that is precisely how this one survived."""
    src = _probe_src()
    assert "_mem0.add(" in src, (
        "health_probe does not exercise extraction; an LLM failure on the write "
        "path stays invisible while search keeps returning rows"
    )


def test_an_extraction_failure_is_surfaced_not_swallowed() -> None:
    """The everyday write path swallows by design (memory is an enhancement).
    The probe must do the opposite or it inherits the blindness."""
    src = _probe_src()
    assert "extraction failed" in src
    idx = src.index("_mem0.add(")
    after = src[idx:]
    assert "return (False," in after, "an add() failure must fail the probe"


def test_the_probe_writes_only_to_the_throwaway_partition() -> None:
    """No tenant data may be touched. The partition is the same one the
    read-side probe already uses."""
    src = _probe_src()
    idx = src.index("_mem0.add(")
    assert "__smoke_health__" in src[idx : idx + 400]


def test_a_failed_sweep_does_not_fail_the_probe() -> None:
    """The row is inert test text in a partition nothing reads, so a delete
    failure must degrade the detail line, not report memory as broken. A probe
    that cries wolf over its own cleanup gets muted, and then it protects
    nothing."""
    src = _probe_src()
    assert "probe row left behind" in src
    tail = src[src.index("delete_all") :]
    assert "return (False," not in tail, "a sweep failure must not fail the probe"


@pytest.mark.parametrize("surface", ["embedder", "vector search", "extraction"])
def test_the_success_line_names_all_three_surfaces(surface: str) -> None:
    """The detail string is what a human reads in the smoke output. If it
    still claims only two surfaces, the third is not actually covered — or
    someone removed it and the message lied."""
    assert surface in _probe_src()


def test_the_guard_can_fail() -> None:
    """Guard self-test (the convention). These assertions read source text, so
    prove they discriminate rather than matching anything."""
    assert "_mem0.add(" not in "async def health_probe(self): return (True, 'fine')"
    assert "extraction" not in "embedder + vector search reachable"
