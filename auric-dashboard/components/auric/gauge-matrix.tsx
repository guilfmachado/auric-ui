"use client";

import { Orbit } from "lucide-react";

import { CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

import { MlRadialGauge } from "./ml-radial-gauge";
import { RsiRadialGauge } from "./rsi-radial-gauge";
import { TerminalCard } from "./terminal-card";

type Props = {
  /** `latestLog.probabilidade_ml` [0,1], ou null. */
  mlProb01: number | null;
  /** Rótulo do centro (ex.: `(p*100).toFixed(1)+'%'`). */
  mlPercentLabel?: string | null;
  rsi: number | null;
};

const SHORT_MAX = 0.4;
const LONG_MIN = 0.6;

function mlCardZone(p: number | null): "bear" | "bull" | "neutral" | "loading" {
  if (p == null || Number.isNaN(p)) return "loading";
  if (p < SHORT_MAX) return "bear";
  if (p > LONG_MIN) return "bull";
  return "neutral";
}

export function GaugeMatrix({ mlProb01, mlPercentLabel, rsi }: Props) {
  const z = mlCardZone(mlProb01);

  return (
    <TerminalCard className="flex flex-col">
      <div className="mb-2 flex items-center gap-2 border-b border-[#27272a] pb-3">
        <Orbit className="size-4 text-cyan-500/80" />
        <CardTitle className="text-[11px] font-semibold tracking-[0.18em] text-zinc-500 uppercase">
          Gauge matrix
        </CardTitle>
      </div>
      <div className="grid flex-1 grid-cols-2 gap-2">
        <div
          className={cn(
            "flex flex-col items-center justify-center rounded-lg border py-4 transition-colors duration-300",
            "bg-[#09090b]/60",
            z === "loading" && "border-[#27272a]/80",
            z === "bear" &&
              "border-rose-500/70 bg-rose-950/25 shadow-[inset_0_0_28px_rgba(244,63,94,0.12),0_0_20px_rgba(255,32,86,0.18)]",
            z === "bull" &&
              "border-emerald-500/70 bg-emerald-950/20 shadow-[inset_0_0_28px_rgba(34,197,94,0.12),0_0_20px_rgba(57,255,20,0.15)]",
            z === "neutral" && "border-sky-500/40 bg-sky-950/15"
          )}
        >
          <MlRadialGauge value={mlProb01} percentLabel={mlPercentLabel} />
        </div>
        <div className="flex flex-col items-center justify-center rounded-lg border border-[#27272a]/80 bg-[#09090b]/60 py-4">
          <RsiRadialGauge value={rsi} />
        </div>
      </div>
    </TerminalCard>
  );
}
