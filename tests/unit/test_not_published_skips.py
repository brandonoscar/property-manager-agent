"""Only a file this repository deliberately leaves out turns a failure into a skip.

``tests/conftest.py`` reports a test as SKIPPED when it fails because a vendor
spec, a private note or a dropped workflow is absent. A missing file anywhere
else must still fail, or the hook would hide a real break.
"""

from __future__ import annotations

import os

from tests.conftest import _REPO_ROOT, _unpublished_path


def _missing(rel: str) -> FileNotFoundError:
    return FileNotFoundError(2, "No such file or directory", os.path.join(_REPO_ROOT, rel))


def test_a_vendor_spec_or_private_note_is_named() -> None:
    rel = "docs/rentvine/vendor/docs.rentvine.com/openapi.json"
    assert _unpublished_path(_missing(rel)) == rel
    assert _unpublished_path(_missing("design/rentmanager-connector.md"))
    assert _unpublished_path(_missing("src/helixis/rentvine/CLAUDE.md"))


def test_a_file_the_repository_should_have_still_fails() -> None:
    assert _unpublished_path(_missing("src/helixis/settings.py")) is None
    assert _unpublished_path(_missing("docs/deployment/auth-emails/invite.html")) is None
    assert _unpublished_path(_missing(".github/workflows/ci.yml")) is None
    assert _unpublished_path(_missing("scripts/apply_schema.py")) is None


def test_other_errors_are_never_turned_into_skips() -> None:
    assert _unpublished_path(None) is None
    assert _unpublished_path(AssertionError("docs/rentvine/x")) is None
    assert _unpublished_path(FileNotFoundError("no filename")) is None
