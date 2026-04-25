-- Market mood scores para dashboard e trade logs

alter table if exists public.wallet_status
  add column if not exists social_sentiment_score double precision;

alter table if exists public.trade_logs
  add column if not exists whale_flow_score double precision,
  add column if not exists social_sentiment_score double precision;
