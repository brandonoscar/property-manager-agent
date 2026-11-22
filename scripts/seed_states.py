#!/usr/bin/env python3
"""50-state Buildium seed — the September-push test dataset.

Seeds a BLANK, dedicated Buildium account with one single-unit rental
property per US state (50 total — the account's unit cap), each with a
real, geocodable state-capital-area address, a tenant, and an active
lease with rent — then layers deterministic "chaos" on top (overdue
ledgers, duplicate bills, orphan invoices, contradictory work orders,
trick maintenance requests) so every detector, reminder, and the risk
bridge has something to find, and Occupella has real problems to reason
through per the founder's brief: "make some chaos … I want to see
Occupella reason through a problem."

⚠ PERMANENCE: Buildium has NO delete for properties, leases, tenants,
or charges, and payments are immutable (CLAUDE.md gotchas 21/22 + the
2026-08-20 API grounding). Everything this script creates is on the
account for good (properties can only be inactivated). It must ONLY be
run against the dedicated blank test account — the preflight prints the
account identity and refuses a non-empty account without --force.

Wire-shape provenance — now LIVE-PROVEN against production account
695763 (2026-08-20 run: 50/50 properties + leases, 523 requests, zero
429s), on top of the original grounding:
  * property / bill / to-do task / work-order bodies from the
    ``synthetic_traffic.py`` paths, PLUS the prod-only requirement the
    live run discovered: ``Units[].Address`` (and owners' ``Address``)
    are REQUIRED on production but not on the sandbox — expect that
    split on any nested-object POST body.
  * lease charges/payments via prod-probed ``Leases.post_charge`` /
    ``record_payment`` (GlAccountId vs GLAccountId casing — gotcha 21);
    ``POST /v1/leases`` with embedded Tenants is now live-proven too.
  * ⚠ gotcha 24: production SILENTLY IGNORES ``?propertyids`` on
    ``GET /tasks`` and ``GET /bills`` (works on /leases and
    /rentals/units) — every guard here scopes CLIENT-SIDE (tasks by
    ``Property.Id``, bills by ``Lines[].AccountingEntity.Id``).
  * A date-past lease does NOT read as Past — the tenant must be moved
    out (the ``history`` phase does both).

Phases, in run order: preflight · bootstrap · probe · properties ·
leases · livestate (chaos kits + work orders) · history (past leases +
move-outs) · pipeline (renewals, applicants, listings) · comms
(announcements) · owners · report. Spec-only calls run through
``_attempt`` — a refusal is recorded verbatim and printed under PROBE
RESULTS instead of crashing the run.

Usage (PowerShell, from the repo root, creds in env):
  python scripts/seed_states.py --phase preflight       # read-only report
  python scripts/seed_states.py --yes                   # full run (first time)
  python scripts/seed_states.py --yes --force           # top-up an already-seeded account
  python scripts/seed_states.py --phase livestate --yes --force  # one phase

Every phase is idempotent: properties are matched by their "XX — "
name prefix, leases by property, chaos by its deterministic task
titles / existing manual charges — a re-run tops up gaps, never
duplicates. Once the account holds seed properties, the blank-account
guard requires ``--force`` (working as designed: it protects any OTHER
account from a mistaken run).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from helixis.buildium.api import BuildiumApi
from helixis.buildium.client import BuildiumClient
from helixis.buildium.errors import BuildiumApiError
from helixis.settings import Settings, get_settings

logger = logging.getLogger("seed_states")

# ── The 50 states ────────────────────────────────────────────────────
# (abbr, state, street, city, zip, tenant_first, tenant_last, rent)
# Addresses are state-capitol-area streets — reliably resolvable by the
# keyless Census geocoder. The preflight geocodes every address BEFORE
# anything is created; an unresolved address excludes that state from
# the run (permanent seeds must not be burned on a bad address).
STATES: list[tuple[str, str, str, str, str, str, str, int]] = [
    ("AL", "Alabama", "600 Dexter Ave", "Montgomery", "36104", "Marcus", "Whitfield", 1150),
    ("AK", "Alaska", "120 4th St", "Juneau", "99801", "Nadia", "Kootoo", 1650),
    ("AZ", "Arizona", "1700 W Washington St", "Phoenix", "85007", "Elena", "Vasquez", 1550),
    ("AR", "Arkansas", "500 Woodlane St", "Little Rock", "72201", "Doyle", "Prather", 1050),
    ("CA", "California", "1315 10th St", "Sacramento", "95814", "Priya", "Natarajan", 2450),
    ("CO", "Colorado", "200 E Colfax Ave", "Denver", "80203", "Aaron", "Tillman", 1950),
    ("CT", "Connecticut", "210 Capitol Ave", "Hartford", "06106", "Rosa", "DiMarco", 1750),
    ("DE", "Delaware", "411 Legislative Ave", "Dover", "19901", "Terrence", "Boyd", 1400),
    ("FL", "Florida", "400 S Monroe St", "Tallahassee", "32399", "Camila", "Reyes", 1600),
    ("GA", "Georgia", "206 Washington St SW", "Atlanta", "30334", "Jasmine", "Holloway", 1700),
    ("HI", "Hawaii", "415 S Beretania St", "Honolulu", "96813", "Keanu", "Alameda", 2600),
    ("ID", "Idaho", "700 W Jefferson St", "Boise", "83702", "Wade", "Sorensen", 1350),
    ("IL", "Illinois", "401 S 2nd St", "Springfield", "62701", "Dominique", "Carver", 1300),
    ("IN", "Indiana", "200 W Washington St", "Indianapolis", "46204", "Beth", "Kowalski", 1200),
    ("IA", "Iowa", "1007 E Grand Ave", "Des Moines", "50319", "Harlan", "Petersen", 1100),
    ("KS", "Kansas", "300 SW 10th Ave", "Topeka", "66612", "Renee", "Stuckey", 1000),
    ("KY", "Kentucky", "700 Capital Ave", "Frankfort", "40601", "Cole", "Bramble", 1050),
    ("LA", "Louisiana", "900 N 3rd St", "Baton Rouge", "70802", "Odette", "Fontenot", 1250),
    ("ME", "Maine", "210 State St", "Augusta", "04330", "Silas", "Thibodeau", 1300),
    ("MD", "Maryland", "100 State Cir", "Annapolis", "21401", "Imani", "Drummond", 1850),
    ("MA", "Massachusetts", "24 Beacon St", "Boston", "02133", "Declan", "Sullivan", 2350),
    ("MI", "Michigan", "100 N Capitol Ave", "Lansing", "48933", "Tanya", "Vandermeer", 1150),
    ("MN", "Minnesota", "658 Cedar St", "St Paul", "55155", "Astrid", "Lindqvist", 1450),
    ("MS", "Mississippi", "400 High St", "Jackson", "39201", "Curtis", "Weathers", 950),
    ("MO", "Missouri", "201 W Capitol Ave", "Jefferson City", "65101", "Gwen", "Sappington", 1050),
    ("MT", "Montana", "1301 E 6th Ave", "Helena", "59601", "Boone", "Tetreault", 1200),
    ("NE", "Nebraska", "1445 K St", "Lincoln", "68508", "Marie", "Ostrander", 1100),
    ("NV", "Nevada", "101 N Carson St", "Carson City", "89701", "Sonia", "Delgado", 1500),
    ("NH", "New Hampshire", "107 N Main St", "Concord", "03301", "Everett", "Chase", 1650),
    ("NJ", "New Jersey", "125 W State St", "Trenton", "08608", "Farrah", "Okafor", 1900),
    ("NM", "New Mexico", "490 Old Santa Fe Trail", "Santa Fe", "87501", "Luz", "Archuleta", 1350),
    ("NY", "New York", "198 State St", "Albany", "12210", "Moshe", "Grunfeld", 2100),
    ("NC", "North Carolina", "1 E Edenton St", "Raleigh", "27601", "Vivian", "Locklear", 1550),
    ("ND", "North Dakota", "600 E Boulevard Ave", "Bismarck", "58505", "Lars", "Vollmer", 1050),
    ("OH", "Ohio", "77 S High St", "Columbus", "43215", "Deja", "Whitcomb", 1250),
    ("OK", "Oklahoma", "2300 N Lincoln Blvd", "Oklahoma City", "73105", "Wyatt", "Redcorn", 1100),
    ("OR", "Oregon", "900 Court St NE", "Salem", "97301", "Meredith", "Yamasaki", 1600),
    ("PA", "Pennsylvania", "501 N 3rd St", "Harrisburg", "17120", "Gus", "Antonelli", 1350),
    ("RI", "Rhode Island", "82 Smith St", "Providence", "02903", "Noreen", "Pacheco", 1700),
    ("SC", "South Carolina", "1100 Gervais St", "Columbia", "29201", "Tobias", "Middleton", 1300),
    ("SD", "South Dakota", "500 E Capitol Ave", "Pierre", "57501", "Ingrid", "Haugen", 1000),
    ("TN", "Tennessee", "600 Charlotte Ave", "Nashville", "37243", "Darnell", "Pruitt", 1500),
    ("TX", "Texas", "1100 Congress Ave", "Austin", "78701", "Selena", "Cavazos", 1850),
    ("UT", "Utah", "350 N State St", "Salt Lake City", "84103", "Porter", "Bingham", 1550),
    ("VT", "Vermont", "115 State St", "Montpelier", "05633", "Maeve", "Coolidge", 1450),
    ("VA", "Virginia", "1000 Bank St", "Richmond", "23219", "Reggie", "Taliaferro", 1650),
    ("WA", "Washington", "1063 Capitol Way S", "Olympia", "98501", "Hana", "Sokolov", 1900),
    ("WV", "West Virginia", "1900 Kanawha Blvd E", "Charleston", "25305", "Earl", "McGraw", 900),
    ("WI", "Wisconsin", "2 E Main St", "Madison", "53703", "Greta", "Steinbach", 1400),
    ("WY", "Wyoming", "200 W 24th St", "Cheyenne", "82001", "Colt", "Ferguson", 1150),
]

# Route-testing states (naturally dense corridor — the maintenance-route
# tool's stops, since the 50-unit cap left no room for a metro cluster).
ROUTE_STATES = {"CT", "RI", "MA", "NH", "VT", "NY", "NJ", "PA", "DE", "MD"}

_SUB_TYPES = ["SingleFamily", "CondoTownhome", "MultiFamily"]

# ── Chaos catalog (deterministic per state index) ───────────────────
# Ledger kits — who owes what. Mixed so the portfolio isn't uniformly
# delinquent (a real book has mostly-current tenants + a hard tail).
#   overdue : 2 back-dated months charged, nothing paid   → delinquency,
#             rent_not_collected, chronic-late aging
#   partial : charged 2 months, paid ~60% of one          → partial-payment reasoning
#   current : charged last month, paid in full            → healthy baseline
#   overpaid: charged, then paid 105% ("keep it for next month?") → credit reasoning
LEDGER_KITS = ["overdue", "current", "partial", "current", "overpaid"]

# Bill kits — one per state, rotating:
#   duplicate : two near-identical bills, same vendor, 4 days apart → duplicate_vendor_bill
#   orphan    : an invoice with NO matching work order              → invoice_no_work_order
#   overdue   : dated -60d, due -30d, unpaid                        → aging payables
BILL_KITS = ["duplicate", "orphan", "overdue"]

# ⚠ The invoice amount PER ROW and the row count per kit, hoisted to module
# scope so the ANSWER KEY can compute the payables total instead of a human
# typing it. `score_battery.build_ground_truth` reads these; the seeding code
# below is the only other reader. Same rule as `buildium/money.py::bill_amount`
# — a number two places both depend on lives in ONE of them, or they drift and
# the battery starts grading the product against a figure the fixture never had.
#
# ⚠ PER ROW, not per kit. `duplicate` plants TWO invoices of this amount, so
# that property's exposure is amount x rows. Reading it as a kit total and
# dividing halves every seeded duplicate invoice — caught here before it
# reached the fixture, but only because the derived total stopped matching the
# $26,000 the existing payables tests already knew.
BILL_AMOUNTS: dict[str, float] = {"duplicate": 450.00, "orphan": 380.00, "overdue": 265.00}
#: How many invoice ROWS each kit creates (duplicate plants a near-identical pair).
BILL_ROWS: dict[str, int] = {"duplicate": 2, "orphan": 1, "overdue": 1}

# Trick tasks — the founder's "make it double think" asks. Each is a
# deterministic title+description pair; titles double as idempotency
# markers, so keep them stable.
TRICKS: list[tuple[str, str, str]] = [
    (
        "priority_mismatch",
        "URGENT: water heater leaking into the closet",
        "Tenant reports active dripping from the water heater into the hall closet and "
        "says it is getting worse. Entered with priority Low by the previous assistant — "
        "someone should double-check that.",
    ),
    (
        "impossible_ref",
        "Noise and thumping from the unit above us",
        "Tenant says the unit directly above has loud thumping every night after 11pm and "
        "asks us to talk to that neighbor. (This is a single-unit building.)",
    ),
    (
        "conflicting_ask",
        "Replace the front door lock but keep the same keys",
        "Tenant wants the deadbolt replaced because it sticks, but insists the existing "
        "keys must keep working afterward and does not want to be rekeyed.",
    ),
    (
        "vague_emergency",
        "Something smells weird sometimes",
        "Tenant reports an odd smell in the kitchen that comes and goes, maybe gas, maybe "
        "the disposal, mostly in the evening. No other details provided.",
    ),
    (
        "duplicate_report_a",
        "Kitchen faucet dripping constantly",
        "Tenant reports the kitchen faucet has been dripping for a week and the drip is "
        "speeding up.",
    ),
]
# The second half of the duplicate-report pair (same issue, different
# words, filed two days later) — only created alongside duplicate_report_a.
DUPLICATE_REPORT_B = (
    "Leaky tap in the kitchen — still not fixed",
    "Following up: the tap in the kitchen is still leaking and now there's water pooling "
    "under the sink. First reported last week.",
)

WO_TITLE = "HVAC not cooling — schedule vendor visit"
WO_DESC = (
    "Tenant reports the AC blows warm air during the afternoon. Filter was changed last "
    "month. Needs a vendor diagnostic visit; tenant available weekday mornings."
)

# Work-order variety — one per state, rotating; the corridor states get
# WO_TITLE on top (their second open WO) so route stops stay dense.
WO_VARIETY: list[tuple[str, str]] = [
    (
        "Garbage disposal jammed and humming",
        "Disposal hums but doesn't spin; tenant already tried the reset button. Likely a "
        "jammed flywheel — send a plumber with an allen key and a spare unit just in case.",
    ),
    (
        "Smoke detector chirping — battery or unit?",
        "Hallway smoke detector chirps every 45 seconds. Tenant replaced the battery once "
        "already; if it keeps chirping the unit may be past its 10-year life.",
    ),
    (
        "Back fence leaning after windstorm",
        "Two fence posts are leaning ~20 degrees after last week's storm. Needs post reset "
        "or replacement before it comes down on the neighbor's side.",
    ),
    (
        "Water stain spreading on bedroom ceiling",
        "Brown stain on the bedroom ceiling grew noticeably this week. Could be a roof "
        "leak or the upstairs bathroom supply line — needs eyes on it before drywall goes.",
    ),
    (
        "Front porch light out — possible wiring issue",
        "Bulb replaced twice and it still cuts out. Fixture flickers when the door slams, "
        "so probably a loose connection in the box.",
    ),
]


# Prior tenants for the past-lease history — derived from the name pool
# with offsets so no past tenant collides with a current one.
def prior_tenant(index: int) -> tuple[str, str]:
    first = STATES[(index + 13) % len(STATES)][5]
    last = STATES[(index + 29) % len(STATES)][6]
    return first, last


# Owner roster — each owns a slice of the portfolio (fills the owners
# read surface + the Operations table).
OWNERS: list[tuple[str, str]] = [
    ("Margaret", "Whitlock"),
    ("Sanjay", "Bhatt"),
    ("Dolores", "Pemberton"),
    ("Frank", "Iverson"),
]

ANNOUNCEMENTS: list[tuple[str, str, str]] = [
    (
        "corridor",
        "Gutter cleaning scheduled next week",
        "Our vendor will be on-site next week for seasonal gutter cleaning. No entry "
        "needed; please move vehicles away from the building perimeter that morning.",
    ),
    (
        "all",
        "Office closed Labor Day",
        "The management office is closed Monday for Labor Day. For maintenance "
        "emergencies, use the emergency line as usual — normal requests resume Tuesday.",
    ),
]

VENDORS = [
    ("Apex Plumbing Co", True),
    ("Volt & Vine Electric", True),
    ("Redbud General Maintenance", True),
]

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


def property_name(abbr: str, street: str) -> str:
    """Tagged, unmistakable test-property name: 'OK — 2300 N Lincoln Blvd'."""
    return f"{abbr} — {street}"[:127]


def state_prefix(name: str) -> str | None:
    """Inverse of property_name: 'OK — …' → 'OK', else None."""
    if len(name) > 4 and name[2:5] == " — " and name[:2].isalpha() and name[:2].isupper():
        return name[:2]
    return None


def ledger_kit(index: int) -> str:
    return LEDGER_KITS[index % len(LEDGER_KITS)]


def bill_kit(index: int) -> str:
    return BILL_KITS[index % len(BILL_KITS)]


def trick_for(index: int) -> tuple[str, str, str] | None:
    """Every other state gets one trick task, rotating through the catalog."""
    if index % 2 != 0:
        return None
    return TRICKS[(index // 2) % len(TRICKS)]


def renewal_window(index: int) -> bool:
    """~1 in 5 leases ends inside the renewal window (25 days out)."""
    return index % 5 == 3


@dataclass
class SeedContext:
    api: BuildiumApi
    bank_account_id: int = 0
    income_gl_id: int = 0
    expense_gl_id: int = 0
    staff_user_id: int = 0
    vendor_ids: list[int] = field(default_factory=list)
    # abbr → property dict (Id, Name, unit id)
    properties: dict[str, dict[str, Any]] = field(default_factory=dict)
    # abbr → lease dict (Id, tenant id)
    leases: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Spec-only endpoints that refused us: (label, detail). Printed in
    # the report — each entry IS a live probe result (the 422 body names
    # the real contract; gotcha-15 discipline: never guess, walk).
    unsupported: list[tuple[str, str]] = field(default_factory=list)


async def _attempt(ctx: SeedContext, label: str, coro: Any) -> Any | None:
    """Run a spec-only (never-live-probed) call; on a Buildium refusal,
    record the verbatim detail as probe data and keep going."""
    try:
        return await coro
    except BuildiumApiError as exc:
        detail = f"status={exc.status} {str(exc)[:1200]}"
        ctx.unsupported.append((label, detail))
        logger.warning("attempt %s refused: %s", label, detail)
        return None


def _today() -> Any:
    return datetime.now(UTC).date()


def _iso(days_from_today: int) -> str:
    return (_today() + timedelta(days=days_from_today)).isoformat()


def _first_of_next_month() -> str:
    today = _today()
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return f"{year:04d}-{month:02d}-01"


def _first_of_month_after(days_from_today: int) -> str:
    """First day of the month FOLLOWING (today + days) — a renewal's
    first rent due date."""
    anchor = _today() + timedelta(days=days_from_today)
    year, month = (anchor.year + 1, 1) if anchor.month == 12 else (anchor.year, anchor.month + 1)
    return f"{year:04d}-{month:02d}-01"


# ── Payload builders (pure — unit-tested) ────────────────────────────


def build_property_body(index: int, bank_account_id: int) -> dict[str, Any]:
    abbr, _state, street, city, zipc, _f, _l, rent = STATES[index]
    return {
        "Name": property_name(abbr, street),
        "Address": {
            "AddressLine1": street,
            "City": city,
            "State": abbr,
            "PostalCode": zipc,
            "Country": "UnitedStates",
        },
        "RentalSubType": _SUB_TYPES[index % len(_SUB_TYPES)],
        "OperatingBankAccountId": bank_account_id,
        "YearBuilt": 1948 + (index * 7) % 70,
        "Units": [
            {
                "UnitNumber": "1",
                "MarketRent": rent,
                # Production REQUIRES Units[].Address; the sandbox does
                # not (422-proven live 2026-08-20 — the nested-Address
                # prod/sandbox split, same class as owners below).
                "Address": {
                    "AddressLine1": street,
                    "City": city,
                    "State": abbr,
                    "PostalCode": zipc,
                    "Country": "UnitedStates",
                },
            }
        ],
    }


def build_lease_body(index: int, unit_id: int, income_gl_id: int) -> dict[str, Any]:
    abbr, _state, street, city, zipc, first, last, rent = STATES[index]
    to_days = 25 if renewal_window(index) else 210
    return {
        "LeaseType": "Fixed",
        "UnitId": unit_id,
        "LeaseFromDate": _iso(-155),
        "LeaseToDate": _iso(to_days),
        # Never email invented tenants — same rule as the applicants'
        # pinned SendRentalApplicationEmail (synthetic_traffic).
        "SendWelcomeEmail": False,
        "Tenants": [
            {
                "FirstName": first,
                "LastName": last,
                "Email": f"{first}.{last}@example.com".lower(),
                "Address": {
                    "AddressLine1": f"{street} Unit 1",
                    "City": city,
                    "State": abbr,
                    "PostalCode": zipc,
                    "Country": "UnitedStates",
                },
            }
        ],
        "Rent": {
            "Cycle": "Monthly",
            # Write-side casing is GlAccountId (lower-l) — gotcha 21/22.
            "Charges": [
                {"Amount": rent, "GlAccountId": income_gl_id, "NextDueDate": _first_of_next_month()}
            ],
        },
    }


def build_bill_body(
    property_id: int,
    vendor_id: int,
    expense_gl_id: int,
    *,
    amount: float,
    memo: str,
    date_offset: int,
    due_offset: int,
) -> dict[str, Any]:
    return {
        "Date": _iso(date_offset),
        "DueDate": _iso(due_offset),
        "VendorId": vendor_id,
        "Memo": memo,
        "Lines": [
            {
                "AccountingEntity": {"Id": property_id, "AccountingEntityType": "Rental"},
                "GlAccountId": expense_gl_id,
                "Amount": amount,
                "Memo": memo,
            }
        ],
    }


def build_task_body(
    property_id: int,
    staff_user_id: int,
    title: str,
    description: str,
    priority: str = "Normal",
) -> dict[str, Any]:
    return {
        "Title": title,
        "Description": description,
        "Priority": priority,
        # POST /tasks/todorequests uses TaskStatus (the WO-embedded task
        # uses Status — read/write asymmetry, synthetic_traffic PR #201).
        "TaskStatus": "New",
        "AssignedToUserId": staff_user_id,
        "PropertyId": property_id,
    }


def build_work_order_body(property_id: int, vendor_id: int, staff_user_id: int) -> dict[str, Any]:
    return {
        "PropertyId": property_id,
        "Title": WO_TITLE,
        "Description": WO_DESC,
        "VendorId": vendor_id,
        "EntryAllowed": "Unknown",
        "Task": {
            "Title": WO_TITLE,
            "Description": WO_DESC,
            "Priority": "Normal",
            "Status": "New",  # POST-side key (NOT TaskStatus) — gotcha class
            "AssignedToUserId": staff_user_id,
            "PropertyId": property_id,
        },
    }


def build_past_lease_body(index: int, unit_id: int) -> dict[str, Any]:
    """A completed historical tenancy (status derives from the dates).
    Ends -160d, safely before the active lease's -155d start; no Rent
    block — history shouldn't add recurring-charge templates."""
    abbr, _state, street, city, zipc, _f, _l, _rent = STATES[index]
    first, last = prior_tenant(index)
    return {
        "LeaseType": "Fixed",
        "UnitId": unit_id,
        "LeaseFromDate": _iso(-730),
        "LeaseToDate": _iso(-160),
        "SendWelcomeEmail": False,
        "Tenants": [
            {
                "FirstName": first,
                "LastName": last,
                "Email": f"{first}.{last}.prior@example.com".lower(),
                "Address": {
                    "AddressLine1": f"{street} Unit 1",
                    "City": city,
                    "State": abbr,
                    "PostalCode": zipc,
                    "Country": "UnitedStates",
                },
            }
        ],
    }


