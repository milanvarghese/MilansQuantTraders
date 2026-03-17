"""Probability engine: converts NOAA forecasts into temperature bucket probabilities."""

import logging
from dataclasses import dataclass

from scipy.stats import norm

from config import CONFIG

logger = logging.getLogger(__name__)


@dataclass
class BucketSignal:
    """A trading signal for a temperature bucket."""
    city: str
    bucket_low: float
    bucket_high: float
    model_prob: float       # Our estimated probability
    market_price: float     # Current market price (0-1)
    edge: float             # model_prob - market_price
    kelly_size: float       # Recommended position size in USD
    market_id: str          # Polymarket condition ID
    token_id: str           # Specific outcome token ID


def bucket_probability(
    forecast_high: float,
    bucket_low: float,
    bucket_high: float,
    sigma: float = None,
) -> float:
    """Probability that actual high falls within a temperature bucket.

    Uses a normal distribution centered on the NOAA forecast high
    with standard deviation sigma (forecast uncertainty).
    """
    if sigma is None:
        sigma = CONFIG["sigma_c"]
    prob = norm.cdf(bucket_high, forecast_high, sigma) - norm.cdf(bucket_low, forecast_high, sigma)
    return max(0.0, min(1.0, prob))


def compute_all_bucket_probs(
    forecast_high: float,
    buckets: list[tuple[float, float]],
    sigma: float = None,
) -> list[tuple[tuple[float, float], float]]:
    """Compute probabilities for all temperature buckets.

    Args:
        forecast_high: Forecast high temperature in °C.
        buckets: List of (low, high) temperature ranges in °C.
        sigma: Forecast uncertainty.

    Returns:
        List of ((low, high), probability) tuples.
    """
    results = []
    for low, high in buckets:
        prob = bucket_probability(forecast_high, low, high, sigma)
        results.append(((low, high), prob))
    return results


def kelly_criterion(prob: float, market_price: float, fraction: float = None) -> float:
    """Calculate Kelly criterion bet size as fraction of bankroll.

    Uses fractional Kelly (default quarter-Kelly) for conservative sizing.

    Args:
        prob: Estimated true probability of winning.
        market_price: Current market price (cost to buy).
        fraction: Kelly fraction (0.25 = quarter-Kelly).

    Returns:
        Fraction of bankroll to bet (0 if no edge).
    """
    if fraction is None:
        fraction = CONFIG["kelly_fraction"]

    if market_price <= 0 or market_price >= 1 or prob <= market_price:
        return 0.0

    # Kelly formula for binary bets: f = (p * b - q) / b
    # where b = (1 - price) / price (odds), p = prob, q = 1-p
    b = (1.0 - market_price) / market_price
    q = 1.0 - prob
    kelly_full = (prob * b - q) / b

    return max(0.0, kelly_full * fraction)


def find_signals(
    city: str,
    forecast_high: float,
    market_buckets: list[dict],
    bankroll: float = None,
) -> list[BucketSignal]:
    """Find tradeable signals by comparing model probs vs market prices.

    Args:
        city: City name.
        forecast_high: Forecast high in °C.
        market_buckets: List of dicts with keys:
            bucket_low, bucket_high, market_price, market_id, token_id
        bankroll: Current bankroll for sizing. Defaults to CONFIG value.

    Returns:
        List of BucketSignal objects where edge >= entry_threshold.
    """
    if bankroll is None:
        bankroll = CONFIG["bankroll"]

    signals = []
    for bucket in market_buckets:
        model_prob = bucket_probability(
            forecast_high,
            bucket["bucket_low"],
            bucket["bucket_high"],
        )
        market_price = bucket["market_price"]
        edge = model_prob - market_price

        if edge >= CONFIG["entry_threshold"]:
            kelly_frac = kelly_criterion(model_prob, market_price)
            kelly_size_raw = kelly_frac * bankroll
            # Cap at max position size
            kelly_size = min(kelly_size_raw, CONFIG["max_position_usd"])

            if kelly_size > 0.10:  # Skip dust amounts
                signal = BucketSignal(
                    city=city,
                    bucket_low=bucket["bucket_low"],
                    bucket_high=bucket["bucket_high"],
                    model_prob=round(model_prob, 4),
                    market_price=round(market_price, 4),
                    edge=round(edge, 4),
                    kelly_size=round(kelly_size, 2),
                    market_id=bucket["market_id"],
                    token_id=bucket["token_id"],
                )
                signals.append(signal)
                logger.info(
                    f"SIGNAL: {city} {bucket['bucket_low']}-{bucket['bucket_high']}°C | "
                    f"model={model_prob:.1%} market={market_price:.1%} "
                    f"edge={edge:.1%} size=${kelly_size:.2f}"
                )

    return signals
