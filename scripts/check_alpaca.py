"""Quick check of Alpaca account status."""
import os, requests
from dotenv import load_dotenv
load_dotenv()

key = os.getenv("ALPACA_API_KEY", "")
secret = os.getenv("ALPACA_SECRET_KEY", "")
base = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
print(f"Base URL: {base}")
resp = requests.get(
    f"{base}/v2/account",
    headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
    timeout=10,
)
print(f"Status: {resp.status_code}")
if resp.status_code == 200:
    d = resp.json()
    for k in ["cash", "buying_power", "portfolio_value", "status",
              "pattern_day_trader", "account_blocked", "trading_blocked"]:
        print(f"  {k}: {d.get(k, '?')}")
else:
    print(resp.text[:500])
