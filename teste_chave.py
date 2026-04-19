import os
from dotenv import load_dotenv
import ccxt

load_dotenv()

key = os.getenv("BINANCE_API_KEY", "").strip()
secret = os.getenv("BINANCE_API_SECRET", "").strip()

if not key or not secret:
    print("❌ Defina BINANCE_API_KEY e BINANCE_API_SECRET no .env")
    raise SystemExit(1)

print(f"Testando Chave: {key[:5]}... (Oculta)")

exchange = ccxt.binance({
    'apiKey': key,
    'secret': secret,
    'options': {'defaultType': 'future'}
})

try:
    balance = exchange.fetch_balance()
    print("✅ SUCESSO! A chave funciona e o saldo foi lido.")
except Exception as e:
    print(f"❌ ERRO CONTINUA: {e}")
