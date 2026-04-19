-- Colunas dedicadas ao dashboard (justificativa IA + feed de notícias).
do $$
begin
  if to_regclass('public.logs') is not null then
    execute 'alter table public.logs add column if not exists justificativa_ia text';
    execute 'alter table public.logs add column if not exists noticias_agregadas text';
    comment on column public.logs.justificativa_ia is
      'Texto curto do veredito Brain (justificativa_curta) no momento do log.';
    comment on column public.logs.noticias_agregadas is
      'Snapshot do Intelligence Hub (contexto agregado) no momento do log.';
  end if;
end $$;

-- Realtime para saldo USDT (carteira) — ignora se já estiver na publicação.
do $$
begin
  if not exists (
    select 1
    from pg_publication_tables
    where pubname = 'supabase_realtime'
      and schemaname = 'public'
      and tablename = 'wallet_status'
  ) then
    execute 'alter publication supabase_realtime add table public.wallet_status';
  end if;
end $$;
