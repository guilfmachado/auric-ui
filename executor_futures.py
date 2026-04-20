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

# Gestão de risco (frações): gatilho de trailing/SL em relação ao preço de entrada.
LONG_TP = 0.020
LONG_SL = 0.010
SHORT_TP = 0.015
SHORT_SL = 0.008
TRAILING_CALLBACK_RATE = 0.5  # % de recuo da máxima/mínima após ativação
TRAILING_ACTIVATION_MULTIPLIER = 1.0  # 1.0 = mantém gatilho igual ao antigo TP

# Alavancagem usada só no log de liquidação aproximada (ajuste em configurar_alavancagem / main).
ALAVANCAGEM_REF_LOG_PADRAO = 3

# Position sizing: notional USDT = saldo_USDT × PERCENTUAL_BANCA × alavancagem; qty = notional / preço.
PERCENTUAL_BANCA = 0.15

# Abertura em LIMIT agressivo vs. último preço (proteção de slippage): long acima, short abaixo.
PRECO_ABERTURA_LIMITE_OFFSET = 0.0005  # 0,05%

# Trailing stop: após lucro ≥ este valor, SL vai a break-even e depois segue o preço a TRAILING_SL_DIST_FRAC.
TRAILING_LUCRO_ATIVACAO_FRAC = 0.01  # 1%
TRAILING_SL_DIST_FRAC = 0.008  # 0,8% atrás (LONG) / à frente (SHORT) do preço atual

