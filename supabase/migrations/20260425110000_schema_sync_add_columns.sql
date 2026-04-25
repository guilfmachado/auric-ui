-- Migração segura (produção): apenas ADD COLUMN IF NOT EXISTS
-- Execute no SQL Editor do Supabase.

alter table if exists public.wallet_status
  add column if not exists usdt_balance double precision,
  add column if not exists usdc_balance double precision,
  add column if not exists entry_price double precision,
  add column if not exists posicao_aberta boolean default false,
  add column if not exists whale_flow_score double precision,
  add column if not exists social_sentiment_score double precision,
  add column if not exists news_sentiment_score integer,
  add column if not exists forecast_preco_alvo double precision,
  add column if not exists forecast_tendencia_alta boolean,
  add column if not exists llava_veto boolean,
  add column if not exists funnel_stage text,
  add column if not exists funnel_abort_reason text,
  add column if not exists ml_prob_base double precision,
  add column if not exists ml_prob_calibrated double precision,
  add column if not exists updated_at timestamptz default now();

alter table if exists public.logs
  add column if not exists par_moeda text,
  add column if not exists preco_atual double precision,
  add column if not exists probabilidade_ml double precision,
  add column if not exists sentimento_ia text,
  add column if not exists veredito_ia text,
  add column if not exists acao_tomada text,
  add column if not exists justificativa text,
  add column if not exists contexto_raw text,
  add column if not exists justificativa_ia text,
  add column if not exists noticias_agregadas text,
  add column if not exists dist_ema200_pct double precision,
  add column if not exists spread_atual double precision,
  add column if not exists book_imbalance double precision,
  add column if not exists hora_do_dia integer,
  add column if not exists atr_14 double precision,
  add column if not exists funding_rate double precision,
  add column if not exists long_short_ratio double precision,
  add column if not exists whale_flow_score double precision,
  add column if not exists social_sentiment_score double precision,
  add column if not exists funnel_stage text,
  add column if not exists funnel_abort_reason text,
  add column if not exists ml_prob_base double precision,
  add column if not exists ml_prob_calibrated double precision,
  add column if not exists llava_veto boolean,
  add column if not exists commission double precision,
  add column if not exists is_maker boolean,
  add column if not exists rsi_14 double precision,
  add column if not exists adx_14 double precision;

alter table if exists public.trade_logs
  add column if not exists par_moeda text,
  add column if not exists symbol text,
  add column if not exists side text,
  add column if not exists amount double precision,
  add column if not exists price double precision,
  add column if not exists order_id text,
  add column if not exists status text,
  add column if not exists decision_id uuid,
  add column if not exists raw_exchange jsonb,
  add column if not exists mode text,
  add column if not exists contexto_raw text,
  add column if not exists qty_left double precision,
  add column if not exists partial_roi double precision,
  add column if not exists final_roi double precision,
  add column if not exists exit_type text,
  add column if not exists whale_flow_score double precision,
  add column if not exists social_sentiment_score double precision,
  add column if not exists alpha_attribution jsonb;

alter table if exists public.config
  add column if not exists modo_operacao text,
  add column if not exists balance_usdt numeric,
  add column if not exists pnl_day_pct numeric,
  add column if not exists trades_24h integer,
  add column if not exists xgboost_accuracy numeric,
  add column if not exists ml_probability numeric,
  add column if not exists verdict_ia text,
  add column if not exists justificativa_curta text,
  add column if not exists updated_at timestamptz;

alter table if exists public.bot_control
  add column if not exists is_active boolean default true,
  add column if not exists updated_at timestamptz;

alter table if exists public.bot_config
  add column if not exists leverage double precision,
  add column if not exists risk_fraction double precision,
  add column if not exists trailing_callback_rate double precision,
  add column if not exists trailing_rate double precision,
  add column if not exists updated_at timestamptz;

alter table if exists public.manual_commands
  add column if not exists command text,
  add column if not exists executed boolean default false,
  add column if not exists status text,
  add column if not exists last_error text,
  add column if not exists created_at timestamptz default now(),
  add column if not exists updated_at timestamptz;

alter table if exists public.trade_outcomes
  add column if not exists order_id text,
  add column if not exists symbol text,
  add column if not exists side text,
  add column if not exists ml_probability_at_entry double precision,
  add column if not exists claude_justification text,
  add column if not exists pnl_usdc double precision,
  add column if not exists pnl_realized double precision,
  add column if not exists roi_pct double precision,
  add column if not exists final_roi double precision,
  add column if not exists motivo_fecho text,
  add column if not exists exit_type text,
  add column if not exists closed_at timestamptz;
