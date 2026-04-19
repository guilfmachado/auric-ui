"""
Modelo de machine learning para prever a direção do próximo fechamento do ETH/USDT (1h).

Pipeline: download (ccxt) → indicadores (pandas_ta) → alvo binário → XGBoost (padrão) → backtest.

No macOS, se o XGBoost não carregar (libxgboost / libomp), instale OpenMP: brew install libomp
"""

from __future__ import annotations

import warnings

import ccxt
import numpy as np
import pandas as pd
import pandas_ta as ta
from sklearn.metrics import accuracy_score, precision_score
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

warnings.filterwarnings("ignore", category=UserWarning)

SYMBOL = "ETH/USDT"
TIMEFRAME = "1h"
N_CANDLES = 5000
# Probabilidade mínima P(classe=1) para emitir sinal de compra no backtest.
CONFIDENCE_THRESHOLD = 0.60


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

    bb = ta.bbands(out["close"], length=20, std=2.0)
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
