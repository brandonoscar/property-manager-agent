"""Tests for ``scripts/composio_diagnose.py``.

The script's callables are imported via ``importlib.util`` (mirrors
``tests/unit/test_smoke_script.py``) so we don't have to spawn a
subprocess to exercise gather/pretty/JSON.

Goal: pin the data-dict shape AND prove that pretty mode is
byte-identical to a fixed reference string for a known data dict.
That's the constraint from the --json PR — the refactor must not
change anything users see in non-JSON mode.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest


def _load_module():
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_helixis_composio_diagnose",
        repo_root / "scripts" / "composio_diagnose.py",
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_helixis_composio_diagnose"] = mod
    spec.loader.exec_module(mod)
    return mod


diag = _load_module()


# ── Fixtures ────────────────────────────────────────────────────────


class _FakeToolkit:
    def __init__(self, slug: str) -> None:
        self.slug = slug


class _FakeConnection:
    """Minimal stand-in for a Composio v3 ConnectedAccount."""

    def __init__(
        self,
        toolkit: str,
        status: str,
        connection_id: str,
        created_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        self.toolkit = _FakeToolkit(toolkit)
        self.status = status
        self.id = connection_id
        self.created_at = created_at
        self.last_error = last_error


class _FakeResponse:
    def __init__(self, items: list[_FakeConnection]) -> None:
        self.items = items


def _stub_composio(monkeypatch: pytest.MonkeyPatch, items: list[_FakeConnection]) -> None:
    """Patch ``composio.Composio`` to return ``items`` from its v3 list call."""

    class _FakeConnectedAccounts:
        def list(self, user_ids: list[str]) -> _FakeResponse:  # noqa: ARG002
            return _FakeResponse(items)

    class _FakeComposio:
        def __init__(self, *, api_key: str) -> None:  # noqa: ARG002
            self.connected_accounts = _FakeConnectedAccounts()

    fake_module = type(sys)("composio")
    fake_module.Composio = _FakeComposio  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "composio", fake_module)
    monkeypatch.setenv("HELIXIS_COMPOSIO__API_KEY", "test-key")


# ── Precondition gating ─────────────────────────────────────────────


def test_gather_no_api_key_returns_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("HELIXIS_COMPOSIO__API_KEY", raising=False)
    exit_code, data = diag.gather_connections("user-x")
    assert exit_code == 2
    assert data is None
    assert "HELIXIS_COMPOSIO__API_KEY" in capsys.readouterr().err


def test_gather_api_error_returns_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _FakeConnectedAccounts:
        def list(self, user_ids: list[str]) -> Any:  # noqa: ARG002
            raise RuntimeError("upstream 503")

    class _FakeComposio:
        def __init__(self, *, api_key: str) -> None:  # noqa: ARG002
            self.connected_accounts = _FakeConnectedAccounts()

    fake_module = type(sys)("composio")
    fake_module.Composio = _FakeComposio  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "composio", fake_module)
    monkeypatch.setenv("HELIXIS_COMPOSIO__API_KEY", "test-key")

    exit_code, data = diag.gather_connections("user-x")
    assert exit_code == 1
    assert data is None
    assert "RuntimeError: upstream 503" in capsys.readouterr().err


# ── Gather shape ────────────────────────────────────────────────────


def test_gather_returns_shape_with_cross_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_composio(
        monkeypatch,
        [
            _FakeConnection("GMAIL", "ACTIVE", "c1", created_at="2026-05-25T10:00:00Z"),
            _FakeConnection("GMAIL", "EXPIRED", "c2", last_error="token revoked"),
            _FakeConnection("GOOGLECALENDAR", "ACTIVE", "c3"),
        ],
    )
    exit_code, data = diag.gather_connections("user-x")
    assert exit_code == 0
    assert data is not None

    assert data["user_id"] == "user-x"
    assert data["total"] == 3
    assert len(data["connections"]) == 3
    assert data["connections"][0]["toolkit"] == "GMAIL"
    assert data["connections"][0]["status"] == "ACTIVE"
    assert data["connections"][1]["error"] == "token revoked"

    cross = data["cross_tab"]
    assert cross["by_status"] == {"ACTIVE": 2, "EXPIRED": 1}
    assert cross["by_toolkit"] == {"GMAIL": 2, "GOOGLECALENDAR": 1}
    assert cross["by_toolkit_status"] == [
        {"toolkit": "GMAIL", "status": "ACTIVE", "count": 1},
        {"toolkit": "GMAIL", "status": "EXPIRED", "count": 1},
        {"toolkit": "GOOGLECALENDAR", "status": "ACTIVE", "count": 1},
    ]

    assert data["inactive_count"] == 1
    assert data["inactive_sample"] is not None
    # Captured at gather time as repr strings
    assert data["inactive_sample"]["status"] == "'EXPIRED'"
    assert data["inactive_sample"]["last_error"] == "'token revoked'"


def test_gather_empty_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_composio(monkeypatch, [])
    exit_code, data = diag.gather_connections("user-y")
    assert exit_code == 0
    assert data == {
        "user_id": "user-y",
        "total": 0,
        "connections": [],
        "cross_tab": {"by_status": {}, "by_toolkit": {}, "by_toolkit_status": []},
        "inactive_count": 0,
        "inactive_sample": None,
    }


# ── Pretty rendering (byte-identical contract) ──────────────────────


def test_render_pretty_empty_matches_expected() -> None:
    data = {
        "user_id": "user-y",
        "total": 0,
        "connections": [],
        "cross_tab": {"by_status": {}, "by_toolkit": {}, "by_toolkit_status": []},
        "inactive_count": 0,
        "inactive_sample": None,
    }
    expected = "# Composio connections for user_id=user-y\n# Total: 0\n\n"
    assert diag.render_pretty(data) == expected


def test_render_pretty_full_matches_expected_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin one realistic dict to its exact pretty string.

    Freeze ``datetime.now`` via ``_format_relative`` so the relative
    time column is deterministic across runs.
    """
    # Freeze the relative-time helper so the table cell is stable.
    monkeypatch.setattr(diag, "_format_relative", lambda when: "3h ago" if when else "?")

    data = {
        "user_id": "user-x",
        "total": 2,
        "connections": [
            {
                "toolkit": "GMAIL",
                "status": "ACTIVE",
                "created_at": "2026-05-25T07:00:00Z",
                "connection_id": "conn-abc",
                "error": "",
            },
            {
                "toolkit": "GMAIL",
                "status": "EXPIRED",
                "created_at": "2026-05-25T07:00:00Z",
                "connection_id": "conn-def",
                "error": "token revoked",
            },
        ],
        "cross_tab": {
            "by_status": {"ACTIVE": 1, "EXPIRED": 1},
            "by_toolkit": {"GMAIL": 2},
            "by_toolkit_status": [
                {"toolkit": "GMAIL", "status": "ACTIVE", "count": 1},
                {"toolkit": "GMAIL", "status": "EXPIRED", "count": 1},
            ],
        },
        "inactive_count": 1,
        "inactive_sample": {"status": "'EXPIRED'", "id": "'conn-def'"},
    }
    header = "TOOLKIT              STATUS         CREATED        CONNECTION_ID                            ERROR"  # noqa: E501 — fixture matches exact output width
    sep = "-" * len(header)
    expected = (
        "# Composio connections for user_id=user-x\n"
        "# Total: 2\n"
        "\n"
        f"{header}\n"
        f"{sep}\n"
        "GMAIL                ACTIVE         3h ago         conn-abc                                 \n"  # noqa: E501
        "GMAIL                EXPIRED        3h ago         conn-def                                 token revoked\n"  # noqa: E501
        "\n"
        "## Summary\n"
        "Status counts:  {'ACTIVE': 1, 'EXPIRED': 1}\n"
        "Toolkit counts: {'GMAIL': 2}\n"
        "\n"
        "## Status × Toolkit\n"
        "  GMAIL                ACTIVE         1\n"
        "  GMAIL                EXPIRED        1\n"
        "\n"
        "## Full attribute dump (first of 1 inactive connection)\n"
        "  status: 'EXPIRED'\n"
        "  id: 'conn-def'\n"
    )
    assert diag.render_pretty(data) == expected


# ── JSON path ───────────────────────────────────────────────────────


def test_diagnose_json_emits_only_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--json`` mode writes exactly one JSON payload to stdout, nothing else."""
    _stub_composio(
        monkeypatch,
        [_FakeConnection("GMAIL", "ACTIVE", "c1", created_at="2026-05-25T07:00:00Z")],
    )
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)

    exit_code = diag.diagnose("user-x", as_json=True)
    assert exit_code == 0

    payload = json.loads(buf.getvalue())
    assert payload["user_id"] == "user-x"
    assert payload["total"] == 1
    assert payload["connections"][0]["toolkit"] == "GMAIL"
    assert payload["cross_tab"]["by_status"] == {"ACTIVE": 1}


def test_diagnose_pretty_writes_pretty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``--json`` the output starts with the # header line."""
    _stub_composio(monkeypatch, [])
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)

    exit_code = diag.diagnose("user-y", as_json=False)
    assert exit_code == 0
    assert buf.getvalue().startswith("# Composio connections for user_id=user-y\n")
