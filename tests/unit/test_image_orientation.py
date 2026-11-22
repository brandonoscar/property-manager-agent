"""A sideways photo reaches the model sideways, and the obvious fix is worse.

Every phone writes its sensor rows in one order and records how it was HELD in
an EXIF ``Orientation`` tag. Two paths base64 raw image bytes into an
``image_url`` block — ``image_tools`` (a maintenance photo, or the
handwritten-notice TRANSCRIPTION mode) and ``document_tools`` (a photographed
lease page). Both read text at least half the time, which is where a 90-degree
rotation costs the most.

⚠ The half that makes this worth guarding rather than just writing: rotating
the pixels and LEAVING the tag is strictly worse than doing nothing, because
any reader that honours orientation then rotates a second time. So the tests
below assert BOTH halves on the same output — upright pixels *and* a cleared
tag — because a one-sided test passes against the harmful version (gotchas
38/51).
"""

from __future__ import annotations

import ast
import io
import re
from pathlib import Path

import pytest

# ⚠ A PLAIN IMPORT, deliberately NOT ``pytest.importorskip``. Pillow absent
# means this feature is entirely dark, and a skip answering that question reads
# as a pass (gotcha 60's skip-floor). A loud collection error naming PIL is the
# signal wanted. The PRODUCT still degrades gracefully — that asymmetry is the
# point: the runtime keeps working, the test refuses to pretend it was checked.
from PIL import Image, ImageOps

from helixis.agents.image_orientation import IMAGE_MIME_TYPES, upright_image_bytes

_IMAGE_TOOLS = Path("src/helixis/agents/image_tools.py")
_DOCUMENT_TOOLS = Path("src/helixis/agents/document_tools.py")
_PYPROJECT = Path("pyproject.toml")


def _asymmetric_image() -> Image.Image:
    """A 40x20 image whose black half is on the LEFT.

    ⚠ Deliberately NOT square and NOT symmetric. A square fixture cannot tell a
    90-degree rotation from a no-op by SIZE, and a symmetric one cannot tell it
    by PIXELS — either would pass against code that does nothing at all.
    """
    im = Image.new("RGB", (40, 20), "white")
    for x in range(20):
        for y in range(20):
            im.putpixel((x, y), (0, 0, 0))
    return im


def _jpeg(orientation: int | None, image: Image.Image | None = None) -> bytes:
    buf = io.BytesIO()
    kwargs = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        kwargs["exif"] = exif
    (image or _asymmetric_image()).save(buf, format="JPEG", quality=92, **kwargs)
    return buf.getvalue()


# ── the safety property: pixels AND tag, on one output ───────────────


def test_a_sideways_photo_comes_back_upright() -> None:
    """Orientation 6 means "rotate 90 CW to display", so the black LEFT half
    of the stored pixels belongs at the TOP once corrected."""
    out = upright_image_bytes(_jpeg(6), "image/jpeg")
    fixed = Image.open(io.BytesIO(out)).convert("RGB")

    assert fixed.size == (20, 40), "the image was not rotated — it is still landscape"
    assert fixed.getpixel((5, 5)) == (0, 0, 0), "the black half did not move to the top"
    assert fixed.getpixel((5, 35)) == (255, 255, 255)


def test_the_orientation_tag_is_gone_so_nothing_rotates_it_twice() -> None:
    """THE OTHER HALF, and the one that makes this fix safe under a provider
    whose behaviour we cannot measure. Upright pixels still flagged "rotate me"
    double-rotate on any reader that honours EXIF — worse than the defect."""
    out = upright_image_bytes(_jpeg(6), "image/jpeg")
    assert Image.open(io.BytesIO(out)).getexif().get(274) is None


@pytest.mark.parametrize("orientation", [2, 3, 4, 5, 6, 7, 8])
def test_every_flagged_orientation_is_normalised_and_disarmed(orientation: int) -> None:
    """All seven non-upright values, not just the common phone one. 5 and 7 are
    transposes, which change the SIZE — code that handled only rotations would
    pass a 6-only test."""
    out = upright_image_bytes(_jpeg(orientation), "image/jpeg")
    assert Image.open(io.BytesIO(out)).getexif().get(274) is None
    # And it matches what Pillow itself would do, so the fix is not inventing
    # its own rotation table.
    expected = ImageOps.exif_transpose(Image.open(io.BytesIO(_jpeg(orientation))))
    assert Image.open(io.BytesIO(out)).size == expected.size


