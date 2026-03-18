"""Backtest laboratory for systematic crypto scalper improvements.

Runs parameterized backtests, logs every result to reports/backtest_history.csv,
and produces comparison reports. Designed for iterative model improvement.

Usage:
    python scripts/backtest_lab.py baseline              # Run baseline with current config
    python scripts/backtest_lab.py sweep-tp-sl           # Sweep TP/SL ATR multipliers
    python scripts/backtest_lab.py sweep-confluence      # Sweep min confluence score
    python scripts/backtest_lab.py sweep-regime          # Test regime filters
    python scripts/backtest_lab.py custom "description" key=val key2=val2
    python scripts/backtest_lab.py report                # Print history summary
"""

import argparse
import csv
import json
import logging
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))
from crypto_scalper import SCALPER_CONFIG
from scalper_backtester import (
    ScalperBacktester, find_available_pairs, BacktestResult, print_report
)

logger = logging.getLogger(__name__)

REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reports")
HISTORY_FILE = os.path.join(REPORT_DIR, "backtest_history.csv")


def log_result(run_id: str, description: str, result: BacktestResult,
               config: dict, config_changes: str = ""):
    """Append a backtest result to the history CSV."""
    os.makedirs(REPORT_DIR, exist_ok=True)

    wr = result.winning_trades / result.total_trades if result.total_trades > 0 else 0

    # Count exit reasons
    tp_exits = result.by_exit_reason.get("take_profit", {}).get("total", 0)
    sl_exits = result.by_exit_reason.get("stop_loss", {}).get("total", 0)
    trail_exits = result.by_exit_reason.get("trailing_stop", {}).get("total", 0)
    time_exits = result.by_exit_reason.get("time_exit", {}).get("total", 0)

    row = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "description": description,
        "trades": result.total_trades,
        "win_rate": f"{wr:.4f}",
        "total_pnl": f"{result.total_pnl:.4f}",
        "final_bankroll": f"{result.final_bankroll:.2f}",
        "profit_factor": f"{result.profit_factor:.4f}",
        "sharpe_ratio": f"{result.sharpe_ratio:.4f}",
        "max_dd_pct": f"{result.max_drawdown_pct:.2f}",
        "avg_win": f"{result.avg_win:.4f}",
        "avg_loss": f"{result.avg_loss:.4f}",
        "avg_hold_min": f"{result.avg_hold_bars * 5:.0f}",
        "tp_exits": tp_exits,
        "sl_exits": sl_exits,
        "trail_exits": trail_exits,
        "time_exits": time_exits,
        "config_changes": config_changes,
    }

    file_exists = os.path.exists(HISTORY_FILE)
    with open(HISTORY_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists or os.path.getsize(HISTORY_FILE) < 10:
            writer.writeheader()
        writer.writerow(row)

    return row


def run_test(description: str, config_overrides: dict = None,
             pairs: list = None, days: int = None, run_id: str = None) -> BacktestResult:
    """Run a single backtest with overrides, log result, print summary."""
    config = dict(SCALPER_CONFIG)
    changes_str = ""
    if config_overrides:
        config.update(config_overrides)
        changes_str = "; ".join(f"{k}={v}" for k, v in config_overrides.items())

    if pairs is None:
        pairs = find_available_pairs()

    if run_id is None:
        run_id = f"T{int(time.time()) % 100000:05d}"

    bt = ScalperBacktester(config=config, bankroll=50.0)
    result = bt.run(pairs, days=days)

    row = log_result(run_id, description, result, config, changes_str)

    wr = result.winning_trades / result.total_trades if result.total_trades > 0 else 0
    tp = result.by_exit_reason.get("take_profit", {}).get("total", 0)
    sl = result.by_exit_reason.get("stop_loss", {}).get("total", 0)

    print(f"  [{run_id}] {description}")
    print(f"    Trades={result.total_trades} WR={wr:.1%} PnL=${result.total_pnl:+.2f} "
          f"PF={result.profit_factor:.2f} Sharpe={result.sharpe_ratio:.2f} "
          f"DD={result.max_drawdown_pct:.1f}% TP={tp} SL={sl}")

    return result


def run_baseline(pairs, days):
    """Run baseline with current SCALPER_CONFIG."""
    print("\n" + "=" * 70)
    print("  BASELINE TEST (current SCALPER_CONFIG)")
    print("=" * 70)
    result = run_test("BASELINE - current config", pairs=pairs, days=days, run_id="BASE")
    print_report(result, SCALPER_CONFIG)
    return result


def sweep_tp_sl(pairs, days):
    """Sweep TP and SL ATR multipliers."""
    print("\n" + "=" * 70)
    print("  SWEEP: TP/SL ATR MULTIPLIERS")
    print("=" * 70)

    tp_values = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]
    sl_values = [0.8, 1.0, 1.2, 1.5, 2.0, 2.5]

    results = []
    for tp in tp_values:
        for sl in sl_values:
            if tp / sl < 1.2:  # Skip bad R:R ratios
                continue
            desc = f"TP={tp}x SL={sl}x ATR (R:R={tp/sl:.1f})"
            result = run_test(desc, {"tp_atr_mult": tp, "sl_atr_mult": sl},
                            pairs=pairs, days=days)
            wr = result.winning_trades / result.total_trades if result.total_trades > 0 else 0
            results.append({
                "tp": tp, "sl": sl, "rr": tp/sl,
                "trades": result.total_trades, "wr": wr,
                "pnl": result.total_pnl, "pf": result.profit_factor,
                "sharpe": result.sharpe_ratio,
            })

    # Sort by PnL
    results.sort(key=lambda r: r["pnl"], reverse=True)
    print("\n--- TOP 5 TP/SL COMBINATIONS ---")
    for i, r in enumerate(results[:5]):
        print(f"  {i+1}. TP={r['tp']}x SL={r['sl']}x (R:R={r['rr']:.1f}) "
              f"| {r['trades']} trades, {r['wr']:.1%} WR, ${r['pnl']:+.2f} PnL, "
              f"PF={r['pf']:.2f}")

    return results