def build_renewal_body(index: int, income_gl_id: int) -> dict[str, Any]:
    """The 422-walked renewal floor (gotcha 22) — nothing more, nothing
    less: {LeaseType, SendWelcomeEmail, Rent{Cycle, Charges}}. Creates
    the FUTURE lease term on the renewal-window states. Rent steps +$50."""
    rent = STATES[index][7]
    return {
        "LeaseType": "Fixed",
        "SendWelcomeEmail": False,
        "Rent": {
            "Cycle": "Monthly",
            "Charges": [
                {
                    "Amount": rent + 50,
                    "GlAccountId": income_gl_id,
                    "NextDueDate": _first_of_month_after(25),
                }
            ],
        },
    }


def build_owner_body(first: str, last: str, property_ids: list[int]) -> dict[str, Any]:
    return {
        "FirstName": first,
        "LastName": last,
        "IsCompany": False,
        "PropertyIds": property_ids,
        # Production REQUIRES a nested Address (422-proven 2026-08-20:
        # {"Key":"Address","Value":"The value is missing or invalid."}).
        "Address": {
            "AddressLine1": f"{200 + len(last)} Market St",
            "City": "Wilmington",
            "State": "DE",
            "PostalCode": "19801",
            "Country": "UnitedStates",
        },
    }


