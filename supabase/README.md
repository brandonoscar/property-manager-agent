# Supabase setup

Helixis uses Supabase for auth, multi-tenancy, and as the Postgres backend for
Mem0's pgvector store.

## Apply migrations

Against a hosted Supabase project:

```bash
psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0001_init.sql
```

Against the local `docker compose --profile dev up postgres`:

```bash
psql postgresql://helixis:helixis_dev@localhost:5432/helixis \
  -f supabase/migrations/0001_init.sql
```

The migration:

1. Enables `pgvector` (required by Mem0)
2. Creates `companies` and `company_users` tables that back
   `AuthenticatedUser.company_ids`
3. Adds RLS policies so users only see their own company memberships

## Seeding a test company

```sql
insert into public.companies (id, name)
     values ('acme-realty', 'Acme Realty');

insert into public.company_users (user_id, company_id, role)
     values ('<supabase-user-uuid>', 'acme-realty', 'admin');
```

After seeding, the JWT for that user will resolve to `company_ids=['acme-realty']`
inside the Helixis backend.