def sweep_confluence(pairs, days):
    """Sweep min confluence score."""
    print("\n" + "=" * 70)
    print("  SWEEP: MIN CONFLUENCE SCORE")
    print("=" * 70)

    for score in [3, 4, 5, 6, 7, 8]:
        desc = f"min_confluence={score}"
        run_test(desc, {"min_confluence_score": score}, pairs=pairs, days=days)


def sweep_regime(pairs, days):
    """Test regime-based filters."""
    print("\n" + "=" * 70)
    print("  SWEEP: REGIME FILTERS")
    print("=" * 70)

    # Test 1: Higher bar for ranging markets
    for ranging_score in [5, 6, 7, 8, 99]:
        label = "NO RANGING" if ranging_score >= 99 else f"ranging_min={ranging_score}"
        desc = f"Regime filter: {label}"
        run_test(desc, {"min_ranging_score": ranging_score}, pairs=pairs, days=days)

    # Test 2: Trend-only (very high ranging + transitional bar)
    desc = "TREND-ONLY (block ranging+transitional)"
    run_test(desc, {"min_ranging_score": 99, "adx_trending": 20}, pairs=pairs, days=days)


def sweep_hold_time(pairs, days):
    """Sweep max hold hours."""
    print("\n" + "=" * 70)
    print("  SWEEP: MAX HOLD HOURS")
    print("=" * 70)

    for hours in [2, 4, 6, 8, 12, 24, 48]:
        desc = f"max_hold={hours}h"
        run_test(desc, {"max_hold_hours": hours}, pairs=pairs, days=days)


