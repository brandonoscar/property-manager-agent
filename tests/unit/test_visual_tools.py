"""Visual-answer tools — payload shape, guardrails, and the emit queue.

``build_table_payload`` is a pure function (tested directly); ``show_table``
pushes onto the module queue that the orchestrator drains, so we assert the
queue round-trip via ``drain_pending_visuals``.
"""

from __future__ import annotations

from helixis.agents import visual_tools
from helixis.agents.visual_tools import build_table_payload, drain_pending_visuals


def test_build_table_payload_shape_matches_frontend_contract():
    payload = build_table_payload(
        "Behind on rent",
        ["Tenant", "Unit", "Balance"],
        [["Sarah Chen", "4B", "$1,450"], ["Marcus Webb", "7", "$900"]],
    )
    assert payload["kind"] == "agent_table"
    assert payload["title"] == "Behind on rent"
    # columns become keyed left-to-right
    assert payload["columns"] == [
        {"key": "c0", "label": "Tenant"},
        {"key": "c1", "label": "Unit"},
        {"key": "c2", "label": "Balance"},
    ]
    # each cell is a typed 'text' cell keyed by column
    assert payload["rows"][0] == {
        "c0": {"kind": "text", "value": "Sarah Chen"},
        "c1": {"kind": "text", "value": "4B"},
        "c2": {"kind": "text", "value": "$1,450"},
    }
    assert len(payload["rows"]) == 2


def test_build_table_payload_ragged_and_empty():
    # a short row keeps only the cells it has (frontend em-dashes the rest);
    # an empty title normalizes to None.
    payload = build_table_payload("", ["A", "B", "C"], [["only-one"]])
    assert payload["title"] is None
    assert payload["rows"][0] == {"c0": {"kind": "text", "value": "only-one"}}


def test_build_table_payload_caps_size():
    big = build_table_payload(
        "big",
        [f"col{i}" for i in range(50)],  # > _MAX_COLUMNS
        [[str(i) for i in range(50)] for _ in range(500)],  # > _MAX_ROWS
    )
    assert len(big["columns"]) == visual_tools._MAX_COLUMNS
    assert len(big["rows"]) == visual_tools._MAX_ROWS
    # cells never exceed the (capped) column count
    assert all(len(r) <= visual_tools._MAX_COLUMNS for r in big["rows"])


async def test_show_table_enqueues_and_drains():
    # drain any residue from other tests first
    await drain_pending_visuals()
    msg = await visual_tools._show_table_impl(
        "Leases expiring", ["Unit", "Ends"], [["4B", "Aug 12"]]
    )
    assert "Displayed a table" in msg
    drained = await drain_pending_visuals()
    assert len(drained) == 1
    assert drained[0]["title"] == "Leases expiring"
    # queue is empty after draining
    assert await drain_pending_visuals() == []


async def test_show_table_rejects_no_columns():
    await drain_pending_visuals()
    msg = await visual_tools._show_table_impl("x", [], [["a"]])
    assert "needs at least one column" in msg
    assert await drain_pending_visuals() == []  # nothing enqueued


# ── map emission (maps #539) — queue round-trip + payload shape ──


def test_build_map_payload_shape_and_key_free_url():
    from helixis.agents.visual_tools import build_map_payload

    payload = build_map_payload([(40.71, -74.0), (40.72, -73.9)], caption="Near you")
    assert payload is not None
    assert payload["caption"] == "Near you"
    assert payload["markers"] == [
        {"lat": 40.71, "lng": -74.0},
        {"lat": 40.72, "lng": -73.9},
    ]
    # url is our key-free proxy path
    assert payload["url"].startswith("/api/v1/maps/static?")
    assert "key" not in payload["url"]


def test_build_map_payload_none_without_markers():
    from helixis.agents.visual_tools import build_map_payload

    assert build_map_payload([]) is None


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_queue_map_round_trips_through_drain():
    from helixis.agents.visual_tools import drain_pending_maps, queue_map

    await drain_pending_maps()  # clear any residue from other tests
    assert queue_map([(30.26, -97.74)], caption="Austin") is True
    drained = await drain_pending_maps()
    assert len(drained) == 1
    assert drained[0]["caption"] == "Austin"
    # drained once, the queue is empty again
    assert await drain_pending_maps() == []


