"use client";

import { Layers } from "lucide-react";
import { motion } from "framer-motion";

import { cn } from "@/lib/utils";

import type { ParsedTelemetry } from "@/lib/auric/parse-telemetry";

import { TerminalCard } from "./terminal-card";

const ADX_STRONG = 25;
const RSI_WARN = 35;

type Props = {
  telemetry: ParsedTelemetry;
};

type PillTone =
  | "cyan"
  | "violet"
  | "amber"
  | "zinc"
  | "yellow"
  | "blueBright";

function Pill({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: PillTone;
}) {
  const tones: Record<PillTone, string> = {
    cyan: "border-cyan-500/30 bg-cyan-500/10 text-cyan-200/90",
    violet: "border-violet-500/30 bg-violet-500/10 text-violet-200/90",
    amber: "border-amber-500/35 bg-amber-500/10 text-amber-200/90",
    zinc: "border-zinc-600 bg-zinc-800/80 text-zinc-400",
    yellow:
      "border-amber-400/60 bg-amber-500/20 text-amber-100 shadow-[0_0_18px_rgba(250,204,21,0.28)]",
    blueBright:
      "border-sky-400/75 bg-sky-500/25 text-sky-50 shadow-[0_0_22px_rgba(56,189,248,0.45)]",
  };
  return (
    <div
      className={cn(
        "flex min-w-0 flex-col gap-1 rounded-lg border px-3 py-2 transition-colors duration-300",
        tones[tone]
      )}
    >
      <span className="text-[9px] font-semibold tracking-widest text-zinc-500 uppercase">
        {label}
      </span>
      <motion.span
        layout
        key={value}
        initial={{ opacity: 0.5, y: 3 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 320, damping: 28 }}
        className="truncate font-mono text-[11px] font-medium text-zinc-100"
        style={{
          fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
        }}
      >
        {value}
      </motion.span>
    </div>
  );
}

export function IndicatorHub({ telemetry }: Props) {
  const adxVal = telemetry.adx;
  const adxStr =
    adxVal != null && !Number.isNaN(adxVal) ? adxVal.toFixed(2) : "—";
  const adxStrong =
    adxVal != null && !Number.isNaN(adxVal) && adxVal > ADX_STRONG;

  const rsiVal = telemetry.rsi;
  const rsiStr =
    rsiVal != null && !Number.isNaN(rsiVal) ? rsiVal.toFixed(2) : "—";
  const rsiWarn =
    rsiVal != null && !Number.isNaN(rsiVal) && rsiVal < RSI_WARN;
  const rsiDisplay =
    rsiStr !== "—" && rsiWarn ? `${rsiStr}  ⚠️` : rsiStr;

  const vwap = telemetry.vwapLabel ?? "—";
  const bb = telemetry.bollingerLabel ?? "—";

  const adxTone: PillTone =
    adxStrong ? "blueBright" : adxStr !== "—" ? "cyan" : "zinc";
  const rsiTone: PillTone = rsiWarn
    ? "yellow"
    : rsiStr !== "—"
      ? "cyan"
      : "zinc";

  return (
    <TerminalCard className="p-4">
      <div className="mb-3 flex items-center gap-2">
        <Layers className="size-4 text-zinc-500" />
        <span className="text-[10px] font-semibold tracking-[0.2em] text-zinc-500 uppercase">
          Indicator hub
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-2">
        <Pill label="ADX (14)" value={adxStr} tone={adxTone} />
        <Pill label="RSI (14)" value={rsiDisplay} tone={rsiTone} />
        <Pill label="VWAP" value={vwap} tone="violet" />
        <Pill label="Bollinger" value={bb} tone="amber" />
      </div>
    </TerminalCard>
  );
}