# ── the no-op property: the overwhelming majority of images ──────────


@pytest.mark.parametrize("orientation", [None, 1])
def test_an_upright_image_is_returned_byte_for_byte(orientation: int | None) -> None:
    """⚠ IDENTITY, not equivalence. A decode/re-encode of every image would
    spend generation loss and CPU on the common case for nothing — and on the
    handwriting-transcription path, quality loss is the exact cost this change
    exists to avoid."""
    raw = _jpeg(orientation)
    assert upright_image_bytes(raw, "image/jpeg") is raw


@pytest.mark.parametrize(
    ("raw", "mime"),
    [
        (b"%PDF-1.7\nnot really a pdf", "application/pdf"),
        (b"a synthetic photo description", "text/plain"),
    ],
)
def test_a_non_image_payload_is_never_handed_to_pillow(
    raw: bytes, mime: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``document_tools`` sends application/pdf through the SAME image_url
    block, and ``text/plain`` is the eval-deterministic mode.

    ⚠ ASSERTS THAT PILLOW IS NOT CALLED, not merely that the bytes come back
    unchanged — a mutation deleting the MIME gate stayed green against the
    weaker version, because ``Image.open`` on a PDF raises, the catch-all
    returns the original bytes, and the OUTPUT is identical either way. What
    the gate actually buys is not spending an exception and a warning log on
    every document extraction, so that is what is measured (gotcha 58: the
    mutant changed behaviour, so the hole was in the test).

    ⚠ COUNTS the call instead of RAISING on it, and the difference is the whole
    test. The first cut raised ``AssertionError`` from the patched ``open`` —
    which is an ``Exception``, so ``upright_image_bytes``' own catch-all
    swallowed it, returned the original bytes, and the guard passed against the
    very mutant it was rewritten to catch. A probe whose failure signal the
    code under test can absorb is not a probe (gotcha 33).
    """
    calls: list[object] = []

    def _record(*args: object, **_k: object) -> None:
        calls.append(args)

    monkeypatch.setattr(Image, "open", _record)

    assert upright_image_bytes(raw, mime) is raw
    assert not calls, f"Pillow was handed a {mime} payload"
    assert mime not in IMAGE_MIME_TYPES


# ── failure is always the original bytes ─────────────────────────────


def test_corrupt_bytes_are_sent_as_they_arrived() -> None:
    """Uprighting is an enhancement and must never be load-bearing (gotcha 7):
    a header Pillow cannot parse costs the rotation, never the assessment."""
    raw = b"\xff\xd8\xff\xe0 this is not a decodable jpeg"
    assert upright_image_bytes(raw, "image/jpeg") is raw


def test_empty_bytes_do_not_raise() -> None:
    assert upright_image_bytes(b"", "image/png") == b""


def test_a_png_stays_a_png() -> None:
    """Saving back in the SOURCE format, not one guessed from the MIME type.
    A PNG rewritten as a JPEG would silently lose transparency and add
    artefacts to a screenshot."""
    im = _asymmetric_image()
    buf = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    im.save(buf, format="PNG", exif=exif)
    out = upright_image_bytes(buf.getvalue(), "image/png")
    assert Image.open(io.BytesIO(out)).format == "PNG"
    assert Image.open(io.BytesIO(out)).size == (20, 40)


# ── it is wired, at BOTH of the two call sites ───────────────────────
#
# ⚠ A behavioural test of the helper cannot see a caller that ignores it
# (gotchas 62/66/69/103 — four prior instances in this repo of a well-tested
# helper a route quietly stopped calling). Both message builders are checked,
# because gotcha 25's fan-out is exactly the shape that reaches one layer and
# not the other.


def _encoded_call(path: Path, fn_name: str) -> ast.Call:
    """The ``b64encode(...)`` call inside ``fn_name``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "b64encode"
                ):
                    return call
    raise AssertionError(f"no b64encode call inside {fn_name} in {path}")


