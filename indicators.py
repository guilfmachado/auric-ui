"""
Indicadores técnicos para ETH (e OHLCV genérico): ADX, VWAP diário, compressão Bollinger.

Usados como features do ML e como filtros operacionais (tendência vs. lateral, viés VWAP).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pandas_ta as ta

# ADX abaixo disto → mercado em geral lateral (sem tendência forte); rompimentos são menos fiáveis.
# Sessão teste HF: 15 aceita tendências iniciais / mais fracas (antes 25).
ADX_LIMIAR_TENDENCIA = 15.0
# Camada TA «quant»: <20 → ruído/lateral fraco; acima do limiar de tendência → leitura mais direcional.
ADX_LIMIAR_LATERAL_RUIDO = 20.0

# Se P(ML) ≥ isto e houver squeeze nas BB, a confiança operacional sobe para CONFIANCA_BOOST_SQUEEZE.
ML_PROB_LIMIAR_BOOST_SQUEEZE = 0.83
CONFIANCA_BOOST_SQUEEZE = 99

# Confluência (janela OHLCV): largura BB / preço < isto → squeeze «SIM» (faixa estreita).
SQUEEZE_BB_LARGURA_REL_MAX = 0.02  # 2%

# Risco operacional: SHORT com RSI abaixo disto = sobrevenda («vender o fundo»).
RSI_LIMIAR_OVERSOLD_SHORT = 30.0
# Volume do último minuto fechado vs minuto anterior: aumento relativo ≥ isto ⇒ spike (+300% → 3.0).
VOLUME_SPIKE_FRACAO_1M = 3.0

# Bollinger (ML + entrada ETH/USDC): σ mais apertado para USDC; squeeze = largura/preço < 0,2%.
BBANDS_LENGTH = 20
BBANDS_STD = 1.5
ENTRY_SQUEEZE_MAX_WIDTH_FRAC = 0.002  # (BBU−BBL)/close < 0,2%

# --- Price Action (Auric) — pivots 1m, squeeze «real», short squeeze ---
DOUBLE_PIVOT_ZONE_FRAC = 0.0005  # ±0,05% vs topo/fundo anterior
SQUEEZE_REAL_BB_FRAC = ENTRY_SQUEEZE_MAX_WIDTH_FRAC  # alinhado ao filtro de entrada
SHORT_SQUEEZE_MOVE_FRAC = 0.004  # vela 1m fechada: variação (close−open)/open
SHORT_SQUEEZE_VOL_MULT = 2.0  # volume da vela ≥ N× média das velas anteriores
SHORT_SQUEEZE_RSI_MIN = 70.0
PIVOT_FRACTAL_LEFT = 2
PIVOT_FRACTAL_RIGHT = 2


def obter_indicadores_confluencia(
    exchange: Any,
    simbolo: str,
    *,
    timeframe: str = "1h",
    limit: int = 100,
) -> dict[str, Any]:
    """
    VWAP aproximado por (preço típico × volume) / volume acumulado na janela;
    ADX(14); Bollinger(20, 1.5σ); squeeze se (BBU − BBL) / preço < 2%.

    `exchange`: instância ccxt com `fetch_ohlcv`.
    """
    ohlcv = exchange.fetch_ohlcv(simbolo, timeframe=timeframe, limit=limit)
    if not ohlcv or len(ohlcv) < 30:
        raise ValueError(
            "obter_indicadores_confluencia: série OHLCV insuficiente (mín. ~30 velas)."
        )

    df = pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )

    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    tp_v = tp * df["volume"]
    vol_sum = float(df["volume"].sum())
    if vol_sum <= 0:
        raise ValueError("obter_indicadores_confluencia: volume acumulado nulo.")
    vwap_atual = float(tp_v.sum() / vol_sum)
    preco_atual = float(df["close"].iloc[-1])
    if preco_atual <= 0:
        raise ValueError("obter_indicadores_confluencia: preço atual inválido.")

    vs_vwap = "ACIMA" if preco_atual > vwap_atual else "ABAIXO"

    adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
    adx_atual = float("nan")
    if adx_df is not None and not adx_df.empty:
        adx_col = next(
            (c for c in adx_df.columns if str(c).upper().startswith("ADX")),
            None,
        )
        if adx_col is not None:
            adx_atual = float(adx_df[adx_col].iloc[-1])

    upper = float("nan")
    lower = float("nan")
    bb_df = ta.bbands(df["close"], length=BBANDS_LENGTH, std=BBANDS_STD)
    if bb_df is not None and not bb_df.empty:
        bbu = next((c for c in bb_df.columns if str(c).startswith("BBU_")), None)
        bbl = next((c for c in bb_df.columns if str(c).startswith("BBL_")), None)
        if bbu and bbl:
            upper = float(bb_df[bbu].iloc[-1])
            lower = float(bb_df[bbl].iloc[-1])

    if (
        not np.isnan(upper)
        and not np.isnan(lower)
        and (upper - lower) / preco_atual < SQUEEZE_BB_LARGURA_REL_MAX
    ):
        squeeze = "SIM"
    else:
        squeeze = "NÃO"

    return {
        "vwap": round(vwap_atual, 2),
        "posicao_vwap": vs_vwap,
        "adx": round(adx_atual, 2) if not np.isnan(adx_atual) else None,
        "squeeze": squeeze,
        "preco": round(preco_atual, 2),
    }


def rsi_proibe_entrada_short(rsi: float | None) -> bool:
    """
    True se RSI(14) está em sobrevenda — abrir SHORT seria «vender o fundo»
    (proibido mesmo com sinal ML/Brain de queda).
    """
    if rsi is None:
        return False
    try:
        r = float(rsi)
        if np.isnan(r):
            return False
        return r < RSI_LIMIAR_OVERSOLD_SHORT
    except (TypeError, ValueError):
        return False


def extrair_pivots_fractais(
    df: pd.DataFrame,
    *,
    left: int = PIVOT_FRACTAL_LEFT,
    right: int = PIVOT_FRACTAL_RIGHT,
) -> list[dict[str, Any]]:
    """
    Máximos/mínimos locais (fractais): pivô em i se high[i] (resp. low[i]) é estritamente
    maior (resp. menor) que todos os highs (resp. lows) em [i−left, i+right] exceto i.
    """
    out: list[dict[str, Any]] = []
    if df is None or len(df) < left + right + 5:
        return out
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    ts = df["timestamp"].to_numpy()
    n = len(df)
    for i in range(left, n - right):
        lmx = float(np.max(highs[i - left : i]))
        rmx = float(np.max(highs[i + 1 : i + right + 1]))
        if highs[i] > lmx and highs[i] > rmx:
            out.append({"i": i, "ts": int(ts[i]), "kind": "H", "price": float(highs[i])})
        lmn = float(np.min(lows[i - left : i]))
        rmn = float(np.min(lows[i + 1 : i + right + 1]))
        if lows[i] < lmn and lows[i] < rmn:
            out.append({"i": i, "ts": int(ts[i]), "kind": "L", "price": float(lows[i])})
    return out


def sinal_double_top_ultima_vela(
    pivots_ultimos: list[dict[str, Any]],
    close: float,
    high: float,
) -> bool:
    """Dois topos consecutivos na janela de pivots alinhados (≤0,05%) e recuo do fecho."""
    z = DOUBLE_PIVOT_ZONE_FRAC
    if len(pivots_ultimos) < 2:
        return False
    hs = [float(p["price"]) for p in pivots_ultimos if p.get("kind") == "H"]
    if len(hs) < 2:
        return False
    h1, h2 = hs[-2], hs[-1]
    if abs(h2 - h1) / max(h1, 1e-12) > z:
        return False
    peak_ref = max(h1, h2)
    if peak_ref <= 0:
        return False
    touched = high >= peak_ref * (1.0 - z)
    recuo = close < peak_ref * (1.0 - z)
    return bool(touched and recuo)


def sinal_double_bottom_ultima_vela(
    pivots_ultimos: list[dict[str, Any]],
    close: float,
    low: float,
) -> bool:
    """Dois fundos consecutivos alinhados (≤0,05%) e repique do fecho."""
    z = DOUBLE_PIVOT_ZONE_FRAC
    if len(pivots_ultimos) < 2:
        return False
    ls = [float(p["price"]) for p in pivots_ultimos if p.get("kind") == "L"]
    if len(ls) < 2:
        return False
    l1, l2 = ls[-2], ls[-1]
    if abs(l2 - l1) / max(l1, 1e-12) > z:
        return False
    trough_ref = min(l1, l2)
    if trough_ref <= 0:
        return False
    touched = low <= trough_ref * (1.0 + z)
    bounce = close > trough_ref * (1.0 + z)
    return bool(touched and bounce)


def detectar_short_squeeze_1m(df: pd.DataFrame) -> dict[str, Any]:
    """
    Vela 1m **fechada** (penúltima linha do OHLCV): (C−O)/O > 0,4%, volume ≥ 2× média
    das 20 velas anteriores, RSI(14) > 70 → possível short squeeze / coberturas.
    """
    out: dict[str, Any] = {
        "ativo": False,
        "move_frac": None,
        "vol_ratio": None,
        "rsi": None,
    }
    if df is None or len(df) < 25:
        return out
    d = df.iloc[-2]
    o = float(d["open"])
    c = float(d["close"])
    v = float(d["volume"])
    if o <= 0:
        return out
    move = (c - o) / o
    win = df["volume"].iloc[-22:-2]
    vma = float(win.mean()) if len(win) else 0.0
    rsi_s = ta.rsi(df["close"], length=14)
    rsi_last: float | None = None
    if rsi_s is not None and len(rsi_s) >= 2:
        rv = float(rsi_s.iloc[-2])
        rsi_last = None if (isinstance(rv, float) and np.isnan(rv)) else rv
    out["move_frac"] = float(move)
    out["vol_ratio"] = (v / vma) if vma > 1e-12 else None
    out["rsi"] = rsi_last
    out["ativo"] = bool(
        move >= SHORT_SQUEEZE_MOVE_FRAC
        and vma > 1e-12
        and v >= SHORT_SQUEEZE_VOL_MULT * vma
        and rsi_last is not None
        and rsi_last > SHORT_SQUEEZE_RSI_MIN
    )
    return out


def analisar_bb_entrada_squeeze_breakout(feat: pd.DataFrame) -> dict[str, Any]:
    """
    Entrada «sniper» ETH/USDC:
    - `bb_squeeze_tight_002`: (BBU−BBL)/close < 0,2% — mercado em compressão.
    - Rompimento válido: largura das bandas a **expandir** vs vela anterior, preço a romper
      BBU (long) ou BBL (short), volume da última vela > média das **10** anteriores.
    """
    out: dict[str, Any] = {
        "bb_squeeze_tight_002": False,
        "bb_width_frac_now": None,
        "bb_width_frac_prev": None,
        "bb_width_expanding": False,
        "bb_breakout_long_ok": False,
        "bb_breakout_short_ok": False,
    }
    if feat is None or len(feat) < 2:
        return out
    cols = list(feat.columns)
    bbu = next((c for c in cols if str(c).startswith("BBU_")), None)
    bbl = next((c for c in cols if str(c).startswith("BBL_")), None)
    if not bbu or not bbl or "close" not in feat.columns or "volume" not in feat.columns:
        return out

    def width_frac_at(j: int) -> float | None:
        try:
            u = float(feat[bbu].iloc[j])
            ell = float(feat[bbl].iloc[j])
            c = float(feat["close"].iloc[j])
            if c <= 0 or np.isnan(u) or np.isnan(ell) or np.isnan(c):
                return None
            return (u - ell) / c
        except (IndexError, TypeError, ValueError):
            return None

    wn = width_frac_at(-1)
    wp = width_frac_at(-2)
    out["bb_width_frac_now"] = wn
    out["bb_width_frac_prev"] = wp
    if wn is not None:
        out["bb_squeeze_tight_002"] = bool(wn < float(ENTRY_SQUEEZE_MAX_WIDTH_FRAC))
    if wn is not None and wp is not None:
        out["bb_width_expanding"] = bool(wn > wp)

    try:
        c_last = float(feat["close"].iloc[-1])
        u_last = float(feat[bbu].iloc[-1])
        l_last = float(feat[bbl].iloc[-1])
        v_last = float(feat["volume"].iloc[-1])
    except (TypeError, ValueError, IndexError):
        return out

    vol_mean_10: float | None = None
    if len(feat) >= 12:
        seg = feat["volume"].iloc[-11:-1]
        vol_mean_10 = float(seg.mean()) if len(seg) else None
    vol_ok = (
        vol_mean_10 is not None
        and vol_mean_10 > 1e-12
        and not np.isnan(v_last)
        and v_last > vol_mean_10
    )
    exp = bool(out["bb_width_expanding"])
    if not (np.isnan(c_last) or np.isnan(u_last)):
        out["bb_breakout_long_ok"] = bool(exp and c_last > u_last and vol_ok)
    if not (np.isnan(c_last) or np.isnan(l_last)):
        out["bb_breakout_short_ok"] = bool(exp and c_last < l_last and vol_ok)
    return out


def reunir_sinais_price_action(
    exchange: Any,
    simbolo: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """
    Double Top/Bottom (últimos 3 pivots em 1m), Short Squeeze (vela 1m fechada),
    Bollinger «Squeeze Real» (largura bandas vs preço no snapshot, tipicamente 1h).
    """
    out: dict[str, Any] = {
        "double_top": False,
        "double_bottom": False,
        "short_squeeze": False,
        "squeeze_real": False,
        "squeeze_real_block": False,
        "bb_breakout_up": False,
        "bb_breakout_down": False,
        "resumo": "—",
    }
    bu = snapshot.get("bb_upper")
    bl = snapshot.get("bb_lower")
    pc = snapshot.get("preco_close")
    pct_b = snapshot.get("bb_pct_b")
    try:
        if bu is not None and bl is not None and pc is not None and float(pc) > 0:
            fu, fl, fp = float(bu), float(bl), float(pc)
            bw_abs = (fu - fl) / fp
            out["squeeze_real"] = bool(bw_abs < float(ENTRY_SQUEEZE_MAX_WIDTH_FRAC))
            if pct_b is not None:
                pb = float(pct_b)
                out["bb_breakout_up"] = pb >= 1.0
                out["bb_breakout_down"] = pb <= 0.0
            out["squeeze_real_block"] = bool(out["squeeze_real"]) and not (
                out["bb_breakout_up"] or out["bb_breakout_down"]
            )
    except (TypeError, ValueError):
        pass

    try:
        raw = exchange.fetch_ohlcv(simbolo, "1m", limit=120)
    except Exception:
        raw = []
    if raw and len(raw) >= 25:
        df1 = pd.DataFrame(
            raw,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        pivots_all = extrair_pivots_fractais(df1)
        last3 = pivots_all[-3:] if len(pivots_all) >= 3 else pivots_all
        lc = float(df1["close"].iloc[-2])
        lh = float(df1["high"].iloc[-2])
        ll = float(df1["low"].iloc[-2])
        out["double_top"] = bool(sinal_double_top_ultima_vela(last3, lc, lh))
        out["double_bottom"] = bool(sinal_double_bottom_ultima_vela(last3, lc, ll))
        ss = detectar_short_squeeze_1m(df1)
        out["short_squeeze"] = bool(ss.get("ativo"))
        out["short_squeeze_detail"] = {
            "move_frac": ss.get("move_frac"),
            "vol_ratio": ss.get("vol_ratio"),
            "rsi": ss.get("rsi"),
        }
    parts: list[str] = []
    if out["double_top"]:
        parts.append("Double Top (viés SHORT)")
    if out["double_bottom"]:
        parts.append("Double Bottom (viés LONG)")
    if out["short_squeeze"]:
        parts.append("Short squeeze em curso (evitar SHORT)")
    if out["squeeze_real_block"]:
        parts.append("Squeeze Real BB — aguardar rompimento")
    elif out["squeeze_real"] and (out["bb_breakout_up"] or out["bb_breakout_down"]):
        parts.append("Squeeze Real com rompimento de banda")
    out["resumo"] = "; ".join(parts) if parts else "—"
    return out


def volume_compra_spike_1m(
    exchange: Any,
    simbolo: str,
    *,
    spike_frac: float | None = None,
) -> bool:
    """
    Compara o volume da vela 1m **já fechada** com a vela 1m anterior.
    Spike se V_now >= (1 + spike_frac) × V_prev (ex.: spike_frac=3.0 → +300%, ou seja, 4×).

    Usa volume agregado da vela (OHLCV); em mercados líquidos reflete picos de execução no minuto.
    """
    sf = float(VOLUME_SPIKE_FRACAO_1M if spike_frac is None else spike_frac)
    try:
        ohlcv = exchange.fetch_ohlcv(simbolo, "1m", limit=5)
    except Exception:
        return False
    if not ohlcv or len(ohlcv) < 3:
        return False
    v_now = float(ohlcv[-2][5])
    v_prev = float(ohlcv[-3][5])
    if v_prev <= 1e-12:
        return False
    return v_now >= (1.0 + sf) * v_prev


def formatar_confluencia_para_llm(d: dict[str, Any]) -> str:
    """Texto para o Claude: ADX, VWAP (janela), posição vs VWAP, squeeze (regra 2% largura/preço)."""
    adx = d.get("adx")
    vw = d.get("vwap")
    pos = d.get("posicao_vwap")
    sq = d.get("squeeze")
    pr = d.get("preco")
    adx_s = f"{float(adx):.2f}" if adx is not None else "N/A"
    return (
        "=== CONFLUÊNCIA (100 velas 1h — VWAP por preço típico×volume; ADX 14; squeeze se (BBU−BBL)/preço < 2%) ===\n"
        f"ADX: {adx_s} | VWAP (janela): {vw} | Preço fechamento: {pr} | Posição vs VWAP: {pos}\n"
        f"Squeeze (faixa estreita): {sq}"
    )


def adicionar_adx_e_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """
    Acrescenta ADX(14) e VWAP diário (âncora «D») ao DataFrame OHLCV com coluna `timestamp`.
    Nomes típicos: ADX_14, …, vwap_d (série VWAP_D do pandas_ta).
    """
    out = df.copy()
    if len(out) < 30:
        return out

    adx_df = ta.adx(out["high"], out["low"], out["close"], length=14)
    if adx_df is not None and not adx_df.empty:
        out = pd.concat([out, adx_df], axis=1)

    if "timestamp" not in out.columns:
        return out

    tmp = out.set_index("timestamp")
    vw = ta.vwap(tmp.high, tmp.low, tmp.close, tmp.volume, anchor="D")
    if vw is None:
        return out.reset_index() if out.index.name == "timestamp" else out

    merged = tmp.assign(vwap_d=vw).reset_index()
    return merged


def largura_bollinger_pct(df: pd.DataFrame) -> pd.Series | None:
    """(BBU − BBL) / BBM — nomes pandas_ta BBU_*, BBL_*, BBM_*."""
    cols = list(df.columns)
    bbu = next((c for c in cols if str(c).startswith("BBU_")), None)
    bbl = next((c for c in cols if str(c).startswith("BBL_")), None)
    bbm = next((c for c in cols if str(c).startswith("BBM_")), None)
    if not (bbu and bbl and bbm):
        return None
    mid = df[bbm].replace(0, np.nan)
    return (df[bbu] - df[bbl]) / mid


def bollinger_squeeze(
    df: pd.DataFrame,
    *,
    janela_hist: int = 120,
    quantil: float = 0.15,
) -> bool:
    """
    «Squeeze»: largura BB atual no quantil inferior da distribuição recente (faixa comprimida).
    """
    bw = largura_bollinger_pct(df)
    if bw is None:
        return False
    s = bw.replace([np.inf, -np.inf], np.nan).dropna()
    if len(s) < max(50, janela_hist // 2):
        return False
    hist = s.iloc[:-1].tail(janela_hist)
    if len(hist) < 30:
        return False
    cur = float(s.iloc[-1])
    q = float(hist.quantile(quantil))
    return cur <= q


def mercado_lateral_por_adx(adx: float | None) -> bool:
    """True se ADX indica ausência de tendência forte (lateralização)."""
    if adx is None or (isinstance(adx, float) and np.isnan(adx)):
        return True
    return float(adx) < ADX_LIMIAR_TENDENCIA


def regime_adx_semantico(adx: float | None) -> str:
    """Classificação explícita para log / prompt (alinhado à camada TA)."""
    if adx is None or (isinstance(adx, float) and np.isnan(adx)):
        return "INDEFINIDO"
    x = float(adx)
    lo = min(ADX_LIMIAR_LATERAL_RUIDO, ADX_LIMIAR_TENDENCIA)
    hi = max(ADX_LIMIAR_LATERAL_RUIDO, ADX_LIMIAR_TENDENCIA)
    if x < lo:
        return "LATERAL_RUIDO"
    if x < hi:
        return "TRANSICAO"
    return "TENDENCIA_FORTE"


def legenda_forca_adx_prompt(adx: float | None) -> str:
    """Texto curto para o bloco do Claude (Final Boss)."""
    if adx is None or (isinstance(adx, float) and np.isnan(adx)):
        return "indisponível"
    x = float(adx)
    lo = min(ADX_LIMIAR_LATERAL_RUIDO, ADX_LIMIAR_TENDENCIA)
    hi = max(ADX_LIMIAR_LATERAL_RUIDO, ADX_LIMIAR_TENDENCIA)
    if x < lo:
        return f"mercado lateral / baixa força de tendência (ADX<{lo:.0f})"
    if x < hi:
        return f"zona de transição ({lo:.0f}≤ADX<{hi:.0f})"
    return f"tendência com força típica (ADX≥{hi:.0f})"


def texto_preco_vs_vwap(vies: str | None) -> str:
    """Rótulo legível para Preço vs VWAP diária."""
    m = {
        "ACIMA_VWAP": "ACIMA da VWAP diária",
        "ABAIXO_VWAP": "ABAIXO da VWAP diária",
        "NO_VWAP": "no VWAP (neutro)",
        "INDEFINIDO": "indefinido",
    }
    return m.get((vies or "").strip(), str(vies or "indefinido"))


def extrair_bollinger_pct_b_ultima(feat: pd.DataFrame) -> dict[str, Any]:
    """
    %B = (close − BBL) / (BBU − BBL) — Bollinger(20, 2σ) via pandas_ta.
    Retorna também extremos da última vela para auditoria.
    """
    out: dict[str, Any] = {
        "bb_pct_b": None,
        "bb_upper": None,
        "bb_lower": None,
        "bb_middle": None,
    }
    if feat is None or len(feat) < 1:
        return out
    cols = list(feat.columns)
    bbu = next((c for c in cols if str(c).startswith("BBU_")), None)
    bbl = next((c for c in cols if str(c).startswith("BBL_")), None)
    bbm = next((c for c in cols if str(c).startswith("BBM_")), None)
    if not bbu or not bbl:
        return out
    last = feat.iloc[-1]
    try:
        u = float(last[bbu])
        lo = float(last[bbl])
        cl = float(last["close"])
        out["bb_upper"], out["bb_lower"] = u, lo
        if bbm:
            out["bb_middle"] = float(last[bbm])
        span = u - lo
        if span and span > 1e-12 and not (isinstance(span, float) and np.isnan(span)):
            out["bb_pct_b"] = (cl - lo) / span
    except (TypeError, ValueError, KeyError):
        pass
    return out


def descrever_status_bollinger_para_prompt(snapshot: dict[str, Any]) -> str:
    """Resumo humano: %B + squeeze (esticado vs comprimido)."""
    pct_raw = snapshot.get("bb_pct_b")
    sq = bool(snapshot.get("bollinger_squeeze"))
    parts: list[str] = []
    if pct_raw is not None:
        try:
            p = float(pct_raw)
            if np.isnan(p):
                raise ValueError
            if p > 1.0:
                parts.append("preço acima da banda superior (esticado para cima)")
            elif p < 0.0:
                parts.append("preço abaixo da banda inferior (esticado para baixo)")
            elif p > 0.8:
                parts.append("próximo da banda superior (ainda dentro da faixa)")
            elif p < 0.2:
                parts.append("próximo da banda inferior (ainda dentro da faixa)")
            else:
                parts.append("posição intermediária entre as bandas")
            parts.append(f"%B={p:.3f}")
        except (TypeError, ValueError):
            parts.append("%B indisponível")
    else:
        parts.append("%B indisponível")
    if sq:
        parts.append(
            "squeeze ativo (bandas comprimidas — possível expansão de volatilidade)"
        )
    else:
        parts.append("sem squeeze forte (faixa típica ou bandas expandidas)")
    return "; ".join(parts)


def snapshot_para_contexto_raw_json(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Subconjunto serializável para coluna `contexto_raw` (Supabase / dashboard)."""
    keys = (
        "prob_ml",
        "adx_14",
        "adx_regime",
        "rsi_14",
        "vies_vwap",
        "preco_close",
        "vwap_d",
        "bb_pct_b",
        "bb_upper",
        "bb_lower",
        "bb_middle",
        "bollinger_squeeze",
        "mercado_lateral",
        "regime",
        "atr_pct",
        "bb_width_pct",
        "auric_price_action",
        "bb_squeeze_tight_002",
        "bb_width_expanding",
        "bb_breakout_long_ok",
        "bb_breakout_short_ok",
        "bb_width_frac_now",
        "bb_width_frac_prev",
    )
    out: dict[str, Any] = {}
    for k in keys:
        if k in snapshot and snapshot[k] is not None:
            v = snapshot[k]
            if isinstance(v, (float, np.floating)) and np.isnan(float(v)):
                continue
            out[k] = float(v) if isinstance(v, (float, np.floating)) else v
    cf = snapshot.get("confluencia")
    if isinstance(cf, dict) and cf:
        out["confluencia"] = cf
    return out


