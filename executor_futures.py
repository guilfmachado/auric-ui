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

# Gestão de risco (frações): TP/SL em relação ao preço de entrada.
LONG_TP = 0.020
LONG_SL = 0.010
SHORT_TP = 0.015
SHORT_SL = 0.008

# Alavancagem usada só no log de liquidação aproximada (ajuste em configurar_alavancagem / main).
ALAVANCAGEM_REF_LOG_PADRAO = 3


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


def _quantidade_base_a_partir_de_usd(
    exchange: ccxt.binance,
    simbolo: str,
    quantidade_usd: float,
) -> float:
    """Converte notional em USDT para quantidade na base (contratos ETH), com precisão."""
    if quantidade_usd <= 0:
        raise ValueError("quantidade_usd deve ser positiva.")
    ticker = exchange.fetch_ticker(simbolo)
    preco = float(ticker.get("last") or ticker.get("close") or 0)
    if preco <= 0:
        raise RuntimeError("Preço inválido para calcular quantidade.")
    q = quantidade_usd / preco
    return float(exchange.amount_to_precision(simbolo, q))


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
    """Após ordem a mercado, obtém quantidade (contratos) e preço médio de entrada."""
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
) -> tuple[dict[str, Any], dict[str, Any]]:
    """LIMIT take-profit (sell) + STOP_MARKET stop-loss (sell), reduce-only."""
    q = float(exchange.amount_to_precision(simbolo, qty))
    tp_raw = preco_entrada * (1.0 + LONG_TP)
    sl_raw = preco_entrada * (1.0 - LONG_SL)
    tp_p = float(exchange.price_to_precision(simbolo, tp_raw))
    sl_p = float(exchange.price_to_precision(simbolo, sl_raw))

    p_ro = _params_reduce_futures()
    ord_tp = exchange.create_order(
        simbolo,
        "limit",
        "sell",
        q,
        tp_p,
        {**p_ro, "timeInForce": "GTC"},
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
        f"{_TAG} Bracket LONG: TP LIMIT sell @ {tp_p} (+{LONG_TP:.1%}), "
        f"SL STOP_MARKET @ {sl_p} (−{LONG_SL:.1%}), qty={q}"
    )
    return ord_tp, ord_sl


def _criar_bracket_short(
    exchange: ccxt.binance,
    simbolo: str,
    qty: float,
    preco_entrada: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """LIMIT take-profit (buy) + STOP_MARKET stop-loss (buy), reduce-only."""
    q = float(exchange.amount_to_precision(simbolo, qty))
    tp_raw = preco_entrada * (1.0 - SHORT_TP)
    sl_raw = preco_entrada * (1.0 + SHORT_SL)
    tp_p = float(exchange.price_to_precision(simbolo, tp_raw))
    sl_p = float(exchange.price_to_precision(simbolo, sl_raw))

    p_ro = _params_reduce_futures()
    ord_tp = exchange.create_order(
        simbolo,
        "limit",
        "buy",
        q,
        tp_p,
        {**p_ro, "timeInForce": "GTC"},
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
        f"{_TAG} Bracket SHORT: TP LIMIT buy @ {tp_p} (−{SHORT_TP:.1%}), "
        f"SL STOP_MARKET @ {sl_p} (+{SHORT_SL:.1%}), qty={q}"
    )
    return ord_tp, ord_sl


def obter_saldo_usdt_margem(exchange: ccxt.binance | None = None) -> float:
    """Saldo USDT disponível na carteira de Futuros (USDT-M)."""
    ex = exchange or criar_exchange_binance()
    try:
        bal = ex.fetch_balance()
        usdt = bal.get("USDT") or {}
        livre = usdt.get("free")
        if livre is None:
            return 0.0
        return float(livre)
    except ccxt.BaseError as e:
        print(f"{_TAG} Erro ao obter saldo USDT (futures): {e}", file=sys.stderr)
        raise


def abrir_long_market(
    simbolo: str,
    quantidade_usd: float,
    exchange: ccxt.binance | None = None,
    *,
    alavancagem: float | None = None,
) -> dict[str, Any]:
    """
    Abre **long** a mercado com notional aproximado `quantidade_usd` em USDT.
    Em seguida coloca **reduce-only**: LIMIT (TP) e STOP_MARKET (SL).
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    amt = _quantidade_base_a_partir_de_usd(ex, sym, quantidade_usd)
    lev = float(alavancagem) if alavancagem is not None else float(ALAVANCAGEM_REF_LOG_PADRAO)

    print(
        f"{_TAG} Abrindo LONG MARKET em {sym} — notional ~{quantidade_usd:.2f} USDT "
        f"(qty base ≈ {amt})..."
    )
    try:
        ordem = ex.create_order(sym, "market", "buy", amt, None, {})
        print(f"{_TAG} Long aceito. id={ordem.get('id')} status={ordem.get('status')}")
        qty_pos, preco_ent = _aguardar_qty_e_preco_entrada(ex, sym)
        _log_liquidacao_estimada(preco_ent, "LONG", lev)
        ord_tp, ord_sl = _criar_bracket_long(ex, sym, qty_pos, preco_ent)
        out = dict(ordem)
        out["auric_take_profit"] = ord_tp
        out["auric_stop_loss"] = ord_sl
        out["auric_entry_qty"] = qty_pos
        out["auric_entry_price"] = preco_ent
        return out
    except Exception as e:
        print(f"{_TAG} Erro ao abrir long / bracket: {e}", file=sys.stderr)
        raise


def abrir_short_market(
    simbolo: str,
    quantidade_usd: float,
    exchange: ccxt.binance | None = None,
    *,
    alavancagem: float | None = None,
) -> dict[str, Any]:
    """
    Abre **short** a mercado; depois **reduce-only** LIMIT (TP) e STOP_MARKET (SL)
    com lógica de preço invertida em relação ao long.
    """
    ex = exchange or criar_exchange_binance()
    sym = _resolver_simbolo_perp(ex, simbolo)
    amt = _quantidade_base_a_partir_de_usd(ex, sym, quantidade_usd)
    lev = float(alavancagem) if alavancagem is not None else float(ALAVANCAGEM_REF_LOG_PADRAO)

    print(
        f"{_TAG} Abrindo SHORT MARKET em {sym} — notional ~{quantidade_usd:.2f} USDT "
        f"(qty base ≈ {amt})..."
    )
    try:
        ordem = ex.create_order(sym, "market", "sell", amt, None, {})
        print(f"{_TAG} Short aceito. id={ordem.get('id')} status={ordem.get('status')}")
        qty_pos, preco_ent = _aguardar_qty_e_preco_entrada(ex, sym)
        _log_liquidacao_estimada(preco_ent, "SHORT", lev)
        ord_tp, ord_sl = _criar_bracket_short(ex, sym, qty_pos, preco_ent)
        out = dict(ordem)
        out["auric_take_profit"] = ord_tp
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


# --- Compatibilidade com código legado (Spot) — redireciona para Futures ---

def consultar_posicao_spot(
    simbolo: str,
    exchange: ccxt.binance | None = None,
) -> dict[str, Any]:
    """Alias: usa posição em **Futures** USDT-M (nome histórico)."""
    return consultar_posicao_futures(simbolo, exchange)


def executar_compra_spot_market(
    simbolo: str,
    valor_usd: float,
    exchange: ccxt.binance | None = None,
    *,
    alavancagem: float | None = None,
) -> dict[str, Any]:
    """Alias: abre **long** a mercado em USDT-M (nome histórico 'Spot')."""
    return abrir_long_market(simbolo, valor_usd, exchange, alavancagem=alavancagem)


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
