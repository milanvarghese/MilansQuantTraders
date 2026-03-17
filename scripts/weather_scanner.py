"""Main scanning loop: finds mispriced weather buckets and executes trades."""

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone

import schedule

from config import CONFIG, CITIES, PAPER_TRADE_LOG
from noaa_client import WeatherClient
from probability_engine import find_signals, BucketSignal
from polymarket_client import GammaClient, PolymarketTrader
from risk_manager import RiskManager
from alerts import send_telegram

logger = logging.getLogger(__name__)


class WeatherScanner:
    """Scans weather markets for mispriced temperature buckets."""

    def __init__(self, paper_mode: bool = True):
        self.paper_mode = paper_mode
        self.weather = WeatherClient()
        self.gamma = GammaClient()
        self.risk = RiskManager()
        self.trader = None

        if not paper_mode:
            self.trader = PolymarketTrader()

    def scan_once(self) -> list[BucketSignal]:
        """Run a single scan across all cities.

        Returns list of signals found (traded or logged).
        """
        logger.info("=" * 60)
        logger.info(f"SCAN START | mode={'PAPER' if self.paper_mode else 'LIVE'} | "
                    f"{datetime.now(timezone.utc).isoformat()}")
        logger.info(self.risk.get_summary())

        all_signals = []

        for city in CITIES:
            try:
                signals = self._scan_city(city)
                all_signals.extend(signals)
            except Exception as e:
                logger.error(f"Error scanning {city}: {e}")

        logger.info(f"SCAN COMPLETE | {len(all_signals)} signals found")
        return all_signals

    def _scan_city(self, city: str) -> list[BucketSignal]:
        """Scan a single city for trading signals."""
        # 1. Get weather forecast (NOAA for US, Open-Meteo for international)
        forecast_high = self.weather.get_forecast_high_celsius(city)
        if forecast_high is None:
            logger.warning(f"Skipping {city}: no forecast data")
            return []

        # 2. Get active markets for this city
        markets = self.gamma.search_weather_markets(city)
        if not markets:
            logger.info(f"No active temperature markets for {city}")
            return []

        # 3. Parse buckets and find signals
        city_signals = []
        for market in markets:
            buckets = self.gamma.parse_temperature_buckets(market)
            if not buckets:
                continue

            signals = find_signals(
                city=city,
                forecast_high=forecast_high,
                market_buckets=buckets,
                bankroll=self.risk.state.bankroll,
            )

            for signal in signals:
                self._handle_signal(signal)
                city_signals.append(signal)

        return city_signals

    def _handle_signal(self, signal: BucketSignal):
        """Either paper-trade or live-trade a signal."""
        if self.paper_mode:
            self._paper_trade(signal)
            return

        # Live trading
        allowed, reason = self.risk.can_trade(signal.kelly_size)
        if not allowed:
            logger.info(f"Trade blocked: {reason}")
            return

        order_price = signal.market_price + 0.01  # Slight premium for fill
        resp = self.trader.buy(
            token_id=signal.token_id,
            amount_usd=signal.kelly_size,
            price=order_price,
        )

        if resp:
            self.risk.record_entry(
                token_id=signal.token_id,
                market_id=signal.market_id,
                city=signal.city,
                bucket=f"{signal.bucket_low}-{signal.bucket_high}",
                price=order_price,
                size_shares=signal.kelly_size / order_price,
                cost_usd=signal.kelly_size,
            )
            alert = (
                f"🔔 <b>TRADE</b>: {signal.city} {signal.bucket_low}-{signal.bucket_high}°C\n"
                f"Edge: {signal.edge:.1%} | Size: ${signal.kelly_size:.2f}"
            )
            send_telegram(alert)
        else:
            logger.error(f"Order failed for {signal.city} {signal.bucket_low}-{signal.bucket_high}")

    def _paper_trade(self, signal: BucketSignal):
        """Log a hypothetical trade for paper trading."""
        os.makedirs(os.path.dirname(PAPER_TRADE_LOG), exist_ok=True)

        file_exists = os.path.exists(PAPER_TRADE_LOG)
        with open(PAPER_TRADE_LOG, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "city", "bucket_low", "bucket_high",
                    "model_prob", "market_price", "edge", "kelly_size",
                    "market_id", "token_id",
                ])
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                signal.city,
                signal.bucket_low,
                signal.bucket_high,
                signal.model_prob,
                signal.market_price,
                signal.edge,
                signal.kelly_size,
                signal.market_id,
                signal.token_id,
            ])

        logger.info(
            f"PAPER TRADE: {signal.city} {signal.bucket_low}-{signal.bucket_high}°C | "
            f"edge={signal.edge:.1%} size=${signal.kelly_size:.2f}"
        )

    def check_exits(self):
        """Check open positions for exit conditions."""
        if self.paper_mode:
            return

        for pos in list(self.risk.state.open_positions):
            try:
                market = self.gamma.get_market_detail(pos["market_id"])
                if not market:
                    continue

                # Find current price for our token
                for token in market.get("tokens", []):
                    if token.get("token_id") == pos["token_id"]:
                        current_price = float(token.get("price", 0))

                        if current_price >= CONFIG["exit_threshold"]:
                            size = pos["size_shares"]
                            sell_price = current_price - 0.01
                            resp = self.trader.sell(
                                token_id=pos["token_id"],
                                size=size,
                                price=sell_price,
                            )
                            if resp:
                                proceeds = size * sell_price
                                self.risk.record_exit(
                                    token_id=pos["token_id"],
                                    exit_price=current_price,
                                    proceeds=proceeds,
                                )
                                alert = (
                                    f"💰 <b>EXIT</b>: {pos['city']} {pos['bucket']}\n"
                                    f"Entry: ${pos['entry_price']:.3f} → Exit: ${current_price:.3f}"
                                )
                                send_telegram(alert)
                        break
            except Exception as e:
                logger.error(f"Exit check failed for {pos.get('city')}: {e}")

    def run_loop(self):
        """Run the scanner on a schedule."""
        mode = "PAPER" if self.paper_mode else "LIVE"
        logger.info(f"Starting weather scanner in {mode} mode")
        logger.info(f"Scan interval: {CONFIG['scan_interval_min']} minutes")
        logger.info(self.risk.get_summary())

        if not self.paper_mode and self.trader:
            self.trader.start_heartbeat()

        # Run immediately on start
        self.scan_once()
        self.check_exits()

        # Schedule recurring scans
        schedule.every(CONFIG["scan_interval_min"]).minutes.do(self.scan_once)
        schedule.every(CONFIG["scan_interval_min"]).minutes.do(self.check_exits)
        schedule.every().day.at("00:00").do(self.risk.reset_daily_pnl)

        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Scanner stopped by user")
            if self.trader:
                self.trader.stop_heartbeat()


