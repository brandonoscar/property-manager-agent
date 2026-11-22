"""The ``run_computation`` tool — agent-authored compute behind a plain-English
approval gate (code-execution Increment 2).

The safety spine (see the substrate in ``codeexec/executor.py`` + the ADR):

* **Compute, never act.** The tool runs Python in the zero-authority sandbox —
  no secrets, no network, no DB, no write path. It can answer, compute, and
  produce artifacts; it cannot move money or change records, because it was
  never handed the power to. Any real write happens elsewhere, through the
  existing confirm-gated write tools (that's a later increment).
* **Plain-English approval, every run.** The model must write ``plan`` — a
  business-English description of what the computation *does* — and the user
  approves THAT (not the Python) through ``confirm_or_abort``. The code rides
  along in ``request_body`` so the UI can show it secondary/collapsed. Because
  the gate lives inside the tool body, the model cannot reach execution
  without the user's click.
* **Self-gating / dark by default.** Returns a "not enabled" message unless
  ``settings.codeexec.enabled`` — same safe-off shape as the analyst.

Residual risk is bounded to "a wrong answer the user reviews": the English can
diverge from the code under injection, but a divergent run still can't act, so
the worst case is a bad draft, never a bad effect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from agents import function_tool

from helixis.agents.tools import confirm_or_abort
from helixis.agents.visual_tools import queue_artifacts
from helixis.codeexec import audit
from helixis.codeexec.apply import apply_proposed_actions
from helixis.codeexec.dispatch import default_dispatch
from helixis.codeexec.executor import run_code
from helixis.codeexec.storage_io import load_input_document, persist_artifacts
from helixis.memory.context import current_memory_context
from helixis.settings import get_settings

logger = logging.getLogger(__name__)

_MAX_STREAM_ECHO = 2_000  # chars of stdout/stderr echoed back to the model
_MAX_INPUT_DOC_BYTES = 25 * 1024 * 1024  # cap a fed-in document so it can't balloon the sandbox


async def _require_manager() -> str | None:
    """Manager+ fail-closed floor for code execution.

    Running arbitrary Python is a strictly larger capability than posting a
    charge, and posting a charge already requires Manager+ (gotcha 21). Before
    this, ``settings.codeexec.enabled`` was the ONLY gate — any ``member`` in
    any company could run code the moment the flag went true.

    Reuses the shared helper so the role vocabulary, the ordering, and the
    unverifiable→DENY behaviour stay in one place. Imported lazily: the module
    graph already runs ``codeexec.dispatch`` → ``agents.buildium_tools``, and a
    top-level import here would add a second edge into that cycle for no gain.
    """
    from helixis.agents.buildium_tools import _require_manager_role

    return await _require_manager_role(
        "running a custom computation",
        alt="ask a teammate with that role to run it",
    )


async def _resolve_input_document(document_name: str) -> tuple[dict[str, bytes] | None, str | None]:
    """Resolve a company-library document by name into an ``input_files`` entry.

    Company scope is enforced here: we only ever list + download documents that
    belong to the caller's company (``list_documents`` is company-keyed, and the
    storage path came from that row). Returns ``(input_files, None)`` on success
    or ``(None, message)`` with a user-facing explanation the tool returns
    verbatim — a requested-but-unresolvable document must NOT silently run
    without its input.
    """
    ctx = current_memory_context()
    company_id = ctx.company_id if ctx else None
    if not company_id:
        return None, "I couldn't determine which company you belong to — please re-open chat."

    try:
        from helixis.db.engine import get_pool
        from helixis.db.repositories import docs_links as docs_repo

        docs = await docs_repo.list_documents(get_pool(), company_id=company_id)
    except Exception:  # noqa: BLE001 — no pool (tests/scripts) or DB error
        logger.warning("codeexec: could not list documents for input", exc_info=True)
        return None, "I couldn't reach the Documents library right now — try again shortly."

    if not docs:
        return None, (
            "Your Documents library is empty, so there is no file to compute "
            "over. Do NOT point them at a place to upload one — the library "
            "has no upload screen in Occupella today."
        )
    needle = document_name.strip().lower()
    matches = [d for d in docs if d.name.strip().lower() == needle]
    if not matches:
        matches = [d for d in docs if needle in d.name.lower()]
    if not matches:
        names = ", ".join(d.name for d in docs[:10])
        return None, f'No document matches "{document_name}". Your library has: {names}.'
    if len(matches) > 1:
        names = ", ".join(d.name for d in matches[:10])
        return None, f"Multiple documents match: {names}. Which one?"
    doc = matches[0]

    try:
        filename, data = await load_input_document(
            storage_path=doc.storage_path, filename=doc.name, settings=get_settings()
        )
    except Exception:  # noqa: BLE001 — storage hiccup; don't run without the input
        logger.warning("codeexec: could not download input document %s", doc.name, exc_info=True)
        return None, f'I found "{doc.name}" but couldn\'t download it just now — try again shortly.'

    if len(data) > _MAX_INPUT_DOC_BYTES:
        return None, f'"{doc.name}" is too large to feed into a computation (max 25 MB).'
    return {filename: data}, None


async def _run_computation_impl(plan: str, code: str, document_name: str = "") -> str:
    settings = get_settings()
    if not settings.codeexec.enabled:
        return (
            "Code execution isn't enabled on this deployment, so I can't run a "
            "custom computation right now."
        )

    # Role floor BEFORE the document resolve and before the confirm gate: never
    # ask someone to approve a run they aren't permitted to make (the sms_tools
    # gate-ordering precedent — dark check, then permission, then the ask).
    denied = await _require_manager()
    if denied:
        return denied

    clean_plan = (plan or "").strip()
    clean_code = (code or "").strip()
    if not clean_plan:
        return (
            "I need a plain-English description of what the computation does "
            "(the `plan`) before I can ask the user to approve it."
        )
    if not clean_code:
        return "There's no code to run."

    # Optional: feed a company-library document's bytes into the sandbox as an
    # INPUT_DIR file. Resolved (company-scoped) BEFORE the approval gate so a
    # bad name is caught early and the user approves a run that actually has its
    # input. A requested-but-unresolvable document short-circuits — we never run
    # the code without the file the user expected.
    input_files: dict[str, bytes] | None = None
    if (document_name or "").strip():
        input_files, doc_error = await _resolve_input_document(document_name.strip())
        if doc_error is not None:
            return doc_error

    # Identity for the audit record, captured once up front — a run that is
    # DECLINED still has to be attributable, so this can't wait until the
    # artifact block further down.
    ctx = current_memory_context()
    company_id = ctx.company_id if ctx else None
    user_id = getattr(ctx, "user_id", None) if ctx else None
    started = time.monotonic()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    # Plain-English approval: the user sees + approves the PM action, not the
    # Python. The code rides in request_body (secondary/collapsible in the UI).
    approved = await confirm_or_abort(
        action="run_computation",
        title="Run this computation?",
        details=clean_plan,
        request_body={"language": "python", "code": clean_code},
    )
    if not approved:
        # A declined run is an audited event. "Someone was asked to approve
        # arbitrary code and said no" is exactly the kind of thing a reviewer
        # needs to see, and a trail that keeps only the happy path is useless.
        audit.record_run(
            company_id=company_id,
            user_id=user_id,
            code=clean_code,
            plan=clean_plan,
            approved=False,
            ok=False,
            duration_ms=_elapsed_ms(),
        )
        return "Cancelled — nothing was run."

    cx = settings.codeexec
    # The substrate is a blocking subprocess; keep it off the event loop. Any
    # resolved document lands in INPUT_DIR keyed by its filename.
    result = await asyncio.to_thread(
        run_code,
        clean_code,
        {},  # the model supplies literals inline; files arrive via INPUT_DIR
        input_files=input_files,
        wall_clock_s=cx.wall_clock_s,
        cpu_s=cx.cpu_s,
        memory_mb=cx.memory_mb,
        fsize_mb=cx.fsize_mb,
    )

    if not result.ok:
        audit.record_run(
            company_id=company_id,
            user_id=user_id,
            code=clean_code,
            plan=clean_plan,
            approved=True,
            ok=False,
            duration_ms=_elapsed_ms(),
            error=result.error,
            timed_out=result.timed_out,
        )
        # Structured, fenced error so the model can read it and self-correct
        # (bad column, syntax error, timeout) rather than guessing.
        return json.dumps(
            {
                "ok": False,
                "error": result.error,
                "timed_out": result.timed_out,
                "stderr": result.stderr[:_MAX_STREAM_ECHO],
            }
        )

    audit.record_run(
        company_id=company_id,
        user_id=user_id,
        code=clean_code,
        plan=clean_plan,
        approved=True,
        ok=True,
        duration_ms=_elapsed_ms(),
        artifacts=result.artifacts,
    )

    # Persist any generated files to storage and hand back signed download
    # links. Best-effort — a missing company scope or a storage hiccup degrades
    # to "artifacts listed by name, no link", never fails the run.
    artifact_links: list[Any] = []
    if result.artifact_contents:
        try:
            if company_id:
                artifact_links = await persist_artifacts(
                    result.artifact_contents, company_id=company_id, settings=settings
                )
                # Surface download buttons inline (no model URL relay), the
                # tabular/map precedent. Best-effort — never breaks the answer.
                queue_artifacts(artifact_links)
        except Exception:  # noqa: BLE001 — artifact delivery is best-effort
            logger.warning("codeexec: artifact persistence failed", exc_info=True)

    return json.dumps(
        {
            "ok": True,
            "result": result.result,
            "artifacts": result.artifacts,
            "artifact_links": artifact_links,
            "stdout": result.stdout[:_MAX_STREAM_ECHO],
        }
    )


@function_tool
async def run_computation(plan: str, code: str, document_name: str = "") -> str:
    """Run a short Python computation to answer or produce something no fixed
    tool covers — crunch numbers, transform data you already have in the
    conversation, convert or summarise an uploaded document, or compute a
    one-off result.

    This runs in a locked-down sandbox: no internet, no file access beyond a
    scratch ``OUTPUT_DIR``, no database, and NO ability to write to Buildium or
    change any record. Use it for read/compute/generate only. To actually
    change something (post a charge, create a task, send a message), use the
    dedicated write tools — never try to do it from here.

    The user must approve every run. Write ``plan`` as a clear, business-English
    sentence describing what the computation does and what it produces — that is
    what the user reads and approves, so no code jargon.

    In ``code`` you have: ``inputs`` (a dict — empty unless the caller provides
    one), ``INPUT_DIR`` (a str path holding any fed-in document, keyed by its
    filename), and ``OUTPUT_DIR`` (a str path for any file you generate). Set a
    ``result`` variable to whatever you want back (must be JSON-friendly). Files
    you write to ``OUTPUT_DIR`` come back to the user as download links.

    Args:
        plan: Plain-English description of what this computation does and
            produces. No Python, no jargon — the user approves this.
        code: The Python to run. Reads ``inputs`` / ``INPUT_DIR`` /
            ``OUTPUT_DIR``; returns via a ``result`` variable.
        document_name: Optional. The name of a file in the user's Documents
            library to feed into the sandbox — its bytes are placed in
            ``INPUT_DIR`` under this name. Leave empty when no document is
            needed. Use this for "convert/summarise/extract from this PDF".
    """
    return await _run_computation_impl(plan, code, document_name)


async def _apply_actions_impl(plan: str, actions: list[dict[str, Any]]) -> str:
    settings = get_settings()
    if not settings.codeexec.enabled:
        return (
            "Code execution isn't enabled on this deployment, so I can't apply actions right now."
        )
    # Same Manager+ floor as ``run_computation``: this is the write half of the
    # same capability, so gating one and not the other would leave the door open.
    denied = await _require_manager()
    if denied:
        return denied
    if not (plan or "").strip():
        return "I need a plain-English description of what these actions do before applying them."
    # The guard filters out anything not permitted; each allowed action is
    # applied through its own confirm-gated tool (the user approves each write).
    report = await apply_proposed_actions(actions, default_dispatch)
    return json.dumps(report.to_dict())


@function_tool
async def apply_actions(plan: str, actions_json: str) -> str:
    """Apply a batch of property-management actions you computed — e.g. add a
    note to each of several tasks, or set a computed status on a to-do.

    Each action is applied through its normal confirm-gated tool, so **the user
    approves every write** (one approval per action). Only reversible,
    non-financial writes are permitted; money-writes (charges/payments),
    closing or renewing, deletes, applicant decisions, tenant messages, and bulk
    batches are refused automatically and reported back — do not try to route
    those through here.

    Args:
        plan: Plain-English summary of what this batch accomplishes (the user
            sees this). No code jargon.
        actions_json: A JSON array string of ``{"action": <name>, "args": {...}}``
            items. Allowed actions include ``buildium_add_task_note``
            (task_id, note), ``buildium_add_work_order_note`` (work_order_id,
            note), ``buildium_add_vendor_note`` (vendor_id, note),
            ``buildium_create_to_do_task`` (title, …), and
            ``buildium_update_task_status`` (task_id, status).
    """
    try:
        actions = json.loads(actions_json)
    except (ValueError, TypeError):
        return 'The `actions_json` must be a JSON array of {"action", "args"} items.'
    return await _apply_actions_impl(plan, actions)


__all__ = [
    "_apply_actions_impl",
    "_run_computation_impl",
    "apply_actions",
    "run_computation",
]
