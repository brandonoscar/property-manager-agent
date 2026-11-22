"""A chat thread belongs to a person; the Inbox belongs to the company.

⚠ WHAT SHIPPED — gotcha 85, reported by the founder on 2026-09-04 with a
screenshot: *"both accounts still have matching threads"*. The first invited
teammate opened Occupella and their sidebar was the admin's — *"How many
properties do I manage"*, *"can you draft an email to…"*, twenty threads they
had never written. All three reads scoped on ``company_id`` alone.

``user_id`` was on the row the whole time. ``save_message`` has always written
it, and it is populated on **3,189 of 3,235** live rows; every read threw it
away. That is gotchas 41 / 50 / 51 / 76 — *the field was already fetched and
thrown away* — arriving as a missing WHERE clause instead of a dropped column.

MEASURED BEFORE CHANGING ANYTHING, because "scope it tighter" can silently
delete history (gotcha 55's shape):

* **0 of 580** sessions are shared between two users, so no existing thread
  loses a reader.
* **12** sessions carry a NULL ``user_id`` (2026-07-21..08-19), and **0** are
  partly attributed — a session is either wholly owned or wholly anonymous.
  Those 12 become invisible rather than mis-filed: showing them to everyone is
  the leak being closed, and inventing an owner is a guess.

⚠ THE COMPANY PREDICATE STAYS. Gotcha 47 requires it, and it is load-bearing
rather than ceremonial: a user id alone would return a person's threads from a
company they have since left.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents.chat_history import (
    list_sessions_for_company,
    load_session_history,
    load_session_messages_for_company,
)

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src/helixis"
_ROUTES = _SRC / "api/routes/chat.py"
_ORCHESTRATOR = _SRC / "agents/orchestrator.py"

COMPANY = "5ca1ab1e-0000-4000-8000-000000000004"
CALLER = "11111111-2222-3333-4444-555555555555"


def _capturing_pool() -> tuple[Any, dict[str, Any]]:
    seen: dict[str, Any] = {}

    async def fetch(sql: str, *args: Any) -> list[Any]:
        seen["sql"] = " ".join(sql.split()).lower()
        seen["args"] = args
        return []

    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=fetch)
    return pool, seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "call"),
    [
        ("the sidebar list", lambda: list_sessions_for_company(COMPANY, user_id=CALLER)),
        (
            "replaying a thread",
            lambda: load_session_messages_for_company("s1", COMPANY, user_id=CALLER),
        ),
        (
            "the history the MODEL reads",
            lambda: load_session_history("s1", COMPANY, user_id=CALLER),
        ),
    ],
)
async def test_every_read_filters_on_both_the_company_and_the_person(label, call) -> None:
    """⚠ ALL THREE, not just the list.

    Hiding a teammate's thread from the sidebar while still serving its
    messages to anyone who names the id is a guard that only works against
    people who do not look. And the third one is the one that matters most:
    it reaches the MODEL, so a company-only scope replayed somebody else's
    conversation into this turn as context.
    """
    pool, seen = _capturing_pool()
    with patch("helixis.agents.chat_history.get_pool", return_value=pool):
        await call()

    assert "company_id = $" in seen["sql"], f"{label} dropped the company fence (gotcha 47)"
    assert "user_id = $" in seen["sql"], (
        f"{label} is company-scoped only — every member of a company sees every "
        "other member's threads, which is exactly what was reported"
    )
    assert CALLER in seen["args"], f"{label} builds the predicate but never binds the caller"


@pytest.mark.asyncio
async def test_a_thread_the_caller_does_not_own_comes_back_empty() -> None:
    """The behavioural statement, from the caller's side rather than the SQL's."""
    pool, _ = _capturing_pool()  # fetch returns [] — the DB found no matching rows
    with patch("helixis.agents.chat_history.get_pool", return_value=pool):
        assert (
            await load_session_messages_for_company("someone-elses", COMPANY, user_id=CALLER) == []
        )
        assert await load_session_history("someone-elses", COMPANY, user_id=CALLER) == []


def _keywords_at(path: pathlib.Path, func_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == func_name:
                found |= {kw.arg for kw in node.keywords if kw.arg}
    return found


@pytest.mark.parametrize(
    ("path", "func"),
    [
        (_ROUTES, "list_sessions_for_company"),
        (_ROUTES, "load_session_messages_for_company"),
        (_ORCHESTRATOR, "load_session_history"),
    ],
)
def test_the_call_sites_actually_pass_a_user(path: pathlib.Path, func: str) -> None:
    """⚠ The wiring half — no behavioural test above can see it (gotcha 62).

    ``user_id`` is keyword-ONLY and has no default precisely so a caller that
    forgets it fails loudly rather than falling back to the old behaviour. This
    guard is what notices somebody "fixing" that failure by giving it a default
    of ``""`` — which would match no row and silently empty everyone's sidebar,
    or worse, by widening the predicate back to the company.
    """
    assert "user_id" in _keywords_at(path, func), (
        f"{path.name} calls {func} without user_id — either the scope was reverted "
        "or a default was added to silence the signature"
    )


def test_the_helpers_keep_user_id_required_and_keyword_only() -> None:
    """A default is how this regresses without any test going red."""
    import inspect

    for fn in (list_sessions_for_company, load_session_messages_for_company, load_session_history):
        param = inspect.signature(fn).parameters["user_id"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{fn.__name__}: user_id went positional"
        )
        assert param.default is inspect.Parameter.empty, (
            f"{fn.__name__}: user_id gained a default. A caller that forgets it must FAIL, "
            "not quietly fall back — an empty string matches no row and empties the sidebar, "
            "which reads as data loss rather than as a wiring bug."
        )
