"""The `@unverified` work list — and the reason `grep -c` cannot be it.

WHAT THE MARKER IS FOR
    Phases 4-6 build against a spec nobody has watched answer. The plan's
    bargain is that the code gets written now instead of waiting on a sandbox,
    and the cost — that none of it is proven — stays VISIBLE as a literal
    ``@unverified`` comment naming the assumption and how Phase 8 settles it.
    Phase 8 then discharges each one against a live account and deletes it.

    A marker deleted because the code "looks right" is the 200-but-lie shape
    CLAUDE.md gotcha 70 records: reporting success for a thing that did not
    happen. The tag is a claim that something was never checked.

⚠ WHY THIS EXISTS RATHER THAN THE GREP THE PLAN NAMES
    ``nightly-routines.md`` makes Phase 8's pass criterion
    ``grep -c '@unverified' src/helixis/rentvine/`` returning 0. Measured
    2026-09-17, that criterion **cannot reach zero**, for two independent
    reasons:

      1. It counts PROSE. ``transport.py`` mentions the marker twice while
         explaining the convention — once in a class docstring, once in an
         error message that tells a caller to mark their own call. Both are
         STRING tokens, neither is a work item, and both would still be there
         after every real marker was settled.
      2. It counts BYTECODE. ``grep -rc`` matches ``__pycache__/*.pyc`` —
         measured, it reports ``transport.cpython-311.pyc:2`` — so a stale
         compiled file keeps the count non-zero on its own.

    A criterion that cannot be satisfied gets satisfied the wrong way: by
    deleting the explanation, or — much worse — by deleting real markers to
    make the number go down. That is the same trap this repo has recorded five
    times (gotchas 40/59/91/114/124): **a scanner that cannot tell a defect
    from the sentence explaining it.** Here it landed on the plan's own gate.

    So this tool reads the token stream. A marker is ``@unverified`` in a
    COMMENT; anything inside a string or docstring is prose and is reported
    SEPARATELY rather than silently ignored — a reader who greps and sees two
    hits needs to find out here why they are not work.

USE
    python scripts/rentvine_unverified.py                 # the work list
    python scripts/rentvine_unverified.py --check         # format gate (CI)
    python scripts/rentvine_unverified.py --phase8        # Phase 8's real gate
"""

from __future__ import annotations

import argparse
import io
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = Path("src/helixis/rentvine")
MARKER = "@unverified"

#: A marker has to say how it gets settled, not just that it is unsettled.
#: An unsettleable tag is indistinguishable from a shrug at Phase 8, when the
#: person reading it has a live account and one afternoon.
SETTLE_CUES = ("phase 8", "phase8", "probe", "sandbox", "settle")

#: Enough text beyond the token to be a reason rather than a bare flag.
MIN_REASON_CHARS = 20


@dataclass
class Marker:
    path: str
    line: int
    text: str
    block: str = ""
    problems: list[str] = field(default_factory=list)


@dataclass
class Scan:
    markers: list[Marker] = field(default_factory=list)
    prose: list[Marker] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)

    @property
    def malformed(self) -> list[Marker]:
        return [m for m in self.markers if m.problems]


def _python_files(root: Path) -> list[Path]:
    """Source only.

    ⚠ The ``*.py`` glob is what excludes bytecode — it never matches ``.pyc``.
    An earlier cut also filtered ``__pycache__`` out of the parts, which reads
    like a second defence and is dead code: no input distinguishes the two
    (gotcha 58). Deleted rather than kept as decoration. What the glob must NOT
    become is ``*``, which would read compiled bytes and re-create exactly the
    overcount the plan's ``grep -rc`` has.
    """
    return sorted(root.rglob("*.py"))