def sweep_combined(pairs, days):
    """Test promising combinations from individual sweeps."""
    print("\n" + "=" * 70)
    print("  COMBINED OPTIMIZATION")
    print("=" * 70)

    combos = [
        ("Wide TP + tight SL", {"tp_atr_mult": 4.0, "sl_atr_mult": 1.0}),
        ("Wide TP + tight SL + high confluence", {"tp_atr_mult": 4.0, "sl_atr_mult": 1.0, "min_confluence_score": 5}),
        ("Very wide TP + moderate SL", {"tp_atr_mult": 5.0, "sl_atr_mult": 1.5}),
        ("Wide TP + tight SL + trend only", {"tp_atr_mult": 4.0, "sl_atr_mult": 1.0, "min_ranging_score": 99}),
        ("TP=3.5 SL=1.2 score=5 hold=24h", {"tp_atr_mult": 3.5, "sl_atr_mult": 1.2, "min_confluence_score": 5, "max_hold_hours": 24}),
        ("TP=3.0 SL=1.0 score=5 no-ranging", {"tp_atr_mult": 3.0, "sl_atr_mult": 1.0, "min_confluence_score": 5, "min_ranging_score": 99}),
        ("Conservative: TP=3.5 SL=1.5 score=6", {"tp_atr_mult": 3.5, "sl_atr_mult": 1.5, "min_confluence_score": 6}),
        ("Aggressive R:R: TP=5.0 SL=1.0", {"tp_atr_mult": 5.0, "sl_atr_mult": 1.0}),
        ("TP=4.0 SL=1.2 hold=24h", {"tp_atr_mult": 4.0, "sl_atr_mult": 1.2, "max_hold_hours": 24}),
        ("Best R:R + Kelly 0.15", {"tp_atr_mult": 4.0, "sl_atr_mult": 1.0, "kelly_fraction": 0.15}),
    ]

    results = []
    for desc, overrides in combos:
        result = run_test(desc, overrides, pairs=pairs, days=days)
        wr = result.winning_trades / result.total_trades if result.total_trades > 0 else 0
        results.append((desc, result.total_pnl, wr, result.profit_factor, result))

    results.sort(key=lambda r: r[1], reverse=True)
    print("\n--- COMBINED RESULTS RANKED BY PnL ---")
    for i, (desc, pnl, wr, pf, _) in enumerate(results):
        print(f"  {i+1}. {desc}: ${pnl:+.2f} PnL, {wr:.1%} WR, PF={pf:.2f}")

    return results


def sweep_progressive_stop(pairs, days):
    """Sweep progressive stop tightening parameters."""
    print("\n" + "=" * 70)
    print("  SWEEP: PROGRESSIVE STOP TIGHTENING")
    print("=" * 70)

    # Test 1: Progressive stop on/off
    run_test("progressive_stop=OFF (baseline)", {"progressive_stop": False}, pairs=pairs, days=days)
    run_test("progressive_stop=ON (default 50%/0.5x)", {"progressive_stop": True}, pairs=pairs, days=days)

    # Test 2: Different start percentages
    for start in [0.3, 0.4, 0.5, 0.6, 0.7]:
        desc = f"prog_stop start={start:.0%} end=0.5x"
        run_test(desc, {"progressive_stop": True, "progressive_stop_start_pct": start,
                        "progressive_stop_end_mult": 0.5}, pairs=pairs, days=days)

    # Test 3: Different end multipliers (how tight the final SL gets)
    for end in [0.2, 0.3, 0.5, 0.7, 1.0]:
        desc = f"prog_stop start=50% end={end}x"
        run_test(desc, {"progressive_stop": True, "progressive_stop_start_pct": 0.5,
                        "progressive_stop_end_mult": end}, pairs=pairs, days=days)

    # Test 4: Progressive stop + time exit extension for losers
    run_test("prog_stop + extend losers 6h",
             {"progressive_stop": True, "time_exit_require_profit": True,
              "time_exit_extension_hours": 6}, pairs=pairs, days=days)
    run_test("prog_stop + extend losers 12h",
             {"progressive_stop": True, "time_exit_require_profit": True,
              "time_exit_extension_hours": 12}, pairs=pairs, days=days)


