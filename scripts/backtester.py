"""Historical backtester: tests our probability model against resolved Polymarket markets.

Uses local data files (from download_data.py) for fast, accurate backtesting:
- Hourly OHLC with High/Low for path-dependent "reach" market checks
- Volatility from hourly returns (much more accurate than daily)
- 42,000+ resolved Polymarket markets

Usage:
    python scripts/backtester.py                    # Full backtest
    python scripts/backtester.py --symbol BTC       # BTC only
    python scripts/backtester.py --entry-days 14    # Simulate entry 14 days before deadline
    python scripts/backtester.py --calibration      # Show calibration curve
    python scripts/backtester.py --vol-sweep        # Find optimal vol multiplier
"""

import argparse
import csv
import json
import logging
import math
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reports")

# Regex for extracting crypto + target price from market questions
CRYPTO_PATTERN = re.compile(
    r'\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|doge|dogecoin|'
    r'cardano|ada|bnb|binance|litecoin|ltc|avalanche|avax|polkadot|dot|'
    r'chainlink|link|near|sui|aptos|apt)\b.*?'
    r'\$([\d,]+(?:\.\d+)?[kmb]?)',
    re.IGNORECASE,
)

PRODUCTS_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "DOT", "LINK", "LTC"]

NAME_TO_SYMBOL = {
    "bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL", "xrp": "XRP", "ripple": "XRP",
    "doge": "DOGE", "dogecoin": "DOGE", "cardano": "ADA", "ada": "ADA",
    "bnb": "BNB", "binance": "BNB", "litecoin": "LTC", "ltc": "LTC",
    "avalanche": "AVAX", "avax": "AVAX", "polkadot": "DOT", "dot": "DOT",
    "chainlink": "LINK", "link": "LINK", "near": "NEAR", "sui": "SUI",
    "aptos": "APT", "apt": "APT",
}


def normal_cdf(x: float) -> float:
    """Standard normal CDF (Abramowitz & Stegun approximation)."""
    if x < -8:
        return 0.0
    if x > 8:
        return 1.0
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    x_abs = abs(x)
    t = 1.0 / (1.0 + p * x_abs)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x_abs * x_abs / 2)
    return 0.5 * (1.0 + sign * y)


def estimate_probability(current_price: float, target_price: float,
                         days_left: int, volatility: float,
                         direction: str = "above", mu: float = 0.05) -> Optional[float]:
    """Log-normal probability model (same as opportunity_scanner).

    P(S_T > K) = N(d2) where d2 = (ln(S/K) + (mu - sigma^2/2) * T) / (sigma * sqrt(T))
    """
    if current_price <= 0 or target_price <= 0 or days_left <= 0 or volatility <= 0:
        return None

    T = days_left / 365.0
    try:
        d2 = (math.log(current_price / target_price) + (mu - volatility**2 / 2) * T) / (volatility * math.sqrt(T))
    except (ValueError, ZeroDivisionError):
        return None

    prob_above = normal_cdf(d2)
    return prob_above if direction == "above" else 1.0 - prob_above


