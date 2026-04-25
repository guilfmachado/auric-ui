from __future__ import annotations

import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


def _build_client() -> OpenAI:
    api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY não encontrado. Defina no .env antes de executar."
        )
    return OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )


def auditoria_profunda(
    codigo_ou_dados: str,
    *,
    model: str | None = None,
    max_tokens: int = 4000,
) -> str:
    print("🧠 Chamando DeepSeek para auditoria...")
    client = _build_client()
    model_name = (model or os.getenv("DEEPSEEK_MODEL") or "deepseek-v4-pro").strip()

    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Atuas como um Quant e Auditor de Código Sênior. "
                        "Revisa a lógica a seguir para prevenir loops infinitos "
                        "e vazamento de capital."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Analise isto criticamente e aponte falhas: "
                        f"{codigo_ou_dados}"
                    ),
                },
            ],
            max_tokens=max_tokens,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        return f"Erro na API: {e}"


if __name__ == "__main__":
    codigo_do_claude = """..."""
    parecer = auditoria_profunda(codigo_do_claude)
    print(parecer)