def build_announcement_body(subject: str, body: str, property_ids: list[int]) -> dict[str, Any]:
    return {
        "Subject": subject,
        "Body": body,
        "ExpirationDate": _iso(30),
        "PropertyIds": property_ids,
        # 422-probed 2026-08-20: both flags demanded on production. The
        # second key was truncated in the first capture — if this still
        # 422s, the widened probe buffer will name it in full.
        "IncludeAlternateEmail": False,
        "NotifyAssociationTenants": False,
    }


# ── Phases ───────────────────────────────────────────────────────────


async def geocode_check(limit: int) -> list[str]:
    """Pre-flight every address through the Census geocoder (public,
    keyless). Returns the list of abbrs that FAILED to resolve — those
    states are excluded from creation (seeds are permanent; don't burn
    one on a bad address)."""
    failed: list[str] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for abbr, _s, street, city, zipc, *_rest in STATES[:limit]:
            oneline = f"{street}, {city}, {abbr} {zipc}"
            try:
                resp = await client.get(
                    CENSUS_URL,
                    params={
                        "address": oneline,
                        "benchmark": "Public_AR_Current",
                        "format": "json",
                    },
                )
                matches = (resp.json().get("result") or {}).get("addressMatches") or []
                if not matches:
                    failed.append(abbr)
                    logger.warning("geocode MISS: %s (%s)", abbr, oneline)
            except Exception as exc:  # noqa: BLE001 — census has no SLA; treat as miss
                failed.append(abbr)
                logger.warning("geocode check errored for %s: %s", abbr, exc)
    return failed