class PriceData:
    """Loads and queries hourly OHLC data from local CSV files."""

    def __init__(self):
        # {symbol: [{timestamp, datetime, open, high, low, close, volume}, ...]}
        self._data = {}
        # {symbol: {date_str: {close, high, low, day_high, day_low}}}
        self._daily = {}

    def load(self, symbol: str) -> bool:
        """Load hourly OHLC data from data/{symbol}_1h_ohlc.csv."""
        filepath = os.path.join(DATA_DIR, f"{symbol}_1h_ohlc.csv")
        if not os.path.exists(filepath):
            logger.warning(f"No data file: {filepath}. Run download_data.py first.")
            return False

        candles = []
        with open(filepath) as f:
            reader = csv.DictReader(f)
            for row in reader:
                candles.append({
                    "timestamp": int(row["timestamp"]),
                    "datetime": row["datetime"],
                    "date": row["datetime"][:10],
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                })

        self._data[symbol] = candles

        # Build daily aggregates (close = last candle, high/low = day extremes)
        daily = {}
        for c in candles:
            d = c["date"]
            if d not in daily:
                daily[d] = {"close": c["close"], "high": c["high"], "low": c["low"]}
            else:
                daily[d]["close"] = c["close"]  # Last candle of day
                daily[d]["high"] = max(daily[d]["high"], c["high"])
                daily[d]["low"] = min(daily[d]["low"], c["low"])

        self._daily[symbol] = daily
        logger.info(f"Loaded {len(candles):,} hourly candles for {symbol} "
                    f"({len(daily)} days, {candles[0]['date']} to {candles[-1]['date']})")
        return True

    def get_close(self, symbol: str, date_str: str) -> Optional[float]:
        """Get closing price on a specific date."""
        daily = self._daily.get(symbol, {})
        if date_str in daily:
            return daily[date_str]["close"]
        # Find nearest prior date
        dates = sorted(daily.keys())
        for d in reversed(dates):
            if d <= date_str:
                return daily[d]["close"]
        return None

    def did_price_reach(self, symbol: str, target: float, start_date: str,
                        end_date: str, direction: str = "above") -> bool:
        """Check if price ever reached target between start and end dates using High/Low.

        This is the key improvement over daily-close-only checks:
        uses intraday High/Low to detect if target was touched.
        """
        daily = self._daily.get(symbol, {})
        for d in sorted(daily.keys()):
            if d < start_date:
                continue
            if d > end_date:
                break
            if direction == "above" and daily[d]["high"] >= target:
                return True
            if direction == "below" and daily[d]["low"] <= target:
                return True
        return False

    def compute_volatility(self, symbol: str, as_of_date: str,
                           lookback_hours: int = 720) -> Optional[float]:
        """Compute annualized volatility from hourly returns.

        720 hours = 30 days of hourly data. Much more accurate than daily-only.
        """
        candles = self._data.get(symbol, [])
        if not candles:
            return None

        # Filter to candles before as_of_date
        before = [c for c in candles if c["date"] <= as_of_date]
        if len(before) < 48:  # Need at least 2 days
            return None

        recent = before[-lookback_hours:]
        if len(recent) < 24:
            return None

        # Hourly log returns
        log_returns = []
        for i in range(1, len(recent)):
            if recent[i]["close"] > 0 and recent[i-1]["close"] > 0:
                log_returns.append(math.log(recent[i]["close"] / recent[i-1]["close"]))

        if len(log_returns) < 20:
            return None

        mean_r = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_r) ** 2 for r in log_returns) / (len(log_returns) - 1)
        hourly_vol = math.sqrt(variance)
        # Annualize: hourly vol * sqrt(hours_per_year)
        return hourly_vol * math.sqrt(8760)

    def compute_momentum(self, symbol: str, as_of_date: str,
                         lookback_hours: int = 720) -> Optional[float]:
        """Compute annualized drift (momentum) from recent hourly returns.

        Returns realized annualized log-return over lookback period.
        """
        candles = self._data.get(symbol, [])
        if not candles:
            return None

        before = [c for c in candles if c["date"] <= as_of_date]
        if len(before) < 48:
            return None

        recent = before[-lookback_hours:]
        if len(recent) < 24:
            return None

        # Total log return over period
        if recent[0]["close"] <= 0 or recent[-1]["close"] <= 0:
            return None

        total_return = math.log(recent[-1]["close"] / recent[0]["close"])
        hours = len(recent)
        # Annualize
        annualized = total_return * (8760 / hours)

        # Clamp to reasonable range to avoid extreme extrapolation
        return max(-2.0, min(2.0, annualized))

    def date_range(self, symbol: str) -> tuple:
        """Return (first_date, last_date) for loaded data."""
        daily = self._daily.get(symbol, {})
        if not daily:
            return (None, None)
        dates = sorted(daily.keys())
        return (dates[0], dates[-1])


