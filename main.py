"""
Maestro do sistema: estado global de posição → ML + sentimento → execução Spot ou Futures (Supabase) → Supabase.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv(override=True)

import argparse
import asyncio
from collections import deque
import contextlib
import json
import os
import re
import time
import threading
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
import websockets

SYMBOL_TRADE = "ETH/USDT"
WS_FUTURES_KLINE_1M = "wss://fstream.binance.com/ws/ethusdt@kline_1m"
ALAVANCAGEM_PADRAO = 3
RISK_FRACTION_PADRAO = 0.10
TRAILING_CALLBACK_PADRAO = 0.5
TRAILING_ACTIVATION_MULTIPLIER_PADRAO = 1.0
TRIGGER_COOLDOWN_S = 15.0
CLAUDE_RATE_LIMIT_MAX_CALLS = 1
CLAUDE_RATE_LIMIT_WINDOW_S = 300.0
METRICS_FLUSH_EVERY_TRIGGERS = 50
METRICS_FLUSH_EVERY_S = 3600.0
OUTCOME_AUDIT_INTERVAL_S = 300.0
# Resiliência HFT: reconexão WS e backoff após falha de ordem (HTTP / margem / notional).
WS_RECONNECT_DELAY_S = 5.0
ORDER_FAILURE_BACKOFF_S = 5.0
# Loop asyncio principal (para asyncio.sleep desde threads do ciclo ML sem bloquear outras tasks).
_main_event_loop: asyncio.AbstractEventLoop | None = None

# Multiplicadores de saída sobre o preço de entrada gravado em memória (LONG: sobe = lucro).
FATOR_TAKE_PROFIT = 1.02  # preço >= entrada × 1.02
FATOR_STOP_LOSS = 0.99  # preço <= entrada × 0.99
# SHORT (futures): lucro se preço cai; TP −2% / SL +1% sobre o preço de entrada.
FATOR_TAKE_PROFIT_SHORT = 0.98  # preço <= entrada × 0.98
FATOR_STOP_LOSS_SHORT = 1.01  # preço >= entrada × 1.01

# Zona ML que aciona Hub + Brain: P(alta) ≥ limiar long OU P(alta) ≤ limiar short.
ML_PROB_SHORT_MAX = 0.40
WAKE_FILTER_SHORT_MAX = 0.40
WAKE_FILTER_LONG_MIN = 0.60

# Mínimo de confiança do Brain (0–100) para permitir entrada.
CONFIANCA_BRAIN_MIN_ENTRADA = 70
TRAILING_CALLBACK_RATE_PADRAO = 0.5
TRAILING_ACTIVATION_MULTIPLIER_PADRAO = 1.0

# --- Estado do bot (memória do processo; reinício do script perde o estado) ---
posicao_aberta: bool = False
preco_compra: float = 0.0
direcao_posicao: str = "LONG"  # "LONG" | "SHORT" — lado da posição aberta (vigia / TP-SL)
_last_modo_detectado: str | None = None
_claude_call_timestamps: deque[float] = deque()
_risk_fraction_cfg: float = RISK_FRACTION_PADRAO
_alavancagem_cfg: float = float(ALAVANCAGEM_PADRAO)
_trailing_callback_cfg: float = TRAILING_CALLBACK_PADRAO
_trailing_activation_mult_cfg: float = TRAILING_ACTIVATION_MULTIPLIER_PADRAO
_metrics_lock = threading.Lock()
_metrics_counters: dict[str, int] = {
    "triggers_total": 0,
    "triggers_ignored_lock": 0,
    "ignored_cooldown": 0,
    "claude_rate_limited": 0,
}


def _metric_inc(nome: str, delta: int = 1) -> None:
    with _metrics_lock:
        _metrics_counters[nome] = int(_metrics_counters.get(nome, 0)) + int(delta)


def _metrics_snapshot() -> dict[str, int]:
    with _metrics_lock:
        return {k: int(v) for k, v in _metrics_counters.items()}


def _parse_order_id_from_justificativa(texto: str | None) -> str | None:
    if not texto:
        return None
    m = re.search(r"\b(?:ordem\s+)?id\s*=\s*([A-Za-z0-9_-]+)\b", texto, flags=re.IGNORECASE)
    if not m:
        return None
    return str(m.group(1)).strip() or None


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _registar_loop_principal_async() -> None:
    """
    Grava o loop em execução para permitir `asyncio.sleep` desde `rodar_ciclo`
    (thread worker) sem bloquear o event loop (graceful degradation / anti-spam).
    """
    global _main_event_loop
    _main_event_loop = asyncio.get_running_loop()


def _cooldown_apos_falha_ordem_desde_thread_ciclo() -> None:
    """
    Chamado desde `rodar_ciclo` (asyncio.to_thread): após falha de ordem ML,
    impõe pausa antes de novos HTTP — usa o loop principal para não parar
    o WebSocket, métricas ou listener manual durante o backoff.
    """
    loop = _main_event_loop
    sec = float(ORDER_FAILURE_BACKOFF_S)
    print(
        f"⏳ [RATE LIMIT] Backoff {sec:.0f}s após falha de execução de ordem (anti-spam HTTP).",
        flush=True,
    )
    if loop is not None and loop.is_running():
        try:
            fut = asyncio.run_coroutine_threadsafe(asyncio.sleep(sec), loop)
            fut.result(timeout=sec + 15.0)
        except Exception as e:  # noqa: BLE001
            print(
                f"⚠️ [RATE LIMIT] asyncio.sleep no loop principal falhou ({e}); "
                f"time.sleep({sec:.0f}s) neste worker.",
                flush=True,
            )
            time.sleep(sec)
    else:
        time.sleep(sec)


def _permitir_consulta_claude() -> tuple[bool, int, float]:
    """
    Limite de chamadas do Claude/Replicate:
    no máximo `CLAUDE_RATE_LIMIT_MAX_CALLS` em `CLAUDE_RATE_LIMIT_WINDOW_S`.
    """
    now = time.monotonic()
    while _claude_call_timestamps and (now - _claude_call_timestamps[0]) > CLAUDE_RATE_LIMIT_WINDOW_S:
        _claude_call_timestamps.popleft()

    if len(_claude_call_timestamps) >= CLAUDE_RATE_LIMIT_MAX_CALLS:
        espera = max(0.0, CLAUDE_RATE_LIMIT_WINDOW_S - (now - _claude_call_timestamps[0]))
        return False, len(_claude_call_timestamps), espera

    _claude_call_timestamps.append(now)
    return True, len(_claude_call_timestamps), 0.0


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


def _obter_configuracoes_dinamicas_sync() -> dict[str, float]:
    """
    Lê `bot_config` (id=1) para parâmetros operacionais dinâmicos.
    Fallback seguro em falha:
    - risk_fraction=0.10
    - leverage=3.0
    - trailing_callback_rate=0.5
    - trailing_activation_multiplier=1.0
    """
    cfg = {
        "risk_fraction": float(RISK_FRACTION_PADRAO),
        "leverage": float(ALAVANCAGEM_PADRAO),
        "trailing_callback_rate": float(TRAILING_CALLBACK_PADRAO),
        "trailing_activation_multiplier": float(TRAILING_ACTIVATION_MULTIPLIER_PADRAO),
    }
    if logger.supabase is None:
        return cfg
    try:
        res = (
            logger.supabase.table("bot_config")
            .select(
                "risk_fraction, leverage, trailing_callback_rate, trailing_activation_multiplier"
            )
            .eq("id", 1)
            .single()
            .execute()
        )
        row = res.data if isinstance(res.data, dict) else {}
        rf = row.get("risk_fraction")
        lv = row.get("leverage")
        cb = row.get("trailing_callback_rate")
        am = row.get("trailing_activation_multiplier")
        if rf is not None and float(rf) > 0:
            cfg["risk_fraction"] = float(rf)
        if lv is not None and float(lv) > 0:
            cfg["leverage"] = float(lv)
        if cb is not None and float(cb) > 0:
            cfg["trailing_callback_rate"] = float(cb)
        if am is not None and float(am) > 0:
            cfg["trailing_activation_multiplier"] = float(am)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ _obter_configuracoes_dinamicas: {e} — usando fallback seguro.")
    return cfg


async def _obter_configuracoes_dinamicas() -> dict[str, float]:
    return await asyncio.to_thread(_obter_configuracoes_dinamicas_sync)


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
    Processa fila `manual_commands` (executed = false): LONG → `abrir_long_market` (LIMIT bid×1,0005 GTC+chase),
    SHORT → `abrir_short_market` (LIMIT ask×0,9995), CLOSE → `fechar_posicao_market` (LIMIT reduce-only+chase).
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
                trailing_cb, trailing_mult = _trailing_callback_cfg, _trailing_activation_mult_cfg
                print(
                    f"👑 [GOD MODE] Manual Override ativo (id={rid}, LONG): "
                    "ignorando filtros RSI/ADX/VWAP e enviando ordem."
                )
                executor_futures.abrir_long_market(
                    SYMBOL_TRADE,
                    ex,
                    alavancagem=float(_alavancagem_cfg),
                    risk_fraction=float(_risk_fraction_cfg),
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
                trailing_cb, trailing_mult = _trailing_callback_cfg, _trailing_activation_mult_cfg
                executor_futures.abrir_short_market(
                    SYMBOL_TRADE,
                    ex,
                    alavancagem=float(_alavancagem_cfg),
                    risk_fraction=float(_risk_fraction_cfg),
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


def _fetch_pending_manual_command_sync() -> dict[str, Any] | None:
    """
    Busca 1 comando manual pendente (mais antigo primeiro).
    Preferência por `status='PENDING'`; fallback para esquema legado `executed=false`.
    """
    if logger.supabase is None:
        return None
    try:
        res = (
            logger.supabase.table("manual_commands")
            .select("id, command, status")
            .eq("status", "PENDING")
            .order("id", desc=False)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if rows:
            return rows[0]
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ [MANUAL LISTENER] select status=PENDING falhou: {e}")

    # Fallback legado
    try:
        res = (
            logger.supabase.table("manual_commands")
            .select("id, command")
            .eq("executed", False)
            .order("id", desc=False)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ [MANUAL LISTENER] fallback executed=false falhou: {e}")
        return None


def _mark_manual_command_status_sync(row_id: int, status: str) -> None:
    if logger.supabase is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    payload = {"status": status, "updated_at": now}
    # Compat schema antigo/novo: manter também `executed`.
    if status == "EXECUTED":
        payload["executed"] = True
    try:
        logger.supabase.table("manual_commands").update(payload).eq("id", row_id).execute()
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ [MANUAL LISTENER] update status={status} id={row_id} falhou: {e}")


def _executar_comando_manual_imediato_sync(cmd: str) -> None:
    """
    Executor GOD MODE: sem filtros de veto (RSI/ADX/VWAP).
    """
    global _risk_fraction_cfg, _alavancagem_cfg, _trailing_callback_cfg, _trailing_activation_mult_cfg

    ex = executor_futures.criar_exchange_binance()
    c = str(cmd or "").upper().strip()
    if c == "LONG":
        executor_futures.abrir_long_market(
            SYMBOL_TRADE,
            ex,
            alavancagem=float(_alavancagem_cfg),
            risk_fraction=float(_risk_fraction_cfg),
            trailing_callback_rate=float(_trailing_callback_cfg),
            trailing_activation_multiplier=float(_trailing_activation_mult_cfg),
        )
        return
    if c == "SHORT":
        executor_futures.abrir_short_market(
            SYMBOL_TRADE,
            ex,
            alavancagem=float(_alavancagem_cfg),
            risk_fraction=float(_risk_fraction_cfg),
            trailing_callback_rate=float(_trailing_callback_cfg),
            trailing_activation_multiplier=float(_trailing_activation_mult_cfg),
        )
        return
    if c in ("CLOSE", "CLOSE_ALL"):
        executor_futures.fechar_posicao_market(SYMBOL_TRADE, ex)
        return
    raise ValueError(f"Comando manual inválido: {c!r}")


async def _escutar_comandos_manuais() -> None:
    """
    Listener assíncrono de comandos manuais:
    - polling a cada 1s
    - interceta 1 comando pendente
    - marca EXECUTED imediatamente (evita duplo processamento)
    - executa instantaneamente com config dinâmica atual
    """
    global _risk_fraction_cfg, _alavancagem_cfg, _trailing_callback_cfg, _trailing_activation_mult_cfg

    while True:
        await asyncio.sleep(1)
        row = await asyncio.to_thread(_fetch_pending_manual_command_sync)
        if not row:
            continue

        rid_raw = row.get("id")
        cmd = str(row.get("command") or "").upper().strip()
        try:
            rid = int(rid_raw)
        except Exception:
            print(f"⚠️ [MANUAL LISTENER] id inválido: {rid_raw!r}")
            continue

        print(
            f"👑 [GOD MODE] Comando MANUAL {cmd} intercetado e a ser executado INSTANTANEAMENTE! (id={rid})"
        )

        cfg_dyn = await _obter_configuracoes_dinamicas()
        _risk_fraction_cfg = float(cfg_dyn.get("risk_fraction", RISK_FRACTION_PADRAO))
        _alavancagem_cfg = float(cfg_dyn.get("leverage", ALAVANCAGEM_PADRAO))
        _trailing_callback_cfg = float(
            cfg_dyn.get("trailing_callback_rate", TRAILING_CALLBACK_PADRAO)
        )
        _trailing_activation_mult_cfg = float(
            cfg_dyn.get(
                "trailing_activation_multiplier",
                TRAILING_ACTIVATION_MULTIPLIER_PADRAO,
            )
        )

        # Marcar imediatamente como EXECUTED para evitar leitura duplicada.
        await asyncio.to_thread(_mark_manual_command_status_sync, rid, "EXECUTED")

        try:
            await asyncio.to_thread(_executar_comando_manual_imediato_sync, cmd)
            print(f"✅ [MANUAL LISTENER] Comando {cmd} (id={rid}) executado com sucesso.")
        except Exception as e:  # noqa: BLE001
            print(f"❌ [MANUAL LISTENER] Falha ao executar {cmd} (id={rid}): {e}")
            await asyncio.to_thread(_mark_manual_command_status_sync, rid, "FAILED")
            print(
                f"⏳ [MANUAL LISTENER] Cooldown {ORDER_FAILURE_BACKOFF_S:.0f}s após falha "
                "(anti-spam / rate limit exchange)."
            )
            await asyncio.sleep(ORDER_FAILURE_BACKOFF_S)


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
    global _risk_fraction_cfg, _alavancagem_cfg, _trailing_callback_cfg, _trailing_activation_mult_cfg

    if not logger.obter_bot_ativo():
        print("Bot em modo Standby via Dashboard")
        return

    atualizar_saldo_supabase()

    ex_mod = executor_futures if modo == "FUTURES" else executor_spot
    modo_label = "FUTURES USDT-M" if modo == "FUTURES" else "SPOT"

    if modo == "FUTURES" and _last_modo_detectado != "FUTURES":
        try:
            ex_cfg = executor_futures.criar_exchange_binance()
            executor_futures.configurar_alavancagem(
                SYMBOL_TRADE, int(round(_alavancagem_cfg)), ex_cfg
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

    limiar_short = WAKE_FILTER_SHORT_MAX
    limiar_long = WAKE_FILTER_LONG_MIN
    if limiar_short <= probabilidade <= limiar_long:
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
            f"[{limiar_short:.2f},{limiar_long:.2f}] — Claude poupado (filtro de despertar ativo)."
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
            sentimento="NEUTRAL",
            acao="MONITORANDO",
            justificativa=(
                "Mercado lateral (XGBoost Neutro). IA poupada para reduzir custos de API."
            ),
            contexto_raw=indicators.formatar_log_contexto_raw(
                "(Intelligence Hub/Claude não consultados — filtro de despertar ML na zona neutra.)",
                {**snap, "prob_ml": probabilidade},
            ),
            justificativa_ia=(
                "Mercado lateral (XGBoost Neutro). IA poupada para reduzir custos de API."
            ),
        )
        print(
            "💤 [WAKE FILTER] XGBoost em zona neutra [0.40, 0.60] — Claude NÃO chamado. "
            'Mock: {"sentimento":"NEUTRAL","posicao_recomendada":"VETO","justificativa_curta":"Mercado lateral (XGBoost Neutro). IA poupada para reduzir custos de API."}'
        )
        return

    if probabilidade > limiar_long:
        direcao_sugerida = "LONG"
    elif probabilidade < limiar_short:
        direcao_sugerida = "SHORT"
    else:
        print(
            "⚠️ [WAKE FILTER] Probabilidade neutra detetada em ramo de segurança; "
            "sem consulta ao Claude."
        )
        return

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

    # 3. Ativa o Cérebro (Claude) com rate limiter para proteger a API.
    pode_chamar_claude, n_chamadas, espera_s = _permitir_consulta_claude()
    if pode_chamar_claude:
        print(
            "🧠 [BRAIN] Consultando Claude 3.5 Sonnet (Final Boss Mode)... "
            f"(janela 60s: {n_chamadas}/{CLAUDE_RATE_LIMIT_MAX_CALLS})"
        )
        veredito = brain.analisar_sentimento_mercado(
            contexto,
            prob_ml=probabilidade,
            limiar_ml=limiar,
            direcao_sugerida=direcao_sugerida,
            verbose=False,
            bloco_tecnico_prioritario=bloco_ta,
        )
    else:
        _metric_inc("claude_rate_limited", 1)
        print(
            "⚠️ [RATE LIMIT] Claude bloqueado: "
            f"{n_chamadas}/{CLAUDE_RATE_LIMIT_MAX_CALLS} chamadas em "
            f"{CLAUDE_RATE_LIMIT_WINDOW_S:.0f}s. "
            f"Aguardando ~{espera_s:.1f}s. "
            "Ciclo segue em modo CAUTIOUS (sem nova chamada LLM)."
        )
        veredito = {
            "sentimento": "CAUTIOUS",
            "confianca": 0,
            "justificativa_curta": (
                "Rate limiter Claude ativo (2 chamadas/60s); "
                "execução adiada para proteger a API do Replicate."
            ),
            "alerta_macro": "rate_limit_claude",
            "posicao_recomendada": "VETO",
        }

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
                ex,
                float(_alavancagem_cfg),
                risk_fraction=float(_risk_fraction_cfg),
            )
            entrada_txt = (
                f"COMPRA (Long) LIMIT (+0,05% vs. último) — notional ≈ {notional_alvo:.2f} USDT "
                f"(risk {_risk_fraction_cfg*100:.1f}% × {_alavancagem_cfg:.2f}x, {modo_label}, mainnet)."
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
                trailing_cb, trailing_mult = _trailing_callback_cfg, _trailing_activation_mult_cfg
                ordem = executor_futures.abrir_long_market(
                    SYMBOL_TRADE,
                    ex,
                    alavancagem=float(_alavancagem_cfg),
                    risk_fraction=float(_risk_fraction_cfg),
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
            _cooldown_apos_falha_ordem_desde_thread_ciclo()
        return

    elif sent == "BEARISH":
        notional_alvo = executor_futures.notional_usdt_futuros_position_sizing(
            ex,
            float(_alavancagem_cfg),
            risk_fraction=float(_risk_fraction_cfg),
        )
        entrada_txt = (
            f"VENDA (Short) LIMIT (−0,05% vs. último) — notional ≈ {notional_alvo:.2f} USDT "
            f"(risk {_risk_fraction_cfg*100:.1f}% × {_alavancagem_cfg:.2f}x, {modo_label})."
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
            trailing_cb, trailing_mult = _trailing_callback_cfg, _trailing_activation_mult_cfg
            ordem = executor_futures.abrir_short_market(
                SYMBOL_TRADE,
                ex,
                alavancagem=float(_alavancagem_cfg),
                risk_fraction=float(_risk_fraction_cfg),
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
            _cooldown_apos_falha_ordem_desde_thread_ciclo()


async def _executar_ciclo_assincrono(modo: str, origem: str) -> None:
    """Executa o ciclo completo em thread para não bloquear o event loop."""
    try:
        await asyncio.to_thread(rodar_ciclo, modo)
    except Exception as e:  # noqa: BLE001
        print(f"\n[ERRO CICLO — gatilho {origem}] {e}")
        traceback.print_exc()
        try:
            preco = await asyncio.to_thread(obter_preco_referencia, SYMBOL_TRADE, modo)
            await asyncio.to_thread(
                logger.registrar_log_trade,
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=-1.0,
                sentimento="—",
                acao="ERRO_CICLO",
                justificativa=f"Exceção no ciclo ({origem}): {e!s}",
            )
        except Exception:  # noqa: BLE001
            pass


async def _flush_metrics_supabase(uptime_s: float) -> None:
    """
    Upsert de métricas operacionais no Supabase.
    Preferência: tabela `system_metrics` (id=1), fallback para `bot_control` (id=1).
    """
    if logger.supabase is None:
        return

    snap = _metrics_snapshot()
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "id": 1,
        "triggers_total": snap["triggers_total"],
        "triggers_ignored_lock": snap["triggers_ignored_lock"],
        "ignored_cooldown": snap["ignored_cooldown"],
        "claude_rate_limited": snap["claude_rate_limited"],
        "uptime_seconds": int(uptime_s),
        "updated_at": now_iso,
    }

    def _write_sync() -> None:
        # 1) Tabela dedicada (recomendado)
        try:
            logger.supabase.table("system_metrics").upsert(payload).execute()
            return
        except Exception as e_sys:  # noqa: BLE001
            print(f"⚠️ [METRICS] upsert system_metrics falhou: {e_sys}")

        # 2) Fallback bot_control (se a equipa preferir armazenar ali)
        try:
            logger.supabase.table("bot_control").update(payload).eq("id", 1).execute()
            return
        except Exception as e_bot:  # noqa: BLE001
            print(f"⚠️ [METRICS] update bot_control falhou: {e_bot}")

    await asyncio.to_thread(_write_sync)
    print(
        "[METRICS] Flush Supabase concluído: "
        f"triggers={snap['triggers_total']}, "
        f"lock={snap['triggers_ignored_lock']}, "
        f"cooldown={snap['ignored_cooldown']}, "
        f"claude_rl={snap['claude_rate_limited']}, "
        f"uptime={int(uptime_s)}s"
    )


async def _metrics_reporter_task(start_monotonic: float) -> None:
    """
    Reporter em background (não bloqueante):
    - flush a cada 50 triggers totais
    - OU a cada 1h de uptime
    """
    last_flush_time = time.monotonic()
    last_flush_triggers = 0
    while True:
        await asyncio.sleep(5)
        snap = _metrics_snapshot()
        now = time.monotonic()
        uptime_s = now - start_monotonic
        triggers_delta = snap["triggers_total"] - last_flush_triggers
        due_triggers = triggers_delta >= METRICS_FLUSH_EVERY_TRIGGERS
        due_time = (now - last_flush_time) >= METRICS_FLUSH_EVERY_S
        if not (due_triggers or due_time):
            continue
        try:
            await _flush_metrics_supabase(uptime_s)
            last_flush_time = now
            last_flush_triggers = snap["triggers_total"]
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ [METRICS] Falha no flush periódico: {e}")


def _buscar_logs_recentes_para_outcome(limit: int = 400) -> list[dict[str, Any]]:
    if logger.supabase is None:
        return []
    try:
        res = (
            logger.supabase.table("logs")
            .select(
                "id, created_at, par_moeda, probabilidade_ml, justificativa_ia, justificativa, acao_tomada"
            )
            .order("id", desc=True)
            .limit(limit)
            .execute()
        )
        data = res.data or []
        return data if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ [AUDITORIA] Falha ao ler logs recentes: {e}")
        return []


def _deduzir_pnl_realizado_ordem(order: dict[str, Any]) -> float | None:
    info = order.get("info") if isinstance(order, dict) else None
    if isinstance(info, dict):
        for key in ("realizedPnl", "realizedProfit", "profit", "pnl"):
            v = _safe_float(info.get(key))
            if v is not None:
                return v
    for key in ("pnl", "realizedPnl"):
        v = _safe_float(order.get(key))
        if v is not None:
            return v
    return None


def _match_log_para_ordem(
    order: dict[str, Any], logs: list[dict[str, Any]]
) -> dict[str, Any] | None:
    oid = str(order.get("id") or "").strip()
    side = str(order.get("side") or "").lower().strip()
    side_alvo = "SHORT" if side == "sell" else "LONG"

    # 1) Match por order_id dentro da justificativa
    if oid:
        for row in logs:
            jid = _parse_order_id_from_justificativa(str(row.get("justificativa") or ""))
            if jid and jid == oid:
                return row

    # 2) Fallback por lado em ações de abertura (mais recente primeiro)
    for row in logs:
        ac = str(row.get("acao_tomada") or "").upper().strip()
        if side_alvo == "LONG" and ac in (
            "COMPRA_LONG",
            "COMPRA_LONG_LIMIT",
            "LONG_ADX_BYPASS",
            "COMPRA_MARKET",
            "COMPRA_LONG_MARKET",
        ):
            return row
        if side_alvo == "SHORT" and ac in (
            "ABRE_SHORT",
            "ABRE_SHORT_LIMIT",
            "SHORT_ADX_BYPASS",
            "ABRE_SHORT_MARKET",
        ):
            return row
    return None


def _upsert_outcomes_sync(rows: list[dict[str, Any]]) -> None:
    if logger.supabase is None or not rows:
        return
    logger.supabase.table("trade_outcomes").upsert(rows, on_conflict="order_id").execute()


async def _auditoria_outcome_engine_task() -> None:
    """
    Reconciliação assíncrona:
    - a cada 5 minutos busca ordens fechadas (futures)
    - cruza com logs (ML + justificativa IA na entrada)
    - upsert em trade_outcomes
    """
    last_audit_ms = int((time.time() - 24 * 3600) * 1000)  # bootstrap: últimas 24h
    while True:
        await asyncio.sleep(OUTCOME_AUDIT_INTERVAL_S)
        try:
            if logger.supabase is None:
                continue
            ex = await asyncio.to_thread(executor_futures.criar_exchange_binance)
            sym = await asyncio.to_thread(executor_futures._resolver_simbolo_perp, ex, SYMBOL_TRADE)
            closed = await asyncio.to_thread(ex.fetch_closed_orders, sym, last_audit_ms, 200)
            now_ms = int(time.time() * 1000)
            last_audit_ms = now_ms
            if not closed:
                continue

            logs = await asyncio.to_thread(_buscar_logs_recentes_para_outcome, 500)
            upserts: list[dict[str, Any]] = []
            for order in closed:
                oid = str(order.get("id") or "").strip()
                if not oid:
                    continue
                symbol = str(order.get("symbol") or SYMBOL_TRADE)
                side_raw = str(order.get("side") or "").lower().strip()
                side = "SHORT" if side_raw == "sell" else "LONG"
                pnl = _deduzir_pnl_realizado_ordem(order)
                ts = order.get("timestamp") or order.get("lastTradeTimestamp") or now_ms
                try:
                    closed_at = datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc).isoformat()
                except Exception:
                    closed_at = datetime.now(timezone.utc).isoformat()

                row_log = _match_log_para_ordem(order, logs)
                ml_prob = _safe_float(row_log.get("probabilidade_ml")) if row_log else None
                just_ia = ""
                if row_log:
                    just_ia = str(
                        row_log.get("justificativa_ia")
                        or row_log.get("justificativa")
                        or ""
                    )[:2000]

                upserts.append(
                    {
                        "order_id": oid,
                        "symbol": symbol,
                        "side": side,
                        "ml_probability_at_entry": ml_prob,
                        "claude_justification": just_ia,
                        "pnl_realized": pnl,
                        "closed_at": closed_at,
                    }
                )
                pnl_txt = f"{pnl:+.2f}" if pnl is not None else "N/A"
                print(
                    "[AUDITORIA] Ordem "
                    f"{side} fechada. PnL Realizado: {pnl_txt} USDT. "
                    "Resultado guardado no Outcome Engine."
                )

            if upserts:
                await asyncio.to_thread(_upsert_outcomes_sync, upserts)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ [AUDITORIA] Falha no loop de reconciliação: {e}")


async def _loop_websocket_tempo_real(args: argparse.Namespace) -> None:
    """
    Engine orientado por eventos:
    - escuta WebSocket 1m Futures (ETHUSDT)
    - dispara ciclo em fechamento da vela OU pico de volume intra-vela
    - mantém o socket vivo sem bloquear durante ML/Hub/Brain (to_thread)
    - `while True` + backoff: quedas de WS não derrubam o processo; durante o sleep
      outras coroutines (manual, métricas, auditoria) continuam a correr.
    """
    del args  # mantido por compat CLI; loop é orientado por eventos WS.
    ultima_vela_fechada_ts: int | None = None
    volume_ultima_vela_fechada: float | None = None
    execucao_lock = asyncio.Lock()
    last_trigger_time = 0.0

    global _risk_fraction_cfg, _alavancagem_cfg, _trailing_callback_cfg, _trailing_activation_mult_cfg

    while True:
        try:
            print(f"🛰️ [WS] Ligando stream: {WS_FUTURES_KLINE_1M}")
            async with websockets.connect(WS_FUTURES_KLINE_1M, ping_interval=20, ping_timeout=20) as ws:
                print("✅ [WS] Conectado. Aguardando ticks 1m (ETHUSDT)...")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    k = msg.get("k") if isinstance(msg, dict) else None
                    if not isinstance(k, dict):
                        continue

                    preco = float(k.get("c") or 0.0)
                    vol_atual = float(k.get("v") or 0.0)
                    is_kline_closed = bool(k.get("x"))
                    open_time = int(k.get("t") or 0)

                    print(
                        f"[WS] Tick: Preço {preco:.2f} | Aguardando fecho de vela ou pico de volume..."
                    )

                    trigger = None
                    if is_kline_closed and open_time != ultima_vela_fechada_ts:
                        trigger = "CLOSE_1M"
                        ultima_vela_fechada_ts = open_time
                        volume_ultima_vela_fechada = vol_atual
                    elif volume_ultima_vela_fechada and volume_ultima_vela_fechada > 0:
                        fator = 1.0 + float(indicators.VOLUME_SPIKE_FRACAO_1M)
                        if vol_atual >= fator * volume_ultima_vela_fechada:
                            trigger = "VOLUME_SPIKE_INTRA_1M"

                    if not trigger:
                        continue

                    _metric_inc("triggers_total", 1)
                    now = time.monotonic()
                    if now - last_trigger_time < TRIGGER_COOLDOWN_S:
                        _metric_inc("ignored_cooldown", 1)
                        restante = TRIGGER_COOLDOWN_S - (now - last_trigger_time)
                        print(
                            f"[WS] {trigger} detetado, mas bloqueado por cooldown de "
                            f"{TRIGGER_COOLDOWN_S:.0f}s (restam {restante:.1f}s)."
                        )
                        continue

                    if execucao_lock.locked():
                        _metric_inc("triggers_ignored_lock", 1)
                        print(
                            f"⏳ [WS] Trigger={trigger}, mas bloqueado por lock de execução única "
                            "(ciclo em andamento)."
                        )
                        continue

                    cfg_dyn = await _obter_configuracoes_dinamicas()
                    _risk_fraction_cfg = float(cfg_dyn.get("risk_fraction", RISK_FRACTION_PADRAO))
                    _alavancagem_cfg = float(cfg_dyn.get("leverage", ALAVANCAGEM_PADRAO))
                    _trailing_callback_cfg = float(
                        cfg_dyn.get("trailing_callback_rate", TRAILING_CALLBACK_PADRAO)
                    )
                    _trailing_activation_mult_cfg = float(
                        cfg_dyn.get(
                            "trailing_activation_multiplier",
                            TRAILING_ACTIVATION_MULTIPLIER_PADRAO,
                        )
                    )
                    print(
                        "[WS] Config dinâmica carregada: "
                        f"risk={_risk_fraction_cfg*100:.1f}% | "
                        f"lev={_alavancagem_cfg:.2f}x | "
                        f"trail_cb={_trailing_callback_cfg:.3f}% | "
                        f"trail_act_mult={_trailing_activation_mult_cfg:.3f}"
                    )

                    permitido = await asyncio.to_thread(verificar_permissao_operacao)
                    if not permitido:
                        print("💤 [WS] Trigger recebido, mas bot em STANDBY (is_active=false).")
                        continue

                    modo = await asyncio.to_thread(obter_modo_operacao)
                    print(
                        f"🚀 [WS] Trigger={trigger} | modo={modo} | iniciando ciclo ML+Hub+Claude..."
                    )
                    async with execucao_lock:
                        last_trigger_time = time.monotonic()
                        try:
                            await _executar_ciclo_assincrono(modo, trigger)
                        finally:
                            estado = "MODO VIGIA" if posicao_aberta else "BUSCANDO OPORTUNIDADE"
                            print(f"✅ [WS] Ciclo concluído. Estado atual: {estado}")
        except Exception as e:  # noqa: BLE001
            # Auto-reconnect: não propaga — outras tasks (manual, métricas, auditoria) continuam.
            print(
                f"⚠️ [WS] Stream Binance Futures caiu ou erro ({type(e).__name__}: {e}). "
                f"Auto-reconnect após {WS_RECONNECT_DELAY_S:.0f}s...",
                flush=True,
            )
            traceback.print_exc()
            await asyncio.sleep(WS_RECONNECT_DELAY_S)


async def main() -> None:
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
    _registar_loop_principal_async()

    print(
        "\n[Maestro] Bot quantitativo | MAINNET | modo SPOT/FUTURES via Supabase (config) | "
        "TP/SL 1.02 / 0.99.\n"
    )

    if args.once:
        if not await asyncio.to_thread(verificar_permissao_operacao):
            print("💤 [STANDBY] Bot desativado via Dashboard. Ciclo único não executado.")
            return
        print("🚀 [AURIC] Bot Ativo. Iniciando ciclo de análise...")
        modo = await asyncio.to_thread(obter_modo_operacao)
        print(f"🔄 Modo de Operação detectado: {modo}")
        await _executar_ciclo_assincrono(modo, "ONCE")
        return

    print(
        "\n[Maestro] Engine assíncrono ativado: loop bloqueante removido, "
        "gatilhos por WebSocket (fecho de vela 1m / pico de volume intra-vela).\n"
    )
    start_monotonic = time.monotonic()
    manual_listener_task = asyncio.create_task(_escutar_comandos_manuais())
    metrics_task = asyncio.create_task(_metrics_reporter_task(start_monotonic))
    outcome_task = asyncio.create_task(_auditoria_outcome_engine_task())
    try:
        await _loop_websocket_tempo_real(args)
    finally:
        manual_listener_task.cancel()
        outcome_task.cancel()
        metrics_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await manual_listener_task
        with contextlib.suppress(asyncio.CancelledError):
            await outcome_task
        with contextlib.suppress(asyncio.CancelledError):
            await metrics_task


if __name__ == "__main__":
    asyncio.run(main())
