"""Polymarket API client for market discovery and order execution.

Includes Cloudflare 403 mitigation with exponential backoff + jitter,
proxy rotation support, and automatic retry logic.
"""

import logging
import random
import re
import time
import threading
from typing import Optional

import requests

from config import (
    GAMMA_API_URL,
    CLOB_API_URL,
    PRIVATE_KEY,
    POLY_API_KEY,
    POLY_API_SECRET,
    POLY_API_PASSPHRASE,
    CHAIN_ID,
    PROXIES,
    CONFIG,
)

logger = logging.getLogger(__name__)

# Common browser-like headers to reduce Cloudflare blocks
_CF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


def _cf_retry_request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """Make an HTTP request with Cloudflare 403 retry + exponential backoff.

    Retries on 403/429/5xx with jittered exponential backoff.
    """
    max_retries = CONFIG.get("cf_max_retries", 3)
    base_delay = CONFIG.get("cf_base_delay", 2.0)

    for attempt in range(max_retries + 1):
        try:
            resp = getattr(session, method)(url, **kwargs)

            if resp.status_code == 403:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(
                        f"Cloudflare 403 on {url} (attempt {attempt+1}/{max_retries+1}), "
                        f"retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
                    continue
                logger.error(f"Cloudflare 403 persists after {max_retries+1} attempts: {url}")

            elif resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", base_delay * 2))
                if attempt < max_retries:
                    logger.warning(f"Rate limited (429), waiting {retry_after}s")
                    time.sleep(retry_after)
                    continue

            elif resp.status_code >= 500:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"Server error {resp.status_code}, retrying in {delay:.1f}s")
                    time.sleep(delay)
                    continue

            return resp

        except requests.exceptions.ConnectionError as e:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"Connection error (attempt {attempt+1}): {e}, retrying in {delay:.1f}s")
                time.sleep(delay)
                continue
            raise

    return resp


