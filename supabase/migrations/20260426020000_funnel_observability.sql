-- Observabilidade do funil (dashboard + logs)

alter table if exists public.wallet_status
  add column if not exists news_sentiment_score integer,
  add column if not exists forecast_preco_alvo double precision,
  add column if not exists forecast_tendencia_alta boolean,
  add column if not exists llava_veto boolean,
  add column if not exists funnel_stage text,
  add column if not exists funnel_abort_reason text,
  add column if not exists ml_prob_base double precision,
  add column if not exists ml_prob_calibrated double precision;

alter table if exists public.logs
  add column if not exists funnel_stage text,
  add column if not exists funnel_abort_reason text,
  add column if not exists ml_prob_base double precision,
  add column if not exists ml_prob_calibrated double precision,
  add column if not exists llava_veto boolean;
