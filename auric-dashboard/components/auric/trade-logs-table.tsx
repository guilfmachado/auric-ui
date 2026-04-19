"use client";

import { motion } from "framer-motion";
import { ScrollText } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { mapActionToBadge } from "@/lib/auric/map-action";
import type { TradeLogRow } from "@/lib/types/auric";
import { cn } from "@/lib/utils";

type Props = {
  rows: TradeLogRow[];
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

export function TradeLogsTable({ rows }: Props) {
  return (
    <motion.section
      initial={{ opacity: 0, y: 24 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.45, delay: 0.15 }}
      className="rounded-xl border border-white/[0.06] bg-zinc-900/30"
    >
      <div className="flex items-center gap-2 border-b border-white/[0.06] px-5 py-4">
        <ScrollText className="size-4 text-zinc-500" />
        <h2 className="text-sm font-semibold tracking-tight text-zinc-200">
          Registro de trades
        </h2>
        <span className="ml-auto rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-emerald-400/90">
          Realtime
        </span>
      </div>
      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow className="border-white/[0.06] hover:bg-transparent">
              <TableHead className="text-zinc-500">Horário</TableHead>
              <TableHead className="text-zinc-500">Ativo</TableHead>
              <TableHead className="text-zinc-500">Ação</TableHead>
              <TableHead className="text-right text-zinc-500">Preço</TableHead>
              <TableHead className="text-zinc-500">Sentimento</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
              {rows.length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={5}
                    className="py-12 text-center text-sm text-zinc-600"
                  >
                    Nenhum log ainda. O bot começará a preencher esta tabela.
                  </TableCell>
                </TableRow>
              ) : (
                rows.map((row, i) => {
                  const badge = mapActionToBadge(row.acao_tomada);
                  return (
                    <TableRow
                      key={row.id}
                      className="border-white/[0.04] hover:bg-white/[0.02]"
                    >
                      <TableCell className="font-mono text-xs text-zinc-400">
                        <motion.span
                          initial={{ opacity: 0 }}
                          animate={{ opacity: 1 }}
                          transition={{ delay: i * 0.02 }}
                        >
                          {formatTime(row.created_at)}
                        </motion.span>
                      </TableCell>
                      <TableCell className="font-medium text-zinc-200">
                        {row.ativo}
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant="outline"
                          className={cn(
                            "border font-semibold",
                            badge === "BUY" &&
                              "border-emerald-500/35 bg-emerald-500/10 text-emerald-400",
                            badge === "SELL" &&
                              "border-red-500/40 bg-red-950/40 text-red-400",
                            badge === "HOLD" &&
                              "border-zinc-600 bg-zinc-800/80 text-zinc-400"
                          )}
                        >
                          {badge}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right font-mono tabular-nums text-zinc-300">
                        {Number(row.preco_atual).toLocaleString("en-US", {
                          minimumFractionDigits: 2,
                          maximumFractionDigits: 2,
                        })}
                      </TableCell>
                      <TableCell className="text-zinc-400">
                        {row.sentimento_ia || "—"}
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
          </TableBody>
        </Table>
      </div>
    </motion.section>
  );
}
