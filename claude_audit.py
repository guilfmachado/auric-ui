"""
Auditor de risco macro via Claude (Anthropic API).
Requer ANTHROPIC_API_KEY no .env.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

_JSON_RE = re.compile(r"\{[\s\S]*\}")


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
    m = _JSON_RE.search(txt)
    if not m:
        raise ValueError("JSON não encontrado na resposta.")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("JSON retornado não é objeto.")
    return obj


def _text_from_message_content(content: object) -> str:
    """Junta blocos de texto da resposta Messages API."""
    parts: list[str] = []
    if isinstance(content, str):
        return content.strip()
    for block in content or []:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            continue
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    return "".join(parts).strip()


def _build_client() -> Anthropic:
    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY não configurada.")
    return Anthropic(api_key=api_key)


def get_claude_audit(news_summary: str, deepseek_score: int | float) -> dict[str, Any]:
    """
    Claude atua como o Auditor de Risco (segunda opinião ao score DeepSeek).

    Retorno tipado (após parse):
        consensus_score: int 0–100
        audit_comment: str
        approved: bool
    """
    client = _build_client()
    prompt = f"""
Como Auditor de Risco do Projeto AURIC, analisa este resumo de mercado:
{news_summary}

O analista primário (DeepSeek) deu um score de {deepseek_score}.
Concordas com este nível de agressividade/risco?
Responde APENAS com um JSON válido (sem markdown) neste formato exato:
{{
  "consensus_score": <inteiro de 0 a 100>,
  "audit_comment": "<breve explicação da divergência ou concordância>",
  "approved": <true ou false>
}}
""".strip()

    message = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _text_from_message_content(message.content)
    parsed = _extract_json_object(raw)

    score_raw = parsed.get("consensus_score", deepseek_score)
    try:
        consensus_score = int(float(score_raw))
    except (TypeError, ValueError):
        consensus_score = int(float(deepseek_score))
    consensus_score = max(0, min(100, consensus_score))

    audit_comment = str(parsed.get("audit_comment") or "").strip()
    approved = bool(parsed.get("approved", False))

    return {
        "consensus_score": consensus_score,
        "audit_comment": audit_comment,
        "approved": approved,
    }
