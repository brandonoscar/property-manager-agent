"""Every JSON-object ``detail`` this API raises carries a sentence the client reads.

Six routes answer with an object rather than a string, because each carries a
machine-readable half beside the sentence — ``tool_id`` on the four inbox-execute
raises, the applications list on the CRM conflict, the problem list on A2P
submit. The web client's ``apiFetch`` kept ``detail`` only when it was a string,
so all six sentences were dropped and replaced by a generic status fallback. The
sharpest of them, the execute route's ``blocked_needs_review``, reached the
screen as "Somebody changed this while you were working on it. Reload and try
again." — a fabricated cause and an instruction that cannot clear that state.

``writtenDetail`` in ``frontend/src/services/api.ts`` now reads the object, and
this is the other side of that contract (gotcha 28): **the key list is parsed
out of the TypeScript**, so there is one source of truth and a seventh route
cannot invent a fourth spelling and go quiet.

⚠ THE FAILURE MODE IS "SILENTLY PERMITS MORE" (gotcha 45), which is why this
reads the CORPUS rather than feeding hand-written inputs to a function. A
mis-keyed detail does not raise, does not log, and does not fail any behavioural
test — it renders a true-but-useless status sentence, which is exactly what a
route with no detail at all renders.
"""

from __future__ import annotations

import ast
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "helixis"
API_TS = REPO / "frontend" / "src" / "services" / "api.ts"

# ⚠ Python reading a frontend file, and that is the established pattern here
# rather than a preference: `frontend/src/services/api.ts` is outside no root
# that matters, but the thing being checked lives in `src/helixis`, so the scan
# has to run under pytest. `test_favicon_contrast.py` and
# `test_frontend_api_paths_exist.py` are the precedents.
_KEYS_RE = re.compile(r"export const DETAIL_MESSAGE_KEYS\s*=\s*\[([^\]]*)\]")


def _client_message_keys() -> list[str]:
    """The keys `writtenDetail` looks under, read off the client."""
    match = _KEYS_RE.search(API_TS.read_text(encoding="utf-8"))
    assert match, (
        "DETAIL_MESSAGE_KEYS is gone from frontend/src/services/api.ts, or was "
        "renamed. It is the one source of truth for which keys a JSON-object "
        "`detail` may carry its sentence under, and this guard reads it so the "
        "two sides cannot drift. Re-point this regex at wherever it lives now — "
        "do not delete the check."
    )
    keys = re.findall(r"'([^']+)'", match.group(1))
    assert keys, (
        "DETAIL_MESSAGE_KEYS parsed as EMPTY. The declaration was found but no "
        "single-quoted entries came out of it — most likely the array was "
        "reformatted with double quotes. An empty key list makes the corpus "
        "check below vacuous, so it fails here rather than reporting green."
    )
    return keys


def _dict_details() -> list[tuple[str, int, ast.Dict]]:
    """Every ``HTTPException(detail={...})`` in the backend, with its position."""
    found: list[tuple[str, int, ast.Dict]] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - the repo parses
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "HTTPException":
                continue
            for keyword in node.keywords:
                if keyword.arg == "detail" and isinstance(keyword.value, ast.Dict):
                    found.append((str(path.relative_to(REPO)), node.lineno, keyword.value))
    return found


def _literal_keys(node: ast.Dict) -> list[str]:
    return [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]


def _value_for(node: ast.Dict, key: str) -> ast.expr | None:
    for k, v in zip(node.keys, node.values, strict=True):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    return None


def _carries_a_sentence(value: ast.expr) -> bool:
    """Would `writtenDetail` find something here?

    Only a LITERAL can be judged statically. ``str(exc)`` and
    ``current.problems`` are decided at runtime, so they are accepted — the
    checkable property is that the key is there at all.
    """
    if isinstance(value, ast.Constant):
        return isinstance(value.value, str) and bool(value.value.strip())
    if isinstance(value, ast.List):
        # A literal list must hold at least one non-empty string.
        return any(
            isinstance(e, ast.Constant) and isinstance(e.value, str) and e.value.strip()
            for e in value.elts
        ) or any(not isinstance(e, ast.Constant) for e in value.elts)
    return True


