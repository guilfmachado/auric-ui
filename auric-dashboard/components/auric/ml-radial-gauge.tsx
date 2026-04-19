"use client";

import { useId, useMemo } from "react";
import { motion } from "framer-motion";

import { cn } from "@/lib/utils";

type Props = {
  /** Probabilidade em [0, 1] para o arco do gauge. */
  value: number | null;
  /** Rótulo explícito (ex.: `(p*100).toFixed(1)+'%'` do último log). */
  percentLabel?: string | null;
  className?: string;
};

const R = 44;
const C = 2 * Math.PI * R;

const SHORT_MAX = 0.4;
const LONG_MIN = 0.6;

function mlZone(p: number): "bear" | "bull" | "neutral" {
  if (p < SHORT_MAX) return "bear";
  if (p > LONG_MIN) return "bull";
  return "neutral";
}

export function MlRadialGauge({ value, percentLabel, className }: Props) {
  const gid = useId().replace(/:/g, "");
  const gradId = `auric-ml-grad-${gid}`;
  const loading = value == null || Number.isNaN(value);
  const p = loading ? 0 : Math.min(1, Math.max(0, value));
  const zone = loading ? "neutral" : mlZone(p);
  const offset = C * (1 - p);

  const { stopA, stopB, textClass } = useMemo(() => {
    if (zone === "bear") {
      return {
        stopA: "#fb7185",
        stopB: "#f43f5e",
        textClass: "text-[#ff4d6d] drop-shadow-[0_0_12px_rgba(255,45,86,0.55)]",
      };
    }
    if (zone === "bull") {
      return {
        stopA: "#4ade80",
        stopB: "#22c55e",
        textClass: "text-[#5cff5c] drop-shadow-[0_0_12px_rgba(57,255,20,0.45)]",
      };
    }
    return {
      stopA: "oklch(0.65 0.12 250)",
      stopB: "oklch(0.55 0.1 250)",
      textClass: "text-sky-400/90",
    };
  }, [zone]);

  return (
    <div className={className}>
      <svg
        viewBox="0 0 120 120"
        className="mx-auto size-40 sm:size-44"
        aria-hidden
      >
        <defs>
          <linearGradient id={gradId} x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor={stopA} />
            <stop offset="100%" stopColor={stopB} />
          </linearGradient>
        </defs>
        <circle
          cx="60"
          cy="60"
          r={R}
          fill="none"
          className="stroke-white/5"
          strokeWidth="10"
        />
        <motion.circle
          cx="60"
          cy="60"
          r={R}
          fill="none"
          stroke={loading ? "oklch(0.35 0 0)" : `url(#${gradId})`}
          strokeWidth="10"
          strokeLinecap="round"
          strokeDasharray={C}
          initial={{ strokeDashoffset: C }}
          animate={{ strokeDashoffset: offset }}
          transition={{ type: "spring", stiffness: 60, damping: 18 }}
          transform="rotate(-90 60 60)"
          className={cn(
            !loading &&
              zone === "bear" &&
              "drop-shadow-[0_0_10px_rgba(255,45,86,0.45)]",
            !loading &&
              zone === "bull" &&
              "drop-shadow-[0_0_10px_rgba(57,255,20,0.4)]"
          )}
        />
      </svg>
      <div className="mt-2 flex flex-col items-center gap-0.5 pb-1 text-center">
        {loading ? (
          <span className="text-sm font-medium tracking-tight text-zinc-500">
            Carregando…
          </span>
        ) : (
          <motion.span
            key={percentLabel ?? String(p)}
            initial={{ opacity: 0.65, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ type: "spring", stiffness: 280, damping: 24 }}
            className={cn(
              "inline-block text-3xl font-semibold tabular-nums tracking-tight",
              textClass
            )}
            style={{
              fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
            }}
          >
            {percentLabel?.trim()
              ? percentLabel.trim()
              : `${(p * 100).toFixed(1)}%`}
          </motion.span>
        )}
        <span className="text-[11px] uppercase tracking-[0.2em] text-zinc-500">
          P(alta) ML · log recente
        </span>
      </div>
    </div>
  );
}
