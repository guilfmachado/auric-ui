"use client";

import { SlidersHorizontal } from "lucide-react";

import type { BotConfigRow } from "@/lib/types/auric";
import { cn } from "@/lib/utils";

import { Skeleton } from "@/components/ui/skeleton";

import { TerminalCard } from "./terminal-card";

type Props = {
  config: BotConfigRow;
  loading?: boolean;
  isSyncing: boolean;
  syncError: string | null;
  showSynced: boolean;
  onPatch: (
    patch: Partial<Pick<BotConfigRow, "leverage" | "risk_fraction" | "trailing_callback_rate">>
  ) => void;
};

function SliderRow({
  label,
  valueLabel,
  min,
  max,
  step,
  value,
  onChange,
  accentClass,
}: {
  label: string;
  valueLabel: string;
  min: number;
  max: number;
  step: number;
  value: number;
  onChange: (v: number) => void;
  accentClass: string;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <p className="text-[10px] tracking-[0.16em] text-zinc-500 uppercase">{label}</p>
        <p className={cn("font-mono text-sm font-semibold", accentClass)}>{valueLabel}</p>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className={cn(
          "h-1.5 w-full cursor-pointer appearance-none rounded-full bg-zinc-800",
          "[&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:w-3.5 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full",
          "[&::-webkit-slider-thumb]:bg-current [&::-webkit-slider-thumb]:shadow-[0_0_10px_currentColor]",
          accentClass
        )}
      />
    </div>
  );
}

export function TacticalOverridePanel({
  config,
  loading,
  isSyncing,
  syncError,
  showSynced,
  onPatch,
}: Props) {
  const leverage = Math.max(1, Math.min(20, Math.round(Number(config.leverage ?? 3))));
  const riskPct = Math.max(
    1,
    Math.min(100, Number(((Number(config.risk_fraction ?? 0.1)) * 100).toFixed(1)))
  );
  const trailing = Math.max(
    0.1,
    Math.min(5.0, Number((Number(config.trailing_callback_rate ?? 0.6)).toFixed(1)))
  );

  return (
    <TerminalCard className="space-y-4">
      <div className="flex items-center justify-between border-b border-[#27272a] pb-3">
        <div className="flex items-center gap-2">
          <SlidersHorizontal className="size-4 text-cyan-400" />
          <h2 className="text-[11px] font-semibold tracking-[0.18em] text-zinc-400 uppercase">
            Tactical Override
          </h2>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "inline-flex h-2.5 w-2.5 rounded-full",
              isSyncing && "animate-pulse bg-amber-400",
              showSynced && "animate-pulse bg-emerald-400",
              syncError && "bg-red-400",
              !isSyncing && !showSynced && !syncError && "bg-zinc-600"
            )}
          />
          <span
            className={cn(
              "text-[10px] tracking-[0.12em] uppercase",
              isSyncing && "text-amber-300",
              showSynced && "text-emerald-300",
              syncError && "text-red-300",
              !isSyncing && !showSynced && !syncError && "text-zinc-500"
            )}
          >
            {isSyncing
              ? "Syncing..."
              : showSynced
                ? "✅ Synced"
                : syncError
                  ? "Sync Error"
                  : "Idle"}
          </span>
        </div>
      </div>

      {loading ? (
        <div className="space-y-4">
          <Skeleton className="h-10 w-full rounded-md" />
          <Skeleton className="h-10 w-full rounded-md" />
          <Skeleton className="h-10 w-full rounded-md" />
        </div>
      ) : (
        <div className="space-y-5">
          <SliderRow
            label="Leverage"
            valueLabel={`${leverage}x`}
            min={1}
            max={20}
            step={1}
            value={leverage}
            onChange={(v) => onPatch({ leverage: v })}
            accentClass="text-cyan-300"
          />
          <SliderRow
            label="Risk per Trade"
            valueLabel={`${riskPct.toFixed(1)}%`}
            min={1}
            max={100}
            step={0.5}
            value={riskPct}
            onChange={(v) => onPatch({ risk_fraction: v / 100 })}
            accentClass="text-violet-300"
          />
          <SliderRow
            label="Trailing Stop (Callback Rate)"
            valueLabel={`${trailing.toFixed(1)}%`}
            min={0.1}
            max={5}
            step={0.1}
            value={trailing}
            onChange={(v) => onPatch({ trailing_callback_rate: v })}
            accentClass="text-emerald-300"
          />
        </div>
      )}
      {syncError && (
        <p className="text-xs text-red-400">
          {syncError}
        </p>
      )}
    </TerminalCard>
  );
}
