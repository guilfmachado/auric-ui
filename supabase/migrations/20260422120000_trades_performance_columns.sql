-- Performance da trade (híbrido + fecho): idempotente se já existirem no projeto remoto.
alter table public.trades
  add column if not exists partial_roi double precision,
  add column if not exists final_roi double precision,
  add column if not exists exit_type text;
