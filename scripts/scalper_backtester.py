"""Historical backtester for the crypto scalper signal engine.

Replays 5m candle data through the exact same SignalDetector + exit logic
used in live trading. Measures real performance: win rate, profit factor,
Sharpe ratio, max drawdown, per-signal and per-pair breakdowns.

Historical order book / funding rate data is not freely available, so those
signals are set to neutral. This tests the 11 technical signals that can
be computed from OHLCV data alone.

Usage:
    python scripts/scalper_backtester.py                     # Backtest all pairs with data
    python scripts/scalper_backtester.py --pairs BTC,ETH     # Specific pairs only
    python scripts/scalper_backtester.py --days 30           # Last 30 days only
    python scripts/scalper_backtester.py --optimize          # Sweep key parameters
    python scripts/scalper_backtester.py --report            # Generate detailed report

Prerequisites:
    python scripts/download_data.py --scalper-backtest       # Download 5m data first
"""

import argparse
import csv
import json
import logging
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

# Import the actual scalper signal engine (same code used in live trading)
sys.path.insert(0, os.path.dirname(__file__))
from crypto_scalper import (
    SCALPER_CONFIG,
    SignalDetector,
    SignalBandit,
    CUSUMFilter,
    MarketContext,
    Signal,
    calc_rsi,
    calc_ema,
    calc_adx,
    calc_atr,
    calc_volume_profile,
    detect_rsi_divergence,
    detect_engulfing,
    find_swing_levels,
    calc_atr_ratio,
    GRADE_MAP,
)

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reports")


# ================================================================
# DATA LOADING
# ================================================================

