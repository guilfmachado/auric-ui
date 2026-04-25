-- Ajuste canónico: alpha attribution em public.trade_logs (não em trades).

alter table if exists public.trade_logs
  add column if not exists alpha_attribution jsonb;