def sweep_fees(pairs, days):
    """Sweep fee tiers to understand fee sensitivity."""
    print("\n" + "=" * 70)
    print("  SWEEP: FEE SENSITIVITY")
    print("=" * 70)

    for fee in [0.0, 0.001, 0.002, 0.003, 0.004, 0.006, 0.008]:
        label = {0.0: "zero", 0.001: "Binance VIP", 0.002: "Binance/Kraken",
                 0.003: "CB Advanced", 0.004: "CB Active", 0.006: "CB Standard",
                 0.008: "CB Default"}.get(fee, f"{fee:.3f}")
        desc = f"fee={fee:.1%} ({label})"
        run_test(desc, {"taker_fee_pct": fee, "maker_fee_pct": fee * 0.6}, pairs=pairs, days=days)


def sweep_sl_tighter(pairs, days):
    """Sweep tighter SL values — current 3.5x may be too wide, causing large losses."""
    print("\n" + "=" * 70)
    print("  SWEEP: TIGHTER STOP LOSSES (with current TP=4.0)")
    print("=" * 70)

    for sl in [1.0, 1.2, 1.5, 1.8, 2.0, 2.5, 3.0, 3.5]:
        rr = 4.0 / sl
        desc = f"TP=4.0 SL={sl} (R:R={rr:.1f})"
        run_test(desc, {"sl_atr_mult": sl}, pairs=pairs, days=days)


