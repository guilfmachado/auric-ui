"""
executor_spot — Binance **Spot** MAINNET via ccxt.

Compra em LIMIT (teto +0,05% vs. último) por custo alvo em USDT; venda total do saldo base a mercado.
Logs em português com tag [MAINNET SPOT].
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import ccxt
from dotenv import load_dotenv

load_dotenv()

_TAG = "[MAINNET SPOT]"

# Spot: custo em USDT = saldo_USDT × PERCENTUAL_BANCA (sem alavancagem).
PERCENTUAL_BANCA = 0.15
PRECO_ABERTURA_LIMITE_OFFSET = 0.0005  # 0,05% acima do último (compra)
CHASE_ENTRADA_TIMEOUT_S = float(os.getenv("AURIC_CHASE_TIMEOUT_S", "15"))
CHASE_ENTRADA_MAX_ROUNDS = int(os.getenv("AURIC_CHASE_MAX_ROUNDS", "3"))


def _carregar_chaves() -> tuple[str, str]:
    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError(
            "Defina BINANCE_API_KEY e BINANCE_API_SECRET no .env (nomes iguais às variáveis)."
        )
    return api_key, api_secret


def criar_exchange_binance() -> ccxt.binance:
    """Cliente Binance **Spot** (defaultType = spot), MAINNET."""
    api_key, api_secret = _carregar_chaves()
    exchange = ccxt.binance(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )
    exchange.set_sandbox_mode(False)
    return exchange


def _compra_limit_chase_spot(
    ex: ccxt.binance,
    simbolo: str,
    amt: float,
) -> dict[str, Any]:
    """LIMIT buy GTC + offset 0,05%; sleep + fetch; cancela e reabre (igual futuros)."""
    params_gtc: dict[str, Any] = {"timeInForce": "GTC"}
    cap = CHASE_ENTRADA_MAX_ROUNDS

    for rnd in range(1, cap + 1):
        ticker = ex.fetch_ticker(simbolo)
        ultimo = float(ticker.get("last") or ticker.get("close") or 0)
        if ultimo <= 0:
            raise RuntimeError(f"{_TAG} Preço inválido (chase {rnd}).")
        preco_limite = float(
            ex.price_to_precision(
                simbolo, ultimo * (1.0 + PRECO_ABERTURA_LIMITE_OFFSET)
            )
        )

        try:
            order = ex.create_order(
                simbolo, "limit", "buy", amt, preco_limite, params_gtc
            )
        except ccxt.BaseError as e:
            print(f"{_TAG} ❌ Erro no Chase (create_order): {e}", file=sys.stderr)
            continue

        oid = order.get("id")
        if oid is None:
            raise RuntimeError(f"{_TAG} Ordem sem id.")

        filled0 = float(order.get("filled") or 0)
        st0 = (order.get("status") or "").lower()
        if filled0 >= amt * 0.97 or st0 in ("closed", "filled"):
            print(f"{_TAG} ✅ [SUCESSO] fill na resposta da bolsa.")
            return dict(order)

        print(
            f"{_TAG} ⏳ [CHASE {rnd}/{cap}] Ordem LIMIT em {preco_limite}. "
            f"Aguardando {CHASE_ENTRADA_TIMEOUT_S:.0f}s..."
        )
        time.sleep(CHASE_ENTRADA_TIMEOUT_S)

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

    raise RuntimeError(f"{_TAG} Compra LIMIT: esgotadas {cap} tentativas de chase.")


def consultar_posicao_spot(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> dict[str, Any]:
    """
    Saldo livre do ativo base no par spot; `posicao_aberta` se >= mínimo negociável.
    """
    ex = exchange or criar_exchange_binance()
    ex.load_markets()
    if simbolo not in ex.markets:
        raise ValueError(f"{_TAG} Par não encontrado: {simbolo}")
    market = ex.markets[simbolo]
    base = market["base"]
    min_amt = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0)

    bal = ex.fetch_balance()
    coin = bal.get(base) or {}
    livre = float(coin.get("free") or 0)

    aberta = livre > 0 and (min_amt <= 0 or livre >= min_amt * 0.5)

    return {
        "base": base,
        "contratos": 0.0,
        "lado": "",
        "saldo_base_livre": livre,
        "min_quantidade_base": min_amt,
        "posicao_aberta": aberta,
    }


def executar_compra_spot_market(
    simbolo: str,
    valor_usd: float | None = None,
    exchange: ccxt.binance | None = None,
) -> dict[str, Any]:
    """
    Compra **limit** GTC com chase: teto = último × (1 + offset 0,05%); sleep + cancel + reabre.
    Quantidade base ≈ custo_alvo / preço de referência inicial. Se `valor_usd` for None, custo = saldo × 15%.
    """
    ex = exchange or criar_exchange_binance()
    ex.load_markets()
    if valor_usd is None:
        saldo = obter_saldo_usdt(ex)
        valor_usd = float(saldo) * PERCENTUAL_BANCA
    if valor_usd <= 0:
        raise ValueError(f"{_TAG} Custo alvo inválido ({valor_usd}) — verifique saldo USDT (spot).")
    ticker = ex.fetch_ticker(simbolo)
    preco_ref = float(ticker.get("last") or ticker.get("close") or 0)
    if preco_ref <= 0:
        raise RuntimeError(f"{_TAG} Preço inválido para compra limit em {simbolo}.")
    preco_lim0 = float(
        ex.price_to_precision(simbolo, preco_ref * (1.0 + PRECO_ABERTURA_LIMITE_OFFSET))
    )
    q_raw = valor_usd / preco_lim0
    amt = float(ex.amount_to_precision(simbolo, q_raw))
    print(
        f"{_TAG} Compra LIMIT {simbolo} — custo alvo ~{valor_usd:.2f} USDT (15% banca), "
        f"qty base ≈ {amt}; chase até {CHASE_ENTRADA_MAX_ROUNDS}×/{CHASE_ENTRADA_TIMEOUT_S:.0f}s..."
    )
    try:
        ordem = _compra_limit_chase_spot(ex, simbolo, amt)
        print(f"{_TAG} Compra aceita. id={ordem.get('id')} status={ordem.get('status')}")
        return ordem
    except ccxt.BaseError as e:
        print(f"{_TAG} Erro na compra: {e}", file=sys.stderr)
        raise


def executar_venda_spot_total(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> dict[str, Any]:
    """Vende **toda** a quantidade livre do ativo base no par spot (market sell)."""
    ex = exchange or criar_exchange_binance()
    ex.load_markets()
    if simbolo not in ex.markets:
        raise ValueError(f"{_TAG} Par não encontrado: {simbolo}")
    base = ex.markets[simbolo]["base"]
    bal = ex.fetch_balance()
    livre = float((bal.get(base) or {}).get("free") or 0)
    if livre <= 0:
        msg = f"{_TAG} Sem saldo {base} para vender."
        print(msg, file=sys.stderr)
        raise ccxt.InvalidOrder(msg)

    amt = float(ex.amount_to_precision(simbolo, livre))
    print(f"{_TAG} Venda MARKET total {simbolo} — qty={amt} {base}...")
    try:
        ordem = ex.create_market_sell_order(simbolo, amt)
        print(f"{_TAG} Venda aceita. id={ordem.get('id')} status={ordem.get('status')}")
        return ordem
    except ccxt.BaseError as e:
        print(f"{_TAG} Erro na venda: {e}", file=sys.stderr)
        raise


def obter_saldo_usdt(exchange: ccxt.binance | None = None) -> float:
    """Saldo USDT livre na carteira spot."""
    ex = exchange or criar_exchange_binance()
    try:
        bal = ex.fetch_balance()
        usdt = bal.get("USDT") or {}
        livre = usdt.get("free")
        if livre is None:
            return 0.0
        return float(livre)
    except ccxt.BaseError as e:
        print(f"{_TAG} Erro ao obter saldo USDT: {e}", file=sys.stderr)
        raise


def configurar_alavancagem(*_args: Any, **_kwargs: Any) -> None:
    """No-op no Spot (API de futuros não se aplica)."""
    return None
