"use client";

import { AnimatePresence, motion } from "framer-motion";
import { ScrollText } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { actionBadgeClass, mapActionToBadge } from "@/lib/auric/map-action";
import type { LogRow } from "@/lib/types/auric";
import { toMlProb01 } from "@/lib/auric/coerce-metrics";
import { cn } from "@/lib/utils";

import { TerminalCard } from "./terminal-card";

type Props = {
  rows: LogRow[];
  /** Número de linhas esperadas (rótulo no header). */
  maxRows?: number;
  /** Primeira carga Supabase: linhas placeholder. */
  isLoading?: boolean;
};

const logRowVariants = {
  initial: { opacity: 0, y: 10 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -6 },
};

const rowTransition = {
  duration: 0.38,
  ease: [0.22, 1, 0.36, 1] as const,
};

function formatTime(iso: string | undefined) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("pt-BR", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return "—";
  }
}

export function LogsTable({ rows, maxRows = 5, isLoading }: Props) {
  return (
    <motion.section
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: 0.08 }}
    >
      <TerminalCard className="overflow-hidden p-0">
        <div className="flex items-center gap-2 border-b border-[#27272a] px-4 py-3 sm:px-5">
          <ScrollText className="size-4 text-zinc-500" />
          <h2 className="text-xs font-semibold tracking-tight text-zinc-200">
            Live logs
          </h2>
          <span className="ml-auto rounded border border-emerald-500/25 bg-emerald-500/10 px-2 py-0.5 text-[9px] font-semibold tracking-widest text-emerald-400/90 uppercase">
            últimos {maxRows}
          </span>
        </div>
        <div className="max-h-[min(52vh,28rem)] overflow-x-auto overflow-y-auto">
          <Table>
            <TableHeader>
              <TableRow className="border-[#27272a] hover:bg-transparent">
                <TableHead className="sticky top-0 z-[1] bg-[#18181b] text-[9px] font-semibold tracking-wider text-zinc-500 uppercase">
                  Horário
                </TableHead>
                <TableHead className="sticky top-0 z-[1] bg-[#18181b] text-[9px] font-semibold tracking-wider text-zinc-500 uppercase">
                  Ativo
                </TableHead>
                <TableHead className="sticky top-0 z-[1] bg-[#18181b] text-[9px] font-semibold tracking-wider text-zinc-500 uppercase">
                  Ação
                </TableHead>
                <TableHead className="sticky top-0 z-[1] bg-[#18181b] text-right text-[9px] font-semibold tracking-wider text-zinc-500 uppercase">
                  XGB
                </TableHead>
                <TableHead className="sticky top-0 z-[1] bg-[#18181b] text-right text-[9px] font-semibold tracking-wider text-zinc-500 uppercase">
                  Preço
                </TableHead>
                <TableHead className="sticky top-0 z-[1] bg-[#18181b] text-[9px] font-semibold tracking-wider text-zinc-500 uppercase">
                  IA
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading ? (
                Array.from({ length: maxRows }).map((_, i) => (
                  <TableRow
                    key={`log-skeleton-${i}`}
                    className="border-b border-[#27272a]/80"
                  >
                    <TableCell className="py-3">
                      <Skeleton className="h-3 w-28 rounded-md" />
                    </TableCell>
                    <TableCell className="py-3">
                      <Skeleton className="h-3 w-16 rounded-md" />
                    </TableCell>
                    <TableCell className="py-3">
                      <Skeleton className="h-5 w-12 rounded-md" />
                    </TableCell>
                    <TableCell className="py-3 text-right">
                      <Skeleton className="ml-auto h-3 w-10 rounded-md" />
                    </TableCell>
                    <TableCell className="py-3 text-right">
                      <Skeleton className="ml-auto h-3 w-14 rounded-md" />
                    </TableCell>
                    <TableCell className="py-3">
                      <Skeleton className="h-3 w-20 rounded-md" />
                    </TableCell>
                  </TableRow>
                ))
              ) : rows.length === 0 ? (
                <tr>
                  <TableCell
                    colSpan={6}
                    className="py-10 text-center text-xs text-zinc-600"
                  >
                    Nenhum log. O bot preencherá aqui.
                  </TableCell>
                </tr>
              ) : (
                <AnimatePresence initial={false} mode="popLayout">
                  {rows.map((row) => {
                    const badge = mapActionToBadge(row.acao_tomada);
                    const ac = row.acao_tomada ?? "—";
                    return (
                      <motion.tr
                        key={row.id}
                        layout
                        variants={logRowVariants}
                        initial="initial"
                        animate="animate"
                        exit="exit"
                        transition={rowTransition}
                        className="border-b border-[#27272a]/80 transition-colors hover:bg-[#09090b]/80"
                      >
                        <TableCell
                          className="py-2 font-mono text-[10px] text-zinc-500"
                          style={{
                            fontFamily:
                              "var(--font-geist-mono), ui-monospace, monospace",
                          }}
                        >
                          {formatTime(row.created_at)}
                        </TableCell>
                        <TableCell className="py-2 text-[11px] font-medium text-zinc-300">
                          {row.par_moeda ??
                            (typeof row.ativo === "string" ? row.ativo : "—")}
                        </TableCell>
                        <TableCell className="py-2">
                          <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-2">
                            <Badge
                              variant="outline"
                              className={cn(
                                "w-fit border px-1.5 py-0 text-[9px] font-bold tracking-wide",
                                badge === "BUY" &&
                                  "border-emerald-500/40 bg-emerald-500/10 text-emerald-400",
                                badge === "SELL" &&
                                  "border-rose-500/45 bg-rose-950/45 text-rose-300",
                                badge === "HOLD" &&
                                  "border-zinc-600 bg-zinc-800/90 text-zinc-400"
                              )}
                            >
                              {badge}
                            </Badge>
                            <span
                              className={cn(
                                "w-fit max-w-[10rem] truncate rounded border px-1.5 py-0 text-[9px] font-semibold sm:max-w-[14rem]",
                                actionBadgeClass(ac)
                              )}
                              title={ac}
                            >
                              {ac}
                            </span>
                          </div>
                        </TableCell>
                        <TableCell
                          className="py-2 text-right font-mono text-[10px] font-semibold text-zinc-300"
                          style={{
                            fontFamily:
                              "var(--font-geist-mono), ui-monospace, monospace",
                          }}
                        >
                          {((toMlProb01(row.probabilidade_ml) ?? 0) * 100).toFixed(
                            1
                          )}
                          %
                        </TableCell>
                        <TableCell
                          className="py-2 text-right font-mono text-[10px] font-semibold text-zinc-200"
                          style={{
                            fontFamily:
                              "var(--font-geist-mono), ui-monospace, monospace",
                          }}
                        >
                          {Number(row.preco_atual).toLocaleString("en-US", {
                            minimumFractionDigits: 2,
                            maximumFractionDigits: 2,
                          })}
                        </TableCell>
                        <TableCell className="py-2 text-[10px] text-zinc-500">
                          {row.veredito_ia?.trim() ||
                            row.sentimento_ia ||
                            "—"}
                        </TableCell>
                      </motion.tr>
                    );
                  })}
                </AnimatePresence>
              )}
            </TableBody>
          </Table>
        </div>
      </TerminalCard>
    </motion.section>
  );
}
