"""
Supabase: registra cada decisão do modelo e cada execução de trade.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from supabase import Client, create_client

_client: Client | None = None


def get_client() -> Client:
    global _client
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        or os.environ.get("SUPABASE_KEY", "").strip()
    )
    if not url or not key:
        raise RuntimeError(
            "Defina SUPABASE_URL e SUPABASE_KEY (ou SUPABASE_SERVICE_ROLE_KEY) no ambiente (.env)."
        )
    if _client is None:
        _client = create_client(url, key)
    return _client


def log_decision(
    *,
    symbol: str,
    action: str,
    confidence: Optional[float],
    raw_response: str,
    market_snapshot: dict[str, Any],
    model: Optional[str] = None,
    replicate_prediction_id: Optional[str] = None,
) -> dict[str, Any]:
    payload = {
        "symbol": symbol,
        "action": action.upper(),
        "confidence": confidence,
        "raw_response": raw_response,
        "market_snapshot": market_snapshot,
        "model": model,
        "replicate_prediction_id": replicate_prediction_id,
    }
    res = get_client().table("decisions").insert(payload).execute()
    if res.data and len(res.data) > 0:
        return res.data[0]
    return {}


def log_trade(
    *,
    symbol: str,
    side: str,
    amount: float,
    price: Optional[float],
    order_id: Optional[str],
    status: str,
    decision_id: Optional[str],
    raw_exchange: Optional[dict[str, Any]] = None,
    mode: str = "paper",
) -> dict[str, Any]:
    payload = {
        "symbol": symbol,
        "side": side.lower(),
        "amount": amount,
        "price": price,
        "order_id": order_id,
        "status": status,
        "decision_id": decision_id,
        "raw_exchange": raw_exchange or {},
        "mode": mode,
    }
    res = get_client().table("trade_logs").insert(payload).execute()
    if res.data and len(res.data) > 0:
        return res.data[0]
    return {}
