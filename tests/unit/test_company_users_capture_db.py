"""The capture through PostgREST, played on a throwaway Postgres before and after 0112.

``test_invites_need_acceptance`` reads the migrations as text. This runs them: a
Postgres 16 with Supabase's two API roles, ``auth.uid()`` reading the request's JWT
claims as PostgREST sets them, and ``company_users`` with every policy the migrations
ever created or dropped on it, in their order, from the files themselves, plus the RLS
helper (0052) and the sign-up trigger (0034). Then, as a signed-in admin of their own
company (everyone is one):

* **before 0112**, inserting a stranger into their company works (the control: the
  rebuilt state really has the hole), and so does re-pointing their own row at the
  stranger;
* **after 0112**, applied from the file verbatim, both are refused, so is a delete,
  and the invites table reads as nothing to them; the web app's read of its own
  companies still works, and so does the sign-up trigger.

It also runs ``helixis.db.repositories.member_invites`` against the real SQL: a pending
invite grants nothing through the RLS helper, accepting is the invitee's alone and
writes their membership once, and before 0112 the repository fails closed.

Needs ``HELIXIS_THROWAWAY_PG_URL``: a superuser URL to a Postgres 16 it may create and
drop databases on (CI's backend job runs one; locally ``docker run -p 5432:5432 -e
POSTGRES_HOST_AUTH_METHOD=trust postgres:16``). Missing in CI it FAILS rather than
skips, so the job can't go green without running it.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from helixis.db.repositories import member_invites

URL = os.environ.get("HELIXIS_THROWAWAY_PG_URL", "")
if not URL and os.environ.get("GITHUB_ACTIONS"):
    pytest.fail(
        "HELIXIS_THROWAWAY_PG_URL is not set in CI: the backend job's Postgres service "
        "is what this test runs on.",
        pytrace=False,
    )
pytestmark = pytest.mark.skipif(not URL, reason="HELIXIS_THROWAWAY_PG_URL not set")

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
MIGRATION_0112 = MIGRATIONS / "0112_company_invites_need_acceptance.sql"

ATTACKER = "aaaaaaaa-0000-4000-8000-000000000001"
VICTIM = "aaaaaaaa-0000-4000-8000-000000000002"
NEWCOMER = "aaaaaaaa-0000-4000-8000-000000000003"

#: What Supabase gives a project before any migration: its API roles, a schema of
#: its own for auth, and every privilege on a new public table to both roles.
_PLATFORM = """
do $$ begin
    if not exists (select from pg_roles where rolname = 'anon') then
        create role anon nologin;
    end if;
    if not exists (select from pg_roles where rolname = 'authenticated') then
        create role authenticated nologin;
    end if;
end $$;
create schema auth;
create table auth.users (id uuid primary key, email text, raw_user_meta_data jsonb);
create function auth.uid() returns uuid language sql stable as $$
    select nullif(current_setting('request.jwt.claims', true)::jsonb ->> 'sub', '')::uuid
