"""
Smart Money Flow + utilitários de inteligência para veto de bull trap.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from smart_money_api import CoinApiClient, CoinMarketCapClient

WHALE_ALERT_API_BASE = os.getenv("WHALE_ALERT_API_BASE", "https://api.whale-alert.io/v1/transactions")
WHALE_ALERT_API_KEY = (os.getenv("WHALE_ALERT_API_KEY") or "").strip()
SMART_MONEY_LOOKBACK_MINUTES = int(os.getenv("SMART_MONEY_LOOKBACK_MINUTES", "30"))
SMART_MONEY_INFLOW_THRESHOLD_USDT = float(os.getenv("SMART_MONEY_INFLOW_THRESHOLD_USDT", "50000000"))
SMART_MONEY_OUTFLOW_THRESHOLD_USDT = float(os.getenv("SMART_MONEY_OUTFLOW_THRESHOLD_USDT", "20000000"))

_CACHE_TTL_S = 30.0
_last_flow_snapshot: dict[str, Any] | None = None
_last_flow_ts: float = 0.0
_cmc_client = CoinMarketCapClient()
_coinapi_client = CoinApiClient()


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _is_binance_label(label: str) -> bool:
    s = str(label or "").strip().lower()
    return "binance" in s


def _is_cold_wallet_label(label: str) -> bool:
    s = str(label or "").strip().lower()
    cold_tokens = ("cold", "unknown", "wallet", "self custody", "trezor", "ledger")
    return any(tok in s for tok in cold_tokens)


def _fetch_whale_alert_transactions(*, lookback_minutes: int) -> list[dict[str, Any]]:
    if not WHALE_ALERT_API_KEY:
        return []
    end_ts = int(time.time())
    start_ts = max(0, end_ts - int(lookback_minutes) * 60)
    q = urllib.parse.urlencode(
        {
            "api_key": WHALE_ALERT_API_KEY,
            "start": start_ts,
            "end": end_ts,
            "limit": 100,
            "min_value": 500000,
        }
    )
    url = f"{WHALE_ALERT_API_BASE}?{q}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "AuricSmartMoney/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(body)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
        return []
    txs = payload.get("transactions")
    if not isinstance(txs, list):
        return []
    return [t for t in txs if isinstance(t, dict)]


def _summarize_whale_alert_flows(transactions: list[dict[str, Any]]) -> dict[str, float]:
    inflow_usdt_binance = 0.0
    outflow_usdt_binance = 0.0
    cold_outflow_btc_eth = 0.0
    for tx in transactions:
        symbol = str(tx.get("symbol") or "").upper()
        amount_usd = _safe_float(tx.get("amount_usd"))
        frm = tx.get("from") or {}
        to = tx.get("to") or {}
        from_owner = str((frm.get("owner") if isinstance(frm, dict) else "") or "")
        to_owner = str((to.get("owner") if isinstance(to, dict) else "") or "")
        from_owner_type = str((frm.get("owner_type") if isinstance(frm, dict) else "") or "")
        to_owner_type = str((to.get("owner_type") if isinstance(to, dict) else "") or "")

        from_binance = _is_binance_label(from_owner) or _is_binance_label(from_owner_type)
        to_binance = _is_binance_label(to_owner) or _is_binance_label(to_owner_type)
        to_cold = _is_cold_wallet_label(to_owner) or _is_cold_wallet_label(to_owner_type)

        if symbol == "USDT":
            if to_binance:
                inflow_usdt_binance += amount_usd
            if from_binance and not to_binance:
                outflow_usdt_binance += amount_usd
        if symbol in ("BTC", "ETH") and from_binance and to_cold:
            cold_outflow_btc_eth += amount_usd

    return {
        "inflow_usdt_binance": float(inflow_usdt_binance),
        "outflow_usdt_binance": float(outflow_usdt_binance),
        "cold_outflow_btc_eth_usd": float(cold_outflow_btc_eth),
    }


def _simulate_whale_flow_from_order_book(exchange: Any, symbol: str) -> dict[str, float]:
    """
    Fallback sem Whale Alert:
    - asks_5pct_notional >> bids_5pct_notional sugere potencial entrada de liquidez para venda.
    - bids_5pct_notional >> asks_5pct_notional sugere suporte comprador.
    """
    try:
        ob = exchange.fetch_order_book(symbol, limit=200) or {}
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
    except Exception:
        return {
            "inflow_usdt_binance": 0.0,
            "outflow_usdt_binance": 0.0,
            "cold_outflow_btc_eth_usd": 0.0,
        }

    bids_notional = sum(_safe_float(px) * _safe_float(qty) for px, qty, *_ in bids[:120])
    asks_notional = sum(_safe_float(px) * _safe_float(qty) for px, qty, *_ in asks[:120])
    if asks_notional > bids_notional * 1.35:
        return {
            "inflow_usdt_binance": min(asks_notional - bids_notional, SMART_MONEY_INFLOW_THRESHOLD_USDT),
            "outflow_usdt_binance": 0.0,
            "cold_outflow_btc_eth_usd": 0.0,
        }
    if bids_notional > asks_notional * 1.35:
        return {
            "inflow_usdt_binance": 0.0,
            "outflow_usdt_binance": min(bids_notional - asks_notional, SMART_MONEY_OUTFLOW_THRESHOLD_USDT),
            "cold_outflow_btc_eth_usd": 0.0,
        }
    return {
        "inflow_usdt_binance": 0.0,
        "outflow_usdt_binance": 0.0,
        "cold_outflow_btc_eth_usd": 0.0,
    }


def _price_change_last_30m(exchange: Any, symbol: str) -> float | None:
    try:
        rows = exchange.fetch_ohlcv(symbol, timeframe="5m", limit=7)
        if not rows or len(rows) < 7:
            return None
        old_px = _safe_float(rows[0][4])
        new_px = _safe_float(rows[-1][4])
        if old_px <= 0:
            return None
        return (new_px - old_px) / old_px
    except Exception:
        return None


def obter_smart_money_flow(exchange: Any, symbol: str) -> dict[str, Any]:
    """
    Score:
    - +1.0: influxo > 50M USDT para Binance (30min)
    - +0.5: saída BTC/ETH de Binance para cold wallets (bullish)
    - -1.0: saída material de USDT da Binance (saída de liquidez)
    """
    global _last_flow_snapshot, _last_flow_ts
    now = time.time()
    if _last_flow_snapshot is not None and (now - _last_flow_ts) <= _CACHE_TTL_S:
        return dict(_last_flow_snapshot)

    txs = _fetch_whale_alert_transactions(lookback_minutes=SMART_MONEY_LOOKBACK_MINUTES)
    if txs:
        flow = _summarize_whale_alert_flows(txs)
        source = "whale_alert"
    else:
        flow = _simulate_whale_flow_from_order_book(exchange, symbol)
        source = "order_book_fallback"

    inflow = float(flow.get("inflow_usdt_binance") or 0.0)
    outflow = float(flow.get("outflow_usdt_binance") or 0.0)
    cold = float(flow.get("cold_outflow_btc_eth_usd") or 0.0)
    coinapi_whale = float(_coinapi_client.whale_flow_score())
    cmc_macro = float(_cmc_client.macro_sentiment_score())

    whale_score = 0.0
    signal = "NEUTRAL"
    if inflow >= SMART_MONEY_INFLOW_THRESHOLD_USDT:
        whale_score = 1.0
        signal = "USDT_INFLOW_BINANCE"
    elif cold > 0:
        whale_score = 0.5
        signal = "BTC_ETH_TO_COLD_WALLETS"
    elif outflow >= SMART_MONEY_OUTFLOW_THRESHOLD_USDT:
        whale_score = -1.0
        signal = "USDT_LIQUIDITY_OUTFLOW"

    price_change_30m = _price_change_last_30m(exchange, symbol)
    possible_bull_trap = bool((whale_score < 0.0) and (price_change_30m is not None and price_change_30m > 0))

    whale_flow_score = coinapi_whale if abs(coinapi_whale) > 1e-12 else whale_score
    social_sentiment_score = cmc_macro
    snap = {
        "whale_score": float(whale_score),
        "whale_flow_score": float(whale_flow_score),
        "social_sentiment_score": float(social_sentiment_score),
        "signal": signal,
        "source": source,
        "coinapi_source": "coinapi_trades_latest",
        "cmc_source": "cmc_global_metrics",
        "inflow_usdt_binance": inflow,
        "outflow_usdt_binance": outflow,
        "cold_outflow_btc_eth_usd": cold,
        "lookback_minutes": int(SMART_MONEY_LOOKBACK_MINUTES),
        "price_change_30m": price_change_30m,
        "possible_bull_trap": possible_bull_trap,
        "updated_at": int(now),
    }
    _last_flow_snapshot = dict(snap)
    _last_flow_ts = now
    return snap
