from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import logger

ENTRY_ACTIONS = {
    "COMPRA_LONG",
    "COMPRA_LONG_LIMIT",
    "COMPRA_LONG_MARKET",
    "COMPRA_MARKET",
    "ABRE_SHORT",
    "ABRE_SHORT_LIMIT",
    "ABRE_SHORT_MARKET",
    "RECON_EMERGENCY_LONG",
    "RECON_EMERGENCY_SHORT",
}
EXIT_ACTIONS = {
    "VENDA_PROFIT",
    "VENDA_STOP",
    "STALL_EXIT_SHORT",
    "CLOSE_MANUAL",
    "CLOSE_ALL_MANUAL",
}


@dataclass
class TradeFeedback:
    side: str
    entry_price: float
    exit_price: float
    potential_roi_pct: float
    realized_roi_pct: float
    efficiency_pct: float
    exit_action: str
    rsi: float | None
    volatility: float | None
    spread: float | None
    exit_meta: dict[str, Any] = field(default_factory=dict)


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _parse_exit_meta(contexto_raw: str | None) -> dict[str, Any]:
    if not contexto_raw:
        return {}
    try:
        obj = json.loads(contexto_raw)
        em = obj.get("auric_exit_meta")
        return em if isinstance(em, dict) else {}
    except Exception:
        return {}


