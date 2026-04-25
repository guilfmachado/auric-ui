from __future__ import annotations

import json
import os
import re
from typing import Any

from openai import OpenAI


def _build_client() -> OpenAI:
    api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY não configurada.")
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    txt = str(raw_text or "").strip()
    if not txt:
        raise ValueError("Resposta vazia.")
    try:
        obj = json.loads(txt)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", txt)
    if not m:
        raise ValueError("JSON não encontrado na resposta.")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("JSON retornado não é objeto.")
    return obj


def evaluate_trade_risk(
    symbol: str,
    current_price: float,
    volume_data: dict[str, Any],
    recent_supabase_logs: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Chama DeepSeek-V4 como comitê de risco final.
    Retorna: {"risk_score": 0-100, "market_regime": str, "veto_trade": bool}
    """
    client = _build_client()
    system_prompt = (
        "Você é um gestor de risco quantitativo (Smart Money Concepts). "
        "Analise os dados de mercado atuais e o histórico de rejeições recentes do bot. "
        'Avalie a probabilidade de um bull trap ou bear trap. Responda ESTRITAMENTE em JSON '
        'com as chaves: "risk_score" (0 a 100), "market_regime" (trending, ranging, manipulation), '
        'e "veto_trade" (booleano).'
    )
    user_payload = {
        "symbol": symbol,
        "current_price": current_price,
        "volume_data": volume_data,
        "recent_supabase_logs": recent_supabase_logs,
    }
    response = client.with_options(timeout=12.0).chat.completions.create(
        model="deepseek-reasoner",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        max_tokens=400,
        temperature=0.0,
    )
    mensagem = response.choices[0].message
    if hasattr(mensagem, "reasoning_content") and mensagem.reasoning_content:
        print("\n🧠 [DEEPSEEK V4 PENSANDO...]")
        print(f"{mensagem.reasoning_content}")
        print("--------------------------------------------------\n")
    conteudo_final = mensagem.content
    parsed = _extract_json_object((conteudo_final or "").strip())
    risk_score_raw = parsed.get("risk_score", 0)
    try:
        risk_score = float(risk_score_raw)
    except (TypeError, ValueError):
        risk_score = 0.0
    risk_score = max(0.0, min(100.0, risk_score))
    market_regime = str(parsed.get("market_regime") or "ranging").strip().lower()
    if market_regime not in ("trending", "ranging", "manipulation"):
        market_regime = "ranging"
    veto_trade = bool(parsed.get("veto_trade", False))
    return {
        "risk_score": float(risk_score),
        "market_regime": market_regime,
        "veto_trade": veto_trade,
    }
