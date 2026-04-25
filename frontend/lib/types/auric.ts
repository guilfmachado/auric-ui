/** Linha única de configuração do bot (id = 1). */
export type TradingMode = "SPOT" | "FUTURES";

export interface ConfigRow {
  id: number;
  trading_mode: TradingMode;
  /** Coluna usada pelo bot Python (`obter_modo_operacao`). */
  modo_operacao?: TradingMode;
  balance_usdt: number | null;
  balance_usdc?: number | null;
  pnl_day_pct: number | null;
  trades_24h: number | null;
  xgboost_accuracy: number | null;
  ml_probability: number | null;
  verdict_ia: string | null;
  justificativa_curta: string | null;
  updated_at: string | null;
}

/** Tabela `logs` (alinhada ao logger Python `TABELA_LOGS` / `registrar_log_trade`). */
export interface LogRow {
  id: number;
  created_at?: string;
  /** Par negociado (ex.: ETH/USDC) — coluna canónica no Supabase. */
  par_moeda?: string;
  /** Legado ou flag booleana na base; o dashboard usa `par_moeda` para exibir o par. */
  ativo?: string | boolean;
  preco_atual?: number | null;
  /** Se existir na base (entrada da posição); senão usar `preco_atual` para referência no UI. */
  preco_entrada?: number | null;
  probabilidade_ml: number;
  /** Indicadores persistidos em colunas (opcional); senão vêm de `contexto_raw`. */
  rsi_14?: number | null;
  adx_14?: number | null;
  sentimento_ia: string;
  /** Veredito Brain (BULLISH / BEARISH / …); espelha `sentimento_ia` quando gravado pelo bot. */
  veredito_ia?: string | null;
  acao_tomada: string;
  justificativa: string;
  /** Texto bruto do Intelligence Hub (Python: `contexto` / `obter_contexto_agregado`). */
  contexto_raw?: string | null;
  /** Justificativa curta do Brain (coluna dedicada ao dashboard). */
  justificativa_ia?: string | null;
  /** Snapshot das notícias / Hub no momento do log. */
  noticias_agregadas?: string | null;
  /** Quando existir na tabela Supabase: % de PnL daquele evento (ex.: saída TP/SL). */
  resultado_trade?: number | null;
  pnl_pct?: number | null;
  funnel_stage?: string | null;
  funnel_abort_reason?: string | null;
  ml_prob_base?: number | null;
  ml_prob_calibrated?: number | null;
  llava_veto?: boolean | null;
  whale_flow_score?: number | null;
  social_sentiment_score?: number | null;
}

/** Tabela `trade_outcomes` (Outcome Engine / auditoria pós-fecho). */
export interface TradeOutcomeRow {
  order_id: string;
  symbol: string;
  side: "LONG" | "SHORT" | string;
  ml_probability_at_entry?: number | null;
  claude_justification?: string | null;
  /** PnL realizado em USDC (nome novo) */
  pnl_usdc?: number | null;
  /** Compat legado */
  pnl_realized?: number | null;
  /** ROI em % (nome novo) */
  roi_pct?: number | null;
  /** Compat legado */
  final_roi?: number | null;
  /** Motivo de fecho (nome novo) */
  motivo_fecho?: string | null;
  /** Compat legado */
  exit_type?: string | null;
  closed_at: string;
}

/** View `analytics_outcomes` (cards globais de auditoria). */
export interface AnalyticsOutcomesRow {
  win_rate?: number | null;
  win_rate_real?: number | null;
  pnl_accumulated?: number | null;
  pnl_acumulado?: number | null;
  total_trades?: number | null;
  trades_total?: number | null;
}

/** Tabela `bot_config` (overrides táticos em tempo real). */
export interface BotConfigRow {
  id: number;
  leverage?: number | null;
  risk_fraction?: number | null; // 0..1
  trailing_callback_rate?: number | null; // percentual (ex.: 0.5)
  updated_at?: string | null;
}

/** @deprecated Use `LogRow` — nome antigo quando a tabela era `trade_logs`. */
export type TradeLogRow = LogRow;
