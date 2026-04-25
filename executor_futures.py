"""
executor_futures — Binance Futures linear (USDC), MAINNET via ccxt.

Margem isolada e alavancagem configuráveis. Logs em português com tag [MAINNET FUTURES].
"""

from __future__ import annotations

import os
import sys
import time
import math
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import ccxt
from dotenv import load_dotenv
try:
    import redis
except Exception:  # noqa: BLE001
    redis = None  # type: ignore[assignment]

load_dotenv()

_TAG = "[MAINNET FUTURES]"
QUOTE_ASSET = "USDC"
_SYMBOL_ENV = str(os.getenv("SYMBOL", "ETHUSDC")).strip().upper()
MARKET_SYMBOL = "ETH/USDC:USDC" if _SYMBOL_ENV in ("ETHUSDC", "ETH/USDC", "ETH/USDC:USDC") else _SYMBOL_ENV

# Gestão de risco (frações): gatilho de trailing/SL em relação ao preço de entrada.
LONG_TP = 0.020
LONG_SL = 0.008
SHORT_TP = 0.015
SHORT_SL = 0.008
TRAILING_CALLBACK_RATE = 0.6  # % de recuo da máxima/mínima após ativação (sweet spot ETH/USDC)
TRAILING_ACTIVATION_MULTIPLIER = 1.0  # 1.0 = mantém gatilho igual ao antigo TP
# SL inicial na bolsa = N × (trailing em % expresso como fração de preço). Com trailing 0,6% e N=1,0 → SL ~0,6%.
SL_DISTANCE_VS_TRAILING_MULT = float(os.getenv("AURIC_SL_VS_TRAILING_MULT", "1.0"))

# Alavancagem usada só no log de liquidação aproximada (ajuste em configurar_alavancagem / main).
ALAVANCAGEM_REF_LOG_PADRAO = 3

# Position sizing: notional USDC = saldo_quote × PERCENTUAL_BANCA × alavancagem; qty = notional / preço.
# Fallback quando `risk_fraction` não é passado (alinhado a main.RISK_FRACTION_PADRAO em sessão agressiva).
PERCENTUAL_BANCA = 0.20

# LIMIT + offset vs. livro: compra ref. bid (+0,05%); venda ref. ask (−0,05%).
# Entradas: timeInForce IOC (sem GTC pendurado — liberta margem). Fechos reduce-only: GTC + chase.
PRECO_ABERTURA_LIMITE_OFFSET = 0.0005  # 0,05%
# Nível 3: livro vazio após REST — bid/ask sintéticos em torno de mark/last (USDC).
SYNTHETIC_BOOK_HALF_SPREAD = 0.05

# Trailing stop: após lucro ≥ este valor, SL vai a break-even e depois segue o preço a TRAILING_SL_DIST_FRAC.
TRAILING_LUCRO_ATIVACAO_FRAC = 0.01  # 1%
TRAILING_SL_DIST_FRAC = 0.008  # 0,8% atrás (LONG) / à frente (SHORT) do preço atual

# --- Risco dinâmico por ATR (Wilder) — SL/ativação/callback derivados da volatilidade ---
RISK_ATR_MODE = str(os.getenv("AURIC_RISK_ATR_MODE", "1")).strip().lower() in ("1", "true", "yes")
AURIC_ATR_TIMEFRAME = os.getenv("AURIC_ATR_TIMEFRAME", "15m").strip() or "15m"
AURIC_ATR_PERIOD = max(2, int(os.getenv("AURIC_ATR_PERIOD", "14")))
RISK_ATR_SL_MULT = float(os.getenv("AURIC_ATR_SL_MULT", "1.5"))
RISK_ATR_TP_REF_MULT = float(os.getenv("AURIC_ATR_TP_REF_MULT", "2.5"))
RISK_ATR_TRAIL_ACTIV_MULT = float(os.getenv("AURIC_ATR_TRAIL_ACTIV_MULT", "1.0"))
RISK_ATR_TRAIL_CALLBACK_FRAC = float(os.getenv("AURIC_ATR_TRAIL_CALLBACK_FRAC", "1.0"))
# Free runner (scale-out): fração da posição em TAKE_PROFIT_MARKET a 2,5×ATR; resto = runner (SL+trailing).
AURIC_PARTIAL_TP_PCT = max(0.01, min(0.99, float(os.getenv("AURIC_PARTIAL_TP_PCT", "0.50"))))
FREE_RUNNER_ATR_ENABLED = str(os.getenv("AURIC_FREE_RUNNER", "1")).strip().lower() in ("1", "true", "yes")

# Estado Modo Vigia — Free Runner (qty inicial após abertura; break-even após TP parcial).
_free_runner_anchor_qty: dict[str, float] = {}
_free_runner_preco_entrada: dict[str, float] = {}
_free_runner_be_aplicado: dict[str, bool] = {}

# Abertura LIMIT IOC + chase: após timeout sem fill suficiente, cancela (se ainda aberta) e reabre
# no novo bid/ask até N rondas. LONG: defaults abaixo. SHORT: mercado em queda rápida —
# timeouts mais curtos / mais rondas. Overrides via env.
CHASE_ENTRADA_TIMEOUT_S = float(os.getenv("AURIC_CHASE_TIMEOUT_S", "15"))
CHASE_ENTRADA_MAX_ROUNDS = int(os.getenv("AURIC_CHASE_MAX_ROUNDS", "3"))
# Chase agressivo só para abertura SHORT (ETH a «despencar» — ordem não fica pendurada acima do mercado).
CHASE_SHORT_TIMEOUT_S = float(os.getenv("AURIC_CHASE_SHORT_TIMEOUT_S", "8"))
CHASE_SHORT_MAX_ROUNDS = int(os.getenv("AURIC_CHASE_SHORT_MAX_ROUNDS", "6"))
# Entrada com comando TURBO (main.py): chase mais curto + offset mais apertado.
TURBO_CHASE_TIMEOUT_S = float(os.getenv("AURIC_TURBO_CHASE_TIMEOUT_S", "6"))
TURBO_CHASE_MAX_ROUNDS = int(os.getenv("AURIC_TURBO_CHASE_MAX_ROUNDS", "6"))
TURBO_CHASE_OFFSET_FRAC = float(os.getenv("AURIC_TURBO_CHASE_OFFSET", "0.00025"))  # 0,025%
# Realização parcial: `market` (default) ou `ioc` (LIMIT IOC agressivo).
PARTIAL_TP_EXECUTION = os.getenv("AURIC_PARTIAL_TP_EXEC", "market").strip().lower()
# Alinhado ao `PARTIAL_TP_ROI_FRAC` do main (fracção → % no Supabase `trades.partial_roi`).
PARTIAL_TP_ROI_PCT_SUPABASE = float(os.getenv("AURIC_PARTIAL_TP_ROI_PCT", "0.6"))
# Trava anti-loop: mais ordens abertas que isto no símbolo → abortar criação (default 4).
# Hard cap: `fetch_open_orders` com mais ordens que isto → abortar criação (default: >5 = 6+ ordens).
AURIC_MAX_OPEN_ORDERS_GUARD = max(1, int(os.getenv("AURIC_MAX_OPEN_ORDERS_BEFORE_PROTECT", "5")))
# Só substituir SL na bolsa se o novo stop difere > esta fração do preço já armado (anti micro-loop).
SL_REPLACE_MIN_REL_DIFF = float(os.getenv("AURIC_SL_REPLACE_MIN_REL_DIFF", "0.001"))  # 0,1 %
# Após cada `create_order` de proteção: dar tempo à API refletir estado.
PROTECTION_ORDER_CREATE_THROTTLE_S = float(os.getenv("AURIC_PROTECTION_CREATE_THROTTLE_S", "5"))
# Após cancelar ordem do mesmo tipo (fetch_open_orders) antes de recriar.
PROTECTION_PRE_CREATE_CANCEL_SLEEP_S = float(os.getenv("AURIC_PROTECTION_PRE_CREATE_CANCEL_SLEEP_S", "5"))
# Loop `cancelar_livro_aberto_ate_zero_sync`: pausa entre rondas até `fetch_open_orders` = [].
PROTECTION_CANCEL_CONFIRM_LOOP_SLEEP_S = float(os.getenv("AURIC_PROTECTION_CANCEL_LOOP_SLEEP_S", "5"))
# Bloqueio total de spam: mínimo de segundos entre criações de ordens de proteção (SL/TP cond./trailing).
PROTECTION_MIN_INTERVAL_BETWEEN_CREATES_S = float(os.getenv("AURIC_PROTECTION_MIN_INTERVAL_S", "10"))
# Último `time.monotonic()` em que uma ordem de proteção foi criada com sucesso (0 = ainda nenhuma).
_last_order_time: float = 0.0
# True entre «livro confirmado vazio» e o fim de `_criar_bracket_*` (permite TP+trailing+SL em sequência).
_in_protection_order_batch: bool = False

# Lock atómico de entrada (anti-spam entre loops/processos): SET key value NX EX <ttl>.
ORDER_LOCK_TTL_S = max(1.0, float(os.getenv("AURIC_ORDER_LOCK_TTL_S", "5")))
ORDER_LOCK_KEY_PREFIX = (os.getenv("AURIC_ORDER_LOCK_PREFIX") or "lock").strip() or "lock"
REDIS_URL = (os.getenv("REDIS_URL") or "redis://localhost:6379/0").strip()
REDIS_SOCKET_TIMEOUT_S = max(0.2, float(os.getenv("REDIS_SOCKET_TIMEOUT_S", "0.5")))
_redis_client: Any | None = None


def _order_lock_key(simbolo: str) -> str:
    clean = "".join(ch for ch in str(simbolo or "").upper() if ch.isalnum())
    return f"{ORDER_LOCK_KEY_PREFIX}_{clean or 'SYMBOL'}"


def _obter_redis_client() -> Any | None:
    global _redis_client
    if redis is None:
        return None
    if _redis_client is not None:
        return _redis_client
    try:
        _redis_client = redis.Redis.from_url(  # type: ignore[attr-defined]
            REDIS_URL,
            socket_timeout=REDIS_SOCKET_TIMEOUT_S,
            socket_connect_timeout=REDIS_SOCKET_TIMEOUT_S,
            decode_responses=True,
        )
        _redis_client.ping()
        return _redis_client
    except Exception as e:  # noqa: BLE001
        print(f"{_TAG} [REDIS] indisponível em {REDIS_URL}: {e}. Lock atómico desativado.", flush=True)
        _redis_client = None
        return None


def _adquirir_lock_entrada(simbolo: str) -> tuple[str, str] | None:
    cli = _obter_redis_client()
    if cli is None:
        return None
    key = _order_lock_key(simbolo)
    token = f"{os.getpid()}-{time.time_ns()}"
    ttl_int = int(math.ceil(float(ORDER_LOCK_TTL_S)))
    try:
        ok = bool(cli.set(key, token, nx=True, ex=ttl_int))
        if ok:
            return key, token
        return ("", "")
    except Exception as e:  # noqa: BLE001
        print(f"{_TAG} [REDIS] falha ao adquirir lock {key}: {e}. Prosseguindo sem lock.", flush=True)
        return None


def _liberar_lock_entrada(lock_ctx: tuple[str, str] | None) -> None:
    if not lock_ctx:
        return
    key, token = lock_ctx
    if not key:
        return
    cli = _obter_redis_client()
    if cli is None:
        return
    # Release seguro: remove só se o token atual for nosso (evita apagar lock novo de outro loop).
    lua = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
else
  return 0
