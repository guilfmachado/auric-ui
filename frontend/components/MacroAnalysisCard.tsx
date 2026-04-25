"use client";

import { useMemo } from "react";
import Typewriter from "typewriter-effect";
import { motion } from "framer-motion";

export interface MacroProps {
  macro_score: number;
  market_vibe?: string;
  reasoning_content?: string | string[];
  reasoningKey?: string | number;
}

function reasoningToStrings(
  raw: string | string[] | undefined,
): string[] {
  if (raw == null) return [];
  if (Array.isArray(raw)) {
    return raw.map((s) => String(s).trim()).filter(Boolean);
  }
  const t = String(raw).trim();
  return t ? [t] : [];
}

function clampScore(n: number): number {
  if (Number.isNaN(Number(n))) return 0;
  return Math.max(0, Math.min(100, n));
}

export const MacroAnalysisCard = ({
  macro_score,
  market_vibe,
  reasoning_content,
  reasoningKey,
}: MacroProps) => {
  const score = clampScore(macro_score);
  const strings = useMemo(
    () => reasoningToStrings(reasoning_content),
    [reasoning_content],
  );
  const hasReasoning = strings.length > 0;
  const typewriterKey =
    reasoningKey !== undefined && reasoningKey !== null
      ? String(reasoningKey)
      : hasReasoning
        ? `hash-${strings[0].length}-${strings[0].slice(0, 48)}`
        : "idle";

  const getScoreColor = (s: number) => {
    if (s >= 70) return "text-emerald-400 border-emerald-500/30";
    if (s >= 40) return "text-cyan-400 border-cyan-500/30";
    return "text-rose-400 border-rose-500/30";
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35 }}
      className={`p-6 bg-black/40 border ${getScoreColor(score)} rounded-xl backdrop-blur-md shadow-[0_0_20px_rgba(0,0,0,0.5)]`}
    >
      <div className="flex justify-between items-end mb-6">
        <div>
          <h3 className="text-[10px] uppercase tracking-[0.2em] opacity-60 font-bold">
            Macro Intelligence Score
          </h3>
          <p className="text-sm font-mono mt-1 opacity-90">
            {market_vibe?.trim() || "ANALYZING MARKET PULSE..."}
          </p>
        </div>
        <div className="text-4xl font-black font-mono tracking-tighter">
          {score}
        </div>
      </div>

      <div className="relative group">
        <div className="absolute -left-3 top-0 bottom-0 w-[2px] bg-cyan-500 shadow-[0_0_10px_rgba(6,182,212,0.5)]" />
        <div className="bg-cyan-950/20 p-4 rounded-r-lg min-h-[100px]">
          <div className="flex items-center gap-2 mb-2">
            <div className="w-1.5 h-1.5 rounded-full bg-cyan-400 animate-pulse" />
            <span className="text-[9px] uppercase tracking-widest text-cyan-400/70 font-bold">
              DeepSeek V4 Reasoning Path
            </span>
          </div>

          <div className="font-mono italic text-sm text-cyan-200/90 leading-relaxed [&_.Typewriter__cursor]:text-cyan-400">
            {hasReasoning ? (
              <Typewriter
                key={typewriterKey}
                options={{
                  strings,
                  autoStart: true,
                  delay: 15,
                  cursor: "▋",
                  wrapperClassName: "typewriter-text",
                }}
              />
            ) : (
              <span className="opacity-30">Aguardando telemetria de decisão...</span>
            )}
          </div>
        </div>
      </div>
    </motion.div>
  );
};
