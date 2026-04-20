"""
Maestro do sistema: estado global de posição → ML + sentimento → execução Spot ou Futures (Supabase) → Supabase.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv(override=True)

import argparse
import json
import os
import time
import traceback
from datetime import datetime, timezone
import urllib.error
import urllib.request
from typing import Any

import ccxt

import brain
import executor_futures
import executor_spot
import indicators
import intelligence_hub
import logger
import ml_model

SYMBOL_TRADE = "ETH/USDT"
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
TRAILING_CALLBACK_RATE_PADRAO = 0.5
TRAILING_ACTIVATION_MULTIPLIER_PADRAO = 1.0

# --- Estado do bot (memória do processo; reinício do script perde o estado) ---
posicao_aberta: bool = False
preco_compra: float = 0.0
direcao_posicao: str = "LONG"  # "LONG" | "SHORT" — lado da posição aberta (vigia / TP-SL)
_last_modo_detectado: str | None = None


def obter_modo_operacao() -> str:
    """
    Modo SPOT ou FUTURES: se `TRADING_MODE` estiver definido no .env, tem prioridade;
    caso contrário usa `config.modo_operacao` no Supabase (id=1).
    """
    env_modo = (os.getenv("TRADING_MODE") or "").strip().upper()
    if env_modo in ("SPOT", "FUTURES"):
        return env_modo
    op_fut = (os.getenv("OPERACAO_FUTURES") or "").strip().lower()
    if op_fut in ("1", "true", "yes", "on"):
        return "FUTURES"
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
    Pedágio ao arrancar o loop: `bot_control.is_active` onde `id = 1`
    (mesma tabela/coluna que `logger.obter_bot_ativo`, mas sem Supabase → False).
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


def obter_parametros_trailing_supabase() -> tuple[float, float]:
    """
    Lê parâmetros dinâmicos do trailing no Supabase (`config`, id=1):
    - `trailing_callback_rate` (percentual; ex.: 0.5)
    - `trailing_activation_multiplier` (multiplicador do antigo TP; ex.: 1.0)

    Fallback seguro:
    - callback_rate = 0.5
    - activation_multiplier = 1.0 (mantém lógica atual de ativação)
    """
    callback = TRAILING_CALLBACK_RATE_PADRAO
    activation_mult = TRAILING_ACTIVATION_MULTIPLIER_PADRAO

    if logger.supabase is None:
        return callback, activation_mult

    try:
        res = (
            logger.supabase.table("config")
            .select("trailing_callback_rate, trailing_activation_multiplier")
            .eq("id", 1)
            .single()
            .execute()
        )
        row = res.data if isinstance(res.data, dict) else {}
        cb_raw = row.get("trailing_callback_rate")
        am_raw = row.get("trailing_activation_multiplier")

        if cb_raw is not None:
            cb = float(cb_raw)
            if cb > 0:
                callback = cb
        if am_raw is not None:
            am = float(am_raw)
            if am > 0:
                activation_mult = am
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ obter_parametros_trailing_supabase: {e}")

    return callback, activation_mult


def atualizar_saldo_supabase() -> None:
    """
    Lê o saldo USDT na carteira **Futuros USDT-M** via CCXT, exatamente:
    ``exchange.fetch_balance(params={'type': 'future'})['total']['USDT']``.

    Depois persiste com ``logger.persistir_saldo_usdt`` (**upsert** em
    ``wallet_status.usdt_balance``, id=1; opcionalmente **update** em
    ``config.balance_usdt``).
    """
    if logger.supabase is None:
        return
    try:
        ex = executor_futures.criar_exchange_binance()
        bal = ex.fetch_balance(params={"type": "future"})
        total = bal.get("total")
        if not isinstance(total, dict):
            print(
                "⚠️ atualizar_saldo_supabase: resposta sem chave `total` dict — "
                f"tipo={type(total).__name__}"
            )
            return
        raw_usdt = total.get("USDT")
        if raw_usdt is None:
            print("⚠️ atualizar_saldo_supabase: total['USDT'] ausente no balance futures.")
            return
        saldo = float(raw_usdt)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ atualizar_saldo_supabase (Binance): {e}")
        return
    try:
        logger.persistir_saldo_usdt(saldo)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ atualizar_saldo_supabase (Supabase): {e}")


def _marcar_comando_manual_executado(row_id: int) -> None:
    if logger.supabase is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    logger.supabase.table("manual_commands").update(
        {"executed": True, "updated_at": now}
    ).eq("id", row_id).execute()


def verificar_comandos_manuais() -> None:
    """
    Processa fila `manual_commands` (executed = false): LONG → `abrir_long_market` (LIMIT +0,05%),
    SHORT → `abrir_short_market` (LIMIT −0,05%), CLOSE → `fechar_posicao_market` (Futuros USDT-M).
    Após sucesso, marca executed = true.
    """
    if logger.supabase is None:
        return
    if os.getenv("AURIC_DRY_RUN", "").strip().lower() in ("1", "true", "yes"):
        return
    try:
        res = (
            logger.supabase.table("manual_commands")
            .select("id, command")
            .eq("executed", False)
            .order("id", desc=False)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ verificar_comandos_manuais (select): {e}")
        return

    rows = res.data or []
    if not rows:
        return

    ex = executor_futures.criar_exchange_binance()
    for row in rows:
        rid = row.get("id")
        cmd = str(row.get("command") or "").upper().strip()
        if rid is None:
            continue
        try:
            if cmd == "LONG":
                trailing_cb, trailing_mult = obter_parametros_trailing_supabase()
                print(
                    f"👑 [GOD MODE] Manual Override ativo (id={rid}, LONG): "
                    "ignorando filtros RSI/ADX/VWAP e enviando ordem."
                )
                executor_futures.abrir_long_market(
                    SYMBOL_TRADE,
                    ex,
                    alavancagem=float(ALAVANCAGEM_PADRAO),
                    trailing_callback_rate=trailing_cb,
                    trailing_activation_multiplier=trailing_mult,
                )
                try:
                    t = ex.fetch_ticker(executor_futures._resolver_simbolo_perp(ex, SYMBOL_TRADE))
                    preco_man = float(t.get("last") or t.get("close") or 0.0)
                except Exception:  # noqa: BLE001
                    preco_man = 0.0
                logger.registrar_log_trade(
                    par_moeda=SYMBOL_TRADE,
                    preco=preco_man,
                    prob_ml=0.0,
                    sentimento="MANUAL",
                    acao="COMPRA_LONG",
                    justificativa=(
                        f"Manual Override (GOD MODE) id={rid}: execução direta LONG "
                        "(RSI/ADX/VWAP ignorados)."
                    ),
                    lado_ordem="LONG",
                )
                _marcar_comando_manual_executado(int(rid))
                print(f"🎮 [MANUAL] LONG id={rid} concluído; executed=true.")
            elif cmd == "SHORT":
                print(
                    f"👑 [GOD MODE] Manual Override ativo (id={rid}, SHORT): "
                    "ignorando filtros RSI/ADX/VWAP e enviando ordem."
                )
                trailing_cb, trailing_mult = obter_parametros_trailing_supabase()
                executor_futures.abrir_short_market(
                    SYMBOL_TRADE,
                    ex,
                    alavancagem=float(ALAVANCAGEM_PADRAO),
                    trailing_callback_rate=trailing_cb,
                    trailing_activation_multiplier=trailing_mult,
                )
                try:
                    sym_man = executor_futures._resolver_simbolo_perp(ex, SYMBOL_TRADE)
                    t = ex.fetch_ticker(sym_man)
                    preco_man = float(t.get("last") or t.get("close") or 0.0)
                except Exception:  # noqa: BLE001
                    preco_man = 0.0
                logger.registrar_log_trade(
                    par_moeda=SYMBOL_TRADE,
                    preco=preco_man,
                    prob_ml=0.0,
                    sentimento="MANUAL",
                    acao="ABRE_SHORT",
                    justificativa=(
                        f"Manual Override (GOD MODE) id={rid}: execução direta SHORT "
                        "(RSI/ADX/VWAP ignorados)."
                    ),
                    lado_ordem="SHORT",
                )
                _marcar_comando_manual_executado(int(rid))
                print(f"🎮 [MANUAL] SHORT id={rid} concluído; executed=true.")
            elif cmd in ("CLOSE", "CLOSE_ALL"):
                print(f"🎮 [MANUAL] Emergency close id={rid} ({cmd}) → Binance Futuros...")
                executor_futures.fechar_posicao_market(SYMBOL_TRADE, ex)
                _marcar_comando_manual_executado(int(rid))
                print(f"🎮 [MANUAL] {cmd} id={rid} concluído; executed=true.")
            else:
                print(
                    f"⚠️ [MANUAL] Comando ignorado id={rid} (command={cmd!r}; use LONG, SHORT ou CLOSE)."
                )
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ [MANUAL] Falha ao executar id={rid} cmd={cmd!r}: {e}")


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
                    par_moeda=SYMBOL_TRADE,
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
                    par_moeda=SYMBOL_TRADE,
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
                    par_moeda=SYMBOL_TRADE,
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
                    par_moeda=SYMBOL_TRADE,
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
                par_moeda=SYMBOL_TRADE,
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
                par_moeda=SYMBOL_TRADE,
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
                par_moeda=SYMBOL_TRADE,
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
                par_moeda=SYMBOL_TRADE,
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

    atualizar_saldo_supabase()
    verificar_comandos_manuais()

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

    if modo == "FUTURES":
        try:
            sym_f = executor_futures._resolver_simbolo_perp(ex, SYMBOL_TRADE)
            if indicators.volume_compra_spike_1m(ex, sym_f):
                ncx = executor_futures.cancelar_ordens_entrada_short(ex, SYMBOL_TRADE)
                if ncx:
                    print(
                        f"    [Risco] Volume 1m +300% vs. minuto anterior — "
                        f"{ncx} ordem(ns) de entrada SHORT cancelada(s)."
                    )
                    logger.registrar_log_trade(
                        par_moeda=SYMBOL_TRADE,
                        preco=preco,
                        prob_ml=0.0,
                        sentimento="—",
                        acao="CANCEL_SHORT_ENTRADA_SPIKE_1M",
                        justificativa=(
                            f"Volume vela 1m fechada ≥ {1.0 + indicators.VOLUME_SPIKE_FRACAO_1M:.0f}× "
                            f"a anterior; ordens entrada SHORT canceladas (n={ncx})."
                        ),
                        lado_ordem="SHORT",
                    )
        except Exception as e_vs:  # noqa: BLE001
            print(f"    ⚠️ Verificação volume spike: {e_vs}")

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

    # 1. Sinal Técnico (XGBoost) + snapshot ADX / VWAP / BB (um download OHLCV)
    probabilidade = ml_model.obter_sinal_atual()
    print(f"📊 [ML] Probabilidade de Alta: {probabilidade:.2%}")

    try:
        snap = ml_model.obter_snapshot_indicadores_eth(
            SYMBOL_TRADE, prob_ml=probabilidade
        )
    except Exception as e_snap:  # noqa: BLE001
        print(f"    ⚠️ Snapshot indicadores indisponível: {e_snap}")
        snap = {
            "regime": "BAIXA",
            "atr_pct": None,
            "atr_pct_median": None,
            "bb_width_pct": None,
            "bb_width_median": None,
            "bollinger_squeeze": False,
            "detalhe": str(e_snap),
            "adx_14": None,
            "adx_regime": "INDEFINIDO",
            "bb_pct_b": None,
            "bb_upper": None,
            "bb_lower": None,
            "bb_middle": None,
            "vwap_d": None,
            "preco_close": preco,
            "vies_vwap": "INDEFINIDO",
            "mercado_lateral": True,
            "rsi_14": None,
        }

    adx_v = snap.get("adx_14")
    rsi_v = snap.get("rsi_14")
    vies = snap.get("vies_vwap", "INDEFINIDO")
    print(
        f"    [Indicadores] ADX(14)={adx_v if adx_v is not None else 'N/A'} "
        f"| RSI(14)={rsi_v if rsi_v is not None else 'N/A'} "
        f"| viés VWAP: {vies} | squeeze BB: {snap.get('bollinger_squeeze')}"
    )

    limiar_short = ML_PROB_SHORT_MAX
    if probabilidade > limiar_short and probabilidade < limiar:
        reg_vol = snap
        regime = str(reg_vol.get("regime") or "BAIXA").upper()
        squeeze = bool(reg_vol.get("bollinger_squeeze"))

        if squeeze:
            linha_compressao = "Volatilidade em compressão (Bollinger Squeeze)."
        else:
            linha_compressao = (
                "Bandas de Bollinger sem squeeze forte (faixa típica ou expansão)."
            )
        if regime == "BAIXA":
            linha_regime = (
                "Regime ATR/BB: volatilidade baixa vs. mediana recente (possível acumulação)."
            )
        else:
            linha_regime = (
                "Regime ATR/BB: volatilidade elevada (mercado errático / chicote)."
            )
        linha_decay = (
            "Camada social (ex.: @VitalikButerin / feeds Alpha): notícias com mais de 2h "
            "são ignoradas pelo filtro SENTIMENT DECAY (notícias velhas = ruído)."
        )
        linha_adx_vwap = (
            f"ADX(14)={adx_v if adx_v is not None else 'N/A'} "
            f"({'lateral' if snap.get('mercado_lateral') else 'tendência'}) — "
            f"preço vs VWAP diário: {vies}."
        )
        contexto_dashboard = f"{linha_compressao} {linha_regime} {linha_adx_vwap} {linha_decay}"

        atr_p = reg_vol.get("atr_pct")
        med_a = reg_vol.get("atr_pct_median")
        bw = reg_vol.get("bb_width_pct")
        med_b = reg_vol.get("bb_width_median")
        partes_metricas: list[str] = []
        if atr_p is not None and med_a is not None:
            partes_metricas.append(f"ATR%={float(atr_p):.5f} (mediana ref. {float(med_a):.5f})")
        if bw is not None and med_b is not None:
            partes_metricas.append(
                f"BB width={float(bw):.5f} (mediana ref. {float(med_b):.5f})"
            )
        metricas_txt = (
            "; ".join(partes_metricas)
            if partes_metricas
            else str(reg_vol.get("detalhe") or "—")
        )

        adx_txt = (
            f"{float(adx_v):.0f}"
            if adx_v is not None
            else "—"
        )
        rsi_txt = (
            f"{float(rsi_v):.0f}"
            if rsi_v is not None
            else "—"
        )
        rot_adx = indicators.rotulo_regime_adx(
            float(adx_v) if adx_v is not None else None
        )
        rot_rsi = indicators.rotulo_regime_rsi(
            float(rsi_v) if rsi_v is not None else None
        )
        if squeeze:
            linha_decisao = (
                "Mercado sem tendência definida. Protegendo capital e aguardando expansão "
                "de volume (Bollinger Squeeze detectado)."
            )
        else:
            linha_decisao = (
                "Mercado sem tendência definida. Protegendo capital e aguardando "
                "clarificação de volatilidade ou sinal ML fora da zona neutra."
            )

        just_dashboard = (
            f"STATUS: MONITORANDO\n"
            f"ML: {probabilidade * 100:.1f}% (Neutro)\n"
            f"ADX: {adx_txt} ({rot_adx})\n"
            f"RSI: {rsi_txt} ({rot_rsi})\n"
            f"DECISÃO: {linha_decisao}\n"
            f"---\n"
            f"CONTEXTO: {contexto_dashboard}\n"
            f"{metricas_txt} | P(alta)={probabilidade:.4f} ∈ "
            f"({limiar_short},{limiar}); Hub/Brain inativos."
        )

        print("\n    [Painel de mercado — zona neutra / heatmap]")
        for ln in just_dashboard.split("\n"):
            if ln.strip() and not ln.startswith("---"):
                print(f"    {ln}")
        print(f"    [Métricas técnicas] {metricas_txt}")

        logger.registrar_log_trade(
            par_moeda=SYMBOL_TRADE,
            preco=preco,
            prob_ml=probabilidade,
            sentimento="—",
            acao="MONITORANDO",
            justificativa=just_dashboard,
            contexto_raw=indicators.formatar_log_contexto_raw(
                "(Intelligence Hub não consultado — ML na zona neutra.)",
                {**snap, "prob_ml": probabilidade},
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

    sym_ohlcv = SYMBOL_TRADE
    if modo == "FUTURES":
        sym_ohlcv = executor_futures._resolver_simbolo_perp(ex, SYMBOL_TRADE)

    confluencia: dict[str, Any] | None = None
    try:
        confluencia = indicators.obter_indicadores_confluencia(ex, sym_ohlcv)
        print(
            "    [Confluência 1h] "
            f"ADX={confluencia.get('adx')} VWAP={confluencia.get('vwap')} "
            f"| vs VWAP: {confluencia.get('posicao_vwap')} "
            f"| squeeze: {confluencia.get('squeeze')}"
        )
    except Exception as e_cf:  # noqa: BLE001
        print(f"    ⚠️ Indicadores confluência indisponíveis: {e_cf}")

    snap_log = {**snap, "prob_ml": probabilidade}
    if confluencia is not None:
        snap_log["confluencia"] = confluencia

    contexto_raw_supabase = indicators.formatar_log_contexto_raw(contexto, snap_log)
    bloco_ta = brain.montar_bloco_tecnico_final_boss(snap_log)

    # 3. Ativa o Cérebro (Claude 3.5 Sonnet)
    print("🧠 [BRAIN] Consultando Claude 3.5 Sonnet (Final Boss Mode)...")
    veredito = brain.analisar_sentimento_mercado(
        contexto,
        prob_ml=probabilidade,
        limiar_ml=limiar,
        direcao_sugerida=direcao_sugerida,
        verbose=False,
        bloco_tecnico_prioritario=bloco_ta,
    )

    sent = str(veredito.get("sentimento", "")).upper().strip()
    try:
        conf_num = int(float(veredito.get("confianca", 0)))
    except (TypeError, ValueError):
        conf_num = 0
    conf_num = indicators.aplicar_boost_confianca_squeeze(
        probabilidade,
        bool(snap.get("bollinger_squeeze")),
        conf_num,
    )
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
        f"Confiança efetiva: {conf_num}% "
        f"(Brain {veredito.get('confianca')}% + regras; "
        f"se P≥{indicators.ML_PROB_LIMIAR_BOOST_SQUEEZE:.0%} e squeeze BB → "
        f"mín. {indicators.CONFIANCA_BOOST_SQUEEZE}%)"
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

    manual_override_veredito = sent == "MANUAL"
    if manual_override_veredito:
        alvo = pos_rec if pos_rec in ("LONG", "SHORT") else direcao_sugerida
        sent = "BULLISH" if alvo == "LONG" else "BEARISH"
        pos_rec = alvo
        conf_num = max(conf_num, 100)
        print(
            f"👑 [GOD MODE] Veredito MANUAL detetado — filtros RSI/ADX/VWAP ignorados; "
            f"execução forçada para {alvo}."
        )

    # 4. Filtro de execução (entrada)
    if not manual_override_veredito and sent not in ("BULLISH", "BEARISH"):
        print(
            "    [Buscando Oportunidade] Só executa com sentimento BULLISH ou BEARISH; "
            f"recebido={sent}."
        )
        logger.registrar_log_trade(
            par_moeda=SYMBOL_TRADE,
            preco=preco,
            prob_ml=probabilidade,
            sentimento=sent,
            acao="VETO_SENTIMENTO",
            justificativa=f"{just_ia} | sentimento={sent} (exige BULLISH/BEARISH)",
            contexto_raw=contexto_raw_supabase,
            justificativa_ia=just_ia,
            noticias_agregadas=contexto,
        )
        return

    if not manual_override_veredito and (pos_rec == "VETO" or pos_rec != direcao_sugerida):
        print(
            "    [Buscando Oportunidade] Brain não confirma a direção técnica (VETO ou divergência)."
        )
        logger.registrar_log_trade(
            par_moeda=SYMBOL_TRADE,
            preco=preco,
            prob_ml=probabilidade,
            sentimento=sent,
            acao="VETO_POSICAO",
            justificativa=(
                f"{just_ia} | posicao_recomendada={pos_rec} vs direcao_sugerida={direcao_sugerida}"
            ),
            contexto_raw=contexto_raw_supabase,
            justificativa_ia=just_ia,
            noticias_agregadas=contexto,
        )
        return

    if not manual_override_veredito and conf_num < CONFIANCA_BRAIN_MIN_ENTRADA:
        print(
            f"    [Buscando Oportunidade] Sinal alinhado mas confiança {conf_num}% < "
            f"{CONFIANCA_BRAIN_MIN_ENTRADA}% — sem entrada."
        )
        logger.registrar_log_trade(
            par_moeda=SYMBOL_TRADE,
            preco=preco,
            prob_ml=probabilidade,
            sentimento=sent,
            acao="VETO_CONFIANCA_BRAIN",
            justificativa=f"{just_ia} (conf {conf_num}% < {CONFIANCA_BRAIN_MIN_ENTRADA}%)",
            contexto_raw=contexto_raw_supabase,
            justificativa_ia=just_ia,
            noticias_agregadas=contexto,
        )
        return

    if (
        not manual_override_veredito
        and snap.get("mercado_lateral")
        and not snap.get("bollinger_squeeze")
    ):
        if pos_rec == direcao_sugerida and sent in ("BULLISH", "BEARISH"):
            adx_s = snap.get("adx_14")
            print(
                "    [Buscando Oportunidade] ADX indica mercado lateral (ADX "
                f"< {indicators.ADX_LIMIAR_TENDENCIA} sem squeeze BB): não operar "
                "rompimento na direção do ML — preferir reversão/mean-reversion."
            )
            logger.registrar_log_trade(
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=probabilidade,
                sentimento=sent,
                acao="VETO_ADX_LATERAL",
                justificativa=(
                    f"{just_ia} | ADX(14)={adx_s} < {indicators.ADX_LIMIAR_TENDENCIA} "
                    "sem Bollinger Squeeze — sinal de continuação/tendência desvalorizado."
                ),
                contexto_raw=contexto_raw_supabase,
                justificativa_ia=just_ia,
                noticias_agregadas=contexto,
            )
            return

    dry_run = os.getenv("AURIC_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    if dry_run:
        print(
            "🔸 [DRY RUN] AURIC_DRY_RUN ativo — nenhuma ordem real será enviada à Binance."
        )
        logger.registrar_log_trade(
            par_moeda=SYMBOL_TRADE,
            preco=preco,
            prob_ml=probabilidade,
            sentimento=sent,
            acao="DRY_RUN",
            justificativa=f"{just_ia} | simulação apenas (defina AURIC_DRY_RUN=0 para mainnet)",
            contexto_raw=contexto_raw_supabase,
            justificativa_ia=just_ia,
            noticias_agregadas=contexto,
        )
        return

    if sent == "BEARISH" and modo != "FUTURES":
        print(
            "    [Buscando Oportunidade] BEARISH/SHORT requer FUTURES — modo SPOT não abre short."
        )
        logger.registrar_log_trade(
            par_moeda=SYMBOL_TRADE,
            preco=preco,
            prob_ml=probabilidade,
            sentimento=sent,
            acao="VETO_SHORT_SPOT",
            justificativa=f"{just_ia} | ative FUTURES na config para operar SHORT.",
            lado_ordem="SHORT",
            contexto_raw=contexto_raw_supabase,
            justificativa_ia=just_ia,
            noticias_agregadas=contexto,
        )
        return

    adx_raw = snap.get("adx_14")
    rsi_raw = snap.get("rsi_14")
    try:
        adx_num = float(adx_raw) if adx_raw is not None else None
    except (TypeError, ValueError):
        adx_num = None
    try:
        rsi_num = float(rsi_raw) if rsi_raw is not None else None
    except (TypeError, ValueError):
        rsi_num = None

    short_adx_bypass = False
    if (
        not manual_override_veredito
        and sent == "BEARISH"
        and indicators.rsi_proibe_entrada_short(rsi_num)
    ):
        if adx_num is not None and adx_num > 30.0:
            short_adx_bypass = True
            print(
                "    [ADX BYPASS] RSI sobrevendido para SHORT, mas ADX(14)>30 "
                f"(ADX={adx_num:.2f}) — tendência forte confirmada; veto ignorado."
            )
        else:
            print(
                f"    [Risco] RSI sobrevendido (RSI(14)={rsi_num}) — SHORT proibido "
                "(sem ADX BYPASS, exigido ADX>30)."
            )
            logger.registrar_log_trade(
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=probabilidade,
                sentimento=sent,
                acao="VETO_RSI_OVERSOLD",
                justificativa=(
                    f"{just_ia} | RSI(14)={rsi_num} < {indicators.RSI_LIMIAR_OVERSOLD_SHORT} "
                    f"e ADX(14)={adx_num} <= 30 (abrir SHORT vetado)"
                ),
                lado_ordem="SHORT",
                contexto_raw=contexto_raw_supabase,
                justificativa_ia=just_ia,
                noticias_agregadas=contexto,
            )
            return

    long_adx_bypass = False
    if (
        not manual_override_veredito
        and sent == "BULLISH"
        and rsi_num is not None
        and rsi_num > 70.0
    ):
        if adx_num is not None and adx_num > 30.0:
            long_adx_bypass = True
            print(
                "    [ADX BYPASS] RSI sobrecomprado para LONG, mas ADX(14)>30 "
                f"(ADX={adx_num:.2f}) — tendência forte confirmada; veto ignorado."
            )
        else:
            print(
                f"    [Risco] RSI sobrecomprado (RSI(14)={rsi_num:.2f}) — LONG proibido "
                "(sem ADX BYPASS, exigido ADX>30)."
            )
            logger.registrar_log_trade(
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=probabilidade,
                sentimento=sent,
                acao="VETO_RSI_OVERBOUGHT",
                justificativa=(
                    f"{just_ia} | RSI(14)={rsi_num:.2f} > 70 e ADX(14)={adx_num} <= 30 "
                    "(abrir LONG vetado)"
                ),
                lado_ordem="LONG",
                contexto_raw=contexto_raw_supabase,
                justificativa_ia=just_ia,
                noticias_agregadas=contexto,
            )
            return

    if sent == "BULLISH":
        if modo == "FUTURES":
            notional_alvo = executor_futures.notional_usdt_futuros_position_sizing(
                ex, float(ALAVANCAGEM_PADRAO)
            )
            entrada_txt = (
                f"COMPRA (Long) LIMIT (+0,05% vs. último) — notional ≈ {notional_alvo:.2f} USDT "
                f"(15% banca × {ALAVANCAGEM_PADRAO}x, {modo_label}, mainnet)."
            )
        else:
            notional_alvo = executor_spot.obter_saldo_usdt(ex) * executor_spot.PERCENTUAL_BANCA
            entrada_txt = (
                f"COMPRA (Long) LIMIT (+0,05%) — custo ≈ {notional_alvo:.2f} USDT "
                f"(15% saldo spot, {modo_label}, mainnet)."
            )
        print(
            f"    [Buscando Oportunidade] BULLISH + ML/Brain alinhados + "
            f"conf≥{CONFIANCA_BRAIN_MIN_ENTRADA}% — {entrada_txt}"
        )
        if manual_override_veredito:
            print("    👑 [GOD MODE] Execução LONG forçada por veredito MANUAL.")
        elif long_adx_bypass:
            print("    🚀 [ADX BYPASS] LONG autorizado apesar de RSI extremo (ADX > 30).")
        try:
            print(f"⚡ [ORDEM] Enviando comando de {sent} para a Binance...")
            if modo == "FUTURES":
                trailing_cb, trailing_mult = obter_parametros_trailing_supabase()
                ordem = executor_futures.abrir_long_market(
                    SYMBOL_TRADE,
                    ex,
                    alavancagem=float(ALAVANCAGEM_PADRAO),
                    trailing_callback_rate=trailing_cb,
                    trailing_activation_multiplier=trailing_mult,
                )
            else:
                ordem = ex_mod.executar_compra_spot_market(SYMBOL_TRADE, exchange=ex)
            oid = ordem.get("id", "?")
            st = ordem.get("status", "?")
            preco_compra = float(ordem.get("auric_entry_price") or preco)
            posicao_aberta = True
            direcao_posicao = "LONG"
            bracket_note = (
                " Bracket reduce-only na Binance (SL STOP_MARKET + TRAILING_STOP_MARKET; "
                f"activation no antigo TP×{trailing_mult:.3f}, callback {trailing_cb:.3f}%)."
                if modo == "FUTURES"
                else ""
            )
            just_final = (
                f"{just_ia} | Ordem id={oid} status={st}; "
                f"notional/custo alvo ≈ {notional_alvo:.2f} USDT.{bracket_note} "
                f"preco ref. entrada={preco_compra:.4f} → Modo Vigia (LONG)."
            )
            if manual_override_veredito:
                acao_long = "COMPRA_LONG"
            else:
                acao_long = "LONG_ADX_BYPASS" if long_adx_bypass else "COMPRA_LONG_LIMIT"
            logger.registrar_log_trade(
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=probabilidade,
                sentimento=sent,
                acao=acao_long,
                justificativa=just_final,
                lado_ordem="LONG",
                contexto_raw=contexto_raw_supabase,
                justificativa_ia=just_ia,
                noticias_agregadas=contexto,
            )
            print(
                f"\n    [Estado] COMPRA Long: posicao_aberta=True | preco ref.={preco_compra:.4f} USDT"
            )
            print(
                "    Próximos ciclos: >>> MODO VIGIA <<< "
                + (
                    "(FUTURES: SL fixo + trailing stop já na bolsa; "
                    "o maestro ainda monitora como rede de segurança)"
                    if modo == "FUTURES"
                    else "(TP +2% / SL -1%)."
                )
            )
        except Exception as e_ord:  # noqa: BLE001
            err_txt = f"Falha na ordem: {e_ord!s}"
            print(f"    [ERRO] {err_txt}")
            logger.registrar_log_trade(
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=probabilidade,
                sentimento=sent,
                acao="ERRO_EXECUCAO",
                justificativa=f"{just_ia} | {err_txt}",
                lado_ordem="LONG",
                contexto_raw=contexto_raw_supabase,
                justificativa_ia=just_ia,
                noticias_agregadas=contexto,
            )
        return

    elif sent == "BEARISH":
        notional_alvo = executor_futures.notional_usdt_futuros_position_sizing(
            ex, float(ALAVANCAGEM_PADRAO)
        )
        entrada_txt = (
            f"VENDA (Short) LIMIT (−0,05% vs. último) — notional ≈ {notional_alvo:.2f} USDT "
            f"(15% banca × {ALAVANCAGEM_PADRAO}x, {modo_label})."
        )
        print(
            f"    [Buscando Oportunidade] BEARISH + ML/Brain alinhados + "
            f"conf≥{CONFIANCA_BRAIN_MIN_ENTRADA}% — {entrada_txt}"
        )
        if manual_override_veredito:
            print("    👑 [GOD MODE] Execução SHORT forçada por veredito MANUAL.")
        elif short_adx_bypass:
            print("    🚀 [ADX BYPASS] SHORT autorizado apesar de RSI extremo (ADX > 30).")
        try:
            sym_f = executor_futures._resolver_simbolo_perp(ex, SYMBOL_TRADE)
            if indicators.volume_compra_spike_1m(ex, sym_f):
                executor_futures.cancelar_ordens_entrada_short(ex, SYMBOL_TRADE)
                print(
                    "    [Risco] Volume 1m +300% vs. minuto anterior — "
                    "SHORT abortado (spike de volume)."
                )
                logger.registrar_log_trade(
                    par_moeda=SYMBOL_TRADE,
                    preco=preco,
                    prob_ml=probabilidade,
                    sentimento=sent,
                    acao="VETO_VOLUME_SPIKE",
                    justificativa=(
                        f"{just_ia} | volume 1m fechado ≥ "
                        f"{1.0 + indicators.VOLUME_SPIKE_FRACAO_1M:.0f}× o minuto anterior — "
                        "sem abrir SHORT"
                    ),
                    lado_ordem="SHORT",
                    contexto_raw=contexto_raw_supabase,
                    justificativa_ia=just_ia,
                    noticias_agregadas=contexto,
                )
                return
        except Exception as e_sp:  # noqa: BLE001
            print(f"    ⚠️ Verificação volume spike (antes do SHORT): {e_sp}")
        try:
            print(f"⚡ [ORDEM] Enviando comando de {sent} para a Binance...")
            trailing_cb, trailing_mult = obter_parametros_trailing_supabase()
            ordem = executor_futures.abrir_short_market(
                SYMBOL_TRADE,
                ex,
                alavancagem=float(ALAVANCAGEM_PADRAO),
                trailing_callback_rate=trailing_cb,
                trailing_activation_multiplier=trailing_mult,
            )
            oid = ordem.get("id", "?")
            st = ordem.get("status", "?")
            preco_compra = float(ordem.get("auric_entry_price") or preco)
            posicao_aberta = True
            direcao_posicao = "SHORT"
            just_final = (
                f"{just_ia} | Short id={oid} status={st}; notional alvo ≈ {notional_alvo:.2f} USDT. "
                " Bracket reduce-only na Binance (SL STOP_MARKET + TRAILING_STOP_MARKET; "
                f"activation no antigo TP×{trailing_mult:.3f}, callback {trailing_cb:.3f}%). "
                f"preco ref. entrada={preco_compra:.4f} → Modo Vigia (SHORT)."
            )
            if manual_override_veredito:
                acao_short = "ABRE_SHORT"
            else:
                acao_short = "SHORT_ADX_BYPASS" if short_adx_bypass else "ABRE_SHORT_LIMIT"
            logger.registrar_log_trade(
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=probabilidade,
                sentimento=sent,
                acao=acao_short,
                justificativa=just_final,
                lado_ordem="SHORT",
                contexto_raw=contexto_raw_supabase,
                justificativa_ia=just_ia,
                noticias_agregadas=contexto,
            )
            print(
                f"\n    [Estado] SHORT aberto: posicao_aberta=True | preco ref.={preco_compra:.4f} USDT"
            )
            print(
                "    Próximos ciclos: >>> MODO VIGIA <<< "
                "(FUTURES: SL fixo + trailing stop na bolsa; maestro como rede de segurança)"
            )
        except Exception as e_ord:  # noqa: BLE001
            err_txt = f"Falha na ordem short: {e_ord!s}"
            print(f"    [ERRO] {err_txt}")
            logger.registrar_log_trade(
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=probabilidade,
                sentimento=sent,
                acao="ERRO_EXECUCAO",
                justificativa=f"{just_ia} | {err_txt}",
                lado_ordem="SHORT",
                contexto_raw=contexto_raw_supabase,
                justificativa_ia=just_ia,
                noticias_agregadas=contexto,
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
                    par_moeda=SYMBOL_TRADE,
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