end
"""
    try:
        cli.eval(lua, 1, key, token)
    except Exception:
        pass


def _ordem_skip_lock(simbolo: str) -> dict[str, Any]:
    return {
        "id": None,
        "status": "skipped",
        "symbol": str(simbolo),
        "auric_skipped": True,
        "auric_skip_reason": "REDIS_LOCK_ACTIVE",
    }

# ORDER-GUARD (vigia): verificar SL + TP na bolsa no máximo 1×/30s por ciclo agregado.
ORDER_GUARD_INTERVAL_S = 30.0
_order_guard_last_monotonic: float = 0.0
_order_guard_cooldown_until_monotonic: float = 0.0

# Anti-spam: `assegurar_brackets_apos_reconciliacao` com a mesma assinatura (callback/mult/BE).
BRACKET_REPLACE_DEBOUNCE_S = 25.0
# Só retunar brackets «discrecionários» se o mark se mover ≥ isto a favor desde o último sucesso.
BRACKET_REPLACE_MIN_FAVORABLE_PRICE_MOVE = float(
    os.getenv("AURIC_BRACKET_MIN_FAVORABLE_MOVE", "0.001")
)
_bracket_last_mono_by_sym: dict[str, float] = {}
_bracket_last_sig_by_sym: dict[str, tuple[float, float, bool]] = {}
_bracket_last_anchor_mark_by_sym: dict[str, float] = {}
# Último preço de referência onde `gerenciar_trailing_stop` mexeu no SL (anti-spam 0,1% a favor).
_trailing_sl_adjust_anchor_by_sym: dict[str, float] = {}

# Tipos condicionais de fecho (reduce-only) na Binance Futures — cancelar antes de recolocar.
_FUTURES_PROTECTIVE_ORDER_TYPES = frozenset(
    {
        "STOP_MARKET",
        "TAKE_PROFIT_MARKET",
        "TRAILING_STOP_MARKET",
        "STOP",  # STOP condicional (limit stop)
        "TAKE_PROFIT",
    }
)
# Só SL + trailing — nunca TAKE_PROFIT_* nem LIMIT (TP parcial / reduce-only limit).
_FUTURES_SL_TRAILING_CANCEL_TYPES = frozenset(
    {
        "STOP_MARKET",
        "TRAILING_STOP_MARKET",
        "STOP",
        "TRAILING_STOP",
    }
)
_FUTURES_STOP_MARKET_ONLY_CANCEL_TYPES = frozenset({"STOP_MARKET", "STOP"})


def reset_protective_order_guard_throttle() -> None:
    """Repor o throttle do ORDER-GUARD (ex.: ao fechar posição / reset vigia)."""
    global _order_guard_last_monotonic, _order_guard_cooldown_until_monotonic
    global _bracket_last_mono_by_sym, _bracket_last_sig_by_sym, _bracket_last_anchor_mark_by_sym
    global _last_order_time, _in_protection_order_batch
    _order_guard_last_monotonic = 0.0
    _order_guard_cooldown_until_monotonic = 0.0
    _last_order_time = 0.0
    _in_protection_order_batch = False
    _bracket_last_mono_by_sym.clear()
    _bracket_last_sig_by_sym.clear()
    _bracket_last_anchor_mark_by_sym.clear()
    _trailing_sl_adjust_anchor_by_sym.clear()
    reset_free_runner_state()


def reset_free_runner_state(simbolo: str | None = None, exchange: ccxt.binance | None = None) -> None:
    """Limpa tracking do Free Runner (`simbolo is None` → todas as chaves; caso contrário só o par)."""
    global _free_runner_anchor_qty, _free_runner_preco_entrada, _free_runner_be_aplicado
    if simbolo is None:
        _free_runner_anchor_qty.clear()
        _free_runner_preco_entrada.clear()
        _free_runner_be_aplicado.clear()
        return
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    _free_runner_anchor_qty.pop(sym, None)
    _free_runner_preco_entrada.pop(sym, None)
    _free_runner_be_aplicado.pop(sym, None)


def armar_free_runner_tracking(
    simbolo: str,
    exchange: ccxt.binance,
    qty_inicial: float,
    preco_entrada: float,
) -> None:
    """Chamar após abrir posição com TP parcial Free Runner armado (para detetar fill na vigia)."""
    sym = _resolver_simbolo_perp(exchange, simbolo)
    _free_runner_anchor_qty[sym] = abs(float(qty_inicial))
    _free_runner_preco_entrada[sym] = float(preco_entrada)
    _free_runner_be_aplicado[sym] = False


def free_runner_tracking_ativo(simbolo: str, exchange: ccxt.binance | None = None) -> bool:
    """True enquanto há sessão Free Runner (evita parcial ROI duplicada no main)."""
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    return sym in _free_runner_anchor_qty


def _free_runner_split_qty(
    exchange: ccxt.binance,
    simbolo: str,
    qty_total: float,
    partial_pct: float,
) -> tuple[float, float, bool]:
    """
    Divide qty em (parcial TP, runner). Quantidades com `amount_to_precision` da Binance.
    Devolve (q_tp, q_runner, ok). ok=False se min amount ou arredondamento inviabilizam o split.
    """
    q_tot = float(exchange.amount_to_precision(simbolo, float(qty_total)))
    if q_tot <= 0:
        return 0.0, 0.0, False
    mkt = exchange.market(simbolo)
    min_amt = float(((mkt.get("limits") or {}).get("amount") or {}).get("min") or 0.0)
    raw_tp = q_tot * float(partial_pct)
    q_tp = float(exchange.amount_to_precision(simbolo, raw_tp))
    q_runner = float(exchange.amount_to_precision(simbolo, q_tot - q_tp))
    if q_tp <= 0 or q_runner <= 0:
        return q_tot, 0.0, False
    if min_amt > 0:
        if q_tp + 1e-12 < min_amt or q_runner + 1e-12 < min_amt:
            return q_tot, 0.0, False
    if q_tp + q_runner > q_tot + 1e-9:
        q_runner = float(exchange.amount_to_precision(simbolo, max(0.0, q_tot - q_tp)))
    if q_runner <= 0:
        return q_tot, 0.0, False
    return q_tp, q_runner, True


def verificar_free_runner_breakeven_posicao(
    simbolo: str,
    exchange: ccxt.binance,
    direcao: str,
    preco_entrada_ram: float,
    *,
    trailing_callback_rate: float,
    trailing_activation_multiplier: float,
) -> bool:
    """
    Se a posição encolheu ~para o runner (TP parcial 50% executado), cancela SL/trailing antigos
    e recoloca brackets em break-even + trailing só sobre o restante.
    Devolve True se aplicou o passo (o caller pode fixar `_partial_tp_locked` no main).
    """
    sym = _resolver_simbolo_perp(exchange, simbolo)
    if sym not in _free_runner_anchor_qty:
        return False
    if _free_runner_be_aplicado.get(sym):
        return False
    anchor = float(_free_runner_anchor_qty.get(sym) or 0.0)
    if anchor <= 0:
        return False
    snap = consultar_posicao_futures(simbolo, exchange)
    if not snap.get("posicao_aberta"):
        reset_free_runner_state(simbolo, exchange)
        return False
    q_now = abs(float(snap.get("contratos") or 0.0))
    if q_now <= 0:
        reset_free_runner_state(simbolo, exchange)
        return False
    target_rem = anchor * (1.0 - float(AURIC_PARTIAL_TP_PCT))
    tol_hi = target_rem * 1.18
    tol_lo = target_rem * 0.72
    if not (tol_lo <= q_now <= tol_hi):
        return False
    pe = float(_free_runner_preco_entrada.get(sym) or preco_entrada_ram)
    if pe <= 0:
        pe = float(preco_entrada_ram)
    d = str(direcao).strip().upper()
    if d not in ("LONG", "SHORT"):
        return False
    print(
        f"🛡️ [BREAKEVEN] Parcial atingida! Lucro no bolso. Stop Loss movido para o preço de entrada "
        f"({pe:.6f}). Risco zero.",
        flush=True,
    )
    try:
        assegurar_brackets_apos_reconciliacao(
            simbolo,
            exchange,
            d,
            q_now,
            pe,
            trailing_callback_rate=float(trailing_callback_rate),
            trailing_activation_multiplier=float(trailing_activation_multiplier),
            source_tag="FREE_RUNNER_BE",
            sl_break_even=True,
            force_replace=True,
        )
    except Exception as e_be:  # noqa: BLE001
        print(f"{_TAG} [FREE RUNNER] Falha ao recolocar break-even: {e_be}", file=sys.stderr)
        return False
    _free_runner_be_aplicado[sym] = True
    try:
        import logger

        logger.registrar_log_trade(
            par_moeda=simbolo,
            preco=float(preco_entrada_ram),
            prob_ml=0.0,
            sentimento="—",
            acao="FREE_RUNNER_BE",
            justificativa=(
                f"[FREE RUNNER] TP parcial ~{AURIC_PARTIAL_TP_PCT:.0%} executado; qty {anchor:g}→{q_now:g}; "
                f"SL reposicionado em break-even @ {pe:.6f}."
            ),
            lado_ordem=d,
        )
    except Exception as e_log:  # noqa: BLE001
        print(f"{_TAG} [FREE RUNNER] Log Supabase: {e_log}", file=sys.stderr)
    return True


def _order_reduce_only_flag(o: dict[str, Any]) -> bool:
    if bool(o.get("reduceOnly")):
        return True
    info = o.get("info") or {}
    v = str(info.get("reduceOnly", "")).lower()
    if v in ("true", "1", "yes"):
        return True
    # Binance Futures: fechos «close entire position» vêm por vezes só com closePosition.
    if str(info.get("closePosition", "")).lower() in ("true", "1", "yes"):
        return True
    return False


def _order_type_norm(o: dict[str, Any]) -> str:
    t = str(o.get("type") or o.get("origType") or "").upper().replace("-", "_")
    if t:
        return t
    info = o.get("info") or {}
    raw = info.get("type") or info.get("origType") or info.get("orderType") or ""
    return str(raw).upper().replace("-", "_")


def _fetch_open_orders_ccxt_list(exchange: ccxt.binance, simbolo_ccxt: str) -> list[dict[str, Any]]:
    try:
        rows = exchange.fetch_open_orders(simbolo_ccxt)
        return rows if isinstance(rows, list) else []
    except ccxt.BaseError as e:
        print(f"{_TAG} fetch_open_orders({simbolo_ccxt}): {e}", file=sys.stderr)
        return []


def _futures_one_way_position_side(o: dict[str, Any]) -> bool:
    """True se parecer modo one-way (BOTH) — sem hedge LONG/SHORT separado."""
    info = o.get("info") or {}
    ps = str(info.get("positionSide") or "").upper()
    return ps in ("", "BOTH")


def _ordem_sl_ou_trailing_para_cancelar(o: dict[str, Any], direcao: str) -> bool:
    """STOP_MARKET / TRAILING_* no lado de fecho; preserva TP (TAKE_PROFIT_*) e LIMIT."""
    d = str(direcao).strip().upper()
    if d not in ("LONG", "SHORT"):
        return False
    need_side = "sell" if d == "LONG" else "buy"
    if str(o.get("side") or "").lower() != need_side:
        return False
    typ = _order_type_norm(o)
    if typ in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
        return False
    if typ == "LIMIT":
        return False
    if typ not in _FUTURES_SL_TRAILING_CANCEL_TYPES:
        return False
    if _order_reduce_only_flag(o):
        return True
    # fetch_open_orders por vezes omite reduceOnly em TRAILING_STOP_MARKET / STOP_MARKET;
    # em one-way, estas ordens no lado de fecho são sempre proteção — cancelar para evitar spam.
    return _futures_one_way_position_side(o)


def _ordem_stop_market_protecao_para_cancelar(o: dict[str, Any], direcao: str) -> bool:
    """Só STOP_MARKET / STOP (stop-limit); não cancela trailing nem TP."""
    d = str(direcao).strip().upper()
    if d not in ("LONG", "SHORT"):
        return False
    need_side = "sell" if d == "LONG" else "buy"
    if str(o.get("side") or "").lower() != need_side:
        return False
    typ = _order_type_norm(o)
    if typ not in _FUTURES_STOP_MARKET_ONLY_CANCEL_TYPES:
        return False
    if _order_reduce_only_flag(o):
        return True
    return _futures_one_way_position_side(o)


def cancelar_sl_trailing_reduce_only_ccxt(
    exchange: ccxt.binance,
    simbolo: str,
    direcao: str,
    *,
    max_passes: int = 10,
    sleep_s: float = 5.0,
) -> int:
    """
    Cancela via CCXT (`fetch_open_orders` + `cancel_order`) todas as STOP_MARKET e
    TRAILING_STOP_MARKET reduce-only do lado de fecho. Não cancela TAKE_PROFIT_MARKET
    nem ordens LIMIT (TP parcial / exits limit).
    """
    sym = _resolver_simbolo_perp(exchange, simbolo)
    d = str(direcao).strip().upper()
    total = 0
    for _ in range(max(1, int(max_passes))):
        rows = _fetch_open_orders_ccxt_list(exchange, sym)
        alvo = [o for o in rows if _ordem_sl_ou_trailing_para_cancelar(o, d)]
        if not alvo:
            return total
        for o in alvo:
            oid = o.get("id")
            if oid is None:
                continue
            try:
                exchange.cancel_order(str(oid), sym)
                total += 1
            except ccxt.BaseError as ec:
                print(f"{_TAG} cancel SL/trail id={oid}: {ec}", file=sys.stderr)
        time.sleep(float(sleep_s))
    rem = [o for o in _fetch_open_orders_ccxt_list(exchange, sym) if _ordem_sl_ou_trailing_para_cancelar(o, d)]
    if rem:
        print(
            f"{_TAG} [RISCO] Após {max_passes} passes persistem {len(rem)} ordem(ns) "
            f"STOP/TRAILING reduce-only em {sym}.",
            file=sys.stderr,
            flush=True,
        )
    return total


def protecao_sl_trailing_limpa_ccxt(exchange: ccxt.binance, simbolo: str, direcao: str) -> bool:
    sym = _resolver_simbolo_perp(exchange, simbolo)
    d = str(direcao).strip().upper()
    rows = _fetch_open_orders_ccxt_list(exchange, sym)
    return not any(_ordem_sl_ou_trailing_para_cancelar(o, d) for o in rows)


def cancelar_stop_market_protecao_ccxt(
    exchange: ccxt.binance,
    simbolo: str,
    direcao: str,
    *,
    max_passes: int = 10,
    sleep_s: float = 5.0,
) -> int:
    """Só STOP_MARKET / STOP reduce-only no lado de fecho (atualização de SL sem remover trailing)."""
    sym = _resolver_simbolo_perp(exchange, simbolo)
    d = str(direcao).strip().upper()
    total = 0
    for _ in range(max(1, int(max_passes))):
        rows = _fetch_open_orders_ccxt_list(exchange, sym)
        alvo = [o for o in rows if _ordem_stop_market_protecao_para_cancelar(o, d)]
        if not alvo:
            return total
        for o in alvo:
            oid = o.get("id")
            if oid is None:
                continue
            try:
                exchange.cancel_order(str(oid), sym)
                total += 1
            except ccxt.BaseError as ec:
                print(f"{_TAG} cancel STOP id={oid}: {ec}", file=sys.stderr)
        time.sleep(float(sleep_s))
    return total


def protecao_stop_market_limpa_ccxt(exchange: ccxt.binance, simbolo: str, direcao: str) -> bool:
    sym = _resolver_simbolo_perp(exchange, simbolo)
    d = str(direcao).strip().upper()
    rows = _fetch_open_orders_ccxt_list(exchange, sym)
    return not any(_ordem_stop_market_protecao_para_cancelar(o, d) for o in rows)


def cancelar_livro_aberto_ate_zero_sync(
    exchange: ccxt.binance,
    simbolo_any: str,
    *,
    context: str,
    max_rounds: int = 48,
) -> None:
    """
    Cancela **todas** as ordens abertas do símbolo até `fetch_open_orders` devolver lista vazia.
    Entre rondas: `PROTECTION_CANCEL_CONFIRM_LOOP_SLEEP_S` (default 5s) para a API acompanhar.
    """
    sym = _resolver_simbolo_perp(exchange, simbolo_any)
    slp = float(PROTECTION_CANCEL_CONFIRM_LOOP_SLEEP_S)
    nmax = max(1, int(max_rounds))
    rnd = 0
    while True:
        rnd += 1
        if rnd > nmax:
            break
        rows = _fetch_open_orders_ccxt_list(exchange, sym)
        if not rows:
            return
        for o in rows:
            oid = o.get("id")
            if oid is None:
                continue
            try:
                exchange.cancel_order(str(oid), sym)
            except ccxt.BaseError as ec:
                print(
                    f"{_TAG} cancelar_livro→0 [{context}] id={oid}: {ec}",
                    file=sys.stderr,
                    flush=True,
                )
        time.sleep(slp)
    rows = _fetch_open_orders_ccxt_list(exchange, sym)
    if rows:
        msg = (
            f"{_TAG} [CRÍTICO] {context} | {sym}: após {nmax} rondas persistem {len(rows)} "
            "ordem(ns) abertas (esperado zero)."
        )
        print(msg, file=sys.stderr, flush=True)
        raise RuntimeError(msg)


def _marcar_criacao_ordem_protecao() -> None:
    global _last_order_time
    _last_order_time = time.monotonic()


def _exigir_intervalo_min_entre_ordens_protecao(*, context: str) -> bool:
    """Bloqueio total de spam: False se a última ordem de proteção foi criada há < N segundos."""
    global _last_order_time
    if _in_protection_order_batch:
        # Dentro do mesmo batch (SL/TP/TS em sequência) não aplicar bloqueio global.
        return True
    if _last_order_time <= 0:
        return True
    elapsed = time.monotonic() - _last_order_time
    need = float(PROTECTION_MIN_INTERVAL_BETWEEN_CREATES_S)
    if elapsed < need:
        print(
            f"{_TAG} [ANTI-SPAM] Criação bloqueada: última ordem de proteção há {elapsed:.1f}s "
            f"(mínimo {need:.0f}s). {context}",
            file=sys.stderr,
            flush=True,
        )
        return False
    return True


def limpar_sl_trailing_e_confirmar_sync(
    exchange: ccxt.binance,
    simbolo: str,
    direcao: str,
    *,
    context: str,
) -> None:
    """
    Limpeza **síncrona** de SL/trailing: cancela via CCXT e só conclui quando
    `protecao_sl_trailing_limpa_ccxt` confirmar livro sem esses tipos (senão RuntimeError).
    """
    d = str(direcao).strip().upper()
    sym = _resolver_simbolo_perp(exchange, simbolo)
    slp_confirm = float(PROTECTION_CANCEL_CONFIRM_LOOP_SLEEP_S)
    cancelar_sl_trailing_reduce_only_ccxt(
        exchange, simbolo, d, max_passes=16, sleep_s=slp_confirm
    )
    time.sleep(slp_confirm)
    if not protecao_sl_trailing_limpa_ccxt(exchange, simbolo, d):
        cancelar_sl_trailing_reduce_only_ccxt(
            exchange, simbolo, d, max_passes=20, sleep_s=slp_confirm
        )
        time.sleep(slp_confirm)
    if not protecao_sl_trailing_limpa_ccxt(exchange, simbolo, d):
        msg = (
            f"{_TAG} [CRÍTICO] {context} | {sym}: SL/TRAILING ainda presentes após limpeza síncrona."
        )
        print(msg, file=sys.stderr, flush=True)
        raise RuntimeError(msg)


def _contar_ordens_abertas_simbolo_ccxt(exchange: ccxt.binance, simbolo_any: str) -> int:
    sym = _resolver_simbolo_perp(exchange, simbolo_any)
    return len(_fetch_open_orders_ccxt_list(exchange, sym))


def _quantidade_ordem_aberta_ccxt(
    exchange: ccxt.binance,
    simbolo_ccxt: str,
    o: dict[str, Any],
) -> float:
    """Quantidade em aberto (prioriza `remaining` CCXT, fallback Binance `origQty`)."""
    raw_amt: float | None = None
    rem = o.get("remaining")
    if rem is not None and str(rem).strip() != "":
        try:
            r = float(rem)
            if r > 0:
                raw_amt = r
        except (TypeError, ValueError):
            pass
    if raw_amt is None:
        info = o.get("info") or {}
        for k in ("origQty", "quantity", "q", "positionAmt"):
            v = info.get(k)
            if v is not None and str(v).strip() != "":
                try:
                    raw_amt = float(v)
                    break
                except (TypeError, ValueError):
                    pass
    if raw_amt is None:
        v2 = o.get("amount")
        if v2 is not None and str(v2).strip() != "":
            try:
                raw_amt = float(v2)
            except (TypeError, ValueError):
                raw_amt = 0.0
    if raw_amt is None or raw_amt <= 0:
        return 0.0
    return float(exchange.amount_to_precision(simbolo_ccxt, float(raw_amt)))


def _buscar_take_profit_market_parcial_aberto(
    exchange: ccxt.binance,
    simbolo_ccxt: str,
    direcao: str,
) -> dict[str, Any] | None:
    """TP parcial Free Runner (TAKE_PROFIT_MARKET reduce-only, lado de fecho)."""
    d = str(direcao).strip().upper()
    if d not in ("LONG", "SHORT"):
        return None
    need_side = "sell" if d == "LONG" else "buy"
    for o in _fetch_open_orders_ccxt_list(exchange, simbolo_ccxt):
        if not _order_reduce_only_flag(o):
            continue
        if _order_type_norm(o) != "TAKE_PROFIT_MARKET":
            continue
        if str(o.get("side") or "").lower() != need_side:
            continue
        return o
    return None


def _buscar_take_profit_parcial_aberto_any(
    exchange: ccxt.binance,
    simbolo_ccxt: str,
    direcao: str,
) -> dict[str, Any] | None:
    """
    TP parcial Free Runner já armado:
    aceita TAKE_PROFIT_MARKET e TAKE_PROFIT (reduce-only, lado de fecho).
    """
    d = str(direcao).strip().upper()
    if d not in ("LONG", "SHORT"):
        return None
    need_side = "sell" if d == "LONG" else "buy"
    for o in _fetch_open_orders_ccxt_list(exchange, simbolo_ccxt):
        if not _order_reduce_only_flag(o):
            continue
        if _order_type_norm(o) not in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
            continue
        if str(o.get("side") or "").lower() != need_side:
            continue
        return o
    return None


def _obter_mark_price_futures(exchange: ccxt.binance, simbolo_ccxt: str) -> float:
    """Mark ou last do ticker (Futures); 0.0 se indisponível."""
    try:
        t = exchange.fetch_ticker(simbolo_ccxt)
        info = t.get("info") or {}
        for k in ("markPrice", "p"):
            raw = info.get(k)
            if raw is not None and str(raw).strip() != "":
                try:
                    v = float(raw)
                    if v > 0:
                        return v
                except (TypeError, ValueError):
                    pass
        mk = t.get("mark")
        if mk is not None:
            try:
                v = float(mk)
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
        for k in ("last", "close"):
            raw = t.get(k)
            if raw is not None:
                try:
                    v = float(raw)
                    if v > 0:
                        return v
                except (TypeError, ValueError):
                    pass
    except ccxt.BaseError:
        pass
    return 0.0


def cancelar_ordens_condicionais_protecao(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> int:
    """
    Cancela ordens condicionais reduce-only de protecção (SL/TP/trailing) no par.
    Usar **antes** de recriar brackets — complementa o nuke global (alguns estados da API
    podem atrasar ordens condicionais no livro).
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    n = 0
    for pass_i in range(2):
        try:
            rows = ex.fetch_open_orders(sym)
        except ccxt.BaseError as e:
            print(f"{_TAG} cancelar_ordens_condicionais_protecao fetch: {e}", file=sys.stderr)
            return n
        for o in rows:
            if not _order_reduce_only_flag(o):
                continue
            typ = _order_type_norm(o)
            if typ not in _FUTURES_PROTECTIVE_ORDER_TYPES:
                continue
            oid = o.get("id")
            if oid is None:
                continue
            try:
                ex.cancel_order(oid, sym)
                n += 1
            except ccxt.BaseError as ec:
                print(f"{_TAG} cancel condicional {typ} id={oid}: {ec}", file=sys.stderr)
        if pass_i == 0 and n:
            time.sleep(float(PROTECTION_PRE_CREATE_CANCEL_SLEEP_S))
    if n:
        print(
            f"{_TAG} cancelar_ordens_condicionais_protecao({sym}): {n} ordem(ns) "
            "STOP/TP/TRAILING reduce-only cancelada(s).",
            flush=True,
        )
    return n


def _protective_stop_and_tp_present(
    exchange: ccxt.binance,
    simbolo_ccxt: str,
    direcao: str,
) -> tuple[bool, bool, bool, list[str]]:
    """
    Ordens reduce-only de fecho: LONG → sell; SHORT → buy.
    SL = STOP_MARKET; TP (vigia Auric) = TRAILING_STOP_MARKET (ou TAKE_PROFIT_MARKET se existir).
    """
    d = str(direcao).strip().upper()
    if d not in ("LONG", "SHORT"):
        return False, False
    # Modo Vigia: ler ordens abertas pelo endpoint nativo futures_get_open_orders.
    rows = obter_ordens_abertas_futures_nativo(simbolo_ccxt, exchange)
    side_close = _lado_fecho_protecao(d)
    has_sl = False
    has_tp = False
    has_ts = False
    found_types: list[str] = []
    for o in rows:
        if str(o.get("side") or "").lower() != side_close:
            continue
        typ = _order_type_norm(o)
        if typ:
            found_types.append(typ)
        if typ == "STOP_MARKET":
            has_sl = True
        elif typ in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
            has_tp = True
        elif typ == "TRAILING_STOP_MARKET":
            has_ts = True
    # trailing conta como proteção de lucro para o vigia.
    return bool(has_sl), bool(has_tp or has_ts), bool(has_ts), found_types


def check_and_verify_protective_orders(
    simbolo: str,
    exchange: ccxt.binance,
    direcao: str,
    preco_entrada_ram: float,
    *,
    trailing_callback_rate: float,
    trailing_activation_multiplier: float,
    sl_break_even: bool = False,
) -> None:
    """
    Redundância em MODO VIGIA: confirma STOP_MARKET (SL) + ordem de TP (TRAILING_STOP_MARKET
    ou TAKE_PROFIT_MARKET) em aberto; se faltar alguma, recria o par via `assegurar_brackets_apos_reconciliacao`.
    """
    global _order_guard_last_monotonic, _order_guard_cooldown_until_monotonic
    now = time.monotonic()
    if now < float(_order_guard_cooldown_until_monotonic):
        rem = float(_order_guard_cooldown_until_monotonic) - now
        print(
            f"{_TAG} [SYNC] ORDER-GUARD em cooldown de sincronização ({rem:.1f}s restantes).",
            flush=True,
        )
        return
    if now - _order_guard_last_monotonic < float(ORDER_GUARD_INTERVAL_S):
        return
    _order_guard_last_monotonic = now

    sym = _resolver_simbolo_perp(exchange, simbolo)
    snap = consultar_posicao_futures(simbolo, exchange)
    if not snap.get("posicao_aberta"):
        return

    qty = abs(float(snap.get("contratos") or 0.0))
    if qty <= 0:
        return

    pe = float(preco_entrada_ram)
    if pe <= 0:
        ep = snap.get("entry_price")
        if ep is not None:
            try:
                pe = float(ep)
            except (TypeError, ValueError):
                pe = 0.0
    if pe <= 0:
        return

    d = str(direcao).strip().upper()
    if d not in ("LONG", "SHORT"):
        return
    _log_protection_counts(exchange, sym, d, context="ORDER_GUARD(pre)")
    rows_native = obter_ordens_abertas_futures_nativo(sym, exchange)
    found_types: list[str] = []
    has_sl = False
    has_tp = False
    for o in rows_native:
        typ = _order_type_norm(o)
        if typ:
            found_types.append(typ)
        if typ == "STOP_MARKET":
            has_sl = True
        elif typ in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
            has_tp = True
    print(
        f"{_TAG} [DEBUG VIGIA] Ordens ativas encontradas: {found_types} para {sym}",
        flush=True,
    )
    if has_sl and has_tp:
        print(
            f"{_TAG} [ORDER_GUARD] Proteções já no livro (STOP={has_sl}, TP={has_tp}) "
            "— sem recriar.",
            flush=True,
        )
        _log_protection_counts(exchange, sym, d, context="ORDER_GUARD(raw-open-orders)")
        return

    print(
        f"{_TAG} [ORDER-GUARD] Proteção incompleta (STOP={has_sl}, TP={has_tp}). "
        "A executar fluxo SYNC nuke-and-pave.",
        flush=True,
    )
    sid = _futures_symbol_id(exchange, sym)
    exchange.fapiPrivateDeleteAllOpenOrders({"symbol": sid})
    print(f"{_TAG} [SYNC] Limpeza total de ordens pendentes concluída.", flush=True)
    time.sleep(2.0)

    assegurar_brackets_apos_reconciliacao(
        simbolo,
        exchange,
        d,
        qty,
        pe,
        trailing_callback_rate=float(trailing_callback_rate),
        trailing_activation_multiplier=float(trailing_activation_multiplier),
        source_tag="ORDER_GUARD",
        sl_break_even=bool(sl_break_even),
        force_replace=True,
    )
    print(
        f"{_TAG} [SYNC] Proteções criadas, aguardando 5s para sincronização da Binance...",
        flush=True,
    )
    _order_guard_cooldown_until_monotonic = time.monotonic() + 5.0
    time.sleep(5.0)
    _log_protection_counts(exchange, sym, d, context="ORDER_GUARD(post)")


def _carregar_chaves() -> tuple[str, str]:
    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError(
            "Defina BINANCE_API_KEY e BINANCE_API_SECRET no .env (nomes iguais às variáveis)."
        )
    return api_key, api_secret


def criar_exchange_binance() -> ccxt.binance:
    """
    Cliente Binance em modo **Futures linear** (defaultType = future), MAINNET.
    """
    api_key, api_secret = _carregar_chaves()
    exchange = ccxt.binance(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "timeout": 30000,
            "options": {
                "defaultType": "future",
                "defaultSubType": "linear",
                "settlement": QUOTE_ASSET,
                "settle": QUOTE_ASSET,
            },
        }
    )
    exchange.set_sandbox_mode(False)
    return exchange


def _simbolo_para_rest(symbol: str) -> str:
    s = str(symbol or "").strip().upper().replace(":USDC", "").replace(":USDT", "")
    return s.replace("/", "")


def obter_funding_rate(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> float | None:
    """
    Funding rate atual (premiumIndex) para o perp do símbolo.
    Em erro (timeout/rate limit/rede/API), devolve None.
    """
    ex = exchange or criar_exchange_binance()
    sym_rest = _simbolo_para_rest(simbolo)
    try:
        # CCXT implicit endpoint Binance Futures: /fapi/v1/premiumIndex
        data = ex.fapiPublicGetPremiumIndex({"symbol": sym_rest})  # type: ignore[attr-defined]
        fr = data.get("lastFundingRate") if isinstance(data, dict) else None
        if fr is None:
            return None
        return float(fr)
    except Exception:
        # Fallback HTTP direto (mesmo endpoint), com timeout curto.
        try:
            url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym_rest}"
            with urllib.request.urlopen(url, timeout=3.0) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                data = json.loads(body) if body else {}
            fr = data.get("lastFundingRate") if isinstance(data, dict) else None
            if fr is None:
                return None
            return float(fr)
        except Exception as e_fr:  # noqa: BLE001
            print(f"{_TAG} ⚠️ funding_rate indisponível ({sym_rest}): {e_fr}")
            return None


def obter_long_short_ratio_global(
    simbolo: str,
    exchange: ccxt.binance | None = None,
    *,
    period: str = "5m",
) -> float | None:
    """
    Global Long/Short Account Ratio (Binance Futures Data API).
    Em erro (timeout/rate limit/rede/API), devolve None.
    """
    ex = exchange or criar_exchange_binance()
    sym_rest = _simbolo_para_rest(simbolo)
    params = {"symbol": sym_rest, "period": period, "limit": 1}
    try:
        # CCXT implicit endpoint Binance Futures Data:
        # /futures/data/globalLongShortAccountRatio
        rows = ex.fapiDataGetGlobalLongShortAccountRatio(params)  # type: ignore[attr-defined]
        if isinstance(rows, list) and rows:
            ratio = rows[-1].get("longShortRatio")
            if ratio is None:
                return None
            return float(ratio)
        return None
    except Exception:
        # Fallback HTTP direto
        try:
            qs = urllib.parse.urlencode(params)
            url = f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio?{qs}"
            with urllib.request.urlopen(url, timeout=3.0) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                rows = json.loads(body) if body else []
            if isinstance(rows, list) and rows:
                ratio = rows[-1].get("longShortRatio")
                if ratio is None:
                    return None
                return float(ratio)
            return None
        except Exception as e_lsr:  # noqa: BLE001
            print(f"{_TAG} ⚠️ long_short_ratio indisponível ({sym_rest}): {e_lsr}")
            return None


