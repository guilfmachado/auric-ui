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

# Abertura LIMIT IOC + chase: após timeout sem fill suficiente, cancela (se ainda aberta) e reabre
# no novo bid/ask até N rondas. LONG: defaults abaixo. SHORT: mercado em queda rápida —
# timeouts mais curtos / mais rondas. Overrides via env.
CHASE_ENTRADA_TIMEOUT_S = float(os.getenv("AURIC_CHASE_TIMEOUT_S", "15"))
CHASE_ENTRADA_MAX_ROUNDS = int(os.getenv("AURIC_CHASE_MAX_ROUNDS", "3"))
# Chase agressivo só para abertura SHORT (ETH a «despencar» — ordem não fica pendurada acima do mercado).
CHASE_SHORT_TIMEOUT_S = float(os.getenv("AURIC_CHASE_SHORT_TIMEOUT_S", "8"))
CHASE_SHORT_MAX_ROUNDS = int(os.getenv("AURIC_CHASE_SHORT_MAX_ROUNDS", "6"))
# Realização parcial: `market` (default) ou `ioc` (LIMIT IOC agressivo).
PARTIAL_TP_EXECUTION = os.getenv("AURIC_PARTIAL_TP_EXEC", "market").strip().lower()
# Alinhado ao `PARTIAL_TP_ROI_FRAC` do main (fracção → % no Supabase `trades.partial_roi`).
PARTIAL_TP_ROI_PCT_SUPABASE = float(os.getenv("AURIC_PARTIAL_TP_ROI_PCT", "0.6"))

# ORDER-GUARD (vigia): verificar SL + TP na bolsa no máximo 1×/30s por ciclo agregado.
ORDER_GUARD_INTERVAL_S = 30.0
_order_guard_last_monotonic: float = 0.0


def reset_protective_order_guard_throttle() -> None:
    """Repor o throttle do ORDER-GUARD (ex.: ao fechar posição / reset vigia)."""
    global _order_guard_last_monotonic
    _order_guard_last_monotonic = 0.0


def _order_reduce_only_flag(o: dict[str, Any]) -> bool:
    if bool(o.get("reduceOnly")):
        return True
    info = o.get("info") or {}
    v = str(info.get("reduceOnly", "")).lower()
    return v in ("true", "1", "yes")


def _order_type_norm(o: dict[str, Any]) -> str:
    t = str(o.get("type") or "").upper().replace("-", "_")
    if t:
        return t
    info = o.get("info") or {}
    raw = info.get("type") or info.get("origType") or info.get("orderType") or ""
    return str(raw).upper().replace("-", "_")


def _protective_stop_and_tp_present(
    exchange: ccxt.binance,
    simbolo_ccxt: str,
    direcao: str,
) -> tuple[bool, bool]:
    """
    Ordens reduce-only de fecho: LONG → sell; SHORT → buy.
    SL = STOP_MARKET; TP (vigia Auric) = TRAILING_STOP_MARKET (ou TAKE_PROFIT_MARKET se existir).
    """
    d = str(direcao).strip().upper()
    if d not in ("LONG", "SHORT"):
        return False, False
    need_side = "sell" if d == "LONG" else "buy"
    has_sl = False
    has_tp = False
    try:
        rows = exchange.fetch_open_orders(simbolo_ccxt)
    except ccxt.BaseError:
        return False, False
    for o in rows:
        if not _order_reduce_only_flag(o):
            continue
        if str(o.get("side") or "").lower() != need_side:
            continue
        typ = _order_type_norm(o)
        if typ == "STOP_MARKET":
            has_sl = True
        elif typ in (
            "TRAILING_STOP_MARKET",
            "TAKE_PROFIT_MARKET",
            "TAKE_PROFIT",
        ):
            has_tp = True
    return has_sl, has_tp


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
    global _order_guard_last_monotonic
    now = time.monotonic()
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

    has_sl, has_tp = _protective_stop_and_tp_present(exchange, sym, d)
    if has_sl and has_tp:
        return

    for tipo, ok in (("Stop Loss", has_sl), ("Take Profit", has_tp)):
        if ok:
            continue
        print(
            f"🚨 [ORDER-GUARD] Ordem de {tipo} ausente! Recriando para proteção do capital...."
        )

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
    )


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
) -> dict[str, Any]:
    """
    LIMIT + offset (bid/ask ±0,05%) com protocolo maker-only (GTX / Post-Only).
    Se rejeitar por Post-Only, reenvia com ajuste de 1 tick no preço.
    """
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
            offset_frac=PRECO_ABERTURA_LIMITE_OFFSET,
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
                    off_rt = float(PRECO_ABERTURA_LIMITE_OFFSET)
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
) -> None:
    """
    Após detetar posição na Binance sem estado completo no maestro: remove TP/SL reduce-only
    antigos e recria SL dinâmico (N× trailing) + TRAILING_STOP (modo vigia na bolsa).
    Se `sl_break_even=True`, o STOP_MARKET de protecção fica no preço de entrada (risco zero).
    """
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

    # Nuke estrito: cancelar tudo no símbolo (endpoint nativo futures), depois validar zero abertas.
    cancelar_todas_ordens_futures_nativo(simbolo, exchange)
    ordens_restantes = obter_ordens_abertas_futures_nativo(simbolo, exchange)
    if len(ordens_restantes) > 0:
        print(
            f"{_TAG} [ERRO] Falha ao limpar ordens antigas. Abortando criação de brackets para evitar spam.",
            file=sys.stderr,
        )
        return
    if d == "LONG":
        _criar_bracket_long(
            exchange,
            sym,
            q,
            float(preco_entrada),
            trailing_callback_rate=cb_rate,
            trailing_activation_multiplier=act_mult,
            sl_break_even=sl_break_even,
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
        )
    else:
        raise ValueError(f"{_TAG} direcao inválida: {d!r}")
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


