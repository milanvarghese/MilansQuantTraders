"""Configuration for the Polymarket weather trading bot."""

import os
from dotenv import load_dotenv

load_dotenv()

# === Trading Parameters ===
CONFIG = {
    "entry_threshold": 0.08,       # Min 8% edge to enter
    "exit_threshold": 0.45,        # Sell when bucket price > 45c
    "max_position_usd": 2.00,      # Max $2 per bucket
    "max_open_positions": 5,       # Max 5 concurrent positions
    "max_exposure_pct": 0.30,      # Max 30% of bankroll at risk
    "daily_loss_limit": -5.00,     # Auto-pause if -$5 on the day
    "kelly_fraction": 0.25,        # Quarter-Kelly sizing
    "scan_interval_min": 30,       # Scan every 30 minutes
    "min_hours_to_resolution": 6,  # Don't trade < 6hrs before resolution
    "sigma_c": 1.1,                # Forecast uncertainty (°C) — ~2°F
    "bankroll": 43.00,             # Starting trading capital in USDC
    "heartbeat_interval": 10,      # Seconds between heartbeats
    # --- Advanced Risk Checks ---
    "max_spread": 0.15,             # Skip markets with bid-ask spread > 15%
    "min_liquidity_usd": 50.0,     # Skip markets with < $50 orderbook depth
    "min_hours_to_resolution": 6,  # Don't trade < 6hrs before resolution (override above)
    "max_drawdown_pct": 0.25,      # Auto-kill if bankroll drops 25% from peak
    "max_consecutive_losses": 5,   # Pause after 5 straight losses
    "max_daily_trades": 20,        # Max trades per day
    "cf_max_retries": 3,           # Cloudflare retry attempts
    "cf_base_delay": 2.0,          # Base delay for exponential backoff (seconds)
}

# === City Configurations ===
# lat, lon for forecast lookups. source: "noaa" (US only) or "openmeteo" (worldwide)
CITIES = {
    "NYC": {"lat": 40.7128, "lon": -74.0060, "source": "noaa"},
    "Chicago": {"lat": 41.8781, "lon": -87.6298, "source": "noaa"},
    "Dallas": {"lat": 32.7767, "lon": -96.7970, "source": "noaa"},
    "Seattle": {"lat": 47.6062, "lon": -122.3321, "source": "noaa"},
    "Atlanta": {"lat": 33.7490, "lon": -84.3880, "source": "noaa"},
    "Miami": {"lat": 25.7617, "lon": -80.1918, "source": "noaa"},
    "Tel Aviv": {"lat": 32.0853, "lon": 34.7818, "source": "openmeteo"},
    "Seoul": {"lat": 37.5665, "lon": 126.9780, "source": "openmeteo"},
    "Shanghai": {"lat": 31.2304, "lon": 121.4737, "source": "openmeteo"},
    "London": {"lat": 51.5074, "lon": -0.1278, "source": "openmeteo"},
    "Tokyo": {"lat": 35.6762, "lon": 139.6503, "source": "openmeteo"},
}

# Open-Meteo API (free, no key, worldwide)
OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"

# === API Endpoints ===
NOAA_BASE_URL = "https://api.weather.gov"
NOAA_USER_AGENT = "(polymarket-weather-bot, contact@example.com)"

GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"

# === Wallet / Auth ===
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
POLY_API_KEY = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE", "")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))

# === Proxy ===
HTTP_PROXY = os.getenv("HTTP_PROXY", "")
PROXIES = {"http": HTTP_PROXY, "https": HTTP_PROXY} if HTTP_PROXY else {}

# === Telegram Alerts ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# === Logging ===
LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "trades.log")
PAPER_TRADE_LOG = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "paper_trades.csv")