def get_price_via_rest(symbol: str = "ETHUSDC") -> dict[str, float]:
    """
    Fallback REST direto (python-binance) para preço + L2 mínimo.
    Retorna dict com `price`, `bid`, `ask` (0.0 em falha).
    """
    api_key, api_secret = _carregar_chaves()
    try:
        from binance.client import Client  # type: ignore
    except Exception as e:  # noqa: BLE001
        print(f"{_TAG} REST fallback indisponível (python-binance não instalado): {e}")
        return {"price": 0.0, "bid": 0.0, "ask": 0.0}

    try:
        # Preferência por CCXT com símbolo perp (ETH/USDC:USDC) para evitar vazio no livro.
        ex = criar_exchange_binance()
        sym_ccxt = _resolver_simbolo_perp(ex, symbol)
        t_ccxt = ex.fetch_ticker(sym_ccxt)
        ob_ccxt = ex.fetch_order_book(sym_ccxt, limit=5)
        p_ccxt = float(t_ccxt.get("last") or t_ccxt.get("close") or 0.0)
        mk_c, lt_c = _extract_mark_and_last_from_ticker(t_ccxt)
        if p_ccxt <= 0 and mk_c > 0:
            p_ccxt = mk_c
        elif p_ccxt <= 0 and lt_c > 0:
            p_ccxt = lt_c
        bids_ccxt = (ob_ccxt or {}).get("bids") or []
        asks_ccxt = (ob_ccxt or {}).get("asks") or []
        b_ccxt = float(bids_ccxt[0][0]) if bids_ccxt else 0.0
        a_ccxt = float(asks_ccxt[0][0]) if asks_ccxt else 0.0
        if p_ccxt > 0:
            return {"price": p_ccxt, "bid": b_ccxt, "ask": a_ccxt}
    except Exception:
        pass

    try:
        rest_symbol = _simbolo_para_rest(symbol)
        client = Client(api_key, api_secret)
        t = client.futures_symbol_ticker(symbol=rest_symbol)
        ob = client.futures_order_book(symbol=rest_symbol, limit=5)
        price = float((t or {}).get("price") or 0.0)
        if price <= 0:
            try:
                mp_raw = client.futures_mark_price(symbol=rest_symbol)
                mp_row: dict[str, Any] = {}
                if isinstance(mp_raw, list) and mp_raw:
                    mp_row = mp_raw[0] if isinstance(mp_raw[0], dict) else {}
                elif isinstance(mp_raw, dict):
                    mp_row = mp_raw
                mv = (mp_row or {}).get("markPrice") or (mp_row or {}).get("price")
                if mv is not None:
                    price = float(mv)
            except Exception:
                pass
        bids = (ob or {}).get("bids") or []
        asks = (ob or {}).get("asks") or []
        best_bid = float(bids[0][0]) if bids else 0.0
        best_ask = float(asks[0][0]) if asks else 0.0
        return {"price": price, "bid": best_bid, "ask": best_ask}
    except Exception as e:  # noqa: BLE001
        print(f"{_TAG} REST fallback falhou ({symbol}): {e}")
        return {"price": 0.0, "bid": 0.0, "ask": 0.0}


def _extract_mark_and_last_from_ticker(t: dict[str, Any]) -> tuple[float, float]:
    """Mark price e last (CCXT Binance futures — `info` traz `markPrice`)."""
    last = float(t.get("last") or t.get("close") or 0.0)
    mark = 0.0
    raw_mark = t.get("mark")
    if raw_mark is not None:
        try:
            mark = float(raw_mark)
        except (TypeError, ValueError):
            mark = 0.0
    info = t.get("info") or {}
    if mark <= 0 and isinstance(info, dict):
        for key in ("markPrice", "indexPrice", "lastPrice", "p"):
            v = info.get(key)
            if v is None:
                continue
            try:
                mf = float(v)
                if mf > 0:
                    mark = mf
                    break
            except (TypeError, ValueError):
                pass
    if last <= 0 and isinstance(info, dict):
        for key in ("lastPrice", "c"):
            v = info.get(key)
            if v is None:
                continue
            try:
                lf = float(v)
                if lf > 0:
                    last = lf
                    break
            except (TypeError, ValueError):
                pass
    return mark, last


def _synthetic_book_level3(
    exchange: ccxt.binance,
    simbolo_ccxt: str,
    ticker: dict[str, Any] | None = None,
    *,
    emit_log: bool = True,
) -> tuple[float, float, float] | None:
    """
    Last resort: markPrice (preferido) ou lastPrice → bid = ref − 0.05, ask = ref + 0.05.
    Devolve (bid, ask, ref) com ref = preço de referência usado.
    Se `ticker` for passado (ex.: primeiro fetch_ticker), evita round-trip extra à API.
    """
    t: dict[str, Any] | None = ticker
    if t is None:
        try:
            t = exchange.fetch_ticker(simbolo_ccxt)
        except Exception:
            return None
    mk, lt = _extract_mark_and_last_from_ticker(t)
    ref = mk if mk > 0 else (lt if lt > 0 else 0.0)
    if ref <= 0:
        return None
    h = float(SYNTHETIC_BOOK_HALF_SPREAD)
    bid = ref - h
    ask = ref + h
    if bid <= 0 or ask <= bid:
        return None
    if emit_log:
        if mk > 0:
            print(
                "⚠️ [SYNTHETIC-BOOK] Livro vazio no REST/WS. "
                "Usando Mark Price como referência para execução."
            )
        else:
            print(
                "⚠️ [SYNTHETIC-BOOK] Livro vazio no REST/WS. "
                "Usando Last Price como referência para execução."
            )
    return bid, ask, ref


def _obter_tick_size(exchange: ccxt.binance, simbolo: str) -> float:
    m = exchange.market(simbolo)
    precision = (m.get("precision") or {}).get("price")
    if precision is not None:
        return float(10 ** (-int(precision)))
    price_filter_tick = None
    filters = (m.get("info") or {}).get("filters")
    if isinstance(filters, list):
        for f in filters:
            if str((f or {}).get("filterType") or "").upper() == "PRICE_FILTER":
                price_filter_tick = (f or {}).get("tickSize")
                break
    if price_filter_tick is not None:
        return float(price_filter_tick)
    return 0.01


def _resolver_simbolo_perp(exchange: ccxt.binance, simbolo: str) -> str:
    """
    Converte ETH/USDC no símbolo unificado do perpétuo linear (ex.: ETH/USDC:USDC).
    """
    exchange.load_markets()
    s_norm = str(simbolo or "").strip().upper()
    if s_norm in ("ETHUSDC", "ETH/USDC", "ETH/USDC:USDC"):
        simbolo = MARKET_SYMBOL
    if simbolo in exchange.markets:
        m = exchange.markets[simbolo]
        if m.get("swap") and m.get("linear"):
            return simbolo
    if "/" in simbolo and ":" not in simbolo:
        base, quote = simbolo.split("/", 1)
        cand = f"{base}/{quote}:{quote}"
        if cand in exchange.markets:
            return cand
    raise ValueError(
        f"{_TAG} Par perp linear não encontrado: {simbolo}. Use ex. ETH/{QUOTE_ASSET} ou ETH/{QUOTE_ASSET}:{QUOTE_ASSET}."
    )


def configurar_alavancagem(
    simbolo: str,
    alavancagem: int = 3,
    exchange: ccxt.binance | None = None,
) -> None:
    """
    Define margem **isolada** e alavancagem no contrato linear.
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    try:
        ex.set_margin_mode("ISOLATED", sym)
        print(f"{_TAG} Margem ISOLATED definida para {sym}.")
    except ccxt.ExchangeError as e:
        # Binance devolve erro se já estiver no modo ou houver posição aberta.
        print(f"{_TAG} set_margin_mode (pode ser esperado se já configurado): {e}", file=sys.stderr)
    try:
        ex.set_leverage(alavancagem, sym)
        print(f"{_TAG} Alavancagem {alavancagem}x aplicada em {sym}.")
    except ccxt.BaseError as e:
        print(f"{_TAG} Erro em set_leverage: {e}", file=sys.stderr)
        raise


def notional_usdt_futuros_position_sizing(
    exchange: ccxt.binance,
    alavancagem: float,
    *,
    risk_fraction: float | None = None,
) -> float:
    """Notional alvo: saldo USDC (margem futuros) × risk_fraction × alavancagem."""
    saldo = obter_saldo_usdt_margem(exchange)
    rf = float(risk_fraction) if risk_fraction is not None else float(PERCENTUAL_BANCA)
    if rf <= 0:
        rf = float(PERCENTUAL_BANCA)
    return float(saldo) * rf * float(alavancagem)


def analisar_pressao_order_book(
    simbolo: str = "ETH/USDC",
    exchange: ccxt.binance | None = None,
    *,
    depth_limit: int = 100,
    raio_frac: float = 0.01,
    parede_proxima_frac: float = 0.005,
) -> dict[str, Any]:
    """
    Analisa depth (100 níveis por omissão) e soma volumes no raio de preço:
    - bids em [preço_atual * (1-raio), preço_atual]
    - asks em [preço_atual, preço_atual * (1+raio)]
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    ticker = ex.fetch_ticker(sym)
    preco = float(ticker.get("last") or ticker.get("close") or 0.0)
    if preco <= 0:
        raise RuntimeError(f"{_TAG} Preço inválido para análise de order book em {sym}.")
    ob = ex.fetch_order_book(sym, limit=int(depth_limit))
    bids = ob.get("bids") or []
    asks = ob.get("asks") or []
    p_min = preco * (1.0 - float(raio_frac))
    p_max = preco * (1.0 + float(raio_frac))

    bid_vol = 0.0
    for lvl in bids:
        p = float(lvl[0])
        q = float(lvl[1])
        if p >= p_min and p <= preco:
            bid_vol += q

    ask_vol = 0.0
    min_ask_dist = None
    for lvl in asks:
        p = float(lvl[0])
        q = float(lvl[1])
        if p >= preco and p <= p_max:
            ask_vol += q
            dist = (p - preco) / preco
            if min_ask_dist is None or dist < min_ask_dist:
                min_ask_dist = dist

    ratio = ask_vol / bid_vol if bid_vol > 0 else float("inf") if ask_vol > 0 else 0.0
    muralha_venda = ask_vol >= (3.0 * bid_vol) and ask_vol > 0
    muralha_proxima = bool(
        muralha_venda
        and min_ask_dist is not None
        and float(min_ask_dist) <= float(parede_proxima_frac)
    )
    return {
        "simbolo": sym,
        "preco_atual": preco,
        "bid_volume_1pct": bid_vol,
        "ask_volume_1pct": ask_vol,
        "ask_bid_ratio": ratio,
        "muralha_venda": muralha_venda,
        "muralha_proxima": muralha_proxima,
        "dist_muralha_frac": min_ask_dist,
    }


def _quantidade_base_a_partir_de_usd(
    exchange: ccxt.binance,
    simbolo: str,
    quantidade_usd: float,
) -> float:
    """
    Converte notional em USDC para quantidade na base (contratos ETH), com precisão.
    Equivale a (notional_usd / preco_atual), com `amount_to_precision`.
    """
    if quantidade_usd <= 0:
        raise ValueError("quantidade_usd deve ser positiva.")
    ticker = exchange.fetch_ticker(simbolo)
    preco = float(ticker.get("last") or ticker.get("close") or 0)
    if preco <= 0:
        raise RuntimeError("Preço inválido para calcular quantidade.")
    q = quantidade_usd / preco
    return float(exchange.amount_to_precision(simbolo, q))


def _preco_referencia_ultimo(exchange: ccxt.binance, simbolo: str) -> float:
    """Último preço do ticker (mesma referência usada no sizing)."""
    ticker = exchange.fetch_ticker(simbolo)
    preco = float(ticker.get("last") or ticker.get("close") or 0)
    if preco <= 0:
        raise RuntimeError(f"{_TAG} Preço inválido para ordem limit de abertura em {simbolo}.")
    return preco


def _auto_heal_orderbook_via_rest(
    exchange: ccxt.binance,
    simbolo_ccxt: str,
) -> tuple[float, float, float] | None:
    """
    REST (CCXT + fallback) quando o livro/ticker vêm zerados; depois Nível 3 (mark/last ±0,05).
    Devolve (bid, ask, last_ref) ou None se não existir preço de referência.
    """
    print(
        "🔄 [AUTO-HEALING] WebSocket zerado, recuperando dados via REST para entrada automática..."
    )
    rest_sym = _simbolo_para_rest(simbolo_ccxt)
    data = get_price_via_rest(rest_sym)
    price = float(data.get("price") or 0.0)
    best_bid = float(data.get("bid") or 0.0)
    best_ask = float(data.get("ask") or 0.0)
    if price > 0 and best_bid > 0 and best_ask > 0:
        return best_bid, best_ask, price

    synth = _synthetic_book_level3(exchange, simbolo_ccxt)
    if synth is not None:
        return synth

    if price > 0:
        tick = _obter_tick_size(exchange, simbolo_ccxt)
        return max(0.0, price - tick), price + tick, price

    diag_bid, diag_ask, diag_last = best_bid, best_ask, price
    try:
        tf = exchange.fetch_ticker(simbolo_ccxt)
        diag_bid = float(tf.get("bid") or 0.0)
        diag_ask = float(tf.get("ask") or 0.0)
        diag_last = float(tf.get("last") or tf.get("close") or 0.0)
    except Exception:
        pass
    last_price = float(diag_last)
    print(f"[DEBUG] Abortado: bid={diag_bid}, ask={diag_ask}, last={last_price}")
    return None


def _preco_limite_limit_offset_book(
    exchange: ccxt.binance,
    simbolo: str,
    side: str,
    *,
    offset_frac: float | None = None,
    force_reference_price: float | None = None,
    is_manual_force: bool = False,
) -> tuple[float, float, float, float]:
    """
    Preço LIMIT com offset 0,05% a partir do livro:
    - buy (LONG / fechar SHORT): base = bid → limite = bid × (1 + offset)
    - sell (SHORT / fechar LONG): base = ask → limite = ask × (1 − offset)
    Devolve (limite_arredondado, base, bid, ask) para logs.
    """
    off = float(offset_frac) if offset_frac is not None else float(PRECO_ABERTURA_LIMITE_OFFSET)
    ticker = exchange.fetch_ticker(simbolo)
    bid = float(ticker.get("bid") or 0.0)
    ask = float(ticker.get("ask") or 0.0)
    last = float(ticker.get("last") or ticker.get("close") or 0.0)
    if bid > 0 and ask > 0:
        pass
    elif is_manual_force:
        ref = float(force_reference_price or last or 0.0)
        used_synth_book = False
        if ref <= 0:
            healed_m = _synthetic_book_level3(exchange, simbolo, ticker=ticker, emit_log=True)
            if healed_m is not None:
                bid, ask, last = healed_m
                ref = float(last)
                used_synth_book = True
            if ref <= 0:
                msg = "❌ [DATA-ERROR] Orderbook zerado. Abortando entrada para evitar preço inválido."
                print(msg, file=sys.stderr)
                last_price = float(last)
                print(f"[DEBUG] Abortado: bid={bid}, ask={ask}, last={last_price}")
                raise RuntimeError(msg)
        if not used_synth_book:
            bid = max(0.0, ref - 0.01)
            ask = ref + 0.01
        print(
            "⚠️ [FORCE-BYPASS] Orderbook zerado, usando Preço WS como referência para não travar a execução."
        )
    else:
        syn_first = _synthetic_book_level3(exchange, simbolo, ticker=ticker, emit_log=True)
        if syn_first is not None:
            bid, ask, last = syn_first
        else:
            healed = _auto_heal_orderbook_via_rest(exchange, simbolo)
            if healed is None:
                msg = "❌ [CRITICAL] Dados indisponíveis em todos os canais. Abortando IA."
                print(msg, file=sys.stderr)
                raise RuntimeError(msg)
            bid, ask, last = healed
    s = str(side).strip().lower()
    if s == "buy":
        base = bid if bid > 0 else last
        raw = base * (1.0 + off)
    elif s == "sell":
        base = ask if ask > 0 else last
        raw_offset = base * (1.0 - off)
        raw = max(ask, raw_offset)  # maker-guaranteed: SHORT nunca cruza abaixo do melhor ASK
    else:
        raise ValueError(f"{_TAG} side inválido para LIMIT+offset: {side!r}")
    if base <= 0 or raw <= 0:
        last_price = float(last)
        print(f"[DEBUG] Abortado: bid={bid}, ask={ask}, last={last_price}")
        raise RuntimeError(
            f"{_TAG} bid/ask/last inválidos para {simbolo} após cálculo de offset."
        )
    limite = float(exchange.price_to_precision(simbolo, raw))
    return limite, base, bid, ask


