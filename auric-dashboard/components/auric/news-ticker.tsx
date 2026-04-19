"use client";

import { useMemo } from "react";
import { motion } from "framer-motion";

import { cn } from "@/lib/utils";

type Props = {
  text: string;
  className?: string;
};

function splitNews(raw: string): string[] {
  const byNl = raw
    .split(/\n+/)
    .map((l) => l.trim())
    .filter(Boolean);
  if (byNl.length > 1) return byNl;
  const one = byNl[0] ?? "";
  if (!one) return [];
  if (one.length < 160) return [one];
  const chunks = one.split(/\.\s+/).map((s) => s.replace(/\s+$/, "").trim()).filter(Boolean);
  return chunks.length > 1 ? chunks.map((c) => (c.endsWith(".") ? c : `${c}.`)) : [one];
}

/** Lista rolável do que foi capturado em `noticias_agregadas`. */
export function NewsTicker({ text, className }: Props) {
  const lines = useMemo(() => splitNews(text), [text]);

  if (!lines.length) {
    return (
      <p
        className={cn(
          "rounded border border-dashed border-zinc-700/60 bg-black/30 px-3 py-2 text-[10px] text-zinc-600",
          className
        )}
        style={{
          fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
        }}
      >
        Nenhuma notícia agregada neste log.
      </p>
    );
  }

  return (
    <div
      className={cn(
        "relative overflow-hidden rounded border border-amber-500/20 bg-amber-950/10",
        className
      )}
    >
      <p className="border-b border-amber-500/15 px-3 py-1.5 text-[8px] font-semibold tracking-[0.2em] text-amber-500/80 uppercase">
        News feed
      </p>
      <ul className="max-h-32 space-y-1.5 overflow-y-auto px-3 py-2">
        {lines.map((line, i) => (
          <motion.li
            key={`${i}-${line.slice(0, 32)}`}
            initial={{ opacity: 0, x: -8 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: i * 0.05, duration: 0.28 }}
            className="flex gap-2 text-[10px] leading-snug text-zinc-400"
            style={{
              fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
            }}
          >
            <span className="shrink-0 font-semibold text-amber-400/90">▸</span>
            <span className="min-w-0 break-words">{line}</span>
          </motion.li>
        ))}
      </ul>
    </div>
  );
}
