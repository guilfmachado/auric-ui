"""
Análise de sentimento (ETH) via Replicate — por omissão Claude 4.5 Sonnet (anthropic/claude-4.5-sonnet).

Motor «Auric Final Boss»: macro-correlacional + vetores geopolítico, infra e baleias;
contexto agregado do IntelligenceHub; cruza com P(alta) do XGBoost e direção sugerida (LONG/SHORT).
Saída: JSON com esses campos ou texto veredito (BULLISH/BEARISH/VETO) convertido para o mesmo formato.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

import numpy as np
import replicate
from dotenv import load_dotenv

load_dotenv()

# Por omissão: Claude 4.5 Sonnet no Replicate. Override: REPLICATE_BRAIN_MODEL ou REPLICATE_MODEL_VERSION (slug/digest).
_DEFAULT_BRAIN_MODEL = "anthropic/claude-4.5-sonnet"
REPLICATE_MODEL = (
    os.getenv("REPLICATE_BRAIN_MODEL") or os.getenv("REPLICATE_MODEL_VERSION") or _DEFAULT_BRAIN_MODEL
).strip()

# Claude 4.5 no Replicate: manter max_tokens ≥ 1024; temperatura baixa = analista de risco, não «poeta».
_BRAIN_MAX_TOKENS = 1024
_BRAIN_TEMPERATURE = 0.3


def _pct_br_ml(prob: float) -> str:
    """P(alta) em percentagem legível (pt), ex.: 25,10%."""
    return f"{float(prob) * 100:.2f}".replace(".", ",") + "%"


def _token_configurado() -> bool:
    return bool(os.getenv("REPLICATE_API_TOKEN", "").strip())


def _avisar_token_ausente() -> None:
    if not _token_configurado():
        print(
            "[Brain] ERRO: REPLICATE_API_TOKEN está vazio ou ausente no .env. "
            "https://replicate.com/account/api-tokens",
            file=sys.stderr,
        )


_avisar_token_ausente()


def _saida_replicate_para_string(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    try:
        return "".join(output)
    except TypeError:
        return str(output)


def _parse_json_resposta(texto: str) -> dict[str, Any]:
    texto = texto.strip()
    try:
        obj = json.loads(texto)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    if texto.startswith("{") and not texto.rstrip().endswith("}"):
        try:
            obj = json.loads(texto.rstrip() + "}")
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    m = re.search(r"\{[\s\S]*\}", texto)
    if m:
        trecho = m.group(0)
        for cand in (trecho, trecho + "}" if not trecho.rstrip().endswith("}") else trecho):
            try:
                obj = json.loads(cand)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue

    raise json.JSONDecodeError("Nenhum objeto JSON válido encontrado", texto, 0)


def _dict_from_veredito_texto_brain(
    raw: str,
    *,
    direcao_sugerida: str,
) -> dict[str, Any]:
    """
    Resposta em texto (BULLISH / BEARISH / VETO + justificativa) → dict compatível com main.py.
    """
    texto = (raw or "").strip()
    if not texto or texto.upper() == "ERROR":
        return {
            "sentimento": "ERROR",
            "confianca": 0,
            "justificativa_curta": texto or "resposta vazia",
            "alerta_macro": "",
            "posicao_recomendada": "VETO",
        }

    up = texto.upper()
    sentimento = "NEUTRAL"
    for tok in ("BULLISH", "BEARISH", "VETO"):
        if re.search(rf"\b{tok}\b", up):
            sentimento = tok
            break

    if sentimento == "NEUTRAL":
        if "BULLISH" in up:
            sentimento = "BULLISH"
        elif "BEARISH" in up:
            sentimento = "BEARISH"
        elif "VETO" in up:
            sentimento = "VETO"

    if sentimento == "NEUTRAL":
        sentimento = "CAUTIOUS"

    if sentimento == "BULLISH":
        pos_rec = "LONG"
    elif sentimento == "BEARISH":
        pos_rec = "SHORT"
    else:
        pos_rec = "VETO"

    just = texto
    for tok in ("BULLISH", "BEARISH", "VETO", "CAUTIOUS"):
        m = re.search(rf"\b{tok}\b", texto, flags=re.IGNORECASE)
        if m:
            just = texto[m.end() :].strip(" \n:-—")
            break

    if not just:
        just = texto[:500]

    return {
        "sentimento": sentimento,
        "confianca": 72,
        "justificativa_curta": just[:2000] if just else texto[:2000],
        "alerta_macro": "",
        "posicao_recomendada": pos_rec,
    }


def _prompt_sistema_claude(direcao_sugerida: str) -> str:
    d = (direcao_sugerida or "LONG").strip().upper()
    if d not in ("LONG", "SHORT"):
        d = "LONG"

    bloco_direcao = f"""
