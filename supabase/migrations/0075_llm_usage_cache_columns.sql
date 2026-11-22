-- 0075: split the LLM ledger into its billable components.
--
-- 0050 recorded one number per (company, day): total_tokens. That made our
-- largest implemented cost lever — prompt caching — invisible: we could not
-- tell whether the ~13k of tool schema on every completion was being cached
-- or re-billed in full.
--
-- It also made the daily budget WRONG. `budget_exceeded` gates on `tokens`,
-- which counts a cached read at full weight; Anthropic bills it at 10% and a
-- cache write at 125%. On cache-heavy traffic the ledger over-states real
-- spend by close to an order of magnitude, so the company whose cache works
-- BEST hits its daily 429 first.
--
-- LiteLLM already carries all of this on usage.prompt_tokens_details; we were
-- discarding it. `effective_tokens` is the billable-weight figure the gate
-- now reads. `tokens` stays as the raw total so nothing reading it breaks.
--
-- Anthropic's own input_tokens EXCLUDES the cache components and LiteLLM adds
-- them in (AnthropicConfig: `prompt_tokens += cache_creation_input_tokens`,
-- `prompt_tokens += cache_read_input_tokens`), so cached_tokens and
-- cache_write_tokens are SUBSETS of prompt_tokens. The weighting depends on
-- that and it is verified against the transform, not assumed.

alter table public.helixis_llm_usage
    add column if not exists prompt_tokens     bigint not null default 0,
    add column if not exists completion_tokens bigint not null default 0,
    add column if not exists cached_tokens     bigint not null default 0,
    add column if not exists cache_write_tokens bigint not null default 0,
    add column if not exists effective_tokens  bigint not null default 0;

comment on column public.helixis_llm_usage.cached_tokens is
    'Cache READ tokens (billed ~10%). Subset of prompt_tokens.';
comment on column public.helixis_llm_usage.cache_write_tokens is
    'Cache CREATION tokens (billed ~125%). Subset of prompt_tokens.';
comment on column public.helixis_llm_usage.effective_tokens is
    'Billable-weight tokens: uncached + 0.1*cached + 1.25*cache_write + completion. '
    'This is what the daily budget gate reads; `tokens` remains the raw total.';

-- Existing rows predate the split: their `tokens` is the only figure we have,
-- so seed `effective_tokens` from it. That is an OVER-estimate for any day
-- that was actually cached well, which is the safe direction for a budget
-- gate — it can only be stricter than reality, never looser.
update public.helixis_llm_usage
   set effective_tokens = tokens
 where effective_tokens = 0
   and tokens > 0;
