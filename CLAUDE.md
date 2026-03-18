# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated trading system with two independent bots:
1. **Crypto Scalper** (`scripts/crypto_scalper.py`) - Confluence-based crypto spot trading via Coinbase/Binance APIs
2. **Polymarket Paper Trader** (`scripts/paper_trader.py`) - Prediction market trading with probability estimation via weather ensembles, crypto prices, and market analysis

Both run 24/7 on an Oracle Cloud server (129.153.155.228) with a Flask dashboard (`scripts/dashboard.py`) on port 8080.

## Commands

```bash
# Activate virtualenv
source venv/bin/activate  # Linux/server
venv\Scripts\activate     # Windows local

# Crypto scalper (v3.2: 17 signals, backtest-optimized exits)
python scripts/crypto_scalper.py --interval 60           # Run with 60-sec scans
python scripts/crypto_scalper.py --status                 # Print current state
python scripts/crypto_scalper.py --reset                  # Wipe state for fresh start
python scripts/crypto_scalper.py --scan-once --verbose    # Single scan with debug output
python scripts/crypto_scalper.py --pairs "BTC,ETH" --min-score 5  # Static pairs (disables dynamic)

# Scalper backtester (validate signal engine against historical data)
python scripts/download_data.py --scalper-backtest        # Download 5m data for all pairs (90d)
python scripts/scalper_backtester.py                      # Backtest all pairs with data
python scripts/scalper_backtester.py --optimize --report  # Parameter optimization sweep
python scripts/scalper_backtester.py --pairs BTC,ETH --days 30 --tp-mult 3.0

# Backtest laboratory (systematic testing with history tracking)
python scripts/backtest_lab.py baseline                   # Run baseline with current config
python scripts/backtest_lab.py sweep-tp-sl                # Sweep TP/SL ATR multipliers
python scripts/backtest_lab.py sweep-confluence            # Sweep min confluence score
python scripts/backtest_lab.py sweep-combined              # Test promising combinations
python scripts/backtest_lab.py custom "description" key=val  # Custom parameter test
python scripts/backtest_lab.py report                      # Print full backtest history

# Polymarket paper trader
python scripts/paper_trader.py --interval 5 --update-interval 1  # 5-min scans, 1-min price updates
python scripts/paper_trader.py --status
python scripts/paper_trader.py --reset
python scripts/paper_trader.py --bankroll 50.0

# Dashboard
python scripts/dashboard.py --port 8080   # Auth: DASH_USER/DASH_PASS from .env

# Opportunity scanner (finds mispriced Polymarket markets)
python scripts/opportunity_scanner.py --scan-once
```

## Architecture

### Crypto Scalper (`scripts/crypto_scalper.py` ~2500 lines)
Self-contained trading engine with no imports from other project modules. Key classes:
- `PairScanner` - Dynamically discovers 60-80+ liquid USD pairs from Coinbase product catalog (refreshes hourly)
- `CoinbaseDataClient` / `BinanceDataClient` - Market data (OHLCV candles, order book, funding rates)
- `SignalDetector` - 17-signal confluence scoring (0-17 scale): regime detection (ADX), multi-timeframe (1H+5M), order book imbalance, funding rates, FVG, volume profile, RSI, Bollinger, MACD, RSI divergence, engulfing patterns, swing S/R, ATR expansion
- `CryptoScalper` - Main loop: scan pairs, score signals, open/manage/close positions
- Dynamic pair discovery: scans Coinbase for all USD pairs, filters by 24h volume ($500K+) and spread (<0.15%)
- Dynamic ATR exits: TP=4.0x 1H ATR, SL=3.5x 1H ATR, no trailing (backtest-optimized v3.2)
- Auto-tunes every 20 closed trades via `_auto_tune()` (adjusts thresholds, drops losing pairs)
- Records full market context per trade in `analytics.jsonl` for post-hoc analysis

