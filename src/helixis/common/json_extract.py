"""Tolerant JSON extraction for LLM responses.

Even with ``response_format=json_object``, Gemini (multimodal + the planner's
text path) wraps the JSON in a ```json fence or prepends a line of prose a large
fraction of the time. A bare ``json.loads`` rejects that, which fires an
expensive correction-nudge retry for output that is actually parseable
(issue #161 on the document path; the planner shares the same shape).

``coerce_json`` recovers those shapes deterministically — a code-fence strip,
a string-aware balanced-object extraction, then a trailing-comma repair (the
one other malformation Gemini emits often — ``{"a": 1,}`` / ``[1, 2,]`` — which
``json.loads`` rejects but has an unambiguous fix) — before the caller declares
failure. Each recovered class is one fewer paid correction-nudge retry (#161).
Genuinely malformed JSON (unbalanced braces, truncation) still raises so the
caller's retry path still fires.
"""

from __future__ import annotations

import json
import re
from typing import Any

_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL | re.IGNORECASE)


def strip_code_fence(text: str) -> str:
    """Return ``text`` without a surrounding ```` ```json … ``` ```` fence."""
    match = _JSON_FENCE_RE.match(text)
    return match.group(1) if match else text


def extract_first_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` span, ignoring braces inside strings.

    Recovers a JSON object the model wrapped in surrounding prose. Returns None
    when there is no balanced object (e.g. truncated output).
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def strip_trailing_commas(text: str) -> str:
    """Remove commas that immediately precede a ``}`` or ``]`` (ignoring any
    whitespace between), skipping commas inside string literals.

    ``{"a": 1,}`` → ``{"a": 1}``; ``[1, 2, ]`` → ``[1, 2]``. String-aware so a
    literal like ``"a,}"`` is never touched. Purely syntactic — the downstream
    schema/Pydantic validation still runs, so this can't mask a wrong value.
    """
    out: list[str] = []
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            continue
        if ch == ",":
            j = i + 1
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
            if j < len(text) and text[j] in "}]":
                continue  # drop this trailing comma
        out.append(ch)
    return "".join(out)


def coerce_json(raw: str) -> tuple[Any, bool]:
    """Parse JSON from ``raw``, tolerating a code fence, surrounding prose, or a
    trailing comma.

    Returns ``(parsed, coerced)`` where ``coerced`` is True when recovery beyond
    a clean ``json.loads`` was needed. Raises ``json.JSONDecodeError`` when the
    content is genuinely unparseable (so the caller's retry path still fires).
    """
    try:
        return json.loads(raw), False
    except json.JSONDecodeError:
        pass
    stripped = strip_code_fence(raw)
    # Try each recovered candidate in turn; for each, attempt a plain parse and
    # then a trailing-comma repair before moving on.
    candidates = [stripped, extract_first_json_object(stripped)]
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            return json.loads(candidate), True
        except json.JSONDecodeError:
            pass
        repaired = strip_trailing_commas(candidate)
        if repaired != candidate:
            try:
                return json.loads(repaired), True
            except json.JSONDecodeError:
                pass
    raise json.JSONDecodeError("no parseable JSON object found", raw, 0)