$$;
grant usage on schema auth to anon, authenticated;
grant execute on function auth.uid() to anon, authenticated;
grant usage on schema public to anon, authenticated;
create table public.companies (id uuid primary key, name text not null);
-- The live shape (information_schema, helixis-test, 2026-11-11).
create table public.company_users (
    id uuid primary key default gen_random_uuid(),
    company_id uuid not null references public.companies (id) on delete cascade,
    user_id uuid not null references auth.users (id) on delete cascade,
    role text not null default 'member' check (role in ('admin', 'manager', 'member')),
    created_at timestamptz not null default now(),
    unique (company_id, user_id)
);
alter table public.company_users enable row level security;
grant all on table public.companies, public.company_users to anon, authenticated;
"""

_FUNCTION_BODY = re.compile(
    r"create (?:or replace )?function.*?\$(\w*)\$.*?\$\1\$\s*;", re.I | re.S
)


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _function(file: str, name: str) -> str:
    """The ``create or replace function public.<name>`` statement in a migration, whole."""
    text = _strip_comments((MIGRATIONS / file).read_text(encoding="utf-8"))
    for m in _FUNCTION_BODY.finditer(text):
        if re.match(rf"create (?:or replace )?function\s+public\.{name}\s*\(", m[0], re.I):
            return m[0]
    raise AssertionError(f"{file} defines no {name}")


def _membership_policies_before_0112() -> list[str]:
    """Every drop/create policy on company_users, in migration order, up to 0111."""
    out: list[str] = []
    for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        if path.name >= "0112":
            break
        text = _FUNCTION_BODY.sub(" ", _strip_comments(path.read_text(encoding="utf-8")))
        for stmt in text.split(";"):
            flat = " ".join(stmt.split())
            if re.match(r"^(create|drop) policy .+? on (public\.)?company_users\b", flat, re.I):
                out.append(flat)
    return out


async def _seed(conn: asyncpg.Connection) -> None:
    for user_id, email in ((ATTACKER, "attacker@example.com"), (VICTIM, "victim@example.com")):
        # The sign-up trigger gives each their own company, as its admin (0034).
        await conn.execute(
            "insert into auth.users (id, email) values ($1::uuid, $2)", user_id, email
        )


@asynccontextmanager
async def _database(*, apply_0112: bool) -> AsyncIterator[str]:
    """A new database in the throwaway cluster, as the live project stood before 0112
    (and after it, when asked). Dropped afterwards."""
    name = f"capture_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(URL)
    try:
        await admin.execute(f'create database "{name}"')
    finally:
        await admin.close()
    db_url = urlunsplit(urlsplit(URL)._replace(path=f"/{name}"))
    try:
        conn = await asyncpg.connect(db_url)
        try:
            await conn.execute(_PLATFORM)
            await conn.execute(
                _function("0052_clamp_rls_helper_to_caller.sql", "user_company_ids_with_role")
            )
            await conn.execute(_function("0034_reconcile_team_roles.sql", "handle_new_user"))
            await conn.execute(
                "create trigger on_auth_user_created after insert on auth.users "
                "for each row execute function public.handle_new_user()"
            )
            policies = _membership_policies_before_0112()
            assert any("admins can add company members" in p for p in policies)
            for stmt in policies:
                await conn.execute(stmt)
            await _seed(conn)
            if apply_0112:
                await conn.execute(MIGRATION_0112.read_text(encoding="utf-8"))
        finally:
            await conn.close()
        yield db_url
    finally:
        admin = await asyncpg.connect(URL)
        try:
            await admin.execute(f'drop database if exists "{name}" with (force)')
        finally:
            await admin.close()


async def _as(conn: asyncpg.Connection, role: str, sub: str | None) -> None:
    """Act as PostgREST does for a request: its role, and the token's claims."""
    await conn.execute(f"set local role {role}")
    claims = json.dumps({"sub": sub, "role": role} if sub else {"role": role})
    await conn.execute("select set_config('request.jwt.claims', $1, true)", claims)


_CAPTURE = (
    "insert into public.company_users (user_id, company_id, role)"
    " values ($1::uuid, $2::uuid, 'member')"
)
_REPOINT = (
    "update public.company_users set user_id = $1::uuid"
    " where company_id = $2::uuid and user_id = $2::uuid"
)


async def test_before_0112_a_signed_in_admin_could_add_a_stranger() -> None:
    """The control: the rebuilt state reproduces the hole the audit found."""
    async with _database(apply_0112=False) as db:
        conn = await asyncpg.connect(db)
        try:
            tx = conn.transaction()
            await tx.start()
            await _as(conn, "authenticated", ATTACKER)
            assert await conn.execute(_CAPTURE, VICTIM, ATTACKER) == "INSERT 0 1"
            await tx.rollback()

            tx = conn.transaction()
            await tx.start()
            await _as(conn, "authenticated", ATTACKER)
            assert await conn.execute(_REPOINT, VICTIM, ATTACKER) == "UPDATE 1"
            await tx.rollback()
        finally:
            await conn.close()


@pytest.mark.parametrize(
    ("sql", "args"),
    [
        (_CAPTURE, (VICTIM, ATTACKER)),
        (_REPOINT, (VICTIM, ATTACKER)),
        ("delete from public.company_users where company_id = $1::uuid", (ATTACKER,)),
    ],
    ids=["insert", "update", "delete"],
)
async def test_after_0112_the_policy_refuses_every_write(sql: str, args: tuple[str, ...]) -> None:
    async with _database(apply_0112=True) as db:
        conn = await asyncpg.connect(db)
        try:
            async with conn.transaction():
                await _as(conn, "authenticated", ATTACKER)
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.execute(sql, *args)
            # As the backend (the table's owner), the victim is still only in their own.
            rows = await conn.fetch(
                "select company_id::text from public.company_users where user_id = $1::uuid",
                VICTIM,
            )
            assert [r["company_id"] for r in rows] == [VICTIM]
        finally:
            await conn.close()


