"""The attention-budget engine — the volume governor over detector findings.

Pure, no-I/O engine, so every rule from the ambient-detector spec is pinned
here: hard daily cap + overflow, critical→urgent (uncapped), cross-detector
dedup by entity, priority sort order, and per-detector dismissal calibration.
"""

from __future__ import annotations

from helixis.detectors.attention import (
    DEFAULT_DAILY_CAP,
    NoticedFinding,
    detector_calibration,
    select_noticed,
)


def _f(
    fid: str,
    severity: str = "warning",
    *,
    amount: float | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    last_seen: str | None = None,
    rule_key: str = "rk",
) -> NoticedFinding:
    return NoticedFinding(
        id=fid,
        rule_key=rule_key,
        severity=severity,
        title=f"finding {fid}",
        detail="",
        amount=amount,
        entity_type=entity_type,
        entity_id=entity_id,
        last_seen=last_seen,
    )


# ── empty / trivial ──────────────────────────────────────────────────


def test_empty_digest() -> None:
    d = select_noticed([])
    assert d.urgent == []
    assert d.noticed == []
    assert d.overflow_count == 0
    assert d.delivered_count == 0


def test_default_cap_constant() -> None:
    assert DEFAULT_DAILY_CAP == 8


# ── rule 2: critical is urgent and uncapped ──────────────────────────


def test_critical_is_urgent_and_uncapped() -> None:
    # Ten critical findings, cap of 2 — every one is still delivered as urgent.
    findings = [_f(str(i), "critical", entity_id=i) for i in range(10)]
    d = select_noticed(findings, daily_cap=2)
    assert len(d.urgent) == 10
    assert d.noticed == []
    assert d.overflow_count == 0
    assert d.delivered_count == 10


def test_urgent_and_capped_coexist() -> None:
    findings = [_f("c", "critical", entity_id=1)] + [
        _f(str(i), "warning", entity_id=100 + i) for i in range(5)
    ]
    d = select_noticed(findings, daily_cap=2)
    assert len(d.urgent) == 1
    assert len(d.noticed) == 2
    assert d.overflow_count == 3


# ── rule 1: hard daily cap + overflow ────────────────────────────────


def test_daily_cap_and_overflow() -> None:
    findings = [_f(str(i), "warning", entity_id=i) for i in range(12)]
    d = select_noticed(findings, daily_cap=8)
    assert len(d.noticed) == 8
    assert d.overflow_count == 4
    assert d.delivered_count == 8


def test_cap_zero_is_all_overflow() -> None:
    findings = [_f(str(i), "warning", entity_id=i) for i in range(3)]
    d = select_noticed(findings, daily_cap=0)
    assert d.noticed == []
    assert d.overflow_count == 3


def test_negative_cap_clamped_to_zero() -> None:
    findings = [_f(str(i), "info", entity_id=i) for i in range(3)]
    d = select_noticed(findings, daily_cap=-5)
    assert d.noticed == []
    assert d.overflow_count == 3


def test_under_cap_no_overflow() -> None:
    findings = [_f(str(i), "info", entity_id=i) for i in range(3)]
    d = select_noticed(findings, daily_cap=8)
    assert len(d.noticed) == 3
    assert d.overflow_count == 0


# ── rule 4: cross-detector dedup by entity ───────────────────────────


def test_same_entity_collapses_to_one_item() -> None:
    # A lease that's both under-charged (warning) and 90-days-late (critical):
    # one item on that entity, not two lines.
    findings = [
        _f("a", "warning", entity_type="lease", entity_id=4001, rule_key="rent_under_charged"),
        _f("b", "critical", entity_type="lease", entity_id=4001, rule_key="delinquent_90"),
    ]
    d = select_noticed(findings)
    # Critical wins the entity → it's the urgent item; the warning rides as related.
    assert len(d.urgent) == 1
    assert d.noticed == []
    item = d.urgent[0]
    assert item.finding.id == "b"  # highest-priority (critical) wins
    assert [r.id for r in item.related] == ["a"]
    assert d.delivered_count == 1


def test_different_entities_do_not_merge() -> None:
    findings = [
        _f("a", "warning", entity_type="lease", entity_id=1),
        _f("b", "warning", entity_type="lease", entity_id=2),
        _f("c", "warning", entity_type="unit", entity_id=1),  # same id, different type
    ]
    d = select_noticed(findings)
    assert len(d.noticed) == 3
    assert all(not it.related for it in d.noticed)


