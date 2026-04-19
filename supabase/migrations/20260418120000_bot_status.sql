-- Master switch do bot (lido no main.py a cada ciclo).
-- Execute no SQL Editor do Supabase ou via CLI migrate.

create table if not exists public.bot_status (
  id smallint primary key default 1 check (id = 1),
  is_active boolean not null default true,
  updated_at timestamptz default now()
);

comment on table public.bot_status is 'MASTER SWITCH: is_active=false pausa o maestro (Python) no próximo ciclo.';

insert into public.bot_status (id, is_active)
values (1, true)
on conflict (id) do nothing;

alter table public.bot_status enable row level security;

-- Dashboard (anon key) pode ler/atualizar a linha única
create policy "bot_status_select_anon"
  on public.bot_status for select
  to anon, authenticated
  using (true);

create policy "bot_status_update_anon"
  on public.bot_status for update
  to anon, authenticated
  using (true)
  with check (true);

create policy "bot_status_insert_anon"
  on public.bot_status for insert
  to anon, authenticated
  with check (true);

-- Realtime (Dashboard): em Supabase → Database → Publications → supabase_realtime → marque bot_status
-- ou: alter publication supabase_realtime add table public.bot_status;