def _criar_bracket_long(
    exchange: ccxt.binance,
    simbolo: str,
    qty: float,
    preco_entrada: float,
    *,
    trailing_callback_rate: float | None = None,
    trailing_activation_multiplier: float | None = None,
    sl_break_even: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Bracket LONG na Binance Futures linear:
    - STOP_MARKET (SL inicial = SL_DISTANCE_VS_TRAILING_MULT × trailing %, ou break-even)
    - TRAILING_STOP_MARKET (take-profit dinâmico; ativa no antigo gatilho de TP)
    """
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
    else:
        tp_raw = preco_entrada * (1.0 + (LONG_TP * act_mult))  # activationPrice do trailing
        sl_frac = _sl_frac_from_trailing_callback_pct(cb_rate)
        sl_raw = preco_entrada * (1.0 - sl_frac)
        act_txt = f"+{(LONG_TP * act_mult):.1%}"
    tp_p = float(exchange.price_to_precision(simbolo, tp_raw))
    sl_p = float(exchange.price_to_precision(simbolo, sl_raw))

    p_ro = _params_reduce_futures()
    ord_trailing = exchange.create_order(
        simbolo,
        "TRAILING_STOP_MARKET",
        "sell",
        q,
        None,
        {
            **p_ro,
            "activationPrice": tp_p,
            "callbackRate": cb_rate,
        },
    )
    ord_sl = exchange.create_order(
        simbolo,
        "STOP_MARKET",
        "sell",
        q,
        None,
        {**p_ro, "stopPrice": sl_p},
    )
    if sl_break_even:
        print(
            f"{_TAG} Bracket LONG: SL STOP_MARKET sell @ {sl_p} (BREAK-EVEN entrada) + "
            f"TRAILING_STOP_MARKET sell (activation @ {tp_p} / {act_txt}, "
            f"callbackRate={cb_rate:.1f}%), qty={q}"
        )
    else:
        print(
            f"{_TAG} Bracket LONG: SL STOP_MARKET sell @ {sl_p} (−{sl_frac:.3%} = "
            f"{SL_DISTANCE_VS_TRAILING_MULT:g}× trailing {cb_rate:.3f}%) + "
            f"TRAILING_STOP_MARKET sell (activation @ {tp_p} / {act_txt}, "
            f"callbackRate={cb_rate:.1f}%), qty={q}"
        )
    return ord_trailing, ord_sl


def _criar_bracket_short(
    exchange: ccxt.binance,
    simbolo: str,
    qty: float,
    preco_entrada: float,
    *,
    trailing_callback_rate: float | None = None,
    trailing_activation_multiplier: float | None = None,
    sl_break_even: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Bracket SHORT na Binance Futures linear:
    - STOP_MARKET (SL inicial = SL_DISTANCE_VS_TRAILING_MULT × trailing %, ou break-even)
    - TRAILING_STOP_MARKET (take-profit dinâmico; ativa no antigo gatilho de TP)
    """
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
    else:
        tp_raw = preco_entrada * (1.0 - (SHORT_TP * act_mult))  # activationPrice do trailing
        sl_frac = _sl_frac_from_trailing_callback_pct(cb_rate)
        sl_raw = preco_entrada * (1.0 + sl_frac)
        act_txt = f"−{(SHORT_TP * act_mult):.1%}"
    tp_p = float(exchange.price_to_precision(simbolo, tp_raw))
    sl_p = float(exchange.price_to_precision(simbolo, sl_raw))

    p_ro = _params_reduce_futures()
    ord_trailing = exchange.create_order(
        simbolo,
        "TRAILING_STOP_MARKET",
        "buy",
        q,
        None,
        {
            **p_ro,
            "activationPrice": tp_p,
            "callbackRate": cb_rate,
        },
    )
    ord_sl = exchange.create_order(
        simbolo,
        "STOP_MARKET",
        "buy",
        q,
        None,
        {**p_ro, "stopPrice": sl_p},
    )
    if sl_break_even:
        print(
            f"{_TAG} Bracket SHORT: SL STOP_MARKET buy @ {sl_p} (BREAK-EVEN entrada) + "
            f"TRAILING_STOP_MARKET buy (activation @ {tp_p} / {act_txt}, "
            f"callbackRate={cb_rate:.1f}%), qty={q}"
        )
    else:
        print(
            f"{_TAG} Bracket SHORT: SL STOP_MARKET buy @ {sl_p} (+{sl_frac:.3%} = "
            f"{SL_DISTANCE_VS_TRAILING_MULT:g}× trailing {cb_rate:.3f}%) + "
            f"TRAILING_STOP_MARKET buy (activation @ {tp_p} / {act_txt}, "
            f"callbackRate={cb_rate:.1f}%), qty={q}"
        )
    return ord_trailing, ord_sl


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
) -> dict[str, Any]:
    """
    Abre **long** com LIMIT **IOC** + **chase**: preço = bid × (1 + offset 0,05%), `price_to_precision`;
    após ~CHASE_ENTRADA_TIMEOUT_S sem fill suficiente, reabre no novo livro (sem GTC pendurado).
    Notional = risk_fraction×saldo_margem×alav (fallback PERCENTUAL_BANCA); depois bracket (SL + trailing).
    """
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
        f"chase até {CHASE_ENTRADA_MAX_ROUNDS}×/{CHASE_ENTRADA_TIMEOUT_S:.0f}s..."
    )
    try:
        ordem = _entrar_limite_com_chase_futuros(
            ex,
            sym,
            "buy",
            amt,
            nome_lado="LONG",
            max_rounds=CHASE_ENTRADA_MAX_ROUNDS,
            force_reference_price=force_reference_price,
            is_manual_force=is_manual_force,
        )
        print(f"{_TAG} Long aceito. id={ordem.get('id')} status={ordem.get('status')}")
        qty_pos, preco_ent = _aguardar_qty_e_preco_entrada(ex, sym)
        _log_liquidacao_estimada(preco_ent, "LONG", lev)
        ord_trailing, ord_sl = _criar_bracket_long(
            ex,
            sym,
            qty_pos,
            preco_ent,
            trailing_callback_rate=trailing_callback_rate,
            trailing_activation_multiplier=trailing_activation_multiplier,
        )
        out = dict(ordem)
        out["auric_take_profit"] = ord_trailing  # compat legado
        out["auric_trailing_stop"] = ord_trailing
        out["auric_stop_loss"] = ord_sl
        out["auric_entry_qty"] = qty_pos
        out["auric_entry_price"] = preco_ent
        return out
    except Exception as e:
        print(f"{_TAG} ❌ [BINANCE-ERROR] {e}", file=sys.stderr)
        print(f"{_TAG} Erro ao abrir long / bracket: {e}", file=sys.stderr)
        raise


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
) -> dict[str, Any]:
    """
    Abre **short** com LIMIT IOC + chase: preço = ask × (1 − offset 0,05%), `price_to_precision`;
    mesmo fluxo que o long. Depois **reduce-only** STOP_MARKET (SL) e TRAILING_STOP_MARKET.
    """
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
        f"chase SHORT {CHASE_SHORT_MAX_ROUNDS}×/{CHASE_SHORT_TIMEOUT_S:.0f}s "
        f"(queda rápida — re-quote apertado; env: AURIC_CHASE_SHORT_*)..."
    )
    try:
        ordem = _entrar_limite_com_chase_futuros(
            ex,
            sym,
            "sell",
            amt,
            nome_lado="SHORT",
            max_rounds=CHASE_SHORT_MAX_ROUNDS,
            timeout_s=CHASE_SHORT_TIMEOUT_S,
            force_reference_price=force_reference_price,
            is_manual_force=is_manual_force,
        )
        print(f"{_TAG} Short aceito. id={ordem.get('id')} status={ordem.get('status')}")
        qty_pos, preco_ent = _aguardar_qty_e_preco_entrada(ex, sym)
        _log_liquidacao_estimada(preco_ent, "SHORT", lev)
        ord_trailing, ord_sl = _criar_bracket_short(
            ex,
            sym,
            qty_pos,
            preco_ent,
            trailing_callback_rate=trailing_callback_rate,
            trailing_activation_multiplier=trailing_activation_multiplier,
        )
        out = dict(ordem)
        out["auric_take_profit"] = ord_trailing  # compat legado
        out["auric_trailing_stop"] = ord_trailing
        out["auric_stop_loss"] = ord_sl
        out["auric_entry_qty"] = qty_pos
        out["auric_entry_price"] = preco_ent
        return out
    except Exception as e:
        print(f"{_TAG} ❌ [BINANCE-ERROR] {e}", file=sys.stderr)
        print(f"{_TAG} Erro ao abrir short / bracket: {e}", file=sys.stderr)
        raise


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
    """Grava `final_roi` (% vs entrada) e `exit_type` na última linha `trades` do símbolo."""
    try:
        import logger

        roi = _roi_fechamento_percentual(direcao, preco_entrada, preco_saida)
        logger.atualizar_ultimo_trade_campos(
            simbolo,
            {"final_roi": float(roi), "exit_type": str(exit_type)},
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
    )
    try:
        import logger

        logger.atualizar_ultimo_trade_campos(
            simbolo, {"partial_roi": float(PARTIAL_TP_ROI_PCT_SUPABASE)}
        )
    except Exception as e_pr:  # noqa: BLE001
        print(f"{_TAG} ⚠️ partial_roi Supabase: {e_pr}")
    return {
        "ordem_parcial": ord_p,
        "qty_remaining": float(qty_r),
        "trailing_callback_rate": float(cb),
    }


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
            # CCXT por vezes devolve contracts>0 com side='short' — SHORT exige sinal negativo.
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
        if not o.get("reduceOnly"):
            continue
        typ = str(o.get("type") or "").upper()
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

    sp = float(ex.price_to_precision(sym, float(novo_stop)))
    if sp <= 0:
        raise ValueError(f"{_TAG} novo_stop inválido: {novo_stop}")

    ord_sl = _buscar_ordem_stop_loss_aberta(ex, sym, d)
    p_ro = _params_reduce_futures()

    if ord_sl is not None:
        oid = ord_sl.get("id")
        rem = ord_sl.get("remaining")
        amt_raw = rem
        if amt_raw is None or float(amt_raw) <= 0:
            amt_raw = ord_sl.get("amount")
        if amt_raw is None:
            amt_raw = _quantidade_posicao_abs(ex, sym, d)
        q = float(ex.amount_to_precision(sym, float(amt_raw)))
        prev = _stop_price_de_ordem(ord_sl)
        if prev is not None:
            sp_cmp = float(ex.price_to_precision(sym, prev))
            if sp_cmp == sp:
                print(f"{_TAG} Stop já em {sp}; sem alteração.")
                return {"ok": True, "skipped": True, "stopPrice": sp, "order": ord_sl}

        if oid is not None:
            try:
                ex.cancel_order(oid, sym)
            except ccxt.BaseError as e:
                print(f"{_TAG} Erro ao cancelar SL anterior: {e}", file=sys.stderr)
                raise
    else:
        q = _quantidade_posicao_abs(ex, sym, d)
        print(
            f"{_TAG} Aviso: SL STOP_MARKET em aberto não encontrado; "
            f"a criar nova ordem reduce-only qty={q}.",
            file=sys.stderr,
        )

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

    print(
        f"{_TAG} 🔄 [TRAILING STOP] Ajustando SL para {novo_sl:.4f} "
        f"(lucro {lucro_atual:.2%}, ref entrada {pe:.4f}, último {pa:.4f})"
    )
    out = atualizar_ordem_stop(simbolo, novo_sl, d, ex)
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
