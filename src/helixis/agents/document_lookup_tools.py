"""Read the company's OWN uploaded documents — the half that was never wired.

``company_documents`` has a complete write path: an upload route, a client that
calls it, storage for the bytes, and a daily distiller that extracts each file
into a ``text_digest``. What it has never had is a way for the agent to look
one up. The digests feed a single auto-maintained markdown profile injected
into every prompt, so whatever the distiller squeezed into that summary is all
the agent can ever see — ask "what coverage limit does our vendor insurance
certificate require?" and there is no path to the document that says.

⚠ **The corpus is EMPTY in every company today (measured 2026-08-29: zero rows,
all three), and that is the whole reason this tool is careful rather than the
reason not to build it.** A search over an empty store that answers "no
documents mention that" is a fabricated-absence generator — this product's
worst failure mode (gotchas 26/55), and exactly the trap that made a
``vendor_payments`` rollup the wrong fix for the missing paid bills (gotcha 57).
The difference is that paid bills need a sync change nobody has made, while
this becomes correct the moment a user uploads their first SOP. So it ships,
and it distinguishes three states that a naive tool collapses into one:

* **nothing uploaded** — a fact about the STORE, not about any document;
* **uploaded, none matching** — a real search over a real corpus, so an honest
  "not in these N files", with the names listed so the user can see what WAS
  searched;
* **matched but not yet extracted** — the file exists and its contents are
  unknown, which must never read as "the document does not mention it"
  (gotcha 51's rule that unknown must not render as current).

Modelled on ``drive_lookup_tools``, deliberately: same one-call-off-a-cached-
index shape, same refusal to report a source as empty on our own limitation.
"""

from __future__ import annotations

import logging
import re

from agents import function_tool

from helixis.memory.context import current_memory_context

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 5
_MAX_LIMIT = 15
#: Enough of the digest to answer "does this say anything about X", short
#: enough that five hits do not crowd out the answer.
_EXCERPT_CHARS = 600

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "our",
        "of",
        "for",
        "and",
        "what",
        "does",
        "do",
        "is",
        "are",
        "in",
        "on",
        "to",
        "we",
        "us",
        "say",
        "says",
        "about",
        "any",
        "have",
        "has",
        "with",
        "that",
        "this",
        "it",
        "from",
        "can",
        "i",
        "my",
    }
)


def _terms(query: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9'-]*", (query or "").lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 2][:8]


def _excerpt(digest: str, terms: list[str]) -> str:
    """The window around the first matching term, so the model sees WHY this
    document matched instead of the first 600 characters of a cover page."""
    low = digest.lower()
    at = next((low.index(t) for t in terms if t in low), 0)
    start = max(0, at - _EXCERPT_CHARS // 3)
    body = digest[start : start + _EXCERPT_CHARS].strip()
    return ("…" if start else "") + body + ("…" if start + _EXCERPT_CHARS < len(digest) else "")


async def _company_docs_find_impl(query: str, limit: int = _DEFAULT_LIMIT) -> str:
    from helixis.db.engine import get_pool
    from helixis.db.repositories.docs_links import documents_for_knowledge
    from helixis.security.prompt_delimit import wrap_untrusted

    ctx = current_memory_context()
    company_id = ctx.company_id if ctx else ""
    if not company_id:
        return (
            "Document lookup unavailable: no company context on this run. This is "
            "OUR limitation — do NOT report the company as having no documents."
        )

    limit = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))
    terms = _terms(query)
    if not terms:
        return 'Give me something to search on — "insurance", "lease template", a filename.'

    try:
        docs = await documents_for_knowledge(get_pool(), company_id=company_id, limit=100)
    except Exception as exc:  # noqa: BLE001 — a lookup must degrade, never raise
        logger.warning("company document lookup failed: %s", exc, exc_info=True)
        return (
            "The document library could not be read just now (an error on OUR "
            "side). Do NOT report the company as having no documents — say the "
            "library was unavailable and offer to try again."
        )

    if not docs:
        # ⚠ A fact about the STORE, said as one. "No documents mention X" would
        # be a claim about content we have never held.
        return (
            "This company has no documents uploaded to Occupella at all — the "
            "library is empty, which is NOT the same as 'nothing on file mentions "
            "that'. Say so plainly. Do NOT tell them where to upload one: the "
            "document library has no upload screen in Occupella today, so naming "
            "a place to go would send them somewhere that does not exist. "
            "(Files in a connected Google Drive are separate — use the Drive "
            "lookup for those.)"
        )

    hits = []
    for doc in docs:
        haystack = f"{doc.name} {doc.kind} {doc.text_digest or ''}".lower()
        score = sum(1 for t in terms if t in haystack)
        if score:
            hits.append((score, doc))
    hits.sort(key=lambda pair: -pair[0])

    if not hits:
        names = ", ".join(d.name for d in docs[:10])
        more = f" (+{len(docs) - 10} more)" if len(docs) > 10 else ""
        return (
            f"None of the {len(docs)} documents on file match '{query}'. "
            f"Searched: {names}{more}. That is a real result over a real library "
            "— but it only covers what has been uploaded to Occupella, so say "
            "which files you checked rather than answering as if nothing exists."
        )

    lines = [f"{len(hits)} of {len(docs)} uploaded documents match '{query}':"]
    for _, doc in hits[:limit]:
        lines.append(f"\n— {doc.name} ({doc.kind})")
        if not doc.text_digest:
            # ⚠ Third state. The file is here and its contents are unknown;
            # letting that read as "it doesn't say" would be a false absence
            # about a document sitting in the library.
            lines.append(
                "  ⚠ CONTENTS NOT EXTRACTED YET — this file matched on its NAME "
                "only. Do not say what it does or does not contain."
            )
            continue
        lines.append(
            "  "
            + wrap_untrusted(
                _excerpt(doc.text_digest, terms),
                label=f"uploaded document '{doc.name}', written by someone outside Occupella",
            )
        )
    if len(hits) > limit:
        lines.append(f"\n({len(hits) - limit} further matches not shown — narrow the search.)")
    return "\n".join(lines)


@function_tool
async def company_docs_find(query: str, limit: int = _DEFAULT_LIMIT) -> str:
    """Search the company's OWN uploaded documents (SOPs, lease templates,
    insurance certificates, policies) and return matching excerpts, in ONE call.

    Use this whenever a question turns on what the company's own paperwork
    says — "what's our late-fee policy", "what coverage do we require from
    vendors", "what does our lease template say about pets".

    This covers files uploaded to Occupella. Files in a connected Google Drive
    are a different corpus — use the Drive lookup for those.

    Instant (no file downloads, no external API calls).
    """
    return await _company_docs_find_impl(query, limit)


__all__ = ["company_docs_find"]
