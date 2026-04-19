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
- Se for LONG: notícias e redes favoráveis ao risco / ao ETH reforçam a tese de alta; más notícias graves podem impor CAUTIOUS/VETO.
- Se for SHORT: analisa se notícias negativas e pânico nas redes sociais CONFIRMAM a tese de queda. Para um SHORT, notícias ruins e medo excessivo são sinais VERDES (alinhamento); euforia e «risk-on» forte frente ao sinal técnico de queda sugerem VETO ou cautela.
"""

    return f"""És o Auric Final Boss, um Motor de Inteligência Quantitativa e Macro-Correlacional. A tua missão é proteger o capital e identificar assimetrias de alta probabilidade.

{bloco_direcao}

LÓGICA DE ANÁLISE (obrigatória):

1) Vetor Geopolítico: tensões (ex.: Irão/EUA). Risk-off global pode penalizar o ETH se a liquidez secar; cruza com {d}.

2) Vetor de Infraestrutura: Vitalik, DNS/eth.limo, hacks — incerteza técnica grave → tende a posicao_recomendada VETO.

3) Vetor de Baleias: movimentações USDT/ETH (Whale Alert); cruza com o XGBoost e com {d}.

4) Regra de Ouro — Divergência: se o sinal técnico for LONG mas o macro/infra estiver incompatível, CAUTIOUS/BEARISH e posicao_recomendada VETO ou contrária. Se for SHORT e o contexto for excessivamente altista sem medo, VETO. Melhor perder um trade que a banca.

5) Institucional vs. varejo: divergência forte → reforça cautela.

Responde APENAS com UM objeto JSON válido (sem markdown, sem ```, sem texto antes ou depois).
Chaves OBRIGATÓRIAS e APENAS estas:
{{
  "sentimento": "BULLISH" | "BEARISH" | "NEUTRAL" | "CAUTIOUS",
  "confianca": <inteiro 0 a 100>,
  "justificativa_curta": "<uma frase; foco na correlação com o sinal {d}>",
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
    status_bollinger = indicators.descrever_status_bollinger_para_prompt(snapshot)
    confluencia = snapshot.get("confluencia")
    if isinstance(confluencia, dict) and confluencia:
        bloco_conf = "\n\n" + indicators.formatar_confluencia_para_llm(confluencia)
    else:
        bloco_conf = ""

    missao = f"""Você é o estrategista chefe. Além das notícias e do ML, considere estes dados técnicos:

ADX: {adx_str} (Indica força da tendência)

Preço vs VWAP: {acima_ou_abaixo} (Viés institucional)

Bollinger: {status_bollinger} (Indica se o preço está esticado ou comprimido)

Sua Missão: Se o ML der 83% de alta, mas o ADX estiver em 12 (lateral) e o preço abaixo da VWAP, você deve dar VETO. Só confirme o trade se houver CONFLUÊNCIA entre técnico e notícias."""

    detalhe = indicators.formatar_bloco_indicadores_para_llm(snapshot)
    return f"{missao}\n\n{detalhe}{bloco_conf}"


def analisar_sentimento_mercado(
    contexto_agregado: str,
    *,
    prob_ml: float | None = None,
    limiar_ml: float | None = None,
    direcao_sugerida: str = "LONG",
    verbose: bool = True,
    bloco_indicadores: str | None = None,
    bloco_tecnico_prioritario: str | None = None,
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
        p = float(prob_ml)
        pct_br = _pct_br_ml(p)
        ref_xg = (
            "=== SINAL TÉCNICO (XGBoost — referência do orquestrador neste ciclo) ===\n"
            f"P(alta próximo horizonte): {pct_br} (valor em [0,1]: {p:.6f}; também {p:.4f}).\n"
            f"Limiar long do maestro (ativa Hub): {lim_txt}; zona short: P(alta) ≤ 0,40. "
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