def test_entityless_findings_never_merge() -> None:
    # entity_id is None (e.g. a promise keyed by email) → each stays its own item.
    findings = [
        _f("a", "warning", entity_type="promise", entity_id=None),
        _f("b", "warning", entity_type="promise", entity_id=None),
    ]
    d = select_noticed(findings)
    assert len(d.noticed) == 2
    assert all(not it.related for it in d.noticed)


def test_dedup_counts_before_cap() -> None:
    # Five findings but all on the same entity → one delivered item, no overflow.
    findings = [_f(str(i), "warning", entity_type="lease", entity_id=99) for i in range(5)]
    d = select_noticed(findings, daily_cap=2)
    assert len(d.noticed) == 1
    assert d.overflow_count == 0
    assert len(d.noticed[0].related) == 4


# ── priority ordering ────────────────────────────────────────────────


def test_severity_orders_before_amount() -> None:
    findings = [
        _f("small_crit", "critical", amount=10.0, entity_id=1),
        _f("big_warn", "warning", amount=9999.0, entity_id=2),
    ]
    d = select_noticed(findings)
    # critical is urgent (own tier); warning is in noticed.
    assert d.urgent[0].finding.id == "small_crit"
    assert d.noticed[0].finding.id == "big_warn"


def test_larger_amount_first_within_severity() -> None:
    findings = [
        _f("small", "warning", amount=100.0, entity_id=1),
        _f("big", "warning", amount=5000.0, entity_id=2),
        _f("mid", "warning", amount=1000.0, entity_id=3),
    ]
    d = select_noticed(findings)
    assert [it.finding.id for it in d.noticed] == ["big", "mid", "small"]


def test_recent_last_seen_first_as_tiebreaker() -> None:
    findings = [
        _f("old", "warning", entity_id=1, last_seen="2026-08-01T00:00:00"),
        _f("new", "warning", entity_id=2, last_seen="2026-08-05T00:00:00"),
    ]
    d = select_noticed(findings)
    assert [it.finding.id for it in d.noticed] == ["new", "old"]


def test_winner_within_entity_group_is_highest_priority() -> None:
    # Same entity, same severity — the larger amount wins the group.
    findings = [
        _f("cheap", "warning", amount=50.0, entity_type="bill", entity_id=7),
        _f("pricey", "warning", amount=800.0, entity_type="bill", entity_id=7),
    ]
    d = select_noticed(findings)
    assert len(d.noticed) == 1
    assert d.noticed[0].finding.id == "pricey"
    assert d.noticed[0].related[0].id == "cheap"


# ── rule 5: per-detector dismissal calibration ───────────────────────


def test_calibration_flags_noisy_detector() -> None:
    counts = {"noisy": (6, 10), "good": (1, 10)}
    reports = detector_calibration(counts)
    by_key = {r.rule_key: r for r in reports}
    assert by_key["noisy"].miscalibrated is True
    assert by_key["noisy"].dismissal_rate == 0.6
    assert by_key["good"].miscalibrated is False


def test_calibration_respects_min_samples() -> None:
    # 100% dismissed but only 3 samples — not enough to call it miscalibrated.
    counts = {"sparse": (3, 3)}
    reports = detector_calibration(counts, min_samples=5)
    assert reports[0].miscalibrated is False
    assert reports[0].dismissal_rate == 1.0


def test_calibration_boundary_not_flagged_at_exactly_half() -> None:
    # Strictly greater-than, so exactly 50% is not miscalibrated.
    counts = {"half": (5, 10)}
    reports = detector_calibration(counts)
    assert reports[0].dismissal_rate == 0.5
    assert reports[0].miscalibrated is False


def test_calibration_sorted_worst_first() -> None:
    counts = {"a": (2, 10), "b": (9, 10), "c": (5, 10)}
    reports = detector_calibration(counts)
    assert [r.rule_key for r in reports] == ["b", "c", "a"]


def test_calibration_zero_total_is_safe() -> None:
    counts = {"never_fired": (0, 0)}
    reports = detector_calibration(counts)
    assert reports[0].dismissal_rate == 0.0
    assert reports[0].miscalibrated is False
