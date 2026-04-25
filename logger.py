from __future__ import annotations

import os
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv
from supabase import create_client, Client

# Carrega as chaves do .env
load_dotenv()

URL_SUPABASE = os.environ.get("SUPABASE_URL")
CHAVE_SUPABASE = os.environ.get("SUPABASE_KEY")

# Inicializa o cliente do Supabase
if URL_SUPABASE and CHAVE_SUPABASE:
    supabase: Client = create_client(URL_SUPABASE, CHAVE_SUPABASE)
else:
    supabase = None
    print("⚠️ Chaves do Supabase não encontradas no .env")

# Nomes canónicos Supabase (sincronia com migrações / dashboard).
TABELA_LOGS = "logs"
TABELA_TRADES = "trade_logs"
TABELA_WALLET_STATUS = "wallet_status"
TABELA_CONFIG = "config"
COLUNA_USDT_BALANCE = "usdt_balance"
COLUNA_USDC_BALANCE = "usdc_balance"

# Features auxiliares para treino (setadas por ciclo no main; fallback None).
_FEATURES_LOG_DEFAULTS: dict[str, Any] = {
    "dist_ema200_pct": None,
    "spread_atual": None,
    "book_imbalance": None,
    "hora_do_dia": None,
    "atr_14": None,
    "funding_rate": None,
    "long_short_ratio": None,
    "whale_flow_score": None,
    "social_sentiment_score": None,
    "funnel_stage": None,
    "funnel_abort_reason": None,
    "ml_prob_base": None,
    "ml_prob_calibrated": None,
    "llava_veto": None,
}


def configurar_features_log_ciclo(
    *,
    dist_ema200_pct: float | None = None,
    spread_atual: float | None = None,
    book_imbalance: float | None = None,
    hora_do_dia: int | None = None,
    atr_14: float | None = None,
    funding_rate: float | None = None,
    long_short_ratio: float | None = None,
    whale_flow_score: float | None = None,
    social_sentiment_score: float | None = None,
    funnel_stage: str | None = None,
    funnel_abort_reason: str | None = None,
    ml_prob_base: float | None = None,
    ml_prob_calibrated: float | None = None,
    llava_veto: bool | None = None,
) -> None:
    """Atualiza defaults de features para os próximos `registrar_log_trade`."""
    global _FEATURES_LOG_DEFAULTS
    _FEATURES_LOG_DEFAULTS = {
        "dist_ema200_pct": dist_ema200_pct,
        "spread_atual": spread_atual,
        "book_imbalance": book_imbalance,
        "hora_do_dia": hora_do_dia,
        "atr_14": atr_14,
        "funding_rate": funding_rate,
        "long_short_ratio": long_short_ratio,
        "whale_flow_score": whale_flow_score,
        "social_sentiment_score": social_sentiment_score,
        "funnel_stage": funnel_stage,
        "funnel_abort_reason": funnel_abort_reason,
        "ml_prob_base": ml_prob_base,
        "ml_prob_calibrated": ml_prob_calibrated,
        "llava_veto": llava_veto,
    }


def persistir_whale_flow_score(
    score: float,
    *,
    row_id: int = 1,
    social_sentiment_score: float | None = None,
) -> None:
    """Upsert de `wallet_status.whale_flow_score` + `social_sentiment_score` para dashboard."""
    if not supabase:
        return
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "id": row_id,
        "whale_flow_score": float(score),
        "updated_at": now,
    }
    if social_sentiment_score is not None:
        payload["social_sentiment_score"] = float(social_sentiment_score)
    try:
        supabase.table(TABELA_WALLET_STATUS).upsert(payload, on_conflict="id").execute()
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ persistir_whale_flow_score ({TABELA_WALLET_STATUS}): {e}")


