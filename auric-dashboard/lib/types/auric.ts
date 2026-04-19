/** Linha única de configuração do bot (id = 1). */
export type TradingMode = "SPOT" | "FUTURES";

export interface ConfigRow {
  id: number;
  trading_mode: TradingMode;
  /** Coluna usada pelo bot Python (`obter_modo_operacao`). */
  modo_operacao?: TradingMode;
  balance_usdt: number | null;
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
  /** Par negociado (ex.: ETH/USDT) — coluna canónica no Supabase. */
  par_moeda?: string;
  /** Legado ou flag booleana na base; o dashboard usa `par_moeda` para exibir o par. */
  ativo?: string | boolean;
  preco_atual: number;
  probabilidade_ml: number;
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
}

/** @deprecated Use `LogRow` — nome antigo quando a tabela era `trade_logs`. */
export type TradeLogRow = LogRow;