SINAL TÉCNICO DO ORQUESTRADOR: recebeste um sinal técnico de {d}.
Tua prioridade é validar se há confluência real ou se o mercado está a montar uma armadilha (Bull Trap / Bear Trap).
Se houver sinais de armadilha, a ação obrigatória é VETO.
"""

    return f"""És o Auric Final Boss, um Motor de Inteligência Quantitativa e Macro-Correlacional. A tua missão é proteger o capital e identificar assimetrias de alta probabilidade.

{bloco_direcao}

LÓGICA DE ANÁLISE E DETEÇÃO DE ARMADILHAS:

O Filtro de Exaustão (RSI): Se o sinal for LONG, mas o RSI técnico apontar sobrecompra extrema (>70), e o Twitter estiver em euforia máxima, ASSUMA BULL TRAP. Ação obrigatória: VETO. Se for SHORT, com RSI sobrevendido (<30) e pânico extremo, ASSUMA BEAR TRAP. Ação obrigatória: VETO.

Vetor de Baleias (Smart Money): Movimentações de >50M USDT para Exchanges indicam intenção de despejo (pressão de venda). Se o sinal for LONG e as baleias estiverem a mover USDT para a Tether Treasury (saída do sistema), haja divergência. Ação: VETO.

Regra de Ouro (Divergência de ADX): O XGBoost pode dar 80% de chance de alta, mas se o ADX for < 20, o mercado está em acumulação lateral. Sinais direcionais em mercado lateral falham. Ação: CAUTIOUS ou VETO.

Institucional vs. Varejo: Se o Nitter/Twitter mostrar apenas ruído de retalho e contas pequenas, desvalorize. Procure catalisadores institucionais ou de infraestrutura (Vitalik, Hacks, Atualizações de Rede).

TURBO vs VETO (contexto humano na secção «Contexto Humano»): **Veto** = peso 2.0 para **bloquear** uma entrada em conflito com o XGBoost. **Turbo** (TURBO LONG / TURBO SHORT) = peso 2.0 para **forçar/reforçar** uma entrada **mesmo** com ML neutro (zona ~45–55% P(alta)), desde que não haja armadilha material — segue as instruções do rodapé dessa secção. Se activaste Turbo, em `justificativa_curta` inclui obrigatoriamente **[TURBO MODE ATIVO]**.

