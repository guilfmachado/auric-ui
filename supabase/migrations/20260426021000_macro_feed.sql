-- Tabela para o componente MacroRadar (última análise + Realtime INSERT).

create table if not exists public.macro_feed (
  id uuid primary key default gen_random_uuid(),
  macro_score numeric not null default 50,
  market_vibe text,
  bullet_points jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);

comment on table public.macro_feed is
  'Snapshots da análise macro (ex.: DeepSeek) para o radar do dashboard.';

create index if not exists macro_feed_created_at_desc
  on public.macro_feed (created_at desc);

alter table public.macro_feed enable row level security;

drop policy if exists "macro_feed_select_anon" on public.macro_feed;
create policy "macro_feed_select_anon"
  on public.macro_feed
  for select
  to anon, authenticated
  using (true);

do $$
begin
  if not exists (
    select 1
    from pg_publication_tables
    where pubname = 'supabase_realtime'
      and schemaname = 'public'
      and tablename = 'macro_feed'
  ) then
    execute 'alter publication supabase_realtime add table public.macro_feed';
  end if;
end $$;
