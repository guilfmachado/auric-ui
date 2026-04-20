/** PostgREST / JSON podem devolver `numeric` como string. */
export function coerceUsdtBalance(raw: unknown): number | null {
  if (raw === null || raw === undefined) return null;
  if (typeof raw === "number" && !Number.isNaN(raw)) return raw;
  const n = Number(String(raw).replace(",", "."));
  return Number.isFinite(n) ? n : null;
}

/**
 * `probabilidade_ml` em [0, 1] no bot; aceita legado em [0, 100] (ex.: 26.5 → 0.265).
 */
export function toMlProb01(raw: unknown): number | null {
  if (raw === null || raw === undefined) return null;
  const n =
    typeof raw === "number" ? raw : Number(String(raw).replace(",", "."));
  if (!Number.isFinite(n)) return null;
  const p = n > 1 ? n / 100 : n;
  return Math.min(1, Math.max(0, p));
}
