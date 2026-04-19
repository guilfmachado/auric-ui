-- Veredito dedicado ao dashboard + Realtime em `logs` (lista / último log).
do $$
begin
  if to_regclass('public.logs') is not null then
    execute 'alter table public.logs add column if not exists veredito_ia text';
    comment on column public.logs.veredito_ia is
      'Veredito textual Brain (ex.: BULLISH, BEARISH, VETO) no momento do log.';
  end if;
end $$;

do $$
begin
  if not exists (
    select 1
    from pg_publication_tables
    where pubname = 'supabase_realtime'
      and schemaname = 'public'
      and tablename = 'logs'
  ) then
    execute 'alter publication supabase_realtime add table public.logs';
  end if;
end $$;