def _comment_blocks(tokens: list[tokenize.TokenInfo]) -> dict[int, str]:
    """Map each comment's line to its contiguous run of comment lines.

    A marker's reason usually continues onto the next ``#`` line, so the format
    check has to read the block rather than the single line it started on.
    """
    comments = [t for t in tokens if t.type == tokenize.COMMENT]
    by_line = {t.start[0]: t.string.lstrip("#").strip() for t in comments}
    blocks: dict[int, str] = {}
    for line in by_line:
        parts = [by_line[line]]
        nxt = line + 1
        while nxt in by_line:
            parts.append(by_line[nxt])
            nxt += 1
        blocks[line] = " ".join(parts)
    return blocks


def scan_text(source: str, path: str) -> Scan:
    out = Scan()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        out.unreadable.append(f"{path}: {type(exc).__name__}: {exc}")
        return out

    blocks = _comment_blocks(tokens)
    for tok in tokens:
        if MARKER not in tok.string:
            continue
        line = tok.start[0]
        if tok.type == tokenize.COMMENT:
            block = blocks.get(line, tok.string.lstrip("#").strip())
            marker = Marker(path=path, line=line, text=tok.string.strip(), block=block)
            reason = block.replace(MARKER, "", 1).strip(" -—:")
            if len(reason) < MIN_REASON_CHARS:
                marker.problems.append(
                    f"no reason — a bare {MARKER} says something is unproven without "
                    f"saying what, which Phase 8 cannot act on"
                )
            if not any(cue in block.lower() for cue in SETTLE_CUES):
                marker.problems.append(
                    f"does not say how it gets settled — name one of {list(SETTLE_CUES)} "
                    f"so Phase 8 knows what call discharges it"
                )
            out.markers.append(marker)
        elif tok.type == tokenize.STRING:
            # Prose: a docstring or a message that MENTIONS the convention.
            # Reported so a reader who greps understands why the counts differ.
            out.prose.append(Marker(path=path, line=line, text=f"{MARKER} (in a string)"))
    return out


def scan(root: Path = DEFAULT_ROOT) -> Scan:
    total = Scan()
    if not root.exists():
        return total
    for path in _python_files(root):
        part = scan_text(path.read_text(encoding="utf-8"), str(path))
        total.markers.extend(part.markers)
        total.prose.extend(part.prose)
        total.unreadable.extend(part.unreadable)
    return total


def render(result: Scan) -> str:
    lines: list[str] = []
    if result.markers:
        lines.append(f"{len(result.markers)} assumption(s) awaiting Phase 8:\n")
        for m in result.markers:
            flag = "  ⚠ MALFORMED" if m.problems else ""
            lines.append(f"  {m.path}:{m.line}{flag}")
            lines.append(f"      {m.block[:160]}")
            for p in m.problems:
                lines.append(f"      -> {p}")
    else:
        lines.append("0 assumptions awaiting Phase 8.")
    if result.prose:
        lines.append(
            f"\n{len(result.prose)} mention(s) of the marker inside strings — PROSE, not work:"
        )
        for m in result.prose:
            lines.append(f"  {m.path}:{m.line}")
        lines.append(
            "  (this is why `grep -c` overcounts; these survive after every real\n"
            "   marker is settled, and deleting them to make a number go down is\n"
            "   the wrong fix)"
        )
    if result.unreadable:
        lines.append("\nCOULD NOT READ:")
        lines.extend(f"  {u}" for u in result.unreadable)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--check", action="store_true", help="fail if any marker is malformed")
    ap.add_argument("--phase8", action="store_true", help="fail if any marker remains")
    args = ap.parse_args(argv)

    try:  # Windows console (CLAUDE.md gotcha 49)
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    result = scan(Path(args.root))
    print(render(result))

    if result.unreadable:
        # A file we could not parse is not a file with no markers.
        print("\nVERDICT: COULD NOT RUN — a source file did not tokenize")
        return 2
    if args.check and result.malformed:
        print(f"\nVERDICT: {len(result.malformed)} malformed marker(s)")
        return 1
    if args.phase8 and result.markers:
        print(
            f"\nVERDICT: {len(result.markers)} assumption(s) still unproven — Phase 8 is not done"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
