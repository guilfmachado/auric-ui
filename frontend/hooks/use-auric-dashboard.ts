'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { coerceQuoteBalance } from "@/lib/auric/coerce-metrics";
import { createClient } from "@/lib/supabase/client";
import type {
  AnalyticsOutcomesRow,
  BotConfigRow,
  ConfigRow,
  LogRow,
  TradeOutcomeRow,
  TradingMode,
} from "@/lib/types/auric";

const CONFIG_ID = 1;

/**
 * PostgREST pode devolver array, uma linha única (.maybeSingle/.single) ou objeto
 * agregado (ex.: { latestByIdDesc: [...] } em depurações).
 */
function pickLatestLogRow(data: unknown): LogRow | null {
  if (data == null) return null;
  if (Array.isArray(data)) {
    const row = data[0];
    return row !== undefined && row !== null ? (row as LogRow) : null;
  }
  if (typeof data === "object" && data !== null && "latestByIdDesc" in data) {
    const arr = (data as { latestByIdDesc: unknown }).latestByIdDesc;
    if (Array.isArray(arr) && arr[0] != null) return arr[0] as LogRow;
  }
  if (typeof data === "object" && data !== null && "id" in data) {
    return data as LogRow;
  }
  return null;
}

function coerceBooleanLike(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") {
    const v = value.trim().toLowerCase();
    if (["true", "t", "1", "yes", "y", "on"].includes(v)) return true;
    if (["false", "f", "0", "no", "n", "off", ""].includes(v)) return false;
  }
  return false;
}

function defaultConfig(): ConfigRow {
  return {
    id: CONFIG_ID,
    trading_mode: "FUTURES",
    modo_operacao: "FUTURES",
    balance_usdt: null,
    pnl_day_pct: null,
    trades_24h: null,
    xgboost_accuracy: null,
    ml_probability: null,
    verdict_ia: null,
    justificativa_curta: null,
    updated_at: null,
  };
}

