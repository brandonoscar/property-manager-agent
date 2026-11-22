"""Exercise the whole A2P chain against the LIVE carrier API, for $0.

Every endpoint path in ``comms/a2p_pipeline`` is documented, not live-probed —
the module says so itself. A wrong path or field name costs a rejection, and a
rejection at the campaign step costs the brand fee that was already spent plus
a fortnight. This runs the real code against the real API so the first customer
is not the probe.

Two tiers, and the default is the free one:

  1. ``--tier free`` (DEFAULT) — steps 1-5: customer profile, both end users,
     address, supporting document, the entity assignments, and the trust
     product. **Charges nothing, ever.** This is where a wrong field name
     actually lives, so most of the value is here.

     ⚠ **What a green free tier means CHANGED on 2026-09-04.** It used to mean
     only "Twilio accepted every POST" — which it did, with 2xx on all five
     steps, while both bundles were ``twilio-rejected`` seconds later and this
     script still exited 0. Six rejected profiles came from trusting that. The
     pipeline now runs ``POST /Evaluations`` — the carrier's own policy check,
     free and repeatable — BEFORE each ``Status=pending-review``, and refuses
     to submit a bundle that fails it. So exit 0 now means the carrier itself
     called both bundles compliant, and a policy failure is exit 1 carrying
     each failed requirement verbatim.

  2. ``--tier mock`` — adds brand registration with the MOCK flag, the
     messaging service, and the campaign. A mock brand carries no fee, cannot
     route real messages, and expires after 30 days.

⚠ **The mock parameter name is UNPROBED.** twilio.com is egress-blocked from
the dev container (gotcha 75), so ``_MOCK_BRAND_PARAM`` is the likeliest thing
in the pipeline to be wrong — and a wrong name is IGNORED by the API, which
registers a real brand at full fee. The pipeline reads the response back and
refuses to continue unless the carrier itself says the brand is mock. That
cannot un-charge the fee; it makes the charge impossible to miss and stops the
run before the campaign spends more. **Run ``--tier free`` first, every time.**

⚠ **This creates real Trust Hub resources** on the account (a customer profile,
two end users, an address). They are free and they accumulate — the run prints
every SID it made so they can be cleaned up.

Usage (PowerShell; Python because the dev box has PowerShell 5.1 only):

    $env:PYTHONUTF8 = "1"
    $env:HELIXIS_TWILIO__ACCOUNT_SID = "<AC...>"
    $s = Read-Host "HELIXIS_TWILIO__AUTH_TOKEN" -AsSecureString     # NEVER an argument
    $env:HELIXIS_TWILIO__AUTH_TOKEN  = [Net.NetworkCredential]::new("", $s).Password
    $env:HELIXIS_TWILIO__WEBHOOK_BASE_URL = "https://<the deployed API>"
    $env:HELIXIS_TWILIO__PRIMARY_PROFILE_SID = "<BU...>"   # the ISV primary

    python scripts/a2p_mock_run.py                   # free tier
    python scripts/a2p_mock_run.py --tier mock       # + mock brand + campaign

Exit codes: 0 every step passed AND the carrier evaluated both bundles as
compliant; 1 a step failed or a bundle failed its policy evaluation (the
carrier's verbatim words are printed); 2 could not run at all (missing
credentials or configuration).

⚠ Exit 0 is a claim about VALIDITY, not about receipt. It was the weaker claim
until 2026-09-04 and read identically, which is the only reason the chain's
three evaluation defects survived as long as they did.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

# ⚠ UTF-8 before anything prints: this emits ⚠/✓ and a redirected run on
# Windows dies on the LAST line otherwise, after all the work (gotcha 49).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _console import force_utf8_console  # noqa: E402 — must follow the path setup

from helixis.comms import a2p, a2p_defaults, a2p_pipeline  # noqa: E402

force_utf8_console()

#: A throwaway business. Deliberately NOT a real customer: a mock brand still
#: creates a real customer profile, and filing one under a live company's name
#: would leave a stray registration nobody can tell from a genuine one.
FIXTURE: dict[str, Any] = {
    "legal_business_name": "Occupella A2P Harness LLC",
    "ein": "12-3456789",
    "business_type": "Limited Liability Corporation",
    "business_vertical": a2p_defaults.BUSINESS_VERTICAL,
    "website": "https://occupella.com",
    "street": "200 E Colfax Ave",
    "city": "Denver",
    "region": "CO",
    "postal_code": "80203",
    "iso_country": "US",
    "rep_first_name": "A2P",
    "rep_last_name": "Harness",
    "rep_email": "compliance@occupella.com",
    "rep_phone": "+13035550100",
    "rep_job_position": "Director",
    "use_case": a2p_defaults.USE_CASE,
    "campaign_description": a2p_defaults.CAMPAIGN_DESCRIPTION,
    "opt_in_description": a2p_defaults.opt_in_text(attested=True),
    "sample_messages": a2p_defaults.sample_messages("Occupella A2P Harness LLC"),
    # A real uuid so the trust-page URLs are well-formed. The pages 404 for it
    # (no registration row), which is itself worth seeing: a carrier fetching
    # a 404 is the 30933-adjacent failure, and better found here.
    "company_id": "00000000-0000-4000-8000-000000000001",
}


#: How long to wait for BRAND vetting before giving up, and how often to ask.
#: Brand vetting is minutes; the 10-15 DAY wait is the campaign's, which this
#: script does not wait on — it only needs the campaign ACCEPTED, not approved.
_BRAND_WAIT_S = 600.0
_BRAND_POLL_S = 15.0


def _credentials() -> a2p_pipeline.A2PCredentials | None:
    creds = a2p_pipeline.A2PCredentials(
        account_sid=os.environ.get("HELIXIS_TWILIO__ACCOUNT_SID", ""),
        auth_token=os.environ.get("HELIXIS_TWILIO__AUTH_TOKEN", ""),
        webhook_base_url=os.environ.get("HELIXIS_TWILIO__WEBHOOK_BASE_URL", ""),
        primary_profile_sid=os.environ.get("HELIXIS_TWILIO__PRIMARY_PROFILE_SID", ""),
    )
    return creds if creds.usable else None


def _report(updates: dict[str, Any]) -> None:
    """Print every SID this run created, so nothing is orphaned silently."""
    sids = updates.get("step_sids")
    made = json.loads(sids) if isinstance(sids, str) else dict(sids or {})
    for key in ("customer_profile_sid", "trust_product_sid", "brand_sid", "messaging_service_sid"):
        if updates.get(key):
            made[key] = updates[key]
    if not made:
        print("  (no resources created)")
        return
    print("  created:")
    for key, value in sorted(made.items()):
        print(f"    {key:28} {value}")


async def _run(tier: str) -> int:
    creds = _credentials()
    if creds is None:
        print("⚠ No Twilio credentials in the environment — nothing was run.")
        print("  Set HELIXIS_TWILIO__ACCOUNT_SID and HELIXIS_TWILIO__AUTH_TOKEN.")
        # ⚠ Exit 2, never 0. A harness that did not run is not one that passed
        # (the LED-013 skip-floor rule) — a green exit here would be read as
        # "the chain is validated" by whoever wired it into CI.
        return 2
    if not creds.webhook_base_url:
        print("⚠ HELIXIS_TWILIO__WEBHOOK_BASE_URL is empty.")
        print("  The campaign step needs it to build the policy URLs Twilio fetches.")
        return 2
    if not creds.primary_profile_sid:
        # ⚠ Exit 2 and not 1: nothing was measured. The pipeline refuses this
        # too, but saying it here names the variable before a single carrier
        # request is spent.
        print("⚠ HELIXIS_TWILIO__PRIMARY_PROFILE_SID is empty.")
        print("  Every secondary customer profile must be assigned to this")
        print("  account's APPROVED primary profile bundle (BU...) or the")
        print("  carrier rejects it: 'Primary customer profile bundle is null'.")
        return 2

    problems = a2p.validate(a2p_pipeline._details_from_row(FIXTURE))
    if problems:
        print("⚠ The fixture itself is incomplete — fix it before spending a request:")
        for problem in problems:
            print(f"    - {problem}")
        return 2

    free_only = tier == "free"
    print(f"A2P chain — tier={tier} ({'no charges possible' if free_only else 'mock brand'})")
    print(f"  base url: {creds.webhook_base_url}")
    print()

    row = dict(FIXTURE)
    try:
        updates = await a2p_pipeline.advance_submission(
            row,
            creds,
            callback_url=f"{creds.webhook_base_url}/api/v1/twilio/trusthub-status",
            stop_before_paid=free_only,
            mock=not free_only,
        )
    except a2p.A2PError as exc:
        # The carrier's own words — "address could not be validated" names the
        # fix, "registration failed" does not.
        print(f"✗ FAILED at step {exc.step!r}")
        print(f"  {exc}")
        if getattr(exc, "reason", ""):
            print(f"  carrier said: {exc.reason}")
        return 1

    print("✓ trust-hub chain completed" + ("" if free_only else " + brand and messaging service"))
    _report(updates)

    if free_only:
        print()
        print("Stopped before brand registration — the first step that costs money.")
        print("Re-run with --tier mock to exercise the brand and campaign for free.")
        return 0

    row.update(updates)
    if not row.get("brand_sid"):
        print("⚠ No brand SID — cannot submit a campaign.")
        return 1

    # ⚠ THE CAMPAIGN CANNOT GO IN WHILE THE BRAND IS PENDING. The carrier
    # says so in as many words — "Can't create a Campaign until Brand is
    # registered. Current status: PENDING" — and `submit_campaign`'s own
    # docstring says it is normally reached by the poller, days later, not
    # inline. This script had no wait at all, so it could only ever have hit
    # that error even with a perfect chain: the first two --tier mock runs
    # blamed the campaign for a brand that simply had not finished.
    #
    # Vetting is minutes for a brand (it is the CAMPAIGN that takes 10-15
    # days), so a bounded wait is enough to actually exercise the step.
    # `poll_status` is reused rather than re-reading the brand here: a second
    # status reader would be a second answer to "is it approved yet".
    print()
    print(f"  brand {row['brand_sid']} — waiting for vetting (up to {_BRAND_WAIT_S // 60} min)")
    waited = 0.0
    while True:
        polled = await a2p_pipeline.poll_status(row, creds)
        row.update(polled)
        state = str(polled.get("status") or "")
        if state == "brand_approved":
            print(f"  brand approved after {int(waited)}s")
            break
        if state == "failed":
            print("✗ FAILED at step 'brand'")
            print(f"  carrier said: {polled.get('failure_reason') or 'no reason given'}")
            return 1
        if waited >= _BRAND_WAIT_S:
            # ⚠ NOT exit 0 and NOT exit 1. The campaign was never reached, so
            # nothing was learned about it — reporting a pass would be a lie
            # and reporting a defect would blame the carrier's queue.
            print(
                f"⚠ brand still {state or 'pending'} after {int(waited)}s — "
                "the campaign was NOT exercised."
            )
            print(f"  Nothing is wrong yet; re-run to retry. brand_sid={row['brand_sid']}")
            return 2
        await asyncio.sleep(_BRAND_POLL_S)
        waited += _BRAND_POLL_S

    try:
        campaign = await a2p_pipeline.submit_campaign(row, creds)
    except a2p.A2PError as exc:
        print(f"✗ FAILED at step {exc.step!r}")
        print(f"  {exc}")
        if getattr(exc, "reason", ""):
            print(f"  carrier said: {exc.reason}")
        return 1
    print(f"✓ campaign submitted: {campaign.get('campaign_sid')} ({campaign.get('status')})")
    print()
    print("⚠ Mock brands and campaigns expire after 30 days and cannot send.")
    print("  Delete the SIDs above once the shapes are confirmed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tier",
        choices=("free", "mock"),
        default="free",
        help="free (default): steps 1-5, no charges possible. mock: + brand and campaign.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.tier))


if __name__ == "__main__":
    raise SystemExit(main())
