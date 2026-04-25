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
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp
import ccxt
try:
    import redis.asyncio as redis_async  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001
    redis_async = None  # type: ignore[assignment]

import brain
import executor_futures
import executor_spot
import indicators
import intelligence_hub
import intelligence_module
import logger
import ml_model
import websockets
try:
    import sentry_sdk  # type: ignore
except Exception:  # noqa: BLE001
    sentry_sdk = None
try:
    import logfire  # type: ignore
except Exception:  # noqa: BLE001
    logfire = None

def _normalizar_symbol_env_para_ccxt(raw: str) -> str:
    s = str(raw or "").strip().upper()
    if not s:
        return "ETH/USDC"
    if "/" in s:
        return s
    if s.endswith("USDC") and len(s) > 4:
        return f"{s[:-4]}/USDC"
    if s.endswith("USDT") and len(s) > 4:
        return f"{s[:-4]}/USDT"
    return s


# Motor + REST: sempre USDC-margined ETHUSDC (independente de SYMBOL no .env ou teste WS).
SYMBOL_TRADE = "ETH/USDC"
_SYMBOL_REST = "ETHUSDC"
# WebSocket: stream multiplexado na URL (Binance Futures).
_SYM_WS = _SYMBOL_REST.lower()
WS_FUTURES_STREAM = (
    f"wss://fstream.binance.com/stream?streams={_SYM_WS}@kline_1m/{_SYM_WS}@bookTicker"
)
ALAVANCAGEM_PADRAO = 6
RISK_FRACTION_PADRAO = 0.20
TRAILING_CALLBACK_PADRAO = 0.6
TRAILING_ACTIVATION_MULTIPLIER_PADRAO = 1.0
TRIGGER_COOLDOWN_S = 15.0
EMA_BUFFER_PCT = 0.0015  # 0.15% — no-trade zone para evitar whipsaw na EMA200 (15m)
CLAUDE_RATE_LIMIT_MAX_CALLS = 1
CLAUDE_RATE_LIMIT_WINDOW_S = 300.0
METRICS_FLUSH_EVERY_TRIGGERS = 50
METRICS_FLUSH_EVERY_S = 3600.0
OUTCOME_AUDIT_INTERVAL_S = 300.0
REVISAO_TESE_INTERVAL_S = 300.0
# Resiliência HFT: reconexão WS e backoff após falha de ordem (HTTP / margem / notional).
WS_RECONNECT_DELAY_S = 5.0
ORDER_FAILURE_BACKOFF_S = 5.0
# Loop asyncio principal (para asyncio.sleep desde threads do ciclo ML sem bloquear outras tasks).
_main_event_loop: asyncio.AbstractEventLoop | None = None
REDIS_ORDER_LOCK_TTL_S = max(1, int(float(os.getenv("AURIC_ORDER_LOCK_TTL_S", "5"))))
REDIS_ORDER_LOCK_URL = (os.getenv("REDIS_URL") or "redis://localhost:6379/0").strip()
_redis_async_client: Any | None = None

# Multiplicadores de saída sobre o preço de entrada gravado em memória (LONG: sobe = lucro).
FATOR_TAKE_PROFIT = 1.02  # preço >= entrada × 1.02
FATOR_STOP_LOSS = 0.994  # preço <= entrada × 0.994  (ROI -0,6%)
# SHORT (futures): lucro se preço cai; TP −2% / SL +0,6% sobre o preço de entrada.
FATOR_TAKE_PROFIT_SHORT = 0.98  # preço <= entrada × 0.98
FATOR_STOP_LOSS_SHORT = 1.006  # preço >= entrada × 1.006 (ROI -0,6%)

# Zona ML que aciona Hub + Brain: P(alta) ≥ limiar long OU P(alta) ≤ limiar short.
# Zona neutra (sessão teste HF): P ∈ [SHORT_MAX, LONG_MIN] → sem Hub/Claude.
ML_PROB_SHORT_MAX = 0.45
WAKE_FILTER_SHORT_MAX = 0.45
WAKE_FILTER_LONG_MIN = 0.55
ML_MACRO_OVERRIDE_PROB = 0.70  # Override counter-trend quando confiança estatística é muito alta.
# Anti-contra-tendência (XGBoost bruto `prob_ml_base`, antes do ajuste opcional do order book):
# P(alta) acima disto → proibido abrir SHORT; abaixo disto → proibido abrir LONG.
ML_PROB_BLOQUEIO_SHORT = float(os.getenv("AURIC_ML_ANTISHORT_PROB", "0.70"))
ML_PROB_BLOQUEIO_LONG = float(os.getenv("AURIC_ML_ANTILONG_PROB", "0.30"))
# Submissão do LLM: acima disto (LONG) / abaixo disto (SHORT) o prompt restringe vetos por notícias leves.
ML_PROB_SUBMISSAO_LONG = float(os.getenv("AURIC_ML_SUBMISSAO_LONG", "0.80"))
ML_PROB_SUBMISSAO_SHORT = float(os.getenv("AURIC_ML_SUBMISSAO_SHORT", "0.20"))

# Mínimo de confiança do Brain (0–100) para permitir entrada.
CONFIANCA_BRAIN_MIN_ENTRADA = 70
TRAILING_CALLBACK_RATE_PADRAO = 0.6
TRAILING_ACTIVATION_MULTIPLIER_PADRAO = 1.0
ROI_APERTO_SEGURANCA_ATIVACAO = 0.02  # +2%
TRAILING_CALLBACK_APERTO_SEGURANCA = 0.4
STALL_EXIT_VELAS_15M = 4
TRAILING_CALLBACK_RSI_TIGHTEN = 0.3
TRAILING_CALLBACK_GOD_MODE = 0.4
ROI_GOD_MODE_ATIVACAO = 0.005  # +0.5%
RSI_GOD_MODE_ATIVACAO = 70.0
# ETH/USDC futures: realização parcial + spread guard (pausa refresh de trailing na bolsa).
PARTIAL_TP_ROI_FRAC = 0.006  # 0,6% ROI — saída híbrida (50% + SL break-even + trailing na «moon bag»)
PARTIAL_TP_CLOSE_FRAC = 0.5
# Trailing só sobre a metade restante após o parcial (callbackRate Binance em %).
TRAILING_CALLBACK_APOS_PARTIAL_TP = 0.6  # 0.6 == 0,6%
SPREAD_GUARD_MAX_FRAC = 0.001  # (ask−bid)/mid > 0,1%
SPREAD_GUARD_PAUSE_S = 5.0

# --- Observação humana → prompt do Brain (Claude), secção [USER_SENTIMENT_CONTEXT] / Contexto Humano ---
# Supabase: tabela `bot_commands`, key='market_observation', colunas active + value (ver migração).
# RAM (opcional): atribui manualmente (ex. REPL): main.USER_MARKET_OBSERVATION = "Double bottom em 15m"
USER_MARKET_OBSERVATION: str | None = None
# True: apaga a observação logo após cada chamada bem-sucedida ao Brain neste ciclo.
# False: mantém até mudares USER_MARKET_OBSERVATION (ou None) manualmente.
USER_MARKET_OBSERVATION_CLEAR_AFTER_BRAIN_CALL = False
# Auto-expiração da linha `market_observation` no Supabase (idade desde `updated_at`).
MARKET_OBSERVATION_EXPIRY_MINUTES = 30

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
_aperto_seguranca_ativo: bool = False
_rsi_tighten_ativo: bool = False
_short_stall_count_15m: int = 0
_short_last_candle_ts_15m: int | None = None
_short_last_low_15m: float | None = None
_god_mode_auto_ativo: bool = False
_ultimo_preco_ws: float = 0.0
_best_bid_ws: float = 0.0
_best_ask_ws: float = 0.0
_ultimo_tick_ws_ts: float = 0.0
_ws_force_restart_requested: bool = False
_equity_inicio_dia: float | None = None
_travado_ate_ts: float = 0.0
_spread_trailing_pause_until: float = 0.0
_partial_tp_pos_key: tuple[float, str] | None = None
_partial_tp_locked: bool = False
_ultimo_rsi_14: float = 0.0
_ultimo_adx_14: float = 0.0
# Watchdog de vela 1m: minuto UTC (0–59) visto com vela fechada (x) no WS; alinhado ao relógio do watchdog.
ultimo_minuto_processado: int = -1
# Lock + cooldown partilhados entre WebSocket e `safe_rodar_ciclo` (watchdog).
_gatilho_ciclo_lock: asyncio.Lock | None = None
_ultimo_gatilho_ciclo_mono: float = 0.0
_watchdog_relogio_task: asyncio.Task[None] | None = None
_ultimo_contexto_raw_supabase: str = "{}"
_ultimo_whale_flow_score: float = 0.0
_ultimo_whale_flow_signal: str = "NEUTRAL"
_ultimo_social_sentiment_score: float = 0.0
_ultimo_news_sentiment_score: int = 0
_ultimo_minuto_previsao_candle: int = -1
_ultimo_preco_alvo_previsao: float | None = None
_ultima_tendencia_alta_previsao: bool | None = None
_ultimo_llava_veto: bool = False
_ultima_etapa_funil: str = "IDLE"
_ultima_razao_abort_funil: str | None = None
_ultima_ml_prob_base: float | None = None
_ultima_ml_prob_calibrada: float | None = None
justificativa_ia: str = "Aguardando primeira leitura..."
# Heartbeat Supabase em MODO VIGIA (não bloqueia TP/SL — envio em thread daemon).
VIGIA_HEARTBEAT_INTERVAL_S = 30.0
_vigia_heartbeat_last_monotonic: float = 0.0
# Log único de confirmação de direção/gatilhos ao entrar numa chave (entrada, lado) de vigia.
_vigia_gatilhos_confirm_key: tuple[float, str] | None = None
ultima_revisao_tese_ts: float = 0.0
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


def _redis_lock_key_symbol(symbol: str) -> str:
    clean = "".join(ch for ch in str(symbol or "").upper() if ch.isalnum())
    return f"lock_{clean or 'SYMBOL'}"


async def _get_redis_async_client() -> Any | None:
    global _redis_async_client
    if redis_async is None:
        return None
    if _redis_async_client is not None:
        return _redis_async_client
    try:
        _redis_async_client = redis_async.from_url(
            REDIS_ORDER_LOCK_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
        )
        await _redis_async_client.ping()
        return _redis_async_client
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ [REDIS] indisponível ({REDIS_ORDER_LOCK_URL}): {e}.", flush=True)
        _redis_async_client = None
        return None


async def _acquire_redis_order_lock(symbol: str) -> tuple[str, str] | None:
    """
    Lock atômico com Redis (SET NX EX).
    - None => Redis indisponível (segue sem lock).
    - ("","") => lock já existe (pular execução).
    - (key, token) => lock adquirido.
    """
    cli = await _get_redis_async_client()
    if cli is None:
        return None
    key = _redis_lock_key_symbol(symbol)
    token = f"{os.getpid()}-{time.time_ns()}"
    try:
        ok = bool(await cli.set(key, token, nx=True, ex=REDIS_ORDER_LOCK_TTL_S))
        if ok:
            return key, token
        return ("", "")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ [REDIS] falha ao criar lock {key}: {e}.", flush=True)
        return None


async def _release_redis_order_lock(lock_ctx: tuple[str, str] | None) -> None:
    if not lock_ctx:
        return
    key, token = lock_ctx
    if not key:
        return
    cli = await _get_redis_async_client()
    if cli is None:
        return
    # Release seguro (somente se token ainda for o nosso).
    lua = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
else
  return 0