def _parse_ta_json_block(contexto_raw: str | None) -> dict[str, Any]:
    """Extrai o objeto JSON de indicadores (formato `formatar_log_contexto_raw` do main)."""
    if not contexto_raw:
        return {}
    marker = "=== INDICADORES_TA (JSON) ==="
    if marker not in contexto_raw:
        return {}
    try:
        rest = contexto_raw.split(marker, 1)[1].strip()
        start = rest.find("{")
        if start < 0:
            return {}
        depth = 0
        for j in range(start, len(rest)):
            ch = rest[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(rest[start : j + 1])
    except Exception:
        return {}
    return {}


def _count_price_action_vetoes(rows: list[dict[str, Any]], tail: int = 800) -> dict[str, int]:
    keys = (
        "VETO_BB_SQUEEZE_ENTRADA",
        "VETO_SHORT_SQUEEZE",
        "VETO_DOUBLE_TOP",
        "VETO_DOUBLE_BOTTOM",
    )
    acc = {k: 0 for k in keys}
    for row in rows[-tail:]:
        a = str(row.get("acao_tomada") or "").upper()
        if a in acc:
            acc[a] += 1
    return acc


def _aggregate_price_action_flags(rows: list[dict[str, Any]], tail: int = 500) -> dict[str, int]:
    """Conta flags `auric_price_action` + BB squeeze no bloco TA dos logs."""
    acc = {
        "squeeze_real_block": 0,
        "bb_squeeze_tight_002": 0,
        "short_squeeze": 0,
        "double_top": 0,
        "double_bottom": 0,
    }
    pa_keys = ("squeeze_real_block", "short_squeeze", "double_top", "double_bottom")
    for row in rows[-tail:]:
        ta = _parse_ta_json_block(str(row.get("contexto_raw") or ""))
        if not ta:
            continue
        if bool(ta.get("bb_squeeze_tight_002")):
            acc["bb_squeeze_tight_002"] += 1
        pa = ta.get("auric_price_action")
        if not isinstance(pa, dict):
            continue
        for k in pa_keys:
            if pa.get(k):
                acc[k] += 1
    return acc


def _parse_contexto(contexto_raw: str | None) -> tuple[float | None, float | None, float | None]:
    if not contexto_raw:
        return None, None, None
    rsi = vol = spread = None
    try:
        obj = json.loads(contexto_raw)
        rsi = _safe_float(obj.get("rsi_14") or obj.get("rsi"))
        vol = _safe_float(obj.get("volatility") or obj.get("atr"))
        spread = _safe_float(obj.get("spread"))
        return rsi, vol, spread
    except Exception:
        pass
    m_rsi = re.search(r"RSI(?:\(14\))?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", contexto_raw, re.IGNORECASE)
    m_spread = re.search(r"Spread\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", contexto_raw, re.IGNORECASE)
    m_vol = re.search(r"(Volatilidade|Volatility)\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", contexto_raw, re.IGNORECASE)
    if m_rsi:
        rsi = _safe_float(m_rsi.group(1))
    if m_spread:
        spread = _safe_float(m_spread.group(1))
    if m_vol:
        vol = _safe_float(m_vol.group(2))
    return rsi, vol, spread


def _fetch_recent_logs(limit: int = 1500) -> list[dict[str, Any]]:
    res = (
        logger.supabase.table("logs")
        .select("id, created_at, par_moeda, preco_atual, acao_tomada, justificativa, contexto_raw")
        .order("id", desc=True)
        .limit(limit)
        .execute()
    )
    rows = res.data or []
    rows.sort(key=lambda r: int(r.get("id") or 0))
    return rows


def _infer_side_from_action(action: str) -> str:
    a = (action or "").upper()
    if "SHORT" in a:
        return "SHORT"
    return "LONG"


def _build_feedback(rows: list[dict[str, Any]]) -> list[TradeFeedback]:
    feedbacks: list[TradeFeedback] = []
    for i, row in enumerate(rows):
        action = str(row.get("acao_tomada") or "").upper()
        if action not in EXIT_ACTIONS:
            continue
        side = _infer_side_from_action(action)
        exit_price = _safe_float(row.get("preco_atual")) or 0.0
        if exit_price <= 0:
            continue
        entry_idx = -1
        for j in range(i - 1, -1, -1):
            a_prev = str(rows[j].get("acao_tomada") or "").upper()
            if a_prev in ENTRY_ACTIONS and _infer_side_from_action(a_prev) == side:
                entry_idx = j
                break
        if entry_idx < 0:
            continue
        entry_price = _safe_float(rows[entry_idx].get("preco_atual")) or 0.0
        if entry_price <= 0:
            continue

        window_prices: list[float] = []
        for k in range(entry_idx, i + 1):
            p = _safe_float(rows[k].get("preco_atual"))
            if p is not None and p > 0:
                window_prices.append(p)
        if not window_prices:
            continue

        if side == "LONG":
            potential = (max(window_prices) - entry_price) / entry_price
            realized = (exit_price - entry_price) / entry_price
        else:
            potential = (entry_price - min(window_prices)) / entry_price
            realized = (entry_price - exit_price) / entry_price
        potential_pct = max(0.0, potential * 100.0)
        realized_pct = max(0.0, realized * 100.0)
        efficiency = 0.0 if potential_pct <= 0 else min(200.0, (realized_pct / potential_pct) * 100.0)

        rsi, vol, spread = _parse_contexto(row.get("contexto_raw"))
        exit_meta = _parse_exit_meta(row.get("contexto_raw"))
        feedbacks.append(
            TradeFeedback(
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                potential_roi_pct=potential_pct,
                realized_roi_pct=realized_pct,
                efficiency_pct=efficiency,
                exit_action=action,
                rsi=rsi,
                volatility=vol,
                spread=spread,
                exit_meta=exit_meta,
            )
        )
    return feedbacks[-20:]


def _fetch_current_trailing() -> float:
    res = (
        logger.supabase.table("bot_config")
        .select("trailing_callback_rate, trailing_rate")
        .eq("id", 1)
        .single()
        .execute()
    )
    row = res.data or {}
    return float(row.get("trailing_callback_rate") or row.get("trailing_rate") or 0.6)


def _count_recent_stop_streak(feedbacks: list[TradeFeedback]) -> int:
    streak = 0
    for f in reversed(feedbacks):
        if "STOP" in f.exit_action:
            streak += 1
        else:
            break
    return streak


def _compute_next_trailing(current: float, feedbacks: list[TradeFeedback]) -> tuple[float, str]:
    if not feedbacks:
        return current, "Sem trades suficientes para ajuste."
    avg_eff = sum(f.efficiency_pct for f in feedbacks) / len(feedbacks)
    stop_streak = _count_recent_stop_streak(feedbacks)
    n_partial = sum(1 for f in feedbacks if f.exit_meta.get("partial_tp_50"))
    n_stop_sl = sum(1 for f in feedbacks if f.exit_meta.get("stop_hit"))
    if stop_streak >= 2:
        nxt = max(0.2, current - 0.1)
        return (
            nxt,
            f"Stop-loss consecutivo={stop_streak}; apertando trailing. "
            f"(meta: partial_tp={n_partial}, sl_exits={n_stop_sl})",
        )
    if n_partial >= 2 and avg_eff < 50.0:
        nxt = min(1.2, current + 0.05)
        return (
            nxt,
            f"Muitas saídas após TP parcial ({n_partial}) com eficiência média baixa "
            f"({avg_eff:.1f}%): trailing inicial ligeiramente mais largo.",
        )
    if avg_eff < 55.0:
        nxt = min(1.2, current + 0.1)
        return (
            nxt,
            f"Exit Efficiency média baixa ({avg_eff:.1f}%). Dando mais espaço ao mercado. "
            f"(meta: partial_tp={n_partial}, sl_exits={n_stop_sl})",
        )
    return (
        current,
        f"Exit Efficiency média saudável ({avg_eff:.1f}%). Mantendo trailing. "
        f"(meta: partial_tp={n_partial}, sl_exits={n_stop_sl})",
    )


def _compute_next_trailing_with_pa_context(
    current: float,
    feedbacks: list[TradeFeedback],
    rows: list[dict[str, Any]],
) -> tuple[float, str]:
    """
    Igual a `_compute_next_trailing`, mas se muitos vetos por compressão BB (entrada)
    nos logs, abre ligeiramente o trailing (mercado comprimido → saídas prematuras).
    """
    nxt, reason = _compute_next_trailing(current, feedbacks)
    pa_v = _count_price_action_vetoes(rows)
    sq_bb = int(pa_v.get("VETO_BB_SQUEEZE_ENTRADA") or 0)
    if sq_bb >= 8 and nxt <= current + 1e-9:
        nxt2 = min(1.2, current + 0.03)
        return (
            nxt2,
            f"{reason} | Price Action: {sq_bb}×VETO_BB_SQUEEZE_ENTRADA nos logs recentes — "
            f"+0,03% trailing para respirar em compressões BB.",
        )
    return nxt, reason


def _push_bot_config(new_trailing: float) -> None:
    payload = {
        "id": 1,
        "trailing_callback_rate": float(new_trailing),
        "trailing_rate": float(new_trailing),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.supabase.table("bot_config").upsert(payload, on_conflict="id").execute()


def run_optimizer(*, dry_run: bool = False) -> None:
    if logger.supabase is None:
        raise RuntimeError("Supabase não configurado (SUPABASE_URL/SUPABASE_KEY).")

    rows = _fetch_recent_logs(limit=1500)
    feedbacks = _build_feedback(rows)
    current = _fetch_current_trailing()
    new_trailing, reason = _compute_next_trailing_with_pa_context(current, feedbacks, rows)

    # Correlação simples de contexto de saída
    ctx_count = len([f for f in feedbacks if f.rsi is not None or f.volatility is not None or f.spread is not None])
    if not dry_run and abs(new_trailing - current) > 1e-9:
        _push_bot_config(new_trailing)

    meta_partial = sum(1 for f in feedbacks if f.exit_meta.get("partial_tp_50"))
    meta_sl = sum(1 for f in feedbacks if f.exit_meta.get("stop_hit"))
    print(
        f"🧠 [BRAIN] Ajustando Trailing para {new_trailing:.3f} com base nas últimas trades. "
        f"(atual={current:.3f}, n={len(feedbacks)}, contexto_saida={ctx_count}; "
        f"saídas c/ TP parcial={meta_partial}, c/ SL={meta_sl})"
    )
    print(f"🧠 [BRAIN] Motivo: {reason}")
    pa_v = _count_price_action_vetoes(rows)
    pa_fl = _aggregate_price_action_flags(rows)
    print(f"🧠 [BRAIN] Price Action: vetos_recentes={pa_v} | ocorrências_em_TA_JSON={pa_fl}")
    if dry_run:
        print("🧪 [BRAIN] Dry-run ativo: sem update no bot_config.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Otimiza trailing_rate com feedback das últimas trades.")
    parser.add_argument("--dry-run", action="store_true", help="Calcula e reporta sem atualizar bot_config.")
    args = parser.parse_args()
    run_optimizer(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
