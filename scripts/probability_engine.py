"""Probability engine: converts multi-model ensemble forecasts into temperature
bucket probabilities using Kernel Density Estimation (KDE).

Research-backed approach:
- KDE instead of raw counting (fixes ensemble underdispersion)
- Silverman bandwidth with spread-inflation factor
- Supports multi-model ensembles (ECMWF + GFS + ICON = 120+ members)
- Falls back to normal distribution if ensemble unavailable

References:
- Gneiting et al. (2005), Monthly Weather Review — EMOS calibration
- Hamill (2001), MWR — ensemble reliability/underdispersion
"""

import logging
import math
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
    """Calculate bucket probability using Kernel Density Estimation.

    Places a Gaussian kernel at each ensemble member and integrates
    over the bucket range. This corrects for ensemble underdispersion
    (raw ensembles are overconfident — observations fall outside the
    ensemble range 15-25% of the time, not the expected 6%).

    The bandwidth is Silverman's rule * 1.2 spread-inflation factor
    to account for known NWP underdispersion.

    Args:
        ensemble_members: Temperature forecasts from one or more models (°C).
        bucket_low: Lower bound of temperature bucket.
        bucket_high: Upper bound of temperature bucket.

    Returns:
        Probability (0-1) that actual temp falls in the bucket.
    """
    if not ensemble_members:
        return 0.0

    n = len(ensemble_members)
    mean = sum(ensemble_members) / n
    variance = sum((t - mean) ** 2 for t in ensemble_members) / (n - 1) if n > 1 else 1.0
    std = math.sqrt(variance) if variance > 0 else 1.0

    # Silverman's rule of thumb: h = 1.06 * sigma * N^(-1/5)
    # With 1.2x inflation factor for ensemble underdispersion
    spread_inflation = 1.2
    bandwidth = 1.06 * std * (n ** -0.2) * spread_inflation

    # Minimum bandwidth to avoid delta-function spikes
    bandwidth = max(bandwidth, 0.3)

    # KDE probability: average of each member's contribution to the bucket
    # P(bucket) = (1/N) * sum_i [Phi((high - member_i) / h) - Phi((low - member_i) / h)]
    prob = 0.0
    for member in ensemble_members:
        p_high = norm.cdf(bucket_high, loc=member, scale=bandwidth)
        p_low = norm.cdf(bucket_low, loc=member, scale=bandwidth)
        prob += (p_high - p_low)

    prob /= n

    return max(0.001, min(0.999, prob))


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
    """Calculate fee-aware Kelly criterion bet size as fraction of bankroll.

    Accounts for Polymarket's 2% winner fee on net winnings.
    Uses fractional Kelly (default from config, adjusted by dynamic Kelly).
    Research: Proskurnikov & Barmish (2023) — bet LESS when edge uncertain.
    """
    if fraction is None:
        fraction = CONFIG["kelly_fraction"]

    if market_price <= 0 or market_price >= 1 or prob <= market_price:
        return 0.0

    # Fee-aware Kelly: net win is reduced by fee_rate on winnings
    fee_rate = CONFIG.get("fee_rate", 0.02)
    net_win = (1.0 - market_price) * (1.0 - fee_rate)
    loss = market_price
    edge = prob * net_win - (1.0 - prob) * loss

    if edge <= 0:
        return 0.0

    kelly_full = edge / net_win
    return max(0.0, kelly_full * fraction)


def find_signals(
    city: str,
    forecast_high: float,
    market_buckets: list[dict],
    bankroll: float = None,
    ensemble_members: list[float] = None,
) -> list[BucketSignal]:
    """Find tradeable signals by comparing model probs vs market prices.

    Uses KDE-based ensemble probability if ensemble_members is provided,
    otherwise falls back to normal distribution.

    Args:
        city: City name.
        forecast_high: Forecast high in °C (used as fallback).
        market_buckets: List of dicts with keys:
            bucket_low, bucket_high, market_price, market_id, token_id
        bankroll: Current bankroll for sizing.
        ensemble_members: Optional list of ensemble temperature values
            (can be 31 GFS, 51 ECMWF, or 120+ multi-model).

    Returns:
        List of BucketSignal objects where edge >= entry_threshold.
    """
    if bankroll is None:
        bankroll = CONFIG["bankroll"]

    use_ensemble = ensemble_members and len(ensemble_members) >= 10
    method = f"KDE-{len(ensemble_members)}m" if use_ensemble else "normal"

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