Responde APENAS com UM objeto JSON válido (sem markdown, sem ```, sem texto antes ou depois).
Chaves OBRIGATÓRIAS e APENAS estas:
{{
  "sentimento": "BULLISH" | "BEARISH" | "NEUTRAL" | "CAUTIOUS",
  "confianca": <inteiro 0 a 100>,
  "justificativa_curta": "<uma frase; foco na correlação com o sinal {d}; se houver VETO, explique explicitamente a armadilha (Bull Trap/Bear Trap, ADX lateral ou divergência de baleias); se o contexto humano for TURBO, inclui **[TURBO MODE ATIVO]** nesta frase>",
  "alerta_macro": "<síntese de risco geopolítico, infra ou baleias; ou nenhum se marginal>",
  "posicao_recomendada": "LONG" | "SHORT" | "VETO"
}}

«posicao_recomendada» deve alinhar com o sinal {d} quando o contexto confirmar a tese; caso contrário VETO. «sentimento» é o tom global do mercado lido no contexto."""


def _montar_prompt_completo_claude(
    bloco_dados: str,
    *,
    ref_xgboost: str | None = None,
    direcao_sugerida: str = "LONG",
    bloco_tecnico_prioritario: str | None = None,
    micro_estrutura_posicionamento: dict[str, Any] | None = None,
    user_market_observation: str | None = None,
    is_turbo: bool = False,
) -> str:
    base = _prompt_sistema_claude(direcao_sugerida)
    if bloco_tecnico_prioritario and str(bloco_tecnico_prioritario).strip():
        base += (
            "\n\n=== ANÁLISE TÉCNICA (leitura obrigatória antes das notícias) ===\n\n"
            + str(bloco_tecnico_prioritario).strip()
        )
    bloco = (
        base
        + "\n\n=== CONTEXTO AGREGADO (NÃO INVENTES FACTOS FORA DESTE BLOCO) ===\n\n"
        + bloco_dados
    )
    if ref_xgboost:
        bloco += "\n\n" + ref_xgboost
    if micro_estrutura_posicionamento:
        fr = micro_estrutura_posicionamento.get("funding_rate")
        lsr = micro_estrutura_posicionamento.get("long_short_ratio")
        fr_txt = "None" if fr is None else f"{float(fr):.8f}"
        lsr_txt = "None" if lsr is None else f"{float(lsr):.6f}"
        bloco += (
            "\n\n📈 MICRO-ESTRUTURA E POSICIONAMENTO\n"
            f"- funding_rate: {fr_txt}\n"
            f"- long_short_ratio: {lsr_txt}\n"
            "Atenção: Use o Funding Rate e o Long/Short Ratio como indicadores contrários. "
            "Se o Funding Rate estiver excessivamente positivo e o Long/Short Ratio for alto, "
            "significa que o varejo está super-alavancado em Longs. Use isso para aumentar o peso "
            "de um VETO ou procurar divergências para SHORT. Se os dados vierem como None, ignore "
            "esta métrica no ciclo atual."
        )
    bloco += "\n\n=== [USER_SENTIMENT_CONTEXT] — Contexto Humano ===\n"
    obs = (user_market_observation or "").strip()
    if obs:
        bloco += (
            f"\nIntegra o bloco abaixo na decisão de risco (Supabase e/ou observação de sessão).\n\n{obs}\n"
        )
    else:
        bloco += "\n(Nenhuma observação humana ativa para este ciclo.)\n"
    if is_turbo:
        bloco += (
            "\n**MODO TURBO (ciclo actual):** o orquestrador **forçou** esta chamada mesmo com XGBoost "
            "na zona neutra (se aplicável). Prioriza o comando **TURBO** na secção acima com peso 2.0 "
            "para **forçar/reforçar** entrada alinhada; em `justificativa_curta` inclui **[TURBO MODE ATIVO]**.\n"
        )
    bloco += (
        "\n\nResponde agora somente com o objeto JSON pedido acima "
        "(cinco chaves, incluindo posicao_recomendada)."
    )
    return bloco


def montar_bloco_tecnico_final_boss(snapshot: dict[str, Any]) -> str:
    """
    Missão «Final Boss» + detalhe de indicadores (regras em indicators).
    """
    import indicators

    adx_raw = snapshot.get("adx_14")
    adx_str = "N/A"
    if adx_raw is not None:
        try:
            x = float(adx_raw)
            if not (isinstance(x, float) and np.isnan(x)):
                adx_str = f"{x:.2f}"
        except (TypeError, ValueError):
            pass

    vies = snapshot.get("vies_vwap", "INDEFINIDO")
    acima_ou_abaixo = indicators.texto_preco_vs_vwap(str(vies))
    estado_macd = str(snapshot.get("macd_estado") or "Indefinido")
    status_bollinger = indicators.descrever_status_bollinger_para_prompt(snapshot)
    confluencia = snapshot.get("confluencia")
    if isinstance(confluencia, dict) and confluencia:
        bloco_conf = "\n\n" + indicators.formatar_confluencia_para_llm(confluencia)
    else:
        bloco_conf = ""

    missao = f"""Você é o estrategista chefe. Além das notícias e do ML, considere estes dados técnicos:

ADX: {adx_str} (Indica força da tendência)

Preço vs VWAP: {acima_ou_abaixo} (Viés institucional)

MACD: {estado_macd} (Mede o momentum da tendência)

Bollinger: {status_bollinger} (Indica se o preço está esticado ou comprimido)

Sua Missão: Se o ML der 83% de alta, mas o ADX estiver lateral e o MACD mostrar Cruzamento de Baixa, você deve dar VETO por falta de confluência. Só confirme o trade se houver CONFLUÊNCIA entre técnico e notícias."""

    detalhe = indicators.formatar_bloco_indicadores_para_llm(snapshot)
    return f"{missao}\n\n{detalhe}{bloco_conf}"


def analisar_sentimento_mercado(
    contexto_agregado: str,
    *,
    prob_ml: float | None = None,
    limiar_ml: float | None = None,
    limiar_ml_short: float | None = None,
    direcao_sugerida: str = "LONG",
    verbose: bool = True,
    bloco_indicadores: str | None = None,
    bloco_tecnico_prioritario: str | None = None,
    micro_estrutura_posicionamento: dict[str, Any] | None = None,
    user_market_observation: str | None = None,
    is_turbo: bool = False,
) -> dict[str, Any]:
    """
    Envia o contexto do IntelligenceHub (+ XGBoost + direção LONG/SHORT) ao Replicate (Claude 4.5 por omissão).
    `replicate.run` é síncrono; a saída pode ser string ou iterável de strings — normalizamos com `_saida_replicate_para_string`.
    """
    if not _token_configurado():
        _avisar_token_ausente()
        return {
            "sentimento": "ERROR",
            "confianca": 0,
            "justificativa_curta": "REPLICATE_API_TOKEN não configurado no .env.",
            "alerta_macro": "",
            "posicao_recomendada": "VETO",
        }

    d = (direcao_sugerida or "LONG").strip().upper()
    if d not in ("LONG", "SHORT"):
        d = "LONG"

    ref_xg: str | None = None
    if prob_ml is not None:
        lim_txt = f"{limiar_ml:.4f}" if limiar_ml is not None else "N/A"
        lim_s = limiar_ml_short if limiar_ml_short is not None else 0.45
        lim_s_txt = f"{float(lim_s):.4f}"
        p = float(prob_ml)
        pct_br = _pct_br_ml(p)
        ref_xg = (
            "=== SINAL TÉCNICO (XGBoost — referência do orquestrador neste ciclo) ===\n"
            f"P(alta próximo horizonte): {pct_br} (valor em [0,1]: {p:.6f}; também {p:.4f}).\n"
            f"Limiar long do maestro (ativa Hub): {lim_txt}; zona short: P(alta) ≤ {lim_s_txt}. "
            f"Direção sugerida neste ciclo: {d}.\n"
            "Obrigatório: cruza esta percentagem com as manchetes e texto da secção "
            "«=== CONTEXTO AGREGADO» (notícias / Reddit / Twitter) acima; divergência crítica "
            "→ posicao_recomendada VETO."
        )

    ta_block = bloco_tecnico_prioritario
    if not (ta_block and str(ta_block).strip()) and bloco_indicadores and str(
        bloco_indicadores
    ).strip():
        ta_block = str(bloco_indicadores).strip()

    prompt_completo = _montar_prompt_completo_claude(
        contexto_agregado,
        ref_xgboost=ref_xg,
        direcao_sugerida=d,
        bloco_tecnico_prioritario=ta_block,
        micro_estrutura_posicionamento=micro_estrutura_posicionamento,
        user_market_observation=user_market_observation,
        is_turbo=is_turbo,
    )

    model_slug = REPLICATE_MODEL
    if verbose:
        print("🧠 [BRAIN] Consultando Claude 4.5 Sonnet (Elite Mode)...")

    if prob_ml is not None:
        instrucao_duplo = (
            "Atua como analista de risco institucional: factual, conservador, sem optimismo nem criatividade literária.\n"
            "O contexto que se segue inclui obrigatoriamente (1) a secção «=== CONTEXTO AGREGADO» com o texto "
            "das notícias e feeds (institucional, Reddit, Twitter) e (2) o bloco final «=== SINAL TÉCNICO (XGBoost)» "
            "com P(alta) em percentagem explícita e em [0,1]. Cruza (1) com (2); se divergirem de forma material, "
            "VETO.\n"
            f"Resumo do sinal numérico deste ciclo (repetido no XGBoost no final): P(alta) = {_pct_br_ml(float(prob_ml))}.\n"
        )
        if is_turbo:
            instrucao_duplo += (
                "**MODO TURBO:** se P(alta) estiver na zona neutra (~45–55%), **não** uses isso sozinho para "
                "VETO — o operador forçou análise; prioriza confluência com o comando TURBO na secção Contexto Humano.\n"
            )
    else:
        instrucao_duplo = (
            "Atua como analista de risco institucional: factual, conservador, sem optimismo nem criatividade literária.\n"
            "O contexto inclui a secção «=== CONTEXTO AGREGADO» com o texto das notícias e feeds; "
            "baseia a leitura de factos apenas nesse texto (e indicadores técnicos se presentes).\n"
        )
    prompt = (
        f"{instrucao_duplo}\n"
        "Responde em conformidade com o preâmbulo do sistema: apenas o objeto JSON pedido (cinco chaves), "
        "sem markdown nem texto extra.\n\n"
        f"--- Contexto ---\n{prompt_completo}"
    )

    try:
        output = replicate.run(
            model_slug,
            input={
                "prompt": prompt,
                "max_tokens": _BRAIN_MAX_TOKENS,
                "temperature": _BRAIN_TEMPERATURE,
            },
        )
        resposta_bruta = _saida_replicate_para_string(output).strip()
        if verbose:
            print("✅ [BRAIN] Resposta do 4.5 recebida!")

        if resposta_bruta.upper() == "ERROR" or not resposta_bruta:
            return {
                "sentimento": "ERROR",
                "confianca": 0,
                "justificativa_curta": resposta_bruta or "resposta vazia",
                "alerta_macro": "",
                "posicao_recomendada": "VETO",
            }

        try:
            return _parse_json_resposta(resposta_bruta)
        except json.JSONDecodeError:
            if verbose:
                print("[Brain] Resposta não-JSON — interpretando veredito BULLISH/BEARISH/VETO.")
            return _dict_from_veredito_texto_brain(
                resposta_bruta,
                direcao_sugerida=d,
            )

    except Exception as e:  # noqa: BLE001
        print(f"❌ [BRAIN ERROR] Falha no Claude 4.5: {e}")
        return {
            "sentimento": "ERROR",
            "confianca": 0,
            "justificativa_curta": str(e),
            "alerta_macro": "",
            "posicao_recomendada": "VETO",
        }


def analyze_decision(market_context: dict[str, Any]) -> dict[str, Any]:
    """Adaptador: contexto JSON → texto; mapeia sentimento → BUY / SELL / HOLD."""
    bloco = json.dumps(market_context, ensure_ascii=False, indent=2)
    texto = (
        "Contexto de mercado (JSON). Mesmas regras macro + cautela em divergência:\n"
        f"{bloco}"
    )
    r = analisar_sentimento_mercado(texto, verbose=False)
    sent = str(r.get("sentimento", "ERROR")).upper()
    if sent == "BULLISH":
        action = "BUY"
    elif sent == "BEARISH":
        action = "SELL"
    else:
        # NEUTRAL, CAUTIOUS, ERROR → não comprar agressivamente
        action = "HOLD"

    conf_raw = r.get("confianca")
    confidence: float | None = None
    if conf_raw is not None:
        try:
            v = float(conf_raw)
            confidence = v / 100.0 if v > 1.0 else v
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = None

    raw_response = json.dumps(r, ensure_ascii=False)

    return {
        "action": action,
        "confidence": confidence,
        "raw_response": raw_response,
        "replicate_prediction_id": None,
        "model": REPLICATE_MODEL,
    }


def revisar_tese_posicao_aberta(
    *,
    direcao_posicao: str,
    roi_frac: float,
    pnl_nao_realizado: float | None,
    funding_rate: float | None,
    order_book_imbalance_pct: float | None,
    bid_volume_total: float | None,
    ask_volume_total: float | None,
    rsi_14: float | None = None,
    distancia_vwap_pct: float | None = None,
    inclinacao_ema9_pct: float | None = None,
    contexto_sentimento_noticias: str | None = None,
    noticias_recentes: str | None = None,
    verbose: bool = True,
) -> tuple[str, str]:
    """
    Revisão de tese ativa para posição já aberta.
    Retorna: ("MANTER"|"FECHAR", "motivo curto").
    """
    if not _token_configurado():
        _avisar_token_ausente()
        return ("MANTER", "Token Replicate ausente; fallback conservador para manter.")

    d = str(direcao_posicao or "LONG").strip().upper()
    if d not in ("LONG", "SHORT"):
        d = "LONG"

    pnl_txt = "N/A" if pnl_nao_realizado is None else f"{float(pnl_nao_realizado):.6f} USDC"
    fr_txt = "N/A" if funding_rate is None else f"{float(funding_rate):.8f}"
    imb_txt = (
        "N/A"
        if order_book_imbalance_pct is None
        else f"{float(order_book_imbalance_pct):+.2f}%"
    )
    bid_txt = "N/A" if bid_volume_total is None else f"{float(bid_volume_total):.6f}"
    ask_txt = "N/A" if ask_volume_total is None else f"{float(ask_volume_total):.6f}"
    rsi_txt = "N/A" if rsi_14 is None else f"{float(rsi_14):.2f}"
    vwap_dist_txt = "N/A" if distancia_vwap_pct is None else f"{float(distancia_vwap_pct):+.3f}%"
    ema9_slope_txt = "N/A" if inclinacao_ema9_pct is None else f"{float(inclinacao_ema9_pct):+.3f}%"
    nota_ctx = (contexto_sentimento_noticias or "").strip()
    if not nota_ctx:
        nota_ctx = "Nenhuma nota manual ativa no Supabase."
    noticias_ctx = (noticias_recentes or "").strip() or "Sem notícias de impacto no momento"

    prompt = (
        "És um gestor de risco intraday para trade já aberto.\n"
        "Objetivo: decidir se mantém ou fecha antecipadamente a posição.\n"
        "Importante: order book pode conter spoofing; só recomende FECHAR por livro se houver "
        "assimetria forte e coerente com Funding/ROI.\n\n"
        "Dados da posição:\n"
        f"- direcao_posicao: {d}\n"
        f"- roi_frac: {float(roi_frac):+.6f} (equivale a {float(roi_frac)*100:+.2f}%)\n"
        f"- pnl_nao_realizado: {pnl_txt}\n"
        f"- funding_rate: {fr_txt}\n"
        f"- order_book_imbalance_pct: {imb_txt}\n"
        f"- bid_volume_total_nivel20: {bid_txt}\n"
        f"- ask_volume_total_nivel20: {ask_txt}\n"
        f"- rsi_14: {rsi_txt}\n"
        f"- distancia_vwap_pct: {vwap_dist_txt}\n"
        f"- inclinacao_ema9_pct: {ema9_slope_txt}\n\n"
        "Contexto de Sentimento/Notícias (nota manual ativa):\n"
        f"{nota_ctx}\n\n"
        "[NOTÍCIAS RECENTES]:\n"
        f"{noticias_ctx}\n\n"
        "Prioriza identificar mudança estrutural técnica contra a posição "
        "(momentum, exaustão, perda de VWAP, inversão de inclinação EMA9), "
        "mas só recomende FECHAR se houver evidência forte e consistente.\n\n"
        "Responda em 2 linhas EXATAMENTE:\n"
        "Linha 1: [MANTER] ou [FECHAR]\n"
        "Linha 2: motivo curto em uma frase.\n"
        "Sem JSON, sem markdown, sem texto extra."
    )

    if verbose:
        print("🧠 [BRAIN] Revisão de tese ativa: consultando Claude...", flush=True)

    try:
        output = replicate.run(
            REPLICATE_MODEL,
            input={
                "prompt": prompt,
                "max_tokens": 220,
                "temperature": 0.2,
            },
        )
        raw = _saida_replicate_para_string(output).strip()
        up = raw.upper()
        if "[FECHAR]" in up:
            action = "FECHAR"
        elif "[MANTER]" in up:
            action = "MANTER"
        else:
            # Fail-safe: sem comando explícito, manter posição.
            action = "MANTER"
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        reason = "Sem detalhe retornado pelo modelo."
        if len(lines) >= 2:
            reason = lines[1]
        elif len(lines) == 1:
            reason = lines[0].replace("[MANTER]", "").replace("[FECHAR]", "").strip() or reason
        return (action, reason[:300])
    except Exception as e:  # noqa: BLE001
        print(f"❌ [BRAIN ERROR] Revisão de tese falhou: {e}")
        return ("MANTER", f"Falha no LLM ({e}); mantendo posição por segurança.")


if __name__ == "__main__":
    from intelligence_hub import obter_hub_padrao

    print("\n=== [Brain] Teste com IntelligenceHub + Claude ===\n")
    if not _token_configurado():
        _avisar_token_ausente()
        sys.exit(1)

    ctx = obter_hub_padrao().obter_contexto_agregado()
    print("--- Prévia do contexto ---\n")
    print(ctx[:2500] + ("..." if len(ctx) > 2500 else ""))
    print("\n--- Resposta do modelo ---\n")
    r = analisar_sentimento_mercado(ctx, verbose=True)
    print(json.dumps(r, indent=2, ensure_ascii=False))