def obter_ultimo_preco(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> float:
    """Último preço (ticker) do perpétuo — alias explícito para chase / scripts externos."""
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    return _preco_referencia_ultimo(ex, sym)


def executar_com_chase(
    simbolo: str,
    direcao: str,
    qty: float,
    exchange: ccxt.binance | None = None,
    *,
    tentativas: int = 3,
) -> dict[str, Any] | None:
    """
    Chase: LIMIT IOC (entrada) com offset 0,05% vs bid/ask; após timeout verifica fill;
    se não, cancela (se aplicável) e reabre até `tentativas` rondas.

    - `direcao`: «LONG» (buy) ou «SHORT» (sell).
    Devolve o dict da ordem da bolsa se preenchida; `None` se esgotar tentativas.
    """
    d = str(direcao).strip().upper()
    if d not in ("LONG", "SHORT"):
        raise ValueError(f"{_TAG} direcao deve ser LONG ou SHORT, recebido: {d!r}")
    if qty <= 0:
        raise ValueError(f"{_TAG} qty deve ser positivo.")

    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    side = "buy" if d == "LONG" else "sell"

    try:
        return _entrar_limite_com_chase_futuros(
            ex,
            sym,
            side,
            float(qty),
            nome_lado=d,
            max_rounds=max(1, int(tentativas)),
        )
    except RuntimeError as e:
        print(f"{_TAG} executar_com_chase: {e}", file=sys.stderr)
        return None


def _entrar_limite_com_chase_futuros(
    ex: ccxt.binance,
    simbolo: str,
    side: str,
    amt: float,
    *,
    nome_lado: str,
    max_rounds: int | None = None,
    timeout_s: float | None = None,
    reduce_only: bool = False,
    force_reference_price: float | None = None,
    is_manual_force: bool = False,
    offset_frac: float | None = None,
) -> dict[str, Any]:
    """
    LIMIT + offset (bid/ask ±0,05%) com protocolo maker-only (GTX / Post-Only).
    Se rejeitar por Post-Only, reenvia com ajuste de 1 tick no preço.
    """
    off_effective = (
        float(offset_frac) if offset_frac is not None else float(PRECO_ABERTURA_LIMITE_OFFSET)
    )
    cap = max_rounds if max_rounds is not None else CHASE_ENTRADA_MAX_ROUNDS
    wait = float(timeout_s) if timeout_s is not None else CHASE_ENTRADA_TIMEOUT_S
    tif = "GTX"
    params_order: dict[str, Any] = {"timeInForce": tif}
    if reduce_only:
        params_order["reduceOnly"] = True
        params_order["workingType"] = "MARK_PRICE"

    qty_left = float(ex.amount_to_precision(simbolo, float(amt)))
    if qty_left <= 0:
        raise ValueError(f"{_TAG} qty inválida após precisão: {amt!r}")

    last_order: dict[str, Any] | None = None

    for rnd in range(1, cap + 1):
        preco_limite, base, bid, ask = _preco_limite_limit_offset_book(
            ex,
            simbolo,
            side,
            offset_frac=off_effective,
            force_reference_price=force_reference_price,
            is_manual_force=is_manual_force,
        )
        ref = "bid" if str(side).lower() == "buy" else "ask"
        ro_txt = ", reduceOnly" if reduce_only else ""
        print(
            f"{_TAG} [LIMIT+offset {rnd}/{cap}] {nome_lado} {side.upper()} @ {preco_limite} "
            f"qty={qty_left} (ref {ref}={base:.4f}, bid={bid:.4f}, ask={ask:.4f}, {tif}{ro_txt})"
        )
        print(f"[DEBUG] Spread: {ask - bid:.4f} | Alvo Maker: {preco_limite:.4f}")

        if reduce_only:
            d_ro = "LONG" if str(side).lower() == "sell" else "SHORT"
            if not _assert_seguranca_antes_create_ordem_protecao(
                ex,
                simbolo,
                d_ro,
                context=f"chase_LIMIT_reduce {nome_lado} r{rnd}",
                allow_other_open_orders=True,
            ):
                raise RuntimeError(f"{_TAG} chase reduce-only bloqueado por anti-spam ({nome_lado} r{rnd}).")

        try:
            order = ex.create_order(
                simbolo,
                "limit",
                side,
                qty_left,
                preco_limite,
                params_order,
            )
        except ccxt.BaseError as e:
            err = str(e).lower()
            post_only = ("post" in err and "only" in err) or "gtx" in err
            if not post_only:
                print(f"{_TAG} ❌ [BINANCE-ERROR] {e}", file=sys.stderr)
                print(f"{_TAG} ❌ Erro no Chase (create_order): {e}", file=sys.stderr)
                continue
            try:
                time.sleep(0.2)  # backoff curto para evitar spam de API em rejeições encadeadas
                tick = _obter_tick_size(ex, simbolo)
                tick_steps = 1 if (rnd % 2 == 1) else 2
                if str(side).lower() == "sell":
                    tkr_rt = ex.fetch_ticker(simbolo)
                    bid_rt = float(tkr_rt.get("bid") or 0.0)
                    ask_rt = float(tkr_rt.get("ask") or tkr_rt.get("last") or tkr_rt.get("close") or 0.0)
                    if bid_rt > 0 and ask_rt > 0:
                        pass
                    elif is_manual_force:
                        ref_rt = float(force_reference_price or 0.0)
                        last_rt = float(tkr_rt.get("last") or tkr_rt.get("close") or 0.0)
                        used_synth_rt = False
                        if ref_rt <= 0:
                            ref_rt = last_rt
                        if ref_rt <= 0:
                            sm_rt = _synthetic_book_level3(ex, simbolo, ticker=tkr_rt, emit_log=True)
                            if sm_rt is not None:
                                bid_rt, ask_rt, _ = sm_rt
                                used_synth_rt = True
                            else:
                                msg = (
                                    "❌ [DATA-ERROR] Orderbook zerado. "
                                    "Abortando entrada para evitar preço inválido."
                                )
                                print(msg, file=sys.stderr)
                                last_price = float(last_rt)
                                print(
                                    f"[DEBUG] Abortado: bid={bid_rt}, ask={ask_rt}, last={last_price}"
                                )
                                raise RuntimeError(msg)
                        if not used_synth_rt:
                            bid_rt = max(0.0, ref_rt - 0.01)
                            ask_rt = ref_rt + 0.01
                        print(
                            "⚠️ [FORCE-BYPASS] Orderbook zerado, usando Preço WS como referência "
                            "para não travar a execução."
                        )
                    else:
                        sm_ch = _synthetic_book_level3(ex, simbolo, ticker=tkr_rt, emit_log=True)
                        if sm_ch is not None:
                            bid_rt, ask_rt, _ = sm_ch
                        else:
                            healed_rt = _auto_heal_orderbook_via_rest(ex, simbolo)
                            if healed_rt is None:
                                msg = (
                                    "❌ [CRITICAL] Dados indisponíveis em todos os canais. "
                                    "Abortando IA."
                                )
                                print(msg, file=sys.stderr)
                                raise RuntimeError(msg)
                            bid_rt, ask_rt, _ = healed_rt
                    off_rt = off_effective
                    raw_calc_rt = ask_rt * (1.0 - off_rt)
                    preco_aj = max(ask_rt + (tick * tick_steps), raw_calc_rt)
                    print(f"🎯 [MAKER-SYNC] Ajustando preço para ficar acima do Bid: {preco_aj:.2f}")
                    print(f"[DEBUG] Spread: {ask_rt - bid_rt:.4f} | Alvo Maker: {preco_aj:.4f}")
                else:
                    preco_aj = max(tick, float(preco_limite) - (tick * tick_steps))
                preco_aj = float(ex.price_to_precision(simbolo, preco_aj))
                print(
                    f"{_TAG} ⚠️ Rejeição Post-Only; retry com {tick_steps} ticks "
                    f"({preco_limite} -> {preco_aj})."
                )
                if reduce_only:
                    d_ro2 = "LONG" if str(side).lower() == "sell" else "SHORT"
                    if not _assert_seguranca_antes_create_ordem_protecao(
                        ex,
                        simbolo,
                        d_ro2,
                        context=f"chase_LIMIT_reduce_retry {nome_lado} r{rnd}",
                        allow_other_open_orders=True,
                    ):
                        raise RuntimeError(
                            f"{_TAG} chase reduce-only retry bloqueado por anti-spam ({nome_lado} r{rnd})."
                        )
                order = ex.create_order(
                    simbolo,
                    "limit",
                    side,
                    qty_left,
                    preco_aj,
                    params_order,
                )
            except ccxt.BaseError as e2:
                print(f"{_TAG} ❌ [BINANCE-ERROR] {e2}", file=sys.stderr)
                print(f"{_TAG} ❌ Erro no retry GTX dinâmico: {e2}", file=sys.stderr)
                if rnd >= cap:
                    msg = "OPORTUNIDADE PERDIDA - VOLATILIDADE ALTA"
                    print(f"{_TAG} ⚠️ [{msg}] {nome_lado} após {cap} tentativas GTX.")
                    raise RuntimeError(f"{_TAG} {msg}") from e2
                continue

        oid = order.get("id")
        if oid is None:
            raise RuntimeError(f"{_TAG} Ordem sem id após create_order.")

        last_order = dict(order)

        filled0 = float(order.get("filled") or 0)
        st0 = (order.get("status") or "").lower()
        if filled0 >= qty_left * 0.97 or st0 in ("closed", "filled"):
            print(f"{_TAG} ✅ [SUCESSO] {nome_lado}: fill na resposta da bolsa.")
            return last_order

        print(
            f"{_TAG} ⏳ [CHASE {rnd}/{cap}] Ordem LIMIT {tif} em {preco_limite}. "
            f"Aguardando {wait:.0f}s..."
        )
        time.sleep(wait)

        try:
            check = ex.fetch_order(oid, simbolo)
        except ccxt.BaseError as ef:
            print(f"{_TAG} fetch_order: {ef}", file=sys.stderr)
            check = order

        last_order = dict(check)

        filled = float(check.get("filled") or 0)
        status = (check.get("status") or "").lower()
        if filled >= qty_left * 0.97 or status in ("closed", "filled"):
            print(f"{_TAG} ✅ [SUCESSO] Ordem preenchida (ronda {rnd}).")
            return last_order

        if filled > 0:
            qty_left = float(
                ex.amount_to_precision(simbolo, max(0.0, qty_left - filled))
            )
        if qty_left <= 0:
            print(f"{_TAG} ✅ [SUCESSO] qty alvo preenchida por fills acumulados.")
            return last_order

        try:
            ex.cancel_order(oid, simbolo)
        except ccxt.BaseError as ec:
            print(f"{_TAG} cancel chase (ok se já executou/IOC): {ec}", file=sys.stderr)

        print(
            f"{_TAG} ⚠️ [TIMEOUT] Preço fugiu — recalculando chase ({rnd}/{cap}) "
            f"(qty_left={qty_left})..."
        )

    msg = "OPORTUNIDADE PERDIDA - VOLATILIDADE ALTA"
    print(f"{_TAG} ⚠️ [{msg}] {nome_lado} após {cap} tentativas GTX.")
    raise RuntimeError(f"{_TAG} {msg}")


def _log_liquidacao_estimada(
    preco_entrada: float,
    lado: str,
    alavancagem: float,
) -> None:
    """
    Estimativa didática (margem isolada, sem taxas de manutenção):
    Long: P_liq ≈ P × (1 − 1/L) | Short: P_liq ≈ P × (1 + 1/L)
    """
    if preco_entrada <= 0 or alavancagem <= 0:
        return
    inv = 1.0 / alavancagem
    if str(lado).upper() == "LONG":
        liq = preco_entrada * (1.0 - inv)
        print(
            f"{_TAG} Liquidação estimada (≈): LONG — preço ~{liq:.4f} USDC "
            f"| P_entrada×(1−1/L) = {preco_entrada:.4f}×(1−1/{alavancagem:.4f})"
        )
    else:
        liq = preco_entrada * (1.0 + inv)
        print(
            f"{_TAG} Liquidação estimada (≈): SHORT — preço ~{liq:.4f} USDC "
            f"| P_entrada×(1+1/L) = {preco_entrada:.4f}×(1+1/{alavancagem:.4f})"
        )


def _aguardar_qty_e_preco_entrada(
    exchange: ccxt.binance,
    simbolo: str,
    *,
    timeout_s: float = 4.0,
) -> tuple[float, float]:
    """Após ordem de abertura (limit), obtém quantidade (contratos) e preço médio de entrada."""
    t0 = time.time()
    last_err: str | None = None
    while time.time() - t0 < timeout_s:
        try:
            exchange.fetch_positions([simbolo])
            for p in exchange.fetch_positions([simbolo]):
                c = float(p.get("contracts") or 0)
                ep = p.get("entryPrice")
                if ep is None and p.get("info"):
                    ep = p["info"].get("entryPrice")
                if abs(c) > 0 and ep is not None:
                    efv = float(ep)
                    if efv > 0:
                        return abs(c), efv
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(0.25)
    raise RuntimeError(
        f"{_TAG} Não foi possível ler qty/entry após abrir posição em {simbolo}. "
        f"Último erro: {last_err}"
    )


def cancelar_ordens_entrada_short(
    exchange: ccxt.binance,
    simbolo: str,
) -> int:
    """
    Cancela ordens LIMIT de abertura de SHORT (venda sem reduce-only).
    Não cancela TP/SL (reduce-only).
    """
    sym = _resolver_simbolo_perp(exchange, simbolo)
    n = 0
    try:
        abertas = exchange.fetch_open_orders(sym)
    except ccxt.BaseError as e:
        print(f"{_TAG} fetch_open_orders: {e}", file=sys.stderr)
        return 0
    for o in abertas:
        if str(o.get("side") or "").lower() != "sell":
            continue
        if bool(o.get("reduceOnly")):
            continue
        info = o.get("info") or {}
        if info.get("reduceOnly") in (True, "true", "True"):
            continue
        oid = o.get("id")
        if oid is None:
            continue
        try:
            exchange.cancel_order(oid, sym)
            n += 1
        except ccxt.BaseError as ec:
            print(f"{_TAG} cancel ordem entrada SHORT {oid}: {ec}", file=sys.stderr)
    if n:
        print(f"{_TAG} Canceladas {n} ordem(ns) de entrada SHORT em {sym}.")
    return n


def cancelar_todas_ordens_abertas(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> int:
    """
    Cancela todas as ordens abertas no par (futures linear).
    Chame ao fechar posição para evitar ordens reduce-only órfãs.
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    n = 0
    try:
        r = ex.cancel_all_orders(sym)
        if isinstance(r, list):
            n = len(r)
        print(f"{_TAG} cancelar_todas_ordens_abertas({sym}): removidas ~{n} ordem(ns).")
        return n
    except ccxt.BaseError as e1:
        print(f"{_TAG} cancel_all_orders falhou ({e1}); a tentar ordem a ordem...", file=sys.stderr)
        try:
            abertas = ex.fetch_open_orders(sym)
            for o in abertas:
                oid = o.get("id")
                if oid is not None:
                    ex.cancel_order(oid, sym)
                    n += 1
            print(f"{_TAG} Canceladas {n} ordem(ns) em {sym}.")
        except ccxt.BaseError as e2:
            print(f"{_TAG} Erro ao cancelar ordens: {e2}", file=sys.stderr)
            raise
        return n


def cancelar_ordens_reduce_only_abertas(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> int:
    """
    Cancela apenas ordens reduce-only (TP/SL/trailing) no par — usado na reconciliação
    antes de recriar brackets sem apagar ordens de entrada não-reduce.
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    n = 0
    try:
        abertas = ex.fetch_open_orders(sym)
    except ccxt.BaseError as e:
        print(f"{_TAG} fetch_open_orders (reduce-only cleanup): {e}", file=sys.stderr)
        return 0
    for o in abertas:
        ro = bool(o.get("reduceOnly"))
        if not ro:
            info = o.get("info") or {}
            v = str(info.get("reduceOnly", "")).lower()
            ro = v in ("true", "1", "yes")
        if not ro:
            continue
        oid = o.get("id")
        if oid is None:
            continue
        try:
            ex.cancel_order(oid, sym)
            n += 1
        except ccxt.BaseError as ec:
            print(f"{_TAG} cancel reduce-only {oid}: {ec}", file=sys.stderr)
    if n:
        print(f"{_TAG} Canceladas {n} ordem(ns) reduce-only em {sym} (reconciliação).")
    return n


def _futures_symbol_id(exchange: ccxt.binance, simbolo: str) -> str:
    """Símbolo no formato nativo da API Binance Futures (ex.: ETHUSDC)."""
    sym = _resolver_simbolo_perp(exchange, simbolo)
    m = exchange.market(sym)
    sid = str(m.get("id") or "").strip().upper()
    return sid if sid else str(simbolo).replace("/", "").replace(":USDC", "").upper()


def cancelar_todas_ordens_futures_nativo(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> None:
    """
    Cancelamento nativo de Futures (equiv. `futures_cancel_all_open_orders`).
    Fallback para CCXT unificado apenas se endpoint nativo falhar.
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    sid = _futures_symbol_id(ex, simbolo)
    try:
        ex.fapiPrivateDeleteAllOpenOrders({"symbol": sid})
        print(f"{_TAG} Nuke nativo futures executado para {sid}.")
        return
    except Exception as e_native:  # noqa: BLE001
        print(
            f"{_TAG} Nuke nativo futures falhou ({e_native}); fallback cancel_all_orders em {sym}.",
            file=sys.stderr,
        )
    try:
        ex.cancel_all_orders(sym)
    except Exception as e_fallback:  # noqa: BLE001
        print(f"{_TAG} Fallback cancel_all_orders falhou: {e_fallback}", file=sys.stderr)


def obter_ordens_abertas_futures_nativo(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> list[dict[str, Any]]:
    """Leitura nativa de ordens abertas futures (equiv. `futures_get_open_orders`)."""
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    sid = _futures_symbol_id(ex, simbolo)
    try:
        rows = ex.fapiPrivateGetOpenOrders({"symbol": sid})
        return rows if isinstance(rows, list) else []
    except Exception as e_native:  # noqa: BLE001
        print(
            f"{_TAG} leitura nativa open_orders falhou ({e_native}); fallback fetch_open_orders({sym}).",
            file=sys.stderr,
        )
    try:
        rows_ccxt = ex.fetch_open_orders(sym)
        return rows_ccxt if isinstance(rows_ccxt, list) else []
    except Exception as e_fb:  # noqa: BLE001
        print(f"{_TAG} fetch_open_orders fallback falhou: {e_fb}", file=sys.stderr)
        return []


def enforce_single_order_type(
    exchange: ccxt.binance,
    symbol: str,
    order_type: str,
) -> int:
    """
    Nuke-and-pave por tipo: cancela TODAS as ordens abertas do `order_type` no símbolo.
    Usa cancelamento nativo futures por `orderId` (fapiPrivateDeleteOrder).
    """
    sym = _resolver_simbolo_perp(exchange, symbol)
    sid = _futures_symbol_id(exchange, sym)
    tipo = str(order_type).upper().replace("-", "_")
    to_cancel: list[str] = []
    for o in _fetch_open_orders_ccxt_list(exchange, sym):
        if _order_type_norm(o) != tipo:
            continue
        oid = o.get("id")
        if oid is None:
            info = o.get("info") or {}
            oid = info.get("orderId")
        if oid is None:
            continue
        to_cancel.append(str(oid))
    if not to_cancel:
        return 0
    print(
        f"{_TAG} [SHIELD] Limpando ordens antigas do tipo {tipo} antes de criar a nova.",
        flush=True,
    )
    n = 0
    for oid in to_cancel:
        try:
            exchange.fapiPrivateDeleteOrder({"symbol": sid, "orderId": oid})
            n += 1
        except Exception as e_native:  # noqa: BLE001
            try:
                exchange.cancel_order(oid, sym)
                n += 1
            except Exception as e_fb:  # noqa: BLE001
                print(
                    f"{_TAG} [SHIELD] Falha ao cancelar {tipo} orderId={oid} "
                    f"(native={e_native} | fallback={e_fb})",
                    file=sys.stderr,
                    flush=True,
                )
    time.sleep(1.0)
    return n


def assegurar_brackets_apos_reconciliacao(
    simbolo: str,
    exchange: ccxt.binance,
    direcao: str,
    qty_contratos: float,
    preco_entrada: float,
    *,
    trailing_callback_rate: float | None = None,
    trailing_activation_multiplier: float | None = None,
    source_tag: str | None = None,
    sl_break_even: bool = False,
    force_replace: bool = True,
) -> None:
    """
    Após detetar posição na Binance sem estado completo no maestro: cancela **só**
    STOP_MARKET / TRAILING_STOP_MARKET reduce-only (via `fetch_open_orders` + `cancel_order`),
    preservando TAKE_PROFIT_MARKET e ordens LIMIT; valida livro limpo antes de recriar
    SL dinâmico + TRAILING_STOP (modo vigia na bolsa).
    Se `sl_break_even=True`, o STOP_MARKET de protecção fica no preço de entrada (risco zero).

    `force_replace=True` (default): sempre cancela e recria (recon, ORDER_GUARD, híbrido).
    `force_replace=False`: ignora chamadas repetidas com a mesma assinatura dentro de
    `BRACKET_REPLACE_DEBOUNCE_S`; se a assinatura mudar dentro da janela, exige movimento de mark
    ≥ `BRACKET_REPLACE_MIN_FAVORABLE_PRICE_MOVE` a favor da posição (anti-spam API).
    """
    global _bracket_last_mono_by_sym, _bracket_last_sig_by_sym, _bracket_last_anchor_mark_by_sym
    d = str(direcao).strip().upper()
    sym = _resolver_simbolo_perp(exchange, simbolo)
    q = abs(float(qty_contratos))
    if q <= 0:
        print(f"{_TAG} assegurar_brackets: qty inválida ({qty_contratos!r})", file=sys.stderr)
        return
    cb_rate = (
        float(trailing_callback_rate)
        if trailing_callback_rate is not None
        else float(TRAILING_CALLBACK_RATE)
    )
    act_mult = (
        float(trailing_activation_multiplier)
        if trailing_activation_multiplier is not None
        else float(TRAILING_ACTIVATION_MULTIPLIER)
    )

    sig = (round(cb_rate, 9), round(act_mult, 9), bool(sl_break_even))
    now_m = time.monotonic()
    if not force_replace:
        prev_sig = _bracket_last_sig_by_sym.get(sym)
        last_t = _bracket_last_mono_by_sym.get(sym, 0.0)
        if prev_sig == sig and (now_m - last_t) < float(BRACKET_REPLACE_DEBOUNCE_S):
            print(
                f"{_TAG} assegurar_brackets: debounce {BRACKET_REPLACE_DEBOUNCE_S:.0f}s "
                f"(mesma assinatura callback/mult/BE) — skip [{source_tag or '—'}].",
                flush=True,
            )
            return
        if (
            prev_sig is not None
            and prev_sig != sig
            and (now_m - last_t) < float(BRACKET_REPLACE_DEBOUNCE_S)
        ):
            mark_now = _obter_mark_price_futures(exchange, sym)
            anchor = _bracket_last_anchor_mark_by_sym.get(sym, 0.0)
            thr = float(BRACKET_REPLACE_MIN_FAVORABLE_PRICE_MOVE)
            if anchor > 0 and mark_now > 0 and thr > 0:
                if d == "LONG":
                    if mark_now < anchor * (1.0 + thr):
                        print(
                            f"{_TAG} assegurar_brackets: mark ainda não subiu ≥{thr:.2%} vs último "
                            f"sucesso — skip retune [{source_tag or '—'}].",
                            flush=True,
                        )
                        return
                elif d == "SHORT":
                    if mark_now > anchor * (1.0 - thr):
                        print(
                            f"{_TAG} assegurar_brackets: mark ainda não desceu ≥{thr:.2%} vs último "
                            f"sucesso — skip retune [{source_tag or '—'}].",
                            flush=True,
                        )
                        return

    print(
        f"{_TAG} assegurar_brackets [{source_tag or '—'}]: reconciliar proteções "
        "(limpeza total + recriação síncrona em `_criar_bracket_*`).",
        flush=True,
    )
    _assert_posicao_aberta_para_protecao_sync(
        exchange, simbolo, d, context=f"assegurar[{source_tag or '—'}]_pre_create"
    )
    atr_risk: float | None = None
    if RISK_ATR_MODE:
        atr_risk = calcular_atr_absoluto(exchange, sym)
        if atr_risk is not None:
            print(
                f"{_TAG} [RISK-ATR] assegurar_brackets [{source_tag or '—'}]: ATR14={atr_risk:.6f} "
                f"tf={AURIC_ATR_TIMEFRAME} n={AURIC_ATR_PERIOD}",
                flush=True,
            )
        elif atr_risk is None:
            print(
                f"{_TAG} [RISK-ATR] ATR indisponível — a usar brackets em percentagem (legado).",
                flush=True,
            )
    if d == "LONG":
        _criar_bracket_long(
            exchange,
            sym,
            q,
            float(preco_entrada),
            trailing_callback_rate=cb_rate,
            trailing_activation_multiplier=act_mult,
            sl_break_even=sl_break_even,
            risk_atr_abs=atr_risk,
        )
    elif d == "SHORT":
        _criar_bracket_short(
            exchange,
            sym,
            q,
            float(preco_entrada),
            trailing_callback_rate=cb_rate,
            trailing_activation_multiplier=act_mult,
            sl_break_even=sl_break_even,
            risk_atr_abs=atr_risk,
        )
    else:
        raise ValueError(f"{_TAG} direcao inválida: {d!r}")
    mark_done = _obter_mark_price_futures(exchange, sym)
    if mark_done > 0:
        _bracket_last_anchor_mark_by_sym[sym] = mark_done
    _bracket_last_mono_by_sym[sym] = time.monotonic()
    _bracket_last_sig_by_sym[sym] = sig

    if source_tag:
        print(
            f"{_TAG} [{source_tag}] Brackets reconciliados com callbackRate="
            f"{float(trailing_callback_rate) if trailing_callback_rate is not None else float(TRAILING_CALLBACK_RATE):.3f}%."
        )


def _params_reduce_futures() -> dict[str, Any]:
    """Parâmetros comuns em ordens de fecho na Binance Futures linear."""
    return {
        "reduceOnly": True,
        "workingType": "MARK_PRICE",
    }


def _sl_frac_from_trailing_callback_pct(cb_rate_pct: float) -> float:
    """Fração de preço do SL inicial = SL_DISTANCE_VS_TRAILING_MULT × (callbackRate%/100)."""
    return max(1e-9, (float(cb_rate_pct) / 100.0) * float(SL_DISTANCE_VS_TRAILING_MULT))


def _stop_price_break_even_exato(
    exchange: ccxt.binance,
    simbolo: str,
    preco_entrada: float,
    *,
    direcao: str,
) -> float:
    """
    Break-even estrito para STOP_MARKET:
    - LONG (stop de venda): nunca abaixo da entrada (arredonda para cima no tick).
    - SHORT (stop de compra): nunca acima da entrada (arredonda para baixo no tick).
    """
    pe = float(preco_entrada)
    m = exchange.market(simbolo)
    prec = m.get("precision", {}) if isinstance(m, dict) else {}
    p_digits_raw = prec.get("price")
    if isinstance(p_digits_raw, int) and p_digits_raw >= 0:
        step = 10.0 ** (-p_digits_raw)
        if str(direcao).upper() == "LONG":
            px = math.ceil(pe / step) * step
        else:
            px = math.floor(pe / step) * step
        return float(exchange.price_to_precision(simbolo, px))
    # Fallback para símbolos sem precisão decimal explícita.
    return float(exchange.price_to_precision(simbolo, pe))


def calcular_atr_absoluto(
    exchange: ccxt.binance,
    simbolo_ccxt: str,
    *,
    timeframe: str | None = None,
    period: int | None = None,
) -> float | None:
    """
    ATR (Wilder) em preço absoluto sobre `timeframe` (default `AURIC_ATR_TIMEFRAME`).
    Devolve None se dados insuficientes ou erro de API.
    """
    tf = (timeframe or AURIC_ATR_TIMEFRAME).strip() or "15m"
    n = int(period) if period is not None else int(AURIC_ATR_PERIOD)
    n = max(2, n)
    lim = max(n + 5, 80)
    try:
        exchange.load_markets()
        ohlcv = exchange.fetch_ohlcv(simbolo_ccxt, timeframe=tf, limit=lim)
    except ccxt.BaseError as e:
        print(f"{_TAG} calcular_atr_absoluto fetch_ohlcv: {e}", file=sys.stderr)
        return None
    if not ohlcv or len(ohlcv) < n + 1:
        return None
    highs = [float(c[2]) for c in ohlcv]
    lows = [float(c[3]) for c in ohlcv]
    closes = [float(c[4]) for c in ohlcv]
    if len(highs) < n + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    atr = sum(trs[:n]) / float(n)
    for tr in trs[n:]:
        atr = (atr * float(n - 1) + float(tr)) / float(n)
    return float(atr) if atr > 0 else None


def _callback_rate_pct_from_atr(atr: float, ref_price: float) -> float:
    """
    Converte ~1×ATR de recuo desde o máximo pós-ativação num `callbackRate` % (Binance TRAILING_STOP_MARKET).
    Limites típicos Binance: 0,1%–5%.
    """
    px = max(float(ref_price), 1e-12)
    a = max(float(atr), 0.0) * float(RISK_ATR_TRAIL_CALLBACK_FRAC)
    raw_pct = 100.0 * a / px
    return max(0.1, min(5.0, round(raw_pct, 2)))


def _criar_bracket_long(
    exchange: ccxt.binance,
    simbolo: str,
    qty: float,
    preco_entrada: float,
    *,
    trailing_callback_rate: float | None = None,
    trailing_activation_multiplier: float | None = None,
    sl_break_even: bool = False,
    risk_atr_abs: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """
    Bracket LONG na Binance Futures linear:
    - STOP_MARKET (SL inicial = SL_DISTANCE_VS_TRAILING_MULT × trailing %, ou break-even)
    - TRAILING_STOP_MARKET (take-profit dinâmico; ativa no antigo gatilho de TP)

    Se `risk_atr_abs` > 0 (modo ATR): SL a `RISK_ATR_SL_MULT`×ATR abaixo da entrada; ativação do
    trailing a `RISK_ATR_TRAIL_ACTIV_MULT`×ATR acima; `callbackRate` ≈ 1×ATR em % do preço (rede
    ~1 ATR abaixo do máximo pós-ativação). Com Free Runner: TAKE_PROFIT_MARKET em
    `RISK_ATR_TP_REF_MULT`×ATR sobre `AURIC_PARTIAL_TP_PCT` da posição; runner com SL+trailing.

    Devolve (ord_trailing, ord_sl, ord_tp_parcial) — o terceiro dict pode estar vazio.
    """
    global _in_protection_order_batch
    cancelar_livro_aberto_ate_zero_sync(
        exchange, simbolo, context="bracket_LONG(pré)"
    )
    _in_protection_order_batch = True
    try:
        return _criar_bracket_long_impl(
            exchange,
            simbolo,
            qty,
            preco_entrada,
            trailing_callback_rate=trailing_callback_rate,
            trailing_activation_multiplier=trailing_activation_multiplier,
            sl_break_even=sl_break_even,
            risk_atr_abs=risk_atr_abs,
        )
    finally:
        _in_protection_order_batch = False


def _criar_bracket_long_impl(
    exchange: ccxt.binance,
    simbolo: str,
    qty: float,
    preco_entrada: float,
    *,
    trailing_callback_rate: float | None = None,
    trailing_activation_multiplier: float | None = None,
    sl_break_even: bool = False,
    risk_atr_abs: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not _sync_posicao_antes_loop_protecao(
        exchange,
        simbolo,
        "LONG",
        context="bracket_LONG",
    ):
        return {}, {}, {}
    _log_protection_counts(exchange, simbolo, "LONG", context="bracket_LONG(pre)")
    _enforce_max_tres_condicionais(exchange, simbolo, "LONG")
    q = float(exchange.amount_to_precision(simbolo, qty))
    cb_rate = (
        float(trailing_callback_rate)
        if trailing_callback_rate is not None
        else float(TRAILING_CALLBACK_RATE)
    )
    act_mult = (
        float(trailing_activation_multiplier)
        if trailing_activation_multiplier is not None
        else float(TRAILING_ACTIVATION_MULTIPLIER)
    )
    if cb_rate <= 0:
        cb_rate = float(TRAILING_CALLBACK_RATE)
    if act_mult <= 0:
        act_mult = float(TRAILING_ACTIVATION_MULTIPLIER)

    atr_use = float(risk_atr_abs) if risk_atr_abs is not None and float(risk_atr_abs) > 0 else None
    pe = float(preco_entrada)
    tp_ref_audit: float | None = None

    if sl_break_even:
        tp_raw = float(preco_entrada)
        sl_raw = _stop_price_break_even_exato(
            exchange,
            simbolo,
            float(preco_entrada),
            direcao="LONG",
        )
        sl_frac = 0.0
        act_txt = "break-even (entrada)"
        if atr_use is not None:
            cb_rate = _callback_rate_pct_from_atr(atr_use, pe)
    elif atr_use is not None:
        sl_raw = pe - float(RISK_ATR_SL_MULT) * atr_use
        tp_raw = pe + float(RISK_ATR_TRAIL_ACTIV_MULT) * atr_use
        tp_ref_audit = pe + float(RISK_ATR_TP_REF_MULT) * atr_use
        cb_rate = _callback_rate_pct_from_atr(atr_use, pe)
        sl_frac = max(0.0, (pe - sl_raw) / max(pe, 1e-12))
        act_txt = (
            f"+{RISK_ATR_TRAIL_ACTIV_MULT:g}×ATR (ativa trailing); "
            f"TP_ref_audit +{RISK_ATR_TP_REF_MULT:g}×ATR"
        )
    else:
        tp_raw = preco_entrada * (1.0 + (LONG_TP * act_mult))  # activationPrice do trailing
        sl_frac = _sl_frac_from_trailing_callback_pct(cb_rate)
        sl_raw = preco_entrada * (1.0 - sl_frac)
        act_txt = f"+{(LONG_TP * act_mult):.1%}"
    tp_p = float(exchange.price_to_precision(simbolo, tp_raw))
    sl_p = float(exchange.price_to_precision(simbolo, sl_raw))

    p_ro = _params_reduce_futures()
    ord_tp_partial: dict[str, Any] = {}
    q_work = q

    free_runner_enabled = (
        (not sl_break_even)
        and atr_use is not None
        and FREE_RUNNER_ATR_ENABLED
        and tp_ref_audit is not None
    )
    q_tp: float | None = None
    tp_market_p: float | None = None
    tp_reuse_logged = False
    if free_runner_enabled:
        ex_tp = _buscar_take_profit_parcial_aberto_any(exchange, simbolo, "LONG")
        reuse_tp = False
        if ex_tp is not None:
            q_tp_re = float(_quantidade_ordem_aberta_ccxt(exchange, simbolo, ex_tp))
            q_runner_re = float(exchange.amount_to_precision(simbolo, max(0.0, q - q_tp_re)))
            mkt = exchange.market(simbolo)
            min_amt = float(((mkt.get("limits") or {}).get("amount") or {}).get("min") or 0.0)
            ok_sz = q_tp_re > 0 and q_runner_re > 0
            if ok_sz and min_amt > 0:
                if q_tp_re + 1e-12 < min_amt or q_runner_re + 1e-12 < min_amt:
                    ok_sz = False
            if ok_sz:
                q_tp, q_runner, split_ok = q_tp_re, q_runner_re, True
                reuse_tp = True
            else:
                q_tp, q_runner, split_ok = _free_runner_split_qty(
                    exchange, simbolo, q, float(AURIC_PARTIAL_TP_PCT)
                )
        else:
            q_tp, q_runner, split_ok = _free_runner_split_qty(
                exchange, simbolo, q, float(AURIC_PARTIAL_TP_PCT)
            )
        if split_ok:
            q_work = float(exchange.amount_to_precision(simbolo, q_runner))
            tp_market_p = float(exchange.price_to_precision(simbolo, float(tp_ref_audit)))
            if reuse_tp:
                ord_tp_partial = ex_tp
                tp_reuse_logged = True
                print(
                    f"{_TAG} [FREE RUNNER] TP parcial já na bolsa id={ord_tp_partial.get('id')} "
                    f"qty≈{q_tp:g} — sem duplicar; trailing+SL só no runner {q_work:g}.",
                    flush=True,
                )
    ord_trailing: dict[str, Any] = {}
    ord_sl: dict[str, Any] = {}
    prot_now = check_existing_protection(exchange, simbolo, "LONG")
    if prot_now["all_three"]:
        print(
            f"{_TAG} [ANTI-SPAM] {simbolo}: SL+TP+Trailing já existem. Pausando 60s sem criar.",
            flush=True,
        )
        time.sleep(60.0)
        return (
            dict(prot_now.get("trailing_order") or {}),
            dict(prot_now.get("sl_order") or {}),
            dict(prot_now.get("tp_order") or {}),
        )
    try:
        prot_sl = check_existing_protection(exchange, simbolo, "LONG")
        if not _exigir_intervalo_min_entre_ordens_protecao(context="bracket_LONG STOP_MARKET"):
            print(f"{_TAG} [PROTECT] STOP_MARKET LONG não criado: throttle anti-spam.", flush=True)
        elif not _assert_seguranca_antes_create_ordem_protecao(
            exchange, simbolo, "LONG", context="bracket_LONG STOP_MARKET"
        ):
            print(
                f"{_TAG} [PROTECT] STOP_MARKET LONG não criado: segurança pré-create bloqueou.",
                flush=True,
            )
        elif bool(prot_sl.get("sl_exists")):
            print("[SKIP] Ordem de SL já existe na Binance.", flush=True)
            pass
        else:
            enforce_single_order_type(exchange, simbolo, "STOP_MARKET")
            ord_sl = exchange.create_order(
                simbolo,
                "STOP_MARKET",
                "sell",
                q_work,
                None,
                {
                    **p_ro,
                    "stopPrice": sl_p,
                    "clientOrderId": _client_order_id_protecao("SL", simbolo, "LONG"),
                },
            )
            _marcar_criacao_ordem_protecao()
            _sleep_cadencia_ordens_protecao()
    except Exception as e_sl:  # noqa: BLE001
        print(f"{_TAG} [PROTECT] Falha ao criar STOP_MARKET LONG: {e_sl}", file=sys.stderr, flush=True)

    try:
        if free_runner_enabled and q_tp is not None and tp_market_p is not None:
            prot_tp = check_existing_protection(exchange, simbolo, "LONG")
            if tp_reuse_logged:
                pass
            elif not _exigir_intervalo_min_entre_ordens_protecao(context="bracket_LONG TP_PARTIAL"):
                print(
                    f"{_TAG} [FREE RUNNER] TP parcial LONG não criado: throttle anti-spam.",
                    flush=True,
                )
            elif not _assert_seguranca_antes_create_ordem_protecao(
                exchange, simbolo, "LONG", context="bracket_LONG TP_PARTIAL"
            ):
                print(
                    f"{_TAG} [FREE RUNNER] TP parcial LONG não criado: segurança pré-create bloqueou.",
                    flush=True,
                )
            elif bool(prot_tp.get("tp_exists")):
                print("[SKIP] Ordem de TP já existe na Binance.", flush=True)
                pass
            else:
                enforce_single_order_type(exchange, simbolo, "TAKE_PROFIT_MARKET")
                ord_tp_partial = exchange.create_order(
                    simbolo,
                    "TAKE_PROFIT_MARKET",
                    "sell",
                    float(exchange.amount_to_precision(simbolo, q_tp)),
                    None,
                    {
                        **p_ro,
                        "stopPrice": tp_market_p,
                        "clientOrderId": _client_order_id_protecao("TP", simbolo, "LONG"),
                    },
                )
                _marcar_criacao_ordem_protecao()
                _sleep_cadencia_ordens_protecao()
                print(
                    f"🎯 [FREE RUNNER] TP Parcial ({AURIC_PARTIAL_TP_PCT:.0%}) armado em {tp_market_p}.",
                    flush=True,
                )
            print(
                f"{_TAG} [FREE RUNNER] LONG qty total={q:g} → TP={q_tp:g} + runner={q_work:g} | "
                f"TAKE_PROFIT_MARKET id={ord_tp_partial.get('id')}",
                flush=True,
            )
    except Exception as e_tp:  # noqa: BLE001
        print(
            f"{_TAG} [FREE RUNNER] Falha ao criar TP parcial LONG: {e_tp} "
            "(seguindo sem interromper proteção).",
            file=sys.stderr,
            flush=True,
        )
    try:
        prot_ts = check_existing_protection(exchange, simbolo, "LONG")
        if not _exigir_intervalo_min_entre_ordens_protecao(context="bracket_LONG TRAILING"):
            print(f"{_TAG} [PROTECT] Trailing LONG não criado: throttle anti-spam.", flush=True)
        elif not _assert_seguranca_antes_create_ordem_protecao(
            exchange, simbolo, "LONG", context="bracket_LONG TRAILING"
        ):
            print(
                f"{_TAG} [PROTECT] Trailing LONG não criado: segurança pré-create bloqueou.",
                flush=True,
            )
        elif bool(prot_ts.get("trailing_exists")):
            print("[SKIP] Ordem de Trailing já existe na Binance.", flush=True)
            pass
        else:
            enforce_single_order_type(exchange, simbolo, "TRAILING_STOP_MARKET")
            ord_trailing = exchange.create_order(
                simbolo,
                "TRAILING_STOP_MARKET",
                "sell",
                q_work,
                None,
                {
                    **p_ro,
                    "activationPrice": tp_p,
                    "callbackRate": cb_rate,
                    "clientOrderId": _client_order_id_protecao("TS", simbolo, "LONG"),
                },
            )
            _marcar_criacao_ordem_protecao()
            _sleep_cadencia_ordens_protecao()
    except Exception as e_tr:  # noqa: BLE001
        print(
            f"{_TAG} [PROTECT] Falha ao criar TRAILING LONG: {e_tr} (seguindo sem interromper).",
            file=sys.stderr,
            flush=True,
        )
    if sl_break_even:
        print(
            f"{_TAG} Bracket LONG: SL STOP_MARKET sell @ {sl_p} (BREAK-EVEN entrada) + "
            f"TRAILING_STOP_MARKET sell (activation @ {tp_p} / {act_txt}, "
            f"callbackRate={cb_rate:.1f}%), qty={q_work}"
        )
        if atr_use is not None:
            print(
                f"{_TAG} [RISK-ATR] LONG break-even+trail: ATR14≈{atr_use:.6f} | "
                f"trail_cb%≈1×ATR/px → {cb_rate:.2f}% (auditoria respiro trailing).",
                flush=True,
            )
    elif atr_use is not None:
        tpref = (
            float(exchange.price_to_precision(simbolo, float(tp_ref_audit)))
            if tp_ref_audit is not None
            else None
        )
        rr = RISK_ATR_TP_REF_MULT / max(RISK_ATR_SL_MULT, 1e-9)
        print(
            f"{_TAG} [RISK-ATR] LONG brackets: ATR14={atr_use:.6f} (tf={AURIC_ATR_TIMEFRAME}, n={AURIC_ATR_PERIOD}) | "
            f"SL_dist={RISK_ATR_SL_MULT:g}×ATR={RISK_ATR_SL_MULT * atr_use:.6f} → SL@{sl_p} | "
            f"ativação_trail={RISK_ATR_TRAIL_ACTIV_MULT:g}×ATR @ {tp_p} | "
            f"TP_ref_audit {RISK_ATR_TP_REF_MULT:g}×ATR → {tpref if tpref is not None else 'N/A'} | "
            f"R/R_ref≈1:{rr:.2f} (TP_ref/SL_dist em ATR) | "
            f"callbackRate={cb_rate:.2f}% (~1×ATR recuo desde máximo) | qty_runner={q_work}",
            flush=True,
        )
    else:
        print(
            f"{_TAG} Bracket LONG: SL STOP_MARKET sell @ {sl_p} (−{sl_frac:.3%} = "
            f"{SL_DISTANCE_VS_TRAILING_MULT:g}× trailing {cb_rate:.3f}%) + "
            f"TRAILING_STOP_MARKET sell (activation @ {tp_p} / {act_txt}, "
            f"callbackRate={cb_rate:.1f}%), qty={q_work}"
        )
    _enforce_max_tres_condicionais(exchange, simbolo, "LONG")
    _log_protection_counts(exchange, simbolo, "LONG", context="bracket_LONG(post)")
    return ord_trailing, ord_sl, ord_tp_partial


def _criar_bracket_short(
    exchange: ccxt.binance,
    simbolo: str,
    qty: float,
    preco_entrada: float,
    *,
    trailing_callback_rate: float | None = None,
    trailing_activation_multiplier: float | None = None,
    sl_break_even: bool = False,
    risk_atr_abs: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """
    Bracket SHORT na Binance Futures linear:
    - STOP_MARKET (SL inicial = SL_DISTANCE_VS_TRAILING_MULT × trailing %, ou break-even)
    - TRAILING_STOP_MARKET (take-profit dinâmico; ativa no antigo gatilho de TP)

    Modo ATR (`risk_atr_abs`): espelho do LONG (SL acima, ativação trailing abaixo da entrada).
    Free Runner: TAKE_PROFIT_MARKET (buy) na fração parcial ao preço entrada − 2,5×ATR.

    Devolve (ord_trailing, ord_sl, ord_tp_parcial).
    """
    global _in_protection_order_batch
    cancelar_livro_aberto_ate_zero_sync(
        exchange, simbolo, context="bracket_SHORT(pré)"
    )
    _in_protection_order_batch = True
    try:
        return _criar_bracket_short_impl(
            exchange,
            simbolo,
            qty,
            preco_entrada,
            trailing_callback_rate=trailing_callback_rate,
            trailing_activation_multiplier=trailing_activation_multiplier,
            sl_break_even=sl_break_even,
            risk_atr_abs=risk_atr_abs,
        )
    finally:
        _in_protection_order_batch = False


def _criar_bracket_short_impl(
    exchange: ccxt.binance,
    simbolo: str,
    qty: float,
    preco_entrada: float,
    *,
    trailing_callback_rate: float | None = None,
    trailing_activation_multiplier: float | None = None,
    sl_break_even: bool = False,
    risk_atr_abs: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not _sync_posicao_antes_loop_protecao(
        exchange,
        simbolo,
        "SHORT",
        context="bracket_SHORT",
    ):
        return {}, {}, {}
    _log_protection_counts(exchange, simbolo, "SHORT", context="bracket_SHORT(pre)")
    _enforce_max_tres_condicionais(exchange, simbolo, "SHORT")
    q = float(exchange.amount_to_precision(simbolo, qty))
    cb_rate = (
        float(trailing_callback_rate)
        if trailing_callback_rate is not None
        else float(TRAILING_CALLBACK_RATE)
    )
    act_mult = (
        float(trailing_activation_multiplier)
        if trailing_activation_multiplier is not None
        else float(TRAILING_ACTIVATION_MULTIPLIER)
    )
    if cb_rate <= 0:
        cb_rate = float(TRAILING_CALLBACK_RATE)
    if act_mult <= 0:
        act_mult = float(TRAILING_ACTIVATION_MULTIPLIER)

    atr_use = float(risk_atr_abs) if risk_atr_abs is not None and float(risk_atr_abs) > 0 else None
    pe = float(preco_entrada)
    tp_ref_audit: float | None = None

    if sl_break_even:
        tp_raw = float(preco_entrada)
        sl_raw = _stop_price_break_even_exato(
            exchange,
            simbolo,
            float(preco_entrada),
            direcao="SHORT",
        )
        sl_frac = 0.0
        act_txt = "break-even (entrada)"
        if atr_use is not None:
            cb_rate = _callback_rate_pct_from_atr(atr_use, pe)
    elif atr_use is not None:
        sl_raw = pe + float(RISK_ATR_SL_MULT) * atr_use
        tp_raw = pe - float(RISK_ATR_TRAIL_ACTIV_MULT) * atr_use
        tp_ref_audit = pe - float(RISK_ATR_TP_REF_MULT) * atr_use
        cb_rate = _callback_rate_pct_from_atr(atr_use, pe)
        sl_frac = max(0.0, (sl_raw - pe) / max(pe, 1e-12))
        act_txt = (
            f"−{RISK_ATR_TRAIL_ACTIV_MULT:g}×ATR (ativa trailing); "
            f"TP_ref_audit −{RISK_ATR_TP_REF_MULT:g}×ATR"
        )
    else:
        tp_raw = preco_entrada * (1.0 - (SHORT_TP * act_mult))  # activationPrice do trailing
        sl_frac = _sl_frac_from_trailing_callback_pct(cb_rate)
        sl_raw = preco_entrada * (1.0 + sl_frac)
        act_txt = f"−{(SHORT_TP * act_mult):.1%}"
    tp_p = float(exchange.price_to_precision(simbolo, tp_raw))
    sl_p = float(exchange.price_to_precision(simbolo, sl_raw))

    p_ro = _params_reduce_futures()
    ord_tp_partial: dict[str, Any] = {}
    q_work = q

    free_runner_enabled = (
        (not sl_break_even)
        and atr_use is not None
        and FREE_RUNNER_ATR_ENABLED
        and tp_ref_audit is not None
    )
    q_tp: float | None = None
    tp_market_p: float | None = None
    tp_reuse_logged = False
    if free_runner_enabled:
        ex_tp = _buscar_take_profit_parcial_aberto_any(exchange, simbolo, "SHORT")
        reuse_tp = False
        if ex_tp is not None:
            q_tp_re = float(_quantidade_ordem_aberta_ccxt(exchange, simbolo, ex_tp))
            q_runner_re = float(exchange.amount_to_precision(simbolo, max(0.0, q - q_tp_re)))
            mkt = exchange.market(simbolo)
            min_amt = float(((mkt.get("limits") or {}).get("amount") or {}).get("min") or 0.0)
            ok_sz = q_tp_re > 0 and q_runner_re > 0
            if ok_sz and min_amt > 0:
                if q_tp_re + 1e-12 < min_amt or q_runner_re + 1e-12 < min_amt:
                    ok_sz = False
            if ok_sz:
                q_tp, q_runner, split_ok = q_tp_re, q_runner_re, True
                reuse_tp = True
            else:
                q_tp, q_runner, split_ok = _free_runner_split_qty(
                    exchange, simbolo, q, float(AURIC_PARTIAL_TP_PCT)
                )
        else:
            q_tp, q_runner, split_ok = _free_runner_split_qty(
                exchange, simbolo, q, float(AURIC_PARTIAL_TP_PCT)
            )
        if split_ok:
            q_work = float(exchange.amount_to_precision(simbolo, q_runner))
            tp_market_p = float(exchange.price_to_precision(simbolo, float(tp_ref_audit)))
            if reuse_tp:
                ord_tp_partial = ex_tp
                tp_reuse_logged = True
                print(
                    f"{_TAG} [FREE RUNNER] TP parcial já na bolsa id={ord_tp_partial.get('id')} "
                    f"qty≈{q_tp:g} — sem duplicar; trailing+SL só no runner {q_work:g}.",
                    flush=True,
                )
    ord_trailing: dict[str, Any] = {}
    ord_sl: dict[str, Any] = {}
    prot_now = check_existing_protection(exchange, simbolo, "SHORT")
    if prot_now["all_three"]:
        print(
            f"{_TAG} [ANTI-SPAM] {simbolo}: SL+TP+Trailing já existem. Pausando 60s sem criar.",
            flush=True,
        )
        time.sleep(60.0)
        return (
            dict(prot_now.get("trailing_order") or {}),
            dict(prot_now.get("sl_order") or {}),
            dict(prot_now.get("tp_order") or {}),
        )
    prot_sl = check_existing_protection(exchange, simbolo, "SHORT")
    sl_floor = max(float(preco_entrada), float(prot_sl.get("price_now") or 0.0))
    if float(sl_p) <= sl_floor:
        tick = max(float(_obter_tick_size(exchange, simbolo)), 1e-8)
        sl_fix = float(exchange.price_to_precision(simbolo, float(sl_floor) + tick))
        print(
            f"{_TAG} [PROTECT] Ajuste crítico SHORT: SL {sl_p} <= max(entrada={preco_entrada}, mercado={sl_floor}); "
            f"corrigido para {sl_fix} (SL deve ficar acima da entrada e do preço atual).",
            file=sys.stderr,
            flush=True,
        )
        sl_p = sl_fix
    try:
        if not _exigir_intervalo_min_entre_ordens_protecao(context="bracket_SHORT STOP_MARKET"):
            print(f"{_TAG} [PROTECT] STOP_MARKET SHORT não criado: throttle anti-spam.", flush=True)
        elif not _assert_seguranca_antes_create_ordem_protecao(
            exchange, simbolo, "SHORT", context="bracket_SHORT STOP_MARKET"
        ):
            print(
                f"{_TAG} [PROTECT] STOP_MARKET SHORT não criado: segurança pré-create bloqueou.",
                flush=True,
            )
        elif bool(prot_sl.get("sl_exists")):
            print("[SKIP] Ordem de SL já existe na Binance.", flush=True)
            pass
        else:
            enforce_single_order_type(exchange, simbolo, "STOP_MARKET")
            ord_sl = exchange.create_order(
                simbolo,
                "STOP_MARKET",
                "buy",
                q_work,
                None,
                {
                    **p_ro,
                    "stopPrice": sl_p,
                    "clientOrderId": _client_order_id_protecao("SL", simbolo, "SHORT"),
                },
            )
            _marcar_criacao_ordem_protecao()
            _sleep_cadencia_ordens_protecao()
    except Exception as e_sl:  # noqa: BLE001
        print(f"{_TAG} [PROTECT] Falha ao criar STOP_MARKET SHORT: {e_sl}", file=sys.stderr, flush=True)

    try:
        if free_runner_enabled and q_tp is not None and tp_market_p is not None:
            prot_tp = check_existing_protection(exchange, simbolo, "SHORT")
            if tp_reuse_logged:
                pass
            elif not _exigir_intervalo_min_entre_ordens_protecao(context="bracket_SHORT TP_PARTIAL"):
                print(
                    f"{_TAG} [FREE RUNNER] TP parcial SHORT não criado: throttle anti-spam.",
                    flush=True,
                )
            elif not _assert_seguranca_antes_create_ordem_protecao(
                exchange, simbolo, "SHORT", context="bracket_SHORT TP_PARTIAL"
            ):
                print(
                    f"{_TAG} [FREE RUNNER] TP parcial SHORT não criado: segurança pré-create bloqueou.",
                    flush=True,
                )
            elif bool(prot_tp.get("tp_exists")):
                print("[SKIP] Ordem de TP já existe na Binance.", flush=True)
                pass
            else:
                enforce_single_order_type(exchange, simbolo, "TAKE_PROFIT_MARKET")
                ord_tp_partial = exchange.create_order(
                    simbolo,
                    "TAKE_PROFIT_MARKET",
                    "buy",
                    float(exchange.amount_to_precision(simbolo, q_tp)),
                    None,
                    {
                        **p_ro,
                        "stopPrice": tp_market_p,
                        "clientOrderId": _client_order_id_protecao("TP", simbolo, "SHORT"),
                    },
                )
                _marcar_criacao_ordem_protecao()
                _sleep_cadencia_ordens_protecao()
                print(
                    f"🎯 [FREE RUNNER] TP Parcial ({AURIC_PARTIAL_TP_PCT:.0%}) armado em {tp_market_p}.",
                    flush=True,
                )
            print(
                f"{_TAG} [FREE RUNNER] SHORT qty total={q:g} → TP={q_tp:g} + runner={q_work:g} | "
                f"TAKE_PROFIT_MARKET id={ord_tp_partial.get('id')}",
                flush=True,
            )
    except Exception as e_tp:  # noqa: BLE001
        print(
            f"{_TAG} [FREE RUNNER] Falha ao criar TP parcial SHORT: {e_tp} "
            "(seguindo sem interromper proteção).",
            file=sys.stderr,
            flush=True,
        )
    try:
        prot_ts = check_existing_protection(exchange, simbolo, "SHORT")
        if not _exigir_intervalo_min_entre_ordens_protecao(context="bracket_SHORT TRAILING"):
            print(f"{_TAG} [PROTECT] Trailing SHORT não criado: throttle anti-spam.", flush=True)
        elif not _assert_seguranca_antes_create_ordem_protecao(
            exchange, simbolo, "SHORT", context="bracket_SHORT TRAILING"
        ):
            print(
                f"{_TAG} [PROTECT] Trailing SHORT não criado: segurança pré-create bloqueou.",
                flush=True,
            )
        elif bool(prot_ts.get("trailing_exists")):
            print("[SKIP] Ordem de Trailing já existe na Binance.", flush=True)
            pass
        else:
            enforce_single_order_type(exchange, simbolo, "TRAILING_STOP_MARKET")
            ord_trailing = exchange.create_order(
                simbolo,
                "TRAILING_STOP_MARKET",
                "buy",
                q_work,
                None,
                {
                    **p_ro,
                    "activationPrice": tp_p,
                    "callbackRate": cb_rate,
                    "clientOrderId": _client_order_id_protecao("TS", simbolo, "SHORT"),
                },
            )
            _marcar_criacao_ordem_protecao()
            _sleep_cadencia_ordens_protecao()
    except Exception as e_tr:  # noqa: BLE001
        print(
            f"{_TAG} [PROTECT] Falha ao criar TRAILING SHORT: {e_tr} (seguindo sem interromper).",
            file=sys.stderr,
            flush=True,
        )
    if sl_break_even:
        print(
            f"{_TAG} Bracket SHORT: SL STOP_MARKET buy @ {sl_p} (BREAK-EVEN entrada) + "
            f"TRAILING_STOP_MARKET buy (activation @ {tp_p} / {act_txt}, "
            f"callbackRate={cb_rate:.1f}%), qty={q_work}"
        )
        if atr_use is not None:
            print(
                f"{_TAG} [RISK-ATR] SHORT break-even+trail: ATR14≈{atr_use:.6f} | "
                f"trail_cb%≈1×ATR/px → {cb_rate:.2f}% (auditoria respiro trailing).",
                flush=True,
            )
    elif atr_use is not None:
        tpref = (
            float(exchange.price_to_precision(simbolo, float(tp_ref_audit)))
            if tp_ref_audit is not None
            else None
        )
        rr = RISK_ATR_TP_REF_MULT / max(RISK_ATR_SL_MULT, 1e-9)
        print(
            f"{_TAG} [RISK-ATR] SHORT brackets: ATR14={atr_use:.6f} (tf={AURIC_ATR_TIMEFRAME}, n={AURIC_ATR_PERIOD}) | "
            f"SL_dist={RISK_ATR_SL_MULT:g}×ATR={RISK_ATR_SL_MULT * atr_use:.6f} → SL@{sl_p} | "
            f"ativação_trail={RISK_ATR_TRAIL_ACTIV_MULT:g}×ATR @ {tp_p} | "
            f"TP_ref_audit {RISK_ATR_TP_REF_MULT:g}×ATR → {tpref if tpref is not None else 'N/A'} | "
            f"R/R_ref≈1:{rr:.2f} (TP_ref/SL_dist em ATR) | "
            f"callbackRate={cb_rate:.2f}% (~1×ATR recuo desde mínimo) | qty_runner={q_work}",
            flush=True,
        )
    else:
        print(
            f"{_TAG} Bracket SHORT: SL STOP_MARKET buy @ {sl_p} (+{sl_frac:.3%} = "
            f"{SL_DISTANCE_VS_TRAILING_MULT:g}× trailing {cb_rate:.3f}%) + "
            f"TRAILING_STOP_MARKET buy (activation @ {tp_p} / {act_txt}, "
            f"callbackRate={cb_rate:.1f}%), qty={q_work}"
        )
    _enforce_max_tres_condicionais(exchange, simbolo, "SHORT")
    _log_protection_counts(exchange, simbolo, "SHORT", context="bracket_SHORT(post)")
    return ord_trailing, ord_sl, ord_tp_partial


def _quote_de_balance_unificado(bal: dict[str, Any]) -> float:
    """
    Extrai saldo da moeda de cotação do dict devolvido por `fetch_balance`: preferência `free`,
    senão `total` (saldo líquido na conta de futuros).
    """
    quote = bal.get(QUOTE_ASSET)
    if quote is None:
        return 0.0
    if isinstance(quote, dict):
        livre = quote.get("free")
        total = quote.get("total")
        if livre is not None:
            return float(livre)
        if total is not None:
            return float(total)
        return 0.0
    try:
        return float(quote)
    except (TypeError, ValueError):
        return 0.0


def obter_saldo_usdt_margem(exchange: ccxt.binance | None = None) -> float:
    """Saldo de cotação (USDC) na carteira de Futuros — `fetch_balance` com `type: future`."""
    ex = exchange or criar_exchange_binance()
    try:
        bal = ex.fetch_balance(params={"type": "future"})
        return _quote_de_balance_unificado(bal)
    except ccxt.BaseError as e:
        print(f"{_TAG} Erro ao obter saldo USDC (futures): {e}", file=sys.stderr)
        raise


def abrir_long_market(
    simbolo: str,
    exchange: ccxt.binance | None = None,
    *,
    alavancagem: float | None = None,
    notional_usdt_override: float | None = None,
    risk_fraction: float | None = None,
    trailing_callback_rate: float | None = None,
    trailing_activation_multiplier: float | None = None,
    force_reference_price: float | None = None,
    is_manual_force: bool = False,
    turbo_chase: bool = False,
) -> dict[str, Any]:
    """
    Abre **long** com LIMIT **IOC** + **chase**: preço = bid × (1 + offset 0,05%), `price_to_precision`;
    após ~CHASE_ENTRADA_TIMEOUT_S sem fill suficiente, reabre no novo livro (sem GTC pendurado).
    Notional = risk_fraction×saldo_margem×alav (fallback PERCENTUAL_BANCA); depois bracket (SL + trailing).
    """
    lock_ctx = _adquirir_lock_entrada(simbolo)
    if lock_ctx == ("", ""):
        key = _order_lock_key(simbolo)
        print(
            f"{_TAG} [LOCK] Entrada LONG ignorada: lock ativo em `{key}` (TTL {ORDER_LOCK_TTL_S:.0f}s).",
            flush=True,
        )
        return _ordem_skip_lock(simbolo)
    try:
        ex = exchange or criar_exchange_binance()
        sym = _resolver_simbolo_perp(ex, simbolo)
        snap_pre = consultar_posicao_futures(simbolo, ex)
        if bool(snap_pre.get("posicao_aberta")):
            qty_pre = abs(float(snap_pre.get("contratos") or 0.0))
            side_pre = str(snap_pre.get("direcao_posicao") or "LONG").upper()
            raise RuntimeError(
                f"{_TAG} Entrada LONG abortada: já existe posição aberta ({side_pre}, qty={qty_pre:g}) em {sym}."
            )
        lev = float(alavancagem) if alavancagem is not None else float(ALAVANCAGEM_REF_LOG_PADRAO)
        if notional_usdt_override is not None:
            quantidade_usd = float(notional_usdt_override)
            rf_used = None
        else:
            quantidade_usd = notional_usdt_futuros_position_sizing(
                ex, lev, risk_fraction=risk_fraction
            )
            rf_used = float(risk_fraction) if risk_fraction is not None else float(PERCENTUAL_BANCA)
            if rf_used <= 0:
                rf_used = float(PERCENTUAL_BANCA)
        if quantidade_usd <= 0:
            raise ValueError(
                f"{_TAG} Notional inválido ({quantidade_usd:.4f} USDC). Verifique saldo em margem."
            )
        amt = _quantidade_base_a_partir_de_usd(ex, sym, quantidade_usd)
        saldo_m = obter_saldo_usdt_margem(ex)
        risk_txt = (
            f"{rf_used * 100:.1f}% margem USDC"
            if rf_used is not None
            else "notional override (risk N/A)"
        )

        print(
            f"{_TAG} Abrir LONG {sym} — notional ~{quantidade_usd:.2f} USDC "
            f"({risk_txt} × {lev:g}x lev; saldo_margem≈{saldo_m:.4f}; qty base ≈ {amt}); "
            "execução MARKET (set-and-forget)."
        )
        ordem = ex.create_order(sym, "market", "buy", amt, None, {})
        print(f"{_TAG} Long aceito. id={ordem.get('id')} status={ordem.get('status')}")
        qty_pos, preco_ent = _aguardar_qty_e_preco_entrada(ex, sym)
        time.sleep(2.0)
        _log_liquidacao_estimada(preco_ent, "LONG", lev)
        print("🧹 [ORDENS] Limpando stops antigos antes de atualizar...", flush=True)
        cancelar_todas_ordens_futures_nativo(simbolo, ex)
        atr_abs = calcular_atr_absoluto(ex, sym) if RISK_ATR_MODE else None
        if RISK_ATR_MODE and atr_abs is None:
            print(f"{_TAG} [RISK-ATR] LONG: ATR indisponível — brackets em percentagem (legado).", flush=True)
        ord_trailing, ord_sl, ord_tp_part = _criar_bracket_long(
            ex,
            sym,
            qty_pos,
            preco_ent,
            trailing_callback_rate=trailing_callback_rate,
            trailing_activation_multiplier=trailing_activation_multiplier,
            risk_atr_abs=atr_abs,
        )
        out = dict(ordem)
        out["auric_take_profit"] = ord_trailing  # compat legado
        out["auric_trailing_stop"] = ord_trailing
        out["auric_stop_loss"] = ord_sl
        out["auric_tp_partial"] = ord_tp_part
        out["auric_entry_qty"] = qty_pos
        out["auric_entry_price"] = preco_ent
        if atr_abs is not None:
            out["auric_atr_14"] = float(atr_abs)
            out["auric_atr_timeframe"] = AURIC_ATR_TIMEFRAME
        if ord_tp_part.get("id"):
            out["auric_free_runner"] = True
            armar_free_runner_tracking(simbolo, ex, float(qty_pos), float(preco_ent))
        print(
            f"{_TAG} [ENTRY-SHIELD] Posição aberta. SL, TP e Trailing Stop criados com sucesso. "
            "O bot não recriará estas ordens.",
            flush=True,
        )
        return out
    except Exception as e:
        print(f"{_TAG} ❌ [BINANCE-ERROR] {e}", file=sys.stderr)
        print(f"{_TAG} Erro ao abrir long / bracket: {e}", file=sys.stderr)
        raise
    finally:
        _liberar_lock_entrada(lock_ctx)


def abrir_short_market(
    simbolo: str,
    exchange: ccxt.binance | None = None,
    *,
    alavancagem: float | None = None,
    notional_usdt_override: float | None = None,
    risk_fraction: float | None = None,
    trailing_callback_rate: float | None = None,
    trailing_activation_multiplier: float | None = None,
    force_reference_price: float | None = None,
    is_manual_force: bool = False,
    turbo_chase: bool = False,
) -> dict[str, Any]:
    """
    Abre **short** com LIMIT IOC + chase: preço = ask × (1 − offset 0,05%), `price_to_precision`;
    mesmo fluxo que o long. Depois **reduce-only** STOP_MARKET (SL) e TRAILING_STOP_MARKET.
    """
    lock_ctx = _adquirir_lock_entrada(simbolo)
    if lock_ctx == ("", ""):
        key = _order_lock_key(simbolo)
        print(
            f"{_TAG} [LOCK] Entrada SHORT ignorada: lock ativo em `{key}` (TTL {ORDER_LOCK_TTL_S:.0f}s).",
            flush=True,
        )
        return _ordem_skip_lock(simbolo)
    try:
        ex = exchange or criar_exchange_binance()
        sym = _resolver_simbolo_perp(ex, simbolo)
        snap_pre = consultar_posicao_futures(simbolo, ex)
        if bool(snap_pre.get("posicao_aberta")):
            qty_pre = abs(float(snap_pre.get("contratos") or 0.0))
            side_pre = str(snap_pre.get("direcao_posicao") or "LONG").upper()
            raise RuntimeError(
                f"{_TAG} Entrada SHORT abortada: já existe posição aberta ({side_pre}, qty={qty_pre:g}) em {sym}."
            )
        lev = float(alavancagem) if alavancagem is not None else float(ALAVANCAGEM_REF_LOG_PADRAO)
        if notional_usdt_override is not None:
            quantidade_usd = float(notional_usdt_override)
            rf_used = None
        else:
            quantidade_usd = notional_usdt_futuros_position_sizing(
                ex, lev, risk_fraction=risk_fraction
            )
            rf_used = float(risk_fraction) if risk_fraction is not None else float(PERCENTUAL_BANCA)
            if rf_used <= 0:
                rf_used = float(PERCENTUAL_BANCA)
        if quantidade_usd <= 0:
            raise ValueError(
                f"{_TAG} Notional inválido ({quantidade_usd:.4f} USDC). Verifique saldo em margem."
            )
        amt = _quantidade_base_a_partir_de_usd(ex, sym, quantidade_usd)
        saldo_m = obter_saldo_usdt_margem(ex)
        risk_txt = (
            f"{rf_used * 100:.1f}% margem USDC"
            if rf_used is not None
            else "notional override (risk N/A)"
        )

        print(
            f"{_TAG} Abrir SHORT {sym} — notional ~{quantidade_usd:.2f} USDC "
            f"({risk_txt} × {lev:g}x lev; saldo_margem≈{saldo_m:.4f}; qty base ≈ {amt}); "
            "execução MARKET (set-and-forget)."
        )
        ordem = ex.create_order(sym, "market", "sell", amt, None, {})
        print(f"{_TAG} Short aceito. id={ordem.get('id')} status={ordem.get('status')}")
        qty_pos, preco_ent = _aguardar_qty_e_preco_entrada(ex, sym)
        time.sleep(2.0)
        _log_liquidacao_estimada(preco_ent, "SHORT", lev)
        print("🧹 [ORDENS] Limpando stops antigos antes de atualizar...", flush=True)
        cancelar_todas_ordens_futures_nativo(simbolo, ex)
        atr_abs = calcular_atr_absoluto(ex, sym) if RISK_ATR_MODE else None
        if RISK_ATR_MODE and atr_abs is None:
            print(f"{_TAG} [RISK-ATR] SHORT: ATR indisponível — brackets em percentagem (legado).", flush=True)
        ord_trailing, ord_sl, ord_tp_part = _criar_bracket_short(
            ex,
            sym,
            qty_pos,
            preco_ent,
            trailing_callback_rate=trailing_callback_rate,
            trailing_activation_multiplier=trailing_activation_multiplier,
            risk_atr_abs=atr_abs,
        )
        out = dict(ordem)
        out["auric_take_profit"] = ord_trailing  # compat legado
        out["auric_trailing_stop"] = ord_trailing
        out["auric_stop_loss"] = ord_sl
        out["auric_tp_partial"] = ord_tp_part
        out["auric_entry_qty"] = qty_pos
        out["auric_entry_price"] = preco_ent
        if atr_abs is not None:
            out["auric_atr_14"] = float(atr_abs)
            out["auric_atr_timeframe"] = AURIC_ATR_TIMEFRAME
        if ord_tp_part.get("id"):
            out["auric_free_runner"] = True
            armar_free_runner_tracking(simbolo, ex, float(qty_pos), float(preco_ent))
        print(
            f"{_TAG} [ENTRY-SHIELD] Posição aberta. SL, TP e Trailing Stop criados com sucesso. "
            "O bot não recriará estas ordens.",
            flush=True,
        )
        return out
    except Exception as e:
        print(f"{_TAG} ❌ [BINANCE-ERROR] {e}", file=sys.stderr)
        print(f"{_TAG} Erro ao abrir short / bracket: {e}", file=sys.stderr)
        raise
    finally:
        _liberar_lock_entrada(lock_ctx)


def fechar_posicao_market(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> dict[str, Any]:
    """
    Cancela ordens abertas no símbolo (TP/SL órfãs) e fecha a posição com LIMIT **GTC**
    (bid/ask + offset 0,05%) + chase, reduce-only — sem ordens MARKET.
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)

    try:
        cancelar_todas_ordens_abertas(simbolo, ex)
    except ccxt.BaseError as e_can:
        print(f"{_TAG} Aviso: cancelamento pré-fecho: {e_can}", file=sys.stderr)

    posicoes = ex.fetch_positions([sym])
    alvo: dict[str, Any] | None = None
    for p in posicoes:
        c = float(p.get("contracts") or 0)
        if abs(c) > 0:
            alvo = p
            break

    if alvo is None:
        msg = f"{_TAG} Nenhuma posição aberta em {sym} para fechar."
        print(msg, file=sys.stderr)
        raise ccxt.InvalidOrder(msg)

    contratos = float(alvo.get("contracts") or 0)
    lado = str(alvo.get("side") or "").lower()
    qty = float(ex.amount_to_precision(sym, abs(contratos)))

    if qty <= 0:
        raise ccxt.InvalidOrder(f"{_TAG} Quantidade de fechamento inválida: {contratos}")

    # Long: vender (ref. ask × 0,9995); Short: comprar (ref. bid × 1,0005).
    if lado == "short" or contratos < 0:
        print(
            f"{_TAG} Fechando SHORT em {sym} — compra LIMIT+offset (chase) qty={qty}..."
        )
        ordem = _entrar_limite_com_chase_futuros(
            ex,
            sym,
            "buy",
            qty,
            nome_lado="FECHO_SHORT",
            max_rounds=CHASE_SHORT_MAX_ROUNDS,
            timeout_s=CHASE_SHORT_TIMEOUT_S,
            reduce_only=True,
        )
    else:
        print(
            f"{_TAG} Fechando LONG em {sym} — venda LIMIT+offset (chase) qty={qty}..."
        )
        ordem = _entrar_limite_com_chase_futuros(
            ex,
            sym,
            "sell",
            qty,
            nome_lado="FECHO_LONG",
            max_rounds=CHASE_ENTRADA_MAX_ROUNDS,
            timeout_s=CHASE_ENTRADA_TIMEOUT_S,
            reduce_only=True,
        )

    print(f"{_TAG} Posição encerrada. id={ordem.get('id')} status={ordem.get('status')}")
    try:
        print("🧹 [ORDENS] Posição fechada. Limpando todas as ordens órfãs...", flush=True)
        cancelar_todas_ordens_futures_nativo(simbolo, ex)
        cancelar_todas_ordens_abertas(simbolo, ex)
    except ccxt.BaseError as e2:
        print(f"{_TAG} Pós-fecho: cancelamento extra: {e2}", file=sys.stderr)
    return ordem


def preco_medio_execucao_ordem(ordem: dict[str, Any] | None, fallback: float) -> float:
    """Preço médio de execução (ccxt) ou `fallback` (ex.: último tick do ciclo)."""
    fb = float(fallback)
    if not ordem:
        return fb
    for k in ("average", "price"):
        v = ordem.get(k)
        try:
            fv = float(v)  # type: ignore[arg-type]
            if fv > 0:
                return fv
        except (TypeError, ValueError):
            continue
    info = ordem.get("info")
    if isinstance(info, dict):
        for k in ("avgPrice", "price", "averagePrice"):
            raw = info.get(k)
            try:
                fv = float(raw)  # type: ignore[arg-type]
                if fv > 0:
                    return fv
            except (TypeError, ValueError):
                continue
    return fb


def _roi_fechamento_percentual(direcao: str, preco_entrada: float, preco_saida: float) -> float:
    """ROI % sobre o preço de entrada (LONG: sobe = +; SHORT: desce = +)."""
    entry = float(preco_entrada)
    if entry <= 0:
        return 0.0
    px = float(preco_saida)
    d = str(direcao).strip().upper()
    if d == "SHORT":
        return (entry - px) / entry * 100.0
    if d == "LONG":
        return (px - entry) / entry * 100.0
    return 0.0


def registrar_trade_performance_fecho(
    simbolo: str,
    *,
    preco_entrada: float,
    preco_saida: float,
    direcao: str,
    exit_type: str,
) -> None:
    """Grava `final_roi` + `exit_type` + `alpha_attribution` na última linha `trades`."""
    try:
        import logger
        import json

        roi = _roi_fechamento_percentual(direcao, preco_entrada, preco_saida)
        motivo = str(exit_type)
        alpha_attr: dict[str, Any] = {
            "ml_confidence": 0.0,
            "whale_signal": "neutral",
            "indicator_primary": "RSI_Squeeze",
        }
        try:
            entrada_ctx = logger.obter_contexto_ultima_abertura(simbolo)
            p_ml_open = float(entrada_ctx.get("probabilidade_ml") or 0.0)
            raw_ctx = str(entrada_ctx.get("contexto_raw") or "")
            whale_score_open = 0.0
            whale_signal_open = ""
            if raw_ctx:
                try:
                    ctx_obj = json.loads(raw_ctx)
                    if isinstance(ctx_obj, dict):
                        whale_score_open = float(ctx_obj.get("whale_flow_score") or 0.0)
                        whale_signal_open = str(ctx_obj.get("whale_flow_signal") or "")
                except Exception:
                    whale_score_open = 0.0
            if float(roi) > 0 and p_ml_open >= 0.80:
                alpha_attr["ml_confidence"] = 0.9
                alpha_attr["indicator_primary"] = "XGBoost_Confidence"
            if (
                float(roi) > 0
                and str(direcao).upper() == "SHORT"
                and (whale_score_open < 0.0 or whale_signal_open == "USDT_LIQUIDITY_OUTFLOW")
            ):
                alpha_attr["whale_signal"] = "bearish_liquidity_outflow"
                alpha_attr["indicator_primary"] = "Whale_Flow"
            elif whale_score_open > 0:
                alpha_attr["whale_signal"] = "bullish"
            elif whale_score_open < 0:
                alpha_attr["whale_signal"] = "bearish"
            if alpha_attr["ml_confidence"] <= 0:
                alpha_attr["ml_confidence"] = max(0.0, min(1.0, p_ml_open))
        except Exception as e_attr:  # noqa: BLE001
            alpha_attr["indicator_primary"] = f"Fallback:{e_attr}"
        logger.atualizar_ultimo_trade_campos(
            simbolo,
            {
                "final_roi": float(roi),
                "exit_type": motivo,
                "alpha_attribution": alpha_attr,
            },
        )
        print(
            f"📝 [DB SYNC] Trade fechado gravado. Final ROI: {float(roi):+.2f}% | Motivo: {motivo}."
        )
    except Exception as e:  # noqa: BLE001
        print(f"{_TAG} ⚠️ registrar_trade_performance_fecho (Supabase): {e}")


def fechar_posicao_emergencia_market(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> dict[str, Any]:
    """
    Exceção de segurança: fecha posição com MARKET reduce-only.
    """
    ex = exchange or criar_exchange_binance()
    snap = consultar_posicao_futures(simbolo, ex)
    if not snap.get("posicao_aberta"):
        raise ccxt.InvalidOrder(f"{_TAG} Nenhuma posição aberta para fecho de emergência.")
    qty = abs(float(snap.get("contratos") or 0.0))
    if qty <= 0:
        raise ccxt.InvalidOrder(f"{_TAG} Quantidade inválida para fecho emergência: {qty}")
    side = "sell" if str(snap.get("direcao_posicao") or "LONG").upper() == "LONG" else "buy"
    sym = _resolver_simbolo_perp(ex, simbolo)
    d_em = str(snap.get("direcao_posicao") or "LONG").upper()
    _abortar_se_demasiadas_ordens_abertas(ex, simbolo, context="fechar_posicao_emergencia_market")
    _assert_posicao_aberta_para_protecao_sync(
        ex, simbolo, d_em, context="fechar_posicao_emergencia_market"
    )
    ordem = ex.create_order(sym, "market", side, qty, None, _params_reduce_futures())
    try:
        cancelar_todas_ordens_abertas(simbolo, ex)
    except Exception:
        pass
    return ordem


def fechar_parcial_posicao_market(
    simbolo: str,
    exchange: ccxt.binance | None = None,
    *,
    frac: float = 0.5,
    execution: str | None = None,
) -> dict[str, Any]:
    """
    Fecha uma fracção da posição (ex.: 50%) com **MARKET** reduce-only ou **LIMIT IOC**
    (preço agressivo no livro) conforme `execution` / env `AURIC_PARTIAL_TP_EXEC`.
    Não cancela ordens reduce-only existentes — o chamador deve recolocar brackets se necessário.
    """
    ex = exchange or criar_exchange_binance()
    snap = consultar_posicao_futures(simbolo, ex)
    if not snap.get("posicao_aberta"):
        raise ccxt.InvalidOrder(f"{_TAG} Nenhuma posição aberta para fecho parcial.")
    sym = _resolver_simbolo_perp(ex, simbolo)
    qty_full = abs(float(snap.get("contratos") or 0.0))
    frac_f = max(0.0, min(1.0, float(frac)))
    qty_close = qty_full * frac_f
    qty_close = float(ex.amount_to_precision(sym, qty_close))
    if qty_close <= 0:
        raise ccxt.InvalidOrder(f"{_TAG} Qty parcial inválida após precisão: {qty_close!r}")
    if qty_close >= qty_full * 0.99:
        raise ccxt.InvalidOrder(
            f"{_TAG} Fecho parcial ≈100% da posição (full={qty_full}, parcial={qty_close}); usar fecho total."
        )
    lado = str(snap.get("direcao_posicao") or "LONG").upper()
    side = "sell" if lado == "LONG" else "buy"
    _abortar_se_demasiadas_ordens_abertas(ex, simbolo, context="fechar_parcial_posicao_market")
    _assert_posicao_aberta_para_protecao_sync(
        ex, simbolo, lado, context="fechar_parcial_posicao_market"
    )
    mode = (execution or PARTIAL_TP_EXECUTION or "market").strip().lower()
    p_ro = _params_reduce_futures()
    if mode == "ioc":
        try:
            ob = ex.fetch_order_book(sym, limit=5)
            bids = (ob or {}).get("bids") or []
            asks = (ob or {}).get("asks") or []
            if lado == "LONG":
                ref = float(bids[0][0]) if bids else 0.0
                if ref <= 0:
                    t = ex.fetch_ticker(sym)
                    ref = float(t.get("bid") or t.get("last") or 0.0)
                px_raw = ref * 0.999
            else:
                ref = float(asks[0][0]) if asks else 0.0
                if ref <= 0:
                    t = ex.fetch_ticker(sym)
                    ref = float(t.get("ask") or t.get("last") or 0.0)
                px_raw = ref * 1.001
            if px_raw <= 0:
                raise ccxt.InvalidOrder(f"{_TAG} IOC parcial: preço de referência inválido.")
            price = float(ex.price_to_precision(sym, px_raw))
            return ex.create_order(
                sym,
                "limit",
                side,
                qty_close,
                price,
                {**p_ro, "timeInForce": "IOC"},
            )
        except Exception as e_ioc:  # noqa: BLE001
            print(f"{_TAG} IOC parcial falhou ({e_ioc}); fallback MARKET.", file=sys.stderr)
    return ex.create_order(sym, "market", side, qty_close, None, p_ro)


def executar_saida_hibrida_roi_break_even_trailing(
    simbolo: str,
    exchange: ccxt.binance,
    direcao: str,
    preco_entrada: float,
    *,
    close_frac: float = 0.5,
    trailing_callback_rate: float | None = None,
    trailing_activation_multiplier: float | None = None,
) -> dict[str, Any]:
    """
    Saída híbrida (vigia): fecho **MARKET** de `close_frac` da posição; em seguida recoloca
    brackets só sobre o restante — SL em **break-even** (preço de entrada) e **TRAILING_STOP_MARKET**
    com `callbackRate` (ex.: 0,6 %) na metade restante.
    """
    d = str(direcao).strip().upper()
    if d not in ("LONG", "SHORT"):
        raise ValueError(f"{_TAG} direcao inválida para saída híbrida: {d!r}")
    pe = float(preco_entrada)
    if pe <= 0:
        raise ValueError(f"{_TAG} preco_entrada inválido para saída híbrida: {preco_entrada!r}")

    cb = (
        float(trailing_callback_rate)
        if trailing_callback_rate is not None
        else float(TRAILING_CALLBACK_RATE)
    )
    act_mult = (
        float(trailing_activation_multiplier)
        if trailing_activation_multiplier is not None
        else float(TRAILING_ACTIVATION_MULTIPLIER)
    )

    ord_p = fechar_parcial_posicao_market(
        simbolo,
        exchange,
        frac=float(close_frac),
        execution="market",
    )
    snap_r = consultar_posicao_futures(simbolo, exchange)
    qty_r = abs(float(snap_r.get("contratos") or 0.0))
    if qty_r <= 0:
        print(
            f"{_TAG} ⚠️ [HYBRID-EXIT] Fecho parcial executado mas qty restante ≈ 0 — "
            "sem recolocação de brackets."
        )
        return {
            "ordem_parcial": ord_p,
            "qty_remaining": 0.0,
            "trailing_callback_rate": cb,
        }

    assegurar_brackets_apos_reconciliacao(
        simbolo,
        exchange,
        d,
        qty_r,
        pe,
        trailing_callback_rate=cb,
        trailing_activation_multiplier=act_mult,
        source_tag="HYBRID_EXIT",
        sl_break_even=True,
        force_replace=True,
    )
    try:
        import logger

        logger.atualizar_ultimo_trade_campos(
            simbolo, {"partial_roi": float(PARTIAL_TP_ROI_PCT_SUPABASE)}
        )
        print(
            "📝 [DB SYNC] Parcial gravado. "
            f"Partial ROI: {float(PARTIAL_TP_ROI_PCT_SUPABASE):+.2f}% | Motivo: PARTIAL_TP_50."
        )
    except Exception as e_pr:  # noqa: BLE001
        print(f"{_TAG} ⚠️ partial_roi Supabase: {e_pr}")
    return {
        "ordem_parcial": ord_p,
        "qty_remaining": float(qty_r),
        "trailing_callback_rate": float(cb),
    }


def _primeira_posicao_info_de_fetch_rows(
    posicoes: list[dict[str, Any]],
) -> tuple[float, str, float | None]:
    """
    Primeira posição não-zero em `fetch_positions`: (contratos assinados, lado CCXT, entry_price).
    (0.0, "", None) se flat.
    """
    contratos = 0.0
    lado = ""
    entry_price: float | None = None
    for p in posicoes:
        info = p.get("info") or {}
        signed = float(p.get("contracts") or 0.0)
        pa_raw = info.get("positionAmt")
        usou_native = False
        if pa_raw is not None and str(pa_raw).strip() != "":
            try:
                pa_f = float(pa_raw)
                if abs(pa_f) > 1e-18:
                    signed = pa_f
                    usou_native = True
            except (TypeError, ValueError):
                pass
        if not usou_native:
            side_l = str(p.get("side") or "").lower()
            if signed > 0 and side_l == "short":
                signed = -abs(signed)

        if abs(signed) <= 0:
            continue

        contratos = signed
        lado = str(p.get("side") or "")
        ep_raw = p.get("entryPrice")
        if ep_raw is None or (isinstance(ep_raw, (int, float)) and float(ep_raw) == 0.0):
            ep_raw = info.get("entryPrice") or info.get("avgPrice")
        if ep_raw is not None:
            try:
                efv = float(ep_raw)
                if efv > 0:
                    entry_price = efv
            except (TypeError, ValueError):
                entry_price = None
        break
    return contratos, lado, entry_price


def consultar_posicao_futures(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> dict[str, Any]:
    """
    Indica se há posição líquida no perpétuo (contratos != 0).
    Compatível com a lógica antiga de `posicao_aberta` no main.
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    ex.load_markets()
    market = ex.markets[sym]
    base = market["base"]
    min_amt = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0)

    posicoes = ex.fetch_positions([sym])
    contratos, lado, entry_price = _primeira_posicao_info_de_fetch_rows(posicoes)

    aberta = abs(contratos) > 0 and abs(contratos) >= (min_amt * 0.5 if min_amt else 1e-12)
    direcao_pos = "SHORT" if contratos < 0 else "LONG"

    return {
        "base": base,
        "contratos": contratos,
        "lado": lado,
        "direcao_posicao": direcao_pos,
        "entry_price": entry_price,
        "saldo_base_livre": abs(contratos),
        "min_quantidade_base": min_amt,
        "posicao_aberta": aberta,
    }


def _abortar_se_demasiadas_ordens_abertas(
    exchange: ccxt.binance,
    simbolo_any: str,
    *,
    context: str,
) -> int:
    """`fetch_open_orders`: se contagem > limite → alerta + RuntimeError (parar criação)."""
    sym = _resolver_simbolo_perp(exchange, simbolo_any)
    n = _contar_ordens_abertas_simbolo_ccxt(exchange, simbolo_any)
    if n > int(AURIC_MAX_OPEN_ORDERS_GUARD):
        print(
            "⚠️ [SEGURANÇA] Demasiadas ordens abertas. Abortando criação para evitar spam.",
            flush=True,
        )
        print(
            f"🚨 [ERRO CRÍTICO] {sym}: {n} ordens abertas via fetch_open_orders "
            f"(limite {AURIC_MAX_OPEN_ORDERS_GUARD}). Criação interrompida. {context}",
            file=sys.stderr,
            flush=True,
        )
        raise RuntimeError(
            f"{_TAG} {sym}: {n} ordens abertas > {AURIC_MAX_OPEN_ORDERS_GUARD} ({context})"
        )
    return n


def _assert_posicao_aberta_para_protecao_sync(
    exchange: ccxt.binance,
    simbolo_any: str,
    direcao: str,
    *,
    context: str,
) -> float:
    """
    Obrigatório `fetch_positions` na corretora (não RAM/BD): tamanho ≠ 0 antes de SL/TP/trailing.
    """
    sym = _resolver_simbolo_perp(exchange, simbolo_any)
    exchange.load_markets()
    try:
        pos_live = exchange.fetch_positions([sym])
    except ccxt.BaseError as e_fp:
        msg = (
            f"{_TAG} [CRÍTICO] {context} | {sym}: fetch_positions falhou ({e_fp}). "
            "Abortar criação de proteção."
        )
        print(msg, file=sys.stderr, flush=True)
        raise RuntimeError(msg) from e_fp
    c_signed, _, _ = _primeira_posicao_info_de_fetch_rows(pos_live)
    if abs(float(c_signed)) <= 1e-18:
        cancelar_livro_aberto_ate_zero_sync(
            exchange,
            sym,
            context=f"{context} (posição ZERO em fetch_positions)",
        )
        msg = (
            f"{_TAG} [CRÍTICO] {context} | {sym}: fetch_positions → tamanho ZERO. "
            "Proibido criar ordens de proteção (SL/TP/trailing)."
        )
        print(msg, file=sys.stderr, flush=True)
        raise RuntimeError(msg)
    snap = consultar_posicao_futures(simbolo_any, exchange)
    if not bool(snap.get("posicao_aberta")):
        cancelar_livro_aberto_ate_zero_sync(
            exchange,
            sym,
            context=f"{context} (posição ZERO em consultar_posicao_futures)",
        )
        msg = (
            f"{_TAG} [CRÍTICO] {context} | {sym}: posição ZERO na Binance. "
            "Proibido criar ordens de proteção (SL/TP/trailing)."
        )
        print(msg, file=sys.stderr, flush=True)
        raise RuntimeError(msg)
    d_snap = str(snap.get("direcao_posicao") or "").upper()
    d_exp = str(direcao).strip().upper()
    if d_exp in ("LONG", "SHORT") and d_snap in ("LONG", "SHORT") and d_snap != d_exp:
        msg = (
            f"{_TAG} [CRÍTICO] {context} | {sym}: direção esperada {d_exp} ≠ posição {d_snap}."
        )
        print(msg, file=sys.stderr, flush=True)
        raise RuntimeError(msg)
    return abs(float(snap.get("contratos") or 0.0))


def _assert_seguranca_antes_create_ordem_protecao(
    exchange: ccxt.binance,
    simbolo_any: str,
    direcao: str,
    *,
    context: str,
    allow_other_open_orders: bool = False,
) -> bool:
    """
    Posição real ≠ 0 antes de `create_order` de proteção.
    Fora de `_in_protection_order_batch`: exige livro **vazio** (anti-spam; só criar se lista vazia).
    `allow_other_open_orders=True` para p.ex. `atualizar_ordem_stop` (trailing/TP podem ficar no livro).
    """
    sym = _resolver_simbolo_perp(exchange, simbolo_any)
    if not _in_protection_order_batch:
        _abortar_se_demasiadas_ordens_abertas(exchange, simbolo_any, context=context)
        if not allow_other_open_orders:
            n = len(_fetch_open_orders_ccxt_list(exchange, sym))
            if n > 0:
                print(
                    f"{_TAG} [ANTI-SPAM] {context} | {sym}: {n} ordem(ns) em aberto — "
                    "skip imediato (livro tem de estar vazio antes de criar).",
                    file=sys.stderr,
                    flush=True,
                )
                return False
    _assert_posicao_aberta_para_protecao_sync(exchange, simbolo_any, direcao, context=context)
    return True


def _lado_fecho_protecao(direcao: str) -> str:
    d = str(direcao).strip().upper()
    return "sell" if d == "LONG" else "buy"


def _ordem_protecao_mesmo_tipo_lado(
    o: dict[str, Any],
    direcao: str,
    tipo_norm: str,
) -> bool:
    """tipo_norm: STOP_MARKET | TRAILING_STOP_MARKET | TAKE_PROFIT_MARKET."""
    if _order_type_norm(o) != str(tipo_norm).upper().replace("-", "_"):
        return False
    need = _lado_fecho_protecao(direcao)
    if str(o.get("side") or "").lower() != need:
        return False
    # Matching por tipo+lateralidade: não depender de reduceOnly/clientOrderId,
    # porque alguns retornos de open_orders omitem esses campos.
    return True


def _skip_se_ordem_protecao_ja_existe(
    exchange: ccxt.binance,
    simbolo_ccxt: str,
    direcao: str,
    tipo_norm: str,
) -> dict[str, Any] | None:
    """
    Faz `fetch_open_orders` e devolve a 1ª ordem do mesmo tipo/lado já existente.
    """
    tipo = str(tipo_norm).upper().replace("-", "_")
    for o in _fetch_open_orders_ccxt_list(exchange, simbolo_ccxt):
        if _ordem_protecao_mesmo_tipo_lado(o, direcao, tipo):
            print(f"[SKIP] Ordem de {tipo} já existe na Binance.", flush=True)
            return o
    return None


def _sleep_cadencia_ordens_protecao() -> None:
    time.sleep(2.0)


def _client_order_id_de_ordem(ordem: dict[str, Any]) -> str:
    info = ordem.get("info") or {}
    raw = (
        ordem.get("clientOrderId")
        or info.get("clientOrderId")
        or info.get("newClientOrderId")
        or info.get("origClientOrderId")
        or ""
    )
    return str(raw or "").strip()


def _client_order_id_protecao(prefixo: str, simbolo_ccxt: str, direcao: str) -> str:
    prefix = str(prefixo).strip().upper().replace("-", "_")
    sym_tag = str(simbolo_ccxt).replace("/", "").replace(":", "_")
    side_tag = str(direcao).strip().upper()
    ts = int(time.time() * 1000)
    return f"{prefix}_{sym_tag}_{side_tag}_{ts}"


def check_existing_protection(
    exchange: ccxt.binance,
    symbol: str,
    direcao: str,
) -> dict[str, Any]:
    """
    Lê `fetch_open_orders(symbol)` e classifica proteção existente.

    Regras:
    - SHORT: stopPrice > preço atual => SL; stopPrice < preço atual => TP.
    - LONG:  stopPrice < preço atual => SL; stopPrice > preço atual => TP.
    - Qualquer TRAILING_STOP_MARKET (lado de fecho) => trailing_exists.
    - Prefixos clientOrderId (`SL_`, `TP_`, `TS_`) também contam.
    """
    d = str(direcao).strip().upper()
    rows = _fetch_open_orders_ccxt_list(exchange, symbol)
    px_now = float(_preco_referencia_ultimo(exchange, symbol))
    side_close = _lado_fecho_protecao(d)
    out: dict[str, Any] = {
        "price_now": px_now,
        "sl_exists": False,
        "tp_exists": False,
        "trailing_exists": False,
        "sl_order": None,
        "tp_order": None,
        "trailing_order": None,
    }
    for o in rows:
        if str(o.get("side") or "").lower() != side_close:
            continue
        cid = _client_order_id_de_ordem(o).upper()
        typ = _order_type_norm(o)
        if (cid.startswith("TS_") or typ == "TRAILING_STOP_MARKET") and not out["trailing_exists"]:
            out["trailing_exists"] = True
            out["trailing_order"] = o
        if cid.startswith("SL_") and not out["sl_exists"]:
            out["sl_exists"] = True
            out["sl_order"] = o
        if cid.startswith("TP_") and not out["tp_exists"]:
            out["tp_exists"] = True
            out["tp_order"] = o
        if typ == "TRAILING_STOP_MARKET":
            continue
        sp = _stop_price_de_ordem(o)
        if sp is None:
            continue
        if d == "SHORT":
            if sp > px_now and not out["sl_exists"]:
                out["sl_exists"] = True
                out["sl_order"] = o
            elif sp < px_now and not out["tp_exists"]:
                out["tp_exists"] = True
                out["tp_order"] = o
        else:
            if sp < px_now and not out["sl_exists"]:
                out["sl_exists"] = True
                out["sl_order"] = o
            elif sp > px_now and not out["tp_exists"]:
                out["tp_exists"] = True
                out["tp_order"] = o
    out["all_three"] = bool(out["sl_exists"] and out["tp_exists"] and out["trailing_exists"])
    return out


def _freeze_if_all_three_protections(
    exchange: ccxt.binance,
    simbolo: str,
    direcao: str,
    *,
    context: str,
    sleep_s: float = 60.0,
) -> bool:
    """
    Se já existir SL+TP+Trailing, não recalcula nem recria ordens de proteção.
    """
    prot = check_existing_protection(exchange, simbolo, direcao)
    if not bool(prot.get("all_three")):
        return False
    print(
        f"{_TAG} [FREEZE] {context}: SL+TP+Trailing já armados. Sem alterações; aguardando {sleep_s:.0f}s.",
        flush=True,
    )
    time.sleep(float(sleep_s))
    return True


def _log_protection_counts(
    exchange: ccxt.binance,
    simbolo: str,
    direcao: str,
    *,
    context: str,
) -> dict[str, int]:
    """
    Loga contagem de proteções abertas por tipo no lado de fecho.
    """
    d = str(direcao).strip().upper()
    side_close = _lado_fecho_protecao(d)
    rows = _fetch_open_orders_ccxt_list(exchange, simbolo)
    c_sl = 0
    c_tp = 0
    c_ts = 0
    for o in rows:
        if str(o.get("side") or "").lower() != side_close:
            continue
        typ = _order_type_norm(o)
        if typ == "STOP_MARKET":
            c_sl += 1
        elif typ in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
            c_tp += 1
        elif typ == "TRAILING_STOP_MARKET":
            c_ts += 1
    print(
        f"{_TAG} [PROTECT-COUNT] {context} {simbolo}: SL={c_sl} TP={c_tp} TS={c_ts}",
        flush=True,
    )
    return {"sl": c_sl, "tp": c_tp, "ts": c_ts}


def _cancelar_ordens_abertas_tipo_protecao(
    exchange: ccxt.binance,
    simbolo_ccxt: str,
    direcao: str,
    tipo_norm: str,
    *,
    any_side: bool = False,
) -> int:
    """
    Cancela todas as ordens abertas do mesmo tipo (lado de fecho). Se cancelou alguma,
    aguarda `PROTECTION_PRE_CREATE_CANCEL_SLEEP_S` antes do caller criar a nova.
    """
    d = str(direcao).strip().upper()
    tipo = str(tipo_norm).upper().replace("-", "_")
    n = 0
    for o in list(_fetch_open_orders_ccxt_list(exchange, simbolo_ccxt)):
        if _order_type_norm(o) != tipo:
            continue
        if not any_side and not _ordem_protecao_mesmo_tipo_lado(o, d, tipo):
            continue
        oid = o.get("id")
        if oid is None:
            continue
        try:
            print(
                f"{_TAG} [CLEANUP] Cancelando ordem antiga de {tipo} antes de atualizar. id={oid}",
                flush=True,
            )
            exchange.cancel_order(str(oid), simbolo_ccxt)
            n += 1
        except ccxt.BaseError as ec:
            print(f"{_TAG} cancel pré-create {tipo_norm} id={oid}: {ec}", file=sys.stderr)
    if n:
        time.sleep(float(PROTECTION_PRE_CREATE_CANCEL_SLEEP_S))
    return n


def _sync_posicao_antes_loop_protecao(
    exchange: ccxt.binance,
    simbolo: str,
    direcao: str,
    *,
    context: str,
) -> bool:
    """
    Se a posição não existir mais, limpa ordens do símbolo e sinaliza aguardo.
    """
    snap = consultar_posicao_futures(simbolo, exchange)
    if bool(snap.get("posicao_aberta")):
        d_snap = str(snap.get("direcao_posicao") or "").upper()
        d_exp = str(direcao).strip().upper()
        if d_snap in ("LONG", "SHORT") and d_exp in ("LONG", "SHORT") and d_snap != d_exp:
            cancelar_todas_ordens_abertas(simbolo, exchange)
            print(
                f"{_TAG} [Aguardando Sinal] {context}: direção divergente ({d_snap} != {d_exp}). "
                "Ordens limpas e proteção interrompida.",
                flush=True,
            )
            return False
        return True
    cancelar_todas_ordens_abertas(simbolo, exchange)
    print(
        f"{_TAG} [Aguardando Sinal] {context}: posição fechada. cancel_all_orders executado; "
        "sem recriar proteções.",
        flush=True,
    )
    return False


def _enforce_max_tres_condicionais(
    exchange: ccxt.binance,
    simbolo_ccxt: str,
    direcao: str,
) -> None:
    """
    Garante no máximo 1 ordem por tipo condicional (SL/TP/Trailing) no símbolo.
    """
    d = str(direcao).strip().upper()
    side_close = _lado_fecho_protecao(d)
    keep_types = ("STOP_MARKET", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET")
    rows = _fetch_open_orders_ccxt_list(exchange, simbolo_ccxt)
    for typ in keep_types:
        same_type: list[dict[str, Any]] = []
        for o in rows:
            if _order_type_norm(o) != typ:
                continue
            if str(o.get("side") or "").lower() != side_close:
                continue
            same_type.append(o)
        if len(same_type) <= 1:
            continue
        same_type.sort(
            key=lambda r: float(
                (r.get("timestamp") or ((r.get("info") or {}).get("updateTime")) or 0)
            ),
            reverse=True,
        )
        for old in same_type[1:]:
            oid = old.get("id")
            if oid is None:
                continue
            try:
                print(
                    f"{_TAG} [CLEANUP] Cancelando ordem antiga de {typ} antes de atualizar. id={oid}",
                    flush=True,
                )
                exchange.cancel_order(str(oid), simbolo_ccxt)
            except ccxt.BaseError as ec:
                print(f"{_TAG} cleanup pós-loop {typ} id={oid}: {ec}", file=sys.stderr, flush=True)


def _sleep_apos_criar_ordem_protecao() -> None:
    time.sleep(float(PROTECTION_ORDER_CREATE_THROTTLE_S))


def _rsi_14_de_fechamentos(fechamentos: list[float]) -> float | None:
    """RSI(14) simples (Wilder) para série de fechamentos já ordenada."""
    if len(fechamentos) < 16:
        return None
    ganhos: list[float] = []
    perdas: list[float] = []
    for i in range(1, len(fechamentos)):
        d = float(fechamentos[i]) - float(fechamentos[i - 1])
        ganhos.append(max(d, 0.0))
        perdas.append(max(-d, 0.0))
    period = 14
    avg_gain = sum(ganhos[:period]) / period
    avg_loss = sum(perdas[:period]) / period
    for i in range(period, len(ganhos)):
        avg_gain = ((avg_gain * (period - 1)) + ganhos[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + perdas[i]) / period
    if avg_loss <= 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def obter_sinais_exaustao_short_15m(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> dict[str, Any]:
    """
    Snapshot 15m para proteção de SHORT:
    - low_15m + candle_ts para regra de stall (sem nova mínima).
    - RSI(14) atual e anterior para detetar repique de fundo (rsi<30 e a subir).
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    candles = ex.fetch_ohlcv(sym, timeframe="15m", limit=80) or []
    if len(candles) < 20:
        return {
            "ok": False,
            "motivo": "ohlcv_insuficiente",
            "candle_ts_15m": None,
            "low_15m": None,
            "rsi_14_15m": None,
            "rsi_14_15m_prev": None,
            "rsi_repique_fundo": False,
        }

    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    rsi_now = _rsi_14_de_fechamentos(closes)
    rsi_prev = _rsi_14_de_fechamentos(closes[:-1]) if len(closes) >= 17 else None
    repique = bool(
        rsi_now is not None
        and rsi_prev is not None
        and rsi_prev < 30.0
        and rsi_now > rsi_prev
    )
    return {
        "ok": True,
        "candle_ts_15m": int(candles[-1][0]),
        "low_15m": float(lows[-1]),
        "rsi_14_15m": rsi_now,
        "rsi_14_15m_prev": rsi_prev,
        "rsi_repique_fundo": repique,
    }


def _buscar_ordem_stop_loss_aberta(
    exchange: ccxt.binance,
    simbolo: str,
    direcao: str,
) -> dict[str, Any] | None:
    """
    Ordens reduce-only STOP_MARKET que fecham a posição: LONG → sell, SHORT → buy.
    """
    d = str(direcao).strip().upper()
    for o in exchange.fetch_open_orders(simbolo):
        if not _order_reduce_only_flag(o):
            continue
        typ = _order_type_norm(o)
        if typ != "STOP_MARKET":
            continue
        side = str(o.get("side") or "").lower()
        if d == "LONG" and side == "sell":
            return o
        if d == "SHORT" and side == "buy":
            return o
    return None


def _stop_price_de_ordem(ordem: dict[str, Any]) -> float | None:
    sp = ordem.get("stopPrice")
    if sp is not None and sp != "":
        try:
            return float(sp)
        except (TypeError, ValueError):
            pass
    info = ordem.get("info") or {}
    raw = info.get("stopPrice") or info.get("activatePrice")
    if raw is not None and raw != "":
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return None


def _quantidade_posicao_abs(
    exchange: ccxt.binance,
    simbolo: str,
    direcao: str,
) -> float:
    """Contratos absolutos da posição alinhada a `direcao` (LONG/SHORT)."""
    d = str(direcao).strip().upper()
    for p in exchange.fetch_positions([simbolo]):
        c = float(p.get("contracts") or 0)
        if abs(c) <= 0:
            continue
        lado = str(p.get("side") or "").lower()
        if d == "LONG" and (lado == "long" or (not lado and c > 0)):
            return float(exchange.amount_to_precision(simbolo, abs(c)))
        if d == "SHORT" and (lado == "short" or (not lado and c < 0)):
            return float(exchange.amount_to_precision(simbolo, abs(c)))
    raise ccxt.InvalidOrder(
        f"{_TAG} Nenhuma posição {d} aberta em {simbolo} para atualizar stop."
    )


def atualizar_ordem_stop(
    simbolo: str,
    novo_stop: float,
    direcao: str,
    exchange: ccxt.binance | None = None,
) -> dict[str, Any]:
    """
    Substitui a ordem STOP_MARKET reduce-only (SL) por uma nova com `stopPrice` = `novo_stop`.
    Mantém o take-profit (LIMIT) inalterado.
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    d = str(direcao).strip().upper()
    if d not in ("LONG", "SHORT"):
        raise ValueError(f"{_TAG} direcao deve ser LONG ou SHORT, recebido: {d!r}")
    if not _sync_posicao_antes_loop_protecao(
        ex, sym, d, context="atualizar_ordem_stop"
    ):
        return {"ok": False, "skipped": True, "reason": "position_closed"}
    if _freeze_if_all_three_protections(
        ex,
        sym,
        d,
        context="atualizar_ordem_stop",
        sleep_s=60.0,
    ):
        return {"ok": True, "skipped": True, "reason": "all_three_protections_present"}

    sp = float(ex.price_to_precision(sym, float(novo_stop)))
    if sp <= 0:
        raise ValueError(f"{_TAG} novo_stop inválido: {novo_stop}")

    ord_sl = _buscar_ordem_stop_loss_aberta(ex, sym, d)
    p_ro = _params_reduce_futures()

    if ord_sl is not None:
        prev = _stop_price_de_ordem(ord_sl)
        if prev is not None:
            sp_cmp = float(ex.price_to_precision(sym, prev))
            ref_px = max(abs(sp_cmp), abs(sp), 1e-12)
            rel_diff = abs(sp - sp_cmp) / ref_px
            if rel_diff <= float(SL_REPLACE_MIN_REL_DIFF):
                print(
                    f"{_TAG} Stop novo {sp} ≈ existente {sp_cmp} (Δ {rel_diff:.4%} ≤ "
                    f"{SL_REPLACE_MIN_REL_DIFF:.2%}); sem alteração."
                )
                return {"ok": True, "skipped": True, "stopPrice": sp, "order": ord_sl}

    enforce_single_order_type(ex, sym, "STOP_MARKET")
    try:
        q = float(ex.amount_to_precision(sym, float(_quantidade_posicao_abs(ex, sym, d))))
    except ccxt.BaseError:
        raise
    if ord_sl is None:
        print(
            f"{_TAG} Aviso: SL STOP_MARKET em aberto não encontrado antes do replace; "
            f"a criar nova ordem reduce-only qty={q}.",
            file=sys.stderr,
        )

    if not _exigir_intervalo_min_entre_ordens_protecao(context="atualizar_ordem_stop"):
        return {"ok": False, "skipped": True, "reason": "anti_spam_interval"}
    if not _assert_seguranca_antes_create_ordem_protecao(
        ex, simbolo, d, context="atualizar_ordem_stop", allow_other_open_orders=True
    ):
        return {"ok": False, "skipped": True, "reason": "open_orders_not_empty"}

    try:
        if d == "LONG":
            nova = ex.create_order(
                sym,
                "STOP_MARKET",
                "sell",
                q,
                None,
                {**p_ro, "stopPrice": sp},
            )
        else:
            nova = ex.create_order(
                sym,
                "STOP_MARKET",
                "buy",
                q,
                None,
                {**p_ro, "stopPrice": sp},
            )
        print(f"{_TAG} SL atualizado: STOP_MARKET @ {sp} qty={q} id={nova.get('id')}")
        _marcar_criacao_ordem_protecao()
        _sleep_apos_criar_ordem_protecao()
        return {"ok": True, "skipped": False, "stopPrice": sp, "order": nova}
    except ccxt.BaseError as e:
        print(f"{_TAG} Erro ao criar novo SL: {e}", file=sys.stderr)
        raise


def gerenciar_trailing_stop(
    simbolo: str,
    preco_atual: float,
    preco_entrada: float,
    direcao: str,
    exchange: ccxt.binance | None = None,
) -> dict[str, Any] | None:
    """
    Atualiza o stop loss dinamicamente para proteger lucros.

    - Lucro < TRAILING_LUCRO_ATIVACAO_FRAC (1%): não altera.
    - Caso contrário: SL = max/mín. entre entrada (break-even) e preço atual
      afastado por TRAILING_SL_DIST_FRAC (0,8%), conforme o lado.
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    d = str(direcao).strip().upper()
    if d not in ("LONG", "SHORT"):
        raise ValueError(f"{_TAG} direcao deve ser LONG ou SHORT, recebido: {d!r}")
    if _freeze_if_all_three_protections(
        ex,
        sym,
        d,
        context="gerenciar_trailing_stop",
        sleep_s=60.0,
    ):
        return {
            "ok": True,
            "skipped": True,
            "reason": "all_three_protections_present",
        }

    pa = float(preco_atual)
    pe = float(preco_entrada)
    if pe <= 0 or pa <= 0:
        raise ValueError(f"{_TAG} preco_entrada e preco_atual devem ser positivos.")

    if d == "LONG":
        lucro_atual = (pa - pe) / pe
    else:
        lucro_atual = (pe - pa) / pe

    if lucro_atual < TRAILING_LUCRO_ATIVACAO_FRAC:
        return None

    novo_sl = pe
    if d == "LONG":
        distancia_sl = pa * (1.0 - TRAILING_SL_DIST_FRAC)
        novo_sl = max(pe, distancia_sl)
    else:
        distancia_sl = pa * (1.0 + TRAILING_SL_DIST_FRAC)
        novo_sl = min(pe, distancia_sl)

    novo_sl = float(ex.price_to_precision(sym, novo_sl))

    global _trailing_sl_adjust_anchor_by_sym
    anchor = float(_trailing_sl_adjust_anchor_by_sym.get(sym, 0.0))
    thr_mv = float(BRACKET_REPLACE_MIN_FAVORABLE_PRICE_MOVE)
    if anchor > 0 and pa > 0 and thr_mv > 0:
        if d == "LONG" and pa < anchor * (1.0 + thr_mv):
            return None
        if d == "SHORT" and pa > anchor * (1.0 - thr_mv):
            return None

    print(
        f"{_TAG} 🔄 [TRAILING STOP] Ajustando SL para {novo_sl:.4f} "
        f"(lucro {lucro_atual:.2%}, ref entrada {pe:.4f}, último {pa:.4f})"
    )
    out = atualizar_ordem_stop(simbolo, novo_sl, d, ex)
    if out.get("ok"):
        _trailing_sl_adjust_anchor_by_sym[sym] = pa
    out["lucro_atual"] = lucro_atual
    out["novo_sl"] = novo_sl
    return out


# --- Compatibilidade com código legado (Spot) — redireciona para Futures ---

def consultar_posicao_spot(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> dict[str, Any]:
    """Alias: usa posição em **Futures** linear (nome histórico)."""
    return consultar_posicao_futures(simbolo, exchange)


def executar_compra_spot_market(
    simbolo: str,
    exchange: ccxt.binance | None = None,
    *,
    alavancagem: float | None = None,
    notional_usdt_override: float | None = None,
) -> dict[str, Any]:
    """Alias: abre **long** a mercado em Futures linear (nome histórico 'Spot')."""
    return abrir_long_market(
        simbolo,
        exchange,
        alavancagem=alavancagem,
        notional_usdt_override=notional_usdt_override,
    )


def executar_venda_spot_total(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> dict[str, Any]:
    """Alias: **fecha** a posição futura (nome histórico 'venda spot total')."""
    return fechar_posicao_market(simbolo, exchange)


def obter_saldo_usdt(exchange: ccxt.binance | None = None) -> float:
    """Alias: saldo USDC na carteira de futuros."""
    return obter_saldo_usdt_margem(exchange)


if __name__ == "__main__":
    print(_TAG, "Verificação: saldo USDC (margem futuros) e mercados carregados.")
    try:
        ex = criar_exchange_binance()
        u = obter_saldo_usdt_margem(ex)
        print(f"Saldo USDC (futures): {u:.8f}")
    except Exception as e:  # noqa: BLE001
        print(f"Falha: {e}", file=sys.stderr)
        sys.exit(1)
