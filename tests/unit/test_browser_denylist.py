"""Unit tests for the browser deny-list policy.

Pure-function module — no I/O, no mocks needed beyond URL inputs."""

from __future__ import annotations

import pytest

from helixis.agents.browser_denylist import (
    HARD_BLOCKED_PATTERNS,
    SOFT_BLOCKED_DOMAINS,
    SOFT_WARN_PATTERNS,
    check_domain,
    user_facing_message,
)
from helixis.pms import registry


@pytest.mark.parametrize(
    "url,expected_category,expected_reason",
    [
        # Hard blocks — all four categories represented
        ("https://www.pornhub.com/", "hard_blocked", "adult_content"),
        ("https://www.onlyfans.com/x", "hard_blocked", "adult_content"),
        ("https://twitter.com/something", "hard_blocked", "social_media"),
        ("https://x.com/foo", "hard_blocked", "social_media"),
        ("https://api.twitter.com/2/users", "hard_blocked", "social_media"),
        ("https://www.reddit.com/r/...", "hard_blocked", "social_media"),
        ("https://www.facebook.com/", "hard_blocked", "social_media"),
        ("https://www.linkedin.com/in/x", "hard_blocked", "social_media"),
        ("https://thepiratebay.org/", "hard_blocked", "file_sharing"),
        ("https://www.draftkings.com/", "hard_blocked", "gambling"),
        # Soft blocks — competitor PM + AI consumer
        ("https://appfolio.com/login", "soft_blocked", "competitor_pm_saas"),
        ("https://yardi.com/", "soft_blocked", "competitor_pm_saas"),
        ("https://buildium.com/", "soft_blocked", "competitor_pm_saas"),
        ("https://claude.ai/chat/abc", "soft_blocked", "ai_consumer_surface"),
        ("https://chatgpt.com/", "soft_blocked", "ai_consumer_surface"),
        ("https://perplexity.ai/search", "soft_blocked", "ai_consumer_surface"),
        # Soft warns — gov + edu
        ("https://www.fairfaxcounty.gov/permits", "soft_warn", None),
        ("https://stanford.edu/admissions", "soft_warn", None),
        ("https://mit.edu/", "soft_warn", None),
        # Allowed
        ("https://example.com/page", "allowed", None),
        ("https://news.ycombinator.com/", "allowed", None),
        # Malformed
        ("not-a-url", "hard_blocked", "malformed_url"),
        ("", "hard_blocked", "malformed_url"),
    ],
)
def test_check_domain_categorizes_correctly(
    url: str,
    expected_category: str,
    expected_reason: str | None,
) -> None:
    result = check_domain(url)
    assert result.category == expected_category
    if expected_reason is not None:
        assert result.reason == expected_reason


def test_soft_warn_attaches_warning_tag() -> None:
    """``.gov`` should carry the ``soft_warn:government_site`` tag so
    the tool layer can attach it to the result's warnings list — the
    agent prompt's rule about citing .gov / .edu sources depends on
    this tag being observable."""
    gov = check_domain("https://fairfaxcounty.gov/")
    assert gov.warning_tag == "soft_warn:government_site"
    edu = check_domain("https://stanford.edu/")
    assert edu.warning_tag == "soft_warn:educational_site"


def test_suffix_domain_match_does_not_overreach() -> None:
    """``twitter.com`` matches ``twitter.com`` and ``foo.twitter.com``
    but NOT ``faketwitter.com.evil.example`` — exact suffix only,
    not substring across dot boundaries."""
    assert check_domain("https://twitter.com").category == "hard_blocked"
    assert check_domain("https://api.twitter.com/").category == "hard_blocked"
    # The deny pattern is "twitter.com" (no wildcard), so suffix match
    # requires "twitter.com" as a full label. A bare host like
    # "faketwitter.com" should NOT match (no trailing ".twitter.com").
    assert check_domain("https://faketwitter.com/").category == "allowed"


def test_wildcard_pattern_substring_matches() -> None:
    """``*pornhub*`` matches any host containing the literal core."""
    assert check_domain("https://www.pornhub.com/").category == "hard_blocked"
    assert check_domain("https://pornhubpremium.example/").category == "hard_blocked"