async def test_after_0112_the_web_app_still_reads_its_own_companies() -> None:
    async with _database(apply_0112=True) as db:
        conn = await asyncpg.connect(db)
        try:
            async with conn.transaction():
                await _as(conn, "authenticated", ATTACKER)
                rows = await conn.fetch("select company_id::text from public.company_users")
                assert [r["company_id"] for r in rows] == [ATTACKER]
            async with conn.transaction():
                await _as(conn, "anon", None)
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.fetch("select 1 from public.company_users")
        finally:
            await conn.close()


async def test_after_0112_the_invites_table_is_the_backends_alone() -> None:
    async with _database(apply_0112=True) as db:
        conn = await asyncpg.connect(db)
        try:
            for role, sub in (("authenticated", VICTIM), ("anon", None)):
                async with conn.transaction():
                    await _as(conn, role, sub)
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        await conn.fetch("select 1 from public.company_member_invites")
            async with conn.transaction():
                await _as(conn, "authenticated", ATTACKER)
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.execute(
                        "insert into public.company_member_invites (company_id, user_id, role)"
                        " values ($1::uuid, $2::uuid, 'member')",
                        ATTACKER,
                        VICTIM,
                    )
        finally:
            await conn.close()


async def test_after_0112_sign_up_still_makes_a_company() -> None:
    """handle_new_user is SECURITY DEFINER, so the revokes don't reach it."""
    async with _database(apply_0112=True) as db:
        conn = await asyncpg.connect(db)
        try:
            await conn.execute(
                "insert into auth.users (id, email) values ($1::uuid, 'new@example.com')", NEWCOMER
            )
            row = await conn.fetchrow(
                "select company_id::text, role from public.company_users where user_id = $1::uuid",
                NEWCOMER,
            )
            assert dict(row) == {"company_id": NEWCOMER, "role": "admin"}
        finally:
            await conn.close()


async def test_0112_applies_twice() -> None:
    async with _database(apply_0112=True) as db:
        conn = await asyncpg.connect(db)
        try:
            await conn.execute(MIGRATION_0112.read_text(encoding="utf-8"))
        finally:
            await conn.close()


# ── The repository, against the real SQL ─────────────────────────────────────


async def _companies_of(conn: asyncpg.Connection, user_id: str) -> list[str]:
    """What the RLS helper grants ``user_id``, as their own token sees it."""
    async with conn.transaction():
        await _as(conn, "authenticated", user_id)
        rows = await conn.fetch("select c::text from public.user_company_ids_with_role() c")
    return sorted(r["c"] for r in rows)


async def test_a_pending_invite_grants_nothing_until_accepted() -> None:
    async with _database(apply_0112=True) as db:
        pool = await asyncpg.create_pool(db, min_size=1, max_size=2)
        assert pool is not None
        try:
            await member_invites.upsert(
                pool, company_id=ATTACKER, user_id=VICTIM, role="admin", invited_by=ATTACKER
            )
            async with pool.acquire() as conn:
                assert await _companies_of(conn, VICTIM) == [VICTIM]

            waiting = await member_invites.for_invitee(pool, user_id=VICTIM)
            assert [(i.company_id, i.role, i.invited_by_email) for i in waiting] == [
                (ATTACKER, "admin", "attacker@example.com")
            ]
            assert [
                i.email for i in await member_invites.for_company(pool, company_id=ATTACKER)
            ] == ["victim@example.com"]

            # Nobody but the invitee accepts it.
            assert (
                await member_invites.accept(pool, invite_id=waiting[0].id, user_id=ATTACKER) is None
            )
            async with pool.acquire() as conn:
                assert await _companies_of(conn, VICTIM) == [VICTIM]

            accepted = await member_invites.accept(pool, invite_id=waiting[0].id, user_id=VICTIM)
            assert accepted is not None
            assert (accepted.company_id, accepted.role) == (ATTACKER, "admin")
            async with pool.acquire() as conn:
                assert await _companies_of(conn, VICTIM) == sorted([ATTACKER, VICTIM])
            # Used up: a second accept finds nothing.
            assert (
                await member_invites.accept(pool, invite_id=waiting[0].id, user_id=VICTIM) is None
            )
            assert await member_invites.for_invitee(pool, user_id=VICTIM) == []
        finally:
            await pool.close()


