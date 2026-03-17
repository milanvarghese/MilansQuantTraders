"""One-time setup: derive Polymarket L2 API credentials from your wallet.

Usage:
    1. Set PRIVATE_KEY in .env (your dedicated bot wallet, NOT main wallet)
    2. Run: python setup_creds.py
    3. Copy the output into your .env file
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))

from polymarket_client import PolymarketTrader

def main():
    print("Deriving Polymarket L2 API credentials from your wallet...")
    print("Make sure PRIVATE_KEY is set in your .env file.\n")

    trader = PolymarketTrader()
    try:
        creds = trader.derive_api_creds()
        print("=== Add these to your .env file ===\n")
        print(f"POLY_API_KEY={creds.get('apiKey', '')}")
        print(f"POLY_API_SECRET={creds.get('secret', '')}")
        print(f"POLY_API_PASSPHRASE={creds.get('passphrase', '')}")
        print("\nDone! Now restart the bot.")
    except Exception as e:
        print(f"Error: {e}")
        print("Make sure PRIVATE_KEY is set and py-clob-client is installed.")
        sys.exit(1)

if __name__ == "__main__":
    main()
