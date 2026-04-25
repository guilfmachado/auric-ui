from __future__ import annotations

import os
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

# Colunas opcionais em `logs` — removidas em retry se PostgREST PGRST204 (schema desatualizado).
_LOGS_OPTIONAL_KEYS_PGRST204_RETRY: tuple[str, ...] = (
    "funnel_abort_reason",
    "llava_veto",
    "justificativa_ia",
    "ml_prob_base",
    "ml_prob_calibrated",
)


def _postgrest_error_code(err: BaseException) -> str | None:
    code = getattr(err, "code", None)
    if code:
        return str(code)
    args = getattr(err, "args", None)
    if args and isinstance(args[0], dict):
        c = args[0].get("code")
        return str(c) if c is not None else None
    return None


def _is_pgrst204_schema_cache(err: BaseException) -> bool:
    if _postgrest_error_code(err) == "PGRST204":
        return True
    msg = str(err).lower()
    return "pgrst204" in msg or "schema cache" in msg


def _insert_log_row(payload: dict[str, Any]) -> Any:
    return supabase.table(TABELA_LOGS).insert(payload).execute()


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
    variants = _variants_symbolo_trade(par_moeda)
    if not variants:
        return {}
    try:
        res = (
            supabase.table(TABELA_LOGS)
            .select("id, par_moeda, probabilidade_ml, acao_tomada, contexto_raw, created_at")
            .in_("par_moeda", variants)
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
        print(f"⚠️ obter_contexto_ultima_abertura ({par_moeda!r} variants={variants!r}): {e}")
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
    variants = _variants_symbolo_trade(par_moeda)
    if not variants:
        return None, "LONG"
    try:
        res = (
            supabase.table(TABELA_LOGS)
            .select("preco_atual, acao_tomada")
            .in_("par_moeda", variants)
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
    """
    Formas de `par_moeda` que o bot e a Binance/CCXT podem usar vs. o que está em Supabase.
    Inclui: ETH/USDC, ETH/USDC:USDC, ETHUSDC, etc. — para match em `.in_('par_moeda', ...)`.
    """
    p = (par_moeda or "").strip() or "ETH/USDC"
    xs: set[str] = {p}
    if ":" in p:
        head = p.split(":", 1)[0].strip()
        if head:
            xs.add(head)
    compact = (
        p.replace("/", "")
        .replace(":USDC", "")
        .replace(":USDT", "")
        .replace(":BUSD", "")
    )
    if compact:
        xs.add(compact)
    # ETHUSDC -> ETH/USDC e ETH/USDC:USDC
    if "/" not in p and len(p) >= 6:
        u = p.upper()
        for suf in ("USDC", "USDT", "BUSD"):
            if u.endswith(suf) and len(p) > len(suf):
                base = p[: -len(suf)]
                if base:
                    xs.add(f"{base}/{suf}")
                    xs.add(f"{base}/{suf}:{suf}")
                break
    # ETH/USDC (ou ETH/USDC:USDC já em p) — garantir forma perp CCXT
    if "/" in p:
        left, right = p.split("/", 1)
        quote = right.split(":", 1)[0].upper()
        if quote == "USDC":
            xs.add(f"{left}/USDC:USDC")
        elif quote == "USDT":
            xs.add(f"{left}/USDT:USDT")
        elif quote == "BUSD":
            xs.add(f"{left}/BUSD:BUSD")
    return sorted({x for x in xs if x})


def _buscar_id_ultimo_trade_log(par_moeda: str) -> tuple[Any, str] | None:
    """
    Última linha em `trade_logs` para qualquer variante de símbolo alinhada a `par_moeda`.
    Devolve (id, par_moeda_tal_como_na_base) ou None.
    """
    if not supabase:
        return None
    variants = _variants_symbolo_trade(par_moeda)
    if not variants:
        return None
    try:
        q = (
            supabase.table(TABELA_TRADES)
            .select("id, par_moeda")
            .in_("par_moeda", variants)
            .order("created_at", desc=True, nullsfirst=False)
            .order("id", desc=True)
            .limit(1)
        )
        res = q.execute()
        rows = res.data or []
        if not rows:
            return None
        row = rows[0]
        tid = row.get("id")
        if tid is None:
            return None
        stored = str(row.get("par_moeda") or "").strip()
        return (tid, stored or str(variants[0]))
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ _buscar_id_ultimo_trade_log ({par_moeda!r}): {e}")
        return None


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
    hit = _buscar_id_ultimo_trade_log(par_moeda)
    if not hit:
        print(
            f"⚠️ atualizar_qty_left_ultimo_trade: sem linha em {TABELA_TRADES} para "
            f"{par_moeda!r} (variantes {_variants_symbolo_trade(par_moeda)!r})."
        )
        return
    tid, par_db = hit
    try:
        supabase.table(TABELA_TRADES).update(payload).eq("id", tid).execute()
        print(
            f"💾 [{TABELA_TRADES}] qty_left={qv:g} actualizado (id={tid}, par_moeda na base={par_db!r})."
        )
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ atualizar_qty_left_ultimo_trade (id={tid}, par_moeda={par_db!r}): {e}")


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
    hit = _buscar_id_ultimo_trade_log(par_moeda)
    if not hit:
        print(
            f"⚠️ atualizar_ultimo_trade_campos: sem linha em {TABELA_TRADES} para {par_moeda!r} "
            f"(variantes {_variants_symbolo_trade(par_moeda)!r}) — campos não actualizados."
        )
        return
    tid, par_db = hit
    try:
        supabase.table(TABELA_TRADES).update(payload).eq("id", tid).execute()
        keys = ", ".join(f"{k}={payload[k]!r}" for k in sorted(payload))
        print(f"💾 [{TABELA_TRADES}] {keys} (id={tid}, par_moeda na base={par_db!r}).")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ atualizar_ultimo_trade_campos (id={tid}, par_moeda={par_db!r}): {e}")


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

    def _log_insert_success(resposta: Any) -> None:
        try:
            row0 = (resposta.data or [{}])[0]
            rid = row0.get("id", "?")
            print(f"💾 Log salvo com sucesso! ID: {rid}")
        except Exception:
            print("💾 Log salvo com sucesso!")

    payload = dict(dados_log)
    try:
        resposta = _insert_log_row(payload)
        _log_insert_success(resposta)
        return
    except Exception as e_first:  # noqa: BLE001
        if not _is_pgrst204_schema_cache(e_first):
            print(
                f"⚠️ [LOGS] Falha ao gravar log (não é PGRST204): {e_first}. Ciclo continua.",
                flush=True,
            )
            return
        for k in _LOGS_OPTIONAL_KEYS_PGRST204_RETRY:
            payload.pop(k, None)
        try:
            resposta = _insert_log_row(payload)
            _log_insert_success(resposta)
            print(
                "⚠️ [LOGS] Schema desatualizado: insert repetido sem colunas opcionais "
                f"({', '.join(_LOGS_OPTIONAL_KEYS_PGRST204_RETRY)}).",
                flush=True,
            )
            return
        except Exception as e_second:  # noqa: BLE001
            if not _is_pgrst204_schema_cache(e_second):
                print(
                    f"⚠️ [LOGS] Falha no retry após PGRST204: {e_second}. Ciclo continua.",
                    flush=True,
                )
                return
    # Segundo PGRST204: tentar só colunas básicas (compat schema antigo).
    basico: dict[str, Any] = {
        "par_moeda": par,
        "preco_atual": preco,
        "probabilidade_ml": prob_ml,
        "sentimento_ia": sentimento,
        "veredito_ia": sentimento,
        "acao_tomada": acao,
        "justificativa": justificativa,
        "contexto_raw": contexto_raw,
    }
    if noticias_agregadas is not None:
        basico["noticias_agregadas"] = noticias_agregadas
    if commission is not None:
        basico["commission"] = float(commission)
    if is_maker is not None:
        basico["is_maker"] = bool(is_maker)
    if rsi_14 is not None:
        basico["rsi_14"] = float(rsi_14)
    if adx_14 is not None:
        basico["adx_14"] = float(adx_14)
    try:
        resposta = _insert_log_row(basico)
        _log_insert_success(resposta)
        print(
            "⚠️ [LOGS] Schema muito antigo: gravado apenas bloco básico "
            "(sem features de funil / whale / etc.). Migre o Supabase.",
            flush=True,
        )
    except Exception as e_final:  # noqa: BLE001
        print(
            f"⚠️ [LOGS] Não foi possível gravar em `{TABELA_LOGS}` após fallbacks: {e_final}. "
            "Ciclo de trading continua.",
            flush=True,
        )

# Teste rápido se rodar o arquivo diretamente
if __name__ == "__main__":
    registrar_log_trade("ETH/USDC", 3000.0, 0.65, "BULLISH", "TESTE_CONEXAO", "Validando integração.")