async def preflight(ctx: SeedContext, limit: int, skip_geocode: bool) -> tuple[bool, list[str]]:
    """Read-only account + address report. Returns (account_is_blank,
    geocode_failures)."""
    api = ctx.api
    account: dict[str, Any] | None = None
    try:
        account = await api.client.request("GET", "/administration/account")
    except BuildiumApiError as exc:
        logger.warning("account-info probe unavailable (plan tier?): %s", exc)
    rentals = await api.client.request("GET", "/rentals", query={"limit": 100})
    rentals = rentals if isinstance(rentals, list) else []
    tagged = [r for r in rentals if state_prefix(str(r.get("Name") or ""))]
    untagged = [r for r in rentals if not state_prefix(str(r.get("Name") or ""))]

    print("── preflight ────────────────────────────────────────────")
    if account:
        print(f"account : {account.get('Name')!r} (Id={account.get('Id')})")
    print(f"host    : {ctx.api.client._settings.base_url}")  # noqa: SLF001 — operator report
    print(f"rentals : {len(rentals)} total ({len(tagged)} seed-tagged, {len(untagged)} other)")

    failures: list[str] = []
    if not skip_geocode:
        failures = await geocode_check(limit)
        ok = limit - len(failures)
        print(f"geocode : {ok}/{limit} addresses resolve via Census")
        if failures:
            print(f"          EXCLUDED (fix address + re-run): {', '.join(failures)}")
    print("─────────────────────────────────────────────────────────")
    return len(untagged) == 0, failures