export function useAuricDashboard() {
  const supabase = useMemo(() => createClient(), []);

  const [ready, setReady] = useState(false);
  const [config, setConfig] = useState<ConfigRow>(defaultConfig());
  const [logs, setLogs] = useState<LogRow[]>([]);
  const [tradeOutcomes, setTradeOutcomes] = useState<TradeOutcomeRow[]>([]);
  const [tradeOutcomesLoading, setTradeOutcomesLoading] = useState(false);
  const [analyticsOutcomes, setAnalyticsOutcomes] =
    useState<AnalyticsOutcomesRow | null>(null);
  const [analyticsOutcomesLoading, setAnalyticsOutcomesLoading] = useState(false);
  const [botConfig, setBotConfig] = useState<BotConfigRow>({
    id: 1,
    leverage: 3,
    risk_fraction: 0.1,
    trailing_callback_rate: 0.6,
  });
  const [botConfigLoading, setBotConfigLoading] = useState(false);
  const [isSyncing, setIsSyncing] = useState(false);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [showSynced, setShowSynced] = useState(false);
  const botConfigDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const botConfigSyncedPulseRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const botConfigRef = useRef<BotConfigRow>(botConfig);
  const [pingMs, setPingMs] = useState<number | null>(null);
  const [trades24hComputed, setTrades24hComputed] = useState<number | null>(
    null
  );
  const [switchBusy, setSwitchBusy] = useState(false);
  /** Só reflecte o valor real após `refreshBotControl` (evita default optimista). */
  const [botActive, setBotActiveState] = useState(false);
  const [botBusy, setBotBusy] = useState(false);
  const [walletUsdt, setWalletUsdt] = useState<number | null>(null);
  const [entryPrice, setEntryPrice] = useState<number | null>(null);
  const [positionOpen, setPositionOpen] = useState(false);
  const [whaleFlowScore, setWhaleFlowScore] = useState<number | null>(null);
  const [socialSentimentScore, setSocialSentimentScore] = useState<number | null>(null);
  const [newsSentimentScore, setNewsSentimentScore] = useState<number | null>(null);
  const [forecastPrecoAlvo, setForecastPrecoAlvo] = useState<number | null>(null);
  const [forecastTendenciaAlta, setForecastTendenciaAlta] = useState<boolean | null>(null);
  const [llavaVeto, setLlavaVeto] = useState<boolean>(false);
  const [funnelStage, setFunnelStage] = useState<string | null>(null);
  const [funnelAbortReason, setFunnelAbortReason] = useState<string | null>(null);
  /** Após o primeiro fetch de `wallet_status` (mesmo com saldo null). */
  const [walletHydrated, setWalletHydrated] = useState(false);
  /** Erro na leitura de `wallet_status` (UI neutra; detalhe na consola). */
  const [walletFetchFailed, setWalletFetchFailed] = useState(false);
  /** Último log canónico: `logs` ordenado por `id` desc, `limit(1)` (motor Auric). */
  const [lastLog, setLastLog] = useState<LogRow | null>(null);
  /** Primeiro fetch paralelo `logs` (1) + `wallet_status` concluído. */
  const [motorHydrated, setMotorHydrated] = useState(false);
  /** Comando manual em voo (loading por botão). */
  const [manualPending, setManualPending] = useState<
    "LONG" | "SHORT" | "CLOSE_ALL" | null
  >(null);
  /** Falha crítica no fetch inicial ao Supabase (UI de ecrã cheio). */
  const [connectionError, setConnectionError] = useState<Error | null>(null);

  /** `wallet_status` saldo quote (USDC preferencial; fallback legado). */
  const fetchWalletBalance = useCallback(async () => {
    if (!supabase) {
      setWalletHydrated(true);
      return;
    }
    setWalletFetchFailed(false);
    try {
      const { data, error } = await supabase
        .from("wallet_status")
        .select("usdc_balance, usdt_balance, entry_price, posicao_aberta, whale_flow_score, social_sentiment_score, news_sentiment_score, forecast_preco_alvo, forecast_tendencia_alta, llava_veto, funnel_stage, funnel_abort_reason")
        .eq("id", 1)
        .single();

      if (error) {
        console.error(
          "[auric] wallet_status select:",
          error.message,
          error
        );
        setWalletFetchFailed(true);
        setWalletUsdt(null);
      } else {
        const row = data as { usdc_balance?: unknown; usdt_balance?: unknown } | null;
        setWalletUsdt(
          row ? coerceQuoteBalance(row.usdc_balance ?? row.usdt_balance) : null
        );
        const openNow = coerceBooleanLike(
          (row as { posicao_aberta?: unknown } | null)?.posicao_aberta
        );
        const ep =
          row && "entry_price" in row
            ? Number((row as { entry_price?: unknown }).entry_price)
            : NaN;
        setPositionOpen(openNow);
        setEntryPrice(openNow && Number.isFinite(ep) && ep > 0 ? ep : null);
        const wfs = Number((row as { whale_flow_score?: unknown } | null)?.whale_flow_score);
        setWhaleFlowScore(Number.isFinite(wfs) ? wfs : null);
        const sss = Number((row as { social_sentiment_score?: unknown } | null)?.social_sentiment_score);
        setSocialSentimentScore(Number.isFinite(sss) ? sss : null);
        const nss = Number((row as { news_sentiment_score?: unknown } | null)?.news_sentiment_score);
        setNewsSentimentScore(Number.isFinite(nss) ? nss : null);
        const fpa = Number((row as { forecast_preco_alvo?: unknown } | null)?.forecast_preco_alvo);
        setForecastPrecoAlvo(Number.isFinite(fpa) ? fpa : null);
        const ftaRaw = (row as { forecast_tendencia_alta?: unknown } | null)?.forecast_tendencia_alta;
        setForecastTendenciaAlta(typeof ftaRaw === "boolean" ? ftaRaw : null);
        setLlavaVeto(Boolean((row as { llava_veto?: unknown } | null)?.llava_veto));
        const fs = (row as { funnel_stage?: unknown } | null)?.funnel_stage;
        setFunnelStage(typeof fs === "string" && fs.length > 0 ? fs : null);
        const far = (row as { funnel_abort_reason?: unknown } | null)?.funnel_abort_reason;
        setFunnelAbortReason(typeof far === "string" && far.length > 0 ? far : null);
      }
    } catch (err) {
      console.error("[auric] fetchWalletBalance:", err);
      setWalletFetchFailed(true);
      setWalletUsdt(null);
    } finally {
      setWalletHydrated(true);
    }
  }, [supabase]);

  /** Última linha de `logs` por `id` desc (fonte do gauge ML, veredito, preço ref., indicadores). */
  const fetchLatestLogRow = useCallback(async () => {
    if (!supabase) return;
    try {
      const { data, error } = await supabase
        .from("logs")
        .select("*")
        .order("id", { ascending: false })
        .limit(1)
        .maybeSingle();

      if (error) {
        console.error("[auric] logs select (último):", error.message, error);
        setLastLog(null);
        return;
      }
      setLastLog(pickLatestLogRow(data));
    } catch (err) {
      console.error("[auric] fetchLatestLogRow:", err);
      setLastLog(null);
    }
  }, [supabase]);

  const refreshBotControl = useCallback(async () => {
    if (!supabase) return;
    const { data, error: e } = await supabase
      .from("bot_control")
      .select("is_active")
      .eq("id", 1)
      .maybeSingle();
    if (e) {
      console.warn("[auric] refreshBotControl:", e.message);
      return;
    }
    if (data && typeof (data as { is_active?: boolean }).is_active === "boolean") {
      setBotActiveState((data as { is_active: boolean }).is_active);
    }
  }, [supabase]);

  const refreshConfig = useCallback(async () => {
    if (!supabase) return;
    const { data, error: e } = await supabase
      .from("config")
      .select("*")
      .eq("id", CONFIG_ID)
      .maybeSingle();

    if (e) {
      console.warn("[auric] refreshConfig:", e.message);
      return;
    }
    if (data) {
      setConfig(data as ConfigRow);
    } else {
      const ins = await supabase
        .from("config")
        .insert({
          id: CONFIG_ID,
          trading_mode: "FUTURES",
          modo_operacao: "FUTURES",
        })
        .select()
        .single();
      if (ins.error) {
        console.warn("[auric] refreshConfig insert:", ins.error.message);
        return;
      }
      setConfig(ins.data as ConfigRow);
    }
  }, [supabase]);

  const refreshWallet = useCallback(async () => {
    if (!supabase) {
      setWalletHydrated(true);
      return;
    }
    try {
      const { data, error: e } = await supabase
        .from("wallet_status")
        .select("usdc_balance, usdt_balance, entry_price, posicao_aberta, whale_flow_score, social_sentiment_score, news_sentiment_score, forecast_preco_alvo, forecast_tendencia_alta, llava_veto, funnel_stage, funnel_abort_reason")
        .eq("id", 1)
        .single();
      if (e) {
        console.error("[auric] refreshWallet:", e.message, e);
        setWalletFetchFailed(true);
        return;
      }
      setWalletFetchFailed(false);
      const row = data as { usdc_balance?: unknown; usdt_balance?: unknown } | null;
      setWalletUsdt(
        row ? coerceQuoteBalance(row.usdc_balance ?? row.usdt_balance) : null
      );
      const openNow = coerceBooleanLike(
        (row as { posicao_aberta?: unknown } | null)?.posicao_aberta
      );
      const ep =
        row && "entry_price" in row
          ? Number((row as { entry_price?: unknown }).entry_price)
          : NaN;
      setPositionOpen(openNow);
      setEntryPrice(openNow && Number.isFinite(ep) && ep > 0 ? ep : null);
      const wfs = Number((row as { whale_flow_score?: unknown } | null)?.whale_flow_score);
      setWhaleFlowScore(Number.isFinite(wfs) ? wfs : null);
      const sss = Number((row as { social_sentiment_score?: unknown } | null)?.social_sentiment_score);
      setSocialSentimentScore(Number.isFinite(sss) ? sss : null);
      const nss = Number((row as { news_sentiment_score?: unknown } | null)?.news_sentiment_score);
      setNewsSentimentScore(Number.isFinite(nss) ? nss : null);
      const fpa = Number((row as { forecast_preco_alvo?: unknown } | null)?.forecast_preco_alvo);
      setForecastPrecoAlvo(Number.isFinite(fpa) ? fpa : null);
      const ftaRaw = (row as { forecast_tendencia_alta?: unknown } | null)?.forecast_tendencia_alta;
      setForecastTendenciaAlta(typeof ftaRaw === "boolean" ? ftaRaw : null);
      setLlavaVeto(Boolean((row as { llava_veto?: unknown } | null)?.llava_veto));
      const fs = (row as { funnel_stage?: unknown } | null)?.funnel_stage;
      setFunnelStage(typeof fs === "string" && fs.length > 0 ? fs : null);
      const far = (row as { funnel_abort_reason?: unknown } | null)?.funnel_abort_reason;
      setFunnelAbortReason(typeof far === "string" && far.length > 0 ? far : null);
    } finally {
      setWalletHydrated(true);
    }
  }, [supabase]);

  const refreshLogs = useCallback(async () => {
    if (!supabase) return;
    const { data, error: e } = await supabase
      .from("logs")
      .select("*")
      .order("created_at", { ascending: false, nullsFirst: false })
      .order("id", { ascending: false })
      .limit(50);

    if (e) {
      console.warn("[auric] refreshLogs:", e.message);
      return;
    }
    setLogs((data ?? []) as LogRow[]);
  }, [supabase]);

  const countTrades24h = useCallback(async () => {
    if (!supabase) return;
    const since = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
    const { count, error: e } = await supabase
      .from("logs")
      .select("id", { count: "exact", head: true })
      .gte("created_at", since);

    if (!e && count !== null) {
      setTrades24hComputed(count);
    } else {
      setTrades24hComputed(null);
    }
  }, [supabase]);

  const refreshTradeOutcomes = useCallback(async () => {
    if (!supabase) return;
    setTradeOutcomesLoading(true);
    const { data, error: e } = await supabase
      .from("trade_outcomes")
      .select(
        "order_id, symbol, side, ml_probability_at_entry, claude_justification, pnl_usdc, pnl_realized, roi_pct, final_roi, motivo_fecho, exit_type, closed_at"
      )
      .not("closed_at", "is", null)
      .order("closed_at", { ascending: false })
      .limit(50);
    setTradeOutcomesLoading(false);
    if (e) {
      console.warn("[auric] refreshTradeOutcomes:", e.message);
      return;
    }
    setTradeOutcomes((data ?? []) as TradeOutcomeRow[]);
  }, [supabase]);

  const refreshAnalyticsOutcomes = useCallback(async () => {
    if (!supabase) return;
    setAnalyticsOutcomesLoading(true);
    const { data, error: e } = await supabase
      .from("analytics_outcomes")
      .select("*")
      .single();
    setAnalyticsOutcomesLoading(false);
    if (e) {
      console.warn("[auric] refreshAnalyticsOutcomes:", e.message);
      return;
    }
    setAnalyticsOutcomes((data ?? null) as AnalyticsOutcomesRow | null);
  }, [supabase]);

  const refreshBotConfig = useCallback(async () => {
    if (!supabase) return;
    setBotConfigLoading(true);
    const { data, error: e } = await supabase
      .from("bot_config")
      .select("id, leverage, risk_fraction, trailing_callback_rate, updated_at")
      .eq("id", 1)
      .maybeSingle();
    setBotConfigLoading(false);
    if (e) {
      console.warn("[auric] refreshBotConfig:", e.message);
      return;
    }
    if (data) {
      setBotConfig((prev) => ({
        ...prev,
        ...(data as BotConfigRow),
      }));
      return;
    }
    const defaults: BotConfigRow = {
      id: 1,
      leverage: 3,
      risk_fraction: 0.1,
      trailing_callback_rate: 0.6,
      updated_at: new Date().toISOString(),
    };
    const ins = await supabase.from("bot_config").upsert(defaults, { onConflict: "id" });
    if (ins.error) {
      console.warn("[auric] refreshBotConfig upsert:", ins.error.message);
      return;
    }
    setBotConfig(defaults);
  }, [supabase]);

  const updateBotConfigDebounced = useCallback(
    (patch: Partial<Pick<BotConfigRow, "leverage" | "risk_fraction" | "trailing_callback_rate">>) => {
      setBotConfig((prev) => ({ ...prev, ...patch }));
      if (!supabase) return;
      if (botConfigDebounceRef.current) clearTimeout(botConfigDebounceRef.current);
      setIsSyncing(true);
      setSyncError(null);
      setShowSynced(false);
      botConfigDebounceRef.current = setTimeout(async () => {
        const next = { ...botConfigRef.current, ...patch };
        const leverage = Number(next.leverage ?? 3);
        const riskFraction = parseFloat(String(next.risk_fraction ?? 0.1));
        const trailingCallback = parseFloat(
          String(next.trailing_callback_rate ?? 0.6)
        );
        const payload = {
          leverage,
          risk_fraction: riskFraction,
          trailing_callback_rate: trailingCallback,
          updated_at: new Date().toISOString(),
        };
        const { error: e } = await supabase
          .from("bot_config")
          .update(payload)
          .eq("id", 1);
        if (e) {
          setIsSyncing(false);
          setSyncError(e.message);
          console.error("[auric] updateBotConfigDebounced:", e.message, e);
          return;
        }
        setIsSyncing(false);
        setShowSynced(true);
        if (botConfigSyncedPulseRef.current) clearTimeout(botConfigSyncedPulseRef.current);
        botConfigSyncedPulseRef.current = setTimeout(() => {
          setShowSynced(false);
        }, 2000);
      }, 350);
    },
    [supabase]
  );

  useEffect(() => {
    botConfigRef.current = botConfig;
  }, [botConfig]);

  const measurePing = useCallback(async () => {
    if (!supabase) return;
    const t0 = performance.now();
    const { error: e } = await supabase.from("logs").select("id").limit(1);
    const t1 = performance.now();
    if (!e) {
      setPingMs(Math.round(t1 - t0));
    } else {
      setPingMs(null);
    }
  }, [supabase]);

  /**
   * Fetch inicial: `config`, depois em paralelo saldo (`wallet_status`) + último `logs`,
   * depois lista de logs / bot / métricas. Erros reportados ao estado + `ERRO SUPABASE:` no terminal (Node).
   */
  useEffect(() => {
    const reportFailure = (error: unknown) => {
      const err =
        error instanceof Error
          ? error
          : new Error(
              error &&
                typeof error === "object" &&
                "message" in error &&
                typeof (error as { message: unknown }).message === "string"
                ? (error as { message: string }).message
                : String(error)
            );
      setConnectionError(err);
      console.error("ERRO SUPABASE:", error);
    };

    if (!supabase) {
      const err = new Error(
        "Cliente Supabase não criado — confirma NEXT_PUBLIC_SUPABASE_URL e NEXT_PUBLIC_SUPABASE_ANON_KEY em .env.local (reinicia o dev server após alterar)."
      );
      setConnectionError(err);
      console.error("ERRO SUPABASE:", err);
      setMotorHydrated(true);
      setWalletHydrated(true);
      setReady(true);
      return;
    }

    let cancelled = false;
    (async () => {
      setConnectionError(null);
      try {
        const { data: cdata, error: ce } = await supabase
          .from("config")
          .select("*")
          .eq("id", CONFIG_ID)
          .maybeSingle();
        if (ce) {
          reportFailure(ce);
          return;
        }
        if (cdata) {
          setConfig(cdata as ConfigRow);
        } else {
          const ins = await supabase
            .from("config")
            .insert({
              id: CONFIG_ID,
              trading_mode: "FUTURES",
              modo_operacao: "FUTURES",
            })
            .select()
            .single();
          if (ins.error) {
            reportFailure(ins.error);
            return;
          }
          setConfig(ins.data as ConfigRow);
        }

        const [wRes, lRes] = await Promise.all([
          supabase
            .from("wallet_status")
            .select("usdc_balance, usdt_balance, entry_price, posicao_aberta, whale_flow_score, social_sentiment_score, news_sentiment_score, forecast_preco_alvo, forecast_tendencia_alta, llava_veto, funnel_stage, funnel_abort_reason")
            .eq("id", 1)
            .single(),
          supabase
            .from("logs")
            .select("*")
            .order("id", { ascending: false })
            .limit(1)
            .maybeSingle(),
        ]);
        if (wRes.error) {
          reportFailure(wRes.error);
          return;
        }
        setWalletFetchFailed(false);
        const wrow = wRes.data as {
          usdc_balance?: unknown;
          usdt_balance?: unknown;
          entry_price?: unknown;
          posicao_aberta?: unknown;
          whale_flow_score?: unknown;
          social_sentiment_score?: unknown;
          news_sentiment_score?: unknown;
          forecast_preco_alvo?: unknown;
          forecast_tendencia_alta?: unknown;
          llava_veto?: unknown;
          funnel_stage?: unknown;
          funnel_abort_reason?: unknown;
        } | null;
        setWalletUsdt(
          wrow != null ? coerceQuoteBalance(wrow.usdc_balance ?? wrow.usdt_balance) : null
        );
        const openNow = coerceBooleanLike(wrow?.posicao_aberta);
        const ep = Number(wrow?.entry_price);
        setPositionOpen(openNow);
        setEntryPrice(openNow && Number.isFinite(ep) && ep > 0 ? ep : null);
        const wfs = Number(wrow?.whale_flow_score);
        setWhaleFlowScore(Number.isFinite(wfs) ? wfs : null);
        const sss = Number(wrow?.social_sentiment_score);
        setSocialSentimentScore(Number.isFinite(sss) ? sss : null);
        const nss = Number(wrow?.news_sentiment_score);
        setNewsSentimentScore(Number.isFinite(nss) ? nss : null);
        const fpa = Number(wrow?.forecast_preco_alvo);
        setForecastPrecoAlvo(Number.isFinite(fpa) ? fpa : null);
        setForecastTendenciaAlta(
          typeof wrow?.forecast_tendencia_alta === "boolean"
            ? wrow.forecast_tendencia_alta
            : null
        );
        setLlavaVeto(Boolean(wrow?.llava_veto));
        setFunnelStage(
          typeof wrow?.funnel_stage === "string" && wrow.funnel_stage.length > 0
            ? wrow.funnel_stage
            : null
        );
        setFunnelAbortReason(
          typeof wrow?.funnel_abort_reason === "string" &&
            wrow.funnel_abort_reason.length > 0
            ? wrow.funnel_abort_reason
            : null
        );
        setWalletHydrated(true);

        if (lRes.error) {
          reportFailure(lRes.error);
          return;
        }
        setLastLog(pickLatestLogRow(lRes.data));

        if (cancelled) return;
        setMotorHydrated(true);

        const since = new Date(
          Date.now() - 24 * 60 * 60 * 1000
        ).toISOString();
        const [logsList, botRes, countRes] = await Promise.all([
          supabase
            .from("logs")
            .select("*")
            .order("created_at", { ascending: false, nullsFirst: false })
            .order("id", { ascending: false })
            .limit(50),
          supabase
            .from("bot_control")
            .select("is_active")
            .eq("id", 1)
            .maybeSingle(),
          supabase
            .from("logs")
            .select("id", { count: "exact", head: true })
            .gte("created_at", since),
        ]);

        if (logsList.error) {
          reportFailure(logsList.error);
          return;
        }
        setLogs((logsList.data ?? []) as LogRow[]);

        if (botRes.error) {
          reportFailure(botRes.error);
          return;
        }
        if (
          botRes.data &&
          typeof (botRes.data as { is_active?: boolean }).is_active ===
            "boolean"
        ) {
          setBotActiveState((botRes.data as { is_active: boolean }).is_active);
        }

        if (countRes.error) {
          reportFailure(countRes.error);
          return;
        }
        if (countRes.count !== null) {
          setTrades24hComputed(countRes.count);
        } else {
          setTrades24hComputed(null);
        }

        const t0 = performance.now();
        const pingRes = await supabase.from("logs").select("id").limit(1);
        const t1 = performance.now();
        if (pingRes.error) {
          reportFailure(pingRes.error);
          return;
        }
        setPingMs(Math.round(t1 - t0));
      } catch (error) {
        reportFailure(error);
      } finally {
        if (!cancelled) {
          setMotorHydrated(true);
          setReady(true);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [supabase]);

  useEffect(() => {
    if (!supabase) return;
    const id = window.setInterval(() => {
      void measurePing();
    }, 12000);
    return () => clearInterval(id);
  }, [supabase, measurePing]);

  useEffect(() => {
    if (!supabase) return;
    void refreshTradeOutcomes();
    void refreshAnalyticsOutcomes();
    void refreshBotConfig();
    const id = window.setInterval(() => {
      void refreshTradeOutcomes();
      void refreshAnalyticsOutcomes();
    }, 30000);
    return () => clearInterval(id);
  }, [supabase, refreshTradeOutcomes, refreshAnalyticsOutcomes, refreshBotConfig]);

  useEffect(() => {
    return () => {
      if (botConfigDebounceRef.current) clearTimeout(botConfigDebounceRef.current);
      if (botConfigSyncedPulseRef.current) clearTimeout(botConfigSyncedPulseRef.current);
    };
  }, []);

  useEffect(() => {
    if (!supabase) return;
    const chConfig = supabase
      .channel("auric-realtime-config")
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "config" },
        () => {
          void refreshConfig();
        }
      )
      .subscribe((status, err) => {
        if (err) console.error("[auric] Realtime config:", err);
        else if (status === "SUBSCRIBED")
          console.info("[auric] Realtime: config inscrito");
      });

    /** Motor: `logs` + `wallet_status` + `bot_control`. */
    const chMotor = supabase
      .channel("auric-motor-realtime")
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "logs" },
        () => {
          void fetchLatestLogRow();
          void refreshLogs();
          void countTrades24h();
        }
      )
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "wallet_status" },
        (payload) => {
          const row = payload.new as {
            usdc_balance?: unknown;
            usdt_balance?: unknown;
            entry_price?: unknown;
            posicao_aberta?: unknown;
            whale_flow_score?: unknown;
            social_sentiment_score?: unknown;
            news_sentiment_score?: unknown;
            forecast_preco_alvo?: unknown;
            forecast_tendencia_alta?: unknown;
            llava_veto?: unknown;
            funnel_stage?: unknown;
            funnel_abort_reason?: unknown;
          } | null;
          if (row && ("usdc_balance" in row || "usdt_balance" in row)) {
            const n = coerceQuoteBalance(row.usdc_balance ?? row.usdt_balance);
            if (n !== null) {
              setWalletFetchFailed(false);
              setWalletUsdt(n);
              const openNow = coerceBooleanLike(row.posicao_aberta);
              const ep = Number(row.entry_price);
              setPositionOpen(openNow);
              setEntryPrice(openNow && Number.isFinite(ep) && ep > 0 ? ep : null);
              const wfs = Number(row.whale_flow_score);
              setWhaleFlowScore(Number.isFinite(wfs) ? wfs : null);
              const sss = Number(row.social_sentiment_score);
              setSocialSentimentScore(Number.isFinite(sss) ? sss : null);
              const nss = Number(row.news_sentiment_score);
              setNewsSentimentScore(Number.isFinite(nss) ? nss : null);
              const fpa = Number(row.forecast_preco_alvo);
              setForecastPrecoAlvo(Number.isFinite(fpa) ? fpa : null);
              setForecastTendenciaAlta(
                typeof row.forecast_tendencia_alta === "boolean"
                  ? row.forecast_tendencia_alta
                  : null
              );
              setLlavaVeto(Boolean(row.llava_veto));
              setFunnelStage(
                typeof row.funnel_stage === "string" && row.funnel_stage.length > 0
                  ? row.funnel_stage
                  : null
              );
              setFunnelAbortReason(
                typeof row.funnel_abort_reason === "string" && row.funnel_abort_reason.length > 0
                  ? row.funnel_abort_reason
                  : null
              );
              setWalletHydrated(true);
              return;
            }
          }
          void fetchWalletBalance();
        }
      )
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "bot_control" },
        () => {
          void refreshBotControl();
        }
      )
      .subscribe((status, err) => {
        if (err) console.error("[auric] Realtime motor:", err);
        else if (status === "SUBSCRIBED")
          console.info("[auric] Realtime: logs + wallet_status + bot_control inscrito");
      });

    return () => {
      void supabase.removeChannel(chConfig);
      void supabase.removeChannel(chMotor);
    };
  }, [
    supabase,
    refreshConfig,
    refreshLogs,
    fetchWalletBalance,
    fetchLatestLogRow,
    refreshBotControl,
    countTrades24h,
  ]);

  const setBotActive = useCallback(
    async (active: boolean) => {
      if (!supabase) return;
      setBotBusy(true);
      setBotActiveState(active);
      const { error: e } = await supabase
        .from("bot_control")
        .update({
          is_active: active,
          updated_at: new Date().toISOString(),
        })
        .eq("id", 1);
      setBotBusy(false);
      if (e) {
        console.error("[auric] bot_control update:", e.message);
        setBotActiveState(!active);
        return;
      }
      await refreshBotControl();
    },
    [supabase, refreshBotControl]
  );

  const setTradingMode = useCallback(
    async (mode: TradingMode) => {
      if (!supabase) return;
      setSwitchBusy(true);
      const { error: e } = await supabase
        .from("config")
        .upsert(
          {
            id: CONFIG_ID,
            trading_mode: mode,
            modo_operacao: mode,
            updated_at: new Date().toISOString(),
          },
          { onConflict: "id" }
        );
      setSwitchBusy(false);
      if (e) {
        console.warn("[auric] config upsert:", e.message);
        return;
      }
      await refreshConfig();
    },
    [supabase, refreshConfig]
  );

  const insertManualCommand = useCallback(
    async (command: "LONG" | "SHORT" | "CLOSE_ALL") => {
      if (!supabase) return;
      setManualPending(command);
      const { error: e } = await supabase
        .from("manual_commands")
        .insert({ command, executed: false })
        .select("id")
        .maybeSingle();
      setManualPending(null);
      if (e) {
        console.error("[auric] manual_commands insert:", e.message);
      }
    },
    [supabase]
  );

  const latestLog = lastLog;

  return {
    ready,
    connectionError,
    supabaseReady: !!supabase,
    motorHydrated,
    config,
    logs,
    latestLog,
    pingMs,
    trades24hComputed,
    switchBusy,
    setTradingMode,
    botActive,
    botBusy,
    setBotActive,
    walletUsdt,
    entryPrice,
    positionOpen,
    walletHydrated,
    walletFetchFailed,
    whaleFlowScore,
    socialSentimentScore,
    newsSentimentScore,
    forecastPrecoAlvo,
    forecastTendenciaAlta,
    llavaVeto,
    funnelStage,
    funnelAbortReason,
    manualPending,
    insertManualCommand,
    tradeOutcomes,
    tradeOutcomesLoading,
    analyticsOutcomes,
    analyticsOutcomesLoading,
    botConfig,
    botConfigLoading,
    isSyncing,
    syncError,
    showSynced,
    updateBotConfigDebounced,
  };
}
