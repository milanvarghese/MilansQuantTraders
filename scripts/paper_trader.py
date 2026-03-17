"""Paper trading engine: simulates trades with fake money.

Tracks positions, monitors price changes, calculates P&L,
and builds a track record before going live.
"""

import csv
import json
import logging
import os
import sys
import time
import argparse
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import requests
import schedule

from config import CONFIG, GAMMA_API_URL, PROXIES
from opportunity_scanner import MarketAnalyzer, Opportunity

logger = logging.getLogger(__name__)

PAPER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "paper_trading")
STATE_FILE = os.path.join(PAPER_DIR, "state.json")
TRADE_LOG = os.path.join(PAPER_DIR, "trades.csv")
PNL_LOG = os.path.join(PAPER_DIR, "daily_pnl.csv")


@dataclass
class PaperPosition:
    """A simulated open position."""
    id: str
    market_question: str
    market_id: str
    token_id: str
    category: str
    side: str               # YES or NO
    entry_price: float
    shares: float
    cost_usd: float
    estimated_prob: float
    edge_at_entry: float
    confidence: str
    reasoning: str
    end_date: str
    opened_at: str


@dataclass
class PaperState:
    """Paper trading account state."""
    bankroll: float = 50.00
    starting_bankroll: float = 50.00
    peak_bankroll: float = 50.00
    positions: list = field(default_factory=list)
    closed_trades: list = field(default_factory=list)
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: float = 0.0
    daily_pnl: float = 0.0
    daily_trade_count: int = 0
    last_scan: str = ""

    @property
    def open_exposure(self) -> float:
        return sum(p["cost_usd"] for p in self.positions)

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.get("unrealized_pnl", 0) for p in self.positions)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    @property
    def roi(self) -> float:
        if self.starting_bankroll <= 0:
            return 0.0
        return self.total_pnl / self.starting_bankroll