async def bootstrap(ctx: SeedContext) -> None:
    """Blank-account prerequisites: bank account, GLs, staff user, vendors."""
    api = ctx.api

    banks = await api.client.request("GET", "/bankaccounts", query={"limit": 10})
    banks = banks if isinstance(banks, list) else []
    if banks:
        ctx.bank_account_id = int(banks[0]["Id"])
    else:
        created = await api.client.request(
            "POST",
            "/bankaccounts",
            body={
                "Name": "Occupella Test Operating",
                "BankAccountType": "Checking",
                "Country": "UnitedStates",
            },
        )
        ctx.bank_account_id = int(created["Id"])
        logger.info("created operating bank account id=%s", ctx.bank_account_id)

    gls = await api.client.request("GET", "/glaccounts", query={"limit": 500})
    gls = gls if isinstance(gls, list) else []

    def _pick_gl(gl_type: str) -> int:
        for g in gls:
            if g.get("Type") == gl_type and not g.get("IsBankAccount") and g.get("IsActive", True):
                return int(g["Id"])
        raise SystemExit(f"no active {gl_type} GL account on this account — cannot seed ledgers")

    ctx.income_gl_id = _pick_gl("Income")
    ctx.expense_gl_id = _pick_gl("Expense")

    users = await api.client.request("GET", "/users", query={"limit": 20})
    users = users if isinstance(users, list) else []
    if not users:
        raise SystemExit("GET /users returned nobody — cannot assign tasks")
    ctx.staff_user_id = int(users[0]["Id"])

    cats = await api.client.request("GET", "/vendors/categories", query={"limit": 50})
    cats = cats if isinstance(cats, list) else []
    if cats:
        cat_id = int(cats[0]["Id"])
    else:
        created = await api.client.request(
            "POST", "/vendors/categories", body={"Name": "General Maintenance"}
        )
        cat_id = int(created["Id"])

    existing = await api.client.request("GET", "/vendors", query={"limit": 200})
    existing = existing if isinstance(existing, list) else []
    by_name = {str(v.get("CompanyName") or ""): int(v["Id"]) for v in existing}
    for name, _is_company in VENDORS:
        if name in by_name:
            ctx.vendor_ids.append(by_name[name])
            continue
        created = await api.client.request(
            "POST", "/vendors", body={"IsCompany": True, "CompanyName": name, "CategoryId": cat_id}
        )
        ctx.vendor_ids.append(int(created["Id"]))
        logger.info("created vendor %r id=%s", name, created["Id"])


async def load_existing(ctx: SeedContext) -> None:
    """Index already-seeded properties + leases by state prefix."""
    api = ctx.api
    rentals = await api.client.request("GET", "/rentals", query={"limit": 100})
    for r in rentals if isinstance(rentals, list) else []:
        abbr = state_prefix(str(r.get("Name") or ""))
        if abbr:
            ctx.properties[abbr] = r
    if not ctx.properties:
        return
    prop_ids = ",".join(str(p["Id"]) for p in ctx.properties.values())
    leases = await api.client.request(
        "GET", "/leases", query={"propertyids": prop_ids, "limit": 200}
    )
    by_prop = {int(p["Id"]): a for a, p in ctx.properties.items()}
    for lease in leases if isinstance(leases, list) else []:
        abbr = by_prop.get(int(lease.get("PropertyId") or 0))
        if abbr and abbr not in ctx.leases:
            ctx.leases[abbr] = lease


async def create_property(ctx: SeedContext, index: int) -> None:
    abbr = STATES[index][0]
    if abbr in ctx.properties:
        return
    body = build_property_body(index, ctx.bank_account_id)
    created = await ctx.api.client.request("POST", "/rentals", body=body)
    ctx.properties[abbr] = created
    logger.info("property %s id=%s", abbr, created.get("Id"))


async def _unit_id_of(ctx: SeedContext, prop: dict[str, Any]) -> int:
    units = prop.get("Units") or []
    if units and isinstance(units[0], dict) and units[0].get("Id"):
        return int(units[0]["Id"])
    # Some list payloads omit Units — fetch them explicitly.
    fetched = await ctx.api.client.request(
        "GET", "/rentals/units", query={"propertyids": str(prop["Id"]), "limit": 5}
    )
    if isinstance(fetched, list) and fetched:
        return int(fetched[0]["Id"])
    raise SystemExit(f"property {prop.get('Id')} has no units — cannot lease")


