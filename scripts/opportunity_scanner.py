"""Opportunity scanner: finds mispriced markets across ALL Polymarket categories.

Expands beyond weather to crypto, sports, politics, and events.
Uses multiple data sources to estimate true probabilities and find edges.
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from config import CONFIG, CITIES, GAMMA_API_URL, PROXIES, OPENMETEO_URL

logger = logging.getLogger(__name__)

# --- Data Source URLs ---
COINGECKO_API = "https://api.coingecko.com/api/v3"
ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Crypto symbol -> CoinGecko ID mapping
CRYPTO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "XRP": "ripple", "DOGE": "dogecoin", "ADA": "cardano",
    "AVAX": "avalanche-2", "DOT": "polkadot", "MATIC": "matic-network",
    "LINK": "chainlink", "UNI": "uniswap", "ATOM": "cosmos",
    "LTC": "litecoin", "BNB": "binancecoin", "NEAR": "near",
    "ARB": "arbitrum", "OP": "optimism", "SUI": "sui",
    "APT": "aptos", "TIA": "celestia", "SEI": "sei-network",
    "INJ": "injective-protocol", "PEPE": "pepe", "WIF": "dogwifcoin",
    "BONK": "bonk", "SHIB": "shiba-inu", "FET": "fetch-ai",
    "RENDER": "render-token", "TAO": "bittensor", "KAS": "kaspa",
}

# Output paths
REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reports")
OPPORTUNITIES_LOG = os.path.join(REPORT_DIR, "opportunities.csv")


@dataclass
class Opportunity:
    """A potential trading opportunity."""
    category: str          # crypto, weather, sports, politics, event
    market_question: str
    market_id: str
    token_id: str
    side: str              # YES or NO
    market_price: float    # Current market price
    estimated_prob: float  # Our estimated probability
    edge: float            # estimated_prob - market_price (or market_price - estimated_prob for NO)
    confidence: str        # high, medium, low
    reasoning: str         # Why we think there's an edge
    volume: float          # Market volume
    liquidity: float       # Market liquidity
    end_date: str          # When market closes
    kelly_size: float      # Suggested position size


class CryptoDataClient:
    """Fetches real-time and historical crypto data from CoinGecko."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "PolymarketBot/1.0",
        })
        self._price_cache = {}
        self._cache_time = 0

    def get_prices(self, symbols: list[str] = None) -> dict:
        """Get current prices for major cryptos. Returns {symbol: price_usd}."""
        now = time.time()
        if self._price_cache and (now - self._cache_time) < 60:
            return self._price_cache

        if symbols is None:
            symbols = list(CRYPTO_IDS.keys())

        ids = [CRYPTO_IDS[s] for s in symbols if s in CRYPTO_IDS]
        if not ids:
            return {}

        try:
            resp = self.session.get(
                f"{COINGECKO_API}/simple/price",
                params={
                    "ids": ",".join(ids),
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                    "include_market_cap": "true",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            prices = {}
            id_to_symbol = {v: k for k, v in CRYPTO_IDS.items()}
            for coin_id, info in data.items():
                symbol = id_to_symbol.get(coin_id, coin_id.upper())
                prices[symbol] = {
                    "price": info.get("usd", 0),
                    "change_24h": info.get("usd_24h_change", 0),
                    "market_cap": info.get("usd_market_cap", 0),
                }

            self._price_cache = prices
            self._cache_time = now
            return prices

        except Exception as e:
            logger.error(f"CoinGecko fetch failed: {e}")
            return self._price_cache or {}

    def get_price_at_date(self, symbol: str, date_str: str) -> Optional[float]:
        """Get historical price for a crypto on a specific date."""
        coin_id = CRYPTO_IDS.get(symbol)
        if not coin_id:
            return None

        try:
            # CoinGecko expects dd-mm-yyyy
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            date_param = dt.strftime("%d-%m-%Y")

            resp = self.session.get(
                f"{COINGECKO_API}/coins/{coin_id}/history",
                params={"date": date_param},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("market_data", {}).get("current_price", {}).get("usd")
        except Exception as e:
            logger.warning(f"Historical price fetch failed for {symbol}: {e}")
            return None


class MarketAnalyzer:
    """Analyzes Polymarket markets to find mispriced opportunities."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        if PROXIES:
            self.session.proxies.update(PROXIES)
        self.crypto = CryptoDataClient()

    def fetch_all_active_markets(self, limit: int = 1000) -> list[dict]:
        """Fetch all active markets from Polymarket Gamma API."""
        all_markets = []
        for offset in range(0, limit, 100):
            try:
                resp = self.session.get(
                    f"{GAMMA_API_URL}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": 100,
                        "offset": offset,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                all_markets.extend(batch)
                time.sleep(0.3)
            except Exception as e:
                logger.error(f"Failed to fetch markets at offset {offset}: {e}")
                break

        logger.info(f"Fetched {len(all_markets)} active markets")
        return all_markets

    def categorize_market(self, market: dict) -> str:
        """Categorize a market by type."""
        question = market.get("question", "").lower()
        tags = [t.lower() for t in market.get("tags", [])]

        if any(kw in question for kw in ["temperature", "°c", "°f", "weather", "rain", "snow", "hurricane"]):
            return "weather"
        # Use word boundaries to avoid matching "eth" in "Netherlands", "Beth", etc.
        crypto_patterns = [
            r'\bbitcoin\b', r'\bbtc\b', r'\bethereum\b', r'\bcrypto\b',
            r'\bsolana\b', r'\bxrp\b', r'\bdogecoin\b', r'\bdoge\b',
            r'\$\d+k\b', r'\bmemecoin\b', r'\bairdrop\b', r'\btoken\b',
            r'\bdefi\b', r'\bnft\b', r'\bcoin\b',
        ]
        if any(re.search(p, question) for p in crypto_patterns):
            return "crypto"
        if any(kw in question for kw in ["win the", "nba", "nfl", "mlb", "nhl", "fifa", "premier league",
                                          "champions league", "world cup", "super bowl", "playoff"]):
            return "sports"
        if any(kw in question for kw in ["president", "trump", "biden", "election", "congress", "senate",
                                          "governor", "democrat", "republican", "vote"]):
            return "politics"
        return "event"

    def analyze_crypto_market(self, market: dict) -> Optional[Opportunity]:
        """Analyze a crypto prediction market against real price data."""
        question = market.get("question", "")
        desc = market.get("description", "").lower()

        # Skip 50-50 resolution markets (GTA VI traps, etc.)
        if "50-50" in desc or "50/50" in desc:
            return None
        prices = self.crypto.get_prices()

        # Pattern: "Will Bitcoin hit $X by DATE?"
        price_match = re.search(
            r'\b(bitcoin|btc|ethereum|solana|xrp|doge|dogecoin)\b.*?'
            r'\$([\d,]+(?:\.\d+)?[kmb]?)',
            question.lower()
        )
        if not price_match:
            return None

        # Map to symbol
        name_to_symbol = {
            "bitcoin": "BTC", "btc": "BTC",
            "ethereum": "ETH",
            "solana": "SOL",
            "xrp": "XRP", "doge": "DOGE", "dogecoin": "DOGE",
        }
        symbol = name_to_symbol.get(price_match.group(1))
        if not symbol or symbol not in prices:
            return None

        # Parse target price
        target_str = price_match.group(2).replace(",", "")
        multiplier = 1
        if target_str.endswith("k"):
            multiplier = 1000
            target_str = target_str[:-1]
        elif target_str.endswith("m"):
            multiplier = 1_000_000
            target_str = target_str[:-1]
        elif target_str.endswith("b"):
            multiplier = 1_000_000_000
            target_str = target_str[:-1]

        try:
            target_price = float(target_str) * multiplier
        except ValueError:
            return None

        current_price = prices[symbol]["price"]
        change_24h = prices[symbol].get("change_24h", 0)

        # Calculate distance to target
        pct_to_target = (target_price - current_price) / current_price

        # Get market end date
        end_date = market.get("endDate", "")[:10]
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            days_left = (end_dt - datetime.now()).days
        except (ValueError, TypeError):
            days_left = 365

        # Estimate probability based on distance and time
        # Simple model: likelihood decreases with distance, increases with time
        yes_price = 0
        no_price = 0
        yes_token_id = ""
        no_token_id = ""

        outcomes = json.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
        outcome_prices = json.loads(market.get("outcomePrices", "[]")) if isinstance(market.get("outcomePrices"), str) else market.get("outcomePrices", [])

        for i, outcome in enumerate(outcomes):
            price = float(outcome_prices[i]) if i < len(outcome_prices) else 0
            if outcome.lower() == "yes":
                yes_price = price
            elif outcome.lower() == "no":
                no_price = price

        tokens = market.get("tokens", [])
        for token in tokens:
            if token.get("outcome", "").lower() == "yes":
                yes_token_id = token.get("token_id", "")
            elif token.get("outcome", "").lower() == "no":
                no_token_id = token.get("token_id", "")

        if yes_price <= 0:
            return None

        # Determine if "above" or "below" market
        is_above = any(kw in question.lower() for kw in ["hit", "reach", "above", "over", "higher", "exceed"])
        is_below = any(kw in question.lower() for kw in ["below", "under", "fall", "drop", "crash"])

        # Estimate our probability
        # For "will X hit $Y" — if current price is already above target, YES is very likely
        if is_above or (not is_below):
            if current_price >= target_price:
                estimated_prob = 0.92  # Already above target
                confidence = "high"
                reasoning = f"{symbol} already at ${current_price:,.0f}, above target ${target_price:,.0f}"
            elif pct_to_target < 0.05 and days_left > 7:
                estimated_prob = 0.75
                confidence = "medium"
                reasoning = f"{symbol} only {pct_to_target:.1%} below target with {days_left} days left"
            elif pct_to_target < 0.15 and days_left > 30:
                estimated_prob = 0.55
                confidence = "low"
                reasoning = f"{symbol} {pct_to_target:.1%} below, {days_left} days — possible"
            elif pct_to_target > 1.0:
                estimated_prob = 0.02
                confidence = "high"
                reasoning = f"{symbol} needs {pct_to_target:.0%} gain — extremely unlikely"
            elif pct_to_target > 0.5:
                estimated_prob = 0.08
                confidence = "medium"
                reasoning = f"{symbol} needs {pct_to_target:.0%} gain in {days_left} days"
            else:
                estimated_prob = max(0.05, 0.4 - pct_to_target * 2)
                confidence = "low"
                reasoning = f"{symbol} at ${current_price:,.0f}, needs {pct_to_target:.1%} to hit ${target_price:,.0f}"
        else:
            # "Will X drop below Y"
            if current_price <= target_price:
                estimated_prob = 0.90
                confidence = "high"
                reasoning = f"{symbol} already below target"
            else:
                drop_needed = (current_price - target_price) / current_price
                estimated_prob = max(0.05, 0.3 - drop_needed * 2)
                confidence = "low"
                reasoning = f"{symbol} needs {drop_needed:.1%} drop"

        # Determine best side to trade
        edge_yes = estimated_prob - yes_price
        edge_no = (1 - estimated_prob) - no_price

        if edge_yes > edge_no and edge_yes > CONFIG["entry_threshold"]:
            side = "YES"
            edge = edge_yes
            market_price = yes_price
            token_id = yes_token_id
        elif edge_no > CONFIG["entry_threshold"]:
            side = "NO"
            edge = edge_no
            market_price = no_price
            estimated_prob = 1 - estimated_prob
            token_id = no_token_id
        else:
            return None  # No edge

        # Kelly sizing
        if market_price > 0 and market_price < 1 and estimated_prob > market_price:
            b = (1.0 - market_price) / market_price
            q = 1.0 - estimated_prob
            kelly_full = (estimated_prob * b - q) / b
            kelly_size = max(0, kelly_full * CONFIG["kelly_fraction"] * CONFIG["bankroll"])
            kelly_size = min(kelly_size, CONFIG["max_position_usd"])
        else:
            kelly_size = 0

        volume = float(market.get("volume", 0))
        liquidity = float(market.get("liquidity", 0))

        if kelly_size < 0.10:
            return None

        return Opportunity(
            category="crypto",
            market_question=question,
            market_id=market.get("conditionId", market.get("condition_id", "")),
            token_id=token_id,
            side=side,
            market_price=round(market_price, 4),
            estimated_prob=round(estimated_prob, 4),
            edge=round(edge, 4),
            confidence=confidence,
            reasoning=reasoning,
            volume=volume,
            liquidity=liquidity,
            end_date=end_date,
            kelly_size=round(kelly_size, 2),
        )

    def analyze_event_market(self, market: dict) -> Optional[Opportunity]:
        """Analyze general event markets for obvious mispricings.

        Looks for:
        - Markets priced near 50/50 with strong directional evidence
        - Nearly-resolved markets with stale prices
        - Markets with extreme prices that should be more extreme
        """
        question = market.get("question", "")
        outcomes = json.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
        outcome_prices = json.loads(market.get("outcomePrices", "[]")) if isinstance(market.get("outcomePrices"), str) else market.get("outcomePrices", [])

        if len(outcomes) < 2 or len(outcome_prices) < 2:
            return None

        yes_price = float(outcome_prices[0]) if outcome_prices else 0
        no_price = float(outcome_prices[1]) if len(outcome_prices) > 1 else 0

        tokens = market.get("tokens", [])
        yes_token_id = ""
        no_token_id = ""
        for token in tokens:
            if token.get("outcome", "").lower() == "yes":
                yes_token_id = token.get("token_id", "")
            elif token.get("outcome", "").lower() == "no":
                no_token_id = token.get("token_id", "")

        volume = float(market.get("volume", 0))
        liquidity = float(market.get("liquidity", 0))
        end_date = market.get("endDate", "")[:10]

        # Skip low-volume/low-liquidity markets
        if volume < 5000 or liquidity < 200:
            return None

        # Look for "almost certain NO" markets where YES is still priced > 3c
        # These are often free money — things that won't happen
        q_lower = question.lower()

        # TRAP DETECTION: "before GTA VI" and similar conditional markets
        # resolve 50-50 if neither event happens — NOT to NO!
        # Buying NO at 51c when it resolves to 50c = guaranteed loss
        conditional_traps = ["before gta", "before gta vi", "50-50", "50/50"]
        if any(kw in q_lower for kw in conditional_traps):
            desc = market.get("description", "").lower()
            if "50-50" in desc or "50/50" in desc:
                logger.debug(f"TRAP detected (50-50 resolution): {question[:60]}")
                return None  # Skip these entirely

        # Pattern: extremely unlikely events priced too high
        impossible_keywords = [
            "jesus christ return", "alien", "zombie", "end of the world",
            "asteroid", "nuclear war",
        ]
        if any(kw in q_lower for kw in impossible_keywords) and yes_price > 0.03:
            return Opportunity(
                category="event",
                market_question=question,
                market_id=market.get("conditionId", ""),
                token_id=no_token_id,
                side="NO",
                market_price=no_price,
                estimated_prob=0.99,
                edge=round(0.99 - no_price, 4),
                confidence="high",
                reasoning=f"Extremely unlikely event priced at {yes_price:.1%} YES — free NO",
                volume=volume,
                liquidity=liquidity,
                end_date=end_date,
                kelly_size=min(CONFIG["max_position_usd"],
                              CONFIG["kelly_fraction"] * CONFIG["bankroll"]),
            )

        # Pattern: time-based markets where deadline is very close
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            days_left = (end_dt - datetime.now()).days

            if days_left <= 3 and yes_price > 0.10:
                # Market closing in 3 days with YES still > 10% — check if it's resolvable
                # These often present NO opportunities as events haven't happened
                if not any(kw in q_lower for kw in ["will", "by"]):
                    pass  # Skip if we can't determine directionality
                elif volume > 50000:
                    return Opportunity(
                        category="event",
                        market_question=question,
                        market_id=market.get("conditionId", ""),
                        token_id=no_token_id,
                        side="NO",
                        market_price=no_price,
                        estimated_prob=0.85,
                        edge=round(0.85 - no_price, 4) if (0.85 - no_price) > CONFIG["entry_threshold"] else 0,
                        confidence="medium",
                        reasoning=f"Only {days_left} days left, event hasn't occurred, YES at {yes_price:.1%}",
                        volume=volume,
                        liquidity=liquidity,
                        end_date=end_date,
                        kelly_size=min(CONFIG["max_position_usd"],
                                      CONFIG["kelly_fraction"] * CONFIG["bankroll"] * 0.5),
                    )
        except (ValueError, TypeError):
            pass

        return None

    def scan_all(self) -> list[Opportunity]:
        """Scan all active markets and return ranked opportunities."""
        markets = self.fetch_all_active_markets()
        opportunities = []

        # Pre-fetch crypto prices
        self.crypto.get_prices()

        for market in markets:
            try:
                # Skip markets that already ended
                end_date = market.get("endDate", "")[:10]
                if end_date:
                    try:
                        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                        if end_dt < datetime.now():
                            continue
                    except ValueError:
                        pass

                category = self.categorize_market(market)

                opp = None
                if category == "crypto":
                    opp = self.analyze_crypto_market(market)
                elif category in ("event", "politics"):
                    opp = self.analyze_event_market(market)
                # Weather is handled by the existing weather_scanner.py

                if opp and opp.edge > CONFIG["entry_threshold"]:
                    opportunities.append(opp)

            except Exception as e:
                logger.debug(f"Error analyzing market: {e}")
                continue

        # Sort by edge * confidence
        confidence_weight = {"high": 3, "medium": 2, "low": 1}
        opportunities.sort(
            key=lambda o: o.edge * confidence_weight.get(o.confidence, 1),
            reverse=True,
        )

        logger.info(f"Found {len(opportunities)} opportunities across {len(markets)} markets")
        return opportunities


def generate_report(opportunities: list[Opportunity]) -> str:
    """Generate a human-readable report of opportunities."""
    now = datetime.now(timezone.utc)
    lines = [
        f"{'='*70}",
        f"  POLYMARKET OPPORTUNITY REPORT",
        f"  Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"  Total opportunities: {len(opportunities)}",
        f"{'='*70}",
        "",
    ]

    # Group by category
    by_category = {}
    for opp in opportunities:
        by_category.setdefault(opp.category, []).append(opp)

    for category, opps in sorted(by_category.items()):
        lines.append(f"--- {category.upper()} ({len(opps)} opportunities) ---")
        lines.append("")

        for i, opp in enumerate(opps[:10], 1):  # Top 10 per category
            lines.append(f"  {i}. {opp.market_question[:80]}")
            lines.append(f"     Side: {opp.side} @ {opp.market_price:.1%} | "
                        f"Our estimate: {opp.estimated_prob:.1%} | "
                        f"Edge: {opp.edge:.1%}")
            lines.append(f"     Confidence: {opp.confidence} | "
                        f"Kelly size: ${opp.kelly_size:.2f} | "
                        f"Volume: ${opp.volume:,.0f}")
            lines.append(f"     Reasoning: {opp.reasoning}")
            lines.append(f"     Ends: {opp.end_date}")
            lines.append("")

    if not opportunities:
        lines.append("  No opportunities found meeting edge threshold.")
        lines.append("  (This is normal — good opportunities are rare)")
        lines.append("")

    # Summary stats
    if opportunities:
        total_kelly = sum(o.kelly_size for o in opportunities)
        avg_edge = sum(o.edge for o in opportunities) / len(opportunities)
        high_conf = sum(1 for o in opportunities if o.confidence == "high")

        lines.append(f"{'='*70}")
        lines.append(f"  SUMMARY")
        lines.append(f"  High confidence: {high_conf} | Avg edge: {avg_edge:.1%}")
        lines.append(f"  Total suggested allocation: ${total_kelly:.2f}")
        lines.append(f"  Bankroll: ${CONFIG['bankroll']:.2f}")
        lines.append(f"{'='*70}")

    return "\n".join(lines)


def save_opportunities(opportunities: list[Opportunity]):
    """Save opportunities to CSV for tracking."""
    os.makedirs(REPORT_DIR, exist_ok=True)

    file_exists = os.path.exists(OPPORTUNITIES_LOG)
    with open(OPPORTUNITIES_LOG, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "timestamp", "category", "question", "side", "market_price",
                "estimated_prob", "edge", "confidence", "kelly_size",
                "volume", "reasoning", "market_id",
            ])
        for opp in opportunities:
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                opp.category,
                opp.market_question[:100],
                opp.side,
                opp.market_price,
                opp.estimated_prob,
                opp.edge,
                opp.confidence,
                opp.kelly_size,
                opp.volume,
                opp.reasoning[:100],
                opp.market_id,
            ])


def save_report(report: str):
    """Save report to file."""
    os.makedirs(REPORT_DIR, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d_%H%M")
    filepath = os.path.join(REPORT_DIR, f"report_{date_str}.txt")
    with open(filepath, "w") as f:
        f.write(report)
    logger.info(f"Report saved to {filepath}")
    return filepath


def main():
    parser = argparse.ArgumentParser(description="Polymarket Opportunity Scanner")
    parser.add_argument("--category", choices=["crypto", "weather", "sports", "politics", "event", "all"],
                       default="all", help="Category to scan")
    parser.add_argument("--min-edge", type=float, default=None,
                       help="Minimum edge threshold (overrides config)")
    parser.add_argument("--top", type=int, default=20,
                       help="Show top N opportunities")
    parser.add_argument("--save", action="store_true",
                       help="Save report and opportunities to files")
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if args.min_edge is not None:
        CONFIG["entry_threshold"] = args.min_edge

    analyzer = MarketAnalyzer()
    logger.info("Scanning all active Polymarket markets...")

    opportunities = analyzer.scan_all()

    if args.category != "all":
        opportunities = [o for o in opportunities if o.category == args.category]

    opportunities = opportunities[:args.top]

    report = generate_report(opportunities)
    print(report)

    if args.save and opportunities:
        save_opportunities(opportunities)
        filepath = save_report(report)
        print(f"\nReport saved to: {filepath}")


if __name__ == "__main__":
    main()
