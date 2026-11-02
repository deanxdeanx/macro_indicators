-- Analysis tab accounts. Run this in the Supabase SQL editor.
-- New users receive a 7-day trial and 3 credits. There is no payment flow.
-- A future billing webhook can set tier to 'pro' and add credits.

create table if not exists public.accounts (
  user_id uuid primary key references auth.users (id) on delete cascade,
  tier text not null default 'trial' check (tier in ('trial', 'pro')),
  credits integer not null default 3 check (credits >= 0),
  trial_ends_at timestamptz not null default (now() + interval '7 days'),
  created_at timestamptz not null default now()
);

create table if not exists public.usage_log (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  created_at timestamptz not null default now(),
  ticker text,
  credits_charged integer not null,
  estimated_cost_usd numeric,
  actual_cost_usd numeric,
  models text
);

alter table public.accounts enable row level security;
alter table public.usage_log enable row level security;

create policy "read own account"
  on public.accounts
  for select
  to authenticated
  using (auth.uid() = user_id);

create policy "read own usage"
  on public.usage_log
  for select
  to authenticated
  using (auth.uid() = user_id);

-- Inserts and credit changes go through spend_credits, which only the
-- service role can execute. Authenticated clients cannot write either table.

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.accounts (user_id)
  values (new.id)
  on conflict (user_id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

create or replace function public.spend_credits(
  p_user uuid,
  p_cost integer,
  p_ticker text,
  p_estimated_cost numeric,
  p_actual_cost numeric,
  p_models text
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  acct public.accounts%rowtype;
  remaining integer;
begin
  if p_cost < 1 then
    raise exception 'invalid_cost';
  end if;
  select * into acct from public.accounts where user_id = p_user for update;
  if not found then
    raise exception 'account_missing';
  end if;
  if acct.tier = 'trial' and acct.trial_ends_at <= now() then
    raise exception 'trial_expired';
  end if;
  if acct.credits < p_cost then
    raise exception 'insufficient_credits';
  end if;
  update public.accounts
    set credits = credits - p_cost
    where user_id = p_user
    returning credits into remaining;
  insert into public.usage_log (
    user_id, ticker, credits_charged, estimated_cost_usd, actual_cost_usd, models
  ) values (
    p_user, p_ticker, p_cost, p_estimated_cost, p_actual_cost, p_models
  );
  return remaining;
end;
$$;

revoke all on function public.spend_credits(uuid, integer, text, numeric, numeric, text) from public;
revoke all on function public.spend_credits(uuid, integer, text, numeric, numeric, text) from anon, authenticated;
grant execute on function public.spend_credits(uuid, integer, text, numeric, numeric, text) to service_role;
