"use client";

import { Activity, AlertTriangle, Loader2, Radio, Zap } from "lucide-react";
import { motion } from "framer-motion";

import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import type { TradingMode } from "@/lib/types/auric";

type Props = {
  pingMs: number | null;
  tradingMode: TradingMode;
  onTradingModeChange: (mode: TradingMode) => void;
  busy?: boolean;
  botActive: boolean;
  botBusy?: boolean;
  onBotToggle?: (active: boolean) => void;
  trades24h?: string;
  winRate?: string;
  onManualLong?: () => void;
  onManualShort?: () => void;
  onManualCloseAll?: () => void;
  manualPending?: "LONG" | "SHORT" | "CLOSE_ALL" | null;
};

export function AuricHeader({
  pingMs,
  tradingMode,
  onTradingModeChange,
  busy,
  botActive,
  botBusy,
  onBotToggle,
  trades24h,
  winRate,
  onManualLong,
  onManualShort,
  onManualCloseAll,
  manualPending,
}: Props) {
  const isFutures = tradingMode === "FUTURES";

  return (
    <motion.header
      initial={{ opacity: 0, y: -12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
      className="flex flex-col gap-6 border-b border-[#27272a] pb-8 lg:flex-row lg:items-center lg:justify-between"
    >
      <div className="flex items-center gap-4">
        <div className="flex size-11 items-center justify-center rounded-xl border border-[#27272a] bg-[#18181b]">
          <Radio className="size-5 text-emerald-500/90" strokeWidth={1.75} />
        </div>
        <div>
          <h1
            className="text-2xl font-semibold tracking-tight text-zinc-50"
            style={{
              fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
            }}
          >
            AURIC
            <span className="text-emerald-500"> {"//"} FINAL BOSS</span>
          </h1>
          <p className="text-xs tracking-wide text-zinc-500">
            Terminal quant · midnight desk
          </p>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-4 lg:gap-5">
        {onBotToggle && (
          <div className="flex items-center gap-3 rounded-full border border-[#27272a] bg-[#18181b] px-4 py-2">
            <div className="relative flex items-center gap-2">
              <span className="relative flex h-2.5 w-2.5">
                {botActive ? (
                  <>
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-70" />
                    <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-emerald-500 shadow-[0_0_10px_rgba(34,197,94,0.9)]" />
                  </>
                ) : (
                  <span className="h-2.5 w-2.5 rounded-full bg-zinc-600" />
                )}
              </span>
              <Zap
                className={cn(
                  "size-4",
                  botActive ? "text-emerald-400" : "text-zinc-600"
                )}
              />
            </div>
            <span className="text-[10px] font-semibold tracking-[0.2em] text-zinc-500 uppercase">
              Bot
            </span>
            <Switch
              checked={botActive}
              disabled={botBusy}
              onCheckedChange={(v) => onBotToggle(v)}
              className="h-7 w-12 data-checked:border-emerald-500/50 data-checked:bg-emerald-600 data-unchecked:bg-zinc-800"
            />
            <span
              className={cn(
                "min-w-[2.75rem] text-[10px] font-bold tracking-widest",
                botActive ? "text-emerald-400" : "text-red-400/90"
              )}
              style={{
                fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
              }}
            >
              {botActive ? "RUN" : "STBY"}
            </span>
          </div>
        )}

        {onManualLong && onManualShort && (
          <div className="flex flex-col items-start gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded-md border border-zinc-700/70 bg-zinc-900/70 px-2 py-1 text-[9px] font-bold tracking-[0.18em] text-zinc-400 uppercase">
                Terminal Institucional · Manual Override
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                disabled={manualPending !== null}
                onClick={onManualLong}
                className={cn(
                  "inline-flex min-w-[10rem] items-center justify-center gap-2 rounded-lg border border-emerald-400/80 bg-emerald-500/15 px-4 py-2 text-[11px] font-black tracking-[0.15em] text-emerald-200 uppercase",
                  "shadow-[0_0_24px_rgba(16,185,129,0.45)] transition hover:border-emerald-300 hover:bg-emerald-500/25 hover:shadow-[0_0_36px_rgba(16,185,129,0.6)]",
                  "disabled:pointer-events-none disabled:opacity-50"
                )}
                style={{
                  fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
                }}
              >
                {manualPending === "LONG" ? (
                  <>
                    <Loader2 className="size-3.5 animate-spin text-emerald-300" />
                    A executar...
                  </>
                ) : (
                  <>
                    <Zap className="size-3.5 text-emerald-300" />
                    FORCE LONG
                  </>
                )}
              </button>
              <button
                type="button"
                disabled={manualPending !== null}
                onClick={onManualShort}
                className={cn(
                  "inline-flex min-w-[10rem] items-center justify-center gap-2 rounded-lg border border-red-400/80 bg-red-500/15 px-4 py-2 text-[11px] font-black tracking-[0.15em] text-red-200 uppercase",
                  "shadow-[0_0_24px_rgba(248,113,113,0.45)] transition hover:border-red-300 hover:bg-red-500/25 hover:shadow-[0_0_36px_rgba(248,113,113,0.6)]",
                  "disabled:pointer-events-none disabled:opacity-50"
                )}
                style={{
                  fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
                }}
              >
                {manualPending === "SHORT" ? (
                  <>
                    <Loader2 className="size-3.5 animate-spin text-red-300" />
                    A executar...
                  </>
                ) : (
                  <>
                    <Zap className="size-3.5 text-red-300" />
                    FORCE SHORT
                  </>
                )}
              </button>
            </div>
            {onManualCloseAll && (
              <button
                type="button"
                disabled={manualPending !== null}
                onClick={onManualCloseAll}
                className={cn(
                  "inline-flex min-w-[10rem] items-center justify-center gap-2 rounded-md border border-amber-500/70 bg-amber-500/10 px-3 py-1.5 text-[10px] font-bold tracking-[0.14em] text-amber-200 uppercase",
                  "shadow-[0_0_16px_rgba(245,158,11,0.25)] transition hover:border-amber-400 hover:bg-amber-500/20",
                  "disabled:pointer-events-none disabled:opacity-50"
                )}
                style={{
                  fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
                }}
              >
                {manualPending === "CLOSE_ALL" ? (
                  <>
                    <Loader2 className="size-3.5 animate-spin text-amber-300" />
                    A executar...
                  </>
                ) : (
                  <>
                    <AlertTriangle className="size-3.5 text-amber-300" />
                    PANIC CLOSE ALL
                  </>
                )}
              </button>
            )}
          </div>
        )}

        <div className="flex items-center gap-2 rounded-full border border-[#27272a] bg-[#18181b] px-4 py-2">
          <Activity
            className={cn(
              "size-4",
              pingMs !== null ? "text-emerald-400" : "text-zinc-600"
            )}
          />
          <span className="text-[10px] font-semibold tracking-[0.18em] text-zinc-500 uppercase">
            Ping
          </span>
          <span
            className={cn(
              "font-mono text-sm tabular-nums",
              pingMs !== null && pingMs < 400
                ? "text-emerald-400"
                : pingMs !== null
                  ? "text-amber-400"
                  : "text-zinc-600"
            )}
            style={{
              fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
            }}
          >
            {pingMs !== null ? `${pingMs} ms` : "—"}
          </span>
        </div>

        {(trades24h || winRate) && (
          <div className="hidden items-center gap-3 rounded-full border border-[#27272a] bg-[#18181b] px-4 py-2 font-mono text-[10px] text-zinc-400 xl:flex">
            {trades24h != null && (
              <span>
                <span className="text-zinc-600">24h</span>{" "}
                <span className="text-zinc-200">{trades24h}</span>
              </span>
            )}
            {winRate != null && trades24h != null && (
              <span className="text-zinc-700">|</span>
            )}
            {winRate != null && (
              <span>
                <span className="text-zinc-600">WR</span>{" "}
                <span className="text-zinc-200">{winRate}</span>
              </span>
            )}
          </div>
        )}

        <div className="flex items-center gap-4">
          <span
            className={cn(
              "text-[10px] font-semibold tracking-[0.2em] transition-colors",
              !isFutures ? "text-zinc-100" : "text-zinc-600"
            )}
          >
            Spot
          </span>
          <Switch
            checked={isFutures}
            disabled={busy}
            onCheckedChange={(checked) =>
              onTradingModeChange(checked ? "FUTURES" : "SPOT")
            }
            className="h-7 w-12 data-checked:border-orange-500/40 data-checked:bg-orange-600/90 data-unchecked:bg-zinc-800"
          />
          <span
            className={cn(
              "text-[10px] font-semibold tracking-[0.2em] transition-colors",
              isFutures ? "text-orange-400" : "text-zinc-600"
            )}
          >
            Futures
          </span>
        </div>
      </div>
    </motion.header>
  );
}
