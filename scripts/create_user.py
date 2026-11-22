#!/usr/bin/env python3
"""Create a Helixis user account on a real Supabase project.

Solves the common bootstrap problem: a fresh Supabase instance has no
users, so signing into the production app is impossible until at least
one account exists. This script does the full sequence atomically:

  1. POST {supabase_url}/auth/v1/admin/users  (Auth API, service role)
       — hashed password, identities row, audit log, the works.
  2. INSERT INTO public.companies        (asyncpg, idempotent)
  3. INSERT INTO public.company_users    (asyncpg, idempotent)

Without step 3 the user can sign in but every API call hits 403 on
``require_company_access``.

Required env (loaded from your local ``.env``):
  HELIXIS_SUPABASE__URL           https://<project>.supabase.co
  HELIXIS_SUPABASE__SERVICE_KEY   service-role JWT (NOT the anon key)
  HELIXIS_SUPABASE__DB_URL        postgresql://… (for direct table writes)

Usage:
  python3 scripts/create_user.py \\
      --email you@example.com \\
      --password 'pick-something-strong' \\
      --company-id acme-realty \\
      --company-name 'Acme Realty' \\
      --role owner

The script is **idempotent** on every step — re-running with the same
inputs re-fetches the existing user / company / membership rather than
creating duplicates. Safe to run as part of a deploy bootstrap.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

import httpx

logger = logging.getLogger("create_user")


# ─────────────────────────────────────────────────────────────────────
# Supabase Auth Admin API
# ─────────────────────────────────────────────────────────────────────


async def _admin_create_or_get_user(
    *,
    supabase_url: str,
    service_key: str,
    email: str,
    password: str,
) -> str:
    """Create the user via Supabase Auth Admin API. If the email
    already exists, look up and return the existing user's id.

    Returns the user's UUID (string).
    """
    base = supabase_url.rstrip("/")
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        # Try to create. The Auth Admin API returns 200/201 on create,
        # 422 with a "User already registered" body when the email is
        # taken. We treat 422 as a signal to look up the existing user
        # rather than fail.
        resp = await client.post(
            f"{base}/auth/v1/admin/users",
            headers=headers,
            json={
                "email": email,
                "password": password,
                "email_confirm": True,
            },
        )
        if 200 <= resp.status_code < 300:
            data = resp.json()
            user_id = data.get("id") or data.get("user", {}).get("id")
            if not user_id:
                raise RuntimeError(f"Auth API returned 2xx but no user id: {data}")
            logger.info("created auth user: %s (%s)", email, user_id)
            return str(user_id)

        if resp.status_code == 422:
            # Email already registered — page through admin user list to
            # find them. The list endpoint accepts ``filter`` to scope
            # by email substring; we still scan the page since the API
            # doesn't guarantee exact-match here.
            logger.info("auth user already exists, fetching id: %s", email)
            list_resp = await client.get(
                f"{base}/auth/v1/admin/users",
                headers=headers,
                params={"per_page": 200, "filter": email},
            )
            list_resp.raise_for_status()
            for user in list_resp.json().get("users", []):
                if (user.get("email") or "").lower() == email.lower():
                    return str(user["id"])
            raise RuntimeError(f"Auth API said {email} exists but list endpoint can't find it")

        # Anything else is a real error.
        raise RuntimeError(f"Auth API error {resp.status_code}: {resp.text[:500]}")


# ─────────────────────────────────────────────────────────────────────
# Tenancy rows (companies + company_users)
# ─────────────────────────────────────────────────────────────────────


async def _ensure_company(conn: Any, *, company_id: str, company_name: str) -> None:
    await conn.execute(
        "INSERT INTO public.companies (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
        company_id,
        company_name,
    )


async def _ensure_membership(conn: Any, *, user_id: str, company_id: str, role: str) -> None:
    await conn.execute(
        "INSERT INTO public.company_users (user_id, company_id, role) "
        "VALUES ($1::uuid, $2, $3) "
        "ON CONFLICT (user_id, company_id) DO NOTHING",
        user_id,
        company_id,
        role,
    )


# ─────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────


async def create_user(
    *,
    email: str,
    password: str,
    company_id: str,
    company_name: str,
    role: str,
    supabase_url: str,
    service_key: str,
    db_url: str,
) -> str:
    """End-to-end flow. Returns the user's UUID."""
    import asyncpg

    user_id = await _admin_create_or_get_user(
        supabase_url=supabase_url,
        service_key=service_key,
        email=email,
        password=password,
    )

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await _ensure_company(conn, company_id=company_id, company_name=company_name)
                await _ensure_membership(
                    conn,
                    user_id=user_id,
                    company_id=company_id,
                    role=role,
                )
        logger.info(
            "membership ensured: user=%s company=%s role=%s",
            user_id,
            company_id,
            role,
        )
    finally:
        await pool.close()

    return user_id


def _require_env() -> tuple[str, str, str]:
    url = os.environ.get("HELIXIS_SUPABASE__URL", "").strip()
    key = os.environ.get("HELIXIS_SUPABASE__SERVICE_KEY", "").strip()
    db = os.environ.get("HELIXIS_SUPABASE__DB_URL", "").strip()
    missing = [
        name
        for name, val in [
            ("HELIXIS_SUPABASE__URL", url),
            ("HELIXIS_SUPABASE__SERVICE_KEY", key),
            ("HELIXIS_SUPABASE__DB_URL", db),
        ]
        if not val
    ]
    if missing:
        sys.stderr.write("create_user: missing env vars: " + ", ".join(missing) + "\n")
        sys.exit(2)
    if "placeholder.supabase.co" in url:
        sys.stderr.write(
            "create_user: HELIXIS_SUPABASE__URL is the placeholder. "
            "This script needs a real Supabase project URL.\n"
        )
        sys.exit(3)
    return url, key, db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--company-id", required=True)
    parser.add_argument(
        "--company-name",
        default=None,
        help="Display name for the company. Defaults to the company-id.",
    )
    parser.add_argument(
        "--role",
        default="owner",
        choices=["owner", "admin", "member"],
        help="Role to assign on company_users (default: owner).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    url, key, db = _require_env()

    company_name = args.company_name or args.company_id

    user_id = asyncio.run(
        create_user(
            email=args.email,
            password=args.password,
            company_id=args.company_id,
            company_name=company_name,
            role=args.role,
            supabase_url=url,
            service_key=key,
            db_url=db,
        )
    )

    print()
    print("✅ User ready to sign in.")
    print(f"   email     {args.email}")
    print(f"   user_id   {user_id}")
    print(f"   company   {args.company_id} ({args.role})")
    print()
    print("Sign in at the production URL with the email + password above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