async def test_accepting_while_in_another_company_is_refused_and_keeps_the_invite() -> None:
    async with _database(apply_0112=True) as db:
        pool = await asyncpg.create_pool(db, min_size=1, max_size=2)
        assert pool is not None
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "insert into auth.users (id, email) values ($1::uuid, 'third@example.com')",
                    NEWCOMER,
                )
                # The victim already works in the newcomer's company.
                await conn.execute(
                    "insert into public.company_users (user_id, company_id, role)"
                    " values ($1::uuid, $2::uuid, 'member')",
                    VICTIM,
                    NEWCOMER,
                )
            await member_invites.upsert(
                pool, company_id=ATTACKER, user_id=VICTIM, role="member", invited_by=ATTACKER
            )
            (invite,) = await member_invites.for_invitee(pool, user_id=VICTIM)
            with pytest.raises(member_invites.AcceptRefusedError):
                await member_invites.accept(pool, invite_id=invite.id, user_id=VICTIM)
            assert [i.id for i in await member_invites.for_invitee(pool, user_id=VICTIM)] == [
                invite.id
            ]
            async with pool.acquire() as conn:
                assert ATTACKER not in await _companies_of(conn, VICTIM)
        finally:
            await pool.close()


async def test_decline_withdraw_and_expiry() -> None:
    async with _database(apply_0112=True) as db:
        pool = await asyncpg.create_pool(db, min_size=1, max_size=2)
        assert pool is not None
        try:
            await member_invites.upsert(
                pool, company_id=ATTACKER, user_id=VICTIM, role="member", invited_by=ATTACKER
            )
            (invite,) = await member_invites.for_invitee(pool, user_id=VICTIM)
            assert await member_invites.decline(pool, invite_id=invite.id, user_id=ATTACKER) is None
            assert (
                await member_invites.decline(pool, invite_id=invite.id, user_id=VICTIM) == ATTACKER
            )

            await member_invites.upsert(
                pool, company_id=ATTACKER, user_id=VICTIM, role="member", invited_by=ATTACKER
            )
            (invite,) = await member_invites.for_invitee(pool, user_id=VICTIM)
            assert (
                await member_invites.withdraw(pool, invite_id=invite.id, company_id=VICTIM) is None
            )
            assert (
                await member_invites.withdraw(pool, invite_id=invite.id, company_id=ATTACKER)
                == VICTIM
            )

            await member_invites.upsert(
                pool, company_id=ATTACKER, user_id=VICTIM, role="member", invited_by=ATTACKER
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    "update public.company_member_invites"
                    " set expires_at = now() - interval '1 second'"
                )
            assert await member_invites.for_invitee(pool, user_id=VICTIM) == []
            (expired,) = await pool.fetch("select id::text from public.company_member_invites")
            assert (
                await member_invites.accept(pool, invite_id=expired["id"], user_id=VICTIM) is None
            )
            # A new invite starts the clock again.
            await member_invites.upsert(
                pool, company_id=ATTACKER, user_id=VICTIM, role="manager", invited_by=ATTACKER
            )
            assert [i.role for i in await member_invites.for_invitee(pool, user_id=VICTIM)] == [
                "manager"
            ]
        finally:
            await pool.close()


async def test_before_0112_the_repository_fails_closed() -> None:
    async with _database(apply_0112=False) as db:
        pool = await asyncpg.create_pool(db, min_size=1, max_size=2)
        assert pool is not None
        try:
            with pytest.raises(member_invites.InvitesUnavailableError):
                await member_invites.upsert(
                    pool, company_id=ATTACKER, user_id=VICTIM, role="member", invited_by=ATTACKER
                )
            assert await member_invites.for_invitee(pool, user_id=VICTIM) == []
            assert await member_invites.for_company(pool, company_id=ATTACKER) == []
            missing = str(uuid.uuid4())
            assert await member_invites.accept(pool, invite_id=missing, user_id=VICTIM) is None
            assert await member_invites.decline(pool, invite_id=missing, user_id=VICTIM) is None
            assert (
                await member_invites.withdraw(pool, invite_id=missing, company_id=ATTACKER) is None
            )
            async with pool.acquire() as conn:
                assert await _companies_of(conn, VICTIM) == [VICTIM]
        finally:
            await pool.close()
