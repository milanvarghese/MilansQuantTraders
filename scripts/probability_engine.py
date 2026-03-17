"""Probability engine: converts GFS ensemble forecasts into temperature bucket probabilities.

Uses 31-member GFS ensemble for empirical probability estimation instead of
assuming a normal distribution. Falls back to normal distribution if ensemble
data is unavailable.
"""

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


def ensemble_bucket_probability(
    ensemble_members: list[float],
    bucket_low: float,
    bucket_high: float,
) -> float:
    """Calculate bucket probability from GFS ensemble members.

    Counts how many of the 31 ensemble members fall within the bucket range.
    This is empirical probability — no distributional assumptions.

    Args:
        ensemble_members: List of 31 temperature forecasts in °C.
        bucket_low: Lower bound of temperature bucket.
        bucket_high: Upper bound of temperature bucket.

    Returns:
        Probability (0-1) that actual temp falls in the bucket.
    """
    if not ensemble_members:
        return 0.0

    count = sum(1 for t in ensemble_members if bucket_low <= t < bucket_high)
    prob = count / len(ensemble_members)

    # Apply Laplace smoothing to avoid 0% or 100% probabilities
    # (with 31 members, raw 0/31 = 0% is too aggressive)
    smoothed = (count + 0.5) / (len(ensemble_members) + 1)

    return smoothed


def bucket_probability(
    forecast_high: float,
    bucket_low: float,
    bucket_high: float,
    sigma: float = None,
) -> float:
    """Fallback: probability using normal distribution (when ensemble unavailable).

    Uses a normal distribution centered on the forecast high
    with standard deviation sigma (forecast uncertainty).
    """
    if sigma is None:
        sigma = CONFIG["sigma_c"]
    prob = norm.cdf(bucket_high, forecast_high, sigma) - norm.cdf(bucket_low, forecast_high, sigma)
    return max(0.0, min(1.0, prob))


def kelly_criterion(prob: float, market_price: float, fraction: float = None) -> float:
    """Calculate Kelly criterion bet size as fraction of bankroll.

    Uses fractional Kelly (default quarter-Kelly) for conservative sizing.
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
    ensemble_members: list[float] = None,
) -> list[BucketSignal]:
    """Find tradeable signals by comparing model probs vs market prices.

    Uses ensemble-based probability if ensemble_members is provided,
    otherwise falls back to normal distribution.

    Args:
        city: City name.
        forecast_high: Forecast high in °C (used as fallback).
        market_buckets: List of dicts with keys:
            bucket_low, bucket_high, market_price, market_id, token_id
        bankroll: Current bankroll for sizing.
        ensemble_members: Optional list of 31 GFS ensemble temperature values.

    Returns:
        List of BucketSignal objects where edge >= entry_threshold.
    """
    if bankroll is None:
        bankroll = CONFIG["bankroll"]

    use_ensemble = ensemble_members and len(ensemble_members) >= 10
    method = "ensemble" if use_ensemble else "normal"

    signals = []
    for bucket in market_buckets:
        if use_ensemble:
            model_prob = ensemble_bucket_probability(
                ensemble_members,
                bucket["bucket_low"],
                bucket["bucket_high"],
            )
        else:
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
                    f"SIGNAL [{method}]: {city} {bucket['bucket_low']}-{bucket['bucket_high']}°C | "
                    f"model={model_prob:.1%} market={market_price:.1%} "
                    f"edge={edge:.1%} size=${kelly_size:.2f}"
                )

    return signals
