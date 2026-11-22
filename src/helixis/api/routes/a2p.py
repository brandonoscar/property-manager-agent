"""A2P 10DLC registration — the customer-facing half of the white-label flow.

The customer never opens a carrier account, never sees a vendor name and never
leaves Occupella. They fill this in; we run the ISV pipeline against OUR
messaging account (``comms/a2p.py`` holds the status machine, the field
contract and the wire payloads).

⚠ **"Submit" here means QUEUED, and every string in this module says so.**
The carrier pipeline is a separate runner. This endpoint validates the details
and moves the row ``draft -> submitted``; it does NOT talk to Twilio, and a
reply that said "submitted to the carriers" would be the same class of claim
as a 2xx read as a completed write (gotcha 70) — reporting an outcome that has
not happened. The status a customer sees must never run ahead of the row.

⚠ **ADMIN only for writes.** This is a legal attestation about the business,
naming an authorized representative who will be emailed for 2FA. That is a
strictly stronger act than managing consent rows, and matches the floor on
claiming a phone number (``/sms/numbers``). READS are open to any member —
everyone needs to see why Leasing is closed.

⚠ **The wait is the product constraint and the copy must not soften it.**
Brand vetting returns in minutes; CAMPAIGN vetting is 10-15 days and per-number
carrier approval takes several more. Nothing here shortens that. What
submitting early buys is that the clock starts now.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from helixis.api.auth import (
    AuthenticatedUser,
    get_current_user,
    require_unambiguous_company,
)
from helixis.api.roles import ADMIN, normalize_role, role_at_least
from helixis.common.support import SUPPORT_EMAIL
from helixis.comms import a2p, a2p_defaults
from helixis.db.engine import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/a2p", tags=["a2p"])

#: Statuses whose row the customer may still edit. Once queued, the details
#: are what we are about to submit on their behalf; letting them drift from
#: what the runner reads would mean the confirmation they saw described a
#: different registration from the one that went out.
EDITABLE_STATUSES = ("draft", "failed")

#: Every column the form owns, in the order the table declares them. Kept as
#: DATA because the SELECT, the upsert and the response all have to agree —
#: three hand-written copies drift (the reason ``REQUIRED_FIELDS`` is a list).
_DETAIL_COLUMNS = (
    "legal_business_name",
    "ein",
    "business_type",
    "business_vertical",
    "website",
    "street",
    "city",
    "region",
    "postal_code",
    "iso_country",
    "rep_first_name",
    "rep_last_name",
    "rep_email",
    "rep_phone",
    "rep_job_position",
    "use_case",
    "campaign_description",
    "opt_in_description",
)


class RegistrationIn(BaseModel):
    """What the form sends. Everything optional so a half-filled draft saves.

    ⚠ ``ein`` is ``str | None`` and the absence is MEANINGFUL — no EIN means a
    Sole Proprietor brand, an EIN means Standard, and TCR rejects the wrong
    kind (``a2p.brand_kind``). An empty string is NOT the same as absent and
    must not be normalised into one, so the blank-to-None coercion below is
    deliberate and applies to ``ein`` exactly as to any other optional text.
    """

    legal_business_name: str = ""
    ein: str | None = None
    business_type: str = ""
    website: str | None = None
    street: str = ""
    city: str = ""
    region: str = ""
    postal_code: str = ""
    iso_country: str = "US"
    rep_first_name: str = ""
    rep_last_name: str = ""
    rep_email: str = ""
    rep_phone: str = ""
    rep_job_position: str = ""

    #: ⚠ The ONE consent fact only the customer can supply — whether their own
    #: lease/application discloses texting. A checkbox, not a paragraph: it is
    #: impossible to word badly and still a real attestation. Without it
    #: `opt_in_description` renders empty and `a2p.validate` blocks the submit,
    #: because a campaign with no consent basis is not one we can register in
    #: somebody's name.
    consent_attested: bool = False


class RegistrationOut(BaseModel):
    status: str
    #: Everything the form owns, echoed back so the client renders the SAVED
    #: row rather than its own optimistic copy.
    details: dict[str, Any]
    sample_messages: list[str]
    #: What still stops a submission, in the customer's words. Present on a
    #: draft read too, so the form can show its own gaps before they press
    #: anything.
    problems: list[str]
    editable: bool
    #: The carrier's own words on a rejection, verbatim (never paraphrased —
    #: "address could not be validated" is actionable, "failed" is not).
    failure_reason: str | None = None
    submitted_at: datetime | None = None
    brand_approved_at: datetime | None = None
    campaign_approved_at: datetime | None = None


def _company_id(user: AuthenticatedUser) -> str:
    if not user.company_ids:
        raise HTTPException(status_code=403, detail="No company on this account.")
    return str(user.company_ids[0])


async def _require_admin(user: AuthenticatedUser, company_id: str) -> None:
    """Admin-or-refuse, FAIL CLOSED — an unverifiable role is not an admin.

    Same posture and wording shape as ``/sms/numbers``: registering a brand
    names a real person as the authorized representative and is an attestation
    about the business, so an unreadable role must refuse rather than assume.
    """
    try:
        row = await get_pool().fetchrow(
            "SELECT role FROM public.company_users "
            "WHERE user_id = $1::uuid AND company_id = $2::uuid",
            user.user_id,
            company_id,
        )
    except Exception:  # noqa: BLE001 — an unverifiable role is no role
        logger.warning("a2p role check failed for user=%s", user.user_id)
        raise HTTPException(status_code=403, detail="Could not verify your role.") from None
    role = normalize_role(row["role"]) if row and row["role"] else None
    if not role_at_least(role, ADMIN):
        raise HTTPException(
            status_code=403,
            detail="Only an admin can register the business for text messaging.",
        )


def _blank_to_none(value: str | None) -> str | None:
    """`""` and `None` both mean "not supplied" coming off an HTML form."""
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def _details_from(payload: RegistrationIn) -> a2p.BusinessDetails:
    """Build the submission from the form, SERVER-FILLING the four fields the
    customer must not author.

    ⚠ These are set from ``comms/a2p_defaults`` unconditionally — a client
    value is ignored, not merely defaulted. The vertical, the campaign
    description, the samples and the enforced half of the opt-in text are all
    facts about what THIS SOFTWARE does, identical for every customer, and they
    are the three fields carriers reject over. A field a caller can override is
    a field that can be wrong, and there is no version of a customer knowing
    better here.

    The one thing only they know — whether their own paperwork discloses
    texting — stays theirs, as a checkbox (``consent_attested``).
    """
    return a2p.BusinessDetails(
        legal_business_name=payload.legal_business_name.strip(),
        business_type=payload.business_type.strip(),
        business_vertical=a2p_defaults.BUSINESS_VERTICAL,
        street=payload.street.strip(),
        city=payload.city.strip(),
        region=payload.region.strip(),
        postal_code=payload.postal_code.strip(),
        rep_first_name=payload.rep_first_name.strip(),
        rep_last_name=payload.rep_last_name.strip(),
        rep_email=payload.rep_email.strip(),
        rep_phone=payload.rep_phone.strip(),
        rep_job_position=payload.rep_job_position.strip(),
        campaign_description=a2p_defaults.CAMPAIGN_DESCRIPTION,
        opt_in_description=a2p_defaults.opt_in_text(attested=payload.consent_attested),
        sample_messages=a2p_defaults.sample_messages(payload.legal_business_name),
        ein=_blank_to_none(payload.ein),
        website=_blank_to_none(payload.website),
        iso_country=(payload.iso_country or "US").strip() or "US",
        use_case=a2p_defaults.USE_CASE,
    )


def _row_to_out(row: Any) -> RegistrationOut:
    """Render a stored row, re-deriving `problems` from what is SAVED.

    ⚠ Re-derived, never stored. A cached problem list would go stale the
    moment the row is edited, and a form telling somebody a field is missing
    when they have just filled it in is how a working submit button gets read
    as broken.
    """
    details = {c: row[c] for c in _DETAIL_COLUMNS}
    raw_samples = row["sample_messages"]
    samples = json.loads(raw_samples) if isinstance(raw_samples, str) else list(raw_samples or [])
    status = row["status"]
    stored = a2p.BusinessDetails(
        **{k: (v or "") for k, v in details.items() if k not in ("ein", "website")},
        ein=details["ein"],
        website=details["website"],
        sample_messages=samples,
    )
    return RegistrationOut(
        status=status,
        details=details,
        sample_messages=samples,
        problems=a2p.validate(stored),
        editable=status in EDITABLE_STATUSES,
        failure_reason=row["failure_reason"],
        submitted_at=row["submitted_at"],
        brand_approved_at=row["brand_approved_at"],
        campaign_approved_at=row["campaign_approved_at"],
    )


def _empty_registration() -> RegistrationOut:
    """A company with no row reads as an EMPTY DRAFT, not a 404.

    "Not started" is a real state of this flow, and the form's job is to be
    fillable. A 404 would make the client invent the empty shape itself.

    ⚠ The defaults come from ``BusinessDetails``, never restated here — the
    country and use-case defaults belong to the field contract, and a second
    copy in this module is the drift gotcha 25 keeps finding.
    """
    # The comprehension covers only the str-typed fields — the four excluded
    # ones are optional or defaulted — but mypy cannot narrow a dict
    # comprehension's value type per key, so it sees a `dict[str, str]` that
    # might be supplying `sample_messages: list[str]`. The exclusion is the
    # real constraint and it is right here in the expression.
    blank = a2p.BusinessDetails(
        **{  # type: ignore[arg-type]
            f: "" for f in _DETAIL_COLUMNS if f not in ("ein", "website", "iso_country", "use_case")
        }
    )
    details: dict[str, Any] = {c: None for c in _DETAIL_COLUMNS}
    details["iso_country"] = blank.iso_country
    details["use_case"] = blank.use_case
    return RegistrationOut(
        status="draft",
        details=details,
        sample_messages=[],
        problems=a2p.validate(blank),
        editable=True,
    )


_SELECT = (
    "SELECT status, failure_reason, submitted_at, brand_approved_at, "
    "campaign_approved_at, sample_messages, " + ", ".join(_DETAIL_COLUMNS) + " "
    "FROM public.helixis_a2p_registrations WHERE company_id = $1::uuid"
)


@router.get("/registration", response_model=RegistrationOut)
async def get_registration(
    user: AuthenticatedUser = Depends(get_current_user),
) -> RegistrationOut:
    """The caller's company's registration. READ is open to any member.

    A company with no row yet reads as an EMPTY DRAFT rather than a 404: the
    form's job is to be fillable, and "not started" is a real state of this
    flow, not a missing resource.
    """
    company_id = _company_id(user)
    row = await get_pool().fetchrow(_SELECT, company_id)
    if row is None:
        return _empty_registration()
    return _row_to_out(row)


@router.put("/registration", response_model=RegistrationOut)
async def save_registration(
    payload: RegistrationIn,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RegistrationOut:
    """Save the draft. ADMIN only. Partial is fine — this is not the submit.

    Returns the SAVED row with a freshly-derived `problems` list, so the form
    shows what is still missing without pretending anything was sent.
    """
    company_id = _company_id(user)
    await _require_admin(user, company_id)

    pool = get_pool()
    existing = await pool.fetchrow(
        "SELECT status FROM public.helixis_a2p_registrations WHERE company_id = $1::uuid",
        company_id,
    )
    if existing is not None and existing["status"] not in EDITABLE_STATUSES:
        # Editing a queued registration would let the details drift from what
        # the runner is about to submit -- the customer would have confirmed
        # one thing and we would send another.
        raise HTTPException(
            status_code=409,
            detail=(
                "This registration has already been submitted and can't be edited. "
                "Email " + SUPPORT_EMAIL + " if something needs to change."
            ),
        )

    details = _details_from(payload)
    values = [
        company_id,
        details.legal_business_name,
        details.ein,
        details.business_type,
        details.business_vertical,
        details.website,
        details.street,
        details.city,
        details.region,
        details.postal_code,
        details.iso_country,
        details.rep_first_name,
        details.rep_last_name,
        details.rep_email,
        details.rep_phone,
        details.rep_job_position,
        details.use_case,
        details.campaign_description,
        details.opt_in_description,
        json.dumps(details.sample_messages),
    ]
    # company_id is UNIQUE, so the upsert is the whole concurrency story: two
    # admins saving at once cannot create two brands for one business.
    await pool.execute(
        """
        INSERT INTO public.helixis_a2p_registrations (
            company_id, legal_business_name, ein, business_type, business_vertical,
            website, street, city, region, postal_code, iso_country,
            rep_first_name, rep_last_name, rep_email, rep_phone, rep_job_position,
            use_case, campaign_description, opt_in_description, sample_messages
        ) VALUES (
            $1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
            $12, $13, $14, $15, $16, $17, $18, $19, $20::jsonb
        )
        ON CONFLICT (company_id) DO UPDATE SET
            legal_business_name = EXCLUDED.legal_business_name,
            ein = EXCLUDED.ein,
            business_type = EXCLUDED.business_type,
            business_vertical = EXCLUDED.business_vertical,
            website = EXCLUDED.website,
            street = EXCLUDED.street,
            city = EXCLUDED.city,
            region = EXCLUDED.region,
            postal_code = EXCLUDED.postal_code,
            iso_country = EXCLUDED.iso_country,
            rep_first_name = EXCLUDED.rep_first_name,
            rep_last_name = EXCLUDED.rep_last_name,
            rep_email = EXCLUDED.rep_email,
            rep_phone = EXCLUDED.rep_phone,
            rep_job_position = EXCLUDED.rep_job_position,
            use_case = EXCLUDED.use_case,
            campaign_description = EXCLUDED.campaign_description,
            opt_in_description = EXCLUDED.opt_in_description,
            sample_messages = EXCLUDED.sample_messages,
            updated_at = now()
        """,
        *values,
    )
    row = await pool.fetchrow(_SELECT, company_id)
    return _row_to_out(row)


@router.post("/registration/submit", response_model=RegistrationOut)
async def submit_registration(
    user: AuthenticatedUser = Depends(get_current_user),
) -> RegistrationOut:
    """Queue the registration for submission. ADMIN only.

    ⚠ This moves the row ``draft -> submitted``. It does NOT reach a carrier:
    the ISV pipeline is a separate runner that picks queued rows up. The
    status vocabulary is deliberately the same one the runner advances, so
    when it lands nothing here changes — and until it does, no string in this
    response claims the carriers have seen anything.

    Validation is strict here and lenient on save, deliberately. A rejected
    brand costs a registration fee and a 10-15 day wait to discover a typo, so
    the cheap half of that check runs before anything is queued.
    """
    # ⚠ Not `_company_id(user)`. This files a LEGAL ATTESTATION and is
    # one-way: the row locks to `submitted`, and `company_id` is UNIQUE, so
    # the company that actually operates could never get its own
    # registration afterwards. GET and PUT above keep [0] deliberately —
    # reading or editing a draft is reversible and must stay frictionless.
    company_id = require_unambiguous_company(user)
    await _require_admin(user, company_id)

    pool = get_pool()
    row = await pool.fetchrow(_SELECT, company_id)
    if row is None:
        raise HTTPException(
            status_code=400, detail="Fill in your business details before submitting."
        )
    if row["status"] not in EDITABLE_STATUSES:
        # Idempotent by intent: a double-click must not queue a second brand.
        # TCR rejects duplicates and a brand costs money, so the second press
        # returns the CURRENT state rather than an error the user cannot act
        # on -- they asked for it to be submitted, and it is.
        return _row_to_out(row)

    current = _row_to_out(row)
    if current.problems:
        raise HTTPException(status_code=422, detail={"problems": current.problems})

    await pool.execute(
        "UPDATE public.helixis_a2p_registrations "
        "SET status = 'submitted', submitted_at = $2, failure_reason = NULL, updated_at = now() "
        "WHERE company_id = $1::uuid",
        company_id,
        datetime.now(UTC),
    )
    logger.info("a2p registration queued for company=%s", company_id)
    return _row_to_out(await pool.fetchrow(_SELECT, company_id))
