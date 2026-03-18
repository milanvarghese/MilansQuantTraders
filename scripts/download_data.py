"""Download historical crypto price data for backtesting.

Uses Coinbase public API (free, no auth) to get hourly OHLC candles
with High/Low for accurate path-dependent market resolution checks.

Usage:
    python scripts/download_data.py                  # Download all (BTC, ETH, SOL)
    python scripts/download_data.py --symbol BTC     # BTC only
    python scripts/download_data.py --days 730       # 2 years (default)
    python scripts/download_data.py --granularity 60 # 1-minute candles (warning: large)
"""

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
COINBASE_API = "https://api.exchange.coinbase.com"

# Coinbase product IDs — top liquid pairs for scalper backtesting
PRODUCTS = {
    # Tier 1: Major pairs (highest volume)
    "BTC": "BTC-USD", "ETH": "ETH-USD", "XRP": "XRP-USD", "SOL": "SOL-USD",
    "DOGE": "DOGE-USD", "ADA": "ADA-USD", "AVAX": "AVAX-USD", "LINK": "LINK-USD",
    "DOT": "DOT-USD", "LTC": "LTC-USD",
    # Tier 2: High volume altcoins
    "SUI": "SUI-USD", "NEAR": "NEAR-USD", "UNI": "UNI-USD", "ATOM": "ATOM-USD",
    "ARB": "ARB-USD", "OP": "OP-USD", "HBAR": "HBAR-USD", "FET": "FET-USD",
    "RENDER": "RENDER-USD", "ONDO": "ONDO-USD",
    # Tier 3: Active trading pairs
    "AAVE": "AAVE-USD", "ICP": "ICP-USD", "FIL": "FIL-USD", "APT": "APT-USD",
    "XLM": "XLM-USD", "ALGO": "ALGO-USD", "VET": "VET-USD", "SEI": "SEI-USD",
    "STX": "STX-USD", "CRV": "CRV-USD", "ZEC": "ZEC-USD", "BCH": "BCH-USD",
    "ENA": "ENA-USD", "TIA": "TIA-USD", "BONK": "BONK-USD", "TAO": "TAO-USD",
    "WLD": "WLD-USD", "DASH": "DASH-USD", "BNB": "BNB-USD", "QNT": "QNT-USD",
}

# Coinbase returns max 300 candles per request
MAX_CANDLES_PER_REQUEST = 300


def download_candles(symbol: str, days: int = 730, granularity: int = 3600) -> str:
    """Download OHLC candles from Coinbase and save to CSV.

    Args:
        symbol: Crypto symbol (BTC, ETH, SOL, etc.)
        days: How many days of history to download
        granularity: Candle interval in seconds (3600=1h, 86400=1d, 60=1min)

    Returns:
        Path to saved CSV file.
    """
    product_id = PRODUCTS.get(symbol)
    if not product_id:
        # Allow arbitrary Coinbase product IDs (e.g. "TAO" -> "TAO-USD")
        product_id = f"{symbol}-USD"
        logger.info(f"Symbol {symbol} not in default list, trying {product_id}")

    os.makedirs(DATA_DIR, exist_ok=True)

    interval_name = {60: "1m", 300: "5m", 900: "15m", 3600: "1h", 86400: "1d"}.get(granularity, f"{granularity}s")
    filepath = os.path.join(DATA_DIR, f"{symbol}_{interval_name}_ohlc.csv")

    session = requests.Session()
    session.headers.update({"User-Agent": "PolymarketBot/1.0"})

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)

    # Calculate chunk size based on granularity
    chunk_seconds = MAX_CANDLES_PER_REQUEST * granularity
    chunk_delta = timedelta(seconds=chunk_seconds)

    all_candles = []
    current_start = start_dt
    request_count = 0
    total_chunks = (days * 86400) // chunk_seconds + 1

    print(f"  Downloading {symbol} {interval_name} candles: {start_dt.date()} to {end_dt.date()} ({days} days)")
    print(f"  Estimated {total_chunks} API requests needed...")

    while current_start < end_dt:
        current_end = min(current_start + chunk_delta, end_dt)

        try:
            resp = session.get(
                f"{COINBASE_API}/products/{product_id}/candles",
                params={
                    "granularity": granularity,
                    "start": current_start.isoformat(),
                    "end": current_end.isoformat(),
                },
                timeout=15,
            )

            if resp.status_code == 429:
                logger.warning("Rate limited, waiting 5s...")
                time.sleep(5)
                continue

            if resp.status_code != 200:
                logger.warning(f"HTTP {resp.status_code} for {symbol} chunk {current_start.date()}")
                current_start = current_end
                time.sleep(0.5)
                continue

            candles = resp.json()
            if isinstance(candles, list) and candles:
                # Coinbase format: [timestamp, low, high, open, close, volume]
                for c in candles:
                    all_candles.append({
                        "timestamp": c[0],
                        "open": c[3],
                        "high": c[2],
                        "low": c[1],
                        "close": c[4],
                        "volume": c[5],
                    })

            request_count += 1
            if request_count % 20 == 0:
                print(f"    {request_count}/{total_chunks} requests | {len(all_candles)} candles | up to {current_end.date()}")

        except Exception as e:
            logger.warning(f"Request failed: {e}")

        current_start = current_end
        time.sleep(0.2)  # Rate limit: ~5 req/sec

    if not all_candles:
        print(f"  No data retrieved for {symbol}")
        return ""

    # Deduplicate and sort by timestamp
    seen = set()
    unique = []
    for c in all_candles:
        if c["timestamp"] not in seen:
            seen.add(c["timestamp"])
            unique.append(c)
    unique.sort(key=lambda x: x["timestamp"])

    # Save to CSV
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "datetime", "open", "high", "low", "close", "volume"])
        for c in unique:
            dt = datetime.fromtimestamp(c["timestamp"], tz=timezone.utc)
            writer.writerow([
                c["timestamp"],
                dt.strftime("%Y-%m-%d %H:%M:%S"),
                c["open"],
                c["high"],
                c["low"],
                c["close"],
                round(c["volume"], 4),
            ])

    # Summary
    first_dt = datetime.fromtimestamp(unique[0]["timestamp"], tz=timezone.utc)
    last_dt = datetime.fromtimestamp(unique[-1]["timestamp"], tz=timezone.utc)
    file_size_mb = os.path.getsize(filepath) / (1024 * 1024)

    print(f"  Saved: {filepath}")
    print(f"  Range: {first_dt.date()} to {last_dt.date()}")
    print(f"  Candles: {len(unique):,} | Size: {file_size_mb:.1f} MB")

    return filepath


