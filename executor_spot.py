"""
executor_spot — Binance **Spot** MAINNET via ccxt.

Compra a mercado por custo em USDT; venda total do saldo base.
Logs em português com tag [MAINNET SPOT].
"""

from __future__ import annotations

import os
import sys
from typing import Any

import ccxt
from dotenv import load_dotenv

load_dotenv()

_TAG = "[MAINNET SPOT]"


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
    valor_usd: float,
    exchange: ccxt.binance | None = None,
) -> dict[str, Any]:
    """Compra **market** gastando aproximadamente `valor_usd` em USDT (Binance Spot)."""
    ex = exchange or criar_exchange_binance()
    ex.load_markets()
    print(f"{_TAG} Compra MARKET {simbolo} — custo alvo ~{valor_usd:.2f} USDT...")
    try:
        ordem = ex.create_market_buy_order_with_cost(simbolo, valor_usd)
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
