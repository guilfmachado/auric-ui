"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { coerceUsdtBalance } from "@/lib/auric/coerce-metrics";
import { createClient } from "@/lib/supabase/client";
import type { ConfigRow, LogRow, TradingMode } from "@/lib/types/auric";

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
  const [config, setConfig] = useState<ConfigRow>(defaultConfig());
  const [logs, setLogs] = useState<LogRow[]>([]);
  const [pingMs, setPingMs] = useState<number | null>(null);
  const [trades24hComputed, setTrades24hComputed] = useState<number | null>(
    null
  );
  const [switchBusy, setSwitchBusy] = useState(false);
  /** Só reflecte o valor real após `refreshBotControl` (evita default optimista). */
  const [botActive, setBotActiveState] = useState(false);
  const [botBusy, setBotBusy] = useState(false);
  const [walletUsdt, setWalletUsdt] = useState<number | null>(null);
  /** Após o primeiro fetch de `wallet_status` (mesmo com saldo null). */
  const [walletHydrated, setWalletHydrated] = useState(false);
  /** Erro na leitura de `wallet_status` (UI neutra; detalhe na consola). */
  const [walletFetchFailed, setWalletFetchFailed] = useState(false);
  /** Último log canónico: `logs` ordenado por `id` desc, `limit(1)` (motor Auric). */
  const [lastLog, setLastLog] = useState<LogRow | null>(null);
  /** Primeiro fetch paralelo `logs` (1) + `wallet_status` concluído. */
  const [motorHydrated, setMotorHydrated] = useState(false);
  /** Comando manual em voo (loading por botão). */
  const [manualPending, setManualPending] = useState<"LONG" | "SHORT" | null>(
    null
  );

  /**
   * Hidrata carteira + último log (fonte única para gauge / Intelligence / indicadores).
   * Equivalente a um useEffect com:
   * `.from('logs').select('*').order('id', { ascending: false }).limit(1)` +
   * `wallet_status` id=1.
   */
  const hydrateMotor = useCallback(async () => {
    if (!supabase) {
      setMotorHydrated(true);
      setWalletHydrated(true);
      return;
    }
    setWalletFetchFailed(false);
    try {
      const [logsRes, walletRes] = await Promise.all([
        supabase
          .from("logs")
          .select("*")
          .order("id", { ascending: false })
          .limit(1),
        supabase
          .from("wallet_status")
          .select("usdt_balance")
          .eq("id", 1)
          .maybeSingle(),
      ]);

      if (logsRes.error) {
        console.error(
          "[auric] fetch último log (logs):",
          logsRes.error.message,
          logsRes.error
        );
        setLastLog(null);
      } else {
        const row = (logsRes.data?.[0] as LogRow | undefined) ?? null;
        setLastLog(row);
      }

      if (walletRes.error) {
        console.error(
          "[auric] fetch wallet_status:",
          walletRes.error.message,
          walletRes.error
        );
        setWalletFetchFailed(true);
        setWalletUsdt(null);
      } else {
        const w = walletRes.data as { usdt_balance?: unknown } | null;
        setWalletUsdt(w ? coerceUsdtBalance(w.usdt_balance) : null);
      }
    } catch (err) {
      console.error("[auric] hydrateMotor exceção:", err);
      setWalletFetchFailed(true);
    } finally {
      setWalletHydrated(true);
      setMotorHydrated(true);
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
        .select("usdt_balance")
        .eq("id", 1)
        .maybeSingle();
      if (e) {
        console.error("[auric] refreshWallet:", e.message, e);
        setWalletFetchFailed(true);
        return;
      }
      setWalletFetchFailed(false);
      const row = data as { usdt_balance?: unknown } | null;
      setWalletUsdt(row ? coerceUsdtBalance(row.usdt_balance) : null);
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
   * Bootstrap + leitura canónica do motor: último `logs` (order id desc, limit 1)
   * e `wallet_status` (Supabase JS).
   */
  useEffect(() => {
    if (!supabase) {
      setMotorHydrated(true);
      setWalletHydrated(true);
      setReady(true);
      return;
    }

    let cancelled = false;
    (async () => {
      try {
        await refreshConfig();
        await hydrateMotor();
        await Promise.all([
          refreshLogs(),
          refreshBotControl(),
          countTrades24h(),
          measurePing(),
        ]);
      } catch (err) {
        if (!cancelled) {
          console.error("[auric] bootstrap:", err);
        }
      } finally {
        if (!cancelled) setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    supabase,
    hydrateMotor,
    refreshConfig,
    refreshLogs,
    refreshBotControl,
    countTrades24h,
    measurePing,
  ]);

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
      .channel("auric-realtime-config")
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "config" },
        () => {
          void refreshConfig();
        }
      )
      .subscribe();

    /** Motor Auric: logs + carteira + bot em um único canal Realtime. */
    const chMotor = supabase
      .channel("auric-motor-realtime")
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "logs" },
        () => {
          void hydrateMotor();
          void refreshLogs();
          void countTrades24h();
        }
      )
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "wallet_status" },
        (payload) => {
          const row = payload.new as { usdt_balance?: unknown } | null;
          if (row && "usdt_balance" in row) {
            const n = coerceUsdtBalance(row.usdt_balance);
            if (n !== null) {
              setWalletFetchFailed(false);
              setWalletUsdt(n);
              setWalletHydrated(true);
              return;
            }
          }
          void refreshWallet();
        }
      )
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "bot_control" },
        () => {
          void refreshBotControl();
        }
      )
      .subscribe();

    return () => {
      void supabase.removeChannel(chConfig);
      void supabase.removeChannel(chMotor);
    };
  }, [
    supabase,
    refreshConfig,
    refreshLogs,
    refreshWallet,
    hydrateMotor,
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
    async (command: "LONG" | "SHORT") => {
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
    walletHydrated,
    walletFetchFailed,
    manualPending,
    insertManualCommand,
  };
}
