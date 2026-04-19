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

/** Tabela trade_logs (alinhada ao logger Python). */
export interface TradeLogRow {
  id: number;
  created_at?: string;
  ativo: string;
  preco_atual: number;
  probabilidade_ml: number;
  sentimento_ia: string;
  acao_tomada: string;
  justificativa: string;
  /** Quando existir na tabela Supabase: % de PnL daquele evento (ex.: saída TP/SL). */
  resultado_trade?: number | null;
  pnl_pct?: number | null;
}
