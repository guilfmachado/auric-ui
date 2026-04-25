"""
Recebe sinais do TradingView (POST JSON) e delega abertura de posição em futuros.
Requer: pip install fastapi uvicorn
"""

from __future__ import annotations

import traceback

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

import uvicorn

from executor_futures import abrir_long_market, abrir_short_market

app = FastAPI()

# MANTÉM ESTA CHAVE SEGURA - Vais usá-la no TradingView
WEBHOOK_SECRET = "vibe_coding_2026_guilherme"


def _open_position(symbol: str, side: str) -> None:
    """Abre posição síncrona (ccxt); chamado em background para não bloquear o HTTP."""
    s = str(side).strip().upper()
    if s == "BUY":
        abrir_long_market(symbol)
    elif s == "SELL":
        abrir_short_market(symbol)
    else:
        print(f"⚠️ Ação desconhecida: {side!r}")


@app.post("/webhook")
async def receive_signal(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()

        # 1. Validação de Segurança
        if data.get("secret") != WEBHOOK_SECRET:
            print("⚠️ Tentativa de acesso sem chave válida!")
            return JSONResponse(
                status_code=403,
                content={"status": "error", "message": "Unauthorized"},
            )

        action_raw = data.get("action")
        if action_raw is None or str(action_raw).strip() == "":
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Missing action"},
            )
        action = str(action_raw).strip().upper()
        symbol = str(data.get("symbol", "ETHUSDC")).strip()

        print(f"🚀 SINAL RECEBIDO: {action} em {symbol}")

        # 2. Executar a Ordem (em background: entrada + brackets podem demorar)
        if action == "BUY":
            background_tasks.add_task(_open_position, symbol, "BUY")
        elif action == "SELL":
            background_tasks.add_task(_open_position, symbol, "SELL")
        else:
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": f"Invalid action: {action} (use BUY or SELL)",
                },
            )

        return {"status": "success", "action": action, "symbol": symbol}

    except Exception as e:
        print(f"❌ Erro no Webhook: {e}")
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)},
        )


if __name__ == "__main__":
    # Roda na porta 5000 para o TradingView conseguir falar com o teu VPS
    uvicorn.run(app, host="0.0.0.0", port=5000)