@pytest.mark.asyncio
async def test_queue_map_no_markers_is_noop_false():
    from helixis.agents.visual_tools import drain_pending_maps, queue_map

    await drain_pending_maps()
    assert queue_map([]) is False
    assert await drain_pending_maps() == []


@pytest.mark.asyncio
async def test_queue_artifacts_round_trips_and_filters_urlless():
    from helixis.agents.visual_tools import drain_pending_artifacts, queue_artifacts

    await drain_pending_artifacts()  # clear residue
    # A mix: one real link, one that failed to sign (url=None) — only the
    # signed one is surfaced.
    ok = queue_artifacts(
        [
            {"name": "report.csv", "size": 42, "url": "https://signed/report.csv?t=abc"},
            {"name": "skipped.bin", "size": 9, "url": None},
        ]
    )
    assert ok is True
    drained = await drain_pending_artifacts()
    assert len(drained) == 1
    arts = drained[0]["artifacts"]
    assert [a["name"] for a in arts] == ["report.csv"]
    assert arts[0]["url"].startswith("https://signed/")
    assert await drain_pending_artifacts() == []


@pytest.mark.asyncio
async def test_queue_artifacts_all_urlless_is_noop_false():
    from helixis.agents.visual_tools import drain_pending_artifacts, queue_artifacts

    await drain_pending_artifacts()
    assert queue_artifacts([{"name": "x.bin", "size": 1, "url": None}]) is False
    assert queue_artifacts([]) is False
    assert await drain_pending_artifacts() == []


# ── email-draft emission (UI/UX day, 2026-08-19) ─────────────────────
#
# ``show_email_draft`` renders the model's OWN composed draft as an email
# card. These pin: the payload passes the draft through verbatim (framing,
# never authoring), the defensive caps, the queue round-trip, and that an
# empty draft enqueues nothing (the model must compose first).

from helixis.agents.visual_tools import (  # noqa: E402
    build_email_draft_payload,
    drain_pending_email_drafts,
)


def test_build_email_draft_payload_passes_the_draft_through_verbatim():
    body = "Hi Sarah,\n\nRent stays at $1,850/mo.\n\nBest,\nDana"
    p = build_email_draft_payload(
        ["Sarah Chen <sarah@x.com>"],
        [{"label": "Standard", "subject": "Your lease renewal", "body": body}],
        cc=["owner@pm.com"],
    )
    assert p == {
        "to": ["Sarah Chen <sarah@x.com>"],
        "cc": ["owner@pm.com"],
        "variants": [{"label": "Standard", "subject": "Your lease renewal", "body": body}],
    }


def test_build_email_draft_payload_caps_variants_and_drops_empty_bodies():
    variants = [{"label": f"v{i}", "subject": f"s{i}", "body": f"b{i}"} for i in range(6)]
    variants.insert(0, {"label": "empty", "subject": "s", "body": "   "})
    p = build_email_draft_payload(["a@x.com"], variants)
    # Empty-body variant dropped; capped at 4.
    assert [v["label"] for v in p["variants"]] == ["v0", "v1", "v2", "v3"]


def test_build_email_draft_payload_caps_and_cleans():
    p = build_email_draft_payload(
        ["  a@x.com  ", "", "   "] + [f"r{i}@x.com" for i in range(40)],
        [{"label": "L" * 200, "subject": "s" * 1000, "body": "b" * 20_000}],
        cc=None,
    )
    assert p["to"][0] == "a@x.com"
    assert len(p["to"]) == 20  # recipient cap
    v = p["variants"][0]
    assert len(v["label"]) == 60
    assert len(v["subject"]) == 300
    assert len(v["body"]) == 8_000
    assert p["cc"] == []


