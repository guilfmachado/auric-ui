"""
executor_futures — Binance Futures USDT-M (perpétuos), MAINNET via ccxt.

Margem isolada e alavancagem configuráveis. Logs em português com tag [MAINNET FUTURES].
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import ccxt
from dotenv import load_dotenv

load_dotenv()

_TAG = "[MAINNET FUTURES]"
QUOTE_ASSET = "USDC"

# Gestão de risco (frações): gatilho de trailing/SL em relação ao preço de entrada.
LONG_TP = 0.020
LONG_SL = 0.010
SHORT_TP = 0.015
SHORT_SL = 0.008
TRAILING_CALLBACK_RATE = 0.7  # % de recuo da máxima/mínima após ativação
TRAILING_ACTIVATION_MULTIPLIER = 1.0  # 1.0 = mantém gatilho igual ao antigo TP

# Alavancagem usada só no log de liquidação aproximada (ajuste em configurar_alavancagem / main).
ALAVANCAGEM_REF_LOG_PADRAO = 3

# Position sizing: notional USDT = saldo_USDT × PERCENTUAL_BANCA × alavancagem; qty = notional / preço.
# Fallback quando `risk_fraction` não é passado (alinhado a main.RISK_FRACTION_PADRAO em sessão agressiva).
PERCENTUAL_BANCA = 0.20

# LIMIT + offset vs. livro: compra ref. bid (+0,05%); venda ref. ask (−0,05%).
# Entradas: timeInForce IOC (sem GTC pendurado — liberta margem). Fechos reduce-only: GTC + chase.
PRECO_ABERTURA_LIMITE_OFFSET = 0.0005  # 0,05%

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
    Cliente Binance em modo **Futures USDT-M** (defaultType = future), MAINNET.
    """
    api_key, api_secret = _carregar_chaves()
    exchange = ccxt.binance(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        }
    )
    exchange.set_sandbox_mode(False)
    return exchange


def _resolver_simbolo_perp(exchange: ccxt.binance, simbolo: str) -> str:
    """
    Converte ETH/USDC no símbolo unificado do perpétuo linear (ex.: ETH/USDC:USDC).
    """
    exchange.load_markets()
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
    Define margem **isolada** e alavancagem no contrato (USDT-M).
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
    """Notional alvo: saldo USDT (margem futuros) × risk_fraction × alavancagem."""
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
    Converte notional em USDT para quantidade na base (contratos ETH), com precisão.
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


