"""
Modelo de machine learning para prever a direção do próximo fechamento do ETH/USDT (1h).

Pipeline: download (ccxt) → indicadores (pandas_ta) → alvo binário → XGBoost (padrão) → backtest.

No macOS, se o XGBoost não carregar (libxgboost / libomp), instale OpenMP: brew install libomp
"""

from __future__ import annotations

import warnings
from typing import Any

import ccxt
import numpy as np
import pandas as pd
import pandas_ta as ta

from indicators import adicionar_adx_e_vwap
from sklearn.metrics import accuracy_score, precision_score
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

warnings.filterwarnings("ignore", category=UserWarning)

SYMBOL = "ETH/USDT"
TIMEFRAME = "1h"
N_CANDLES = 5000
# Probabilidade mínima P(classe=1) para emitir sinal de compra no backtest.
CONFIDENCE_THRESHOLD = 0.60


def ajustar_probabilidade_com_whale_flow(prob_alta: float, whale_flow_score: float | None) -> float:
    """
    Feature extra de Smart Money para o motor de decisão:
    - score positivo desloca P(alta) para baixo (risco de distribuição em exchange).
    - score negativo desloca P(alta) para cima levemente (saída de liquidez = squeeze possível).
    """
    p = max(0.0, min(1.0, float(prob_alta)))
    if whale_flow_score is None:
        return p
    s = float(whale_flow_score)
    # Shift pequeno e controlado para não sobrepor o modelo.
    shift = max(-0.08, min(0.08, -0.05 * s))
    return max(0.0, min(1.0, p + shift))


