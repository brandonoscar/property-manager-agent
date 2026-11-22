"""Unit tests for the shared tolerant-JSON helper (helixis.common.json_extract).

Used by both the document path (#161) and the planner. The document-tool tests
also exercise it via the dt._coerce_json_object alias; these cover the module
directly, including the planner-relevant shapes.
"""

from __future__ import annotations

import json

import pytest

from helixis.common.json_extract import (
    coerce_json,
    extract_first_json_object,
    strip_trailing_commas,
)


def test_clean_passthrough() -> None:
    data, coerced = coerce_json('{"a": 1}')
    assert data == {"a": 1} and coerced is False


def test_strips_code_fence() -> None:
    data, coerced = coerce_json('```json\n{"a": 1}\n```')
    assert data == {"a": 1} and coerced is True


def test_extracts_object_from_prose() -> None:
    data, coerced = coerce_json('Sure! Here is the surface:\n{"schema_version": 2}\nDone.')
    assert data == {"schema_version": 2} and coerced is True


def test_brace_inside_string_ignored() -> None:
    data, coerced = coerce_json('{"summary": "uses a } brace", "n": 1}')
    assert data == {"summary": "uses a } brace", "n": 1} and coerced is False


def test_nested_object_balanced() -> None:
    assert extract_first_json_object('x {"a": {"b": 2}} y') == '{"a": {"b": 2}}'


def test_genuinely_broken_raises() -> None:
    with pytest.raises(json.JSONDecodeError):
        coerce_json('{"a": {"b": 1}')  # unbalanced — must not be "recovered"


def test_no_object_raises() -> None:
    with pytest.raises(json.JSONDecodeError):
        coerce_json("not json at all")


# ── trailing-comma repair (#161: one fewer paid retry) ──────────


def test_trailing_comma_in_object_recovered() -> None:
    data, coerced = coerce_json('{"a": 1, "b": 2,}')
    assert data == {"a": 1, "b": 2} and coerced is True


def test_trailing_comma_in_array_recovered() -> None:
    data, coerced = coerce_json('{"items": [1, 2, 3,]}')
    assert data == {"items": [1, 2, 3]} and coerced is True


def test_trailing_comma_with_whitespace_recovered() -> None:
    data, _ = coerce_json('{"a": 1 ,\n}')
    assert data == {"a": 1}


def test_trailing_comma_inside_fence_recovered() -> None:
    data, coerced = coerce_json('```json\n{"a": 1,}\n```')
    assert data == {"a": 1} and coerced is True


def test_repair_never_touches_commas_inside_strings() -> None:
    # The literal contains ",]" — the trailing comma is only the one after 1.
    data, _ = coerce_json('{"s": "x,]", "n": 1,}')
    assert data == {"s": "x,]", "n": 1}


def test_strip_trailing_commas_is_string_aware() -> None:
    # A brace/bracket inside a string must not be seen as a container end.
    assert strip_trailing_commas('{"s": "a,}"}') == '{"s": "a,}"}'
    # A real trailing comma is dropped (whitespace before the brace is kept).
    assert json.loads(strip_trailing_commas("[1, 2, ]")) == [1, 2]


def test_repair_does_not_rescue_genuinely_broken() -> None:
    # Unbalanced even after comma repair — must still raise.
    with pytest.raises(json.JSONDecodeError):
        coerce_json('{"a": [1, 2,')
