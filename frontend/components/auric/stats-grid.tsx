"use client";

import { BarChart3, Percent, Target, Wallet } from "lucide-react";
import { motion } from "framer-motion";

import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";

type Props = {
  balance: string;
  pnlDay: string;
  pnlPositive?: boolean | null;
  trades24h: string;
  winRate: string;
};

const container = {
  hidden: { opacity: 0 },
  show: {
    opacity: 1,
    transition: { staggerChildren: 0.08 },
  },
};

const item = {
  hidden: { opacity: 0, y: 16 },
  show: { opacity: 1, y: 0 },
};

export function StatsGrid({
  balance,
  pnlDay,
  pnlPositive,
  trades24h,
  winRate,
}: Props) {
  const cards = [
    {
      label: "Saldo total",
      value: balance,
      hint: "Carteira USDT",
      icon: <Wallet className="size-4" />,
      accent: "neutral" as const,
    },
    {
      label: "P&L do dia",
      value: pnlDay,
      hint: "Variação hoje",
      icon: <Percent className="size-4" />,
      accent:
        pnlPositive === true
          ? ("profit" as const)
          : pnlPositive === false
            ? ("loss" as const)
            : ("neutral" as const),
    },
    {
      label: "Trades executados",
      value: trades24h,
      hint: "Últimas 24h",
      icon: <BarChart3 className="size-4" />,
      accent: "neutral" as const,
    },
    {
      label: "Win-rate",
      value: winRate,
      hint: "XGBoost accuracy",
      icon: <Target className="size-4" />,
      accent: "neutral" as const,
    },
  ];

  return (
    <motion.div
      variants={container}
      initial="hidden"
      animate="show"
      className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4"
    >
      {cards.map((s) => (
        <motion.div key={s.label} variants={item}>
          <Card className="border-white/[0.06] bg-zinc-900/40 backdrop-blur-sm">
            <CardContent className="p-5">
              <div className="mb-4 flex items-center justify-between">
                <span className="text-xs font-medium uppercase tracking-wider text-zinc-500">
                  {s.label}
                </span>
                <span
                  className={cn(
                    "flex size-8 items-center justify-center rounded-lg border",
                    s.accent === "profit" &&
                      "border-emerald-500/25 bg-emerald-500/10 text-emerald-400",
                    s.accent === "loss" &&
                      "border-red-500/30 bg-red-950/50 text-red-400",
                    s.accent === "neutral" &&
                      "border-white/[0.06] bg-white/[0.03] text-zinc-400"
                  )}
                >
                  {s.icon}
                </span>
              </div>
              <p
                className={cn(
                  "text-2xl font-semibold tracking-tight tabular-nums text-zinc-100",
                  s.accent === "profit" && "text-emerald-400",
                  s.accent === "loss" && "text-red-400"
                )}
              >
                {s.value}
              </p>
              <p className="mt-1 text-[11px] text-zinc-600">{s.hint}</p>
            </CardContent>
          </Card>
        </motion.div>
      ))}
    </motion.div>
  );
}