def test_every_object_detail_carries_a_key_the_client_reads() -> None:
    keys = _client_message_keys()
    offenders: list[str] = []
    for where, line, node in _dict_details():
        present = [k for k in _literal_keys(node) if k in keys]
        if not present:
            offenders.append(
                f"{where}:{line} carries {_literal_keys(node)} — none of {keys}. "
                "The client renders the generic status sentence instead of what "
                "this route wrote."
            )
            continue
        if not any(
            _carries_a_sentence(v) for k in present if (v := _value_for(node, k)) is not None
        ):
            offenders.append(
                f"{where}:{line} has {present} but every one is empty or not a "
                "string, so `writtenDetail` finds nothing and falls back."
            )
    assert not offenders, "\n".join(offenders)


def test_the_scan_finds_the_object_details_that_exist() -> None:
    """A scan that finds zero is a claim to check, not a result.

    ⚠ Gotchas 49/100/101/114/128 — six recorded instances of a probe whose
    empty result was read as an absence. Without this, a renamed import, a
    changed call spelling or a wrong root would report a clean corpus forever.
    """
    found = _dict_details()
    files = {where for where, _, _ in found}
    assert len(found) >= 5, f"expected the six known object details, found {len(found)}"
    for expected in (
        "src/helixis/api/routes/buildium.py",
        "src/helixis/api/routes/a2p.py",
        "src/helixis/api/routes/crm.py",
    ):
        assert expected in files, f"{expected} no longer contributes an object detail — re-check"


def test_the_client_keys_are_the_ones_the_backend_actually_uses() -> None:
    """The other direction: a key in the list that nothing produces is a licence
    nobody needs, and it makes the contract read as wider than it is.

    Same reasoning as the stale-allowlist test in `test_tenant_scoping_corpus`
    (gotcha 63) — an exemption that matches nothing should be noticed, not left
    to rot into a permission the next reader trusts.
    """
    keys = _client_message_keys()
    used: set[str] = set()
    for _, _, node in _dict_details():
        used.update(k for k in _literal_keys(node) if k in keys)
    unused = [k for k in keys if k not in used]
    assert not unused, (
        f"DETAIL_MESSAGE_KEYS lists {unused}, which no HTTPException in "
        "src/helixis produces. Either a route that used it was deleted — drop "
        "the key — or it was added speculatively, which is not what this list is."
    )


# ── the other end of the same pipe ───────────────────────────────────────────
#
# A `HandlerError` message becomes the object detail's `error` value verbatim
# (`detail={"error": str(exc), "tool_id": tool_id}`), so it now reaches a screen.
# That is the fix working, and it is also why a sentence written for a log is a
# defect on this path rather than a curiosity. Two of them existed and both said
# so themselves — "Internal: missing event_task_id in context" — safe only
# because the client discarded the whole object.
HANDLERS = REPO / "src" / "helixis" / "agent" / "handlers"
_LOG_VOICE = ("internal:", "traceback", "assertionerror", "none type", "nonetype")


def _handler_error_literals() -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for path in sorted(HANDLERS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "HandlerError" or not node.args:
                continue
            first = node.args[0]
            # Only a literal can be judged. An f-string's constant fragments are
            # judged too — "Buildium rejected the note: {exc}" is fine, the
            # interpolation is the provider's words and cannot be checked here.
            parts: list[str] = []
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                parts = [first.value]
            elif isinstance(first, ast.JoinedStr):
                parts = [
                    v.value
                    for v in first.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                ]
            for text in parts:
                out.append((str(path.relative_to(REPO)), node.lineno, text))
    return out


def test_no_handler_speaks_to_a_log_on_a_path_that_reaches_a_screen() -> None:
    offenders = [
        f"{where}:{line} — {text!r}"
        for where, line, text in _handler_error_literals()
        if any(marker in text.lower() for marker in _LOG_VOICE)
    ]
    assert not offenders, (
        "A HandlerError message is rendered to the person who clicked Execute. "
        "These are written for a log:\n"
        + "\n".join(offenders)
        + "\nSay what happened and whether retrying can help; the route already "
        "logs the tool id and event id beside it for diagnosis."
    )


def test_the_handler_scan_reaches_real_messages() -> None:
    """Same canary as above — a scan that finds nothing must say so."""
    found = _handler_error_literals()
    assert len(found) > 10, f"only {len(found)} HandlerError literals found — re-check the scan"
