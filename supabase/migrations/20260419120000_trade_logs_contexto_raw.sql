-- Coluna `contexto_raw` na tabela de logs (nome canónico: `logs`; legado: `trade_logs`).
do $$
begin
  if to_regclass('public.logs') is not null then
    execute 'alter table public.logs add column if not exists contexto_raw text';
    comment on column public.logs.contexto_raw is
      'JSON de indicadores TA + texto do Intelligence Hub (formatar_log_contexto_raw) no momento do log.';
  elsif to_regclass('public.trade_logs') is not null then
    execute 'alter table public.trade_logs add column if not exists contexto_raw text';
    comment on column public.trade_logs.contexto_raw is
      'JSON de indicadores TA + texto do Intelligence Hub (formatar_log_contexto_raw) no momento do log.';
  end if;
end $$;