end
"""
    try:
        await cli.eval(lua, 1, key, token)
    except Exception:
        pass


def _registar_loop_principal_async() -> None:
    """
    Grava o loop em execução para permitir `asyncio.sleep` desde `rodar_ciclo`
    (thread worker) sem bloquear o event loop (graceful degradation / anti-spam).
    """
    global _main_event_loop
    _main_event_loop = asyncio.get_running_loop()


def _sleep_off_event_loop(sec: float, timeout_buf: float = 30.0) -> None:
    """
    Pausa chamada desde threads (ex.: `asyncio.to_thread(rodar_ciclo)` ou comando manual).
    Agenda `asyncio.sleep` no loop principal; se não houver loop, usa `Event.wait`
    (evita `time.sleep` e não bloqueia o asyncio na thread do motor WS).
    """
    loop = _main_event_loop
    s = float(sec)
    if loop is not None and loop.is_running():
        try:
            fut = asyncio.run_coroutine_threadsafe(asyncio.sleep(s), loop)
            fut.result(timeout=s + timeout_buf)
            return
        except Exception:
            pass
    threading.Event().wait(s)


def _cooldown_apos_falha_ordem_desde_thread_ciclo() -> None:
    """
    Chamado desde `rodar_ciclo` (asyncio.to_thread): após falha de ordem ML,
    impõe pausa antes de novos HTTP — usa o loop principal para não parar
    o WebSocket, métricas ou listener manual durante o backoff.
    """
    sec = float(ORDER_FAILURE_BACKOFF_S)
    print(
        f"⏳ [RATE LIMIT] Backoff {sec:.0f}s após falha de execução de ordem (anti-spam HTTP).",
        flush=True,
    )
    _sleep_off_event_loop(sec)


def _commission_and_maker_from_order(ordem: dict[str, Any]) -> tuple[float | None, bool]:
    fee = ordem.get("fee") or {}
    c = fee.get("cost") if isinstance(fee, dict) else None
    try:
        commission = float(c) if c is not None else None
    except (TypeError, ValueError):
        commission = None
    info = ordem.get("info") or {}
    maker = False
    if isinstance(info, dict):
        maker = str(info.get("maker") or "").lower() in ("true", "1", "yes")
    if commission is not None and commission == 0.0:
        maker = True
    return commission, maker


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


def _formatar_peso_conflito_humano_xgboost() -> str:
    """Rodapé padrão: observação humana de *bloqueio* (Veto) vs XGBoost."""
    return (
        "PESO VETO em conflito (obrigatório): observação humana de **bloqueio** (ex.: double top/bottom) — "
        "atribui **peso relativo 2.0** para **não** confirmar a entrada alinhada só ao XGBoost quando isso "
        "materialmente divergir; em empate, o veto humano prevalece (VETO ou CAUTIOUS)."
    )


def _formatar_peso_turbo_long_vs_xgboost() -> str:
    return (
        "PESO TURBO — LONG (obrigatório): o operador activou **TURBO LONG**. Atribui **peso relativo 2.0** a "
        "**forçar/reforçar** LONG / tese de compra **mesmo** com XGBoost neutro (ex.: P(alta) entre ~0,45 e ~0,55) — "
        "procura motivos concretos no contexto agregado e técnico para comprar; só VETO se armadilha for óbvia. "
        "Em `justificativa_curta` inclui obrigatoriamente o literal **[TURBO MODE ATIVO]**."
    )


def _formatar_peso_turbo_short_vs_xgboost() -> str:
    return (
        "PESO TURBO — SHORT (obrigatório): o operador activou **TURBO SHORT**. Atribui **peso relativo 2.0** a "
        "**forçar/reforçar** SHORT; podes **desvalorizar** viés macro de alta se necessário e focar na execução "
        "imediata da tese baixista com confluência operacional. Em `justificativa_curta` inclui obrigatoriamente "
        "**[TURBO MODE ATIVO]**."
    )


def _footer_peso_contexto_humano(valores_fonte: list[str | None]) -> str:
    """Turbo (forçar entrada) vs Veto (bloquear): um rodapé conforme o comando humano."""
    blob = " ".join(str(v).lower() for v in valores_fonte if v and str(v).strip())
    if "turbo long" in blob:
        return _formatar_peso_turbo_long_vs_xgboost()
    if "turbo short" in blob:
        return _formatar_peso_turbo_short_vs_xgboost()
    return _formatar_peso_conflito_humano_xgboost()


def _corpo_contexto_humano_por_texto(valor: str) -> str:
    """Corpo narrativo para o Brain (sem linha de peso; combinação junta o peso uma vez)."""
    v = (valor or "").strip()
    if not v:
        return ""
    low = v.lower()
    if "turbo long" in low:
        return (
            f"Comando humano: {v}\n\n"
            "Modo **TURBO LONG** (espelho oposto ao Veto): prioriza encontrar motivos para **comprar** e "
            "alinhar BULLISH / `posicao_recomendada` LONG mesmo com ML **neutro** (P(alta) tipicamente ~0,45–0,55); "
            "usa notícias e técnico como catalisador, não como desculpa para ficar neutro se a tese de compra for plausível."
        )
    if "turbo short" in low:
        return (
            f"Comando humano: {v}\n\n"
            "Modo **TURBO SHORT**: prioriza tese **vendedora**; se o contexto macro empurrar alta mas a nota "
            "técnica/operador pede SHORT, **podes ignorar** esse viés macro em favor da execução da venda com "
            "confluência razoável (preço, resistências, catalisador de curto prazo)."
        )
    if "double bottom" in low or "fundo duplo" in low:
        return (
            f"Padrão reportado: {v}\n\n"
            "Instrução extra: com Double Bottom / Fundo Duplo, se o RSI no bloco técnico estiver "
            "**baixo** (sobrevenda / RSI que não confirma pressão de venda), **não** confirmes "
            "SHORT apenas com base no XGBoost — favorece VETO, CAUTIOUS ou leitura de armadilha "
            "para shorts em suporte."
        )
    if "double top" in low or "topo duplo" in low:
        return (
            f"Padrão reportado: {v}\n\n"
            "Instrução extra: com Double Top / Topo Duplo, se houver **rejeição em resistência** "
            "(falha de rompimento, pavios superiores, recuo após teste do topo), **não** confirmes "
            "LONG apenas com base no XGBoost — favorece VETO, CAUTIOUS ou bull trap."
        )
    return f"OBSERVAÇÃO DO FOUNDER: {v}"


def _combinar_contexto_humano_supabase_e_ram(
    valor_supabase: str | None,
    texto_ram: str | None,
) -> str | None:
    corpos: list[str] = []
    if valor_supabase and str(valor_supabase).strip():
        corpos.append(
            "[Fonte: Supabase `bot_commands` key=market_observation, active]\n"
            + _corpo_contexto_humano_por_texto(str(valor_supabase).strip())
        )
    if texto_ram and str(texto_ram).strip():
        corpos.append(
            "[Fonte: sessão RAM `USER_MARKET_OBSERVATION`]\n"
            + _corpo_contexto_humano_por_texto(str(texto_ram).strip())
        )
    if not corpos:
        return None
    return (
        "\n\n---\n\n".join(corpos)
        + "\n\n"
        + _footer_peso_contexto_humano([valor_supabase, texto_ram])
    )


def _contexto_tem_turbo(valor_supabase: str | None, texto_ram: str | None) -> bool:
    blob = f"{valor_supabase or ''} {texto_ram or ''}".upper()
    return "TURBO" in blob


def _inferir_direcao_sugerida_turbo(valor_supabase: str | None, texto_ram: str | None) -> str:
    blob = f"{valor_supabase or ''} {texto_ram or ''}".upper()
    if "TURBO SHORT" in blob:
        return "SHORT"
    if "TURBO LONG" in blob:
        return "LONG"
    return "LONG"


def _contexto_tem_catastrofe_sistemica(texto: str) -> bool:
    """
    Eventos graves no texto agregado (notícias + alertas + justificativa).
    Usado para manter VETO LONG mesmo com P(alta) muito alta (submissão ML).
    """
    t = (texto or "").lower()
    needles = (
        "hack",
        "hacked",
        "security breach",
        "bridge hack",
        "bridge exploit",
        "bankruptcy",
        "bankrupt",
        "insolvent",
        "insolvency",
        "sec sue",
        "sec lawsuit",
        "sec charges",
        "criminal charge",
        "halt withdrawal",
        "paused withdrawal",
        "withdrawals suspended",
        "exchange hacked",
        "fraud charges",
        "falência",
        "falencia",
        "binance foi hackeada",
        "custody loss",
    )
    return any(n in t for n in needles)


def _contexto_tem_catalisador_altista_estrutural(texto: str) -> bool:
    """Catalisador raro que pode manter VETO SHORT mesmo com P(alta) muito baixa."""
    t = (texto or "").lower()
    needles = (
        "etf approved",
        "etf approval",
        "sec approves bitcoin",
        "sec approves spot",
        "spot etf approved",
        "major regulatory approval",
    )
    return any(n in t for n in needles)


def _parse_supabase_updated_at(raw: Any) -> datetime | None:
    """Converte `updated_at` do PostgREST para UTC; None se inválido."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def obter_valor_market_observation_supabase() -> str | None:
    """
    Lê `bot_commands` onde key == 'market_observation'.
    Se `active` for True, devolve o texto em `value` (trim); caso contrário None.
    Se `updated_at` tiver mais de MARKET_OBSERVATION_EXPIRY_MINUTES, ignora o valor,
    tenta desativar a linha no Supabase e devolve None.
    """
    if logger.supabase is None:
        return None
    try:
        res = (
            logger.supabase.table("bot_commands")
            .select("active", "value", "updated_at")
            .eq("key", "market_observation")
            .limit(1)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
        if not rows:
            return None
        row = rows[0]
        if not bool(row.get("active")):
            return None

        updated_dt = _parse_supabase_updated_at(row.get("updated_at"))
        if updated_dt is not None:
            age = datetime.now(timezone.utc) - updated_dt
            if age > timedelta(minutes=MARKET_OBSERVATION_EXPIRY_MINUTES):
                print(
                    "[SUPABASE] ⏰ Sinal de mercado expirou por tempo (30min) e foi desativado."
                )
                try:
                    logger.supabase.table("bot_commands").update(
                        {
                            "active": False,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ).eq("key", "market_observation").execute()
                except Exception as e_exp:  # noqa: BLE001
                    print(
                        f"⚠️ [SUPABASE] Falha ao desativar observação expirada (bot continua): {e_exp}"
                    )
                return None

        raw = row.get("value")
        if raw is None:
            return None
        s = str(raw).strip()
        return s or None
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ Erro ao ler observação do Supabase: {e}")
        return None


def obter_parametros_trailing_supabase() -> tuple[float, float]:
    """
    Lê parâmetros dinâmicos do trailing no Supabase (`config`, id=1):
    - `trailing_callback_rate` (percentual; ex.: 0.6)
    - `trailing_activation_multiplier` (multiplicador do antigo TP; ex.: 1.0)

    Fallback seguro:
    - callback_rate = 0.6
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
    - risk_fraction=0.20
    - leverage=3.0
    - trailing_callback_rate=0.6
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
    Lê o saldo USDC na carteira de Futures via CCXT.

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
        raw_usdc = total.get("USDC")
        if raw_usdc is None:
            print("⚠️ atualizar_saldo_supabase: total['USDC'] ausente no balance futures.")
            return
        saldo = float(raw_usdc)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ atualizar_saldo_supabase (Binance): {e}")
        return
    try:
        logger.persistir_saldo_usdt(saldo)
        logger.persistir_whale_flow_score(
            float(_ultimo_whale_flow_score),
            social_sentiment_score=float(_ultimo_social_sentiment_score),
        )
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ atualizar_saldo_supabase (Supabase): {e}")
        return
    rf = float(_risk_fraction_cfg)
    lev = float(_alavancagem_cfg)
    notional_ref = float(saldo) * rf * lev
    print(
        f"💰 [WALLET] Position sizing (futuros): {saldo:.4f} USDC (margem) × "
        f"risk={rf*100:.1f}% × lev={lev:g}x → notional_ref≈{notional_ref:.2f} USDC "
        "(qty base ≈ notional / preço ticker)."
    )


def _upsert_god_mode_trailing_sync(taxa_agressiva: float) -> None:
    if logger.supabase is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "id": 1,
        "trailing_callback_rate": float(taxa_agressiva),
        "trailing_rate": float(taxa_agressiva),
        "updated_at": now,
    }
    logger.supabase.table("bot_config").upsert(payload, on_conflict="id").execute()


def _vigia_eth_usdc_futures(modo_label: str) -> bool:
    u = SYMBOL_TRADE.upper()
    return "FUTURES" in modo_label and "ETH" in u and "USDC" in u


def _vigia_trailing_updates_allowed() -> bool:
    return time.time() >= float(_spread_trailing_pause_until)


def _reset_vigia_eth_usdc_guards() -> None:
    global _spread_trailing_pause_until, _partial_tp_pos_key, _partial_tp_locked
    global _vigia_heartbeat_last_monotonic, _vigia_gatilhos_confirm_key
    _spread_trailing_pause_until = 0.0
    _partial_tp_pos_key = None
    _partial_tp_locked = False
    _vigia_heartbeat_last_monotonic = 0.0
    _vigia_gatilhos_confirm_key = None
    executor_futures.reset_protective_order_guard_throttle()


def _maybe_emit_vigia_heartbeat_supabase(
    *,
    preco: float,
    roi_frac: float,
    lado: str,
    trailing_pct: float,
    par_moeda: str,
    contexto_raw: str | None = None,
    rsi_14: float | None = None,
    adx_14: float | None = None,
) -> None:
    """
    Log leve para o Supabase ~1×/30s enquanto em vigia, sem bloquear o ciclo (thread daemon).
    """
    global _vigia_heartbeat_last_monotonic
    now = time.monotonic()
    if now - _vigia_heartbeat_last_monotonic < float(VIGIA_HEARTBEAT_INTERVAL_S):
        return
    _vigia_heartbeat_last_monotonic = now

    lado_u = str(lado).upper().strip() or "LONG"
    roi_pct = float(roi_frac) * 100.0
    trail_disp = float(trailing_pct)
    px = float(preco)
    texto = (
        f"👀 [VIGIA] Gerindo posição {lado_u} | ROI: {roi_pct:.2f}% | "
        f"Preço: {px:.4f} | Trailing: {trail_disp:.1f}%."
    )

    def _run() -> None:
        try:
            logger.registrar_log_trade(
                par_moeda=par_moeda,
                preco=px,
                prob_ml=0.0,
                sentimento="—",
                acao="VIGIA_HEARTBEAT",
                justificativa=texto,
                contexto_raw=contexto_raw,
                justificativa_ia=(
                    f"VIGIA_HEARTBEAT | RSI(14)={rsi_14:.2f} | ADX(14)={adx_14:.2f}"
                    if rsi_14 is not None and adx_14 is not None
                    else "VIGIA_HEARTBEAT | indicadores indisponíveis no ciclo"
                ),
                rsi_14=rsi_14,
                adx_14=adx_14,
            )
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def _sync_entry_price_supabase() -> None:
    """
    Sincroniza estado da posição no Supabase para o dashboard:
    - wallet_status(id=1): posicao_aberta + entry_price
    - fallback bot_control(id=1) quando schema/colunas divergirem
    """
    if logger.supabase is None:
        return
    opened = bool(posicao_aberta and float(preco_compra) > 0)
    # Limpa estado antigo no dashboard: sem posição => entry_price explícito em 0.0
    entry_price = float(preco_compra) if opened else 0.0
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "id": 1,
        "posicao_aberta": opened,
        "entry_price": entry_price,
        "whale_flow_score": float(_ultimo_whale_flow_score),
        "social_sentiment_score": float(_ultimo_social_sentiment_score),
        "news_sentiment_score": int(_ultimo_news_sentiment_score),
        "forecast_preco_alvo": _ultimo_preco_alvo_previsao,
        "forecast_tendencia_alta": _ultima_tendencia_alta_previsao,
        "llava_veto": bool(_ultimo_llava_veto),
        "funnel_stage": str(_ultima_etapa_funil),
        "funnel_abort_reason": _ultima_razao_abort_funil,
        "ml_prob_base": _ultima_ml_prob_base,
        "ml_prob_calibrated": _ultima_ml_prob_calibrada,
        "updated_at": now_iso,
    }
    try:
        logger.supabase.table("wallet_status").upsert(payload, on_conflict="id").execute()
        return
    except Exception as e_ws:  # noqa: BLE001
        print(f"⚠️ [STATE SYNC] wallet_status entry_price falhou: {e_ws}")
    try:
        logger.supabase.table("bot_control").update(payload).eq("id", 1).execute()
    except Exception as e_bc:  # noqa: BLE001
        print(f"⚠️ [STATE SYNC] bot_control entry_price fallback falhou: {e_bc}")


def _exit_meta_contexto_json(*, partial_tp_50: bool, stop_hit: bool) -> str:
    return json.dumps(
        {
            "auric_exit_meta": {
                "partial_tp_50": bool(partial_tp_50),
                "stop_hit": bool(stop_hit),
                "sl_dynamic_vs_trailing": True,
            }
        }
    )


def _emit_funnel_observability(event: str, payload: dict[str, Any]) -> None:
    """Telemetria opcional para Sentry/Logfire sem hard dependency."""
    try:
        if sentry_sdk is not None:
            with sentry_sdk.push_scope() as scope:  # type: ignore[attr-defined]
                for k, v in payload.items():
                    scope.set_extra(str(k), v)
                sentry_sdk.capture_message(f"[FUNIL] {event}")  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        if logfire is not None and hasattr(logfire, "info"):
            logfire.info(f"[FUNIL] {event}", **payload)  # type: ignore[attr-defined]
    except Exception:
        pass


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
                ordem_manual = executor_futures.abrir_long_market(
                    SYMBOL_TRADE,
                    ex,
                    alavancagem=float(_alavancagem_cfg),
                    risk_fraction=float(_risk_fraction_cfg),
                    trailing_callback_rate=trailing_cb,
                    trailing_activation_multiplier=trailing_mult,
                )
                if isinstance(ordem_manual, dict) and bool(ordem_manual.get("auric_skipped")):
                    print("⏭️ [MANUAL] LONG ignorado: lock Redis ativo (anti-spam).")
                    continue
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
                ordem_manual = executor_futures.abrir_short_market(
                    SYMBOL_TRADE,
                    ex,
                    alavancagem=float(_alavancagem_cfg),
                    risk_fraction=float(_risk_fraction_cfg),
                    trailing_callback_rate=trailing_cb,
                    trailing_activation_multiplier=trailing_mult,
                )
                if isinstance(ordem_manual, dict) and bool(ordem_manual.get("auric_skipped")):
                    print("⏭️ [MANUAL] SHORT ignorado: lock Redis ativo (anti-spam).")
                    continue
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


def _limpar_comandos_manuais_pendentes_sync() -> None:
    """Evita fila travada: marca pendências antigas como FAILED."""
    if logger.supabase is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        logger.supabase.table("manual_commands").update(
            {"status": "FAILED", "updated_at": now}
        ).eq("status", "PENDING").execute()
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ [MANUAL LISTENER] limpeza status=PENDING falhou: {e}")
    try:
        logger.supabase.table("manual_commands").update(
            {"executed": True, "updated_at": now}
        ).eq("executed", False).execute()
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ [MANUAL LISTENER] limpeza executed=false falhou: {e}")


def _preco_entrada_fallback_de_ordem(ordem: dict[str, Any]) -> float:
    for k in ("auric_entry_price", "average", "price"):
        v = ordem.get(k)
        if v is not None:
            try:
                fv = float(v)
                if fv > 0:
                    return fv
            except (TypeError, ValueError):
                continue
    return 0.0


def _god_mode_sincronizar_ram_e_supabase_posicao_vigia_sync(
    cmd: str,
    ordem: dict[str, Any],
    manual_row_id: int | None,
) -> None:
    """
    Após LONG/SHORT manual na Binance (já com bracket): alinha RAM e grava log de abertura
    no Supabase para o próximo ciclo WS tratar como Modo Vigia sem reconciliação tardia.
    """
    global posicao_aberta, preco_compra, direcao_posicao

    c = str(cmd or "").upper().strip()
    if c == "LONG":
        direcao = "LONG"
        acao = "COMPRA_LONG"
    elif c == "SHORT":
        direcao = "SHORT"
        acao = "ABRE_SHORT"
    else:
        return

    pe = float(ordem.get("auric_entry_price") or 0.0)
    if pe <= 0:
        pe = _preco_entrada_fallback_de_ordem(ordem)
    if pe <= 0:
        try:
            ex_snap = executor_futures.criar_exchange_binance()
            snap = executor_futures.consultar_posicao_futures(SYMBOL_TRADE, ex_snap)
            ep = snap.get("entry_price")
            if ep is not None and float(ep) > 0:
                pe = float(ep)
        except Exception as e_snap:  # noqa: BLE001
            print(f"⚠️ [GOD MODE] Fallback entryPrice carteira: {e_snap}")

    if pe <= 0:
        print(
            "⚠️ [GOD MODE] preço de entrada indeterminado — RAM não atualizada; "
            "reconciliação no próximo ciclo pode corrigir."
        )
        return

    ref_manual = (
        f"manual_commands id={int(manual_row_id)}"
        if manual_row_id is not None
        else "manual_commands (id desconhecido)"
    )
    oid = ordem.get("id")
    just = (
        f"GOD MODE {direcao} via dashboard/Supabase; {ref_manual}. "
        f"Bracket Binance (SL STOP_MARKET + TRAILING_STOP_MARKET) já ativo. "
        f"ordem_ref id={oid} | entry ref.={pe:.6f} USDC."
    )

    logger.registrar_log_trade(
        par_moeda=SYMBOL_TRADE,
        preco=float(pe),
        prob_ml=-1.0,
        sentimento="MANUAL",
        acao=acao,
        justificativa=just,
        lado_ordem=direcao,
        contexto_raw=None,
        justificativa_ia="GOD_MODE_HANDOFF_VIGIA",
    )

    posicao_aberta = True
    preco_compra = float(pe)
    direcao_posicao = direcao
    print(
        f"👑 [GOD MODE] Handoff Modo Vigia: posicao_aberta=True | {direcao_posicao} @ "
        f"{preco_compra:.4f} USDC | log Supabase gravado ({acao})."
    )


def _normalizar_comando_manual_god_mode(cmd: str) -> str:
    """Aceita LONG/SHORT do dashboard ou variantes MANUAL LONG / MANUAL SHORT."""
    c = str(cmd or "").upper().strip()
    if c.startswith("MANUAL "):
        c = c.removeprefix("MANUAL ").strip()
    return c


def _executar_comando_manual_imediato_sync(
    cmd: str,
    manual_row_id: int | None = None,
) -> None:
    """
    Executor GOD MODE: sem filtros de veto (RSI/ADX/VWAP).
    LONG/SHORT: após fill na Binance, atualiza RAM + Supabase para o WS já gerir trailing.
    """
    global _risk_fraction_cfg, _alavancagem_cfg, _trailing_callback_cfg, _trailing_activation_mult_cfg
    global posicao_aberta, preco_compra, direcao_posicao
    global _ultimo_preco_ws, _best_bid_ws, _best_ask_ws

    ex = executor_futures.criar_exchange_binance()
    c = _normalizar_comando_manual_god_mode(cmd)
    if c in ("LONG", "SHORT"):
        max_tentativas = 5
        intervalo_s = 0.5
        deadline = time.monotonic() + 3.0
        tentativas = 0
        preco_ws = float(_ultimo_preco_ws or 0.0)
        best_bid = float(_best_bid_ws or 0.0)
        best_ask = float(_best_ask_ws or 0.0)
        rest_fallback_ok = False
        while tentativas < max_tentativas and time.monotonic() < deadline:
            best_bid = float(_best_bid_ws or best_bid or 0.0)
            best_ask = float(_best_ask_ws or best_ask or 0.0)
            if preco_ws > 0:
                print("🎯 [WARM-UP] Sistema 100% pronto. Executando...")
                break
            if tentativas >= 2 and not rest_fallback_ok:
                rest_symbol = executor_futures.MARKET_SYMBOL
                rest_px = executor_futures.get_price_via_rest(rest_symbol)
                rp = float(rest_px.get("price") or 0.0)
                rb = float(rest_px.get("bid") or 0.0)
                ra = float(rest_px.get("ask") or 0.0)
                if rp > 0:
                    _ultimo_preco_ws = rp
                    preco_ws = rp
                    if rb > 0:
                        best_bid = rb
                        _best_bid_ws = rb
                    if ra > 0:
                        best_ask = ra
                        _best_ask_ws = ra
                    rest_fallback_ok = True
                    print(
                        "⚠️ [REST-FALLBACK] WebSocket falhou, mas preço obtido via REST. "
                        "Executando Force Command..."
                    )
                    print("🎯 [WARM-UP] Sistema 100% pronto. Executando...")
                    break
            print("⏳ [FORCE-WAIT] Aguardando Preço + Orderbook (L2)...")
            _sleep_off_event_loop(intervalo_s)
            tentativas += 1
            preco_ws = float(_ultimo_preco_ws or 0.0)
        if not (preco_ws > 0):
            print(
                f"[DEBUG-FAIL] Price: {preco_ws} | Bid: {best_bid} | Ask: {best_ask} | Symbol: {SYMBOL_TRADE}."
            )
            raise RuntimeError(
                f"❌ [DATA-ERROR] Falha em: [bid: {best_bid}, ask: {best_ask}, price: {preco_ws}]."
            )

    if c == "LONG":
        try:
            ordem = executor_futures.abrir_long_market(
                SYMBOL_TRADE,
                ex,
                alavancagem=float(_alavancagem_cfg),
                risk_fraction=float(_risk_fraction_cfg),
                trailing_callback_rate=float(_trailing_callback_cfg),
                trailing_activation_multiplier=float(_trailing_activation_mult_cfg),
                force_reference_price=float(_ultimo_preco_ws or 0.0),
                is_manual_force=manual_row_id is not None,
            )
        except RuntimeError as e_long:
            err_txt = str(e_long)
            if "DATA-ERROR" not in err_txt or manual_row_id is None:
                raise
            ws_ref = float(_ultimo_preco_ws or 0.0)
            if ws_ref <= 0:
                ws_ref = float(_preco_via_ccxt_ticker(SYMBOL_TRADE, "FUTURES") or 0.0)
            print(
                f"⚠️ [FORCE-BYPASS] Aviso manual LONG id={manual_row_id}: {err_txt} "
                f"| retry final com ref={ws_ref:.4f}"
            )
            if ws_ref <= 0:
                return
            ordem = executor_futures.abrir_long_market(
                SYMBOL_TRADE,
                ex,
                alavancagem=float(_alavancagem_cfg),
                risk_fraction=float(_risk_fraction_cfg),
                trailing_callback_rate=float(_trailing_callback_cfg),
                trailing_activation_multiplier=float(_trailing_activation_mult_cfg),
                force_reference_price=ws_ref,
                is_manual_force=manual_row_id is not None,
            )
        if bool(ordem.get("auric_skipped")):
            print("⏭️ [GOD MODE] LONG ignorado: lock Redis ativo (anti-spam).")
            return
        _god_mode_sincronizar_ram_e_supabase_posicao_vigia_sync(c, ordem, manual_row_id)
        return
    if c == "SHORT":
        try:
            ordem = executor_futures.abrir_short_market(
                SYMBOL_TRADE,
                ex,
                alavancagem=float(_alavancagem_cfg),
                risk_fraction=float(_risk_fraction_cfg),
                trailing_callback_rate=float(_trailing_callback_cfg),
                trailing_activation_multiplier=float(_trailing_activation_mult_cfg),
                force_reference_price=float(_ultimo_preco_ws or 0.0),
                is_manual_force=manual_row_id is not None,
            )
        except RuntimeError as e_short:
            err_txt = str(e_short)
            if "DATA-ERROR" not in err_txt or manual_row_id is None:
                raise
            ws_ref = float(_ultimo_preco_ws or 0.0)
            if ws_ref <= 0:
                ws_ref = float(_preco_via_ccxt_ticker(SYMBOL_TRADE, "FUTURES") or 0.0)
            print(
                f"⚠️ [FORCE-BYPASS] Aviso manual SHORT id={manual_row_id}: {err_txt} "
                f"| retry final com ref={ws_ref:.4f}"
            )
            if ws_ref <= 0:
                return
            ordem = executor_futures.abrir_short_market(
                SYMBOL_TRADE,
                ex,
                alavancagem=float(_alavancagem_cfg),
                risk_fraction=float(_risk_fraction_cfg),
                trailing_callback_rate=float(_trailing_callback_cfg),
                trailing_activation_multiplier=float(_trailing_activation_mult_cfg),
                force_reference_price=ws_ref,
                is_manual_force=manual_row_id is not None,
            )
        if bool(ordem.get("auric_skipped")):
            print("⏭️ [GOD MODE] SHORT ignorado: lock Redis ativo (anti-spam).")
            return
        _god_mode_sincronizar_ram_e_supabase_posicao_vigia_sync(c, ordem, manual_row_id)
        return
    if c in ("CLOSE", "CLOSE_ALL"):
        pe = float(preco_compra)
        dr = str(direcao_posicao)
        px_close = float(_ultimo_preco_ws or 0.0)
        if px_close <= 0:
            px_close = float(_preco_via_ccxt_ticker(SYMBOL_TRADE, "FUTURES") or 0.0)
        ordem = executor_futures.fechar_posicao_market(SYMBOL_TRADE, ex)
        try:
            _gravar_performance_fecho_trade(
                preco_entrada=pe,
                preco_ref_ciclo=px_close,
                direcao=dr,
                ordem=ordem,
                exit_type="FORCE_CLOSE",
            )
        except Exception as e_gc:  # noqa: BLE001
            print(f"⚠️ [GOD MODE] CLOSE Supabase performance: {e_gc}")
        posicao_aberta = False
        preco_compra = 0.0
        direcao_posicao = "LONG"
        _reset_vigia_eth_usdc_guards()
        _sync_entry_price_supabase()
        print("👑 [GOD MODE] CLOSE: RAM resetada (sem posição).")
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
    global _ultimo_tick_ws_ts, _ws_force_restart_requested

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
            f"\n👑 [GOD MODE] Comando MANUAL {cmd} intercetado e a ser executado INSTANTANEAMENTE! (id={rid})"
        )
        if time.monotonic() - float(_ultimo_tick_ws_ts or 0.0) > 10.0:
            _ws_force_restart_requested = True
            print("\n⚠️ [WS-WATCHDOG] Sem ticks há >10s. Solicitando ws.restart() automático.")

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
            await asyncio.to_thread(_executar_comando_manual_imediato_sync, cmd, rid)
            print(f"\n✅ [MANUAL LISTENER] Comando {cmd} (id={rid}) executado com sucesso.")
        except Exception as e:  # noqa: BLE001
            print(f"\n❌ [MANUAL LISTENER] Falha ao executar {cmd} (id={rid}): {e}")
            await asyncio.to_thread(_mark_manual_command_status_sync, rid, "FAILED")
            await asyncio.to_thread(_limpar_comandos_manuais_pendentes_sync)
            print(
                f"\n⏳ [MANUAL LISTENER] Cooldown {ORDER_FAILURE_BACKOFF_S:.0f}s após falha "
                "(anti-spam / rate limit exchange)."
            )
            await asyncio.sleep(ORDER_FAILURE_BACKOFF_S)


def _simbolo_binance_rest(simbolo: str) -> str:
    """ETH/USDC ou ETH/USDC:USDC → ETHUSDC (formato query Binance)."""
    s = simbolo.strip().upper().replace(":USDC", "")
    if "/" in s:
        a, b = s.split("/", 1)
        return f"{a}{b}"
    return s.replace("/", "")


def _preco_via_ccxt_ticker(simbolo: str, modo: str) -> float:
    """Fallback: pedido ao mercado via ccxt (também rede ao vivo; sem cache no nosso código)."""
    if modo == "FUTURES":
        ex = ccxt.binance(
            {"enableRateLimit": True, "timeout": 30000, "options": {"defaultType": "future"}}
        )
        sym = simbolo
        if ":USDC" not in simbolo and "/USDC" in simbolo:
            sym = f"{simbolo.split('/')[0]}/USDC:USDC"
        t = ex.fetch_ticker(sym)
    else:
        ex = ccxt.binance({"enableRateLimit": True, "timeout": 30000})
        t = ex.fetch_ticker(simbolo)
    last = t.get("last") or t.get("close")
    return float(last) if last is not None else 0.0


def obter_preco_atual(simbolo: str = SYMBOL_TRADE, modo: str = "FUTURES") -> float:
    """
    Preço de mercado ao vivo: cada chamada abre um pedido HTTP novo à API pública Binance
    (REST), sem variáveis globais nem cache — adequado a ser chamado a cada ciclo.

    FUTURES: fapi.binance.com/fapi/v1/ticker/price
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


def _detectar_tendencia_macro_ema200_15m(
    ex: Any,
    *,
    simbolo: str,
    modo: str,
    preco_atual: float,
) -> dict[str, Any]:
    """
    Tendência macro pelo filtro direcional EMA200 (15m).
    Retorna `trend` em {"ALTA","BAIXA","INDEFINIDA"} e `allow_dir` em {"LONG","SHORT",None}.
    """
    px = float(preco_atual)
    if px <= 0:
        return {"trend": "INDEFINIDA", "allow_dir": None, "ema200": None}
    try:
        sym = simbolo
        if modo == "FUTURES":
            sym = executor_futures._resolver_simbolo_perp(ex, simbolo)
        ohlcv = ex.fetch_ohlcv(sym, timeframe="15m", limit=260)
        if not ohlcv or len(ohlcv) < 200:
            raise RuntimeError(f"OHLCV insuficiente para EMA200 15m: {len(ohlcv) if ohlcv else 0}")
        closes = [float(c[4]) for c in ohlcv if c and len(c) > 4]
        if len(closes) < 200:
            raise RuntimeError(f"Fechos insuficientes para EMA200 15m: {len(closes)}")
        alpha = 2.0 / (200.0 + 1.0)
        ema = closes[0]
        for c in closes[1:]:
            ema = (c * alpha) + (ema * (1.0 - alpha))
        distancia = abs(px - ema) / ema
        if distancia < float(EMA_BUFFER_PCT):
            return {
                "trend": "INDEFINIDA",
                "allow_dir": None,
                "ema200": float(ema),
                "distance_pct": float(distancia),
            }
        if px > ema:
            return {
                "trend": "ALTA",
                "allow_dir": "LONG",
                "ema200": float(ema),
                "distance_pct": float(distancia),
            }
        if px < ema:
            return {
                "trend": "BAIXA",
                "allow_dir": "SHORT",
                "ema200": float(ema),
                "distance_pct": float(distancia),
            }
        return {"trend": "INDEFINIDA", "allow_dir": None, "ema200": float(ema), "distance_pct": 0.0}
    except Exception as e_tm:  # noqa: BLE001
        print(f"⚠️ [SINAL] Filtro EMA200 (15m) indisponível: {e_tm}")
        return {"trend": "INDEFINIDA", "allow_dir": None, "ema200": None, "distance_pct": None}


def _gravar_performance_fecho_trade(
    *,
    preco_entrada: float,
    preco_ref_ciclo: float,
    direcao: str,
    ordem: dict[str, Any] | None,
    exit_type: str,
) -> None:
    """Supabase `final_roi` + `exit_type` na última linha `trades` (antes de voltar à busca)."""
    px_exit = executor_futures.preco_medio_execucao_ordem(ordem, float(preco_ref_ciclo))
    executor_futures.registrar_trade_performance_fecho(
        SYMBOL_TRADE,
        preco_entrada=float(preco_entrada),
        preco_saida=float(px_exit),
        direcao=str(direcao),
        exit_type=str(exit_type),
    )


def _features_dataset_intraday(
    *,
    best_bid: float | None,
    best_ask: float | None,
    vol_bids: float | None,
    vol_asks: float | None,
) -> tuple[float | None, float | None]:
    spread_atual: float | None = None
    book_imbalance: float | None = None
    try:
        bb = float(best_bid) if best_bid is not None else None
        ba = float(best_ask) if best_ask is not None else None
        if bb is not None and ba is not None and bb > 0 and ba > 0 and ba >= bb:
            spread_atual = float(ba - bb)
    except Exception:  # noqa: BLE001
        spread_atual = None
    try:
        vb = float(vol_bids) if vol_bids is not None else None
        va = float(vol_asks) if vol_asks is not None else None
        den = (vb + va) if (vb is not None and va is not None) else None
        if den is not None and den > 0:
            book_imbalance = float((vb - va) / den)
    except Exception:  # noqa: BLE001
        book_imbalance = None
    return spread_atual, book_imbalance


def _atr_14_15m(ex: Any, *, simbolo: str, modo: str) -> float | None:
    """
    ATR(14) sobre velas de 15m. Em falha/dados insuficientes devolve None.
    """
    try:
        sym = simbolo
        if modo == "FUTURES":
            sym = executor_futures._resolver_simbolo_perp(ex, simbolo)
        ohlcv = ex.fetch_ohlcv(sym, timeframe="15m", limit=80)
        if not ohlcv or len(ohlcv) < 16:
            return None
        highs = [float(x[2]) for x in ohlcv if x and len(x) > 4]
        lows = [float(x[3]) for x in ohlcv if x and len(x) > 4]
        closes = [float(x[4]) for x in ohlcv if x and len(x) > 4]
        if len(highs) < 16 or len(lows) < 16 or len(closes) < 16:
            return None
        trs: list[float] = []
        for i in range(1, len(closes)):
            h = highs[i]
            l = lows[i]
            pc = closes[i - 1]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(float(tr))
        if len(trs) < 14:
            return None
        atr = sum(trs[:14]) / 14.0
        for tr in trs[14:]:
            atr = ((atr * 13.0) + float(tr)) / 14.0
        return float(atr)
    except Exception:  # noqa: BLE001
        return None


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
            lado_snap = str(snap.get("direcao_posicao") or "").upper()
            if lado_snap in ("LONG", "SHORT") and direcao_posicao != lado_snap:
                print(
                    f"🔄 [Estado] Lado do log Supabase ({direcao_posicao}) ≠ carteira ({lado_snap}) — "
                    "a usar direção da API (contracts/positionAmt)."
                )
                direcao_posicao = lado_snap
            print(
                f"[Estado] Sincronizado: carteira com posição ({direcao_posicao}) — "
                f"preco ref. entrada = {preco_compra:.4f} USDC"
            )
            _sync_entry_price_supabase()
        else:
            print(
                "[Estado] Aviso: há posição, mas não há log de abertura (COMPRA_LONG / ABRE_SHORT) "
                "no Supabase. Ajuste manual ou opere até zerar; TP/SL podem falhar."
            )

    if not snap["posicao_aberta"] and posicao_aberta:
        pe = float(preco_compra)
        dr = str(direcao_posicao)
        exit_tp = "TRAILING_STOP" if _partial_tp_locked else "MANUAL"
        try:
            modo_p = "FUTURES" if ex_mod is executor_futures else "SPOT"
            px = float(obter_preco_referencia(SYMBOL_TRADE, modo_p))
            executor_futures.registrar_trade_performance_fecho(
                SYMBOL_TRADE,
                preco_entrada=pe,
                preco_saida=px,
                direcao=dr,
                exit_type=exit_tp,
            )
        except Exception as e_pf:  # noqa: BLE001
            print(f"⚠️ [Estado] Supabase fecho (carteira flat, RAM com posição): {e_pf}")
        try:
            print("🧹 [ORDENS] Carteira flat detectada. Limpando ordens abertas do símbolo...", flush=True)
            executor_futures.cancelar_todas_ordens_futures_nativo(SYMBOL_TRADE, ex)
            executor_futures.cancelar_todas_ordens_abertas(SYMBOL_TRADE, ex)
        except Exception as e_orf:  # noqa: BLE001
            print(f"⚠️ [ORDENS] Limpeza pós-fecho (orphan orders) falhou: {e_orf}")
        print("[Estado] Carteira sem posição — resetando posicao_aberta / preco_compra.")
        posicao_aberta = False
        preco_compra = 0.0
        direcao_posicao = "LONG"
        _reset_vigia_eth_usdc_guards()
        _sync_entry_price_supabase()


def _reconciliar_posicao_futures_binance_supabase(ex: Any, modo: str) -> None:
    """
    Reconciliação Binance ↔ Supabase ↔ RAM (FUTURES):
    - Se há posição na carteira mas não há log de abertura → log de emergência + RAM + brackets.
    - Se há posição e log mas RAM diz flat → resincroniza RAM a partir do log + brackets.
    """
    global posicao_aberta, preco_compra, direcao_posicao
    global _trailing_callback_cfg, _trailing_activation_mult_cfg

    if modo != "FUTURES":
        return

    snap = executor_futures.consultar_posicao_futures(SYMBOL_TRADE, ex)
    if not snap.get("posicao_aberta"):
        return

    entrada_log, lado_log = logger.obter_preco_entrada_ultima_posicao(SYMBOL_TRADE)
    tem_log = entrada_log is not None and float(entrada_log) > 0

    ep_raw = snap.get("entry_price")
    if ep_raw is not None and float(ep_raw) > 0:
        ep = float(ep_raw)
    else:
        ep = float(obter_preco_referencia(SYMBOL_TRADE, modo))

    qty = abs(float(snap.get("contratos") or 0.0))
    lado_bn = str(snap.get("direcao_posicao") or "LONG").upper()
    if lado_bn not in ("LONG", "SHORT"):
        lado_bn = "LONG"

    if tem_log and posicao_aberta:
        if lado_bn != str(direcao_posicao).upper():
            print(
                f"🔄 [RECON] Direção RAM ({direcao_posicao}) ≠ Binance ({lado_bn}) — "
                "a corrigir a partir de contracts/positionAmt."
            )
            direcao_posicao = lado_bn
        return

    if tem_log and not posicao_aberta:
        posicao_aberta = True
        preco_compra = float(entrada_log)
        # Binance (contracts / positionAmt) é fonte de verdade para o lado; o log pode estar desatualizado.
        direcao_posicao = lado_bn if lado_bn in ("LONG", "SHORT") else (
            lado_log if lado_log in ("LONG", "SHORT") else "LONG"
        )
        if lado_log in ("LONG", "SHORT") and direcao_posicao != lado_log:
            print(
                f"🔄 [RECON] Log Supabase diz {lado_log}, mas carteira é {direcao_posicao} — "
                "a usar lado da Binance."
            )
        print(
            f"🔄 [RECON] Binance com posição + log Supabase: RAM atualizada "
            f"({direcao_posicao} @ {preco_compra:.4f}). A assegurar brackets na bolsa..."
        )
        try:
            executor_futures.assegurar_brackets_apos_reconciliacao(
                SYMBOL_TRADE,
                ex,
                direcao_posicao,
                qty,
                preco_compra,
                trailing_callback_rate=float(_trailing_callback_cfg),
                trailing_activation_multiplier=float(_trailing_activation_mult_cfg),
            )
        except Exception as e_br:  # noqa: BLE001
            print(f"⚠️ [RECON] Falha ao (re)criar brackets após resync RAM: {e_br}")
        return

    if not tem_log:
        acao = "RECON_EMERGENCY_LONG" if lado_bn == "LONG" else "RECON_EMERGENCY_SHORT"
        just = (
            "Reconciliação: posição aberta na Binance Futures sem log de abertura ativo no "
            f"Supabase. entryPrice={ep:.6f}, contracts={qty}, side={lado_bn} (API carteira)."
        )
        print(
            f"🚨 [RECON] Log de emergência + RAM ({lado_bn} @ {ep:.4f}, qty={qty}) — "
            "sem COMPRA_LONG/ABRE_SHORT_* no histórico."
        )
        logger.registrar_log_trade(
            par_moeda=SYMBOL_TRADE,
            preco=ep,
            prob_ml=-1.0,
            sentimento="—",
            acao=acao,
            justificativa=just,
            lado_ordem=lado_bn,
            contexto_raw=None,
            justificativa_ia="RECONCILIACAO_BINANCE_SEM_LOG",
        )
        posicao_aberta = True
        preco_compra = ep
        direcao_posicao = lado_bn
        _sync_entry_price_supabase()
        try:
            executor_futures.assegurar_brackets_apos_reconciliacao(
                SYMBOL_TRADE,
                ex,
                direcao_posicao,
                qty,
                preco_compra,
                trailing_callback_rate=float(_trailing_callback_cfg),
                trailing_activation_multiplier=float(_trailing_activation_mult_cfg),
            )
        except Exception as e_br:  # noqa: BLE001
            print(f"⚠️ [RECON] Falha ao criar brackets após log de emergência: {e_br}")


def _gerenciar_saida_modo_vigia(
    preco: float,
    ex: Any,
    ex_mod: Any,
    modo_label: str,
) -> None:
    """
    [Modo Vigia] Com posição aberta: TP/SL sobre preco_compra (LONG: alta=lucro; SHORT: queda=lucro).
    """
    global posicao_aberta, preco_compra, direcao_posicao, _aperto_seguranca_ativo
    global _rsi_tighten_ativo, _short_stall_count_15m, _short_last_candle_ts_15m, _short_last_low_15m
    global _god_mode_auto_ativo, _trailing_callback_cfg
    global _spread_trailing_pause_until, _partial_tp_pos_key, _partial_tp_locked
    global _vigia_gatilhos_confirm_key, ultima_revisao_tese_ts

    if float(preco) <= 0:
        print("⚠️ [GOD MODE] Tick inválido (0.0000). Mantendo último stop seguro sem alterações.")
        return

    roi = 0.0
    if direcao_posicao == "SHORT":
        roi = (preco_compra - preco) / preco_compra
    else:
        roi = (preco - preco_compra) / preco_compra

    if preco_compra > 0:
        _pk = (round(float(preco_compra), 6), str(direcao_posicao))
        if _partial_tp_pos_key != _pk:
            _partial_tp_pos_key = _pk
            _partial_tp_locked = False

    if preco_compra > 0:
        ck_v = (round(float(preco_compra), 6), str(direcao_posicao).upper())
        if _vigia_gatilhos_confirm_key != ck_v:
            _vigia_gatilhos_confirm_key = ck_v
            print(
                f"[VIGIA] Direção Confirmada: {direcao_posicao} | "
                "Verificando integridade dos gatilhos..."
            )
            # Nova posição/chave de vigia: reinicia relógio da revisão de tese.
            ultima_revisao_tese_ts = time.time()

    _maybe_emit_vigia_heartbeat_supabase(
        preco=float(preco),
        roi_frac=float(roi),
        lado=str(direcao_posicao),
        trailing_pct=float(_trailing_callback_cfg),
        par_moeda=SYMBOL_TRADE,
        contexto_raw=_ultimo_contexto_raw_supabase,
        rsi_14=_ultimo_rsi_14,
        adx_14=_ultimo_adx_14,
    )
    _sync_entry_price_supabase()

    if "FUTURES" in modo_label and float(preco_compra) > 0:
        try:
            executor_futures.check_and_verify_protective_orders(
                SYMBOL_TRADE,
                ex,
                str(direcao_posicao),
                float(preco_compra),
                trailing_callback_rate=float(_trailing_callback_cfg),
                trailing_activation_multiplier=float(_trailing_activation_mult_cfg),
                sl_break_even=bool(_partial_tp_locked),
            )
        except Exception as e_og:  # noqa: BLE001
            print(f"⚠️ [ORDER-GUARD] {e_og}")

    if "FUTURES" in modo_label and float(preco_compra) > 0:
        try:
            if executor_futures.verificar_free_runner_breakeven_posicao(
                SYMBOL_TRADE,
                ex,
                str(direcao_posicao),
                float(preco_compra),
                trailing_callback_rate=float(_trailing_callback_cfg),
                trailing_activation_multiplier=float(_trailing_activation_mult_cfg),
            ):
                _partial_tp_locked = True
        except Exception as e_fr:  # noqa: BLE001
            print(f"⚠️ [FREE RUNNER] {e_fr}")

    if _vigia_eth_usdc_futures(modo_label):
        bid_s = float(_best_bid_ws or 0.0)
        ask_s = float(_best_ask_ws or 0.0)
        if bid_s <= 0 or ask_s <= 0:
            try:
                sym_cc = executor_futures._resolver_simbolo_perp(ex, SYMBOL_TRADE)
                t_ob = ex.fetch_ticker(sym_cc)
                bid_s = float(t_ob.get("bid") or 0.0)
                ask_s = float(t_ob.get("ask") or 0.0)
            except Exception:
                pass
        if bid_s > 0 and ask_s > 0:
            mid = (bid_s + ask_s) / 2.0
            sp = ask_s - bid_s
            if mid > 0 and (sp / mid) > SPREAD_GUARD_MAX_FRAC:
                was_paused = time.time() < _spread_trailing_pause_until
                _spread_trailing_pause_until = time.time() + SPREAD_GUARD_PAUSE_S
                if not was_paused:
                    print(
                        "⏸️ [SPREAD-GUARD] Spread >0.1% do mid — atualizações de trailing "
                        f"na bolsa pausadas {SPREAD_GUARD_PAUSE_S:.0f}s (protecção USDC)."
                    )

    if (
        _vigia_eth_usdc_futures(modo_label)
        and not _partial_tp_locked
        and not executor_futures.free_runner_tracking_ativo(SYMBOL_TRADE, ex)
        and roi >= PARTIAL_TP_ROI_FRAC
    ):
        try:
            out_h = executor_futures.executar_saida_hibrida_roi_break_even_trailing(
                SYMBOL_TRADE,
                ex,
                str(direcao_posicao),
                float(preco_compra),
                close_frac=float(PARTIAL_TP_CLOSE_FRAC),
                trailing_callback_rate=float(TRAILING_CALLBACK_APOS_PARTIAL_TP),
                trailing_activation_multiplier=float(_trailing_activation_mult_cfg),
            )
            print(
                f"💰 [HYBRID-EXIT] {PARTIAL_TP_ROI_FRAC*100}% realizado. Protegendo o resto no Break-Even com Trailing {TRAILING_CALLBACK_APOS_PARTIAL_TP}%."
            )
            ord_p = out_h.get("ordem_parcial") or {}
            qty_r = float(out_h.get("qty_remaining") or 0.0)
            trailing_ap = float(out_h.get("trailing_callback_rate") or TRAILING_CALLBACK_APOS_PARTIAL_TP)
            exec_mode = "MARKET"
            logger.registrar_log_trade(
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=0.0,
                sentimento="—",
                acao="PARTIAL_TP_50",
                justificativa=(
                    f"[HYBRID-EXIT] ROI {roi*100:.3f}% ≥ {PARTIAL_TP_ROI_FRAC*100:.2f}%; "
                    f"fecho {PARTIAL_TP_CLOSE_FRAC*100:.0f}% MARKET; id={ord_p.get('id')}; "
                    f"qty_left≈{qty_r:g}."
                ),
                lado_ordem=direcao_posicao,
                contexto_raw=json.dumps(
                    {
                        "auric_partial_tp": {
                            "roi_frac": roi,
                            "close_frac": PARTIAL_TP_CLOSE_FRAC,
                            "qty_left": qty_r,
                            "execution": exec_mode.lower(),
                            "hybrid_exit": True,
                        }
                    }
                ),
            )
            if qty_r > 0:
                logger.atualizar_qty_left_ultimo_trade(SYMBOL_TRADE, qty_r)
                try:
                    logger.registrar_log_trade(
                        par_moeda=SYMBOL_TRADE,
                        preco=preco,
                        prob_ml=0.0,
                        sentimento="—",
                        acao="HYBRID_BE_TRAILING",
                        justificativa=(
                            f"[HYBRID-EXIT] SL break-even @ entrada {preco_compra:.4f} + "
                            f"TRAILING_STOP_MARKET callbackRate={trailing_ap:.2f}% "
                            f"(qty restante ≈ {qty_r:g})."
                        ),
                        lado_ordem=direcao_posicao,
                        contexto_raw=json.dumps(
                            {
                                "auric_hybrid_brackets": {
                                    "sl_break_even": True,
                                    "entry_price": float(preco_compra),
                                    "qty_remaining": float(qty_r),
                                    "trailing_callback_pct": float(trailing_ap),
                                    "activation_mult": float(_trailing_activation_mult_cfg),
                                }
                            }
                        ),
                    )
                except Exception as e_be:  # noqa: BLE001
                    print(f"⚠️ [HYBRID-EXIT] Falha ao logar HYBRID_BE_TRAILING: {e_be}")
            _partial_tp_locked = True
            _trailing_callback_cfg = trailing_ap
            _god_mode_auto_ativo = True
            _upsert_god_mode_trailing_sync(trailing_ap)
            print(
                f"⚡️ [GOD MODE DE SAÍDA] Trailing {trailing_ap:.3f}% + SL break-even @ entrada "
                f"{preco_compra:.4f} (qty restante ≈ {qty_r:g})."
            )
            return
        except Exception as e_ptp:  # noqa: BLE001
            print(f"⚠️ [HYBRID-EXIT] Falha no fecho parcial ou brackets: {e_ptp}")

    trailing_ativo = float(_trailing_callback_cfg)
    rsi_15m_now: float | None = None
    if roi >= ROI_APERTO_SEGURANCA_ATIVACAO:
        trailing_ativo = min(trailing_ativo, TRAILING_CALLBACK_APERTO_SEGURANCA)
        if not _aperto_seguranca_ativo:
            _aperto_seguranca_ativo = True
            print(
                "🔒 [MODO VIGIA] Aperto de Segurança ATIVADO: "
                f"ROI={roi*100:.2f}% (>= {ROI_APERTO_SEGURANCA_ATIVACAO*100:.1f}%) → "
                f"trailing_callback_rate ativo={trailing_ativo:.3f}%."
            )
            if "FUTURES" in modo_label:
                try:
                    snap = executor_futures.consultar_posicao_futures(SYMBOL_TRADE, ex)
                    qty = abs(float(snap.get("contratos") or 0.0))
                    if qty > 0:
                        if _vigia_trailing_updates_allowed():
                            executor_futures.assegurar_brackets_apos_reconciliacao(
                                SYMBOL_TRADE,
                                ex,
                                direcao_posicao,
                                qty,
                                preco_compra,
                                trailing_callback_rate=trailing_ativo,
                                trailing_activation_multiplier=float(_trailing_activation_mult_cfg),
                                source_tag="APERTO_SEGURANCA",
                            )
                            print(
                                "🔒 [MODO VIGIA] Brackets Futures atualizados com "
                                f"callbackRate={trailing_ativo:.3f}% (anti-devolução de lucro)."
                            )
                except Exception as e_ap:  # noqa: BLE001
                    print(f"⚠️ [MODO VIGIA] Falha ao aplicar Aperto de Segurança: {e_ap}")
    else:
        if _aperto_seguranca_ativo:
            print(
                "ℹ️ [MODO VIGIA] Aperto de Segurança desativado "
                f"(ROI={roi*100:.2f}% < {ROI_APERTO_SEGURANCA_ATIVACAO*100:.1f}%)."
            )
        _aperto_seguranca_ativo = False

    if "FUTURES" in modo_label:
        try:
            sx_gm = executor_futures.obter_sinais_exaustao_short_15m(SYMBOL_TRADE, ex)
            if sx_gm.get("ok"):
                rv = sx_gm.get("rsi_14_15m")
                if rv is not None:
                    rsi_15m_now = float(rv)
        except Exception:
            rsi_15m_now = None

    if (
        "FUTURES" in modo_label
        and (
            roi >= ROI_GOD_MODE_ATIVACAO
            or (rsi_15m_now is not None and rsi_15m_now >= RSI_GOD_MODE_ATIVACAO)
        )
    ):
        try:
            simbolos_recon = [SYMBOL_TRADE]
            if "ETH/USDC:USDC" not in simbolos_recon:
                simbolos_recon.append("ETH/USDC:USDC")
            if "ETH/USDC" not in simbolos_recon:
                simbolos_recon.append("ETH/USDC")

            snap_bn = {"posicao_aberta": False, "contratos": 0.0, "entry_price": None}
            for sym_recon in simbolos_recon:
                try:
                    snap_try = executor_futures.consultar_posicao_futures(sym_recon, ex)
                except Exception:
                    continue
                qty_try = abs(float(snap_try.get("contratos") or 0.0))
                if bool(snap_try.get("posicao_aberta")) and qty_try > 0:
                    snap_bn = snap_try
                    break

            entrada_sb, _ = logger.obter_preco_entrada_ultima_posicao(SYMBOL_TRADE)
            if entrada_sb is None or float(entrada_sb) <= 0:
                ep_bn = float(snap_bn.get("entry_price") or 0.0)
                if ep_bn > 0:
                    entrada_sb = ep_bn
            recon_ok = bool(
                snap_bn.get("posicao_aberta")
                and float(snap_bn.get("contratos") or 0.0) != 0.0
                and entrada_sb is not None
                and float(entrada_sb) > 0
            )
            if recon_ok and (not _god_mode_auto_ativo or trailing_ativo > TRAILING_CALLBACK_GOD_MODE):
                trailing_ativo = min(trailing_ativo, TRAILING_CALLBACK_GOD_MODE)
                _trailing_callback_cfg = trailing_ativo
                _upsert_god_mode_trailing_sync(trailing_ativo)
                _god_mode_auto_ativo = True
                print(f"⚡️ [GOD MODE ACTIVATED] - Apertando trailing para {trailing_ativo:.3f}%.")
                qty = abs(float(snap_bn.get("contratos") or 0.0))
                if qty > 0:
                    executor_futures.assegurar_brackets_apos_reconciliacao(
                        SYMBOL_TRADE,
                        ex,
                        direcao_posicao,
                        qty,
                        preco_compra,
                        trailing_callback_rate=trailing_ativo,
                        trailing_activation_multiplier=float(_trailing_activation_mult_cfg),
                        source_tag="GOD_MODE_AUTO",
                    )
            elif not recon_ok:
                bn_dbg = {
                    "posicao_aberta": bool(snap_bn.get("posicao_aberta")),
                    "contratos": float(snap_bn.get("contratos") or 0.0),
                    "entry_price": float(snap_bn.get("entry_price") or 0.0),
                }
                sb_dbg = float(entrada_sb or 0.0)
                print(f"[DEBUG-RECON] Binance Pos: {bn_dbg} | Supabase Pos: {sb_dbg}")
                print(
                    "⚠️ [GOD MODE] Recon Check falhou: posição não confirmada simultaneamente "
                    "na Binance e no Supabase. Sem alteração de trailing."
                )
        except Exception as e_gm:
            print(f"⚠️ [GOD MODE] Falha na ativação automática: {e_gm}")

    if direcao_posicao == "SHORT" and "FUTURES" in modo_label:
        try:
            sx = executor_futures.obter_sinais_exaustao_short_15m(SYMBOL_TRADE, ex)
            if sx.get("ok"):
                candle_ts = int(sx.get("candle_ts_15m"))
                low_now = float(sx.get("low_15m"))
                if _short_last_candle_ts_15m != candle_ts:
                    if _short_last_low_15m is None or low_now < _short_last_low_15m:
                        _short_stall_count_15m = 0
                    else:
                        _short_stall_count_15m += 1
                    _short_last_low_15m = low_now
                    _short_last_candle_ts_15m = candle_ts
                rsi_now = sx.get("rsi_14_15m")
                rsi_prev = sx.get("rsi_14_15m_prev")
                rsi_repique = bool(sx.get("rsi_repique_fundo"))
                if rsi_repique:
                    if _partial_tp_locked and _vigia_eth_usdc_futures(modo_label):
                        print(
                            "ℹ️ [RSI TIGHTEN] Ignorado — posição com TP parcial (trailing mín. "
                            f"{TRAILING_CALLBACK_APOS_PARTIAL_TP:.1f}%)."
                        )
                    else:
                        trailing_ativo = min(trailing_ativo, TRAILING_CALLBACK_RSI_TIGHTEN)
                        rsi_tighten_primeiro_ciclo = not _rsi_tighten_ativo
                        if not _rsi_tighten_ativo:
                            _rsi_tighten_ativo = True
                            print(
                                "⚡ [RSI TIGHTEN] RSI subindo no fundo. Apertando Trailing para 0.3%."
                            )
                        try:
                            snap = executor_futures.consultar_posicao_futures(SYMBOL_TRADE, ex)
                            qty = abs(float(snap.get("contratos") or 0.0))
                            if (
                                rsi_tighten_primeiro_ciclo
                                and qty > 0
                                and _vigia_trailing_updates_allowed()
                            ):
                                executor_futures.assegurar_brackets_apos_reconciliacao(
                                    SYMBOL_TRADE,
                                    ex,
                                    "SHORT",
                                    qty,
                                    preco_compra,
                                    trailing_callback_rate=trailing_ativo,
                                    trailing_activation_multiplier=float(_trailing_activation_mult_cfg),
                                    source_tag="RSI_TIGHTEN",
                                )
                        except Exception as e_tight:  # noqa: BLE001
                            print(f"⚠️ [RSI TIGHTEN] Falha ao aplicar trailing apertado: {e_tight}")
                else:
                    _rsi_tighten_ativo = False

                print(
                    "    [VIGIA SHORT 15m] "
                    f"stall_count={_short_stall_count_15m}/{STALL_EXIT_VELAS_15M} | "
                    f"low15m={low_now:.4f} | RSI14={rsi_now if rsi_now is not None else 'N/A'} "
                    f"(prev={rsi_prev if rsi_prev is not None else 'N/A'})"
                )
        except Exception as e_sx:  # noqa: BLE001
            print(f"⚠️ [VIGIA SHORT 15m] Falha ao ler sinais de exaustão: {e_sx}")

    print(
        f"    [VIGIA] ROI atual={roi*100:.2f}% | trailing_callback_rate ativo={trailing_ativo:.3f}%"
    )

    # Revisão de tese ativa: a cada 5 minutos com posição aberta.
    if posicao_aberta and preco_compra > 0:
        now_ts = time.time()
        if ultima_revisao_tese_ts <= 0:
            ultima_revisao_tese_ts = now_ts
        elif (now_ts - ultima_revisao_tese_ts) >= REVISAO_TESE_INTERVAL_S:
            print(
                "⏳ [REVISÃO DE TESE] 5 min passados. Analisando Livro de Ordens e Funding...",
                flush=True,
            )
            ultima_revisao_tese_ts = now_ts
            try:
                fr_now: float | None = None
                bid_vol_total: float | None = None
                ask_vol_total: float | None = None
                imbalance_pct: float | None = None
                pnl_unreal: float | None = None
                rsi_rev: float | None = None
                vwap_dist_pct: float | None = None
                ema9_slope_pct: float | None = None
                nota_manual_ctx: str | None = None
                noticias_rss: str = "Sem notícias de impacto no momento"
                if "FUTURES" in modo_label:
                    try:
                        fr_now = executor_futures.obter_funding_rate(SYMBOL_TRADE, ex)
                    except Exception:
                        fr_now = None
                    sym_ob = executor_futures._resolver_simbolo_perp(ex, SYMBOL_TRADE)
                else:
                    sym_ob = SYMBOL_TRADE
                try:
                    ob = ex.fetch_order_book(sym_ob, limit=20)
                    bids = ob.get("bids") if isinstance(ob, dict) else []
                    asks = ob.get("asks") if isinstance(ob, dict) else []
                    bid_vol_total = sum(float(lv[1]) for lv in (bids or []) if len(lv) >= 2)
                    ask_vol_total = sum(float(lv[1]) for lv in (asks or []) if len(lv) >= 2)
                    den = bid_vol_total + ask_vol_total
                    if den > 0:
                        imbalance_pct = ((bid_vol_total - ask_vol_total) / den) * 100.0
                except Exception as e_ob:
                    print(f"⚠️ [REVISÃO DE TESE] Falha ao capturar Order Book: {e_ob}")
                try:
                    qty_abs = 0.0
                    if "FUTURES" in modo_label:
                        snap_pos = executor_futures.consultar_posicao_futures(SYMBOL_TRADE, ex)
                        qty_abs = abs(float(snap_pos.get("contratos") or 0.0))
                    if qty_abs > 0:
                        if str(direcao_posicao).upper() == "SHORT":
                            pnl_unreal = (float(preco_compra) - float(preco)) * qty_abs
                        else:
                            pnl_unreal = (float(preco) - float(preco_compra)) * qty_abs
                except Exception:
                    pnl_unreal = None
                try:
                    snap_tese = ml_model.obter_snapshot_indicadores_eth(SYMBOL_TRADE, prob_ml=None)
                    rv = snap_tese.get("rsi_14")
                    rsi_rev = float(rv) if rv is not None else None
                    vwap_v = snap_tese.get("vwap_d")
                    close_v = snap_tese.get("preco_close")
                    if vwap_v is not None and close_v is not None:
                        vwap_f = float(vwap_v)
                        close_f = float(close_v)
                        if abs(vwap_f) > 1e-12:
                            vwap_dist_pct = ((close_f - vwap_f) / vwap_f) * 100.0
                except Exception as e_ta:
                    print(f"⚠️ [REVISÃO DE TESE] TA snapshot indisponível: {e_ta}")
                try:
                    ema9_slope_pct = _ema9_slope_pct_1m(ex, sym_ob)
                except Exception as e_ema:
                    print(f"⚠️ [REVISÃO DE TESE] EMA9 slope indisponível: {e_ema}")
                try:
                    nota_manual_ctx = obter_valor_market_observation_supabase()
                except Exception:
                    nota_manual_ctx = None
                try:
                    noticias_rss = fetch_crypto_rss_news("ETH")
                except Exception:
                    noticias_rss = "Sem notícias de impacto no momento"
                print(
                    "📈 [REVISÃO TA] "
                    f"RSI={rsi_rev if rsi_rev is not None else 'N/A'} | "
                    f"dist_vwap={f'{vwap_dist_pct:+.3f}%' if vwap_dist_pct is not None else 'N/A'} | "
                    f"ema9_slope={f'{ema9_slope_pct:+.3f}%' if ema9_slope_pct is not None else 'N/A'}",
                    flush=True,
                )

                decisao, motivo = brain.revisar_tese_posicao_aberta(
                    direcao_posicao=str(direcao_posicao),
                    roi_frac=float(roi),
                    pnl_nao_realizado=pnl_unreal,
                    funding_rate=fr_now,
                    order_book_imbalance_pct=imbalance_pct,
                    bid_volume_total=bid_vol_total,
                    ask_volume_total=ask_vol_total,
                    rsi_14=rsi_rev,
                    distancia_vwap_pct=vwap_dist_pct,
                    inclinacao_ema9_pct=ema9_slope_pct,
                    contexto_sentimento_noticias=nota_manual_ctx,
                    noticias_recentes=noticias_rss,
                    verbose=False,
                )
                print(
                    f"🧠 [DECISÃO CLAUDE] {decisao} posição. Motivo: {motivo}",
                    flush=True,
                )
                if decisao == "FECHAR":
                    try:
                        if "FUTURES" in modo_label:
                            ordem = executor_futures.fechar_posicao_market(SYMBOL_TRADE, ex)
                        else:
                            ordem = ex_mod.executar_venda_spot_total(SYMBOL_TRADE, ex)
                        logger.registrar_log_trade(
                            par_moeda=SYMBOL_TRADE,
                            preco=preco,
                            prob_ml=0.0,
                            sentimento="—",
                            acao="REVISAO_TESE_FECHAR",
                            justificativa=(
                                f"Claude revisão de tese (5m): FECHAR. {motivo} "
                                f"| funding={fr_now} | ob_imbalance_pct={imbalance_pct}"
                            ),
                            lado_ordem=str(direcao_posicao).upper(),
                            contexto_raw=_ultimo_contexto_raw_supabase,
                            justificativa_ia=motivo,
                        )
                        posicao_aberta = False
                        preco_compra = 0.0
                        direcao_posicao = "LONG"
                        ultima_revisao_tese_ts = 0.0
                        _reset_vigia_eth_usdc_guards()
                        print(
                            f"🏁 [REVISÃO DE TESE] Posição encerrada por decisão ativa. id={ordem.get('id')}",
                            flush=True,
                        )
                        return
                    except Exception as e_close:
                        print(f"🚨 [REVISÃO DE TESE] Falha ao fechar posição: {e_close}", flush=True)
                else:
                    logger.registrar_log_trade(
                        par_moeda=SYMBOL_TRADE,
                        preco=preco,
                        prob_ml=0.0,
                        sentimento="—",
                        acao="REVISAO_TESE_MANTER",
                        justificativa=(
                            f"Claude revisão de tese (5m): MANTER. {motivo} "
                            f"| funding={fr_now} | ob_imbalance_pct={imbalance_pct}"
                        ),
                        lado_ordem=str(direcao_posicao).upper(),
                        contexto_raw=_ultimo_contexto_raw_supabase,
                        justificativa_ia=motivo,
                    )
            except Exception as e_rt:  # noqa: BLE001
                print(f"⚠️ [REVISÃO DE TESE] Erro interno na revisão ativa: {e_rt}", flush=True)

    if preco_compra <= 0:
        print("[Modo Vigia] preco_compra inválido — não é possível avaliar TP/SL.")
        return

    if direcao_posicao == "SHORT":
        if _short_stall_count_15m >= STALL_EXIT_VELAS_15M and "FUTURES" in modo_label:
            print("⚠️ [STALL EXIT] Preço preso em suporte por 4 velas. Fechando para proteger lucro.")
            try:
                ordem = executor_futures.fechar_posicao_market(SYMBOL_TRADE, ex)
                logger.registrar_log_trade(
                    par_moeda=SYMBOL_TRADE,
                    preco=preco,
                    prob_ml=0.0,
                    sentimento="—",
                    acao="STALL_EXIT_SHORT",
                    justificativa=(
                        f"SHORT sem nova mínima por {STALL_EXIT_VELAS_15M} velas de 15m; "
                        "saída de emergência por exaustão em suporte."
                    ),
                    lado_ordem="SHORT",
                )
                _gravar_performance_fecho_trade(
                    preco_entrada=float(preco_compra),
                    preco_ref_ciclo=float(preco),
                    direcao="SHORT",
                    ordem=ordem,
                    exit_type="STALL_EXIT",
                )
                posicao_aberta = False
                preco_compra = 0.0
                direcao_posicao = "LONG"
                _short_stall_count_15m = 0
                _short_last_candle_ts_15m = None
                _short_last_low_15m = None
                _rsi_tighten_ativo = False
                _reset_vigia_eth_usdc_guards()
                _sync_entry_price_supabase()
                print(f"  [Estado] posição encerrada por STALL EXIT. id={ordem.get('id')}")
            except Exception as e_stall:  # noqa: BLE001
                print(f"  [ERRO] STALL EXIT SHORT: {e_stall}")
            return

        # SHORT: TP = entrada × (1 − alvo); SL = entrada × (1 + stop) — alinhado a FATOR_*SHORT.
        alvo_tp_frac = 1.0 - float(FATOR_TAKE_PROFIT_SHORT)
        stop_sl_frac = float(FATOR_STOP_LOSS_SHORT) - 1.0
        limite_tp = float(preco_compra) * (1.0 - alvo_tp_frac)
        limite_sl = float(preco_compra) * (1.0 + stop_sl_frac)
        print(
            f"\n  Posição SHORT | ref. entrada: {preco_compra:.4f} USDC | atual: {preco:.4f} ({modo_label})\n"
            f"  TP (lucro se cair ~{alvo_tp_frac*100:.0f}%): <= {limite_tp:.4f}  |  "
            f"SL (perda se subir ~{stop_sl_frac*100:.0f}%): >= {limite_sl:.4f}"
        )
        if preco <= limite_tp:
            print("\n  [Take Profit SHORT] Preço <= entrada × 0,98 — fechando posição...")
            try:
                if "FUTURES" in modo_label:
                    ordem = executor_futures.fechar_posicao_market(SYMBOL_TRADE, ex)
                else:
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
                    contexto_raw=_exit_meta_contexto_json(
                        partial_tp_50=_partial_tp_locked, stop_hit=False
                    ),
                )
                _gravar_performance_fecho_trade(
                    preco_entrada=float(preco_compra),
                    preco_ref_ciclo=float(preco),
                    direcao="SHORT",
                    ordem=ordem,
                    exit_type="TAKE_PROFIT",
                )
                posicao_aberta = False
                preco_compra = 0.0
                direcao_posicao = "LONG"
                _reset_vigia_eth_usdc_guards()
                _sync_entry_price_supabase()
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
            print("\n  [Stop Loss SHORT] Preço >= entrada × 1,006 — fechando posição...")
            try:
                if "FUTURES" in modo_label:
                    ordem = executor_futures.fechar_posicao_market(SYMBOL_TRADE, ex)
                else:
                    ordem = ex_mod.executar_venda_spot_total(SYMBOL_TRADE, ex)
                just = (
                    f"SL short +0.6%: entrada {preco_compra:.4f}, ref. {preco:.4f}, "
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
                    contexto_raw=_exit_meta_contexto_json(
                        partial_tp_50=_partial_tp_locked, stop_hit=True
                    ),
                )
                _gravar_performance_fecho_trade(
                    preco_entrada=float(preco_compra),
                    preco_ref_ciclo=float(preco),
                    direcao="SHORT",
                    ordem=ordem,
                    exit_type="STOP_LOSS",
                )
                posicao_aberta = False
                preco_compra = 0.0
                direcao_posicao = "LONG"
                _reset_vigia_eth_usdc_guards()
                _sync_entry_price_supabase()
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
        f"\n  Referência entrada LONG: {preco_compra:.4f} USDC | atual: {preco:.4f} USDC ({modo_label})\n"
        f"  Gatilho TP: >= {limite_tp:.4f}  |  Gatilho SL: <= {limite_sl:.4f}"
    )

    if preco >= limite_tp:
        print("\n  [Take Profit] Preço atual >= entrada × 1,02 — executando saída total...")
        try:
            if "FUTURES" in modo_label:
                ordem = executor_futures.fechar_posicao_market(SYMBOL_TRADE, ex)
            else:
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
                contexto_raw=_exit_meta_contexto_json(
                    partial_tp_50=_partial_tp_locked, stop_hit=False
                ),
            )
            _gravar_performance_fecho_trade(
                preco_entrada=float(preco_compra),
                preco_ref_ciclo=float(preco),
                direcao="LONG",
                ordem=ordem,
                exit_type="TAKE_PROFIT",
            )
            posicao_aberta = False
            preco_compra = 0.0
            direcao_posicao = "LONG"
            _reset_vigia_eth_usdc_guards()
            _sync_entry_price_supabase()
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
        print("\n  [Stop Loss] Preço atual <= entrada × 0,994 — executando saída total...")
        try:
            if "FUTURES" in modo_label:
                ordem = executor_futures.fechar_posicao_market(SYMBOL_TRADE, ex)
            else:
                ordem = ex_mod.executar_venda_spot_total(SYMBOL_TRADE, ex)
            just = (
                f"SL -0.6%: entrada {preco_compra:.4f}, saída ref. {preco:.4f}, "
                f"ordem id={ordem.get('id')}."
            )
            logger.registrar_log_trade(
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=0.0,
                sentimento="—",
                acao="VENDA_STOP",
                justificativa=just,
                contexto_raw=_exit_meta_contexto_json(
                    partial_tp_50=_partial_tp_locked, stop_hit=True
                ),
            )
            _gravar_performance_fecho_trade(
                preco_entrada=float(preco_compra),
                preco_ref_ciclo=float(preco),
                direcao="LONG",
                ordem=ordem,
                exit_type="STOP_LOSS",
            )
            posicao_aberta = False
            preco_compra = 0.0
            direcao_posicao = "LONG"
            _reset_vigia_eth_usdc_guards()
            _sync_entry_price_supabase()
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
    """Delega o ciclo à implementação; captura exceções não tratadas (ex.: asyncio.to_thread)."""
    try:
        _rodar_ciclo_impl(modo)
    except Exception as e:  # noqa: BLE001
        print(f"\n🚨 ERRO FATAL NO CICLO: {e}", flush=True)
        traceback.print_exc()


