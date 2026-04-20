import type { LogRow } from "@/lib/types/auric";

import { extractAdxRsiFromContextoRaw } from "@/lib/auric/extract-ta-from-contexto-raw";

function parseFirstNumber(s: string | undefined): number | null {
  if (!s) return null;
  const m = s.match(/([\d.]+)/);
  if (!m) return null;
  const n = parseFloat(m[1]);
  return Number.isFinite(n) ? n : null;
}

export type ParsedTelemetry = {
  ml: number | null;
  adx: number | null;
  rsi: number | null;
  vwapLabel: string | null;
  bollingerLabel: string | null;
};

/** Extrai bloco JSON de `contexto_raw` (formato Python `formatar_log_contexto_raw`). */
function parseContextoRawTa(raw: string | null | undefined): Partial<ParsedTelemetry> {
  if (!raw?.trim()) return {};
  const start = "=== INDICADORES_TA (JSON) ===";
  const end = "=== INTELLIGENCE_HUB ===";
  const i = raw.indexOf(start);
  if (i < 0) return {};
  const j = raw.indexOf(end, i + start.length);
  const jsonSlice =
    j < 0 ? raw.slice(i + start.length).trim() : raw.slice(i + start.length, j).trim();
  try {
    const ta = JSON.parse(jsonSlice) as Record<string, unknown>;
    const num = (v: unknown): number | null => {
      if (v == null) return null;
      if (typeof v === "number" && !Number.isNaN(v)) return v;
      const x = parseFloat(String(v));
      return Number.isFinite(x) ? x : null;
    };

    const adx = num(ta.adx_14);
    const rsi = num(ta.rsi_14);
    const vies = ta.vies_vwap != null ? String(ta.vies_vwap) : null;
    const vwapD = num(ta.vwap_d);
    let vwapLabel: string | null = null;
    if (vies) {
      vwapLabel =
        vies === "ACIMA_VWAP"
          ? "Acima VWAP"
          : vies === "ABAIXO_VWAP"
            ? "Abaixo VWAP"
            : vies === "NO_VWAP"
              ? "No VWAP"
              : vies;
      if (vwapD != null) {
        vwapLabel = `${vwapLabel} (${vwapD.toFixed(2)})`;
      }
    } else if (vwapD != null) {
      vwapLabel = `VWAP D ${vwapD.toFixed(2)}`;
    }

    let bollingerLabel: string | null = null;
    if (ta.bollinger_squeeze === true) bollingerLabel = "Squeeze ativo";
    else if (typeof ta.bb_pct_b === "number" && !Number.isNaN(ta.bb_pct_b)) {
      bollingerLabel = `%B ${(ta.bb_pct_b as number).toFixed(3)}`;
    }

    const prob = num(ta.prob_ml);
    const mlPct = prob != null && prob <= 1 && prob >= 0 ? prob * 100 : prob;

    return {
      adx,
      rsi,
      vwapLabel,
      bollingerLabel,
      ml: mlPct,
    };
  } catch {
    return {};
  }
}

function parseStructuredMonitorando(justificativa: string): ParsedTelemetry | null {
  if (!justificativa.includes("\n")) return null;
  const parts = justificativa.split("\n---\n");
  const pre = parts[0] ?? "";
  const m: Record<string, string> = {};
  for (const line of pre.split("\n")) {
    const idx = line.indexOf(":");
    if (idx <= 0) continue;
    const k = line.slice(0, idx).trim().toUpperCase();
    m[k] = line.slice(idx + 1).trim();
  }
  if (!Object.keys(m).length) return null;

  return {
    ml: parseFirstNumber(m.ML),
    adx: parseFirstNumber(m.ADX),
    rsi: parseFirstNumber(m.RSI),
    vwapLabel: m.VWAP || m["VIÉS"] || m.VIES || null,
    bollingerLabel: m.BOLLINGER || m["BB"] || null,
  };
}

function sniffVwap(full: string): string | null {
  const u = full.toUpperCase();
  if (u.includes("ABAIXO") && u.includes("VWAP")) return "Abaixo VWAP";
  if (u.includes("ACIMA") && u.includes("VWAP")) return "Acima VWAP";
  if (u.includes("NO_VWAP") || u.includes("NO VWAP")) return "No VWAP";
  return null;
}

function sniffBollinger(full: string): string | null {
  const u = full.toLowerCase();
  if (u.includes("squeeze")) return "Squeeze";
  if (u.includes("bollinger")) {
    const short = full.split(/\n/).find((l) => l.toLowerCase().includes("bollinger"));
    return short ? short.slice(0, 72).trim() : "Bollinger";
  }
  return null;
}