def obter_contexto_ultima_abertura(par_moeda: str = "ETH/USDC") -> dict[str, Any]:
    """Último log de abertura LONG/SHORT para atribuição de alpha no fecho."""
    if not supabase:
        return {}
    acoes_abertura = (
        "COMPRA_LONG_LIMIT",
        "COMPRA_LONG_MARKET",
        "COMPRA_LONG",
        "ABRE_SHORT_LIMIT",
        "ABRE_SHORT_MARKET",
        "ABRE_SHORT",
        "RECON_EMERGENCY_LONG",
        "RECON_EMERGENCY_SHORT",
    )
    for sym in _variants_symbolo_trade(par_moeda):
        try:
            res = (
                supabase.table(TABELA_LOGS)
                .select("id, par_moeda, probabilidade_ml, acao_tomada, contexto_raw, created_at")
                .eq("par_moeda", sym)
                .in_("acao_tomada", list(acoes_abertura))
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            if rows:
                row = rows[0]
                return row if isinstance(row, dict) else {}
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ obter_contexto_ultima_abertura ({sym}): {e}")
    return {}


def obter_bot_ativo() -> bool:
    """
    Pedágio do maestro: `public.bot_control.is_active` onde `id = 1`.
    Controlado pelo dashboard. Se a tabela não existir ou houver erro → True (o maestro continua).
    """
    if not supabase:
        return True
    try:
        res = (
            supabase.table("bot_control")
            .select("is_active")
            .eq("id", 1)
            .maybe_single()
            .execute()
        )
        if res is None:
            return True
        row = res.data
        if isinstance(row, dict):
            return bool(row.get("is_active", True))
        return True
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ obter_bot_ativo (Supabase): {e}")
        return True


def obter_preco_entrada_ultima_compra(par_moeda: str = "ETH/USDC") -> Optional[float]:
    """Compat: última entrada Long (COMPRA_LONG_LIMIT / legacy COMPRA_LONG_MARKET, etc.)."""
    p, _ = obter_preco_entrada_ultima_posicao(par_moeda)
    return p


def obter_preco_entrada_ultima_posicao(
    par_moeda: str = "ETH/USDC",
) -> tuple[Optional[float], str]:
    """
    Último log de abertura (Long ou Short), por id desc.
    Devolve (preco_ref, 'LONG'|'SHORT'). Sem linha → (None, 'LONG').
    """
    if not supabase:
        return None, "LONG"
    entradas = (
        "COMPRA_MARKET",
        "COMPRA_LONG_MARKET",
        "COMPRA_LONG_LIMIT",
        "COMPRA_LONG",
        "ABRE_SHORT_MARKET",
        "ABRE_SHORT_LIMIT",
        "ABRE_SHORT",
        "RECON_EMERGENCY_LONG",
        "RECON_EMERGENCY_SHORT",
    )
    try:
        res = (
            supabase.table(TABELA_LOGS)
            .select("preco_atual, acao_tomada")
            .eq("par_moeda", par_moeda)
            .in_("acao_tomada", list(entradas))
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        if not res.data:
            return None, "LONG"
        row = res.data[0]
        ac = (row.get("acao_tomada") or "").strip()
        lado = (
            "SHORT"
            if ac
            in (
                "ABRE_SHORT_MARKET",
                "ABRE_SHORT_LIMIT",
                "ABRE_SHORT",
                "RECON_EMERGENCY_SHORT",
            )
            else "LONG"
        )
        return float(row["preco_atual"]), lado
    except Exception as e:
        print(f"⚠️ Erro ao ler entrada no Supabase: {e}")
        return None, "LONG"


def _variants_symbolo_trade(par_moeda: str) -> list[str]:
    p = (par_moeda or "").strip() or "ETH/USDC"
    xs = {p, p.replace(":USDC", ""), p.replace("/", "").replace(":USDC", "")}
    return [x for x in xs if x]


def atualizar_qty_left_ultimo_trade(par_moeda: str, qty_left: float) -> None:
    """
    Atualiza `qty_left` na linha mais recente de `public.trade_logs` para o símbolo
    (abertura mais recente). Falha de schema/RLS apenas regista aviso.
    """
    if not supabase:
        return
    qv = float(qty_left)
    if qv < 0:
        return
    payload: dict[str, Any] = {"qty_left": qv}
    for sym in _variants_symbolo_trade(par_moeda):
        try:
            res = (
                supabase.table(TABELA_TRADES)
                .select("id")
                .eq("symbol", sym)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            if not rows:
                continue
            tid = rows[0].get("id")
            if tid is None:
                continue
            supabase.table(TABELA_TRADES).update(payload).eq("id", tid).execute()
            print(
                f"💾 [{TABELA_TRADES}] qty_left={qv:g} actualizado (id={tid}, symbol={sym})."
            )
            return
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ atualizar_qty_left_ultimo_trade ({sym}): {e}")
    print(
        f"⚠️ atualizar_qty_left_ultimo_trade: sem linha em {TABELA_TRADES} para {par_moeda!r} — "
        "qty_left não actualizado (insira trade ou migre schema)."
    )


def atualizar_ultimo_trade_campos(par_moeda: str, campos: dict[str, Any]) -> None:
    """
    UPDATE na linha mais recente de `public.trade_logs` para o símbolo (ex.: `partial_roi`,
    `final_roi`, `exit_type`). Chaves com valor `None` são omitidas do payload.
    """
    if not supabase:
        return
    payload = {k: v for k, v in campos.items() if v is not None}
    if not payload:
        return
    for sym in _variants_symbolo_trade(par_moeda):
        try:
            res = (
                supabase.table(TABELA_TRADES)
                .select("id")
                .eq("symbol", sym)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            if not rows:
                continue
            tid = rows[0].get("id")
            if tid is None:
                continue
            supabase.table(TABELA_TRADES).update(payload).eq("id", tid).execute()
            keys = ", ".join(f"{k}={payload[k]!r}" for k in sorted(payload))
            print(f"💾 [{TABELA_TRADES}] {keys} (id={tid}, symbol={sym}).")
            return
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ atualizar_ultimo_trade_campos ({sym}): {e}")
    print(
        f"⚠️ atualizar_ultimo_trade_campos: sem linha em {TABELA_TRADES} para {par_moeda!r} — "
        "campos não actualizados."
    )


def atualizar_ultimo_trade_mood_scores(
    par_moeda: str,
    *,
    whale_flow_score: float | None,
    social_sentiment_score: float | None,
) -> None:
    """Atualiza scores de humor na última linha de `trade_logs` (quando existir)."""
    payload: dict[str, Any] = {}
    if whale_flow_score is not None:
        payload["whale_flow_score"] = float(whale_flow_score)
    if social_sentiment_score is not None:
        payload["social_sentiment_score"] = float(social_sentiment_score)
    if payload:
        atualizar_ultimo_trade_campos(par_moeda, payload)


def persistir_saldo_usdt(saldo: float, *, row_id: int = 1) -> None:
    """
    Grava o saldo USDC de Futures (o mesmo valor de
    `exchange.fetch_balance(params={'type': 'future'})['total']['USDC']`).

    1) **wallet_status** (`public.wallet_status`, linha `id=1`): **upsert** na coluna
       `usdt_balance` + `updated_at`. No PostgREST isto corresponde a
       ``INSERT ... ON CONFLICT (id) DO UPDATE`` — cria a linha se não existir,
       ou atualiza se já existir.

    2) **config** (opcional): **UPDATE** em `public.config` (`balance_usdt`, `id=1`)
       quando a coluna existir, para dashboards que ainda leem `config`.
    """
    if not supabase:
        return

    now = datetime.now(timezone.utc).isoformat()
    v = float(saldo)
    payload = {
        "id": row_id,
        COLUNA_USDC_BALANCE: v,
        COLUNA_USDT_BALANCE: v,
        "updated_at": now,
    }
    try:
        supabase.table(TABELA_WALLET_STATUS).upsert(
            payload,
            on_conflict="id",
        ).execute()
        print(
            f"💰 [WALLET] Supabase {TABELA_WALLET_STATUS} upsert: "
            f"{COLUNA_USDC_BALANCE}={v:.4f} USDC (id={row_id})"
        )
    except Exception as e:
        print(f"⚠️ persistir_saldo_usdt — {TABELA_WALLET_STATUS} (Supabase): {e}")
        return

    try:
        supabase.table(TABELA_CONFIG).update(
            {"balance_usdt": v, "updated_at": now}
        ).eq("id", row_id).execute()
    except Exception:
        # Sem coluna balance_usdt, RLS ou tabela — ignorar (fonte canónica é wallet_status).
        pass


def registrar_log_trade(
    par_moeda: str,
    preco: float,
    prob_ml: float,
    sentimento: str,
    acao: str,
    justificativa: str,
    *,
    lado_ordem: str | None = None,
    contexto_raw: str | None = None,
    justificativa_ia: str | None = None,
    noticias_agregadas: str | None = None,
    commission: float | None = None,
    is_maker: bool | None = None,
    dist_ema200_pct: float | None = None,
    spread_atual: float | None = None,
    book_imbalance: float | None = None,
    hora_do_dia: int | None = None,
    atr_14: float | None = None,
    funding_rate: float | None = None,
    long_short_ratio: float | None = None,
    whale_flow_score: float | None = None,
    social_sentiment_score: float | None = None,
    funnel_stage: str | None = None,
    funnel_abort_reason: str | None = None,
    ml_prob_base: float | None = None,
    ml_prob_calibrated: float | None = None,
    llava_veto: bool | None = None,
    rsi_14: float | None = None,
    adx_14: float | None = None,
):
    """
    INSERT na tabela `logs` no Supabase.

    Colunas enviadas: `par_moeda` (texto, ex. ETH/USDC), `preco_atual`, `probabilidade_ml`,
    `sentimento_ia`, `veredito_ia` (espelho do veredito para o dashboard),
    `acao_tomada`, `justificativa`, `contexto_raw`,
    opcionais `justificativa_ia`, `noticias_agregadas` (dashboard).
    Não enviar o par em `ativo` — na base real `ativo` é boolean (ex.: posição/sessão ativa), não o símbolo.

    lado_ordem: 'LONG' | 'SHORT' — identifica tentativa de compra (long) vs abertura short (dashboard).
    contexto_raw: bloco JSON de indicadores TA + texto do Intelligence Hub (`formatar_log_contexto_raw`).
    """
    if lado_ordem:
        justificativa = f"[{lado_ordem}] {justificativa}"

    par = (par_moeda or "").strip() or "ETH/USDC"

    dados_log: dict[str, Any] = {
        "par_moeda": par,
        "preco_atual": preco,
        "probabilidade_ml": prob_ml,
        "sentimento_ia": sentimento,
        "veredito_ia": sentimento,
        "acao_tomada": acao,
        "justificativa": justificativa,
        "contexto_raw": contexto_raw,
        # Colunas de features para dataset (enviar sempre; None quando indisponível).
        "dist_ema200_pct": (
            dist_ema200_pct if dist_ema200_pct is not None else _FEATURES_LOG_DEFAULTS["dist_ema200_pct"]
        ),
        "spread_atual": (
            spread_atual if spread_atual is not None else _FEATURES_LOG_DEFAULTS["spread_atual"]
        ),
        "book_imbalance": (
            book_imbalance if book_imbalance is not None else _FEATURES_LOG_DEFAULTS["book_imbalance"]
        ),
        "hora_do_dia": (
            hora_do_dia if hora_do_dia is not None else _FEATURES_LOG_DEFAULTS["hora_do_dia"]
        ),
        "atr_14": (
            atr_14 if atr_14 is not None else _FEATURES_LOG_DEFAULTS["atr_14"]
        ),
        "funding_rate": (
            funding_rate if funding_rate is not None else _FEATURES_LOG_DEFAULTS["funding_rate"]
        ),
        "long_short_ratio": (
            long_short_ratio
            if long_short_ratio is not None
            else _FEATURES_LOG_DEFAULTS["long_short_ratio"]
        ),
        "whale_flow_score": (
            whale_flow_score
            if whale_flow_score is not None
            else _FEATURES_LOG_DEFAULTS["whale_flow_score"]
        ),
        "social_sentiment_score": (
            social_sentiment_score
            if social_sentiment_score is not None
            else _FEATURES_LOG_DEFAULTS["social_sentiment_score"]
        ),
        "funnel_stage": (
            funnel_stage if funnel_stage is not None else _FEATURES_LOG_DEFAULTS["funnel_stage"]
        ),
        "funnel_abort_reason": (
            funnel_abort_reason
            if funnel_abort_reason is not None
            else _FEATURES_LOG_DEFAULTS["funnel_abort_reason"]
        ),
        "ml_prob_base": (
            ml_prob_base if ml_prob_base is not None else _FEATURES_LOG_DEFAULTS["ml_prob_base"]
        ),
        "ml_prob_calibrated": (
            ml_prob_calibrated
            if ml_prob_calibrated is not None
            else _FEATURES_LOG_DEFAULTS["ml_prob_calibrated"]
        ),
        "llava_veto": (
            llava_veto if llava_veto is not None else _FEATURES_LOG_DEFAULTS["llava_veto"]
        ),
    }
    if rsi_14 is not None:
        dados_log["rsi_14"] = float(rsi_14)
    if adx_14 is not None:
        dados_log["adx_14"] = float(adx_14)
    if justificativa_ia is not None:
        dados_log["justificativa_ia"] = justificativa_ia
    if noticias_agregadas is not None:
        dados_log["noticias_agregadas"] = noticias_agregadas
    if commission is not None:
        dados_log["commission"] = float(commission)
    if is_maker is not None:
        dados_log["is_maker"] = bool(is_maker)

    if not supabase:
        print(f"\n[SIMULAÇÃO] {acao} | Preço: {preco} | Justificativa: {justificativa}")
        return

    try:
        resposta = supabase.table(TABELA_LOGS).insert(dados_log).execute()
        print(f"💾 Log salvo com sucesso! ID: {resposta.data[0]['id']}")
    except Exception as e:
        print(f"❌ ERRO CRÍTICO AO SALVAR LOG: {e}", flush=True)
        print(f"❌ ERRO CRÍTICO AO SALVAR LOG — chaves do payload: {list(dados_log.keys())}", flush=True)
        traceback.print_exc()

# Teste rápido se rodar o arquivo diretamente
if __name__ == "__main__":
    registrar_log_trade("ETH/USDC", 3000.0, 0.65, "BULLISH", "TESTE_CONEXAO", "Validando integração.")