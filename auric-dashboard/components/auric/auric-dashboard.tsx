'use client';

import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";

import { Skeleton } from "@/components/ui/skeleton";
import { useAuricDashboard } from "@/hooks/use-auric-dashboard";
import { useEthTicker } from "@/hooks/use-eth-ticker";
import { parseTelemetryFromLog } from "@/lib/auric/parse-telemetry";

import { ProfitChart } from "@/components/ProfitChart";

import { AuricHeader } from "./auric-header";
import { AiAuditPanel } from "./ai-audit-panel";
import { BrainFeed } from "./brain-feed";
import { GaugeMatrix } from "./gauge-matrix";
import { IndicatorHub } from "./indicator-hub";
import { LogsTable } from "./logs-table";
import { PulseHero } from "./pulse-hero";
import { TacticalOverridePanel } from "./tactical-override-panel";
import { TerminalCard } from "./terminal-card";

/** Saldo `wallet_status`: duas casas decimais. */
function formatWalletUsd(n: number) {
  if (Number.isNaN(n)) return "0.00";
  return n.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatPct(n: number | null | undefined) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}%`;
}

const BINANCE_FAPI_ETH_PRICE =
  "https://fapi.binance.com/fapi/v1/ticker/price?symbol=ETHUSDT";

const LIVE_LOGS_N = 5;

/** Gauge ML: arco em [0,1]; rótulo = `probabilidade_ml * 100` (formato %) quando p ≤ 1. */
function mlFromProbabilidade(raw: unknown): {
  prob01: number | null;
  pctLabel: string | null;
} {
  if (raw === null || raw === undefined) {
    return { prob01: null, pctLabel: null };
  }
  const p = Number(raw);
  if (!Number.isFinite(p)) {
    return { prob01: null, pctLabel: null };
  }
  const prob01 = p > 1 ? Math.min(1, p / 100) : Math.min(1, Math.max(0, p));
  const pctLabel =
    p <= 1 ? `${(p * 100).toFixed(1)}%` : `${p.toFixed(1)}%`;
  return { prob01, pctLabel };
}

export function AuricDashboard() {
  const {
    ready,
    connectionError,
    supabaseReady,
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
    walletFetchFailed,
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
  } = useAuricDashboard();

  const { changePct: ethCh } = useEthTicker();

  const [livePrice, setLivePrice] = useState<number | null>(null);
  const [priceFlash, setPriceFlash] = useState<"up" | "down" | null>(null);
  const prevPriceRef = useRef<number | null>(null);
  const flashClearTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(
    null
  );

  useEffect(() => {
    let cancelled = false;

    const tick = async () => {
      if (cancelled) return;
      try {
        const res = await fetch(BINANCE_FAPI_ETH_PRICE);
        if (!res.ok || cancelled) return;
        const j = (await res.json()) as { price?: string };
        const p = parseFloat(j.price ?? "");
        if (!Number.isFinite(p) || cancelled) return;

        const prev = prevPriceRef.current;
        prevPriceRef.current = p;
        setLivePrice(p);

        if (prev !== null && p !== prev) {
          if (flashClearTimeoutRef.current) {
            clearTimeout(flashClearTimeoutRef.current);
          }
          setPriceFlash(p > prev ? "up" : "down");
          flashClearTimeoutRef.current = setTimeout(() => {
            setPriceFlash(null);
            flashClearTimeoutRef.current = null;
          }, 1000);
        }
      } catch {
        /* rede / CORS: mantém último livePrice */
      }
    };

    void tick();
    const id = window.setInterval(() => void tick(), 2000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      if (flashClearTimeoutRef.current) {
        clearTimeout(flashClearTimeoutRef.current);
      }
    };
  }, []);

  const ethTickerFootnote =
    "Binance USDT-M Futures · preço ~2s · % 24h (Spot ref.)";

  const { prob01: mlFromLatestLog, pctLabel: mlPctLabel } =
    mlFromProbabilidade(latestLog?.probabilidade_ml);

  const vereditoIaRaw =
    latestLog?.veredito_ia != null
      ? String(latestLog.veredito_ia).trim()
      : "";
  const vereditoIa = vereditoIaRaw.length > 0 ? vereditoIaRaw : "AGUARDANDO";

  const justificativaRaw =
    latestLog?.justificativa != null
      ? String(latestLog.justificativa).trim()
      : "";
  const justificativaLog =
    justificativaRaw.length > 0
      ? justificativaRaw
      : "Sem análise neste ciclo";

  const noticiasAgregadas =
    (latestLog?.noticias_agregadas &&
      String(latestLog.noticias_agregadas).trim()) ||
    "";

  const balanceStr =
    walletFetchFailed ||
    walletUsdt == null ||
    Number.isNaN(walletUsdt as number)
      ? "—"
      : `$${formatWalletUsd(walletUsdt)}`;

  const pnl = config.pnl_day_pct;
  const pnlPositive =
    pnl === null || pnl === undefined ? null : pnl > 0 ? true : pnl < 0 ? false : null;

  const tradesStr =
    config.trades_24h != null
      ? String(config.trades_24h)
      : trades24hComputed != null
        ? String(trades24hComputed)
        : "—";

  const winRateStr =
    config.xgboost_accuracy !== null &&
    config.xgboost_accuracy !== undefined
      ? `${(config.xgboost_accuracy <= 1
          ? config.xgboost_accuracy * 100
          : config.xgboost_accuracy
        ).toFixed(1)}%`
      : "—";

  const telemetry = parseTelemetryFromLog(latestLog);
  const rsiForGauge =
    telemetry.rsi ??
    (() => {
      const j = latestLog?.justificativa ?? "";
      const m = j.match(/RSI(?:\s*\(\s*14\s*\))?\s*[:=]\s*([\d.]+)/i);
      return m ? parseFloat(m[1]) : null;
    })();

  const logsForTable = logs.slice(0, LIVE_LOGS_N);
  const emptyTelemetry = parseTelemetryFromLog(null);
  const isMotorLoading = ready && supabaseReady && !motorHydrated;

  if (connectionError != null) {
    return (
      <div className="fixed inset-0 flex min-h-screen items-center justify-center bg-black">
        <div className="max-w-[95vw] break-words px-4 text-center text-5xl font-bold leading-tight text-red-600 sm:text-7xl md:text-8xl">
          ERRO DE CONEXÃO: {connectionError.message}
        </div>
      </div>
    );
  }

  if (!ready) {
    return (
      <div className="min-h-screen bg-[#09090b] px-4 py-10 sm:px-6">
        <Skeleton className="mx-auto mb-8 h-20 max-w-6xl rounded-xl border border-[#27272a] bg-[#18181b]" />
        <div className="mx-auto grid max-w-6xl gap-4 sm:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton
              key={i}
              className="h-36 rounded-xl border border-[#27272a] bg-[#18181b]"
            />
          ))}
        </div>
      </div>
    );
  }

  const showMotor = supabaseReady && motorHydrated;

  return (
    <div className="min-h-screen bg-[#09090b] text-zinc-100">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.35 }}
        className="mx-auto flex max-w-7xl flex-col gap-6 px-4 py-8 sm:px-6 lg:gap-8 lg:py-12"
      >
        {!supabaseReady && (
          <div
            className="rounded-lg border border-zinc-700/50 bg-zinc-900/50 px-4 py-3 text-sm text-zinc-400"
            style={{
              fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
            }}
          >
            Configure NEXT_PUBLIC_SUPABASE_URL e NEXT_PUBLIC_SUPABASE_ANON_KEY
            em .env.local (Project Settings → API → anon public) para ligar o
            motor Auric.
          </div>
        )}

        <AuricHeader
          pingMs={pingMs}
          tradingMode={config.modo_operacao ?? config.trading_mode}
          onTradingModeChange={setTradingMode}
          busy={switchBusy}
          botActive={botActive}
          botBusy={botBusy}
          onBotToggle={(v) => void setBotActive(v)}
          trades24h={tradesStr}
          winRate={winRateStr}
          manualPending={manualPending}
          onManualLong={() => void insertManualCommand("LONG")}
          onManualShort={() => void insertManualCommand("SHORT")}
          onManualCloseAll={() => void insertManualCommand("CLOSE_ALL")}
        />
        <TacticalOverridePanel
          config={botConfig}
          loading={botConfigLoading}
          isSyncing={isSyncing}
          syncError={syncError}
          showSynced={showSynced}
          onPatch={updateBotConfigDebounced}
        />

        {isMotorLoading && (
          <>
            <PulseHero
              isLoading
              balanceUsdt="—"
              balanceLoading={false}
              pnlDayPct={formatPct(pnl ?? null)}
              pnlPositive={pnlPositive}
              ethPrice={livePrice}
              ethChangePct={ethCh}
              ethLoading={livePrice == null}
              ethFootnote={ethTickerFootnote}
              ethPriceFlash={priceFlash}
            />

            <div className="flex min-h-0 w-full min-w-0 flex-col gap-6 xl:gap-8">
              <div className="grid min-h-0 w-full min-w-0 grid-cols-1 items-start gap-6 xl:grid-cols-12">
                <div className="flex min-h-0 min-w-0 flex-col gap-6 xl:col-span-8">
                  <TerminalCard className="min-h-[280px] space-y-5">
                    <Skeleton className="h-3 w-36 rounded-md" />
                    <div className="flex justify-center pt-4">
                      <Skeleton className="h-24 w-[min(100%,14rem)] rounded-2xl" />
                    </div>
                    <Skeleton className="h-24 w-full rounded-lg" />
                    <Skeleton className="h-20 w-full rounded-lg" />
                  </TerminalCard>
                  <TerminalCard>
                    <Skeleton className="mb-3 h-3 w-40 rounded-md" />
                    <Skeleton className="h-4 w-full max-w-md rounded-md" />
                    <Skeleton className="mt-4 h-48 w-full rounded-xl" />
                  </TerminalCard>
                </div>

                <aside className="flex w-full min-w-0 flex-col gap-6 xl:col-span-4">
                  <GaugeMatrix
                    isLoading
                    mlProb01={null}
                    mlPercentLabel={null}
                    rsi={null}
                  />
                  <IndicatorHub isLoading telemetry={emptyTelemetry} />
                </aside>
              </div>

              <section className="w-full min-w-0 shrink-0">
                <LogsTable
                  isLoading
                  rows={[]}
                  maxRows={LIVE_LOGS_N}
                />
              </section>
              <section className="w-full min-w-0 shrink-0">
                <AiAuditPanel
                  rows={[]}
                  analytics={null}
                  isLoading
                  metricsLoading
                />
              </section>
            </div>
          </>
        )}

        {showMotor && (
          <>
            <PulseHero
              balanceUsdt={balanceStr}
              balanceLoading={false}
              pnlDayPct={formatPct(pnl ?? null)}
              pnlPositive={pnlPositive}
              ethPrice={livePrice}
              ethChangePct={ethCh}
              ethLoading={livePrice == null}
              ethFootnote={ethTickerFootnote}
              ethPriceFlash={priceFlash}
            />

            <div className="flex min-h-0 w-full min-w-0 flex-col gap-6 xl:gap-8">
              <div className="grid min-h-0 w-full min-w-0 grid-cols-1 items-start gap-6 xl:grid-cols-12">
                <div className="flex min-h-0 min-w-0 flex-col gap-6 xl:col-span-8">
                  <BrainFeed
                    vereditoIa={vereditoIa}
                    justificativaLog={justificativaLog}
                    noticiasAgregadas={noticiasAgregadas}
                  />
                  <TerminalCard>
                    <h2 className="text-[10px] font-semibold tracking-[0.22em] text-zinc-500 uppercase">
                      Curva acumulada (%)
                    </h2>
                    <p className="mt-1 text-[10px] text-zinc-600">
                      <span className="font-mono text-zinc-500">
                        resultado_trade
                      </span>{" "}
                      ou heurística TP/SL.
                    </p>
                    <div className="mt-4">
                      <ProfitChart data={logs} />
                    </div>
                  </TerminalCard>
                </div>

                <aside className="flex w-full min-w-0 flex-col gap-6 xl:col-span-4">
                  <GaugeMatrix
                    mlProb01={mlFromLatestLog}
                    mlPercentLabel={mlPctLabel}
                    rsi={rsiForGauge}
                  />
                  <IndicatorHub telemetry={telemetry} />
                </aside>
              </div>

              <section className="w-full min-w-0 shrink-0">
                <LogsTable rows={logsForTable} maxRows={LIVE_LOGS_N} />
              </section>
              <section className="w-full min-w-0 shrink-0">
                <AiAuditPanel
                  rows={tradeOutcomes}
                  analytics={analyticsOutcomes}
                  isLoading={tradeOutcomesLoading}
                  metricsLoading={analyticsOutcomesLoading}
                />
              </section>
            </div>
          </>
        )}
      </motion.div>
    </div>
  );
}