def _preco_limite_limit_offset_book(
    exchange: ccxt.binance,
    simbolo: str,
    side: str,
    *,
    offset_frac: float | None = None,
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
    s = str(side).strip().lower()
    if s == "buy":
        base = bid if bid > 0 else last
        raw = base * (1.0 + off)
    elif s == "sell":
        base = ask if ask > 0 else last
        raw = base * (1.0 - off)
    else:
        raise ValueError(f"{_TAG} side inválido para LIMIT+offset: {side!r}")
    if base <= 0 or raw <= 0:
        raise RuntimeError(
            f"{_TAG} bid/ask/last inválidos para {simbolo} (bid={bid}, ask={ask}, last={last})."
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
            ex, simbolo, side, offset_frac=PRECO_ABERTURA_LIMITE_OFFSET
        )
        ref = "bid" if str(side).lower() == "buy" else "ask"
        ro_txt = ", reduceOnly" if reduce_only else ""
        print(
            f"{_TAG} [LIMIT+offset {rnd}/{cap}] {nome_lado} {side.upper()} @ {preco_limite} "
            f"qty={qty_left} (ref {ref}={base:.4f}, bid={bid:.4f}, ask={ask:.4f}, {tif}{ro_txt})"
        )

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
                print(f"{_TAG} ❌ Erro no Chase (create_order): {e}", file=sys.stderr)
                continue
            try:
                market = ex.market(simbolo)
                tick = float(
                    (((market.get("limits") or {}).get("price") or {}).get("min"))
                    or 0.01
                )
                preco_aj = (
                    max(tick, float(preco_limite) - tick)
                    if str(side).lower() == "buy"
                    else float(preco_limite) + tick
                )
                preco_aj = float(ex.price_to_precision(simbolo, preco_aj))
                print(
                    f"{_TAG} ⚠️ Rejeição Post-Only; retry com 1 tick ({preco_limite} -> {preco_aj})."
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
                print(f"{_TAG} ❌ Erro no retry GTX 1 tick: {e2}", file=sys.stderr)
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

    raise RuntimeError(
        f"{_TAG} Entrada LIMIT: esgotadas {cap} tentativas de chase."
    )


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
            f"{_TAG} Liquidação estimada (≈): LONG — preço ~{liq:.4f} USDT "
            f"| P_entrada×(1−1/L) = {preco_entrada:.4f}×(1−1/{alavancagem:.4f})"
        )
    else:
        liq = preco_entrada * (1.0 + inv)
        print(
            f"{_TAG} Liquidação estimada (≈): SHORT — preço ~{liq:.4f} USDT "
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
    Cancela todas as ordens abertas no par (futures USDT-M).
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


def assegurar_brackets_apos_reconciliacao(
    simbolo: str,
    exchange: ccxt.binance,
    direcao: str,
    qty_contratos: float,
    preco_entrada: float,
    *,
    trailing_callback_rate: float | None = None,
    trailing_activation_multiplier: float | None = None,
) -> None:
    """
    Após detetar posição na Binance sem estado completo no maestro: remove TP/SL reduce-only
    antigos e recria SL fixo + TRAILING_STOP (modo vigia na bolsa).
    """
    d = str(direcao).strip().upper()
    cancelar_ordens_reduce_only_abertas(simbolo, exchange)
    sym = _resolver_simbolo_perp(exchange, simbolo)
    q = abs(float(qty_contratos))
    if q <= 0:
        print(f"{_TAG} assegurar_brackets: qty inválida ({qty_contratos!r})", file=sys.stderr)
        return
    if d == "LONG":
        _criar_bracket_long(
            exchange,
            sym,
            q,
            float(preco_entrada),
            trailing_callback_rate=trailing_callback_rate,
            trailing_activation_multiplier=trailing_activation_multiplier,
        )
    elif d == "SHORT":
        _criar_bracket_short(
            exchange,
            sym,
            q,
            float(preco_entrada),
            trailing_callback_rate=trailing_callback_rate,
            trailing_activation_multiplier=trailing_activation_multiplier,
        )
    else:
        raise ValueError(f"{_TAG} direcao inválida: {d!r}")


def _params_reduce_futures() -> dict[str, Any]:
    """Parâmetros comuns em ordens de fecho na Binance USDT-M."""
    return {
        "reduceOnly": True,
        "workingType": "MARK_PRICE",
    }


def _criar_bracket_long(
    exchange: ccxt.binance,
    simbolo: str,
    qty: float,
    preco_entrada: float,
    *,
    trailing_callback_rate: float | None = None,
    trailing_activation_multiplier: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Bracket LONG na Binance USDT-M:
    - STOP_MARKET (SL fixo de proteção inicial)
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

    tp_raw = preco_entrada * (1.0 + (LONG_TP * act_mult))  # activationPrice do trailing
    sl_raw = preco_entrada * (1.0 - LONG_SL)
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
    print(
        f"{_TAG} Bracket LONG: SL STOP_MARKET sell @ {sl_p} (−{LONG_SL:.1%}) + "
        f"TRAILING_STOP_MARKET sell (activation @ {tp_p} / +{(LONG_TP * act_mult):.1%}, "
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
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Bracket SHORT na Binance USDT-M:
    - STOP_MARKET (SL fixo de proteção inicial)
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

    tp_raw = preco_entrada * (1.0 - (SHORT_TP * act_mult))  # activationPrice do trailing
    sl_raw = preco_entrada * (1.0 + SHORT_SL)
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
    print(
        f"{_TAG} Bracket SHORT: SL STOP_MARKET buy @ {sl_p} (+{SHORT_SL:.1%}) + "
        f"TRAILING_STOP_MARKET buy (activation @ {tp_p} / −{(SHORT_TP * act_mult):.1%}, "
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
        print(f"{_TAG} Erro ao obter saldo USDT (futures): {e}", file=sys.stderr)
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
) -> dict[str, Any]:
    """
    Abre **long** com LIMIT **IOC** + **chase**: preço = bid × (1 + offset 0,05%), `price_to_precision`;
    após ~CHASE_ENTRADA_TIMEOUT_S sem fill suficiente, reabre no novo livro (sem GTC pendurado).
    Notional = risk_fraction×saldo_margem×alav (fallback PERCENTUAL_BANCA); depois bracket (SL + trailing).
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
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
            f"{_TAG} Notional inválido ({quantidade_usd:.4f} USDT). Verifique saldo em margem."
        )
    amt = _quantidade_base_a_partir_de_usd(ex, sym, quantidade_usd)
    saldo_m = obter_saldo_usdt_margem(ex)
    risk_txt = (
        f"{rf_used * 100:.1f}% margem USDT"
        if rf_used is not None
        else "notional override (risk N/A)"
    )

    print(
        f"{_TAG} Abrir LONG {sym} — notional ~{quantidade_usd:.2f} USDT "
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
) -> dict[str, Any]:
    """
    Abre **short** com LIMIT IOC + chase: preço = ask × (1 − offset 0,05%), `price_to_precision`;
    mesmo fluxo que o long. Depois **reduce-only** STOP_MARKET (SL) e TRAILING_STOP_MARKET.
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
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
            f"{_TAG} Notional inválido ({quantidade_usd:.4f} USDT). Verifique saldo em margem."
        )
    amt = _quantidade_base_a_partir_de_usd(ex, sym, quantidade_usd)
    saldo_m = obter_saldo_usdt_margem(ex)
    risk_txt = (
        f"{rf_used * 100:.1f}% margem USDT"
        if rf_used is not None
        else "notional override (risk N/A)"
    )

    print(
        f"{_TAG} Abrir SHORT {sym} — notional ~{quantidade_usd:.2f} USDT "
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
        c = float(p.get("contracts") or 0)
        if abs(c) > 0:
            contratos = c
            lado = str(p.get("side") or "")
            ep_raw = p.get("entryPrice")
            if ep_raw is None or (isinstance(ep_raw, (int, float)) and float(ep_raw) == 0.0):
                inf = p.get("info") or {}
                ep_raw = inf.get("entryPrice") or inf.get("avgPrice")
            if ep_raw is not None:
                try:
                    efv = float(ep_raw)
                    if efv > 0:
                        entry_price = efv
                except (TypeError, ValueError):
                    entry_price = None
            break

    aberta = abs(contratos) > 0 and abs(contratos) >= (min_amt * 0.5 if min_amt else 1e-12)
    direcao_pos = (
        "SHORT"
        if (str(lado).lower() == "short" or contratos < 0)
        else "LONG"
    )

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
    """Alias: usa posição em **Futures** USDT-M (nome histórico)."""
    return consultar_posicao_futures(simbolo, exchange)


def executar_compra_spot_market(
    simbolo: str,
    exchange: ccxt.binance | None = None,
    *,
    alavancagem: float | None = None,
    notional_usdt_override: float | None = None,
) -> dict[str, Any]:
    """Alias: abre **long** a mercado em USDT-M (nome histórico 'Spot')."""
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
    """Alias: saldo USDT na carteira de futuros."""
    return obter_saldo_usdt_margem(exchange)


if __name__ == "__main__":
    print(_TAG, "Verificação: saldo USDT (margem futuros) e mercados carregados.")
    try:
        ex = criar_exchange_binance()
        u = obter_saldo_usdt_margem(ex)
        print(f"Saldo USDT (futures): {u:.8f}")
    except Exception as e:  # noqa: BLE001
        print(f"Falha: {e}", file=sys.stderr)
        sys.exit(1)
