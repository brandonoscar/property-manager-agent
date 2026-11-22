"""What the user reads while the agent works.

A live turn showed the user this, five times in a row, for three minutes:

    Called buildium agent
    Called buildium agent
    Called buildium agent — no result

That is a machine name split on underscores. It cannot tell you whether the
agent is making progress or spinning, so a slow turn and a broken turn look
identical — and the honest reading, from the outside, is "this is broken".

This module turns a tool name into a sentence a property manager would say.

⚠ **DETERMINISTIC. No model call, no round trip.** The phrasing is a pure
function of the tool name, computed at the emit site and carried on the event
that already exists (``tool_start_event``'s ``description``, which the frontend
was throwing away in favour of re-humanising the raw name). Narration that
depended on the model would go quiet exactly when the model is busy, which is
the only time anybody is reading it.

⚠ **There is an OLDER map, ``agent/narration.py::TOOL_NARRATION``, and it
covers NOTHING on this path.** Its 17 keys are dotted legacy ids
(``buildium.tasks.get``) from the pre-Agents-SDK planner; the tools that
actually reach ``tool_start`` are ``buildium_get_property`` and 58 siblings.
Every live chat tool fell through its ``_GENERIC_FALLBACK``. That is gotcha
45's shape — a control keyed to a vocabulary that no longer exists, silently
doing nothing, with nothing failing to announce it. The old map is still
correct for its own caller (``api/routes/buildium.py``) and is left alone;
this one is keyed on the LIVE registry and guarded against it.

⚠ **Derivation first, overrides second.** 59 Buildium tools plus specialists,
mirror rollups and card renderers is too many to hand-write, and a
hand-written list rots the moment somebody registers a tool. So the rule is
mechanical — verb + subject — and the explicit table only holds the ones where
the mechanical answer reads badly. A new tool therefore gets a decent sentence
for free rather than falling back to a machine name.
"""

from __future__ import annotations

import re

#: Leading verb -> how a person says it. Present continuous throughout: the
#: line is on screen WHILE the work happens, and a past tense there reads as a
#: claim that it finished.
_VERBS = {
    "get": "Looking up",
    "list": "Listing",
    "count": "Counting",
    "search": "Searching",
    "find": "Finding",
    "create": "Creating",
    "update": "Updating",
    "add": "Adding",
    "close": "Closing",
    "record": "Recording",
    "post": "Posting",
    "undo": "Undoing",
    "upload": "Uploading",
    "share": "Sharing",
    "show": "Putting together",
    "suggest": "Working out",
    "run": "Running",
    # ⚠ "open" and "maintenance" are NOT verbs here, and treating them as one
    # produced "Checking open the maintenance by unit". A name that does not
    # start with a verb falls through to the subject branch below, which reads
    # correctly — a wrong verb is worse than no verb.
}

#: Words that read badly split apart, and how to say them instead.
_SUBJECT_FIXES = {
    "gl accounts": "the chart of accounts",
    "gl account balances": "account balances",
    "gl transactions": "the general ledger",
    "to do task": "a to-do",
    "work orders for unit": "work orders at the unit",
    "maintenance history by unit": "maintenance history for the unit",
    "open maintenance by unit": "open maintenance by unit",
    "upcoming lease expirations": "leases coming up for renewal",
    "recent lease transactions": "the lease ledger",
    "overdue balances": "who is behind on rent",
    "unpaid bills": "unpaid bills",
    "lease departures": "move-outs on file",
    "rent roll": "the rent roll",
    "vacant units": "vacant units",
    "tenant for unit": "who is on the lease",
    "contact requests": "resident contact requests",
    "rental owner requests": "owner requests",
    "resident requests": "resident requests",
}

#: Where the mechanical rule cannot win, because the name is not verb-shaped.
#: The specialists matter most — they are what the three-minute turn showed
#: five times, and each one is a whole nested agent run rather than a lookup.
_EXPLICIT = {
    "buildium_agent": "Checking Buildium",
    "gmail_agent": "Checking email",
    "calendar_agent": "Checking the calendar",
    "drive_agent": "Reading company documents",
    "document_agent": "Reading the document",
    "web_agent": "Searching the web",
    "browser_agent": "Looking at the page",
    "search_memory": "Recalling what you have told me",
    "portfolio_review": "Reviewing the whole portfolio",
    "portfolio_overview": "Getting the portfolio picture",
    "delinquency_report": "Pulling who is behind on rent",
    "expiring_leases": "Finding leases coming up for renewal",
    "rent_roll": "Building the rent roll",
    "lease_payment_history": "Reading the lease's payment history",
    "property_snapshot": "Pulling the property together",
    "contact_lookup": "Looking the person up",
    "unpaid_bills": "Checking unpaid bills",
    "send_text_message": "Preparing a text message",
    "run_computation": "Working through the numbers",
    "apply_actions": "Preparing the changes",
}

