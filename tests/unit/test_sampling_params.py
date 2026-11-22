"""No call site may send a temperature the production model rejects.

``anthropic/claude-sonnet-5`` raises ``UnsupportedParamsError`` for **any**
explicit temperature other than 1, including ``0`` — client-side, before the
request leaves the process. Every background LLM path did exactly that, and had
been failing since the 2026-07-21 Anthropic migration: 417 of 452 webhook events
with both ``planner_status`` and ``summary_status`` at ``'failed'``, the last
fully-successful event on **2026-05-22**. The Inbox, email triage, risk-flag
cards, document extraction and the CRM/renewal drafts were all dead for five
weeks, silently, because each failure was written to the row instead of raised.

Two layers, and the second is the one that matters:

* ``sampling_kwargs`` behaves correctly in isolation;
* **no call site anywhere sends a literal temperature**, checked by AST scan.
  The unit test alone would not have prevented this — the helper did not exist
  and each call site was individually "fine". Only a scan of every site catches
  the next one someone adds.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

import helixis
from helixis.llm.sampling import sampling_kwargs, supports_temperature

_SRC = Path(helixis.__file__).parent

#: The production model, and the tier the fast router / eval gate can use.
_PROD = "anthropic/claude-sonnet-5"
_HAIKU = "anthropic/claude-haiku-4-5"


# ── the helper ──────────────────────────────────────────────────────────────


def test_the_production_model_rejects_an_explicit_temperature() -> None:
    assert supports_temperature(_PROD) is False
    assert sampling_kwargs(_PROD, 0.3) == {}
    assert sampling_kwargs(_PROD, 0) == {}, "0 is rejected too — it is not a free pass"


def test_haiku_still_honours_one() -> None:
    """The value is preserved where it is honoured rather than deleted at every
    call site, so pointing a path at a cheaper tier keeps its tuning."""
    assert supports_temperature(_HAIKU) is True
    assert sampling_kwargs(_HAIKU, 0.3) == {"temperature": 0.3}


def test_none_sends_nothing() -> None:
    assert sampling_kwargs(_PROD, None) == {}
    assert sampling_kwargs(_HAIKU, None) == {}


def test_the_provider_prefix_does_not_hide_the_answer() -> None:
    """``_supports_sampling_params`` answers True for the PREFIXED name and
    False for the bare one — litellm strips the prefix before the real check.

    Asking it with the configured name therefore says the call will succeed
    when it will not, which is precisely how this shipped. The helper strips
    first; this pins that it does.
    """
    assert supports_temperature("claude-sonnet-5") is False
    assert supports_temperature(_PROD) is False


def test_an_unknown_model_fails_open() -> None:
    """A wrong True costs one clear 400 naming the model and the param. A wrong
    False silently drops a chosen temperature on every call, forever."""
    assert supports_temperature("some-provider/unknown-model-9") is True


# ── the real call path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_against_the_actual_litellm_guard() -> None:
    """Drive ``acompletion`` for real. Costs no network: ``UnsupportedParamsError``
    is raised client-side, so a fake key never gets used.

    This is the assertion that would have caught the bug. The capability table
    is litellm's, and a version bump can change it under us — only exercising
    the path that actually raised proves the helper still agrees with it.
    """
    import litellm

    async def outcome(**extra: object) -> str:
        try:
            await litellm.acompletion(
                model=_PROD,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=5,
                api_key="sk-ant-not-a-real-key",
                **extra,
            )
        except litellm.UnsupportedParamsError:
            return "rejected"
        except Exception:
            return "accepted"  # got past the param guard; auth is not our concern
        return "accepted"

    assert await outcome(temperature=0.3) == "rejected"
    assert await outcome(temperature=0) == "rejected"
    # What every patched call site now sends.
    assert await outcome(**sampling_kwargs(_PROD, 0.3)) == "accepted"
    assert await outcome(**sampling_kwargs(_PROD, 0)) == "accepted"


# ── the scan (the layer that actually prevents a recurrence) ────────────────


def _literal_temperature_sites() -> list[str]:
    """Every place in the package that hands a NON-1 literal ``temperature`` to
    a completion call — as a kwarg or as a dict key in a kwargs bag.

    ``temperature=1`` is exempt: every model accepts it, and two eval-harness
    sites pass it deliberately (the Gemini-3 floor, now the Claude-5 floor).
    Flagging those would train the reader to add exemptions, which is how a
    scan stops being read.
    """
    found: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.name == "sampling.py":
            continue  # the helper itself BUILDS the dict — that is its job
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        rel = path.relative_to(_SRC.parent)
        for node in ast.walk(tree):
            # temperature=<literal> passed to a call
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if (
                        kw.arg == "temperature"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value != 1
                    ):
                        found.append(f"{rel}:{node.lineno}")
            # {"temperature": <literal>} inside a kwargs dict
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "temperature"
                        and isinstance(value, ast.Constant)
                        and value.value != 1
                    ):
                        found.append(f"{rel}:{node.lineno}")
    return found


def test_no_call_site_sends_a_literal_temperature() -> None:
    offenders = _literal_temperature_sites()
    assert not offenders, (
        "these send a literal temperature, which the production model rejects "
        f"with a client-side 400: {offenders}. Use "
        "`**sampling_kwargs(model_name, <value>)` instead — it keeps the value "
        "for models that honour it and sends nothing to the ones that do not."
    )


def test_the_scan_catches_a_literal() -> None:
    """Self-test: a scan whose pattern stops matching passes forever.

    Both shapes must be caught — a bare kwarg and a dict entry — because the
    real bug appeared as a dict entry in a kwargs bag, which a kwarg-only scan
    would have missed entirely.
    """
    kwarg = ast.parse("acompletion(model=m, temperature=0.3)")
    dict_entry = ast.parse('kwargs = {"model": m, "temperature": 0.3}')

    def scan(tree: ast.AST) -> int:
        hits = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                hits += sum(
                    1
                    for kw in node.keywords
                    if kw.arg == "temperature"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value != 1
                )
            if isinstance(node, ast.Dict):
                hits += sum(
                    1
                    for key, value in zip(node.keys, node.values, strict=True)
                    if isinstance(key, ast.Constant)
                    and key.value == "temperature"
                    and isinstance(value, ast.Constant)
                    and value.value != 1
                )
        return hits

    assert scan(kwarg) == 1
    assert scan(dict_entry) == 1
    assert scan(ast.parse("acompletion(model=m, **sampling_kwargs(m, 0.3))")) == 0
    # temperature=1 is universally accepted and must NOT be flagged.
    assert scan(ast.parse("acompletion(model=m, temperature=1.0)")) == 0
    assert scan(ast.parse('kwargs = {"temperature": 1}')) == 0


def test_the_helper_never_raises() -> None:
    """It is a capability hint on the request path — it must degrade, not throw."""

    async def _run() -> None:
        for model in ("", "   ", "weird//name", _PROD):
            sampling_kwargs(model, 0.5)

    asyncio.run(_run())
