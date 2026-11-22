"""Unit tests for the conformance CLI's arg-default resolution (no network).

Regression coverage for #420: a GitHub Actions ``vars.X`` reference to an
*unset* repo variable resolves to ``""``, which lands as an empty-but-set
env var — ``os.environ.get(NAME, default)`` returns ``""`` in that case
instead of falling back, silently sending every probe to a URL-less client.
"""

from __future__ import annotations

from helixis.conformance.__main__ import _parse_args
from helixis.conformance.checks import DEFAULT_WIZARD_ORIGIN
from helixis.conformance.runner import DEFAULT_BASE_URL


def test_base_url_falls_back_when_env_var_is_empty_string(monkeypatch) -> None:
    monkeypatch.setenv("HELIXIS_CONFORMANCE_BASE_URL", "")
    args = _parse_args([])
    assert args.base_url == DEFAULT_BASE_URL


def test_base_url_uses_nonempty_env_var(monkeypatch) -> None:
    monkeypatch.setenv("HELIXIS_CONFORMANCE_BASE_URL", "https://example.test")
    args = _parse_args([])
    assert args.base_url == "https://example.test"


def test_base_url_uses_default_when_env_var_unset(monkeypatch) -> None:
    monkeypatch.delenv("HELIXIS_CONFORMANCE_BASE_URL", raising=False)
    args = _parse_args([])
    assert args.base_url == DEFAULT_BASE_URL


def test_origin_falls_back_when_env_var_is_empty_string(monkeypatch) -> None:
    monkeypatch.setenv("HELIXIS_CONFORMANCE_ORIGIN", "")
    args = _parse_args([])
    assert args.origin == DEFAULT_WIZARD_ORIGIN


def test_cli_flag_overrides_empty_env_var(monkeypatch) -> None:
    monkeypatch.setenv("HELIXIS_CONFORMANCE_BASE_URL", "")
    args = _parse_args(["--base-url", "https://explicit.test"])
    assert args.base_url == "https://explicit.test"