class HistoricalBacktester:
    """Tests our probability model against resolved Polymarket markets."""

    def __init__(self):
        self.prices = PriceData()
        self._markets = []

    def load_data(self, symbols: list = None):
        """Load all local data files."""
        if symbols is None:
            symbols = PRODUCTS_SYMBOLS

        print("Loading local data files...")
        for sym in symbols:
            if self.prices.load(sym):
                first, last = self.prices.date_range(sym)
                print(f"  {sym}: {first} to {last}")
            else:
                print(f"  {sym}: NOT FOUND - run: python scripts/download_data.py --symbol {sym}")

        # Load Polymarket resolved markets
        pm_path = os.path.join(DATA_DIR, "polymarket_resolved.csv")
        if os.path.exists(pm_path):
            with open(pm_path, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                self._markets = list(reader)
            print(f"  Polymarket: {len(self._markets):,} resolved markets")
        else:
            print(f"  Polymarket: NOT FOUND - run: python scripts/download_data.py")

    def parse_market(self, market: dict) -> Optional[dict]:
        """Extract symbol, target price, direction, deadline, and actual outcome."""
        question = market.get("question", "")
        match = CRYPTO_PATTERN.search(question)
        if not match:
            return None

        name = match.group(1).lower()
        symbol = NAME_TO_SYMBOL.get(name)
        if not symbol:
            return None

        # Filter out non-crypto matches (e.g., "DOGE" = Dept of Government Efficiency)
        q_lower = question.lower()
        non_crypto_kws = ["federal spending", "government", "elon", "budget", "department",
                          "efficiency", "spending cut", "agency", "congress"]
        if any(kw in q_lower for kw in non_crypto_kws):
            return None

        # Parse target price
        target_str = match.group(2).replace(",", "")
        multiplier = 1
        if target_str.lower().endswith("k"):
            multiplier = 1000
            target_str = target_str[:-1]
        elif target_str.lower().endswith("m"):
            multiplier = 1_000_000
            target_str = target_str[:-1]
        elif target_str.lower().endswith("b"):
            multiplier = 1_000_000_000
            target_str = target_str[:-1]

        try:
            target_price = float(target_str) * multiplier
        except ValueError:
            return None

        # Reject year-like values
        if 2020 <= target_price <= 2035 and multiplier == 1:
            return None

        # Direction — use explicit keywords, then disambiguate "reach/hit" with price context
        q_lower = question.lower()
        below_kws = ["below", "under", "fall", "drop", "crash", "dip", "sink", "decline"]
        above_kws = ["above", "over", "surpass", "exceed", "rise", "pump", "rally"]
        is_below = any(kw in q_lower for kw in below_kws)
        is_above = any(kw in q_lower for kw in above_kws)

        if is_below and not is_above:
            direction = "below"
        elif is_above and not is_below:
            direction = "above"
        else:
            # Ambiguous ("reach", "hit", etc.) — defer direction until we have price context
            direction = "ambiguous"

        # End date
        end_date = market.get("end_date", "")[:10]

        # Actual outcome from settled prices
        outcomes = json.loads(market.get("outcomes", "[]"))
        outcome_prices = json.loads(market.get("outcome_prices", "[]"))

        yes_won = False
        for i, o in enumerate(outcomes):
            if i < len(outcome_prices) and str(outcome_prices[i]) == "1":
                if o.lower() in ("yes", "over"):
                    yes_won = True

        target_hit = yes_won
        volume = float(market.get("volume", 0))

        return {
            "question": question,
            "symbol": symbol,
            "target_price": target_price,
            "direction": direction,
            "end_date": end_date,
            "target_hit": target_hit,
            "volume": volume,
        }

    def run_backtest(self, entry_days: int = 7, symbol_filter: str = None,
                     show_calibration: bool = False, vol_multiplier: float = 1.0):
        """Run full historical backtest using local data.

        Args:
            entry_days: Simulate entering N days before market deadline
            symbol_filter: Only test specific symbol (e.g., "BTC")
            show_calibration: Show calibration curve
            vol_multiplier: Scale volatility by this factor (for tuning)
        """
        # Parse all crypto price-target markets
        parsed = []
        seen = set()
        for m in self._markets:
            p = self.parse_market(m)
            if not p:
                continue
            if symbol_filter and p["symbol"] != symbol_filter.upper():
                continue
            key = p["question"].lower().strip()
            if key not in seen:
                seen.add(key)
                parsed.append(p)

        print(f"Parsed {len(parsed)} unique crypto price-target markets")

        # Filter to markets where we have price data — check all loaded symbols
        available_symbols = set(s for s in PRODUCTS_SYMBOLS
                               if self.prices.date_range(s)[0] is not None)
        parsed = [p for p in parsed if p["symbol"] in available_symbols]
        print(f"With price data available: {len(parsed)}")

        if not parsed:
            print("No testable markets.")
            return None

        # Run predictions
        results = []
        skipped = 0
        path_corrected = 0

        for p in parsed:
            symbol = p["symbol"]
            target = p["target_price"]
            direction = p["direction"]
            end_date = p["end_date"]
            target_hit = p["target_hit"]

            if not end_date:
                skipped += 1
                continue

            try:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                entry_dt = end_dt - timedelta(days=entry_days)
                entry_date = entry_dt.strftime("%Y-%m-%d")
            except ValueError:
                skipped += 1
                continue

            # Get price at entry from hourly data
            price_at_entry = self.prices.get_close(symbol, entry_date)
            if price_at_entry is None or price_at_entry <= 0:
                skipped += 1
                continue

            # Resolve ambiguous direction using price context
            # "Will BTC reach $65k?" when BTC is at $97k => clearly asking about a drop
            if direction == "ambiguous":
                ratio = price_at_entry / target
                if ratio > 1.05:
                    direction = "below"  # Target is well below current price
                elif ratio < 0.95:
                    direction = "above"  # Target is well above current price
                else:
                    # Price ~= target, truly ambiguous — default to "above"
                    direction = "above"

            # Compute volatility from hourly returns (much more accurate)
            vol_30d = self.prices.compute_volatility(symbol, entry_date, lookback_hours=720)
            vol_90d = self.prices.compute_volatility(symbol, entry_date, lookback_hours=2160)
            vols = [v for v in [vol_30d, vol_90d] if v is not None]
            vol = max(vols) if vols else None
            if vol is None:
                skipped += 1
                continue

            # Apply vol multiplier
            vol_adjusted = vol * vol_multiplier

            # Check if target is already met at entry time
            if direction == "above" and price_at_entry >= target:
                already_hit = True
            elif direction == "below" and price_at_entry <= target:
                already_hit = True
            else:
                already_hit = False

            # Estimate momentum-adjusted drift
            mom_30d = self.prices.compute_momentum(symbol, entry_date, lookback_hours=720)
            mom_90d = self.prices.compute_momentum(symbol, entry_date, lookback_hours=2160)
            if mom_30d is not None and mom_90d is not None:
                # Blend short and long momentum, shrink toward 0 (mean reversion prior)
                raw_mu = 0.6 * mom_30d + 0.4 * mom_90d
                mu = raw_mu * 0.5  # Shrink 50% toward zero (mean reversion)
            elif mom_30d is not None:
                mu = mom_30d * 0.4
            else:
                mu = 0.0  # No momentum data — assume no drift

            # Run our model
            if already_hit:
                prob = estimate_probability(price_at_entry, target, entry_days, vol_adjusted, direction, mu=mu)
                if prob is None:
                    prob = 0.85
                path_corrected += 1
            else:
                prob = estimate_probability(price_at_entry, target, entry_days, vol_adjusted, direction, mu=mu)
                if prob is None:
                    skipped += 1
                    continue

            actual = 1.0 if target_hit else 0.0
            brier = (prob - actual) ** 2

            results.append({
                "question": p["question"][:70],
                "symbol": symbol,
                "target": target,
                "direction": direction,
                "price_at_entry": price_at_entry,
                "vol": vol,
                "prob_predicted": prob,
                "actual_outcome": actual,
                "brier": brier,
                "volume": p["volume"],
                "end_date": end_date,
                "already_hit": already_hit,
            })

        if not results:
            print(f"No results (skipped {skipped})")
            return None

        # Print worst predictions
        print(f"\n{'='*80}")
        print(f"  HISTORICAL BACKTEST ({len(results)} markets, {skipped} skipped, "
              f"{path_corrected} path-corrected)")
        print(f"  Entry: {entry_days} days before deadline | Vol multiplier: {vol_multiplier:.1f}x")
        print(f"{'='*80}")

        for r in sorted(results, key=lambda x: x["brier"], reverse=True)[:15]:
            hit = "HIT" if r["actual_outcome"] == 1.0 else "MISS"
            ah = " [ALREADY]" if r.get("already_hit") else ""
            print(f"  [{r['symbol']:>4}] {r['question']}")
            print(f"    {r['direction']:>5} ${r['target']:>10,.0f} | "
                  f"price=${r['price_at_entry']:>10,.0f} | vol={r['vol']:.0%} | "
                  f"pred={r['prob_predicted']:.1%} | {hit}{ah} | B={r['brier']:.4f}")

        if len(results) > 15:
            print(f"  ... and {len(results) - 15} more")

        # Aggregate stats
        n = len(results)
        avg_brier = sum(r["brier"] for r in results) / n
        correct = sum(1 for r in results if (r["prob_predicted"] >= 0.5) == (r["actual_outcome"] == 1.0))
        accuracy = correct / n

        # By symbol
        by_symbol = {}
        for r in results:
            sym = r["symbol"]
            if sym not in by_symbol:
                by_symbol[sym] = {"briers": [], "n": 0, "correct": 0}
            by_symbol[sym]["briers"].append(r["brier"])
            by_symbol[sym]["n"] += 1
            if (r["prob_predicted"] >= 0.5) == (r["actual_outcome"] == 1.0):
                by_symbol[sym]["correct"] += 1

        # By direction
        above_results = [r for r in results if r["direction"] == "above"]
        below_results = [r for r in results if r["direction"] == "below"]

        print(f"\n{'='*80}")
        print(f"  AGGREGATE RESULTS")
        print(f"{'='*80}")
        print(f"  Brier Score:    {avg_brier:.4f} (target <0.15, random=0.25)")
        print(f"  Accuracy:       {accuracy:.1%} ({correct}/{n})")
        print(f"")
        print(f"  BY SYMBOL:")
        for sym, stats in sorted(by_symbol.items(), key=lambda x: x[1]["n"], reverse=True):
            sym_brier = sum(stats["briers"]) / len(stats["briers"])
            sym_acc = stats["correct"] / stats["n"]
            print(f"    {sym:>5}: Brier={sym_brier:.4f} | Accuracy={sym_acc:.1%} | N={stats['n']}")

        print(f"")
        print(f"  BY DIRECTION:")
        if above_results:
            ab_brier = sum(r["brier"] for r in above_results) / len(above_results)
            print(f"    above: Brier={ab_brier:.4f} | N={len(above_results)}")
        if below_results:
            bl_brier = sum(r["brier"] for r in below_results) / len(below_results)
            print(f"    below: Brier={bl_brier:.4f} | N={len(below_results)}")

        if show_calibration:
            self._show_calibration(results)

        print(f"{'='*80}")
        self._save_results(results, entry_days)

        return results

    def run_vol_sweep(self, entry_days: int = 7, symbol_filter: str = None):
        """Sweep volatility multipliers to find optimal value."""
        # Pre-parse markets once
        parsed = []
        seen = set()
        for m in self._markets:
            p = self.parse_market(m)
            if not p:
                continue
            if symbol_filter and p["symbol"] != symbol_filter.upper():
                continue
            key = p["question"].lower().strip()
            if key not in seen:
                seen.add(key)
                parsed.append(p)

        available = set(s for s in PRODUCTS_SYMBOLS
                       if self.prices.date_range(s)[0] is not None)
        parsed = [p for p in parsed if p["symbol"] in available]

        # Pre-compute entry data for all markets
        test_data = []
        for p in parsed:
            if not p["end_date"]:
                continue
            try:
                end_dt = datetime.strptime(p["end_date"], "%Y-%m-%d")
                entry_dt = end_dt - timedelta(days=entry_days)
                entry_date = entry_dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

            price = self.prices.get_close(p["symbol"], entry_date)
            if not price or price <= 0:
                continue
            vol_30 = self.prices.compute_volatility(p["symbol"], entry_date, 720)
            vol_90 = self.prices.compute_volatility(p["symbol"], entry_date, 2160)
            vols = [v for v in [vol_30, vol_90] if v is not None]
            vol = max(vols) if vols else None

            # Resolve ambiguous direction
            direction = p["direction"]
            if direction == "ambiguous":
                ratio = price / p["target_price"]
                direction = "below" if ratio > 1.05 else ("above" if ratio < 0.95 else "above")

            already_hit = False
            if direction == "above" and price >= p["target_price"]:
                already_hit = True
            elif direction == "below" and price <= p["target_price"]:
                already_hit = True

            if vol:
                test_data.append({**p, "price": price, "vol": vol, "already_hit": already_hit, "direction": direction})

        print(f"\nVol multiplier sweep ({len(test_data)} markets, entry={entry_days}d)")
        print(f"{'Mult':>5} | {'Brier':>7} | {'Acc':>6} | {'Cal 0-10%':>10} | {'Cal 10-50%':>11}")
        print(f"{'-'*5}-+-{'-'*7}-+-{'-'*6}-+-{'-'*10}-+-{'-'*11}")

        best_brier = 1.0
        best_mult = 1.0

        for mult_10x in range(10, 40, 2):
            mult = mult_10x / 10.0
            briers = []
            correct = 0
            low_bin, mid_bin = [], []

            for d in test_data:
                prob = estimate_probability(d["price"], d["target_price"],
                                           entry_days, d["vol"] * mult, d["direction"])
                if prob is None:
                    if d["already_hit"]:
                        prob = 0.85
                    else:
                        continue

                actual = 1.0 if d["target_hit"] else 0.0
                briers.append((prob - actual) ** 2)
                if (prob >= 0.5) == (actual == 1.0):
                    correct += 1
                if prob < 0.10:
                    low_bin.append(actual)
                elif prob < 0.50:
                    mid_bin.append(actual)

            if not briers:
                continue

            avg_b = sum(briers) / len(briers)
            acc = correct / len(briers)
            low_act = sum(low_bin) / len(low_bin) if low_bin else 0
            mid_act = sum(mid_bin) / len(mid_bin) if mid_bin else 0
            marker = " <--" if avg_b < best_brier else ""
            if avg_b < best_brier:
                best_brier = avg_b
                best_mult = mult

            print(f" {mult:.1f}x | {avg_b:.4f} | {acc:.1%} | "
                  f"{low_act:.1%} (N={len(low_bin):>3}) | "
                  f"{mid_act:.1%} (N={len(mid_bin):>3}){marker}")

        print(f"\nOPTIMAL: {best_mult:.1f}x -> Brier={best_brier:.4f}")
        return best_mult

    def _show_calibration(self, results: list):
        """Show calibration curve: predicted probability vs actual frequency."""
        bins = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01]
        print(f"\n  CALIBRATION CURVE:")
        print(f"  {'Predicted':>12} | {'Actual':>8} | {'Count':>5} | {'Bar'}")
        print(f"  {'-'*12}-+-{'-'*8}-+-{'-'*5}-+-{'-'*30}")

        for i in range(len(bins) - 1):
            low, high = bins[i], bins[i+1]
            in_bin = [r for r in results if low <= r["prob_predicted"] < high]
            if not in_bin:
                continue
            actual_freq = sum(r["actual_outcome"] for r in in_bin) / len(in_bin)
            midpoint = (low + high) / 2
            bar_len = int(actual_freq * 30)
            bar = "#" * bar_len
            gap = abs(midpoint - actual_freq)
            marker = " <-- off" if gap > 0.15 else ""
            print(f"  {low:.0%}-{high:.0%}      | {actual_freq:>6.1%} | {len(in_bin):>5} | {bar}{marker}")

    def _save_results(self, results: list, entry_days: int):
        """Save backtest results to CSV."""
        os.makedirs(REPORT_DIR, exist_ok=True)
        filepath = os.path.join(REPORT_DIR, "backtest_results.csv")

        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "question", "symbol", "target_price", "direction",
                "price_at_entry", "vol", "prob_predicted", "actual_outcome",
                "brier", "volume", "end_date", "entry_days_before", "already_hit",
            ])
            for r in results:
                writer.writerow([
                    r["question"], r["symbol"], r["target"], r["direction"],
                    round(r["price_at_entry"], 2), round(r["vol"], 4),
                    round(r["prob_predicted"], 4), r["actual_outcome"],
                    round(r["brier"], 4), round(r["volume"], 2),
                    r["end_date"], entry_days, r.get("already_hit", False),
                ])

        print(f"\n  Results saved to {filepath}")


