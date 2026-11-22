"""The feedback length bounds exist twice, and only one of them is enforced.

`api/routes/feedback.py` declares `message: str = Field(min_length=3,
max_length=5000)`. `frontend/src/services/feedback.ts` mirrors those numbers so
the dialog can stop somebody BEFORE they lose a report the server would refuse.
TypeScript cannot import Python, so nothing but this file keeps the two honest.

⚠ **Why the mirror is not belt-and-braces.** Validating twice would be
redundant; knowing the number on the client is not. Without it, a long
report — the most valuable kind, and the one somebody spent ten minutes
writing — is composed happily, rejected by the server, and lost. Drift makes
the client's guidance wrong in whichever direction it drifted: too high and
people still lose text, too low and the dialog refuses reports the server would
have taken.

Same shape as `billableSeats` in `InviteUsersDialog.tsx`, which carries the same
warning about a second copy of a rule and is pinned the same way.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_ROUTE = _ROOT / "src" / "helixis" / "api" / "routes" / "feedback.py"
_CLIENT = _ROOT / "frontend" / "src" / "services" / "feedback.ts"


def _server_bounds() -> tuple[int, int]:
    """`min_length` / `max_length` off the `message` field.

    Anchored on `message:` specifically — the same model declares four other
    capped fields (`view`, `session_id`, `app_version`, `user_agent`), and a
    scan that took the first `max_length` in the file would pin the wrong
    number while looking exactly right.
    """
    src = _ROUTE.read_text(encoding="utf-8")
    line = next(
        (ln for ln in src.splitlines() if re.match(r"\s*message\s*:\s*str\s*=\s*Field\(", ln)),
        None,
    )
    assert line, "no `message: str = Field(...)` in the feedback route — the scan is broken"
    lo = re.search(r"min_length\s*=\s*(\d+)", line)
    hi = re.search(r"max_length\s*=\s*(\d+)", line)
    assert lo and hi, f"message field declares no bounds: {line.strip()}"
    return int(lo.group(1)), int(hi.group(1))


def _client_bounds() -> tuple[int, int]:
    src = _CLIENT.read_text(encoding="utf-8")
    lo = re.search(r"MIN_MESSAGE_CHARS\s*=\s*(\d+)", src)
    hi = re.search(r"MAX_MESSAGE_CHARS\s*=\s*(\d+)", src)
    assert lo and hi, "the client no longer exports both bounds"
    return int(lo.group(1)), int(hi.group(1))


def test_the_client_mirrors_the_bounds_the_server_enforces() -> None:
    server, client = _server_bounds(), _client_bounds()
    assert client == server, (
        f"feedback length bounds have drifted — server {server}, client {client}. "
        "Both numbers describe the same rule; change them together."
    )


def test_the_route_still_checks_ok_on_the_client() -> None:
    """The bug the whole mirror was written alongside.

    `apiFetch` resolves with the Response whatever the status, so a
    `submitFeedback` that never reads `res.ok` reports a discarded report as
    sent. A behavioural test in the frontend covers what the user sees; this
    one covers the call site, because the failure is a MISSING line and a
    missing line is what a behavioural suite stops seeing the moment somebody
    deletes the test that noticed it.
    """
    src = _CLIENT.read_text(encoding="utf-8")
    assert "res.ok" in src, (
        "submitFeedback no longer checks the response status. Without it a 422 "
        "or a 500 resolves and the dialog tells the person their report was "
        "sent — in the only support channel inside the product."
    )
    assert "422" in src, (
        "the 422 branch is gone. A rejected message and a dropped connection "
        "need opposite advice: 'try again' is correct for one and wrong "
        "forever for the other."
    )
