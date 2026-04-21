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


def obter_indicadores_confluencia(
    exchange: Any,
    simbolo: str,
    *,
    timeframe: str = "1h",
    limit: int = 100,
) -> dict[str, Any]:
    """
    VWAP aproximado por (preço típico × volume) / volume acumulado na janela;
    ADX(14); Bollinger(20, 2); squeeze se (BBU − BBL) / preço < 2%.

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
    bb_df = ta.bbands(df["close"], length=20, std=2.0)
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
        f"Bollinger (20, 2σ): %B = {pct_txt} — {descrever_status_bollinger_para_prompt(snapshot)}",
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
    return "\n".join(lines)
