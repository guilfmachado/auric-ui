/**
 * Normaliza ações do bot para exibição BUY / SELL / HOLD.
 */
export function mapActionToBadge(acao: string): "BUY" | "SELL" | "HOLD" {
  const u = acao.toUpperCase();
  if (
    u.includes("COMPRA") ||
    u === "BUY" ||
    u.includes("LONG") ||
    u.includes("ENTRADA")
  ) {
    return "BUY";
  }
  if (
    u.includes("VENDA") ||
    u === "SELL" ||
    u.includes("SHORT") ||
    u.includes("FECHA") ||
    u.includes("PROFIT") ||
    u.includes("STOP")
  ) {
    return "SELL";
  }
  return "HOLD";
}