def setup_logging():
    """Configure logging to console and file."""
    from config import LOG_FILE
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE),
        ],
    )


def main():
    parser = argparse.ArgumentParser(description="Polymarket Weather Trading Bot")
    parser.add_argument(
        "--live", action="store_true",
        help="Run in live trading mode (default: paper trading)"
    )
    parser.add_argument(
        "--scan-once", action="store_true",
        help="Run a single scan and exit"
    )
    parser.add_argument(
        "--derive-creds", action="store_true",
        help="Derive Polymarket L2 API credentials from wallet"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show current risk/position status"
    )
    args = parser.parse_args()

    setup_logging()

    if args.derive_creds:
        trader = PolymarketTrader()
        creds = trader.derive_api_creds()
        print("\n=== Add these to your .env file ===")
        print(f"POLY_API_KEY={creds.get('apiKey', '')}")
        print(f"POLY_API_SECRET={creds.get('secret', '')}")
        print(f"POLY_API_PASSPHRASE={creds.get('passphrase', '')}")
        return

    if args.status:
        risk = RiskManager()
        print(risk.get_summary())
        if risk.state.open_positions:
            print("\nOpen positions:")
            for p in risk.state.open_positions:
                print(f"  {p['city']} {p['bucket']} | ${p['cost_usd']:.2f} @ {p['entry_price']:.3f}")
        return

    scanner = WeatherScanner(paper_mode=not args.live)

    if args.scan_once:
        signals = scanner.scan_once()
        print(f"\nFound {len(signals)} signals")
        for s in signals:
            print(f"  {s.city} {s.bucket_low}-{s.bucket_high}°C | "
                  f"edge={s.edge:.1%} size=${s.kelly_size:.2f}")
    else:
        scanner.run_loop()


if __name__ == "__main__":
    main()