class GammaClient:
    """Read-only client for Polymarket Gamma API (market discovery)."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(_CF_HEADERS)
        if PROXIES:
            self.session.proxies.update(PROXIES)
        self._events_cache: Optional[list] = None
        self._cache_time: float = 0

    def _fetch_weather_events(self) -> list[dict]:
        """Fetch active events and filter for temperature markets.

        Caches results for 10 minutes to avoid redundant API calls.
        """
        now = time.time()
        if self._events_cache is not None and (now - self._cache_time) < 600:
            return self._events_cache

        try:
            all_events = []
            for offset in range(0, 500, 100):
                resp = _cf_retry_request(
                    self.session, "get",
                    f"{GAMMA_API_URL}/events",
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
                all_events.extend(batch)

            # Filter for temperature/weather events
            weather_events = []
            keywords = ["temperature", "highest temp", "°c", "°f", "weather"]
            for e in all_events:
                title = e.get("title", "").lower()
                if any(kw in title for kw in keywords):
                    weather_events.append(e)

            self._events_cache = weather_events
            self._cache_time = now
            logger.info(f"Found {len(weather_events)} weather events out of {len(all_events)} total")
            return weather_events

        except Exception as e:
            logger.error(f"Failed to fetch events: {e}")
            return self._events_cache or []

    def search_weather_markets(self, city: str = "") -> list[dict]:
        """Search for active weather/temperature markets for a city.

        Returns list of market dicts from matching events.
        """
        events = self._fetch_weather_events()
        city_lower = city.lower()

        markets = []
        for event in events:
            title = event.get("title", "").lower()
            if city_lower and city_lower not in title:
                continue
            event_markets = event.get("markets", [])
            markets.extend(event_markets)

        logger.info(f"Found {len(markets)} temperature markets" +
                   (f" for {city}" if city else ""))
        return markets

    def get_market_detail(self, condition_id: str) -> Optional[dict]:
        """Get detailed info for a specific market."""
        try:
            resp = _cf_retry_request(
                self.session, "get",
                f"{GAMMA_API_URL}/markets/{condition_id}",
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to get market {condition_id}: {e}")
            return None

    def parse_temperature_buckets(self, market: dict) -> list[dict]:
        """Parse a temperature market into tradeable buckets.

        Polymarket weather markets use outcomes like:
        - "Will the highest temperature in Seoul be 8°C on March 17?"
        - Individual temperature values as Yes/No markets

        For single-value markets, we create a 1-degree bucket (e.g., 8°C = [7.5, 8.5]).
        For range markets, we parse the range directly.

        Returns list of dicts with:
            bucket_low, bucket_high, market_price, market_id, token_id
        """
        buckets = []
        question = market.get("question", "")
        condition_id = market.get("condition_id", "")
        tokens = market.get("tokens", [])

        # Try to extract a single temperature value from the question
        # Pattern: "be X°C" or "be X °C"
        single_match = re.search(r'be\s+(-?\d+(?:\.\d+)?)\s*°[CcFf]', question)
        # Pattern: "X°C or higher" / "X°C or below"
        or_higher = re.search(r'(-?\d+(?:\.\d+)?)\s*°[CcFf]\s+or\s+higher', question)
        or_below = re.search(r'(-?\d+(?:\.\d+)?)\s*°[CcFf]\s+or\s+(?:below|lower)', question)
        # Pattern for ranges: "X-Y°C" or "X to Y°C"
        range_match = re.search(r'(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)\s*°[CcFf]', question)

        if or_higher:
            # "X°C or higher" — open-ended high bucket
            val = float(or_higher.group(1))
            bucket_low = val
            bucket_high = val + 10  # wide upper range
        elif or_below:
            # "X°C or below" — open-ended low bucket
            val = float(or_below.group(1))
            bucket_low = val - 10
            bucket_high = val
        elif single_match and not range_match:
            # Single value like "8°C" → bucket [7.5, 8.5]
            val = float(single_match.group(1))
            bucket_low = val - 0.5
            bucket_high = val + 0.5
        elif range_match:
            bucket_low = float(range_match.group(1))
            bucket_high = float(range_match.group(2))
        else:
            logger.debug(f"Could not parse temperature from: {question}")
            return []

        # Find the "Yes" token and its price
        for token in tokens:
            outcome = token.get("outcome", "").lower()
            if outcome == "yes":
                price = float(token.get("price", 0))
                if price > 0:
                    buckets.append({
                        "bucket_low": bucket_low,
                        "bucket_high": bucket_high,
                        "market_price": price,
                        "market_id": condition_id,
                        "token_id": token.get("token_id", ""),
                        "outcome": question,
                    })
                break

        return buckets


class PolymarketTrader:
    """Trading client wrapping py-clob-client for order execution."""

    def __init__(self):
        self._clob_client = None
        self._heartbeat_thread = None
        self._heartbeat_running = False

    def _init_clob(self):
        """Initialize the CLOB client (lazy, so imports don't fail if not trading)."""
        if self._clob_client is not None:
            return

        if not PRIVATE_KEY:
            raise ValueError("PRIVATE_KEY not set in .env - cannot trade")

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=POLY_API_KEY,
                api_secret=POLY_API_SECRET,
                api_passphrase=POLY_API_PASSPHRASE,
            )

            self._clob_client = ClobClient(
                CLOB_API_URL,
                key=PRIVATE_KEY,
                chain_id=CHAIN_ID,
                creds=creds,
            )
            logger.info("CLOB client initialized")
        except ImportError:
            raise ImportError(
                "py-clob-client not installed. Run: pip install py-clob-client"
            )

    def derive_api_creds(self) -> dict:
        """Generate/derive L2 API credentials from wallet.

        Run this once, then save the output to your .env file.
        """
        self._init_clob()
        creds = self._clob_client.create_or_derive_api_creds()
        logger.info("API credentials derived successfully")
        return creds

    def get_orderbook(self, token_id: str) -> Optional[dict]:
        """Fetch the order book for a token with Cloudflare retry."""
        self._init_clob()
        max_retries = CONFIG.get("cf_max_retries", 3)
        base_delay = CONFIG.get("cf_base_delay", 2.0)

        for attempt in range(max_retries + 1):
            try:
                book = self._clob_client.get_order_book(token_id)
                return book
            except Exception as e:
                err_str = str(e)
                if ("403" in err_str or "cloudflare" in err_str.lower()) and attempt < max_retries:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(f"Cloudflare block on orderbook (attempt {attempt+1}), retry in {delay:.1f}s")
                    time.sleep(delay)
                    continue
                logger.error(f"Failed to get orderbook for {token_id}: {e}")
                return None

    def buy(
        self,
        token_id: str,
        amount_usd: float,
        price: float,
    ) -> Optional[dict]:
        """Place a limit buy order.

        Args:
            token_id: The outcome token to buy.
            amount_usd: Dollar amount to spend.
            price: Limit price (0-1).

        Returns:
            Order response dict, or None on failure.
        """
        self._init_clob()
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY

        size = amount_usd / price  # Number of shares

        max_retries = CONFIG.get("cf_max_retries", 3)
        base_delay = CONFIG.get("cf_base_delay", 2.0)

        for attempt in range(max_retries + 1):
            try:
                order_args = OrderArgs(
                    price=price,
                    size=size,
                    side=BUY,
                    token_id=token_id,
                )
                signed_order = self._clob_client.create_order(order_args)
                resp = self._clob_client.post_order(signed_order)
                logger.info(f"BUY order placed: {size:.2f} shares @ ${price:.3f} = ${amount_usd:.2f}")
                return resp
            except Exception as e:
                err_str = str(e)
                if ("403" in err_str or "cloudflare" in err_str.lower()) and attempt < max_retries:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(f"Cloudflare block on BUY (attempt {attempt+1}), retry in {delay:.1f}s")
                    time.sleep(delay)
                    continue
                logger.error(f"BUY order failed: {e}")
                return None

    def sell(
        self,
        token_id: str,
        size: float,
        price: float,
    ) -> Optional[dict]:
        """Place a limit sell order.

        Args:
            token_id: The outcome token to sell.
            size: Number of shares to sell.
            price: Limit price (0-1).

        Returns:
            Order response dict, or None on failure.
        """
        self._init_clob()
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import SELL

        max_retries = CONFIG.get("cf_max_retries", 3)
        base_delay = CONFIG.get("cf_base_delay", 2.0)

        for attempt in range(max_retries + 1):
            try:
                order_args = OrderArgs(
                    price=price,
                    size=size,
                    side=SELL,
                    token_id=token_id,
                )
                signed_order = self._clob_client.create_order(order_args)
                resp = self._clob_client.post_order(signed_order)
                logger.info(f"SELL order placed: {size:.2f} shares @ ${price:.3f}")
                return resp
            except Exception as e:
                err_str = str(e)
                if ("403" in err_str or "cloudflare" in err_str.lower()) and attempt < max_retries:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(f"Cloudflare block on SELL (attempt {attempt+1}), retry in {delay:.1f}s")
                    time.sleep(delay)
                    continue
                logger.error(f"SELL order failed: {e}")
                return None

    def get_market_liquidity(self, token_id: str) -> dict:
        """Get spread and liquidity info for risk checks.

        Returns dict with 'spread' (0-1) and 'liquidity_usd' (float).
        """
        book = self.get_orderbook(token_id)
        if not book:
            return {"spread": 1.0, "liquidity_usd": 0.0}

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 1.0
        spread = best_ask - best_bid

        # Sum top-of-book liquidity (top 3 levels each side)
        bid_liq = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in bids[:3])
        ask_liq = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks[:3])
        liquidity_usd = bid_liq + ask_liq

        return {"spread": spread, "liquidity_usd": liquidity_usd}

    def get_open_orders(self) -> list[dict]:
        """Get all open orders for this wallet."""
        self._init_clob()
        try:
            return self._clob_client.get_orders() or []
        except Exception as e:
            logger.error(f"Failed to fetch open orders: {e}")
            return []

    def cancel_all(self) -> bool:
        """Cancel all open orders."""
        self._init_clob()
        try:
            self._clob_client.cancel_all()
            logger.info("All open orders cancelled")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel orders: {e}")
            return False

    def start_heartbeat(self):
        """Start the heartbeat thread (required for GTC/GTD orders)."""
        if self._heartbeat_running:
            return

        self._init_clob()
        self._heartbeat_running = True

        def _beat():
            while self._heartbeat_running:
                try:
                    self._clob_client.drop_notifications()
                except Exception as e:
                    logger.warning(f"Heartbeat failed: {e}")
                time.sleep(CONFIG["heartbeat_interval"])

        self._heartbeat_thread = threading.Thread(target=_beat, daemon=True)
        self._heartbeat_thread.start()
        logger.info("Heartbeat started")

    def stop_heartbeat(self):
        """Stop the heartbeat thread."""
        self._heartbeat_running = False
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=15)
        logger.info("Heartbeat stopped")
