"""Does this file's CONTENT match the type it claims to be?

WHY
---
Two upload paths take a document from a browser and they trusted different
things:

* ``POST /documents`` reads the bytes and checks them (``content.startswith
  (b"%PDF")``), stores to Supabase Storage with a hardcoded
  ``content_type="application/pdf"``, and hands back a signed URL. Correct —
  and the stored path is the one where a mislabelled file would matter most,
  because a browser opening that URL renders whatever it actually is.
* ``POST /agent/extract-document`` gates on ``body.mime_type``, a
  CLIENT-SUPPLIED string, and never looks at the bytes at all.

⚠ **BE PRECISE ABOUT WHAT THAT SECOND ONE IS, BECAUSE THE OBVIOUS FRAMING
OVERSTATES IT.** The bytes there are base64 in a JSON body; they are decoded,
handed to a multimodal model, and never written to a filesystem, executed, or
served back to a browser. So this is NOT the "executable disguised with a
benign extension" it looks like from the outside. What it actually is:

  the product tells a provider "this is a PDF", ships something else, and
  reports whatever comes back as a confident extraction from a lease.

That is this repo's own documented worst failure mode — a plausible wrong
answer — arriving through a field nobody validated (gotchas 26/41/51). It also
spends a real multimodal call on bytes that cannot answer.

⚠ **A sniffer is a NECESSARY check, never a sufficient one.** Matching magic
bytes proves the file starts the way its type starts; it does not prove the
file is safe, well-formed, or free of injected instructions — golden case 131
covers that separately, at the model. Do not let this module's existence read
as "uploads are validated".

THE SIGNATURES
--------------
Grounded in each format's own specification rather than a library, because the
set is five formats and a dependency here would be the larger commitment:

* PDF — ``%PDF`` (ISO 32000, header at offset 0)
* PNG — the 8-byte signature ``\\x89PNG\\r\\n\\x1a\\n`` (RFC 2083 §3.1), which
  is deliberately long: it catches newline-mangling transfers too
* JPEG — ``\\xff\\xd8\\xff`` (SOI marker + the start of the first segment)
* WebP — RIFF container: ``RIFF`` at 0 and ``WEBP`` at 8, with a 4-byte length
  in between that is NOT part of the identity

⚠ ``text/plain`` has no signature and never will — any byte string is valid
text. It is EVAL-ONLY (the deterministic fixture mode) and is deliberately
absent from the map rather than given a fake rule, so :func:`content_matches`
declines to judge it instead of pretending. A checker that invents a rule for
a format that has none is worse than one that says it cannot tell.
"""

from __future__ import annotations

from collections.abc import Callable

#: mime type → predicate over the leading bytes. Absence means "no signature
#: exists for this type", which is a different answer from "it does not match".
_SIGNATURES: dict[str, Callable[[bytes], bool]] = {
    "application/pdf": lambda b: b.startswith(b"%PDF"),
    "image/png": lambda b: b.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda b: b.startswith(b"\xff\xd8\xff"),
    # Both spellings reach us: `image/jpg` is not a registered type but
    # browsers and hand-written clients send it, and document_tools already
    # accepts it. Mapping it to the same rule beats rejecting a real JPEG on a
    # naming technicality.
    "image/jpg": lambda b: b.startswith(b"\xff\xd8\xff"),
    # RIFF container: the 4 bytes between the two tags are a length, not part
    # of the identity, so this cannot be a single startswith.
    "image/webp": lambda b: b.startswith(b"RIFF") and b[8:12] == b"WEBP",
}

#: Enough bytes for the longest check above (WebP needs 12).
SNIFF_BYTES = 16


def has_signature(mime_type: str) -> bool:
    """Is there a byte signature that can confirm this type at all?

    ``text/plain`` answers False — see the module docstring. Callers use this
    to tell "unverifiable" apart from "wrong", which are different outcomes and
    deserve different handling.
    """
    return mime_type in _SIGNATURES


def content_matches(content: bytes, mime_type: str) -> bool | None:
    """True / False / **None when the type has no signature to check**.

    ⚠ Three-valued on purpose. Collapsing ``None`` into ``True`` would silently
    wave through every future type somebody adds to the map's caller and not to
    the map; collapsing it into ``False`` would reject ``text/plain`` outright.
    Neither is honest, and a two-valued answer forces one of them — the same
    reasoning as ``write_verify``'s WROTE-UNVERIFIED (gotcha 70).
    """
    check = _SIGNATURES.get(mime_type)
    if check is None:
        return None
    return check(content[:SNIFF_BYTES])


__all__ = ["SNIFF_BYTES", "content_matches", "has_signature"]