class PaperTrader:
    """Simulates trading on Polymarket with fake money."""

    def __init__(self, bankroll: float = None):
        os.makedirs(PAPER_DIR, exist_ok=True)
        self.state = self._load_state()
        if bankroll is not None:
            self.state.bankroll = bankroll
            self.state.starting_bankroll = bankroll
            self.state.peak_bankroll = bankroll
        self.analyzer = MarketAnalyzer()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        if PROXIES:
            self.session.proxies.update(PROXIES)

    def _load_state(self) -> PaperState:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    data = json.load(f)
                known = {f.name for f in PaperState.__dataclass_fields__.values()}
                return PaperState(**{k: v for k, v in data.items() if k in known})
            except Exception as e:
                logger.warning(f"Could not load state: {e}")
        return PaperState()

    def _save_state(self):
        with open(STATE_FILE, "w") as f:
            json.dump(asdict(self.state), f, indent=2)

    def _log_trade(self, action: str, data: dict):
        file_exists = os.path.exists(TRADE_LOG)
        with open(TRADE_LOG, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "action", "market_question", "category",
                    "side", "price", "shares", "cost_usd", "pnl",
                    "edge", "confidence", "market_id",
                ])
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                action,
                data.get("market_question", "")[:80],
                data.get("category", ""),
                data.get("side", ""),
                data.get("price", 0),
                data.get("shares", 0),
                data.get("cost_usd", 0),
                data.get("pnl", 0),
                data.get("edge", 0),
                data.get("confidence", ""),
                data.get("market_id", ""),
            ])

    def open_position(self, opp: Opportunity) -> bool:
        """Open a paper position from an opportunity signal."""
        # Risk checks
        if self.state.open_exposure + opp.kelly_size > self.state.bankroll * CONFIG["max_exposure_pct"]:
            logger.info(f"Skip: would exceed exposure limit")
            return False

        if len(self.state.positions) >= CONFIG["max_open_positions"]:
            logger.info(f"Skip: max positions reached")
            return False

        # Check for duplicate
        for pos in self.state.positions:
            if pos["market_id"] == opp.market_id:
                logger.info(f"Skip: already have position in this market")
                return False

        # Minimum order size on Polymarket is $5
        size_usd = max(5.0, min(opp.kelly_size, CONFIG["max_position_usd"], self.state.bankroll * 0.10))

        # Calculate shares
        shares = size_usd / opp.market_price

        pos_id = f"P{self.state.total_trades + 1:04d}"

        position = {
            "id": pos_id,
            "market_question": opp.market_question,
            "market_id": opp.market_id,
            "token_id": opp.token_id,
            "category": opp.category,
            "side": opp.side,
            "entry_price": opp.market_price,
            "shares": round(shares, 2),
            "cost_usd": round(size_usd, 2),
            "estimated_prob": opp.estimated_prob,
            "edge_at_entry": opp.edge,
            "confidence": opp.confidence,
            "reasoning": opp.reasoning,
            "end_date": opp.end_date,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "current_price": opp.market_price,
            "unrealized_pnl": 0.0,
        }

        self.state.positions.append(position)
        self.state.bankroll -= size_usd
        self.state.total_trades += 1
        self.state.daily_trade_count += 1
        self._save_state()

        self._log_trade("OPEN", {
            **position,
            "price": opp.market_price,
            "pnl": 0,
            "edge": opp.edge,
        })

        logger.info(
            f"PAPER OPEN [{pos_id}]: {opp.side} {opp.market_question[:50]} "
            f"@ {opp.market_price:.1%} | ${size_usd:.2f} ({shares:.1f} shares) | "
            f"edge={opp.edge:.1%}"
        )
        return True

    def update_prices(self):
        """Fetch current prices for all open positions."""
        for pos in self.state.positions:
            try:
                resp = self.session.get(
                    f"{GAMMA_API_URL}/markets/{pos['market_id']}",
                    timeout=15,
                )
                if resp.status_code != 200:
                    continue

                market = resp.json()
                tokens = market.get("tokens", [])

                for token in tokens:
                    outcome = token.get("outcome", "").lower()
                    if (pos["side"] == "YES" and outcome == "yes") or \
                       (pos["side"] == "NO" and outcome == "no"):
                        new_price = float(token.get("price", pos["entry_price"]))
                        pos["current_price"] = new_price
                        pos["unrealized_pnl"] = round(
                            (new_price - pos["entry_price"]) * pos["shares"], 2
                        )
                        break

                time.sleep(0.3)
            except Exception as e:
                logger.debug(f"Price update failed for {pos['id']}: {e}")

        self._save_state()

    def check_exits(self):
        """Check if any positions should be closed."""
        to_close = []

        for pos in self.state.positions:
            current_price = pos.get("current_price", pos["entry_price"])

            # Exit if price moved significantly in our favor
            if current_price >= 0.90:
                to_close.append((pos, "target_hit", current_price))
            # Exit if edge has reversed (price dropped below entry significantly)
            elif current_price < pos["entry_price"] * 0.5:
                to_close.append((pos, "stop_loss", current_price))
            # Check if market has ended
            elif pos.get("end_date"):
                try:
                    end = datetime.strptime(pos["end_date"], "%Y-%m-%d")
                    if datetime.now() > end:
                        to_close.append((pos, "expired", current_price))
                except (ValueError, TypeError):
                    pass

        for pos, reason, exit_price in to_close:
            self.close_position(pos, exit_price, reason)

    def close_position(self, pos: dict, exit_price: float, reason: str = "manual"):
        """Close a paper position and record P&L."""
        pnl = (exit_price - pos["entry_price"]) * pos["shares"]
        pnl = round(pnl, 2)

        self.state.bankroll += pos["cost_usd"] + pnl
        self.state.total_pnl += pnl
        self.state.daily_pnl += pnl

        if pnl > 0:
            self.state.winning_trades += 1

        if self.state.bankroll > self.state.peak_bankroll:
            self.state.peak_bankroll = self.state.bankroll

        # Move to closed trades
        closed = {
            **pos,
            "exit_price": exit_price,
            "pnl": pnl,
            "reason": reason,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.state.closed_trades.append(closed)
        self.state.positions = [p for p in self.state.positions if p["id"] != pos["id"]]

        self._save_state()
        self._log_trade("CLOSE", {
            **pos,
            "price": exit_price,
            "pnl": pnl,
        })

        logger.info(
            f"PAPER CLOSE [{pos['id']}]: {pos['side']} {pos['market_question'][:50]} "
            f"@ {exit_price:.1%} | PnL: ${pnl:+.2f} ({reason})"
        )

    def scan_and_trade(self):
        """Run a full scan and open positions on good opportunities."""
        logger.info("=" * 60)
        logger.info(f"PAPER SCAN | {datetime.now(timezone.utc).isoformat()}")
        logger.info(self.get_summary())

        # Update existing positions
        if self.state.positions:
            logger.info(f"Updating {len(self.state.positions)} open positions...")
            self.update_prices()
            self.check_exits()

        # Scan for new opportunities
        opportunities = self.analyzer.scan_all()

        opened = 0
        for opp in opportunities:
            if opp.edge >= CONFIG["entry_threshold"] and opp.confidence in ("high", "medium"):
                if self.open_position(opp):
                    opened += 1
                if opened >= 3:  # Max 3 new positions per scan
                    break

        self.state.last_scan = datetime.now(timezone.utc).isoformat()
        self._save_state()

        logger.info(f"SCAN COMPLETE | Opened {opened} new positions | "
                    f"{len(self.state.positions)} open | "
                    f"Bankroll: ${self.state.bankroll:.2f}")

    def get_summary(self) -> str:
        """Human-readable summary of paper trading state."""
        s = self.state
        lines = [
            f"{'='*60}",
            f"  PAPER TRADING DASHBOARD",
            f"{'='*60}",
            f"  Bankroll:     ${s.bankroll:.2f} (started ${s.starting_bankroll:.2f})",
            f"  Total P&L:    ${s.total_pnl:+.2f} ({s.roi:+.1%} ROI)",
            f"  Unrealized:   ${s.unrealized_pnl:+.2f}",
            f"  Peak:         ${s.peak_bankroll:.2f}",
            f"  Trades:       {s.total_trades} ({s.win_rate:.0%} win rate)",
            f"  Open:         {len(s.positions)} positions (${s.open_exposure:.2f} exposed)",
            f"  Last scan:    {s.last_scan[:19] if s.last_scan else 'never'}",
            f"{'='*60}",
        ]

        if s.positions:
            lines.append("")
            lines.append("  OPEN POSITIONS:")
            for pos in s.positions:
                upnl = pos.get("unrealized_pnl", 0)
                lines.append(
                    f"  [{pos['id']}] {pos['side']} {pos['market_question'][:45]}"
                )
                lines.append(
                    f"         Entry: {pos['entry_price']:.1%} -> "
                    f"Now: {pos.get('current_price', pos['entry_price']):.1%} | "
                    f"P&L: ${upnl:+.2f} | ${pos['cost_usd']:.2f}"
                )

        if s.closed_trades:
            lines.append("")
            recent = s.closed_trades[-5:]  # Last 5
            lines.append(f"  RECENT CLOSED ({len(s.closed_trades)} total):")
            for trade in reversed(recent):
                lines.append(
                    f"  [{trade['id']}] {trade['side']} {trade['market_question'][:45]}"
                )
                lines.append(
                    f"         {trade['entry_price']:.1%} -> {trade['exit_price']:.1%} | "
                    f"P&L: ${trade['pnl']:+.2f} ({trade['reason']})"
                )

        lines.append(f"{'='*60}")
        return "\n".join(lines)

    def run_loop(self, interval_min: int = 30):
        """Run paper trading on a schedule."""
        logger.info(f"Starting paper trading loop (every {interval_min} min)")
        logger.info(self.get_summary())

        # Run immediately
        self.scan_and_trade()

        # Schedule
        schedule.every(interval_min).minutes.do(self.scan_and_trade)

        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Paper trading stopped")
            print(self.get_summary())


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                os.path.join(PAPER_DIR, "paper_trading.log"),
                mode="a",
            ),
        ],
    )


