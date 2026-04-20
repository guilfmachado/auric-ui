"use client";

import { useMemo, useState } from "react";
import { ShieldCheck } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import type { AnalyticsOutcomesRow, TradeOutcomeRow } from "@/lib/types/auric";
import { cn } from "@/lib/utils";

import { TerminalCard } from "./terminal-card";

type Props = {
  rows: TradeOutcomeRow[];
  analytics: AnalyticsOutcomesRow | null;
  isLoading?: boolean;
  metricsLoading?: boolean;
};

function fmtSignedUsd(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}$${v.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function fmtClosedAt(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("pt-PT", { hour12: false });
}

export function AiAuditPanel({
  rows,
  analytics,
  isLoading,
  metricsLoading,
}: Props) {
  const [selectedOrderId, setSelectedOrderId] = useState<string | null>(null);
  const closedCount = rows.length;
  const pnlSum = rows.reduce((acc, r) => {
    const v = Number(r.pnl_realized ?? 0);
    return Number.isFinite(v) ? acc + v : acc;
  }, 0);
  const wins = rows.filter((r) => Number(r.pnl_realized ?? 0) > 0).length;
  const winRate = closedCount > 0 ? (wins / closedCount) * 100 : 0;
  const selected = useMemo(
    () => rows.find((r) => r.order_id === selectedOrderId) ?? null,
    [rows, selectedOrderId]
  );

  const winRateGlobal =
    Number(
      analytics?.win_rate_real ??
        analytics?.win_rate ??
        null
    ) || winRate;
  const pnlGlobal =
    Number(analytics?.pnl_accumulated ?? analytics?.pnl_acumulado ?? null) ||
    pnlSum;
  const totalGlobal =
    Number(analytics?.total_trades ?? analytics?.trades_total ?? null) ||
    closedCount;

  return (
    <TerminalCard className="space-y-4">
      <div className="flex items-center gap-2 border-b border-[#27272a] pb-3">
        <ShieldCheck className="size-4 text-sky-400" />
        <h2 className="text-[11px] font-semibold tracking-[0.18em] text-zinc-400 uppercase">
          AI Audit &amp; Realized PnL
        </h2>
      </div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <div className="rounded-lg border border-[#27272a] bg-[#09090b]/60 p-3">
          <p className="text-[10px] tracking-[0.16em] text-zinc-500 uppercase">
            Win Rate Real (%)
          </p>
          {metricsLoading ? (
            <Skeleton className="mt-2 h-7 w-24 rounded-md" />
          ) : (
            <p className="mt-1 font-mono text-2xl text-emerald-300">
              {winRateGlobal.toFixed(1)}%
            </p>
          )}
        </div>
        <div className="rounded-lg border border-[#27272a] bg-[#09090b]/60 p-3">
          <p className="text-[10px] tracking-[0.16em] text-zinc-500 uppercase">
            PnL Acumulado (USDT)
          </p>
          {metricsLoading ? (
            <Skeleton className="mt-2 h-7 w-28 rounded-md" />
          ) : (
            <p
              className={cn(
                "mt-1 font-mono text-2xl",
                pnlGlobal > 0
                  ? "text-emerald-300"
                  : pnlGlobal < 0
                    ? "text-red-300"
                    : "text-zinc-300"
              )}
            >
              {fmtSignedUsd(pnlGlobal)}
            </p>
          )}
        </div>
        <div className="rounded-lg border border-[#27272a] bg-[#09090b]/60 p-3">
          <p className="text-[10px] tracking-[0.16em] text-zinc-500 uppercase">
            Total de Trades Fechados
          </p>
          {metricsLoading ? (
            <Skeleton className="mt-2 h-7 w-20 rounded-md" />
          ) : (
            <p className="mt-1 font-mono text-2xl text-zinc-100">{totalGlobal}</p>
          )}
        </div>
      </div>

      <div className="overflow-hidden rounded-lg border border-[#27272a]">
        <div className="grid grid-cols-12 bg-[#111113] px-3 py-2 text-[10px] font-semibold tracking-[0.14em] text-zinc-500 uppercase">
          <div className="col-span-3">Data</div>
          <div className="col-span-2">Tipo</div>
          <div className="col-span-2">PnL</div>
          <div className="col-span-5">Auditoria IA</div>
        </div>

        {isLoading ? (
          <div className="space-y-2 p-3">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full rounded-md" />
            ))}
          </div>
        ) : rows.length === 0 ? (
          <div className="p-4 text-sm text-zinc-500">
            Sem trades fechados no Outcome Engine ainda.
          </div>
        ) : (
          <div className="max-h-96 overflow-auto">
            {rows.map((r) => {
              const pnl = Number(r.pnl_realized ?? 0);
              const pnlTxt = fmtSignedUsd(Number.isFinite(pnl) ? pnl : null);
              const isWin = pnl > 0;
              const isLoss = pnl < 0;
              const just = (r.claude_justification ?? "").trim();
              return (
                <button
                  type="button"
                  key={r.order_id}
                  onClick={() =>
                    setSelectedOrderId((prev) => (prev === r.order_id ? null : r.order_id))
                  }
                  className={cn(
                    "grid w-full grid-cols-12 items-start border-t border-[#27272a] px-3 py-2 text-left text-sm transition",
                    "hover:bg-zinc-900/50",
                    selectedOrderId === r.order_id && "bg-zinc-900/70"
                  )}
                >
                  <div className="col-span-3 font-mono text-xs text-zinc-300">
                    {fmtClosedAt(r.closed_at)}
                  </div>
                  <div className="col-span-2">
                    <span
                      className={cn(
                        "rounded-md border px-2 py-0.5 text-[11px] font-bold tracking-wide",
                        String(r.side).toUpperCase() === "LONG"
                          ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
                          : "border-red-500/40 bg-red-500/15 text-red-300"
                      )}
                    >
                      {String(r.side).toUpperCase()}
                    </span>
                  </div>
                  <div
                    className={cn(
                      "col-span-2 font-mono text-xs font-semibold",
                      isWin && "text-emerald-300",
                      isLoss && "text-red-300",
                      !isWin && !isLoss && "text-zinc-400"
                    )}
                  >
                    {pnlTxt}
                  </div>
                  <div className="col-span-5 text-xs text-zinc-400">
                    {just
                      ? "Clique para ver justificativa completa e probabilidade ML"
                      : "Sem justificativa IA"}
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </div>

      <div className="rounded-lg border border-zinc-700/60 bg-zinc-900/50 p-3">
        <p className="text-[10px] tracking-[0.14em] text-zinc-500 uppercase">
          Drill-down da decisão
        </p>
        {selected ? (
          <div className="mt-2 space-y-2">
            <p className="text-xs text-zinc-300">
              <span className="font-semibold text-zinc-200">Order ID:</span>{" "}
              <span className="font-mono">{selected.order_id}</span>
            </p>
            <p className="text-xs text-zinc-300">
              <span className="font-semibold text-zinc-200">ML na entrada:</span>{" "}
              <span className="font-mono">
                {selected.ml_probability_at_entry == null ||
                Number.isNaN(Number(selected.ml_probability_at_entry))
                  ? "—"
                  : `${(Number(selected.ml_probability_at_entry) <= 1
                      ? Number(selected.ml_probability_at_entry) * 100
                      : Number(selected.ml_probability_at_entry)
                    ).toFixed(2)}%`}
              </span>
            </p>
            <p className="whitespace-pre-wrap text-xs leading-relaxed text-zinc-400">
              {selected.claude_justification?.trim() || "Sem justificativa IA."}
            </p>
          </div>
        ) : (
          <p className="mt-2 text-xs text-zinc-500">
            Clique numa linha da tabela para auditar a justificativa completa da IA.
          </p>
        )}
      </div>
    </TerminalCard>
  );
}