def test_user_facing_message_for_hard_block() -> None:
    result = check_domain("https://twitter.com/foo")
    msg = user_facing_message(result, "twitter.com")
    assert "cannot navigate" in msg.lower()
    assert "social_media" in msg or "blocked" in msg.lower()


def test_user_facing_message_for_competitor_soft_block() -> None:
    result = check_domain("https://appfolio.com/")
    msg = user_facing_message(result, "appfolio.com")
    assert "competitor" in msg.lower()
    assert "appfolio.com" in msg


def test_user_facing_message_empty_for_allowed_and_warn() -> None:
    """Allowed and soft_warn produce no user-facing block message —
    the tool layer attaches the warning tag instead."""
    assert user_facing_message(check_domain("https://example.com"), "example.com") == ""
    assert (
        user_facing_message(
            check_domain("https://fairfaxcounty.gov/"),
            "fairfaxcounty.gov",
        )
        == ""
    )


def test_pattern_lists_are_lowercase_and_well_formed() -> None:
    """Module-level self-test re-runs the constant validation. If a
    contributor adds a pattern with disallowed chars (e.g. a regex
    metachar), the module import would have failed; this test
    documents the invariant."""
    for p in (*HARD_BLOCKED_PATTERNS, *SOFT_BLOCKED_DOMAINS, *SOFT_WARN_PATTERNS):
        assert p == p.lower(), f"pattern not lowercase: {p!r}"


# ── SSRF / infrastructure policy (LED-045) ───────────────────────


@pytest.mark.parametrize(
    "url,reason",
    [
        # Cloud metadata endpoint (the classic SSRF target).
        ("http://169.254.169.254/latest/meta-data/", "private_or_metadata_ip"),
        ("http://[fe80::1]/", "private_or_metadata_ip"),  # IPv6 link-local
        # Loopback.
        ("http://127.0.0.1:8000/admin", "private_or_metadata_ip"),
        ("http://[::1]/", "private_or_metadata_ip"),
        ("http://localhost/health", "internal_hostname"),
        # RFC1918 private ranges.
        ("http://10.0.0.5/", "private_or_metadata_ip"),
        ("http://192.168.1.1/", "private_or_metadata_ip"),
        ("http://172.16.5.4/", "private_or_metadata_ip"),
        ("http://0.0.0.0/", "private_or_metadata_ip"),
        # Internal / metadata hostnames.
        ("http://metadata.google.internal/", "internal_hostname"),
        ("http://db.internal/", "internal_hostname"),
        ("http://foo.localhost/", "internal_hostname"),
        # Non-HTTP schemes.
        ("file://localhost/etc/passwd", "non_http_scheme"),
        ("ftp://198.51.100.9/", "non_http_scheme"),
        ("gopher://evil/", "non_http_scheme"),
    ],
)
def test_ssrf_targets_hard_blocked(url: str, reason: str) -> None:
    result = check_domain(url)
    assert result.category == "hard_blocked"
    assert result.reason == reason


@pytest.mark.parametrize(
    "url",
    [
        "https://developer.buildium.example.com/docs",  # public docs, not the buildium.com apex
        "https://www.irs.gov/forms",  # soft_warn, but not infra-blocked
        "https://helixis-os.com/",
        "http://93.184.216.34/",  # a public literal IP (example.com) — allowed
    ],
)
def test_public_urls_not_infra_blocked(url: str) -> None:
    # These must NOT be hard-blocked for an infrastructure reason (they may be
    # soft_warn/allowed, but never private_or_metadata_ip / non_http_scheme).
    result = check_domain(url)
    assert result.reason not in ("private_or_metadata_ip", "internal_hostname", "non_http_scheme")


def test_ssrf_block_has_public_web_message() -> None:
    msg = user_facing_message(check_domain("http://169.254.169.254/"), "169.254.169.254")
    assert "public web" in msg.lower()


@pytest.mark.parametrize("system", sorted(registry.PMS))
def test_every_system_occupella_connects_to_is_blocked_to_the_browser(system: str) -> None:
    """Each connector reads its vendor through the API its terms allow, never
    by driving the vendor's site: Rent Manager, Rentvine and Buildium were
    blocked, and Propertyware, whose terms forbid scraping, was not. The
    registry's key is each vendor's domain name."""
    for url in (f"https://{system}.com/", f"https://app.{system}.com/pw/login"):
        result = check_domain(url)
        assert result.category == "soft_blocked", (url, result)
