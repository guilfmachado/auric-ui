"use client";

import { useEffect, useState } from "react";
import { Cpu } from "lucide-react";
import { motion } from "framer-motion";

import { cn } from "@/lib/utils";

import { NewsTicker } from "./news-ticker";
import { TerminalCard } from "./terminal-card";

type Props = {
  /** Coluna `veredito_ia` (ex.: BEARISH). */
  vereditoIa: string;
  /** Coluna `justificativa` do último log. */
  justificativaLog: string;
  noticiasAgregadas: string;
};

function verdictStyle(v: string) {
  const u = v.toUpperCase();
  if (/\bVETO\b/.test(u))
    return {
      label: v || "VETO",
      wrap: "from-amber-600/50 via-yellow-500/30 to-amber-950/55",
      border: "border-amber-400/60",
      text: "text-amber-100",
      glow:
        "shadow-[0_0_64px_rgba(250,204,21,0.35),inset_0_0_40px_rgba(250,204,21,0.08)]",
      cardTint: "bg-amber-950/35",
    };
  if (/\bBULL(ISH)?\b/.test(u) || u.includes("BUY"))
    return {
      label: v || "—",
      wrap: "from-emerald-600/45 via-emerald-500/30 to-emerald-950/50",
      border: "border-emerald-400/55",
      text: "text-emerald-100",
      glow:
        "shadow-[0_0_64px_rgba(34,197,94,0.4),inset_0_0_48px_rgba(34,197,94,0.1)]",
      cardTint: "bg-emerald-950/35",
    };
  if (/\bBEAR(ISH)?\b/.test(u) || u.includes("SELL"))
    return {
      label: v || "—",
      wrap: "from-red-700/50 via-rose-600/35 to-red-950/60",
      border: "border-red-500/55",
      text: "text-red-100",
      glow:
        "shadow-[0_0_64px_rgba(248,113,113,0.38),inset_0_0_40px_rgba(239,68,68,0.12)]",
      cardTint: "bg-red-950/40",
    };
  return {
    label: v || "SCANNING",
    wrap: "from-zinc-700/30 via-zinc-800/50 to-zinc-950/70",
    border: "border-zinc-600/50",
    text: "text-zinc-200",
    glow: "",
    cardTint: "bg-zinc-950/40",
  };
}

export function BrainFeed({
  vereditoIa,
  justificativaLog,
  noticiasAgregadas,
}: Props) {
  const raw = vereditoIa.trim();
  const st = verdictStyle(raw);
  const headline = raw ? raw.toUpperCase() : "SCANNING";
  const [typed, setTyped] = useState("");

  const body =
    justificativaLog.trim() ||
    "(sem justificativa neste log — aguarda próximo ciclo do motor.)";

  useEffect(() => {
    setTyped("");
    let i = 0;
    const id = window.setInterval(() => {
      i += 1;
      setTyped(body.slice(0, i));
      if (i >= body.length) window.clearInterval(id);
    }, 8);
    return () => window.clearInterval(id);
  }, [body]);

  return (
    <TerminalCard
      className={cn(
        "relative min-h-[320px] overflow-hidden bg-gradient-to-br transition-colors duration-500",
        st.wrap,
        st.glow,
        st.cardTint
      )}
    >
      <div
        className={cn(
          "pointer-events-none absolute inset-0 rounded-xl border",
          st.border
        )}
      />
      <div className="relative flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <Cpu className="size-4 text-zinc-500" />
          <span className="text-[10px] font-semibold tracking-[0.22em] text-zinc-500 uppercase">
            Intelligence log
          </span>
        </div>
      </div>

      <motion.div
        key={headline}
        initial={{ opacity: 0, scale: 0.97 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ type: "spring", stiffness: 140, damping: 20 }}
        className="relative mt-8 flex justify-center"
      >
        <div
          className={cn(
            "rounded-2xl border px-10 py-5 backdrop-blur-md",
            st.border,
            "bg-black/40"
          )}
        >
          <motion.p
            layout
            className={cn(
              "text-center text-4xl font-black tracking-tighter sm:text-5xl",
              st.text
            )}
            style={{
              fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
            }}
          >
            {headline}
          </motion.p>
          <p className="mt-2 text-center text-[10px] tracking-[0.25em] text-zinc-500 uppercase">
            Veredito IA
          </p>
        </div>
      </motion.div>

      <div className="relative mt-8 rounded-lg border border-[#27272a]/90 bg-black/50 p-4 backdrop-blur-sm">
        <p className="mb-2 text-[9px] font-semibold tracking-widest text-zinc-500 uppercase">
          Justificativa
        </p>
        <pre
          className="max-h-40 overflow-y-auto whitespace-pre-wrap font-mono text-[12px] leading-relaxed text-zinc-300"
          style={{
            fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
          }}
        >
          <span className="text-emerald-500/90">{">"} </span>
          {typed}
          <span className="ml-0.5 inline-block h-4 w-2 animate-pulse bg-emerald-500/60 align-[-2px]" />
        </pre>
      </div>

      <div className="relative mt-4">
        <NewsTicker text={noticiasAgregadas} />
      </div>
    </TerminalCard>
  );
}
