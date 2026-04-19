"""
Maestro do sistema: estado global de posição → ML + sentimento → execução Spot ou Futures (Supabase) → Supabase.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv(override=True)

import argparse
import json
import time
import traceback
import urllib.error
import urllib.request
from typing import Any

import ccxt

import brain
import executor_futures
import executor_spot
import intelligence_hub
import logger
import ml_model

SYMBOL_TRADE = "ETH/USDT"
VALOR_COMPRA_USDT = 15.0
ALAVANCAGEM_PADRAO = 3
# Multiplicadores de saída sobre o preço de entrada gravado em memória (LONG: sobe = lucro).
FATOR_TAKE_PROFIT = 1.02  # preço >= entrada × 1.02
FATOR_STOP_LOSS = 0.99  # preço <= entrada × 0.99
# SHORT (futures): lucro se preço cai; TP −2% / SL +1% sobre o preço de entrada.
FATOR_TAKE_PROFIT_SHORT = 0.98  # preço <= entrada × 0.98
FATOR_STOP_LOSS_SHORT = 1.01  # preço >= entrada × 1.01

# Zona ML que aciona Hub + Brain: P(alta) ≥ limiar long OU P(alta) ≤ limiar short.
ML_PROB_SHORT_MAX = 0.40

# Mínimo de confiança do Brain (0–100) para permitir entrada.
CONFIANCA_BRAIN_MIN_ENTRADA = 70

# --- Estado do bot (memória do processo; reinício do script perde o estado) ---
posicao_aberta: bool = False
preco_compra: float = 0.0
direcao_posicao: str = "LONG"  # "LONG" | "SHORT" — lado da posição aberta (vigia / TP-SL)
_last_modo_detectado: str | None = None


def obter_modo_operacao() -> str:
    """Consulta no Supabase se o bot deve operar em SPOT ou FUTURES."""
    if logger.supabase is None:
        return "FUTURES"
    try:
        res = (
            logger.supabase.table("config")
            .select("modo_operacao")
            .eq("id", 1)
            .execute()
        )
        if not res.data:
            return "FUTURES"
        row = res.data[0]
        modo = row.get("modo_operacao")
        if modo in ("SPOT", "FUTURES"):
            return modo
        return "FUTURES"
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ obter_modo_operacao: {e}")
        return "FUTURES"


def verificar_permissao_operacao() -> bool:
    """
    Pedágio: lê `bot_control.is_active` (id=1). Sem Supabase ou erro → False.
    """
    if logger.supabase is None:
        print("⚠️ [ERRO] Falha ao ler permissão: Supabase não configurado (SUPABASE_URL / SUPABASE_KEY).")
        return False
    try:
        res = (
            logger.supabase.table("bot_control")
            .select("is_active")
            .eq("id", 1)
            .single()
            .execute()
        )
        data = res.data
        if not isinstance(data, dict):
            return False
        return bool(data.get("is_active", False))
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ [ERRO] Falha ao ler permissão: {e}")
        return False


def _simbolo_binance_rest(simbolo: str) -> str:
    """ETH/USDT ou ETH/USDT:USDT → ETHUSDT (formato query Binance)."""
    s = simbolo.strip().upper().replace(":USDT", "")
    if "/" in s:
        a, b = s.split("/", 1)
        return f"{a}{b}"
    return s.replace("/", "")


def _preco_via_ccxt_ticker(simbolo: str, modo: str) -> float:
    """Fallback: pedido ao mercado via ccxt (também rede ao vivo; sem cache no nosso código)."""
    if modo == "FUTURES":
        ex = ccxt.binance(
            {"enableRateLimit": True, "options": {"defaultType": "future"}}
        )
        sym = simbolo
        if ":USDT" not in simbolo and "/USDT" in simbolo:
            sym = f"{simbolo.split('/')[0]}/USDT:USDT"
        t = ex.fetch_ticker(sym)
    else:
        ex = ccxt.binance({"enableRateLimit": True})
        t = ex.fetch_ticker(simbolo)
    last = t.get("last") or t.get("close")
    return float(last) if last is not None else 0.0


def obter_preco_atual(simbolo: str = SYMBOL_TRADE, modo: str = "FUTURES") -> float:
    """
    Preço de mercado ao vivo: cada chamada abre um pedido HTTP novo à API pública Binance
    (REST), sem variáveis globais nem cache — adequado a ser chamado a cada ciclo.

    FUTURES USDT-M: fapi.binance.com/fapi/v1/ticker/price
    SPOT: api.binance.com/api/v3/ticker/price
    """
    sym_rest = _simbolo_binance_rest(simbolo)
    if modo == "FUTURES":
        url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={sym_rest}"
    else:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={sym_rest}"

    preco: float
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "AuricMaestro/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
        preco = float(payload["price"])
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        OSError,
        KeyError,
        ValueError,
        json.JSONDecodeError,
    ) as e:
        print(
            f"[obter_preco_atual] REST Binance falhou ({e!s}); a usar ccxt como fallback."
        )
        preco = _preco_via_ccxt_ticker(simbolo, modo)

    print(f"DEBUG: Preço capturado agora: {preco}")
    return preco


def obter_preco_referencia(simbolo: str = SYMBOL_TRADE, modo: str = "FUTURES") -> float:
    """Alias: mesmo comportamento que obter_preco_atual (preço vivo por ciclo)."""
    return obter_preco_atual(simbolo, modo)


def _sincronizar_estado_com_carteira(ex: Any, ex_mod: Any) -> None:
    """
    Evita divergência: se há posição (spot ou contratos) mas o estado em RAM foi perdido,
    tenta recuperar o preço de entrada pelo último log de abertura no Supabase.
    """
    global posicao_aberta, preco_compra, direcao_posicao

    snap = ex_mod.consultar_posicao_spot(SYMBOL_TRADE, ex)

    if snap["posicao_aberta"] and not posicao_aberta:
        entrada, lado = logger.obter_preco_entrada_ultima_posicao(SYMBOL_TRADE)
        if entrada and entrada > 0:
            posicao_aberta = True
            preco_compra = float(entrada)
            direcao_posicao = lado if lado in ("LONG", "SHORT") else "LONG"
            print(
                f"[Estado] Sincronizado: carteira com posição ({direcao_posicao}) — "
                f"preco ref. entrada = {preco_compra:.4f} USDT"
            )
        else:
            print(
                "[Estado] Aviso: há posição, mas não há log de abertura (COMPRA_LONG / ABRE_SHORT) "
                "no Supabase. Ajuste manual ou opere até zerar; TP/SL podem falhar."
            )

    if not snap["posicao_aberta"] and posicao_aberta:
        print("[Estado] Carteira sem posição — resetando posicao_aberta / preco_compra.")
        posicao_aberta = False
        preco_compra = 0.0
        direcao_posicao = "LONG"


def _gerenciar_saida_modo_vigia(
    preco: float,
    ex: Any,
    ex_mod: Any,
    modo_label: str,
) -> None:
    """
    [Modo Vigia] Com posição aberta: TP/SL sobre preco_compra (LONG: alta=lucro; SHORT: queda=lucro).
    """
    global posicao_aberta, preco_compra, direcao_posicao

    if preco_compra <= 0:
        print("[Modo Vigia] preco_compra inválido — não é possível avaliar TP/SL.")
        return

    if direcao_posicao == "SHORT":
        limite_tp = preco_compra * FATOR_TAKE_PROFIT_SHORT
        limite_sl = preco_compra * FATOR_STOP_LOSS_SHORT
        print(
            f"\n  Posição SHORT | ref. entrada: {preco_compra:.4f} USDT | atual: {preco:.4f} ({modo_label})\n"
            f"  TP (lucro se cair ~2%): <= {limite_tp:.4f}  |  SL (perda se subir ~1%): >= {limite_sl:.4f}"
        )
        if preco <= limite_tp:
            print("\n  [Take Profit SHORT] Preço <= entrada × 0,98 — fechando posição...")
            try:
                ordem = ex_mod.executar_venda_spot_total(SYMBOL_TRADE, ex)
                just = (
                    f"TP short −2%: entrada {preco_compra:.4f}, ref. {preco:.4f}, "
                    f"id={ordem.get('id')}."
                )
                logger.registrar_log_trade(
                    ativo=SYMBOL_TRADE,
                    preco=preco,
                    prob_ml=0.0,
                    sentimento="—",
                    acao="VENDA_PROFIT",
                    justificativa=just,
                    lado_ordem="SHORT",
                )
                posicao_aberta = False
                preco_compra = 0.0
                direcao_posicao = "LONG"
                print("  [Estado] posição encerrada após TP (short).")
            except Exception as e_v:  # noqa: BLE001
                print(f"  [ERRO] Saída TP short: {e_v}")
                logger.registrar_log_trade(
                    ativo=SYMBOL_TRADE,
                    preco=preco,
                    prob_ml=0.0,
                    sentimento="—",
                    acao="ERRO_VENDA_TP",
                    justificativa=str(e_v),
                    lado_ordem="SHORT",
                )
            return

        if preco >= limite_sl:
            print("\n  [Stop Loss SHORT] Preço >= entrada × 1,01 — fechando posição...")
            try:
                ordem = ex_mod.executar_venda_spot_total(SYMBOL_TRADE, ex)
                just = (
                    f"SL short +1%: entrada {preco_compra:.4f}, ref. {preco:.4f}, "
                    f"id={ordem.get('id')}."
                )
                logger.registrar_log_trade(
                    ativo=SYMBOL_TRADE,
                    preco=preco,
                    prob_ml=0.0,
                    sentimento="—",
                    acao="VENDA_STOP",
                    justificativa=just,
                    lado_ordem="SHORT",
                )
                posicao_aberta = False
                preco_compra = 0.0
                direcao_posicao = "LONG"
                print("  [Estado] posição encerrada após SL (short).")
            except Exception as e_v:  # noqa: BLE001
                print(f"  [ERRO] Saída SL short: {e_v}")
                logger.registrar_log_trade(
                    ativo=SYMBOL_TRADE,
                    preco=preco,
                    prob_ml=0.0,
                    sentimento="—",
                    acao="ERRO_VENDA_SL",
                    justificativa=str(e_v),
                    lado_ordem="SHORT",
                )
            return

        print(
            "\n  [Modo Vigia SHORT] Preço na faixa (nem TP nem SL). "
            "Aguardando próximo ciclo."
        )
        return

    limite_tp = preco_compra * FATOR_TAKE_PROFIT
    limite_sl = preco_compra * FATOR_STOP_LOSS
    print(
        f"\n  Referência entrada LONG: {preco_compra:.4f} USDT | atual: {preco:.4f} USDT ({modo_label})\n"
        f"  Gatilho TP: >= {limite_tp:.4f}  |  Gatilho SL: <= {limite_sl:.4f}"
    )

    if preco >= limite_tp:
        print("\n  [Take Profit] Preço atual >= entrada × 1,02 — executando saída total...")
        try:
            ordem = ex_mod.executar_venda_spot_total(SYMBOL_TRADE, ex)
            just = (
                f"TP +2%: entrada {preco_compra:.4f}, saída ref. {preco:.4f}, "
                f"ordem id={ordem.get('id')}."
            )
            logger.registrar_log_trade(
                ativo=SYMBOL_TRADE,
                preco=preco,
                prob_ml=0.0,
                sentimento="—",
                acao="VENDA_PROFIT",
                justificativa=just,
            )
            posicao_aberta = False
            preco_compra = 0.0
            direcao_posicao = "LONG"
            print("  [Estado] posicao_aberta=False | preco_compra zerado após TP.")
        except Exception as e_v:  # noqa: BLE001
            print(f"  [ERRO] Saída TP: {e_v}")
            logger.registrar_log_trade(
                ativo=SYMBOL_TRADE,
                preco=preco,
                prob_ml=0.0,
                sentimento="—",
                acao="ERRO_VENDA_TP",
                justificativa=str(e_v),
            )
        return

    if preco <= limite_sl:
        print("\n  [Stop Loss] Preço atual <= entrada × 0,99 — executando saída total...")
        try:
            ordem = ex_mod.executar_venda_spot_total(SYMBOL_TRADE, ex)
            just = (
                f"SL -1%: entrada {preco_compra:.4f}, saída ref. {preco:.4f}, "
                f"ordem id={ordem.get('id')}."
            )
            logger.registrar_log_trade(
                ativo=SYMBOL_TRADE,
                preco=preco,
                prob_ml=0.0,
                sentimento="—",
                acao="VENDA_STOP",
                justificativa=just,
            )
            posicao_aberta = False
            preco_compra = 0.0
            direcao_posicao = "LONG"
            print("  [Estado] posicao_aberta=False | preco_compra zerado após SL.")
        except Exception as e_v:  # noqa: BLE001
            print(f"  [ERRO] Saída SL: {e_v}")
            logger.registrar_log_trade(
                ativo=SYMBOL_TRADE,
                preco=preco,
                prob_ml=0.0,
                sentimento="—",
                acao="ERRO_VENDA_SL",
                justificativa=str(e_v),
            )
        return

    print(
        "\n  [Modo Vigia] Preço dentro da faixa (nem TP nem SL). "
        "Aguardando próximo ciclo para reavaliar."
    )


def rodar_ciclo(modo: str) -> None:
    """Um ciclo: Vigia (saída) OU Buscando Oportunidade (entrada ML + sentimento)."""
    global posicao_aberta, preco_compra, direcao_posicao, _last_modo_detectado

    if not logger.obter_bot_ativo():
        print("Bot em modo Standby via Dashboard")
        return

    ex_mod = executor_futures if modo == "FUTURES" else executor_spot
    modo_label = "FUTURES USDT-M" if modo == "FUTURES" else "SPOT"

    if modo == "FUTURES" and _last_modo_detectado != "FUTURES":
        try:
            ex_cfg = executor_futures.criar_exchange_binance()
            executor_futures.configurar_alavancagem(
                SYMBOL_TRADE, ALAVANCAGEM_PADRAO, ex_cfg
            )
        except Exception as e_cfg:  # noqa: BLE001
            print(f"[Maestro] Aviso ao configurar alavancagem/margem: {e_cfg}")
    _last_modo_detectado = modo

    limiar = ml_model.CONFIDENCE_THRESHOLD
    preco = obter_preco_referencia(SYMBOL_TRADE, modo)
    ex = ex_mod.criar_exchange_binance()

    _sincronizar_estado_com_carteira(ex, ex_mod)

    print("\n" + "=" * 64)
    print(f"[Ciclo] {SYMBOL_TRADE} | modo={modo_label} | preço ref. ≈ {preco:.4f} USDT")
    print(f"        Estado RAM: posicao_aberta={posicao_aberta} | preco_compra={preco_compra:.4f}")
    print("=" * 64)

    if posicao_aberta:
        print("\n>>> MODO VIGIA <<<")
        print(
            "    Posição aberta: TP/SL — LONG +2% / −1% | SHORT (fut.) −2% / +1% no preço de entrada."
        )
        _gerenciar_saida_modo_vigia(preco, ex, ex_mod, modo_label)
        return

    print("\n>>> BUSCANDO OPORTUNIDADE <<<")
    print(
        "    ML P(alta)≥0,60 (LONG) ou P(alta)≤0,40 (SHORT) → Hub → Brain → posicao_recomendada."
    )

    print("\n--- [AURIC CYCLE START] ---")

    # 1. Sinal Técnico (XGBoost)
    probabilidade = ml_model.obter_sinal_atual()
    print(f"📊 [ML] Probabilidade de Alta: {probabilidade:.2%}")

    limiar_short = ML_PROB_SHORT_MAX
    if probabilidade > limiar_short and probabilidade < limiar:
        print(
            "\n    [Buscando Oportunidade] ML na zona neutra — sem Hub/Brain; "
            "aguardando próximo ciclo."
        )
        logger.registrar_log_trade(
            ativo=SYMBOL_TRADE,
            preco=preco,
            prob_ml=probabilidade,
            sentimento="—",
            acao="HOLD",
            justificativa=(
                f"P(alta)={probabilidade:.4f} ∈ ({limiar_short},{limiar}); sem ativação Hub/Brain."
            ),
        )
        return

    if probabilidade >= limiar:
        direcao_sugerida = "LONG"
    else:
        direcao_sugerida = "SHORT"

    print(f"    → direcao_sugerida={direcao_sugerida} (ML vs limiares long/short)")

    # 2. Ativa os Olhos (Intelligence Hub)
    print("📡 [HUB] Ativando Intelligence Hub (Nitter + RSS)...")
    hub = intelligence_hub.IntelligenceHub()
    contexto = hub.obter_contexto_agregado()
    print(contexto[:1200] + ("..." if len(contexto) > 1200 else ""))

    # 3. Ativa o Cérebro (Claude 3.5 Sonnet)
    print("🧠 [BRAIN] Consultando Claude 3.5 Sonnet (Final Boss Mode)...")
    veredito = brain.analisar_sentimento_mercado(
        contexto,
        prob_ml=probabilidade,
        limiar_ml=limiar,
        direcao_sugerida=direcao_sugerida,
        verbose=False,
    )

    sent = str(veredito.get("sentimento", "")).upper().strip()
    try:
        conf_num = int(float(veredito.get("confianca", 0)))
    except (TypeError, ValueError):
        conf_num = 0
    alerta = str(veredito.get("alerta_macro") or "").strip()
    just_ia = (
        veredito.get("justificativa_curta")
        or veredito.get("justificativa")
        or ""
    ).strip() or "(sem justificativa no JSON)"
    if alerta and alerta.lower() != "nenhum":
        just_ia = f"{just_ia} | alerta_macro: {alerta}"

    print(
        f"⚖️ [DECISION] Veredito: {veredito.get('sentimento', '—')} | "
        f"Confiança: {veredito.get('confianca')}%"
    )
    print(
        f"📝 [REASON] "
        f"{(veredito.get('justificativa_curta') or veredito.get('justificativa') or '—')}"
    )
    if alerta and alerta.lower() != "nenhum":
        print(f"      alerta_macro={alerta}")

    pos_rec = str(veredito.get("posicao_recomendada") or "VETO").upper().strip()
    if pos_rec not in ("LONG", "SHORT", "VETO"):
        pos_rec = "VETO"
    print(
        f"      posicao_recomendada={pos_rec} | direcao_sugerida={direcao_sugerida}"
    )

    # 4. Filtro de execução (entrada)
    if pos_rec == "VETO" or pos_rec != direcao_sugerida:
        print(
            "    [Buscando Oportunidade] Brain não confirma a direção técnica (VETO ou divergência)."
        )
        logger.registrar_log_trade(
            ativo=SYMBOL_TRADE,
            preco=preco,
            prob_ml=probabilidade,
            sentimento=sent,
            acao="VETO_POSICAO",
            justificativa=(
                f"{just_ia} | posicao_recomendada={pos_rec} vs direcao_sugerida={direcao_sugerida}"
            ),
        )
        return

    if conf_num < CONFIANCA_BRAIN_MIN_ENTRADA:
        print(
            f"    [Buscando Oportunidade] Sinal alinhado mas confiança {conf_num}% < "
            f"{CONFIANCA_BRAIN_MIN_ENTRADA}% — sem entrada."
        )
        logger.registrar_log_trade(
            ativo=SYMBOL_TRADE,
            preco=preco,
            prob_ml=probabilidade,
            sentimento=sent,
            acao="VETO_CONFIANCA_BRAIN",
            justificativa=f"{just_ia} (conf {conf_num}% < {CONFIANCA_BRAIN_MIN_ENTRADA}%)",
        )
        return

    if direcao_sugerida == "SHORT" and modo != "FUTURES":
        print(
            "    [Buscando Oportunidade] SHORT requer FUTURES — modo SPOT não abre venda a descoberto."
        )
        logger.registrar_log_trade(
            ativo=SYMBOL_TRADE,
            preco=preco,
            prob_ml=probabilidade,
            sentimento=sent,
            acao="VETO_SHORT_SPOT",
            justificativa=f"{just_ia} | ative FUTURES na config para operar SHORT.",
            lado_ordem="SHORT",
        )
        return

    if direcao_sugerida == "LONG":
        entrada_txt = (
            f"COMPRA (Long) MARKET ~{VALOR_COMPRA_USDT:.2f} USDT ({modo_label}, mainnet)."
        )
        print(
            f"    [Buscando Oportunidade] Confluência LONG + posicao_recomendada=LONG + "
            f"conf≥{CONFIANCA_BRAIN_MIN_ENTRADA}% — {entrada_txt}"
        )
        try:
            if modo == "FUTURES":
                ordem = ex_mod.executar_compra_spot_market(
                    SYMBOL_TRADE,
                    VALOR_COMPRA_USDT,
                    ex,
                    alavancagem=float(ALAVANCAGEM_PADRAO),
                )
            else:
                ordem = ex_mod.executar_compra_spot_market(
                    SYMBOL_TRADE, VALOR_COMPRA_USDT, ex
                )
            oid = ordem.get("id", "?")
            st = ordem.get("status", "?")
            preco_compra = float(ordem.get("auric_entry_price") or preco)
            posicao_aberta = True
            direcao_posicao = "LONG"
            bracket_note = (
                " TP/SL reduce-only na Binance (LIMIT+STOP_MARKET)."
                if modo == "FUTURES"
                else ""
            )
            just_final = (
                f"{just_ia} | Ordem id={oid} status={st}; custo {VALOR_COMPRA_USDT} USDT.{bracket_note} "
                f"preco ref. entrada={preco_compra:.4f} → Modo Vigia (LONG)."
            )
            logger.registrar_log_trade(
                ativo=SYMBOL_TRADE,
                preco=preco,
                prob_ml=probabilidade,
                sentimento=sent,
                acao="COMPRA_LONG_MARKET",
                justificativa=just_final,
                lado_ordem="LONG",
            )
            print(
                f"\n    [Estado] COMPRA Long: posicao_aberta=True | preco ref.={preco_compra:.4f} USDT"
            )
            print(
                "    Próximos ciclos: >>> MODO VIGIA <<< "
                + (
                    "(FUTURES: TP/SL já na bolsa; o maestro ainda monitora como rede de segurança)"
                    if modo == "FUTURES"
                    else "(TP +2% / SL -1%)."
                )
            )
        except Exception as e_ord:  # noqa: BLE001
            err_txt = f"Falha na ordem: {e_ord!s}"
            print(f"    [ERRO] {err_txt}")
            logger.registrar_log_trade(
                ativo=SYMBOL_TRADE,
                preco=preco,
                prob_ml=probabilidade,
                sentimento=sent,
                acao="ERRO_EXECUCAO",
                justificativa=f"{just_ia} | {err_txt}",
                lado_ordem="LONG",
            )
        return

    # SHORT (futures)
    entrada_txt = (
        f"VENDA (Short) MARKET ~{VALOR_COMPRA_USDT:.2f} USDT notional ({modo_label})."
    )
    print(
        f"    [Buscando Oportunidade] Confluência SHORT + posicao_recomendada=SHORT + "
        f"conf≥{CONFIANCA_BRAIN_MIN_ENTRADA}% — {entrada_txt}"
    )
    try:
        ordem = executor_futures.abrir_short_market(
            SYMBOL_TRADE, VALOR_COMPRA_USDT, ex, alavancagem=float(ALAVANCAGEM_PADRAO)
        )
        oid = ordem.get("id", "?")
        st = ordem.get("status", "?")
        preco_compra = float(ordem.get("auric_entry_price") or preco)
        posicao_aberta = True
        direcao_posicao = "SHORT"
        just_final = (
            f"{just_ia} | Short id={oid} status={st}; notional ~{VALOR_COMPRA_USDT} USDT. "
            " TP/SL reduce-only na Binance. "
            f"preco ref. entrada={preco_compra:.4f} → Modo Vigia (SHORT)."
        )
        logger.registrar_log_trade(
            ativo=SYMBOL_TRADE,
            preco=preco,
            prob_ml=probabilidade,
            sentimento=sent,
            acao="ABRE_SHORT_MARKET",
            justificativa=just_final,
            lado_ordem="SHORT",
        )
        print(
            f"\n    [Estado] SHORT aberto: posicao_aberta=True | preco ref.={preco_compra:.4f} USDT"
        )
        print(
            "    Próximos ciclos: >>> MODO VIGIA <<< "
            "(FUTURES: TP/SL na bolsa; maestro como rede de segurança)"
        )
    except Exception as e_ord:  # noqa: BLE001
        err_txt = f"Falha na ordem short: {e_ord!s}"
        print(f"    [ERRO] {err_txt}")
        logger.registrar_log_trade(
            ativo=SYMBOL_TRADE,
            preco=preco,
            prob_ml=probabilidade,
            sentimento=sent,
            acao="ERRO_EXECUCAO",
            justificativa=f"{just_ia} | {err_txt}",
            lado_ordem="SHORT",
        )


def main() -> None:
    load_dotenv(override=True)
    parser = argparse.ArgumentParser(
        description="Maestro: ML + Intelligence Hub + Brain (Claude) + Executor + Logger.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Executa apenas um ciclo e termina.",
    )
    parser.add_argument(
        "--intervalo",
        type=int,
        default=300,
        metavar="SEG",
        help="Segundos entre ciclos (padrão: 300 = 5 min).",
    )
    args = parser.parse_args()

    print(
        "\n[Maestro] Bot quantitativo | MAINNET | modo SPOT/FUTURES via Supabase (config) | "
        "TP/SL 1.02 / 0.99.\n"
    )

    if args.once:
        if not verificar_permissao_operacao():
            print("💤 [STANDBY] Bot desativado via Dashboard. Ciclo único não executado.")
            return
        print("🚀 [AURIC] Bot Ativo. Iniciando ciclo de análise...")
        modo = obter_modo_operacao()
        print(f"🔄 Modo de Operação detectado: {modo}")
        try:
            rodar_ciclo(modo)
        except Exception as e:  # noqa: BLE001
            print(f"\n[ERRO CICLO] {e}")
            traceback.print_exc()
        return

    while True:
        if not verificar_permissao_operacao():
            print("💤 [STANDBY] Bot desativado via Dashboard. Aguardando 60s...")
            time.sleep(60)
            continue

        print("🚀 [AURIC] Bot Ativo. Iniciando ciclo de análise...")
        modo = obter_modo_operacao()
        print(f"🔄 Modo de Operação detectado: {modo}")

        try:
            rodar_ciclo(modo)
        except Exception as e:  # noqa: BLE001
            print(f"\n[ERRO CICLO — bot continua] {e}")
            traceback.print_exc()
            try:
                preco = obter_preco_referencia(SYMBOL_TRADE, modo)
                logger.registrar_log_trade(
                    ativo=SYMBOL_TRADE,
                    preco=preco,
                    prob_ml=-1.0,
                    sentimento="—",
                    acao="ERRO_CICLO",
                    justificativa=f"Exceção no ciclo: {e!s}",
                )
            except Exception:  # noqa: BLE001
                pass

        modo_txt = "MODO VIGIA" if posicao_aberta else "BUSCANDO OPORTUNIDADE"
        print(
            f"\n[Aguardar] Próximo ciclo em {args.intervalo}s "
            f"({args.intervalo / 60:.1f} min) | estado esperado: {modo_txt}\n"
        )
        time.sleep(args.intervalo)


if __name__ == "__main__":
    main()