def formatar_log_contexto_raw(hub_text: str, snapshot: dict[str, Any]) -> str:
    """Concatena JSON de TA + texto bruto do Intelligence Hub para `contexto_raw`."""
    import json

    ta = snapshot_para_contexto_raw_json(snapshot)
    return (
        "=== INDICADORES_TA (JSON) ===\n"
        + json.dumps(ta, ensure_ascii=False, indent=2)
        + "\n\n=== INTELLIGENCE_HUB ===\n"
        + (hub_text or "")
    )


def vies_vwap(preco: float, vwap: float | None) -> str:
    """Viés institucional simples: preço vs VWAP diário."""
    if vwap is None or preco <= 0:
        return "INDEFINIDO"
    if preco > float(vwap):
        return "ACIMA_VWAP"
    if preco < float(vwap):
        return "ABAIXO_VWAP"
    return "NO_VWAP"


def rotulo_regime_rsi(rsi: float | None) -> str:
    """Legenda humana para RSI(14) no painel / dashboard."""
    if rsi is None or (isinstance(rsi, float) and np.isnan(rsi)):
        return "N/A"
    r = float(rsi)
    if r < 30:
        return "Sobrevenda"
    if r > 70:
        return "Sobrecompra"
    return "Equilíbrio"


def rotulo_regime_adx(adx: float | None) -> str:
    """ADX vs limiar de tendência (ex.: < ADX_LIMIAR_TENDENCIA = lateral)."""
    if adx is None or (isinstance(adx, float) and np.isnan(adx)):
        return "N/A"
    if float(adx) < ADX_LIMIAR_TENDENCIA:
        return "Mercado Lateral"
    return "Tendência"


