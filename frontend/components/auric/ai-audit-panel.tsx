"use client";

import { useMemo, useState } from "react";
import { ShieldCheck } from "lucide-react";

import { Badge } from "@/components/ui/badge";
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
  return `${sign}${v.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} USDC`;
}

function fmtSignedPct(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${Number(v).toFixed(2)}%`;
}

function fmtClosedAt(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("pt-PT", { hour12: false });
}

function sideBadgeClass(side: string): string {
  return String(side).toUpperCase() === "LONG"
    ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-400"
    : "border-rose-500/45 bg-rose-500/10 text-rose-400";
}

function toFinite(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
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
            PnL Acumulado (USDC)
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

      <div className="space-y-2">
        {isLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-20 w-full rounded-xl" />
            ))}
          </div>
        ) : rows.length === 0 ? (
          <div className="rounded-xl border border-[#27272a] bg-[#0f0f11] p-4 text-sm text-zinc-500">
            Sem trades fechados no Outcome Engine ainda.
          </div>
        ) : (
          rows.map((r) => {
            const pnl = toFinite(r.pnl_usdc ?? r.pnl_realized ?? null);
            const roi = toFinite(r.roi_pct ?? r.final_roi ?? null);
            const motivo = (r.motivo_fecho ?? r.exit_type ?? "—").trim() || "—";
            const isWin = (roi ?? pnl ?? 0) > 0;
            const isLoss = (roi ?? pnl ?? 0) < 0;
            const just = (r.claude_justification ?? "").trim();
            return (
              <button
                type="button"
                key={r.order_id}
                onClick={() =>
                  setSelectedOrderId((prev) => (prev === r.order_id ? null : r.order_id))
                }
                className={cn(
                  "w-full rounded-xl border border-[#27272a] bg-[#0f0f11] p-3 text-left transition-all",
                  "hover:border-zinc-600/80 hover:bg-zinc-900/50",
                  selectedOrderId === r.order_id && "border-sky-500/40 bg-zinc-900/70"
                )}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="space-y-1">
                    <p className="font-mono text-[11px] text-zinc-400">
                      {fmtClosedAt(r.closed_at)}
                    </p>
                    <div className="flex items-center gap-2">
                      <Badge
                        variant="outline"
                        className={cn(
                          "border px-2 py-0 text-[10px] font-semibold tracking-wide",
                          sideBadgeClass(r.side)
                        )}
                      >
                        {String(r.side).toUpperCase()}
                      </Badge>
                      <span className="text-xs text-zinc-500">{r.symbol || "ETH/USDC"}</span>
                    </div>
                    <p className="text-[11px] text-gray-400">
                      motivo_fecho: <span className="text-zinc-300">{motivo}</span>
                    </p>
                  </div>
                  <div className="text-right">
                    <p
                      className={cn(
                        "font-mono text-base font-semibold",
                        isWin && "text-emerald-500",
                        isLoss && "text-rose-500",
                        !isWin && !isLoss && "text-zinc-300"
                      )}
                    >
                      {fmtSignedPct(roi)}
                    </p>
                    <p
                      className={cn(
                        "font-mono text-sm",
                        isWin && "text-emerald-500",
                        isLoss && "text-rose-500",
                        !isWin && !isLoss && "text-zinc-400"
                      )}
                    >
                      {fmtSignedUsd(pnl)}
                    </p>
                  </div>
                </div>
                <p className="mt-2 text-xs text-zinc-500">
                  {just
                    ? "Clique para ver justificativa completa e probabilidade ML"
                    : "Sem justificativa IA"}
                </p>
              </button>
            );
          })
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
