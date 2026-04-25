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


def log_audit(
    *,
    symbol: str | None = None,
    action: str | None = None,
    decision: str | None = None,
    reason: str | None = None,
    spread: float | None = 0.0,
    volume: float | int | None = 0,
    risk_score: float | None = None,
    # Compatibilidade com chamadas antigas do código:
    pair: str | None = None,
    signal_type: str | None = None,
    order_book_pressure: float | None = None,
) -> dict[str, Any]:
    """
    Auditoria resiliente com blindagem de campos vazios.
    """
    try:
        symbol_v = (symbol or pair or "ETHUSDC")
        action_v = (action or signal_type or "UNKNOWN")
        decision_v = str(decision or "PENDING").strip().upper()
        reason_v = str(reason or "Motivo não especificado").strip()
        spread_src = spread if spread is not None else order_book_pressure
        spread_v = float(spread_src) if spread_src is not None else 0.0
        volume_v = float(volume) if volume is not None else 0.0

        payload = {
            "symbol": str(symbol_v).strip() or "ETHUSDC",
            "action": str(action_v).strip().upper() or "UNKNOWN",
            "decision": decision_v if decision_v else "PENDING",
            "reason": reason_v if reason_v else "Motivo não especificado",
            "spread": spread_v,
            "volume": volume_v,
            "risk_score": (float(risk_score) if risk_score is not None else None),
        }

        print(f"[DEBUG PAYLOAD EXATO]: {payload}")
        res = get_client().table("trade_logs").insert(payload).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]
        return {}
    except Exception as e:  # noqa: BLE001
        print(f"[ERRO SUPABASE] Falha ao enviar auditoria: {e}")
        return {}


def get_recent_trade_logs(symbol: str, limit: int = 8) -> list[dict[str, Any]]:
    """
    Últimos logs do par em `trade_logs` para contexto de auditoria.
    """
    sym = (symbol or "").strip()
    if not sym:
        return []
    try:
        res = (
            get_client()
            .table("trade_logs")
            .select("*")
            .eq("symbol", sym)
            .order("id", desc=True)
            .limit(max(1, int(limit)))
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
        return [r for r in rows if isinstance(r, dict)]
    except Exception:
        return []
