"use client";

import { motion } from "framer-motion";

type Props = {
  value: number;
  /** 0–1 */
  className?: string;
};

const R = 44;
const C = 2 * Math.PI * R;

export function MlRadialGauge({ value, className }: Props) {
  const clamped = Math.min(1, Math.max(0, value));
  const offset = C * (1 - clamped);

  return (
    <div className={className}>
      <svg
        viewBox="0 0 120 120"
        className="mx-auto size-40 sm:size-44"
        aria-hidden
      >
        <defs>
          <linearGradient id="auric-ml-grad" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="oklch(0.72 0.19 166)" />
            <stop offset="100%" stopColor="oklch(0.55 0.14 166)" />
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
          stroke="url(#auric-ml-grad)"
          strokeWidth="10"
          strokeLinecap="round"
          strokeDasharray={C}
          initial={{ strokeDashoffset: C }}
          animate={{ strokeDashoffset: offset }}
          transition={{ type: "spring", stiffness: 60, damping: 18 }}
          transform="rotate(-90 60 60)"
        />
      </svg>
      <div className="-mt-24 flex flex-col items-center pb-2 text-center">
        <span className="text-3xl font-semibold tabular-nums tracking-tight text-emerald-400">
          {(clamped * 100).toFixed(1)}%
        </span>
        <span className="text-[11px] uppercase tracking-[0.2em] text-zinc-500">
          confiança ML
        </span>
      </div>
    </div>
  );
}
