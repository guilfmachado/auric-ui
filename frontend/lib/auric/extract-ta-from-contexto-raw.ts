/**
 * Extrai ADX / RSI do texto `contexto_raw` (JSON do hub, blocos TA, ou linhas legíveis).
 */
export function extractAdxRsiFromContextoRaw(
  raw: string | null | undefined
): { adx: number | null; rsi: number | null } {
  if (!raw?.trim()) return { adx: null, rsi: null };
  const t = raw;

  const pick = (m: RegExpMatchArray | null): number | null => {
    if (!m?.[1]) return null;
    const n = parseFloat(m[1]);
    return Number.isFinite(n) ? n : null;
  };

  const adxJson =
    pick(t.match(/"adx_14"\s*:\s*([\d.eE+-]+)/)) ??
    pick(t.match(/"adx"\s*:\s*([\d.eE+-]+)/i));
  const rsiJson =
    pick(t.match(/"rsi_14"\s*:\s*([\d.eE+-]+)/)) ??
    pick(t.match(/"rsi"\s*:\s*([\d.eE+-]+)/i));

  const adxLine = pick(
    t.match(/ADX(?:\s*\(\s*14\s*\))?\s*[:=]\s*([\d.]+)/i)
  );
  const rsiLine = pick(
    t.match(/RSI(?:\s*\(\s*14\s*\))?\s*[:=]\s*([\d.]+)/i)
  );

  return {
    adx: adxJson ?? adxLine,
    rsi: rsiJson ?? rsiLine,
  };
}
