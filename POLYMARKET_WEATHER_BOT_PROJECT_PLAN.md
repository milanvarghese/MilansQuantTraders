# Polymarket Weather Trading Bot — Project Plan

> **Budget:** $50 starter | **Stack:** Python + NOAA API + Polymarket CLOB | **Timeline:** 4 weeks to first live trade
>
> *This is not financial advice. Prediction markets carry real risk of total loss. Only trade with money you can afford to lose entirely.*

---

## 1. Project Overview

Build an automated bot that trades Polymarket weather temperature markets by comparing NOAA forecast data against market-implied probabilities. When the bot detects a mispricing (e.g., NOAA says 70% chance of 48–50°F but the market prices it at 12%), it buys the undervalued bucket and sells when the price converges.

### Why This Works (In Theory)

- NOAA's 1–2 day temperature forecasts are **85–90% accurate**
- Polymarket weather buckets are priced by retail participants trading on intuition
- The structural information gap creates exploitable mispricings
- Weather markets currently have **zero maker/taker fees** on Polymarket

### Why $50 Is Tuition, Not Seed Capital

- At $1–2 per trade with a hypothetical 3% weekly edge → ~$1.50/week profit
- 92.4% of Polymarket traders lose money overall
- The top performers run multi-model ensembles on dedicated infrastructure
- Treat this as a learning project; scale only after 100+ trades with >55% win rate

---

## 2. Architecture Options

### Option A: OpenClaw + Simmer SDK (Fastest Setup)

```
Telegram ──→ OpenClaw Agent ──→ simmer-weather skill ──→ Simmer SDK ──→ Polymarket CLOB
                  │                      │
                  └── LLM (Claude/GPT) ──┘── NOAA Weather API
```

**Pros:** 30-minute setup, pre-built trading logic, built-in safeguards
**Cons:** Black-box strategy, Simmer is alpha (invite-only), OpenClaw malware risk (20% of ClawHub skills were purged), less control over execution

### Option B: Custom Python Bot (Recommended for Developers)

```
Cron/Scheduler ──→ weather_scanner.py ──→ NOAA API
                          │
                          ├──→ probability_engine.py (GFS ensemble → bucket probabilities)
                          │
                          ├──→ polymarket_client.py (py-clob-client → order execution)
                          │
                          └──→ risk_manager.py (position sizing, circuit breakers)
```

**Pros:** Full control, auditable code, no third-party trust, customizable strategy
**Cons:** 2–4 weeks build time, ongoing maintenance, must handle Cloudflare blocking yourself

### Option C: Hybrid — Custom Bot + Anthropic Financial Services Plugin Architecture

```
Claude Code ──→ financial-analysis plugin (MCP connectors)
     │                    │
     ├──→ weather-trading plugin (custom)
     │         ├── skills/weather-arbitrage.md
     │         ├── skills/noaa-forecast.md
     │         ├── skills/risk-management.md
     │         └── commands/scan.md, trade.md, positions.md
     │
     └──→ py-clob-client ──→ Polymarket CLOB on Polygon
```

**Pros:** Leverages Anthropic's plugin structure for organized workflows, can integrate financial data MCP connectors (Morningstar, S&P Global, FactSet) for broader market intelligence, extensible to non-weather markets
**Cons:** Requires Claude Code / Cowork environment, more setup overhead

---

## 3. Integrating Anthropic Financial Services Plugins

