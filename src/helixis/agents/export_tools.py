"""Let an answer leave the chat.

Occupella can compute a rent roll, a delinquency report, a portfolio review —
and the only way to get one out of the product was to select the text and copy
it. Every downstream thing a property manager does with a report (send it to an
owner, open it in a spreadsheet, attach it to a filing) starts with a file, and
there was no file.

⚠ **The capability existed and was wired to the wrong thing.**
``codeexec/storage_io.persist_artifacts`` already uploads company-scoped bytes
and hands back a signed URL — its own docstring says *"this is how 'generate an
owner statement / CSV' reaches the user"*. It is reachable only from the code
sandbox, which is dark and must stay dark until the E2B keys are deployed
(gotcha 33). So the single path out of the chat ran through a disabled
subsystem, and serialising a table the agent has already computed never needed
a sandbox in the first place: it is a deterministic write, not arbitrary code.

⚠ **This is the one place identifier hygiene is deliberately OFF.** Every reply
surface scrubs bare record numbers (gotcha 38), because a model writing prose
should not be flinging identifiers around. An export is the opposite: a rent
roll without unit numbers is not a safer rent roll, it is a useless one. The
scrubbing stays on the REPLY — the sentence carrying the link — and off the
file. The bytes are protected by scope and expiry instead.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from typing import Any

from agents import function_tool

logger = logging.getLogger(__name__)

#: Historically the code sandbox was the only producer of downloadable files,
#: so the bucket carries its name. It is the agent-artifact bucket, and reusing
#: it is deliberate: a new bucket needs a migration applied BY HAND against the
#: live project (there is no runner — gotcha 8), which is one more thing that
#: can be committed and never actually exist.
_EXPORT_BUCKET = "codeexec_artifacts"

#: An export is read by a person or a spreadsheet, and neither wants 50,000
#: rows arriving through a chat turn. The cap is stated in the result so a
#: truncated file is never mistaken for the whole set (gotcha 26).
MAX_EXPORT_ROWS = 5000

#: Bytes ceiling, independent of the row cap — one row with a huge free-text
#: column can blow past a row count that looks modest.
MAX_EXPORT_BYTES = 5 * 1024 * 1024

_SAFE_NAME = re.compile(r"[^a-z0-9_-]+")

#: ⚠ A leading =, +, - or @ in a CSV cell is executed as a formula by Excel and
#: Sheets. That turns an exported tenant note into code running on the
#: recipient's machine — CSV injection, and the recipient is typically an OWNER
#: opening a file their property manager sent, so it arrives fully trusted.
#: Buildium free text is attacker-writable (a resident types the work-order
#: description), which makes this reachable rather than theoretical.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _safe_filename(title: str) -> str:
    stem = _SAFE_NAME.sub("-", (title or "export").strip().lower()).strip("-")
    return f"{stem[:60] or 'export'}.csv"


def _defuse(value: Any) -> str:
    """Render one cell, neutralising the spreadsheet-formula lead.

    Prefixed with an apostrophe rather than stripped: the value a tenant
    actually typed still has to be readable, and deleting characters from
    somebody's note to make it safe silently changes the record.
    """
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_LEAD):
        return "'" + text
    return text


def _to_csv(columns: list[str], rows: list[list[Any]]) -> tuple[bytes, int, bool]:
    """``(bytes, rows_written, truncated)``."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([_defuse(c) for c in columns])
    written = 0
    truncated = False
    for row in rows:
        if written >= MAX_EXPORT_ROWS:
            truncated = True
            break
        writer.writerow([_defuse(c) for c in row])
        written += 1
        if buf.tell() > MAX_EXPORT_BYTES:
            truncated = True
            break
    return buf.getvalue().encode("utf-8"), written, truncated


def _parse_rows(rows_json: str) -> list[list[Any]]:
    data = json.loads(rows_json)
    if not isinstance(data, list):
        raise ValueError("rows must be a JSON array")
    out: list[list[Any]] = []
    for row in data:
        if isinstance(row, list):
            out.append(row)
        elif isinstance(row, dict):
            out.append(list(row.values()))
        else:
            raise ValueError("each row must be a JSON array or object")
    return out


async def _export_table_impl(title: str, columns_json: str, rows_json: str) -> str:
    from helixis.agents.mirror_tools import _company_id
    from helixis.codeexec.storage_io import persist_artifacts
    from helixis.settings import get_settings

    company_id = _company_id()
    if not company_id:
        return "I can't build an export without knowing which company this is for."

    try:
        columns = json.loads(columns_json)
        if not isinstance(columns, list) or not columns:
            raise ValueError("columns must be a non-empty JSON array")
        rows = _parse_rows(rows_json)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return f"I couldn't read that table ({exc}). Pass columns and rows as JSON arrays."

    if not rows:
        return "There are no rows to export, so I didn't create a file."

    data, written, truncated = _to_csv([str(c) for c in columns], rows)
    name = _safe_filename(title)

    try:
        artifacts = await persist_artifacts(
            {name: data}, company_id=company_id, settings=get_settings()
        )
    except Exception:  # noqa: BLE001 — an export failing must not cost the answer
        logger.exception("export_table: persist failed")
        return (
            "I built the table but couldn't save it for download. The figures "
            "above are still correct — try the export again in a moment."
        )

    entry = artifacts[0] if artifacts else {}
    url = entry.get("url")
    if not url:
        return (
            "I built the table but couldn't produce a download link. The "
            "figures above are still correct."
        )

    note = f"Exported {written} rows to {name}: {url}"
    if truncated:
        # ⚠ Says what it cut and why, in the terms gotcha 26 demands — a file
        # silently holding the first 5,000 of 12,000 rows is a spreadsheet
        # somebody will total up and act on.
        note += (
            f"\n⚠ TRUNCATED at {MAX_EXPORT_ROWS} rows — this file is NOT the "
            "complete set. Narrow the question and export again if you need all of it."
        )
    note += "\nThe link is private to this company and expires in about an hour."
    return note


@function_tool
async def export_table(title: str, columns_json: str, rows_json: str) -> str:
    """Save a table you have ALREADY computed as a downloadable CSV.

    Use this when the user asks to export, download, or "send me" a report,
    rent roll, list, or any table you just produced — anything they will open
    in a spreadsheet or forward to an owner.

    ``columns_json`` is a JSON array of header strings; ``rows_json`` is a JSON
    array of rows (each an array in column order, or an object). Pass the data
    you already have — do NOT re-query to build an export.

    Returns a private download link that expires in about an hour.
    """
    return await _export_table_impl(title, columns_json, rows_json)


__all__ = ["MAX_EXPORT_BYTES", "MAX_EXPORT_ROWS", "export_table"]