def download_polymarket_resolved(max_pages: int = 20) -> str:
    """Download all resolved Polymarket markets to CSV.

    Fetches closed events from GAMMA API and saves market metadata + outcomes.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    filepath = os.path.join(DATA_DIR, "polymarket_resolved.csv")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "PolymarketBot/1.0",
        "Accept": "application/json",
    })

    all_markets = []
    print(f"  Fetching resolved Polymarket markets (up to {max_pages} pages)...")

    for offset in range(0, max_pages * 100, 100):
        try:
            resp = session.get("https://gamma-api.polymarket.com/events", params={
                "closed": "true",
                "limit": 100,
                "offset": offset,
                "order": "volume",
                "ascending": "false",
            }, timeout=15)

            if resp.status_code != 200:
                break

            events = resp.json()
            if not events:
                break

            for e in events:
                for m in e.get("markets", []):
                    all_markets.append(m)

            page = offset // 100 + 1
            if page % 5 == 0:
                print(f"    Page {page}/{max_pages} | {len(all_markets)} markets")

            time.sleep(0.3)

        except Exception as e:
            logger.warning(f"Fetch error: {e}")
            break

    if not all_markets:
        print("  No markets fetched")
        return ""

    # Save to CSV
    import json
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "condition_id", "question", "outcomes", "outcome_prices",
            "volume", "liquidity", "end_date", "resolved", "resolution",
            "tokens", "description",
        ])
        for m in all_markets:
            outcomes = m.get("outcomes", [])
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            prices = m.get("outcomePrices", [])
            if isinstance(prices, str):
                prices = json.loads(prices)
            tokens = m.get("tokens", [])

            writer.writerow([
                m.get("conditionId", m.get("condition_id", "")),
                m.get("question", ""),
                json.dumps(outcomes),
                json.dumps(prices),
                m.get("volume", 0),
                m.get("liquidity", 0),
                m.get("endDate", "")[:10],
                m.get("resolved", False),
                m.get("resolution", ""),
                json.dumps([{"outcome": t.get("outcome", ""), "token_id": t.get("token_id", "")} for t in tokens]),
                m.get("description", "")[:200],
            ])

    file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
    settled = sum(1 for m in all_markets
                  if any(str(p) in ("0", "1")
                         for p in (json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", []))))

    print(f"  Saved: {filepath}")
    print(f"  Total markets: {len(all_markets):,} | Settled (0/1 prices): {settled:,}")
    print(f"  Size: {file_size_mb:.1f} MB")

    return filepath


def main():
    parser = argparse.ArgumentParser(description="Download historical data for backtesting")
    parser.add_argument("--symbol", type=str, default=None,
                       help="Download specific symbol only (BTC, ETH, SOL)")
    parser.add_argument("--days", type=int, default=730,
                       help="Days of history (default: 730 = 2 years)")
    parser.add_argument("--granularity", type=int, default=3600,
                       help="Candle interval in seconds (3600=1h, 86400=1d, 300=5m)")
    parser.add_argument("--skip-crypto", action="store_true",
                       help="Skip crypto price downloads")
    parser.add_argument("--skip-polymarket", action="store_true",
                       help="Skip Polymarket market downloads")
    parser.add_argument("--polymarket-pages", type=int, default=20,
                       help="Pages of Polymarket data (100 markets/page, default: 20)")
    parser.add_argument("--scalper-backtest", action="store_true",
                       help="Download 5m candles (90 days) for all pairs — for scalper backtesting")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    print("=" * 60)
    print("  DATA DOWNLOADER FOR BACKTESTING")
    print("=" * 60)

    # Scalper backtest mode: download 5m candles for all pairs
    if args.scalper_backtest:
        all_symbols = list(PRODUCTS.keys())
        print(f"\n[SCALPER BACKTEST] Downloading 5m candles for {len(all_symbols)} pairs (90 days)...")
        for sym in all_symbols:
            print()
            download_candles(sym, days=90, granularity=300)
        print(f"\n{'='*60}")
        print(f"  DONE. 5m candle data saved to: {DATA_DIR}")
        print(f"{'='*60}")
        return

    # Download crypto OHLC
    if not args.skip_crypto:
        symbols = [args.symbol.upper()] if args.symbol else list(PRODUCTS.keys())
        print(f"\n[1/2] Downloading crypto OHLC ({', '.join(symbols)})...")
        for sym in symbols:
            print()
            download_candles(sym, days=args.days, granularity=args.granularity)
    else:
        print("\n[1/2] Skipping crypto downloads")

    # Download Polymarket resolved markets
    if not args.skip_polymarket:
        print(f"\n[2/2] Downloading Polymarket resolved markets...")
        print()
        download_polymarket_resolved(max_pages=args.polymarket_pages)
    else:
        print("\n[2/2] Skipping Polymarket downloads")

    print(f"\n{'='*60}")
    print(f"  DONE. Data saved to: {DATA_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
