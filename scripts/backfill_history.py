"""Generate historical backtest data using Open-Meteo weather data.

Fetches actual temps + ensemble forecasts in BATCH (one API call per city),
simulates market buckets, runs our probability engine, and writes results
to paper_trades.csv for the dashboard to display.
"""

import csv
import json
import logging
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from scipy.stats import norm

sys.path.insert(0, os.path.dirname(__file__))
from config import CONFIG, CITIES, PAPER_TRADE_LOG
from probability_engine import ensemble_bucket_probability, kelly_criterion

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ENSEMBLE_API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

session = requests.Session()
session.headers.update({"User-Agent": "polymarket-weather-bot-backtest"})

DAYS_BACK = 14


def get_actual_highs_batch(city_info: dict, start: str, end: str) -> dict:
    """Get actual daily highs for full date range in ONE call."""
    try:
        resp = session.get(ARCHIVE_URL, params={
            "latitude": city_info["lat"], "longitude": city_info["lon"],
            "daily": "temperature_2m_max", "timezone": "auto",
            "start_date": start, "end_date": end,
        }, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        dates = data.get("daily", {}).get("time", [])
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        return {d: t for d, t in zip(dates, temps) if t is not None}
    except Exception as e:
        logger.error(f"Archive fetch failed: {e}")
        return {}


def get_ensemble_batch(city_info: dict, start: str, end: str) -> dict:
    """Get ensemble forecasts for full date range in ONE call.
    Returns {date: [list of members]}.
    """
    try:
        resp = session.get(ENSEMBLE_API_URL, params={
            "latitude": city_info["lat"], "longitude": city_info["lon"],
            "daily": "temperature_2m_max",
            "models": "gfs_seamless",  # Just GFS to reduce load
            "timezone": "auto",
            "start_date": start, "end_date": end,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})
        dates = daily.get("time", [])

        result = {}
        for date_idx, date_str in enumerate(dates):
            members = []
            control = daily.get("temperature_2m_max")
            if control and date_idx < len(control) and control[date_idx] is not None:
                members.append(control[date_idx])
            for m in range(1, 31):
                key = f"temperature_2m_max_member{m:02d}"
                vals = daily.get(key)
                if vals and date_idx < len(vals) and vals[date_idx] is not None:
                    members.append(vals[date_idx])
            if len(members) >= 10:
                result[date_str] = members

        return result
    except Exception as e:
        logger.error(f"Ensemble batch failed: {e}")
        return {}


def generate_buckets(actual_high: float) -> list[dict]:
    """Create realistic 1°C temperature buckets around the actual high."""
    buckets = []
    center = round(actual_high)

    for offset in range(-5, 6):
        temp = center + offset
        bucket_low = temp - 0.5
        bucket_high = temp + 0.5

        # Simulate a slightly noisy market price
        true_prob = norm.cdf(bucket_high, actual_high, 2.0) - norm.cdf(bucket_low, actual_high, 2.0)
        noise = random.gauss(0, 0.04)
        market_price = max(0.02, min(0.95, true_prob + noise))

        buckets.append({
            "bucket_low": bucket_low,
            "bucket_high": bucket_high,
            "market_price": round(market_price, 4),
            "market_id": f"bt-{temp}c",
            "token_id": f"bt-tok-{temp}c",
        })
    return buckets


def main():
    end_dt = datetime.now(timezone.utc).date() - timedelta(days=1)
    start_dt = end_dt - timedelta(days=DAYS_BACK - 1)
    start = start_dt.isoformat()
    end = end_dt.isoformat()

    logger.info(f"Backtesting {start} to {end} across {len(CITIES)} cities")

    all_trades = []
    bankroll = CONFIG["bankroll"]

    for city, city_info in CITIES.items():
        logger.info(f"  {city}...")

        # ONE call for actuals
        actuals = get_actual_highs_batch(city_info, start, end)
        time.sleep(1)

        # ONE call for ensemble
        ensembles = get_ensemble_batch(city_info, start, end)
        time.sleep(1)

        if not actuals:
            logger.warning(f"    No actual data for {city}")
            continue

        logger.info(f"    Got {len(actuals)} days actual, {len(ensembles)} days ensemble")

        for date_str, actual_high in actuals.items():
            ensemble = ensembles.get(date_str, [])
            if len(ensemble) < 10:
                continue

            buckets = generate_buckets(actual_high)

            for bucket in buckets:
                model_prob = ensemble_bucket_probability(
                    ensemble, bucket["bucket_low"], bucket["bucket_high"]
                )
                market_price = bucket["market_price"]
                edge = model_prob - market_price

                if edge >= CONFIG["entry_threshold"]:
                    kelly_frac = kelly_criterion(model_prob, market_price)
                    kelly_size = min(kelly_frac * bankroll, CONFIG["max_position_usd"])

                    if kelly_size > 0.10:
                        won = bucket["bucket_low"] <= actual_high < bucket["bucket_high"]
                        pnl = kelly_size * (1.0 / market_price - 1) if won else -kelly_size

                        all_trades.append({
                            "timestamp": f"{date_str}T{random.randint(6,18):02d}:{random.randint(0,59):02d}:00",
                            "city": city,
                            "bucket_low": bucket["bucket_low"],
                            "bucket_high": bucket["bucket_high"],
                            "model_prob": round(model_prob, 4),
                            "market_price": round(market_price, 4),
                            "edge": round(edge, 4),
                            "kelly_size": round(kelly_size, 2),
                            "market_id": bucket["market_id"],
                            "token_id": bucket["token_id"],
                        })

    # Sort by time
    all_trades.sort(key=lambda x: x["timestamp"])

    if all_trades:
        # Write paper trades
        os.makedirs(os.path.dirname(PAPER_TRADE_LOG), exist_ok=True)
        with open(PAPER_TRADE_LOG, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "city", "bucket_low", "bucket_high",
                "model_prob", "market_price", "edge", "kelly_size",
                "market_id", "token_id",
            ])
            for t in all_trades:
                writer.writerow([
                    t["timestamp"], t["city"], t["bucket_low"], t["bucket_high"],
                    t["model_prob"], t["market_price"], t["edge"], t["kelly_size"],
                    t["market_id"], t["token_id"],
                ])

        # Stats
        total = len(all_trades)
        avg_edge = sum(t["edge"] for t in all_trades) / total
        avg_size = sum(t["kelly_size"] for t in all_trades) / total
        cities_seen = set(t["city"] for t in all_trades)

        logger.info(f"\n{'='*50}")
        logger.info(f"BACKTEST RESULTS")
        logger.info(f"{'='*50}")
        logger.info(f"  Signals: {total} across {len(cities_seen)} cities")
        logger.info(f"  Avg edge: {avg_edge*100:.1f}%")
        logger.info(f"  Avg size: ${avg_size:.2f}")
        logger.info(f"  Wrote to {PAPER_TRADE_LOG}")

        # City breakdown
        from collections import Counter
        city_counts = Counter(t["city"] for t in all_trades)
        for city, count in city_counts.most_common():
            logger.info(f"    {city}: {count} signals")
    else:
        logger.warning("No trades generated")


if __name__ == "__main__":
    main()
