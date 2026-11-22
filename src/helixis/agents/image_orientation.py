"""A phone photo is stored sideways and flagged upright — fix the pixels, once.

Every camera on a phone writes its sensor rows in one fixed order and records
how the phone was HELD in an EXIF ``Orientation`` tag. The bytes are sideways;
the tag says which way to turn them. A viewer that reads the tag shows the
photo the right way up, and anything that ignores it sees a 90-degree rotation.

Two paths here hand raw image bytes to a vision model as a base64 ``image_url``
block — :mod:`helixis.agents.image_tools` (a maintenance photo, or the
handwritten-notice transcription mode) and :mod:`helixis.agents.document_tools`
(a photographed lease page). Both are text-reading modes at least half the
time, which is where a sideways image costs the most: a model asked to
transcribe a rotated notice is doing a harder job than the one it was given.

⚠ **WHETHER THE PROVIDER HONOURS THE TAG IS NOT MEASURED AND DOES NOT MATTER
HERE.** This container cannot reach the provider (gotcha 5), so the honest
position is that we do not know. The fix is built so both readings are correct:
``ImageOps.exif_transpose`` rotates the PIXELS *and drops the whole EXIF
block* — probed, not reasoned (``has exif block at all: False`` after a save
round trip) — so a provider that reads orientation sees ``1``-equivalent bytes
already upright, and one that ignores it sees upright bytes too. The failure
this avoids is the obvious half-fix: rotating pixels while leaving the tag
saying "rotate me", which double-rotates on any reader that honours it and is
strictly worse than doing nothing.

⚠ **AN ORIENTATION-1 IMAGE IS RETURNED BYTE-FOR-BYTE.** No decode, no
re-encode, no generation loss, no size change — which is the overwhelming
majority of images and every PNG screenshot. Only a photo actually flagged
sideways pays for a re-encode.

⚠ **FAILURE IS ALWAYS THE ORIGINAL BYTES.** Uprighting is an enhancement and
must never be load-bearing (gotcha 7): a corrupt header, a format Pillow will
not write back, a missing library — every one of those returns the input
untouched and lets the model see what it would have seen anyway.
"""

from __future__ import annotations

import io
import logging
from typing import Final

logger = logging.getLogger(__name__)

#: EXIF tag 274 is ``Orientation``. 1 means "already upright"; 2-8 are the
#: seven flips and rotations. Anything else (absent, 0, junk) is treated as
#: "nothing to do" — inventing a rotation from a value we do not recognise is
#: the same class of guess this repo refuses everywhere else (gotcha 55).
_ORIENTATION_TAG: Final = 274

#: Only these are handed to Pillow. ``text/plain`` is the eval-deterministic
#: mode and ``application/pdf`` reaches the SAME ``image_url`` block in
#: ``document_tools`` — a PDF is not an image Pillow can open, and feeding it
#: one would spend an exception on every document extraction.
IMAGE_MIME_TYPES: Final[frozenset[str]] = frozenset(
    {"image/png", "image/jpeg", "image/jpg", "image/webp"}
)

#: Re-encode quality for lossy formats. Measured on a worst-case noise image
#: (1200x1600, which JPEG cannot compress): 92 came back at 0.93x the original
#: size, so this does not inflate the payload. Lower would be smaller and is
#: refused deliberately — the mode that most needs uprighting is transcribing
#: handwriting, and that is exactly what quality loss costs.
_LOSSY_QUALITY: Final = 92

#: Formats whose writer takes ``quality``. A PNG saved with it warns; a
#: lossless format re-encoded at a quality setting is a contradiction.
_LOSSY_FORMATS: Final[frozenset[str]] = frozenset({"JPEG", "WEBP"})


def upright_image_bytes(raw_bytes: bytes, mime_type: str) -> bytes:
    """Return ``raw_bytes`` rotated to the way the photo was taken.

    Byte-for-byte identical to the input unless the image carries an EXIF
    orientation of 2-8. Never raises.
    """
    if mime_type not in IMAGE_MIME_TYPES:
        return raw_bytes

    try:
        from PIL import Image, ImageOps
    except Exception:  # pragma: no cover - Pillow ships with py-staticmaps
        # Not a failure worth a warning on every image: the product simply
        # sends what it was given, which is today's behaviour.
        logger.debug("Pillow unavailable; image orientation left as-is")
        return raw_bytes

    try:
        source = Image.open(io.BytesIO(raw_bytes))
        orientation = source.getexif().get(_ORIENTATION_TAG)
        if orientation in (None, 1):
            # The common case, and the reason this check is first: no decode,
            # no re-encode, no generation loss.
            return raw_bytes

        fmt = source.format
        if not fmt:
            # Pillow could not name the format, so it cannot be asked to write
            # one back. Guessing from the MIME type would write a JPEG over
            # whatever this actually is.
            return raw_bytes

        fixed = ImageOps.exif_transpose(source)
        if fixed is None:  # pragma: no cover - defensive; Pillow returns a copy
            return raw_bytes

        buffer = io.BytesIO()
        save_kwargs = {"quality": _LOSSY_QUALITY} if fmt in _LOSSY_FORMATS else {}
        fixed.save(buffer, format=fmt, **save_kwargs)
    except Exception as exc:
        logger.warning("could not upright image (%s); sending as-is: %s", mime_type, exc)
        return raw_bytes

    return buffer.getvalue()
