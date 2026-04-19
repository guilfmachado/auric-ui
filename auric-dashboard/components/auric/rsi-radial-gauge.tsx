"use client";

import { motion } from "framer-motion";

type Props = {
  /** 0–100 ou null (mostra tracinho). */
  value: number | null;
  className?: string;
};

const R = 44;
const C = 2 * Math.PI * R;

export function RsiRadialGauge({ value, className }: Props) {
  const clamped =
    value == null || Number.isNaN(value)
      ? null
      : Math.min(100, Math.max(0, value));
  const frac = clamped == null ? 0 : clamped / 100;
  const offset = C * (1 - frac);

  const oversold = clamped != null && clamped < 30;
  const stroke =
    clamped == null
      ? "oklch(0.45 0 0)"
      : oversold
        ? "oklch(0.86 0.16 95)"
        : clamped > 70
          ? "oklch(0.7 0.2 55)"
          : "oklch(0.72 0.14 200)";

  return (
    <div className={className}>
      <svg
        viewBox="0 0 120 120"
        className="mx-auto size-40 sm:size-44"
        aria-hidden
      >
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
          stroke={stroke}
          strokeWidth="10"
          strokeLinecap="round"
          strokeDasharray={C}
          initial={{ strokeDashoffset: C }}
          animate={{ strokeDashoffset: offset }}
          transition={{ type: "spring", stiffness: 60, damping: 18 }}
          transform="rotate(-90 60 60)"
        />
      </svg>
      <div className="mt-2 flex flex-col items-center gap-0.5 pb-1 text-center">
        <span
          className={
            clamped == null
              ? "text-3xl font-semibold tabular-nums tracking-tight text-zinc-500"
              : oversold
                ? "text-3xl font-semibold tabular-nums tracking-tight text-yellow-400"
                : "text-3xl font-semibold tabular-nums tracking-tight text-zinc-200"
          }
          style={{
            fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
          }}
        >
          {clamped == null ? "…" : clamped.toFixed(2)}
        </span>
        <span className="text-[11px] uppercase tracking-[0.2em] text-zinc-500">
          RSI (14)
          {oversold ? (
            <span className="ml-1 text-yellow-500/90">· sobrevendido</span>
          ) : null}
        </span>
      </div>
    </div>
  );
}
