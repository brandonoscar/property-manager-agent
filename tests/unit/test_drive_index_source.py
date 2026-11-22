"""Unit tests for the Google Drive static-index knowledge source (#67 PR-2).

Covers the parts that carry the logic: the defensive ``parse_drive_listing``
normalizer (the live-seam parser), the facts rendering / signal detection, and
the ``refresh_drive_index`` orchestration (connected vs not, enumeration failure
must NOT prune). The repo's raw SQL (``replace_for_user`` / ``list_for_company``)
is thin and DB-bound — exercised on the first dogfood, not here.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from helixis.company_knowledge import drive as drive_mod
from helixis.company_knowledge.drive import (
    enumerate_drive_files,
    parse_drive_listing,
    refresh_drive_index,
    refresh_drive_index_for_user,
)
from helixis.company_knowledge.sources import CompanyFacts, facts_to_prompt_block
from helixis.db.repositories.drive_index import DriveFile

# ── parse_drive_listing (the live-seam parser) ───────────────────────


def test_parse_unwraps_data_files_envelope() -> None:
    raw = {
        "successful": True,
        "data": {
            "files": [
                {
                    "id": "f1",
                    "name": "Pet Policy.pdf",
                    "mimeType": "application/pdf",
                    "modifiedTime": "2026-07-01T12:00:00Z",
                },
                {"id": "f2", "name": "Lease Template.docx"},
            ]
        },
    }
    files = parse_drive_listing(raw)
    assert [f.file_id for f in files] == ["f1", "f2"]
    assert files[0].title == "Pet Policy.pdf"
    assert files[0].mime_type == "application/pdf"
    assert files[0].modified_at == datetime.datetime(2026, 7, 1, 12, 0, tzinfo=datetime.UTC)
    # No modified time / mime on the second — optional, still indexed.
    assert files[1].mime_type is None
    assert files[1].modified_at is None


def test_parse_handles_top_level_items_and_field_aliases() -> None:
    # No envelope; ``items`` key; snake_case + Google-raw field aliases.
    raw = {
        "items": [
            {"fileId": "x", "title": "SOP", "mime_type": "text/plain", "parent": "Ops"},
        ]
    }
    files = parse_drive_listing(raw)
    assert len(files) == 1
    assert files[0].file_id == "x"
    assert files[0].title == "SOP"
    assert files[0].mime_type == "text/plain"
    assert files[0].path == "Ops"


def test_parse_skips_entries_without_id_or_title() -> None:
    raw = {"files": [{"name": "no id"}, {"id": "y"}, {"id": "z", "name": "ok"}]}
    files = parse_drive_listing(raw)
    assert [f.file_id for f in files] == ["z"]


def test_parse_respects_limit() -> None:
    raw = {"files": [{"id": str(i), "name": f"doc{i}"} for i in range(10)]}
    assert len(parse_drive_listing(raw, limit=3)) == 3


def test_parse_bad_modified_time_is_none_not_dropped() -> None:
    raw = {"files": [{"id": "a", "name": "A", "modifiedTime": "not-a-date"}]}
    files = parse_drive_listing(raw)
    assert len(files) == 1
    assert files[0].modified_at is None


@pytest.mark.parametrize("raw", [None, {}, {"data": {}}, {"files": "nope"}, "string"])
def test_parse_empty_or_malformed_returns_empty(raw: object) -> None:
    assert parse_drive_listing(raw) == []  # type: ignore[arg-type]


# ── facts rendering / signal detection ───────────────────────────────


def test_drive_files_alone_count_as_signal() -> None:
    facts = CompanyFacts(drive_files=[("SOP.pdf", "Ops")])
    assert facts.has_any_signal() is True


def test_facts_block_renders_drive_section_with_folder() -> None:
    facts = CompanyFacts(drive_files=[("Pet Policy.pdf", "Policies"), ("Lease.docx", None)])
    block = facts_to_prompt_block(facts)
    assert "Google Drive documents" in block
    assert "• Pet Policy.pdf [Policies]" in block
    assert "• Lease.docx" in block
    # No folder → no bracket noise.
    assert "Lease.docx []" not in block


def test_facts_block_omits_drive_section_when_empty() -> None:
    facts = CompanyFacts(lease_count=3)
    assert "Google Drive documents" not in facts_to_prompt_block(facts)


# ── enumerate_drive_files ────────────────────────────────────────────


class _FakeComposio:
    available = True

    def __init__(self, *, connected: bool = True, listing: object = None, raise_exec: bool = False):
        self._connected = connected
        self._listing = listing
        self._raise = raise_exec
        self.execute_calls: list[dict] = []

    def is_connected(self, user_id: str, toolkit: str) -> bool:
        return self._connected

    def execute_tool(self, *, user_id: str, slug: str, arguments: dict) -> object:
        self.execute_calls.append({"user_id": user_id, "slug": slug, "arguments": arguments})
        if self._raise:
            raise RuntimeError("composio boom")
        return self._listing


@pytest.mark.asyncio
async def test_enumerate_returns_none_when_unavailable() -> None:
    unavailable = SimpleNamespace(available=False)
    assert await enumerate_drive_files(unavailable, composio_user_id="u1") is None


@pytest.mark.asyncio
async def test_enumerate_returns_none_on_execute_failure() -> None:
    composio = _FakeComposio(raise_exec=True)
    assert await enumerate_drive_files(composio, composio_user_id="u1") is None


@pytest.mark.asyncio
async def test_enumerate_returns_none_when_result_none() -> None:
    composio = _FakeComposio(listing=None)
    # None result (Composio couldn't run) → None so the caller does NOT prune.
    assert await enumerate_drive_files(composio, composio_user_id="u1") is None


@pytest.mark.asyncio
async def test_enumerate_parses_listing() -> None:
    composio = _FakeComposio(listing={"files": [{"id": "a", "name": "A"}]})
    files = await enumerate_drive_files(composio, composio_user_id="u1")
    assert files is not None
    assert [f.title for f in files] == ["A"]


# ── refresh_drive_index orchestration ────────────────────────────────


class _FakePool:
    def __init__(self, user_ids: list[str]):
        self._user_ids = user_ids

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        return [{"user_id": uid} for uid in self._user_ids]


def _settings(drive_source: bool = True, max_files: int = 200) -> object:
    return SimpleNamespace(
        company_knowledge=SimpleNamespace(drive_source=drive_source, drive_max_files=max_files)
    )


@pytest.mark.asyncio
async def test_refresh_disabled_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    composio = _FakeComposio(listing={"files": [{"id": "a", "name": "A"}]})
    calls: list = []

    async def _fake_replace(*args: object, **kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(drive_mod.repo, "replace_for_user", _fake_replace)
    total = await refresh_drive_index(
        _FakePool(["u1"]), _settings(drive_source=False), company_id="c1", composio=composio
    )
    assert total == 0
    assert composio.execute_calls == []  # composio never touched
    assert calls == []


@pytest.mark.asyncio
async def test_refresh_indexes_connected_user(monkeypatch: pytest.MonkeyPatch) -> None:
    composio = _FakeComposio(
        connected=True, listing={"files": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]}
    )
    captured: dict = {}

    async def _fake_replace(
        pool: object, *, company_id: str, composio_user_id: str, files: list[DriveFile]
    ) -> int:
        captured["company_id"] = company_id
        captured["user"] = composio_user_id
        captured["files"] = files
        return len(files)

    monkeypatch.setattr(drive_mod.repo, "replace_for_user", _fake_replace)
    total = await refresh_drive_index(
        _FakePool(["u1"]), _settings(), company_id="c1", composio=composio
    )
    assert total == 2
    assert captured["company_id"] == "c1"
    assert captured["user"] == "u1"
    assert [f.title for f in captured["files"]] == ["A", "B"]


@pytest.mark.asyncio
async def test_refresh_skips_unconnected_user(monkeypatch: pytest.MonkeyPatch) -> None:
    composio = _FakeComposio(connected=False, listing={"files": [{"id": "a", "name": "A"}]})
    calls: list = []

    async def _fake_replace(*args: object, **kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(drive_mod.repo, "replace_for_user", _fake_replace)
    total = await refresh_drive_index(
        _FakePool(["u1"]), _settings(), company_id="c1", composio=composio
    )
    assert total == 0
    assert calls == []  # unconnected → never indexed


@pytest.mark.asyncio
async def test_refresh_enumeration_failure_does_not_prune(monkeypatch: pytest.MonkeyPatch) -> None:
    # Connected, but the list call fails → enumerate returns None → replace (the
    # prune) must NOT run, so a good prior index survives.
    composio = _FakeComposio(connected=True, raise_exec=True)
    calls: list = []

    async def _fake_replace(*args: object, **kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(drive_mod.repo, "replace_for_user", _fake_replace)
    total = await refresh_drive_index(
        _FakePool(["u1"]), _settings(), company_id="c1", composio=composio
    )
    assert total == 0
    assert calls == []  # no prune on a failed enumeration


# ── refresh_drive_index_for_user (the per-user, instant-on-connect path) ──


@pytest.mark.asyncio
async def test_refresh_for_user_disabled_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    composio = _FakeComposio(connected=True, listing={"files": [{"id": "a", "name": "A"}]})

    async def _fake_replace(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr(drive_mod.repo, "replace_for_user", _fake_replace)
    total = await refresh_drive_index_for_user(
        _FakePool([]),
        _settings(drive_source=False),
        company_id="c1",
        composio_user_id="u1",
        composio=composio,
    )
    assert total == 0
    assert composio.execute_calls == []  # disabled → composio untouched


@pytest.mark.asyncio
async def test_refresh_for_user_indexes_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    composio = _FakeComposio(connected=True, listing={"files": [{"id": "a", "name": "A"}]})
    captured: dict = {}

    async def _fake_replace(
        pool: object, *, company_id: str, composio_user_id: str, files: list[DriveFile]
    ) -> int:
        captured["user"] = composio_user_id
        return len(files)

    monkeypatch.setattr(drive_mod.repo, "replace_for_user", _fake_replace)
    total = await refresh_drive_index_for_user(
        _FakePool([]), _settings(), company_id="c1", composio_user_id="u1", composio=composio
    )
    assert total == 1
    assert captured["user"] == "u1"


@pytest.mark.asyncio
async def test_refresh_for_user_not_connected_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    composio = _FakeComposio(connected=False, listing={"files": [{"id": "a", "name": "A"}]})
    calls: list = []

    async def _fake_replace(*args: object, **kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(drive_mod.repo, "replace_for_user", _fake_replace)
    total = await refresh_drive_index_for_user(
        _FakePool([]), _settings(), company_id="c1", composio_user_id="u1", composio=composio
    )
    assert total == 0
    assert calls == []  # unconnected → never indexed


# ── on-connect kick (list_connectors → background build) ─────────────


@pytest.mark.asyncio
async def test_on_connect_kick_skips_when_already_indexed(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.api.routes.connectors import _kick_drive_index_on_connect

    monkeypatch.setattr("helixis.db.engine.get_pool", lambda: object())

    async def _count(pool: object, *, company_id: str, composio_user_id: str) -> int:
        return 5  # already indexed

    monkeypatch.setattr("helixis.db.repositories.drive_index.count_for_user", _count)
    built: list = []

    async def _refresh(*args: object, **kwargs: object) -> int:
        built.append(kwargs)
        return 0

    monkeypatch.setattr(drive_mod, "refresh_drive_index_for_user", _refresh)
    await _kick_drive_index_on_connect(
        settings=_settings(), composio=_FakeComposio(), company_id="c1", composio_user_id="u1"
    )
    assert built == []  # index present → no rebuild on subsequent page loads


@pytest.mark.asyncio
async def test_on_connect_kick_builds_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.api.routes.connectors import _kick_drive_index_on_connect

    monkeypatch.setattr("helixis.db.engine.get_pool", lambda: object())

    async def _count(pool: object, *, company_id: str, composio_user_id: str) -> int:
        return 0  # first connect

    monkeypatch.setattr("helixis.db.repositories.drive_index.count_for_user", _count)
    built: list = []

    async def _refresh(pool: object, settings: object, **kwargs: object) -> int:
        built.append(kwargs)
        return 3

    monkeypatch.setattr(drive_mod, "refresh_drive_index_for_user", _refresh)
    await _kick_drive_index_on_connect(
        settings=_settings(), composio=_FakeComposio(), company_id="c1", composio_user_id="u1"
    )
    assert len(built) == 1
    assert built[0]["composio_user_id"] == "u1"
    assert built[0]["company_id"] == "c1"


@pytest.mark.asyncio
async def test_on_connect_kick_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.api.routes.connectors import _kick_drive_index_on_connect

    def _boom() -> object:
        raise RuntimeError("pool exploded")

    monkeypatch.setattr("helixis.db.engine.get_pool", _boom)
    # Must swallow — a background task that raises is an unretrieved-exception bug.
    await _kick_drive_index_on_connect(
        settings=_settings(), composio=_FakeComposio(), company_id="c1", composio_user_id="u1"
    )