def sweep_pair_filter(pairs, days):
    """Test with subsets of pairs — find optimal pair universe."""
    print("\n" + "=" * 70)
    print("  SWEEP: PAIR FILTERING")
    print("=" * 70)

    # All pairs baseline
    run_test("ALL pairs", pairs=pairs, days=days)

    # Remove worst performers (from baseline: DOT, AVAX, UNI, SUI)
    bad_pairs = {"DOT", "AVAX", "UNI", "SUI"}
    filtered = [p for p in pairs if p not in bad_pairs]
    run_test("DROP DOT/AVAX/UNI/SUI", pairs=filtered, days=days)

    # Only top performers (ATOM, LINK, DOGE, ETH, ADA, BTC)
    top_pairs = [p for p in pairs if p in {"ATOM", "LINK", "DOGE", "ETH", "ADA", "BTC"}]
    if top_pairs:
        run_test("TOP 6 ONLY (ATOM/LINK/DOGE/ETH/ADA/BTC)", pairs=top_pairs, days=days)

    # Large caps only
    large = [p for p in pairs if p in {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA"}]
    if large:
        run_test("LARGE CAPS (BTC/ETH/SOL/XRP/DOGE/ADA)", pairs=large, days=days)

    # Alts only (non BTC/ETH)
    alts = [p for p in pairs if p not in {"BTC", "ETH"}]
    if alts:
        run_test("ALTS ONLY (no BTC/ETH)", pairs=alts, days=days)


def sweep_v4_combos(pairs, days):
    """Test promising v4 combinations: progressive stop + tighter SL + pair filter."""
    print("\n" + "=" * 70)
    print("  V4 OPTIMIZATION COMBOS")
    print("=" * 70)

    bad_pairs = {"DOT", "AVAX", "UNI", "SUI"}
    filtered = [p for p in pairs if p not in bad_pairs]

    combos = [
        # Progressive stop variations with tighter SL
        ("prog_stop + SL=2.0", filtered,
         {"progressive_stop": True, "sl_atr_mult": 2.0}),
        ("prog_stop + SL=2.5", filtered,
         {"progressive_stop": True, "sl_atr_mult": 2.5}),
        ("prog_stop + SL=1.5 + TP=3.5", filtered,
         {"progressive_stop": True, "sl_atr_mult": 1.5, "tp_atr_mult": 3.5}),
        # Tighter SL + delayed trailing (activate at 2.5x ATR profit)
        ("SL=2.0 + trail=1.5 after 2.5x ATR", filtered,
         {"sl_atr_mult": 2.0, "trailing_atr_mult": 1.5, "min_trail_activation_atr": 2.5}),
        ("SL=2.5 + trail=2.0 after 2.0x ATR", filtered,
         {"sl_atr_mult": 2.5, "trailing_atr_mult": 2.0, "min_trail_activation_atr": 2.0}),
        # Higher confluence + tighter SL
        ("score=7 + SL=2.0", filtered,
         {"min_confluence_score": 7, "sl_atr_mult": 2.0}),
        ("score=7 + SL=2.5 + TP=5.0", filtered,
         {"min_confluence_score": 7, "sl_atr_mult": 2.5, "tp_atr_mult": 5.0}),
        # Best combo: prog stop + tighter SL + pair filter + delayed trail
        ("BEST: prog+SL=2.0+trail=1.5@2.5x+filtered", filtered,
         {"progressive_stop": True, "sl_atr_mult": 2.0, "trailing_atr_mult": 1.5,
          "min_trail_activation_atr": 2.5}),
        ("BEST2: prog+SL=2.5+trail=2.0@2.0x+filtered", filtered,
         {"progressive_stop": True, "sl_atr_mult": 2.5, "trailing_atr_mult": 2.0,
          "min_trail_activation_atr": 2.0}),
        # Binance fee tier versions of best combos
        ("BINANCE: prog+SL=2.0+trail=1.5@2.5x", filtered,
         {"progressive_stop": True, "sl_atr_mult": 2.0, "trailing_atr_mult": 1.5,
          "min_trail_activation_atr": 2.5, "taker_fee_pct": 0.001, "maker_fee_pct": 0.001}),
        ("BINANCE: SL=2.5+trail=2.0@2.0x", filtered,
         {"sl_atr_mult": 2.5, "trailing_atr_mult": 2.0, "min_trail_activation_atr": 2.0,
          "taker_fee_pct": 0.001, "maker_fee_pct": 0.001}),
        ("BINANCE: baseline (current v3.2 config)", filtered,
         {"taker_fee_pct": 0.001, "maker_fee_pct": 0.001}),
    ]

    results = []
    for desc, test_pairs, overrides in combos:
        result = run_test(desc, overrides, pairs=test_pairs, days=days)
        wr = result.winning_trades / result.total_trades if result.total_trades > 0 else 0
        results.append((desc, result.total_pnl, wr, result.profit_factor, result))

    results.sort(key=lambda r: r[1], reverse=True)
    print("\n--- V4 COMBOS RANKED BY PnL ---")
    for i, (desc, pnl, wr, pf, _) in enumerate(results):
        marker = " ***" if pf > 1.0 else ""
        print(f"  {i+1}. {desc}: ${pnl:+.2f} PnL, {wr:.1%} WR, PF={pf:.2f}{marker}")

    return results


def print_history():
    """Print backtest history summary."""
    if not os.path.exists(HISTORY_FILE):
        print("No backtest history found.")
        return

    with open(HISTORY_FILE, "r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print("No backtest runs recorded.")
        return

    print(f"\n{'=' * 100}")
    print(f"  BACKTEST HISTORY ({len(rows)} runs)")
    print(f"{'=' * 100}")
    print(f"  {'ID':<8} {'Description':<45} {'Trades':>6} {'WR':>6} {'PnL':>9} {'PF':>6} {'Sharpe':>7} {'DD%':>5}")
    print(f"  {'-' * 95}")

    for r in rows:
        try:
            pnl = float(r.get("total_pnl", 0))
            wr = float(r.get("win_rate", 0))
            pf = float(r.get("profit_factor", 0))
            sharpe = float(r.get("sharpe_ratio", 0))
            dd = float(r.get("max_dd_pct", 0))
            trades = int(r.get("trades", 0))
            desc = r.get("description", "")[:45]
            rid = r.get("run_id", "")[:8]

            # Color-code PnL
            pnl_str = f"${pnl:+.2f}"
            print(f"  {rid:<8} {desc:<45} {trades:>6} {wr:>5.1%} {pnl_str:>9} {pf:>6.2f} {sharpe:>7.2f} {dd:>5.1f}")
        except (ValueError, KeyError):
            continue

    # Find best run
    valid = [r for r in rows if r.get("total_pnl")]
    if valid:
        best = max(valid, key=lambda r: float(r["total_pnl"]))
        print(f"\n  BEST RUN: {best['run_id']} - {best['description']}")
        print(f"    PnL=${float(best['total_pnl']):+.2f}, WR={float(best['win_rate']):.1%}, "
              f"PF={float(best['profit_factor']):.2f}")
        if best.get("config_changes"):
            print(f"    Config: {best['config_changes']}")

    print(f"{'=' * 100}")


def run_all_sweeps(pairs, days):
    """Run all sweep types in sequence."""
    print("\n" + "#" * 70)
    print("  FULL OPTIMIZATION SUITE")
    print("#" * 70)

    run_baseline(pairs, days)
    sweep_tp_sl(pairs, days)
    sweep_confluence(pairs, days)
    sweep_regime(pairs, days)
    sweep_hold_time(pairs, days)
    sweep_combined(pairs, days)
    print_history()


def main():
    parser = argparse.ArgumentParser(description="Backtest Laboratory")
    parser.add_argument("command", nargs="?", default="baseline",
                       help="baseline|sweep-tp-sl|sweep-confluence|sweep-regime|"
                            "sweep-hold|sweep-combined|sweep-all|custom|report")
    parser.add_argument("args", nargs="*", help="For custom: 'description' key=val key2=val2")
    parser.add_argument("--pairs", type=str, default=None)
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    pairs = [p.strip().upper() for p in args.pairs.split(",")] if args.pairs else None

    cmd = args.command.lower()
    all_pairs = pairs or find_available_pairs()

    if cmd == "report":
        print_history()
    elif cmd == "baseline":
        run_baseline(all_pairs, args.days)
    elif cmd == "sweep-tp-sl":
        sweep_tp_sl(all_pairs, args.days)
    elif cmd == "sweep-confluence":
        sweep_confluence(all_pairs, args.days)
    elif cmd == "sweep-regime":
        sweep_regime(all_pairs, args.days)
    elif cmd == "sweep-hold":
        sweep_hold_time(all_pairs, args.days)
    elif cmd == "sweep-combined":
        sweep_combined(all_pairs, args.days)
    elif cmd == "sweep-progressive":
        sweep_progressive_stop(all_pairs, args.days)
    elif cmd == "sweep-fees":
        sweep_fees(all_pairs, args.days)
    elif cmd == "sweep-sl":
        sweep_sl_tighter(all_pairs, args.days)
    elif cmd == "sweep-pairs":
        sweep_pair_filter(all_pairs, args.days)
    elif cmd == "sweep-v4":
        sweep_v4_combos(all_pairs, args.days)
    elif cmd == "sweep-all":
        run_all_sweeps(all_pairs, args.days)
    elif cmd == "custom":
        if len(args.args) < 1:
            print("Usage: custom 'description' key=val key2=val2")
            return
        desc = args.args[0]
        overrides = {}
        for kv in args.args[1:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                try:
                    overrides[k] = json.loads(v)
                except json.JSONDecodeError:
                    overrides[k] = v
        run_test(desc, overrides, pairs=all_pairs, days=args.days)
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: baseline, sweep-tp-sl, sweep-confluence, sweep-regime, "
              "sweep-hold, sweep-combined, sweep-progressive, sweep-fees, "
              "sweep-sl, sweep-pairs, sweep-v4, sweep-all, custom, report")


if __name__ == "__main__":
    main()