def main():
    parser = argparse.ArgumentParser(description="Paper Trading Simulator")
    parser.add_argument("--bankroll", type=float, default=None,
                       help="Starting bankroll (default: $50)")
    parser.add_argument("--scan-once", action="store_true",
                       help="Run one scan and exit")
    parser.add_argument("--status", action="store_true",
                       help="Show current status")
    parser.add_argument("--update", action="store_true",
                       help="Update prices for open positions")
    parser.add_argument("--reset", action="store_true",
                       help="Reset paper trading (start fresh)")
    parser.add_argument("--interval", type=int, default=30,
                       help="Scan interval in minutes (default: 30)")
    parser.add_argument("--close", type=str, default=None,
                       help="Close a position by ID (e.g. P0001)")
    args = parser.parse_args()

    os.makedirs(PAPER_DIR, exist_ok=True)
    setup_logging()

    if args.reset:
        for f in [STATE_FILE, TRADE_LOG]:
            if os.path.exists(f):
                os.remove(f)
        print("Paper trading reset. Starting fresh.")
        return

    trader = PaperTrader(bankroll=args.bankroll)

    if args.status:
        if trader.state.positions:
            trader.update_prices()
        print(trader.get_summary())
        return

    if args.update:
        trader.update_prices()
        trader.check_exits()
        print(trader.get_summary())
        return

    if args.close:
        pos = None
        for p in trader.state.positions:
            if p["id"] == args.close.upper():
                pos = p
                break
        if pos:
            trader.close_position(pos, pos.get("current_price", pos["entry_price"]), "manual")
            print(f"Closed {args.close}")
        else:
            print(f"Position {args.close} not found")
        print(trader.get_summary())
        return

    if args.scan_once:
        trader.scan_and_trade()
        print(trader.get_summary())
        return

    # Run continuous loop
    trader.run_loop(interval_min=args.interval)


if __name__ == "__main__":
    main()
