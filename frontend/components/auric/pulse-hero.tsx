"use client";

import { motion } from "framer-motion";
import { Activity, Percent, TrendingUp } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

import { TerminalCard } from "./terminal-card";

type Props = {
  /** Valor formatado ou "Carregando…" enquanto `balanceLoading`. */
  balanceUsdt: string;
  balanceLoading?: boolean;
  /** Resposta inicial do Supabase ainda em curso: blocos pulsantes nos três cartões. */
  isLoading?: boolean;
  pnlDayPct: string;
  pnlPositive: boolean | null;
  ethPrice: number | null;
  ethChangePct: number | null;
  ethLoading?: boolean;
  /** Legenda sob o preço (ex.: Binance vs. coluna `logs`). */
  ethFootnote?: string;
  /** Pulso visual 1s quando o preço sobe/desce vs. tick anterior. */
  ethPriceFlash?: "up" | "down" | null;
};

function Mono({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn("font-mono tabular-nums tracking-tight", className)}
      style={{ fontFamily: "var(--font-geist-mono), ui-monospace, monospace" }}
    >
      {children}
    </span>
  );
}

const item = {
  hidden: { opacity: 0, y: 14 },
  show: { opacity: 1, y: 0 },
};

export function PulseHero({
  balanceUsdt,
  balanceLoading,
  isLoading,
  pnlDayPct,
  pnlPositive,
  ethPrice,
  ethChangePct,
  ethLoading,
  ethFootnote = "Binance · 24h rolling",
  ethPriceFlash = null,
}: Props) {
  const ch = ethChangePct;
  const chPos = ch != null && ch > 0;
  const chNeg = ch != null && ch < 0;

  return (
    <motion.div
      initial="hidden"
      animate="show"
      variants={{
        show: { transition: { staggerChildren: 0.07 } },
      }}
      className="grid gap-4 md:grid-cols-3"
    >
      <motion.div variants={item}>
        <TerminalCard className="flex min-h-[140px] flex-col justify-between">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[10px] font-semibold tracking-[0.2em] text-zinc-500 uppercase">
              Saldo
            </span>
            <Activity className="size-4 text-emerald-500/70" />
          </div>
          <div>
            {isLoading ? (
              <Skeleton className="h-10 w-[min(100%,11rem)] rounded-lg" />
            ) : (
              <Mono
                className={cn(
                  "font-semibold",
                  balanceLoading
                    ? "text-base text-zinc-400"
                    : "text-3xl text-zinc-100"
                )}
              >
                {balanceLoading ? (
                  "Carregando…"
                ) : (
                  <motion.span
                    key={balanceUsdt}
                    initial={{ opacity: 0.55, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ type: "spring", stiffness: 260, damping: 22 }}
                    className="inline-block"
                  >
                    {balanceUsdt}
                  </motion.span>
                )}
              </Mono>
            )}
            <p className="mt-1 text-[10px] text-zinc-600">
              USDC · <span className="text-zinc-500">wallet_status</span>
            </p>
          </div>
        </TerminalCard>
      </motion.div>

      <motion.div variants={item}>
        <TerminalCard className="flex min-h-[140px] flex-col justify-between">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[10px] font-semibold tracking-[0.2em] text-zinc-500 uppercase">
              PnL do dia
            </span>
            <Percent
              className={cn(
                "size-4",
                pnlPositive === true && "text-emerald-400",
                pnlPositive === false && "text-red-400",
                pnlPositive === null && "text-zinc-600"
              )}
            />
          </div>
          <div>
            {isLoading ? (
              <Skeleton className="h-10 w-[min(100%,8rem)] rounded-lg" />
            ) : (
              <Mono
                className={cn(
                  "text-3xl font-semibold",
                  pnlPositive === true && "text-emerald-400",
                  pnlPositive === false && "text-red-400",
                  pnlPositive === null && "text-zinc-300"
                )}
              >
                {pnlDayPct}
              </Mono>
            )}
            <p className="mt-1 text-[10px] text-zinc-600">Variação hoje (%)</p>
          </div>
        </TerminalCard>
      </motion.div>

      <motion.div variants={item}>
        <TerminalCard className="flex min-h-[140px] flex-col justify-between">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[10px] font-semibold tracking-[0.2em] text-zinc-500 uppercase">
              ETH / USDC
            </span>
            <TrendingUp
              className={cn(
                "size-4",
                chPos && "text-emerald-400",
                chNeg && "text-red-400",
                ch == null && "text-zinc-600"
              )}
            />
          </div>
          <div>
            {isLoading ? (
              <div className="flex flex-wrap items-baseline gap-3">
                <Skeleton className="h-10 w-36 rounded-lg" />
                <Skeleton className="h-6 w-16 rounded-md" />
              </div>
            ) : (
              <div className="flex flex-wrap items-baseline gap-3">
                <Mono
                  className={cn(
                    "text-3xl font-semibold transition-colors duration-300",
                    ethPriceFlash === "up" && "text-emerald-400",
                    ethPriceFlash === "down" && "text-red-400",
                    ethPriceFlash == null && "text-zinc-100"
                  )}
                >
                  {ethLoading && ethPrice == null ? (
                    "…"
                  ) : ethPrice != null ? (
                    <motion.span
                      key={ethPrice}
                      initial={{ opacity: 0.55, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{
                        type: "spring",
                        stiffness: 280,
                        damping: 22,
                      }}
                      className="inline-block"
                    >
                      $
                      {ethPrice.toLocaleString("en-US", {
                        minimumFractionDigits: 2,
                        maximumFractionDigits: 2,
                      })}
                    </motion.span>
                  ) : (
                    "—"
                  )}
                </Mono>
                {ch != null && (
                  <Mono
                    className={cn(
                      "text-sm font-medium",
                      chPos && "text-emerald-400",
                      chNeg && "text-red-400",
                      !chPos && !chNeg && "text-zinc-500"
                    )}
                  >
                    <motion.span
                      key={ch}
                      initial={{ opacity: 0.6 }}
                      animate={{ opacity: 1 }}
                      transition={{ duration: 0.25 }}
                      className="inline-block"
                    >
                      {ch > 0 ? "+" : ""}
                      {ch.toFixed(2)}%
                    </motion.span>
                  </Mono>
                )}
              </div>
            )}
            <p className="mt-1 text-[10px] text-zinc-600">{ethFootnote}</p>
          </div>
        </TerminalCard>
      </motion.div>
    </motion.div>
  );
}