def load_5m_candles(symbol: str) -> list[dict]:
    """Load 5m candle data from CSV."""
    filepath = os.path.join(DATA_DIR, f"{symbol}_5m_ohlc.csv")
    if not os.path.exists(filepath):
        return []

    candles = []
    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append({
                "time": int(row["timestamp"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            })

    candles.sort(key=lambda c: c["time"])
    return candles


def aggregate_to_1h(candles_5m: list[dict]) -> list[dict]:
    """Aggregate 5m candles to 1h candles."""
    if not candles_5m:
        return []

    hourly = []
    bucket = []

    for c in candles_5m:
        # Bucket by hour (floor to nearest hour)
        hour_ts = (c["time"] // 3600) * 3600
        if bucket and (bucket[0]["time"] // 3600) * 3600 != hour_ts:
            # Flush previous hour
            hourly.append({
                "time": (bucket[0]["time"] // 3600) * 3600,
                "open": bucket[0]["open"],
                "high": max(b["high"] for b in bucket),
                "low": min(b["low"] for b in bucket),
                "close": bucket[-1]["close"],
                "volume": sum(b["volume"] for b in bucket),
            })
            bucket = []
        bucket.append(c)

    # Flush last bucket
    if bucket:
        hourly.append({
            "time": (bucket[0]["time"] // 3600) * 3600,
            "open": bucket[0]["open"],
            "high": max(b["high"] for b in bucket),
            "low": min(b["low"] for b in bucket),
            "close": bucket[-1]["close"],
            "volume": sum(b["volume"] for b in bucket),
        })

    return hourly


def find_available_pairs() -> list[str]:
    """Find all pairs that have 5m data downloaded."""
    pairs = []
    if not os.path.exists(DATA_DIR):
        return pairs
    for f in os.listdir(DATA_DIR):
        if f.endswith("_5m_ohlc.csv"):
            symbol = f.replace("_5m_ohlc.csv", "")
            pairs.append(symbol)
    return sorted(pairs)


# ================================================================
# MARKET CONTEXT BUILDER (from historical data)
# ================================================================

def build_context_from_candles(pair: str, candles_1h: list[dict],
                               candles_5m_window: list[dict],
                               cusum: CUSUMFilter,
                               btc_returns: list[float],
                               config: dict) -> MarketContext:
    """Build MarketContext from historical candle data.

    Same logic as CryptoScalper._build_market_context() but without
    live API calls for order book / funding (set to neutral).
    """
    ctx = MarketContext(pair=f"{pair}-USD")

    if len(candles_1h) >= 30:
        closes_1h = [c["close"] for c in candles_1h]
        highs_1h = [c["high"] for c in candles_1h]
        lows_1h = [c["low"] for c in candles_1h]

        # Hourly EMAs
        ema9 = calc_ema(closes_1h, 9)
        ema20 = calc_ema(closes_1h, 20)
        rsi_1h = calc_rsi(closes_1h)

        if ema9 and ema20:
            ctx.hourly_ema_fast = ema9
            ctx.hourly_ema_slow = ema20
            if ema9 > ema20 and closes_1h[-1] > ema9:
                ctx.hourly_trend = "bullish"
            elif ema9 < ema20 and closes_1h[-1] < ema9:
                ctx.hourly_trend = "bearish"
            else:
                ctx.hourly_trend = "neutral"

        if rsi_1h:
            ctx.hourly_rsi = rsi_1h

        # ADX for regime
        adx = calc_adx(highs_1h, lows_1h, closes_1h)
        if adx is not None:
            ctx.adx = adx
            if adx > config["adx_trending"]:
                if ctx.hourly_trend == "bullish":
                    ctx.regime = "trending_up"
                elif ctx.hourly_trend == "bearish":
                    ctx.regime = "trending_down"
                else:
                    ctx.regime = "trending"
                ctx.trend_direction = "up" if ctx.hourly_trend == "bullish" else "down"
            elif adx < config["adx_ranging"]:
                ctx.regime = "ranging"
            else:
                ctx.regime = "transitional"

        # 1H ATR for exit calculations
        atr_1h = calc_atr(highs_1h, lows_1h, closes_1h)
        if atr_1h:
            ctx.atr_1h = atr_1h

        # Volume profile from 1H candles
        vp = calc_volume_profile(candles_1h)
        if vp:
            ctx.poc = vp["poc"]
            ctx.vah = vp["vah"]
            ctx.val = vp["val"]

    # Order book: not available historically (set neutral)
    ctx.ob_imbalance = 0.0
    ctx.spread_pct = 0.02  # Assume typical spread

    # Funding rate: not available historically (set neutral)
    ctx.funding_rate = 0.0
    ctx.funding_signal = "neutral"

    # Fear & Greed: set to neutral (50) for consistency
    ctx.fear_greed = 50

    # CUSUM filter from 5m returns
    if len(candles_5m_window) >= 2:
        prev_close = candles_5m_window[-2]["close"]
        curr_close = candles_5m_window[-1]["close"]
        if prev_close > 0:
            ret = (curr_close - prev_close) / prev_close
            cusum_sig = cusum.update(pair, ret)
            if cusum_sig:
                ctx.cusum_signal = cusum_sig

    # BTC vol ratio from BTC returns
    if len(btc_returns) >= 100:
        recent = btc_returns[-24:]
        full = btc_returns
        recent_var = sum(r**2 for r in recent) / len(recent)
        full_var = sum(r**2 for r in full) / len(full)
        if full_var > 0:
            ctx.btc_vol_ratio = math.sqrt(recent_var / full_var)

    # Pump detection from 1H candles
    if len(candles_1h) >= 5:
        avg_vol_1h = sum(c["volume"] for c in candles_1h[-5:-1]) / 4
        curr_vol_1h = candles_1h[-1]["volume"]
        price_chg_1h = (candles_1h[-1]["close"] - candles_1h[-2]["close"]) / candles_1h[-2]["close"] if candles_1h[-2]["close"] > 0 else 0
        if (avg_vol_1h > 0
            and curr_vol_1h > avg_vol_1h * config.get("pump_volume_mult", 5.0)
            and abs(price_chg_1h) > config.get("pump_price_change_pct", 0.03)):
            ctx.is_pump = True

    # v3.2: RSI divergence from 5m closes
    if len(candles_5m_window) >= 35:
        closes_5m = [c["close"] for c in candles_5m_window]
        div = detect_rsi_divergence(closes_5m, period=14, lookback=15)
        if div:
            ctx.rsi_divergence = div

    # v3.2: Engulfing candle pattern from last 2 candles
    if len(candles_5m_window) >= 2:
        eng = detect_engulfing(candles_5m_window)
        if eng:
            ctx.engulfing = eng

    # v3.2: Swing levels from 1H candles
    if len(candles_1h) >= 10:
        levels = find_swing_levels(candles_1h)
        ctx.swing_supports = levels["support"]
        ctx.swing_resistances = levels["resistance"]
        # Check proximity to support/resistance
        price = candles_5m_window[-1]["close"] if candles_5m_window else 0
        if price > 0:
            for s in ctx.swing_supports:
                if abs(price - s) / price < 0.005:  # Within 0.5%
                    ctx.near_support = True
                    break
            for r in ctx.swing_resistances:
                if abs(price - r) / price < 0.005:
                    ctx.near_resistance = True
                    break

    # v3.2: ATR expansion ratio from 5m candles
    if len(candles_5m_window) >= 25:
        highs_5m = [c["high"] for c in candles_5m_window]
        lows_5m = [c["low"] for c in candles_5m_window]
        closes_5m = [c["close"] for c in candles_5m_window]
        atr_r = calc_atr_ratio(highs_5m, lows_5m, closes_5m, fast_period=5, slow_period=20)
        if atr_r is not None:
            ctx.atr_ratio = atr_r

    return ctx


# ================================================================
# BACKTESTER ENGINE
# ================================================================

@dataclass
class BacktestPosition:
    """A simulated position during backtest."""
    pair: str
    side: str
    entry_price: float
    entry_time: int
    shares: float
    cost_usd: float
    take_profit: float
    stop_loss: float
    atr_at_entry: float
    signal: Signal
    peak_price: float = 0.0
    trough_price: float = 999999.0
    breakeven_moved: bool = False


@dataclass
class BacktestTrade:
    """A completed backtest trade."""
    pair: str
    side: str
    entry_price: float
    exit_price: float
    entry_time: int
    exit_time: int
    pnl: float
    pnl_pct: float
    fees: float
    reason: str
    confluence_score: int
    quality_grade: str
    regime: str
    components: list
    hold_bars: int


@dataclass
class BacktestResult:
    """Aggregate backtest results."""
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    peak_bankroll: float = 0.0
    final_bankroll: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_hold_bars: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    trades: list = field(default_factory=list)
    # Breakdowns
    by_component: dict = field(default_factory=dict)
    by_pair: dict = field(default_factory=dict)
    by_regime: dict = field(default_factory=dict)
    by_grade: dict = field(default_factory=dict)
    by_exit_reason: dict = field(default_factory=dict)
    by_side: dict = field(default_factory=dict)


class ScalperBacktester:
    """Replays historical data through the scalper signal engine."""

    def __init__(self, config: dict = None, bankroll: float = 50.0):
        self.config = dict(config or SCALPER_CONFIG)
        self.bankroll = bankroll
        self.starting_bankroll = bankroll
        self.peak_bankroll = bankroll

        self.bandit = SignalBandit(decay=self.config.get("bandit_decay", 0.95))
        self.detector = SignalDetector(self.config, bandit=self.bandit)
        self.cusum = CUSUMFilter(threshold=self.config.get("cusum_threshold", 0.003))

        self.positions: list[BacktestPosition] = []
        self.trades: list[BacktestTrade] = []
        self.equity_curve: list[float] = []
        self.daily_returns: list[float] = []
        self._prev_equity = bankroll

    def run(self, pairs: list[str], days: Optional[int] = None) -> BacktestResult:
        """Run backtest across all pairs.

        Loads 5m data, derives 1h data, then replays bar-by-bar.
        """
        # Load all data
        pair_data = {}
        for symbol in pairs:
            candles_5m = load_5m_candles(symbol)
            if not candles_5m:
                logger.warning(f"No 5m data for {symbol}, skipping")
                continue
            if days:
                cutoff = candles_5m[-1]["time"] - (days * 86400)
                candles_5m = [c for c in candles_5m if c["time"] >= cutoff]
            if len(candles_5m) < 100:
                logger.warning(f"{symbol}: only {len(candles_5m)} candles, skipping (need 100+)")
                continue
            candles_1h = aggregate_to_1h(candles_5m)
            pair_data[symbol] = {"5m": candles_5m, "1h": candles_1h}
            logger.info(f"Loaded {symbol}: {len(candles_5m)} 5m candles, {len(candles_1h)} 1h candles")

        if not pair_data:
            print("No data available. Run: python scripts/download_data.py --scalper-backtest")
            return BacktestResult()

        # Build BTC returns for vol tracker
        btc_returns = []
        if "BTC" in pair_data:
            btc_5m = pair_data["BTC"]["5m"]
            for i in range(1, len(btc_5m)):
                if btc_5m[i-1]["close"] > 0:
                    btc_returns.append(
                        (btc_5m[i]["close"] - btc_5m[i-1]["close"]) / btc_5m[i-1]["close"]
                    )

        # Find the common time range across all pairs
        all_timestamps = set()
        for symbol, data in pair_data.items():
            for c in data["5m"]:
                all_timestamps.add(c["time"])
        sorted_timestamps = sorted(all_timestamps)

        # Build index: timestamp -> candle for each pair
        pair_index = {}
        for symbol, data in pair_data.items():
            idx = {}
            for c in data["5m"]:
                idx[c["time"]] = c
            pair_index[symbol] = idx

        # Build 1h index
        pair_1h_index = {}
        for symbol, data in pair_data.items():
            idx = {}
            for c in data["1h"]:
                idx[c["time"]] = c
            pair_1h_index[symbol] = idx

        # We scan every 12 bars (= 1 hour in 5m candles) to match realistic scanning
        # This avoids unrealistic signal-on-every-bar behavior
        scan_interval_bars = 12  # 1 hour

        total_bars = len(sorted_timestamps)
        warmup = 50 * 12  # 50 hours of warmup for indicators (50 1H candles)
        daily_pnl = 0.0
        trades_today = 0
        consecutive_losses = 0
        last_day = None

        logger.info(f"Backtesting {len(pair_data)} pairs over {total_bars} bars "
                     f"({total_bars * 5 / 60 / 24:.0f} days)")

        for bar_idx in range(warmup, total_bars):
            ts = sorted_timestamps[bar_idx]
            current_day = ts // 86400

            # Daily reset
            if last_day is not None and current_day != last_day:
                if self._prev_equity > 0:
                    daily_ret = (self.bankroll - self._prev_equity) / self._prev_equity
                    self.daily_returns.append(daily_ret)
                self._prev_equity = self.bankroll
                daily_pnl = 0.0
                trades_today = 0
                consecutive_losses = 0
            last_day = current_day

            # Update positions with current prices (every bar)
            self._update_positions(ts, pair_index, pair_data)

            # Check exits (every bar)
            self._check_exits(ts, pair_index, pair_data)

            # Only scan for new signals every scan_interval_bars
            if bar_idx % scan_interval_bars != 0:
                continue

            # Risk checks
            if daily_pnl <= self.config["max_daily_loss"]:
                continue
            if trades_today >= self.config["max_daily_trades"]:
                continue
            if consecutive_losses >= self.config["max_consecutive_losses"]:
                continue

            # BTC returns up to this point for vol tracker
            if "BTC" in pair_data:
                btc_5m = pair_data["BTC"]["5m"]
                # Find how many BTC candles we've seen so far
                btc_ret_idx = 0
                for i, c in enumerate(btc_5m):
                    if c["time"] <= ts:
                        btc_ret_idx = i
                    else:
                        break
                btc_rets_now = btc_returns[:max(0, btc_ret_idx)]
            else:
                btc_rets_now = []

            # Scan all pairs for signals
            all_signals = []
            for symbol in pair_data:
                # Skip if already have position in this pair
                if any(p.pair == symbol for p in self.positions):
                    continue

                # Get 5m window (last 50 candles up to current timestamp)
                candles_5m_all = pair_data[symbol]["5m"]
                # Find the index of the current timestamp
                window_end = 0
                for i, c in enumerate(candles_5m_all):
                    if c["time"] <= ts:
                        window_end = i
                    else:
                        break
                if window_end < 50:
                    continue
                window_5m = candles_5m_all[window_end - 49:window_end + 1]

                # Get 1h window (last 50 1h candles)
                hour_ts = (ts // 3600) * 3600
                candles_1h_all = pair_data[symbol]["1h"]
                window_1h_end = 0
                for i, c in enumerate(candles_1h_all):
                    if c["time"] <= hour_ts:
                        window_1h_end = i
                    else:
                        break
                if window_1h_end < 30:
                    continue
                window_1h = candles_1h_all[max(0, window_1h_end - 49):window_1h_end + 1]

                # Build context
                ctx = build_context_from_candles(
                    symbol, window_1h, window_5m, self.cusum, btc_rets_now, self.config
                )

                # Run signal detection
                signal = self.detector.analyze(f"{symbol}-USD", window_5m, ctx)
                if signal:
                    all_signals.append((signal, ctx))

            # Sort by confluence score
            all_signals.sort(
                key=lambda s: (s[0].confluence_score, GRADE_MAP.get(s[0].quality_grade, 0)),
                reverse=True
            )

            # Open up to 2 positions per scan
            opened = 0
            for signal, ctx in all_signals:
                if self._open_position(signal, ts):
                    opened += 1
                    trades_today += 1
                if opened >= 2:
                    break

            self.equity_curve.append(self.bankroll)

        # Close any remaining positions at last price
        for pos in list(self.positions):
            last_candle = pair_data.get(pos.pair, {}).get("5m", [{}])[-1]
            last_price = last_candle.get("close", pos.entry_price)
            self._close_position(pos, last_price, sorted_timestamps[-1], "backtest_end")

        return self._compile_results()

    def _open_position(self, signal: Signal, ts: int) -> bool:
        """Open a simulated position."""
        if len(self.positions) >= self.config["max_open_positions"]:
            return False

        exposure = sum(p.cost_usd for p in self.positions)
        if exposure >= self.bankroll * self.config["max_exposure_pct"]:
            return False

        # Position sizing (same as live)
        atr_pct = signal.atr / signal.price if signal.price > 0 else 0.03
        kelly_size = self.bankroll * self.config["kelly_fraction"]
        vol_adj = min(1.0, 0.02 / max(atr_pct, 0.005))
        quality_mult = {"A": 1.3, "B": 1.0, "C": 0.7, "D": 0.5}
        q_mult = quality_mult.get(signal.quality_grade, 0.7)

        position_size = min(
            self.config["max_position_usd"],
            kelly_size * vol_adj * q_mult,
        )
        position_size = max(1.0, round(position_size, 2))

        if position_size > self.bankroll:
            return False

        shares = position_size / signal.price
        pair_symbol = signal.pair.replace("-USD", "")

        self.positions.append(BacktestPosition(
            pair=pair_symbol,
            side=signal.side,
            entry_price=signal.price,
            entry_time=ts,
            shares=shares,
            cost_usd=position_size,
            take_profit=signal.take_profit,
            stop_loss=signal.stop_loss,
            atr_at_entry=signal.atr,
            signal=signal,
            peak_price=signal.price,
            trough_price=signal.price,
        ))
        self.bankroll -= position_size
        return True

    def _update_positions(self, ts: int, pair_index: dict, pair_data: dict):
        """Update positions with current bar's high/low for accurate exit simulation."""
        for pos in self.positions:
            candle = pair_index.get(pos.pair, {}).get(ts)
            if not candle:
                continue
            # Track peak/trough using the bar's high/low (not just close)
            if candle["high"] > pos.peak_price:
                pos.peak_price = candle["high"]
            if candle["low"] < pos.trough_price:
                pos.trough_price = candle["low"]

    def _check_exits(self, ts: int, pair_index: dict, pair_data: dict):
        """Check exit conditions using intra-bar high/low for realistic fills."""
        to_close = []
        for pos in self.positions:
            candle = pair_index.get(pos.pair, {}).get(ts)
            if not candle:
                continue

            is_long = pos.side == "buy"
            atr = pos.atr_at_entry

            # Use high/low for stop/TP checks (more realistic than close-only)
            bar_high = candle["high"]
            bar_low = candle["low"]
            bar_close = candle["close"]

            # 1. TAKE PROFIT
            if is_long and bar_high >= pos.take_profit:
                to_close.append((pos, "take_profit", pos.take_profit, ts))
                continue
            elif not is_long and bar_low <= pos.take_profit:
                to_close.append((pos, "take_profit", pos.take_profit, ts))
                continue

            # 2. STOP LOSS
            if is_long and bar_low <= pos.stop_loss:
                to_close.append((pos, "stop_loss", pos.stop_loss, ts))
                continue
            elif not is_long and bar_high >= pos.stop_loss:
                to_close.append((pos, "stop_loss", pos.stop_loss, ts))
                continue

            # 3. BREAKEVEN STOP
            if self.config["breakeven_at_1r"] and not pos.breakeven_moved:
                one_r = atr * self.config["sl_atr_mult"]
                if is_long and bar_high >= pos.entry_price + one_r:
                    pos.stop_loss = round(pos.entry_price + atr * 0.2, 6)
                    pos.breakeven_moved = True
                elif not is_long and bar_low <= pos.entry_price - one_r:
                    pos.stop_loss = round(pos.entry_price - atr * 0.2, 6)
                    pos.breakeven_moved = True

            # 4. TRAILING STOP (delayed activation: only after min_trail_activation_atr profit)
            trail_distance = atr * self.config["trailing_atr_mult"]
            min_activation = atr * self.config.get("min_trail_activation_atr", 0)
            if is_long:
                profit_from_entry = pos.peak_price - pos.entry_price
                if profit_from_entry >= min_activation:
                    trailing_stop = pos.peak_price - trail_distance
                    if trailing_stop > pos.stop_loss:
                        pos.stop_loss = round(trailing_stop, 6)
                if bar_low < pos.stop_loss and pos.stop_loss > pos.entry_price:
                    to_close.append((pos, "trailing_stop", pos.stop_loss, ts))
                    continue
            else:
                profit_from_entry = pos.entry_price - pos.trough_price
                if profit_from_entry >= min_activation:
                    trailing_stop = pos.trough_price + trail_distance
                    if trailing_stop < pos.stop_loss:
                        pos.stop_loss = round(trailing_stop, 6)
                if bar_high > pos.stop_loss and pos.stop_loss < pos.entry_price:
                    to_close.append((pos, "trailing_stop", pos.stop_loss, ts))
                    continue

            # 5. PROGRESSIVE STOP TIGHTENING
            # After X% of max hold time, gradually tighten SL toward entry price
            # Converts losing time_exits into earlier SL exits (reduces drag)
            elapsed_h = (ts - pos.entry_time) / 3600
            if self.config.get("progressive_stop", False):
                max_hold = self.config["max_hold_hours"]
                start_pct = self.config.get("progressive_stop_start_pct", 0.5)
                end_mult = self.config.get("progressive_stop_end_mult", 0.5)
                hold_pct = elapsed_h / max_hold if max_hold > 0 else 0

                if hold_pct >= start_pct:
                    # Linear interpolation: at start_pct use original SL, at 100% use tighter SL
                    progress = min(1.0, (hold_pct - start_pct) / (1.0 - start_pct))
                    original_sl_dist = atr * self.config["sl_atr_mult"]
                    tight_sl_dist = atr * self.config["sl_atr_mult"] * end_mult
                    current_sl_dist = original_sl_dist - (original_sl_dist - tight_sl_dist) * progress

                    if is_long:
                        new_sl = round(pos.entry_price - current_sl_dist, 6)
                        if new_sl > pos.stop_loss:
                            pos.stop_loss = new_sl
                    else:
                        new_sl = round(pos.entry_price + current_sl_dist, 6)
                        if new_sl < pos.stop_loss:
                            pos.stop_loss = new_sl

            # 6. TIME EXIT
            if elapsed_h > self.config["max_hold_hours"]:
                # Optional: extend hold for losing trades
                if self.config.get("time_exit_require_profit", False):
                    if is_long:
                        unrealized = (bar_close - pos.entry_price) * pos.shares
                    else:
                        unrealized = (pos.entry_price - bar_close) * pos.shares
                    fee_est = pos.cost_usd * self.config["taker_fee_pct"] * 2
                    if unrealized < fee_est:
                        ext_hours = self.config.get("time_exit_extension_hours", 6)
                        if elapsed_h <= self.config["max_hold_hours"] + ext_hours:
                            continue  # Hold longer, let progressive stop handle it
                to_close.append((pos, "time_exit", bar_close, ts))
                continue

        for pos, reason, price, exit_ts in to_close:
            self._close_position(pos, price, exit_ts, reason)

    def _close_position(self, pos: BacktestPosition, exit_price: float,
                         exit_ts: int, reason: str):
        """Close a position and record trade."""
        if pos.side == "buy":
            gross_pnl = (exit_price - pos.entry_price) * pos.shares
        else:
            gross_pnl = (pos.entry_price - exit_price) * pos.shares

        fee_pct = self.config["taker_fee_pct"]
        entry_fee = pos.cost_usd * fee_pct
        exit_value = exit_price * pos.shares
        exit_fee = exit_value * fee_pct
        total_fees = entry_fee + exit_fee
        pnl = round(gross_pnl - total_fees, 4)
        pnl_pct = (pnl / pos.cost_usd) * 100 if pos.cost_usd > 0 else 0

        self.bankroll += pos.cost_usd + pnl
        if self.bankroll > self.peak_bankroll:
            self.peak_bankroll = self.bankroll

        hold_bars = (exit_ts - pos.entry_time) // 300  # 5m bars

        trade = BacktestTrade(
            pair=pos.pair,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time=pos.entry_time,
            exit_time=exit_ts,
            pnl=pnl,
            pnl_pct=round(pnl_pct, 2),
            fees=round(total_fees, 4),
            reason=reason,
            confluence_score=pos.signal.confluence_score,
            quality_grade=pos.signal.quality_grade,
            regime=pos.signal.regime,
            components=pos.signal.components,
            hold_bars=hold_bars,
        )
        self.trades.append(trade)

        # Update bandit
        self.bandit.record(pos.signal.components, won=(pnl > 0))

        # Remove from positions
        self.positions = [p for p in self.positions if p is not pos]

    def _compile_results(self) -> BacktestResult:
        """Compile all trades into a BacktestResult."""
        result = BacktestResult()
        result.trades = self.trades
        result.total_trades = len(self.trades)
        result.final_bankroll = self.bankroll
        result.peak_bankroll = self.peak_bankroll

        if not self.trades:
            return result

        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        result.winning_trades = len(wins)
        result.total_pnl = sum(t.pnl for t in self.trades)
        result.gross_profit = sum(t.pnl for t in wins) if wins else 0
        result.gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0
        result.avg_win = result.gross_profit / len(wins) if wins else 0
        result.avg_loss = result.gross_loss / len(losses) if losses else 0
        result.profit_factor = (result.gross_profit / result.gross_loss
                                if result.gross_loss > 0 else float('inf'))
        result.avg_hold_bars = sum(t.hold_bars for t in self.trades) / len(self.trades)

        # Max drawdown
        peak = self.starting_bankroll
        max_dd = 0
        max_dd_usd = 0
        running = self.starting_bankroll
        for t in self.trades:
            running += t.pnl
            if running > peak:
                peak = running
            dd = (peak - running) / peak if peak > 0 else 0
            dd_usd = peak - running
            if dd > max_dd:
                max_dd = dd
            if dd_usd > max_dd_usd:
                max_dd_usd = dd_usd
        result.max_drawdown_pct = max_dd * 100
        result.max_drawdown_usd = max_dd_usd

        # Sharpe ratio from daily returns
        if len(self.daily_returns) > 1:
            mean_ret = sum(self.daily_returns) / len(self.daily_returns)
            var = sum((r - mean_ret) ** 2 for r in self.daily_returns) / len(self.daily_returns)
            std_ret = math.sqrt(var) if var > 0 else 0.001
            result.sharpe_ratio = (mean_ret / std_ret) * math.sqrt(365) if std_ret > 0 else 0

        # Consecutive wins/losses
        streak = 0
        max_w_streak = 0
        max_l_streak = 0
        for t in self.trades:
            if t.pnl > 0:
                if streak > 0:
                    streak += 1
                else:
                    streak = 1
                max_w_streak = max(max_w_streak, streak)
            else:
                if streak < 0:
                    streak -= 1
                else:
                    streak = -1
                max_l_streak = max(max_l_streak, abs(streak))
        result.max_consecutive_wins = max_w_streak
        result.max_consecutive_losses = max_l_streak

        # Breakdowns
        result.by_component = self._breakdown_by_key("components")
        result.by_pair = self._breakdown_by_key("pair")
        result.by_regime = self._breakdown_by_key("regime")
        result.by_grade = self._breakdown_by_key("quality_grade")
        result.by_exit_reason = self._breakdown_by_key("reason")
        result.by_side = self._breakdown_by_key("side")

        return result

    def _breakdown_by_key(self, key: str) -> dict:
        """Build win rate / PnL breakdown by a trade attribute."""
        stats = defaultdict(lambda: {"wins": 0, "total": 0, "pnl": 0.0})

        for t in self.trades:
            val = getattr(t, key)
            if isinstance(val, list):
                # For components (list), count each component
                for v in val:
                    stats[v]["total"] += 1
                    stats[v]["pnl"] += t.pnl
                    if t.pnl > 0:
                        stats[v]["wins"] += 1
            else:
                stats[val]["total"] += 1
                stats[val]["pnl"] += t.pnl
                if t.pnl > 0:
                    stats[val]["wins"] += 1

        # Add win rate
        for k, v in stats.items():
            v["win_rate"] = v["wins"] / v["total"] if v["total"] > 0 else 0
            v["pnl"] = round(v["pnl"], 4)

        return dict(stats)


# ================================================================
# PARAMETER OPTIMIZATION
# ================================================================

def run_optimization(pairs: list[str], days: Optional[int] = None) -> dict:
    """Sweep key parameters to find optimal settings."""
    print("\n=== PARAMETER OPTIMIZATION ===\n")

    param_grid = {
        "min_confluence_score": [2, 3, 4, 5, 6],
        "tp_atr_mult": [1.2, 1.5, 1.8, 2.5, 3.0],
        "sl_atr_mult": [0.8, 1.0, 1.2, 1.5, 2.0],
        "max_hold_hours": [1, 2, 4, 8],
    }

    results = []

    # First, test each parameter independently against the default
    for param_name, values in param_grid.items():
        print(f"\nSweeping {param_name}...")
        for val in values:
            config = dict(SCALPER_CONFIG)
            config[param_name] = val

            bt = ScalperBacktester(config=config, bankroll=50.0)
            result = bt.run(pairs, days=days)

            win_rate = result.winning_trades / result.total_trades if result.total_trades > 0 else 0
            entry = {
                "param": param_name,
                "value": val,
                "trades": result.total_trades,
                "win_rate": win_rate,
                "pnl": result.total_pnl,
                "sharpe": result.sharpe_ratio,
                "max_dd": result.max_drawdown_pct,
                "profit_factor": result.profit_factor,
            }
            results.append(entry)
            print(f"  {param_name}={val}: {result.total_trades} trades, "
                  f"{win_rate:.1%} WR, ${result.total_pnl:+.2f} PnL, "
                  f"Sharpe={result.sharpe_ratio:.2f}, DD={result.max_drawdown_pct:.1f}%")

    # Find best for each parameter
    print("\n--- OPTIMAL VALUES ---")
    best_by_param = {}
    for param_name in param_grid:
        param_results = [r for r in results if r["param"] == param_name]
        # Rank by Sharpe (risk-adjusted), then by PnL
        best = max(param_results, key=lambda r: (r["sharpe"], r["pnl"]))
        best_by_param[param_name] = best["value"]
        print(f"  {param_name} = {best['value']} "
              f"(Sharpe={best['sharpe']:.2f}, PnL=${best['pnl']:+.2f}, "
              f"WR={best['win_rate']:.1%})")

    # Run combined best
    print("\n--- COMBINED OPTIMAL ---")
    config = dict(SCALPER_CONFIG)
    config.update(best_by_param)
    bt = ScalperBacktester(config=config, bankroll=50.0)
    result = bt.run(pairs, days=days)
    win_rate = result.winning_trades / result.total_trades if result.total_trades > 0 else 0
    print(f"  {result.total_trades} trades, {win_rate:.1%} WR, "
          f"${result.total_pnl:+.2f} PnL, Sharpe={result.sharpe_ratio:.2f}")

    return {"best_params": best_by_param, "all_results": results}


# ================================================================
# REPORTING
# ================================================================

def print_report(result: BacktestResult, config: dict):
    """Print comprehensive backtest report."""
    if result.total_trades == 0:
        print("\nNo trades generated. Check data availability and signal thresholds.")
        return

    win_rate = result.winning_trades / result.total_trades
    roi = (result.total_pnl / 50.0) * 100  # Assuming $50 start

    print(f"\n{'=' * 70}")
    print(f"  SCALPER BACKTEST REPORT")
    print(f"{'=' * 70}")
    print(f"  Config: confluence >= {config['min_confluence_score']}, "
          f"TP={config['tp_atr_mult']}x ATR, SL={config['sl_atr_mult']}x ATR, "
          f"max hold={config['max_hold_hours']}h")
    print(f"{'=' * 70}")
    print(f"  Total Trades:     {result.total_trades}")
    print(f"  Win Rate:         {win_rate:.1%} ({result.winning_trades}W / "
          f"{result.total_trades - result.winning_trades}L)")
    print(f"  Total PnL:        ${result.total_pnl:+.2f} ({roi:+.1f}% ROI)")
    print(f"  Final Bankroll:   ${result.final_bankroll:.2f}")
    print(f"  Profit Factor:    {result.profit_factor:.2f}")
    print(f"  Sharpe Ratio:     {result.sharpe_ratio:.2f}")
    print(f"  Max Drawdown:     {result.max_drawdown_pct:.1f}% (${result.max_drawdown_usd:.2f})")
    print(f"  Avg Win:          ${result.avg_win:.4f}")
    print(f"  Avg Loss:         ${result.avg_loss:.4f}")
    print(f"  Avg Hold:         {result.avg_hold_bars:.0f} bars ({result.avg_hold_bars * 5:.0f} min)")
    print(f"  Max Win Streak:   {result.max_consecutive_wins}")
    print(f"  Max Loss Streak:  {result.max_consecutive_losses}")

    # Signal component breakdown
    print(f"\n  {'SIGNAL COMPONENT PERFORMANCE':^50}")
    print(f"  {'Component':<25} {'Trades':>6} {'Win%':>6} {'PnL':>10}")
    print(f"  {'-' * 50}")
    sorted_comps = sorted(result.by_component.items(),
                          key=lambda x: x[1]["pnl"], reverse=True)
    for comp, stats in sorted_comps:
        print(f"  {comp:<25} {stats['total']:>6} "
              f"{stats['win_rate']:>5.0%} ${stats['pnl']:>+9.4f}")

    # Pair breakdown
    print(f"\n  {'PAIR PERFORMANCE':^50}")
    print(f"  {'Pair':<12} {'Trades':>6} {'Win%':>6} {'PnL':>10}")
    print(f"  {'-' * 38}")
    sorted_pairs = sorted(result.by_pair.items(),
                          key=lambda x: x[1]["pnl"], reverse=True)
    for pair, stats in sorted_pairs:
        print(f"  {pair:<12} {stats['total']:>6} "
              f"{stats['win_rate']:>5.0%} ${stats['pnl']:>+9.4f}")

    # Regime breakdown
    print(f"\n  {'REGIME PERFORMANCE':^50}")
    print(f"  {'Regime':<20} {'Trades':>6} {'Win%':>6} {'PnL':>10}")
    print(f"  {'-' * 45}")
    for regime, stats in sorted(result.by_regime.items(),
                                 key=lambda x: x[1]["total"], reverse=True):
        print(f"  {regime:<20} {stats['total']:>6} "
              f"{stats['win_rate']:>5.0%} ${stats['pnl']:>+9.4f}")

    # Grade breakdown
    print(f"\n  {'GRADE PERFORMANCE':^50}")
    print(f"  {'Grade':<8} {'Trades':>6} {'Win%':>6} {'PnL':>10}")
    print(f"  {'-' * 33}")
    for grade in ["A", "B", "C", "D"]:
        if grade in result.by_grade:
            stats = result.by_grade[grade]
            print(f"  {grade:<8} {stats['total']:>6} "
                  f"{stats['win_rate']:>5.0%} ${stats['pnl']:>+9.4f}")

    # Side breakdown (buy/sell)
    if result.by_side:
        print(f"\n  {'SIDE PERFORMANCE (LONG vs SHORT)':^50}")
        print(f"  {'Side':<12} {'Trades':>6} {'Win%':>6} {'PnL':>10}")
        print(f"  {'-' * 38}")
        for side in ["buy", "sell"]:
            if side in result.by_side:
                stats = result.by_side[side]
                label = "LONG" if side == "buy" else "SHORT"
                print(f"  {label:<12} {stats['total']:>6} "
                      f"{stats['win_rate']:>5.0%} ${stats['pnl']:>+9.4f}")

    # Exit reason breakdown
    print(f"\n  {'EXIT REASON':^50}")
    print(f"  {'Reason':<18} {'Trades':>6} {'Win%':>6} {'PnL':>10}")
    print(f"  {'-' * 43}")
    for reason, stats in sorted(result.by_exit_reason.items(),
                                 key=lambda x: x[1]["total"], reverse=True):
        print(f"  {reason:<18} {stats['total']:>6} "
              f"{stats['win_rate']:>5.0%} ${stats['pnl']:>+9.4f}")

    print(f"\n{'=' * 70}")


def save_report(result: BacktestResult, config: dict, filepath: str):
    """Save backtest report to JSON."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    win_rate = result.winning_trades / result.total_trades if result.total_trades > 0 else 0

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {k: v for k, v in config.items() if not callable(v)},
        "summary": {
            "total_trades": result.total_trades,
            "winning_trades": result.winning_trades,
            "win_rate": round(win_rate, 4),
            "total_pnl": round(result.total_pnl, 4),
            "final_bankroll": round(result.final_bankroll, 2),
            "profit_factor": round(result.profit_factor, 4),
            "sharpe_ratio": round(result.sharpe_ratio, 4),
            "max_drawdown_pct": round(result.max_drawdown_pct, 2),
            "max_drawdown_usd": round(result.max_drawdown_usd, 2),
            "avg_win": round(result.avg_win, 4),
            "avg_loss": round(result.avg_loss, 4),
            "avg_hold_minutes": round(result.avg_hold_bars * 5, 1),
        },
        "by_component": result.by_component,
        "by_pair": result.by_pair,
        "by_regime": result.by_regime,
        "by_grade": result.by_grade,
        "by_exit_reason": result.by_exit_reason,
        "trade_count": len(result.trades),
    }

    with open(filepath, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport saved to: {filepath}")


# ================================================================
# MAIN
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="Crypto Scalper Historical Backtester")
    parser.add_argument("--pairs", type=str, default=None,
                       help="Comma-separated pairs (e.g. BTC,ETH,SOL). Default: all available")
    parser.add_argument("--days", type=int, default=None,
                       help="Backtest last N days only (default: all available data)")
    parser.add_argument("--optimize", action="store_true",
                       help="Run parameter optimization sweep")
    parser.add_argument("--report", action="store_true",
                       help="Save detailed JSON report")
    parser.add_argument("--min-score", type=int, default=None,
                       help="Override min confluence score")
    parser.add_argument("--tp-mult", type=float, default=None,
                       help="Override TP ATR multiplier")
    parser.add_argument("--sl-mult", type=float, default=None,
                       help="Override SL ATR multiplier")
    parser.add_argument("--max-hold", type=int, default=None,
                       help="Override max hold hours")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # Determine pairs
    if args.pairs:
        pairs = [p.strip().upper() for p in args.pairs.split(",")]
    else:
        pairs = find_available_pairs()

    if not pairs:
        print("No 5m candle data found. First download data:")
        print("  python scripts/download_data.py --scalper-backtest")
        return

    print(f"Pairs: {', '.join(pairs)}")

    # Config overrides
    config = dict(SCALPER_CONFIG)
    if args.min_score is not None:
        config["min_confluence_score"] = args.min_score
    if args.tp_mult is not None:
        config["tp_atr_mult"] = args.tp_mult
    if args.sl_mult is not None:
        config["sl_atr_mult"] = args.sl_mult
    if args.max_hold is not None:
        config["max_hold_hours"] = args.max_hold

    if args.optimize:
        opt_results = run_optimization(pairs, days=args.days)
        if args.report:
            os.makedirs(REPORT_DIR, exist_ok=True)
            filepath = os.path.join(REPORT_DIR, "scalper_optimization.json")
            with open(filepath, "w") as f:
                json.dump(opt_results, f, indent=2)
            print(f"\nOptimization results saved to: {filepath}")
        return

    # Standard backtest
    bt = ScalperBacktester(config=config, bankroll=50.0)
    result = bt.run(pairs, days=args.days)
    print_report(result, config)

    if args.report:
        os.makedirs(REPORT_DIR, exist_ok=True)
        filepath = os.path.join(REPORT_DIR, "scalper_backtest.json")
        save_report(result, config, filepath)


if __name__ == "__main__":
    main()
