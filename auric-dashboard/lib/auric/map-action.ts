import { cn } from "@/lib/utils";

/**
 * Normaliza ações do bot para exibição BUY / SELL / HOLD.
 */
export function mapActionToBadge(acao: string): "BUY" | "SELL" | "HOLD" {
  const u = acao.toUpperCase();
  if (u === "MONITORANDO" || u.includes("HOLD_VOL")) {
    return "HOLD";
  }
  if (
    u.includes("COMPRA") ||
    u === "BUY" ||
    u.includes("LONG") ||
    u.includes("ENTRADA") ||
    u.includes("OPEN_LONG")
  ) {
    return "BUY";
  }
  if (
    u.includes("VENDA") ||
    u === "SELL" ||
    u.includes("SHORT") ||
    u.includes("FECHA") ||
    u.includes("PROFIT") ||
    u.includes("STOP") ||
    u.includes("OPEN_SHORT")
  ) {
    return "SELL";
  }
  return "HOLD";
}

/** Classes Tailwind para badge compacto da ação bruta (ex.: VETO_RSI, ABRE_SHORT_LIMIT). */
export function actionBadgeClass(acao: string): string {
  const u = acao.toUpperCase();
  if (u.includes("ERRO") || u.includes("FAIL")) {
    return "border-orange-500/50 bg-orange-950/50 text-orange-300";
  }
  if (u.includes("DRY_RUN")) {
    return "border-violet-500/45 bg-violet-950/40 text-violet-300";
  }
  if (u.includes("VETO") || u.includes("ABORT")) {
    return cn(
      "border-amber-500/40 bg-amber-500/15 text-amber-300",
      "shadow-[0_0_12px_rgba(251,191,36,0.12)]"
    );
  }
  if (u.includes("MONITOR") || u.includes("HOLD")) {
    return "border-sky-500/35 bg-sky-500/10 text-sky-300/95";
  }
  if (
    u.includes("SHORT") ||
    u.includes("OPEN_SHORT") ||
    u.includes("VENDA") ||
    u.includes("STOP")
  ) {
    return "border-rose-500/45 bg-rose-950/50 text-rose-300";
  }
  if (
    u.includes("LONG") ||
    u.includes("OPEN_LONG") ||
    u.includes("COMPRA") ||
    u.includes("PROFIT")
  ) {
    return "border-emerald-500/40 bg-emerald-500/12 text-emerald-300";
  }
  return "border-zinc-600 bg-zinc-800/90 text-zinc-400";
}
