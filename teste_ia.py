import os
from openai import OpenAI
from dotenv import load_dotenv

# Carrega a chave do teu .env
load_dotenv()

print("🔌 Tentando conectar ao DeepSeek-V4...")

try:
    client = OpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
    )

    response = client.chat.completions.create(
        model="deepseek-chat",  # Usamos o chat normal só para o teste rápido
        messages=[
            {
                "role": "user",
                "content": "Responde apenas: 'Sistema Operacional Smart Money Online e a escutar.'",
            }
        ],
        max_tokens=50,
    )

    print(f"✅ SUCESSO! A IA respondeu: {response.choices[0].message.content}")

except Exception as e:
    print(f"❌ ERRO NA CONEXÃO: {e}")
