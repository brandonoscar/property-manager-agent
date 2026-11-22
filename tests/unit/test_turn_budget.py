"""Unit tests for the opt-in eval max-turns override (evals/turn_budget.py)."""

from __future__ import annotations

import pytest

from helixis.evals.turn_budget import ENV_VAR, resolve_max_turns


def test_unset_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert resolve_max_turns(12) == 12
    assert resolve_max_turns(8) == 8


def test_positive_override_replaces_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "6")
    assert resolve_max_turns(12) == 6
    assert resolve_max_turns(8) == 6


def test_whitespace_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "  4  ")
    assert resolve_max_turns(12) == 4


def test_empty_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "   ")
    assert resolve_max_turns(12) == 12


def test_non_numeric_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "lots")
    assert resolve_max_turns(12) == 12


def test_non_positive_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "0")
    assert resolve_max_turns(12) == 12
    monkeypatch.setenv(ENV_VAR, "-3")
    assert resolve_max_turns(12) == 12