async def test_show_email_draft_enqueues_and_drains():
    await drain_pending_email_drafts()
    msg = await visual_tools._show_email_draft_impl(
        ["sarah@x.com"],
        ["Friendly", "Firm"],
        ["Renewal", "Renewal — action needed"],
        ["Hi Sarah — friendly draft.", "Hi Sarah — firm draft."],
        None,
    )
    assert "2 draft options shown" in msg
    # The tool tells the model NOT to duplicate the drafts as prose.
    assert "Do NOT repeat" in msg
    drained = await drain_pending_email_drafts()
    assert len(drained) == 1
    assert [v["label"] for v in drained[0]["variants"]] == ["Friendly", "Firm"]
    assert await drain_pending_email_drafts() == []


async def test_show_email_draft_rejects_an_empty_draft():
    await drain_pending_email_drafts()
    msg = await visual_tools._show_email_draft_impl([], [], [], ["   "], None)
    assert "compose the draft" in msg
    assert await drain_pending_email_drafts() == []


# ── wave-2 cards: detail / chart / schedule / checklist ──────────────

from helixis.agents.visual_tools import (  # noqa: E402
    build_chart_payload,
    build_checklist_payload,
    build_detail_card_payload,
    build_schedule_payload,
    drain_pending_cards,
)


def test_build_detail_card_payload_shapes_and_guards():
    p = build_detail_card_payload(
        "AC not cooling — unit 4B",
        "128 Lexington Ave",
        "In progress",
        "caution",
        [["Vendor", "Northside Plumbing"], ["Priority", "High"], ["Empty", "   "]],
        "3rd similar issue in 90 days.",
    )
    assert p["title"] == "AC not cooling — unit 4B"
    assert p["tone"] == "caution"
    # Empty-value fields are dropped.
    assert [f["label"] for f in p["fields"]] == ["Vendor", "Priority"]
    # Unknown tone collapses to neutral.
    assert build_detail_card_payload("t", "", "", "sparkly", [["a", "b"]], "")["tone"] == "neutral"


def test_build_chart_payload_parses_currency_and_drops_junk():
    p = build_chart_payload(
        "Rent collected",
        "line",
        [["Mar", "$11,400"], ["Apr", "11850"], ["May", "n/a"], ["Jun", "12000"]],
        "currency",
    )
    assert p["kind"] == "line" and p["unit"] == "currency"
    assert [pt["value"] for pt in p["points"]] == [11400.0, 11850.0, 12000.0]
    # Unknown kind/unit collapse to defaults.
    q = build_chart_payload("x", "pie", [["a", "1"], ["b", "2"]], "furlongs")
    assert q["kind"] == "bar" and q["unit"] == "count"


def test_build_schedule_payload_states():
    p = build_schedule_payload(
        "Showings",
        "Thu Aug 21",
        [
            ["9:00", "Alicia"],
            ["10:00", "Jordan", "confirmed"],
            ["11:00", "Blocked", "busy"],
            ["12:00", "X", "weird"],
        ],
    )
    assert [s["state"] for s in p["slots"]] == ["proposed", "confirmed", "busy", "proposed"]


def test_build_checklist_payload_done_flags():
    p = build_checklist_payload(
        "Turnover",
        [["Inspection", "done"], ["Paint"], ["List unit", "false"]],
    )
    assert [(i["text"], i["done"]) for i in p["items"]] == [
        ("Inspection", True),
        ("Paint", False),
        ("List unit", False),
    ]


async def test_wave_two_tools_share_one_queue_with_typed_events():
    await drain_pending_cards()
    # The function_tool wrappers are exercised by the SDK; here we pin the
    # shared-queue contract the orchestrator drains.
    assert visual_tools.show_detail_card.name == "show_detail_card"
    visual_tools._queue_card("chart", {"title": "t"})
    visual_tools._queue_card("checklist", {"title": "u"})
    drained = await drain_pending_cards()
    assert [(t, p["title"]) for t, p in drained] == [("chart", "t"), ("checklist", "u")]
    assert await drain_pending_cards() == []
