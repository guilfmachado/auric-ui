-- Alinha nomes ao banco atual: `logs`, `wallet_status.usdt_balance`, leitura em `bot_control`.
-- Idempotente: só renomeia se o objeto antigo existir e o novo ainda não existir.

-- trade_logs → logs
do $$
begin
  if to_regclass('public.trade_logs') is not null and to_regclass('public.logs') is null then
    alter table public.trade_logs rename to logs;
  end if;
end $$;

-- contexto_raw em logs (se vier de migração antiga com trade_logs)
alter table public.logs add column if not exists contexto_raw text;

-- wallet_status: usdt_futures → usdt_balance
do $$
begin
  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'wallet_status' and column_name = 'usdt_futures'
  ) and not exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'wallet_status' and column_name = 'usdt_balance'
  ) then
    alter table public.wallet_status rename column usdt_futures to usdt_balance;
  end if;
end $$;

comment on column public.logs.contexto_raw is 'JSON de indicadores TA + texto do Intelligence Hub (formatar_log_contexto_raw) no momento do log.';