function mergePrefer<T>(a: T | null | undefined, b: T | null | undefined): T | null {
  if (a != null && !(typeof a === "number" && Number.isNaN(a))) return a as T;
  if (b != null && !(typeof b === "number" && Number.isNaN(b))) return b as T;
  return null;
}

function mergePreferStr(
  a: string | null | undefined,
  b: string | null | undefined
): string | null {
  if (a != null && String(a).trim() !== "") return String(a);
  if (b != null && String(b).trim() !== "") return String(b);
  return null;
}

function numCol(v: unknown): number | null {
  if (v == null || v === "") return null;
  const x = typeof v === "number" ? v : parseFloat(String(v));
  return Number.isFinite(x) ? x : null;
}

/** RSI/ADX em colunas dedicadas da tabela `logs` (prioridade sobre texto/JSON). */
function taFromTableColumns(log: LogRow): { adx: number | null; rsi: number | null } {
  const r = log as unknown as Record<string, unknown>;
  return {
    rsi: numCol(log.rsi_14) ?? numCol(r.rsi),
    adx: numCol(log.adx_14) ?? numCol(r.adx),
  };
}

/** ADX / RSI / ML / VWAP a partir do último log: colunas TA, `contexto_raw` (JSON TA) + `justificativa`. */
export function parseTelemetryFromLog(log: LogRow | null): ParsedTelemetry {
  const empty: ParsedTelemetry = {
    ml: null,
    adx: null,
    rsi: null,
    vwapLabel: null,
    bollingerLabel: null,
  };
  if (!log) return empty;

  const cols = taFromTableColumns(log);
  const fromCtx = parseContextoRawTa(log.contexto_raw ?? null);
  const rawTa = extractAdxRsiFromContextoRaw(log.contexto_raw);

  const j = log.justificativa ?? "";
  if (!j.trim()) {
    return {
      ml: mergePrefer(fromCtx.ml, null),
      adx: mergePrefer(
        cols.adx,
        mergePrefer(mergePrefer(fromCtx.adx, null), rawTa.adx)
      ),
      rsi: mergePrefer(
        cols.rsi,
        mergePrefer(mergePrefer(fromCtx.rsi, null), rawTa.rsi)
      ),
      vwapLabel: fromCtx.vwapLabel ?? null,
      bollingerLabel: fromCtx.bollingerLabel ?? null,
    };
  }

  const structured = parseStructuredMonitorando(j);
  if (structured && (structured.ml != null || structured.adx != null || structured.rsi != null)) {
    const normMl =
      structured.ml != null && structured.ml <= 1 && structured.ml >= 0
        ? structured.ml * 100
        : structured.ml;
    return {
      ml: mergePrefer(normMl, fromCtx.ml),
      adx: mergePrefer(
        cols.adx,
        mergePrefer(mergePrefer(structured.adx, fromCtx.adx), rawTa.adx)
      ),
      rsi: mergePrefer(
        cols.rsi,
        mergePrefer(mergePrefer(structured.rsi, fromCtx.rsi), rawTa.rsi)
      ),
      vwapLabel: mergePreferStr(structured.vwapLabel, fromCtx.vwapLabel),
      bollingerLabel:
        mergePreferStr(structured.bollingerLabel, fromCtx.bollingerLabel) ??
        sniffBollinger(j),
    };
  }

  let mlPct: number | null = null;
  if (log.probabilidade_ml != null && !Number.isNaN(Number(log.probabilidade_ml))) {
    const p = Number(log.probabilidade_ml);
    mlPct = p <= 1 ? p * 100 : p;
  } else {
    const fromText = parseFirstNumber(j.match(/ML[^\d]*([\d.]+)/i)?.[0] ?? undefined);
    if (fromText != null) {
      mlPct = fromText <= 1 ? fromText * 100 : fromText;
    }
  }

  const adxM = j.match(/ADX(?:\s*\(\s*14\s*\))?\s*[:=]\s*([\d.]+)/i);
  const rsiM = j.match(/RSI(?:\s*\(\s*14\s*\))?\s*[:=]\s*([\d.]+)/i);

  return {
    ml: mergePrefer(mlPct, fromCtx.ml),
    adx: mergePrefer(
      cols.adx,
      mergePrefer(
        mergePrefer(adxM ? parseFloat(adxM[1]) : null, fromCtx.adx),
        rawTa.adx
      )
    ),
    rsi: mergePrefer(
      cols.rsi,
      mergePrefer(
        mergePrefer(rsiM ? parseFloat(rsiM[1]) : null, fromCtx.rsi),
        rawTa.rsi
      )
    ),
    vwapLabel: mergePreferStr(sniffVwap(j), fromCtx.vwapLabel),
    bollingerLabel: mergePreferStr(sniffBollinger(j), fromCtx.bollingerLabel),
  };
}