#: Vendor prefixes that carry no meaning for a reader. "buildium_get_property"
#: is "looking up the property" — naming the system says nothing about the step.
_STRIP_PREFIXES = ("buildium_", "mirror_", "helixis_")

_FALLBACK = "Working on it"

#: Connector toolkit -> what the reader calls it.
#:
#: ⚠ A live turn rendered ``GMAIL_FETCH_EMAILS`` as **"Checking the GMAIL FETCH
#: EMAILS"**, forty times in a row. The mechanical rule below is built for our
#: own lower_snake_case names: it strips a vendor prefix, reads the first word
#: as a verb, and when that fails says "Checking the <rest>" — which on a
#: SCREAMING_SNAKE Composio slug is the machine name with spaces in it.
#:
#: ⚠ **DERIVED, like the connector cap.** Composio tools are fetched per user
#: at runtime and drive alone carries ~186 of them, so there is no list to
#: hand-write and any list would rot. Toolkit plus verb is the rule; a
#: connector added next month reads as English on arrival.
_TOOLKITS: dict[str, str] = {
    "GMAIL": "email",
    "OUTLOOK": "email",
    "GOOGLEDRIVE": "the company files",
    "GOOGLEDOCS": "the document",
    "GOOGLESHEETS": "the spreadsheet",
    "GOOGLECALENDAR": "the calendar",
}

#: Composio verb -> how a person says it. Keyed on the SECOND segment of the
#: slug (``GMAIL_FETCH_EMAILS`` -> ``FETCH``).
_TOOLKIT_VERBS: dict[str, str] = {
    "FETCH": "Reading",
    "GET": "Reading",
    "LIST": "Looking through",
    "SEARCH": "Searching",
    "FIND": "Searching",
    "CREATE": "Creating",
    "SEND": "Sending",
    "REPLY": "Replying in",
    "UPDATE": "Updating",
    "DELETE": "Removing",
    "MOVE": "Moving",
    "ADD": "Adding to",
}


def _narrate_connector(name: str) -> str | None:
    """A sentence for a Composio slug, or None if this is not one.

    ⚠ Never returns the slug. An unknown toolkit is still a connector step,
    and "Checking a connected account" is honest about an unnamed source in a
    way that "Checking the FRESHBOOKS LIST INVOICES" is not.
    """
    if not name or not name.isupper() or "_" not in name:
        return None
    head, rest = name.split("_", 1)
    subject = _TOOLKITS.get(head)
    verb_word = rest.split("_", 1)[0]
    verb = _TOOLKIT_VERBS.get(verb_word)
    if subject is None:
        return "Checking a connected account"
    if verb is None:
        return f"Checking {subject}"
    return f"{verb} {subject}"


def narrate_step(tool_name: str | None) -> str:
    """One sentence describing the step. Never raises, never returns empty.

    ⚠ The fallback is deliberately vague rather than a machine name. "Working
    on it" is honest about an unknown step; "Called buildium_get_gl_account_
    balances" is noise that reads as a stack trace to somebody who just wanted
    to know if their email was drafted.
    """
    name = (tool_name or "").strip()
    if not name:
        return _FALLBACK
    if name in _EXPLICIT:
        return _EXPLICIT[name]

    connector = _narrate_connector(name)
    if connector is not None:
        return connector

    bare = name
    for prefix in _STRIP_PREFIXES:
        if bare.startswith(prefix):
            bare = bare[len(prefix) :]
            break

    parts = bare.split("_")
    if not parts or not parts[0]:
        return _FALLBACK
    verb = _VERBS.get(parts[0])
    subject = " ".join(parts[1:]).strip()
    if verb is None:
        # Not verb-shaped. Say the subject plainly rather than inventing an
        # action we cannot infer — a wrong verb is worse than none.
        return f"Checking {_subject(bare)}"
    if not subject:
        return verb.rstrip() + " it"
    return f"{verb} {_subject(subject)}"


def _subject(raw: str) -> str:
    """The noun phrase, with an article where English wants one."""
    words = raw.replace("_", " ").strip()
    if words in _SUBJECT_FIXES:
        return _SUBJECT_FIXES[words]
    for phrase, better in _SUBJECT_FIXES.items():
        if words.endswith(phrase):
            return better
    # Plurals ("properties", "tenants") take no article; singulars do.
    if re.search(r"(s|es)$", words) and not words.endswith("ss"):
        return f"the {words}"
    return f"the {words}"


def narrate_empty(tool_name: str | None) -> str:
    """The line for a step that returned nothing.

    ⚠ Says WHAT was empty and does not call it an answer. A step that came
    back with no rows is not evidence the record does not exist — this
    product's worst failure mode is a confident absence (gotcha 26), and the
    narration a user reads while it happens should not seed that reading.
    """
    return f"{narrate_step(tool_name)} — nothing came back"


__all__ = ["narrate_empty", "narrate_step"]