# Abertura LIMIT GTC + chase: após timeout sem fill, cancela e reabre no novo último
# (+0,05% long / −0,05% short) até N rondas. LONG: defaults abaixo. SHORT: mercado em queda rápida —
# usar timeouts mais curtos e mais rondas (caça o preço). Overrides via env.
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
    Converte ETH/USDT no símbolo unificado do perpétuo USDT-M (ex.: ETH/USDT:USDT).
    """
    exchange.load_markets()
    if simbolo in exchange.markets:
        m = exchange.markets[simbolo]
        if m.get("swap") and m.get("linear"):
            return simbolo
    if "/USDT" in simbolo and ":USDT" not in simbolo:
        cand = f"{simbolo.split('/')[0]}/USDT:USDT"
        if cand in exchange.markets:
            return cand
    raise ValueError(
        f"{_TAG} Par USDT-M não encontrado: {simbolo}. Use ex. ETH/USDT ou ETH/USDT:USDT."
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
) -> float:
    """Notional alvo: saldo USDT (margem futuros) × PERCENTUAL_BANCA × alavancagem."""
    saldo = obter_saldo_usdt_margem(exchange)
    return float(saldo) * PERCENTUAL_BANCA * float(alavancagem)


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
    Chase: LIMIT GTC com offset 0,05% vs último; após `CHASE_ENTRADA_TIMEOUT_S` verifica fill;
    se não, cancela e reabre até `tentativas` rondas.

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
) -> dict[str, Any]:
    """
    Abertura LIMIT GTC com offset 0,05% vs último (`fetch_ticker`); aguarda
    `timeout_s` (ou default global); se não houver fill suficiente, cancela e repete (caça o preço).
    """
    cap = max_rounds if max_rounds is not None else CHASE_ENTRADA_MAX_ROUNDS
    wait = float(timeout_s) if timeout_s is not None else CHASE_ENTRADA_TIMEOUT_S
    params_gtc: dict[str, Any] = {"timeInForce": "GTC"}

    for rnd in range(1, cap + 1):
        ticker = ex.fetch_ticker(simbolo)
        ultimo = float(ticker.get("last") or ticker.get("close") or 0)
        if ultimo <= 0:
            raise RuntimeError(f"{_TAG} Preço último inválido (chase {rnd}).")

        if side == "buy":
            preco_limite = float(
                ex.price_to_precision(
                    simbolo, ultimo * (1.0 + PRECO_ABERTURA_LIMITE_OFFSET)
                )
            )
        else:
            preco_limite = float(
                ex.price_to_precision(
                    simbolo, ultimo * (1.0 - PRECO_ABERTURA_LIMITE_OFFSET)
                )
            )

        try:
            order = ex.create_order(
                simbolo,
                "limit",
                side,
                amt,
                preco_limite,
                params_gtc,
            )
        except ccxt.BaseError as e:
            print(f"{_TAG} ❌ Erro no Chase (create_order): {e}", file=sys.stderr)
            continue

        oid = order.get("id")
        if oid is None:
            raise RuntimeError(f"{_TAG} Ordem sem id após create_order.")

        filled0 = float(order.get("filled") or 0)
        st0 = (order.get("status") or "").lower()
        if filled0 >= amt * 0.97 or st0 in ("closed", "filled"):
            print(f"{_TAG} ✅ [SUCESSO] {nome_lado}: fill na resposta da bolsa.")
            return dict(order)

        print(
            f"{_TAG} ⏳ [CHASE {rnd}/{cap}] Ordem LIMIT em {preco_limite}. "
            f"Aguardando {wait:.0f}s..."
        )
        time.sleep(wait)

        try:
            check = ex.fetch_order(oid, simbolo)
        except ccxt.BaseError as ef:
            print(f"{_TAG} fetch_order: {ef}", file=sys.stderr)
            check = order

        filled = float(check.get("filled") or 0)
        status = (check.get("status") or "").lower()
        if filled >= amt * 0.97 or status in ("closed", "filled"):
            print(f"{_TAG} ✅ [SUCESSO] Ordem preenchida (ronda {rnd}).")
            return dict(check)

        try:
            ex.cancel_order(oid, simbolo)
        except ccxt.BaseError as ec:
            print(f"{_TAG} cancel chase (ok se já executou): {ec}", file=sys.stderr)

        print(
            f"{_TAG} ⚠️ [TIMEOUT] Preço fugiu — recalculando chase ({rnd}/{cap})..."
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


def _usdt_de_balance_unificado(bal: dict[str, Any]) -> float:
    """
    Extrai USDT do dict devolvido por `fetch_balance`: preferência `free` (disponível),
    senão `total` (saldo líquido na conta de futuros).
    """
    usdt = bal.get("USDT")
    if usdt is None:
        return 0.0
    if isinstance(usdt, dict):
        livre = usdt.get("free")
        total = usdt.get("total")
        if livre is not None:
            return float(livre)
        if total is not None:
            return float(total)
        return 0.0
    try:
        return float(usdt)
    except (TypeError, ValueError):
        return 0.0


def obter_saldo_usdt_margem(exchange: ccxt.binance | None = None) -> float:
    """Saldo USDT na carteira de Futuros USDT-M — `fetch_balance` explícito para `type: future`."""
    ex = exchange or criar_exchange_binance()
    try:
        bal = ex.fetch_balance(params={"type": "future"})
        return _usdt_de_balance_unificado(bal)
    except ccxt.BaseError as e:
        print(f"{_TAG} Erro ao obter saldo USDT (futures): {e}", file=sys.stderr)
        raise


def abrir_long_market(
    simbolo: str,
    exchange: ccxt.binance | None = None,
    *,
    alavancagem: float | None = None,
    notional_usdt_override: float | None = None,
    trailing_callback_rate: float | None = None,
    trailing_activation_multiplier: float | None = None,
) -> dict[str, Any]:
    """
    Abre **long** com LIMIT **GTC** + **chase**: preço = último × (1 + offset 0,05%);
    após ~CHASE_ENTRADA_TIMEOUT_S sem fill, cancela e reabre no novo último.
    Notional = 15%×banca×alav; depois bracket (SL fixo + trailing stop).
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    lev = float(alavancagem) if alavancagem is not None else float(ALAVANCAGEM_REF_LOG_PADRAO)
    if notional_usdt_override is not None:
        quantidade_usd = float(notional_usdt_override)
    else:
        quantidade_usd = notional_usdt_futuros_position_sizing(ex, lev)
    if quantidade_usd <= 0:
        raise ValueError(
            f"{_TAG} Notional inválido ({quantidade_usd:.4f} USDT). Verifique saldo em margem."
        )
    amt = _quantidade_base_a_partir_de_usd(ex, sym, quantidade_usd)

    print(
        f"{_TAG} Abrir LONG {sym} — notional ~{quantidade_usd:.2f} USDT "
        f"(15% banca × alav {lev:g}x; qty base ≈ {amt}); chase até {CHASE_ENTRADA_MAX_ROUNDS}×/"
        f"{CHASE_ENTRADA_TIMEOUT_S:.0f}s..."
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
    trailing_callback_rate: float | None = None,
    trailing_activation_multiplier: float | None = None,
) -> dict[str, Any]:
    """
    Abre **short** com LIMIT GTC + chase (−0,05% vs. último); mesmo fluxo que o long.
    Depois **reduce-only** STOP_MARKET (SL) e TRAILING_STOP_MARKET.
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    lev = float(alavancagem) if alavancagem is not None else float(ALAVANCAGEM_REF_LOG_PADRAO)
    if notional_usdt_override is not None:
        quantidade_usd = float(notional_usdt_override)
    else:
        quantidade_usd = notional_usdt_futuros_position_sizing(ex, lev)
    if quantidade_usd <= 0:
        raise ValueError(
            f"{_TAG} Notional inválido ({quantidade_usd:.4f} USDT). Verifique saldo em margem."
        )
    amt = _quantidade_base_a_partir_de_usd(ex, sym, quantidade_usd)

    print(
        f"{_TAG} Abrir SHORT {sym} — notional ~{quantidade_usd:.2f} USDT "
        f"(15% banca × alav {lev:g}x; qty base ≈ {amt}); "
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
    Cancela ordens abertas no símbolo (TP/SL órfãs) e fecha a posição a mercado.
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

    # Long: vender; Short: comprar de volta.
    if lado == "short" or contratos < 0:
        print(f"{_TAG} Fechando SHORT em {sym} — compra MARKET qty={qty}...")
        ordem = ex.create_order(sym, "market", "buy", qty, None, {})
    else:
        print(f"{_TAG} Fechando LONG em {sym} — venda MARKET qty={qty}...")
        ordem = ex.create_order(sym, "market", "sell", qty, None, {})

    print(f"{_TAG} Posição encerrada. id={ordem.get('id')} status={ordem.get('status')}")
    try:
        cancelar_todas_ordens_abertas(simbolo, ex)
    except ccxt.BaseError as e2:
        print(f"{_TAG} Pós-fecho: cancelamento extra: {e2}", file=sys.stderr)
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
    for p in posicoes:
        c = float(p.get("contracts") or 0)
        if abs(c) > 0:
            contratos = c
            lado = str(p.get("side") or "")
            break

    aberta = abs(contratos) > 0 and abs(contratos) >= (min_amt * 0.5 if min_amt else 1e-12)

    return {
        "base": base,
        "contratos": contratos,
        "lado": lado,
        "saldo_base_livre": abs(contratos),
        "min_quantidade_base": min_amt,
        "posicao_aberta": aberta,
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
