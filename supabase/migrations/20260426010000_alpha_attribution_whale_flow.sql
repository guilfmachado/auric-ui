-- Alpha Attribution + Whale Radar
-- 1) trades.alpha_attribution (jsonb)
-- 2) wallet_status.whale_flow_score (numeric)

alter table if exists public.trades
  add column if not exists alpha_attribution jsonb;

alter table if exists public.wallet_status
  add column if not exists whale_flow_score double precision;
