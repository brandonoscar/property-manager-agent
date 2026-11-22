"""The write probe must answer the two Phase 8 write questions, or say it could not.

WHY THIS FILE EXISTS
    ``scripts/rentvine_write_probe.py`` is the instrument for
    ``work_orders.py``'s two ``@unverified`` markers, which both read "Settled
    by a Phase 8 probe" and which no other harness can reach: ``rentvine_probe``
    hardcodes ``method="GET"`` and ``rentvine_phase8`` asserts it never calls
    ``post``. So Phase 8's pass criterion named an event no command could
    cause (CLAUDE.md gotcha 151).

    The two things this file is really guarding are not the happy path:

    1. **A verdict drawn from no evidence.** A work order carrying nothing but
       a status looks IDENTICAL after a full replace and after a partial
       update, so a probe run against one would report PARTIAL having measured
       nothing — gotcha 58's "a mutation that cannot change any output",
       arriving as a live write against a customer's account. Both the gate
       and the verdict function must refuse.
    2. **A restore that emails somebody else's contractor.**
       ``sendVendorNotification`` and ``sendReview`` are side-effect triggers,
       not state. The restore body is built by the product's own
       ``merge_update`` precisely so they cannot be echoed; a hand-rolled
       restore here would re-open that (gotcha 25's two-homes drift, in its
       expensive direction).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "rentvine_write_probe.py"


def _load():
    spec = importlib.util.spec_from_file_location("rentvine_write_probe", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["rentvine_write_probe"] = module
    spec.loader.exec_module(module)
    return module


probe = _load()


# --------------------------------------------------------------------------
# 1. The verdict cannot be fabricated
# --------------------------------------------------------------------------


def test_nothing_watched_means_could_not_answer_not_partial():
    """The distinguishing input: with no watched field the two readings agree.

    Reporting PARTIAL here is the whole failure mode — a confident answer to a
    question the run did not ask, on the marker that decides whether
    ``merge_update`` is load-bearing.
    """
    assert probe.replace_verdict({"a": 1}, {"a": 1}, []) == probe._UNKNOWN


def test_a_field_that_vanished_is_a_full_replace():
    before = {"vendorContactID": 7, "dateDue": "2026-10-01", "entryInstructions": "gate code 4821"}
    after = {"vendorContactID": None, "dateDue": None, "entryInstructions": None}
    watched = ["dateDue", "entryInstructions", "vendorContactID"]
    assert probe.replace_verdict(before, after, watched) == "FULL REPLACE"


def test_a_field_that_survived_is_a_partial_update():
    """The other direction — a one-sided test passes against a verdict function
    that answers FULL REPLACE for everything (gotchas 38/51)."""
    before = {"vendorContactID": 7, "dateDue": "2026-10-01", "entryInstructions": "gate code 4821"}
    after = dict(before)
    watched = ["dateDue", "entryInstructions", "vendorContactID"]
    assert probe.replace_verdict(before, after, watched) == "PARTIAL"


def test_a_field_that_was_already_blank_is_not_evidence_of_a_replace():
    """It cannot have been erased by us if it was never populated."""
    before = {"vendorContactID": 7, "dateDue": None}
    after = {"vendorContactID": 7, "dateDue": None}
    assert probe.replace_verdict(before, after, ["vendorContactID", "dateDue"]) == "PARTIAL"


# --------------------------------------------------------------------------
# 2. What the probe watches
# --------------------------------------------------------------------------


def test_the_status_field_is_never_a_witness():
    """We are about to CHANGE the status, so watching it would report a full
    replace on every run."""
    record = {
        probe.STATUS_FIELD: 3,
        "vendorContactID": 7,
        "dateDue": "2026-10-01",
        "entryInstructions": "x",
    }
    assert probe.STATUS_FIELD not in probe.witness_fields(record)


def test_blank_and_absent_fields_are_not_watched():
    record = {"vendorContactID": 7, "dateDue": "", "entryInstructions": None}
    assert probe.witness_fields(record) == ["vendorContactID"]


def test_a_field_the_endpoint_does_not_declare_is_not_watched():
    """Watching an unwritable field would report a full replace whenever the
    server simply declined to echo something we could never have sent."""
    record = {"vendorContactID": 7, "notAThingRentvineDeclares": "hello"}
    assert "notAThingRentvineDeclares" not in probe.witness_fields(record)


# --------------------------------------------------------------------------
# 3. The restore must not mail a real vendor
# --------------------------------------------------------------------------


def test_the_restore_body_never_carries_a_side_effect_trigger():
    from helixis.rentvine.work_orders import NEVER_ECHOED, merge_update

    captured = {
        "vendorContactID": 7,
        "dateDue": "2026-10-01",
        "entryInstructions": "gate code 4821",
        "sendVendorNotification": True,
        "sendReview": True,
    }
    body = merge_update(captured, {})
    # ⚠ Re-pointed 2026-10-03, not relaxed: this asserted the triggers were
    # ABSENT, and absent is not off. The spec defaults sendVendorNotification
    # to TRUE, so an omitted one mails the vendor too (gotcha 191). Each must be
    # sent, and sent false: an echoed True and an omission both fail here.
    assert {k: body.get(k) for k in NEVER_ECHOED} == dict.fromkeys(NEVER_ECHOED, False), (
        "a restore that echoes or omits these emails somebody else's contractor every run"
    )


def test_every_body_the_probe_posts_keeps_the_vendor_unmailed(monkeypatch, tmp_path):
    """The spec defaults ``sendVendorNotification`` to TRUE on this update, so a
    body that leaves it out mails the work order's vendor (gotcha 191). All three
    POSTs (the one-field probe, the move back, the restore) send both false."""
    monkeypatch.setenv("RENTVINE_ACCOUNT_CODE", "acme")
    monkeypatch.setenv("RENTVINE_API_KEY", "k")
    monkeypatch.setenv("RENTVINE_API_SECRET", "s")
    record = {
        "workOrderID": 1,
        "workOrderStatusID": 1,
        "vendorContactID": 7,
        "dateDue": "2026-10-01",
        "entryInstructions": "gate code 4821",
    }
    statuses = [
        {"name": "Open", "workOrderStatusID": 1},
        {"name": "Completed", "workOrderStatusID": 2},
    ]
    posted: list[dict] = []

    def fake(_creds, method, path, body=None, **_k):
        if method == "POST":
            posted.append(body)
            return {"status": 200, "body": "{}"}
        if path.startswith("/maintenance/work-order/statuses"):
            return {"status": 200, "body": json.dumps(statuses)}
        return {"status": 200, "body": json.dumps({"workOrder": record})}

    monkeypatch.setattr(probe, "_request", fake)
    out = tmp_path / "r.json"
    probe.main(["--work-order-id", "1", "--execute", "--i-accept-the-write", "--out", str(out)])
    assert len(posted) == 3, posted
    for body in posted:
        assert body["sendVendorNotification"] is False and body["sendReview"] is False, body


def test_the_probe_imports_the_rules_rather_than_restating_them():
    """A second copy of the writable-field set here is the drift gotcha 25 is
    about, and it drifts in the direction that mails a vendor."""
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "work_orders" in node.module:
            imported |= {alias.name for alias in node.names}
    for required in ("merge_update", "writable_fields", "NEVER_ECHOED", "STATUS_FIELD"):
        assert required in imported, f"{required} must come from work_orders, not be restated"


def test_the_restore_runs_even_when_a_probe_raises():
    """The writes are inside a try whose finally restores. Without that, an
    exception between the unsafe POST and the restore leaves a customer's work
    order erased with nothing putting it back."""
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main")
    tries = [n for n in ast.walk(main) if isinstance(n, ast.Try) and n.finalbody]
    assert tries, "the write sequence must sit in a try/finally that restores"
    restoring = [
        t
        for t in tries
        if any(
            isinstance(c, ast.Name) and c.id == "merge_update"
            for node in t.finalbody
            for c in ast.walk(node)
        )
    ]
    assert restoring, "the finally block must rebuild the body with merge_update"


# --------------------------------------------------------------------------
# 4. Envelope shape is recorded, not guessed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "shape"),
    [
        ({"workOrder": {"workOrderID": 1}}, "wrapped:workOrder"),
        # The live shape, and the spec's example (measured 2026-10-02).
        (
            {"workOrder": {"workOrderID": 1}, "schedulingStatusID": 2},
            "wrapped:workOrder beside schedulingStatusID",
        ),
        ({"workOrderID": 1, "vendorID": 2}, "bare"),
        ([], "unreadable"),
    ],
)
def test_the_single_record_envelope_is_reported(payload, shape):
    assert probe.envelope_shape(payload) == shape


def test_both_envelope_shapes_yield_the_same_record():
    wrapped = {"workOrder": {"workOrderID": 1, "vendorID": 2}}
    bare = {"workOrderID": 1, "vendorID": 2}
    assert probe.unwrap_record(wrapped) == probe.unwrap_record(bare)


def test_the_live_envelope_yields_the_record_and_its_witness_fields():
    """Measured 2026-10-02: the GET returns the record BESIDE a scalar sibling.
    Reading only one-key wrappers took the envelope for the record, saw no
    writable field and refused seed work order 9, which has twenty."""
    record = {
        "workOrderID": 9,
        "description": "OCC-SEED work order 7",
        "propertyID": "1",
        "priorityID": "1",
        "workOrderStatusID": "2",
    }
    live = {"workOrder": record, "schedulingStatusID": 1}
    assert probe.unwrap_record(live) == record
    assert len(probe.witness_fields(probe.unwrap_record(live) or {})) >= probe.MIN_WITNESS_FIELDS


def test_a_bare_record_with_a_nested_object_is_not_unwrapped():
    bare = {"workOrderID": 9, "vendor": {"contactID": 5}, "description": "x"}
    assert probe.unwrap_record(bare) == bare


# --------------------------------------------------------------------------
# 5. It cannot write by accident
# --------------------------------------------------------------------------


def test_execute_without_the_acknowledgement_sends_nothing(monkeypatch, capsys):
    monkeypatch.setenv("RENTVINE_ACCOUNT_CODE", "acme")
    monkeypatch.setenv("RENTVINE_API_KEY", "k")
    monkeypatch.setenv("RENTVINE_API_SECRET", "s")

    def _boom(*_a, **_k):  # pragma: no cover - the point is that it is not reached
        raise AssertionError("no request may be issued without the acknowledgement")

    monkeypatch.setattr(probe, "_request", _boom)
    assert probe.main(["--work-order-id", "1", "--execute"]) == 2


def test_no_work_order_id_is_exit_two_not_a_guess(monkeypatch):
    monkeypatch.setenv("RENTVINE_ACCOUNT_CODE", "acme")
    monkeypatch.setenv("RENTVINE_API_KEY", "k")
    monkeypatch.setenv("RENTVINE_API_SECRET", "s")
    monkeypatch.setattr(probe, "_request", lambda *a, **k: pytest.fail("nothing may be sent"))
    assert probe.main([]) == 2


def test_missing_credentials_is_exit_two(monkeypatch):
    for name in ("RENTVINE_ACCOUNT_CODE", "RENTVINE_API_KEY", "RENTVINE_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    assert probe.main(["--work-order-id", "1"]) == 2


def test_a_thin_work_order_is_refused_before_anything_is_written(monkeypatch, tmp_path):
    """The gate that stops a fabricated verdict, measured end to end."""
    monkeypatch.setenv("RENTVINE_ACCOUNT_CODE", "acme")
    monkeypatch.setenv("RENTVINE_API_KEY", "k")
    monkeypatch.setenv("RENTVINE_API_SECRET", "s")

    sent: list[str] = []

    def fake(_creds, method, path, body=None, **_k):
        sent.append(method)
        if path.startswith("/maintenance/work-order/statuses"):
            return {"status": 200, "body": json.dumps([{"name": "Open", "workOrderStatusID": 1}])}
        return {"status": 200, "body": json.dumps({"workOrder": {"workOrderID": 1}})}

    monkeypatch.setattr(probe, "_request", fake)
    out = tmp_path / "r.json"
    code = probe.main(
        ["--work-order-id", "1", "--execute", "--i-accept-the-write", "--out", str(out)]
    )
    assert code == 2
    assert "POST" not in sent, "a record too thin to answer must not be written to"
    assert json.loads(out.read_text())["replace_semantics"] == probe._UNKNOWN


def test_the_default_run_is_a_dry_run(monkeypatch, tmp_path):
    monkeypatch.setenv("RENTVINE_ACCOUNT_CODE", "acme")
    monkeypatch.setenv("RENTVINE_API_KEY", "k")
    monkeypatch.setenv("RENTVINE_API_SECRET", "s")

    sent: list[str] = []

    def fake(_creds, method, path, body=None, **_k):
        sent.append(method)
        if path.startswith("/maintenance/work-order/statuses"):
            return {
                "status": 200,
                "body": json.dumps(
                    [
                        {"name": "Open", "workOrderStatusID": 1},
                        {"name": "Completed", "workOrderStatusID": 2},
                    ]
                ),
            }
        return {
            "status": 200,
            "body": json.dumps(
                {
                    "workOrder": {
                        "workOrderID": 1,
                        "workOrderStatusID": 1,
                        "vendorContactID": 7,
                        "dateDue": "2026-10-01",
                        "entryInstructions": "gate code 4821",
                    }
                }
            ),
        }

    monkeypatch.setattr(probe, "_request", fake)
    out = tmp_path / "r.json"
    code = probe.main(["--work-order-id", "1", "--out", str(out)])
    assert code == 0
    assert "POST" not in sent, "a run without --execute must not write"
    assert out.exists(), "even a dry run records what it would have done"


# --------------------------------------------------------------------------
# 6. The answer survives the terminal, and cannot clobber another harness
# --------------------------------------------------------------------------


def test_the_report_is_written_not_only_printed(tmp_path):
    """Gotcha 146: the webhook capture derived the signature scheme and left it
    in scrollback, so the operator had to hand-transcribe it."""
    path = probe._write_report(str(tmp_path / "out.json"), {"replace_semantics": "PARTIAL"})
    assert json.loads(pathlib.Path(path).read_text())["replace_semantics"] == "PARTIAL"


#: The sibling harnesses whose output this one must never land on.
_SIBLINGS = ("rentvine_probe.py", "rentvine_phase8.py", "rentvine_webhook_capture.py")


def _report_paths(script: str) -> set[str]:
    """Every ``docs/rentvine/*.json`` literal in a sibling script.

    ⚠ EVERY quoted string on the line, not ``split('"')[1]``. The first cut
    took index 1 and `rentvine_probe.py` declares its default INLINE —
    ``ap.add_argument("--out", default="docs/rentvine/probe-report.json")`` —
    so index 1 is ``--out``, the path sat at index 3, and the one sibling this
    harness could actually collide with was the one the scan could not see.
    The other two use ``DEFAULT_OUT = "..."``, where index 1 IS the path, so
    the scan found something and the non-empty canary passed (CLAUDE.md
    gotchas 49/100/101/114/128: a scan whose result is INCOMPLETE reads
    exactly like one that is right).
    """
    text = (_ROOT / "scripts" / script).read_text(encoding="utf-8")
    return set(re.findall(r'"(docs/rentvine/[^"]+\.json)"', text))


@pytest.mark.parametrize("sibling", _SIBLINGS)
def test_the_report_path_is_not_another_harnesss_only_copy(sibling):
    """Read out of the sibling rather than typed here, so a rename over there
    fails this rather than silently colliding.

    ⚠ PER SIBLING, not a pooled set — a total-count canary lets one script
    contribute zero while another contributes two, which is the granularity
    bound gotchas 118/122 record.
    """
    paths = _report_paths(sibling)
    assert paths, (
        f"found no report path in {sibling} — the scan is broken, not the code. "
        f"Re-point it at however that script now names its output."
    )
    assert probe.DEFAULT_OUT not in paths, (
        f"{probe.DEFAULT_OUT} is also written by {sibling}; one run would "
        f"destroy the other's only copy of a live capture"
    )


def test_the_secret_never_reaches_the_report():
    creds = probe.Creds.__new__(probe.Creds)
    creds.account, creds.key, creds.secret = "acme", "KEY123", "SECRET456"
    assert "SECRET456" not in probe._redact("token=SECRET456 key=KEY123", creds)
    assert "KEY123" not in probe._redact("token=SECRET456 key=KEY123", creds)


# --------------------------------------------------------------------------
# 7. The marker this closes
# --------------------------------------------------------------------------


def test_the_two_write_markers_name_an_instrument_that_exists():
    """The finding itself: both markers said "Settled by a Phase 8 probe" while
    every probe in the repo was read-only. If a marker stops naming this file,
    Phase 8's work list has gone back to naming an event no command can cause."""
    text = (_ROOT / "src" / "helixis" / "rentvine" / "work_orders.py").read_text(encoding="utf-8")
    assert "rentvine_write_probe" in text, (
        "work_orders.py's two @unverified markers must name the harness that "
        "settles them — a work-list item with no instrument is gotcha 151"
    )


# --------------------------------------------------------------------------
# 8. The reversibility probe uses the status the marker asks about
# --------------------------------------------------------------------------


def _dry_run(monkeypatch, tmp_path, statuses):
    """Drive the real main() in DRY mode and hand back the written report."""
    monkeypatch.setenv("RENTVINE_ACCOUNT_CODE", "acme")
    monkeypatch.setenv("RENTVINE_API_KEY", "k")
    monkeypatch.setenv("RENTVINE_API_SECRET", "s")

    def fake(_creds, method, path, body=None, **_k):
        if path.startswith("/maintenance/work-order/statuses"):
            return {"status": 200, "body": json.dumps(statuses)}
        return {
            "status": 200,
            "body": json.dumps(
                {
                    "workOrder": {
                        "workOrderID": 1,
                        "workOrderStatusID": 1,
                        "vendorContactID": 7,
                        "dateDue": "2026-10-01",
                        "entryInstructions": "gate code 4821",
                    }
                }
            ),
        }

    monkeypatch.setattr(probe, "_request", fake)
    out = tmp_path / "r.json"
    assert probe.main(["--work-order-id", "1", "--out", str(out)]) == 0
    return json.loads(out.read_text())


def test_completed_is_preferred_when_the_account_has_it(monkeypatch, tmp_path):
    """Buildium's equivalent latches one-way (gotcha 20), so "can it move back"
    is only sharp from a terminal status."""
    report = _dry_run(
        monkeypatch,
        tmp_path,
        [
            {"name": "Open", "workOrderStatusID": 1},
            {"name": "Assigned", "workOrderStatusID": 2},
            {"name": "Completed", "workOrderStatusID": 3},
        ],
    )
    # LOWERCASE because `status_choices` is documented as returning
    # "{lowercased status name: id}" — the harness reports in the same
    # vocabulary the product resolves in, rather than inventing a second.
    assert report["reversibility_status_used"] == "completed"


def test_an_account_without_completed_still_answers_and_names_what_it_used(monkeypatch, tmp_path):
    """The vocabulary is per ACCOUNT, so refusing would strand a customer whose
    list is spelled differently — but the verdict must name the status it
    actually exercised (gotchas 55/57: say what was measured)."""
    report = _dry_run(
        monkeypatch,
        tmp_path,
        [
            {"name": "Open", "workOrderStatusID": 1},
            {"name": "Finished", "workOrderStatusID": 2},
        ],
    )
    assert report["reversibility_status_used"] == "finished"