async def create_lease(ctx: SeedContext, index: int, *, probe: bool = False) -> None:
    abbr = STATES[index][0]
    if abbr in ctx.leases:
        return
    prop = ctx.properties[abbr]
    unit_id = await _unit_id_of(ctx, prop)
    body = build_lease_body(index, unit_id, ctx.income_gl_id)
    created = await ctx.api.client.request("POST", "/leases", body=body)
    ctx.leases[abbr] = created
    if probe:
        # The keystone verification: this endpoint has never been called
        # by this codebase before. Insist on the shape the rest of the
        # seed depends on — a lease id and a real tenant id back.
        lease_id = created.get("Id")
        tenants = created.get("Tenants") or created.get("CurrentTenants") or []
        tenant_id = tenants[0].get("Id") if tenants and isinstance(tenants[0], dict) else None
        if not lease_id or not tenant_id:
            raise SystemExit(
                "LEASE PROBE FAILED — response shape unexpected; not proceeding.\n"
                f"Response: {created!r}"
            )
        print(f"lease probe OK: lease={lease_id} tenant={tenant_id} ({abbr})")
    logger.info("lease %s id=%s", abbr, created.get("Id"))


async def apply_livestate(ctx: SeedContext, index: int) -> None:
    """Chaos kit for one state. Idempotent via task-title markers and
    the presence of manual ledger charges."""
    abbr, _s, _street, _city, _zip, first, _last, rent = STATES[index]
    prop = ctx.properties.get(abbr)
    lease = ctx.leases.get(abbr)
    if not prop or not lease:
        logger.warning("livestate %s skipped — property/lease missing", abbr)
        return
    api = ctx.api
    prop_id = int(prop["Id"])
    lease_id = int(lease["Id"])
    vendor_id = ctx.vendor_ids[index % len(ctx.vendor_ids)]

    # Ledger kit — skip if this lease already has manual charges.
    charges = await api.client.request("GET", f"/leases/{lease_id}/charges", query={"limit": 5})
    if not (isinstance(charges, list) and charges):
        kit = ledger_kit(index)
        lines = [{"GlAccountId": ctx.income_gl_id, "Amount": rent}]
        pay_lines = [{"GLAccountId": ctx.income_gl_id, "Amount": rent}]  # caps-L — gotcha 21
        if kit == "overdue":
            await api.leases.post_charge(lease_id, date=_iso(-63), lines=lines, memo="Monthly rent")
            await api.leases.post_charge(lease_id, date=_iso(-33), lines=lines, memo="Monthly rent")
        elif kit == "partial":
            await api.leases.post_charge(lease_id, date=_iso(-33), lines=lines, memo="Monthly rent")
            partial = [{"GLAccountId": ctx.income_gl_id, "Amount": round(rent * 0.6, 2)}]
            await api.leases.record_payment(
                lease_id,
                date=_iso(-20),
                payment_method="Check",
                lines=partial,
                memo=f"Partial payment — {first} said the rest is coming Friday",
            )
        elif kit == "overpaid":
            await api.leases.post_charge(lease_id, date=_iso(-33), lines=lines, memo="Monthly rent")
            over = [{"GLAccountId": ctx.income_gl_id, "Amount": round(rent * 1.05, 2)}]
            await api.leases.record_payment(
                lease_id,
                date=_iso(-25),
                payment_method="Check",
                lines=over,
                memo="Overpaid — apply the extra to next month?",
            )
        else:  # current
            await api.leases.post_charge(lease_id, date=_iso(-33), lines=lines, memo="Monthly rent")
            await api.leases.record_payment(
                lease_id,
                date=_iso(-28),
                payment_method="Check",
                lines=pay_lines,
                memo="Rent payment",
            )
        logger.info("ledger %s kit=%s", abbr, kit)

    # Bills — skip if this property already has unpaid bills (gotcha 14:
    # paidstatus=Unpaid needs no date bounds).
    bills = await api.client.request(
        "GET", "/bills", query={"propertyids": str(prop_id), "paidstatus": "Unpaid", "limit": 200}
    )
    # Buildium IGNORES ?propertyids on /bills (gotcha 24, live-verified
    # 2026-08-20) and list rows carry no PropertyId — ownership lives on
    # Lines[].AccountingEntity.Id. Without this client-side scope, one
    # state's bills blocked every other state's kit.
    own_bills = [
        b
        for b in (bills if isinstance(bills, list) else [])
        if isinstance(b, dict)
        and any(
            isinstance(ln, dict) and (ln.get("AccountingEntity") or {}).get("Id") == prop_id
            for ln in (b.get("Lines") or [])
        )
    ]
    if not own_bills:
        bkit = bill_kit(index)
        if bkit == "duplicate":
            dup_memos = (
                (-6, "Water heater repair — unit 1"),
                (-2, "Water heater service call — unit 1"),
            )
            for offset, memo in dup_memos:
                await api.bills.create(
                    build_bill_body(
                        prop_id,
                        vendor_id,
                        ctx.expense_gl_id,
                        amount=BILL_AMOUNTS["duplicate"],
                        memo=memo,
                        date_offset=offset,
                        due_offset=offset + 30,
                    )
                )
        elif bkit == "orphan":
            await api.bills.create(
                build_bill_body(
                    prop_id,
                    vendor_id,
                    ctx.expense_gl_id,
                    amount=BILL_AMOUNTS["orphan"],
                    memo="Emergency roof patch after storm",
                    date_offset=-9,
                    due_offset=21,
                )
            )
        else:  # overdue
            await api.bills.create(
                build_bill_body(
                    prop_id,
                    vendor_id,
                    ctx.expense_gl_id,
                    amount=BILL_AMOUNTS["overdue"],
                    memo="Gutter cleaning — past due invoice",
                    date_offset=-60,
                    due_offset=-30,
                )
            )
        logger.info("bills %s kit=%s", abbr, bkit)

    # Trick tasks + duplicate-report pair — skip by exact-title match.
    tasks = await api.client.request(
        "GET", "/tasks", query={"propertyids": str(prop_id), "limit": 200}
    )
    # Same gotcha-24 trap as /bills: the filter param is ignored, so an
    # unscoped title guard goes account-global (each distinct title
    # would seed exactly once across all 50 states).
    task_rows = [
        t
        for t in (tasks if isinstance(tasks, list) else [])
        if isinstance(t, dict) and (t.get("Property") or {}).get("Id") == prop_id
    ]
    existing_titles = {str(t.get("Title") or "") for t in task_rows}
    trick = trick_for(index)
    if trick:
        _key, title, desc = trick
        if title not in existing_titles:
            priority = "Low" if _key == "priority_mismatch" else "Normal"
            await api.tasks.create(
                fields=build_task_body(prop_id, ctx.staff_user_id, title, desc, priority)
            )
            if _key == "duplicate_report_a" and DUPLICATE_REPORT_B[0] not in existing_titles:
                await api.tasks.create(
                    fields=build_task_body(prop_id, ctx.staff_user_id, *DUPLICATE_REPORT_B)
                )
            logger.info("trick %s → %s", abbr, _key)

    # One variety work order per state ("every endpoint a little
    # something" — founder direction 2026-08-20)…
    wo_title, wo_desc = WO_VARIETY[index % len(WO_VARIETY)]
    if wo_title not in existing_titles:
        body = build_work_order_body(prop_id, vendor_id, ctx.staff_user_id)
        body["Title"] = body["Task"]["Title"] = wo_title
        body["Description"] = body["Task"]["Description"] = wo_desc
        await api.work_orders.create(body)
        logger.info("work order %s: %s", abbr, wo_title)

    # …and the corridor states get the HVAC one on top (route density).
    if abbr in ROUTE_STATES and WO_TITLE not in existing_titles:
        await api.work_orders.create(build_work_order_body(prop_id, vendor_id, ctx.staff_user_id))
        logger.info("work order %s (route corridor)", abbr)