def _rodar_ciclo_impl(modo: str) -> None:
    """Um ciclo: Vigia (saída) OU Buscando Oportunidade (entrada ML + sentimento)."""
    global posicao_aberta, preco_compra, direcao_posicao, _last_modo_detectado, _aperto_seguranca_ativo
    global _rsi_tighten_ativo, _short_stall_count_15m, _short_last_candle_ts_15m, _short_last_low_15m
    global _equity_inicio_dia, _travado_ate_ts
    global _risk_fraction_cfg, _alavancagem_cfg, _trailing_callback_cfg, _trailing_activation_mult_cfg
    global _ultimo_preco_ws, _best_bid_ws, _best_ask_ws
    global USER_MARKET_OBSERVATION
    global _ultimo_rsi_14, _ultimo_adx_14, _ultimo_contexto_raw_supabase
    global _ultimo_news_sentiment_score, _ultimo_minuto_previsao_candle
    global _ultimo_preco_alvo_previsao, _ultima_tendencia_alta_previsao
    global _ultimo_llava_veto, _ultima_etapa_funil, _ultima_razao_abort_funil
    global _ultima_ml_prob_base, _ultima_ml_prob_calibrada

    if not logger.obter_bot_ativo():
        print("Bot em modo Standby via Dashboard")
        return

    atualizar_saldo_supabase()
    # Human sentiment: leitura fresca do Supabase em **cada** ciclo (ex.: a cada ~1 min no gatilho WS)
    # para que alterações via dashboard (Vercel /api/bot/command) entrem no próximo ciclo antes do Brain.
    contexto_humano = obter_valor_market_observation_supabase()
    if contexto_humano:
        print(f"\n[SUPABASE] 🧠 Observação Humana Ativa: {contexto_humano}")

    ex_mod = executor_futures if modo == "FUTURES" else executor_spot
    modo_label = "FUTURES USDC" if modo == "FUTURES" else "SPOT"

    if modo == "FUTURES" and _last_modo_detectado != "FUTURES":
        try:
            ex_cfg = executor_futures.criar_exchange_binance()
            executor_futures.configurar_alavancagem(
                SYMBOL_TRADE, int(round(_alavancagem_cfg)), ex_cfg
            )
        except Exception as e_cfg:  # noqa: BLE001
            print(f"[Maestro] Aviso ao configurar alavancagem/margem: {e_cfg}")
    _last_modo_detectado = modo

    if modo == "FUTURES":
        preco = float(_ultimo_preco_ws or 0.0)
        if preco <= 0:
            bid_bt = float(_best_bid_ws or 0.0)
            ask_bt = float(_best_ask_ws or 0.0)
            # Mesma referência que o terminal ("Preço Atual" = bookTicker bid); depois ask / mid.
            if bid_bt > 0:
                preco = bid_bt
            elif ask_bt > 0:
                preco = ask_bt
            if preco > 0:
                _ultimo_preco_ws = preco
                print(
                    f"ℹ️ [WS PRICE] Kline sem close válido; a usar bookTicker → {preco:.4f} USDC "
                    "(referência para ciclo / notional).",
                    flush=True,
                )
        if preco <= 0:
            print(
                "⚠️ [WS PRICE] Sem preço kline nem bookTicker válido. Ciclo ignorado para preservar stops.",
                flush=True,
            )
            return
    else:
        preco = obter_preco_referencia(SYMBOL_TRADE, modo)
    ex = ex_mod.criar_exchange_binance()
    try:
        atr_14 = _atr_14_15m(ex, simbolo=SYMBOL_TRADE, modo=modo)
        funding_rate = (
            executor_futures.obter_funding_rate(SYMBOL_TRADE, ex)
            if modo == "FUTURES"
            else None
        )
        long_short_ratio = (
            executor_futures.obter_long_short_ratio_global(SYMBOL_TRADE, ex)
            if modo == "FUTURES"
            else None
        )
        spread_boot, _ = _features_dataset_intraday(
            best_bid=float(_best_bid_ws or 0.0),
            best_ask=float(_best_ask_ws or 0.0),
            vol_bids=None,
            vol_asks=None,
        )
        logger.configurar_features_log_ciclo(
            dist_ema200_pct=None,
            spread_atual=spread_boot,
            book_imbalance=None,
            hora_do_dia=int(datetime.now(timezone.utc).hour),
            atr_14=atr_14,
            funding_rate=funding_rate,
            long_short_ratio=long_short_ratio,
        )
    except Exception:  # noqa: BLE001
        logger.configurar_features_log_ciclo(
            dist_ema200_pct=None,
            spread_atual=None,
            book_imbalance=None,
            hora_do_dia=None,
            atr_14=None,
            funding_rate=None,
            long_short_ratio=None,
        )

    _sincronizar_estado_com_carteira(ex, ex_mod)

    now = time.time()
    if now < _travado_ate_ts:
        rem_h = (_travado_ate_ts - now) / 3600.0
        print(f"🛑 [RISK LOCK] Execução travada por drawdown diário. Retoma em ~{rem_h:.2f}h.")
        return
    if modo == "FUTURES":
        try:
            bal = ex.fetch_balance(params={"type": "future"})
            total = bal.get("total") if isinstance(bal, dict) else {}
            free = bal.get("free") if isinstance(bal, dict) else {}
            usdc_tot = float((total or {}).get("USDC") or 0.0)
            usdc_free = float((free or {}).get("USDC") or 0.0)
            snap_dd = executor_futures.consultar_posicao_futures(SYMBOL_TRADE, ex)
            unreal = 0.0
            try:
                sym = executor_futures._resolver_simbolo_perp(ex, SYMBOL_TRADE)
                for p in ex.fetch_positions([sym]):
                    c = float(p.get("contracts") or 0.0)
                    if abs(c) > 0:
                        unreal = float(p.get("unrealizedPnl") or p.get("info", {}).get("unRealizedProfit") or 0.0)
                        break
            except Exception:
                pass
            equity = usdc_tot + unreal
            if _equity_inicio_dia is None or _equity_inicio_dia <= 0:
                _equity_inicio_dia = max(1e-9, equity)
            dd = (_equity_inicio_dia - equity) / _equity_inicio_dia
            if dd >= 0.25:
                print("🛑 [DRAWDOWN] 25% diário atingido. Acionando travão de mão.")
                try:
                    executor_futures.cancelar_todas_ordens_abertas(SYMBOL_TRADE, ex)
                except Exception:
                    pass
                if snap_dd.get("posicao_aberta"):
                    try:
                        executor_futures.fechar_posicao_emergencia_market(SYMBOL_TRADE, ex)
                    except Exception as e_em:
                        print(f"⚠️ [DRAWDOWN] Falha no fecho emergência: {e_em}")
                _travado_ate_ts = time.time() + 24 * 3600
                logger.registrar_log_trade(
                    par_moeda=SYMBOL_TRADE,
                    preco=preco,
                    prob_ml=0.0,
                    sentimento="CRITICAL",
                    acao="EMERGENCY_DRAWDOWN_LOCK",
                    justificativa="Drawdown diário >=25% (realizado+flutuante). Execução travada por 24h.",
                    is_maker=False,
                )
                return
            print(
                f"🧯 [RISK] Equity diária={equity:.4f} USDC | início={_equity_inicio_dia:.4f} | "
                f"drawdown={dd*100:.2f}% | free={usdc_free:.4f} USDC"
            )
        except Exception as e_dd:
            print(f"⚠️ [RISK] checkDailyDrawdown falhou: {e_dd}")

    if modo == "FUTURES":
        _reconciliar_posicao_futures_binance_supabase(ex, modo)
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
    print(f"[Ciclo] {SYMBOL_TRADE} | modo={modo_label} | preço ref. ≈ {preco:.4f} USDC")
    print(f"        Estado RAM: posicao_aberta={posicao_aberta} | preco_compra={preco_compra:.4f}")
    print("=" * 64)

    if posicao_aberta and modo == "FUTURES":
        try:
            snap_align = executor_futures.consultar_posicao_futures(SYMBOL_TRADE, ex)
            if snap_align.get("posicao_aberta"):
                lado_api = str(snap_align.get("direcao_posicao") or "").upper()
                if lado_api in ("LONG", "SHORT") and lado_api != str(direcao_posicao).upper():
                    print(
                        f"🔄 [VIGIA] Pré-ciclo: alinhar direção RAM {direcao_posicao} → {lado_api} "
                        "(contracts/positionAmt)."
                    )
                    direcao_posicao = lado_api
        except Exception as e_al:  # noqa: BLE001
            print(f"⚠️ [VIGIA] Pré-ciclo: leitura de posição falhou: {e_al}")

    if posicao_aberta:
        print("\n>>> MODO VIGIA <<<")
        print(
            "    Posição aberta: TP/SL — LONG +2% / −0,6% | SHORT (fut.) −2% / +0,6% no preço de entrada."
        )
        _gerenciar_saida_modo_vigia(preco, ex, ex_mod, modo_label)
        return
    _aperto_seguranca_ativo = False
    _rsi_tighten_ativo = False
    _god_mode_auto_ativo = False
    _short_stall_count_15m = 0
    _short_last_candle_ts_15m = None
    _short_last_low_15m = None
    _reset_vigia_eth_usdc_guards()

    # Busca de oportunidade (sem posição em RAM após sync+recon): limpar livro na Binance (margem livre).
    if modo == "FUTURES":
        try:
            executor_futures.cancelar_todas_ordens_abertas(SYMBOL_TRADE, ex)
        except Exception as e_co:  # noqa: BLE001
            print(f"⚠️ [Ciclo] Limpeza cancel_all (busca / sem posição): {e_co}")

    print("\n>>> BUSCANDO OPORTUNIDADE <<<")
    print(
        "    [Filtros — sessão teste / alta frequência] "
        f"VETO_ADX_LATERAL: ADX(14) < {indicators.ADX_LIMIAR_TENDENCIA:.0f} (sem squeeze BB). "
        f"Wake XGBoost zona neutra P(alta) ∈ [{WAKE_FILTER_SHORT_MAX:.2f}, {WAKE_FILTER_LONG_MIN:.2f}] "
        "(dentro → sem Hub/Claude; fora → Hub + Brain)."
    )
    print(
        f"    ML P(alta)≥{WAKE_FILTER_LONG_MIN:.2f} (LONG) ou P(alta)≤{WAKE_FILTER_SHORT_MAX:.2f} (SHORT) "
        "→ Hub → Brain → posicao_recomendada."
    )
    _ultima_etapa_funil = "PASSO_1_GUARDA_COSTAS"
    _ultima_razao_abort_funil = None
    _ultimo_llava_veto = False
    # Passo 1 (Guarda-Costas): notícia macro primeiro; pânico => não procurar LONG neste ciclo.
    try:
        _ultimo_news_sentiment_score = int(
            asyncio.run(intelligence_hub.analisar_sentimento_noticias(SYMBOL_TRADE))
        )
    except Exception as e_news:  # noqa: BLE001
        _ultimo_news_sentiment_score = 0
        print(f"⚠️ [NEWS SENTIMENT] Falha ao analisar RSS: {e_news}")
    print(f"🛡️ [GUARDA-COSTAS] sentimento_noticias={_ultimo_news_sentiment_score:+d}")
    if _ultimo_news_sentiment_score <= -1:
        _ultima_razao_abort_funil = "NEWS_PANIC"
        msg_news_panic = (
            "Pânico/venda forte no RSS da última hora. LONG bloqueado e análise técnica pulada neste ciclo."
        )
        print(f"🚫 [GUARDA-COSTAS] {msg_news_panic}")
        logger.registrar_log_trade(
            par_moeda=SYMBOL_TRADE,
            preco=preco,
            prob_ml=0.0,
            sentimento="BEARISH",
            acao="VETO_NEWS_PANIC",
            justificativa=msg_news_panic,
            contexto_raw=_ultimo_contexto_raw_supabase,
            justificativa_ia=msg_news_panic,
            social_sentiment_score=float(_ultimo_social_sentiment_score),
            whale_flow_score=float(_ultimo_whale_flow_score),
            funnel_stage=_ultima_etapa_funil,
            funnel_abort_reason=_ultima_razao_abort_funil,
            llava_veto=False,
        )
        _emit_funnel_observability(
            "ABORT_PASSO_1",
            {
                "reason": _ultima_razao_abort_funil,
                "news_sentiment_score": _ultimo_news_sentiment_score,
                "symbol": SYMBOL_TRADE,
            },
        )
        return

    # Passo 2 (Estrategista): só em fechamento de candle, prever próximos candles para peso extra.
    _ultima_etapa_funil = "PASSO_2_ESTRATEGISTA"
    minuto_atual_utc = int(datetime.now(timezone.utc).minute)
    if _ultimo_minuto_previsao_candle != minuto_atual_utc:
        _ultimo_minuto_previsao_candle = minuto_atual_utc
        try:
            df_hist = ml_model.fetch_ohlcv_binance(
                symbol=SYMBOL_TRADE,
                timeframe=ml_model.TIMEFRAME,
                total=220,
            )
            prev = asyncio.run(intelligence_hub.prever_proximos_candles(df_hist))
            _ultima_tendencia_alta_previsao = prev.get("tendencia_alta")
            _ultimo_preco_alvo_previsao = prev.get("preco_alvo")
            print(
                "🧭 [ESTRATEGISTA] "
                f"tendencia_alta={_ultima_tendencia_alta_previsao} | "
                f"preco_alvo={_ultimo_preco_alvo_previsao}"
            )
        except Exception as e_prev:  # noqa: BLE001
            _ultima_tendencia_alta_previsao = None
            _ultimo_preco_alvo_previsao = None
            print(f"⚠️ [ESTRATEGISTA] previsão indisponível: {e_prev}")
    hora_do_dia_cycle = int(datetime.now(timezone.utc).hour)
    spread_atual: float | None = None
    book_imbalance: float | None = None
    atr_14_cycle: float | None = None
    funding_rate: float | None = None
    long_short_ratio: float | None = None
    macro = _detectar_tendencia_macro_ema200_15m(
        ex,
        simbolo=SYMBOL_TRADE,
        modo=modo,
        preco_atual=preco,
    )
    macro_trend = str(macro.get("trend") or "INDEFINIDA").upper()
    macro_allow = macro.get("allow_dir")
    ema200_v = macro.get("ema200")
    dist_ema200_pct = _safe_float(macro.get("distance_pct"))
    if macro_trend == "ALTA":
        print("📈 [SINAL] Tendência Macro: ALTA (Preço > EMA200). Procurando apenas LONGs.")
    elif macro_trend == "BAIXA":
        print("📉 [SINAL] Tendência Macro: BAIXA (Preço < EMA200). Procurando apenas SHORTs.")
    else:
        ema_txt = f"{float(ema200_v):.4f}" if ema200_v is not None else "N/A"
        print(
            f"⚖️ [SINAL] Tendência Macro: INDEFINIDA (Preço ~= EMA200={ema_txt}). "
            "Sem bloqueio direcional adicional."
        )

    print("\n--- [AURIC CYCLE START] ---")

    # 1. Sinal Técnico (XGBoost) + snapshot ADX / VWAP / BB (um download OHLCV)
    probabilidade = ml_model.obter_sinal_atual()
    print(f"📊 [ML] Probabilidade de Alta: {probabilidade:.2%}")
    long_prob = max(0.0, min(1.0, float(probabilidade)))
    short_prob = max(0.0, min(1.0, 1.0 - long_prob))
    print(
        f"📊 [ML PROBABILIDADES] Long: {long_prob:.2%} | Short: {short_prob:.2%} "
        f"(limiares: long>{WAKE_FILTER_LONG_MIN:.0%}, short<{WAKE_FILTER_SHORT_MAX:.0%})"
    )
    prob_ml_base = float(probabilidade)
    parede_venda_proxima_confirma_short = False
    parede_venda_preco_ref: float | None = None
    obp: dict[str, Any] | None = None
    if modo == "FUTURES":
        try:
            obp = executor_futures.analisar_pressao_order_book(SYMBOL_TRADE, ex, depth_limit=100)
            if obp.get("muralha_venda"):
                short_prob = max(0.0, min(1.0, 1.0 - prob_ml_base))
                short_prob_aj = min(1.0, short_prob * 1.20)
                probabilidade = max(0.0, min(1.0, 1.0 - short_prob_aj))
                print(
                    "🧱 [ORDER BOOK] Pressão vendedora forte no raio 1%: "
                    f"asks={obp.get('ask_volume_1pct'):.4f} vs bids={obp.get('bid_volume_1pct'):.4f} "
                    f"(ratio={obp.get('ask_bid_ratio'):.2f}x). "
                    f"P(alta) ajustada {prob_ml_base:.2%} → {probabilidade:.2%} "
                    "(peso +20% na probabilidade de SHORT)."
                )
                if obp.get("muralha_proxima"):
                    parede_venda_proxima_confirma_short = True
                    dist = obp.get("dist_muralha_frac")
                    if dist is not None:
                        parede_venda_preco_ref = float(obp.get("preco_atual")) * (1.0 + float(dist))
        except Exception as e_ob:  # noqa: BLE001
            print(f"⚠️ [ORDER BOOK] Falha ao analisar depth Binance: {e_ob}")
    whale_flow = {
        "whale_score": 0.0,
        "signal": "NEUTRAL",
        "possible_bull_trap": False,
        "source": "disabled",
    }
    try:
        whale_flow = intelligence_module.obter_smart_money_flow(ex, SYMBOL_TRADE)
        _ultimo_whale_flow_score = float(
            whale_flow.get("whale_flow_score", whale_flow.get("whale_score") or 0.0)
        )
        _ultimo_whale_flow_signal = str(whale_flow.get("signal") or "NEUTRAL")
        _ultimo_social_sentiment_score = float(whale_flow.get("social_sentiment_score") or 0.0)
        print(
            "🐋 [SMART MONEY] "
            f"score={_ultimo_whale_flow_score:+.2f} | signal={_ultimo_whale_flow_signal} | "
            f"social={_ultimo_social_sentiment_score:+.2f} | "
            f"src={whale_flow.get('source')} | bull_trap={bool(whale_flow.get('possible_bull_trap'))}"
        )
    except Exception as e_whale:  # noqa: BLE001
        _ultimo_whale_flow_score = 0.0
        _ultimo_whale_flow_signal = "NEUTRAL"
        _ultimo_social_sentiment_score = 0.0
        print(f"⚠️ [SMART MONEY] indisponível neste ciclo: {e_whale}")
    finally:
        try:
            # Garantia: dashboard recebe atualização do humor (whale_flow_score) em todo ciclo de inteligência.
            logger.persistir_whale_flow_score(
                float(_ultimo_whale_flow_score),
                social_sentiment_score=float(_ultimo_social_sentiment_score),
            )
            logger.atualizar_ultimo_trade_mood_scores(
                SYMBOL_TRADE,
                whale_flow_score=float(_ultimo_whale_flow_score),
                social_sentiment_score=float(_ultimo_social_sentiment_score),
            )
        except Exception as e_whale_db:  # noqa: BLE001
            print(f"⚠️ [SMART MONEY] persistência wallet_status falhou: {e_whale_db}")
    prob_sem_whale = float(probabilidade)
    probabilidade = ml_model.ajustar_probabilidade_com_whale_flow(
        prob_sem_whale,
        float(_ultimo_whale_flow_score),
    )
    _ultima_ml_prob_base = float(prob_ml_base)
    _ultima_ml_prob_calibrada = float(probabilidade)
    if abs(probabilidade - prob_sem_whale) > 1e-12:
        print(
            "📊 [ML+WHALE FEATURE] "
            f"P(alta) ajustada por whale_flow {prob_sem_whale:.2%} → {probabilidade:.2%} "
            f"(score={_ultimo_whale_flow_score:+.2f})."
        )
    try:
        spread_atual, book_imbalance = _features_dataset_intraday(
            best_bid=float(_best_bid_ws or 0.0),
            best_ask=float(_best_ask_ws or 0.0),
            vol_bids=_safe_float((obp or {}).get("bid_volume_1pct")),
            vol_asks=_safe_float((obp or {}).get("ask_volume_1pct")),
        )
        funding_rate = (
            executor_futures.obter_funding_rate(SYMBOL_TRADE, ex)
            if modo == "FUTURES"
            else None
        )
        long_short_ratio = (
            executor_futures.obter_long_short_ratio_global(SYMBOL_TRADE, ex)
            if modo == "FUTURES"
            else None
        )
        atr_14_cycle = _atr_14_15m(ex, simbolo=SYMBOL_TRADE, modo=modo)
        logger.configurar_features_log_ciclo(
            dist_ema200_pct=dist_ema200_pct,
            spread_atual=spread_atual,
            book_imbalance=book_imbalance,
            hora_do_dia=hora_do_dia_cycle,
            atr_14=atr_14_cycle,
            funding_rate=funding_rate,
            long_short_ratio=long_short_ratio,
            whale_flow_score=float(_ultimo_whale_flow_score),
            social_sentiment_score=float(_ultimo_social_sentiment_score),
        )
    except Exception:  # noqa: BLE001
        logger.configurar_features_log_ciclo(
            dist_ema200_pct=dist_ema200_pct,
            spread_atual=None,
            book_imbalance=None,
            hora_do_dia=hora_do_dia_cycle,
            atr_14=None,
            funding_rate=None,
            long_short_ratio=None,
            whale_flow_score=float(_ultimo_whale_flow_score),
            social_sentiment_score=float(_ultimo_social_sentiment_score),
        )

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
            "bb_squeeze_tight_002": False,
            "bb_width_expanding": False,
            "bb_breakout_long_ok": False,
            "bb_breakout_short_ok": False,
        }
    snap["whale_flow_score"] = float(_ultimo_whale_flow_score)
    snap["whale_flow_signal"] = str(_ultimo_whale_flow_signal)
    snap["social_sentiment_score"] = float(_ultimo_social_sentiment_score)
    snap["news_sentiment_score"] = int(_ultimo_news_sentiment_score)
    snap["forecast_preco_alvo"] = _ultimo_preco_alvo_previsao
    snap["forecast_tendencia_alta"] = _ultima_tendencia_alta_previsao

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
    turbo_neutral_bypass = False
    is_turbo_this_cycle = _contexto_tem_turbo(contexto_humano, USER_MARKET_OBSERVATION)

    if limiar_short <= probabilidade <= limiar_long:
        if is_turbo_this_cycle:
            print(
                "⚡ [TURBO MODE] Forçando análise do Brain e ignorando filtros de neutralidade."
            )
            direcao_sugerida = _inferir_direcao_sugerida_turbo(
                contexto_humano, USER_MARKET_OBSERVATION
            )
            turbo_neutral_bypass = True
            print(
                f"    → [TURBO] direcao_sugerida={direcao_sugerida} "
                f"(ML P(alta)={probabilidade:.4f} na zona neutra; Hub+Brain activados)."
            )
        else:
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
                f"💤 [WAKE FILTER] XGBoost em zona neutra "
                f"[{limiar_short:.2f}, {limiar_long:.2f}] — Claude NÃO chamado. "
                'Mock: {"sentimento":"NEUTRAL","posicao_recomendada":"VETO","justificativa_curta":"Mercado lateral (XGBoost Neutro). IA poupada para reduzir custos de API."}'
            )
            return

    print(
        f"🧭 [DIRECAO ML] Regras: LONG se P(alta)>{limiar_long:.2f}; "
        f"SHORT se P(alta)<{limiar_short:.2f}; caso contrário NEUTRO."
    )
    if probabilidade > limiar_long:
        direcao_sugerida = "LONG"
    elif probabilidade < limiar_short:
        direcao_sugerida = "SHORT"
    else:
        if not turbo_neutral_bypass:
            print(
                "⚠️ [WAKE FILTER] Probabilidade neutra detetada em ramo de segurança; "
                "sem consulta ao Claude."
            )
            return

    if (
        direcao_sugerida == "LONG"
        and float(_ultimo_whale_flow_score) < -0.5
    ):
        bull_trap_msg = (
            "VETO_WHALE_DUMP: XGBoost sinalizou LONG, mas CoinAPI detectou forte pressão "
            "vendedora de baleias (whale_flow_score < -0.5). Entrada cancelada."
        )
        print(f"🚫 [VETO_WHALE_DUMP] {bull_trap_msg}")
        try:
            ctx_bt = indicators.formatar_log_contexto_raw(
                bull_trap_msg,
                {
                    **snap,
                    "prob_ml": probabilidade,
                    "prob_ml_bruto": prob_ml_base,
                    "direcao_sugerida": direcao_sugerida,
                    "whale_flow_score": _ultimo_whale_flow_score,
                    "whale_flow_signal": _ultimo_whale_flow_signal,
                    "whale_flow_source": whale_flow.get("source"),
                },
            )
        except Exception:
            ctx_bt = None
        logger.registrar_log_trade(
            par_moeda=SYMBOL_TRADE,
            preco=preco,
            prob_ml=probabilidade,
            sentimento="CAUTIOUS",
            acao="VETO_WHALE_DUMP",
            justificativa=bull_trap_msg,
            lado_ordem="LONG",
            contexto_raw=ctx_bt,
            justificativa_ia=bull_trap_msg,
            whale_flow_score=float(_ultimo_whale_flow_score),
            social_sentiment_score=float(_ultimo_social_sentiment_score),
        )
        return

    # Anti-contra-tendência: usa P(alta) **bruta** do XGBoost (antes do ajuste opcional do order book).
    palta_bruto_pct = prob_ml_base * 100.0
    if direcao_sugerida == "SHORT" and prob_ml_base > ML_PROB_BLOQUEIO_SHORT:
        msg_v = (
            f"🚫 [VETO] Bloqueio de SHORT: Força de alta do ML ({palta_bruto_pct:.2f}%) é demasiado "
            "alta para apostar contra."
        )
        print(msg_v)
        try:
            ctx_ml = indicators.formatar_log_contexto_raw(
                msg_v,
                {**snap, "prob_ml": probabilidade, "prob_ml_bruto": prob_ml_base, "direcao_sugerida": "SHORT"},
            )
        except Exception:
            ctx_ml = None
        try:
            logger.registrar_log_trade(
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=prob_ml_base,
                sentimento="NEUTRAL",
                acao="VETO_ML_ANTI_SHORT",
                justificativa=msg_v,
                contexto_raw=ctx_ml,
                justificativa_ia=msg_v,
                lado_ordem="SHORT",
            )
        except Exception as e_ml:  # noqa: BLE001
            print(f"⚠️ [VETO_ML_ANTI_SHORT] Falha ao gravar log: {e_ml}")
        return
    if direcao_sugerida == "LONG" and prob_ml_base < ML_PROB_BLOQUEIO_LONG:
        msg_l = (
            f"🚫 [VETO] Bloqueio de LONG: P(alta) bruta={palta_bruto_pct:.2f}% — modelo indica viés forte "
            "de queda (não apostar contra a tendência matemática)."
        )
        print(msg_l)
        try:
            ctx_ml2 = indicators.formatar_log_contexto_raw(
                msg_l,
                {**snap, "prob_ml": probabilidade, "prob_ml_bruto": prob_ml_base, "direcao_sugerida": "LONG"},
            )
        except Exception:
            ctx_ml2 = None
        try:
            logger.registrar_log_trade(
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=prob_ml_base,
                sentimento="NEUTRAL",
                acao="VETO_ML_ANTI_LONG",
                justificativa=msg_l,
                contexto_raw=ctx_ml2,
                justificativa_ia=msg_l,
                lado_ordem="LONG",
            )
        except Exception as e_ml2:  # noqa: BLE001
            print(f"⚠️ [VETO_ML_ANTI_LONG] Falha ao gravar log: {e_ml2}")
        return

    if macro_trend == "INDEFINIDA":
        print("🚧 [SINAL] Preço na Chop Zone da EMA200 (Indefinida). Aguardando rompimento claro.")
        snap_veto = {
            **snap,
            "prob_ml": probabilidade,
            "macro_trend": macro_trend,
            "macro_allow_dir": macro_allow,
            "macro_ema200": ema200_v,
        }
        try:
            ctx_veto = indicators.formatar_log_contexto_raw(
                "VETO_TENDENCIA_EMA200 por Chop Zone (EMA200 15m).",
                snap_veto,
            )
        except Exception:
            ctx_veto = None
        try:
            logger.registrar_log_trade(
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=probabilidade,
                sentimento="NEUTRAL",
                acao="VETO_TENDENCIA_EMA200",
                justificativa=(
                    "No-Trade Zone EMA200 15m: tendência INDEFINIDA "
                    f"(buffer {EMA_BUFFER_PCT*100:.2f}%)."
                ),
                contexto_raw=ctx_veto,
                justificativa_ia=(
                    "No-Trade Zone EMA200 15m: tendência INDEFINIDA "
                    f"(buffer {EMA_BUFFER_PCT*100:.2f}%)."
                ),
                dist_ema200_pct=dist_ema200_pct,
                spread_atual=spread_atual,
                book_imbalance=book_imbalance,
                hora_do_dia=hora_do_dia_cycle,
                atr_14=atr_14_cycle,
                funding_rate=funding_rate,
                long_short_ratio=long_short_ratio,
            )
        except Exception as e_veto:  # noqa: BLE001
            print(f"⚠️ [VETO_TENDENCIA_EMA200] Falha ao salvar log: {e_veto}")
        return

    counter_trend_ml_override = False
    counter_trend_ml_override_msg = ""
    if macro_allow in ("LONG", "SHORT") and direcao_sugerida != macro_allow:
        prob_alta = max(0.0, min(1.0, float(probabilidade)))
        prob_baixa = max(0.0, min(1.0, 1.0 - prob_alta))
        allow_long_override = direcao_sugerida == "LONG" and prob_alta >= ML_MACRO_OVERRIDE_PROB
        allow_short_override = direcao_sugerida == "SHORT" and prob_baixa >= ML_MACRO_OVERRIDE_PROB
        if allow_long_override or allow_short_override:
            counter_trend_ml_override = True
            if direcao_sugerida == "LONG":
                counter_trend_ml_override_msg = (
                    "Operação Counter-Trend autorizada por override estatístico: "
                    f"P(alta)={prob_alta:.2%} ≥ {ML_MACRO_OVERRIDE_PROB:.0%}."
                )
                print(
                    f"🔥 [ML OVERRIDE] LONG contra a macro! Confiança do ML ({prob_alta:.2%}) "
                    "superou o bloqueio da EMA200."
                )
            else:
                counter_trend_ml_override_msg = (
                    "Operação Counter-Trend autorizada por override estatístico: "
                    f"P(baixa)={prob_baixa:.2%} ≥ {ML_MACRO_OVERRIDE_PROB:.0%}."
                )
                print(
                    f"🔥 [ML OVERRIDE] SHORT contra a macro! Confiança do ML ({prob_baixa:.2%}) "
                    "superou o bloqueio da EMA200."
                )
        else:
            print(
                f"🛑 [EMA200 FILTER] {direcao_sugerida} bloqueado pela tendência macro "
                f"({macro_trend}). Permitido apenas {macro_allow}."
            )
            if direcao_sugerida == "LONG":
                print(
                    f"🧾 [LONG ABORTADO] Filtro macro EMA200 vetou LONG (tendência={macro_trend}, "
                    f"permitido={macro_allow})."
                )
            snap_veto = {
                **snap,
                "prob_ml": probabilidade,
                "macro_trend": macro_trend,
                "macro_allow_dir": macro_allow,
                "macro_ema200": ema200_v,
                "direcao_sugerida": direcao_sugerida,
            }
            try:
                ctx_veto = indicators.formatar_log_contexto_raw(
                    "VETO_TENDENCIA_EMA200 por direção oposta ao filtro macro EMA200 15m.",
                    snap_veto,
                )
            except Exception:
                ctx_veto = None
            try:
                logger.registrar_log_trade(
                    par_moeda=SYMBOL_TRADE,
                    preco=preco,
                    prob_ml=probabilidade,
                    sentimento="NEUTRAL",
                    acao="VETO_TENDENCIA_EMA200",
                    justificativa=(
                        f"Filtro direcional EMA200 15m: tendência {macro_trend}, "
                        f"direção sugerida={direcao_sugerida}, permitido={macro_allow}."
                    ),
                    contexto_raw=ctx_veto,
                    justificativa_ia=(
                        f"Filtro direcional EMA200 15m: tendência {macro_trend}, "
                        f"direção sugerida={direcao_sugerida}, permitido={macro_allow}."
                    ),
                    dist_ema200_pct=dist_ema200_pct,
                    spread_atual=spread_atual,
                    book_imbalance=book_imbalance,
                    hora_do_dia=hora_do_dia_cycle,
                    atr_14=atr_14_cycle,
                    funding_rate=funding_rate,
                    long_short_ratio=long_short_ratio,
                )
            except Exception as e_veto:  # noqa: BLE001
                print(f"⚠️ [VETO_TENDENCIA_EMA200] Falha ao salvar log: {e_veto}")
            return

    print(f"    → direcao_sugerida={direcao_sugerida} (ML vs limiares long/short)")
    pa_bundle: dict[str, Any] = {}

    # 2. Ativa os Olhos (Intelligence Hub)
    print("📡 [HUB] Ativando Intelligence Hub (Nitter + RSS)...")
    hub = intelligence_hub.IntelligenceHub()
    contexto = hub.obter_contexto_agregado()
    print(contexto[:1200] + ("..." if len(contexto) > 1200 else ""))

    sym_ohlcv = SYMBOL_TRADE
    if modo == "FUTURES":
        sym_ohlcv = executor_futures._resolver_simbolo_perp(ex, SYMBOL_TRADE)
        try:
            pa_bundle = indicators.reunir_sinais_price_action(ex, sym_ohlcv, snap)
            rs = pa_bundle.get("resumo")
            if rs not in (None, "", "—"):
                print(f"    [Price Action] {rs}")
        except Exception as e_pa:  # noqa: BLE001
            print(f"    ⚠️ Price Action: {e_pa}")

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

    snap_log = {**snap, "prob_ml": probabilidade, "auric_price_action": pa_bundle}
    if confluencia is not None:
        snap_log["confluencia"] = confluencia
    snap_log["funding_rate"] = (
        executor_futures.obter_funding_rate(SYMBOL_TRADE, ex) if modo == "FUTURES" else None
    )
    snap_log["long_short_ratio"] = (
        executor_futures.obter_long_short_ratio_global(SYMBOL_TRADE, ex)
        if modo == "FUTURES"
        else None
    )
    snap_log["whale_flow_score"] = float(_ultimo_whale_flow_score)
    snap_log["whale_flow_signal"] = str(_ultimo_whale_flow_signal)
    snap_log["whale_flow_source"] = whale_flow.get("source")
    snap_log["news_sentiment_score"] = int(_ultimo_news_sentiment_score)
    snap_log["forecast_preco_alvo"] = _ultimo_preco_alvo_previsao
    snap_log["forecast_tendencia_alta"] = _ultima_tendencia_alta_previsao

    contexto_raw_supabase = indicators.formatar_log_contexto_raw(contexto, snap_log)
    try:
        _ultimo_rsi_14 = float(snap.get("rsi_14")) if snap.get("rsi_14") is not None else 0.0
    except (TypeError, ValueError):
        _ultimo_rsi_14 = 0.0
    try:
        _ultimo_adx_14 = float(snap.get("adx_14")) if snap.get("adx_14") is not None else 0.0
    except (TypeError, ValueError):
        _ultimo_adx_14 = 0.0
    _ultimo_contexto_raw_supabase = contexto_raw_supabase
    bloco_ta = brain.montar_bloco_tecnico_final_boss(snap_log)

    contexto_humano_brain = _combinar_contexto_humano_supabase_e_ram(
        contexto_humano,
        USER_MARKET_OBSERVATION,
    )
    if counter_trend_ml_override:
        extra_override = (
            "[COUNTER_TREND_ML_OVERRIDE]\n"
            f"{counter_trend_ml_override_msg}\n"
            "Esta operação é contra a direção macro EMA200, mas foi aprovada por confiança "
            "estatística elevada do modelo. Não vetar apenas por estar contra a EMA200; "
            "avaliar gestão de risco, exaustão e confirmação contextual."
        )
        contexto_humano_brain = (
            f"{contexto_humano_brain}\n\n{extra_override}"
            if contexto_humano_brain
            else extra_override
        )

    # 3. Ativa o Cérebro (Claude) com rate limiter para proteger a API.
    print(
        "🧪 [TA->CLAUDE] "
        f"RSI14={snap.get('rsi_14')} | ADX14={snap.get('adx_14')} | "
        f"ATR%={snap.get('atr_pct')} | BB_squeeze={snap.get('bollinger_squeeze')} | "
        f"BB_width={snap.get('bb_width_pct')} | VWAP_vies={snap.get('vies_vwap')} | "
        f"funding={snap_log.get('funding_rate')} | long_short_ratio={snap_log.get('long_short_ratio')} | "
        f"direcao_sugerida={direcao_sugerida}"
    )
    if counter_trend_ml_override:
        print(
            "🧠 [CLAUDE CONTEXTO] Counter-Trend override ativo por alta confiança do ML; "
            "instrução enviada para não vetar apenas por EMA200."
        )
    pode_chamar_claude, n_chamadas, espera_s = _permitir_consulta_claude()
    if pode_chamar_claude:
        print(
            "🧠 [BRAIN] Consultando Claude 3.5 Sonnet (Final Boss Mode)... "
            f"(janela 60s: {n_chamadas}/{CLAUDE_RATE_LIMIT_MAX_CALLS})"
        )
        veredito = brain.analisar_sentimento_mercado(
            contexto,
            prob_ml=probabilidade,
            prob_ml_bruto=prob_ml_base,
            limiar_ml=WAKE_FILTER_LONG_MIN,
            limiar_ml_short=WAKE_FILTER_SHORT_MAX,
            direcao_sugerida=direcao_sugerida,
            verbose=False,
            bloco_tecnico_prioritario=bloco_ta,
            micro_estrutura_posicionamento={
                "funding_rate": snap_log.get("funding_rate"),
                "long_short_ratio": snap_log.get("long_short_ratio"),
            },
            user_market_observation=contexto_humano_brain,
            is_turbo=is_turbo_this_cycle,
            whale_flow_score=float(_ultimo_whale_flow_score),
            whale_flow_signal=str(_ultimo_whale_flow_signal),
        )
        if USER_MARKET_OBSERVATION_CLEAR_AFTER_BRAIN_CALL and USER_MARKET_OBSERVATION:
            if str(veredito.get("sentimento", "")).upper() != "ERROR":
                USER_MARKET_OBSERVATION = None
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

    if not manual_override_veredito:
        texto_audit_ml = f"{contexto}\n{alerta}\n{just_ia}"
        if (
            prob_ml_base > ML_PROB_SUBMISSAO_LONG
            and direcao_sugerida == "LONG"
            and pos_rec != "LONG"
        ):
            if not _contexto_tem_catastrofe_sistemica(texto_audit_ml):
                print(
                    f"⚜️ [ML SUBMISSÃO] P(alta) bruto {palta_bruto_pct:.2f}% > "
                    f"{ML_PROB_SUBMISSAO_LONG:.0%} — Claude não pode vetar LONG por notícias leves; "
                    "a forçar confirmação (sem catástrofe sistémica no contexto)."
                )
                pos_rec = "LONG"
                sent = "BULLISH"
                conf_num = max(conf_num, CONFIANCA_BRAIN_MIN_ENTRADA)
        if (
            prob_ml_base < ML_PROB_SUBMISSAO_SHORT
            and direcao_sugerida == "SHORT"
            and pos_rec != "SHORT"
        ):
            if not _contexto_tem_catalisador_altista_estrutural(texto_audit_ml):
                print(
                    f"⚜️ [ML SUBMISSÃO] P(alta) bruto {palta_bruto_pct:.2f}% < "
                    f"{ML_PROB_SUBMISSAO_SHORT:.0%} — Claude não pode vetar SHORT por ruído altista moderado; "
                    "a forçar confirmação (sem catalisador estrutural explícito)."
                )
                pos_rec = "SHORT"
                sent = "BEARISH"
                conf_num = max(conf_num, CONFIANCA_BRAIN_MIN_ENTRADA)

    if (
        parede_venda_proxima_confirma_short
        and modo == "FUTURES"
        and direcao_sugerida == "SHORT"
        and not manual_override_veredito
        and sent == "CAUTIOUS"
    ):
        px_wall = (
            f"{parede_venda_preco_ref:.2f}"
            if parede_venda_preco_ref is not None
            else "X.XX"
        )
        print(f"🧱 [WALL DETECTED] Muralha de venda detectada em {px_wall}. Confirmando Short.")
        sent = "BEARISH"
        pos_rec = "SHORT"
        conf_num = max(conf_num, CONFIANCA_BRAIN_MIN_ENTRADA)

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
        if direcao_sugerida == "LONG":
            print(
                f"🧾 [LONG ABORTADO] Sentimento do Claude não autorizou entrada LONG "
                f"(sentimento={sent})."
            )
        return

    if (
        not manual_override_veredito
        and not (parede_venda_proxima_confirma_short and sent == "BEARISH" and direcao_sugerida == "SHORT")
        and (pos_rec == "VETO" or pos_rec != direcao_sugerida)
    ):
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
        if direcao_sugerida == "LONG" or pos_rec == "LONG":
            print(
                f"🧾 [LONG ABORTADO] Claude não confirmou LONG "
                f"(posicao_recomendada={pos_rec}, direcao_sugerida={direcao_sugerida})."
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
        if direcao_sugerida == "LONG":
            print(
                f"🧾 [LONG ABORTADO] Confiança insuficiente para LONG: "
                f"{conf_num}% < {CONFIANCA_BRAIN_MIN_ENTRADA}%."
            )
        return

    if (
        not manual_override_veredito
        and modo == "FUTURES"
        and _vigia_eth_usdc_futures(modo_label)
    ):
        tight = bool(snap.get("bb_squeeze_tight_002"))
        if tight:
            bloq_long = (
                sent == "BULLISH"
                and pos_rec == "LONG"
                and not bool(snap.get("bb_breakout_long_ok"))
            )
            bloq_short = (
                sent == "BEARISH"
                and pos_rec == "SHORT"
                and not bool(snap.get("bb_breakout_short_ok"))
            )
            if bloq_long or bloq_short:
                msg_squeeze = (
                    "💤 [SQUEEZE] Mercado lateral (volatilidade < 0.2%). Sniper em standby.'"
                )
                print(msg_squeeze)
                logger.registrar_log_trade(
                    par_moeda=SYMBOL_TRADE,
                    preco=preco,
                    prob_ml=probabilidade,
                    sentimento=sent,
                    acao="VETO_BB_SQUEEZE_ENTRADA",
                    justificativa=(
                        f"{msg_squeeze} | {just_ia} | BB σ=1.5: compressão <0,2% do preço "
                        "sem setup válido (bandas a expandir + rompimento BBU/BBL + volume "
                        "> média 10 velas)."
                    ),
                    lado_ordem="LONG" if bloq_long else "SHORT",
                    contexto_raw=contexto_raw_supabase,
                    justificativa_ia=just_ia,
                    noticias_agregadas=contexto,
                )
                if bloq_long:
                    print(
                        "🧾 [LONG ABORTADO] BB Squeeze sem breakout válido para entrada LONG."
                    )
                return

    if not manual_override_veredito and modo == "FUTURES" and pa_bundle.get("short_squeeze"):
        if sent == "BEARISH" and pos_rec == "SHORT":
            print(
                "    📈 [SHORT SQUEEZE] Vela 1m forte + volume 2× média + RSI>70 — "
                "evitar SHORT; preferir seguir fluxo (trend following / coberturas)."
            )
            logger.registrar_log_trade(
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=probabilidade,
                sentimento=sent,
                acao="VETO_SHORT_SQUEEZE",
                justificativa=(
                    f"{just_ia} | Short squeeze em curso (1m: Δpreço>0,4%, vol≥2×média, RSI>70)."
                ),
                lado_ordem="SHORT",
                contexto_raw=contexto_raw_supabase,
                justificativa_ia=just_ia,
                noticias_agregadas=contexto,
            )
            return

    if (
        not manual_override_veredito
        and modo == "FUTURES"
        and pa_bundle.get("double_top")
        and prob_ml_base <= ML_PROB_BLOQUEIO_SHORT
    ):
        if sent == "BULLISH" and pos_rec == "LONG":
            print(
                "    [Price Action] Double Top (dois topos locais + recuo) — viés baixa; LONG vetado."
            )
            print("🧾 [LONG ABORTADO] Padrão Double Top detectado (filtro de Price Action).")
            logger.registrar_log_trade(
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=probabilidade,
                sentimento=sent,
                acao="VETO_DOUBLE_TOP",
                justificativa=(
                    f"{just_ia} | Double Top: pivots 1m alinhados (±{indicators.DOUBLE_PIVOT_ZONE_FRAC*100:.3f}%) "
                    "e recuo após teste de topo anterior."
                ),
                lado_ordem="LONG",
                contexto_raw=contexto_raw_supabase,
                justificativa_ia=just_ia,
                noticias_agregadas=contexto,
            )
            return

    if (
        not manual_override_veredito
        and modo == "FUTURES"
        and sent == "BEARISH"
        and pos_rec == "SHORT"
        and prob_ml_base >= ML_PROB_BLOQUEIO_LONG
    ):
        db_pa = bool(pa_bundle.get("double_bottom"))
        db_troughs = False
        try:
            sym_db = executor_futures._resolver_simbolo_perp(ex, SYMBOL_TRADE)
            ohlcv_db = ex.fetch_ohlcv(sym_db, "1m", limit=50)
            db_troughs = indicators.detect_double_bottom(ohlcv_db, tolerance=0.001)
        except Exception:
            db_troughs = False
        if db_pa or db_troughs:
            if db_troughs and not db_pa:
                print(
                    "    [Filtro] Dois últimos mínimos locais (50×1m) alinhados + preço no nível — "
                    "evitar SHORT em suporte duplo (VETO_DOUBLE_BOTTOM)."
                )
                j_db = (
                    "Dois últimos troughs 1m (±0,1% nos lows e close no nível médio); "
                    "risco de suporte / fundo duplo."
                )
            elif db_pa and not db_troughs:
                print(
                    "    [Price Action] Double Bottom (dois fundos locais + repique) — viés alta; SHORT vetado."
                )
                j_db = (
                    f"Double Bottom (PA): pivots 1m alinhados (±{indicators.DOUBLE_PIVOT_ZONE_FRAC*100:.3f}%) "
                    "e repique após teste de fundo anterior."
                )
            else:
                print(
                    "    [Price Action + Filtro] Double Bottom (PA e troughs 50×1m) — SHORT vetado."
                )
                j_db = (
                    f"Double Bottom: PA (±{indicators.DOUBLE_PIVOT_ZONE_FRAC*100:.3f}%) e/ou "
                    "dois últimos mínimos locais 1m com preço no nível (tolerance 0,1%)."
                )
            logger.registrar_log_trade(
                par_moeda=SYMBOL_TRADE,
                preco=preco,
                prob_ml=probabilidade,
                sentimento=sent,
                acao="VETO_DOUBLE_BOTTOM",
                justificativa=f"{just_ia} | {j_db}",
                lado_ordem="SHORT",
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
            try:
                logger.registrar_log_trade(
                    par_moeda=SYMBOL_TRADE,
                    preco=preco,
                    prob_ml=probabilidade,
                    sentimento="NEUTRAL",
                    acao="VETO_ADX_LATERAL",
                    justificativa=(
                        f"{just_ia} | ADX(14)={adx_s} < {indicators.ADX_LIMIAR_TENDENCIA} "
                        "sem Bollinger Squeeze — sinal de continuação/tendência desvalorizado."
                    ),
                    contexto_raw=contexto_raw_supabase,
                    justificativa_ia=just_ia,
                    noticias_agregadas=contexto,
                    dist_ema200_pct=dist_ema200_pct,
                    spread_atual=spread_atual,
                    book_imbalance=book_imbalance,
                    hora_do_dia=hora_do_dia_cycle,
                    atr_14=atr_14_cycle,
                    funding_rate=funding_rate,
                    long_short_ratio=long_short_ratio,
                )
            except Exception as e_veto_adx:  # noqa: BLE001
                print(f"⚠️ [VETO_ADX_LATERAL] Falha ao salvar log: {e_veto_adx}")
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

    rsi_raw = snap.get("rsi_14")
    try:
        rsi_num = float(rsi_raw) if rsi_raw is not None else None
    except (TypeError, ValueError):
        rsi_num = None

    adx_num = _ultimo_adx_14
    rsi_txt = f"{rsi_num:.2f}" if rsi_num is not None else "N/A"
    adx_txt = f"{adx_num:.2f}" if adx_num is not None else "N/A"
    if sent == "BULLISH":
        just_ia_entry = (
            str(just_ia).strip()
            if str(just_ia).strip()
            else f"Sinal LONG - Tendência de alta confirmada. RSI: {rsi_txt}, ADX: {adx_txt}"
        )
    elif sent == "BEARISH":
        just_ia_entry = (
            str(just_ia).strip()
            if str(just_ia).strip()
            else f"Sinal SHORT - Tendência de baixa confirmada. RSI: {rsi_txt}, ADX: {adx_txt}"
        )
    else:
        just_ia_entry = str(just_ia).strip()

    short_adx_bypass = False
    if (
        not manual_override_veredito
        and sent == "BEARISH"
        and rsi_num is not None
        and rsi_num < 35.0
    ):
        print(
            f"    [Risco] RSI sobrevendido extremo (RSI(14)={rsi_num:.2f} < 35) — SHORT proibido."
        )
        logger.registrar_log_trade(
            par_moeda=SYMBOL_TRADE,
            preco=preco,
            prob_ml=probabilidade,
            sentimento=sent,
            acao="VETO_RSI_OVERSOLD",
            justificativa=(
                f"{just_ia} | RSI(14)={rsi_num:.2f} < 35 (abrir SHORT vetado para evitar vender fundo)"
            ),
            lado_ordem="SHORT",
            contexto_raw=contexto_raw_supabase,
            justificativa_ia=just_ia,
            noticias_agregadas=contexto,
        )
        return

    if (
        not manual_override_veredito
        and sent == "BULLISH"
        and rsi_num is not None
        and rsi_num > 65.0
    ):
        print(
            f"    [VETO_RSI_EXAUSTAO] Sinal de LONG bloqueado (RSI muito alto: {rsi_num:.2f})."
        )
        print(
            f"🧾 [LONG ABORTADO] RSI em exaustão ({rsi_num:.2f} > 65.00) — evitando compra no topo."
        )
        logger.registrar_log_trade(
            par_moeda=SYMBOL_TRADE,
            preco=preco,
            prob_ml=probabilidade,
            sentimento=sent,
            acao="VETO_RSI_EXAUSTAO",
            justificativa=(
                f"{just_ia} | RSI(14)={rsi_num:.2f} > 65 (abrir LONG vetado para evitar comprar topo)"
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
                f"COMPRA (Long) LIMIT (+0,05% vs. último) — notional ≈ {notional_alvo:.2f} USDC "
                f"(risk {_risk_fraction_cfg*100:.1f}% × {_alavancagem_cfg:.2f}x, {modo_label}, mainnet)."
            )
        else:
            notional_alvo = executor_spot.obter_saldo_usdt(ex) * executor_spot.PERCENTUAL_BANCA
            pct_spot = executor_spot.PERCENTUAL_BANCA * 100.0
            entrada_txt = (
                f"COMPRA (Long) LIMIT (+0,05%) — custo ≈ {notional_alvo:.2f} USDC "
                f"({pct_spot:.1f}% saldo spot, {modo_label}, mainnet)."
            )
        print(
            f"    [Buscando Oportunidade] BULLISH + ML/Brain alinhados + "
            f"conf≥{CONFIANCA_BRAIN_MIN_ENTRADA}% — {entrada_txt}"
        )
        if manual_override_veredito:
            print("    👑 [GOD MODE] Execução LONG forçada por veredito MANUAL.")
        try:
            # Passo 3 (Juiz Final): só no instante pré-ordem para poupar API/custo.
            try:
                df_vis = ml_model.fetch_ohlcv_binance(
                    symbol=SYMBOL_TRADE,
                    timeframe=ml_model.TIMEFRAME,
                    total=120,
                )
            except Exception:
                df_vis = None
            _ultima_etapa_funil = "PASSO_3_JUIZ_FINAL"
            veto_visual = bool(asyncio.run(intelligence_hub.confirmar_entrada_visao(df_vis, "LONG")))
            if veto_visual:
                _ultimo_llava_veto = True
                _ultima_razao_abort_funil = "LLAVA_VISUAL_BARRIER"
                msg_veto_vis = "Llava vetou a entrada por barreira visual (Juiz Final)."
                print(f"🛑 [JUIZ FINAL] {msg_veto_vis}")
                logger.registrar_log_trade(
                    par_moeda=SYMBOL_TRADE,
                    preco=preco,
                    prob_ml=probabilidade,
                    sentimento=sent,
                    acao="VETO_LLAVA_VISUAL",
                    justificativa=f"{just_ia_entry} | {msg_veto_vis}",
                    lado_ordem="LONG",
                    contexto_raw=contexto_raw_supabase,
                    justificativa_ia=just_ia_entry,
                    noticias_agregadas=contexto,
                    whale_flow_score=float(_ultimo_whale_flow_score),
                    social_sentiment_score=float(_ultimo_social_sentiment_score),
                    funnel_stage=_ultima_etapa_funil,
                    funnel_abort_reason=_ultima_razao_abort_funil,
                    ml_prob_base=_ultima_ml_prob_base,
                    ml_prob_calibrated=_ultima_ml_prob_calibrada,
                    llava_veto=True,
                )
                _emit_funnel_observability(
                    "ABORT_PASSO_3",
                    {
                        "reason": _ultima_razao_abort_funil,
                        "order_side": "LONG",
                        "ml_prob_base": _ultima_ml_prob_base,
                        "ml_prob_calibrated": _ultima_ml_prob_calibrada,
                        "forecast_target": _ultimo_preco_alvo_previsao,
                        "forecast_trend_up": _ultima_tendencia_alta_previsao,
                    },
                )
                return
            _ultimo_llava_veto = False
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
                    turbo_chase=is_turbo_this_cycle,
                )
                if bool(ordem.get("auric_skipped")):
                    _ultima_razao_abort_funil = "REDIS_LOCK_ACTIVE"
                    print("⏭️ [LOCK] LONG ignorado: lock Redis ativo (anti-spam 5s).")
                    logger.registrar_log_trade(
                        par_moeda=SYMBOL_TRADE,
                        preco=preco,
                        prob_ml=probabilidade,
                        sentimento=sent,
                        acao="VETO_REDIS_LOCK",
                        justificativa=f"{just_ia_entry} | lock Redis ativo; entrada ignorada.",
                        lado_ordem="LONG",
                        contexto_raw=contexto_raw_supabase,
                        justificativa_ia=just_ia_entry,
                        noticias_agregadas=contexto,
                        whale_flow_score=float(_ultimo_whale_flow_score),
                        social_sentiment_score=float(_ultimo_social_sentiment_score),
                        funnel_stage="PASSO_3_EXECUCAO",
                        funnel_abort_reason=_ultima_razao_abort_funil,
                        ml_prob_base=_ultima_ml_prob_base,
                        ml_prob_calibrated=_ultima_ml_prob_calibrada,
                        llava_veto=False,
                    )
                    return
            else:
                ordem = ex_mod.executar_compra_spot_market(SYMBOL_TRADE, exchange=ex)
            oid = ordem.get("id", "?")
            st = ordem.get("status", "?")
            commission, is_maker = _commission_and_maker_from_order(ordem)
            preco_compra = float(ordem.get("auric_entry_price") or preco)
            posicao_aberta = True
            direcao_posicao = "LONG"
            _sync_entry_price_supabase()
            bracket_note = (
                " Bracket reduce-only na Binance (SL STOP_MARKET + TRAILING_STOP_MARKET; "
                f"activation no antigo TP×{trailing_mult:.3f}, callback {trailing_cb:.3f}%)."
                if modo == "FUTURES"
                else ""
            )
            just_final = (
                f"{just_ia_entry} | Ordem id={oid} status={st}; "
                f"notional/custo alvo ≈ {notional_alvo:.2f} USDC.{bracket_note} "
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
                justificativa_ia=just_ia_entry,
                noticias_agregadas=contexto,
                commission=commission,
                is_maker=is_maker,
                rsi_14=rsi_num,
                adx_14=adx_num,
                funnel_stage="PASSO_3_EXECUCAO",
                funnel_abort_reason=None,
                ml_prob_base=_ultima_ml_prob_base,
                ml_prob_calibrated=_ultima_ml_prob_calibrada,
                llava_veto=False,
            )
            _emit_funnel_observability(
                "EXEC_PASSO_3",
                {
                    "order_side": "LONG",
                    "ml_prob_base": _ultima_ml_prob_base,
                    "ml_prob_calibrated": _ultima_ml_prob_calibrada,
                    "forecast_target": _ultimo_preco_alvo_previsao,
                    "forecast_trend_up": _ultima_tendencia_alta_previsao,
                },
            )
            print(
                f"\n    [Estado] COMPRA Long: posicao_aberta=True | preco ref.={preco_compra:.4f} USDC"
            )
            print(
                "    Próximos ciclos: >>> MODO VIGIA <<< "
                + (
                    "(FUTURES: SL fixo + trailing stop já na bolsa; "
                    "o maestro ainda monitora como rede de segurança)"
                    if modo == "FUTURES"
                    else "(TP +2% / SL -0.6%)."
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
            f"VENDA (Short) LIMIT (−0,05% vs. último) — notional ≈ {notional_alvo:.2f} USDC "
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
            # Passo 3 (Juiz Final): só no instante pré-ordem para poupar API/custo.
            try:
                df_vis = ml_model.fetch_ohlcv_binance(
                    symbol=SYMBOL_TRADE,
                    timeframe=ml_model.TIMEFRAME,
                    total=120,
                )
            except Exception:
                df_vis = None
            _ultima_etapa_funil = "PASSO_3_JUIZ_FINAL"
            veto_visual = bool(asyncio.run(intelligence_hub.confirmar_entrada_visao(df_vis, "SHORT")))
            if veto_visual:
                _ultimo_llava_veto = True
                _ultima_razao_abort_funil = "LLAVA_VISUAL_BARRIER"
                msg_veto_vis = "Llava vetou a entrada por barreira visual (Juiz Final)."
                print(f"🛑 [JUIZ FINAL] {msg_veto_vis}")
                logger.registrar_log_trade(
                    par_moeda=SYMBOL_TRADE,
                    preco=preco,
                    prob_ml=probabilidade,
                    sentimento=sent,
                    acao="VETO_LLAVA_VISUAL",
                    justificativa=f"{just_ia_entry} | {msg_veto_vis}",
                    lado_ordem="SHORT",
                    contexto_raw=contexto_raw_supabase,
                    justificativa_ia=just_ia_entry,
                    noticias_agregadas=contexto,
                    whale_flow_score=float(_ultimo_whale_flow_score),
                    social_sentiment_score=float(_ultimo_social_sentiment_score),
                    funnel_stage=_ultima_etapa_funil,
                    funnel_abort_reason=_ultima_razao_abort_funil,
                    ml_prob_base=_ultima_ml_prob_base,
                    ml_prob_calibrated=_ultima_ml_prob_calibrada,
                    llava_veto=True,
                )
                _emit_funnel_observability(
                    "ABORT_PASSO_3",
                    {
                        "reason": _ultima_razao_abort_funil,
                        "order_side": "SHORT",
                        "ml_prob_base": _ultima_ml_prob_base,
                        "ml_prob_calibrated": _ultima_ml_prob_calibrada,
                        "forecast_target": _ultimo_preco_alvo_previsao,
                        "forecast_trend_up": _ultima_tendencia_alta_previsao,
                    },
                )
                return
            _ultimo_llava_veto = False
            print(f"⚡ [ORDEM] Enviando comando de {sent} para a Binance...")
            trailing_cb, trailing_mult = _trailing_callback_cfg, _trailing_activation_mult_cfg
            ordem = executor_futures.abrir_short_market(
                SYMBOL_TRADE,
                ex,
                alavancagem=float(_alavancagem_cfg),
                risk_fraction=float(_risk_fraction_cfg),
                trailing_callback_rate=trailing_cb,
                trailing_activation_multiplier=trailing_mult,
                turbo_chase=is_turbo_this_cycle,
            )
            if bool(ordem.get("auric_skipped")):
                _ultima_razao_abort_funil = "REDIS_LOCK_ACTIVE"
                print("⏭️ [LOCK] SHORT ignorado: lock Redis ativo (anti-spam 5s).")
                logger.registrar_log_trade(
                    par_moeda=SYMBOL_TRADE,
                    preco=preco,
                    prob_ml=probabilidade,
                    sentimento=sent,
                    acao="VETO_REDIS_LOCK",
                    justificativa=f"{just_ia_entry} | lock Redis ativo; entrada ignorada.",
                    lado_ordem="SHORT",
                    contexto_raw=contexto_raw_supabase,
                    justificativa_ia=just_ia_entry,
                    noticias_agregadas=contexto,
                    whale_flow_score=float(_ultimo_whale_flow_score),
                    social_sentiment_score=float(_ultimo_social_sentiment_score),
                    funnel_stage="PASSO_3_EXECUCAO",
                    funnel_abort_reason=_ultima_razao_abort_funil,
                    ml_prob_base=_ultima_ml_prob_base,
                    ml_prob_calibrated=_ultima_ml_prob_calibrada,
                    llava_veto=False,
                )
                return
            oid = ordem.get("id", "?")
            st = ordem.get("status", "?")
            commission, is_maker = _commission_and_maker_from_order(ordem)
            preco_compra = float(ordem.get("auric_entry_price") or preco)
            posicao_aberta = True
            direcao_posicao = "SHORT"
            _sync_entry_price_supabase()
            just_final = (
                f"{just_ia_entry} | Short id={oid} status={st}; notional alvo ≈ {notional_alvo:.2f} USDC. "
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
                justificativa_ia=just_ia_entry,
                noticias_agregadas=contexto,
                commission=commission,
                is_maker=is_maker,
                rsi_14=rsi_num,
                adx_14=adx_num,
                funnel_stage="PASSO_3_EXECUCAO",
                funnel_abort_reason=None,
                ml_prob_base=_ultima_ml_prob_base,
                ml_prob_calibrated=_ultima_ml_prob_calibrada,
                llava_veto=False,
            )
            _emit_funnel_observability(
                "EXEC_PASSO_3",
                {
                    "order_side": "SHORT",
                    "ml_prob_base": _ultima_ml_prob_base,
                    "ml_prob_calibrated": _ultima_ml_prob_calibrada,
                    "forecast_target": _ultimo_preco_alvo_previsao,
                    "forecast_trend_up": _ultima_tendencia_alta_previsao,
                },
            )
            print(
                f"\n    [Estado] SHORT aberto: posicao_aberta=True | preco ref.={preco_compra:.4f} USDC"
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
    """Executa o ciclo completo em thread para não bloquear o event loop (asyncio.to_thread)."""
    lock_ctx = await _acquire_redis_order_lock(SYMBOL_TRADE)
    if lock_ctx == ("", ""):
        print("⚠️ Ordem bloqueada pelo Redis para evitar spam", flush=True)
        return
    try:
        await asyncio.to_thread(rodar_ciclo, modo)
    except Exception as e:  # noqa: BLE001
        print(f"\n🚨 ERRO CRÍTICO FATAL NO CICLO (rodar_ciclo / {origem}): {e}", flush=True)
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
    finally:
        await _release_redis_order_lock(lock_ctx)


async def safe_rodar_ciclo() -> None:
    """
    Pipeline de gatilho alinhado ao WS (config dinâmica, standby, lock, cooldown, RSI/ADX).
    Usado pelo watchdog quando o fecho 1m (x) falha no multiplex.
    """
    global _risk_fraction_cfg, _alavancagem_cfg, _trailing_callback_cfg, _trailing_activation_mult_cfg
    global _ultimo_gatilho_ciclo_mono
    if _gatilho_ciclo_lock is None:
        return
    now_mono = time.monotonic()
    if now_mono - _ultimo_gatilho_ciclo_mono < TRIGGER_COOLDOWN_S:
        return
    if _gatilho_ciclo_lock.locked():
        return

    cfg_dyn = await _obter_configuracoes_dinamicas()
    _risk_fraction_cfg = float(cfg_dyn.get("risk_fraction", RISK_FRACTION_PADRAO))
    _alavancagem_cfg = float(cfg_dyn.get("leverage", ALAVANCAGEM_PADRAO))
    _trailing_callback_cfg = float(cfg_dyn.get("trailing_callback_rate", TRAILING_CALLBACK_PADRAO))
    _trailing_activation_mult_cfg = float(
        cfg_dyn.get(
            "trailing_activation_multiplier",
            TRAILING_ACTIVATION_MULTIPLIER_PADRAO,
        )
    )
    print(
        "\n[WATCHDOG] Config dinâmica (relogio): "
        f"risk={_risk_fraction_cfg*100:.1f}% | lev={_alavancagem_cfg:.2f}x | "
        f"trail_cb={_trailing_callback_cfg:.3f}% | trail_act_mult={_trailing_activation_mult_cfg:.3f}",
        flush=True,
    )

    permitido = await asyncio.to_thread(verificar_permissao_operacao)
    if not permitido:
        print("\n💤 [WATCHDOG] Standby (is_active=false). Ciclo não executado.", flush=True)
        return

    modo = await asyncio.to_thread(obter_modo_operacao)
    print(
        f"\n🚀 [WATCHDOG] modo={modo} | iniciando ciclo (rodar_ciclo em worker thread)...",
        flush=True,
    )
    try:
        await asyncio.to_thread(_sincronizar_rsi_adx_globais_de_snap_ws)
    except Exception as e_snap:  # noqa: BLE001
        print(
            f"\n🚨 [WATCHDOG] sync RSI/ADX: {e_snap}",
            flush=True,
        )
        traceback.print_exc()

    async with _gatilho_ciclo_lock:
        _ultimo_gatilho_ciclo_mono = time.monotonic()
        try:
            await _executar_ciclo_assincrono(modo, "WATCHDOG_CLOCK")
        finally:
            estado = "MODO VIGIA" if posicao_aberta else "BUSCANDO OPORTUNIDADE"
            print(f"\n✅ [WATCHDOG] Ciclo concluído. Estado atual: {estado}", flush=True)


async def _watchdog_relogio() -> None:
    """
    Rede de segurança: se o minuto UTC mudar e o WS não tiver atualizado `ultimo_minuto_processado`,
    força `safe_rodar_ciclo`. Heartbeat em :00, :20, :40 para confirmar que o loop corre.
    """
    global ultimo_minuto_processado
    print(
        "🐕 [WATCHDOG] Cão de guarda ativado e a monitorizar o relógio (Versão Blindada)!",
        flush=True,
    )
    while True:
        try:
            await asyncio.sleep(1)
            agora = datetime.now(timezone.utc)
            minuto_atual = agora.minute
            segundo_atual = agora.second

            if segundo_atual in (0, 20, 40):
                print(
                    f"🐕 [WATCHDOG] ♥ vivo | UTC {agora.strftime('%H:%M:%S')}",
                    flush=True,
                )

            if segundo_atual >= 2 and minuto_atual != ultimo_minuto_processado:
                if ultimo_minuto_processado == -1:
                    ultimo_minuto_processado = minuto_atual
                    continue

                print(
                    f"🚨 [WATCHDOG] Binance falhou na virada do minuto! "
                    f"Forçando ciclo do minuto {minuto_atual}...",
                    flush=True,
                )
                ultimo_minuto_processado = minuto_atual
                asyncio.create_task(safe_rodar_ciclo())
        except Exception as e:  # noqa: BLE001
            print(f"🐕 [WATCHDOG] ERRO FATAL: {e}", flush=True)
            traceback.print_exc()


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
        print(f"\n⚠️ [AUDITORIA] Falha ao ler logs recentes: {e}")
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
                # Silenciado: um print por ordem fechada inunda o terminal e compete com o WS.
                # pnl_txt = f"{pnl:+.2f}" if pnl is not None else "N/A"
                # print(
                #     "[AUDITORIA] Ordem "
                #     f"{side} fechada. PnL Realizado: {pnl_txt} USDC. "
                #     "Resultado guardado no Outcome Engine."
                # )

            if upserts:
                print(
                    f"\n[AUDITORIA] Reconciliação: {len(upserts)} ordem(ns) fechada(s) "
                    f"→ trade_outcomes (sem print por ordem; evita flood)."
                )
                await asyncio.to_thread(_upsert_outcomes_sync, upserts)
        except Exception as e:  # noqa: BLE001
            print(f"\n⚠️ [AUDITORIA] Falha no loop de reconciliação: {e}")


def _sincronizar_rsi_adx_globais_de_snap_ws() -> None:
    """
    Atualiza `_ultimo_rsi_14` / `_ultimo_adx_14` a partir do snapshot OHLCV (mesma fonte que o ciclo ML).
    Chamado desde o WS antes do print de vela fechada, para não mostrar 0.0 no primeiro tick.
    """
    global _ultimo_rsi_14, _ultimo_adx_14
    try:
        snap = ml_model.obter_snapshot_indicadores_eth(SYMBOL_TRADE, prob_ml=None)
    except Exception as e:  # noqa: BLE001
        print(f"\n⚠️ [WS] Snapshot indicadores indisponível para log de vela: {e}")
        return
    try:
        _ultimo_rsi_14 = float(snap.get("rsi_14")) if snap.get("rsi_14") is not None else 0.0
    except (TypeError, ValueError):
        _ultimo_rsi_14 = 0.0
    try:
        _ultimo_adx_14 = float(snap.get("adx_14")) if snap.get("adx_14") is not None else 0.0
    except (TypeError, ValueError):
        _ultimo_adx_14 = 0.0


def _ema9_slope_pct_1m(ex: Any, simbolo_ccxt: str) -> float | None:
    """
    Inclinação percentual da EMA9 (1m) entre os dois últimos pontos.
    Retorna % (ex.: +0.12, -0.08) ou None se dados insuficientes.
    """
    candles = ex.fetch_ohlcv(simbolo_ccxt, timeframe="1m", limit=40) or []
    closes = [float(c[4]) for c in candles if len(c) >= 5]
    if len(closes) < 12:
        return None
    alpha = 2.0 / (9.0 + 1.0)
    ema_vals: list[float] = []
    ema_prev = closes[0]
    for px in closes:
        ema_prev = (px * alpha) + (ema_prev * (1.0 - alpha))
        ema_vals.append(float(ema_prev))
    if len(ema_vals) < 2:
        return None
    prev = float(ema_vals[-2])
    cur = float(ema_vals[-1])
    if abs(prev) <= 1e-12:
        return None
    return ((cur - prev) / prev) * 100.0


async def _fetch_rss_feed_entries(
    session: aiohttp.ClientSession,
    *,
    source: str,
    url: str,
) -> list[tuple[datetime, str, str]]:
    """
    Busca um RSS e devolve [(dt_utc, fonte, título)].
    """
    out: list[tuple[datetime, str, str]] = []
    try:
        async with session.get(url, timeout=3) as resp:
            if resp.status != 200:
                return out
            raw = await resp.read()
        root = ET.fromstring(raw)
        for it in root.findall(".//item"):
            title = ((it.findtext("title") or "").strip())
            if not title:
                continue
            dt_obj: datetime | None = None
            pub_raw = (it.findtext("pubDate") or it.findtext("published") or "").strip()
            if pub_raw:
                try:
                    dt_obj = parsedate_to_datetime(pub_raw)
                except Exception:
                    dt_obj = None
            if dt_obj is None:
                # Atom fallback
                upd_raw = (it.findtext("{http://www.w3.org/2005/Atom}updated") or "").strip()
                if upd_raw:
                    try:
                        dt_obj = datetime.fromisoformat(upd_raw.replace("Z", "+00:00"))
                    except Exception:
                        dt_obj = None
            if dt_obj is None:
                # Sem data: assume "agora" para não descartar sinal potencial.
                dt_obj = datetime.now(timezone.utc)
            if dt_obj.tzinfo is None:
                dt_obj = dt_obj.replace(tzinfo=timezone.utc)
            out.append((dt_obj.astimezone(timezone.utc), source, title))
    except Exception:
        return out
    return out


async def _fetch_crypto_rss_news_async(symbol: str = "ETH") -> str:
    feeds = [
        ("Cointelegraph", "https://cointelegraph.com/rss"),
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("CryptoSlate", "https://cryptoslate.com/feed/"),
    ]
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=2)
    sym = str(symbol or "ETH").upper().strip()
    keywords = {sym, "ETH", "ETHEREUM", "SEC", "ETF", "HACK"}
    timeout = aiohttp.ClientTimeout(total=3.0)
    headers = {"User-Agent": "AuricBot/1.0 (+rss-aggregator)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        tasks = [
            _fetch_rss_feed_entries(session, source=src, url=url)
            for src, url in feeds
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    entries: list[tuple[datetime, str, str]] = []
    for res in results:
        if isinstance(res, Exception):
            continue
        entries.extend(res)
    entries_recent = [e for e in entries if e[0] >= cutoff]
    # Embaralha por data: mistura multi-fonte e ordena por recência para priorizar contexto atual.
    entries_recent.sort(key=lambda x: x[0], reverse=True)
    relevant = [e for e in entries_recent if any(k in e[2].upper() for k in keywords)]
    chosen = (relevant[:5] if relevant else entries_recent[:3])
    if not chosen:
        return "Sem notícias de impacto no momento"
    return "\n".join([f"- [{src}]: {title}" for _, src, title in chosen])


def fetch_crypto_rss_news(symbol: str = "ETH") -> str:
    """
    Agregador RSS robusto (Cointelegraph, CoinDesk, CryptoSlate) com fetch assíncrono em paralelo.
    """
    print("📰 [RSS NEWS] Lendo cabeçalhos globais para análise...", flush=True)
    try:
        return asyncio.run(_fetch_crypto_rss_news_async(symbol))
    except Exception as e_rss:  # noqa: BLE001
        print(f"⚠️ [RSS NEWS] Falha no agregador RSS ({e_rss}).", flush=True)
        return "Sem notícias de impacto no momento"


async def _loop_websocket_tempo_real(args: argparse.Namespace) -> None:
    """
    Engine orientado por eventos:
    - escuta WebSocket 1m Futures (multiplex na URL: ethusdc@kline_1m + ethusdc@bookTicker)
    - dispara ciclo em fechamento da vela OU pico de volume intra-vela
    - mantém o socket vivo sem bloquear durante ML/Hub/Brain (to_thread)
    - `while True` + backoff: quedas de WS não derrubam o processo; durante o sleep
      outras coroutines (manual, métricas, auditoria) continuam a correr.
    """
    del args  # mantido por compat CLI; loop é orientado por eventos WS.
    ultima_vela_fechada_ts: int | None = None
    volume_ultima_vela_fechada: float | None = None

    global _risk_fraction_cfg, _alavancagem_cfg, _trailing_callback_cfg, _trailing_activation_mult_cfg
    global _ultimo_preco_ws, _best_bid_ws, _best_ask_ws, _ultimo_tick_ws_ts, _ws_force_restart_requested
    global _ultimo_rsi_14, _ultimo_adx_14
    global _gatilho_ciclo_lock, _ultimo_gatilho_ciclo_mono, ultimo_minuto_processado
    if _gatilho_ciclo_lock is None:
        raise RuntimeError("_gatilho_ciclo_lock ausente: inicializar em main() antes do loop WS.")
    execucao_lock = _gatilho_ciclo_lock

    while True:
        try:
            print(f"\n🛰️ [WS] Ligando stream: {WS_FUTURES_STREAM}")
            async with websockets.connect(WS_FUTURES_STREAM, ping_interval=20, ping_timeout=20) as ws:
                print(
                    f"\n✅ [WS] Conectado. Motor REST={_SYMBOL_REST} | multiplex na URL (produção)."
                )
                _ultimo_tick_ws_ts = time.monotonic()
                _last_heartbeat_print = time.monotonic()
                global _watchdog_relogio_task
                if _watchdog_relogio_task is None or _watchdog_relogio_task.done():
                    _watchdog_relogio_task = asyncio.create_task(_watchdog_relogio())
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except TimeoutError:
                        if _ws_force_restart_requested:
                            _ws_force_restart_requested = False
                            raise RuntimeError("ws.restart solicitado pelo watchdog")
                        now_hb = time.monotonic()
                        if now_hb - _last_heartbeat_print >= 10.0:
                            print("\n💓 Bot Operando...", flush=True)
                            _last_heartbeat_print = now_hb
                        sem_dados_s = now_hb - float(_ultimo_tick_ws_ts or 0.0)
                        if sem_dados_s > 30.0:
                            raise RuntimeError("ws.restart automático: stream sem dados >30s")
                        continue
                    now_loop = time.monotonic()
                    if now_loop - _last_heartbeat_print >= 10.0:
                        print("\n💓 Bot Operando...", flush=True)
                        _last_heartbeat_print = now_loop
                    if _ws_force_restart_requested:
                        _ws_force_restart_requested = False
                        raise RuntimeError("ws.restart solicitado pelo watchdog")
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if isinstance(msg, dict) and "result" in msg:
                        continue
                    if not isinstance(msg, dict):
                        continue

                    try:
                        # Multiplex Binance: envelope {"stream": "...", "data": {...}}; payload em data.
                        data = msg.get("data", msg)
                        if not isinstance(data, dict):
                            continue
                        e = data.get("e")
                        if e == "bookTicker":
                            try:
                                bid_bt = float(data.get("b") or 0.0)
                                ask_bt = float(data.get("a") or 0.0)
                                if bid_bt > 0:
                                    _best_bid_ws = bid_bt
                                    _ultimo_preco_ws = bid_bt
                                if ask_bt > 0:
                                    _best_ask_ws = ask_bt
                                if bid_bt <= 0 and ask_bt > 0:
                                    _ultimo_preco_ws = ask_bt
                            except Exception:
                                pass
                            # Imprime apenas o preço atual na mesma linha para não gerar flood
                            try:
                                print(
                                    f"\rPreço Atual: {data['b']} USDC | {time.strftime('%H:%M:%S')}",
                                    end="",
                                    flush=True,
                                )
                            except Exception:
                                pass
                            _ultimo_tick_ws_ts = time.monotonic()
                            continue
                        if e != "kline":
                            _ultimo_tick_ws_ts = time.monotonic()
                            continue
                        if str(data.get("s") or "").upper() != "ETHUSDC":
                            _ultimo_tick_ws_ts = time.monotonic()
                            continue
                        k = data.get("k")
                        if not isinstance(k, dict):
                            _ultimo_tick_ws_ts = time.monotonic()
                            continue
                        _ultimo_tick_ws_ts = time.monotonic()
                        preco = float(k.get("c") or 0.0)
                        if preco > 0:
                            _ultimo_preco_ws = preco
                        try:
                            is_kline_closed = bool(k.get("x"))
                            vol_atual = float(k.get("v") or 0.0)
                            open_time = int(k.get("t") or 0)
                            if is_kline_closed:
                                ultimo_minuto_processado = datetime.now(timezone.utc).minute
                                try:
                                    await asyncio.to_thread(
                                        _sincronizar_rsi_adx_globais_de_snap_ws
                                    )
                                except Exception as e_snap:  # noqa: BLE001
                                    print(
                                        f"\n🚨 ERRO CRÍTICO FATAL NO CICLO (sync RSI/ADX WS): {e_snap}",
                                        flush=True,
                                    )
                                    traceback.print_exc()
                                print(
                                    f"\n✅ [VELA FECHADA] ETHUSDC | RSI: {_ultimo_rsi_14:.1f} | "
                                    f"ADX: {_ultimo_adx_14:.1f}"
                                )

                            trigger: str | None = None
                            pending_close_ack: tuple[int, float] | None = None
                            if is_kline_closed and open_time != ultima_vela_fechada_ts:
                                trigger = "CLOSE_1M"
                                pending_close_ack = (open_time, vol_atual)
                            elif volume_ultima_vela_fechada and volume_ultima_vela_fechada > 0:
                                fator = 1.0 + float(indicators.VOLUME_SPIKE_FRACAO_1M)
                                if vol_atual >= fator * volume_ultima_vela_fechada:
                                    trigger = "VOLUME_SPIKE_INTRA_1M"

                            if not trigger:
                                continue

                            _metric_inc("triggers_total", 1)
                            now = time.monotonic()
                            if now - _ultimo_gatilho_ciclo_mono < TRIGGER_COOLDOWN_S:
                                _metric_inc("ignored_cooldown", 1)
                                restante = TRIGGER_COOLDOWN_S - (now - _ultimo_gatilho_ciclo_mono)
                                print(
                                    f"\n[WS] {trigger} detetado, mas bloqueado por cooldown de "
                                    f"{TRIGGER_COOLDOWN_S:.0f}s (restam {restante:.1f}s)."
                                )
                                continue

                            if execucao_lock.locked():
                                _metric_inc("triggers_ignored_lock", 1)
                                print(
                                    f"\n⏳ [WS] Trigger={trigger}, mas bloqueado por lock de execução única "
                                    "(ciclo em andamento)."
                                )
                                continue

                            cfg_dyn = await _obter_configuracoes_dinamicas()
                            _risk_fraction_cfg = float(
                                cfg_dyn.get("risk_fraction", RISK_FRACTION_PADRAO)
                            )
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
                                "\n[WS] Config dinâmica carregada: "
                                f"risk={_risk_fraction_cfg*100:.1f}% do saldo USDC (margem) por trade | "
                                f"lev={_alavancagem_cfg:.2f}x → notional≈saldo×risk×lev | "
                                f"trail_cb={_trailing_callback_cfg:.3f}% | "
                                f"trail_act_mult={_trailing_activation_mult_cfg:.3f}"
                            )

                            permitido = await asyncio.to_thread(verificar_permissao_operacao)
                            if not permitido:
                                print(
                                    "\n💤 [WS] Trigger recebido, mas bot em STANDBY (is_active=false)."
                                )
                                continue

                            modo = await asyncio.to_thread(obter_modo_operacao)
                            print(
                                f"\n🚀 [WS] Trigger={trigger} | modo={modo} | "
                                "iniciando ciclo ML+Hub+Claude (rodar_ciclo em worker thread)..."
                            )
                            async with execucao_lock:
                                _ultimo_gatilho_ciclo_mono = time.monotonic()
                                try:
                                    await _executar_ciclo_assincrono(modo, trigger)
                                    if pending_close_ack is not None:
                                        ultima_vela_fechada_ts = pending_close_ack[0]
                                        volume_ultima_vela_fechada = pending_close_ack[1]
                                finally:
                                    estado = (
                                        "MODO VIGIA" if posicao_aberta else "BUSCANDO OPORTUNIDADE"
                                    )
                                    print(
                                        f"\n✅ [WS] Ciclo concluído. Estado atual: {estado}",
                                        flush=True,
                                    )
                        except Exception as e:  # noqa: BLE001
                            print(f"\n🚨 ERRO CRÍTICO FATAL NO CICLO: {e}", flush=True)
                            traceback.print_exc()
                            continue
                    except Exception as e:
                        print(f"\n[ERRO FATAL WS] Falha ao processar mensagem: {e}", flush=True)
                        traceback.print_exc()
                        continue
        except Exception as e:  # noqa: BLE001
            # Auto-reconnect: não propaga — outras tasks (manual, métricas, auditoria) continuam.
            print(
                f"\n⚠️ [WS] Stream Binance Futures caiu ou erro ({type(e).__name__}: {e}). "
                f"Auto-reconnect após {WS_RECONNECT_DELAY_S:.0f}s...",
                flush=True,
            )
            traceback.print_exc()
            await asyncio.sleep(WS_RECONNECT_DELAY_S)


def _validar_preflight_futures_usdc() -> None:
    print("[DEBUG REST] Preflight: criar_exchange_binance()...", flush=True)
    ex = executor_futures.criar_exchange_binance()
    print("[DEBUG REST] Preflight: criar_exchange_binance() concluído.", flush=True)

    print("[DEBUG REST] Preflight: fetch_balance(params={type: future})...", flush=True)
    bal = ex.fetch_balance(params={"type": "future"})
    print("[DEBUG REST] Preflight: fetch_balance concluído.", flush=True)
    free = bal.get("free") if isinstance(bal, dict) else {}
    usdc_free = float((free or {}).get("USDC") or 0.0)
    if usdc_free <= 0:
        raise RuntimeError("Preflight falhou: sem saldo USDC livre em Futures.")

    sym = executor_futures._resolver_simbolo_perp(ex, SYMBOL_TRADE)
    print(
        f"[DEBUG REST] A solicitar alteração de margem (set_margin_mode ISOLATED) em {sym}...",
        flush=True,
    )
    try:
        ex.set_margin_mode("ISOLATED", sym)
        print("[DEBUG REST] set_margin_mode concluído (sem exceção).", flush=True)
    except ccxt.ExchangeError as e:
        print(
            f"[DEBUG REST] set_margin_mode retornou ExchangeError (pode ser esperado): {e}",
            flush=True,
        )
    print(f"[DEBUG REST] A solicitar alteração de alavancagem (set_leverage 6x) em {sym}...", flush=True)
    try:
        ex.set_leverage(6, sym)
        print("[DEBUG REST] set_leverage concluído (sem exceção).", flush=True)
    except ccxt.BaseError as e:
        print(f"[DEBUG REST] set_leverage falhou: {e}", flush=True)
        raise

    print(f"✅ [PREFLIGHT] Futures USDC ok | saldo livre={usdc_free:.4f} | leverage=6x confirmado.")


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
        "TP/SL 1.02 / 0.994.\n"
    )
    print(f"🚀 [AURIC-USDC] Iniciando motor para mercado USDC-Margined: {_SYMBOL_REST}.")
    await asyncio.to_thread(_validar_preflight_futures_usdc)

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
    global _gatilho_ciclo_lock
    _gatilho_ciclo_lock = asyncio.Lock()
    manual_listener_task = asyncio.create_task(_escutar_comandos_manuais())
    metrics_task = asyncio.create_task(_metrics_reporter_task(start_monotonic))
    outcome_task = asyncio.create_task(_auditoria_outcome_engine_task())
    try:
        await _loop_websocket_tempo_real(args)
    finally:
        manual_listener_task.cancel()
        outcome_task.cancel()
        metrics_task.cancel()
        global _watchdog_relogio_task
        if _watchdog_relogio_task is not None:
            _watchdog_relogio_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await manual_listener_task
        with contextlib.suppress(asyncio.CancelledError):
            await outcome_task
        with contextlib.suppress(asyncio.CancelledError):
            await metrics_task
        if _watchdog_relogio_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await _watchdog_relogio_task


if __name__ == "__main__":
    asyncio.run(main())