The [`anthropics/financial-services-plugins`](https://github.com/anthropics/financial-services-plugins) repo provides a battle-tested plugin architecture that can be adapted for prediction market trading. Here's what's relevant and how to use it.

### What We Can Borrow

| Component | From the Repo | Our Use Case |
|-----------|--------------|--------------|
| **Plugin structure** | `plugin.json` manifest, `skills/`, `commands/` layout | Structure our weather bot as a proper Claude plugin |
| **MCP connectors** | `.mcp.json` with 11 financial data providers | Add weather data sources (NOAA, Open-Meteo) as MCP connectors |
| **Skill format** | Markdown-based skills with trigger conditions | Encode weather arbitrage logic, risk rules, NOAA parsing as skills |
| **Slash commands** | `/comps`, `/dcf`, `/earnings` patterns | Create `/scan`, `/trade`, `/positions`, `/pnl` commands |
| **Financial analysis core** | Modeling tools, QC workflows | Reuse for portfolio tracking, P&L analysis, position sizing |
| **Wealth management plugin** | Portfolio rebalancing, risk assessment | Adapt for weather portfolio exposure management |

### Custom Plugin Structure

```
weather-trading/
├── .claude-plugin/
│   └── plugin.json              # Manifest declaring skills + commands
├── .mcp.json                    # NOAA API, Open-Meteo, Polymarket as connectors
├── commands/
│   ├── scan.md                  # /scan [city] — find mispriced weather buckets
│   ├── trade.md                 # /trade [market_id] [side] [amount]
│   ├── positions.md             # /positions — show open weather positions
│   ├── pnl.md                   # /pnl — daily/weekly profit & loss summary
│   └── forecast.md              # /forecast [city] [date] — show NOAA vs market
├── skills/
│   ├── weather-arbitrage.md     # Core strategy: NOAA vs bucket pricing logic
│   ├── noaa-data-pipeline.md    # How to fetch/parse NOAA forecast data
│   ├── ensemble-probability.md  # GFS ensemble → temperature bucket probabilities
│   ├── polymarket-execution.md  # Order placement, CLOB mechanics, signing
│   ├── risk-management.md       # Position sizing, Kelly criterion, circuit breakers
│   ├── cloudflare-mitigation.md # Handling 403 blocks, retry logic, proxy setup
│   └── resolution-rules.md     # Which weather stations resolve which markets
└── scripts/
    ├── weather_scanner.py       # Main scanning loop
    ├── noaa_client.py           # NOAA Weather API wrapper
    ├── probability_engine.py    # Ensemble → probability calculations
    ├── polymarket_trader.py     # py-clob-client wrapper with retry logic
    ├── risk_manager.py          # Position limits, exposure tracking
    └── config.py                # Thresholds, cities, intervals
```

### MCP Configuration (`.mcp.json`)

```json
{
  "mcpServers": {
    "noaa-weather": {
      "url": "https://api.weather.gov",
      "description": "NOAA National Weather Service — free, no API key",
      "note": "Requires User-Agent header; rate limited to ~30 req/min"
    },
    "open-meteo-ensemble": {
      "url": "https://ensemble-api.open-meteo.com/v1/ensemble",
      "description": "GFS 31-member ensemble forecasts — free, no API key"
    },
    "polymarket-clob": {
      "url": "https://clob.polymarket.com",
      "description": "Polymarket CLOB API for order execution",
      "note": "Requires Cloudflare-friendly IP; L2 auth with HMAC-SHA256"
    },
    "polymarket-gamma": {
      "url": "https://gamma-api.polymarket.com",
      "description": "Market discovery and metadata"
    }
  }
}
```

---

## 4. Phased Development Plan

### Phase 1: Research & Paper Trading (Week 1)

**Goal:** Validate the strategy with zero money at risk.

**Tasks:**

- [ ] Set up Python environment (`py-clob-client`, `requests`, `scipy`, `web3==6.14.0`)
- [ ] Build NOAA data pipeline — fetch hourly forecasts for NYC, Chicago, Dallas, Seattle, Atlanta, Miami
- [ ] Build probability engine — convert point forecasts to bucket probabilities using normal distribution (σ=2°F)
- [ ] Scrape active Polymarket weather markets via Gamma API
- [ ] Compare model probabilities vs market prices — log "would-trade" signals for 7 days
- [ ] Track hypothetical P&L in a spreadsheet/CSV
- [ ] **Decision gate:** If paper win rate < 50% over 50+ signals → stop and rethink

**Key Code — NOAA Forecast Fetch:**

```python
import requests

def get_noaa_forecast(lat: float, lon: float) -> dict:
    """Fetch hourly temperature forecast from NOAA."""
    headers = {"User-Agent": "(weather-bot, your@email.com)"}

    # Step 1: Get grid coordinates
    points = requests.get(
        f"https://api.weather.gov/points/{lat},{lon}",
        headers=headers
    ).json()

    forecast_url = points["properties"]["forecastHourly"]

    # Step 2: Get hourly forecast
    forecast = requests.get(forecast_url, headers=headers).json()
    return forecast["properties"]["periods"]
```

**Key Code — Bucket Probability Estimation:**

```python
from scipy.stats import norm

def bucket_probability(forecast_high: float, bucket_low: float,
                       bucket_high: float, sigma: float = 2.0) -> float:
    """Probability that actual high falls within a temperature bucket."""
    return norm.cdf(bucket_high, forecast_high, sigma) - \
           norm.cdf(bucket_low, forecast_high, sigma)

# Example: NOAA says NYC high = 48°F
# What's the probability it lands in 46-48°F bucket?
prob = bucket_probability(48, 46, 48, sigma=2.0)
# → 0.34 (34%)
# If market prices this at $0.12, edge = 0.34 - 0.12 = 0.22 → BUY
```

### Phase 2: Wallet Setup & Micro-Trading (Week 2)

**Goal:** Execute first real trade with $1.

**Tasks:**

- [ ] Create a **dedicated bot wallet** (never use your main wallet)
- [ ] Fund with $5 USDC.e + 0.5 POL on Polygon network
- [ ] Set token allowances (USDC.e → CTF Exchange, CTF → CTF Exchange)
- [ ] Generate Polymarket L2 API credentials via `create_or_derive_api_creds()`
- [ ] Execute a single manual $1 trade via `py-clob-client` to verify pipeline
- [ ] Measure actual slippage vs paper assumptions
- [ ] Implement heartbeat (required: every 10 seconds for open orders)
- [ ] Handle Cloudflare 403 errors with exponential backoff + residential proxy

**Wallet Funding Path (Cheapest):**

```
1. Buy USDC on Coinbase/Kraken
2. Withdraw USDC to Polygon network (direct, ~$0-1 fee)
3. Bridge arrives as USDC.e in your bot wallet
4. Buy 0.5 POL on a DEX (Uniswap/QuickSwap) or withdraw from exchange
```

**Budget Allocation ($50):**

| Item | Amount | Purpose |
|------|--------|---------|
| USDC.e trading capital | $43 | Actual bets (21x $2 max positions) |
| POL gas reserve | $2 | Months of Polygon transactions |
| Withdrawal fee buffer | $1 | 1.5% Polymarket withdrawal fee |
| Slippage/learning losses | $4 | Expected early mistakes |

### Phase 3: Automated Bot Deployment (Week 3)

**Goal:** Bot runs autonomously, scanning and trading every 30 minutes.

**Tasks:**

- [ ] Integrate scanner → probability engine → trader into single pipeline
- [ ] Implement risk manager:
  - Max $2 per position
  - Max 5 open positions simultaneously
  - Max 30% of bankroll exposed at once
  - Auto-pause if daily P&L drops below -$5
  - Quarter-Kelly position sizing
- [ ] Add logging: every scan, signal, trade, and error to `trades.log`
- [ ] Set up cron job or systemd service (scan every 30 min)
- [ ] Implement Telegram/Discord alerts for trade execution
- [ ] Deploy on VPS ($5/month Hetzner) or Raspberry Pi
- [ ] Monitor for 7 days with $1 max trades before scaling

**Risk Parameters Config:**

```python
CONFIG = {
    "entry_threshold": 0.08,     # Min 8% edge to enter
    "exit_threshold": 0.45,      # Sell when bucket price > 45¢
    "max_position_usd": 2.00,    # Max $2 per bucket
    "max_open_positions": 5,     # Max 5 concurrent positions
    "max_exposure_pct": 0.30,    # Max 30% of bankroll at risk
    "daily_loss_limit": -5.00,   # Auto-pause if -$5 on the day
    "kelly_fraction": 0.25,      # Quarter-Kelly sizing
    "scan_interval_min": 30,     # Scan every 30 minutes
    "min_hours_to_resolution": 6,# Don't trade < 6hrs before resolution
    "cities": ["NYC", "Chicago", "Seattle", "Atlanta", "Dallas", "Miami"],
    "sigma": 2.0,                # Forecast uncertainty (°F)
}
```

### Phase 4: Optimization & Scaling (Week 4+)

**Goal:** Improve edge, expand to more markets, consider plugin architecture.

**Tasks:**

- [ ] Upgrade from point forecasts to **GFS 31-member ensemble** (Open-Meteo API)
- [ ] Backtest against historical market data
- [ ] Add precipitation and wind markets (if profitable)
- [ ] Implement temperature laddering strategy (multi-bucket micro-bets)
- [ ] Build Claude plugin structure (Option C) for natural language portfolio management
- [ ] Integrate Anthropic financial-services-plugins for:
  - Portfolio analytics (P&L tracking, Sharpe ratio, drawdown analysis)
  - Automated reporting (daily performance summaries)
  - Risk dashboards
- [ ] Scale position sizes based on tracked performance
- [ ] Add London, Seoul, Tokyo weather markets for 24/7 coverage

---

## 5. Key Technical Gotchas

### Cloudflare Blocking (The #1 Pain Point)

Polymarket's CLOB API sits behind Cloudflare, which intermittently blocks POST requests from datacenter and some residential IPs with 403 errors. This is the most commonly reported issue on GitHub (Issue #143 on `py-clob-client`).

**Mitigations:**
- Use residential proxy with rotation (IPRoyal, BrightData)
- Implement exponential backoff: wait 1s, 2s, 4s, 8s between retries
- Route through Cloudflare Workers for better trust scoring
- Contact Polymarket Discord support with your Cloudflare Ray ID for whitelisting
- Set `CLOB_MAX_RETRIES=10` in environment

### API Heartbeat Requirement

If using GTC/GTD orders, you **must** send a heartbeat to the CLOB every 10 seconds (5-second grace period). Failure auto-cancels all open orders. Implement as a background thread or async task.

### Resolution Station Data

Markets resolve based on specific weather station readings, not NOAA forecasts:
- **NYC** → NOAA Central Park station
- **London** → Weather Underground, London City Airport (EGLC)
- Station outages or delayed reporting can cause unexpected resolutions
- Avoid trading within 6 hours of resolution when this risk peaks

### Web3 Dependency Pinning

Pin `web3==6.14.0` explicitly. Newer versions introduce breaking dependency conflicts with `py-clob-client`. This will save you hours of debugging.

---

## 6. Monitoring & Success Metrics

### Track These Numbers Daily

| Metric | Target | Red Flag |
|--------|--------|----------|
| Win rate | > 55% | < 45% over 50+ trades |
| Average edge per trade | > 5% | < 2% consistently |
| Daily P&L | Positive trend | 3+ consecutive losing days |
| Slippage vs expected | < 2% deviation | > 5% systematic slippage |
| Cloudflare block rate | < 10% of requests | > 30% blocks |
| Scan-to-trade latency | < 5 seconds | > 30 seconds |

### Decision Gates

- **After 50 trades:** If win rate < 50% → stop live trading, return to paper trading
- **After 100 trades:** If cumulative P&L negative → reassess strategy fundamentals
- **After $10 profit:** Consider increasing max position to $5
- **After $25 profit:** Consider increasing bankroll to $100

---

## 7. Legal & Regulatory Considerations

- **US residents:** Polymarket Global is blocked; must use Polymarket US (CFTC-regulated, KYC required, launched Dec 2025) via FCM intermediaries
- **Blocked countries:** 33+ countries restricted; VPN use violates ToS and risks fund freezes
- **Tax obligations:** Prediction market winnings are generally taxable income; consult a tax professional
- **Bot-friendliness:** Polymarket explicitly supports programmatic trading and publishes open-source bot frameworks

---

## 8. Repository & Resources

### Essential GitHub Repos

| Repo | Stars | What It Does |
|------|-------|-------------|
| [`Polymarket/py-clob-client`](https://github.com/Polymarket/py-clob-client) | — | Official Python client for Polymarket CLOB API |
| [`Polymarket/agents`](https://github.com/Polymarket/agents) | 1.7K | Official autonomous trading agent framework |
| [`anthropics/financial-services-plugins`](https://github.com/anthropics/financial-services-plugins) | 6.2K | Claude plugin architecture for financial workflows |
| [`solship/Polymarket-Weather-Trading-Bot`](https://github.com/solship/Polymarket-Weather-Trading-Bot) | 66 | TypeScript weather bot with NWS integration |
| [`suislanchez/polymarket-kalshi-weather-bot`](https://github.com/suislanchez/polymarket-kalshi-weather-bot) | — | Multi-platform bot with GFS ensemble + Kelly criterion |
| [`chainstacklabs/polyclaw`](https://github.com/chainstacklabs/polyclaw) | — | OpenClaw skill for Polymarket trading |
| [`SpartanLabsXyz/simmer-sdk`](https://github.com/SpartanLabsXyz/simmer-sdk) | — | Python SDK for Simmer prediction market platform |

### APIs (All Free)

| API | URL | Auth |
|-----|-----|------|
| NOAA Weather Service | `api.weather.gov` | User-Agent header only |
| Open-Meteo GFS Ensemble | `ensemble-api.open-meteo.com` | None |
| Polymarket Gamma (markets) | `gamma-api.polymarket.com` | None |
| Polymarket CLOB (trading) | `clob.polymarket.com` | L1/L2 wallet-based auth |

### Documentation

- [Polymarket CLOB API docs](https://docs.polymarket.com)
- [NOAA Weather API docs](https://www.weather.gov/documentation/services-web-api)
- [Anthropic Financial Plugins README](https://github.com/anthropics/financial-services-plugins)
- [simmer-weather skill reference](https://playbooks.com/skills/openclaw/skills/simmer-weather)

---

## 9. Honest Expected Outcomes

### With $50 Over 3 Months

| Scenario | Probability | Outcome |
|----------|------------|---------|
| Lose most/all of $50 | ~40% | Common for first-time prediction market traders |
| Break even (±$10) | ~30% | Bot works but edge is razor-thin after fees/slippage |
| Modest profit ($10–50) | ~25% | Strategy works; validates scaling potential |
| Significant profit ($50+) | ~5% | Outlier; would require favorable market conditions + strong execution |

### The Real Value

Even in the losing scenarios, you gain practical experience with blockchain trading infrastructure, API integration, probability modeling, and automated systems — skills worth far more than $50 on the job market.

---

*Last updated: March 2026 · Created as a learning project — not financial advice*