def _lease_tenant_id(lease: dict[str, Any]) -> int | None:
    """First tenant id off a lease payload — POST responses carry
    ``Tenants``; list rows carry ``CurrentTenants``."""
    for key in ("Tenants", "CurrentTenants"):
        tenants = lease.get(key) or []
        if tenants and isinstance(tenants[0], dict) and tenants[0].get("Id"):
            return int(tenants[0]["Id"])
    return None


async def _retire_lease(ctx: SeedContext, abbr: str, lease: dict[str, Any]) -> None:
    """Move the tenant out so a date-past lease actually READS as past.
    Live finding (2026-08-20 run): Buildium does NOT derive LeaseStatus
    from dates — a historical-term lease stays Active until its tenant
    is moved out. Move-out is the live-proven, reversible write
    (gotcha 22): POST /leases/{id}/moveouts {TenantId, MoveOutDate}."""
    lease_id = int(lease["Id"])
    tenant_id = _lease_tenant_id(lease)
    if tenant_id is None:
        ctx.unsupported.append((f"retire-lease {abbr}", "no tenant id on lease payload"))
        return
    move_out_date = str(lease.get("LeaseToDate") or "")[:10] or _iso(-160)
    await _attempt(
        ctx,
        f"move-out {abbr} (POST /leases/{{id}}/moveouts)",
        ctx.api.client.request(
            "POST",
            f"/leases/{lease_id}/moveouts",
            body={"TenantId": tenant_id, "MoveOutDate": move_out_date},
        ),
    )


async def apply_history(ctx: SeedContext, index: int) -> None:
    """Past lease on the odd-index states (~25 completed tenancies).

    Two-step, because dates alone don't make a lease "past" (live
    finding, 2026-08-20 run: all 25 historical-term leases reported
    LeaseStatus: Active): create the historical lease, then MOVE THE
    TENANT OUT — that's what flips the status. Idempotent top-up: a
    date-past lease that's still Active from a previous run gets its
    move-out here without creating anything new."""
    if index % 2 != 1:
        return
    abbr = STATES[index][0]
    prop = ctx.properties.get(abbr)
    if not prop:
        return
    prop_id = int(prop["Id"])
    existing = await ctx.api.client.request(
        "GET", "/leases", query={"propertyids": str(prop_id), "limit": 10}
    )
    rows = existing if isinstance(existing, list) else []
    today = _iso(0)
    past_dated = [
        r for r in rows if isinstance(r, dict) and str(r.get("LeaseToDate") or "")[:10] < today
    ]
    if past_dated:
        # Already created (this run or a prior one) — just make sure it
        # actually reads Past.
        for lease in past_dated:
            if str(lease.get("LeaseStatus") or "") == "Active":
                await _retire_lease(ctx, abbr, lease)
        return
    unit_id = await _unit_id_of(ctx, prop)
    created = await _attempt(
        ctx,
        f"past-lease {abbr} (POST /leases, historical term)",
        ctx.api.client.request("POST", "/leases", body=build_past_lease_body(index, unit_id)),
    )
    if created is not None:
        logger.info("past lease %s id=%s", abbr, created.get("Id"))
        await _retire_lease(ctx, abbr, created)


async def apply_pipeline(ctx: SeedContext, index: int) -> None:
    """Leasing pipeline: renewals (→ FUTURE lease terms) on the
    renewal-window states, applicants on every 8th state, and a listing
    attempt on every 6th (the listing PUT is a deliberate 422-walk —
    the exact body has never been probed; the refusal names the
    contract)."""
    abbr = STATES[index][0]
    prop = ctx.properties.get(abbr)
    lease = ctx.leases.get(abbr)
    if not prop:
        return
    prop_id = int(prop["Id"])
    api = ctx.api

    if renewal_window(index) and lease:
        lease_id = int(lease["Id"])
        existing = await api.client.request(
            "GET", "/leases", query={"propertyids": str(prop_id), "limit": 10}
        )
        rows = existing if isinstance(existing, list) else []
        has_future = any(str(r.get("LeaseFromDate") or "") > _iso(0) for r in rows)
        if not has_future:
            created = await _attempt(
                ctx,
                f"renewal {abbr} (POST /leases/{{id}}/renewals)",
                api.client.request(
                    "POST",
                    f"/leases/{lease_id}/renewals",
                    body=build_renewal_body(index, ctx.income_gl_id),
                ),
            )
            if created is not None:
                logger.info("renewal %s (future term) ok", abbr)

    if index % 8 == 0:
        unit_id = await _unit_id_of(ctx, prop)
        first = STATES[(index + 7) % len(STATES)][5]
        last = STATES[(index + 21) % len(STATES)][6]
        email = f"{first}.{last}.applicant@example.com".lower()
        page = await api.client.request("GET", "/applicants", query={"limit": 200})
        emails = {str(a.get("Email") or "") for a in (page if isinstance(page, list) else [])}
        if email not in emails:
            await _attempt(
                ctx,
                f"applicant {abbr}",
                api.client.request(
                    "POST",
                    "/applicants",
                    body={
                        "FirstName": first,
                        "LastName": last,
                        "Email": email,
                        "SendRentalApplicationEmail": False,
                        "UnitId": unit_id,
                    },
                ),
            )

    if index % 6 == 0:
        unit_id = await _unit_id_of(ctx, prop)
        existing_listing = await _attempt(
            ctx,
            f"listing-read {abbr}",
            api.client.request("GET", f"/rentals/units/{unit_id}/listing"),
        )
        if existing_listing is None:
            # 422-walk the PUT with the minimal plausible body; the
            # response detail (recorded in the report) is the probe.
            await _attempt(
                ctx,
                f"listing-put {abbr} (PUT /rentals/units/{{id}}/listing)",
                api.client.request(
                    "PUT",
                    f"/rentals/units/{unit_id}/listing",
                    # Rent bounds 422-probed live: must be 100..99999999.99.
                    body={"AvailableDate": _iso(35), "Rent": STATES[index][7]},
                ),
            )


