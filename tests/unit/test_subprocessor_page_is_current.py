"""A subprocessor list is only worth reading if adding a vendor updates it.

`frontend/public/legal/subprocessors.html` is a legal disclosure: it tells a
customer which outside companies can see their residents' details. The failure
mode is silent and one-directional — somebody wires up a new service, the
credential goes into `settings.py`, data starts flowing, and the page still
lists yesterday's vendors. Nothing breaks, nobody is told, and the page quietly
becomes a false statement.

So this guard reads the CORPUS rather than a function (gotcha 45): every
credential-shaped field in `settings.py` is pinned to a decision — either it
belongs to a named vendor that MUST appear on the page, or it carries a written
reason for being exempt. A new credential the pin does not know about fails,
which is the whole point: the author has to say which kind it is.

⚠ WHAT THIS CANNOT SEE: a third party reached by a BROWSER SCRIPT rather than
by the backend. Such a vendor has no credential in `settings.py` — only a
public id in a `VITE_*` variable — so the pin below is blind to it by
construction. Apollo (website-visitor identification on the marketing site,
added 2026-09-15) is the first instance: it receives visitor IP addresses, it
is listed on the page, and nothing here would have noticed if it were not.
Every other vendor happens to hold a server credential too, which is why this
gap has never bitten.

Closing it is not possible from this repo — the marketing site is
`brandonoscar/helixis-onboarding`, a separate checkout — so this is a stated
boundary rather than a TODO. The rule for a human: a `<script>` from a domain
we do not own is a subprocessor, wherever the page lives.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS = _ROOT / "src" / "helixis" / "settings.py"
_PAGE = _ROOT / "frontend" / "public" / "legal" / "subprocessors.html"

#: `<SettingsClass>.<field>` → the vendor name the page must contain.
_DISCLOSED = {
    "LLMSettings.anthropic_api_key": "Anthropic",
    "LLMSettings.voyage_api_key": "Voyage AI",
    "BrowserSettings.browserbase_api_key": "Browserbase",
    "WebSettings.tavily_api_key": "Tavily",
    "ComposioSettings.api_key": "Composio",
    "TwilioSettings.auth_token": "Twilio",
    "BillingSettings.stripe_secret_key": "Stripe",
    "SentrySettings.dsn": "Sentry",
    "PostHogSettings.project_api_key": "PostHog",
    "LangfuseSettings.secret_key": "Langfuse",
    "SupabaseSettings.service_key": "Supabase",
}

#: `<SettingsClass>.<field>` → why this credential does NOT put a vendor on the
#: page. Every reason has to be a fact about data flow, never convenience.
_NOT_A_SUBPROCESSOR = {
    # Dead accounts. Gotcha 18: the Google billing cutoff killed both keys and
    # nothing reads them any more. Naming a vendor that receives nothing is as
    # misleading as omitting one that receives something.
    "LLMSettings.google_api_key": "dead account, no calls are made (gotcha 18)",
    "MapsSettings.api_key": "dead account; the geocoder defaults to US Census",
    # Configured but never called — the product runs Anthropic only, and
    # `_assert_anthropic_only` refuses a non-Anthropic model slot at boot.
    "LLMSettings.openai_api_key": "no call site; the router is Anthropic-only",
    # Ships switched OFF. It goes on the page in the same change that enables
    # it, not before — see the comment at the top of subprocessors.html.
    "CodeExecSettings.e2b_api_key": "code execution ships disabled; receives nothing",
    # Public data pulled IN. Nothing of the customer's is sent: the key
    # identifies us to a US government dataset, it does not carry their records.
    "IntelSettings.hud_api_key": "read-only public dataset; no customer data sent",
    # Ours, not a vendor's. These are secrets the product HOLDS; no third
    # party is on the other end of them.
    "AuthSettings.jwt_secret": "our own token signing key, not a vendor",
    "BillingSettings.stripe_webhook_secret": "Stripe is already disclosed above",
    # The CUSTOMER'S own Buildium credential, and Buildium is the customer's
    # system of record rather than our subprocessor. The page names it under
    # "Systems you connect yourself" for exactly this reason.
    "BuildiumSettings.client_secret": "the customer's own system, named separately",
    "BuildiumSettings.webhook_secret": "the customer's own system, named separately",
    # Rentvine — the second PMS (ADR 0010). TWO independent reasons, either
    # sufficient on its own, which is why this is not a close call:
    #   * it is the CUSTOMER'S system of record, exactly like Buildium above,
    #     so it belongs in the page's "Systems you connect yourself" section
    #     and never in the subprocessor list;
    #   * the connector ships DARK (`RentvineSettings.enabled` defaults False)
    #     and has never been run against a Rentvine server, so nothing reaches
    #     it today — the same trade the page's header records for E2B.
    # ⚠ It is deliberately NOT on the page yet. That section says these are
    # "your own accounts, connected by you", and today a customer cannot
    # connect one: there is no UI, the migrations are unapplied, and the flag
    # is off. Naming it now would be a false statement in the direction
    # `test_the_page_does_not_name_a_vendor_that_receives_nothing` exists to
    # stop. `_DARK_BY_DEFAULT` below fails the build when the flag flips, so
    # the page edit cannot be forgotten.
    "RentvineSettings.api_key": "the customer's own system; dark until enabled",
    "RentvineSettings.api_secret": "the customer's own system; dark until enabled",
}

#: Exemptions whose stated reason is "this ships switched off" → the flag that
#: reason rests on.
#:
#: ⚠ A REASON IS PROSE AND PROSE CANNOT FAIL (gotcha 48). "Ships disabled;
#: receives nothing" was true of E2B the day it was written and nothing
#: anywhere re-read it — flip the flag and the exemption keeps the vendor off
#: a legal disclosure while data flows to it, silently, in the one direction
#: this page exists to prevent. Naming the flag turns that sentence into a
#: measurement (gotcha 60's rule: a decision taken from a measurement needs a
#: test that re-measures).
_DARK_BY_DEFAULT = {
    "CodeExecSettings.e2b_api_key": "CodeExecSettings.enabled",
    "RentvineSettings.api_key": "RentvineSettings.enabled",
    "RentvineSettings.api_secret": "RentvineSettings.enabled",
}


def _credential_fields() -> set[str]:
    """`Class.field` for every credential-shaped setting.

    Read from the source rather than by importing: this container cannot import
    the app (broken crypto stack), and the question is which fields are
    DECLARED, which the text answers exactly.

    ⚠ The net is deliberately WIDE (`api_key` / `secret` / `token` / `dsn` /
    `service_key`) because a new vendor's credential could be called any of
    them, and a name the scan misses is a vendor nobody has to classify. It is
    narrowed on TYPE instead: only `str` fields count, which drops
    `daily_token_budget` — a spend cap that matched on the word "token" and is
    not a credential at all.
    """
    src = _SETTINGS.read_text(encoding="utf-8")
    found: set[str] = set()
    current = ""
    for line in src.splitlines():
        cls = re.match(r"class (\w+)\(", line)
        if cls:
            current = cls.group(1)
            continue
        field = re.match(
            r"    (\w*(?:api_key|secret|token|dsn|service_key)\w*)\s*:\s*str\b",
            line,
        )
        if field and current:
            found.add(f"{current}.{field.group(1)}")
    return found


def _bool_defaults() -> dict[str, bool]:
    """`Class.field` → the DECLARED default of every `bool` setting.

    ⚠ DECLARED, not live. Gotcha 29 is the standing record that a repo default
    is not what the deployed process runs — `HELIXIS_RENTVINE__ENABLED=true`
    set in Render would leave the test below green. What this catches is the
    flip a DIFF can see, which is the one a reviewer can act on. Stating that
    boundary beats implying coverage it cannot have.
    """
    src = _SETTINGS.read_text(encoding="utf-8")
    found: dict[str, bool] = {}
    current = ""
    for line in src.splitlines():
        cls = re.match(r"class (\w+)\(", line)
        if cls:
            current = cls.group(1)
            continue
        field = re.match(r"    (\w+)\s*:\s*bool\s*=\s*(True|False)\b", line)
        if field and current:
            found[f"{current}.{field.group(1)}"] = field.group(2) == "True"
    return found


#: Words by which an exemption CLAIMS its vendor is switched off. Narrow on
#: purpose: "dead account" reasons (gotcha 18's Google keys) name no flag and
#: have none to re-read, so a wider net would demand a pin that cannot exist
#: and the guard would be loosened to shut it up (gotchas 52/103/107).
_DARK_CLAIM_WORDS = ("dark", "disabled", "switched off")


def test_every_exemption_that_claims_to_be_dark_is_pinned_to_a_flag() -> None:
    """⚠ The load-bearing half — `_DARK_BY_DEFAULT` is hand-typed.

    The test below re-measures every entry IN that map, and is blind to an
    exemption left OUT of it: delete a line and the reason quietly reverts to
    prose nobody re-reads, which is the exact state this change fixes. That is
    the "silently permits more" direction, so the requirement is derived from
    the REASONS themselves rather than from a second hand-typed list
    (gotcha 45).
    """
    claiming = {
        credential: reason
        for credential, reason in _NOT_A_SUBPROCESSOR.items()
        if any(word in reason.lower() for word in _DARK_CLAIM_WORDS)
    }
    # Canary: a scan that finds nothing passes the loop below vacuously, and a
    # renamed reason is exactly how it would come to find nothing
    # (gotchas 49/100/101 — a scan returning zero is a claim to check).
    assert len(claiming) >= 3, (
        f"only {len(claiming)} exemption(s) read as dark-by-default, which is "
        f"fewer than the ones known to exist (E2B + the two Rentvine keys). "
        f"A reason was reworded out of {_DARK_CLAIM_WORDS} — re-point this."
    )
    unpinned = sorted(set(claiming) - set(_DARK_BY_DEFAULT))
    assert not unpinned, (
        f"these exemptions claim the vendor is switched off but name no flag: "
        f"{unpinned}. Add each to _DARK_BY_DEFAULT with the bool setting its "
        f"reason rests on, or the claim is prose that cannot fail."
    )
    # The other direction, so the vocabulary cannot quietly stop matching: a
    # reason with no dark claim must not be pinned as dark.
    for credential in _DARK_BY_DEFAULT:
        assert credential in claiming, (
            f"{credential} is pinned as dark-by-default but its reason "
            f"({_NOT_A_SUBPROCESSOR.get(credential)!r}) makes no such claim"
        )


def test_a_dead_account_reason_is_not_mistaken_for_a_dark_flag() -> None:
    """The false-positive direction, asserted rather than assumed.

    A dead vendor account (gotcha 18) is switched off in someone else's
    billing system, not by a flag in this repo — demanding a pin for it would
    make the guard unsatisfiable, and an unsatisfiable guard gets deleted.
    """
    for credential in ("LLMSettings.google_api_key", "MapsSettings.api_key"):
        reason = _NOT_A_SUBPROCESSOR[credential].lower()
        assert not any(word in reason for word in _DARK_CLAIM_WORDS), (
            f"{credential}'s reason now reads as a dark-flag claim, so the "
            f"test above will demand a flag it does not have"
        )


def test_a_dark_exemption_stops_being_true_the_moment_the_flag_flips() -> None:
    """The exemptions that rest on "it ships switched off" are re-measured.

    Both of this file's dark exemptions were written as sentences, and a
    sentence cannot notice that it has become false. Turning the flag on is a
    one-word diff; without this, that diff silently converts a correct
    exemption into a vendor receiving customer data with nothing on the legal
    page saying so.
    """
    defaults = _bool_defaults()
    assert len(defaults) > 5, f"the bool-default scan found only {len(defaults)} — it is broken"

    for credential, flag in sorted(_DARK_BY_DEFAULT.items()):
        assert credential in _NOT_A_SUBPROCESSOR, (
            f"{credential} is pinned as dark-by-default but is not exempt — "
            f"the two lists have drifted; re-point whichever one moved"
        )
        assert flag in defaults, (
            f"{credential}'s exemption rests on {flag}, which no longer exists "
            f"as a bool setting. Re-point this at the flag that replaced it — "
            f"an exemption whose reason names nothing is an exemption nobody "
            f"can check"
        )
        assert defaults[flag] is False, (
            f"{flag} now ships ENABLED, so {credential}'s exemption "
            f"({_NOT_A_SUBPROCESSOR[credential]!r}) is no longer true: that "
            f"vendor can now receive customer data. Name it in "
            f"frontend/public/legal/subprocessors.html IN THIS SAME CHANGE, "
            f"then move it out of _NOT_A_SUBPROCESSOR. ⚠ A customer's own PMS "
            f"belongs in the page's 'Systems you connect yourself' section, "
            f"NOT the subprocessor list — see the Buildium entry."
        )


def test_every_credential_in_settings_is_either_disclosed_or_excused() -> None:
    fields = _credential_fields()
    assert len(fields) > 8, f"the settings scan found only {len(fields)} — it is broken"

    known = set(_DISCLOSED) | set(_NOT_A_SUBPROCESSOR)
    unclassified = sorted(fields - known)
    assert not unclassified, (
        f"new credential(s) in settings.py that nobody has classified: {unclassified}. "
        "If the vendor can see customer data, add it to _DISCLOSED **and** to "
        "frontend/public/legal/subprocessors.html. If it cannot, add it to "
        "_NOT_A_SUBPROCESSOR with the data-flow reason."
    )

    # The other direction: a pin naming a setting that no longer exists is a
    # licence nobody needs, and makes the list read as broader than the code is
    # (gotcha 63's stale-exemption lesson).
    stale = sorted(known - fields)
    assert not stale, f"pinned settings that no longer exist: {stale}"


def test_the_page_names_every_disclosed_vendor() -> None:
    page = _PAGE.read_text(encoding="utf-8")
    missing = sorted({v for v in _DISCLOSED.values() if v not in page})
    assert not missing, (
        f"these vendors handle customer data and are not named on the subprocessors page: {missing}"
    )


def test_the_page_does_not_name_a_vendor_that_receives_nothing() -> None:
    """Over-listing is not the safe direction here. A page naming a vendor
    that holds nothing trains the reader that the list is approximate, which
    costs it the only thing it has."""
    page = _PAGE.read_text(encoding="utf-8")
    # mem0 is a library in our own process against our own pgvector store;
    # E2B is switched off. Both are argued in the page's own header comment,
    # inside an HTML comment a reader never sees — so assert on the BODY.
    body = page.split("</head>", 1)[-1]
    for absent in ("E2B", "Mem0", "OpenAI", "Gemini"):
        assert absent not in body, (
            f"{absent} appears in the visible page. It receives no customer "
            "data today — if that changed, this test changes with the code."
        )
