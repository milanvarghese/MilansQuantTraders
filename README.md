# Automated Trading System

Three independent trading bots with a unified web dashboard, running 24/7 on Oracle Cloud.

## Bots

### Crypto Scalper (`scripts/crypto_scalper.py`)
Confluence-based spot trading on Coinbase/Binance. Scans a curated whitelist (BTC, ETH, DOGE, ADA, ATOM, LINK) using 17-signal scoring: regime detection (ADX), multi-timeframe analysis (1H+5M), order book imbalance, funding rates, fair value gaps, volume profile, RSI, Bollinger Bands, MACD, RSI divergence, engulfing patterns, swing S/R, and ATR expansion.

- **Exits**: TP at 4.0x ATR, SL at 3.5x ATR with progressive tightening
- **Auto-tuning**: Adjusts thresholds and drops losing pairs every 20 trades
- **Backtested**: PF 1.77, 59.6% WR, 2.3% max drawdown over 90 days (166 trades at 0.1% fees)

### Stock Trader (`scripts/stock_trader.py`)
12-signal confluence stock trading via Alpaca API across 110 instruments (mega cap, growth/tech, sector leaders, high-beta, ETFs). Signals include daily/weekly trend EMAs, RSI bounce, MACD cross, volume spike, relative strength vs SPY, Bollinger band, VWAP reclaim, 52-week proximity, sector momentum, earnings shield, and market regime.

- **Regime detection**: SPY/VIX-based (Bull/Cautious/Bear/HighVol/Choppy) with anti-whipsaw
- **Position sizing**: Kelly criterion with volatility adjustment
- **Risk management**: Daily loss limit (-3%), consecutive loss cooldown, drawdown pause (-15%), kill switch (-25%)
- **Signal Bandit**: Thompson Sampling auto-weights signals by recent success

### Polymarket Paper Trader (`scripts/paper_trader.py`)
Prediction market trading using probability estimation from weather ensembles (120+ ECMWF/GFS/ICON members), crypto prices, and sports/finance analysis.

- **Opportunity scanner** discovers mispriced markets across weather, crypto, sports, and politics
- **Fee-aware Kelly sizing** with graduated drawdown scaling (100%/75%/50%/25%/pause)
- **12 circuit breakers** in the risk manager
- **Near-expiry harvesting** for high-probability markets close to resolution

## Dashboard

Flask web dashboard (`scripts/dashboard.py`) on port 8080 with HTTP basic auth. Tabbed view showing each bot's:
- Real-time P&L charts
- Open positions with live prices
- Trade history and activity logs

## Quick Start

```bash
# Clone and install
git clone https://github.com/milanvarghese/PolymarketWeatherBot.git
cd PolymarketWeatherBot
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt

# Configure API keys
cp .env.example .env  # Edit with your keys

# Run individual bots
python scripts/crypto_scalper.py --interval 30
python scripts/stock_trader.py --interval 900
python scripts/paper_trader.py --interval 5 --update-interval 1

# Dashboard
python scripts/dashboard.py --port 8080

# Scan for opportunities
python scripts/opportunity_scanner.py --scan-once
```

## Backtesting

```bash
# Download historical data
python scripts/download_data.py --scalper-backtest

# Run backtest
python scripts/scalper_backtester.py --optimize --report

# Backtest laboratory (systematic parameter sweeps)
python scripts/backtest_lab.py baseline
python scripts/backtest_lab.py sweep-tp-sl
python scripts/backtest_lab.py sweep-confluence
python scripts/backtest_lab.py report
```

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Alpaca stock trading API |
| `ALPACA_BASE_URL` | Alpaca endpoint (default: paper trading) |
| `PRIVATE_KEY` | Polygon wallet private key |
| `POLY_API_KEY` / `POLY_API_SECRET` / `POLY_API_PASSPHRASE` | Polymarket CLOB API |
| `ODDS_API_KEY` | Sports bookmaker odds (the-odds-api.com) |
| `DASH_USER` / `DASH_PASS` | Dashboard authentication |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Alert notifications |

## Project Structure

```
scripts/
  crypto_scalper.py      # Crypto spot trading bot (3200 lines)
  stock_trader.py        # Stock trading bot (2000 lines)
  paper_trader.py        # Polymarket paper trader
  dashboard.py           # Flask web dashboard
  opportunity_scanner.py # Mispriced market discovery
  probability_engine.py  # Multi-model KDE probability estimation
  risk_manager.py        # 12 circuit breakers, drawdown scaling
  scalper_backtester.py  # Crypto backtesting engine
  backtest_lab.py        # Systematic parameter sweep lab
  config.py              # Polymarket trading parameters
  polymarket_client.py   # GAMMA + CLOB API client
  noaa_client.py         # Weather data (NOAA + Open-Meteo)

crypto_trading/          # Crypto bot state, trades, analytics
stock_trading/           # Stock bot state, trades, analytics
paper_trading/           # Polymarket paper trading state
reports/                 # Backtest results and research
```

## Data Sources

All free tier or API-key based:
- **Coinbase** / **Binance** - Crypto OHLCV, order books, funding rates
- **Alpaca** - Stock bars, quotes, orders (commission-free, fractional shares)
- **Polymarket** - GAMMA API (discovery) + CLOB API (execution)
- **Open-Meteo** - Weather ensemble forecasts (ECMWF/GFS/ICON)
- **NOAA** - US weather forecasts
- **CoinGecko** - Crypto prices for opportunity scanning
- **The Odds API** - Sports bookmaker odds

## License

Private project. Not for redistribution.
