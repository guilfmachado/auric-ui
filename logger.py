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
TABELA_WALLET_STATUS = "wallet_status"
TABELA_CONFIG = "config"
COLUNA_USDT_BALANCE = "usdt_balance"


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


def obter_preco_entrada_ultima_compra(par_moeda: str = "ETH/USDT") -> Optional[float]:
    """Compat: última entrada Long (COMPRA_LONG_LIMIT / legacy COMPRA_LONG_MARKET, etc.)."""
    p, _ = obter_preco_entrada_ultima_posicao(par_moeda)
    return p


def obter_preco_entrada_ultima_posicao(
    par_moeda: str = "ETH/USDT",
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


def persistir_saldo_usdt(saldo: float, *, row_id: int = 1) -> None:
    """
    Grava o saldo USDT **Futuros USDT-M** (o mesmo valor que
    `exchange.fetch_balance(params={'type': 'future'})['total']['USDT']`).

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
            f"{COLUNA_USDT_BALANCE}={v:.4f} USDT (id={row_id})"
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
):
    """
    INSERT na tabela `logs` no Supabase.

    Colunas enviadas: `par_moeda` (texto, ex. ETH/USDT), `preco_atual`, `probabilidade_ml`,
    `sentimento_ia`, `veredito_ia` (espelho do veredito para o dashboard),
    `acao_tomada`, `justificativa`, `contexto_raw`,
    opcionais `justificativa_ia`, `noticias_agregadas` (dashboard).
    Não enviar o par em `ativo` — na base real `ativo` é boolean (ex.: posição/sessão ativa), não o símbolo.

    lado_ordem: 'LONG' | 'SHORT' — identifica tentativa de compra (long) vs abertura short (dashboard).
    contexto_raw: bloco JSON de indicadores TA + texto do Intelligence Hub (`formatar_log_contexto_raw`).
    """
    if lado_ordem:
        justificativa = f"[{lado_ordem}] {justificativa}"

    par = (par_moeda or "").strip() or "ETH/USDT"

    dados_log: dict[str, Any] = {
        "par_moeda": par,
        "preco_atual": preco,
        "probabilidade_ml": prob_ml,
        "sentimento_ia": sentimento,
        "veredito_ia": sentimento,
        "acao_tomada": acao,
        "justificativa": justificativa,
        "contexto_raw": contexto_raw,
    }
    if justificativa_ia is not None:
        dados_log["justificativa_ia"] = justificativa_ia
    if noticias_agregadas is not None:
        dados_log["noticias_agregadas"] = noticias_agregadas

    if not supabase:
        print(f"\n[SIMULAÇÃO] {acao} | Preço: {preco} | Justificativa: {justificativa}")
        return

    try:
        resposta = supabase.table(TABELA_LOGS).insert(dados_log).execute()
        print(f"💾 Log salvo com sucesso! ID: {resposta.data[0]['id']}")
    except Exception as e:
        print(f"❌ Erro ao salvar no Supabase: {e}")

# Teste rápido se rodar o arquivo diretamente
if __name__ == "__main__":
    registrar_log_trade("ETH/USDT", 3000.0, 0.65, "BULLISH", "TESTE_CONEXAO", "Validando integração.")