### Polymarket Side (`scripts/paper_trader.py`, `opportunity_scanner.py`, `probability_engine.py`, `risk_manager.py`)
These modules work together:
- `config.py` - Central parameters (thresholds, Kelly fractions, drawdown levels, API URLs)
- `probability_engine.py` - Multi-model KDE using 120+ ensemble members (ECMWF+GFS+ICON), fee-aware Kelly sizing
- `risk_manager.py` - 12 circuit breakers, graduated drawdown scaling (100%/75%/50%/25%/pause)
- `opportunity_scanner.py` - Discovers mispriced markets across weather, crypto, sports, politics
- `paper_trader.py` - Simulates trades, tracks P&L, manages exits (edge reversal, trailing stop, time-based)
- `polymarket_client.py` - GAMMA API (market discovery) + CLOB API (order execution) with Cloudflare retry
- `noaa_client.py` - Weather data from NOAA (US) and Open-Meteo (worldwide)

### State Files
- `crypto_trading/state.json` - Scalper positions, bankroll, counters
- `crypto_trading/trades.csv` - Closed trade history
- `crypto_trading/analytics.json` - Full market context per trade (for ML/analysis)
- `crypto_trading/tuning.json` - Auto-tuner adjustments log
- `paper_trading/state.json` - Paper trader positions, bankroll
- `paper_trading/trades.csv` - Paper trade history
- `logs/risk_state.json` - Risk manager persistent state

### Critical Implementation Details
- **1H ATR exits**: Exit levels (TP/SL) use 1H ATR, not 5m ATR. v3.2 backtest (117 runs, 14 pairs, 90 days): TP=4.0x ATR, SL=3.5x ATR, no trailing. Profitable at Binance fees (PF=1.18, +$3.56), break-even at CB Advanced (PF=1.00). Key finding: trailing stop (was 1.5x ATR) was destroying edge by cutting winners short
- **Atomic state writes**: Both bots write to temp file then `os.replace()` to prevent corruption on crash
- **Wilder's smoothing**: Used for RSI, ATR, ADX (not simple moving averages)
- **MACD**: Running EMA series via `calc_ema_series()`, not re-seeded subset EMAs
- **Order book depth**: Measured by 2.5% price depth from mid-price, not by level count
- **Daily reset**: Counters reset at midnight UTC via `_check_daily_reset()`
- **Loss cooldown**: 3 consecutive losses triggers 4-hour cooldown (not permanent halt)
- **Fee model**: Polymarket charges 2% on net winnings (exit proceeds), not on notional

## Server Deployment

```bash
# SSH to server
ssh -i ssh-key-2026-03-17.key ubuntu@129.153.155.228

# Processes run from /home/ubuntu/polymarket-bot/
# Start all three:
cd /home/ubuntu/polymarket-bot
nohup venv/bin/python scripts/crypto_scalper.py --interval 30 > /dev/null 2>&1 &
nohup venv/bin/python scripts/paper_trader.py --interval 5 --update-interval 1 > /dev/null 2>&1 &
nohup venv/bin/python scripts/dashboard.py --port 8080 > /dev/null 2>&1 &
```

## Data Sources (all free, no API keys needed)

- **Coinbase**: OHLCV candles, order book (L2), ticker prices
- **Binance**: Perpetual funding rates (public endpoint)
- **Open-Meteo**: Weather forecasts + ensemble data (ECMWF/GFS/ICON)
- **NOAA**: US weather forecasts (User-Agent header required, no key)
- **CoinGecko**: Crypto prices for Polymarket opportunity scanning
- **Polymarket GAMMA API**: Market discovery, prices, orderbook

## Environment Variables (.env)

Required for live trading only (paper trading works without):
- `PRIVATE_KEY` - Polygon wallet private key
- `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE` - CLOB API credentials
- `DASH_USER`, `DASH_PASS` - Dashboard authentication
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` - Alert notifications
- `HTTP_PROXY` - Optional, for Cloudflare/geo-blocking issues
