"""Jurisdiction intelligence — which landlord-tenant law governs a property.

Three layers, resolved once per property and read instantly at question time:

* :func:`resolve_stack` — point -> the legal jurisdiction stack (state /
  county / incorporated place) via ONE call to the Census geographies
  endpoint (:data:`~helixis.geo.intel.CENSUS_GEO_URL` — the same keyless
  service the intel clients use). The legal place comes from Census
  incorporated-place BOUNDARIES, never the postal city; a point outside any
  incorporated place resolves ``place_fips=None``, which is an answer
  (unincorporated — county/state law governs), not an error.
* :func:`load_rules` — the bundled, versioned rules snapshot
  (``data/jurisdiction_rules.json``, the source of truth). Cached; a missing
  file degrades to ``{}`` with a warning so the tool stays honest before the
  data lands.
* :func:`lookup` + :func:`format_compact` — merge federal + state + overlay
  layers with an ``overrides`` dict on top (the ``jurisdiction_notes``
  config-table layer, migration 0067 — a config row WINS over the JSON and
  is marked ``source="config"``), then render a compact snapshot with the
  not-legal-advice footer. A parameter the snapshot doesn't encode renders
  as "apply the most tenant-protective assumption and verify" — the
  conservative default, never a guess.

Same iron discipline as :mod:`helixis.geo.geocode`: :func:`resolve_stack`
never raises — catch ``Exception``, log, return ``None``. Jurisdiction data
is an enhancement, never load-bearing; an unresolved property just answers
honestly until the next backfill pass.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from helixis.geo import deadline_math
from helixis.geo.intel import CENSUS_GEO_URL
from helixis.settings import Settings

logger = logging.getLogger(__name__)

_RULES_PATH = Path(__file__).resolve().parent / "data" / "jurisdiction_rules.json"

# State FIPS -> USPS abbreviation (50 states + DC + the territories). The
# backfill persists FIPS codes (stable Census identifiers); the rules JSON is
# keyed by abbreviation, so the tool maps through this table.
STATE_FIPS_TO_ABBR: dict[str, str] = {
    "01": "AL",
    "02": "AK",
    "04": "AZ",
    "05": "AR",
    "06": "CA",
    "08": "CO",
    "09": "CT",
    "10": "DE",
    "11": "DC",
    "12": "FL",
    "13": "GA",
    "15": "HI",
    "16": "ID",
    "17": "IL",
    "18": "IN",
    "19": "IA",
    "20": "KS",
    "21": "KY",
    "22": "LA",
    "23": "ME",
    "24": "MD",
    "25": "MA",
    "26": "MI",
    "27": "MN",
    "28": "MS",
    "29": "MO",
    "30": "MT",
    "31": "NE",
    "32": "NV",
    "33": "NH",
    "34": "NJ",
    "35": "NM",
    "36": "NY",
    "37": "NC",
    "38": "ND",
    "39": "OH",
    "40": "OK",
    "41": "OR",
    "42": "PA",
    "44": "RI",
    "45": "SC",
    "46": "SD",
    "47": "TN",
    "48": "TX",
    "49": "UT",
    "50": "VT",
    "51": "VA",
    "53": "WA",
    "54": "WV",
    "55": "WI",
    "56": "WY",
    "60": "AS",
    "66": "GU",
    "69": "MP",
    "72": "PR",
    "78": "VI",
}

# The conservative default for anything the snapshot doesn't encode.
NOT_ENCODED = "not encoded — apply the most tenant-protective assumption and verify"

_FOOTER = (
    "snapshot last verified {version}; verify the controlling statute before "
    "executing deposit dispositions, termination notices, rent increases, or "
    "screening denials — not legal advice."
)


@dataclass(frozen=True)
class JurisdictionStack:
    """The resolved legal-jurisdiction stack for one point."""

    state_fips: str
    state_abbr: str | None
    county_fips: str | None
    county_name: str | None
    # None = unincorporated territory (an answer, not an error).
    place_fips: str | None
    place_name: str | None


@dataclass
class JurisdictionAnswer:
    """One merged jurisdiction answer, ready for :func:`format_compact`.

    ``state`` is a deep copy of the state block with any ``overrides``
    applied on top; ``sources`` records which dotted param paths came from
    the config layer (``"config"``) rather than the bundled JSON.
    """

    state_abbr: str
    state_name: str | None
    version: str | None
    #: The county and incorporated place the point actually resolved to.
    #:
    #: ⚠ RENDERED, and that is the whole reason they are here. The snapshot used
    #: to name only the STATE while the deposit cap, notice periods and late-fee
    #: rules came from the state block PLUS whichever county/place overlay
    #: matched — so a property geocoded a block across a city line produced a
    #: confidently wrong legal answer that read exactly like a right one, with
    #: nothing on screen a manager could catch it by (coverage item 196). They
    #: know their own city; they cannot see a FIPS code.
    #:
    #: ``None`` is not "no county" — it is "not resolved yet", which is every
    #: row written before migration 0099. The formatter says nothing rather
    #: than inventing a name, because a header reading "None County" is a fresh
    #: false statement where silence is merely a missing one.
    county_name: str | None = None
    place_name: str | None = None
    federal: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    overlay_market: bool = False
    overlay_note: str | None = None
    overlay_places: list[dict[str, Any]] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    # Dated-rule-change flags from resolving each group's ``pending_change``
    # against today (deadline_math.apply_pending_changes) — rendered as ⚠
    # lines so the transition is always surfaced, applied or upcoming.
    pending_flags: list[str] = field(default_factory=list)


async def resolve_stack(lat: float, lng: float, *, settings: Settings) -> JurisdictionStack | None:
    """Resolve the point's jurisdiction stack via ONE Census geographies
    call, or ``None`` on any failure (never raises — geocode.py discipline).

    Census speaks x/y: ``x`` is LONGITUDE and ``y`` is LATITUDE (the classic
    transposition trap — same as geo/geocode.py). An absent/empty
    "Incorporated Places" list is a real answer: ``place_fips=None`` means
    unincorporated, and the caller persists it as resolved.
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=settings.maps.request_timeout_seconds) as client:
            resp = await client.get(
                CENSUS_GEO_URL,
                params={
                    "x": lng,
                    "y": lat,
                    "benchmark": "Public_AR_Current",
                    "vintage": "Current_Current",
                    "format": "json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        geographies = ((data.get("result") or {}).get("geographies")) or {}
        states = geographies.get("States") or []
        if not states:
            return None  # outside the US / service hiccup — retry next pass
        state = states[0]
        state_fips = str(state.get("GEOID") or state.get("STATE") or "").strip()
        if not state_fips:
            return None
        abbr = str(state.get("STUSAB") or "").strip().upper() or STATE_FIPS_TO_ABBR.get(state_fips)
        counties = geographies.get("Counties") or []
        county = counties[0] if counties else {}
        places = geographies.get("Incorporated Places") or []
        place = places[0] if places else None
        return JurisdictionStack(
            state_fips=state_fips,
            state_abbr=abbr,
            county_fips=str(county.get("GEOID") or "").strip() or None,
            county_name=str(county.get("NAME") or "").strip() or None,
            place_fips=(str(place.get("GEOID") or "").strip() or None) if place else None,
            place_name=(str(place.get("NAME") or "").strip() or None) if place else None,
        )
    except Exception:  # noqa: BLE001 — best-effort; degrade, never propagate
        logger.warning("resolve_stack failed for (%s,%s)", lat, lng, exc_info=True)
        return None


@lru_cache(maxsize=1)
def load_rules() -> dict[str, Any]:
    """The bundled jurisdiction rules snapshot, cached for the process.

    A missing or corrupt file degrades to ``{}`` with a warning — the lookup
    then renders every parameter as the conservative default instead of
    failing, so the tool stays honest before the data file lands.
    """
    try:
        with open(_RULES_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning("jurisdiction rules at %s are not a JSON object", _RULES_PATH)
            return {}
        return data
    except FileNotFoundError:
        logger.warning(
            "jurisdiction rules file missing at %s — lookups degrade to the "
            "conservative default until it lands",
            _RULES_PATH,
        )
        return {}
    except Exception:  # noqa: BLE001 — corrupt data file → empty, degrade honestly
        logger.warning("jurisdiction rules failed to load from %s", _RULES_PATH, exc_info=True)
        return {}


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    """Set a dotted param path ('deposit.cap_months') into a nested dict."""
    parts = [p for p in path.split(".") if p]
    if not parts:
        return
    for part in parts[:-1]:
        nxt = target.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            target[part] = nxt
        target = nxt
    target[parts[-1]] = value


def _matches_place(
    entry: dict[str, Any], place_fips: str | None, county_fips: str | None, place_name: str | None
) -> bool:
    """An overlay place entry matches on its FIPS keys when it carries them,
    or on a case-insensitive name match when a name is available."""
    entry_fips = str(entry.get("place_fips") or entry.get("fips") or "").strip()
    if entry_fips and entry_fips in {place_fips, county_fips}:
        return True
    entry_name = str(entry.get("name") or "").strip().lower()
    return bool(entry_name and place_name and entry_name == place_name.strip().lower())


def lookup(
    state_abbr: str,
    *,
    county_fips: str | None = None,
    place_fips: str | None = None,
    place_name: str | None = None,
    county_name: str | None = None,
    overrides: dict[str, Any] | None = None,
    today: date | None = None,
) -> JurisdictionAnswer:
    """Merge the layers for one jurisdiction into a :class:`JurisdictionAnswer`.

    Precedence: bundled JSON (federal + state + overlay) < ``overrides`` (the
    future ``jurisdiction_notes`` config-table layer) — any dotted param path
    present in ``overrides`` replaces the JSON value and is marked
    ``source="config"``. Overrides support dotted paths of any depth into the
    nested structures ("deposit.cap_months", "rates.ab1482_max_increase.value").
    Values may be numbers, strings ("demand", "none set"), or ``None`` — all
    are carried through untouched.

    After the merge, any group carrying a dated ``pending_change`` is resolved
    against ``today`` (default: the real today) so the answer always reflects
    the value in force NOW; the transition is flagged either way
    (``pending_flags``). A field the config layer overrode keeps its config
    value — config remains the topmost layer.
    """
    state_abbr = (state_abbr or "").strip().upper()
    rules = load_rules()
    state_block = (rules.get("states") or {}).get(state_abbr) or {}
    state = copy.deepcopy(state_block) if isinstance(state_block, dict) else {}
    answer = JurisdictionAnswer(
        state_abbr=state_abbr,
        state_name=state.get("name"),
        version=rules.get("version"),
        county_name=(county_name or "").strip() or None,
        place_name=(place_name or "").strip() or None,
        federal=copy.deepcopy(rules.get("federal") or {}),
        state=state,
        overlay_market=bool(state.get("overlay_market")),
        overlay_note=state.get("overlay_note"),
    )
    overlay = (rules.get("overlay_jurisdictions") or {}).get(state_abbr) or {}
    for entry in overlay.get("places") or []:
        if isinstance(entry, dict) and _matches_place(entry, place_fips, county_fips, place_name):
            answer.overlay_places.append(entry)
    for path, value in (overrides or {}).items():
        _set_path(answer.state, path, value)
        answer.sources[path] = "config"
    _resolve_pending_changes(answer, today=today or date.today())
    return answer


def _resolve_pending_changes(answer: JurisdictionAnswer, *, today: date) -> None:
    """Apply each state group's dated ``pending_change`` for ``today``
    (deadline_math.apply_pending_changes) and collect the flags. A field the
    config-override layer already set is left as the config value (config
    wins over the bundled transition), with no flag."""
    for group_name, group in list(answer.state.items()):
        if not isinstance(group, dict) or "pending_change" not in group:
            continue
        target = group.get("pending_change")
        field_name = target.get("field") if isinstance(target, dict) else None
        if field_name and f"{group_name}.{field_name}" in answer.sources:
            continue  # config override outranks the bundled pending change
        updated, flags = deadline_math.apply_pending_changes(group, today=today)
        answer.state[group_name] = updated
        answer.pending_flags.extend(flags)


# ── compact rendering ─────────────────────────────────────────────────


def _group(answer: JurisdictionAnswer, name: str) -> dict[str, Any] | None:
    group = answer.state.get(name)
    return group if isinstance(group, dict) else None


def _cite(group: dict[str, Any]) -> str:
    citation = group.get("citation")
    return f" ({citation})" if citation else ""


def _verify(group: dict[str, Any]) -> str:
    return " ⚠ verify against current statute before executing" if group.get("verify_flag") else ""


def _interest_phrase(interest: Any) -> str:
    """Render ``deposit.interest_owed``, which is a bool OR a qualifying STRING.

    ⚠ **A CONDITION COLLAPSED INTO A BARE CLAIM IS A WRONG STATEMENT ABOUT
    MONEY OWED TO A TENANT.** This read ``"interest owed" if interest else
    "no interest owed"``, and a non-empty string is truthy — so Illinois'
    ``"required for buildings with 25+ units"`` rendered as a flat
    ``interest owed``, identical to Connecticut's unconditional ``True``. A
    manager with a four-unit building in Illinois was told interest is owed
    when it is not. Measured against the shipped corpus: **42 of 51 states are
    bool, 9 carry a string**, and the strings are all narrowing conditions —
    unit counts (IL 25+, NY 6+), holding periods (NH 1 year, ND 9 months, PA
    year 2), a deposit threshold (NM), an accounting option (FL) and one rate
    rule (OH). Same shape as gotcha 51's FORMER tenant, pointed at a statute.

    ``interest_owed`` is the ONLY mixed bool/str field in the whole rules
    corpus — scanned, not assumed — so this is one instance, not a class.

    ⚠ **CONDITIONALLY is uppercase for gotcha 51's reason.** A model reads
    this row and writes prose; a lowercase mid-row qualifier is exactly what
    gets smoothed away in summary, which puts us back where we started.

    ⚠ **Blank is UNKNOWN, never "no".** An empty string is falsy, so the old
    expression answered a question the data declines to answer — the
    decline-to-assert posture of gotchas 55/57, on the side where being wrong
    costs a tenant money.
    """
    # bool FIRST: gotcha 27 records that `isinstance(True, int)` is True in
    # Python, so type dispatch here is order-sensitive by habit even though
    # the corpus is only ever bool or str.
    if isinstance(interest, bool):
        return "interest owed" if interest else "no interest owed"
    text = str(interest).strip()
    if not text:
        return f"interest owed {NOT_ENCODED}"
    return f"interest owed CONDITIONALLY ({text})"


def _deposit_line(answer: JurisdictionAnswer) -> str:
    group = _group(answer, "deposit")
    if group is None:
        return f"Deposit: {NOT_ENCODED}"
    bits: list[str] = []
    cap = group.get("cap_months")
    bits.append(f"cap {cap} months' rent" if cap is not None else "no statutory cap")
    deadline = group.get("return_deadline_days")
    if deadline is not None:
        trigger = group.get("deadline_trigger") or "tenancy end"
        bits.append(f"return within {deadline} days of {trigger}")
    else:
        bits.append(f"return deadline {NOT_ENCODED}")
    penalty = group.get("penalty")
    if penalty:
        bits.append(f"penalty {penalty}")
    interest = group.get("interest_owed")
    if interest is not None:
        bits.append(_interest_phrase(interest))
    return "Deposit: " + "; ".join(bits) + _cite(group) + _verify(group)


def _late_fee_line(answer: JurisdictionAnswer) -> str:
    group = _group(answer, "late_fee")
    if group is None:
        return f"Late fee: {NOT_ENCODED}"
    rule = group.get("rule")
    bits = [str(rule) if rule is not None else NOT_ENCODED]
    grace = group.get("grace_days")
    if grace is not None:
        bits.append(f"{grace}-day grace period")
    return "Late fee: " + "; ".join(bits) + _cite(group) + _verify(group)


def _notice_lines(answer: JurisdictionAnswer) -> list[str]:
    group = _group(answer, "notice")
    if group is None:
        return [f"Notice periods: {NOT_ENCODED}"]
    lines: list[str] = []
    nonpay = group.get("nonpay_days")
    if nonpay is not None:
        # Unit suffix only makes sense on a numeric count — string regimes
        # ("demand", "none - file directly") stand on their own.
        unit = group.get("nonpay_unit")
        suffix = f" ({unit} days)" if unit and isinstance(nonpay, int | float) else ""
        lines.append(f"Nonpayment notice: {nonpay}{suffix}{_cite(group)}{_verify(group)}")
    else:
        lines.append(f"Nonpayment notice: {NOT_ENCODED}")
    mtm = group.get("mtm_termination_days")
    if mtm is None:
        lines.append(f"Month-to-month termination: {NOT_ENCODED}")
    elif isinstance(mtm, int | float):
        lines.append(f"Month-to-month termination: {mtm} days")
    else:
        lines.append(f"Month-to-month termination: {mtm}")
    entry = group.get("entry_hours")
    if entry is None:
        lines.append(
            "Entry notice: none set by statute — give reasonable notice and verify locally"
        )
    elif isinstance(entry, int | float):
        lines.append(f"Entry notice: {entry} hours")
    else:
        lines.append(f"Entry notice: {entry}")
    return lines


def _screening_line(answer: JurisdictionAnswer) -> str:
    group = _group(answer, "screening")
    if group is None:
        return f"Source of income: {NOT_ENCODED}"
    soi = str(group.get("soi_protection") or "").strip().upper()
    if soi == "Y":
        text = "Source of income: PROTECTED — voucher refusal is illegal here"
    elif soi == "N":
        text = (
            "Source of income: no state protection (local rules may protect; never refuse vouchers)"
        )
    else:
        text = f"Source of income: {NOT_ENCODED}"
    note = group.get("soi_note")
    return text + (f" — {note}" if note else "")


def _licensing_line(answer: JurisdictionAnswer) -> str:
    group = _group(answer, "licensing")
    if group is None:
        return f"PM licensing: {NOT_ENCODED}"
    license_code = group.get("pm_license")
    text = (
        f"PM licensing: {license_code}"
        if license_code is not None
        else f"PM licensing: {NOT_ENCODED}"
    )
    note = group.get("note")
    return text + (f" — {note}" if note else "")


_MAX_RATE_LINES = 3


def _rates_lines(answer: JurisdictionAnswer) -> list[str]:
    """At most three annually-published-rate lines. A loaded value renders
    with its citation; ``value=null`` means the current-cycle figure is NOT
    loaded — render the formula + publisher and say so, NEVER a number."""
    rates = answer.state.get("rates")
    if not isinstance(rates, dict):
        return []
    entries = [(rid, r) for rid, r in rates.items() if isinstance(r, dict)]
    lines: list[str] = []
    for rate_id, rate in entries[:_MAX_RATE_LINES]:
        name = str(rate_id).replace("_", " ")
        citation = rate.get("citation") or "citation not encoded"
        value = rate.get("value")
        if value is not None:
            lines.append(f"Rate: {name} — {value}% ({citation})")
        else:
            formula = rate.get("formula") or "formula not encoded"
            publisher = rate.get("publisher") or "publisher not encoded"
            lines.append(f"Rate: {name} — {formula} ({publisher}) — current-year rate not loaded")
    hidden = len(entries) - _MAX_RATE_LINES
    if hidden > 0:
        lines.append(f"(+{hidden} more rates not shown)")
    return lines


def format_compact(answer: JurisdictionAnswer, *, property_label: str) -> str:
    """Render one jurisdiction answer as a compact (≤ ~26 line) snapshot.

    Always ends with the not-legal-advice footer; anything the snapshot
    doesn't encode renders as the conservative default, never a guess.
    """
    where = answer.state_name or answer.state_abbr or "unknown jurisdiction"
    lines = [f"{property_label} — {where} landlord-tenant snapshot:"]
    # ⚠ NAME WHAT WAS RESOLVED. The rules below come from the state block PLUS
    # whichever county/place overlay matched, and until 2026-09-10 the header
    # named only the state — so a property geocoded a block across a city line
    # gave a confidently wrong deposit or notice answer that read exactly like
    # a right one. The manager knows their own city; this is the line that lets
    # them catch it (coverage item 196).
    #
    # ⚠ Emitted ONLY when a name is actually known. Every row written before
    # migration 0099 has FIPS codes and NULL names, and "None County" would be
    # a fresh false statement where saying nothing is merely a missing one —
    # those render exactly as they did before this line existed.
    resolved = [p for p in (answer.place_name, answer.county_name) if p]
    if resolved:
        lines.append(f"- Resolved to: {', '.join(resolved)} ({answer.state_abbr})")
    lines.append(f"- {_deposit_line(answer)}")
    lines.append(f"- {_late_fee_line(answer)}")
    lines.extend(f"- {line}" for line in _notice_lines(answer))
    lines.append(f"- {_screening_line(answer)}")
    lines.append(f"- {_licensing_line(answer)}")
    tier = answer.state.get("eviction_speed_tier")
    lines.append(
        f"- Eviction speed: tier {tier} (1 = fastest)"
        if tier is not None
        else f"- Eviction speed: {NOT_ENCODED}"
    )
    quirks = [q for q in (answer.state.get("quirks") or []) if q][:3]
    if quirks:
        lines.append("- Quirks: " + "; ".join(str(q) for q in quirks))
    # Annually-published rates + dated-rule-change flags render before the
    # ⚠ overlay warnings.
    lines.extend(f"- {line}" for line in _rates_lines(answer))
    for flag in answer.pending_flags:
        lines.append(f"- ⚠ Rule change: {flag}")
    if answer.overlay_market:
        note = answer.overlay_note or (
            "local ordinances layer on top of state law here — never answer from state law alone"
        )
        lines.append(f"- ⚠ Overlay market: {note}")
    for place in answer.overlay_places:
        name = place.get("name") or "local jurisdiction"
        warning = place.get("warning") or "has its own ordinances — check local rules"
        license_flag = " (license to operate required)" if place.get("license_to_operate") else ""
        lines.append(f"- ⚠ {name}: {warning}{license_flag}")
    if answer.sources:
        lines.append("- Config overrides applied to: " + ", ".join(sorted(answer.sources)))
    lines.append(_FOOTER.format(version=answer.version or "unknown"))
    return "\n".join(lines)


__all__ = [
    "CENSUS_GEO_URL",
    "NOT_ENCODED",
    "STATE_FIPS_TO_ABBR",
    "JurisdictionAnswer",
    "JurisdictionStack",
    "format_compact",
    "load_rules",
    "lookup",
    "resolve_stack",
]
