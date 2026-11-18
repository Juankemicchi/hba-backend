-- HBA Betting — Supabase schema for accounts + VIP access
-- Apply in the Supabase SQL editor once the project exists.
-- The backend activates automatically when SUPABASE_URL and
-- SUPABASE_ANON_KEY / SUPABASE_JWT_SECRET (or SUPABASE_SERVICE_ROLE_KEY)
-- are set in Render. Missing vars do not break the public site.

create table if not exists public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  email text,
  display_name text,
  is_vip boolean not null default false,
  vip_since timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists profiles_email_idx on public.profiles (email);
create index if not exists profiles_is_vip_idx on public.profiles (is_vip);

alter table public.profiles enable row level security;

drop policy if exists "profiles_select_own" on public.profiles;
create policy "profiles_select_own"
  on public.profiles for select
  to authenticated
  using (auth.uid() = id);

drop policy if exists "profiles_update_own_profile" on public.profiles;
create policy "profiles_update_own_profile"
  on public.profiles for update
  to authenticated
  using (auth.uid() = id)
  with check (
    auth.uid() = id
    and is_vip is not distinct from (select p.is_vip from public.profiles p where p.id = auth.uid())
  );

-- Users must not be able to flip is_vip themselves. Admin / payment webhooks
-- use the service role (bypasses RLS) via POST /api/admin/vip.

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, email, display_name)
  values (
    new.id,
    new.email,
    coalesce(new.raw_user_meta_data->>'full_name', new.raw_user_meta_data->>'name')
  )
  on conflict (id) do update
    set email = excluded.email,
        updated_at = now();
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- Optional: record payment events later (Mercado Pago / Wompi). Unused until
-- a payment platform is connected.
create table if not exists public.vip_payments (
  id bigserial primary key,
  profile_id uuid references public.profiles (id) on delete set null,
  provider text not null,
  provider_ref text,
  status text,
  payload jsonb,
  created_at timestamptz not null default now()
);

alter table public.vip_payments enable row level security;
