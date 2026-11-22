"""Write-path probe for the §C reliability bar — run LOCALLY against the sandbox.

Exercises every Buildium WRITE endpoint the agent's tools use, end-to-end,
with read-back verification after each step:

  1. tasks.create           (to-do task)
  2. tasks.add_note
  3. tasks.update           ({"TaskStatus": "Completed"})
  4. work_orders.create     (embedded parent Task, per #200/#201)
  5. work_orders.add_note

Payload shapes are copied from the shipped write tools in
``agents/buildium_tools.py`` — the exact bodies chat sends after a confirm —
so a green run here validates the same wire contract the product uses. What
this deliberately does NOT cover (and chat still must): the confirm gate and
the idempotency guard, which live in the agent tool layer above these calls.

Run (PowerShell):
    $env:HELIXIS_BUILDIUM__CLIENT_ID     = Read-Host "Buildium sandbox client ID"
    $s = Read-Host "Buildium sandbox client secret" -AsSecureString
    $env:HELIXIS_BUILDIUM__CLIENT_SECRET = [Net.NetworkCredential]::new("", $s).Password
    python scripts\\buildium_write_probe.py

Artifacts are clearly labeled "Write-probe <UTC timestamp>" and left in place
so the webhook/mirror side can be verified independently (Buildium fires
Task.Created / WorkOrder.Created at our receiver; check ``buildium_events``).
Nothing is deleted. Output prints created IDs — paste it back for the
server-side verification pass.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any

from helixis.buildium.api import BuildiumApi
from helixis.buildium.client import BuildiumClient
from helixis.buildium.errors import BuildiumApiError
from helixis.settings import Settings

_RESULTS: list[tuple[str, bool, str]] = []


def _record(step: str, ok: bool, detail: str) -> None:
    _RESULTS.append((step, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {step}: {detail}")


async def main() -> int:
    settings = Settings()
    if not settings.buildium.client_id or not settings.buildium.client_secret:
        print("Set HELIXIS_BUILDIUM__CLIENT_ID / HELIXIS_BUILDIUM__CLIENT_SECRET first.")
        return 1
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    label = f"Write-probe {stamp}"
    print(f"base_url: {settings.buildium.base_url}\nlabel: {label}\n")

    async with BuildiumClient(settings.buildium) as client:
        api = BuildiumApi(client)

        # Real ids to attach the artifacts to — first property, first vendor,
        # first active staff user (assignee), matching what chat would pick.
        props = await api.rentals.list_properties()
        vendors = await api.vendors.list()
        users = await api.administration.list_users(usertypes=["Staff"], status="Active")
        if not props or not vendors or not users:
            print("Sandbox missing a property/vendor/staff user — cannot probe writes.")
            return 1
        prop_id = props[0].id
        vendor_id = vendors[0].id
        user_id = users[0].id
        print(f"using property={prop_id}, vendor={vendor_id}, assignee={user_id}\n")

        task_id: int | None = None
        wo_id: int | None = None

        # 1) tasks.create — same payload the confirm-gated tool sends.
        # Priority + TaskStatus are REQUIRED by Buildium (422 without them —
        # the probe finding that #478 fixed in the tool; mirrored here).
        try:
            created = await api.tasks.create(
                fields={
                    "Title": f"{label} — task A",
                    "Description": "Created by the write-path probe (safe to close).",
                    "Priority": "Normal",
                    "TaskStatus": "New",
                    "PropertyId": prop_id,
                    "AssignedToUserId": user_id,
                }
            )
            task_id = int(created.get("Id", 0)) or None
            fetched = await api.tasks.get(int(task_id)) if task_id else None
            title_ok = fetched is not None and f"{label} — task A" in (
                getattr(fetched, "title", "") or str(fetched)
            )
            _record("tasks.create + read-back", bool(task_id and title_ok), f"Id={task_id}")
        except BuildiumApiError as exc:
            _record("tasks.create", False, f"{exc.status}: {exc}")

        # 2) tasks.add_note — the note is the PUT's Message (the update
        # comment) and lands as a TASK HISTORY entry, NOT in Description
        # (probe round 3 live proof). Read back via history — WITH retries:
        # round 4 proved /tasks/{id}/history is eventually consistent (an
        # instant read missed the entry; a re-read seconds later had it),
        # so a single immediate GET false-negatives a good write.
        if task_id:
            try:
                note_text = f"{label} — note via probe"
                await api.tasks.add_note(task_id, note=note_text)
                found = False
                history: list[dict[str, Any]] = []
                for attempt in range(8):
                    if attempt:
                        await asyncio.sleep(1.5)
                    history = await api.tasks.list_history(task_id)
                    found = any(note_text in json.dumps(entry) for entry in history)
                    if found:
                        break
                _record(
                    "tasks.add_note + history read-back",
                    found,
                    f"note {'found' if found else 'MISSING'} in {len(history)} history "
                    f"entries (attempt {attempt + 1}/8)",
                )
            except BuildiumApiError as exc:
                _record("tasks.add_note", False, f"{exc.status}: {exc}")

        # 3) tasks.update — status flip, the exact partial body the tool sends.
        if task_id:
            try:
                await api.tasks.update(task_id, fields={"TaskStatus": "Completed"})
                fetched = await api.tasks.get(task_id)
                # The Task model aliases "Status"; permissive extras may carry
                # "TaskStatus" under its original key — accept either.
                status = (
                    getattr(fetched, "status", None)
                    or getattr(fetched, "task_status", None)
                    or getattr(fetched, "TaskStatus", None)
                )
                _record(
                    "tasks.update TaskStatus=Completed + read-back",
                    str(status).lower() == "completed",
                    f"status={status}",
                )
            except BuildiumApiError as exc:
                _record("tasks.update", False, f"{exc.status}: {exc}")

        # 4) work_orders.create — embedded parent Task shape (#200/#201).
        try:
            body: dict[str, Any] = {
                "PropertyId": prop_id,
                "Title": f"{label} — WO B",
                "Description": "Created by the write-path probe (safe to close).",
                "VendorId": vendor_id,
                "EntryAllowed": "Unknown",
                "Priority": "Normal",
                "Task": {
                    "Title": f"{label} — WO B",
                    "Description": "Created by the write-path probe (safe to close).",
                    "Priority": "Normal",
                    "Status": "New",
                    "AssignedToUserId": user_id,
                    "PropertyId": prop_id,
                },
            }
            wo = await api.work_orders.create(body)
            wo_id = wo.id
            fetched_wo = await api.work_orders.get(wo_id)
            _record(
                "work_orders.create + read-back",
                fetched_wo.id == wo_id,
                f"Id={wo_id}, status={fetched_wo.status}",
            )
        except BuildiumApiError as exc:
            _record("work_orders.create", False, f"{exc.status}: {exc}")

        # 5) work_orders.add_note — rides the WO PUT (no note endpoint exists);
        # read back WorkDetails and find the note line.
        if wo_id:
            try:
                wo_note = f"{label} — WO note via probe"
                await api.work_orders.add_note(wo_id, note=wo_note)
                fetched_wo = await api.work_orders.get(wo_id)
                details = str(getattr(fetched_wo, "work_details", None) or "")
                if not details:
                    raw = fetched_wo.model_dump(mode="json", by_alias=True)
                    details = str(raw.get("WorkDetails") or "")
                _record(
                    "work_orders.add_note + WorkDetails read-back",
                    wo_note in details,
                    f"note {'found' if wo_note in details else 'MISSING'} in WorkDetails",
                )
            except BuildiumApiError as exc:
                _record("work_orders.add_note", False, f"{exc.status}: {exc}")

    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    print(f"\n{passed}/{len(_RESULTS)} write steps verified.")
    print(
        "Paste this output back to Claude — the webhook (Task.Created / "
        "WorkOrder.Created arriving at the receiver) and mirror sides get "
        "verified server-side against these IDs."
    )
    return 0 if passed == len(_RESULTS) else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
