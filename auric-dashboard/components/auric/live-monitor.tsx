"use client";

import { Brain, Sparkles } from "lucide-react";
import { motion } from "framer-motion";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

import { MlRadialGauge } from "./ml-radial-gauge";

type Props = {
  mlProb: number;
  verdict: string;
  justification: string;
};

export function LiveMonitor({ mlProb, verdict, justification }: Props) {
  const pct = Math.min(100, Math.max(0, mlProb * 100));

  return (
    <motion.section
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: 0.1 }}
      className="grid gap-6 lg:grid-cols-[minmax(0,280px)_1fr]"
    >
      <Card className="overflow-hidden border-white/[0.06] bg-gradient-to-b from-zinc-900/80 to-zinc-950/90">
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-sm font-medium text-zinc-400">
            <Brain className="size-4 text-emerald-500/80" />
            Probabilidade ML
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col items-center pb-8 pt-2">
          <MlRadialGauge value={mlProb} />
          <div className="mt-4 w-full max-w-[200px] space-y-2 px-2">
            <div className="flex justify-between text-[10px] uppercase tracking-wider text-zinc-600">
              <span>0%</span>
              <span>Confiança</span>
              <span>100%</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-zinc-800">
              <motion.div
                className="h-full rounded-full bg-gradient-to-r from-emerald-700 to-emerald-400"
                initial={{ width: 0 }}
                animate={{ width: `${pct}%` }}
                transition={{ type: "spring", stiffness: 80, damping: 20 }}
              />
            </div>
          </div>
        </CardContent>
      </Card>

      <Card className="border-white/[0.06] bg-zinc-900/40 backdrop-blur-sm">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm font-medium text-zinc-400">
            <Sparkles className="size-4 text-amber-400/90" />
            Veredito da IA · Llama 3
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <motion.div
            key={verdict}
            initial={{ opacity: 0, x: 8 }}
            animate={{ opacity: 1, x: 0 }}
            className="inline-flex rounded-lg border border-emerald-500/20 bg-emerald-500/5 px-4 py-2"
          >
            <span className="text-lg font-semibold tracking-tight text-emerald-100">
              {verdict || "—"}
            </span>
          </motion.div>
          <motion.p
            key={justification.slice(0, 48)}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="text-sm leading-relaxed text-zinc-400"
          >
            {justification || "Aguardando próximo ciclo do bot…"}
          </motion.p>
        </CardContent>
      </Card>
    </motion.section>
  );
}