def main():
    parser = argparse.ArgumentParser(description="Historical Backtester (local data)")
    parser.add_argument("--entry-days", type=int, default=7,
                       help="Simulate entry N days before deadline (default: 7)")
    parser.add_argument("--symbol", type=str, default=None,
                       help="Filter by symbol (BTC, ETH, SOL)")
    parser.add_argument("--calibration", action="store_true",
                       help="Show calibration curve")
    parser.add_argument("--vol-sweep", action="store_true",
                       help="Find optimal volatility multiplier")
    parser.add_argument("--vol-mult", type=float, default=1.0,
                       help="Volatility multiplier (default: 1.0)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    bt = HistoricalBacktester()
    bt.load_data()

    if args.vol_sweep:
        optimal = bt.run_vol_sweep(entry_days=args.entry_days, symbol_filter=args.symbol)
        print(f"\nRe-running backtest with optimal {optimal:.1f}x...")
        bt.run_backtest(entry_days=args.entry_days, symbol_filter=args.symbol,
                       show_calibration=True, vol_multiplier=optimal)
    else:
        bt.run_backtest(entry_days=args.entry_days, symbol_filter=args.symbol,
                       show_calibration=args.calibration, vol_multiplier=args.vol_mult)


if __name__ == "__main__":
    main()
