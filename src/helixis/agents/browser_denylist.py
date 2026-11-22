"""Deny-list policy for browser_agent navigation.

The routing-layer rule "Browser Agent fires only when no in-domain
specialist can answer" is the primary gate. This module is the
safety floor — hand-maintained list of patterns the agent must
never navigate to (or must navigate to with extra disclosure).

Three categories:

* ``HARD_BLOCKED_PATTERNS`` — adult content, social media surfaces
  rich in prompt-injection content, file-sharing, gambling. Every
  navigation attempt to a hard-blocked target ends the task with
  ``disallowed_domain`` and a "Helixis cannot navigate to that
  type of site" user-facing message.
* ``SOFT_BLOCKED_DOMAINS`` — competitor PM SaaS and AI consumer
  surfaces. Same enforcement as hard-blocked but a different
  user-facing message ("Helixis does not navigate to competitor
  products") and INFO-level log severity instead of WARNING. The
  hard/soft split is purely for observability + UX; both deny.
* ``SOFT_WARN_PATTERNS`` — government and educational sites. Allowed,
  but ``check_domain`` returns a warning tag the tool layer attaches
  to the result so the agent's prompt rule about explicitly citing
  ``.gov`` / ``.edu`` sources actually fires.

Matching is intentionally simple — substring match for entries
with ``*`` wildcards, exact suffix-domain match otherwise. We
prefer false positives (extra block) over false negatives (missed
block) because the failure mode of blocking too aggressively is
"agent escalates to user" and the failure mode of missing a block
is "agent visits a site we said it wouldn't."

Per the PR #153 out-of-scope clause: deny-list is Helixis-wide
for v1. Per-customer customization is a follow-up.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

DenyCategory = Literal["allowed", "soft_warn", "soft_blocked", "hard_blocked"]

# ── SSRF / infrastructure policy (LED-045) ───────────────────────────────
# A browser tool is an SSRF primitive if its URL policy only filters by domain
# reputation. Block anything that isn't public-web HTTP(S): non-HTTP schemes
# (file://, ftp://, gopher://, …) and hosts that resolve to a private, loopback,
# link-local, or cloud-metadata address. (Browserbase runs the browser on its
# OWN remote network, so the blast radius of a miss is Browserbase's sandbox,
# not Helixis infra — but a metadata/loopback fetch is never a legitimate PM
# task, so we refuse it regardless.)
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
_BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        # Cloud metadata service hostnames (GCP / AWS / Azure).
        "metadata.google.internal",
        "metadata",
        "instance-data",
        "instance-data.ec2.internal",
    }
)


def _infrastructure_reason(scheme: str, host: str) -> str | None:
    """Return a reason code if the URL targets non-web infrastructure — a
    non-HTTP scheme, or a private/loopback/link-local/metadata address — else
    None. Pure + deterministic; does NOT resolve DNS (keeps ``check_domain``
    I/O-free), so it blocks literal IPs + internal hostnames. DNS-rebinding to a
    private IP is a residual, and lands on Browserbase's network, not ours.
    """
    if scheme and scheme not in _ALLOWED_SCHEMES:
        return "non_http_scheme"
    # Literal IP target: allow only globally-routable addresses.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        blocked = (
            ip.is_private  # RFC1918 (10/8, 172.16/12, 192.168/16) + CGNAT
            or ip.is_loopback  # 127/8, ::1
            or ip.is_link_local  # 169.254/16 (incl. 169.254.169.254 metadata), fe80::/10
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified  # 0.0.0.0, ::
        )
        return "private_or_metadata_ip" if blocked else None
    # Hostname target: block loopback / internal / metadata names.
    if host in _BLOCKED_HOSTNAMES or host.endswith((".localhost", ".internal")):
        return "internal_hostname"
    return None


HARD_BLOCKED_PATTERNS: tuple[str, ...] = (
    # Adult content
    "*pornhub*",
    "*xvideos*",
    "*xhamster*",
    "*onlyfans.com",
    # Social media surfaces — high prompt-injection risk from user content
    "twitter.com",
    "x.com",
    "reddit.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "linkedin.com",
    # File-sharing / piracy
    "*thepiratebay*",
    "*1337x*",
    "*rarbg*",
    # Gambling
    "*draftkings*",
    "*fanduel*",
    "*bet365*",
)

SOFT_BLOCKED_DOMAINS: tuple[str, ...] = (
    # Competitor PM SaaS — no Helixis use case for navigating to these
    "appfolio.com",
    "yardi.com",
    "rentmanager.com",
    "tenantcloud.com",
    "rentvine.com",
    "hemlane.com",
    "doorloop.com",
    "buildium.com",
    # ⚠ Its terms forbid scraping (design/propertyware-terms.md); every system
    # Occupella connects to is blocked here, which a test holds to the registry.
    "propertyware.com",
    # AI consumer surfaces — no Helixis use case
    "chatgpt.com",
    "claude.ai",
    "gemini.google.com",
    "perplexity.ai",
)

SOFT_WARN_PATTERNS: tuple[str, ...] = (
    "*.gov",
    "*.edu",
)


@dataclass(frozen=True)
class DenyCheckResult:
    """Result of running a URL through the deny-list.

    ``category`` drives the tool-layer response:
      * ``allowed`` — proceed normally.
      * ``soft_warn`` — proceed AND attach a warning tag (e.g.
        ``soft_warn:government_site``) to the tool result.
      * ``soft_blocked`` / ``hard_blocked`` — refuse the navigation,
        return ``disallowed_domain`` to the agent.

    ``reason`` is a short machine-readable code for the soft_blocked
    / hard_blocked categories (e.g. ``competitor_pm_saas``,
    ``social_media``). ``warning_tag`` is set only on ``soft_warn``.
    """

    category: DenyCategory
    reason: str | None = None
    warning_tag: str | None = None


def _extract_host(url: str) -> str:
    """Pull the lowercased hostname from a URL. Returns empty string
    when the input isn't parseable — the caller treats that as
    hard_blocked (we'd rather refuse on malformed input than guess)."""
    try:
        parsed = urlparse(url.strip())
    except (ValueError, AttributeError):
        return ""
    host = (parsed.hostname or "").lower()
    return host


def _matches_pattern(host: str, pattern: str) -> bool:
    """Pattern matching with two rules:

    * Pattern contains ``*`` → substring match (e.g. ``*pornhub*``
      matches ``www.pornhub.com`` AND ``pornhubpremium.example``).
    * Pattern has no wildcard → exact match OR suffix-domain match
      (``twitter.com`` matches both ``twitter.com`` and
      ``api.twitter.com`` but NOT ``faketwitter.com``).

    Both inputs are case-normalized (host is already lowercased by
    ``_extract_host``; pattern is lowercased here)."""
    pattern_lc = pattern.lower()
    if "*" in pattern_lc:
        # Strip wildcards and substring-match the literal core.
        literal = pattern_lc.replace("*", "")
        return literal in host
    # No wildcard — exact or suffix-domain match.
    if host == pattern_lc:
        return True
    return host.endswith("." + pattern_lc)


# Reason codes for the hard-blocked patterns. Indexed parallel to
# ``HARD_BLOCKED_PATTERNS`` ranges; kept inline so adding a new
# hard block in the constant tuple plus this dict is one edit each.
_HARD_REASON_BY_INDEX_RANGE: tuple[tuple[int, int, str], ...] = (
    (0, 4, "adult_content"),
    (4, 11, "social_media"),
    (11, 14, "file_sharing"),
    (14, 17, "gambling"),
)


def _hard_reason_for_index(idx: int) -> str:
    for start, end, code in _HARD_REASON_BY_INDEX_RANGE:
        if start <= idx < end:
            return code
    return "hard_blocked"


_SOFT_WARN_TAG_BY_PATTERN: dict[str, str] = {
    "*.gov": "soft_warn:government_site",
    "*.edu": "soft_warn:educational_site",
}


def check_domain(url: str) -> DenyCheckResult:
    """Run a URL through the deny-list. Pure function, no I/O.

    Precedence (first match wins):
      1. Malformed URL → hard_blocked with ``malformed_url`` reason.
      2. SSRF / infrastructure (non-HTTP scheme, private / loopback /
         link-local / metadata address) → hard_blocked.
      3. ``HARD_BLOCKED_PATTERNS`` → hard_blocked with category code.
      4. ``SOFT_BLOCKED_DOMAINS`` → soft_blocked with
         ``competitor_pm_saas`` or ``ai_consumer_surface`` reason.
      5. ``SOFT_WARN_PATTERNS`` → allowed + warning_tag.
      6. Otherwise → allowed.
    """
    host = _extract_host(url)
    if not host:
        return DenyCheckResult(category="hard_blocked", reason="malformed_url")

    # SSRF / infrastructure guard — before reputation checks (LED-045).
    scheme = ""
    try:
        scheme = (urlparse(url.strip()).scheme or "").lower()
    except (ValueError, AttributeError):
        pass
    infra = _infrastructure_reason(scheme, host)
    if infra is not None:
        return DenyCheckResult(category="hard_blocked", reason=infra)

    # Hard blocks first.
    for idx, pattern in enumerate(HARD_BLOCKED_PATTERNS):
        if _matches_pattern(host, pattern):
            return DenyCheckResult(
                category="hard_blocked",
                reason=_hard_reason_for_index(idx),
            )

    # Soft blocks.
    for pattern in SOFT_BLOCKED_DOMAINS:
        if _matches_pattern(host, pattern):
            # Cheap categorization for observability — competitor PM
            # SaaS lists end at index 7, AI consumer surfaces start
            # at index 8 in the constant. Hard-coded boundary keeps
            # the dependency local; updates require touching both.
            idx = SOFT_BLOCKED_DOMAINS.index(pattern)
            reason = "competitor_pm_saas" if idx < 8 else "ai_consumer_surface"
            return DenyCheckResult(category="soft_blocked", reason=reason)

    # Soft warns.
    for pattern in SOFT_WARN_PATTERNS:
        if _matches_pattern(host, pattern):
            tag = _SOFT_WARN_TAG_BY_PATTERN.get(pattern, "soft_warn:unknown")
            return DenyCheckResult(category="soft_warn", warning_tag=tag)

    return DenyCheckResult(category="allowed")


def user_facing_message(result: DenyCheckResult, host: str) -> str:
    """Build the user-facing message the browser_agent surfaces when
    a navigation is blocked. The agent's system prompt teaches it to
    relay this text verbatim — the deny-list policy stays in code,
    not in the LLM's interpretation."""
    if result.category == "hard_blocked":
        if result.reason == "malformed_url":
            return "Occupella could not parse that URL."
        if result.reason in ("private_or_metadata_ip", "internal_hostname", "non_http_scheme"):
            return (
                "Occupella only browses the public web — it can't open internal, "
                "local, or non-web addresses. Give me a public https:// URL."
            )
        return (
            f"Occupella cannot navigate to that type of site "
            f"({result.reason or 'blocked'}). Try a different source or "
            "ask me to look this up another way."
        )
    if result.category == "soft_blocked":
        if result.reason == "competitor_pm_saas":
            return (
                "Occupella does not navigate to competitor property-management "
                f"products ({host}). If you need data from there, export it "
                "and share the file instead."
            )
        return f"Occupella does not navigate to {host}. Ask me to look this up another way."
    return ""


__all__ = [
    "DenyCheckResult",
    "HARD_BLOCKED_PATTERNS",
    "SOFT_BLOCKED_DOMAINS",
    "SOFT_WARN_PATTERNS",
    "check_domain",
    "user_facing_message",
]


# ── small self-test on import — surfaces typos in the constants ──
# Runs once at module load; ~50 µs. If a pattern in the constants is
# malformed (e.g. accidentally regex-shaped), the import fails
# loudly rather than silently producing wrong matches at runtime.
_VALID_PATTERN_RE = re.compile(r"^[a-z0-9.\-*]+$")
for _p in (*HARD_BLOCKED_PATTERNS, *SOFT_BLOCKED_DOMAINS, *SOFT_WARN_PATTERNS):
    if not _VALID_PATTERN_RE.fullmatch(_p.lower()):
        raise ValueError(
            f"browser_denylist pattern {_p!r} contains characters outside "
            "[a-z0-9.-*]; matching expects plain substrings / suffix domains."
        )
