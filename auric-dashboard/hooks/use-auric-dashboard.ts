"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { createClient } from "@/lib/supabase/client";
import type { ConfigRow, TradeLogRow, TradingMode } from "@/lib/types/auric";

const CONFIG_ID = 1;

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
  const [error, setError] = useState<string | null>(null);
  const [config, setConfig] = useState<ConfigRow>(defaultConfig());
  const [logs, setLogs] = useState<TradeLogRow[]>([]);
  const [pingMs, setPingMs] = useState<number | null>(null);
  const [trades24hComputed, setTrades24hComputed] = useState<number | null>(
    null
  );
  const [switchBusy, setSwitchBusy] = useState(false);

  const refreshConfig = useCallback(async () => {
    if (!supabase) return;
    const { data, error: e } = await supabase
      .from("config")
      .select("*")
      .eq("id", CONFIG_ID)
      .maybeSingle();

    if (e) {
      setError(e.message);
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
        setError(ins.error.message);
        return;
      }
      setConfig(ins.data as ConfigRow);
    }
  }, [supabase]);

  const refreshLogs = useCallback(async () => {
    if (!supabase) return;
    const { data, error: e } = await supabase
      .from("trade_logs")
      .select("*")
      .order("id", { ascending: false })
      .limit(50);

    if (e) {
      setError(e.message);
      return;
    }
    setLogs((data ?? []) as TradeLogRow[]);
  }, [supabase]);

  const countTrades24h = useCallback(async () => {
    if (!supabase) return;
    const since = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
    const { count, error: e } = await supabase
      .from("trade_logs")
      .select("id", { count: "exact", head: true })
      .gte("created_at", since);

    if (!e && count !== null) {
      setTrades24hComputed(count);
    } else {
      setTrades24hComputed(null);
    }
  }, [supabase]);

  const measurePing = useCallback(async () => {
    if (!supabase) return;
    const t0 = performance.now();
    const { error: e } = await supabase.from("trade_logs").select("id").limit(1);
    const t1 = performance.now();
    if (!e) {
      setPingMs(Math.round(t1 - t0));
    } else {
      setPingMs(null);
    }
  }, [supabase]);

  useEffect(() => {
    if (!supabase) {
      setReady(true);
      setError(
        "Configure NEXT_PUBLIC_SUPABASE_URL e NEXT_PUBLIC_SUPABASE_ANON_KEY no .env.local"
      );
      return;
    }

    let cancelled = false;
    (async () => {
      setError(null);
      try {
        await refreshConfig();
        await refreshLogs();
        await countTrades24h();
        await measurePing();
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Erro desconhecido");
        }
      } finally {
        if (!cancelled) setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [supabase, refreshConfig, refreshLogs, countTrades24h, measurePing]);

  useEffect(() => {
    if (!supabase) return;
    const id = window.setInterval(() => {
      void measurePing();
    }, 12000);
    return () => clearInterval(id);
  }, [supabase, measurePing]);

  useEffect(() => {
    if (!supabase) return;
    const chConfig = supabase
      .channel("realtime-config")
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "config" },
        () => {
          void refreshConfig();
        }
      )
      .subscribe();

    const chLogs = supabase
      .channel("realtime-trade_logs")
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "trade_logs" },
        () => {
          void refreshLogs();
          void countTrades24h();
        }
      )
      .subscribe();

    return () => {
      void supabase.removeChannel(chConfig);
      void supabase.removeChannel(chLogs);
    };
  }, [supabase, refreshConfig, refreshLogs, countTrades24h]);

  const setTradingMode = useCallback(
    async (mode: TradingMode) => {
      if (!supabase) return;
      setSwitchBusy(true);
      setError(null);
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
        setError(e.message);
        return;
      }
      await refreshConfig();
    },
    [supabase, refreshConfig]
  );

  const latestLog = logs[0] ?? null;

  return {
    ready,
    error,
    supabaseReady: !!supabase,
    config,
    logs,
    latestLog,
    pingMs,
    trades24hComputed,
    switchBusy,
    setTradingMode,
  };
}