def fetch_ohlcv_binance(
    symbol: str = SYMBOL,
    timeframe: str = TIMEFRAME,
    total: int = N_CANDLES,
) -> pd.DataFrame:
    """
    Baixa as últimas `total` velas OHLCV de 1h apenas para o par indicado (ETH/USDT).

    A Binance limita velas por requisição (~1000); paginamos até reunir `total` observações
    e mantemos as mais recentes (série cronológica crescente).
    """
    exchange = ccxt.binance({"enableRateLimit": True})
    timeframe_sec = exchange.parse_timeframe(timeframe)
    timeframe_ms = int(timeframe_sec * 1000)

    now_ms = exchange.milliseconds()
    since = now_ms - total * timeframe_ms

    all_rows: list[list[float]] = []
    while len(all_rows) < total:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not batch:
            break
        all_rows.extend(batch)
        since = int(batch[-1][0]) + 1
        if len(batch) < 1000:
            break

    if len(all_rows) < total:
        raise RuntimeError(
            f"Foram obtidas apenas {len(all_rows)} velas; verifique o símbolo ou a rede."
        )

    all_rows = all_rows[-total:]

    df = pd.DataFrame(
        all_rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def add_technical_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Features apenas com pandas_ta e derivações explícitas:

    - RSI(14): força relativa do movimento.
    - MACD(12, 26, 9): momentum / tendência via médias exponenciais.
    - Bollinger(20, 2σ): volatilidade em torno da média.
    - EMA(20), EMA(50): posição em relação a médias curtas e médias.
    - Variação percentual do volume: (V_t - V_{t-1}) / V_{t-1}.
    - ATR(14): amplitude típica (volatilidade absoluta).
    - dist_ema_50: (Close - EMA_50) / EMA_50 × 100 — distância percentual do preço à EMA 50.
    """
    out = df.copy()

    out["rsi"] = ta.rsi(out["close"], length=14)

    macd_df = ta.macd(out["close"], fast=12, slow=26, signal=9)
    if macd_df is not None and not macd_df.empty:
        out = pd.concat([out, macd_df], axis=1)

    from indicators import BBANDS_LENGTH, BBANDS_STD

    bb = ta.bbands(out["close"], length=BBANDS_LENGTH, std=BBANDS_STD)
    if bb is not None and not bb.empty:
        out = pd.concat([out, bb], axis=1)

    out["ema_20"] = ta.ema(out["close"], length=20)
    out["ema_50"] = ta.ema(out["close"], length=50)

    prev_vol = out["volume"].shift(1)
    out["volume_pct_change"] = (out["volume"] - prev_vol) / prev_vol.replace(0, np.nan)

    atr_raw = ta.atr(out["high"], out["low"], out["close"], length=14)
    out["atr"] = atr_raw.iloc[:, 0] if isinstance(atr_raw, pd.DataFrame) else atr_raw

    ema50_safe = out["ema_50"].replace(0, np.nan)
    out["dist_ema_50"] = (out["close"] - ema50_safe) / ema50_safe * 100.0

    out = adicionar_adx_e_vwap(out)
    return out


def build_target(df: pd.DataFrame) -> pd.DataFrame:
    """
    Alvo clássico: 1 se o próximo fechamento for estritamente maior que o atual; 0 se for menor
    ou igual. A última linha não tem próximo candle (alvo NaN, removido no dropna).
    """
    out = df.copy()
    nxt = out["close"].shift(-1)
    out["target"] = (nxt > out["close"]).astype(float)
    out.loc[out.index[-1], "target"] = np.nan
    return out


def select_feature_columns(df: pd.DataFrame) -> list[str]:
    """Colunas numéricas usadas como X (exclui OHLCV bruto, timestamp e alvo)."""
    exclude = {"timestamp", "open", "high", "low", "close", "volume", "target"}
    return sorted(
        c for c in df.columns if c not in exclude and df[c].dtype != object
    )


def _bb_width_pct_series(df: pd.DataFrame) -> pd.Series | None:
    """Delega para `indicators.largura_bollinger_pct` (única definição de largura BB)."""
    from indicators import largura_bollinger_pct

    return largura_bollinger_pct(df)


def _regime_volatilidade_de_feat(feat: pd.DataFrame) -> dict[str, Any]:
    """Núcleo de `obter_regime_volatilidade` a partir de OHLCV já com features."""
    from indicators import bollinger_squeeze as bb_squeeze_flag

    feat = feat.replace([np.inf, -np.inf], np.nan)

    if feat.empty or "atr" not in feat.columns:
        return {
            "regime": "BAIXA",
            "atr_pct": None,
            "atr_pct_median": None,
            "bb_width_pct": None,
            "bb_width_median": None,
            "bollinger_squeeze": False,
            "detalhe": "sem ATR",
        }

    atr_pct = (feat["atr"] / feat["close"]).dropna()
    if len(atr_pct) < 50:
        return {
            "regime": "BAIXA",
            "atr_pct": float(atr_pct.iloc[-1]) if len(atr_pct) else None,
            "atr_pct_median": None,
            "bb_width_pct": None,
            "bb_width_median": None,
            "bollinger_squeeze": False,
            "detalhe": "série ATR% curta",
        }

    cur_atr_pct = float(atr_pct.iloc[-1])
    hist_atr = atr_pct.iloc[:-1].tail(160)
    med_atr_pct = float(hist_atr.median())
    v_atr = "BAIXA" if cur_atr_pct <= med_atr_pct else "ALTA"

    votes: list[str] = [v_atr]
    bbw = _bb_width_pct_series(feat)
    cur_bw: float | None = None
    med_bw: float | None = None
    if bbw is not None:
        bw_clean = bbw.replace([np.inf, -np.inf], np.nan).dropna()
        if len(bw_clean) >= 50:
            cur_bw = float(bw_clean.iloc[-1])
            hist_bw = bw_clean.iloc[:-1].tail(120)
            med_bw = float(hist_bw.median())
            votes.append("BAIXA" if cur_bw <= med_bw else "ALTA")

    bollinger_squeeze = bool(bb_squeeze_flag(feat))

    n_baixa = sum(1 for v in votes if v == "BAIXA")
    if n_baixa > len(votes) / 2:
        regime = "BAIXA"
    elif n_baixa < len(votes) / 2:
        regime = "ALTA"
    else:
        regime = v_atr  # empate ATR vs BB → prevalece ATR

    return {
        "regime": regime,
        "atr_pct": cur_atr_pct,
        "atr_pct_median": med_atr_pct,
        "bb_width_pct": cur_bw,
        "bb_width_median": med_bw,
        "bollinger_squeeze": bollinger_squeeze,
        "detalhe": f"ATR→{v_atr}" + (f", BB→{votes[-1]}" if len(votes) > 1 else ""),
    }


def _extrair_macd_snapshot(feat: pd.DataFrame) -> dict[str, Any]:
    """
    Extrai MACD(12,26,9) da última vela fechada:
    - macd_line
    - signal_line
    - macd_hist
    - macd_estado (texto para leitura humana)
    """
    out = {
        "macd_line": None,
        "signal_line": None,
        "macd_hist": None,
        "macd_estado": "Indefinido",
    }
    if feat.empty:
        return out

    macd_col = "MACD_12_26_9"
    signal_col = "MACDs_12_26_9"
    hist_col = "MACDh_12_26_9"
    if macd_col not in feat.columns or signal_col not in feat.columns or hist_col not in feat.columns:
        return out

    def _to_f(x: Any) -> float | None:
        try:
            if x is None or (isinstance(x, float) and np.isnan(x)):
                return None
            return float(x)
        except (TypeError, ValueError):
            return None

    last = feat.iloc[-1]
    prev = feat.iloc[-2] if len(feat) >= 2 else None
    macd_line = _to_f(last.get(macd_col))
    signal_line = _to_f(last.get(signal_col))
    hist = _to_f(last.get(hist_col))
    prev_hist = _to_f(prev.get(hist_col)) if prev is not None else None

    estado = "Indefinido"
    if macd_line is not None and signal_line is not None and hist is not None:
        if macd_line > signal_line and hist > 0:
            estado = "Cruzamento de Alta (Bullish Momentum)"
        elif macd_line < signal_line and hist < 0:
            estado = "Cruzamento de Baixa (Bearish Momentum)"
        if prev_hist is not None and abs(hist) < abs(prev_hist):
            estado = "Perda de Momentum"

    out.update(
        {
            "macd_line": macd_line,
            "signal_line": signal_line,
            "macd_hist": hist,
            "macd_estado": estado,
        }
    )
    return out


def obter_regime_volatilidade(
    symbol: str = SYMBOL,
    timeframe: str = TIMEFRAME,
    n_candles: int = 320,
) -> dict[str, Any]:
    """
    Regime de volatilidade para contexto quando P(ML) está na zona neutra (sem direção clara).

    Usa ATR% (ATR/close) e, se existir, largura relativa das Bandas de Bollinger, cada uma
    comparada à mediana de uma janela histórica (última barra excluída da mediana).
    Votos: maioria define «BAIXA» vs «ALTA»; empate favorece ATR.
    """
    ohlcv = fetch_ohlcv_binance(symbol=symbol, timeframe=timeframe, total=n_candles)
    feat = add_technical_features(ohlcv)
    return _regime_volatilidade_de_feat(feat)


def obter_snapshot_indicadores_eth(
    symbol: str = SYMBOL,
    timeframe: str = TIMEFRAME,
    n_candles: int = 400,
    *,
    prob_ml: float | None = None,
) -> dict[str, Any]:
    """
    Última vela: ADX, VWAP diário, viés preço/VWAP, squeeze BB, regime ATR/BB.
    Um único download OHLCV por ciclo quando usado em vez de chamadas separadas.
    """
    from indicators import (
        analisar_bb_entrada_squeeze_breakout,
        extrair_bollinger_pct_b_ultima,
        mercado_lateral_por_adx,
        regime_adx_semantico,
        vies_vwap,
    )

    ohlcv = fetch_ohlcv_binance(symbol=symbol, timeframe=timeframe, total=n_candles)
    feat = add_technical_features(ohlcv)
    reg = _regime_volatilidade_de_feat(feat)
    last = feat.iloc[-1]
    bb_extra = extrair_bollinger_pct_b_ultima(feat)
    bb_entrada = analisar_bb_entrada_squeeze_breakout(feat)

    def _to_f(x: Any) -> float | None:
        try:
            if x is None or (isinstance(x, float) and np.isnan(x)):
                return None
            return float(x)
        except (TypeError, ValueError):
            return None

    adx = _to_f(last.get("ADX_14"))
    vwap = _to_f(last.get("vwap_d"))
    rsi = _to_f(last.get("rsi"))
    close = _to_f(last.get("close")) or 0.0

    lateral = mercado_lateral_por_adx(adx)
    bias = vies_vwap(close, vwap)
    adx_regime = regime_adx_semantico(adx)

    macd = _extrair_macd_snapshot(feat)

    out: dict[str, Any] = {
        **reg,
        **bb_extra,
        **bb_entrada,
        **macd,
        "adx_14": adx,
        "adx_regime": adx_regime,
        "rsi_14": rsi,
        "vwap_d": vwap,
        "preco_close": close,
        "vies_vwap": bias,
        "mercado_lateral": lateral,
        "prob_ml": prob_ml,
    }
    return out


def obter_sinal_atual(
    symbol: str = SYMBOL,
    timeframe: str = TIMEFRAME,
    n_candles: int = N_CANDLES,
) -> float:
    """
    Modo tempo real: baixa velas recentes, calcula indicadores, treina o XGBoost com *todas*
    as linhas que possuem alvo válido (sem split treino/teste) e devolve apenas
    P(classe=1 | última vela), ou seja, a probabilidade de alta no próximo fechamento
    condicionada ao estado atual do mercado (última linha).
    """
    ohlcv = fetch_ohlcv_binance(symbol=symbol, timeframe=timeframe, total=n_candles)
    feat = add_technical_features(ohlcv)
    feat = build_target(feat)
    feature_cols = select_feature_columns(feat)
    feat[feature_cols] = feat[feature_cols].replace([np.inf, -np.inf], np.nan)

    # Estado atual = última vela (ainda sem retorno futuro; target é NaN).
    X_atual = feat[feature_cols].iloc[[-1]]
    if X_atual.isna().any().any():
        raise RuntimeError(
            "Indicadores incompletos na última vela. Aumente N_CANDLES ou verifique os dados."
        )

    # Treino: apenas linhas com alvo observado e features completas.
    treino = feat.loc[feat["target"].notna(), feature_cols + ["target"]].dropna()
    if treino.empty or len(treino) < 10:
        raise RuntimeError("Dados insuficientes para treinar o modelo.")

    X = treino[feature_cols]
    y = treino["target"].astype(int)

    clf = XGBClassifier()
    clf.fit(X, y)

    proba = clf.predict_proba(X_atual)[0, 1]
    return float(proba)


def main() -> None:
    print("1) Baixando 5000 velas 1h ETH/USDT (ccxt)...")
    ohlcv = fetch_ohlcv_binance()

    print("2) Feature engineering (pandas_ta)...")
    feat = add_technical_features(ohlcv)
    feat = build_target(feat)

    feature_cols = select_feature_columns(feat)
    model_df = feat[feature_cols + ["target"]]
    model_df[feature_cols] = model_df[feature_cols].replace([np.inf, -np.inf], np.nan)
    model_df = model_df.dropna()

    X = model_df[feature_cols]
    y = model_df["target"].astype(int)

    print("3) Split temporal 80/20 (shuffle=False)...")
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, shuffle=False,
    )

    print("4) Treinando XGBClassifier (hiperparâmetros padrão da biblioteca)...")
    clf = XGBClassifier()
    clf.fit(X_train, y_train)

    print("5) Avaliação no teste com predict_proba + limiar de confiança...")
    proba = clf.predict_proba(X_test)
    p1 = proba[:, 1]
    y_hat = (p1 > CONFIDENCE_THRESHOLD).astype(int)

    acc = accuracy_score(y_test, y_hat)
    prec1 = precision_score(
        y_test, y_hat, pos_label=1, average="binary", zero_division=0,
    )
    n_test = len(y_test)
    n_trades = int((y_hat == 1).sum())

    print("\n--- Conjunto de teste ---")
    print(f"Limiar: P(classe=1) > {CONFIDENCE_THRESHOLD:.0%} → sinal de compra ('1').")
    print(f"Acurácia (Accuracy): {acc:.4f}")
    print(f"Precision (classe 1): {prec1:.4f}")
    print(f"Trades (sinais '1'): {n_trades} de {n_test} linhas.")


if __name__ == "__main__":
    main()
