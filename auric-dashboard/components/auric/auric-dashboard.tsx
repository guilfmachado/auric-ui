"use client";

import { motion } from "framer-motion";

import { Skeleton } from "@/components/ui/skeleton";
import { useAuricDashboard } from "@/hooks/use-auric-dashboard";

import { ProfitChart } from "@/components/ProfitChart";

import { AuricHeader } from "./auric-header";
import { LiveMonitor } from "./live-monitor";
import { StatsGrid } from "./stats-grid";
import { TradeLogsTable } from "./trade-logs-table";

function formatUsd(n: number | null | undefined) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
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

export function AuricDashboard() {
  const {
    ready,
    error,
    supabaseReady,
    config,
    logs,
    latestLog,
    pingMs,
    trades24hComputed,
    switchBusy,
    setTradingMode,
  } = useAuricDashboard();

  const rawMl =
    latestLog?.probabilidade_ml ?? config.ml_probability ?? null;
  const mlClamped =
    rawMl === null || Number.isNaN(Number(rawMl))
      ? 0.5
      : Math.min(1, Math.max(0, Number(rawMl)));

  const verdict =
    (latestLog?.sentimento_ia && latestLog.sentimento_ia.trim()) ||
    config.verdict_ia ||
    "NEUTRAL";

  const justification =
    (latestLog?.justificativa && latestLog.justificativa.trim()) ||
    config.justificativa_curta ||
    "";

  const balanceStr =
    config.balance_usdt !== null && config.balance_usdt !== undefined
      ? `${formatUsd(config.balance_usdt)} USDT`
      : "—";

  const pnl = config.pnl_day_pct;
  const pnlPositive =
    pnl === null || pnl === undefined ? null : pnl > 0 ? true : pnl < 0 ? false : null;

  const tradesStr = String(
    config.trades_24h ?? trades24hComputed ?? "—"
  );

  const winRateStr =
    config.xgboost_accuracy !== null &&
    config.xgboost_accuracy !== undefined
      ? `${(config.xgboost_accuracy <= 1
          ? config.xgboost_accuracy * 100
          : config.xgboost_accuracy
        ).toFixed(1)}%`
      : "—";

  if (!ready) {
    return (
      <div className="mx-auto max-w-6xl space-y-8 px-4 py-10 sm:px-6">
        <Skeleton className="h-24 w-full rounded-xl bg-zinc-800/80" />
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-28 rounded-xl bg-zinc-800/80" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.4 }}
      className="mx-auto min-h-screen max-w-6xl space-y-10 px-4 py-8 sm:px-6 lg:py-12"
    >
      {!supabaseReady && (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-200/90">
          {error}
        </div>
      )}

      {supabaseReady && error && (
        <div className="rounded-lg border border-red-500/35 bg-red-950/40 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      <AuricHeader
        pingMs={pingMs}
        tradingMode={config.modo_operacao ?? config.trading_mode}
        onTradingModeChange={setTradingMode}
        busy={switchBusy}
      />

      <StatsGrid
        balance={balanceStr}
        pnlDay={formatPct(pnl ?? null)}
        pnlPositive={pnlPositive}
        trades24h={tradesStr}
        winRate={winRateStr}
      />

      <LiveMonitor
        mlProb={mlClamped}
        verdict={verdict}
        justification={justification}
      />

      <section className="rounded-xl border border-zinc-800/80 bg-zinc-950/40 p-4 sm:p-6">
        <h2 className="text-xs font-bold tracking-widest text-zinc-500 uppercase">
          Curva de lucro (acumulado %)
        </h2>
        <p className="mt-1 text-[11px] text-zinc-600">
          Usa <span className="font-mono text-zinc-500">resultado_trade</span> no
          Supabase quando existir; caso contrário, heurística TP +2% / SL −1% em{" "}
          <span className="font-mono text-zinc-500">VENDA_PROFIT</span> /{" "}
          <span className="font-mono text-zinc-500">VENDA_STOP</span>.
        </p>
        <ProfitChart data={logs} />
      </section>

      <TradeLogsTable rows={logs} />
    </motion.div>
  );
}