def aplicar_boost_confianca_squeeze(
    prob_ml: float,
    squeeze: bool,
    confianca_atual: int,
) -> int:
    """
    Se P(ML) é muito alto e há squeeze, eleva a confiança operacional para CONFIANCA_BOOST_SQUEEZE.
    """
    if squeeze and float(prob_ml) >= ML_PROB_LIMIAR_BOOST_SQUEEZE:
        return max(int(confianca_atual), CONFIANCA_BOOST_SQUEEZE)
    return int(confianca_atual)


def formatar_bloco_indicadores_para_llm(snapshot: dict[str, Any]) -> str:
    """Detalhe operacional para o Claude (ADX / VWAP / Bollinger %B + squeeze)."""
    adx = snapshot.get("adx_14")
    sq = snapshot.get("bollinger_squeeze")
    bias = snapshot.get("vies_vwap", "INDEFINIDO")
    lateral = snapshot.get("mercado_lateral", True)
    adx_f: float | None = None
    if adx is not None:
        try:
            adx_f = float(adx)
            if isinstance(adx_f, float) and np.isnan(adx_f):
                adx_f = None
        except (TypeError, ValueError):
            adx_f = None
    adx_reg = str(snapshot.get("adx_regime") or regime_adx_semantico(adx_f))
    pct_b = snapshot.get("bb_pct_b")
    pct_txt = (
        f"{float(pct_b):.4f}"
        if pct_b is not None
        and not (isinstance(pct_b, float) and np.isnan(float(pct_b)))
        else "N/A"
    )
    lines = [
        "=== INDICADORES TÉCNICOS (ETH — referência institucional) ===",
        f"ADX(14) = {adx if adx is not None else 'N/A'} | regime={adx_reg} | "
        f"leitura: {legenda_forca_adx_prompt(adx_f)}. "
        f"Veto rompimento (sem squeeze): mercado_lateral se ADX<{ADX_LIMIAR_TENDENCIA:.0f}; "
        f"referência ruído típico: ADX<{ADX_LIMIAR_LATERAL_RUIDO:.0f}.",
        f"VWAP (diário): preço fechamento vs VWAP → viés = {bias} "
        f"({texto_preco_vs_vwap(bias)}). "
        "Acima do VWAP favorece leitura altista de curto prazo; abaixo, baixista.",
        f"Bollinger (20, 1.5σ): %B = {pct_txt} — {descrever_status_bollinger_para_prompt(snapshot)}",
        f"Compressão squeeze (BB vs histórico) = {'SIM — possível expansão de volatilidade iminente' if sq else 'NÃO'}.",
    ]
    if lateral and not sq:
        lines.append(
            "Regra operacional: com ADX baixo e sem squeeze, desvalorizar sinais de «rompimento» "
            "puro; preferir mean-reversion/reversão se o contexto macro alinhar."
        )
    if sq and snapshot.get("prob_ml") is not None:
        p = float(snapshot["prob_ml"])
        if p >= ML_PROB_LIMIAR_BOOST_SQUEEZE:
            lines.append(
                f"P(ML)={p:.1%} com squeeze: tratar como setup de alta convicção potencial "
                f"(alvo de confiança operacional ≥ {CONFIANCA_BOOST_SQUEEZE}% se resto alinhado)."
            )
    pa = snapshot.get("auric_price_action")
    if isinstance(pa, dict) and pa.get("resumo") not in (None, "", "—"):
        import json

        lines.append("=== PRICE ACTION (Auric) ===")
        lines.append(json.dumps(pa, ensure_ascii=False, indent=2))
    return "\n".join(lines)
