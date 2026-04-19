from __future__ import annotations

import os
from typing import Optional

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


def obter_bot_ativo() -> bool:
    """
    Lê `bot_status.is_active` (linha id=1), controlada pelo dashboard.
    Se a tabela não existir ou houver erro → True (o maestro continua a correr).
    """
    if not supabase:
        return True
    try:
        res = (
            supabase.table("bot_status")
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


def obter_preco_entrada_ultima_compra(ativo: str = "ETH/USDT") -> Optional[float]:
    """Compat: última entrada Long (legacy COMPRA_MARKET ou COMPRA_LONG_MARKET)."""
    p, _ = obter_preco_entrada_ultima_posicao(ativo)
    return p


def obter_preco_entrada_ultima_posicao(
    ativo: str = "ETH/USDT",
) -> tuple[Optional[float], str]:
    """
    Último log de abertura (Long ou Short), por id desc.
    Devolve (preco_ref, 'LONG'|'SHORT'). Sem linha → (None, 'LONG').
    """
    if not supabase:
        return None, "LONG"
    entradas = ("COMPRA_MARKET", "COMPRA_LONG_MARKET", "ABRE_SHORT_MARKET")
    try:
        res = (
            supabase.table("trade_logs")
            .select("preco_atual, acao_tomada")
            .eq("ativo", ativo)
            .in_("acao_tomada", list(entradas))
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        if not res.data:
            return None, "LONG"
        row = res.data[0]
        ac = (row.get("acao_tomada") or "").strip()
        lado = "SHORT" if ac == "ABRE_SHORT_MARKET" else "LONG"
        return float(row["preco_atual"]), lado
    except Exception as e:
        print(f"⚠️ Erro ao ler entrada no Supabase: {e}")
        return None, "LONG"


def registrar_log_trade(
    ativo: str,
    preco: float,
    prob_ml: float,
    sentimento: str,
    acao: str,
    justificativa: str,
    *,
    lado_ordem: str | None = None,
):
    """
    Envia a decisão do bot para a tabela 'trade_logs' no Supabase.
    lado_ordem: 'LONG' | 'SHORT' — identifica tentativa de compra (long) vs abertura short (dashboard).
    """
    if lado_ordem:
        justificativa = f"[{lado_ordem}] {justificativa}"

    dados_log = {
        "ativo": ativo,
        "preco_atual": preco,
        "probabilidade_ml": prob_ml,
        "sentimento_ia": sentimento,
        "acao_tomada": acao,
        "justificativa": justificativa,
    }
    
    if not supabase:
        print(f"\n[SIMULAÇÃO] {acao} | Preço: {preco} | Justificativa: {justificativa}")
        return

    try:
        # Insere na tabela 'trade_logs'
        resposta = supabase.table("trade_logs").insert(dados_log).execute()
        print(f"💾 Log salvo com sucesso! ID: {resposta.data[0]['id']}")
    except Exception as e:
        print(f"❌ Erro ao salvar no Supabase: {e}")

# Teste rápido se rodar o arquivo diretamente
if __name__ == "__main__":
    registrar_log_trade("ETH/USDT", 3000.0, 0.65, "BULLISH", "TESTE_CONEXAO", "Validando integração.")