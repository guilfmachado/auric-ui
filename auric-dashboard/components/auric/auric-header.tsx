"use client";

import { Activity, Radio } from "lucide-react";
import { motion } from "framer-motion";

import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import type { TradingMode } from "@/lib/types/auric";

type Props = {
  pingMs: number | null;
  tradingMode: TradingMode;
  onTradingModeChange: (mode: TradingMode) => void;
  busy?: boolean;
};

export function AuricHeader({
  pingMs,
  tradingMode,
  onTradingModeChange,
  busy,
}: Props) {
  const isFutures = tradingMode === "FUTURES";

  return (
    <motion.header
      initial={{ opacity: 0, y: -12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
      className="flex flex-col gap-6 border-b border-white/[0.06] pb-8 lg:flex-row lg:items-center lg:justify-between"
    >
      <div className="flex items-center gap-4">
        <div className="flex size-11 items-center justify-center rounded-xl border border-emerald-500/25 bg-emerald-500/10">
          <Radio className="size-5 text-emerald-400" strokeWidth={1.75} />
        </div>
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-50">
            Auric
          </h1>
          <p className="text-sm text-zinc-500">
            Quant desk · monitoramento em tempo real
          </p>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-6 lg:gap-10">
        <div className="flex items-center gap-2 rounded-full border border-white/[0.08] bg-zinc-900/80 px-4 py-2">
          <Activity
            className={cn(
              "size-4",
              pingMs !== null ? "text-emerald-400" : "text-zinc-600"
            )}
          />
          <span className="text-xs font-medium uppercase tracking-wider text-zinc-500">
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
          >
            {pingMs !== null ? `${pingMs} ms` : "—"}
          </span>
        </div>

        <div className="flex items-center gap-4">
          <span
            className={cn(
              "text-xs font-semibold uppercase tracking-[0.15em] transition-colors",
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
            className="h-7 w-12 scale-110 data-checked:border-emerald-500/40 data-checked:bg-emerald-600/90 data-unchecked:bg-zinc-800 dark:data-unchecked:bg-zinc-800"
          />
          <span
            className={cn(
              "text-xs font-semibold uppercase tracking-[0.15em] transition-colors",
              isFutures ? "text-emerald-400" : "text-zinc-600"
            )}
          >
            Futures
          </span>
        </div>
      </div>
    </motion.header>
  );
}
