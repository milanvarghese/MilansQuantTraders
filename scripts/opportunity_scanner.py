"""Opportunity scanner: finds mispriced markets across ALL Polymarket categories.

Expands beyond weather to crypto, sports, politics, and events.
Uses multiple data sources to estimate true probabilities and find edges.
"""

import argparse
import csv
import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from config import CONFIG, CITIES, GAMMA_API_URL, PROXIES, OPENMETEO_URL, ODDS_API_KEY

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

    def _request_with_retry(self, url: str, params: dict, max_retries: int = 3) -> Optional[dict]:
        """Make a CoinGecko request with exponential backoff on rate limits and network errors."""
        for attempt in range(max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=15)
                if resp.status_code == 429:
                    wait = 2 ** (attempt + 1)  # 2, 4, 8 seconds
                    logger.debug(f"CoinGecko rate limit, waiting {wait}s (attempt {attempt + 1})")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (requests.exceptions.HTTPError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                if attempt < max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                logger.warning(f"Request failed after {max_retries} attempts: {e}")
                return None
        return None

    def get_prices(self, symbols: list[str] = None) -> dict:
        """Get current prices for major cryptos. Returns {symbol: price_usd}."""
        now = time.time()
        # Cache prices for 5 minutes (scanner runs every 30 min, no need to hammer API)
        if self._price_cache and (now - self._cache_time) < 300:
            return self._price_cache

        if symbols is None:
            symbols = list(CRYPTO_IDS.keys())

        ids = [CRYPTO_IDS[s] for s in symbols if s in CRYPTO_IDS]
        if not ids:
            return {}

        try:
            data = self._request_with_retry(
                f"{COINGECKO_API}/simple/price",
                params={
                    "ids": ",".join(ids),
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                    "include_market_cap": "true",
                },
            )
            if not data:
                return self._price_cache or {}

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

    def get_historical_volatility(self, symbol: str, days: int = 30) -> Optional[float]:
        """Calculate annualized volatility from CoinGecko daily prices (free endpoint).

        Returns annualized volatility as a decimal (e.g., 0.65 = 65%).
        Uses log returns for proper geometric Brownian motion modeling.
        """
        cache_key = f"vol_{symbol}_{days}"
        if cache_key in self._price_cache:
            cached_time = self._price_cache.get(f"{cache_key}_time", 0)
            if time.time() - cached_time < 3600:  # Cache volatility for 1 hour
                return self._price_cache[cache_key]

        coin_id = CRYPTO_IDS.get(symbol)
        if not coin_id:
            return None

        try:
            # CoinGecko free tier: ~10-30 req/min. Add delay between calls.
            last_vol_call = self._price_cache.get("_last_vol_call", 0)
            elapsed = time.time() - last_vol_call
            if elapsed < 2.0:
                time.sleep(2.0 - elapsed)
            self._price_cache["_last_vol_call"] = time.time()

            data = self._request_with_retry(
                f"{COINGECKO_API}/coins/{coin_id}/market_chart",
                params={"vs_currency": "usd", "days": str(days)},
            )
            if not data:
                return None

            raw_prices = data.get("prices", [])
            if len(raw_prices) < 7:
                return None

            # CoinGecko returns different granularity based on `days`:
            #   1 day  -> 5-min intervals
            #   2-90   -> hourly intervals (~24 points/day)
            #   91+    -> daily intervals
            # We need to sample daily closing prices to get proper daily volatility.
            n_points = len(raw_prices)
            span_ms = raw_prices[-1][0] - raw_prices[0][0] if n_points > 1 else 0
            span_days = span_ms / (1000 * 86400) if span_ms > 0 else days
            points_per_day = n_points / max(span_days, 1)

            # Sample approximately one price per day
            step = max(1, round(points_per_day))
            daily_prices = [raw_prices[i][1] for i in range(0, n_points, step)]

            if len(daily_prices) < 5:
                return None

            # Cache daily price series for momentum estimation
            self._price_cache[f"daily_prices_{symbol}"] = daily_prices

            # Calculate daily log returns
            log_returns = []
            for i in range(1, len(daily_prices)):
                if daily_prices[i - 1] > 0 and daily_prices[i] > 0:
                    log_returns.append(math.log(daily_prices[i] / daily_prices[i - 1]))

            if len(log_returns) < 5:
                return None

            # Daily volatility (std dev of log returns)
            mean_ret = sum(log_returns) / len(log_returns)
            variance = sum((r - mean_ret) ** 2 for r in log_returns) / (len(log_returns) - 1)
            daily_vol = math.sqrt(variance)

            # Annualize: daily_vol * sqrt(365) for crypto (trades 365 days)
            annual_vol = daily_vol * math.sqrt(365)

            self._price_cache[cache_key] = annual_vol
            self._price_cache[f"{cache_key}_time"] = time.time()

            logger.debug(f"{symbol} volatility: {annual_vol:.1%} annualized ({daily_vol:.2%} daily)")
            return annual_vol

        except Exception as e:
            logger.warning(f"Volatility fetch failed for {symbol}: {e}")
            return None

    def estimate_probability(self, symbol: str, current_price: float,
                             target_price: float, days_left: int,
                             direction: str = "above") -> Optional[float]:
        """Estimate probability of price reaching target using log-normal model.

        Backtested improvements over naive GBM:
        - 2.2x volatility multiplier (crypto vol is systematically underestimated
          by short lookback windows — backtested optimal on 2600+ resolved markets)
        - Momentum-adjusted drift instead of fixed 5% (blended 30d/90d momentum
          shrunk 50% toward zero for mean-reversion prior)
        - Uses max(30d, 90d) realized vol as base
        """
        if current_price <= 0 or target_price <= 0 or days_left <= 0:
            return None

        # Use the higher of 30-day and 90-day vol to avoid underestimating
        # long-term uncertainty from calm recent periods
        vol_30 = self.get_historical_volatility(symbol, days=30)
        vol_90 = self.get_historical_volatility(symbol, days=90) if days_left > 30 else None
        vol = max(filter(None, [vol_30, vol_90]), default=None)
        if vol is None:
            return None

        T = days_left / 365.0  # Time in years

        # Backtested vol multiplier on 2600+ resolved Polymarket markets (with momentum drift).
        # Optimal per horizon: 3d=1.6x, 7d=1.6x, 14d=1.8x, 30d=2.4x.
        # At very long horizons GBM's sigma^2*T term dominates and produces absurd medians,
        # so we cap the multiplier and let the momentum term carry directional info instead.
        if days_left <= 7:
            vol_mult = 1.6
        elif days_left <= 14:
            vol_mult = 1.8
        elif days_left <= 30:
            vol_mult = 2.0
        elif days_left <= 90:
            vol_mult = 1.8  # Taper back — GBM breaks down at extreme vol*sqrt(T)
        else:
            vol_mult = 1.5  # Long-dated: mild correction only
        sigma = vol * vol_mult

        # Momentum-adjusted drift: blend recent 30d/90d returns, shrink toward zero
        # This captures trending regimes while not over-extrapolating
        mu = 0.0  # Default: no drift (safer than assuming 5% annual)
        # Momentum from cached daily price series (stored by get_historical_volatility)
        daily_key = f"daily_prices_{symbol}"
        cached_daily = self._price_cache.get(daily_key, [])
        if len(cached_daily) >= 30:
            # 30-day momentum (annualized)
            ret_30 = math.log(cached_daily[-1] / cached_daily[-30]) if cached_daily[-30] > 0 else 0
            mom_30 = ret_30 * (365 / 30)
            if len(cached_daily) >= 90:
                ret_90 = math.log(cached_daily[-1] / cached_daily[-90]) if cached_daily[-90] > 0 else 0
                mom_90 = ret_90 * (365 / 90)
                raw_mu = 0.6 * mom_30 + 0.4 * mom_90
            else:
                raw_mu = mom_30
            # Shrink 50% toward zero (mean-reversion prior)
            mu = max(-2.0, min(2.0, raw_mu * 0.5))

        # d2 = (ln(S/K) + (mu - sigma^2/2) * T) / (sigma * sqrt(T))
        try:
            d2 = (math.log(current_price / target_price) + (mu - sigma**2 / 2) * T) / (sigma * math.sqrt(T))
        except (ValueError, ZeroDivisionError):
            return None

        # N(d2) using approximation of standard normal CDF
        prob_above = _normal_cdf(d2)

        if direction == "above":
            return prob_above
        else:  # "below"
            return 1.0 - prob_above


def _normal_cdf(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz & Stegun).

    Accurate to ~1e-7. Avoids scipy dependency.
    """
    # Constants
    a1 = 0.254829592
    a2 = -0.284496736
    a3 = 1.421413741
    a4 = -1.453152027
    a5 = 1.061405429
    p = 0.3275911

    sign = 1
    if x < 0:
        sign = -1
    x = abs(x)

    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x / 2)

    return 0.5 * (1.0 + sign * y)


class SportsOddsClient:
    """Fetches bookmaker odds from The Odds API for cross-referencing.

    Free tier: 500 requests/month. Supports soccer, basketball, baseball,
    hockey, MMA, esports, and more. Returns odds from Pinnacle, Betfair,
    DraftKings, FanDuel, and other sharp books.
    """

    ODDS_API_URL = "https://api.the-odds-api.com/v4"

    SPORT_KEYS = {
        "soccer_epl": "soccer_epl",
        "soccer_uefa_champs_league": "soccer_uefa_champs_league",
        "soccer_spain_la_liga": "soccer_spain_la_liga",
        "soccer_germany_bundesliga": "soccer_germany_bundesliga",
        "soccer_italy_serie_a": "soccer_italy_serie_a",
        "soccer_france_ligue_one": "soccer_france_ligue_one",
        "soccer_uefa_europa_league": "soccer_uefa_europa_league",
        "basketball_nba": "basketball_nba",
        "basketball_ncaab": "basketball_ncaab",
        "baseball_mlb": "baseball_mlb",
        "icehockey_nhl": "icehockey_nhl",
        "mma_mixed_martial_arts": "mma_mixed_martial_arts",
        "americanfootball_nfl": "americanfootball_nfl",
        "soccer_fifa_world_cup": "soccer_fifa_world_cup",
    }

    TEAM_ALIASES = {
        "man city": "manchester city", "man utd": "manchester united",
        "man united": "manchester united", "spurs": "tottenham hotspur",
        "tottenham": "tottenham hotspur", "wolves": "wolverhampton wanderers",
        "wolverhampton": "wolverhampton wanderers",
        "atletico": "atletico madrid", "atletico madrid": "atletico madrid",
        "barca": "barcelona", "fc barcelona": "barcelona",
        "real": "real madrid", "psg": "paris saint-germain",
        "paris saint germain": "paris saint-germain",
        "bayern": "bayern munich", "bayern munchen": "bayern munich",
        "inter": "inter milan", "ac milan": "ac milan",
        "juve": "juventus", "napoli": "napoli",
        "dortmund": "borussia dortmund", "bvb": "borussia dortmund",
        "lakers": "los angeles lakers", "celtics": "boston celtics",
        "warriors": "golden state warriors", "knicks": "new york knicks",
        "nets": "brooklyn nets", "sixers": "philadelphia 76ers",
        "76ers": "philadelphia 76ers", "heat": "miami heat",
        "bucks": "milwaukee bucks", "thunder": "oklahoma city thunder",
        "nuggets": "denver nuggets", "suns": "phoenix suns",
        "mavs": "dallas mavericks", "mavericks": "dallas mavericks",
        "yankees": "new york yankees", "dodgers": "los angeles dodgers",
        "red sox": "boston red sox", "cubs": "chicago cubs",
        "diamondbacks": "arizona diamondbacks", "d-backs": "arizona diamondbacks",
        "chelsea": "chelsea", "arsenal": "arsenal", "liverpool": "liverpool",
        "west ham": "west ham united", "newcastle": "newcastle united",
        "aston villa": "aston villa", "everton": "everton",
        "leicester": "leicester city", "crystal palace": "crystal palace",
    }

    def __init__(self):
        self.api_key = ODDS_API_KEY
        self.session = requests.Session()
        self._odds_cache = {}
        self._cache_time = 0
        self._remaining_requests = None

    def _normalize_team(self, name: str) -> str:
        name = name.lower().strip()
        return self.TEAM_ALIASES.get(name, name)

    def get_upcoming_odds(self) -> dict:
        """Fetch odds for all supported sports. Returns {normalized_matchup: {home_prob, away_prob, draw_prob, source}}."""
        if not self.api_key:
            return {}

        now = time.time()
        if self._odds_cache and (now - self._cache_time) < 600:
            return self._odds_cache

        all_odds = {}
        for sport_key in self.SPORT_KEYS.values():
            try:
                resp = self.session.get(
                    f"{self.ODDS_API_URL}/sports/{sport_key}/odds",
                    params={
                        "apiKey": self.api_key,
                        "regions": "us,eu,uk",
                        "markets": "h2h",
                        "oddsFormat": "decimal",
                        "bookmakers": "pinnacle,betfair_ex_eu,draftkings,fanduel,williamhill_us",
                    },
                    timeout=15,
                )
                if resp.status_code == 401:
                    logger.warning("Odds API key invalid or missing")
                    return {}
                if resp.status_code == 429:
                    logger.warning("Odds API rate limit reached")
                    break
                if resp.status_code != 200:
                    continue

                self._remaining_requests = resp.headers.get("x-requests-remaining")
                events = resp.json()

                for event in events:
                    home = self._normalize_team(event.get("home_team", ""))
                    away = self._normalize_team(event.get("away_team", ""))
                    commence = event.get("commence_time", "")

                    sharp_probs = self._extract_sharp_probs(event.get("bookmakers", []))
                    if sharp_probs:
                        matchup_key = f"{home} vs {away}"
                        all_odds[matchup_key] = {
                            **sharp_probs,
                            "home_team": home,
                            "away_team": away,
                            "commence_time": commence,
                            "sport": sport_key,
                        }
                        reverse_key = f"{away} vs {home}"
                        all_odds[reverse_key] = {
                            "home_prob": sharp_probs.get("away_prob", 0),
                            "away_prob": sharp_probs.get("home_prob", 0),
                            "draw_prob": sharp_probs.get("draw_prob", 0),
                            "home_team": away,
                            "away_team": home,
                            "commence_time": commence,
                            "sport": sport_key,
                        }

                time.sleep(0.3)
            except Exception as e:
                logger.debug(f"Failed to fetch odds for {sport_key}: {e}")
                continue

        self._odds_cache = all_odds
        self._cache_time = time.time()
        logger.info(f"Fetched odds for {len(all_odds) // 2} upcoming matches across {len(self.SPORT_KEYS)} sports "
                    f"(API requests remaining: {self._remaining_requests})")
        return all_odds

    def _extract_sharp_probs(self, bookmakers: list) -> Optional[dict]:
        """Extract implied probabilities from sharpest available bookmaker.

        Priority: Pinnacle (sharpest) > Betfair > others.
        Converts decimal odds to implied probability, removes vig using
        multiplicative method (standard industry approach).
        """
        sharp_order = ["pinnacle", "betfair_ex_eu", "williamhill_us", "draftkings", "fanduel"]
        for target_book in sharp_order:
            for book in bookmakers:
                if book.get("key") == target_book:
                    markets = book.get("markets", [])
                    for market in markets:
                        if market.get("key") == "h2h":
                            outcomes = market.get("outcomes", [])
                            if len(outcomes) < 2:
                                continue
                            raw_probs = {}
                            for o in outcomes:
                                price = o.get("price", 0)
                                if price > 1:
                                    raw_probs[o["name"]] = 1.0 / price
                            total = sum(raw_probs.values())
                            if total <= 0:
                                continue
                            fair_probs = {k: v / total for k, v in raw_probs.items()}
                            result = {"home_prob": 0, "away_prob": 0, "draw_prob": 0}
                            for name, prob in fair_probs.items():
                                name_lower = name.lower()
                                if name_lower == "draw":
                                    result["draw_prob"] = prob
                                elif len(fair_probs) == 2:
                                    if result["home_prob"] == 0:
                                        result["home_prob"] = prob
                                    else:
                                        result["away_prob"] = prob
                                else:
                                    if result["home_prob"] == 0:
                                        result["home_prob"] = prob
                                    elif result["away_prob"] == 0:
                                        result["away_prob"] = prob
                            result["source"] = target_book
                            return result
        return None

    def find_match_odds(self, team1: str, team2: str = "") -> Optional[dict]:
        """Find odds for a specific matchup by team name fuzzy matching."""
        odds = self.get_upcoming_odds()
        if not odds:
            return None

        t1 = self._normalize_team(team1)
        t2 = self._normalize_team(team2) if team2 else ""

        for key, data in odds.items():
            home = data.get("home_team", "")
            away = data.get("away_team", "")
            if t1 in home or t1 in away:
                if not t2 or t2 in home or t2 in away:
                    return data
            if t1 in key:
                if not t2 or t2 in key:
                    return data

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
        self.sports = SportsOddsClient()

    def fetch_all_active_markets(self, limit: int = None) -> list[dict]:
        """Fetch ALL active markets from Polymarket Gamma API.

        FIX: Previously capped at 1000, missing half the markets.
        Now paginates until no more results.
        """
        all_markets = []
        offset = 0
        batch_size = 100
        max_markets = limit or 10000  # Safety cap

        while offset < max_markets:
            try:
                resp = self.session.get(
                    f"{GAMMA_API_URL}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": batch_size,
                        "offset": offset,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                all_markets.extend(batch)
                offset += batch_size
                if len(batch) < batch_size:
                    break  # Last page
                time.sleep(0.3)
            except Exception as e:
                logger.error(f"Failed to fetch markets at offset {offset}: {e}")
                break

        logger.info(f"Fetched {len(all_markets)} active markets (full pagination)")
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
        sports_keywords = [
            "win the", "nba", "nfl", "mlb", "nhl", "fifa", "premier league",
            "champions league", "world cup", "super bowl", "playoff",
            "la liga", "serie a", "bundesliga", "ligue 1", "europa league",
            "ncaa", "ncaab", "march madness", " vs.", " vs ",
            "f1 ", "formula 1", "grand prix", "mma", "ufc",
            "boxing", "tennis", "atp", "wta", "wimbledon",
            "mvp", "champion", "championship",
            "dodgers", "yankees", "lakers", "celtics", "warriors",
            "arsenal", "chelsea", "liverpool", "manchester", "real madrid",
            "barcelona", "bayern", "juventus", "psg", "inter milan",
            "esports", "cs2", "counter-strike", "valorant", "league of legends",
        ]
        if any(kw in question for kw in sports_keywords):
            return "sports"
        if any(t in tags for t in ["sports", "soccer", "football", "basketball",
                                    "baseball", "hockey", "esports", "mma", "tennis"]):
            return "sports"
        politics_keywords = [
            "president", "trump", "biden", "election", "congress", "senate",
            "governor", "democrat", "republican", "vote", "parliamentary",
            "prime minister", "cabinet", "impeach", "nominee", "ballot",
        ]
        if any(kw in question for kw in politics_keywords):
            return "politics"
        finance_keywords = [
            "fed ", "federal reserve", "interest rate", "bps", "basis point",
            "ipo", "s&p 500", "nasdaq", "dow jones", "crude oil",
            "gdp", "inflation", "cpi", "fomc", "tariff",
        ]
        if any(kw in question for kw in finance_keywords):
            return "finance"
        return "event"

    def analyze_crypto_market(self, market: dict) -> Optional[Opportunity]:
        """Analyze a crypto prediction market against real price data."""
        question = market.get("question", "")
        desc = market.get("description", "").lower()

        # Skip 50-50 resolution markets (GTA VI traps, etc.)
        if "50-50" in desc or "50/50" in desc:
            return None
        prices = self.crypto.get_prices()

        # Pattern: "Will Bitcoin hit $X by DATE?" or "Bitcoin above $X" etc.
        price_match = re.search(
            r'\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|doge|dogecoin|'
            r'cardano|ada|avalanche|avax|polkadot|dot|chainlink|link|'
            r'bnb|binance|litecoin|ltc|near|sui|aptos|apt|'
            r'pepe|bonk|shib|shiba|dogwifhat|wif|'
            r'render|bittensor|tao|kaspa|kas|injective|inj|'
            r'sei|celestia|tia|optimism|arbitrum|arb|uniswap|uni|'
            r'fetch|fet|cosmos|atom)\b.*?'
            r'\$([\d,]+(?:\.\d+)?[kmb]?)',
            question.lower()
        )
        if not price_match:
            return None

        # Map to symbol
        name_to_symbol = {
            "bitcoin": "BTC", "btc": "BTC",
            "ethereum": "ETH", "eth": "ETH",
            "solana": "SOL", "sol": "SOL",
            "xrp": "XRP", "ripple": "XRP",
            "doge": "DOGE", "dogecoin": "DOGE",
            "cardano": "ADA", "ada": "ADA",
            "avalanche": "AVAX", "avax": "AVAX",
            "polkadot": "DOT", "dot": "DOT",
            "chainlink": "LINK", "link": "LINK",
            "bnb": "BNB", "binance": "BNB",
            "litecoin": "LTC", "ltc": "LTC",
            "near": "NEAR", "sui": "SUI",
            "aptos": "APT", "apt": "APT",
            "pepe": "PEPE", "bonk": "BONK",
            "shib": "SHIB", "shiba": "SHIB",
            "dogwifhat": "WIF", "wif": "WIF",
            "render": "RENDER", "bittensor": "TAO", "tao": "TAO",
            "kaspa": "KAS", "kas": "KAS",
            "injective": "INJ", "inj": "INJ",
            "sei": "SEI", "celestia": "TIA", "tia": "TIA",
            "optimism": "OP", "arbitrum": "ARB", "arb": "ARB",
            "uniswap": "UNI", "uni": "UNI",
            "fetch": "FET", "fet": "FET",
            "cosmos": "ATOM", "atom": "ATOM",
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

        # Sanity check: reject year-like values (2020-2035) that aren't real price targets
        # Also reject targets < $1 for BTC/ETH (clearly not a price target)
        if 2020 <= target_price <= 2035 and multiplier == 1:
            logger.debug(f"Skipping likely year extracted as price: ${target_price:.0f} in '{question[:60]}'")
            return None

        current_price = prices[symbol]["price"]
        change_24h = prices[symbol].get("change_24h", 0)

        # Calculate distance to target
        pct_to_target = (target_price - current_price) / current_price

        # Get market end date
        end_date = market.get("endDate", "")[:10]
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_left = (end_dt - datetime.now(timezone.utc)).days
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
        q_lower = question.lower()
        is_above = any(kw in q_lower for kw in ["above", "over", "higher", "exceed", "surpass", "rise", "pump", "rally"])
        is_below = any(kw in q_lower for kw in ["below", "under", "fall", "drop", "crash", "dip", "sink", "decline", "plunge"])

        # === VOLATILITY-BASED PROBABILITY MODEL ===
        # Uses log-normal distribution with actual historical volatility from CoinGecko.
        # Same math as Black-Scholes: P(S_T > K) = N(d2)
        # Each coin gets its own volatility — BTC and DOGE are treated completely differently.
        if is_below and not is_above:
            direction = "below"
        elif is_above and not is_below:
            direction = "above"
        else:
            # Ambiguous ("hit", "reach") — use price context to disambiguate
            if current_price > target_price * 1.05:
                direction = "below"  # Target well below current = asking about a drop
            else:
                direction = "above"

        # Handle already-hit targets
        # Format prices smartly: $74,501 for BTC, $0.1005 for DOGE
        def fmt_price(p):
            if p >= 1:
                return f"${p:,.0f}"
            elif p >= 0.01:
                return f"${p:.4f}"
            else:
                return f"${p:.6f}"

        if (direction == "above" and current_price >= target_price) or \
           (direction == "below" and current_price <= target_price):
            # Target already met — use model to estimate probability of STAYING there
            # (price can reverse before deadline). Don't hardcode 0.95.
            vol_prob = self.crypto.estimate_probability(
                symbol, current_price, target_price, max(days_left, 1), direction
            )
            estimated_prob = vol_prob if vol_prob is not None else 0.85
            confidence = "high" if estimated_prob > 0.80 else "medium"
            reasoning = (
                f"{symbol} already at {fmt_price(current_price)}, "
                f"{'above' if direction == 'above' else 'below'} target {fmt_price(target_price)} | "
                f"stay_prob={estimated_prob:.1%}"
            )
        else:
            # Try volatility model first
            vol_prob = self.crypto.estimate_probability(
                symbol, current_price, target_price, max(days_left, 1), direction
            )

            if vol_prob is not None:
                estimated_prob = vol_prob
                vol_value = self.crypto.get_historical_volatility(symbol)
                vol_display = f"{vol_value:.0%}" if vol_value else "?"

                # Model confidence assessment:
                # Our zero-drift model is ACCURATE for downside/crash predictions
                # but UNDERESTIMATES upside for reasonable targets.
                # Highest confidence: extreme tail events (very low or very high prob)
                # where the market is likely pricing hype/fear, not fundamentals.

                # How far is the target from current price?
                abs_pct = abs(pct_to_target)

                if direction == "below":
                    # Downside model is well-calibrated (matches market within 1%)
                    if estimated_prob < 0.15:
                        confidence = "high"
                    elif estimated_prob < 0.30:
                        confidence = "medium"
                    else:
                        confidence = "low"
                elif abs_pct > 0.5:
                    # Extreme upside targets (>50% move): model is reliable
                    # because these are tail events where market often overprices hype
                    if estimated_prob < 0.15:
                        confidence = "high"
                    else:
                        confidence = "medium"
                elif abs_pct < 0.15:
                    # Small moves: market is probably right (drift dominates)
                    # Our model underestimates these, don't trust our edge
                    confidence = "low"
                else:
                    # Medium moves: uncertain, moderate confidence
                    confidence = "medium" if estimated_prob < 0.25 else "low"

                reasoning = (
                    f"{symbol} {fmt_price(current_price)} -> {fmt_price(target_price)} "
                    f"({pct_to_target:+.1%}) in {days_left}d | "
                    f"vol={vol_display} | model_prob={estimated_prob:.1%}"
                )
            else:
                # Fallback: simple distance-based estimate (no volatility data available)
                if direction == "above":
                    if pct_to_target > 1.0:
                        estimated_prob = 0.02
                    elif pct_to_target > 0.5:
                        estimated_prob = 0.08
                    else:
                        estimated_prob = max(0.05, 0.4 - pct_to_target * 2)
                else:
                    drop_needed = (current_price - target_price) / current_price
                    estimated_prob = max(0.05, 0.3 - drop_needed * 2)

                confidence = "low"
                reasoning = f"{symbol} {fmt_price(current_price)} -> {fmt_price(target_price)} ({days_left}d) [no vol data, fallback model]"

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
            fee_rate = CONFIG.get("fee_rate", 0.02)
            net_win = ((1.0 - market_price) / market_price) * (1.0 - fee_rate)
            b = net_win
            q = 1.0 - estimated_prob
            kelly_full = (estimated_prob * b - q) / b if b > 0 else 0
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

        # FIX: Match outcomes by name, don't assume ordering
        # Bug: outcomes[0] was assumed to be YES, but Gamma API doesn't guarantee order
        yes_price = 0
        no_price = 0
        for i, outcome in enumerate(outcomes):
            price = float(outcome_prices[i]) if i < len(outcome_prices) else 0
            if outcome.lower() == "yes":
                yes_price = price
            elif outcome.lower() == "no":
                no_price = price

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

        # Skip penny markets — prices under 5c are untradeable noise
        # Research: favorite-longshot bias means longshots are OVERPRICED
        if yes_price < 0.05 and no_price < 0.05:
            return None

        # Look for "almost certain NO" markets where YES is still priced > 3c
        # These are often free money — things that won't happen
        q_lower = question.lower()

        # TRAP DETECTION: "before GTA VI" and similar conditional markets
        # resolve 50-50 if neither event happens — NOT to NO!
        # Buying NO at 51c when it resolves to 50c = guaranteed loss
        conditional_traps = ["before gta", "before gta vi", "50-50", "50/50"]
        if any(kw in q_lower for kw in conditional_traps):
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
            days_left = (end_dt - datetime.now(timezone.utc).replace(tzinfo=None)).days

            if days_left <= 3 and yes_price > 0.10:
                # Market closing in 3 days with YES still > 10% — check if it's resolvable
                # These often present NO opportunities as events haven't happened
                near_expiry_edge = round(0.85 - no_price, 4)
                if not any(kw in q_lower for kw in ["will", "by"]):
                    pass  # Skip if we can't determine directionality
                elif volume > 50000 and near_expiry_edge > CONFIG["entry_threshold"]:
                    return Opportunity(
                        category="event",
                        market_question=question,
                        market_id=market.get("conditionId", ""),
                        token_id=no_token_id,
                        side="NO",
                        market_price=no_price,
                        estimated_prob=0.85,
                        edge=near_expiry_edge,
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

    def analyze_sports_market(self, market: dict) -> Optional[Opportunity]:
        """Cross-reference Polymarket sports prices against bookmaker odds.

        Strategy: top Polymarket traders make millions by finding mispricings
        between Polymarket and sharp bookmakers (Pinnacle, Betfair). When
        Polymarket prices diverge from sharp book implied probabilities by
        more than the fee threshold, there's an edge.
        """
        question = market.get("question", "")
        outcomes = json.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
        outcome_prices = json.loads(market.get("outcomePrices", "[]")) if isinstance(market.get("outcomePrices"), str) else market.get("outcomePrices", [])

        if len(outcomes) < 2 or len(outcome_prices) < 2:
            return None

        volume = float(market.get("volume", 0))
        liquidity = float(market.get("liquidity", 0))
        end_date = market.get("endDate", "")[:10]

        if volume < CONFIG.get("sports_min_volume", 5000):
            return None
        if liquidity < CONFIG.get("sports_min_liquidity", 200):
            return None

        tokens = market.get("tokens", [])
        token_map = {}
        for token in tokens:
            token_map[token.get("outcome", "").lower()] = token.get("token_id", "")

        outcome_data = {}
        for i, outcome in enumerate(outcomes):
            price = float(outcome_prices[i]) if i < len(outcome_prices) else 0
            outcome_data[outcome] = price

        teams = self._extract_teams_from_question(question, outcomes)
        if not teams:
            return self._analyze_sports_near_expiry(market, outcomes, outcome_prices, token_map, volume, liquidity, end_date)

        odds_data = self.sports.find_match_odds(teams[0], teams[1] if len(teams) > 1 else "")
        if not odds_data:
            return self._analyze_sports_near_expiry(market, outcomes, outcome_prices, token_map, volume, liquidity, end_date)

        best_opp = None
        best_edge = 0

        for outcome_name, poly_price in outcome_data.items():
            if poly_price < 0.05 or poly_price > 0.95:
                continue

            book_prob = self._match_outcome_to_odds(outcome_name, odds_data, question)
            if book_prob is None or book_prob <= 0:
                continue

            edge = book_prob - poly_price
            min_edge = CONFIG.get("sports_min_edge", 0.04)

            if edge > min_edge and edge > best_edge:
                fee_rate = CONFIG.get("fee_rate", 0.02)
                net_win = (1.0 - poly_price) * (1.0 - fee_rate)
                loss = poly_price
                kelly_full = (book_prob * net_win - (1 - book_prob) * loss) / net_win if net_win > 0 else 0
                kelly_size = max(0, kelly_full * CONFIG["kelly_fraction"] * CONFIG["bankroll"])
                kelly_size = min(kelly_size, CONFIG["max_position_usd"])

                if kelly_size < 0.10:
                    continue

                side = outcome_name.upper() if outcome_name.upper() in ("YES", "NO") else "YES"
                token_id = token_map.get(outcome_name.lower(), "")

                confidence = "high" if edge > 0.08 else "medium"

                best_opp = Opportunity(
                    category="sports",
                    market_question=question,
                    market_id=market.get("conditionId", market.get("condition_id", "")),
                    token_id=token_id,
                    side=side,
                    market_price=round(poly_price, 4),
                    estimated_prob=round(book_prob, 4),
                    edge=round(edge, 4),
                    confidence=confidence,
                    reasoning=(f"Bookmaker edge: {odds_data.get('source', 'unknown')} implies "
                              f"{book_prob:.1%} vs Polymarket {poly_price:.1%} "
                              f"({edge:.1%} edge). Vol ${volume:,.0f}"),
                    volume=volume,
                    liquidity=liquidity,
                    end_date=end_date,
                    kelly_size=round(kelly_size, 2),
                )
                best_edge = edge

        return best_opp

    def _extract_teams_from_question(self, question: str, outcomes: list) -> list[str]:
        """Extract team names from market question or outcomes."""
        vs_match = re.search(r'(.+?)\s+(?:vs\.?|versus)\s+(.+?)(?:\?|$|\s+[-–])', question, re.IGNORECASE)
        if vs_match:
            return [vs_match.group(1).strip(), vs_match.group(2).strip()]

        clean_outcomes = [o for o in outcomes if o.lower() not in ("yes", "no", "draw")]
        if len(clean_outcomes) >= 2:
            return clean_outcomes[:2]
        if len(clean_outcomes) == 1:
            return clean_outcomes

        return []

    def _match_outcome_to_odds(self, outcome_name: str, odds_data: dict, question: str) -> Optional[float]:
        """Match a Polymarket outcome to bookmaker probability."""
        outcome_lower = self.sports._normalize_team(outcome_name)
        home = odds_data.get("home_team", "")
        away = odds_data.get("away_team", "")

        if outcome_lower == "draw" or outcome_name.lower() == "draw":
            return odds_data.get("draw_prob", 0) or None

        if outcome_lower in home or home in outcome_lower:
            return odds_data.get("home_prob")
        if outcome_lower in away or away in outcome_lower:
            return odds_data.get("away_prob")

        for word in outcome_lower.split():
            if len(word) > 3:
                if word in home:
                    return odds_data.get("home_prob")
                if word in away:
                    return odds_data.get("away_prob")

        if outcome_name.lower() == "yes":
            q_lower = question.lower()
            if home in q_lower and away not in q_lower:
                return odds_data.get("home_prob")
            if away in q_lower and home not in q_lower:
                return odds_data.get("away_prob")

        return None

    def _analyze_sports_near_expiry(self, market: dict, outcomes: list, outcome_prices: list,
                                     token_map: dict, volume: float, liquidity: float,
                                     end_date: str) -> Optional[Opportunity]:
        """Fallback for sports markets without bookmaker odds — use near-expiry logic.

        Sports favorites at 85-95c resolving soon are often underpriced
        (favorite-longshot bias applies to sports too).
        """
        if not end_date:
            return None
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
        except (ValueError, TypeError):
            return None

        if hours_left < 0 or hours_left > 48:
            return None

        for i, outcome in enumerate(outcomes):
            price = float(outcome_prices[i]) if i < len(outcome_prices) else 0
            if 0.85 <= price <= 0.95 and volume > 10000:
                estimated_prob = min(0.98, price + 0.04)
                edge = estimated_prob - price
                if edge < CONFIG.get("sports_min_edge", 0.04):
                    continue

                fee_rate = CONFIG.get("fee_rate", 0.02)
                net_win = (1.0 - price) * (1.0 - fee_rate)
                kelly_full = (estimated_prob * net_win - (1 - estimated_prob) * price) / net_win if net_win > 0 else 0
                kelly_size = max(0, kelly_full * CONFIG["kelly_fraction"] * CONFIG["bankroll"])
                kelly_size = min(kelly_size, CONFIG["max_position_usd"])

                if kelly_size < 0.10:
                    continue

                side = outcome.upper() if outcome.upper() in ("YES", "NO") else "YES"
                token_id = token_map.get(outcome.lower(), "")

                return Opportunity(
                    category="sports",
                    market_question=market.get("question", ""),
                    market_id=market.get("conditionId", market.get("condition_id", "")),
                    token_id=token_id,
                    side=side,
                    market_price=round(price, 4),
                    estimated_prob=round(estimated_prob, 4),
                    edge=round(edge, 4),
                    confidence="medium",
                    reasoning=(f"Sports near-expiry: {hours_left:.0f}h left, "
                              f"favorite at {price:.1%}, vol ${volume:,.0f}"),
                    volume=volume,
                    liquidity=liquidity,
                    end_date=end_date,
                    kelly_size=round(kelly_size, 2),
                )

        return None

    def analyze_finance_market(self, market: dict) -> Optional[Opportunity]:
        """Analyze finance/macro markets (Fed decisions, oil prices, etc).

        Uses consensus data: Fed futures imply probabilities for rate decisions,
        oil futures for price targets, etc.
        """
        question = market.get("question", "")
        outcomes = json.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
        outcome_prices = json.loads(market.get("outcomePrices", "[]")) if isinstance(market.get("outcomePrices"), str) else market.get("outcomePrices", [])

        if len(outcomes) < 2 or len(outcome_prices) < 2:
            return None

        volume = float(market.get("volume", 0))
        liquidity = float(market.get("liquidity", 0))
        end_date = market.get("endDate", "")[:10]

        if volume < 5000 or liquidity < 200:
            return None

        tokens = market.get("tokens", [])
        token_map = {}
        for token in tokens:
            token_map[token.get("outcome", "").lower()] = token.get("token_id", "")

        yes_price = 0
        no_price = 0
        for i, outcome in enumerate(outcomes):
            price = float(outcome_prices[i]) if i < len(outcome_prices) else 0
            if outcome.lower() == "yes":
                yes_price = price
            elif outcome.lower() == "no":
                no_price = price

        q_lower = question.lower()

        fed_patterns = ["fed ", "fomc", "interest rate", "rate cut", "rate hike", "basis point", "bps"]
        if any(p in q_lower for p in fed_patterns):
            if "no change" in q_lower or "hold" in q_lower or "unchanged" in q_lower:
                if yes_price > 0.90:
                    estimated_prob = min(0.98, yes_price + 0.03)
                    edge = estimated_prob - yes_price
                    if edge > CONFIG["entry_threshold"]:
                        return Opportunity(
                            category="finance",
                            market_question=question,
                            market_id=market.get("conditionId", ""),
                            token_id=token_map.get("yes", ""),
                            side="YES",
                            market_price=round(yes_price, 4),
                            estimated_prob=round(estimated_prob, 4),
                            edge=round(edge, 4),
                            confidence="high",
                            reasoning=f"Fed hold consensus strong at {yes_price:.1%}, vol ${volume:,.0f}",
                            volume=volume,
                            liquidity=liquidity,
                            end_date=end_date,
                            kelly_size=min(CONFIG["max_position_usd"],
                                          CONFIG["kelly_fraction"] * CONFIG["bankroll"]),
                        )

        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_left = (end_dt - datetime.now(timezone.utc)).days
            if days_left <= 3 and volume > 20000:
                for i, outcome in enumerate(outcomes):
                    price = float(outcome_prices[i]) if i < len(outcome_prices) else 0
                    if 0.88 <= price <= 0.96:
                        estimated_prob = min(0.98, price + 0.04)
                        edge = estimated_prob - price
                        if edge > CONFIG["entry_threshold"]:
                            side = outcome.upper() if outcome.upper() in ("YES", "NO") else "YES"
                            return Opportunity(
                                category="finance",
                                market_question=question,
                                market_id=market.get("conditionId", ""),
                                token_id=token_map.get(outcome.lower(), ""),
                                side=side,
                                market_price=round(price, 4),
                                estimated_prob=round(estimated_prob, 4),
                                edge=round(edge, 4),
                                confidence="medium",
                                reasoning=f"Finance near-expiry: {days_left}d left, {price:.1%}, vol ${volume:,.0f}",
                                volume=volume,
                                liquidity=liquidity,
                                end_date=end_date,
                                kelly_size=min(CONFIG["max_position_usd"],
                                              CONFIG["kelly_fraction"] * CONFIG["bankroll"] * 0.5),
                            )
        except (ValueError, TypeError):
            pass

        return None

    def analyze_near_expiry(self, market: dict) -> Optional[Opportunity]:
        """Near-expiry harvesting: buy favorites at $0.88-$0.93 resolving within 2 days.

        Research (IMDEA 2025): $40M extracted via fast capital turnover, not large mispricings.
        Fee-adjusted sweet spot: $0.88-$0.93 entry (at $0.97+ fees eat the margin).
        Favorite-longshot bias (Snowberg & Wolfers): favorites are systematically underpriced.
        """
        end_date = market.get("endDate", "")[:10]
        if not end_date:
            return None

        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_left = (end_dt - datetime.now(timezone.utc)).days
        except (ValueError, TypeError):
            return None

        if days_left < 0 or days_left > CONFIG.get("near_expiry_max_days", 2):
            return None

        volume = float(market.get("volume", 0))
        if volume < CONFIG.get("near_expiry_min_volume", 10000):
            return None

        outcomes = json.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
        outcome_prices = json.loads(market.get("outcomePrices", "[]")) if isinstance(market.get("outcomePrices"), str) else market.get("outcomePrices", [])

        entry_low = CONFIG.get("near_expiry_entry_low", 0.88)
        entry_high = CONFIG.get("near_expiry_entry_high", 0.93)

        tokens = market.get("tokens", [])
        token_map = {}
        for token in tokens:
            token_map[token.get("outcome", "").lower()] = token.get("token_id", "")

        for i, outcome in enumerate(outcomes):
            price = float(outcome_prices[i]) if i < len(outcome_prices) else 0
            # Look for favorites in the sweet spot
            if entry_low <= price <= entry_high:
                # This is a favorite priced in our optimal range
                estimated_prob = min(0.98, price + 0.05)  # Conservative: assume slightly underpriced
                edge = estimated_prob - price

                # Fee-aware Kelly
                fee_rate = CONFIG.get("fee_rate", 0.02)
                net_win = (1.0 - price) * (1.0 - fee_rate)
                kelly_full = (estimated_prob * net_win - (1 - estimated_prob) * price) / net_win if net_win > 0 else 0
                kelly_size = max(0, kelly_full * CONFIG["kelly_fraction"] * CONFIG["bankroll"])
                kelly_size = min(kelly_size, CONFIG["max_position_usd"])

                if kelly_size < 0.10 or edge < CONFIG["entry_threshold"]:
                    continue

                side = outcome.upper() if outcome.upper() in ("YES", "NO") else "YES"
                token_id = token_map.get(outcome.lower(), "")

                return Opportunity(
                    category="near_expiry",
                    market_question=market.get("question", ""),
                    market_id=market.get("conditionId", market.get("condition_id", "")),
                    token_id=token_id,
                    side=side,
                    market_price=round(price, 4),
                    estimated_prob=round(estimated_prob, 4),
                    edge=round(edge, 4),
                    confidence="high",
                    reasoning=f"Near-expiry favorite: {days_left}d left, ${volume:,.0f} vol, fee-adj edge {edge:.1%}",
                    volume=volume,
                    liquidity=float(market.get("liquidity", 0)),
                    end_date=end_date,
                    kelly_size=round(kelly_size, 2),
                )

        return None

    def check_arbitrage(self, market: dict) -> Optional[Opportunity]:
        """Intra-market arbitrage: when YES + NO < $0.97, buy both for riskless profit.

        Must exceed 2% fee + slippage to be viable.
        Rare but riskless when found.
        """
        outcomes = json.loads(market.get("outcomes", "[]")) if isinstance(market.get("outcomes"), str) else market.get("outcomes", [])
        outcome_prices = json.loads(market.get("outcomePrices", "[]")) if isinstance(market.get("outcomePrices"), str) else market.get("outcomePrices", [])

        if len(outcomes) < 2 or len(outcome_prices) < 2:
            return None

        yes_price = 0
        no_price = 0
        for i, outcome in enumerate(outcomes):
            price = float(outcome_prices[i]) if i < len(outcome_prices) else 0
            if outcome.lower() == "yes":
                yes_price = price
            elif outcome.lower() == "no":
                no_price = price

        if yes_price <= 0 or no_price <= 0:
            return None

        total = yes_price + no_price
        # Profit = $1 - total cost. Must exceed 2% fee + buffer
        if total < 0.97:
            profit_per_dollar = 1.0 - total
            volume = float(market.get("volume", 0))
            if volume < 5000:
                return None

            tokens = market.get("tokens", [])
            # Buy the cheaper side for tracking purposes
            cheaper_side = "YES" if yes_price < no_price else "NO"
            cheaper_price = min(yes_price, no_price)
            token_id = ""
            for token in tokens:
                if token.get("outcome", "").lower() == cheaper_side.lower():
                    token_id = token.get("token_id", "")

            return Opportunity(
                category="arbitrage",
                market_question=market.get("question", ""),
                market_id=market.get("conditionId", market.get("condition_id", "")),
                token_id=token_id,
                side=cheaper_side,
                market_price=round(cheaper_price, 4),
                estimated_prob=0.99,  # Arbitrage is near-certain
                edge=round(profit_per_dollar, 4),
                confidence="high",
                reasoning=f"ARB: YES({yes_price:.2f})+NO({no_price:.2f})={total:.2f} < $1.00, profit={profit_per_dollar:.1%}",
                volume=volume,
                liquidity=float(market.get("liquidity", 0)),
                end_date=market.get("endDate", "")[:10],
                kelly_size=CONFIG["max_position_usd"],  # Max size for riskless trades
            )

        return None

    def scan_dutch_book(self) -> list[Opportunity]:
        """Dutch-book scanner: find multi-outcome events where all YES prices sum < $1.00 - fees.

        Fetches events (not individual markets) from the GAMMA API.
        For truly mutually exclusive outcomes (election winners, sentencing ranges, etc.),
        buying all outcomes guarantees profit if total cost + worst-case fee < $1.00.

        Fee model: Polymarket charges 2% on net winnings per winning leg only.
        Worst case fee = 0.02 * (1.0 - cheapest_outcome_price).
        """
        opportunities = []
        try:
            offset = 0
            all_events = []
            while offset < 2000:
                resp = self.session.get(
                    f"{GAMMA_API_URL}/events",
                    params={"active": "true", "closed": "false", "limit": 50, "offset": offset},
                    timeout=15,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                all_events.extend(batch)
                offset += 50
                if len(batch) < 50:
                    break
                time.sleep(0.3)
        except Exception as e:
            logger.warning(f"Failed to fetch events for Dutch-book scan: {e}")
            return []

        for event in all_events:
            markets = event.get("markets", [])
            if len(markets) < 3:
                continue  # Need 3+ outcomes for multi-outcome Dutch-book

            title = event.get("title", "")

            # Skip cumulative/threshold events — NOT mutually exclusive
            # "by ___?" = cumulative dates, "above ___" = cumulative thresholds
            title_lower = title.lower()
            if re.search(r'\bby\b.*\?', title, re.IGNORECASE):
                continue
            if any(kw in title_lower for kw in ["above", "below", "before", "when will", "hit $"]):
                continue

            # Check individual market titles for cumulative patterns
            # ">$X" or "by DATE" legs indicate thresholds, not mutually exclusive
            leg_titles = [m.get("groupItemTitle", m.get("question", "")).lower() for m in markets]
            is_cumulative = False
            for lt in leg_titles:
                if lt.startswith(">") or lt.startswith("<") or lt.startswith("above") or lt.startswith("below"):
                    is_cumulative = True
                    break
                # "by <date>" pattern in leg titles (e.g., "by March 31, 2026")
                if re.match(r'^by\s', lt):
                    is_cumulative = True
                    break
            if is_cumulative:
                continue

            # Skip FDV/market-cap threshold events (cumulative even without "above" in title)
            if any(kw in title_lower for kw in ["fdv", "market cap", "marketcap", "fully diluted"]):
                continue

            # Collect YES prices for each market in the event
            legs = []
            total_cost = 0
            min_price = 1.0
            max_price = 0.0
            min_liquidity = float('inf')
            total_volume = 0
            has_valid_prices = True

            for m in markets:
                prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
                if not prices or len(prices) < 1:
                    has_valid_prices = False
                    break

                yes_price = float(prices[0])
                if yes_price <= 0:
                    continue  # Skip zero-priced outcomes (already resolved)

                liq = float(m.get("liquidity", 0))
                vol = float(m.get("volume", 0))

                legs.append({
                    "question": m.get("groupItemTitle", m.get("question", "?"))[:60],
                    "yes_price": yes_price,
                    "market_id": m.get("conditionId", ""),
                    "liquidity": liq,
                })
                total_cost += yes_price
                min_price = min(min_price, yes_price)
                max_price = max(max_price, yes_price)
                min_liquidity = min(min_liquidity, liq)
                total_volume += vol

            if not has_valid_prices or len(legs) < 3:
                continue

            # Skip if any leg has <$50 liquidity (can't execute)
            if min_liquidity < 50:
                continue

            # Sanity check: truly mutually exclusive events should sum near 1.0
            # If sum is < 0.70, it's likely cumulative/threshold (not mutually exclusive)
            if total_cost < 0.70:
                continue

            # Fee calculation: worst case = cheapest outcome wins (highest fee)
            fee_rate = CONFIG.get("fee_rate", 0.02)
            worst_fee = fee_rate * (1.0 - min_price)
            best_fee = fee_rate * (1.0 - max_price)

            # Profit = $1.00 - total_cost - fee
            worst_profit = 1.0 - total_cost - worst_fee
            best_profit = 1.0 - total_cost - best_fee

            # Only flag if profitable even in worst case
            if worst_profit > 0.005:  # >0.5% minimum profit after fees
                leg_summary = " + ".join(f"{l['question'][:25]}={l['yes_price']:.3f}" for l in legs[:5])
                if len(legs) > 5:
                    leg_summary += f" +{len(legs)-5} more"

                opportunities.append(Opportunity(
                    category="dutch_book",
                    market_question=f"[DUTCH-BOOK] {title}",
                    market_id=legs[0]["market_id"],  # First leg for reference
                    token_id="",  # Multi-leg, no single token
                    side="ALL",
                    market_price=round(total_cost, 4),
                    estimated_prob=0.999,  # Guaranteed profit
                    edge=round(worst_profit, 4),
                    confidence="high",
                    reasoning=(f"Dutch-book: {len(legs)} outcomes sum to {total_cost:.4f}, "
                              f"profit {worst_profit:.2%}-{best_profit:.2%} after 2% fee. "
                              f"Legs: {leg_summary}"),
                    volume=total_volume,
                    liquidity=min_liquidity,
                    end_date=markets[0].get("endDate", "")[:10] if markets else "",
                    kelly_size=CONFIG["max_position_usd"],  # Max size for riskless
                ))

        logger.info(f"Dutch-book scan: checked {len(all_events)} events, "
                    f"found {len(opportunities)} arbitrage opportunities")
        return opportunities

    def scan_all(self) -> list[Opportunity]:
        """Scan all active markets and return ranked opportunities.

        Includes:
        - Dutch-book arbitrage (multi-outcome events, guaranteed profit)
        - Intra-market arbitrage (YES+NO < $0.97)
        - Near-expiry harvesting (fastest compounding, <48h priority)
        - Crypto, event, politics analysis
        - Near-resolution prioritization (markets closing <48h ranked higher)
        """
        markets = self.fetch_all_active_markets()
        opportunities = []

        # Pre-fetch crypto prices
        self.crypto.get_prices()

        # Dutch-book scan (multi-outcome events)
        dutch_opps = self.scan_dutch_book()
        opportunities.extend(dutch_opps)

        now = datetime.now(timezone.utc)

        for market in markets:
            try:
                # Skip markets that already ended (FIX: use UTC)
                end_date = market.get("endDate", "")[:10]
                if end_date:
                    try:
                        # Use end-of-day (23:59:59) so we don't skip markets on their closing date
                        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
                            hour=23, minute=59, second=59, tzinfo=timezone.utc)
                        if end_dt < now:
                            continue
                    except ValueError:
                        pass

                # ALWAYS check arbitrage first (riskless profit)
                arb_opp = self.check_arbitrage(market)
                if arb_opp:
                    opportunities.append(arb_opp)

                # Check near-expiry harvesting (fast compounding)
                near_opp = self.analyze_near_expiry(market)
                if near_opp:
                    # Add urgency tag for markets closing very soon
                    try:
                        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        hours_left = (end_dt - now).total_seconds() / 3600
                        if hours_left < 24:
                            near_opp.reasoning = f"[URGENT <24h] {near_opp.reasoning}"
                            near_opp.confidence = "high"
                    except (ValueError, TypeError):
                        pass
                    opportunities.append(near_opp)

                # Category-specific analysis
                category = self.categorize_market(market)

                opp = None
                if category == "crypto":
                    opp = self.analyze_crypto_market(market)
                elif category == "sports":
                    opp = self.analyze_sports_market(market)
                elif category == "finance":
                    opp = self.analyze_finance_market(market)
                elif category in ("event", "politics"):
                    opp = self.analyze_event_market(market)
                # Weather handled by weather_scanner.py

                if opp and opp.edge > CONFIG["entry_threshold"]:
                    # Boost near-resolution markets
                    try:
                        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        hours_left = (end_dt - now).total_seconds() / 3600
                        if hours_left < 48:
                            opp.reasoning = f"[CLOSING <48h] {opp.reasoning}"
                            if opp.confidence == "medium":
                                opp.confidence = "high"  # Upgrade confidence for near-resolution
                    except (ValueError, TypeError):
                        pass
                    opportunities.append(opp)

            except Exception as e:
                logger.debug(f"Error analyzing market: {e}")
                continue

        # Sort: dutch_book/arbitrage first (riskless), then near-resolution, then by edge * confidence
        confidence_weight = {"high": 3, "medium": 2, "low": 1}

        def sort_key(o):
            if o.category in ("dutch_book", "arbitrage"):
                return (3, o.edge)  # Riskless always first
            # Near-resolution bonus: markets closing <48h get priority
            near_bonus = 0
            if "[URGENT" in o.reasoning or "[CLOSING" in o.reasoning:
                near_bonus = 1
            if o.category == "near_expiry":
                return (2 + near_bonus, o.edge * confidence_weight.get(o.confidence, 1))
            return (0 + near_bonus, o.edge * confidence_weight.get(o.confidence, 1))

        opportunities.sort(key=sort_key, reverse=True)

        n_dutch = sum(1 for o in opportunities if o.category == 'dutch_book')
        n_arb = sum(1 for o in opportunities if o.category == 'arbitrage')
        n_near = sum(1 for o in opportunities if o.category == 'near_expiry')
        n_sports = sum(1 for o in opportunities if o.category == 'sports')
        n_finance = sum(1 for o in opportunities if o.category == 'finance')
        n_urgent = sum(1 for o in opportunities if '[URGENT' in o.reasoning or '[CLOSING' in o.reasoning)

        logger.info(f"Found {len(opportunities)} opportunities across {len(markets)} markets "
                    f"(dutch_book: {n_dutch}, arb: {n_arb}, near_expiry: {n_near}, "
                    f"sports: {n_sports}, finance: {n_finance}, urgent<48h: {n_urgent})")
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
