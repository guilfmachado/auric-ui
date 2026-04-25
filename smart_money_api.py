"""
Clientes de APIs externas para Smart Money / Macro Sentiment.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class CoinMarketCapClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = (api_key or os.getenv("COINMARKETCAP_API_KEY") or "").strip()
        self.base_url = "https://pro-api.coinmarketcap.com/v1/global-metrics/quotes/latest"

    def fetch_global_metrics(self) -> dict[str, Any]:
        if not self.api_key:
            return {}
        req = urllib.request.Request(
            self.base_url,
            headers={
                "X-CMC_PRO_API_KEY": self.api_key,
                "Accept": "application/json",
                "User-Agent": "AuricSmartMoney/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
            return {}

    def macro_sentiment_score(self) -> float:
        """
        Score simples (-1..1) com:
        - volume_change_24h
        - btc_dominance / eth_dominance
        """
        gm = self.fetch_global_metrics()
        if not gm:
            return 0.0
        vol_24h = _safe_float(gm.get("quote", {}).get("USD", {}).get("total_volume_24h_yesterday_percentage_change"))
        btc_dom = _safe_float(gm.get("btc_dominance"))
        eth_dom = _safe_float(gm.get("eth_dominance"))

        score = 0.0
        if vol_24h > 8.0:
            score += 0.4
        elif vol_24h < -8.0:
            score -= 0.4
        if btc_dom > 54.0:
            score -= 0.2
        elif btc_dom < 48.0:
            score += 0.2
        if eth_dom > 19.0:
            score += 0.2
        elif eth_dom < 16.0:
            score -= 0.2
        return max(-1.0, min(1.0, score))


class CoinApiClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = (api_key or os.getenv("COINAPI_KEY") or "").strip()
        self.base_url = "https://rest.coinapi.io/v1"

    def fetch_latest_trades(self, symbol_id: str = "BINANCE_SPOT_ETH_USDT", limit: int = 50) -> list[dict[str, Any]]:
        if not self.api_key:
            return []
        url = f"{self.base_url}/trades/{symbol_id}/latest?limit={int(limit)}"
        req = urllib.request.Request(
            url,
            headers={
                "X-CoinAPI-Key": self.api_key,
                "Accept": "application/json",
                "User-Agent": "AuricSmartMoney/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            return data if isinstance(data, list) else []
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
            return []

    def whale_flow_score(self, symbol_id: str = "BINANCE_SPOT_ETH_USDT", limit: int = 50) -> float:
        """
        Se volume comprador nas últimas N transações > média por margem relevante -> score positivo.
        Se vendedor domina -> score negativo.
        """
        trades = self.fetch_latest_trades(symbol_id=symbol_id, limit=limit)
        if not trades:
            return 0.0
        buy_vol = 0.0
        sell_vol = 0.0
        for t in trades:
            sz = _safe_float(t.get("size"))
            side = str(t.get("taker_side") or "").upper()
            if side == "BUY":
                buy_vol += sz
            elif side == "SELL":
                sell_vol += sz
        total = buy_vol + sell_vol
        if total <= 0:
            return 0.0
        imbalance = (buy_vol - sell_vol) / total
        return max(-1.0, min(1.0, float(imbalance)))
