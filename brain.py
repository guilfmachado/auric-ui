"""
Análise de sentimento (ETH) via Replicate — Claude 3.5 Sonnet.

Motor «Auric Final Boss»: macro-correlacional + vetores geopolítico, infra e baleias;
contexto agregado do IntelligenceHub; cruza com P(alta) do XGBoost e direção sugerida (LONG/SHORT).
Saída JSON: sentimento, confianca, justificativa_curta, alerta_macro, posicao_recomendada.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

import replicate
from dotenv import load_dotenv

load_dotenv()

# Modelo padrão no Replicate — versão estável oficial (sobrescreva com REPLICATE_MODEL no .env).
MODEL_SLUG = os.getenv("REPLICATE_MODEL", "anthropic/claude-3.5-sonnet").strip()


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
) -> str:
    bloco = (
        _prompt_sistema_claude(direcao_sugerida)
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


def _input_replicate_claude(prompt_completo: str) -> dict[str, Any]:
    return {
        "prompt": prompt_completo,
        "max_tokens": 1024,
        "temperature": 0.15,
    }


def analisar_sentimento_mercado(
    contexto_agregado: str,
    *,
    prob_ml: float | None = None,
    limiar_ml: float | None = None,
    direcao_sugerida: str = "LONG",
    verbose: bool = True,
) -> dict[str, Any]:
    """
    Envia o contexto do IntelligenceHub (+ XGBoost + direção LONG/SHORT) ao Claude via Replicate.
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
        ref_xg = (
            "=== SINAL TÉCNICO (XGBoost — referência do orquestrador neste ciclo) ===\n"
            f"P(alta próximo horizonte) = {prob_ml:.4f}. "
            f"Limiar long do maestro (ativa Hub): {lim_txt}; zona short: P(alta) ≤ 0,40. "
            f"Direção sugerida neste ciclo: {d}. "
            "Cruza com macro/geopolítica/infra/baleias; divergência crítica → posicao_recomendada VETO."
        )

    prompt_completo = _montar_prompt_completo_claude(
        contexto_agregado,
        ref_xgboost=ref_xg,
        direcao_sugerida=d,
    )

    if verbose:
        print("[Brain] Enviando contexto para Replicate (Claude 3.5 Sonnet)...")
        print(f"   Modelo: {MODEL_SLUG}")

    try:
        inp = _input_replicate_claude(prompt_completo)
        output = replicate.run(MODEL_SLUG, input=inp)
        resposta_bruta = _saida_replicate_para_string(output).strip()

        try:
            dados = _parse_json_resposta(resposta_bruta)
            return dados
        except json.JSONDecodeError as e:
            if verbose:
                print(f"[Brain] Falha no parse JSON: {e}")
                print(f"[Brain] Trecho (800 chars):\n{resposta_bruta[:800]}")
            return {
                "sentimento": "ERROR",
                "confianca": 0,
                "justificativa_curta": f"JSON inválido: {e!s}",
                "alerta_macro": "",
                "posicao_recomendada": "VETO",
            }

    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"[Brain] Erro na chamada ao Replicate: {e}")
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
        "model": MODEL_SLUG,
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