@pytest.mark.parametrize(
    ("path", "fn_name"),
    [(_IMAGE_TOOLS, "_build_messages"), (_DOCUMENT_TOOLS, "_build_messages")],
)
def test_the_bytes_that_reach_the_model_are_uprighted_first(path: Path, fn_name: str) -> None:
    """⚠ Asserts the ARGUMENT, not the presence of a name anywhere in the file.
    ``_ = upright_image_bytes(raw)`` beside an unchanged ``b64encode(raw_bytes)``
    satisfies a substring scan and ships the defect — gotcha 94's mutation that
    walked past its own guard, and gotcha 40's rule."""
    call = _encoded_call(path, fn_name)
    assert call.args, f"{path.name}:{fn_name} b64encode takes no argument"
    arg = call.args[0]
    assert isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name), (
        f"{path.name}:{fn_name} base64-encodes bytes that were not passed through "
        "a call — a sideways photo now reaches the model rotated"
    )
    assert arg.func.id == "upright_image_bytes", (
        f"{path.name}:{fn_name} encodes the output of {arg.func.id!r}, not upright_image_bytes"
    )


def test_the_mime_type_travels_with_the_bytes() -> None:
    """The helper decides from the MIME type whether to open the payload at
    all, so a call site that passes only the bytes turns the PDF gate off."""
    for path in (_IMAGE_TOOLS, _DOCUMENT_TOOLS):
        call = _encoded_call(path, "_build_messages")
        inner = call.args[0]
        assert len(inner.args) == 2, f"{path.name} calls upright_image_bytes with 1 argument"
        assert isinstance(inner.args[1], ast.Name) and inner.args[1].id == "mime_type"


# ── the corpus: a THIRD caller cannot skip it ────────────────────────


def test_no_other_module_base64s_an_image_payload_without_uprighting_it() -> None:
    """⚠ Reads the CORPUS, because the failure mode here is "silently sends a
    rotated image" — which looks exactly like working (gotcha 45). The loud
    direction, a crash, is the one you would have noticed anyway.

    Scoped to modules that build an ``image_url`` block, since that is what
    identifies bytes headed for a VISION model; base64 elsewhere (a webhook
    signature, an attachment store) is a different job.
    """
    offenders: list[str] = []
    checked = 0
    for path in sorted(Path("src/helixis").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "image_url" not in source:
            continue
        tree = ast.parse(source)
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "b64encode"
                and call.args
            ):
                continue
            checked += 1
            arg = call.args[0]
            ok = (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Name)
                and arg.func.id == "upright_image_bytes"
            )
            if not ok:
                offenders.append(f"{path}:{call.lineno}")

    assert checked >= 2, (
        "the scan found fewer than the two known image_url builders — a scan "
        "that finds nothing is a claim to check, not a result (gotchas 49/100/101)"
    )
    assert not offenders, (
        f"these base64 an image payload for a vision model without uprighting it first: {offenders}"
    )


def test_pillow_is_a_declared_dependency_and_not_a_lucky_transitive_one() -> None:
    """⚠ THE CORRECTION THAT PROMPTED THIS TEST. A session asserted Pillow
    "ships transitively with py-staticmaps" and built on it. Measured instead:
    a fresh environment here had Pillow present with ``Required-by:`` EMPTY and
    py-staticmaps not installed at all — so the provenance was incidental and
    the claim was gotcha 47's guess-in-a-measurement's-voice.

    An undeclared dependency that something depends on for CORRECTNESS goes
    dark the day an upstream drops it, with no error anywhere (gotcha 17). The
    declaration is the only thing that stops that, and prose cannot fail
    (gotcha 48) — so it is asserted.

    ⚠ PARSED, not substring-scanned. The first cut read the raw text, and a
    mutation that COMMENTED THE LINE OUT left ``"pillow`` sitting in the file
    and the guard green — gotcha 40's trap for the third time in this loop,
    arriving inside the guard written to prevent the very drift it describes.
    """
    import tomllib

    declared = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    names = {re.split(r"[<>=!~\[; ]", spec, maxsplit=1)[0].strip().lower() for spec in declared}
    assert "pillow" in names, (
        "Pillow is not a declared CORE dependency. image_orientation degrades "
        "silently without it, so a fresh install would upright nothing and "
        f"report no problem. Declared: {sorted(names)}"
    )
