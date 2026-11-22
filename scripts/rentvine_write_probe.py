"""Settle the two Rentvine WRITE questions Phase 8 names and no instrument can reach.

WHY THIS EXISTS
    ``scripts/rentvine_unverified.py --phase8`` is Phase 8's work list AND its
    pass criterion. Two of its markers are questions about what a POST does,
    and both say, in the module itself, "Settled by a Phase 8 probe":

      src/helixis/rentvine/work_orders.py  is the update partial or a full replace?
      src/helixis/rentvine/work_orders.py  can a completed work order move back?

    ⚠ NEITHER THE LINE NUMBERS NOR THE TOTAL ARE WRITTEN HERE, and both used to
    be. The marker moved from line 56 to 63 and the list went from seventeen to
    fifteen within days, so a pointer that cited either sent the next reader to
    the wrong line and told them the wrong amount of work was left — while the
    thing it points at prints both correctly on demand. Run the script.

    Neither could be settled by anything in the repo. ``rentvine_probe.py``
    hardcodes its verb (``method="GET"  # the verb is not a variable``) and
    says so itself: *"Whether a POST to a record path REPLACES or MERGES is
    therefore NOT answered here. It is the sixth open question and it needs
    its own gated harness against a disposable record."*  ``rentvine_phase8.py``
    states it never calls ``post`` and a unit test enforces it. So the pass
    criterion named an event no command could cause and Phase 8 could never
    reach zero — CLAUDE.md gotcha 151, one table over. This file is that
    harness, and it is deliberately the SMALLEST thing that answers the two
    questions rather than a general write surface.

WHY THE FIRST QUESTION IS THE DANGEROUS ONE
    If the update is a full replace, then a one-field POST erases the work
    order's vendor, due date and instructions — and the only thing standing
    between a status change and an erased record is ``work_orders.merge_update``,
    whose correctness nobody has watched. The product's tool already sends the
    full merged body, so it is safe under BOTH readings; what is unknown is
    whether that care is load-bearing or free. This probe finds out by
    deliberately sending the unsafe shape ONCE, against a record the operator
    nominates as disposable, having captured the whole of it first.

    ⚠ So this file writes to a real account, and the restore is the only thing
    that undoes it. Every write is inside a ``try`` whose ``finally`` restores,
    the captured pre-state is printed and written to the report BEFORE the
    first write, and a failed restore is exit 1 with the body a human needs to
    rebuild the record by hand. A harness that could lose a customer's work
    order without saying which one is worse than no harness.

WHAT IT REUSES RATHER THAN RESTATES
    ``helixis.rentvine.work_orders`` already owns the update rules:
    ``writable_fields()`` (read from the generated index), ``NEVER_ECHOED``
    (the two side-effect triggers that email somebody else's contractor),
    ``merge_update``, ``resolve_status``. A second copy here would be the
    two-homes drift CLAUDE.md gotcha 25 is about, and it would drift in the
    expensive direction: a restore that echoed ``sendVendorNotification``
    mails a real vendor every time this runs.

CREDENTIALS
    Environment only — never arguments, because argv reaches shell history and
    process listings. The same three names ``rentvine_probe.py`` uses, so an
    operator who ran the probe has already done this step.

      $env:RENTVINE_ACCOUNT_CODE = "<subdomain>"
      $s = Read-Host "RENTVINE_API_KEY" -AsSecureString
      $env:RENTVINE_API_KEY      = [Net.NetworkCredential]::new("", $s).Password
      $s = Read-Host "RENTVINE_API_SECRET" -AsSecureString
      $env:RENTVINE_API_SECRET   = [Net.NetworkCredential]::new("", $s).Password

EXIT CODES
    0  both questions answered and the record was restored
    1  a real finding — the record was left MODIFIED and needs a human
    2  could not run, which is never a pass (LED-013's skip-floor)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from helixis.rentvine.work_orders import (  # noqa: E402
    GET_PATH,
    NEVER_ECHOED,
    QUIET,
    STATUS_FIELD,
    STATUSES_PATH,
    UPDATE_PATH,
    RentvineWorkOrderError,
    current_status_id,
    merge_update,
    resolve_status,
    status_choices,
    writable_fields,
)

_BASE = "https://{account}.rentvine.com/api/manager"

#: Where the report lands. NOT ``rentvine_probe.py``'s ``probe-report.json``
#: and NOT ``rentvine_phase8.py``'s output — a harness that overwrites another
#: harness's only copy of a live capture is one tab-completion from data loss
#: (gotcha 146, which is the same reasoning pointed at ``--scheme-out``).
DEFAULT_OUT = "docs/rentvine/write-probe-report.json"

#: A work order with almost nothing on it cannot answer question one: under a
#: full replace and under a partial update it comes back looking identical, so
#: the probe would report PARTIAL having measured nothing. That is gotcha 58's
#: "a mutation that cannot change any output" arriving as a live write, and it
#: would burn the operator's trip. The nominated record must carry at least
#: this many populated writable fields BESIDES the status we are about to
#: change.
MIN_WITNESS_FIELDS = 3

_UNKNOWN = "COULD NOT ANSWER"


class Creds:
    """The same three environment variables ``rentvine_probe.py`` reads."""

    def __init__(self) -> None:
        self.account = os.environ.get("RENTVINE_ACCOUNT_CODE", "").strip()
        self.key = os.environ.get("RENTVINE_API_KEY", "").strip()
        self.secret = os.environ.get("RENTVINE_API_SECRET", "").strip()

    @property
    def complete(self) -> bool:
        return bool(self.account and self.key and self.secret)

    @property
    def missing(self) -> list[str]:
        names = {
            "RENTVINE_ACCOUNT_CODE": self.account,
            "RENTVINE_API_KEY": self.key,
            "RENTVINE_API_SECRET": self.secret,
        }
        return [n for n, v in names.items() if not v]

    def base(self) -> str:
        return _BASE.format(account=self.account)

    def header(self) -> str:
        raw = f"{self.key}:{self.secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()


def _redact(text: str, creds: Creds) -> str:
    """Never let a secret reach the report or the terminal."""
    out = text
    for secret in (creds.secret, creds.key):
        if secret:
            out = out.replace(secret, "***")
    return out


def _request(
    creds: Creds,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: int = 45,
) -> dict[str, Any]:
    """The one request function.

    ⚠ Unlike ``rentvine_probe.py``, the verb here IS a variable — that is the
    whole point of this file, and it is why this harness is separate rather
    than a flag on that one. Keeping the read-only probe structurally unable
    to write is worth more than sharing a transport.
    """
    url = creds.base() + path
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", creds.header())
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    out: dict[str, Any] = {
        "method": method,
        "url": _redact(url, creds),
        "status": None,
        "body": None,
        "error": None,
    }
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out["status"] = resp.status
            out["body"] = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        out["status"] = exc.code
        try:
            out["body"] = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - the body is diagnostics, never load-bearing
            out["body"] = None
    except Exception as exc:  # noqa: BLE001 - a transport failure is a finding
        out["error"] = f"{type(exc).__name__}: {exc}"
    if out["body"]:
        out["body"] = _redact(str(out["body"]), creds)
    return out


def unwrap_record(payload: Any) -> dict[str, Any] | None:
    """The record out of whatever a single-record GET returns.

    ⚠ ``GET /maintenance/work-orders/{workOrderID}`` is ABSENT from
    ``_row_envelopes.ROW_ENVELOPE``, because that map covers collections. So
    the single-record envelope is itself unknown — it is the other half of
    ``client.py:331``'s marker — and this accepts either shape rather than
    guessing one. Which shape actually arrived is recorded in the report, so
    one run answers a third question in passing.
    """
    if not isinstance(payload, dict):
        return None
    # ⚠ A bare work order carries its own id; nothing is unwrapped from one.
    if "workOrderID" in payload:
        return payload
    # The record is the one object inside, whatever sits beside it. Measured
    # 2026-10-02 (and the spec's own example): the GET returns
    # {"workOrder": {...}, "schedulingStatusID": ...}. Reading only a ONE-key
    # wrapper took that whole envelope for the record, found no writable field
    # on it, and refused a work order with twenty populated (exit 2).
    objects = [v for v in payload.values() if isinstance(v, dict)]
    if len(objects) == 1:
        return objects[0]
    return payload


def envelope_shape(payload: Any) -> str:
    """``"wrapped:<key>"``, ``"bare"``, or ``"unreadable"`` — recorded, not assumed."""
    if not isinstance(payload, dict):
        return "unreadable"
    if "workOrderID" in payload:
        return "bare"
    objects = [k for k, v in payload.items() if isinstance(v, dict)]
    if len(objects) == 1:
        siblings = sorted(k for k in payload if k != objects[0])
        return f"wrapped:{objects[0]}" + (f" beside {','.join(siblings)}" if siblings else "")
    return "bare"


def witness_fields(record: dict[str, Any]) -> list[str]:
    """Populated writable fields OTHER than the status, sorted.

    These are what the probe watches. If a full replace happens, these are the
    fields that come back missing; if nothing else is populated there is
    nothing to watch and the probe cannot answer (``MIN_WITNESS_FIELDS``).
    """
    try:
        allowed = writable_fields()
    except RentvineWorkOrderError:
        return []
    return sorted(
        k
        for k, v in record.items()
        if k in allowed and k != STATUS_FIELD and v is not None and v != ""
    )


def replace_verdict(before: dict[str, Any], after: dict[str, Any], watched: list[str]) -> str:
    """``PARTIAL``, ``FULL REPLACE``, or ``COULD NOT ANSWER``.

    ⚠ Three-valued on purpose. With nothing watched the two readings are
    indistinguishable, and reporting PARTIAL there would be a confident answer
    drawn from no evidence — the failure this file's ``MIN_WITNESS_FIELDS``
    gate exists to prevent, restated so a caller that skips the gate still
    cannot get a fabricated verdict.
    """
    if not watched:
        return _UNKNOWN
    lost = [k for k in watched if after.get(k) in (None, "") and before.get(k) not in (None, "")]
    if lost:
        return "FULL REPLACE"
    return "PARTIAL"


def _write_report(path: str, report: dict[str, Any]) -> str:
    out = pathlib.Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return str(out)


def _print_plan(
    record: dict[str, Any], watched: list[str], target: Any, statuses: list[str]
) -> None:
    print("  DRY RUN — nothing has been sent.")
    print(f"  work order carries {len(watched)} watchable field(s): {', '.join(watched)}")
    print(f"  account statuses: {', '.join(statuses) or '(none readable)'}")
    print(f"  current {STATUS_FIELD}: {current_status_id(record)!r}")
    print()
    print("  Probe 1 WOULD POST this one-field body, then re-GET and compare:")
    print(f"    {json.dumps({STATUS_FIELD: target})}")
    print("    If the watched fields come back blank, the update is a FULL REPLACE")
    print("    and merge_update is the only thing preventing an erased work order.")
    print()
    print("  Probe 2 WOULD then move the status and move it back, through the")
    print("  product's own merge_update, to see whether the move back takes.")
    print()
    print("  Both restore the captured body afterwards. Re-run with --execute")
    print("  --i-accept-the-write to actually send them.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Settle the two Rentvine write questions on Phase 8's work list.",
    )
    parser.add_argument(
        "--work-order-id",
        help=(
            "A DISPOSABLE sandbox work order carrying a vendor, a due date and "
            "instructions. Required: this harness will not pick one for you, "
            "because nothing here can know which record is safe to modify."
        ),
    )
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"report path (default {DEFAULT_OUT})")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report which credentials are set and send NOTHING.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually send the writes. Without this the run is a dry run.",
    )
    parser.add_argument(
        "--i-accept-the-write",
        action="store_true",
        help=(
            "required beside --execute. This modifies a real work order and "
            "relies on its own restore to put it back."
        ),
    )
    args = parser.parse_args(argv)

    creds = Creds()

    if args.check:
        print("Rentvine write probe — credential check (nothing sent)")
        for name in ("RENTVINE_ACCOUNT_CODE", "RENTVINE_API_KEY", "RENTVINE_API_SECRET"):
            print(f"  {name}: {'set' if os.environ.get(name, '').strip() else 'NOT SET'}")
        print(f"  --work-order-id: {args.work_order_id or 'NOT SET'}")
        if not creds.complete or not args.work_order_id:
            print("\nNot runnable yet. Set the missing values above.")
            return 2
        print("\nRunnable. Next: omit --check for a dry run.")
        return 0

    if not creds.complete:
        print(f"Missing credentials: {', '.join(creds.missing)}", file=sys.stderr)
        print("Nothing was sent. This is exit 2 — could not run, not a pass.", file=sys.stderr)
        return 2

    if not args.work_order_id:
        print("--work-order-id is required and there is no default.", file=sys.stderr)
        print(
            "Nominate a DISPOSABLE sandbox work order: this harness modifies it.",
            file=sys.stderr,
        )
        return 2

    if args.execute and not args.i_accept_the_write:
        print("--execute needs --i-accept-the-write beside it.", file=sys.stderr)
        print(
            "Probe 1 sends a deliberately unsafe one-field body. If Rentvine's "
            "update is a full replace, that erases the work order's other "
            "fields until the restore puts them back.",
            file=sys.stderr,
        )
        return 2

    report: dict[str, Any] = {
        "work_order_id": args.work_order_id,
        "executed": bool(args.execute),
        "single_record_envelope": _UNKNOWN,
        "pre_state": None,
        "watched_fields": [],
        "replace_semantics": _UNKNOWN,
        "reversibility": _UNKNOWN,
        "restored": None,
        "notes": [],
    }

    wo_path = GET_PATH.format(workOrderID=args.work_order_id)
    got = _request(creds, "GET", wo_path)
    if got["status"] != 200 or not got["body"]:
        report["notes"].append(f"could not read the work order: status={got['status']}")
        print(f"Could not read work order {args.work_order_id}: status={got['status']}")
        print(f"Report: {_write_report(args.out, report)}")
        return 2

    try:
        payload = json.loads(got["body"])
    except json.JSONDecodeError as exc:
        report["notes"].append(f"work order body was not JSON: {exc}")
        print(f"Report: {_write_report(args.out, report)}")
        return 2

    report["single_record_envelope"] = envelope_shape(payload)
    record = unwrap_record(payload)
    if not record:
        report["notes"].append("could not find a record inside the response")
        print(f"Report: {_write_report(args.out, report)}")
        return 2

    report["pre_state"] = record
    watched = witness_fields(record)
    report["watched_fields"] = watched

    statuses_resp = _request(creds, "GET", STATUSES_PATH)
    status_rows: list[dict[str, Any]] = []
    if statuses_resp["status"] == 200 and statuses_resp["body"]:
        try:
            raw = json.loads(statuses_resp["body"])
            if isinstance(raw, dict):
                for value in raw.values():
                    if isinstance(value, list):
                        raw = value
                        break
            if isinstance(raw, list):
                status_rows = [unwrap_record(r) or {} for r in raw]
        except json.JSONDecodeError:
            status_rows = []
    choices = status_choices(status_rows)
    names = sorted(choices)
    report["account_statuses"] = names

    if len(watched) < MIN_WITNESS_FIELDS:
        report["notes"].append(
            f"work order carries only {len(watched)} populated writable field(s) "
            f"besides the status; {MIN_WITNESS_FIELDS} are needed. Under a full "
            f"replace and under a partial update it would come back looking "
            f"identical, so the probe would report PARTIAL having measured "
            f"nothing. Nominate a work order with a vendor, a due date and "
            f"instructions."
        )
        print(f"Cannot answer with work order {args.work_order_id}:")
        print(f"  {report['notes'][-1]}")
        print(f"Report: {_write_report(args.out, report)}")
        return 2

    current = current_status_id(record)
    # PREFER "Completed", because that is the status the marker asks about:
    # Buildium's equivalent latches one-way with no API reopen (gotcha 20), so
    # "can it move back" is sharpest from a TERMINAL status and weakest from an
    # arbitrary one. ⚠ The vocabulary is per ACCOUNT — `status_choices`' own
    # docstring says "Completed" is not guaranteed to exist — so this falls back
    # to any other status rather than refusing, and RECORDS which one it used.
    # A verdict that does not name the status it exercised is a claim about a
    # status list the reader cannot see.
    completed, _all_names = resolve_status(status_rows, "completed")
    target = completed if completed not in (None, current) else None
    if target is None:
        target = next((choices[n] for n in names if choices[n] != current), None)
    report["reversibility_status_used"] = next((n for n in names if choices[n] == target), None)
    if target is None:
        report["notes"].append(
            "the account has no status other than the one this work order is "
            "already in, so there is nothing to change it to."
        )
        print(f"  {report['notes'][-1]}")
        print(f"Report: {_write_report(args.out, report)}")
        return 2

    if not args.execute:
        _print_plan(record, watched, target, names)
        print(f"\nReport (plan only): {_write_report(args.out, report)}")
        return 0

    # ---- from here on the account is being modified -------------------------
    exit_code = 0
    try:
        # One field of STATE. The triggers ride along false: the spec defaults
        # sendVendorNotification to TRUE, so leaving it out mails the vendor
        # (gotcha 191). They are not state, so the replace verdict is unchanged.
        one_field = {STATUS_FIELD: target, **QUIET}
        report["probe1_body_sent"] = one_field
        posted = _request(
            creds, "POST", UPDATE_PATH.format(workOrderID=args.work_order_id), one_field
        )
        report["probe1_post_status"] = posted["status"]

        after_resp = _request(creds, "GET", wo_path)
        after = {}
        if after_resp["status"] == 200 and after_resp["body"]:
            after = unwrap_record(json.loads(after_resp["body"])) or {}
        report["probe1_after"] = after
        report["replace_semantics"] = replace_verdict(record, after, watched)

        # Probe 2 rides on the product's own merge, which is the path a
        # customer's work order actually takes.
        back = merge_update(after or record, {STATUS_FIELD: current})
        report["probe2_body_sent"] = back
        _request(creds, "POST", UPDATE_PATH.format(workOrderID=args.work_order_id), back)
        back_resp = _request(creds, "GET", wo_path)
        moved_back = {}
        if back_resp["status"] == 200 and back_resp["body"]:
            moved_back = unwrap_record(json.loads(back_resp["body"])) or {}
        report["reversibility"] = (
            "REVERSIBLE"
            if current_status_id(moved_back) == current
            else "ONE-WAY — the status did not move back"
        )
    finally:
        restore = merge_update(record, {})
        assert all(restore.get(k) is False for k in NEVER_ECHOED), (
            "restore must send every side-effect trigger false, never echo or omit one"
        )
        report["restore_body"] = restore
        done = _request(creds, "POST", UPDATE_PATH.format(workOrderID=args.work_order_id), restore)
        final = _request(creds, "GET", wo_path)
        final_record = {}
        if final["status"] == 200 and final["body"]:
            final_record = unwrap_record(json.loads(final["body"])) or {}
        lost = [k for k in watched if final_record.get(k) in (None, "")]
        report["restored"] = not lost and done["status"] in (200, 201, 204)
        if not report["restored"]:
            report["notes"].append(
                f"RESTORE DID NOT CONFIRM (status={done['status']}, still-missing={lost}). "
                f"The work order is left modified. `restore_body` in this report "
                f"is what it should contain."
            )
            exit_code = 1

    path = _write_report(args.out, report)
    print(f"  single-record envelope : {report['single_record_envelope']}")
    print(f"  update semantics       : {report['replace_semantics']}")
    print(f"  status reversibility   : {report['reversibility']}")
    print(f"  record restored        : {report['restored']}")
    for note in report["notes"]:
        print(f"  ⚠ {note}")
    print(f"\nReport: {path}")
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
