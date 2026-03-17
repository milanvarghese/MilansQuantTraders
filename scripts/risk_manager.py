"""Risk manager: position sizing, exposure tracking, and circuit breakers."""

import dataclasses
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from config import CONFIG, LOG_FILE

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """An open trading position."""
    token_id: str
    market_id: str
    city: str
    bucket: str           # e.g. "46-48"
    side: str             # "BUY"
    entry_price: float
    size_shares: float
    cost_usd: float
    timestamp: str


@dataclass
class RiskState:
    """Tracks current risk exposure and P&L."""
    bankroll: float = CONFIG["bankroll"]
    open_positions: list = field(default_factory=list)
    daily_pnl: float = 0.0
    total_pnl: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    is_paused: bool = False
    pause_reason: str = ""
    last_reset_date: str = ""

    @property
    def open_exposure(self) -> float:
        return sum(p["cost_usd"] for p in self.open_positions)

    @property
    def exposure_pct(self) -> float:
        if self.bankroll <= 0:
            return 1.0
        return self.open_exposure / self.bankroll

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades


class RiskManager:
    """Enforces position limits, exposure caps, and circuit breakers."""

    STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "risk_state.json")

    def __init__(self):
        self.state = self._load_state()

    def _load_state(self) -> RiskState:
        """Load state from disk, or create fresh."""
        if os.path.exists(self.STATE_FILE):
            try:
                with open(self.STATE_FILE) as f:
                    data = json.load(f)
                # Filter unknown keys to avoid TypeError on schema changes
                known = {f.name for f in dataclasses.fields(RiskState)}
                state = RiskState(**{k: v for k, v in data.items() if k in known})
                # Auto-reset daily P&L if loaded from a prior day
                today = datetime.now(timezone.utc).date().isoformat()
                if state.last_reset_date != today:
                    state.daily_pnl = 0.0
                    state.last_reset_date = today
                    if state.is_paused and "Daily loss" in state.pause_reason:
                        state.is_paused = False
                        state.pause_reason = ""
                logger.info(f"Loaded risk state: bankroll=${state.bankroll:.2f}, "
                           f"{len(state.open_positions)} open positions")
                return state
            except Exception as e:
                logger.warning(f"Could not load risk state: {e}")
        return RiskState()

    def _save_state(self):
        """Persist state to disk."""
        os.makedirs(os.path.dirname(self.STATE_FILE), exist_ok=True)
        with open(self.STATE_FILE, "w") as f:
            json.dump(asdict(self.state), f, indent=2)

    def can_trade(self, amount_usd: float) -> tuple[bool, str]:
        """Check if a new trade of given size is allowed.

        Returns (allowed, reason).
        """
        if self.state.is_paused:
            return False, f"Trading paused: {self.state.pause_reason}"

        if len(self.state.open_positions) >= CONFIG["max_open_positions"]:
            return False, f"Max open positions reached ({CONFIG['max_open_positions']})"

        if amount_usd > CONFIG["max_position_usd"]:
            return False, f"Amount ${amount_usd:.2f} exceeds max ${CONFIG['max_position_usd']:.2f}"

        new_exposure = self.state.open_exposure + amount_usd
        max_allowed = self.state.bankroll * CONFIG["max_exposure_pct"]
        if new_exposure > max_allowed:
            return False, (f"Would exceed max exposure: "
                          f"${new_exposure:.2f} > ${max_allowed:.2f} "
                          f"({CONFIG['max_exposure_pct']:.0%} of ${self.state.bankroll:.2f})")

        if self.state.daily_pnl <= CONFIG["daily_loss_limit"]:
            self.state.is_paused = True
            self.state.pause_reason = f"Daily loss limit hit: ${self.state.daily_pnl:.2f}"
            self._save_state()
            return False, self.state.pause_reason

        return True, "OK"

    def record_entry(self, token_id: str, market_id: str, city: str,
                     bucket: str, price: float, size_shares: float, cost_usd: float):
        """Record a new position entry."""
        pos = {
            "token_id": token_id,
            "market_id": market_id,
            "city": city,
            "bucket": bucket,
            "side": "BUY",
            "entry_price": price,
            "size_shares": size_shares,
            "cost_usd": cost_usd,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.state.open_positions.append(pos)
        self.state.total_trades += 1
        self._save_state()
        self._log_trade("ENTRY", pos)
        logger.info(f"Position opened: {city} {bucket} ${cost_usd:.2f}")

    def record_exit(self, token_id: str, exit_price: float, proceeds: float):
        """Record a position exit and update P&L."""
        pos = None
        for i, p in enumerate(self.state.open_positions):
            if p["token_id"] == token_id:
                pos = self.state.open_positions.pop(i)
                break

        if pos is None:
            logger.warning(f"No open position found for token {token_id}")
            return

        pnl = proceeds - pos["cost_usd"]
        self.state.daily_pnl += pnl
        self.state.total_pnl += pnl
        self.state.bankroll += pnl

        if pnl > 0:
            self.state.winning_trades += 1

        self._save_state()
        self._log_trade("EXIT", {**pos, "exit_price": exit_price, "pnl": pnl})
        logger.info(f"Position closed: {pos['city']} {pos['bucket']} "
                    f"PnL=${pnl:+.2f} (total: ${self.state.total_pnl:+.2f})")

    def reset_daily_pnl(self):
        """Reset daily P&L counter (call at start of each trading day)."""
        self.state.daily_pnl = 0.0
        if self.state.is_paused and "Daily loss" in self.state.pause_reason:
            self.state.is_paused = False
            self.state.pause_reason = ""
        self._save_state()

    def get_summary(self) -> str:
        """Human-readable risk summary."""
        s = self.state
        return (
            f"Bankroll: ${s.bankroll:.2f} | "
            f"Open: {len(s.open_positions)} (${s.open_exposure:.2f}, {s.exposure_pct:.0%}) | "
            f"Daily P&L: ${s.daily_pnl:+.2f} | "
            f"Total P&L: ${s.total_pnl:+.2f} | "
            f"Trades: {s.total_trades} (win {s.win_rate:.0%}) | "
            f"{'PAUSED: ' + s.pause_reason if s.is_paused else 'ACTIVE'}"
        )

    def _log_trade(self, action: str, data: dict):
        """Append trade to log file."""
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            ts = datetime.now(timezone.utc).isoformat()
            f.write(f"{ts} | {action} | {json.dumps(data)}\n")