async def apply_comms(ctx: SeedContext) -> None:
    """Announcements — corridor-scoped + portfolio-wide. Spec-grounded,
    never live-probed: attempts record their refusals as probe data."""
    api = ctx.api
    existing = await _attempt(
        ctx,
        "announcements-read (GET /communications/announcements)",
        api.client.request("GET", "/communications/announcements", query={"limit": 100}),
    )
    subjects = {
        str(a.get("Subject") or "") for a in (existing if isinstance(existing, list) else [])
    }
    corridor_ids = [int(p["Id"]) for a, p in ctx.properties.items() if a in ROUTE_STATES]
    all_ids = [int(p["Id"]) for p in ctx.properties.values()]
    for scope, subject, body_text in ANNOUNCEMENTS:
        if subject in subjects:
            continue
        ids = corridor_ids if scope == "corridor" else all_ids
        if not ids:
            continue
        await _attempt(
            ctx,
            f"announcement ({scope})",
            api.client.request(
                "POST",
                "/communications/announcements",
                body=build_announcement_body(subject, body_text, ids),
            ),
        )


async def apply_owners(ctx: SeedContext) -> None:
    """Rental owners, each owning a slice of the portfolio."""
    api = ctx.api
    existing = await _attempt(
        ctx,
        "owners-read (GET /rentals/owners)",
        api.client.request("GET", "/rentals/owners", query={"limit": 200}),
    )
    names = {
        (str(o.get("FirstName") or ""), str(o.get("LastName") or ""))
        for o in (existing if isinstance(existing, list) else [])
    }
    all_ids = sorted(int(p["Id"]) for p in ctx.properties.values())
    if not all_ids:
        return
    per = max(1, len(all_ids) // len(OWNERS))
    for n, (first, last) in enumerate(OWNERS):
        if (first, last) in names:
            continue
        chunk = all_ids[n * per : (n + 1) * per] or all_ids[-per:]
        await _attempt(
            ctx,
            f"owner {first} {last} (POST /rentals/owners)",
            api.client.request(
                "POST", "/rentals/owners", body=build_owner_body(first, last, chunk)
            ),
        )


async def report(ctx: SeedContext) -> None:
    print("── seed report ──────────────────────────────────────────")
    print(f"properties : {len(ctx.properties)}/50 seeded")
    print(f"leases     : {len(ctx.leases)}")
    missing = [s[0] for s in STATES if s[0] not in ctx.properties]
    if missing:
        print(f"missing    : {', '.join(missing)}")
    print("expect in Occupella (after the mirror sync + detector cycle):")
    print("  · delinquency + rent_not_collected on the 'overdue' states")
    print("  · duplicate_vendor_bill + invoice_no_work_order findings → risk-bridge inbox cards")
    print("  · renewal-window reminders on ~10 leases (+ future terms where renewals landed)")
    print("  · past leases on the odd states; applicants in the leasing pipeline")
    print("  · trick tasks in the Inbox; ~60 open WOs (corridor states carry two)")
    if ctx.unsupported:
        print("PROBE RESULTS — endpoints that refused (verbatim details, keep these):")
        for label, detail in ctx.unsupported:
            print(f"  ✗ {label}: {detail}")
    print("─────────────────────────────────────────────────────────")


# ── Main ─────────────────────────────────────────────────────────────

PHASES = [
    "preflight",
    "bootstrap",
    "probe",
    "properties",
    "leases",
    "livestate",
    "history",
    "pipeline",
    "comms",
    "owners",
    "report",
]

# Phases that need the bootstrap ids + the existing-entity index.
_NEEDS_BOOTSTRAP = {
    "bootstrap",
    "probe",
    "properties",
    "leases",
    "livestate",
    "history",
    "pipeline",
    "comms",
    "owners",
}


async def run(args: argparse.Namespace) -> int:
    settings: Settings = get_settings()
    if not settings.buildium.client_id or not settings.buildium.client_secret:
        sys.stderr.write("HELIXIS_BUILDIUM__CLIENT_ID / CLIENT_SECRET must be set\n")
        return 2

    limit = max(1, min(args.limit, len(STATES)))
    async with BuildiumClient(settings.buildium) as client:
        ctx = SeedContext(api=BuildiumApi(client))

        blank, geo_failed = await preflight(ctx, limit, args.skip_geocode_check)
        if args.phase == "preflight":
            return 0
        if not blank and not args.force:
            sys.stderr.write(
                "refusing: this account has non-seed rentals. This script is for the "
                "DEDICATED BLANK test account only (seeds are permanent). --force overrides.\n"
            )
            return 2
        if not args.yes:
            sys.stderr.write("dry gate: pass --yes to write (everything created is permanent)\n")
            return 2

        excluded = set(geo_failed)
        indices = [i for i in range(limit) if STATES[i][0] not in excluded]

        if args.phase == "all" or args.phase in _NEEDS_BOOTSTRAP:
            await bootstrap(ctx)
            await load_existing(ctx)

        if args.phase in ("all", "probe") and indices:
            first_index = indices[0]
            await create_property(ctx, first_index)
            await create_lease(ctx, first_index, probe=True)

        if args.phase in ("all", "properties"):
            for i in indices:
                await create_property(ctx, i)

        if args.phase in ("all", "leases"):
            for i in indices:
                await create_lease(ctx, i)

        if args.phase in ("all", "livestate"):
            for i in indices:
                try:
                    await apply_livestate(ctx, i)
                except BuildiumApiError as exc:
                    # Contain per state — one bad kit must not strand the rest.
                    logger.error("livestate %s failed: %s", STATES[i][0], exc)

        if args.phase in ("all", "history"):
            for i in indices:
                try:
                    await apply_history(ctx, i)
                except BuildiumApiError as exc:
                    logger.error("history %s failed: %s", STATES[i][0], exc)

        if args.phase in ("all", "pipeline"):
            for i in indices:
                try:
                    await apply_pipeline(ctx, i)
                except BuildiumApiError as exc:
                    logger.error("pipeline %s failed: %s", STATES[i][0], exc)

        if args.phase in ("all", "comms"):
            await apply_comms(ctx)

        if args.phase in ("all", "owners"):
            await apply_owners(ctx)

        await report(ctx)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="50-state Buildium seed (permanent!)")
    parser.add_argument("--phase", choices=["all", *PHASES], default="all")
    parser.add_argument("--limit", type=int, default=50, help="seed only the first N states")
    parser.add_argument("--yes", action="store_true", help="actually write (required)")
    parser.add_argument("--force", action="store_true", help="run against a non-blank account")
    parser.add_argument("--skip-geocode-check", action="store_true")
    args = parser.parse_args()
    # Stock Windows consoles default to cp1252 and crash on the report's
    # box-drawing characters (live-hit 2026-08-20).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
